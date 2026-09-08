"""돈을 만지는 계층이 약속을 지키는지 확인합니다. (디스코드에 연결하지 않아요)

`check_modules`·`check_help`·`check_guild`는 **배선**을 봅니다 — 코그가 올라오는지,
명령이 등록되는지, 설정을 제대로 읽는지. 그런데 정작 **잔액이 맞게 계산되는지,
저장이 실패했을 때 돈이 사라지지 않는지**는 아무도 안 보고 있었어요.

이 봇은 유저 지갑·상점·주식을 다룹니다. 잔고가 한 번 틀어지면 클라이언트가 바로
알아채고, 그 신뢰는 되돌리기 어려워요. 여기서 지키는 약속들 —

  · 저장이 실패하면 **반드시 예외를 던진다** (조용히 성공한 척하면 안 됨)
  · 원장 기록은 **절대 예외를 던지지 않는다** (이미 돈이 오간 뒤라서)
  · 파일이 깨졌으면 **읽지 말고 멈춘다** (빈 값으로 덮어쓰면 전액 소실)
  · 저장 도중 죽어도 **원본이 반쯤 쓰인 상태로 남지 않는다**

사용:
    .venv\\Scripts\\python tools/check_money.py

문제가 있으면 종료 코드 1로 끝납니다.

⚠️ 진짜 `chunsik/data/`는 절대 안 건드려요. 매번 임시 폴더를 새로 씁니다.

## 여기서 **안 보는** 것 (과신하지 마세요)

  · **동시성.** `economy_lock`이 실제로 이중 지급을 막는지는 검사하지 않아요.
    명령 핸들러가 interaction을 받아야 돌아가서, 디스코드 없이는 재현이 어렵습니다.
  · **명령 흐름 전체.** `/송금`·`/출석`·상점 구매가 처음부터 끝까지 맞게 도는지는
    안 봐요. 여기서 보는 건 그것들이 딛고 선 **바닥**입니다.
  · **주식 평가액·수수료 계산** 같은 도메인 산수.

바닥이 튼튼하다는 것과 위층이 맞다는 건 다른 얘기예요. 돈 관련 코드를 고쳤다면
이 도구를 돌리고, **그다음 실제 서버에서 한 번 해보세요.**
"""

import io
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CHUNSIK = os.path.join(os.path.dirname(HERE), "chunsik")
sys.path.insert(0, CHUNSIK)

# 🇰🇷 한국어 윈도우 콘솔(cp949)에서 이모지를 찍으면 UnicodeEncodeError로 죽습니다.
#    납품 절차에서 클라이언트가 직접 돌리는 도구라 콘솔을 고르게 할 수 없어요.
#    아래 chunsik 모듈들은 import되는 순간 이모지를 찍으므로 반드시 그보다 먼저 불러야 합니다.
from chunsik_console import force_utf8_console  # noqa: E402  (경로를 먼저 꽂아야 해서)

force_utf8_console()

# 설정을 읽기 전에 임시 데이터 폴더를 꽂아둡니다. (import 시점에 굳어요)
os.environ["CHUNSIK_GUILD_CONFIG"] = os.path.join(tempfile.gettempdir(), "no-such-guild.json")
os.environ["CHUNSIK_DATA_DIR"] = tempfile.mkdtemp()

_fails = []


def check(label, got, want):
    if got == want:
        print(f"  OK  {label}")
        return
    print(f"  !!  {label}")
    print(f"        기대: {want!r}")
    print(f"        실제: {got!r}")
    _fails.append(label)


def check_raises(label, fn, exc):
    try:
        fn()
    except exc:
        print(f"  OK  {label}")
        return
    except Exception as e:
        print(f"  !!  {label} — 다른 예외가 났어요: {type(e).__name__}: {e}")
        _fails.append(label)
        return
    print(f"  !!  {label} — 예외가 안 났어요 (조용히 성공한 척했습니다)")
    _fails.append(label)


def tmp(name="x.json"):
    return os.path.join(tempfile.mkdtemp(), name)


