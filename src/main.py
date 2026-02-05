#!/usr/bin/env python3
"""
XWatch - X/Twitter Tweet Monitor
监控指定 Twitter 用户的推文并发送通知到 Telegram
支持多用户监控
"""

import time
import signal
import logging
from pathlib import Path
from typing import Dict, List

from src.config import get_config_manager, Config
from src.fetcher import TweetFetcher
from src.notifier import TelegramNotifier
from src.state import StateStore


# 配置日志格式
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("xwatch")


class XTweetMonitor:
    """X 推文监控器 - 支持多用户"""

    def __init__(self):
        self.running = False
        self.config_manager = get_config_manager()

        # 每个用户一个 fetcher
        self.fetchers: Dict[str, TweetFetcher] = {}

        config = self.config_manager.config
        self.state_store = StateStore(Path(config.state_file))
        self._init_fetchers(config.twitter_usernames)

        self.notifier = TelegramNotifier(
            config.telegram_bot_token, config.telegram_chat_id
        )

        # 注册配置变更回调
        self.config_manager.on_config_change(self._on_config_change)

        # 设置信号处理
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _init_fetchers(self, usernames: List[str]):
        """初始化用户抓取器"""
        # 移除不再监控的用户
        current_users = set(self.fetchers.keys())
        new_users = set(usernames)

        for user in current_users - new_users:
            logger.info(f"移除监控用户: @{user}")
            del self.fetchers[user]

        # 添加新用户
        for user in new_users - current_users:
            logger.info(f"添加监控用户: @{user}")
            config = self.config_manager.config
            self.fetchers[user] = TweetFetcher(
                user,
                self.state_store,
                min_user_interval=config.min_user_interval,
                global_min_request_interval=config.global_min_request_interval,
                rate_limit_backoff_max=config.rate_limit_backoff_max,
                rsshub_enabled=config.rsshub_enabled,
                rsshub_base_url=config.rsshub_base_url,
                rsshub_timeout=config.rsshub_timeout,
            )

    def _signal_handler(self, signum, frame):
        """处理退出信号"""
        logger.info("收到退出信号，正在停止...")
        self.running = False

    def _on_config_change(self, config: Config):
        """配置变更回调"""
        logger.info("配置已更新")

        # 更新状态存储路径（如有变化）
        if Path(config.state_file) != self.state_store.path:
            logger.info(f"状态文件路径已更新: {self.state_store.path} -> {config.state_file}")
            self.state_store = StateStore(Path(config.state_file))
            for fetcher in self.fetchers.values():
                fetcher.update_state_store(self.state_store)

        # 更新 fetchers
        self._init_fetchers(config.twitter_usernames)

        # 更新速率限制参数
        for fetcher in self.fetchers.values():
            fetcher.update_rate_limits(
                min_user_interval=config.min_user_interval,
                global_min_request_interval=config.global_min_request_interval,
                rate_limit_backoff_max=config.rate_limit_backoff_max,
            )
            fetcher.update_rsshub_config(
                enabled=config.rsshub_enabled,
                base_url=config.rsshub_base_url,
                timeout=config.rsshub_timeout,
            )

        # 更新 notifier
        self.notifier.update_config(config.telegram_bot_token, config.telegram_chat_id)

        # 发送通知
        try:
            self.notifier.send_config_reload_message()
        except Exception as e:
            logger.warning(f"发送配置重载通知失败: {e}")

    def start(self):
        """启动监控"""
        config = self.config_manager.config

        logger.info("=" * 50)
        logger.info("XWatch - X/Twitter Tweet Monitor")
        logger.info("=" * 50)
        logger.info(
            f"监控用户: {', '.join(['@' + u for u in config.twitter_usernames])}"
        )
        logger.info(f"检查间隔: {config.check_interval} 秒")

        # 检查配置
        if not config.is_valid():
            logger.error(
                "配置无效，请检查 .env 文件中的 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID"
            )
            return

        # 启动配置热更新
        self.config_manager.start_watching()

        # 初始化
        if not config.send_existing_on_start:
            logger.info("初始化已有推文记录...")
            for username, fetcher in self.fetchers.items():
                logger.info(f"  初始化 @{username}...")
                fetcher.initialize_seen_tweets()

        # 发送启动通知
        try:
            users_str = ", ".join(["@" + u for u in config.twitter_usernames])
            if self.notifier.send_startup_message(users_str):
                logger.info("✅ Telegram 通知已连接")
            else:
                logger.warning("⚠️ Telegram 通知发送失败")
        except Exception as e:
            logger.error(f"Telegram 连接失败: {e}")
            return

        self.running = True
        logger.info("开始监控...")

        # 主循环
        while self.running:
            try:
                self._check_new_tweets()

                # 动态获取检查间隔（支持热更新）
                interval = self.config_manager.config.check_interval

                # 分段等待以便快速响应退出信号
                for _ in range(interval):
                    if not self.running:
                        break
                    time.sleep(1)

            except Exception as e:
                logger.error(f"发生错误: {e}")
                time.sleep(5)

        # 清理
        self._cleanup()

    def _check_new_tweets(self):
        """检查所有用户的新推文"""
        import random

        # 随机打乱用户顺序，避免每次都按相同顺序检查
        usernames = list(self.fetchers.keys())
        random.shuffle(usernames)

        for username in usernames:
            fetcher = self.fetchers[username]
            try:
                # 用户之间随机间隔 2-8 秒，避免短时间并发请求
                time.sleep(random.uniform(2, 8))

                new_tweets = fetcher.get_new_tweets()

                if new_tweets:
                    logger.info(f"[@{username}] 发现 {len(new_tweets)} 条新推文")

                    for tweet in new_tweets:
                        logger.info(f"  📝 {tweet['text'][:80]}...")

                        try:
                            if self.notifier.send_tweet_notification(tweet):
                                logger.info("  ✅ 已发送通知")
                            else:
                                logger.warning("  ❌ 通知发送失败")
                        except Exception as e:
                            logger.error(f"  发送通知异常: {e}")

                        time.sleep(1)  # 避免发送过快

            except Exception as e:
                logger.error(f"[@{username}] 检查推文失败: {e}")

    def _cleanup(self):
        """清理资源"""
        logger.info("正在清理资源...")

        # 停止配置监听
        self.config_manager.stop_watching()

        # 发送停止通知
        try:
            self.notifier.send_shutdown_message()
        except Exception as e:
            logger.warning(f"发送停止通知失败: {e}")

        logger.info("监控已停止")


def main():
    """主函数"""
    monitor = XTweetMonitor()
    monitor.start()


if __name__ == "__main__":
    main()
