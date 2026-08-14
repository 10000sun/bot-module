"""MariStock — 주식 매매, 종가 게시, 주가 그래프, 개폐장 관리."""

import asyncio
import os
import io
import datetime as dt
from typing import Optional
import holidays
import discord
from discord import app_commands
from discord.ext import commands
# 📈 주가 그래프 생성용. 서버엔 화면이 없어서 'Agg' 백엔드를 꼭 지정해야 해요.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

from mari_config import CHART_BG, CHART_DOWN, CHART_GRID, CHART_INK, CHART_INK_SOFT, CHART_UP, ECONOMY_FILE, KST, PORTFOLIO_HISTORY_FILE, STOCKS_FILE, _CHART_EMOJI_RE
from mari_storage import atomic_json_save, atomic_json_save_or_raise, safe_json_load
from mari_settings import LOG_STYLES, build_log_embed, feature_gate, has_admin_or_role, load_settings
from mari_state import record_ledger
from mari_utils import (MariView, chunk_lines, holding_avg_price, holding_shares,
                        portfolio_value, report_broken_transaction)
from mari_names import currency, josa, server_name

# [UI 뷰 클래스] 종가 게시 승인용 버튼 뷰
class ClosingPriceView(MariView):
    def __init__(self, stock_cog, admin_user):
        super().__init__(timeout=60.0)
        self.stock_cog = stock_cog
        self.admin_user = admin_user
        # 🔒 [버그 수정] 버튼 연타 방지. view=None으로 지우는 것만으로는 이미 눌린 두 번째
        # 클릭을 못 막아요. 종가게시가 두 번 실행되면 주가 확정/포트폴리오 스냅샷이
        # 이중 반영돼서 돈이 꼬이기 때문에, 반드시 플래그로 한 번만 통과시켜야 해요.
        self.processing = False

    @discord.ui.button(label="종가 게시 승인", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.admin_user.id:
            return await interaction.response.send_message("❌ 명령어를 실행한 관리자만 누를 수 있어요.", ephemeral=True)

        if self.processing:
            return await interaction.response.send_message("⏳ 이미 종가게시를 처리하는 중이에요! 잠시만요.", ephemeral=True)
        self.processing = True

        await interaction.response.edit_message(view=None)

        if not os.path.exists(self.stock_cog.STOCKS_FILE):
            return await interaction.followup.send("❌ 주식 데이터 파일이 존재하지 않아요.")

        # 🔒 동시성 제어: 장 마감 정산 중 유저의 매수/매도 개입을 원천 차단
        async with self.stock_cog.bot.economy_lock:
            try:
                # 🚨 손상 파일을 조용히 넘기지 않고, 원본을 백업한 뒤 중단시켜요.
                data = safe_json_load(self.stock_cog.STOCKS_FILE, {})
            except Exception as e:
                return await interaction.followup.send(f"❌ 데이터 로드 오류: {e}")

            stocks_data = data.get("stocks", {})
            user_shares = data.get("user_shares", {})

            summary_lines = []

            for stock_name, stock_info in stocks_data.items():
                old_price = stock_info.get("price", 0)
                # 🔄 [변경] 오늘 변동(예약)이 없는 종목은 막지 않고 현재가를 그대로 유지합니다.
                had_pending = stock_info.get("pending_price") is not None
                new_price = stock_info["pending_price"] if had_pending else old_price
                # 🐛 [버그 수정] 예전엔 변동이 없으면 사유가 "유지 (변동 없음)"으로 덮어써져서,
                # 그 주식이 예전에 왜 지금 가격이 됐는지 정보가 매일 사라졌어요. 이제는 변동이
                # 없는 날엔 사유를 그대로 유지해서, 마지막으로 왜 움직였는지 계속 보여요.
                if had_pending:
                    reason = stock_info.get("pending_reason")
                else:
                    reason = stock_info.get("reason", "정보 없음")

                # 변동률(%) 및 화살표 가공
                if old_price > 0:
                    change_rate = ((new_price - old_price) / old_price) * 100
                    if change_rate > 0:
                        rate_str = f"🔺 +{change_rate:.1f}%"
                    elif change_rate < 0:
                        rate_str = f"🔻 {change_rate:.1f}%"
                    else:
                        rate_str = "🔹 0.0% (유지)"
                else:
                    rate_str = "🔹 0.0%"

                # 💡 딕셔너리 예외 방어 후 총 발행량 실시간 합산
                shareholders = []
                total_shares_sum = 0
                for user_id_str, portfolio in user_shares.items():
                    shares_count = holding_shares(portfolio.get(stock_name, 0))

                    if shares_count > 0:
                        total_shares_sum += shares_count
                        shareholders.append((user_id_str, shares_count))

                # 주가 및 발행량 업데이트 
                stock_info["yesterday_price"] = old_price
                stock_info["last_price"] = old_price
                stock_info["price"] = new_price
                stock_info["reason"] = reason
                stock_info["total_shares"] = total_shares_sum
                # 📅 [신규] 실제로 가격이 바뀐 날짜만(하루 단위) 기록해요. 변동 없는 날엔
                # 이 날짜도 그대로 유지되니까, "이 주식이 마지막으로 언제 움직였는지" 알 수 있어요.
                if had_pending:
                    stock_info["last_changed_date"] = dt.datetime.now(KST).strftime("%Y-%m-%d")

                # 📈 [신규] 종가게시할 때마다 (날짜, 가격) 기록을 남겨서 나중에 그래프로 그릴 수 있게 해요.
                # 최근 60개(대략 두 달치)만 유지해서 파일이 무한정 안 커지게 관리해요.
                today_str = dt.datetime.now(KST).strftime("%Y-%m-%d")
                history = stock_info.setdefault("price_history", [])
                if not history or history[-1].get("date") != today_str:
                    history.append({"date": today_str, "price": new_price})
                else:
                    history[-1]["price"] = new_price  # 같은 날 여러 번 마감하면 마지막 값으로 덮어씀
                if len(history) > 60:
                    del history[:len(history) - 60]

                # 주주 명단 정렬 및 텍스트 렌더링
                shareholders.sort(key=lambda x: x[1], reverse=True)
                holders_str_list = [f"<@{uid}> ({qty:,}주)" for uid, qty in shareholders]
                holders_str = ", ".join(holders_str_list) if holders_str_list else "없음"

                last_changed = stock_info.get("last_changed_date")
                date_text = f" · `{last_changed}` 변동" if last_changed else ""
                line = f"📈 **{stock_name}**: `현재가` ➡️ **`{new_price:,}원`** ({rate_str})\n" \
                       f"└ 💡 사유: {reason}{date_text}\n" \
                       f"└ 👥 주주: {holders_str}"
                summary_lines.append(line)

                # 다음 날 감지를 위한 예약 대기 데이터 완벽 초기화
                stock_info["pending_price"] = None
                stock_info["pending_reason"] = None

            # 🗑️ [정리] 예전엔 여기서 data["last_closed_date"]에 마감 시각을 적었는데,
            # 이 값을 읽는 코드가 어디에도 없는 죽은 데이터였어요. 게다가 이 프로젝트에서
            # 유일하게 시간대를 안 붙인 datetime.now()라 서버 설정에 따라 어긋날 수도 있었고요.
            # "이 주식이 마지막으로 언제 움직였는지"는 종목별 last_changed_date가 이미 보여줍니다.
            # (기존 파일에 남아있는 last_closed_date 값은 그냥 무시돼요)

            # 📊 [신규] 유저별 포트폴리오 총 가치 스냅샷을 기록해요.
            # (예전엔 /주식 포폴의 추세 그래프에 썼고, 지금은 기록만 계속 쌓아둡니다)
            portfolio_snapshot = {}
            for user_id_str in user_shares:
                total_value = portfolio_value(data, user_id_str)
                if total_value > 0:
                    portfolio_snapshot[user_id_str] = total_value

            if portfolio_snapshot:
                history_data = safe_json_load(PORTFOLIO_HISTORY_FILE, {})
                today_str = dt.datetime.now(KST).strftime("%Y-%m-%d")
                for uid, value in portfolio_snapshot.items():
                    user_hist = history_data.setdefault(uid, [])
                    if not user_hist or user_hist[-1].get("date") != today_str:
                        user_hist.append({"date": today_str, "value": value})
                    else:
                        user_hist[-1]["value"] = value
                    if len(user_hist) > 60:
                        del user_hist[:len(user_hist) - 60]
                atomic_json_save(PORTFOLIO_HISTORY_FILE, history_data, indent=2)

            if not atomic_json_save(self.stock_cog.STOCKS_FILE, data, indent=4):
                return await interaction.followup.send("❌ 데이터 저장 오류가 발생했어요.")

        # 🖥️ 파일 락 해제 후 스마트 전광판 업데이트 (오류 발생하더라도 멈추지 않음)
        board_updated = True
        try:
            await self.stock_cog.update_stock_board_smart()
        except Exception as e:
            board_updated = False
            print(f"[스마트 전광판 업데이트 실패] : {e}")

        # 📨 글자 수 1900자 제한 방어 분할
        # (종목마다 세 줄짜리 블록이라, 블록 사이가 벌어지도록 빈 줄을 하나씩 끼워서 넘깁니다)
        chunks = chunk_lines([line + "\n" for line in summary_lines])

        status_msg = "스마트 전광판 동기화 완료." if board_updated else "완료. (단, 전광판 갱신 실패)"
        await interaction.followup.send(
            f"✅ **장 마감 및 종가 정산이 성공적으로 끝났어요!**\n"
            f"──────────────────────────────\n"
            f"📈 모든 상장 종목 주가/발행량 반영 및 {status_msg}\n"
            f"🧹 내일 방어를 위한 예약 주가(`None`) 초기화 완료."
        )

        # 🎯 [핵심 수정] JSON 설정 파일에서 동적으로 closing_log 채널 찾아오기
        try:
            settings = load_settings()
            closing_log_id = settings.get("channels", {}).get("closing_log")
        except Exception:
            closing_log_id = None

        # 봇 오브젝트에서 실제 디스코드 채널 객체 획득
        target_board_channel = None
        if closing_log_id:
            target_board_channel = self.stock_cog.bot.get_channel(closing_log_id)

        # 🚨 만약 설정된 채널이 없거나 못 찾을 경우, 터지지 않게 현재 채널을 대피소로 지정
        if not target_board_channel:
            target_board_channel = interaction.channel
            await interaction.followup.send("⚠️ **안내:** `종가게시판` 채널 설정이 누락되어 현재 채널에 임시 게시합니다.", ephemeral=True)

        # 📬 지정된 종가게시판 채널로 장마감 공개 장부 전송
        
        # 1️⃣ 채널 내에 봇이 이전에 작성한 장부 메시지가 있는지 탐색
        existing_messages = []
        try:
            # 최근 30개의 메시지를 확인하여 봇의 장부 메시지를 수집합니다.
            async for msg in target_board_channel.history(limit=30):
                if msg.author == self.stock_cog.bot.user and msg.embeds:
                    if msg.embeds[0].title == f"🔔 [{currency()}증권 공개 장부]":
                        existing_messages.append(msg)
        except Exception as e:
            print(f"⚠️ 채널 기록 조회 실패: {e}")

        # 🔔 [변경] 예전엔 기존 장부 메시지를 조용히 edit(수정)만 해서 알림이 안 가고
        # 사람들이 갱신된 걸 놓치는 경우가 많았어요. 이제는 기존 메시지를 전부 지우고
        # 새로 게시해서, 새 메시지 알림으로 장마감 결과가 확실히 눈에 띄게 했습니다.
        for old_msg in existing_messages:
            try:
                await old_msg.delete()
            except Exception as e:
                print(f"⚠️ 기존 종가게시판 메시지 삭제 실패: {e}")

        for chunk in chunks:
            embed = discord.Embed(
                title=f"🔔 [{currency()}증권 공개 장부]",
                description=chunk,
                color=discord.Color.green()
            )
            try:
                await target_board_channel.send(embed=embed)
            except Exception as e:
                print(f"⚠️ 종가게시판 전송 실패: {e}")


class MariStock(commands.Cog):
    """개선된 주식 시스템 (경제 코그 연동, 문자열 기반 종목, 실시간 포폴 생성)"""

    # 📂 [그룹화] 예전엔 매수/매도/포폴 등 12개가 전부 최상위 명령어였는데,
    # /주식 그룹 하나로 묶어서 최상위 슬롯을 절약하고 목록도 깔끔하게 정리했어요.
    stock_group = app_commands.Group(name="주식", description="주식 거래 및 관리 명령어 모음")

    # 🚧 상장 가능한 종목 수 상한.
    # /주식 목록과 실시간 전광판이 종목마다 임베드 필드를 하나씩 쓰는데, 디스코드는 임베드
    # 하나에 필드를 25개까지만 허용해요. 넘기면 목록과 전광판이 "일부만 빠지는" 게 아니라
    # 메시지 전송 자체가 400 에러로 실패해서 통째로 안 보입니다.
    # 나중에 조회할 때 터뜨리지 않고 상장하는 자리에서 막습니다.
    MAX_STOCKS = 25

    def __init__(self, bot):
        self.bot = bot
        self.market_open = True
        self.stocks = {}
        self.STOCKS_FILE = STOCKS_FILE

    # ---------- 📈 [신규] 그래프 생성 공용 헬퍼 ----------
    def _setup_korean_font(self):
        """서버에 설치된 한글 지원 폰트를 찾아서 적용해요. 윈도우는 보통 '맑은 고딕(Malgun Gothic)'이
        기본 내장돼 있어서 별도 설치 없이 자동으로 잡혀요. 혹시 하나도 못 찾으면 기본 폰트로 진행되고,
        이 경우 한글이 네모(□)로 깨져 보일 수 있어요."""
        candidates = ["Malgun Gothic", "NanumGothic", "AppleGothic", "Noto Sans CJK KR", "Noto Sans KR"]
        available = {f.name for f in fm.fontManager.ttflist}
        for name in candidates:
            if name in available:
                plt.rcParams["font.family"] = name
                return

    def _make_line_chart(self, dates: list, values: list, title: str, ylabel: str,
                         color: str = CHART_UP) -> io.BytesIO:
        """(날짜, 값) 리스트로 라인 그래프 PNG를 그려서 메모리 버퍼로 돌려줘요.

        📊 [가독성 개선] 예전 그래프의 문제와 해결:
          • 배경을 투명(transparent=True)으로 저장해서, 보는 사람의 디스코드 테마에 따라
            축·눈금·제목이 배경에 묻혀 안 보였어요 → 흰 배경으로 고정
          • 선 색이 흰 배경 기준 대비 1.56:1 밖에 안 나왔어요 → 검증된 색으로 교체
          • 격자가 데이터만큼 진해서 시선을 뺏었어요 → 연한 회색으로 뒤로 물림
          • 모든 점에 마커만 있고 값은 하나도 안 보였어요 → 마지막 값만 직접 표시
        """
        self._setup_korean_font()
        plt.rcParams["axes.unicode_minus"] = False

        # 한글 폰트에 이모지 글리프가 없어 두부(□)로 깨지므로 제목에서 걷어냅니다.
        title = _CHART_EMOJI_RE.sub("", title).strip()

        fig, ax = plt.subplots(figsize=(8, 4.2), dpi=140)
        fig.patch.set_facecolor(CHART_BG)
        ax.set_facecolor(CHART_BG)

        # 📈 데이터 선: 2px, 마커 테두리를 배경색으로 둘러 점들이 겹쳐도 구분되게
        ax.plot(dates, values, marker="o", markersize=6, linewidth=2, color=color,
                markerfacecolor=color, markeredgecolor=CHART_BG, markeredgewidth=1.5,
                zorder=3)

        # 🔲 격자·축은 데이터보다 뒤로 (가로선만 남기고 세로선은 제거)
        ax.set_axisbelow(True)
        ax.grid(True, axis="y", color=CHART_GRID, linewidth=1)
        ax.grid(False, axis="x")
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(CHART_GRID)
            ax.spines[side].set_linewidth(1)

        # 🔤 글자는 전부 잉크색으로 (선 색을 글자에 쓰면 색이 두 가지 의미를 갖게 돼요)
        ax.set_title(title, color=CHART_INK, fontsize=13, fontweight="bold", pad=14, loc="left")
        ax.set_ylabel(ylabel, color=CHART_INK_SOFT, fontsize=10)
        ax.tick_params(colors=CHART_INK_SOFT, labelsize=9, length=0)
        ax.yaxis.set_major_formatter(lambda v, _pos: f"{int(v):,}")

        # 🏷️ 마지막 값만 직접 표시 (모든 점에 숫자를 붙이면 오히려 안 읽혀요)
        try:
            ax.annotate(
                f"{int(values[-1]):,}",
                xy=(len(values) - 1, values[-1]),
                xytext=(6, 6), textcoords="offset points",
                color=CHART_INK, fontsize=10, fontweight="bold",
                zorder=4,
            )
        except Exception:
            pass

        # 📅 x축 라벨이 많으면 겹치므로 솎아냅니다
        if len(dates) > 12:
            step = max(1, len(dates) // 10)
            ax.set_xticks(range(0, len(dates), step))
            ax.set_xticklabels([dates[i] for i in range(0, len(dates), step)])
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right")

        # 📐 마지막 값 라벨이 오른쪽 끝에서 잘리지 않도록 자리를 미리 비워둡니다.
        # (margins만으론 부족해서 x축 범위를 직접 잡아요)
        ax.set_xlim(-0.5, (len(values) - 1) + 1.1)
        ax.margins(y=0.16)

        buf = io.BytesIO()
        fig.tight_layout()
        fig.savefig(buf, format="png", facecolor=CHART_BG)
        plt.close(fig)
        buf.seek(0)
        return buf

    # 🗓️ 공휴일 표는 연도별로 한 번만 만들어 재사용합니다.
    # 🐛 [성능] 예전엔 개폐장을 판정할 때마다 holidays.KR()로 표를 통째로 새로 만들었어요.
    # 이 판정은 매수·매도가 한 번 일어날 때마다 불리는데, 공휴일 목록은 1년에 한 번 바뀔까 말까
    # 하는 값이라 매번 다시 계산할 이유가 없습니다.
    _kr_holiday_cache: dict = {}

    @classmethod
    def _kr_holidays(cls, year: int):
        cached = cls._kr_holiday_cache.get(year)
        if cached is None:
            cached = holidays.KR(years=year)
            cls._kr_holiday_cache[year] = cached
        return cached

    def _market_closed_reason(self) -> Optional[str]:
        """장이 닫혀있으면 그 사유 안내문을, 열려있으면 None을 돌려줍니다.

        🐛 [버그 수정] 예전엔 개폐장 판정이 `_is_market_open`(주말·공휴일 기준)과
        `is_market_open`(09:00~23:59 기준) 두 함수로 쪼개져 있었고, 매수/매도가 둘 다
        호출해서 우연히 AND 조건처럼 동작했어요. 한쪽만 손대면 조용히 어긋나는 구조라
        판정 로직을 이 함수 하나로 합쳤습니다. (아래 두 함수는 호환용 껍데기예요)"""
        # 1. 관리자 강제 폐장이 무엇보다 우선
        if getattr(self, 'market_forced_close', False):
            return "❌ 지금은 관리자가 장을 강제로 닫아둔 상태예요."

        now_kst = dt.datetime.now(KST)  # 💡 pytz 대신 파일 상단의 전역 KST 상수 재사용

        # 2. 강제 개장 중(자정 전까지 유지)이면 휴일/시간 상관없이 열려있음
        forced_open_until = getattr(self, 'forced_open_until', None)
        if forced_open_until and now_kst <= forced_open_until:
            return None

        # 3. 주말 및 법정 공휴일 휴장
        if now_kst.weekday() >= 5 or now_kst.date() in self._kr_holidays(now_kst.year):
            return "❌ **지금은 주식 시장 휴장일이에요.**\n주말 및 법정 공휴일에는 주식을 거래할 수 없어요."

        # 4. 정규 개장 시간(09:00 ~ 23:59)
        if not (dt.time(9, 0) <= now_kst.time() <= dt.time(23, 59)):
            return "❌ 지금은 장이 닫혀서 주식을 거래할 수 없어요! (개장시간: 09시 ~ 자정)"

        return None

    def is_market_open(self) -> bool:
        """장이 열려있는지 여부. 판정은 전부 _market_closed_reason() 하나에 위임합니다."""
        return self._market_closed_reason() is None

    # 🔁 예전 이름으로 호출하던 코드가 남아있어도 같은 판정을 쓰도록 별칭으로 연결
    _is_market_open = is_market_open

    def _has_admin_permissive(self, interaction: discord.Interaction) -> bool:
        """서버 관리자 또는 주식 관리자(settings.json의 roles.stock_admin)인지 확인합니다.
        이 코그의 모든 관리 명령어가 쓰는 단 하나의 권한 기준이에요."""
        return has_admin_or_role(interaction, "stock_admin")

    # 🗑️ [정리] 예전엔 여기 has_permission()이 있었는데, 주식 코그인데도 ids_admin(아이디 관리자)을
    # 보고 있었어요. cogs/ids.py에서 복붙된 흔적이고 호출하는 곳이 하나도 없는 죽은 코드였습니다.
    # 누가 무심코 갖다 쓰면 주식 명령어가 아이디 관리자에게 열리는 사고가 나므로 지웠어요.

    def _load_stocks(self) -> dict:
        default_structure = {
            "stocks": {},          
            "board_message_id": None, 
            "user_shares": {}      
        }

        if not os.path.exists(self.STOCKS_FILE):
            if atomic_json_save(self.STOCKS_FILE, default_structure, indent=4):
                print(f"📁 [자동 생성] 표준 주식 데이터베이스 파일 생성 완료")
            return default_structure

        # ⚠️ 파일이 있는데 손상되어 있으면 safe_json_load가 예외를 던져서 여기서 멈춥니다.
        # (주식/보유 지분 데이터가 걸린 파일이라, 조용히 빈 값으로 넘어가면 훨씬 위험해요)
        data = safe_json_load(self.STOCKS_FILE, None)

        if not isinstance(data, dict):
            return default_structure

        if "stocks" not in data:
            old_stocks = {k: v for k, v in data.items() if k not in ["board_message_id", "user_shares"]}
            new_data = default_structure.copy()
            new_data["stocks"] = old_stocks
            new_data["board_message_id"] = data.get("board_message_id")
            new_data["user_shares"] = data.get("user_shares", {})
            return new_data

        for key in default_structure:
            if key not in data:
                data[key] = default_structure[key]

        return data

    def _save_stocks(self, data: dict):
        atomic_json_save_or_raise(self.STOCKS_FILE, data, indent=4)

    # 🎯 [로그 채널 통합 및 확장] stock_log 설정 파싱 연동 및 임베드 대응 가능화
    async def _log_to_channel(self, text: str = None, embed: discord.Embed = None):
        try:
            # 설정 파일에서 stock_log 채널 ID 동적 획득
            stock_log_id = load_settings().get("channels", {}).get("stock_log")
            channel = self.bot.get_channel(stock_log_id) if stock_log_id else None
                
            if channel:
                if embed:
                    # 💡 [통일] 이미 완성된 임베드가 넘어와도 색상/제목만은 stock_log 스타일로 맞춥니다.
                    style = LOG_STYLES["stock_log"]
                    embed.color = style["color"]
                    if not embed.title:
                        embed.title = f"{style['emoji']} {style['title']}"
                    await channel.send(embed=embed)
                elif text:
                    await channel.send(embed=build_log_embed("stock_log", text))
        except Exception as e:
            print(f"로그 전송 실패: {e}")

    async def stock_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            stock_data = self._load_stocks()
            stocks_dict = stock_data.get("stocks", {})
            return [
                app_commands.Choice(name=name, value=name)
                for name in stocks_dict.keys()
                if current.lower() in name.lower()
            ][:25]  
        except Exception as e:
            print(f"자동완성 오류 발생: {e}")
            return []

    # ================= 일반 유저 명령어 =================

    @stock_group.command(name="매수", description="현재가로 주식을 매수합니다.")
    @app_commands.describe(stock="매수할 주식 선택", amount="매수할 주 수")
    @app_commands.autocomplete(stock=stock_autocomplete)
    async def buy_stock(self, interaction: discord.Interaction, stock: str, amount: int):
        if await feature_gate(interaction, "stock", "주식"):
            return
        # ⏱️ 3초 응답 제한 방지: 락 대기/파일 IO 전에 선제적으로 defer
        await interaction.response.defer(ephemeral=True)

        # (채널 제한 해제: 아무 채널에서나 매수 가능)

        closed_reason = self._market_closed_reason()
        if closed_reason:
            await interaction.followup.send(closed_reason, ephemeral=True)
            return

        if amount <= 0:
            await interaction.followup.send("💥 1주 이상만 매수할 수 있어요.", ephemeral=True)
            return

        economy_cog = self.bot.get_cog('MariEconomy')
        if not economy_cog:
            await interaction.followup.send("❌ 경제 시스템을 불러올 수 없어요. 관리자에게 문의하세요.", ephemeral=True)
            return

        # 🚦 [성능 버그 수정] 예전엔 아래 economy_lock 안에서 곧바로 followup.send와 로그 전송까지
        # 했어요. economy_lock은 송금·출석·지급·회수·상점구매·에바시·캠프통장까지 전부 통과하는
        # 서버 공용 관문이라, 거래 한 건이 디스코드에 메세지를 보내는 동안(수백ms, 레이트리밋에
        # 걸리면 몇 초) 서버의 모든 돈 관련 기능이 줄을 서서 멈췄습니다.
        # 이제 락 안에서는 "읽고·계산하고·저장"까지만 하고, 안내와 로그는 락을 놓은 뒤에 보냅니다.
        error_message = None
        result = None       # 매수 성공했을 때만 채워져요: (종목명, 체결가, 총액, 남은잔고)
        broken = None       # 지갑은 빠졌는데 주식 저장이 실패한 경우의 상세 정보

        try:
            async with self.bot.economy_lock:
                stock_data = self._load_stocks()
                stocks_dict = stock_data.get("stocks", stock_data)

                stock_name = stock.strip()
                found_stock = None
                for name in stocks_dict.keys():
                    if name.lower().strip() == stock_name.lower().strip():
                        found_stock = name
                        break

                if not found_stock or "price" not in stocks_dict[found_stock]:
                    error_message = f"❌ 상장되지 않았거나 주가 정보가 없는 종목이에요: {stock_name}"
                else:
                    stock_name = found_stock
                    current_price = int(stocks_dict[stock_name]["price"])
                    total_cost = current_price * amount

                    if hasattr(economy_cog, '_load_raw_economy'):
                        economy_data = economy_cog._load_raw_economy()
                    else:
                        economy_data = {}

                    user_id = str(interaction.user.id)
                    user_bal = economy_data.get(user_id, 0)

                    if user_bal < total_cost:
                        error_message = f"❌ 잔액이 부족합니다!\n(필요 {currency()}: {total_cost:,} / 보유 {currency()}: {user_bal:,})"
                    else:
                        economy_data[user_id] = user_bal - total_cost
                        if hasattr(economy_cog, '_save_raw_economy'):
                            economy_cog._save_raw_economy(economy_data)

                        if "user_shares" not in stock_data:
                            stock_data["user_shares"] = {}
                        user_shares = stock_data["user_shares"].setdefault(user_id, {})

                        # 🐛 [일관성] 예전엔 여기서 `.get("shares", 0)`으로 직접 읽었어요. 보유 기록이
                        # 옛 형식(종목: 주식수 정수)으로 남아 있으면 int에 .get을 부르는 셈이라
                        # AttributeError로 매수가 통째로 터집니다. /주식 지급·회수는 옛 형식을
                        # 받아주는데 여기만 안 받아주는, 경로마다 다른 상태였어요.
                        # 읽는 방법을 공용 함수(mari_utils)로 통일했습니다.
                        holding = user_shares.get(stock_name)
                        current_held = holding_shares(holding)
                        current_avg = holding_avg_price(holding)

                        new_shares = current_held + amount
                        new_avg = int(((current_avg * current_held) + total_cost) / new_shares)
                        user_shares[stock_name] = {"shares": new_shares, "avg_price": new_avg}

                        # 💾 지갑은 이미 차감돼 저장됐어요. 여기가 실패하면 "돈만 빠지고 주식은
                        # 못 받은" 반쪽 상태가 됩니다. 예외를 그냥 흘려보내면 유저에게는 "내부 오류"로만
                        # 보여서 돈이 빠진 줄도 모르니, 여기서 붙잡아 정확히 알립니다.
                        try:
                            self._save_stocks(stock_data)
                        except Exception as save_err:
                            broken = {
                                "user_id": user_id,
                                "lost": f"{currency()} {total_cost:,}",
                                "not_received": f"{stock_name} {amount}주",
                                "error": save_err,
                                "balance": economy_data[user_id],
                                "delta": -total_cost,
                            }
                        else:
                            record_ledger(user_id, -total_cost, economy_data[user_id],
                                          "주식 매수", f"{stock_name} {amount}주 @{current_price:,}")
                            result = (stock_name, current_price, total_cost, economy_data[user_id])
        except Exception as e:
            print(f"매수 중 내부 예외 발생: {e}")
            err_text = f"❗ 매수 처리 도중 내부 오류가 터졌어요: {type(e).__name__} - {e}"
            if interaction.response.is_done():
                await interaction.followup.send(err_text, ephemeral=True)
            else:
                await interaction.response.send_message(err_text, ephemeral=True)
            return

        # 🔓 여기서부터는 락을 놓은 상태예요. 이제 마음 놓고 디스코드와 통신합니다.
        if broken:
            return await report_broken_transaction(
                interaction, action="주식 매수", user_id=broken["user_id"],
                lost=broken["lost"], not_received=broken["not_received"], error=broken["error"],
                ledger_delta=broken["delta"], ledger_balance=broken["balance"],
            )

        if error_message:
            return await interaction.followup.send(error_message, ephemeral=True)

        stock_name, current_price, total_cost, balance_after = result
        await interaction.followup.send(f"✅ {stock_name} 주식을 {amount}주 매수했어요!\n(체결가: {current_price:,} {currency()})", ephemeral=True)
        await self._log_to_channel(text=f"📥 {interaction.user.mention}님이 **{stock_name}**을 {amount}주 매수.\n"
                                         f"💵 체결가: {current_price:,} {currency()} | 총 차감: -{total_cost:,} {currency()} | 남은 잔고: {balance_after:,} {currency()}")

    @stock_group.command(name="매도", description="현재가로 주식을 매도합니다. (거래채널 전용)")
    @app_commands.describe(stock="매도할 주식 선택", amount="매도할 주 수")
    @app_commands.autocomplete(stock=stock_autocomplete)
    async def sell_stock(self, interaction: discord.Interaction, stock: str, amount: int):
        if await feature_gate(interaction, "stock", "주식"):
            return
        # ⏱️ 3초 응답 제한 방지: 락 대기/파일 IO 전에 선제적으로 defer
        await interaction.response.defer(ephemeral=True)

        # (채널 제한 해제: 아무 채널에서나 매도 가능)

        closed_reason = self._market_closed_reason()
        if closed_reason:
            await interaction.followup.send(closed_reason, ephemeral=True)
            return

        if amount <= 0:
            await interaction.followup.send("💥 1주 이상만 매도할 수 있어요.", ephemeral=True)
            return

        economy_cog = self.bot.get_cog('MariEconomy')
        if not economy_cog:
            await interaction.followup.send("❌ 경제 시스템을 불러올 수 없어요.", ephemeral=True)
            return

        # 🚦 [성능 버그 수정] 매수와 같은 이유로, 락 안에서는 데이터 처리만 하고
        # 안내·로그 전송은 락을 놓은 뒤로 미룹니다. (자세한 설명은 buy_stock 참고)
        error_message = None
        result = None       # 매도 성공했을 때만 채워져요: (종목명, 체결가, 총액, 현재잔고)
        broken = None       # 주식은 빠졌는데 지갑 입금이 실패한 경우의 상세 정보

        try:
            async with self.bot.economy_lock:
                stock_data = self._load_stocks()
                stocks_dict = stock_data.get("stocks", stock_data)

                stock_name = stock.strip()
                found_stock = None
                for name in stocks_dict.keys():
                    if name.lower().strip() == stock_name.lower().strip():
                        found_stock = name
                        break

                if not found_stock:
                    error_message = f"❌ 상장되지 않은 종목이에요: {stock_name}"
                else:
                    stock_name = found_stock
                    user_id = str(interaction.user.id)
                    user_shares = stock_data.get("user_shares", {}).get(user_id, {})

                    # 🐛 [일관성] 매수와 같은 이유로 공용 함수를 씁니다. 예전엔
                    # `user_shares[stock_name]["shares"]`로 바로 파고들어서, 옛 형식 기록이
                    # 하나라도 남아 있으면 'int' object is not subscriptable로 매도가 터졌어요.
                    holding = user_shares.get(stock_name)
                    current_count = holding_shares(holding)

                    if current_count < amount:
                        error_message = f"❌ 보유 주식이 부족합니다!\n(현재 보유: {current_count}주)"
                    else:
                        current_price = int(stocks_dict[stock_name]["price"])
                        total_earning = current_price * amount

                        remaining = current_count - amount
                        if remaining <= 0:
                            del user_shares[stock_name]
                        else:
                            # 옛 형식으로 남아 있던 기록은 이 참에 새 형식으로 맞춰서 저장해요.
                            # (평단 정보가 없으면 지금 체결가를 평단으로 봅니다)
                            user_shares[stock_name] = {
                                "shares": remaining,
                                "avg_price": holding_avg_price(holding) or current_price,
                            }

                        self._save_stocks(stock_data)

                        if hasattr(economy_cog, '_load_raw_economy'):
                            economy_data = economy_cog._load_raw_economy()
                        else:
                            economy_data = {}

                        economy_data[user_id] = economy_data.get(user_id, 0) + total_earning

                        # 💾 주식은 이미 회수돼 저장됐어요. 여기가 실패하면 "주식만 사라지고 돈은
                        # 못 받은" 반쪽 상태가 됩니다. (매수와 방향만 반대인 같은 문제예요)
                        try:
                            if hasattr(economy_cog, '_save_raw_economy'):
                                economy_cog._save_raw_economy(economy_data)
                        except Exception as save_err:
                            broken = {
                                "user_id": user_id,
                                "lost": f"{stock_name} {amount}주",
                                "not_received": f"{currency()} {total_earning:,}",
                                "error": save_err,
                            }
                        else:
                            record_ledger(user_id, total_earning, economy_data[user_id],
                                          "주식 매도", f"{stock_name} {amount}주 @{current_price:,}")
                            result = (stock_name, current_price, total_earning, economy_data[user_id])
        except Exception as e:
            print(f"매도 중 내부 예외 발생: {e}")
            err_text = f"❗ 매도 처리 도중 내부 오류가 터졌어요: {type(e).__name__} - {e}"
            if interaction.response.is_done():
                await interaction.followup.send(err_text, ephemeral=True)
            else:
                await interaction.response.send_message(err_text, ephemeral=True)
            return

        # 🔓 여기서부터는 락을 놓은 상태예요.
        if broken:
            # 주식은 이미 빠져나가 원장에 남길 에바 변동이 없어요. 그래서 ledger_delta는 0이고,
            # 관리자 알림과 유저 안내만 나갑니다. (복구는 /주식 지급으로)
            return await report_broken_transaction(
                interaction, action="주식 매도", user_id=broken["user_id"],
                lost=broken["lost"], not_received=broken["not_received"], error=broken["error"],
            )

        if error_message:
            return await interaction.followup.send(error_message, ephemeral=True)

        stock_name, current_price, total_earning, balance_after = result
        await interaction.followup.send(f"✅ {stock_name} 주식을 {amount}주 매도했어요!\n(체결가: {current_price:,} {currency()})", ephemeral=True)
        await self._log_to_channel(text=f"📤 {interaction.user.mention}님이 **{stock_name}**을 {amount}주 매도.\n"
                                         f"💵 체결가: {current_price:,} {currency()} | 총 입금: +{total_earning:,} {currency()} | 현재 잔고: {balance_after:,} {currency()}")

    @stock_group.command(name="포폴", description="본인 또는 타인의 주식 포트폴리오를 조회합니다.")
    @app_commands.describe(member="조회할 유저 (미입력 시 본인)")
    async def view_portfolio(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        # 🔒 [로그 제외 타깃 1] 포폴은 아무런 외부 로그를 발송하지 않음
        await interaction.response.defer()

        try:
            target = member or interaction.user
            is_self = target.id == interaction.user.id

            if not is_self and not self._has_admin_permissive(interaction):
                return await interaction.followup.send("❌ 타인의 포트폴리오는 관리자 및 상점주인만 조회할 수 있어요.", ephemeral=True)

            stock_data = self._load_stocks()
            user_id = str(target.id)

            if "user_shares" not in stock_data:
                stock_data["user_shares"] = {}
            if user_id not in stock_data["user_shares"]:
                stock_data["user_shares"][user_id] = {}
                self._save_stocks(stock_data)

            user_shares = stock_data["user_shares"][user_id]

            user_bal = 0
            economy_cog = self.bot.get_cog('MariEconomy')
            if economy_cog and hasattr(economy_cog, '_load_raw_economy'):
                user_bal = economy_cog._load_raw_economy().get(user_id, 0)
            else:
                # 🚨 [버그 수정] 손상된 지갑 파일을 잔고 0으로 조용히 처리하지 않아요.
                user_bal = safe_json_load(ECONOMY_FILE, {}).get(user_id, 0)

            embed = discord.Embed(
                title=f"📊 {target.display_name}님의 투자 포트폴리오",
                color=discord.Color.blue(),
                timestamp=dt.datetime.now(KST)
            )
            embed.set_thumbnail(url=target.display_avatar.url)
            embed.add_field(name="보유 지갑 잔고", value=f"`{user_bal:,}` {currency()}", inline=False)

            total_eval_asset = 0
            total_purchase_asset = 0
            table_lines = []

            if not user_shares:
                embed.description = "현재 보유 중인 주식이 없어요."
            else:
                for name, info in user_shares.items():
                    if name not in stock_data.get("stocks", {}):
                        continue 
                    
                    shares = holding_shares(info)
                    avg_price = holding_avg_price(info)
                    current_price = stock_data["stocks"][name].get("price", 0)

                    purchase_sum = avg_price * shares
                    eval_sum = current_price * shares 
                    
                    total_purchase_asset += purchase_sum
                    total_eval_asset += eval_sum
          
                    profit = eval_sum - purchase_sum
                    profit_rate = (profit / purchase_sum) * 100 if purchase_sum > 0 else 0
              
                    sign = "+" if profit > 0 else ""
                    color_indicator = "🔺" if profit > 0 else ("🔻" if profit < 0 else "🔹")
                    
                    table_lines.append(
                        f"**{name}** | `{shares}주` 보유\n"
                        f"평단: `{avg_price:,}` ➡️ 현재가: `{current_price:,}`\n"
                        f"평가금: `{eval_sum:,}` {currency()} ({color_indicator} {sign}{profit_rate:.2f}%)"
                    )
                
                if table_lines:
                    embed.description = "\n\n".join(table_lines)
                else:
                    embed.description = "현재 보유 중인 주식이 없어요."
                
                if total_purchase_asset > 0:
                    total_profit = total_eval_asset - total_purchase_asset
                    total_rate = (total_profit / total_purchase_asset) * 100
                    total_sign = "+" if total_profit > 0 else ""
                    
                    embed.add_field(
                        name="📈 총 주식 평가 요약",
                        value=f"총 매수금액: `{total_purchase_asset:,}` {currency()}\n"
                              f"총 평가금액: `{total_eval_asset:,}` {currency()}\n"
                              f"총 순수익: `{total_sign}{total_profit:,}` {currency()} ({total_sign}{total_rate:.2f}%)",
                        inline=False
                    )

            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            print(f"❌ [포폴 명령어 오류] {e}")
            await interaction.followup.send(f"❗ 포트폴리오를 불러오는 중 오류가 발생했어요: `{e}`\n관리자(콘솔)를 확인해주세요.", ephemeral=True)

        # 🗑️ [제거] 예전엔 여기서 자산 추세 그래프를 이미지로 만들어 함께 보냈어요.
        # 포폴은 텍스트 요약만으로 충분하다는 판단으로 뺐습니다.
        # (종목별 추이는 `/주식 그래프`로 계속 볼 수 있어요. 자산 스냅샷 기록인
        #  PORTFOLIO_HISTORY_FILE은 종가게시 때 계속 쌓이므로, 나중에 되살리고 싶으면 그대로 쓸 수 있습니다)

    # ================= 관리자 및 상점주인 전용 명령어 =================

    @stock_group.command(name="변동", description="[관리자] 특정 종목의 주가 변동 및 찌라시를 예약합니다.")
    @app_commands.describe(주식명="변동할 주식 이름", 변동값="예: 5000, +500, +10%, -5%, 또는 유지 입력", 찌라시="사유 (유지 입력 시 생략 가능)")
    async def 주식변동(self, interaction: discord.Interaction, 주식명: str, 변동값: str, 찌라시: Optional[str] = None):
        if not self._has_admin_permissive(interaction):
            return await interaction.response.send_message("❌ 관리자 또는 상점주인 권한이 필요합니다.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        data = self._load_stocks()
        stocks_dict = data.get("stocks", {})

        target_stock = None
        for name in stocks_dict.keys():
            if name.lower().strip() == 주식명.lower().strip():
                target_stock = name
                break

        if not target_stock:
            return await interaction.followup.send(f"❌ 존재하지 않는 종목이에요: {주식명}", ephemeral=True)

        stock_info = stocks_dict[target_stock]
        current_price = stock_info.get("price", 0)

        input_val = 변동값.strip()
        pending_price = current_price
        pending_reason = 찌라시

        if input_val == "유지":
            pending_price = current_price
            pending_reason = "주가 유지 (변동 없음)" if not 찌라시 else 찌라시
        else:
            try:
                if input_val.endswith("%"):
                    raw_percent = input_val[:-1].strip()
                    if raw_percent.startswith("+"):
                        percent_val = float(raw_percent[1:])
                    elif raw_percent.startswith("-"):
                        percent_val = -float(raw_percent[1:])
                    else:
                        percent_val = float(raw_percent)
                    pending_price = int(current_price * (1 + percent_val / 100))
                else:
                    if input_val.startswith("+"):
                        pending_price = current_price + int(input_val[1:])
                    elif input_val.startswith("-"):
                        pending_price = current_price - int(input_val[1:])
                    else:
                        pending_price = int(input_val)

                if pending_price < 0:
                    pending_price = 0
                if not pending_reason:
                    pending_reason = "시장의 흐름 반영"

            except ValueError:
                return await interaction.followup.send("❌ 올바른 형식의 금액, 퍼센트(%), 또는 '유지'를 입력해 주세요.", ephemeral=True)

        stock_info["pending_price"] = pending_price
        stock_info["pending_reason"] = pending_reason
        self._save_stocks(data)

        if hasattr(self, 'update_stock_board_smart'):
            await self.update_stock_board_smart()

        await interaction.followup.send(
            f"✅ **`{target_stock}` 변동 예약 완료**\n"
            f"• 현재가: `{current_price:,} {currency()}` ➡️ **예약 종가: `{pending_price:,} {currency()}`**\n"
            f"• 사유: `{pending_reason}`", 
            ephemeral=True
        )
        
        # 주식변동 로그 연결 추가
        await self._log_to_channel(text=f"⚙️ {interaction.user.mention} 관리자가 **{target_stock}**의 변동을 예약함.\n"
                                         f"• 변동값 수식: `{input_val}` ➡️ 반영 예정가: `{pending_price:,} {currency()}`\n"
                                         f"• 찌라시 사유: `{pending_reason}`")

    @주식변동.autocomplete('주식명')
    async def 주식명_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        # 🐛 [버그 수정] 예전엔 try/except 없이 파일을 직접 열어서, 주식 파일이 손상되면
        # 자동완성이 통째로 터졌어요(다른 자동완성들은 이미 방어 처리가 돼 있었는데 여기만 빠져있었어요).
        try:
            data = safe_json_load(self.STOCKS_FILE, {})
            stocks_data = data.get("stocks", {})
            return [
                app_commands.Choice(name=stock, value=stock)
                for stock in stocks_data.keys()
                if current.lower() in stock.lower()
            ][:25]
        except Exception as e:
            print(f"⚠️ 주식변동 자동완성 조회 실패: {e}")
            return []

    @stock_group.command(name="생성", description="[관리자] 신규 주식 종목을 상장합니다.")
    @app_commands.describe(종목="신규 종목으로 등록할 주식 이름 입력", price="상장 기준 가격")
    async def create_stock(self, interaction: discord.Interaction, 종목: str, price: int):
        stock_name = 종목.strip()
        if not self._has_admin_permissive(interaction):
            return await interaction.response.send_message("❌ 관리자 또는 상점주인 권한이 필요합니다.", ephemeral=True)

        # 🚨 [버그 수정] 상장가 검증이 빠져 있었어요. 음수 주가는 매도할 때 잔고가 줄고,
        # 평가액·세금 계산까지 전부 음수로 오염됩니다. (`/주식 변동`은 이미 0 아래로는
        # 안 내려가게 막고 있어서, 그 기준을 상장 쪽에도 맞췄어요)
        if price < 0:
            return await interaction.response.send_message(f"❌ 상장 가격은 0 {currency()} 이상이어야 해요.", ephemeral=True)

        stock_data = self._load_stocks()
        if "stocks" not in stock_data:
            stock_data["stocks"] = {}
            
        if stock_name in stock_data["stocks"]:
            return await interaction.response.send_message("❌ 이미 상장되어 있는 동일 종목이 있어요.", ephemeral=True)

        # 🚧 26번째부터는 /주식 목록과 전광판이 통째로 안 그려져요. (위 MAX_STOCKS 주석 참고)
        current_count = len(stock_data["stocks"])
        if current_count >= self.MAX_STOCKS:
            return await interaction.response.send_message(
                f"❌ 종목은 **{self.MAX_STOCKS}개까지만** 상장할 수 있어요. (지금 {current_count}개)\n"
                f"└ 디스코드가 임베드 필드를 {self.MAX_STOCKS}개로 제한해서, 넘기면 `/주식 목록`과 "
                f"실시간 전광판이 아예 안 보이게 돼요.\n"
                f"└ `/주식 삭제`로 안 쓰는 종목을 먼저 상장 폐지해 주세요.",
                ephemeral=True,
            )

        stock_data["stocks"][stock_name] = {
            "price": price,
            "yesterday_price": price,
            "last_price": price,
            "reason": "신규 상장된 종목이에요."
        }
        self._save_stocks(stock_data)
        await self.update_stock_board_smart()
        await interaction.response.send_message(f"📊 신규 주식 **{stock_name}**({price:,} {currency()}){josa(currency(), '이가')} 성공적으로 상장됐어요.", ephemeral=True)
        
        # 신규 상장 로그 연동
        await self._log_to_channel(text=f"✨ **{interaction.user.mention}** 관리자가 새로운 주식 **[{stock_name}]**을 상장함. (상장가: `{price:,}` {currency()})")

    @stock_group.command(name="삭제", description="[관리자] 특정 주식 종목을 상장 폐지합니다.")
    @app_commands.describe(종목="삭제할 주식 선택")
    @app_commands.autocomplete(종목=stock_autocomplete)
    async def delete_stock(self, interaction: discord.Interaction, 종목: str):
        stock_name = 종목.strip()
        if not self._has_admin_permissive(interaction):
            return await interaction.response.send_message("❌ 관리자 또는 상점주인 권한이 필요합니다.", ephemeral=True)

        stock_data = self._load_stocks()
        if stock_name not in stock_data.get("stocks", {}):
            return await interaction.response.send_message("❌ 존재하지 않는 종목이에요.", ephemeral=True)

        del stock_data["stocks"][stock_name]
        self._save_stocks(stock_data)
        
        await interaction.response.send_message(f"🔥 **{stock_name}** 종목이 전면 상장 폐지(삭제)됐어요.", ephemeral=True)
        
        # 상장 폐지 로그 연동
        await self._log_to_channel(text=f"💥 **{interaction.user.mention}** 관리자가 **[{stock_name}]** 종목을 전면 상장 폐지(삭제) 처리함.")
            
        await self.update_stock_board_smart()

    @stock_group.command(name="종가게시", description="[관리자] 오늘 주가 변동을 마감하고 전광판에 최종 반영합니다.")
    async def 종가게시(self, interaction: discord.Interaction):
        if not self._has_admin_permissive(interaction):
            return await interaction.response.send_message("❌ 관리자 또는 주식 관리자 권한이 필요합니다.", ephemeral=True)

        data = self._load_stocks()
        stocks_data = data.get("stocks", {})

        # 🔄 [변경] 이제 미변동 종목은 막지 않고 "유지"로 자동 처리됩니다.
        # (실제 유지 처리는 ClosingPriceView의 승인 버튼에서 일어나요)
        held_stocks = [
            name for name, info in stocks_data.items()
            if info.get("pending_price") is None
        ]

        view = ClosingPriceView(self, interaction.user)
        embed = discord.Embed(
            title="🔔 장마감 종가 최종 승인 대기",
            description="아래 [종가 게시 승인] 버튼을 누르면 전체 유저 잔고 합산 및 전광판 동기화가 완료됩니다.",
            color=0xffcc00
        )
        if held_stocks:
            embed.add_field(
                name="🔹 변동 없이 '유지'로 자동 처리될 종목",
                value=", ".join(f"`{s}`" for s in held_stocks),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, view=view)
        
        # 종가게시 요청 트리거 로그 기록
        await self._log_to_channel(text=f"🔔 {interaction.user.mention} 관리자가 **종가 게시 정산 승인 절차**를 시작했어요. (승인 패널 대기 중)")

    @stock_group.command(name="목록", description="현재 상장된 모든 주식 종목과 정보를 보여줍니다.")
    async def list_stocks(self, interaction: discord.Interaction):
        # 🔒 [로그 제외 타깃 2] 목록 확인 명령어는 아무런 외부 로그를 기록하지 않음
        stock_data = self._load_stocks()
        stocks_dict = stock_data.get("stocks", {})
        
        if not stocks_dict:
            return await interaction.response.send_message("현재 상장된 주식 종목이 없어요.", ephemeral=True)
            
        embed = discord.Embed(title="📊 현재 상장된 주식 목록", color=discord.Color.green())
        # 🛡️ [안전망] 상장은 MAX_STOCKS에서 막지만, 그 제한이 생기기 전에 이미 25개를
        # 넘겨둔 데이터가 있으면 여기서 잘라주지 않는 한 목록 전체가 안 보이게 돼요.
        for name, info in list(stocks_dict.items())[:self.MAX_STOCKS]:
            price = info.get("price", 0)
            reason = info.get("reason", "정보 없음")
            last_changed = info.get("last_changed_date")
            date_text = f" (`{last_changed}` 변동)" if last_changed else ""
            embed.add_field(name=f"🔹 {name}", value=f"현재가: `{price:,}` {currency()}\n💡 사유: {reason}{date_text}", inline=False)

        hidden = len(stocks_dict) - self.MAX_STOCKS
        if hidden > 0:
            embed.set_footer(text=f"⚠️ 종목이 {len(stocks_dict)}개라 {self.MAX_STOCKS}개만 보여요 (숨은 종목 {hidden}개)")

        await interaction.response.send_message(embed=embed)

    @stock_group.command(name="그래프", description="특정 종목의 최근 가격 변동 추이를 그래프로 보여줍니다.")
    @app_commands.describe(주식명="그래프를 볼 종목 이름")
    async def stock_graph(self, interaction: discord.Interaction, 주식명: str):
        stock_data = self._load_stocks()
        stocks_dict = stock_data.get("stocks", {})
        info = stocks_dict.get(주식명)
        if not info:
            return await interaction.response.send_message(f"❌ `{주식명}` 종목을 찾을 수 없어요.", ephemeral=True)

        history = info.get("price_history", [])
        if len(history) < 2:
            return await interaction.response.send_message("ℹ️ 아직 그래프를 그릴 만큼 가격 기록이 쌓이지 않았어요. (종가게시가 2번 이상 있어야 해요)", ephemeral=True)

        await interaction.response.defer()
        dates = [h["date"][5:] for h in history]  # MM-DD만 표시
        prices = [h["price"] for h in history]
        trend_color = CHART_UP if prices[-1] >= prices[0] else CHART_DOWN
        # 🖼️ 렌더링은 스레드로 분리 (이벤트 루프 블로킹 방지)
        buf = await asyncio.to_thread(
            self._make_line_chart, dates, prices, f"📈 {주식명} 가격 추이", f"가격 ({currency()})", trend_color
        )
        await interaction.followup.send(file=discord.File(buf, filename="stock_graph.png"))

    @stock_graph.autocomplete('주식명')
    async def stock_graph_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            stocks_dict = self._load_stocks().get("stocks", {})
            return [app_commands.Choice(name=s, value=s) for s in stocks_dict if current.lower() in s.lower()][:25]
        except Exception:
            return []

    async def update_stock_board_smart(self):
        try:
            board_ch_id = load_settings().get("channels", {}).get("stock_board")
            if not board_ch_id: return
            channel = self.bot.get_channel(board_ch_id)
            if not channel: return
                
            data = self._load_stocks()
            stocks_dict = data.get("stocks", {})
            
            embed = discord.Embed(
                title=f"📈 {server_name()} 실시간 주식 전광판", 
                color=discord.Color.gold(),
                timestamp=dt.datetime.now(KST)
            )
            
            if not stocks_dict:
                embed.description = "현재 거래 가능한 주식 종목이 없어요."
            else:
                # 🛡️ [안전망] /주식 목록과 같은 이유로 잘라냅니다. 필드가 25개를 넘으면
                # 전광판 메시지 전송 자체가 400 에러로 실패해서 전광판이 통째로 사라져요.
                for name, info in list(stocks_dict.items())[:self.MAX_STOCKS]:
                    price = info.get("price", 0)
                    last_price = info.get("last_price", price)
                    reason = info.get("reason", "시장 유동성에 따른 주가 변동")
                    last_changed = info.get("last_changed_date")
                    date_text = f" (`{last_changed}`)" if last_changed else ""
                    
                    diff = price - last_price
                    diff_percent = (diff / last_price * 100) if last_price > 0 else 0
                    
                    if diff > 0:
                        status = f"🔺 `{diff:,}` {currency()} (+{diff_percent:.2f}%)"
                    elif diff < 0:
                        status = f"🔻 `{diff:,}` {currency()} ({diff_percent:.2f}%)"
                    else:
                        status = "➖ `변동 없음`"

                    embed.add_field(
                        name=f"**🔹 {name}**",
                        value=f"> 현재가: **`{price:,}` {currency()}**\n"
                              f"> 직전 대비: {status}\n"
                              f"> 💡 사유: {reason}{date_text}",
                        inline=False
                    )
                    
            embed.set_footer(text="⚠️ 주식 투자는 신중하게 결정하세요.")

            # 🔔 [변경] 예전엔 전광판이 채널의 마지막 메시지일 때 조용히 edit(수정)만 해서
            # 사람들이 갱신된 걸 못 알아채는 경우가 많았어요. 이제는 항상 기존 메시지를
            # 삭제하고 새로 게시해서, 새 메시지 알림으로 갱신 사실이 확실히 보이게 했습니다.
            board_message_id = data.get("board_message_id")
            if board_message_id:
                try:
                    old_msg = await channel.fetch_message(board_message_id)
                    await old_msg.delete()
                except Exception:
                    pass  # 이미 삭제됐거나 못 찾으면 그냥 새로 게시

            new_msg = await channel.send(embed=embed)

            # 🚨 [심각한 버그 수정] 예전엔 위에서 읽어둔 `data`를 그대로 저장했어요.
            # 그런데 그 사이에 fetch_message/delete/send 세 번의 네트워크 통신이 끼어 있어서,
            # 그 몇 초 동안 누가 매수/매도를 하면 그 사람의 보유 주식(user_shares)이
            # 통째로 옛날 상태로 덮어써졌어요. 돈은 이미 빠져나간 뒤라 주식만 증발합니다.
            # (주식 파일 하나에 전광판 메세지 ID와 전원의 보유 주식이 같이 들어있어서 생긴 문제예요)
            # 이제 저장 직전에 파일을 새로 읽어서, 메세지 ID 한 줄만 갱신합니다.
            fresh = self._load_stocks()
            fresh["board_message_id"] = new_msg.id
            self._save_stocks(fresh)
        except Exception as e:
            print(f"전광판 업데이트 실패: {e}")      
            
    # ================== 📈 관리자 전용 주식 지급/회수 시스템 ==================

    @stock_group.command(name="지급", description="[관리자] 특정 멤버에게 주식을 지급합니다.")
    @app_commands.describe(멤버="주식을 지급할 대상 유저", 주식명="지급할 주식 종목 이름", 갯수="지급할 주식 수량 (기본값: 1)")
    async def stock_give(self, interaction: discord.Interaction, 멤버: discord.Member, 주식명: str, 갯수: Optional[int] = 1):
        # 🐛 [버그 수정] 여기만 shop_admin(상점 관리자)을 보고 있었어요. 짝인 /주식 회수는 물론
        # /주식 변동·생성·삭제까지 전부 stock_admin(주식 관리자) 기준이라, 주식 관리자는 회수만
        # 되고 지급은 안 되는 반쪽 권한이었습니다. 같은 그룹의 대칭 명령어끼리 기준을 맞춰요.
        if not self._has_admin_permissive(interaction):
            return await interaction.response.send_message("❌ 관리자 또는 주식 관리자 권한이 필요합니다.", ephemeral=True)

        if 갯수 <= 0:
            return await interaction.response.send_message("❌ 지급할 갯수는 1개 이상이어야 합니다.", ephemeral=True)
            
        await interaction.response.defer()
        
        async with self.bot.economy_lock:
            data = self._load_stocks()
            stocks_dict = data.get("stocks", {})
            
            target_stock = None
            for name in stocks_dict.keys():
                if name.lower().strip() == 주식명.lower().strip():
                    target_stock = name
                    break
                    
            if not target_stock:
                return await interaction.followup.send(f"❌ 상장되지 않았거나 존재하지 않는 주식 종목이에요: `{주식명}`")
                
            stock_name = target_stock
            current_price = stocks_dict[stock_name].get("price", 0)
            
            user_shares = data.setdefault("user_shares", {})
            user_id = str(멤버.id)
            user_portfolio = user_shares.setdefault(user_id, {})
            
            raw_holding = user_portfolio.get(stock_name, {"shares": 0, "avg_price": current_price})
            
            if not isinstance(raw_holding, dict):
                stock_holding = {"shares": int(raw_holding), "avg_price": current_price}
            else:
                stock_holding = raw_holding

            current_held = stock_holding.get("shares", 0)
            current_avg = stock_holding.get("avg_price", 0)
            if current_avg == 0:
                current_avg = current_price
                
            new_shares = current_held + 갯수
            new_avg = int(((current_avg * current_held) + (current_price * 갯수)) / new_shares) if new_shares > 0 else current_price
            
            user_portfolio[stock_name] = {"shares": new_shares, "avg_price": new_avg}
            self._save_stocks(data)
                
        # 🧾 [주식 로그 연동 교체] shop_log 대신 주식 전용 로그로 전송하도록 교정 완료
        embed = discord.Embed(
            title="📈 [주식 지급 알림]",
            color=0x00ff00,
            timestamp=dt.datetime.now(KST)
        )
        embed.add_field(name="대상 유저", value=멤버.mention, inline=True)
        embed.add_field(name="종목명", value=f"`{stock_name}`", inline=True)
        embed.add_field(name="지급 수량", value=f"`{갯수:,} 주`", inline=True)
        embed.add_field(name="지급 후 수량", value=f"`{new_shares:,} 주` (평단: `{new_avg:,} {currency()}`)", inline=False)
        embed.add_field(name="담당 관리자", value=interaction.user.mention, inline=False)
        
        await self._log_to_channel(embed=embed)
            
        await interaction.followup.send(f"✅ {멤버.mention}님에게 `{stock_name}` 주식 `{갯수:,}주`를 성공적으로 지급했어요.")

    @stock_group.command(name="회수", description="[관리자] 특정 멤버의 주식을 회수합니다.")
    @app_commands.describe(멤버="주식을 회수할 대상 유저", 주식명="회수할 주식 종목 이름", 갯수="회수할 주식 수량 (기본값: 1)")
    async def stock_take(self, interaction: discord.Interaction, 멤버: discord.Member, 주식명: str, 갯수: Optional[int] = 1):
        if not self._has_admin_permissive(interaction):
            return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)
            
        if 갯수 <= 0:
            return await interaction.response.send_message("❌ 회수할 갯수는 1개 이상이어야 합니다.", ephemeral=True)
            
        await interaction.response.defer()
        
        async with self.bot.economy_lock:
            data = self._load_stocks()
            stocks_dict = data.get("stocks", {})
            
            target_stock = None
            for name in stocks_dict.keys():
                if name.lower().strip() == 주식명.lower().strip():
                    target_stock = name
                    break
                    
            if not target_stock:
                return await interaction.followup.send(f"❌ 상장되지 않았거나 존재하지 않는 주식 종목이에요: `{주식명}`")
                
            stock_name = target_stock
            user_shares = data.get("user_shares", {})
            user_id = str(멤버.id)
            user_portfolio = user_shares.get(user_id, {})
            
            if stock_name not in user_portfolio:
                return await interaction.followup.send(f"❌ {멤버.mention}님은 해당 `{stock_name}` 주식을 보유하고 있지 않아요.")
                
            raw_holding = user_portfolio[stock_name]
            
            if not isinstance(raw_holding, dict):
                stock_holding = {"shares": int(raw_holding), "avg_price": stocks_dict[stock_name].get("price", 0)}
            else:
                stock_holding = raw_holding
                
            current_shares = stock_holding.get("shares", 0)
            
            if current_shares < 갯수:
                return await interaction.followup.send(f"❌ {멤버.mention}님은 해당 주식을 `{current_shares:,}주`만 보유하고 있어, 요청하신 `{갯수:,}주`를 회수할 수 없어요.")
                
            new_shares = current_shares - 갯수
            
            if new_shares <= 0:
                del user_portfolio[stock_name]
            else:
                stock_holding["shares"] = new_shares
                user_portfolio[stock_name] = stock_holding
                
            self._save_stocks(data)
                
        # 🧾 [주식 로그 연동 교체] 구형 변수 우회 및 주식 전용 로그 연동 완료
        embed = discord.Embed(
            title="📉 [주식 회수 알림]",
            color=0xff0000,
            timestamp=dt.datetime.now(KST)
        )
        embed.add_field(name="대상 유저", value=멤버.mention, inline=True)
        embed.add_field(name="종목명", value=f"`{stock_name}`", inline=True)
        embed.add_field(name="회수 수량", value=f"`{갯수:,} 주`", inline=True)
        embed.add_field(name="회수 후 수량", value=f"`{new_shares:,} 주`", inline=True)
        embed.add_field(name="담당 관리자", value=interaction.user.mention, inline=False)
        
        await self._log_to_channel(embed=embed)
            
        await interaction.followup.send(f"✅ {멤버.mention}님의 `{stock_name}` 주식 `{갯수:,}주`를 성공적으로 회수했어요.")

    @stock_give.autocomplete('주식명')
    async def stock_give_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            data = safe_json_load(self.STOCKS_FILE, {})
            stocks_data = data.get("stocks", {})
            return [
                app_commands.Choice(name=stock, value=stock)
                for stock in stocks_data.keys()
                if current.lower() in stock.lower()
            ][:25]
        except (RuntimeError, AttributeError) as e:
            # RuntimeError: safe_json_load가 파일 손상을 감지한 경우 / AttributeError: 데이터 구조가 dict가 아닌 경우
            print(f"⚠️ 주식지급 자동완성 조회 실패: {e}")
            return []

    @stock_take.autocomplete('주식명')
    async def stock_take_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            data = safe_json_load(self.STOCKS_FILE, {})
            stocks_data = data.get("stocks", {})
            return [
                app_commands.Choice(name=stock, value=stock)
                for stock in stocks_data.keys()
                if current.lower() in stock.lower()
            ][:25]
        except (RuntimeError, AttributeError) as e:
            print(f"⚠️ 주식회수 자동완성 조회 실패: {e}")
            return []
        
    @stock_group.command(name="폐장", description="[관리자] 주식 시장을 강제로 폐장합니다. (개장 명령어 전까지 유지)")
    async def force_close_market(self, interaction: discord.Interaction):
        if not self._has_admin_permissive(interaction):
            return await interaction.response.send_message("❌ 관리자 또는 상점주인 권한이 필요합니다.", ephemeral=True)

        self.market_forced_close = True
        self.forced_open_until = None  # 기존의 강제 개장 상태 무효화
        
        embed = discord.Embed(
            title="🛑 [주식 시장 강제 폐장]",
            description="관리자에 의해 주식 거래가 전면 **중단(폐장)** 됐어요.\n별도의 개장 지시가 있을 때까지 매수 및 매도가 불가능합니다.",
            color=0xff0000,
            timestamp=dt.datetime.now(KST)
        )
        await interaction.response.send_message(embed=embed)
        await self._log_to_channel(text=f"🛑 {interaction.user.mention} 관리자가 주식 시장을 **강제 폐장**했어요.")

    @stock_group.command(name="개장", description="[관리자] 주식 시장을 강제로 개장합니다. (오늘 장 마감 시간에 자연 종료)")
    async def force_open_market(self, interaction: discord.Interaction):
        if not self._has_admin_permissive(interaction):
            return await interaction.response.send_message("❌ 관리자 또는 상점주인 권한이 필요합니다.", ephemeral=True)

        self.market_forced_close = False
        
        # 오늘 자정(23:59:59)으로 강제 개장 유통기한 설정 (자정이 지나면 자연 폐장됨)
        tz = KST  # 💡 [정리] pytz 대신 파일 상단에 이미 정의된 전역 KST 상수 재사용
        now_kst = dt.datetime.now(tz)
        close_time = now_kst.replace(hour=23, minute=59, second=59, microsecond=0)
        self.forced_open_until = close_time
        
        embed = discord.Embed(
            title="🟢 [주식 시장 강제 개장]",
            description=f"관리자에 의해 주식 거래가 **임시 개장** 됐어요.\n이 개장 상태는 오늘 장 마감(자정) 시까지 유지되며 이후 자연스럽게 종료됩니다.",
            color=0x00ff00,
            timestamp=dt.datetime.now(KST)
        )
        await interaction.response.send_message(embed=embed)
        await self._log_to_channel(text=f"🟢 {interaction.user.mention} 관리자가 주식 시장을 **강제 개장**했어요. (유지: ~오늘 23:59:59)")
        
