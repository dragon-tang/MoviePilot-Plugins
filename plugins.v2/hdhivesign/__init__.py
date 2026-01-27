# =========================
# 影巢签到插件（多账号版）
# =========================
import time
import requests
import re
import json
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional

import jwt
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.plugins import _PluginBase
from app.log import logger
from app.schemas import NotificationType

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class HdhiveSign(_PluginBase):
    plugin_name = "影巢签到AI版"
    plugin_desc = "影巢(HDHive)多账号自动签到"
    plugin_version = "2.0.0"
    plugin_author = "madrays"
    plugin_config_prefix = "hdhivesign_"
    plugin_order = 1
    auth_level = 2

    _enabled = False
    _notify = False
    _cron = None
    _onlyonce = False
    _accounts: List[Dict[str, str]] = []

    _scheduler: Optional[BackgroundScheduler] = None
    _current_trigger_type = None

    _base_url = "https://hdhive.com"

    def init_plugin(self, config: dict = None):
        self.stop_service()
        logger.info("===== HDHive 多账号签到初始化 =====")

        if not config:
            return

        self._enabled = config.get("enabled", False)
        self._notify = config.get("notify", True)
        self._cron = config.get("cron")
        self._onlyonce = config.get("onlyonce", False)
        self._base_url = (config.get("base_url") or self._base_url).rstrip("/")

        # ===== 多账号配置 =====
        accounts = config.get("accounts") or []

        # 兼容旧版单账号
        if not accounts and config.get("cookie"):
            accounts = [{
                "name": "默认账号",
                "cookie": config.get("cookie"),
                "username": config.get("username"),
                "password": config.get("password"),
            }]

        self._accounts = accounts

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.sign_all_accounts,
                trigger="date",
                run_date=datetime.now(pytz.timezone(settings.TZ)) + timedelta(seconds=3)
            )
            self._scheduler.start()

    # ======================
    # 核心：多账号循环入口
    # ======================
    def sign_all_accounts(self):
        self._current_trigger_type = "手动触发" if self._onlyonce else "定时触发"

        for idx, account in enumerate(self._accounts, start=1):
            try:
                logger.info(f"▶ 开始签到账号 [{account.get('name') or idx}]")
                self._sign_single_account(account, idx)
            except Exception as e:
                logger.error(f"账号[{account.get('name')}] 签到异常: {e}", exc_info=True)

    # ======================
    # 单账号签到逻辑
    # ======================
    def _sign_single_account(self, account: dict, index: int):
        name = account.get("name") or f"账号{index}"
        cookie = account.get("cookie")

        if not cookie:
            logger.warning(f"[{name}] 未配置 Cookie，跳过")
            return

        cookies = self._parse_cookie(cookie)
        token = cookies.get("token")

        if not token:
            self._notify_fail(name, "Cookie 中缺少 token")
            return

        headers = {
            "User-Agent": settings.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Bearer {token}",
            "Origin": self._base_url,
            "Referer": self._base_url,
        }

        url = f"{self._base_url}/api/customer/user/checkin"

        resp = requests.post(
            url=url,
            headers=headers,
            cookies=cookies,
            proxies=settings.PROXY,
            timeout=30,
            verify=False
        )

        data = {}
        try:
            data = resp.json()
        except Exception:
            pass

        success = data.get("success")
        message = data.get("message", "未知返回")

        if success or "已签到" in message:
            self._save_success(name, message)
        else:
            self._notify_fail(name, message)

    # ======================
    # 工具方法
    # ======================
    def _parse_cookie(self, cookie_str: str) -> Dict[str, str]:
        cookies = {}
        for part in cookie_str.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                cookies[k] = v
        return cookies

    def _save_success(self, name: str, message: str):
        key = f"sign_history_{name}"
        history = self.get_data(key) or []

        history.append({
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "签到成功" if "已" not in message else "已签到",
            "message": message
        })

        self.save_data(key, history)

        if self._notify:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title=f"【✅ 影巢签到成功】{name}",
                text=f"{message}"
            )

    def _notify_fail(self, name: str, msg: str):
        if self._notify:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title=f"【❌ 影巢签到失败】{name}",
                text=msg
            )

    # ======================
    # 定时服务
    # ======================
    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "hdhivesign_multi",
                "name": "影巢多账号签到",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sign_all_accounts,
                "kwargs": {}
            }]
        return []

    def stop_service(self):
        if self._scheduler:
            self._scheduler.shutdown()
            self._scheduler = None
