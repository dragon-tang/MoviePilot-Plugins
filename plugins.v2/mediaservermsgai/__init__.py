import re
import time
import traceback
import threading
import os
import urllib.parse
from typing import Any, List, Dict, Tuple, Optional

from app.core.cache import cached
from app.core.event import eventmanager, Event
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.modules.themoviedb import CategoryHelper
from app.plugins import _PluginBase
from app.schemas import WebhookEventInfo, ServiceInfo, MediaServerItem
from app.schemas.types import EventType, MediaType, MediaImageType, NotificationType
from app.utils.web import WebUtils


class mediaservermsgai(_PluginBase):
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
    8. 支持路径关键词黑名单，命中路径跳过TMDB识别（适用于成人内容等不需刮削的媒体库）
    9. 拦截路径的媒体自动使用Emby本地图片（Backdrop/Primary），避免错误刮削带来的错误图片
    """

    # ==================== 常量定义 ====================
    DEFAULT_EXPIRATION_TIME = 600              # 默认过期时间（秒）
    DEFAULT_AGGREGATE_TIME = 15                # 默认聚合时间（秒）
    DEFAULT_OVERVIEW_MAX_LENGTH = 150          # 默认简介最大长度
    IMAGE_CACHE_MAX_SIZE = 100                 # 图片缓存最大数量

    # ==================== 插件基本信息 ====================
    plugin_name = "媒体库服务器通知AI版"
    plugin_desc = "基于Emby识别结果+TMDB元数据+微信清爽版(全消息类型+剧集聚合+未识别过滤)"
    plugin_icon = "mediaplay.png"
    plugin_version = "2.0.0"
    plugin_author = "jxxghp,dragon-tang"
    author_url = "https://github.com/dragon-tang"
    plugin_config_prefix = "mediaservermsgai_"
    plugin_order = 14
    auth_level = 1

    # ==================== 插件运行时状态配置 ====================
    _enabled = False                           # 插件是否启用
    _add_play_link = False                     # 是否添加播放链接
    _mediaservers = None                       # 媒体服务器列表
    _types = []                                # 启用的消息类型
    _webhook_msg_keys = {}                     # Webhook消息去重缓存
    _lock = threading.Lock()                   # 线程锁
    _last_event_cache: Tuple[Optional[Event], float] = (None, 0.0)  # 事件去重缓存
    _image_cache = {}                          # 图片URL缓存
    _overview_max_length = DEFAULT_OVERVIEW_MAX_LENGTH  # 简介最大长度
    _filter_unrecognized = True                # TMDB未识别视频不发送通知
    _path_skip_keywords = []                   # 路径关键词黑名单（命中则跳过TMDB识别）
    _emby_image_host = ""                      # 自定义Emby图片Host（用于拦截路径的本地图片）

    # ==================== TV剧集消息聚合配置 ====================
    _aggregate_enabled = False                 # 是否启用TV剧集聚合功能
    _aggregate_time = DEFAULT_AGGREGATE_TIME   # 聚合时间窗口（秒）
    _pending_messages = {}                     # 待聚合的消息 {series_key: [(event_info, event), ...]}
    _aggregate_timers = {}                     # 聚合定时器 {series_key: timer}
    _smart_category_enabled = True             # 是否启用智能分类（CategoryHelper）

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
        "strm.deepdelete": "深度删除"
    }
    
    # ==================== 媒体服务器默认图标（优化后的官方高清图标）====================
    _webhook_images = {
        "emby": "https://raw.githubusercontent.com/qqcomeup/MoviePilot-Plugins/bb3ca257f74cf000640f9ebadab257bb0850baac/icons/11-11.jpg",
        "plex": "https://raw.githubusercontent.com/qqcomeup/MoviePilot-Plugins/bb3ca257f74cf000640f9ebadab257bb0850baac/icons/11-11.jpg",
        "jellyfin": "https://raw.githubusercontent.com/qqcomeup/MoviePilot-Plugins/bb3ca257f74cf000640f9ebadab257bb0850baac/icons/11-11.jpg"
    }

    # ==================== 国家/地区中文映射 ====================
    _country_cn_map = {
        'CN': '中国大陆', 'US': '美国', 'JP': '日本', 'KR': '韩国',
        'HK': '中国香港', 'TW': '中国台湾', 'GB': '英国', 'FR': '法国',
        'DE': '德国', 'IT': '意大利', 'ES': '西班牙', 'IN': '印度',
        'TH': '泰国', 'RU': '俄罗斯', 'CA': '加拿大', 'AU': '澳大利亚',
        'SG': '新加坡', 'MY': '马来西亚', 'VN': '越南', 'PH': '菲律宾',
        'ID': '印度尼西亚', 'BR': '巴西', 'MX': '墨西哥', 'AR': '阿根廷',
        'NL': '荷兰', 'BE': '比利时', 'SE': '瑞典', 'DK': '丹麦',
        'NO': '挪威', 'FI': '芬兰', 'PL': '波兰', 'TR': '土耳其'
    }

    def __init__(self):
        """
        初始化插件实例
        """
        super().__init__()
        self.category = CategoryHelper()
        logger.info("媒体服务器消息插件AI版初始化完成")
        logger.debug(f"插件版本: {self.plugin_version}, 插件名称: {self.plugin_name}")

    def init_plugin(self, config: dict = None):
        """
        初始化插件配置

        Args:
            config (dict, optional): 插件配置参数
        """
        if config:
            self._enabled = config.get("enabled")
            self._types = config.get("types") or []
            self._mediaservers = config.get("mediaservers") or []
            self._add_play_link = config.get("add_play_link", False)
            self._overview_max_length = int(config.get("overview_max_length", self.DEFAULT_OVERVIEW_MAX_LENGTH))
            self._aggregate_enabled = config.get("aggregate_enabled", False)
            self._aggregate_time = int(config.get("aggregate_time", self.DEFAULT_AGGREGATE_TIME))
            self._smart_category_enabled = config.get("smart_category_enabled", True)
            self._filter_unrecognized = config.get("filter_unrecognized", True)
            path_skip_keywords_raw = config.get("path_skip_keywords", "")
            self._path_skip_keywords = [
                kw.strip() for kw in path_skip_keywords_raw.splitlines() if kw.strip()
            ]
            self._emby_image_host = config.get("emby_image_host", "").rstrip("/")
            
            logger.info("插件配置初始化完成:")
            logger.info(f"  - 启用状态: {self._enabled}")
            logger.info(f"  - 消息类型: {self._types}")
            logger.info(f"  - 媒体服务器: {self._mediaservers}")
            logger.info(f"  - 添加播放链接: {self._add_play_link}")
            logger.info(f"  - 聚合功能: {self._aggregate_enabled} (等待时间: {self._aggregate_time}秒)")
            logger.info(f"  - 智能分类: {self._smart_category_enabled}")
            logger.info(f"  - TMDB未识别过滤: {self._filter_unrecognized}")
            logger.info(f"  - 简介最大长度: {self._overview_max_length}")

    def service_infos(self, type_filter: Optional[str] = None) -> Optional[Dict[str, ServiceInfo]]:
        """
        获取媒体服务器信息服务信息

        Args:
            type_filter (str, optional): 媒体服务器类型过滤器

        Returns:
            Dict[str, ServiceInfo]: 活跃的媒体服务器服务信息字典
        """
        if not self._mediaservers:
            logger.debug("尚未配置媒体服务器")
            return None
        
        logger.debug(f"正在获取媒体服务器信息，过滤器: {type_filter}")
        services = MediaServerHelper().get_services(type_filter=type_filter, name_filters=self._mediaservers)
        
        if not services:
            logger.warning("获取媒体服务器实例失败")
            return None
        
        active_services = {}
        inactive_count = 0
        
        for service_name, service_info in services.items():
            if service_info.instance.is_inactive():
                logger.warning(f"媒体服务器 {service_name} 未连接")
                inactive_count += 1
            else:
                active_services[service_name] = service_info
                logger.debug(f"媒体服务器 {service_name} 连接正常")
        
        logger.info(f"媒体服务器状态统计: 活跃 {len(active_services)}个, 未连接 {inactive_count}个")
        return active_services if active_services else None

    def service_info(self, name: str) -> Optional[ServiceInfo]:
        """
        根据名称获取特定媒体服务器服务信息

        Args:
            name (str): 媒体服务器名称

        Returns:
            ServiceInfo: 媒体服务器服务信息
        """
        logger.debug(f"查找媒体服务器: {name}")
        services = self.service_infos()
        if not services:
            logger.warning(f"没有找到任何媒体服务器")
            return None
        
        service = services.get(name)
        if service:
            logger.debug(f"找到媒体服务器: {name}")
        else:
            logger.warning(f"未找到媒体服务器: {name}")
        
        return service

    def get_state(self) -> bool:
        """
        获取插件状态

        Returns:
            bool: 插件是否启用
        """
        logger.debug(f"插件状态查询: {self._enabled}")
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        获取插件命令（当前未实现）

        Returns:
            List[Dict[str, Any]]: 空列表
        """
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API（当前未实现）

        Returns:
            List[Dict[str, Any]]: 空列表
        """
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        
        Returns:
            Tuple[List[dict], Dict[str, Any]]: 页面配置和默认数据
        """
        types_options = [
            {"title": "新入库", "value": "library.new"},
            {"title": "开始播放", "value": "playback.start|media.play|PlaybackStart"},
            {"title": "停止播放", "value": "playback.stop|media.stop|PlaybackStop"},
            {"title": "暂停/继续", "value": "playback.pause|playback.unpause|media.pause|media.resume"},
            {"title": "用户标记", "value": "item.rate|item.markplayed|item.markunplayed"},
            {"title": "登录提醒", "value": "user.authenticated|user.authenticationfailed"},
            {"title": "系统测试", "value": "system.webhooktest|system.notificationtest"},
            {"title": "媒体深度删除", "value": "strm.deepdelete"},
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
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VSelect', 'props': {'multiple': True, 'chips': True, 'clearable': True, 'model': 'mediaservers', 'label': '媒体服务器', 'items': [{"title": config.name, "value": config.name} for config in MediaServerHelper().get_configs().values()]}}]}
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
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextField', 'props': {'model': 'emby_image_host', 'label': '自定义Emby图片Host', 'placeholder': '例如：http://1.1.1.1:8099', 'hint': '拦截路径的媒体图片将使用此Host构造URL，留空则使用插件内配置的Emby地址'}}]}
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "types": [],
            "aggregate_enabled": False,
            "aggregate_time": self.DEFAULT_AGGREGATE_TIME,
            "smart_category_enabled": True,
            "filter_unrecognized": True,
            "path_skip_keywords": "",
            "emby_image_host": ""
        }
    
    def get_page(self) -> List[dict]:
        """
        获取插件页面（当前未实现）

        Returns:
            List[dict]: 空列表
        """
        pass

    @eventmanager.register(EventType.WebhookMessage)
    def send(self, event: Event):
        """
        发送通知消息主入口函数
        处理来自媒体服务器的Webhook事件，并根据配置决定是否发送通知消息

        处理流程：
        1. 检查插件是否启用
        2. 验证事件数据有效性
        3. 检查事件类型是否在支持范围内
        4. 检查事件类型是否在用户配置的允许范围内
        5. 验证媒体服务器配置
        6. 根据事件类型分发到对应处理函数

        Args:
            event (Event): Webhook事件对象
        """
        try:
            logger.info("=" * 60)
            logger.info("收到新的Webhook事件")
            logger.info(f"事件ID: {event.event_id}")
            logger.info(f"接收时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            
            # 1. 检查插件是否启用
            if not self._enabled:
                logger.info("插件未启用，跳过处理")
                return
            
            event_info: WebhookEventInfo = event.event_data
            if not event_info:
                logger.warning("事件数据为空，跳过处理")
                return
            
            # 记录事件基本信息
            logger.info(f"事件类型: {event_info.event}")
            logger.info(f"媒体名称: {event_info.item_name}")
            logger.info(f"媒体类型: {event_info.item_type}")
            logger.info(f"服务器: {event_info.server_name}")
            logger.info(f"用户: {event_info.user_name}")
            
            # 2. 兼容性处理：如果没有映射的动作，尝试使用原始事件名
            if not self._webhook_actions.get(event_info.event):
                logger.warning(f"未知的Webhook事件类型: {event_info.event}")
                return

            # 3. 类型过滤 - 将配置的类型预处理为一个扁平集合，提高查找效率
            allowed_types = set()
            for _type in self._types:
                allowed_types.update(_type.split("|"))
            
            if event_info.event not in allowed_types:
                logger.info(f"未开启 {event_info.event} 类型的消息通知")
                return

            # 4. 验证媒体服务器配置
            if event_info.server_name:
                logger.info(f"验证媒体服务器: {event_info.server_name}")
                if not self.service_info(name=event_info.server_name):
                    logger.info(f"未开启媒体服务器 {event_info.server_name} 的消息通知")
                    return
            
            event_type = str(event_info.event).lower()

            # 5. TMDB未识别视频过滤检查
            if self._filter_unrecognized:
                logger.info("正在检查TMDB识别状态...")
                # 跳过音乐类型的过滤
                if event_info.item_type not in ["AUD", "MusicAlbum"]:
                    # 检查是否为视频类型
                    if event_info.item_type in ["MOV", "TV", "SHOW"]:
                        # 只对用户关心的消息类型进行过滤
                        if event_type in ["library.new", "playback.start", "playback.stop", 
                                         "media.play", "media.stop", "PlaybackStart", "PlaybackStop",
                                         "playback.pause", "playback.unpause", "media.pause", "media.resume"]:
                            tmdb_id = self._extract_tmdb_id(event_info)
                            if not tmdb_id:
                                logger.info(f"TMDB未识别视频，跳过通知: {event_info.item_name} ({event_info.event})")
                                return
                            else:
                                logger.info(f"TMDB识别成功: {event_info.item_name}, TMDB ID: {tmdb_id}")

            # 6. 根据事件类型分发处理
            logger.info(f"开始处理事件类型: {event_type}")
            
            # === 系统测试消息 ===
            if "test" in event_type:
                logger.info("处理系统测试消息")
                self._handle_test_event(event_info)
                return

            # === 用户登录消息 ===
            if "user.authentic" in event_type:
                logger.info("处理用户登录消息")
                self._handle_login_event(event_info)
                return

            # === 评分/标记消息 ===
            if "item." in event_type and ("rate" in event_type or "mark" in event_type):
                logger.info("处理评分/标记消息")
                self._handle_rate_event(event_info)
                return

            # === 媒体深度删除消息 ===
            if "strm.deepdelete" in event_type:
                logger.info("处理媒体深度删除消息")
                self._handle_deep_delete_event(event_info)
                return

            # === 音乐专辑处理 ===
            if event_info.json_object and event_info.json_object.get('Item', {}).get('Type') == 'MusicAlbum' and event_type == 'library.new':
                logger.info("处理音乐专辑消息")
                self._handle_music_album(event_info, event_info.json_object.get('Item', {}))
                return

            # === 剧集聚合处理 ===
            if (self._aggregate_enabled and 
                event_type == "library.new" and 
                event_info.item_type in ["TV", "SHOW"]):
                
                series_id = self._get_series_id(event_info)
                if series_id:
                    logger.info(f"TV剧集聚合处理，series_id={series_id}")
                    self._aggregate_tv_episodes(series_id, event_info, event)
                    return

            # === 常规媒体消息 ===
            logger.info("处理常规媒体消息")
            self._process_media_event(event, event_info)

        except Exception as e:
            logger.error(f"Webhook分发异常: {str(e)}")
            logger.error("异常堆栈:")
            logger.error(traceback.format_exc())
        finally:
            logger.info("事件处理完成")
            logger.info("=" * 60)

    def _handle_test_event(self, event_info: WebhookEventInfo):
        """
        处理测试消息

        Args:
            event_info (WebhookEventInfo): Webhook事件信息
        """
        logger.info("发送测试消息通知")
        title = f"🔔 媒体服务器通知测试"
        server_name = self._get_server_name_cn(event_info)
        texts = [
            f"来自：{server_name}",
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"状态：连接正常"
        ]
        if event_info.user_name:
            texts.append(f"用户：{event_info.user_name}")
        
        logger.debug(f"发送测试消息: {title}")
        self.post_message(
            mtype=NotificationType.MediaServer,
            title=title,
            text="\n".join(texts),
            image=self._webhook_images.get(event_info.channel)
        )

    def _handle_login_event(self, event_info: WebhookEventInfo):
        """
        处理登录消息

        Args:
            event_info (WebhookEventInfo): Webhook事件信息
        """
        logger.info("处理登录事件通知")
        action = "登录成功" if "authenticated" in event_info.event and "failed" not in event_info.event else "登录失败"
        title = f"🔐 {action}提醒"
        
        texts = []
        texts.append(f"👤 用户：{event_info.user_name}")
        texts.append(f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        if event_info.device_name:
            texts.append(f"📱 设备：{event_info.client} {event_info.device_name}")
        if event_info.ip:
            try:
                location = WebUtils.get_location(event_info.ip)
                texts.append(f"🌐 IP：{event_info.ip} {location}")
            except Exception as e:
                logger.debug(f"获取IP位置信息时出错: {str(e)}")
                texts.append(f"🌐 IP：{event_info.ip}")
            
        server_name = self._get_server_name_cn(event_info)
        texts.append(f"🖥️ 服务器：{server_name}")

        logger.debug(f"发送登录消息: {title}")
        self.post_message(
            mtype=NotificationType.MediaServer,
            title=title,
            text="\n".join(texts),
            image=self._webhook_images.get(event_info.channel)
        )

    def _handle_rate_event(self, event_info: WebhookEventInfo):
        """
        处理评分/标记消息

        Args:
            event_info (WebhookEventInfo): Webhook事件信息
        """
        logger.info("处理评分/标记事件通知")

        # 评分/标记事件也需要检查TMDB识别
        if self._filter_unrecognized and event_info.item_type in ["MOV", "TV", "SHOW"]:
            tmdb_id = self._extract_tmdb_id(event_info)
            if not tmdb_id:
                logger.info(f"TMDB未识别视频，跳过评分通知: {event_info.item_name}")
                return

        item_name = event_info.item_name

        title = f"⭐ 用户评分：{item_name}"
        texts = []
        texts.append(f"👤 用户：{event_info.user_name}")
        texts.append(f"🏷️ 标记：{self._webhook_actions.get(event_info.event, '已标记')}")
        texts.append(f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")

        # 尝试获取图片
        tmdb_id = self._extract_tmdb_id(event_info)
        image_url = event_info.image_url
        if not image_url and tmdb_id:
            logger.debug(f"尝试获取TMDB图片: {tmdb_id}")
            mtype = MediaType.MOVIE if event_info.item_type == "MOV" else MediaType.TV
            image_url = self._get_tmdb_image(event_info, mtype)
            if image_url:
                logger.debug(f"成功获取TMDB图片: {image_url[:50]}...")

        logger.debug(f"发送评分消息: {title}")
        self.post_message(
            mtype=NotificationType.MediaServer,
            title=title,
            text="\n".join(texts),
            image=image_url or self._webhook_images.get(event_info.channel)
        )

    def _handle_deep_delete_event(self, event_info: WebhookEventInfo):
        """
        处理神医助手媒体深度删除消息

        Args:
            event_info (WebhookEventInfo): Webhook事件信息
        """
        logger.info("处理神医助手媒体深度删除事件通知")

        item_name = event_info.item_name or "未知媒体"
        item_path = event_info.item_path or ""

        # 从json_object中提取挂载路径信息
        mount_paths = []
        if event_info.json_object and isinstance(event_info.json_object, dict):
            # 尝试从不同可能的字段获取挂载路径
            mount_paths_raw = (
                event_info.json_object.get('MountPaths') or
                event_info.json_object.get('mount_paths') or
                event_info.json_object.get('Description', '').split('\n')
            )

            if isinstance(mount_paths_raw, list):
                mount_paths = [p.strip() for p in mount_paths_raw if p and p.strip()]
            elif isinstance(mount_paths_raw, str):
                # 如果是字符串，按行分割
                mount_paths = [p.strip() for p in mount_paths_raw.split('\n') if p.strip() and not p.strip().startswith('Item')]

        title = f"🗑️ 神医助手 - 媒体深度删除"
        texts = []
        texts.append(f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
        texts.append(f"📝 媒体名称：\n{item_name}")

        if item_path:
            texts.append(f"📂 本地路径：\n{item_path}")

        if mount_paths:
            mount_paths_text = "\n".join([f"  • {path}" for path in mount_paths])
            texts.append(f"💾 挂载路径：\n{mount_paths_text}")

        # 尝试提取TMDB ID并获取图片
        tmdb_id = self._extract_tmdb_id(event_info)
        image_url = event_info.image_url

        if not image_url and tmdb_id:
            logger.debug(f"尝试获取TMDB图片: {tmdb_id}")
            mtype = MediaType.MOVIE if event_info.item_type == "MOV" else MediaType.TV
            image_url = self._get_tmdb_image(event_info, mtype)
            if image_url:
                logger.debug(f"成功获取TMDB图片: {image_url[:50]}...")

        logger.debug(f"发送深度删除消息: {title}")
        self.post_message(
            mtype=NotificationType.MediaServer,
            title=title,
            text="\n".join(texts),
            image=image_url or self._webhook_images.get(event_info.channel)
        )

    def _process_media_event(self, event: Event, event_info: WebhookEventInfo):
        """处理常规媒体消息（入库/播放）"""
        try:
            logger.info("开始处理媒体事件")
            logger.debug(f"事件详情: {event_info.event}, 媒体: {event_info.item_name}")
            
            # 0. 清理过期缓存
            logger.debug("清理过期缓存")
            self._clean_expired_cache()
            
            # 1. 防重复与防抖
            expiring_key = f"{event_info.item_id}-{event_info.client}-{event_info.user_name}-{event_info.event}"
            logger.debug(f"事件去重键: {expiring_key}")
            
            if str(event_info.event) == "playback.stop" and expiring_key in self._webhook_msg_keys:
                logger.info("重复的停止播放事件，跳过处理")
                self._add_key_cache(expiring_key)
                return
            
            with self._lock:
                current_time = time.time()
                last_event, last_time = self._last_event_cache
                if last_event and (current_time - last_time < 2):
                    if last_event.event_id == event.event_id or last_event.event_data == event_info: 
                        logger.info("事件去重检查: 相同事件在2秒内重复，跳过处理")
                        return
                self._last_event_cache = (event, current_time)
                logger.debug("事件去重检查通过")

            # 2. 元数据识别
            logger.info("开始元数据识别")
            # 检查路径是否命中黑名单（用于后续跳过TMDB图片）
            _raw_path = event_info.item_path or ""
            if not _raw_path and event_info.json_object:
                _raw_path = event_info.json_object.get('Item', {}).get('Path', '')
            _path_blocked = any(kw in _raw_path for kw in self._path_skip_keywords) if (self._path_skip_keywords and _raw_path) else False

            tmdb_id = self._extract_tmdb_id(event_info)
            event_info.tmdb_id = tmdb_id
            logger.debug(f"TMDB ID: {tmdb_id}")

            message_texts = []
            message_title = ""
            # 路径被拦截时不使用 event_info.image_url（可能来自错误刮削），改从 Emby 本地构造
            if _path_blocked:
                image_url = self._get_emby_local_image(event_info)
                logger.info(f"路径已拦截，使用Emby本地图片: {image_url}")
            else:
                image_url = event_info.image_url
            
            # 3. 音频单曲特殊处理
            if event_info.item_type == "AUD":
                logger.info("处理音频文件")
                self._build_audio_message(event_info, message_texts)
                # 标题构造
                action_base = self._webhook_actions.get(event_info.event, "通知")
                server_name = self._get_server_name_cn(event_info)
                song_name = event_info.item_name
                if event_info.json_object:
                    song_name = event_info.json_object.get('Item', {}).get('Name') or song_name
                message_title = f"{song_name} {action_base} {server_name}"
                # 图片
                img = self._get_audio_image_url(event_info.server_name, event_info.json_object.get('Item', {}))
                if img: 
                    image_url = img
                    logger.debug(f"获取到音频图片: {img[:50]}...")

            # 4. 视频处理 (TV/MOV)
            else:
                logger.info(f"处理视频文件，类型: {event_info.item_type}")
                tmdb_info = None
                if tmdb_id:
                    mtype = MediaType.MOVIE if event_info.item_type == "MOV" else MediaType.TV
                    logger.debug(f"尝试识别TMDB媒体: {tmdb_id}, 类型: {mtype}")
                    try:
                        tmdb_info = self.chain.recognize_media(tmdbid=int(tmdb_id), mtype=mtype)
                        if tmdb_info:
                            logger.info(f"TMDB信息识别成功: {tmdb_info.title if hasattr(tmdb_info, 'title') else 'Unknown'}")
                        else:
                            logger.warning(f"无法识别TMDB媒体: {tmdb_id}")
                    except Exception as e: 
                        logger.error(f"识别TMDB媒体异常: {str(e)}")

                # 标题构造
                title_name = tmdb_info.title if (tmdb_info and tmdb_info.title) else event_info.item_name
                logger.debug(f"原始标题: {title_name}")
                
                year = tmdb_info.year if (tmdb_info and tmdb_info.year) else event_info.json_object.get('Item', {}).get('ProductionYear')
                if year and str(year) not in title_name:
                    title_name += f" ({year})"
                    logger.debug(f"添加年份信息: {year}")
                
                action_base = self._webhook_actions.get(event_info.event, "通知")
                logger.debug(f"事件动作: {action_base}")

                # 根据事件类型设置不同的标题前缀
                if "library.new" in event_info.event:
                    message_title = f"🆕 {title_name} 已入库"
                elif "playback.start" in event_info.event or "media.play" in event_info.event or "PlaybackStart" in event_info.event:
                    message_title = f"▶️ 开始播放：{title_name}"
                elif "playback.stop" in event_info.event or "media.stop" in event_info.event or "PlaybackStop" in event_info.event:
                    message_title = f"⏹️ 停止播放：{title_name}"
                elif "pause" in event_info.event:
                    message_title = f"⏸️ 暂停播放：{title_name}"
                elif "resume" in event_info.event or "unpause" in event_info.event:
                    message_title = f"▶️ 继续播放：{title_name}"
                else:
                    message_title = f"📢 {action_base}：{title_name}"
                
                logger.debug(f"消息标题: {message_title}")

                # 内容构造
                message_texts.append(f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}")
                
                # 智能分类（优先使用CategoryHelper，fallback到路径解析）
                category = None
                if self._smart_category_enabled and tmdb_info:
                    logger.debug("尝试智能分类")
                    try:
                        if event_info.item_type == "MOV":
                            category = self.category.get_movie_category(tmdb_info)
                        else:
                            category = self.category.get_tv_category(tmdb_info)
                        if category:
                            logger.debug(f"智能分类成功: {category}")
                    except Exception as e:
                        logger.debug(f"获取TMDB分类时出错: {str(e)}")
                
                if not category:
                    logger.debug("使用路径解析分类")
                    is_folder = event_info.json_object.get('Item', {}).get('IsFolder', False) if event_info.json_object else False
                    category = self._get_category_from_path(event_info.item_path, event_info.item_type, is_folder)
                    if category:
                        logger.debug(f"路径解析分类: {category}")
                
                if category:
                    message_texts.append(f"📂 分类：{category}")

                self._append_season_episode_info(message_texts, event_info, title_name)
                self._append_meta_info(message_texts, tmdb_info)
                self._append_genres_actors(message_texts, tmdb_info)

                # 简介 (播放事件可能不需要太长的简介，可选优化)
                overview = ""
                if tmdb_info and tmdb_info.overview: 
                    overview = tmdb_info.overview
                    logger.debug(f"获取到TMDB简介，长度: {len(overview)}")
                elif event_info.overview: 
                    overview = event_info.overview
                    logger.debug(f"获取到事件简介，长度: {len(overview)}")
                
                if overview and "library.new" in event_info.event:  # 仅入库事件显示简介
                    if len(overview) > self._overview_max_length:
                        overview = overview[:self._overview_max_length].rstrip() + "..."
                        logger.debug(f"简介截断为: {self._overview_max_length}字符")
                    message_texts.append(f"📖 简介：\n{overview}")
                elif overview:
                    logger.debug("播放事件，不显示简介")

                # 图片
                if not image_url and not _path_blocked:
                    logger.debug("尝试获取TMDB图片")
                    if event_info.item_type in ["TV", "SHOW"] and tmdb_id:
                        image_url = self._get_tmdb_image(event_info, MediaType.TV)
                    elif event_info.item_type == "MOV" and tmdb_id:
                        image_url = self._get_tmdb_image(event_info, MediaType.MOVIE)

                    if image_url:
                        logger.debug(f"获取到TMDB图片: {image_url[:50]}...")
                    else:
                        logger.debug("无法获取TMDB图片")

            # 5. 附加信息（用户、进度等）
            logger.debug("添加附加信息")
            self._append_extra_info(message_texts, event_info)
            
            # 6. 播放链接
            play_link = self._get_play_link(event_info)
            if play_link:
                logger.debug(f"生成播放链接: {play_link[:50]}...")
            
            # 7. 兜底图片
            if not image_url:
                image_url = self._webhook_images.get(event_info.channel)
                logger.debug(f"使用默认图片: {event_info.channel}")

            # 8. 缓存管理（用于过滤重复停止事件）
            if str(event_info.event) == "playback.stop":
                logger.debug("缓存停止播放事件")
                self._add_key_cache(expiring_key)
            if str(event_info.event) == "playback.start":
                logger.debug("清理开始播放事件缓存")
                self._remove_key_cache(expiring_key)

            # 9. 发送
            logger.info("准备发送消息通知")
            logger.debug(f"消息标题: {message_title}")
            logger.debug(f"消息内容行数: {len(message_texts)}")
            logger.debug(f"消息图片: {'已设置' if image_url else '未设置'}")
            logger.debug(f"播放链接: {'已设置' if play_link else '未设置'}")

            # 入库消息在标题和内容之间添加空行
            message_text = "\n".join(message_texts)
            if "library.new" in event_info.event:
                message_text = "\n" + message_text

            self.post_message(
                mtype=NotificationType.MediaServer,
                title=message_title,
                text=message_text,
                image=image_url,
                link=play_link
            )
            
            logger.info("消息发送完成")
            
        except Exception as e:
            logger.error(f"处理媒体事件异常: {str(e)}")
            logger.error("异常堆栈:")
            logger.error(traceback.format_exc())

    # === 辅助构建函数 ===
    def _build_audio_message(self, event_info, texts):
        """构建音频消息内容"""
        logger.debug("构建音频消息内容")
        item_data = event_info.json_object.get('Item', {})
        artist = (item_data.get('Artists') or ['未知歌手'])[0]
        album = item_data.get('Album', '')
        duration = self._format_ticks(item_data.get('RunTimeTicks', 0))
        container = item_data.get('Container', '').upper()
        size = self._format_size(item_data.get('Size', 0))

        texts.append(f"⏰ 时间：{time.strftime('%H:%M:%S', time.localtime())}")
        texts.append(f"👤 歌手：{artist}")
        if album: 
            texts.append(f"💿 专辑：{album}")
            logger.debug(f"专辑信息: {album}")
        texts.append(f"⏱️ 时长：{duration}")
        texts.append(f"📦 格式：{container} · {size}")
        logger.debug(f"音频信息: 歌手={artist}, 时长={duration}, 格式={container}")

    def _get_series_id(self, event_info: WebhookEventInfo) -> Optional[str]:
        """获取剧集系列ID"""
        logger.debug("获取剧集系列ID")
        if event_info.json_object and isinstance(event_info.json_object, dict):
            item = event_info.json_object.get("Item", {})
            series_id = item.get("SeriesId") or item.get("SeriesName")
            logger.debug(f"剧集系列ID: {series_id}")
            return series_id
        series_id = getattr(event_info, "series_id", None)
        logger.debug(f"备用剧集系列ID: {series_id}")
        return series_id

    # === 剧集聚合逻辑 ===
    def _aggregate_tv_episodes(self, series_id: str, event_info: WebhookEventInfo, event: Event):
        """聚合TV剧集消息"""
        logger.info(f"开始聚合TV剧集消息，series_id={series_id}")
        with self._lock:
            if series_id not in self._pending_messages:
                self._pending_messages[series_id] = []
                logger.debug(f"创建新的聚合队列: {series_id}")
            
            self._pending_messages[series_id].append((event_info, event))
            logger.debug(f"添加到聚合队列，当前队列长度: {len(self._pending_messages[series_id])}")
            
            if series_id in self._aggregate_timers:
                logger.debug(f"取消现有定时器: {series_id}")
                self._aggregate_timers[series_id].cancel()
            
            logger.info(f"设置聚合定时器，等待 {self._aggregate_time} 秒")
            timer = threading.Timer(self._aggregate_time, self._send_aggregated_message, [series_id])
            self._aggregate_timers[series_id] = timer
            timer.start()
            logger.debug(f"定时器已启动: {series_id}")

    def _send_aggregated_message(self, series_id: str):
        """发送聚合的剧集消息"""
        logger.info(f"发送聚合消息，series_id={series_id}")
        with self._lock:
            if series_id not in self._pending_messages or not self._pending_messages[series_id]:
                logger.debug(f"聚合队列为空，series_id={series_id}")
                if series_id in self._aggregate_timers: 
                    del self._aggregate_timers[series_id]
                return
            
            logger.debug(f"获取聚合消息，数量: {len(self._pending_messages[series_id])}")
            msg_list = self._pending_messages.pop(series_id)
            if series_id in self._aggregate_timers: 
                del self._aggregate_timers[series_id]
            logger.debug(f"清理定时器和队列: {series_id}")

        if not msg_list: 
            logger.debug("消息列表为空")
            return
        
        # 单条直接回退到常规处理
        if len(msg_list) == 1:
            logger.info("单条消息，回退到常规处理")
            self._process_media_event(msg_list[0][1], msg_list[0][0])
            return

        # 多条聚合
        logger.info(f"处理多条聚合消息，数量: {len(msg_list)}")
        first_info = msg_list[0][0]
        events_info = [x[0] for x in msg_list]
        count = len(events_info)

        tmdb_id = self._extract_tmdb_id(first_info)
        first_info.tmdb_id = tmdb_id
        
        tmdb_info = None
        if tmdb_id:
            logger.debug(f"识别TMDB信息: {tmdb_id}")
            try:
                tmdb_info = self.chain.recognize_media(tmdbid=int(tmdb_id), mtype=MediaType.TV)
                if tmdb_info:
                    logger.info(f"TMDB信息识别成功")
            except Exception as e:
                logger.error(f"识别TMDB信息异常: {str(e)}")

        title_name = first_info.item_name
        if first_info.json_object:
            title_name = first_info.json_object.get('Item', {}).get('SeriesName') or title_name
            logger.debug(f"获取系列名称: {title_name}")
        
        year = tmdb_info.year if (tmdb_info and tmdb_info.year) else first_info.json_object.get('Item', {}).get('ProductionYear')
        if year and str(year) not in title_name:
            title_name += f" ({year})"
            logger.debug(f"添加年份: {year}")

        message_title = f"🆕 {title_name} 已入库 (含{count}个文件)"
        logger.debug(f"聚合消息标题: {message_title}")

        message_texts = []
        message_texts.append(f"⏰ {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}")
        
        # 智能分类（优先使用CategoryHelper）
        category = None
        if self._smart_category_enabled and tmdb_info:
            logger.debug("尝试智能分类")
            try:
                category = self.category.get_tv_category(tmdb_info)
                if category:
                    logger.debug(f"智能分类成功: {category}")
            except Exception as e:
                logger.debug(f"获取TMDB分类时出错: {str(e)}")
        
        if not category:
            logger.debug("使用路径解析分类")
            category = self._get_category_from_path(first_info.item_path, "TV", False)
            if category:
                logger.debug(f"路径解析分类: {category}")
        
        if category:
            message_texts.append(f"📂 分类：{category}")

        episodes_str = self._merge_continuous_episodes(events_info)
        message_texts.append(f"📺 季集：{episodes_str}")
        logger.debug(f"聚合季集信息: {episodes_str}")

        self._append_meta_info(message_texts, tmdb_info)
        self._append_genres_actors(message_texts, tmdb_info)

        overview = ""
        if tmdb_info and tmdb_info.overview: 
            overview = tmdb_info.overview
            logger.debug(f"获取到TMDB简介，长度: {len(overview)}")
        elif first_info.overview: 
            overview = first_info.overview
            logger.debug(f"获取到事件简介，长度: {len(overview)}")
        
        if overview:
            if len(overview) > self._overview_max_length:
                overview = overview[:self._overview_max_length].rstrip() + "..."
                logger.debug(f"简介截断为: {self._overview_max_length}字符")
            message_texts.append(f"📖 简介：\n{overview}")

        image_url = first_info.image_url
        if not image_url and tmdb_id:
            logger.debug("尝试获取TMDB图片")
            image_url = self._get_tmdb_image(first_info, MediaType.TV)
            if image_url:
                logger.debug(f"获取到TMDB图片: {image_url[:50]}...")
        
        if not image_url:
            image_url = self._webhook_images.get(first_info.channel)
            logger.debug(f"使用默认图片: {first_info.channel}")
        
        play_link = self._get_play_link(first_info)
        if play_link:
            logger.debug(f"生成播放链接: {play_link[:50]}...")

        logger.info("发送聚合消息")
        self.post_message(
            mtype=NotificationType.MediaServer,
            title=message_title,
            text="\n" + "\n".join(message_texts),
            image=image_url,
            link=play_link
        )
        logger.info("聚合消息发送完成")

    # === 集数合并逻辑 ===
    def _merge_continuous_episodes(self, events: List[WebhookEventInfo]) -> str:
        """合并连续剧集"""
        logger.debug("开始合并连续剧集")
        season_episodes = {}
        for i, event in enumerate(events):
            season, episode = None, None
            episode_name = ""
            if event.json_object and isinstance(event.json_object, dict):
                item = event.json_object.get("Item", {})
                season = item.get("ParentIndexNumber")
                episode = item.get("IndexNumber")
                episode_name = item.get("Name", "")
                logger.debug(f"剧集 {i+1}: S{season}E{episode} - {episode_name}")
            
            if season is None: season = getattr(event, "season_id", None)
            if episode is None: episode = getattr(event, "episode_id", None)
            if not episode_name: episode_name = getattr(event, "item_name", "")

            if season is not None and episode is not None:
                if season not in season_episodes: 
                    season_episodes[season] = []
                season_episodes[season].append({"episode": int(episode), "name": episode_name})

        merged_details = []
        for season in sorted(season_episodes.keys()):
            episodes = season_episodes[season]
            episodes.sort(key=lambda x: x["episode"])
            if not episodes: continue

            start = episodes[0]["episode"]
            end = episodes[0]["episode"]
            
            for i in range(1, len(episodes)):
                current = episodes[i]["episode"]
                if current == end + 1:
                    end = current
                else:
                    merged_details.append(f"S{str(season).zfill(2)}E{str(start).zfill(2)}-E{str(end).zfill(2)}" if start != end else f"S{str(season).zfill(2)}E{str(start).zfill(2)}")
                    start = end = current
            
            merged_details.append(f"S{str(season).zfill(2)}E{str(start).zfill(2)}-E{str(end).zfill(2)}" if start != end else f"S{str(season).zfill(2)}E{str(start).zfill(2)}")
        
        result = ", ".join(merged_details)
        logger.debug(f"剧集合并结果: {result}")
        return result

    def _extract_tmdb_id(self, event_info: WebhookEventInfo) -> Optional[str]:
        """提取TMDB ID"""
        logger.debug("开始提取TMDB ID")

        # 路径关键词黑名单检查：命中则跳过TMDB识别
        item_path = event_info.item_path or ""
        if not item_path and event_info.json_object:
            item_path = event_info.json_object.get('Item', {}).get('Path', '')
        if self._path_skip_keywords and item_path:
            for kw in self._path_skip_keywords:
                if kw in item_path:
                    logger.info(f"路径命中黑名单关键词「{kw}」，跳过TMDB识别: {item_path}")
                    return None

        tmdb_id = event_info.tmdb_id
        if tmdb_id:
            logger.debug(f"从event_info获取TMDB ID: {tmdb_id}")
            return tmdb_id
        
        if not tmdb_id and event_info.json_object:
            provider_ids = event_info.json_object.get('Item', {}).get('ProviderIds', {})
            tmdb_id = provider_ids.get('Tmdb')
            if tmdb_id:
                logger.debug(f"从ProviderIds获取TMDB ID: {tmdb_id}")
                return tmdb_id
        
        if not tmdb_id and event_info.item_path:
            logger.debug(f"从文件路径提取: {event_info.item_path}")
            if match := re.search(r'[\[{](?:tmdbid|tmdb)[=-](\d+)[\]}]', event_info.item_path, re.IGNORECASE):
                tmdb_id = match.group(1)
                logger.debug(f"从文件路径提取TMDB ID: {tmdb_id}")
                return tmdb_id

        if not tmdb_id and event_info.json_object:
            item_data = event_info.json_object.get('Item', {})
            series_id = item_data.get('SeriesId')
            if series_id and item_data.get('Type') == 'Episode':
                try:
                    logger.debug(f"尝试获取剧集系列TMDB ID: {series_id}")
                    service = self.service_info(event_info.server_name)
                    if service:
                        host = service.config.config.get('host')
                        apikey = service.config.config.get('apikey')
                        if host and apikey:
                            import requests
                            # 根据服务器类型选择API路径
                            api_path = self._get_api_path(event_info.server_name)
                            api_url = f"{host}{api_path}/Items?Ids={series_id}&Fields=ProviderIds&api_key={apikey}"
                            logger.debug(f"请求API: {api_url}")
                            res = requests.get(api_url, timeout=5)
                            if res.status_code == 200:
                                data = res.json()
                                if data and data.get('Items'):
                                    parent_ids = data['Items'][0].get('ProviderIds', {})
                                    tmdb_id = parent_ids.get('Tmdb')
                                    if tmdb_id:
                                        logger.debug(f"从API获取TMDB ID: {tmdb_id}")
                                        return tmdb_id
                except Exception as e:
                    logger.debug(f"获取系列TMDB ID异常: {str(e)}")
        
        logger.debug(f"未提取到TMDB ID: {event_info.item_name}")
        return None

    def _get_api_path(self, server_name: str) -> str:
        """
        根据服务器类型获取API路径

        Args:
            server_name: 服务器名称

        Returns:
            str: API路径前缀 (/emby, /jellyfin, 或空字符串用于Plex)
        """
        if not server_name:
            return "/emby"

        server_lower = server_name.lower()
        if "jellyfin" in server_lower:
            return ""
        elif "plex" in server_lower:
            return ""
        else:
            return "/emby"

    def _get_server_name_cn(self, event_info):
        """获取服务器中文名称"""
        server_name = ""
        if event_info.json_object and isinstance(event_info.json_object.get('Server'), dict):
            server_name = event_info.json_object.get('Server', {}).get('Name')
            logger.debug(f"从JSON获取服务器名: {server_name}")

        if not server_name:
            server_name = event_info.server_name or "媒体服务器"
            logger.debug(f"从event_info获取服务器名: {server_name}")

        return server_name

    def _get_emby_local_image(self, event_info: WebhookEventInfo) -> Optional[str]:
        """从Emby本地构造图片URL（不经过TMDB），优先使用Backdrop横幅图"""
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
            # 优先使用自定义图片Host，否则使用插件配置的服务器地址
            host = (self._emby_image_host or service.config.config.get('host', '')).rstrip('/')
            if not host:
                return None
            # 获取API路径
            api_path = self._get_api_path(event_info.server_name)
            # 优先 Backdrop（横幅大图）
            backdrop_tags = item_data.get('BackdropImageTags', [])
            if backdrop_tags:
                tag = backdrop_tags[0]
                url = f"{host}{api_path}/Items/{item_id}/Images/Backdrop/0?tag={tag}&maxWidth=1920&quality=70"
                logger.debug(f"构造 Backdrop图片URL: {url[:80]}...")
                return url
            # 回退 Primary
            image_tags = item_data.get('ImageTags', {})
            tag = image_tags.get('Primary') or image_tags.get('Thumb')
            image_type = 'Primary' if image_tags.get('Primary') else 'Thumb'
            if not tag:
                return None
            url = f"{host}{api_path}/Items/{item_id}/Images/{image_type}?maxHeight=450&maxWidth=450&tag={tag}&quality=90"
            logger.debug(f"构造 Primary图片URL: {url[:80]}...")
            return url
        except Exception as e:
            logger.debug(f"构造Emby本地图片URL异常: {str(e)}")
            return None

    def _get_audio_image_url(self, server_name: str, item_data: dict) -> Optional[str]:
        """获取音频图片URL"""
        logger.debug("获取音频图片URL")
        if not server_name: 
            logger.debug("服务器名称为空")
            return None
        
        try:
            service = self.service_info(server_name)
            if not service or not service.instance: 
                logger.debug("无法获取服务器服务")
                return None
            
            play_url = service.instance.get_play_url("dummy")
            if not play_url: 
                logger.debug("无法获取播放URL")
                return None
            
            parsed = urllib.parse.urlparse(play_url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            item_id = item_data.get('Id')
            primary_tag = item_data.get('ImageTags', {}).get('Primary')

            if not primary_tag:
                item_id = item_data.get('PrimaryImageItemId')
                primary_tag = item_data.get('PrimaryImageTag')
                logger.debug(f"备用图片标签: item_id={item_id}, tag={primary_tag}")

            if item_id and primary_tag:
                api_path = self._get_api_path(server_name)
                img_url = f"{base_url}{api_path}/Items/{item_id}/Images/Primary?maxHeight=450&maxWidth=450&tag={primary_tag}&quality=90"
                logger.debug(f"生成音频图片URL: {img_url[:50]}...")
                return img_url
            
            logger.debug("未找到音频图片信息")
        except Exception as e:
            logger.debug(f"获取音频图片URL异常: {str(e)}")
        
        return None

    def _get_tmdb_image(self, event_info: WebhookEventInfo, mtype: MediaType) -> Optional[str]:
        """获取TMDB图片"""
        key = f"{event_info.tmdb_id}_{event_info.season_id}_{event_info.episode_id}"
        logger.debug(f"获取TMDB图片，缓存键: {key}")
        
        if key in self._image_cache:
            logger.debug(f"从缓存获取图片: {key}")
            return self._image_cache[key]
        
        try:
            logger.debug(f"请求TMDB背景图片")
            img = self.chain.obtain_specific_image(
                mediaid=event_info.tmdb_id, mtype=mtype, 
                image_type=MediaImageType.Backdrop, 
                season=event_info.season_id, episode=event_info.episode_id
            )
            
            if not img:
                logger.debug(f"请求TMDB海报图片")
                img = self.chain.obtain_specific_image(
                    mediaid=event_info.tmdb_id, mtype=mtype, 
                    image_type=MediaImageType.Poster, 
                    season=event_info.season_id, episode=event_info.episode_id
                )
            
            if img:
                # 缓存管理
                if len(self._image_cache) > self.IMAGE_CACHE_MAX_SIZE:
                    oldest_key = next(iter(self._image_cache))
                    logger.debug(f"清理缓存图片: {oldest_key}")
                    self._image_cache.pop(oldest_key)
                
                self._image_cache[key] = img
                logger.debug(f"获取到TMDB图片: {img[:50]}...")
                return img
            else:
                logger.debug("未获取到TMDB图片")
                
        except Exception as e:
            logger.error(f"获取TMDB图片异常: {str(e)}")
        
        return None

    def _get_category_from_path(self, path: str, item_type: str, is_folder: bool = False) -> str:
        """从路径获取分类"""
        logger.debug(f"从路径获取分类: {path}")
        if not path: 
            logger.debug("路径为空")
            return ""
        
        try:
            path = os.path.normpath(path)
            logger.debug(f"规范化路径: {path}")
            
            if is_folder and item_type in ["TV", "SHOW"]:
                category = os.path.basename(os.path.dirname(path))
                logger.debug(f"文件夹模式获取分类: {category}")
                return category
            
            current_dir = os.path.dirname(path)
            dir_name = os.path.basename(current_dir)
            logger.debug(f"当前目录: {current_dir}, 目录名: {dir_name}")
            
            if re.search(r'^(Season|季|S\d)', dir_name, re.IGNORECASE):
                current_dir = os.path.dirname(current_dir)
                logger.debug(f"跳过季目录，上级目录: {current_dir}")
            
            category_dir = os.path.dirname(current_dir)
            category = os.path.basename(category_dir)
            logger.debug(f"分类目录: {category_dir}, 分类: {category}")
            
            if not category or category == os.path.sep: 
                logger.debug("分类为空或根目录")
                return ""
            
            return category
        except Exception as e:
            logger.error(f"从路径获取分类异常: {str(e)}")
            return ""

    def _handle_music_album(self, event_info: WebhookEventInfo, item_data: dict):
        """处理音乐专辑"""
        logger.info("开始处理音乐专辑")
        try:
            album_name = item_data.get('Name', '')
            album_id = item_data.get('Id', '')
            album_artist = (item_data.get('Artists') or ['未知艺术家'])[0]
            primary_image_item_id = item_data.get('PrimaryImageItemId') or album_id
            primary_image_tag = item_data.get('PrimaryImageTag') or item_data.get('ImageTags', {}).get('Primary')

            logger.debug(f"专辑信息: {album_name}, 艺术家: {album_artist}")

            service = self.service_info(event_info.server_name)
            if not service or not service.instance: 
                logger.warning("无法获取服务器服务")
                return
            
            base_url = service.config.config.get('host', '')
            api_key = service.config.config.get('apikey', '')
            
            if not base_url or not api_key:
                logger.warning("服务器配置不完整")
                return

            import requests
            fields = "Path,MediaStreams,Container,Size,RunTimeTicks,ImageTags,ProviderIds"
            api_path = self._get_api_path(event_info.server_name)
            api_url = f"{base_url}{api_path}/Items?ParentId={album_id}&Fields={fields}&api_key={api_key}"

            logger.debug(f"请求专辑歌曲列表: {api_url}")
            res = requests.get(api_url, timeout=10)
            
            if res.status_code == 200:
                items = res.json().get('Items', [])
                logger.info(f"专辑 [{album_name}] 包含 {len(items)} 首歌曲")
                
                for i, song in enumerate(items):
                    logger.debug(f"处理第 {i+1} 首歌曲: {song.get('Name', '未知歌曲')}")
                    self._send_single_audio_notify(
                        song, album_name, album_artist,
                        primary_image_item_id, primary_image_tag,
                        base_url, event_info.server_name
                    )
            else:
                logger.error(f"请求专辑歌曲失败，状态码: {res.status_code}")
                
        except Exception as e:
            logger.error(f"处理音乐专辑失败: {str(e)}")
            logger.error(traceback.format_exc())

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

            title = f"🎵 新入库媒体：{song_name}"
            texts = []

            texts.append(f"⏰ 入库：{time.strftime('%H:%M:%S', time.localtime())}")
            texts.append(f"👤 歌手：{artist}")
            if album_name: texts.append(f"💿 专辑：{album_name}")
            texts.append(f"⏱️ 时长：{duration}")
            texts.append(f"📦 格式：{container} · {size}")

            image_url = None
            if cover_item_id and cover_tag:
                 api_path = self._get_api_path(server_name) if server_name else "/emby"
                 image_url = f"{base_url}{api_path}/Items/{cover_item_id}/Images/Primary?maxHeight=450&maxWidth=450&tag={cover_tag}&quality=90"
                 logger.debug(f"设置专辑封面图片")

            link = None
            if self._add_play_link:
                link = f"{base_url}/web/index.html#!/item?id={song_id}&serverId={song.get('ServerId', '')}"
                logger.debug(f"生成播放链接")

            logger.info(f"发送单曲通知: {song_name}")
            self.post_message(
                mtype=NotificationType.MediaServer,
                title=title,
                text="\n" + "\n".join(texts),
                image=image_url,
                link=link
            )
            logger.debug(f"单曲通知发送完成")
            
        except Exception as e:
            logger.error(f"发送单曲通知失败: {str(e)}")
            logger.error(traceback.format_exc())

    def _append_meta_info(self, texts: List[str], tmdb_info):
        """追加元数据信息"""
        if not tmdb_info: 
            logger.debug("无TMDB元数据信息")
            return
        
        logger.debug("追加TMDB元数据信息")
        if hasattr(tmdb_info, 'vote_average') and tmdb_info.vote_average:
            score = round(float(tmdb_info.vote_average), 1)
            texts.append(f"⭐️ 评分：{score}")
            logger.debug(f"评分信息: {score}")
        
        region = self._get_region_text_cn(tmdb_info)
        if region:
            #texts.append(f"🏳️ 地区：{region}")
            logger.debug(f"地区信息: {region}")

        if hasattr(tmdb_info, 'status') and tmdb_info.status:
            status_map = {'Ended': '已完结', 'Returning Series': '连载中', 'Canceled': '已取消', 
                         'In Production': '制作中', 'Planned': '计划中', 'Released': '已上映', 
                         'Continuing': '连载中'}
            status_text = status_map.get(tmdb_info.status, tmdb_info.status)
            #texts.append(f"📡 状态：{status_text}")
            logger.debug(f"状态信息: {status_text}")

    def _get_region_text_cn(self, tmdb_info) -> str:
        """获取地区中文文本"""
        if not tmdb_info: 
            return ""
        
        try:
            codes = []
            if hasattr(tmdb_info, 'origin_country') and tmdb_info.origin_country:
                codes = tmdb_info.origin_country[:2]
                logger.debug(f"原始国家代码: {codes}")
            elif hasattr(tmdb_info, 'production_countries') and tmdb_info.production_countries:
                for c in tmdb_info.production_countries[:2]:
                    if isinstance(c, dict): 
                        code = c.get('iso_3166_1')
                    else: 
                        code = getattr(c, 'iso_3166_1', str(c))
                    if code: 
                        codes.append(code)
                logger.debug(f"制作国家代码: {codes}")
            
            if not codes: 
                return ""
            
            cn_names = [self._country_cn_map.get(code.upper(), code) for code in codes]
            result = "、".join(cn_names)
            logger.debug(f"地区中文名: {result}")
            return result
        except Exception as e:
            logger.debug(f"获取地区信息异常: {str(e)}")
            return ""

    def _append_genres_actors(self, texts: List[str], tmdb_info):
        """追加类型和演员信息"""
        if not tmdb_info: 
            logger.debug("无类型和演员信息")
            return
        
        logger.debug("追加类型和演员信息")
        if hasattr(tmdb_info, 'genres') and tmdb_info.genres:
            genres = [g.get('name') if isinstance(g, dict) else str(g) for g in tmdb_info.genres[:3]]
            if genres:
                #texts.append(f"🎭 类型：{'、'.join(genres)}")
                logger.debug(f"类型信息: {'、'.join(genres)}")
        
        if hasattr(tmdb_info, 'actors') and tmdb_info.actors:
            actors = [a.get('name') if isinstance(a, dict) else str(a) for a in tmdb_info.actors[:3]]
            if actors: 
                texts.append(f"🎬 演员：{'、'.join(actors)}")
                logger.debug(f"演员信息: {'、'.join(actors)}")

    def _append_season_episode_info(self, texts: List[str], event_info: WebhookEventInfo, series_name: str):
        """追加季集信息"""
        logger.debug("追加季集信息")
        if event_info.season_id is not None and event_info.episode_id is not None:
            s_str, e_str = str(event_info.season_id).zfill(2), str(event_info.episode_id).zfill(2)
            info = f"📺 季集：S{s_str}E{e_str}"
            #ep_name = event_info.json_object.get('Item', {}).get('Name')
            #if ep_name and ep_name != series_name: 
                #info += f" - {ep_name}"
                #logger.debug(f"剧集名称: {ep_name}")
            texts.append(info)
            logger.debug(f"季集信息: {info}")
        elif description := event_info.json_object.get('Description'):
            first_line = description.split('\n\n')[0].strip()
            if re.search(r'S\d+\s+E\d+', first_line):
                 texts.append(f"📺 季集：{first_line}")
                 logger.debug(f"从描述提取季集: {first_line}")

    def _append_extra_info(self, texts: List[str], event_info: WebhookEventInfo):
        """追加额外信息"""
        logger.debug("追加额外信息")
        extras = []
        if event_info.user_name: 
            extras.append(f"👤 用户：{event_info.user_name}")
            logger.debug(f"用户信息: {event_info.user_name}")
        
        if event_info.device_name: 
            device = event_info.device_name
            if event_info.client and event_info.client not in device:
                device = f"{event_info.client} {device}"
            extras.append(f"📱 设备：{device}")
            logger.debug(f"设备信息: {device}")
        
        if event_info.ip: 
            try:
                location = WebUtils.get_location(event_info.ip)
                extras.append(f"🌐 IP：{event_info.ip} ({location})")
                logger.debug(f"IP信息: {event_info.ip} ({location})")
            except Exception as e:
                logger.debug(f"获取IP位置信息时出错: {str(e)}")
                extras.append(f"🌐 IP：{event_info.ip}")
        
        if event_info.percentage: 
            percentage = round(float(event_info.percentage), 2)
            extras.append(f"📊 进度：{percentage}%")
            logger.debug(f"播放进度: {percentage}%")
        
        if extras: 
            texts.extend(extras)
            logger.debug(f"添加了 {len(extras)} 条额外信息")

    def _get_play_link(self, event_info: WebhookEventInfo) -> Optional[str]:
        """获取播放链接"""
        if not self._add_play_link or not event_info.server_name: 
            logger.debug("播放链接未启用或服务器名为空")
            return None
        
        logger.debug(f"生成播放链接，服务器: {event_info.server_name}")
        service = self.service_info(event_info.server_name)
        if service and service.instance:
            link = service.instance.get_play_url(event_info.item_id)
            if link:
                logger.debug(f"播放链接生成成功: {link[:50]}...")
            else:
                logger.debug("播放链接生成失败")
            return link
        else:
            logger.debug("无法获取服务器实例")
            return None

    def _format_ticks(self, ticks) -> str:
        """格式化时间刻度"""
        if not ticks: 
            return "00:00"
        s = ticks / 10000000
        result = f"{int(s // 60)}:{int(s % 60):02d}"
        logger.debug(f"时间格式化: {ticks} -> {result}")
        return result

    def _format_size(self, size) -> str:
        """格式化文件大小"""
        if not size: 
            return "0MB"
        mb_size = round(size / 1024 / 1024, 1)
        result = f"{mb_size} MB"
        logger.debug(f"大小格式化: {size} -> {result}")
        return result

    def _add_key_cache(self, key):
        """添加元素到过期字典中"""
        logger.debug(f"添加缓存键: {key}")
        self._webhook_msg_keys[key] = time.time() + self.DEFAULT_EXPIRATION_TIME
        logger.debug(f"当前缓存数量: {len(self._webhook_msg_keys)}")

    def _remove_key_cache(self, key):
        """从过期字典中移除指定元素"""
        if key in self._webhook_msg_keys: 
            del self._webhook_msg_keys[key]
            logger.debug(f"移除缓存键: {key}")
            logger.debug(f"当前缓存数量: {len(self._webhook_msg_keys)}")

    def _clean_expired_cache(self):
        """清理过期的缓存元素"""
        current_time = time.time()
        expired_keys = [k for k, v in self._webhook_msg_keys.items() if v <= current_time]
        
        if expired_keys:
            logger.debug(f"清理 {len(expired_keys)} 个过期缓存")
            for key in expired_keys:
                self._webhook_msg_keys.pop(key, None)
            logger.debug(f"清理后缓存数量: {len(self._webhook_msg_keys)}")

    @cached(
        region="MediaServerMsgAI",
        maxsize=128,
        ttl=600,
        skip_none=True,
        skip_empty=False
    )
    def _get_tmdb_info(self, tmdb_id: str, mtype: MediaType, season: Optional[int] = None):
        """
        获取TMDB信息（带缓存）

        Args:
            tmdb_id: TMDB ID
            mtype: 媒体类型
            season: 季数（仅电视剧需要）

        Returns:
            dict: TMDB信息
        """
        logger.debug(f"获取TMDB信息，ID: {tmdb_id}, 类型: {mtype}, 季: {season}")
        try:
            if mtype == MediaType.MOVIE:
                info = self.chain.tmdb_info(tmdbid=tmdb_id, mtype=mtype)
            else:
                tmdb_info = self.chain.tmdb_info(tmdbid=tmdb_id, mtype=mtype, season=season)
                tmdb_info2 = self.chain.tmdb_info(tmdbid=tmdb_id, mtype=mtype)
                if tmdb_info and tmdb_info2:
                    info = {**tmdb_info2, **tmdb_info}
                else:
                    info = tmdb_info or tmdb_info2
            
            if info:
                logger.debug(f"TMDB信息获取成功: {tmdb_id}")
            else:
                logger.debug(f"TMDB信息获取失败: {tmdb_id}")
            
            return info
        except Exception as e:
            logger.error(f"获取TMDB信息异常: {str(e)}")
            return None

    def stop_service(self):
        """
        退出插件时的清理工作

        确保：
        1. 所有待处理的聚合消息被立即发送
        2. 所有定时器被取消
        3. 清空所有内部缓存数据
        """
        logger.info("插件停止，开始清理工作")
        try:
            # 发送所有待处理的聚合消息
            pending_count = len(self._pending_messages)
            if pending_count > 0:
                logger.info(f"发送 {pending_count} 个待处理的聚合消息")
                for series_id in list(self._pending_messages.keys()):
                    try:
                        logger.debug(f"发送聚合消息: {series_id}")
                        self._send_aggregated_message(series_id)
                    except Exception as e:
                        logger.error(f"发送聚合消息时出错: {str(e)}")
            else:
                logger.debug("无待处理的聚合消息")
            
            # 取消所有定时器
            timer_count = len(self._aggregate_timers)
            if timer_count > 0:
                logger.info(f"取消 {timer_count} 个定时器")
                for timer in self._aggregate_timers.values():
                    try:
                        timer.cancel()
                        logger.debug("定时器取消成功")
                    except Exception as e:
                        logger.debug(f"取消定时器时出错: {str(e)}")
            else:
                logger.debug("无活跃定时器")
            
            # 清理缓存数据
            logger.info("清理缓存数据")
            self._aggregate_timers.clear()
            self._pending_messages.clear()
            self._webhook_msg_keys.clear()
            self._image_cache.clear()
            
            # 清理TMDB缓存
            try:
                self._get_tmdb_info.cache_clear()
                logger.debug("TMDB缓存清理完成")
            except Exception as e:
                logger.debug(f"清理TMDB缓存时出错: {str(e)}")
            
            logger.info("插件清理完成")
            
        except Exception as e:
            logger.error(f"插件停止时发生错误: {str(e)}")
            logger.error(traceback.format_exc())
