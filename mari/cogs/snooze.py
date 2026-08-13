"""MariSnooze — 나중에 답장할 메시지를 미뤄뒀다가, 정한 시각에 DM으로 다시 받아보는 기능.

⏰ [이 기능이 뭔가요]
지금은 답장 못 하는데 나중에 꼭 해야 하는 메시지가 있죠. 우클릭 한 번으로 미뤄두면
정한 시각에 마리가 원본 링크와 함께 DM으로 다시 알려줍니다.

🔑 [원리 — 타이머를 걸지 않아요]
"3시간 뒤"라고 해서 `asyncio.sleep(10800)`을 걸면 안 됩니다. 봇이 재시작되는 순간
대기 중이던 예약이 전부 증발해요. 배포 한 번에 다 날아갑니다.

그래서 **깨울 시각을 파일에 적어두고, 1분마다 훑으면서 시간이 지난 것만** 처리해요.
봇이 꺼져 있는 동안 시각이 지났어도 다시 켜지면 다음 순회에서 바로 잡힙니다.
(늦게 갈지언정 사라지지는 않아요)

📎 [메시지가 아니라 링크를 저장해요]
본문을 복사해두지 않고 jump_url만 저장합니다. 원본이 수정되면 최신 내용을 보게 되고
용량도 거의 안 들어요. 대신 원본이 지워지면 링크가 죽기 때문에, 앞부분만 짧게
preview로 같이 저장해서 "무슨 메시지였는지"는 남게 해뒀습니다.
"""

import datetime as dt
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from mari_config import KST, SNOOZE_FILE
from mari_alerts import report_loop_error
from mari_settings import feature_gate, is_feature_enabled
from mari_storage import atomic_json_save_or_raise, safe_json_load
from mari_utils import MariView

# 🕘 "내일 아침" 같은 말이 실제로 몇 시인지. 하루 중 그 시각이 이미 지났으면 다음 날로 넘어가요.
PRESET_HOURS = {
    "morning": (9, "아침 9시"),
    "noon": (12, "점심 12시"),
    "evening": (18, "저녁 6시"),
    "night": (22, "밤 10시"),
}

MAX_DAYS = 30           # 이보다 먼 미래는 안 받아요. 파일에 영영 남는 예약이 쌓이거든요.
PREVIEW_LIMIT = 200     # 원본이 지워졌을 때를 대비해 남겨두는 본문 길이
MAX_PER_USER = 50       # 한 사람이 쌓아둘 수 있는 예약 수 (실수로 무한정 쌓이는 것 방지)

# 📏 임베드 전체(제목 + 모든 필드) 글자 수 상한은 6000자예요. 여유를 두고 이 선에서 끊습니다.
# 🐛 [버그 수정] 예전엔 개수(25개)로만 잘랐어요. 그런데 필드 하나가 preview 200자 + memo 200자
# + 원본 링크까지 해서 최대 541자라, 12개쯤부터 6000자를 넘겨 목록 전송이 400으로 실패했습니다.
# (25개면 13,574자) 하필 **예약이 많이 쌓여서 정리하려고 여는 화면**이 안 열리는 셈이었어요.
EMBED_TOTAL_LIMIT = 5500
MAX_LIST_FIELDS = 25    # 디스코드 임베드 필드 개수 한도

# 🔁 DM 전송에 이만큼 실패하면 포기하고 목록에서 지웁니다.
# DM을 아예 막아두신 분에게는 영영 성공하지 않아서, 안 그러면 1분마다 무한히 재시도해요.
MAX_DELIVER_TRIES = 3

# "30분", "2시간 뒤", "3일후" 같은 상대 시각
_REL_PATTERN = re.compile(r"^\s*(\d+)\s*(분|시간|일)\s*(뒤|후)?\s*$")
_REL_UNITS = {"분": "minutes", "시간": "hours", "일": "days"}


