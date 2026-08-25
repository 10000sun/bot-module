"""ChunsikSelfRole — 버튼으로 스스로 붙였다 떼는 역할(셀프 역할) 패널.

알림 역할·게임별 역할처럼 "관리자가 일일이 달아줄 필요 없는" 역할을 유저가 직접
켜고 끄게 합니다. 관리자가 패널을 하나 만들고 역할을 담아두면, 유저는 버튼만 누르면 돼요.

🔐 **권한 상승을 막는 게 이 기능의 전부입니다.** 셀프 역할은 "아무나 눌러서 가져가는
   역할"이라, 관리자 권한이 딸린 역할을 실수로 하나 담는 순간 서버가 통째로 넘어가요.
   그래서 담을 때(role_reject_reason)와 누를 때(toggle_role) **두 번** 검사합니다.
   두 검사는 입장 자동 역할과 같은 것이라 chunsik_utils에 함께 뒀어요.
   담은 뒤에 그 역할에 권한이 추가될 수도 있어서, 누를 때 다시 보는 게 꼭 필요해요.
"""

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from chunsik_settings import (feature_gate, has_admin_or_role, is_feature_enabled,
                           send_log_embed)
from chunsik_state import load_selfroles, save_selfroles
from chunsik_utils import (EMBED_DESC_LIMIT, EMBED_FIELD_LIMIT,
                        EMBED_TITLE_LIMIT, ChunsikView, button_emoji_error, clip,
                        dangerous_permission, role_reject_reason,
                        safe_button_emoji)

# 📏 디스코드 제한 — 한 메시지에 버튼은 5줄 × 5개까지.
MAX_ROLES_PER_PANEL = 25


class SelfRoleButton(discord.ui.Button):
    """역할 하나를 켜고 끄는 버튼. custom_id에 역할 ID를 담아 재시작에도 살아남아요."""

    def __init__(self, panel_id: str, role_id: int, label: str, emoji: str | None):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label[:80],
            # 🩹 검사를 붙이기 전에 저장된 엉뚱한 값은 여기서 조용히 버립니다. 하나라도
            #    남아 있으면 패널을 다시 그릴 수가 없어서 관리자가 손댈 방법이 없어져요.
            emoji=safe_button_emoji(emoji),
            custom_id=f"selfrole:{panel_id}:{role_id}",
        )
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction):
        await self.view.toggle(interaction, self.role_id)


class SelfRolePanelView(ChunsikView):
    """패널 하나에 붙는 버튼 묶음. timeout=None이라 봇을 재시작해도 계속 눌려요."""

    def __init__(self, cog: "ChunsikSelfRole", panel_id: str, roles: list):
        super().__init__(timeout=None)
        self.cog = cog
        self.panel_id = panel_id
        for entry in roles[:MAX_ROLES_PER_PANEL]:
            self.add_item(SelfRoleButton(
                panel_id, int(entry["id"]), entry.get("label") or "역할", entry.get("emoji"),
            ))

    async def toggle(self, interaction: discord.Interaction, role_id: int):
        await self.cog.toggle_role(interaction, role_id)


