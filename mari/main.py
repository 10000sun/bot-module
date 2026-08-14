"""봇 진입점. 실제 기능은 mari_*.py와 cogs/ 아래에 나뉘어 있어요.

실행: python main.py
"""

import sys


# 🖥️ [안전장치] 콘솔 출력 인코딩을 UTF-8로 고정합니다.
#
# ⚠️ 이 블록은 **다른 봇 모듈을 import하기 전에** 실행돼야 해요. 아래 설명 참고.
#
# 한국어 윈도우에서 파이썬은 콘솔 코드페이지(cp949)를 그대로 출력 인코딩으로 씁니다.
# 그런데 이 프로젝트는 로그에 이모지를 잔뜩 쓰기 때문에, cp949 콘솔에서는 print 한 줄이
# UnicodeEncodeError로 터져요. (cp949에는 이모지 글리프가 아예 없어요)
#
# 제일 위험한 건 mari_storage.init_json_files()입니다. 이건 **모듈을 import하는 순간**
# 실행되면서 "📦 빈 데이터 파일이 자동 생성됐어요"를 찍어요. 여기서 터지면 아래 try/except나
# 다운 알림 웹훅에 닿기도 전에 죽어서, 봇이 왜 안 켜졌는지 아무도 모르는 상태가 됩니다.
# (main() 안에서 state.load()를 감싸둔 것과 똑같은 이유예요)
#
# 지금 서버 콘솔은 UTF-8이라 잘 돌지만, 작업 스케줄러나 cmd.exe로 실행 방식이 바뀌면 걸립니다.
# errors="replace"까지 붙여서, 혹시 UTF-8로 못 바꾸는 환경이어도 글자가 물음표로 바뀔지언정
# 봇이 죽지는 않게 했어요.
def _force_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # pythonw처럼 stdout이 아예 없거나(None) 바꿀 수 없는 스트림인 경우예요.
            # 출력 인코딩을 못 바꾼다고 봇을 못 켤 이유는 없으니 조용히 넘어갑니다.
            pass


_force_utf8_console()

import asyncio      # noqa: E402  (위 인코딩 설정이 반드시 먼저 실행돼야 해요)
import traceback
import discord

from mari_config import BASE_DIR, DISCORD_TOKEN, intents
from mari_alerts import send_alert_sync
from mari_client import MariBotClient
from mari_state import state
from mari_names import bot_name, josa

bot = MariBotClient(command_prefix="/", intents=intents)

# ========== 🚀 봇 구동부 ==========
async def main():
    # 🔐 [변경] 토큰은 더 이상 코드에 없어요. 같은 폴더의 .env 파일에서 읽어옵니다.
    if not DISCORD_TOKEN:
        print("❌ 봇 토큰이 없어요!")
        print(f"   {BASE_DIR} 안에 .env 파일을 만들고 아래 한 줄을 넣어주세요:")
        print("   DISCORD_TOKEN=여기에_봇_토큰")
        print("   (.env.example 파일을 복사해서 이름만 .env로 바꾸면 편해요)")
        return

    # 🗃️ 아이디 DB를 메모리로 읽어옵니다.
    # 예전엔 mari_state를 import하는 순간 자동으로 읽혔어요. 그런데 ids.json이 손상되면
    # import 도중에 예외가 터져서, 봇이 왜 안 켜졌는지 알리는 웹훅조차 못 보내고 죽었습니다.
    # 이제 여기서 명시적으로 읽어 실패를 붙잡고 관리자에게 알립니다.
    try:
        state.load()
    except Exception as e:
        print(f"❌ 아이디 데이터를 읽을 수 없어요: {type(e).__name__}: {e}")
        send_alert_sync(
            f"🔴 {bot_name()}봇 기동 실패",
            f"`ids.json`을 읽지 못해서 봇을 시작할 수 없어요.\n\n**{type(e).__name__}**: {e}",
        )
        return

    try:
        await bot.start(DISCORD_TOKEN)
    except discord.LoginFailure:
        # 토큰이 재발급되면 예전 토큰은 즉시 무효가 돼요. 가장 흔한 기동 실패 원인입니다.
        print("❌ 봇 토큰이 올바르지 않아요. 디스코드 개발자 포털에서 토큰을 다시 확인해 주세요.")
        send_alert_sync(
            f"❌ {bot_name()}봇 로그인 실패",
            "봇 토큰이 유효하지 않아요. 토큰이 재발급됐는지 확인하고 .env를 갱신해 주세요.",
        )
    except Exception as e:
        print(f"❗예외 발생! {bot_name()}{josa(bot_name(), '이가')} 깜짝 놀랐어요:", e)
        traceback.print_exc()
        # 🚨 [신규] 예상 못한 종료는 반드시 관리자에게 알립니다.
        send_alert_sync(
            f"🔴 {bot_name()}봇이 예기치 않게 종료됐어요",
            f"**{type(e).__name__}**: {e}\n\n```\n{traceback.format_exc()[-1200:]}\n```",
        )
        # ⚙️ 프로세스를 0이 아닌 코드로 끝내야 systemd/도커의 자동 재시작이 동작해요.
        raise
    finally:
        # 정리 중에 난 오류가 원래 원인을 덮어버리면 디버깅이 어려워지므로 따로 삼킵니다.
        try:
            if not bot.is_closed():
                await bot.close()
        except Exception as close_err:
            print(f"⚠️ 종료 처리 중 오류: {type(close_err).__name__}: {close_err}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # 관리자가 직접 껐을 때. 크래시와 구분해서 알려야 새벽에 헛걸음하지 않아요.
        print(f"\n👋 {bot_name()}봇을 수동으로 종료했어요.")
        send_alert_sync(
            f"🟡 {bot_name()}봇이 수동으로 종료됐어요",
            "관리자가 직접 봇을 껐어요. (Ctrl+C)",
            color=0xF1C40F,
        )
