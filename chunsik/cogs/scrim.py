"""ChunsikScrim — 내전. 사람을 모아 팀으로 나누고, 승패를 기록합니다.

게임 서버가 제일 자주 하는 일인데 손이 제일 많이 가는 자리예요. 누가 온다고 했는지
채팅으로 세다가 흩어지고, 팀은 매번 손으로 나누고, "지난주에 누가 이겼더라"는
아무도 기억 못 합니다.

모집글 하나에 버튼을 붙여서 **모집 → 팀 나누기 → 결과 기록**까지 그 자리에서 끝냅니다.

🎯 **파티 모집(party)과 무엇이 다른가**
   · 파티 = "같이 하러 가자" (레이드·던전). 정원을 채우고 **대기열**을 받아요.
   · 내전 = "우리끼리 붙자" (5:5). 모인 사람을 **두 팀으로 나누고 승패를 남깁니다.**
   둘 다 담아도 겹치지 않고, 하나만 담아도 온전히 돌아가요.

⚖️ **팀은 전적을 보고 나눕니다.** 그냥 섞으면 이긴 팀이 계속 이겨서 재미가 없어요.
   승률 순으로 세워 뱀처럼 번갈아 담는(스네이크) 방식이라, 잘하는 사람이 한쪽에
   몰리지 않습니다. 전적이 없는 사람은 5할로 봐요. 완전 무작위도 고를 수 있습니다.

🆔 **아이디 등록부(id)를 함께 담으면** 팀 명단에 게임 아이디가 같이 붙어요.
   팀이 나뉘자마자 바로 초대할 수 있습니다. (코그를 부르지 않고 module_active로만 봅니다)

🔁 모집 버튼 부분이 party.py와 닮았지만 **일부러 따로 뒀습니다.** 코그끼리 import하면
   한쪽만 담아 납품할 때 죽어요. 공용으로 쓸 만한 것(락 패턴·명단 자르기·시각 파서)은
   이미 chunsik_utils에 있고, 그건 둘 다 거기서 가져다 씁니다.
"""

import asyncio
import datetime as dt
import random

import discord
from discord import app_commands
from discord.ext import commands, tasks

from chunsik_config import KST, module_active
from chunsik_settings import (feature_gate, has_admin_or_role, is_feature_enabled,
                              send_log_embed)
from chunsik_state import load_scrim, save_scrim, state
from chunsik_utils import (EMBED_DESC_LIMIT, EMBED_TITLE_LIMIT, MESSAGE_LIMIT,
                           ChunsikView, add_lines_field, clip, mention_list,
                           parse_datetime_text)

REMIND_BEFORE_MINUTES = 10
MAX_OPEN_SCRIMS = 25
MAX_TEAM_SIZE = 20          # 한 팀 인원 상한 (모드가 커봐야 이 정도예요)

# 🎨 팀 색과 이름. 두 팀뿐이라 표 하나로 충분해요.
TEAMS = {
    "A": ("🔵 A팀", 0x3B82F6),
    "B": ("🔴 B팀", 0xEF4444),
}


def _ids_line(guild_id, user_id) -> str:
    """그 사람의 게임 아이디 한 줄. 아이디 모듈을 안 담았으면 빈 문자열."""
    if not module_active("id"):
        return ""
    entry = state.user_ids.get(str(guild_id), {}).get(str(user_id), {})
    if not entry:
        return ""
    return " · " + " / ".join(f"{platform} `{value}`" for platform, value in list(entry.items())[:2])


def win_rate(record: dict) -> float:
    """승률. 전적이 없으면 5할로 봅니다.

    ⚖️ 신입을 0할로 두면 팀을 짤 때 약체 취급을 받아 한쪽으로 몰려요. 아직 모르는
       사람은 '보통'으로 두는 게 맞습니다.
    """
    win, lose = record.get("win", 0), record.get("lose", 0)
    played = win + lose
    return 0.5 if played == 0 else win / played


