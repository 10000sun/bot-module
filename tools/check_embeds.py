"""화면(임베드)이 디스코드 한도를 넘지 않는지 확인합니다. (디스코드에 연결하지 않아요)

`check_money`가 돈 계층의 바닥을 본다면, 이건 **화면의 바닥**을 봅니다.

임베드 말고 **그냥 메세지 본문(2000자)**도 같이 봅니다. 여기도 똑같이 한 글자만
넘어가면 그 메세지가 통째로 안 나가요.

디스코드 임베드에는 한도가 다섯 개 있어요.

    제목 256 · 설명 4096 · 필드 이름 256 · 필드 값 1024 · **전체 6000**

마지막 것이 함정입니다. 칸을 하나하나 1024자로 잘라놔도 여러 개가 쌓이면 여기 걸려요.
그리고 넘겼을 때 **그 줄만 빠지는 게 아니라 메세지 전송 자체가 400으로 거부**됩니다.
discord.py는 이걸 로컬에서 전혀 검사하지 않아요 — 보내는 순간에야 터집니다.

실제로 이 프로젝트에서 한 번에 다섯 화면이 이 상태였습니다. 전부 "상한을 지킨 입력"만
넣었는데도요.

    상점 전광판 9,687자 · 주식 목록 7,214자 · 위키 조회 6,239자
    내전 모집글 6,505자 · 파티 모집글 6,056자

증상이 고약합니다. 참가 버튼은 "저장 → 모집글 다시 그리기" 순서로 도는데, 다시 그리기가
거부되면 참가는 저장됐는데 화면만 그대로예요. 명단이 그 시점에 얼어붙습니다.
전광판은 아예 안 그려지고, 관리자는 왜인지 알 방법이 없어요.

이 도구는 각 화면을 **가장 나쁜 입력**으로 한 번씩 그려보고 재봅니다.
   · 자유 입력 칸은 코드에 걸린 상한만큼 (상한이 없으면 디스코드 최대치 6000자)
   · 항목 수는 코드에 걸린 최대치만큼 (상품 25개, 종목 25개, 참가자 99명 …)

상한 값을 각 모듈에서 **읽어와서** 쓰기 때문에, 나중에 상한을 올리면 이 검사가
자동으로 그 값으로 다시 재봐요. 숫자를 두 군데 적을 필요가 없습니다.

사용:
    .venv\\Scripts\\python tools/check_embeds.py

문제가 있으면 종료 코드 1로 끝납니다.

## 여기서 **안 보는** 것

  · 여기 적지 않은 화면. 자유 입력이 들어가는 화면을 새로 만들면 **여기에도 추가**하세요.
  · 드롭다운(라벨 100자)·자동완성(항목 100자) 한도.
    (자동완성은 chunsik_utils.name_choices가, 드롭다운은 각 View가 따로 막고 있어요)
  · 실제 디스코드가 받아주는지. 여기서 통과해도 권한·레이트리밋은 별개예요.
"""

import os
import sys
import datetime as dt
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHUNSIK = os.path.join(os.path.dirname(HERE), "chunsik")
sys.path.insert(0, CHUNSIK)

# 🇰🇷 한국어 윈도우 콘솔(cp949)에서 이모지를 찍으면 UnicodeEncodeError로 죽습니다.
#    납품 절차에서 클라이언트가 직접 돌리는 도구라 콘솔을 고르게 할 수 없어요.
#    아래 chunsik 모듈들은 import되는 순간 이모지를 찍으므로 반드시 그보다 먼저 불러야 합니다.
from chunsik_console import force_utf8_console  # noqa: E402  (경로를 먼저 꽂아야 해서)

force_utf8_console()

# 진짜 데이터 폴더를 건드리지 않게, 설정을 읽기 전에 임시 폴더를 꽂아둡니다.
os.environ["CHUNSIK_GUILD_CONFIG"] = os.path.join(tempfile.gettempdir(), "no-such-guild.json")
os.environ["CHUNSIK_DATA_DIR"] = tempfile.mkdtemp()

import discord  # noqa: E402
from chunsik_config import KST  # noqa: E402
from chunsik_names import currency  # noqa: E402
from chunsik_utils import add_lines_field, chunk_lines, fit_embed, mention_lines  # noqa: E402

