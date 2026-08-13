"""MariGames — 하이로우 게임, 에바시 선착순 이벤트, 고성능 확성기."""

import random
import asyncio
import os
import traceback
import datetime as dt
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands, tasks

from mari_config import GOHWAK_MENTION_ROLE_ID, KST, SHOP_FILE
from mari_alerts import report_loop_error, send_alert
from mari_storage import atomic_json_save_or_raise, safe_json_load
from mari_state import record_ledger
from mari_utils import chunk_lines
from mari_settings import has_admin_or_role, load_settings, save_settings, send_log_embed

class MariGames(commands.Cog):
    """하이로우 등 미니게임 시스템"""

    def __init__(self, bot):
        # 🐛 [버그 수정] 이 코그에는 원래 __init__이 없어서 self.bot이 보장되지 않았어요
        # (그래서 고확 명령어에 getattr(self, "bot", None) 방어 코드가 있었던 거예요).
        # 이제 정식으로 저장해서 에바시 기능(백그라운드 스케줄러, on_message)에서도 안전하게 씁니다.
        self.bot = bot

        # 🎲 하이로우 게임 상태 (메모리 전용)
        # 🗑️ [정리] 예전엔 current_numbers를 ids.json에 저장했어요. 그런데 아래 active_games는
        # 애초에 저장되지 않아서 재시작하면 항상 비어 있고, process_guess가 이걸로 먼저 막기
        # 때문에 재시작 후엔 반드시 /하이로우가 먼저 실행돼 숫자를 덮어써요. 즉 파일에서
        # 읽어온 값이 쓰일 수 있는 경로가 아예 없는, 완전히 죽은 영속화였습니다.
        # 그런데 그것 때문에 /하이·/로우를 칠 때마다 유저 전체의 아이디 DB가 통째로
        # 다시 저장되고 있었어요. 이제 게임 상태는 이 코그가 메모리에서만 들고 있습니다.
        self.current_numbers = {}   # {guild_id: 현재 숫자}
        self.active_games = set()   # 하이로우가 시작된 guild_id

        # 🎉 에바시 이벤트 상태
        self.evashi_window_open_until = None  # 이 시각까지는 "에바시"를 치면 보상을 받을 수 있음 (KST datetime)
        self.evashi_participants = set()      # 이번 창(window)에서 이미 보상을 받은 유저 ID (선착순+나머지 전부)
        self.evashi_first_claimed = 0         # 이번 창에서 "선착순" 보상을 받은 인원 수
        self.evashi_first_winners = []        # 이번 창에서 선착순 보상을 받은 유저 ID 목록
        self.evashi_guild = None              # 이번 창에서 첫 참가자가 나온 길드 (마감 공지 보낼 곳)
        self.evashi_close_task = None         # 창을 정확히 window_seconds 뒤에 닫고 결과를 공지하는 예약 태스크

        # 🐛 [버그 수정] 예전엔 여기 self._shop_file_lock = asyncio.Lock()으로 **이 코그 전용**
        # 잠금을 따로 만들어 썼어요. 그런데 shop.json을 쓰는 다른 기능(상점 구매·되팔기·선물,
        # 견학권 차감)은 전부 bot.economy_lock을 잡습니다. 서로 다른 잠금이라 상대를 전혀 막지
        # 못해서, /고확이 확성기를 깎는 사이 역할 아이템 구매가 shop.json을 통째로 덮어쓰면
        # (구매는 add_roles를 await 하는 동안 파일 사본을 들고 있어요) 확성기 차감이 되돌려져요.
        # 즉 확성기 하나로 방송을 여러 번 할 수 있었습니다.
        # 이제 shop.json을 건드리는 모든 코드가 같은 잠금(economy_lock) 하나를 지나갑니다.
        self.evashi_loop.start()

    def cog_unload(self):
        self.evashi_loop.cancel()
        if self.evashi_close_task and not self.evashi_close_task.done():
            self.evashi_close_task.cancel()

    # ---------- 🎉 에바시 설정 로드/저장 ----------
    def _load_evashi_settings(self) -> dict:
        settings = load_settings()
        defaults = {"first_count": 3, "first_amount": 12210, "rest_amount": 1221, "window_seconds": 60}
        evashi = settings.get("evashi", {})
        for k, v in defaults.items():
            evashi.setdefault(k, v)
        return evashi

    def _save_evashi_settings(self, evashi: dict):
        settings = load_settings()
        settings["evashi"] = evashi
        save_settings(settings)

    def _is_evashi_admin(self, interaction: discord.Interaction) -> bool:
        return has_admin_or_role(interaction, "evashi_admin")

    async def _send_evashi_announce(self, guild: discord.Guild, text: str):
        """마리 전용 에바시 '안내 채널'로 공지를 보냅니다. (유저가 '에바시'를 치는 채널이 아니라
        별도로 설정해둔, 마리가 결과를 알려주는 전용 채널이에요.)"""
        ch_id = load_settings().get("channels", {}).get("evashi_announce")
        if not ch_id:
            print("⚠️ [에바시] 안내 채널이 설정 안 돼있어서 결과 공지를 못 올렸어요. `/설정 채널 에바시`로 지정해주세요.")
            return
        channel = guild.get_channel(ch_id) if guild else self.bot.get_channel(ch_id)
        if not channel:
            print(f"⚠️ [에바시] 안내 채널(id={ch_id})을 찾을 수 없어서 결과 공지를 못 올렸어요.")
            return
        # 🐛 [버그 수정] 참가자를 전부 멘션하다 보니 사람이 많이 몰린 회차에서는 본문이
        # 디스코드 한 메세지 제한(2000자)을 넘겨 전송이 통째로 실패했어요. 멘션 하나가 22자쯤
        # 되니 90명 남짓이면 걸립니다. 참가자가 많을수록 결과 발표가 확실히 안 나가는 셈이라,
        # 제일 필요한 순간에 못 쓰는 상태였어요. 이제 넘치면 나눠서 보냅니다.
        for part in chunk_lines(text.split("\n"), limit=1900):
            try:
                await channel.send(part)
            except Exception as e:
                print(f"❗ 에바시 안내 채널 발송 실패: {e}")
                return

    async def _give_evashi_reward(self, user_id: int, amount: int, *, is_first: bool = False):
        """에바시 보상을 안전하게 지급합니다. (동시 지급 충돌 방지용 economy_lock 사용)"""
        economy_cog = self.bot.get_cog("MariEconomy")
        if not economy_cog:
            return
        async with self.bot.economy_lock:
            data = economy_cog._load_raw_economy()
            user_key = str(user_id)
            data[user_key] = data.get(user_key, 0) + amount
            economy_cog._save_raw_economy(data)
            balance_after = data[user_key]

        # 🧾 [버그 수정] 지갑에 돈이 들어오는데 원장에는 안 남고 있었어요. 송금·출석·상점·주식·
        # 캠프통장은 전부 남기는데 여기만 빠져서, 유저가 /지갑내역을 열면 에바시로 받은 돈만
        # 출처 없이 잔액에 얹혀 있었습니다. (락을 놓은 뒤에 기록해요)
        record_ledger(user_key, amount, balance_after, "에바시 보상",
                      "선착순" if is_first else "참여")

    @tasks.loop(time=[dt.time(hour=0, minute=21, tzinfo=KST), dt.time(hour=12, minute=21, tzinfo=KST)])
    async def evashi_loop(self):
        """🐛 [버그 수정] 예전엔 60초마다 폴링해서 시(hour)/분(minute)이 맞는지 확인하는 방식이라,
        타이밍에 따라 최대 59초까지 늦게 열릴 수 있었어요. 이제는 discord.py의 time= 스케줄링을 써서
        00:21, 12:21(KST) 정각에 정확하게 실행돼요."""
        # 🛡️ 설정 파일이 손상되면 load_settings()가 예외를 던져요. 그게 밖으로 새면
        # 루프가 영구히 멈춰서 에바시 이벤트가 다시는 안 열립니다. 이번 회차만 포기해요.
        try:
            evashi = self._load_evashi_settings()
        except Exception as e:
            print(f"❗ [에바시] 설정을 읽지 못해 이번 회차를 건너뛰어요: {type(e).__name__}: {e}")
            return

        self.evashi_participants = set()
        self.evashi_first_claimed = 0
        self.evashi_first_winners = []
        self.evashi_guild = None
        now = dt.datetime.now(KST)
        self.evashi_window_open_until = now + dt.timedelta(seconds=evashi["window_seconds"])
        print(f"🎉 [에바시] 이벤트 창 열림! {evashi['window_seconds']}초 동안 '에바시'를 치면 보상을 받아요.")

        # ⏰ [신규] window_seconds 뒤에 자동으로 창을 닫고, 그때까지 모인 참가자 전원을
        # 한 번에 모아서 공지해요. (더 이상 선착순 채워지는 순간에만 알리지 않아요)
        if self.evashi_close_task and not self.evashi_close_task.done():
            self.evashi_close_task.cancel()
        self.evashi_close_task = asyncio.create_task(self._close_evashi_window_later(evashi["window_seconds"]))

    @evashi_loop.before_loop
    async def before_evashi_loop(self):
        await self.bot.wait_until_ready()

    @evashi_loop.error
    async def evashi_loop_error(self, error: BaseException):
        await report_loop_error(self.evashi_loop, "에바시 이벤트", error)

    async def _close_evashi_window_later(self, window_seconds: int):
        """정확히 window_seconds가 지난 뒤 창을 닫고, 참가자 전원을 한 번에 공지합니다."""
        try:
            await self._close_evashi_window_now(window_seconds)
        except asyncio.CancelledError:
            raise       # 봇 종료/재시작으로 인한 정상적인 취소는 그대로 흘려보내요
        except Exception as e:
            # 🛡️ create_task로 띄운 태스크는 예외가 나도 아무 데도 안 뜨고 조용히 사라져요.
            # 그러면 참가자들은 결과 발표를 영영 못 받는데 아무도 이유를 모릅니다.
            print(f"❗ [에바시] 마감 공지 중 오류가 났어요: {type(e).__name__}: {e}")
            traceback.print_exception(type(e), e, e.__traceback__)

    async def _close_evashi_window_now(self, window_seconds: int):
        await asyncio.sleep(window_seconds)
        self.evashi_window_open_until = None

        if not self.evashi_participants or not self.evashi_guild:
            return  # 아무도 참가 안 했으면 조용히 넘어감

        evashi = self._load_evashi_settings()
        rest_ids = [uid for uid in self.evashi_participants if uid not in self.evashi_first_winners]

        # 📢 [버그 수정] 예전엔 사람이 몰릴 때마다 개인별로 로그가 따로 남아서 도배됐었어요.
        # 이제는 창이 닫히는 시점에 딱 한 번, 참가자 전원(선착순+나머지)을 모아서 공지해요.
        lines = [f"🎉 **에바시 이벤트 결과** (총 {len(self.evashi_participants)}명 참여)"]
        if self.evashi_first_winners:
            mentions = " ".join(f"<@{uid}>" for uid in self.evashi_first_winners)
            lines.append(f"🥇 선착순 {len(self.evashi_first_winners)}명: {mentions}\n└ 각 {evashi['first_amount']:,} 에바 지급")
        if rest_ids:
            mentions = " ".join(f"<@{uid}>" for uid in rest_ids)
            lines.append(f"🎊 참가 {len(rest_ids)}명: {mentions}\n└ 각 {evashi['rest_amount']:,} 에바 지급")

        await self._send_evashi_announce(self.evashi_guild, "\n\n".join(lines))

        await send_log_embed(
            self.bot, "economy_log", "에바시 이벤트 전체 결과 기록이에요.",
            fields=[
                ("총 참여 인원", f"{len(self.evashi_participants)}명", False),
                ("선착순", f"{len(self.evashi_first_winners)}명 · 각 {evashi['first_amount']:,} 에바", False),
                ("나머지 참가", f"{len(rest_ids)}명 · 각 {evashi['rest_amount']:,} 에바", False),
            ],
            guild=self.evashi_guild,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if self.evashi_window_open_until is None:
            return
        if message.content.strip() != "에바시":
            return

        now = dt.datetime.now(KST)
        if now > self.evashi_window_open_until:
            return  # 이벤트 창이 이미 닫혔음
        if message.author.id in self.evashi_participants:
            return  # 이미 이번 창에서 보상을 받은 유저 (중복 방지)

        if self.evashi_guild is None:
            self.evashi_guild = message.guild  # 마감 공지를 보낼 길드를 첫 참가자 기준으로 기억해둬요

        self.evashi_participants.add(message.author.id)
        evashi = self._load_evashi_settings()

        is_first = self.evashi_first_claimed < evashi["first_count"]
        if is_first:
            self.evashi_first_claimed += 1
            self.evashi_first_winners.append(message.author.id)
            amount = evashi["first_amount"]
        else:
            amount = evashi["rest_amount"]

        # 💰 보상 지급은 실시간으로 바로 해요. (공지/로그만 창이 닫힐 때 한 번에 모아서 나가요)
        await self._give_evashi_reward(message.author.id, amount, is_first=is_first)

        # 👍 [버그 수정] 도배를 막으려고 개인별 메세지/로그를 다 없앴더니, "에바시" 쳐도
        # 그 순간엔 아무 반응이 없어서 마치 안 되는 것처럼 보였어요. 새 메세지를 추가로
        # 보내지 않으면서도 즉시 확인할 수 있게, 본인이 친 메세지에 리액션만 살짝 달아줘요.
        try:
            emoji = "🥇" if amount == evashi["first_amount"] else "🎉"
            await message.add_reaction(emoji)
        except Exception:
            pass

    # ---------- 관리자 설정 명령어 ----------
    @app_commands.command(name="에바시설정", description="[관리자] 에바시 이벤트의 선착순 인원/금액을 설정합니다.")
    @app_commands.guild_only()
    @app_commands.describe(
        선착순인원="큰 보상을 받을 선착순 인원 수",
        선착순금액="선착순 인원에게 줄 금액",
        나머지금액="선착순 이후 참가자에게 줄 금액",
        지속시간초="이벤트 창이 열려있는 시간(초). 생략 시 기존 값 유지",
    )
    async def set_evashi_settings(
        self,
        interaction: discord.Interaction,
        선착순인원: Optional[int] = None,
        선착순금액: Optional[int] = None,
        나머지금액: Optional[int] = None,
        지속시간초: Optional[int] = None,
    ):
        if not self._is_evashi_admin(interaction):
            return await interaction.response.send_message("⛔ 권한이 없어요. 에바시 관리자만 설정할 수 있어요.", ephemeral=True)

        if 선착순인원 is None and 선착순금액 is None and 나머지금액 is None and 지속시간초 is None:
            evashi = self._load_evashi_settings()
            return await interaction.response.send_message(
                f"ℹ️ **현재 에바시 설정**\n"
                f"└ 선착순 인원: {evashi['first_count']}명\n"
                f"└ 선착순 금액: {evashi['first_amount']:,} 에바\n"
                f"└ 나머지 금액: {evashi['rest_amount']:,} 에바\n"
                f"└ 지속시간: {evashi['window_seconds']}초",
                ephemeral=True,
            )

        evashi = self._load_evashi_settings()
        logs = []
        if 선착순인원 is not None:
            if 선착순인원 < 0: return await interaction.response.send_message("❌ 선착순인원은 0 이상이어야 해요.", ephemeral=True)
            evashi["first_count"] = 선착순인원
            logs.append(f"선착순 인원: {선착순인원}명")
        if 선착순금액 is not None:
            if 선착순금액 < 0: return await interaction.response.send_message("❌ 선착순금액은 0 이상이어야 해요.", ephemeral=True)
            evashi["first_amount"] = 선착순금액
            logs.append(f"선착순 금액: {선착순금액:,} 에바")
        if 나머지금액 is not None:
            if 나머지금액 < 0: return await interaction.response.send_message("❌ 나머지금액은 0 이상이어야 해요.", ephemeral=True)
            evashi["rest_amount"] = 나머지금액
            logs.append(f"나머지 금액: {나머지금액:,} 에바")
        if 지속시간초 is not None:
            if 지속시간초 <= 0: return await interaction.response.send_message("❌ 지속시간초는 1 이상이어야 해요.", ephemeral=True)
            evashi["window_seconds"] = 지속시간초
            logs.append(f"지속시간: {지속시간초}초")

        self._save_evashi_settings(evashi)
        await interaction.response.send_message("✅ 에바시 설정을 업데이트했어요.\n" + "\n".join(f"└ {l}" for l in logs), ephemeral=True)

    @app_commands.command(name="하이로우", description="하이로우 게임 시작!")
    @app_commands.guild_only()
    async def highlow_intro(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        self.current_numbers[gid] = random.randint(1, 100)
        self.active_games.add(gid)
        embed = discord.Embed(title="🔮 마리의 하이로우 게임 안내!", color=discord.Color.blurple())
        embed.add_field(name="게임 방법", value="다음 숫자가 높을지/낮을지 맞혀보세요!", inline=False)
        embed.add_field(name="참여 방법", value="/하이 또는 /로우", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="하이", description="높을 것 같아요!")
    @app_commands.guild_only()
    async def guess_high(self, interaction: discord.Interaction):
        await self.process_guess(interaction, "하이")

    @app_commands.command(name="로우", description="낮을 것 같아요!")
    @app_commands.guild_only()
    async def guess_low(self, interaction: discord.Interaction):
        await self.process_guess(interaction, "로우")

    async def process_guess(self, interaction: discord.Interaction, guess: str):
        gid = interaction.guild.id
        if gid not in self.active_games:
            await interaction.response.send_message("먼저 /하이로우로 시작해요! 🎲", ephemeral=True)
            return

        cur = self.current_numbers[gid]
        nxt = random.randint(1, 100)
        win = (guess == "하이" and nxt > cur) or (guess == "로우" and nxt < cur)
        text = "✨ 정답이에요! 마리가 감동하며 윙크~ 😉✨" if win else "😭 틀렸어요... 마리가 삐죽! 다음엔 꼭 맞춰요!"
        embed = discord.Embed(title="💗 하이로우 결과!", color=discord.Color.pink())
        embed.add_field(name="현재 숫자", value=str(cur), inline=True)
        embed.add_field(name="예측", value=guess, inline=True)
        embed.add_field(name="다음 숫자", value=str(nxt), inline=True)
        embed.add_field(name="결과", value=text, inline=False)
        await interaction.response.send_message(embed=embed)
        self.current_numbers[gid] = nxt


    @app_commands.command(
        name="고확",
        description="인벤토리에 있는 확성기 아이템을 사용하여 전용 채널에 큰 글씨로 메시지를 전송합니다."
    )
    @app_commands.describe(내용="확성기로 방송할 내용 (줄바꿈은 \\n 을 입력하면 실제 줄바꿈으로 바뀌어요)")
    async def global_broadcast(self, interaction: discord.Interaction, 내용: str):
        # 1. 디스코드 '생각 중...' 응답 제한 시간 연장
        await interaction.response.defer(ephemeral=True)

        # 📝 [신규] 슬래시 명령어 입력창은 줄바꿈 키 입력이 안 되니까,
        # 사용자가 문자 그대로 "\n"을 타이핑하면 그걸 실제 줄바꿈으로 바꿔줍니다.
        내용 = 내용.replace("\\n", "\n")
        
        # 🐛 [버그 수정] 예전엔 여기서 `SHOP_FILE = "mari_shop.json"` 이라고 전역 상수를
        # 상대경로로 덮어쓰고 있었어요. 봇을 스크립트 폴더가 아닌 곳에서 실행하면
        # (작업 스케줄러/바로가기 등) 상점 파일을 못 찾거나, 최악의 경우 엉뚱한 위치에
        # 새 mari_shop.json을 만들어서 상점 데이터가 두 갈래로 쪼개졌어요.
        # 이제 전역의 절대경로 SHOP_FILE을 그대로 씁니다.
        ROLE_ID = GOHWAK_MENTION_ROLE_ID
        # ⚙️ 채널 설정 (동적 로드)

        TARGET_CHANNEL_ID = load_settings().get("channels", {}).get("global_broadcast")
        if not TARGET_CHANNEL_ID:
            return await interaction.followup.send("❌ 고확 채널이 설정되지 않았어요.", ephemeral=True)

        try:
            # 2. 🚨 [심각한 버그 수정] 예전엔 여기서 threading.Lock을 만들어 썼어요. 두 가지 문제가 있었습니다.
            #    ① 봇은 스레드가 아니라 비동기(코루틴)로 도는데, threading.Lock은 코루틴끼리는
            #       전혀 막아주지 못해서 "동기화한다"는 목적 자체가 사실상 무효였어요.
            #    ② 더 심각한 건, 이 잠금을 쥔 채로 `await`을 하고 있었다는 점이에요. 그 사이에
            #       다른 사람이 /고확을 쓰면 그 사람은 잠금을 기다리며 **이벤트 루프 자체를 멈춰버려요.**
            #       그러면 먼저 들어온 사람도 영영 재개되지 못해서 **봇 전체가 완전히 얼어붙습니다.**
            #       (재시작 말고는 풀 방법이 없어요)
            #    이제 asyncio.Lock을 쓰고, 잠금 안에서는 절대 await 하지 않도록 안내 메세지를
            #    변수에 담아뒀다가 잠금을 빠져나온 뒤에 보냅니다.
            # 3. 안전하게 파일 검증 및 확성기 차감 진행
            error_message = None
            async with self.bot.economy_lock:
                if not os.path.exists(SHOP_FILE):
                    error_message = f"❌ 상점 데이터 파일(`{SHOP_FILE}`)이 존재하지 않아요."

                if error_message is None:
                    data = safe_json_load(SHOP_FILE, {})

                    # 🏪 [다중 매대] '확성기' 아이템은 여러 매대 중 어느 곳에나 등록되어 있을 수 있어서,
                    # 모든 매대를 돌면서 이름에 '확성기'가 들어간 아이템 + 유저가 실제로 보유한 매대를 찾습니다.
                    user_id = str(interaction.user.id)
                    target_board = None
                    target_ch_id = None
                    target_item_id = None
                    found_any_launcher = False

                    for ch_id_str, board in data.get("boards", {}).items():
                        for item_id, item_info in board.get("items", {}).items():
                            if "확성기" in item_info.get("name", ""):
                                found_any_launcher = True
                                owned_qty = board.get("inventories", {}).get(user_id, {}).get(str(item_id), 0)
                                if owned_qty > 0:
                                    target_board = board
                                    target_ch_id = ch_id_str      # 되돌려줄 때 어느 매대인지 알아야 해요
                                    target_item_id = str(item_id)
                                    break
                        if target_board:
                            break

                    if not found_any_launcher:
                        error_message = "❌ 어떤 매대에도 '확성기' 아이템이 등록되어 있지 않아요."
                    elif not target_board:
                        error_message = "❌ '확성기' 아이템을 보유하고 있지 않아요."
                    else:
                        # 갯수 1개 차감 후 저장
                        user_inventory = target_board["inventories"][user_id]
                        user_inventory[target_item_id] -= 1
                        if user_inventory[target_item_id] <= 0:
                            user_inventory.pop(target_item_id, None)

                        atomic_json_save_or_raise(SHOP_FILE, data, indent=4)

            # 잠금을 빠져나온 뒤에 안내해요. (잠금 안에서 await 하면 봇이 얼어붙을 수 있어요)
            if error_message:
                return await interaction.followup.send(error_message, ephemeral=True)

            # 4. 고확 전송할 채널 가져오기 (클래스 내부 구조에 맞춰 클라이언트 참조)
            # 💡 여기서부터는 확성기가 **이미 차감된 상태**예요. 차감을 먼저 하는 건 일부러 그런 건데,
            # 반대로 하면 두 명이 동시에(또는 한 명이 연타로) 쓸 때 둘 다 보유 검사를 통과해서
            # 확성기 하나로 방송이 두 번 나가버립니다. 대신 전송이 실패하면 아래에서 되돌려줘요.
            bot_client = getattr(self, "bot", None) or interaction.client
            channel = bot_client.get_channel(TARGET_CHANNEL_ID)
            if channel is None:
                try:
                    channel = await bot_client.fetch_channel(TARGET_CHANNEL_ID)
                except Exception:
                    await self._refund_launcher(user_id, target_ch_id, target_item_id)
                    return await interaction.followup.send(
                        "❌ 고확 채널을 찾을 수 없거나 봇이 해당 채널의 권한을 가지고 있지 않아요.\n"
                        "확성기는 다시 넣어드렸어요.", ephemeral=True
                    )

            # 5. 전송 및 유저 알림 마무리
            broadcast_message = f"# 📢 {내용}\n\n<@&{ROLE_ID}>"
            try:
                await channel.send(broadcast_message)
            except Exception as e:
                # 🐛 [버그 수정] 예전엔 여기서 실패하면 확성기만 사라지고 방송은 안 나갔어요.
                print(f"❗ [고확] 전송 실패: {type(e).__name__}: {e}")
                await self._refund_launcher(user_id, target_ch_id, target_item_id)
                return await interaction.followup.send(
                    f"❌ 고확 메세지를 보내지 못했어요. ({type(e).__name__})\n확성기는 다시 넣어드렸어요.",
                    ephemeral=True
                )

            await interaction.followup.send("✅ 확성기를 사용하여 고확 메시지를 전송했어요!", ephemeral=True)

        except Exception as e:
            print(f"\n[고확 명령어 치명적 에러]: {e}")
            traceback.print_exc()
            await interaction.followup.send("❌ 실행 중 알 수 없는 오류가 발생했어요. 콘솔 창을 확인해 주세요.", ephemeral=True)

    async def _refund_launcher(self, user_id: str, ch_id_str: str, item_id: str):
        """방송 전송에 실패했을 때, 차감했던 확성기 1개를 도로 넣어줍니다.

        수량이 0이 되면 인벤토리 칸 자체가 지워지기 때문에, 없으면 새로 만들어서 넣어요.
        """
        try:
            # 🔒 파일을 새로 읽어서 되돌려요. 차감할 때 쓰던 사본을 재사용하면
            # 그 사이에 일어난 다른 구매/사용까지 같이 되돌려버립니다.
            async with self.bot.economy_lock:
                data = safe_json_load(SHOP_FILE, {})
                board = data.get("boards", {}).get(ch_id_str)
                if board is None:
                    raise RuntimeError(f"매대(채널 {ch_id_str})를 찾을 수 없어요")

                user_inventory = board.setdefault("inventories", {}).setdefault(user_id, {})
                user_inventory[item_id] = user_inventory.get(item_id, 0) + 1
                atomic_json_save_or_raise(SHOP_FILE, data, indent=4)

            print(f"↩️ [고확] 전송 실패라서 확성기 1개를 되돌려줬어요. (유저 {user_id})")
        except Exception as e:
            # 되돌리기까지 실패하면 유저는 아이템만 잃은 상태라 반드시 사람이 개입해야 해요.
            print(f"❗ [고확] 확성기 되돌리기 실패: {type(e).__name__}: {e}")
            await send_alert(
                "🔴 고확 확성기를 되돌리지 못했어요",
                f"유저 `{user_id}`님의 확성기가 차감됐는데 방송은 나가지 않았고, 되돌리기도 실패했어요.\n"
                f"관리자가 `/상점` 기능으로 직접 확성기 1개를 넣어주셔야 해요.\n"
                f"(매대 채널: `{ch_id_str}` · 아이템: `{item_id}`)\n\n**{type(e).__name__}**: {e}",
            )