class ChunsikSelfRole(commands.Cog):
    """셀프 역할 패널 — 유저가 버튼으로 직접 역할을 붙였다 뗍니다."""

    def __init__(self, bot):
        self.bot = bot
        # 🔒 패널 파일도 "읽고 → 고치고 → 저장"이에요. 관리자 둘이 같은 패널에 역할을
        #    동시에 담으면 한쪽이 조용히 사라집니다. (파티 버튼과 같은 이유)
        self._lock = asyncio.Lock()

    async def cog_load(self):
        self._register_persistent_views()

    def _register_persistent_views(self):
        """저장된 패널마다 버튼을 다시 등록합니다.

        🧩 상점(cogs/shop.py)이 매대 버튼을 되살리는 방식과 같아요. 이 일을 클라이언트
        본체가 하면, 셀프 역할을 빼고 납품했을 때 갈 곳 없는 코드가 남습니다.
        """
        registered = 0
        for panel_id, panel in self._panels().items():
            try:
                self.bot.add_view(
                    SelfRolePanelView(self, panel_id, panel.get("roles", [])),
                    message_id=int(panel_id),
                )
                registered += 1
            except Exception as e:
                print(f"❗ 셀프 역할 패널({panel_id}) 버튼 등록 실패: {e}")
        if registered:
            print(f"🎚️ 셀프 역할 패널 {registered}개 버튼 등록 완료!")

    # ---------- 데이터 ----------

    def _panels(self) -> dict:
        return load_selfroles().get("panels", {})

    def _save_panels(self, panels: dict):
        save_selfroles({"panels": panels})

    def _panel_embed(self, panel: dict) -> discord.Embed:
        lines = [f"{(e.get('emoji') or '•')} **{e.get('label')}** — <@&{e['id']}>"
                 for e in panel.get("roles", [])]
        # ✂️ 제목·설명은 관리자가 자유롭게 적는 자리라 한도를 넘길 수 있어요. 넘으면
        #    패널이 아예 안 올라가고, 그 뒤로는 다시 그릴 수도 없습니다.
        embed = discord.Embed(
            title=clip(panel.get("title") or "셀프 역할", EMBED_TITLE_LIMIT),
            description=clip((panel.get("description") or "") + ("\n\n" + "\n".join(lines) if lines else ""),
                             EMBED_DESC_LIMIT),
            color=0x89CFF0,
        )
        embed.set_footer(text="버튼을 누르면 역할이 붙고, 한 번 더 누르면 떨어져요.")
        return embed

    async def _repaint(self, panel_id: str, panel: dict):
        """패널 메시지의 임베드와 버튼을 지금 저장된 내용으로 다시 그립니다."""
        channel = self.bot.get_channel(int(panel["channel_id"]))
        if channel is None:
            raise RuntimeError("패널이 있던 채널을 찾을 수 없어요. 채널이 지워졌나요?")
        message = await channel.fetch_message(int(panel_id))
        view = SelfRolePanelView(self, panel_id, panel.get("roles", []))
        await message.edit(embed=self._panel_embed(panel), view=view)
        self.bot.add_view(view, message_id=int(panel_id))

    # ---------- 버튼 ----------

    async def toggle_role(self, interaction: discord.Interaction, role_id: int):
        """버튼을 눌렀을 때. 이미 갖고 있으면 떼고, 없으면 붙여요."""
        if not is_feature_enabled("selfrole"):
            return await interaction.response.send_message(
                "🚧 셀프 역할 기능이 잠시 정지돼 있어요.", ephemeral=True)

        member = interaction.user
        guild = interaction.guild
        role = guild.get_role(role_id) if guild else None
        if role is None:
            return await interaction.response.send_message(
                "❌ 그 역할이 서버에서 사라졌어요. 관리자에게 알려주세요.", ephemeral=True)

        # 🔐 담을 때 통과했어도 그 뒤에 권한이 붙었을 수 있어요. 누를 때 다시 봅니다.
        danger = dangerous_permission(role)
        if danger:
            print(f"🚨 셀프 역할 차단: {role.name}({role.id})에 '{danger}' 권한이 생겼어요.")
            return await interaction.response.send_message(
                f"⛔ 이 역할에 **{danger}** 권한이 생겨서 더 이상 셀프로 가져갈 수 없어요.\n"
                "관리자가 패널에서 빼야 합니다.", ephemeral=True)

        try:
            if role in member.roles:
                await member.remove_roles(role, reason="셀프 역할 (본인이 뗌)")
                verb, mark = "뗐어요", "➖"
            else:
                await member.add_roles(role, reason="셀프 역할 (본인이 붙임)")
                verb, mark = "붙였어요", "➕"
        except discord.Forbidden:
            return await interaction.response.send_message(
                f"🪜 봇이 {role.mention} 역할을 다룰 권한이 없어요.\n"
                "서버 설정 → 역할에서 **봇 역할을 그 역할보다 위로** 올려주세요.", ephemeral=True)

        await interaction.response.send_message(f"{mark} {role.mention} 역할을 {verb}.", ephemeral=True)
        await send_log_embed(
            self.bot, "role_log", f"{mark} 셀프 역할 — {member.mention} 이(가) 직접 {verb}",
            fields=[("역할", role.mention, True), ("멤버", f"{member} (`{member.id}`)", True)],
        )

    # ---------- 관리 명령 ----------

    셀프역할 = app_commands.Group(name="셀프역할", description="유저가 버튼으로 직접 붙였다 뗄 수 있는 역할 패널을 관리합니다.")

    def _admin_only(self, interaction: discord.Interaction) -> bool:
        return has_admin_or_role(interaction, "selfrole_admin")

    async def panel_autocomplete(self, interaction: discord.Interaction, current: str):
        """지금 있는 패널을 골라주는 자동완성. 값은 메시지 ID예요."""
        options = []
        for panel_id, panel in self._panels().items():
            name = f"{panel.get('title') or '제목 없음'} (#{getattr(self.bot.get_channel(int(panel['channel_id'])), 'name', '알 수 없음')})"
            if current.lower() in name.lower():
                options.append(app_commands.Choice(name=name[:100], value=panel_id))
        return options[:25]

    @셀프역할.command(name="만들기", description="[관리자] 이 채널에 셀프 역할 패널을 새로 올려요. (역할은 만든 뒤에 담습니다)")
    @app_commands.describe(제목="패널 제목 (예: 알림 역할)", 설명="패널에 적을 안내 문구 (생략 가능)")
    async def create_panel(self, interaction: discord.Interaction, 제목: str, 설명: str = ""):
        if await feature_gate(interaction, "selfrole", "셀프 역할"):
            return
        if not self._admin_only(interaction):
            return await interaction.response.send_message("⛔ 셀프 역할을 관리할 권한이 없어요!", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        panel = {"channel_id": interaction.channel.id, "title": 제목, "description": 설명, "roles": []}
        message = await interaction.channel.send(embed=self._panel_embed(panel))

        async with self._lock:
            panels = self._panels()
            panels[str(message.id)] = panel
            self._save_panels(panels)

        await interaction.followup.send(
            f"✅ 패널을 올렸어요. 이제 `/셀프역할 역할추가`로 역할을 담아주세요.\n{message.jump_url}",
            ephemeral=True)

    @셀프역할.command(name="역할추가", description="[관리자] 패널에 유저가 스스로 가져갈 수 있는 역할을 담아요.")
    @app_commands.describe(패널="역할을 담을 패널", 역할="담을 역할", 이름="버튼에 적을 이름 (생략하면 역할 이름)", 이모지="버튼에 붙일 이모지 (생략 가능)")
    @app_commands.autocomplete(패널=panel_autocomplete)
    async def add_role(self, interaction: discord.Interaction, 패널: str, 역할: discord.Role,
                       이름: str = "", 이모지: str = ""):
        if await feature_gate(interaction, "selfrole", "셀프 역할"):
            return
        if not self._admin_only(interaction):
            return await interaction.response.send_message("⛔ 셀프 역할을 관리할 권한이 없어요!", ephemeral=True)

        panels = self._panels()
        panel = panels.get(패널)
        if panel is None:
            return await interaction.response.send_message("❌ 그런 패널이 없어요. 자동완성에서 골라주세요.", ephemeral=True)

        reason = role_reject_reason(역할, interaction.guild.me)
        if reason:
            return await interaction.response.send_message(reason, ephemeral=True)
        # 🙂 이모지 칸은 자유 입력이라 글자가 들어옵니다. discord.py는 검사하지 않고,
        #    디스코드는 그 버튼이 달린 메세지를 통째로 거부해요. 여기서 막습니다.
        emoji_error = button_emoji_error(이모지)
        if emoji_error:
            return await interaction.response.send_message(emoji_error, ephemeral=True)
        if any(int(e["id"]) == 역할.id for e in panel["roles"]):
            return await interaction.response.send_message(f"❌ {역할.mention} 은(는) 이미 담겨 있어요.", ephemeral=True)
        if len(panel["roles"]) >= MAX_ROLES_PER_PANEL:
            return await interaction.response.send_message(
                f"❌ 한 패널에는 역할을 {MAX_ROLES_PER_PANEL}개까지만 담을 수 있어요. (디스코드 제한)\n"
                "패널을 하나 더 만들어서 나눠 담아주세요.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        # 🔒 위에서 읽어둔 panels는 검사를 거치는 사이에 낡았을 수 있어요. 락 안에서
        #    다시 읽어 고칩니다. (관리자 둘이 같은 패널을 동시에 손대면 한쪽이 사라져요)
        async with self._lock:
            panels = self._panels()
            panel = panels.get(패널)
            if panel is None:
                return await interaction.followup.send("❌ 그새 패널이 사라졌어요.", ephemeral=True)
            panel["roles"].append({"id": 역할.id, "label": 이름 or 역할.name, "emoji": 이모지.strip() or None})
            self._save_panels(panels)
        await self._repaint(패널, panel)
        await interaction.followup.send(f"✅ {역할.mention} 을(를) 패널에 담았어요.", ephemeral=True)

    @셀프역할.command(name="역할빼기", description="[관리자] 패널에서 역할을 빼요. (이미 가져간 사람의 역할은 그대로 둡니다)")
    @app_commands.describe(패널="역할을 뺄 패널", 역할="뺄 역할")
    @app_commands.autocomplete(패널=panel_autocomplete)
    async def remove_role(self, interaction: discord.Interaction, 패널: str, 역할: discord.Role):
        if await feature_gate(interaction, "selfrole", "셀프 역할"):
            return
        if not self._admin_only(interaction):
            return await interaction.response.send_message("⛔ 셀프 역할을 관리할 권한이 없어요!", ephemeral=True)

        panels = self._panels()
        panel = panels.get(패널)
        if panel is None:
            return await interaction.response.send_message("❌ 그런 패널이 없어요.", ephemeral=True)
        if not any(int(e["id"]) == 역할.id for e in panel["roles"]):
            return await interaction.response.send_message(f"❌ {역할.mention} 은(는) 그 패널에 없어요.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        async with self._lock:
            panels = self._panels()
            panel = panels.get(패널)
            if panel is None:
                return await interaction.followup.send("❌ 그새 패널이 사라졌어요.", ephemeral=True)
            panel["roles"] = [e for e in panel["roles"] if int(e["id"]) != 역할.id]
            self._save_panels(panels)
        await self._repaint(패널, panel)
        # 💡 이미 가져간 사람의 역할은 일부러 회수하지 않아요. 패널에서 빼는 건 "더 이상
        #    나눠주지 않는다"는 뜻이지, "지금 가진 사람에게서 뺏는다"가 아닙니다.
        await interaction.followup.send(
            f"🗑️ {역할.mention} 을(를) 패널에서 뺐어요. (이미 가진 사람은 그대로예요)", ephemeral=True)

    @셀프역할.command(name="패널삭제", description="[관리자] 패널을 지워요. (이미 가져간 사람의 역할은 그대로 둡니다)")
    @app_commands.describe(패널="지울 패널")
    @app_commands.autocomplete(패널=panel_autocomplete)
    async def delete_panel(self, interaction: discord.Interaction, 패널: str):
        if await feature_gate(interaction, "selfrole", "셀프 역할"):
            return
        if not self._admin_only(interaction):
            return await interaction.response.send_message("⛔ 셀프 역할을 관리할 권한이 없어요!", ephemeral=True)

        if 패널 not in self._panels():
            return await interaction.response.send_message("❌ 그런 패널이 없어요.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        async with self._lock:
            panels = self._panels()
            panel = panels.pop(패널, None)
            if panel is None:
                return await interaction.followup.send("❌ 그새 패널이 사라졌어요.", ephemeral=True)
            self._save_panels(panels)
        # 메시지는 이미 없을 수도 있어요(관리자가 손으로 지운 경우). 그래도 기록은 지워야 합니다.
        try:
            channel = self.bot.get_channel(int(panel["channel_id"]))
            message = await channel.fetch_message(int(패널))
            await message.delete()
        except Exception:
            pass
        await interaction.followup.send("🗑️ 패널을 지웠어요. (이미 가진 사람의 역할은 그대로예요)", ephemeral=True)

    @셀프역할.command(name="목록", description="[관리자] 지금 있는 셀프 역할 패널을 전부 확인해요.")
    async def list_panels(self, interaction: discord.Interaction):
        if await feature_gate(interaction, "selfrole", "셀프 역할"):
            return
        if not self._admin_only(interaction):
            return await interaction.response.send_message("⛔ 셀프 역할을 관리할 권한이 없어요!", ephemeral=True)

        panels = self._panels()
        if not panels:
            return await interaction.response.send_message(
                "아직 패널이 없어요. `/셀프역할 만들기`로 하나 올려보세요.", ephemeral=True)

        embed = discord.Embed(title="🎚️ 셀프 역할 패널", color=0x89CFF0)
        for panel_id, panel in list(panels.items())[:25]:
            roles = ", ".join(f"<@&{e['id']}>" for e in panel.get("roles", [])) or "*(담긴 역할 없음)*"
            embed.add_field(
                name=clip(f"{panel.get('title') or '제목 없음'} — <#{panel['channel_id']}>", EMBED_TITLE_LIMIT),
                value=clip(f"{roles}\n`{panel_id}`", EMBED_FIELD_LIMIT),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