# 📏 디스코드 임베드 한도. chunsik_utils에도 같은 값이 있지만, 검사 도구가 검사 대상의
#    상수를 그대로 가져다 쓰면 그 값이 틀렸을 때 둘 다 같이 틀려서 아무것도 못 잡아요.
#    그래서 여기엔 일부러 손으로 적습니다. (디스코드 공식 문서 기준)
TITLE_MAX = 256
DESC_MAX = 4096
FIELD_NAME_MAX = 256
FIELD_VALUE_MAX = 1024
TOTAL_MAX = 6000

# 디스코드 슬래시 명령의 문자열 옵션이 받을 수 있는 최대 길이.
# 코드에 상한이 없으면 유저는 여기까지 넣을 수 있어요.
DISCORD_STRING_MAX = 6000

# 임베드가 아닌 그냥 메세지 본문의 한도.
MESSAGE_MAX = 2000

_fails = []


def _fill(n):
    """길이 n짜리 한글 문자열. (한글 한 글자도 디스코드는 1자로 셉니다)"""
    return "가" * n


def measure(label, embed):
    """임베드 하나를 재고 한도를 넘는 항목을 전부 보고합니다."""
    problems = []
    if embed.title and len(embed.title) > TITLE_MAX:
        problems.append(f"제목 {len(embed.title):,}자 > {TITLE_MAX}")
    if embed.description and len(embed.description) > DESC_MAX:
        problems.append(f"설명 {len(embed.description):,}자 > {DESC_MAX}")
    for i, field in enumerate(embed.fields):
        if len(field.name or "") > FIELD_NAME_MAX:
            problems.append(f"{i + 1}번째 필드 이름 {len(field.name):,}자 > {FIELD_NAME_MAX}")
        if len(field.value or "") > FIELD_VALUE_MAX:
            problems.append(f"{i + 1}번째 필드 값 {len(field.value):,}자 > {FIELD_VALUE_MAX}")
        if not (field.value or ""):
            problems.append(f"{i + 1}번째 필드 값이 비었어요 (디스코드가 거부합니다)")
    total = len(embed)
    if total > TOTAL_MAX:
        problems.append(f"전체 {total:,}자 > {TOTAL_MAX}")

    if problems:
        _fails.append(label)
        print(f"  🚨 {label} — {total:,}자")
        for problem in problems:
            print(f"       · {problem}")
    else:
        print(f"  OK  {label} — {total:,}자 / {TOTAL_MAX:,}")


def untouched(label, embed, mark="길어서 줄였어요"):
    """평범한 길이의 입력에는 잘림 표시가 붙지 않아야 합니다.

    fit_embed가 무조건 깎아버리면 한도는 지키지만 멀쩡한 화면까지 망가져요.
    "안 넘치면 손대지 않는다"도 같이 지켜져야 합니다.
    """
    trimmed = [f.name for f in embed.fields if mark in (f.value or "")]
    if trimmed:
        _fails.append(label)
        print(f"  🚨 {label} — 안 넘치는데 잘렸어요: {trimmed}")
    else:
        print(f"  OK  {label} — {len(embed):,}자, 손대지 않음")


def check_shop(shop_mod):
    print("\n[1] 🛒 상점 전광판 — 상품이 꽉 찼을 때")
    shop = object.__new__(shop_mod.ChunsikShop)
    board = {
        "shop_name": _fill(shop_mod.MAX_BOARD_NAME),
        "shop_desc": _fill(shop_mod.MAX_BOARD_DESC),
        "items": {
            str(i): {
                "name": _fill(shop_mod.MAX_ITEM_NAME), "price": 999_999_999,
                "desc": _fill(shop_mod.MAX_ITEM_DESC), "is_role": True,
                "role_id": 123456789012345678, "can_buy": True, "can_sell": True,
                "resale_percent": 50,
            }
            for i in range(shop_mod.MAX_BOARD_ITEMS)
        },
    }
    measure(f"전광판 (상품 {shop_mod.MAX_BOARD_ITEMS}개, 이름·설명 상한까지)",
            shop._generate_shop_embed(board))

    normal = {"shop_name": "잡화점", "shop_desc": "이것저것 팝니다", "items": {
        str(i): {"name": f"물건{i}", "price": 1000, "desc": "쓸 만해요", "is_role": False,
                 "role_id": None, "can_buy": True, "can_sell": True, "resale_percent": 50}
        for i in range(5)}}
    untouched("전광판 (평범한 매대 5개)", shop._generate_shop_embed(normal))


