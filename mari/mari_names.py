"""서버마다 달라지는 '이름'(재화·봇·이벤트·서버)을 한 곳에서 관리해요.

예전엔 원본 서버의 재화·봇·이벤트·서버 이름이 코드 곳곳의 문자열에 그대로 박혀 있었어요.
다른 서버에 납품하려면 스무 개 파일을 뒤져가며 400곳 가까이 고쳐야 했고, 조사(을/를)까지
같이 손봐야 해서 빠뜨리기 쉬웠습니다. 이제 코드는 이름을 모르고, 값은 여기서 옵니다.

값은 settings.json의 `names` 섹션에 살아요. 배포 설정(guild.json)이 아니라 런타임
설정(settings.json)에 두는 이유는, 이 값을 **봇이 스스로 쓰기** 때문이에요.
관리자가 `/초기설정`으로 입력하면 봇이 그 자리에서 저장합니다. (cogs/setting.py 참고)

┌─ 쓰는 법 ────────────────────────────────────────────────────────────┐
│   from mari_names import currency, bot_name, event_name, josa        │
│                                                                       │
│   f"{amount:,} {currency()}"          → "1,000 골드"                  │
│   f"{currency()}{josa(currency(), '을를')} 보냈어요"                   │
│                                   → "골드를 보냈어요" / "코인을 …"      │
└───────────────────────────────────────────────────────────────────────┘

⚠️ 조사를 손으로 붙이지 마세요. 이름이 "루나"(받침 없음)에서 "골드"·"코인"(받침 있음)으로
   바뀌는 순간 "코인를 보냈어요"가 됩니다. 이름 바로 뒤에 오는 을/를·이/가·은/는·와/과·
   로/으로는 전부 josa()를 거쳐야 해요.

⚠️ 이름을 모듈 수준 상수로 받아두지 마세요(`CURRENCY = currency()` 금지). 그러면 기동
   시점의 값이 얼어붙어서, `/초기설정`으로 바꿔도 봇을 껐다 켜기 전까지 옛 이름이 계속
   나옵니다. **함수를 호출하는 시점에** 읽어야 해요.
   (슬래시 명령의 이름·설명만은 예외입니다. 데코레이터는 import 시점에 한 번만 실행되니까요.
    그래서 명령 '이름'에는 아예 이 값을 쓰지 않습니다 — 아래 설명 참고)
"""

import os

from mari_config import SETTINGS_FILE
from mari_settings import load_settings, save_settings

# ========== 🏷️ 받는 이름들 ==========
# key      : settings.json의 names 아래 저장되는 키
# label    : /초기설정 입력창에 보이는 항목 이름
# default  : 입력받기 전까지 쓸 값. **일반 명사만 씁니다** — 재화·봇·이벤트·서버.
#            예전엔 원본 서버의 이름이 기본값이었는데, `/초기설정`을 아직 안 한 서버의
#            문구에 남의 서버 이름이 그대로 나갔어요. 지금은 "1,000 재화"처럼 밋밋할
#            뿐이라 사고가 아니라 "아직 안 정했구나"로 읽힙니다.
# example  : 입력창 placeholder
# hint     : 무엇을 가리키는 이름인지 (도움말·안내 문구용)
NAME_FIELDS = {
    "currency": {
        "label": "재화 이름",
        "default": "재화",
        "example": "예) 골드, 코인, 별사탕",
        "hint": "지갑·송금·상점에서 쓰는 서버 화폐 이름",
    },
    "bot": {
        "label": "봇 이름",
        "default": "봇",
        "example": "예) 루나, 하루",
        "hint": "봇이 스스로를 부르는 이름",
    },
    "event": {
        "label": "선착순 이벤트 이름",
        "default": "이벤트",
        "example": "예) 타임세일, 번개장터",
        "hint": "정해진 시각에 채널에서 이 단어를 치면 선착순 보상을 주는 이벤트",
    },
    "server": {
        "label": "서버 이름",
        "default": "서버",
        "example": "예) 달빛마을",
        "hint": "안내 문구에서 서버를 가리킬 때 쓰는 이름",
    },
}

# 이름 길이 상한.
#
# ⚠️ 넉넉해 보이지만 이유가 있어요. 이 값은 슬래시 명령 **설명문**에도 들어가는데,
#    디스코드는 설명을 100자로 제한하고 넘으면 명령 등록 자체를 거부합니다.
#    (= 동기화가 통째로 실패해서 명령어가 전부 사라져요)
#    설명 중 제일 긴 것이 60자 남짓이고 이름이 최대 두 번 들어가므로 12자면 안전합니다.
#    늘리려면 `python tools/check_modules.py`로 payload 생성이 되는지 먼저 확인하세요.
MAX_NAME_LENGTH = 12

