"""
配置管理模块
负责加载环境变量和管理全局配置
"""

import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Dict

from dotenv import load_dotenv


# 交易对名称映射：ticker -> (Paradex合约名, Variational合约名)
MARKET_MAPPING: Dict[str, Dict[str, str]] = {
    "BTC": {"paradex": "BTC-USD-PERP", "variational": "BTC"},
    "ETH": {"paradex": "ETH-USD-PERP", "variational": "ETH"},
    "SOL": {"paradex": "SOL-USD-PERP", "variational": "SOL"},
    "ARB": {"paradex": "ARB-USD-PERP", "variational": "ARB"},
    "DOGE": {"paradex": "DOGE-USD-PERP", "variational": "DOGE"},
    "AVAX": {"paradex": "AVAX-USD-PERP", "variational": "AVAX"},
    "LINK": {"paradex": "LINK-USD-PERP", "variational": "LINK"},
    "OP": {"paradex": "OP-USD-PERP", "variational": "OP"},
    "WIF": {"paradex": "WIF-USD-PERP", "variational": "WIF"},
    "PEPE": {"paradex": "PEPE-USD-PERP", "variational": "PEPE"},
}


@dataclass
class ParadexConfig:
    """Paradex 交易所配置"""
    l2_private_key: str = ""
    l2_address: str = ""
    environment: str = "prod"

    @classmethod
    def from_env(cls) -> "ParadexConfig":
        return cls(
            l2_private_key=os.getenv("PARADEX_L2_PRIVATE_KEY", ""),
            l2_address=os.getenv("PARADEX_L2_ADDRESS", ""),
            environment=os.getenv("PARADEX_ENVIRONMENT", "prod"),
        )

    def validate(self):
        if not self.l2_private_key:
            raise ValueError("PARADEX_L2_PRIVATE_KEY 未设置")
        if not self.l2_address:
            raise ValueError("PARADEX_L2_ADDRESS 未设置")


@dataclass
class VariationalConfig:
    """Variational 交易所配置"""
    vr_token: str = ""
    wallet_address: str = ""
    cookies: str = ""
    private_key: str = ""
    base_url: str = "https://omni.variational.io"
    auth_mode: str = "cookie"

    @classmethod
    def from_env(cls) -> "VariationalConfig":
        return cls(
            vr_token=os.getenv("VARIATIONAL_VR_TOKEN", ""),
            wallet_address=os.getenv("VARIATIONAL_WALLET_ADDRESS", ""),
            cookies=os.getenv("VARIATIONAL_COOKIES", ""),
            private_key=os.getenv("VARIATIONAL_PRIVATE_KEY", ""),
            base_url=os.getenv(
                "VARIATIONAL_BASE_URL", "https://omni.variational.io"
            ),
        )

    def validate(self, auth_mode: str):
        self.auth_mode = auth_mode
        if auth_mode == "cookie":
            if not self.vr_token and not self.cookies:
                raise ValueError(
                    "Cookie 模式需要设置 VARIATIONAL_VR_TOKEN 或 VARIATIONAL_COOKIES\n"
                    "从浏览器 F12 控制台执行 document.cookie 获取"
                )
            if not self.wallet_address:
                raise ValueError(
                    "需要设置 VARIATIONAL_WALLET_ADDRESS\n"
                    "你在 Variational 上连接的钱包地址"
                )


@dataclass
class TelegramConfig:
    """Telegram 通知配置"""
    bot_token: str = ""
    group_id: str = ""
    account_label: str = ""
    enabled: bool = False

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        group_id = os.getenv("TELEGRAM_GROUP_ID", "")
        return cls(
            bot_token=bot_token,
            group_id=group_id,
            account_label=os.getenv("ACCOUNT_LABEL", "A1"),
            enabled=bool(bot_token and group_id),
        )


@dataclass
class TradingConfig:
    """交易配置"""
    ticker: str = "BTC"
    size: Decimal = Decimal("0.001")
    max_position: Decimal = Decimal("0.01")
    long_threshold: Decimal = Decimal("10")
    short_threshold: Decimal = Decimal("10")
    fill_timeout: int = 5
    min_balance: Decimal = Decimal("10")
    warmup_samples: int = 100

    # 自动从映射表获取合约名
    paradex_market: str = ""
    variational_market: str = ""

    def resolve_markets(self):
        """根据 ticker 解析两边的合约名称"""
        # 优先使用环境变量中的自定义映射
        env_paradex = os.getenv("PARADEX_MARKET", "")
        env_variational = os.getenv("VARIATIONAL_MARKET", "")

        if env_paradex:
            self.paradex_market = env_paradex
        elif self.ticker in MARKET_MAPPING:
            self.paradex_market = MARKET_MAPPING[self.ticker]["paradex"]
        else:
            self.paradex_market = f"{self.ticker}-USD-PERP"

        if env_variational:
            self.variational_market = env_variational
        elif self.ticker in MARKET_MAPPING:
            self.variational_market = MARKET_MAPPING[self.ticker]["variational"]
        else:
            self.variational_market = self.ticker


@dataclass
class AppConfig:
    """应用总配置"""
    paradex: ParadexConfig = field(default_factory=ParadexConfig)
    variational: VariationalConfig = field(default_factory=VariationalConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    variational_auth_mode: str = "cookie"

    @classmethod
    def load(cls, env_file: str = ".env") -> "AppConfig":
        """从环境变量加载配置"""
        load_dotenv(env_file)
        config = cls(
            paradex=ParadexConfig.from_env(),
            variational=VariationalConfig.from_env(),
            telegram=TelegramConfig.from_env(),
        )
        return config

    def validate(self):
        """验证所有必要配置"""
        self.paradex.validate()
        self.variational.validate(self.variational_auth_mode)
        self.trading.resolve_markets()
