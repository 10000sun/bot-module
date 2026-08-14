"""MariCore — 게임 아이디 등록/수정/삭제/자동등록 및 아이디 명단 자동 게시."""

import asyncio
import os
import uuid
import datetime as dt
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands

# 📁 개별 파일 상수 9개를 기동 로그에 찍으려고 하나씩 가져왔었는데,
# 이제 json_data_files()가 데이터 파일 전체를 자동으로 모아주므로 필요 없어졌어요.
from mari_config import DATA_DIR, ID_PENDING_FILE, KST, json_data_files
from mari_storage import atomic_json_save_or_raise, safe_json_load
from mari_settings import _get_role_ids, feature_gate, is_feature_enabled, load_settings, member_has_admin_or_role, save_settings, send_log_embed
from mari_state import state
from mari_utils import KNOWN_PLATFORMS, MariView, _looks_like_id_entry, _split_platform_and_id, extract_id_from_mention, find_guild_member_by_name, get_platform_candidates, next_misc_name, next_platform_name, normalize_platform, notify_log, parse_legacy_id_document, respond_modify
from mari_names import bot_name, josa

class ImportConfirmView(MariView):
    """/아이디목록가져오기 미리보기 결과를 실제로 등록할지 확인받는 버튼"""
    def __init__(self, core_cog, guild: discord.Guild, matched: list, unmatched: list = None):
        super().__init__(timeout=300)
        self.core_cog = core_cog
        self.guild = guild
        self.matched = matched      # (name, discord.Member, {platform: id})
        self.unmatched = unmatched or []  # (name, {platform: id})
        self.processing = False     # 🔒 버튼 연타로 같은 아이디가 두 번 등록되는 것 방지

        # 매칭 안 된 이름이 없으면 "연결하기" 버튼은 아예 안 보이게 정리
        if not self.unmatched:
            self.remove_item(self.resolve_unmatched)

    @discord.ui.button(label="✅ 확정 (실제로 등록)", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.processing:
            return await interaction.response.send_message("⏳ 이미 등록을 처리하는 중이에요! 잠시만요.", ephemeral=True)
        self.processing = True

        await interaction.response.defer(ephemeral=True)
        gid = str(self.guild.id)
        registered_people = 0
        registered_entries = 0

        for name, member, ids in self.matched:
            uid = str(member.id)
            for plat_label, value in ids.items():
                self.core_cog._register_single_id(gid, uid, plat_label, value)
                registered_entries += 1
            registered_people += 1

        state.save()
        await self.core_cog._refresh_id_roster(self.guild)

        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)

        skipped_note = f" (연결 안 된 {len(self.unmatched)}명은 이번에 포함 안 됐어요)" if self.unmatched else ""
        await interaction.followup.send(
            f"✅ **{registered_people}명, {registered_entries}건**의 아이디를 등록 완료했어요! 아이디 명단도 갱신했어요.{skipped_note}",
            ephemeral=True
        )

    @discord.ui.button(label="🔗 매칭 안 된 이름 연결하기", style=discord.ButtonStyle.blurple)
    async def resolve_unmatched(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.unmatched:
            return await interaction.response.send_message("연결할 이름이 없어요.", ephemeral=True)
        resolver = UnmatchedResolveView(self.core_cog, self.guild, self.matched, self.unmatched)
        await interaction.response.send_message(embed=resolver.build_embed(), view=resolver, ephemeral=True)

    @discord.ui.button(label="❌ 취소", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.processing:
            return await interaction.response.send_message("⏳ 이미 등록이 진행 중이라 지금은 취소할 수 없어요.", ephemeral=True)
        self.processing = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("🚫 가져오기를 취소했어요. 아무것도 등록되지 않았어요.", ephemeral=True)


class UnmatchedResolveView(MariView):
    """매칭 안 된 이름을 하나씩 보여주면서, 관리자가 직접 서버 멤버를 골라 연결할 수 있게 하는 뷰"""

    def __init__(self, core_cog, guild: discord.Guild, matched: list, unmatched: list):
        super().__init__(timeout=300)
        self.core_cog = core_cog
        self.guild = guild
        self.matched = list(matched)      # 원래 매칭됐던 것 + 여기서 새로 연결한 것들이 계속 쌓여요
        self.remaining = list(unmatched)  # 아직 처리 안 한 (name, ids) 큐
        self.still_unmatched = []         # 건너뛴 것들
        self.user_select = None
        # 🔒 연타 방지. 이 뷰는 큐를 하나씩 pop하며 진행하기 때문에, 두 번 눌리면
        # 아직 처리하지도 않은 사람이 통째로 건너뛰어질 수 있어요.
        self.processing = False
        self._refresh_select()

    def _refresh_select(self):
        """현재 처리 중인 이름에 맞는 UserSelect로 교체합니다. (버튼들은 그대로 유지)"""
        if self.user_select is not None:
            self.remove_item(self.user_select)
        name, ids = self.remaining[0]
        select = discord.ui.UserSelect(
            placeholder=f"'{name}'님을 서버 멤버 중에서 선택하세요...",
            min_values=1, max_values=1,
        )
        select.callback = self._on_select
        self.user_select = select
        self.add_item(select)

    def build_embed(self) -> discord.Embed:
        name, ids = self.remaining[0]
        id_lines = "\n".join(f"• {k}: {v}" for k, v in ids.items())
        embed = discord.Embed(
            title=f"🔗 이름 연결 (남은 {len(self.remaining)}명)",
            description=(
                f"**이름:** {name}\n"
                f"**등록될 아이디:**\n{id_lines}\n\n"
                f"이 사람이 서버의 누구인지 아래에서 선택해주세요. 모르면 [건너뛰기]를 눌러주세요."
            ),
            color=discord.Color.blurple(),
        )
        return embed

    async def _advance(self, interaction: discord.Interaction):
        self.remaining.pop(0)
        if self.remaining:
            self._refresh_select()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        else:
            await self._finish(interaction)

    async def _on_select(self, interaction: discord.Interaction):
        if not await self._begin(interaction):
            return
        try:
            name, ids = self.remaining[0]
            picked = self.user_select.values[0]  # discord.Member 또는 discord.User
            member = picked if isinstance(picked, discord.Member) else self.guild.get_member(picked.id)
            if not member:
                return await interaction.response.send_message("❌ 서버 멤버가 아니에요. 다시 선택해주세요.", ephemeral=True)
            self.matched.append((name, member, ids))
            await self._advance(interaction)
        finally:
            self.processing = False

    @discord.ui.button(label="⏭️ 건너뛰기", style=discord.ButtonStyle.gray, row=1)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._begin(interaction):
            return
        try:
            name, ids = self.remaining[0]
            self.still_unmatched.append((name, ids))
            await self._advance(interaction)
        finally:
            self.processing = False

    @discord.ui.button(label="🏁 여기까지만 하고 확정 화면으로", style=discord.ButtonStyle.gray, row=1)
    async def finish_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._begin(interaction):
            return
        self.still_unmatched.extend(self.remaining)
        self.remaining = []
        await self._finish(interaction)

    async def _begin(self, interaction: discord.Interaction) -> bool:
        """연타 방지 게이트. 이미 처리 중이거나 큐가 비었으면 False를 돌려주고 안내만 해요."""
        if self.processing:
            await interaction.response.send_message("⏳ 방금 누른 게 아직 처리 중이에요! 잠시만요.", ephemeral=True)
            return False
        if not self.remaining:
            await interaction.response.send_message("✅ 이미 모두 처리했어요. 확정 화면에서 진행해주세요.", ephemeral=True)
            return False
        self.processing = True
        return True

    async def _finish(self, interaction: discord.Interaction):
        confirm_view = ImportConfirmView(self.core_cog, self.guild, self.matched, self.still_unmatched)
        total_entries = sum(len(ids) for _, _, ids in self.matched)
        desc = f"🔗 연결 완료! 이제 **{len(self.matched)}명** (아이디 {total_entries}건)이 등록 준비됐어요."
        if self.still_unmatched:
            names = ", ".join(n for n, _ in self.still_unmatched)
            desc += f"\n\n여전히 연결 안 된 {len(self.still_unmatched)}명: {names}"
        embed = discord.Embed(title="📦 아이디 목록 일괄 가져오기 (연결 완료)", description=desc[:4000], color=discord.Color.orange())
        await interaction.response.edit_message(embed=embed, view=confirm_view)

class MariCore(commands.Cog):
    """아이디 검색, 로깅 등 봇의 핵심 유틸리티 기능"""

    id_group = app_commands.Group(name="아이디", description="게임 아이디 등록/조회/관리 명령어 모음")

    # 📄 한 화면에 보여줄 인원 수. 디스코드가 임베드 하나에 허용하는 필드 수(25개)와 같아요.
    # 한 명이 필드 하나를 차지하므로 이 숫자를 넘기면 메시지 전송이 400으로 실패합니다.
    PAGE_SIZE = 25
    # 임베드 전체(제목+필드 이름+값 총합) 글자 수 상한은 6000자예요. 아이디를 아주 많이
    # 등록해둔 사람이 몰려 있으면 25명이 안 돼도 이 선에 먼저 걸릴 수 있어서 같이 셉니다.
    EMBED_TOTAL_LIMIT = 5800    # 제목·푸터 몫을 조금 남겨둔 값

    def __init__(self, bot):
        self.bot = bot
        # 🔁 on_ready는 재연결될 때마다 다시 불려요. 기동 로그를 매번 찍으면 콘솔이
        # 같은 내용으로 도배돼서 정작 중요한 오류를 놓칩니다. 처음 한 번만 출력하려고 둔 표시예요.
        self._boot_log_printed = False

        # 🖱️ [신규] 멤버 우클릭 → 앱 → 아이디 보기
        # ⚠️ 컨텍스트 메뉴는 코그 안에서 데코레이터로 못 만들어요. 객체를 직접 만들어 트리에 넣고,
        #    cog_unload에서 반드시 빼야 합니다. (안 빼면 코그를 다시 부를 때 중복 등록돼요)
        self.id_menu = app_commands.ContextMenu(name="아이디 보기", callback=self.show_ids_menu)
        self.bot.tree.add_command(self.id_menu)

    async def cog_unload(self):
        self.bot.tree.remove_command(self.id_menu.name, type=self.id_menu.type)

    # ========== 🖱️ 우클릭 조회 ==========
    def build_id_embed(self, user, guild: discord.Guild) -> Optional[discord.Embed]:
        """그 사람의 등록 아이디 카드를 만듭니다. 등록된 게 없으면 None.
        (`/아이디 조회`와 우클릭 메뉴가 같은 화면을 쓰도록 여기 한 군데에 모아뒀어요)"""
        ids = state.user_ids.get(str(guild.id), {}).get(str(user.id))
        if not ids:
            return None
        embed = discord.Embed(
            title=f"🎮 {user.display_name}님의 게임 아이디",
            description=f"{bot_name()}{josa(bot_name(), '이가')} 예쁘게 정리했어요~ 💖",
            color=discord.Color.green(),
        )
        # 아이디를 아주 많이 등록한 사람이 있어서, 임베드 필드 25개 한도에 걸리지 않게 잘라요.
        for platform, value in list(ids.items())[:self.PAGE_SIZE]:
            # 🛡️ 값이 비어 있으면 디스코드가 임베드 자체를 400으로 거부해서 카드가 통째로 안 떠요.
            #    (빈 값이 들어가는 경로는 /아이디 수정에서 막았지만, 이미 저장된 데이터가 있을 수
            #     있어서 여기서도 받아냅니다. 무엇이 비었는지 보여야 고칠 수 있으니까요)
            shown = str(value).strip() or "*(비어 있음 — `/아이디 수정`으로 채워주세요)*"
            embed.add_field(name=str(platform)[:256] or "?", value=shown[:1024], inline=False)
        if len(ids) > self.PAGE_SIZE:
            embed.set_footer(text=f"등록 {len(ids)}건 중 {self.PAGE_SIZE}건만 보여요")
        return embed

    async def show_ids_menu(self, interaction: discord.Interaction, member: discord.Member):
        """멤버를 우클릭해서 등록된 게임 아이디를 바로 봅니다. (`/아이디 조회`와 같은 내용)"""
        embed = self.build_id_embed(member, interaction.guild)
        if embed is None:
            return await interaction.response.send_message(
                f"{member.mention}님의 등록된 아이디가 없어요!", ephemeral=True
            )
        # 👀 우클릭 조회는 나만 보이게 합니다. 슬래시 `/아이디 조회`는 지금처럼 공개로 두고요.
        #    (툭 눌러보는 동작이라 채널에 흔적을 남기지 않는 편이 자연스러워요)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def has_permission(self, member: discord.Member) -> bool:
        """관리자 권한 혹은 허가된 역할(ids_admin)이 있는지 검증하는 함수"""
        # 🗑️ [정리] mari_access 레거시 개별 허용 목록 체크는 제거했습니다.
        # `/설정 관리자 아이디`로 지정하는 ids_admin 역할이 완전히 같은 역할을 대체합니다.
        return member_has_admin_or_role(member, "ids_admin")

    # ========== 🆔 [신규] 아이디 자동등록 시스템 ==========
    # 지정된 채널에 유저가 "플랫폼 아이디" 형식으로 올리면, 마리가 알아서 아이디 데이터베이스에
    # 등록하고 원본 메세지는 삭제해요. 플랫폼을 못 알아보겠으면 조용히 넘기지 않고
    # ids_admin 역할을 가진 분들께 "아이디 로그" 채널에서 확인을 받아요.

    def _load_id_pending(self) -> dict:
        data = safe_json_load(ID_PENDING_FILE, {"requests": {}})
        data.setdefault("requests", {})
        return data

    def _save_id_pending(self, data: dict):
        atomic_json_save_or_raise(ID_PENDING_FILE, data, indent=2)

    def _register_single_id(self, gid: str, uid: str, plat_input: str, game_id: str) -> str:
        """정규화 + 중복 방지 이름 부여까지 처리하고, 실제 저장된 키 이름을 반환합니다.
        (state.save()는 호출부에서 한 번에 처리해요)"""
        state.user_ids.setdefault(gid, {}).setdefault(uid, {})
        existing = state.user_ids[gid][uid]
        normalized = normalize_platform(plat_input)

        # 🐛 [버그 수정 + 강화] 이미 완전히 똑같은 값이 등록돼 있으면 새 키를 또 만들지 않고
        # 기존 키를 그대로 재사용해요. 예전엔 "같은 플랫폼 계열(Riot, Riot2...)" 안에서만
        # 검사해서, 플랫폼 표기가 다르게 들어오면(예: '라이엇' vs '롤') 못 걸러냈어요.
        # 이제는 이 사람이 등록해둔 값 전체를 통틀어 검사해서 훨씬 확실하게 막아요.
        game_id_norm = game_id.strip() if isinstance(game_id, str) else game_id
        for key, val in existing.items():
            if (val.strip() if isinstance(val, str) else val) == game_id_norm:
                return key

        if normalized == "기타":
            actual = next_misc_name(existing)
        else:
            actual = next_platform_name(existing, normalized)

        existing[actual] = game_id
        return actual

    # ========== 📋 [신규] 아이디 명단 자동 유지 ==========
    #
    # 🗑️ [정리] 예전엔 이 명단이 **0~4레벨 등급 사다리**를 중심으로 짜여 있었어요.
    # 등급제는 원본 서버 고유 제도라 통째로 걷어냈습니다. (parked/core_levels.py.txt)
    # 지금은 "대장 / 그 외 멤버" 두 칸이라 어느 서버에서나 바로 씁니다.
    #
    # ⚠️ settings.json 의 키 이름(channels.level_roster, level_roster_message_ids)은
    #    일부러 옛 이름 그대로 뒀어요. 이미 돌고 있는 봇의 설정 파일에 들어 있는 값이라
    #    이름을 바꾸면 업데이트하는 순간 지정해둔 채널을 잃어버립니다.
    def _is_chief(self, member: discord.Member, settings: Optional[dict] = None) -> bool:
        """이 멤버가 '대장' 역할(`/설정 명단 대장`으로 지정)을 갖고 있는지.

        🐛 [성능 버그 수정] 예전엔 이 함수가 불릴 때마다 매번 load_settings()로 파일을 새로
        읽었는데, 명단 자동 갱신처럼 서버 멤버 전원을 한 명씩 순회하며 이 함수를 부르는
        곳에서는 그게 "멤버 수만큼 파일을 반복해서 여는" 것과 같았어요. 멤버가 몇백 명이면
        몇백 번 파일을 열게 되는 구조라 체감상 느려질 수 있었어요. 이제 settings를 미리
        한 번만 읽어서 넘겨줄 수 있게 했고, 안 넘기면 예전처럼 그때 한 번만 읽어요."""
        if settings is None:
            settings = load_settings()
        chief_role_id = settings.get("roles", {}).get("chief_role")
        return bool(chief_role_id and any(r.id == chief_role_id for r in member.roles))

    # 📛 명단 블록의 제목과 색. ANSI 색은 디스코드 ```ansi 코드블록에서만 먹혀요.
    # (앞자리 2는 흐리게, 41은 배경 빨강이라 대장 칸만 반전돼 보입니다)
    ROSTER_SECTIONS = (
        ("chief", "대장", "2;41"),
        ("member", "멤버", "2;34"),
    )

    async def _refresh_id_roster(self, guild: discord.Guild, force_repost: bool = False):
        """등록/수정/삭제가 일어날 때마다 호출되어, 아이디 명단 채널을 최신 상태로 다시 게시합니다.
        force_repost=True면 내용이 그대로여도 무조건 전체를 지우고 순서대로 새로 게시해요. (수동 "갱신" 트리거용)"""
        try:
            async with self.bot.settings_lock:
                settings = load_settings()
                ch_id = settings.get("channels", {}).get("level_roster")
                if not ch_id:
                    return
                channel = guild.get_channel(ch_id)
                if not channel:
                    return

                gid = str(guild.id)
                guild_ids_data = state.user_ids.get(gid, {})

                # 🗑️ [정리] 예전엔 여기서 레벨 역할을 보고 0~4레벨 칸에 나눠 담았어요.
                # 그런데 레벨 역할표가 창고로 간 뒤로는 아무 칸에도 안 걸리는 사람이 명단에서
                # **통째로 빠져** 있었습니다. 이제 대장이 아니면 전부 '멤버' 칸에 들어가요.
                buckets: dict = {"chief": [], "member": []}
                for member in guild.members:
                    if member.bot:
                        continue
                    buckets["chief" if self._is_chief(member, settings) else "member"].append(member)
                for key in buckets:
                    buckets[key].sort(key=lambda m: m.display_name.lower())

                # 📦 [개선] 칸마다 각각 독립된 블록을 만들고, 칸 경계를 넘어서 섞이지 않게 분할합니다.
                # (예전엔 그냥 2000자 넘으면 뚝 잘라서, 한 칸이 메세지 두 개에 걸쳐 어중간하게 잘릴 수 있었어요)
                CHUNK_LIMIT = 1800  # ```ansi 코드블록 오버헤드 감안

                def split_lines_to_chunks(block_lines: list) -> list:
                    """긴 블록 하나를 글자 수 제한에 맞춰 여러 청크로 쪼갭니다."""
                    result, current = [], ""
                    for line in block_lines:
                        if len(current) + len(line) + 1 > CHUNK_LIMIT:
                            result.append(current)
                            current = line + "\n"
                        else:
                            current += line + "\n"
                    if current.strip():
                        result.append(current)
                    return result

                chunks = []
                header = "게임 아이디 목록"
                for key, title, color in self.ROSTER_SECTIONS:
                    members = buckets[key]
                    if not members:
                        continue
                    label = title if key == "chief" else f"{title} ({len(members)}명)"
                    block_lines = [header, "", f"\u001b[{color}m{label}\u001b[0m", ""]
                    for m in members:
                        ids = guild_ids_data.get(str(m.id), {})
                        block_lines.append(f"[\u001b[{color}m{m.display_name}\u001b[0m]")
                        if ids:
                            for plat, val in ids.items():
                                block_lines.append(f"{plat} : {val}")
                        else:
                            block_lines.append("(등록된 아이디 없음)")
                        block_lines.append("")

                    block_text = "\n".join(block_lines).rstrip()
                    if len(block_text) <= CHUNK_LIMIT:
                        # 이 칸은 통째로 메세지 하나에 딱 들어감
                        chunks.append(block_text)
                    else:
                        # 칸 하나가 너무 커서 어쩔 수 없이 그 칸 안에서만 쪼갬 (다른 칸과는 안 섞임)
                        chunks.extend(split_lines_to_chunks(block_lines))

                if not chunks:
                    chunks = ["(명단에 올릴 멤버가 없어요)"]

                # 📢 [신규] 아이디 관리자가 /아이디공지로 작성한 공지사항을 맨 끝에 별도 블록으로 추가
                notice_text = settings.get("id_roster_notice", "").strip()
                if notice_text:
                    notice_lines = [header, "", "\u001b[2;41mNOTE\u001b[0m", ""] + notice_text.split("\n")
                    notice_block = "\n".join(notice_lines).rstrip()
                    if len(notice_block) <= CHUNK_LIMIT:
                        chunks.append(notice_block)
                    else:
                        chunks.extend(split_lines_to_chunks(notice_lines))

                # 🔕 이 명단은 자주 확인하는 게 아니라 알림이 굳이 필요 없다고 하셔서,
                # 기존 메세지를 조용히 수정(edit)하는 걸 기본으로 해요.
                old_ids = settings.get("level_roster_message_ids", [])
                old_messages = []
                for mid in old_ids:
                    try:
                        old_messages.append(await channel.fetch_message(mid))
                    except Exception:
                        old_messages.append(None)

                # 🔢 순서("대장→4→3→2→1→0→공지")를 항상 보장하기 위해, 하나라도 그대로
                # 수정이 안 되는 게 있으면(1시간 지난 메세지 수정 한도 등) 낱개로 땜질하지 않고
                # 명단 메세지 전체를 지운 뒤 순서대로 한꺼번에 다시 게시해요. (낱개로 새로 올리면
                # 그 메세지만 채널 맨 아래로 밀려서 순서가 뒤죽박죽되기 때문이에요)
                # 🔄 force_repost=True("갱신" 수동 트리거)면 내용이 그대로여도 무조건 전체 재게시해요.
                need_full_repost = force_repost or (len(old_messages) != len(chunks)) or any(m is None for m in old_messages)
                if not need_full_repost:
                    for i, chunk in enumerate(chunks):
                        content = f"```ansi\n{chunk}\n```"
                        try:
                            await old_messages[i].edit(content=content)
                        except Exception as e:
                            print(f"⚠️ 아이디 명단 수정 실패, 전체 재게시로 전환: {e}")
                            need_full_repost = True
                            break

                new_ids = []
                if need_full_repost:
                    for m in old_messages:
                        if m:
                            try:
                                await m.delete()
                            except Exception:
                                pass
                    for chunk in chunks:
                        content = f"```ansi\n{chunk}\n```"
                        try:
                            msg = await channel.send(content)
                            new_ids.append(msg.id)
                        except Exception as e:
                            print(f"⚠️ 아이디 명단 게시 실패: {e}")
                else:
                    new_ids = [m.id for m in old_messages]

                settings["level_roster_message_ids"] = new_ids
                save_settings(settings)
        except Exception as e:
            print(f"⚠️ 아이디 명단 갱신 중 오류: {e}")

    @id_group.command(name="공지", description="[관리자] 아이디 명단 맨 아래에 공지사항 한 줄을 추가하거나 확인해요. (아이디 관리자 전용)")
    @app_commands.describe(
        내용="추가할 공지 내용 (줄바꿈은 \\n 입력, 생략하면 지금 공지를 보여줘요)",
        덮어쓰기="True로 하면 추가가 아니라 기존 공지 전체를 이 내용으로 완전히 바꿔치기해요",
        삭제="공지를 전부 지우려면 True로 설정하세요",
    )
    @app_commands.guild_only()
    async def set_id_roster_notice(self, interaction: discord.Interaction, 내용: Optional[str] = None, 덮어쓰기: bool = False, 삭제: bool = False):
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not self.has_permission(member):
            return await interaction.response.send_message("❌ 권한이 없어요! 관리자에게 역할을 받아주세요.", ephemeral=True)

        # 🔒 등록 채널의 자동 명단 갱신과 동시에 settings.json을 건드릴 수 있어서 같은 잠금을 써요.
        # ⚠️ asyncio.Lock은 재진입이 안 되니까, _refresh_id_roster 호출은 반드시 이 블록 밖에서 해요.
        need_refresh = False
        async with self.bot.settings_lock:
            settings = load_settings()

            if 삭제:
                settings.pop("id_roster_notice", None)
                save_settings(settings)
                await interaction.response.send_message("🗑️ 공지사항을 전부 지웠어요. 명단에도 반영할게요.", ephemeral=True)
                need_refresh = True
            elif 내용 is None:
                current = settings.get("id_roster_notice", "").strip()
                if not current:
                    await interaction.response.send_message("ℹ️ 등록된 공지사항이 없어요. `/아이디공지 내용:...`으로 추가할 수 있어요.", ephemeral=True)
                else:
                    await interaction.response.send_message(f"📋 **현재 공지사항**\n```\n{current}\n```", ephemeral=True)
            else:
                내용 = 내용.replace("\\n", "\n")

                # 🐛 [버그 수정] 예전엔 매번 통째로 덮어써서, 원본 문서의 "밑에 계속 추가해주세요" 취지랑
                # 안 맞았어요. 이제는 기본이 "이어붙이기(추가)"이고, 완전 교체는 덮어쓰기 옵션을 켰을 때만 해요.
                if 덮어쓰기:
                    settings["id_roster_notice"] = 내용
                    msg = "✅ 공지사항을 통째로 새로 바꿨어요! 아이디 명단 맨 아래에 반영할게요."
                else:
                    existing = settings.get("id_roster_notice", "").strip()
                    settings["id_roster_notice"] = f"{existing}\n{내용}" if existing else 내용
                    msg = "✅ 공지사항에 추가했어요! 아이디 명단 맨 아래에 반영할게요."

                save_settings(settings)
                await interaction.response.send_message(msg, ephemeral=True)
                need_refresh = True

        if need_refresh:
            await self._refresh_id_roster(interaction.guild)

    @id_group.command(name="새로고침", description="[관리자] ids.json 파일을 다시 읽어와 메모리 데이터를 갱신해요. (파일을 직접 수정한 뒤 꼭 실행!)")
    @app_commands.guild_only()
    async def reload_id_data(self, interaction: discord.Interaction):
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not self.has_permission(member):
            return await interaction.response.send_message("❌ 권한이 없어요! 관리자에게 역할을 받아주세요.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        # 🚨 [중요] 아이디 DB는 봇이 켜질 때 파일에서 읽어와 메모리에 들고 있어요.
        # 그래서 ids.json을 직접 수정해도 봇은 그걸 모르고, 다음 저장 때 메모리에 있던
        # 옛날 데이터로 파일을 통째로 덮어써버려요. 이 명령어로 파일 내용을 강제로 다시 읽어옵니다.
        before_users = len(state.user_ids)
        try:
            state.load()
        except Exception as e:
            return await interaction.followup.send(f"❌ 파일을 읽는 중 오류가 발생했어요: {e}", ephemeral=True)
        after_users = len(state.user_ids)

        await interaction.followup.send(
            f"🔄 `ids.json`을 다시 읽어왔어요!\n"
            f"└ 등록된 서버 수: {before_users}개 → **{after_users}개**\n"
            f"이제 `/아이디 목록` 갱신하시면 파일 내용 그대로 반영돼요.",
            ephemeral=True
        )
        await self._refresh_id_roster(interaction.guild, force_repost=True)

    @id_group.command(name="대기열정리", description="[관리자] 쌓여있는 아이디 확인 대기 요청을 전부 비워요. (엉뚱한 등록 사고 방지용)")
    @app_commands.guild_only()
    async def clear_id_pending(self, interaction: discord.Interaction):
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not self.has_permission(member):
            return await interaction.response.send_message("❌ 권한이 없어요! 관리자에게 역할을 받아주세요.", ephemeral=True)

        data = self._load_id_pending()
        count = len(data.get("requests", {}))
        data["requests"] = {}
        self._save_id_pending(data)
        await interaction.response.send_message(
            f"🧹 쌓여있던 아이디 확인 대기 요청 **{count}건**을 전부 비웠어요.\n"
            f"(이제 로그 채널에서 대화해도 엉뚱하게 등록되지 않아요)",
            ephemeral=True
        )

    @id_group.command(name="중복정리", description="[관리자] 같은 아이디가 실수로 중복 등록된 걸 한 번에 정리해요.")
    @app_commands.guild_only()
    async def cleanup_duplicate_ids(self, interaction: discord.Interaction):
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not self.has_permission(member):
            return await interaction.response.send_message("❌ 권한이 없어요! 관리자에게 역할을 받아주세요.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        gid = str(interaction.guild.id)
        guild_data = state.user_ids.get(gid, {})

        removed_count = 0
        affected_people = 0

        # 🐛 [대폭 강화] 예전엔 "같은 플랫폼 계열(Riot, Riot2...)"일 때만 중복으로 봤는데,
        # 그거보다 훨씬 많은 중복이 있었어요. 이제는 플랫폼 이름이 무엇이든 상관없이,
        # 같은 사람 안에서 "값(아이디)이 완전히 똑같으면" 전부 중복으로 간주하고 정리해요.
        for uid, platforms in guild_data.items():
            seen_values = {}  # 정리된 값(공백 제거) -> 맨 처음 등록된 키
            to_delete = []
            for key in list(platforms.keys()):
                val = platforms[key]
                norm_val = val.strip() if isinstance(val, str) else val
                if norm_val in seen_values:
                    to_delete.append(key)
                else:
                    seen_values[norm_val] = key

            # 🗑️ [신규] "기타N": "라벨 : 값" 형태로 예전 방식으로 저장된 항목이, 나중에
            # 같은 정보가 "라벨": "값"으로 깔끔하게 따로 등록되면서 남긴 레거시 중복도 정리해요.
            # (값 문자열 자체는 서로 달라서("라벨 : 값" vs "값") 위 검사로는 못 잡혀요)
            for key in list(platforms.keys()):
                if not key.startswith("기타"):
                    continue
                val = platforms.get(key)
                if not isinstance(val, str) or " : " not in val:
                    continue
                embedded_label, _, embedded_value = val.partition(" : ")
                embedded_value = embedded_value.strip()
                for other_key, other_val in platforms.items():
                    if other_key == key:
                        continue
                    if isinstance(other_val, str) and other_val.strip() == embedded_value and embedded_value:
                        if key not in to_delete:
                            to_delete.append(key)
                        break

            for key in to_delete:
                del platforms[key]
                removed_count += 1
            if to_delete:
                affected_people += 1

        if removed_count:
            state.save()
            await self._refresh_id_roster(interaction.guild)
            await interaction.followup.send(
                f"🧹 중복 정리 완료! **{affected_people}명**에게서 중복 항목 **{removed_count}건**을 삭제했어요. 명단도 갱신했어요.",
                ephemeral=True
            )
        else:
            await interaction.followup.send("✨ 중복된 항목이 없었어요!", ephemeral=True)

    # ========== 📦 [신규] 기존 아이디 목록 문서 일괄 가져오기 ==========
    @id_group.command(name="가져오기", description="[관리자] 예전에 쓰던 아이디 목록 게시글을 .txt 파일로 올리면 한 번에 등록해요.")
    @app_commands.describe(파일="아이디 목록 전체를 담은 .txt 파일")
    @app_commands.guild_only()
    async def import_legacy_id_list(self, interaction: discord.Interaction, 파일: discord.Attachment):
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not self.has_permission(member):
            return await interaction.response.send_message("❌ 권한이 없어요! 관리자에게 역할을 받아주세요.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        try:
            raw_bytes = await 파일.read()
            text = raw_bytes.decode("utf-8", errors="ignore")
        except Exception as e:
            return await interaction.followup.send(f"❌ 파일을 읽을 수 없어요: {e}", ephemeral=True)

        parsed = parse_legacy_id_document(text)
        if not parsed:
            return await interaction.followup.send("❌ 파일에서 아무 항목도 인식하지 못했어요. 형식을 확인해주세요.", ephemeral=True)

        guild = interaction.guild
        matched = []    # (name, member, id_dict)
        unmatched = []  # (name, id_dict)

        for name, ids in parsed.items():
            found = find_guild_member_by_name(guild, name)
            if found:
                matched.append((name, found, ids))
            else:
                unmatched.append((name, ids))

        total_entries = sum(len(ids) for _, _, ids in matched)

        desc = (
            f"📄 총 **{len(matched) + len(unmatched)}명** 인식, 그중 **{len(matched)}명 매칭 성공** (아이디 {total_entries}건)\n"
            f"⚠️ 서버 멤버와 매칭 안 된 이름: **{len(unmatched)}명**\n\n"
            f"바로 **[확정]** 하시거나, **[🔗 매칭 안 된 이름 연결하기]**로 직접 서버 멤버를 골라 연결할 수 있어요. (5분 내 미클릭 시 자동 취소)"
        )
        if unmatched:
            names_text = ", ".join(n for n, _ in unmatched[:30])
            if len(unmatched) > 30:
                names_text += f" 외 {len(unmatched) - 30}명"
            desc += f"\n\n**매칭 실패 목록**\n{names_text}"

        embed = discord.Embed(title="📦 아이디 목록 일괄 가져오기 미리보기", description=desc[:4000], color=discord.Color.orange())
        view = ImportConfirmView(self, guild, matched, unmatched)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def _notify_admins_for_clarification(self, guild: discord.Guild, member: discord.Member, request_id: str, req: dict):
        """플랫폼(또는 형식)이 불명확한 항목을 개인 DM 대신 '아이디 로그' 채널에 올려서 물어봅니다."""
        settings = load_settings()
        admin_role_ids = _get_role_ids(settings, "ids_admin")
        admins = [m for m in guild.members if not m.bot and any(r.id in admin_role_ids for r in m.roles)]
        if not admins and guild.owner:
            admins = [guild.owner]  # 관리자 역할이 아직 설정 안 됐으면 최소한 서버 소유자에게라도

        log_ch_id = settings.get("channels", {}).get("id_log")
        log_channel = guild.get_channel(log_ch_id) if log_ch_id else None
        if not log_channel:
            print("⚠️ 아이디 로그 채널이 설정되어 있지 않아서 확인 요청을 올릴 수 없어요. `/설정 채널 아이디로그`로 지정해주세요.")
            req["notified_admin_ids"] = [a.id for a in admins]  # 채널이 없어도 누가 답할 수 있는지는 기록해둬요
            return

        if req["kind"] == "platform_only":
            desc = (
                f"대상: {member.mention} ({member.display_name})\n"
                f"입력: `{req['raw_text']}`\n"
                f"아이디는 `{req['game_id']}`로 인식했는데, **플랫폼이 뭔지** 이 채널에 답장해주세요! (예: 라이엇, 스팀 등)\n"
                f"└ 등록 안 하고 넘어가려면 `무시`라고 답장해주세요."
            )
        elif req["kind"] == "change_request":
            # 🔄 [신규] 같은 플랫폼의 기존 값을 찾아서, 뭐가 뭐로 바뀌는지 미리 보여줘요.
            normalized = normalize_platform(req["plat_input"])
            gid_str, uid_str = str(req["guild_id"]), str(req["user_id"])
            existing = state.user_ids.get(gid_str, {}).get(uid_str, {})
            candidates = get_platform_candidates(existing, normalized) if normalized != "기타" else []
            if candidates:
                old_key, old_val = candidates[0]
                change_line = f"`{old_key}`: `{old_val}` → `{req['new_value']}`"
                extra_note = f" (동일 플랫폼으로 등록된 항목이 {len(candidates)}개라 그중 `{old_key}`를 바꿔요)" if len(candidates) > 1 else ""
            else:
                change_line = f"`{normalized}`: (기존 등록 없음) → `{req['new_value']}`"
                extra_note = " (기존에 등록된 게 없어서, 승인하면 새로 등록돼요)"
            desc = (
                f"대상: {member.mention} ({member.display_name})\n"
                f"입력: `{req['raw_text']}`\n"
                f"🔄 **변경 요청**: {change_line}{extra_note}\n"
                f"승인하려면 `승인`, 거부하려면 `거부`라고 이 채널에 답장해주세요."
            )
        else:
            desc = (
                f"대상: {member.mention} ({member.display_name})\n"
                f"입력: `{req['raw_text']}`\n"
                f"형식을 못 알아봤어요. `플랫폼 아이디` 형식으로 이 채널에 답장해주세요! (예: `라이엇 만해#kr1`)\n"
                f"└ 등록 안 하고 넘어가려면 `무시`라고 답장해주세요."
            )

        try:
            sent = await log_channel.send(
                content=(", ".join(a.mention for a in admins) + "\n💬 이 메세지에 **답장(reply)**으로 알려주세요!") if admins else "💬 이 메세지에 **답장(reply)**으로 알려주세요!",
                embed=discord.Embed(title="🔍 아이디 자동등록 확인이 필요해요!", description=desc, color=discord.Color.orange()),
            )
            # 🚨 어느 요청에 대한 답장인지 정확히 구분하기 위해, 이 안내 메세지의 ID를 같이 기록해둬요.
            req["notice_message_id"] = sent.id
        except Exception as e:
            print(f"⚠️ 아이디등록 확인 요청 게시 실패: {e}")

        req["notified_admin_ids"] = [a.id for a in admins]

    async def _resolve_id_pending_from_reply(self, message: discord.Message):
        """ids_admin이 아이디 로그 채널에서 마리의 확인 요청 메세지에 '답장(reply)'하면 처리합니다."""
        # 🚨 [심각한 버그 수정] 예전엔 이 채널에서 관리자가 무슨 말을 하든(잡담이라도) 그걸
        # "가장 오래된 대기 요청의 답변"으로 해석해서 아무 사람한테나 등록해버렸어요.
        # 그래서 대기 요청이 쌓여있는 상태에서 관리자가 대화하면, 엉뚱한 사람 아이디가
        # 다른 사람 계정에 줄줄이 등록되는 사고가 났어요.
        # 이제는 마리가 보낸 확인 요청 메세지에 "답장(reply)"한 경우에만 처리해요.
        if message.reference is None or message.reference.message_id is None:
            return

        try:
            replied_to = await message.channel.fetch_message(message.reference.message_id)
        except Exception:
            return
        if not replied_to.author.bot or replied_to.author.id != self.bot.user.id:
            return  # 마리가 보낸 메세지에 답장한 게 아니면 무시

        data = self._load_id_pending()
        requests = data.get("requests", {})

        # 답장한 그 메세지에 딱 연결된 요청만 찾아요 (엉뚱한 요청이 처리되지 않도록)
        matched = [(rid, req) for rid, req in requests.items() if req.get("notice_message_id") == replied_to.id]
        if not matched:
            return  # 이 메세지에 연결된 대기 항목이 없으면 그냥 일반 대화로 취급
        request_id, req = matched[0]

        reply_text = message.content.strip()
        if not reply_text:
            return

        # 🙅 [신규] "무시"(또는 "스킵"/"건너뛰기"/"거부")라고 답장하면, 등록/변경하지 않고
        # 이 대기 요청만 지워요. 애매해서 굳이 대답하기 애매한 항목은 관리자가 직접 나중에
        # 수동으로 `/아이디 등록`으로 처리하거나 그냥 넘어갈 수 있게 하기 위한 옵션이에요.
        if reply_text in ("무시", "스킵", "건너뛰기", "거부", "취소"):
            del requests[request_id]
            self._save_id_pending(data)
            guild = self.bot.get_guild(req["guild_id"])
            member = guild.get_member(req["user_id"]) if guild else None
            member_label = member.mention if member else f"(유저ID: {req['user_id']})"
            verb = "변경" if req["kind"] == "change_request" else "등록"
            await message.channel.send(
                f"🙅 {'거부' if reply_text in ('거부', '취소') else '무시'}했어요. {member_label}님의 `{req['raw_text']}` 항목은 {verb}하지 않았어요. "
                f"필요하면 `/아이디 등록`(또는 `/아이디 수정`)으로 직접 처리해주세요."
            )
            return

        guild = self.bot.get_guild(req["guild_id"])
        if not guild:
            del requests[request_id]
            self._save_id_pending(data)
            return await message.channel.send("❌ 서버를 찾을 수 없어서 처리를 취소했어요.")

        member = guild.get_member(req["user_id"])
        member_label = member.mention if member else f"(탈퇴한 유저: {req['user_id']})"

        try:
            if req["kind"] == "platform_only":
                # 플랫폼명 답변에 실수로 콜론이 붙어도("라이엇:") 깔끔하게 떼고 등록해요.
                clean_platform = reply_text.rstrip(":").strip()
                actual = self._register_single_id(str(req["guild_id"]), str(req["user_id"]), clean_platform, req["game_id"])
                state.save()
                await message.channel.send(f"✅ 등록 완료! {member_label}님의 `{actual}` → `{req['game_id']}`")
            elif req["kind"] == "change_request":
                # 🔄 [신규] "승인"류 답장만 처리하고, 그 외 텍스트는 알아듣게 다시 안내해요.
                if reply_text not in ("승인", "예", "네", "확인", "ok", "OK"):
                    return await message.channel.send("❓ `승인` 또는 `거부`로만 답장해주세요.")

                gid_str, uid_str = str(req["guild_id"]), str(req["user_id"])
                normalized = normalize_platform(req["plat_input"])
                existing = state.user_ids.setdefault(gid_str, {}).setdefault(uid_str, {})
                candidates = get_platform_candidates(existing, normalized) if normalized != "기타" else []

                if candidates:
                    old_key, old_val = candidates[0]
                    existing[old_key] = req["new_value"]
                    actual = old_key
                    state.save()
                    await message.channel.send(f"✅ 변경 승인 완료! {member_label}님의 `{actual}`: `{old_val}` → `{req['new_value']}`")
                else:
                    actual = self._register_single_id(gid_str, uid_str, req["plat_input"], req["new_value"])
                    state.save()
                    await message.channel.send(f"✅ 변경 승인 완료! (기존 등록이 없어서 새로 등록됐어요) {member_label}님의 `{actual}` → `{req['new_value']}`")
            else:
                parsed = _split_platform_and_id(reply_text)
                if not parsed:
                    return await message.channel.send("❌ `플랫폼 아이디` 형식으로 다시 답장해주세요! (예: `라이엇 만해#kr1`)")
                plat_input, game_id = parsed
                actual = self._register_single_id(str(req["guild_id"]), str(req["user_id"]), plat_input, game_id)
                state.save()
                await message.channel.send(f"✅ 등록 완료! {member_label}님의 `{actual}` → `{game_id}`")
        except Exception as e:
            print(f"⚠️ 확인 답변 기반 아이디 등록 처리 오류: {e}")
            return await message.channel.send("❌ 처리 중 오류가 발생했어요. 다시 시도해주세요.")

        del requests[request_id]
        self._save_id_pending(data)

        verb = "변경을 승인" if req["kind"] == "change_request" else "등록"
        await send_log_embed(
            self.bot, "id_log",
            f"{message.author.mention} 님이 확인 답변을 거쳐 {member_label}님의 아이디 {verb}했어요.",
            guild=guild,
        )
        await self._refresh_id_roster(guild)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # 🚫 [완전 제거] DM 기반 확인/답장 처리는 더 이상 안 해요. 관리자 전원이 같은 DM을
        # 받아서 헷갈렸다는 피드백이 있어서, 확인 요청/답장은 전부 아이디 로그 채널로만 받아요.
        if message.guild is None:
            return

        # 🐛 [성능 버그 수정] 예전엔 이 아래 세 채널 체크마다 load_settings()로 매번 파일을
        # 새로 읽었어요. on_message는 서버의 "모든" 메세지마다 호출되니까, 활발한 서버라면
        # 메세지 하나당 최대 3번씩, 하루 수만 번 파일을 반복해서 여는 셈이었어요.
        # 이제 딱 한 번만 읽어서 재사용해요.
        settings = load_settings()
        channels = settings.get("channels", {})

        # 📋 [아이디 로그 채널] 확인 요청에 대한 답장인지 먼저 확인
        log_ch_id = channels.get("id_log")
        if log_ch_id and message.channel.id == log_ch_id:
            return await self._resolve_id_pending_from_reply(message)

        # 🔄 [신규] 아이디 명단 채널에서 아이디 관리자(또는 서버 관리자)가 "갱신"이라고 치면
        # 그 자리에서 바로 새로고침해요. (평소엔 등록/수정/삭제할 때만 자동 갱신되니까,
        # 급하게 바로 반영하고 싶을 때 쓰는 수동 트리거예요)
        roster_ch_id = channels.get("level_roster")
        if roster_ch_id and message.channel.id == roster_ch_id and message.content.strip() == "갱신":
            member = message.guild.get_member(message.author.id)
            if not member or not self.has_permission(member):
                try:
                    await message.delete()
                except Exception:
                    pass
                return
            try:
                await message.delete()
            except Exception:
                pass
            await self._refresh_id_roster(message.guild, force_repost=True)
            confirm = await message.channel.send(f"🔄 {message.author.mention}님이 명단을 갱신했어요.")
            await asyncio.sleep(5)
            try:
                await confirm.delete()
            except Exception:
                pass
            return

        # 📥 [채널] 지정된 아이디 자동등록 채널인지 확인
        submit_ch_id = channels.get("id_submit")
        if not submit_ch_id or message.channel.id != submit_ch_id:
            return
        if not is_feature_enabled("id"):
            return  # 🚧 아이디 기능이 정지된 상태면 자동등록도 건너뜀

        await self._process_id_submission(message)

    async def _process_id_submission(self, message: discord.Message):
        """아이디 자동등록 채널에 올라온 메세지 하나를 파싱해서 등록/대기열 처리까지 합니다.
        실시간 on_message에서 호출돼요."""
        content = message.content or ""
        if not content.strip():
            return

        gid, uid = str(message.guild.id), str(message.author.id)

        # 쉼표/줄바꿈 둘 다 구분자로 지원 (한 줄에 하나씩, 또는 쉼표로 나열)
        raw_segments = [seg.strip() for chunk in content.split("\n") for seg in chunk.split(",") if seg.strip()]
        if not raw_segments:
            return

        registered = []
        pending_data = self._load_id_pending()
        new_pending = []  # (request_id, req) 튜플들, 나중에 한꺼번에 DM 발송
        touched_any = False  # 이 메세지 안에 "아이디 시도로 보이는 것"이 하나라도 있었는지

        for seg in raw_segments:
            # 🔄 [신규] "변경 라이엇 새아이디#kr1" / "수정 스팀 123456" 처럼 맨 앞에 변경/수정을
            # 붙이면, 플랫폼을 알아보든 못 알아보든 상관없이 무조건 관리자 승인을 거쳐서
            # "같은 플랫폼의 기존 값"을 새 값으로 바꿔줘요. (새로 등록이 아니라 교체가 목적)
            first_word = seg.split(maxsplit=1)[0] if seg.split() else ""
            if first_word in ("변경", "수정"):
                rest = seg[len(first_word):].strip()
                change_parsed = _split_platform_and_id(rest) if rest else None
                if change_parsed:
                    plat_input, new_value = change_parsed
                    touched_any = True
                    request_id = str(uuid.uuid4())
                    req = {
                        "guild_id": message.guild.id, "user_id": message.author.id,
                        "kind": "change_request", "raw_text": seg,
                        "plat_input": plat_input, "new_value": new_value,
                        "timestamp": dt.datetime.now(KST).isoformat(),
                    }
                    new_pending.append((request_id, req))
                    continue
                # "변경"/"수정"이라고 써놓고 뒤에 플랫폼/아이디를 못 알아봤으면, 형식 불명확으로 넘겨서
                # 관리자가 직접 "플랫폼 아이디" 형식으로 답장하게 해요.
                touched_any = True
                request_id = str(uuid.uuid4())
                req = {
                    "guild_id": message.guild.id, "user_id": message.author.id,
                    "kind": "full", "raw_text": seg,
                    "timestamp": dt.datetime.now(KST).isoformat(),
                }
                new_pending.append((request_id, req))
                continue

            parsed = _split_platform_and_id(seg)
            if parsed:
                plat_input, game_id = parsed
                normalized = normalize_platform(plat_input)
                if normalized in KNOWN_PLATFORMS:
                    actual = self._register_single_id(gid, uid, plat_input, game_id)
                    registered.append(f"`{actual}` → `{game_id}`")
                    touched_any = True
                    continue
                if not _looks_like_id_entry(seg):
                    # 🗨️ [신규] 아이디처럼 안 생긴 잡담(예: 그냥 두 단어짜리 문장)은 조용히 무시하고 지나가요.
                    continue
                # 플랫폼을 못 알아봄 → 관리자 확인 대기열로
                touched_any = True
                request_id = str(uuid.uuid4())
                req = {
                    "guild_id": message.guild.id, "user_id": message.author.id,
                    "kind": "platform_only", "raw_text": seg, "game_id": game_id,
                    "timestamp": dt.datetime.now(KST).isoformat(),
                }
                new_pending.append((request_id, req))
            else:
                if not _looks_like_id_entry(seg):
                    # 🗨️ [신규] "#"나 긴 숫자, 알려진 플랫폼 단어 등 아이디스러운 흔적이 전혀 없으면
                    # 그냥 일반 채팅으로 보고 삭제/DM 없이 그대로 둬요.
                    continue
                # 형식 자체를 못 알아봄 → 관리자 확인 대기열로
                touched_any = True
                request_id = str(uuid.uuid4())
                req = {
                    "guild_id": message.guild.id, "user_id": message.author.id,
                    "kind": "full", "raw_text": seg,
                    "timestamp": dt.datetime.now(KST).isoformat(),
                }
                new_pending.append((request_id, req))

        if not touched_any:
            # 이 메세지 전체가 아이디 시도로 안 보이면, 삭제도 DM도 없이 완전히 그냥 넘어가요.
            return

        if registered:
            state.save()
            await self._refresh_id_roster(message.guild)
            # 🐛 [버그 수정] 수동 /아이디 등록은 로그가 남는데, 자동등록 채널은 이 로그 전송이
            # 아예 빠져있었어요. 이제 자동등록도 똑같이 id_log 채널에 기록이 남아요.
            await send_log_embed(
                self.bot, "id_log",
                f"{message.author.mention} 님이 아이디 자동등록 채널에서 등록했어요.",
                fields=[("등록 내역", "\n".join(registered), False)],
                guild=message.guild,
            )

        for request_id, req in new_pending:
            await self._notify_admins_for_clarification(message.guild, message.author, request_id, req)
            pending_data["requests"][request_id] = req
        if new_pending:
            self._save_id_pending(pending_data)

        # 📨 처리 결과를 본인에게 DM으로 짧게 알려드려요 (실패해도 조용히 넘어감)
        try:
            lines = []
            if registered:
                lines.append("✅ **바로 등록됐어요**\n" + "\n".join(registered))
            if new_pending:
                lines.append(f"🔍 플랫폼/형식이 불명확한 항목 {len(new_pending)}개는 관리자 확인 후 등록될 예정이에요.")
            if lines:
                await message.author.send(f"({message.guild.name}) 아이디 등록 처리 결과예요.\n\n" + "\n\n".join(lines))
        except Exception:
            pass

        # 🧹 원본 메세지는 처리 후 삭제 (채널을 깔끔하게 유지)
        try:
            await message.delete()
        except Exception as e:
            print(f"⚠️ 아이디등록 채널 원본 메세지 삭제 실패: {e}")

    @id_group.command(name="등록", description="[관리자] 여러 게임 아이디를 한 번에 등록해요")
    @app_commands.describe(user="아이디를 등록할 유저", entries="플랫폼1 아이디1, 플랫폼2 아이디2, ...")
    @app_commands.guild_only()
    async def register_multi(self, interaction: discord.Interaction, user: discord.User, entries: str):
        if await feature_gate(interaction, "id", "아이디"):
            return
        # 3초 제한 방지를 위해 선제적 defer 선언 (내역 확인 등 안전장치)
        await interaction.response.defer(ephemeral=True)
        
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not self.has_permission(member):
            await interaction.followup.send("❌ 권한이 없어요! 관리자에게 역할을 받아주세요.", ephemeral=True)
            return

        gid, uid = str(interaction.guild.id), str(user.id)
        state.user_ids.setdefault(gid, {}).setdefault(uid, {})

        parts = [e.strip() for e in entries.split(",") if e.strip()]
        if not parts:
            await interaction.followup.send("❌ 등록할 플랫폼·아이디 쌍을 `플랫폼 아이디, ...` 형태로 입력해주세요!", ephemeral=True)
            return

        results = []
        for part in parts:
            parsed = _split_platform_and_id(part)
            if not parsed:
                results.append(f"❌ `{part}` 형식 오류")
                continue

            plat_input, game_id = parsed

            # 🐛 [버그 수정] 예전엔 여기서 정규화·이름 붙이기를 직접 해서, _register_single_id에
            # 들어있는 "이미 똑같은 값이 등록돼 있으면 새 칸을 만들지 않는다" 검사를 건너뛰었어요.
            # 그래서 자동등록 채널로 올리면 중복이 걸러지는데 관리자가 /아이디 등록으로 넣으면
            # 그대로 중복이 쌓이는, 경로마다 결과가 다른 상태였습니다. (그 중복을 나중에
            # /아이디 중복정리로 다시 치우고 있었어요) 이제 두 경로가 같은 함수를 씁니다.
            before = dict(state.user_ids[gid][uid])
            actual = self._register_single_id(gid, uid, plat_input, game_id)

            if actual in before:
                results.append(f"↩️ `{actual}`에 이미 같은 값이 등록돼 있어요 → `{game_id}`")
            else:
                results.append(f"✅ `{actual}` → `{game_id}`")

        state.save()
        summary = "\n".join(results)
        await interaction.followup.send(f"{user.mention}님의 아이디 등록 결과:\n{summary}", ephemeral=True)

        await send_log_embed(
            interaction.client, "id_log",
            f"{interaction.user.mention} 님이 {user.mention}님의 다중 아이디를 등록했어요.",
            fields=[("등록 결과", summary, False)],
            guild=interaction.guild,
        )
        await self._refresh_id_roster(interaction.guild)

    @id_group.command(name="수정", description="[관리자] 등록된 아이디를 수정해요")
    @app_commands.describe(user="수정할 유저", platform=f"플랫폼명 (비워두면 {bot_name()}{josa(bot_name(), '이가')} 목록 보여줘, 자동완성으로 이 유저가 등록해둔 플랫폼이 힌트로 떠요)", game_id="새 아이디")
    @app_commands.guild_only()
    async def modify_id(self, interaction: discord.Interaction, user: discord.User, platform: Optional[str] = None, game_id: str = ""):
        if await feature_gate(interaction, "id", "아이디"):
            return
        # 3초 초과(애플리케이션이 응답하지 않습니다) 에러 방지 선언
        await interaction.response.defer(ephemeral=True)

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not self.has_permission(member):
            await interaction.followup.send("❌ 권한이 없어요! 관리자에게 역할을 받아주세요.", ephemeral=True)
            return

        gid, uid = str(interaction.guild.id), str(user.id)
        state.user_ids.setdefault(gid, {}).setdefault(uid, {})

        if platform is None:
            keys = list(state.user_ids[gid][uid].keys())
            if not keys:
                await interaction.followup.send(f"❌ {user.mention}님은 등록된 아이디가 없어요!", ephemeral=True)
                return
            lines = [f"• {k}: {state.user_ids[gid][uid][k]}" for k in sorted(keys)]
            embed = discord.Embed(title="🔧 수정 가능한 항목", description="\n".join(lines) + "\n\n수정할 플랫폼명을 다시 입력해 주세요.", color=discord.Color.orange())
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # 🐛 [버그 수정] game_id의 기본값이 빈 문자열("")이라, 플랫폼만 넣고 새 아이디를 빼먹으면
        # 그대로 **빈 값이 저장**됐어요. 그런데 디스코드는 임베드 필드 값이 비어 있으면 400으로
        # 거부합니다. 그래서 그 사람의 `/아이디 조회`(와 멤버 우클릭 '아이디 보기')가 그때부터
        # 통째로 안 뜨게 돼요. 아이디 하나가 빈 게 아니라 카드 전체가 실패합니다.
        # 애초에 빈 값이 들어가지 않게 여기서 막아요. (지우려면 `/아이디 삭제`를 쓰면 됩니다)
        game_id = (game_id or "").strip()
        if not game_id:
            await interaction.followup.send(
                "❌ 새 아이디(`game_id`)를 입력해 주세요. 비워두면 수정할 내용이 없어요.\n"
                "└ 항목을 아예 없애려면 `/아이디 삭제`를 써주세요.",
                ephemeral=True,
            )
            return

        if platform == "기타":
            existing = [k for k in state.user_ids[gid][uid] if k.startswith("기타") and k[2:].isdigit()]
            if not existing:
                await interaction.followup.send(f"❌ {user.mention}님은 등록된 기타 플랫폼이 없어요!", ephemeral=True)
                return

            misc_list = "\n".join(f"• {p}: {state.user_ids[gid][uid][p]}" for p in sorted(existing))
            embed = discord.Embed(title="🔧 기타 플랫폼 수정", description=f"{user.mention}님의 기타 플랫폼:\n{misc_list}\n\n몇 번을 수정할까요? (숫자만 입력)", color=discord.Color.orange())
            embed.set_footer(text="30초 내에 채팅창에 숫자를 입력해 주세요")
            
            # 안내 메시지 역시 유저 개인에게만 보이도록 복구용 followup 사용
            await interaction.followup.send(content=interaction.user.mention, embed=embed, ephemeral=True)

            def check(m):
                return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id and m.content.isdigit()

            try:
                msg = await self.bot.wait_for("message", check=check, timeout=30)
            except asyncio.TimeoutError:
                await interaction.followup.send("⏰ 시간이 초과되어 수정이 취소됐어요!", ephemeral=True)
                return

            target = f"기타{msg.content.strip()}"
            if target not in state.user_ids[gid][uid]:
                await interaction.followup.send(f"❌ `{target}` 은(는) 등록되어 있지 않아요!", ephemeral=True)
                return

            old = state.user_ids[gid][uid][target]
            state.user_ids[gid][uid][target] = game_id
            state.save()
            
            await respond_modify(interaction, user, target, old, game_id, is_misc=True)
            await notify_log(interaction, user, target, old, game_id)
            await self._refresh_id_roster(interaction.guild)
            return

        candidates = get_platform_candidates(state.user_ids[gid][uid], platform)
        if not candidates:
            await interaction.followup.send(f"❌ {user.mention}님의 `{platform}` 아이디가 등록되어 있지 않아요!", ephemeral=True)
            return

        if len(candidates) == 1:
            target, old = candidates[0]
            state.user_ids[gid][uid][target] = game_id
            state.save()
            await respond_modify(interaction, user, target, old, game_id, is_misc=False)
            await notify_log(interaction, user, target, old, game_id)
            await self._refresh_id_roster(interaction.guild)
            return

        text = "\n".join(f"{i+1}. {k}: {v}" for i, (k, v) in enumerate(candidates))
        embed = discord.Embed(title="🔧 수정할 항목 선택", description=f"{user.mention}님의 `{platform}` 항목이 여러 개예요.\n\n{text}\n\n몇 번을 수정할까요?", color=discord.Color.orange())
        embed.set_footer(text="30초 내에 숫자를 입력해 주세요")
        await interaction.followup.send(content=interaction.user.mention, embed=embed, ephemeral=True)

        def check(m):
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id and m.content.isdigit()

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=30)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ 시간이 초과되어 수정이 취소됐어요!", ephemeral=True)
            return

        idx = int(msg.content.strip()) - 1
        if idx < 0 or idx >= len(candidates):
            await interaction.followup.send("❌ 올바른 번호가 아니에요!", ephemeral=True)
            return

        target, old = candidates[idx]
        state.user_ids[gid][uid][target] = game_id
        state.save()
        await respond_modify(interaction, user, target, old, game_id, is_misc=False)
        await notify_log(interaction, user, target, old, game_id)
        await self._refresh_id_roster(interaction.guild)

    # 🐛 [버그 수정] 예전엔 platform이 고정된 10개짜리 드롭다운이라, "닌텐도"·"GTA"·"군번" 같은
    # 자유롭게 등록된 플랫폼은 목록에 없어서 수정 자체가 불가능했어요. 이제는 자유 텍스트 +
    # "이 유저가 실제로 등록해둔 플랫폼"을 자동완성 힌트로 보여주는 방식으로 바꿨어요.
    @modify_id.autocomplete('platform')
    async def modify_id_platform_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not interaction.guild:
            return []
        target_user = interaction.namespace.user
        if not target_user:
            return []
        gid, uid = str(interaction.guild.id), str(target_user.id)
        keys = state.user_ids.get(gid, {}).get(uid, {}).keys()
        current_lower = current.lower()
        return [app_commands.Choice(name=k, value=k) for k in keys if current_lower in k.lower()][:25]

    @id_group.command(name="삭제", description="[관리자] 등록된 아이디를 삭제해요")
    @app_commands.describe(user="삭제할 유저 또는 '탈퇴자'", platform="플랫폼명 (탈퇴자일 경우 생략 가능, 자동완성으로 이 유저가 등록해둔 플랫폼이 힌트로 떠요)")
    @app_commands.guild_only()
    async def delete_id(self, interaction: discord.Interaction, user: str, platform: Optional[str] = None):
        if await feature_gate(interaction, "id", "아이디"):
            return
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not self.has_permission(member):
            await interaction.response.send_message("❌ 권한이 없어요! 관리자에게 역할을 받아주세요.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        gid = str(interaction.guild.id)

        if user == "탈퇴자":
            info = state.user_ids.get(gid, {})
            leavers = [(uid, plats) for uid, plats in info.items() if interaction.guild.get_member(int(uid)) is None]
            if not leavers:
                await interaction.followup.send("탈퇴한 유저가 없어요! 🎉")
                return

            # 🐛 [버그 수정] 예전엔 탈퇴자 전원을 임베드 필드로 하나씩 붙였어요. 임베드 필드는
            # 25개가 한계라, 탈퇴자가 26명을 넘으면 이 확인 화면 자체가 400 에러로 안 떠서
            # 정리 자체를 못 하는 상태가 됐습니다. 여기는 조회가 아니라 "지울까요?" 확인
            # 화면이라 페이지를 넘길 데가 없으니, 미리보기만 25명으로 자르고 삭제는 전원 그대로 합니다.
            embed = discord.Embed(title="🚪 탈퇴자 목록", description=f"{len(leavers)}명의 탈퇴자 정보가 발견됐어요.", color=discord.Color.orange())
            for uid, plats in leavers[:self.PAGE_SIZE]:
                value = "\n".join(f"{p}: {v}" for p, v in plats.items()) or "(등록된 아이디 없음)"
                embed.add_field(name=f"탈퇴자 ({uid})", value=value[:1024], inline=False)
            hidden = len(leavers) - self.PAGE_SIZE
            if hidden > 0:
                embed.description += f"\n(화면에는 {self.PAGE_SIZE}명만 보여요. **삭제는 {len(leavers)}명 전원**에게 적용됩니다)"
            embed.set_footer(text="삭제할까요? (응/아니)")
            await interaction.channel.send(embed=embed)

            def chk(m):
                return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id and m.content.strip() in ("응", "아니")

            try:
                resp = await self.bot.wait_for("message", check=chk, timeout=30)
            except asyncio.TimeoutError:
                await interaction.followup.send("⏰ 시간이 초과되어 취소됐어요!")
                return

            if resp.content.strip() == "응":
                for uid, _ in leavers:
                    del state.user_ids[gid][uid]
                state.save()
                await interaction.followup.send(f"✅ {len(leavers)}명의 탈퇴자 정보를 모두 삭제했어요! 🧹💖")
                await send_log_embed(
                    self.bot, "id_log",
                    f"{interaction.user.mention} 님이 탈퇴자 {len(leavers)}명을 삭제했어요.",
                    guild=interaction.guild,
                )
                await self._refresh_id_roster(interaction.guild)
            else:
                await interaction.followup.send("취소했어요! 😊")
            return

        if platform is None:
            await interaction.followup.send("❌ 탈퇴자가 아닐 때는 플랫폼을 꼭 지정해야 해요!")
            return

        uid = extract_id_from_mention(user) or ""
        if not uid.isdigit():
            await interaction.followup.send("유저 멘션을 올바르게 입력해 주세요!")
            return

        if platform == "기타":
            existing = [k for k in state.user_ids.get(gid, {}).get(uid, {}) if k.startswith("기타") and k[2:].isdigit()]
            if not existing:
                await interaction.followup.send(f"❌ <@{uid}>님은 등록된 기타 플랫폼이 없어요!")
                return

            misc_list = "\n".join(f"• {p}: {state.user_ids[gid][uid][p]}" for p in sorted(existing))
            embed = discord.Embed(title="🗑️ 기타 플랫폼 삭제", description=f"<@{uid}>님의 기타 플랫폼:\n{misc_list}\n\n어떤 번호를 삭제할까요? (숫자만 입력)", color=discord.Color.red())
            embed.set_footer(text="30초 내에 숫자를 입력해 주세요")
            await interaction.channel.send(embed=embed)

            def check(m):
                return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id and m.content.isdigit()

            try:
                msg = await self.bot.wait_for("message", check=check, timeout=30)
            except asyncio.TimeoutError:
                await interaction.followup.send("⏰ 시간이 초과되어 삭제가 취소됐어요!")
                return

            target = f"기타{msg.content.strip()}"
            if target not in state.user_ids[gid][uid]:
                await interaction.followup.send(f"❌ `{target}` 은(는) 등록되어 있지 않아요!")
                return

            del state.user_ids[gid][uid][target]
            state.save()
            await interaction.followup.send(f"✅ <@{uid}>님의 `{target}` 아이디를 삭제했어요! 🗑️")
            await send_log_embed(
                self.bot, "id_log",
                f"{interaction.user.mention} 님이 <@{uid}>님의 `{target}` 아이디를 삭제했어요.",
                guild=interaction.guild,
            )
            await self._refresh_id_roster(interaction.guild)
            return

        candidates = get_platform_candidates(state.user_ids.get(gid, {}).get(uid, {}), platform)
        if not candidates:
            await interaction.followup.send("조건에 맞는 등록 정보가 없어요!")
            return

        if len(candidates) == 1:
            target, _ = candidates[0]
            del state.user_ids[gid][uid][target]
            state.save()
            await interaction.followup.send(f"✅ <@{uid}>님의 `{target}` 아이디가 삭제되었어요.")
            await self._refresh_id_roster(interaction.guild)
            return

        text = "\n".join(f"{i+1}. {k}: {v}" for i, (k, v) in enumerate(candidates))
        embed = discord.Embed(title="🗑️ 삭제할 항목 선택", description=f"<@{uid}>님의 `{platform}` 항목이 여러 개예요.\n\n{text}\n\n몇 번을 삭제할까요?", color=discord.Color.red())
        embed.set_footer(text="30초 내에 숫자를 입력해 주세요")
        await interaction.channel.send(embed=embed)

        def check(m):
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id and m.content.isdigit()

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=30)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ 시간이 초과되어 삭제가 취소됐어요!")
            return

        idx = int(msg.content.strip()) - 1
        if idx < 0 or idx >= len(candidates):
            await interaction.followup.send("❌ 올바른 번호가 아니에요!")
            return

        target, _ = candidates[idx]
        del state.user_ids[gid][uid][target]
        state.save()
        await interaction.followup.send(f"✅ <@{uid}>님의 `{target}` 아이디가 삭제되었어요.")
        await self._refresh_id_roster(interaction.guild)

    # 🐛 [버그 수정] user 파라미터가 일반 텍스트라 디스코드 클라이언트가 채널에 캐싱된
    # 사람만 멘션 힌트로 보여주고 있었어요. 자동완성을 직접 붙여서 서버 전체 멤버를
    # 검색해서 보여주도록 고쳤어요. (선택하면 실제 멘션 문자열이 자동으로 채워져요)
    @delete_id.autocomplete('user')
    async def delete_id_user_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        choices = []
        current_lower = current.lower().strip()

        if not current_lower or current_lower in "탈퇴자":
            choices.append(app_commands.Choice(name="🚪 탈퇴자 (탈퇴한 유저 일괄 정리)", value="탈퇴자"))

        if interaction.guild:
            for m in interaction.guild.members:
                if m.bot:
                    continue
                if current_lower in m.display_name.lower() or current_lower in m.name.lower():
                    choices.append(app_commands.Choice(name=f"{m.display_name} ({m.name})", value=f"<@{m.id}>"))
                if len(choices) >= 25:
                    break

        return choices[:25]

    # 🐛 [버그 수정] 등록수정과 같은 이유로, platform 자동완성도 고정 목록 대신
    # "이 유저가 실제로 등록해둔 플랫폼"을 힌트로 보여주도록 바꿨어요.
    @delete_id.autocomplete('platform')
    async def delete_id_platform_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not interaction.guild:
            return []
        user_str = interaction.namespace.user
        if not user_str or user_str == "탈퇴자":
            return []
        uid = extract_id_from_mention(user_str)
        if not uid:
            return []
        gid = str(interaction.guild.id)
        keys = state.user_ids.get(gid, {}).get(uid, {}).keys()
        current_lower = current.lower()
        return [app_commands.Choice(name=k, value=k) for k in keys if current_lower in k.lower()][:25]

    @id_group.command(name="조회", description="개별 사용자의 등록 아이디 확인")
    @app_commands.describe(user="확인할 유저")
    @app_commands.guild_only()
    async def check_id(self, interaction: discord.Interaction, user: discord.User):
        # 🖱️ 화면 만드는 건 우클릭 메뉴와 공유해요. (build_id_embed 참고)
        embed = self.build_id_embed(user, interaction.guild)
        if embed is None:
            await interaction.response.send_message(f"{user.mention}님의 등록된 아이디가 없어요!", ephemeral=True)
            return
        await interaction.response.send_message(embed=embed)

    @id_group.command(name="전체조회", description="[관리자] 서버 전체 등록 정보를 25명씩 나눠서 확인해요")
    @app_commands.describe(페이지="볼 페이지 번호 (1부터, 생략하면 첫 페이지)")
    @app_commands.guild_only()
    async def all_ids(self, interaction: discord.Interaction, 페이지: int = 1):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ 관리자만 사용할 수 있어요!", ephemeral=True)
            return

        await interaction.response.defer()
        gid = str(interaction.guild.id)
        info = state.user_ids.get(gid, {})
        if not info:
            await interaction.followup.send("등록된 정보가 없어요!", ephemeral=True)
            return

        # 🐛 [버그 수정] 예전엔 등록된 사람 전원을 임베드 필드로 하나씩 붙였어요. 디스코드는
        # 임베드 하나에 필드를 25개까지만 허용해서, 등록 인원이 26명을 넘어선 뒤로 이 명령어는
        # 400 에러가 나며 아무것도 못 보여주는 상태였습니다. (지금 이 서버는 70명)
        # 이제 25명씩 페이지로 끊어서 보여줘요.
        entries = list(info.items())
        total_pages = max(1, -(-len(entries) // self.PAGE_SIZE))    # 올림 나눗셈
        page = max(1, min(페이지, total_pages))
        start = (page - 1) * self.PAGE_SIZE

        embed = discord.Embed(
            title=f"📋 서버 전체 등록 게임 아이디 ({page}/{total_pages} 페이지)",
            color=discord.Color.teal(),
        )

        page_entries = entries[start:start + self.PAGE_SIZE]
        used = len(embed.title)
        shown = 0
        for uid, plats in page_entries:
            member = interaction.guild.get_member(int(uid))
            name = member.display_name if member else f"탈퇴자 ({uid})"
            value = "\n".join(f"{p}: {v}" for p, v in plats.items()) or "(등록된 아이디 없음)"
            value = value[:1024]     # 필드 값 하나의 상한
            if used + len(name) + len(value) > self.EMBED_TOTAL_LIMIT:
                break                # 6000자 총량에 먼저 걸린 경우 (아이디를 아주 많이 등록한 사람들)
            embed.add_field(name=name, value=value, inline=False)
            used += len(name) + len(value)
            shown += 1

        footer = f"총 {len(entries)}명 · 이 페이지 {start + 1}~{start + shown}번째"
        if page < total_pages:
            footer += f" · 다음 장: /아이디 전체조회 페이지:{page + 1}"
        elif total_pages > 1:
            footer += " · 마지막 페이지예요"
        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed)



    @commands.Cog.listener()
    async def on_ready(self):
        # 🔁 재연결 때마다 기동 로그를 다시 찍지 않도록 한 번만 출력합니다.
        if self._boot_log_printed:
            return
        self._boot_log_printed = True

        try:
            print(f"✅ {bot_name()}봇이 깨어났어요: {self.bot.user} (ID: {self.bot.user.id})")
            self._print_data_status()
        except Exception as e:
            print("❗ on_ready 초기 출력 중 오류:", type(e).__name__, repr(e))

    @staticmethod
    def _fmt_size(n: float) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{int(n):,} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
            n /= 1024

    def _print_data_status(self):
        """기동 시 데이터 폴더 위치와 파일 상태를 요약해서 보여줍니다.

        예전엔 파일 9개의 전체 경로를 한 줄씩 찍었어요. 문제가 세 가지였습니다.
          ① 파일이 전부 같은 폴더에 있는데 똑같이 긴 경로가 9번 반복돼서,
             정작 제일 중요한 정보(어느 폴더를 쓰고 있는가)가 눈에 안 들어왔어요.
          ② 그 뒤로 늘어난 파일 13개(캠프 통장·견학·원장·프로필·채팅기억 등)가 빠져 있었어요.
             새 데이터 파일을 만들 때마다 여기에 손으로 한 줄씩 추가해야 하는 구조였거든요.
          ③ 경로만 찍고 파일이 실제로 있는지·읽히는지는 확인하지 않아서,
             "경로 확인"이라는 목적 자체를 절반만 달성하고 있었어요.

        이제 mari_config.json_data_files()로 전부 자동 수집하므로 새 파일을 추가해도
        여기를 손볼 필요가 없고, 폴더는 한 번만 찍고 파일은 존재 여부·용량으로 요약합니다.
        """
        data_dir = os.path.abspath(DATA_DIR)
        print(f"📁 데이터 폴더: {data_dir}")

        present, total_bytes = 0, 0
        missing, empty = [], []

        for path in sorted(json_data_files().values(), key=os.path.basename):
            base = os.path.basename(path)
            try:
                size = os.path.getsize(path)
            except OSError:
                missing.append(base)     # 아직 안 만들어진 파일 (정상일 수 있어요)
                continue
            present += 1
            total_bytes += size
            if size == 0:
                empty.append(base)

        total_files = present + len(missing)
        print(f"   📄 데이터 파일 {present}/{total_files}개 · 합계 {self._fmt_size(total_bytes)}")

        if missing:
            print(f"   ⚪ 아직 없는 파일 {len(missing)}개 (처음 쓸 때 자동 생성돼요): {', '.join(missing)}")
        if empty:
            # 0바이트는 저장이 중간에 끊겼을 때 남는 흔적이라 그냥 넘기면 안 돼요.
            print(f"   🔴 내용이 비어 있는 파일 {len(empty)}개 — 확인이 필요해요: {', '.join(empty)}")
