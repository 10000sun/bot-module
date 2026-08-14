"""도움말이 안내하는 명령이 실제로 등록되는지 대조합니다. (디스코드에 연결하지 않아요)

기능을 창고로 보낼 때마다 `cogs/help.py`의 안내문을 같이 고쳐야 하는데, 이게 계속
빠졌어요. 그러면 유저가 `/도움말`을 눌러 **존재하지 않는 명령**을 안내받게 됩니다.
(실제로 캠프·명단·프로필을 창고로 보낸 뒤 그 상태로 한 커밋이 지나갔습니다)

사람이 눈으로 대조할 일이 아니라서 여기 자동화해뒀어요.

사용:
    .venv\\Scripts\\python tools/check_help.py

없는 명령을 안내하고 있으면 종료 코드 1로 끝납니다.

**모듈을 전부 켠 상태로만 검사합니다.** 조합을 골라서 검사하지 않는 이유가 있어요:
`cogs/help.py`의 안내문은 지금 **어떤 모듈이 올라왔는지 보지 않는 고정 문자열**이라,
일부만 담아 납품하면 안 담은 기능의 명령까지 그대로 안내합니다. 그래서 조합별로
돌리면 "이 조합엔 없는 명령"이 잔뜩 나오는데, 그건 help.py를 고쳐야 사라지는
**별개의 문제**예요. 여기서 섞어 보고하면 진짜 오타를 못 찾습니다.

⚠️ 반대 방향(실제로 있는데 도움말에 없는 명령)도 검사하지 않아요. 연대기처럼 **일부러**
   안 싣는 명령이 있어서, 그건 사람이 판단할 일입니다.
"""

import asyncio
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MARI = os.path.join(os.path.dirname(HERE), "mari")
sys.path.insert(0, MARI)

# 설정 파일이 없는 것처럼 띄웁니다. modules 항목이 비면 전부 켜지므로(resolve_modules 참고)
# 로컬 guild.json이 어떻게 돼 있든 항상 "모듈 전부" 기준으로 검사해요.
os.environ["MARI_GUILD_CONFIG"] = os.path.join(tempfile.gettempdir(), "no-such-guild.json")


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
    blob = "\n".join(
        content
        for categories in (help_cog.ADMIN_CATEGORIES, help_cog.USER_CATEGORIES)
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


async def main():
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

    print(f"\n{'=' * 60}\n도움말 ↔ 실제 명령 대조 (모듈 전부 켠 상태)\n{'=' * 60}")
    print(f"  올라간 모듈      : {len(bot.loaded_modules)}개")
    print(f"  등록된 명령 경로 : {len(real)}개")
    print(f"  도움말이 언급    : {len(mentioned)}개")
    if missing:
        print(f"\n  🚨 도움말에만 있고 실제로는 없는 명령 {len(missing)}개:")
        for name in missing:
            print(f"     /{name}")
        print("\n  cogs/help.py의 안내문을 고쳐주세요.")
    else:
        print("\n  ✅ 도움말의 모든 명령이 실제로 등록됩니다.")

    await bot.close()
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
