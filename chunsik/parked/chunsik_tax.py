"""캠프별 세금 기준과 계산 규칙.

캠프마다 세금 철학이 완전히 달라서, 계산식과 기준값만 여기 따로 모아뒀어요.
(걷는 절차 — 대상 추리기·지갑 차감·통장 적립·결과 표 — 는 세 캠프가 똑같아서 camp.py에 한 벌만 있어요)

🔧 기준값은 코드가 아니라 chunsik_camp_tax.json에 저장돼요.
   캠프장이 `/캠프 세금기준`으로 보고 `/캠프 세금기준수정`으로 직접 고칠 수 있습니다.
   (봇을 재시작하거나 코드를 건드릴 필요가 없어요)

계산 함수는 디스코드와 아무 상관 없는 순수 함수라, 봇을 켜지 않고도 값을 확인할 수 있어요.
"""

import copy
import random

from chunsik_config import CAMP_TAX_FILE
from chunsik_storage import atomic_json_save_or_raise, safe_json_load

# 캠프별 과세 방식. 이건 캠프의 정체성이라 명령어로는 못 바꾸고, 숫자만 조정할 수 있어요.
CAMP_TAX_TYPE = {
    "여백": "progressive",    # 초과 누진세 — 구간을 넘는 만큼에만 그 구간 세율이 붙어요
    "나래": "flat_bracket",   # 구간별 단일세율 — 총자산이 속한 구간의 세율을 전액에 적용
    "악동": "roulette",       # 세금 룰렛 — 참여자는 뽑기, 미참여자는 최고액 고정
}

# ─────────────────────────────────────────────────────────────
# 기본값 (파일이 없거나 항목이 빠졌을 때 쓰는 값)
# ─────────────────────────────────────────────────────────────
DEFAULT_TAX_CONFIG = {
    # 🕊️ 여백 — 지갑에 고인 에바에는 초과 누진세를,
    #    주식에 묻어둔 자산에는 낮은 세율의 '주식 보유세'를 따로 매깁니다.
    #    (지갑세만 걷으니 자산을 전부 주식에 넣어 세금을 피하는 현상이 생겨서 추가됐어요)
    "여백": {
        "exempt": 10_000,                       # 이 금액까지는 비과세
        "brackets": [[100_000, 3.0],            # (구간 상한, 세율%) — 상한 None은 무제한
                     [500_000, 5.0],
                     [None,    10.0]],
        "stock_tax_rate": 1.0,                  # 주식 평가액에 매기는 보유세(%) — 0으로 두면 안 걷어요
        "ticket_min_tax": 100,                  # 이 금액 미만은 무임승차로 보고 티켓 없음
        "ticket_tiers": [[5, 100],              # (이 단계 최대 장수, 1장당 필요 세액)
                         [5, 500],
                         [10, 2_000],
                         [30, 5_000]],
        "ticket_cap": 50,                       # 고래의 확률 독점을 막는 1인당 상한
    },
    # 🌈 나래 — 주식 평가액까지 합쳐서 자산을 보고, 구간 세율을 전액에 적용합니다.
    "나래": {
        "include_stocks": True,
        "brackets": [[100_000, 5.0],
                     [300_000, 7.0],
                     [700_000, 9.0],
                     [None,    10.0]],
        "late_rate": 15.0,                      # 납부 기간을 넘긴 미납자에게 강제 징수할 세율
    },
    # 😈 악동 — 월 1회 룰렛. "많이 걸리면 확률을 조정"할 수 있게 가중치를 그대로 노출했어요.
    "악동": {
        "absent_tax": 30_000,                   # 룰렛에 참여하지 않은 사람이 무는 고정액
        "roulette": [[30_000, 1],               # (세액, 가중치)
                     [20_000, 3],
                     [10_000, 4],
                     [5_000,  3],
                     [1_000,  2]],
    },
}


def load_tax_config() -> dict:
    """저장된 세금 기준을 불러옵니다. 빠진 항목은 기본값으로 채워요."""
    saved = safe_json_load(CAMP_TAX_FILE, {})
    config = copy.deepcopy(DEFAULT_TAX_CONFIG)
    if isinstance(saved, dict):
        for camp, values in saved.items():
            if camp in config and isinstance(values, dict):
                config[camp].update(values)
    return config


