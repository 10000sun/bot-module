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

        settings = load_settings()
        settings.setdefault("channels", {})
        settings.setdefault("roles", {})
        made_channels, made_roles, failed = [], [], []

        made_categories = []
        board_note = ""
        blocked = False  # 권한 때문에 막혔으면 나머지도 전부 막혀요. 같은 말을 반복하지 않습니다.

        for category_name, items in groups:
            if blocked:
                break
            name = _prefixed(category_name, 접두사)
            # 같은 이름이 이미 있으면 그걸 씁니다. (두 번 돌려도 카테고리가 안 늘어나요)
            category = discord.utils.get(interaction.guild.categories, name=name)
            if category is None:
                try:
                    category = await interaction.guild.create_category(name)
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
                    ch = await interaction.guild.create_text_channel(_channel_name(label), category=category)
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
                role = await interaction.guild.create_role(
                    name=f"{label} 관리자", permissions=discord.Permissions.none(),
                    reason="/설치 자동생성")
                settings["roles"][key] = [role.id]
                made_roles.append(role)
            except discord.Forbidden:
                failed.append(f"@{label} 관리자 (봇에게 **역할 관리** 권한이 없어요)")
                break
            except Exception as e:
                failed.append(f"@{label} 관리자 ({type(e).__name__}: {e})")

        # 🛒 상점 채널을 새로 만들었다면 **매대까지** 세워둡니다. 채널만 덩그러니 있으면
        #    클라이언트가 `/상점 생성`을 따로 쳐야 하는데, 그게 제일 자주 빠지는 자리예요.
        #    (상점을 안 담은 구성에서는 코그 자체가 없어서 조용히 건너뜁니다)
        shop_channel_id = settings["channels"].get("shop_board")
        if shop_channel_id and any(c.id == shop_channel_id for c in made_channels):
            shop = self.bot.get_cog("ChunsikShop")
            if shop is not None:
                try:
                    if await shop.ensure_board(shop_channel_id, "상점", "필요한 걸 골라 담으세요."):
                        board_note = f"\n🛒 <#{shop_channel_id}> 에 매대까지 세워뒀어요. `/상점 항목추가`로 물건만 넣으면 됩니다."
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
        if made_channels:
            add_lines_field(
                result, f"📺 만든 채널 {len(made_channels)}개 (카테고리 {len(made_categories)}개)",
                [c.mention for c in made_channels] + ([board_note.strip()] if board_note else []),
                note="*…외 {count}개*")
        if made_roles:
            add_lines_field(
                result, f"🎭 만든 역할 {len(made_roles)}개",
                [r.mention for r in made_roles] + ["담당자에게 이 역할을 달아주면 그 기능의 관리자가 돼요."],
                note="*…외 {count}개*")
        if failed:
            add_lines_field(result, "🚨 못 만든 것", failed[:10], note="*…외 {count}개*")
        result.set_footer(text="남은 설정은 /설치 점검으로 다시 확인할 수 있어요.")
        await interaction.edit_original_response(content=None, embed=result, view=None)
