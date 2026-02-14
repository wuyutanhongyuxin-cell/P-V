#!/usr/bin/env python3
"""
Paradex × Variational 跨所价差套利脚本
主入口 — 命令行参数解析 → 配置加载 → 创建客户端 → 启动套利引擎

用法:
  python main.py --ticker BTC --size 0.001 --max-position 0.01

依赖:
  pip install -r requirements.txt
"""

import argparse
import asyncio
import sys
from decimal import Decimal

from config import AppConfig
from exchanges.paradex_client import ParadexInteractiveClient
from exchanges.variational_client import VariationalClient
from helpers.logger import setup_logging
from helpers.telegram_bot import create_telegram_notifier
from strategy.arbitrage_engine import ArbitrageEngine


def parse_arguments():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Paradex × Variational 跨所价差套利脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py --ticker BTC --size 0.001 --max-position 0.01
  python main.py --ticker ETH --size 0.01 --max-position 0.1 --long-threshold 5
  python main.py --ticker BTC --size 0.001 --max-position 0.01 --variational-auth-mode siwe
        """,
    )

    parser.add_argument(
        "--ticker", type=str, default="BTC", help="交易对符号，如 BTC、ETH (默认: BTC)"
    )
    parser.add_argument(
        "--size", type=Decimal, required=True, help="每笔订单的交易数量"
    )
    parser.add_argument(
        "--max-position", type=Decimal, required=True, help="最大持仓限制（单边绝对值）"
    )
    parser.add_argument(
        "--long-threshold",
        type=Decimal,
        default=Decimal("10"),
        help="做多价差触发偏移量 (默认: 10)",
    )
    parser.add_argument(
        "--short-threshold",
        type=Decimal,
        default=Decimal("10"),
        help="做空价差触发偏移量 (默认: 10)",
    )
    parser.add_argument(
        "--min-spread",
        type=Decimal,
        default=Decimal("5"),
        help="最低价差绝对阈值 (防止均值为负时误触发) (默认: 5)",
    )
    parser.add_argument(
        "--fill-timeout",
        type=int,
        default=5,
        help="限价单成交超时（秒）(默认: 5)",
    )
    parser.add_argument(
        "--min-balance",
        type=Decimal,
        default=Decimal("10"),
        help="最低余额阈值 (USDC)，低于此值将平仓退出 (默认: 10)",
    )
    parser.add_argument(
        "--warmup-samples",
        type=int,
        default=100,
        help="预热采样数量 (默认: 100)",
    )
    parser.add_argument(
        "--env-file",
        type=str,
        default=".env",
        help=".env 文件路径 (默认: .env)",
    )
    parser.add_argument(
        "--variational-auth-mode",
        type=str,
        default="cookie",
        choices=["cookie", "siwe"],
        help="Variational 认证模式 (默认: cookie)",
    )

    return parser.parse_args()


async def main():
    """主函数"""
    args = parse_arguments()

    # 设置日志
    setup_logging(ticker=args.ticker)

    # 加载配置
    config = AppConfig.load(env_file=args.env_file)
    config.variational_auth_mode = args.variational_auth_mode
    config.trading.ticker = args.ticker.upper()
    config.trading.size = args.size
    config.trading.max_position = args.max_position
    config.trading.long_threshold = args.long_threshold
    config.trading.short_threshold = args.short_threshold
    config.trading.min_spread = args.min_spread
    config.trading.fill_timeout = args.fill_timeout
    config.trading.min_balance = args.min_balance
    config.trading.warmup_samples = args.warmup_samples

    # 验证配置
    try:
        config.validate()
    except ValueError as e:
        print(f"配置错误: {e}")
        sys.exit(1)

    # 创建 Paradex 客户端 (Interactive Token 零手续费)
    paradex = ParadexInteractiveClient(
        l2_private_key=config.paradex.l2_private_key,
        l2_address=config.paradex.l2_address,
        environment=config.paradex.environment,
    )

    # 创建 Variational 客户端 (curl_cffi + Cookie 认证)
    variational = VariationalClient(
        vr_token=config.variational.vr_token,
        wallet_address=config.variational.wallet_address,
        cookies=config.variational.cookies,
        private_key=config.variational.private_key,
        base_url=config.variational.base_url,
        auth_mode=config.variational_auth_mode,
    )

    # 创建 Telegram 通知器（可选）
    telegram = create_telegram_notifier()

    # 创建并运行套利引擎
    engine = ArbitrageEngine(
        paradex=paradex,
        variational=variational,
        paradex_market=config.trading.paradex_market,
        variational_market=config.trading.variational_market,
        ticker=config.trading.ticker,
        size=config.trading.size,
        max_position=config.trading.max_position,
        long_threshold=config.trading.long_threshold,
        short_threshold=config.trading.short_threshold,
        min_spread=config.trading.min_spread,
        fill_timeout=config.trading.fill_timeout,
        min_balance=config.trading.min_balance,
        warmup_samples=config.trading.warmup_samples,
        telegram=telegram,
    )

    await engine.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序已退出")