# ============================================================
def test_storage():
    """저장 계층 — 여기가 무너지면 아래 전부가 무의미해요."""
    from chunsik_storage import (DataSaveError, atomic_json_save,
                              atomic_json_save_or_raise, safe_json_load)

    print("\n[1] 저장하고 다시 읽기")
    p = tmp()
    data = {"123": 1000, "456": 0, "한글키": {"중첩": [1, 2, 3]}}
    check("저장 성공", atomic_json_save(p, data), True)
    check("읽은 값이 같음", safe_json_load(p, None), data)
    check("파일이 UTF-8 (한글이 \\uXXXX로 안 깨짐)",
          "한글키" in io.open(p, encoding="utf-8").read(), True)

    print("\n[2] 파일이 없을 때는 기본값")
    check("없으면 default", safe_json_load(tmp("nope.json"), {"기본": True}), {"기본": True})

    print("\n[3] 🚨 파일이 깨졌으면 읽지 말고 멈춰야 해요")
    p = tmp()
    io.open(p, "w", encoding="utf-8").write("{이건 JSON이 아님")
    # 여기서 조용히 기본값을 돌려주면, 부르는 쪽이 그 빈 값을 저장해서
    # **전 유저 잔고가 0으로 덮어써집니다.** 반드시 예외여야 해요.
    check_raises("깨진 파일은 RuntimeError", lambda: safe_json_load(p, {}), RuntimeError)
    backups = [f for f in os.listdir(os.path.dirname(p)) if ".corrupt_" in f]
    check("원본을 .corrupt_ 로 백업해둠", len(backups), 1)

    # 🚨 손상은 저절로 낫지 않아요. 그 파일을 읽는 모든 길(1분 tick·명령·다시 그리기)이
    #    계속 여기로 옵니다. 읽을 때마다 사본이 새로 생기면 하루에 수백 개가 쌓여서
    #    데이터 폴더가 파묻히고 어느 게 원본인지 알아보기 어려워져요.
    # 이름을 "지금 시각"으로 만들면 초가 바뀌는 순간부터 사본이 계속 늘어나요.
    # 그래서 1초 이상 벌려서도 읽어봅니다. (같은 초 안에서만 재보면 옛 코드도 통과해요)
    for _ in range(20):
        try:
            safe_json_load(p, {})
        except RuntimeError:
            pass
    time.sleep(1.1)
    try:
        safe_json_load(p, {})
    except RuntimeError:
        pass
    backups = [f for f in os.listdir(os.path.dirname(p)) if ".corrupt_" in f]
    check("여러 번, 시간을 벌려서 읽어도 사본은 그대로 1개", len(backups), 1)

    # 파일이 **또 바뀌어서** 다시 깨진 건 새 사고예요. 그건 따로 남아야 합니다.
    time.sleep(1.1)     # 수정 시각이 실제로 달라지도록 (초 단위 이름이라)
    io.open(p, "w", encoding="utf-8").write("{또 깨진 내용")
    try:
        safe_json_load(p, {})
    except RuntimeError:
        pass
    backups = [f for f in os.listdir(os.path.dirname(p)) if ".corrupt_" in f]
    check("파일이 또 깨지면 그건 따로 남김", len(backups), 2)

    print("\n[4] 🚨 저장이 실패하면 예외를 던져야 해요")
    # 폴더 경로에 저장을 시도하면 실패합니다. (조용히 넘어가면 "성공 메세지 + 잔액 그대로")
    d = tempfile.mkdtemp()
    check("atomic_json_save는 False를 돌려줌", atomic_json_save(d, {"a": 1}), False)
    check_raises("_or_raise는 DataSaveError", lambda: atomic_json_save_or_raise(d, {"a": 1}),
                 DataSaveError)

    print("\n[5] 🚨 저장에 실패해도 원본은 멀쩡해야 해요")
    p = tmp()
    atomic_json_save(p, {"잔고": 5000})
    # 직렬화할 수 없는 값을 넣어 저장을 실패시킵니다.
    try:
        atomic_json_save_or_raise(p, {"잔고": {1, 2, 3}})   # set은 JSON이 아니에요
    except DataSaveError:
        pass
    check("원본이 그대로 남음", safe_json_load(p, None), {"잔고": 5000})
    leftovers = [f for f in os.listdir(os.path.dirname(p)) if f.endswith(".tmp")]
    check("임시 파일이 안 남음", leftovers, [])


