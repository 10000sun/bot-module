"""MariBackup — 매일 새벽 3시 데이터 자동 백업과 오래된 백업 정리."""

import asyncio
import os
import shutil
import datetime as dt
from discord.ext import commands, tasks

from mari_config import BACKUP_DIR, KST, json_data_files
from mari_alerts import report_loop_error, send_alert

# ========== 💾 [신규] 자동 백업 시스템 ==========
class MariBackup(commands.Cog):
    """매일 한 번, 모든 데이터 파일을 backups/YYYY-MM-DD/ 폴더로 통째로 복사해두고,
    7일 지난 백업은 자동으로 정리합니다. (데이터 손상/실수 시 되돌릴 수 있는 최후의 보루예요)"""

    # 📁 [변경] 백업 폴더도 데이터 폴더(data/) 안으로 들어갔어요.
    # 🐛 [버그 수정] 예전엔 여기 BACKUP_DIR = os.path.join(DATA_DIR, "backups")로 똑같은
    # 경로를 다시 계산해뒀어요. 주석엔 "위쪽 경로 상수를 그대로 쓴다"고 적혀 있었는데 실제로는
    # 아니었습니다. mari_config.BACKUP_DIR을 다른 위치로 바꾸면 백업만 엉뚱한 폴더에 쌓이고,
    # 예전 폴더를 data/로 옮겨주는 마이그레이션(migrate_legacy_data_files)과도 어긋나요.
    # 이제 클래스 속성 없이 mari_config에서 가져온 상수를 그대로 씁니다.
    RETENTION_DAYS = 7

    def __init__(self, bot):
        self.bot = bot
        self.daily_backup_loop.start()

    def cog_unload(self):
        self.daily_backup_loop.cancel()

    @tasks.loop(time=dt.time(hour=3, minute=0, tzinfo=KST))
    async def daily_backup_loop(self):
        # 💡 [정리] before_loop에서 이미 wait_until_ready()를 하고 있어서 여기선 불필요했어요.
        try:
            # 📀 파일 복사는 동기 작업이라, 파일이 많아지면 그동안 봇 전체가 멈춰요.
            # 별도 스레드로 넘겨서 이벤트 루프를 막지 않게 합니다.
            copied, removed, failed = await asyncio.to_thread(self._run_backup)
        except Exception as e:
            # 🚨 백업은 데이터 사고의 마지막 보루예요. 실패를 콘솔에만 남기면
            # 정작 복구가 필요한 순간에야 "백업이 없다"는 걸 알게 됩니다.
            print(f"❗ 자동 백업 실패: {e}")
            await send_alert(
                "🔴 자동 백업이 실패했어요",
                f"오늘 백업을 만들지 못했어요.\n\n**{type(e).__name__}**: {e}",
            )
            return

        if failed:
            lines = "\n".join(f"• {name}: {err}" for name, err in failed[:10])
            more = f"\n... 외 {len(failed) - 10}개" if len(failed) > 10 else ""
            await send_alert(
                "⚠️ 백업이 일부만 완료됐어요",
                f"파일 {copied}개는 복사했지만 **{len(failed)}개는 실패**했어요.\n"
                f"백업 폴더 이름 끝에 `.partial`이 붙어 있습니다.\n\n{lines}{more}\n\n"
                f"다른 프로그램(백신·클라우드 동기화)이 파일을 붙잡고 있는지 확인해 주세요.",
                color=0xF39C12,
            )

    @daily_backup_loop.before_loop
    async def before_daily_backup_loop(self):
        await self.bot.wait_until_ready()

    @daily_backup_loop.error
    async def daily_backup_loop_error(self, error: BaseException):
        # 루프 본문은 이미 try로 감싸져 있지만, 알림 전송 쪽에서 예외가 나면 여기로 와요.
        # 백업 루프가 멈춘 걸 모른 채 지내는 게 제일 위험합니다.
        await report_loop_error(self.daily_backup_loop, "자동 백업", error)

    def _run_backup(self):
        """백업을 수행하고 (복사 수, 정리한 폴더 수, 실패 목록)을 돌려줍니다.

        실패 목록은 [(파일명, 오류문구), ...] 형태예요.
        """
        today = dt.datetime.now(KST).strftime("%Y-%m-%d")
        target_dir = os.path.join(BACKUP_DIR, today)
        os.makedirs(target_dir, exist_ok=True)

        # 🔎 JSON 데이터 파일만 골라서 복사해요.
        # ⚠️ [수정] 예전엔 *_FILE 상수를 전부 복사해서 .env(토큰·API 키)까지
        # 백업 폴더에 매일 사본이 쌓였어요. 비밀값을 한 곳에 모아둔 의미가 없어지므로 제외합니다.
        file_consts = json_data_files()
        copied, skipped = 0, 0
        failed = []
        for name, path in file_consts.items():
            if not os.path.exists(path):
                skipped += 1
                continue
            # 🛡️ [수정] 예전엔 파일 하나가 잠겨 있으면 여기서 예외가 터져 나머지 파일은
            # 아예 복사도 못 해봤어요. 이제 실패한 것만 기록하고 나머지는 계속 복사합니다.
            # (백신·OneDrive가 파일을 붙잡는 상황은 이 프로젝트에서 반복적으로 겪은 문제예요)
            try:
                shutil.copy2(path, os.path.join(target_dir, os.path.basename(path)))
                copied += 1
            except Exception as e:
                failed.append((os.path.basename(path), f"{type(e).__name__}: {e}"))
                print(f"❗ [자동 백업] {os.path.basename(path)} 복사 실패: {type(e).__name__}: {e}")

        # 🗑️ [정리] 여기서 프로필 사진 폴더(PROFILE_PHOTO_DIR)를 따로 챙겨 복사했어요.
        # 프로필 기능이 창고로 가면서 그 폴더를 만드는 코드가 없어져 늘 비어 있었습니다.
        # 프로필을 되살릴 땐 이 블록도 같이 되살리세요 (parked/profile_leftovers.py.txt [5]).
        # ⚠️ json_data_files()는 .json으로 끝나는 것만 고르므로, 이미지·첨부 같은 폴더를
        #    새로 만든다면 반드시 여기에 손으로 추가해야 백업에 들어갑니다.

        # 🏷️ 일부만 복사됐다면 폴더 이름에 표시를 남겨요.
        # 겉보기엔 멀쩡한 백업 폴더가 사실 반쪽이었다는 걸 복구하려는 순간에 알게 되면 늦습니다.
        if failed:
            partial_dir = f"{target_dir}.partial"
            try:
                if os.path.exists(partial_dir):
                    shutil.rmtree(partial_dir, ignore_errors=True)
                os.rename(target_dir, partial_dir)
                target_dir = partial_dir
            except Exception as e:
                print(f"⚠️ [자동 백업] 부분 백업 표시(.partial)를 못 남겼어요: {type(e).__name__}: {e}")

        # 🧹 7일 지난 백업 폴더는 자동으로 정리
        # (.partial 폴더도 날짜 부분으로 인식해서 같이 정리돼요)
        removed = 0
        if os.path.exists(BACKUP_DIR):
            cutoff = dt.datetime.now(KST) - dt.timedelta(days=self.RETENTION_DAYS)
            for folder_name in os.listdir(BACKUP_DIR):
                date_part = folder_name[:-len(".partial")] if folder_name.endswith(".partial") else folder_name
                try:
                    folder_date = dt.datetime.strptime(date_part, "%Y-%m-%d").replace(tzinfo=KST)
                except ValueError:
                    continue
                if folder_date < cutoff:
                    shutil.rmtree(os.path.join(BACKUP_DIR, folder_name), ignore_errors=True)
                    removed += 1

        status = f"파일 {copied}개" + (f", 실패 {len(failed)}개" if failed else "")
        print(f"💾 [자동 백업] {today} 백업 {'일부 완료' if failed else '완료'} ({status}, 오래된 백업 {removed}개 정리)")
        return copied, removed, failed
