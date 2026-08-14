"""플랫폼 이름 정규화, 아이디 문자열 파싱, 멤버 검색 등 공용 도우미 함수."""

import asyncio
import re
from typing import Optional
import discord

from mari_alerts import send_alert
from mari_settings import send_log_embed
from mari_state import record_ledger
from mari_storage import DataSaveError
from mari_names import bot_name, currency, josa


# ========== 🧨 반쪽만 반영된 거래 처리 ==========
async def report_broken_transaction(
    interaction, *, action: str, user_id, lost: str, not_received: str,
    error: BaseException, ledger_delta: int = 0, ledger_balance=None,
) -> None:
    """거래가 반쪽만 반영된 채 끊겼을 때, 유저·관리자·원장 세 곳에 전부 알립니다.

    🚨 [왜 필요한가]
    지갑·주식·상점 인벤토리·캠프 통장은 서로 다른 JSON 파일이라 한 번에 저장할 수 없어요.
    그래서 "지갑에서 빼고 → 주식을 넣는" 거래는 저장이 두 번 일어나고, 그 사이에 두 번째가
    실패하면 앞쪽만 반영된 상태로 남습니다.

    저장 순서는 이미 **가치가 사라지는 쪽**(유저가 손해 보는 쪽)으로 잡혀 있어서 재화가
    복사되는 사고는 없어요. 문제는 그 손해가 **조용하다**는 거였습니다.
      • 유저에게는 "내부 오류"라고만 떠서 돈이 빠진 줄도 몰랐고
      • record_ledger는 성공 경로에서만 불려서 /지갑내역에도 안 남았고
      • 관리자는 사고가 났다는 사실 자체를 알 방법이 없었어요
    이제 유저에게 정확히 알리고, 원장에 흔적을 남기고, 관리자 웹훅까지 울립니다.

    ⚠️ 이 함수는 economy_lock을 **놓은 뒤에** 부르세요. (웹훅·디스코드 전송이 들어있어요)

    action        — "주식 매수", "상점 구매" 처럼 무슨 거래였는지
    lost          — 이미 빠져나간 것 ("재화 12,000")
    not_received  — 못 받은 것 ("삼성전자 3주")
    ledger_delta  — 원장에 남길 실제 변동. 0이면 원장 기록을 건너뜁니다
    """
    detail = f"{type(error).__name__}: {error}"
    print(f"🧨 [거래 중단] {action} — 유저 {user_id}: {lost} 빠졌으나 {not_received} 미지급 ({detail})")

    # 1️⃣ 원장에 남겨서 /지갑내역과 /지급취소가 볼 수 있게 합니다.
    #    (record_ledger는 절대 예외를 던지지 않으니 여기서 또 실패할 걱정은 없어요)
    if ledger_delta:
        record_ledger(user_id, ledger_delta, ledger_balance,
                      f"⚠️ {action} 중단", f"{not_received} 미지급 — {detail}")

    # 2️⃣ 관리자에게 즉시 알립니다. 손으로 메꿔줘야 하는 일이라 묻히면 안 돼요.
    await send_alert(
        f"🧨 {action}가 반쪽만 처리됐어요",
        f"**대상:** <@{user_id}> (`{user_id}`)\n"
        f"**빠져나간 것:** {lost}\n"
        f"**못 받은 것:** {not_received}\n"
        f"**원인:** `{detail}`\n\n"
        f"저장이 두 번 일어나는 거래에서 뒤쪽이 실패했어요. {currency()}{josa(currency(), '이가')} 복사되진 않았지만 "
        f"유저가 손해를 본 상태라 `/지급`이나 `/주식 지급` 등으로 채워주셔야 합니다.\n"
        f"자세한 내용은 `/지갑내역 유저조회`와 `/테스트 데이터점검`에서 확인할 수 있어요.",
    )

    # 3️⃣ 유저에게 정확히 알립니다. "오류가 났어요"로 끝내면 돈이 빠진 줄 모르고 다시 시도해요.
    if interaction is None:
        return
    msg = (f"🧨 **{action}가 도중에 끊겼어요.**\n"
           f"└ 빠져나간 것: **{lost}**\n"
           f"└ 못 받은 것: **{not_received}**\n"
           f"관리자에게 자동으로 알렸어요. 다시 시도하지 마시고 조금만 기다려 주세요!")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception as e:
        print(f"❗ 거래 중단 안내 전송 실패: {e}")


