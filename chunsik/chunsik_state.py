"""봇이 메모리에 들고 있는 공용 데이터(아이디 DB 등)와 재화 입출금 원장."""

import uuid
import datetime as dt

from chunsik_config import (IDS_FILE, KST, LEDGER_FILE, LEVELS_FILE, PARTY_FILE, SCRIM_FILE,
                         SELFROLE_FILE, WELCOME_FILE, WIKI_FILE)
from chunsik_storage import atomic_json_save, atomic_json_save_or_raise, safe_json_load

# ========== 💾 데이터 로드/세이브 유틸리티 ==========
def load_ids():
    return safe_json_load(IDS_FILE, {})

def save_ids(data):
    atomic_json_save_or_raise(IDS_FILE, data, indent=2)

# 🧩 [버그 방지] 로더가 "이런 모양을 돌려준다"고 적어둔 기본값은 **파일이 아예 없을 때만**
#    쓰입니다. 그런데 `chunsik_storage.init_json_files()`가 봇이 뜰 때 데이터 파일을 전부
#    빈 `{}` 로 미리 만들어둬요. 그래서 **새로 설치한 환경에서는 그 기본값이 한 번도
#    안 쓰이고 언제나 `{}` 가 넘어옵니다.**
#
#    지금은 부르는 쪽이 전부 `.get()`·`setdefault()`로 조심하고 있어서 안 터져요. 하지만
#    그건 **주석에도 없는 약속**이라, 나중에 누가 `load_selfroles()["panels"]` 라고 쓰면
#    개발 PC(파일에 내용이 있음)에서는 멀쩡하고 **새로 납품한 서버에서만** 죽습니다.
#    실제로 출석 데이터가 그 사고를 냈었어요(cogs/economy.py의 `_load_attendance` 참고).
#
#    그래서 로더가 약속한 모양을 **직접 채워서** 돌려줍니다. 이제 기본값이 진짜예요.
#    (`tools/check_money.py` [15]가 빈 파일로 확인합니다)
def _shaped(data, shape: dict) -> dict:
    """읽어온 값을 약속한 모양으로 채워서 돌려줍니다. dict가 아니면 통째로 새로 만들어요."""
    if not isinstance(data, dict):
        data = {}
    for key, default in shape.items():
        value = data.get(key)
        if not isinstance(value, type(default)):
            data[key] = type(default)()
    return data


def load_party():
    return _shaped(safe_json_load(PARTY_FILE, {}), {"parties": {}})

def save_party(data):
    atomic_json_save_or_raise(PARTY_FILE, data, indent=2)

def load_scrim():
    return _shaped(safe_json_load(SCRIM_FILE, {}), {"matches": {}, "records": {}})

def save_scrim(data):
    atomic_json_save_or_raise(SCRIM_FILE, data, indent=2)

def load_levels():
    return _shaped(safe_json_load(LEVELS_FILE, {}), {"users": {}, "rewards": {}, "config": {}})

def save_levels(data):
    atomic_json_save_or_raise(LEVELS_FILE, data, indent=2)

def load_welcome():
    return _shaped(safe_json_load(WELCOME_FILE, {}), {"auto_roles": [], "message": ""})

def save_welcome(data):
    atomic_json_save_or_raise(WELCOME_FILE, data, indent=2)

def load_selfroles():
    return _shaped(safe_json_load(SELFROLE_FILE, {}), {"panels": {}})

def save_selfroles(data):
    atomic_json_save_or_raise(SELFROLE_FILE, data, indent=2)

def load_wiki():
    # 📚 위키는 `{"wiki": {...}}` 모양이에요. 부르는 쪽이 전부 `.get("wiki", {})`로 조심하고
    #    있었지만, 여기서 채워두면 그 조심이 없어도 안전합니다.
    return _shaped(safe_json_load(WIKI_FILE, {}), {"wiki": {}})

def save_wiki(data):
    atomic_json_save_or_raise(WIKI_FILE, data, indent=2)