def save_tax_config(config: dict):
    atomic_json_save_or_raise(CAMP_TAX_FILE, config, indent=2)


# ─────────────────────────────────────────────────────────────
# 세액 계산
# ─────────────────────────────────────────────────────────────
def _bracket_rate(brackets: list, amount: int) -> float:
    for upper, rate in brackets:
        if upper is None or amount <= upper:
            return rate / 100
    return brackets[-1][1] / 100


def progressive_tax(cfg: dict, balance: int) -> int:
    """초과 누진세. (여백)

    ⚠️ 10만 에바를 가졌다고 전액에 3%가 아니라, 면세점(1만)을 넘는 9만에만 3%가 붙어요.

    >>> progressive_tax(DEFAULT_TAX_CONFIG["여백"], 1_790_000)   # 캠프 공지의 산출 예시
    151700
    """
    exempt = cfg.get("exempt", 0)
    if balance <= exempt:
        return 0

    tax = 0.0
    lower = exempt
    for upper, rate in cfg["brackets"]:
        if upper is None or balance <= upper:
            tax += (balance - lower) * (rate / 100)
            break
        tax += (upper - lower) * (rate / 100)
        lower = upper
    return int(tax)      # 원 단위 절사


def stock_holding_tax(cfg: dict, stock_value: int) -> int:
    """주식 보유세. (여백)

    지갑세와 별개로, 주식 평가액에 낮은 세율을 매겨요.
    자산을 전부 주식에 묻어두면 세금이 0이 되던 구멍을 막는 세목입니다.
    """
    rate = cfg.get("stock_tax_rate", 0)
    if stock_value <= 0 or rate <= 0:
        return 0
    return int(stock_value * rate / 100)


def flat_bracket_tax(cfg: dict, total_assets: int) -> int:
    """구간별 단일세율. (나래)"""
    if total_assets <= 0:
        return 0
    return int(total_assets * _bracket_rate(cfg["brackets"], total_assets))


def roulette_tax(cfg: dict, participated: bool, rng: random.Random = None) -> int:
    """세금 룰렛. (악동) 참여하지 않았으면 최고액 고정."""
    if not participated:
        return int(cfg.get("absent_tax", 0))
    rng = rng or random
    amounts = [int(a) for a, _ in cfg["roulette"]]
    weights = [w for _, w in cfg["roulette"]]
    return rng.choices(amounts, weights=weights, k=1)[0]


