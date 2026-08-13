"""경로·비밀값(.env)·시간대·역할 ID·그래프 색상 등 순수 설정값 모음.
다른 마리 모듈을 일절 import하지 않는 최하위 계층이에요."""

import os
import re
import shutil
import datetime as dt
import discord

# ========== ⚙️ 기본 설정 ==========
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.members = True

KST = dt.timezone(dt.timedelta(hours=9))

# 🌎 [신규] Gemini API의 일일 한도(RPD)는 태평양 시간(PT) 자정 기준으로 초기화되므로,
# 한국 시간이 아니라 이 기준으로 "오늘 날짜"를 계산해야 실제 구글 쪽 한도와 어긋나지 않습니다.
try:
    from zoneinfo import ZoneInfo
    _PACIFIC_TZ = ZoneInfo("America/Los_Angeles")  # DST(서머타임) 자동 반영
except Exception:
    _PACIFIC_TZ = dt.timezone(dt.timedelta(hours=-8))  # zoneinfo/tzdata가 없을 때의 안전한 폴백(PST 고정)

def get_pacific_date_str() -> str:
    """구글 Gemini API의 RPD 리셋 기준(태평양 시간)에 맞춘 오늘 날짜 문자열."""
    return dt.datetime.now(_PACIFIC_TZ).date().isoformat()

# ========== ⚠️ 파일 경로 및 상수 ==========
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ========== 🔐 [신규] 비밀값 분리 (.env) ==========
# 봇 토큰과 API 키는 예전엔 코드 안에 그대로 적혀 있었어요. 그러면 깃에 올리는 순간
# 누구든 봇을 통째로 조종할 수 있게 됩니다. 이제 코드가 아니라 .env 파일에서 읽어와요.
# python-dotenv 같은 외부 패키지 없이 표준 라이브러리만으로 처리하므로 추가 설치가 필요 없습니다.
#
# ⚠️ 이 블록은 반드시 DATA_DIR보다 먼저 실행돼야 해요.
# .env의 MARI_DATA_DIR 값을 읽어야 데이터 폴더 위치를 정할 수 있으니까요.
ENV_FILE = os.path.join(BASE_DIR, ".env")


