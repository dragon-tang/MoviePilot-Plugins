import json
import re
import time
import traceback
import threading
import os
from collections import OrderedDict
from typing import Any, List, Dict, Tuple, Optional

import requests

from app.core.event import eventmanager, Event
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.modules.themoviedb import CategoryHelper
from app.plugins import _PluginBase
from app.schemas import WebhookEventInfo, ServiceInfo
from app.schemas.types import EventType, MediaType, MediaImageType, NotificationType
from app.utils.web import WebUtils


class MediaServerMsgAI(_PluginBase):
    """
    媒体服务器通知插件 AI增强版

    功能：
    1. 监听Emby/Jellyfin/Plex等媒体服务器的Webhook事件
    2. 根据配置发送播放、入库等通知消息
    3. 对TV剧集入库事件进行智能聚合，避免消息轰炸
    4. 支持多种媒体服务器和丰富的消息类型配置
    5. 基于TMDB元数据增强消息内容（评分、分类、演员等）
    6. 支持音乐专辑和单曲入库通知
    7. 支持TMDB未识别视频不发送通知（包含播放事件）
    8. 支持路径关键词黑名单，命中路径跳过TMDB识别
    9. 拦截路径的媒体自动使用Emby本地图片（Backdrop/Primary）
    """

    # ==================== 常量定义 ====================
    DEFAULT_EXPIRATION_TIME = 600
    DEFAULT_AGGREGATE_TIME = 15
    DEFAULT_OVERVIEW_MAX_LENGTH = 150
    MIN_AGGREGATE_TIME = 1
    MIN_OVERVIEW_MAX_LENGTH = 20
    SERIES_TMDB_CACHE_TTL = 3600
    SERIES_TMDB_NEGATIVE_CACHE_TTL = 300

    # ==================== 媒体类型常量 ====================
    MT_MOVIE = "MOV"
    MT_TV = "TV"
    MT_SHOW = "SHOW"
    MT_AUDIO = "AUD"
    MT_MUSIC_ALBUM = "MusicAlbum"
    # ==================== 插件基本信息 ====================
    plugin_name = "媒体库服务器通知AI版"
    plugin_desc = "基于Emby识别结果+TMDB元数据+微信清爽版(全消息类型+剧集聚合+未识别过滤)"
    plugin_icon = "mediaplay.png"
    plugin_version = "2.1.5"
    plugin_author = "dragon-tang"
    author_url = "https://github.com/dragon-tang"
    plugin_config_prefix = "mediaservermsgai_"
    plugin_order = 14
    auth_level = 1

    # ==================== Webhook事件映射配置 ====================
    _webhook_actions = {
        "library.new": "已入库",
        "system.webhooktest": "测试",
        "system.notificationtest": "测试",
        "playback.start": "开始播放",
        "playback.stop": "停止播放",
        "playback.pause": "暂停播放",
        "playback.unpause": "继续播放",
        "user.authenticated": "登录成功",
        "user.authenticationfailed": "登录失败",
        "media.play": "开始播放",
        "media.stop": "停止播放",
        "media.pause": "暂停播放",
        "media.resume": "继续播放",
        "item.rate": "标记了",
        "item.markplayed": "标记已播放",
        "item.markunplayed": "标记未播放",
        "PlaybackStart": "开始播放",
        "PlaybackStop": "停止播放",
        "deep.delete": "深度删除"
    }

    # ==================== 媒体服务器默认图标 ====================
    _webhook_images = {
        "emby": "https://raw.githubusercontent.com/dragon-tang/MoviePilot-Plugins/refs/heads/main/icons/emby.png",
        "plex": "https://raw.githubusercontent.com/dragon-tang/MoviePilot-Plugins/refs/heads/main/icons/Plex_A.png",
        "jellyfin": "https://raw.githubusercontent.com/dragon-tang/MoviePilot-Plugins/refs/heads/main/icons/Jellyfin_A.png"
    }

    def __init__(self):
        super().__init__()
        # FIX: 延迟到 init_plugin 初始化，避免未配置时消耗资源
        self.category: Optional[CategoryHelper] = None
        # 运行时可变状态（实例属性）
        self._enabled = False
        self._add_play_link = False
        self._mediaservers = None
        self._types = []
        self._webhook_msg_keys = {}
        self._lock = threading.Lock()
        self._total_events = 0  # 处理事件总数
        self._event_history = [] # 存储最近 5-10 条记录
        self._last_event_cache: Tuple[Optional[Event], float] = (None, 0.0)
        self._http_session = requests.Session()
        self._overview_max_length = self.DEFAULT_OVERVIEW_MAX_LENGTH
        self._filter_unrecognized = True
        self._path_skip_keywords = []
        self._emby_image_host = ""
        self._aggregate_enabled = False
        self._aggregate_time = self.DEFAULT_AGGREGATE_TIME
        self._pending_messages = {}
        self._aggregate_timers = {}
        self._smart_category_enabled = True
        self._service_infos_cache: Tuple[Optional[Dict], float] = (None, 0.0)
        self._series_tmdb_cache = {}
        self._series_tmdb_inflight = set()
        self._webhook_actions_lower: frozenset = frozenset()
        self._allowed_event_types: frozenset = frozenset()
        self._last_event_snapshot: Dict[str, str] = {}
        self._last_notification_snapshot: Dict[str, str] = {}

    @staticmethod
    def _safe_int(value: Any, default: int, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError):
            result = default
        if min_value is not None:
            result = max(min_value, result)
        if max_value is not None:
            result = min(max_value, result)
        return result

    def init_plugin(self, config: dict = None):
        # 重置运行时状态，防止重新配置后旧 timer/缓存残留
        self.stop_service()
        if self.category is None:
            self.category = CategoryHelper()
        self._webhook_actions_lower = frozenset(k.lower() for k in self._webhook_actions)
        self._allowed_event_types = frozenset()
        if config:
            self._enabled = config.get("enabled")
            self._types = config.get("types") or []
            self._mediaservers = config.get("mediaservers") or []
            self._add_play_link = config.get("add_play_link", False)
            self._overview_max_length = self._safe_int(
                config.get("overview_max_length", self.DEFAULT_OVERVIEW_MAX_LENGTH),
                self.DEFAULT_OVERVIEW_MAX_LENGTH,
                min_value=self.MIN_OVERVIEW_MAX_LENGTH,
            )
            self._aggregate_enabled = config.get("aggregate_enabled", False)
            self._aggregate_time = self._safe_int(
                config.get("aggregate_time", self.DEFAULT_AGGREGATE_TIME),
                self.DEFAULT_AGGREGATE_TIME,
                min_value=self.MIN_AGGREGATE_TIME,
            )
            self._smart_category_enabled = config.get("smart_category_enabled", True)
            self._filter_unrecognized = config.get("filter_unrecognized", True)
            path_skip_keywords_raw = config.get("path_skip_keywords", "")
            self._path_skip_keywords = [
                kw.strip().lower() for kw in path_skip_keywords_raw.splitlines() if kw.strip()
            ]
            self._allowed_event_types = frozenset(
                t.lower()
                for event_group in self._types
                for t in str(event_group).split("|")
                if t
            )
            self._emby_image_host = config.get("emby_image_host", "").rstrip("/")
            logger.info(f"插件配置初始化完成: 启用={self._enabled}, 聚合={self._aggregate_enabled}({self._aggregate_time}s), "
                        f"智能分类={self._smart_category_enabled}, TMDB过滤={self._filter_unrecognized}")

    def service_infos(self, type_filter: Optional[str] = None) -> Optional[Dict[str, ServiceInfo]]:
        if not self._mediaservers:
            return None

        # PERF: 60s TTL 缓存，避免每次 Webhook 都重建 MediaServerHelper
        now = time.time()
        with self._lock:
            cached, ts = self._service_infos_cache
        if cached is not None and (now - ts) < 60:
            return cached

        services = MediaServerHelper().get_services(type_filter=type_filter, name_filters=self._mediaservers)
        if not services:
            logger.warning("获取媒体服务器实例失败")
            with self._lock:
                self._service_infos_cache = (None, now)
            return None

        active_services = {}
        for service_name, service_info in services.items():
            if service_info.instance.is_inactive():
                logger.warning(f"媒体服务器 {service_name} 未连接")
            else:
                active_services[service_name] = service_info

        result = active_services if active_services else None
        with self._lock:
            self._service_infos_cache = (result, now)
        return result

    def service_info(self, name: str) -> Optional[ServiceInfo]:
        services = self.service_infos()
        if not services:
            return None
        return services.get(name)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def _get_mediaserver_items(self) -> list:
        """获取媒体服务器列表，带异常保护，避免配置页面白屏"""
        try:
            return [{"title": cfg.name, "value": cfg.name}
                    for cfg in MediaServerHelper().get_configs().values()]
        except Exception as e:
            logger.error(f"获取媒体服务器列表失败: {str(e)}")
            return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        types_options = [
            {"title": "新入库", "value": "library.new"},
            {"title": "开始播放", "value": "playback.start|media.play|PlaybackStart"},
            {"title": "停止播放", "value": "playback.stop|media.stop|PlaybackStop"},
            {"title": "暂停/继续", "value": "playback.pause|playback.unpause|media.pause|media.resume"},
            {"title": "用户标记", "value": "item.rate|item.markplayed|item.markunplayed"},
            {"title": "登录成功", "value": "user.authenticated"},
            {"title": "登录失败", "value": "user.authenticationfailed"},
            {"title": "系统测试", "value": "system.webhooktest|system.notificationtest"},
            {"title": "媒体深度删除", "value": "deep.delete"},
        ]
        return [
            {
                'component': 'VForm',
                'content': [
                    {'component': 'VRow', 'content': [
                        # Left Column
                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                            # ===== 🛠️ 基本设置 =====
                            {
                                'component': 'VCard', 'props': {'variant': 'flat', 'class': 'mb-4'},
                                'content': [
                                    {'component': 'VCardTitle', 'props': {'class': 'pa-3'}, 'text': '🛠️ 基本设置'},
                                    {'component': 'VDivider'},
                                    {'component': 'VCardText', 'content': [
                                        {'component': 'VRow', 'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'sm': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件', 'density': 'compact', 'hide-details': 'auto'}}]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'sm': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'add_play_link', 'label': '添加播放链接', 'density': 'compact', 'hide-details': 'auto'}}]}
                                        ]},
                                        {'component': 'VSelect', 'props': {'multiple': True, 'chips': True, 'clearable': True, 'model': 'mediaservers', 'label': '媒体服务器', 'items': self._get_mediaserver_items(), 'density': 'compact', 'hide-details': 'auto', 'class': 'mt-4'}},
                                        {'component': 'VSelect', 'props': {'chips': True, 'multiple': True, 'model': 'types', 'label': '消息类型', 'items': types_options, 'density': 'compact', 'hide-details': 'auto', 'class': 'mt-4'}}
                                    ]}
                                ]
                            },
                            # ===== 📦 入库设置 =====
                            {
                                'component': 'VCard', 'props': {'variant': 'flat', 'class': 'mb-4'},
                                'content': [
                                    {'component': 'VCardTitle', 'props': {'class': 'pa-3'}, 'text': '📦 入库设置'},
                                    {'component': 'VDivider'},
                                    {'component': 'VCardText', 'content': [
                                        {'component': 'VRow', 'content': [
                                            {'component': 'VCol', 'props': {'cols': 12, 'sm': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'aggregate_enabled', 'label': '启用TV剧集入库聚合', 'density': 'compact', 'hide-details': 'auto'}}]},
                                            {'component': 'VCol', 'props': {'cols': 12, 'sm': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'smart_category_enabled', 'label': '启用智能分类', 'density': 'compact', 'hide-details': 'auto'}}]}
                                        ]},
                                        {'component': 'VTextField', 'props': {'show': '{{aggregate_enabled}}', 'model': 'aggregate_time', 'label': '聚合等待时间（秒）', 'placeholder': '15', 'type': 'number', 'density': 'compact', 'hide-details': 'auto', 'class': 'mt-4'}}
                                    ]}
                                ]
                            }
                        ]},
                        # Right Column
                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                            # ===== 🔍 过滤设置 =====
                            {
                                'component': 'VCard', 'props': {'variant': 'flat', 'class': 'mb-4'},
                                'content': [
                                    {'component': 'VCardTitle', 'props': {'class': 'pa-3'}, 'text': '🔍 过滤设置'},
                                    {'component': 'VDivider'},
                                    {'component': 'VCardText', 'content': [
                                        {'component': 'VSwitch', 'props': {'model': 'filter_unrecognized', 'label': 'TMDB未识别视频不发送通知', 'hint': '启用后，未识别到TMDB信息的视频（入库和播放）都不会发送通知', 'density': 'compact'}},
                                        {'component': 'VTextarea', 'props': {'model': 'path_skip_keywords', 'label': '路径关键词黑名单（跳过TMDB识别）', 'placeholder': '每行一个关键词，Path包含任意关键词时跳过TMDB识别', 'rows': 4, 'hint': '命中关键词的媒体不会进行TMDB识别', 'density': 'compact', 'class': 'mt-4'}}
                                    ]}
                                ]
                            },
                            # ===== 🖼️ 显示设置 =====
                            {
                                'component': 'VCard', 'props': {'variant': 'flat'},
                                'content': [
                                    {'component': 'VCardTitle', 'props': {'class': 'pa-3'}, 'text': '🖼️ 显示设置'},
                                    {'component': 'VDivider'},
                                    {'component': 'VCardText', 'content': [
                                        {'component': 'VTextField', 'props': {'model': 'overview_max_length', 'label': '简介最大长度', 'placeholder': '150', 'type': 'number', 'hint': '入库通知中简介文字的最大字符数', 'density': 'compact', 'hide-details': 'auto'}},
                                        {'component': 'VTextField', 'props': {'model': 'emby_image_host', 'label': '自定义Emby图片Host', 'placeholder': '例如：http://1.1.1.1:8099', 'hint': '拦截路径的媒体图片将使用此Host构造URL', 'density': 'compact', 'hide-details': 'auto', 'class': 'mt-4'}}
                                    ]}
                                ]
                            }
                        ]}
                    ]}
                ]
            }
        ], {
            "enabled": False,
            "add_play_link": False,
            "mediaservers": [],
            "types": [],
            "aggregate_enabled": False,
            "aggregate_time": self.DEFAULT_AGGREGATE_TIME,
            "smart_category_enabled": True,
            "filter_unrecognized": True,
            "path_skip_keywords": "",
            "overview_max_length": self.DEFAULT_OVERVIEW_MAX_LENGTH,
            "emby_image_host": ""
        }

    def get_page(self) -> List[dict]:
        return []