def check_stock(stock_mod):
    print("\n[2] 📈 주식 목록 — 종목이 꽉 찼을 때")

    def build(count, name_len, reason_len):
        # `/주식 목록`이 만드는 것과 같은 모양이에요. (cogs/stock.py list_stocks)
        embed = discord.Embed(title="📊 현재 상장된 주식 목록", color=discord.Color.green())
        for _ in range(count):
            embed.add_field(
                name=f"🔹 {_fill(name_len)}",
                value=(f"현재가: `999,999,999` {currency()}\n"
                       f"💡 사유: {_fill(reason_len)} (`2026-01-01` 변동)"),
                inline=False)
        embed.set_footer(text="⚠️ 종목이 99개라 25개만 보여요 (숨은 종목 74개)")
        return fit_embed(embed)

    limit = stock_mod.ChunsikStock.MAX_STOCKS
    measure(f"주식 목록 (종목 {limit}개, 이름·찌라시 상한까지)",
            build(limit, stock_mod.MAX_STOCK_NAME, stock_mod.MAX_STOCK_REASON))
    untouched("주식 목록 (평범한 종목 5개)", build(5, 6, 20))


def check_wiki(wiki_mod):
    print("\n[3] 📚 위키 조회 — 여섯 칸이 전부 길 때")
    # 위키는 일부러 입력 상한을 안 겁니다. 길게 적어두고 조회할 때 줄이는 설계예요.
    # (등록할 때 "조회하면 뒷부분이 생략돼요"라고 안내까지 합니다) 그래서 여기서는
    # 디스코드가 받아주는 최대치를 그대로 넣어봅니다.
    long_text = _fill(DISCORD_STRING_MAX)
    fit = wiki_mod._fit_field
    blank = "​"
    embed = discord.Embed(title=_fill(32), description=f"좌우명: {fit(long_text)}", color=0x89CFF0)
    embed.add_field(name=blank, value=blank, inline=False)
    embed.add_field(name="🎂 생일", value=fit(long_text), inline=True)
    embed.add_field(name="📍 서식지", value=fit(long_text), inline=True)
    embed.add_field(name="🧠 MBTI", value=fit(long_text), inline=True)
    embed.add_field(name=blank, value=blank, inline=False)
    embed.add_field(name="🌀 논란 및 사건 사고", value=fit(long_text), inline=False)
    embed.add_field(name=blank, value=blank, inline=False)
    embed.add_field(name="📎 TMI", value=fit(long_text), inline=False)
    # 푸터도 실제 코드가 만드는 것을 씁니다. 손으로 적어두면 저기서 이름 상한이
    # 바뀌어도 여기는 옛 길이로 계속 통과시켜요.
    embed.set_footer(text=wiki_mod.editor_footer(
        wiki_mod.stamp_editor({}, _fill(DISCORD_STRING_MAX))))
    measure("위키 조회 (여섯 칸 전부 최대)", fit_embed(embed))


def check_wiki_list(wiki_mod):
    print("\n[3-2] 📚 위키 목록 — 사람이 늘었을 때")
    # 예전엔 4000자가 넘으면 "나중에 검색 기능도 추가할게!" 한 줄만 나가고 목록을
    # 아예 못 봤어요. 지금은 페이지로 나눕니다. 나눈 **뒤 제일 긴 페이지**를 재야 해요.
    #
    # 🚨 소개는 상한 없는 자유 입력입니다. 한 명이 6000자를 적으면 chunk_lines로는
    #    못 나눠요(줄 사이에서만 나눔). 그래서 줄부터 자르는지가 진짜 검사입니다.
    def worst(count, intro_len):
        rows = [(_fill(32), str(10 ** 17 + i), _fill(intro_len)) for i in range(count)]
        return wiki_mod.build_wiki_list_pages(rows), len(rows)

    for label, count, intro_len in (
        ("위키 목록 (100명 · 소개 평범)", 100, 30),
        ("위키 목록 (100명 · 소개 전부 최대)", 100, DISCORD_STRING_MAX),
        ("위키 목록 (1명 · 소개 최대)", 1, DISCORD_STRING_MAX),
        ("위키 목록 (1000명 · 소개 최대)", 1000, DISCORD_STRING_MAX),
    ):
        (pages, dropped), total = worst(count, intro_len)
        longest = max((len(page) for page in pages), default=0)
        if longest > DESC_MAX:
            _fails.append(label)
            print(f"  🚨 {label} — {len(pages)}페이지로 나눴는데 제일 긴 페이지가 "
                  f"{longest:,}자 > {DESC_MAX:,}")
            print("       · 한 줄이 혼자 한도를 넘으면 chunk_lines로는 못 나눠요.")
            print("       · cogs/wiki.py의 LIST_LINE_LIMIT으로 줄을 먼저 자르세요.")
        elif not pages:
            _fails.append(label)
            print(f"  🚨 {label} — 페이지가 하나도 안 나왔어요 (빈 임베드는 디스코드가 거부합니다)")
        else:
            note = f", {dropped}명 생략" if dropped else ""
            print(f"  OK  {label} — {len(pages)}페이지, 제일 긴 페이지 {longest:,}자 "
                  f"/ {DESC_MAX:,} (총 {total}명{note})")

    # 다 담기지 못한 사람이 있으면 반드시 세어서 알려야 해요. 조용히 사라지면
    # "등록했는데 목록에 없다"는 문의로 돌아옵니다.
    (pages, dropped), total = worst(1000, DISCORD_STRING_MAX)
    if dropped <= 0:
        _fails.append("위키 목록 생략 인원 표시")
        print("  🚨 목록에서 잘려나간 사람이 있는데 인원을 세지 않았어요")
    else:
        print(f"  OK  생략 인원 표시 — {total}명 중 {dropped}명 생략을 알려줍니다")


