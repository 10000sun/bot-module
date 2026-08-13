"""MariProfile — 프로필 카드(레벨/소속/직책/업적/사진)."""

import os
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands

from mari_config import PROFILE_FILE, PROFILE_PHOTO_DIR
from mari_storage import atomic_json_save_or_raise, safe_json_load
from mari_settings import has_admin_or_role

# 🖼️ 프로필 사진으로 받아줄 형식과 크기 상한.
# 상한은 봇이 파일을 올릴 수 있는 한도(부스트 없는 서버 기준 8MB)에 맞췄어요.
# 저장할 때가 아니라 **보여줄 때마다** 첨부해서 올리기 때문에, 여기서 막지 않으면
# /프로필을 부를 때마다 큰 파일이 올라가서 느려집니다.
PHOTO_EXTENSIONS = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
}
PHOTO_MAX_BYTES = 8 * 1024 * 1024


# ========== 👤 [신규] 프로필 시스템 ==========
class MariProfile(commands.Cog):
    """닉네임/아이디/레벨/소속/직책/업적/사진을 보여주는 유저 프로필 카드 시스템"""

    def __init__(self, bot):
        self.bot = bot
        self.PROFILE_FILE = PROFILE_FILE

        # 🖱️ [신규] 멤버 우클릭 → 앱 → 프로필 보기
        # (컨텍스트 메뉴 등록/해제 규칙은 core.py·snooze.py와 같아요)
        self.profile_menu = app_commands.ContextMenu(name="프로필 보기", callback=self.show_profile_menu)
        self.bot.tree.add_command(self.profile_menu)

    async def cog_unload(self):
        self.bot.tree.remove_command(self.profile_menu.name, type=self.profile_menu.type)

    async def show_profile_menu(self, interaction: discord.Interaction, member: discord.Member):
        """멤버를 우클릭해서 프로필 카드를 바로 봅니다. (`/프로필`과 같은 내용, 나만 보여요)"""
        embed, attachment = self._build_profile_payload(member, interaction.user)
        await interaction.response.send_message(embed=embed, file=attachment, ephemeral=True)

    # ---------- 🖼️ 프로필 사진 파일 다루기 ----------
    # 🐛 [버그 수정] 예전엔 data["photos"][uid]에 첨부파일 URL(사진.url)을 그대로 저장했어요.
    # 그런데 디스코드 CDN 링크에는 만료 서명(ex/is/hm)이 붙어 있어서 하루 이틀이면 죽습니다.
    # 등록할 땐 멀쩡히 보이다가 며칠 뒤 조용히 깨지는 거라, 관리자가 알아채기도 어려웠어요.
    # 이제 이미지를 봇 서버에 받아두고, 보여줄 때마다 그 파일을 첨부해서 씁니다.
    #
    # 📦 저장 형식도 바뀌었어요.
    #   예전: "https://cdn.discordapp.com/..."   (문자열 URL)
    #   지금: {"file": "123456789.png"}          (PROFILE_PHOTO_DIR 안의 파일 이름)
    # 예전 값이 남아 있어도 그대로 URL로 취급해서 보여줘요. (만료됐으면 안 보이지만,
    # 관리자가 /프로필설정 사진으로 다시 올리면 새 방식으로 저장됩니다)

    @staticmethod
    def _photo_path(filename: str) -> str:
        return os.path.join(PROFILE_PHOTO_DIR, filename)

    def _stored_photo_file(self, entry) -> Optional[str]:
        """저장된 값에서 '실제로 존재하는 로컬 파일 이름'을 꺼냅니다. 없으면 None."""
        if not isinstance(entry, dict):
            return None
        filename = entry.get("file")
        if not filename:
            return None
        return filename if os.path.exists(self._photo_path(filename)) else None

    def _delete_photo_files(self, user_id_str: str):
        """그 유저의 사진 파일을 확장자와 상관없이 전부 지웁니다.
        (png로 올렸다가 jpg로 바꾸면 예전 파일이 남아 쌓이기 때문이에요)"""
        for ext in set(PHOTO_EXTENSIONS.values()):
            path = self._photo_path(f"{user_id_str}{ext}")
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                print(f"⚠️ 프로필 사진 삭제 실패({path}): {type(e).__name__}: {e}")

    # 🏆 [업적] 한 번 만들어두고 목록에서 골라 붙이는 방식이에요.
    # 예전엔 부여할 때마다 내용을 손으로 쳤어요. 그러다 보니 같은 업적을 여러 명에게 줄 때
    # 오타 하나로 프로필마다 다른 글자가 남고, 나중에 문구를 바꾸려면 전부 찾아 고쳐야 했어요.
    # 이제 "만들어둔 업적 목록(achievement_catalog)"을 따로 들고 있어서,
    # 부여할 땐 목록에서 고르고 이름을 바꾸면 갖고 있는 사람 전원에게 한 번에 반영됩니다.
    # (목록에 없는 걸 직접 입력해도 되고, 그러면 그 자리에서 목록에 자동 등록돼요)
    def _load_profile_data(self) -> dict:
        default = {
            "role_categories": {"레벨": [], "소속": [], "직책": []},
            "achievements": {},          # {user_id(str): [업적 이름, ...]}
            "photos": {},
            "achievement_catalog": [],   # 만들어둔 업적 이름 목록
        }
        data = safe_json_load(self.PROFILE_FILE, None)
        if data is None:
            return default

        # 🔄 [마이그레이션] "캠프" 카테고리는 세금/견학을 다루는 캠프 시스템과 헷갈리기 쉬워서
        # "소속"으로 이름을 바꿨어요. 예전에 등록해둔 값이 있으면 그대로 옮겨드려요.
        rc = data.get("role_categories", {})
        if "캠프" in rc and "소속" not in rc:
            rc["소속"] = rc.pop("캠프")
        data.setdefault("role_categories", {"레벨": [], "소속": [], "직책": []})
        for cat in ("레벨", "소속", "직책"):
            data["role_categories"].setdefault(cat, [])
        data.setdefault("achievements", {})
        data.setdefault("photos", {})

        # 🔄 [마이그레이션] 업적 목록은 나중에 추가된 개념이라 예전 파일엔 이 키가 없어요.
        # 이미 사람들에게 부여돼 있던 업적들을 그대로 목록으로 끌어올려서,
        # 기존 업적도 처음부터 골라 쓸 수 있게 합니다. (중복은 한 번만, 순서는 유지)
        if "achievement_catalog" not in data:
            catalog = []
            for owned in data["achievements"].values():
                for name in owned:
                    if name not in catalog:
                        catalog.append(name)
            data["achievement_catalog"] = catalog
        return data

    def _save_profile_data(self, data: dict):
        atomic_json_save_or_raise(self.PROFILE_FILE, data, indent=2)

    def _is_profile_admin(self, interaction: discord.Interaction) -> bool:
        return has_admin_or_role(interaction, "profile_admin")

    @app_commands.command(name="프로필", description="유저의 프로필 카드를 보여줘요. (닉네임/아이디/레벨/소속/직책/업적)")
    @app_commands.guild_only()
    @app_commands.describe(유저="확인할 유저 (생략 시 본인)")
    async def show_profile(self, interaction: discord.Interaction, 유저: Optional[discord.Member] = None):
        target = 유저 or interaction.user
        embed, attachment = self._build_profile_payload(target, interaction.user)
        await interaction.response.send_message(embed=embed, file=attachment)

    def _build_profile_payload(self, target: discord.Member, requester: discord.abc.User) -> tuple:
        """프로필 카드를 만들어 (임베드, 첨부파일) 로 돌려줍니다.

        🖱️ 슬래시 `/프로필`과 멤버 우클릭 메뉴가 **같은 화면**을 쓰도록 여기 한 군데에 모아뒀어요.
        (한쪽만 고쳐서 두 화면이 달라지는 걸 막으려는 거예요)
        """
        data = self._load_profile_data()
        role_categories = data["role_categories"]
        user_id_str = str(target.id)

        # 🏷️ [개선] 임베드 안의 역할 언급은 실제로 알림(핑)이 가지 않아서, 아이디 태그처럼
        # 예쁜 색깔 태그 형태로 보여줄 수 있어요. 그래서 이름 텍스트 대신 역할 태그로 표시합니다.
        def matched_role_tags(category: str) -> list:
            ids = set(role_categories.get(category, []))
            return [r.mention for r in target.roles if r.id in ids]

        레벨_목록 = matched_role_tags("레벨")
        소속_목록 = matched_role_tags("소속")
        직책_목록 = matched_role_tags("직책")
        업적_목록 = data.get("achievements", {}).get(user_id_str, [])

        embed = discord.Embed(
            title=f"👤 {target.display_name}님의 프로필",
            color=0x5ce6b4,
        )
        embed.add_field(name="📛 닉네임", value=target.display_name, inline=True)
        embed.add_field(name="🆔 아이디", value=f"{target.mention}\n`{target.name}`", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)  # 줄맞춤용 빈 칸

        embed.add_field(name="⭐ 레벨", value=", ".join(레벨_목록) if 레벨_목록 else "*미지정*", inline=True)
        embed.add_field(name="🏕️ 소속", value=", ".join(소속_목록) if 소속_목록 else "*미지정*", inline=True)
        embed.add_field(name="💼 직책", value=", ".join(직책_목록) if 직책_목록 else "*미지정*", inline=True)

        if 업적_목록:
            achievements_text = "\n".join(f"🏆 {i+1}. {a}" for i, a in enumerate(업적_목록))
        else:
            achievements_text = "*아직 등록된 업적이 없어요.*"
        embed.add_field(name="📜 업적", value=achievements_text, inline=False)

        # 🖼️ 사진: 관리자가 등록해 둔 프로필 사진이 있으면 그걸, 없으면 디스코드 아바타를 오른쪽에 표시
        entry = data.get("photos", {}).get(user_id_str)
        photo_file = self._stored_photo_file(entry)
        attachment = discord.utils.MISSING

        if photo_file:
            # 봇 서버에 보관해둔 원본을 함께 올리고, 임베드에서 attachment:// 로 가리켜요.
            # 이러면 링크 만료라는 개념 자체가 없어집니다.
            attachment = discord.File(self._photo_path(photo_file), filename=photo_file)
            embed.set_thumbnail(url=f"attachment://{photo_file}")
        elif isinstance(entry, str) and entry.startswith("http"):
            # 📦 예전 방식(첨부파일 URL)으로 저장된 값. 아직 살아있으면 보이고, 만료됐으면 안 보여요.
            # `/프로필설정 사진`으로 다시 올리면 새 방식으로 저장됩니다.
            embed.set_thumbnail(url=entry)
        else:
            embed.set_thumbnail(url=target.display_avatar.url)

        embed.set_footer(text=f"요청자: {requester.display_name}", icon_url=requester.display_avatar.url)
        return embed, attachment

    # ---------- 관리자 설정 명령어 ----------
    profile_setting_group = app_commands.Group(name="프로필설정", description="프로필 시스템 관리자 설정 (레벨/소속/직책/업적/사진)")

    @profile_setting_group.command(name="역할등록", description="[관리자] 레벨/소속/직책 카테고리에 역할을 등록합니다. (해당 역할 보유 시 프로필에 자동 표시)")
    @app_commands.describe(종류="등록할 카테고리", 역할="등록할 역할")
    @app_commands.choices(종류=[
        app_commands.Choice(name="레벨", value="레벨"),
        app_commands.Choice(name="소속", value="소속"),
        app_commands.Choice(name="직책", value="직책"),
    ])
    async def add_profile_role(self, interaction: discord.Interaction, 종류: app_commands.Choice[str], 역할: discord.Role):
        if not self._is_profile_admin(interaction):
            return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)
        data = self._load_profile_data()
        role_ids = data["role_categories"][종류.value]
        if 역할.id in role_ids:
            return await interaction.response.send_message(f"❌ 이미 **{종류.value}** 카테고리에 등록된 역할이에요.", ephemeral=True)
        role_ids.append(역할.id)
        self._save_profile_data(data)
        await interaction.response.send_message(f"✅ {역할.mention} 역할을 **{종류.value}** 카테고리에 등록했어요. 이제 이 역할을 가진 유저는 프로필에 자동으로 표시돼요.", ephemeral=True)

    @profile_setting_group.command(name="역할해제", description="[관리자] 레벨/소속/직책 카테고리에서 역할을 해제합니다.")
    @app_commands.describe(종류="해제할 카테고리", 역할="해제할 역할")
    @app_commands.choices(종류=[
        app_commands.Choice(name="레벨", value="레벨"),
        app_commands.Choice(name="소속", value="소속"),
        app_commands.Choice(name="직책", value="직책"),
    ])
    async def remove_profile_role(self, interaction: discord.Interaction, 종류: app_commands.Choice[str], 역할: discord.Role):
        if not self._is_profile_admin(interaction):
            return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)
        data = self._load_profile_data()
        role_ids = data["role_categories"][종류.value]
        if 역할.id not in role_ids:
            return await interaction.response.send_message(f"❌ **{종류.value}** 카테고리에 등록되어 있지 않은 역할이에요.", ephemeral=True)
        role_ids.remove(역할.id)
        self._save_profile_data(data)
        await interaction.response.send_message(f"🗑️ {역할.mention} 역할을 **{종류.value}** 카테고리에서 해제했어요.", ephemeral=True)

    # ---------- 🏆 업적 ----------
    async def achievement_catalog_autocomplete(self, interaction: discord.Interaction, current: str):
        """지금까지 만들어둔 업적을 자동완성으로 보여줍니다.

        디스코드 자동완성은 후보를 25개까지만 보여줄 수 있는데, 타이핑하는 대로 걸러지기 때문에
        업적이 수십 개로 늘어도 원하는 걸 금방 찾을 수 있어요.
        목록에 없는 걸 직접 쳐도 그대로 부여되고, 그때 목록에도 자동으로 등록됩니다.
        """
        try:
            catalog = self._load_profile_data().get("achievement_catalog", [])
            return [app_commands.Choice(name=a[:100], value=a)
                    for a in catalog if current.lower() in a.lower()][:25]
        except Exception as e:
            print(f"⚠️ 업적 목록 자동완성 조회 실패: {type(e).__name__}: {e}")
            return []

    async def user_achievement_autocomplete(self, interaction: discord.Interaction, current: str):
        """삭제할 때는 '그 유저가 실제로 가진 업적'만 보여줘요. (번호를 세거나 오타 낼 일이 없어요)"""
        try:
            target = getattr(interaction.namespace, "유저", None)
            if target is None:
                return []
            owned = self._load_profile_data().get("achievements", {}).get(str(target.id), [])
            return [app_commands.Choice(name=a[:100], value=a)
                    for a in owned if current.lower() in a.lower()][:25]
        except Exception as e:
            print(f"⚠️ 보유 업적 자동완성 조회 실패: {type(e).__name__}: {e}")
            return []

    @profile_setting_group.command(name="업적추가", description="[관리자] 유저에게 업적을 붙여줍니다. (만들어둔 업적은 목록에서 골라요)")
    @app_commands.describe(유저="업적을 받을 유저", 내용="목록에서 고르거나, 새 업적을 직접 입력하세요")
    @app_commands.autocomplete(내용=achievement_catalog_autocomplete)
    async def add_achievement(self, interaction: discord.Interaction, 유저: discord.Member, 내용: str):
        if not self._is_profile_admin(interaction):
            return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)

        name = 내용.strip()
        if not name:
            return await interaction.response.send_message("❌ 업적 내용이 비어 있어요.", ephemeral=True)

        data = self._load_profile_data()
        user_id_str = str(유저.id)
        owned = data["achievements"].setdefault(user_id_str, [])

        if name in owned:
            return await interaction.response.send_message(f"❌ {유저.mention}님은 이미 `{name}` 업적을 갖고 있어요.", ephemeral=True)

        # 📝 목록에 없는 업적을 직접 입력한 경우, 부여하면서 목록에도 등록해요.
        # 그래야 다음부터는 같은 업적을 골라 붙일 수 있어서 문구가 어긋나지 않습니다.
        is_new = name not in data["achievement_catalog"]
        if is_new:
            data["achievement_catalog"].append(name)

        owned.append(name)
        self._save_profile_data(data)

        note = "\n└ 처음 쓰는 업적이라 목록에도 등록해뒀어요. 다음부터는 골라서 붙일 수 있어요." if is_new else ""
        await interaction.response.send_message(f"🏆 {유저.mention}님에게 `{name}` 업적을 추가했어요.{note}", ephemeral=True)

    @profile_setting_group.command(name="업적삭제", description="[관리자] 유저에게서 업적을 떼어냅니다. (목록에서는 지워지지 않아요)")
    @app_commands.describe(유저="대상 유저", 내용="삭제할 업적")
    @app_commands.autocomplete(내용=user_achievement_autocomplete)
    async def remove_achievement(self, interaction: discord.Interaction, 유저: discord.Member, 내용: str):
        if not self._is_profile_admin(interaction):
            return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)

        name = 내용.strip()
        data = self._load_profile_data()
        user_id_str = str(유저.id)
        owned = data["achievements"].get(user_id_str, [])

        if name not in owned:
            return await interaction.response.send_message(f"❌ {유저.mention}님은 `{name}` 업적을 갖고 있지 않아요.", ephemeral=True)

        owned.remove(name)
        if not owned:
            del data["achievements"][user_id_str]     # 빈 칸은 남겨두지 않아요
        self._save_profile_data(data)
        await interaction.response.send_message(f"🗑️ {유저.mention}님의 `{name}` 업적을 삭제했어요.", ephemeral=True)

    @profile_setting_group.command(name="업적목록", description="[관리자] 만들어둔 업적과 각각 몇 명이 갖고 있는지 보여줍니다.")
    async def list_achievements(self, interaction: discord.Interaction):
        if not self._is_profile_admin(interaction):
            return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)

        data = self._load_profile_data()
        catalog = data.get("achievement_catalog", [])
        if not catalog:
            return await interaction.response.send_message(
                "📭 아직 만들어둔 업적이 없어요. `/프로필설정 업적추가`로 새 내용을 입력하면 자동으로 목록에 등록돼요.",
                ephemeral=True,
            )

        counts = {}
        for owned in data.get("achievements", {}).values():
            for a in owned:
                counts[a] = counts.get(a, 0) + 1

        lines = [f"🏆 `{a}` — {counts.get(a, 0)}명" for a in catalog]
        embed = discord.Embed(title="🏆 업적 목록", description="\n".join(lines)[:4000], color=0x5ce6b4)
        embed.set_footer(text=f"총 {len(catalog)}개")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @profile_setting_group.command(name="업적수정", description="[관리자] 업적 내용을 고칩니다. 갖고 있는 사람 전원에게 한 번에 반영돼요.")
    @app_commands.describe(내용="수정할 업적", 새내용="새 업적 내용")
    @app_commands.autocomplete(내용=achievement_catalog_autocomplete)
    async def edit_achievement(self, interaction: discord.Interaction, 내용: str, 새내용: str):
        if not self._is_profile_admin(interaction):
            return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)

        old, new_name = 내용.strip(), 새내용.strip()
        if not new_name:
            return await interaction.response.send_message("❌ 새 내용이 비어 있어요.", ephemeral=True)
        if old == new_name:
            return await interaction.response.send_message("❌ 지금과 같은 내용이에요.", ephemeral=True)

        data = self._load_profile_data()
        if old not in data["achievement_catalog"]:
            return await interaction.response.send_message(f"❌ `{old}` 업적이 목록에 없어요.", ephemeral=True)
        if new_name in data["achievement_catalog"]:
            return await interaction.response.send_message(f"❌ `{new_name}`은(는) 이미 있는 업적이에요.", ephemeral=True)

        data["achievement_catalog"][data["achievement_catalog"].index(old)] = new_name

        # ✨ 목록으로 관리하는 가장 큰 이점이에요. 갖고 있는 사람들 프로필도 전부 같이 바뀝니다.
        changed = 0
        for owned in data.get("achievements", {}).values():
            if old in owned:
                owned[owned.index(old)] = new_name
                changed += 1

        self._save_profile_data(data)
        await interaction.response.send_message(
            f"✏️ 업적 내용을 수정했어요.\n└ `{old}` ➡️ `{new_name}` (갖고 있던 **{changed}명**에게도 반영됐어요)",
            ephemeral=True,
        )

    @profile_setting_group.command(name="업적목록삭제", description="[관리자] 업적을 목록에서 없앱니다. 갖고 있는 사람들에게서도 전부 회수돼요.")
    @app_commands.describe(내용="목록에서 없앨 업적")
    @app_commands.autocomplete(내용=achievement_catalog_autocomplete)
    async def delete_achievement_from_catalog(self, interaction: discord.Interaction, 내용: str):
        if not self._is_profile_admin(interaction):
            return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)

        name = 내용.strip()
        data = self._load_profile_data()
        if name not in data["achievement_catalog"]:
            return await interaction.response.send_message(f"❌ `{name}` 업적이 목록에 없어요.", ephemeral=True)

        data["achievement_catalog"].remove(name)

        removed = 0
        for user_id_str in list(data.get("achievements", {})):
            owned = data["achievements"][user_id_str]
            if name in owned:
                owned.remove(name)
                removed += 1
            if not owned:
                del data["achievements"][user_id_str]

        self._save_profile_data(data)
        await interaction.response.send_message(
            f"🗑️ `{name}` 업적을 목록에서 삭제하고, 갖고 있던 **{removed}명**에게서도 회수했어요.",
            ephemeral=True,
        )

    @profile_setting_group.command(name="사진", description="[관리자] 유저의 프로필 사진을 등록/교체합니다. (첨부파일로 업로드)")
    @app_commands.describe(유저="대상 유저", 사진="프로필에 표시할 이미지 파일 (png/jpg/gif/webp, 8MB 이하)")
    async def set_profile_photo(self, interaction: discord.Interaction, 유저: discord.Member, 사진: discord.Attachment):
        if not self._is_profile_admin(interaction):
            return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)

        ext = PHOTO_EXTENSIONS.get((사진.content_type or "").split(";")[0].strip())
        if not ext:
            return await interaction.response.send_message(
                "❌ png · jpg · gif · webp 이미지만 등록할 수 있어요.", ephemeral=True
            )
        if 사진.size > PHOTO_MAX_BYTES:
            return await interaction.response.send_message(
                f"❌ 사진이 너무 커요. ({사진.size / 1024 / 1024:.1f}MB) "
                f"{PHOTO_MAX_BYTES // 1024 // 1024}MB 이하로 올려주세요.\n"
                f"└ 프로필을 열 때마다 이 파일을 함께 올려야 해서, 크면 조회가 느려져요.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        user_id_str = str(유저.id)
        try:
            raw = await 사진.read()
            os.makedirs(PROFILE_PHOTO_DIR, exist_ok=True)
            # 확장자가 바뀌는 경우가 있어서, 새로 쓰기 전에 예전 파일부터 정리해요.
            self._delete_photo_files(user_id_str)
            with open(self._photo_path(f"{user_id_str}{ext}"), "wb") as f:
                f.write(raw)
        except Exception as e:
            print(f"❗ 프로필 사진 저장 실패: {type(e).__name__}: {e}")
            return await interaction.followup.send(
                f"❌ 사진을 저장하지 못했어요. ({type(e).__name__}) 잠시 후 다시 시도해 주세요.", ephemeral=True
            )

        data = self._load_profile_data()
        data["photos"][user_id_str] = {"file": f"{user_id_str}{ext}"}
        self._save_profile_data(data)
        await interaction.followup.send(
            f"✅ {유저.mention}님의 프로필 사진을 등록했어요. (봇 서버에 보관해서 만료되지 않아요)",
            ephemeral=True,
        )

    @profile_setting_group.command(name="사진삭제", description="[관리자] 유저의 프로필 사진을 삭제합니다. (이후 디스코드 기본 아바타로 표시)")
    @app_commands.describe(유저="대상 유저")
    async def remove_profile_photo(self, interaction: discord.Interaction, 유저: discord.Member):
        if not self._is_profile_admin(interaction):
            return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)
        data = self._load_profile_data()
        user_id_str = str(유저.id)
        if user_id_str not in data["photos"]:
            return await interaction.response.send_message("❌ 등록된 프로필 사진이 없어요.", ephemeral=True)
        del data["photos"][user_id_str]
        self._save_profile_data(data)
        # 목록에서 지우는 것뿐 아니라 보관해둔 원본 파일도 같이 지워요.
        self._delete_photo_files(user_id_str)
        await interaction.response.send_message(f"🗑️ {유저.mention}님의 프로필 사진을 삭제했어요.", ephemeral=True)
