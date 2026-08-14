"""경로·비밀값(.env)·시간대·서버 고유 ID(guild.json)·그래프 색상 등 순수 설정값 모음.

거의 최하위 계층이에요. modules(순수 자료) 하나만 import하고, 다른 봇 모듈은
일절 부르지 않습니다. modules를 부르는 이유는 "이번 배포에 뭐가 담겼나"를 여기서 한 번
확정해서 나눠주기 위해서예요. (ENABLED_SPECS / active_data_files 참고)"""

import json
import os
import re
import shutil
import datetime as dt
import discord

# modules는 아무것도 import하지 않는 순수 자료 계층이라 여기서 불러도 순환이 생기지 않아요.
from modules import is_active, owners, resolve_modules

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

# 🏷️ 서버 고유 설정 파일의 위치. (내용을 읽는 부분은 파일 아래쪽 "서버 고유 ID 분리" 절에 있어요)
#
# ⚠️ 이 경로는 아래 migrate_legacy_data_files()보다 **먼저** 정해져야 합니다.
#    그 함수가 BASE_DIR의 .json을 전부 data/로 옮기는데, guild.json은 코드 옆(.env 자리)에
#    두는 배포 설정이라 같이 쓸려가면 안 돼요. 옮겨지고 나면 설정을 못 찾아서
#    "설정 파일이 없다"며 역할 기능이 통째로 죽습니다.
GUILD_CONFIG_PATH = os.environ.get("MARI_GUILD_CONFIG", "").strip() or os.path.join(BASE_DIR, "guild.json")

# 데이터가 아니라 설정이라서 data/로 이사시키면 안 되는 파일들.
#
# 🐛 [버그 수정] 예전엔 이 집합을 `{basename(GUILD_CONFIG_PATH), "guild.example.json"}`으로만
# 만들었어요. 그런데 MARI_GUILD_CONFIG로 다른 경로를 지정하면 기본 이름인 guild.json이
# 목록에서 빠져버려서, **코드 옆에 있던 guild.json이 data/로 실려갔습니다.**
# (실제로 검증 중에 당했어요. 봇 한 대에서 설정만 바꿔 띄우는 순간 원래 설정이 사라집니다)
# 기본 이름은 무슨 일이 있어도 항상 지킵니다.
_CONFIG_FILENAMES = {
    "guild.json",
    "guild.example.json",
    os.path.basename(GUILD_CONFIG_PATH).lower(),
}

