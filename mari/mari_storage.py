"""JSON 원자적 저장 / 손상 감지 로드 / 데이터 파일 자동 생성."""

import json
import os
import shutil
import time
import datetime as dt
from collections import deque

from mari_config import KST, json_data_files

# 💾 [개선] 원자적(atomic) JSON 저장 유틸리티
# 기존에는 json.dump()로 파일에 바로 덮어썼는데, 저장 도중 봇이 죽거나
# 서버가 강제 종료되면 파일이 반쯤 쓰인 상태로 깨질 수 있었어요.
# 임시 파일에 먼저 완전히 쓴 뒤 os.replace()로 "한 번에" 교체하면
# 저장은 항상 성공하거나(완료) 전혀 반영되지 않거나(원본 그대로) 둘 중 하나만 일어납니다.
class DataSaveError(RuntimeError):
    """돈·아이디·주식처럼 절대 조용히 넘어가면 안 되는 데이터의 저장이 실패했을 때 던지는 예외.

    🚨 [안전장치] 예전엔 atomic_json_save가 실패해도 콘솔에 한 줄 찍고 False만 돌려줬는데,
    호출하는 28곳 중 대부분이 그 반환값을 확인하지 않았어요. 그래서 저장이 실패해도
    "✅ 송금 완료!" 같은 성공 메세지가 그대로 나가고, 유저는 잔액이 바뀐 줄 알게 됐습니다.
    (특히 OneDrive/백신처럼 파일을 붙잡는 프로그램이 있으면 os.replace가 거부당해요)
    이제 중요한 데이터는 이 예외를 던져서, 전역 에러 핸들러가 유저에게 실패를 알립니다."""
    pass


# 🧾 최근 저장 실패 기록. /테스트 데이터점검에서 확인할 수 있어요.
SAVE_FAILURES = deque(maxlen=20)


# 🔁 [신규] 저장 재시도
# 저장이 실패하는 실질적 원인은 거의 항상 **일시적인 파일 잠금**이에요. OneDrive 동기화나
# 백신 실시간 검사가 파일을 잠깐 붙잡으면 os.replace가 거부당합니다. (위 DataSaveError 설명 참고)
# 원인이 일시적이라는 게 핵심이에요 — 잠깐 기다렸다 다시 쓰면 대부분 그냥 성공합니다.
#
# ⏱️ 대기 시간을 아주 짧게 잡은 이유: 이 함수는 동기 함수라 sleep이 이벤트 루프를 통째로
# 멈춰요. 게다가 대부분 economy_lock을 쥔 채로 불리기 때문에, 여기서 오래 자면 서버의 모든
# 돈 관련 기능이 같이 멈춥니다. 전부 실패해도 총 대기가 0.1초를 넘지 않게 잡았어요.
# (파일 잠금은 보통 수십~수백ms라 이 정도로 대부분 넘어갑니다)
SAVE_RETRIES = 3
SAVE_RETRY_DELAY = 0.05     # 초


def atomic_json_save(path, data, indent=2):
    tmp_path = f"{path}.tmp"
    last_detail = "알 수 없는 오류"

    for attempt in range(1, SAVE_RETRIES + 1):
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=indent)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)  # 같은 파일시스템 내에서 원자적 교체
            if attempt > 1:
                print(f"✅ [저장 성공] {os.path.basename(path)} — {attempt}번째 시도에서 성공했어요. "
                      f"(다른 프로그램이 파일을 잠깐 붙잡고 있었던 것 같아요)")
            return True
        except Exception as e:
            last_detail = f"{type(e).__name__}: {e}"
            # 실패 시 임시 파일 잔여물 정리 (다음 시도가 깨끗한 상태에서 시작하도록)
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            if attempt < SAVE_RETRIES:
                print(f"⏳ [저장 재시도] {os.path.basename(path)} — {last_detail} "
                      f"({attempt}/{SAVE_RETRIES}회 실패, {SAVE_RETRY_DELAY}초 후 다시)")
                time.sleep(SAVE_RETRY_DELAY)

    print(f"❗❗❗ [저장 실패] {os.path.basename(path)} — {SAVE_RETRIES}번 모두 실패: {last_detail}")
    SAVE_FAILURES.append({
        "time": dt.datetime.now(KST).strftime("%m-%d %H:%M:%S"),
        "file": os.path.basename(path),
        "error": last_detail,
    })
    return False


def atomic_json_save_or_raise(path, data, indent=2):
    """저장 실패를 절대 못 본 척하면 안 되는 데이터 전용 저장 함수.
    실패하면 DataSaveError를 던져서 명령어가 성공한 척 끝나지 않게 막습니다."""
    if not atomic_json_save(path, data, indent=indent):
        raise DataSaveError(
            f"'{os.path.basename(path)}' 파일에 저장하지 못했어요. "
            f"변경 내용이 반영되지 않았으니 잠시 후 다시 시도해 주세요. "
            f"(계속 실패하면 다른 프로그램이 파일을 붙잡고 있는지 확인이 필요해요)"
        )
    return True

# 🚨 [안전장치] "파일이 원래 없어서 비어있는 것"과 "파일이 있는데 깨져서 못 읽는 것"을
# 예전엔 똑같이 취급해서 둘 다 빈 딕셔너리를 돌려줬어요. 문제는, 파일이 손상돼서 빈 데이터가
# 반환된 뒤 무언가 저장까지 되면 원본 데이터가 통째로 덮어써져서 날아갈 수 있다는 거예요.
# 이제는 파일이 손상된 경우 절대 조용히 빈 데이터로 넘어가지 않고, 손상된 파일을 그대로
# 백업해둔 뒤 예외를 던져서 호출부(명령어)가 에러로 멈추게 만듭니다. (전역 에러 핸들러가 처리)
def safe_json_load(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        backup_path = f"{path}.corrupt_{int(dt.datetime.now().timestamp())}"
        try:
            shutil.copy(path, backup_path)
            print(f"🚨🚨🚨 [심각] '{path}' 파일이 손상돼서 못 읽어요! 원본을 '{backup_path}'로 백업해뒀어요.")
        except Exception:
            print(f"🚨🚨🚨 [심각] '{path}' 파일이 손상돼서 못 읽어요! (백업도 실패함)")
        raise RuntimeError(f"'{os.path.basename(path)}' 파일이 손상되어 안전을 위해 작업을 중단했어요. 관리자에게 문의해주세요. (원본 오류: {e})")

# 🌟 [추가] JSON 파일이 없을 때 자동으로 빈 데이터 파일을 생성해 주는 로직

def init_json_files():
    # (각 로드 함수들이 이미 .get()/.setdefault()로 빈 {} 구조에도 안전하게 대응하도록
    # 짜여있어서, 파일마다 정확한 기본 구조를 여기 미리 안 적어놔도 문제없어요)
    for name, file_path in json_data_files().items():
        if not os.path.exists(file_path):
            if atomic_json_save(file_path, {}, indent=2):
                print(f"📦 빈 데이터 파일이 자동 생성됐어요: {os.path.basename(file_path)} ({name})")

# 🚀 데이터 로딩 유틸리티를 호출하기 전에 최우선 실행합니다.
init_json_files()
