"""MariSetting — 채널/관리자 역할 동적 설정, 기능 정지·재개, 역할 부여."""

import datetime as dt
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands

from mari_config import EXILE_DATA_FILE, KST, ONBOARDING_PRESETS, TOWN_GUIDE_ROLE_ID
from mari_storage import atomic_json_save_or_raise, safe_json_load
from mari_utils import MariView
from mari_settings import FEATURE_KEYS, FEATURE_LIST_TEXT, _get_role_ids, has_admin_or_role, is_super_admin, load_settings, save_settings, send_log_embed, set_all_features_enabled, set_feature_enabled

class RoleGrantView(MariView):
    """/역할부여에서 가이드 프리셋 없이 직접 역할을 고를 때 쓰는 뷰.
    디스코드 자체 RoleSelect를 써서 한 번에 최대 25개(디스코드 자체 한도)까지 선택할 수 있어요.
    (예전엔 역할1~역할5, 딱 5개짜리 파라미터로 하드코딩되어 있었어요)"""
    def __init__(self, setting_cog, member: discord.Member):
        super().__init__(timeout=120)
        self.setting_cog = setting_cog
        self.member = member
        self.processing = False  # 🔒 연타로 역할 부여가 두 번 실행되는 것 방지
        select = discord.ui.RoleSelect(placeholder="부여할 역할을 선택하세요 (최대 25개)", min_values=1, max_values=25)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        if self.processing:
            return await interaction.response.send_message("⏳ 이미 역할을 적용하는 중이에요! 잠시만요.", ephemeral=True)
        self.processing = True

        select: discord.ui.RoleSelect = self.children[0]
        roles = select.values
        await interaction.response.defer(ephemeral=True)
        for child in self.children:
            child.disabled = True
        try:
            await interaction.edit_original_response(content=f"{self.member.mention}님에게 {len(roles)}개 역할을 적용하는 중...", view=self)
        except Exception:
            pass
        await self.setting_cog._apply_role_changes(interaction, self.member, list(roles), None)


# 🧭 [신규] 입주 절차 프리셋(/역할부여의 "가이드") 선택지를 guild.json에서 만들어요.
# 예전엔 프리셋 2종과 그 안에서 주고받을 역할 ID가 전부 코드에 박혀 있어서, 서버마다
# 다른 입주 절차를 담으려면 코드를 고쳐야 했습니다.
_GUIDE_CHOICES = [
    app_commands.Choice(name=preset["label"], value=key)
    for key, preset in ONBOARDING_PRESETS.items()
]


def _with_guide_choices(func):
    """프리셋이 하나라도 설정돼 있을 때만 '가이드' 선택지를 답니다.

    ⚠️ 빈 리스트를 넘기면 디스코드가 명령어 등록을 거부해서 동기화 전체가 실패해요.
    입주 절차를 안 쓰는 서버에서는 선택지 없이 두면 됩니다. (프리셋이 없으면 아래
    _apply_role_changes도 자연스럽게 아무것도 안 해요)
    """
    return app_commands.choices(가이드=_GUIDE_CHOICES)(func) if _GUIDE_CHOICES else func