class SnoozeTimeSelect(discord.ui.Select):
    """언제 다시 알려줄지 고르는 드롭다운.

    ⚠️ 모달(창)에는 드롭다운을 넣을 수 없어요. 디스코드 모달은 텍스트 입력만 지원합니다.
    그래서 우클릭 → (나만 보이는) 메시지 + 드롭다운 → 필요하면 모달, 순서로 갑니다.
    """

    def __init__(self):
        super().__init__(
            placeholder="⏰ 언제 다시 알려드릴까요?",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(label="1시간 뒤", value="1h", emoji="⏱️"),
                discord.SelectOption(label="3시간 뒤", value="3h", emoji="⏱️"),
                discord.SelectOption(label="아침 9시", value="morning", emoji="🌅",
                                     description="오늘 9시가 지났으면 내일 아침이에요"),
                discord.SelectOption(label="점심 12시", value="noon", emoji="🍚",
                                     description="오늘 12시가 지났으면 내일 점심이에요"),
                discord.SelectOption(label="저녁 6시", value="evening", emoji="🌆",
                                     description="오늘 6시가 지났으면 내일 저녁이에요"),
                discord.SelectOption(label="밤 10시", value="night", emoji="🌙",
                                     description="오늘 10시가 지났으면 내일 밤이에요"),
                discord.SelectOption(label="직접 입력", value="custom", emoji="✏️",
                                     description="30분 · 2시간 · 3일 · 8-10 14:00 …"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        view: "SnoozeView" = self.view
        choice = self.values[0]

        # 🐛 [버그 수정] '직접 입력'을 고르면 모달이 뜨는데, 그동안 **이 드롭다운은 그대로
        #    살아 있습니다.** 모달을 제출해 예약을 잡은 뒤에 드롭다운에서 다른 시간을 또 고르면
        #    같은 메시지로 예약이 두 개 잡혔어요. 한 번 예약되면 이 창은 닫힌 것으로 봅니다.
        if view.done:
            return await interaction.response.send_message(
                "⏰ 이 메시지는 이미 미뤄뒀어요. `/스누즈 목록`에서 확인할 수 있어요.", ephemeral=True
            )

        if choice == "custom":
            # 모달은 '아직 응답하지 않은' 인터랙션에서만 열 수 있어서 여기서 바로 띄웁니다.
            return await interaction.response.send_modal(SnoozeCustomModal(view))

        now = dt.datetime.now(KST)
        if choice.endswith("h"):
            wake_at = now + dt.timedelta(hours=int(choice[:-1]))
        else:
            wake_at = view.cog.next_time_at(PRESET_HOURS[choice][0], now)

        await view.cog.create_reminder(interaction, view.message, wake_at, view=view)


class SnoozeView(MariView):
    """우클릭 직후 나만 보이는 시간 선택 창."""

    def __init__(self, cog, message: discord.Message):
        super().__init__(timeout=120)
        self.cog = cog
        self.message = message
        self.done = False               # 예약이 한 번 잡히면 True. 두 번 잡히는 걸 막아요.
        self.origin_interaction = None  # 예약 뒤 드롭다운을 치우려면 이 핸들이 필요해요
        self.add_item(SnoozeTimeSelect())


class SnoozeCustomModal(discord.ui.Modal, title="⏰ 언제 알려드릴까요?"):
    """직접 입력 창. 상대 시각과 절대 시각을 모두 받아요."""

    언제 = discord.ui.TextInput(
        label="시간",
        placeholder="30분 · 2시간 · 3일 · 14:00 · 8-10 14:00",
        max_length=40,
    )
    메모 = discord.ui.TextInput(
        label="메모 (선택)",
        placeholder="나중에 봤을 때 왜 미뤘는지 알 수 있게",
        required=False,
        max_length=200,
    )

    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view      # 예약이 잡히면 이 창의 드롭다운을 잠가야 해요
        self.cog = parent_view.cog
        self.message = parent_view.message

    async def on_submit(self, interaction: discord.Interaction):
        if self.parent_view.done:
            return await interaction.response.send_message(
                "⏰ 이 메시지는 이미 미뤄뒀어요. `/스누즈 목록`에서 확인할 수 있어요.", ephemeral=True
            )
        wake_at, err = self.cog.parse_when(str(self.언제), dt.datetime.now(KST))
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        await self.cog.create_reminder(
            interaction, self.message, wake_at, str(self.메모), view=self.parent_view
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        """모달은 MariView와 상속 관계가 아니라서 오류 처리를 따로 붙여둬요.

        🐛 [버그 수정] 예전엔 무조건 send_message를 불렀어요. 예약은 defer로 시작하기 때문에
        저장 중에 난 오류는 **항상** 이미 응답한 뒤라, send_message가 또 실패하면서 유저
        화면엔 아무것도 안 떴습니다. (MariView.on_error와 같은 방식으로 갈라요)
        """
        print(f"❗ [스누즈] 예약 처리 중 오류: {type(error).__name__}: {error}")
        msg = f"❌ 예약하지 못했어요. (`{type(error).__name__}`)"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception as e:
            print(f"❗ [스누즈] 오류 안내 전송 실패: {e}")


class MariSnooze(commands.Cog):
    """⏰ 나중에 답장 — 미뤄둔 메시지를 정한 시각에 DM으로 다시 받아보기."""

    스누즈 = app_commands.Group(name="스누즈", description="나중에 답장하려고 미뤄둔 메시지를 관리합니다.")

    def __init__(self, bot):
        self.bot = bot
        # ⚠️ 우클릭(컨텍스트) 메뉴는 코그 안에서 데코레이터로 만들 수 없어요. 객체를 직접 만들어
        #    트리에 넣고, cog_unload에서 반드시 다시 빼야 합니다. (안 빼면 중복 등록돼요)
        self.remind_menu = app_commands.ContextMenu(name="나중에 답장", callback=self.remind_later)
        self.bot.tree.add_command(self.remind_menu)
        # 📮 DM은 이미 보냈는데 파일에서 지우지 못한 예약 번호들. (저장이 실패했을 때 채워져요)
        # 다음 회차에 같은 알림을 또 보내지 않도록 여기서 걸러냅니다. 저장이 한 번 성공하면 비워져요.
        self._sent_but_unsaved = set()

    async def cog_load(self):
        self.snooze_loop.start()

    async def cog_unload(self):
        self.snooze_loop.cancel()
        self.bot.tree.remove_command(self.remind_menu.name, type=self.remind_menu.type)

    # ========== 💾 저장소 ==========
    def _load(self) -> dict:
        data = safe_json_load(SNOOZE_FILE, None)
        if not isinstance(data, dict):
            return {"next_id": 1, "reminders": []}
        data.setdefault("next_id", 1)
        data.setdefault("reminders", [])
        return data

    def _save(self, data: dict):
        atomic_json_save_or_raise(SNOOZE_FILE, data, indent=2)

    # ========== 🗓️ 시각 계산 ==========
    @staticmethod
    def next_time_at(hour: int, now: dt.datetime) -> dt.datetime:
        """오늘 그 시각. 이미 지났으면 내일 그 시각.

        '저녁 6시'를 밤 8시에 고르면 이미 지난 시각이라 그대로 두면 즉시 알림이 가버려요.
        그래서 항상 **다음에 오는** 그 시각으로 넘깁니다.
        """
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        return target

    @staticmethod
    def _parse_absolute(cleaned: str, now: dt.datetime) -> Optional[dt.datetime]:
        """`2026-08-10 14:00` · `8-10 14:00` · `14:00` 을 시각으로 바꿉니다. 못 알아보면 None.

        🐛 [버그 수정] 연도를 생략한 `2-29 14:00`이 **항상** 거부됐어요.
        strptime은 입력에 없는 값을 기본값으로 채우는데, 연도의 기본값이 **1900년**입니다.
        1900년은 윤년이 아니라서(4로 나눠떨어져도 100으로 나눠떨어지면 윤년이 아니에요)
        2월 29일이 존재하지 않고, 그래서 `parsed.replace(year=올해)`로 고쳐볼 기회조차 없이
        파싱 단계에서 죽었습니다.
        이제 연도를 생략하면 **올해를 앞에 붙여서** 파싱해요. 1900년을 거치지 않습니다.
        """
        # 1) 연도까지 적어준 경우
        try:
            return dt.datetime.strptime(cleaned, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        except ValueError:
            pass

        # 2) 연도를 생략한 경우 — 올해로 붙여보고, 이미 지났으면 내년으로.
        #    (평년에 `2-29`를 넣으면 올해 파싱이 실패하니 내년까지 시도해요)
        for year in (now.year, now.year + 1):
            try:
                parsed = dt.datetime.strptime(f"{year}-{cleaned}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            candidate = parsed.replace(tzinfo=KST)
            if candidate > now:
                return candidate

        # 3) 시각만 준 경우 — 오늘 그 시각, 이미 지났으면 내일 그 시각.
        try:
            only_time = dt.datetime.strptime(cleaned, "%H:%M")
        except ValueError:
            return None
        wake_at = now.replace(hour=only_time.hour, minute=only_time.minute,
                              second=0, microsecond=0)
        return wake_at if wake_at > now else wake_at + dt.timedelta(days=1)

    def parse_when(self, text: str, now: dt.datetime) -> tuple:
        """직접 입력을 시각으로 바꿉니다. (시각, 오류메세지) 중 한쪽만 채워서 돌려줘요.

        받는 형식:
          • 상대 — `30분` `2시간` `3일` (뒤/후 붙여도 돼요)
          • 절대 — `14:00` `8-10 14:00` `2026-08-10 14:00` (`/`나 `.`로 구분해도 돼요)
        """
        raw = (text or "").strip()
        if not raw:
            return None, "❌ 시간을 입력해 주세요. (예: `30분`, `2시간`, `내일 14:00`은 `8-10 14:00` 형식으로)"

        wake_at = None

        rel = _REL_PATTERN.match(raw)
        if rel:
            amount = int(rel.group(1))
            if amount <= 0:
                return None, "❌ 시간은 1 이상으로 넣어주세요."
            wake_at = now + dt.timedelta(**{_REL_UNITS[rel.group(2)]: amount})
        else:
            wake_at = self._parse_absolute(raw.replace("/", "-").replace(".", "-"), now)

        if wake_at is None:
            return None, ("❌ 시간을 알아볼 수 없어요.\n"
                          "└ 이렇게 넣어주세요: `30분` · `2시간` · `3일` · `14:00` · `8-10 14:00`")

        if wake_at <= now:
            return None, "❌ 이미 지난 시각이에요. 앞으로의 시각을 넣어주세요."
        if wake_at > now + dt.timedelta(days=MAX_DAYS):
            return None, f"❌ 너무 먼 미래예요. 최대 {MAX_DAYS}일 뒤까지만 미뤄둘 수 있어요."
        return wake_at, None

    # ========== 🖱️ 메시지 우클릭 → 나중에 답장 ==========
    async def remind_later(self, interaction: discord.Interaction, message: discord.Message):
        """메시지를 우클릭해서 나중에 다시 받아보도록 미뤄둡니다."""
        # 🚧 `/기능제어 정지 기능:나중에답장`으로 꺼두면 새 예약을 받지 않아요.
        if await feature_gate(interaction, "snooze", "나중에 답장"):
            return
        data = self._load()
        mine = [r for r in data["reminders"] if r.get("user_id") == str(interaction.user.id)]
        if len(mine) >= MAX_PER_USER:
            return await interaction.response.send_message(
                f"❌ 미뤄둔 메시지가 이미 {MAX_PER_USER}개예요. `/스누즈 목록`에서 정리해 주세요.",
                ephemeral=True,
            )

        preview = (message.content or "").strip()
        if not preview and message.attachments:
            preview = "(사진·파일 메시지)"

        embed = discord.Embed(
            title="⏰ 나중에 답장",
            description=(f"> {preview[:PREVIEW_LIMIT]}" if preview else "*(내용 없는 메시지)*"),
            color=0x5CE6B4,
        )
        embed.set_footer(text="시간을 고르면 그때 DM으로 알려드릴게요")
        view = SnoozeView(self, message)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        # '직접 입력'으로 예약하면 모달 쪽 인터랙션으로 답하게 돼서, 이 드롭다운 창은 따로
        # 치워줘야 해요. 그러려면 이 인터랙션 핸들을 들고 있어야 합니다.
        view.origin_interaction = interaction

    async def create_reminder(self, interaction: discord.Interaction, message: discord.Message,
                              wake_at: dt.datetime, memo: str = "", view=None) -> None:
        """예약을 실제로 저장합니다. (드롭다운·직접입력 양쪽에서 여기로 모여요)"""
        # ⏱️ 아래에 DM 전송이 있어서 먼저 defer로 3초 시한을 벗어나 둡니다.
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        preview = (message.content or "").strip()
        if not preview and message.attachments:
            preview = "(사진·파일 메시지)"

        # 📮 DM이 되는지 **저장하기 전에** 확인해요. 시간 다 돼서 못 보내면 예약이 조용히
        #    사라지는데, 그러면 유저는 알림을 기다리다 놓칩니다. 확인 DM이 그 검사를 겸해요.
        # 🐛 [버그 수정] 예전엔 여기에 `format_dt(wake_at, 'R')`로 "3시간 후" 같은 **상대 시각**을
        # 넣었어요. 그런데 디스코드의 상대 시각(`<t:…:R>`)은 메세지에 박제되는 글자가 아니라
        # **화면에서 계속 다시 계산되는 표시**입니다. 이 확인 DM은 지운 적이 없으니 DM 목록에
        # 영영 남는데, 약속한 시각이 지나는 순간 "3시간 후"가 "1분 전 → 2분 전 …"으로 뒤집혀서
        # 계속 흘러가요. 알림이 정상으로 갔는데도 **타이머가 멈추지 않은 것처럼** 보였습니다.
        # (디스코드가 그려주는 값이라 봇이 "멈출" 방법이 없어요 — 애초에 안 쓰는 게 맞습니다)
        # 이제 확인 DM에는 고정된 절대 시각만 적고, 실제 알림은 그때 **새 메세지**로 갑니다.
        confirm = discord.Embed(
            title="⏰ 미뤄뒀어요",
            description=(f"**{wake_at.strftime('%m월 %d일 %H:%M')}**에 다시 알려드릴게요.\n"
                         f"└ 그때 이 대화로 알림이 **새 메세지**로 하나 더 갑니다."),
            color=0x5CE6B4,
        )
        if preview:
            confirm.add_field(name="미뤄둔 메시지", value=f"> {preview[:PREVIEW_LIMIT]}", inline=False)
        if memo.strip():
            confirm.add_field(name="메모", value=memo.strip()[:1024], inline=False)

        try:
            await interaction.user.send(embed=confirm)
        except Exception as e:
            print(f"❗ [스누즈] 확인 DM 전송 실패 (유저 {interaction.user.id}): {type(e).__name__}: {e}")
            return await interaction.edit_original_response(
                content=("❌ DM을 보낼 수 없어서 예약하지 못했어요.\n"
                         "└ 서버 설정에서 **개인 메시지 허용**을 켜고 다시 시도해 주세요."),
                embed=None, view=None,
            )

        # 💾 DM이 되는 걸 확인한 뒤에 저장합니다. (읽고→고치고→저장 사이에 await 없음)
        try:
            data = self._load()
            data["reminders"].append({
                "id": str(data["next_id"]),
                "user_id": str(interaction.user.id),
                "wake_at": wake_at.isoformat(),
                "message_link": message.jump_url,
                "preview": preview[:PREVIEW_LIMIT],
                "memo": memo.strip()[:200],
                "created_at": dt.datetime.now(KST).isoformat(),
            })
            data["next_id"] += 1
            self._save(data)
        except Exception as save_err:
            # 🧨 확인 DM은 이미 나갔는데 저장이 실패한 '반쪽 상태'예요. 그냥 두면 유저는
            #    "미뤄뒀어요" DM만 믿고 오지 않을 알림을 기다리게 됩니다. 반드시 바로잡아야 해요.
            print(f"❗ [스누즈] 예약 저장 실패 (유저 {interaction.user.id}): "
                  f"{type(save_err).__name__}: {save_err}")
            try:
                await interaction.user.send(
                    "🧨 방금 보내드린 **'미뤄뒀어요' 안내는 취소해 주세요.**\n"
                    "저장에 실패해서 그 시각에 알림이 가지 않아요. 번거로우시겠지만 다시 미뤄주세요."
                )
            except Exception:
                pass
            return await interaction.edit_original_response(
                content=(f"❌ 예약을 저장하지 못했어요. (`{type(save_err).__name__}`)\n"
                         "└ 알림이 가지 않으니 잠시 뒤 다시 시도해 주세요."),
                embed=None, view=None,
            )

        # 🔒 이 창으로는 더 예약할 수 없게 잠급니다. (같은 메시지가 두 번 잡히는 걸 막아요)
        if view is not None:
            view.done = True
            view.stop()

        # 확인 DM과 같은 이유로 상대 시각을 쓰지 않아요. 이 안내도 화면에 계속 남거든요.
        done_text = f"✅ **{wake_at.strftime('%m월 %d일 %H:%M')}**에 DM으로 알려드릴게요!"
        await interaction.edit_original_response(content=done_text, embed=None, view=None)

        # '직접 입력'으로 온 경우엔 위에서 답한 게 모달 쪽이라, 드롭다운이 달린 원래 창이
        # 그대로 남아 있어요. 그것도 같이 치웁니다. (실패해도 done 플래그가 막아주니 괜찮아요)
        # 드롭다운으로 곧장 고른 경우엔 위 edit_original_response가 이미 그 창을 고쳤으니
        # 여기서 또 건드리지 않습니다.
        origin = getattr(view, "origin_interaction", None)
        if origin is not None and interaction.type is discord.InteractionType.modal_submit:
            try:
                await origin.edit_original_response(content=done_text, embed=None, view=None)
            except Exception:
                pass

    # ========== ⏰ 깨우기 ==========
    @staticmethod
    def _wake_at(row: dict) -> Optional[dt.datetime]:
        """예약 한 줄의 '깨울 시각'. 값이 깨져 있으면 None을 돌려줍니다."""
        try:
            parsed = dt.datetime.fromisoformat(row.get("wake_at", ""))
        except (TypeError, ValueError):
            return None
        return parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed

    @tasks.loop(minutes=1)
    async def snooze_loop(self):
        """1분마다 '시간이 지난 예약'을 찾아 DM을 보냅니다.

        타이머(sleep)를 쓰지 않는 이유는 이 파일 맨 위 주석을 봐주세요.
        봇이 꺼져 있던 동안 시각이 지난 예약도 켜지자마자 여기서 잡힙니다.
        """
        # 🚧 기능이 정지된 동안에는 DM을 보내지 않아요. 예약이 사라지는 게 아니라 **미뤄질 뿐**이라,
        #    재개하면 그동안 시각이 지난 것부터 다음 회차에 순서대로 나갑니다.
        #    (봇이 꺼져 있던 경우와 똑같이 동작해요 — 늦게 갈지언정 사라지지는 않아요)
        if not is_feature_enabled("snooze"):
            return

        data = self._load()
        if not data["reminders"]:
            return

        now = dt.datetime.now(KST)
        due, broken, already_sent = [], [], []
        for row in data["reminders"]:
            rid = row.get("id")
            wake_at = self._wake_at(row)
            if wake_at is None:
                broken.append(rid)              # 시각이 깨진 줄은 여기서 정리해요
            elif wake_at <= now:
                if rid in self._sent_but_unsaved:
                    already_sent.append(rid)    # 지난 회차에 보냈는데 못 지운 것 — 지우기만 하면 돼요
                else:
                    due.append(row)

        if not due and not broken and not already_sent:
            return

        # 📬 보낸 뒤에 지웁니다. tasks.loop은 이전 회차가 끝나기 전에 다시 돌지 않아서
        #    중복 발송 걱정이 없고, 먼저 지웠다가 전송이 실패하면 알림이 영영 사라져요.
        # 🐛 [버그 수정] 예전엔 결과와 상관없이 due를 전부 지웠어요. _deliver가 예외를 삼키기
        #    때문에, 예약해둔 뒤 DM을 막았거나 디스코드가 잠깐 5xx를 내면 알림이 그대로
        #    증발했습니다. "보낸 뒤에 지운다"로 순서를 잡아둔 취지가 무색했어요.
        #    이제 **성공한 것만** 지우고, 실패한 건 다음 회차에 다시 시도합니다.
        delivered, retry = [], {}
        for row in due:
            rid = row.get("id")
            if await self._deliver(row):
                delivered.append(rid)
                continue
            # 🔁 무한 재시도는 안 돼요. DM을 아예 막아둔 분이면 영영 성공하지 않으니,
            #    몇 번 해보고 포기합니다. (안 그러면 1분마다 실패 로그가 계속 쌓여요)
            tries = int(row.get("tries", 0) or 0) + 1
            if tries >= MAX_DELIVER_TRIES:
                print(f"❗ [스누즈] 예약 #{rid}을 {MAX_DELIVER_TRIES}번 보내지 못해 포기해요. "
                      f"(유저 {row.get('user_id')} — DM을 막아두셨을 가능성이 커요)")
                delivered.append(rid)       # 더 보낼 방법이 없으니 목록에서는 정리합니다
            else:
                retry[rid] = tries

        # 🚨 [중요] 위에서 읽은 `data`를 그대로 저장하면 안 돼요. 바로 위 _deliver는 DM을
        #    보내느라 await을 하는데, 그 사이에 누군가 새로 미뤄둔 예약이 파일에 들어옵니다.
        #    낡은 사본을 덮어쓰면 그 예약이 통째로 사라져요. (shop.json에서 똑같이 겪은 문제)
        #    그래서 저장 직전에 다시 읽어서 **이번에 처리한 것만** 걷어냅니다.
        remove = set(delivered) | set(broken) | set(already_sent)
        try:
            fresh = self._load()
            kept = []
            for r in fresh["reminders"]:
                rid = r.get("id")
                if rid in remove:
                    continue
                if rid in retry:
                    r["tries"] = retry[rid]     # 실패 횟수를 남겨서 다음 회차가 이어서 셉니다
                kept.append(r)
            fresh["reminders"] = kept
            self._save(fresh)
            self._sent_but_unsaved -= remove
        except Exception as e:
            # 🐛 [버그 수정] 저장 실패가 루프 밖으로 나가면 error 핸들러가 루프를 되살리는데,
            #    파일에는 방금 보낸 예약이 그대로 남아 있어서 **1분 뒤 같은 DM이 또 갑니다.**
            #    파일이 잠겨 있는 내내 반복돼요. (백신·클라우드 동기화에서 실제로 겪은 상황)
            #    이제 여기서 붙잡고, 보낸 건은 메모리에 적어둬서 다음 회차가 건너뛰게 합니다.
            print(f"❗ [스누즈] 처리 결과를 저장하지 못했어요. 보낸 알림은 다시 보내지 않아요: "
                  f"{type(e).__name__}: {e}")
            self._sent_but_unsaved |= set(delivered) | set(already_sent)

    @snooze_loop.before_loop
    async def before_snooze_loop(self):
        await self.bot.wait_until_ready()

    @snooze_loop.error
    async def snooze_loop_error(self, error: BaseException):
        await report_loop_error(self.snooze_loop, "나중에 답장 알림", error)

    async def _deliver(self, row: dict) -> bool:
        """예약 한 건을 DM으로 보냅니다. 보냈으면 True, 못 보냈으면 False.

        실패해도 예외를 밖으로 내보내지 않아요. 한 사람에게 못 보낸 것 때문에 나머지 사람들의
        알림까지 멈추면 안 되니까요. 대신 **성공 여부는 반드시 돌려줍니다** — 호출부가 이걸 보고
        지울지 다시 시도할지 정해요. (예전엔 아무것도 돌려주지 않아서 실패한 것도 지워졌어요)
        """
        try:
            user = self.bot.get_user(int(row["user_id"])) or await self.bot.fetch_user(int(row["user_id"]))
            embed = discord.Embed(
                title="⏰ 아까 미뤄둔 메시지예요!",
                description=(f"> {row['preview']}" if row.get("preview") else "*(내용 없는 메시지)*"),
                color=0x5CE6B4,
            )
            if row.get("memo"):
                embed.add_field(name="📝 메모", value=row["memo"][:1024], inline=False)
            if row.get("message_link"):
                # 원본이 지워졌으면 이 링크는 열리지 않아요. 그래서 위에 preview를 같이 둡니다.
                embed.add_field(name="원본", value=f"[메시지로 이동]({row['message_link']})", inline=False)
            await user.send(embed=embed)
            return True
        except Exception as e:
            print(f"❗ [스누즈] 알림 전송 실패 (유저 {row.get('user_id')}): {type(e).__name__}: {e}")
            return False

    # ========== 📋 목록 / 취소 ==========
    @스누즈.command(name="목록", description="내가 미뤄둔 메시지를 모아 봅니다.")
    async def list_reminders(self, interaction: discord.Interaction):
        mine = [r for r in self._load()["reminders"] if r.get("user_id") == str(interaction.user.id)]
        if not mine:
            return await interaction.response.send_message(
                "📭 미뤄둔 메시지가 없어요.\n└ 메시지를 **우클릭 → 나중에 답장**으로 미뤄둘 수 있어요.",
                ephemeral=True,
            )

        mine.sort(key=lambda r: r.get("wake_at", ""))
        embed = discord.Embed(title="⏰ 미뤄둔 메시지", color=0x5CE6B4)

        # 개수뿐 아니라 **글자 수 총합**도 같이 셉니다. (위 EMBED_TOTAL_LIMIT 주석 참고)
        used = len(embed.title)
        shown = 0
        now = dt.datetime.now(KST)
        for row in mine[:MAX_LIST_FIELDS]:
            wake_at = self._wake_at(row)
            if wake_at is None:
                when = "(시각 정보 없음)"
            elif wake_at <= now:
                # ⏳ 시각은 지났는데 아직 목록에 있다는 건 발송을 기다리는 중이라는 뜻이에요.
                #    (루프가 1분마다 도니까 곧 나가요) 여기서 상대 시각을 쓰면 "3분 전"처럼
                #    거꾸로 흘러서 마치 고장 난 타이머처럼 보입니다.
                when = f"{wake_at.strftime('%m/%d %H:%M')} · ⏳ 곧 발송돼요"
            else:
                # 아직 오지 않은 시각이라 상대 표시가 도움이 돼요. ("2시간 후")
                when = f"{wake_at.strftime('%m/%d %H:%M')} · {discord.utils.format_dt(wake_at, 'R')}"

            value = f"> {row.get('preview') or '(내용 없음)'}"
            if row.get("memo"):
                value += f"\n📝 {row['memo']}"
            if row.get("message_link"):
                value += f"\n[메시지로 이동]({row['message_link']})"
            value += f"\n`#{row['id']}`"

            name, value = f"⏱️ {when}", value[:1024]
            if used + len(name) + len(value) > EMBED_TOTAL_LIMIT:
                break       # 총량에 먼저 걸린 경우 (메모·미리보기가 긴 예약이 몰려 있을 때)
            embed.add_field(name=name, value=value, inline=False)
            used += len(name) + len(value)
            shown += 1

        if shown < len(mine):
            embed.set_footer(
                text=f"가장 이른 {shown}개만 보여요 (총 {len(mine)}개) · "
                     f"/스누즈 취소 로 정리하면 나머지가 보여요"
            )
        else:
            embed.set_footer(text="취소하려면 /스누즈 취소 에 번호를 넣어주세요")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @스누즈.command(name="취소", description="미뤄둔 메시지 알림을 취소합니다. (번호는 /스누즈 목록의 #숫자)")
    @app_commands.describe(번호="취소할 예약 번호 (예: 7 또는 #7)")
    async def cancel(self, interaction: discord.Interaction, 번호: str):
        target = 번호.strip().lstrip("#")
        data = self._load()

        # 🔒 남의 예약은 건드릴 수 없어요. 번호만 알면 지워지면 안 되니까 소유자까지 확인합니다.
        hit = next((r for r in data["reminders"]
                    if r.get("id") == target and r.get("user_id") == str(interaction.user.id)), None)
        if hit is None:
            return await interaction.response.send_message(
                f"❌ `#{target}` 번 예약을 찾을 수 없어요. `/스누즈 목록`에서 번호를 확인해 주세요.",
                ephemeral=True,
            )

        data["reminders"] = [r for r in data["reminders"] if r is not hit]
        self._save(data)
        await interaction.response.send_message(f"🗑️ `#{target}` 번 예약을 취소했어요.", ephemeral=True)