IDS_FILE = os.path.join(DATA_DIR, "ids.json")
WIKI_FILE = os.path.join(DATA_DIR, "mari_wiki.json")
ATTENDANCE_FILE = os.path.join(DATA_DIR, "mari_attendance.json")
STOCKS_FILE = os.path.join(DATA_DIR, "mari_stocks.json")
ECONOMY_FILE = os.path.join(DATA_DIR, "mari_economy.json")
SHOP_FILE = os.path.join(DATA_DIR, "mari_shop.json")
SHOP_TRANSACTIONS_FILE = os.path.join(DATA_DIR, "mari_shop_transactions.json")  # 💰 [신규] /정산용 구매·되팔기 거래 내역
CHRONICLE_FILE = os.path.join(DATA_DIR, "mari_chronicle.json")  # 📜 [신규] 서버 연대기 (서버에서 일어난 일을 날짜순으로 축적)
SNOOZE_FILE = os.path.join(DATA_DIR, "mari_snooze.json")  # ⏰ [신규] '나중에 답장' 예약 목록 (우클릭으로 미뤄둔 메시지)
LIMIT_FILE = os.path.join(DATA_DIR, "mari_limit.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "mari_settings.json") # 💡 신규 설정 파일
BIRTHDAY_FILE = os.path.join(DATA_DIR, "mari_birthday.json")
CHAT_MEMORY_FILE = os.path.join(DATA_DIR, "mari_chat_memory.json") # 💬 유저별 마지막 대화 기억 파일
CHAT_LOG_FILE = os.path.join(DATA_DIR, "mari_chat_log.json") # 📜 서버별 최근 채팅 로그(맥락 참고용, 개수는 MariGPT.CHAT_LOG_MAXLEN 참고)
MARI_USER_MEMORY_FILE = os.path.join(DATA_DIR, "mari_user_memory.json") # 🧠 [신규] 유저별 장기 기억(사실 목록)
ID_PENDING_FILE = os.path.join(DATA_DIR, "mari_id_pending.json") # 🆔 [신규] 아이디 자동등록 중 플랫폼이 불명확해 관리자 확인이 필요한 대기열
PORTFOLIO_HISTORY_FILE = os.path.join(DATA_DIR, "mari_portfolio_history.json") # 📈 [신규] 종가게시 시점 유저별 포트폴리오 총 가치 스냅샷
LEDGER_FILE = os.path.join(DATA_DIR, "mari_ledger.json") # 🧾 [신규] 재화 입출금 원장 (유저 거래내역 조회 / 지급 되돌리기용)
CHAT_STATS_FILE = os.path.join(DATA_DIR, "mari_chat_stats.json") # 💬 [신규] 날짜별 채팅 "개수"만 세는 통계 (내용·작성자 미저장)
HEARTBEAT_FILE = os.path.join(DATA_DIR, "mari_heartbeat.txt") # 💓 [신규] 봇이 살아있음을 외부 감시 도구에 알리는 심장박동 파일
BACKUP_DIR = os.path.join(DATA_DIR, "backups") # 💾 매일 자동 백업이 쌓이는 폴더

# 🗑️ [정리] 창고로 간 기능들의 데이터 파일 상수도 같이 걷어냈어요.
# (mari_profile.json / mari_visit_pass_ask.json / mari_camp_*.json / mari_exile_data.json,
#  그리고 프로필 사진 폴더 PROFILE_PHOTO_DIR)
# 남겨두면 아래 json_data_files()가 이 이름들을 자동으로 주워서, 아무도 안 쓰는 빈 JSON을
# 배포마다 새로 만들고 매일 백업까지 합니다. 되살릴 때는 각 parked/*.txt 를 참고하세요.


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
    # (단, guild.json 같은 **배포 설정**은 원래 여기 사는 파일이라 건드리면 안 돼요)
    try:
        legacy_names = [
            n for n in os.listdir(BASE_DIR)
            if n.lower().endswith(".json") and n.lower() not in _CONFIG_FILENAMES
        ]
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

# ========== 🏷️ [신규] 서버 고유 ID 분리 (guild.json) ==========
# 예전엔 역할·유저 ID가 코드 곳곳에 숫자로 박혀 있었어요. 이 봇을 다른 서버에 올리려면
# mari_config.py / cogs/roster.py / cogs/setting.py / cogs/gpt.py 네 파일을 뒤져가며
# 스무 개쯤 되는 숫자를 고쳐야 했고, roster.py는 캠프 역할을 여기와 **따로 한 벌 더**
# 들고 있어서 한쪽만 고치면 조용히 어긋났습니다.
#
# 이제 서버마다 달라지는 값은 전부 guild.json 한 파일에 모여 있어요. 코드는 특정 서버를
# 전혀 모릅니다. 새 서버에 납품할 때는 guild.example.json을 복사해 값만 채우면 됩니다.
#
# ⚠️ 상수 이름이 GUILD_CONFIG_"PATH"인 건 의도적이에요. _FILE로 바꾸지 마세요.
#    json_data_files()가 "_FILE로 끝나고 .json인 전역"을 전부 데이터 파일로 간주하는데,
#    그러면 init_json_files()가 설정 파일을 "{}"로 자동 생성해 버려요. 설정을 빠뜨린 채
#    켰을 때 경고 대신 **빈 설정으로 조용히 기동**하게 되는데, 그게 제일 나쁜 결과입니다.
#    (자동 백업 대상에 섞이는 것도 곤란해요. 이건 데이터가 아니라 배포 설정이에요)
#
# (경로 자체는 파일 위쪽 DATA_DIR 옆에서 정합니다. migrate_legacy_data_files()가
#  이 파일을 data/로 옮겨버리지 않게 하려면 그보다 먼저 정해져 있어야 해요)

# 📋 설정을 읽으면서 발견한 문제들. 기동 로그에 한 번 찍고, 진단 명령에서도 볼 수 있게 남겨둬요.
GUILD_CONFIG_WARNINGS = []


# 🚨 설정 파일이 **있는데 못 읽었을 때** 여기에 이유가 담깁니다. (없으면 None)
# main.py가 이걸 보고 봇을 시작하지 않아요.
#
# ⚠️ 여기서 예외를 던지면 안 됩니다. mari_config는 main.py가 제일 먼저 import하는
#    모듈이라, import 도중에 터지면 다운 알림 웹훅에 닿기도 전에 죽어요. systemd로
#    띄웠다면 아무도 모르는 채 크래시 루프만 돕니다. 그래서 "값으로 알리고 main.py가
#    판단"하는 방식입니다. (state.load() 실패를 다루는 방식과 같아요)
GUILD_CONFIG_FATAL = None


def load_guild_config(path: str = GUILD_CONFIG_PATH) -> dict:
    """guild.json을 읽어옵니다.

    파일이 **없으면** 빈 설정으로 보고 그냥 뜹니다. 새로 납품한 서버가 그렇고,
    그때는 전부 켜진 상태가 맞아요.

    파일이 **있는데 못 읽으면** 기동을 막습니다(GUILD_CONFIG_FATAL). 누군가 고치다
    실수한 상황인데, 그대로 켜면 `modules`를 못 읽어 **주문하지 않은 기능까지 전부
    켜진 채로** 납품돼요. 클라이언트가 그걸 한 달 쓰다가 쉼표를 고치는 순간 그
    데이터가 통째로 빠집니다. 조용히 잘못 켜지는 것보다 시끄럽게 안 켜지는 게 나아요.
    """
    global GUILD_CONFIG_FATAL

    if not os.path.exists(path):
        GUILD_CONFIG_WARNINGS.append(
            f"설정 파일이 없어요: {path}\n"
            f"   guild.example.json을 복사해 guild.json으로 만들고 값을 채워주세요.\n"
            f"   (역할 ID가 필요한 기능은 전부 동작하지 않습니다)"
        )
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        GUILD_CONFIG_FATAL = (
            f"{type(e).__name__}: {e}\n\n"
            f"   파일은 있는데 읽을 수가 없어요 ({path}).\n"
            f"   이대로 켜면 modules를 못 읽어서 **주문하지 않은 기능까지 전부**\n"
            f"   켜진 채로 납품됩니다. guild.json을 고친 뒤 다시 실행해 주세요.\n"
            f"   (JSON 문법 검사: https://jsonlint.com)"
        )
        return {}
    if not isinstance(data, dict):
        GUILD_CONFIG_FATAL = (
            f"설정 파일의 최상위가 객체({{...}})가 아니에요.\n\n"
            f"   파일: {path}\n"
            f"   맨 바깥이 {{ }} 로 감싸져 있어야 해요. guild.example.json을 참고하세요."
        )
        return {}
    return data


_GUILD = load_guild_config()


def _section(name: str) -> dict:
    """guild.json의 한 섹션을 딕셔너리로. 없거나 형식이 틀리면 빈 값."""
    value = _GUILD.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        GUILD_CONFIG_WARNINGS.append(f"'{name}' 항목은 객체({{...}})여야 해요. 무시하고 넘어갑니다.")
        return {}
    # "_"로 시작하는 키는 사람이 읽는 주석이에요. (JSON에는 주석 문법이 없어서 이렇게 씁니다)
    return {k: v for k, v in value.items() if not k.startswith("_")}


def _as_id(value, where: str) -> int:
    """디스코드 ID로 쓸 수 있는 정수로 바꿔요. 못 쓰는 값이면 0(=미설정)."""
    if value is None or value == 0 or value == "":
        return 0
    try:
        # JSON에 문자열로 적어둔 ID("123...")도 받아줍니다. 자릿수가 커서 따옴표를 치는 사람이 많아요.
        return int(value)
    except (TypeError, ValueError):
        GUILD_CONFIG_WARNINGS.append(f"'{where}'의 값이 ID(숫자)가 아니에요: {value!r} — 미설정으로 둡니다.")
        return 0


_ROLES = _section("roles")

# 🔄 예전 키 이름 → 지금 이름. 이미 돌고 있는 서버의 guild.json은 깃이 안 건드리므로
# (`.gitignore` 대상이에요) 코드를 업데이트해도 설정 파일은 옛날 그대로입니다.
# 새 이름이 없으면 옛 이름을 대신 읽어서, **업데이트만 하고 아무것도 안 고쳐도**
# 지금까지처럼 동작하게 해요. 대신 기동 로그로 이름을 바꾸라고 알려줍니다.
_ROLE_ALIASES = {
    # /고확을 창고로 보내면서 이 역할의 남은 쓰임이 생일 알림뿐이라 이름을 바꿨어요.
    # (원본 서버에선 고확 멘션과 생일 멘션이 같은 '시민권' 역할이라 상수를 공유했습니다)
    "birthday_mention": "broadcast_mention",
}


def _role(key: str) -> int:
    if key in _ROLES:
        return _as_id(_ROLES[key], f"roles.{key}")

    old = _ROLE_ALIASES.get(key)
    if old and old in _ROLES:
        GUILD_CONFIG_WARNINGS.append(
            f"'roles.{old}'는 예전 이름이에요. 지금은 'roles.{key}'입니다. "
            f"일단 옛 이름 값을 그대로 쓰지만, guild.json에서 이름을 바꿔주세요."
        )
        return _as_id(_ROLES[old], f"roles.{old}")

    return 0


# 📢 서버마다 다른 단일 역할들.
#
# 🗑️ [정리] 예전엔 여기에 캠프 역할(CAMP_ROLE_IDS·CAMP_LEADER_ROLE_ID·JEONYUL_ROLE_ID)과
# 레벨/소속 역할표(LEVEL_ROLES·AFFILIATIONS)도 함께 있었어요. 전부 원본 서버 고유 제도라
# 기능째로 창고(parked/)로 옮기면서 설정도 같이 걷어냈습니다.
# 🎂 [이름 변경] 예전 이름은 GOHWAK_MENTION_ROLE_ID(= /고확 방송에 함께 멘션할 역할)였어요.
# 원본 서버에서는 고확과 생일 알림이 **같은 역할(시민권)** 을 태그해서 상수 하나를 공유했는데,
# 고확을 창고(parked/games_broadcast.py.txt)로 보내면서 남은 사용처가 생일 알림뿐이라
# 그 쓰임에 맞는 이름으로 바꿨습니다. 고확을 되살릴 땐 별도 키를 새로 만드세요.
BIRTHDAY_MENTION_ROLE_ID = _role("birthday_mention")  # 생일 알림 때 함께 멘션할 역할
MARI_CALL_ROLE_ID = _role("bot_mention")             # 이 역할을 멘션하면 봇을 부른 것으로 취급


# 🪜 [신규] 등급 사다리. 아이디 명단(cogs/ids.py)을 이 순서대로 묶어서 게시해요.
#
# 원본 서버에는 '대장 / 4~0레벨'이라는 등급 제도가 있었고, 그 이름과 역할 ID가 코드에
# 통째로 박혀 있었어요. 등급 제도는 서버마다 완전히 달라서(있는 곳도, 없는 곳도 있어요)
# 설정으로 뺐습니다. **안 적으면 등급 구분 없이 한 덩어리로 게시해요.**
# (원래 코드는 parked/core_levels.py.txt 에 보관돼 있고, 이 설계도 거기 적어둔 것입니다)
#
# 명단에 쓰는 ANSI 코드는 사람이 외우기 어려워서 색깔 이름으로 받습니다.
_RANK_COLORS = {
    "빨강": "2;41", "보라": "2;35", "파랑": "2;34",
    "청록": "2;36", "노랑": "2;33", "초록": "2;32", "회색": "2;30",
}
# 색을 안 적었을 때 위에서부터 차례로 물려주는 기본 팔레트예요.
_RANK_COLOR_CYCLE = ("2;41", "2;35", "2;34", "2;36", "2;33", "2;32")


def _load_ranks() -> list:
    """[{"key","label","role","color"}] 목록으로. 순서가 곧 명단에 찍히는 순서예요."""
    raw_list = _GUILD.get("ranks")
    if raw_list is None:
        return []
    if not isinstance(raw_list, list):
        GUILD_CONFIG_WARNINGS.append("'ranks'는 목록([...])이어야 해요. 무시하고 등급 구분 없이 갑니다.")
        return []

    ranks = []
    seen_keys = set()
    for index, raw in enumerate(raw_list):
        where = f"ranks[{index}]"
        if not isinstance(raw, dict):
            GUILD_CONFIG_WARNINGS.append(f"'{where}'는 객체({{...}})여야 해요. 건너뜁니다.")
            continue

        role_id = _as_id(raw.get("role"), f"{where}.role")
        if not role_id:
            # 역할이 없으면 누가 이 등급인지 판별할 방법이 없어요.
            GUILD_CONFIG_WARNINGS.append(f"'{where}'에 role(역할 ID)이 없어요. 건너뜁니다.")
            continue

        # 키는 데이터에 남지 않고 코드 안에서만 쓰는 이름이라, 없으면 순번으로 만들어줘요.
        key = str(raw.get("key") or f"rank{index}").strip()
        if key in seen_keys:
            GUILD_CONFIG_WARNINGS.append(f"'{where}'의 key '{key}'가 앞에서 이미 쓰였어요. 건너뜁니다.")
            continue
        seen_keys.add(key)

        color = raw.get("color")
        if color in _RANK_COLORS:
            ansi = _RANK_COLORS[color]
        else:
            if color:
                GUILD_CONFIG_WARNINGS.append(
                    f"'{where}.color'로 '{color}'는 못 알아들어요. "
                    f"쓸 수 있는 색: {', '.join(_RANK_COLORS)} — 기본색으로 둡니다."
                )
            ansi = _RANK_COLOR_CYCLE[len(ranks) % len(_RANK_COLOR_CYCLE)]

        ranks.append({
            "key": key,
            "label": str(raw.get("label") or key),
            "role": role_id,
            "color": ansi,
        })
    return ranks


RANKS = _load_ranks()


# 🧹 코드가 읽지 않는 항목 알려주기.
#
# guild.json은 깃이 안 건드리는 파일이라(`.gitignore`), 창고로 보낸 기능의 설정이
# 그대로 남아 있기 쉬워요. 지금은 그냥 조용히 무시되는데, 그러면 오타로 적은 키도
# 똑같이 조용히 무시됩니다. 역할 ID를 정성껏 채워 넣고 "왜 안 되지?" 하는 상황이
# 제일 나빠서, 무엇을 안 읽고 있는지 기동 때 알려줍니다.
_READ_TOP_LEVEL = {"modules", "roles", "ranks"}
_READ_ROLES = {"birthday_mention", "bot_mention"}

# 📦 창고(parked/)로 간 기능이 쓰던 것들. 지워도 되는 잔재라고 알려줄 수 있어요.
_PARKED_TOP_LEVEL = {"camps", "levels", "affiliations", "onboarding", "personas"}
_PARKED_ROLES = {"camp_leader", "camp_overseer", "town_guide"}


def _warn_unused_guild_keys() -> None:
    def look(actual, read, parked, where):
        # "_"로 시작하는 건 사람이 읽는 주석이라 건너뜁니다.
        leftover = sorted(k for k in actual if not k.startswith("_") and k not in read)
        gone = [k for k in leftover if k in parked]
        unknown = [k for k in leftover if k not in parked]
        if gone:
            GUILD_CONFIG_WARNINGS.append(
                f"{where}의 {gone}는 창고(parked/)로 간 기능이 쓰던 설정이에요. 지워도 됩니다.")
        if unknown:
            GUILD_CONFIG_WARNINGS.append(
                f"{where}의 {unknown}는 코드가 읽지 않아요. 오타가 아닌지 확인해주세요.")

    look(_GUILD, _READ_TOP_LEVEL, _PARKED_TOP_LEVEL, "설정 최상위")
    # 옛 이름은 위 _role()이 "이름을 바꾸세요"라고 이미 안내했으니 여기선 또 말하지 않아요.
    look(_ROLES, _READ_ROLES | set(_ROLE_ALIASES.values()), _PARKED_ROLES, "roles")


_warn_unused_guild_keys()


# 🧩 [신규] 이 배포에 담을 기능 목록. 실제 정의는 modules.py에 있어요.
# 안 적으면(None) 전부 켭니다. 설정을 손대지 않은 기존 배포가 그대로 돌아가야 하니까요.
def _load_enabled_modules():
    value = _GUILD.get("modules")
    if value is None:
        return None
    if not isinstance(value, list):
        GUILD_CONFIG_WARNINGS.append(
            "'modules'는 목록([\"economy\", \"shop\", ...])이어야 해요. 무시하고 전부 켭니다."
        )
        return None
    names = [str(v).strip() for v in value if str(v).strip()]
    if not names:
        # 빈 목록은 "아무것도 켜지 마"라는 뜻일 수도, 실수일 수도 있어요. 사고 쪽이 훨씬
        # 비싸므로(명령어가 전부 사라진 봇) 실수로 보고 전부 켭니다.
        GUILD_CONFIG_WARNINGS.append("'modules'가 비어 있어요. 실수로 보고 전부 켭니다.")
        return None
    return names


ENABLED_MODULES = _load_enabled_modules()

# 🧩 이번 배포에 실제로 담기는 모듈을 여기서 딱 한 번 확정합니다.
# (코어 강제 포함과 의존 모듈 끌어오기까지 끝난 결과예요)
#
# 예전엔 mari_client가 자기 혼자 resolve_modules()를 불렀어요. 그런데 "지금 뭐가 담겼나"를
# 알아야 하는 곳이 클라이언트 말고도 여럿(설정 명령·도움말·진단·데이터 파일 생성)이라,
# 각자 부르면 답이 갈라질 수 있습니다. 한 곳에서 정하고 나눠 씁니다.
ENABLED_SPECS, MODULE_WARNINGS = resolve_modules(ENABLED_MODULES)
ENABLED_MODULE_KEYS = frozenset(spec.key for spec in ENABLED_SPECS)


def module_active(key: str) -> bool:
    """그 모듈이 이번 배포에 담겼는지."""
    return key in ENABLED_MODULE_KEYS


def active_keys(kind: str, names) -> list:
    """`names` 중에서 지금 담긴 모듈이 데려온 것만 순서대로 골라냅니다.

    kind는 "channels" / "roles" / "features" 중 하나예요. 어느 모듈도 소유하지 않은
    키는 공용으로 보고 그대로 남깁니다. (modules.owners 설명 참고)
    """
    return [name for name in names if is_active(kind, name, ENABLED_MODULE_KEYS)]


def report_guild_config() -> None:
    """기동 시 설정 상태를 한 번 알려줍니다. (main.py가 아니라 여기서 부르는 이유는
    설정을 읽자마자 알리는 게 원인 파악에 제일 빠르기 때문이에요)"""
    if GUILD_CONFIG_WARNINGS:
        print("⚠️ 서버 설정(guild.json)에 확인할 점이 있어요:")
        for warning in GUILD_CONFIG_WARNINGS:
            print(f" • {warning}")
    else:
        print(f"🏷️ 서버 설정을 읽었어요. 역할 {len(_ROLES)}개")


report_guild_config()


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


def active_data_files() -> dict:
    """위 목록 중, **이번 배포에 담긴 모듈이 실제로 쓰는** 파일만.

    주식을 안 담고 납품했는데도 `mari_stocks.json`이 만들어지고 매일 백업까지 되던 문제를
    막습니다. 어느 모듈의 것도 아닌 파일(설정 등)은 공용이라 항상 남아요.

    ⚠️ 백업(cogs/backup.py)과 손상 점검(cogs/diagnostics.py)은 일부러 이걸 쓰지 않고
       json_data_files() 전체를 봅니다. 둘 다 **없는 파일은 알아서 건너뛰기** 때문에,
       전체를 훑어야 예전 배포에서 넘어온 파일까지 빠짐없이 지킬 수 있어요.
       여기서 거르면 기능을 뺀 순간 그 데이터가 백업 대상에서 조용히 빠집니다.
    """
    owner = module_owner_of_data_files()
    return {
        name: path for name, path in json_data_files().items()
        if owner.get(name) is None or owner[name] in ENABLED_MODULE_KEYS
    }


def module_owner_of_data_files() -> dict:
    """`{상수 이름: 소유 모듈 키}`. (modules.py의 data_files를 그대로 뒤집은 것)"""
    return owners("data_files")