class MariSetting(commands.Cog):
    """봇의 모든 권한 역할 및 채널을 동적으로 변경하는 마리 설정 시스템"""
    def __init__(self, bot):
        self.bot = bot

    설정 = app_commands.Group(name="설정", description="마리봇의 채널 및 관리자 역할을 동적 관리합니다.")
    관리자 = app_commands.Group(parent=설정, name="관리자", description="기능별 전용 관리자 역할을 설정합니다.")
    채널 = app_commands.Group(parent=설정, name="채널", description="봇 알림 및 로그 채널들을 지정합니다.")
    레벨 = app_commands.Group(parent=설정, name="레벨", description="레벨별 아이디 명단(로스터) 자동 관리 설정입니다.")

    # 🚧 [신규] 기능 정지/재개. 다른 /설정 명령어들과 다르게 일부러 '채널관리자' 역할로는
    # 못 쓰게 하고, 진짜 서버 관리자 또는 대장(chief_role) 역할만 쓸 수 있게 엄격히 제한했어요.
    기능제어 = app_commands.Group(name="기능제어", description=f"[관리자] {FEATURE_LIST_TEXT} 기능을 정지·재개합니다. (서버 관리자 또는 대장 전용)", guild_only=True)

    FEATURE_CHOICES = [app_commands.Choice(name=label, value=key) for label, key in FEATURE_KEYS.items()]

    @기능제어.command(name="정지", description="[관리자] 특정 기능을 일시 정지합니다. (서버 관리자 또는 대장 전용)")
    @app_commands.describe(기능="정지할 기능")
    @app_commands.choices(기능=FEATURE_CHOICES)
    async def stop_feature(self, interaction: discord.Interaction, 기능: app_commands.Choice[str]):
        if not is_super_admin(interaction):
            return await interaction.response.send_message("⛔ 서버 관리자 또는 대장 역할만 사용할 수 있어요.", ephemeral=True)
        set_feature_enabled(기능.value, False)
        await interaction.response.send_message(f"🚧 **{기능.name}** 기능을 정지했어요. (`/기능제어 재개`로 다시 켤 수 있어요)", ephemeral=False)

    @기능제어.command(name="재개", description="[관리자] 정지된 특정 기능을 다시 켭니다. (서버 관리자 또는 대장 전용)")
    @app_commands.describe(기능="재개할 기능")
    @app_commands.choices(기능=FEATURE_CHOICES)
    async def resume_feature(self, interaction: discord.Interaction, 기능: app_commands.Choice[str]):
        if not is_super_admin(interaction):
            return await interaction.response.send_message("⛔ 서버 관리자 또는 대장 역할만 사용할 수 있어요.", ephemeral=True)
        set_feature_enabled(기능.value, True)
        await interaction.response.send_message(f"✅ **{기능.name}** 기능을 다시 켰어요.", ephemeral=False)

    # 🔤 [버그 수정] 예전엔 `{FEATURE_LIST_TEXT}를 전부…`였어요. 손으로 나열하던 시절엔 목록이
    # 항상 "…/위키"로 끝나서 받침 없는 '위키' + '를'이 맞았는데, 목록 끝에 '나중에답장'(받침 ㅇ)이
    # 붙으면서 "…/나중에답장를"이 됐습니다. 목록 마지막 글자에 따라 조사가 달라지니, 아예 조사가
    # 목록에 붙지 않게 위 그룹 설명과 똑같이 '기능을'을 사이에 끼웠어요. 이제 뭘 추가해도 안전합니다.
    @기능제어.command(name="전체정지", description=f"[관리자] {FEATURE_LIST_TEXT} 기능을 전부 한 번에 정지합니다. (서버 관리자 또는 대장 전용)")
    async def stop_all_features(self, interaction: discord.Interaction):
        if not is_super_admin(interaction):
            return await interaction.response.send_message("⛔ 서버 관리자 또는 대장 역할만 사용할 수 있어요.", ephemeral=True)
        set_all_features_enabled(False)
        await interaction.response.send_message(f"🚨 **전체 기능을 정지했어요.** ({FEATURE_LIST_TEXT})\n`/기능제어 전체재개`로 한 번에 다시 켤 수 있어요.", ephemeral=False)

    @기능제어.command(name="전체재개", description="[관리자] 정지된 모든 기능을 한 번에 다시 켭니다. (서버 관리자 또는 대장 전용)")
    async def resume_all_features(self, interaction: discord.Interaction):
        if not is_super_admin(interaction):
            return await interaction.response.send_message("⛔ 서버 관리자 또는 대장 역할만 사용할 수 있어요.", ephemeral=True)
        set_all_features_enabled(True)
        await interaction.response.send_message(f"✅ **전체 기능을 다시 켰어요.** ({FEATURE_LIST_TEXT})", ephemeral=False)

    @기능제어.command(name="상태", description="지금 어떤 기능이 정지되어 있는지 확인합니다.")
    async def feature_status(self, interaction: discord.Interaction):
        # 🐛 [성능 버그 수정] 예전엔 기능 7개를 확인할 때마다 is_feature_enabled()가 매번
        # 파일을 새로 읽어서, 7개 확인하는 데 파일을 7번 열었어요. 이제 한 번만 읽어서 재사용해요.
        status = load_settings().get("feature_status", {})
        lines = []
        for label, key in FEATURE_KEYS.items():
            enabled = status.get(key, True)
            lines.append(f"{'🟢' if enabled else '🔴'} {label}: {'정상' if enabled else '정지됨'}")
        embed = discord.Embed(title="🚧 기능 상태", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @레벨.command(name="대장", description="[관리자] 레벨 명단에서 '대장' 칸으로 따로 표시할 역할을 지정합니다.")
    async def set_chief_role(self, interaction: discord.Interaction, 역할: discord.Role):
        if not self.has_channel_permission(interaction): return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)
        settings = load_settings()
        if "roles" not in settings: settings["roles"] = {}
        settings["roles"]["chief_role"] = 역할.id
        save_settings(settings)
        await interaction.response.send_message(f"✅ {역할.mention} 역할이 레벨 명단의 **대장** 칸으로 지정됐어요.", ephemeral=True)

    def has_channel_permission(self, interaction: discord.Interaction) -> bool:
        """최고 관리자 권한이 있거나 '채널관리자' 역할을 가지고 있는지 검사하는 헬퍼 함수"""
        if interaction.user.guild_permissions.administrator:
            return True
        return any(role.name == "채널관리자" for role in interaction.user.roles)

    # 🔄 [개선] 예전엔 역할을 새로 지정하면 이전 역할이 통째로 덮어써져서, 한 기능에
    # 역할을 하나밖에 못 줬어요. 이제는 "추가/제거" 방식으로 바뀌어서, 같은 기능에
    # 여러 역할을 동시에 부여할 수 있어요.
    ADMIN_ROLE_ACTION_CHOICES = [
        app_commands.Choice(name="추가", value="add"),
        app_commands.Choice(name="제거", value="remove"),
    ]

    async def _update_admin_role_list(self, interaction: discord.Interaction, role_key: str, label: str, 역할: discord.Role, 동작: Optional[app_commands.Choice[str]]):
        if not self.has_channel_permission(interaction):
            return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)

        settings = load_settings()
        if "roles" not in settings: settings["roles"] = {}
        role_ids = _get_role_ids(settings, role_key)
        action = 동작.value if 동작 else "add"

        if action == "remove":
            if 역할.id not in role_ids:
                return await interaction.response.send_message(f"❌ {역할.mention} 역할은 원래 **{label}**가 아니었어요.", ephemeral=True)
            role_ids.remove(역할.id)
            msg = f"🗑️ {역할.mention} 역할을 **{label}**에서 제거했어요."
        else:
            if 역할.id in role_ids:
                return await interaction.response.send_message(f"❌ {역할.mention} 역할은 이미 **{label}**예요.", ephemeral=True)
            role_ids.append(역할.id)
            msg = f"✅ {역할.mention} 역할을 **{label}**로 추가했어요."

        settings["roles"][role_key] = role_ids
        save_settings(settings)

        if role_ids:
            current = ", ".join(f"<@&{rid}>" for rid in role_ids)
            msg += f"\n현재 **{label}**: {current}"
        await interaction.response.send_message(msg, ephemeral=True)

    @관리자.command(name="아이디", description="[관리자] 아이디 관리자(등록/수정) 역할을 추가/제거합니다. (여러 역할 동시 지정 가능)")
    @app_commands.describe(역할="추가/제거할 역할", 동작="추가 또는 제거 (기본값: 추가)")
    @app_commands.choices(동작=ADMIN_ROLE_ACTION_CHOICES)
    async def set_ids_admin(self, interaction: discord.Interaction, 역할: discord.Role, 동작: Optional[app_commands.Choice[str]] = None):
        await self._update_admin_role_list(interaction, "ids_admin", "아이디 관리자", 역할, 동작)

    @관리자.command(name="상점", description="[관리자] 상점 관리자 역할을 추가/제거합니다. (여러 역할 동시 지정 가능)")
    @app_commands.describe(역할="추가/제거할 역할", 동작="추가 또는 제거 (기본값: 추가)")
    @app_commands.choices(동작=ADMIN_ROLE_ACTION_CHOICES)
    async def set_shop_admin(self, interaction: discord.Interaction, 역할: discord.Role, 동작: Optional[app_commands.Choice[str]] = None):
        await self._update_admin_role_list(interaction, "shop_admin", "상점 관리자", 역할, 동작)

    @관리자.command(name="주식", description="[관리자] 주식 관리자 역할을 추가/제거합니다. (여러 역할 동시 지정 가능)")
    @app_commands.describe(역할="추가/제거할 역할", 동작="추가 또는 제거 (기본값: 추가)")
    @app_commands.choices(동작=ADMIN_ROLE_ACTION_CHOICES)
    async def set_stock_admin(self, interaction: discord.Interaction, 역할: discord.Role, 동작: Optional[app_commands.Choice[str]] = None):
        await self._update_admin_role_list(interaction, "stock_admin", "주식 관리자", 역할, 동작)

    @관리자.command(name="프로필", description="[관리자] 프로필 관리자(레벨/소속/직책/업적/사진 편집 권한) 역할을 추가/제거합니다. (여러 역할 동시 지정 가능)")
    @app_commands.describe(역할="추가/제거할 역할", 동작="추가 또는 제거 (기본값: 추가)")
    @app_commands.choices(동작=ADMIN_ROLE_ACTION_CHOICES)
    async def set_profile_admin(self, interaction: discord.Interaction, 역할: discord.Role, 동작: Optional[app_commands.Choice[str]] = None):
        await self._update_admin_role_list(interaction, "profile_admin", "프로필 관리자", 역할, 동작)

    @관리자.command(name="에바시", description="[관리자] 에바시 이벤트 보상 설정 권한(에바시 관리자) 역할을 추가/제거합니다. (여러 역할 동시 지정 가능)")
    @app_commands.describe(역할="추가/제거할 역할", 동작="추가 또는 제거 (기본값: 추가)")
    @app_commands.choices(동작=ADMIN_ROLE_ACTION_CHOICES)
    async def set_evashi_admin(self, interaction: discord.Interaction, 역할: discord.Role, 동작: Optional[app_commands.Choice[str]] = None):
        await self._update_admin_role_list(interaction, "evashi_admin", "에바시 관리자", 역할, 동작)

    @관리자.command(name="연대기", description="[관리자] 서버 연대기를 기록·조회할 수 있는 역할을 추가/제거합니다. (여러 역할 동시 지정 가능)")
    @app_commands.describe(역할="추가/제거할 역할", 동작="추가 또는 제거 (기본값: 추가)")
    @app_commands.choices(동작=ADMIN_ROLE_ACTION_CHOICES)
    async def set_chronicle_admin(self, interaction: discord.Interaction, 역할: discord.Role, 동작: Optional[app_commands.Choice[str]] = None):
        await self._update_admin_role_list(interaction, "chronicle_admin", "연대기 관리자", 역할, 동작)

    @관리자.command(name="테스트", description="[관리자] /테스트 그룹(진단 도구) 사용 권한을 가질 역할을 추가/제거합니다. (여러 역할 동시 지정 가능)")
    @app_commands.describe(역할="추가/제거할 역할", 동작="추가 또는 제거 (기본값: 추가)")
    @app_commands.choices(동작=ADMIN_ROLE_ACTION_CHOICES)
    async def set_test_admin(self, interaction: discord.Interaction, 역할: discord.Role, 동작: Optional[app_commands.Choice[str]] = None):
        await self._update_admin_role_list(interaction, "test_admin", "테스트 관리자", 역할, 동작)

    @관리자.command(name="추방관", description="[관리자] /역할부여의 '유배' 처리를 쓸 수 있는 역할을 추가/제거합니다. (여러 역할 동시 지정 가능)")
    @app_commands.describe(역할="추가/제거할 역할", 동작="추가 또는 제거 (기본값: 추가)")
    @app_commands.choices(동작=ADMIN_ROLE_ACTION_CHOICES)
    async def set_exile_officer(self, interaction: discord.Interaction, 역할: discord.Role, 동작: Optional[app_commands.Choice[str]] = None):
        await self._update_admin_role_list(interaction, "exile_officer", "추방관", 역할, 동작)

    @관리자.command(name="지옥간수", description="[관리자] /역할부여의 '복귀'(사면) 처리를 쓸 수 있는 역할을 추가/제거합니다. (여러 역할 동시 지정 가능)")
    @app_commands.describe(역할="추가/제거할 역할", 동작="추가 또는 제거 (기본값: 추가)")
    @app_commands.choices(동작=ADMIN_ROLE_ACTION_CHOICES)
    async def set_hell_warden(self, interaction: discord.Interaction, 역할: discord.Role, 동작: Optional[app_commands.Choice[str]] = None):
        await self._update_admin_role_list(interaction, "hell_warden", "지옥간수", 역할, 동작)

    @관리자.command(name="유배자", description="[관리자] '유배' 처리된 유저에게 부여할 역할을 지정합니다. (권한 역할이 아니라 대상 역할이에요)")
    async def set_exile_role(self, interaction: discord.Interaction, 역할: discord.Role):
        if not self.has_channel_permission(interaction): return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)
        settings = load_settings()
        if "roles" not in settings: settings["roles"] = {}
        settings["roles"]["exile_role"] = 역할.id
        save_settings(settings)
        await interaction.response.send_message(f"⛓️ {역할.mention} 역할이 **유배자** 역할로 지정됐어요. (`/역할부여`의 '유배' 처리 시 이 역할만 남아요)", ephemeral=True)

    @채널.command(name="출석", description="[관리자] 출석체크가 진행될 채널을 고정합니다.")
    async def set_attendance_ch(self, interaction: discord.Interaction, 채널: discord.TextChannel):
        if not self.has_channel_permission(interaction): return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)
        settings = load_settings()
        if "channels" not in settings: settings["channels"] = {}
        settings["channels"]["attendance"] = 채널.id
        save_settings(settings)
        await interaction.response.send_message(f"📌 출석체크 전용 채널이 {채널.mention}로 설정됐어요.", ephemeral=True)

    @채널.command(name="경제로그", description="[관리자] 에바 지급/회수 등이 남는 로그 채널이에요.")
    async def set_economy_log_ch(self, interaction: discord.Interaction, 채널: discord.TextChannel):
        if not self.has_channel_permission(interaction): return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)
        settings = load_settings()
        if "channels" not in settings: settings["channels"] = {}
        settings["channels"]["economy_log"] = 채널.id
        save_settings(settings)
        await interaction.response.send_message(f"📌 경제 로그 채널이 {채널.mention}로 설정됐어요.", ephemeral=True)

    @채널.command(name="상점로그", description="[관리자] 상점 구매 영수증이 남는 로그 채널이에요.")
    async def set_shop_log_ch(self, interaction: discord.Interaction, 채널: discord.TextChannel):
        if not self.has_channel_permission(interaction): return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)
        settings = load_settings()
        if "channels" not in settings: settings["channels"] = {}
        settings["channels"]["shop_log"] = 채널.id
        save_settings(settings)
        await interaction.response.send_message(f"📌 상점 영수증 로그 채널이 {채널.mention}로 설정됐어요.", ephemeral=True)

    @채널.command(name="주식전광판", description="[관리자] 실시간 주식 전광판 임베드가 고정될 채널이에요.")
    async def set_stock_board_ch(self, interaction: discord.Interaction, 채널: discord.TextChannel):
        if not self.has_channel_permission(interaction): return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)
        settings = load_settings()
        if "channels" not in settings: settings["channels"] = {}
        settings["channels"]["stock_board"] = 채널.id
        save_settings(settings)
        await interaction.response.send_message(f"📌 주식 전광판 게시 채널이 {채널.mention}로 설정됐어요.", ephemeral=True)

    @채널.command(name="주식로그", description="[관리자] 주식 매수/매도 등의 기록이 남는 로그 채널이에요.")
    async def set_stock_log_ch(self, interaction: discord.Interaction, 채널: discord.TextChannel):
        if not self.has_channel_permission(interaction): return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)
        settings = load_settings()
        if "channels" not in settings: settings["channels"] = {}
        settings["channels"]["stock_log"] = 채널.id
        save_settings(settings)
        await interaction.response.send_message(f"📌 주식 로그 채널이 {채널.mention}로 설정됐어요.", ephemeral=True)
        
    @채널.command(name="종가게시판", description="[관리자] 장마감 후 종가, 주식 목록, 주주 목록이 적힐 채널이에요.")
    async def set_closing_board_ch(self, interaction: discord.Interaction, 채널: discord.TextChannel):
        if not self.has_channel_permission(interaction): return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)
        settings = load_settings()
        if "channels" not in settings: settings["channels"] = {}
        settings["channels"]["closing_log"] = 채널.id
        save_settings(settings)
        await interaction.response.send_message(f"📌 마감 종가 게시 채널이 {채널.mention}로 설정됐어요.", ephemeral=True)

    @채널.command(name="에바시", description="[관리자] 에바시 이벤트 선착순 마감 안내가 올라갈 채널이에요. (유저가 '에바시'를 치는 채널이 아니라, 마리가 결과를 알려주는 전용 채널이에요!)")
    async def set_evashi_announce_ch(self, interaction: discord.Interaction, 채널: discord.TextChannel):
        if not self.has_channel_permission(interaction): return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)
        settings = load_settings()
        if "channels" not in settings: settings["channels"] = {}
        settings["channels"]["evashi_announce"] = 채널.id
        save_settings(settings)
        await interaction.response.send_message(f"📌 에바시 선착순 마감 안내 채널이 {채널.mention}로 설정됐어요. (유저는 여전히 아무 채널에서나 '에바시'를 칠 수 있어요)", ephemeral=True)

    @채널.command(name="고확", description="[관리자] 고성능 확성기 사용 채널이에요.")
    async def set_global_broadcast_ch(self, interaction: discord.Interaction, 채널: discord.TextChannel):
        if not self.has_channel_permission(interaction): return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)
        settings = load_settings()
        if "channels" not in settings: settings["channels"] = {}
        settings["channels"]["global_broadcast"] = 채널.id
        save_settings(settings)
        await interaction.response.send_message(f"📌 전역 고확 채널이 {채널.mention}로 설정됐어요.", ephemeral=True)

    @채널.command(name="아이디로그", description="[관리자] 아이디 등록/수정/삭제 시 로그가 남는 채널이에요.")
    async def set_id_log_ch(self, interaction: discord.Interaction, 채널: discord.TextChannel):
        if not self.has_channel_permission(interaction): return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)
        settings = load_settings()
        if "channels" not in settings: settings["channels"] = {}
        settings["channels"]["id_log"] = 채널.id
        save_settings(settings)
        await interaction.response.send_message(f"📌 아이디 관리 로그 채널이 {채널.mention}로 설정됐어요.", ephemeral=True)

    @채널.command(name="아이디목록", description="[관리자] 레벨별 아이디 명단이 자동으로 갱신되어 올라갈 채널이에요.")
    async def set_level_roster_ch(self, interaction: discord.Interaction, 채널: discord.TextChannel):
        if not self.has_channel_permission(interaction): return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)
        settings = load_settings()
        if "channels" not in settings: settings["channels"] = {}
        settings["channels"]["level_roster"] = 채널.id
        save_settings(settings)
        await interaction.response.send_message(
            f"📌 레벨별 아이디 명단 채널이 {채널.mention}로 설정됐어요. "
            f"이제 아이디가 등록/수정/삭제될 때마다 이 채널의 명단이 자동으로 갱신돼요.",
            ephemeral=True
        )

    @채널.command(name="아이디등록", description="[관리자] 유저들이 '플랫폼 아이디' 형식으로 올리면 마리가 자동으로 등록해주는 채널이에요.")
    async def set_id_submit_ch(self, interaction: discord.Interaction, 채널: discord.TextChannel):
        if not self.has_channel_permission(interaction): return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)
        settings = load_settings()
        if "channels" not in settings: settings["channels"] = {}
        settings["channels"]["id_submit"] = 채널.id
        save_settings(settings)
        await interaction.response.send_message(
            f"📌 아이디 자동등록 채널이 {채널.mention}로 설정됐어요.\n"
            f"이제 이 채널에 `플랫폼 아이디` 형식(예: `라이엇 만해#kr1`)으로 올리면 마리가 자동으로 등록하고 원본 메세지는 삭제해요.\n"
            f"플랫폼을 못 알아보면 `/설정 관리자 아이디`로 지정된 분들께 '아이디 로그' 채널에서 확인을 받아요. (`/설정 채널 아이디로그`로 지정 필요)",
            ephemeral=True
        )

    @채널.command(name="역할로그", description="[관리자] 역할 부여 내역이 남는 로그 채널이에요.")
    async def set_role_log_ch(self, interaction: discord.Interaction, 채널: discord.TextChannel):
        if not self.has_channel_permission(interaction): return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)
        settings = load_settings()
        if "channels" not in settings: settings["channels"] = {}
        settings["channels"]["role_log"] = 채널.id
        save_settings(settings)
        await interaction.response.send_message(f"📌 역할 관리 로그 채널이 {채널.mention}로 설정됐어요.", ephemeral=True)
        
    @채널.command(name="생일알림", description="[관리자] 생일 축하 메시지가 올라갈 채널을 지정합니다.")
    async def set_birthday_announce(self, interaction: discord.Interaction, 채널: discord.TextChannel):
        if not self.has_channel_permission(interaction): 
            return await interaction.response.send_message("❌ 권한이 없어요.", ephemeral=True)
        settings = load_settings() # 기존 클래스의 파일 로드 함수
        if "channels" not in settings: settings["channels"] = {}
        settings["channels"]["birthday_announce"] = 채널.id
        save_settings(settings) # 기존 클래스의 파일 저장 함수
        await interaction.response.send_message(f"📢 생일 알림 채널이 {채널.mention}(으)로 지정됐어요.")

    @채널.command(name="생일로그", description="[관리자] 생일 등록/변경/삭제 로그가 기록될 채널을 지정합니다.")
    async def set_birthday_log(self, interaction: discord.Interaction, 채널: discord.TextChannel):
        if not self.has_channel_permission(interaction): 
            return await interaction.response.send_message("❌ 권한이 없어요.", ephemeral=True)
        settings = load_settings()
        if "channels" not in settings: settings["channels"] = {}
        settings["channels"]["birthday_log"] = 채널.id
        save_settings(settings)
        await interaction.response.send_message(f"📜 생일 로그 채널이 {채널.mention}(으)로 지정됐어요.")

    # 📋 [추가] 현재 지정된 모든 채널 및 관리자 역할 설정을 한눈에 조회
    @설정.command(name="채널지정내역", description="[관리자] 현재 지정된 모든 채널 및 관리자 역할 설정 내역을 보여줍니다.")
    @app_commands.guild_only()
    async def show_channel_config(self, interaction: discord.Interaction):
        if not self.has_channel_permission(interaction):
            return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)

        settings = load_settings()
        channels = settings.get("channels", {})
        roles = settings.get("roles", {})

        channel_labels = {
            "attendance": "📅 출석체크",
            "economy_log": "💰 경제 로그",
            "shop_log": "🛒 상점 로그",
            "stock_board": "📈 주식 전광판",
            "stock_log": "📊 주식 로그",
            "closing_log": "🔔 종가 게시판",
            "global_broadcast": "📢 고확",
            "id_log": "🆔 아이디 로그",
            "role_log": "👥 역할 로그",
            "birthday_announce": "🎂 생일 알림",
            "birthday_log": "🎉 생일 로그",
            "evashi_announce": "🎉 에바시 안내",
            "id_submit": "🆔 아이디 자동등록",
            "level_roster": "📋 레벨별 아이디 명단",
        }
        role_labels = {
            "ids_admin": "🆔 아이디 관리자",
            "shop_admin": "🛒 상점 관리자",
            "stock_admin": "📈 주식 관리자",
            "profile_admin": "👤 프로필 관리자",
            "evashi_admin": "🎉 에바시 관리자",
            "chronicle_admin": "📜 연대기 관리자",
            "chief_role": "👑 대장 (레벨 명단용)",
            "test_admin": "🧪 테스트 관리자",
            "hell_warden": "😈 지옥간수 (유배 복귀/사면 권한)",
            "exile_officer": "🏹 추방관 (유배 처리 권한)",
            "exile_role": "⛓️ 유배자 (유배 대상 역할)",
        }

        embed = discord.Embed(
            title="⚙️ 마리봇 채널/역할 지정 내역",
            color=0x5CE6B4,
            timestamp=dt.datetime.now(KST)
        )

        ch_lines = []
        for key, label in channel_labels.items():
            cid = channels.get(key)
            if cid:
                ch = interaction.guild.get_channel(cid)
                value = ch.mention if ch else f"<#{cid}> ⚠️(채널을 찾을 수 없음)"
            else:
                value = "❌ 미설정"
            ch_lines.append(f"{label}: {value}")
        embed.add_field(name="📁 채널 설정", value="\n".join(ch_lines), inline=False)

        role_lines = []
        for key, label in role_labels.items():
            if key in ("chief_role", "exile_role"):
                # 👑⛓️ 대장/유배자 역할은 "누가 쓸 수 있는지"가 아니라 "어느 역할을 가리키는지"라
                # 지금처럼 단일 역할로 유지해요.
                rid = roles.get(key)
                if rid:
                    role = interaction.guild.get_role(rid)
                    value = role.mention if role else f"<@&{rid}> ⚠️(역할을 찾을 수 없음)"
                else:
                    value = "❌ 미설정"
            else:
                role_ids = _get_role_ids({"roles": roles}, key)
                if role_ids:
                    mentions = []
                    for rid in role_ids:
                        role = interaction.guild.get_role(rid)
                        mentions.append(role.mention if role else f"<@&{rid}> ⚠️")
                    value = ", ".join(mentions)
                else:
                    value = "❌ 미설정"
            role_lines.append(f"{label}: {value}")
        embed.add_field(name="🛡️ 관리자 역할 설정", value="\n".join(role_lines), inline=False)

        embed.set_footer(text="지정/변경은 /설정 채널, /설정 관리자 명령어로 가능합니다.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="감사로그", description="[관리자] 경제/상점/아이디/역할/주식/생일 로그 채널을 한 번에 모아 최신순으로 보여줍니다.")
    @app_commands.describe(개수="가져올 최대 항목 수 (기본 20, 최대 50)")
    @app_commands.guild_only()
    async def audit_log(self, interaction: discord.Interaction, 개수: int = 20):
        if not self.has_channel_permission(interaction):
            return await interaction.response.send_message("❌ 관리자 또는 '채널관리자' 역할이 필요해요!", ephemeral=True)
        개수 = max(1, min(개수, 50))
        await interaction.response.defer(ephemeral=True)

        # 📡 [통합] 흩어져있는 로그 채널들을 한 번에 모아서 시간순으로 섞어 보여줘요.
        # (별도 저장소를 새로 만들지 않고, 이미 있는 로그 채널들의 메세지 기록을 그대로 재사용해요)
        log_keys = {
            "economy_log": "💰 경제", "shop_log": "🛒 상점", "id_log": "🆔 아이디",
            "role_log": "👥 역할", "stock_log": "📈 주식", "birthday_log": "🎉 생일",
        }
        settings = load_settings()
        channels = settings.get("channels", {})

        collected = []
        for key, label in log_keys.items():
            ch_id = channels.get(key)
            if not ch_id:
                continue
            channel = interaction.guild.get_channel(ch_id)
            if not channel:
                continue
            try:
                async for msg in channel.history(limit=개수):
                    if not msg.author.bot:
                        continue
                    if msg.embeds:
                        emb = msg.embeds[0]
                        summary = emb.title or (emb.description[:100] if emb.description else "(내용 없음)")
                    else:
                        summary = msg.content[:100] if msg.content else "(내용 없음)"
                    collected.append((msg.created_at, label, summary))
            except Exception as e:
                print(f"⚠️ 감사로그: {label} 채널 조회 실패: {e}")

        if not collected:
            return await interaction.followup.send("ℹ️ 표시할 로그가 없어요. 로그 채널들이 지정돼있는지 확인해주세요.", ephemeral=True)

        collected.sort(key=lambda x: x[0], reverse=True)
        collected = collected[:개수]

        lines = []
        for created_at, label, summary in collected:
            ts = f"<t:{int(created_at.timestamp())}:R>"
            lines.append(f"{ts} `{label}` {summary}")

        embed = discord.Embed(
            title="📋 통합 감사 로그",
            description="\n".join(lines)[:4000],
            color=discord.Color.dark_teal(),
        )
        embed.set_footer(text=f"최근 {len(collected)}건 · 경제/상점/아이디/역할/주식/생일 로그 통합")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ✨ [가이드 드롭다운 추가 및 전체 옵션 선택형으로 고도화]
    # ✨ [권한 변경] 인사관리자 대신 타운가이드(ID 기준) 권한으로 변경
    @app_commands.command(name="역할부여", description="[관리자] 특정 멤버에게 역할을 지급하거나 가이드/유배지 프리셋을 적용합니다.")
    @app_commands.describe(
        멤버="역할을 부여할 유저", 
        가이드="설정된 가이드 프리셋을 적용합니다 (선택)",
        유배지="'유배'(현재 역할 전부 뺏고 유배자 역할만 부여, 추방관 전용) 또는 '복귀'(원래 역할 복원, 지옥간수 전용)",
    )
    @app_commands.choices(
        유배지=[
            app_commands.Choice(name="유배 (현재 역할 전부 저장 후 제거, 유배자 역할만 부여)", value="exile"),
            app_commands.Choice(name="복귀 (저장해둔 원래 역할 복원, 유배자 역할 제거)", value="return"),
        ],
    )
    @_with_guide_choices
    async def grant_role(
        self, 
        interaction: discord.Interaction, 
        멤버: discord.Member, 
        가이드: Optional[app_commands.Choice[str]] = None,
        유배지: Optional[app_commands.Choice[str]] = None,
    ):
        # ⛓️ [신규] '유배지' 옵션은 완전히 별개의 흐름이라(전체 역할 교체), 다른 옵션들과
        # 섞이지 않게 맨 앞에서 따로 처리하고 끝내요. 유배는 '추방관', 복귀(사면)는 '지옥간수'로
        # 서로 다른 사람이 담당할 수 있게 권한을 분리했어요.
        if 유배지 is not None:
            if 유배지.value == "exile":
                if not (interaction.user.guild_permissions.administrator or has_admin_or_role(interaction, "exile_officer")):
                    return await interaction.response.send_message("❌ 유배 처리는 '추방관' 역할(또는 관리자)만 사용할 수 있어요!", ephemeral=True)
            else:  # "return"
                if not (interaction.user.guild_permissions.administrator or has_admin_or_role(interaction, "hell_warden")):
                    return await interaction.response.send_message("❌ 복귀(사면) 처리는 '지옥간수' 역할(또는 관리자)만 사용할 수 있어요!", ephemeral=True)

            settings = load_settings()
            exile_role_id = settings.get("roles", {}).get("exile_role")
            exile_role = interaction.guild.get_role(exile_role_id) if exile_role_id else None
            if not exile_role:
                return await interaction.response.send_message("❌ 먼저 `/설정 관리자 유배자`로 유배자 역할을 지정해주세요!", ephemeral=True)

            await interaction.response.defer(ephemeral=True)
            exile_data = safe_json_load(EXILE_DATA_FILE, {})
            gid, uid = str(interaction.guild.id), str(멤버.id)

            if 유배지.value == "exile":
                # 현재 보유 역할(진짜 관리 가능한 역할만)을 저장해두고 전부 제거
                #
                # 🐛 [버그 수정] 예전엔 @everyone과 봇보다 높은 역할만 걸렀어요. 그런데 **서버 부스트
                # 역할**(Nitro Booster, 예: 1057226954534834187)은 봇 역할보다 아래에 있어서 이 검사를
                # 통과해 목록에 섞였습니다. 부스트 역할은 디스코드가 직접 관리하는 managed 역할이라
                # 봇이 절대 뗄 수 없고, remove_roles는 넘긴 역할을 한 번에 처리하기 때문에 그거 하나
                # 때문에 호출 전체가 실패해서 **역할이 하나도 안 빠졌어요.**
                # r.managed로 거르면 부스트 역할뿐 아니라 봇 전용 역할·연동(integration) 역할까지
                # 같이 걸러집니다. 전부 같은 이유로 제거가 불가능한 역할들이에요.
                # (이 역할들은 저장도 하지 않아요. 어차피 복귀 때 다시 붙일 수 없고, 유배 중에도
                #  디스코드가 알아서 유지해주는 역할이라 그대로 두는 게 맞습니다)
                skipped = [r for r in 멤버.roles
                           if not r.is_default() and (r.managed or r >= interaction.guild.me.top_role)]
                current_roles = [r for r in 멤버.roles
                                 if not r.is_default() and not r.managed and r < interaction.guild.me.top_role]
                if not current_roles:
                    saved_ids = []
                else:
                    saved_ids = [r.id for r in current_roles]
                    try:
                        await 멤버.remove_roles(*current_roles, reason="유배 처리")
                    except Exception as e:
                        return await interaction.followup.send(f"❌ 기존 역할 제거 중 오류가 발생했어요: {e}")

                exile_data.setdefault(gid, {})[uid] = saved_ids
                atomic_json_save_or_raise(EXILE_DATA_FILE, exile_data, indent=2)

                try:
                    await 멤버.add_roles(exile_role, reason="유배 처리")
                except Exception as e:
                    return await interaction.followup.send(f"⚠️ 기존 역할은 제거했지만, 유배자 역할 부여 중 오류가 발생했어요: {e}")

                msg = f"⛓️ {멤버.mention}님을 유배했어요. (기존 역할 {len(saved_ids)}개 저장 후 제거, {exile_role.mention}만 부여)"
                if skipped:
                    # 뗄 수 없는 역할이 남아있다는 걸 알려야, 유배했는데 왜 저 역할이 그대로인지 헷갈리지 않아요.
                    msg += ("\nℹ️ 아래 역할은 봇이 뗄 수 없어서 그대로 뒀어요 "
                            "(서버 부스트·봇·연동 역할이거나 마리보다 높은 역할):\n"
                            f"└ {', '.join(r.mention for r in skipped)}")
                await interaction.followup.send(msg)
                await send_log_embed(interaction.client, "role_log", "유배 처리 기록이에요.",
                                      fields=[("실행자", interaction.user.mention, True), ("대상자", 멤버.mention, True),
                                              ("저장된 역할 수", f"{len(saved_ids)}개", True)]
                                             + ([("제거 못한 역할", ", ".join(r.name for r in skipped)[:1024], False)] if skipped else []),
                                      guild=interaction.guild)
            else:  # "return"
                saved_ids = exile_data.get(gid, {}).get(uid)
                if saved_ids is None:
                    return await interaction.followup.send(f"❌ {멤버.mention}님은 유배 기록이 없어요.")

                try:
                    if exile_role in 멤버.roles:
                        await 멤버.remove_roles(exile_role, reason="유배 복귀")
                except Exception as e:
                    return await interaction.followup.send(f"❌ 유배자 역할 제거 중 오류가 발생했어요: {e}")

                restored, missing = [], []
                for rid in saved_ids:
                    role = interaction.guild.get_role(rid)
                    if not role:
                        missing.append(str(rid))
                        continue
                    try:
                        await 멤버.add_roles(role, reason="유배 복귀")
                        restored.append(role.mention)
                    except Exception as e:
                        missing.append(f"{role.name} ({e})")

                del exile_data[gid][uid]
                atomic_json_save_or_raise(EXILE_DATA_FILE, exile_data, indent=2)

                msg = f"🕊️ {멤버.mention}님을 복귀시켰어요. 복원된 역할: {', '.join(restored) if restored else '없음'}"
                if missing:
                    msg += f"\n⚠️ 복원 실패(역할이 삭제됐거나 권한 부족): {', '.join(missing)}"
                await interaction.followup.send(msg)
                await send_log_embed(interaction.client, "role_log", "유배 복귀 처리 기록이에요.",
                                      fields=[("실행자", interaction.user.mention, True), ("대상자", 멤버.mention, True),
                                              ("복원된 역할 수", f"{len(restored)}개", True)],
                                      guild=interaction.guild)
            return

        # 1. 권한 검사 (서버 최고 관리자이거나, '타운가이드' 역할 ID를 가지고 있는지 확인)
        is_admin = interaction.user.guild_permissions.administrator
        is_town_guide = any(role.id == TOWN_GUIDE_ROLE_ID for role in interaction.user.roles)

        if not (is_admin or is_town_guide):
            return await interaction.response.send_message("❌ 권한이 없어요! 최고 관리자나 '타운가이드' 역할이 필요합니다.", ephemeral=True)

        # 🐛 [버그 수정] 예전엔 역할1~역할5, 딱 5개까지만 하드코딩된 파라미터로 지급할 수 있었는데
        # 한 사람한테 5개 넘게 줘야 할 때는 명령어를 여러 번 나눠 쳐야 했어요. 이제는 디스코드
        # 자체 역할 선택 UI(RoleSelect)를 써서 최대 25개(디스코드 자체 한도)까지 한 번에 골라서
        # 줄 수 있게 바꿨어요. 가이드 프리셋을 고르면 이 선택 과정 없이 바로 적용돼요.
        if 가이드 is None:
            view = RoleGrantView(self, 멤버)
            return await interaction.response.send_message(
                f"{멤버.mention}님에게 부여할 역할을 아래에서 선택해주세요. (최대 25개까지 한 번에 가능)",
                view=view, ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        await self._apply_role_changes(interaction, 멤버, [], 가이드)

    async def _apply_role_changes(self, interaction: discord.Interaction, 멤버: discord.Member, roles_to_add: list, 가이드: Optional[app_commands.Choice[str]] = None):
        """역할 직접 지급(RoleSelect) 및 가이드 프리셋 적용 + 결과 메세지 + 로그까지 처리하는 공용 로직.
        호출 시점에 interaction이 이미 defer/response 완료된 상태여야 하고, followup으로 응답해요."""
        success_added = []
        success_removed = []
        failed_actions = []

        # 3. 일반 다중 역할 부여 처리
        for 역할 in roles_to_add:
            try:
                await 멤버.add_roles(역할)
                success_added.append(역할.mention)
            except discord.Forbidden:
                failed_actions.append(f"지급 실패: `{역할.name}` (봇 역할 순위 부족)")
            except Exception as e:
                failed_actions.append(f"지급 실패: `{역할.name}` ({e})")

        # 4. 가이드 프리셋 처리 (정직하게 빼고 넣기)
        #
        # 🗑️ [정리] 예전엔 여기가 `if 가이드.value == "guide1": ... elif == "guide2": ...` 로
        # 나뉘어 있었고, 주고받을 역할 ID가 그 안에 숫자로 박혀 있었어요. 프리셋을 하나 더
        # 만들려면 거의 같은 코드를 통째로 복사해야 했고, 실제로 제거/추가 처리가 두 벌
        # 중복돼 있었습니다. 이제 설정에 적힌 프리셋을 그대로 순회해요.
        if 가이드 is not None:
            preset = ONBOARDING_PRESETS.get(가이드.value)
            if preset is None:
                # 설정에서 프리셋을 지웠는데 디스코드에 옛 선택지가 아직 남아있는 경우예요.
                # (명령어 동기화가 퍼지는 데 시간이 걸립니다)
                failed_actions.append(f"`{가이드.name}` 프리셋이 설정에 없어요. 관리자에게 문의해 주세요.")
            else:
                label = preset["label"]

                # 4-1. 정직하게 역할 제거 진행
                for rid in preset["remove"]:
                    role = interaction.guild.get_role(rid)
                    if role and role in 멤버.roles:
                        try:
                            await 멤버.remove_roles(role)
                            success_removed.append(f"`{role.name}`")
                        except discord.Forbidden:
                            failed_actions.append(f"{label} 제거 실패: `{role.name}` (봇 역할 순위 부족)")
                        except Exception as e:
                            failed_actions.append(f"{label} 제거 실패: `{role.name}` ({e})")

                # 4-2. 정직하게 역할 추가 진행
                for rid in preset["add"]:
                    role = interaction.guild.get_role(rid)
                    if not role:
                        failed_actions.append(f"{label} 지급 실패: 서버에서 ID `{rid}` 역할을 찾을 수 없어요.")
                        continue
                    try:
                        await 멤버.add_roles(role)
                        success_added.append(f"{role.mention} *({label})*")
                    except discord.Forbidden:
                        failed_actions.append(f"{label} 지급 실패: `{role.name}` (봇 역할 순위 부족)")
                    except Exception as e:
                        failed_actions.append(f"{label} 지급 실패: `{role.name}` ({e})")

        # 5. 사용한 사람에게만 보여줄 결과 메시지 조합 (ephemeral=True)
        result_msg = []
        if success_added:
            result_msg.append(f"✅ **부여된 역할:** {', '.join(success_added)}")
        if success_removed:
            result_msg.append(f"🗑️ **제거된 역할:** {', '.join(success_removed)}")
        if failed_actions:
            result_msg.append(f"❌ **일부 작업 실패:**\n> " + "\n> ".join(failed_actions))
        if not result_msg:
            result_msg.append("ℹ️ 적용된 변경사항이 없어요.")

        await interaction.followup.send("\n".join(result_msg), ephemeral=True)

        # 6. 변경 성공 로그 남기기 (변동 사항이 있을 때만 로그 전송)
        if success_added or success_removed:
            fields = [("실행자", interaction.user.mention, True), ("대상자", 멤버.mention, True)]
            if 가이드:
                fields.append(("선택 템플릿", f"`{가이드.name}`", False))
            if success_added:
                fields.append(("지급 내역", ", ".join(success_added), False))
            if success_removed:
                fields.append(("제거 내역", ", ".join(success_removed), False))
            await send_log_embed(interaction.client, "role_log", "역할 부여 처리 기록이에요.",
                                  fields=fields, guild=interaction.guild)
              
              
