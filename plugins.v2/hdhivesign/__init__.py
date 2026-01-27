"""
影巢签到插件
版本: 2.0.0
作者: madrays
功能:
- 自动完成影巢(HDHive)每日签到
- 支持多账户配置和轮流签到
- 支持签到失败重试
- 保存签到历史记录
- 提供详细的签到通知
- 默认使用代理访问

修改记录:
- v2.0.0: AI添加多账户支持，每个账户独立配置和记录
- v1.3.0: 域名改为可配置，统一API拼接(Referer/Origin/接口)，精简日志
- v1.2.0: 增加自动登录功能
- v1.1.0: 修复签到历史显示和通知格式
- v1.0.0: 初始版本，基于影巢网站结构实现自动签到
"""
import time
import requests
import re
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple, Optional

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
    # 插件名称
    plugin_name = "影巢签到AI版"
    # 插件描述
    plugin_desc = "自动完成影巢(HDHive)每日签到，支持多账户和失败重试"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/madrays/MoviePilot-Plugins/main/icons/hdhive.ico"
    # 插件版本
    plugin_version = "2.0.0"
    # 插件作者
    plugin_author = "madrays"
    # 作者主页
    author_url = "https://github.com/madrays"
    # 插件配置项ID前缀
    plugin_config_prefix = "hdhivesign_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    _accounts = []
    _notify = False
    _onlyonce = False
    _cron = None
    _max_retries = 3  # 最大重试次数
    _retry_interval = 30  # 重试间隔(秒)
    _history_days = 30  # 历史保留天数
    _manual_trigger = False
    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    _current_trigger_type = None  # 保存当前执行的触发类型
    
    # 影巢站点配置（域名可配置）
    _base_url = "https://hdhive.com"
    _site_url = f"{_base_url}/"
    _signin_api = f"{_base_url}/api/customer/user/checkin"
    _user_info_api = f"{self._base_url}/api/customer/user/info"
    _login_api_candidates = [
        "/api/customer/user/login",
        "/api/customer/auth/login",
    ]
    _login_page = "/login"

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        logger.info("============= hdhivesign 初始化 =============")
        try:
            if config:
                self._enabled = config.get("enabled")
                self._accounts = self._parse_accounts(config.get("accounts", ""))
                self._notify = config.get("notify")
                self._cron = config.get("cron")
                self._onlyonce = config.get("onlyonce")
                # 新增：站点地址配置
                self._base_url = (config.get("base_url") or self._base_url or "").rstrip("/") or "https://hdhive.com"
                # 基于 base_url 统一构建接口地址
                self._site_url = f"{self._base_url}/"
                self._signin_api = f"{self._base_url}/api/customer/user/checkin"
                self._user_info_api = f"{self._base_url}/api/customer/user/info"
                self._max_retries = int(config.get("max_retries", 3))
                self._retry_interval = int(config.get("retry_interval", 30))
                self._history_days = int(config.get("history_days", 30))
                logger.info(f"影巢签到插件已加载，账户数量：{len(self._accounts)}")
            
            # 清理所有可能的延长重试任务
            self._clear_extended_retry_tasks()
            
            if self._onlyonce:
                logger.info("执行一次性签到")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._manual_trigger = True
                self._scheduler.add_job(func=self.sign, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="影巢签到")
                self._onlyonce = False
                self.update_config({
                    "onlyonce": False,
                    "enabled": self._enabled,
                    "accounts": config.get("accounts", "") if config else "",
                    "notify": self._notify,
                    "cron": self._cron,
                    "base_url": self._base_url,
                    "max_retries": self._max_retries,
                    "retry_interval": self._retry_interval,
                    "history_days": self._history_days
                })

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

        except Exception as e:
            logger.error(f"hdhivesign初始化错误: {str(e)}", exc_info=True)

    def _parse_accounts(self, accounts_str: str) -> List[Dict]:
        """解析账户配置字符串为账户列表"""
        accounts = []
        if not accounts_str:
            return accounts
        
        # 支持多种分隔符：换行、分号、逗号
        lines = accounts_str.replace(';', '\n').replace(',', '\n').split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # 解析格式：cookie|username|password|alias
            parts = line.split('|')
            if len(parts) >= 3:
                cookie = parts[0].strip()
                username = parts[1].strip()
                password = parts[2].strip()
                alias = parts[3].strip() if len(parts) >= 4 else username  # 默认使用用户名作为别名
                
                account = {
                    "cookie": cookie,
                    "username": username,
                    "password": password,
                    "alias": alias,
                    "enabled": bool(cookie or (username and password))
                }
                accounts.append(account)
            elif len(parts) == 1 and parts[0].strip():  # 只配置了cookie的情况
                cookie = parts[0].strip()
                account = {
                    "cookie": cookie,
                    "username": "",
                    "password": "",
                    "alias": "账户" + str(len(accounts) + 1),
                    "enabled": bool(cookie)
                }
                accounts.append(account)
        
        return accounts

    def _format_accounts(self, accounts: List[Dict]) -> str:
        """将账户列表格式化为字符串"""
        lines = []
        for account in accounts:
            line = f"{account.get('cookie', '')}|{account.get('username', '')}|{account.get('password', '')}|{account.get('alias', '')}"
            lines.append(line)
        return "\n".join(lines)

    def sign(self, retry_count=0, extended_retry=0):
        """
        执行所有账户的签到，支持失败重试。
        参数：
            retry_count: 常规重试计数
            extended_retry: 延长重试计数（0=首次尝试, 1=第一次延长重试, 2=第二次延长重试）
        """
        # 设置执行超时保护
        start_time = datetime.now()
        sign_timeout = 300  # 限制签到执行最长时间为5分钟
        
        # 保存当前执行的触发类型
        self._current_trigger_type = "手动触发" if self._is_manual_trigger() else "定时触发"
        
        # 如果是定时任务且不是重试，检查是否有正在运行的延长重试任务
        if retry_count == 0 and extended_retry == 0 and not self._is_manual_trigger():
            if self._has_running_extended_retry():
                logger.warning("检测到有正在运行的延长重试任务，跳过本次执行")
                return
        
        logger.info(f"开始影巢签到，账户数量：{len(self._accounts)}")
        
        results = []
        for idx, account in enumerate(self._accounts, 1):
            if not account.get("enabled", True):
                logger.info(f"跳过禁用账户：{account.get('alias', f'账户{idx}')}")
                continue
                
            logger.info(f"开始处理第 {idx}/{len(self._accounts)} 个账户：{account.get('alias', f'账户{idx}')}")
            result = self._sign_account(account, idx, retry_count, extended_retry)
            results.append(result)
            
            # 账户之间短暂间隔，避免请求过快
            if idx < len(self._accounts):
                time.sleep(2)
        
        # 发送汇总通知
        if self._notify and results:
            self._send_summary_notification(results)
        
        return results

    def _sign_account(self, account: Dict, account_idx: int, retry_count=0, extended_retry=0) -> Dict:
        """
        执行单个账户的签到
        """
        alias = account.get("alias", f"账户{account_idx}")
        cookie = account.get("cookie", "")
        username = account.get("username", "")
        password = account.get("password", "")
        
        logger.info(f"执行账户签到: {alias}")
        logger.debug(f"参数: retry={retry_count}, ext_retry={extended_retry}, trigger={self._current_trigger_type}")

        sign_dict = None
        sign_status = None  # 记录签到状态
        
        # 保存当前账户信息到临时属性，供其他方法使用
        self._current_account = account
        
        try:
            # 检查今日是否已成功签到
            if not self._is_manual_trigger() and self._is_account_already_signed_today(alias):
                logger.info(f"账户 {alias} 根据历史记录，今日已成功签到，跳过本次执行")
                
                # 创建跳过记录
                sign_dict = {
                    "account": alias,
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": "跳过: 今日已签到",
                }
                
                # 获取最后一次成功签到的记录信息
                history = self.get_data(f'sign_history_{alias}') or []
                today = datetime.now().strftime('%Y-%m-%d')
                today_success = [
                    record for record in history 
                    if record.get("date", "").startswith(today) 
                    and record.get("status") in ["签到成功", "已签到"]
                ]
                
                # 添加最后成功签到记录的详细信息
                if today_success:
                    last_success = max(today_success, key=lambda x: x.get("date", ""))
                    # 复制积分信息到跳过记录
                    sign_dict.update({
                        "message": last_success.get("message"),
                        "points": last_success.get("points"),
                        "days": last_success.get("days")
                    })
                
                # 尝试获取用户信息
                try:
                    cookies = {}
                    if cookie:
                        for cookie_item in cookie.split(';'):
                            if '=' in cookie_item:
                                name, value = cookie_item.strip().split('=', 1)
                                cookies[name] = value
                    token = cookies.get('token')
                    if token:
                        user_info = self._fetch_user_info(cookies, token, alias)
                except Exception:
                    pass
                
                return sign_dict
            
            # 如果没有cookie但有用户名密码，尝试自动登录
            if not cookie and username and password:
                logger.info(f"账户 {alias} 未配置Cookie，尝试自动登录")
                new_cookie = self._auto_login(username, password)
                if new_cookie:
                    cookie = new_cookie
                    # 更新账户配置
                    account["cookie"] = new_cookie
                    self._update_account_config(account_idx, new_cookie)
                    logger.info(f"账户 {alias} 已通过自动登录获取新Cookie")
                else:
                    logger.error(f"账户 {alias} 自动登录失败")
                    sign_dict = {
                        "account": alias,
                        "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                        "status": "签到失败: 自动登录失败",
                    }
                    self._save_account_sign_history(alias, sign_dict)
                    return sign_dict
            elif not cookie:
                logger.error(f"账户 {alias} 未配置Cookie且无登录凭据")
                sign_dict = {
                    "account": alias,
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": "签到失败: 未配置Cookie",
                }
                self._save_account_sign_history(alias, sign_dict)
                return sign_dict
            
            logger.info(f"账户 {alias} 执行签到...")

            # 确保Cookie有效
            try:
                ensured = self._ensure_valid_cookie(cookie, username, password)
                if ensured:
                    cookie = ensured
                    account["cookie"] = ensured
                    self._update_account_config(account_idx, ensured)
            except Exception as e:
                logger.warning(f"账户 {alias} 检查Cookie有效性失败: {e}")

            # 预拉取用户信息
            try:
                cookies = {}
                if cookie:
                    for cookie_item in cookie.split(';'):
                        if '=' in cookie_item:
                            name, value = cookie_item.strip().split('=', 1)
                            cookies[name] = value
                token = cookies.get('token')
                if token:
                    logger.info(f"账户 {alias} 尝试预拉取用户信息")
                    self._fetch_user_info(cookies, token, alias)
            except Exception:
                pass
            
            # 执行签到
            state, message = self._signin_base(cookie, alias)
            
            if state:
                logger.debug(f"账户 {alias} 签到API消息: {message}")
                
                if "已经签到" in message or "签到过" in message:
                    sign_status = "已签到"
                else:
                    sign_status = "签到成功"
                
                logger.debug(f"账户 {alias} 签到状态: {sign_status}")

                # 计算连续签到天数
                today_str = datetime.now().strftime('%Y-%m-%d')
                last_date_str = self.get_data(f'last_success_date_{alias}')
                consecutive_days = self.get_data(f'consecutive_days_{alias}', 0)

                if last_date_str == today_str:
                    # 当天重复运行，天数不变
                    pass
                elif last_date_str == (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d'):
                    # 连续签到，天数+1
                    consecutive_days += 1
                else:
                    # 签到中断或首次签到，重置为1
                    consecutive_days = 1
                
                # 更新连续签到数据
                self.save_data(f'consecutive_days_{alias}', consecutive_days)
                self.save_data(f'last_success_date_{alias}', today_str)

                # 创建签到记录
                sign_dict = {
                    "account": alias,
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": sign_status,
                    "message": message,
                    "days": consecutive_days  # 使用计算出的天数
                }
                
                # 解析奖励积分
                points_match = re.search(r'获得 (\d+) 积分', message)
                sign_dict['points'] = int(points_match.group(1)) if points_match else "—"

                self._save_account_sign_history(alias, sign_dict)
                return sign_dict
            else:
                # 签到失败
                logger.error(f"账户 {alias} 签到失败: {message}")

                # 检测鉴权失败，尝试自动登录刷新 Cookie 后重试一次
                if any(k in (message or "") for k in ["未配置Cookie", "缺少'token'", "未授权", "Unauthorized", "token", "csrf", "登录已过期", "过期", "expired"]):
                    logger.info(f"账户 {alias} 检测到Cookie或鉴权问题，尝试自动登录刷新Cookie后重试一次")
                    if username and password:
                        new_cookie = self._auto_login(username, password)
                        if new_cookie:
                            cookie = new_cookie
                            account["cookie"] = new_cookie
                            self._update_account_config(account_idx, new_cookie)
                            logger.info(f"账户 {alias} 自动登录成功，使用新Cookie重试签到")
                            state2, message2 = self._signin_base(cookie, alias)
                            if state2:
                                sign_status = "签到成功" if "签到" in (message2 or "") and "已" not in message2 else "已签到"
                                sign_dict = {
                                    "account": alias,
                                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                                    "status": sign_status,
                                    "message": message2,
                                }
                                # 解析奖励积分
                                points_match = re.search(r'获得 (\d+) 积分', message2 or "")
                                sign_dict['points'] = int(points_match.group(1)) if points_match else "—"
                                self._save_account_sign_history(alias, sign_dict)
                                return sign_dict
                
                # 常规重试逻辑
                if retry_count < self._max_retries:
                    logger.info(f"账户 {alias} 将在{self._retry_interval}秒后进行第{retry_count+1}次常规重试...")
                    time.sleep(self._retry_interval)
                    return self._sign_account(account, account_idx, retry_count + 1, extended_retry)
                
                # 所有重试都失败
                sign_dict = {
                    "account": alias,
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": f"签到失败: {message}",
                    "message": message
                }
                self._save_account_sign_history(alias, sign_dict)
                return sign_dict
        
        except requests.RequestException as req_exc:
            # 网络请求异常处理
            logger.error(f"账户 {alias} 网络请求异常: {str(req_exc)}")
            # 添加执行超时检查
            if (datetime.now() - start_time).total_seconds() > 300:  # 单个账户5分钟超时
                logger.error(f"账户 {alias} 签到执行时间超过5分钟，执行超时")
                sign_dict = {
                    "account": alias,
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": "签到失败: 执行超时",
                }
                self._save_account_sign_history(alias, sign_dict)
                return sign_dict
        except Exception as e:
            logger.error(f"账户 {alias} 签到异常: {str(e)}", exc_info=True)
            sign_dict = {
                "account": alias,
                "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                "status": f"签到失败: {str(e)}",
            }
            self._save_account_sign_history(alias, sign_dict)
        
        # 清理临时属性
        if hasattr(self, '_current_account'):
            delattr(self, '_current_account')
            
        return sign_dict

    def _signin_base(self, cookie: str, alias: str) -> Tuple[bool, str]:
        """
        基于影巢API的签到实现
        """
        try:
            cookies = {}
            if cookie:
                for cookie_item in cookie.split(';'):
                    if '=' in cookie_item:
                        name, value = cookie_item.strip().split('=', 1)
                        cookies[name] = value
            else:
                return False, "未配置Cookie"

            token = cookies.get('token')
            csrf_token = cookies.get('csrf_access_token')

            if not token:
                return False, "Cookie中缺少'token'"

            user_id = None
            referer = self._site_url
            try:
                decoded_token = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
                user_id = decoded_token.get('sub')
                if user_id:
                    referer = f"{self._base_url}/user/{user_id}"
            except Exception as e:
                logger.warning(f"账户 {alias} 从Token中解析用户ID失败，将使用默认Referer: {e}")

            proxies = settings.PROXY
            ua = settings.USER_AGENT

            headers = {
                'User-Agent': ua,
                'Accept': 'application/json, text/plain, */*',
                'Origin': self._base_url,
                'Referer': referer,
                'Authorization': f'Bearer {token}',
            }
            if csrf_token:
                headers['x-csrf-token'] = csrf_token

            signin_res = requests.post(
                url=self._signin_api,
                headers=headers,
                cookies=cookies,
                proxies=proxies,
                timeout=30,
                verify=False
            )

            if signin_res is None:
                return False, '签到请求失败，响应为空，请检查代理或网络环境'

            try:
                signin_result = signin_res.json()
            except json.JSONDecodeError:
                logger.error(f"账户 {alias} API响应JSON解析失败 (状态码 {signin_res.status_code}): {signin_res.text[:500]}")
                return False, f'签到API响应格式错误，状态码: {signin_res.status_code}'

            message = signin_result.get('message', '无明确消息')
            
            if signin_result.get('success'):
                try:
                    self._fetch_user_info(cookies, token, alias)
                except Exception:
                    pass
                return True, message

            if "已经签到" in message or "签到过" in message:
                try:
                    self._fetch_user_info(cookies, token, alias)
                except Exception:
                    pass
                return True, message 

            logger.error(f"账户 {alias} 签到失败, HTTP状态码: {signin_res.status_code}, 消息: {message}")
            return False, message

        except Exception as e:
            logger.error(f"账户 {alias} 签到流程发生未知异常", exc_info=True)
            return False, f'签到异常: {str(e)}'

    def _save_account_sign_history(self, alias: str, sign_data: Dict):
        """
        保存单个账户的签到历史记录
        """
        try:
            # 读取现有历史
            history = self.get_data(f'sign_history_{alias}') or []

            # 确保日期格式正确
            if "date" not in sign_data:
                sign_data["date"] = datetime.today().strftime('%Y-%m-%d %H:%M:%S')

            history.append(sign_data)

            # 清理旧记录
            retention_days = int(self._history_days)
            now = datetime.now()
            valid_history = []

            for record in history:
                try:
                    # 尝试将记录日期转换为datetime对象
                    record_date = datetime.strptime(record["date"], '%Y-%m-%d %H:%M:%S')
                    # 检查是否在保留期内
                    if (now - record_date).days < retention_days:
                        valid_history.append(record)
                except (ValueError, KeyError):
                    # 如果记录日期格式不正确，尝试修复
                    logger.warning(f"账户 {alias} 历史记录日期格式无效: {record.get('date', '无日期')}")
                    # 添加新的日期并保留记录
                    record["date"] = datetime.today().strftime('%Y-%m-%d %H:%M:%S')
                    valid_history.append(record)

            # 保存历史
            self.save_data(key=f"sign_history_{alias}", value=valid_history)
            logger.info(f"账户 {alias} 保存签到历史记录，当前共有 {len(valid_history)} 条记录")

        except Exception as e:
            logger.error(f"账户 {alias} 保存签到历史记录失败: {str(e)}", exc_info=True)

    def _fetch_user_info(self, cookies: Dict[str, str], token: str, alias: str) -> Optional[dict]:
        try:
            referer = self._site_url
            try:
                decoded_token = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
                user_id = decoded_token.get('sub')
                if user_id:
                    referer = f"{self._base_url}/user/{user_id}"
            except Exception:
                pass
            headers = {
                'User-Agent': settings.USER_AGENT,
                'Accept': 'application/json, text/plain, */*',
                'Origin': self._base_url,
                'Referer': referer,
                'Authorization': f'Bearer {token}',
            }
            resp = requests.get(self._user_info_api, headers=headers, cookies=cookies, proxies=settings.PROXY, timeout=30, verify=False)
            logger.info(f"账户 {alias} 拉取用户信息 API 状态码: {getattr(resp,'status_code','unknown')} CT: {getattr(resp.headers,'get',lambda k:'' )('Content-Type')}")
            data = {}
            try:
                data = resp.json()
            except Exception:
                data = {}
            # 统一解析 response.data / detail / data 结构
            detail = (data.get('response') or {}).get('data') or data.get('detail') or data.get('data') or {}
            if not isinstance(detail, dict):
                detail = {}
            info = {
                'account': alias,
                'id': detail.get('id') or detail.get('member_id'),
                'nickname': detail.get('nickname') or detail.get('member_name'),
                'avatar_url': detail.get('avatar_url') or detail.get('gravatar_url'),
                'created_at': detail.get('created_at'),
                'points': ((detail.get('user_meta') or {}).get('points')),
                'signin_days_total': ((detail.get('user_meta') or {}).get('signin_days_total')),
                'warnings_nums': detail.get('warnings_nums'),
            }
            # 若 API 未返回完整信息，尝试 RSC 页面解析
            if not info.get('nickname') or info.get('points') is None or info.get('signin_days_total') is None:
                try:
                    rsc_headers = {
                        'User-Agent': settings.USER_AGENT,
                        'Accept': 'text/x-component',
                        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                        'Origin': self._base_url,
                        'Referer': referer,
                        'rsc': '1',
                    }
                    rsc_url = referer
                    rsc_resp = requests.get(rsc_url, headers=rsc_headers, cookies=cookies, proxies=settings.PROXY, timeout=30, verify=False)
                    logger.info(f"账户 {alias} RSC 用户页状态码: {getattr(rsc_resp,'status_code','unknown')} CT: {getattr(rsc_resp.headers,'get',lambda k:'' )('Content-Type')}")
                    rsc_text = rsc_resp.text or ''
                    import re as _re
                    m_nick = _re.search(r'"nickname":"([^"]+)"', rsc_text)
                    m_points = _re.search(r'"points":(\d+)', rsc_text)
                    m_days = _re.search(r'"signin_days_total":(\d+)', rsc_text)
                    m_avatar = _re.search(r'"avatar_url":"([^"]+)"', rsc_text)
                    m_created = _re.search(r'"created_at":"([^"]+)"', rsc_text)
                    if m_nick:
                        info['nickname'] = m_nick.group(1)
                    if m_points:
                        info['points'] = int(m_points.group(1))
                    if m_days:
                        info['signin_days_total'] = int(m_days.group(1))
                    if m_avatar:
                        info['avatar_url'] = m_avatar.group(1)
                    if m_created:
                        info['created_at'] = m_created.group(1)
                    if (not info.get('nickname') or info.get('points') is None or info.get('signin_days_total') is None) and '"user":' in rsc_text:
                        user_json = self._extract_rsc_object(rsc_text, 'user')
                        if user_json:
                            try:
                                obj = json.loads(user_json)
                                info['id'] = obj.get('id') or info.get('id')
                                info['nickname'] = obj.get('nickname') or info.get('nickname')
                                info['avatar_url'] = obj.get('avatar_url') or info.get('avatar_url')
                                info['created_at'] = obj.get('created_at') or info.get('created_at')
                                meta = obj.get('user_meta') or {}
                                if isinstance(meta, dict):
                                    if meta.get('points') is not None:
                                        info['points'] = meta.get('points')
                                    if meta.get('signin_days_total') is not None:
                                        info['signin_days_total'] = meta.get('signin_days_total')
                            except Exception:
                                pass
                except Exception:
                    pass
            self.save_data(f'hdhive_user_info_{alias}', info)
            return info
        except Exception as e:
            logger.warning(f"账户 {alias} 获取用户信息失败: {e}")
            return None

    def _extract_rsc_object(self, text: str, key: str) -> Optional[str]:
        try:
            marker = f'"{key}":'
            idx = text.find(marker)
            if idx == -1:
                return None
            brace_idx = text.find('{', idx + len(marker))
            if brace_idx == -1:
                return None
            depth = 0
            i = brace_idx
            in_str = False
            prev = ''
            while i < len(text):
                ch = text[i]
                if ch == '"' and prev != '\\':
                    in_str = not in_str
                if not in_str:
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            segment = text[brace_idx:i+1]
                            return segment
                prev = ch
                i += 1
            return None
        except Exception:
            return None

    def _send_summary_notification(self, results: List[Dict]):
        """
        发送签到汇总通知
        """
        if not self._notify or not results:
            return
        
        success_count = sum(1 for r in results if r and "成功" in r.get("status", ""))
        skip_count = sum(1 for r in results if r and "跳过" in r.get("status", ""))
        fail_count = sum(1 for r in results if r and "失败" in r.get("status", ""))
        
        # 构建通知标题
        if fail_count == 0 and success_count > 0:
            title = f"【✅ 影巢签到完成】"
        elif fail_count > 0:
            title = f"【⚠️ 影巢签到结果】"
        else:
            title = f"【ℹ️ 影巢签到状态】"
        
        # 构建通知内容
        text = f"📊 签到汇总报告\n━━━━━━━━━━\n"
        text += f"🕐 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        text += f"📍 方式：{self._current_trigger_type}\n"
        text += f"📋 总数：{len(results)} 个账户\n"
        text += f"✅ 成功：{success_count}\n"
        text += f"⏭️ 跳过：{skip_count}\n"
        text += f"❌ 失败：{fail_count}\n"
        text += f"━━━━━━━━━━\n"
        
        # 添加每个账户的详细信息
        for result in results:
            if not result:
                continue
                
            account = result.get("account", "未知账户")
            status = result.get("status", "未知")
            message = result.get("message", "—")
            points = result.get("points", "—")
            
            status_icon = "✅" if "成功" in status else "⏭️" if "跳过" in status else "❌"
            
            text += f"{status_icon} {account}：{status}\n"
            if "成功" in status and message != "—":
                text += f"   💬 {message}\n"
                if points != "—":
                    text += f"   🎁 奖励积分：{points}\n"
            elif "失败" in status and message != "—":
                text += f"   ❗ 错误：{message}\n"
        
        text += f"━━━━━━━━━━"
        
        # 发送通知
        self.post_message(
            mtype=NotificationType.SiteMessage,
            title=title,
            text=text
        )

    def get_state(self) -> bool:
        logger.info(f"hdhivesign状态: {self._enabled}")
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            logger.info(f"注册定时服务: {self._cron}")
            return [{
                "id": "hdhivesign",
                "name": "影巢签到",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sign,
                "kwargs": {}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        返回插件配置的表单
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '开启通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'accounts',
                                            'label': '账户配置',
                                            'placeholder': '每行一个账户，格式：cookie|用户名|密码|别名（别名可选）\n例如：token=xxx; csrf_access_token=yyy|user@example.com|password123|主账号',
                                            'rows': 4,
                                            'hint': '支持多个账户，每行一个。如果只有Cookie，可只填Cookie部分'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'base_url',
                                            'label': '站点地址',
                                            'placeholder': '例如：https://hdhive.online 或新域名',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '签到周期'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'max_retries',
                                            'label': '最大重试次数',
                                            'type': 'number',
                                            'placeholder': '3'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'retry_interval',
                                            'label': '重试间隔(秒)',
                                            'type': 'number',
                                            'placeholder': '30'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'history_days',
                                            'label': '历史保留天数',
                                            'type': 'number',
                                            'placeholder': '30'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '【多账户使用教程】\n1. 每个账户单独一行配置\n2. 格式：cookie|用户名|密码|别名\n3. 示例1（完整配置）：token=xxx; csrf_access_token=yyy|user1@example.com|password123|主账号\n4. 示例2（仅Cookie）：token=xxx; csrf_access_token=yyy\n5. 如果没有密码或自动登录失败，请手动登录获取Cookie\n6. 别名可选，不填则使用用户名\n\n⚠️ 影巢可能变更域名，若签到异常请先更新"站点地址"'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "accounts": "",
            "base_url": "https://hdhive.com",
            "cron": "0 8 * * *",
            "max_retries": 3,
            "retry_interval": 30,
            "history_days": 30
        }

    def get_page(self) -> List[dict]:
        """
        构建插件详情页面，展示所有账户的签到历史
        """
        if not self._accounts:
            return [{
                'component': 'VAlert',
                'props': {
                    'type': 'info', 'variant': 'tonal',
                    'text': '暂无账户配置，请先在设置中配置账户信息。',
                    'class': 'mb-2'
                }
            }]
        
        content = []
        
        # 显示每个账户的信息和签到历史
        for idx, account in enumerate(self._accounts, 1):
            alias = account.get("alias", f"账户{idx}")
            username = account.get("username", "")
            enabled = account.get("enabled", True)
            
            # 获取用户信息
            user_info = self.get_data(f'hdhive_user_info_{alias}') or {}
            consecutive_days = self.get_data(f'consecutive_days_{alias}', 0)
            
            # 账户信息卡片
            avatar = user_info.get('avatar_url') or ''
            nickname = user_info.get('nickname') or username or '—'
            points = user_info.get('points') if user_info.get('points') is not None else '—'
            signin_days_total = user_info.get('signin_days_total') if user_info.get('signin_days_total') is not None else '—'
            created_at = user_info.get('created_at') or '—'
            
            account_card = {
                'component': 'VCard',
                'props': {'variant': 'outlined', 'class': 'mb-4'},
                'content': [
                    {
                        'component': 'VCardTitle',
                        'props': {'class': 'd-flex align-center justify-space-between'},
                        'content': [
                            {
                                'component': 'div',
                                'content': [
                                    {'component': 'span', 'props': {'class': 'text-h6'}, 'text': f'👤 {alias}'},
                                    {'component': 'div', 'props': {'class': 'text-caption'}, 
                                     'text': f'状态：{"✅ 启用" if enabled else "❌ 禁用"} | 用户名：{username}'},
                                    {'component': 'div', 'props': {'class': 'text-caption'}, 'text': f'加入时间：{created_at}'}
                                ]
                            },
                            {'component': 'VAvatar', 'props': {'size': 64}, 
                             'content': [{'component': 'img', 'props': {'src': avatar, 'alt': nickname}}]}
                        ]
                    },
                    {'component': 'VDivider'},
                    {
                        'component': 'VCardText',
                        'content': [
                            {
                                'component': 'VRow',
                                'content': [
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 
                                     'content': [{'component': 'VChip', 'props': {'variant': 'elevated', 'color': 'primary', 'class': 'mb-2'}, 
                                                 'text': f'用户：{nickname}'}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 
                                     'content': [{'component': 'VChip', 'props': {'variant': 'elevated', 'color': 'amber-darken-2', 'class': 'mb-2'}, 
                                                 'text': f'积分：{points}'}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 
                                     'content': [{'component': 'VChip', 'props': {'variant': 'elevated', 'color': 'success', 'class': 'mb-2'}, 
                                                 'text': f'累计签到：{signin_days_total}'}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 
                                     'content': [{'component': 'VChip', 'props': {'variant': 'elevated', 'color': 'cyan-darken-2', 'class': 'mb-2'}, 
                                                 'text': f'连续签到：{consecutive_days}'}]},
                                ]
                            }
                        ]
                    }
                ]
            }
            content.append(account_card)
            
            # 获取该账户的签到历史
            historys = self.get_data(f'sign_history_{alias}') or []
            
            if historys:
                historys = sorted(historys, key=lambda x: x.get("date", ""), reverse=True)
                history_rows = []
                for history in historys[:20]:  # 只显示最近20条记录
                    status = history.get("status", "未知")
                    if "成功" in status or "已签到" in status:
                        status_color = "success"
                    elif "失败" in status:
                        status_color = "error"
                    else:
                        status_color = "info"

                    history_rows.append({
                        'component': 'tr',
                        'content': [
                            {'component': 'td', 'props': {'class': 'text-caption'}, 'text': history.get("date", "")},
                            {
                                'component': 'td',
                                'content': [{
                                    'component': 'VChip',
                                    'props': {'color': status_color, 'size': 'small', 'variant': 'outlined'},
                                    'text': status
                                }]
                            },
                            {'component': 'td', 'text': history.get('message', '—')},
                            {'component': 'td', 'text': str(history.get('points', '—'))},
                            {'component': 'td', 'text': str(history.get('days', '—'))},
                        ]
                    })
                
                history_card = {
                    'component': 'VCard',
                    'props': {'variant': 'outlined', 'class': 'mb-4'},
                    'content': [
                        {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': f'📊 {alias} 签到历史（最近{min(len(historys), 20)}条）'},
                        {
                            'component': 'VCardText',
                            'content': [{
                                'component': 'VTable',
                                'props': {'hover': True, 'density': 'compact'},
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [{
                                            'component': 'tr',
                                            'content': [
                                                {'component': 'th', 'text': '时间'},
                                                {'component': 'th', 'text': '状态'},
                                                {'component': 'th', 'text': '详情'},
                                                {'component': 'th', 'text': '奖励积分'},
                                                {'component': 'th', 'text': '连续天数'}
                                            ]
                                        }]
                                    },
                                    {'component': 'tbody', 'content': history_rows}
                                ]
                            }]
                        }
                    ]
                }
                content.append(history_card)
            else:
                content.append({
                    'component': 'VAlert',
                    'props': {
                        'type': 'info', 'variant': 'tonal',
                        'text': f'账户 {alias} 暂无签到记录',
                        'class': 'mb-4'
                    }
                })
        
        return content

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def stop_service(self):
        """
        停止服务
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止影巢签到服务失败: {str(e)}")

    def _is_manual_trigger(self) -> bool:
        """
        判断是否为手动触发
        """
        return getattr(self, '_manual_trigger', False)

    def _clear_extended_retry_tasks(self):
        """
        清理所有延长重试任务
        """
        try:
            if self._scheduler:
                jobs = self._scheduler.get_jobs()
                for job in jobs:
                    if "延长重试" in job.name:
                        self._scheduler.remove_job(job.id)
                        logger.info(f"清理延长重试任务: {job.name}")
        except Exception as e:
            logger.warning(f"清理延长重试任务失败: {str(e)}")

    def _has_running_extended_retry(self) -> bool:
        """
        检查是否有正在运行的延长重试任务
        """
        try:
            if self._scheduler:
                jobs = self._scheduler.get_jobs()
                for job in jobs:
                    if "延长重试" in job.name:
                        return True
            return False
        except Exception:
            return False

    def _is_account_already_signed_today(self, alias: str) -> bool:
        """
        检查指定账户今天是否已经签到成功
        """
        history = self.get_data(f'sign_history_{alias}') or []
        if not history:
            return False
        today = datetime.now().strftime('%Y-%m-%d')
        # 查找今日是否有成功签到记录
        return any(
            record.get("date", "").startswith(today)
            and record.get("status") in ["签到成功", "已签到"]
            for record in history
        )

    def _ensure_valid_cookie(self, cookie: str, username: str = "", password: str = "") -> Optional[str]:
        try:
            if not cookie:
                return None
            token = None
            for part in cookie.split(';'):
                p = part.strip()
                if p.startswith('token='):
                    token = p.split('=', 1)[1]
                    break
            if not token:
                return None
            try:
                decoded = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
                exp_ts = decoded.get('exp')
            except Exception:
                exp_ts = None
            if exp_ts and isinstance(exp_ts, (int, float)):
                import time as _t
                now_ts = int(_t.time())
                if exp_ts <= now_ts:
                    return self._auto_login(username, password)
            return None
        except Exception:
            return None

    def _auto_login(self, username: str, password: str) -> Optional[str]:
        try:
            if not username or not password:
                logger.warning("未配置用户名或密码，无法自动登录")
                return None
            try:
                import cloudscraper
                scraper = cloudscraper.create_scraper()
                logger.info("自动登录: 使用 cloudscraper")
            except Exception as e:
                logger.warning(f"cloudscraper 不可用，将尝试 requests：{e}")
                scraper = requests
                logger.info("自动登录: 回退到 requests")
            # 预热登录页，拿到初始 Cookie
            login_url = f"{self._base_url}{self._login_page}"
            try:
                logger.info(f"自动登录: 预热 {login_url}")
                resp_warm = scraper.get(login_url, timeout=30, proxies=settings.PROXY)
                logger.info(f"自动登录: 预热状态码 {getattr(resp_warm, 'status_code', 'unknown')} Content-Type {getattr(resp_warm.headers, 'get', lambda k: '')('Content-Type')}")
            except Exception:
                pass
            # 尝试 API 登录候选
            for path in self._login_api_candidates:
                url = f"{self._base_url}{path}"
                headers = {
                    'User-Agent': settings.USER_AGENT,
                    'Accept': 'application/json, text/plain, */*',
                    'Origin': self._base_url,
                    'Referer': login_url,
                    'Content-Type': 'application/json'
                }
                payload = {
                    'username': username,
                    'password': password
                }
                try:
                    logger.info(f"自动登录: 尝试 API 登录 {url}")
                    resp = scraper.post(url, headers=headers, json=payload, timeout=30, proxies=settings.PROXY)
                    logger.info(f"自动登录: API 登录状态码 {getattr(resp, 'status_code', 'unknown')} Content-Type {getattr(resp.headers, 'get', lambda k: '')('Content-Type')}")
                    # 成功条件：响应包含 set-cookie 或 JSON 内含 meta.access_token
                    cookies_dict = None
                    try:
                        cookies_dict = getattr(resp, 'cookies', None).get_dict() if getattr(resp, 'cookies', None) else {}
                    except Exception:
                        cookies_dict = {}
                    token_cookie = cookies_dict.get('token')
                    csrf_cookie = cookies_dict.get('csrf_access_token')
                    if not token_cookie:
                        try:
                            data = resp.json()
                            logger.info(f"自动登录: API 登录返回JSON keys {list(data.keys()) if isinstance(data, dict) else 'non-dict'}")
                            meta = (data.get('meta') or {})
                            acc = meta.get('access_token')
                            ref = meta.get('refresh_token')
                            if acc:
                                # 将 access_token 写入 token Cookie
                                if hasattr(scraper, 'cookies'):
                                    try:
                                        scraper.cookies.set('token', acc, domain=self._base_url.replace('https://','').replace('http://',''))
                                        token_cookie = acc
                                    except Exception:
                                        token_cookie = acc
                                else:
                                    token_cookie = acc
                        except Exception:
                            pass
                    if token_cookie:
                        cookie_items = [f"token={token_cookie}"]
                        if csrf_cookie:
                            cookie_items.append(f"csrf_access_token={csrf_cookie}")
                        cookie_str = "; ".join(cookie_items)
                        logger.info("API登录成功，已生成Cookie")
                        return cookie_str
                except Exception as e:
                    logger.debug(f"API登录候选失败: {path} -> {e}")
            # 尝试 Next.js Server Action 登录
            url = f"{self._base_url}{self._login_page}"
            headers = {
                'User-Agent': settings.USER_AGENT,
                'Accept': 'text/x-component',
                'Origin': self._base_url,
                'Referer': login_url,
                'Content-Type': 'text/plain;charset=UTF-8'
            }
            # 从预热页面尝试提取 next-action token
            next_action_token = None
            try:
                warm_text = getattr(resp_warm, 'text', '') or ''
                # 常见形式：next-action":"<token>" 或 name="next-action" value="<token>"
                import re as _re
                m = _re.search(r'next-action"\s*:\s*"([a-fA-F0-9]{16,64})"', warm_text)
                if not m:
                    m = _re.search(r'name="next-action"\s+value="([a-fA-F0-9]{16,64})"', warm_text)
                if m:
                    next_action_token = m.group(1)
                    headers['next-action'] = next_action_token
                    # 参考样例的最小 router state（静态值）
                    headers['next-router-state-tree'] = '%5B%22%22%2C%7B%22children%22%3A%5B%22(auth)%22%2C%7B%22children%22%3A%5B%22login%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2C%22%2Flogin%22%2C%22refresh%22%5D%7D%5D%7D%2Cnull%2Cnull%2Ctrue%5D%7D%2Cnull%2Cnull%2Ctrue%5D'
                    logger.info(f"自动登录: 提取 next-action={next_action_token}")
                else:
                    logger.info("自动登录: 未在页面提取到 next-action token")
            except Exception as e:
                logger.debug(f"自动登录: 提取 next-action 失败: {e}")
            body = json.dumps([{'username': username, 'password': password}])
            try:
                logger.info(f"自动登录: 尝试 Server Action 登录 {url}")
                resp = scraper.post(url, headers=headers, data=body, timeout=30, proxies=settings.PROXY)
                logger.info(f"自动登录: SA 登录状态码 {getattr(resp, 'status_code', 'unknown')} Content-Type {getattr(resp.headers, 'get', lambda k: '')('Content-Type')}")
                cookies_dict = None
                try:
                    cookies_dict = getattr(resp, 'cookies', None).get_dict() if getattr(resp, 'cookies', None) else {}
                except Exception:
                    cookies_dict = {}
                token_cookie = cookies_dict.get('token')
                csrf_cookie = cookies_dict.get('csrf_access_token')
                if token_cookie:
                    cookie_items = [f"token={token_cookie}"]
                    if csrf_cookie:
                        cookie_items.append(f"csrf_access_token={csrf_cookie}")
                    cookie_str = "; ".join(cookie_items)
                    logger.info("Server Action 登录成功，已生成Cookie")
                    return cookie_str
            except Exception as e:
                logger.warning(f"Server Action 登录失败: {e}")
            # 浏览器自动化兜底：使用 Playwright 直接执行页面登录并读取 Cookie
            try:
                from playwright.sync_api import sync_playwright
                logger.info("自动登录: 尝试使用 Playwright 浏览器自动化")
                proxy = None
                try:
                    pxy = settings.PROXY or {}
                    server = pxy.get('http') or pxy.get('https')
                    if server:
                        proxy = {"server": server}
                except Exception:
                    proxy = None
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True, proxy=proxy) if proxy else pw.chromium.launch(headless=True)
                    context = browser.new_context()
                    page = context.new_page()
                    page.goto(login_url, wait_until="domcontentloaded")
                    # 选择器启发式
                    selectors = [
                        "input[name='username']",
                        "input[name='email']",
                        "input[type='email']",
                        "input[placeholder*='邮箱']",
                        "input[placeholder*='email']",
                        "input[placeholder*='用户名']",
                    ]
                    pwd_selectors = [
                        "input[name='password']",
                        "input[type='password']",
                        "input[placeholder*='密码']",
                    ]
                    for sel in selectors:
                        try:
                            if page.query_selector(sel):
                                page.fill(sel, username)
                                break
                        except Exception:
                            continue
                    for sel in pwd_selectors:
                        try:
                            if page.query_selector(sel):
                                page.fill(sel, password)
                                break
                        except Exception:
                            continue
                    # 点击提交按钮
                    try:
                        btn = page.query_selector("button[type='submit']") or page.query_selector("button:has-text('登录')") or page.query_selector("button:has-text('Login')")
                        if btn:
                            btn.click()
                        else:
                            page.keyboard.press("Enter")
                    except Exception:
                        page.keyboard.press("Enter")
                    # 等待可能的跳转或网络静止
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    # 读取 Cookie
                    cookies = context.cookies()
                    token_cookie = None
                    csrf_cookie = None
                    for c in cookies:
                        if c.get('name') == 'token':
                            token_cookie = c.get('value')
                        elif c.get('name') == 'csrf_access_token':
                            csrf_cookie = c.get('value')
                    context.close()
                    browser.close()
                    if token_cookie:
                        cookie_items = [f"token={token_cookie}"]
                        if csrf_cookie:
                            cookie_items.append(f"csrf_access_token={csrf_cookie}")
                        cookie_str = "; ".join(cookie_items)
                        logger.info("Playwright 登录成功，已生成Cookie")
                        return cookie_str
                logger.error("自动登录失败，未获取到有效Cookie")
                return None
            except Exception as e:
                logger.error(f"Playwright 自动登录异常: {e}")
                logger.error("自动登录失败，未获取到有效Cookie")
                return None
        except Exception as e:
            logger.error(f"自动登录异常: {str(e)}")
            return None

    def _update_account_config(self, account_idx: int, new_cookie: str):
        """
        更新账户配置中的Cookie
        """
        if 0 <= account_idx - 1 < len(self._accounts):
            self._accounts[account_idx - 1]["cookie"] = new_cookie
            # 更新配置文件
            config = self.get_config()
            if config:
                config["accounts"] = self._format_accounts(self._accounts)
                self.update_config(config)

    def _get_last_sign_time(self, alias: str) -> str:
        """
        获取指定账户最后一次签到成功的时间
        """
        history = self.get_data(f'sign_history_{alias}') or []
        if history:
            try:
                last_success = max([
                    record for record in history if record.get("status") in ["签到成功", "已签到"]
                ], key=lambda x: x.get("date", ""))
                return last_success.get("date")
            except ValueError:
                return "从未"
        return "从未"