# ========== 🧾 [신규] 재화 입출금 원장 ==========
# 유저의 "내 재화 왜 줄었어요?" 문의에 관리자가 로그 채널을 뒤지지 않아도 되도록,
# 모든 잔액 변동을 파일에 남깁니다. /지갑 내역 조회와 /지급 되돌리기가 이걸 씁니다.
LEDGER_MAX_ENTRIES = 3000  # 파일이 무한정 커지지 않도록 오래된 것부터 잘라냅니다


def load_ledger() -> dict:
    # 🧩 `setdefault`만 쓰면 **칸이 있는데 타입이 틀린** 경우를 못 고쳐요.
    #    (`{"entries": 0}` 이면 그대로 0이 넘어가고, 다음 append에서 터집니다)
    #    돈 기록이라 특히 조용히 죽으면 안 되는 자리예요. 위 _shaped를 씁니다.
    return _shaped(safe_json_load(LEDGER_FILE, {}), {"entries": []})


def record_ledger(user_id, delta: int, balance_after, kind: str,
                  detail: str = "", actor_id=None, batch_id: str = None) -> str:
    """재화 변동 한 건을 원장에 기록하고 기록 id를 돌려줍니다.

    🚨 이 함수는 절대 예외를 밖으로 던지지 않아요.
    이미 돈이 오간 뒤에 호출되기 때문에, 여기서 실패가 터지면 유저에게는
    "거래 실패"로 보이는데 실제 잔액은 바뀐 채로 남는 최악의 상황이 됩니다.
    기록이 못 남는 건 아쉬운 일이지만, 거래 자체를 깨뜨리는 것보다는 낫습니다.
    """
    try:
        data = load_ledger()
        entry = {
            "id": uuid.uuid4().hex[:10],
            "ts": dt.datetime.now(KST).isoformat(timespec="seconds"),
            "user": str(user_id),
            "delta": int(delta),
            "balance": int(balance_after) if balance_after is not None else None,
            "kind": kind,
            "detail": detail,
            "actor": str(actor_id) if actor_id else None,
            "batch": batch_id,
            "reverted": False,
        }
        data["entries"].append(entry)
        if len(data["entries"]) > LEDGER_MAX_ENTRIES:
            data["entries"] = data["entries"][-LEDGER_MAX_ENTRIES:]
        # ⚠️ 여기는 _or_raise를 쓰지 않아요 (위 설명 참고)
        atomic_json_save(LEDGER_FILE, data, indent=2)
        return entry["id"]
    except Exception as e:
        print(f"⚠️ 원장 기록 실패 (거래 자체는 정상 처리됨): {type(e).__name__}: {e}")
        return ""


def record_ledger_many(rows: list, kind: str, detail: str = "", actor_id=None) -> str:
    """여러 명에게 한꺼번에 지급/회수한 건을 하나의 batch로 묶어 기록합니다.

    rows: [(user_id, delta, balance_after), ...]
    되돌리기는 이 batch 단위로 이뤄져요.

    🧮 줄마다 `batch_size`(이 묶음의 **원래 인원**)를 같이 적습니다.
       원장은 LEDGER_MAX_ENTRIES에서 오래된 것부터 잘리는데, 500명짜리 `/지급` 한 건은
       500줄이에요. 그 뒤로 거래가 쌓이면 묶음의 **앞부분만 잘려나갑니다.** 그러면
       `/지급취소`가 살아남은 사람만 되돌리는데, 화면에는 "대상 N명"이라고만 떠서
       관리자는 **반쪽인 줄을 모릅니다.** 원래 인원을 적어두면 그걸 알릴 수 있어요.
       (예전 기록에는 이 값이 없어요 — 없으면 "모름"으로 보고 아무 말도 안 합니다)
    """
    batch_id = uuid.uuid4().hex[:10]
    try:
        data = load_ledger()
        now = dt.datetime.now(KST).isoformat(timespec="seconds")
        batch_size = len(rows)
        for user_id, delta, balance_after in rows:
            data["entries"].append({
                "id": uuid.uuid4().hex[:10],
                "ts": now,
                "user": str(user_id),
                "delta": int(delta),
                "balance": int(balance_after) if balance_after is not None else None,
                "kind": kind,
                "detail": detail,
                "actor": str(actor_id) if actor_id else None,
                "batch": batch_id,
                "batch_size": batch_size,
                "reverted": False,
            })
        if len(data["entries"]) > LEDGER_MAX_ENTRIES:
            data["entries"] = data["entries"][-LEDGER_MAX_ENTRIES:]
        atomic_json_save(LEDGER_FILE, data, indent=2)
    except Exception as e:
        print(f"⚠️ 원장 일괄 기록 실패 (거래 자체는 정상 처리됨): {type(e).__name__}: {e}")
    return batch_id