# ========== 📄 임베드 길이 제한에 맞춰 줄 나누기 ==========
# 디스코드 임베드 설명은 4096자, 코드블록까지 감안하면 넉넉잡아 1900자에서 끊어야 해요.
# 이 로직이 캠프 지갑·세금환수·전체 지갑·종가게시 네 군데에 복붙돼 있었고, 그중 하나만
# 여유분을 +2로 잡아 미묘하게 달랐습니다. 하나로 모아서 전부 같은 규칙을 쓰게 했어요.
EMBED_CHUNK_LIMIT = 1900


def chunk_lines(lines: list, limit: int = EMBED_CHUNK_LIMIT) -> list:
    """줄 목록을 limit 글자 이하의 덩어리 여러 개로 나눕니다. (줄 중간에서 자르지 않아요)

    한 줄이 혼자서 limit을 넘으면 그 줄만 담긴 덩어리가 됩니다. 억지로 쪼개서 깨뜨리는 것보다
    한 덩어리가 조금 넘치는 편이 낫고, 애초에 그런 줄이 나오면 그건 부르는 쪽에서 다룰 일이에요.
    """
    chunks, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line + "\n"
        else:
            current += line + "\n"
    if current:
        chunks.append(current)
    return chunks


# ========== 📈 주식 보유량·평가액 ==========
# 보유 기록은 원래 {"종목": 주식수(int)} 였다가 지금은 {"종목": {"shares": n, "avg_price": p}} 예요.
# 옛 형식을 받아주는 처리가 어떤 곳엔 있고 어떤 곳엔 없어서, 예전 데이터가 남아 있으면
# 화면마다 다른 금액이 나올 수 있었습니다. 읽는 방법을 여기 한 군데로 모읍니다.
def holding_shares(holding) -> int:
    """보유 기록 하나에서 주식 수를 꺼냅니다. (신·구 형식 모두 지원)"""
    try:
        if isinstance(holding, dict):
            return int(holding.get("shares", 0))
        return int(holding)
    except (TypeError, ValueError):
        return 0


def holding_avg_price(holding) -> int:
    """보유 기록 하나에서 평단가를 꺼냅니다. (신·구 형식 모두 지원)

    구 형식({"종목": 주식수})에는 평단 정보 자체가 없어서 0을 돌려줘요.
    부르는 쪽에서 "0이면 현재가로 친다"처럼 알아서 메꿔야 합니다.
    """
    if isinstance(holding, dict):
        try:
            return int(holding.get("avg_price", 0))
        except (TypeError, ValueError):
            return 0
    return 0


def portfolio_value(stock_data: dict, user_id) -> int:
    """그 사람이 들고 있는 주식의 현재가 기준 평가액 합계를 구합니다.

    stock_data는 MariStock._load_stocks()가 돌려주는 전체 구조 그대로 넘기면 돼요.
    상장 폐지된 종목은 현재가를 알 수 없으니 0으로 칩니다.
    """
    prices = stock_data.get("stocks", {})
    holdings = stock_data.get("user_shares", {}).get(str(user_id), {})
    total = 0
    for name, holding in (holdings or {}).items():
        try:
            total += holding_shares(holding) * int(prices.get(name, {}).get("price", 0))
        except (TypeError, ValueError):
            continue
    return total


# ========== 🚨 오류 안내 문구 통일 ==========
def describe_user_error(error: BaseException) -> str:
    """유저에게 보여줄 오류 안내 문구를 만듭니다.

    슬래시 명령어(mari_client.on_app_command_error)와 버튼/드롭다운(MariView.on_error)이
    같은 문구를 쓰도록 한 군데에 모아뒀어요. 특히 저장 실패(DataSaveError)와 파일 손상은
    "명령은 받았는데 반영이 안 됐다"는 뜻이라 반드시 유저에게 보여야 합니다.
    """
    if isinstance(error, DataSaveError):
        return f"💾 {error}"
    if isinstance(error, RuntimeError) and "손상되어" in str(error):
        # safe_json_load가 파일 손상을 감지했을 때 던지는 메시지는 그대로 보여줘도 안전해요
        return f"🚨 {error}"
    return "❗ 처리하는 중 예상치 못한 오류가 발생했어요. 잠시 후 다시 시도해 주세요."


