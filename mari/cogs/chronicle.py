"""MariChronicle — 서버에서 일어난 일을 날짜순으로 쌓아두는 '서버 연대기'.

📜 [이 기능이 뭔가요]
서버의 큰 사건을 기록해두고 나중에 되돌아보는 기능이에요. 채팅은 흘러가 버리지만
연대기는 남아서, 나중에 들어온 사람도 "이 서버가 어떤 곳인지" 읽을 수 있게 됩니다.

기록되는 경로는 세 가지예요.
  1. `/연대기 기록`      — 손으로 직접 남기기 (과거 날짜도 지정 가능)
  2. 메시지 우클릭       — "연대기에 박제". 날짜·작성자·원본 링크가 자동으로 채워져요
  3. 온에어(무대) 채널   — 방송이 시작되면 자동으로 남습니다

🔐 [권한]
기록·조회·삭제 전부 **연대기 관리자**만 할 수 있어요. `/설정 관리자 연대기`로 지정하고,
그 지정은 서버 관리자나 '채널관리자' 역할만 할 수 있습니다. (다른 관리자 설정과 동일)
다만 `/연대기 보기`의 **결과는 채널에 공개**돼서 모두가 함께 봅니다.
"""

import datetime as dt
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from mari_config import CHRONICLE_FILE, KST
from mari_settings import member_has_admin_or_role
from mari_storage import atomic_json_save_or_raise, safe_json_load
from mari_utils import MariView
from mari_names import server_name

# 🏷️ 사건 분류. 앞에 붙는 이모지는 목록에서 한눈에 구분하려고 쓰는 거예요.
CHRONICLE_TAGS = ["이벤트", "기록", "사건", "인물", "공지"]
TAG_EMOJI = {
    "이벤트": "🎉", "기록": "🏆", "사건": "⚡", "인물": "🙋", "공지": "📢",
    "온에어": "🎙️",      # 자동 기록 전용 (사람이 직접 고를 수는 없어요)
}

PAGE_SIZE = 5           # 한 페이지에 보여줄 사건 수 (임베드 필드 25개 제한에 여유 있게)
DETAIL_LIMIT = 300      # 목록에서 보여줄 본문 길이. 넘으면 잘라요