def check_chronicle(chron_mod):
    print("\n[4] 📜 연대기 — 목록과 기록 확인")
    chron = object.__new__(chron_mod.ChunsikChronicle)
    worst = {
        "id": "1", "title": _fill(chron_mod.TITLE_INPUT_LIMIT),
        "detail": _fill(chron_mod.DETAIL_INPUT_LIMIT), "date": "2026-01-01",
        "tag": "사건", "message_link": "https://discord.com/channels/1/2/3",
    }
    measure("기록 확인 (제목·내용 상한까지)", chron._entry_embed(worst, "📜 연대기에 남겼어요"))

    view = object.__new__(chron_mod.ChroniclePageView)
    view.page, view.tag, view.newest_first = 0, None, True
    view._rows = lambda: [dict(worst, id=str(i)) for i in range(chron_mod.PAGE_SIZE)]
    measure(f"목록 ({chron_mod.PAGE_SIZE}건 전부 상한까지)", view.build_embed())

    # 🕰️ 상한이 생기기 전에는 6000자까지 들어갔어요. 그렇게 저장된 옛 기록도 떠야 합니다.
    legacy = dict(worst, title=_fill(DISCORD_STRING_MAX), detail=_fill(DISCORD_STRING_MAX))
    measure("기록 확인 (상한 이전 옛 기록)", chron._entry_embed(legacy, "📜 연대기에 남겼어요"))
    view._rows = lambda: [dict(legacy, id=str(i)) for i in range(chron_mod.PAGE_SIZE)]
    measure("목록 (상한 이전 옛 기록)", view.build_embed())


def check_recruit(party_mod, scrim_mod):
    print("\n[5] 🎯 파티·내전 모집글 — 정원이 꽉 찼을 때")
    start = dt.datetime.now(KST) + dt.timedelta(hours=1)
    party = object.__new__(party_mod.ChunsikParty)
    scrim = object.__new__(scrim_mod.ChunsikScrim)

    def party_embed(title_len, note_len, members, waiting):
        return party._embed({
            "title": _fill(title_len), "note": _fill(note_len), "size": 99, "host": 1,
            "start": start.isoformat(), "channel_id": 1, "guild_id": 1,
            "members": list(range(1, members + 1)),
            "waiting": list(range(500, 500 + waiting)), "closed": False, "reminded": False,
        }, 1)

    def scrim_embed(title_len, note_len, members):
        return scrim._embed({
            "title": _fill(title_len), "note": _fill(note_len),
            "size": scrim_mod.MAX_TEAM_SIZE, "host": 1, "start": start.isoformat(),
            "channel_id": 1, "guild_id": 1, "members": list(range(1, members + 1)),
            "teams": None, "result": None, "closed": False, "reminded": False, "balanced": True,
        }, {})

    measure("파티 (정원 99 + 대기 60, 제목·설명 상한까지)",
            party_embed(party_mod.TITLE_LIMIT, party_mod.NOTE_LIMIT, 99, 60))
    measure("파티 (상한 이전 옛 모집글)",
            party_embed(DISCORD_STRING_MAX, DISCORD_STRING_MAX, 99, 60))
    untouched("파티 (평범한 모집 10명)", party_embed(12, 40, 10, 0))

    full = scrim_mod.MAX_TEAM_SIZE * 2 + 20   # 양 팀 정원에 대기까지 넉넉히
    measure(f"내전 (참가 {full}명, 제목·설명 상한까지)",
            scrim_embed(scrim_mod.TITLE_LIMIT, scrim_mod.NOTE_LIMIT, full))
    measure("내전 (상한 이전 옛 모집글)",
            scrim_embed(DISCORD_STRING_MAX, DISCORD_STRING_MAX, full))
    untouched("내전 (평범한 내전 10명)", scrim_embed(12, 40, 10))


