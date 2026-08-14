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

⚠️ 진짜 `mari/data/`는 절대 안 건드려요. 매번 임시 폴더를 새로 씁니다.

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
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MARI = os.path.join(os.path.dirname(HERE), "mari")
sys.path.insert(0, MARI)

# 설정을 읽기 전에 임시 데이터 폴더를 꽂아둡니다. (import 시점에 굳어요)
os.environ["MARI_GUILD_CONFIG"] = os.path.join(tempfile.gettempdir(), "no-such-guild.json")
os.environ["MARI_DATA_DIR"] = tempfile.mkdtemp()

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
    from mari_storage import (DataSaveError, atomic_json_save,
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
    import mari_config as cfg
    from mari_state import load_ledger, record_ledger, record_ledger_many

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
        import mari_state
        mari_state.LEDGER_FILE = tempfile.mkdtemp()   # 폴더 → 저장 실패
        got = record_ledger(111, -1, 0, "실패해야함")
        check("저장이 실패해도 조용히 넘어감", got, "")
    finally:
        mari_state.LEDGER_FILE = saved

    print("\n[8] 일괄 지급은 batch로 묶임")
    batch = record_ledger_many([(1, 100, 100), (2, 100, 100), (3, 100, 100)],
                               "지급", "이벤트 보상", actor_id=9)
    rows = [x for x in load_ledger()["entries"] if x.get("batch") == batch]
    check("세 명이 같은 batch", len(rows), 3)
    check("되돌리기 단위가 됨 (batch id 존재)", bool(batch), True)

    print("\n[9] 원장이 무한정 커지지 않음")
    from mari_state import LEDGER_MAX_ENTRIES
    check("상한이 정해져 있음", LEDGER_MAX_ENTRIES > 0, True)
    n = load_ledger()
    n["entries"] = [dict(id=str(i), user="1", delta=1, balance=1, kind="k",
                         detail="", actor=None, batch=None, reverted=False)
                    for i in range(LEDGER_MAX_ENTRIES + 50)]
    from mari_storage import atomic_json_save
    atomic_json_save(cfg.LEDGER_FILE, n)
    record_ledger(1, 1, 1, "넘침")
    check(f"{LEDGER_MAX_ENTRIES}건으로 잘림",
          len(load_ledger()["entries"]), LEDGER_MAX_ENTRIES)


# ============================================================
def test_wallet():
    """지갑 — 잔액을 읽고 만드는 자리."""
    from cogs.economy import MariEconomy
    from mari_storage import DataSaveError, atomic_json_save, safe_json_load
    import mari_config as cfg

    eco = MariEconomy.__new__(MariEconomy)   # 생성자(루프 시작)를 거치지 않아요

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


# ============================================================
if __name__ == "__main__":
    test_storage()
    test_ledger()
    test_wallet()
    print()
    if _fails:
        print(f"🚨 {len(_fails)}건 실패: {', '.join(_fails)}")
        sys.exit(1)
    print("✅ 돈 계층 검사 전부 통과")
    sys.exit(0)
