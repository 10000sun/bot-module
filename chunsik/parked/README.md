# parked — 떼어냈지만 버리지 않은 기능들

여기 있는 코드는 **봇이 절대 불러오지 않습니다.** `modules.py`의 목록에 없고,
어디서도 import하지 않아요. 그냥 보관되어 있을 뿐입니다.

## 왜 지우지 않고 남겼나

전부 원본 서버(에바스타운)의 고유 제도에 맞춰 만들어진 기능이라, 다른 서버에
그대로 납품할 수가 없어요. 캠프 세금 구조나 견학 이동 같은 건 그 서버가 아니면
쓸 일이 없습니다.

그렇다고 지워버리면, 나중에 "우리도 조직별 공용 통장 같은 거 되나요?" 같은
주문이 들어왔을 때 처음부터 다시 짜야 합니다. 이미 한 번 만들면서 밟은 함정
(동시 지급 경합, 저장 실패 처리, 버튼 만료 등)까지 통째로 다시 밟게 되고요.

그래서 **꺼내 쓸 수 있는 형태로 세워둡니다.** 비슷한 주문이 들어오면 여기서
하나를 꺼내, 그 클라이언트의 개념에 맞게 이름과 규칙만 고쳐서 붙이면 됩니다.

## 꺼내 쓸 때

1. 필요한 파일을 `cogs/` 로 옮깁니다.
2. `modules.py` 의 `MODULE_SPECS` 에 항목을 추가합니다.
3. 서버 고유 이름·역할 ID를 그 클라이언트 것으로 바꿉니다.
   (여기 코드에는 원본 서버의 개념이 이름째로 남아 있어요 — 캠프, 견학, 전율 등)
4. 필요한 설정 항목을 `guild.example.json` 에 추가합니다.

⚠️ 여기 파일들은 **지금 그대로는 import되지 않습니다.** 떼어내면서 사라진 설정값
(`CAMP_ROLE_IDS`, `LEVEL_ROLES` 등)을 아직 참조하고 있어요. 꺼내 쓸 때 그 부분을
새 설정으로 바꿔주면 됩니다. 문법은 살아 있으니 읽고 고치는 데는 문제없어요.

## 무엇이 들어 있나

### 통째로 옮긴 것 (`.py` — 그 자체로 온전한 파일)

| 파일 | 원래 기능 | 원본 서버에서의 의미 |
|---|---|---|
| `camp.py` | 캠프 세금·통장·견학 | 서버를 3개 캠프(악동/나래/여백)로 나누고, 캠프별 세금과 공용 통장을 운영. '전율'이 전체 감독 |
| `chunsik_tax.py` | 세율 구간·누진세·보유세 계산 | 위 캠프 세금의 계산기 |
| `roster.py` | 레벨·소속 명단 | 0~4레벨 등급 사다리와 소속 조직 기준으로 멤버를 필터링 |
| `profile.py` | 프로필 카드 | 닉네임/아이디/레벨/소속/직책/업적 카드. 위 레벨·소속 개념 위에 세워져 있음 |

⚠️ `profile.py`를 꺼낼 때는 `profile_leftovers.py.txt`와 `gpt_persona.py.txt`의
`check_my_profile` 도구도 **반드시 같이** 꺼내세요. 코그만 되살리면 관리자 역할을
지정할 명령도, 사진을 백업할 코드도, AI가 프로필을 읽을 도구도 없습니다.

### 다른 파일에서 떼어낸 조각 (`.py.txt`)

여러 파일에 흩어져 있던 부분을 오려 모은 것이라 **그 자체로는 실행되지 않습니다.**
파이썬이 실수로 import하거나 문법 검사에 걸리지 않도록 일부러 `.py.txt`로 두었어요.
꺼내 쓸 때는 각 블록 제목에 적힌 원래 자리에 도로 붙이면 됩니다.

| 파일 | 원래 기능 | 어디서 떼어냈나 |
|---|---|---|
| `shop_visit_pass.py.txt` | 견학권 구매 처리 | `cogs/shop.py` — 사면 DM으로 "어느 캠프로 갈래?"를 묻고 상점주인에게 전달 |
| `camp_leftovers.py.txt` | 캠프·명단이 남긴 잔가지 | `cogs/diagnostics.py`(세금 미리보기·캠프 중복검사·권한표시), `cogs/economy.py`(캠프별 지갑 조회) |
| `profile_leftovers.py.txt` | 프로필이 남긴 잔가지 | `cogs/setting.py`(`/설정 관리자 프로필`·역할표), `cogs/diagnostics.py`(권한표), `cogs/backup.py`(사진 폴더 백업), `chunsik_config.py`(파일 경로) |
| `games_broadcast.py.txt` | 고확 (고성능 확성기) | `cogs/games.py`(`/고확`·되돌리기), `cogs/setting.py`(채널 지정), `cogs/diagnostics.py`, `chunsik_config.py`, `guild.example.json` |
| `setting_exile.py.txt` | 유배 · 복귀 | `cogs/setting.py`(`/역할부여`의 유배지 처리, 추방관·지옥간수·유배자 설정), `cogs/diagnostics.py`, `chunsik_config.py` |
| `setting_onboarding.py.txt` | 타운가이드 · 입주 프리셋 | `cogs/setting.py`(가이드 선택지·프리셋 적용), `chunsik_config.py`(`ONBOARDING_PRESETS`), `cogs/help.py`, `guild.example.json` |
| `gpt_persona.py.txt` | AI 페르소나 · 프로필 조회 도구 | `cogs/gpt.py`(아빠/엄마/disdain 분기, `check_my_profile`), `chunsik_config.py`, `guild.example.json` |
| `core_levels.py.txt` | 0~4레벨 등급 사다리 | `cogs/ids.py`(레벨별 명단 게시·레벨 판별), `chunsik_utils.py`(레벨별 문서 파서), `cogs/setting.py`(`/설정 레벨`) |

### 데이터 파일도 같이 걷어냈어요

창고로 간 기능이 쓰던 `chunsik_config.py`의 파일 경로 상수는 전부 지웠습니다.
(`PROFILE_FILE`, `PROFILE_PHOTO_DIR`, `VISIT_PASS_ASK_FILE`, `CAMP_TREASURY_FILE`,
`CAMP_VISIT_FILE`, `CAMP_TAX_FILE`, `EXILE_DATA_FILE`)

남겨두면 `json_data_files()`가 이름만 보고 주워가서, **아무도 안 쓰는 빈 JSON을
배포마다 새로 만들고 매일 백업**합니다. 꺼내 쓸 때 각 `.txt`에 적힌 상수를 도로
넣어주세요. 이름은 반드시 `_FILE`로 끝내고 `.json`이어야 자동 생성·백업에 잡힙니다.
