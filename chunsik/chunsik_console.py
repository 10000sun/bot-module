"""콘솔 출력 인코딩을 UTF-8로 고정하는 안전장치 한 개.

이 파일은 **표준 라이브러리(sys)만 씁니다.** 다른 chunsik 모듈을 절대 import하지
마세요. 봇도 검사 도구도 "아무것도 출력되기 전에" 이걸 먼저 부르려고 import하는데,
여기서 다른 모듈을 끌어오면 그 모듈이 먼저 print해서 고치려던 사고가 그대로 납니다.

왜 필요한가
-----------
한국어 윈도우에서 파이썬은 콘솔 코드페이지(cp949)를 그대로 출력 인코딩으로 씁니다.
그런데 이 프로젝트는 로그에 이모지를 잔뜩 쓰기 때문에, cp949 콘솔에서는 print 한 줄이
UnicodeEncodeError로 터져요. (cp949에는 이모지 글리프가 아예 없어요)

제일 위험한 건 **import하는 순간 출력하는 코드**입니다.
  - `chunsik_storage.init_json_files()` — "📦 빈 데이터 파일이 자동 생성됐어요"
  - `chunsik_config.report_guild_config()` — "⚠️ 서버 설정(guild.json)에 확인할 것이…"
여기서 터지면 try/except나 다운 알림 웹훅에 닿기도 전에 죽어서, 봇이 왜 안 켜졌는지
아무도 모르는 상태가 됩니다. 그래서 **import보다 먼저** 불러야 해요.

errors="replace"까지 붙인 이유는, 혹시 UTF-8로 못 바꾸는 환경이어도 글자가 물음표로
바뀔지언정 봇이 죽지는 않게 하려는 겁니다.
"""

import sys


def force_utf8_console() -> None:
    """stdout·stderr를 UTF-8(errors="replace")로 다시 엽니다. 실패해도 조용히 넘어가요.

    stderr까지 손대는 이유: 파이썬은 처리되지 않은 예외의 트레이스백을 stderr로 찍습니다.
    stdout만 고쳐두면, 정작 무엇이 잘못됐는지 알려주는 트레이스백이 이모지 한 글자 때문에
    통째로 사라져요. (원래 예외 대신 UnicodeEncodeError가 찍힙니다)
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # pythonw처럼 stdout이 아예 없거나(None) 바꿀 수 없는 스트림인 경우예요.
            # 출력 인코딩을 못 바꾼다고 봇을 못 켤 이유는 없으니 조용히 넘어갑니다.
            pass
