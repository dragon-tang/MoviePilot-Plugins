import time
import random
import requests
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import urllib3
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.log import logger
from app.core.config import settings
from app.schemas import NotificationType

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class HdhiveSign(_PluginBase):
    """
    影巢(HDHive)多账号签到插件
    - 无 UI
    - 无可配置项
    - 并发 + 随机错峰
    """

    plugin_name = "影巢签到"
    plugin_desc = "影巢(HDHive)多账号自动签到（并发 + 错峰）"
    plugin_icon = "https://raw.githubusercontent.com/madrays/MoviePilot-Plugins/main/icons/hdhive.ico"
    plugin_version = "3.0.0"
    plugin_author = "madrays + ChatGPT"
    plugin_order = 1
    auth_level = 2

    # =========================
    # 固定参数（无配置）
    # =========================
    _base_url = "https://hdhive.com"
    _signin_api = "/api/customer/user/checkin"

    # 随机错峰范围（秒）
    _delay_min = 5
    _delay_max = 20

    # 最大并发
    _max_workers = 5

    # =========================
    # 多账号配置（写在这里）
    # =========================
    _accounts: List[Dict[str, str]] = [
        {
            "name": "主号",
            "cookie": "token=xxxx; csrf_access_token=xxxx",
            "username": "",
            "password": ""
        },
        {
            "name": "小号",
            "cookie": "",
            "username": "email@example.com",
            "password": "password"
        }
    ]

    # =========================
    # 插件初始化（无需配置）
    # =========================
    def init_plugin(self, config: dict = None):
        logger.info(f"影巢签到插件初始化，账号数：{len(self._accounts)}")

    # =========================
    # 定时服务
    # =========================
    def get_service(self):
        return [{
            "id": "hdhive_sign_multi",
            "name": "影巢多账号签到",
            "trigger": CronTrigger(hour=9, minute=0),
            "func": self.sign,
            "kwargs": {}
        }]

    # =========================
    # 并发签到入口
    # =========================
    def sign(self):
        if not self._accounts:
            logger.warning("未配置任何影巢账号")
            return []

        workers = min(len(self._accounts), self._max_workers)
        results = []

        logger.info(f"影巢签到开始，并发数：{workers}")

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(self._sign_single_account, acc): acc
                for acc in self._accounts
            }

            for future in as_completed(future_map):
                acc = future_map[future]
                name = acc.get("name", "未命名账号")
                try:
                    results.append(future.result())
                except Exception as e:
                    logger.error(f"{name} 签到异常: {e}", exc_info=True)
                    results.append(self._result(name, False, str(e)))

        self._send_summary_notification(results)
        return results

    # =========================
    # 单账号签到（线程安全 + 错峰）
    # =========================
    def _sign_single_account(self, account: dict):
        name = account.get("name", "未命名账号")

        delay = random.randint(self._delay_min, self._delay_max)
        logger.info(f"[{name}] 随机延迟 {delay}s 后开始签到")
        time.sleep(delay)

        cookie = account.get("cookie", "")
        username = account.get("username", "")
        password = account.get("password", "")

        ok, msg = self._signin(cookie, username, password)
        return self._result(name, ok, msg)

    # =========================
    # 实际签到逻辑
    # =========================
    def _signin(self, cookie: str, username: str, password: str) -> Tuple[bool, str]:
        if not cookie:
            return False, "未提供 Cookie（自动登录未实现）"

        cookies = self._parse_cookie(cookie)
        token = cookies.get("token")
        if not token:
            return False, "Cookie 缺少 token"

        headers = {
            "User-Agent": settings.USER_AGENT,
            "Authorization": f"Bearer {token}",
            "Origin": self._base_url,
            "Referer": self._base_url
        }

        r = requests.post(
            self._base_url + self._signin_api,
            headers=headers,
            cookies=cookies,
            proxies=settings.PROXY,
            timeout=30,
            verify=False
        )

        data = r.json()
        msg = data.get("message", "未知")

        ok = data.get("success") or "签到" in msg
        return ok, msg

    # =========================
    # 工具函数
    # =========================
    def _parse_cookie(self, cookie: str) -> Dict[str, str]:
        return dict(
            part.strip().split("=", 1)
            for part in cookie.split(";")
            if "=" in part
        )

    def _result(self, name, ok, msg):
        return {
            "account": name,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "签到成功" if ok else "签到失败",
            "message": msg
        }

    # =========================
    # 汇总通知
    # =========================
    def _send_summary_notification(self, results: list):
        success = sum(1 for r in results if "成功" in r["status"])
        fail = len(results) - success

        text = (
            f"📊 影巢签到汇总\n"
            f"━━━━━━━━━━━━\n"
            f"✅ 成功：{success}\n"
            f"❌ 失败：{fail}\n\n"
        )

        for r in results:
            icon = "✅" if "成功" in r["status"] else "❌"
            text += f"{icon} {r['account']}：{r['message']}\n"

        self.post_message(
            mtype=NotificationType.SiteMessage,
            title="【影巢签到汇总】",
            text=text
        )
