"""도움말이 안내하는 명령이 실제로 등록되는지 대조합니다. (디스코드에 연결하지 않아요)

기능을 창고로 보낼 때마다 `cogs/help.py`의 안내문을 같이 고쳐야 하는데, 이게 계속
빠졌어요. 그러면 유저가 `/도움말`을 눌러 **존재하지 않는 명령**을 안내받게 됩니다.
(실제로 캠프·명단·프로필을 창고로 보낸 뒤 그 상태로 한 커밋이 지나갔습니다)

사람이 눈으로 대조할 일이 아니라서 여기 자동화해뒀어요.

사용:
    .venv\\Scripts\\python tools/check_help.py                # 아래 조합 전부
    .venv\\Scripts\\python tools/check_help.py shop birthday  # 이 조합만

없는 명령을 안내하고 있으면 종료 코드 1로 끝납니다.

**여러 모듈 조합으로 검사합니다.** 예전엔 전부 켠 상태로만 봤어요. `cogs/help.py`의
안내문이 어떤 모듈이 올라왔는지 보지 않는 고정 문자열이라, 조합별로 돌리면 "이 조합엔
없는 명령"이 잔뜩 나와서 진짜 오타가 묻혔거든요. 지금은 도움말이 모듈 구성을 보고
스스로 걸러내므로(`MariHelp._visible`) **어느 조합에서든 0개가 정상**입니다.

조합을 섞어 보는 게 중요해요. 전부 켠 상태만 보면 "안 담긴 기능의 명령을 안내한다"는,
납품할 때마다 실제로 터지던 문제를 영영 못 잡습니다.

⚠️ 반대 방향(실제로 있는데 도움말에 없는 명령)도 검사하지 않아요. 연대기처럼 **일부러**
   안 싣는 명령이 있어서, 그건 사람이 판단할 일입니다.

📌 백틱은 "지금 칠 수 있는 명령"이라는 뜻으로 씁니다. 없어진 명령을 설명에 언급할 때는
   백틱을 씌우지 마세요 — 여기서 안내 중인 명령으로 잡히기도 하지만, 그보다 유저가
   실제로 쳐볼 수 있는 명령으로 오해합니다. (`/랭킹`을 그렇게 적어둬서 실제로 걸렸어요)
"""

import asyncio
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MARI = os.path.join(os.path.dirname(HERE), "mari")
sys.path.insert(0, MARI)

# 🧩 검사할 조합들. 납품에서 실제로 나올 법한 모양으로 골랐어요.
# (None은 "설정 파일 없음" = 전부 켜짐. 코어만 담은 구성은 유저용 카테고리가 통째로
#  비는 유일한 경우라 꼭 넣어야 합니다 — 예전에 여기서 /도움말이 터질 뻔했어요)
COMBOS = [
    (None, "전부 (설정 없음)"),
    (["shop", "birthday"], "shop + birthday"),
    (["id"], "id 만"),
    (["stock"], "stock 만 (economy 자동 포함)"),
    (["gpt"], "gpt 만"),
    (["wiki", "snooze"], "wiki + snooze"),
    (["diagnostics"], "코어 모듈만"),
]


