"""settings.json 읽기·쓰기(캐시 포함), 권한 체크, 기능 킬 스위치, 로그 임베드."""

import os
import copy
import datetime as dt
import discord
from discord.ext import commands

from mari_config import KST, SETTINGS_FILE, active_keys
from mari_storage import DataSaveError, atomic_json_save, safe_json_load

# ========== 🔐 권한 체크 공용 유틸리티 ==========
# 코그마다 제각각 구현되어 있던 "관리자 or 지정 역할 보유" 체크 로직을 하나로 통일.
# settings.json의 roles.<role_key> 에 저장된 역할 ID를 사람이 들고 있는지,
# 혹은 서버 관리자 권한을 갖고 있는지 확인합니다.
def _get_role_ids(settings: dict, role_key: str) -> list:
    """settings["roles"][role_key]를 항상 리스트로 돌려줍니다.
    (예전엔 역할 1개(int)만 저장했는데, 이제는 여러 역할을 동시에 지정할 수 있게 리스트로 관리해요.
    예전 데이터(단일 int)가 남아있어도 자동으로 리스트 취급해서 하위 호환돼요.)"""
    val = settings.get("roles", {}).get(role_key)
    if val is None:
        return []
    if isinstance(val, list):
        return val
    return [val]

def member_has_admin_or_role(member: discord.Member, role_key: str = None) -> bool:
    """서버 관리자 권한을 갖고 있거나, settings.json의 roles.<role_key>에 저장된
    역할(들) 중 하나라도 보유하고 있으면 True. 여러 코그(아이디/상점/주식/설정 등)에서 공용으로 사용합니다."""
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    if role_key:
        settings = load_settings()
        role_ids = _get_role_ids(settings, role_key)
        if role_ids and any(r.id in role_ids for r in member.roles):
            return True
    return False

def has_admin_or_role(interaction: discord.Interaction, role_key: str = None) -> bool:
    """interaction 기반 래퍼 (슬래시 명령어에서 바로 사용)."""
    return member_has_admin_or_role(interaction.user, role_key)

# ========== 🚧 [신규] 기능 정지/재개(킬 스위치) 시스템 ==========
# 아래 FEATURE_KEYS에 있는 기능들을 개별적으로, 또는 한 번에 전부 정지·재개할 수 있어요.
# 이 시스템을 다루는 권한은 일부러 다른 관리자 설정(채널관리자 등)보다 훨씬 엄격하게,
# "진짜 서버 관리자" 또는 "대장 역할" 보유자로만 제한했어요.
_ALL_FEATURE_KEYS = {
    "주식": "stock", "상점": "shop", "송금": "transfer",
    "출석": "attendance", "생일": "birthday", "아이디": "id", "위키": "wiki",
    # 🎚️ 셀프 역할은 **아무나 눌러서 역할을 가져가는** 기능이라, 이상하면 즉시 끌 수
    # 있어야 해요. 정지하면 버튼도 안 먹고 새 패널도 못 만듭니다.
    "셀프역할": "selfrole",
    # 🚪 입장 자동화는 **사람이 안 보는 사이에** 역할을 붙이는 유일한 기능이에요.
    # 잘못 지정했을 때 즉시 멈출 수단이 있어야 합니다. (정지하면 인사·자동 역할·동의 버튼이 전부 멈춰요)
    "입장": "welcome",
    # 📊 레벨은 **모든 메시지를 훑는** 유일한 기능이에요(gpt는 호출됐을 때만 봅니다).
    # 부담이 되거나 이벤트로 경험치를 잠시 멈추고 싶을 때 끌 수 있어야 합니다.
    "레벨": "level",
    # 🎯 파티 모집은 **정해둔 시각에 사람들을 멘션**해요. 오작동하면 알림 폭탄이 되니 끌 수 있어야 합니다.
    "파티": "party",
    # ⏰ 나중에 답장(스누즈)은 돈을 만지지 않지만 **DM을 자동으로 발송**하는 유일한 기능이라,
    # 오작동했을 때 끌 수단이 있어야 해요. 정지하면 새 예약도 막히고 알림 발송도 멈춥니다.
    # (이미 잡혀 있는 예약은 사라지지 않고, 재개하면 밀린 것부터 순서대로 나갑니다)
    "나중에답장": "snooze",
}

# 🧩 [변경] 이번 배포에 담긴 기능만 남깁니다. 예전엔 위 목록이 그대로 선택지가 돼서,
# 주식을 빼고 납품한 서버에서도 `/기능제어 정지 주식`이 버젓이 떴어요.
# 어느 모듈이 어떤 기능 키를 데려오는지는 modules.py의 features에 적혀 있습니다.
_ACTIVE_FEATURES = set(active_keys("features", _ALL_FEATURE_KEYS.values()))
FEATURE_KEYS = {
    label: key for label, key in _ALL_FEATURE_KEYS.items() if key in _ACTIVE_FEATURES
}