def check_selfrole(selfrole_mod):
    print("\n[6] 🎭 셀프 역할 패널 — 역할이 꽉 찼을 때")
    selfrole = object.__new__(selfrole_mod.ChunsikSelfRole)
    panel = {
        "title": _fill(selfrole_mod.TITLE_LIMIT),
        "description": _fill(selfrole_mod.DESCRIPTION_LIMIT),
        "roles": [{"id": 123456789012345678 + i, "label": _fill(50), "emoji": "🎮"}
                  for i in range(selfrole_mod.MAX_ROLES_PER_PANEL)],
    }
    embed = selfrole._panel_embed(panel)
    measure(f"패널 (역할 {selfrole_mod.MAX_ROLES_PER_PANEL}개, 제목·설명 상한까지)", embed)

    # 🎭 이 패널은 설명과 역할 목록이 임베드 설명 **한 칸**을 나눠 씁니다. 설명이 칸을
    #    다 먹으면 역할 목록이 통째로 잘려서, 버튼만 있고 무슨 역할인지는 안 보이는
    #    패널이 돼요. 오류가 안 나는 종류의 고장이라 길이와 따로 봅니다.
    if "<@&" in (embed.description or ""):
        print("  OK  패널 (설명이 상한까지 차도 역할 목록이 남아요)")
    else:
        _fails.append("셀프 역할 패널 역할 목록")
        print("  🚨 패널 — 설명이 길어서 역할 목록이 통째로 밀려났어요")
        print("       · 버튼은 있는데 무슨 역할인지 안 보이는 패널이 됩니다")
        print("       · cogs/selfrole.py의 DESCRIPTION_LIMIT을 줄이거나 목록을 필드로 옮기세요")


def measure_message(label, text, limit=MESSAGE_MAX):
    """나눠 보내는 메세지가 조각마다 한도 안에 들어오는지 봅니다.

    ⚠️ 나눠 보낸다고 안심하면 안 돼요. chunk_lines는 줄과 줄 **사이**에서만 나눕니다.
       멘션처럼 한 줄에 몰아넣은 내용은 그 줄이 통째로 한 조각이 돼서, 나눠도 여전히
       한도를 넘어요. 그래서 재야 하는 건 본문 전체 길이가 아니라
       **나눈 뒤 제일 긴 조각**입니다.
    """
    parts = chunk_lines(text.split("\n"), limit=1900)
    longest = max((len(part) for part in parts), default=0)
    if longest > limit:
        _fails.append(label)
        print(f"  🚨 {label} — 본문 {len(text):,}자를 {len(parts)}조각으로 나눴는데도 "
              f"제일 긴 조각이 {longest:,}자 > {limit:,}")
        print("       · 한 줄이 혼자 한도를 넘으면 chunk_lines로는 못 나눠요.")
        print("       · 멘션 목록이라면 chunsik_utils.mention_lines로 줄을 먼저 끊으세요.")
    else:
        print(f"  OK  {label} — {len(parts)}조각, 제일 긴 조각 {longest:,}자 / {limit:,}")


def check_event_announce():
    print("\n[7] 🎉 이벤트 결과 발표 — 참가자가 몰렸을 때 (임베드가 아니라 그냥 메세지)")

    def build(first_n, rest_n):
        # cogs/games.py의 _close_evashi_window_now가 만드는 것과 같은 모양이에요.
        lines = [f"🎉 **이벤트 결과** (총 {first_n + rest_n}명 참여)"]
        if first_n:
            lines.append("")
            lines.append(f"🥇 선착순 {first_n}명 · 각 5,000 {currency()} 지급")
            lines.extend(mention_lines([700000000000000000 + i for i in range(first_n)]))
        if rest_n:
            lines.append("")
            lines.append(f"🎊 참가 {rest_n}명 · 각 1,000 {currency()} 지급")
            lines.extend(mention_lines([800000000000000000 + i for i in range(rest_n)]))
        return "\n".join(lines)

    # 참가 인원에 상한이 없는 기능이라, 큰 서버 기준으로 넉넉히 봅니다.
    for count in (100, 500, 1000):
        measure_message(f"결과 발표 (선착순 3명 + 참가 {count}명)", build(3, count))


