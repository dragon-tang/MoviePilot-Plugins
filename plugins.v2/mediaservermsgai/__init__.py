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
    IMAGE_CACHE_MAX_SIZE = 100
    IMAGE_NEGATIVE_CACHE_TTL = 300
    SERIES_TMDB_CACHE_TTL = 3600
    SERIES_TMDB_NEGATIVE_CACHE_TTL = 300

    # ==================== 插件基本信息 ====================
    plugin_name = "媒体库服务器通知AI版"
    plugin_desc = "基于Emby识别结果+TMDB元数据+微信清爽版(全消息类型+剧集聚合+未识别过滤)"
    plugin_icon = "mediaplay.png"
    plugin_version = "2.0.6"
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
        "emby": "Emby_A.png",
        "plex": "Plex_A.png",
        "jellyfin": "Jellyfin_A.png"
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
        self._image_cache = OrderedDict()
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
            {"title": "登录提醒", "value": "user.authenticated|user.authenticationfailed"},
            {"title": "系统测试", "value": "system.webhooktest|system.notificationtest"},
            {"title": "媒体深度删除", "value": "deep.delete"},
        ]
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'add_play_link', 'label': '添加播放链接'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VSelect', 'props': {'multiple': True, 'chips': True, 'clearable': True, 'model': 'mediaservers', 'label': '媒体服务器', 'items': self._get_mediaserver_items()}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VSelect', 'props': {'chips': True, 'multiple': True, 'model': 'types', 'label': '消息类型', 'items': types_options}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'aggregate_enabled', 'label': '启用TV剧集入库聚合'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'smart_category_enabled', 'label': '启用智能分类（关闭则使用路径解析）'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'props': {'show': '{{aggregate_enabled}}'},
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'aggregate_time', 'label': '聚合等待时间（秒）', 'placeholder': '15', 'type': 'number'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'filter_unrecognized', 'label': 'TMDB未识别视频不发送通知', 'hint': '启用后，未识别到TMDB信息的视频（入库和播放）都不会发送通知'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextarea', 'props': {'model': 'path_skip_keywords', 'label': '路径关键词黑名单（跳过TMDB识别）', 'placeholder': '每行一个关键词，Path包含任意关键词时跳过TMDB识别\n例如：\n日本有码\n日本无码', 'rows': 4, 'hint': '命中关键词的媒体不会进行TMDB识别，若同时开启「未识别过滤」则也不会发送通知'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'overview_max_length', 'label': '简介最大长度', 'placeholder': '150', 'type': 'number', 'hint': '入库通知中简介文字的最大字符数，超出部分截断，默认150'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextField', 'props': {'model': 'emby_image_host', 'label': '自定义Emby图片Host', 'placeholder': '例如：http://1.1.1.1:8099', 'hint': '拦截路径的媒体图片将使用此Host构造URL，留空则使用插件内配置的Emby地址'}}]}
                        ]
                    }
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
            with self._lock:
                last_event = dict(self._last_event_snapshot)
                last_notify = dict(self._last_notification_snapshot)
                history = list(self._event_history or [])
                cache_count = len(self._image_cache)
                
            status_color = "success" if self._enabled else "error"
            status_text = "运行中" if self._enabled else "已停止"

            return [
                {
                    'component': 'VRow',
                    'content': [
                        # --- 【左侧区域】 占据 8/12 宽度 ---
                        {
                            'component': 'VCol',
                            'props': {'cols': 12, 'md': 8},
                            'content': [
                                # 左上：两个统计小卡片 (md=6, 平分左侧宽度)
                                {
                                    'component': 'VRow',
                                    'content': [
                                        self._build_stat_card("累计处理次数", str(self._total_events), "mdi-history", "primary", md=6),
                                        self._build_stat_card("TMDB 图片缓存", str(cache_count), "mdi-image-multiple", "info", md=6),
                                    ]
                                },
                                # 左中：配置 + 最新事件
                                {
                                    'component': 'VRow',
                                    'props': {'class': 'mt-2'},
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                            {'component': 'VCard', 'props': {'variant': 'flat', 'class': 'fill-height'}, 'content': [
                                                {'component': 'VCardTitle', 'text': '🛠️ 核心配置'},
                                                {'component': 'VDivider'},
                                                {'component': 'VCardText', 'content': [
                                                    self._render_info_item("媒体服务器", "、".join(self._mediaservers or ["未选择"])),
                                                    self._render_info_item("剧集聚合", f"{self._aggregate_time}s" if self._aggregate_enabled else "已禁用"),
                                                    self._render_info_item("智能分类", "✅ 开启" if self._smart_category_enabled else "❌ 关闭"),
                                                    self._render_info_item("未识别过滤", "🛡️ 开启" if self._filter_unrecognized else "🔓 关闭"),
                                                ]}
                                            ]}
                                        ]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                            {'component': 'VCard', 'props': {'variant': 'flat', 'class': 'fill-height'}, 'content': [
                                                {'component': 'VCardTitle', 'text': '📡 最新事件'},
                                                {'component': 'VDivider'},
                                                {'component': 'VCardText', 'content': self._build_event_detail(last_event)}
                                            ]}
                                        ]}
                                    ]
                                },
                                # 左下：历史表格 (独立一行)
                                {
                                    'component': 'VCard',
                                    'props': {'class': 'mt-4', 'variant': 'flat'},
                                    'content': [
                                        {'component': 'VCardTitle', 'text': '📜 处理历史 (最近5条)'},
                                        {'component': 'VDivider'},
                                        {
                                            'component': 'VTable',
                                            'props': {'density': 'compact'},
                                            'content': [
                                                {'component': 'thead', 'content': [{'component': 'tr', 'content': [
                                                    {'component': 'th', 'text': '时间'},
                                                    {'component': 'th', 'text': '事件'},
                                                    {'component': 'th', 'text': '结果'}
                                                ]}]},
                                                {'component': 'tbody', 'content': [
                                                    {'component': 'tr', 'content': [
                                                        {'component': 'td', 'text': h.get('time', '')[11:16]}, # 仅显示 12:00 格式
                                                        {'component': 'td', 'props': {'class': 'text-truncate', 'style': 'max-width: 180px'}, 'text': h.get('media', '-')},
                                                        {'component': 'td', 'content': [{'component': 'VChip', 'props': {'color': 'success', 'size': 'x-small'}, 'text': '已完成'}]}
                                                    ]} for h in history
                                                ] if history else [
                                                    {'component': 'tr', 'content': [{'component': 'td', 'props': {'colspan': 3, 'class': 'text-center pa-4'}, 'text': '暂无历史'}]}
                                                ]}
                                            ]
                                        }
                                    ]
                                }
                            ]
                        },
                        # --- 【右侧区域】 占据 4/12 宽度 (插件状态 + 预览图) ---
                        {
                            'component': 'VCol',
                            'props': {'cols': 12, 'md': 4},
                            'content': [
                                # 右侧顶部：插件状态卡片 (md=12 铺满侧边栏宽度)
                                self._build_stat_card("插件状态", status_text, "mdi-play-circle", status_color, md=12),
                                # 右侧主体：通知预览 (自适应剩余高度)
                                {
                                    'component': 'VCard',
                                    'props': {'class': 'mt-4 d-flex flex-column', 'variant': 'flat', 'style': 'min-height: 540px'},
                                    'content': [
                                        {'component': 'VCardTitle', 'text': '🔔 通知预览'},
                                        {'component': 'VDivider'},
                                        {
                                            'component': 'VImg',
                                            'props': {
                                                'src': last_notify.get('image'),
                                                'height': '280',
                                                'cover': True,
                                                'class': 'bg-grey-lighten-2',
                                                'show': bool(last_notify.get('image'))
                                            }
                                        } if last_notify.get('image') else {'component': 'VSpacer'},
                                        {
                                            'component': 'VCardText',
                                            'props': {'class': 'flex-grow-1'},
                                            'content': [
                                                {'component': 'div', 'props': {'class': 'text-subtitle-1 font-weight-bold mb-2'}, 'text': last_notify.get('title', '等待入库...')},
                                                {'component': 'div', 'props': {'class': 'text-caption'}, 'text': last_notify.get('text', '-')}
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]

    @staticmethod
    def _short_page_text(value: Any, limit: int = 120, default: str = '-') -> str:
        text = str(value).strip() if value is not None else ''
        if not text:
            return default
        if len(text) > limit:
            return text[:limit].rstrip() + '...'
        return text

    def _build_stat_card(self, title: str, value: str, icon: str, color: str, md: int = 4) -> dict:
            """构建统计卡片，md 参数决定它占几分之十二的宽度"""
            return {
                'component': 'VCol',
                'props': {'cols': 12, 'sm': 6, 'md': md}, 
                'content': [
                    {
                        'component': 'VCard',
                        'props': {'color': color, 'variant': 'tonal', 'class': 'fill-height'},
                        'content': [
                            {
                                'component': 'VListItems',
                                'props': {'align': 'center', 'class': 'pa-2'},
                                'content': [
                                    {'component': 'VIcon', 'props': {'icon': icon, 'size': 'large', 'class': 'mr-2'}},
                                    {
                                        'component': 'div',
                                        'content': [
                                            {'component': 'div', 'props': {'class': 'text-caption'}, 'text': title},
                                            {'component': 'div', 'props': {'class': 'text-h6 font-weight-bold'}, 'text': value}
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }

    def _render_info_item(self, label: str, value: str) -> dict:
        """渲染名值对"""
        return {
            'component': 'div',
            'props': {'class': 'd-flex justify-space-between mb-1'},
            'content': [
                {'component': 'span', 'props': {'class': 'text-grey-darken-1'}, 'text': f"{label}:"},
                {'component': 'span', 'props': {'class': 'font-weight-medium'}, 'text': value}
            ]
        }

    def _build_event_detail(self, event: dict) -> List[dict]:
        """构建事件详情列表"""
        if not event:
            return [{'component': 'div', 'props': {'class': 'text-center py-4 text-grey'}, 'text': '等待事件触发...'}]
        
        return [
            self._render_info_item("触发时间", event.get('time', '-')),
            self._render_info_item("操作类型", event.get('action', '-')),
            self._render_info_item("媒体名称", event.get('media', '-')),
            self._render_info_item("所属服务器", event.get('server', '-')),
            self._render_info_item("TMDB ID", event.get('tmdb_id', '-')),
            {'component': 'VDivider', 'props': {'class': 'my-2'}},
            {'component': 'div', 'props': {'class': 'text-caption text-grey-darken-1'}, 'text': "路径地址:"},
            {'component': 'div', 'props': {'class': 'text-caption text-truncate', 'title': event.get('path')}, 'text': event.get('path', '-')}
        ]

    def _set_last_event_snapshot(self, event_info: WebhookEventInfo):
        item_path = event_info.item_path or ''
        if not item_path and event_info.json_object:
            item_path = event_info.json_object.get('Item', {}).get('Path', '')
        event_name = str(event_info.event) if event_info.event is not None else ''
        snapshot = {
            'time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
            'event': self._short_page_text(event_name, 80, '未知事件'),
            'action': self._short_page_text(
                self._webhook_actions.get(event_name) or self._webhook_actions.get(event_name.lower()),
                80,
                '通知'
            ),
            'server': self._short_page_text(self._get_server_name_cn(event_info), 80, '媒体服务器'),
            'media': self._short_page_text(event_info.item_name, 120, '未知媒体'),
            'item_type': self._short_page_text(event_info.item_type, 40, '-'),
            'user': self._short_page_text(event_info.user_name, 80, '-'),
            'device': self._short_page_text(event_info.device_name, 80, '-'),
            'tmdb_id': self._short_page_text(event_info.tmdb_id, 40, '-'),
            'path': self._short_page_text(item_path, 160, '-'),
        }
        with self._lock:
            self._last_event_snapshot = snapshot
            # 新增：记录到历史列表，只保留最近 5 条
            if not hasattr(self, '_event_history'): self._event_history = []
            self._event_history.insert(0, snapshot)
            if len(self._event_history) > 5:
                self._event_history.pop()

    def _set_last_notification_snapshot(self, title: str, text: str, image: Optional[str] = None, link: Optional[str] = None):
        snapshot = {
            'time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
            'title': self._short_page_text(title, 120, '-'),
            'text': self._short_page_text(text, 220, '-'),
            'image': self._short_page_text(image, 120, '无'),
            'link': self._short_page_text(link, 120, '无'),
        }
        with self._lock:
            self._last_notification_snapshot = snapshot

    @eventmanager.register(EventType.WebhookMessage)
    def send(self, event: Event):
        """发送通知消息主入口"""
        try:
            if not self._enabled:
                return

            self._total_events += 1

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
            if self._filter_unrecognized:
                if event_info.item_type not in ["AUD", "MusicAlbum"]:
                    if event_info.item_type in ["MOV", "TV", "SHOW"]:
                        if event_lower in ("library.new", "playback.start", "playback.stop",
                                           "media.play", "media.stop", "playbackstart", "playbackstop",
                                           "playback.pause", "playback.unpause", "media.pause", "media.resume"):
                            # 仅用本地数据判断，不依赖异步 API 结果，避免误丢通知
                            tmdb_id = self._extract_tmdb_id_local(event_info)
                            if not tmdb_id:
                                logger.info(f"TMDB未识别视频，跳过通知: {event_info.item_name}")
                                return

            # 根据事件类型分发处理
            # self._set_last_event_snapshot(event_info)

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

            if event_info.json_object and event_info.json_object.get('Item', {}).get('Type') == 'MusicAlbum' and event_lower == 'library.new':
                self._handle_music_album(event_info, event_info.json_object.get('Item', {}))
                return

            if (self._aggregate_enabled and
                event_lower == "library.new" and
                event_info.item_type in ["TV", "SHOW"]):
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
        texts = [
            f"来自：{server_name}",
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"状态：连接正常"
        ]
        if event_info.user_name:
            texts.append(f"用户：{event_info.user_name}")

        self._send_notification(
            title="🔔 媒体服务器通知测试",
            text="\n".join(texts),
            image=self._webhook_images.get(event_info.channel)
        )

    def _handle_login_event(self, event_info: WebhookEventInfo):
        """处理登录消息"""
        action = "登录成功" if "authenticated" in event_info.event.lower() and "failed" not in event_info.event.lower() else "登录失败"

        texts = [
            f"👤 用户：{event_info.user_name or '未知用户'}",
            f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
        ]

        if event_info.device_name:
            client_str = f"{event_info.client} " if event_info.client else ""
            texts.append(f"📱 设备：{client_str}{event_info.device_name}")
        if event_info.ip:
            try:
                location = WebUtils.get_location(event_info.ip)
                texts.append(f"🌐 IP：{event_info.ip} {location}")
            except Exception:
                texts.append(f"🌐 IP：{event_info.ip}")

        texts.append(f"🖥️ 服务器：{self._get_server_name_cn(event_info)}")

        self._send_notification(
            title=f"🔐 {action}提醒",
            text="\n".join(texts),
            image=self._webhook_images.get(event_info.channel)
        )

    def _handle_rate_event(self, event_info: WebhookEventInfo):
        """处理评分/标记消息"""
        tmdb_id = self._extract_tmdb_id_local(event_info)

        if self._filter_unrecognized and event_info.item_type in ["MOV", "TV", "SHOW"]:
            if not tmdb_id:
                logger.info(f"TMDB未识别视频，跳过评分通知: {event_info.item_name}")
                return

        texts = [
            f"👤 用户：{event_info.user_name or '未知用户'}",
            f"🏷️ 标记：{self._webhook_actions.get(event_info.event) or self._webhook_actions.get(event_info.event.lower(), '已标记')}",
            f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
        ]

        if tmdb_id:
            event_info.tmdb_id = tmdb_id
        self._set_last_event_snapshot(event_info)
        image_url = event_info.image_url
        if not image_url and tmdb_id:
            mtype = MediaType.MOVIE if event_info.item_type == "MOV" else MediaType.TV
            image_url = self._get_tmdb_image(event_info, mtype)

        self._send_notification(
            title=f"⭐ 用户评分：{event_info.item_name}",
            text="\n".join(texts),
            image=image_url or self._webhook_images.get(event_info.channel)
        )

    def _handle_deep_delete_event(self, event_info: WebhookEventInfo):
        """处理神医助手媒体深度删除消息"""
        item_name = event_info.item_name or "未知媒体"
        item_path = event_info.item_path or ""

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
            for path in mount_paths:
                texts.append(f"• {path}")

        self._send_notification(
            title="🗑️ 神医助手 - 媒体深度删除",
            text="\n" + "\n".join(texts),
            image=None
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
            self._set_last_event_snapshot(event_info)

            message_texts = []
            message_title = ""
            image_url = self._get_emby_local_image(event_info) if _path_blocked else event_info.image_url

            # 音频单曲特殊处理
            if event_info.item_type == "AUD":
                self._build_audio_message(event_info, message_texts)
                action_base = (self._webhook_actions.get(event_info.event)
                               or self._webhook_actions.get(event_info.event.lower(), "通知"))
                server_name = self._get_server_name_cn(event_info)
                song_name = (event_info.json_object.get('Item', {}).get('Name')
                             if event_info.json_object else None) or event_info.item_name or '未知媒体'
                message_title = f"{song_name} {action_base} {server_name}"
                img = self._get_audio_image_url(event_info.server_name, event_info.json_object.get('Item', {}) if event_info.json_object else {})
                if img:
                    image_url = img

            # 视频处理 (TV/MOV)
            else:
                tmdb_info = None
                if tmdb_id:
                    mtype = MediaType.MOVIE if event_info.item_type == "MOV" else MediaType.TV
                    try:
                        tmdb_info = self.chain.recognize_media(tmdbid=int(tmdb_id), mtype=mtype)
                    except Exception as e:
                        logger.error(f"识别TMDB媒体异常: {str(e)}")

                # 标题构造
                title_name = self._build_title_name(tmdb_info, event_info)
                message_title = self._build_message_title(event_info.event, title_name)

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
                    mtype = MediaType.TV if event_info.item_type in ["TV", "SHOW"] else MediaType.MOVIE
                    image_url = self._get_tmdb_image(event_info, mtype)

            if not message_title:
                logger.warning(f"消息标题为空，跳过发送: {event_info.event} / {event_info.item_name}")
                return

            # 附加信息
            self._append_extra_info(message_texts, event_info)
            play_link = self._get_play_link(event_info)

            if not image_url:
                image_url = self._webhook_images.get(event_info.channel)

            with self._lock:
                if ev_lower in ("playback.stop", "media.stop", "playbackstop"):
                    self._add_key_cache_locked(expiring_key)
                elif ev_lower in ("playback.start", "media.play", "playbackstart"):
                    self._webhook_msg_keys.pop(expiring_key, None)

            # 发送
            message_text = "\n".join(message_texts)
            if "library.new" in ev_lower:
                message_text = "\n" + message_text

            self._send_notification(
                title=message_title,
                text=message_text,
                image=image_url,
                link=play_link
            )

        except Exception as e:
            logger.error(f"处理媒体事件异常: {str(e)}\n{traceback.format_exc()}")

    # ==================== 公共辅助方法 ====================

    def _send_notification(self, title: str, text: str, image: Optional[str] = None, link: Optional[str] = None):
        self._set_last_notification_snapshot(title=title, text=text, image=image, link=link)
        self.post_message(
            mtype=NotificationType.MediaServer,
            title=title,
            text=text,
            image=image,
            link=link
        )

    def _build_title_name(self, tmdb_info, event_info: WebhookEventInfo) -> str:
        """构建带年份的标题名称"""
        title_name = (tmdb_info.title if (tmdb_info and tmdb_info.title) else event_info.item_name) or "未知媒体"
        year = tmdb_info.year if (tmdb_info and tmdb_info.year) else (
            event_info.json_object.get('Item', {}).get('ProductionYear') if event_info.json_object else None
        )
        if year and str(year) not in title_name:
            title_name += f" ({year})"
        return title_name

    def _build_message_title(self, event: str, title_name: str) -> str:
        """根据事件类型构建消息标题"""
        ev = event.lower()
        if "library.new" in ev:
            return f"🆕 {title_name} 已入库"
        elif "playback.start" in ev or "media.play" in ev or "playbackstart" in ev:
            return f"▶️ 开始播放：{title_name}"
        elif "playback.stop" in ev or "media.stop" in ev or "playbackstop" in ev:
            return f"⏹️ 停止播放：{title_name}"
        elif "pause" in ev:
            return f"⏸️ 暂停播放：{title_name}"
        elif "resume" in ev or "unpause" in ev:
            return f"▶️ 继续播放：{title_name}"
        else:
            action_base = self._webhook_actions.get(event) or self._webhook_actions.get(ev, "通知")
            return f"📢 {action_base}：{title_name}"

    def _get_category(self, tmdb_info, event_info: WebhookEventInfo) -> Optional[str]:
        """获取分类（优先智能分类，fallback路径解析）"""
        category = None
        if self._smart_category_enabled and tmdb_info and self.category:
            try:
                if event_info.item_type == "MOV":
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
            return item.get("SeriesId") or item.get("SeriesName")
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
        self._set_last_event_snapshot(first_info)

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

        message_title = f"🆕 {title_name} 已入库 (含{count}个文件)"

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
            image_url = self._webhook_images.get(first_info.channel)

        play_link = self._get_play_link(first_info)

        self._send_notification(
            title=message_title,
            text="\n" + "\n".join(message_texts),
            image=image_url,
            link=play_link
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

    def _cache_image_result(self, key: str, image_url: Optional[str]):
        expires_at = 0 if image_url else time.time() + self.IMAGE_NEGATIVE_CACHE_TTL
        with self._lock:
            self._image_cache[key] = (image_url, expires_at)
            if len(self._image_cache) > self.IMAGE_CACHE_MAX_SIZE:
                self._image_cache.popitem(last=False)

    def _get_tmdb_image(self, event_info: WebhookEventInfo, mtype: MediaType) -> Optional[str]:
        """获取TMDB图片（带OrderedDict LRU缓存）"""
        key = f"{event_info.tmdb_id}_{event_info.season_id}_{event_info.episode_id}"

        with self._lock:
            cached = self._image_cache.get(key)
            if cached:
                self._image_cache.move_to_end(key)
                image_url, expires_at = cached
                if image_url or expires_at > time.time():
                    return image_url
                self._image_cache.pop(key, None)

        try:
            img = self.chain.obtain_specific_image(
                mediaid=event_info.tmdb_id, mtype=mtype,
                image_type=MediaImageType.Backdrop,
                season=event_info.season_id, episode=event_info.episode_id
            )

            if not img:
                img = self.chain.obtain_specific_image(
                    mediaid=event_info.tmdb_id, mtype=mtype,
                    image_type=MediaImageType.Poster,
                    season=event_info.season_id, episode=event_info.episode_id
                )

            self._cache_image_result(key, img)
            return img

        except Exception as e:
            self._cache_image_result(key, None)
            logger.error(f"获取TMDB图片异常: {str(e)}")

        return None

    def _get_category_from_path(self, path: str, item_type: str, is_folder: bool = False) -> str:
        """从路径获取分类"""
        if not path:
            return ""
        try:
            path = os.path.normpath(path)

            if is_folder and item_type in ["TV", "SHOW"]:
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

            self._send_notification(
                title=f"🎵 新入库媒体：{song_name}",
                text="\n" + "\n".join(texts),
                image=image_url,
                link=link
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
                self._image_cache.clear()
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
