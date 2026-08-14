"""봇이 죽거나 연결이 끊겼을 때 관리자 웹훅으로 알리는 기능."""

import asyncio
import json
import traceback
import urllib.request
import datetime as dt

from mari_config import ALERT_MENTION, ALERT_TIMEOUT, ALERT_WEBHOOK_URL
from mari_names import bot_name

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
            headers={"Content-Type": "application/json", "User-Agent": "MariBot-Alert/1.0"},
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
    print(f"❗ [{label}] 백그라운드 루프가 멈췄어요: {detail}")
    traceback.print_exception(type(error), error, error.__traceback__)

    # 전달받은 예외 객체에서 직접 뽑아요. format_exc()는 "지금 처리 중인 예외"에 의존해서,
    # except 블록 밖에서 불리면 내용이 통째로 비어버립니다.
    tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))

    await send_alert(
        f"⚠️ {label} 루프가 멈췄어요",
        f"**{detail}**\n\n자동으로 다시 시작합니다. 반복되면 데이터 파일 손상을 확인해 주세요.\n"
        f"```\n{tb[-1000:]}\n```",
        color=0xF39C12,
    )

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