def check_roster(ids_mod):
    print("\n[8] 🆔 아이디 명단 — 아주 긴 아이디가 섞였을 때 (```ansi 코드블록)")

    def build(members, id_len):
        # cogs/ids.py의 _refresh_id_roster가 만드는 것과 같은 모양이에요.
        # ⚠️ ESC 문자는 편집 도구를 거치면 진짜 제어문자로 박혀요. chr(27)로 직접 만듭니다.
        #    (NEXT.md의 "소스에 이스케이프를 넣을 때 편집 도구를 믿지 말 것" 항목)
        esc = chr(27)
        lines = ["게임 아이디 목록", "", f"{esc}[2;34m멤버{esc}[0m", ""]
        for i in range(members):
            lines.append(f"[{esc}[2;34m유저{i}{esc}[0m]")
            lines.append(f"롤 : {_fill(id_len)}")
            lines.append("")
        return lines

    for members, id_len in ((30, 12), (30, ids_mod.MAX_ID_LENGTH), (1, 6000), (1, 50000)):
        lines = build(members, id_len)
        # 진짜 코드를 그대로 부릅니다. 여기서 베껴 쓰면 코드가 바뀌어도 검사는 옛것을 봐요.
        chunks = ids_mod.split_roster_lines(lines)
        longest = max((len(f"```ansi\n{chunk}\n```") for chunk in chunks), default=0)
        label = f"명단 (멤버 {members}명 · 아이디 {id_len:,}자)"
        if longest > MESSAGE_MAX:
            _fails.append(label)
            print(f"  🚨 {label} — {len(chunks)}조각인데 제일 긴 메세지가 {longest:,}자 > {MESSAGE_MAX:,}")
            print("       · 한 줄이 혼자 한도를 넘으면 줄 사이에서만 나눠서는 못 줄여요.")
            print("       · 명단 게시가 거부되면 그때부터 명단이 그 상태로 굳습니다.")
        else:
            print(f"  OK  {label} — {len(chunks)}조각, 제일 긴 메세지 {longest:,}자 / {MESSAGE_MAX:,}")


def check_snooze(snooze_mod):
    print("\n[9] ⏰ 스누즈 목록 — 예약이 꽉 찼을 때")
    cog = object.__new__(snooze_mod.ChunsikSnooze)
    now = dt.datetime.now(KST)
    rows = [{
        "id": str(i), "user_id": "1",
        "preview": _fill(snooze_mod.PREVIEW_LIMIT),
        "memo": _fill(FIELD_VALUE_MAX),
        "message_link": "https://discord.com/channels/1/2/3",
        "wake_at": (now + dt.timedelta(hours=i + 1)).isoformat(),
    } for i in range(snooze_mod.MAX_PER_USER)]

    # 진짜 코드를 그대로 부릅니다.
    measure(f"목록 (예약 {snooze_mod.MAX_PER_USER}개, 미리보기·메모 상한까지)",
            cog.build_list_embed(rows))
    untouched("목록 (평범한 예약 3개)", cog.build_list_embed([
        dict(row, preview="내일까지 답장", memo="") for row in rows[:3]]))


def check_diagnostics(diag_mod, setting_mod):
    print("\n[10] 🧪 채널점검 결과 — 전부 실패했을 때")
    # 🚨 하필 **전부 실패했을 때** 결과를 못 보면 제일 나쁩니다. 봇 권한을 아직 안 준
    #    설치 직후가 딱 그 상태예요. 실패 줄에는 예외 원문까지 붙는데 그건 길이 제한이
    #    없어서, 예전엔 칸 하나가 1024자를 넘겨 결과 화면이 통째로 거부됐어요.
    #
    # 채널 개수는 /설정 표에서 읽어옵니다. 채널이 늘면 이 검사도 자동으로 따라가요.
    labels = list(setting_mod.ChunsikSetting._CHANNEL_COMMANDS.keys())
    worst_error = _fill(diag_mod.ERROR_TEXT_LIMIT)

    measure(f"채널점검 (채널 {len(labels)}개 전부 실패 · 오류 원문 최대)",
            diag_mod.build_channel_report(
                [], [f"{name} ({worst_error})" for name in labels], []))
    measure(f"채널점검 (채널 {len(labels)}개 전부 미설정)",
            diag_mod.build_channel_report([], [], labels))
    untouched("채널점검 (전부 정상)", diag_mod.build_channel_report(labels, [], []),
              mark="외 ")


