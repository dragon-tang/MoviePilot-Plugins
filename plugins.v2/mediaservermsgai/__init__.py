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
    媒体服务器通知插件 AI增强版 (1.9.5 完整逻辑版)
    """

    # ==================== 常量定义 ====================
    DEFAULT_EXPIRATION_TIME = 600              
    DEFAULT_AGGREGATE_TIME = 15                
    DEFAULT_OVERVIEW_MAX_LENGTH = 150          
    IMAGE_CACHE_MAX_SIZE = 100                 

    # ==================== 插件基本信息 ====================
    plugin_name = "媒体库服务器通知AI版"
    plugin_desc = "基于Emby识别结果+TMDB元数据+微信清爽版(全消息类型+剧集聚合+排除库过滤)"
    plugin_icon = "mediaplay.png"
    plugin_version = "1.9.5"
    plugin_author = "jxxghp"
    author_url = "https://github.com/jxxghp"
    plugin_config_prefix = "mediaservermsgai_"
    plugin_order = 14
    auth_level = 1

    # ==================== 插件运行状态 ====================
    _enabled = False                           
    _add_play_link = False                     
    _mediaservers = None                       
    _types = []                                
    _webhook_msg_keys = {}                     
    _lock = threading.Lock()                   
    _last_event_cache: Tuple[Optional[Event], float] = (None, 0.0)  
    _image_cache = {}                          
    _overview_max_length = DEFAULT_OVERVIEW_MAX_LENGTH  
    _filter_unrecognized = True                
    _exclude_libs = []                         # 排除媒体库列表

    _aggregate_enabled = False                 
    _aggregate_time = DEFAULT_AGGREGATE_TIME   
    _pending_messages = {}                     
    _aggregate_timers = {}                     
    _smart_category_enabled = True             

    _webhook_actions = {
        "library.new": "已入库", "system.webhooktest": "测试", "system.notificationtest": "测试",
        "playback.start": "开始播放", "playback.stop": "停止播放", "playback.pause": "暂停播放",
        "playback.unpause": "继续播放", "user.authenticated": "登录成功", "user.authenticationfailed": "登录失败",
        "media.play": "开始播放", "media.stop": "停止播放", "media.pause": "暂停播放",
        "media.resume": "继续播放", "item.rate": "标记了", "item.markplayed": "标记已播放",
        "item.markunplayed": "标记未播放", "PlaybackStart": "开始播放", "PlaybackStop": "停止播放"
    }
    
    _webhook_images = {
        "emby": "https://raw.githubusercontent.com/qqcomeup/MoviePilot-Plugins/bb3ca257f74cf000640f9ebadab257bb0850baac/icons/11-11.jpg",
        "plex": "https://raw.githubusercontent.com/qqcomeup/MoviePilot-Plugins/bb3ca257f74cf000640f9ebadab257bb0850baac/icons/11-11.jpg",
        "jellyfin": "https://raw.githubusercontent.com/qqcomeup/MoviePilot-Plugins/bb3ca257f74cf000640f9ebadab257bb0850baac/icons/11-11.jpg"
    }

    _country_cn_map = {
        'CN': '中国大陆', 'US': '美国', 'JP': '日本', 'KR': '韩国', 'HK': '中国香港', 'TW': '中国台湾', 
        'GB': '英国', 'FR': '法国', 'DE': '德国', 'IT': '意大利', 'ES': '西班牙', 'IN': '印度',
        'TH': '泰国', 'RU': '俄罗斯', 'CA': '加拿大', 'AU': '澳大利亚', 'SG': '新加坡', 'MY': '马来西亚'
    }

    def __init__(self):
        super().__init__()
        self.category = CategoryHelper()

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._types = config.get("types") or []
            self._mediaservers = config.get("mediaservers") or []
            self._add_play_link = config.get("add_play_link", False)
            self._overview_max_length = int(config.get("overview_max_length") or self.DEFAULT_OVERVIEW_MAX_LENGTH)
            self._aggregate_enabled = config.get("aggregate_enabled", False)
            self._aggregate_time = int(config.get("aggregate_time") or self.DEFAULT_AGGREGATE_TIME)
            self._smart_category_enabled = config.get("smart_category_enabled", True)
            self._filter_unrecognized = config.get("filter_unrecognized", True)
            
            # 解析排除库
            exclude_str = config.get("exclude_libs") or ""
            self._exclude_libs = [s.strip() for s in re.split(r'[，,]', exclude_str) if s.strip()]
            logger.info(f"【mediaservermsgai】初始化。排除库清单: {self._exclude_libs}")

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        types_options = [
            {"title": "新入库", "value": "library.new"},
            {"title": "开始播放", "value": "playback.start|media.play|PlaybackStart"},
            {"title": "停止播放", "value": "playback.stop|media.stop|PlaybackStop"},
            {"title": "暂停/继续", "value": "playback.pause|playback.unpause|media.pause|media.resume"},
            {"title": "用户标记", "value": "item.rate|item.markplayed|item.markunplayed"},
            {"title": "登录提醒", "value": "user.authenticated|user.authenticationfailed"},
            {"title": "系统测试", "value": "system.webhooktest|system.notificationtest"},
        ]
        
        # 极稳获取服务器列表
        try:
            ms_items = [{"title": c.name, "value": c.name} for c in MediaServerHelper().get_configs().values()]
        except:
            ms_items = []

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
                    {'component': 'VRow', 'content': [{'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VSelect', 'props': {'multiple': True, 'chips': True, 'clearable': True, 'model': 'mediaservers', 'label': '媒体服务器', 'items': ms_items}}]}]},
                    {'component': 'VRow', 'content': [{'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VSelect', 'props': {'chips': True, 'multiple': True, 'model': 'types', 'label': '消息类型', 'items': types_options}}]}]},
                    {'component': 'VRow', 'content': [{'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextField', 'props': {'model': 'exclude_libs', 'label': '排除的媒体库', 'placeholder': '库名1,库名2', 'hint': '填入不希望进行 TMDB 识别的媒体库名称，多个用逗号分隔'}}]}]},
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'aggregate_enabled', 'label': '启用TV剧集聚合'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'smart_category_enabled', 'label': '启用智能分类'}}]}
                        ]
                    },
                    {'component': 'VRow', 'props': {'show': '{{aggregate_enabled}}'}, 'content': [{'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'aggregate_time', 'label': '聚合等待时间', 'type': 'number'}}]}]},
                    {'component': 'VRow', 'content': [{'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'filter_unrecognized', 'label': 'TMDB未识别不发送通知'}}]}]}
                ]
            }
        ], {
            "enabled": False, "types": [], "mediaservers": [], "add_play_link": False, "exclude_libs": "",
            "aggregate_enabled": False, "aggregate_time": 15, "smart_category_enabled": True, "filter_unrecognized": True
        }

    def _get_library_name(self, event_info: WebhookEventInfo) -> str:
        if not event_info or not event_info.json_object: return ""
        lib_name = event_info.json_object.get('Item', {}).get('LibraryName')
        if not lib_name:
            lib_name = event_info.json_object.get('Metadata', {}).get('librarySectionTitle')
        return lib_name or ""

    @eventmanager.register(EventType.WebhookMessage)
    def send(self, event: Event):
        try:
            if not self._enabled: return
            event_info: WebhookEventInfo = event.event_data
            if not event_info: return
            if not self._webhook_actions.get(event_info.event): return

            allowed_types = set()
            for _type in self._types: allowed_types.update(_type.split("|"))
            if event_info.event not in allowed_types: return

            if event_info.server_name and not self.service_info(name=event_info.server_name): return
            
            event_type = str(event_info.event).lower()
            curr_lib = self._get_library_name(event_info)
            is_excluded = curr_lib in self._exclude_libs

            # 未识别过滤
            if self._filter_unrecognized and not is_excluded:
                if event_info.item_type in ["MOV", "TV", "SHOW"]:
                    if event_type in ["library.new", "playback.start", "playback.stop", "media.play", "media.stop"]:
                        if not self._extract_tmdb_id(event_info):
                            logger.info(f"TMDB未识别视频，跳过通知: {event_info.item_name}")
                            return

            # 分发
            if "test" in event_type: self._handle_test_event(event_info); return
            if "user.authentic" in event_type: self._handle_login_event(event_info); return
            if "item." in event_type and ("rate" in event_type or "mark" in event_type): self._handle_rate_event(event_info); return
            
            if event_info.json_object and event_info.json_object.get('Item', {}).get('Type') == 'MusicAlbum' and event_type == 'library.new':
                self._handle_music_album(event_info, event_info.json_object.get('Item', {})); return

            if self._aggregate_enabled and not is_excluded and event_type == "library.new" and event_info.item_type in ["TV", "SHOW"]:
                series_id = self._get_series_id(event_info)
                if series_id: self._aggregate_tv_episodes(series_id, event_info, event); return

            self._process_media_event(event, event_info)
        except Exception as e: logger.error(f"Webhook异常: {str(e)}\n{traceback.format_exc()}")

    def _process_media_event(self, event: Event, event_info: WebhookEventInfo):
        """处理主函数"""
        try:
            self._clean_expired_cache()
            expiring_key = f"{event_info.item_id}-{event_info.client}-{event_info.user_name}-{event_info.event}"
            if str(event_info.event) == "playback.stop" and expiring_key in self._webhook_msg_keys: return
            
            with self._lock:
                current_time = time.time()
                last_event, last_time = self._last_event_cache
                if last_event and (current_time - last_time < 2) and last_event.event_id == event.event_id: return
                self._last_event_cache = (event, current_time)

            curr_lib = self._get_library_name(event_info)
            # 排除库检测：如果库名匹配，直接令 tmdb_id 为 None
            tmdb_id = None if curr_lib in self._exclude_libs else self._extract_tmdb_id(event_info)
            event_info.tmdb_id = tmdb_id
            
            message_texts = []
            message_title = ""
            image_url = event_info.image_url
            
            if event_info.item_type == "AUD":
                self._build_audio_message(event_info, message_texts)
                song_name = event_info.json_object.get('Item', {}).get('Name') if event_info.json_object else event_info.item_name
                message_title = f"{song_name} {self._webhook_actions.get(event_info.event, '通知')} {self._get_server_name_cn(event_info)}"
                img = self._get_audio_image_url(event_info.server_name, event_info.json_object.get('Item', {}))
                if img: image_url = img
            else:
                tmdb_info = None
                if tmdb_id:
                    mtype = MediaType.MOVIE if event_info.item_type == "MOV" else MediaType.TV
                    try: tmdb_info = self.chain.recognize_media(tmdbid=int(tmdb_id), mtype=mtype)
                    except: pass

                title_name = tmdb_info.title if (tmdb_info and tmdb_info.title) else event_info.item_name
                year = tmdb_info.year if (tmdb_info and tmdb_info.year) else (event_info.json_object.get('Item', {}).get('ProductionYear') if event_info.json_object else None)
                if year and str(year) not in title_name: title_name += f" ({year})"
                
                action = self._webhook_actions.get(event_info.event, "通知")
                if "library.new" in event_info.event: message_title = f"🆕 {title_name} 已入库"
                elif "play" in str(event_info.event).lower() or "start" in str(event_info.event).lower(): message_title = f"▶️ 开始播放：{title_name}"
                elif "stop" in str(event_info.event).lower(): message_title = f"⏹️ 停止播放：{title_name}"
                else: message_title = f"📢 {action}：{title_name}"
                
                message_texts.append(f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
                if curr_lib: message_texts.append(f"📁 媒体库：{curr_lib}")

                category = None
                if self._smart_category_enabled and tmdb_info:
                    try: category = self.category.get_movie_category(tmdb_info) if event_info.item_type == "MOV" else self.category.get_tv_category(tmdb_info)
                    except: pass
                if not category: category = self._get_category_from_path(event_info.item_path, event_info.item_type, event_info.json_object.get('Item', {}).get('IsFolder', False) if event_info.json_object else False)
                if category: message_texts.append(f"📂 分类：{category}")

                self._append_season_episode_info(message_texts, event_info, title_name)
                self._append_meta_info(message_texts, tmdb_info)
                self._append_genres_actors(message_texts, tmdb_info)

                overview = tmdb_info.overview if (tmdb_info and tmdb_info.overview) else event_info.overview
                if overview and "library.new" in event_info.event:
                    if len(overview) > self._overview_max_length: overview = overview[:self._overview_max_length] + "..."
                    message_texts.append(f"📖 简介：\n{overview}")

                if not image_url and tmdb_id:
                    image_url = self._get_tmdb_image(event_info, MediaType.MOVIE if event_info.item_type == "MOV" else MediaType.TV)

            self._append_extra_info(message_texts, event_info)
            if str(event_info.event) == "playback.stop": self._add_key_cache(expiring_key)
            if str(event_info.event) == "playback.start": self._remove_key_cache(expiring_key)

            self.post_message(mtype=NotificationType.MediaServer, title=message_title, text="\n".join(message_texts), image=image_url or self._webhook_images.get(event_info.channel), link=self._get_play_link(event_info))
        except Exception as e: logger.error(f"处理媒体事件异常: {str(e)}\n{traceback.format_exc()}")

    # ==================== 原版辅助函数全量补回 ====================
    def _handle_test_event(self, event_info):
        self.post_message(mtype=NotificationType.MediaServer, title="🔔 媒体服务器通知测试", text=f"来自：{self._get_server_name_cn(event_info)}\n状态：正常", image=self._webhook_images.get(event_info.channel))

    def _handle_login_event(self, event_info):
        action = "登录成功" if "authenticated" in event_info.event and "failed" not in event_info.event else "登录失败"
        texts = [f"👤 用户：{event_info.user_name}", f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"]
        if event_info.device_name: texts.append(f"📱 设备：{event_info.client} {event_info.device_name}")
        self.post_message(mtype=NotificationType.MediaServer, title=f"🔐 {action}提醒", text="\n".join(texts), image=self._webhook_images.get(event_info.channel))

    def _handle_rate_event(self, event_info):
        title = f"⭐ 用户评分：{event_info.item_name}"
        texts = [f"👤 用户：{event_info.user_name}", f"🏷️ 标记：{self._webhook_actions.get(event_info.event, '已标记')}"]
        self.post_message(mtype=NotificationType.MediaServer, title=title, text="\n".join(texts), image=self._webhook_images.get(event_info.channel))

    def _build_audio_message(self, event_info, texts):
        item = event_info.json_object.get('Item', {}) if event_info.json_object else {}
        texts.append(f"👤 歌手：{(item.get('Artists') or ['未知歌手'])[0]}")
        if item.get('Album'): texts.append(f"💿 专辑：{item.get('Album')}")
        texts.append(f"⏱️ 时长：{self._format_ticks(item.get('RunTimeTicks', 0))}")

    def _get_series_id(self, event_info):
        if event_info.json_object: return event_info.json_object.get("Item", {}).get("SeriesId") or event_info.json_object.get("Item", {}).get("SeriesName")
        return getattr(event_info, "series_id", None)

    def _aggregate_tv_episodes(self, series_id, event_info, event):
        with self._lock:
            if series_id not in self._pending_messages: self._pending_messages[series_id] = []
            self._pending_messages[series_id].append((event_info, event))
            if series_id in self._aggregate_timers: self._aggregate_timers[series_id].cancel()
            t = threading.Timer(self._aggregate_time, self._send_aggregated_message, [series_id])
            self._aggregate_timers[series_id] = t
            t.start()

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
        title = first_info.json_object.get('Item', {}).get('SeriesName') if first_info.json_object else first_info.item_name
        message_texts = [f"⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}", f"📺 季集：{self._merge_continuous_episodes([x[0] for x in msg_list])}"]
        self._append_meta_info(message_texts, tmdb_info)
        self.post_message(mtype=NotificationType.MediaServer, title=f"🆕 {title} 已入库 (含{len(msg_list)}个文件)", text="\n".join(message_texts), image=self._get_tmdb_image(first_info, MediaType.TV) or self._webhook_images.get(first_info.channel))

    def _merge_continuous_episodes(self, events):
        """核心算法：合并连续集数"""
        season_episodes = {}
        for event in events:
            if event.json_object:
                item = event.json_object.get("Item", {})
                s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
                if s is not None and e is not None:
                    if s not in season_episodes: season_episodes[s] = []
                    season_episodes[s].append(int(e))
        merged = []
        for s in sorted(season_episodes.keys()):
            eps = sorted(list(set(season_episodes[s])))
            if not eps: continue
            start = eps[0]
            end = eps[0]
            for i in range(1, len(eps)):
                if eps[i] == end + 1: end = eps[i]
                else:
                    merged.append(f"S{str(s).zfill(2)}E{str(start).zfill(2)}-E{str(end).zfill(2)}" if start != end else f"S{str(s).zfill(2)}E{str(start).zfill(2)}")
                    start = end = eps[i]
            merged.append(f"S{str(s).zfill(2)}E{str(start).zfill(2)}-E{str(end).zfill(2)}" if start != end else f"S{str(s).zfill(2)}E{str(start).zfill(2)}")
        return ", ".join(merged)

    def _extract_tmdb_id(self, event_info: WebhookEventInfo):
        tmdb_id = event_info.tmdb_id
        if not tmdb_id and event_info.json_object:
            tmdb_id = event_info.json_object.get('Item', {}).get('ProviderIds', {}).get('Tmdb')
        if not tmdb_id and event_info.item_path:
            match = re.search(r'tmdb[=-](\d+)', event_info.item_path, re.IGNORECASE)
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

    def service_infos(self, type_filter=None):
        try:
            services = MediaServerHelper().get_services(type_filter=type_filter, name_filters=self._mediaservers)
            return {n: s for n, s in services.items() if not s.instance.is_inactive()} if services else None
        except: return None

    def service_info(self, name):
        s = self.service_infos()
        return s.get(name) if s else None

    def stop_service(self):
        for t in self._aggregate_timers.values(): t.cancel()
        self._aggregate_timers.clear()
        self._pending_messages.clear()