def split_teams(members: list, records: dict, balanced: bool = True) -> dict:
    """참가자를 두 팀으로 나눕니다. → {"A": [...], "B": [...]}

    ⚖️ 스네이크 방식이에요. 승률 순으로 세운 뒤 A-B-B-A-A-B… 로 번갈아 담습니다.
       그냥 위에서부터 반씩 자르면 1등부터 5등이 한 팀이 돼서 경기가 안 됩니다.
       번갈아 담기만 해도(A-B-A-B) 1등과 3등이 같은 팀이라 여전히 기울어요.
    """
    picks = list(members)
    if balanced:
        picks.sort(key=lambda uid: win_rate(records.get(str(uid), {})), reverse=True)
    else:
        random.shuffle(picks)

    teams = {"A": [], "B": []}
    for index, uid in enumerate(picks):
        # 스네이크 — 두 명씩 묶어 방향을 뒤집습니다 (0:A 1:B 2:B 3:A 4:A 5:B …)
        teams["A" if (index // 2) % 2 == index % 2 else "B"].append(uid)
    return teams


class ScrimView(ChunsikView):
    """모집글에 붙는 버튼. 내전이 어느 단계인지에 따라 버튼이 바뀝니다.

    timeout=None이라 봇을 재시작해도 계속 눌려요.
    """

    def __init__(self, cog: "ChunsikScrim", scrim_id: str, stage: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.scrim_id = scrim_id

        if stage == "recruit":
            layout = (
                ("참가", "✅", "join", discord.ButtonStyle.success),
                ("취소", "🚪", "leave", discord.ButtonStyle.secondary),
                ("팀 짜기", "⚖️", "draft", discord.ButtonStyle.primary),
                ("마감", "🔒", "close", discord.ButtonStyle.danger),
            )
        elif stage == "drafted":
            layout = (
                ("A팀 승", "🔵", "win:A", discord.ButtonStyle.primary),
                ("B팀 승", "🔴", "win:B", discord.ButtonStyle.danger),
                ("무승부", "🤝", "win:draw", discord.ButtonStyle.secondary),
                ("다시 짜기", "🔀", "draft", discord.ButtonStyle.secondary),
            )
        else:                       # 끝난 내전 — 버튼 없음
            layout = ()

        for label, emoji, action, style in layout:
            button = discord.ui.Button(label=label, emoji=emoji, style=style,
                                       custom_id=f"scrim:{scrim_id}:{action}")
            button.callback = self._make(action)
            self.add_item(button)

    def _make(self, action):
        async def callback(interaction: discord.Interaction):
            await self.cog.handle(interaction, self.scrim_id, action)
        return callback


class ChunsikScrim(commands.Cog):
    """내전 — 모집, 팀 나누기, 승패 기록."""

    def __init__(self, bot):
        self.bot = bot
        # 🔒 버튼으로 고쳐지는 파일이라 "읽고 → 고치고 → 저장"이 겹치면 참가가 사라집니다.
        #    (파티 모집이 같은 이유로 락을 씁니다)
        self._lock = asyncio.Lock()

    async def cog_load(self):
        data = self._all()
        for sid, scrim in data.get("matches", {}).items():
            if scrim.get("closed"):
                continue
            try:
                self.bot.add_view(ScrimView(self, sid, self._stage(scrim)), message_id=int(sid))
            except Exception as e:
                print(f"❗ 내전({sid}) 버튼 등록 실패: {e}")
        self.tick.start()

    async def cog_unload(self):
        self.tick.cancel()

    # ---------- 데이터 ----------

    @staticmethod
    def _stage(scrim: dict) -> str:
        if scrim.get("closed"):
            return "done"
        return "drafted" if scrim.get("teams") else "recruit"

    def _all(self) -> dict:
        data = load_scrim()
        data.setdefault("matches", {})
        data.setdefault("records", {})
        return data

    def _save(self, data: dict):
        save_scrim(data)

    # ---------- 화면 ----------

    def _embed(self, scrim: dict, records: dict) -> discord.Embed:
        start = dt.datetime.fromisoformat(scrim["start"])
        joined = scrim.get("members", [])
        size = scrim["size"]
        need = size * 2
        teams = scrim.get("teams")
        result = scrim.get("result")
        guild_id = scrim.get("guild_id")

        if result:
            head = "🏁 **끝났어요** — " + ("🤝 무승부" if result == "draw" else f"{TEAMS[result][0]} 승리")
            color = 0x99AAB5
        elif teams:
            head = "⚔️ **팀이 나뉘었어요.** 경기가 끝나면 아래 버튼으로 결과를 남겨주세요."
            color = 0x8B5CF6
        elif len(joined) >= need:
            head = f"🈵 **{need}명이 다 모였어요!** 팀 짜기를 누르세요."
            color = 0x22C55E
        else:
            head = f"🈳 모집 중 — **{len(joined)} / {need}명**"
            color = 0x8B5CF6

        embed = discord.Embed(
            title=clip(f"⚔️ {scrim['title']}", EMBED_TITLE_LIMIT),
            description=clip((scrim.get("note") or "") + f"\n\n{head}", EMBED_DESC_LIMIT),
            color=color,
        )
        embed.add_field(name="시작", value=f"<t:{int(start.timestamp())}:F>\n<t:{int(start.timestamp())}:R>", inline=True)
        embed.add_field(name="형식", value=f"**{size} : {size}**", inline=True)
        embed.add_field(name="주최", value=f"<@{scrim['host']}>", inline=True)

        if teams:
            for key in ("A", "B"):
                label = TEAMS[key][0]
                mark = ""
                if result == key:
                    mark = "  🏆"
                elif result and result != "draw":
                    mark = "  ·"
                add_lines_field(
                    embed, f"{label} ({len(teams[key])}명){mark}",
                    [f"<@{uid}>{_ids_line(guild_id, uid)}" for uid in teams[key]],
                    empty="*(없음)*")
        else:
            add_lines_field(
                embed, f"참가자 {len(joined)}명",
                [f"`{i + 1}.` <@{uid}> {self._record_tag(records, uid)}"
                 for i, uid in enumerate(joined)],
                empty="*(아직 없어요)*")

        if not result:
            embed.set_footer(text="참가를 누르면 자리를 잡아요. 못 오게 되면 취소를 눌러주세요.")
        return embed

    @staticmethod
    def _record_tag(records: dict, uid) -> str:
        """이름 옆에 붙는 작은 전적. 아직 안 해봤으면 아무것도 안 붙여요."""
        record = records.get(str(uid), {})
        win, lose = record.get("win", 0), record.get("lose", 0)
        return f"`{win}승 {lose}패`" if (win or lose) else ""

    async def _repaint(self, scrim_id: str, scrim: dict, records: dict):
        channel = self.bot.get_channel(int(scrim["channel_id"]))
        if channel is None:
            return
        message = await channel.fetch_message(int(scrim_id))
        stage = self._stage(scrim)
        view = ScrimView(self, scrim_id, stage) if stage != "done" else None
        await message.edit(embed=self._embed(scrim, records), view=view)

    # ---------- 버튼 ----------

    def _apply(self, data: dict, scrim: dict, uid: int, action: str, is_admin: bool) -> tuple:
        """버튼 하나를 반영합니다. → (안내 문구, 저장할 것인가)

        🚨 **await가 하나도 없어야 해요.** 락을 쥔 채로 디스코드에 말을 걸면 그 왕복
           사이에 다른 사람의 클릭이 끼어들어, 방금 읽은 내용이 낡은 것이 됩니다.
        """
        members = scrim.setdefault("members", [])
        records = data["records"]
        host_or_admin = uid == scrim["host"] or is_admin

        if action == "join":
            if scrim.get("teams"):
                return "❌ 팀이 이미 나뉘었어요. 주최자에게 말씀해 주세요.", False
            if uid in members:
                return "이미 참가하셨어요! 🙂", False
            if len(members) >= scrim["size"] * 2:
                return f"❌ 인원이 다 찼어요. ({scrim['size'] * 2}명)", False
            members.append(uid)
            return "✅ 참가했어요!", True

        if action == "leave":
            if uid not in members:
                return "❌ 참가하지 않으셨어요.", False
            if scrim.get("teams"):
                return "❌ 팀이 나뉜 뒤에는 빠질 수 없어요. 주최자에게 말씀해 주세요.", False
            members.remove(uid)
            return "🚪 빠졌어요.", True

        if action == "close":
            if not host_or_admin:
                return "⛔ 내전을 연 사람이나 내전 관리자만 마감할 수 있어요.", False
            scrim["closed"] = True
            return None, True

        if action == "draft":
            if not host_or_admin:
                return "⛔ 내전을 연 사람이나 내전 관리자만 팀을 짤 수 있어요.", False
            need = scrim["size"] * 2
            if len(members) < need:
                return (f"❌ 아직 {need - len(members)}명 모자라요. "
                        f"({len(members)}/{need}명)"), False
            scrim["teams"] = split_teams(members, records, balanced=scrim.get("balanced", True))
            how = "전적을 보고" if scrim.get("balanced", True) else "무작위로"
            return f"⚖️ {how} 팀을 나눴어요!", True

        if action.startswith("win:"):
            if not host_or_admin:
                return "⛔ 내전을 연 사람이나 내전 관리자만 결과를 남길 수 있어요.", False
            teams = scrim.get("teams")
            if not teams:
                return "❌ 팀이 아직 안 나뉘었어요.", False
            if scrim.get("result"):
                return "❌ 이미 결과를 남긴 내전이에요.", False

            winner = action.split(":", 1)[1]
            scrim["result"] = winner
            scrim["closed"] = True
            # 🏆 전적은 여기서만 쌓입니다. 결과를 남기지 않은 내전은 전적에 안 들어가요.
            for key in ("A", "B"):
                if winner == "draw":
                    outcome = "draw"
                else:
                    outcome = "win" if key == winner else "lose"
                for player in teams[key]:
                    record = records.setdefault(str(player), {"win": 0, "lose": 0, "draw": 0})
                    record[outcome] = record.get(outcome, 0) + 1
            label = "🤝 무승부로" if winner == "draw" else f"{TEAMS[winner][0]} 승리로"
            return f"🏁 {label} 기록했어요.", True

        return "❌ 알 수 없는 버튼이에요.", False

    async def handle(self, interaction: discord.Interaction, scrim_id: str, action: str):
        if not is_feature_enabled("scrim"):
            return await interaction.response.send_message("🚧 내전 기능이 잠시 정지돼 있어요.", ephemeral=True)

        uid = interaction.user.id
        is_admin = has_admin_or_role(interaction, "scrim_admin")
        changed = None

        async with self._lock:
            data = self._all()
            scrim = data["matches"].get(scrim_id)
            if scrim is None or (scrim.get("closed") and not scrim.get("result")):
                reply, save = "❌ 이미 끝난 내전이에요.", False
            else:
                reply, save = self._apply(data, scrim, uid, action, is_admin)
                if save:
                    self._save(data)
                    changed = (scrim, data["records"])

        if reply is None:
            await interaction.response.defer()
        else:
            await interaction.response.send_message(reply, ephemeral=True)

        if changed is not None:
            await self._repaint(scrim_id, *changed)
            if action.startswith("win:"):
                await self._announce_result(scrim_id, changed[0])

    async def _announce_result(self, scrim_id: str, scrim: dict):
        """결과를 기록 채널에 남깁니다. (채널을 안 정했으면 조용히 건너뜁니다)"""
        winner = scrim.get("result")
        teams = scrim.get("teams") or {}
        text = "🤝 무승부" if winner == "draw" else f"{TEAMS[winner][0]} 승리"
        await send_log_embed(
            self.bot, "scrim_log", f"🏁 **{scrim['title']}** — {text}",
            fields=[(TEAMS[key][0], mention_list(teams.get(key, []), 12) or "*(없음)*", False)
                    for key in ("A", "B")],
        )

    # ---------- 시작 시각 챙기기 ----------

    @tasks.loop(minutes=1)
    async def tick(self):
        if not is_feature_enabled("scrim"):
            return
        now = dt.datetime.now(KST)
        todo = []
        async with self._lock:
            data = self._all()
            changed = False
            for sid, scrim in list(data["matches"].items()):
                if scrim.get("closed") or scrim.get("reminded"):
                    continue
                start = dt.datetime.fromisoformat(scrim["start"])
                if 0 < (start - now).total_seconds() <= REMIND_BEFORE_MINUTES * 60:
                    scrim["reminded"] = True
                    changed = True
                    todo.append((dict(scrim), scrim.get("members", [])))
            if changed:
                self._save(data)

        for scrim, members in todo:
            channel = self.bot.get_channel(int(scrim["channel_id"]))
            if channel and members:
                try:
                    await channel.send(clip(
                        f"⏰ **{scrim['title']}** 시작 {REMIND_BEFORE_MINUTES}분 전이에요! "
                        + mention_list(members), MESSAGE_LIMIT))
                except Exception as e:
                    print(f"❗ 내전 알림 실패: {type(e).__name__}: {e}")

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    # ---------- 명령 ----------

    내전 = app_commands.Group(name="내전", description="사람을 모아 팀으로 나누고 승패를 기록합니다.")

    @내전.command(name="열기", description="이 채널에 내전을 열어요. 버튼으로 사람이 모이고 팀까지 나눕니다.")
    @app_commands.describe(제목="무엇을 하는 내전인지 (예: 5:5 칼바람)",
                           한팀인원="한 팀에 몇 명인지 (5를 넣으면 5:5)",
                           시각="예: `20:00` · `8-25 20:00` · `2026-08-25 20:00`",
                           팀짜기="전적을 보고 나눌지, 완전 무작위로 나눌지",
                           설명="더 적을 말 (생략 가능)")
    @app_commands.choices(팀짜기=[
        app_commands.Choice(name="전적을 보고 (기본)", value="balanced"),
        app_commands.Choice(name="완전 무작위", value="random"),
    ])
    async def open_scrim(self, interaction: discord.Interaction, 제목: str, 한팀인원: int, 시각: str,
                         팀짜기: app_commands.Choice[str] = None, 설명: str = ""):
        if await feature_gate(interaction, "scrim", "내전"):
            return
        if not 1 <= 한팀인원 <= MAX_TEAM_SIZE:
            return await interaction.response.send_message(
                f"❌ 한 팀 인원은 1~{MAX_TEAM_SIZE}명 사이로 정해주세요.", ephemeral=True)

        start = parse_datetime_text(시각.strip(), dt.datetime.now(KST))
        if start is None:
            return await interaction.response.send_message(
                "❌ 시각을 못 알아들었어요. `20:00` · `8-25 20:00` 처럼 적어주세요.", ephemeral=True)

        data = self._all()
        if len([m for m in data["matches"].values() if not m.get("closed")]) >= MAX_OPEN_SCRIMS:
            return await interaction.response.send_message(
                f"❌ 열려 있는 내전이 너무 많아요({MAX_OPEN_SCRIMS}개). 끝난 것을 먼저 마감해 주세요.",
                ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        scrim = {
            "title": 제목, "note": 설명, "size": 한팀인원, "host": interaction.user.id,
            "start": start.isoformat(), "channel_id": interaction.channel.id,
            "guild_id": interaction.guild.id, "members": [interaction.user.id],
            "teams": None, "result": None, "closed": False, "reminded": False,
            "balanced": (팀짜기.value if 팀짜기 else "balanced") == "balanced",
        }
        message = await interaction.channel.send(embed=self._embed(scrim, data["records"]))

        # 🔒 메세지를 보내는 사이에 위에서 읽은 data가 낡았을 수 있어요. 다시 읽습니다.
        async with self._lock:
            data = self._all()
            data["matches"][str(message.id)] = scrim
            self._save(data)

        view = ScrimView(self, str(message.id), "recruit")
        await message.edit(view=view)
        self.bot.add_view(view, message_id=message.id)
        await interaction.followup.send(
            f"✅ 내전을 열었어요! (주최자는 자동으로 참가돼요)\n{message.jump_url}", ephemeral=True)

    @내전.command(name="전적", description="내 내전 전적을 봐요. (다른 사람 것도 볼 수 있어요)")
    @app_commands.describe(멤버="확인할 멤버 (생략하면 나)")
    async def record(self, interaction: discord.Interaction, 멤버: discord.Member = None):
        if await feature_gate(interaction, "scrim", "내전"):
            return
        member = 멤버 or interaction.user
        records = self._all()["records"]
        record = records.get(str(member.id))
        if not record or not (record.get("win") or record.get("lose") or record.get("draw")):
            return await interaction.response.send_message(
                f"아직 {member.display_name} 님의 내전 기록이 없어요.", ephemeral=True)

        win, lose, draw = record.get("win", 0), record.get("lose", 0), record.get("draw", 0)
        played = win + lose + draw
        rate = win_rate(record)

        ranking = sorted(records.items(),
                         key=lambda kv: (win_rate(kv[1]), kv[1].get("win", 0)), reverse=True)
        place = next((i + 1 for i, (uid, _) in enumerate(ranking) if uid == str(member.id)), None)

        filled = int(rate * 10)
        embed = discord.Embed(title=f"⚔️ {member.display_name}", color=0x8B5CF6)
        embed.add_field(name="전적", value=f"**{win}승 {lose}패**" + (f" {draw}무" if draw else ""), inline=True)
        embed.add_field(name="승률", value=f"**{rate * 100:.0f}%**", inline=True)
        embed.add_field(name="순위", value=f"{place}위 / {len(ranking)}명", inline=True)
        embed.add_field(name=f"총 {played}판",
                        value="🟪" * filled + "⬜" * (10 - filled), inline=False)
        await interaction.response.send_message(embed=embed)

    @내전.command(name="순위", description="내전을 잘하는 사람 순으로 봐요.")
    async def ranking(self, interaction: discord.Interaction):
        if await feature_gate(interaction, "scrim", "내전"):
            return
        records = self._all()["records"]
        # 🎲 한두 판 하고 전승인 사람이 1등이 되면 순위가 의미를 잃어요. 3판부터 셉니다.
        rows = [(uid, r) for uid, r in records.items()
                if r.get("win", 0) + r.get("lose", 0) + r.get("draw", 0) >= 3]
        if not rows:
            return await interaction.response.send_message(
                "아직 순위에 오를 사람이 없어요. **3판**부터 순위에 들어갑니다.", ephemeral=True)

        rows.sort(key=lambda kv: (win_rate(kv[1]), kv[1].get("win", 0)), reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (uid, r) in enumerate(rows[:10]):
            win, lose = r.get("win", 0), r.get("lose", 0)
            lines.append(f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{uid}> — "
                         f"**{win_rate(r) * 100:.0f}%** ({win}승 {lose}패)")
        embed = discord.Embed(title="🏆 내전 순위", description="\n".join(lines), color=0xFFD700)
        embed.set_footer(text="3판 이상 한 사람만 순위에 들어갑니다.")
        await interaction.response.send_message(embed=embed)

    @내전.command(name="정리", description="[관리자] 끝난 내전 기록을 지워요. (전적은 그대로 남습니다)")
    async def cleanup(self, interaction: discord.Interaction):
        if await feature_gate(interaction, "scrim", "내전"):
            return
        if not has_admin_or_role(interaction, "scrim_admin"):
            return await interaction.response.send_message("⛔ 내전을 관리할 권한이 없어요!", ephemeral=True)

        async with self._lock:
            data = self._all()
            closed = [sid for sid, m in data["matches"].items() if m.get("closed")]
            for sid in closed:
                data["matches"].pop(sid, None)
            self._save(data)
        await interaction.response.send_message(
            f"🧹 끝난 내전 {len(closed)}건을 지웠어요.\n"
            "**전적과 순위는 그대로예요.** 올라간 메시지도 남아 있습니다.", ephemeral=True)
