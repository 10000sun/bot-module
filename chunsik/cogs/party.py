"""ChunsikParty — 파티·레이드 모집. 시간과 인원을 정해 올리면 버튼으로 모입니다.

"오늘 밤 8시 레이드 다섯 명"을 채팅으로 굴리면 누가 온다고 했는지 금세 흩어져요.
모집글 하나에 참가 버튼을 붙이고, 정원이 차면 대기까지 받고, 시작 전에 한 번 불러줍니다.

🆔 **아이디 등록부(id)를 함께 담으면** 참가자 목록에 그 사람의 게임 아이디가 같이
   붙어요. 모이기 전에 친구 추가를 끝낼 수 있습니다. 안 담았으면 이름만 나와요.
   (코그를 import하지 않고 `module_active`로만 봅니다 — 코그끼리는 서로를 몰라요)

⏰ 시작 시각이 되면 자동으로 마감하고 참가자를 한 번 부릅니다. 봇이 꺼져 있던 사이에
   지나간 모집은 다시 켜질 때 조용히 정리돼요. (지난 모집을 뒤늦게 알리지 않습니다)
"""

import asyncio
import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands, tasks

from chunsik_config import KST, module_active
from chunsik_settings import feature_gate, has_admin_or_role, is_feature_enabled
from chunsik_state import load_party, save_party, state
from chunsik_utils import (EMBED_DESC_LIMIT, EMBED_TITLE_LIMIT, MESSAGE_LIMIT,
                        ChunsikView, add_lines_field, clip, mention_list,
                        parse_datetime_text)

# ⏰ 시작 몇 분 전에 부를지. 0이면 시작할 때만 불러요.
REMIND_BEFORE_MINUTES = 10
MAX_OPEN_PARTIES = 25  # /파티 목록 임베드가 감당하는 칸 수와 같아요


def _ids_line(guild_id, user_id) -> str:
    """그 사람의 게임 아이디 한 줄. 아이디 모듈을 안 담았으면 빈 문자열."""
    if not module_active("id"):
        return ""
    entry = state.user_ids.get(str(guild_id), {}).get(str(user_id), {})
    if not entry:
        return ""
    return " · " + " / ".join(f"{platform} `{value}`" for platform, value in list(entry.items())[:3])


