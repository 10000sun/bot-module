"""ChunsikBirthday — 생일 등록과 자정 축하 알림."""

import calendar
import datetime as dt
import discord
from discord import app_commands
from discord.ext import commands, tasks

from chunsik_config import BIRTHDAY_FILE, BIRTHDAY_MENTION_ROLE_ID, KST, SETTINGS_FILE
from chunsik_alerts import report_loop_error
from chunsik_storage import atomic_json_save_or_raise, safe_json_load
from chunsik_settings import feature_gate, is_feature_enabled, load_settings, save_settings, send_log_embed
from chunsik_names import bot_name

def birthday_matches_today(info: dict, today: dt.date) -> bool:
    """오늘 이 사람의 생일을 축하해야 하는지 봅니다.

    🐛 [버그 수정] 예전엔 `월 == 오늘의 월 and 일 == 오늘의 일`로만 봤어요. 그래서
    **2월 29일생은 평년에 축하가 아예 안 나갔습니다.** 등록도 되고(달력에 있는 날짜라
    `_invalid_date_message`도 통과해요) 화면에도 잘 뜨는데, 축하만 4년에 한 번 옵니다.
    본인도 관리자도 원인을 모르는 조용한 실패라, 등록 자체를 막았던 2월 30일 부류보다
    오히려 알아채기 어려웠어요.
    이제 평년에는 **2월 28일**에 축하합니다. (민법의 나이 계산이 같은 날을 씁니다)

    🔎 모듈 바깥에 둔 이유: 명령 안쪽 함수로 두면 검사 도구가 이 규칙을 베껴 써야 하고,
       그러면 진짜 코드가 바뀌어도 검사는 옛 규칙을 계속 통과시킵니다.
    """
    month, day = info.get("month"), info.get("day")
    if month == today.month and day == today.day:
        return True
    # 평년의 2월 28일 — 2월 29일생을 이 날 함께 축하해요. (윤년이면 29일에 제대로 나가요)
    if (month, day) == (2, 29) and (today.month, today.day) == (2, 28):
        return not calendar.isleap(today.year)
    return False


# 📅 마지막으로 생일 축하를 내보낸 날짜를 적어두는 자리. (settings.json)
#    이게 있어야 "자정 회차를 놓쳤는지"를 알 수 있고, 하루에 두 번 켜도 두 번 안 갑니다.
LAST_RUN_KEY = "birthday_last_run"


def _stored_date(value):
    """저장해둔 날짜 문자열 → date. 없거나 깨졌으면 None.

    🛡️ 여기서 예외가 새면 생일 축하가 통째로 멈춰요. 깨진 값은 "모름"으로 보고
       평소처럼 오늘 것만 내보냅니다.
    """
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


