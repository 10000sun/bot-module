"""ChunsikCamp — 캠프 견학 이동/복귀와 캠프 세금 통장."""

import datetime as dt
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands

from chunsik_config import CAMP_LEADER_ROLE_ID, CAMP_ROLE_IDS, CAMP_TREASURY_FILE, CAMP_VISIT_FILE, JEONYUL_ROLE_ID, KST
from chunsik_settings import has_admin_or_role
from chunsik_state import record_ledger, record_ledger_many
from chunsik_storage import atomic_json_save_or_raise, safe_json_load
from chunsik_utils import chunk_lines, describe_user_error, portfolio_value, report_broken_transaction
from chunsik_tax import (CAMP_TAX_TYPE, config_to_text, flat_bracket_tax, load_tax_config,
                      lottery_tickets, progressive_tax, roulette_tax, rule_summary,
                      save_tax_config, stock_holding_tax, text_to_config)


class TaxRuleModal(discord.ui.Modal):
    """세금 기준을 고치는 창.

    구간표나 룰렛 확률표는 슬래시 명령어 인자로 받기엔 너무 불편해서,
    지금 값을 그대로 적어서 띄워주고 숫자만 고쳐서 제출하게 했어요.
    형식이 틀리면 저장하지 않고 어디가 잘못됐는지 알려줍니다.
    """

    def __init__(self, camp_cog, camp: str, camp_label: str, cfg: dict):
        super().__init__(title=f"{camp_label} 세금 기준 수정")
        self.camp_cog = camp_cog
        self.camp = camp
        self.camp_label = camp_label
        self.rule_input = discord.ui.TextInput(
            label="기준 (숫자만 고치고 형식은 그대로 두세요)",
            style=discord.TextStyle.paragraph,
            default=config_to_text(camp, cfg),
            max_length=1500,
        )
        self.add_item(self.rule_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_cfg = text_to_config(self.camp, self.rule_input.value)
        except ValueError as e:
            return await interaction.response.send_message(
                f"❌ 형식이 맞지 않아 저장하지 않았어요.\n└ {e}", ephemeral=True
            )
        except Exception as e:
            return await interaction.response.send_message(
                f"❌ 기준을 읽지 못했어요. ({type(e).__name__}: {e})", ephemeral=True
            )

        try:
            config = load_tax_config()
            config[self.camp] = new_cfg
            save_tax_config(config)
        except Exception as e:
            return await interaction.response.send_message(
                f"❌ 저장에 실패해서 기준이 바뀌지 않았어요. ({type(e).__name__}: {e})", ephemeral=True
            )

        embed = discord.Embed(
            title=f"✅ {self.camp_label} 세금 기준을 바꿨어요",
            description=rule_summary(self.camp, new_cfg),
            color=0x2ECC71,
            timestamp=dt.datetime.now(KST),
        )
        embed.set_footer(text="다음 /캠프 세금환수부터 이 기준이 적용돼요.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        """Modal도 View와 마찬가지로 예외가 tree.on_error로 가지 않아요.
        기본 동작은 콘솔 출력뿐이라 유저는 창만 닫히고 아무 설명을 못 받습니다."""
        print(f"❗ [세금기준 창 오류] {type(error).__name__}: {error}")
        try:
            await interaction.response.send_message(describe_user_error(error), ephemeral=True)
        except Exception:
            pass


class ChunsikCamp(commands.Cog):
    """캠프장 전용 기능 모음: 세금 환수, 캠프통장, 견학생 관리"""

    visit_group = app_commands.Group(name="견학", description="견학생 이동/조회/복귀 관련 명령어 모음")
    camp_treasury_group = app_commands.Group(name="캠프", description="캠프 세금환수 및 캠프통장 조회/인출/송금/회수", guild_only=True)

    def __init__(self, bot):
        self.bot = bot

    def _get_economy_cog(self):
        return self.bot.get_cog("ChunsikEconomy")

    # ================== 🏦 캠프 세금 통장(가상 통장) 시스템 ==================
    def _load_camp_treasury(self) -> dict:
        """캠프별 세금 통장 잔액을 불러옵니다."""
        return safe_json_load(CAMP_TREASURY_FILE, {})

    def _save_camp_treasury(self, data: dict):
        """캠프별 세금 통장 잔액을 저장합니다."""
        atomic_json_save_or_raise(CAMP_TREASURY_FILE, data, indent=4)

    def _is_camp_leader(self, interaction: discord.Interaction) -> bool:
        """캠프장 역할 보유자, 전율(캠프 관리 기구), 또는 서버 관리자인지 확인합니다."""
        if interaction.user.guild_permissions.administrator:
            return True
        if any(r.id == JEONYUL_ROLE_ID for r in interaction.user.roles):
            return True
        return any(r.id == CAMP_LEADER_ROLE_ID for r in interaction.user.roles)

    def _can_manage_visit(self, interaction: discord.Interaction) -> bool:
        """견학 '보내기'·'복귀'·'조회'를 할 수 있는 사람인지 확인합니다.

        캠프장·전율·서버 관리자에 더해 **상점주인(settings.json의 roles.shop_admin)** 도 허용해요.
        (`/설정 관리자 상점` 으로 지정한 역할과 같은 역할이에요)
        견학 '내역'(전체 이동 기록)만 기존대로 캠프장 전용으로 남겨뒀습니다.
        """
        return self._is_camp_leader(interaction) or has_admin_or_role(interaction, "shop_admin")

    # ================== 🚌 [신규] 견학생 시스템 ==================
    def _load_visits(self) -> dict:
        """견학 현황(active)과 전체 이동 기록(history)을 불러옵니다."""
        data = safe_json_load(CAMP_VISIT_FILE, {"active": {}, "history": []})
        data.setdefault("active", {})
        data.setdefault("history", [])
        return data

    def _save_visits(self, data: dict):
        atomic_json_save_or_raise(CAMP_VISIT_FILE, data, indent=2)

    def _get_member_camp(self, member: discord.Member) -> Optional[str]:
        """멤버가 지금 갖고 있는 캠프 역할을 캠프 이름(문자열)으로 반환합니다. (없으면 None)"""
        for value, role_id in CAMP_ROLE_IDS.items():
            if any(r.id == role_id for r in member.roles):
                return value
        return None

    # ---------- 🎫 일일견학권 ----------
    # 상점에서 파는 '일일견학권'으로 보낸 견학을 일반 견학과 구분해서 관리해요.
    #
    # 📅 [중요] 여기서 '하루'는 24시간이 아니라 **달력상 그 날**이에요.
    #    밤 11시 59분에 1일권을 쓰면 1분 뒤 자정에 끝납니다. (하루를 꽉 채워 쓰는 게 아니에요)
    #    그래서 만료 시각은 언제나 **자정 정각(다음 날 00:00 KST)** 으로 떨어집니다.
    #      · 1장 → 오늘 자정까지
    #      · 2장 → 내일 자정까지
    #      · 3장 → 모레 자정까지
    #
    # 한 사람이 여러 장을 사서 며칠씩 머무는 경우가 있어서, 이미 견학 중인 사람에게
    # 또 태우면 만료일을 **덮어쓰지 않고 마지막 날 뒤에 날짜를 이어붙입니다.**
    # 그래서 1일권 3장을 따로따로 태워도 3일권 한 장과 결과가 같아요.

    MAX_PASS_DAYS = 30      # 한 번에 태울 수 있는 최대 일수 (오타로 999일이 들어가는 사고 방지)

    # 🎟️ 일일견학권으로 보낼 때 실제로 차감할 상점 아이템.
    # 정확히 이 이름을 먼저 찾고, 없으면 이름에 VISIT_PASS_KEYWORD가 들어간 상품으로 넓혀서 찾아요.
    # (상품명 앞뒤에 이모지를 붙여 등록해도 걸리도록. 상품 이름을 바꾸면 여기만 고치면 됩니다)
    VISIT_PASS_ITEM = "타 캠프 1일 견학권"
    VISIT_PASS_KEYWORD = "견학권"

    async def _refund_pass(self, shop_cog, user_id: int, taken: list) -> str:
        """차감했던 견학권을 되돌리고, 안내 메세지에 덧붙일 문구를 돌려줍니다.

        되돌리기까지 실패하면(파일 저장 불가 등) 관리자가 손으로 넣어줘야 하므로 그 사실을 알려줘요.
        """
        if not taken:
            return ""
        total = sum(row["count"] for row in taken)
        try:
            async with self.bot.economy_lock:
                shop_cog.restore_items(user_id, taken)
            return f"\n└ 🎟️ 차감했던 견학권 **{total}장**은 다시 넣어드렸어요."
        except Exception as e:
            print(f"❗ [견학 보내기] 견학권 되돌리기 실패 (유저 {user_id}): {type(e).__name__}: {e}")
            return (f"\n└ 🔴 **견학권 {total}장을 되돌리지 못했어요.** ({type(e).__name__}) "
                    f"관리자가 `/지급`이나 상점에서 직접 채워주셔야 해요.")

    @staticmethod
    def _parse_dt(value: str) -> Optional[dt.datetime]:
        """저장된 ISO 시각 문자열을 datetime으로 바꿉니다. 깨져 있으면 None."""
        try:
            parsed = dt.datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        # 예전 기록에는 타임존이 없을 수 있어서, 없으면 KST로 간주합니다.
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=KST)

    @staticmethod
    def _humanize(delta: dt.timedelta) -> str:
        """timedelta를 '2일 3시간' 같은 짧은 한국어로 바꿉니다."""
        total = max(0, int(delta.total_seconds()))
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            return f"{days}일 {hours}시간" if hours else f"{days}일"
        if hours:
            return f"{hours}시간 {minutes}분" if minutes else f"{hours}시간"
        return f"{minutes}분"

    @staticmethod
    def _end_of_day(d: dt.date) -> dt.datetime:
        """그 날의 끝(= 다음 날 00:00 KST)을 돌려줍니다. 이 시각이 되는 순간 견학권이 끝나요."""
        return dt.datetime.combine(d + dt.timedelta(days=1), dt.time.min).replace(tzinfo=KST)

    @staticmethod
    def _last_day_of(expires: dt.datetime) -> dt.date:
        """만료 시각(자정)에서 '마지막으로 유효한 날짜'를 되짚습니다."""
        return (expires - dt.timedelta(seconds=1)).date()

    def _extend_day_pass(self, entry: dict, days: int, now: dt.datetime, executor_id: int) -> dict:
        """일일견학권 일수를 기존 권에 합산합니다. (날짜 단위, 만료는 항상 자정)

        아직 기간이 남아 있으면 마지막 날 뒤에 days일을 이어붙이고,
        이미 끝났거나 처음 받는 거라면 **오늘이 1일차**라서 days-1일만 더합니다.
        (1장이면 오늘 자정까지, 2장이면 내일 자정까지)
        """
        pass_info = entry.get("pass") or {}
        prev_expires = self._parse_dt(pass_info.get("expires_at", ""))

        if prev_expires and prev_expires > now:
            last_day = self._last_day_of(prev_expires) + dt.timedelta(days=days)
        else:
            last_day = now.date() + dt.timedelta(days=days - 1)

        expires_at = self._end_of_day(last_day)

        grants = list(pass_info.get("grants", []))
        grants.append({"days": days, "at": now.isoformat(), "by": executor_id})

        entry["pass"] = {
            "total_days": int(pass_info.get("total_days", 0)) + days,
            "started_at": pass_info.get("started_at") or now.isoformat(),
            "last_day": last_day.isoformat(),      # 사람이 읽기 쉬우라고 같이 남겨둬요
            "expires_at": expires_at.isoformat(),
            "grants": grants,
        }
        return entry["pass"]

    def _pass_deadline_text(self, pass_info: dict, now: dt.datetime) -> str:
        """'8/10 자정까지 (오늘 포함 3일)' 같은 마감 안내 문구를 만듭니다."""
        expires = self._parse_dt(pass_info.get("expires_at", ""))
        if not expires:
            return "기한 정보 없음"
        last_day = self._last_day_of(expires)
        days_left = (last_day - now.date()).days      # 0이면 오늘이 마지막 날
        if days_left <= 0:
            return "오늘 자정까지"
        return f"{last_day.strftime('%m/%d')} 자정까지 (오늘 포함 {days_left + 1}일)"

    def _pass_status(self, info: dict, now: dt.datetime) -> Optional[str]:
        """조회 화면에 붙일 일일견학권 상태 문구. 일일견학권이 아니면 None을 돌려줍니다."""
        pass_info = info.get("pass")
        if not pass_info:
            return None

        total = pass_info.get("total_days", 0)
        tickets = len(pass_info.get("grants", []))
        label = f"{total}일권" + (f" ({tickets}장 합산)" if tickets > 1 else "")

        expires = self._parse_dt(pass_info.get("expires_at", ""))
        if not expires:
            return f"🎫 {label}"

        last_day = self._last_day_of(expires)
        if now >= expires:
            return f"🎫 {label} · ⚠️ **만료** ({last_day.strftime('%m/%d')} 자정에 끝났어요)"

        days_left = (last_day - now.date()).days
        if days_left <= 0:
            # 오늘이 마지막 날 — 자정까지 얼마 안 남았을 수 있어서 남은 시간도 같이 보여줍니다.
            return f"🎫 {label} · ⏳ **오늘 자정까지** ({self._humanize(expires - now)} 남음)"
        return f"🎫 {label} · **{last_day.strftime('%m/%d')} 자정까지** · 오늘 포함 {days_left + 1}일 남음"

    @staticmethod
    def _fit_lines(lines: list, limit: int = 1024) -> str:
        """임베드 필드 1024자 제한에 맞춰 줄 단위로 자릅니다. (줄 중간에서 잘리지 않게)"""
        out, used = [], 0
        for i, line in enumerate(lines):
            tail = f"…외 {len(lines) - i}명"
            if used + len(line) + 1 + len(tail) + 1 > limit:
                out.append(tail)
                break
            out.append(line)
            used += len(line) + 1
        return "\n".join(out)

    @visit_group.command(name="보내기", description="[관리자] 멤버를 다른 캠프로 견학 보냅니다. (원래 캠프 역할은 자동으로 제거돼요)")
    @app_commands.describe(
        유저="견학 보낼 멤버",
        캠프="보낼 목적지 캠프",
        일일견학권="상점에서 판매하는 일일견학권으로 보내는 건지 (상점주인이 보내면 자동으로 켜져요)",
        일수="견학권을 몇 장 쓸지 (기본 1장). 1장=오늘 자정까지, 2장=내일 자정까지. 이미 견학 중이면 마지막 날 뒤에 이어붙어요",
    )
    @app_commands.choices(캠프=[
        app_commands.Choice(name="악동캠프", value="악동"),
        app_commands.Choice(name="나래캠프", value="나래"),
        app_commands.Choice(name="여백캠프", value="여백"),
    ])
    @app_commands.guild_only()
    async def send_visit(self, interaction: discord.Interaction, 유저: discord.Member, 캠프: app_commands.Choice[str],
                         일일견학권: Optional[bool] = None, 일수: int = 1):
        if not self._can_manage_visit(interaction):
            return await interaction.response.send_message("🙅‍♀️ 견학 처리는 캠프장·상점주인(또는 관리자)만 가능해요!", ephemeral=True)

        # 🎫 상점주인 자격으로만 통과한 사람이 보내는 건 언제나 일일견학권이에요.
        # 캠프장·전율·관리자는 직접 고르고, 안 고르면 예전처럼 일반 견학입니다.
        shop_only = not self._is_camp_leader(interaction)
        forced_note = ""
        if shop_only:
            if 일일견학권 is False:
                forced_note = "\n└ ℹ️ 상점주인이 보내는 견학은 항상 일일견학권으로 기록돼요."
            일일견학권 = True
        일일견학권 = bool(일일견학권)

        if 일일견학권 and not (1 <= 일수 <= self.MAX_PASS_DAYS):
            return await interaction.response.send_message(
                f"❌ 일수는 1~{self.MAX_PASS_DAYS}일 사이로 넣어주세요. (지금 값: {일수})", ephemeral=True
            )

        dest_role = interaction.guild.get_role(CAMP_ROLE_IDS[캠프.value])
        if not dest_role:
            return await interaction.response.send_message(f"❌ '{캠프.name}' 역할을 서버에서 찾을 수 없어요.", ephemeral=True)

        current_camp = self._get_member_camp(유저)
        if current_camp == 캠프.value:
            return await interaction.response.send_message(f"❌ {유저.mention}님은 이미 {캠프.name} 소속이에요.", ephemeral=True)

        visits = self._load_visits()
        uid = str(유저.id)
        now = dt.datetime.now(KST)

        # 🏠 home_camp: 이미 견학 중이었다면 최초 기록된 "원래 집"을 그대로 유지,
        # 처음 견학 보내는 거라면 지금 캠프(교체 전)가 곧 home이 됨
        if uid in visits["active"]:
            home_camp = visits["active"][uid]["home_camp"]
        else:
            home_camp = current_camp or "미상"

        # 🏠 원래 집으로 돌아가는 건 '복귀'라서 견학권을 쓰지 않아요.
        going_home = (캠프.value == home_camp)
        if going_home and 일일견학권:
            일일견학권 = False
            forced_note = "\n└ ℹ️ 원래 소속 캠프로 돌아가는 복귀라서 견학권은 차감하지 않았어요."

        # 🎟️ 일일견학권은 실제로 상점 아이템을 소모해요. 없으면 아예 보내지 않습니다.
        # 💡 역할을 바꾸기 **전에** 먼저 차감하는 건 일부러예요. 반대로 하면 연타했을 때
        #    둘 다 보유 검사를 통과해서 표 한 장으로 두 번 보내집니다. (`/고확`과 같은 이유)
        #    대신 이후 단계가 실패하면 아래에서 되돌려줘요.
        shop_cog = self.bot.get_cog("ChunsikShop")
        taken = []
        if 일일견학권:
            if not shop_cog or not hasattr(shop_cog, "consume_items_by_name"):
                return await interaction.response.send_message(
                    "❌ 상점 시스템을 불러올 수 없어서 일일견학권을 확인하지 못했어요.", ephemeral=True)

            # 🔒 구매·되팔기와 같은 락을 잡아야 shop.json 저장이 겹치지 않아요.
            async with self.bot.economy_lock:
                err, taken = shop_cog.consume_items_by_name(
                    유저.id, self.VISIT_PASS_KEYWORD, 일수, exact=self.VISIT_PASS_ITEM
                )

            if err:
                return await interaction.response.send_message(
                    f"❌ {유저.mention}님의 인벤토리에 **{self.VISIT_PASS_ITEM}**이(가) 부족해요.\n"
                    f"└ 보유 **{err['have']}장** / 필요 **{일수}장**\n"
                    f"└ 상점에서 견학권을 먼저 구매하셔야 해요.",
                    ephemeral=True,
                )

        await interaction.response.defer()

        # 🔁 역할 교체: 기존 캠프 역할 제거 + 목적지 캠프 역할 지급
        try:
            if current_camp:
                old_role = interaction.guild.get_role(CAMP_ROLE_IDS[current_camp])
                if old_role:
                    await 유저.remove_roles(old_role)
            await 유저.add_roles(dest_role)
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ 봇에게 역할 변경 권한이 없어요." + await self._refund_pass(shop_cog, 유저.id, taken)
            )

        # 🔄 역할을 바꾸는 동안(await) 다른 사람의 견학 처리가 끼어들어 파일이 바뀌었을 수 있어요.
        # 위에서 읽어둔 사본을 그대로 저장하면 그 사이 기록이 통째로 날아가므로, 여기서 다시 읽습니다.
        # (home_camp는 위에서 정한 값을 그대로 씁니다. 견학권을 이미 그 기준으로 차감했으니까요)
        visits = self._load_visits()

        visits["history"].append({
            "user_id": 유저.id,
            "from": current_camp or "미상",
            "to": 캠프.value,
            "timestamp": now.isoformat(),
            "executor_id": interaction.user.id,
            "pass": 일일견학권,
            "pass_days": 일수 if 일일견학권 else 0,
        })

        pass_line = ""
        if going_home:
            # 🏠 원래 집으로 복귀 → 더 이상 "견학 중"이 아니므로 active에서 제거
            # (남아있던 일일견학권 기록도 같이 사라져요)
            visits["active"].pop(uid, None)
            status_text = "원래 소속 캠프로 복귀 처리됐어요."
        else:
            # 이미 견학 중이면 그 기록을 이어받아요. (일일견학권 일수를 합산해야 하니까요)
            entry = visits["active"].get(uid) or {}
            entry["home_camp"] = home_camp
            entry["current_camp"] = 캠프.value
            entry.setdefault("since", now.isoformat())

            if 일일견학권:
                pass_info = self._extend_day_pass(entry, 일수, now, interaction.user.id)
                tickets = len(pass_info.get("grants", []))
                pass_line = (
                    f"\n└ 🎫 **일일견학권 {일수}장** 사용 → 누적 **{pass_info['total_days']}일권**"
                    + (f" ({tickets}장 합산)" if tickets > 1 else "")
                    + f"\n└ ⏰ **{self._pass_deadline_text(pass_info, now)}**"
                    + "\n└ 📅 견학권은 시간이 아니라 **날짜** 기준이에요. 자정이 지나면 하루가 소진돼요."
                )
            else:
                # 일반 견학으로 보내면 붙어 있던 일일견학권은 해제합니다.
                entry.pop("pass", None)

            visits["active"][uid] = entry
            status_text = f"현재 견학 중 (원 소속: {home_camp}캠프)"

        try:
            self._save_visits(visits)
        except Exception as e:
            # 💾 저장이 실패하면 견학 기록이 안 남아요. 역할은 이미 바뀐 뒤라 되돌릴 수 없지만,
            # 차감한 견학권만큼은 돌려드려야 표를 그냥 날리는 일이 없습니다.
            print(f"❗ [견학 보내기] 기록 저장 실패: {type(e).__name__}: {e}")
            return await interaction.followup.send(
                f"⚠️ 역할은 바꿨지만 견학 기록을 저장하지 못했어요. ({type(e).__name__})\n"
                f"└ 견학 목록에 안 잡히니 `/견학 보내기`를 다시 실행해 주세요."
                + await self._refund_pass(shop_cog, 유저.id, taken)
            )

        await interaction.followup.send(
            f"✅ {유저.mention}님을 {current_camp + '캠프' if current_camp else '(소속 없음)'}에서 **{캠프.name}**(으)로 보냈어요.\n"
            f"└ {status_text}"
            f"{pass_line}{forced_note}\n"
            f"└ 💡 견학 중인 인원은 `/세금환수` 대상에서 자동으로 제외돼요."
        )

    @visit_group.command(name="조회", description="[관리자] 현재 견학 중인 인원을 확인합니다. (누가 어느 캠프에서 어느 캠프로 갔는지)")
    @app_commands.guild_only()
    async def check_visits(self, interaction: discord.Interaction):
        if not self._can_manage_visit(interaction):
            return await interaction.response.send_message("🙅‍♀️ 견학 조회는 캠프장·상점주인(또는 관리자)만 가능해요!", ephemeral=True)

        visits = self._load_visits()
        active = visits.get("active", {})
        if not active:
            return await interaction.response.send_message("ℹ️ 현재 견학 중인 인원이 없어요.", ephemeral=True)

        # 🎫 일일견학권으로 온 사람과 일반 견학을 나눠서 보여줍니다.
        # 일일견학권은 여러 장을 합산할 수 있어서 남은 기간·만료 여부가 중요해요.
        now = dt.datetime.now(KST)
        pass_lines, normal_lines, expired_count = [], [], 0

        for uid, info in active.items():
            member = interaction.guild.get_member(int(uid))
            name = member.display_name if member else f"(탈퇴/알수없음: {uid})"
            route = f"{info.get('home_camp', '미상')}캠프 → {info.get('current_camp', '미상')}캠프"

            status = self._pass_status(info, now)
            if status:
                if "만료" in status:
                    expired_count += 1
                pass_lines.append(f"**{name}**  |  {route}\n└ {status}")
            else:
                normal_lines.append(f"**{name}**  |  {route}  (since {info.get('since', '')[:10]})")

        embed = discord.Embed(
            title="🚌 현재 견학 중인 인원",
            description=f"총 **{len(active)}명** (일일견학권 {len(pass_lines)}명 · 일반 {len(normal_lines)}명)",
            color=0x5ce6b4,
            timestamp=now,
        )
        if pass_lines:
            header = f"🎫 일일견학권 ({len(pass_lines)}명"
            header += f" · ⚠️ 만료 {expired_count}명)" if expired_count else ")"
            embed.add_field(name=header, value=self._fit_lines(pass_lines), inline=False)
        if normal_lines:
            embed.add_field(name=f"🚌 일반 견학 ({len(normal_lines)}명)",
                            value=self._fit_lines(normal_lines), inline=False)
        if expired_count:
            embed.add_field(
                name="⚠️ 만료된 일일견학권이 있어요",
                value="기간이 지나도 자동으로 돌아가지는 않아요. `/견학 복귀`로 직접 처리해 주세요.",
                inline=False,
            )

        embed.set_footer(text=f"조회자: {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @visit_group.command(name="내역", description="[관리자] 지금까지의 견학 이동 기록 전체를 조회합니다.")
    @app_commands.guild_only()
    async def visit_history(self, interaction: discord.Interaction):
        if not self._is_camp_leader(interaction):
            return await interaction.response.send_message("🙅‍♀️ 견학내역 조회는 캠프장(또는 관리자)만 가능해요!", ephemeral=True)

        visits = self._load_visits()
        history = visits.get("history", [])
        if not history:
            return await interaction.response.send_message("ℹ️ 아직 견학 이동 기록이 없어요.", ephemeral=True)

        lines = []
        for h in reversed(history):  # 최신순
            member = interaction.guild.get_member(h["user_id"])
            name = member.display_name if member else f"(탈퇴/알수없음: {h['user_id']})"
            try:
                ts = dt.datetime.fromisoformat(h["timestamp"]).strftime("%Y-%m-%d %H:%M")
            except Exception:
                ts = h.get("timestamp", "")
            lines.append(f"[{ts}] {name}: {h['from']}캠프 → {h['to']}캠프")

        chunks = chunk_lines(lines)

        for idx, chunk in enumerate(chunks[:5]):  # 너무 길어지지 않게 최대 5개 임베드까지만
            embed = discord.Embed(
                title="🚌 견학 이동 기록" + (f" ({idx+1}/{len(chunks)})" if len(chunks) > 1 else ""),
                description=f"```\n{chunk}\n```",
                color=0x5ce6b4,
            )
            if idx == 0:
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)

    @visit_group.command(name="복귀", description="[관리자] 견학 중인 인원을 원래 캠프로 복귀시킵니다. 캠프 선택 시 '그 캠프가 원 소속'인 인원만, '전원' 선택 시 견학 중인 모두 복귀해요.")
    @app_commands.describe(캠프="복귀시킬 대상의 원래(원 소속) 캠프. '전원'을 고르면 견학 중인 모든 인원이 한번에 복귀해요.")
    @app_commands.choices(캠프=[
        app_commands.Choice(name="악동캠프", value="악동"),
        app_commands.Choice(name="나래캠프", value="나래"),
        app_commands.Choice(name="여백캠프", value="여백"),
        app_commands.Choice(name="전원", value="전원"),
    ])
    @app_commands.guild_only()
    async def return_visits(self, interaction: discord.Interaction, 캠프: app_commands.Choice[str]):
        if not self._can_manage_visit(interaction):
            return await interaction.response.send_message("🙅‍♀️ 견학 복귀 처리는 캠프장·상점주인(또는 관리자)만 가능해요!", ephemeral=True)

        visits = self._load_visits()
        active = visits.get("active", {})

        # 🎯 대상 선정: '전원'이면 견학 중인 전부, 특정 캠프면 그 캠프가 "원 소속(home_camp)"인 사람만
        targets = [
            (uid, info) for uid, info in list(active.items())
            if 캠프.value == "전원" or info.get("home_camp") == 캠프.value
        ]

        if not targets:
            return await interaction.response.send_message(
                f"ℹ️ {'견학 중인 인원이 없어요.' if 캠프.value == '전원' else f'{캠프.name} 소속으로 견학 중인 인원이 없어요.'}",
                ephemeral=True
            )

        await interaction.response.defer()

        returned, failed, cleaned = [], [], []
        # 🔄 파일에 반영할 내용은 여기 모아뒀다가, 역할 변경이 다 끝난 뒤 한 번에 씁니다.
        # (아래 저장 직전 주석 참고)
        done_uids, new_history = [], []

        for uid, info in targets:
            home_value = info.get("home_camp")
            current_value = info.get("current_camp")
            member = interaction.guild.get_member(int(uid))

            if not member:
                # 서버에서 이미 나간 멤버 → 역할 변경은 불가능하니 기록만 정리
                done_uids.append(uid)
                cleaned.append(uid)
                continue

            try:
                current_role = interaction.guild.get_role(CAMP_ROLE_IDS.get(current_value)) if current_value else None
                home_role = interaction.guild.get_role(CAMP_ROLE_IDS.get(home_value)) if home_value else None

                if current_role and current_role in member.roles:
                    await member.remove_roles(current_role)
                if home_role:
                    await member.add_roles(home_role)

                new_history.append({
                    "user_id": member.id,
                    "from": current_value or "미상",
                    "to": home_value or "미상",
                    "timestamp": dt.datetime.now(KST).isoformat(),
                    "executor_id": interaction.user.id,
                })
                done_uids.append(uid)
                returned.append(f"{member.display_name} ({current_value}캠프 → {home_value}캠프)")
            except discord.Forbidden:
                failed.append(member.display_name)

        # 🐛 [버그 수정] 예전엔 맨 위에서 읽어둔 `visits` 사본을 그대로 저장했어요. 그런데 바로 위
        # 반복문이 인원 수만큼 역할 변경을 await 하기 때문에, 그 몇 초 사이에 다른 사람이 실행한
        # `/견학 보내기` 기록이 통째로 덮어써져 사라졌습니다. (send_visit은 같은 이유로 저장 직전에
        # 파일을 다시 읽도록 이미 고쳐뒀는데, 복귀 쪽만 빠져 있었어요)
        # 이제 파일을 새로 읽어서 "이번에 처리한 사람"만 반영합니다.
        save_warning = ""
        try:
            visits = self._load_visits()
            for uid in done_uids:
                visits["active"].pop(uid, None)
            visits["history"].extend(new_history)
            self._save_visits(visits)
        except Exception as e:
            # 역할은 이미 바뀐 뒤라 되돌릴 수 없어요. 기록만 안 남았다는 걸 정확히 알려야
            # 관리자가 "복귀가 안 됐나?" 하고 또 실행하는 일이 없습니다.
            print(f"❗ [견학 복귀] 기록 저장 실패: {type(e).__name__}: {e}")
            save_warning = (f"\n\n🔴 **역할은 바꿨지만 견학 기록을 저장하지 못했어요.** ({type(e).__name__})\n"
                            f"└ `/견학 조회`에 아직 견학 중으로 남아 있을 수 있어요. 잠시 후 다시 실행해 주세요.")

        lines = []
        if returned:
            lines.append(f"✅ **{len(returned)}명 복귀 완료**\n" + "\n".join(returned))
        if failed:
            lines.append(f"⚠️ **역할 변경 권한 부족으로 실패** ({len(failed)}명): {', '.join(failed)}")
        if cleaned:
            lines.append(f"🧹 서버에서 이미 나간 인원 기록 {len(cleaned)}건은 목록에서만 정리했어요.")

        embed = discord.Embed(
            title=f"🚌 견학 복귀 처리 결과 ({캠프.name})",
            description=("\n\n".join(lines) if lines else "처리할 대상이 없었어요.") + save_warning,
            color=0x5ce6b4,
            timestamp=dt.datetime.now(KST),
        )
        embed.set_footer(text=f"집행자: {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    # ================== 🧾 캠프별 세금 시스템 ==================
    def _camp_choice_list(self):
        return [
            app_commands.Choice(name="악동캠프", value="악동"),
            app_commands.Choice(name="나래캠프", value="나래"),
            app_commands.Choice(name="여백캠프", value="여백"),
        ]

    def _check_camp_leader_scope(self, interaction: discord.Interaction, camp: str) -> Optional[str]:
        """이 사람이 해당 캠프를 다룰 수 있는지 확인합니다. 문제가 있으면 안내 문구를, 없으면 None을 돌려줘요.

        관리자와 전율(캠프 관리 기구)은 모든 캠프를, 캠프장은 본인이 속한 캠프만 다룰 수 있어요.
        세금 기준·세금환수와 캠프통장(송금·회수)이 이 함수 하나를 같이 씁니다.

        🐛 [버그 수정] 예전엔 이 클래스 안에 같은 이름의 메서드가 **두 개** 있었어요.
        파이썬은 나중 정의가 앞 정의를 통째로 덮어쓰기 때문에, 위쪽에 있던 이 정의는
        한 번도 불린 적이 없는 죽은 코드였습니다. 판정 로직 자체는 똑같아서 권한이
        새거나 막히지는 않았지만, 안내 문구가 아래쪽(통장용) 것으로 고정돼서
        `/캠프 세금기준`·`/캠프 세금환수`가 거부될 때 "캠프통장은 캠프장만 다룰 수
        있어요"라는 엉뚱한 말이 나갔어요. 하나로 합치고 문구도 중립적으로 바꿨습니다.
        """
        if interaction.user.guild_permissions.administrator:
            return None
        if any(r.id == JEONYUL_ROLE_ID for r in interaction.user.roles):
            return None
        if not any(r.id == CAMP_LEADER_ROLE_ID for r in interaction.user.roles):
            return "🙅‍♀️ 캠프장(또는 관리자)만 사용할 수 있어요!"
        if not any(r.id == CAMP_ROLE_IDS[camp] for r in interaction.user.roles):
            return f"🙅‍♀️ 본인이 속한 캠프만 다룰 수 있어요! ({camp}캠프는 담당이 아니에요.)"
        return None

    # 🗑️ [정리] 여기 있던 _stock_value_of는 chunsik_utils.portfolio_value와 하는 일이 같았어요.
    # 평가액 계산이 캠프·지갑·통계·종가게시 네 군데에 복붙돼 있었고, 옛 형식 보유 기록을
    # 받아주는 처리가 곳마다 달라서 예전 데이터가 남아 있으면 화면마다 금액이 어긋날 수
    # 있었습니다. 공용 함수 하나로 모았어요.

    @camp_treasury_group.command(name="세금기준", description="[캠프장] 우리 캠프의 세금 기준이 지금 어떻게 되어 있는지 확인합니다.")
    @app_commands.describe(캠프="확인할 캠프")
    @app_commands.choices(캠프=[
        app_commands.Choice(name="악동캠프", value="악동"),
        app_commands.Choice(name="나래캠프", value="나래"),
        app_commands.Choice(name="여백캠프", value="여백"),
    ])
    @app_commands.guild_only()
    async def show_tax_rule(self, interaction: discord.Interaction, 캠프: app_commands.Choice[str]):
        err = self._check_camp_leader_scope(interaction, 캠프.value)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)

        config = load_tax_config()
        cfg = config[캠프.value]
        embed = discord.Embed(
            title=f"🧾 {캠프.name} 세금 기준",
            description=rule_summary(캠프.value, cfg),
            color=0x5CE6B4,
            timestamp=dt.datetime.now(KST),
        )
        embed.add_field(
            name="✏️ 고치려면",
            value="`/캠프 세금기준수정` 명령을 쓰면 지금 값이 그대로 담긴 창이 열려요. 숫자만 바꿔서 제출하면 바로 반영됩니다.",
            inline=False,
        )
        embed.set_footer(text="이 기준은 /캠프 세금환수를 실행할 때 그대로 적용돼요.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @camp_treasury_group.command(name="세금기준수정", description="[캠프장] 우리 캠프의 세율·구간·확률을 직접 고칩니다.")
    @app_commands.describe(캠프="기준을 고칠 캠프")
    @app_commands.choices(캠프=[
        app_commands.Choice(name="악동캠프", value="악동"),
        app_commands.Choice(name="나래캠프", value="나래"),
        app_commands.Choice(name="여백캠프", value="여백"),
    ])
    @app_commands.guild_only()
    async def edit_tax_rule(self, interaction: discord.Interaction, 캠프: app_commands.Choice[str]):
        err = self._check_camp_leader_scope(interaction, 캠프.value)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)

        config = load_tax_config()
        await interaction.response.send_modal(TaxRuleModal(self, 캠프.value, 캠프.name, config[캠프.value]))

    @camp_treasury_group.command(name="세금환수", description="[캠프장] 캠프 세금 기준대로 세금을 계산해 걷습니다. (기본은 미리보기)")
    @app_commands.describe(
        캠프="세금을 걷을 캠프",
        실제징수="True로 해야 진짜 걷어요. 비워두면 계산 결과만 보여드립니다.",
        정률="캠프 기준 대신 전원에게 똑같은 비율(%)을 적용해요. (나래 첫 회 일괄 10% 같은 예외 상황용)",
        참여자역할="[악동 전용] 세금 룰렛에 참여한 사람들의 역할. 지정하지 않으면 전원 미참여로 최고액을 뭅니다.",
    )
    @app_commands.choices(캠프=[
        app_commands.Choice(name="악동캠프", value="악동"),
        app_commands.Choice(name="나래캠프", value="나래"),
        app_commands.Choice(name="여백캠프", value="여백"),
    ])
    @app_commands.guild_only()
    async def collect_tax(self, interaction: discord.Interaction, 캠프: app_commands.Choice[str],
                          실제징수: bool = False, 정률: Optional[float] = None,
                          참여자역할: Optional[discord.Role] = None):
        economy_cog = self._get_economy_cog()
        if not economy_cog:
            return await interaction.response.send_message("❌ 경제 시스템 코그를 찾을 수 없어요.", ephemeral=True)

        err = self._check_camp_leader_scope(interaction, 캠프.value)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)

        if 정률 is not None and not (0 < 정률 <= 100):
            return await interaction.response.send_message("❌ 정률은 0 초과 100 이하로 입력해 주세요.", ephemeral=True)

        await interaction.response.defer()

        role = interaction.guild.get_role(CAMP_ROLE_IDS[캠프.value])
        if not role:
            return await interaction.followup.send(f"❌ '{캠프.name}' 역할을 서버에서 찾을 수 없어요.")

        # 🚫 세금 징수 제외 대상 판별
        # - 현재 견학 중인 멤버 (🚌 /견학 시스템과 연동)
        # - 닉네임 어디든 "부계정"이 포함된 멤버
        # - 이 캠프 말고 다른 캠프 역할도 같이 달고 있는 멤버 (소속이 애매한 겸직자)
        visits = self._load_visits()
        active_visitors = visits.get("active", {})
        EXCLUDE_NICK_KEYWORDS = ("부계정",)

        def _is_tax_excluded(member: discord.Member) -> tuple:
            if str(member.id) in active_visitors:
                return True, "견학 중"
            for kw in EXCLUDE_NICK_KEYWORDS:
                if kw in member.display_name:
                    return True, kw
            member_camp_roles = [cid for cid in CAMP_ROLE_IDS.values() if any(r.id == cid for r in member.roles)]
            if len(member_camp_roles) > 1:
                return True, "다른 캠프 겸직"
            return False, ""

        members, excluded_rows = [], []
        for m in role.members:
            if m.bot:
                continue
            is_excluded, reason = _is_tax_excluded(m)
            if is_excluded:
                excluded_rows.append((m.display_name, reason))
            else:
                members.append(m)

        if not members:
            return await interaction.followup.send(f"❌ {캠프.name}에 세금 징수 대상 인원이 없어요. (제외 {len(excluded_rows)}명)")

        config = load_tax_config()
        cfg = config[캠프.value]
        kind = CAMP_TAX_TYPE.get(캠프.value)
        participants = {m.id for m in 참여자역할.members} if 참여자역할 else set()

        # 🧮 계산과 저장은 락 안에서, 결과 안내는 락 밖에서 합니다.
        # (economy_lock은 서버 전체 경제의 공용 관문이라, 쥔 채로 메세지를 보내면 다 같이 멈춰요)
        rows = []               # (이름, 과세표준, 세액, 실제징수액, 미납액, 티켓, 주식평가액, 주식보유세)
        ledger_rows = []        # 🧾 원장에 한 묶음으로 남길 (유저id, 변동액, 징수후잔액)
        total_collected = 0
        total_unpaid = 0
        total_stock_tax = 0
        before = after = 0

        async with self.bot.economy_lock:
            economy = economy_cog._load_raw_economy()

            # 📈 나래는 자산 조사에, 여백은 주식 보유세 계산에 주식 평가액이 필요해요.
            needs_stocks = (kind == "flat_bracket" and cfg.get("include_stocks")) or \
                           (kind == "progressive" and cfg.get("stock_tax_rate", 0) > 0)
            stock_data = {}
            if needs_stocks:
                stock_cog = self.bot.get_cog("ChunsikStock")
                if stock_cog:
                    try:
                        stock_data = stock_cog._load_stocks()
                    except Exception as e:
                        print(f"⚠️ [세금환수] 주식 평가액을 불러오지 못했어요: {type(e).__name__}: {e}")

            for m in members:
                uid = str(m.id)
                balance = economy.get(uid, 0)
                if not isinstance(balance, (int, float)):
                    balance = 0
                balance = int(balance)

                stock_value = portfolio_value(stock_data, uid) if needs_stocks else 0

                # 📐 과세표준: 나래는 지갑+주식을 합쳐 하나로 보고,
                #    여백은 지갑세와 주식 보유세를 따로 매겨서 더합니다.
                base = balance
                if kind == "flat_bracket" and cfg.get("include_stocks"):
                    base = balance + stock_value

                # 💰 세액
                stock_tax = 0
                if 정률 is not None:
                    tax = int(base * 정률 / 100) if base > 0 else 0
                elif kind == "progressive":
                    wallet_tax = progressive_tax(cfg, balance)
                    stock_tax = stock_holding_tax(cfg, stock_value)
                    tax = wallet_tax + stock_tax
                elif kind == "flat_bracket":
                    tax = flat_bracket_tax(cfg, base)
                elif kind == "roulette":
                    tax = roulette_tax(cfg, m.id in participants)
                else:
                    tax = 0

                # 🪙 지갑에 있는 만큼만 걷어요. 모자란 건 미납으로 남겨 캠프장이 처리하게 합니다.
                # (악동의 노비 페널티, 나래의 미납자 강제징수가 여기에 걸립니다)
                paid = max(0, min(tax, balance))
                unpaid = tax - paid
                tickets = lottery_tickets(cfg, paid) if kind == "progressive" else 0

                if 실제징수 and paid > 0:
                    economy[uid] = balance - paid
                    ledger_rows.append((uid, -paid, economy[uid]))
                total_collected += paid
                total_unpaid += unpaid
                total_stock_tax += stock_tax
                rows.append((m.display_name, base, tax, paid, unpaid, tickets, stock_value, stock_tax))

            if 실제징수:
                economy_cog._save_raw_economy(economy)
                treasury = self._load_camp_treasury()
                before = treasury.get(캠프.value, 0)
                after = before + total_collected
                treasury[캠프.value] = after
                self._save_camp_treasury(treasury)

        rule_label = f"정률 {정률:g}%" if 정률 is not None else {
            "progressive": "초과 누진세", "flat_bracket": "구간별 단일세율", "roulette": "세금 룰렛",
        }.get(kind, "기준 없음")

        # 🧾 걷은 세금을 원장에 한 묶음으로 남깁니다. (락을 놓은 뒤에)
        # 유저가 "내 에바 왜 줄었어요?" 하고 /지갑내역을 열었을 때 세금만 안 보이면
        # 관리자가 로그 채널을 뒤져야 했어요. 이제 다른 입출금과 똑같이 남습니다.
        if ledger_rows:
            record_ledger_many(ledger_rows, "캠프 세금", f"{캠프.name} · {rule_label}",
                               actor_id=interaction.user.id)

        # 📊 결과 표 (많이 낸 순)
        rows.sort(key=lambda r: r[3], reverse=True)
        base_label = "총자산" if (kind == "flat_bracket" and cfg.get("include_stocks")) else "잔액"
        lines = []
        for name, base, tax, paid, unpaid, tickets, stock_value, stock_tax in rows:
            line = f"{name}  |  {base_label} {base:,} → -{paid:,} 에바"
            if stock_tax:
                # 주식에 묻어둔 자산이 얼마고 거기서 얼마가 나왔는지 바로 보이게 합니다.
                line += f"  📈주식 {stock_value:,}(-{stock_tax:,})"
            if tickets:
                line += f"  🎲{tickets}장"
            if unpaid:
                line += f"  ⚠️미납 {unpaid:,}"
            lines.append(line)

        chunks = chunk_lines(lines)

        mode = "실제 징수 완료" if 실제징수 else "미리보기 (실제로 걷지 않았어요)"

        for idx, chunk in enumerate(chunks):
            embed = discord.Embed(
                title=f"🧾 {캠프.name} 세금 {'환수' if 실제징수 else '미리보기'} · {rule_label}"
                      + (f" ({idx+1}/{len(chunks)})" if len(chunks) > 1 else ""),
                description=f"```\n{chunk}\n```",
                color=0xe67e22 if 실제징수 else 0x95a5a6,
                timestamp=dt.datetime.now(KST),
            )
            if idx == len(chunks) - 1:
                embed.add_field(name="대상 인원", value=f"{len(members)}명", inline=True)
                embed.add_field(name="걷은 세금 합계", value=f"{total_collected:,} 에바", inline=True)
                if 실제징수:
                    embed.add_field(name=f"🏦 {캠프.name} 통장", value=f"{before:,} → **{after:,} 에바**", inline=True)
                if total_stock_tax:
                    embed.add_field(name="📈 그중 주식 보유세",
                                    value=f"{total_stock_tax:,} 에바 ({cfg.get('stock_tax_rate', 0):g}%)", inline=True)
                if total_unpaid:
                    unpaid_names = ", ".join(f"{r[0]}({r[4]:,})" for r in rows if r[4])
                    embed.add_field(
                        name=f"⚠️ 잔액 부족 미납 ({total_unpaid:,} 에바)",
                        value=((unpaid_names[:950] + "...") if len(unpaid_names) > 950 else unpaid_names)
                              + ("\n└ 주식은 많은데 지갑이 빈 경우예요. 주식 국유화는 아직 도입 전이라 수동 처리가 필요해요."
                                 if kind == "progressive" and total_stock_tax else ""),
                        inline=False,
                    )
                if kind == "progressive":
                    total_tickets = sum(r[5] for r in rows)
                    holders = sum(1 for r in rows if r[5])
                    embed.add_field(name="🎲 로또 티켓", value=f"{holders}명 · 총 {total_tickets}장", inline=False)
                if excluded_rows:
                    excluded_text = ", ".join(f"{n}({r})" for n, r in excluded_rows)
                    embed.add_field(name=f"🚫 징수 제외 ({len(excluded_rows)}명)",
                                    value=(excluded_text[:1000] + "...") if len(excluded_text) > 1000 else excluded_text,
                                    inline=False)
                embed.add_field(name="상태", value=f"**{mode}**", inline=False)
                if not 실제징수:
                    embed.set_footer(text="진짜 걷으려면 실제징수:True 로 다시 실행하세요.")
                else:
                    embed.set_footer(text=f"집행자: {interaction.user.display_name}")
            await interaction.followup.send(embed=embed)

        if 실제징수:
            await economy_cog._log_economy_action(
                title="🧾 [캠프 세금 환수]",
                description=f"**집행자:** {interaction.user.mention}\n"
                            f"**대상 캠프:** {캠프.name} ({len(members)}명)\n"
                            f"**적용 기준:** {rule_label}\n"
                            f"**걷은 세금:** {total_collected:,} 에바\n"
                            f"**미납 합계:** {total_unpaid:,} 에바\n"
                            f"**{캠프.name} 통장:** {before:,} → {after:,} 에바",
                color=0xe67e22
            )

    @camp_treasury_group.command(name="조회", description="[관리자] 캠프별 세금 통장 잔액을 확인합니다.")
    @app_commands.describe(캠프="조회할 캠프 (비우면 전체 캠프)")
    @app_commands.choices(캠프=[
        app_commands.Choice(name="악동캠프", value="악동"),
        app_commands.Choice(name="나래캠프", value="나래"),
        app_commands.Choice(name="여백캠프", value="여백"),
    ])
    async def camp_treasury(self, interaction: discord.Interaction, 캠프: Optional[app_commands.Choice[str]] = None):
        if not self._is_camp_leader(interaction):
            return await interaction.response.send_message("🙅‍♀️ 캠프통장 조회는 캠프장(또는 관리자)만 가능해요!", ephemeral=True)

        treasury = self._load_camp_treasury()
        embed = discord.Embed(title="🏦 캠프 세금 통장 현황", color=0x5CE6B4, timestamp=dt.datetime.now(KST))

        if 캠프 is not None:
            bal = treasury.get(캠프.value, 0)
            embed.add_field(name=f"{캠프.name}", value=f"**{bal:,} 에바**", inline=False)
        else:
            for value in CAMP_ROLE_IDS.keys():
                bal = treasury.get(value, 0)
                embed.add_field(name=f"{value}캠프", value=f"{bal:,} 에바", inline=False)

        embed.set_footer(text=f"조회자: {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @camp_treasury_group.command(name="인출", description="[관리자] 캠프 세금 통장에서 에바를 인출해 본인 지갑으로 받아요.")
    @app_commands.describe(캠프="인출할 캠프 통장", 금액="인출할 에바 금액")
    @app_commands.choices(캠프=[
        app_commands.Choice(name="악동캠프", value="악동"),
        app_commands.Choice(name="나래캠프", value="나래"),
        app_commands.Choice(name="여백캠프", value="여백"),
    ])
    async def withdraw_camp_treasury(self, interaction: discord.Interaction, 캠프: app_commands.Choice[str], 금액: int):
        economy_cog = self._get_economy_cog()
        if not economy_cog:
            return await interaction.response.send_message("❌ 경제 시스템 코그를 찾을 수 없어요.", ephemeral=True)

        # 🔐 권한: 관리자/전율(캠프 관리 기구)는 모든 캠프 / 캠프장은 '본인이 속한 캠프'만 인출 가능
        is_jeonyul = any(r.id == JEONYUL_ROLE_ID for r in interaction.user.roles)
        if not (interaction.user.guild_permissions.administrator or is_jeonyul):
            is_leader = any(r.id == CAMP_LEADER_ROLE_ID for r in interaction.user.roles)
            if not is_leader:
                return await interaction.response.send_message("🙅‍♀️ 캠프통장 인출은 캠프장(또는 관리자)만 가능해요!", ephemeral=True)

            in_this_camp = any(r.id == CAMP_ROLE_IDS[캠프.value] for r in interaction.user.roles)
            if not in_this_camp:
                return await interaction.response.send_message(
                    f"🙅‍♀️ 본인이 속한 캠프 통장만 인출할 수 있어요! ({캠프.name}은(는) 담당 캠프가 아니에요.)",
                    ephemeral=True
                )

        if 금액 <= 0:
            return await interaction.response.send_message("❌ 인출 금액은 1 에바 이상이어야 합니다.", ephemeral=True)

        await interaction.response.defer()

        broken = None       # 두 번째 저장이 실패했을 때의 예외 (반쪽 거래 감지용)

        async with self.bot.economy_lock:
            treasury = self._load_camp_treasury()
            before = treasury.get(캠프.value, 0)

            if before < 금액:
                return await interaction.followup.send(
                    f"❌ {캠프.name} 통장 잔액이 부족합니다! (현재 잔액: {before:,} 에바 / 요청: {금액:,} 에바)"
                )

            after = before - 금액
            treasury[캠프.value] = after
            self._save_camp_treasury(treasury)

            # 💳 인출액을 실행자 본인 지갑으로 입금
            # 💾 통장은 이미 차감돼 저장됐어요. 여기가 실패하면 "통장에서만 빠지고 지갑에는
            # 안 들어온" 반쪽 상태가 됩니다.
            economy = economy_cog._load_raw_economy()
            user_id = str(interaction.user.id)
            user_after = economy.get(user_id, 0) + 금액
            economy[user_id] = user_after
            try:
                economy_cog._save_raw_economy(economy)
            except Exception as save_err:
                broken = save_err
            else:
                broken = None

        if broken:
            return await report_broken_transaction(
                interaction, action="캠프통장 인출", user_id=user_id,
                lost=f"{캠프.name} 통장에서 에바 {금액:,}", not_received=f"에바 {금액:,}",
                error=broken,
            )

        # 🧾 원장에도 남깁니다. (락을 놓은 뒤에)
        record_ledger(user_id, 금액, user_after, "캠프통장 인출", f"{캠프.name}",
                      actor_id=interaction.user.id)

        embed = discord.Embed(
            title="🏦 [캠프통장 인출]",
            color=0x5CE6B4,
            timestamp=dt.datetime.now(KST)
        )
        embed.add_field(name="대상 캠프", value=f"{캠프.name}", inline=True)
        embed.add_field(name="인출 금액", value=f"{금액:,} 에바", inline=True)
        embed.add_field(name="수령자", value=interaction.user.mention, inline=True)
        embed.add_field(name=f"🏦 {캠프.name} 통장", value=f"{before:,} → **{after:,} 에바**", inline=False)
        embed.add_field(name="수령자 지갑 잔액", value=f"{user_after:,} 에바", inline=False)
        embed.set_footer(text=f"집행자: {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

        await economy_cog._log_economy_action(
            title="🏦 [캠프통장 인출]",
            description=f"**집행자:** {interaction.user.mention}\n"
                        f"**대상 캠프:** {캠프.name}\n"
                        f"**인출 금액:** {금액:,} 에바\n"
                        f"**{캠프.name} 통장:** {before:,} → {after:,} 에바\n"
                        f"**수령자 지갑 잔액:** {user_after:,} 에바",
            color=0x5CE6B4
        )

    # 🗑️ [정리] 여기 있던 두 번째 _check_camp_leader_scope는 삭제했어요.
    # 위쪽(세금 섹션)에 완전히 같은 판정을 하는 정의가 이미 있었고, 이게 그걸 덮어쓰고
    # 있었습니다. 자세한 내용은 그쪽 docstring을 봐주세요.

    @camp_treasury_group.command(name="송금", description="[관리자] 캠프 통장에서 특정 유저에게 에바를 보내요. (진짜 지갑처럼 누구에게나 송금 가능)")
    @app_commands.describe(캠프="보낼 캠프 통장", 유저="에바를 받을 유저", 금액="보낼 에바 금액")
    @app_commands.choices(캠프=[
        app_commands.Choice(name="악동캠프", value="악동"),
        app_commands.Choice(name="나래캠프", value="나래"),
        app_commands.Choice(name="여백캠프", value="여백"),
    ])
    async def camp_treasury_send(self, interaction: discord.Interaction, 캠프: app_commands.Choice[str], 유저: discord.Member, 금액: int):
        economy_cog = self._get_economy_cog()
        if not economy_cog:
            return await interaction.response.send_message("❌ 경제 시스템 코그를 찾을 수 없어요.", ephemeral=True)

        err = self._check_camp_leader_scope(interaction, 캠프.value)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)

        if 금액 <= 0:
            return await interaction.response.send_message("❌ 송금 금액은 1 에바 이상이어야 합니다.", ephemeral=True)

        await interaction.response.defer()

        broken = None       # 두 번째 저장이 실패했을 때의 예외 (반쪽 거래 감지용)

        async with self.bot.economy_lock:
            treasury = self._load_camp_treasury()
            before = treasury.get(캠프.value, 0)

            if before < 금액:
                return await interaction.followup.send(
                    f"❌ {캠프.name} 통장 잔액이 부족합니다! (현재 잔액: {before:,} 에바 / 요청: {금액:,} 에바)"
                )

            after = before - 금액
            treasury[캠프.value] = after
            self._save_camp_treasury(treasury)

            # 💾 인출과 같은 이유로, 통장 차감이 저장된 뒤 지갑 입금이 실패할 수 있어요.
            economy = economy_cog._load_raw_economy()
            user_id = str(유저.id)
            user_after = economy.get(user_id, 0) + 금액
            economy[user_id] = user_after
            try:
                economy_cog._save_raw_economy(economy)
            except Exception as save_err:
                broken = save_err
            else:
                broken = None

        if broken:
            return await report_broken_transaction(
                interaction, action="캠프통장 송금", user_id=user_id,
                lost=f"{캠프.name} 통장에서 에바 {금액:,}", not_received=f"에바 {금액:,}",
                error=broken,
            )

        # 🧾 원장에도 남깁니다. (락을 놓은 뒤에)
        record_ledger(user_id, 금액, user_after, "캠프통장 송금", f"{캠프.name} → 지급",
                      actor_id=interaction.user.id)

        embed = discord.Embed(title="🏦 [캠프통장 송금]", color=0x5CE6B4, timestamp=dt.datetime.now(KST))
        embed.add_field(name="대상 캠프", value=f"{캠프.name}", inline=True)
        embed.add_field(name="송금 금액", value=f"{금액:,} 에바", inline=True)
        embed.add_field(name="수령자", value=유저.mention, inline=True)
        embed.add_field(name=f"🏦 {캠프.name} 통장", value=f"{before:,} → **{after:,} 에바**", inline=False)
        embed.add_field(name="수령자 지갑 잔액", value=f"{user_after:,} 에바", inline=False)
        embed.set_footer(text=f"집행자: {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

        await economy_cog._log_economy_action(
            title="🏦 [캠프통장 송금]",
            description=f"**집행자:** {interaction.user.mention}\n"
                        f"**대상 캠프:** {캠프.name}\n"
                        f"**수령자:** {유저.mention}\n"
                        f"**송금 금액:** {금액:,} 에바\n"
                        f"**{캠프.name} 통장:** {before:,} → {after:,} 에바",
            color=0x5CE6B4
        )

    @camp_treasury_group.command(name="회수", description="[관리자] 특정 유저의 지갑에서 캠프 통장으로 에바를 회수해요. (진짜 지갑처럼 누구한테서나 회수 가능)")
    @app_commands.describe(캠프="회수해서 넣을 캠프 통장", 유저="에바를 회수할 유저", 금액="회수할 에바 금액")
    @app_commands.choices(캠프=[
        app_commands.Choice(name="악동캠프", value="악동"),
        app_commands.Choice(name="나래캠프", value="나래"),
        app_commands.Choice(name="여백캠프", value="여백"),
    ])
    async def camp_treasury_collect(self, interaction: discord.Interaction, 캠프: app_commands.Choice[str], 유저: discord.Member, 금액: int):
        economy_cog = self._get_economy_cog()
        if not economy_cog:
            return await interaction.response.send_message("❌ 경제 시스템 코그를 찾을 수 없어요.", ephemeral=True)

        err = self._check_camp_leader_scope(interaction, 캠프.value)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)

        if 금액 <= 0:
            return await interaction.response.send_message("❌ 회수 금액은 1 에바 이상이어야 합니다.", ephemeral=True)

        await interaction.response.defer()

        broken = None       # 두 번째 저장이 실패했을 때의 예외 (반쪽 거래 감지용)

        async with self.bot.economy_lock:
            economy = economy_cog._load_raw_economy()
            user_id = str(유저.id)
            user_before = economy.get(user_id, 0)

            if user_before < 금액:
                return await interaction.followup.send(
                    f"❌ {유저.mention}님의 잔액이 부족해요! (현재 잔액: {user_before:,} 에바 / 요청: {금액:,} 에바)"
                )

            user_after = user_before - 금액
            economy[user_id] = user_after
            economy_cog._save_raw_economy(economy)

            # 💾 여기는 방향이 반대예요. 지갑에서 이미 빠진 뒤라, 통장 입금이 실패하면
            # "유저 지갑에서만 빠지고 통장에는 안 들어온" 반쪽 상태가 됩니다.
            treasury = self._load_camp_treasury()
            before = treasury.get(캠프.value, 0)
            after = before + 금액
            treasury[캠프.value] = after
            try:
                self._save_camp_treasury(treasury)
            except Exception as save_err:
                broken = save_err
            else:
                broken = None

        if broken:
            return await report_broken_transaction(
                interaction, action="캠프통장 회수", user_id=user_id,
                lost=f"에바 {금액:,}", not_received=f"{캠프.name} 통장 입금 {금액:,}",
                error=broken, ledger_delta=-금액, ledger_balance=user_after,
            )

        # 🧾 원장에도 남깁니다. (락을 놓은 뒤에)
        record_ledger(user_id, -금액, user_after, "캠프통장 회수", f"{캠프.name}",
                      actor_id=interaction.user.id)

        embed = discord.Embed(title="🏦 [캠프통장 회수]", color=0xe67e22, timestamp=dt.datetime.now(KST))
        embed.add_field(name="대상 캠프", value=f"{캠프.name}", inline=True)
        embed.add_field(name="회수 금액", value=f"{금액:,} 에바", inline=True)
        embed.add_field(name="회수 대상자", value=유저.mention, inline=True)
        embed.add_field(name="대상자 지갑 잔액", value=f"{user_before:,} → **{user_after:,} 에바**", inline=False)
        embed.add_field(name=f"🏦 {캠프.name} 통장", value=f"{before:,} → **{after:,} 에바**", inline=False)
        embed.set_footer(text=f"집행자: {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

        await economy_cog._log_economy_action(
            title="🏦 [캠프통장 회수]",
            description=f"**집행자:** {interaction.user.mention}\n"
                        f"**대상 캠프:** {캠프.name}\n"
                        f"**회수 대상자:** {유저.mention}\n"
                        f"**회수 금액:** {금액:,} 에바\n"
                        f"**{캠프.name} 통장:** {before:,} → {after:,} 에바",
            color=0xe67e22
        )