def check_ai_reply():
    print("\n[13] 🤖 AI 답변 — 길게 써 왔을 때 (임베드가 아니라 그냥 메세지)")
    # 🚨 AI는 2,000자보다 긴 답을 쉽게 씁니다("○○에 대해 길게 설명해줘"). 예전엔 통째로
    #    보내다가 400으로 거부됐고, 그게 except에 걸려 "지금 머리가 띵해서 대답을 못
    #    하겠어요"가 나갔어요. **답은 멀쩡히 만들어졌고 돈도 이미 나간 뒤**인데 유저에겐
    #    고장으로 보입니다.
    #
    # ⚠️ chunk_lines로는 못 나눠요 — 줄바꿈 없는 긴 답변이 바로 그 경우입니다.
    #    split_message는 줄 경계를 되도록 지키되 한 줄이 혼자 넘치면 글자 단위로 자릅니다.
    from chunsik_utils import split_message

    mention = 24            # "<@000000000000000000> " 몫
    limit = MESSAGE_MAX - mention

    cases = [
        ("AI 답변 (짧음)", "안녕하세요!"),
        ("AI 답변 (딱 한도)", _fill(limit)),
        ("AI 답변 (한도+1)", _fill(limit + 1)),
        ("AI 답변 (줄바꿈 없이 1만자)", _fill(10000)),
        ("AI 답변 (문단이 여러 개)", ("문단입니다. " * 100 + "\n") * 12),
        ("AI 답변 (10만자)", _fill(100000)),
    ]
    for label, text in cases:
        parts = split_message(text, limit)
        longest = max((len(p) for p in parts), default=0)
        empty = [i for i, p in enumerate(parts) if not p.strip()]
        if longest > limit:
            _fails.append(label)
            print(f"  🚨 {label} — {len(parts)}조각인데 제일 긴 조각이 {longest:,}자 > {limit:,}")
        elif empty:
            # 빈 조각을 보내면 디스코드가 거부해요. 나누려다 오히려 전송이 실패합니다.
            _fails.append(label)
            print(f"  🚨 {label} — 빈 조각이 {len(empty)}개 생겼어요 (디스코드가 거부합니다)")
        else:
            print(f"  OK  {label} — {len(parts)}조각, 제일 긴 조각 {longest:,}자 / {limit:,}")

    # 너무 길면 줄이되, **줄였다는 걸 알려야** 해요. 조용히 끊기면 원인을 알 수 없습니다.
    tail = split_message(_fill(100000), limit)[-1]
    if "줄였어요" not in tail:
        _fails.append("AI 답변 줄임 안내")
        print("  🚨 아주 긴 답변을 줄이면서 그 사실을 알리지 않았어요")
    else:
        print("  OK  줄임 안내 — 마지막 조각에 몇 조각을 줄였는지 적습니다")


def check_settlement(shop_mod):
    print("\n[12] 📊 상점 정산 — 매대가 여러 개일 때")
    # 🚨 매대는 **채널마다 하나씩** 만들 수 있어서 개수에 상한이 없어요. 한 줄이 70자쯤이라
    #    매대가 15개만 넘어가도 칸 하나(1024자)를 넘겨 정산 화면이 통째로 안 뜹니다.
    #    (상품 개수는 MAX_BOARD_ITEMS로 막았는데 매대 개수 쪽은 열려 있어요)
    def build(board_count):
        embed = discord.Embed(title=_fill(30), description=_fill(80), color=0x5ce6b4)
        embed.add_field(name="💰 총 구매 매출", value="1,000,000", inline=True)
        embed.add_field(name="♻️ 총 되팔기 환급", value="500,000", inline=True)
        embed.add_field(name="✅ 순 정산액", value="**500,000**", inline=True)
        lines = [f"<#{800000000000000000 + i}> · 구매 1,234,567 / 되팔기 890,123 / 순액 344,444 원"
                 for i in range(board_count)]
        add_lines_field(embed, "🏪 매대별 상세", lines, empty="해당 월 거래 내역이 없어요.")
        return fit_embed(embed)

    for count in (0, 5, 30, 100):
        measure(f"상점 정산 (매대 {count}개)", build(count))


