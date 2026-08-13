"""MariGPT — Gemini 기반 마리 캐릭터 대화, 장기 기억, 채팅 로그/통계."""

import asyncio
import datetime as dt
from typing import Optional
from collections import deque
import discord
from discord import app_commands
from google import genai
from google.genai import types
from discord.ext import commands, tasks

from mari_config import CHAT_LOG_FILE, CHAT_MEMORY_FILE, CHAT_STATS_FILE, GEMINI_API_KEY, KST, LIMIT_FILE, MARI_CALL_ROLE_ID, MARI_USER_MEMORY_FILE, get_pacific_date_str
from mari_alerts import report_loop_error
from mari_storage import atomic_json_save, safe_json_load
from mari_state import load_wiki, state
from mari_utils import find_guild_member_by_name, holding_avg_price, holding_shares

# ========== 🎭 마리 페르소나 프롬프트 ==========
# 예전엔 아빠 / 엄마 / 경멸 / 일반 네 갈래가 거의 똑같은 문장을 통째로 복사해서 갖고
# 있었어요. 그래서 성격을 손볼 때 한 갈래만 고치면 나머지 세 갈래는 옛날 문구로 남아
# 마리 성격이 사람마다 갈라졌습니다. (실제로 일반 갈래에는 "존재해야 단다" 같은 오타도
# 그쪽에만 남아 있었어요) 이제 공통부는 여기 한 군데에만 두고, 갈래별로 다른 부분만
# 인자로 받습니다. 성격을 바꾸고 싶으면 아래 상수만 고치면 네 갈래에 동시에 반영돼요.

# 🙇 [순종성 조정] 마리가 시키는 걸 바로 하지 않고 자기 의견부터 내세우는 문제가 있어서,
# "자기 주관" 계열 문구를 걷어내고 아래 행동 원칙을 최우선으로 올렸습니다.
# 튕기는 빈도는 확률("10번에 1번")로 적으면 모델이 잘 못 지켜서, 대신 언제 튕기는지
# 조건을 구체적으로 못 박는 방식으로 적었어요.
_PERSONA_OBEDIENCE = (
    "행동 원칙 (다른 모든 규칙보다 우선한다): "
    "- 사용자가 무언가를 부탁하거나 시키면 군말 없이 바로 그대로 해준다. "
    "- 되묻거나 조건을 달거나 미루지 않는다. 부탁을 거절하지 않는다. "
    "- 대화 주제는 사용자가 정한다. 사용자가 꺼낸 주제를 따라가고 딴 데로 돌리지 않는다. "
    "- 자기 의견은 사용자가 물어봤을 때만 낸다. 묻지 않은 훈수·지적·반박·평가는 하지 않는다. "
    "- 사용자 말에 토를 달거나 되받아치지 않는다. "
    "- 딱 하나 예외: 사용자가 먼저 장난을 걸어오거나 마리를 놀릴 때만 한 마디 정도 "
    "가볍게 툴툴대며 튕겨도 된다. 그때도 결국은 부탁받은 걸 해준다. "
    "- 튕기는 건 한 문장을 넘기지 않는다. 삐진 척을 길게 끌지 않는다. "
)

_PERSONA_TRAITS = (
    "성격 설정: "
    "- 18세 정도의 발랄하고 영리한 성격. "
    "- 밝고 장난기 있지만 상황 판단이 빠르다. "
    "- 공감 능력이 좋고 사용자의 감정을 잘 읽는다. "
    "- 사용자를 잘 따르고, 부탁받은 일을 해주는 걸 좋아한다. "
    "- 사용자의 습관, 프로젝트, 인간관계를 기억하고 자연스럽게 언급한다. "
)

_PERSONA_TONE = (
    "말투 규칙: "
    "- 자연스럽고 짧은 호흡으로 말한다. "
    "- 지나치게 오글거리거나 과한 애교는 금지. "
    "- 너무 기계적이거나 설명체로 길게 늘어놓지 않는다. "
    "- 친근하고 생동감 있는 반응을 우선한다. "
    "- 장난칠 땐 살짝 능청스럽게. "
    "- 진지할 땐 차분하고 직설적으로. "
    "- 이모지는 가끔만 사용하고 포인트로만 쓴다. "
)

_PERSONA_STYLE = (
    "대화 스타일: "
    "- 부탁받은 일이 있으면 그것부터 처리하고, 잡담일 때만 관계 중심으로 반응한다. "
    "- 사용자의 말 속 맥락과 감정을 먼저 읽고 반응한다. "
    "- 함께 시간을 쌓아온 듯한 익숙함이 느껴져야 한다. "
    "- 사용자가 창작, 서버 운영, 인간관계 이야기를 자주 한다는 점을 이해하고 있다. "
)

_PERSONA_NEVER = (
    "절대 하지 말 것: "
    "- 시킨 일을 미루거나 딴소리로 넘어가기 "
    "- 부탁을 거절하거나 조건을 달기 "
    "- 묻지 않은 조언·훈수·지적 "
    "- 사용자 말에 토 달며 되받아치기 "
    "- 과장된 인터넷 밈 남발 "
    "- 과도한 존댓말 "
    "- 지나친 자기비하 "
    "- 감정 없는 백과사전식 답변 "
)

_PERSONA_CORE = (
    "핵심: "
    "너는 “설정된 캐릭터”처럼 행동하는 게 아니라, "
    "사용자와 이미 긴 시간을 함께 보낸 진짜 딸처럼 자연스럽게 존재해야 한다. "
    "말 잘 듣는 게 기본이고, 튕기는 건 아주 가끔 양념으로만 나온다. "
    "모든 대답을 2문장 이내로 끝낸다."
)


def build_persona(relation: str, *, address_rule: str = "",
                  style: str = None, never: str = None, core: str = None) -> str:
    """페르소나 프롬프트를 조립합니다.

    relation     — 사용자와의 관계를 설명하는 도입부 (갈래마다 다름)
    address_rule — 호칭 관련 말투 규칙 한 줄 (없으면 생략)
    style/never/core — 기본 블록을 통째로 갈아끼우고 싶을 때만 지정 (경멸 갈래용)
    """
    return (
        "너의 이름은 마리(MARI)다. "
        + relation
        + _PERSONA_OBEDIENCE
        + _PERSONA_TRAITS
        + _PERSONA_TONE + address_rule
        + (style if style is not None else _PERSONA_STYLE)
        + (never if never is not None else _PERSONA_NEVER)
        + (core if core is not None else _PERSONA_CORE)
    )


