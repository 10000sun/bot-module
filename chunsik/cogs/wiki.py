"""ChunsikWiki — 멤버 위키(소개/취미/MBTI 등) 등록·조회.

🗣️ [말투] 이 코그만 **반말**로 말하고 있었어요("등록됐어!", "위키 정보가 없어!").
   나머지 스무 개 코그는 전부 존댓말(~어요/~해요)이라, 같은 봇인데 위키를 쓸 때만
   갑자기 말투가 바뀌었습니다. 원본 봇에서 옮겨오며 남은 자리예요.
   납품물이라 더 눈에 띕니다 — 클라이언트 입장에선 고장이나 미완성으로 보여요.
   문구 일곱 개를 존댓말로 맞췄고, `check_modules`의 '말투' 검사가 지킵니다.
"""

import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands

from chunsik_config import KST
from chunsik_settings import feature_gate, has_admin_or_role, send_log_embed
from chunsik_utils import EMBED_DESC_LIMIT, EMBED_FIELD_LIMIT, INPUT_ECHO_LIMIT, chunk_lines, clip, fit_embed
from chunsik_state import load_wiki, save_wiki
from chunsik_names import server_name

# 📏 [버그 수정] 임베드 필드 값 하나의 상한은 1024자예요. 넘기면 그 필드만 잘리는 게 아니라
# **메세지 전송 자체가 400으로 실패**해서 그 사람 위키를 아예 조회할 수 없게 됩니다.
# `논란`·`TMI`는 길이 제한 없는 자유 입력이라 실제로 넘길 수 있어서, 보여줄 때 잘라둡니다.
FIELD_LIMIT = 1024
_TRUNCATED_NOTE = "\n… (내용이 길어 생략했어요)"

# 📚 `/위키 목록` 한 줄이 차지할 수 있는 길이. 소개는 상한 없는 자유 입력이라,
#    한 명이 길게 적으면 그 줄 하나가 페이지 한도를 통째로 먹습니다.
LIST_LINE_LIMIT = 150
# 📄 목록이 몇 페이지까지 나갈지. 사람이 아주 많은 서버에서 봇이 도배하지 않게 막아둡니다.
LIST_MAX_PAGES = 5


def build_wiki_list_pages(rows: list) -> tuple:
    """(이름, 아이디, 소개) 목록을 임베드 설명 한도에 맞는 페이지들로 나눕니다.

    → (페이지 목록, 못 담은 인원 수)

    🐛 [버그 수정] 예전엔 전부 한 덩어리로 이어 붙인 뒤 4000자가 넘으면
    **"⚠️ 목록이 너무 길어. 나중에 검색 기능도 추가할게!"** 한 줄만 내보냈어요.
    나중은 오지 않은 채로 납품물에 들어갔고, 사람이 늘면 `/위키 목록`이 그냥 안 됩니다.
    등록·조회·수정·삭제는 다 되는데 목록만 영영 못 보는 상태였어요.

    🚨 나누는 것만으로는 부족합니다. `chunk_lines`는 줄과 줄 **사이**에서만 나눠요.
       소개에 긴 글을 적은 사람이 하나 있으면 그 줄이 혼자 한 페이지가 되어, 나눠도
       여전히 한도를 넘습니다. (#11 명단이 굳던 것과 같은 부류) 줄부터 잘라둬요.

    🔎 모듈 바깥에 둔 이유: 검사 도구(tools/check_embeds.py)가 진짜 코드를 그대로
       불러서 재게 하려고요. 베껴 쓰면 코드가 바뀔 때 옛 규칙을 계속 통과시킵니다.
    """
    lines = [clip(f"• `{name}` (`{uid}`) – {intro or '-'}", LIST_LINE_LIMIT)
             for name, uid, intro in rows]
    pages = chunk_lines(lines, limit=EMBED_DESC_LIMIT)
    dropped = 0
    if len(pages) > LIST_MAX_PAGES:
        kept_lines = sum(page.count("\n") for page in pages[:LIST_MAX_PAGES])
        dropped = len(lines) - kept_lines
        pages = pages[:LIST_MAX_PAGES]
    return pages, dropped


