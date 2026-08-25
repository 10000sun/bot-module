"""MariLevels — 채팅 활동으로 쌓는 레벨. 레벨이 오르면 역할(과 재화)을 줍니다.

말을 많이 한 사람이 눈에 보이게 하는 기능이에요. 메시지를 올릴 때마다 조금씩 경험치가
쌓이고, 일정 선을 넘으면 레벨이 오르면서 정해둔 역할이 자동으로 붙습니다.

🧩 **지갑(economy)이 없어도 동작해요.** 지갑을 함께 담은 서버에서만 레벨업 재화 보상이
   켜집니다. 그래서 modules.py에 requires를 걸지 않았어요 — 걸면 "레벨만 주세요" 하는
   주문에 지갑이 통째로 딸려갑니다.

💾 경험치는 **메모리에 들고 있다가 주기적으로 저장**합니다. 메시지마다 파일을 쓰면 사람
   많은 서버에서 디스크가 쉴 새 없이 돌아요. 대신 봇이 갑자기 죽으면 마지막 저장 이후
   경험치가 날아갑니다 — 재화가 아니라 경험치라서 감수할 만한 손해라고 봤어요.
   (지갑은 절대 이렇게 하지 않습니다. cogs/economy.py는 건건이 즉시 저장해요)
"""

import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from mari_names import currency
from mari_settings import (feature_gate, has_admin_or_role, is_feature_enabled,
                           load_settings)
from mari_state import load_levels, save_levels
from mari_storage import DataSaveError
from mari_utils import EMBED_FIELD_LIMIT, clip, dangerous_permission, role_reject_reason

SAVE_INTERVAL_SECONDS = 30

DEFAULT_CONFIG = {
    "min_xp": 15,        # 메시지 하나에 주는 경험치 (최소)
    "max_xp": 25,        # 〃 (최대)
    "cooldown": 60,      # 이 초 안에 또 말해도 경험치는 안 올라요 (도배 방지)
    "coin_per_level": 0, # 레벨업 1회당 줄 재화. 0이면 안 줍니다
    "announce": "current",  # 레벨업을 어디에 알릴지 (아래 ANNOUNCE_MODES)
}

# 📣 레벨업 알림을 어디에 올릴지. 선택지 이름과 설명을 한 곳에서만 만들어요
#    (명령 선택지와 `/레벨 설정` 확인 문구가 같이 따라오게 하려고요)
ANNOUNCE_MODES = {
    "current": "말한 채널 — 레벨이 오른 그 자리에서 바로 축하해요",
    "channel": "지정 채널 — `/설정 채널 레벨알림`으로 정한 곳에 모아서 올려요",
    "off": "안 올림 — 조용히 레벨만 올라가요",
}


def xp_to_next(level: int) -> int:
    """다음 레벨까지 필요한 경험치.

    레벨이 오를수록 완만하게 멀어지는 흔한 곡선(5L²+50L+100)이에요. 1→2는 155,
    10→11은 1100쯤 됩니다. 숫자를 바꾸면 **이미 쌓인 경험치의 레벨이 통째로 달라지니**
    운영 중에는 손대지 마세요.
    """
    return 5 * (level ** 2) + 50 * level + 100


def announce_mode(cfg: dict) -> str:
    """설정에서 알림 위치를 꺼냅니다. 모르는 값이면 '말한 채널'로 봐요.

    🔁 예전엔 켜기/끄기(True/False)만 있었어요. 그때 저장된 값이 남아 있어도 그대로
       동작하게 받아둡니다. (True → 말한 채널, False → 안 올림)
    """
    value = cfg.get("announce", "current")
    if value is True:
        return "current"
    if value is False:
        return "off"
    return value if value in ANNOUNCE_MODES else "current"


def level_from_xp(total_xp: int) -> tuple:
    """누적 경험치 → (레벨, 지금 레벨에서 쌓은 경험치, 다음 레벨까지 필요한 양)."""
    level, remain = 0, max(0, int(total_xp))
    while remain >= xp_to_next(level):
        remain -= xp_to_next(level)
        level += 1
    return level, remain, xp_to_next(level)


