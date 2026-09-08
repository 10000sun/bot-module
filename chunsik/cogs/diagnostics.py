"""ChunsikTest — 관리자 전용 진단·테스트 도구 모음 (/테스트 그룹)."""

import asyncio
import os
import datetime as dt
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands

from chunsik_config import BACKUP_DIR, DATA_DIR, KST, json_data_files, module_active
from chunsik_storage import SAVE_FAILURES, safe_json_load
from chunsik_settings import _get_role_ids, has_admin_or_role, load_settings
from chunsik_state import state
from chunsik_utils import (EMBED_DESC_LIMIT, EMBED_FIELD_LIMIT, KNOWN_PLATFORMS, _looks_like_id_entry,
                          _split_platform_and_id, add_lines_field, clip, fit_embed,
                          normalize_platform)
from chunsik_names import bot_name, currency, event_name

# ✂️ 실패 줄에 붙는 오류 원문 길이. 예외 문구는 길이에 제한이 없어서 그대로 실으면
#    한 줄이 칸 하나(1024자)를 통째로 먹습니다.
ERROR_TEXT_LIMIT = 120


def build_channel_report(ok: list, failed: list, missing: list) -> discord.Embed:
    """`/테스트 채널점검` 결과 화면.

    🐛 [버그 수정] 예전엔 세 칸에 `", ".join(...)`을 그대로 실었어요. 지정할 수 있는
    채널이 18개인데 **실패 줄에는 예외 원문까지 붙습니다**(`f"{label} ({e})"`). 길이 제한이
    없는 값이라 칸 하나가 1024자를 넘기면 **결과 화면이 통째로 400으로 거부**돼요.

    하필 **전부 실패했을 때** — 봇 권한을 아직 안 준 설치 직후가 딱 그 상태예요 — 결과를
    못 봅니다. 점검 도구가 정작 문제가 있을 때만 안 뜨는 셈이라 제일 나쁜 자리였어요.

    🔎 모듈 바깥에 둔 이유: tools/check_embeds.py가 진짜 코드를 그대로 불러서 재게 하려고요.
    """
    embed = discord.Embed(title="🧪 채널점검 결과", color=discord.Color.blurple())
    add_lines_field(embed, f"✅ 정상 ({len(ok)})", ok, empty="없음")
    # 실패 줄은 하나도 빠뜨리면 안 돼요 — 그게 고칠 목록이니까요. 기본 예산(2048자)으로는
    # 채널 18개가 전부 실패하면 뒷줄이 "…외 N줄"로 잘립니다. 넉넉히 잡고 총량은 fit_embed에 맡겨요.
    add_lines_field(embed, f"❌ 실패 ({len(failed)})", failed, empty="없음",
                    budget=EMBED_FIELD_LIMIT * 4)
    add_lines_field(embed, f"⚠️ 미설정 ({len(missing)})", missing, empty="없음")
    # 🧮 칸을 각각 맞춰도 셋이 쌓이면 전체 6000자를 넘을 수 있어요.
    return fit_embed(embed)


# ========== 🧪 [신규] 관리자 전용 테스트 도구 모음 ==========

