import ast
import json
import re
import time
import traceback
import threading
import os
from collections import OrderedDict
from typing import Any, List, Dict, Tuple, Optional

import requests

from app.core.config import settings
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
    TITLE_TEMPLATE_LIMIT = 120
    DEFAULT_TITLE_TEMPLATES = OrderedDict([
        ("library_new", "🆕 {title} 已入库"),
        ("library_aggregate", "🆕 {title} 已入库 (含{count}个文件)"),
        ("playback_start", "▶️ 开始播放：{title}"),
        ("playback_stop", "⏹️ 停止播放：{title}"),
        ("playback_pause", "⏸️ 暂停播放：{title}"),
        ("playback_resume", "▶️ 继续播放：{title}"),
        ("rate", "⭐ 用户评分：{title}"),
        ("login_success", "✅ 登录成功提醒"),
        ("login_failed", "🚫 登录失败提醒"),
        ("test", "🔔 媒体服务器通知测试"),
        ("deep_delete", "🗑️ 神医助手 - 媒体深度删除"),
        ("audio", "{title} {action} {server}"),
        ("audio_library", "🎵 新入库媒体：{title}"),
    ])

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
    plugin_version = "2.1.11"
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
        self._title_templates = self.DEFAULT_TITLE_TEMPLATES.copy()
        self._notification_templates = self._build_default_notification_templates(self._title_templates)

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
            title_templates_raw = config.get("title_templates")
            title_templates_text = self._normalize_title_templates_text(title_templates_raw)
            self._title_templates = self._parse_title_templates(title_templates_text)
            template_updates = {}
            if title_templates_raw != title_templates_text:
                template_updates["title_templates"] = title_templates_text
            notification_templates_raw = config.get("notification_templates")
            notification_templates_text = self._normalize_notification_templates_text(notification_templates_raw)
            self._notification_templates = self._parse_notification_templates(notification_templates_text)
            if notification_templates_raw != notification_templates_text:
                template_updates["notification_templates"] = notification_templates_text
            if template_updates:
                self._save_template_defaults(config, template_updates)
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
        return [
            {
                "path": "/test_notification",
                "endpoint": self.test_notification,
                "methods": ["GET"],
                "summary": "发送媒体服务器通知测试消息",
                "description": "按指定类型发送一条测试通知，用于调试标题模板和消息样式。",
            }
        ]

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
                                'component': 'VCard', 'props': {'variant': 'flat', 'class': 'mb-4'},
                                'content': [
                                    {'component': 'VCardTitle', 'props': {'class': 'pa-3'}, 'text': '🖼️ 显示设置'},
                                    {'component': 'VDivider'},
                                    {'component': 'VCardText', 'content': [
                                        {'component': 'VTextField', 'props': {'model': 'overview_max_length', 'label': '简介最大长度', 'placeholder': '150', 'type': 'number', 'hint': '入库通知中简介文字的最大字符数', 'density': 'compact', 'hide-details': 'auto'}},
                                        {'component': 'VTextField', 'props': {'model': 'emby_image_host', 'label': '自定义Emby图片Host', 'placeholder': '例如：http://1.1.1.1:8099', 'hint': '拦截路径的媒体图片将使用此Host构造URL', 'density': 'compact', 'hide-details': 'auto', 'class': 'mt-4'}}
                                    ]}
                                ]
                            },
                            # ===== 🎨 通知模板 =====
                            {
                                'component': 'VCard', 'props': {'variant': 'flat'},
                                'content': [
                                    {'component': 'VCardTitle', 'props': {'class': 'pa-3'}, 'text': '🎨 通知模板'},
                                    {'component': 'VDivider'},
                                    {'component': 'VCardText', 'content': [
                                        {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'density': 'compact', 'class': 'mb-3'}, 'text': '完整通知模板，JSON 或 Python 字面量格式；每个类型包含 title/text。支持 {{ title_year }}、{{ current_time }}、{{ season_episode }}、{{ category }}、{{ releaseGroup }}、{{ resource_term }}、{{ audioCodec }}、{{ total_size }}、{{ err_msg }} 等变量，以及 {% if xxx %}...{% endif %} 条件块。清空保存会自动恢复默认模板。'},
                                        {'component': 'VTextarea', 'props': {'model': 'notification_templates', 'label': '通知模板（标题 + 正文）', 'rows': 14, 'auto-grow': True, 'density': 'compact', 'hide-details': 'auto', 'placeholder': self._default_notification_templates_text()}}
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
            "emby_image_host": "",
            "title_templates": self._default_title_templates_text(),
            "notification_templates": self._default_notification_templates_text()
        }

    def get_page(self) -> List[dict]:
        token = getattr(settings, "API_TOKEN", "")
        test_kinds = [
            ("library", "测试入库通知", "primary", "mdi-plus-box"),
            ("playback", "测试播放通知", "success", "mdi-play-circle"),
            ("login_success", "测试登录成功", "info", "mdi-login"),
            ("login_failed", "测试登录失败", "error", "mdi-alert-circle"),
        ]
        buttons = [
            {
                'component': 'VCol',
                'props': {'cols': 12, 'sm': 6, 'md': 3},
                'content': [
                    {
                        'component': 'VBtn',
                        'props': {'block': True, 'color': color, 'variant': 'tonal', 'prepend-icon': icon},
                        'text': text,
                        'events': {
                            'click': {
                                'api': f'plugin/MediaServerMsgAI/test_notification?kind={kind}&apikey={token}',
                                'method': 'get'
                            }
                        }
                    }
                ]
            }
            for kind, text, color, icon in test_kinds
        ]
        return [
            {
                'component': 'VCard',
                'props': {'variant': 'flat', 'class': 'pa-2'},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'd-flex align-center'}, 'text': '🧪 测试通知中心'},
                    {'component': 'VCardText', 'content': [
                        {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'density': 'compact', 'class': 'mb-4'}, 'text': '点击下面按钮会立即发送一条测试通知，用来校验标题模板、图片和通知渠道展示效果。'},
                        {'component': 'VRow', 'content': buttons},
                        {'component': 'VDivider', 'props': {'class': 'my-4'}},
                        {'component': 'div', 'props': {'class': 'text-caption text-medium-emphasis'}, 'text': '标题模板在插件配置页维护；留空或格式错误时自动回退默认标题，不影响正常通知。'}
                    ]}
                ]
            }
        ]


    class _SafeTitleDict(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    @classmethod
    def _default_title_templates_text(cls) -> str:
        return "\n".join(f"{key}={value}" for key, value in cls.DEFAULT_TITLE_TEMPLATES.items())

    @staticmethod
    def _title_format_to_mini_template(title_template: str) -> str:
        return re.sub(r"{([a-zA-Z_][a-zA-Z0-9_]*)}", r"{{ \1 }}", str(title_template))

    @staticmethod
    def _build_default_notification_templates(title_templates: Dict[str, str]) -> OrderedDict:
        def title_for(key: str, fallback: str) -> str:
            return MediaServerMsgAI._title_format_to_mini_template(title_templates.get(key, fallback))

        return OrderedDict([
            ("library_new", {
                "title": title_for("library_new", "🎥 {{ title_year }}"),
                "text": "{% if current_time %}\n🕒 时间: {{ current_time }}{% endif %}\n🏷 状态: 整理完成{% if season_episode %}\n📺 集数: {{ season_episode }}{% endif %}{% if category %}\n🎬 类别: {{ category }}{% endif %}{% if releaseGroup %}\n👥 小组: {{ releaseGroup }}{% endif %}{% if resource_term %}\n⭐️ 质量: {{ resource_term }}{% endif %}{% if audioCodec %} {{ audioCodec }}{% endif %}{% if total_size %}\n💾 大小: {{ total_size }}{% endif %}{% if overview %}\n📖 简介：\n{{ overview }}{% endif %}{% if err_msg %}\n⚠️ 处理失败: {{ err_msg }}{% endif %}"
            }),
            ("library_aggregate", {
                "title": title_for("library_aggregate", "🆕 {{ title_year }} 已入库 (含{{ count }}个文件)"),
                "text": "{% if current_time %}\n🕒 时间: {{ current_time }}{% endif %}\n🏷 状态: 剧集聚合完成{% if season_episode %}\n📺 集数: {{ season_episode }}{% endif %}{% if category %}\n🎬 类别: {{ category }}{% endif %}{% if count %}\n📦 文件数: {{ count }}{% endif %}{% if overview %}\n📖 简介：\n{{ overview }}{% endif %}"
            }),
            ("playback_start", {
                "title": title_for("playback_start", "▶️ 开始播放：{{ title_year }}"),
                "text": "{% if current_time %}\n🕒 时间: {{ current_time }}{% endif %}{% if user %}\n👤 用户: {{ user }}{% endif %}{% if device %}\n📱 设备: {{ device }}{% endif %}{% if ip %}\n🌐 IP: {{ ip }}{% endif %}{% if percentage %}\n📊 进度: {{ percentage }}%{% endif %}"
            }),
            ("playback_stop", {
                "title": title_for("playback_stop", "⏹️ 停止播放：{{ title_year }}"),
                "text": "{% if current_time %}\n🕒 时间: {{ current_time }}{% endif %}{% if user %}\n👤 用户: {{ user }}{% endif %}{% if device %}\n📱 设备: {{ device }}{% endif %}{% if percentage %}\n📊 进度: {{ percentage }}%{% endif %}"
            }),
            ("playback_pause", {"title": title_for("playback_pause", "⏸️ 暂停播放：{{ title_year }}"), "text": "{{ text }}"}),
            ("playback_resume", {"title": title_for("playback_resume", "▶️ 继续播放：{{ title_year }}"), "text": "{{ text }}"}),
            ("rate", {"title": title_for("rate", "⭐ 用户评分：{{ title_year }}"), "text": "{{ text }}"}),
            ("login_success", {"title": title_for("login_success", "✅ 登录成功提醒"), "text": "{{ text }}"}),
            ("login_failed", {"title": title_for("login_failed", "🚫 登录失败提醒"), "text": "{{ text }}"}),
            ("test", {"title": title_for("test", "🔔 媒体服务器通知测试"), "text": "{{ text }}"}),
            ("deep_delete", {"title": title_for("deep_delete", "🗑️ 神医助手 - 媒体深度删除"), "text": "{{ text }}"}),
            ("audio", {"title": title_for("audio", "{{ title }} {{ action }} {{ server }}"), "text": "{{ text }}"}),
            ("audio_library", {"title": title_for("audio_library", "🎵 新入库媒体：{{ title }}"), "text": "{{ text }}"}),
        ])

    def _default_notification_templates_text(self) -> str:
        return json.dumps(
            self._build_default_notification_templates(self._title_templates),
            ensure_ascii=False,
            indent=2,
        )

    def _normalize_notification_templates_text(self, raw_templates: Any) -> str:
        if isinstance(raw_templates, str) and raw_templates.strip():
            return raw_templates.strip()
        if isinstance(raw_templates, dict) and raw_templates:
            return json.dumps(raw_templates, ensure_ascii=False, indent=2)
        return self._default_notification_templates_text()

    def _save_template_defaults(self, config: dict, updates: Dict[str, str]):
        try:
            config_to_save = dict(config or {})
            config_to_save.update(updates)
            self.update_config(config_to_save)
            logger.info(f"模板为空或缺失，已自动写入默认模板: {', '.join(updates.keys())}")
        except Exception as e:
            logger.warning(f"写入默认模板失败: {str(e)}")

    def _parse_notification_templates(self, raw_templates: Any) -> OrderedDict:
        templates = self._build_default_notification_templates(self._title_templates)
        if not raw_templates:
            return templates
        try:
            data = raw_templates if isinstance(raw_templates, dict) else json.loads(str(raw_templates))
        except Exception:
            try:
                data = ast.literal_eval(str(raw_templates))
            except Exception as e:
                logger.warning(f"通知模板解析失败，使用默认模板: {str(e)}")
                return templates
        if isinstance(data, dict) and ("title" in data or "text" in data):
            data = {"library_new": data}
        if not isinstance(data, dict):
            return templates
        for key, value in data.items():
            if key not in templates or not isinstance(value, dict):
                continue
            title = str(value.get("title", "")).strip()
            text = str(value.get("text", "")).strip()
            if title or text:
                templates[key] = {
                    "title": title or templates[key]["title"],
                    "text": text or templates[key]["text"],
                }
        return templates

    @staticmethod
    def _render_mini_template(template: str, values: Dict[str, Any]) -> str:
        safe_values = {k: "" if v is None else str(v) for k, v in values.items()}

        def render_if(match):
            key = match.group(1).strip()
            body = match.group(2)
            return body if safe_values.get(key) else ""

        rendered = re.sub(r"{%\s*if\s+([\w_]+)\s*%}(.*?){%\s*endif\s*%}", render_if, template, flags=re.DOTALL)
        rendered = re.sub(r"{{\s*([\w_]+)\s*}}", lambda m: safe_values.get(m.group(1), ""), rendered)
        try:
            rendered = rendered.format_map(MediaServerMsgAI._SafeTitleDict(safe_values))
        except Exception:
            pass
        return rendered.strip()

    def _build_template_values(self, event_info: WebhookEventInfo, title_name: str, default_text: str,
                               tmdb_info=None, **extra) -> Dict[str, Any]:
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        json_obj = event_info.json_object if isinstance(getattr(event_info, "json_object", None), dict) else {}
        item = json_obj.get("Item", {}) if isinstance(json_obj.get("Item", {}), dict) else {}
        media_source = self._get_media_source(item)
        streams = self._get_media_streams(item, media_source)
        video_stream = self._first_media_stream(streams, "Video")
        audio_stream = self._first_media_stream(streams, "Audio")
        year = self._first_value(
            item.get("ProductionYear"),
            getattr(tmdb_info, "year", None) if tmdb_info else None,
            self._extract_year_from_title(title_name),
        )
        season_episode = extra.get("season_episode") or self._get_season_episode_label(event_info)
        total_size = self._format_size(self._safe_int(self._first_value(
            item.get("Size"), media_source.get("Size") if isinstance(media_source, dict) else None
        ), 0))
        if total_size == "0 MB":
            total_size = ""
        device = getattr(event_info, "device_name", "") or ""
        client = getattr(event_info, "client", "") or ""
        if device and client and client not in device:
            device = f"{client} {device}"
        values = {
            "title": title_name,
            "title_year": title_name,
            "raw_title": getattr(event_info, "item_name", None) or title_name,
            "name": getattr(event_info, "item_name", None) or title_name,
            "year": year or "",
            "text": default_text,
            "current_time": now,
            "time": now,
            "server": self._get_server_name_cn(event_info),
            "user": getattr(event_info, "user_name", "") or "",
            "client": client,
            "device": device,
            "ip": getattr(event_info, "ip", "") or "",
            "percentage": self._format_percentage(getattr(event_info, "percentage", None)),
            "season_episode": season_episode,
            "category": extra.get("category", "") or "",
            "releaseGroup": self._extract_release_group(json_obj, item, getattr(event_info, "item_path", "") or ""),
            "resource_term": self._build_resource_term(video_stream, media_source, item),
            "audioCodec": self._build_audio_codec(audio_stream),
            "videoCodec": str(video_stream.get("Codec") or "").upper() if isinstance(video_stream, dict) else "",
            "total_size": total_size,
            "err_msg": self._extract_error_message(json_obj),
            "overview": getattr(event_info, "overview", "") or (getattr(tmdb_info, "overview", "") if tmdb_info else ""),
            "tmdbid": getattr(event_info, "tmdb_id", "") or item.get("ProviderIds", {}).get("Tmdb", ""),
            "imdbid": item.get("ProviderIds", {}).get("Imdb", "") if isinstance(item.get("ProviderIds"), dict) else "",
            "item_path": getattr(event_info, "item_path", "") or item.get("Path", ""),
            "container": str(item.get("Container") or media_source.get("Container") or "").upper() if isinstance(media_source, dict) else str(item.get("Container") or "").upper(),
            "action": extra.get("action", ""),
            "count": extra.get("count", ""),
        }
        values.update({k: v for k, v in extra.items() if v is not None})
        return values

    @staticmethod
    def _first_value(*values):
        for value in values:
            if value not in (None, ""):
                return value
        return ""

    @staticmethod
    def _extract_year_from_title(title: str) -> str:
        match = re.search(r"\((\d{4})\)", str(title or ""))
        return match.group(1) if match else ""

    @staticmethod
    def _get_media_source(item: dict) -> dict:
        sources = item.get("MediaSources") if isinstance(item, dict) else None
        if isinstance(sources, list) and sources:
            return sources[0] if isinstance(sources[0], dict) else {}
        return {}

    @staticmethod
    def _get_media_streams(item: dict, media_source: dict) -> list:
        streams = item.get("MediaStreams") if isinstance(item, dict) else None
        if not streams and isinstance(media_source, dict):
            streams = media_source.get("MediaStreams")
        return streams if isinstance(streams, list) else []

    @staticmethod
    def _first_media_stream(streams: list, stream_type: str) -> dict:
        for stream in streams:
            if isinstance(stream, dict) and str(stream.get("Type", "")).lower() == stream_type.lower():
                return stream
        return {}

    def _get_season_episode_label(self, event_info: WebhookEventInfo) -> str:
        season = getattr(event_info, "season_id", None)
        episode = getattr(event_info, "episode_id", None)
        item = event_info.json_object.get("Item", {}) if isinstance(getattr(event_info, "json_object", None), dict) else {}
        if season is None:
            season = item.get("ParentIndexNumber")
        if episode is None:
            episode = item.get("IndexNumber")
        if season is not None and episode is not None:
            return f"S{str(season).zfill(2)}E{str(episode).zfill(2)}"
        return ""

    @staticmethod
    def _format_percentage(value: Any) -> str:
        if value is None or value == "":
            return ""
        try:
            return str(round(float(value), 2))
        except Exception:
            return str(value)

    @staticmethod
    def _extract_release_group(json_obj: dict, item: dict, path: str) -> str:
        for source in (json_obj, item):
            for key in ("ReleaseGroup", "releaseGroup", "release_group", "Group", "ResourceTeam"):
                if isinstance(source, dict) and source.get(key):
                    return str(source.get(key))
        name = os.path.basename(path or "")
        match = re.search(r"-([A-Za-z0-9]+)(?:\.[^.]+)?$", name)
        return match.group(1) if match else ""

    @staticmethod
    def _build_resource_term(video_stream: dict, media_source: dict, item: dict) -> str:
        parts = []
        height = video_stream.get("Height") if isinstance(video_stream, dict) else None
        if height:
            try:
                parts.append(f"{int(height)}p")
            except Exception:
                parts.append(str(height))
        video_range = video_stream.get("VideoRange") or video_stream.get("VideoRangeType") if isinstance(video_stream, dict) else ""
        if video_range:
            parts.append(str(video_range).upper())
        container = item.get("Container") or media_source.get("Container") if isinstance(media_source, dict) else item.get("Container")
        if container:
            parts.append(str(container).upper())
        return " ".join(dict.fromkeys(p for p in parts if p))

    @staticmethod
    def _build_audio_codec(audio_stream: dict) -> str:
        if not isinstance(audio_stream, dict) or not audio_stream:
            return ""
        parts = []
        if audio_stream.get("Codec"):
            parts.append(str(audio_stream.get("Codec")).upper())
        if audio_stream.get("Channels"):
            parts.append(f"{audio_stream.get('Channels')}ch")
        if audio_stream.get("Language"):
            parts.append(str(audio_stream.get("Language")).upper())
        return " ".join(parts)

    @staticmethod
    def _extract_error_message(json_obj: dict) -> str:
        for key in ("err_msg", "ErrMsg", "error", "Error", "message", "Message"):
            if isinstance(json_obj, dict) and json_obj.get(key):
                return str(json_obj.get(key))
        return ""

    def _render_notification_template(self, key: str, default_title_template: str, default_text: str, **values) -> Tuple[str, str]:
        values = dict(values)
        values.setdefault("text", default_text)
        default_title = self._render_title_template(key, default_title_template, **values)
        template = self._notification_templates.get(key) or {}
        title_template = template.get("title") or default_title
        text_template = template.get("text") or "{{ text }}"
        title = self._render_mini_template(title_template, {**values, "title": values.get("title", "")}) or default_title
        text = self._render_mini_template(text_template, {**values, "text": default_text}) or default_text
        return title, text

    def _send_templated_notification(self, template_key: str, default_title_template: str, text: str,
                                     image: Optional[str] = None, link: Optional[str] = None, **values):
        title, body = self._render_notification_template(template_key, default_title_template, text, **values)
        self._send_notification(title=title, text=body, image=image, link=link)

    @classmethod
    def _normalize_title_templates_text(cls, raw_templates: Any) -> str:
        if isinstance(raw_templates, dict) and raw_templates:
            lines = [f"{key}={value}" for key, value in raw_templates.items() if str(key).strip() and str(value).strip()]
            return "\n".join(lines) if lines else cls._default_title_templates_text()
        if isinstance(raw_templates, str) and raw_templates.strip():
            return raw_templates.strip()
        return cls._default_title_templates_text()

    def _parse_title_templates(self, raw_templates: Any) -> OrderedDict:
        templates = self.DEFAULT_TITLE_TEMPLATES.copy()
        if not raw_templates:
            return templates
        if isinstance(raw_templates, dict):
            items = raw_templates.items()
        else:
            items = []
            for line in str(raw_templates).splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    logger.warning(f"标题模板格式无效，已忽略: {line}")
                    continue
                key, value = line.split("=", 1)
                items.append((key.strip(), value.strip()))
        for key, value in items:
            if key in templates and str(value).strip():
                templates[key] = str(value).strip()
        return templates

    def _render_title_template(self, key: str, default_template: str, **values) -> str:
        template = (self._title_templates.get(key) or default_template or "").strip()
        try:
            rendered = template.format_map(self._SafeTitleDict({k: "" if v is None else str(v) for k, v in values.items()}))
        except Exception as e:
            logger.warning(f"标题模板渲染失败，使用默认标题: {key} - {str(e)}")
            rendered = default_template.format_map(self._SafeTitleDict({k: "" if v is None else str(v) for k, v in values.items()}))
        rendered = re.sub(r"\s+", " ", rendered).strip()
        if len(rendered) > self.TITLE_TEMPLATE_LIMIT:
            rendered = rendered[:self.TITLE_TEMPLATE_LIMIT].rstrip() + "..."
        return rendered or default_template or "通知"

    def _get_webhook_image(self, channel: Optional[str]) -> Optional[str]:
        return self._webhook_images.get(str(channel or "").lower())

    def test_notification(self, kind: str = "library", apikey: str = "", **kwargs) -> Dict[str, Any]:
        api_token = getattr(settings, "API_TOKEN", "")
        if not api_token or apikey != api_token:
            return {"success": False, "message": "API token 无效"}
        kind = (kind or "library").strip().lower()
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        title_name = "示例电影 (2026)"
        server = "Emby"
        user = "Dragon"
        templates = {
            "library": ("library_new", "🆕 {title} 已入库", "https://raw.githubusercontent.com/dragon-tang/MoviePilot-Plugins/refs/heads/main/icons/emby.png"),
            "playback": ("playback_start", "▶️ 开始播放：{title}", "https://raw.githubusercontent.com/dragon-tang/MoviePilot-Plugins/refs/heads/main/icons/emby.png"),
            "login_success": ("login_success", "✅ 登录成功提醒", self._webhook_images.get("emby")),
            "login_failed": ("login_failed", "🚫 登录失败提醒", self._webhook_images.get("emby")),
        }
        if kind not in templates:
            return {"success": False, "message": f"未知测试类型: {kind}"}
        template_key, default_template, image = templates[kind]
        action = "登录失败" if kind == "login_failed" else ("登录成功" if kind == "login_success" else "开始播放")
        text = "\n".join([
            "🧪 这是一条测试通知",
            f"🎬 媒体：{title_name}",
            f"👤 用户：{user}",
            f"🖥️ 服务器：{server}",
            f"⏰ 时间：{now}",
        ])
        title, body = self._render_notification_template(
            template_key,
            default_template,
            text,
            title=title_name,
            user=user,
            server=server,
            action=action,
            time=now,
            year="2026",
            ip="127.0.0.1",
            device="测试浏览器",
            count=3,
        )
        try:
            self._send_notification(title=title, text=body, image=image)
        except Exception as e:
            logger.error(f"发送测试通知失败: {str(e)}")
            return {"success": False, "message": f"发送失败: {str(e)}"}
        return {"success": True, "message": f"已发送：{title}"}

    @staticmethod
    def _short_page_text(value: Any, limit: int = 120, default: str = '-') -> str:
        text = str(value).strip() if value is not None else ''
        if not text:
            return default
        if len(text) > limit:
            return text[:limit].rstrip() + '...'
        return text

    @eventmanager.register(EventType.WebhookMessage)
    def send(self, event: Event):
        """发送通知消息主入口"""
        try:
            if not self._enabled:
                return

            event_info: WebhookEventInfo = event.event_data
            if not event_info:
                return

            logger.info(f"收到Webhook事件: {event_info.event}, 媒体: {event_info.item_name}, 服务器: {event_info.server_name}")
            logger.debug(f"Webhook原始数据: {json.dumps(event_info.json_object, ensure_ascii=False) if event_info.json_object else 'None'}")

            # 事件类型检查（统一转小写，兼容 Jellyfin/Plex 大小写差异）
            event_lower = str(event_info.event).lower()
            if event_lower not in self._webhook_actions_lower:
                logger.warning(f"未知的Webhook事件类型: {event_info.event}")
                return

            # 类型过滤
            if event_lower not in self._allowed_event_types:
                logger.debug(f"未开启 {event_info.event} 类型的消息通知")
                return

            # 验证媒体服务器配置
            if event_info.server_name:
                if not self.service_info(name=event_info.server_name):
                    logger.debug(f"未开启媒体服务器 {event_info.server_name} 的消息通知")
                    return

            # TMDB未识别视频过滤
            if self._filter_unrecognized and event_info.item_type in (self.MT_MOVIE, self.MT_TV, self.MT_SHOW):
                if event_lower in ("library.new", "playback.start", "playback.stop",
                                   "media.play", "media.stop", "playbackstart", "playbackstop",
                                   "playback.pause", "playback.unpause", "media.pause", "media.resume"):
                    # 仅用本地数据判断，不依赖异步 API 结果，避免误丢通知
                    tmdb_id = self._extract_tmdb_id_local(event_info)
                    if not tmdb_id:
                        logger.info(f"TMDB未识别视频，跳过通知: {event_info.item_name}")
                        return

            # 根据事件类型分发处理

            if "test" in event_lower:
                self._handle_test_event(event_info)
                return

            if "user.authentic" in event_lower:
                self._handle_login_event(event_info)
                return

            if "item." in event_lower and ("rate" in event_lower or "mark" in event_lower):
                self._handle_rate_event(event_info)
                return

            if "deep.delete" in event_lower:
                self._handle_deep_delete_event(event_info)
                return

            if event_info.json_object and event_info.json_object.get('Item', {}).get('Type') == self.MT_MUSIC_ALBUM and event_lower == 'library.new':
                self._handle_music_album(event_info, event_info.json_object.get('Item', {}))
                return

            if (self._aggregate_enabled and
                event_lower == "library.new" and
                event_info.item_type in (self.MT_TV, self.MT_SHOW)):
                series_id = self._get_series_id(event_info)
                if series_id:
                    self._aggregate_tv_episodes(series_id, event_info, event)
                    return

            self._process_media_event(event, event_info)

        except Exception as e:
            logger.error(f"Webhook分发异常: {str(e)}\n{traceback.format_exc()}")

    def _handle_test_event(self, event_info: WebhookEventInfo):
        """处理测试消息"""
        server_name = self._get_server_name_cn(event_info)
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        texts = [
            f"来自：{server_name}",
            f"时间：{now}",
            f"状态：连接正常"
        ]
        if event_info.user_name:
            texts.append(f"用户：{event_info.user_name}")

        self._send_templated_notification(
            template_key="test",
            default_title_template="🔔 媒体服务器通知测试",
            text="\n".join(texts),
            image=self._get_webhook_image(event_info.channel),
            server=server_name,
            user=event_info.user_name,
            time=now,
        )

    def _handle_login_event(self, event_info: WebhookEventInfo):
        """处理登录消息"""
        action = "登录成功" if "authenticated" in event_info.event.lower() and "failed" not in event_info.event.lower() else "登录失败"

        # 用户
        username = event_info.user_name
        if not username and event_info.json_object:
            username = event_info.json_object.get('User', {}).get('Name')
            if not username:
                title = event_info.json_object.get('Title', '')
                m = re.search(r'来自\s*(\S+)', title)
                if m:
                    username = m.group(1)

        texts = [f"👤 用户：{username or '未知用户'}"]

        # 设备信息细分
        device_name = ""
        if event_info.json_object:
            dev = event_info.json_object.get('DeviceInfo', {}) or {}
            if dev.get('Name'):
                device_name = dev['Name']
                texts.append(f"📱 设备：{device_name}")
            client_parts = []
            if dev.get('AppName'):
                client_parts.append(dev['AppName'])
            if dev.get('AppVersion'):
                client_parts.append(f"v{dev['AppVersion']}")
            if client_parts:
                texts.append(f"📱 客户端：{' · '.join(client_parts)}")

        # IP
        ip_addr = None
        if event_info.ip:
            ip_addr = event_info.ip
        elif event_info.json_object:
            desc = event_info.json_object.get('Description', '') or ''
            for line in desc.split('\n'):
                line = line.strip()
                if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', line):
                    ip_addr = line
                    break
        if ip_addr:
            try:
                location = WebUtils.get_location(ip_addr)
                texts.append(f"🌐 IP：{ip_addr} {location}")
            except Exception:
                texts.append(f"🌐 IP：{ip_addr}")

        server_name = self._get_server_name_cn(event_info)
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        texts.append(f"🖥️ 服务器：{server_name}")
        texts.append(f"⏰ 时间：{now}")

        template_key = "login_failed" if "失败" in action else "login_success"
        default_title = "🚫 登录失败提醒" if "失败" in action else "✅ 登录成功提醒"
        self._send_templated_notification(
            template_key=template_key,
            default_title_template=default_title,
            text="\n".join(texts),
            image=self._get_webhook_image(event_info.channel),
            user=username or "未知用户",
            server=server_name,
            action=action,
            time=now,
            ip=ip_addr or "",
            device=device_name,
        )

    def _handle_rate_event(self, event_info: WebhookEventInfo):
        """处理评分/标记消息"""
        tmdb_id = self._extract_tmdb_id_local(event_info)

        if self._filter_unrecognized and event_info.item_type in (self.MT_MOVIE, self.MT_TV, self.MT_SHOW):
            if not tmdb_id:
                logger.info(f"TMDB未识别视频，跳过评分通知: {event_info.item_name}")
                return

        action = self._webhook_actions.get(event_info.event) or self._webhook_actions.get(event_info.event.lower(), '已标记')
        texts = [
            f"👤 用户：{event_info.user_name or '未知用户'}",
            f"🏷️ 标记：{action}",
            f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
        ]

        if tmdb_id:
            event_info.tmdb_id = tmdb_id
        image_url = event_info.image_url
        if not image_url and tmdb_id:
            mtype = MediaType.MOVIE if event_info.item_type == self.MT_MOVIE else MediaType.TV
            image_url = self._get_tmdb_image(event_info, mtype)

        self._send_templated_notification(
            template_key="rate",
            default_title_template="⭐ 用户评分：{title}",
            text="\n".join(texts),
            image=image_url or self._get_webhook_image(event_info.channel),
            title=event_info.item_name,
            user=event_info.user_name or "未知用户",
            action=action,
            server=self._get_server_name_cn(event_info),
            time=time.strftime('%Y-%m-%d %H:%M:%S'),
        )

    def _handle_deep_delete_event(self, event_info: WebhookEventInfo):
        """处理神医助手媒体深度删除消息"""
        item_name = self._short_page_text(event_info.item_name, 120, "未知媒体")
        item_path = self._short_page_text(event_info.item_path, 300, "")

        mount_paths = []
        if event_info.json_object and isinstance(event_info.json_object, dict):
            description = event_info.json_object.get('Description', '')
            if description:
                lines = description.split('\n')
                in_mount_section = False
                for line in lines:
                    line = line.strip()
                    if line == 'Mount Paths:':
                        in_mount_section = True
                        continue
                    if in_mount_section and line:
                        if ':' in line and not line.startswith('http'):
                            break
                        if line.startswith('http') or line.startswith('/'):
                            mount_paths.append(line)

            if not mount_paths:
                mount_paths_raw = (
                    event_info.json_object.get('MountPaths') or
                    event_info.json_object.get('mount_paths')
                )
                if isinstance(mount_paths_raw, list):
                    mount_paths = [p.strip() for p in mount_paths_raw if p and p.strip()]
                elif isinstance(mount_paths_raw, str):
                    mount_paths = [p.strip() for p in mount_paths_raw.split('\n') if p.strip()]

        texts = [
            f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"📝 媒体名称：\n{item_name}"
        ]

        if item_path:
            texts.extend(["", f"📂 本地路径：\n{item_path}"])

        if mount_paths:
            texts.extend(["", "💾 挂载路径："])
            for path in mount_paths[:5]:
                texts.append(f"• {self._short_page_text(path, 200, '')}")
            if len(mount_paths) > 5:
                texts.append(f"… 及其他 {len(mount_paths) - 5} 条路径")

        self._send_templated_notification(
            template_key="deep_delete",
            default_title_template="🗑️ 神医助手 - 媒体深度删除",
            text="\n" + "\n".join(texts),
            image=None,
            title=item_name,
            time=time.strftime('%Y-%m-%d %H:%M:%S'),
        )

    def _process_media_event(self, event: Event, event_info: WebhookEventInfo):
        """处理常规媒体消息（入库/播放）"""
        try:
            # FIX: 所有对 _webhook_msg_keys 和 _last_event_cache 的读写统一在锁内完成
            expiring_key = f"{event_info.item_id}-{event_info.client}-{event_info.user_name}-{event_info.event}"

            ev_lower = str(event_info.event).lower()
            with self._lock:
                self._clean_expired_cache_locked()

                # 统一用小写比较，兼容 Jellyfin PlaybackStop / Plex media.stop
                if ev_lower in ("playback.stop", "media.stop", "playbackstop") and expiring_key in self._webhook_msg_keys:
                    self._add_key_cache_locked(expiring_key)
                    return

                current_time = time.time()
                last_event, last_time = self._last_event_cache
                if last_event and (current_time - last_time < 2):
                    if last_event.event_id == event.event_id or last_event.event_data == event_info:
                        return
                self._last_event_cache = (event, current_time)

            # 元数据识别
            _raw_path = event_info.item_path or ""
            if not _raw_path and event_info.json_object:
                _raw_path = event_info.json_object.get('Item', {}).get('Path', '')
            _path_for_match = _raw_path.lower() if _raw_path else ""
            _path_blocked = any(kw in _path_for_match for kw in self._path_skip_keywords) if (self._path_skip_keywords and _path_for_match) else False

            tmdb_id = self._extract_tmdb_id(event_info, item_path=_raw_path)
            event_info.tmdb_id = tmdb_id
            message_texts = []
            message_title = ""
            template_key = self._get_template_key_for_event(event_info.event)
            title_name = event_info.item_name or "未知媒体"
            action_base = (self._webhook_actions.get(event_info.event)
                           or self._webhook_actions.get(event_info.event.lower(), "通知"))
            category = ""
            overview = ""
            tmdb_info = None
            image_url = self._get_emby_local_image(event_info) if _path_blocked else event_info.image_url

            # 音频单曲特殊处理
            if event_info.item_type == self.MT_AUDIO:
                self._build_audio_message(event_info, message_texts)
                server_name = self._get_server_name_cn(event_info)
                song_name = (event_info.json_object.get('Item', {}).get('Name')
                             if event_info.json_object else None) or event_info.item_name or '未知媒体'
                title_name = song_name
                message_title = self._render_title_template(
                    "audio",
                    "{title} {action} {server}",
                    title=song_name,
                    action=action_base,
                    server=server_name,
                    user=event_info.user_name or "",
                    time=time.strftime('%Y-%m-%d %H:%M:%S'),
                )
                img = self._get_audio_image_url(event_info.server_name, event_info.json_object.get('Item', {}) if event_info.json_object else {})
                if img:
                    image_url = img

            # 视频处理 (TV/MOV)
            else:
                tmdb_info = None
                if tmdb_id:
                    mtype = MediaType.MOVIE if event_info.item_type == self.MT_MOVIE else MediaType.TV
                    try:
                        tmdb_info = self.chain.recognize_media(tmdbid=int(tmdb_id), mtype=mtype)
                    except Exception as e:
                        logger.error(f"识别TMDB媒体异常: {str(e)}")

                # 标题构造
                title_name = self._build_title_name(tmdb_info, event_info)
                message_title = self._build_message_title(
                    event_info.event,
                    title_name,
                    user=event_info.user_name or "",
                    server=self._get_server_name_cn(event_info),
                    time=time.strftime('%Y-%m-%d %H:%M:%S'),
                )

                # 内容构造
                message_texts.append(f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}")

                category = self._get_category(tmdb_info, event_info)
                if category:
                    message_texts.append(f"📂 分类：{category}")

                self._append_season_episode_info(message_texts, event_info)
                self._append_meta_info(message_texts, tmdb_info)
                self._append_genres_actors(message_texts, tmdb_info)

                # 简介（仅入库事件）
                overview = (tmdb_info.overview if tmdb_info and tmdb_info.overview else None) or event_info.overview
                if overview and "library.new" in ev_lower:
                    if len(overview) > self._overview_max_length:
                        overview = overview[:self._overview_max_length].rstrip() + "..."
                    message_texts.append(f"📖 简介：\n{overview}")

                # 图片
                if not image_url and not _path_blocked and tmdb_id:
                    mtype = MediaType.TV if event_info.item_type in (self.MT_TV, self.MT_SHOW) else MediaType.MOVIE
                    image_url = self._get_tmdb_image(event_info, mtype)

            if not message_title:
                logger.warning(f"消息标题为空，跳过发送: {event_info.event} / {event_info.item_name}")
                return

            # 附加信息
            self._append_extra_info(message_texts, event_info)
            play_link = self._get_play_link(event_info)

            if not image_url:
                image_url = self._get_webhook_image(event_info.channel)

            with self._lock:
                if ev_lower in ("playback.stop", "media.stop", "playbackstop"):
                    self._add_key_cache_locked(expiring_key)
                elif ev_lower in ("playback.start", "media.play", "playbackstart"):
                    self._webhook_msg_keys.pop(expiring_key, None)

            # 发送
            message_text = "\n".join(message_texts)
            if "library.new" in ev_lower:
                message_text = "\n" + message_text

            self._send_templated_notification(
                template_key=template_key,
                default_title_template=message_title,
                text=message_text,
                image=image_url,
                link=play_link,
                **self._build_template_values(
                    event_info,
                    title_name,
                    message_text,
                    tmdb_info=tmdb_info,
                    category=category,
                    overview=overview,
                    action=action_base,
                ),
            )

        except Exception as e:
            logger.error(f"处理媒体事件异常: {str(e)}\n{traceback.format_exc()}")

    # ==================== 公共辅助方法 ====================

    def _send_notification(self, title: str, text: str, image: Optional[str] = None, link: Optional[str] = None):
        image = self._normalize_notification_image(image)
        self.post_message(
            mtype=NotificationType.MediaServer,
            title=title,
            text=text,
            image=image,
            link=link
        )

    @staticmethod
    def _normalize_notification_image(image: Optional[str]) -> Optional[str]:
        """仅保留可被通知渠道直接访问的图片 URL，避免本地文件名导致部分渠道发送失败。"""
        if not image:
            return None
        image = str(image).strip()
        if re.match(r"^https?://", image, re.IGNORECASE):
            return image
        return None

    def _build_title_name(self, tmdb_info, event_info: WebhookEventInfo) -> str:
        """构建带年份的标题名称"""
        title_name = (tmdb_info.title if (tmdb_info and tmdb_info.title) else event_info.item_name) or "未知媒体"
        year = tmdb_info.year if (tmdb_info and tmdb_info.year) else (
            event_info.json_object.get('Item', {}).get('ProductionYear') if event_info.json_object else None
        )
        if year and str(year) not in title_name:
            title_name += f" ({year})"
        return title_name

    def _get_template_key_for_event(self, event: str) -> str:
        ev = str(event or "").lower()
        if "library.new" in ev:
            return "library_new"
        if "playback.start" in ev or "media.play" in ev or "playbackstart" in ev:
            return "playback_start"
        if "playback.stop" in ev or "media.stop" in ev or "playbackstop" in ev:
            return "playback_stop"
        if "pause" in ev:
            return "playback_pause"
        if "resume" in ev or "unpause" in ev:
            return "playback_resume"
        return "audio" if ev else "test"

    def _build_message_title(self, event: str, title_name: str, **values) -> str:
        """根据事件类型构建消息标题"""
        ev = event.lower()
        context = {"title": title_name, "action": self._webhook_actions.get(event) or self._webhook_actions.get(ev, "通知")}
        context.update(values)
        if "library.new" in ev:
            return self._render_title_template("library_new", "🆕 {title} 已入库", **context)
        elif "playback.start" in ev or "media.play" in ev or "playbackstart" in ev:
            return self._render_title_template("playback_start", "▶️ 开始播放：{title}", **context)
        elif "playback.stop" in ev or "media.stop" in ev or "playbackstop" in ev:
            return self._render_title_template("playback_stop", "⏹️ 停止播放：{title}", **context)
        elif "pause" in ev:
            return self._render_title_template("playback_pause", "⏸️ 暂停播放：{title}", **context)
        elif "resume" in ev or "unpause" in ev:
            return self._render_title_template("playback_resume", "▶️ 继续播放：{title}", **context)
        else:
            return f"📢 {context['action']}：{title_name}"

    def _get_category(self, tmdb_info, event_info: WebhookEventInfo) -> Optional[str]:
        """获取分类（优先智能分类，fallback路径解析）"""
        category = None
        if self._smart_category_enabled and tmdb_info and self.category:
            try:
                if event_info.item_type == self.MT_MOVIE:
                    category = self.category.get_movie_category(tmdb_info)
                else:
                    category = self.category.get_tv_category(tmdb_info)
            except Exception:
                pass

        if not category:
            is_folder = event_info.json_object.get('Item', {}).get('IsFolder', False) if event_info.json_object else False
            category = self._get_category_from_path(event_info.item_path, event_info.item_type, is_folder)

        return category

    # ==================== 辅助构建函数 ====================

    def _build_audio_message(self, event_info, texts):
        """构建音频消息内容"""
        item_data = event_info.json_object.get('Item', {}) if event_info.json_object else {}
        artist = (item_data.get('Artists') or ['未知歌手'])[0]
        album = item_data.get('Album', '')
        duration = self._format_ticks(item_data.get('RunTimeTicks', 0))
        container = item_data.get('Container', '').upper()
        size = self._format_size(item_data.get('Size', 0))

        texts.append(f"⏰ 时间：{time.strftime('%H:%M:%S', time.localtime())}")
        texts.append(f"👤 歌手：{artist}")
        if album:
            texts.append(f"💿 专辑：{album}")
        texts.append(f"⏱️ 时长：{duration}")
        texts.append(f"📦 格式：{container} · {size}")

    def _get_series_id(self, event_info: WebhookEventInfo) -> Optional[str]:
        """获取剧集系列ID"""
        if event_info.json_object and isinstance(event_info.json_object, dict):
            item = event_info.json_object.get("Item", {})
            return item.get("SeriesId") or getattr(event_info, "series_id", None)
        return getattr(event_info, "series_id", None)

    # ==================== 剧集聚合逻辑 ====================

    def _aggregate_tv_episodes(self, series_id: str, event_info: WebhookEventInfo, event: Event):
        """聚合TV剧集消息"""
        with self._lock:
            if series_id not in self._pending_messages:
                self._pending_messages[series_id] = []

            self._pending_messages[series_id].append((event_info, event))

            if series_id in self._aggregate_timers:
                self._aggregate_timers[series_id].cancel()

            timer = threading.Timer(self._aggregate_time, self._send_aggregated_message, [series_id])
            timer.daemon = True
            self._aggregate_timers[series_id] = timer
            timer.start()

    def _send_aggregated_message(self, series_id: str):
        """发送聚合的剧集消息"""
        with self._lock:
            if series_id not in self._pending_messages or not self._pending_messages[series_id]:
                self._aggregate_timers.pop(series_id, None)
                return

            msg_list = self._pending_messages.pop(series_id)
            self._aggregate_timers.pop(series_id, None)

        if not msg_list:
            return

        # 单条直接回退到常规处理
        if len(msg_list) == 1:
            self._process_media_event(msg_list[0][1], msg_list[0][0])
            return

        # 多条聚合
        self._do_send_aggregated(msg_list)

    def _do_send_aggregated(self, msg_list: list):
        """聚合消息发送核心逻辑，供 _send_aggregated_message 和 _send_aggregated_message_from_list 共用"""
        first_info = msg_list[0][0]
        events_info = [x[0] for x in msg_list]
        count = len(events_info)

        tmdb_id = self._extract_tmdb_id(first_info)
        if not tmdb_id:
            series_id = self._get_series_id(first_info)
            tmdb_id = self._get_series_tmdb_cache(series_id) if series_id else None
        first_info.tmdb_id = tmdb_id
        tmdb_info = None
        if tmdb_id:
            try:
                tmdb_info = self.chain.recognize_media(tmdbid=int(tmdb_id), mtype=MediaType.TV)
            except Exception as e:
                logger.error(f"识别TMDB信息异常: {str(e)}")

        title_name = first_info.item_name or "未知媒体"
        if first_info.json_object:
            title_name = first_info.json_object.get('Item', {}).get('SeriesName') or title_name

        year = tmdb_info.year if (tmdb_info and tmdb_info.year) else (
            first_info.json_object.get('Item', {}).get('ProductionYear') if first_info.json_object else None
        )
        if year and str(year) not in title_name:
            title_name += f" ({year})"

        message_title = self._render_title_template(
            "library_aggregate",
            "🆕 {title} 已入库 (含{count}个文件)",
            title=title_name,
            count=count,
            server=self._get_server_name_cn(first_info),
            time=time.strftime('%Y-%m-%d %H:%M:%S'),
        )

        message_texts = [f"⏰ {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}"]

        category = self._get_category(tmdb_info, first_info)
        if category:
            message_texts.append(f"📂 分类：{category}")

        episodes_str = self._merge_continuous_episodes(events_info)
        if episodes_str:
            message_texts.append(f"📺 季集：{episodes_str}")

        self._append_meta_info(message_texts, tmdb_info)
        self._append_genres_actors(message_texts, tmdb_info)

        overview = (tmdb_info.overview if tmdb_info and tmdb_info.overview else None) or first_info.overview
        if overview:
            if len(overview) > self._overview_max_length:
                overview = overview[:self._overview_max_length].rstrip() + "..."
            message_texts.append(f"📖 简介：\n{overview}")

        image_url = first_info.image_url
        if not image_url and tmdb_id:
            image_url = self._get_tmdb_image(first_info, MediaType.TV)
        if not image_url:
            image_url = self._get_webhook_image(first_info.channel)

        play_link = self._get_play_link(first_info)

        aggregate_text = "\n" + "\n".join(message_texts)
        self._send_templated_notification(
            template_key="library_aggregate",
            default_title_template=message_title,
            text=aggregate_text,
            image=image_url,
            link=play_link,
            **self._build_template_values(
                first_info,
                title_name,
                aggregate_text,
                tmdb_info=tmdb_info,
                category=category,
                overview=overview,
                season_episode=episodes_str,
                count=count,
                action="已入库",
            ),
        )

    # ==================== 集数合并逻辑 ====================

    def _merge_continuous_episodes(self, events: List[WebhookEventInfo]) -> str:
        """合并连续剧集"""
        season_episodes = {}
        for event in events:
            season, episode = None, None
            if event.json_object and isinstance(event.json_object, dict):
                item = event.json_object.get("Item", {})
                season = item.get("ParentIndexNumber")
                episode = item.get("IndexNumber")

            if season is None:
                season = getattr(event, "season_id", None)
            if episode is None:
                episode = getattr(event, "episode_id", None)

            if season is not None and episode is not None:
                season_episodes.setdefault(season, set()).add(int(episode))

        merged_details = []
        for season in sorted(season_episodes.keys()):
            episodes = sorted(season_episodes[season])
            if not episodes:
                continue

            start = end = episodes[0]
            for i in range(1, len(episodes)):
                current = episodes[i]
                if current == end + 1:
                    end = current
                else:
                    merged_details.append(
                        f"S{str(season).zfill(2)}E{str(start).zfill(2)}-E{str(end).zfill(2)}" if start != end
                        else f"S{str(season).zfill(2)}E{str(start).zfill(2)}"
                    )
                    start = end = current

            merged_details.append(
                f"S{str(season).zfill(2)}E{str(start).zfill(2)}-E{str(end).zfill(2)}" if start != end
                else f"S{str(season).zfill(2)}E{str(start).zfill(2)}"
            )

        return ", ".join(merged_details)

    def _extract_tmdb_id_local(self, event_info: WebhookEventInfo, item_path: str = None) -> Optional[str]:
        """仅从本地数据提取TMDB ID，不发起网络请求。
        item_path 可由调用方传入以避免重复解析。
        """
        if item_path is None:
            item_path = event_info.item_path or ""
            if not item_path and event_info.json_object:
                item_path = event_info.json_object.get('Item', {}).get('Path', '')
        if self._path_skip_keywords and item_path:
            item_path_lower = item_path.lower()
            if any(kw in item_path_lower for kw in self._path_skip_keywords):
                logger.info(f"路径命中黑名单，跳过TMDB识别: {item_path}")
                return None

        if event_info.tmdb_id:
            return event_info.tmdb_id

        if event_info.json_object:
            tmdb_id = event_info.json_object.get('Item', {}).get('ProviderIds', {}).get('Tmdb')
            if tmdb_id:
                return tmdb_id

        if item_path:
            if match := re.search(r'[\[{](?:tmdbid|tmdb)[=-](\d+)[\]}]', item_path, re.IGNORECASE):
                return match.group(1)

        return None

    def _get_series_tmdb_cache(self, series_id: str) -> Optional[str]:
        if not series_id:
            return None
        now = time.time()
        with self._lock:
            cached = self._series_tmdb_cache.get(series_id)
            if not cached:
                return None
            tmdb_id, expires_at = cached
            if expires_at <= now:
                self._series_tmdb_cache.pop(series_id, None)
                return None
            return tmdb_id

    def _set_series_tmdb_cache(self, series_id: str, tmdb_id: Optional[str]):
        if not series_id:
            return
        ttl = self.SERIES_TMDB_CACHE_TTL if tmdb_id else self.SERIES_TMDB_NEGATIVE_CACHE_TTL
        with self._lock:
            self._series_tmdb_cache[series_id] = (tmdb_id, time.time() + ttl)

    def _extract_tmdb_id(self, event_info: WebhookEventInfo, item_path: str = None) -> Optional[str]:
        """提取TMDB ID。
        先从本地数据查找；若为剧集且本地无结果，启动后台线程从 API 补全（写回 event_info.tmdb_id）。
        """
        tmdb_id = self._extract_tmdb_id_local(event_info, item_path=item_path)
        if tmdb_id:
            return tmdb_id

        if event_info.json_object:
            item_data = event_info.json_object.get('Item', {})
            series_id = item_data.get('SeriesId')
            if series_id and item_data.get('Type') == 'Episode':
                cached_tmdb_id = self._get_series_tmdb_cache(series_id)
                if cached_tmdb_id:
                    event_info.tmdb_id = cached_tmdb_id
                    return cached_tmdb_id

                should_fetch = False
                with self._lock:
                    if series_id not in self._series_tmdb_inflight:
                        self._series_tmdb_inflight.add(series_id)
                        should_fetch = True
                if should_fetch:
                    t = threading.Thread(
                        target=self._fetch_series_tmdb_id_async,
                        args=(event_info, series_id),
                        daemon=True
                    )
                    t.start()

        return None

    def _http_get(self, url: str, timeout: int = 5) -> Optional[requests.Response]:
        try:
            response = self._http_session.get(url, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as e:
            logger.debug(f"HTTP请求失败: {type(e).__name__}: {str(e)}")
            return None

    def _http_get_json(self, url: str, timeout: int = 5) -> Optional[dict]:
        response = self._http_get(url, timeout=timeout)
        if not response:
            return None
        try:
            return response.json()
        except ValueError as e:
            logger.debug(f"HTTP响应JSON解析失败: {url} - {str(e)}")
            return None

    def _fetch_series_tmdb_id_async(self, event_info, series_id: str):
        """后台线程：从媒体服务器 API 查询剧集系列的 TMDB ID，写入共享缓存并回填当前 event_info"""
        try:
            if not self._enabled:
                return
            if not series_id:
                return
            service = self.service_info(event_info.server_name)
            if not service:
                self._set_series_tmdb_cache(series_id, None)
                return
            host = service.config.config.get('host')
            apikey = service.config.config.get('apikey')
            if not host or not apikey:
                self._set_series_tmdb_cache(series_id, None)
                return
            api_path = self._get_api_path(event_info.server_name)
            if api_path is None:
                self._set_series_tmdb_cache(series_id, None)
                return
            api_url = f"{host}{api_path}/Items?Ids={series_id}&Fields=ProviderIds&api_key={apikey}"
            data = self._http_get_json(api_url, timeout=5)
            tmdb_id = None
            if data and data.get('Items'):
                parent_ids = data['Items'][0].get('ProviderIds', {})
                tmdb_id = parent_ids.get('Tmdb')
            if tmdb_id:
                event_info.tmdb_id = tmdb_id
                logger.debug(f"异步获取系列 TMDB ID 成功: {tmdb_id}")
            self._set_series_tmdb_cache(series_id, tmdb_id)
        except Exception as e:
            self._set_series_tmdb_cache(series_id, None)
            logger.debug(f"异步获取系列 TMDB ID 异常: {str(e)}")
        finally:
            with self._lock:
                self._series_tmdb_inflight.discard(series_id)

    def _get_api_path(self, server_name: str) -> Optional[str]:
        """根据服务器类型返回 API 路径前缀。
        优先通过 ServiceInfo.config.type 判断，名称匹配作为 fallback。
        Plex 使用完全不同的 API，返回 None 表示不支持 Emby/Jellyfin Items API。
        """
        if not server_name:
            return "/emby"
        # 优先用 ServiceInfo 中的 type 字段判断
        service = self.service_info(server_name)
        if service and service.config:
            stype = (getattr(service.config, "type", None) or "").lower()
            if stype == "plex":
                return None
            if stype == "jellyfin":
                return ""
            if stype == "emby":
                return "/emby"
        # fallback: 名称字符串匹配
        server_lower = server_name.lower()
        if "plex" in server_lower:
            return None
        if "jellyfin" in server_lower:
            return ""
        return "/emby"

    def _get_server_name_cn(self, event_info):
        """获取服务器中文名称"""
        if event_info.json_object and isinstance(event_info.json_object.get('Server'), dict):
            name = event_info.json_object.get('Server', {}).get('Name')
            if name:
                return name
        return event_info.server_name or "媒体服务器"

    def _get_emby_local_image(self, event_info: WebhookEventInfo) -> Optional[str]:
        """从Emby本地构造图片URL，优先Backdrop横幅图"""
        try:
            if not event_info.json_object:
                return None
            item_data = event_info.json_object.get('Item', {})
            item_id = item_data.get('Id')
            if not item_id:
                return None
            service = self.service_info(event_info.server_name)
            if not service:
                return None
            host = (self._emby_image_host or service.config.config.get('host', '')).rstrip('/')
            if not host:
                return None
            api_path = self._get_api_path(event_info.server_name)
            if api_path is None:
                return None
            # 优先Backdrop
            backdrop_tags = item_data.get('BackdropImageTags', [])
            if backdrop_tags:
                tag = backdrop_tags[0]
                return f"{host}{api_path}/Items/{item_id}/Images/Backdrop/0?tag={tag}&maxWidth=1920&quality=70"
            # 回退Primary
            image_tags = item_data.get('ImageTags', {})
            tag = image_tags.get('Primary') or image_tags.get('Thumb')
            image_type = 'Primary' if image_tags.get('Primary') else 'Thumb'
            if not tag:
                return None
            return f"{host}{api_path}/Items/{item_id}/Images/{image_type}?maxHeight=450&maxWidth=450&tag={tag}&quality=90"
        except Exception:
            return None

    def _get_audio_image_url(self, server_name: str, item_data: dict) -> Optional[str]:
        """获取音频图片URL"""
        if not server_name:
            return None
        try:
            service = self.service_info(server_name)
            if not service:
                return None
            # 直接从配置获取 host，不用 get_play_url("dummy") 这种 hack
            base_url = service.config.config.get("host", "").rstrip("/")
            if not base_url:
                return None

            item_id = item_data.get('Id')
            primary_tag = item_data.get('ImageTags', {}).get('Primary')

            if not primary_tag:
                item_id = item_data.get('PrimaryImageItemId')
                primary_tag = item_data.get('PrimaryImageTag')

            if item_id and primary_tag:
                api_path = self._get_api_path(server_name)
                if api_path is None:
                    return None
                return f"{base_url}{api_path}/Items/{item_id}/Images/Primary?maxHeight=450&maxWidth=450&tag={primary_tag}&quality=90"
        except Exception:
            pass
        return None

    def _get_tmdb_image(self, event_info: WebhookEventInfo, mtype: MediaType) -> Optional[str]:
        """
        获取TMDB图片。
        注意：插件自身的内存缓存已移除，完全依赖 MoviePilot 核心的 self.chain.obtain_specific_image 缓存机制。
        """
        try:
            # 优先获取横版背景图 (Backdrop)
            img = self.chain.obtain_specific_image(
                mediaid=event_info.tmdb_id, mtype=mtype,
                image_type=MediaImageType.Backdrop,
                season=event_info.season_id, episode=event_info.episode_id
            )
            # 若无背景图，回退到竖版海报图 (Poster)
            if not img:
                img = self.chain.obtain_specific_image(
                    mediaid=event_info.tmdb_id, mtype=mtype,
                    image_type=MediaImageType.Poster,
                    season=event_info.season_id, episode=event_info.episode_id
                )
            return img
        except Exception as e:
            logger.error(f"获取TMDB图片异常: {str(e)}")
            return None

    def _get_category_from_path(self, path: str, item_type: str, is_folder: bool = False) -> str:
        """从路径获取分类"""
        if not path:
            return ""
        try:
            path = os.path.normpath(path)

            if is_folder and item_type in (self.MT_TV, self.MT_SHOW):
                return os.path.basename(os.path.dirname(path))

            current_dir = os.path.dirname(path)
            dir_name = os.path.basename(current_dir)

            if re.search(r'^(Season|季|S\d)', dir_name, re.IGNORECASE):
                current_dir = os.path.dirname(current_dir)

            category = os.path.basename(os.path.dirname(current_dir))
            if not category or category == os.path.sep:
                return ""
            return category
        except Exception:
            return ""

    def _handle_music_album(self, event_info: WebhookEventInfo, item_data: dict):
        """处理音乐专辑 — 启动后台线程，避免阻塞事件回调"""
        # FIX P1: 异步化，不阻塞事件处理线程
        threading.Thread(
            target=self._handle_music_album_async,
            args=(event_info, item_data),
            daemon=True
        ).start()

    def _handle_music_album_async(self, event_info: WebhookEventInfo, item_data: dict):
        """后台线程：拉取专辑曲目并发送通知"""
        try:
            if not self._enabled:
                return
            album_name = item_data.get('Name', '')
            album_id = item_data.get('Id', '')
            album_artist = (item_data.get('Artists') or ['未知艺术家'])[0]
            primary_image_item_id = item_data.get('PrimaryImageItemId') or album_id
            primary_image_tag = item_data.get('PrimaryImageTag') or item_data.get('ImageTags', {}).get('Primary')

            service = self.service_info(event_info.server_name)
            if not service or not service.instance:
                return

            base_url = service.config.config.get('host', '')
            api_key = service.config.config.get('apikey', '')
            if not base_url or not api_key:
                return

            # FIX P0: Plex 不支持此 API，跳过
            api_path = self._get_api_path(event_info.server_name)
            if api_path is None:
                logger.debug(f"服务器 {event_info.server_name} 不支持专辑曲目 API")
                return

            fields = "Path,MediaStreams,Container,Size,RunTimeTicks,ImageTags,ProviderIds"
            api_url = f"{base_url}{api_path}/Items?ParentId={album_id}&Fields={fields}&api_key={api_key}"

            data = self._http_get_json(api_url, timeout=10)
            if not data:
                return
            items = data.get('Items', [])
            logger.info(f"专辑 [{album_name}] 包含 {len(items)} 首歌曲")
            for song in items:
                self._send_single_audio_notify(
                    song, album_name, album_artist,
                    primary_image_item_id, primary_image_tag,
                    base_url, event_info.server_name
                )

        except Exception as e:
            logger.error(f"处理音乐专辑失败: {str(e)}\n{traceback.format_exc()}")

    def _send_single_audio_notify(self, song: dict, album_name, album_artist,
                                  cover_item_id, cover_tag, base_url, server_name: str = None):
        """发送单曲通知"""
        try:
            song_name = song.get('Name', '未知歌曲')
            song_id = song.get('Id')
            artist = (song.get('Artists') or [album_artist])[0]
            duration = self._format_ticks(song.get('RunTimeTicks', 0))
            container = song.get('Container', '').upper()
            size = self._format_size(song.get('Size', 0))

            texts = [
                f"⏰ 入库：{time.strftime('%H:%M:%S', time.localtime())}",
                f"👤 歌手：{artist}"
            ]
            if album_name:
                texts.append(f"💿 专辑：{album_name}")
            texts.append(f"⏱️ 时长：{duration}")
            texts.append(f"📦 格式：{container} · {size}")

            image_url = None
            if cover_item_id and cover_tag:
                api_path = self._get_api_path(server_name) if server_name else "/emby"
                if api_path is not None:
                    image_url = f"{base_url}{api_path}/Items/{cover_item_id}/Images/Primary?maxHeight=450&maxWidth=450&tag={cover_tag}&quality=90"

            link = None
            if self._add_play_link:
                link = f"{base_url}/web/index.html#!/item?id={song_id}&serverId={song.get('ServerId', '')}"

            self._send_templated_notification(
                template_key="audio_library",
                default_title_template="🎵 新入库媒体：{title}",
                text="\n" + "\n".join(texts),
                image=image_url,
                link=link,
                title=song_name,
                action="已入库",
                server=server_name or "媒体服务器",
                time=time.strftime('%Y-%m-%d %H:%M:%S'),
            )
        except Exception as e:
            logger.error(f"发送单曲通知失败: {str(e)}")

    def _append_meta_info(self, texts: List[str], tmdb_info):
        """追加元数据信息（评分）"""
        if not tmdb_info:
            return
        if hasattr(tmdb_info, 'vote_average') and tmdb_info.vote_average:
            score = round(float(tmdb_info.vote_average), 1)
            texts.append(f"⭐️ 评分：{score}")

    def _append_genres_actors(self, texts: List[str], tmdb_info):
        """追加演员信息"""
        if not tmdb_info:
            return
        if hasattr(tmdb_info, 'actors') and tmdb_info.actors:
            actors = [a.get('name') if isinstance(a, dict) else str(a) for a in tmdb_info.actors[:3]]
            if actors:
                texts.append(f"🎬 演员：{'、'.join(actors)}")

    def _append_season_episode_info(self, texts: List[str], event_info: WebhookEventInfo):
        """追加季集信息"""
        if event_info.season_id is not None and event_info.episode_id is not None:
            s_str, e_str = str(event_info.season_id).zfill(2), str(event_info.episode_id).zfill(2)
            texts.append(f"📺 季集：S{s_str}E{e_str}")
        elif event_info.json_object and isinstance(event_info.json_object, dict):
            description = event_info.json_object.get('Description')
            if description:
                first_line = description.split('\n\n')[0].strip()
                if re.search(r'S\d+\s+E\d+', first_line):
                    texts.append(f"📺 季集：{first_line}")

    def _append_extra_info(self, texts: List[str], event_info: WebhookEventInfo):
        """追加额外信息（用户、设备、IP、进度）"""
        if event_info.user_name:
            texts.append(f"👤 用户：{event_info.user_name}")

        if event_info.device_name:
            device = event_info.device_name
            if event_info.client and event_info.client not in device:
                device = f"{event_info.client} {device}"
            texts.append(f"📱 设备：{device}")

        if event_info.ip:
            try:
                location = WebUtils.get_location(event_info.ip)
                texts.append(f"🌐 IP：{event_info.ip} ({location})")
            except Exception:
                texts.append(f"🌐 IP：{event_info.ip}")

        if event_info.percentage is not None:
            percentage = round(float(event_info.percentage), 2)
            texts.append(f"📊 进度：{percentage}%")

    def _get_play_link(self, event_info: WebhookEventInfo) -> Optional[str]:
        """获取播放链接"""
        if not self._add_play_link or not event_info.server_name:
            return None
        service = self.service_info(event_info.server_name)
        if service and service.instance:
            return service.instance.get_play_url(event_info.item_id)
        return None

    def _format_ticks(self, ticks) -> str:
        """格式化时间刻度，支持小时"""
        if not ticks:
            return "00:00"
        s = int(ticks / 10000000)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{sec:02d}"
        return f"{m}:{sec:02d}"

    def _format_size(self, size) -> str:
        """格式化文件大小，自动切换 GB/MB 单位"""
        if not size:
            return "0 MB"
        mb = size / 1024 / 1024
        if mb >= 1024:
            return f"{round(mb / 1024, 2)} GB"
        return f"{round(mb, 1)} MB"

    def _add_key_cache_locked(self, key):
        """添加元素到过期字典（调用方须持有 self._lock）"""
        self._webhook_msg_keys[key] = time.time() + self.DEFAULT_EXPIRATION_TIME

    def _clean_expired_cache_locked(self):
        """清理过期缓存（调用方须持有 self._lock）"""
        if not self._webhook_msg_keys:
            return
        ct = time.time()
        self._webhook_msg_keys = {k: v for k, v in self._webhook_msg_keys.items() if v > ct}

    def stop_service(self):
        """退出插件时的清理工作"""
        try:
            with self._lock:
                pending_snapshot = {sid: msgs[:] for sid, msgs in self._pending_messages.items()}
                self._pending_messages.clear()
                has_timers = bool(self._aggregate_timers)
                for timer in self._aggregate_timers.values():
                    try:
                        timer.cancel()
                    except Exception:
                        pass
                self._aggregate_timers.clear()

            if pending_snapshot or has_timers:
                logger.info("插件停止，开始清理工作")

            # 在锁外发送剩余聚合消息
            for series_id, msg_list in pending_snapshot.items():
                if not msg_list:
                    continue
                try:
                    if len(msg_list) == 1:
                        self._process_media_event(msg_list[0][1], msg_list[0][0])
                    else:
                        self._send_aggregated_message_from_list(msg_list)
                except Exception as e:
                    logger.error(f"stop_service 发送聚合消息出错: {str(e)}")

            with self._lock:
                self._webhook_msg_keys.clear()
                self._service_infos_cache = (None, 0.0)
                self._series_tmdb_cache.clear()
                self._series_tmdb_inflight.clear()
            self._http_session.close()
            self._http_session = requests.Session()
            if pending_snapshot or has_timers:
                logger.info("插件清理完成")
        except Exception as e:
            logger.error(f"插件停止时发生错误: {str(e)}")

    def _send_aggregated_message_from_list(self, msg_list: list):
        """供 stop_service 调用：直接传入消息列表发送聚合通知，不经过 _pending_messages"""
        if not msg_list:
            return
        if len(msg_list) == 1:
            self._process_media_event(msg_list[0][1], msg_list[0][0])
            return
        self._do_send_aggregated(msg_list)
