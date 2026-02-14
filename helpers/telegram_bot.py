"""
Telegram 通知模块
参考: perp-dex-tools/helpers/telegram_bot.py
"""

import logging
import os
from typing import Optional

import requests
import certifi

logger = logging.getLogger("telegram")


class TelegramNotifier:
    """Telegram 消息通知"""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        account_label: str = "",
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.account_label = account_label
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

        self.session = requests.Session()
        self.session.verify = certifi.where()
        self.session.timeout = 10

    def send(self, message: str, parse_mode: str = "HTML") -> bool:
        """发送消息"""
        try:
            # 添加账号标签前缀
            if self.account_label:
                message = f"[{self.account_label}] {message}"

            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode,
            }

            url = f"{self.base_url}/sendMessage"
            resp = self.session.post(url, json=payload)
            data = resp.json()

            if not data.get("ok", False):
                logger.warning(f"Telegram 发送失败: {data}")
                return False
            return True

        except Exception as e:
            logger.warning(f"Telegram 发送异常: {e}")
            return False

    def close(self):
        """关闭 session"""
        if self.session:
            self.session.close()


def create_telegram_notifier() -> Optional[TelegramNotifier]:
    """从环境变量创建 Telegram 通知器（如果配置了的话）"""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_GROUP_ID", "")
    label = os.getenv("ACCOUNT_LABEL", "")

    if bot_token and chat_id:
        return TelegramNotifier(bot_token, chat_id, label)
    return None
