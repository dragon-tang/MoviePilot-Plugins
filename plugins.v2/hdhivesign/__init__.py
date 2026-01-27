import json
import requests
from datetime import datetime
from typing import Any, List, Dict, Tuple, Optional

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.core.config import settings
from app.schemas import NotificationType
from app.log import logger

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class HdhiveSign(_PluginBase):
    # ===== 插件信息 =====
    plugin_name = "影巢签到"
    plugin_desc = "影巢(HDHive)多账号自动签到"
    plugin_version = "2.1.0"
    plugin_author = "madrays"
    plugin_config_prefix = "hdhivesign_"
    plugin_order = 1
    auth_level = 2

    # ===== 配置 =====
    _enabled = False
    _notify = True
    _cron = None
    _base_url = "https://hdhive.com"
    _accounts: List[Dict[str, str]] = []

    # =========================
    # 插件初始化
    # =========================
    def init_plugin(self, config: dict = None):
        if not config:
            return

        self._enabled = config.get("enabled", False)
        self._notify = config.get("notify", True)
        self._cron = config.get("cron")
        self._base_url = (config.get("base_url") or self._base_url).rstrip("/")

        accounts = config.get("accounts") or []

        # Textarea 会传字符串，需解析
        if isinstance(accounts, str):
            try:
                accounts = json.loads(accounts)
            except Exception as e:
                logger.error("影巢签到：账号 JSON 解析失败")
                accounts = []

        if not isinstance(accounts, list):
            accounts = []

        self._accounts = accounts

    # =========================
    # 插件状态（必须）
    # =========================
    def get_state(self) -> bool:
        return self._enabled

    # =========================
    # 定时服务（必须）
    # =========================
    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []

        return [{
            "id": "hdhivesign_multi",
            "name": "影巢多账号签到",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self.sign_all_accounts,
            "kwargs": {}
        }]

    # =========================
    # 多账号签到
    # =========================
    def sign_all_accounts(self):
        for idx, account in enumerate(self._accounts, start=1):
            try:
                self._sign_single(account, idx)
            except Exception as e:
                logger.error(f"影巢签到异常: {e}", exc_info=True)

    def _sign_single(self, account: dict, idx: int):
        name = account.get("name") or f"账号{idx}"
        cookie = account.get("cookie")

        if not cookie:
            self._notify_msg(name, "❌ 未配置 Cookie")
            return

        cookies = self._parse_cookie(cookie)
        token = cookies.get("token")

        if not token:
            self._notify_msg(name, "❌ Cookie 中缺少 token")
            return

        headers = {
            "User-Agent": settings.USER_AGENT,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }

        url = f"{self._base_url}/api/customer/user/checkin"

        r = requests.post(
            url=url,
            headers=headers,
            cookies=cookies,
            proxies=settings.PROXY,
            timeout=20,
            verify=False
        )

        try:
            data = r.json()
        except Exception:
            data = {}

        msg = data.get("message", "未知返回")
        success = data.get("success") or "已签到" in msg

        status = "✅ 签到成功" if success else "❌ 签到失败"

        self._save_history(name, status, msg)
        self._notify_msg(name, f"{status}\n{msg}")

    # =========================
    # 历史记录
    # =========================
    def _save_history(self, name: str, status: str, msg: str):
        key = f"hdhive_history_{name}"
        history = self.get_data(key) or []

        history.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": status,
            "message": msg
        })

        self.save_data(key, history)

    # =========================
    # 工具方法
    # =========================
    def _parse_cookie(self, cookie_str: str) -> Dict[str, str]:
        cookies = {}
        for part in cookie_str.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                cookies[k] = v
        return cookies

    def _notify_msg(self, name: str, text: str):
        if not self._notify:
            return

        self.post_message(
            mtype=NotificationType.SiteMessage,
            title=f"【影巢签到】{name}",
            text=text
        )

    # =========================
    # 插件配置页（必须）
    # =========================
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {"model": "enabled", "label": "启用插件"}
                    },
                    {
                        "component": "VSwitch",
                        "props": {"model": "notify", "label": "开启通知"}
                    },
                    {
                        "component": "VCronField",
                        "props": {"model": "cron", "label": "签到周期"}
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "base_url",
                            "label": "站点地址",
                            "placeholder": "https://hdhive.com"
                        }
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": (
                                "多账号配置（JSON 格式）示例：\n\n"
                                "[\n"
                                "  {\n"
                                "    \"name\": \"主账号\",\n"
                                "    \"cookie\": \"token=xxx; csrf_access_token=yyy\"\n"
                                "  },\n"
                                "  {\n"
                                "    \"name\": \"小号\",\n"
                                "    \"cookie\": \"token=aaa; csrf_access_token=bbb\"\n"
                                "  }\n"
                                "]"
                            )
                        }
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "accounts",
                            "label": "账号配置（JSON）",
                            "rows": 10
                        }
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "cron": "0 8 * * *",
            "base_url": "https://hdhive.com",
            "accounts": json.dumps(
                [
                    {"name": "主账号", "cookie": ""}
                ],
                ensure_ascii=False,
                indent=2
            )
        }

    # =========================
    # 插件页面（必须）
    # =========================
    def get_page(self) -> List[dict]:
        pages = []
        for account in self._accounts:
            name = account.get("name")
            history = self.get_data(f"hdhive_history_{name}") or []

            pages.append({
                "component": "VCard",
                "content": [
                    {"component": "VCardTitle", "text": f"📌 {name}"},
                    {
                        "component": "VCardText",
                        "text": f"历史签到记录：{len(history)} 条"
                    }
                ]
            })
        return pages

    # =========================
    # API（必须）
    # =========================
    def get_api(self) -> List[Dict[str, Any]]:
        return []
