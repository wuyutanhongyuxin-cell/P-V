"""
价差分析器
参考: cross-exchange-arbitrage/strategy/edgex_arb.py 的价差采样+动态均值阈值逻辑
功能:
  - 每秒采样价差
  - 预热阶段收集前 N 条数据
  - 触发条件: 瞬时价差 > 滚动均值 + offset
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("spread_analyzer")


@dataclass
class SpreadSample:
    """价差采样"""
    timestamp: float
    long_spread: Decimal    # Variational_bid - Paradex_ask (Paradex做多, Variational做空)
    short_spread: Decimal   # Paradex_bid - Variational_ask (Paradex做空, Variational做多)


@dataclass
class SpreadSignal:
    """价差信号"""
    direction: str  # "LONG" 或 "SHORT"
    spread: Decimal
    mean: Decimal
    threshold: Decimal


class SpreadAnalyzer:
    """
    价差分析器 — 动态均值阈值策略
    核心逻辑:
      1. 每秒采样一次两个方向的价差
      2. 预热阶段收集 warmup_samples 条数据
      3. 触发条件: 瞬时价差 > 滚动均值 + offset
    """

    def __init__(
        self,
        long_threshold: Decimal = Decimal("10"),
        short_threshold: Decimal = Decimal("10"),
        min_spread: Decimal = Decimal("5"),
        warmup_samples: int = 100,
        window_size: int = 500,
    ):
        self.long_threshold = long_threshold
        self.short_threshold = short_threshold
        self.min_spread = min_spread  # 绝对最低价差，防止 mean 为负时阈值过低
        self.warmup_samples = warmup_samples

        # 滑动窗口 — 用于计算滚动均值
        self.long_spreads: deque = deque(maxlen=window_size)
        self.short_spreads: deque = deque(maxlen=window_size)

        # 采样计数
        self.sample_count: int = 0
        self.last_sample_time: float = 0

        # 当前价差
        self.current_long_spread: Decimal = Decimal("0")
        self.current_short_spread: Decimal = Decimal("0")

        # 统计
        self.long_mean: Decimal = Decimal("0")
        self.short_mean: Decimal = Decimal("0")

    @property
    def is_warmed_up(self) -> bool:
        """是否已完成预热"""
        return self.sample_count >= self.warmup_samples

    def add_sample(
        self,
        paradex_bid: Decimal,
        paradex_ask: Decimal,
        variational_bid: Decimal,
        variational_ask: Decimal,
    ) -> None:
        """
        添加一条价差样本
        long_spread = Variational_bid - Paradex_ask  (Paradex做多, Variational做空)
        short_spread = Paradex_bid - Variational_ask  (Paradex做空, Variational做多)
        """
        now = time.time()

        # 限流: 最快 1 秒采样一次
        if now - self.last_sample_time < 0.9:
            # 仍然更新当前值，但不添加到窗口
            self.current_long_spread = variational_bid - paradex_ask
            self.current_short_spread = paradex_bid - variational_ask
            return

        self.current_long_spread = variational_bid - paradex_ask
        self.current_short_spread = paradex_bid - variational_ask

        self.long_spreads.append(self.current_long_spread)
        self.short_spreads.append(self.current_short_spread)

        self.sample_count += 1
        self.last_sample_time = now

        # 更新均值
        if len(self.long_spreads) > 0:
            self.long_mean = sum(self.long_spreads) / len(self.long_spreads)
        if len(self.short_spreads) > 0:
            self.short_mean = sum(self.short_spreads) / len(self.short_spreads)

        # 预热进度日志
        if self.sample_count < self.warmup_samples:
            # 预热中：每 20 个样本打印一次
            if self.sample_count % 20 == 0:
                logger.info(
                    f"预热中: {self.sample_count}/{self.warmup_samples} | "
                    f"long_mean={self.long_mean:.4f} short_mean={self.short_mean:.4f}"
                )
        elif self.sample_count == self.warmup_samples:
            # 预热刚完成：打印最终状态
            logger.info(
                f"✅ 预热完成: {self.sample_count}/{self.warmup_samples} | "
                f"long_mean={self.long_mean:.4f} short_mean={self.short_mean:.4f}"
            )

    def check_signal(self) -> Optional[SpreadSignal]:
        """
        检查是否触发交易信号
        触发条件: 瞬时价差 > 滚动均值 + offset
        返回: SpreadSignal 或 None
        """
        if not self.is_warmed_up:
            return None

        # 触发条件: spread > max(mean + threshold, min_spread)
        # max() 确保即使 mean 深度为负，也不会用过低的阈值触发
        # 例: mean=-12, threshold=10 → max(-2, 5) = 5，而非 -2

        # 检查做多信号: Variational_bid - Paradex_ask
        long_trigger = max(self.long_mean + self.long_threshold, self.min_spread)
        if self.current_long_spread > long_trigger:
            return SpreadSignal(
                direction="LONG",
                spread=self.current_long_spread,
                mean=self.long_mean,
                threshold=self.long_threshold,
            )

        # 检查做空信号: Paradex_bid - Variational_ask
        short_trigger = max(self.short_mean + self.short_threshold, self.min_spread)
        if self.current_short_spread > short_trigger:
            return SpreadSignal(
                direction="SHORT",
                spread=self.current_short_spread,
                mean=self.short_mean,
                threshold=self.short_threshold,
            )

        return None

    def get_status(self) -> dict:
        """获取当前状态"""
        return {
            "sample_count": self.sample_count,
            "warmed_up": self.is_warmed_up,
            "long_spread": float(self.current_long_spread),
            "short_spread": float(self.current_short_spread),
            "long_mean": float(self.long_mean),
            "short_mean": float(self.short_mean),
            "long_threshold": float(self.long_threshold),
            "short_threshold": float(self.short_threshold),
        }
