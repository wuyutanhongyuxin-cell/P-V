"""
Variational (Omni) 交易所客户端
使用 curl_cffi (Chrome TLS 指纹) 绕过 Cloudflare 保护
所有端点均已通过 API 测试确认 (2026-02-14)

公开数据 (后端直连, 无 Cloudflare):
  GET  /metadata/stats                  — 所有市场的统计和报价

交易数据 (Cookie 认证 + curl_cffi Chrome TLS):
  GET  /api/positions                   — 获取持仓
  GET  /api/orders/v2?status=pending    — 查询挂单
  POST /api/quotes/indicative           — 获取 RFQ 报价 (市价单第1步)
  POST /api/orders/new/market           — 提交市价单 (第2步, 需 quote_id)
  POST /api/orders/new/limit            — 提交限价单
  POST /api/orders/cancel               — 取消订单
  GET  /api/portfolio?compute_margin=true — 余额和保证金
  GET  /api/metadata/supported_assets   — 支持的资产列表

认证: Cookie 中的 vr-token (JWT) + Header: vr-connected-address
Front URL: https://omni.variational.io (需 curl_cffi 绕过 Cloudflare)
Public URL: https://omni-client-api.prod.ap-northeast-1.variational.io (后端直连)
"""

import logging
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from curl_cffi.requests import AsyncSession

from .base import (
    BBO,
    BaseExchangeClient,
    MarketInfo,
    OrderResult,
    PositionInfo,
)

logger = logging.getLogger("variational")

# =============================================================================
# API 端点 — 全部已通过测试确认 (2026-02-14)
# =============================================================================
ENDPOINTS = {
    # 公开数据 (通过后端 URL 访问)
    "stats": "/metadata/stats",
    # 持仓和订单查询
    "positions": "/api/positions",
    "orders": "/api/orders/v2",
    # 下单 (RFQ 模式)
    "quote_indicative": "/api/quotes/indicative",
    "order_market": "/api/orders/new/market",
    "order_limit": "/api/orders/new/limit",
    "order_tpsl": "/api/orders/tpsl",
    # 取消订单
    "cancel_order": "/api/orders/cancel",
    # 余额
    "balance": "/api/portfolio",
    # 其他
    "supported_assets": "/api/metadata/supported_assets",
    "leverage": "/api/settlement_pools/leverage",
    "open_interest": "/api/metadata/open_interest",
    "candles": "/api/candles",
    "funding": "/api/funding",
}

# 后端直连 URL — 用于公开数据 (无 Cloudflare)
PUBLIC_BASE_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io"