# ============================================================
def test_ledger():
    """원장 — /지갑내역과 /지급취소가 이걸 봅니다."""
    import chunsik_config as cfg
    from chunsik_state import load_ledger, record_ledger, record_ledger_many

    print("\n[6] 원장 한 건 기록")
    entry_id = record_ledger(111, -500, 4500, "송금", "테스트", actor_id=222)
    check("기록 id를 돌려줌", bool(entry_id), True)
    e = load_ledger()["entries"][-1]
    check("유저", e["user"], "111")
    check("변동액", e["delta"], -500)
    check("변동 후 잔액", e["balance"], 4500)
    check("종류", e["kind"], "송금")
    check("행위자", e["actor"], "222")
    check("아직 안 되돌림", e["reverted"], False)

    print("\n[7] 🚨 원장 기록은 절대 예외를 던지면 안 돼요")
    # 이미 돈이 오간 뒤에 불리는 함수예요. 여기서 터지면 유저에겐 "실패"로 보이는데
    # 잔액은 이미 바뀐, 제일 나쁜 상태가 됩니다.
    saved = cfg.LEDGER_FILE
    try:
        import chunsik_state
        chunsik_state.LEDGER_FILE = tempfile.mkdtemp()   # 폴더 → 저장 실패
        got = record_ledger(111, -1, 0, "실패해야함")
        check("저장이 실패해도 조용히 넘어감", got, "")
    finally:
        chunsik_state.LEDGER_FILE = saved

    print("\n[8] 일괄 지급은 batch로 묶임")
    batch = record_ledger_many([(1, 100, 100), (2, 100, 100), (3, 100, 100)],
                               "지급", "이벤트 보상", actor_id=9)
    rows = [x for x in load_ledger()["entries"] if x.get("batch") == batch]
    check("세 명이 같은 batch", len(rows), 3)
    check("되돌리기 단위가 됨 (batch id 존재)", bool(batch), True)

    print("\n[9] 원장이 무한정 커지지 않음")
    from chunsik_state import LEDGER_MAX_ENTRIES
    check("상한이 정해져 있음", LEDGER_MAX_ENTRIES > 0, True)
    n = load_ledger()
    n["entries"] = [dict(id=str(i), user="1", delta=1, balance=1, kind="k",
                         detail="", actor=None, batch=None, reverted=False)
                    for i in range(LEDGER_MAX_ENTRIES + 50)]
    from chunsik_storage import atomic_json_save
    atomic_json_save(cfg.LEDGER_FILE, n)
    record_ledger(1, 1, 1, "넘침")
    check(f"{LEDGER_MAX_ENTRIES}건으로 잘림",
          len(load_ledger()["entries"]), LEDGER_MAX_ENTRIES)


# ============================================================
def test_wallet():
    """지갑 — 잔액을 읽고 만드는 자리."""
    from cogs.economy import ChunsikEconomy
    from chunsik_storage import DataSaveError, atomic_json_save, safe_json_load
    import chunsik_config as cfg

    eco = ChunsikEconomy.__new__(ChunsikEconomy)   # 생성자(루프 시작)를 거치지 않아요

    print("\n[10] 지갑 읽기·자동 생성")
    atomic_json_save(cfg.ECONOMY_FILE, {})
    check("없던 유저는 0원으로 생김", eco._get_or_create_balance(777), 0)
    check("파일에도 실제로 남음", safe_json_load(cfg.ECONOMY_FILE, {}).get("777"), 0)

    atomic_json_save(cfg.ECONOMY_FILE, {"777": 12345})
    check("있는 유저는 그 값을 그대로", eco._get_or_create_balance(777), 12345)
    check("키는 문자열로 다뤄짐 (int로 넣어도 같은 지갑)",
          eco._get_or_create_balance("777"), 12345)

    print("\n[11] 🚨 지갑 저장이 실패하면 예외를 던져야 해요")
    # 조용히 넘어가면 "송금했어요!" 라고 답해놓고 잔액은 그대로인 상태가 됩니다.
    saved = cfg.ECONOMY_FILE
    try:
        import cogs.economy as ec
        ec.ECONOMY_FILE = tempfile.mkdtemp()
        check_raises("_save_raw_economy는 DataSaveError",
                     lambda: eco._save_raw_economy({"1": 1}), DataSaveError)
    finally:
        import cogs.economy as ec
        ec.ECONOMY_FILE = saved

    print("\n[12] 🚨 지갑 파일이 깨졌으면 0원으로 덮어쓰지 말 것")
    io.open(cfg.ECONOMY_FILE, "w", encoding="utf-8").write("{깨짐")
    # 여기서 예외가 안 나고 {} 가 돌아오면, 다음 저장에 전 유저 잔고가 날아갑니다.
    check_raises("깨진 지갑 파일은 읽다가 멈춤",
                 lambda: eco._get_or_create_balance(777), RuntimeError)

    print("\n[13] 출석 데이터는 빠진 항목을 채워서 돌려줌")
    atomic_json_save(cfg.ATTENDANCE_FILE, {})   # init_json_files가 만드는 빈 상태
    a = eco._load_attendance()
    for key, want in (("today_count", 0), ("today_users", []),
                      ("reward_amount", 200), ("user_stats", {})):
        check(f"{key} 기본값", a[key], want)

    print("\n[14] 🚨 자정 루프를 놓쳐도 다음 날 출석이 막히면 안 돼요")
    # 오늘 명단을 지우는 건 자정 00:00 루프뿐이었어요. 그 시각에 봇이 꺼져 있으면
    # (재시작·PC 종료·정전) 그 회차는 그냥 지나가고, tasks.loop은 따라잡지 않습니다.
    # 그러면 어제 출석한 사람 전원이 하루 종일 "이미 출석하셨어요"를 봅니다.
    from cogs.economy import roll_attendance_day

    yesterday = {"date": "2026-09-07", "today_count": 3, "today_users": ["1", "2", "3"],
                 "user_stats": {"1": 10}}
    rolled = roll_attendance_day(yesterday, "2026-09-08")
    check("날짜가 지났으면 명단을 비움", (rolled, yesterday["today_users"], yesterday["today_count"]),
          (True, [], 0))
    check("누적 기록은 건드리지 않음", yesterday["user_stats"], {"1": 10})

    same = {"date": "2026-09-08", "today_count": 2, "today_users": ["1", "2"]}
    check("같은 날이면 그대로", (roll_attendance_day(same, "2026-09-08"), same["today_users"]),
          (False, ["1", "2"]))

    # 📌 날짜 칸이 없는 옛 파일을 비워버리면, 오늘 이미 출석한 사람이 한 번 더 받아요.
    #    재화가 복사되는 쪽으로는 기울이지 않습니다.
    legacy = {"today_count": 2, "today_users": ["1", "2"]}
    check("옛 파일은 비우지 않고 날짜만 박음",
          (roll_attendance_day(legacy, "2026-09-08"), legacy["today_users"], legacy["date"]),
          (False, ["1", "2"], "2026-09-08"))

    # 실제 로더도 같은 일을 하는지. (규칙만 맞고 로더가 안 부르면 소용없어요)
    atomic_json_save(cfg.ATTENDANCE_FILE,
                     {"date": "2000-01-01", "today_count": 5, "today_users": ["9"],
                      "reward_amount": 200, "user_stats": {}})
    check("_load_attendance가 실제로 넘겨줌", eco._load_attendance()["today_users"], [])


