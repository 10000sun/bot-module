# bot-module — 주문제작용 모듈형 디스코드 봇 프레임워크

[![check](https://github.com/10000sun/bot-module/actions/workflows/check.yml/badge.svg)](https://github.com/10000sun/bot-module/actions/workflows/check.yml)

프리랜서로 디스코드 봇을 **주문받아 납품**하면서 만든 모듈형 프레임워크입니다.
서버마다 필요한 기능이 달라서, 기능을 코그(cog) 단위 모듈로 나누고 `guild.json` 한 파일로
**골라 담아** 납품합니다. 안 담은 기능은 슬래시 명령·설정·도움말·데이터 파일까지 통째로 따라오지 않습니다.

- Python 3.10+ / discord.py 2.x (슬래시 명령·버튼·드롭다운) / Gemini API(AI 대화, 선택)
- 데이터는 JSON 파일에 저장 — 임시 파일에 쓴 뒤 교체하는 방식이라 저장 도중 죽어도 원본이 반쯤 쓰인 채 남지 않습니다
- 디스코드에 접속하지 않고 돌리는 검증 스크립트(`tools/check_*.py`)와 GitHub Actions CI 포함
- 이 저장소에는 **실제 서버 ID·토큰·유저 데이터가 없습니다.** `.env`, `guild.json`, `data/`는 `.gitignore` 대상이고
  예시 파일(`.env.example`, `guild.example.json`)만 올라가 있습니다

> 스크린샷은 아직 없습니다.

## 기능 모듈

`guild.json`의 `modules`에 아래 **키**를 적으면 켜집니다. 목록은 `chunsik/modules.py`의 `MODULE_SPECS`가
정의하며, 아래 명령어는 `chunsik/cogs/` 코드에서 확인한 것입니다. (재화·봇 이름은 `/초기설정`으로 바뀌는 값이라 `재화`로 적었습니다)

| 키 | 기능 | 주요 슬래시 명령 |
|---|---|---|
| `economy` | 지갑·출석·송금·지급/회수·거래내역·서버 통계 | `/지갑` `/출석` `/송금` `/지급` `/회수` `/지갑내역 조회` `/통계` |
| `shop` | 채널별 상점 매대, 구매/되팔기, 인벤토리, 월별 정산 (지갑 필요) | `/상점 생성` `/상점 항목추가` `/상점 사용` `/상점 정산` |
| `stock` | 모의 주식: 매수·매도·포트폴리오·그래프·종가게시·개폐장 (지갑 필요) | `/주식 매수` `/주식 매도` `/주식 포폴` `/주식 그래프` `/주식 종가게시` |
| `id` | 게임 아이디 등록부: 등록·수정·조회, 메시지에서 자동등록, 명단 자동 게시 | `/아이디 등록` `/아이디 조회` `/아이디 전체조회` `/아이디 가져오기` |
| `birthday` | 생일 등록, 자정에 축하 알림·스레드 | `/생일 등록` `/생일 변경` `/생일 확인` `/생일 삭제` |
| `gpt` | Gemini 기반 AI 대화, 유저별 장기 기억, 채팅 통계 (`GEMINI_API_KEY` 필요) | `/기억 목록` `/기억 삭제` `/기억 초기화` (대화는 봇 멘션 또는 호출 역할 멘션) |
| `games` | 하이로우, 선착순 이벤트 (지갑 필요) | `/하이로우` `/이벤트설정` |
| `levels` | 채팅 활동으로 쌓는 레벨·순위, 레벨업 보상 역할(지갑이 있으면 재화도) | `/레벨 확인` `/레벨 순위` `/레벨 보상설정` |
| `wiki` | 멤버 위키(소개·취미·MBTI 등) | `/위키 등록` `/위키 조회` `/위키 목록` |
| `welcome` | 입장 자동 역할·인사말·규칙 패널 | `/입장 자동역할` `/입장 인사말` `/입장 규칙패널` |
| `selfrole` | 버튼식 셀프 역할 패널 | `/셀프역할 만들기` `/셀프역할 역할추가` |
| `party` · `scrim` | 파티 모집(정원·대기열) · 내전(팀 분할·전적) | `/파티 모집` · `/내전 열기` `/내전 전적` |
| `snooze` | 메시지 우클릭으로 "나중에 답장" 예약, 정한 시각에 DM으로 재알림 | `/스누즈 목록` `/스누즈 취소` |
| `chronicle` | 서버 연대기(날짜순 사건 기록) | `/연대기 기록` `/연대기 보기` |

코어(끌 수 없음): `setting`(`/설정` `/기능제어`) · `help`(`/도움말`) · `wizard`(`/설치 시작`) ·
`diagnostics`(`/테스트`) · `backup`(매일 03:00 자동 백업).

## 동작 방식

```
guild.json ─┐
            ├─ chunsik_config.py ── modules.resolve_modules() ─ 켤 모듈 확정(의존 모듈 자동 추가 + 경고)
.env ───────┘                                   │
main.py → chunsik_client.py: setup_hook → load_modules(): 모듈마다 importlib로 cog를 import → add_cog
                                                │ (한 모듈이 실패해도 나머지는 계속 올라가고 로그에 남김)
                                                └→ 슬래시 명령 동기화
```

- **cog 로딩/토글**: `modules.py`의 `ModuleSpec`이 모듈의 신원(import 경로·cog 클래스·의존 모듈·소유한 채널/역할/기능 스위치/데이터 파일)을
  한 곳에 모아 둡니다. `guild.json`의 `modules`가 켤 목록을 정합니다. 항목을 지우거나 빈 목록이면 전부 켜지므로(빈 목록은 경고를 남깁니다) 골라 담을 땐 꼭 적으세요. 도움말·설정 명령·데이터 파일 생성도 이 "소유 표"를 보고 걸러냅니다.
- **데이터 저장**: `chunsik/data/*.json` (경로는 `chunsik_config.py`). 저장은 `chunsik_storage.atomic_json_save`
  (임시 파일 → fsync → 교체, 잠금 충돌 시 짧게 재시도), 읽기는 `safe_json_load`(파일이 깨졌으면 빈 값으로 덮어쓰지 않고 멈춤).
- **런타임 설정**: 채널·관리자 역할 지정은 `/설정`·`/설치`로 하고 `data/chunsik_settings.json`에 저장됩니다.
  서버 잠금·역할 사다리 같은 배포 단위 값은 `guild.json`에 둡니다.

### 폴더 구조

```
chunsik/
  main.py               진입점
  chunsik_client.py        봇 클라이언트 (모듈 로딩, 명령 동기화)
  chunsik_config.py        경로·.env·guild.json 읽기
  chunsik_settings.py      채널/역할 지정 저장소 (/설정)
  chunsik_storage.py       JSON 원자적 저장·안전한 읽기
  chunsik_names.py         재화·봇·이벤트·서버 이름 (/초기설정으로 입력)
  chunsik_utils.py · chunsik_alerts.py · chunsik_state.py · chunsik_console.py   공용 유틸·다운 알림·상태·콘솔
  modules.py            모듈 레지스트리 (MODULE_SPECS, 판매 라인 TIER_SPECS)
  cogs/                 기능별 코그
  parked/               떼어내 보관 중인 코드 (봇이 import하지 않음, 자세한 건 parked/README.md)
  .env.example · guild.example.json · requirements.txt
  data/                 실행 중 생기는 데이터 (깃 제외)
tools/
  check_*.py            디스코드 없이 도는 검증 스크립트 (아래 참고)
  package.py            납품용 폴더 만들기
.github/workflows/check.yml   CI
NEXT.md                 개발 인계 문서 (설계 결정·이력)
설치안내문.md / .html     클라이언트용 설치 안내
```

## 검증 도구와 CI

`tools/check_*.py`는 토큰 없이, 디스코드에 접속하지 않고 돌아갑니다. 하나라도 틀리면 종료 코드 1입니다.

| 스크립트 | 보는 것 |
|---|---|
| `check_modules.py` | 모듈 조합별로 코그가 로딩되는지, 슬래시 명령이 디스코드 규격(이름·설명 길이 등)에 맞는지, 소유 표가 코드와 일치하는지 |
| `check_help.py` | `/도움말`이 없는 명령을 안내하지 않는지, 안 담은 기능 이름이 문구에 남지 않는지 (11개 조합) |
| `check_guild.py` | `guild.json` 파싱, 깨진 설정에서 기동을 막는지 |
| `check_money.py` | 저장 실패·파일 손상 시 잔액이 사라지지 않는지 (임시 폴더에서 실행) |
| `check_embeds.py` | 가장 나쁜 입력에서도 임베드·메시지가 디스코드 글자 한도 안인지 |
| `check_tiers.py` | 판매 라인(에센셜/스탠다드/프리미엄) 정의와 `guild.example.json` 프리셋이 일치하는지 |

GitHub Actions(`.github/workflows/check.yml`)가 push/PR마다 의존성을 설치하고
`python -m compileall chunsik tools` 와 위 여섯 스크립트를 순서대로 실행합니다. 로컬에서도 같은 명령입니다.

```bash
pip install -r chunsik/requirements.txt
python -m compileall -q chunsik tools
for f in tools/check_*.py; do python "$f" || exit 1; done
```

> `check_money.py`가 보는 건 저장 계층(바닥)까지입니다. 동시성과 명령 흐름 전체는 자동으로 검증하지 않습니다.

## 새 기능(cog) 추가하기

1. `chunsik/cogs/새기능.py`에 `commands.Cog` 클래스를 만듭니다. (생성자는 `bot` 하나를 받습니다 — 기존 코그를 참고하세요)
2. `chunsik/modules.py`의 `MODULE_SPECS`에 `ModuleSpec("키", "이름", "cogs.새기능", "클래스이름", ...)`를 추가합니다.
   필요한 모듈은 `requires`, 쓰는 채널·역할·킬스위치·데이터 파일 이름은 `channels`/`roles`/`features`/`data_files`에 적습니다.
3. 데이터 파일을 쓴다면 `chunsik_config.py`에 경로를 선언하고 `chunsik_storage`의 저장/읽기 함수를 씁니다.
4. `cogs/help.py`에 도움말 항목을 추가하고, `guild.example.json`의 `_modules설명`에 설명을 적습니다.
5. `python tools/check_modules.py 새기능키` 와 `python tools/check_help.py`, `python tools/check_tiers.py`로 확인합니다.
   (소유 표 누락, 명령 규격 위반, 도움말 불일치를 여기서 잡습니다)
6. 판매 라인에 넣을 거면 `TIER_SPECS`와 `guild.example.json`의 `_티어프리셋`을 같이 고칩니다.

## 빠른 실행 (개발용)

```bash
python -m venv .venv && source .venv/bin/activate     # 윈도우: .venv\Scripts\activate
pip install -r chunsik/requirements.txt
cp chunsik/.env.example chunsik/.env                  # DISCORD_TOKEN 입력
cp chunsik/guild.example.json chunsik/guild.json      # modules 목록 편집
cd chunsik && python main.py
```

봇 생성·Privileged Intent·초대·설정 절차는 아래 "납품 순서"에 자세히 있습니다.

## 라이선스

[LICENSE](LICENSE) — 소스 공개 상태이지만 사용 범위가 제한된 라이선스입니다. 사용 전에 읽어 주세요.

---

## 📦 납품 순서

한 서버에 세팅하는 데 20~30분쯤 걸립니다. 순서대로 따라가세요.

### 1. 디스코드 봇 만들기

[Discord Developer Portal](https://discord.com/developers/applications) → **New Application**
→ 왼쪽 **Bot** 탭 → **Reset Token** 으로 토큰을 받아둡니다. (한 번만 보여줘요)

### 2. 🚨 Privileged Intent 두 개를 켜세요

같은 **Bot** 탭 아래 *Privileged Gateway Intents* 에서 **반드시** 켜야 합니다.

- ✅ **SERVER MEMBERS INTENT**
- ✅ **MESSAGE CONTENT INTENT**

> **안 켜면 봇이 기동조차 못 합니다.** `PrivilegedIntentsRequired` 예외로 죽는데,
> 에러만 보면 원인을 짐작하기 어려워요. 납품할 때 제일 자주 걸리는 자리입니다.

왜 필요한가 — 멤버 인텐트는 아이디 명단·생일·지갑청소가 서버 인원을 훑어야 해서,
메시지 콘텐츠 인텐트는 아이디 자동등록·AI 대화·이벤트 키워드가 **본문을 읽어야** 해서예요.
(둘 다 `chunsik/chunsik_config.py` 맨 위에서 선언합니다)

### 3. 서버에 초대하기

아래 URL의 `<봇ID>`를 Application ID로 바꿔서 여세요.

```
https://discord.com/api/oauth2/authorize?client_id=<봇ID>&permissions=311653690448&scope=bot%20applications.commands
```

`permissions=311653690448`에 들어 있는 것:

| 권한 | 왜 |
|---|---|
| 채널 보기 / 메시지 보내기 / 링크 첨부 | 거의 모든 기능 (임베드로 응답해요) |
| 파일 첨부 | 주가 그래프 이미지 |
| 메시지 기록 보기 | 아이디 명단을 다시 찾아 수정 |
| 메시지 관리 | 명단 재게시, 공지 고정 |
| 반응 추가 | 이벤트 참여 반응 |
| **역할 관리** | `/역할부여`, 셀프 역할·입장 자동 역할·레벨 보상, 상점의 역할 아이템 |
| **채널 관리** | `/설치`가 채널·카테고리를 만들 때만 씁니다 |
| 공개 스레드 만들기 / 스레드에 쓰기 | 생일 축하 스레드 |

> ⚠️ **봇 역할을 서버 역할 목록에서 위로 올려주세요.** 디스코드는 자기보다
> **아래에 있는 역할만** 부여할 수 있습니다. 안 올리면 `/역할부여`와 상점의 역할
> 아이템이 조용히 실패해요. (봇은 안 죽습니다)

### 4. 코드와 파이썬 준비

파이썬 **3.10 이상** (3.12에서 동작 확인).

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r chunsik/requirements.txt
```

### 5. `.env` — 토큰과 비밀값

`chunsik/.env.example` 을 복사해 `chunsik/.env` 로 만들고 채웁니다.

| 키 | 필수 | 비우면 |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | 봇이 안 켜져요 |
| `GEMINI_API_KEY` | | AI 대화만 꺼지고 나머지는 정상 |
| `ALERT_WEBHOOK_URL` | | 다운 알림만 꺼져요 (**넣는 걸 권합니다**) |

`ALERT_WEBHOOK_URL`은 관리자만 보는 채널에 웹훅을 만들어 넣으세요.
봇이 죽거나 저장이 실패했을 때 알려주는 유일한 창구예요.

### 6. `guild.json` — 무엇을 담을지

`chunsik/guild.example.json` 을 복사해 `chunsik/guild.json` 으로 만듭니다.

```jsonc
{
  "modules": ["id", "economy", "shop", "birthday"],   // 주문받은 기능만
  "roles": {
    "birthday_mention": 0,   // 생일 알림에 함께 멘션할 역할 (0이면 멘션 없음)
    "bot_mention": 0         // 이 역할을 멘션하면 봇을 부른 것으로 취급
  }
}
```

#### 🔒 서버 잠금 (납품할 때 반드시)

```jsonc
"allowed_guilds": [123456789012345678]   // 이 서버에서만 동작
```

여기 적힌 서버에서만 명령이 먹고, **다른 서버에 초대되면 봇이 스스로 나갑니다.**
서버 ID는 개발자 모드를 켠 뒤 서버 이름 우클릭 → '서버 ID 복사하기'.

- 비워두면 **잠금이 꺼져서 아무 서버에서나 동작합니다.** 개발 PC가 죽으면 안 되니
  이렇게 해뒀어요. 꺼져 있으면 기동 로그가 매번 알려줍니다 (`🔓 서버 잠금이 꺼져…`)
- 기동할 때 허가 안 된 서버에 이미 들어가 있으면 **나가지 않고 명령만 막습니다.**
  ID를 한 자리 잘못 적었을 때 봇이 클라이언트의 진짜 서버에서 나가버리면, 다시
  초대해서 세팅을 처음부터 해야 하거든요. 오타 하나에 치르는 대가로는 너무 큽니다
- ⚠️ **기술적 보호가 아니라 자물쇠예요.** 코드를 뜯으면 이 검사도 지울 수 있습니다.
  실제로 남을 막는 건 [LICENSE](LICENSE)고, 이건 "복사해서 옆 서버에 꽂아 쓰기"라는
  제일 흔하고 제일 쉬운 새어나감을 막는 장치입니다

고를 수 있는 기능과 의존 관계는 `guild.example.json`의 `_modules설명`에 전부 적혀
있어요. `setting`·`help`·`wizard`·`diagnostics`·`backup` 다섯 개는 안 적어도 항상 켜집니다.

> 💡 `modules` 항목을 통째로 지우면 **전부 켜기**가 됩니다. 골라 담을 거면 꼭 적으세요.

#### 판매 라인 프리셋

| 라인 | 컨셉 | `modules` |
|---|---|---|
| **에센셜** | 서버 관리 기본 | `id` `wiki` `birthday` `snooze` `selfrole` `welcome` |
| **스탠다드** | ＋ 활동과 보상 | ＋ `economy` `shop` `games` `levels` |
| **프리미엄** | ＋ 운영 콘텐츠·AI | ＋ `stock` `gpt` `chronicle` `party` `scrim` |

전 라인 공통(코어, 끌 수 없음): `setting` `help` `diagnostics` `backup` `wizard`

값은 `guild.example.json`의 `_티어프리셋`에 그대로 들어 있어요. 한 줄을 복사해
`modules` 자리에 넣으면 됩니다.

위 라인은 아래 라인을 **통째로** 포함하고, 라인 안에서 의존 관계도 미리 맞춰뒀어요
(상점을 담으면 지갑이 이미 들어 있습니다). 그래서 주문서에 없던 기능이 자동으로
딸려가는 일이 없어요. 라인 정의는 `chunsik/modules.py`의 `TIER_SPECS` 한 곳에 있고,
`tools/check_tiers.py`가 예시 파일과 대조합니다.

> 💡 AI 대화(`gpt`)는 `GEMINI_API_KEY` 사용량이 계속 나가는 기능이에요. 라인에 넣는
> 대신 월 애드온으로 빼는 것도 방법입니다.

> 🚨 **JSON 문법이 깨지면 봇이 아예 안 켜집니다.** 일부러 그렇게 해뒀어요 — 그대로
> 켜면 `modules`를 못 읽어서 주문하지 않은 기능까지 전부 켜진 채로 납품되거든요.
> 클라이언트가 그 기능을 쓰기 시작한 뒤에 고치면 그 데이터가 통째로 빠집니다.
> 저장하기 전에 문법 검사를 한 번 돌리세요. (https://jsonlint.com)

### 7. 납품 전 점검 (봇을 켜기 전에)

```bash
.venv\Scripts\python tools\check_modules.py id economy shop birthday   # 이 조합으로
.venv\Scripts\python tools\check_help.py
.venv\Scripts\python tools\check_guild.py
.venv\Scripts\python tools\check_money.py
.venv\Scripts\python tools\check_embeds.py
```

다섯 다 **종료 코드 0** 이어야 합니다. 토큰 없이 돌아가고 디스코드에 연결하지 않아요.
여기서 잡히는 사고 — 슬래시 명령 규격 위반(하나만 틀려도 **명령이 전부 사라집니다**),
없는 명령을 안내하는 도움말, 주문하지 않은 기능이 안내문에 남는 것, 설정 파일 오타,
그리고 **화면이 디스코드 길이 한도를 넘어 통째로 안 보이는 것**.

판매 라인으로 파는 거면 앞의 둘 대신 이 한 줄로 끝납니다. 라인 셋을 각각의 조합으로
검사하고, 예시 파일의 프리셋이 코드와 어긋났는지도 같이 봐요.

```bash
.venv\Scripts\python tools\check_tiers.py            # 세 라인 전부
.venv\Scripts\python tools\check_tiers.py standard   # 이 라인만
```

`check_help.py`는 조합을 열한 개 바꿔가며 두 가지를 봅니다 — 도움말이 **없는 명령**을
안내하는지, 그리고 도움말·명령 설명문에 **주문하지 않은 기능의 이름**이 남아 있는지.
(주식을 안 담았는데 `/감사로그` 설명에 "주식 로그"가 적혀 있는 식. 명령은 제대로
빠졌는데 글자만 남는 거라 눈으로는 잘 안 보여요)

### 7-2. 넘길 폴더 만들기 — `tools/package.py`

이 레포에는 **개발하면서만 쓰는 문서와 코드**도 같이 있습니다. 통째로 넘기면
클라이언트가 그것까지 그대로 봐요.

```bash
.venv\Scripts\python tools\package.py                 # 레포 옆에 "-납품" 폴더를 만듭니다
.venv\Scripts\python tools\package.py D:\어디에\만들지   # 자리를 직접 정하려면
```

**깃이 추적하는 파일만** 담습니다. 그래서 `chunsik/data/`(유저 지갑·아이디)·
`chunsik/guild.json`(서버 고유 ID)·`.env`(토큰)는 **애초에 담길 수가 없어요.**
거기서 개발용 문서와 창고 폴더를 더 빼냅니다.

만든 뒤에 두 가지를 같이 봐요 — 담긴 문서가 **안 나가는 파일을 가리키는지**(깨진 링크가
되거나, 없는 문서를 찾아 헤매게 됩니다), 그리고 밖으로 나가면 안 되는 말이 남아 있는지.
무엇이 나가고 무엇이 안 나가는지는 `tools/package.py`의 `EXCLUDE`에 이유와 함께
적혀 있습니다. 문제가 있으면 **종료 코드 1**로 끝나요.

> 💡 만든 폴더에서 **위 7번 검사를 한 번 더** 돌려보세요. 빠뜨린 파일이 있으면
> 거기서 바로 걸립니다.

### 8. 실행

```bash
cd chunsik
..\.venv\Scripts\python main.py
```

기동 로그에서 확인할 것:

```
🧩 모듈 N/N개 로드 완료: ...        ← 주문한 기능이 다 있나
⚠️ 서버 설정(guild.json)에 ...      ← 경고가 있으면 읽어보세요
🏷️ 서버 이름이 아직 기본값이에요     ← 정상 (다음 단계에서 처리)
```

### 9. `/초기설정` — 클라이언트가 이름 정하기

서버 관리자가 `/초기설정` 을 한 번 실행하면 입력창이 뜹니다.
**재화 / 봇 / 이벤트 / 서버** 이름 네 가지를 넣으면 그 자리에서 저장돼요.

- 각 12자 이내
- 메시지·도움말은 **즉시** 반영, 슬래시 명령 **설명문**은 봇 재시작 후 반영
- 명령어 **이름**은 안 바뀝니다 (일부러 — 이름을 값으로 만들면 그 값이 굳는 시점과
  디스코드에 등록되는 시점이 어긋나서, 바꿔도 옛 이름으로 등록됩니다)

### 10. `/설치 시작` — 채널·역할 한 번에

```
/설치 시작        설명을 보면서 하나씩 정해요 (← 클라이언트에게 안내할 것)
/설치 점검        아직 지정 안 된 채널·역할 확인 (아무것도 안 만들어요)
/설치 자동생성     고르는 과정 없이 빠진 것을 전부 새로 만들어요 (빈 서버용 급행)
```

`/설치 시작`이 하는 일 —

1. **설명부터.** 채널이 몇 개 필요한지, 관리자만 보는 곳이 몇 개이고 유저도 보는 곳이
   몇 개인지 먼저 알려줍니다.
2. **있는 채널을 먼저 물어봅니다.** 채널마다 드롭다운이 하나씩 나오고, 서버에 이미
   쓰는 채널이 있으면 그걸 고르면 돼요. 고른 건 만들지 않고 **지정만** 합니다.
3. **비워둔 것만 새로 만듭니다.** 마지막 확인 화면에서 "그대로 쓸 것 / 새로 만들 것"을
   나눠 보여주고, 거기서 눌러야 실제로 만들어져요.

채널마다 **무엇에 쓰는 곳이고 누가 보는 곳인지**(🔒 관리자만 / 👥 유저도 봄)가 같이
나옵니다. 이게 없으면 `역할로그`와 `아이디로그`가 뭐가 다른지 알 수 없어서 일단 다
만들게 되고, 이미 있던 로그 채널 옆에 똑같은 게 하나 더 생겨요.

> 🚦 **마지막 확인을 누르기 전까지는 아무것도 만들지 않습니다.** 중간에 창을 닫으면
> 서버는 손댄 적 없는 상태 그대로예요.

담은 기능이 요구하는 것만 나옵니다. 여기서 제일 오래 걸리던 20~30분이 몇 분으로 줄어듭니다.

채널은 **성격별 카테고리로 나눠** 담아요 — `📋 로그` · `🆔 아이디` · `📈 주식` ·
`🛒 상점` · `💰 경제` · `🎉 알림`. 상점 채널을 새로 만들면 **매대까지 세워둡니다**
(물건만 `/상점 항목추가`로 넣으면 돼요). 한 카테고리에 열몇 개를 몰아넣으면 서버 목록이 벽이 되니까요.
카테고리 이름 앞에 뭘 붙이고 싶으면 `접두사`를 주세요 (`봇` → `봇 📋 로그`).
묶음을 바꾸려면 `chunsik/cogs/wizard.py`의 `CATEGORY_BY_CHANNEL` 한 곳만 고치면 됩니다.

> 🎭 만들어지는 관리자 역할은 **권한이 하나도 없는 표식**이에요. 이 봇의 권한은
> 디스코드 권한이 아니라 "settings.json에 그 역할 ID가 있느냐"로 도니까요.
> 담당자에게 그 역할만 달아주면 됩니다.

손으로 하거나 나중에 바꾸려면:

```
/설정 채널        시스템·로그 채널 지정
/설정 관리자      기능별 관리자 역할 지정
/설정 채널지정내역  지금까지 지정한 것 한눈에 확인
```

### 11. 상시 실행 등록

봇은 계속 켜져 있어야 합니다. 창을 닫으면 꺼져요.

- **윈도우** — 작업 스케줄러에 "시스템 시작 시" 트리거로 등록.
  콘솔이 cp949면 이모지 출력에서 죽을 수 있는데, `main.py`가 스스로 UTF-8로
  바꾸니 괜찮습니다.
- **리눅스** — systemd 유닛으로 등록하고 `Restart=always` 를 넣으세요.
  `main.py`는 예상 못한 종료 시 0이 아닌 코드로 끝나서 자동 재시작이 걸립니다.
  같이 `RestartPreventExitStatus=2` 도 넣어주세요. **사람이 고치기 전에는 다시 켜봐야
  소용없는 실패**(guild.json이 깨짐·토큰이 없거나 무효·ids.json이 깨짐)는 2번으로
  끝나는데, 그 줄이 있으면 그 자리에서 멈춰 서비스가 `failed` 로 보입니다.
  없으면 100ms마다 되살아나며 같은 알림만 쌓여요.

### 12. 🚨 백업을 서버 밖으로

봇이 매일 새벽 3시에 `chunsik/data/backups/` 로 데이터를 통째로 복사합니다.
그 시각에 봇이 꺼져 있었으면 **다음에 켤 때 한 번 따라잡아요** — 낮에만 켜두는 PC라도
하루 한 벌은 남습니다. (마지막 백업이 언제였는지는 `/테스트 데이터점검`에서 봐요)
**그 폴더를 클라우드 드라이브(구글 드라이브·원드라이브 등)에 동기화해 두세요.**

같은 디스크에만 있으면 디스크가 죽는 순간 지갑·아이디·상점 데이터가 같이 사라집니다.
지금 즉시 백업을 돌려보려면 `/테스트 백업실행`.

---

## ✅ 납품 직전 체크리스트

- [ ] Privileged Intent 2개 켰나 (**제일 자주 빠뜨림**)
- [ ] 봇 역할이 부여할 역할들보다 **위**에 있나
- [ ] `check_modules` / `check_help` / `check_guild` / `check_money` / `check_embeds` 전부 종료 0
- [ ] 기동 로그에 담은 모듈이 전부 보이나
- [ ] `ALERT_WEBHOOK_URL` 넣었나 (안 넣으면 봇이 죽어도 아무도 모름)
- [ ] `/초기설정` 으로 이름 넣었나
- [ ] `/설정 채널지정내역` 으로 채널·역할 지정 확인했나
- [ ] `/테스트 채널점검` 으로 실제 발송 되는지 봤나
- [ ] `/도움말` 을 눌러 **없는 기능이 안 보이는지** 확인했나
- [ ] 상시 실행 등록했나
- [ ] `data/backups/` 를 클라우드에 동기화했나
- [ ] **`allowed_guilds` 에 서버 ID를 넣었나** (🔒 서버 잠금 — 기동 로그에 `🔒 서버 잠금 켜짐`)
- [ ] **[LICENSE](LICENSE) 의 빈칸을 채웠나** (저작권자·클라이언트·**서버 ID**·납품일)
      — 안 채우면 "누가 누구에게 무엇을 허락했는지"가 비어서 이 문서로 다툴 수 없어요

---

## 🔧 문제가 생기면

| 증상 | 원인 |
|---|---|
| 기동하자마자 죽음 (`PrivilegedIntentsRequired`) | Intent 2개를 안 켰어요 (위 2번) |
| `❌ 봇 토큰이 없어요!` | `chunsik/.env` 가 없거나 `DISCORD_TOKEN` 이 비었어요 |
| `🚨 서버 설정을 읽을 수 없어…` | `guild.json` JSON 문법이 깨졌어요. 고치기 전엔 **일부러 안 켜집니다** |
| 슬래시 명령이 하나도 안 보임 | 반영에 최대 1시간. 급하면 `/테스트 명령어동기화` |
| 명령이 **통째로** 사라짐 | 동기화 규격 위반. `check_modules.py` 로 원인 확인 |
| `/역할부여`가 조용히 실패 | 봇 역할이 대상 역할보다 아래예요 (위 3번) |
| AI 대화만 안 됨 | `GEMINI_API_KEY` 가 없거나 한도 초과. `/테스트 ai상태` |
| 데이터가 이상함 | `/테스트 데이터점검` — 중복·손상·저장 실패를 한 번에 봐요 |

`/테스트` 그룹이 진단 창구입니다. 담은 기능에 해당하는 점검만 나와요.

---

## 🗂️ 데이터와 커밋 규칙

`chunsik/guild.json`, `chunsik/.env`, `chunsik/data/` 는 `.gitignore` 대상이에요.
**클라이언트 데이터라 절대 커밋하지 마세요.** (폴더 구조는 위 "동작 방식" 참고)
