# 다음에 할 일

*2026-08-13 기준. 커밋 `56eae07`까지 끝난 상태입니다.*

## 지금 어디까지 왔나

`maribot` 코드를 가져와 **모듈로 골라 담을 수 있는 형태**로 바꾸는 중입니다.

- ✅ 코드만 복사 (유저 데이터·깃 히스토리는 안 가져옴)
- ✅ 서버 고유 ID를 코드에서 걷어내 `mari/guild.json` 으로 분리
- ✅ 모듈 레지스트리(`mari/modules.py`) + 동적 로딩. 담을 기능은 `guild.json`의 `modules`가 결정
- ✅ 코그 간 직접 import 제거 (이제 14개 코그가 전부 독립)
- ✅ 서버 고유 기능 일부를 `mari/parked/` 창고로 (캠프·명단·프로필·견학권)
- ⬜ **남은 서버 고유 기능 제거** ← 여기부터
- ⬜ **이름 변수화 + 첫 기동 입력받기**

현재 상태: 모듈 14개, 슬래시 명령 34개. `python tools/check_modules.py` 로 확인 가능.

---

## 0. 먼저 고칠 것 — `help.py`가 없는 명령을 안내함 🔴

지난 커밋에서 캠프·명단·프로필을 창고로 보냈는데 **도움말은 안 고쳤습니다.**
지금 `/도움말`을 누르면 존재하지 않는 명령이 그대로 나옵니다.

`mari/cogs/help.py` 에서 지울 것:

| 줄 | 내용 |
|---|---|
| 76 | `` `/지갑 캠프:전체` `` → **`` `/지갑 전체:True` `` 로 고칠 것** (기능은 살아있음) |
| 86 | `` `/명단` `` |
| 93–99 | `/프로필설정` 7줄 전부 |
| 107–114+ | "캠프장 전용" 섹션 통째로 |
| 56, 58, 60 | 레벨 명단 관련 — 아래 4번(레벨 체계)과 같이 처리 |

88–92줄(유배 관련)은 아래 3번에서 같이 지웁니다.

---

## 1. 고확 (고성능 확성기)

특정 역할을 멘션해 서버 전체에 방송하는 기능. 원본 서버 전용입니다.

- `mari/cogs/games.py` — `name="고확"` 명령 (332줄 근처)
- `mari/cogs/setting.py` — `@채널.command(name="고확")` (304줄 근처)
- `mari/mari_config.py` — `GOHWAK_MENTION_ROLE_ID`
- `mari/guild.example.json` / `guild.json` — `roles.broadcast_mention`
- `mari/cogs/help.py` — 안내문

떼어낸 코드는 `mari/parked/games_broadcast.py.txt` 로.

⚠️ `games.py`에는 **에바시(선착순 이벤트)** 도 같이 있습니다. 그건 이름만 서버
고유이고 구조는 범용이라 남기고, 이름만 나중에 5번에서 바꾸는 게 좋습니다.

## 2. 유배 · 복귀 (지옥간수 / 추방관 / 유배자)

- `mari/cogs/setting.py` — `/역할부여`의 `유배지` 선택지와 그 처리 블록 전체,
  `@관리자.command(name="유배자")`, 추방관/지옥간수 권한 설정
- `mari/cogs/diagnostics.py` — 88줄 `role_labels`의 `hell_warden` / `exile_officer` / `exile_role`
- `mari/mari_config.py` — `EXILE_DATA_FILE`
- `mari/cogs/help.py` — 88–92줄
- `mari/modules.py` — `setting` 모듈 설명에 "역할부여·유배"라고 적혀 있음

떼어낸 코드는 `mari/parked/setting_exile.py.txt` 로.

## 3. 타운가이드 + 입주 가이드 프리셋

신규 입주 절차(여행자 → 입주심사 → 시민권)를 한 번에 처리하는 기능.

