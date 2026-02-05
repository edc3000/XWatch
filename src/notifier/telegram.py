"""
Telegram Notifier Module
发送推文通知到 Telegram
"""

import asyncio
import logging
from typing import Dict, Optional, List
from email.utils import parsedate_to_datetime
from datetime import timezone
from zoneinfo import ZoneInfo

from telegram import Bot, InputMediaPhoto
from telegram.error import TelegramError


logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Telegram 通知器"""

    CAPTION_LIMIT = 1024
    BEIJING_TZ = ZoneInfo("Asia/Shanghai")

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._bot: Optional[Bot] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def bot(self) -> Bot:
        """延迟初始化 Bot"""
        if self._bot is None:
            self._bot = Bot(token=self.bot_token)
        return self._bot

    def update_config(self, bot_token: str, chat_id: str):
        """更新配置"""
        if bot_token != self.bot_token:
            self.bot_token = bot_token
            self._bot = None  # 重新初始化
            logger.info("Telegram Bot Token 已更新")

        if chat_id != self.chat_id:
            self.chat_id = chat_id
            logger.info("Telegram Chat ID 已更新")

    def format_tweet_message(self, tweet: Dict) -> str:
        """格式化推文为消息"""
        # 转义用户名和文本
        user = self._escape_markdown(tweet["user"])
        text = self._escape_markdown(tweet["text"])

        message = f"""🐦 *@{user}* 发布了新推文

{text}

🔗 [查看原文]({tweet["url"]})
"""
        if tweet.get("created_at"):
            formatted = self._format_created_at(tweet["created_at"])
            # 转义时间中的特殊字符（如 - 和 .）
            created_at = self._escape_markdown(formatted)
            message += f"\n⏰ {created_at}"

        return message

    def _format_created_at(self, created_at: str) -> str:
        """将推文时间格式化为北京时间（YYYY年MM月DD日HH时MM分）"""
        try:
            dt = parsedate_to_datetime(created_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(self.BEIJING_TZ)
            return dt.strftime("%Y年%m月%d日%H时%M分")
        except Exception:
            return created_at

    def _escape_markdown(self, text: str) -> str:
        """转义 MarkdownV2 特殊字符"""
        # MarkdownV2 需要转义的字符列表
        escape_chars = [
            "_",
            "*",
            "[",
            "]",
            "(",
            ")",
            "~",
            "`",
            ">",
            "#",
            "+",
            "-",
            "=",
            "|",
            "{",
            "}",
            ".",
            "!",
        ]
        for char in escape_chars:
            text = text.replace(char, f"\\{char}")
        return text

    def _get_event_loop(self) -> asyncio.AbstractEventLoop:
        """获取或创建事件循环"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("Event loop is closed")
            return loop
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

    async def _send_message_async(
        self, text: str, parse_mode: str = "MarkdownV2"
    ) -> bool:
        """异步发送消息"""
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=False,
            )
            return True
        except TelegramError as e:
            logger.error(f"Telegram ({parse_mode}) 发送失败: {e}")
            # 如果 Markdown 解析失败，尝试纯文本
            if parse_mode == "MarkdownV2":
                try:
                    logger.info("尝试降级为纯文本发送...")
                    await self.bot.send_message(
                        chat_id=self.chat_id, text=text, disable_web_page_preview=False
                    )
                    return True
                except TelegramError as e2:
                    logger.error(f"Telegram 纯文本发送也失败: {e2}")
            return False

    async def _send_media_item_async(
        self, media: Dict, caption: Optional[str] = None
    ) -> bool:
        """发送单个媒体"""
        media_type = media.get("type")
        url = media.get("url")
        if not url or not media_type:
            return False

        try:
            if media_type == "photo":
                await self.bot.send_photo(
                    chat_id=self.chat_id,
                    photo=url,
                    caption=caption,
                    parse_mode="MarkdownV2" if caption else None,
                )
            elif media_type == "video":
                await self.bot.send_video(
                    chat_id=self.chat_id,
                    video=url,
                    caption=caption,
                    parse_mode="MarkdownV2" if caption else None,
                    supports_streaming=True,
                )
            else:
                return False
            return True
        except TelegramError as e:
            logger.error(f"Telegram 媒体发送失败: {e}")
            if caption:
                try:
                    if media_type == "photo":
                        await self.bot.send_photo(
                            chat_id=self.chat_id,
                            photo=url,
                            caption=caption,
                        )
                    elif media_type == "video":
                        await self.bot.send_video(
                            chat_id=self.chat_id,
                            video=url,
                            caption=caption,
                            supports_streaming=True,
                        )
                    return True
                except TelegramError as e2:
                    logger.error(f"Telegram 媒体纯文本发送也失败: {e2}")
            return False

    async def _send_media_group_async(
        self, media_list: List[Dict], caption: Optional[str] = None
    ) -> bool:
        """发送媒体组（仅图片）"""
        if not media_list:
            return False

        media_group = []
        for idx, media in enumerate(media_list):
            url = media.get("url")
            if not url:
                continue
            item_caption = caption if idx == 0 else None
            media_group.append(
                InputMediaPhoto(
                    media=url,
                    caption=item_caption,
                    parse_mode="MarkdownV2" if item_caption else None,
                )
            )

        if not media_group:
            return False

        try:
            await self.bot.send_media_group(chat_id=self.chat_id, media=media_group)
            return True
        except TelegramError as e:
            logger.error(f"Telegram 媒体组发送失败: {e}")
            return False

    def send_message(self, text: str) -> bool:
        """同步发送消息"""
        loop = self._get_event_loop()
        return loop.run_until_complete(self._send_message_async(text))

    def send_tweet_notification(self, tweet: Dict) -> bool:
        """发送推文通知"""
        message = self.format_tweet_message(tweet)
        media_list: List[Dict] = tweet.get("media", []) or []

        if not media_list:
            return self.send_message(message)

        loop = self._get_event_loop()

        # 如果消息太长，先发文本，再发媒体
        if len(message) > self.CAPTION_LIMIT:
            ok = loop.run_until_complete(self._send_message_async(message))
            for media in media_list:
                ok = loop.run_until_complete(self._send_media_item_async(media)) and ok
            return ok

        # 单个媒体：直接用 caption
        if len(media_list) == 1:
            return loop.run_until_complete(
                self._send_media_item_async(media_list[0], caption=message)
            )

        # 多张图片：发送媒体组
        if all(m.get("type") == "photo" for m in media_list):
            sent = loop.run_until_complete(
                self._send_media_group_async(media_list, caption=message)
            )
            if sent:
                return True

        # 混合媒体：先发文本，再逐个发媒体
        ok = loop.run_until_complete(self._send_message_async(message))
        for media in media_list:
            ok = loop.run_until_complete(self._send_media_item_async(media)) and ok
        return ok

    def send_startup_message(self, username: str) -> bool:
        """发送启动通知"""
        # 转义用户名字符串（可能包含多个用户）
        escaped_username = self._escape_markdown(username)
        message = f"🚀 *XWatch 已启动*\n\n正在监控: {escaped_username}"
        return self.send_message(message)

    def send_shutdown_message(self) -> bool:
        """发送关闭通知"""
        message = "🛑 *XWatch 已停止*"
        return self.send_message(message)

    def send_config_reload_message(self) -> bool:
        """发送配置重载通知"""
        message = "🔄 *配置已重新加载*"
        return self.send_message(message)


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv

    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if token and chat_id:
        notifier = TelegramNotifier(token, chat_id)
        notifier.send_message("🧪 XWatch 测试消息")
        print("测试消息已发送")
    else:
        print("请先配置 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID")
