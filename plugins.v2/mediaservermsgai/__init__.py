import re
import time
import traceback
import threading
import os
import urllib.parse
import requests
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
    8. 新增：支持设置排除媒体库，不触发TMDB元数据识别
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
    plugin_version = "1.9.5"
    plugin_author = "jxxghp"
    author_url = "https://github.com/jxxghp"
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
    _exclude_libs = []                         # 新增：排除库列表

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
        "PlaybackStop": "停止播放"
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

    def init_plugin(self, config: dict = None):
        """
        初始化插件配置
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
            
            # --- 新增：解析排除库配置 ---
            exclude_str = config.get("exclude_libs") or ""
            self._exclude_libs = [s.strip() for s in re.split(r'[，,]', exclude_str) if s.strip()]
            
            logger.info(f"插件配置初始化完成。排除库: {self._exclude_libs}")

    def service_infos(self, type_filter: Optional[str] = None) -> Optional[Dict[str, ServiceInfo]]:
        if not self._mediaservers: return None
        services = MediaServerHelper().get_services(type_filter=type_filter, name_filters=self._mediaservers)
        if not services: return None
        active_services = {}
        for name, info in services.items():
            if not info.instance.is_inactive(): active_services[name] = info
        return active_services if active_services else None

    def service_info(self, name: str) -> Optional[ServiceInfo]:
        services = self.service_infos()
        return services.get(name) if services else None

    def get_state(self) -> bool:
        return self._enabled

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面
        """
        types_options = [
            {"title": "新入库", "value": "library.new"},
            {"title": "开始播放", "value": "playback.start|media.play|PlaybackStart"},
            {"title": "停止播放", "value": "playback.stop|media.stop|PlaybackStop"},
            {"title": "暂停/继续", "value": "playback.pause|playback.unpause|media.pause|media.resume"},
            {"title": "用户标记", "value": "item.rate|item.markplayed|item.markunplayed"},
            {"title": "登录提醒", "value": "user.authenticated|user.authenticationfailed"},
            {"title": "系统测试", "value": "system.webhooktest|system.notificationtest"},
        ]
        
        # 预先获取服务器列表，防止渲染失败
        ms_configs = MediaServerHelper().get_configs()
        ms_items = [{"title": config.name, "value": config.name} for config in ms_configs.values()]

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
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VSelect', 'props': {'multiple': True, 'chips': True, 'clearable': True, 'model': 'mediaservers', 'label': '媒体服务器', 'items': ms_items}}]}
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
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextField', 'props': {'model': 'exclude_libs', 'label': '排除的媒体库', 'placeholder': '库名1,库名2', 'hint': '填入不希望进行 TMDB 识别的媒体库名称，多个用逗号分隔'}}]}
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
                    }
                ]
            }
        ], {
            "enabled": False, 
            "types": [], 
            "mediaservers": [],
            "add_play_link": False,
            "aggregate_enabled": False, 
            "aggregate_time": self.DEFAULT_AGGREGATE_TIME,
            "smart_category_enabled": True,
            "filter_unrecognized": True,
            "exclude_libs": "",
            "overview_max_length": self.DEFAULT_OVERVIEW_MAX_LENGTH
        }

    def _get_library_name(self, event_info: WebhookEventInfo) -> str:
        """从事件中提取媒体库名称"""
        if not event_info or not event_info.json_object: return ""
        # Emby / Jellyfin
        lib_name = event_info.json_object.get('Item', {}).get('LibraryName')
        # Plex
        if not lib_name:
            lib_name = event_info.json_object.get('Metadata', {}).get('librarySectionTitle')
        return lib_name or ""

    @eventmanager.register(EventType.WebhookMessage)
    def send(self, event: Event):
        """
        发送通知消息主入口函数
        """
        try:
            if not self._enabled: return
            
            event_info: WebhookEventInfo = event.event_data
            if not event_info: return
            
            if not self._webhook_actions.get(event_info.event): return

            allowed_types = set()
            for _type in self._types: allowed_types.update(_type.split("|"))
            
            if event_info.event not in allowed_types: return

            if event_info.server_name:
                if not self.service_info(name=event_info.server_name): return
            
            event_type = str(event_info.event).lower()
            
            # --- 新增：排除库检测 ---
            curr_lib = self._get_library_name(event_info)
            is_excluded = curr_lib in self._exclude_libs

            # 5. TMDB未识别视频过滤检查 (排除库跳过此项)
            if self._filter_unrecognized and not is_excluded:
                if event_info.item_type not in ["AUD", "MusicAlbum"]:
                    if event_info.item_type in ["MOV", "TV", "SHOW"]:
                        if event_type in ["library.new", "playback.start", "playback.stop", 
                                         "media.play", "media.stop", "PlaybackStart", "PlaybackStop",
                                         "playback.pause", "playback.unpause", "media.pause", "media.resume"]:
                            tmdb_id = self._extract_tmdb_id(event_info)
                            if not tmdb_id:
                                logger.info(f"TMDB未识别视频，跳过通知: {event_info.item_name}")
                                return

            # 6. 根据事件类型分发处理
            if "test" in event_type:
                self._handle_test_event(event_info)
                return

            if "user.authentic" in event_type:
                self._handle_login_event(event_info)
                return

            if "item." in event_type and ("rate" in event_type or "mark" in event_type):
                self._handle_rate_event(event_info)
                return

            if event_info.json_object and event_info.json_object.get('Item', {}).get('Type') == 'MusicAlbum' and event_type == 'library.new':
                self._handle_music_album(event_info, event_info.json_object.get('Item', {}))
                return

            # 剧集聚合 (排除库不参与聚合)
            if (self._aggregate_enabled and not is_excluded and
                event_type == "library.new" and 
                event_info.item_type in ["TV", "SHOW"]):
                series_id = self._get_series_id(event_info)
                if series_id:
                    self._aggregate_tv_episodes(series_id, event_info, event)
                    return

            self._process_media_event(event, event_info)

        except Exception as e:
            logger.error(f"Webhook分发异常: {str(e)}\n{traceback.format_exc()}")

    def _process_media_event(self, event: Event, event_info: WebhookEventInfo):
        """处理常规媒体消息（入库/播放）"""
        try:
            self._clean_expired_cache()
            
            # 1. 防重复与防抖
            expiring_key = f"{event_info.item_id}-{event_info.client}-{event_info.user_name}-{event_info.event}"
            if str(event_info.event) == "playback.stop" and expiring_key in self._webhook_msg_keys:
                return
            
            with self._lock:
                current_time = time.time()
                last_event, last_time = self._last_event_cache
                if last_event and (current_time - last_time < 2):
                    if last_event.event_id == event.event_id: return
                self._last_event_cache = (event, current_time)

            # 2. 元数据识别 (如果是排除库，则不提取 TMDB ID)
            curr_lib = self._get_library_name(event_info)
            if curr_lib in self._exclude_libs:
                logger.info(f"检测到排除库【{curr_lib}】，跳过TMDB识别。")
                tmdb_id = None
            else:
                tmdb_id = self._extract_tmdb_id(event_info)
            
            event_info.tmdb_id = tmdb_id
            message_texts = []
            message_title = ""
            image_url = event_info.image_url
            
            # 3. 音频处理
            if event_info.item_type == "AUD":
                self._build_audio_message(event_info, message_texts)
                action_base = self._webhook_actions.get(event_info.event, "通知")
                song_name = event_info.json_object.get('Item', {}).get('Name') if event_info.json_object else event_info.item_name
                message_title = f"{song_name} {action_base} {self._get_server_name_cn(event_info)}"
                img = self._get_audio_image_url(event_info.server_name, event_info.json_object.get('Item', {}))
                if img: image_url = img

            # 4. 视频处理
            else:
                tmdb_info = None
                if tmdb_id:
                    mtype = MediaType.MOVIE if event_info.item_type == "MOV" else MediaType.TV
                    try:
                        tmdb_info = self.chain.recognize_media(tmdbid=int(tmdb_id), mtype=mtype)
                    except: pass

                title_name = tmdb_info.title if (tmdb_info and tmdb_info.title) else event_info.item_name
                year = tmdb_info.year if (tmdb_info and tmdb_info.year) else (event_info.json_object.get('Item', {}).get('ProductionYear') if event_info.json_object else None)
                if year and str(year) not in title_name:
                    title_name += f" ({year})"
                
                action_base = self._webhook_actions.get(event_info.event, "通知")
                if "library.new" in event_info.event:
                    message_title = f"🆕 {title_name} 已入库"
                elif "playback.start" in event_info.event or "media.play" in event_info.event or "PlaybackStart" in event_info.event:
                    message_title = f"▶️ 开始播放：{title_name}"
                elif "playback.stop" in event_info.event or "media.stop" in event_info.event or "PlaybackStop" in event_info.event:
                    message_title = f"⏹️ 停止播放：{title_name}"
                else:
                    message_title = f"📢 {action_base}：{title_name}"
                
                message_texts.append(f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
                if curr_lib: message_texts.append(f"📁 媒体库：{curr_lib}")

                category = None
                if self._smart_category_enabled and tmdb_info:
                    try:
                        category = self.category.get_movie_category(tmdb_info) if event_info.item_type == "MOV" else self.category.get_tv_category(tmdb_info)
                    except: pass
                
                if not category:
                    is_folder = event_info.json_object.get('Item', {}).get('IsFolder', False) if event_info.json_object else False
                    category = self._get_category_from_path(event_info.item_path, event_info.item_type, is_folder)
                
                if category: message_texts.append(f"📂 分类：{category}")

                self._append_season_episode_info(message_texts, event_info, title_name)
                self._append_meta_info(message_texts, tmdb_info)
                self._append_genres_actors(message_texts, tmdb_info)

                overview = tmdb_info.overview if (tmdb_info and tmdb_info.overview) else event_info.overview
                if overview and "library.new" in event_info.event:
                    if len(overview) > self._overview_max_length:
                        overview = overview[:self._overview_max_length] + "..."
                    message_texts.append(f"📖 简介：\n{overview}")

                if not image_url and tmdb_id:
                    image_url = self._get_tmdb_image(event_info, MediaType.MOVIE if event_info.item_type == "MOV" else MediaType.TV)

            self._append_extra_info(message_texts, event_info)
            play_link = self._get_play_link(event_info)
            if not image_url: image_url = self._webhook_images.get(event_info.channel)

            if str(event_info.event) == "playback.stop": self._add_key_cache(expiring_key)
            if str(event_info.event) == "playback.start": self._remove_key_cache(expiring_key)

            self.post_message(mtype=NotificationType.MediaServer, title=message_title, text="\n".join(message_texts), image=image_url, link=play_link)
            
        except Exception as e:
            logger.error(f"处理媒体事件异常: {str(e)}\n{traceback.format_exc()}")

    # ==================== 所有辅助函数（维持原状） ====================
    def _handle_test_event(self, event_info: WebhookEventInfo):
        title = f"🔔 媒体服务器通知测试"
        texts = [f"来自：{self._get_server_name_cn(event_info)}", f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", "状态：连接正常"]
        if event_info.user_name: texts.append(f"用户：{event_info.user_name}")
        self.post_message(mtype=NotificationType.MediaServer, title=title, text="\n".join(texts), image=self._webhook_images.get(event_info.channel))

    def _handle_login_event(self, event_info: WebhookEventInfo):
        action = "登录成功" if "authenticated" in event_info.event and "failed" not in event_info.event else "登录失败"
        texts = [f"👤 用户：{event_info.user_name}", f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"]
        if event_info.device_name: texts.append(f"📱 设备：{event_info.client} {event_info.device_name}")
        if event_info.ip:
            try: texts.append(f"🌐 IP：{event_info.ip} {WebUtils.get_location(event_info.ip)}")
            except: texts.append(f"🌐 IP：{event_info.ip}")
        texts.append(f"🖥️ 服务器：{self._get_server_name_cn(event_info)}")
        self.post_message(mtype=NotificationType.MediaServer, title=f"🔐 {action}提醒", text="\n".join(texts), image=self._webhook_images.get(event_info.channel))

    def _handle_rate_event(self, event_info: WebhookEventInfo):
        curr_lib = self._get_library_name(event_info)
        tmdb_id = None if curr_lib in self._exclude_libs else self._extract_tmdb_id(event_info)
        texts = [f"👤 用户：{event_info.user_name}", f"🏷️ 标记：{self._webhook_actions.get(event_info.event, '已标记')}", f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"]
        image_url = event_info.image_url
        if not image_url and tmdb_id:
            image_url = self._get_tmdb_image(event_info, MediaType.MOVIE if event_info.item_type == "MOV" else MediaType.TV)
        self.post_message(mtype=NotificationType.MediaServer, title=f"⭐ 用户评分：{event_info.item_name}", text="\n".join(texts), image=image_url or self._webhook_images.get(event_info.channel))

    def _build_audio_message(self, event_info, texts):
        item_data = event_info.json_object.get('Item', {}) if event_info.json_object else {}
        texts.append(f"⏰ 时间：{time.strftime('%H:%M:%S')}")
        texts.append(f"👤 歌手：{(item_data.get('Artists') or ['未知歌手'])[0]}")
        if item_data.get('Album'): texts.append(f"💿 专辑：{item_data.get('Album')}")
        texts.append(f"⏱️ 时长：{self._format_ticks(item_data.get('RunTimeTicks', 0))}")
        texts.append(f"📦 格式：{item_data.get('Container', '').upper()} · {self._format_size(item_data.get('Size', 0))}")

    def _get_series_id(self, event_info: WebhookEventInfo):
        if event_info.json_object:
            item = event_info.json_object.get("Item", {})
            return item.get("SeriesId") or item.get("SeriesName")
        return getattr(event_info, "series_id", None)

    def _aggregate_tv_episodes(self, series_id, event_info, event):
        with self._lock:
            if series_id not in self._pending_messages: self._pending_messages[series_id] = []
            self._pending_messages[series_id].append((event_info, event))
            if series_id in self._aggregate_timers: self._aggregate_timers[series_id].cancel()
            timer = threading.Timer(self._aggregate_time, self._send_aggregated_message, [series_id])
            self._aggregate_timers[series_id] = timer
            timer.start()

    def _send_aggregated_message(self, series_id):
        with self._lock:
            if series_id not in self._pending_messages: return
            msg_list = self._pending_messages.pop(series_id)
            if series_id in self._aggregate_timers: del self._aggregate_timers[series_id]
        if not msg_list: return
        if len(msg_list) == 1: self._process_media_event(msg_list[0][1], msg_list[0][0]); return
        first_info = msg_list[0][0]
        tmdb_id = self._extract_tmdb_id(first_info)
        tmdb_info = None
        if tmdb_id:
            try: tmdb_info = self.chain.recognize_media(tmdbid=int(tmdb_id), mtype=MediaType.TV)
            except: pass
        title_name = first_info.json_object.get('Item', {}).get('SeriesName') if first_info.json_object else first_info.item_name
        message_texts = [f"⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}", f"📺 季集：{self._merge_continuous_episodes([x[0] for x in msg_list])}"]
        self._append_meta_info(message_texts, tmdb_info)
        self.post_message(mtype=NotificationType.MediaServer, title=f"🆕 {title_name} 已入库 (含{len(msg_list)}个文件)", text="\n".join(message_texts), image=self._get_tmdb_image(first_info, MediaType.TV) or self._webhook_images.get(first_info.channel))

    def _merge_continuous_episodes(self, events):
        season_episodes = {}
        for event in events:
            if event.json_object:
                item = event.json_object.get("Item", {})
                s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
                if s is not None and e is not None:
                    if s not in season_episodes: season_episodes[s] = []
                    season_episodes[s].append(int(e))
        res = []
        for s in sorted(season_episodes.keys()):
            eps = sorted(list(set(season_episodes[s])))
            if not eps: continue
            res.append(f"S{str(s).zfill(2)}E{str(eps[0]).zfill(2)}-E{str(eps[-1]).zfill(2)}" if len(eps)>1 else f"S{str(s).zfill(2)}E{str(eps[0]).zfill(2)}")
        return ", ".join(res)

    def _extract_tmdb_id(self, event_info: WebhookEventInfo):
        tmdb_id = event_info.tmdb_id
        if not tmdb_id and event_info.json_object:
            tmdb_id = event_info.json_object.get('Item', {}).get('ProviderIds', {}).get('Tmdb')
        if not tmdb_id and event_info.item_path:
            match = re.search(r'[\[{](?:tmdbid|tmdb)[=-](\d+)[\]}]', event_info.item_path, re.IGNORECASE)
            if match: tmdb_id = match.group(1)
        return tmdb_id

    def _get_server_name_cn(self, event_info):
        name = (event_info.json_object.get('Server', {}).get('Name') if event_info.json_object else "") or event_info.server_name or "Emby"
        return name if "emby" in name.lower() else f"{name}Emby"

    def _get_audio_image_url(self, server_name, item_data):
        try:
            service = self.service_info(server_name)
            if not service: return None
            parsed = urllib.parse.urlparse(service.instance.get_play_url("dummy"))
            base = f"{parsed.scheme}://{parsed.netloc}"
            item_id = item_data.get('Id') or item_data.get('PrimaryImageItemId')
            tag = item_data.get('ImageTags', {}).get('Primary') or item_data.get('PrimaryImageTag')
            if item_id and tag: return f"{base}/emby/Items/{item_id}/Images/Primary?maxHeight=450&maxWidth=450&tag={tag}&quality=90"
        except: return None

    def _get_tmdb_image(self, event_info, mtype):
        key = f"{event_info.tmdb_id}_{event_info.season_id}_{event_info.episode_id}"
        if key in self._image_cache: return self._image_cache[key]
        try:
            img = self.chain.obtain_specific_image(mediaid=event_info.tmdb_id, mtype=mtype, image_type=MediaImageType.Backdrop, season=event_info.season_id, episode=event_info.episode_id)
            if img: self._image_cache[key] = img
            return img
        except: return None

    def _get_category_from_path(self, path, item_type, is_folder=False):
        if not path: return ""
        try:
            path = os.path.normpath(path)
            if is_folder and item_type in ["TV", "SHOW"]: return os.path.basename(os.path.dirname(path))
            curr = os.path.dirname(path)
            if re.search(r'^(Season|季|S\d)', os.path.basename(curr), re.IGNORECASE): curr = os.path.dirname(curr)
            return os.path.basename(os.path.dirname(curr))
        except: return ""

    def _handle_music_album(self, event_info: WebhookEventInfo, item_data: dict):
        try:
            album_name = item_data.get('Name', '')
            service = self.service_info(event_info.server_name)
            if not service: return
            host, apikey = service.config.config.get('host'), service.config.config.get('apikey')
            res = requests.get(f"{host}/emby/Items?ParentId={item_data.get('Id')}&Fields=Path,Size,RunTimeTicks&api_key={apikey}", timeout=10)
            if res.status_code == 200:
                for song in res.json().get('Items', []):
                    self._send_single_audio_notify(song, album_name, (item_data.get('Artists') or ['未知歌手'])[0], item_data.get('Id'), item_data.get('PrimaryImageTag'), host)
        except: pass

    def _send_single_audio_notify(self, song, album, artist, cid, tag, host):
        texts = [f"⏰ 入库：{time.strftime('%H:%M:%S')}", f"👤 歌手：{artist}", f"💿 专辑：{album}", f"⏱️ 时长：{self._format_ticks(song.get('RunTimeTicks', 0))}"]
        img = f"{host}/emby/Items/{cid}/Images/Primary?maxHeight=450&maxWidth=450&tag={tag}&quality=90" if tag else None
        self.post_message(mtype=NotificationType.MediaServer, title=f"🎵 新入库媒体：{song.get('Name')}", text="\n".join(texts), image=img)

    def _append_meta_info(self, texts, tmdb_info):
        if tmdb_info and hasattr(tmdb_info, 'vote_average') and tmdb_info.vote_average: texts.append(f"⭐️ 评分：{round(float(tmdb_info.vote_average), 1)}")

    def _append_genres_actors(self, texts, tmdb_info):
        if tmdb_info and hasattr(tmdb_info, 'actors') and tmdb_info.actors:
            actors = [a.get('name') if isinstance(a, dict) else str(a) for a in tmdb_info.actors[:3]]
            if actors: texts.append(f"🎬 演员：{'、'.join(actors)}")

    def _append_season_episode_info(self, texts, event_info, series_name):
        if event_info.season_id is not None and event_info.episode_id is not None:
            texts.append(f"📺 季集：S{str(event_info.season_id).zfill(2)}E{str(event_info.episode_id).zfill(2)}")

    def _append_extra_info(self, texts, event_info):
        if event_info.user_name: texts.append(f"👤 用户：{event_info.user_name}")
        if event_info.device_name: texts.append(f"📱 设备：{event_info.device_name}")
        if event_info.percentage: texts.append(f"📊 进度：{round(float(event_info.percentage), 2)}%")

    def _get_play_link(self, event_info):
        if not self._add_play_link: return None
        service = self.service_info(event_info.server_name)
        return service.instance.get_play_url(event_info.item_id) if service else None

    def _format_ticks(self, ticks):
        s = ticks / 10000000
        return f"{int(s // 60)}:{int(s % 60):02d}"

    def _format_size(self, size):
        return f"{round(size / 1048576, 1)} MB"

    def _add_key_cache(self, key): self._webhook_msg_keys[key] = time.time() + self.DEFAULT_EXPIRATION_TIME
    def _remove_key_cache(self, key): self._webhook_msg_keys.pop(key, None)
    def _clean_expired_cache(self):
        cur = time.time()
        for k in [k for k, v in self._webhook_msg_keys.items() if v <= cur]: self._webhook_msg_keys.pop(k, None)

    def stop_service(self):
        for timer in self._aggregate_timers.values(): timer.cancel()
        self._aggregate_timers.clear()
        self._pending_messages.clear()