- `mari/cogs/setting.py` — `_GUIDE_CHOICES`, `_with_guide_choices`, `_apply_role_changes`의 프리셋 순회 블록, `TOWN_GUIDE_ROLE_ID` 권한 검사
- `mari/mari_config.py` — `ONBOARDING_PRESETS`, `_load_onboarding()`, `TOWN_GUIDE_ROLE_ID`
- `mari/guild.example.json` — `onboarding` 섹션, `roles.town_guide`
- `mari/cogs/help.py` — 관리자 모드 안내

⚠️ `/역할부여` 명령 **자체는 남기세요.** 역할을 직접 골라 주는 부분(`RoleGrantView`)은
어느 서버에서나 쓸 수 있는 범용 기능입니다. 가이드 프리셋만 걷어내면 됩니다.

떼어낸 코드는 `mari/parked/setting_onboarding.py.txt` 로.

## 4. AI 페르소나 + `core.py`의 레벨 체계

**페르소나** (`mari/cogs/gpt.py`) — 특정 유저를 "아빠/엄마"로 부르거나 일부러
냉대하는 분기. 476줄 근처 `persona_key` 와 그 아래 if/elif 갈래들.
`mari_config.PERSONA_USER_IDS`, `guild.example.json`의 `personas` 섹션도 같이.

같은 파일의 `check_my_profile` 도구(733줄 근처)도 지워야 합니다. 프로필 모듈을
창고로 보내서 지금은 "프로필 시스템을 불러올 수 없어요"만 돌려줘요. 위쪽
도구 목록 안내문(`system_instruction +=` 블록)에서도 빼주세요.

**레벨 체계** (`mari/cogs/core.py`) — 이게 제일 큽니다. 아이디 등록부가
"레벨별 아이디 명단 자동 게시"를 중심으로 짜여 있어서 레벨 언급이 30군데쯤 있어요.

- `LEVEL_LABELS` (85줄), `LEVEL_TITLES` (316줄)
- `_get_member_level_key()` — 지금은 항상 `None`을 돌려주게 해뒀습니다
- `_refresh_level_roster()` 계열 (294–450줄) — 레벨별로 묶어 채널에 게시
- `/아이디 공지` (450줄) — 레벨 명단 맨 아래 공지
- `/아이디 가져오기` (607줄) — 옛 레벨별 문서 파싱

지금은 전원 "레벨 미상"으로 묶여서 동작에 문제는 없지만, 아이디 등록부를 팔려면
정리가 필요합니다. **두 갈래 중 하나를 골라야 해요:**

- **(a) 레벨을 걷어내고 평평한 등록부로** — 닉네임 ↔ 게임 아이디만. 어느 서버든 바로 씀
- **(b) 등급 체계를 설정으로** — `guild.json`에 등급 목록을 두고 그대로 유지

(a)가 납품하기 쉽고, (b)는 원본 서버 기능을 그대로 남길 수 있습니다. 아직 안 정했습니다.

---

## 5. 이름 변수화 + 첫 기동 입력받기

**정한 방향:** 이름을 코드에 박지 말고 변수로 두고, 봇을 처음 초대해서 켰을 때
입력을 받는다.

바꿔야 할 이름:

| 지금 | 무엇 | 나오는 파일 수 |
|---|---|---|
| 에바 | 서버 재화 이름 | 16개 |
| 마리 | 봇 이름 | 17개 |
| 에바시 | 선착순 이벤트 이름 | `games.py`, `setting.py`, `diagnostics.py` |
| 에바스타운 | 서버 이름 | `shop.py` 등 |

**입력받는 방법은 아직 안 정했습니다.** 콘솔 입력은 위험해 보여요 — 이 봇은
systemd/도커로 돌리는 걸 전제로 짜여 있어서(`main.py` 주석 참고) 표준입력이
없을 수 있습니다. 후보:

