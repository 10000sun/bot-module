"""판매 라인(에센셜·스탠다드·프리미엄)으로 납품해도 되는지 한 번에 검사합니다.

라인별 조합은 `chunsik/modules.py`의 TIER_SPECS 한 곳에만 적혀 있어요. 납품할 때마다
modules 목록을 손으로 적으면 조합이 조금씩 어긋나고, 나중에 "이 서버는 어느 라인으로
판 건가"를 아무도 모르게 됩니다. 그래서 코드에 박아두고 이 도구가 대조해요.

사용:
    python tools/check_tiers.py             # 세 라인 전부
    python tools/check_tiers.py standard    # 이 라인만

보는 것 —
  [1] 라인 정의 — 모르는 모듈 키, 중복, 어느 라인에도 안 실린 모듈
  [2] 라인 안의 의존 관계 — 상점만 넣고 지갑을 빠뜨리면 지갑이 자동으로 딸려가서
      주문서에 없던 `/지갑`이 납품됩니다. 그건 라인 정의에서 미리 맞춰야 해요
  [3] 사다리 — 아래 라인이 위 라인에 통째로 들어 있는지
      ("스탠다드엔 있는데 프리미엄엔 없는 기능"이 생기면 환불 사유예요)
  [4] guild.example.json의 `_티어프리셋`이 코드와 같은지 (클라이언트가 복사하는 값)
  [5] 라인마다 check_modules.py · check_help.py 실행

check_guild.py·check_money.py는 모듈 조합과 상관없어서 여기서 안 돌립니다.
납품 전에 따로 한 번씩 돌리세요. (README 7번)

문제가 있으면 종료 코드 1로 끝납니다.
"""

import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHUNSIK = os.path.join(ROOT, "chunsik")
sys.path.insert(0, CHUNSIK)

import modules as mod  # noqa: E402  (경로를 먼저 꽂아야 해서)

EXAMPLE = os.path.join(CHUNSIK, "guild.example.json")

# 🇰🇷 한국어 윈도우 콘솔(cp949)에서 이모지를 찍으면 UnicodeEncodeError로 죽습니다.
#    납품 절차에서 클라이언트가 직접 돌리는 도구라 콘솔을 고르게 할 수 없어요.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}

_fails = []


def fail(msg):
    _fails.append(msg)
    print(f"  🚨 {msg}")


def check_definitions():
    """[1][2][3] 라인 정의 자체를 봅니다. 파이썬만으로 끝나는 검사예요."""
    print("\n[1] 라인 정의")
    seen = {}
    for spec in mod.TIER_SPECS:
        for key in spec.adds:
            if key not in mod.MODULES:
                fail(f"{spec.label}: '{key}'라는 모듈은 없어요")
            elif key in mod.CORE_KEYS:
                fail(f"{spec.label}: '{key}'는 항상 켜지는 코어라 라인에 적을 필요가 없어요")
            elif key in seen:
                fail(f"{spec.label}: '{key}'가 {seen[key]}에도 있어요 (adds에는 더해지는 것만)")
            else:
                seen[key] = spec.label

    unsold = [k for k in mod.OPTIONAL_KEYS if k not in seen]
    if unsold:
        fail(f"어느 라인에도 안 실린 모듈: {', '.join(unsold)} — 팔 라인을 정해 adds에 넣으세요")
    if not _fails:
        print(f"  OK  선택 모듈 {len(mod.OPTIONAL_KEYS)}개가 라인 {len(mod.TIER_SPECS)}개에 빠짐없이 실림")

    print("\n[2] 라인 안의 의존 관계")
    for spec in mod.TIER_SPECS:
        wanted = mod.tier_modules(spec.key)
        resolved, _ = mod.resolve_modules(wanted)
        extra = [s.key for s in resolved if s.key not in wanted and s.key not in mod.CORE_KEYS]
        if extra:
            fail(f"{spec.label}: {', '.join(extra)}이(가) 자동으로 딸려와요 — adds에 직접 넣으세요")
        else:
            print(f"  OK  {spec.label} — 주문서 밖 모듈이 안 따라옴")

    print("\n[3] 사다리 (아래 라인 ⊂ 위 라인)")
    lower = None
    for spec in mod.TIER_SPECS:
        current = set(mod.tier_modules(spec.key))
        if lower is not None:
            dropped = sorted(lower[1] - current)
            if dropped:
                fail(f"{spec.label}에 {lower[0]}의 {', '.join(dropped)}이(가) 빠졌어요")
                lower = (spec.label, current)
                continue
        print(f"  OK  {spec.label} — {len(current)}개")
        lower = (spec.label, current)