def _use_config(mods):
    """이 조합으로 guild.json을 임시로 만들고 경로를 환경변수에 꽂습니다.

    ⚠️ 진짜 guild.json / data 폴더는 안 건드려요. 실제 설정이 날아가면 안 되니까요.
    """
    if mods is None:
        # 없는 경로를 가리키면 "설정 파일 없음"이 되고, 그때는 전부 켜집니다.
        path = os.path.join(tempfile.gettempdir(), "no-such-guild.json")
    else:
        path = os.path.join(tempfile.mkdtemp(), "guild.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"modules": mods}, f)
    os.environ["MARI_GUILD_CONFIG"] = path
    os.environ["MARI_DATA_DIR"] = tempfile.mkdtemp()
    # 설정은 import 시점에 한 번 읽고 굳어요. 조합마다 모듈을 다시 들여야 합니다.
    for name in [m for m in sys.modules if m.startswith(("mari", "cogs")) or m == "modules"]:
        del sys.modules[name]


def _registered_command_paths(bot) -> set:
    """등록된 명령을 "아이디 등록" 같은 경로 문자열 집합으로 모읍니다."""
    from discord import app_commands

    paths = set()

    def walk(cmd, prefix=""):
        name = f"{prefix}{cmd.name}"
        paths.add(name)
        if isinstance(cmd, app_commands.Group):
            for sub in cmd.commands:
                walk(sub, name + " ")

    for cmd in bot.tree.get_commands():
        walk(cmd)
    return paths


def _mentioned_command_paths(help_cog) -> set:
    """도움말 본문에서 `/명령 ...` 꼴을 긁어옵니다."""
    # 📌 카테고리는 클래스 변수가 아니라 **메서드**예요. 도움말 문구에는 재화·봇·이벤트
    #    이름이 잔뜩 들어가는데, 클래스 변수로 두면 봇이 켜질 때 한 번 만들어져 굳어서
    #    `/초기설정`으로 이름을 바꿔도 도움말만 옛 이름으로 남았습니다. (cogs/help.py 참고)
    blob = "\n".join(
        content
        for categories in (help_cog._admin_categories(), help_cog._user_categories())
        for _emoji, content in categories.values()
    )

    mentioned = set()
    for text in re.findall(r"`/([^`]+)`", blob):
        # `/아이디 등록`, `/설정 채널 아이디등록`, `/지갑 전체:True` 같은 형태를 받아요.
        parts = []
        for token in text.split():
            if ":" in token:
                break        # 여기부터는 옵션이라 명령 이름이 아니에요
            parts.append(token)
        if parts:
            mentioned.add(" ".join(parts))
    return mentioned


async def check_one(mods, label) -> int:
    """조합 하나를 검사하고 없는 명령 개수를 돌려줍니다."""
    _use_config(mods)

    import mari_config as cfg
    from mari_client import MariBotClient

    bot = MariBotClient(command_prefix="/", intents=cfg.intents)
    await bot.load_modules()

    real = _registered_command_paths(bot)
    help_cog = bot.get_cog("MariHelp")
    if help_cog is None:
        print("🚨 도움말 모듈(MariHelp)이 올라오지 않았어요. 대조할 수가 없습니다.")
        await bot.close()
        return 1

    mentioned = _mentioned_command_paths(help_cog)

    # `/설정 채널 아이디등록`처럼 실제 명령(`/설정 채널`)보다 더 깊은 경로를 안내하는 경우가
    # 있어요. 옵션 값을 이름처럼 적어둔 안내문이라, 앞에서부터 하나라도 맞으면 통과로 봅니다.
    missing = []
    for name in sorted(mentioned):
        tokens = name.split()
        if any(" ".join(tokens[:i]) in real for i in range(len(tokens), 0, -1)):
            continue
        missing.append(name)

    admin = help_cog._admin_categories()
    user = help_cog._user_categories()

    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    print(f"  올라간 모듈      : {len(bot.loaded_modules)}개")
    print(f"  등록된 명령 경로 : {len(real)}개")
    print(f"  도움말이 언급    : {len(mentioned)}개")
    print(f"  카테고리         : 관리자 {len(admin)}개 / 유저 {len(user)}개")
    if missing:
        print(f"\n  🚨 도움말에만 있고 실제로는 없는 명령 {len(missing)}개:")
        for name in missing:
            print(f"     /{name}")
        print("\n  cogs/help.py에서 그 줄의 모듈 태그를 확인해주세요. (_visible 설명 참고)")
    else:
        print("  ✅ 도움말의 모든 명령이 실제로 등록됩니다.")

    await bot.close()
    return len(missing)


async def main(argv):
    combos = COMBOS if not argv else [(list(argv), f"주문 모듈: {', '.join(argv)}")]
    total = 0
    for mods, label in combos:
        total += await check_one(mods, label)

    print(f"\n{'=' * 60}")
    if total:
        print(f"🚨 조합 {len(combos)}개 중 없는 명령 안내 총 {total}건")
    else:
        print(f"✅ 조합 {len(combos)}개 전부 통과 — 어느 구성에서도 유령 명령 없음")
    print("=" * 60)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