# ============================================================
def test_loader_shapes():
    """데이터 로더가 **빈 파일에서도 약속한 모양**을 돌려주는지."""
    import chunsik_config as cfg
    import chunsik_state as st
    from chunsik_storage import atomic_json_save

    print("\n[15] 🚨 빈 파일에서도 약속한 모양이 나와야 해요")
    # 로더에 적어둔 기본값은 **파일이 아예 없을 때만** 쓰입니다. 그런데 init_json_files가
    # 봇이 뜰 때 데이터 파일을 전부 빈 {} 로 미리 만들어둬요. 그래서 새로 설치한 환경에서는
    # 그 기본값이 한 번도 안 쓰이고 언제나 {} 가 넘어옵니다.
    #
    # 지금 부르는 쪽이 .get()으로 조심하고 있어도, 나중에 누가 load_selfroles()["panels"]
    # 라고 쓰면 **개발 PC에서는 멀쩡하고 새로 납품한 서버에서만** 죽어요.
    # (출석 데이터가 실제로 그 사고를 냈습니다)
    cases = [
        (cfg.PARTY_FILE, st.load_party, {"parties": dict}),
        (cfg.SCRIM_FILE, st.load_scrim, {"matches": dict, "records": dict}),
        (cfg.LEVELS_FILE, st.load_levels, {"users": dict, "rewards": dict, "config": dict}),
        (cfg.WELCOME_FILE, st.load_welcome, {"auto_roles": list, "message": str}),
        (cfg.SELFROLE_FILE, st.load_selfroles, {"panels": dict}),
        (cfg.WIKI_FILE, st.load_wiki, {"wiki": dict}),
        (cfg.LEDGER_FILE, st.load_ledger, {"entries": list}),
    ]
    for path, loader, shape in cases:
        name = os.path.basename(path)
        for label, written in (("빈 파일", {}), ("칸 타입이 어긋남", {k: 0 for k in shape})):
            atomic_json_save(path, written)
            try:
                data = loader()
                missing = [k for k, t in shape.items() if not isinstance(data.get(k), t)]
            except Exception as e:
                missing = [f"{type(e).__name__}: {e}"]
            check(f"{name} ({label})", missing, [])


# ============================================================
if __name__ == "__main__":
    test_storage()
    test_ledger()
    test_wallet()
    test_loader_shapes()
    print()
    if _fails:
        print(f"🚨 {len(_fails)}건 실패: {', '.join(_fails)}")
        sys.exit(1)
    print("✅ 돈 계층 검사 전부 통과")
    sys.exit(0)
