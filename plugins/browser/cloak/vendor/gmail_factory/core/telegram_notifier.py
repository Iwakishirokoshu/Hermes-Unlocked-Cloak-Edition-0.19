"""
Telegram Notifier - Send notifications about account creation status
"""
import logging
import requests

logger = logging.getLogger('gmail_creator_telegram')


class TelegramNotifier:
    def __init__(self, bot_token=None, chat_id=None):
        from config.settings import Config
        self.bot_token = bot_token or getattr(Config, 'TELEGRAM_BOT_TOKEN', '')
        self.chat_id = chat_id or getattr(Config, 'TELEGRAM_CHAT_ID', '')
        self.enabled = bool(self.bot_token and self.chat_id)

    def send(self, message, silent=False):
        if not self.enabled:
            return False
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            resp = requests.post(url, json={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_notification": silent,
            }, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")
            return False

    def notify_account_created(self, email, password, strategy="", proxy=""):
        msg = (
            f"<b>Account Created</b>\n"
            f"<b>Email:</b> <code>{email}</code>\n"
            f"<b>Password:</b> <code>{password}</code>\n"
        )
        if strategy:
            msg += f"<b>Strategy:</b> {strategy}\n"
        if proxy:
            msg += f"<b>Proxy:</b> {proxy}\n"
        return self.send(msg)

    def notify_account_failed(self, username, error_type="", strategy=""):
        msg = (
            f"<b>Account Failed</b>\n"
            f"<b>Username:</b> {username}\n"
        )
        if error_type:
            msg += f"<b>Error:</b> {error_type}\n"
        if strategy:
            msg += f"<b>Strategy:</b> {strategy}\n"
        return self.send(msg, silent=True)

    def notify_batch_complete(self, total, successes, failures, duration):
        rate = (successes / total * 100) if total > 0 else 0
        msg = (
            f"<b>Batch Complete</b>\n"
            f"Total: {total} | Success: {successes} | Failed: {failures}\n"
            f"Rate: {rate:.1f}% | Duration: {duration:.0f}s"
        )
        return self.send(msg)

    def test_connection(self):
        if not self.enabled:
            return False, "Bot token or chat ID not configured"
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/getMe"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                bot_name = resp.json().get("result", {}).get("username", "Unknown")
                return True, f"Connected to @{bot_name}"
            return False, f"API error: {resp.status_code}"
        except Exception as e:
            return False, str(e)


notifier = TelegramNotifier()
