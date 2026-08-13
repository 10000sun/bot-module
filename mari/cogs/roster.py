"""MariRoster — 레벨 역할 기반 멤버 명단."""

import datetime as dt
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands

from mari_config import KST
from mari_settings import has_admin_or_role
from mari_utils import chunk_lines

class MariRoster(commands.Cog):
    """레벨별, 소속별 맞춤 필터링 및 정렬이 가능한 서버 명단 시스템"""

    # 닉네임에 이 단어가 포함되면 부계정으로 간주하여 명단에서 제외합니다.
    ALT_ACCOUNT_KEYWORD = "부계정"

    # 🗑️ [정리] 예전엔 여기 SHOP_ADMIN_ROLE_ID = 1147957568560439418 이 하드코딩돼 있었어요.
    # 다른 코그는 전부 settings.json의 roles.shop_admin을 보는데 여기만 코드에 박아둬서,
    # `/설정 관리자 상점`으로 역할을 바꿔도 /명단은 안 따라갔습니다. (그 역할을 지우면
    # 서버 관리자 말고는 아무도 /명단을 못 쓰게 되는 상태였어요) 이제 설정을 그대로 따릅니다.

    # 역할 ID 정의 매핑 테이블
    LEVEL_ROLES = {
        "0": 1207153785084846150,
        "1": 1055028921445396522,
        "2": 1055029115549401138,
        "3": 1100662528884875285,
        "4": 1477355735397699705
    }

    # 💡 [명칭 정리] 여기서는 "캠프"가 아니라 "소속"이에요. 세금/견학 같은 캠프 시스템(MariCamp)과는
    # 별개로, 명단/프로필에서 사람들이 어디 소속인지 보여주기 위한 목적이라 전율도 포함됩니다.
    AFFILIATION_ROLES = {
        "나래": 1457359755616977118,
        "여백": 1457359769999507588,
        "악동": 1457359696540336199,
        "전율": 1457363783826538852,
    }

    # 디스코드 ANSI 이스케이프 컬러 코드 정의
    ANSI_RESET = "\u001b[0m"
    
    # 요구사항 색상 매핑 (디스코드 ANSI 표준 지원 색상으로 보정)
    COLOR_MAP = {
        "0Lv": "",                     # 색상 없음 (일반 텍스트)
        "1Lv": "\u001b[1;34m",         # Light Blue
        "2Lv": "\u001b[0;34m",         # Dark Blue
        "3Lv": "\u001b[1;35m",         # Magenta/Purple
        "4Lv": "\u001b[1;31m",         # Pink 느낌의 Light Red
        "악동": "\u001b[0;31m",         # Dark Red
        "여백": "\u001b[1;37m",         # Bright White
        "나래": "\u001b[0;35m",         # Dark Purple
        "전율": "\u001b[0;34m",         # 다크네이비 (디스코드 ANSI Dark Blue 매칭)
    }

    def __init__(self, bot):
        self.bot = bot

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        """관리자 권한 또는 상점 관리자 역할을 보유하고 있는지 확인합니다."""
        return has_admin_or_role(interaction, "shop_admin")

    @app_commands.command(
        name="명단",
        description="레벨별 또는 소속별로 멤버 명단을 조건에 맞게 조회합니다 (부계정 제외)"
    )
    @app_commands.choices(
        레벨별=[
            app_commands.Choice(name="0레벨", value="0"),
            app_commands.Choice(name="1레벨", value="1"),
            app_commands.Choice(name="2레벨", value="2"),
            app_commands.Choice(name="3레벨", value="3"),
            app_commands.Choice(name="4레벨", value="4"),
            app_commands.Choice(name="총원 (모든 레벨)", value="총원")
        ],
        소속별=[
            app_commands.Choice(name="나래", value="나래"),
            app_commands.Choice(name="여백", value="여백"),
            app_commands.Choice(name="악동", value="악동"),
            app_commands.Choice(name="전율", value="전율"),
            app_commands.Choice(name="총원 (모든 소속)", value="총원")
        ],
        정렬=[
            app_commands.Choice(name="이름 가나다순", value="이름순"),
            app_commands.Choice(name="서버 들어온 순 (가입일순)", value="들어온순")
        ]
    )
    @app_commands.describe(
        레벨별="조회할 레벨을 선택하세요 (선택 사항)",
        소속별="조회할 소속을 선택하세요 (선택 사항)",
        정렬="명단의 정렬 기준을 선택하세요 (기본값: 이름 가나다순)"
    )
    @app_commands.guild_only()
    async def roster(
        self,
        interaction: discord.Interaction, 
        레벨별: Optional[str] = None, 
        소속별: Optional[str] = None, 
        정렬: Optional[str] = "이름순"
    ):
        # 🛡️ 최상단에서 권한 체크 수행 (비밀 메시지로 처리)
        if not self._is_admin(interaction):
            return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)

        if 레벨별 is None and 소속별 is None:
            return await interaction.response.send_message(
                "❌ `레벨별` 또는 `소속별` 중 최소 하나의 필터는 선택하셔야 명단 조회가 가능합니다.", 
                ephemeral=True
            )

        await interaction.response.defer()
        guild = interaction.guild
        filtered_members = []

        for m in guild.members:
            if m.bot or self.ALT_ACCOUNT_KEYWORD in m.display_name:
                continue

            level_pass = True
            if 레벨별 is not None:
                if 레벨별 == "총원":
                    level_pass = any(m.get_role(role_id) is not None for role_id in self.LEVEL_ROLES.values())
                else:
                    level_pass = m.get_role(self.LEVEL_ROLES.get(레벨별)) is not None

            camp_pass = True
            if 소속별 is not None:
                if 소속별 == "총원":
                    camp_pass = any(m.get_role(role_id) is not None for role_id in self.AFFILIATION_ROLES.values())
                else:
                    camp_pass = m.get_role(self.AFFILIATION_ROLES.get(소속별)) is not None

            if level_pass and camp_pass:
                filtered_members.append(m)

        if not filtered_members:
            return await interaction.followup.send("🥲 설정하신 필터 조건에 일치하는 멤버가 존재하지 않아요.")

        if 정렬 == "들어온순":
            filtered_members.sort(key=lambda m: m.joined_at if m.joined_at else dt.datetime.now(dt.timezone.utc))
        else:
            filtered_members.sort(key=lambda m: m.display_name.lower())

        lines = []
        for i, m in enumerate(filtered_members, start=1):
            m_level = "0"
            for lvl_k, lvl_v in self.LEVEL_ROLES.items():
                if m.get_role(lvl_v):
                    m_level = lvl_k
                    break
            
            m_camp = "없음"
            for camp_k, camp_v in self.AFFILIATION_ROLES.items():
                if m.get_role(camp_v):
                    m_camp = camp_k
                    break

            # 🎨 ANSI 컬러 조립 파트
            lvl_tag = f"{m_level}Lv"
            lvl_color = self.COLOR_MAP.get(f"{m_level}Lv", "")
            camp_color = self.COLOR_MAP.get(m_camp, "")

            # 컬러가 지정된 경우 ANSI 코드로 래핑, 0레벨이나 캠프 없음은 일반 텍스트 처리
            if lvl_color:
                lvl_str = f"[{lvl_color}{lvl_tag}{self.ANSI_RESET}]"
            else:
                lvl_str = f"[{lvl_tag}]"

            if camp_color:
                camp_str = f"[{camp_color}{m_camp}{self.ANSI_RESET}]"
            else:
                camp_str = f"[{m_camp}]"

            # 가로 줄 정렬 맞춤 (ANSI 제어 문자가 길이를 차지하므로 깔끔하게 결합)
            info_str = f"{lvl_str} {camp_str}"
            
            if 정렬 == "들어온순" and m.joined_at:
                join_date = m.joined_at.strftime("%Y-%m-%d")
                lines.append(f"{i:02d}. {info_str} | {m.display_name} ({join_date})")
            else:
                lines.append(f"{i:02d}. {info_str} | {m.display_name}")

        # 📄 [버그 수정] 예전엔 무조건 50줄씩 잘랐어요. 그런데 줄마다 ANSI 색 코드가 22자쯤
        # 더 붙기 때문에, 닉네임이 긴 사람이 몰린 구간에서는 50줄이 임베드 설명 한도(4096자)를
        # 넘겨서 그 페이지가 통째로 400 에러로 안 떴습니다. 인원이 늘수록 잘 터지는 구조라,
        # 다른 화면들과 똑같이 **글자 수 기준**으로 나누도록 공용 함수에 맡겼어요.
        # (```ansi 펜스와 여유분을 빼고 3800자로 잡습니다)
        chunks = chunk_lines(lines, limit=3800)

        level_status = f"{레벨별}레벨" if 레벨별 and 레벨별 != "총원" else ("모든 레벨 (총원)" if 레벨별 == "총원" else "필터 안 함")
        camp_status = f"{소속별}" if 소속별 and 소속별 != "총원" else ("모든 소속 (총원)" if 소속별 == "총원" else "필터 안 함")
        sort_status = "⌛ 서버 들어온 순" if 정렬 == "들어온순" else "🔤 이름 가나다순"

        total_count = len(filtered_members)
        shown = 0
        for idx, chunk in enumerate(chunks):
            # 페이지마다 줄 수가 달라졌으므로 번호도 실제 줄 수로 셉니다.
            # (chunk_lines가 줄마다 \n을 하나씩 붙여줘서 개수가 곧 인원 수예요)
            start_num = shown + 1
            shown += chunk.count("\n")
            end_num = shown

            # 🔥 텍스트 내부에 주입된 ANSI 전용 코드박스 서식 지정 (```ansi)
            formatted_description = f"```ansi\n{chunk.rstrip()}\n```"

            embed = discord.Embed(
                title=f"📋 [서버 조건부 인원 명단]" + (f" ({idx + 1}/{len(chunks)})" if len(chunks) > 1 else ""),
                description=formatted_description,
                color=0x2ecc71,
                timestamp=dt.datetime.now(KST)
            )
            embed.add_field(
                name="🔍 적용된 필터 조건", 
                value=f"• **레벨별:** `{level_status}`\n• **소속별:** `{camp_status}`\n• **정렬 방식:** `{sort_status}`", 
                inline=False
            )
            embed.set_footer(text=f"조건부 인원: 총 {total_count}명 (부계정 제외) | {start_num} ~ {end_num}번째 구역")

            await interaction.followup.send(embed=embed)
