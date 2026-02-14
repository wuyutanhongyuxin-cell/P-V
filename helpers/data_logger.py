"""
CSV 数据记录模块
参考: cross-exchange-arbitrage/strategy/data_logger.py
功能:
  - BBO 价差数据记录
  - 交易记录
"""

import csv
import logging
import os
from datetime import datetime
from decimal import Decimal

import pytz

logger = logging.getLogger("data_logger")


class DataLogger:
    """数据记录到 CSV"""

    def __init__(self, ticker: str, log_dir: str = "logs"):
        self.ticker = ticker
        os.makedirs(log_dir, exist_ok=True)

        self.bbo_file_path = os.path.join(log_dir, f"arb_{ticker}_bbo.csv")
        self.trade_file_path = os.path.join(log_dir, f"arb_{ticker}_trades_detail.csv")
        self.timezone = pytz.timezone(os.getenv("TIMEZONE", "Asia/Shanghai"))

        # 打开 BBO 文件（保持打开以提高写入效率）
        self._bbo_file = None
        self._bbo_writer = None
        self._bbo_counter = 0
        self._flush_interval = 10

        self._init_bbo_file()
        self._init_trade_file()

    def _init_bbo_file(self):
        """初始化 BBO CSV"""
        file_exists = os.path.exists(self.bbo_file_path)
        self._bbo_file = open(
            self.bbo_file_path, "a", newline="", buffering=8192, encoding="utf-8"
        )
        self._bbo_writer = csv.writer(self._bbo_file)

        if not file_exists:
            self._bbo_writer.writerow([
                "timestamp",
                "paradex_bid",
                "paradex_ask",
                "variational_bid",
                "variational_ask",
                "long_spread",
                "short_spread",
            ])
            self._bbo_file.flush()

    def _init_trade_file(self):
        """初始化交易 CSV"""
        if not os.path.exists(self.trade_file_path):
            with open(self.trade_file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp",
                    "direction",
                    "paradex_side",
                    "paradex_price",
                    "variational_side",
                    "size",
                    "spread",
                ])

    def log_bbo(
        self,
        paradex_bid: Decimal,
        paradex_ask: Decimal,
        variational_bid: Decimal,
        variational_ask: Decimal,
        long_spread: Decimal,
        short_spread: Decimal,
    ):
        """记录 BBO 数据"""
        if not self._bbo_writer:
            return

        try:
            timestamp = datetime.now(self.timezone).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            self._bbo_writer.writerow([
                timestamp,
                float(paradex_bid),
                float(paradex_ask),
                float(variational_bid),
                float(variational_ask),
                float(long_spread),
                float(short_spread),
            ])

            self._bbo_counter += 1
            if self._bbo_counter >= self._flush_interval:
                self._bbo_file.flush()
                self._bbo_counter = 0

        except Exception as e:
            logger.error(f"记录 BBO 失败: {e}")

    def log_trade(
        self,
        direction: str,
        paradex_side: str,
        paradex_price: Decimal,
        variational_side: str,
        size: Decimal,
        spread: Decimal,
    ):
        """记录一笔套利交易"""
        try:
            timestamp = datetime.now(self.timezone).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            with open(self.trade_file_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    timestamp,
                    direction,
                    paradex_side,
                    str(paradex_price),
                    variational_side,
                    str(size),
                    str(spread),
                ])
        except Exception as e:
            logger.error(f"记录交易失败: {e}")

    def close(self):
        """关闭文件句柄"""
        if self._bbo_file:
            try:
                self._bbo_file.flush()
                self._bbo_file.close()
                self._bbo_file = None
                self._bbo_writer = None
            except Exception as e:
                logger.error(f"关闭 BBO 文件失败: {e}")
                self._bbo_file = None
                self._bbo_writer = None