class MariView(discord.ui.View):
    """봇의 모든 버튼/드롭다운 창이 상속하는 기본 View.

    🚨 [버그 수정] discord.py는 슬래시 명령어에서 난 예외만 tree.on_error로 모아주고,
    버튼·드롭다운 콜백에서 난 예외는 기본 동작(콘솔에 트레이스백 출력)으로 끝냅니다.
    그래서 상점 구매·되팔기·선물처럼 버튼으로 도는 기능에서 저장이 실패하면(DataSaveError)
    콘솔에만 한 줄 찍히고 유저 화면에는 아무 반응이 없었어요.

    "눌렀는데 아무 일도 안 일어났네" 하고 다시 누르게 되는데, 정작 돈은 이미 빠져나갔을 수도
    있는 상황이라 이 침묵이 제일 위험했습니다. (DataSaveError를 만든 취지가 바로 이걸 막는
    거였는데 정작 버튼 경로만 빠져 있었어요) 이제 슬래시 명령어와 똑같은 문구로 알려줍니다.

    ⚠️ 드롭다운(Select)이나 버튼을 이 View 안에 넣으면 그 콜백의 예외도 여기로 올라오므로,
    Select 클래스마다 따로 처리할 필요는 없어요.
    """

    async def on_error(self, interaction: discord.Interaction,
                       error: Exception, item: discord.ui.Item) -> None:
        print(f"❗ [버튼/드롭다운 오류] {type(self).__name__}.{getattr(item, 'custom_id', None) or type(item).__name__} "
              f"처리 중: {type(error).__name__}: {error}")
        msg = describe_user_error(error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception as e:
            print(f"❗ 오류 메시지 전송 실패: {e}")

# ========== ✨ 플랫폼 정규화 매핑 테이블 ==========
PLATFORM_ALIASES = {
    "battle.net": "Battle.net",
    "battlenet": "Battle.net",
    "배틀넷": "Battle.net",
    "베틀넷": "Battle.net",
    "btlnet": "Battle.net",
    "블리자드": "Battle.net",
    "오버워치": "Battle.net",
    "옵치": "Battle.net",
    "배틀태그": "Battle.net",

    "riot": "Riot",
    "라이엇": "Riot",
    "롤": "Riot",
    "발로란트": "Riot",
    "발로": "Riot",

    "steam": "Steam",
    "스팀": "Steam",

    "insta": "Insta",
    "instagram": "Insta",
    "인스타": "Insta",
    "인스타그램": "Insta",
    "인스타그렘": "Insta",

    "계좌": "계좌번호",
    "계좌번호": "계좌번호",

    "전화": "전화번호",
    "전번": "전화번호",

    "에픽게임즈": "EpicGames",
    "에픽겜즈": "EpicGames",
    "epicgames": "EpicGames",

    "락스타": "Rockstar",
    "rockstar": "Rockstar",
    "rockstargames": "Rockstar",
    "rockstargame": "Rockstar",
    "락스타게임즈": "Rockstar",

    "레식": "유비소프트",
    "레인보우식스": "유비소프트",
    "유비": "유비소프트",
    "유비게임즈": "유비소프트",
    "윾비": "유비소프트",
    "유비플레이": "유비소프트",

    "스팀옵치": "스팀오버워치",
}


def normalize_platform(name: str) -> str:
    key = name.strip().lower()
    return PLATFORM_ALIASES.get(key, name.strip())

# 🆔 [신규] 정식으로 "인식되는" 플랫폼 이름 목록. 자동등록 시 여기 없는 플랫폼이 나오면
# (오타거나 진짜 새로운 플랫폼이거나) 봇이 알아서 등록하지 않고 관리자에게 확인을 받습니다.
KNOWN_PLATFORMS = set(PLATFORM_ALIASES.values()) | {"기타"}

# 🗨️ [신규] 아이디 자동등록 채널에서 "이건 그냥 잡담이지 아이디 등록 시도가 아니다"를
# 가려내기 위한 최소한의 휴리스틱. 아래 중 하나라도 해당하면 "아이디처럼 생겼다"고 봐요.
_ID_LIKE_HINT_RE = re.compile(r"#|\d{4,}")

def _looks_like_id_entry(seg: str) -> bool:
    """줄바꿈/쉼표로 나뉜 한 조각이 게임 아이디 등록 시도처럼 보이는지 판별합니다."""
    if _ID_LIKE_HINT_RE.search(seg):  # '#태그' 또는 4자리 이상 숫자(스팀ID 등)
        return True
    seg_lower = seg.lower()
    for alias_key in PLATFORM_ALIASES:
        if alias_key in seg_lower:  # "라이엇", "배틀넷", "스팀" 같은 플랫폼 단어가 포함돼있는지
            return True
    return False

# 🆔 [신규] "플랫폼 아이디" 형식으로 올려달라고 안내해도, 예전 습관대로 콜론(:)을 넣어서
# "플랫폼 : 아이디", "플랫폼: 아이디", "플랫폼:아이디" 이렇게 올리는 분들이 있어요.
# 이 함수는 콜론이 어디 붙어있든 상관없이 (플랫폼, 아이디)로 정확히 나눠줍니다.
def _split_platform_and_id(seg: str) -> Optional[tuple]:
    tokens = seg.split(maxsplit=1)
    if len(tokens) == 2:
        plat, val = tokens
        plat = plat.rstrip(":").strip()
        val = val.lstrip(":").strip()
        if plat and val:
            return plat, val
    # 공백 없이 콜론만으로 붙어있는 경우 (예: "라이엇:만해#kr1")
    if ":" in seg:
        plat, _, val = seg.partition(":")
        plat, val = plat.strip(), val.strip()
        if plat and val:
            return plat, val
    return None

def next_misc_name(existing: dict) -> str:
    nums = [int(k[2:]) for k in existing if k.startswith("기타") and k[2:].isdigit()]
    return f"기타{max(nums)+1}" if nums else "기타1"

def next_platform_name(existing: dict, base_name: str) -> str:
    if base_name not in existing:
        return base_name

    nums = []
    if base_name in existing:
        nums.append(1)

    for key in existing.keys():
        if key.startswith(base_name):
            suffix = key[len(base_name):]
            if suffix.isdigit():
                nums.append(int(suffix))

    n = 2
    while n in nums:
        n += 1
    return f"{base_name}{n}"

def get_platform_candidates(user_data: dict, base_name: str):
    candidates = []
    for key, value in user_data.items():
        if key == base_name:
            candidates.append((key, value))
        elif key.startswith(base_name):
            suffix = key[len(base_name):]
            if suffix.isdigit():
                candidates.append((key, value))
    # 🐛 [버그 수정] 예전 정렬 키는 `(len(x), x)`였는데, x가 (키, 값) 튜플이라 len(x)는
    # 언제나 2였어요. 즉 길이 조건이 통째로 죽어 있었고 실제로는 키·값을 사전순으로만
    # 정렬해서 Riot → Riot10 → Riot2 순이 됐습니다. 이 순서는 그냥 보기 나쁜 정도가 아니라,
    # 후보가 여러 개일 때 `candidates[0]`을 "기본으로 고칠 항목"으로 쓰는 곳들
    # (/아이디 수정, 자동등록 변경 승인)이 엉뚱한 항목을 집게 만들어요.
    # 키 길이를 먼저 보면 Riot → Riot2 → Riot10 으로 번호순이 제대로 나옵니다.
    return sorted(candidates, key=lambda kv: (len(kv[0]), kv[0]))

async def respond_modify(inter, target_user, target, old, new, is_misc: bool):
    # inter.user 대신 실제 수정된 대상인 target_user.mention을 사용합니다.
    text = f"✅ {target_user.mention}님의 {target} 아이디가 {old} → {new}로 수정되었어요!"
    if is_misc:
        await inter.followup.send(f"{text} {bot_name()}{josa(bot_name(), '이가')} 깔끔하게 바꿨어요~ 💖")
    else:
        await inter.followup.send(text)

async def notify_log(interaction: discord.Interaction, user: discord.User, target: str, old: str, game_id: str):
    await send_log_embed(
        interaction.client, "id_log",
        f"{interaction.user.mention} 님이 {user.mention}님의 `{target}` 아이디를 수정했어요!",
        fields=[("변경 전", f"`{old}`", True), ("변경 후", f"`{game_id}`", True)],
        guild=interaction.guild,
    )

def find_guild_member_by_name(guild: discord.Guild, name: str) -> Optional[discord.Member]:
    """닉네임/유저명으로 서버 멤버를 최대한 유사하게 찾아줍니다. (위키/아이디 조회 도구, 아이디 일괄가져오기가 공용으로 사용)"""
    target = name.strip().lstrip('@')
    if not target:
        return None
    # 1순위: 서버 닉네임 또는 유저명이 정확히 일치
    for m in guild.members:
        if m.display_name == target or m.name == target:
            return m
    # 2순위: 부분 일치 (대소문자 무시)
    target_lower = target.lower()
    for m in guild.members:
        if target_lower in m.display_name.lower() or target_lower in m.name.lower():
            return m
    return None

MENTION_USER_RE = re.compile(r"^<@!?(\d+)>$")

def extract_id_from_mention(mention: str) -> Optional[str]:
    m = MENTION_USER_RE.match(mention.strip())
    return m.group(1) if m else None

# 🆔 [신규] 예전에 수기로 관리하던 "아이디 목록" 게시글(```ansi 코드블록)을
# 파싱해서 {이름: {플랫폼: 아이디}} 구조로 뽑아내는 함수입니다. (/아이디 가져오기용)
#
# 🐛 [버그 수정] 예전엔 색상 코드(`[2;41m`)만 지우고 그 앞의 ESC 문자(\u001b)는
# 그대로 남겼어요. 명단은 "\u001b[2;41m대장\u001b[0m" 형태로 올라가니까, 디스코드에서
# 원문을 복사해 .txt로 올리면 이름이 "\u001b홍길동\u001b"로 들어옵니다. 그러면
# find_guild_member_by_name이 아무도 못 찾아 전원이 매칭 실패로 떨어지고, 등급 헤더도
# 인식이 안 돼 사람 이름으로 새요. 이제 ESC까지 같이 먹습니다.
# (ESC 없이 색상 코드만 남은 문서도 처리되게 ? 로 선택 처리했어요)
_ANSI_CODE_RE = re.compile(r"\u001b?\[[\d;]*m")
_INVISIBLE_CHARS_RE = re.compile(r"[\u200b-\u200f\u2066-\u2069\ufeff]")
# 🪜 등급 헤더("대장", "4레벨", "3LEVEL"...)를 알아보는 정규식.
# 등급제 자체는 걷어냈지만(parked/core_levels.py.txt), 옛 문서에는 이 줄이 그대로
# 들어 있어요. **읽어서 버리려고** 남겨둡니다. 안 그러면 등급 이름이 사람 이름으로
# 오인돼서 "4레벨"이라는 멤버가 등록될 수 있어요.
_LEVEL_HEADER_RE = re.compile(r"^(대장|\(?(\d)\s*레벨\)?|(\d)\s*LEVEL)$", re.IGNORECASE)
_NAME_HEADER_RE = re.compile(r"^\[([^\[\]]+)\]$")

def _section_lookup_key(text) -> str:
    """구획 제목 비교용 키. 공백과 대소문자 차이를 무시해요."""
    return re.sub(r"\s+", "", str(text)).casefold()


def parse_legacy_id_document(text: str, section_labels=()) -> dict:
    """예전에 쓰던 아이디 목록 게시글 원문을 파싱합니다.
    반환값: {"이름": {"플랫폼": "값", ...}, ...}

    section_labels — 구획 제목으로 볼 이름들. 보통 guild.json에 적어둔 등급 이름
                     (RANKS의 label)을 그대로 넘겨요. 등급을 안 쓰면 비워두면 됩니다.

    🗑️ [정리] 예전엔 등급별로 나눠서 {레벨: {이름: {...}}} 2단 구조를 돌려줬어요.
    등급제를 걷어내면서(parked/core_levels.py.txt) 평평한 1단 구조가 됐습니다.
    등록은 사람 단위라 구획 정보가 쓰이는 곳이 없고, 명단의 등급은 문서가 아니라
    **디스코드 역할**을 보고 정해지거든요. 그래서 1단 구조를 유지합니다.

    구획 제목 줄은 알아보고 **건너뜁니다.** 옛 문서를 그대로 올려도 등급 이름이
    사람으로 잘못 등록되지 않아요.

    ⚠️ 아래 _LEVEL_HEADER_RE는 원본 서버의 등급 이름('대장'·'4레벨'·'3 LEVEL')이
       박혀 있는 정규식이에요. 이미 그 형식으로 쌓인 문서가 있어서 남겨둡니다.
       다른 이름을 쓰는 서버는 section_labels로 알려주세요. 안 알려주면 그 제목이
       `[이름]` 꼴이 아니라서 그냥 무시되긴 하지만, 제목을 대괄호로 감싼 문서라면
       사람으로 잘못 잡힙니다."""
    known_sections = {
        _section_lookup_key(label) for label in section_labels if str(label).strip()
    }
    result: dict = {}
    current_name = None

    for raw_line in text.split("\n"):
        # 코드블록 펜스(```ansi, ```)나 마크다운 헤더(#로 시작) 줄은 건너뜀
        stripped_fence = raw_line.strip()
        if stripped_fence.startswith("```") or stripped_fence.startswith("#"):
            continue

        line = _ANSI_CODE_RE.sub("", raw_line)
        line = _INVISIBLE_CHARS_RE.sub("", line).strip()
        if not line:
            continue

        # 구획 제목 줄은 이름이 아니므로 그냥 버려요. (아래 이름 인식으로 새지 않게)
        # 제목이 `[4레벨]`처럼 대괄호에 싸여 있을 수도 있어서 벗겨보고 한 번 더 봅니다.
        bracket = _NAME_HEADER_RE.match(line)
        candidate = bracket.group(1).strip() if bracket else line
        if _LEVEL_HEADER_RE.match(candidate) or _section_lookup_key(candidate) in known_sections:
            current_name = None
            continue

        name_match = _NAME_HEADER_RE.match(line)
        if name_match:
            current_name = name_match.group(1).strip()
            if current_name:
                result.setdefault(current_name, {})
            continue

        if current_name and ":" in line:
            label, _, value = line.partition(":")
            label, value = label.strip(), value.strip()
            if label and value:
                result[current_name][label] = value

    # 아이디가 하나도 안 딸린 이름은 등록할 게 없으니 빼요.
    return {name: ids for name, ids in result.items() if ids}

# 🧹 [신규] 백그라운드 자동 삭제 유틸리티
# asyncio.create_task로 만든 태스크는 어디에도 참조가 없으면 실행 도중 GC될 수 있어서,
# 완료될 때까지 여기에 붙잡아둡니다.
_BACKGROUND_TASKS = set()


def schedule_delete(msg, delay: float):
    """메시지를 delay초 뒤에 조용히 삭제합니다. (기다리지 않고 백그라운드로 처리)

    🐛 [버그 수정] 예전엔 안내 메시지를 띄운 자리에서 바로 `await asyncio.sleep(60)`을 했어요.
    그런데 그 코드가 economy_lock 안쪽에 있어서, 잔액 부족 같은 흔한 실패가 한 번 날 때마다
    서버 전체의 송금·출석·주식거래·상점구매가 60초씩 통째로 멈췄습니다.
    이제 삭제 예약만 걸고 즉시 반환하므로 락을 붙잡고 있지 않아요.
    """
    if msg is None:
        return

    async def _later():
        try:
            await asyncio.sleep(delay)
            await msg.delete()
        except Exception:
            pass

    task = asyncio.create_task(_later())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