def check_example():
    """[4] 클라이언트가 실제로 복사해 가는 값이 코드와 같은지."""
    print("\n[4] guild.example.json의 _티어프리셋")
    try:
        data = json.load(io.open(EXAMPLE, encoding="utf-8-sig"))
    except Exception as e:
        fail(f"{os.path.basename(EXAMPLE)}을 읽을 수 없어요 — {type(e).__name__}: {e}")
        return

    presets = data.get("_티어프리셋")
    if not isinstance(presets, dict):
        fail("_티어프리셋 항목이 없어요 (클라이언트가 복사할 값이라 예시 파일에 있어야 합니다)")
        return

    for spec in mod.TIER_SPECS:
        want = mod.tier_modules(spec.key)
        got = presets.get(spec.key)
        if got == want:
            print(f"  OK  {spec.key} — {', '.join(want)}")
        else:
            fail(f"{spec.key}: 예시 파일과 코드가 달라요\n"
                 f"        예시: {got}\n"
                 f"        코드: {want}")

    stray = sorted(set(presets) - set(mod.TIERS))
    if stray:
        fail(f"코드에 없는 라인이 예시 파일에 남아 있어요: {', '.join(stray)}")


def run_checks(keys):
    """[5] 라인마다 기존 검사 도구를 그 조합으로 돌립니다."""
    print("\n[5] 라인별 검사")
    for key in keys:
        spec = mod.TIERS[key]
        mods = mod.tier_modules(key)
        print(f"\n  ── {spec.label} ({key}) — {spec.pitch}")
        print(f"     modules: {', '.join(mods)}")
        for tool in ("check_modules.py", "check_help.py"):
            proc = subprocess.run(
                [sys.executable, os.path.join(HERE, tool), *mods],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=CHILD_ENV, cwd=ROOT,
            )
            if proc.returncode == 0:
                print(f"     ✅ {tool}")
                # 개수는 견적서에 그대로 쓰는 값이라 성공했을 때도 보여줍니다.
                # (check_modules.py는 이름을 상한까지 늘려 한 번 더 돌아서 같은 줄이
                #  두 번 나와요. 중복은 접습니다)
                if tool == "check_modules.py":
                    shown = []
                    for line in proc.stdout.splitlines():
                        line = line.strip()
                        if re.match(r"(올라간 모듈|최상위 명령)\s*:", line) and line not in shown:
                            shown.append(line)
                            print(f"        {line}")
            else:
                fail(f"{spec.label}: {tool} 실패 (종료 코드 {proc.returncode})")
                print((proc.stdout or "") + (proc.stderr or ""))


def main(argv):
    if argv:
        unknown = [a for a in argv if a not in mod.TIERS]
        if unknown:
            print(f"🚨 모르는 라인이에요: {', '.join(unknown)}")
            print(f"   가능: {', '.join(mod.TIERS)}")
            return 1
        keys = argv
    else:
        keys = list(mod.TIERS)

    print("=" * 60)
    print(f"판매 라인 검사 — {', '.join(mod.TIERS[k].label for k in keys)}")
    print("=" * 60)

    check_definitions()
    check_example()
    run_checks(keys)

    print("\n" + "=" * 60)
    if _fails:
        print(f"🚨 {len(_fails)}건 — 위 내역을 보세요")
        print("=" * 60)
        return 1
    print(f"✅ 라인 {len(keys)}개 전부 통과 — 이 조합 그대로 납품해도 됩니다")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
