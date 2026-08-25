"""ChunsikWizard — 납품 세팅을 봇이 대신 해줍니다. (`/설치`)

담긴 기능이 요구하는 채널과 관리자 역할을 **봇이 직접 만들고 곧바로 지정**해요.
예전엔 관리자가 채널을 손으로 만들고 `/설정 채널 …`을 열몇 번 쳐야 했는데, 그게 한
서버에 20~30분씩 드는 자리였습니다.

무엇이 필요한지는 **modules.py의 소유 표**가 이미 알고 있어요. 담지 않은 기능의
채널은 애초에 목록에 안 나옵니다. 이름표(한글 이름)는 `/설정` 명령이 쓰는 표를
그대로 빌려 씁니다 — 사람이 보는 이름이 두 군데로 갈라지면 안 되니까요.

🔐 만들어지는 관리자 역할은 **권한이 하나도 없는 표식**이에요. 이 봇의 권한 체계는
   "역할에 디스코드 권한이 있느냐"가 아니라 "settings.json에 그 역할 ID가 적혀 있느냐"로
   돌아갑니다. 그래서 빈 역할이면 충분하고, 그래야 안전해요.

⚠️ 채널·역할을 만들려면 봇에게 **채널 관리**와 **역할 관리** 권한이 있어야 해요.
   없으면 무엇이 없어서 실패했는지 그대로 알려줍니다. (README 3번 초대 URL 참고)
"""

import discord
from discord import app_commands
from discord.ext import commands

from chunsik_config import ENABLED_MODULE_KEYS
from chunsik_settings import load_settings, save_settings
from chunsik_utils import ChunsikView, add_lines_field
from modules import is_active


# 🗂️ 어느 채널을 어느 카테고리에 담을지.
#
# 한 카테고리에 열몇 개를 몰아넣으면 서버 목록이 그냥 벽이 돼요. 성격끼리 묶습니다 —
# 관리자만 보는 로그는 로그끼리, 주식은 주식끼리, 아이디는 아이디끼리.
#
# 여기 없는 채널 키는 맨 아래 DEFAULT_CATEGORY로 갑니다. 새 채널을 만들면 여기
# 한 줄만 추가하면 돼요. (담기지 않은 기능의 채널은 애초에 목록에 안 나옵니다)
#
# 📛 카테고리 이름에 재화·이벤트 이름을 넣지 마세요. 카테고리는 **한 번 만들면 그대로**
#    남는데, 나중에 `/초기설정`으로 이름을 바꾸면 카테고리만 옛 이름으로 남습니다.
CATEGORY_BY_CHANNEL = {
    # 📋 관리자만 보는 기록. 채널이 제일 많아지는 쪽이라 반드시 따로 묶어요.
    "role_log": "📋 로그",
    "id_log": "📋 로그",
    "economy_log": "📋 로그",
    "shop_log": "📋 로그",
    "stock_log": "📋 로그",
    "birthday_log": "📋 로그",
    "member_log": "📋 로그",

    # 🆔 아이디 등록부 — 유저가 올리는 채널과 명단이 나란히 있어야 편해요.
    "id_submit": "🆔 아이디",
    "level_roster": "🆔 아이디",

    # 📈 주식 — 전광판과 종가 게시판은 유저가 보는 곳이라 로그와 갈라둡니다.
    "stock_board": "📈 주식",
    "closing_log": "📈 주식",

    # 🛒 상점 — 매대가 걸리는 채널이라 유저가 제일 자주 옵니다.
    "shop_board": "🛒 상점",

    # 💰 돈 — 출석은 매일 사람이 드나드는 채널이에요.
    "attendance": "💰 경제",

    # 🎉 사람을 맞고 축하하는 채널들.
    "welcome": "🎉 알림",
    "birthday_announce": "🎉 알림",
    "level_announce": "🎉 알림",
    "evashi_announce": "🎉 알림",
}

DEFAULT_CATEGORY = "🧩 기타"

# 📖 채널 하나하나가 **무엇에 쓰는 곳인지.**
#
# 이 표가 이 파일에서 제일 중요합니다. 봇이 처음 서버에 들어왔을 때 관리자가 보는 건
# "채널 13개가 필요합니다"라는 목록뿐이었어요. 이름만 봐서는 `역할로그`와 `아이디로그`가
# 뭐가 다른지, 유저에게 보여야 하는 곳인지 관리자만 보는 곳인지 알 수가 없습니다.
# 모르면 일단 다 만들게 되고, 그러면 이미 있던 로그 채널 옆에 똑같은 게 하나 더 생겨요.
#
# 그래서 (한 줄 설명, 누가 보는 곳인가) 두 가지를 답니다. 두 번째가 특히 중요해요 —
# 관리자만 보는 채널을 유저에게 열어두면 남의 지갑 잔액이 그대로 보입니다.
SEEN_BY_STAFF = "🔒 관리자만"
SEEN_BY_USERS = "👥 유저도 봄"

