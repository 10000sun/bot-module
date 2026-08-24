"""MariWelcome — 입장 자동화. 새로 들어온 사람에게 역할을 달아주고 인사합니다.

서버 하나를 굴릴 때 사람 손이 제일 많이 가는 자리예요. 새 멤버가 들어올 때마다
관리자가 역할을 달아주고 인사를 붙이는 걸 자동으로 대신합니다.

담고 있는 것 —
  · 입장하면 정해둔 역할을 자동으로 부여
  · 환영 채널에 인사 (문구는 서버가 직접 정해요)
  · 입장·퇴장을 로그 채널에 기록
  · 규칙 동의 버튼 — 누른 사람에게만 역할을 주는 잠금 장치 (선택)

🔐 여기서도 권한 상승을 막습니다. 자동 역할과 동의 역할은 **본인 확인 없이 붙는**
   역할이라, 관리자 권한이 딸린 역할을 담으면 아무나 들어와서 서버를 가져가요.
   셀프 역할과 같은 검사를 씁니다. (mari_utils.role_reject_reason — 두 코그가 공유해요)

⚠️ 이 기능은 **SERVER MEMBERS INTENT**가 있어야 동작해요. 없으면 입장 자체가
   봇에게 안 들어옵니다. (README 2번 — 어차피 없으면 봇이 기동조차 못 해요)
"""

import discord
from discord import app_commands
from discord.ext import commands

from mari_names import server_name
from mari_settings import (feature_gate, has_admin_or_role, is_feature_enabled,
                           load_settings, send_log_embed)
from mari_state import load_welcome, save_welcome
from mari_utils import MariView, role_reject_reason

# 📝 환영 문구에 넣을 수 있는 자리표시자. 여기 없는 글자는 그대로 나갑니다.
#    (설명을 한 곳에서만 만들려고 표로 뒀어요 — 명령 설명과 안내문이 같이 따라옵니다)
PLACEHOLDERS = {
    "{멘션}": "새 멤버를 멘션해요 (알림이 갑니다)",
    "{이름}": "새 멤버의 표시 이름",
    "{서버}": "서버 이름",
    "{인원}": "지금 서버 인원 수",
}

DEFAULT_MESSAGE = "{멘션} 님, {서버}에 오신 걸 환영해요! 🎉 (지금 {인원}번째 멤버예요)"

MAX_AUTO_ROLES = 10


def _fill(template: str, member: discord.Member) -> str:
    """환영 문구의 자리표시자를 실제 값으로 바꿉니다."""
    return (template
            .replace("{멘션}", member.mention)
            .replace("{이름}", member.display_name)
            .replace("{서버}", member.guild.name)
            .replace("{인원}", str(member.guild.member_count or 0)))


class RuleAgreeView(MariView):
    """규칙 동의 버튼. timeout=None이라 봇을 재시작해도 계속 눌려요."""

    def __init__(self, cog: "MariWelcome", label: str = "규칙에 동의합니다"):
        super().__init__(timeout=None)
        self.cog = cog
        button = discord.ui.Button(
            style=discord.ButtonStyle.success, label=label[:80],
            emoji="✅", custom_id="welcome:agree",
        )
        button.callback = self._agree
        self.add_item(button)

    async def _agree(self, interaction: discord.Interaction):
        await self.cog.handle_agree(interaction)