# 이름에 쓸 수 없는 글자.
#   공백·줄바꿈 — 문구가 깨지고, 설명문 안에서 단어 경계가 사라져요
#   @ # < > &   — 멘션·채널 링크로 해석돼서 엉뚱한 알림이 울립니다
#   ` * _ ~ |   — 디스코드 마크다운. 문구 전체의 굵게/기울임이 어긋나요
#   \ " '       — 문자열을 그대로 넣는 자리가 있어서 미리 막아둡니다
_FORBIDDEN_CHARS = set(' \t\n\r@#<>&`*_~|\\"\'')


def validate_name(value: str) -> tuple:
    """이름으로 쓸 수 있는 값인지 확인해요. (쓸 수 있으면, 다듬은 값 또는 사유)를 돌려줍니다.

    반환: (True, 다듬은값) 또는 (False, 사람이 읽는 사유)
    """
    text = (value or "").strip()
    if not text:
        return False, "비어 있어요."
    if len(text) > MAX_NAME_LENGTH:
        return False, f"{MAX_NAME_LENGTH}자 이내로 지어주세요. (지금 {len(text)}자)"
    bad = sorted({c for c in text if c in _FORBIDDEN_CHARS})
    if bad:
        shown = " ".join(repr(c) if c.isspace() else c for c in bad)
        return False, f"쓸 수 없는 글자가 있어요: {shown}"
    return True, text


# ========== 💾 값 읽기 ==========
# settings.json은 서버의 모든 메세지마다 읽히는 파일이고, load_settings()는 호출할 때마다
# 딥카피를 한 벌 만들어요. 이름은 임베드 하나를 만드는 동안에도 수십 번 불리기 때문에
# 그때마다 딥카피를 하면 낭비가 큽니다. 파일의 (수정시각, 크기)가 그대로면 지난 결과를
# 재사용해요. (mari_settings의 캐시와 같은 방식이라, 파일을 손으로 고쳐도 바로 반영됩니다)
_names_cache = None
_names_cache_stamp = None