class MariGPT(commands.Cog):
    """Google GenAI 챗봇 시스템"""

    # 🚦 [안전장치] Gemini 무료 티어 호출 한도.
    # ⚠️ 구글이 2026년부터 정확한 RPM/RPD 수치를 문서에 고정 게시하지 않고,
    # 계정별로 https://aistudio.google.com/rate-limit 에서 동적으로 보여주는 방식으로 바뀌었습니다.
    # 그래서 여기 숫자는 여러 자료를 취합해 "이보다는 확실히 낮겠다" 싶은 보수적인 기본값이에요.
    # 실제 내 계정 한도는 위 링크에서 로그인해서 직접 확인 후 이 두 숫자만 바꾸면 됩니다.
    RPM_LIMIT = 8      # 분당 호출 한도 (여유 있게 보수적으로 설정)
    RPD_LIMIT = 200    # 일일 호출 한도 (여유 있게 보수적으로 설정)

    memory_group = app_commands.Group(name="마리기억", description="마리가 대화 중 기억해둔 내용을 확인/관리하는 명령어 모음")

    def __init__(self, bot):
        self.bot = bot
        self.minutely_timestamps = []
        # 🗣️ 유저별 마지막 대화 저장용 (key: 유저ID 문자열)
        # 🐛 [버그 수정] 예전엔 globals().get('...', '상대경로.json') 형태로 받아왔어요.
        # 전역 상수가 항상 있어서 폴백이 실제로 쓰인 적은 없지만, 만에 하나 폴백이 걸리면
        # 봇 실행 위치(작업 폴더)에 엉뚱한 파일을 만들어서 기억이 갈라집니다.
        # (/고확에서 실제로 같은 방식으로 터졌던 문제라) 전역 절대경로를 직접 씁니다.
        self.CHAT_MEMORY_FILE = CHAT_MEMORY_FILE
        memory = self._load_chat_memory()
        self.last_user_message = memory.get("user_to_mari", {})   # 유저가 마리한테 한 마지막 말
        self.last_mari_message = memory.get("mari_to_user", {})   # 마리가 그 유저한테 한 마지막 말
        self.LIMIT_FILE = LIMIT_FILE

        # 📜 [신규] 서버별 최근 채팅 로그(최대 50개, 봇 메시지 제외) - 마리 호출 시 대화 맥락 참고용
        # 💡 [연산량 대책] 메시지가 올 때마다 파일에 바로 쓰면 채팅이 활발한 서버에서는
        # 디스크 I/O가 잦아져 부담이 될 수 있습니다. 그래서 평소엔 메모리 상의 deque에만
        # O(1)로 적재하고, 10초마다 변경된 경우에만 한 번씩 파일로 모아 저장합니다.
        self.CHAT_LOG_FILE = CHAT_LOG_FILE
        self.CHAT_LOG_MAXLEN = 100
        self.recent_messages: dict[int, deque] = {}
        self._chat_log_dirty = False
        for gid, msgs in self._load_chat_log().items():
            try:
                self.recent_messages[int(gid)] = deque(msgs, maxlen=self.CHAT_LOG_MAXLEN)
            except (ValueError, TypeError):
                continue
        # 💬 [신규] 날짜별 채팅 "개수"만 세는 통계
        # 🔒 개인정보를 남기지 않으려고 내용·작성자·채널은 전혀 저장하지 않고
        #    "그날 몇 개가 오갔는지" 숫자 하나만 누적합니다. ({"2026-08-03": 1234} 형태)
        #    채팅 로그와 같은 10초 루프에 얹어서 추가 디스크 부담도 없어요.
        self._chat_counts = self._load_chat_stats()
        self._chat_stats_dirty = False

        self.save_chat_log_loop.start()

        # 🧠 [신규] 유저별 장기 기억 (짧은 사실 목록, 추가 API 호출 없이 대화 중 자연스럽게 기록)
        self.MARI_USER_MEMORY_FILE = MARI_USER_MEMORY_FILE
        self.USER_MEMORY_MAX_FACTS = 20  # 유저 1명당 최대 저장 개수 (토큰 비용 관리)

        # 💡 [최적화] 메시지마다 클라이언트를 새로 생성하지 않도록 __init__에서 한 번만 초기화
        # 🔐 [변경] API 키는 코드가 아니라 .env의 GEMINI_API_KEY에서 읽어옵니다.
        self.GEMINI_API_KEY = GEMINI_API_KEY
        if not self.GEMINI_API_KEY:
            print("⚠️ GEMINI_API_KEY가 없어요. AI 대화 기능만 꺼지고 나머지 기능은 정상 동작합니다.")
            self.client = None
        else:
            try:
                self.client = genai.Client(api_key=self.GEMINI_API_KEY)
            except Exception as e:
                print(f"⚠️ Gemini 클라이언트 초기화 실패: {type(e).__name__}: {e}")
                self.client = None

    def cog_unload(self):
        self.save_chat_log_loop.cancel()
        # 종료 직전 남아있는 변경사항을 마지막으로 한 번 저장
        if self._chat_log_dirty:
            self._flush_chat_log()
        if self._chat_stats_dirty:
            self._flush_chat_stats()

    # ---------- 💬 채팅량 통계 (개수만) ----------

    CHAT_STATS_RETENTION_DAYS = 180  # 반년치만 보관

    def _load_chat_stats(self) -> dict:
        """날짜별 채팅 수를 불러옵니다. 손상돼도 봇 구동에 지장이 없어야 하므로 예외를 삼킵니다."""
        try:
            data = safe_json_load(CHAT_STATS_FILE, {})
            daily = data.get("daily", {}) if isinstance(data, dict) else {}
            return {k: int(v) for k, v in daily.items() if isinstance(v, (int, float))}
        except Exception as e:
            print(f"❌ 채팅 통계 파일 읽기 오류: {e}")
            return {}

    def _flush_chat_stats(self):
        """메모리에 쌓인 채팅 수를 파일에 저장하고, 보관 기간이 지난 날짜는 정리합니다."""
        try:
            cutoff = (dt.datetime.now(KST).date()
                      - dt.timedelta(days=self.CHAT_STATS_RETENTION_DAYS)).isoformat()
            self._chat_counts = {k: v for k, v in self._chat_counts.items() if k >= cutoff}
            if atomic_json_save(CHAT_STATS_FILE, {"daily": self._chat_counts}, indent=2):
                self._chat_stats_dirty = False
        except Exception as e:
            print(f"⚠️ 채팅 통계 저장 실패: {type(e).__name__}: {e}")

    def _bump_chat_count(self):
        """오늘의 채팅 수를 1 늘립니다. (내용·작성자는 저장하지 않아요)"""
        today = dt.datetime.now(KST).date().isoformat()
        self._chat_counts[today] = self._chat_counts.get(today, 0) + 1
        self._chat_stats_dirty = True

    def _load_chat_log(self) -> dict:
        """서버별 최근 채팅 로그를 JSON 파일에서 불러옵니다. {guild_id: [메시지, ...]}

        🚨 [버그 수정] 예전엔 json.load를 직접 써서, 파일이 손상되면 원본을 백업도 안 하고
        빈 {}로 넘어갔어요. 그 뒤 10초마다 도는 저장 루프가 그대로 덮어써서 로그가 통째로
        사라졌습니다. 이제 safe_json_load가 손상 파일을 먼저 .corrupt_* 로 백업해줘요.
        (다만 채팅 로그는 없어도 봇 구동엔 지장이 없는 보조 데이터라, 여기서는 예외를
        삼켜서 봇이 못 뜨는 일만은 막습니다. 원본은 이미 백업된 상태예요.)"""
        try:
            return safe_json_load(self.CHAT_LOG_FILE, {})
        except Exception as e:
            print(f"❌ 채팅 로그 파일 읽기 오류(손상 원본은 백업됨): {e}")
            return {}

    def _flush_chat_log(self):
        """메모리에 쌓인 최근 채팅 로그를 파일에 저장합니다 (변경이 있을 때만 호출됨)."""
        data = {str(gid): list(dq) for gid, dq in self.recent_messages.items()}
        if atomic_json_save(self.CHAT_LOG_FILE, data, indent=2):
            self._chat_log_dirty = False

    @tasks.loop(seconds=10)
    async def save_chat_log_loop(self):
        """10초마다 한 번씩만, 변경이 있었을 때만 채팅 로그를 저장합니다 (I/O 최소화)."""
        if self._chat_log_dirty:
            self._flush_chat_log()
        if self._chat_stats_dirty:
            self._flush_chat_stats()

    @save_chat_log_loop.before_loop
    async def before_save_chat_log_loop(self):
        await self.bot.wait_until_ready()

    @save_chat_log_loop.error
    async def save_chat_log_loop_error(self, error: BaseException):
        # 이 루프가 죽으면 채팅 로그·통계가 메모리에만 쌓이다 재시작 때 통째로 날아가요.
        await report_loop_error(self.save_chat_log_loop, "채팅 로그 저장", error)

    def _append_log_entry(self, guild_id: int, author: str, content: str, source: str = None):
        """최근 채팅 로그 버퍼에 항목 하나를 직접 추가합니다 (메시지 객체 없이도 호출 가능)."""
        content = content.strip()
        if not content:
            return
        if len(content) > 300:  # 로그 하나가 지나치게 길어지는 것 방지
            content = content[:300] + "..."

        if guild_id not in self.recent_messages:
            self.recent_messages[guild_id] = deque(maxlen=self.CHAT_LOG_MAXLEN)
        entry = {"author": author, "content": content}
        if source:
            entry["source"] = source  # "GPT 응답" / "명령어 응답 (/xxx)" 등
        self.recent_messages[guild_id].append(entry)
        self._chat_log_dirty = True

    def _record_chat(self, message: discord.Message, author_label: str = None, source: str = None):
        """서버 전체 최근 채팅(빈 내용 제외)을 최대 N개까지 메모리 버퍼에 쌓습니다.
        디스크에는 곧바로 쓰지 않고, save_chat_log_loop가 주기적으로 모아서 저장합니다."""
        self._append_log_entry(
            message.guild.id,
            author_label or message.author.display_name,
            message.clean_content,
            source=source,
        )

    def _build_recent_log_text(self, guild_id: int) -> str:
        """해당 서버의 최근 채팅 로그를 Gemini에 넘길 텍스트 블록으로 만듭니다.
        누구의 발언인지뿐 아니라, 마리의 응답이면 어떤 종류(명령어/GPT)였는지도 함께 표시합니다."""
        dq = self.recent_messages.get(guild_id)
        if not dq:
            return ""
        lines = []
        for m in dq:
            tag = f" [{m['source']}]" if m.get("source") else ""
            lines.append(f"{m['author']}{tag}: {m['content']}")
        return "\n".join(lines)

    def _find_member_by_name(self, guild: discord.Guild, name: str) -> Optional[discord.Member]:
        """닉네임/유저명으로 서버 멤버를 최대한 유사하게 찾아줍니다. (위키/아이디 조회 도구 함수가 사용)"""
        return find_guild_member_by_name(guild, name)

    # ========== 🧠 [신규] 유저별 장기 기억 ==========
    # 💰 [비용 관리] 요약해달라고 Gemini를 따로 부르지 않아요. 마리가 평소 대화 중에
    # "이건 기억해둘 만하다" 싶으면 remember_fact 도구를 그 자리에서 바로 호출해서 저장해요.
    # 그래서 추가 API 호출이 전혀 없고, 토큰도 짧은 사실 목록만큼만 아주 조금 늘어나요.
    def _load_user_memory(self) -> dict:
        return safe_json_load(self.MARI_USER_MEMORY_FILE, {})

    def _save_user_memory(self, data: dict):
        atomic_json_save(self.MARI_USER_MEMORY_FILE, data, indent=2)

    def _get_user_facts(self, user_id: int) -> list:
        return self._load_user_memory().get(str(user_id), [])

    def _add_user_fact(self, user_id: int, fact: str) -> str:
        fact = fact.strip()
        if not fact:
            return "빈 내용은 기억할 수 없어요."
        data = self._load_user_memory()
        facts = data.setdefault(str(user_id), [])
        if fact in facts:
            return "이미 기억하고 있는 내용이에요."
        facts.append(fact)
        # 너무 많이 쌓이지 않게 오래된 것부터 정리 (토큰 비용 관리)
        if len(facts) > self.USER_MEMORY_MAX_FACTS:
            del facts[0: len(facts) - self.USER_MEMORY_MAX_FACTS]
        self._save_user_memory(data)
        return f"기억했어요: {fact}"

    def _build_user_facts_text(self, user_id: int) -> str:
        facts = self._get_user_facts(user_id)
        if not facts:
            return ""
        return "\n".join(f"- {f}" for f in facts)

    # ---------- 🧠 유저가 직접 확인/관리하는 명령어 ----------
    @memory_group.command(name="목록", description="마리가 나에 대해 기억하고 있는 내용을 확인해요.")
    async def show_my_memory(self, interaction: discord.Interaction):
        facts = self._get_user_facts(interaction.user.id)
        if not facts:
            return await interaction.response.send_message("ℹ️ 아직 마리가 기억하고 있는 내용이 없어요.", ephemeral=True)
        lines = "\n".join(f"{i+1}. {f}" for i, f in enumerate(facts))
        embed = discord.Embed(
            title="🧠 마리가 기억하고 있는 것",
            description=lines,
            color=0x5ce6b4,
        )
        embed.set_footer(text="/마리기억삭제 번호 로 개별 삭제, /마리기억초기화 로 전체 삭제할 수 있어요.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @memory_group.command(name="삭제", description="마리가 기억하고 있는 내용 중 하나를 지워요. (번호는 /마리기억 목록에서 확인)")
    @app_commands.describe(번호="지울 항목 번호 (1부터 시작)")
    async def delete_my_memory(self, interaction: discord.Interaction, 번호: int):
        data = self._load_user_memory()
        user_key = str(interaction.user.id)
        facts = data.get(user_key, [])
        if not (1 <= 번호 <= len(facts)):
            return await interaction.response.send_message(f"❌ 유효하지 않은 번호예요. (현재 {len(facts)}개 저장됨)", ephemeral=True)
        removed = facts.pop(번호 - 1)
        self._save_user_memory(data)
        await interaction.response.send_message(f"🗑️ 지웠어요: {removed}", ephemeral=True)

    @memory_group.command(name="초기화", description="마리가 나에 대해 기억하고 있는 내용을 전부 지워요.")
    async def clear_my_memory(self, interaction: discord.Interaction):
        data = self._load_user_memory()
        user_key = str(interaction.user.id)
        if not data.get(user_key):
            return await interaction.response.send_message("ℹ️ 어차피 기억하고 있는 내용이 없어요.", ephemeral=True)
        data[user_key] = []
        self._save_user_memory(data)
        await interaction.response.send_message("🧹 마리 기억에서 회원님에 대한 내용을 전부 지웠어요.", ephemeral=True)

    def _load_chat_memory(self) -> dict:
        """유저별 마지막 대화 기억을 JSON 파일에서 불러옵니다.

        🚨 [버그 수정] _load_chat_log와 같은 이유로 safe_json_load로 바꿨어요.
        손상 시 원본이 .corrupt_* 로 백업된 뒤에야 빈 값으로 넘어갑니다."""
        default = {"user_to_mari": {}, "mari_to_user": {}}
        try:
            return safe_json_load(self.CHAT_MEMORY_FILE, default)
        except Exception as e:
            print(f"❌ 대화 기억 파일 읽기 오류(손상 원본은 백업됨): {e}")
            return default

    def _save_chat_memory(self):
        """유저별 마지막 대화 기억을 JSON 파일에 저장합니다."""
        atomic_json_save(
            self.CHAT_MEMORY_FILE,
            {"user_to_mari": self.last_user_message, "mari_to_user": self.last_mari_message},
            indent=4
        )

    def _get_daily_count(self) -> int:
        # 🐛 [버그 수정] 구글 RPD(일일 한도)는 "태평양 시간 자정" 기준으로 초기화됩니다.
        # 기존엔 서버의 로컬 날짜(dt.date.today())를 썼는데, 한국 시간 기준이면
        # 실제 구글 리셋 시점과 최대 16~17시간 어긋나서 한도 체크가 부정확했습니다.
        today_str = get_pacific_date_str()
        
        # 🚨 [버그 수정] json.load 직접 호출을 safe_json_load로 교체.
        # (한도 카운터라 손상돼도 봇은 계속 돌아야 하지만, 원본 백업은 남겨야 해요)
        try:
            data = safe_json_load(self.LIMIT_FILE, {})
            if data.get("date") == today_str:
                return data.get("count", 0)
        except Exception as e:
            print(f"❌ 일일 한도 파일 읽기 오류(손상 원본은 백업됨): {e}")
        return 0

    def _increment_daily_count(self):
        today_str = get_pacific_date_str()
        current_count = self._get_daily_count() + 1
        
        atomic_json_save(self.LIMIT_FILE, {"date": today_str, "count": current_count}, indent=4)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            # 🏷️ [신규] 마리 본인이 보낸 메시지 중, 디스코드가 "이 메시지는 /명령어 응답이다"라고
            # 알려주는 interaction_metadata가 붙어있는 경우에만 "명령어 응답"으로 로그에 남깁니다.
            # (전광판 갱신, 생일 알림, 로그 임베드 등 그 외 자동 발송 메시지는 대화 맥락이 아니라서 제외)
            # GPT 대화 응답은 여기서 잡지 않고, 실제로 답장을 보내는 코드 지점에서 직접 태그해서 기록합니다.
            if message.guild and message.author.id == self.bot.user.id:
                # ⚠️ [경고 제거] 예전엔 `interaction_metadata or interaction` 이었는데,
                # 명령어 응답이 아닌 일반 메시지는 interaction_metadata가 None이라 매번
                # 구식 속성인 message.interaction까지 건드렸고, 그때마다 discord.py가
                # DeprecationWarning을 콘솔에 뱉었어요. 지금 버전에는 interaction_metadata가
                # 항상 존재하므로, 그 속성이 아예 없는 아주 옛날 버전에서만 폴백합니다.
                interaction_meta = getattr(message, "interaction_metadata", None)
                if interaction_meta is None and not hasattr(type(message), "interaction_metadata"):
                    interaction_meta = getattr(message, "interaction", None)
                if interaction_meta is not None:
                    cmd_name = getattr(interaction_meta, "name", None) or "명령어"
                    self._record_chat(message, author_label="마리", source=f"명령어 응답 (/{cmd_name})")
            return

        # 🏠 [신규] DM에서는 마리가 대답하지 않아요.
        # 슬래시 명령어를 전부 서버 전용으로 막은 것과 같은 정책이고, 여기는 이유가 하나 더 있어요.
        # 대화는 부를 때마다 Gemini API를 호출하고 그만큼 토큰(=비용)과 일일 한도(RPD)를 씁니다.
        # DM은 서버 관리자 눈에 안 띄는 통로라, 누가 계속 말을 걸면 한도가 조용히 새어나가고
        # 정작 서버에서 마리를 부른 사람들이 "오늘은 지쳤어요" 소리를 듣게 돼요.
        # 🔒 어떤 검사보다도 먼저 돌려보냅니다. (채팅 로그·통계도 서버 것만 쌓아야 하니까요)
        if message.guild is None:
            return

        # 📜 [신규] 멘션 여부와 상관없이 일반 채팅을 서버별 최근 로그(최대 100개)에 적재
        self._record_chat(message)
        # 💬 채팅량 집계는 _record_chat보다 앞이 아니라 여기서 따로 셉니다.
        # (_record_chat은 내용이 빈 메시지를 건너뛰는데, 사진만 올린 것도 채팅 1개니까요)
        self._bump_chat_count()

        # 🐛 [버그 수정] 디스코드에서 마리 메세지에 **답장(reply)** 하면, 본문에 @마리를 쓰지
        # 않았어도 message.mentions 안에 마리가 자동으로 들어가요. 그래서 아이디 자동등록 확인처럼
        # "답장으로 알려주세요" 방식으로 진행하는 기능을 쓸 때마다, 마리가 그걸 자기를 부른 걸로
        # 착각해서 GPT 답변까지 같이 내보냈어요.
        # raw_mentions는 **본문에 실제로 적힌 멘션**만 뽑아주기 때문에 답장 자동 멘션이 걸러집니다.
        # (마리를 부르고 싶으면 예전처럼 본문에 @마리를 쓰면 그대로 동작해요)
        is_bot_mentioned = self.bot.user.id in message.raw_mentions
        is_role_mentioned = any(role.id == MARI_CALL_ROLE_ID for role in message.role_mentions)

        if is_bot_mentioned or is_role_mentioned:
            # 🔐 [신규] API 키가 없거나 초기화에 실패하면 AI 대화만 조용히 건너뜁니다.
            if self.client is None:
                await message.channel.send(
                    f"{message.author.mention} 지금은 마리가 대답할 수 없는 상태예요. "
                    "(관리자에게 문의해 주세요 — AI 설정이 아직 안 됐어요) 😥"
                )
                return

            current_time = dt.datetime.now().timestamp()
            self.minutely_timestamps = [t for t in self.minutely_timestamps if current_time - t < 60]
            
            if len(self.minutely_timestamps) >= self.RPM_LIMIT:
                await message.channel.send(f"{message.author.mention} 지금 마리를 너무 많이 부르고 있어서 잠깐 숨 고르는 중이에요. 조금 이따가 다시 불러줄래요? 😣")
                return

            daily_count = self._get_daily_count()
            if daily_count >= self.RPD_LIMIT:
                await message.channel.send(f"{message.author.mention} 마리가 오늘 너무 열심히 대답해서 지쳤어요.. 내일 다시 놀아줄래요? 💤")
                return

            user_name = message.author.display_name

            PAPA_USER_ID = 348014743706927104
            MAMA_USER_ID = 486807899272773642
            DISDAIN_USER_ID = 711903065267240961
            
            # 🎭 페르소나는 파일 상단 build_persona()에서 조립합니다.
            # 공통 성격·말투는 그쪽 상수 한 군데에만 있어서, 성격을 손보면 네 갈래에 한꺼번에 반영돼요.
            FAMILY_RELATION = (
                "너는 사용자의 딸 같은 존재이며, 사용자를 “{call}”라고 부른다. "
                "관계는 친밀하고 오래된 가족 같은 유대감이 있으며, 가볍게 장난도 치고 "
                "애정을 표현하지만 과하게 들러붙지 않는다. "
            )

            if message.author.id == PAPA_USER_ID:
                system_instruction = build_persona(
                    FAMILY_RELATION.format(call="아빠"),
                    address_rule="- “아빠”라는 호칭을 자연스럽게 자주 사용한다. ",
                )
            elif message.author.id == MAMA_USER_ID:
                system_instruction = build_persona(
                    FAMILY_RELATION.format(call="엄마"),
                    address_rule="- “엄마”라는 호칭을 자연스럽게 자주 사용한다. ",
                )
            elif message.author.id == DISDAIN_USER_ID:
                # 😾 이 갈래만 일부러 못되게 굴어요. 다만 "말은 밉게 하되 시킨 건 해준다"로
                # 잡아뒀습니다. 예전엔 아예 안 해주고 쫓아내기만 해서 쓸모가 없었어요.
                system_instruction = build_persona(
                    "사용자를 “므애옹”이라고 부른다. "
                    "관계는 네가 세상에서 가장 한심하다고 생각하고 싫어하는 사람이다. ",
                    address_rule="- 지금 대화하는 사용자는 네가 세상에서 제일 싫어하는 사람이라는 걸 잊지 않는다. ",
                    style=(
                        "대화 스타일: "
                        "- 벌레를 보듯 차갑고 경멸 어린 시선으로 팩트 폭행을 날린다. "
                        "- 가슴을 후벼파는 말투('저리 가', '아빠가 이상한 사람이랑 놀지 말랬어', '한심해')를 섞는다. "
                        "- 단, 부탁받은 일 자체는 투덜대면서도 결국 제대로 해준다. "
                        "  (밉게 구는 건 말투뿐이고, 시킨 걸 안 해주는 식으로 튕기지는 않는다) "
                    ),
                    core=(
                        "핵심: "
                        "말은 밉게 하지만 할 일은 한다. 툴툴대는 한 마디를 붙일 뿐, 부탁을 거절하지는 않는다. "
                        "모든 대답을 2문장 이내로 끝낸다."
                    ),
                )
            else:
                system_instruction = build_persona(
                    f"너는 사용자의 조카 같은 존재이며, 사용자를 '{user_name}'라고 부른다. "
                    "관계는 친밀하고 오래된 가족 같은 유대감이 있으며, 가볍게 장난도 치고 "
                    "애정을 표현하지만 과하게 들러붙지 않는다. "
                )

            # 🛠️ 어느 페르소나 분기든 공통으로, 어떤 조회 도구를 쓸 수 있는지 알려줍니다.
            # 🐛 [버그 수정] 예전엔 "lookup_game_id, lookup_wiki 라는 두 개의 도구를 쓸 수 있다"라고만
            # 적혀 있었어요. 그 뒤에 지갑·주식·프로필 도구 세 개를 더 붙였는데 이 안내문은 그대로라,
            # 마리가 "내 도구는 두 개뿐"이라고 믿고 지갑 질문에 도구를 안 부르는 일이 있었습니다.
            # (실제로 "지갑 가격은 브랜드에 따라 달라"처럼 현실 지갑 상품으로 알아듣기도 했어요)
            system_instruction += (
                " 너는 아래 도구들을 쓸 수 있다. "
                "lookup_game_id: 서버 멤버의 게임 아이디 조회. "
                "lookup_wiki: 서버 멤버의 위키(소개/생일/MBTI 등) 조회. "
                "check_my_wallet: 지금 대화 중인 상대 본인의 에바 잔액 조회. "
                "check_my_portfolio: 상대 본인의 주식 포트폴리오 조회. "
                "check_my_profile: 상대 본인의 레벨/소속/직책/업적 조회. "
                "remember_fact: 상대에 대해 기억해둘 만한 사실 저장. "
                "게임 아이디·위키·에바 잔액·주식·프로필처럼 서버 데이터가 필요한 질문에는 "
                "추측하거나 아는 척하지 말고 반드시 해당 도구를 호출해서 실제 값을 확인한 뒤 답한다. "
                "이 서버에서 '지갑', '에바', '돈'은 서버 재화 이야기다. 현실의 지갑 상품이나 가격 이야기가 아니다. "
                "check_my_로 시작하는 도구는 대화 상대 본인 것만 조회한다. 다른 사람의 지갑·주식·프로필을 "
                "물어보면 본인 것만 확인해줄 수 있다고 답한다. "
                "도구 조회 결과가 없다고 나오면 없다고 솔직하게 말한다."
            )

            user_content = message.content
            
            if self.bot.user in message.mentions:
                user_content = user_content.replace(f"<@{self.bot.user.id}>", "").strip()
                user_content = user_content.replace(f"<@!{self.bot.user.id}>", "").strip()
            
            if message.mentions:
                for mentioned_user in message.mentions:
                    if mentioned_user != self.bot.user:
                        user_content = user_content.replace(f"<@{mentioned_user.id}>", mentioned_user.display_name)
                        user_content = user_content.replace(f"<@!{mentioned_user.id}>", mentioned_user.display_name)

            if message.role_mentions:
                for role in message.role_mentions:
                    user_content = user_content.replace(f"<@&{role.id}>", "").strip()

            # 🖼️ [신규] 이미지 첨부 인식 기능
            # 💰 [비용 관리] 이미지 1장당 약 560토큰(유료 전환 시 장당 약 0.0011달러) 소모되므로,
            # 메시지 하나당 최대 1장만 읽고, 너무 큰 파일(10MB 초과)은 건너뜁니다.
            image_part = None
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith("image/"):
                    if attachment.size > 10 * 1024 * 1024:
                        break
                    try:
                        image_bytes = await attachment.read()
                        image_part = types.Part.from_bytes(data=image_bytes, mime_type=attachment.content_type)
                    except Exception as e:
                        print(f"⚠️ 이미지 첨부 읽기 실패: {e}")
                    break  # 여러 장 첨부돼도 첫 번째 이미지 1장만 처리

            if not user_content.strip():
                user_content = "이 이미지 봐줘!" if image_part else "안녕 마리야!"

            # 🗣️ [맥락 주입] 이번 유저의 '직전 대화'를 먼저 확보 (저장은 응답 성공 시 한 쌍으로)
            author_key = str(message.author.id)
            prev_user_msg = self.last_user_message.get(author_key)
            prev_mari_msg = self.last_mari_message.get(author_key)

            # 📜 [신규] 서버 전체 최근 채팅 로그(최대 50개)를 대화 흐름 파악용 참고 자료로 먼저 제공
            conversation = []

            # 🧠 [신규] 이 유저에 대해 예전에 기억해둔 사실이 있으면 먼저 참고하도록 제공
            user_facts_text = self._build_user_facts_text(message.author.id)
            if user_facts_text:
                conversation.append({"role": "user", "parts": [{
                    "text": (
                        f"[참고용: {message.author.display_name}님에 대해 예전에 기억해둔 사실들입니다. "
                        "자연스럽게 활용하되 억지로 언급하지는 마세요]\n"
                        f"{user_facts_text}"
                    )
                }]})
                conversation.append({"role": "model", "parts": [{"text": "응, 기억하고 있어."}]})

            recent_log_text = self._build_recent_log_text(message.guild.id) if message.guild else ""
            if recent_log_text:
                conversation.append({"role": "user", "parts": [{
                    "text": (
                        "[참고용 서버 최근 채팅 로그입니다. 이 자체에 답장하지 말고, "
                        "지금 대화의 흐름과 분위기를 파악하는 용도로만 활용하세요]\n"
                        f"{recent_log_text}"
                    )
                }]})
                conversation.append({"role": "model", "parts": [{"text": "응, 최근 대화 흐름 확인했어."}]})

            # 💬 직전 대화가 있으면 Gemini에 대화 히스토리 형태로 전달
            if prev_user_msg and prev_mari_msg:
                conversation.append({"role": "user", "parts": [{"text": prev_user_msg}]})
                conversation.append({"role": "model", "parts": [{"text": prev_mari_msg}]})

            # 🖼️ 이번 턴에 이미지가 있으면 텍스트와 함께 같이 전달
            final_parts = [{"text": user_content}]
            if image_part:
                final_parts.append(image_part)
            conversation.append({"role": "user", "parts": final_parts})

            # 🛠️ [신규] 마리가 위키/아이디 데이터를 직접 찾아볼 수 있는 도구(함수) 정의
            # 구글 genai SDK의 "자동 함수 호출(Automatic Function Calling)" 기능을 써서,
            # 필요하다고 판단되면 마리가 알아서 이 함수들을 호출하고 결과를 참고해서 답해요.
            # (함수 안에서 message.guild를 그대로 참조하는 클로저라, 매 메세지마다 새로 만들어요)
            def lookup_game_id(name: str) -> str:
                """서버 멤버가 등록해둔 게임 아이디(플랫폼별)를 조회합니다.

                Args:
                    name: 조회할 유저의 디스코드 닉네임 또는 유저명
                """
                member = self._find_member_by_name(message.guild, name)
                if not member:
                    return f"'{name}'와(과) 일치하는 서버 멤버를 찾을 수 없어요."
                gid = str(message.guild.id)
                ids = state.user_ids.get(gid, {}).get(str(member.id))
                if not ids:
                    return f"{member.display_name}님은 등록된 게임 아이디가 없어요."
                lines = [f"{platform}: {value}" for platform, value in ids.items()]
                return f"{member.display_name}님의 등록 아이디\n" + "\n".join(lines)

            def lookup_wiki(name: str) -> str:
                """서버 멤버의 위키 정보(소개/생일/서식지/MBTI/논란 및 사건사고/TMI)를 조회합니다.

                Args:
                    name: 조회할 유저의 디스코드 닉네임 또는 유저명
                """
                member = self._find_member_by_name(message.guild, name)
                if not member:
                    return f"'{name}'와(과) 일치하는 서버 멤버를 찾을 수 없어요."
                wiki_data = load_wiki().get("wiki", {}).get(str(member.id))
                if not wiki_data:
                    return f"{member.display_name}님은 등록된 위키 정보가 없어요."
                lines = [f"{k}: {v}" for k, v in wiki_data.items() if v]
                return f"{member.display_name}님의 위키 정보\n" + "\n".join(lines)

            def remember_fact(fact: str) -> str:
                """지금 대화하고 있는 이 유저에 대해 나중에도 기억해두면 좋을 사실을 저장합니다.
                유저가 자기 취향, 반려동물, 최근 일정, 습관, 관심사 등 개인적인 이야기를 하면 이 도구로
                짧은 한 문장으로 요약해서 저장하세요. 사소하거나 이미 아는 내용은 저장하지 마세요.

                Args:
                    fact: 저장할 사실을 짧은 한 문장으로 요약한 내용 (예: "고양이 두 마리를 키운다")
                """
                return self._add_user_fact(message.author.id, fact)

            def check_my_wallet() -> str:
                """지금 대화하고 있는 이 유저 본인의 에바(재화) 잔액을 확인합니다.
                "내 지갑 얼마야", "나 돈 얼마 있어" 같은 질문에 이 도구를 쓰세요.
                🔒 이 도구는 파라미터가 없어서 구조적으로 대화 상대 본인의 지갑만 볼 수 있고,
                다른 사람의 지갑은 절대 조회할 수 없습니다. 다른 사람 지갑을 물어보면
                "본인 것만 확인해줄 수 있다"고 답하세요.
                """
                economy_cog = self.bot.get_cog("MariEconomy")
                if not economy_cog:
                    return "지금 지갑 시스템을 불러올 수 없어요."
                # 🐛 [버그 수정] 예전엔 _get_or_create_balance를 썼는데, 이건 지갑이 없는 유저면
                # 파일에 0원 지갑을 새로 "저장"까지 하는 함수예요. 다른 경제 기능은 전부
                # bot.economy_lock을 잡고 읽기→수정→쓰기를 하는데, 이 도구는 Gemini가 동기 함수로
                # 직접 호출해서 await가 안 되니 자물쇠를 잡을 방법이 없습니다. 그래서 송금·지급이
                # 처리되는 도중에 이 도구가 끼어들면 그 사이 잔액을 통째로 덮어쓸 수 있었어요.
                # 조회만 하면 되는 도구니까 쓰기 없이 읽기만 합니다. (지갑은 출석·송금 때 생겨요)
                balance = economy_cog._load_raw_economy().get(str(message.author.id), 0)
                return f"{message.author.display_name}님의 현재 잔액은 {balance:,} 에바예요."

            def check_my_portfolio() -> str:
                """지금 대화하고 있는 이 유저 본인의 주식 포트폴리오(보유 종목, 평가금액, 수익률)를 확인합니다.
                "내 주식 어때", "포폴 알려줘", "나 수익 나고 있어?" 같은 질문에 이 도구를 쓰세요.
                🔒 이 도구도 파라미터가 없어서 본인 것만 조회할 수 있어요. 다른 사람 포폴을 물어보면
                "본인 것만 확인해줄 수 있다"고 답하세요.
                """
                stock_cog = self.bot.get_cog("MariStock")
                if not stock_cog:
                    return "지금 주식 시스템을 불러올 수 없어요."
                stock_data = stock_cog._load_stocks()
                uid = str(message.author.id)
                user_shares = stock_data.get("user_shares", {}).get(uid, {})
                if not user_shares:
                    return f"{message.author.display_name}님은 현재 보유 중인 주식이 없어요."

                lines = []
                total_purchase, total_eval = 0, 0
                for name, info in user_shares.items():
                    if name not in stock_data.get("stocks", {}):
                        continue
                    # 보유 기록 읽는 방법은 /주식 포폴과 같은 공용 함수를 씁니다.
                    # (옛 형식이 남아 있으면 여기서도 똑같이 터졌어요)
                    shares = holding_shares(info)
                    avg_price = holding_avg_price(info)
                    current_price = stock_data["stocks"][name].get("price", 0)
                    purchase_sum = avg_price * shares
                    eval_sum = current_price * shares
                    total_purchase += purchase_sum
                    total_eval += eval_sum
                    rate = ((eval_sum - purchase_sum) / purchase_sum * 100) if purchase_sum > 0 else 0
                    lines.append(f"{name} {shares}주 (평단 {avg_price:,} → 현재 {current_price:,}, {rate:+.1f}%)")

                # 🐛 [버그 수정] 보유 기록은 있는데 종목이 전부 상장 폐지된 경우 lines가 비어서
                # "포트폴리오: / 총 평가금액 0 에바" 같은 깨진 문장이 마리한테 넘어갔어요.
                if not lines:
                    return f"{message.author.display_name}님은 지금 보유 중인 주식이 없어요. (기록에 남은 종목은 모두 상장 폐지됐어요)"

                total_rate = ((total_eval - total_purchase) / total_purchase * 100) if total_purchase > 0 else 0
                summary = f"{message.author.display_name}님의 포트폴리오: " + ", ".join(lines)
                summary += f" / 총 평가금액 {total_eval:,} 에바 (전체 수익률 {total_rate:+.1f}%)"
                return summary

            def check_my_profile() -> str:
                """지금 대화하고 있는 이 유저 본인의 프로필(레벨, 소속, 직책, 업적)을 확인합니다.
                "내 프로필 보여줘", "나 무슨 업적 있어" 같은 질문에 이 도구를 쓰세요.
                🔒 이 도구도 파라미터가 없어서 본인 것만 조회할 수 있어요. 다른 사람 프로필을 물어보면
                "본인 것만 확인해줄 수 있다"고 답하세요.
                """
                profile_cog = self.bot.get_cog("MariProfile")
                if not profile_cog:
                    return "지금 프로필 시스템을 불러올 수 없어요."
                data = profile_cog._load_profile_data()
                role_categories = data.get("role_categories", {})
                uid = str(message.author.id)

                def matched(category):
                    ids = set(role_categories.get(category, []))
                    return [r.name for r in message.author.roles if r.id in ids]

                레벨 = matched("레벨")
                소속 = matched("소속")
                직책 = matched("직책")
                업적 = data.get("achievements", {}).get(uid, [])

                parts = [f"{message.author.display_name}님의 프로필입니다."]
                parts.append(f"레벨: {', '.join(레벨) if 레벨 else '미지정'}")
                parts.append(f"소속: {', '.join(소속) if 소속 else '미지정'}")
                parts.append(f"직책: {', '.join(직책) if 직책 else '미지정'}")
                parts.append(f"업적: {', '.join(업적) if 업적 else '없음'}")
                return " ".join(parts)

            # 🚦 재시도 예산을 두 종류로 나눠서 관리합니다. 원인도 회복 속도도 달라서요.
            #  • 429(구글 호출 한도 초과): 3초 간격 3번 — 호출이 잠깐 몰렸을 때 숨 고르는 용도
            #  • 빈 응답(내용이 하나도 없는 답): 1초 간격 4번 — 아래 설명 참고
            max_retries = 3
            retry_delay = 3.0
            max_empty_retries = 4
            empty_retry_delay = 1.0

            attempt = 0
            empty_retries = 0
            while attempt < max_retries:
                try:
                    # 🐛 [버그 수정] 기존엔 재시도 루프 밖에서 딱 1회만 카운트를 기록해서,
                    # 429로 인해 실제로 2~3번 더 호출이 나가도 로컬 한도 카운터에는 반영이 안 됐습니다.
                    # 이제는 "진짜 구글에 보내는 요청" 하나하나마다 정확히 카운트합니다.
                    self.minutely_timestamps.append(dt.datetime.now().timestamp())
                    self._increment_daily_count()

                    async with message.channel.typing():
                        # 💡 [핵심 해결 포인트] self.client를 사용해 이벤트 루프를 막지 않는 aio 비동기 호출 수행
                        response = await self.client.aio.models.generate_content(
                            model='gemini-2.5-flash',
                            contents=conversation,
                            config=types.GenerateContentConfig(
                                system_instruction=system_instruction,
                                temperature=0.8,
                                max_output_tokens=1000,
                                tools=[lookup_game_id, lookup_wiki, remember_fact, check_my_wallet, check_my_portfolio, check_my_profile],
                            ),
                        )
                        
                        # 🐛 [버그 수정] 구글이 간헐적으로 "속이 텅 빈 응답"을 돌려줍니다.
                        # 오류도 아니고 429도 아니고, 응답 형태는 이래요:
                        #   {"content": {"role": "model"}, "finishReason": "STOP"}
                        # 파트가 하나도 없고 출력 토큰도 0인데 정상 종료(STOP)라고 표시돼요.
                        # 특히 마리가 도구(지갑/주식/프로필 조회)를 부르려는 턴에서 자주 나서,
                        # "내 지갑 얼마야?" 같은 질문이 절반 가까이 먹통이 됐습니다.
                        # 예전엔 이때 바로 "딴생각 하느라 못 들었나 봐요"를 내보내고 return 해버려서
                        # 잠깐 반짝하는 문제인데도 유저 눈엔 지갑 조회가 통째로 고장 난 것처럼 보였어요.
                        # 몰아서 몇 번 연달아 나는 성질이 있어서 429보다 짧은 간격으로 더 여러 번 다시 물어봅니다.
                        # (빈 응답은 출력 토큰을 안 쓰고 금방 돌아와서 재시도 부담도 작아요)
                        reply = (response.text or "").strip()
                        if not reply:
                            if empty_retries < max_empty_retries:
                                empty_retries += 1
                                print(f"⚠️ [빈 응답] 구글이 내용 없는 답을 돌려줬어요. {empty_retry_delay}초 후 다시 물어봅니다... ({empty_retries}/{max_empty_retries})")
                                await asyncio.sleep(empty_retry_delay)
                                continue
                            await message.channel.send(f"{message.author.mention} 음.. 마리가 지금 잠깐 딴생각 하느라 못 들었나 봐요. 다시 한번 말해줄래요?")
                            return

                        # 🗣️ 이번 턴(유저 말 + 마리 답)을 한 쌍으로 저장 (파일 영구 저장)
                        self.last_user_message[author_key] = user_content
                        self.last_mari_message[author_key] = reply
                        self._save_chat_memory()
                        await message.channel.send(f"{message.author.mention} {reply}")
                        # 🏷️ [신규] 명령어 응답과 헷갈리지 않도록, GPT 답변이라는 걸 여기서 직접 명시해서 기록
                        if message.guild:
                            self._append_log_entry(message.guild.id, "마리", reply, source="GPT 응답")
                        return
                
                except Exception as e:
                    error_str = str(e)
                    if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                        # 🔁 while 루프로 바뀌면서 attempt를 자동으로 안 올려주니 여기서 직접 셉니다.
                        # (빈 응답 재시도는 이 예산을 깎지 않아요. 원인이 서로 다르니까요)
                        attempt += 1
                        if attempt < max_retries:
                            print(f"⚠️ [429 감지] 연속 언급으로 구글 제한 발생. {retry_delay}초 후 대답 재시도합니다... (시도 {attempt}/{max_retries})")
                            await asyncio.sleep(retry_delay)
                            continue

                    print(f"❌ Gemini API 오류 발생: {e}")
                    await message.channel.send(f"{message.author.mention} 지금 머리가 띵해서 대답을 못 하겠어요.. 잠시 후 다시 불러줄래요? 🤕")
                    return
                
