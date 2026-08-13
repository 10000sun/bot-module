"""봇 클라이언트 본체. 생존 감시(하트비트), 전역 에러 핸들러, 코그 등록을 담당해요."""

import asyncio
import importlib
import os
import traceback
import datetime as dt
import discord
from discord import app_commands
from discord.ext import commands, tasks

from mari_config import ALERT_DISCONNECT_SECONDS, ENABLED_MODULES, HEARTBEAT_FILE, KST
from mari_alerts import send_alert
from mari_utils import describe_user_error
from modules import resolve_modules

# 🧩 [변경] 예전엔 여기 `from cogs.X import Y` 17줄이 있었고, setup_hook에는 add_cog 17줄이
# 짝을 이뤄 있었어요. 기능 하나를 빼려면 두 곳을 같이 고쳐야 했고, 무엇보다 **한 코그의
# import가 실패하면 봇이 기동조차 못 했습니다.**
#
# 이제 modules.py의 목록을 보고 importlib으로 하나씩 불러와요. 덕분에
#   ① 담을 기능을 guild.json에서 고를 수 있고,
#   ② 한 모듈이 터져도 나머지는 정상적으로 뜹니다. (무엇이 실패했는지는 크게 알려요)

# ========== 🤖 봇 클라이언트 정의 ==========
class MariBotClient(commands.Bot):
    def __init__(self, *, command_prefix: str, intents: discord.Intents):
        super().__init__(command_prefix=command_prefix, intents=intents)
        self.synced_once = False
        # 🧩 이번 기동에 실제로 올라간/실패한 모듈. 진단 명령에서 확인할 수 있어요.
        self.loaded_modules = []
        self.failed_modules = []  # [(모듈 키, 실패 사유)]
        self.economy_lock = asyncio.Lock()
        # 🔒 [신규] settings.json은 채널/역할 설정뿐 아니라 레벨 명단 메세지 ID까지 같이
        # 들고 있는데, 여러 등록이 거의 동시에 일어나면 "읽고→고치고→저장" 과정이 겹쳐서
        # 서로 덮어써버리는 경합(race condition)이 생길 수 있어요. 이걸 막는 잠금이에요.
        self.settings_lock = asyncio.Lock()
        # 🏠 [신규] 모든 슬래시 명령어를 서버 전용으로. (자세한 설명은 _enforce_guild_only 참고)
        # 여기 런타임 검사는 "동기화가 아직 안 퍼진 사이"와 혹시 빠뜨린 경로를 막는 2차 방어선이에요.
        self.tree.interaction_check = self._guild_only_check
        # 🚨 [추가] 슬래시 명령어 전역 에러 핸들러 등록
        # 기존에는 각 명령어의 try/except가 못 잡는 예외(예: 권한 체크 실패,
        # 파라미터 변환 오류, 예상 못한 버그)가 발생하면 유저에게는 그냥
        # "상호작용에 실패했습니다"만 보이고 원인 로그도 남지 않았어요.
        # 여기서 한 곳에 모아 처리하면 원인 파악과 유저 안내가 훨씬 쉬워집니다.
        self.tree.on_error = self.on_app_command_error

        # 💓 [신규] 다운 알림용 상태값
        self._startup_alert_sent = False      # 기동 알림은 재연결 때마다가 아니라 딱 한 번만
        self._disconnected_since = None       # 게이트웨이가 끊긴 시각(UTC)
        self._alerted_disconnect = False      # 이번 끊김에 대해 이미 알렸는지

    # ========== 💓 [신규] 생존 감시 및 다운 알림 ==========

    async def on_ready(self):
        # 재시작이 반복되면 이 알림이 계속 오므로, 크래시 루프를 바로 알아챌 수 있어요.
        if not self._startup_alert_sent:
            self._startup_alert_sent = True
            await send_alert(
                "✅ 마리봇이 정상 기동했어요",
                f"계정: `{self.user}` (ID: `{self.user.id}`)\n"
                f"참여 서버: {len(self.guilds)}개\n"
                f"기동 시각: {dt.datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')} KST",
                color=0x2ECC71,
            )
        await self._mark_connected()
        if not self.heartbeat_loop.is_running():
            self.heartbeat_loop.start()

    async def on_disconnect(self):
        # ⚠️ 이 이벤트는 정상적인 짧은 재연결에서도 자주 발생해요.
        # 그래서 여기서 바로 알리지 않고 시각만 기록해두고, heartbeat_loop이
        # "충분히 오래 끊겨 있는지" 판단한 뒤에 알립니다.
        if self._disconnected_since is None:
            self._disconnected_since = dt.datetime.now(dt.timezone.utc)

    async def on_resumed(self):
        await self._mark_connected()

    async def _mark_connected(self):
        """연결이 회복됐을 때 상태를 리셋하고, 끊김을 알렸던 경우에만 복구 알림을 보내요."""
        was_alerted = self._alerted_disconnect
        down_since = self._disconnected_since
        self._disconnected_since = None
        self._alerted_disconnect = False
        if was_alerted and down_since is not None:
            seconds = int((dt.datetime.now(dt.timezone.utc) - down_since).total_seconds())
            await send_alert(
                "🟢 마리봇 연결이 복구됐어요",
                f"약 {seconds}초 만에 디스코드에 다시 연결됐어요.",
                color=0x2ECC71,
            )

    @tasks.loop(seconds=30)
    async def heartbeat_loop(self):
        # 1) 심장박동 파일 기록
        # 정전이나 강제 종료(kill -9)처럼 봇이 스스로 알림을 보낼 틈도 없이 죽는 경우가 있어요.
        # 그때는 외부 감시 도구가 이 파일의 수정 시각을 보고 "몇 분째 안 뛴다"를 감지해야 합니다.
        try:
            with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                f.write(f"{dt.datetime.now(KST).isoformat()}\n{os.getpid()}\n")
        except Exception:
            pass  # 심장박동 기록 실패가 봇 동작을 막아서는 안 돼요

        # 2) 장시간 연결 끊김 감지
        # 게이트웨이가 끊겨도 웹훅은 별도 HTTPS 요청이라 정상적으로 전송됩니다.
        if self._disconnected_since is not None and not self._alerted_disconnect:
            down = (dt.datetime.now(dt.timezone.utc) - self._disconnected_since).total_seconds()
            if down >= ALERT_DISCONNECT_SECONDS:
                self._alerted_disconnect = True
                await send_alert(
                    "⚠️ 마리봇 연결이 끊겼어요",
                    f"{int(down)}초째 디스코드 게이트웨이에 연결되지 않고 있어요.\n"
                    "네트워크 문제이거나 디스코드 장애일 수 있어요. 자동 재연결을 계속 시도 중입니다.",
                    color=0xF39C12,
                )

    @heartbeat_loop.error
    async def heartbeat_loop_error(self, error: BaseException):
        # 감시 루프가 예외로 멈춰버리면 "조용히 감시가 꺼진" 최악의 상태가 되므로 다시 살립니다.
        print(f"❗ 심장박동 루프 오류: {type(error).__name__}: {error}")
        if not self.heartbeat_loop.is_running():
            self.heartbeat_loop.start()

    # ========== 🏠 [신규] 모든 명령어를 서버 전용으로 ==========

    async def _guild_only_check(self, interaction: discord.Interaction) -> bool:
        """DM에서 들어온 슬래시 명령어를 실행 전에 돌려보냅니다. (tree.interaction_check로 등록)

        아래 _enforce_guild_only가 디스코드 쪽에 "이 명령어는 서버 전용"이라고 등록해두지만,
        그 정보가 클라이언트에 퍼지기까지는 시간이 걸려요. 그 사이에 이미 DM 목록에 떠 있던
        명령어를 누르면 그대로 들어옵니다. 그때 여기서 막습니다.
        """
        if interaction.guild is None:
            try:
                await interaction.response.send_message(
                    "🙅‍♀️ 마리 명령어는 서버 안에서만 쓸 수 있어요! 서버에 와서 불러주세요.",
                    ephemeral=True,
                )
            except Exception:
                pass    # 이미 응답이 나갔거나 만료된 경우 — 어차피 아래에서 실행을 막아요
            return False
        return True

    def _enforce_guild_only(self):
        """등록된 **모든** 최상위 명령어를 서버에서만 호출 가능하게 만듭니다. (동기화 직전에 호출)

        화면에 무슨 문구를 띄우는 게 아니라, 디스코드에 명령어를 등록할 때 같이 보내는
        플래그(dm_permission / allowed_contexts)를 끄는 거예요. 그러면 DM 명령어 목록에
        아예 뜨지 않고 호출 자체가 되지 않습니다.

        🐛 [버그 수정] 예전엔 명령어마다 @app_commands.guild_only()를 손으로 붙였는데, 두 가지가
        문제였어요.
          ① 붙이는 걸 잊은 명령어가 훨씬 많았습니다. 실제로 서버 전용이던 건 /캠프·/테스트·
             /기능제어 3개뿐이고 나머지 27개는 DM에서 그대로 열렸어요. 대부분 interaction.guild가
             None이라 누르면 그냥 터졌습니다.
          ② 그룹 **안쪽** 하위 명령어(/아이디 조회, /상점 항목추가 …)에 붙인 데코레이터는 사실
             아무 효과가 없어요. 디스코드는 이 플래그를 최상위 명령어에서만 받기 때문에,
             붙여둔 걸 보고 막혔다고 착각하기 딱 좋았습니다.
        이제 여기서 트리를 통째로 훑어 한 번에 걸어요. **새 명령어를 추가해도 자동으로 포함되니
        따로 데코레이터를 붙일 필요가 없습니다.**
        """
        count = 0
        for command in self.tree.get_commands():
            try:
                command.guild_only = True
                # discord.py 2.4부터는 dm_permission 대신 allowed_contexts를 씁니다.
                # 버전에 따라 이 속성이 아예 없거나 None일 수 있어서, 있을 때만 손댑니다.
                contexts = getattr(command, "allowed_contexts", None)
                if contexts is not None:
                    contexts.guild = True
                    contexts.dm_channel = False
                    contexts.private_channel = False
                count += 1
            except Exception as e:
                # 한 명령어의 속성 설정 실패가 봇 기동 전체를 막아서는 안 돼요.
                # (런타임 검사인 _guild_only_check가 어차피 한 번 더 걸러줍니다)
                print(f"⚠️ '{getattr(command, 'name', '?')}' 서버 전용 설정 실패: {type(e).__name__}: {e}")
        print(f"🏠 명령어 {count}개를 서버 전용으로 설정했어요. (DM에서는 보이지 않아요)")

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        # 각 명령어 내부에서 이미 처리한 뒤 다시 올라온 예외는 조용히 무시
        if isinstance(error, app_commands.CommandInvokeError):
            original = error.original
        else:
            original = error

        if isinstance(error, app_commands.CheckFailure):
            msg = "⛔ 이 명령어를 사용할 권한이 없어요."
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ 잠시 후 다시 시도해 주세요! ({error.retry_after:.1f}초 남음)"
        else:
            # 💾 저장 실패·파일 손상 안내는 버튼(MariView.on_error)과 문구를 공유합니다.
            msg = describe_user_error(original)

        print(f"❗ [전역 에러] /{getattr(interaction.command, 'qualified_name', '알수없음')} 처리 중 오류: "
              f"{type(original).__name__}: {original}")

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception as e:
            print(f"❗ 에러 메시지 전송 실패: {e}")

    async def load_modules(self):
        """modules.py 목록을 보고 코그를 하나씩 불러옵니다.

        🛡️ 한 모듈이 터져도 나머지는 계속 올려요. 예전엔 정적 import라 코그 하나가
        ImportError를 내면 봇이 통째로 안 켜졌습니다. 기능을 골라 담는 구조에서는
        "방금 넣은 모듈 하나 때문에 전부 죽는" 게 특히 곤란해요.
        """
        specs, warnings = resolve_modules(ENABLED_MODULES)
        for warning in warnings:
            print(f"🧩 {warning}")

        for spec in specs:
            try:
                cog_cls = getattr(importlib.import_module(spec.module), spec.cog)
                await self.add_cog(cog_cls(self))
                self.loaded_modules.append(spec.key)
            except Exception as e:
                self.failed_modules.append((spec.key, f"{type(e).__name__}: {e}"))
                print(f"❗ '{spec.label}'({spec.key}) 모듈을 불러오지 못했어요: {type(e).__name__}: {e}")
                traceback.print_exc()

        loaded = ", ".join(self.loaded_modules) or "없음"
        print(f"🧩 모듈 {len(self.loaded_modules)}/{len(specs)}개 로드 완료: {loaded}")
        if self.failed_modules:
            # 실패를 조용히 넘기면 "명령어가 왜 안 보이지"로 며칠을 헤매게 돼요.
            print(f"🚨 실패한 모듈 {len(self.failed_modules)}개: "
                  f"{', '.join(key for key, _ in self.failed_modules)}")

    async def setup_hook(self):
        await self.load_modules()

        # 🗑️ [정리] 예전엔 여기에 상점 지속성 버튼(Persistent View) 등록 코드가 있었어요.
        # 클라이언트 본체가 상점의 내부 구조(매대 목록, 견학권 DM 버튼)를 알아야 해서,
        # 상점을 빼고 납품하면 이 코드가 갈 곳을 잃었습니다. 이제 MariShop.cog_load()가
        # 스스로 등록해요. 클라이언트는 특정 모듈을 전혀 모릅니다.

        # 🐛 [버그 수정] 예전엔 이 동기화 블록이 `if shop_cog:` 안에 들여쓰기되어 있었어요.
        # 그래서 상점 코그 로드가 실패하면 상점뿐 아니라 봇의 "모든" 슬래시 명령어가
        # 통째로 동기화되지 않는 상태가 됐어요. 이제 상점과 무관하게 항상 실행됩니다.
        # 🏠 동기화 직전에 모든 명령어를 서버 전용으로 못박습니다.
        # 반드시 add_cog가 전부 끝난 뒤·sync보다 앞이어야 해요. (그래야 새로 추가한 명령어도 포함되고,
        # 그 설정이 이번 동기화에 같이 실려 나갑니다)
        self._enforce_guild_only()

        if not self.synced_once:
            try:
                await self.tree.sync()
                self.synced_once = True
                print("🔁 setup_hook: 글로벌 슬래시 명령 동기화 완료")
            except Exception as e:
                print("❗ setup_hook 동기화 오류:", type(e).__name__, repr(e))
