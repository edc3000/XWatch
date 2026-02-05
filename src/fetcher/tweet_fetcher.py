"""
Tweet Fetcher Module
使用 Twitter Syndication API 获取用户推文
"""

import re
import json
import logging
import time
from threading import Lock
from typing import List, Dict, Optional

import requests
import feedparser

from src.state import StateStore


logger = logging.getLogger(__name__)


class TweetFetcher:
    """推文抓取器"""

    SYNDICATION_URL = (
        "https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
    )
    _request_lock = Lock()
    _last_request_time = 0.0
    _global_backoff_until = 0.0

    def __init__(
        self,
        username: str,
        state_store: Optional[StateStore] = None,
        min_user_interval: int = 60,
        global_min_request_interval: float = 2.0,
        rate_limit_backoff_max: int = 300,
        rsshub_enabled: bool = False,
        rsshub_base_url: str = "",
        rsshub_timeout: int = 15,
    ):
        self.username = username
        self.seen_tweet_ids: set = set()
        self.last_seen_id: Optional[str] = None
        self.session = requests.Session()
        self.state_store = state_store
        self.min_user_interval = max(5, int(min_user_interval))
        self.global_min_request_interval = max(0.0, float(global_min_request_interval))
        self.rate_limit_backoff_max = max(30, int(rate_limit_backoff_max))
        self.rsshub_enabled = bool(rsshub_enabled)
        self.rsshub_base_url = rsshub_base_url.rstrip("/")
        self.rsshub_timeout = max(5, int(rsshub_timeout))
        self.last_fetch_at = 0.0
        self.backoff_until = 0.0
        self.consecutive_429 = 0
        self._rotate_user_agent()
        self._load_state()

    def _rotate_user_agent(self):
        """随机切换 User-Agent"""
        import random

        user_agents = [
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]
        self.session.headers.update(
            {
                "User-Agent": random.choice(user_agents),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            }
        )

    def update_username(self, username: str):
        """更新监控的用户名"""
        if username != self.username:
            logger.info(f"更新监控用户: {self.username} -> {username}")
            self.username = username
            self.seen_tweet_ids.clear()
            self.last_seen_id = None
            self._load_state()

    def update_state_store(self, state_store: Optional[StateStore]):
        """更新状态存储并重新加载状态"""
        self.state_store = state_store
        self.last_seen_id = None
        self._load_state()

    def update_rate_limits(
        self,
        min_user_interval: int,
        global_min_request_interval: float,
        rate_limit_backoff_max: int,
    ):
        """更新速率限制参数"""
        self.min_user_interval = max(5, int(min_user_interval))
        self.global_min_request_interval = max(0.0, float(global_min_request_interval))
        self.rate_limit_backoff_max = max(30, int(rate_limit_backoff_max))

    def update_rsshub_config(
        self, enabled: bool, base_url: str, timeout: int
    ):
        """更新 RSSHub 配置"""
        self.rsshub_enabled = bool(enabled)
        self.rsshub_base_url = base_url.rstrip("/")
        self.rsshub_timeout = max(5, int(timeout))

    def _load_state(self):
        """加载持久化状态"""
        if not self.state_store:
            return
        last_seen_id = self.state_store.get_last_seen_id(self.username)
        if last_seen_id:
            self.last_seen_id = last_seen_id
            self.seen_tweet_ids.add(last_seen_id)
            logger.info(f"[@{self.username}] 已加载上次记录的推文 ID: {last_seen_id}")

    def _persist_last_seen(self):
        """持久化最近的推文 ID"""
        if self.state_store and self.last_seen_id:
            self.state_store.set_last_seen_id(self.username, self.last_seen_id)

    def _wait_for_global_slot(self):
        """全局请求节流，避免短时间内过多请求"""
        if self.global_min_request_interval <= 0:
            return
        with TweetFetcher._request_lock:
            now = time.time()
            elapsed = now - TweetFetcher._last_request_time
            if elapsed < self.global_min_request_interval:
                time.sleep(self.global_min_request_interval - elapsed)
            TweetFetcher._last_request_time = time.time()

    def fetch_tweets(self) -> List[Dict]:
        """获取用户推文列表（带重试机制）"""
        import random

        max_retries = 3
        base_delay = 15  # 429 基础等待时间（秒）

        now = time.time()
        if (
            TweetFetcher._global_backoff_until
            and now < TweetFetcher._global_backoff_until
        ):
            return self._fetch_tweets_rsshub()

        if self.backoff_until and now < self.backoff_until:
            logger.warning(
                f"[@{self.username}] 处于限流冷却期，跳过请求（剩余 {self.backoff_until - now:.1f}s）"
            )
            return self._fetch_tweets_rsshub()

        if self.last_fetch_at and now - self.last_fetch_at < self.min_user_interval:
            return []

        for attempt in range(max_retries):
            try:
                # 每次请求前随机等待 1-3 秒，避免过于规律
                time.sleep(random.uniform(1, 3))
                self._wait_for_global_slot()

                url = self.SYNDICATION_URL.format(username=self.username)
                response = self.session.get(url, timeout=30)

                if response.status_code == 429:
                    # 触发限流，设置冷却时间并停止本次请求
                    self.consecutive_429 += 1
                    wait_time = base_delay * (2 ** (self.consecutive_429 - 1)) + random.uniform(0, 5)
                    wait_time = min(wait_time, self.rate_limit_backoff_max)
                    self.backoff_until = time.time() + wait_time
                    TweetFetcher._global_backoff_until = self.backoff_until
                    self.last_fetch_at = time.time()
                    logger.warning(
                        f"[@{self.username}] 触发 429 限流，进入冷却 {wait_time:.1f} 秒"
                    )
                    self._rotate_user_agent()  # 切换 UA
                    return self._fetch_tweets_rsshub()

                response.raise_for_status()

                tweets = self._parse_tweets(response.text)
                logger.debug(f"获取到 {len(tweets)} 条推文")
                self.last_fetch_at = time.time()
                self.consecutive_429 = 0
                self.backoff_until = 0.0
                return tweets

            except requests.RequestException as e:
                logger.error(
                    f"[@{self.username}] 获取推文失败 (尝试 {attempt + 1}/{max_retries}): {e}"
                )
                if attempt < max_retries - 1:
                    time.sleep(2)

        return self._fetch_tweets_rsshub()

    def _fetch_tweets_rsshub(self) -> List[Dict]:
        """使用 RSSHub 获取用户推文（备用通道）"""
        if not self.rsshub_enabled or not self.rsshub_base_url:
            return []

        try:
            url = f"{self.rsshub_base_url}/twitter/user/{self.username}"
            response = self.session.get(url, timeout=self.rsshub_timeout)
            response.raise_for_status()

            feed = feedparser.parse(response.text)
            entries = feed.entries or []
            tweets: List[Dict] = []

            for entry in entries:
                link = entry.get("link") or ""
                match = re.search(r"/status/(\d+)", link)
                if not match:
                    continue
                tweet_id = match.group(1)
                text = entry.get("title") or entry.get("summary") or ""
                text = re.sub(r"<[^>]+>", "", text).strip()

                tweets.append(
                    {
                        "id": tweet_id,
                        "text": text,
                        "created_at": entry.get("published", ""),
                        "user": self.username,
                        "url": link or f"https://twitter.com/{self.username}/status/{tweet_id}",
                        "retweet_count": 0,
                        "favorite_count": 0,
                        "media": [],
                    }
                )

            if tweets:
                self.last_fetch_at = time.time()
            return tweets
        except Exception as e:
            logger.debug(f"[@{self.username}] RSSHub 备用通道失败: {e}")
            return []

    def _parse_tweets(self, html: str) -> List[Dict]:
        """从 HTML 中解析推文数据"""
        tweets = []

        try:
            pattern = (
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>'
            )
            match = re.search(pattern, html, re.DOTALL)

            if match:
                data = json.loads(match.group(1))
                timeline_data = (
                    data.get("props", {}).get("pageProps", {}).get("timeline", {})
                )
                entries = timeline_data.get("entries", [])

                for entry in entries:
                    content = entry.get("content", {})
                    tweet_data = content.get("tweet", {})

                    if tweet_data:
                        entry_id = str(entry.get("entryId", "")).lower()
                        is_pinned = (
                            "pinned" in entry_id
                            or tweet_data.get("pinned")
                            or tweet_data.get("is_pinned")
                        )
                        tweet = self._extract_tweet_info(tweet_data, is_pinned=is_pinned)
                        if tweet:
                            tweets.append(tweet)
            else:
                tweets = self._parse_tweets_fallback(html)

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"解析推文数据失败: {e}")
            tweets = self._parse_tweets_fallback(html)

        return tweets

    def _extract_tweet_info(
        self, tweet_data: Dict, is_pinned: bool = False
    ) -> Optional[Dict]:
        """从推文数据中提取关键信息"""
        try:
            tweet_id = tweet_data.get("id_str") or str(tweet_data.get("id", ""))
            if not tweet_id:
                return None

            return {
                "id": tweet_id,
                "text": tweet_data.get("full_text") or tweet_data.get("text", ""),
                "created_at": tweet_data.get("created_at", ""),
                "user": tweet_data.get("user", {}).get("screen_name", self.username),
                "url": f"https://twitter.com/{self.username}/status/{tweet_id}",
                "retweet_count": tweet_data.get("retweet_count", 0),
                "favorite_count": tweet_data.get("favorite_count", 0),
                "media": self._extract_media(tweet_data),
                "is_pinned": bool(is_pinned),
            }
        except Exception:
            return None

    def _extract_media(self, tweet_data: Dict) -> List[Dict]:
        """提取媒体信息（图片/视频）"""
        media_items: List[Dict] = []

        entities = tweet_data.get("extended_entities") or tweet_data.get("entities") or {}
        media_list = entities.get("media", []) if isinstance(entities, dict) else []

        for media in media_list:
            media_type = media.get("type")
            if media_type == "photo":
                url = media.get("media_url_https") or media.get("media_url")
                if url:
                    media_items.append({"type": "photo", "url": url})
            elif media_type in ("video", "animated_gif"):
                variants = media.get("video_info", {}).get("variants", [])
                best_url = None
                best_bitrate = -1
                for variant in variants:
                    if variant.get("content_type") != "video/mp4":
                        continue
                    bitrate = variant.get("bitrate", 0)
                    if bitrate > best_bitrate and variant.get("url"):
                        best_bitrate = bitrate
                        best_url = variant.get("url")
                if not best_url and variants:
                    best_url = variants[0].get("url")
                if best_url:
                    media_items.append({"type": "video", "url": best_url})

        return media_items

    def _parse_tweets_fallback(self, html: str) -> List[Dict]:
        """备用解析方法"""
        tweets = []

        tweet_pattern = r"/status/(\d+)"
        tweet_ids = set(re.findall(tweet_pattern, html))

        text_pattern = r'<p[^>]*class="[^"]*tweet-text[^"]*"[^>]*>(.*?)</p>'
        texts = re.findall(text_pattern, html, re.DOTALL)

        for i, tweet_id in enumerate(tweet_ids):
            text = texts[i] if i < len(texts) else "[无法获取推文内容]"
            text = re.sub(r"<[^>]+>", "", text).strip()

            tweets.append(
                {
                    "id": tweet_id,
                    "text": text,
                    "created_at": "",
                    "user": self.username,
                    "url": f"https://twitter.com/{self.username}/status/{tweet_id}",
                    "retweet_count": 0,
                    "favorite_count": 0,
                }
            )

        return tweets

    def get_new_tweets(self) -> List[Dict]:
        """获取新推文（未发送过的）"""
        tweets = self.fetch_tweets()

        # 按 ID 降序排序（最新的在前）- 确保正确识别最新推文
        tweets.sort(key=lambda x: int(x["id"]), reverse=True)

        if not tweets:
            return []

        first_run = not self.last_seen_id and not self.seen_tweet_ids

        # 标记置顶推文为已读，避免发送
        for tweet in tweets:
            if tweet.get("is_pinned"):
                self.seen_tweet_ids.add(tweet["id"])

        candidates = [t for t in tweets if not t.get("is_pinned")]
        if not candidates:
            return []

        latest_id = candidates[0]["id"]

        # 首次运行（无任何记录）
        if first_run:
            for tweet in tweets:
                self.seen_tweet_ids.add(tweet["id"])
            self.last_seen_id = latest_id
            self._persist_last_seen()
            logger.info(
                f"[@{self.username}] 初始化/恢复状态：标记 {len(tweets)} 条历史推文为已读，仅推送最新一条（非置顶）"
            )
            return [candidates[0]]

        # 优先使用持久化的 last_seen_id 判断增量
        if self.last_seen_id:
            new_tweets = [
                tweet
                for tweet in candidates
                if int(tweet["id"]) > int(self.last_seen_id)
            ]
            if new_tweets:
                self.last_seen_id = new_tweets[0]["id"]
                for tweet in new_tweets:
                    self.seen_tweet_ids.add(tweet["id"])
                self._persist_last_seen()
            return new_tweets

        # 兜底：使用 seen_tweet_ids 判断
        new_tweets = []
        for tweet in candidates:
            if tweet["id"] not in self.seen_tweet_ids:
                new_tweets.append(tweet)
                self.seen_tweet_ids.add(tweet["id"])

        if new_tweets:
            self.last_seen_id = new_tweets[0]["id"]
            self._persist_last_seen()

        return new_tweets

    def mark_as_seen(self, tweet_ids: List[str]):
        """标记推文为已发送"""
        self.seen_tweet_ids.update(tweet_ids)

    def initialize_seen_tweets(self):
        """初始化已见推文列表"""
        tweets = self.fetch_tweets()
        for tweet in tweets:
            self.seen_tweet_ids.add(tweet["id"])
        if tweets:
            # 最新推文在列表未必排序，这里取最大 ID
            latest_id = max(tweets, key=lambda x: int(x["id"]))["id"]
            self.last_seen_id = latest_id
            self._persist_last_seen()
        logger.info(f"已记录 {len(self.seen_tweet_ids)} 条现有推文")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    fetcher = TweetFetcher("Vito777_")
    tweets = fetcher.fetch_tweets()
    print(f"获取到 {len(tweets)} 条推文:")
    for tweet in tweets[:3]:
        print(f"  - {tweet['id']}: {tweet['text'][:50]}...")