# 📝 명령어 설명과 안내 문구에 들어갈 기능 목록.
# 손으로 "주식/상점/송금/..."이라고 나열해두면 기능이 하나 늘 때마다 흩어진 문구를 전부
# 찾아 고쳐야 하고, 실제로 '나중에답장'을 추가할 때 다섯 군데가 어긋나 있었어요.
# FEATURE_KEYS 한 곳만 고치면 모든 문구가 따라오도록 여기서 만들어 씁니다.
#
# ⚠️ 쓸 때 주의: 이 값 **바로 뒤에 조사(을/를, 이/가, 은/는)를 붙이지 마세요.** 목록 마지막
# 항목이 무엇이냐에 따라 맞는 조사가 달라져서, 기능을 추가하는 순간 조용히 틀린 문장이 됩니다.
# (실제로 '나중에답장'이 들어오면서 "…/위키를"이 "…/나중에답장를"이 됐어요)
# "{FEATURE_LIST_TEXT} 기능을 …"처럼 사이에 명사를 하나 끼우거나, 괄호로 감싸서 쓰면 안전해요.
FEATURE_LIST_TEXT = "/".join(FEATURE_KEYS)

def is_feature_enabled(feature_key: str) -> bool:
    settings = load_settings()
    status = settings.get("feature_status", {})
    return status.get(feature_key, True)  # 명시적으로 꺼둔 적 없으면 기본은 항상 켜져 있음

def set_feature_enabled(feature_key: str, enabled: bool):
    settings = load_settings()
    if "feature_status" not in settings:
        settings["feature_status"] = {}
    settings["feature_status"][feature_key] = enabled
    save_settings(settings)

def set_all_features_enabled(enabled: bool):
    """🐛 [성능 버그 수정] 예전엔 /기능제어 전체정지·전체재개가 기능 7개를 하나씩
    set_feature_enabled()로 처리해서, 파일을 7번 읽고 7번 쓰는 구조였어요.
    이제 한 번만 읽고 한 번만 저장해요."""
    settings = load_settings()
    if "feature_status" not in settings:
        settings["feature_status"] = {}
    for key in FEATURE_KEYS.values():
        settings["feature_status"][key] = enabled
    save_settings(settings)

async def feature_gate(interaction: discord.Interaction, feature_key: str, feature_label: str) -> bool:
    """이 기능이 정지 상태면 안내 메세지를 보내고 True(막혔음)를 반환합니다. 정상이면 False."""
    if not is_feature_enabled(feature_key):
        await interaction.response.send_message(f"🚧 **{feature_label}** 기능은 지금 관리자에 의해 일시 정지된 상태예요. 잠시 후 다시 시도해주세요.", ephemeral=True)
        return True
    return False

def is_super_admin(interaction: discord.Interaction) -> bool:
    """서버 관리자 또는 대장(chief_role) 역할 보유자만 True. (테스트 관리자 등 세부 역할로는 절대 통과 못 함)"""
    if interaction.user.guild_permissions.administrator:
        return True
    settings = load_settings()
    chief_id = settings.get("roles", {}).get("chief_role")
    return bool(chief_id and any(r.id == chief_id for r in interaction.user.roles))

# ⚙️ 동적 설정값 불러오기 및 저장하기 유틸리티 함수
# ⚡ [성능 개선] settings.json은 on_message(=서버의 모든 메세지)마다 읽히는데, 예전엔
# 그때마다 파일을 열고 JSON을 파싱했어요. 이제 파일의 (수정시각, 크기)를 기억해뒀다가
# 바뀐 게 없으면 파싱 결과를 재사용합니다. 파일을 손으로 고쳐도 수정시각이 바뀌니까
# 자동으로 다시 읽어요. (user_ids처럼 새로고침 명령이 따로 필요하지 않습니다)
_settings_cache = None
_settings_cache_stamp = None  # (st_mtime_ns, st_size)