def _settings_stamp():
    try:
        st = os.stat(SETTINGS_FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def get_names() -> dict:
    """네 이름을 전부 담은 딕셔너리. 설정되지 않은 항목은 기본값으로 채워져요."""
    global _names_cache, _names_cache_stamp

    stamp = _settings_stamp()
    if _names_cache is not None and stamp == _names_cache_stamp:
        return _names_cache

    stored = load_settings().get("names") or {}
    if not isinstance(stored, dict):
        stored = {}

    resolved = {}
    for key, field in NAME_FIELDS.items():
        ok, value = validate_name(stored.get(key, ""))
        # 저장된 값이 규칙에 어긋나면(손으로 고쳤다거나) 조용히 기본값으로 돌아갑니다.
        # 여기서 예외를 던지면 이름 하나 때문에 봇 전체가 말을 못 하게 돼요.
        resolved[key] = value if ok else field["default"]

    _names_cache, _names_cache_stamp = resolved, stamp
    return resolved


def currency() -> str:
    """서버 재화 이름. (아직 안 정했으면 "재화")"""
    return get_names()["currency"]


def bot_name() -> str:
    """봇 이름. (아직 안 정했으면 "봇")"""
    return get_names()["bot"]


def event_name() -> str:
    """선착순 이벤트 이름. (아직 안 정했으면 "이벤트")"""
    return get_names()["event"]


def server_name() -> str:
    """서버 이름. (아직 안 정했으면 "서버")"""
    return get_names()["server"]


def is_configured() -> bool:
    """`/초기설정`으로 이름을 한 번이라도 입력했는지."""
    stored = load_settings().get("names")
    return isinstance(stored, dict) and any(
        validate_name(stored.get(key, ""))[0] for key in NAME_FIELDS
    )


def save_names(values: dict) -> dict:
    """이름을 settings.json에 저장하고, 실제로 저장된 값을 돌려줍니다.

    values에 없는 키는 건드리지 않아요. (한 항목만 고치는 경우가 있어서)
    규칙에 어긋나는 값은 ValueError로 거절합니다 — 호출부가 미리 validate_name()으로
    걸러야 하고, 여기 검사는 그걸 빠뜨렸을 때를 위한 2차 방어선이에요.
    """
    global _names_cache, _names_cache_stamp

    cleaned = {}
    for key, raw in values.items():
        if key not in NAME_FIELDS:
            raise ValueError(f"모르는 이름 항목이에요: {key}")
        ok, result = validate_name(raw)
        if not ok:
            raise ValueError(f"{NAME_FIELDS[key]['label']}: {result}")
        cleaned[key] = result

    settings = load_settings()
    names = settings.get("names")
    if not isinstance(names, dict):
        names = {}
    names.update(cleaned)
    settings["names"] = names
    save_settings(settings)

    # 저장 직후에도 바로 새 이름이 나오도록 캐시를 비웁니다.
    # (파일 수정시각만 믿으면 같은 초에 두 번 저장했을 때 옛 값이 남을 수 있어요)
    _names_cache = None
    _names_cache_stamp = None
    return get_names()


# ========== 🇰🇷 조사 ==========
# 한국어는 앞 글자의 받침 유무에 따라 조사가 달라져요. 이름을 변수로 만든 이상
# "골드를"처럼 손으로 붙여두면 이름이 "코인"으로 바뀌는 순간 "코인를"이 됩니다.
#
# 짝은 **받침 있을 때가 앞**입니다. josa("코인", "을를") → "을", josa("루나", "을를") → "를"
_JOSA_PAIRS = {
    "을를": ("을", "를"),
    "이가": ("이", "가"),
    "은는": ("은", "는"),
    "와과": ("과", "와"),   # ⚠️ 받침 있을 때가 '과'예요. 짝 이름과 순서가 반대라 헷갈리기 쉬워요
    "과와": ("과", "와"),   # 부르는 쪽에서 어느 순서로 적든 같게 동작하도록 둘 다 받습니다
    "아야": ("아", "야"),         # 부를 때 — "하늘아" / "루나야"
    "이에요예요": ("이에요", "예요"),
    "이다다": ("이다", "다"),      # 서술 — "하늘이다" / "루나다"
    "으로로": ("으로", "로"),  # ㄹ받침은 예외 — 아래 _final_jamo로 따로 처리해요
    "로으로": ("으로", "로"),
}

# 숫자로 끝나는 이름("코인2")도 소리 나는 대로 판정해요.
# 영(ㅇ)·일(ㄹ)·삼(ㅁ)·육(ㄱ)·칠(ㄹ)·팔(ㄹ)은 받침이 있고, 이·사·오·구는 없어요.
_DIGIT_HAS_BATCHIM = {
    "0": True, "1": True, "2": False, "3": True, "4": False,
    "5": False, "6": True, "7": True, "8": True, "9": False,
}


def _final_jamo(word: str) -> int:
    """마지막 글자의 받침 코드. 받침이 없으면 0, 판단할 수 없으면 -1.

    (한글 음절은 0xAC00부터 초성19 × 중성21 × 종성28 순서로 배열돼 있어서,
     28로 나눈 나머지가 곧 종성 번호예요. 0이면 받침 없음, 8이면 ㄹ)
    """
    text = (word or "").strip()
    if not text:
        return -1
    last = text[-1]
    code = ord(last)
    if 0xAC00 <= code <= 0xD7A3:
        return (code - 0xAC00) % 28
    if last in _DIGIT_HAS_BATCHIM:
        return 1 if _DIGIT_HAS_BATCHIM[last] else 0
    return -1


def josa(word: str, pair: str) -> str:
    """`word` 뒤에 붙일 조사를 골라줍니다.

        f"{currency()}{josa(currency(), '을를')}"  → "골드를" / "코인을"

    한글도 숫자도 아닌 글자로 끝나서 판단할 수 없으면(영문 등) 받침 없는 쪽을 씁니다.
    어느 쪽을 골라도 어색한 경우라, 더 흔한 쪽으로 두는 게 덜 튀어요.
    """
    if pair not in _JOSA_PAIRS:
        raise ValueError(f"모르는 조사 짝이에요: {pair!r} (가능: {', '.join(_JOSA_PAIRS)})")
    with_batchim, without_batchim = _JOSA_PAIRS[pair]

    final = _final_jamo(word)
    if pair in ("으로로", "로으로"):
        # 받침이 ㄹ(종성 8)이면 "로"를 씁니다. ("서울로" — "서울으로"가 아니에요)
        return without_batchim if final in (0, 8, -1) else with_batchim
    return with_batchim if final > 0 else without_batchim
