"""MariShop — 채널별 상점 매대, 구매/되팔기, 인벤토리, 월별 정산."""

import datetime as dt
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands, tasks

from mari_config import ECONOMY_FILE, KST, SHOP_FILE, SHOP_TRANSACTIONS_FILE
from mari_alerts import report_loop_error
from mari_storage import atomic_json_save_or_raise, safe_json_load
from mari_settings import feature_gate, has_admin_or_role, load_settings, send_log_embed
from mari_state import record_ledger
from mari_utils import MariView, report_broken_transaction, schedule_delete

class ShopPurchaseView(MariView):
    """상점 매대에 상시 고정될 드롭다운 뷰 (봇 재시작 대응 Persistent View, 매대=채널별로 독립)"""
    
    def __init__(self, cog, channel_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.channel_id = channel_id
        self.add_item(ShopItemSelect(cog, channel_id))


class ShopItemSelect(discord.ui.Select):
    """상점 물품 목록을 보여주는 드롭다운 메뉴 (자신이 속한 채널=매대의 아이템만 표시)"""
    def __init__(self, cog, channel_id: int):
        self.cog = cog
        self.channel_id = channel_id
        data = self.cog._load_shop()
        board = self.cog._get_board(data, channel_id) or {}
        options = []

        if not board.get("items"):
            options.append(discord.SelectOption(label="🛒 현재 진열된 상품이 없어요.", value="none"))
        else:
            # 🛡️ [안전망] 등록은 MAX_BOARD_ITEMS에서 막지만, 그 제한이 생기기 전에 이미
            # 25개를 넘겨둔 매대가 있을 수 있어요. 여기서 잘라주지 않으면 드롭다운 생성이
            # 예외로 터지면서 매대가 통째로 죽고, 봇 기동 시 Persistent View 등록까지 실패합니다.
            for item_id, info in list(board["items"].items())[:MAX_SELECT_OPTIONS]:
                status = "🟢" if info.get("can_buy", True) else "❌"
                label = f"{status} [{item_id}번] {info['name']}"[:100]
                desc = f"💵 가격: {info['price']:,} 에바 | {info['desc']}"[:100]
                options.append(discord.SelectOption(label=label, value=item_id, description=desc))

        super().__init__(
            placeholder="🛒 구매하거나 되팔고 싶은 상품을 선택하세요...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"mari_shop:item_select:{channel_id}"
        )

    async def callback(self, interaction: discord.Interaction):
        data = self.cog._load_shop()
        board = self.cog._get_board(data, self.channel_id)

        # 🔥 [방어 코드] 이 매대(채널)가 삭제되었거나 없어졌을 경우 차단
        if not board:
            return await interaction.response.send_message("❌ 이 매대는 더 이상 존재하지 않아요.", ephemeral=True)

        if self.values[0] == "none":
            return await interaction.response.send_message("❌ 현재 선택 가능한 상품이 없어요.", ephemeral=True)

        item_id = self.values[0]
        item_info = board["items"].get(item_id)

        if not item_info:
            return await interaction.response.send_message("❌ 존재하지 않거나 삭제된 상품이에요.", ephemeral=True)

        # 개인별 UI 패널 생성
        resale_percent = item_info.get("resale_percent", 0)
        refund_amount = int(item_info["price"] * resale_percent / 100)
        embed = discord.Embed(
            title=f"📦 {item_info['name']} 거래 패널",
            description=(
                f"📝 **설명**: {item_info['desc']}\n💵 **가격**: {item_info['price']:,} 에바\n"
                f"♻️ **되팔기 환급**: 원가의 {resale_percent}% (약 {refund_amount:,} 에바)"
            ),
            color=0x5ce6b4
        )
        type_str = f"역할 지급 (<@&{item_info['role_id']}>)" if item_info["is_role"] else "일반 아이템 (인벤토리 저장)"
        embed.add_field(name="🏷️ 아이템 타입", value=type_str, inline=False)
        
        view = ShopActionButtons(self.cog, self.channel_id, item_id, item_info)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class ShopActionButtons(MariView):
    """유저 개인 전용 구매 / 되팔기 제어 버튼"""

    # ⏱️ 안내 메시지가 사라지기까지의 시간(초)
    NOTICE_AFTER = 5    # ✅ 구매/되팔기 완료 안내 (짧게 확인만 하면 되는 내용)
    ERROR_AFTER = 10    # ❌ 실패 안내 (금액·사유를 읽어야 해서 조금 더 길게)

    def __init__(self, cog, channel_id: int, item_id, item_info):
        super().__init__(timeout=60)
        self.cog = cog
        self.channel_id = channel_id
        self.item_id = item_id
        self.item_info = item_info
        self.buy_button.disabled = not item_info.get("can_buy", True)
        self.sell_btn.disabled = not item_info.get("can_sell", True)
        # 🐛 [버그 수정] 구매/되팔기 버튼을 빠르게 연타하면, 첫 클릭 처리가 끝나기 전에
        # 두 번째 클릭이 들어와서 역할 중복 체크 같은 게 씹히고 두 번 결제되는 문제가 있었어요.
        # 이 패널은 유저 1명당 하나씩 새로 만들어지는 개인 전용 패널이라, 여기에 "처리 중" 표시만
        # 해둬도 같은 패널에서의 연타는 확실하게 막을 수 있어요.
        self.processing = False

    @discord.ui.button(label="구매하기", style=discord.ButtonStyle.green, custom_id="buy_btn")
    async def buy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.processing:
            return await interaction.response.send_message("⏳ 이미 처리 중이에요! 잠시만 기다려주세요.", ephemeral=True)
        self.processing = True
        try:
            await self._do_buy(interaction)
        finally:
            self.processing = False

    async def _dismiss_panel(self, interaction: discord.Interaction):
        """버튼을 누른 즉시 거래 패널을 치웁니다. (인터랙션 응답도 이 함수가 처리해요)

        ⚠️ 이 패널은 ephemeral(누른 사람에게만 보이는) 메시지예요.
        ephemeral 메시지는 `message.delete()`로는 절대 지워지지 않습니다 —
        채널 메시지 삭제 API로는 접근할 수 없고, 인터랙션 응답으로만 수정/삭제가 돼요.
        예전 코드가 `interaction.message.delete()`를 쓰고 있어서 try/except에 걸려
        조용히 실패했고, 그래서 패널이 계속 남아 있었습니다.

        그래서 두 단계로 처리합니다:
          1) edit_message(view=None) — 버튼을 즉시 제거 (이건 확실하게 동작해요)
          2) delete_original_response() — 패널 자체를 삭제
        2번이 실패해도 버튼은 이미 사라진 뒤라 두 번 눌리는 사고는 없습니다.
        """
        self.stop()  # 뷰 타임아웃 타이머 정리
        try:
            await interaction.response.edit_message(view=None)
        except Exception:
            # 이미 응답이 나갔거나 편집이 막힌 경우엔 최소한 인터랙션만 살려둡니다.
            try:
                await interaction.response.defer(ephemeral=True)
            except Exception:
                pass
            return

        try:
            await interaction.delete_original_response()
        except Exception:
            pass

    async def _notify(self, interaction: discord.Interaction, text: str, *, error: bool = False):
        """안내 메시지를 띄우고 자동 삭제를 예약합니다. (락을 붙잡지 않아요)"""
        try:
            msg = await interaction.followup.send(text, ephemeral=True)
        except Exception:
            return
        schedule_delete(msg, self.ERROR_AFTER if error else self.NOTICE_AFTER)

    async def _do_buy(self, interaction: discord.Interaction):
        if await feature_gate(interaction, "shop", "상점"):
            return
        # 🖱️ [UX] 버튼을 누르는 순간 버튼을 없애고 패널을 치웁니다.
        # (인터랙션 응답도 이 안에서 처리하므로 여기서 따로 defer하지 않아요)
        await self._dismiss_panel(interaction)

        user_id = interaction.user.id
        user_id_str = str(user_id)
        price = self.item_info["price"]

        # 1. 역할 중복 소유 체크 (지갑을 건드리지 않으므로 락이 필요 없어요)
        role = None
        if self.item_info["is_role"]:
            role = interaction.guild.get_role(self.item_info["role_id"])
            if not role:
                return await self._notify(interaction, "❌ 서버에 지급할 역할이 존재하지 않아요.", error=True)
            if role in interaction.user.roles:
                return await self._notify(interaction, "❌ 이미 해당 역할을 보유하고 있어 중복 구매할 수 없어요.", error=True)

        # 2. 결제 처리
        # 🔒 [동시성 제어] 연타로 인한 돈 복사 버그 원천 차단
        # 🐛 [버그 수정] 락 안에서는 계산과 저장만 하고, 안내 메시지는 전부 락을 푼 뒤에 보냅니다.
        #    (예전엔 실패 안내를 락 안에서 띄우고 60초를 기다려서 서버 경제 전체가 멈췄어요)
        error = None
        new_balance = None
        broken = None       # 지갑은 빠졌는데 아이템 지급이 실패한 경우
        async with self.cog.bot.economy_lock:
            current_balance = self.cog._get_balance(user_id)
            if current_balance < price:
                error = (f"❌ 에바가 부족합니다!\n"
                         f"└ 보유 잔액: {current_balance:,} 에바 / 필요 금액: {price:,} 에바")
            # 🏪 매대가 살아 있는지만 먼저 확인해요. 여기서 읽은 사본은 **일부러 버립니다.**
            #    (아래 역할 지급 await을 건너온 사본은 낡은 것이라 저장에 쓰면 안 돼요)
            elif not self.cog._get_board(self.cog._load_shop(), self.channel_id):
                error = "❌ 이 매대는 더 이상 존재하지 않아요."
            else:
                self.cog._set_balance(user_id, current_balance - price)

                if self.item_info["is_role"]:
                    # ⚠️ 역할 지급은 락 안에 있어야 해요. 실패 시 잔액을 되돌려야 하는데,
                    # 그 사이 다른 거래가 끼어들면 롤백이 엉뚱한 금액을 덮어쓰게 됩니다.
                    # 🐛 [버그 수정] 예전엔 discord.Forbidden만 잡았어요. 잔액은 바로 위에서
                    # 이미 차감·저장된 뒤라, 디스코드가 5xx를 내거나 요청이 끊기면 예외가 락
                    # 밖으로 튀어나가면서 롤백도 안내도 없이 **돈만 사라졌습니다.**
                    try:
                        await interaction.user.add_roles(role)
                    except Exception as role_err:
                        self.cog._set_balance(user_id, current_balance)  # 실패 시 롤백
                        error = ("❌ 봇에게 역할 지급 권한이 없어요."
                                 if isinstance(role_err, discord.Forbidden) else
                                 f"❌ 역할을 지급하지 못했어요. 잠시 뒤 다시 시도해 주세요.\n"
                                 f"└ 에바는 차감되지 않았어요. ({type(role_err).__name__})")

                if error is None:
                    # 🚨 [심각한 버그 수정] shop.json은 **여기서 처음 읽습니다.**
                    # 예전엔 위 add_roles await보다 먼저 읽은 사본을 그대로 저장했어요.
                    # 그 await이 걸린 동안 economy_lock을 잡지 않는 코드(`/상점 사용`,
                    # `/상점 항목추가` 등)가 shop.json에 쓴 내용이 통째로 되돌아갔습니다.
                    # (같은 사고가 예전에 확성기 쪽에서 터진 적이 있어요 — games.py 상단 주석 참고)
                    data = self.cog._load_shop()
                    board = self.cog._get_board(data, self.channel_id)
                    if board is None:
                        # 역할을 지급하는 찰나에 매대가 삭제된 극단적인 경우예요.
                        # 담을 곳이 없으니 결제만 되돌립니다. (지급된 역할은 관리자가 회수해 주세요)
                        self.cog._set_balance(user_id, current_balance)
                        error = "❌ 이 매대는 더 이상 존재하지 않아요."
                    else:
                        inv = board.setdefault("inventories", {}).setdefault(user_id_str, {})
                        inv[self.item_id] = inv.get(self.item_id, 0) + 1
                        # 💾 지갑은 이미 차감돼 저장됐어요. 여기가 실패하면 "돈만 빠지고 물건은
                        # 못 받은" 반쪽 상태가 됩니다. (역할 아이템은 이미 지급된 뒤예요)
                        try:
                            self.cog._save_shop(data)
                        except Exception as save_err:
                            broken = {
                                "lost": f"에바 {price:,}",
                                "not_received": self.item_info["name"],
                                "error": save_err,
                                "balance": current_balance - price,
                            }
                        else:
                            new_balance = current_balance - price

        if broken:
            return await report_broken_transaction(
                interaction, action="상점 구매", user_id=user_id,
                lost=broken["lost"], not_received=broken["not_received"], error=broken["error"],
                ledger_delta=-price, ledger_balance=broken["balance"],
            )

        if error:
            return await self._notify(interaction, error, error=True)

        await self.cog.log_purchase(interaction.user, self.item_info["name"], 1, price, new_balance)
        self.cog._record_transaction(self.channel_id, self.item_info["name"], "buy", price)
        self.cog._record_ledger(user_id, -price, new_balance, "상점 구매", self.item_info["name"])

        await self._notify(
            interaction,
            f"✅ `{self.item_info['name']}`을(를) 성공적으로 구매했어요! (-{price:,} 에바)",
        )

    @discord.ui.button(label="💰 되팔기", style=discord.ButtonStyle.danger)
    async def sell_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.processing:
            return await interaction.response.send_message("⏳ 이미 처리 중이에요! 잠시만 기다려주세요.", ephemeral=True)
        self.processing = True
        try:
            await self._do_sell(interaction)
        finally:
            self.processing = False

    async def _do_sell(self, interaction: discord.Interaction):
        if await feature_gate(interaction, "shop", "상점"):
            return
        # 🖱️ [UX] 버튼을 누르는 순간 버튼을 없애고 패널을 치웁니다.
        # (인터랙션 응답도 이 안에서 처리하므로 여기서 따로 defer하지 않아요)
        await self._dismiss_panel(interaction)

        user_id = interaction.user.id
        user_id_str = str(user_id)
        price = self.item_info["price"]
        # 💰 [신규] 원가를 그대로 돌려주지 않고, 등록된 되팔기퍼센트만큼만 환급합니다.
        resale_percent = self.item_info.get("resale_percent", 0)
        refund = int(price * resale_percent / 100)
        
        # 🔒 [동시성 제어] 연타 반납 버그 차단
        # 🐛 [버그 수정] 구매와 마찬가지로 안내 메시지는 락을 푼 뒤에 보냅니다.
        error = None
        new_balance = None
        broken = None       # 아이템은 빠졌는데 환급 저장이 실패한 경우
        async with self.cog.bot.economy_lock:
            # 1. 판매 자격 체크
            #    🏪 여기서 읽은 사본은 자격 확인에만 쓰고 **버립니다.** 아래 역할 회수 await을
            #    건너온 사본으로 저장하면, 그 사이 다른 곳이 쓴 내용을 통째로 되돌려버려요.
            board = self.cog._get_board(self.cog._load_shop(), self.channel_id)
            role = None
            if not board:
                error = "❌ 이 매대는 더 이상 존재하지 않아요."
            elif self.item_info["is_role"]:
                role = interaction.guild.get_role(self.item_info["role_id"])
                if not role or role not in interaction.user.roles:
                    error = "❌ 현재 해당 역할을 가지고 있지 않아 판매할 수 없어요."
            elif board.get("inventories", {}).get(user_id_str, {}).get(self.item_id, 0) <= 0:
                error = "❌ 인벤토리에 반납할 아이템 수량이 없어요."

            # 2. 역할 회수 — 이 함수에서 유일한 await 구간이에요.
            if error is None and self.item_info["is_role"]:
                # ⚠️ 역할 회수도 락 안에 있어야 해요. 실패하면 환급을 하면 안 되니까요.
                # 🐛 [버그 수정] 예전엔 discord.Forbidden만 잡았어요. 디스코드가 5xx를 내거나
                # 요청이 끊기면 예외가 락 밖으로 튀어나가서, 유저에게 아무 안내도 없이
                # 패널만 사라진 채 끝났습니다.
                try:
                    await interaction.user.remove_roles(role)
                except Exception as role_err:
                    error = ("❌ 봇에게 역할 회수 권한이 없어요."
                             if isinstance(role_err, discord.Forbidden) else
                             f"❌ 역할을 회수하지 못했어요. 잠시 뒤 다시 시도해 주세요.\n"
                             f"└ 아이템과 에바는 그대로예요. ({type(role_err).__name__})")

            # 3. 아이템 회수 처리 — shop.json은 위 await이 끝난 **뒤에** 다시 읽습니다.
            if error is None:
                data = self.cog._load_shop()
                board = self.cog._get_board(data, self.channel_id)
                if board is None:
                    error = "❌ 이 매대는 더 이상 존재하지 않아요."
                else:
                    # 🐛 [버그 수정] 예전엔 여기서 수량만 1 줄이고 **0이 된 칸을 지우지 않았어요.**
                    # 선물(apply_gift)·차감(consume_items_by_name)·아이템 사용(use_item)은
                    # 전부 0이 되면 칸을 지우는데 되팔기만 빠져 있었습니다. 그래서 산 걸 전부 되팔면
                    # 인벤토리에 {"3": 0}이 남았고, `/상점 사용`의 자동완성이 그걸 그대로 읽어서
                    # "보유: 0개" 항목을 힌트로 띄웠어요. 골라봐야 수량 부족으로 거부당하고,
                    # /지갑에는 안 보이는 물건이 힌트에만 뜨는 상태였습니다.
                    inv = board.setdefault("inventories", {}).setdefault(user_id_str, {})
                    if inv.get(self.item_id, 0) > 0:
                        inv[self.item_id] -= 1
                        if inv[self.item_id] <= 0:
                            del inv[self.item_id]      # 0개가 된 칸은 지워서 인벤토리를 깔끔하게 유지
                        self.cog._save_shop(data)
                    # 역할 아이템인데 인벤토리 기록이 없을 수도 있어요. (관리자가 역할을 직접 준 뒤
                    # 되파는 경우) 지울 칸이 없을 뿐 역할은 이미 회수됐으니 환급은 그대로 진행합니다.

            # 4. 돈 지급 (원가가 아닌, 되팔기퍼센트가 적용된 refund 금액)
            if error is None:
                current_balance = self.cog._get_balance(user_id)
                new_balance = current_balance + refund
                # 💾 아이템(또는 역할)은 이미 회수돼 저장됐어요. 여기가 실패하면
                # "물건만 사라지고 환급은 못 받은" 반쪽 상태가 됩니다.
                try:
                    self.cog._set_balance(user_id, new_balance)
                except Exception as save_err:
                    broken = {
                        "lost": self.item_info["name"],
                        "not_received": f"에바 {refund:,}",
                        "error": save_err,
                    }
                    new_balance = None

        if broken:
            # 환급이 아예 안 들어갔으니 원장에 남길 에바 변동이 없어요. (복구는 /지급 또는 /상점으로)
            return await report_broken_transaction(
                interaction, action="상점 되팔기", user_id=user_id,
                lost=broken["lost"], not_received=broken["not_received"], error=broken["error"],
            )

        if error:
            return await self._notify(interaction, error, error=True)

        await self.cog.log_purchase(
            interaction.user, f"[되팔기 {resale_percent}%] {self.item_info['name']}", 1, -refund, new_balance
        )
        self.cog._record_transaction(self.channel_id, self.item_info["name"], "sell", refund)
        self.cog._record_ledger(user_id, refund, new_balance, "상점 되팔기", self.item_info["name"])

        await self._notify(
            interaction,
            f"💵 `{self.item_info['name']}`을(를) 되팔았어요. "
            f"(원가의 {resale_percent}% · +{refund:,} 에바 지급)",
        )


# ========== 🎁 [신규] 아이템 선물 시스템 ==========
# /지갑에 붙는 버튼으로, 상점에서 산 아이템을 다른 사람에게 그대로 넘겨줍니다.
# (돈을 보내는 /송금과 달리 에바는 전혀 오가지 않고, 인벤토리 숫자만 이동해요)
#
# 🔒 [역할 아이템은 일부러 제외했어요]
# 역할 아이템은 상태가 두 군데로 나뉘어 있습니다 — 인벤토리 숫자(shop.json)와
# 실제 디스코드 역할이요. 인벤토리는 파일 한 번 저장으로 원자적으로 옮길 수 있지만,
# 역할은 API를 두 번(주는 사람 회수 → 받는 사람 부여) 호출해야 하고 그 사이에서
# 따로따로 실패할 수 있어요. 회수만 성공하면 아무도 역할이 없는 상태가 되고,
# 되돌리는 호출마저 실패할 수 있습니다. 게다가 구매(_do_buy: 역할 중복 구매 금지)와
# 되팔기(_do_sell: 역할이 없으면 거부)가 전부 "역할 아이템은 1인 1개"를 전제로
# 짜여 있어서, 선물로 2개가 쌓이면 남은 하나를 영영 되팔 수 없게 돼요.
# 그래서 선물 목록에서 아예 빼두었습니다.

MAX_SELECT_OPTIONS = 25     # 디스코드 드롭다운 한 개에 넣을 수 있는 최대 항목 수

# 🚧 한 매대에 둘 수 있는 상품 수 상한.
# 디스코드가 정한 두 가지 한계가 공교롭게도 똑같이 25라서 이 숫자 하나로 묶었어요.
#   • 임베드 하나에 넣을 수 있는 필드 25개  → _generate_shop_embed가 상품마다 필드를 하나씩 씀
#   • 드롭다운 하나에 넣을 수 있는 항목 25개 → ShopItemSelect가 상품마다 항목을 하나씩 씀
# 이 선을 넘으면 전광판이 "일부만 깨지는" 게 아니라 **아예 안 그려집니다.**
# 게다가 드롭다운 생성이 터지면 봇 기동 시 Persistent View 등록까지 실패해서, 봇을 다시
# 켜도 기존 매대의 구매 버튼이 죽은 채로 남아요. 그래서 나중에 터뜨리지 않고 등록 자리에서 막습니다.
MAX_BOARD_ITEMS = 25


class GiftItemSelect(discord.ui.Select):
    """선물할 아이템 고르기. 값은 panel.giftables의 인덱스예요.
    (아이템 이름을 값으로 쓰면 100자 제한에 걸리거나 특수문자로 깨질 수 있어서요)"""

    def __init__(self, panel):
        self.panel = panel
        options = [
            discord.SelectOption(
                label=f"{it['name']} ({it['qty']}개)"[:100],
                description=f"매대: {it['board_name']}"[:100],
                value=str(idx),
            )
            for idx, it in enumerate(panel.giftables[:MAX_SELECT_OPTIONS])
        ]
        super().__init__(placeholder="📦 선물할 아이템을 고르세요", options=options,
                         min_values=1, max_values=1, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.panel.picked = self.panel.giftables[int(self.values[0])]
        # 아이템이 바뀌면 보낼 수 있는 최대 수량도 달라지니 수량을 1개로 되돌립니다.
        self.panel.amount = 1
        for opt in self.options:
            opt.default = (opt.value == self.values[0])
        await self.panel.refresh(interaction)


class GiftUserSelect(discord.ui.UserSelect):
    """선물 받을 사람 고르기 (서버 멤버 목록에서 직접 검색·선택)"""

    def __init__(self, panel):
        self.panel = panel
        super().__init__(placeholder="🙋 선물 받을 사람을 고르세요",
                         min_values=1, max_values=1, row=1)

    async def callback(self, interaction: discord.Interaction):
        picked = self.values[0]
        # UserSelect는 서버에 없는 유저면 Member가 아닌 User를 돌려줄 수 있어요.
        if not isinstance(picked, discord.Member) and interaction.guild:
            picked = interaction.guild.get_member(picked.id) or picked
        self.panel.receiver = picked
        await self.panel.refresh(interaction)


class GiftAmountSelect(discord.ui.Select):
    """수량 고르기. 고른 아이템의 보유량에 맞춰 선택지를 매번 다시 만듭니다."""

    def __init__(self, panel):
        self.panel = panel
        super().__init__(placeholder="🔢 수량 (아이템을 먼저 고르세요)",
                         options=[discord.SelectOption(label="1개", value="1")],
                         min_values=1, max_values=1, disabled=True, row=2)

    def rebuild(self):
        if not self.panel.picked:
            self.disabled = True
            self.placeholder = "🔢 수량 (아이템을 먼저 고르세요)"
            self.options = [discord.SelectOption(label="1개", value="1")]
            return

        qty = self.panel.picked["qty"]
        self.disabled = False
        self.placeholder = "🔢 몇 개 보낼까요?"

        if qty <= MAX_SELECT_OPTIONS:
            values = list(range(1, qty + 1))
        else:
            # 드롭다운은 25칸이 한계라, 1~24개까지 고르게 하고 마지막 칸은 '전부'로 채워요.
            values = list(range(1, MAX_SELECT_OPTIONS)) + [qty]

        self.options = [
            discord.SelectOption(
                label=(f"전부 ({v}개)" if v > MAX_SELECT_OPTIONS else f"{v}개"),
                value=str(v),
                default=(v == self.panel.amount),
            )
            for v in values
        ]

    async def callback(self, interaction: discord.Interaction):
        self.panel.amount = int(self.values[0])
        await self.panel.refresh(interaction)


class GiftPanelView(MariView):
    """🎁 선물 패널 — 아이템 / 받을 사람 / 수량을 고르고 보내는 개인 전용(ephemeral) 창.

    ephemeral 메세지라 누른 사람에게만 보이고, 다른 사람은 버튼 자체에 접근할 수 없어요.
    """

    def __init__(self, cog, giver: discord.Member, giftables: list):
        super().__init__(timeout=180)
        self.cog = cog
        self.giver = giver
        self.giftables = giftables      # collect_giftable_items() 결과
        self.picked = None              # 고른 아이템 (giftables의 원소)
        self.receiver = None            # 받을 사람
        self.amount = 1
        self.processing = False
        self.origin_interaction = None  # 타임아웃 때 패널을 잠그기 위해 보관

        self.item_select = GiftItemSelect(self)
        self.user_select = GiftUserSelect(self)
        self.amount_select = GiftAmountSelect(self)
        self.add_item(self.item_select)
        self.add_item(self.user_select)
        self.add_item(self.amount_select)
        self.amount_select.rebuild()

    def build_embed(self) -> discord.Embed:
        """지금까지 고른 내용을 위쪽 임베드에 요약해서 보여줍니다.
        (드롭다운은 화면을 새로 그리면 선택 표시가 흐려질 수 있어서, 확정된 값은 여기서 확인해요)"""
        embed = discord.Embed(
            title="🎁 선물 보내기",
            description="아래 세 칸을 모두 고른 뒤 **보내기**를 눌러주세요.\n에바는 오가지 않고 아이템만 넘어가요.",
            color=0x5ce6b4,
        )
        embed.add_field(
            name="📦 선물할 아이템",
            value=(f"**{self.picked['name']}** · 보유 {self.picked['qty']}개 *({self.picked['board_name']})*"
                   if self.picked else "*아직 고르지 않았어요*"),
            inline=False,
        )
        embed.add_field(
            name="🙋 받을 사람",
            value=(self.receiver.mention if self.receiver else "*아직 고르지 않았어요*"),
            inline=False,
        )
        embed.add_field(
            name="🔢 수량",
            value=(f"**{self.amount}개**" if self.picked else "*아이템을 먼저 골라주세요*"),
            inline=False,
        )

        footer = "역할이 지급되는 아이템은 선물할 수 없어서 목록에 없어요."
        if len(self.giftables) > MAX_SELECT_OPTIONS:
            footer += f" · 종류가 많아 앞 {MAX_SELECT_OPTIONS}종만 표시했어요."
        embed.set_footer(text=footer)
        return embed

    async def refresh(self, interaction: discord.Interaction):
        """드롭다운을 하나 고를 때마다 패널을 다시 그립니다."""
        self.amount_select.rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        # ⌛ 방치된 선물 패널이 한참 뒤에 눌리는 일이 없도록 잠가둡니다.
        if self.processing or not self.origin_interaction:
            return
        try:
            await self.origin_interaction.edit_original_response(
                content="⌛ 시간이 지나 선물 창을 닫았어요. 아무것도 보내지 않았어요.",
                embed=None, view=None,
            )
        except Exception:
            pass

    @discord.ui.button(label="🎁 보내기", style=discord.ButtonStyle.green, row=3)
    async def send_gift(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 연타로 두 번 보내지는 사고를 막아요. (이 패널은 유저 1명 전용이라 이걸로 충분합니다)
        if self.processing:
            return await interaction.response.send_message("⏳ 이미 처리 중이에요! 잠시만요.", ephemeral=True)
        self.processing = True
        try:
            await self._do_gift(interaction)
        finally:
            self.processing = False

    @discord.ui.button(label="❌ 취소", style=discord.ButtonStyle.secondary, row=3)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.processing:
            return await interaction.response.send_message("⏳ 이미 선물이 처리 중이라 취소할 수 없어요.", ephemeral=True)
        self.stop()
        await interaction.response.edit_message(content="🚫 선물을 취소했어요.", embed=None, view=None)

    async def _do_gift(self, interaction: discord.Interaction):
        if await feature_gate(interaction, "shop", "상점"):
            return

        # 1️⃣ 아직 안 고른 칸이 있는지 확인
        missing = []
        if not self.picked:
            missing.append("선물할 아이템")
        if not self.receiver:
            missing.append("받을 사람")
        if missing:
            return await interaction.response.send_message(
                f"❌ **{' · '.join(missing)}**을(를) 아직 고르지 않았어요.", ephemeral=True
            )

        # 2️⃣ 받을 사람 검증
        if self.receiver.id == self.giver.id:
            return await interaction.response.send_message(
                "💥 자기 자신에게는 선물할 수 없어요!", ephemeral=True
            )
        if self.receiver.bot:
            return await interaction.response.send_message(
                "🤖 봇에게는 선물할 수 없어요!", ephemeral=True
            )

        # 3️⃣ 실제 이동
        # 🔒 구매·되팔기가 economy_lock을 잡은 채로 shop.json을 쓰기 때문에(_do_buy 참고),
        #    선물도 같은 락을 잡아야 저장이 겹쳐서 인벤토리가 통째로 되돌아가는 일이 없어요.
        #    ⚠️ 락 안에서는 메세지를 보내지 않습니다. (안내를 락 안에서 띄우면 경제 전체가 멈춰요)
        async with self.cog.bot.economy_lock:
            error, result = self.cog.apply_gift(
                self.giver.id, self.receiver.id,
                self.picked["channel_id"], self.picked["item_id"], self.amount,
            )

        if error:
            return await interaction.response.send_message(error, ephemeral=True)

        # 4️⃣ 패널을 결과 화면으로 바꾸고 잠급니다.
        self.stop()
        done = discord.Embed(
            title="🎁 선물 완료!",
            description=(f"{self.receiver.mention}님에게 **{result['name']}** {self.amount}개를 보냈어요.\n"
                         f"└ 내 남은 수량: **{result['remain']}개** *({result['board_name']})*"),
            color=0x5ce6b4,
        )
        await interaction.response.edit_message(embed=done, view=None)

        # 5️⃣ 채널 공개 안내 + 상점 로그 (여기서 실패해도 선물 자체는 이미 끝났어요)
        try:
            await interaction.followup.send(
                f"🎁 {self.giver.mention}님이 {self.receiver.mention}님에게 "
                f"**{result['name']}** {self.amount}개를 선물했어요! ✨",
                ephemeral=False,
            )
        except Exception:
            pass
        await self.cog.log_gift(self.giver, self.receiver, result, self.amount)


class WalletGiftView(MariView):
    """/지갑 임베드 아래에 붙는 '선물하기' 버튼."""

    def __init__(self, cog, owner_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.owner_id = owner_id
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # 🛡️ /지갑은 공개 메세지라 버튼도 모두에게 보여요. 지갑 주인만 누르게 막습니다.
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "🙅‍♀️ 이 버튼은 지갑 주인만 쓸 수 있어요. `/지갑`으로 본인 지갑을 열어주세요.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="🎁 선물하기", style=discord.ButtonStyle.primary)
    async def gift_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await feature_gate(interaction, "shop", "상점"):
            return

        # 지갑을 띄워둔 사이에 되팔았을 수도 있어서, 누른 시점 기준으로 다시 읽어요.
        giftables = self.cog.collect_giftable_items(interaction.user.id)
        if not giftables:
            return await interaction.response.send_message(
                "🎒 지금 선물할 수 있는 아이템이 없어요.\n"
                "└ 역할이 지급되는 아이템은 선물 대상이 아니에요.",
                ephemeral=True,
            )

        panel = GiftPanelView(self.cog, interaction.user, giftables)
        await interaction.response.send_message(embed=panel.build_embed(), view=panel, ephemeral=True)
        panel.origin_interaction = interaction


class MariShop(commands.Cog):
    """에바스타운 공식 상점 및 인벤토리 시스템"""
    
    # 🔑 [이 코그의 데이터 안전 규칙]
    # shop.json 하나에 매대 정보와 **전원의 인벤토리**가 같이 들어 있어요. 그래서
    # `_load_shop()`으로 읽은 사본을 오래 들고 있다가 저장하면, 그 사이에 일어난
    # 다른 사람의 구매가 통째로 되돌아갑니다.
    #
    #   ✅ 읽고 → 고치고 → 저장하는 사이에 `await`을 두지 마세요.
    #      (await이 없으면 중간에 다른 작업이 끼어들 수 없어서 그 구간은 이미 안전해요)
    #   ✅ 메세지 전송처럼 await이 꼭 필요하면, **저장 직전에 파일을 다시 읽어서**
    #      바꿀 값만 반영하세요. (_refresh_board_message가 그렇게 하고 있어요)
    #
    # 예전엔 이 목적으로 self.lock(asyncio.Lock)을 만들어뒀는데 실제로는 아무 데서도
    # 쓰이지 않는 죽은 코드였어요. 락을 늘리면 economy_lock과 순서가 엇갈려 교착이
    # 생길 위험만 커져서, 락 대신 위 규칙을 지키는 방식으로 정리했습니다.

    def __init__(self, bot):
        self.bot = bot
        self.SHOP_FILE = SHOP_FILE
        self.SHOP_TRANSACTIONS_FILE = SHOP_TRANSACTIONS_FILE
        # 🖱️ [신규] 멤버 우클릭 → 앱 → 아이템 선물하기
        # 기존 선물 패널을 그대로 열되 '받을 사람'만 미리 채워둬요. 4단계가 2단계로 줄어듭니다.
        self.gift_menu = app_commands.ContextMenu(name="아이템 선물하기", callback=self.gift_menu_callback)
        self.bot.tree.add_command(self.gift_menu)

    async def cog_load(self):
        self.shop_refresh_loop.start()
        self._register_persistent_views()

    def _register_persistent_views(self):
        """봇을 재시작해도 계속 눌리는 버튼(Persistent View)들을 등록합니다.

        🧩 [이동] 예전엔 이 코드가 mari_client.setup_hook 안에 있었어요. 그래서 클라이언트
        본체가 상점의 내부 구조(매대 목록의 생김새)를 알아야 했고, 상점을
        빼고 납품하면 갈 곳 없는 코드가 남았습니다. 상점 일은 상점이 합니다.
        """
        # 🏪 [다중 매대] 매대가 채널마다 여러 개 있을 수 있으므로, 저장된 모든 매대를
        # 순회하며 각 매대(메시지)마다 별도의 Persistent View를 등록해요.
        try:
            shop_data = self._load_shop()
            registered = 0
            for ch_id_str, board in shop_data.get("boards", {}).items():
                msg_id = board.get("shop_message_id")
                if msg_id:
                    try:
                        self.bot.add_view(ShopPurchaseView(self, int(ch_id_str)), message_id=msg_id)
                        registered += 1
                    except Exception as e:
                        print(f"❗ 매대(채널 {ch_id_str}) Persistent View 등록 실패: {e}")
            print(f"🛒 상점 지속성 버튼(Persistent View) {registered}개 매대 등록 완료!")
        except Exception as e:
            print(f"❗ 상점 매대 목록 로드 실패: {e}")

    def build_wallet_gift_view(self, owner_id: int) -> "WalletGiftView":
        """/지갑 아래에 붙일 '선물하기' 버튼을 만들어 줍니다.

        🧩 지갑(MariEconomy)이 이 버튼을 붙이는데, 선물은 상점 데이터(인벤토리)를 만지는
        일이라 UI도 로직도 여기 있어야 해요. 예전엔 economy.py가 `from cogs.shop import
        WalletGiftView`로 직접 가져다 썼는데, 그러면 **상점을 빼고 납품할 때 지갑까지
        import 단계에서 죽습니다.** 이제 지갑은 get_cog로 이 함수가 있는지만 확인해요.
        """
        return WalletGiftView(self, owner_id)

    async def cog_unload(self):
        self.shop_refresh_loop.cancel()
        self.bot.tree.remove_command(self.gift_menu.name, type=self.gift_menu.type)

    async def gift_menu_callback(self, interaction: discord.Interaction, member: discord.Member):
        """멤버를 우클릭해서 그 사람에게 바로 아이템을 선물합니다."""
        if await feature_gate(interaction, "shop", "상점"):
            return
        if member.bot:
            return await interaction.response.send_message("🤖 봇에게는 선물할 수 없어요!", ephemeral=True)
        if member.id == interaction.user.id:
            return await interaction.response.send_message("💥 자기 자신에게는 선물할 수 없어요!", ephemeral=True)

        giftables = self.collect_giftable_items(interaction.user.id)
        if not giftables:
            return await interaction.response.send_message(
                "🎒 지금 선물할 수 있는 아이템이 없어요.\n"
                "└ 역할이 지급되는 아이템은 선물 대상이 아니에요.",
                ephemeral=True,
            )

        panel = GiftPanelView(self, interaction.user, giftables)
        panel.receiver = member        # ✅ 받을 사람은 이미 정해졌어요 (드롭다운으로 바꿀 수도 있고요)
        await interaction.response.send_message(embed=panel.build_embed(), view=panel, ephemeral=True)
        panel.origin_interaction = interaction

    # ================== 🛡️ 경제 시스템 연동 및 지갑 예외 방어 코드 ==================
    def _load_economy_file(self) -> dict:
        """MariEconomy 지갑 시스템에서 실시간으로 정밀 경제 데이터를 가져옵니다."""
        # 1. 로드된 MariEconomy Cog가 있다면 해당 인스턴스의 로드 함수를 최우선 공유
        economy_cog = self.bot.get_cog("MariEconomy")
        if economy_cog and hasattr(economy_cog, "_load_raw_economy"):
            return economy_cog._load_raw_economy()
        
        # 2. 코그를 못 찾은 예외적인 경우에만 파일을 직접 읽습니다.
        # 🚨 [버그 수정] 예전엔 여기서 json.load를 try/except로 감싸고 실패하면 {}를 돌려줬어요.
        # 지갑 파일이 손상됐을 때 "모두 잔액 0"으로 조용히 넘어간 뒤 그대로 저장되면
        # 서버 전체 재산이 날아갈 수 있어서, safe_json_load로 바꿔 즉시 중단시킵니다.
        return safe_json_load(ECONOMY_FILE, {})

    def _save_economy_file(self, economy_dict: dict):
        """지갑 시스템과 상점 시스템이 동일한 파일에 쓰이도록 동기화하여 저장합니다."""
        # 1. MariEconomy Cog의 저장 함수를 호출하여 실시간 동기화
        economy_cog = self.bot.get_cog("MariEconomy")
        if economy_cog and hasattr(economy_cog, "_save_raw_economy"):
            economy_cog._save_raw_economy(economy_dict)
            return
            
        # 2. 코그를 못 찾은 예외적인 경우에만 파일에 직접 저장합니다.
        atomic_json_save_or_raise(ECONOMY_FILE, economy_dict, indent=4)

    def _get_balance(self, user_id: int) -> int:
        """지갑 스키마에 정의된 단일 숫자 금액을 깨짐 없이 직관적으로 가져옵니다."""
        economy = self._load_economy_file()
        user_data = economy.get(str(user_id), 0)
        
        # 혹시라도 이전 구조의 데이터(딕셔너리)가 잔존할 경우를 대비한 하이브리드 방어
        if isinstance(user_data, dict):
            return user_data.get("money", user_data.get("eva", 0))
        return int(user_data)

    def _set_balance(self, user_id: int, amount: int):
        """차감되거나 획득한 금액을 지갑 데이터 구조에 맞추어 정확하게 업데이트합니다."""
        economy = self._load_economy_file()
        u_str = str(user_id)
        
        # MariEconomy 규격과 완전히 동일하게 단일 정수 값으로 저장 포맷 일치화
        economy[u_str] = amount

        self._save_economy_file(economy)

    def _record_ledger(self, user_id, delta: int, balance_after, kind: str, detail: str = ""):
        """상점 거래를 에바 원장에 남깁니다. (유저가 /지갑 내역으로 확인할 수 있어요)"""
        record_ledger(user_id, delta, balance_after, kind, detail)
    # ==============================================================================

    def _load_shop(self) -> dict:
        """전체 상점 데이터(모든 매대)를 불러옵니다. {"boards": {"<채널ID>": {...매대 하나...}}}"""
        data = safe_json_load(self.SHOP_FILE, None)
        if data is None:
            return self._get_default_data()

        # 🔄 [1회성 마이그레이션] 예전엔 매대가 하나뿐이라 "items"/"inventories"가
        # 파일 최상단에 바로 있었습니다. 이제는 채널마다 독립된 매대를 여러 개 만들 수
        # 있도록 "boards" 딕셔너리 아래로 구조가 바뀌었어요. 기존에 이미 운영 중이던
        # 매대가 있다면, 데이터 유실 없이 그 채널의 매대로 자동 변환해 드립니다.
        if "boards" not in data and ("items" in data or "shop_channel_id" in data):
            old_channel_id = data.get("shop_channel_id")
            migrated_board = {
                "shop_channel_id": old_channel_id,
                "shop_message_id": data.get("shop_message_id"),
                "shop_name": data.get("shop_name", "🛒 상점"),
                "shop_desc": data.get("shop_desc", ""),
                "next_item_id": data.get("next_item_id", 1),
                "items": data.get("items", {}),
                "inventories": data.get("inventories", {}),
            }
            new_data = {"boards": {}}
            if old_channel_id:
                new_data["boards"][str(old_channel_id)] = migrated_board
                print(f"🔄 [마이그레이션] 기존 단일 매대 데이터를 채널 {old_channel_id}의 매대로 이전했어요.")
            data = new_data
            self._save_shop(data)

        if "boards" not in data:
            data["boards"] = {}

        # 💰 [신규 필드 자동 보정] 되팔기퍼센트 값이 아직 없는(예전에 등록된) 아이템은
        # 기본값 0%로 채워둡니다. (0%면 지금까지처럼 되팔아도 돈을 돌려받지 못하는 것과 동일)
        for board in data["boards"].values():
            for info in board.get("items", {}).values():
                if "resale_percent" not in info:
                    info["resale_percent"] = 0

        return data

    def _get_default_data(self) -> dict:
        return {"boards": {}}

    def _get_default_board(self) -> dict:
        return {
            "shop_channel_id": None,
            "shop_message_id": None,
            "shop_name": "🛒 상점",
            "shop_desc": "환영합니다! 모은 에바로 다양한 아이템을 거래해 보세요.",
            "next_item_id": 1,
            "items": {},
            "inventories": {}
        }

    def _get_board(self, data: dict, channel_id) -> Optional[dict]:
        """특정 채널에 매대가 있으면 그 매대 딕셔너리를, 없으면 None을 반환합니다."""
        return data.get("boards", {}).get(str(channel_id))

    def _save_shop(self, data: dict):
        atomic_json_save_or_raise(self.SHOP_FILE, data, indent=4)

    def _get_item_by_name(self, board: dict, name: str) -> tuple:
        """해당 매대(board) 안에서만 이름으로 아이템을 찾습니다 (매대별로 독립적)."""
        if not board:
            return None, None
        for item_id, info in board.get("items", {}).items():
            if info["name"] == name:
                return item_id, info
        return None, None

    # ========== 🎁 [신규] 아이템 선물 ==========
    # UI는 이 파일 위쪽의 GiftPanelView / WalletGiftView가 담당하고,
    # 데이터를 실제로 만지는 건 아래 두 함수뿐입니다. 나중에 `/선물` 같은 슬래시 명령어를
    # 붙이고 싶으면 이 두 함수만 그대로 호출하면 돼요.

    def collect_giftable_items(self, user_id) -> list:
        """유저가 가진 '선물 가능한' 아이템을 모든 매대에서 모아옵니다.

        역할 아이템은 일부러 뺐어요. 역할은 인벤토리(shop.json)가 아니라 디스코드 쪽에
        있어서, 인벤토리만 옮기면 '아이템은 넘어갔는데 역할은 그대로'인 상태가 됩니다.
        (자세한 이유는 이 파일 위쪽 '아이템 선물 시스템' 주석 참고)
        """
        data = self._load_shop()
        user_key = str(user_id)
        result = []

        for channel_id, board in data.get("boards", {}).items():
            shop_items = board.get("items", {})
            for item_id, qty in board.get("inventories", {}).get(user_key, {}).items():
                # 판단 기준을 /지갑 목록과 똑같이 맞춥니다.
                # (매대에서 삭제된 항목은 /지갑에도 안 보이니 선물 목록에도 띄우지 않아요)
                info = shop_items.get(item_id)
                if qty <= 0 or not info or info.get("is_role"):
                    continue
                result.append({
                    "channel_id": channel_id,
                    "item_id": item_id,
                    "name": info.get("name", "알 수 없는 아이템"),
                    "board_name": board.get("shop_name", "상점"),
                    "qty": qty,
                })

        result.sort(key=lambda x: (x["board_name"], x["name"]))
        return result

    def apply_gift(self, giver_id, receiver_id, channel_id, item_id: str, amount: int) -> tuple:
        """아이템을 실제로 옮깁니다. (오류메세지, 결과dict) 중 한쪽만 채워서 돌려줘요.

        ⚠️ 이 함수 안에는 await이 하나도 없습니다. shop.json에는 전원의 인벤토리가 같이
        들어 있어서, 읽고 → 고치고 → 저장하는 사이에 await이 끼면 그 틈에 일어난 다른
        사람의 구매가 통째로 되돌아가요. (이 코그 상단 '데이터 안전 규칙' 참고)
        덕분에 주는 쪽 차감과 받는 쪽 추가가 저장 한 번으로 같이 끝나서,
        '한쪽만 반영된' 상태가 아예 생기지 않습니다.
        """
        if amount <= 0:
            return "❌ 선물 수량은 1개 이상이어야 해요.", None

        data = self._load_shop()
        board = self._get_board(data, channel_id)
        if not board:
            return "❌ 그 아이템이 있던 매대가 사라졌어요.", None

        item_info = board.get("items", {}).get(item_id)
        if not item_info:
            return "❌ 그 아이템은 매대에서 삭제됐어요.", None
        if item_info.get("is_role"):
            # 목록에서 이미 걸러지지만, 고르는 사이에 관리자가 역할 아이템으로 바꿨을 수도 있어요.
            return ("❌ 역할이 지급되는 아이템은 선물할 수 없어요.\n"
                    "└ 되팔기로 정리하신 뒤, 받으실 분이 직접 구매하도록 안내해 주세요.", None)

        inventories = board.setdefault("inventories", {})
        giver_inv = inventories.setdefault(str(giver_id), {})

        have = giver_inv.get(item_id, 0)
        if have < amount:
            return (f"❌ 보유 수량이 부족해요. (지금 {have}개 / 보내려는 수량 {amount}개)\n"
                    f"└ 지갑을 띄워둔 사이에 되팔았거나 사용했을 수 있어요.", None)

        giver_inv[item_id] = have - amount
        if giver_inv[item_id] <= 0:
            del giver_inv[item_id]      # 0개가 된 칸은 지워서 인벤토리를 깔끔하게 유지

        receiver_inv = inventories.setdefault(str(receiver_id), {})
        receiver_inv[item_id] = receiver_inv.get(item_id, 0) + amount

        self._save_shop(data)

        return None, {
            "name": item_info["name"],
            "board_name": board.get("shop_name", "상점"),
            "remain": giver_inv.get(item_id, 0),
            "receiver_total": receiver_inv[item_id],
        }

    # ========== 🎟️ [신규] 이름으로 아이템 찾아 차감/복구 (다른 코그에서 사용) ==========
    # 다른 기능이 "이 아이템 몇 개 까줘"라고 부탁할 때 쓰는 공용 함수예요.
    # 🏪 아이템은 매대마다 따로 저장돼서 같은 이름이 여러 매대에 흩어져 있을 수 있어요.
    #    그래서 전 매대를 돌며 합계를 세고, **모자라면 한 개도 건드리지 않고** 실패시킵니다.

    def find_items_by_name(self, user_id, keyword: str, *, exact: str = None) -> list:
        """유저가 가진 아이템 중 이름이 맞는 것들을 매대별로 모아옵니다.

        정확한 이름(exact)으로 먼저 찾고, 그게 하나도 없을 때만 keyword가 이름에 들어간 걸로
        넓혀서 찾아요. (관리자가 상품명 앞뒤에 이모지나 수식어를 붙여 등록하는 일이 잦아서,
        `/고확`이 '확성기'를 찾을 때 쓰는 것과 같은 방식이에요)
        """
        data = self._load_shop()
        user_key = str(user_id)
        exact_hits, loose_hits = [], []

        for channel_id, board in data.get("boards", {}).items():
            items = board.get("items", {})
            for item_id, qty in board.get("inventories", {}).get(user_key, {}).items():
                info = items.get(item_id)
                if qty <= 0 or not info:
                    continue
                name = info.get("name", "")
                hit = {"channel_id": channel_id, "item_id": item_id, "name": name,
                       "qty": qty, "board_name": board.get("shop_name", "상점")}
                if exact and name == exact:
                    exact_hits.append(hit)
                elif keyword and keyword in name:
                    loose_hits.append(hit)

        return exact_hits or loose_hits

    def consume_items_by_name(self, user_id, keyword: str, amount: int, *, exact: str = None) -> tuple:
        """이름이 맞는 아이템을 amount개 차감합니다. (오류dict, 차감내역리스트) 중 한쪽만 채워서 돌려줘요.

        오류dict는 {"kind": "not_enough", "have": 보유수량, "name": 찾은이름} 형태예요.
        호출한 쪽이 상황에 맞는 문구를 만들 수 있게 메세지 대신 값으로 돌려줍니다.

        ⚠️ apply_gift와 같은 이유로 이 함수 안에는 await이 하나도 없습니다.
        (shop.json에 전원 인벤토리가 같이 들어 있어서, 저장 사이에 await이 끼면 그 틈의 구매가 롤백됨)
        """
        if amount <= 0:
            return {"kind": "bad_amount", "have": 0, "name": exact or keyword}, []

        hits = self.find_items_by_name(user_id, keyword, exact=exact)
        have = sum(h["qty"] for h in hits)
        if have < amount:
            return {"kind": "not_enough", "have": have,
                    "name": hits[0]["name"] if hits else (exact or keyword)}, []

        # 여기서부터는 반드시 성공해요. 매대를 돌며 필요한 만큼만 순서대로 깎습니다.
        data = self._load_shop()
        remaining = amount
        taken = []

        for hit in hits:
            if remaining <= 0:
                break
            board = self._get_board(data, hit["channel_id"])
            if not board:
                continue
            inv = board.get("inventories", {}).get(str(user_id), {})
            owned = inv.get(hit["item_id"], 0)
            if owned <= 0:
                continue

            take = min(owned, remaining)
            inv[hit["item_id"]] = owned - take
            if inv[hit["item_id"]] <= 0:
                del inv[hit["item_id"]]      # 0개가 된 칸은 지워서 인벤토리를 깔끔하게 유지
            remaining -= take
            taken.append({"channel_id": hit["channel_id"], "item_id": hit["item_id"],
                          "name": hit["name"], "count": take})

        if remaining > 0:
            # find_items_by_name과 실제 데이터가 어긋난 경우 (정상적으론 오지 않아요).
            # 저장하지 않고 빠져나가면 아무것도 안 깎인 상태 그대로예요.
            return {"kind": "not_enough", "have": amount - remaining,
                    "name": exact or keyword}, []

        self._save_shop(data)
        return None, taken

    def restore_items(self, user_id, taken: list):
        """consume_items_by_name으로 깎은 아이템을 되돌립니다. (뒤 단계가 실패했을 때 호출)

        `/고확`이 방송 전송에 실패하면 확성기를 되돌려주는 것과 같은 역할이에요.
        """
        if not taken:
            return
        data = self._load_shop()
        user_key = str(user_id)

        for row in taken:
            board = self._get_board(data, row["channel_id"])
            if not board:
                continue    # 그새 매대가 사라졌으면 되돌릴 곳이 없어요
            inv = board.setdefault("inventories", {}).setdefault(user_key, {})
            inv[row["item_id"]] = inv.get(row["item_id"], 0) + row["count"]

        self._save_shop(data)

    async def log_gift(self, giver: discord.Member, receiver: discord.Member, result: dict, amount: int):
        """선물 기록을 상점 로그 채널에 남깁니다. (아이템만 오가므로 에바 원장에는 남기지 않아요)"""
        await send_log_embed(
            self.bot, "shop_log", f"{giver.mention}님이 아이템을 선물했어요.",
            fields=[
                ("매대", result["board_name"], True),
                ("보낸 사람", giver.mention, True),
                ("받은 사람", receiver.mention, True),
                ("아이템", result["name"], True),
                ("수량", f"{amount:,} 개", True),
                ("보낸 사람 잔여", f"{result['remain']:,} 개", True),
            ],
        )

    def _generate_shop_embed(self, board: dict) -> discord.Embed:
        embed = discord.Embed(
            title=board.get("shop_name", "🛒 상점"),
            description=f"{board.get('shop_desc', '')}\n\n⚠️ **주의사항**\n└ *이 게시판 하단의 메뉴를 통해서만 구매/되팔기가 가능합니다.*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            color=0x5ce6b4
        )
        items = board.get("items", {})
        if not items:
            embed.add_field(name="텅 빈 매대", value="현재 판매 중인 상품이 없어요.", inline=False)
        else:
            # 🛡️ [안전망] ShopItemSelect와 같은 이유로 잘라냅니다. 임베드 필드가 25개를
            # 넘으면 전광판 메시지 전송 자체가 400 에러로 실패해요.
            shown = list(items.items())[:MAX_BOARD_ITEMS]
            for item_id, info in shown:
                status = []
                if info.get("can_buy", True): status.append("🟢 구매가능")
                if info.get("can_sell", True): status.append(f"🔵 되팔기가능({info.get('resale_percent', 0)}%)")
                if not status: status.append("❌ 거래불가")

                type_str = f"역할 지급 (<@&{info['role_id']}>)" if info["is_role"] else "일반 아이템"
                field_value = f"💵 **가격**: {info['price']:,} 에바\n📝 **설명**: {info['desc']}\n🏷️ **타입**: {type_str}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                embed.add_field(name=f"📦 {info['name']} ({', '.join(status)})", value=field_value, inline=False)

        hidden = len(items) - MAX_BOARD_ITEMS
        if hidden > 0:
            # 상한이 생기기 전에 쌓인 매대라면 관리자가 정리할 수 있게 사실을 알려줍니다.
            embed.set_footer(
                text=f"⚠️ 상품이 {len(items)}개라 {MAX_BOARD_ITEMS}개만 보여요 "
                     f"(숨은 상품 {hidden}개) · /상점 항목삭제로 정리해 주세요"
            )
        else:
            embed.set_footer(text=f"✨ 마지막 업데이트: {dt.datetime.now(KST).strftime('%H:%M:%S')}")
        return embed

    @tasks.loop(hours=1)
    async def shop_refresh_loop(self):
        """모든 매대 메시지를 최신 상태로 다시 그립니다. (봇 시작 직후 1회, 이후 1시간마다)

        예전엔 봇 시작용(init_shop_board)과 1시간짜리 수동 while 루프가 따로 있었는데,
        하는 일이 같아서 tasks.loop 하나로 합쳤어요. 덕분에 다른 루프들처럼 예외로 멈추면
        아래 error 핸들러가 관리자에게 알리고 다시 살려줍니다.
        """
        data = self._load_shop()
        for channel_id_str in list(data.get("boards", {}).keys()):
            try:
                # 🐛 [성능 버그 수정] 예전엔 채널마다 _refresh_board_message가 shop.json을
                # 매번 다시 읽어서, 매대가 N개면 파일을 N번 읽었어요. 위에서 이미 읽어둔
                # data를 그대로 넘겨서 한 번만 읽도록 했어요.
                # (저장은 _refresh_board_message가 직전에 파일을 다시 읽어서 하므로 안전해요)
                await self._refresh_board_message(int(channel_id_str), preloaded_data=data)
            except Exception as e:
                print(f"❗ 매대(채널 {channel_id_str}) 갱신 실패: {e}")

    @shop_refresh_loop.before_loop
    async def before_shop_refresh_loop(self):
        await self.bot.wait_until_ready()

    @shop_refresh_loop.error
    async def shop_refresh_loop_error(self, error: BaseException):
        await report_loop_error(self.shop_refresh_loop, "상점 매대 갱신", error)

    async def _refresh_board_message(self, channel_id: int, preloaded_data: Optional[dict] = None):
        """해당 채널(매대)의 게시판 메시지를 최신 상태로 새로 그립니다. (신규 생성/기존 수정 모두 처리)
        preloaded_data를 넘기면 다시 파일을 읽지 않고 그걸 그대로 써요. (여러 매대를 한꺼번에
        돌리는 반복문에서, 매대 개수만큼 파일을 반복해서 읽지 않도록 하기 위한 용도예요)"""
        data = preloaded_data if preloaded_data is not None else self._load_shop()
        board = self._get_board(data, channel_id)
        if not board:
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        embed = self._generate_shop_embed(board)
        view = ShopPurchaseView(self, channel_id)
        msg_id = board.get("shop_message_id")

        if msg_id:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(embed=embed, view=view)
                return
            except Exception:
                pass

        new_msg = await channel.send(embed=embed, view=view)
        try: await new_msg.pin()
        except Exception: pass

        # 🚨 [심각한 버그 수정] 예전엔 위에서 읽어둔 `data`를 통째로 저장했어요.
        # 그 사이에 fetch_message/send/pin 같은 네트워크 통신이 끼어 있고, preloaded_data로
        # 넘어온 경우엔 몇 개 매대를 도는 동안 더 오래된 상태일 수도 있어요. 그 시간 동안
        # 누가 물건을 사면, 차감된 인벤토리·재고가 옛날 상태로 되돌아가 버립니다.
        # (shop.json 하나에 매대 메세지 ID와 전원의 인벤토리가 같이 들어있어서 생긴 문제예요)
        # 이제 저장 직전에 파일을 새로 읽어서, 이 매대의 메세지 ID 한 줄만 갱신합니다.
        board["shop_message_id"] = new_msg.id      # 호출부가 들고 있는 사본도 최신으로
        fresh = self._load_shop()
        fresh_board = self._get_board(fresh, channel_id)
        if fresh_board is None:
            return      # 그새 매대가 삭제됐다면 되살리지 않아요
        fresh_board["shop_message_id"] = new_msg.id
        self._save_shop(fresh)

    async def update_shop_board(self, channel_id: int):
        """특정 매대(channel_id) 하나만 갱신합니다. (항목 추가/삭제/수정 직후 호출)"""
        await self._refresh_board_message(channel_id)

    async def log_purchase(self, member: discord.Member, item_name: str, amount: int, total_cost: int, current_balance: int):
        cost_str = f"{total_cost:,} 에바" if total_cost > 0 else f"환불: {abs(total_cost):,} 에바"
        await send_log_embed(
            self.bot, "shop_log", f"{member.mention}님의 상점 거래 영수증이에요.",
            fields=[
                ("이용자", member.mention, True),
                ("거래 항목", item_name, True),
                ("수량", f"{amount:,} 개", True),
                ("사용 금액", cost_str, True),
                ("최종 잔액", f"{current_balance:,} 에바", True),
            ],
        )

    # ========== 💰 [신규] /정산용 거래 내역 기록 ==========
    # 🧹 파일이 무한정 커지지 않도록 오래된 것부터 잘라냅니다. (에바 원장의 LEDGER_MAX_ENTRIES와 같은 취지)
    # 이 기록을 읽는 곳은 /상점 정산 하나뿐이고 월 단위로만 보기 때문에, 최근 몇 달치만 있으면 충분해요.
    TRANSACTIONS_MAX_ENTRIES = 5000

    def _load_transactions(self) -> list:
        return safe_json_load(self.SHOP_TRANSACTIONS_FILE, {"transactions": []}).get("transactions", [])

    def _record_transaction(self, channel_id: int, item_name: str, tx_type: str, amount: int):
        """구매/되팔기 한 건을 기록합니다. tx_type: 'buy'(구매) 또는 'sell'(되팔기).

        🚨 이 함수는 예외를 밖으로 던지지 않아요.
        이미 돈과 아이템이 오간 **뒤에** 호출되기 때문에, 여기서 실패가 터지면 유저에게는
        "구매 실패"로 보이는데 실제로는 결제가 끝난 최악의 상황이 됩니다. 정산 기록이 한 건
        빠지는 건 아쉽지만 거래 자체를 깨뜨리는 것보다는 나아요. (mari_state.record_ledger와 같은 판단)
        """
        try:
            transactions = self._load_transactions()
            transactions.append({
                "channel_id": channel_id,
                "item_name": item_name,
                "type": tx_type,
                "amount": amount,
                "timestamp": dt.datetime.now(KST).isoformat(),
            })
            # 🐛 [버그 수정] 예전엔 상한이 없어서 거래가 쌓이는 만큼 파일이 계속 커졌어요.
            # 게다가 거래 한 건마다 전체 목록을 통째로 다시 쓰기 때문에, 오래 운영할수록
            # 구매 한 번에 드는 디스크 쓰기가 같이 무거워지는 구조였습니다.
            if len(transactions) > self.TRANSACTIONS_MAX_ENTRIES:
                transactions = transactions[-self.TRANSACTIONS_MAX_ENTRIES:]
            atomic_json_save_or_raise(self.SHOP_TRANSACTIONS_FILE, {"transactions": transactions}, indent=2)
        except Exception as e:
            print(f"⚠️ 상점 거래내역 기록 실패 (거래 자체는 정상 처리됨): {type(e).__name__}: {e}")

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        return has_admin_or_role(interaction, "shop_admin")

    # --- 슬래시 명령어 그룹 선언 (/상점) ---
    shop_group = app_commands.Group(name="상점", description="상점 기능 제어 및 관리자 전용 명령어")

    @shop_group.command(name="생성", description="[관리자] 현재 채널에 새로운 상점 매대 전광판을 만듭니다. (채널마다 독립적으로 여러 개 개설 가능)")
    @app_commands.guild_only()
    async def create_shop(self, interaction: discord.Interaction, 이름: str, 설명: str):
        if not self._is_admin(interaction): return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)
        data = self._load_shop()

        # 🏪 [다중 매대] 이 채널에 이미 매대가 있다면 새로 만들지 않고 안내
        if self._get_board(data, interaction.channel_id):
            return await interaction.response.send_message("❌ 이 채널엔 이미 매대가 있어요. 다른 채널에서 새 매대를 만들거나, `/상점 설정`으로 지금 매대를 수정해 주세요.", ephemeral=True)

        board = self._get_default_board()
        board["shop_name"] = 이름
        board["shop_desc"] = 설명
        board["shop_channel_id"] = interaction.channel_id
        data["boards"][str(interaction.channel_id)] = board
        self._save_shop(data)
        await interaction.response.send_message("✅ 이 채널에 새 매대를 만들었어요.", ephemeral=True)
        await self.update_shop_board(interaction.channel_id)

    @shop_group.command(name="삭제", description="[관리자] 현재 채널에 있는 상점 매대만 삭제합니다. (다른 채널의 매대는 영향 없음)")
    @app_commands.guild_only()
    async def delete_shop(self, interaction: discord.Interaction):
        if not self._is_admin(interaction): return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)

        # ⏱️ [버그 수정] 예전엔 defer 없이 진행했어요. 그런데 첫 응답을 내기 전에 아래에서
        # fetch_message·delete 두 번의 네트워크 왕복을 하고, 거기에 economy_lock 대기까지
        # 겹칩니다. 마침 구매가 한 건 진행 중이면(그게 레이트리밋에 걸리면 몇 초) 3초 시한을
        # 넘겨서, **매대는 지워졌는데 화면엔 "상호작용 실패"만 뜨는** 상태가 됐어요.
        # 먼저 defer로 시한을 벗어나고, 안내는 전부 followup으로 보냅니다.
        await interaction.response.defer(ephemeral=True)

        data = self._load_shop()
        board = self._get_board(data, interaction.channel_id)
        if not board:
            return await interaction.followup.send("❌ 이 채널엔 매대가 없어요.", ephemeral=True)

        # 게시판 메시지도 같이 정리 (실패해도 무시하고 진행)
        msg_id = board.get("shop_message_id")
        if msg_id:
            try:
                msg = await interaction.channel.fetch_message(msg_id)
                await msg.delete()
            except Exception:
                pass

        # 🚨 [심각한 버그 수정] 예전엔 맨 위에서 읽어둔 `data`를 그대로 저장했어요. 바로 위
        # fetch_message·delete 두 번의 네트워크 통신을 기다리는 동안 누가 물건을 사거나 선물하면,
        # shop.json에는 **전 매대의 인벤토리가 같이** 들어 있어서 그 거래가 통째로 되돌아갑니다.
        # 지우려는 매대뿐 아니라 **다른 채널 매대의 거래까지** 같이 날아가요.
        # (_do_buy와 _refresh_board_message에서 이미 같은 이유로 고친 문제인데 여기만 남아 있었어요)
        # 이제 저장 직전에 파일을 다시 읽어서, 지울 매대 한 칸만 걷어냅니다.
        #
        # 🚦 [성능 버그 수정] 안내 메세지는 락을 **놓은 뒤에** 보내요. economy_lock은 구매·송금·
        # 출석·주식·캠프통장이 전부 지나가는 서버 공용 관문이라, 락을 쥔 채로 디스코드와 통신하면
        # 그동안 서버의 모든 돈 관련 기능이 줄을 섭니다. (송금·주식에서 같은 이유로 이미 고친 패턴)
        gone = False
        async with self.bot.economy_lock:
            fresh = self._load_shop()
            if fresh.get("boards", {}).pop(str(interaction.channel_id), None) is None:
                gone = True
            else:
                self._save_shop(fresh)

        if gone:
            return await interaction.followup.send(
                "❌ 그 사이에 이 채널의 매대가 이미 사라졌어요.", ephemeral=True
            )
        await interaction.followup.send("🗑️ 이 채널의 매대를 삭제했어요. (다른 채널의 매대는 그대로예요)", ephemeral=True)

    @shop_group.command(name="설정", description="[관리자] 현재 채널에 있는 매대의 타이틀 이름과 설명을 수정합니다.")
    @app_commands.guild_only()
    async def edit_shop(self, interaction: discord.Interaction, 이름: str, 설명: str):
        if not self._is_admin(interaction): return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)
        data = self._load_shop()
        board = self._get_board(data, interaction.channel_id)
        if not board: return await interaction.response.send_message("❌ 이 채널엔 매대가 없어요. 먼저 `/상점 생성`으로 만들어 주세요.", ephemeral=True)
        board["shop_name"] = 이름
        board["shop_desc"] = 설명
        self._save_shop(data)
        # ⏱️ [버그 수정] 응답을 **먼저** 보냅니다. 전광판 갱신(update_shop_board)은 메세지
        # 조회+수정이라 네트워크 왕복이 두 번 들어가는데, 그걸 먼저 하면 디스코드의 3초 응답
        # 시한을 넘겨 인터랙션이 만료돼요. 저장은 이미 끝났는데 화면엔 "응답하지 않았습니다"만
        # 뜨는 상태가 됩니다. (`/상점 생성`이 원래부터 이 순서였어요)
        await interaction.response.send_message("✅ 상점 매대 디자인이 수정됐어요.", ephemeral=True)
        await self.update_shop_board(interaction.channel_id)

    @shop_group.command(name="항목추가", description="[관리자] 현재 채널 매대에 새 물품을 진열합니다 (이름 기반으로 처리).")
    @app_commands.guild_only()
    @app_commands.describe(되팔기퍼센트="되팔기 시 원가의 몇 %를 돌려줄지 (0~100, 판매가능일 때만 사용, 생략 시 0)")
    async def add_item(self, interaction: discord.Interaction, 이름: str, 가격: int, 설명: str, 구매가능: bool, 판매가능: bool, 역할지급: Optional[discord.Role] = None, 되팔기퍼센트: Optional[int] = None):
        if not self._is_admin(interaction): return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)
        # 🚨 [버그 수정] 여기만 가격 검증이 빠져 있었어요. (`/상점 항목설정`은 이미 막고 있었습니다)
        # 가격이 음수면 _do_buy의 잔액 검사(`current_balance < price`)를 무조건 통과하고,
        # 차감식이 `잔액 - (-1000)`이 되면서 **살 때마다 에바가 늘어납니다.** 할인 의도로 `-`를
        # 붙이는 오타 한 번이면 나는 사고라, 등록하는 자리에서 막습니다.
        if 가격 < 0:
            return await interaction.response.send_message(
                "❌ 가격은 0 에바 이상이어야 해요. (음수로 등록하면 구매할 때마다 에바가 늘어나요)",
                ephemeral=True,
            )
        if 되팔기퍼센트 is not None and not (0 <= 되팔기퍼센트 <= 100):
            return await interaction.response.send_message("❌ 되팔기퍼센트는 0~100 사이여야 해요.", ephemeral=True)
        data = self._load_shop()
        board = self._get_board(data, interaction.channel_id)
        if not board: return await interaction.response.send_message("❌ 이 채널엔 매대가 없어요. 먼저 `/상점 생성`으로 만들어 주세요.", ephemeral=True)

        existing_id, _ = self._get_item_by_name(board, 이름)
        if existing_id: return await interaction.response.send_message("❌ 이미 이 매대에 같은 이름의 상품이 있어요.", ephemeral=True)

        # 🚧 26개째부터는 전광판이 통째로 안 그려져요. (위 MAX_BOARD_ITEMS 주석 참고)
        current_count = len(board.get("items", {}))
        if current_count >= MAX_BOARD_ITEMS:
            return await interaction.response.send_message(
                f"❌ 한 매대에는 상품을 **{MAX_BOARD_ITEMS}개까지만** 둘 수 있어요. "
                f"(지금 {current_count}개)\n"
                f"└ 디스코드가 임베드 필드와 드롭다운 항목을 각각 {MAX_BOARD_ITEMS}개로 제한해서, "
                f"넘기면 전광판이 아예 안 그려지고 구매 버튼도 죽어요.\n"
                f"└ 안 쓰는 상품을 `/상점 항목삭제`로 정리하거나, 다른 채널에 `/상점 생성`으로 매대를 하나 더 만들어 주세요.",
                ephemeral=True,
            )

        item_id = str(board["next_item_id"])
        board["next_item_id"] += 1
        board["items"][item_id] = {
            "name": 이름, "price": 가격, "desc": 설명,
            "is_role": 역할지급 is not None, "role_id": 역할지급.id if 역할지급 else None,
            "can_buy": 구매가능, "can_sell": 판매가능,
            "resale_percent": 되팔기퍼센트 if 되팔기퍼센트 is not None else 0,  # 💰 [신규] 되팔기 시 돌려받는 비율(%)
        }
        self._save_shop(data)
        # ⏱️ 응답 먼저, 전광판 갱신은 그 다음. (이유는 `/상점 설정` 쪽 주석 참고)
        await interaction.response.send_message(f"✅ `{이름}` 항목이 이 매대에 추가됐어요.", ephemeral=True)
        await self.update_shop_board(interaction.channel_id)

    # 🔍 [신규] 현재 채널(매대)에 등록된 상품 이름을 힌트로 띄워주는 공용 자동완성 함수
    async def _item_name_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        data = self._load_shop()
        board = self._get_board(data, interaction.channel_id)
        if not board:
            return []
        choices = []
        for info in board.get("items", {}).values():
            name = info.get("name", "")
            if current.lower() in name.lower():
                choices.append(app_commands.Choice(name=f"{name} ({info.get('price', 0):,} 에바)", value=name))
        return choices[:25]

    @shop_group.command(name="항목삭제", description="[관리자] 현재 채널 매대의 진열 물품을 이름 기반으로 식별해 삭제합니다.")
    @app_commands.guild_only()
    async def remove_item(self, interaction: discord.Interaction, 이름: str):
        if not self._is_admin(interaction): return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)
        data = self._load_shop()
        board = self._get_board(data, interaction.channel_id)
        if not board: return await interaction.response.send_message("❌ 이 채널엔 매대가 없어요.", ephemeral=True)

        item_id, _ = self._get_item_by_name(board, 이름)
        if not item_id: return await interaction.response.send_message("❌ 이 매대에서 해당 이름의 상품을 찾을 수 없어요.", ephemeral=True)

        del board["items"][item_id]
        self._save_shop(data)
        # ⏱️ 응답 먼저, 전광판 갱신은 그 다음. (이유는 `/상점 설정` 쪽 주석 참고)
        await interaction.response.send_message(f"🗑️ `{이름}` 항목을 이 매대에서 폐기했어요.", ephemeral=True)
        await self.update_shop_board(interaction.channel_id)

    @remove_item.autocomplete('이름')
    async def remove_item_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._item_name_autocomplete(interaction, current)

    @shop_group.command(name="항목설정", description="[관리자] 현재 채널 매대에 있는 상품의 옵션(새이름/새가격/변동률/설명/상태/되팔기퍼센트 등)을 일괄 수정합니다.")
    @app_commands.guild_only()
    @app_commands.describe(새이름="상품 이름을 바꾸고 싶을 때 입력 (선택)")
    async def edit_item(self, interaction: discord.Interaction, 이름: str, 새이름: Optional[str] = None, 새가격: Optional[int] = None, 퍼센트변동: Optional[float] = None, 설명: Optional[str] = None, 구매가능: Optional[bool] = None, 판매가능: Optional[bool] = None, 되팔기퍼센트: Optional[int] = None):
        if not self._is_admin(interaction): return await interaction.response.send_message("⛔ 권한이 없어요.", ephemeral=True)
        if 되팔기퍼센트 is not None and not (0 <= 되팔기퍼센트 <= 100):
            return await interaction.response.send_message("❌ 되팔기퍼센트는 0~100 사이여야 해요.", ephemeral=True)
        data = self._load_shop()
        board = self._get_board(data, interaction.channel_id)
        if not board: return await interaction.response.send_message("❌ 이 채널엔 매대가 없어요.", ephemeral=True)

        item_id, _ = self._get_item_by_name(board, 이름)
        if not item_id: return await interaction.response.send_message("❌ 이 매대에서 대상을 찾을 수 없어요.", ephemeral=True)

        info = board["items"][item_id]
        logs = []

        if 새이름 is not None and 새이름 != info["name"]:
            existing_id, _ = self._get_item_by_name(board, 새이름)
            if existing_id: return await interaction.response.send_message(f"❌ 이미 이 매대에 `{새이름}`이라는 이름의 상품이 있어요.", ephemeral=True)
            logs.append(f"이름: {info['name']} ➡️ {새이름}")
            info["name"] = 새이름

        if 새가격 is not None:
            if 새가격 < 0: return await interaction.response.send_message("가격은 0 에바 이상이어야 합니다.", ephemeral=True)
            logs.append(f"가격: {info['price']:,} ➡️ {새가격:,} 에바")
            info["price"] = 새가격
        elif 퍼센트변동 is not None:
            old = info["price"]
            calc = int(old * (1 + (퍼센트변동 / 100)))
            if calc < 0: calc = 0
            logs.append(f"비율변동({퍼센트변동:+.1f}%): {old:,} ➡️ {calc:,} 에바")
            info["price"] = calc

        if 설명 is not None: info["desc"] = 설명; logs.append("설명 수정")
        if 구매가능 is not None: info["can_buy"] = 구매가능; logs.append(f"구매여부: {구매가능}")
        if 판매가능 is not None: info["can_sell"] = 판매가능; logs.append(f"판매여부: {판매가능}")
        if 되팔기퍼센트 is not None:
            # 🐛 예전엔 값을 먼저 대입한 뒤에 찍어서, 다른 항목들과 달리 새 값만 두 번 나오고
            #    변경 전 값이 보이지 않았어요. 대입보다 먼저 남겨서 'A ➡️ B' 형식을 맞춥니다.
            logs.append(f"되팔기퍼센트: {info.get('resale_percent', 0)}% ➡️ {되팔기퍼센트}% "
                        f"(원가의 {되팔기퍼센트}%를 돌려줌)")
            info["resale_percent"] = 되팔기퍼센트

        if not logs: return await interaction.response.send_message("❌ 변경할 내용이 입력되지 않았어요.", ephemeral=True)
        self._save_shop(data)
        # ⏱️ 응답 먼저, 전광판 갱신은 그 다음. (이유는 `/상점 설정` 쪽 주석 참고)
        await interaction.response.send_message(f"📢 **[{info['name']} 상품 정보 업데이트 완료]**\n" + "\n".join(f"└ {l}" for l in logs), ephemeral=True)
        await self.update_shop_board(interaction.channel_id)

    @edit_item.autocomplete('이름')
    async def edit_item_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._item_name_autocomplete(interaction, current)

    # 🔎 `/상점 사용`이 "이 아이템이 어느 매대 것인지"를 찾아내는 부분이에요.
    # 예전엔 매대 채널에서만 쓸 수 있어서 "이 채널의 매대"만 보면 됐지만, 이제 아무 채널에서나
    # 쓸 수 있어서 전 매대를 뒤져야 합니다. 자동완성으로 고르면 "<채널ID>:<인벤토리 키>" 형태로
    # 넘어와 매대까지 딱 정해지고, 관리자가 이름을 직접 타이핑했으면 이름으로 전 매대를 훑어요.
    def _find_use_targets(self, data: dict, user_id_str: str, 아이템이름: str) -> list:
        boards = data.get("boards", {})

        picked_channel_id, picked_key = None, None
        if ":" in 아이템이름:
            head, _, tail = 아이템이름.partition(":")
            # 앞부분이 실제로 존재하는 매대 채널일 때만 '자동완성으로 고른 값'으로 취급해요.
            # (이름 안에 ':'가 들어간 상품을 직접 입력한 경우와 헷갈리지 않게)
            if tail and head in boards:
                picked_channel_id, picked_key = head, tail

        results = []
        for channel_id, board in boards.items():
            if picked_channel_id and channel_id != picked_channel_id:
                continue
            items = board.get("items", {})
            for item_key, qty in board.get("inventories", {}).get(user_id_str, {}).items():
                # 🛡️ 0개짜리 칸은 없는 것으로 봅니다. (/지갑·자동완성과 같은 기준)
                if not isinstance(qty, int) or qty <= 0:
                    continue

                info = items.get(item_key)
                name = info.get("name", item_key) if info else item_key

                if picked_key:
                    if item_key != picked_key:
                        continue
                # 인벤토리 키가 아이템 ID일 수도, (예전 방식대로) 이름 그대로일 수도 있어서 둘 다 봐요.
                elif 아이템이름 not in (name, item_key):
                    continue

                results.append({
                    "board": board,
                    "channel_id": channel_id,
                    "board_name": board.get("shop_name", "상점"),
                    "item_key": item_key,
                    "name": name if info else f"(삭제된 상품 #{item_key})",
                    "qty": qty,
                })
        return results

    @shop_group.command(name="사용", description="[관리자] 유저의 인벤토리에서 아이템을 확인 후 차감(사용) 처리합니다. (아무 채널에서나 가능)")
    @app_commands.guild_only()
    async def use_item(self, interaction: discord.Interaction, 유저: discord.Member, 아이템이름: str, 수량: int = 1):
        # ⏱️ [버그 수정] 디스코드는 첫 응답을 3초 안에 요구해요. 그런데 아래에서 전광판 갱신
        # (메세지 조회+수정)과 로그 전송까지 다 마친 **뒤에** 응답하고 있어서, 그게 조금만
        # 느려지면 인터랙션이 만료돼 "응답하지 않았습니다"로 끝났습니다. 차감은 이미 끝난
        # 뒤라 관리자는 처리가 된 건지 아닌지 알 수 없었어요.
        # 먼저 defer로 시간을 벌어두고, 실제 응답은 followup으로 보냅니다.
        await interaction.response.defer(ephemeral=True)

        if not self._is_admin(interaction):
            return await interaction.followup.send("⛔ 권한이 없어요.", ephemeral=True)

        if 수량 <= 0:
            return await interaction.followup.send("❌ 사용 수량은 1개 이상이어야 합니다.", ephemeral=True)

        user_id_str = str(유저.id)

        # 🔒 shop.json을 건드리는 코드는 전부 economy_lock 하나를 지나가야 해요.
        #    (다른 잠금을 쓰면 서로를 막지 못해서 차감이 되돌아갑니다 — games.py 상단 주석 참고)
        #    ⚠️ 이 블록 안에는 await이 하나도 없습니다. 읽고→고치고→저장이 통째로 끝나야
        #       그 사이에 다른 거래가 끼어들지 못해요.
        error, target, remains = None, None, 0
        async with self.bot.economy_lock:
            data = self._load_shop()

            # 🏪 [다중 매대] 아이템은 매대에 종속되지만, 명령어는 아무 채널에서나 쓸 수 있어요.
            # 그래서 전 매대를 돌며 "이 유저가 실제로 갖고 있는" 후보만 모읍니다.
            candidates = self._find_use_targets(data, user_id_str, 아이템이름)

            if not candidates:
                error = (f"❌ {유저.mention}님의 인벤토리에서 `{아이템이름}`을(를) 찾지 못했어요.\n"
                         f"└ 아이템이름 칸의 자동완성 목록에서 골라 주세요.")
            elif len(candidates) > 1:
                # 같은 이름의 상품이 여러 매대에 등록돼 있으면 어느 쪽을 깎을지 알 수 없어요.
                # 마음대로 고르면 엉뚱한 매대 물건이 사라지니, 자동완성으로 매대까지 고르게 안내합니다.
                lines = "\n".join(
                    f"└ {c['board_name']} (<#{c['channel_id']}>) · 보유 {c['qty']:,}개" for c in candidates
                )
                error = f"❓ `{아이템이름}`이(가) 여러 매대에 있어요. 자동완성 목록에서 골라 주세요.\n{lines}"
            elif candidates[0]["qty"] < 수량:
                # 보유 수량 검증
                target = candidates[0]
                error = (f"❌ {유저.mention}님은 `{target['name']}`을(를) 충분히 가지고 있지 않아요.\n"
                         f"└ 현재 보유량: {target['qty']}개 / 요청 수량: {수량}개")
            else:
                target = candidates[0]
                target_key = target["item_key"]

                # 데이터 차감 반영
                user_inventory = target["board"]["inventories"][user_id_str]
                user_inventory[target_key] -= 수량

                # 수량이 0개가 되면 인벤토리 칸을 깔끔하게 제거
                if user_inventory[target_key] <= 0:
                    del user_inventory[target_key]

                self._save_shop(data)
                remains = user_inventory.get(target_key, 0)

        if error:
            return await interaction.followup.send(error, ephemeral=True)

        actual_name, board_channel_id = target["name"], target["channel_id"]

        # 아이템이 있던 매대(현재 채널이 아닐 수 있어요)의 전광판을 갱신해요.
        # 🛡️ 갱신은 메세지 조회·전송이라 실패할 수 있어요. 차감은 이미 끝난 뒤라, 여기서 예외가
        #    터져 안내까지 통째로 날아가면 관리자가 처리 여부를 알 수 없게 됩니다. 삼키고 진행해요.
        #    (전광판은 어차피 1시간마다 도는 shop_refresh_loop가 다시 그려줍니다)
        try:
            await self.update_shop_board(int(board_channel_id))
        except Exception as e:
            print(f"❗ 매대(채널 {board_channel_id}) 전광판 갱신 실패 (차감은 정상 처리됨): {e}")

        # 🧾 상점 로그 채널에 사용 기록 발급
        # 응답이 관리자에게만 보이게 바뀐 만큼, 어디서 처리했는지도 로그에 남겨둡니다.
        await send_log_embed(
            self.bot, "shop_log", "아이템 사용 처리 기록이에요.",
            fields=[
                ("매대 채널", f"<#{board_channel_id}>", True),
                ("대상자", 유저.mention, True),
                ("사용한 아이템", actual_name, True),
                ("사용 수량", f"{수량:,} 개", True),
                ("남은 수량", f"{remains:,} 개", True),
                ("처리 관리자", interaction.user.mention, True),
                ("실행한 채널", f"<#{interaction.channel_id}>", True),
            ],
        )

        # 👀 처리한 관리자에게만 보여줍니다. (기록은 위 상점 로그 채널에 남아요)
        await interaction.followup.send(
            f"✅ {유저.mention}님의 인벤토리에서 `{actual_name}` {수량}개를 사용(차감) 처리했어요.\n"
            f"└ 매대: {target['board_name']} (<#{board_channel_id}>) · 남은 수량 {remains:,}개",
            ephemeral=True
        )

    # 🔍 [아이템이름] 입력 창에 유저가 보유한 아이템을 힌트로 띄워주는 자동완성 함수
    @use_item.autocomplete('아이템이름')
    async def use_item_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        # 1. 명령어 입력창에서 먼저 선택된 '유저' 칸의 데이터 가져오기
        유저 = interaction.namespace.유저
        if not 유저:
            return []  # 유저를 아직 선택하지 않았다면 힌트를 띄우지 않음

        # 유저가 Member 객체인지 ID(문자열/숫자)인지 방어적으로 체크하여 ID 문자열 추출
        user_id_str = str(유저.id) if hasattr(유저, 'id') else str(유저)

        # 2. 데이터 로드 및 해당 유저의 인벤토리 조회
        #    명령어를 아무 채널에서나 쓸 수 있으니, 이 채널 매대만이 아니라 전 매대를 훑어요.
        data = self._load_shop()

        choices = []
        for channel_id, board in data.get("boards", {}).items():
            items = board.get("items", {})
            board_name = board.get("shop_name", "상점")

            for item_key, qty in board.get("inventories", {}).get(user_id_str, {}).items():
                # 🛡️ 0개짜리 칸은 건너뜁니다. 위 되팔기 버그로 이미 쌓여버린 데이터가 있고,
                # /지갑도 0개는 안 보여주기 때문에 여기서도 똑같이 숨겨야 화면끼리 맞아요.
                if not isinstance(qty, int) or qty <= 0:
                    continue

                # 인벤토리 저장 키가 고유 ID일 수도 있고, 한글 이름일 수도 있어서 이름을 안전하게 변환해요.
                item_info = items.get(item_key)
                if item_info:
                    label = item_info.get("name", item_key)
                else:
                    # 🐛 [버그 수정] 매대에서 삭제된 상품이 인벤토리에 남아 있으면, 예전엔 아이템 ID가
                    # 그대로 이름 자리에 나와서 힌트에 "3 (보유: 2개)"처럼 숫자만 떴어요.
                    # 무슨 물건인지 알 수 없으니 삭제된 상품이라는 걸 분명히 알려줍니다.
                    label = f"(삭제된 상품 #{item_key})"

                # 관리자가 지금 타이핑 중인 글자로 걸러요. (매대 이름으로도 찾을 수 있게 같이 봅니다)
                if current.lower() not in f"{label} {board_name}".lower():
                    continue

                # 고른 값에는 매대 채널까지 실어 보냅니다. 같은 이름의 상품이 여러 매대에 있어도
                # 어느 매대 물건인지 헷갈리지 않게 하려는 거예요. (_find_use_targets가 이 형태를 읽어요)
                value = f"{channel_id}:{item_key}"
                if len(value) > 100:
                    # ⚠️ 값이 100자를 넘으면 디스코드가 자동완성 응답을 통째로 거부해요.
                    #    이 경우엔 매대 정보를 빼고 키만 보내고, 매대 선택은 명령어 쪽 안내에 맡깁니다.
                    value = item_key[:100]

                # 💡 '아이템 이름 · 매대 (보유: X개)' 형태로 보여줘요.
                # ⚠️ 표시 이름도 100자를 넘기면 안 되므로, 이름이 긴 상품을 대비해 잘라둡니다.
                suffix = f" · {board_name} (보유: {qty}개)"
                choices.append(app_commands.Choice(
                    name=(label[:max(1, 100 - len(suffix))] + suffix)[:100],
                    value=value,
                ))

        # 이름순으로 정렬하면 관리자가 찾기 쉬워요.
        choices.sort(key=lambda c: c.name)
        # 디스코드 자동완성은 최대 25개까지만 지원하므로 잘라서 반환
        return choices[:25]

    @shop_group.command(name="정산", description="[관리자] 특정 월의 상점 전체(모든 매대 합산) 판매 정산액을 조회합니다.")
    @app_commands.guild_only()
    @app_commands.describe(월="조회할 월 (1~12)", 년도="조회할 연도 (생략 시 올해)")
    async def settlement(self, interaction: discord.Interaction, 월: int, 년도: Optional[int] = None):
        if not self._is_admin(interaction):
            return await interaction.response.send_message("⛔ 권한이 없어요. 상점 관리자만 사용할 수 있어요.", ephemeral=True)
        if not (1 <= 월 <= 12):
            return await interaction.response.send_message("❌ 월은 1~12 사이여야 해요.", ephemeral=True)

        target_year = 년도 if 년도 is not None else dt.datetime.now(KST).year
        target_key = f"{target_year:04d}-{월:02d}"

        transactions = self._load_transactions()
        buy_total, sell_total = 0, 0
        per_board = {}  # channel_id -> {"buy": n, "sell": n}

        for tx in transactions:
            ts = tx.get("timestamp", "")
            if not ts.startswith(target_key):
                continue
            amount = tx.get("amount", 0)
            ch_id = tx.get("channel_id")
            board_stat = per_board.setdefault(ch_id, {"buy": 0, "sell": 0})
            if tx.get("type") == "buy":
                buy_total += amount
                board_stat["buy"] += amount
            elif tx.get("type") == "sell":
                sell_total += amount
                board_stat["sell"] += amount

        net_total = buy_total - sell_total

        embed = discord.Embed(
            title=f"📊 {target_year}년 {월}월 상점 정산",
            description=f"모든 매대를 합산한 {target_year}년 {월}월 정산 결과예요.",
            color=0x5ce6b4,
        )
        embed.add_field(name="💰 총 구매 매출", value=f"{buy_total:,} 에바", inline=True)
        embed.add_field(name="♻️ 총 되팔기 환급", value=f"{sell_total:,} 에바", inline=True)
        embed.add_field(name="✅ 순 정산액", value=f"**{net_total:,} 에바**", inline=True)

        if per_board:
            lines = []
            for ch_id, stat in per_board.items():
                net = stat["buy"] - stat["sell"]
                lines.append(f"<#{ch_id}> · 구매 {stat['buy']:,} / 되팔기 {stat['sell']:,} / 순액 {net:,} 에바")
            embed.add_field(name="🏪 매대별 상세", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="🏪 매대별 상세", value="해당 월 거래 내역이 없어요.", inline=False)

        # 👀 매출 숫자가 아무 채널에나 공개로 남지 않도록, 조회한 관리자에게만 보여줍니다.
        # (매대 채널 밖에서도 편하게 조회할 수 있게 하려는 거예요)
        await interaction.response.send_message(embed=embed, ephemeral=True)