def load_env_file(path: str = ENV_FILE) -> None:
    """.env 파일을 읽어 os.environ에 채워 넣어요.

    - `KEY=VALUE` 형식이며, 빈 줄과 `#`으로 시작하는 줄은 무시해요.
    - 값을 감싼 따옴표(" 또는 ')는 벗겨냅니다.
    - ⚠️ 값 뒤에 주석을 달지 마세요. 토큰에 `#`이 들어갈 수 있어서 줄 전체를 값으로 읽어요.
    - 이미 진짜 환경변수로 지정돼 있으면 덮어쓰지 않아요.
      (서버에서 systemd나 도커로 주입한 값이 .env보다 우선이어야 하니까요)
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):  # 리눅스에서 복붙한 형식도 받아줘요
                    line = line[len("export "):].lstrip()
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as e:
        print(f"⚠️ .env 파일을 읽는 중 문제가 생겼어요: {type(e).__name__}: {e}")


load_env_file()


# 📁 [신규] 데이터 폴더 분리
# 예전엔 데이터 JSON이 main.py 바로 옆에 있어서, 깃으로 코드를 주고받을 때
# 실시간으로 바뀌는 지갑·주식·아이디 데이터까지 같이 덮어써질 위험이 있었어요.
# (개발 PC의 옛날 데이터가 서버로 넘어가면 유저 잔고가 통째로 롤백됩니다)
# 이제 데이터는 전부 data/ 폴더 안에 있고, 그 폴더는 .gitignore로 깃이 아예 무시해요.
#
# .env에서 MARI_DATA_DIR로 아무 경로나 지정할 수 있어요. 서버 한 대에서
# 여러 봇을 돌릴 때 코드는 한 벌만 두고 데이터 폴더만 나누면 됩니다.
DATA_DIR = os.environ.get("MARI_DATA_DIR", "").strip() or os.path.join(BASE_DIR, "data")

IDS_FILE = os.path.join(DATA_DIR, "ids.json")
WIKI_FILE = os.path.join(DATA_DIR, "mari_wiki.json")
ATTENDANCE_FILE = os.path.join(DATA_DIR, "mari_attendance.json")
STOCKS_FILE = os.path.join(DATA_DIR, "mari_stocks.json")
ECONOMY_FILE = os.path.join(DATA_DIR, "mari_economy.json")
SHOP_FILE = os.path.join(DATA_DIR, "mari_shop.json")
SHOP_TRANSACTIONS_FILE = os.path.join(DATA_DIR, "mari_shop_transactions.json")  # 💰 [신규] /정산용 구매·되팔기 거래 내역
VISIT_PASS_ASK_FILE = os.path.join(DATA_DIR, "mari_visit_pass_ask.json")  # 🎫 [신규] 견학권 구매자에게 보낸 "어느 캠프?" DM 대기열
CHRONICLE_FILE = os.path.join(DATA_DIR, "mari_chronicle.json")  # 📜 [신규] 서버 연대기 (서버에서 일어난 일을 날짜순으로 축적)
SNOOZE_FILE = os.path.join(DATA_DIR, "mari_snooze.json")  # ⏰ [신규] '나중에 답장' 예약 목록 (우클릭으로 미뤄둔 메시지)
LIMIT_FILE = os.path.join(DATA_DIR, "mari_limit.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "mari_settings.json") # 💡 신규 설정 파일
BIRTHDAY_FILE = os.path.join(DATA_DIR, "mari_birthday.json")
PROFILE_FILE = os.path.join(DATA_DIR, "mari_profile.json")  # 👤 [신규] 유저 프로필(레벨/소속/직책/업적/사진) 저장
CHAT_MEMORY_FILE = os.path.join(DATA_DIR, "mari_chat_memory.json") # 💬 유저별 마지막 대화 기억 파일
CHAT_LOG_FILE = os.path.join(DATA_DIR, "mari_chat_log.json") # 📜 서버별 최근 채팅 로그(맥락 참고용, 개수는 MariGPT.CHAT_LOG_MAXLEN 참고)
MARI_USER_MEMORY_FILE = os.path.join(DATA_DIR, "mari_user_memory.json") # 🧠 [신규] 유저별 장기 기억(사실 목록)
ID_PENDING_FILE = os.path.join(DATA_DIR, "mari_id_pending.json") # 🆔 [신규] 아이디 자동등록 중 플랫폼이 불명확해 관리자 확인이 필요한 대기열
CAMP_TREASURY_FILE = os.path.join(DATA_DIR, "mari_camp_treasury.json") # 🏦 캠프별 세금 통장(가상 통장)
CAMP_VISIT_FILE = os.path.join(DATA_DIR, "mari_camp_visits.json") # 🚌 [신규] 견학생 이동 현황 및 기록
CAMP_TAX_FILE = os.path.join(DATA_DIR, "mari_camp_tax.json") # 🧾 [신규] 캠프별 세금 기준(세율 구간·룰렛 확률 등). 캠프장이 직접 수정
PORTFOLIO_HISTORY_FILE = os.path.join(DATA_DIR, "mari_portfolio_history.json") # 📈 [신규] 종가게시 시점 유저별 포트폴리오 총 가치 스냅샷
EXILE_DATA_FILE = os.path.join(DATA_DIR, "mari_exile_data.json") # ⛓️ [신규] 유배 전 원래 역할 목록 백업 (복귀 시 되돌리는 용도)
LEDGER_FILE = os.path.join(DATA_DIR, "mari_ledger.json") # 🧾 [신규] 에바 입출금 원장 (유저 거래내역 조회 / 지급 되돌리기용)
CHAT_STATS_FILE = os.path.join(DATA_DIR, "mari_chat_stats.json") # 💬 [신규] 날짜별 채팅 "개수"만 세는 통계 (내용·작성자 미저장)
HEARTBEAT_FILE = os.path.join(DATA_DIR, "mari_heartbeat.txt") # 💓 [신규] 봇이 살아있음을 외부 감시 도구에 알리는 심장박동 파일
# 🖼️ [신규] 프로필 사진 원본을 보관하는 폴더.
# 예전엔 디스코드 첨부파일 URL을 그대로 저장했는데, 그 링크는 서명이 붙어 있어 하루 이틀이면
# 만료돼서 프로필 사진이 조용히 깨졌어요. 이제 이미지를 여기에 받아두고 매번 첨부해서 보여줍니다.
# ⚠️ 이름이 _DIR로 끝나므로 json_data_files()에는 잡히지 않아요. (자동 백업 대상은 .json만)
PROFILE_PHOTO_DIR = os.path.join(DATA_DIR, "profile_photos")
BACKUP_DIR = os.path.join(DATA_DIR, "backups") # 💾 매일 자동 백업이 쌓이는 폴더


def migrate_legacy_data_files():
    """예전 위치(main.py 옆)에 있던 데이터를 data/ 폴더로 한 번만 옮겨줍니다.

    기존 봇을 업데이트하는 경우 서버에 이미 라이브 데이터가 옛날 위치에 있어요.
    이걸 손으로 옮기게 하면 실수로 유실되기 쉬우니 봇이 알아서 이사시킵니다.

    🔒 안전 원칙: data/ 쪽에 같은 이름의 파일이 이미 있으면 절대 덮어쓰지 않아요.
    (새 데이터가 옛날 데이터로 되돌아가는 사고를 막기 위해서예요)
    """
    if os.path.abspath(DATA_DIR) == os.path.abspath(BASE_DIR):
        return  # 데이터 폴더를 코드 옆으로 지정한 경우엔 옮길 것이 없어요

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception as e:
        print(f"❗ 데이터 폴더를 만들 수 없어요: {DATA_DIR} ({type(e).__name__}: {e})")
        return

    moved, skipped = [], []

    # 1) main.py 옆에 남아있는 .json 파일 전부
    try:
        legacy_names = [n for n in os.listdir(BASE_DIR) if n.lower().endswith(".json")]
    except Exception:
        legacy_names = []

    for name in legacy_names:
        src = os.path.join(BASE_DIR, name)
        dst = os.path.join(DATA_DIR, name)
        if not os.path.isfile(src):
            continue
        if os.path.exists(dst):
            skipped.append(name)
            continue
        try:
            shutil.move(src, dst)
            moved.append(name)
        except Exception as e:
            print(f"❗ {name} 이동 실패: {type(e).__name__}: {e}")

    # 2) 예전 위치의 backups/ 폴더 통째로
    legacy_backup = os.path.join(BASE_DIR, "backups")
    if os.path.isdir(legacy_backup) and not os.path.exists(BACKUP_DIR):
        try:
            shutil.move(legacy_backup, BACKUP_DIR)
            moved.append("backups/")
        except Exception as e:
            print(f"❗ backups 폴더 이동 실패: {type(e).__name__}: {e}")

    # 3) 예전 위치의 심장박동 파일은 그냥 정리 (매번 새로 쓰이는 값이라 옮길 필요 없어요)
    legacy_heartbeat = os.path.join(BASE_DIR, "mari_heartbeat.txt")
    if os.path.isfile(legacy_heartbeat):
        try:
            os.remove(legacy_heartbeat)
        except Exception:
            pass

    if moved:
        print(f"📁 데이터를 data/ 폴더로 옮겼어요 ({len(moved)}개): {', '.join(moved[:8])}"
              + (" ..." if len(moved) > 8 else ""))
    if skipped:
        print(f"⚠️ data/ 폴더에 이미 있어서 건너뛴 파일 ({len(skipped)}개): {', '.join(skipped)}")
        print(f"   예전 위치({BASE_DIR})의 해당 파일은 그대로 남겨뒀어요. 확인 후 직접 정리해 주세요.")


migrate_legacy_data_files()

# 🏕️ 캠프 역할 매핑 및 캠프장 권한 역할 (지갑/세금 공용)
CAMP_LEADER_ROLE_ID = 1191777510451585024
CAMP_ROLE_IDS = {
    "악동": 1457359696540336199,
    "나래": 1457359755616977118,
    "여백": 1457359769999507588,
}
# 🏛️ [신규] "전율"은 특정 캠프가 아니라 캠프 전체를 관리하는 관리 기구예요.
# 이 역할을 가진 사람은 관리자처럼 3개 캠프(악동/나래/여백) 전부를 조작할 수 있어야 해요.
JEONYUL_ROLE_ID = 1457363783826538852

# 📢 [정리] 함수 본문 안에 흩어져 있던 하드코딩 역할 ID들을 여기로 모았어요.
GOHWAK_MENTION_ROLE_ID = 1063465600220921897   # /고확 방송에 함께 멘션할 역할
MARI_CALL_ROLE_ID = 1386036454039228510        # 이 역할을 멘션하면 마리를 부른 것으로 취급
# 🧭 타운가이드. /역할부여와 /도움말 관리자 모드 두 곳이 같은 값을 각자 하드코딩하고 있었어요.
# 역할이 바뀔 때 한쪽만 고치면 조용히 어긋나므로 여기로 모았습니다.
TOWN_GUIDE_ROLE_ID = 1135246176036327484


# ========== 🔐 [신규] 비밀값 읽기 ==========
# (.env 파일 자체는 파일 맨 위, 데이터 폴더를 정하기 전에 미리 읽어둡니다)
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# 🚨 [신규] 다운 알림용 웹훅. 봇이 죽는 순간에도 알림이 나가야 하므로,
# 봇 자신의 게이트웨이 연결이 아니라 독립적인 디스코드 웹훅을 씁니다.
# (디스코드 채널 설정 → 연동 → 웹훅에서 URL을 만들어 .env에 넣으세요)
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
ALERT_MENTION = os.environ.get("ALERT_MENTION", "").strip()  # 예: <@123456789> 또는 <@&123456789>

try:
    # 짧은 재연결은 정상이라 알리지 않아요. 이 시간(초) 넘게 끊겨 있을 때만 알립니다.
    ALERT_DISCONNECT_SECONDS = int(os.environ.get("ALERT_DISCONNECT_SECONDS", "60"))
except ValueError:
    ALERT_DISCONNECT_SECONDS = 60

ALERT_TIMEOUT = 5  # 웹훅 전송 대기 시간(초)

# ========== 📊 [신규] 그래프 색상 ==========
# 예전 색(#5ce6b4 / #ff6b6b)은 배경을 투명하게 저장하던 시절에 고른 값이라,
# 흰 배경에 올려두면 대비가 1.56:1 / 2.78:1 밖에 안 나와서 선이 배경에 묻혔어요.
# (선·마커 같은 그래픽 요소는 대비 3:1 이상이어야 읽힙니다)
# 아래 값은 흰 배경 기준으로 검증한 색이에요.
CHART_BG = "#ffffff"        # 배경 흰색 고정 (투명 저장 시 보는 사람 테마에 따라 축이 안 보였어요)
CHART_INK = "#0b0b0b"       # 제목 등 주요 텍스트 (19.7:1)
CHART_INK_SOFT = "#52514e"  # 축 눈금·축 이름 (7.9:1)
CHART_GRID = "#e5e4e0"      # 격자선 — 데이터보다 뒤로 물러나야 해요
CHART_UP = "#008300"        # 상승 (4.95:1)
CHART_DOWN = "#e34948"      # 하락 (3.95:1)

# 🚫 그래프 제목에서 이모지를 걷어내는 정규식.
# 한글 폰트(맑은 고딕 등)에는 이모지 글리프가 없어서 그대로 그리면 두부(□)로 깨져요.
# 이모지는 디스코드 메시지 본문에서 쓰고, 이미지 안에는 넣지 않습니다.
_CHART_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF️‍]+"
)

def json_data_files() -> dict:
    """이름이 "_FILE"로 끝나는 전역 상수 중 '진짜 JSON 데이터 파일'만 골라냅니다.

    🔎 [자동화] 새 데이터 파일이 생길 때마다 목록에 손으로 등록할 필요가 없어요.

    ⚠️ .json 확장자로 거르는 게 핵심입니다. 이 목록은 자동 생성·자동 백업·무결성 점검
    세 곳에서 공통으로 쓰는데, .env(비밀값)나 심장박동 파일처럼 JSON이 아닌 경로가
    섞이면 이런 사고가 납니다:
      • .env가 "{}" 내용으로 덮어써져 봇 설정이 날아감
      • .env가 백업 폴더로 복사되면서 토큰·API 키가 여기저기 퍼짐
      • JSON 파싱에 실패해 멀쩡한 파일이 "손상됨"으로 잘못 보고됨
    """
    return {
        name: value for name, value in globals().items()
        if name.endswith("_FILE") and isinstance(value, str) and value.endswith(".json")
    }
