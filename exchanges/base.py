"""
交易所基类接口
所有交易所实现必须继承此基类
参考: perp-dex-tools/exchanges/base.py + cross-exchange-arbitrage/exchanges/base.py
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple, Type, Union

from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


def query_retry(
    default_return: Any = None,
    exception_type: Union[Type[Exception], Tuple[Type[Exception], ...]] = (Exception,),
    max_attempts: int = 5,
    min_wait: float = 1,
    max_wait: float = 10,
    reraise: bool = False,
):
    """通用重试装饰器"""

    def retry_error_callback(retry_state: RetryCallState):
        print(
            f"操作: [{retry_state.fn.__name__}] 在 {retry_state.attempt_number} 次重试后失败, "
            f"异常: {str(retry_state.outcome.exception())}"
        )
        return default_return

    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        retry=retry_if_exception_type(exception_type),
        retry_error_callback=retry_error_callback,
        reraise=reraise,
    )


@dataclass
class OrderResult:
    """标准化订单结果"""

    success: bool
    order_id: Optional[str] = None
    side: Optional[str] = None
    size: Optional[Decimal] = None
    price: Optional[Decimal] = None
    status: Optional[str] = None
    error_message: Optional[str] = None
    filled_size: Optional[Decimal] = None
    flags: Optional[List[str]] = None


@dataclass
class OrderInfo:
    """标准化订单信息"""

    order_id: str
    side: str
    size: Decimal
    price: Decimal
    status: str
    filled_size: Decimal = Decimal("0")
    remaining_size: Decimal = Decimal("0")
    cancel_reason: str = ""


@dataclass
class BBO:
    """买一卖一价格"""

    bid: Decimal
    ask: Decimal
    bid_size: Decimal = Decimal("0")
    ask_size: Decimal = Decimal("0")
    timestamp: float = 0.0


@dataclass
class PositionInfo:
    """持仓信息"""

    market: str
    side: str  # "LONG" / "SHORT" / "NONE"
    size: Decimal = Decimal("0")
    entry_price: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")


@dataclass
class MarketInfo:
    """市场信息"""

    symbol: str
    tick_size: Decimal = Decimal("0.1")
    step_size: Decimal = Decimal("0.0001")
    min_notional: Decimal = Decimal("10")
    min_size: Decimal = Decimal("0.0001")


class BaseExchangeClient(ABC):
    """交易所客户端基类"""

    def __init__(self, name: str):
        self.name = name

    @staticmethod
    def round_price(price: Decimal, tick_size: Decimal) -> Decimal:
        """按 tick_size 对齐价格"""
        return (price / tick_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick_size

    @staticmethod
    def round_size(size: Decimal, step_size: Decimal) -> Decimal:
        """按 step_size 对齐数量"""
        return (size / step_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_size

    @abstractmethod
    async def connect(self) -> None:
        """连接到交易所"""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接"""
        pass

    @abstractmethod
    async def get_bbo(self, market: str) -> Optional[BBO]:
        """获取买一卖一价格"""
        pass

    @abstractmethod
    async def place_limit_order(
        self,
        market: str,
        side: str,
        size: Decimal,
        price: Decimal,
        post_only: bool = False,
        reduce_only: bool = False,
    ) -> OrderResult:
        """下限价单"""
        pass

    @abstractmethod
    async def place_market_order(
        self,
        market: str,
        side: str,
        size: Decimal,
        reduce_only: bool = False,
    ) -> OrderResult:
        """下市价单"""
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        pass

    @abstractmethod
    async def cancel_all_orders(self, market: str) -> int:
        """取消所有挂单，返回取消数量"""
        pass

    @abstractmethod
    async def get_positions(self, market: str) -> List[PositionInfo]:
        """获取持仓"""
        pass

    @abstractmethod
    async def get_balance(self) -> Optional[Decimal]:
        """获取 USDC 余额"""
        pass

    @abstractmethod
    async def get_market_info(self, market: str) -> Optional[MarketInfo]:
        """获取市场参数（tick_size、step_size 等）"""
        pass

    @abstractmethod
    async def close_position(self, market: str) -> bool:
        """市价平仓指定市场的全部仓位"""
        pass
