"""MariEconomy — 에바 지갑, 출석, 송금, 지급/회수, 거래내역, 서버 통계."""

import datetime as dt
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands, tasks

from mari_config import ATTENDANCE_FILE, CAMP_LEADER_ROLE_ID, CAMP_ROLE_IDS, ECONOMY_FILE, JEONYUL_ROLE_ID, KST, LEDGER_FILE, SHOP_FILE, STOCKS_FILE
from mari_alerts import report_loop_error
from mari_storage import atomic_json_save, atomic_json_save_or_raise, safe_json_load
from mari_settings import feature_gate, has_admin_or_role, load_settings, send_log_embed
from mari_state import load_ledger, record_ledger, record_ledger_many
from mari_utils import MariView, chunk_lines, describe_user_error, portfolio_value
# 🎁 /지갑에 붙는 선물 버튼. 아이템은 상점 데이터라서 UI·로직 모두 MariShop 쪽에 있어요.
# (shop.py는 economy.py를 import하지 않으니 순환 참조는 없습니다)
from cogs.shop import WalletGiftView

class UndoGiveSelect(discord.ui.Select):
    """되돌릴 지급/회수 묶음을 고르는 드롭다운"""
    def __init__(self, economy_cog, batches: list):
        self.economy_cog = economy_cog
        self.batches = {b["id"]: b for b in batches}

        options = []
        for b in batches[:25]:   # 디스코드 드롭다운 최대 25개
            total = sum(abs(e["delta"]) for e in b["rows"])
            when = b["ts"][5:16].replace("T", " ")
            icon = "📤" if b["kind"] == "관리자 지급" else "📥"
            label = f"{icon} {when} · {total:,} 에바"
            desc = f"{b['kind']} · 대상 {len(b['rows'])}명 · {b['detail'][:50]}"
            options.append(discord.SelectOption(label=label[:100], description=desc[:100], value=b["id"]))

        super().__init__(placeholder="되돌릴 기록을 선택하세요", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        self.disabled = True     # 연타로 두 번 되돌리는 사고 방지
        try:
            await interaction.edit_original_response(view=self.view)
        except Exception:
            pass
        await self.economy_cog.apply_undo(interaction, self.values[0])


class UndoGiveView(MariView):
    def __init__(self, economy_cog, batches: list):
        super().__init__(timeout=120)
        self.add_item(UndoGiveSelect(economy_cog, batches))


class WalletCleanConfirmView(MariView):
    """/지갑청소 — 실제로 지우기 전에 대상을 보여주고 한 번 더 확인받는 버튼.

    지갑 삭제는 되돌릴 방법이 백업 복원밖에 없어서, 미리보기 없이 바로 지우면 안 됩니다.
    """
    def __init__(self, economy_cog, targets: list, author_id: int):
        super().__init__(timeout=120)
        self.economy_cog = economy_cog
        self.targets = targets      # [(user_id_str, balance), ...]
        self.author_id = author_id
        self.processing = False
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # 🛡️ 미리보기가 공개 메시지라 버튼도 모두에게 보여요.
        # 명령을 부른 관리자 말고는 누르지 못하게 막습니다.
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "🙅‍♀️ 이 버튼은 명령을 실행한 관리자만 사용할 수 있어요.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        # ⌛ 확인하지 않고 방치된 삭제 버튼이 한참 뒤에 눌리는 일이 없도록 잠가둬요.
        if self.processing:
            return
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    content="⌛ 시간이 지나 지갑청소를 취소했어요. 아무것도 지우지 않았어요.", view=self
                )
            except Exception:
                pass

    @discord.ui.button(label="🧹 확인했어요, 삭제할게요", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.processing:
            return await interaction.response.send_message("⏳ 이미 처리 중이에요!", ephemeral=True)
        self.processing = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()
        await self.economy_cog.apply_wallet_clean(interaction, self.targets)

    @discord.ui.button(label="❌ 취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.processing:
            return await interaction.response.send_message("⏳ 이미 삭제가 진행 중이라 취소할 수 없어요.", ephemeral=True)
        self.processing = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()
        await interaction.followup.send("🚫 취소했어요. 지갑은 하나도 지워지지 않았어요.", ephemeral=True)

class TransferAmountModal(discord.ui.Modal, title="💸 에바 보내기"):
    """멤버를 우클릭해서 송금할 때 금액만 받는 창.

    받을 사람은 우클릭한 그 사람으로 이미 정해져 있어서, 금액만 적으면 끝이에요.
    """

    금액 = discord.ui.TextInput(label="보낼 에바", placeholder="예) 1000", max_length=12)

    def __init__(self, economy_cog, receiver: discord.Member):
        super().__init__()
        self.economy_cog = economy_cog
        self.receiver = receiver

    async def on_submit(self, interaction: discord.Interaction):
        # 🚧 [버그 수정] 기능 정지를 여기서 **한 번 더** 확인해요. 창을 띄우기 전에도 확인하지만,
        # 그건 우클릭한 순간의 이야기입니다. 금액을 타이핑하는 몇 초~몇 분 사이에 관리자가
        # `/기능제어 정지 송금`을 눌러도, 이미 창을 열어둔 사람은 그대로 송금돼 버렸어요.
        # 송금은 킬 스위치를 제일 먼저 당기게 되는 기능이라 이 틈이 그냥 두기엔 아깝습니다.
        # (슬래시 `/송금`은 검사와 실행이 같은 인터랙션이라 이런 틈이 없어요)
        if await feature_gate(interaction, "transfer", "송금"):
            return
        raw = str(self.금액).strip().replace(",", "").replace(" ", "")
        if not raw.isdigit():
            return await interaction.response.send_message(
                "❌ 금액은 숫자만 넣어주세요. (예: `1000`)", ephemeral=True
            )
        await self.economy_cog.perform_transfer(interaction, self.receiver, int(raw))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        """모달은 MariView와 상속 관계가 아니라서 오류 처리를 따로 붙여둬요."""
        print(f"❗ [우클릭 송금] 처리 중 오류: {type(error).__name__}: {error}")
        msg = describe_user_error(error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception as e:
            print(f"❗ [우클릭 송금] 오류 안내 전송 실패: {e}")


class MariEconomy(commands.Cog):
    """JSON 자동 생성, 상점주인 ID 검증, 매일 자정 리셋이 포함된 경제 및 출석 시스템"""
    
    # 전달해주신 상점주인(관리자) 역할 고유 ID 변수 지정
    
    def __init__(self, bot):
        self.bot = bot

        # 매일 자정 출석 초기화를 위한 백그라운드 태스크 시작
        self.midnight_reset_loop.start()

        # 🖱️ [신규] 멤버 우클릭 → 앱 → 에바 보내기 (받을 사람 고르는 단계가 사라져요)
        self.transfer_menu = app_commands.ContextMenu(name="에바 보내기", callback=self.transfer_menu_callback)
        self.bot.tree.add_command(self.transfer_menu)

    def cog_unload(self):
        # 💡 [정리] 다른 코그(백업·상점·스누즈·에바시)는 전부 내려갈 때 루프를 정리하는데
        # 여기만 빠져 있었어요. 코그를 다시 불러올 때 루프가 겹쳐 도는 걸 막아줍니다.
        self.midnight_reset_loop.cancel()
        self.bot.tree.remove_command(self.transfer_menu.name, type=self.transfer_menu.type)

    async def transfer_menu_callback(self, interaction: discord.Interaction, member: discord.Member):
        """멤버를 우클릭해서 바로 송금합니다. 검증은 모달 제출 뒤 perform_transfer가 다시 해요."""
        if await feature_gate(interaction, "transfer", "송금"):
            return
        # 명백히 안 되는 경우는 창을 띄우기 전에 걸러요. (금액을 적게 한 뒤 거절하면 허탈하니까요)
        if member.bot:
            return await interaction.response.send_message("🤖 봇에게는 송금할 수 없어요!", ephemeral=True)
        if member.id == interaction.user.id:
            return await interaction.response.send_message("💥 자기 자신한테는 돈을 보낼 수 없어요!", ephemeral=True)
        await interaction.response.send_modal(TransferAmountModal(self, member))

    @commands.Cog.listener()
    async def on_ready(self):
        # 봇이 켜졌을 때 루프가 실행 중이 아니라면 안전하게 시작합니다.
        if not self.midnight_reset_loop.is_running():
            self.midnight_reset_loop.start()
            print("⏰ [MariEconomy] 매일 자정 초기화 루프가 정상적으로 시작됐어요.")

    # 🗑️ [정리] 예전엔 여기 _ensure_files_exist()가 있어서 파일이 없으면 표준 구조를
    # 만들어 넣었어요. 그런데 mari_storage.init_json_files()가 봇이 뜰 때 모든 데이터
    # 파일을 이미 빈 `{}`로 만들어두기 때문에, "파일이 없을 때"라는 조건이 영영 참이 되지
    # 않아 한 번도 실행된 적이 없는 죽은 코드였습니다. (그래서 아래 _load_attendance가
    # 표준 구조를 직접 채우는 방식으로 바뀌었어요)

    def _load_raw_economy(self) -> dict:
        """JSON 파일에서 순수 경제 데이터를 로드합니다."""
        return safe_json_load(ECONOMY_FILE, {})

    def _save_raw_economy(self, data: dict):
        """경제 데이터를 JSON 파일에 안전하게 저장합니다.
        💾 저장 실패 시 DataSaveError를 던져서, 잔액이 안 바뀐 채로 성공 메세지가 나가는 걸 막아요."""
        atomic_json_save_or_raise(ECONOMY_FILE, data, indent=4)

    def _get_or_create_balance(self, user_id: int) -> int:
        """유저의 잔고를 가져옵니다. 파일에 없는 유저라면 자동으로 지갑을 생성(0원)합니다."""
        data = self._load_raw_economy()
        user_key = str(user_id)
        
        if user_key not in data:
            data[user_key] = 0
            self._save_raw_economy(data)
            print(f"ℹ️ [자동 지갑 생성] 신규 유저 지갑 생성 완료: {user_id}")
            
        return data[user_key]

    def _load_attendance(self) -> dict:
        """출석 데이터를 로드합니다. 빠진 항목은 표준 기본값으로 채워요.

        🐛 [버그 수정] 예전엔 표준 구조를 safe_json_load의 "기본값" 자리에만 적어뒀어요.
        그런데 그 기본값은 **파일이 아예 없을 때만** 쓰입니다. mari_storage.init_json_files()가
        봇이 뜰 때 모든 데이터 파일을 빈 `{}`로 미리 만들어두기 때문에, 새로 설치한 환경에서는
        언제나 `{}`가 그대로 넘어왔고 /출석이 `data["today_users"]`에서 KeyError로 죽었어요.
        자정 초기화 루프가 today_count·today_users는 만들어주지만 user_stats는 아무도 만들지
        않아서, 하루가 지나도 저절로 고쳐지지 않는 상태였습니다.
        이제 다른 로더들(profile·mari_tax·원장)과 똑같이 여기서 빠진 키를 직접 채웁니다.
        """
        data = safe_json_load(ATTENDANCE_FILE, {})
        if not isinstance(data, dict):
            data = {}
        data.setdefault("today_count", 0)
        data.setdefault("today_users", [])
        data.setdefault("reward_amount", 200)
        data.setdefault("user_stats", {})
        return data

    def _save_attendance(self, data: dict):
        """출석 데이터를 JSON 파일에 저장합니다. (출석 보상은 돈이 걸려있어 실패 시 예외를 던져요)"""
        atomic_json_save_or_raise(ATTENDANCE_FILE, data, indent=4)

    def _is_shop_owner(self, interaction: discord.Interaction) -> bool:
        """서버 관리자 또는 상점 관리자(settings.json의 roles.shop_admin)인지 확인합니다.

        🗑️ [정리] 이 코그에는 완전히 똑같은 판정을 하는 _is_economy_admin이 따로 있었어요.
        (지갑내역 유저조회·지급취소가 그쪽을, 나머지가 이쪽을 쓰고 있었습니다) 로직이 한 글자도
        다르지 않은데 이름만 둘이라, 나중에 한쪽 기준만 고치면 조용히 어긋나는 구조였어요.
        이 함수 하나로 합쳤습니다. 판정 자체도 공용 함수(has_admin_or_role)에 위임해서,
        다른 코그들과 같은 코드를 지나가도록 했어요."""
        return has_admin_or_role(interaction, "shop_admin")

    async def _log_economy_action(self, title: str, description: str, color: int = 0x3498db):
        # 💡 [통일] 색상은 economy_log 스타일(주황색)로 고정하고, 기존 title은
        # 임베드 상단 설명으로 유지해서 호출부는 그대로 두고 표시만 통일했습니다.
        await send_log_embed(self.bot, "economy_log", f"**{title}**\n{description}")

    # --- ⏰ 매일 자정 출석체크 자동 초기화 태스크 구현 ---

    @tasks.loop(time=dt.time(hour=0, minute=0, tzinfo=KST))
    async def midnight_reset_loop(self):
        """한국 시간(KST) 기준으로 매일 자정 00:00:00에 자동으로 오늘 하루 출석 데이터를 리셋합니다."""
        # ⚠️ tasks.loop 안에서 예외가 밖으로 새어나가면 루프 자체가 멈춰버려요.
        # (_save_attendance는 이제 저장 실패 시 DataSaveError를 던집니다)
        # 자정 초기화는 하루에 한 번뿐이라 여기서 멈추면 다음 날부터 출석이 안 되므로,
        # 실패해도 로그만 남기고 루프는 계속 살려둡니다.
        try:
            data = self._load_attendance()

            # 오늘 출석한 인원수와 명단을 초기화 (누적 횟수 및 보상 설정값은 보존)
            data["today_count"] = 0
            data["today_users"] = []

            self._save_attendance(data)
            print(f"⏰ [시스템 알림] 자정이 되어 오늘 하루 출석체크 명단이 정상적으로 초기화됐어요.")
        except Exception as e:
            print(f"❗ [자정 초기화 실패] {type(e).__name__}: {e}")

        
    @midnight_reset_loop.before_loop
    async def before_midnight_reset_loop(self):
        await self.bot.wait_until_ready()

    @midnight_reset_loop.error
    async def midnight_reset_loop_error(self, error: BaseException):
        # 본문은 try로 감싸져 있지만, 혹시라도 빠져나오는 예외가 있으면 루프가 영구히 멈춰요.
        # 그러면 다음 날부터 출석 초기화가 안 됩니다.
        await report_loop_error(self.midnight_reset_loop, "자정 출석 초기화", error)

    # ========== 🧾 [신규] 에바 거래 내역 / 지급 되돌리기 / 서버 통계 ==========

    지갑내역 = app_commands.Group(name="지갑내역", description="에바 입출금 내역을 확인해요.")

    @지갑내역.command(name="조회", description="내 에바가 언제 얼마나 들어오고 나갔는지 확인해요.")
    @app_commands.describe(개수="한 번에 볼 내역 수 (기본 15개, 최대 50개)")
    @app_commands.guild_only()
    async def ledger_view(self, interaction: discord.Interaction, 개수: Optional[int] = 15):
        개수 = max(1, min(int(개수 or 15), 50))
        entries = [e for e in load_ledger()["entries"] if e.get("user") == str(interaction.user.id)]

        if not entries:
            return await interaction.response.send_message(
                "📭 아직 기록된 거래 내역이 없어요. (이 기능이 켜진 뒤부터 쌓여요)", ephemeral=True
            )

        recent = list(reversed(entries))[:개수]
        lines = []
        for e in recent:
            sign = "🟢 +" if e["delta"] > 0 else "🔴 "
            when = e["ts"][5:16].replace("T", " ")   # MM-DD HH:MM
            detail = f" · {e['detail']}" if e.get("detail") else ""
            revert = " ~~(취소됨)~~" if e.get("reverted") else ""
            bal = f" → {e['balance']:,}" if e.get("balance") is not None else ""
            lines.append(f"`{when}` {sign}{e['delta']:,}{bal}\n　└ {e['kind']}{detail}{revert}")

        total_in = sum(e["delta"] for e in entries if e["delta"] > 0)
        total_out = sum(-e["delta"] for e in entries if e["delta"] < 0)

        embed = discord.Embed(
            title=f"🧾 {interaction.user.display_name}님의 에바 거래 내역",
            description="\n".join(lines)[:4000],
            color=0x5ce6b4,
        )
        embed.set_footer(text=f"전체 {len(entries)}건 중 최근 {len(recent)}건 · "
                              f"총 입금 {total_in:,} / 총 출금 {total_out:,}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @지갑내역.command(name="유저조회", description="[관리자] 특정 유저의 에바 거래 내역을 확인해요.")
    @app_commands.describe(유저="내역을 확인할 유저", 개수="한 번에 볼 내역 수 (기본 15개, 최대 50개)")
    @app_commands.guild_only()
    async def ledger_view_user(self, interaction: discord.Interaction,
                               유저: discord.Member, 개수: Optional[int] = 15):
        if not self._is_shop_owner(interaction):
            return await interaction.response.send_message("❌ 권한이 없어요.", ephemeral=True)

        개수 = max(1, min(int(개수 or 15), 50))
        entries = [e for e in load_ledger()["entries"] if e.get("user") == str(유저.id)]
        if not entries:
            return await interaction.response.send_message(
                f"📭 {유저.mention}님의 거래 내역이 아직 없어요.", ephemeral=True
            )

        recent = list(reversed(entries))[:개수]
        lines = []
        for e in recent:
            sign = "🟢 +" if e["delta"] > 0 else "🔴 "
            when = e["ts"][5:16].replace("T", " ")
            detail = f" · {e['detail']}" if e.get("detail") else ""
            revert = " ~~(취소됨)~~" if e.get("reverted") else ""
            bal = f" → {e['balance']:,}" if e.get("balance") is not None else ""
            lines.append(f"`{when}` {sign}{e['delta']:,}{bal}\n　└ {e['kind']}{detail}{revert}")

        embed = discord.Embed(
            title=f"🧾 {유저.display_name}님의 에바 거래 내역",
            description="\n".join(lines)[:4000],
            color=0x5ce6b4,
        )
        embed.set_footer(text=f"전체 {len(entries)}건 중 최근 {len(recent)}건")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def _recent_batches(self, limit: int = 20) -> list:
        """되돌릴 수 있는 /지급·/회수 묶음을 최신순으로 추립니다."""
        entries = load_ledger()["entries"]
        batches = {}
        for e in entries:
            bid = e.get("batch")
            if not bid or e.get("kind") not in ("관리자 지급", "관리자 회수"):
                continue
            b = batches.setdefault(bid, {
                "id": bid, "kind": e["kind"], "detail": e.get("detail", ""),
                "ts": e["ts"], "actor": e.get("actor"), "rows": [], "reverted": False,
            })
            b["rows"].append(e)
            if e.get("reverted"):
                b["reverted"] = True
        return sorted(batches.values(), key=lambda b: b["ts"], reverse=True)[:limit]

    @app_commands.command(name="지급취소",
                          description="[관리자] 최근 /지급·/회수를 되돌립니다. (잘못 지급했을 때 복구용)")
    @app_commands.guild_only()
    async def undo_give(self, interaction: discord.Interaction):
        if not self._is_shop_owner(interaction):
            return await interaction.response.send_message("❌ 권한이 없어요.", ephemeral=True)

        batches = [b for b in self._recent_batches() if not b["reverted"]]
        if not batches:
            return await interaction.response.send_message(
                "📭 되돌릴 수 있는 지급/회수 기록이 없어요.\n"
                "(이 기능이 켜진 뒤에 실행한 `/지급`·`/회수`부터 대상이 돼요)", ephemeral=True
            )

        view = UndoGiveView(self, batches)
        embed = discord.Embed(
            title="↩️ 되돌릴 지급/회수를 선택하세요",
            description="선택하면 해당 건의 금액이 **반대로 적용**돼요.\n"
                        "이미 쓴 돈이 있어도 그대로 차감되니 잔액이 마이너스가 될 수 있어요.",
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def apply_undo(self, interaction: discord.Interaction, batch_id: str):
        """선택한 묶음을 실제로 되돌립니다."""
        ledger = load_ledger()
        rows = [e for e in ledger["entries"]
                if e.get("batch") == batch_id and not e.get("reverted")]
        if not rows:
            return await interaction.followup.send("❌ 이미 되돌렸거나 기록을 찾을 수 없어요.", ephemeral=True)

        undo_rows = []
        async with self.bot.economy_lock:
            economy = self._load_raw_economy()
            for e in rows:
                uid = e["user"]
                economy[uid] = economy.get(uid, 0) - e["delta"]   # 부호를 뒤집어 원복
                undo_rows.append((uid, -e["delta"], economy[uid]))
            self._save_raw_economy(economy)

            # 원본 기록에 취소 표시 (같은 건을 두 번 되돌리지 못하게)
            for e in ledger["entries"]:
                if e.get("batch") == batch_id:
                    e["reverted"] = True
            try:
                atomic_json_save(LEDGER_FILE, ledger, indent=2)
            except Exception as ex:
                print(f"⚠️ 원장 취소 표시 저장 실패: {type(ex).__name__}: {ex}")

        kind = rows[0].get("kind", "관리자 지급")
        record_ledger_many(undo_rows, f"{kind} 취소", rows[0].get("detail", ""),
                           actor_id=interaction.user.id)

        total = sum(abs(e["delta"]) for e in rows)
        await send_log_embed(
            self.bot, "economy_log", "지급/회수 되돌리기가 실행됐어요.",
            fields=[
                ("원래 처리", f"{kind} · {rows[0].get('detail','')}", False),
                ("대상 인원", f"{len(rows)}명", True),
                ("되돌린 총액", f"{total:,} 에바", True),
                ("처리 관리자", interaction.user.mention, True),
            ],
        )
        await interaction.followup.send(
            f"✅ `{kind}` 건을 되돌렸어요. (대상 {len(rows)}명 · 총 {total:,} 에바)", ephemeral=True
        )

    @app_commands.command(name="통계", description="서버 활동 요약을 보여줘요. (출석률·에바 유통량·거래 활발도)")
    @app_commands.guild_only()
    async def server_stats(self, interaction: discord.Interaction):
        await interaction.response.defer()

        guild = interaction.guild
        humans = [m for m in guild.members if not m.bot]
        human_ids = {str(m.id) for m in humans}

        # 💰 에바 유통량 (서버에 남아있는 멤버 기준)
        economy = self._load_raw_economy()
        balances = sorted(
            ((uid, v) for uid, v in economy.items() if uid in human_ids and isinstance(v, int)),
            key=lambda kv: kv[1], reverse=True,
        )
        total_eva = sum(v for _, v in balances)
        holders = len(balances)
        avg_eva = total_eva // holders if holders else 0
        median_eva = balances[holders // 2][1] if holders else 0
        top10_share = (sum(v for _, v in balances[:10]) / total_eva * 100) if total_eva > 0 else 0
        negatives = sum(1 for _, v in balances if v < 0)

        # 📅 출석
        att = self._load_attendance()
        today_count = att.get("today_count", 0)
        att_rate = (today_count / len(humans) * 100) if humans else 0
        stats = att.get("user_stats", {})
        top_attender = max(stats.items(), key=lambda kv: kv[1]) if stats else None

        # 🧾 거래 활발도 (원장 기준, 최근 7일)
        entries = load_ledger()["entries"]
        cutoff = (dt.datetime.now(KST) - dt.timedelta(days=7)).isoformat(timespec="seconds")
        recent = [e for e in entries if e.get("ts", "") >= cutoff]
        kind_count = {}
        for e in recent:
            kind_count[e.get("kind", "기타")] = kind_count.get(e.get("kind", "기타"), 0) + 1
        active_users = len({e["user"] for e in recent})

        # 📈 주식
        stock_data = safe_json_load(STOCKS_FILE, {})
        stocks_dict = stock_data.get("stocks", {})
        user_shares = stock_data.get("user_shares", {})
        investors = sum(1 for uid, s in user_shares.items() if uid in human_ids and s)
        # 🐛 [버그 수정] 예전엔 info.get("shares", 0)로 직접 읽어서, 보유 기록이 옛 형식
        # (종목: 주식수 정수)으로 남아 있으면 AttributeError로 /통계 전체가 터졌어요.
        # 신·구 형식을 모두 받아주는 공용 함수로 통일했습니다.
        stock_value = sum(portfolio_value(stock_data, uid) for uid in user_shares if uid in human_ids)

        embed = discord.Embed(
            title=f"📊 {guild.name} 활동 요약",
            color=0x5865F2,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.add_field(
            name="💰 에바 유통량",
            value=(f"총 **{total_eva:,}** 에바 (지갑 {holders}명)\n"
                   f"평균 {avg_eva:,} · 중앙값 {median_eva:,}\n"
                   f"상위 10명이 전체의 **{top10_share:.1f}%** 보유"
                   + (f"\n⚠️ 마이너스 잔고 {negatives}명" if negatives else "")),
            inline=False,
        )
        embed.add_field(
            name="📅 오늘 출석",
            value=(f"**{today_count}명** / 멤버 {len(humans)}명 (**{att_rate:.1f}%**)"
                   + (f"\n🏆 누적 1위: <@{top_attender[0]}> ({top_attender[1]}일)" if top_attender else "")),
            inline=False,
        )
        if recent:
            top_kinds = sorted(kind_count.items(), key=lambda kv: kv[1], reverse=True)[:5]
            embed.add_field(
                name="🧾 최근 7일 거래",
                value=(f"총 **{len(recent)}건** · 참여 **{active_users}명**\n"
                       + "\n".join(f"　• {k}: {v}건" for k, v in top_kinds)),
                inline=False,
            )
        else:
            embed.add_field(name="🧾 최근 7일 거래",
                            value="아직 기록이 없어요. (원장 기능이 켜진 뒤부터 쌓여요)", inline=False)
        embed.add_field(
            name="📈 주식",
            value=(f"상장 **{len(stocks_dict)}종목** · 투자자 **{investors}명**\n"
                   f"주식 평가총액 **{stock_value:,}** 에바"),
            inline=False,
        )

        # 💬 채팅량 (내용·작성자는 저장하지 않고 개수만 셉니다)
        embed.add_field(name="💬 채팅량", value=self._build_chat_stats_text(), inline=False)

        embed.set_footer(text="에바 유통량·주식은 현재 서버에 남아있는 멤버 기준이에요")
        await interaction.followup.send(embed=embed)

    def _build_chat_stats_text(self) -> str:
        """날짜별 채팅 수를 요약하고 요일별 활발도를 계산합니다."""
        gpt_cog = self.bot.get_cog("MariGPT")
        counts = dict(getattr(gpt_cog, "_chat_counts", {}) or {})
        if not counts:
            return "아직 집계된 채팅이 없어요. (이 기능이 켜진 뒤부터 쌓여요)"

        today = dt.datetime.now(KST).date()
        today_n = counts.get(today.isoformat(), 0)
        yesterday_n = counts.get((today - dt.timedelta(days=1)).isoformat(), 0)

        # 최근 7일 평균 (오늘은 아직 하루가 안 끝났으니 제외)
        last7 = [counts.get((today - dt.timedelta(days=i)).isoformat(), 0) for i in range(1, 8)]
        avg7 = sum(last7) // 7 if last7 else 0

        # 요일별 평균 — 오늘은 진행 중이라 빼고 계산해야 공평해요
        WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
        buckets = {i: [] for i in range(7)}
        for day_str, n in counts.items():
            try:
                d = dt.date.fromisoformat(day_str)
            except ValueError:
                continue
            if d >= today:
                continue
            buckets[d.weekday()].append(n)

        lines = [
            f"오늘 **{today_n:,}개** · 어제 **{yesterday_n:,}개**",
            f"최근 7일 평균 **{avg7:,}개/일** · 집계 {len(counts)}일치",
        ]

        ranked = sorted(
            ((wd, sum(v) // len(v), len(v)) for wd, v in buckets.items() if v),
            key=lambda x: x[1], reverse=True,
        )
        if ranked:
            best = ranked[0]
            lines.append(f"\n🔥 가장 활발한 요일: **{WEEKDAYS[best[0]]}요일** (평균 {best[1]:,}개)")
            # 막대로 한눈에 비교 (요일 순서 고정)
            peak = max(avg for _, avg, _ in ranked) or 1
            by_wd = {wd: (avg, n) for wd, avg, n in ranked}
            for i, name in enumerate(WEEKDAYS):
                if i not in by_wd:
                    lines.append(f"`{name}` (데이터 없음)")
                    continue
                avg, n = by_wd[i]
                bar = "█" * max(1, round(avg / peak * 10))
                lines.append(f"`{name}` {bar} {avg:,}")
        else:
            lines.append("\n요일별 통계는 하루가 지나야 나와요.")

        return "\n".join(lines)

    # --- 슬래시 명령어 구현 ---

    @app_commands.command(name="지갑", description="본인의 에바 잔고와 보유 중인 아이템 목록을 확인해요")
    @app_commands.describe(
        member="확인할 멤버 선택 (비워두면 본인 지갑)",
        캠프="여러 명의 지갑을 표로 한꺼번에 조회 (캠프별은 캠프장 전용, 전체는 관리자·상점주인 전용)"
    )
    @app_commands.choices(캠프=[
        app_commands.Choice(name="악동캠프", value="악동"),
        app_commands.Choice(name="나래캠프", value="나래"),
        app_commands.Choice(name="여백캠프", value="여백"),
        app_commands.Choice(name="전체 (서버 인원 전원)", value="전체"),
    ])
    async def check_balance_slash(self, interaction: discord.Interaction, member: discord.Member = None, 캠프: Optional[app_commands.Choice[str]] = None):
        # 📋 캠프 옵션이 들어오면 캠프별 전체 지갑 조회 분기로 처리
        if 캠프 is not None:
            return await self._show_camp_wallets(interaction, 캠프)

        target = member if member else interaction.user

        # 타인 지갑 무단 조회 방어선 (상점주인이나 본인만 가능)
        if target.id != interaction.user.id and not self._is_shop_owner(interaction):
            await interaction.response.send_message(
                "🙅‍♀️ 떽! 다른 사람의 지갑은 상점주인이나 관리자만 열어볼 수 있다구!", 
                ephemeral=True
            )
            return

        async with self.bot.economy_lock:
            balance = self._get_or_create_balance(target.id)
        
        # 🎒 [상점 인벤토리 데이터 실시간 연동 크로스 로드]
        shop_cog = self.bot.get_cog("MariShop")
        shop_data = {}
        if shop_cog and hasattr(shop_cog, "_load_shop"):
            shop_data = shop_cog._load_shop()
        else:
            # 🚨 [버그 수정] 손상된 파일을 조용히 무시하지 않고 safe_json_load로 읽어요.
            shop_data = safe_json_load(SHOP_FILE, {})

        user_id_str = str(target.id)

        # 🏪 [다중 매대] 아이템이 매대별로 나뉘어 저장되므로, 모든 매대를 돌면서
        # 이 유저가 가진 아이템을 전부 모아옵니다. 매대가 여러 개라 이름이 겹칠 수 있어
        # 어느 매대에서 산 물건인지도 함께 표시해요.
        owned_items = []
        for board in shop_data.get("boards", {}).values():
            user_inv = board.get("inventories", {}).get(user_id_str, {})
            shop_items = board.get("items", {})
            board_name = board.get("shop_name", "상점")

            for item_id, count in user_inv.items():
                if count > 0 and item_id in shop_items:
                    item_info = shop_items[item_id]
                    item_name = item_info.get("name", "알 수 없는 아이템")
                    is_role = item_info.get("is_role", False)

                    # 역할 뒤에 (역할) 표시 추가 요구사항 유지
                    suffix = " `(역할)`" if is_role else ""
                    owned_items.append(f"🔹 **{item_name}** {suffix} · `{count}개` · *({board_name})*")

        # ==================== ✨ 프리미엄 프로필 임베드 레이아웃 디자인 ====================
        embed = discord.Embed(
            title=f"💳 {target.display_name}님의 자산 리포트",
            color=0x5CE6B4  # 상점 전광판 매대와 깔끔하게 매칭되는 시그니처 민트색!
        )
        
        # 1. 우측 상단에 대상자의 프로필 사진 배치 (카드 퀄리티 극대화)
        embed.set_thumbnail(url=target.display_avatar.url)
        
        # 2. 화폐 정보 필드 (크고 깔끔하게 강조)
        embed.add_field(
            name="🪙 보유 화폐", 
            value=f"**{balance:,} 에바**를 보유 중이세요.", 
            inline=False
        )
        
        # 3. 인벤토리 목록 필드 (이모지와 가독성 도트 배치)
        if owned_items:
            inv_value = "\n".join(owned_items)
        else:
            inv_value = "*└ 아직 상점에서 구매한 물품이 백팩에 없어요.*"

        embed.add_field(
            name="🎒 소지품 가방 (인벤토리)",
            value=inv_value,
            inline=False
        )

        # 4. 하단 요청자 푸터 정보 각인
        embed.set_footer(
            text=f"조회자: {interaction.user.display_name} · 실시간 데이터 연동 완료",
            icon_url=interaction.user.display_avatar.url
        )
        embed.timestamp = interaction.created_at

        # 🎁 [신규] 본인 지갑을 열었고, 넘겨줄 수 있는 아이템이 있을 때만 선물 버튼을 붙여줍니다.
        # (남의 지갑을 조회한 화면이나, 역할 아이템만 가진 사람에게는 버튼이 아예 안 보여요)
        view = discord.utils.MISSING
        if shop_cog and target.id == interaction.user.id and hasattr(shop_cog, "collect_giftable_items"):
            if shop_cog.collect_giftable_items(target.id):
                view = WalletGiftView(shop_cog, interaction.user.id)

        # 최종 카드 전송
        await interaction.response.send_message(embed=embed, view=view)

        # 타임아웃 때 버튼을 잠그려면 메세지 핸들이 필요해요. 실패해도 버튼 자체는 잘 동작합니다.
        if view is not discord.utils.MISSING:
            try:
                view.message = await interaction.original_response()
            except Exception:
                pass

    async def _show_camp_wallets(self, interaction: discord.Interaction, 캠프: app_commands.Choice[str]):
        """여러 명의 지갑 잔액을 표로 한꺼번에 조회합니다. (캠프별 또는 서버 전원)

        🔁 [통합] 예전엔 서버 전체 순위를 보는 `/랭킹`이 따로 있었어요. 그런데 그쪽은 사람마다
        임베드 필드를 하나씩 써서 26명부터는 아예 안 떴고(디스코드 필드 상한 25개), 지갑만 보여줘서
        주식에 자산을 묻어둔 사람이 가난해 보이는 문제도 있었습니다. 여기 표 방식은 글자 수로
        잘라 나눠 보내기 때문에 인원 제한이 없고 지갑·주식·합계를 같이 보여줘서, `/랭킹`을
        없애고 `캠프:전체`로 합쳤어요.
        """
        # 🏠 지금은 mari_client._enforce_guild_only가 모든 명령어를 서버 전용으로 막아둬서
        # DM으로 들어올 일이 없어요. 그래도 이 검사는 남겨둡니다 — 서버 역할과 멤버 목록이
        # 있어야 도는 코드라, 혹시라도 guild가 None인 채로 들어오면 .roles에서 바로 터지거든요.
        # (동기화가 아직 안 퍼진 사이를 막는 2차 방어선과 같은 취지예요)
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "🙅‍♀️ 이 조회는 서버 안에서만 쓸 수 있어요.",
                ephemeral=True,
            )

        is_all = 캠프.value == "전체"
        is_jeonyul = any(r.id == JEONYUL_ROLE_ID for r in interaction.user.roles)
        is_admin = interaction.user.guild_permissions.administrator

        # 🔐 권한
        #  • 캠프별: 캠프장 / 전율(캠프 관리 기구) / 서버 관리자
        #  • 전체  : 서버 관리자 / 상점주인 / 전율 — 없어진 `/랭킹`과 같은 기준이라
        #            이번 통합으로 새로 열리거나 막히는 사람이 없어요.
        if is_all:
            if not (is_admin or is_jeonyul or self._is_shop_owner(interaction)):
                return await interaction.response.send_message(
                    "🙅‍♀️ 전체 지갑 조회는 관리자나 상점주인만 가능해요!", ephemeral=True
                )
        else:
            is_leader = any(r.id == CAMP_LEADER_ROLE_ID for r in interaction.user.roles)
            if not (is_leader or is_jeonyul or is_admin):
                return await interaction.response.send_message(
                    "🙅‍♀️ 캠프별 지갑 조회는 캠프장(또는 관리자)만 가능해요!", ephemeral=True
                )

        # 📨 전체 조회는 인원이 많아 임베드가 여러 장 나가므로 채널을 어지럽히지 않게
        # 조회한 사람에게만 보여줍니다. 캠프별 조회는 지금까지처럼 공개예요.
        await interaction.response.defer(ephemeral=is_all)

        if is_all:
            members = [m for m in interaction.guild.members if not m.bot]
            if not members:
                return await interaction.followup.send("❌ 조회할 인원이 없어요.", ephemeral=True)
        else:
            role = interaction.guild.get_role(CAMP_ROLE_IDS[캠프.value])
            if not role:
                return await interaction.followup.send(f"❌ '{캠프.name}' 역할을 서버에서 찾을 수 없어요.")

            members = [m for m in role.members if not m.bot]
            if not members:
                return await interaction.followup.send(f"❌ {캠프.name}에 소속된 인원이 없어요.")

        economy = self._load_raw_economy()

        # 📈 [신규] 지갑만 보면 '주식에 자산을 묻어둔 고래'가 가난해 보여요.
        # 세금 기준을 정하려면 주식 평가액까지 같이 봐야 해서 함께 표시합니다.
        stock_data = {}
        stock_cog = self.bot.get_cog("MariStock")
        if stock_cog:
            try:
                stock_data = stock_cog._load_stocks()
            except Exception as e:
                print(f"⚠️ [캠프 지갑] 주식 평가액을 불러오지 못했어요: {type(e).__name__}: {e}")

        rows = []
        for m in members:
            uid = str(m.id)
            bal = economy.get(uid, 0)
            bal = int(bal) if isinstance(bal, (int, float)) else 0
            rows.append((m.display_name, bal, portfolio_value(stock_data, uid)))

        # 총자산(지갑+주식) 많은 순. 지갑만으로 줄 세우면 고래가 아래로 숨어버려요.
        rows.sort(key=lambda x: x[1] + x[2], reverse=True)

        total = sum(bal for _, bal, _ in rows)
        total_stock = sum(stock for _, _, stock in rows)
        lines = [
            f"{i:>2}. {name}  |  지갑 {bal:,}  |  주식 {stock:,}  |  합계 {bal + stock:,}"
            for i, (name, bal, stock) in enumerate(rows, start=1)
        ]

        # 임베드 설명 길이 제한(4096자) 방어를 위해 표를 여러 청크로 분할 전송
        chunks = chunk_lines(lines)

        title_label = "서버 전체" if is_all else 캠프.name
        total_label = "🏦 전체 총자산" if is_all else f"🏦 {캠프.name} 총자산"

        for idx, chunk in enumerate(chunks):
            embed = discord.Embed(
                title=f"📋 {title_label} 지갑 현황" + (f" ({idx+1}/{len(chunks)})" if len(chunks) > 1 else ""),
                description=f"```\n{chunk}\n```",
                color=0x5CE6B4,
                timestamp=dt.datetime.now(KST)
            )
            if idx == len(chunks) - 1:
                embed.add_field(name="총 인원", value=f"{len(rows)}명", inline=True)
                embed.add_field(name="💰 지갑 합계", value=f"{total:,} 에바", inline=True)
                embed.add_field(name="📈 주식 평가액 합계", value=f"{total_stock:,} 에바", inline=True)
                embed.add_field(name=total_label, value=f"**{total + total_stock:,} 에바**", inline=False)
                if not stock_cog:
                    embed.add_field(name="⚠️ 참고", value="주식 시스템을 불러올 수 없어서 주식 평가액이 0으로 표시됐어요.", inline=False)
            embed.set_footer(text=f"조회자: {interaction.user.display_name}")
            await interaction.followup.send(embed=embed, ephemeral=is_all)

    @app_commands.command(name="송금", description="다른 사람에게 에바를 송금해요!")
    @app_commands.describe(member="돈을 받을 멤버 선택", amount="보낼 에바 수량")
    async def transfer_money_slash(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if await feature_gate(interaction, "transfer", "송금"):
            return
        await self.perform_transfer(interaction, member, amount)

    async def perform_transfer(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        """송금 한 건을 처리합니다. `/송금`과 멤버 우클릭 '에바 보내기'가 같이 씁니다.

        ⚠️ 아직 응답하지 않은 인터랙션을 받아요. (슬래시 호출·모달 제출 둘 다 그 상태예요)
        기능 정지(feature_gate)는 부르는 쪽에서 이미 확인하고 들어옵니다.
        """
        if member.bot:
            await interaction.response.send_message("🤖 봇에게는 송금할 수 없어요!", ephemeral=True)
            return
        if interaction.user.id == member.id:
            await interaction.response.send_message("💥 자기 자신한테는 돈을 보낼 수 없어요!", ephemeral=True)
            return
        
        if amount <= 0:
            await interaction.response.send_message("💥 0원 이하의 금액은 송금할 수 없어요! 😤", ephemeral=True)
            return

        # 🚦 [성능 버그 수정] 예전엔 잔고 부족 안내를 economy_lock을 **쥔 채로** 보냈어요.
        # economy_lock은 송금·출석·지급·상점구매·주식거래·캠프통장이 전부 지나가는 서버 공용
        # 관문이라, 흔하게 일어나는 실패 한 건이 디스코드와 통신하는 동안(레이트리밋에 걸리면
        # 몇 초) 서버의 모든 돈 관련 기능이 줄을 섰습니다.
        # 이제 락 안에서는 계산과 저장만 하고, 안내는 전부 락을 놓은 뒤에 보냅니다.
        error = None
        sender_id = str(interaction.user.id)
        receiver_id = str(member.id)
        sender_after = receiver_after = 0

        async with self.bot.economy_lock:
            data = self._load_raw_economy()

            if sender_id not in data: data[sender_id] = 0
            if receiver_id not in data: data[receiver_id] = 0

            sender_balance = data[sender_id]

            if sender_balance < 0:
                error = "❌ 현재 잔고가 마이너스(-) 상태이므로 송금할 수 없어요! 😢"
            elif sender_balance < amount:
                error = f"❌ 잔고가 부족합니다! (보유 잔고: {sender_balance:,} 에바)"
            else:
                data[sender_id] = sender_balance - amount
                data[receiver_id] = data[receiver_id] + amount
                self._save_raw_economy(data)
                sender_after = data[sender_id]
                receiver_after = data[receiver_id]

        if error:
            return await interaction.response.send_message(error, ephemeral=True)

        # 🧾 보낸 쪽·받은 쪽 양쪽 원장에 남겨서 서로 내역을 확인할 수 있게 합니다.
        record_ledger(sender_id, -amount, sender_after, "송금 보냄", f"→ {member.display_name}")
        record_ledger(receiver_id, amount, receiver_after, "송금 받음", f"← {interaction.user.display_name}")

        await interaction.response.send_message(f"💸 {interaction.user.mention}님이 {member.mention}님에게 **{amount:,} 에바**를 보냈어요! 배달 완료! ✨")

        await self._log_economy_action(
            title="💸 [송금 완료]",
            description=f"**송금 유저:** {interaction.user.mention}\n"
                        f"**수신 유저:** {member.mention}\n"
                        f"**송금 액수:** {amount:,} 에바\n"
                        f"**송금자 잔여:** {sender_after:,} 에바",
            color=0x3498db
        )

    @app_commands.command(name="지급", description="[관리자] 특정 유저 또는 특정 역할을 가진 모든 유저에게 에바를 지급합니다.")
    @app_commands.guild_only()
    async def give_money(self, interaction: discord.Interaction, 금액: int, 유저: Optional[discord.Member] = None, 역할: Optional[discord.Role] = None):
        # 1. 관리자 권한 확인 (/지갑청소·/랭킹 등과 같은 기준을 쓰도록 공용 헬퍼에 위임)
        if not self._is_shop_owner(interaction):
            return await interaction.response.send_message("❌ 권한이 없어요.", ephemeral=True)
        
        # 2. 금액 검증
        if 금액 <= 0:
            return await interaction.response.send_message("❌ 지급할 금액은 1 에바 이상이어야 합니다.", ephemeral=True)
            
        # 3. 대상 인자 값 유효성 검사 (둘 다 안 넣었거나 둘 다 넣은 경우 방어)
        if 유저 is None and 역할 is None:
            return await interaction.response.send_message("❌ 지급할 대상(유저 또는 역할) 중 하나를 반드시 지정해 주세요.", ephemeral=True)
        if 유저 is not None and 역할 is not None:
            return await interaction.response.send_message("❌ 유저와 역할은 동시에 지정할 수 없어요. 하나만 선택해 주세요.", ephemeral=True)

        # 4. 3초 API 응답 시간 제한 연장 (대량 처리 대비)
        await interaction.response.defer()

        # 5. 대상 유저 리스트 추출
        targets = []
        target_mention_str = ""
        
        if 유저:
            targets = [유저]
            target_mention_str = 유저.mention
        else:
            # 봇을 제외하고 해당 역할을 보유한 실제 서버 멤버만 필터링
            targets = [m for m in 역할.members if not m.bot]
            target_mention_str = f"{역할.mention} 역할 인원 전체 ({len(targets)}명)"
            
            if not targets:
                return await interaction.followup.send(f"❌ {역할.mention} 역할을 가진 유저(봇 제외)가 서버에 한 명도 없어요.")

        # 6. 실시간 지갑 데이터 수정 (비동기 데이터 자물쇠 가동)
        rows = []
        async with self.bot.economy_lock:
            economy_data = self._load_raw_economy()
            for member in targets:
                m_id = str(member.id)
                economy_data[m_id] = economy_data.get(m_id, 0) + 금액
                rows.append((m_id, 금액, economy_data[m_id]))
            self._save_raw_economy(economy_data)

        # 🧾 되돌리기가 가능하도록 한 건으로 묶어 원장에 기록합니다. (/지급취소)
        record_ledger_many(rows, "관리자 지급", target_mention_str, actor_id=interaction.user.id)

        # 7. 경제 로그 채널에 깔끔한 통합 영수증 발송
        await send_log_embed(
            self.bot, "economy_log", "에바 일괄 지급 기록이에요.",
            fields=[
                ("지급 대상", target_mention_str, False),
                ("지급 금액", f"{금액:,} 에바", True),
                ("처리 관리자", interaction.user.mention, True),
            ],
        )

        # 8. 명령어 호출 완료 피드백
        await interaction.followup.send(f"✅ {target_mention_str}에게 `{금액:,} 에바`를 지급 완료했어요.")


    @app_commands.command(name="회수", description="[관리자] 특정 유저 또는 특정 역할을 가진 모든 유저에게서 에바를 회수합니다.")
    @app_commands.guild_only()
    async def take_money(self, interaction: discord.Interaction, 금액: int, 유저: Optional[discord.Member] = None, 역할: Optional[discord.Role] = None):
        # 1. 관리자 권한 확인 (/지갑청소·/랭킹 등과 같은 기준을 쓰도록 공용 헬퍼에 위임)
        if not self._is_shop_owner(interaction):
            return await interaction.response.send_message("❌ 권한이 없어요.", ephemeral=True)
        
        # 2. 금액 검증
        if 금액 <= 0:
            return await interaction.response.send_message("❌ 회수할 금액은 1 에바 이상이어야 합니다.", ephemeral=True)
            
        # 3. 대상 인자 값 유효성 검사
        if 유저 is None and 역할 is None:
            return await interaction.response.send_message("❌ 회수할 대상(유저 또는 역할) 중 하나를 반드시 지정해 주세요.", ephemeral=True)
        if 유저 is not None and 역할 is not None:
            return await interaction.response.send_message("❌ 유저와 역할은 동시에 지정할 수 없어요. 하나만 선택해 주세요.", ephemeral=True)

        # 4. 3초 API 응답 시간 제한 연장 (대량 처리 대비)
        await interaction.response.defer()

        # 5. 대상 유저 리스트 추출
        targets = []
        target_mention_str = ""
        
        if 유저:
            targets = [유저]
            target_mention_str = 유저.mention
        else:
            # 봇을 제외하고 해당 역할을 보유한 실제 서버 멤버만 필터링
            targets = [m for m in 역할.members if not m.bot]
            target_mention_str = f"{역할.mention} 역할 인원 전체 ({len(targets)}명)"
            
            if not targets:
                return await interaction.followup.send(f"❌ {역할.mention} 역할을 가진 유저(봇 제외)가 서버에 한 명도 없어요.")

        # 6. 실시간 지갑 데이터 수정 (비동기 데이터 자물쇠 가동 / 지갑 회수로 인한 음수 허용)
        rows = []
        async with self.bot.economy_lock:
            economy_data = self._load_raw_economy()
            for member in targets:
                m_id = str(member.id)
                economy_data[m_id] = economy_data.get(m_id, 0) - 금액
                rows.append((m_id, -금액, economy_data[m_id]))
            self._save_raw_economy(economy_data)

        # 🧾 되돌리기가 가능하도록 한 건으로 묶어 원장에 기록합니다. (/지급취소)
        record_ledger_many(rows, "관리자 회수", target_mention_str, actor_id=interaction.user.id)

        # 7. 경제 로그 채널에 깔끔한 통합 영수증 발송
        await send_log_embed(
            self.bot, "economy_log", "에바 일괄 회수 기록이에요.",
            fields=[
                ("회수 대상", target_mention_str, False),
                ("회수 금액", f"{금액:,} 에바", True),
                ("처리 관리자", interaction.user.mention, True),
            ],
        )

        # 8. 명령어 호출 완료 피드백
        await interaction.followup.send(f"✅ {target_mention_str}에게서 `{금액:,} 에바`를 회수 완료했어요.")

    @app_commands.command(name="지갑청소", description="[관리자] 서버를 나간 사람들의 지갑 데이터를 일괄 삭제하여 정리합니다.")
    @app_commands.guild_only()
    async def clean_left_users_slash(self, interaction: discord.Interaction):
        if not self._is_shop_owner(interaction):
            await interaction.response.send_message("🙅‍♀️ 데이터 정리 권한이 없어요!", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        # 1️⃣ 미리보기 단계 — 여기서는 파일을 절대 건드리지 않고 대상만 추립니다.
        # 지갑 삭제는 되돌릴 방법이 백업 복원밖에 없어서, 뭘 지우는지 먼저 보여줘야 해요.
        try:
            data = self._load_raw_economy()
        except Exception as e:
            return await interaction.followup.send(
                f"❌ 지갑 데이터를 읽지 못해서 청소를 중단했어요. ({type(e).__name__}: {e})"
            )

        targets = []
        for user_id_str, balance in data.items():
            try:
                user_id = int(user_id_str)
            except (TypeError, ValueError):
                # 🛡️ 유저 ID 형식이 아닌 키는 퇴장 여부를 판단할 수 없으니 손대지 않고 남겨둬요.
                print(f"⚠️ [지갑청소] 유저 ID 형식이 아닌 키를 건너뛰었어요: {user_id_str!r}")
                continue
            if interaction.guild.get_member(user_id) is None:
                targets.append((user_id_str, balance if isinstance(balance, (int, float)) else 0))

        if not targets:
            return await interaction.followup.send("✨ 현재 서버를 나간 유저의 유령 데이터가 존재하지 않아 깨끗합니다!")

        total = sum(bal for _, bal in targets)
        top = sorted(targets, key=lambda t: t[1], reverse=True)
        preview = "\n".join(f"• `{uid}` — {bal:,} 에바" for uid, bal in top[:10])
        if len(targets) > 10:
            preview += f"\n... 외 {len(targets) - 10}명"

        embed = discord.Embed(
            title="🧹 지갑청소 — 정말 지울까요?",
            description=(
                f"서버에 없는 유저 **{len(targets)}명**의 지갑을 삭제합니다.\n"
                f"파기될 유령 잔고: 총 **{total:,} 에바**\n\n"
                f"⚠️ 한 번 지우면 **백업 복원 말고는 되돌릴 방법이 없어요.**"
            ),
            color=0xE67E22,
        )
        embed.add_field(name="삭제 대상 (잔고 많은 순)", value=preview, inline=False)
        embed.set_footer(text="2분 안에 선택하지 않으면 자동으로 취소돼요.")

        view = WalletCleanConfirmView(self, targets, interaction.user.id)
        view.message = await interaction.followup.send(embed=embed, view=view, wait=True)

    async def apply_wallet_clean(self, interaction: discord.Interaction, targets: list):
        """2️⃣ 확인 버튼을 누른 뒤 실제로 지갑을 삭제합니다.

        미리보기와 확인 사이에는 시간이 있어요. 그 사이에 서버로 돌아온 사람이 있을 수 있어서,
        지우기 직전에 멤버 여부를 한 번 더 확인하고 파일도 새로 읽습니다.
        """
        target_ids = {uid for uid, _ in targets}
        cleaned_count = 0
        recovered_eva = 0
        rejoined = 0

        # 💾 저장이 실패하면 atomic_json_save_or_raise가 예외를 던지고 원본은 그대로예요.
        # 그러니 여기서 예외를 잡으면 "아무것도 안 지워졌다"고 말해도 맞습니다.
        try:
            async with self.bot.economy_lock:
                data = self._load_raw_economy()
                cleaned_data = {}

                for user_id_str, balance in data.items():
                    if user_id_str not in target_ids:
                        cleaned_data[user_id_str] = balance
                        continue

                    try:
                        member = interaction.guild.get_member(int(user_id_str))
                    except (TypeError, ValueError):
                        member = None

                    if member is not None:
                        # ↩️ 미리보기 이후에 서버로 돌아온 사람이에요. 지우면 안 됩니다.
                        cleaned_data[user_id_str] = balance
                        rejoined += 1
                        continue

                    cleaned_count += 1
                    recovered_eva += balance if isinstance(balance, (int, float)) else 0

                self._save_raw_economy(cleaned_data)
        except Exception as e:
            print(f"❗ [지갑청소] 처리 실패: {type(e).__name__}: {e}")
            return await interaction.followup.send(
                f"❌ 지갑 데이터를 저장하지 못해서 **아무것도 지우지 않았어요.** ({type(e).__name__}: {e})"
            )

        note = f"\n↩️ 그 사이 서버로 돌아온 **{rejoined}명**은 지우지 않고 남겨뒀어요." if rejoined else ""

        if cleaned_count == 0:
            return await interaction.followup.send(
                f"✨ 지울 대상이 남아있지 않아 아무것도 지우지 않았어요.{note}"
            )

        await interaction.followup.send(
            f"🧹 **[지갑 데이터 청소 완료]**\n"
            f"서버를 퇴장한 유저 **{cleaned_count}명**의 지갑 데이터를 성공적으로 파기했어요!\n"
            f"정리된 유령 잔고 규모: 총 **{recovered_eva:,} 에바**{note}"
        )
        await self._log_economy_action(
            title="🧹 [디비 청소 작업 수행]",
            description=f"**작업자:** {interaction.user.mention}\n"
                        f"**정리된 유저 수:** {cleaned_count} 명\n"
                        f"**파기된 총 에바:** {recovered_eva:,} 에바",
            color=0x95a5a6
        )

    @app_commands.command(name="출석보상설정", description="[관리자] 하루 출석체크 시 지급할 기본 에바 보상 액수를 조정합니다.")
    @app_commands.describe(amount="새로 지정할 출석 에바 보상 액수")
    @app_commands.guild_only()
    async def set_attendance_reward_slash(self, interaction: discord.Interaction, amount: int):
        if not self._is_shop_owner(interaction):
            await interaction.response.send_message("🙅‍♀️ 출석 보상을 수정할 수 있는 권한이 없어요!", ephemeral=True)
            return

        if amount <= 0:
            await interaction.response.send_message("❌ 보상 금액은 최소 **1 에바** 이상으로 설정해야 합니다! (음수 및 0 불가)", ephemeral=True)
            return

        async with self.bot.economy_lock:
            attendance_data = self._load_attendance()
            old_amount = attendance_data.get("reward_amount", 200)
            attendance_data["reward_amount"] = amount
            self._save_attendance(attendance_data)

        await interaction.response.send_message(
            f"⚙️ **[출석 보상 변경 완료]**\n"
            f"출석체크 기본 보상이 기존 **{old_amount:,} 에바**에서 ➡️ **{amount:,} 에바**로 변경됐어요!"
        )

        await self._log_economy_action(
            title="⚙️ [출석 보상 변경 알림]",
            description=f"**설정 변경자:** {interaction.user.mention}\n"
                        f"**이전 보상:** {old_amount:,} 에바\n"
                        f"**새로운 보상:** {amount:,} 에바",
            color=0x9b59b6
        )

    @app_commands.command(name="출석", description="매일 한 번 출석체크를 하고 에바를 받아요!")
    async def do_attendance_slash(self, interaction: discord.Interaction):
        if await feature_gate(interaction, "attendance", "출석"):
            return
        att_ch_id = load_settings().get("channels", {}).get("attendance")
        if not att_ch_id:
            return await interaction.response.send_message("❌ 관리자가 출석 채널을 아직 설정하지 않았어요.", ephemeral=True)

        if interaction.channel_id != att_ch_id:
            return await interaction.response.send_message(
                f"🙅‍♀️ 출석체크는 전용 채널(<#{att_ch_id}>)에서만 진행 가능합니다!", 
                ephemeral=True
            )

        user_id = str(interaction.user.id)

        # 🏃 이미 출석한 사람은 락을 기다릴 것도 없이 여기서 바로 돌려보냅니다.
        # (아래 락 안에서 한 번 더 제대로 검사하니, 이건 순전히 빠른 응답용 지름길이에요)
        if user_id in self._load_attendance()["today_users"]:
            return await interaction.response.send_message(
                "❌ 이미 오늘 출석 체크를 하셨어요. 내일 다시 와주세요!", ephemeral=True
            )

        # ⏱️ 락을 기다리는 동안 3초 응답 제한에 걸리지 않도록 먼저 응답을 미뤄둡니다.
        await interaction.response.defer()

        # 🔒 [버그 수정] 예전엔 출석 파일을 읽어 중복을 검사하고 저장하는 구간이 전부
        # economy_lock **밖**에 있었어요. 락 안에서는 지갑만 갱신했고요. 그래서 /출석을
        # 빠르게 두 번 누르면 두 호출이 모두 "아직 출석 안 함"으로 판정된 뒤 각자 보상을
        # 받아서, 보상이 두 번 나가고 출석자 명단은 나중에 저장한 쪽이 덮어써 버렸습니다.
        # 이제 읽기·중복검사·저장을 전부 락 안에서 한 덩어리로 처리해요.
        # ⚠️ 락 안에서는 디스코드로 아무것도 보내지 않습니다. (economy_lock은 송금·상점·주식이
        #    전부 지나가는 서버 공용 관문이라, 쥔 채로 메세지를 보내면 다 같이 줄을 서요)
        already_done = False
        async with self.bot.economy_lock:
            data = self._load_attendance()

            if user_id in data["today_users"]:
                already_done = True
            else:
                data["today_count"] += 1
                my_rank = data["today_count"]
                data["today_users"].append(user_id)

                data["user_stats"][user_id] = data["user_stats"].get(user_id, 0) + 1
                total_attendance_days = data["user_stats"][user_id]

                reward = data.get("reward_amount", 200)

                # 💾 출석 기록을 **먼저** 저장하고 보상을 나중에 넣습니다. 순서가 반대면
                # "보상은 들어갔는데 기록이 안 남는" 실패에서 같은 사람이 또 받아갈 수 있어요.
                # 이 순서라면 최악이어도 "출석은 됐는데 보상이 안 들어간" 상태라, 관리자가
                # /지급으로 채워주면 되고 에바가 복사되지는 않습니다.
                self._save_attendance(data)

                economy_data = self._load_raw_economy()
                # 출석 시에도 최초 유저는 지갑을 자동 증설 및 연동시킵니다.
                economy_data[user_id] = economy_data.get(user_id, 0) + reward
                self._save_raw_economy(economy_data)
                balance_after = economy_data[user_id]

        if already_done:
            # 지름길 검사를 통과한 뒤 락을 기다리는 사이에 앞선 요청이 먼저 끝난 경우예요.
            return await interaction.followup.send(
                "❌ 이미 오늘 출석 체크를 하셨어요. 내일 다시 와주세요!", ephemeral=True
            )

        record_ledger(user_id, reward, balance_after, "출석 보상", f"{total_attendance_days}일째")

        if my_rank == 1:
            rank_comment = "🥇 와우! 대박이에요! 오늘 **1등**으로 출석하셨어요! 부지런하시네요! 🥳"
        elif my_rank == 2:
            rank_comment = "🥈 아쉽네요! 오늘 **2등**으로 도장 쾅 찍으셨어요! 🥈"
        elif my_rank == 3:
            rank_comment = "🥉 오늘 **3등**으로 골인 완료했어요! 🥉"
        else:
            rank_comment = f"🏃‍♂️ 오늘 **{my_rank}등**으로 출석 도장을 찍으셨어요!"

        # 위에서 이미 defer()로 응답을 미뤄뒀으므로 followup으로 보냅니다.
        await interaction.followup.send(
            f"📅 **{interaction.user.display_name}님 출석 완료!**\n"
            f"{rank_comment}\n"
            f"🪙 보상으로 **{reward:,} 에바**가 지갑에 지급됐어요!\n"
            f"📊 총 출석 횟수: **{total_attendance_days}회째** ✨"
        )