class ChunsikBirthday(commands.Cog):
    """봇 생일 알림 및 유저 데이터 관리 시스템 (설정 기능 분리 버전)"""
    def __init__(self, bot):
        self.bot = bot
        # ChunsikSetting 클래스가 사용하는 동일한 설정 파일 경로
        self.settings_file = SETTINGS_FILE # 📌 실제 운영 중인 json 파일명으로 맞춰주세요!
        self.birthday_file = BIRTHDAY_FILE # 유저들의 생일 데이터 저장용 파일
        
        # ⏰ 매일 자정(00:00 KST)마다 생일 확인하는 백그라운드 태스크 시작
        self.check_birthday_loop.start()

    def cog_unload(self):
        self.check_birthday_loop.cancel()

    # 📁 [ChunsikSetting 호환용 세팅 로드 헬퍼]
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
    생일 = app_commands.Group(name="생일", description=f"{bot_name()}봇 생일 관련 시스템이에요.")

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
        # 본문에 두면 회차마다 매번 확인하게 되고, 다른 루프들(출석 초기화·백업·이벤트)과
        # 방식이 달라서 읽는 사람이 헷갈립니다.
        await self.catch_up()

    # ---------------------------------------------------------
    # 🕛 놓친 회차 따라잡기
    # ---------------------------------------------------------
    async def catch_up(self):
        """오늘(그리고 필요하면 어제) 축하를 아직 안 했으면 지금 합니다.

        🐛 [버그] `tasks.loop(time=...)`은 **놓친 회차를 따라잡지 않아요.** 새벽에 PC를
           꺼두는 서버라면 자정 회차가 통째로 지나가고, 그날 생일인 사람은 축하를
           **영영** 못 받습니다. 오류도 안 나고 본인만 압니다.
           (출석 초기화·백업은 이미 같은 이유로 따라잡게 해뒀어요)

        📅 **어제까지만** 따라잡습니다. 사흘 전 생일을 이제 와서 축하하면 오히려 어색해요.
           - 자정을 놓치고 같은 날 켜면 → 그냥 평소 문구로 나갑니다. 아직 그 사람 생일이니까요.
           - 하루를 통째로 건너뛰었으면 → **"늦었지만"** 문구를 따로 써요.

        🔁 마지막으로 축하한 날짜를 settings.json에 적어둡니다. 하루에 두 번 켜도
           같은 사람에게 두 번 가지 않아요.
        """
        if not is_feature_enabled("birthday"):
            return  # 🚧 생일 기능이 정지된 상태면 자동 축하도 건너뜀

        today = dt.datetime.now(KST).date()
        try:
            settings = self.load_global_settings()
        except Exception as e:
            print(f"❗ [생일 알림] 설정을 읽지 못해 이번 회차를 건너뛰어요: {type(e).__name__}: {e}")
            return

        last = _stored_date(settings.get(LAST_RUN_KEY))
        if last == today:
            return      # 오늘 이미 했어요

        # 하루를 통째로 건너뛴 경우에만 어제 것도 같이 챙깁니다.
        # (last가 없으면 = 처음 켜는 서버. 어제까지 거슬러 올라가지 않아요 — 설치하자마자
        #  "어제 생일이셨죠?"가 나가면 이상하니까요)
        if last is not None and last < today - dt.timedelta(days=1):
            await self._announce_for(today - dt.timedelta(days=1), late=True)

        await self._announce_for(today)

        # 🔒 읽고 → 고치고 → 저장 사이에 await가 없어야 해요. 그 사이 다른 관리자가
        #    바꾼 설정을 낡은 내용으로 덮어쓰게 됩니다. 그래서 여기서 다시 읽습니다.
        try:
            fresh = self.load_global_settings()
            fresh[LAST_RUN_KEY] = today.isoformat()
            save_settings(fresh)
        except Exception as e:
            # 못 적어도 축하는 이미 나갔어요. 다음 기동 때 한 번 더 갈 수 있는데,
            # 그건 "안 나가는 것"보다 낫습니다.
            print(f"⚠️ [생일 알림] 마지막 실행 날짜를 못 적었어요: {type(e).__name__}: {e}")

    async def _announce_for(self, target_date: dt.date, late: bool = False):
        """target_date가 생일인 사람들을 축하합니다. (late=True면 '늦었지만' 문구)"""
        current_year = target_date.year

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
                if birthday_matches_today(info, target_date):
                    member = guild.get_member(int(user_id))
                    if not member: continue
                    
                    # 💌 N번째 생일 계산 로직
                    birth_year = info.get("year")
                    # 🧵 "스레드에서 축하해 주세요"는 **스레드를 실제로 만든 뒤에만** 맞는 말이에요.
                    #    그래서 본문과 그 안내를 나눠 둡니다. (아래 create_thread 참고)
                    thread_invite = "\n모두 아래 마련된 스레드에서 축하 인사를 건네보세요! 🎂✨"
                    # 🕛 하루를 건너뛴 뒤 따라잡는 회차예요. "오늘은 …생일이에요"라고 하면
                    #    날짜가 어긋나 보입니다. 늦었다는 걸 그대로 말하는 편이 나아요.
                    when = "어제는" if late else "오늘은"
                    if birth_year:
                        nth_birthday = current_year - birth_year
                        title_text = (f"🎂 늦었지만 HAPPY {nth_birthday}TH BIRTHDAY! 🎂" if late
                                      else f"🎉 HAPPY {nth_birthday}TH BIRTHDAY! 🎉")
                        base_desc = f"{when} **{member.mention}** 님의 **{nth_birthday}번째** 생일이었어요!" if late \
                            else f"{when} **{member.mention}** 님의 **{nth_birthday}번째** 생일이에요!"
                    else:
                        title_text = "🎂 늦었지만 HAPPY BIRTHDAY! 🎂" if late else "🎉 HAPPY BIRTHDAY! 🎉"
                        base_desc = (f"{when} **{member.mention}** 님의 생일이었어요!" if late
                                     else f"{when} **{member.mention}** 님의 생일이에요!")
                    desc_text = base_desc + thread_invite
                    
                    # 생일 알림 임베드 구축
                    embed = discord.Embed(
                        title=title_text,
                        description=desc_text,
                        color=discord.Color.from_rgb(255, 182, 193)
                    )
                    embed.set_thumbnail(url=member.display_avatar.url)
                    embed.add_field(name="어제의 주인공" if late else "오늘의 주인공",
                                    value=f"{member.mention} ({member.display_name})", inline=True)
                    
                    # 📅 오늘 날짜가 아니라 **본인이 등록한 생일**을 적어요. 2월 29일생을 평년에
                    #    2월 28일에 축하할 때, 오늘 날짜를 쓰면 남의 생일처럼 보입니다.
                    born_month, born_day = info["month"], info["day"]
                    date_str = f"`{birth_year}년 {born_month}월 {born_day}일`" if birth_year else f"`{born_month}월 {born_day}일`"
                    embed.add_field(name="날짜", value=date_str, inline=True)
                    # 평년에 2월 29일생을 하루 앞당겨 축하하는 회차예요. 안 적으면 날짜 칸과
                    # "오늘은 …생일이에요"가 어긋나 보여서, 보는 사람이 오타로 읽습니다.
                    if (born_month, born_day) == (2, 29) and (target_date.month, target_date.day) == (2, 28):
                        embed.add_field(name="참고", value="올해는 2월 29일이 없어서 오늘 함께 축하해요! 🗓️", inline=False)
                    embed.set_footer(text=f"{bot_name()} 생일 알림 시스템", icon_url=self.bot.user.display_avatar.url)
                    
                    try:
                        # 🏷️ [신규] 스레드에서만 축하하는 게 아니라, 본문 자체에도 지정된 역할을
                        # 태그해서 서버 전체가 알림을 받을 수 있게 했어요.
                        # (guild.json 의 roles.birthday_mention. 안 적으면 멘션 없이 그냥 나가요)
                        mention_role = guild.get_role(BIRTHDAY_MENTION_ROLE_ID)
                        tag_text = mention_role.mention if mention_role else ""

                        # 1. 축하 알림 메시지 전송 (역할 태그 + 생일자 멘션)
                        msg = await announce_ch.send(
                            content=(f"{tag_text} 🔔 {member.mention} 늦었지만 생일 축하합니다!".strip() if late
                                     else f"{tag_text} 🔔 {member.mention} 생일 축하합니다!".strip()),
                            embed=embed
                        )
                        
                        # 2. 전송한 메시지 하단에 [자동 스레드 개설]
                        thread_name = f"🎂 {member.display_name}님의 생일을 축하해 주세요!"
                        try:
                            await msg.create_thread(name=thread_name, auto_archive_duration=1440)
                        except Exception as thread_err:
                            # 🐛 [버그 수정] 스레드 개설은 '공개 스레드 만들기' 권한이 있어야 해요.
                            #    권한이 없으면 예전엔 알림 전체가 실패한 것처럼 콘솔에 찍히고,
                            #    정작 올라간 축하 메세지는 **"아래 마련된 스레드에서 축하해 주세요"**
                            #    라고 안내한 채 남았습니다. 스레드가 없는데요.
                            #    매년·매 생일마다 반복되고 관리자는 콘솔 한 줄로만 알 수 있어요.
                            #    안내를 지우고 무엇이 모자란지 그 자리에 적습니다.
                            print(f"⚠️ [생일 알림] 스레드를 못 만들었어요 ({guild.name}): "
                                  f"{type(thread_err).__name__}: {thread_err}")
                            embed.description = base_desc
                            embed.add_field(
                                name="🧵 스레드는 못 만들었어요",
                                value="봇에게 이 채널의 **공개 스레드 만들기** 권한을 주면 "
                                      "다음부터 축하 스레드가 같이 열려요.",
                                inline=False)
                            try:
                                await msg.edit(embed=embed)
                            except Exception:
                                pass
                        
                    except Exception as e:
                        print(f"생일 알림 발송 에러 ({guild.name}): {e}")

    @check_birthday_loop.before_loop
    async def before_check_birthday_loop(self):
        await self.bot.wait_until_ready()
        # 🕛 기동할 때 한 번 따라잡습니다. `time=` 루프는 다음 자정까지 안 도는데,
        #    아침에 켠 서버라면 **오늘 생일인 사람이 그때까지 아무 축하도 못 받아요.**
        #    (백업이 "오늘 것이 없으면 기동할 때 한 번 돌린다"와 같은 방식입니다)
        await self.catch_up()

    @check_birthday_loop.error
    async def check_birthday_loop_error(self, error: BaseException):
        await report_loop_error(self.check_birthday_loop, "생일 알림", error)