class VariationalClient(BaseExchangeClient):
    """
    Variational (Omni) 客户端
    使用 curl_cffi (Chrome TLS 指纹模拟) 绕过 Cloudflare 保护
    认证方式: Cookie (vr-token JWT) + vr-connected-address header
    """

    # Instrument 命名: P-{underlying}-{settlement}-{funding_interval}
    INSTRUMENT_MAP = {
        "BTC": "P-BTC-USDC-3600",
        "ETH": "P-ETH-USDC-3600",
        "SOL": "P-SOL-USDC-3600",
        "ARB": "P-ARB-USDC-3600",
        "DOGE": "P-DOGE-USDC-3600",
        "AVAX": "P-AVAX-USDC-3600",
        "LINK": "P-LINK-USDC-3600",
        "OP": "P-OP-USDC-3600",
        "WIF": "P-WIF-USDC-3600",
        "PEPE": "P-PEPE-USDC-3600",
    }

    def __init__(
        self,
        vr_token: str = "",
        wallet_address: str = "",
        cookies: str = "",
        private_key: str = "",
        base_url: str = "https://omni.variational.io",
        auth_mode: str = "cookie",
    ):
        super().__init__(name="variational")
        self.vr_token = vr_token
        self.wallet_address = wallet_address
        self.cookies = cookies
        self.private_key = private_key
        self.base_url = base_url.rstrip("/")
        self.auth_mode = auth_mode

        self._session: Optional[AsyncSession] = None

        # 市场数据缓存
        self._stats_cache: Optional[Dict[str, Any]] = None
        self._stats_cache_time: float = 0
        self._stats_cache_ttl: float = 2.0

        # 市场信息缓存
        self._market_info_cache: Dict[str, MarketInfo] = {}

    async def _get_session(self) -> AsyncSession:
        """获取 curl_cffi AsyncSession (模拟 Chrome 131 TLS 指纹)"""
        if self._session is None:
            self._session = AsyncSession(impersonate="chrome131")
            self._session.headers.update(self._build_headers())
        return self._session

    def _build_headers(self) -> Dict[str, str]:
        """
        构建请求头 (基于浏览器抓包)
        curl_cffi 自动处理 User-Agent 和 TLS 指纹
        """
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Referer": f"{self.base_url}/perpetual/BTC",
        }

        # 钱包地址 header (抓包确认必需)
        if self.wallet_address:
            headers["vr-connected-address"] = self.wallet_address

        # Cookie 认证
        if self.cookies:
            headers["Cookie"] = self.cookies
        elif self.vr_token:
            cookie_parts = [f"vr-token={self.vr_token}"]
            if self.wallet_address:
                cookie_parts.append(f"vr-connected-address={self.wallet_address}")
            headers["Cookie"] = "; ".join(cookie_parts)

        return headers

    # ========== 连接管理 ==========

    async def connect(self) -> None:
        """连接并验证认证"""
        if self.auth_mode == "cookie":
            await self._connect_cookie()
        else:
            raise ValueError(f"不支持的认证模式: {self.auth_mode} (当前仅支持 cookie)")

    async def _connect_cookie(self) -> None:
        """Cookie 模式连接验证"""
        if not self.vr_token and not self.cookies:
            raise ConnectionError(
                "Cookie 模式需要设置 vr_token 或 cookies\n"
                "从浏览器 F12 控制台执行 document.cookie 获取"
            )

        # 用 positions 端点验证认证有效性
        session = await self._get_session()
        try:
            r = await session.get(f"{self.base_url}{ENDPOINTS['positions']}")
            if r.status_code == 401:
                raise ConnectionError("vr-token 已过期! 请从浏览器重新获取")
            if r.status_code == 403:
                raise ConnectionError(
                    "Cloudflare 拦截! 请确保 cookies 包含 cf_clearance\n"
                    "刷新浏览器后重新复制 document.cookie"
                )
            if r.status_code != 200:
                raise ConnectionError(f"连接测试失败: HTTP {r.status_code}")
        except ConnectionError:
            raise
        except Exception as e:
            raise ConnectionError(f"无法连接到 Variational: {e}")

        logger.info("Variational 连接成功 (Cookie + curl_cffi)")

    async def disconnect(self) -> None:
        """断开连接"""
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        logger.info("Variational 连接已断开")

    async def ensure_authenticated(self) -> bool:
        """确保认证有效"""
        if not self.vr_token and not self.cookies:
            logger.error("Variational 认证信息为空，请更新 vr-token")
            return False
        return True

    # ========== Instrument 命名转换 ==========

    def market_to_instrument(self, market: str) -> str:
        """BTC → P-BTC-USDC-3600"""
        market_upper = market.upper()
        if market_upper in self.INSTRUMENT_MAP:
            return self.INSTRUMENT_MAP[market_upper]
        return f"P-{market_upper}-USDC-3600"

    @staticmethod
    def instrument_to_market(instrument: str) -> str:
        """P-BTC-USDC-3600 → BTC"""
        parts = instrument.split("-")
        if len(parts) >= 2:
            return parts[1].upper()
        return instrument.upper()

    @staticmethod
    def build_instrument_obj(market: str) -> Dict[str, Any]:
        """构建 API 请求用的 instrument 对象"""
        return {
            "underlying": market.upper(),
            "instrument_type": "perpetual_future",
            "settlement_asset": "USDC",
            "funding_interval_s": 3600,
        }

    # ========== 公开数据 (后端直连, 无 Cloudflare) ==========

    async def _fetch_stats(self) -> Optional[Dict[str, Any]]:
        """
        GET /metadata/stats — 公开接口
        通过后端 URL 直连，避免 Cloudflare 拦截
        """
        now = time.time()
        if self._stats_cache and (now - self._stats_cache_time) < self._stats_cache_ttl:
            return self._stats_cache

        try:
            session = await self._get_session()
            url = f"{PUBLIC_BASE_URL}{ENDPOINTS['stats']}"
            r = await session.get(url)

            if r.status_code == 200:
                data = r.json()
                self._stats_cache = data
                self._stats_cache_time = now
                return data
            else:
                logger.warning(f"获取 stats 失败: {r.status_code}")
                return None

        except Exception as e:
            logger.error(f"获取 stats 异常: {e}")
            return None

    def _find_market_in_stats(
        self, stats: Dict[str, Any], market: str
    ) -> Optional[Dict[str, Any]]:
        """在 stats 数据中查找特定市场"""
        for listing in stats.get("listings", []):
            if listing.get("ticker", "").upper() == market.upper():
                return listing
        return None

    # ========== 市场数据 ==========

    async def get_bbo(self, market: str) -> Optional[BBO]:
        """获取买一卖一价格 (从公开 stats 接口)"""
        try:
            stats = await self._fetch_stats()
            if not stats:
                return None

            listing = self._find_market_in_stats(stats, market)
            if not listing:
                logger.warning(f"Variational 未找到市场: {market}")
                return None

            quotes = listing.get("quotes", {})
            base_quotes = quotes.get("base", {})
            bid = base_quotes.get("bid")
            ask = base_quotes.get("ask")

            if bid is None or ask is None:
                return None

            return BBO(
                bid=Decimal(str(bid)),
                ask=Decimal(str(ask)),
                timestamp=time.time(),
            )

        except Exception as e:
            logger.error(f"获取 BBO 异常: {e}")
            return None

    async def get_mark_price(self, market: str) -> Optional[Decimal]:
        """获取标记价格"""
        try:
            stats = await self._fetch_stats()
            if not stats:
                return None

            listing = self._find_market_in_stats(stats, market)
            if not listing:
                return None

            mark_price = listing.get("mark_price")
            if mark_price:
                return Decimal(str(mark_price))
            return None

        except Exception as e:
            logger.error(f"获取标记价格异常: {e}")
            return None

    async def get_market_info(self, market: str) -> Optional[MarketInfo]:
        """获取市场参数"""
        if market in self._market_info_cache:
            return self._market_info_cache[market]

        try:
            stats = await self._fetch_stats()
            if not stats:
                return None

            listing = self._find_market_in_stats(stats, market)
            if not listing:
                return None

            info = MarketInfo(
                symbol=market,
                tick_size=Decimal("0.1"),
                step_size=Decimal("0.0001"),
                min_notional=Decimal("1"),
                min_size=Decimal("0.0001"),
            )
            self._market_info_cache[market] = info
            return info

        except Exception as e:
            logger.error(f"获取市场信息异常: {e}")
            return None

    # ========== 通用请求方法 ==========

    async def _request(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[Dict] = None,
        params: Optional[Dict] = None,
    ) -> Optional[Any]:
        """
        通用请求方法 — 统一处理认证过期和 Cloudflare 拦截
        使用 curl_cffi Chrome TLS 指纹绕过 Cloudflare
        """
        if not await self.ensure_authenticated():
            return None

        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"

        try:
            if method.upper() == "GET":
                r = await session.get(url, params=params)
            elif method.upper() == "POST":
                r = await session.post(url, json=json_data)
            else:
                r = await session.request(method, url, json=json_data, params=params)

            if r.status_code in [200, 201]:
                return r.json()
            elif r.status_code == 401:
                logger.error("Variational 认证过期! 请更新 vr-token")
                return None
            elif r.status_code == 403:
                if "Just a moment" in r.text[:200]:
                    logger.error("Cloudflare 拦截! cookies 可能已过期")
                else:
                    logger.error(f"Variational {method} {endpoint} 被拒绝: {r.text[:200]}")
                return None
            else:
                logger.error(
                    f"Variational {method} {endpoint} 失败: {r.status_code} - {r.text[:200]}"
                )
                return None

        except Exception as e:
            logger.error(f"Variational {method} {endpoint} 异常: {e}")
            return None

    # ========== 交易接口 (全部已测试确认) ==========

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
        下限价单 — POST /api/orders/new/limit
        请求体:
        {
          "order_type": "limit",
          "limit_price": "68951.765",
          "side": "buy",
          "instrument": {"underlying":"BTC",...},
          "qty": "0.000014",
          "is_auto_resize": false,
          "use_mark_price": false,
          "is_reduce_only": false
        }
        """
        try:
            payload = {
                "order_type": "limit",
                "limit_price": str(price),
                "side": side.lower(),
                "instrument": self.build_instrument_obj(market),
                "qty": str(size),
                "is_auto_resize": False,
                "use_mark_price": False,
                "is_reduce_only": reduce_only,
            }

            data = await self._request("POST", ENDPOINTS["order_limit"], json_data=payload)

            if data is not None:
                order_id = data.get("id", data.get("order_id", str(uuid.uuid4())))
                logger.info(f"Variational 限价单成功: {side} {size} {market} @ {price}")
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    side=side.upper(),
                    size=size,
                    price=price,
                    status=data.get("status", "PENDING"),
                )
            else:
                return OrderResult(success=False, error_message="限价单请求失败")

        except Exception as e:
            logger.error(f"Variational 限价单异常: {e}")
            return OrderResult(success=False, error_message=str(e))

    async def _get_quote(self, market: str, size: Decimal) -> Optional[str]:
        """
        获取 RFQ 报价 — POST /api/quotes/indicative
        返回 quote_id (市价单第1步)
        """
        payload = {
            "instrument": self.build_instrument_obj(market),
            "qty": str(size),
        }

        data = await self._request("POST", ENDPOINTS["quote_indicative"], json_data=payload)
        if data is None:
            return None

        quote_id = data.get("quote_id", data.get("id"))
        if quote_id:
            logger.debug(f"Variational 报价成功: {quote_id}")
        return quote_id

    async def place_market_order(
        self,
        market: str,
        side: str,
        size: Decimal,
        reduce_only: bool = False,
        max_slippage: float = 0.005,
    ) -> OrderResult:
        """
        下市价单 — RFQ 两步流程:
          1. POST /api/quotes/indicative → quote_id
          2. POST /api/orders/new/market → 提交
        """
        try:
            # Step 1: 获取报价
            quote_id = await self._get_quote(market, size)
            if not quote_id:
                return OrderResult(success=False, error_message="获取 RFQ 报价失败")

            # Step 2: 提交市价单
            payload = {
                "quote_id": quote_id,
                "side": side.lower(),
                "max_slippage": max_slippage,
                "is_reduce_only": reduce_only,
            }

            data = await self._request("POST", ENDPOINTS["order_market"], json_data=payload)

            if data is not None:
                order_id = data.get("id", data.get("order_id", quote_id))
                logger.info(f"Variational 市价单成功: {side} {size} {market}")
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    side=side.upper(),
                    size=size,
                    status=data.get("status", "FILLED"),
                )
            else:
                return OrderResult(success=False, error_message="市价单提交失败")

        except Exception as e:
            logger.error(f"Variational 市价单异常: {e}")
            return OrderResult(success=False, error_message=str(e))

    async def cancel_order(self, rfq_id: str) -> bool:
        """取消订单 — POST /api/orders/cancel {"rfq_id": "..."}"""
        try:
            payload = {"rfq_id": rfq_id}
            data = await self._request("POST", ENDPOINTS["cancel_order"], json_data=payload)

            if data is not None:
                logger.info(f"Variational 订单已取消: {rfq_id}")
                return True
            return False

        except Exception as e:
            logger.error(f"Variational 取消订单异常: {e}")
            return False

    async def get_pending_orders(self, market: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取未成交订单 — GET /api/orders/v2?status=pending"""
        try:
            if not await self.ensure_authenticated():
                return []

            session = await self._get_session()
            url = f"{self.base_url}{ENDPOINTS['orders']}"

            params = {"status": "pending"}
            if market:
                params["instrument"] = self.market_to_instrument(market)

            r = await session.get(url, params=params)

            if r.status_code == 200:
                data = r.json()
                orders = data.get("result", [])
                if market:
                    orders = [
                        o for o in orders
                        if o.get("instrument", {}).get("underlying", "").upper() == market.upper()
                    ]
                return orders
            elif r.status_code == 401:
                logger.error("Variational 认证过期!")
                return []
            else:
                logger.warning(f"获取订单失败: {r.status_code}")
                return []

        except Exception as e:
            logger.error(f"获取订单异常: {e}")
            return []

    async def cancel_all_orders(self, market: str) -> int:
        """取消所有挂单"""
        try:
            orders = await self.get_pending_orders(market)
            if not orders:
                return 0

            cancelled = 0
            for order in orders:
                rfq_id = order.get("rfq_id", order.get("id", ""))
                if rfq_id and await self.cancel_order(rfq_id):
                    cancelled += 1

            logger.info(f"Variational 取消了 {cancelled}/{len(orders)} 个挂单")
            return cancelled

        except Exception as e:
            logger.error(f"批量取消订单异常: {e}")
            return 0

    async def get_positions(self, market: str) -> List[PositionInfo]:
        """
        获取持仓 — GET /api/positions

        已确认返回格式:
        [
          {
            "position_info": {
              "instrument": {"underlying": "BTC", ...},
              "qty": "0.000028",
              "avg_entry_price": "69073.41"
            },
            "upnl": "-0.00305452"
          }
        ]
        """
        try:
            if not await self.ensure_authenticated():
                return []

            session = await self._get_session()
            url = f"{self.base_url}{ENDPOINTS['positions']}"
            r = await session.get(url)

            if r.status_code == 200:
                data = r.json()
                positions = []
                items = data if isinstance(data, list) else data.get("results", [])

                for item in items:
                    pos_info = item.get("position_info", {})
                    instrument = pos_info.get("instrument", {})
                    underlying = instrument.get("underlying", "")

                    if underlying.upper() != market.upper():
                        continue

                    qty = Decimal(str(pos_info.get("qty", "0")))
                    if qty == 0:
                        continue

                    side = "LONG" if qty > 0 else "SHORT"

                    positions.append(
                        PositionInfo(
                            market=underlying,
                            side=side,
                            size=abs(qty),
                            entry_price=Decimal(str(pos_info.get("avg_entry_price", "0"))),
                            unrealized_pnl=Decimal(str(item.get("upnl", "0"))),
                        )
                    )
                return positions

            elif r.status_code == 401:
                logger.error("Variational 认证过期!")
                return []
            else:
                logger.warning(f"获取持仓失败: {r.status_code}")
                return []

        except Exception as e:
            logger.error(f"获取持仓异常: {e}")
            return []

    async def get_position_size(self, market: str) -> Decimal:
        """获取持仓大小 (有符号: 多为正, 空为负)"""
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
        """
        获取余额 — GET /api/portfolio?compute_margin=true

        已确认返回格式:
        {
          "margin_usage": {"initial_margin": "0.433498", "maintenance_margin": "0.216749"},
          "balance": "117.240604919999999888",
          "upnl": "-0.065670080000000112"
        }
        """
        try:
            data = await self._request(
                "GET", ENDPOINTS["balance"], params={"compute_margin": "true"}
            )
            if data and isinstance(data, dict):
                balance = data.get("balance", "0")
                return Decimal(str(balance))
            return None

        except Exception as e:
            logger.error(f"获取余额异常: {e}")
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
                    logger.info(f"Variational 平仓成功: {close_side} {pos.size} {market}")
                else:
                    logger.error(f"Variational 平仓失败: {result.error_message}")
                    return False
            return True

        except Exception as e:
            logger.error(f"Variational 平仓异常: {e}")
            return False