def _fit_field(value: str) -> str:
    """임베드 필드에 안전하게 들어가는 길이로 다듬습니다. 비어 있으면 '-'."""
    text = (value or "").replace("\\n", "\n").strip()
    if not text:
        return "-"
    if len(text) <= FIELD_LIMIT:
        return text
    return text[:FIELD_LIMIT - len(_TRUNCATED_NOTE)].rstrip() + _TRUNCATED_NOTE


# 🖋️ 마지막으로 손댄 사람. 위키 항목 이름(소개·생일·…)과 겹치지 않게 밑줄로 시작해요.
_EDITOR_KEY = "_edited_by"
_EDITED_AT_KEY = "_edited_at"
_EDITOR_NAME_LIMIT = 60


def stamp_editor(entry: dict, editor_name: str) -> dict:
    """이 항목을 방금 누가 고쳤는지 적어둡니다."""
    entry[_EDITOR_KEY] = clip(editor_name or "", _EDITOR_NAME_LIMIT)
    entry[_EDITED_AT_KEY] = dt.datetime.now(KST).strftime("%Y-%m-%d")
    return entry


def editor_footer(entry: dict) -> str:
    """조회 화면 아래에 붙는 '마지막으로 고친 사람' 한 줄.

    🐛 [버그 수정] 예전엔 `last edit by {interaction.user.display_name}` 이었어요.
    그 interaction은 **조회한 사람**입니다. 즉 누가 열어보든 자기 이름이 "마지막
    편집자"로 찍혔어요. 아무도 안 고쳤는데 고친 것처럼 보이고, 진짜 고친 사람은
    어디에도 안 남습니다. 오류가 안 나서 티가 안 나는 종류의 거짓말이에요.
    이제 등록·수정할 때 적어둔 사람을 보여줍니다.
    """
    name = entry.get(_EDITOR_KEY)
    if not name:
        # 이 기능이 생기기 전에 등록된 항목이에요. 없는 이름을 지어내지 않습니다.
        return "last edit by 기록 없음"
    when = entry.get(_EDITED_AT_KEY)
    return f"last edit by {name}" + (f" · {when}" if when else "")


