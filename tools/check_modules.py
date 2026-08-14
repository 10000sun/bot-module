"""모듈이 실제로 올라가는지 확인합니다. (디스코드에 연결하지 않아요)

봇을 켜지 않고도 "이 조합으로 납품하면 명령어가 제대로 등록되는가"를 볼 수 있어요.
슬래시 명령 동기화 규격까지 미리 검사하므로, 선택지가 0개라 동기화가 통째로
실패하는 사고 같은 걸 여기서 잡습니다.

준비 (처음 한 번):
    python -m venv .venv
    .venv\\Scripts\\python -m pip install -r mari/requirements.txt

사용:
    .venv\\Scripts\\python tools/check_modules.py                 # guild.json 그대로
    .venv\\Scripts\\python tools/check_modules.py shop birthday   # 이 조합으로만
    .venv\\Scripts\\python tools/check_modules.py --none          # 설정 없는 새 서버처럼
    .venv\\Scripts\\python tools/check_modules.py --max-names     # 이름을 상한까지 늘렸을 때

⚠️ 토큰이 없어도 됩니다. 디스코드에 접속하지 않고 코그 등록까지만 해봐요.

📏 --max-names 는 왜 있나요?
   `/초기설정`으로 받는 이름(재화·봇·이벤트·서버)이 슬래시 명령 **설명문**에 들어갑니다.
   디스코드는 설명을 100자로 제한하고, 넘으면 그 명령만 빠지는 게 아니라 **동기화 전체가
   실패**해서 명령어가 통째로 사라져요. 짧은 이름으로 테스트하면 절대 안 걸립니다.
   그래서 이름을 전부 상한 길이로 채운 채로 한 번 더 검사해요.
   (기본 실행에서도 이 검사를 별도 프로세스로 자동으로 한 번 돌립니다)
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MARI = os.path.join(os.path.dirname(HERE), "mari")
sys.path.insert(0, MARI)

DESCRIPTION_LIMIT = 100  # 디스코드 슬래시 명령 설명 길이 제한


def _max_name_length():
    """mari_names.MAX_NAME_LENGTH 를 import 없이 읽어옵니다.

    (import하면 mari_config가 먼저 딸려 오면서 데이터 폴더가 정해져 버려요.
     이 값은 그보다 먼저 알아야 임시 설정 파일을 만들 수 있습니다)
    """
    source = open(os.path.join(MARI, "mari_names.py"), encoding="utf-8").read()
    match = re.search(r"^MAX_NAME_LENGTH\s*=\s*(\d+)", source, re.M)
    return int(match.group(1)) if match else 12


def _prepare_config(argv):
    """명령줄 인자에 맞춰 임시 guild.json / settings.json을 만들고 경로를 환경변수에 꽂습니다."""
    argv = list(argv)
    max_names = "--max-names" in argv
    if max_names:
        argv.remove("--max-names")

    if not argv:
        label = "guild.json 그대로"
    elif argv == ["--none"]:
        os.environ["MARI_GUILD_CONFIG"] = os.path.join(tempfile.gettempdir(), "no-such-guild.json")
        label = "설정 파일 없음 (새로 납품한 서버처럼)"
    else:
        real = os.path.join(MARI, "guild.json")
        data = {}
        if os.path.exists(real):
            with open(real, "r", encoding="utf-8") as f:
                data = json.load(f)
        data["modules"] = argv
        path = os.path.join(tempfile.gettempdir(), "check_modules_guild.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.environ["MARI_GUILD_CONFIG"] = path
        label = f"주문 모듈: {', '.join(argv)}"

    if max_names:
        # 이름 네 개를 전부 상한 길이로 채운 데이터 폴더를 따로 씁니다.
        # (진짜 data/ 를 건드리면 실제 설정이 날아가요)
        n = _max_name_length()
        data_dir = os.path.join(tempfile.gettempdir(), "check_modules_maxnames")
        os.makedirs(data_dir, exist_ok=True)
        filler = {"currency": "재", "bot": "봇", "event": "행", "server": "터"}
        with open(os.path.join(data_dir, "mari_settings.json"), "w", encoding="utf-8") as f:
            json.dump(
                {"roles": {}, "channels": {},
                 "names": {k: c * n for k, c in filler.items()}},
                f, ensure_ascii=False, indent=2,
            )
        os.environ["MARI_DATA_DIR"] = data_dir
        label += f"  +  이름을 상한({n}자)까지 늘린 상태"

    return label, max_names


def _walk(commands, prefix=""):
    """그룹 안쪽 하위 명령까지 훑습니다."""
    for command in commands:
        yield prefix + command.name, command
        for name, child in _walk(getattr(command, "commands", []), prefix + command.name + " "):
            yield name, child


async def main(label, max_names):
    import mari_config as cfg
    from mari_client import MariBotClient

    bot = MariBotClient(command_prefix="/", intents=cfg.intents)
    await bot.load_modules()

    commands = bot.tree.get_commands()
    bot._enforce_guild_only()

    # 동기화 때 디스코드로 나가는 payload를 미리 만들어 봅니다.
    # 여기서 터지면 선택지 개수·이름 길이 같은 게 규격에 안 맞는 거예요.
    payloads = [c.to_dict(bot.tree) for c in commands]

    # 📏 설명 길이. to_dict()는 이걸 검사하지 않아서 여기서 직접 봅니다.
    too_long = [
        (name, len(desc))
        for name, command in _walk(commands)
        if (desc := getattr(command, "description", "") or "") and len(desc) > DESCRIPTION_LIMIT
    ]

    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    print(f"  올라간 모듈 : {len(bot.loaded_modules)}개 — {', '.join(bot.loaded_modules)}")
    print(f"  최상위 명령 : {len(commands)}개")
    print(f"  동기화 규격 : payload {len(payloads)}개 생성 성공")
    print(f"  상시 버튼   : {len(bot.persistent_views)}개")

    longest = max(
        ((len(getattr(c, "description", "") or ""), n) for n, c in _walk(commands)),
        default=(0, "-"),
    )
    print(f"  설명 최대   : {longest[0]}자 / {DESCRIPTION_LIMIT}자 (/{longest[1]})")

    if too_long:
        print(f"\n  🚨 설명이 {DESCRIPTION_LIMIT}자를 넘는 명령 {len(too_long)}개:")
        for name, length in too_long:
            print(f"     - /{name}: {length}자")
        print("     이대로 켜면 명령어 동기화가 통째로 실패해서 명령이 전부 사라져요.")
        print("     설명에서 이름을 한 번만 쓰거나 문구를 줄이세요.")

    if bot.failed_modules:
        print(f"\n  🚨 실패한 모듈 {len(bot.failed_modules)}개:")
        for key, reason in bot.failed_modules:
            print(f"     - {key}: {reason}")

    await bot.close()

    failed = bool(bot.failed_modules or too_long)

    # 기본 실행이면 "이름을 상한까지 늘린" 검사도 자동으로 한 번 더 돌립니다.
    # (이름은 import 시점에 설명문으로 굳기 때문에 같은 프로세스에서 두 번 볼 수 없어요)
    if not max_names:
        sys.stdout.flush()  # 자식 프로세스 출력과 순서가 뒤섞이지 않게
        result = subprocess.run(
            [sys.executable, os.path.abspath(__file__), *sys.argv[1:], "--max-names"],
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        failed = failed or result.returncode != 0

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(*_prepare_config(sys.argv[1:]))))
