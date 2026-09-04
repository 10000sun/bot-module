"""모듈이 실제로 올라가는지 확인합니다. (디스코드에 연결하지 않아요)

봇을 켜지 않고도 "이 조합으로 납품하면 명령어가 제대로 등록되는가"를 볼 수 있어요.
슬래시 명령 동기화 규격까지 미리 검사하므로, 선택지가 0개라 동기화가 통째로
실패하는 사고 같은 걸 여기서 잡습니다.

준비 (처음 한 번):
    python -m venv .venv
    .venv\\Scripts\\python -m pip install -r chunsik/requirements.txt

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

import ast
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHUNSIK = os.path.join(os.path.dirname(HERE), "chunsik")
sys.path.insert(0, CHUNSIK)

# 🇰🇷 한국어 윈도우 콘솔(cp949)에서 이모지를 찍으면 UnicodeEncodeError로 죽습니다.
#    납품 절차에서 클라이언트가 직접 돌리는 도구라 콘솔을 고르게 할 수 없어요.
#    아래 chunsik 모듈들은 import되는 순간 이모지를 찍으므로 반드시 그보다 먼저 불러야 합니다.
from chunsik_console import force_utf8_console  # noqa: E402  (경로를 먼저 꽂아야 해서)

force_utf8_console()

DESCRIPTION_LIMIT = 100  # 디스코드 슬래시 명령 설명 길이 제한


def _max_name_length():
    """chunsik_names.MAX_NAME_LENGTH 를 import 없이 읽어옵니다.

    (import하면 chunsik_config가 먼저 딸려 오면서 데이터 폴더가 정해져 버려요.
     이 값은 그보다 먼저 알아야 임시 설정 파일을 만들 수 있습니다)
    """
    source = open(os.path.join(CHUNSIK, "chunsik_names.py"), encoding="utf-8").read()
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
        os.environ["CHUNSIK_GUILD_CONFIG"] = os.path.join(tempfile.gettempdir(), "no-such-guild.json")
        label = "설정 파일 없음 (새로 납품한 서버처럼)"
    else:
        real = os.path.join(CHUNSIK, "guild.json")
        data = {}
        if os.path.exists(real):
            # 🚨 여기서 그냥 터지면 클라이언트는 raw 트레이스백만 봅니다. 납품 절차에서
            #    이 도구를 돌리라고 안내하니(README 7번), 이유를 그대로 알려줘야 해요.
            #    봇도 이 경우 기동을 막습니다. (chunsik_config.GUILD_CONFIG_FATAL)
            try:
                # utf-8-sig — 봇과 같은 방식으로 읽어야 해요. (chunsik_config.load_guild_config)
                with open(real, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
            except Exception as e:
                print(f"\n🚨 {real} 을 읽을 수 없어요.\n   {type(e).__name__}: {e}\n")
                print("   JSON 문법이 깨져 있어요. 고치기 전까지는 봇도 기동하지 않습니다.")
                print("   (문법 검사: https://jsonlint.com)\n")
                sys.exit(1)
            if not isinstance(data, dict):
                print(f"\n🚨 {real} 의 최상위가 객체({{...}})가 아니에요.\n")
                sys.exit(1)
        data["modules"] = argv
        path = os.path.join(tempfile.gettempdir(), "check_modules_guild.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.environ["CHUNSIK_GUILD_CONFIG"] = path
        label = f"주문 모듈: {', '.join(argv)}"

    if max_names:
        # 이름 네 개를 전부 상한 길이로 채운 데이터 폴더를 따로 씁니다.
        # (진짜 data/ 를 건드리면 실제 설정이 날아가요)
        n = _max_name_length()
        data_dir = os.path.join(tempfile.gettempdir(), "check_modules_maxnames")
        os.makedirs(data_dir, exist_ok=True)
        filler = {"currency": "재", "bot": "봇", "event": "행", "server": "터"}
        with open(os.path.join(data_dir, "chunsik_settings.json"), "w", encoding="utf-8") as f:
            json.dump(
                {"roles": {}, "channels": {},
                 "names": {k: c * n for k, c in filler.items()}},
                f, ensure_ascii=False, indent=2,
            )
        os.environ["CHUNSIK_DATA_DIR"] = data_dir
        label += f"  +  이름을 상한({n}자)까지 늘린 상태"

    return label, max_names


def _walk(commands, prefix=""):
    """그룹 안쪽 하위 명령까지 훑습니다."""
    for command in commands:
        yield prefix + command.name, command
        for name, child in _walk(getattr(command, "commands", []), prefix + command.name + " "):
            yield name, child


# 🤝 어느 모듈의 것도 아닌 게 **맞는** 데이터 파일. 여기 없는데 소유자도 없으면
# modules.py에 등록을 깜빡한 것으로 보고 알려줍니다.
SHARED_DATA_FILES = {"SETTINGS_FILE"}


def _log_keys_in_code() -> set:
    """send_log_embed / build_log_embed 에 실제로 넘기는 채널 키를 소스에서 뽑습니다.

    부르는 자리가 코그 곳곳에 흩어져 있어서, 실행해봐야 아는 게 아니라 소스를 읽는 쪽이
    빠짐없고 확실해요. 첫 번째 위치 인자가 채널 키인 호출만 봅니다.
    """
    keys = set()
    for folder in (CHUNSIK, os.path.join(CHUNSIK, "cogs")):
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(folder, name), "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                called = ast.unparse(node.func)
                if not called.endswith(("send_log_embed", "build_log_embed")):
                    continue
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        keys.add(arg.value)
    # 채널 키만 남깁니다. (설명문 같은 다른 문자열 인자가 섞이니까요)
    return {k for k in keys if k.endswith(("_log", "_announce", "_board"))}


def _check_ownership(bot) -> list:
    """modules.py의 소유 표가 실제 코드와 어긋나지 않았는지 봅니다.

    소유 표(어느 모듈이 어떤 채널·역할·기능키·데이터파일을 데려오는지)는 담지 않은
    기능의 설정 명령과 빈 JSON이 따라오는 걸 막는 장치예요. 그런데 새 채널이나 새
    데이터 파일을 만들고 여기 등록하는 걸 깜빡하면 **아무 일도 안 일어납니다** —
    등록 안 된 키는 '공용'으로 보고 항상 켜두거든요. (그게 안전한 기본값이라 일부러
    그렇게 했어요. 빠뜨렸을 때 기능이 사라지는 것보다 안 지워지는 게 덜 위험하니까)

    조용히 어긋나는 게 문제라 여기서 대조합니다. modules.py의 owners() 설명이
    "빠뜨린 건 check_modules.py가 찾아줘요"라고 약속하고 있기도 하고요.

    돌려주는 건 사람이 읽을 문제 목록이에요. 비어 있으면 통과.
    """
    import chunsik_config as cfg
    import chunsik_settings as cs
    from modules import MODULES, owners
    from chunsik_settings import _ALL_FEATURE_KEYS

    problems = []
    known_modules = set(MODULES)

    def check_module_keys(where, keys):
        unknown = sorted(k for k in keys if k not in known_modules)
        if unknown:
            problems.append(f"{where}: modules.py에 없는 모듈 키 {unknown}")

    # ① 데이터 파일 — 소유자가 없는데 공용 목록에도 없으면 등록을 깜빡한 거예요.
    owned_files = owners("data_files")
    all_files = set(cfg.json_data_files())
    orphan = sorted(all_files - set(owned_files) - SHARED_DATA_FILES)
    if orphan:
        problems.append(
            f"소유 모듈이 없는 데이터 파일 {orphan} — modules.py의 data_files에 넣거나, "
            f"정말 공용이면 check_modules.py의 SHARED_DATA_FILES에 넣으세요")
    phantom = sorted(set(owned_files) - all_files)
    if phantom:
        problems.append(f"modules.py가 가리키는데 chunsik_config에 없는 상수 {phantom}")

    # ② 설정 명령이 다루는 채널·역할 키 — setting.py의 표가 실제로 설정 가능한 목록이에요.
    setting_cog = bot.get_cog("ChunsikSetting")
    if setting_cog is not None:
        for kind, table in (("channels", setting_cog._CHANNEL_COMMANDS),
                            ("roles", setting_cog._ROLE_COMMANDS)):
            missing = sorted(set(table.values()) - set(owners(kind)))
            if missing:
                problems.append(
                    f"설정 명령은 있는데 modules.py의 {kind}에 없는 키 {missing} — "
                    f"그 기능을 빼도 설정 명령이 남습니다")

    # ③ 기능 킬 스위치 키 — 양쪽 방향을 다 봅니다.
    feature_labels = set(_ALL_FEATURE_KEYS.values())
    owned_features = set(owners("features"))
    missing = sorted(feature_labels - owned_features)
    if missing:
        problems.append(
            f"modules.py의 features에 없는 기능 키 {missing} — "
            f"그 기능을 빼도 /기능제어 선택지에 남습니다")
    # 🐛 반대 방향이 비어 있었어요. modules.py는 선언했는데 _ALL_FEATURE_KEYS에 없으면
    #    `/기능제어` 선택지에 아예 안 떠서 **그 기능만 끌 수 없습니다.** 코드가 게이트를
    #    걸어둬도 켜고 끌 방법이 없으니 게이트가 없는 것과 같아요. (내전이 그 상태였습니다)
    unswitchable = sorted(owned_features - feature_labels)
    if unswitchable:
        problems.append(
            f"modules.py는 선언했는데 chunsik_settings._ALL_FEATURE_KEYS에 없는 기능 키 "
            f"{unswitchable} — /기능제어로 끌 수가 없습니다")

    # ④ 로그 스타일 — 로그를 보내면서 스타일 표에 없으면 회색 "📋 로그"로 뭉뚱그려 나와요.
    #    오류가 안 나는 종류라 아무도 모른 채 지나갑니다. (실제로 두 개가 그 상태였어요)
    used_log_keys = _log_keys_in_code()
    styleless = sorted(used_log_keys - set(cs.LOG_STYLES))
    if styleless:
        problems.append(
            f"로그를 보내는데 chunsik_settings.LOG_STYLES에 없는 키 {styleless} — "
            f"그 로그는 회색 '📋 로그'로 뭉뚱그려 나옵니다")

    # ⑤ 다른 파일이 적어둔 모듈 키에 오타가 없는지
    diag_cog = bot.get_cog("ChunsikTest")
    if diag_cog is not None:
        check_module_keys("cogs/diagnostics.py의 _MODULE_COMMANDS", diag_cog._MODULE_COMMANDS.values())

    help_cog = bot.get_cog("ChunsikHelp")
    if help_cog is not None:
        tags = set()

        def collect(owner):
            if owner is None:
                return
            if isinstance(owner, str):
                tags.add(owner)
                return
            for child in owner:
                collect(child)

        for entries in (help_cog._admin_entries(), help_cog._user_entries()):
            for _emoji, lines in entries.values():
                for owner, _text in lines:
                    collect(owner)
        check_module_keys("cogs/help.py의 도움말 태그", tags)

    for spec in MODULES.values():
        check_module_keys(f"modules.py의 '{spec.key}'.requires", spec.requires)

    return problems


def _check_loop_guards():
    """모든 백그라운드 루프에 @루프이름.error 핸들러가 붙어 있는지 봅니다.

    🚨 discord.py의 tasks.loop은 예외가 밖으로 새어나가면 **영구히 멈춥니다.** 자동
    재시작이 없고, 화면에도 아무것도 안 떠요. 그래서 파일이 한 번 손상된 순간부터
    파티 알림이나 경험치 저장이 조용히 죽은 채로 며칠이 지나갈 수 있습니다.
    (실제로 레벨·파티·내전 세 코그가 이걸 빠뜨린 채 나갔어요)

    handler에서 chunsik_alerts.report_loop_error를 부르면 관리자에게 알리고 루프를
    되살립니다. 담은 모듈과 무관한 정적 검사라 소스만 읽어요.
    """
    problems = []
    files = [os.path.join(CHUNSIK, f) for f in sorted(os.listdir(CHUNSIK)) if f.endswith(".py")]
    cogs_dir = os.path.join(CHUNSIK, "cogs")
    files += [os.path.join(cogs_dir, f) for f in sorted(os.listdir(cogs_dir)) if f.endswith(".py")]

    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        loops, guarded = [], set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for dec in node.decorator_list:
                text = ast.unparse(dec)
                if text.startswith("tasks.loop"):
                    loops.append(node.name)
                elif re.fullmatch(r"\w+\.error", text):
                    guarded.add(text.split(".")[0])
        rel = os.path.relpath(path, os.path.dirname(HERE))
        for name in loops:
            if name not in guarded:
                problems.append(f"{rel}: {name} 루프에 @{name}.error 핸들러가 없어요 "
                                f"(한 번 터지면 그 기능이 조용히 영영 멈춥니다)")
    return problems


# 🔢 코드·문서에 적어도 되는 자리표시자 ID.
#
# 안내문에 "이렇게 생긴 숫자를 넣으세요"를 보여주려면 예시가 하나는 있어야 해요.
# 여기 없는 17~20자리 숫자가 나오면 **원본 서버의 진짜 ID**로 보고 잡습니다.
PLACEHOLDER_IDS = {
    "123456789012345678",   # guild.example.json·README의 표준 예시
    "234567890123456789",   # 두 번째 예시가 필요한 자리 (ranks 표)
    "700000000000000000",   # check_embeds.py가 만들어 쓰는 가짜 유저 번호
    "800000000000000000",
}

_ID_RE = re.compile(r"\b\d{17,20}\b")


def _check_delivery_ids():
    """납품물에 **원본 서버의 진짜 ID**가 남아 있는지 봅니다.

    🚚 이 레포는 통째로 클라이언트에게 넘어갑니다. 이 프로젝트의 출발점이 "서버 고유 ID를
       코드에서 걷어내 guild.json으로 분리"였는데, 주석이나 창고 파일(parked/)에 예시로
       적어둔 진짜 역할 ID는 그 정리에서 두 번 빠져나갔어요. 실제로 두 개가 남아 있었습니다.

    비밀값은 아니에요 — 역할 ID는 그 서버 사람이면 다 볼 수 있습니다. 다만 납품물에
    남의 서버 ID가 섞여 있는 건 그 자체로 사고고, 나중에 그 값을 진짜로 쓰는 코드가
    생기면 엉뚱한 서버를 가리킵니다.

    깃이 **추적하는** 파일만 봅니다. 로컬의 guild.json·data/는 원래 안 나가니까요.
    """
    problems = []
    root = os.path.dirname(HERE)
    listed = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True,
                            encoding="utf-8")
    if listed.returncode != 0:
        return []      # 깃 저장소가 아니면 볼 게 없어요. 검사를 실패로 만들진 않습니다.

    for rel in listed.stdout.split("\n"):
        if not rel.strip():
            continue
        try:
            with open(os.path.join(root, rel), "r", encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue   # 이미지 같은 건 넘어갑니다
        for number, line in enumerate(text.split("\n"), 1):
            for found in _ID_RE.findall(line):
                if found in PLACEHOLDER_IDS:
                    continue
                problems.append(f"{rel}:{number}: 진짜 디스코드 ID로 보이는 값 {found} "
                                f"— 자리표시자로 바꾸거나 문장에서 빼주세요")
    return problems


async def main(label, max_names):
    import chunsik_config as cfg
    from chunsik_client import ChunsikBotClient

    bot = ChunsikBotClient(command_prefix="/", intents=cfg.intents)
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

    # 🧩 소유 표가 코드와 어긋나지 않았는지. (담은 모듈과 무관한 정적 검사예요)
    ownership = _check_ownership(bot)
    print(f"  소유 표     : {'✅ 코드와 일치' if not ownership else f'🚨 {len(ownership)}건 어긋남'}")
    if ownership:
        for problem in ownership:
            print(f"     - {problem}")

    # 🔁 백그라운드 루프가 한 번 터지고 조용히 멈추지 않는지. (이것도 정적 검사예요)
    loop_guards = _check_loop_guards()
    print(f"  루프 안전망 : {'✅ 전부 있음' if not loop_guards else f'🚨 {len(loop_guards)}건 없음'}")
    if loop_guards:
        for problem in loop_guards:
            print(f"     - {problem}")

    # 🚚 납품물에 원본 서버의 진짜 ID가 섞여 있지 않은지. (이것도 정적 검사예요)
    delivery = _check_delivery_ids()
    print(f"  납품물 ID   : {'✅ 자리표시자만 있음' if not delivery else f'🚨 {len(delivery)}건 남음'}")
    if delivery:
        for problem in delivery:
            print(f"     - {problem}")

    if bot.failed_modules:
        print(f"\n  🚨 실패한 모듈 {len(bot.failed_modules)}개:")
        for key, reason in bot.failed_modules:
            print(f"     - {key}: {reason}")

    await bot.close()

    failed = bool(bot.failed_modules or too_long or ownership or loop_guards or delivery)

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