class PartyView(ChunsikView):
    """모집글에 붙는 버튼. timeout=None이라 봇을 재시작해도 계속 눌려요."""

    def __init__(self, cog: "ChunsikParty", party_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.party_id = party_id
        for label, emoji, action, style in (
            ("참가", "✅", "join", discord.ButtonStyle.success),
            ("취소", "🚪", "leave", discord.ButtonStyle.secondary),
            ("마감", "🔒", "close", discord.ButtonStyle.danger),
        ):
            button = discord.ui.Button(label=label, emoji=emoji, style=style,
                                       custom_id=f"party:{party_id}:{action}")
            button.callback = self._make(action)
            self.add_item(button)

    def _make(self, action):
        async def callback(interaction: discord.Interaction):
            await self.cog.handle(interaction, self.party_id, action)
        return callback


class ChunsikParty(commands.Cog):
    """파티·레이드 모집 — 참가 버튼, 대기열, 시작 전 호출."""

    def __init__(self, bot):
        self.bot = bot
        # 🔒 파티 기록은 버튼으로 고쳐지는 파일이에요. 모집글이 올라오면 여러 명이 거의
        #    동시에 누르는 게 정상 사용이라, "읽고 → 고치고 → 저장"이 겹치면 참가가
        #    조용히 한 건씩 사라집니다. (지갑이 economy_lock을 쓰는 것과 같은 이유예요)
        self._lock = asyncio.Lock()

    async def cog_load(self):
        parties = self._all()
        for pid, party in parties.items():
            if party.get("closed"):
                continue
            try:
                self.bot.add_view(PartyView(self, pid), message_id=int(pid))
            except Exception as e:
                print(f"❗ 파티 모집({pid}) 버튼 등록 실패: {e}")
        self.tick.start()

    async def cog_unload(self):
        self.tick.cancel()

    # ---------- 데이터 ----------

    def _all(self) -> dict:
        return load_party().get("parties", {})

    def _save(self, parties: dict):
        save_party({"parties": parties})

    def _embed(self, party: dict, guild_id) -> discord.Embed:
        start = dt.datetime.fromisoformat(party["start"])
        joined = party.get("members", [])
        waiting = party.get("waiting", [])
        size = party["size"]

        state_text = "🔒 **마감됐어요**" if party.get("closed") else (
            "🈵 **정원이 찼어요** (지금 누르면 대기)" if len(joined) >= size else "🈳 모집 중")
        embed = discord.Embed(
            # ✂️ 제목·설명은 관리자가 자유롭게 적는 자리예요. 한도를 넘으면 모집글이
            #    아예 안 올라가니 여기서 자릅니다. (chunsik_utils.clip)
            title=clip(f"🎯 {party['title']}", EMBED_TITLE_LIMIT),
            description=clip((party.get("note") or "") + f"\n\n{state_text}", EMBED_DESC_LIMIT),
            color=0x9B59B6 if not party.get("closed") else 0x99AAB5,
        )
        # ⏱️ 디스코드 타임스탬프로 넣으면 보는 사람의 시간대로 알아서 바뀌어요.
        embed.add_field(name="시작", value=f"<t:{int(start.timestamp())}:F>\n<t:{int(start.timestamp())}:R>", inline=True)
        embed.add_field(name="인원", value=f"**{len(joined)}** / {size}", inline=True)
        embed.add_field(name="주최", value=f"<@{party['host']}>", inline=True)

        # 🚨 참가자 명단은 **반드시** add_lines_field로 담습니다. 한 필드에 몰아넣으면
        #    20명(아이디까지 붙으면 1310자)에서 디스코드가 메세지를 거부해요. 그러면
        #    참가는 저장됐는데 모집글만 그 시점에서 얼어붙습니다 — 정원 99명까지
        #    받는 명령이라 반드시 밟게 되는 자리예요.
        add_lines_field(
            embed, f"참가자 {len(joined)}명",
            [f"`{i + 1}.` <@{uid}>{_ids_line(guild_id, uid)}" for i, uid in enumerate(joined)],
            empty="*(아직 없어요)*")
        if waiting:
            add_lines_field(embed, f"대기 {len(waiting)}명", [f"<@{uid}>" for uid in waiting])
        embed.set_footer(text="참가를 누르면 자리를 잡아요. 못 가게 되면 취소를 눌러주세요.")
        return embed

    async def _repaint(self, party_id: str, party: dict):
        channel = self.bot.get_channel(int(party["channel_id"]))
        if channel is None:
            return
        message = await channel.fetch_message(int(party_id))
        view = None if party.get("closed") else PartyView(self, party_id)
        await message.edit(embed=self._embed(party, party.get("guild_id")), view=view)

    # ---------- 버튼 ----------

    @staticmethod
    def _apply(party: dict, uid: int, action: str, is_admin: bool) -> tuple:
        """버튼 하나를 실제로 반영합니다. → (안내 문구, 저장할 것인가, 승급된 사람)

        🚨 **여기에는 await가 하나도 없어야 해요.** 락을 쥔 채로 디스코드에 말을 걸면
           그 왕복(수백 ms) 사이에 다른 사람의 클릭이 끼어들어, 파일에서 방금 읽은
           내용이 낡은 것이 됩니다. 예전엔 대기 승급 DM이 바로 이 자리에 있었어요 —
           모집글이 올라오자마자 여럿이 동시에 누르는 게 정상 사용이라 실제로 밟는 길입니다.
        """
        members, waiting = party.setdefault("members", []), party.setdefault("waiting", [])

        if action == "close":
            if uid != party["host"] and not is_admin:
                return "⛔ 모집을 연 사람이나 파티 관리자만 마감할 수 있어요.", False, None
            party["closed"] = True
            return None, True, None

        if action == "leave":
            if uid not in members and uid not in waiting:
                return "❌ 참가하지 않으셨어요.", False, None
            promoted = None
            if uid in members:
                members.remove(uid)
                # 🎟️ 자리가 나면 대기 1번을 자동으로 올려요. (안 그러면 대기가 영영 안 들어와요)
                if waiting:
                    promoted = waiting.pop(0)
                    members.append(promoted)
            else:
                waiting.remove(uid)
            return "🚪 빠졌어요.", True, promoted

        # join
        if uid in members or uid in waiting:
            return "이미 참가하셨어요! 🙂", False, None
        if len(members) < party["size"]:
            members.append(uid)
            return "✅ 참가했어요!", True, None
        waiting.append(uid)
        return (f"⏳ 정원이 차서 **대기 {len(waiting)}번**으로 넣었어요. 자리가 나면 DM으로 알려드릴게요.",
                True, None)

    async def handle(self, interaction: discord.Interaction, party_id: str, action: str):
        if not is_feature_enabled("party"):
            return await interaction.response.send_message("🚧 파티 모집이 잠시 정지돼 있어요.", ephemeral=True)

        uid = interaction.user.id
        is_admin = has_admin_or_role(interaction, "party_admin")
        changed_party = None

        # 🔒 "읽고 → 고치고 → 저장"은 한 덩어리여야 합니다. 이 안에서는 디스코드에
        #    아무것도 보내지 않아요 — 안내도 DM도 다시 그리기도 전부 락 밖에서 합니다.
        async with self._lock:
            parties = self._all()
            party = parties.get(party_id)
            if party is None or party.get("closed"):
                reply, changed, promoted = "❌ 이미 끝난 모집이에요.", False, None
            else:
                reply, changed, promoted = self._apply(party, uid, action, is_admin)
                if changed:
                    self._save(parties)
                    changed_party = party

        if reply is None:
            await interaction.response.defer()
        else:
            await interaction.response.send_message(reply, ephemeral=True)

        if promoted is not None:
            try:
                user = await self.bot.fetch_user(promoted)
                await user.send(f"🎉 자리가 났어요! **{changed_party['title']}** 참가로 올라갔습니다.")
            except Exception:
                pass  # DM을 막아둔 사람도 있어요. 목록에는 이미 올라가 있으니 괜찮습니다.

        if changed_party is not None:
            await self._repaint(party_id, changed_party)

    # ---------- 시작 시각 챙기기 ----------

    @tasks.loop(minutes=1)
    async def tick(self):
        if not is_feature_enabled("party"):
            return
        now = dt.datetime.now(KST)
        # 📣 부를 것을 먼저 **락 안에서** 정하고, 실제로 말을 거는 건 락 밖에서 합니다.
        #    (예전엔 알림을 보내는 동안 파일이 열려 있어서, 그 사이 참가 클릭이 지워졌어요)
        todo = []
        async with self._lock:
            parties = self._all()
            changed = False
            for pid, party in list(parties.items()):
                if party.get("closed"):
                    continue
                start = dt.datetime.fromisoformat(party["start"])

                if (not party.get("reminded") and REMIND_BEFORE_MINUTES
                        and 0 < (start - now).total_seconds() <= REMIND_BEFORE_MINUTES * 60):
                    party["reminded"] = True
                    changed = True
                    todo.append((pid, dict(party),
                                 f"⏰ **{party['title']}** 시작 {REMIND_BEFORE_MINUTES}분 전이에요!", False))

                if now >= start:
                    party["closed"] = True
                    changed = True
                    # 🕰️ 봇이 꺼져 있던 사이에 지나간 모집은 조용히 닫기만 해요.
                    #    한참 지난 모집을 이제 와서 부르면 아무 도움이 안 됩니다.
                    late = (now - start).total_seconds() >= 300
                    todo.append((pid, dict(party),
                                 None if late else f"🎯 **{party['title']}** 시작할 시간이에요!", True))
            if changed:
                self._save(parties)

        for pid, party, text, repaint in todo:
            channel = self.bot.get_channel(int(party["channel_id"]))
            if channel and text and party.get("members"):
                try:
                    await channel.send(clip(text + " " + mention_list(party["members"]), MESSAGE_LIMIT))
                except Exception as e:
                    print(f"❗ 파티 알림 실패: {type(e).__name__}: {e}")
            if repaint:
                try:
                    await self._repaint(pid, party)
                except Exception:
                    pass

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    # ---------- 명령 ----------

    파티 = app_commands.Group(name="파티", description="시간과 인원을 정해 파티·레이드 인원을 모읍니다.")

    @파티.command(name="모집", description="이 채널에 파티 모집글을 올려요. 참가 버튼으로 모입니다.")
    @app_commands.describe(제목="무엇을 하는 모집인지 (예: 심연 레이드)", 인원="주최자 포함 정원",
                           시각="예: `20:00` · `8-25 20:00` · `2026-08-25 20:00`", 설명="더 적을 말 (생략 가능)")
    async def recruit(self, interaction: discord.Interaction, 제목: str, 인원: int, 시각: str, 설명: str = ""):
        if await feature_gate(interaction, "party", "파티 모집"):
            return
        if not 2 <= 인원 <= 99:
            return await interaction.response.send_message("❌ 인원은 2~99명 사이로 정해주세요.", ephemeral=True)

        start = parse_datetime_text(시각.strip(), dt.datetime.now(KST))
        if start is None:
            return await interaction.response.send_message(
                "❌ 시각을 못 알아들었어요. `20:00` · `8-25 20:00` · `2026-08-25 20:00` 처럼 적어주세요.",
                ephemeral=True)

        parties = self._all()
        if len([p for p in parties.values() if not p.get("closed")]) >= MAX_OPEN_PARTIES:
            return await interaction.response.send_message(
                f"❌ 열려 있는 모집이 너무 많아요({MAX_OPEN_PARTIES}개). 끝난 모집을 먼저 마감해 주세요.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        party = {
            "title": 제목, "note": 설명, "size": 인원, "host": interaction.user.id,
            "start": start.isoformat(), "channel_id": interaction.channel.id,
            "guild_id": interaction.guild.id, "members": [interaction.user.id],
            "waiting": [], "closed": False, "reminded": False,
        }
        message = await interaction.channel.send(embed=self._embed(party, interaction.guild.id))
        # 🔒 새 모집을 끼워 넣는 것도 "읽고 → 고치고 → 저장"이에요. 위에서 읽어둔
        #    parties는 메세지를 보내는 사이에 낡았을 수 있으니 락 안에서 다시 읽습니다.
        async with self._lock:
            parties = self._all()
            parties[str(message.id)] = party
            self._save(parties)

        view = PartyView(self, str(message.id))
        await message.edit(view=view)
        self.bot.add_view(view, message_id=message.id)
        await interaction.followup.send(f"✅ 모집을 올렸어요! (주최자는 자동으로 참가돼요)\n{message.jump_url}",
                                        ephemeral=True)

    @파티.command(name="목록", description="열려 있는 파티 모집을 전부 봐요.")
    async def listing(self, interaction: discord.Interaction):
        if await feature_gate(interaction, "party", "파티 모집"):
            return
        rows = [(pid, p) for pid, p in self._all().items() if not p.get("closed")]
        if not rows:
            return await interaction.response.send_message(
                "지금 열려 있는 모집이 없어요. `/파티 모집`으로 하나 열어보세요!", ephemeral=True)

        rows.sort(key=lambda kv: kv[1]["start"])
        embed = discord.Embed(title="🎯 모집 중인 파티", color=0x9B59B6)
        for pid, p in rows[:MAX_OPEN_PARTIES]:
            start = int(dt.datetime.fromisoformat(p["start"]).timestamp())
            embed.add_field(
                name=clip(f"{p['title']} — {len(p.get('members', []))}/{p['size']}", EMBED_TITLE_LIMIT),
                value=f"<t:{start}:R> · <#{p['channel_id']}> · 주최 <@{p['host']}>\n"
                      f"https://discord.com/channels/{p['guild_id']}/{p['channel_id']}/{pid}",
                inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @파티.command(name="정리", description="[관리자] 끝난 모집 기록을 지워요. (열려 있는 모집은 그대로 둡니다)")
    async def cleanup(self, interaction: discord.Interaction):
        if await feature_gate(interaction, "party", "파티 모집"):
            return
        if not has_admin_or_role(interaction, "party_admin"):
            return await interaction.response.send_message("⛔ 파티를 관리할 권한이 없어요!", ephemeral=True)

        async with self._lock:
            parties = self._all()
            closed = [pid for pid, p in parties.items() if p.get("closed")]
            for pid in closed:
                parties.pop(pid, None)
            self._save(parties)
        await interaction.response.send_message(
            f"🧹 끝난 모집 {len(closed)}건을 지웠어요. (올라간 메시지는 그대로 남아 있어요)", ephemeral=True)
