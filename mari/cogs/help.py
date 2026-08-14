"""MariHelp — 카테고리별 도움말."""

import discord
from discord import app_commands
from discord.ext import commands

from mari_settings import FEATURE_LIST_TEXT, _get_role_ids, load_settings
from mari_utils import MariView

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


class HelpView(MariView):
    def __init__(self, categories: dict, embed_color: int):
        super().__init__(timeout=180)
        self.add_item(HelpCategorySelect(categories, embed_color))


class MariHelp(commands.Cog):
    """마리봇 전용 명령어 도움말 시스템"""
    def __init__(self, bot):
        self.bot = bot

    # 📂 [가독성 개선] 카테고리별로 내용을 분리해두고, 드롭다운으로 하나씩만 보여줍니다.
    # (예전엔 임베드 하나에 필드 7~8개를 전부 우겨넣어서 스크롤이 길고 한눈에 안 들어왔어요)
    ADMIN_CATEGORIES = {
        "서버/시스템 관리": ("🛡️", (
            "`/설정 관리자` - 기능별 관리자 역할 지정\n"
            "`/설정 채널` - 시스템/로그 전용 채널 맵핑\n"
            "`/설정 채널지정내역` - 지금 지정돼 있는 채널·관리자 역할을 한눈에 확인\n"
            "`/설정 관리자 대장` - `/기능제어`를 쓸 수 있는 역할 지정 (서버 관리자 다음 자리)\n"
            "`/통계` - 출석률·에바 유통량·거래 활발도·주식 현황 요약\n"
            "`/감사로그` - 경제/상점/아이디/역할/주식/생일 로그 통합 조회\n"
            f"`/기능제어 정지`·`재개`·`전체정지`·`전체재개`·`상태` - {FEATURE_LIST_TEXT} 긴급 정지·재개 (서버 관리자 또는 대장 전용)"
        )),
        "게임 아이디 관리": ("🎮", (
            "`/아이디 등록`, `/아이디 수정`, `/아이디 삭제`, `/아이디 전체조회`\n"
            "`/설정 채널 아이디등록` - 이 채널에 유저가 올린 '플랫폼 아이디'를 마리가 자동으로 등록/삭제 처리\n"
            "└ 맨 앞에 `변경` 또는 `수정`을 붙이면(예: `변경 라이엇 새아이디#kr1`) 무조건 관리자 승인을 거쳐서 같은 플랫폼의 기존 값을 바꿔줘요\n"
            "`/설정 채널 아이디목록` - 아이디 명단이 자동 갱신될 채널 지정\n"
            "└ 이 채널에 아이디 관리자가 \"갱신\"이라고 치면 그 자리에서 바로 새로고침돼요\n"
            "└ 명단을 등급별로 묶어서 게시하려면 `guild.json`의 `ranks`에 등급을 적어주세요 (안 적으면 한 덩어리)\n"
            "`/아이디 가져오기` - 예전에 쓰던 아이디 명단 게시글(.txt)을 한 번에 등록\n"
            "`/아이디 공지` - 아이디 명단 맨 아래에 공지 한 줄 추가 (기본은 누적, 덮어쓰기 옵션 있음. 아이디 관리자 전용)\n"
            "`/아이디 중복정리` - 실수로 중복 등록된 같은 아이디를 한 번에 정리\n"
            "`/아이디 대기열정리` - 관리자 확인 대기 중인 자동등록 요청을 전부 비우기\n"
            "`/아이디 새로고침` - ids.json을 직접 수정했을 때 메모리 데이터 갱신"
        )),
        "경제/상점 관리": ("💰", (
            "`/상점 생성`, `/상점 삭제`, `/상점 설정`\n"
            "`/상점 항목추가`, `/상점 항목삭제`\n"
            "`/상점 항목설정` - 새이름·되팔기퍼센트 변경 가능\n"
            "`/상점 사용` - 유저 인벤토리 아이템 차감\n"
            "`/상점 정산` - 월별 상점 총 판매 정산 조회\n"
            "`/지급` & `/회수` - 에바 일괄 통제\n"
            "`/지급취소` - 최근 지급/회수를 목록에서 골라 되돌리기 (잘못 지급했을 때 복구용)\n"
            "`/지갑내역 유저조회` - 특정 유저의 에바 입출금 내역 확인 (문의 대응용)\n"
            "`/출석보상설정` - 기본 보상 금액 조정\n"
            "`/지갑청소` - 퇴장 유저 지갑 정리 (지우기 전에 대상 목록을 먼저 보여주고 확인 버튼을 눌러야 실행돼요)\n"
            "`/지갑 전체:True` - 서버 인원 전원의 지갑·주식·합계를 자산 많은 순으로 조회"
        )),
        "주식 특수 관리": ("📈", (
            "`/주식 생성`, `/주식 삭제`\n"
            "`/주식 변동` - 주가 변동 및 사유 예약\n"
            "`/주식 종가게시` - 일일 장 마감 및 변동 확정 (미변동 종목은 자동 유지)\n"
            "`/주식 지급`, `/주식 회수` - 특정 주식 강제 지급/회수\n"
            "`/주식 폐장`, `/주식 개장` - 장 강제 개폐"
        )),
        "정보 및 역할 관리": ("👥", (
            "`/역할부여` - 특정 멤버에게 역할 지급 (최대 25개까지 선택 UI로 한 번에)\n"
            "`/설정 관리자 역할부여` - `/역할부여`를 쓸 수 있는 역할 지정 (서버 관리자는 항상 사용 가능)"
        )),
        "위키 관리": ("📖", "`/위키 등록`, `/위키 수정`, `/위키 삭제`"),
        # 📜 [의도적 제외] 서버 연대기(/연대기, 우클릭 '연대기에 박제')는 도움말에 싣지 않아요.
        # 조용히 기록만 하고 싶다는 운영 방침이라, 명령어 자체는 살아 있지만 안내에는 안 나옵니다.
        # (권한도 '연대기 관리자'로 따로 걸려 있어서 모르는 사람은 눌러도 막혀요)
        # ⚠️ 나중에 공개하고 싶어지면 여기에 카테고리를 하나 추가하면 됩니다.
        "이벤트 관리": ("🎉", "`/에바시설정` - 에바시 이벤트 선착순 인원/금액/지속시간 조정 (파라미터 없이 실행 시 현재 설정 확인)"),
        "테스트/진단 도구": ("🧪", (
            "`/설정 관리자 테스트` - 아래 명령어들을 쓸 수 있는 '테스트 관리자' 역할 지정 (서버 관리자는 항상 사용 가능)\n"
            "`/테스트 채널점검` - 설정된 채널 전체에 테스트 발송, 문제 있는 채널 리포트\n"
            "`/테스트 권한확인` - 본인(또는 지정 유저)이 가진 관리자 권한 확인\n"
            "`/테스트 에바시` - 에바시 이벤트 창을 짧게 강제로 열어서 전체 흐름 테스트\n"
            "`/테스트 출석초기화` - 본인 오늘 출석 기록만 지워서 반복 테스트\n"
            "`/테스트 아이디파싱` - 실제 등록 없이 텍스트 파싱 결과만 미리보기\n"
            "`/테스트 마리상태` - Gemini RPM/RPD 사용량 및 API 연결 확인\n"
            "`/테스트 데이터점검` - 아이디 중복·상점 설정 누락·파일 손상·저장 실패 일괄 점검\n"
            "`/테스트 명령어동기화` - 슬래시 명령어 강제 재동기화\n"
            "`/테스트 백업실행` - 매일 새벽 3시 자동 백업을 지금 즉시 실행\n"
            "`/테스트 종가게시미리보기` - 실제 마감 없이 종가게시 결과 미리보기"
        )),
    }

    USER_CATEGORIES = {
        "지갑 및 상점": ("💰", (
            "`/지갑` - 잔고 및 소지품 확인\n"
            "└ 🎁 **선물하기** 버튼으로 상점에서 산 아이템을 다른 사람에게 넘겨줄 수 있어요 (에바는 안 나가요)\n"
            "└ 역할이 지급되는 아이템은 선물이 안 돼요. 되팔기로 정리한 뒤 상대가 직접 사면 됩니다\n"
            "`/지갑내역 조회` - 내 에바가 언제 얼마나 들어오고 나갔는지 확인 (송금·출석·상점·주식 전부)\n"
            "`/송금` - 다른 유저에게 에바 이체\n"
            "`/출석` - 매일 출석체크 및 보상 획득\n"
            "상점 매대는 채널마다 따로 있어요! 되팔기 환급 비율은 상품마다 달라요.\n"
            "🖱️ 멤버를 **우클릭 → 앱**하면 `에바 보내기`·`아이템 선물하기`가 바로 열려요. "
            "받을 사람을 고르는 단계가 통째로 없어져요! (모바일은 프로필을 꾹 누르면 돼요)"
        )),
        "주식 거래": ("📈", "`/주식 목록` - 상장 종목 및 시세 확인\n`/주식 매수` & `/주식 매도` - 주식 거래\n`/주식 포폴` - 내 투자 포트폴리오 및 수익률 확인\n`/주식 그래프` - 특정 종목의 가격 변동 추이 그래프"),
        "정보 조회": ("📖", (
            "`/아이디 조회` - 다른 멤버의 게임 아이디 조회\n"
            "🖱️ 멤버 **우클릭 → 앱 → 아이디 보기**가 제일 빨라요 (나만 보여요)\n"
            "`/위키 조회` - 개별 멤버 위키 확인\n`/위키 목록` - 등록된 위키 전체 조회"
        )),
        "생일": ("🎂", (
            "`/생일 등록` - 본인 생일 등록 (예: 년 2000, 월 7, 일 24)\n"
            "`/생일 변경` - 등록한 생일 수정\n"
            "`/생일 삭제` - 등록한 생일 정보 삭제\n"
            "`/생일 확인` - 특정 멤버의 생일 확인\n"
            "생일 당일이 되면 마리가 축하 채널에 자동으로 알려줘요!"
        )),
        "미니게임 및 아이템": ("🎲", (
            "`/하이로우`, `/하이`, `/로우` - 간단한 예측 게임\n"
            "매일 00:21, 12:21에 아무 채널에서나 \"에바시\"라고 치면 에바를 받을 수 있어요! (참가자 전원 결과는 이벤트 종료 후 안내 채널에 한 번에 올라와요)"
        )),
        "나중에 답장": ("⏰", (
            "지금 답장 못 하는 메시지를 미뤄두면, 정한 시각에 마리가 **DM으로** 다시 알려줘요.\n"
            "**쓰는 법**: 메시지 **우클릭 → 앱 → 나중에 답장** (모바일은 메시지를 꾹 누르고 → 앱)\n"
            "└ 1시간 뒤 · 3시간 뒤 · 아침 9시 · 점심 12시 · 저녁 6시 · 밤 10시 중에 고르거나 **직접 입력**\n"
            "└ 직접 입력 형식: `30분` · `2시간` · `3일` · `14:00` · `8-10 14:00` (최대 30일)\n"
            "└ 고른 시각이 오늘 이미 지났으면 자동으로 내일로 넘어가요\n"
            "`/스누즈 목록` - 미뤄둔 메시지 확인 (번호도 여기서 봐요)\n"
            "`/스누즈 취소` - 예약 취소 (예: `번호: 7`)\n"
            "⚠️ 마리 DM을 막아두셨으면 예약이 안 돼요. 개인 메시지를 열어주세요!"
        )),
        "AI 마리 대화": ("💬", (
            "마리봇 호출 구문이나 전용 역할을 멘션해서 말을 걸어보세요! 이미지를 같이 올리면 마리가 사진도 보고 대답해줘요.\n"
            "마리가 대화 중 기억해둘 만한 걸 알아서 기억해요. `/마리기억 목록`으로 확인, `/마리기억 삭제`·`/마리기억 초기화`로 지울 수 있어요.\n"
            "대화 중 \"내 지갑 얼마야?\", \"내 주식 어때?\" 같은 질문도 알아서 대답해줘요 (전부 본인 것만 확인 가능)"
        )),
    }

    @app_commands.command(name="도움말", description="마리가 해줄 수 있는 모든 명령어를 보여드려요!")
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
                for key in ("ids_admin", "shop_admin", "stock_admin", "role_admin"):
                    admin_role_ids.update(_get_role_ids(settings, key))

                # 유저가 가진 역할 중 하나라도 관리자 계열에 속하면 통과
                if any(role.id in admin_role_ids for role in interaction.user.roles):
                    is_admin = True
            except Exception as e:
                print(f"⚠️ 도움말 권한 체크 중 오류 발생: {e}")
                
        if 관리자 and not is_admin:
            return await interaction.response.send_message("❌ 관리자 전용 도움말 메뉴는 관리자만 열람할 수 있어요!", ephemeral=True)

        categories = self.ADMIN_CATEGORIES if 관리자 else self.USER_CATEGORIES
        embed_color = 0x2c3e50 if 관리자 else 0xffb6c1

        # 📋 첫 화면은 카테고리 목록만 짧게 보여주고, 드롭다운으로 하나씩 골라보게 안내
        category_list = "\n".join(f"{emoji}  **{name}**" for name, (emoji, _content) in categories.items())
        embed = discord.Embed(
            title="📚 마리봇 명령어 메뉴얼" + (" (관리자용)" if 관리자 else ""),
            description=f"아래 메뉴에서 카테고리를 선택하면 해당 명령어들을 볼 수 있어요.\n\n{category_list}",
            color=embed_color,
        )
        embed.set_footer(text="🔧 관리자 전용 명령어 모드예요." if 관리자 else "🌸 일반 유저용 기본 명령어 모드예요.")

        view = HelpView(categories, embed_color)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        