CHANNEL_PURPOSE = {
    "attendance":        ("매일 출석 도장을 찍는 곳이에요.", SEEN_BY_USERS),
    "economy_log":       ("송금·지급·회수가 남습니다. 누가 누구에게 얼마를 줬는지요.", SEEN_BY_STAFF),
    "shop_log":          ("무엇을 사고 되팔았는지 남습니다.", SEEN_BY_STAFF),
    "shop_board":        ("매대가 걸리는 곳이에요. 여기서 물건을 고르고 삽니다.", SEEN_BY_USERS),
    "stock_board":       ("시세판이 걸립니다. 종목과 가격을 여기서 봐요.", SEEN_BY_USERS),
    "stock_log":         ("누가 무엇을 얼마에 사고팔았는지 남습니다.", SEEN_BY_STAFF),
    "closing_log":       ("하루 종가를 발표하는 곳이에요.", SEEN_BY_USERS),
    "evashi_announce":   ("선착순 이벤트를 여는 곳이에요.", SEEN_BY_USERS),
    "id_log":            ("아이디를 등록·수정한 기록이 남습니다.", SEEN_BY_STAFF),
    "level_roster":      ("서버원 명단이 **자동으로 갱신되는** 곳이에요. 봇이 계속 고쳐 쓰니 "
                          "사람이 글을 쓰지 않는 채널로 두세요.", SEEN_BY_USERS),
    "id_submit":         ("유저가 자기 게임 아이디를 올리는 곳이에요.", SEEN_BY_USERS),
    "role_log":          ("역할이 붙고 떨어진 기록이 남습니다.", SEEN_BY_STAFF),
    "birthday_announce": ("생일 축하가 올라갑니다.", SEEN_BY_USERS),
    "birthday_log":      ("생일을 등록·수정한 기록이 남습니다.", SEEN_BY_STAFF),
    "welcome":           ("새로 들어온 사람에게 인사하는 곳이에요.", SEEN_BY_USERS),
    "member_log":        ("누가 들어오고 나갔는지 남습니다.", SEEN_BY_STAFF),
    "level_announce":    ("레벨업 축하를 모아서 올리는 곳이에요.", SEEN_BY_USERS),
}

# 🎭 관리자 역할이 **무엇을 할 수 있게 되는지.**
#    이걸 안 적어두면 "아이디 관리자"를 누구에게 줘야 하는지 판단할 수가 없어요.
ROLE_PURPOSE = {
    "ids_admin":      "남의 아이디를 고치고 중복을 정리할 수 있어요.",
    "shop_admin":     "물건을 넣고 빼고 값을 정합니다.",
    "stock_admin":    "종목을 만들고 종가를 게시합니다.",
    "evashi_admin":   "이벤트를 열고 닫습니다.",
    "chronicle_admin": "서버 기록을 남기고 고칩니다.",
    "test_admin":     "진단 명령(`/테스트`)을 쓸 수 있어요.",
    "selfrole_admin": "셀프 역할 패널을 만들고 역할을 담습니다.",
    "welcome_admin":  "입장 자동 역할과 환영 인사를 정합니다.",
    "level_admin":    "경험치와 레벨 보상을 조정합니다.",
    "party_admin":    "남이 연 모집도 마감하고, 끝난 기록을 정리합니다.",
}


def channel_purpose(key: str) -> tuple:
    """(설명, 누가 보는가). 표에 없으면 빈 설명 — 안내가 비어도 설치는 굴러가야 해요."""
    return CHANNEL_PURPOSE.get(key, ("", SEEN_BY_STAFF))

# 카테고리가 서버에 생기는 순서 = 위 표에 처음 나온 순서. (기타는 언제나 맨 뒤)
_CATEGORY_ORDER = list(dict.fromkeys(CATEGORY_BY_CHANNEL.values())) + [DEFAULT_CATEGORY]


def _category_of(channel_key: str) -> str:
    return CATEGORY_BY_CHANNEL.get(channel_key, DEFAULT_CATEGORY)