def _settings_file_stamp():
    try:
        st = os.stat(SETTINGS_FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None

def load_settings():
    global _settings_cache, _settings_cache_stamp
    if os.path.exists(SETTINGS_FILE):
        stamp = _settings_file_stamp()
        if stamp is not None and stamp == _settings_cache_stamp and _settings_cache is not None:
            # 🛡️ 호출부가 반환값을 그대로 수정한 뒤 저장하지 않는 경우가 있어서,
            # 캐시 원본이 오염되지 않도록 항상 복사본을 돌려줍니다.
            return copy.deepcopy(_settings_cache)

        data = safe_json_load(SETTINGS_FILE, {"roles": {}, "channels": {}})
        _settings_cache = data
        _settings_cache_stamp = stamp
        return copy.deepcopy(data)

    # 설정 파일이 없으면 빈 구조로 자동 생성 (하드코딩 완전 제거)
    default_settings = {"roles": {}, "channels": {}}
    save_settings(default_settings)
    return default_settings

def save_settings(data):
    global _settings_cache, _settings_cache_stamp
    if atomic_json_save(SETTINGS_FILE, data, indent=2):
        # 방금 쓴 내용을 그대로 캐시에 반영해두면 바로 다음 load_settings()도 파일을 안 읽어요.
        _settings_cache = copy.deepcopy(data)
        _settings_cache_stamp = _settings_file_stamp()
    else:
        # 저장이 실패했으면 캐시를 믿을 수 없으니 무효화해서 다음 호출 때 다시 읽게 합니다.
        _settings_cache = None
        _settings_cache_stamp = None
        raise DataSaveError(
            "설정 파일(mari_settings.json)에 저장하지 못했어요. 변경 내용이 반영되지 않았어요."
        )

# ========== 📋 로그 임베드 통일 유틸리티 ==========
# 기존에는 로그 채널마다 일반 텍스트/제각각인 임베드로 보내서
# 관리자가 여러 로그 채널을 오갈 때 어떤 로그인지 한눈에 구분하기 어려웠습니다.
# settings.json의 채널 키(channel_key)별로 고유한 색상·아이콘을 지정해서
# 모든 로그를 "같은 틀(임베드) + 다른 색"으로 통일했습니다.
LOG_STYLES = {
    "id_log":       {"color": discord.Color.gold(),    "emoji": "🪪", "title": "아이디 로그"},
    "role_log":     {"color": discord.Color.purple(),  "emoji": "🎖️", "title": "역할 로그"},
    "economy_log":  {"color": discord.Color.orange(),  "emoji": "💰", "title": "경제 로그"},
    "shop_log":     {"color": discord.Color.blue(),    "emoji": "🛒", "title": "상점 로그"},
    "stock_log":    {"color": discord.Color.green(),   "emoji": "📈", "title": "주식 로그"},
    "closing_log":  {"color": discord.Color.green(),   "emoji": "📊", "title": "주식 로그"},
    "birthday_log": {"color": discord.Color.red(),     "emoji": "🎂", "title": "생일 로그"},
}

def build_log_embed(channel_key: str, description: str, fields: list = None) -> discord.Embed:
    """로그 종류(channel_key)에 맞는 색상/아이콘이 자동 적용된 임베드를 만들어 줍니다.
    fields는 (이름, 값, inline여부) 튜플의 리스트입니다."""
    style = LOG_STYLES.get(channel_key, {"color": discord.Color.greyple(), "emoji": "📋", "title": "로그"})
    embed = discord.Embed(
        title=f"{style['emoji']} {style['title']}",
        description=description,
        color=style["color"],
        timestamp=dt.datetime.now(KST),
    )
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    # 🔁 여기서만 함수 안에서 import합니다. mari_names가 이 파일(load_settings)을 쓰기 때문에
    #    맨 위에 두면 서로를 부르는 순환 import가 돼서 봇이 기동조차 못 해요.
    from mari_names import bot_name
    embed.set_footer(text=f"{bot_name()}봇 로그 시스템")
    return embed

async def send_log_embed(bot: commands.Bot, channel_key: str, description: str,
                          fields: list = None, guild: discord.Guild = None,
                          content: str = None) -> bool:
    """settings.json에 지정된 channel_key 로그 채널로 통일된 임베드를 전송합니다.
    채널이 지정되지 않았거나 권한이 없으면 조용히 넘어갑니다(기존 동작과 동일).

    content — 임베드 **바깥**에 함께 보낼 본문. 주로 담당자 태그(<@&역할ID>)를 넣는 자리예요.
    ⚠️ 태그는 반드시 여기로 넣어야 합니다. 임베드 안에 쓴 멘션은 글자만 파랗게 보일 뿐
       알림이 가지 않아요. (실제로 울려야 하는 건 임베드 밖에 있어야 해요)
    """
    ch_id = load_settings().get("channels", {}).get(channel_key)
    if not ch_id:
        return False

    channel = guild.get_channel(ch_id) if guild else bot.get_channel(ch_id)
    if not channel:
        return False

    me = guild.me if guild else channel.guild.me
    if not channel.permissions_for(me).send_messages:
        return False

    try:
        await channel.send(
            content=content or None,
            embed=build_log_embed(channel_key, description, fields),
            # 역할 태그가 실제로 울리도록 명시합니다. (@everyone/@here는 실수로도 울리지 않게 막아둬요)
            allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
        )
        return True
    except Exception as e:
        print(f"❗ 로그 전송 실패 ({channel_key}): {type(e).__name__} {e}")
        return False
