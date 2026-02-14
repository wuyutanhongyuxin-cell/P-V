"""
Paradex 交易所客户端（Interactive Token 零手续费版）
参考: para_tested_02/sniper_bot.py 的 ParadexInteractiveClient
核心: 使用 POST /v1/auth?token_usage=interactive 获取零手续费 JWT
"""

import base64
import json
import logging
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional

import aiohttp
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from .base import (
    BBO,
    BaseExchangeClient,
    MarketInfo,
    OrderResult,
    PositionInfo,
)

logger = logging.getLogger("paradex")


class ParadexInteractiveClient(BaseExchangeClient):
    """
    Paradex 客户端 — Interactive Token 零手续费版本
    关键: 使用 ParadexSubkey + interactive JWT 实现 0 maker/taker 手续费
    """

    def __init__(self, l2_private_key: str, l2_address: str, environment: str = "prod"):
        super().__init__(name="paradex")
        self.l2_private_key = l2_private_key
        self.l2_address = l2_address
        self.environment = environment

        # API 基础地址
        env_domain = "prod" if environment == "prod" else "testnet"
        self.base_url = f"https://api.{env_domain}.paradex.trade/v1"

        # JWT 认证状态
        self.jwt_token: Optional[str] = None
        self.jwt_expires_at: int = 0

        # 市场信息缓存
        self._market_info_cache: Dict[str, MarketInfo] = {}

        # aiohttp session（复用连接）
        self._session: Optional[aiohttp.ClientSession] = None

        # 初始化 paradex-py SDK
        self._init_sdk()

    def _init_sdk(self):
        """初始化 Paradex SDK（使用 ParadexSubkey，不是 Paradex）"""
        try:
            from paradex_py import ParadexSubkey
            from paradex_py.environment import PROD, TESTNET

            env = PROD if self.environment == "prod" else TESTNET
            self.paradex = ParadexSubkey(
                env=env,
                l2_private_key=self.l2_private_key,
                l2_address=self.l2_address,
            )
            logger.info(f"Paradex SDK 初始化成功 (环境: {self.environment})")
        except ImportError:
            raise ImportError("请先安装 paradex-py: pip install paradex-py")

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取复用的 aiohttp session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def connect(self) -> None:
        """连接（获取 interactive JWT）"""
        success = await self.authenticate_interactive()
        if not success:
            raise ConnectionError("Paradex Interactive Token 认证失败")
        logger.info("Paradex 连接成功（Interactive Token 已获取）")

    async def disconnect(self) -> None:
        """断开连接"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("Paradex 连接已断开")

    # ========== 认证 ==========

    async def authenticate_interactive(self) -> bool:
        """
        使用 interactive 模式认证，获取零手续费 JWT
        关键: POST {base_url}/auth?token_usage=interactive
        """
        try:
            # 使用 SDK 生成认证签名头
            auth_headers = self.paradex.account.auth_headers()

            session = await self._get_session()
            url = f"{self.base_url}/auth?token_usage=interactive"

            headers = {
                "Content-Type": "application/json",
                **auth_headers,
            }

            async with session.post(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.jwt_token = data.get("jwt_token")

                    # 解析 JWT payload 获取过期时间
                    payload_part = self.jwt_token.split(".")[1]
                    # 添加 base64 padding
                    payload_part += "=" * (4 - len(payload_part) % 4)
                    decoded = json.loads(base64.b64decode(payload_part))

                    self.jwt_expires_at = decoded.get("exp", 0)
                    token_usage = decoded.get("token_usage", "unknown")

                    logger.info(
                        f"Paradex 认证成功! token_usage={token_usage}"
                    )

                    if token_usage != "interactive":
                        logger.warning(
                            "警告: token_usage 不是 interactive，手续费可能不是 0!"
                        )

                    return True
                else:
                    error = await resp.text()
                    logger.error(f"Paradex 认证失败: {resp.status} - {error}")
                    return False

        except Exception as e:
            logger.error(f"Paradex 认证异常: {e}")
            return False

    async def ensure_authenticated(self) -> bool:
        """确保已认证且 token 未过期（提前 60 秒续期）"""
        now = int(time.time())
        if self.jwt_token and self.jwt_expires_at > now + 60:
            return True
        logger.info("Paradex Token 过期或不存在，重新认证...")
        return await self.authenticate_interactive()

    def _get_auth_headers(self) -> Dict[str, str]:
        """获取带 JWT 的请求头"""
        return {
            "Authorization": f"Bearer {self.jwt_token}",
            "Content-Type": "application/json",
        }

    # ========== 市场数据 ==========

    async def get_bbo(self, market: str) -> Optional[BBO]:
        """
        获取买一卖一价格
        使用 GET /v1/orderbook/{market}?depth=1
        """
        try:
            if not await self.ensure_authenticated():
                return None

            session = await self._get_session()
            url = f"{self.base_url}/orderbook/{market}?depth=1"

            async with session.get(url, headers=self._get_auth_headers()) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])

                    if not bids or not asks:
                        return None

                    return BBO(
                        bid=Decimal(bids[0][0]),
                        ask=Decimal(asks[0][0]),
                        bid_size=Decimal(bids[0][1]),
                        ask_size=Decimal(asks[0][1]),
                        timestamp=time.time(),
                    )
                elif resp.status == 429:
                    import asyncio
                    logger.warning("Paradex 限流 (429)，等待 5 秒...")
                    await asyncio.sleep(5)
                    return None
                else:
                    logger.warning(f"获取 Paradex BBO 失败: {resp.status}")
                    return None

        except Exception as e:
            logger.error(f"获取 Paradex BBO 异常: {e}")
            return None

    async def get_market_info(self, market: str) -> Optional[MarketInfo]:
        """获取市场参数（tick_size、step_size 等），结果会缓存"""
        if market in self._market_info_cache:
            return self._market_info_cache[market]

        try:
            session = await self._get_session()
            url = f"{self.base_url}/markets"

            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for m in data.get("results", []):
                        symbol = m.get("symbol", "")
                        info = MarketInfo(
                            symbol=symbol,
                            tick_size=Decimal(m.get("price_tick_size", "0.1")),
                            step_size=Decimal(m.get("order_size_increment", "0.0001")),
                            min_notional=Decimal(m.get("min_notional", "10")),
                            min_size=Decimal(m.get("order_size_increment", "0.0001")),
                        )
                        self._market_info_cache[symbol] = info

                    return self._market_info_cache.get(market)
                return None

        except Exception as e:
            logger.error(f"获取 Paradex 市场信息失败: {e}")
            return None

    # ========== 下单 ==========

    async def place_limit_order(
        self,
        market: str,
        side: str,
        size: Decimal,
        price: Decimal,
        post_only: bool = False,
        reduce_only: bool = False,
    ) -> OrderResult:
        """
        下限价单 — SDK 签名 + HTTP 发送（使用 interactive JWT）
        不使用 SDK 的 api_client.submit_order()，以携带 interactive token
        """
        try:
            if not await self.ensure_authenticated():
                return OrderResult(success=False, error_message="认证失败")

            from paradex_py.common.order import Order, OrderSide, OrderType

            order_side = OrderSide.Buy if side.upper() == "BUY" else OrderSide.Sell
            instruction = "POST_ONLY" if post_only else "GTC"

            # 获取市场参数并对齐精度
            market_info = await self.get_market_info(market)
            if market_info:
                price = self.round_price(price, market_info.tick_size)
                size = self.round_size(size, market_info.step_size)

            order = Order(
                market=market,
                order_type=OrderType.Limit,
                order_side=order_side,
                size=size,
                limit_price=price,
                client_id=f"arb_{int(time.time() * 1000)}",
                instruction=instruction,
                reduce_only=reduce_only,
                signature_timestamp=int(time.time() * 1000),
            )

            # 使用 SDK 签名订单
            order.signature = self.paradex.account.sign_order(order)

            # 通过 HTTP 发送，携带 interactive JWT
            session = await self._get_session()
            url = f"{self.base_url}/orders"
            payload = order.dump_to_dict()

            async with session.post(
                url, headers=self._get_auth_headers(), json=payload
            ) as resp:
                if resp.status == 201:
                    result = await resp.json()
                    order_id = result.get("id", "")
                    flags = result.get("flags", [])

                    if "INTERACTIVE" in flags:
                        logger.debug(f"订单 {order_id} 确认 INTERACTIVE 模式 (0手续费)")
                    else:
                        logger.warning(
                            f"订单 {order_id} flags={flags}, 可能不是 interactive"
                        )

                    return OrderResult(
                        success=True,
                        order_id=order_id,
                        side=side.upper(),
                        size=size,
                        price=price,
                        status=result.get("status", "NEW"),
                        flags=flags,
                    )
                else:
                    error = await resp.text()
                    logger.error(f"Paradex 下单失败: {resp.status} - {error}")
                    return OrderResult(
                        success=False, error_message=f"{resp.status}: {error}"
                    )

        except Exception as e:
            logger.error(f"Paradex 下单异常: {e}")
            return OrderResult(success=False, error_message=str(e))

    async def place_market_order(
        self,
        market: str,
        side: str,
        size: Decimal,
        reduce_only: bool = False,
    ) -> OrderResult:
        """下市价单（主要用于平仓）"""
        try:
            if not await self.ensure_authenticated():
                return OrderResult(success=False, error_message="认证失败")

            from paradex_py.common.order import Order, OrderSide, OrderType

            order_side = OrderSide.Buy if side.upper() == "BUY" else OrderSide.Sell

            # 对齐数量精度
            market_info = await self.get_market_info(market)
            if market_info:
                size = self.round_size(size, market_info.step_size)

            order = Order(
                market=market,
                order_type=OrderType.Market,
                order_side=order_side,
                size=size,
                client_id=f"arb_mkt_{int(time.time() * 1000)}",
                reduce_only=reduce_only,
                signature_timestamp=int(time.time() * 1000),
            )

            order.signature = self.paradex.account.sign_order(order)

            session = await self._get_session()
            url = f"{self.base_url}/orders"
            payload = order.dump_to_dict()

            async with session.post(
                url, headers=self._get_auth_headers(), json=payload
            ) as resp:
                if resp.status == 201:
                    result = await resp.json()
                    return OrderResult(
                        success=True,
                        order_id=result.get("id", ""),
                        side=side.upper(),
                        size=size,
                        status=result.get("status", "NEW"),
                    )
                else:
                    error = await resp.text()
                    logger.error(f"Paradex 市价单失败: {resp.status} - {error}")
                    return OrderResult(
                        success=False, error_message=f"{resp.status}: {error}"
                    )

        except Exception as e:
            logger.error(f"Paradex 市价单异常: {e}")
            return OrderResult(success=False, error_message=str(e))

    # ========== 订单管理 ==========

    async def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        try:
            if not await self.ensure_authenticated():
                return False

            session = await self._get_session()
            url = f"{self.base_url}/orders/{order_id}"
            async with session.delete(url, headers=self._get_auth_headers()) as resp:
                if resp.status in [200, 204]:
                    logger.info(f"Paradex 订单已取消: {order_id}")
                    return True
                else:
                    error = await resp.text()
                    logger.warning(f"Paradex 取消订单失败: {resp.status} - {error}")
                    return False

        except Exception as e:
            logger.error(f"Paradex 取消订单异常: {e}")
            return False

    async def cancel_all_orders(self, market: str) -> int:
        """取消指定市场的所有挂单"""
        try:
            if not await self.ensure_authenticated():
                return 0

            session = await self._get_session()

            # 获取所有 OPEN 订单
            url = f"{self.base_url}/orders"
            params = {"status": "OPEN", "market": market}
            async with session.get(
                url, headers=self._get_auth_headers(), params=params
            ) as resp:
                if resp.status != 200:
                    return 0
                data = await resp.json()
                orders = data.get("results", [])

            if not orders:
                return 0

            # 逐一取消
            cancelled = 0
            for order in orders:
                oid = order.get("id")
                if oid and await self.cancel_order(oid):
                    cancelled += 1

            logger.info(f"Paradex 已取消 {cancelled}/{len(orders)} 个挂单")
            return cancelled

        except Exception as e:
            logger.error(f"Paradex 取消所有订单异常: {e}")
            return 0

    async def get_order_info(self, order_id: str) -> Optional[Dict[str, Any]]:
        """查询订单状态"""
        try:
            if not await self.ensure_authenticated():
                return None

            session = await self._get_session()
            url = f"{self.base_url}/orders/{order_id}"
            async with session.get(url, headers=self._get_auth_headers()) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None

        except Exception as e:
            logger.error(f"Paradex 查询订单异常: {e}")
            return None

    # ========== 仓位和余额 ==========

    async def get_positions(self, market: str) -> List[PositionInfo]:
        """获取持仓"""
        try:
            if not await self.ensure_authenticated():
                return []

            session = await self._get_session()
            url = f"{self.base_url}/positions"
            async with session.get(url, headers=self._get_auth_headers()) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    positions = []
                    for p in data.get("results", []):
                        if p.get("market") != market:
                            continue
                        if p.get("status") == "CLOSED":
                            continue
                        size = Decimal(p.get("size", "0"))
                        if size == 0:
                            continue
                        positions.append(
                            PositionInfo(
                                market=p.get("market", ""),
                                side=p.get("side", "NONE"),
                                size=size,
                                entry_price=Decimal(p.get("average_entry_price", "0")),
                                unrealized_pnl=Decimal(
                                    p.get("unrealized_pnl", "0")
                                ),
                            )
                        )
                    return positions
                return []

        except Exception as e:
            logger.error(f"Paradex 获取持仓异常: {e}")
            return []

    async def get_position_size(self, market: str) -> Decimal:
        """获取持仓大小（有符号: 多为正，空为负）"""
        positions = await self.get_positions(market)
        if not positions:
            return Decimal("0")
        pos = positions[0]
        if pos.side == "LONG":
            return pos.size
        elif pos.side == "SHORT":
            return -pos.size
        return Decimal("0")

    async def get_balance(self) -> Optional[Decimal]:
        """获取 USDC 余额"""
        try:
            if not await self.ensure_authenticated():
                return None

            session = await self._get_session()
            url = f"{self.base_url}/balance"
            async with session.get(url, headers=self._get_auth_headers()) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data.get("results", []):
                        if item.get("token") == "USDC":
                            return Decimal(str(item.get("size", "0")))
                return Decimal("0")

        except Exception as e:
            logger.error(f"Paradex 获取余额异常: {e}")
            return None

    async def close_position(self, market: str) -> bool:
        """市价平仓指定市场的全部仓位"""
        try:
            positions = await self.get_positions(market)
            if not positions:
                return True

            for pos in positions:
                if pos.size == 0:
                    continue
                close_side = "SELL" if pos.side == "LONG" else "BUY"
                result = await self.place_market_order(
                    market=market,
                    side=close_side,
                    size=pos.size,
                    reduce_only=True,
                )
                if result.success:
                    logger.info(
                        f"Paradex 平仓成功: {close_side} {pos.size} {market}"
                    )
                else:
                    logger.error(
                        f"Paradex 平仓失败: {result.error_message}"
                    )
                    return False
            return True

        except Exception as e:
            logger.error(f"Paradex 平仓异常: {e}")
            return False
