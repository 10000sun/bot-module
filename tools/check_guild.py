"""`guild.json`을 코드가 제대로 읽는지 확인합니다. (디스코드에 연결하지 않아요)

보는 것 —
  · 등급 사다리(`ranks`) 파싱과 아이디 명단 칸 나누기 (가짜 멤버로)
  · 예전 키 이름을 그대로 둔 설정 파일도 동작하는지

두 번째가 특히 중요해요. `guild.json`은 `.gitignore` 대상이라 **깃이 안 건드립니다.**
실제로 봇이 돌고 있는 PC에서 `git pull`만 하면 코드는 새것, 설정은 옛것이 돼요.
그때 조용히 기능이 빠지면(예: 생일 알림 멘션) 한참 뒤에나 알아차립니다.

사용:
    .venv\\Scripts\\python tools/check_guild.py

🚨 여기서 제일 중요한 건 **아무도 명단에서 사라지지 않는다**는 검사예요.
   예전에 레벨 역할표를 걷어낸 뒤 아무 칸에도 안 걸린 사람이 명단에서 통째로 빠진
   적이 있습니다. 등급을 쓰는 서버에서도 그 역할이 없는 사람은 얼마든지 생기고,
   등급 역할을 실수로 지우면 명단이 텅 빌 수 있어요. 그래서 마지막 '미분류' 칸이
   항상 있어야 하고, 이 도구가 그걸 지킵니다.

문제가 있으면 종료 코드 1로 끝납니다.
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MARI = os.path.join(os.path.dirname(HERE), "mari")
sys.path.insert(0, MARI)

_fails = []


def check(label, got, want):
    if got == want:
        print(f"  OK  {label}")
        return
    print(f"  !!  {label}")
    print(f"        기대: {want!r}")
    print(f"        실제: {got!r}")
    _fails.append(label)


def load_config(guild_data):
    """임시 guild.json을 만들어 mari_config를 처음부터 다시 불러옵니다.

    ⚠️ 진짜 guild.json / data 폴더는 건드리지 않아요. 실제 설정이 날아가면 안 되니까요.
    """
    path = os.path.join(tempfile.mkdtemp(), "guild.json")
    if guild_data is not None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(guild_data, f, ensure_ascii=False)
    os.environ["MARI_GUILD_CONFIG"] = path
    os.environ["MARI_DATA_DIR"] = tempfile.mkdtemp()
    # 설정은 import 시점에 한 번 읽고 굳기 때문에, 구성마다 모듈을 다시 들여야 해요.
    for name in [m for m in sys.modules if m.startswith(("mari", "cogs")) or m == "modules"]:
        del sys.modules[name]
    import mari_config
    return mari_config


class _Role:
    def __init__(self, id):
        self.id = id


class _Member:
    """discord.Member 중 명단 코드가 실제로 보는 것만 흉내 냅니다."""

    def __init__(self, name, role_ids, bot=False):
        self.display_name = name
        self.roles = [_Role(i) for i in role_ids]
        self.bot = bot


def _core_with(guild_data):
    """주어진 설정으로 MariIds를 (생성자를 거치지 않고) 하나 만들어 돌려줍니다."""
    load_config(guild_data)
    from cogs.ids import MariIds
    return MariIds.__new__(MariIds)


CHIEF = 999
GOLD, SILVER = 111, 222
TWO_RANKS = {"ranks": [
    {"key": "gold", "label": "골드", "role": GOLD, "color": "노랑"},
    {"key": "silver", "label": "실버", "role": SILVER, "color": "회색"},
]}
SETTINGS = {"roles": {"chief_role": CHIEF}}


def test_parsing():
    print("\n[1] ranks 읽기 — 잘못 적은 줄은 건너뛰고 경고")
    cfg = load_config({"ranks": [
        {"key": "gold", "label": "골드", "role": GOLD, "color": "노랑"},
        {"key": "silver", "label": "실버", "role": SILVER},
        {"label": "키없음", "role": 333},
        {"key": "노역할", "label": "역할없음"},
        {"key": "gold", "label": "중복키", "role": 444},
        {"key": "이상한색", "label": "색이상", "role": 555, "color": "형광핑크"},
        "이건 객체가 아님",
    ]})
    check("유효한 등급만 남음", [r["key"] for r in cfg.RANKS],
          ["gold", "silver", "rank2", "이상한색"])
    check("색 이름이 ANSI로", cfg.RANKS[0]["color"], "2;33")
    check("key를 생략하면 순번으로", cfg.RANKS[2]["key"], "rank2")
    warns = " | ".join(cfg.GUILD_CONFIG_WARNINGS)
    check("role 없으면 경고", "ranks[3]" in warns, True)
    check("중복 key 경고", "이미 쓰였" in warns, True)
    check("모르는 색 경고", "형광핑크" in warns, True)
    check("객체가 아니면 경고", "ranks[6]" in warns, True)

    print("\n[2] ranks가 없거나 형태가 틀렸을 때")
    check("아예 없으면 빈 목록", load_config({}).RANKS, [])
    bad = load_config({"ranks": {"gold": GOLD}})
    check("목록이 아니면 빈 목록", bad.RANKS, [])
    check("그때 경고도 남김",
          any("목록([...])" in w for w in bad.GUILD_CONFIG_WARNINGS), True)


def test_sections():
    print("\n[3] 명단 칸 나누기 — 등급을 쓰는 서버")
    core = _core_with(TWO_RANKS)
    sections = core._roster_sections()
    check("칸 순서 (대장 → 등급 → 나머지)", [k for k, _t, _c in sections],
          ["chief", "rank:gold", "rank:silver", "member"])
    check("마지막 칸 이름", sections[-1][1], "미분류")

    people = [
        _Member("대장님", [CHIEF]),
        _Member("금손", [GOLD]),
        _Member("은손", [SILVER]),
        _Member("둘다가진사람", [GOLD, SILVER]),
        _Member("등급없음", [777]),
        _Member("역할아예없음", []),
        _Member("대장이자골드", [CHIEF, GOLD]),
    ]
    got = {m.display_name: core._member_section_key(m, SETTINGS) for m in people}
    check("대장", got["대장님"], "chief")
    check("골드", got["금손"], "rank:gold")
    check("실버", got["은손"], "rank:silver")
    check("등급 여러 개면 먼저 적힌 쪽", got["둘다가진사람"], "rank:gold")
    check("대장이 등급보다 우선", got["대장이자골드"], "chief")
    check("🚨 등급 역할이 없어도 담김", got["등급없음"], "member")
    check("🚨 역할이 아예 없어도 담김", got["역할아예없음"], "member")
    check("🚨 전원이 실재하는 칸에 들어감",
          set(got.values()) <= {k for k, _t, _c in sections}, True)

    print("\n[4] 등급을 안 쓰는 서버 — 예전과 똑같이 두 칸")
    core = _core_with({})
    sections = core._roster_sections()
    check("두 칸뿐", [k for k, _t, _c in sections], ["chief", "member"])
    check("마지막 칸 이름", sections[-1][1], "멤버")
    got = {m.display_name: core._member_section_key(m, SETTINGS) for m in people}
    check("🚨 대장 아닌 사람은 전부 멤버 칸",
          {v for k, v in got.items() if "대장" not in k}, {"member"})

    print("\n[5] 등급 역할을 실수로 지운 서버 (설정엔 남아 있음)")
    core = _core_with(TWO_RANKS)
    orphans = [_Member("아무개", []), _Member("아무개2", [12345])]
    got = {m.display_name: core._member_section_key(m, SETTINGS) for m in orphans}
    check("🚨 명단이 비지 않고 전원 미분류로", set(got.values()), {"member"})


def test_legacy_parser():
    print("\n[6] 옛 명단 문서 파서 — 구획 제목을 사람으로 오인하지 않기")
    load_config(TWO_RANKS)
    from mari_utils import parse_legacy_id_document

    doc = "\n".join([
        "```ansi", "게임 아이디 목록", "",
        "대장", "[홍길동]", "스팀 : gildong", "",
        "4레벨", "[김철수]", "스팀 : chulsoo", "",
        "골드", "[박영희]", "스팀 : younghee", "```",
    ])
    parsed = parse_legacy_id_document(doc, ["골드", "실버"])
    check("사람만 인식", sorted(parsed), ["김철수", "박영희", "홍길동"])
    check("아이디 값도 그대로", parsed["박영희"], {"스팀": "younghee"})

    # 제목 뒤에 아이디 줄이 붙어버린 문서에서만 차이가 납니다.
    # (아이디가 하나도 안 붙은 이름은 어차피 버려져요)
    dirty = "\n".join(["[골드]", "스팀 : ghost", "[박영희]", "스팀 : younghee"])
    check("🚨 등급 이름을 알려주면 제목이 사람으로 안 잡힘",
          sorted(parse_legacy_id_document(dirty, ["골드"])), ["박영희"])
    check("(대조) 안 알려주면 사람으로 잡힘",
          sorted(parse_legacy_id_document(dirty, [])), ["골드", "박영희"])
    check("공백·대소문자 차이는 무시",
          sorted(parse_legacy_id_document("\n".join(
              ["[실 버]", "스팀 : ghost", "[박영희]", "스팀 : younghee"]), ["실버"])),
          ["박영희"])


def test_legacy_keys():
    """예전 이름을 그대로 둔 guild.json도 동작해야 합니다.

    🚨 여기가 깨지면, 회사 PC에서 `git pull`만 한 순간 생일 알림 멘션이 조용히
       사라집니다. 봇은 안 죽고 기동 로그에도 티가 잘 안 나요.
    """
    print("\n[7] 예전 키 이름 (pull만 하고 설정은 안 고친 서버)")
    cfg = load_config({
        "roles": {"broadcast_mention": 111222333, "bot_mention": 444555666,
                  "town_guide": 777888999},
        "onboarding": {"guide1": {"add": [1]}},
        "personas": {"papa": 12345},
    })
    check("🚨 옛 이름으로도 생일 멘션 역할을 읽음", cfg.BIRTHDAY_MENTION_ROLE_ID, 111222333)
    check("나머지 역할은 그대로", cfg.MARI_CALL_ROLE_ID, 444555666)
    warns = " | ".join(cfg.GUILD_CONFIG_WARNINGS)
    check("이름을 바꾸라고 안내", "예전 이름이에요" in warns, True)
    check("창고로 간 설정도 알려줌 (최상위)", "onboarding" in warns and "personas" in warns, True)
    check("창고로 간 설정도 알려줌 (roles)", "town_guide" in warns, True)

    print("\n[8] 이름을 고친 뒤 — 잔소리가 없어야 해요")
    cfg = load_config({"roles": {"birthday_mention": 111222333, "bot_mention": 444555666}})
    check("새 이름으로 읽음", cfg.BIRTHDAY_MENTION_ROLE_ID, 111222333)
    check("경고 없음", cfg.GUILD_CONFIG_WARNINGS, [])

    print("\n[9] 오타는 조용히 넘어가면 안 됩니다")
    cfg = load_config({"roles": {"birthday_mention": 111, "brithday_mention": 999}})
    check("오타 키를 짚어줌",
          any("brithday_mention" in w and "오타" in w for w in cfg.GUILD_CONFIG_WARNINGS), True)
    check("옛 이름과 헷갈리지 않음", cfg.BIRTHDAY_MENTION_ROLE_ID, 111)


def test_fatal():
    """설정이 **있는데 못 읽을 때**는 기동을 막아야 합니다.

    🚨 그대로 켜면 modules를 못 읽어서 주문하지 않은 기능까지 전부 켜진 채로
       납품돼요. 클라이언트가 그걸 쓰기 시작한 뒤 설정을 고치면 그 데이터가
       통째로 빠집니다. (2026-08-15 결정)
    """
    print("\n[10] 설정 파일이 없을 때 — 막으면 안 됨")
    cfg = load_config(None)          # 파일 자체를 안 만듦
    check("새 서버는 그냥 뜸", cfg.GUILD_CONFIG_FATAL, None)
    check("전부 켜기가 기본", cfg.ENABLED_MODULES, None)

    print("\n[11] 🚨 JSON 문법이 깨졌을 때 — 막아야 함")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "guild.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"modules": ["shop"] "roles": {}}')     # 쉼표 빠짐
    os.environ["MARI_GUILD_CONFIG"] = path
    os.environ["MARI_DATA_DIR"] = tempfile.mkdtemp()
    for name in [m for m in sys.modules if m.startswith(("mari", "cogs")) or m == "modules"]:
        del sys.modules[name]
    import mari_config as broken
    check("기동을 막는 표시가 켜짐", bool(broken.GUILD_CONFIG_FATAL), True)
    check("이유에 원인이 담김", "JSONDecodeError" in broken.GUILD_CONFIG_FATAL, True)
    check("고치는 법도 안내", "jsonlint" in broken.GUILD_CONFIG_FATAL, True)
    # ⚠️ import 도중에 터지면 안 돼요. 여기까지 왔다는 게 그 증거입니다.
    check("🚨 import는 예외 없이 끝남 (알림 웹훅에 닿아야 하니까)", True, True)

    print("\n[12] 🚨 최상위가 객체가 아닐 때 — 막아야 함")
    path = os.path.join(tempfile.mkdtemp(), "guild.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write('["shop", "birthday"]')
    os.environ["MARI_GUILD_CONFIG"] = path
    os.environ["MARI_DATA_DIR"] = tempfile.mkdtemp()
    for name in [m for m in sys.modules if m.startswith(("mari", "cogs")) or m == "modules"]:
        del sys.modules[name]
    import mari_config as arr
    check("기동을 막는 표시가 켜짐", bool(arr.GUILD_CONFIG_FATAL), True)

    print("\n[13] 🚨 못 읽었으면 데이터 파일도 만들면 안 됨")
    # 설정을 못 읽으면 "담긴 모듈"이 전부로 잡혀요. 그대로 두면 주문하지 않은 기능의
    # 빈 JSON이 몽땅 생기고, 나중에 설정을 고쳐도 그 파일들이 그대로 남습니다.
    data_dir = tempfile.mkdtemp()
    path = os.path.join(tempfile.mkdtemp(), "guild.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{이건 JSON이 아님")
    os.environ["MARI_GUILD_CONFIG"] = path
    os.environ["MARI_DATA_DIR"] = data_dir
    for name in [m for m in sys.modules if m.startswith(("mari", "cogs")) or m == "modules"]:
        del sys.modules[name]
    import mari_storage  # noqa: F401  (import하는 순간 init_json_files가 돕니다)
    made = sorted(os.listdir(data_dir))
    check("주문 안 한 기능의 빈 JSON이 안 생김", made, [])
    # (실제 기동에서는 mari_settings.json 하나가 생겨요. 기동 중단 메세지에 봇 이름을
    #  쓰느라 설정을 읽기 때문인데, 모든 배포가 갖는 공용 파일이라 괜찮습니다)

    print("\n[14] 정상 설정은 당연히 안 막힘")
    ok = load_config({"modules": ["shop"], "roles": {}})
    check("표시 없음", ok.GUILD_CONFIG_FATAL, None)
    check("주문한 모듈만", ok.ENABLED_MODULES, ["shop"])


if __name__ == "__main__":
    test_parsing()
    test_sections()
    test_legacy_parser()
    test_legacy_keys()
    test_fatal()
    print()
    if _fails:
        print(f"🚨 {len(_fails)}건 실패: {', '.join(_fails)}")
        sys.exit(1)
    print("✅ guild.json 검사 전부 통과")
    sys.exit(0)
