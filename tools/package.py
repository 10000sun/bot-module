"""납품용 폴더를 만듭니다. — 내부 문서와 창고를 빼고 복사해요.

이 레포는 **주문제작 결과물이 클라이언트에게 넘어가는 곳**입니다. 그런데 개발하면서
쓰는 문서도 같은 레포에 있어요. 레포를 통째로 넘기면 클라이언트가 그걸 그대로 봅니다.

 - `NEXT.md` · `판매문구.md` — 만드는 쪽에서만 보는 문서예요. 클라이언트에게 갈 내용이
   아닌 것들이 들어 있습니다.
레포 구조는 그대로 두고, **넘길 때만** 걸러냅니다. 무엇이 나가고 무엇이 안 나가는지가
여기 코드로 남아요.

    python tools/package.py                 # ../bot-module-납품/ 에 만듭니다
    python tools/package.py 어디에/만들지     # 자리를 직접 정하려면

🚚 무엇을 담는가 — **깃이 추적하는 파일만** 담습니다. 그래서
   `chunsik/data/`(유저 지갑·아이디)·`chunsik/guild.json`(서버 고유 ID)·`.env`(토큰)처럼
   .gitignore에 걸린 것은 **애초에 담길 수가 없어요.** 실수로 새어나가는 길을 막는 게
   목록을 손으로 적는 것보다 확실합니다.

⚠️ 이 도구는 검사를 대신하지 않아요. 납품 전에 `tools/`의 검사 여섯 개를 먼저 돌리세요.
"""

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "chunsik"))
from chunsik_console import force_utf8_console  # noqa: E402  (경로를 먼저 꽂아야 해서)

force_utf8_console()


# 🚫 담지 않을 것. (깃이 추적하고 있지만 클라이언트에게 갈 이유가 없는 것들)
#    경로는 레포 최상단 기준이고, 뒤에 `/`가 붙으면 그 아래 전부를 뜻해요.
EXCLUDE = {
    # ✍️ 이유는 일부러 뭉뚱그려 적습니다. 이 파일도 같이 넘어가는데, 여기에 무엇을 왜
    #    숨기는지 자세히 적어두면 숨긴 의미가 없어요. 경로만 봐도 충분합니다.
    "NEXT.md": "만드는 쪽에서만 보는 인계 문서",
    "판매문구.md": "만드는 쪽에서만 보는 문서",
    # 🗄️ `chunsik/parked/`는 **일부러 담습니다.** 원본 서버 전용이라 쓸모는 없지만,
    #    코드 주석 네 곳이 "이건 parked/○○.py.txt로 걷어냈어요"라고 그 파일을 가리켜요.
    #    빼면 없는 파일을 가리키는 주석만 남습니다. 실제 서버 ID가 남아 있지 않은지는
    #    check_modules의 "납품물 ID" 검사가 따로 봐요. (빼려면 여기 한 줄만 더하면 됩니다)
    # 📷 폴더째로 빼지 않아요. 설치안내문에 그림 자리가 아홉 개 심어져 있어서, 나중에
    #    그림을 넣으면 그건 클라이언트에게 가야 합니다. 판매자용 메모만 뺍니다.
    "이미지/README.md": "그림 넣는 방법을 적어둔 판매자용 메모",
}

# 🔎 담긴 파일에 이런 말이 남아 있으면 알려줍니다.
#    내부 문서를 새로 만들고 위 목록에 넣는 걸 깜빡했을 때 걸리라고 둔 그물이에요.
#    (막지는 않아요 — "원"처럼 흔한 글자가 섞이면 오탐이 나니 사람이 보고 판단합니다)
INTERNAL_WORDS = ("판매가", "원가", "마진", "견적", "입금", "계좌", "외주 단가")


def tracked_files():
    """깃이 추적하는 파일 목록. (여기 없는 건 애초에 안 나갑니다)"""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                         capture_output=True, check=True)
    return [p for p in out.stdout.decode("utf-8").split("\0") if p]


def excluded_reason(path: str):
    """이 경로를 빼야 하면 그 이유를, 담아야 하면 None."""
    for rule, reason in EXCLUDE.items():
        if rule.endswith("/"):
            if path.startswith(rule):
                return reason
        elif path == rule:
            return reason
    return None