class MariLevels(commands.Cog):
    """채팅 활동 레벨 — 경험치·레벨업 역할 보상."""

    def __init__(self, bot):
        self.bot = bot
        self._data = None      # 메모리 사본 (파일은 주기적으로만 씁니다)
        self._dirty = False

    async def cog_load(self):
        self._data = load_levels()
        self._data.setdefault("users", {})
        self._data.setdefault("rewards", {})
        self._data.setdefault("config", {})
        self.flush_loop.start()

    async def cog_unload(self):
        self.flush_loop.cancel()
        self._flush()  # 🚪 끌 때는 반드시 한 번 저장하고 나갑니다

    @tasks.loop(seconds=SAVE_INTERVAL_SECONDS)
    async def flush_loop(self):
        self._flush()

    @flush_loop.before_loop
    async def _before_flush(self):
        await self.bot.wait_until_ready()

    def _flush(self):
        if not self._dirty:
            return
        try:
            save_levels(self._data)
            self._dirty = False
        except DataSaveError as e:
            # 💾 경험치는 다음 주기에 다시 시도하면 되니 여기서 죽이지 않아요.
            #    (dirty를 그대로 두는 게 핵심 — 내려야 다음 저장에서 같이 나갑니다)
            print(f"❗ 레벨 저장 실패(다음 주기에 다시 시도): {e}")

    # ---------- 설정 ----------

    def _config(self) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(self._data.get("config", {}))
        return cfg

    def _user(self, user_id) -> dict:
        return self._data["users"].setdefault(str(user_id), {"xp": 0, "last": 0})

    # ---------- 경험치 적립 ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or self._data is None:
            return
        if not is_feature_enabled("level"):
            return
        # 🤖 명령어 접두사는 이 봇에 없지만(전부 슬래시), 다른 봇을 부르는 말은 흔해요.
        #    그것까지 경험치로 쳐주면 도배로 레벨을 올리는 길이 됩니다.
        if message.content.startswith(("!", "?", ".", "/", "-")):
            return

        cfg = self._config()
        entry = self._user(message.author.id)
        now = time.time()
        if now - entry.get("last", 0) < cfg["cooldown"]:
            return

        entry["last"] = now
        before = level_from_xp(entry["xp"])[0]
        entry["xp"] += random.randint(cfg["min_xp"], max(cfg["min_xp"], cfg["max_xp"]))
        after = level_from_xp(entry["xp"])[0]
        self._dirty = True

        if after > before:
            await self._level_up(message, after, cfg)

    async def _level_up(self, message: discord.Message, level: int, cfg: dict):
        """레벨이 올랐을 때 — 역할·재화를 주고 알립니다."""
        member = message.author
        granted = []
        role_id = self._data["rewards"].get(str(level))
        if role_id:
            role = message.guild.get_role(int(role_id))
            # 🔐 보상 역할도 **본인 확인 없이 붙는 역할**이에요. 담을 때만 검사하면 그 뒤에
            #    권한이 붙는 순간, 그 레벨에 닿는 사람마다 관리자를 받게 됩니다.
            #    (셀프 역할·입장 자동 역할과 같은 검사 — mari_utils에 함께 있어요)
            danger = dangerous_permission(role) if role else None
            if danger:
                print(f"🚨 레벨 {level} 보상 역할 차단: {role.name}({role.id})에 '{danger}' 권한이 생겼어요. "
                      "`/레벨 보상삭제`로 빼거나 그 역할의 권한을 내려주세요.")
                role = None
            if role:
                try:
                    await member.add_roles(role, reason=f"레벨 {level} 달성")
                    granted.append(role)
                except Exception as e:
                    print(f"❗ 레벨 역할 부여 실패 ({role.name}): {type(e).__name__}: {e}")

        coin_note = ""
        amount = int(cfg.get("coin_per_level") or 0)
        if amount > 0:
            # 🧩 지갑을 안 담은 서버에서는 이 코그가 아예 없어요. 그럼 재화 보상은 조용히 건너뜁니다.
            economy = self.bot.get_cog("MariEconomy")
            if economy is not None:
                try:
                    await economy.grant_reward(member.id, amount, "레벨업 보상", f"레벨 {level}")
                    coin_note = f" · {currency()} {amount:,}"
                except DataSaveError as e:
                    print(f"❗ 레벨업 재화 지급 실패: {e}")

        mode = announce_mode(cfg)
        if mode == "off":
            return

        channel = message.channel
        if mode == "channel":
            ch_id = load_settings().get("channels", {}).get("level_announce")
            picked = self.bot.get_channel(ch_id) if ch_id else None
            if picked is None:
                # 🔕 '지정 채널'을 골라놓고 채널을 안 정했거나, 그 채널이 지워진 경우예요.
                #    알림을 통째로 삼키는 것보다 말한 채널에 올리는 쪽이 덜 나쁩니다.
                #    (조용히 사라지면 "레벨이 안 오르는 것 같다"는 문의로 돌아와요)
                print("❗ 레벨업 알림 채널이 없어서 말한 채널에 올렸어요. "
                      "`/설정 채널 레벨알림`으로 정하거나 `/레벨 설정`에서 '말한 채널'로 바꿔주세요.")
            else:
                channel = picked

        text = (f"🎉 {member.mention} 님이 **레벨 {level}** 이(가) 됐어요!"
                f"{(' → ' + ', '.join(r.mention for r in granted)) if granted else ''}{coin_note}")
        try:
            await channel.send(text)
        except Exception as e:
            print(f"❗ 레벨업 알림 실패: {type(e).__name__}: {e}")

    # ---------- 명령 ----------

    레벨 = app_commands.Group(name="레벨", description="채팅 활동으로 쌓이는 레벨을 확인하고 관리합니다.")

    @레벨.command(name="확인", description="내 레벨과 경험치를 확인해요. (다른 사람도 볼 수 있어요)")
    @app_commands.describe(멤버="확인할 멤버 (생략하면 나)")
    async def check(self, interaction: discord.Interaction, 멤버: discord.Member = None):
        if await feature_gate(interaction, "level", "레벨"):
            return
        member = 멤버 or interaction.user
        entry = self._data["users"].get(str(member.id))
        if not entry:
            return await interaction.response.send_message(
                f"아직 {member.display_name} 님의 활동 기록이 없어요. 채팅을 하면 쌓이기 시작해요!", ephemeral=True)

        level, now_xp, need = level_from_xp(entry["xp"])
        ranking = sorted(self._data["users"].items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)
        rank = next((i + 1 for i, (uid, _) in enumerate(ranking) if uid == str(member.id)), None)

        filled = int((now_xp / need) * 10) if need else 0
        embed = discord.Embed(title=f"📊 {member.display_name}", color=0x89CFF0)
        embed.add_field(name="레벨", value=f"**{level}**", inline=True)
        embed.add_field(name="순위", value=f"{rank}위 / {len(ranking)}명", inline=True)
        embed.add_field(name="누적 경험치", value=f"{entry['xp']:,}", inline=True)
        embed.add_field(name=f"다음 레벨까지 {need - now_xp:,}",
                        value="🟦" * filled + "⬜" * (10 - filled) + f"  ({now_xp:,}/{need:,})", inline=False)
        await interaction.response.send_message(embed=embed)

    @레벨.command(name="순위", description="레벨이 높은 사람 순으로 봐요.")
    async def ranking(self, interaction: discord.Interaction):
        if await feature_gate(interaction, "level", "레벨"):
            return
        rows = sorted(self._data["users"].items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)[:10]
        if not rows:
            return await interaction.response.send_message("아직 아무 기록도 없어요.", ephemeral=True)

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (uid, entry) in enumerate(rows):
            level = level_from_xp(entry.get("xp", 0))[0]
            lines.append(f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{uid}> — **Lv.{level}** ({entry.get('xp', 0):,})")
        embed = discord.Embed(title="🏆 활동 순위", description="\n".join(lines), color=0xFFD700)
        await interaction.response.send_message(embed=embed)

    # ---------- 관리 ----------

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if await feature_gate(interaction, "level", "레벨"):
            return True
        if not has_admin_or_role(interaction, "level_admin"):
            await interaction.response.send_message("⛔ 레벨을 관리할 권한이 없어요!", ephemeral=True)
            return True
        return False

    @레벨.command(name="보상설정", description="[관리자] 그 레벨에 도달하면 자동으로 붙을 역할을 정해요.")
    @app_commands.describe(레벨="달성 레벨 (1 이상)", 역할="줄 역할")
    async def set_reward(self, interaction: discord.Interaction, 레벨: int, 역할: discord.Role):
        if await self._guard(interaction):
            return
        if 레벨 < 1:
            return await interaction.response.send_message("❌ 레벨은 1 이상이어야 해요.", ephemeral=True)
        # 🔐 레벨만 채우면 **아무 확인 없이** 붙는 역할이에요. 셀프 역할과 같은 검사를 씁니다.
        reason = role_reject_reason(역할, interaction.guild.me)
        if reason:
            return await interaction.response.send_message(reason, ephemeral=True)

        self._data["rewards"][str(레벨)] = 역할.id
        self._dirty = True
        self._flush()
        await interaction.response.send_message(
            f"✅ **레벨 {레벨}** 을(를) 달성하면 {역할.mention} 역할을 드릴게요.\n"
            "⚠️ 이미 그 레벨을 넘긴 사람에게는 소급되지 않아요. 다음 레벨업부터 적용됩니다.", ephemeral=True)

    @레벨.command(name="보상삭제", description="[관리자] 그 레벨의 역할 보상을 없애요.")
    @app_commands.describe(레벨="지울 보상의 레벨")
    async def remove_reward(self, interaction: discord.Interaction, 레벨: int):
        if await self._guard(interaction):
            return
        if self._data["rewards"].pop(str(레벨), None) is None:
            return await interaction.response.send_message(f"❌ 레벨 {레벨}에 걸린 보상이 없어요.", ephemeral=True)
        self._dirty = True
        self._flush()
        await interaction.response.send_message(
            f"🗑️ 레벨 {레벨} 보상을 없앴어요. (이미 받은 사람의 역할은 그대로예요)", ephemeral=True)

    @레벨.command(name="설정", description="[관리자] 경험치 획득량·쿨다운·레벨업 보상을 조정해요. (비운 항목은 그대로 둡니다)")
    @app_commands.describe(최소="메시지 하나에 줄 경험치 최소값", 최대="메시지 하나에 줄 경험치 최대값",
                           쿨다운="이 초 안에 또 말해도 경험치를 안 줘요 (도배 방지)",
                           레벨업보상=f"레벨이 오를 때마다 줄 {currency()} (0이면 안 줌)",
                           알림="레벨업 축하를 어디에 올릴지")
    @app_commands.choices(알림=[
        app_commands.Choice(name="말한 채널", value="current"),
        app_commands.Choice(name="지정 채널 (/설정 채널 레벨알림)", value="channel"),
        app_commands.Choice(name="안 올림", value="off"),
    ])
    async def configure(self, interaction: discord.Interaction, 최소: int = None, 최대: int = None,
                        쿨다운: int = None, 레벨업보상: int = None,
                        알림: app_commands.Choice[str] = None):
        if await self._guard(interaction):
            return

        cfg = self._data.setdefault("config", {})
        if 최소 is not None:
            cfg["min_xp"] = max(1, 최소)
        if 최대 is not None:
            cfg["max_xp"] = max(1, 최대)
        if 쿨다운 is not None:
            cfg["cooldown"] = max(0, 쿨다운)
        if 레벨업보상 is not None:
            cfg["coin_per_level"] = max(0, 레벨업보상)
        if 알림 is not None:
            cfg["announce"] = 알림.value
        self._dirty = True
        self._flush()

        now = self._config()
        note = ""
        if now["coin_per_level"] > 0 and self.bot.get_cog("MariEconomy") is None:
            note = f"\n⚠️ 이 서버에는 지갑 기능이 없어서 {currency()} 보상은 나가지 않아요."
        where_note = ""
        if announce_mode(now) == "channel" and not load_settings().get("channels", {}).get("level_announce"):
            where_note = ("\n  ⚠️ 아직 채널을 안 정하셨어요. `/설정 채널 레벨알림`으로 정하기 전까지는 "
                          "말한 채널에 올라가요.")
        rewards = self._data.get("rewards", {})
        # ✂️ 보상이 수십 개면 이 안내가 본문 한도(2000자)를 넘겨서 **응답이 통째로 실패**해요.
        #    설정은 이미 저장된 뒤라, 관리자에겐 "안 먹혔나?"로 보입니다.
        reward_text = clip(
            ", ".join(f"Lv.{lv} → <@&{rid}>" for lv, rid in sorted(rewards.items(), key=lambda kv: int(kv[0])))
            or "없음", EMBED_FIELD_LIMIT)
        await interaction.response.send_message(
            f"⚙️ **레벨 설정**\n"
            f"· 경험치: 메시지당 {now['min_xp']}~{now['max_xp']} (쿨다운 {now['cooldown']}초)\n"
            f"· 레벨업 보상: {now['coin_per_level']:,} {currency()}\n"
            f"· 레벨업 알림: {ANNOUNCE_MODES[announce_mode(now)]}{where_note}\n"
            f"· 역할 보상: {reward_text}{note}", ephemeral=True)

    @레벨.command(name="조정", description="[관리자] 특정 멤버의 누적 경험치를 더하거나 뺍니다. (실수 복구용)")
    @app_commands.describe(멤버="조정할 멤버", 경험치="더할 값 (빼려면 음수)")
    async def adjust(self, interaction: discord.Interaction, 멤버: discord.Member, 경험치: int):
        if await self._guard(interaction):
            return
        entry = self._user(멤버.id)
        before = level_from_xp(entry["xp"])[0]
        entry["xp"] = max(0, entry["xp"] + 경험치)
        after = level_from_xp(entry["xp"])[0]
        self._dirty = True
        self._flush()
        await interaction.response.send_message(
            f"✅ {멤버.mention} 의 경험치를 {경험치:+,} 했어요. (누적 {entry['xp']:,} · 레벨 {before} → {after})\n"
            f"⚠️ 레벨이 올랐어도 역할 보상은 자동으로 나가지 않아요. 필요하면 `/역할부여`로 직접 주세요.",
            ephemeral=True)