class ChunsikTest(commands.Cog):
    """실제 데이터에 영향 없이(또는 최소한으로) 각 시스템이 제대로 도는지 확인하는 관리자 전용 테스트 명령어 모음"""

    test_group = app_commands.Group(name="테스트", description="[관리자] 각 시스템 점검용 테스트 명령어 모음", guild_only=True)

    # 🧩 하위 명령이 어느 모듈을 점검하는지. 여기 없는 건 어느 모듈에도 안 매인
    # 공용 점검(채널·권한·데이터·동기화·백업)이라 항상 남습니다.
    #
    # 진단은 코어라 항상 올라오는데, 예전엔 하위 명령이 **어떤 모듈이 담겼는지 보지
    # 않았어요.** 게임을 안 담아도 `/테스트 이벤트`가, 주식을 안 담아도
    # `/테스트 종가게시미리보기`가 그대로 떴습니다. 눌러도 아무 의미가 없고,
    # 클라이언트에겐 "있는데 고장난 기능"처럼 보여요.
    _MODULE_COMMANDS = {
        "이벤트": "games",
        "출석초기화": "economy",
        "아이디파싱": "id",
        "ai상태": "gpt",
        "종가게시미리보기": "stock",
    }

    def __init__(self, bot):
        self.bot = bot
        # ⏱️ 코그가 트리에 등록되기 **전에** 걷어내야 해요. cog_load는 등록보다 나중에
        #    불립니다. (cogs/setting.py의 _prune_module_commands와 같은 이유)
        self._prune_module_commands()

    def _prune_module_commands(self):
        """이번 배포에 담기지 않은 기능의 점검 명령을 걷어냅니다.

        ⚠️ 하위 명령이 0개인 그룹은 디스코드가 등록을 거부해서 **동기화 전체가
           실패**합니다. 공용 점검이 다섯 개나 남아 있어 실제로 빌 일은 없지만,
           나중에 그것들까지 모듈에 매이게 되면 조용히 봇이 죽어요. 방어선을 둡니다.
        """
        removed = []
        for name, key in self._MODULE_COMMANDS.items():
            if module_active(key):
                continue
            # 🐛 remove_command()는 지운 명령을 돌려주지 않아요(항상 None).
            #    반환값으로 판단하면 로그에 아무것도 안 남습니다.
            if self.test_group.get_command(name) is None:
                continue
            self.test_group.remove_command(name)
            removed.append(f"/테스트 {name}")

        if not self.test_group.commands:
            self.bot.tree.remove_command(self.test_group.name)
            removed.append("/테스트 (하위 명령이 없어서)")

        if removed:
            print(f"🧩 담지 않은 기능의 점검 명령 {len(removed)}개를 뺐어요: {', '.join(removed)}")

    def _is_server_admin(self, interaction: discord.Interaction) -> bool:
        # 🔑 서버 관리자 또는 '/설정 관리자 테스트'로 지정된 테스트 관리자 역할 보유자만 사용 가능
        return has_admin_or_role(interaction, "test_admin")

    def _channel_labels(self) -> dict:
        """점검할 채널 {키: 이름표}. `/설정` 명령이 쓰는 표를 그대로 빌려옵니다.

        🐛 [버그 수정] 예전엔 여기에 채널 13개를 **손으로 적어둔 표**가 따로 있었어요.
           그런데 그 뒤에 채널이 늘면서 표는 안 따라갔고, `/설정`으로 지정할 수 있는 18개 중
           **5개가 점검에서 통째로 빠져 있었습니다** — 상점 전광판·환영·입퇴장 로그·
           내전 로그·레벨 알림. 하필 상점 전광판처럼 유저가 제일 자주 보는 채널이 빠져서,
           "점검 다 통과했는데 왜 안 되지"가 될 수 있는 자리였어요.

           점검 명령이 놓치는 건 조용합니다. 결과에 안 나오니 아무도 빠진 줄 몰라요.
           그래서 표를 두 벌 두지 않고, 지정하는 쪽 표를 그대로 씁니다. 이제 채널을
           새로 만들면 `/설정`에 넣는 순간 점검에도 자동으로 들어와요.

        🧩 `from cogs.setting import ...` 하지 않는 이유는 wizard._tables()와 같아요.
           코그끼리 직접 import하면 한쪽만 담아 납품했을 때 import 단계에서 죽습니다.
        """
        cog = self.bot.get_cog("ChunsikSetting")
        if cog is None:
            return {}
        # 이름표는 `/설정 채널 …`의 하위 명령 이름이에요. 관리자가 이미 그 이름으로
        # 지정했으니, 점검 결과도 같은 이름으로 보여야 어느 채널인지 바로 압니다.
        return {key: name for name, key in cog._CHANNEL_COMMANDS.items()}

    def _role_labels(self) -> dict:
        """점검할 관리자 역할 {키: 이름표}. 채널과 같은 이유로 `/설정` 표를 빌려옵니다.

        🐛 [버그 수정] 여기도 손으로 적은 표라 같이 낡아 있었어요. 지정할 수 있는 역할 11개 중
           **5개가 빠져 있었습니다** — 셀프역할·입장·레벨·파티·내전 관리자. 나중에 들어온
           다섯 코그의 것만 정확히 빠졌어요. 그 역할을 받은 사람이 `/테스트 권한확인`을 하면
           "특별한 관리 권한이 없어요"가 나옵니다.

        👑 대장은 `/설정 명단 대장`으로 따로 지정하는 자리라 표에 없어요. 손으로 붙입니다.
        """
        cog = self.bot.get_cog("ChunsikSetting")
        labels = {key: f"{name} 관리자" for name, key in cog._ROLE_COMMANDS.items()} if cog else {}
        labels["chief_role"] = "👑 대장"
        return labels

    # ---------- 1. 채널점검 ----------
    @test_group.command(name="채널점검", description="[관리자] 설정된 채널에 전부 테스트 메세지를 보내보고, 문제(권한 없음/미설정)를 한 번에 리포트해요.")
    async def test_channels(self, interaction: discord.Interaction):
        if not self._is_server_admin(interaction):
            return await interaction.response.send_message("⛔ 서버 관리자 또는 테스트 관리자만 사용할 수 있어요.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        channel_labels = self._channel_labels()
        settings = load_settings()
        channels = settings.get("channels", {})
        ok, failed, missing = [], [], []
        sent = []      # 보낸 테스트 메세지 (전부 보낸 뒤 한꺼번에 지워요)

        for key, label in channel_labels.items():
            ch_id = channels.get(key)
            if not ch_id:
                missing.append(label)
                continue
            channel = interaction.guild.get_channel(ch_id)
            if not channel:
                failed.append(f"{label} (채널 자체를 못 찾음)")
                continue
            try:
                msg = await channel.send(f"🧪 `/테스트 채널점검` - {label} 채널 발송 테스트예요. (곧 자동 삭제)")
                ok.append(label)
                sent.append(msg)
            except discord.Forbidden:
                failed.append(f"{label} (봇 권한 부족)")
            except Exception as e:
                # 예외 문구는 길이 제한이 없어요. 한 줄이 칸 하나를 다 먹지 않게 먼저 자릅니다.
                failed.append(f"{label} ({clip(str(e), ERROR_TEXT_LIMIT)})")

        # 🧹 전부 보낸 **뒤에** 한 번만 쉬고 한꺼번에 지웁니다.
        #    예전엔 채널마다 5초씩 기다리고 지웠어요. 채널이 13개일 땐 65초였는데
        #    이제 18개라 90초가 됩니다. 그동안 명령을 부른 사람은 아무 답도 못 받고,
        #    테스트 메세지는 채널마다 5초씩 차례로 남아 있어요.
        #    한 번에 지우면 "5초쯤 떴다가 사라진다"는 성질은 그대로면서 전체가 5초에 끝납니다.
        if sent:
            await asyncio.sleep(5)
            for msg in sent:
                try:
                    await msg.delete()
                except Exception:
                    # 지우기 실패는 점검 결과와 상관없어요. (메세지 관리 권한이 없는 채널 등)
                    pass

        await interaction.followup.send(embed=build_channel_report(ok, failed, missing), ephemeral=True)

    # ---------- 2. 권한확인 ----------
    @test_group.command(name="권한확인", description="[관리자] 본인이 가진 관리자 권한을 한눈에 확인해요.")
    @app_commands.describe(유저="확인할 유저 (생략 시 본인)")
    async def test_permissions(self, interaction: discord.Interaction, 유저: Optional[discord.Member] = None):
        if not self._is_server_admin(interaction):
            return await interaction.response.send_message("⛔ 서버 관리자 또는 테스트 관리자만 사용할 수 있어요.", ephemeral=True)
        target = 유저 or interaction.user
        settings = load_settings()
        role_labels = self._role_labels()
        has = []
        for key, label in role_labels.items():
            role_ids = _get_role_ids(settings, key)
            if role_ids and any(r.id in role_ids for r in target.roles):
                has.append(label)
        if target.guild_permissions.administrator:
            has.append("🛡️ 서버 관리자 (모든 권한 보유)")

        embed = discord.Embed(
            title=f"🔑 {target.display_name}님의 권한 현황",
            description="\n".join(f"• {h}" for h in has) if has else "특별한 관리 권한이 없어요.",
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- 3. 이벤트 강제 오픈 ----------
    @test_group.command(name="이벤트", description=f"[관리자] {event_name()} 이벤트 창을 짧게 강제로 열어서 전체 흐름(리액션/마감공지/보상)을 테스트해요.")
    @app_commands.describe(초="테스트용 창 지속시간(초). 기본 15초")
    async def test_evashi(self, interaction: discord.Interaction, 초: int = 15):
        if not self._is_server_admin(interaction):
            return await interaction.response.send_message("⛔ 서버 관리자 또는 테스트 관리자만 사용할 수 있어요.", ephemeral=True)
        games_cog = self.bot.get_cog("ChunsikGames")
        if not games_cog:
            return await interaction.response.send_message(f"❌ {event_name()} 시스템을 찾을 수 없어요.", ephemeral=True)

        games_cog.evashi_participants = set()
        games_cog.evashi_first_claimed = 0
        games_cog.evashi_first_winners = []
        games_cog.evashi_guild = None
        games_cog.evashi_window_open_until = dt.datetime.now(KST) + dt.timedelta(seconds=초)
        if games_cog.evashi_close_task and not games_cog.evashi_close_task.done():
            games_cog.evashi_close_task.cancel()
        games_cog.evashi_close_task = asyncio.create_task(games_cog._close_evashi_window_later(초))

        await interaction.response.send_message(
            f"🧪 테스트용 {event_name()} 창을 **{초}초** 동안 열었어요! 아무 채널에서나 \"{event_name()}\"라고 쳐보세요.", ephemeral=True
        )

    # ---------- 4. 출석초기화 ----------
    @test_group.command(name="출석초기화", description="[관리자] 본인의 오늘 출석 기록만 지워서 /출석을 반복 테스트할 수 있게 해요.")
    async def test_reset_attendance(self, interaction: discord.Interaction):
        if not self._is_server_admin(interaction):
            return await interaction.response.send_message("⛔ 서버 관리자 또는 테스트 관리자만 사용할 수 있어요.", ephemeral=True)
        economy_cog = self.bot.get_cog("ChunsikEconomy")
        if not economy_cog:
            return await interaction.response.send_message("❌ 경제 시스템을 찾을 수 없어요.", ephemeral=True)

        # 🔒 [일관성] 출석 파일은 /출석과 자정 초기화가 economy_lock 안에서 읽고→고치고→저장해요.
        # 여기만 락 없이 같은 일을 하면, 테스트 중에 누가 /출석을 누를 경우 둘 중 하나가
        # 통째로 덮어써집니다. 같은 규칙을 따르게 맞췄어요.
        async with self.bot.economy_lock:
            data = economy_cog._load_attendance()
            uid = str(interaction.user.id)
            if uid in data.get("today_users", []):
                data["today_users"].remove(uid)
                data["today_count"] = max(0, data.get("today_count", 1) - 1)
            if uid in data.get("user_stats", {}):
                data["user_stats"][uid] = max(0, data["user_stats"][uid] - 1)
            economy_cog._save_attendance(data)
        await interaction.response.send_message("🧹 본인의 오늘 출석 기록을 지웠어요. `/출석`을 다시 테스트해보세요.", ephemeral=True)

    # ---------- 5. 아이디 파싱 미리보기 ----------
    @test_group.command(name="아이디파싱", description="[관리자] 실제로 등록하지 않고, 이 텍스트가 어떻게 인식될지 미리보기만 해요.")
    @app_commands.describe(텍스트="아이디 자동등록 채널에 올릴 것처럼 시험해볼 텍스트")
    async def test_id_parsing(self, interaction: discord.Interaction, 텍스트: str):
        if not self._is_server_admin(interaction):
            return await interaction.response.send_message("⛔ 서버 관리자 또는 테스트 관리자만 사용할 수 있어요.", ephemeral=True)

        raw_segments = [seg.strip() for chunk in 텍스트.split("\n") for seg in chunk.split(",") if seg.strip()]
        lines = []
        for seg in raw_segments:
            looks_ok = _looks_like_id_entry(seg)
            parsed = _split_platform_and_id(seg)
            if not parsed:
                lines.append(f"`{seg}` → {'😐 잡담으로 무시됨' if not looks_ok else '❓ 형식 인식 실패, 관리자 확인 대기열로'}")
                continue
            plat_input, game_id = parsed
            normalized = normalize_platform(plat_input)
            if normalized in KNOWN_PLATFORMS:
                lines.append(f"`{seg}` → ✅ 바로 등록됨: **{normalized}** = `{game_id}`")
            else:
                lines.append(f"`{seg}` → 🔍 플랫폼 미인식('{plat_input}'), 관리자 확인 대기열로 (아이디는 `{game_id}`로 인식)")

        embed = discord.Embed(title="🧪 아이디 파싱 미리보기",
                              description=clip("\n".join(lines), EMBED_DESC_LIMIT),
                              color=discord.Color.blurple())
        embed.set_footer(text="실제로 등록되지 않아요. 순수 미리보기예요.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- 6. 봇 상태 ----------
    @test_group.command(name="ai상태", description="[관리자] Gemini API 연결과 RPM/RPD 사용량을 확인해요.")
    async def test_chunsik_status(self, interaction: discord.Interaction):
        if not self._is_server_admin(interaction):
            return await interaction.response.send_message("⛔ 서버 관리자 또는 테스트 관리자만 사용할 수 있어요.", ephemeral=True)
        gpt_cog = self.bot.get_cog("ChunsikGPT")
        if not gpt_cog:
            return await interaction.response.send_message(f"❌ {bot_name()} AI 시스템을 찾을 수 없어요.", ephemeral=True)

        current_time = dt.datetime.now().timestamp()
        recent_calls = [t for t in gpt_cog.minutely_timestamps if current_time - t < 60]
        daily_count = gpt_cog._get_daily_count()

        embed = discord.Embed(title=f"🤖 {bot_name()} AI 상태", color=discord.Color.green())
        embed.add_field(name="분당 호출", value=f"{len(recent_calls)} / {gpt_cog.RPM_LIMIT}", inline=True)
        embed.add_field(name="오늘 호출", value=f"{daily_count} / {gpt_cog.RPD_LIMIT}", inline=True)
        embed.add_field(name="API 키 설정", value="✅ 있음" if gpt_cog.GEMINI_API_KEY else "❌ 없음", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- 7. 데이터 점검 ----------
    @test_group.command(name="데이터점검", description="[관리자] 데이터 파일들의 무결성(중복/손상/누락)을 한 번에 점검해요.")
    async def test_data_check(self, interaction: discord.Interaction):
        if not self._is_server_admin(interaction):
            return await interaction.response.send_message("⛔ 서버 관리자 또는 테스트 관리자만 사용할 수 있어요.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        report = []

        # ① 아이디 중복 검사 (실제로 정리는 안 하고 개수만)
        # 🧩 아이디를 안 담은 배포에서는 건너뜁니다. 예전엔 무조건 돌아서 "아이디 중복:
        #    0건"을 보고했는데, 안내하는 `/아이디 중복정리`가 그 서버엔 없는 명령이었어요.
        #    (아래 ②가 상점 코그를 get_cog로 확인하는 것과 같은 이유입니다)
        if module_active("id"):
            gid = str(interaction.guild.id)
            guild_ids = state.user_ids.get(gid, {})
            dup_count = 0
            for uid, platforms in guild_ids.items():
                seen = set()
                for key, val in platforms.items():
                    norm_val = val.strip() if isinstance(val, str) else val
                    if norm_val in seen:
                        dup_count += 1
                    else:
                        seen.add(norm_val)
            report.append(f"{'✅' if dup_count == 0 else '⚠️'} 아이디 중복: {dup_count}건 (`/아이디 중복정리`로 정리 가능)")

        # ② 상점 아이템 되팔기퍼센트 누락 검사
        shop_cog = self.bot.get_cog("ChunsikShop")
        if shop_cog:
            missing_resale = 0
            shop_data = shop_cog._load_shop()
            for board in shop_data.get("boards", {}).values():
                for info in board.get("items", {}).values():
                    if "resale_percent" not in info:
                        missing_resale += 1
            report.append(f"{'✅' if missing_resale == 0 else '⚠️'} 상점 되팔기퍼센트 누락: {missing_resale}건")

        # ③ 전체 JSON 파일 로드 가능 여부
        # ⚠️ [수정] 예전엔 *_FILE 상수를 전부 검사해서, JSON이 아닌 .env와
        # 심장박동 파일이 파싱에 실패해 "손상됨"으로 잘못 보고됐어요.
        file_consts = json_data_files()
        broken = []
        for name, path in file_consts.items():
            try:
                safe_json_load(path, {})
            except Exception:
                broken.append(os.path.basename(path))
        report.append(f"{'✅' if not broken else '❌'} 손상된 데이터 파일: {', '.join(broken) if broken else '없음'}")

        # ④ [신규] 최근 저장 실패 기록
        # 저장 실패는 "명령은 처리됐는데 파일에 안 남은" 상태라 제일 위험해요.
        # OneDrive·백신 같은 프로그램이 파일을 붙잡고 있으면 여기에 쌓입니다.
        if SAVE_FAILURES:
            recent = list(SAVE_FAILURES)[-5:]
            detail = "\n".join(f"　• `{f['time']}` **{f['file']}** — {f['error'][:80]}" for f in recent)
            report.append(f"❌ 최근 저장 실패: 총 {len(SAVE_FAILURES)}건 (최근 {len(recent)}건)\n{detail}")
        else:
            report.append("✅ 최근 저장 실패: 없음")

        # ⑤ 마지막 백업이 언제였는지.
        # 백업은 새벽 3시에만 도는데, 그 시각에 봇이 꺼져 있으면 그 회차는 그냥 지나가요.
        # 낮에만 켜두는 서버라면 한 번도 안 만들어질 수 있는데 **아무도 그걸 모릅니다.**
        # (기동할 때 따라잡게 해뒀지만, 눈으로 확인할 창구도 있어야 해요)
        backup_cog = self.bot.get_cog("ChunsikBackup")
        if backup_cog is not None:
            import cogs.backup as backup_mod
            try:
                names = os.listdir(BACKUP_DIR) if os.path.exists(BACKUP_DIR) else []
            except Exception:
                names = []
            latest = backup_mod.latest_backup_day(names)
            today = dt.datetime.now(KST).date()
            if latest is None:
                report.append("❌ 마지막 백업: **없음** — `/테스트 백업실행`으로 지금 한 번 돌려주세요")
            else:
                days = (today - latest).days
                mark = "✅" if days <= 1 else "⚠️"
                report.append(f"{mark} 마지막 백업: {latest} ({days}일 전 · 보관 {backup_cog.RETENTION_DAYS}일)")

        # ⑥ [신규] 데이터 폴더가 클라우드 동기화 폴더 안에 있는지 확인
        # OneDrive 안에서는 저장(os.replace)이 거부되거나 충돌 사본이 생길 수 있어요.
        # 📁 [수정] 데이터가 data/ 폴더로 분리됐으므로, 코드 위치(BASE_DIR)가 아니라
        # 실제 저장이 일어나는 DATA_DIR을 검사해야 맞아요.
        lowered = DATA_DIR.lower()
        cloud = next((n for n in ("onedrive", "dropbox", "google drive", "icloud") if n in lowered), None)
        if cloud:
            report.append(f"⚠️ 데이터 폴더가 **{cloud}** 동기화 폴더 안에 있어요 — 저장 실패/충돌 사본 위험\n　`{DATA_DIR}`")
        else:
            report.append(f"✅ 데이터 폴더 위치: 클라우드 동기화 폴더 아님\n　`{DATA_DIR}`")

        # ✂️ 손상 파일 목록·최근 저장 실패 원문이 길어지면 설명 한도를 넘겨요.
        #    점검 결과를 못 보게 되는 게 제일 나쁩니다.
        embed = discord.Embed(title="🧪 데이터 무결성 점검 결과",
                              description=clip("\n".join(report), EMBED_DESC_LIMIT),
                              color=discord.Color.blurple())
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------- 8. 명령어 강제 동기화 ----------
    @test_group.command(name="명령어동기화", description="[관리자] 슬래시 명령어 목록을 디스코드와 강제로 다시 동기화해요.")
    async def test_sync(self, interaction: discord.Interaction):
        if not self._is_server_admin(interaction):
            return await interaction.response.send_message("⛔ 서버 관리자 또는 테스트 관리자만 사용할 수 있어요.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            # 🐛 [버그 수정] 예전엔 `tree.sync(guild=interaction.guild)`였어요. 그런데 이 봇의 명령어는
            # setup_hook에서 전부 **글로벌**로 등록됩니다(guild= 지정 없이 add_cog). `guild=`를 주면
            # "이 서버 전용으로 따로 등록된 명령어 목록"을 동기화하는데 그건 언제나 비어 있어서,
            # 정작 글로벌 목록은 손도 안 대고 `✅ 명령어 0개 동기화`만 뜨는 상태였어요.
            # (게다가 서버 전용 오버라이드가 있었다면 그걸 지워버립니다)
            synced = await self.bot.tree.sync()
            await interaction.followup.send(
                f"✅ 글로벌 명령어 {len(synced)}개를 다시 동기화했어요. (반영까지 최대 한 시간 걸릴 수 있어요)",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"❌ 동기화 실패: {e}", ephemeral=True)

    @test_group.command(name="백업실행", description="[관리자] 매일 새벽 3시에 자동으로 도는 데이터 백업을 지금 즉시 실행해요.")
    async def test_backup_now(self, interaction: discord.Interaction):
        if not self._is_server_admin(interaction):
            return await interaction.response.send_message("⛔ 서버 관리자 또는 테스트 관리자만 사용할 수 있어요.", ephemeral=True)
        backup_cog = self.bot.get_cog("ChunsikBackup")
        if not backup_cog:
            return await interaction.response.send_message("❌ 백업 시스템을 찾을 수 없어요.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            copied, removed, failed = await asyncio.to_thread(backup_cog._run_backup)
            if failed:
                lines = "\n".join(f"• `{name}`: {err}" for name, err in failed[:5])
                more = f"\n... 외 {len(failed) - 5}개" if len(failed) > 5 else ""
                await interaction.followup.send(
                    f"⚠️ 백업이 **일부만** 됐어요. 파일 {copied}개 저장, **{len(failed)}개 실패**"
                    f"(오래된 백업 {removed}개 정리)\n"
                    f"백업 폴더 이름 끝에 `.partial`이 붙어 있어요.\n\n{lines}{more}",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(f"💾 백업 완료! 파일 {copied}개 저장, 오래된 백업 {removed}개 정리했어요.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ 백업 실패: {e}", ephemeral=True)

    # ---------- 9. 종가게시 미리보기 ----------
    @test_group.command(name="종가게시미리보기", description="[관리자] 실제로 마감하지 않고, 지금 종가게시하면 어떻게 될지만 미리 보여줘요.")
    async def test_closing_preview(self, interaction: discord.Interaction):
        if not self._is_server_admin(interaction):
            return await interaction.response.send_message("⛔ 서버 관리자 또는 테스트 관리자만 사용할 수 있어요.", ephemeral=True)
        stock_cog = self.bot.get_cog("ChunsikStock")
        if not stock_cog:
            return await interaction.response.send_message("❌ 주식 시스템을 찾을 수 없어요.", ephemeral=True)

        data = stock_cog._load_stocks()
        stocks_data = data.get("stocks", {})
        if not stocks_data:
            return await interaction.response.send_message("ℹ️ 상장된 종목이 없어요.", ephemeral=True)

        lines = []
        for name, info in stocks_data.items():
            old_price = info.get("price", 0)
            had_pending = info.get("pending_price") is not None
            new_price = info["pending_price"] if had_pending else old_price
            reason = info.get("pending_reason") if had_pending else info.get("reason", "정보 없음")
            change = new_price - old_price
            arrow = "🔺" if change > 0 else ("🔻" if change < 0 else "➖")
            lines.append(f"{arrow} **{name}**: {old_price:,} → {new_price:,} {currency()} (사유: {reason})")

        embed = discord.Embed(
            title="🧪 종가게시 미리보기 (실제로 반영되지 않았어요)",
            description=clip("\n".join(lines), EMBED_DESC_LIMIT),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- 10. 세금환수 미리보기 ----------
