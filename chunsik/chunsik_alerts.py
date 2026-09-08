"""봇이 죽거나 연결이 끊겼을 때 관리자 웹훅으로 알리는 기능."""

import asyncio
import json
import time
import traceback
import urllib.request
import datetime as dt

from chunsik_config import ALERT_MENTION, ALERT_TIMEOUT, ALERT_WEBHOOK_URL
from chunsik_names import bot_name

def send_alert_sync(title: str, description: str, color: int = 0xE74C3C) -> None:
    """관리자 웹훅으로 상태 알림을 보냅니다. (동기 버전 — 이벤트 루프 없이도 동작)

    봇이 크래시로 죽는 순간에는 이벤트 루프가 이미 무너져 있을 수 있어서,
    async 대신 표준 라이브러리 urllib으로 그 자리에서 바로 보냅니다.
    알림 실패가 봇을 죽이면 본말전도이므로 모든 예외를 삼킵니다.
    """
    if not ALERT_WEBHOOK_URL:
        return
    try:
        embed = {
            "title": title[:256],
            "description": description[:4000],
            "color": color,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        payload = {"username": f"{bot_name()} 상태 알림", "embeds": [embed]}
        if ALERT_MENTION:
            payload["content"] = ALERT_MENTION
            payload["allowed_mentions"] = {"parse": ["roles", "users"]}

        req = urllib.request.Request(
            ALERT_WEBHOOK_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "ChunsikBot-Alert/1.0"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=ALERT_TIMEOUT).close()
    except Exception as e:
        print(f"⚠️ 다운 알림 전송 실패: {type(e).__name__}: {e}")


async def send_alert(title: str, description: str, color: int = 0xE74C3C) -> None:
    """다운 알림의 비동기 버전. 전송을 별도 스레드로 넘겨 이벤트 루프를 막지 않아요."""
    try:
        await asyncio.to_thread(send_alert_sync, title, description, color)
    except Exception as e:
        print(f"⚠️ 다운 알림 전송 실패: {type(e).__name__}: {e}")


# ========== 🔁 같은 오류로 계속 죽는 루프 다루기 ==========
#
# 🚨 [버그] report_loop_error는 루프를 곧바로 되살립니다. 원인이 한 번 지나가는 것이면
#    그게 맞아요. 그런데 원인이 **남아 있는 것**이면(데이터 파일 손상, start 값이 깨진
#    모집글 하나) 되살아난 루프가 다음 회차에 똑같이 죽습니다. 그리고 그때마다
#    관리자에게 알림이 한 통씩 나가요.
#
#      gpt 루프 10초 · 레벨 저장 30초 · 연결 감시 30초 · 파티/내전/스누즈 1분
#
#    10초 루프면 **하루 8,640통**입니다. 정작 알림이 필요한 다른 사고가 그 사이에 파묻혀요.
#    되살리는 것 자체도 10초마다 같은 예외를 다시 만드는 헛돌기가 됩니다.
#
# 그래서 ①같은 오류의 알림은 간격을 두고 ②되살리기는 점점 늦춥니다.
# 오류가 **달라지거나** 한동안 조용했으면 처음부터 다시 세요 — 회복한 걸로 봅니다.
LOOP_ALERT_COOLDOWN = 1800      # 같은 오류가 이어질 때 알림 사이 최소 간격 (초)
LOOP_FAILURE_RESET = 3600       # 이만큼 조용했으면 "회복했다"로 보고 다시 처음부터
LOOP_RESTART_BACKOFF_MAX = 1800  # 되살리기를 늦추는 한도 (초)

_loop_failures = {}   # label -> {"count", "signature", "last_at", "last_alert"}


def loop_failure_state(label: str, signature: str, now: float) -> dict:
    """이 루프의 연속 실패 상태를 갱신하고 돌려줍니다. (테스트에서 그대로 부를 수 있게 분리)"""
    state = _loop_failures.get(label)
    if (state is None or state["signature"] != signature
            or now - state["last_at"] > LOOP_FAILURE_RESET):
        state = {"count": 0, "signature": signature, "last_at": now, "last_alert": None}
        _loop_failures[label] = state
    state["count"] += 1
    state["last_at"] = now
    return state


def loop_restart_delay(count: int) -> int:
    """연속 실패 횟수 → 되살리기를 얼마나 늦출지(초). 첫 번째는 곧바로 되살립니다."""
    if count <= 1:
        return 0
    return min(60 * (2 ** (count - 2)), LOOP_RESTART_BACKOFF_MAX)


async def report_loop_error(loop, label: str, error: BaseException) -> None:
    """백그라운드 루프가 예외로 멈췄을 때 알리고 다시 살립니다.

    🚨 [중요] discord.py의 tasks.loop은 예외가 밖으로 새어나가면 **영구히 멈춥니다.**
    자동 재시작이 없어요. 그래서 파일이 한 번 손상되거나 백신/클라우드 동기화가 파일을
    잠근 순간, 생일 축하나 선착순 이벤트가 조용히 죽은 채로 며칠이 지나갈 수 있습니다.
    (safe_json_load는 손상을 감지하면 일부러 예외를 던지도록 설계돼 있어서 더 잘 걸립니다)

    각 루프의 @루프이름.error 핸들러에서 이 함수를 부르면, 원인을 관리자에게 알리고
    루프를 되살립니다. time= 스케줄 루프는 이번 회차는 놓치지만 다음 회차부터 정상 동작해요.
    """
    detail = f"{type(error).__name__}: {error}"
    state = loop_failure_state(label, detail, time.monotonic())
    count = state["count"]
    print(f"❗ [{label}] 백그라운드 루프가 멈췄어요 ({count}번째): {detail}")
    traceback.print_exception(type(error), error, error.__traceback__)

    # 전달받은 예외 객체에서 직접 뽑아요. format_exc()는 "지금 처리 중인 예외"에 의존해서,
    # except 블록 밖에서 불리면 내용이 통째로 비어버립니다.
    tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))

    now = state["last_at"]
    delay = loop_restart_delay(count)
    # 📢 첫 실패는 바로 알립니다. 같은 오류가 이어지면 간격을 둬요 — 알림이 도배되면
    #    정작 그 사이에 일어난 다른 사고를 못 봅니다.
    if state["last_alert"] is None or now - state["last_alert"] >= LOOP_ALERT_COOLDOWN:
        state["last_alert"] = now
        repeat = (f"\n⚠️ 같은 오류가 **{count}번째** 이어지고 있어요. "
                  f"다음 알림은 빨라도 {LOOP_ALERT_COOLDOWN // 60}분 뒤입니다."
                  if count > 1 else "")
        await send_alert(
            f"⚠️ {label} 루프가 멈췄어요",
            f"**{detail}**\n\n"
            + (f"{delay}초 뒤에 다시 시작합니다." if delay else "자동으로 다시 시작합니다.")
            + " 반복되면 데이터 파일 손상을 확인해 주세요."
            + f"{repeat}\n```\n{tb[-1000:]}\n```",
            color=0xF39C12,
        )

    # ⏳ 같은 오류가 이어지면 되살리기를 점점 늦춥니다.
    #    10초마다 같은 예외를 다시 만드는 헛돌기를 막아요.
    if delay:
        print(f"⏳ [{label}] 같은 오류가 이어져 {delay}초 뒤에 다시 시작해요.")
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise   # 봇이 내려가는 중이면 그대로 끝내요

    try:
        if not loop.is_running():
            loop.start()
            print(f"🔁 [{label}] 루프를 다시 시작했어요.")
    except Exception as e:
        # 되살리기까지 실패하면 재시작 말고는 방법이 없으니 반드시 알립니다.
        print(f"❗ [{label}] 루프 재시작 실패: {type(e).__name__}: {e}")
        await send_alert(
            f"🔴 {label} 루프를 되살리지 못했어요",
            f"**{type(e).__name__}**: {e}\n\n봇을 재시작해야 이 기능이 복구됩니다.",
        )