# ========== 🗃️ 아이디 DB 공용 상태 ==========
class ChunsikState:
    """ids.json을 메모리에 들고 있는 공용 상태 객체.

    🔑 [설계] 예전엔 `user_ids`가 모듈 전역 딕셔너리였어요. 그런데 각 코그가
    `from chunsik_state import user_ids`로 **자기 모듈에 별도 이름을 묶어두기** 때문에,
    누군가 `chunsik_state.user_ids = 새딕셔너리`로 재할당하면 이미 import된 코그들은
    계속 옛 딕셔너리를 보게 됐어요. 에러도 안 나고 조용히 어긋나는 종류의 버그라,
    새로고침 코드가 `.clear()/.update()`를 반드시 써야 한다는 "주석으로만 존재하는 규칙"에
    의존하고 있었습니다.

    이제 코그들은 딕셔너리가 아니라 이 객체(`state`) 하나를 공유해요. 그래서
    `state.user_ids`를 통째로 갈아끼워도 모두가 같은 객체를 거쳐 보므로 어긋날 수 없습니다.
    """

    def __init__(self):
        self._raw = {}          # ids.json 원본 (economy 등 우리가 안 건드리는 키 보존용)
        self.user_ids = {}      # {guild_id(str): {user_id(str): {platform: id}}}
        self.loaded = False

    def load(self):
        """ids.json을 읽어 메모리 상태를 교체합니다.

        ⚠️ import 시점이 아니라 main.py에서 명시적으로 호출해요.
        예전엔 모듈을 import하는 순간 로드돼서, 파일이 손상되면 import 도중에 예외가 터졌어요.
        그러면 봇이 로그인도 못 하고 죽는데 다운 알림 웹훅조차 못 보내는 상태가 됩니다.
        """
        self._raw = load_ids()
        self.user_ids = self._raw.get("user_ids", {})
        self.loaded = True

        # 🗑️ [정리] chunsik_access(레거시 개별 허용 역할 목록)는 삭제했습니다.
        # 이 목록을 추가/삭제하는 명령어가 코드 어디에도 없어 사실상 죽은 기능이었고,
        # 지금은 `/설정 관리자 아이디`로 지정하는 ids_admin 역할이 같은 역할을 하고 있습니다.
        legacy = self._raw.get("chunsik_access")
        if legacy:
            print(f"ℹ️ [안내] ids.json에 남아있던 레거시 chunsik_access 데이터는 더 이상 사용되지 않습니다: {legacy}")

    def save(self):
        # 원본을 기반으로 병합 저장 (economy 등 다른 키 유실 방지)
        data = dict(self._raw)
        data["user_ids"] = self.user_ids
        data["economy"] = self._raw.get("economy", {})  # 경제 데이터 누락 방지
        # 🗑️ 더 이상 사용하지 않는 레거시 필드 정리
        #  • chunsik_access    : 위 load() 설명 참고
        #  • log_channels   : 로그 채널 지정은 settings.json의 channels.*로 완전히 이관됐어요
        #  • current_numbers: 하이로우 게임의 임시 숫자. ChunsikGames가 메모리에서만 들고 있어요
        for dead_key in ("chunsik_access", "log_channels", "current_numbers"):
            data.pop(dead_key, None)
        save_ids(data)


# 코그들이 공유하는 단 하나의 인스턴스. (로드는 main.py에서 state.load()로)
state = ChunsikState()
