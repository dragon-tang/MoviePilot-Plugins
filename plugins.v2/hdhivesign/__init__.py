import json
import requests
from datetime import datetime
from typing import Any, List, Dict

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.core.config import settings
from app.schemas import NotificationType
from app.log import logger

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class HdhiveSign(_PluginBase):
    # ===== 插件信息 =====
    plugin_name = "影巢签到AI版"
    plugin_desc = "影巢(HDHive)多账号自动签到"
    plugin_version = "2.2.0"
    plugin_author = "madrays + fixed-form"
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
    # 初始化
    # =========================
    def init_plugin(self, config: dict = None):
        if not config:
            return

        self._enabled = config.get("enabled", False)
        self._notify = config.get("notify", True)
        self._cron = config.get("cron")
        self._base_url = (config.get("base_url") or self._base_url).rstrip("/")

        accounts = config.get("accounts") or "[]"

        if isinstance(accounts, str):
            try:
                accounts = json.loads(accounts)
            except Exception:
                logger.error("影巢签到：账号 JSON 解析失败")
                accounts = []

        if not isinstance(accounts, list):
            accounts = []

        self._accounts = accounts

    # =========================
    # 插件状态
    # =========================
    def get_state(self) -> bool:
        return self._enabled

    # =========================
    # 定时服务
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
    # 签到逻辑
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
            self._notify(name, "❌ 未配置 Cookie")
            return

        cookies = self._parse_cookie(cookie)
        token = cookies.get("token")

        if not token:
            self._notify(name, "❌ Cookie 缺少 token")
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
        self._notify(name, f"{status}\n{msg}")

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

    def _notify(self, name: str, text: str):
        if not self._notify:
            return

        self.post_message(
            mtype=NotificationType.SiteMessage,
            title=f"【影巢签到】{name}",
            text=text
        )

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
    # ✅ 关键修复：配置表单（新规范）
    # =========================
    def get_form(self) -> Dict[str, Any]:
        return {
            "form": [
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
                            "  {\"name\": \"主账号\", \"cookie\": \"token=xxx\"},\n"
                            "  {\"name\": \"小号\", \"cookie\": \"token=yyy\"}\n"
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
            ],
            "data": {
                "enabled": False,
                "notify": True,
                "cron": "0 8 * * *",
                "base_url": "https://hdhive.com",
                "accounts": json.dumps(
                    [{"name": "主账号", "cookie": ""}],
                    ensure_ascii=False,
                    indent=2
                )
            }
        }

    # =========================
    # 插件页面
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