def _category_order_by_name(name: str) -> int:
    return _CATEGORY_ORDER.index(name) if name in _CATEGORY_ORDER else len(_CATEGORY_ORDER)


def _category_order(channel_key: str) -> int:
    return _category_order_by_name(_category_of(channel_key))


def _prefixed(category: str, prefix: str) -> str:
    """카테고리 이름 앞에 접두사를 붙입니다. 디스코드 상한(100자)을 넘지 않게 잘라요."""
    name = f"{prefix.strip()} {category}" if prefix.strip() else category
    return name[:100]


def _channel_name(label: str) -> str:
    """'아이디등록' → '아이디등록'. 디스코드가 받아주는 형태로 다듬습니다."""
    return label.strip().replace(" ", "-").lower()[:100]


PICKERS_PER_PAGE = 4   # 📏 한 메세지에 줄은 5개까지. 한 줄은 '이전/다음' 버튼 몫으로 남깁니다.


class GuidedSetup(ChunsikView):
    """`/설치 시작` — 설명부터 하고, **있는 채널을 먼저 물어보는** 안내 흐름.

    예전 `/설치 자동생성`은 무엇을 만들지만 물어봤어요. 그래서 서버에 이미 로그 채널이
    있어도 봇은 그걸 모르고 똑같은 걸 하나 더 만들었고, 관리자는 채널 이름만 보고
    "이게 뭐 하는 곳인지" 판단해야 했습니다.

    이 흐름은 세 가지가 다릅니다 —
      ① **설명이 먼저.** 무엇이 왜 필요한지 읽고 시작해요.
      ② **있는 채널을 먼저 고릅니다.** 새로 만드는 건 고르지 않은 것만.
      ③ 채널마다 **무엇에 쓰는 곳이고 누가 보는 곳인지**를 같이 보여줍니다.

    ⚠️ 마지막 확인을 누르기 전까지는 **아무것도 만들지 않고 아무것도 저장하지 않아요.**
       중간에 창을 닫으면 서버는 손댄 적 없는 상태 그대로입니다.
    """

    def __init__(self, cog: "ChunsikWizard", author_id: int, channels: list, roles: list):
        super().__init__(timeout=600)   # 읽으면서 고르는 창이라 넉넉히
        self.cog = cog
        self.author_id = author_id
        self.channels = channels        # [(이름표, 키), ...] — 아직 지정 안 된 것만
        self.roles = roles
        self.picked_channels = {}       # 키 → 기존 채널 ID (고른 것만)
        self.picked_roles = {}          # 키 → 기존 역할 ID
        self.done = False               # 만들기까지 끝났나 (타임아웃 안내를 가르는 값)

        # 📄 화면을 미리 나눠둡니다. -1은 설명 화면, 마지막은 확인 화면이에요.
        self.pages = []
        for category, items in cog._grouped(channels):
            for i in range(0, len(items), PICKERS_PER_PAGE):
                self.pages.append(("channel", category, items[i:i + PICKERS_PER_PAGE]))
        for i in range(0, len(roles), PICKERS_PER_PAGE):
            self.pages.append(("role", "🎭 관리자 역할", roles[i:i + PICKERS_PER_PAGE]))
        self.page = -1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("명령을 실행한 사람만 누를 수 있어요.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        # 🕰️ 읽다가 자리를 뜬 경우예요. 아무것도 안 만들었다는 걸 분명히 알려야 합니다.
        if self.done:
            return
        try:
            await self.message.edit(
                content="⏳ 시간이 지나 설치를 멈췄어요. **아무것도 만들지 않았습니다.**\n"
                        "`/설치 시작`으로 다시 부르면 처음부터 다시 할 수 있어요.",
                embed=None, view=None)
        except Exception:
            pass

    # ---------- 화면 그리기 ----------

    def _intro_embed(self) -> discord.Embed:
        staff = [k for _, k in self.channels if channel_purpose(k)[1] == SEEN_BY_STAFF]
        users = [k for _, k in self.channels if channel_purpose(k)[1] == SEEN_BY_USERS]
        embed = discord.Embed(
            title="🧰 설치를 도와드릴게요",
            description=(
                "주문하신 기능이 돌아가려면 **채널 몇 개와 관리자 역할 몇 개**를 정해야 해요.\n"
                "지금부터 하나씩 여쭤볼게요. 오래 안 걸립니다.\n\n"
                "**이미 있는 채널을 그대로 쓰셔도 됩니다.** 고르지 않은 것만 새로 만들어요."
            ),
            color=0x89CFF0,
        )
        embed.add_field(
            name=f"📺 정해야 할 채널 {len(self.channels)}개",
            value=(f"· 관리자만 보는 기록용 **{len(staff)}개**\n"
                   f"· 유저도 보는 곳 **{len(users)}개**\n"
                   "채널마다 무엇에 쓰는 곳인지 같이 알려드릴게요." if self.channels
                   else "*(전부 정해져 있어요)*"),
            inline=False)
        embed.add_field(
            name=f"🎭 정해야 할 관리자 역할 {len(self.roles)}개",
            value=("담당자에게 달아줄 표식이에요. **디스코드 권한은 하나도 없는 빈 역할**이라 "
                   "안전합니다 — 이 봇은 '역할에 권한이 있느냐'가 아니라 '그 역할이 설정에 "
                   "적혀 있느냐'로 판단하거든요." if self.roles else "*(전부 정해져 있어요)*"),
            inline=False)
        embed.add_field(
            name="🚦 안심하세요",
            value="**마지막 확인을 누르기 전까지 아무것도 만들지 않아요.** 중간에 그만두면 "
                  "서버는 지금 이대로입니다.",
            inline=False)
        embed.set_footer(text="담지 않은 기능의 채널·역할은 애초에 나오지 않아요.")
        return embed

    def _picker_embed(self, kind: str, category: str, items: list) -> discord.Embed:
        embed = discord.Embed(
            title=f"{category}  ({self.page + 1}/{len(self.pages)})",
            description=("**이미 쓰고 있는 채널이 있으면 골라주세요.** 비워두면 새로 만들어요."
                         if kind == "channel" else
                         "**이미 쓰는 역할이 있으면 골라주세요.** 비워두면 새로 만들어요."),
            color=0x89CFF0,
        )
        for label, key in items:
            if kind == "channel":
                purpose, seen = channel_purpose(key)
                picked = self.picked_channels.get(key)
                mark = f"→ <#{picked}>" if picked else "→ *새로 만듦*"
                embed.add_field(name=f"{label}  ·  {seen}",
                                value=f"{purpose}\n{mark}", inline=False)
            else:
                picked = self.picked_roles.get(key)
                mark = f"→ <@&{picked}>" if picked else "→ *새로 만듦*"
                embed.add_field(name=f"{label} 관리자",
                                value=f"{ROLE_PURPOSE.get(key, '')}\n{mark}", inline=False)
        return embed

    def _confirm_embed(self) -> discord.Embed:
        reuse_c = [(l, k) for l, k in self.channels if k in self.picked_channels]
        make_c = [(l, k) for l, k in self.channels if k not in self.picked_channels]
        reuse_r = [(l, k) for l, k in self.roles if k in self.picked_roles]
        make_r = [(l, k) for l, k in self.roles if k not in self.picked_roles]

        embed = discord.Embed(
            title="🔨 이대로 진행할까요?",
            description="여기서 **만들기**를 눌러야 실제로 만들어집니다.",
            color=0xFFD700)
        if reuse_c:
            add_lines_field(embed, f"♻️ 있는 채널을 그대로 씀 ({len(reuse_c)}개)",
                            [f"`{l}` → <#{self.picked_channels[k]}>" for l, k in reuse_c],
                            note="*…외 {count}개*")
        if make_c:
            for category, items in self.cog._grouped(make_c):
                add_lines_field(embed, f"🆕 새로 만들 채널 — {category} ({len(items)}개)",
                                [f"`#{_channel_name(l)}`" for l, _ in items], note="*…외 {count}개*")
        if reuse_r:
            add_lines_field(embed, f"♻️ 있는 역할을 그대로 씀 ({len(reuse_r)}개)",
                            [f"`{l}` → <@&{self.picked_roles[k]}>" for l, k in reuse_r],
                            note="*…외 {count}개*")
        if make_r:
            add_lines_field(embed, f"🆕 새로 만들 역할 ({len(make_r)}개)",
                            [f"`{l} 관리자`" for l, _ in make_r], note="*…외 {count}개*")
        if not make_c and not make_r:
            embed.add_field(name="만들 것은 없어요",
                            value="전부 있는 것으로 채웠습니다. 지정만 저장할게요.", inline=False)
        return embed

    def _rebuild(self):
        """지금 화면에 맞는 버튼·드롭다운을 다시 답니다."""
        self.clear_items()

        if self.page == -1:
            self.add_item(self._button("시작하기", discord.ButtonStyle.success, "▶️", self._next))
            # 🏃 새 서버라 고를 채널이 애초에 없는 경우예요. 열 화면을 다 넘기게 하지 않습니다.
            self.add_item(self._button("전부 새로 만들기", discord.ButtonStyle.primary, "⚡", self._skip_all))
            self.add_item(self._button("나중에", discord.ButtonStyle.secondary, None, self._cancel))
            return

        if self.page >= len(self.pages):
            self.add_item(self._button("만들기", discord.ButtonStyle.success, "🔨", self._go))
            self.add_item(self._button("뒤로", discord.ButtonStyle.secondary, "◀️", self._prev))
            self.add_item(self._button("취소", discord.ButtonStyle.danger, None, self._cancel))
            return

        kind, _, items = self.pages[self.page]
        for label, key in items:
            self.add_item(self._picker(kind, label, key))
        self.add_item(self._button("◀️ 이전", discord.ButtonStyle.secondary, None, self._prev,
                                   disabled=self.page == 0, row=4))
        self.add_item(self._button("다음 ▶️", discord.ButtonStyle.primary, None, self._next, row=4))

    def _picker(self, kind: str, label: str, key: str):
        """채널/역할 하나를 고르는 드롭다운. 비워두면 '새로 만듦'입니다."""
        if kind == "channel":
            item = discord.ui.ChannelSelect(
                channel_types=[discord.ChannelType.text],
                placeholder=f"「{label}」로 쓸 채널 — 비워두면 새로 만들어요",
                min_values=0, max_values=1)
        else:
            item = discord.ui.RoleSelect(
                placeholder=f"「{label} 관리자」로 쓸 역할 — 비워두면 새로 만들어요",
                min_values=0, max_values=1)

        async def callback(interaction: discord.Interaction):
            store = self.picked_channels if kind == "channel" else self.picked_roles
            if item.values:
                store[key] = item.values[0].id
            else:
                store.pop(key, None)
            # 고른 결과가 임베드에 바로 보여야 "제대로 골라졌나" 헷갈리지 않아요.
            self._rebuild()
            await interaction.response.edit_message(embed=self._current_embed(), view=self)

        item.callback = callback
        return item

    @staticmethod
    def _button(label, style, emoji, callback, *, disabled=False, row=None):
        button = discord.ui.Button(label=label, style=style, emoji=emoji, disabled=disabled, row=row)
        button.callback = callback
        return button

    def _current_embed(self) -> discord.Embed:
        if self.page == -1:
            return self._intro_embed()
        if self.page >= len(self.pages):
            return self._confirm_embed()
        return self._picker_embed(*self.pages[self.page])

    async def _show(self, interaction: discord.Interaction):
        self._rebuild()
        await interaction.response.edit_message(embed=self._current_embed(), view=self)

    # ---------- 버튼 ----------

    async def _next(self, interaction: discord.Interaction):
        self.page += 1
        await self._show(interaction)

    async def _prev(self, interaction: discord.Interaction):
        self.page -= 1
        await self._show(interaction)

    async def _skip_all(self, interaction: discord.Interaction):
        """고르는 화면을 통째로 건너뛰고 확인 화면으로. (아무것도 안 골랐으니 전부 새로 만듭니다)"""
        self.picked_channels.clear()
        self.picked_roles.clear()
        self.page = len(self.pages)
        await self._show(interaction)

    async def _cancel(self, interaction: discord.Interaction):
        self.done = True
        self.stop()
        await interaction.response.edit_message(
            content="괜찮아요. **아무것도 만들지 않았습니다.**\n"
                    "필요할 때 `/설치 시작`으로 다시 불러주세요.",
            embed=None, view=None)

    async def _go(self, interaction: discord.Interaction):
        self.done = True
        self.stop()
        await interaction.response.edit_message(
            content="🔨 만드는 중이에요…", embed=None, view=None)
        result = await self.cog.apply(
            interaction.guild,
            [(l, k) for l, k in self.channels if k not in self.picked_channels],
            [(l, k) for l, k in self.roles if k not in self.picked_roles],
            prefix="",
            reuse_channels=self.picked_channels,
            reuse_roles=self.picked_roles,
        )
        await interaction.edit_original_response(content=None, embed=result, view=None)


class ConfirmSetup(ChunsikView):
    """진짜 만들지 물어보는 확인 창. 만들고 나면 되돌리기가 번거로워서 한 번 끊어요."""

    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.value = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("명령을 실행한 사람만 누를 수 있어요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="만들기", style=discord.ButtonStyle.success, emoji="🔨")
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        self.stop()


class ChunsikWizard(commands.Cog):
    """설치 마법사 — 담긴 기능에 필요한 채널·역할을 만들고 지정합니다."""

    def __init__(self, bot):
        self.bot = bot

    # ---------- 무엇이 필요한가 ----------

    def _tables(self):
        """`/설정` 명령이 쓰는 이름표를 빌려옵니다. (없으면 빈 표)

        🧩 `from cogs.setting import ...` 하지 않아요. 코그끼리 직접 import하면 한쪽만
           담아 납품했을 때 import 단계에서 죽습니다. 설정 모듈은 코어라 항상 있지만,
           규칙은 규칙대로 지킵니다. (check_modules.py도 이 표를 이렇게 읽어요)
        """
        cog = self.bot.get_cog("ChunsikSetting")
        if cog is None:
            return {}, {}
        return cog._CHANNEL_COMMANDS, cog._ROLE_COMMANDS

    def _missing(self):
        """아직 지정되지 않은 (채널, 역할) 목록. 담긴 기능 것만 봅니다."""
        channel_table, role_table = self._tables()
        settings = load_settings()
        set_channels = settings.get("channels", {})
        set_roles = settings.get("roles", {})

        channels = [(label, key) for label, key in channel_table.items()
                    if is_active("channels", key, ENABLED_MODULE_KEYS) and not set_channels.get(key)]
        # 🗂️ 카테고리 순서는 CATEGORY_BY_CHANNEL에 적힌 순서를 따라갑니다.
        #    (표를 고치면 서버에 생기는 순서도 같이 바뀌어요)
        channels.sort(key=lambda item: (_category_order(item[1]), item[0]))
        roles = [(label, key) for label, key in role_table.items()
                 if is_active("roles", key, ENABLED_MODULE_KEYS) and not set_roles.get(key)]
        return channels, roles

    @staticmethod
    def _grouped(channels):
        """[(카테고리, [(이름, 키), ...]), ...] — 표에 적힌 순서대로."""
        groups = {}
        for label, key in channels:
            groups.setdefault(_category_of(key), []).append((label, key))
        return sorted(groups.items(), key=lambda kv: _category_order_by_name(kv[0]))

    설치 = app_commands.Group(name="설치", description="[관리자] 담긴 기능에 필요한 채널·역할을 한 번에 만들고 지정합니다.")

    @설치.command(name="시작", description="[관리자] 설명을 보면서 하나씩 정해요. 이미 쓰는 채널이 있으면 그대로 씁니다.")
    async def guided(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ 서버 관리자만 쓸 수 있어요!", ephemeral=True)

        channels, roles = self._missing()
        if not channels and not roles:
            return await interaction.response.send_message(
                "✅ 이미 전부 지정돼 있어요. 더 할 게 없습니다! 🎉\n"
                "바꾸고 싶은 게 있으면 `/설정 채널`·`/설정 관리자`로 하나씩 고칠 수 있어요.",
                ephemeral=True)

        view = GuidedSetup(self, interaction.user.id, channels, roles)
        view._rebuild()
        await interaction.response.send_message(embed=view._intro_embed(), view=view, ephemeral=True)
        # ⏳ 타임아웃 안내를 그리려면 이 창을 잡고 있어야 해요. (on_timeout 참고)
        view.message = await interaction.original_response()

    @설치.command(name="점검", description="[관리자] 아직 지정되지 않은 채널·역할이 무엇인지 확인해요. (아무것도 만들지 않아요)")
    async def check(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ 서버 관리자만 쓸 수 있어요!", ephemeral=True)

        channels, roles = self._missing()
        embed = discord.Embed(
            title="🧰 설치 점검",
            description=("전부 지정돼 있어요. 더 할 게 없습니다! 🎉"
                         if not channels and not roles else
                         "아래가 아직 비어 있어요. `/설치 자동생성`으로 한 번에 만들 수 있어요."),
            color=0x89CFF0 if (channels or roles) else 0x77DD77,
        )
        if channels:
            for category, items in self._grouped(channels):
                embed.add_field(name=f"{category} — {len(items)}개",
                                value=", ".join(f"`{label}`" for label, _ in items), inline=False)
        if roles:
            embed.add_field(name=f"🎭 관리자 역할 {len(roles)}개",
                            value=", ".join(f"`{label}`" for label, _ in roles), inline=False)
        embed.set_footer(text="담지 않은 기능의 채널·역할은 여기 나오지 않아요.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @설치.command(name="자동생성", description="[관리자] 빠져 있는 채널과 관리자 역할을 만들어서 곧바로 지정해요.")
    @app_commands.describe(접두사="카테고리 이름 앞에 붙일 말 (예: `봇` → `봇 📋 로그`). 생략하면 안 붙여요",
                           역할도="관리자 역할까지 만들지 (기본값: True)")
    async def auto(self, interaction: discord.Interaction, 접두사: str = "", 역할도: bool = True):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ 서버 관리자만 쓸 수 있어요!", ephemeral=True)

        channels, roles = self._missing()
        if not 역할도:
            roles = []
        if not channels and not roles:
            return await interaction.response.send_message(
                "✅ 이미 전부 지정돼 있어요. 만들 게 없습니다.", ephemeral=True)

        # 🚧 만들기 전에 무엇이 생길지 그대로 보여줍니다. 채널·역할을 만드는 건 되돌리기가
        #    번거로운 일이라, 확인 없이 진행하지 않아요.
        groups = self._grouped(channels)
        preview = discord.Embed(
            title="🔨 이렇게 만들게요",
            description=(f"카테고리 **{len(groups)}개**에 나눠 담아요. 성격이 다른 채널을 한 곳에 몰아넣으면 "
                         "서버 목록이 벽이 돼서요." if groups else None),
            color=0xFFD700)
        for category, items in groups:
            preview.add_field(name=_prefixed(category, 접두사),
                              value="\n".join(f"`#{_channel_name(l)}`" for l, _ in items), inline=True)
        if roles:
            preview.add_field(name=f"🎭 역할 {len(roles)}개 (권한 없는 표식용)",
                              value="\n".join(f"`{l} 관리자`" for l, _ in roles), inline=False)
        preview.set_footer(text="60초 안에 눌러주세요.")

        view = ConfirmSetup(interaction.user.id)
        await interaction.response.send_message(embed=preview, view=view, ephemeral=True)
        await view.wait()
        if not view.value:
            return await interaction.edit_original_response(
                content="취소했어요. 아무것도 만들지 않았습니다.", embed=None, view=None)


        result = await self.apply(interaction.guild, channels, roles, 접두사)
        await interaction.edit_original_response(content=None, embed=result, view=None)

    # ---------- 실제로 만들기 ----------

    async def apply(self, guild: discord.Guild, channels: list, roles: list, prefix: str = "",
                    reuse_channels: dict = None, reuse_roles: dict = None) -> discord.Embed:
        """채널·역할을 만들고 지정까지 마칩니다. 결과 임베드를 돌려줘요.

        `/설치 자동생성`(전부 새로)과 `/설치 시작`(고른 건 그대로 쓰고 나머지만 새로)이
        **같은 코드를 씁니다.** 만드는 순서·권한 실패 처리·상점 매대 세우기·저장 실패
        안내가 두 벌로 갈라지면 한쪽만 고쳐지거든요.

        · channels / roles : **새로 만들** 것 [(이름표, 키), ...]
        · reuse_* : 이미 있는 것을 그대로 쓰는 {키: ID} — 만들지 않고 지정만 합니다
        """
        reuse_channels = reuse_channels or {}
        reuse_roles = reuse_roles or {}

        settings = load_settings()
        settings.setdefault("channels", {})
        settings.setdefault("roles", {})

        # ♻️ 고른 것은 만들 것 없이 지정만 하면 끝이에요.
        for key, channel_id in reuse_channels.items():
            settings["channels"][key] = channel_id
        for key, role_id in reuse_roles.items():
            settings["roles"][key] = [role_id]

        made_channels, made_roles, failed = [], [], []
        made_categories = []
        board_note = ""
        blocked = False  # 권한 때문에 막혔으면 나머지도 전부 막혀요. 같은 말을 반복하지 않습니다.

        for category_name, items in self._grouped(channels):
            if blocked:
                break
            name = _prefixed(category_name, prefix)
            # 같은 이름이 이미 있으면 그걸 씁니다. (두 번 돌려도 카테고리가 안 늘어나요)
            category = discord.utils.get(guild.categories, name=name)
            if category is None:
                try:
                    category = await guild.create_category(name)
                    made_categories.append(category)
                except discord.Forbidden:
                    failed.append(f"{name} 카테고리 (봇에게 **채널 관리** 권한이 없어요)")
                    blocked = True
                    break
                except Exception as e:
                    failed.append(f"{name} 카테고리 ({type(e).__name__}: {e})")
                    category = None  # 카테고리 없이라도 채널은 만들어 둡니다

            for label, key in items:
                try:
                    ch = await guild.create_text_channel(_channel_name(label), category=category)
                    settings["channels"][key] = ch.id
                    made_channels.append(ch)
                except discord.Forbidden:
                    failed.append(f"#{label} (봇에게 **채널 관리** 권한이 없어요)")
                    blocked = True
                    break
                except Exception as e:
                    failed.append(f"#{label} ({type(e).__name__}: {e})")

        for label, key in roles:
            try:
                # 🔐 권한 0으로 만듭니다. 이 봇은 역할 ID가 settings.json에 적혀 있는지로
                #    권한을 보기 때문에, 디스코드 권한은 하나도 필요 없어요.
                role = await guild.create_role(
                    name=f"{label} 관리자", permissions=discord.Permissions.none(),
                    reason="/설치")
                settings["roles"][key] = [role.id]
                made_roles.append(role)
            except discord.Forbidden:
                failed.append(f"@{label} 관리자 (봇에게 **역할 관리** 권한이 없어요)")
                break
            except Exception as e:
                failed.append(f"@{label} 관리자 ({type(e).__name__}: {e})")

        # 🛒 상점 채널을 **새로 만들었다면** 매대까지 세워둡니다. 채널만 덩그러니 있으면
        #    클라이언트가 `/상점 생성`을 따로 쳐야 하는데, 그게 제일 자주 빠지는 자리예요.
        #    (상점을 안 담은 구성에서는 코그 자체가 없어서 조용히 건너뜁니다)
        #    ⚠️ 이미 쓰던 채널을 고른 경우엔 건드리지 않아요 — 남의 채널에 매대를 함부로
        #       세우면 안 되고, 이미 매대가 있을 수도 있습니다.
        shop_channel_id = settings["channels"].get("shop_board")
        if shop_channel_id and any(c.id == shop_channel_id for c in made_channels):
            shop = self.bot.get_cog("ChunsikShop")
            if shop is not None:
                try:
                    if await shop.ensure_board(shop_channel_id, "상점", "필요한 걸 골라 담으세요."):
                        board_note = f"🛒 <#{shop_channel_id}> 에 매대까지 세워뒀어요. `/상점 항목추가`로 물건만 넣으면 됩니다."
                except Exception as e:
                    failed.append(f"상점 매대 ({type(e).__name__}: {e}) — 그 채널에서 `/상점 생성`을 직접 실행해 주세요")

        # 💾 만든 것부터 먼저 저장합니다. 여기서 실패하면 채널은 생겼는데 지정이 안 된
        #    상태라, 그때는 `/설정 채널 …`로 손으로 이어 붙일 수 있게 그대로 알려줘요.
        try:
            save_settings(settings)
            saved = True
        except Exception as e:
            saved = False
            failed.append(f"설정 저장 실패 ({type(e).__name__}: {e}) — 만든 채널을 `/설정 채널`로 직접 지정해 주세요")

        result = discord.Embed(
            title="🧰 설치 완료" if saved and not failed else "🧰 설치 결과",
            color=0x77DD77 if saved and not failed else 0xFF6B6B,
        )
        # ✂️ 만든 것이 많으면 필드 하나(1024자)를 넘길 수 있어요. 그러면 **결과 화면이
        #    통째로 안 뜹니다** — 채널은 다 만들어놓고 "무엇을 만들었는지"만 사라지는,
        #    제일 헷갈리는 실패예요.
        if reuse_channels or reuse_roles:
            add_lines_field(
                result, f"♻️ 있던 것을 그대로 씀 ({len(reuse_channels) + len(reuse_roles)}개)",
                [f"<#{cid}>" for cid in reuse_channels.values()]
                + [f"<@&{rid}>" for rid in reuse_roles.values()],
                note="*…외 {count}개*")
        if made_channels:
            add_lines_field(
                result, f"📺 만든 채널 {len(made_channels)}개 (카테고리 {len(made_categories)}개)",
                [c.mention for c in made_channels] + ([board_note] if board_note else []),
                note="*…외 {count}개*")
        if made_roles:
            add_lines_field(
                result, f"🎭 만든 역할 {len(made_roles)}개",
                [r.mention for r in made_roles] + ["담당자에게 이 역할을 달아주면 그 기능의 관리자가 돼요."],
                note="*…외 {count}개*")
        if failed:
            add_lines_field(result, "🚨 못 만든 것", failed[:10], note="*…외 {count}개*")
        result.set_footer(text="남은 설정은 /설치 점검으로 다시 확인할 수 있어요.")
        return result
