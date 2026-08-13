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

⚠️ 토큰이 없어도 됩니다. 디스코드에 접속하지 않고 코그 등록까지만 해봐요.
"""

import asyncio
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MARI = os.path.join(os.path.dirname(HERE), "mari")
sys.path.insert(0, MARI)


def _prepare_config(argv):
    """명령줄 인자에 맞춰 임시 guild.json을 만들고 그 경로를 환경변수에 꽂습니다."""
    if not argv:
        return "guild.json 그대로"

    if argv == ["--none"]:
        os.environ["MARI_GUILD_CONFIG"] = os.path.join(tempfile.gettempdir(), "no-such-guild.json")
        return "설정 파일 없음 (새로 납품한 서버처럼)"

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
    return f"주문 모듈: {', '.join(argv)}"


async def main(label):
    import mari_config as cfg
    from mari_client import MariBotClient

    bot = MariBotClient(command_prefix="/", intents=cfg.intents)
    await bot.load_modules()

    commands = bot.tree.get_commands()
    bot._enforce_guild_only()

    # 동기화 때 디스코드로 나가는 payload를 미리 만들어 봅니다.
    # 여기서 터지면 선택지 개수·이름 길이 같은 게 규격에 안 맞는 거예요.
    payloads = [c.to_dict(bot.tree) for c in commands]

    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    print(f"  올라간 모듈 : {len(bot.loaded_modules)}개 — {', '.join(bot.loaded_modules)}")
    print(f"  최상위 명령 : {len(commands)}개")
    print(f"  동기화 규격 : payload {len(payloads)}개 생성 성공")
    print(f"  상시 버튼   : {len(bot.persistent_views)}개")
    if bot.failed_modules:
        print(f"\n  🚨 실패한 모듈 {len(bot.failed_modules)}개:")
        for key, reason in bot.failed_modules:
            print(f"     - {key}: {reason}")

    await bot.close()
    return 1 if bot.failed_modules else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_prepare_config(sys.argv[1:]))))