def check_log_embeds():
    print("\n[11] 🧾 로그 임베드 — 길이를 알 수 없는 값이 들어올 때")
    # 🚨 로그는 마흔 몇 곳에서 부르고, 이름·아이디·명단처럼 **길이를 알 수 없는 값**이
    #    자주 들어갑니다. 한도를 넘으면 send_log_embed가 예외를 삼켜서 콘솔 한 줄만
    #    남고 **그 로그가 조용히 증발**해요. "누가 무엇을 했나"를 남기는 곳이 정작
    #    그 '무엇'이 길 때만 안 남는 셈입니다.
    #    부르는 쪽마다 자르게 하지 않고 build_log_embed 한 곳에서 맞춥니다.
    from chunsik_settings import LOG_FIELD_COUNT_LIMIT, build_log_embed

    huge = _fill(DISCORD_STRING_MAX)
    measure("로그 (설명이 최대)", build_log_embed("id_log", huge, [("이름", "값", True)]))
    measure("로그 (칸 값이 최대)", build_log_embed("id_log", "설명", [("등록 내역", huge, False)]))
    measure("로그 (칸 이름·값 모두 최대)", build_log_embed("id_log", huge, [(huge, huge, False)]))
    measure("로그 (긴 칸 10개)", build_log_embed("id_log", "설명",
                                            [(f"칸{i}", huge, False) for i in range(10)]))
    # 칸 수 한도(25)를 넘기면 그 로그가 통째로 거부돼요.
    many = build_log_embed("id_log", "설명", [(f"칸{i}", "값", True) for i in range(40)])
    if len(many.fields) > LOG_FIELD_COUNT_LIMIT:
        _fails.append("로그 칸 수")
        print(f"  🚨 로그 (칸 40개) — 칸이 {len(many.fields)}개 > {LOG_FIELD_COUNT_LIMIT}")
    else:
        print(f"  OK  로그 (칸 40개) — {len(many.fields)}개로 줄임")
    # 값이 비면 디스코드가 거부합니다.
    measure("로그 (칸 값이 빈 문자열)", build_log_embed("id_log", "설명", [("이름", "", False)]))
    untouched("로그 (평범한 값)", build_log_embed("id_log", "짧은 설명",
                                             [("실행자", "<@123>", True), ("내용", "아이디 등록", False)]))


def main():
    import cogs.chronicle as chron_mod
    import cogs.diagnostics as diag_mod
    import cogs.setting as setting_mod
    import cogs.snooze as snooze_mod
    import cogs.ids as ids_mod
    import cogs.party as party_mod
    import cogs.scrim as scrim_mod
    import cogs.selfrole as selfrole_mod
    import cogs.shop as shop_mod
    import cogs.stock as stock_mod
    import cogs.wiki as wiki_mod

    print("=" * 62)
    print("화면(임베드) 한도 검사 — 가장 나쁜 입력으로 한 번씩 그려봅니다")
    print("=" * 62)

    check_shop(shop_mod)
    check_stock(stock_mod)
    check_wiki(wiki_mod)
    check_wiki_list(wiki_mod)
    check_chronicle(chron_mod)
    check_recruit(party_mod, scrim_mod)
    check_selfrole(selfrole_mod)
    check_event_announce()
    check_roster(ids_mod)
    check_snooze(snooze_mod)
    check_diagnostics(diag_mod, setting_mod)
    check_settlement(shop_mod)
    check_ai_reply()
    check_log_embeds()

    print("\n" + "=" * 62)
    if _fails:
        print(f"🚨 {len(_fails)}개가 디스코드 한도를 넘어요")
        for label in _fails:
            print(f"   - {label}")
        print("\n넘긴 화면은 '일부만 잘리는' 게 아니라 **통째로 안 보입니다.**")
        print("입력 자리에 app_commands.Range로 상한을 걸거나,")
        print("화면을 다 만든 뒤 chunsik_utils.fit_embed(embed)를 부르세요.")
        print("⚠️ fit_embed는 푸터까지 붙인 **맨 마지막**에 불러야 해요. 푸터도 총량에 들어갑니다.")
        print("=" * 62)
        return 1
    print("✅ 화면 한도 검사 전부 통과")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
