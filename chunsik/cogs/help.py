"""ChunsikHelp — 카테고리별 도움말."""

import discord
from discord import app_commands
from discord.ext import commands

from chunsik_config import module_active
from chunsik_settings import FEATURE_LIST_TEXT, _get_role_ids, load_settings
from chunsik_utils import ChunsikView
from chunsik_names import bot_name, currency, event_name, josa

class HelpCategorySelect(discord.ui.Select):
    """도움말 카테고리를 고르면 그 카테고리 내용만 딱 보여주는 드롭다운"""
    def __init__(self, categories: dict, embed_color: int):
        self.categories = categories
        self.embed_color = embed_color
        options = [
            discord.SelectOption(label=name, emoji=emoji, description=f"{name} 관련 명령어 보기")
            for name, (emoji, _content) in categories.items()
        ]
        super().__init__(placeholder="📂 카테고리를 선택해서 명령어를 확인하세요", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        name = self.values[0]
        emoji, content = self.categories[name]
        embed = discord.Embed(title=f"{emoji} {name}", description=content, color=self.embed_color)
        embed.set_footer(text="🔽 아래 메뉴에서 다른 카테고리도 골라보세요.")
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(ChunsikView):
    def __init__(self, categories: dict, embed_color: int):
        super().__init__(timeout=180)
        self.add_item(HelpCategorySelect(categories, embed_color))


class ChunsikHelp(commands.Cog):
    """봇 전용 명령어 도움말 시스템"""
    def __init__(self, bot):
        self.bot = bot

    # 📂 [가독성 개선] 카테고리별로 내용을 분리해두고, 드롭다운으로 하나씩만 보여줍니다.
    # (예전엔 임베드 하나에 필드 7~8개를 전부 우겨넣어서 스크롤이 길고 한눈에 안 들어왔어요)
    # 📌 [변경] 예전엔 클래스 변수(ADMIN_CATEGORIES/USER_CATEGORIES)였어요. 그러면 문구가
    # **봇이 켜질 때 한 번** 만들어져서 굳습니다. 도움말에는 재화·봇·이벤트 이름이 잔뜩
    # 들어가는데, `/초기설정`으로 이름을 바꿔도 도움말만 옛 이름으로 남아 있었어요.
    # 이제 명령을 부를 때마다 새로 만듭니다. (도움말은 자주 열리는 명령이 아니라
    #  매번 만드는 비용이 문제되지 않아요)
    # 🧩 [신규] 줄마다 "어느 모듈 것인지"를 달아둡니다. 담지 않은 모듈의 줄은 빼고,
    # 그래서 통째로 빈 카테고리는 목록에서도 사라져요. (_visible 참고)
    #
    # 예전엔 이 문구들이 **어떤 모듈이 올라왔는지 보지 않는 고정 문자열**이었어요.
    # 그래서 shop+birthday만 담아 납품하면 `/도움말`이 없는 명령 37개를 안내했습니다.
    # 납품할 때마다 사람이 손으로 도움말을 고쳐야 했고, 그게 계속 빠졌어요.
    #
    # 태그 규칙 — None이면 항상 보임(코어 모듈 것), 문자열이면 그 모듈이 담겼을 때만,
    # 튜플이면 그중 **하나라도**(OR), 리스트면 **전부**(AND). 중첩할 수 있어요.
    # 어느 모듈이 무슨 명령을 데려오는지는 modules.py에 적혀 있고,
    # `python tools/check_help.py`가 도움말과 실제 명령을 대조해줍니다.

    # 🚧 /기능제어는 정지할 기능이 하나도 없으면 명령 자체가 사라져요.
    #    (cogs/setting.py의 _prune_module_commands) 기능 키를 데려오는 모듈들입니다.
    _FEATURE_OWNERS = ("id", "economy", "stock", "wiki", "shop", "birthday", "snooze", "selfrole", "welcome", "levels", "party")

    def _visible(self, categories: dict) -> dict:
        """모듈 구성에 맞춰 걸러낸 `{이름: (이모지, 본문)}`.

        빈 카테고리는 아예 빼요. 드롭다운 선택지로도 안 나가야 하니까요.
        (선택지가 0개면 디스코드가 거부하는데, 도움말 모듈은 코어라 '서버/시스템 관리'
         카테고리가 항상 남습니다. 그래도 만약을 대비해 아래에서 한 번 더 봐요)
        """
        result = {}
        for name, (emoji, lines) in categories.items():
            kept = [text for owner, text in lines if self._owner_active(owner)]
            if kept:
                result[name] = (emoji, "\n".join(kept))
        return result

    @classmethod
    def _owner_active(cls, owner) -> bool:
        """그 줄을 보여줄지. None=항상, 문자열=그 모듈, 튜플=하나라도(OR), 리스트=전부(AND).

        중첩할 수 있어요. `["gpt", ("economy", "stock")]` 은 "gpt가 있고, 그리고
        지갑이나 주식 중 하나는 있을 때"라는 뜻입니다.
        """
        if owner is None:
            return True
        if isinstance(owner, str):
            return module_active(owner)
        if isinstance(owner, tuple):
            return any(cls._owner_active(o) for o in owner)
        return all(cls._owner_active(o) for o in owner)

    def _admin_categories(self):
        """관리자용 도움말 카테고리. (담긴 모듈 것만)"""
        return self._visible(self._admin_entries())

    def _admin_entries(self):
        """걸러내기 **전**의 관리자용 원본. 모듈 태그가 그대로 붙어 있어요.
        (tools/check_modules.py가 태그를 검사하려고 씁니다)"""
        return {
            "서버/시스템 관리": ("🛡️", [
                (None, "`/초기설정` - 재화·봇·이벤트·서버 이름 지정 (봇을 처음 들였을 때 제일 먼저. 나중에 다시 바꿔도 돼요)"),
                (None, "`/설치 시작` - **처음이면 여기부터.** 설명을 보면서 하나씩 정해요. 이미 쓰는 채널이 있으면 그대로 씁니다"),
                (None, "`/설치 점검` - 아직 지정 안 된 채널·역할 확인 (아무것도 만들지 않아요)"),
                (None, "`/설치 자동생성` - 고르는 과정 없이 빠진 것을 전부 새로 만들어요 (만들기 전에 목록을 보여주고 확인을 받아요)"),
                # ⚠️ 여기에 기능 이름을 예로 들지 마세요. 이 줄은 항상 보이는데(태그 None),
                #    "주식은 주식끼리"라고 적었다가 주식을 안 담은 서버에서 걸렸어요.
                (None, "└ 성격이 비슷한 채널끼리 **카테고리를 나눠** 담아요. `접두사`를 주면 카테고리 이름 앞에 붙습니다"),
                (None, "`/설정 관리자` - 기능별 관리자 역할 지정"),
                (None, "`/설정 채널` - 시스템/로그 전용 채널 맵핑"),
                (None, "`/설정 채널지정내역` - 지금 지정돼 있는 채널·관리자 역할을 한눈에 확인"),
                # 💡 기능 나열을 넣지 마세요. 담지 않은 기능까지 안내되고, 기능이 늘 때마다
                #    고쳐야 해요. (`/통계`는 예전에 '주식 현황'까지 적어뒀는데, 주식을 안 담은
                #    서버에서는 그 칸이 아예 안 붙습니다 — cogs/economy.py의 server_stats)
                ("economy", f"`/통계` - 출석률·{currency()} 유통량·거래 활발도 요약"),
                (None, "`/감사로그` - 담긴 기능의 로그를 한 번에 모아 최신순으로 조회"),
                (self._FEATURE_OWNERS,
                 f"`/기능제어 정지`·`재개`·`전체정지`·`전체재개`·`상태` - {FEATURE_LIST_TEXT} 긴급 정지·재개 (서버 관리자 또는 대장 전용)"),
            ]),
            "게임 아이디 관리": ("🎮", [
                ("id", "`/아이디 등록`, `/아이디 수정`, `/아이디 삭제`, `/아이디 전체조회`"),
                ("id", f"`/설정 채널 아이디등록` - 이 채널에 유저가 올린 '플랫폼 아이디'를 {bot_name()}{josa(bot_name(), '이가')} 자동으로 등록/삭제 처리"),
                ("id", "└ 맨 앞에 `변경` 또는 `수정`을 붙이면(예: `변경 라이엇 새아이디#kr1`) 무조건 관리자 승인을 거쳐서 같은 플랫폼의 기존 값을 바꿔줘요"),
                ("id", "`/설정 채널 아이디목록` - 아이디 명단이 자동 갱신될 채널 지정 (👑대장 / 멤버 두 칸으로 올라가요. `guild.json`에 `ranks`를 적으면 등급별로)"),
                ("id", "└ 이 채널에 아이디 관리자가 \"갱신\"이라고 치면 그 자리에서 바로 새로고침돼요"),
                ("id", "`/설정 명단 대장` - 명단에서 '대장' 칸으로 따로 표시할 역할 지정"),
                ("id", "`/아이디 가져오기` - 예전에 쓰던 아이디 목록 게시글(.txt)을 한 번에 등록"),
                ("id", "`/아이디 공지` - 아이디 명단 맨 아래에 공지 한 줄 추가 (기본은 누적, 덮어쓰기 옵션 있음. 아이디 관리자 전용)"),
                ("id", "`/아이디 중복정리` - 실수로 중복 등록된 같은 아이디를 한 번에 정리"),
                ("id", "`/아이디 대기열정리` - 관리자 확인 대기 중인 자동등록 요청을 전부 비우기"),
                ("id", "`/아이디 새로고침` - ids.json을 직접 수정했을 때 메모리 데이터 갱신"),
            ]),
            # 🏷️ 이름도 구성을 따라갑니다. (위 유저용 '지갑 및 상점' 설명 참고)
            ("경제/상점 관리" if module_active("shop") else "경제 관리"): ("💰", [
                ("shop", "`/상점 생성`, `/상점 삭제`, `/상점 설정` - 매대는 **채널마다** 따로 만들 수 있어요 (일반용·역할용 식으로 나누기 좋아요)"),
                ("shop", "`/설정 채널 상점` - 기본 매대를 세울 채널 (`/설치 자동생성`이 이 채널을 만들면 매대까지 세워둬요)"),
                ("shop", "`/상점 항목추가`, `/상점 항목삭제`"),
                ("shop", "`/상점 항목설정` - 새이름·되팔기퍼센트 변경 가능"),
                ("shop", "`/상점 사용` - 유저 인벤토리 아이템 차감"),
                ("shop", "`/상점 정산` - 월별 상점 총 판매 정산 조회"),
                ("economy", f"`/지급` & `/회수` - {currency()} 일괄 통제"),
                ("economy", "`/지급취소` - 최근 지급/회수를 목록에서 골라 되돌리기 (잘못 지급했을 때 복구용)"),
                ("economy", f"`/지갑내역 유저조회` - 특정 유저의 {currency()} 입출금 내역 확인 (문의 대응용)"),
                ("economy", "`/출석보상설정` - 기본 보상 금액 조정"),
                ("economy", "`/지갑청소` - 퇴장 유저 지갑 정리 (지우기 전에 대상 목록을 먼저 보여주고 확인 버튼을 눌러야 실행돼요)"),
                # 📈 주식을 안 담으면 표에 주식 칸이 붙지 않아요. 문구도 같이 따라가야
                #    합니다. (cogs/economy.py의 wallet_command 참고)
                ("economy", "`/지갑 전체:True` - 서버 인원 전원의 "
                            + ("지갑·주식·합계를 자산" if module_active("stock") else "지갑을 잔고")
                            + " 많은 순으로 조회 (예전 랭킹 명령을 대체해요)"),
            ]),
            "주식 특수 관리": ("📈", [
                ("stock", "`/주식 생성`, `/주식 삭제`"),
                ("stock", "`/주식 변동` - 주가 변동 및 사유 예약"),
                ("stock", "`/주식 종가게시` - 일일 장 마감 및 변동 확정 (미변동 종목은 자동 유지)"),
                ("stock", "`/주식 지급`, `/주식 회수` - 특정 주식 강제 지급/회수"),
                ("stock", "`/주식 폐장`, `/주식 개장` - 장 강제 개폐"),
            ]),
            "정보 및 역할 관리": ("👥", [
                (None, "`/역할부여` - 특정 멤버 역할 지급 (최대 25개까지 선택 UI로. 서버 관리자 전용)"),
                ("party", "`/파티 정리` - 끝난 모집 기록 지우기 (열려 있는 모집은 그대로 둬요)"),
                ("party", "`/설정 관리자 파티` - 남의 모집을 마감·정리할 수 있는 역할 지정 (주최자는 언제나 자기 모집을 마감할 수 있어요)"),
                ("levels", "`/레벨 보상설정`·`/레벨 보상삭제` - 그 레벨에 도달하면 자동으로 붙을 역할 (이미 넘긴 사람에겐 소급되지 않아요)"),
                ("levels", "`/레벨 설정` - 메시지당 경험치·쿨다운·레벨업 보상 조정 (파라미터 없이 실행하면 현재 설정 확인)"),
                ("levels", "└ `알림` 항목에서 레벨업 축하를 **말한 채널** / **지정 채널** / **안 올림** 중에 고를 수 있어요"),
                ("levels", "`/레벨 조정` - 특정 멤버의 누적 경험치를 더하거나 빼기 (실수 복구용)"),
                ("levels", "`/설정 채널 레벨알림` - 축하를 한곳에 모을 채널 (`/레벨 설정`에서 '지정 채널'을 골랐을 때 여기로 와요)"),
                ("levels", "`/설정 관리자 레벨` - 위 설정을 다룰 역할 지정"),
                ("welcome", "`/입장 자동역할` - 새로 들어온 멤버에게 저절로 붙을 역할 지정 (여러 개 가능)"),
                ("welcome", "`/입장 인사말` - 환영 문구 지정 (`{멘션}`·`{이름}`·`{서버}`·`{인원}`을 넣으면 실제 값으로 바뀌어요)"),
                ("welcome", "`/입장 규칙패널` - 규칙에 **동의를 누른 사람에게만** 역할을 주는 잠금 장치 (누르기 전엔 자동 역할이 안 나가요)"),
                ("welcome", "`/입장 규칙패널끄기`·`/입장 확인`·`/입장 미리보기`"),
                ("welcome", "`/설정 채널 환영`·`/설정 채널 입퇴장로그` - 인사를 올릴 채널과 들어오고 나간 기록이 남을 채널"),
                ("welcome", "`/설정 관리자 입장` - 위 설정을 다룰 역할 지정"),
                ("selfrole", "`/셀프역할 만들기` - 이 채널에 셀프 역할 패널을 올려요 (제목과 안내 문구만 정하면 됩니다)"),
                ("selfrole", "`/셀프역할 역할추가`·`/셀프역할 역할빼기` - 패널에 담을 역할 관리 (빼도 이미 가진 사람 역할은 그대로예요)"),
                ("selfrole", "`/셀프역할 패널삭제`·`/셀프역할 목록`"),
                ("selfrole", "`/설정 관리자 셀프역할` - 패널을 만들고 관리할 역할 지정"),
                ("selfrole", "└ ⛔ 관리자·역할 관리·밴 같은 권한이 딸린 역할은 패널에 **담기지 않아요.** 아무나 눌러서 가져갈 수 있으니까요"),
            ]),
            "위키 관리": ("📖", [("wiki", "`/위키 등록`, `/위키 수정`, `/위키 삭제`")]),
            # 📜 [의도적 제외] 서버 연대기(/연대기, 우클릭 '연대기에 박제')는 도움말에 싣지 않아요.
            # 조용히 기록만 하고 싶다는 운영 방침이라, 명령어 자체는 살아 있지만 안내에는 안 나옵니다.
            # (권한도 '연대기 관리자'로 따로 걸려 있어서 모르는 사람은 눌러도 막혀요)
            # ⚠️ 나중에 공개하고 싶어지면 여기에 카테고리를 하나 추가하면 됩니다.
            "이벤트 관리": ("🎉", [
                ("games", f"`/이벤트설정` - {event_name()} 이벤트 선착순 인원/금액/지속시간 조정 (파라미터 없이 실행 시 현재 설정 확인)"),
            ]),
            # 🧪 진단은 코어라 명령 자체는 항상 등록돼요. 다만 없는 기능을 점검하는
            #    하위 명령까지 안내할 필요는 없어서 여기서 가립니다.
            #    (명령 자체를 빼는 건 cogs/diagnostics.py가 할 일 — NEXT.md에 적어뒀어요)
            "테스트/진단 도구": ("🧪", [
                (None, "`/설정 관리자 테스트` - 아래 명령어들을 쓸 수 있는 '테스트 관리자' 역할 지정 (서버 관리자는 항상 사용 가능)"),
                (None, "`/테스트 채널점검` - 설정된 채널 전체에 테스트 발송, 문제 있는 채널 리포트"),
                (None, "`/테스트 권한확인` - 본인(또는 지정 유저)이 가진 관리자 권한 확인"),
                ("games", f"`/테스트 이벤트` - {event_name()} 이벤트 창을 짧게 강제로 열어서 전체 흐름 테스트"),
                ("economy", "`/테스트 출석초기화` - 본인 오늘 출석 기록만 지워서 반복 테스트"),
                ("id", "`/테스트 아이디파싱` - 실제 등록 없이 텍스트 파싱 결과만 미리보기"),
                ("gpt", "`/테스트 ai상태` - Gemini RPM/RPD 사용량 및 API 연결 확인"),
                (None, "`/테스트 데이터점검` - 담긴 기능의 데이터를 파일 손상·저장 실패까지 일괄 점검"),
                (None, "`/테스트 명령어동기화` - 슬래시 명령어 강제 재동기화"),
                (None, "`/테스트 백업실행` - 매일 새벽 3시 자동 백업을 지금 즉시 실행"),
                ("stock", "`/테스트 종가게시미리보기` - 실제 마감 없이 종가게시 결과 미리보기"),
            ]),
        }

    def _user_categories(self):
        """일반 유저용 도움말 카테고리. (담긴 모듈 것만)"""
        return self._visible(self._user_entries())

    def _user_entries(self):
        """걸러내기 **전**의 유저용 원본. (위 _admin_entries 설명 참고)"""
        return {
            # 🏷️ 카테고리 **이름**도 구성을 따라가야 해요. 줄만 걸러내면 상점을 안 담은
            #    서버에서 "지갑 및 상점"이라는 제목만 덩그러니 남습니다. (이름은 명령이
            #    아니라서 check_help.py가 못 잡아요 — 실제로 띄워보고 발견했습니다)
            ("지갑 및 상점" if module_active("shop") else "지갑"): ("💰", [
                # 🎒 '소지품'은 상점에서 산 아이템이에요. 상점을 안 담으면 인벤토리 자체가
                #    없어서, 유저가 빈 칸을 찾다가 문의합니다.
                ("economy", "`/지갑` - 잔고 및 소지품 확인" if module_active("shop") else "`/지갑` - 잔고 확인"),
                ("shop", f"└ 🎁 **선물하기** 버튼으로 상점에서 산 아이템을 다른 사람에게 넘겨줄 수 있어요 ({currency()}{josa(currency(), '은는')} 안 나가요)"),
                ("shop", "└ 역할이 지급되는 아이템은 선물이 안 돼요. 되팔기로 정리한 뒤 상대가 직접 사면 됩니다"),
                # 💡 예전엔 "(송금·출석·상점·주식 전부)"라고 출처를 나열했는데, 담지 않은
                #    기능까지 적히더라고요. 어차피 '전부' 보여주니 나열이 필요 없어요.
                ("economy", f"`/지갑내역 조회` - 내 {currency()}{josa(currency(), '이가')} 언제 얼마나 들어오고 나갔는지 전부 확인"),
                ("economy", f"`/송금` - 다른 유저에게 {currency()} 이체"),
                ("economy", "`/출석` - 매일 출석체크 및 보상 획득"),
                ("shop", "상점 매대는 채널마다 따로 있어요! 되팔기 환급 비율은 상품마다 달라요."),
                # 🖱️ 우클릭 앱 안내는 두 모듈에 걸쳐 있어서 문장을 나눠뒀어요.
                # (한 문장에 묶어두면 상점을 안 담은 서버에서 '아이템 선물하기'까지 광고합니다)
                ("economy", "🖱️ 멤버를 **우클릭 → 앱**하면 `송금하기`가 바로 열려요. "
                            "받을 사람을 고르는 단계가 통째로 없어져요! (모바일은 프로필을 꾹 누르면 돼요)"),
                ("shop", "└ 같은 자리에 `아이템 선물하기`도 있어요"),
            ]),
            "주식 거래": ("📈", [
                ("stock", "`/주식 목록` - 상장 종목 및 시세 확인"),
                ("stock", "`/주식 매수` & `/주식 매도` - 주식 거래"),
                ("stock", "`/주식 포폴` - 내 투자 포트폴리오 및 수익률 확인"),
                ("stock", "`/주식 그래프` - 특정 종목의 가격 변동 추이 그래프"),
            ]),
            "정보 조회": ("📖", [
                ("id", "`/아이디 조회` - 다른 멤버의 게임 아이디 조회"),
                ("id", "🖱️ 멤버 **우클릭 → 앱 → 아이디 보기**가 제일 빨라요 (나만 보여요)"),
                ("wiki", "`/위키 조회` - 개별 멤버 위키 확인"),
                ("wiki", "`/위키 목록` - 등록된 위키 전체 조회"),
            ]),
            "파티 모집": ("🎯", [
                ("party", "`/파티 모집` - 제목·인원·시각을 정해 모집글을 올려요 (시각은 `20:00` · `8-25 20:00` 처럼)"),
                ("party", "└ 참가 버튼으로 모이고, 정원이 차면 **대기**로 들어가요. 앞사람이 빠지면 자동으로 올라가고 DM으로 알려드려요"),
                ("party", "└ 시작 10분 전과 시작할 때 참가자를 한 번씩 불러줘요"),
                (["party", "id"], "└ 참가자 목록에 그 사람의 게임 아이디가 같이 붙어요. 모이기 전에 친구 추가를 끝낼 수 있어요"),
                ("party", "`/파티 목록` - 지금 모집 중인 파티 한눈에 보기"),
            ]),
            "활동 레벨": ("📊", [
                ("levels", "채팅을 하면 경험치가 쌓이고 레벨이 올라요. (도배로는 안 올라가요 — 잠깐씩 쉬는 시간이 있어요)"),
                ("levels", "`/레벨 확인` - 내 레벨·경험치·순위 확인 (다른 사람 것도 볼 수 있어요)"),
                ("levels", "`/레벨 순위` - 활동이 많은 사람 순으로 보기"),
            ]),
            "역할 고르기": ("🎚️", [
                ("welcome", "처음 들어오면 안내에 따라 **규칙 동의 버튼**을 눌러주세요. 그래야 서버를 볼 수 있어요. (규칙 패널이 있는 서버만)"),
                ("selfrole", "관리자가 올려둔 패널에서 **버튼을 누르면** 역할이 바로 붙어요. 한 번 더 누르면 떨어지고요."),
                ("selfrole", "└ 알림을 받고 싶은 것만 골라 켜두면 됩니다. 관리자에게 따로 부탁하지 않아도 돼요."),
            ]),
            "생일": ("🎂", [
                ("birthday", "`/생일 등록` - 본인 생일 등록 (예: 년 2000, 월 7, 일 24)"),
                ("birthday", "`/생일 변경` - 등록한 생일 수정"),
                ("birthday", "`/생일 삭제` - 등록한 생일 정보 삭제"),
                ("birthday", "`/생일 확인` - 특정 멤버의 생일 확인"),
                ("birthday", f"생일 당일이 되면 {bot_name()}{josa(bot_name(), '이가')} 축하 채널에 자동으로 알려줘요!"),
            ]),
            "미니게임 및 아이템": ("🎲", [
                ("games", "`/하이로우`, `/하이`, `/로우` - 간단한 예측 게임"),
                ("games", f"매일 00:21, 12:21에 아무 채널에서나 \"{event_name()}\"라고 치면 {currency()}{josa(currency(), '을를')} 받을 수 있어요! (참가자 전원 결과는 이벤트 종료 후 안내 채널에 한 번에 올라와요)"),
            ]),
            "나중에 답장": ("⏰", [("snooze", (
                f"지금 답장 못 하는 메시지를 미뤄두면, 정한 시각에 {bot_name()}{josa(bot_name(), '이가')} **DM으로** 다시 알려줘요.\n"
                "**쓰는 법**: 메시지 **우클릭 → 앱 → 나중에 답장** (모바일은 메시지를 꾹 누르고 → 앱)\n"
                "└ 1시간 뒤 · 3시간 뒤 · 아침 9시 · 점심 12시 · 저녁 6시 · 밤 10시 중에 고르거나 **직접 입력**\n"
                "└ 직접 입력 형식: `30분` · `2시간` · `3일` · `14:00` · `8-10 14:00` (최대 30일)\n"
                "└ 고른 시각이 오늘 이미 지났으면 자동으로 내일로 넘어가요\n"
                "`/스누즈 목록` - 미뤄둔 메시지 확인 (번호도 여기서 봐요)\n"
                "`/스누즈 취소` - 예약 취소 (예: `번호: 7`)\n"
                f"⚠️ {bot_name()} DM을 막아두셨으면 예약이 안 돼요. 개인 메시지를 열어주세요!"
            ))]),
            f"AI {bot_name()} 대화": ("💬", [
                ("gpt", f"{bot_name()}봇 호출 구문이나 전용 역할을 멘션해서 말을 걸어보세요! 이미지를 같이 올리면 {bot_name()}{josa(bot_name(), '이가')} 사진도 보고 대답해줘요."),
                ("gpt", f"{bot_name()}{josa(bot_name(), '이가')} 대화 중 기억해둘 만한 걸 알아서 기억해요. `/기억 목록`으로 확인, `/기억 삭제`·`/기억 초기화`로 지울 수 있어요."),
                # 🧠 지갑·주식 조회 도구는 그 모듈이 함께 켜져 있을 때만 실제로 동작해요. (modules.py의 gpt note)
                #    그래서 "gpt가 있고, 그리고 지갑이나 주식 중 하나는 있을 때"입니다.
                #
                # ⚠️ 줄을 보일지 말지(모듈 태그)와 **어떤 예시를 쓸지**는 다른 문제예요.
                #    예전엔 태그만 "지갑이나 주식 중 하나"로 걸고 문구는 둘 다 나열해서,
                #    주식 없이 미니게임+AI만 담은 서버가 "내 주식 어때?"를 안내받았습니다.
                #    (check_help.py의 낱말 검사가 이걸 잡았어요)
                (["gpt", ("economy", "stock")],
                 "대화 중 "
                 + " ".join(f'"{q}",' for q in
                            (["내 지갑 얼마야?"] if module_active("economy") else [])
                            + (["내 주식 어때?"] if module_active("stock") else [])).rstrip(",")
                 + " 같은 질문도 알아서 대답해줘요 (전부 본인 것만 확인 가능)"),
            ]),
        }

    @app_commands.command(name="도움말", description=f"{bot_name()}{josa(bot_name(), '이가')} 해줄 수 있는 모든 명령어를 보여드려요!")
    @app_commands.describe(관리자="관리자 전용 명령어를 보시려면 True를 선택하세요 (관리자 권한 필요)")
    @app_commands.guild_only()
    async def help_command(self, interaction: discord.Interaction, 관리자: bool = False):
        
        # 🛡️ 권한 체크 로직 (하드코딩 제거 및 JSON 동적 로드 연동)
        is_admin = interaction.user.guild_permissions.administrator
        
        if not is_admin:
            try:
                settings = load_settings()

                # 🐛 [버그 수정] 예전엔 roles.get("ids_admin")을 그대로 목록에 넣고
                # `role.id in admin_role_ids`로 비교했어요. 관리자 역할 설정이
                # "단일 int → 여러 개(리스트)"로 바뀐 뒤로는 이 목록에 `[123]` 같은 리스트가
                # 섞여서, int인 role.id와는 **절대 매치되지 않습니다.**
                # 지금 settings.json에 단일 int가 남아 있는 동안에만 우연히 동작하던 코드라,
                # `/설정 관리자 아이디`를 한 번이라도 쓰는 순간(그 키가 리스트로 바뀜)
                # 서버 관리자 말고는 아무도 관리자 도움말을 못 여는 상태가 돼요.
                # 하위 호환까지 처리해주는 공용 함수(_get_role_ids)로 통일했습니다.
                admin_role_ids = set()
                for key in ("ids_admin", "shop_admin", "stock_admin"):
                    admin_role_ids.update(_get_role_ids(settings, key))

                # 유저가 가진 역할 중 하나라도 관리자 계열에 속하면 통과
                if any(role.id in admin_role_ids for role in interaction.user.roles):
                    is_admin = True
            except Exception as e:
                print(f"⚠️ 도움말 권한 체크 중 오류 발생: {e}")
                
        if 관리자 and not is_admin:
            return await interaction.response.send_message("❌ 관리자 전용 도움말 메뉴는 관리자만 열람할 수 있어요!", ephemeral=True)

        categories = self._admin_categories() if 관리자 else self._user_categories()
        embed_color = 0x2c3e50 if 관리자 else 0xffb6c1

        # 🚨 카테고리가 하나도 안 남는 구성이 실제로 있어요. 관리 기능만 담고 유저용
        #    기능(지갑·아이디·주식·위키·생일·게임·스누즈·AI)을 하나도 안 담으면
        #    일반 도움말이 통째로 빕니다. 빈 선택지로 Select를 만들면 디스코드가
        #    거부해서 `/도움말` 자체가 실패해요. 드롭다운 없이 안내만 보냅니다.
        if not categories:
            embed = discord.Embed(
                title=f"📚 {bot_name()}봇 명령어 메뉴얼",
                description=("이 서버에 담긴 기능 중 일반 유저용 명령어가 아직 없어요.\n"
                             "서버 관리자라면 `/도움말 관리자:True` 로 관리 명령을 볼 수 있어요."),
                color=embed_color,
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        # 📋 첫 화면은 카테고리 목록만 짧게 보여주고, 드롭다운으로 하나씩 골라보게 안내
        category_list = "\n".join(f"{emoji}  **{name}**" for name, (emoji, _content) in categories.items())
        embed = discord.Embed(
            title=f"📚 {bot_name()}봇 명령어 메뉴얼" + (" (관리자용)" if 관리자 else ""),
            description=f"아래 메뉴에서 카테고리를 선택하면 해당 명령어들을 볼 수 있어요.\n\n{category_list}",
            color=embed_color,
        )
        embed.set_footer(text="🔧 관리자 전용 명령어 모드예요." if 관리자 else "🌸 일반 유저용 기본 명령어 모드예요.")

        view = HelpView(categories, embed_color)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        
