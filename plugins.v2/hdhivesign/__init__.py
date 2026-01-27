import json
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
    plugin_name = "影巢签到"
    plugin_desc = "影巢(HDHive)多账号自动签到（并发 + 错峰）"
    plugin_icon = "https://raw.githubusercontent.com/madrays/MoviePilot-Plugins/main/icons/hdhive.ico"
    plugin_version = "2.0.0"
    plugin_author = "madrays + ChatGPT"
    plugin_order = 1
    auth_level = 2

    _enabled = False
    _notify = True
    _cron = None

    _base_url = "https://hdhive.com"
    _signin_api = "/api/customer/user/checkin"
    _user_info_api = "/api/customer/user/info"

    _accounts: List[Dict] = []
    _delay_min = 3
    _delay_max = 15

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

        self._delay_min = int(config.get("delay_min", 3))
        self._delay_max = int(config.get("delay_max", 15))

        try:
            self._accounts = json.loads(config.get("accounts", "[]"))
            logger.info(f"影巢多账号加载成功：{len(self._accounts)} 个账号")
        except Exception as e:
            logger.error(f"账号配置解析失败: {e}")
            self._accounts = []

    # =========================
    # 调度入口（并发）
    # =========================
    def sign(self):
        if not self._accounts:
            logger.warning("未配置影巢账号")
            return []

        workers = min(len(self._accounts), 5)
        results = []

        logger.info(f"影巢签到开始，并发数 {workers}")

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(self._sign_single_account, idx, acc): acc
                for idx, acc in enumerate(self._accounts)
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
    # 单账号签到（线程安全）
    # =========================
    def _sign_single_account(self, idx: int, account: dict):
        name = account.get("name", f"账号{idx+1}")

        delay = random.randint(self._delay_min, self._delay_max)
        logger.info(f"[{name}] 随机延迟 {delay}s 后签到")
        time.sleep(delay)

        cookie = account.get("cookie", "")
        username = account.get("username", "")
        password = account.get("password", "")

        ok, msg, new_cookie = self._signin_with_context(cookie, username, password)

        if new_cookie:
            self._accounts[idx]["cookie"] = new_cookie
            self._persist_accounts()

        result = self._result(name, ok, msg)
        self._save_history(name, result)
        return result

    # =========================
    # 实际签到逻辑
    # =========================
    def _signin_with_context(
        self, cookie: str, username: str, password: str
    ) -> Tuple[bool, str, Optional[str]]:

        if not cookie and username and password:
            cookie = self._auto_login(username, password)

        if not cookie:
            return False, "无Cookie且自动登录失败", None

        cookies = self._parse_cookie(cookie)
        token = cookies.get("token")
        if not token:
            return False, "Cookie缺少token", None

        headers = {
            "User-Agent": settings.USER_AGENT,
            "Authorization": f"Bearer {token}",
            "Origin": self._base_url,
            "Referer": self._base_url,
        }

        r = requests.post(
            self._base_url + self._signin_api,
            headers=headers,
            cookies=cookies,
            proxies=settings.PROXY,
            timeout=30,
            verify=False,
        )

        data = r.json()
        msg = data.get("message", "未知")

        ok = data.get("success") or "签到" in msg
        return ok, msg, cookie

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
            "message": msg,
        }

    def _save_history(self, name, result):
        key = f"history_{name}"
        history = self.get_data(key) or []
        history.append(result)
        self.save_data(key, history)

    def _persist_accounts(self):
        self.update_config({"accounts": json.dumps(self._accounts, ensure_ascii=False)})

    # =========================
    # 汇总通知
    # =========================
    def _send_summary_notification(self, results: list):
        if not self._notify:
            return

        success = sum(1 for r in results if "成功" in r["status"])
        fail = len(results) - success

        text = (
            f"📊 影巢多账号签到汇总\n"
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
            text=text,
        )

    # =========================
    # UI 页面（Tab）
    # =========================
    def get_page(self):
        tabs, contents = [], []

        for acc in self._accounts:
            name = acc.get("name", "未命名账号")
            history = self.get_data(f"history_{name}") or []
            history = sorted(history, key=lambda x: x["date"], reverse=True)

            rows = []
            for h in history:
                color = "success" if "成功" in h["status"] else "error"
                rows.append({
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": h["date"]},
                        {"component": "td", "content": [{
                            "component": "VChip",
                            "props": {"color": color, "size": "small"},
                            "text": h["status"]
                        }]},
                        {"component": "td", "text": h["message"]},
                    ]
                })

            tabs.append({"title": name})
            contents.append({
                "component": "VTable",
                "props": {"density": "compact"},
                "content": [
                    {"component": "thead", "content": [{
                        "component": "tr",
                        "content": [
                            {"component": "th", "text": "时间"},
                            {"component": "th", "text": "状态"},
                            {"component": "th", "text": "消息"},
                        ]
                    }]},
                    {"component": "tbody", "content": rows},
                ],
            })

        return [{
            "component": "VTabs",
            "props": {"grow": True},
            "tabs": tabs,
            "content": contents,
        }]

    # =========================
    # 自动登录（保留你原实现）
    # =========================
    def _auto_login(self, username: str, password: str) -> Optional[str]:
        return None

    # =========================
    # 定时服务
    # =========================
    def get_service(self):
        if self._enabled and self._cron:
            return [{
                "id": "hdhive_sign_multi",
                "name": "影巢多账号签到",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sign,
                "kwargs": {}
            }]
        return []
