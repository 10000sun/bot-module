"""모듈 레지스트리 — 이 봇에 어떤 기능을 담을지 고르는 곳.

주문제작마다 담는 기능이 달라서, 예전처럼 mari_client.py에 import 17줄과 add_cog 17줄을
손으로 적어두면 기능 하나 빼고 넣을 때마다 클라이언트 본체를 고쳐야 했어요. 이제 여기
목록만 보고 로드하며, 무엇을 켤지는 guild.json의 modules 항목이 정합니다.

다른 마리 모듈을 import하지 않습니다. (mari_config보다도 아래, 순수 자료 계층이에요)
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleSpec:
    """모듈 하나의 신원. 키는 guild.json에 적는 이름이라 함부로 바꾸면 안 돼요."""

    key: str            # guild.json의 modules에 적는 이름
    label: str          # 사람에게 보여줄 이름 (기동 로그·안내문)
    module: str         # import 경로
    cog: str            # 그 안의 Cog 클래스 이름
    core: bool = False  # True면 끌 수 없어요 (봇 운영 자체에 필요)
    requires: tuple = ()  # 이게 없으면 기능이 성립하지 않는 모듈들
    note: str = ""


# ⚠️ 여기 적힌 **순서가 곧 로드 순서**입니다. 원본 마리봇의 add_cog 순서를 그대로 옮겨왔어요.
# 로드 순서가 동작을 바꾸는 부분은 지금 없지만(코그끼리는 전부 런타임에 get_cog로 찾습니다),
# 명령어가 디스코드에 등록되는 순서가 달라지므로 이유 없이 흔들지 않는 게 좋아요.
MODULE_SPECS = (
    ModuleSpec(
        "setting", "설정·권한·기능제어", "cogs.setting", "MariSetting", core=True,
        note="관리자 역할/로그 채널 지정, 기능 킬 스위치, 역할부여. 이게 없으면 다른 모듈의 권한 설정을 못 해요.",
    ),
    ModuleSpec(
        "id", "게임 아이디 등록부", "cogs.core", "MariCore",
        note="파일 이름이 cogs/core.py라 '핵심'처럼 보이지만 실제로는 게임 아이디 등록/조회 기능이에요. "
             "(파일명 정리는 별도 작업으로 남겨뒀습니다)",
    ),
    ModuleSpec(
        "economy", "지갑·송금·출석", "cogs.economy", "MariEconomy",
        note="서버 재화의 뿌리. 돈을 만지는 모듈은 전부 이걸 필요로 해요.",
    ),
    ModuleSpec("stock", "모의 주식", "cogs.stock", "MariStock", requires=("economy",)),
    ModuleSpec("wiki", "멤버 위키", "cogs.wiki", "MariWiki"),
    ModuleSpec("games", "미니게임·보상", "cogs.games", "MariGames", requires=("economy",)),
    ModuleSpec(
        "gpt", "AI 대화", "cogs.gpt", "MariGPT",
        note="GEMINI_API_KEY가 없으면 대화 기능만 조용히 꺼집니다. 지갑·주식 조회 도구는 "
             "해당 모듈이 함께 켜져 있을 때만 실제로 동작해요.",
    ),
    ModuleSpec("shop", "상점·인벤토리·선물", "cogs.shop", "MariShop", requires=("economy",)),
    ModuleSpec("help", "도움말", "cogs.help", "MariHelp", core=True),
    ModuleSpec("birthday", "생일 등록·축하", "cogs.birthday", "MariBirthday"),
    ModuleSpec(
        "diagnostics", "진단·데이터 점검", "cogs.diagnostics", "MariTest", core=True,
        note="켜져 있는 모듈만 골라서 점검해요. 문제가 생겼을 때 원인을 찾는 유일한 창구라 항상 포함합니다.",
    ),
    ModuleSpec(
        "backup", "자동 백업", "cogs.backup", "MariBackup", core=True,
        note="매일 데이터 폴더를 통째로 백업해요. 어떤 기능을 담든 데이터는 지켜야 하므로 항상 포함합니다.",
    ),
    ModuleSpec("chronicle", "서버 연대기", "cogs.chronicle", "MariChronicle"),
    ModuleSpec("snooze", "나중에 답장", "cogs.snooze", "MariSnooze"),
)

MODULES = {spec.key: spec for spec in MODULE_SPECS}

CORE_KEYS = tuple(spec.key for spec in MODULE_SPECS if spec.core)
OPTIONAL_KEYS = tuple(spec.key for spec in MODULE_SPECS if not spec.core)


def resolve_modules(requested):
    """켤 모듈을 확정합니다.

    requested — guild.json의 modules 목록. None이면 "전부"로 봅니다.
                (설정을 안 건드린 기존 배포가 그대로 돌아가야 하니까요)

    반환: (로드 순서대로 정렬된 ModuleSpec 목록, 사람이 읽을 경고 목록)
    """
    warnings = []

    if requested is None:
        return list(MODULE_SPECS), warnings

    wanted = set()
    for key in requested:
        name = str(key).strip()
        if name in MODULES:
            wanted.add(name)
        else:
            warnings.append(
                f"'{name}'이라는 모듈은 없어요. 무시합니다. "
                f"(쓸 수 있는 이름: {', '.join(OPTIONAL_KEYS)})"
            )

    # 코어 모듈은 목록에 없어도 항상 들어갑니다.
    forced_core = sorted(set(CORE_KEYS) - wanted)
    if forced_core:
        warnings.append(
            f"{', '.join(forced_core)} 모듈은 봇 운영에 필요해서 목록에 없어도 항상 켜집니다."
        )
    wanted |= set(CORE_KEYS)

    # 의존 모듈을 끌어옵니다. 예: 상점만 골랐다면 지갑(economy)이 함께 켜져야 해요.
    #
    # 💡 여기서 "빼고 경고"가 아니라 "넣고 경고"를 택한 이유: 상점은 지갑 없이는 물건값을
    # 치를 수가 없어서, 의존 모듈을 빼면 주문받은 기능이 통째로 죽은 채 납품됩니다.
    # 반대로 넣으면 주문에 없던 /지갑 명령이 몇 개 늘 뿐이고, 그건 기동 로그에 남습니다.
    while True:
        missing = {
            need
            for key in wanted
            for need in MODULES[key].requires
            if need not in wanted
        }
        if not missing:
            break
        for need in sorted(missing):
            dependents = sorted(k for k in wanted if need in MODULES[k].requires)
            warnings.append(
                f"'{MODULES[need].label}'({need}) 모듈을 함께 켰어요. "
                f"{', '.join(dependents)}이(가) 이 모듈 없이는 동작하지 않아요."
            )
        wanted |= missing

    # 선언 순서(=로드 순서)를 유지해서 돌려줍니다.
    return [spec for spec in MODULE_SPECS if spec.key in wanted], warnings
