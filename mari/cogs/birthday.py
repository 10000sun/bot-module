"""MariBirthday — 생일 등록과 자정 축하 알림."""

import datetime as dt
import discord
from discord import app_commands
from discord.ext import commands, tasks

from mari_config import BIRTHDAY_FILE, GOHWAK_MENTION_ROLE_ID, KST, SETTINGS_FILE
from mari_alerts import report_loop_error
from mari_storage import atomic_json_save_or_raise, safe_json_load
from mari_settings import feature_gate, is_feature_enabled, load_settings, send_log_embed

class MariBirthday(commands.Cog):
    """마리봇 생일 알림 및 유저 데이터 관리 시스템 (설정 기능 분리 버전)"""
    def __init__(self, bot):
        self.bot = bot
        # MariSetting 클래스가 사용하는 동일한 설정 파일 경로
        self.settings_file = SETTINGS_FILE # 📌 실제 운영 중인 json 파일명으로 맞춰주세요!
        self.birthday_file = BIRTHDAY_FILE # 유저들의 생일 데이터 저장용 파일
        
        # ⏰ 매일 자정(00:00 KST)마다 생일 확인하는 백그라운드 태스크 시작
        self.check_birthday_loop.start()

    def cog_unload(self):
        self.check_birthday_loop.cancel()

    # 📁 [MariSetting 호환용 세팅 로드 헬퍼]
    def load_global_settings(self):
        # 💡 [정리] 전역 load_settings()가 이미 손상 파일 방어 처리가 돼 있어서, 중복 구현하지 않고 위임해요.
        return load_settings()

    @staticmethod
    def _invalid_date_message(년: int, 월: int, 일: int):
        """날짜가 실제로 존재하는지 확인하고, 문제가 있으면 안내 문구를 돌려줍니다. (정상이면 None)

        🐛 [버그 수정] 예전엔 `1 <= 월 <= 12 and 1 <= 일 <= 31`로만 봤어요. 그래서 2월 30일,
        4월 31일처럼 **달력에 없는 날짜가 그대로 등록**됐습니다. 그러면 그 날짜는 영원히 오지
        않으니 자정 축하 알림이 한 번도 안 나가요. 등록은 성공했다고 떴는데 축하만 안 오는,
        본인도 관리자도 원인을 모르는 조용한 실패였습니다.
        이제 달력에 실제로 있는 날인지 직접 확인합니다.
        """
        current_year = dt.datetime.now(KST).year
        if not (1900 <= 년 <= current_year):
            return f"❌ 연도는 1900년부터 {current_year}년 사이여야 해요."
        try:
            dt.date(년, 월, 일)
        except ValueError:
            return f"❌ `{년}년 {월}월 {일}일`은 달력에 없는 날짜예요. 다시 확인해 주세요."
        return None

    # 📁 [유저 생일 데이터 데이터베이스 로드/저장]
    def load_birthdays(self):
        return safe_json_load(self.birthday_file, {})

    def save_birthdays(self, data):
        atomic_json_save_or_raise(self.birthday_file, data, indent=4)

    # 📢 [공통 로그 전송 함수]
    async def send_birthday_log(self, guild: discord.Guild, log_type: str, user: discord.User, detail: str):
        await send_log_embed(
            self.bot, "birthday_log", f"**{log_type}**",
            fields=[("실행자", user.mention, True), ("내용", detail, False)],
            guild=guild,
        )

    # ---------------------------------------------------------
    # 📌 명령어 그룹 생성 (/생일)
    # ---------------------------------------------------------
    생일 = app_commands.Group(name="생일", description="마리봇 생일 관련 시스템이에요.")

    @생일.command(name="등록", description="본인의 생일을 등록합니다. (예: 2000년 7월 24일 -> 년: 2000, 월: 7, 일: 24)")
    @app_commands.guild_only()
    async def register(self, interaction: discord.Interaction, 년: int, 월: int, 일: int):
        if await feature_gate(interaction, "birthday", "생일"):
            return
        invalid = self._invalid_date_message(년, 월, 일)
        if invalid:
            return await interaction.response.send_message(invalid, ephemeral=True)
            
        birthdays = self.load_birthdays()
        user_id = str(interaction.user.id)
        
        if user_id in birthdays:
            return await interaction.response.send_message("⚠️ 이미 생일이 등록되어 있어요.\n변경을 원하시면 `/생일 변경`을 사용해 주세요.", ephemeral=True)
            
        birthdays[user_id] = {"year": 년, "month": 월, "day": 일}
        self.save_birthdays(birthdays)
        
        await interaction.response.send_message(f"✅ 생일이 `{년}년 {월}월 {일}일`로 성공적으로 등록됐어요!", ephemeral=True)
        await self.send_birthday_log(interaction.guild, "생일 등록 로그", interaction.user, f"`{년}년 {월}월 {일}일` 신규 등록")

    @생일.command(name="변경", description="등록된 본인의 생일을 변경합니다.")
    @app_commands.guild_only()
    async def change(self, interaction: discord.Interaction, 년: int, 월: int, 일: int):
        if await feature_gate(interaction, "birthday", "생일"):
            return
        invalid = self._invalid_date_message(년, 월, 일)
        if invalid:
            return await interaction.response.send_message(invalid, ephemeral=True)

        birthdays = self.load_birthdays()
        user_id = str(interaction.user.id)
        
        if user_id not in birthdays:
            return await interaction.response.send_message("❌ 등록된 생일 데이터가 없어요. 먼저 `/생일 등록`을 해보세요.", ephemeral=True)
            
        old_data = birthdays[user_id]
        old_birthday = f"{old_data.get('year', '연도미상')}년 {old_data['month']}월 {old_data['day']}일" if "year" in old_data else f"{old_data['month']}월 {old_data['day']}일"
        
        birthdays[user_id] = {"year": 년, "month": 월, "day": 일}
        self.save_birthdays(birthdays)
        
        await interaction.response.send_message(f"🔄 생일이 `{년}년 {월}월 {일}일`로 변경됐어요.", ephemeral=True)
        await self.send_birthday_log(interaction.guild, "생일 변경 로그", interaction.user, f"기존 `{old_birthday}` ➡️ 변경 `{년}년 {월}월 {일}일`")

    @생일.command(name="삭제", description="등록된 본인의 생일 정보를 삭제합니다.")
    @app_commands.guild_only()
    async def delete(self, interaction: discord.Interaction):
        if await feature_gate(interaction, "birthday", "생일"):
            return
        birthdays = self.load_birthdays()
        user_id = str(interaction.user.id)
        
        if user_id not in birthdays:
            return await interaction.response.send_message("❌ 삭제할 생일 정보가 존재하지 않아요.", ephemeral=True)
            
        old_data = birthdays[user_id]
        old_birthday = f"{old_data.get('year', '연도미상')}년 {old_data['month']}월 {old_data['day']}일" if "year" in old_data else f"{old_data['month']}월 {old_data['day']}일"
        
        del birthdays[user_id]
        self.save_birthdays(birthdays)
        
        await interaction.response.send_message("🗑️ 본인의 생일 정보가 정상적으로 삭제됐어요.", ephemeral=True)
        await self.send_birthday_log(interaction.guild, "생일 삭제 로그", interaction.user, f"기존에 등록되어 있던 `{old_birthday}` 정보 삭제")

    @생일.command(name="확인", description="특정 멤버의 생일을 확인합니다.")
    async def check_member(self, interaction: discord.Interaction, 멤버: discord.Member):
        birthdays = self.load_birthdays()
        user_id = str(멤버.id)
        
        if user_id in birthdays:
            info = birthdays[user_id]
            year_str = f"{info['year']}년 " if "year" in info else ""
            await interaction.response.send_message(f"🔍 {멤버.mention}님의 생일은 **{year_str}{info['month']}월 {info['day']}일** 입니다.")
        else:
            await interaction.response.send_message(f"❌ {멤버.display_name}님은 아직 생일을 등록하지 않았어요.")

    # ---------------------------------------------------------
    # ⏰ [자동화 태스크] 매일 자정 생일 체크 및 자동 스레드 생성 로직
    # ---------------------------------------------------------
    @tasks.loop(time=dt.time(hour=0, minute=0, tzinfo=KST))
    async def check_birthday_loop(self):
        # 💡 [정리] wait_until_ready()는 루프 본문이 아니라 아래 @before_loop로 옮겼어요.
        # 본문에 두면 회차마다 매번 확인하게 되고, 다른 루프들(출석 초기화·백업·에바시)과
        # 방식이 달라서 읽는 사람이 헷갈립니다.
        if not is_feature_enabled("birthday"):
            return  # 🚧 생일 기능이 정지된 상태면 자동 축하도 건너뜀

        now = dt.datetime.now(KST)
        current_year = now.year
        current_month = now.month
        current_day = now.day

        # 🛡️ 설정/생일 파일 읽기는 손상 시 예외를 던져요. 여기서 새어나가면 루프가 영구히
        # 멈춰서 다음 해까지 생일 축하가 안 나가므로, 이번 회차만 포기하고 루프는 살려둡니다.
        try:
            settings = self.load_global_settings()
            birthdays = self.load_birthdays()
        except Exception as e:
            print(f"❗ [생일 알림] 데이터를 읽지 못해 오늘 회차를 건너뛰어요: {type(e).__name__}: {e}")
            return

        announce_ch_id = settings.get("channels", {}).get("birthday_announce")
        if not announce_ch_id: return

        for guild in self.bot.guilds:
            announce_ch = guild.get_channel(announce_ch_id)
            if not announce_ch: continue

            for user_id, info in birthdays.items():
                # 🛡️ 손으로 고치다 깨진 항목 하나 때문에 나머지 사람들 축하까지 막히면 안 돼요.
                if not isinstance(info, dict) or "month" not in info or "day" not in info:
                    print(f"⚠️ [생일 알림] 형식이 이상한 항목을 건너뛰었어요: {user_id} -> {info!r}")
                    continue
                if info["month"] == current_month and info["day"] == current_day:
                    member = guild.get_member(int(user_id))
                    if not member: continue
                    
                    # 💌 N번째 생일 계산 로직
                    birth_year = info.get("year")
                    if birth_year:
                        nth_birthday = current_year - birth_year
                        title_text = f"🎉 HAPPY {nth_birthday}TH BIRTHDAY! 🎉"
                        desc_text = f"오늘은 **{member.mention}** 님의 **{nth_birthday}번째** 생일이에요!\n모두 아래 마련된 스레드에서 축하 인사를 건네보세요! 🎂✨"
                    else:
                        title_text = "🎉 HAPPY BIRTHDAY! 🎉"
                        desc_text = f"오늘은 **{member.mention}** 님의 생일이에요!\n모두 아래 마련된 스레드에서 축하 인사를 건네보세요! 🎂✨"
                    
                    # 생일 알림 임베드 구축
                    embed = discord.Embed(
                        title=title_text,
                        description=desc_text,
                        color=discord.Color.from_rgb(255, 182, 193)
                    )
                    embed.set_thumbnail(url=member.display_avatar.url)
                    embed.add_field(name="오늘의 주인공", value=f"{member.mention} ({member.display_name})", inline=True)
                    
                    date_str = f"`{birth_year}년 {current_month}월 {current_day}일`" if birth_year else f"`{current_month}월 {current_day}일`"
                    embed.add_field(name="날짜", value=date_str, inline=True)
                    embed.set_footer(text="마리 생일 알림 시스템", icon_url=self.bot.user.display_avatar.url)
                    
                    try:
                        # 🏷️ [신규] 스레드에서만 축하하는 게 아니라, 본문 자체에도 시민권 역할을
                        # 태그해서 서버 전체가 알림을 받을 수 있게 했어요.
                        # 🏛️ 시민권 역할. 예전엔 여기 역할 ID가 그대로 박혀 있었는데,
                        # /고확이 멘션하는 역할과 **완전히 같은 값**이라 이미 상수로 있었어요.
                        # 두 곳에 따로 적어두면 역할이 바뀔 때 한쪽만 고치게 됩니다.
                        citizen_role = guild.get_role(GOHWAK_MENTION_ROLE_ID)
                        tag_text = citizen_role.mention if citizen_role else ""

                        # 1. 축하 알림 메시지 전송 (시민권 태그 + 생일자 멘션)
                        msg = await announce_ch.send(
                            content=f"{tag_text} 🔔 {member.mention} 생일 축하합니다!".strip(),
                            embed=embed
                        )
                        
                        # 2. 전송한 메시지 하단에 [자동 스레드 개설]
                        thread_name = f"🎂 {member.display_name}님의 생일을 축하해 주세요!"
                        await msg.create_thread(name=thread_name, auto_archive_duration=1440)
                        
                    except Exception as e:
                        print(f"생일 알림 발송 에러 ({guild.name}): {e}")

    @check_birthday_loop.before_loop
    async def before_check_birthday_loop(self):
        await self.bot.wait_until_ready()

    @check_birthday_loop.error
    async def check_birthday_loop_error(self, error: BaseException):
        await report_loop_error(self.check_birthday_loop, "생일 알림", error)