class ChroniclePageView(MariView):
    """📜 연대기 목록을 페이지로 넘겨 보는 창.

    `/연대기 보기` 결과는 채널에 **공개**로 뜹니다. 기록은 관리자만 하지만 읽는 건 다 같이
    하자는 게 이 기능의 취지라서요.

    다만 **버튼은 명령을 실행한 사람만** 누를 수 있어요. 공개 메시지에 달린 버튼은 지나가던
    사람도 누를 수 있는데, 여럿이 동시에 페이지를 넘기면 한 화면을 서로 잡아당기는 꼴이 돼서
    정작 읽던 사람이 못 읽습니다. 다른 사람이 넘겨 보고 싶으면 각자 `/연대기 보기`를 부르면 돼요.
    """

    def __init__(self, cog, entries: list, *, tag: Optional[str] = None, author_id: Optional[int] = None):
        super().__init__(timeout=300)
        self.cog = cog
        self.entries = entries      # 마지막으로 성공한 목록 (파일을 못 읽었을 때 쓰는 예비용)
        self.tag = tag
        self.author_id = author_id
        self.page = 0
        self.newest_first = True
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.author_id is None or interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "🙅‍♀️ 이 버튼은 `/연대기 보기`를 실행한 사람만 누를 수 있어요.\n"
            "└ 직접 넘겨 보시려면 `/연대기 보기`를 불러주세요. (연대기 관리자 전용이에요)",
            ephemeral=True,
        )
        return False

    # ---------- 목록 계산 ----------
    @staticmethod
    def _sort_key(entry: dict):
        """날짜 → 등록 번호 순. 같은 날 여러 건이면 나중에 넣은 게 뒤로 갑니다."""
        try:
            seq = int(entry.get("id", 0))
        except (TypeError, ValueError):
            seq = 0
        return (entry.get("date", ""), seq)

    def _rows(self) -> list:
        # 🔄 [개선] 예전엔 창을 연 순간의 목록을 그대로 들고 5분을 살았어요. 그동안 누가
        #    `/연대기 기록`으로 새 사건을 남기거나 지워도 이 화면에는 반영되지 않아서,
        #    페이지를 넘겨도 없는 기록이 보이거나 새 기록이 안 보였습니다.
        #    이제 버튼을 누를 때마다 파일을 다시 읽어요. 연대기 파일은 작고 버튼도 가끔
        #    눌리는 거라 부담이 없습니다.
        #    ⚠️ 읽기에 실패하면(파일 손상 등) 창을 깨뜨리지 않고 마지막으로 성공한 목록을
        #       그대로 씁니다. 읽는 화면이 저장 사고까지 떠안을 이유는 없으니까요.
        if self.cog is not None:
            try:
                self.entries = self.cog._load()["entries"]
            except Exception as e:
                print(f"⚠️ [연대기] 목록을 다시 읽지 못해 직전 화면을 그대로 보여줘요: "
                      f"{type(e).__name__}: {e}")

        rows = self.entries
        if self.tag:
            rows = [e for e in rows if e.get("tag") == self.tag]
        return sorted(rows, key=self._sort_key, reverse=self.newest_first)

    def build_embed(self) -> discord.Embed:
        rows = self._rows()
        total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        # 정렬을 뒤집거나 필터가 바뀌면 페이지 번호가 범위를 벗어날 수 있어서 여기서 맞춰줍니다.
        self.page = max(0, min(self.page, total_pages - 1))

        title = f"📜 {server_name()} 연대기"
        if self.tag:
            title += f" · {TAG_EMOJI.get(self.tag, '')}{self.tag}"
        embed = discord.Embed(title=title, color=0x5CE6B4)

        if not rows:
            embed.description = "아직 기록된 사건이 없어요. `/연대기 기록`으로 첫 장을 남겨보세요!"
            return embed

        for entry in rows[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]:
            tag = entry.get("tag", "사건")
            name = f"{entry.get('date', '????-??-??')} · {TAG_EMOJI.get(tag, '•')} {tag}"

            value = f"**{entry.get('title', '(제목 없음)')}**"
            if entry.get("detail"):
                value += f"\n{entry['detail'][:DETAIL_LIMIT]}"
            if entry.get("message_link"):
                value += f"\n[원본 메시지 보기]({entry['message_link']})"
            value += f"\n`#{entry.get('id', '?')}`"

            embed.add_field(name=name[:256], value=value[:1024], inline=False)

        embed.set_footer(
            text=f"{self.page + 1}/{total_pages} 페이지 · 총 {len(rows)}건 · "
                 f"{'최신순' if self.newest_first else '오래된 순'}"
        )
        return embed

    async def _refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        # ⌛ 시간이 지나면 버튼만 잠급니다. 내용은 계속 읽을 수 있게 남겨둬요.
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    # ---------- 버튼 ----------
    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self._refresh(interaction)

    @discord.ui.button(label="다음 ▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._refresh(interaction)

    @discord.ui.button(label="🔀 정렬 뒤집기", style=discord.ButtonStyle.primary)
    async def flip_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.newest_first = not self.newest_first
        self.page = 0
        await self._refresh(interaction)


class ChroniclePinModal(discord.ui.Modal, title="📜 연대기에 박제"):
    """메시지를 우클릭해서 박제할 때 뜨는 창. 제목만 적으면 나머지는 자동으로 채워져요."""

    제목 = discord.ui.TextInput(
        label="제목",
        placeholder="한 줄로 요약해 주세요 (예: 길드 첫 우승)",
        max_length=100,
    )
    메모 = discord.ui.TextInput(
        label="덧붙일 메모 (선택)",
        placeholder="왜 기록할 만한 일인지 적어두면 나중에 읽기 좋아요",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, cog, message: discord.Message):
        super().__init__()
        self.cog = cog
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.pin_submit(interaction, self.message, str(self.제목), str(self.메모))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        """모달은 MariView와 상속 관계가 아니라서 오류 처리를 따로 붙여둬요.

        🐛 [버그 수정] 예전엔 무조건 send_message를 불렀어요. 그런데 오류가 응답을 이미
        보낸 **뒤에** 나면 send_message가 또 실패해서, 아래 except에 걸려 유저 화면에는
        아무것도 안 뜹니다. 이미 응답했는지 보고 갈라야 해요. (MariView.on_error와 같은 방식)
        """
        print(f"❗ [연대기] 박제 처리 중 오류: {type(error).__name__}: {error}")
        msg = f"❌ 박제하지 못했어요. (`{type(error).__name__}`)"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception as e:
            print(f"❗ [연대기] 오류 안내 전송 실패: {e}")


class MariChronicle(commands.Cog):
    """📜 서버 연대기"""

    연대기 = app_commands.Group(name="연대기", description="서버에서 일어난 일을 기록하고 되돌아봅니다.")

    def __init__(self, bot):
        self.bot = bot
        # ⚠️ 우클릭(컨텍스트) 메뉴는 코그 안에서 @app_commands.context_menu() 데코레이터로
        #    만들 수 없어요. ContextMenu 객체를 직접 만들어 트리에 넣어야 합니다.
        #    그리고 코그가 내려갈 때 **반드시 다시 빼야** 해요. 안 빼면 코그를 다시 불러올
        #    때마다 같은 메뉴가 중복 등록됩니다.
        # 🏠 서버 전용 설정은 따로 안 붙입니다. mari_client의 tree.interaction_check가
        #    DM에서 들어온 인터랙션을 전부 먼저 걸러줘요. (우클릭 메뉴도 그 검사를 지나갑니다)
        self.pin_menu = app_commands.ContextMenu(name="연대기에 박제", callback=self.pin_message)
        self.bot.tree.add_command(self.pin_menu)

    async def cog_unload(self):
        self.bot.tree.remove_command(self.pin_menu.name, type=self.pin_menu.type)

    # ========== 🔐 권한 ==========
    def _is_chronicle_admin(self, user) -> bool:
        """`/설정 관리자 연대기`로 등록된 역할 보유자(또는 서버 관리자)인지 확인합니다.

        DM에서 들어오면 user가 Member가 아니라 User라서 자동으로 False가 돼요. (안전한 기본값)
        """
        return member_has_admin_or_role(user, "chronicle_admin")

    @staticmethod
    def _deny_message() -> str:
        return ("⛔ 연대기는 **연대기 관리자**만 다룰 수 있어요.\n"
                "└ `/설정 관리자 연대기`로 역할을 지정할 수 있어요. (서버 관리자·채널관리자 전용)")

    # ========== 💾 저장소 ==========
    def _load(self) -> dict:
        data = safe_json_load(CHRONICLE_FILE, None)
        if not isinstance(data, dict):
            return {"next_id": 1, "entries": []}
        data.setdefault("next_id", 1)
        data.setdefault("entries", [])
        return data

    def _save(self, data: dict):
        atomic_json_save_or_raise(CHRONICLE_FILE, data, indent=2)

    def _add_entry(self, *, title: str, detail: str, date: str, tag: str, source: str,
                   author_id=None, message_link: str = None) -> dict:
        """연대기에 한 줄 추가합니다.

        ⚠️ 이 함수 안에는 await이 하나도 없어요. 읽고→고치고→저장 사이에 await이 끼면
        그 틈에 들어온 다른 기록이 통째로 되돌아갑니다. (마리봇 전체가 지키는 규칙이에요)
        """
        data = self._load()
        entry = {
            "id": str(data["next_id"]),
            "date": date,
            "title": (title or "").strip()[:100],
            "detail": (detail or "").strip(),
            "tag": tag,
            "source": source,               # manual(직접) / pin(우클릭) / auto(자동)
            "author_id": str(author_id) if author_id else None,
            "message_link": message_link,
            "created_at": dt.datetime.now(KST).isoformat(),
        }
        data["next_id"] += 1
        data["entries"].append(entry)
        self._save(data)
        return entry

    # ========== 🗓️ 날짜 처리 ==========
    @staticmethod
    def _parse_date(text: Optional[str]) -> tuple:
        """'2026-08-09' / '2026/8/9' / '2026.8.9' 를 모두 받아줍니다. (날짜, 오류메세지)

        📌 사건이 일어난 날과 기록한 날을 일부러 나눠뒀어요. 연대기는 만들자마자
        "그러고 보니 작년에 이런 일도 있었지"부터 채우게 되는데, 날짜를 못 정하면
        전부 오늘로 쌓여서 연대기가 되지 않거든요.
        """
        today = dt.datetime.now(KST).date()
        if not text or not text.strip():
            return today.isoformat(), None

        parts = text.strip().replace("/", "-").replace(".", "-").split("-")
        if len(parts) != 3:
            return None, "❌ 날짜는 `2026-08-09` 형식으로 넣어주세요. (`/`나 `.`로 구분해도 돼요)"
        try:
            parsed = dt.date(*(int(p) for p in parts))
        except (ValueError, TypeError):
            return None, "❌ 날짜를 알아볼 수 없어요. `2026-08-09` 형식으로 넣어주세요."

        if parsed > today:
            # 연대기는 '일어난 일'을 남기는 곳이에요. 앞으로의 일정은 다른 기능의 몫입니다.
            return None, "❌ 아직 오지 않은 날짜는 기록할 수 없어요. 연대기는 **일어난 일**을 남기는 곳이에요."
        return parsed.isoformat(), None

    def _entry_embed(self, entry: dict, title: str) -> discord.Embed:
        """기록 직후 '이렇게 남았어요'를 보여주는 확인용 임베드."""
        tag = entry.get("tag", "사건")
        embed = discord.Embed(
            title=title,
            description=f"**{entry['title']}**",
            color=0x5CE6B4,
        )
        embed.add_field(name="날짜", value=entry["date"], inline=True)
        embed.add_field(name="분류", value=f"{TAG_EMOJI.get(tag, '•')} {tag}", inline=True)
        embed.add_field(name="번호", value=f"`#{entry['id']}`", inline=True)
        if entry.get("detail"):
            embed.add_field(name="내용", value=entry["detail"][:1024], inline=False)
        if entry.get("message_link"):
            embed.add_field(name="원본", value=f"[메시지로 이동]({entry['message_link']})", inline=False)
        embed.set_footer(text="고치려면 /연대기 수정, 지우려면 /연대기 삭제 에 이 번호를 넣어주세요")
        return embed

    # ========== ✍️ 기록 ==========
    @연대기.command(name="기록", description="[연대기 관리자] 서버에서 일어난 일을 연대기에 남깁니다.")
    @app_commands.describe(
        제목="한 줄 요약 (예: 서버 인원 100명 돌파)",
        내용="자세한 설명 (생략 가능)",
        날짜="사건이 일어난 날. 생략하면 오늘이에요 (예: 2026-08-09)",
        분류="사건 종류. 생략하면 '사건'이에요",
    )
    @app_commands.choices(분류=[app_commands.Choice(name=t, value=t) for t in CHRONICLE_TAGS])
    async def record(self, interaction: discord.Interaction, 제목: str,
                     내용: Optional[str] = None, 날짜: Optional[str] = None,
                     분류: Optional[app_commands.Choice[str]] = None):
        if not self._is_chronicle_admin(interaction.user):
            return await interaction.response.send_message(self._deny_message(), ephemeral=True)

        date, err = self._parse_date(날짜)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)

        if not 제목.strip():
            return await interaction.response.send_message("❌ 제목은 비워둘 수 없어요.", ephemeral=True)

        entry = self._add_entry(
            title=제목, detail=내용 or "", date=date,
            tag=분류.value if 분류 else "사건",
            source="manual", author_id=interaction.user.id,
        )
        # 👀 기록 결과는 남긴 사람에게만 보여줍니다. (채널을 어지럽히지 않게)
        await interaction.response.send_message(
            embed=self._entry_embed(entry, "📜 연대기에 남겼어요"), ephemeral=True
        )

    # ========== 🖱️ 메시지 우클릭 → 연대기에 박제 ==========
    async def pin_message(self, interaction: discord.Interaction, message: discord.Message):
        """메시지를 우클릭해서 바로 연대기에 올립니다.

        명령어를 치려면 "지금 이걸 기록해야지" 하고 마음을 먹어야 하는데, 정작 기록할 만한
        순간은 대개 그냥 지나가요. 우클릭 한 번이면 날짜·작성자·원본 링크가 자동으로 채워집니다.
        """
        if not self._is_chronicle_admin(interaction.user):
            return await interaction.response.send_message(self._deny_message(), ephemeral=True)
        await interaction.response.send_modal(ChroniclePinModal(self, message))

    async def pin_submit(self, interaction: discord.Interaction, message: discord.Message,
                         title: str, memo: str):
        """우클릭 박제 창에서 '제출'을 눌렀을 때 실제로 기록하는 부분."""
        origin = (message.content or "").strip()
        detail_parts = []
        if memo.strip():
            detail_parts.append(memo.strip())
        if origin:
            detail_parts.append(f"> {origin[:DETAIL_LIMIT]}")
        if not origin and message.attachments:
            detail_parts.append("> (사진·파일 메시지)")

        # 📅 날짜는 '지금'이 아니라 **그 메시지가 쓰인 날**이에요. 과거 메시지를 뒤늦게
        #    박제해도 연대기에서는 제자리에 꽂힙니다.
        date = message.created_at.astimezone(KST).date().isoformat()

        entry = self._add_entry(
            title=title,
            detail="\n".join(detail_parts),
            date=date,
            tag="사건",
            source="pin",
            author_id=message.author.id,
            message_link=message.jump_url,
        )
        await interaction.response.send_message(
            embed=self._entry_embed(entry, "📜 연대기에 박제했어요"), ephemeral=True
        )

    # ========== 🎙️ 온에어(무대) 채널 자동 기록 ==========
    @commands.Cog.listener()
    async def on_stage_instance_create(self, stage_instance: discord.StageInstance):
        """온에어(무대) 채널에서 방송이 시작되면 자동으로 연대기에 남깁니다.

        디스코드가 무대 채널을 열 때 보내주는 이벤트예요. 별도 인텐트 없이 기본(guilds)으로
        받을 수 있습니다.

        🛡️ 이벤트 처리 중에 예외가 나면 로그만 남기고 넘어가요. 연대기 기록이 실패했다고
           해서 방송 자체에 문제가 생기면 안 되니까요.
        """
        try:
            topic = (stage_instance.topic or "").strip() or "제목 없는 방송"
            today = dt.datetime.now(KST).date().isoformat()

            # 🐛 [버그 수정] 중복 판정을 title.endswith(topic)으로 하고 있었어요. 그런데 저장될 때
            #    제목이 100자로 잘리기 때문에, 주제가 길면 잘린 제목이 topic으로 끝나지 않아
            #    **중복 검사가 항상 빗나갔습니다.** 방송을 열고 닫을 때마다 계속 쌓였어요.
            #    이제 저장될 제목을 먼저 만들어두고 그 값과 통째로 비교합니다.
            title = f"온에어: {topic}"[:100]

            # 🔁 같은 방송이 하루에 여러 번 열리고 닫히면 연대기가 도배돼요.
            #    같은 날·같은 제목으로 이미 남아 있으면 건너뜁니다.
            for entry in self._load()["entries"]:
                if (entry.get("tag") == "온에어" and entry.get("date") == today
                        and entry.get("title") == title):
                    return

            channel = stage_instance.channel
            self._add_entry(
                title=title,
                detail=(f"{channel.mention}에서 방송이 시작됐어요." if channel
                        else "무대 채널에서 방송이 시작됐어요."),
                date=today,
                tag="온에어",
                source="auto",
            )
            print(f"📜 [연대기] 온에어 자동 기록: {topic}")
        except Exception as e:
            print(f"⚠️ [연대기] 온에어 자동 기록 실패: {type(e).__name__}: {e}")

    # ========== 📖 보기 ==========
    @연대기.command(name="보기", description="[연대기 관리자] 쌓인 연대기를 펼쳐 봅니다. (결과는 모두에게 보여요)")
    @app_commands.describe(분류="특정 분류만 보고 싶을 때 (생략하면 전부)")
    @app_commands.choices(분류=[app_commands.Choice(name=t, value=t) for t in CHRONICLE_TAGS]
                          + [app_commands.Choice(name="온에어", value="온에어")])
    async def show(self, interaction: discord.Interaction, 분류: Optional[app_commands.Choice[str]] = None):
        # 🔐 펼치는 건 관리자만, 다만 펼쳐진 결과는 채널에 공개돼서 모두가 함께 봐요.
        if not self._is_chronicle_admin(interaction.user):
            return await interaction.response.send_message(self._deny_message(), ephemeral=True)

        entries = self._load()["entries"]
        view = ChroniclePageView(
            self, entries,
            tag=분류.value if 분류 else None,
            author_id=interaction.user.id,      # 페이지 버튼은 실행한 사람만 누를 수 있어요
        )

        await interaction.response.send_message(embed=view.build_embed(), view=view)
        # 타임아웃 때 버튼을 잠그려면 메세지 핸들이 필요해요. 실패해도 버튼은 잘 동작합니다.
        try:
            view.message = await interaction.original_response()
        except Exception:
            pass

    # ========== ✏️ 수정 ==========
    @연대기.command(name="수정", description="[연대기 관리자] 이미 남긴 기록을 고칩니다. (번호는 목록의 #숫자)")
    @app_commands.describe(
        번호="고칠 기록의 번호 (예: 12 또는 #12)",
        제목="새 제목으로 바꾸고 싶을 때",
        내용="새 내용으로 바꾸고 싶을 때",
        날짜="사건 날짜를 바로잡고 싶을 때 (예: 2026-08-09)",
        분류="분류를 바꾸고 싶을 때",
    )
    @app_commands.choices(분류=[app_commands.Choice(name=t, value=t) for t in CHRONICLE_TAGS]
                          + [app_commands.Choice(name="온에어", value="온에어")])
    async def edit(self, interaction: discord.Interaction, 번호: str,
                   제목: Optional[str] = None, 내용: Optional[str] = None,
                   날짜: Optional[str] = None, 분류: Optional[app_commands.Choice[str]] = None):
        if not self._is_chronicle_admin(interaction.user):
            return await interaction.response.send_message(self._deny_message(), ephemeral=True)

        # 제목·날짜는 빈칸으로 들어오면 '안 건드림'으로 봅니다.
        # (특히 날짜를 빈칸으로 두면 _parse_date가 오늘을 돌려줘서, 손대지도 않은 날짜가
        #  조용히 오늘로 밀려버려요)
        # 내용만은 예외로, 빈칸을 넣으면 **내용을 지우는** 뜻으로 씁니다. 잘못 적은 설명을
        # 통째로 비울 방법이 달리 없거든요.
        새제목 = 제목.strip() if 제목 and 제목.strip() else None
        새내용 = 내용.strip() if 내용 is not None else None
        새분류 = 분류.value if 분류 else None

        새날짜 = None
        if 날짜 and 날짜.strip():
            새날짜, err = self._parse_date(날짜)
            if err:
                return await interaction.response.send_message(err, ephemeral=True)

        if 새제목 is None and 새내용 is None and 새날짜 is None and 새분류 is None:
            return await interaction.response.send_message(
                "❌ 바꿀 내용을 하나 이상 넣어주세요. (제목·내용·날짜·분류)", ephemeral=True
            )

        target = 번호.strip().lstrip("#")
        data = self._load()
        entry = next((e for e in data["entries"] if e.get("id") == target), None)
        if entry is None:
            return await interaction.response.send_message(
                f"❌ `#{target}` 번 기록을 찾을 수 없어요. `/연대기 보기`에서 번호를 확인해 주세요.",
                ephemeral=True,
            )

        # 🐛 바꾼 값을 대입하기 **전에** 기록을 남깁니다. 대입부터 하면 'A ➡️ B'가 아니라
        #    새 값만 두 번 찍혀서 뭐가 바뀌었는지 알 수 없어요. (상점 항목설정에서 겪은 문제)
        logs = []
        if 새제목 is not None:
            logs.append(f"제목: {entry.get('title', '(없음)')} ➡️ {새제목}")
            entry["title"] = 새제목[:100]
        if 새내용 is not None:
            logs.append("내용 수정" if 새내용 else "내용 비움")
            entry["detail"] = 새내용
        if 새날짜 is not None:
            logs.append(f"날짜: {entry.get('date', '(없음)')} ➡️ {새날짜}")
            entry["date"] = 새날짜
        if 새분류 is not None:
            logs.append(f"분류: {entry.get('tag', '사건')} ➡️ {새분류}")
            entry["tag"] = 새분류

        # 누가 언제 손댔는지 남겨둬요. 여럿이 함께 쓰는 기록이라 흔적이 있어야 합니다.
        entry["edited_at"] = dt.datetime.now(KST).isoformat()
        entry["edited_by"] = str(interaction.user.id)
        self._save(data)

        embed = self._entry_embed(entry, "✏️ 기록을 고쳤어요")
        embed.add_field(name="바뀐 것", value="\n".join(f"└ {l}" for l in logs)[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ========== 🗑️ 삭제 ==========
    @연대기.command(name="삭제", description="[연대기 관리자] 잘못 남긴 기록을 지웁니다. (번호는 목록의 #숫자)")
    @app_commands.describe(번호="지울 기록의 번호 (예: 12 또는 #12)")
    async def delete(self, interaction: discord.Interaction, 번호: str):
        if not self._is_chronicle_admin(interaction.user):
            return await interaction.response.send_message(self._deny_message(), ephemeral=True)

        target = 번호.strip().lstrip("#")
        data = self._load()
        remaining = [e for e in data["entries"] if e.get("id") != target]

        if len(remaining) == len(data["entries"]):
            return await interaction.response.send_message(
                f"❌ `#{target}` 번 기록을 찾을 수 없어요. `/연대기 보기`에서 번호를 확인해 주세요.",
                ephemeral=True,
            )

        # 🔢 next_id는 일부러 되돌리지 않아요. 번호를 재사용하면 예전 기록을 가리키던
        #    번호가 다른 사건을 가리키게 돼서 헷갈립니다.
        data["entries"] = remaining
        self._save(data)
        await interaction.response.send_message(f"🗑️ `#{target}` 번 기록을 지웠어요.", ephemeral=True)