def dangling_references(kept: list, dropped: list) -> list:
    """담긴 문서가 **빠진 것**을 가리키고 있는지.

    🔗 안 나가는 파일로 링크를 걸어두면 클라이언트는 깨진 링크를 봅니다. 게다가
       "NEXT.md 참고"라고 적혀 있으면 **없는 문서를 찾아 헤매게** 돼요.
    """
    # 📌 파일은 **적힌 경로 그대로** 찾습니다. 파일 이름만 보면 `README.md` 같은 흔한
    #    이름에서 아무 데나 걸려요. 폴더는 마지막 칸도 같이 봅니다 — 문서에는 보통
    #    `chunsik/parked/`가 아니라 `parked/`라고 적거든요.
    names = {r for r in EXCLUDE if not r.endswith("/")}
    names |= {r for r in EXCLUDE if r.endswith("/")}
    names |= {r.rstrip("/").split("/")[-1] + "/" for r in EXCLUDE if r.endswith("/")}
    problems = []
    here = os.path.relpath(os.path.abspath(__file__), ROOT).replace(os.sep, "/")
    for path in kept:
        # 🪞 규칙을 적어둔 이 파일 자신은 건너뜁니다. 여기엔 **뺄 것의 이름이 그대로**
        #    적혀 있을 수밖에 없어요(그게 EXCLUDE니까요). 그걸 "가리킨다"고 보면
        #    이 검사는 영영 통과할 수 없습니다.
        #    ⚠️ 그래서 이 파일의 머리말과 이유는 **뭉뚱그려** 적어야 해요. 경로는 남지만
        #       무엇을 왜 숨기는지까지 적으면 숨긴 의미가 없습니다.
        if path == here:
            continue
        # 🐍 `.py`도 봅니다. 주석과 독스트링에 적어둔 말도 그대로 넘어가요.
        #    (검사 도구와 코그 주석 네 곳이 실제로 여기 걸렸어요)
        if not path.endswith((".md", ".html", ".txt", ".py")):
            continue
        try:
            text = open(os.path.join(ROOT, path), encoding="utf-8").read()
        except Exception:
            continue
        for name in sorted(names):
            if name in text:
                problems.append(f"{path} 가 `{name}` 를 가리키는데 그건 안 나가요")
    return problems


def internal_words(kept: list) -> list:
    hits = []
    for path in kept:
        if not path.endswith((".md", ".html")):
            continue
        try:
            text = open(os.path.join(ROOT, path), encoding="utf-8").read()
        except Exception:
            continue
        found = sorted({w for w in INTERNAL_WORDS if w in text})
        if found:
            hits.append(f"{path} 에 {', '.join(found)}")
    return hits


def human(size: int) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:,.0f}{unit}" if unit == "B" else f"{size / 1:,.1f}{unit}"
        size /= 1024
    return f"{size}B"


def main(argv):
    dest = os.path.abspath(argv[0]) if argv else os.path.join(
        os.path.dirname(ROOT), os.path.basename(ROOT) + "-납품")

    if os.path.abspath(dest) == ROOT or ROOT.startswith(os.path.abspath(dest) + os.sep):
        print(f"🚨 만들 자리가 레포 안이거나 레포 자신이에요: {dest}")
        print("   다른 폴더를 골라주세요. (기본값은 레포 옆에 만듭니다)")
        return 1

    files = tracked_files()
    kept, dropped = [], []
    for path in files:
        reason = excluded_reason(path)
        (dropped if reason else kept).append(path)

    print(f"\n{'=' * 60}\n📦 납품용 폴더 만들기\n{'=' * 60}")
    print(f"  가져온 목록 : 깃이 추적하는 파일 {len(files)}개")
    print(f"  담을 것     : {len(kept)}개")
    print(f"  뺄 것       : {len(dropped)}개")
    for rule, reason in EXCLUDE.items():
        n = sum(1 for p in dropped if excluded_reason(p) == reason)
        print(f"     - {rule:20} {n:>2}개 — {reason}")

    # 🧹 있던 걸 지우고 새로 만듭니다. 남아 있으면 지난번 납품물이 섞여요.
    if os.path.exists(dest):
        print(f"\n  🧹 이미 있는 폴더를 비우고 다시 만듭니다: {dest}")
        shutil.rmtree(dest)

    total = 0
    for path in kept:
        src = os.path.join(ROOT, path)
        out = os.path.join(dest, path)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copy2(src, out)
        total += os.path.getsize(src)

    print(f"\n  ✅ {len(kept)}개 · 합계 {human(total)}")
    print(f"     {dest}")

    problems = dangling_references(kept, dropped)
    words = internal_words(kept)

    if problems:
        print(f"\n  🚨 안 나가는 것을 가리키는 자리 {len(problems)}건:")
        for line in problems:
            print(f"     - {line}")
        print("     그 문장을 고치거나, 그 파일을 담도록 EXCLUDE에서 빼주세요.")
    else:
        print("\n  ✅ 담긴 문서가 안 나가는 것을 가리키지 않아요.")

    if words:
        print(f"\n  ⚠️ 안쪽 이야기로 보이는 말이 담긴 파일 {len(words)}건 — 눈으로 확인해 주세요:")
        for line in words:
            print(f"     - {line}")
    else:
        print("  ✅ 안쪽 이야기로 보이는 말이 남아 있지 않아요.")

    print("\n  📋 넘기기 전에 — tools/의 검사 여섯 개가 전부 종료 코드 0인지 보세요.")
    print(f"{'=' * 60}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
