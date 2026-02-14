"""
日志模块
参考: perp-dex-tools/helpers/logger.py
功能:
  - 控制台输出关键信息
  - 文件记录详细日志
  - 时区支持
"""

import csv
import logging
import os
from datetime import datetime
from decimal import Decimal

import pytz


class TradingLogger:
    """交易日志记录器"""

    def __init__(self, ticker: str, log_dir: str = "logs"):
        self.ticker = ticker
        os.makedirs(log_dir, exist_ok=True)

        self.trade_file = os.path.join(log_dir, f"arb_{ticker}_trades.csv")
        self.timezone = pytz.timezone(os.getenv("TIMEZONE", "Asia/Shanghai"))

        self._init_trade_csv()

    def _init_trade_csv(self):
        """初始化交易 CSV 文件"""
        if not os.path.exists(self.trade_file):
            with open(self.trade_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp",
                    "direction",
                    "size",
                    "paradex_price",
                    "spread",
                    "cumulative_trades",
                ])

    def log_trade(
        self,
        direction: str,
        size: Decimal,
        paradex_price: Decimal,
        spread: Decimal,
    ):
        """记录一笔套利交易到 CSV"""
        try:
            timestamp = datetime.now(self.timezone).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            with open(self.trade_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    timestamp,
                    direction,
                    str(size),
                    str(paradex_price),
                    str(spread),
                    "",  # cumulative_trades 由外部填充
                ])
        except Exception as e:
            logging.getLogger("trading_logger").error(f"记录交易失败: {e}")


def setup_logging(ticker: str, log_dir: str = "logs") -> logging.Logger:
    """
    设置全局日志系统
    - 控制台: INFO 级别
    - 文件: DEBUG 级别
    """
    os.makedirs(log_dir, exist_ok=True)

    timezone = pytz.timezone(os.getenv("TIMEZONE", "Asia/Shanghai"))

    class TZFormatter(logging.Formatter):
        def __init__(self, fmt=None, datefmt=None, tz=None):
            super().__init__(fmt=fmt, datefmt=datefmt)
            self.tz = tz

        def formatTime(self, record, datefmt=None):
            dt = datetime.fromtimestamp(record.created, tz=self.tz)
            if datefmt:
                return dt.strftime(datefmt)
            return dt.isoformat()

    # 根 logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # 清理已有 handler
    root_logger.handlers.clear()

    formatter = TZFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        tz=timezone,
    )

    # 控制台
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # 文件
    log_file = os.path.join(log_dir, f"arb_{ticker}_activity.log")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # 抑制第三方库日志
    for lib in ["urllib3", "requests", "websockets", "aiohttp", "paradex_py", "curl_cffi"]:
        logging.getLogger(lib).setLevel(logging.WARNING)

    return root_logger