- `/초기설정` 슬래시 명령을 관리자가 실행 → 값을 받아 저장 (가장 안전해 보임)
- 봇이 서버에 처음 들어왔을 때(`on_guild_join`) 서버 주인에게 DM으로 물어보기
- 위 둘을 같이 (자동으로 말 걸고, 놓치면 명령으로 다시)

저장 위치도 정해야 합니다. `guild.json`(배포 설정)과 `settings.json`(런타임 설정)
중 어디가 맞는지 — 첫 기동에 봇이 스스로 쓰는 값이면 `settings.json` 쪽이
자연스러워 보입니다.

**순서 주의:** 이름 작업은 위 1~4번을 **끝낸 뒤에** 하세요. 곧 지울 코드의 문구까지
고치는 헛일이 됩니다.

---

## 작업 요령

### 확인하는 법

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r mari/requirements.txt

.venv\Scripts\python tools/check_modules.py                # 지금 설정 그대로
.venv\Scripts\python tools/check_modules.py shop birthday  # 이 조합만 주문받았다면
.venv\Scripts\python tools/check_modules.py --none         # 설정 없는 새 서버처럼
```

토큰 없이 돌아갑니다. 디스코드에 연결하지 않고 코그 등록까지만 해봐요.
슬래시 명령 동기화 규격도 미리 검사하므로, 선택지가 0개라 동기화가 통째로
실패하는 사고를 여기서 잡을 수 있습니다.

### 창고에 넣는 방식

`mari/parked/README.md` 참고. 요약하면:

- 파일 통째로 옮길 수 있으면 `.py` 그대로 (그 자체로 온전한 모듈)
- 여러 파일에서 오려낸 조각 모음이면 `.py.txt`
  (실행되지 않으므로, 파이썬이 실수로 import하거나 문법 검사에 걸리지 않게)
- 블록마다 **원래 어느 파일 어디에 있었는지** 제목으로 남길 것

### 밟았던 함정 (다시 밟지 말 것)

- **`_FILE`로 끝나는 전역 이름을 새로 만들지 말 것.** `json_data_files()`가
  "`_FILE`로 끝나고 `.json`인 전역"을 전부 데이터 파일로 보고 자동 생성·자동
  백업합니다. 설정 파일이 여기 걸리면 빈 `{}`로 조용히 덮어써져요.
  그래서 `GUILD_CONFIG_PATH`는 `_PATH`입니다.
- **`migrate_legacy_data_files()`가 코드 옆 `.json`을 전부 `data/`로 옮깁니다.**
  설정 파일은 `_CONFIG_FILENAMES`로 제외해뒀어요. 새 설정 파일을 코드 옆에
  둔다면 거기 추가하세요. (한 번 당했습니다)
- **`app_commands.choices()`에 빈 리스트를 넘기지 말 것.** 디스코드가 명령어
  등록을 거부해서 **동기화 전체가 실패**합니다. 설정이 비면 선택지를 아예
  안 다는 식으로 처리해뒀어요 (`roster.py`의 `_with_dynamic_choices` 참고 —
  지금은 창고에 있지만 `setting.py`에 같은 패턴이 남아 있습니다).
- **일괄 수정 스크립트로 코드를 오려낼 때**, 잘라낸 내용을 창고 파일에 **먼저**
  쓰고 원본을 고치세요. 검증에 걸려 중간에 멈추면 오려낸 코드가 사라집니다.
  (한 번 당해서 깃에서 복구했습니다)

### 로컬에만 있는 것

`mari/guild.json`(실제 역할 ID가 든 로컬 설정)과 `mari/data/`는 `.gitignore`
대상이라 **집 PC에는 없습니다.** 원본 서버 ID가 필요하면
`mari/guild.example.json`을 복사해서 채우거나, 회사 PC의 파일을 옮기세요.
값 자체는 `maribot` 레포의 깃 히스토리에도 남아 있습니다.

없어도 `tools/check_modules.py`는 잘 돌아갑니다. (역할 ID가 비었다고 경고만 해요)
