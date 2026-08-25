"""ChunsikTest — 관리자 전용 진단·테스트 도구 모음 (/테스트 그룹)."""

import asyncio
import os
import datetime as dt
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands

from chunsik_config import DATA_DIR, KST, json_data_files, module_active
from chunsik_storage import SAVE_FAILURES, safe_json_load
from chunsik_settings import _get_role_ids, has_admin_or_role, load_settings
from chunsik_state import state
from chunsik_utils import KNOWN_PLATFORMS, _looks_like_id_entry, _split_platform_and_id, normalize_platform
from chunsik_names import bot_name, currency, event_name

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

    # ---------- 1. 채널점검 ----------
    @test_group.command(name="채널점검", description="[관리자] 설정된 채널에 전부 테스트 메세지를 보내보고, 문제(권한 없음/미설정)를 한 번에 리포트해요.")
    async def test_channels(self, interaction: discord.Interaction):
        if not self._is_server_admin(interaction):
            return await interaction.response.send_message("⛔ 서버 관리자 또는 테스트 관리자만 사용할 수 있어요.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        channel_labels = {
            "attendance": "📅 출석체크", "economy_log": "💰 경제 로그", "shop_log": "🛒 상점 로그",
            "stock_board": "📈 주식 전광판", "stock_log": "📊 주식 로그", "closing_log": "🔔 종가 게시판",
            "id_log": "🆔 아이디 로그", "role_log": "👥 역할 로그",
            "birthday_announce": "🎂 생일 알림", "birthday_log": "🎉 생일 로그", "evashi_announce": f"🎉 {event_name()} 안내",
            "id_submit": "🆔 아이디 자동등록", "level_roster": "📋 아이디 명단",
        }
        settings = load_settings()
        channels = settings.get("channels", {})
        ok, failed, missing = [], [], []

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
                msg = await channel.send(f"🧪 `/테스트 채널점검` - {label} 채널 발송 테스트예요. (5초 후 자동 삭제)")
                ok.append(label)
                await asyncio.sleep(5)
                await msg.delete()
            except discord.Forbidden:
                failed.append(f"{label} (봇 권한 부족)")
            except Exception as e:
                failed.append(f"{label} ({e})")

        embed = discord.Embed(title="🧪 채널점검 결과", color=discord.Color.blurple())
        embed.add_field(name=f"✅ 정상 ({len(ok)})", value=", ".join(ok) if ok else "없음", inline=False)
        embed.add_field(name=f"❌ 실패 ({len(failed)})", value="\n".join(failed) if failed else "없음", inline=False)
        embed.add_field(name=f"⚠️ 미설정 ({len(missing)})", value=", ".join(missing) if missing else "없음", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------- 2. 권한확인 ----------
    @test_group.command(name="권한확인", description="[관리자] 본인이 가진 관리자 권한을 한눈에 확인해요.")
    @app_commands.describe(유저="확인할 유저 (생략 시 본인)")
    async def test_permissions(self, interaction: discord.Interaction, 유저: Optional[discord.Member] = None):
        if not self._is_server_admin(interaction):
            return await interaction.response.send_message("⛔ 서버 관리자 또는 테스트 관리자만 사용할 수 있어요.", ephemeral=True)
        target = 유저 or interaction.user
        settings = load_settings()
        role_map = settings.get("roles", {})
        role_labels = {
            "ids_admin": "🆔 아이디 관리자", "shop_admin": "🛒 상점 관리자", "stock_admin": "📈 주식 관리자",
            "evashi_admin": f"🎉 {event_name()} 관리자",
            "chronicle_admin": "📜 연대기 관리자", "chief_role": "👑 대장",
            "test_admin": "🧪 테스트 관리자",
        }
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

        embed = discord.Embed(title="🧪 아이디 파싱 미리보기", description="\n".join(lines)[:4000], color=discord.Color.blurple())
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

        # ⑤ [신규] 데이터 폴더가 클라우드 동기화 폴더 안에 있는지 확인
        # OneDrive 안에서는 저장(os.replace)이 거부되거나 충돌 사본이 생길 수 있어요.
        # 📁 [수정] 데이터가 data/ 폴더로 분리됐으므로, 코드 위치(BASE_DIR)가 아니라
        # 실제 저장이 일어나는 DATA_DIR을 검사해야 맞아요.
        lowered = DATA_DIR.lower()
        cloud = next((n for n in ("onedrive", "dropbox", "google drive", "icloud") if n in lowered), None)
        if cloud:
            report.append(f"⚠️ 데이터 폴더가 **{cloud}** 동기화 폴더 안에 있어요 — 저장 실패/충돌 사본 위험\n　`{DATA_DIR}`")
        else:
            report.append(f"✅ 데이터 폴더 위치: 클라우드 동기화 폴더 아님\n　`{DATA_DIR}`")

        embed = discord.Embed(title="🧪 데이터 무결성 점검 결과", description="\n".join(report), color=discord.Color.blurple())
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
            description="\n".join(lines)[:4000],
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- 10. 세금환수 미리보기 ----------
