"""
双边仓位跟踪器
参考: cross-exchange-arbitrage/strategy/position_tracker.py
功能:
  - 实时跟踪 Paradex 和 Variational 两边的仓位
  - 仓位不平衡检测
  - 最大持仓限制检查
"""

import logging
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("position_tracker")


class PositionTracker:
    """双边仓位跟踪器"""

    def __init__(self, max_position: Decimal = Decimal("0")):
        """
        初始化仓位跟踪器
        max_position: 最大持仓限制（单边绝对值）
        """
        self.max_position = max_position

        # 缓存的仓位值（有符号: 多为正, 空为负）
        self.paradex_position: Decimal = Decimal("0")
        self.variational_position: Decimal = Decimal("0")

    @property
    def net_position(self) -> Decimal:
        """净仓位（两边之和，理想情况应接近 0）"""
        return self.paradex_position + self.variational_position

    @property
    def is_balanced(self) -> bool:
        """仓位是否平衡（允许小误差）"""
        return abs(self.net_position) < Decimal("0.00001")

    def update_paradex(self, position: Decimal) -> None:
        """更新 Paradex 仓位"""
        self.paradex_position = position

    def update_variational(self, position: Decimal) -> None:
        """更新 Variational 仓位"""
        self.variational_position = position

    def can_open_long(self, size: Decimal) -> bool:
        """
        检查是否可以开多（Paradex做多, Variational做空）
        Paradex 仓位 + size 不能超过 max_position
        """
        if self.max_position <= 0:
            return False
        return self.paradex_position + size <= self.max_position

    def can_open_short(self, size: Decimal) -> bool:
        """
        检查是否可以开空（Paradex做空, Variational做多）
        Paradex 仓位 - size 不能低于 -max_position
        """
        if self.max_position <= 0:
            return False
        return self.paradex_position - size >= -self.max_position

    def check_imbalance(self, threshold: Decimal = Decimal("0.001")) -> bool:
        """
        检查仓位不平衡
        如果净仓位超过阈值则报警
        """
        imbalance = abs(self.net_position)
        if imbalance > threshold:
            logger.warning(
                f"仓位不平衡! 净仓位={self.net_position} "
                f"(Paradex={self.paradex_position}, Variational={self.variational_position})"
            )
            return True
        return False

    def get_status(self) -> dict:
        """获取当前仓位状态"""
        return {
            "paradex": float(self.paradex_position),
            "variational": float(self.variational_position),
            "net": float(self.net_position),
            "max_position": float(self.max_position),
            "balanced": self.is_balanced,
        }

    def log_positions(self) -> None:
        """输出当前仓位日志"""
        logger.info(
            f"仓位: Paradex={self.paradex_position:+.6f} | "
            f"Variational={self.variational_position:+.6f} | "
            f"净仓位={self.net_position:+.6f}"
        )