class ChunsikWiki(commands.Cog):
    """서버 위키 시스템"""

    # 📌 예시 항목에 '생일'을 쓰지 않아요. 위키 항목은 자유 입력이라 생일을 적어도 되지만,
    #    생일 모듈을 안 담은 서버의 명령 목록에 '생일'이라는 글자가 뜨면 그 기능이 있는
    #    줄 알고 찾습니다. (tools/check_help.py의 낱말 검사에 실제로 걸렸어요)
    wiki_group = app_commands.Group(name="위키", description="멤버 위키(소개/취미/MBTI 등) 관련 명령어 모음")

    @wiki_group.command(name="등록", description="[관리자] 새로운 위키 항목을 등록해요")
    @app_commands.describe(member="등록할 멤버", 소개="한 줄 소개", 생일="예: 2000.01.01", 서식지="사는 곳", mbti="MBTI (예: INFP)", 논란="논란 및 사건 사고 (줄바꿈 가능)", tmi="TMI (줄바꿈 가능)")
    async def register_wiki(self, interaction: discord.Interaction, member: discord.Member, 소개: str, 생일: str, 서식지: str, mbti: str, 논란: str, tmi: str):
        if await feature_gate(interaction, "wiki", "위키"):
            return
        # ⚙️ 위키는 아이디 관리자(settings.json의 roles.ids_admin)가 함께 관리해요.
        if not has_admin_or_role(interaction, "ids_admin"):
            await interaction.response.send_message("⛔ 위키를 관리할 권한이 없어요!", ephemeral=True)
            return

        await interaction.response.defer()
        user_id = str(member.id)
        data = load_wiki()
        if "wiki" not in data:
            data["wiki"] = {}

        data["wiki"][user_id] = stamp_editor({
            "소개": 소개,
            "생일": 생일,
            "서식지": 서식지,
            "MBTI": mbti,
            "논란": 논란.replace("\\n", "\n"),
            "TMI": tmi.replace("\\n", "\n"),
        }, interaction.user.display_name)
        save_wiki(data)

        # 조회 화면에서 잘릴 항목이 있으면 등록한 사람에게 미리 알려줘요.
        # (모르고 길게 적으면 "왜 뒷부분이 안 보이지?" 하게 되니까요)
        long_fields = [k for k in ("논란", "TMI") if len(data["wiki"][user_id][k]) > FIELD_LIMIT]
        note = (f"\n⚠️ `{'`·`'.join(long_fields)}` 항목이 {FIELD_LIMIT}자를 넘어서, 조회할 땐 뒷부분이 생략돼요."
                if long_fields else "")
        await interaction.followup.send(f"📚 `{member.display_name}` 님의 위키를 등록했어요!{note}")

    @wiki_group.command(name="조회", description="멤버의 위키 정보를 조회해요")
    @app_commands.describe(member="조회할 멤버")
    async def view_wiki(self, interaction: discord.Interaction, member: discord.Member):
        user_id = str(member.id)
        data = load_wiki()
        wiki = data.get("wiki", {})
        if user_id not in wiki:
            await interaction.response.send_message("❌ 아직 등록된 위키가 없어요.", ephemeral=True)
            return

        entry = wiki[user_id]
        embed = discord.Embed(
            title=member.display_name,
            description=f"좌우명: {_fit_field(entry.get('소개'))}",
            color=0x89CFF0,
        )
        embed.add_field(name="\u200B", value="\u200B", inline=False)
        embed.add_field(name="🎂 생일", value=_fit_field(entry.get("생일")), inline=True)
        embed.add_field(name="📍 서식지", value=_fit_field(entry.get("서식지")), inline=True)
        embed.add_field(name="🧠 MBTI", value=_fit_field(entry.get("MBTI")), inline=True)
        embed.add_field(name="\u200B", value="\u200B", inline=False)
        embed.add_field(name="🌀 논란 및 사건 사고", value=_fit_field(entry.get("논란")), inline=False)
        embed.add_field(name="\u200B", value="\u200B", inline=False)
        embed.add_field(name="📎 TMI", value=_fit_field(entry.get("TMI")), inline=False)
        embed.set_footer(text=editor_footer(entry))
        # 🧮 칸마다 1024자로 잘라도 여섯 칸이 쌓이면 합이 6000자를 넘어 조회가 통째로 실패해요.
        #    (위 FIELD_LIMIT은 "칸 하나" 기준이라, 칸이 여러 개면 그것만으론 모자랍니다)
        fit_embed(embed)
        await interaction.response.send_message(embed=embed)

    @wiki_group.command(name="수정", description="[관리자] 위키 항목 중 하나를 수정해요")
    @app_commands.describe(member="수정할 멤버", 항목="수정할 항목명", 내용="새로운 내용")
    @app_commands.choices(
        항목=[
            app_commands.Choice(name="소개", value="소개"),
            app_commands.Choice(name="생일", value="생일"),
            app_commands.Choice(name="서식지", value="서식지"),
            app_commands.Choice(name="MBTI", value="MBTI"),
            app_commands.Choice(name="논란", value="논란"),
            app_commands.Choice(name="TMI", value="TMI"),
        ]
    )
    async def edit_wiki(self, interaction: discord.Interaction, member: discord.Member, 항목: app_commands.Choice[str], 내용: str):
        if await feature_gate(interaction, "wiki", "위키"):
            return
        # ⚙️ 위키는 아이디 관리자(settings.json의 roles.ids_admin)가 함께 관리해요.
        if not has_admin_or_role(interaction, "ids_admin"):
            await interaction.response.send_message("⛔ 위키를 관리할 권한이 없어요!", ephemeral=True)
            return

        await interaction.response.defer()
        user_id = str(member.id)
        data = load_wiki()
        if user_id not in data.get("wiki", {}):
            await interaction.followup.send("❌ 그 멤버의 위키가 없어요. 먼저 `/위키 등록`을 해주세요.", ephemeral=True)
            return

        field_name = 항목.value
        new_value = 내용.replace("\\n", "\n") if field_name in ("논란", "TMI") else 내용
        data["wiki"][user_id][field_name] = new_value
        stamp_editor(data["wiki"][user_id], interaction.user.display_name)
        save_wiki(data)
        # 🧾 [신규] 남의 프로필을 고치는 명령이라 흔적이 남아야 해요.
        #    ⚠️ 이 코그는 __init__이 없어서 self.bot이 **없어요.** interaction.client를 씁니다.
        #    ✂️ 새 내용은 유저가 자유롭게 적는 값이라 칸 한도를 넘길 수 있습니다.
        await send_log_embed(
            interaction.client, "wiki_log", f"{member.mention} 님의 위키를 고쳤어요.",
            fields=[
                ("항목", field_name, True),
                ("처리 관리자", interaction.user.mention, True),
                ("새 내용", clip(new_value, EMBED_FIELD_LIMIT) or "*(비움)*", False),
            ],
            guild=interaction.guild,
        )
        note = (f"\n⚠️ {FIELD_LIMIT}자를 넘어서 조회할 땐 뒷부분이 생략돼요."
                if len(new_value) > FIELD_LIMIT else "")
        await interaction.followup.send(f"✅ `{member.display_name}` 님의 `{field_name}` 항목을 수정했어요!{note}", ephemeral=True)

    @wiki_group.command(name="삭제", description="[관리자] 해당 ID의 위키를 삭제해요")
    @app_commands.describe(user_id="삭제할 대상의 Discord ID")
    @app_commands.guild_only()
    async def delete_wiki(self, interaction: discord.Interaction, user_id: str):
        if await feature_gate(interaction, "wiki", "위키"):
            return
        # ⚙️ 위키는 아이디 관리자(settings.json의 roles.ids_admin)가 함께 관리해요.
        if not has_admin_or_role(interaction, "ids_admin"):
            await interaction.response.send_message("⛔ 위키를 관리할 권한이 없어요!", ephemeral=True)
            return

        await interaction.response.defer()
        data = load_wiki()
        if user_id not in data.get("wiki", {}):
            await interaction.followup.send("❌ 그 아이디로 등록된 위키가 없어요.", ephemeral=True)
            return

        # 🧾 [신규] **지우면 흔적이 통째로 사라지던** 자리예요. 지우기 전에 무엇이 있었는지
        #    한 줄이라도 남겨둡니다. (편집자 표시는 남겨도 지워진 내용은 못 되살리니까요)
        gone = data["wiki"][user_id]
        del data["wiki"][user_id]
        save_wiki(data)
        filled = ", ".join(k for k, v in gone.items() if v and k not in ("편집자", "수정일"))
        await send_log_embed(
            interaction.client, "wiki_log", f"<@{user_id}> 님의 위키를 **지웠어요.**",
            fields=[
                ("처리 관리자", interaction.user.mention, True),
                ("지워진 항목", clip(filled, EMBED_FIELD_LIMIT) or "*(비어 있었어요)*", False),
            ],
            guild=interaction.guild,
        )
        await interaction.followup.send(f"🗑️ `{clip(user_id, INPUT_ECHO_LIMIT)}` 의 위키를 지웠어요.")

    @wiki_group.command(name="목록", description="등록된 모든 위키 항목을 보여줘요")
    @app_commands.guild_only()
    async def list_wiki(self, interaction: discord.Interaction):
        await interaction.response.defer()
        data = load_wiki()
        wiki = data.get("wiki", {})
        if not wiki:
            await interaction.followup.send("❌ 아직 등록된 위키가 하나도 없어요.", ephemeral=True)
            return

        rows = []
        for uid in sorted(wiki.keys()):
            try:
                member = interaction.guild.get_member(int(uid))
                name = member.display_name if member else f"탈퇴자 ({uid})"
            except ValueError:
                # uid가 정수로 변환 안 되는 손상된 키인 경우만 여기로 옵니다.
                name = f"탈퇴자 ({uid})"
            rows.append((name, uid, wiki[uid].get("소개", "-")))

        pages, dropped = build_wiki_list_pages(rows)
        for i, page in enumerate(pages, start=1):
            title = f"📚 {server_name()} 위키 목록"
            if len(pages) > 1:
                title += f" ({i}/{len(pages)})"
            embed = discord.Embed(title=title, description=page, color=0xA9CCE3)
            if i == len(pages) and dropped:
                embed.set_footer(text=f"등록된 {len(rows)}명 중 {dropped}명은 여기 다 못 담았어요. "
                                      "`/위키 조회`로 개별 확인해 주세요.")
            await interaction.followup.send(embed=embed)
