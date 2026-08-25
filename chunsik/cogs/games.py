"""ChunsikGames — 하이로우 게임, 선착순 이벤트."""

import random
import asyncio
import traceback
import datetime as dt
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands, tasks

from chunsik_config import KST
from chunsik_alerts import report_loop_error
from chunsik_state import record_ledger
from chunsik_utils import chunk_lines
from chunsik_settings import has_admin_or_role, load_settings, save_settings, send_log_embed
from chunsik_names import bot_name, currency, event_name, josa

class ChunsikGames(commands.Cog):
    """하이로우 등 미니게임 시스템"""

    def __init__(self, bot):
        # 🐛 [버그 수정] 이 코그에는 원래 __init__이 없어서 self.bot이 보장되지 않았어요.
        # 이제 정식으로 저장해서 이벤트 기능(백그라운드 스케줄러, on_message)에서도 안전하게 씁니다.
        self.bot = bot

        # 🎲 하이로우 게임 상태 (메모리 전용)
        # 🗑️ [정리] 예전엔 current_numbers를 ids.json에 저장했어요. 그런데 아래 active_games는
        # 애초에 저장되지 않아서 재시작하면 항상 비어 있고, process_guess가 이걸로 먼저 막기
        # 때문에 재시작 후엔 반드시 /하이로우가 먼저 실행돼 숫자를 덮어써요. 즉 파일에서
        # 읽어온 값이 쓰일 수 있는 경로가 아예 없는, 완전히 죽은 영속화였습니다.
        # 그런데 그것 때문에 /하이·/로우를 칠 때마다 유저 전체의 아이디 DB가 통째로
        # 다시 저장되고 있었어요. 이제 게임 상태는 이 코그가 메모리에서만 들고 있습니다.
        self.current_numbers = {}   # {guild_id: 현재 숫자}
        self.active_games = set()   # 하이로우가 시작된 guild_id

        # 🎉 선착순 이벤트 상태
        self.evashi_window_open_until = None  # 이 시각까지는 이벤트 키워드를 치면 보상을 받을 수 있음 (KST datetime)
        self.evashi_participants = set()      # 이번 창(window)에서 이미 보상을 받은 유저 ID (선착순+나머지 전부)
        self.evashi_first_claimed = 0         # 이번 창에서 "선착순" 보상을 받은 인원 수
        self.evashi_first_winners = []        # 이번 창에서 선착순 보상을 받은 유저 ID 목록
        self.evashi_guild = None              # 이번 창에서 첫 참가자가 나온 길드 (마감 공지 보낼 곳)
        self.evashi_close_task = None         # 창을 정확히 window_seconds 뒤에 닫고 결과를 공지하는 예약 태스크

        self.evashi_loop.start()

    def cog_unload(self):
        self.evashi_loop.cancel()
        if self.evashi_close_task and not self.evashi_close_task.done():
            self.evashi_close_task.cancel()

    # ---------- 🎉 이벤트 설정 로드/저장 ----------
    def _load_evashi_settings(self) -> dict:
        settings = load_settings()
        defaults = {"first_count": 3, "first_amount": 12210, "rest_amount": 1221, "window_seconds": 60}
        evashi = settings.get("evashi", {})
        for k, v in defaults.items():
            evashi.setdefault(k, v)
        return evashi

    def _save_evashi_settings(self, evashi: dict):
        settings = load_settings()
        settings["evashi"] = evashi
        save_settings(settings)

    def _is_evashi_admin(self, interaction: discord.Interaction) -> bool:
        return has_admin_or_role(interaction, "evashi_admin")

    async def _send_evashi_announce(self, guild: discord.Guild, text: str):
        """봇 전용 이벤트 '안내 채널'로 공지를 보냅니다. (유저가 이벤트 키워드를 치는 채널이 아니라
        별도로 설정해둔, 봇이 결과를 알려주는 전용 채널이에요.)"""
        ch_id = load_settings().get("channels", {}).get("evashi_announce")
        if not ch_id:
            print(f"⚠️ [{event_name()}] 안내 채널이 설정 안 돼있어서 결과 공지를 못 올렸어요. `/설정 채널 이벤트`로 지정해주세요.")
            return
        channel = guild.get_channel(ch_id) if guild else self.bot.get_channel(ch_id)
        if not channel:
            print(f"⚠️ [{event_name()}] 안내 채널(id={ch_id})을 찾을 수 없어서 결과 공지를 못 올렸어요.")
            return
        # 🐛 [버그 수정] 참가자를 전부 멘션하다 보니 사람이 많이 몰린 회차에서는 본문이
        # 디스코드 한 메세지 제한(2000자)을 넘겨 전송이 통째로 실패했어요. 멘션 하나가 22자쯤
        # 되니 90명 남짓이면 걸립니다. 참가자가 많을수록 결과 발표가 확실히 안 나가는 셈이라,
        # 제일 필요한 순간에 못 쓰는 상태였어요. 이제 넘치면 나눠서 보냅니다.
        for part in chunk_lines(text.split("\n"), limit=1900):
            try:
                await channel.send(part)
            except Exception as e:
                print(f"❗ {event_name()} 안내 채널 발송 실패: {e}")
                return

    async def _give_evashi_reward(self, user_id: int, amount: int, *, is_first: bool = False):
        """이벤트 보상을 안전하게 지급합니다. (동시 지급 충돌 방지용 economy_lock 사용)"""
        economy_cog = self.bot.get_cog("ChunsikEconomy")
        if not economy_cog:
            return
        async with self.bot.economy_lock:
            data = economy_cog._load_raw_economy()
            user_key = str(user_id)
            data[user_key] = data.get(user_key, 0) + amount
            economy_cog._save_raw_economy(data)
            balance_after = data[user_key]

        # 🧾 [버그 수정] 지갑에 돈이 들어오는데 원장에는 안 남고 있었어요. 송금·출석·상점·주식·
        # 캠프통장은 전부 남기는데 여기만 빠져서, 유저가 /지갑내역을 열면 이벤트로 받은 돈만
        # 출처 없이 잔액에 얹혀 있었습니다. (락을 놓은 뒤에 기록해요)
        record_ledger(user_key, amount, balance_after, f"{event_name()} 보상",
                      "선착순" if is_first else "참여")

    @tasks.loop(time=[dt.time(hour=0, minute=21, tzinfo=KST), dt.time(hour=12, minute=21, tzinfo=KST)])
    async def evashi_loop(self):
        """🐛 [버그 수정] 예전엔 60초마다 폴링해서 시(hour)/분(minute)이 맞는지 확인하는 방식이라,
        타이밍에 따라 최대 59초까지 늦게 열릴 수 있었어요. 이제는 discord.py의 time= 스케줄링을 써서
        00:21, 12:21(KST) 정각에 정확하게 실행돼요."""
        # 🛡️ 설정 파일이 손상되면 load_settings()가 예외를 던져요. 그게 밖으로 새면
        # 루프가 영구히 멈춰서 선착순 이벤트가 다시는 안 열립니다. 이번 회차만 포기해요.
        try:
            evashi = self._load_evashi_settings()
        except Exception as e:
            print(f"❗ [{event_name()}] 설정을 읽지 못해 이번 회차를 건너뛰어요: {type(e).__name__}: {e}")
            return

        self.evashi_participants = set()
        self.evashi_first_claimed = 0
        self.evashi_first_winners = []
        self.evashi_guild = None
        now = dt.datetime.now(KST)
        self.evashi_window_open_until = now + dt.timedelta(seconds=evashi["window_seconds"])
        print(f"🎉 [{event_name()}] 이벤트 창 열림! {evashi['window_seconds']}초 동안 '{event_name()}'{josa(event_name(), '을를')} 치면 보상을 받아요.")

        # ⏰ [신규] window_seconds 뒤에 자동으로 창을 닫고, 그때까지 모인 참가자 전원을
        # 한 번에 모아서 공지해요. (더 이상 선착순 채워지는 순간에만 알리지 않아요)
        if self.evashi_close_task and not self.evashi_close_task.done():
            self.evashi_close_task.cancel()
        self.evashi_close_task = asyncio.create_task(self._close_evashi_window_later(evashi["window_seconds"]))

    @evashi_loop.before_loop
    async def before_evashi_loop(self):
        await self.bot.wait_until_ready()

    @evashi_loop.error
    async def evashi_loop_error(self, error: BaseException):
        await report_loop_error(self.evashi_loop, f"{event_name()} 이벤트", error)

    async def _close_evashi_window_later(self, window_seconds: int):
        """정확히 window_seconds가 지난 뒤 창을 닫고, 참가자 전원을 한 번에 공지합니다."""
        try:
            await self._close_evashi_window_now(window_seconds)
        except asyncio.CancelledError:
            raise       # 봇 종료/재시작으로 인한 정상적인 취소는 그대로 흘려보내요
        except Exception as e:
            # 🛡️ create_task로 띄운 태스크는 예외가 나도 아무 데도 안 뜨고 조용히 사라져요.
            # 그러면 참가자들은 결과 발표를 영영 못 받는데 아무도 이유를 모릅니다.
            print(f"❗ [{event_name()}] 마감 공지 중 오류가 났어요: {type(e).__name__}: {e}")
            traceback.print_exception(type(e), e, e.__traceback__)

    async def _close_evashi_window_now(self, window_seconds: int):
        await asyncio.sleep(window_seconds)
        self.evashi_window_open_until = None

        if not self.evashi_participants or not self.evashi_guild:
            return  # 아무도 참가 안 했으면 조용히 넘어감

        evashi = self._load_evashi_settings()
        rest_ids = [uid for uid in self.evashi_participants if uid not in self.evashi_first_winners]

        # 📢 [버그 수정] 예전엔 사람이 몰릴 때마다 개인별로 로그가 따로 남아서 도배됐었어요.
        # 이제는 창이 닫히는 시점에 딱 한 번, 참가자 전원(선착순+나머지)을 모아서 공지해요.
        lines = [f"🎉 **{event_name()} 이벤트 결과** (총 {len(self.evashi_participants)}명 참여)"]
        if self.evashi_first_winners:
            mentions = " ".join(f"<@{uid}>" for uid in self.evashi_first_winners)
            lines.append(f"🥇 선착순 {len(self.evashi_first_winners)}명: {mentions}\n└ 각 {evashi['first_amount']:,} {currency()} 지급")
        if rest_ids:
            mentions = " ".join(f"<@{uid}>" for uid in rest_ids)
            lines.append(f"🎊 참가 {len(rest_ids)}명: {mentions}\n└ 각 {evashi['rest_amount']:,} {currency()} 지급")

        await self._send_evashi_announce(self.evashi_guild, "\n\n".join(lines))

        await send_log_embed(
            self.bot, "economy_log", f"{event_name()} 이벤트 전체 결과 기록이에요.",
            fields=[
                ("총 참여 인원", f"{len(self.evashi_participants)}명", False),
                ("선착순", f"{len(self.evashi_first_winners)}명 · 각 {evashi['first_amount']:,} {currency()}", False),
                ("나머지 참가", f"{len(rest_ids)}명 · 각 {evashi['rest_amount']:,} {currency()}", False),
            ],
            guild=self.evashi_guild,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if self.evashi_window_open_until is None:
            return
        if message.content.strip() != event_name():
            return

        now = dt.datetime.now(KST)
        if now > self.evashi_window_open_until:
            return  # 이벤트 창이 이미 닫혔음
        if message.author.id in self.evashi_participants:
            return  # 이미 이번 창에서 보상을 받은 유저 (중복 방지)

        if self.evashi_guild is None:
            self.evashi_guild = message.guild  # 마감 공지를 보낼 길드를 첫 참가자 기준으로 기억해둬요

        self.evashi_participants.add(message.author.id)
        evashi = self._load_evashi_settings()

        is_first = self.evashi_first_claimed < evashi["first_count"]
        if is_first:
            self.evashi_first_claimed += 1
            self.evashi_first_winners.append(message.author.id)
            amount = evashi["first_amount"]
        else:
            amount = evashi["rest_amount"]

        # 💰 보상 지급은 실시간으로 바로 해요. (공지/로그만 창이 닫힐 때 한 번에 모아서 나가요)
        await self._give_evashi_reward(message.author.id, amount, is_first=is_first)

        # 👍 [버그 수정] 도배를 막으려고 개인별 메세지/로그를 다 없앴더니, 이벤트 키워드를 쳐도
        # 그 순간엔 아무 반응이 없어서 마치 안 되는 것처럼 보였어요. 새 메세지를 추가로
        # 보내지 않으면서도 즉시 확인할 수 있게, 본인이 친 메세지에 리액션만 살짝 달아줘요.
        try:
            emoji = "🥇" if amount == evashi["first_amount"] else "🎉"
            await message.add_reaction(emoji)
        except Exception:
            pass

    # ---------- 관리자 설정 명령어 ----------
    @app_commands.command(name="이벤트설정", description=f"[관리자] {event_name()} 이벤트의 선착순 인원/금액을 설정합니다.")
    @app_commands.guild_only()
    @app_commands.describe(
        선착순인원="큰 보상을 받을 선착순 인원 수",
        선착순금액="선착순 인원에게 줄 금액",
        나머지금액="선착순 이후 참가자에게 줄 금액",
        지속시간초="이벤트 창이 열려있는 시간(초). 생략 시 기존 값 유지",
    )
    async def set_evashi_settings(
        self,
        interaction: discord.Interaction,
        선착순인원: Optional[int] = None,
        선착순금액: Optional[int] = None,
        나머지금액: Optional[int] = None,
        지속시간초: Optional[int] = None,
    ):
        if not self._is_evashi_admin(interaction):
            return await interaction.response.send_message(f"⛔ 권한이 없어요. {event_name()} 관리자만 설정할 수 있어요.", ephemeral=True)

        if 선착순인원 is None and 선착순금액 is None and 나머지금액 is None and 지속시간초 is None:
            evashi = self._load_evashi_settings()
            return await interaction.response.send_message(
                f"ℹ️ **현재 {event_name()} 설정**\n"
                f"└ 선착순 인원: {evashi['first_count']}명\n"
                f"└ 선착순 금액: {evashi['first_amount']:,} {currency()}\n"
                f"└ 나머지 금액: {evashi['rest_amount']:,} {currency()}\n"
                f"└ 지속시간: {evashi['window_seconds']}초",
                ephemeral=True,
            )

        evashi = self._load_evashi_settings()
        logs = []
        if 선착순인원 is not None:
            if 선착순인원 < 0: return await interaction.response.send_message("❌ 선착순인원은 0 이상이어야 해요.", ephemeral=True)
            evashi["first_count"] = 선착순인원
            logs.append(f"선착순 인원: {선착순인원}명")
        if 선착순금액 is not None:
            if 선착순금액 < 0: return await interaction.response.send_message("❌ 선착순금액은 0 이상이어야 해요.", ephemeral=True)
            evashi["first_amount"] = 선착순금액
            logs.append(f"선착순 금액: {선착순금액:,} {currency()}")
        if 나머지금액 is not None:
            if 나머지금액 < 0: return await interaction.response.send_message("❌ 나머지금액은 0 이상이어야 해요.", ephemeral=True)
            evashi["rest_amount"] = 나머지금액
            logs.append(f"나머지 금액: {나머지금액:,} {currency()}")
        if 지속시간초 is not None:
            if 지속시간초 <= 0: return await interaction.response.send_message("❌ 지속시간초는 1 이상이어야 해요.", ephemeral=True)
            evashi["window_seconds"] = 지속시간초
            logs.append(f"지속시간: {지속시간초}초")

        self._save_evashi_settings(evashi)
        await interaction.response.send_message(f"✅ {event_name()} 설정을 업데이트했어요.\n" + "\n".join(f"└ {l}" for l in logs), ephemeral=True)

    @app_commands.command(name="하이로우", description="하이로우 게임 시작!")
    @app_commands.guild_only()
    async def highlow_intro(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        self.current_numbers[gid] = random.randint(1, 100)
        self.active_games.add(gid)
        embed = discord.Embed(title=f"🔮 {bot_name()}의 하이로우 게임 안내!", color=discord.Color.blurple())
        embed.add_field(name="게임 방법", value="다음 숫자가 높을지/낮을지 맞혀보세요!", inline=False)
        embed.add_field(name="참여 방법", value="/하이 또는 /로우", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="하이", description="높을 것 같아요!")
    @app_commands.guild_only()
    async def guess_high(self, interaction: discord.Interaction):
        await self.process_guess(interaction, "하이")

    @app_commands.command(name="로우", description="낮을 것 같아요!")
    @app_commands.guild_only()
    async def guess_low(self, interaction: discord.Interaction):
        await self.process_guess(interaction, "로우")

    async def process_guess(self, interaction: discord.Interaction, guess: str):
        gid = interaction.guild.id
        if gid not in self.active_games:
            await interaction.response.send_message("먼저 /하이로우로 시작해요! 🎲", ephemeral=True)
            return

        cur = self.current_numbers[gid]
        nxt = random.randint(1, 100)
        win = (guess == "하이" and nxt > cur) or (guess == "로우" and nxt < cur)
        text = f"✨ 정답이에요! {bot_name()}{josa(bot_name(), '이가')} 감동하며 윙크~ 😉✨" if win else f"😭 틀렸어요... {bot_name()}{josa(bot_name(), '이가')} 삐죽! 다음엔 꼭 맞춰요!"
        embed = discord.Embed(title="💗 하이로우 결과!", color=discord.Color.pink())
        embed.add_field(name="현재 숫자", value=str(cur), inline=True)
        embed.add_field(name="예측", value=guess, inline=True)
        embed.add_field(name="다음 숫자", value=str(nxt), inline=True)
        embed.add_field(name="결과", value=text, inline=False)
        await interaction.response.send_message(embed=embed)
        self.current_numbers[gid] = nxt