def lottery_tickets(cfg: dict, tax: int) -> int:
    """낸 세금으로 받을 로또 티켓 수. (여백)

    세금을 많이 낼수록 티켓 얻는 효율이 계단식으로 떨어져요.
    """
    if tax < cfg.get("ticket_min_tax", 0):
        return 0

    tickets = 0
    remaining = tax
    for max_count, cost in cfg.get("ticket_tiers", []):
        if cost <= 0 or remaining < cost:
            break
        earned = min(max_count, remaining // cost)
        tickets += earned
        remaining -= earned * cost
    return min(tickets, cfg.get("ticket_cap", tickets))


# ─────────────────────────────────────────────────────────────
# 사람이 읽고 고칠 수 있는 텍스트 ↔ 설정 변환
#   슬래시 명령어 인자로는 구간표 같은 걸 받기가 너무 불편해서,
#   창(모달)에 지금 값을 그대로 띄워주고 고쳐서 제출하는 방식을 씁니다.
# ─────────────────────────────────────────────────────────────
def _fmt_num(value) -> str:
    """10.0 → "10", 7.5 → "7.5" (불필요한 소수점 제거)"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _fmt_pairs(pairs: list) -> str:
    return ", ".join(f"{'*' if a is None else _fmt_num(a)}:{_fmt_num(b)}" for a, b in pairs)


def config_to_text(camp: str, cfg: dict) -> str:
    """지금 설정을 편집 가능한 텍스트로 만듭니다."""
    kind = CAMP_TAX_TYPE.get(camp)
    if kind == "progressive":
        return (f"면세점 = {_fmt_num(cfg['exempt'])}\n"
                f"구간 = {_fmt_pairs(cfg['brackets'])}\n"
                f"주식보유세 = {_fmt_num(cfg.get('stock_tax_rate', 0))}\n"
                f"로또최소세액 = {_fmt_num(cfg['ticket_min_tax'])}\n"
                f"로또단계 = {_fmt_pairs(cfg['ticket_tiers'])}\n"
                f"로또상한 = {_fmt_num(cfg['ticket_cap'])}")
    if kind == "flat_bracket":
        return (f"주식포함 = {'예' if cfg.get('include_stocks') else '아니오'}\n"
                f"구간 = {_fmt_pairs(cfg['brackets'])}\n"
                f"미납세율 = {_fmt_num(cfg['late_rate'])}")
    if kind == "roulette":
        return (f"미참여액 = {_fmt_num(cfg['absent_tax'])}\n"
                f"룰렛 = {_fmt_pairs(cfg['roulette'])}")
    return ""


def _parse_pairs(raw: str, label: str, allow_star: bool) -> list:
    pairs = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"`{label}`의 `{chunk}` 부분은 `숫자:숫자` 형식이어야 해요.")
        left, right = (part.strip() for part in chunk.split(":", 1))
        try:
            first = None if (allow_star and left == "*") else float(left)
            second = float(right)
        except ValueError:
            raise ValueError(f"`{label}`의 `{chunk}` 부분에 숫자가 아닌 값이 있어요.")
        if second < 0:
            raise ValueError(f"`{label}`에 음수가 들어 있어요: `{chunk}`")
        pairs.append([None if first is None else int(first), second])
    if not pairs:
        raise ValueError(f"`{label}`이 비어 있어요.")
    return pairs


def _check_brackets(brackets: list):
    if brackets[-1][0] is not None:
        raise ValueError("`구간`의 마지막 칸은 상한 없음(`*`)이어야 해요. 예) `500000:5, *:10`")
    uppers = [u for u, _ in brackets[:-1]]
    if any(u is None for u in uppers):
        raise ValueError("`구간`에서 `*`는 마지막 칸에만 쓸 수 있어요.")
    if uppers != sorted(uppers) or len(set(uppers)) != len(uppers):
        raise ValueError("`구간`의 상한은 작은 값부터 순서대로, 겹치지 않게 적어주세요.")
    for _, rate in brackets:
        if not (0 <= rate <= 100):
            raise ValueError(f"세율은 0~100 사이여야 해요. (입력값: {_fmt_num(rate)})")


def text_to_config(camp: str, text: str) -> dict:
    """편집한 텍스트를 설정으로 되돌립니다. 형식이 잘못되면 ValueError를 던져요."""
    values = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"`{line}` 줄은 `항목 = 값` 형식이어야 해요.")
        key, raw = (part.strip() for part in line.split("=", 1))
        values[key] = raw

    def need(key: str) -> str:
        if key not in values:
            raise ValueError(f"`{key}` 항목이 빠졌어요.")
        return values[key]

    def need_int(key: str) -> int:
        try:
            return int(float(need(key)))
        except ValueError:
            raise ValueError(f"`{key}`에는 숫자를 적어주세요. (입력값: {values[key]})")

    kind = CAMP_TAX_TYPE.get(camp)
    if kind == "progressive":
        brackets = _parse_pairs(need("구간"), "구간", allow_star=True)
        _check_brackets(brackets)
        try:
            stock_rate = float(need("주식보유세"))
        except ValueError:
            raise ValueError(f"`주식보유세`에는 숫자를 적어주세요. (입력값: {values['주식보유세']})")
        if not (0 <= stock_rate <= 100):
            raise ValueError("`주식보유세`는 0~100 사이여야 해요. (0으로 두면 주식에는 세금을 안 걷어요)")

        cfg = {
            "exempt": need_int("면세점"),
            "brackets": brackets,
            "stock_tax_rate": stock_rate,
            "ticket_min_tax": need_int("로또최소세액"),
            "ticket_tiers": [[int(a), int(b)] for a, b in
                             _parse_pairs(need("로또단계"), "로또단계", allow_star=False)],
            "ticket_cap": need_int("로또상한"),
        }
        if cfg["exempt"] < 0:
            raise ValueError("`면세점`은 0 이상이어야 해요.")
        if any(cost <= 0 for _, cost in cfg["ticket_tiers"]):
            raise ValueError("`로또단계`의 1장당 세액은 1 이상이어야 해요.")
        return cfg

    if kind == "flat_bracket":
        brackets = _parse_pairs(need("구간"), "구간", allow_star=True)
        _check_brackets(brackets)
        late = float(need("미납세율"))
        if not (0 <= late <= 100):
            raise ValueError("`미납세율`은 0~100 사이여야 해요.")
        return {
            "include_stocks": need("주식포함") in ("예", "O", "o", "true", "True", "1", "포함"),
            "brackets": brackets,
            "late_rate": late,
        }

    if kind == "roulette":
        roulette = [[int(a), int(b)] for a, b in
                    _parse_pairs(need("룰렛"), "룰렛", allow_star=False)]
        if sum(w for _, w in roulette) <= 0:
            raise ValueError("`룰렛`의 가중치 합이 0이에요. 최소 하나는 1 이상이어야 해요.")
        if any(w < 0 for _, w in roulette):
            raise ValueError("`룰렛`의 가중치는 0 이상이어야 해요.")
        return {"absent_tax": need_int("미참여액"), "roulette": roulette}

    raise ValueError(f"`{camp}`은(는) 세금 규칙이 정해지지 않은 캠프예요.")


# ─────────────────────────────────────────────────────────────
# 안내문
# ─────────────────────────────────────────────────────────────
def rule_summary(camp: str, cfg: dict) -> str:
    """지금 기준을 사람이 읽기 좋게 풀어 씁니다."""
    kind = CAMP_TAX_TYPE.get(camp)

    if kind == "progressive":
        lines = ["💰 **지갑세 — 초과 누진세**",
                 f"└ {cfg['exempt']:,} 에바까지 비과세"]
        lower = cfg["exempt"]
        for upper, rate in cfg["brackets"]:
            span = f"{lower:,} ~ {upper:,}" if upper is not None else f"{lower:,} 초과"
            lines.append(f"└ {span} 구간: **{_fmt_num(rate)}%**")
            lower = upper if upper is not None else lower

        stock_rate = cfg.get("stock_tax_rate", 0)
        if stock_rate > 0:
            lines.append(f"\n📈 **주식 보유세 — 주식 평가액의 {_fmt_num(stock_rate)}%**")
            lines.append("└ 자산을 주식에 묻어둬도 세금을 피할 수 없어요.")
            lines.append("└ 세금은 지갑에서 빠지고, 지갑이 모자라면 미납으로 남아요. (주식 국유화는 아직 미도입)")
        else:
            lines.append("\n📈 주식 보유세: **없음** (주식에는 세금을 안 걷는 중)")

        tiers = " / ".join(f"{int(n)}장까지 {int(c):,}에바당 1장" for n, c in cfg["ticket_tiers"])
        lines.append(f"\n🎲 로또: 세금 {cfg['ticket_min_tax']:,} 이상부터 · {tiers} · 1인 최대 {cfg['ticket_cap']}장")
        return "\n".join(lines)

    if kind == "flat_bracket":
        base = "지갑 + 주식 평가액" if cfg.get("include_stocks") else "지갑 잔액만"
        lines = [f"**구간별 단일세율** · {base} 합산 과세", ""]
        lower = 0
        for upper, rate in cfg["brackets"]:
            span = f"{lower:,} ~ {upper:,}" if upper is not None else f"{lower:,} 초과"
            lines.append(f"└ {span}: 전액의 **{_fmt_num(rate)}%**")
            lower = upper if upper is not None else lower
        lines.append(f"└ 기간 내 미납 시: **{_fmt_num(cfg['late_rate'])}%** 강제 징수")
        return "\n".join(lines)

    if kind == "roulette":
        total = sum(w for _, w in cfg["roulette"]) or 1
        lines = ["**세금 룰렛** · 참여자는 뽑기, 미참여자는 최고액 고정", ""]
        for amount, weight in cfg["roulette"]:
            lines.append(f"└ {int(amount):,} 에바 — {weight}/{total} ({weight / total * 100:.1f}%)")
        expected = sum(a * w for a, w in cfg["roulette"]) / total
        lines.append(f"\n📊 참여 시 평균 예상 세액: 약 **{int(expected):,} 에바**")
        lines.append(f"🚫 미참여 시: **{cfg['absent_tax']:,} 에바** 고정")
        return "\n".join(lines)

    return "규칙이 지정되지 않은 캠프예요."