class MariWelcome(commands.Cog):
    """입장 자동화 — 자동 역할·환영 인사·입퇴장 기록·규칙 동의."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        data = load_welcome()
        msg_id = data.get("rule_message_id")
        if msg_id:
            try:
                self.bot.add_view(RuleAgreeView(self, data.get("rule_button_label") or "규칙에 동의합니다"),
                                  message_id=int(msg_id))
                print("🚪 규칙 동의 버튼 등록 완료!")
            except Exception as e:
                print(f"❗ 규칙 동의 버튼 등록 실패: {e}")

    # ---------- 입장·퇴장 ----------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or not is_feature_enabled("welcome"):
            return
        data = load_welcome()

        # 🎭 자동 역할. 규칙 동의를 켜뒀다면 동의하기 전까지는 안 붙여요.
        if not data.get("rule_message_id"):
            await self._grant(member, data.get("auto_roles", []), "입장 자동 역할")

        template = data.get("message")
        if template:
            ch_id = load_settings().get("channels", {}).get("welcome")
            channel = self.bot.get_channel(ch_id) if ch_id else None
            if channel:
                try:
                    await channel.send(_fill(template, member))
                except Exception as e:
                    print(f"❗ 환영 인사 전송 실패: {type(e).__name__}: {e}")

        await send_log_embed(
            self.bot, "member_log", f"📥 **{member.display_name}** 님이 들어왔어요",
            fields=[("멤버", f"{member} (`{member.id}`)", True),
                    ("지금 인원", f"{member.guild.member_count}명", True)],
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot or not is_feature_enabled("welcome"):
            return
        await send_log_embed(
            self.bot, "member_log", f"📤 **{member.display_name}** 님이 나갔어요",
            fields=[("멤버", f"{member} (`{member.id}`)", True),
                    ("지금 인원", f"{member.guild.member_count}명", True)],
        )

    async def _grant(self, member: discord.Member, role_ids: list, reason: str) -> list:
        """역할을 실제로 달아줍니다. 달아준 역할 목록을 돌려줘요.

        ⚠️ 하나가 실패해도 나머지는 붙여야 해요. 역할 하나가 봇보다 위에 있다는 이유로
           나머지 역할까지 통째로 안 붙으면, 새 멤버가 아무 채널도 못 보게 됩니다.
        """
        granted = []
        for rid in role_ids:
            role = member.guild.get_role(int(rid))
            if role is None:
                continue
            try:
                await member.add_roles(role, reason=reason)
                granted.append(role)
            except Exception as e:
                print(f"❗ 자동 역할 부여 실패 ({role.name}): {type(e).__name__}: {e}")
                await send_log_embed(
                    self.bot, "member_log",
                    f"🪜 **{member.display_name}** 님에게 {role.mention} 역할을 못 붙였어요.\n"
                    "봇 역할이 그 역할보다 아래에 있는지 확인해 주세요.",
                )
        return granted

    async def handle_agree(self, interaction: discord.Interaction):
        """규칙 동의 버튼을 눌렀을 때."""
        if not is_feature_enabled("welcome"):
            return await interaction.response.send_message("🚧 입장 기능이 잠시 정지돼 있어요.", ephemeral=True)

        data = load_welcome()
        role_ids = data.get("auto_roles", [])
        if not role_ids:
            return await interaction.response.send_message(
                "❌ 아직 지급할 역할이 정해지지 않았어요. 관리자에게 알려주세요.", ephemeral=True)

        already = [rid for rid in role_ids if interaction.user.get_role(int(rid))]
        if len(already) == len(role_ids):
            return await interaction.response.send_message("이미 동의하셨어요! 🙂", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        granted = await self._grant(interaction.user, role_ids, "규칙 동의")
        if not granted:
            return await interaction.followup.send(
                "🪜 역할을 붙이지 못했어요. 관리자에게 알려주세요. (봇 역할 위치 문제일 수 있어요)", ephemeral=True)
        await interaction.followup.send(
            f"✅ 환영해요! {', '.join(r.mention for r in granted)} 역할을 드렸어요.", ephemeral=True)
        await send_log_embed(
            self.bot, "member_log", f"✅ **{interaction.user.display_name}** 님이 규칙에 동의했어요",
            fields=[("받은 역할", ", ".join(r.mention for r in granted), False)],
        )

    # ---------- 설정 명령 ----------

    입장 = app_commands.Group(name="입장", description="새로 들어온 멤버에게 줄 역할과 환영 인사를 설정합니다.")

    def _admin_only(self, interaction: discord.Interaction) -> bool:
        return has_admin_or_role(interaction, "welcome_admin")

    async def _guard(self, interaction: discord.Interaction) -> bool:
        """정지됐거나 권한이 없으면 True. (True면 호출한 쪽이 그냥 돌아가면 돼요)"""
        if await feature_gate(interaction, "welcome", "입장"):
            return True
        if not self._admin_only(interaction):
            await interaction.response.send_message("⛔ 입장 설정을 다룰 권한이 없어요!", ephemeral=True)
            return True
        return False

    @입장.command(name="자동역할", description="[관리자] 새로 들어온 멤버에게 자동으로 붙일 역할을 추가/제거해요.")
    @app_commands.describe(역할="추가/제거할 역할", 동작="추가 또는 제거 (기본값: 추가)")
    @app_commands.choices(동작=[app_commands.Choice(name="추가", value="add"),
                               app_commands.Choice(name="제거", value="remove")])
    async def auto_role(self, interaction: discord.Interaction, 역할: discord.Role,
                        동작: app_commands.Choice[str] = None):
        if await self._guard(interaction):
            return

        data = load_welcome()
        roles = data.setdefault("auto_roles", [])
        if (동작.value if 동작 else "add") == "remove":
            if 역할.id not in roles:
                return await interaction.response.send_message(
                    f"❌ {역할.mention} 은(는) 원래 자동 역할이 아니었어요.", ephemeral=True)
            roles.remove(역할.id)
            msg = f"🗑️ {역할.mention} 을(를) 자동 역할에서 뺐어요."
        else:
            # 🔐 자동 역할은 아무나 들어오기만 하면 받는 역할이에요. 셀프 역할과 같은 검사.
            reason = role_reject_reason(역할, interaction.guild.me)
            if reason:
                return await interaction.response.send_message(reason, ephemeral=True)
            if 역할.id in roles:
                return await interaction.response.send_message(
                    f"❌ {역할.mention} 은(는) 이미 자동 역할이에요.", ephemeral=True)
            if len(roles) >= MAX_AUTO_ROLES:
                return await interaction.response.send_message(
                    f"❌ 자동 역할은 {MAX_AUTO_ROLES}개까지만 담을 수 있어요.", ephemeral=True)
            roles.append(역할.id)
            msg = f"✅ {역할.mention} 을(를) 자동 역할로 넣었어요."

        save_welcome(data)
        if roles:
            msg += "\n지금 자동 역할: " + ", ".join(f"<@&{r}>" for r in roles)
        await interaction.response.send_message(msg, ephemeral=True)

    @입장.command(name="인사말", description="[관리자] 환영 채널에 올릴 인사 문구를 정해요. (자리표시자를 쓸 수 있어요)")
    @app_commands.describe(문구=f"예: {DEFAULT_MESSAGE}", 끄기="True로 두면 인사말을 올리지 않아요")
    async def set_message(self, interaction: discord.Interaction, 문구: str = "", 끄기: bool = False):
        if await self._guard(interaction):
            return

        data = load_welcome()
        if 끄기:
            data["message"] = ""
            save_welcome(data)
            return await interaction.response.send_message("🔕 환영 인사를 올리지 않을게요.", ephemeral=True)

        data["message"] = 문구 or DEFAULT_MESSAGE
        save_welcome(data)
        guide = "\n".join(f"`{k}` — {v}" for k, v in PLACEHOLDERS.items())
        await interaction.response.send_message(
            f"✅ 인사말을 정했어요.\n> {data['message']}\n\n**쓸 수 있는 자리표시자**\n{guide}\n"
            "⚠️ `/설정 채널 환영`으로 올릴 채널도 정해주셔야 실제로 나가요.", ephemeral=True)

    @입장.command(name="규칙패널", description="[관리자] 이 채널에 '규칙 동의' 버튼을 올려요. 누른 사람에게만 자동 역할을 줍니다.")
    @app_commands.describe(제목="패널 제목", 내용="규칙 본문", 버튼="버튼에 적을 글자 (생략 가능)")
    async def rule_panel(self, interaction: discord.Interaction, 제목: str, 내용: str, 버튼: str = ""):
        if await self._guard(interaction):
            return
        data = load_welcome()
        if not data.get("auto_roles"):
            return await interaction.response.send_message(
                "❌ 먼저 `/입장 자동역할`로 지급할 역할을 정해주세요. 지금은 눌러도 줄 역할이 없어요.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        label = 버튼 or "규칙에 동의합니다"
        embed = discord.Embed(title=제목, description=내용.replace("\\n", "\n"), color=0x89CFF0)
        embed.set_footer(text="아래 버튼을 누르면 서버를 이용할 수 있어요.")
        message = await interaction.channel.send(embed=embed, view=RuleAgreeView(self, label))

        data["rule_message_id"] = message.id
        data["rule_button_label"] = label
        save_welcome(data)
        self.bot.add_view(RuleAgreeView(self, label), message_id=message.id)
        await interaction.followup.send(
            f"✅ 규칙 패널을 올렸어요. 이제 **동의를 누른 사람에게만** 자동 역할이 나갑니다.\n{message.jump_url}",
            ephemeral=True)

    @입장.command(name="규칙패널끄기", description="[관리자] 규칙 동의 단계를 없애요. (이후로는 들어오자마자 자동 역할을 줍니다)")
    async def rule_off(self, interaction: discord.Interaction):
        if await self._guard(interaction):
            return
        data = load_welcome()
        if not data.get("rule_message_id"):
            return await interaction.response.send_message("❌ 켜져 있는 규칙 패널이 없어요.", ephemeral=True)
        data.pop("rule_message_id", None)
        save_welcome(data)
        await interaction.response.send_message(
            "🔓 규칙 동의 단계를 껐어요. 이제 들어오자마자 자동 역할이 붙어요.\n"
            "(올려둔 패널 메시지는 남아 있어요. 필요 없으면 직접 지워주세요)", ephemeral=True)

    @입장.command(name="확인", description="[관리자] 지금 입장 설정이 어떻게 돼 있는지 한눈에 봐요.")
    async def show(self, interaction: discord.Interaction):
        if await self._guard(interaction):
            return
        data = load_welcome()
        channels = load_settings().get("channels", {})
        roles = ", ".join(f"<@&{r}>" for r in data.get("auto_roles", [])) or "*(없음)*"
        embed = discord.Embed(title="🚪 입장 설정", color=0x89CFF0)
        embed.add_field(name="자동 역할", value=roles, inline=False)
        embed.add_field(name="환영 인사", value=data.get("message") or "*(안 올림)*", inline=False)
        embed.add_field(name="환영 채널",
                        value=f"<#{channels['welcome']}>" if channels.get("welcome") else "*(미지정 — `/설정 채널 환영`)*",
                        inline=True)
        embed.add_field(name="입퇴장 로그",
                        value=f"<#{channels['member_log']}>" if channels.get("member_log") else "*(미지정 — `/설정 채널 입퇴장로그`)*",
                        inline=True)
        embed.add_field(name="규칙 동의",
                        value="켜짐 (동의해야 역할 지급)" if data.get("rule_message_id") else "꺼짐 (들어오면 바로 지급)",
                        inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @입장.command(name="미리보기", description="[관리자] 환영 인사가 어떻게 나가는지 나에게만 보여줘요. (실제로 올리진 않아요)")
    async def preview(self, interaction: discord.Interaction):
        if await self._guard(interaction):
            return
        template = load_welcome().get("message")
        if not template:
            return await interaction.response.send_message(
                "❌ 인사말이 없어요. `/입장 인사말`로 먼저 정해주세요.", ephemeral=True)
        # 🔇 멘션이 실제로 알림을 울리지 않게 막습니다. 미리보기니까요.
        await interaction.response.send_message(
            f"**이렇게 나가요 — {server_name()}**\n{_fill(template, interaction.user)}",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
