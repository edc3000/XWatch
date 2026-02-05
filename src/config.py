"""
Configuration Management Module
支持从环境变量和 .env 文件加载配置，并支持热更新
"""

import os
import logging
from pathlib import Path
from typing import Any, Callable, List, Optional
from dataclasses import dataclass, field
from threading import Lock

from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent


logger = logging.getLogger(__name__)


@dataclass
class Config:
    """配置类"""

    # Twitter 配置 - 支持多个用户（逗号分隔）
    twitter_usernames: List[str] = field(default_factory=lambda: ["Vito777_"])

    # Telegram 配置
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # 运行配置
    check_interval: int = 30
    send_existing_on_start: bool = False
    log_level: str = "INFO"
    state_file: str = "data/seen_tweets.json"
    min_user_interval: int = 60
    global_min_request_interval: float = 2.0
    rate_limit_backoff_max: int = 300
    rsshub_enabled: bool = False
    rsshub_base_url: str = ""
    rsshub_timeout: int = 15

    def is_valid(self) -> bool:
        """检查配置是否有效"""
        return bool(
            self.twitter_usernames
            and self.telegram_bot_token
            and self.telegram_chat_id
            and self.telegram_bot_token != "your_bot_token_here"
            and self.telegram_chat_id != "your_chat_id_here"
        )


class ConfigManager:
    """配置管理器，支持热更新"""

    def __init__(self, env_file: Optional[Path] = None):
        self._config = Config()
        self._lock = Lock()
        self._callbacks: list[Callable[[Config], None]] = []
        self._observer: Optional[Observer] = None

        # 确定 .env 文件路径
        if env_file:
            self._env_file = env_file
        else:
            # 查找项目根目录的 .env 文件
            self._env_file = self._find_env_file()

        # 初始加载配置
        self._load_config()

    def _find_env_file(self) -> Path:
        """查找 .env 文件"""
        # 从当前文件向上查找
        current = Path(__file__).parent
        while current != current.parent:
            env_path = current / ".env"
            if env_path.exists():
                return env_path
            current = current.parent

        # 默认使用项目根目录
        return Path(__file__).parent.parent / ".env"

    def _load_config(self):
        """加载配置"""
        # 加载 .env 文件
        if self._env_file.exists():
            load_dotenv(self._env_file, override=True)
            logger.info(f"已加载配置文件: {self._env_file}")

        with self._lock:
            # 解析用户名列表（支持逗号分隔）
            usernames_str = os.getenv("TWITTER_USERNAMES", "Vito777_")
            usernames = [u.strip() for u in usernames_str.split(",") if u.strip()]

            self._config = Config(
                twitter_usernames=usernames,
                telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
                telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
                check_interval=int(os.getenv("CHECK_INTERVAL", "30")),
                send_existing_on_start=os.getenv(
                    "SEND_EXISTING_ON_START", "false"
                ).lower()
                == "true",
                log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
                state_file=os.getenv("STATE_FILE", "data/seen_tweets.json"),
                min_user_interval=int(os.getenv("MIN_USER_INTERVAL", "60")),
                global_min_request_interval=float(
                    os.getenv("GLOBAL_MIN_REQUEST_INTERVAL", "2.0")
                ),
                rate_limit_backoff_max=int(
                    os.getenv("RATE_LIMIT_BACKOFF_MAX", "300")
                ),
                rsshub_enabled=os.getenv("RSSHUB_ENABLED", "false").lower() == "true",
                rsshub_base_url=os.getenv("RSSHUB_BASE_URL", "").strip(),
                rsshub_timeout=int(os.getenv("RSSHUB_TIMEOUT", "15")),
            )

        # 配置日志级别
        logging.getLogger().setLevel(
            getattr(logging, self._config.log_level, logging.INFO)
        )

    @property
    def config(self) -> Config:
        """获取当前配置"""
        with self._lock:
            return self._config

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
        with self._lock:
            return getattr(self._config, key, default)

    def on_config_change(self, callback: Callable[[Config], None]):
        """注册配置变更回调"""
        self._callbacks.append(callback)

    def _notify_callbacks(self):
        """通知所有回调"""
        config = self.config
        for callback in self._callbacks:
            try:
                callback(config)
            except Exception as e:
                logger.error(f"配置变更回调执行失败: {e}")

    def start_watching(self):
        """开始监听配置文件变化"""
        if not self._env_file.exists():
            logger.warning(f"配置文件不存在，无法启用热更新: {self._env_file}")
            return

        class ConfigFileHandler(FileSystemEventHandler):
            def __init__(self, manager: "ConfigManager"):
                self.manager = manager

            def on_modified(self, event):
                if isinstance(event, FileModifiedEvent):
                    if Path(event.src_path).name == ".env":
                        logger.info("检测到配置文件变更，重新加载...")
                        self.manager._load_config()
                        self.manager._notify_callbacks()

        self._observer = Observer()
        self._observer.schedule(
            ConfigFileHandler(self), str(self._env_file.parent), recursive=False
        )
        self._observer.start()
        logger.info("配置热更新已启用")

    def stop_watching(self):
        """停止监听配置文件"""
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
            logger.info("配置热更新已停止")


# 全局配置管理器实例
_config_manager: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    """获取全局配置管理器"""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager


def get_config() -> Config:
    """获取当前配置"""
    return get_config_manager().config


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)

    manager = get_config_manager()
    config = manager.config

    print(f"Twitter 用户: {', '.join(config.twitter_usernames)}")
    print(
        f"Telegram Token: {config.telegram_bot_token[:10]}..."
        if config.telegram_bot_token
        else "未配置"
    )
    print(f"检查间隔: {config.check_interval}s")
    print(f"配置有效: {config.is_valid()}")
