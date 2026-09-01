# -*- coding: utf-8 -*-
"""
通用爬虫核心模块（支持多厂商配置）

根据厂商配置自动执行登录和数据抓取：
1. 登录：按厂商配置的接口、参数名、响应格式获取token
2. 抓取：按厂商配置的API路径、分页参数、响应格式自动翻页
3. 支持有验证码/无验证码、JSON/表单/GET请求、各种认证方式
4. 保存到本地 SQLite 数据库

作者：Doubao 生成
"""
import json
import time
import hashlib
import os
import threading
import requests
from pathlib import Path
from datetime import datetime
from storage import storage

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ==================== 路径常量 ====================
BASE_DIR = Path(__file__).resolve().parent
CAPTCHA_DIR = BASE_DIR / "captcha"
DATA_DIR = BASE_DIR / "data"
CAPTCHA_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# 默认API基地址（熊猫游，兼容旧代码）
API_BASE = "https://tg-admin-api.jinyou68.com"

# 默认数据接口列表（兼容旧代码）
API_LIST = {
    "玩家账号信息": "/api/cps_info/game_user_list",
    "游戏充值详单": "/api/cps_info/game_recharge_list",
    "游戏角色信息": "/api/cps_info/game_role_list",
    "角色更新记录": "/api/cps_info/role_update_list",
    "小组数据信息": "/api/cps_info/channel_group_list",
    "员工数据信息": "/api/cps_info/channel_member_list",
}


def get_nested_value(data, path, default=None):
    """从嵌套字典/列表中按路径取值，路径用点分隔，如 data.accessToken、rows.0.name"""
    if not path:
        return default
    keys = path.split(".")
    cur = data
    for key in keys:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list):
            try:
                cur = cur[int(key)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return cur if cur is not None else default


class ApiCrawler:
    """通用 API 抓取器"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
        })
        self.logs = []
        # 持久浏览器线程：确保所有浏览器操作在同一个线程里运行（Playwright page 对象不能跨线程）
        self._browser_thread = None
        self._browser_cmd_queue = None
        self._browser_result = None
        self._browser_event = None
        self._browser_running = False

    def log(self, msg):
        """记录日志"""
        ts = datetime.now().strftime("%H:%M:%S")
        line = "[%s] %s" % (ts, msg)
        self.logs.append(line)
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]
        print(line)

    # ==================== 验证码（通用，用于有验证码的厂商） ====================

    def get_captcha(self, api_base_url=None, captcha_config=None):
        """
        通用获取验证码（支持两种模式）：
        模式1 - 直接图片：captcha_config = {"url": "https://xxx/captcha.jpg"}
               直接请求图片URL，verkey随机生成
        模式2 - key+img：captcha_config = {"key_url": "/verify/key", "img_url": "/verify/img?verKey={verkey}"}
               先请求key接口获取verkey，再请求图片
        """
        try:
            base = api_base_url or API_BASE
            cfg = captcha_config or {}

            # 清除旧cookie，确保验证码和新session绑定
            self.session.cookies.clear()
            # 用登录页面地址设置 Referer 和 Origin（如果有 login_url 的话）
            referer_url = login_url or (base + '/')
            # 从 login_url 提取 origin（协议+域名+端口）
            origin_url = base
            if login_url:
                try:
                    from urllib.parse import urlparse
                    parsed = urlparse(login_url)
                    origin_url = f"{parsed.scheme}://{parsed.netloc}"
                except:
                    pass
            # 添加必要的请求头，模拟浏览器
            self.session.headers.update({
                'Referer': referer_url,
                'Origin': origin_url,
                'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
            })

            verkey = ""
            img_url = ""

            # 最多重试3次
            max_retries = 3
            for retry in range(max_retries):
                try:
                    if cfg.get("url"):
                        # 模式1：直接图片URL，verkey随机生成
                        verkey = hashlib.md5(os.urandom(16)).hexdigest()
                        img_url = cfg["url"]
                        if "{verkey}" in img_url:
                            img_url = img_url.format(verkey=verkey)
                        self.log("直接获取验证码图片: " + img_url)
                    else:
                        # 模式2：先获取key，再获取图片
                        key_url = cfg.get("key_url", "/verify/key")
                        img_url_tpl = cfg.get("img_url", "/verify/img?time={time}&verKey={verkey}")
                        self.log(f"获取验证码密钥（第{retry+1}次）: {key_url}")
                        r = self.session.post(base + key_url, timeout=30)
                        try:
                            resp_data = r.json()
                            verkey = resp_data.get("data", {}).get("verKey", "") or resp_data.get("verKey", "")
                        except Exception:
                            verkey = ""
                        if not verkey:
                            if retry < max_retries - 1:
                                self.log(f"获取verkey失败，状态码:{r.status_code}，{retry+1}秒后重试...")
                                time.sleep(retry + 1)
                                continue
                            return {"success": False, "error": f"获取verkey失败（状态码:{r.status_code}, 接口返回: {r.text[:100]}）"}
                        ts = int(time.time() * 1000)
                        img_url = base + img_url_tpl.format(time=ts, verkey=verkey)
                        self.log("获取验证码图片: " + img_url)

                    # 请求验证码图片
                    r = self.session.get(img_url, timeout=30)
                    if r.status_code != 200 or len(r.content) < 50:
                        if retry < max_retries - 1:
                            self.log(f"验证码图片获取失败，状态码:{r.status_code}, 大小:{len(r.content)}，{retry+1}秒后重试...")
                            time.sleep(retry + 1)
                            continue
                        return {"success": False, "error": f"验证码图片获取失败（状态码:{r.status_code}, 大小:{len(r.content)}）"}

                    # 检查是否是有效的图片（不是HTML错误页面）
                    content_type = r.headers.get('content-type', '')
                    if 'html' in content_type or r.content[:20].startswith(b'<!doctype html') or r.content[:20].startswith(b'<html'):
                        if retry < max_retries - 1:
                            self.log("验证码返回的是HTML页面而非图片，重试...")
                            time.sleep(retry + 1)
                            continue
                        return {"success": False, "error": "验证码接口返回HTML页面而非图片，请检查验证码配置"}

                    # 成功，跳出重试循环
                    break

                except Exception as e:
                    if retry < max_retries - 1:
                        self.log(f"获取验证码异常: {str(e)[:80]}，{retry+1}秒后重试...")
                        time.sleep(retry + 1)
                    else:
                        return {"success": False, "error": f"获取验证码异常: {str(e)}"}

            img_name = "captcha_%s.png" % datetime.now().strftime("%Y%m%d_%H%M%S")
            img_path = CAPTCHA_DIR / img_name
            with open(img_path, "wb") as f:
                f.write(r.content)
            self.log("验证码已保存: " + img_name + " (verkey: " + verkey[:8] + "...)")
            # 重置 Accept 头，避免影响后续登录请求
            self.session.headers['Accept'] = 'application/json, text/plain, */*'
            return {"success": True, "verkey": verkey, "image_name": img_name, "image_path": str(img_path)}
        except Exception as e:
            self.log("获取验证码异常: " + str(e))
            return {"success": False, "error": str(e)}

    # ==================== 通用登录（根据厂商配置） ====================

    def login(self, username, password, verkey="", verifyCode="",
              api_base_url=None, login_url=None, login_config=None):
        """
        通用登录（根据厂商配置执行）。
        login_config 结构:
        {
            "method": "POST",
            "url": "/api/auth/login_user_name",
            "content_type": "json",  // json / form / query
            "username_field": "userName",
            "password_field": "password",
            "has_captcha": true,
            "captcha": {"key_url":..., "img_url":..., "verkey_field":"verKey", "code_field":"verifyCode"},
            "token_path": "data.accessToken",
            "auth_header": "access-token",
            "auth_prefix": ""
        }
        """
        try:
            base = api_base_url or API_BASE
            # 如果没有传login_config，用默认（熊猫游）
            cfg = login_config or {
                "method": "POST", "url": "/api/auth/login_user_name", "content_type": "json",
                "username_field": "userName", "password_field": "password",
                "has_captcha": True,
                "captcha": {"verkey_field": "verKey", "code_field": "verifyCode"},
                "token_path": "data.accessToken",
                "auth_header": "access-token", "auth_prefix": ""
            }

            method = (cfg.get("method") or "POST").upper()
            url = base + (cfg.get("url") or "/api/auth/login_user_name")
            content_type = cfg.get("content_type") or "json"
            user_field = cfg.get("username_field") or "username"
            pwd_field = cfg.get("password_field") or "password"
            token_path = cfg.get("token_path") or "data.accessToken"
            auth_header = cfg.get("auth_header") or "access-token"
            auth_prefix = cfg.get("auth_prefix") or ""

            # 设置 Referer 和 Origin 头（用登录页面地址，避免服务器验证失败）
            referer_url = login_url or (base + '/')
            origin_url = base
            if login_url:
                try:
                    from urllib.parse import urlparse
                    parsed = urlparse(login_url)
                    origin_url = f"{parsed.scheme}://{parsed.netloc}"
                except:
                    pass
            self.session.headers.update({
                'Referer': referer_url,
                'Origin': origin_url,
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json' if content_type == 'json' else 'application/x-www-form-urlencoded',
            })

            # 构造请求参数
            payload = {user_field: username, pwd_field: password}
            # 有验证码时加验证码参数
            if cfg.get("has_captcha") and verifyCode:
                cap_cfg = cfg.get("captcha") or {}
                # 同时支持 verkey_field 和 key_field 两种字段名，兼容不同厂商配置
                vk_field = cap_cfg.get("verkey_field") or cap_cfg.get("key_field") or "verKey"
                code_field = cap_cfg.get("code_field") or "verifyCode"
                payload[vk_field] = verkey
                payload[code_field] = verifyCode

            self.log("登录中... 账号: %s 接口: %s %s" % (username, method, url))

            # ===== cookie 会话模式（ThinkPHP 等后台）：不依赖 token，登录后保持 cookie =====
            if cfg.get("cookie_session"):
                max_retries = 2
                for retry in range(max_retries + 1):
                    try:
                        if method == "GET":
                            r = self.session.get(url, params=payload, timeout=30, allow_redirects=True)
                        else:
                            r = self.session.post(url, data=payload, timeout=30, allow_redirects=True)
                        break
                    except Exception as e:
                        if retry < max_retries:
                            self.log(f"登录请求网络错误: {str(e)[:60]}，{retry+1}秒后重试...")
                            time.sleep(retry + 1)
                        else:
                            raise
                # 判断登录成功：出现"用户名同名cookie" 或 响应含后台首页特征（欢迎/退出） 或 跳转到 index
                username_cookie = [c.value for c in self.session.cookies if c.name and c.value == username]
                has_home = ('欢迎' in r.text and '退出' in r.text) or 'index' in (r.url or '').lower() or 'doLogin' in (r.url or '').lower()
                if username_cookie or has_home:
                    self.log("登录成功（cookie 会话模式，session保持）")
                    return {"success": True, "access_token": "", "user_info": {"session": True, "mode": "cookie"}}
                self.log("登录失败（cookie 会话模式，未检测到登录成功标志）")
                return {"success": False, "error": "cookie会话登录失败，未检测到登录成功标志", "raw_response": r.text[:200]}

            # 发送请求（网络错误时自动重试2次）
            max_retries = 2
            for retry in range(max_retries + 1):
                try:
                    if method == "GET":
                        r = self.session.get(url, params=payload, timeout=30)
                    elif content_type == "form":
                        r = self.session.post(url, data=payload, timeout=30)
                    else:
                        r = self.session.post(url, json=payload, timeout=30)
                    break
                except Exception as e:
                    if retry < max_retries:
                        self.log(f"登录请求网络错误: {str(e)[:60]}，{retry+1}秒后重试...")
                        time.sleep(retry + 1)
                    else:
                        raise

            data = r.json()

            # 判断登录是否成功（code=1 或 code=200 或 有token）
            code = data.get("code")
            token = get_nested_value(data, token_path)
            if code not in (1, 200, "1", "200") and not token:
                msg = data.get("msg") or data.get("message") or "登录失败"
                detail = f"账号: {username}，接口: {method} {url}，服务器返回: {msg}"
                self.log("登录失败: " + detail)
                return {"success": False, "error": str(msg), "detail": detail, "raw_response": data}

            if not token:
                return {"success": False, "error": "登录响应中没有找到token（路径: %s）" % token_path}

            self.log("登录成功! token: " + str(token)[:20] + "...")

            # 设置认证头
            self.session.headers[auth_header] = auth_prefix + str(token)
            # 清理可能冲突的其他认证头
            for h in ["access-token", "Authorization"]:
                if h != auth_header:
                    self.session.headers.pop(h, None)

            user_info = data.get("data", {}) if isinstance(data.get("data"), dict) else data
            return {"success": True, "access_token": str(token), "user_info": user_info}

        except Exception as e:
            self.log("登录异常: " + str(e))
            return {"success": False, "error": str(e)}

    # ==================== 通用数据抓取（根据厂商配置） ====================

    def _parse_html_table(self, html):
        """解析 HTML 表格，返回 (数据行列表[dict], 表头列表, 总条数)。
        兼容 ThinkPHP 等后台：POST 表单返回整页 HTML，数据在 <table> 中。
        总条数从"共 N 条"文本提取；分页由调用方按 page_size 计算。
        """
        import re
        # 表头
        ths = [re.sub(r'<[^>]+>', '', t).strip()
               for t in re.findall(r'<th[^>]*>([\s\S]*?)</th>', html, re.I)]
        # 数据行
        rows = []
        for tr in re.findall(r'<tr[^>]*>([\s\S]*?)</tr>', html, re.I):
            tds = [re.sub(r'<[^>]+>', '', td).strip()
                   for td in re.findall(r'<td[^>]*>([\s\S]*?)</td>', tr, re.I)]
            if not tds:
                continue
            # 跳过表头行
            if ths and len(tds) == len(ths) and all(a == b or (not a and not b) for a, b in zip(tds, ths)):
                continue
            if ths and len(tds) == len(ths):
                rows.append(dict(zip(ths, tds)))
            elif not ths:
                rows.append({str(i): v for i, v in enumerate(tds)})
        # 总条数
        total = 0
        m = re.search(r'共\s*(\d+)\s*条', html)
        if m:
            try:
                total = int(m.group(1))
            except ValueError:
                total = 0
        return rows, ths, total

    def fetch_data(self, api_path, access_token, params=None, max_pages=20,
                   source_id=None, task_name="API抓取", api_base_url=None,
                   data_api_config=None):
        """
        通用数据抓取（根据厂商配置自动翻页）。
        data_api_config 结构:
        {
            "path": "/api/xxx/list",
            "method": "POST",
            "content_type": "form",  // json / form / query
            "page_param": "pageNo",
            "page_size_param": "pageSize",
            "page_size": 50,
            "data_path": "data.dataList.list",
            "total_path": "data.dataList.totalCount",
            "extra_params": {"isSummary": "0"}
        }
        """
        try:
            base = api_base_url or API_BASE
            if not base:
                return {"success": False, "error": "缺少 api_base_url"}

            # 默认配置（熊猫游）
            cfg = data_api_config or {
                "method": "POST", "content_type": "form",
                "page_param": "pageNo", "page_size_param": "pageSize", "page_size": 50,
                "data_path": "data.dataList.list", "total_path": "data.dataList.totalCount",
            }

            method = (cfg.get("method") or "POST").upper()
            content_type = cfg.get("content_type") or "form"
            response_type = (cfg.get("response_type") or "json").lower()
            # 非 token 模式（HTML 表格/cookie 会话）不需要 access_token
            if not access_token and response_type != "html":
                return {"success": False, "error": "缺少 access_token，请先登录"}
            page_param = cfg.get("page_param") or "pageNo"
            page_size_param = cfg.get("page_size_param") or "pageSize"
            page_size = int(cfg.get("page_size") or 50)
            data_path = cfg.get("data_path") or "rows"
            total_path = cfg.get("total_path") or "total"
            extra_params = cfg.get("extra_params") or {}

            # 设置认证头（从厂商配置取，这里简单设置两个常用的）
            self.session.headers["access-token"] = access_token
            self.session.headers["Authorization"] = "Bearer " + access_token

            if params is None:
                params = {}
            # 合并额外参数
            for k, v in extra_params.items():
                params.setdefault(k, v)
            params.setdefault(page_size_param, str(page_size))

            # PHP 后台接口可能是完整 URL（含域名），此时不再拼接 base
            if api_path.startswith("http://") or api_path.startswith("https://"):
                full_url = api_path
            else:
                full_url = base + api_path
            all_rows = []
            columns = []
            total_count = 0

            for page_no in range(1, max_pages + 1):
                page_params = dict(params)
                page_params[page_param] = str(page_no)

                self.log("抓取第%d页: %s" % (page_no, api_path))

                # 发送请求
                if method == "GET":
                    r = self.session.get(full_url, params=page_params, timeout=30)
                elif content_type == "json":
                    r = self.session.post(full_url, json=page_params, timeout=30)
                else:
                    files = {k: (None, str(v)) for k, v in page_params.items()}
                    r = self.session.post(full_url, files=files, timeout=30)

                if response_type == "html":
                    # ===== HTML 表格解析模式（ThinkPHP 等后台：POST 表单返回 HTML 表格） =====
                    lst, html_headers, html_total = self._parse_html_table(r.text)
                    if html_headers and not columns:
                        columns = html_headers
                    if not lst:
                        self.log("  本页无数据")
                        break
                    total_count = html_total or len(lst)
                    all_rows.extend(lst)
                    total_pages = (int(total_count) + page_size - 1) // page_size if html_total else page_no
                    self.log("  本页%d条，累计%d条，共%d条" % (len(lst), len(all_rows), total_count))
                    if page_no >= total_pages:
                        break
                    continue

                data = r.json()

                # 判断请求是否成功
                code = data.get("code")
                if code not in (1, 200, "1", "200", None):
                    return {"success": False,
                            "error": "API返回错误: code=%s, msg=%s" % (code, data.get("msg"))}

                # 提取数据列表和总数
                lst = get_nested_value(data, data_path, [])
                total_count = get_nested_value(data, total_path, len(lst)) or 0
                if not isinstance(lst, list):
                    lst = []

                # 从第一行获取列名
                if not columns and lst:
                    columns = list(lst[0].keys()) if isinstance(lst[0], dict) else []

                if not lst:
                    break

                all_rows.extend(lst)
                total_pages = (int(total_count) + page_size - 1) // page_size if total_count else page_no
                self.log("  本页%d条，累计%d条，共%d条" % (len(lst), len(all_rows), total_count))

                if page_no >= total_pages:
                    break

            # 把 dict 列表转成行（按 columns 顺序）
            if not columns and all_rows:
                columns = list(all_rows[0].keys()) if isinstance(all_rows[0], dict) else []

            rows_data = []
            for row in all_rows:
                if isinstance(row, dict):
                    rows_data.append([str(row.get(col, "")) for col in columns])
                else:
                    rows_data.append([str(row)])

            if not rows_data:
                self.log("查询成功但无数据")
                return {"success": True, "count": 0, "total_count": total_count,
                        "message": "查询成功但无数据（该账号后台可能无记录）",
                        "data": all_rows, "columns": columns}

            # 保存到数据库
            task_id = storage.save_task(
                rows=rows_data,
                source_url=full_url,
                task_name=task_name,
                source_id=source_id or 0,
                columns=columns,
            )
            self.log("保存完成: %d条数据，任务ID=%d" % (len(rows_data), task_id))

            return {
                "success": True,
                "count": len(rows_data),
                "total_count": total_count,
                "task_id": task_id,
                "columns": columns,
                "data": all_rows,
            }

        except Exception as e:
            self.log("抓取异常: " + str(e))
            return {"success": False, "error": str(e)}

    # ==================== 浏览器登录（备用方案，确保登录成功） ====================

    # ==================== 持久浏览器线程（解决 Playwright page 不能跨线程问题） ====================

    def _ensure_browser_thread(self):
        """确保浏览器线程正在运行"""
        import threading, queue
        if self._browser_thread and self._browser_thread.is_alive():
            return
        self._browser_cmd_queue = queue.Queue()
        self._browser_event = threading.Event()
        self._browser_result = None
        self._browser_running = True
        self._browser_thread = threading.Thread(target=self._browser_worker, daemon=True)
        self._browser_thread.start()
        self.log("浏览器线程已启动")

    def _browser_worker(self):
        """浏览器线程主循环：所有浏览器操作都在这个线程里运行"""
        while self._browser_running:
            try:
                cmd = self._browser_cmd_queue.get(timeout=1)
                if cmd is None:
                    break
                cmd_type = cmd.get('type')
                if cmd_type == 'get_captcha':
                    result = self._browser_get_captcha_impl(
                        cmd.get('username'), cmd.get('password'), cmd.get('login_url'),
                        cmd.get('manual_input', False)
                    )
                elif cmd_type == 'submit_captcha':
                    result = self._browser_submit_captcha_impl(
                        cmd.get('code'), cmd.get('auth_header'), cmd.get('token_path')
                    )
                elif cmd_type == 'close':
                    self._close_browser()
                    result = {"success": True}
                else:
                    result = {"success": False, "error": f"未知命令: {cmd_type}"}
                self._browser_result = result
                self._browser_event.set()
            except Exception as e:
                self._browser_result = {"success": False, "error": str(e)}
                self._browser_event.set()

    def _send_browser_command(self, cmd, timeout=120):
        """向浏览器线程发送命令并等待结果"""
        self._ensure_browser_thread()
        self._browser_event.clear()
        self._browser_cmd_queue.put(cmd)
        self._browser_event.wait(timeout=timeout)
        return self._browser_result or {"success": False, "error": "浏览器操作超时"}

    def browser_get_captcha(self, username, password, login_url=None, manual_input=False):
        """用浏览器打开登录页，填写账号密码，截图验证码（在浏览器线程里运行）
        manual_input=True 时不自动填写账号密码，等待用户在浏览器里手动输入（60秒）
        """
        return self._send_browser_command({
            'type': 'get_captcha',
            'username': username,
            'password': password,
            'login_url': login_url,
            'manual_input': manual_input,
        })

    def _fill_account_password(self, username, password):
        """自动填写账号密码（用 type 模拟真实键盘输入，确保触发 React onChange）"""
        account_inputs = self._page.query_selector_all('input')
        account_filled = False
        password_filled = False
        self._account_selector = None
        self._password_selector = None

        for el in account_inputs:
            try:
                inp_type = (el.get_attribute('type') or 'text').lower()
                ph = (el.get_attribute('placeholder') or '').lower()
                nm = (el.get_attribute('name') or '').lower()
                id_attr = (el.get_attribute('id') or '').lower()

                # 密码框
                if inp_type == 'password' and not password_filled:
                    el.click()
                    el.type(password, delay=30)
                    password_filled = True
                    self._password_selector = el
                    self.log("已填写密码（type模拟输入）")
                    continue

                # 账号框（排除验证码）
                if inp_type in ('text', 'email', 'tel') and not account_filled:
                    if '验证码' not in ph and 'code' not in ph and 'verify' not in ph:
                        if '账号' in ph or 'account' in ph or 'user' in nm or 'account' in nm or 'name' in nm or 'user' in id_attr or 'account' in id_attr:
                            el.click()
                            el.type(username, delay=30)
                            account_filled = True
                            self._account_selector = el
                            self.log("已填写账号（type模拟输入，选择器匹配）")
                            continue
            except:
                pass

        # 如果账号还没填，用第一个非密码、非验证码的text输入框
        if not account_filled:
            for el in account_inputs:
                try:
                    inp_type = (el.get_attribute('type') or 'text').lower()
                    ph = (el.get_attribute('placeholder') or '').lower()
                    if inp_type in ('text', 'email', 'tel') and '验证码' not in ph and 'code' not in ph:
                        el.click()
                        el.type(username, delay=30)
                        account_filled = True
                        self._account_selector = el
                        self.log("已填写账号（type模拟输入，默认第一个text输入框）")
                        break
                except:
                    pass

        if not password_filled:
            self.log("警告：未找到密码输入框")

        # 验证填写结果
        try:
            if self._account_selector:
                actual_val = self._account_selector.input_value()
                self.log(f"【调试】账号填写验证: 期望={username}, 实际={actual_val}, 匹配={actual_val == username}")
            if self._password_selector:
                actual_pwd = self._password_selector.input_value()
                self.log(f"【调试】密码填写验证: 期望={password}, 实际={actual_pwd}, 匹配={actual_pwd == password}")
        except Exception as e:
            self.log(f"【调试】填写验证出错: {str(e)[:50]}")

        self.log("账号密码填写完成")

    def _browser_get_captcha_impl(self, username, password, login_url=None, manual_input=False):
        """浏览器获取验证码的实际实现
        manual_input=True 时不自动填写账号密码，等待用户在浏览器里手动输入（60秒）
        """
        if not HAS_PLAYWRIGHT:
            return {"success": False, "error": "未安装 playwright，请运行: pip install playwright && playwright install chromium"}
        try:
            self.log("启动浏览器...")
            self._pw = sync_playwright().start()
            # 手动输入模式下用有头模式（用户能看到浏览器窗口），其他情况用无头模式
            headless_mode = not manual_input
            if manual_input:
                self.log("【手动输入模式】浏览器将以有头模式启动，请在弹出的浏览器窗口中手动输入账号密码")
            # 添加反检测参数，避免被网站识别为自动化浏览器
            self._browser = self._pw.chromium.launch(
                headless=headless_mode,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                ]
            )
            self._page = self._browser.new_page(
                viewport={'width': 1440, 'height': 900},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            # 隐藏 webdriver 标志
            self._page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
            """)

            self._login_result = None
            self._login_success = False
            self._login_response_count = 0

            def on_response(resp):
                try:
                    url_lower = resp.url.lower()
                    # 增加更多的登录相关URL关键词匹配
                    login_keywords = ['login', 'auth', 'signin', 'token', 'dologin', 'checklogin', 'login_user', 'loginuser', 'user_login', 'sign_in', 'logon']
                    is_login_url = any(kw in url_lower for kw in login_keywords)
                    # 也检查POST请求的响应
                    is_post = resp.request.method in ('POST', 'PUT')
                    
                    if is_login_url or (is_post and resp.status == 200 and 'api' in url_lower):
                        if resp.status == 200:
                            try:
                                result_data = resp.json()
                                self._login_result = result_data
                                self._login_response_count += 1
                                self.log(f"【调试】捕获到响应 #{self._login_response_count}: URL={resp.url[:100]}")
                                self.log(f"【调试】响应内容: {str(result_data)[:200]}")
                                # 自动判断是否登录成功
                                if isinstance(result_data, dict):
                                    code = result_data.get('code', -1)
                                    msg = str(result_data.get('msg', ''))
                                    data = result_data.get('data')
                                    has_token = False
                                    if isinstance(data, dict):
                                        has_token = bool(data.get('accessToken') or data.get('token'))
                                    else:
                                        has_token = bool(result_data.get('accessToken') or result_data.get('token'))
                                    
                                    if code == 0 or code == 200 or has_token or ('成功' in msg and '失败' not in msg):
                                        self._login_success = True
                                        self.log(f"✅ 检测到登录成功！code={code}, msg={msg}, has_token={has_token}")
                            except Exception as e:
                                self.log(f"【调试】解析响应JSON失败: {str(e)[:50]}, URL={resp.url[:80]}")
                except Exception as e:
                    self.log(f"【调试】on_response出错: {str(e)[:50]}")

            self._page.on('response', on_response)

            # 监听登录请求，输出实际发送的参数
            def on_request(request):
                try:
                    url_lower = request.url.lower()
                    if any(kw in url_lower for kw in ['login', 'auth', 'signin', 'dologin', 'checklogin']):
                        if request.method in ('POST', 'PUT'):
                            self.log(f"【调试】发送登录请求: URL={request.url[:100]}")
                            self.log(f"【调试】请求方法: {request.method}")
                            if request.post_data:
                                self.log(f"【调试】请求参数: {request.post_data[:500]}")
                except:
                    pass
            self._page.on('request', on_request)

            if not login_url:
                return {"success": False, "error": "未提供登录URL，请在来源管理或厂商管理中配置登录页URL"}
            url = login_url
            self.log("打开登录页: " + url)
            try:
                self._page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except Exception as e:
                self.log(f"页面加载警告: {str(e)[:80]}（继续尝试）")
            # 等待页面渲染完成
            self._page.wait_for_timeout(3000)
            try:
                self._page.wait_for_selector('input', timeout=10000)
            except:
                pass
            self._page.wait_for_timeout(1000)

            # 手动输入模式：不自动填写账号密码，等待用户在浏览器里手动输入（60秒）
            skip_auto_fill = False
            if manual_input:
                self.log("【手动输入模式】请在浏览器里手动输入账号、密码和验证码，然后点击登录（60秒内完成）...")
                self.log("【手动输入模式】浏览器窗口已打开，请切换到浏览器窗口操作")
                self._login_success = False
                self._login_result = None
                self._login_response_count = 0
                initial_url = self._page.url
                self.log(f"【手动输入模式】初始页面URL: {initial_url[:80]}")
                # 等待60秒，让用户手动输入账号密码验证码并点击登录
                for i in range(60):
                    self._page.wait_for_timeout(1000)
                    # 检查1：是否已经登录成功（on_response里设置）
                    if self._login_success:
                        self.log(f"【手动输入模式】检测到登录成功（第{i+1}秒）！")
                        break
                    # 检查2：页面URL是否变化（登录成功后通常会跳转）
                    try:
                        current_url = self._page.url
                        if current_url != initial_url and 'login' not in current_url.lower():
                            self.log(f"【手动输入模式】检测到页面跳转（第{i+1}秒）: {current_url[:80]}")
                            # 页面跳转了，可能已经登录成功，再等2秒让响应完成
                            self._page.wait_for_timeout(2000)
                            if self._login_result:
                                self._login_success = True
                                self.log("【手动输入模式】页面跳转且有响应数据，判定登录成功")
                                break
                    except:
                        pass
                    # 检查3：每10秒输出状态
                    if i % 10 == 9:
                        self.log(f"【手动输入模式】还剩 {60 - i - 1} 秒... (已捕获响应: {self._login_response_count})")
                
                # 如果已经登录成功，直接返回成功
                if self._login_success and self._login_result:
                    self.log("【手动输入模式】登录成功，正在保存token...")
                    try:
                        result_data = self._login_result
                        self.log(f"【手动输入模式】登录响应数据: {str(result_data)[:300]}")
                        # 尝试从响应中提取token（支持多种格式）
                        access_token = None
                        if isinstance(result_data, dict):
                            # 格式1: data.accessToken
                            data = result_data.get('data')
                            if isinstance(data, dict):
                                access_token = data.get('accessToken') or data.get('token') or data.get('access_token')
                            # 格式2: 直接在根目录
                            if not access_token:
                                access_token = result_data.get('accessToken') or result_data.get('token') or result_data.get('access_token')
                            # 格式3: data.token
                            if not access_token and isinstance(data, dict):
                                access_token = data.get('token')
                        if access_token:
                            self.log(f"【手动输入模式】获取到token: {str(access_token)[:30]}...")
                            # 关闭浏览器
                            try:
                                self._browser.close()
                                self._pw.stop()
                            except:
                                pass
                            return {"success": True, "access_token": access_token, "raw_response": result_data}
                        else:
                            self.log("【手动输入模式】未从响应中提取到token，继续验证码流程")
                            self.log(f"【手动输入模式】响应数据keys: {list(result_data.keys()) if isinstance(result_data, dict) else 'not dict'}")
                    except Exception as e:
                        self.log(f"【手动输入模式】处理登录结果出错: {str(e)[:50]}，继续验证码流程")
                
                self.log("【手动输入模式】等待结束，继续获取验证码...")
                skip_auto_fill = True
                # 手动输入模式下，不保存账号密码（避免覆盖用户输入）
                self._browser_username = None
                self._browser_password = None

            if not skip_auto_fill:
                # 保存账号密码，提交前重新填写（确保React state正确）
                self._browser_username = username
                self._browser_password = password
                self.log(f"【调试】准备填写账号密码: 账号={username}, 密码={password}")

                # 填写账号密码（用 type 模拟真实键盘输入，确保触发 React onChange）
                account_inputs = self._page.query_selector_all('input')
                account_filled = False
                password_filled = False
                self._account_selector = None
                self._password_selector = None

            if not skip_auto_fill:
                self._fill_account_password(username, password)

            # 截图验证码（找img标签、canvas、背景图）
            img_path = None

            # 1. 找 img 标签
            imgs = self._page.query_selector_all('img')
            for img in imgs:
                try:
                    src = img.get_attribute('src') or ''
                    if any(kw in src.lower() for kw in ['verify', 'captcha', 'code', 'check']):
                        img_name = 'browser_captcha_%s.png' % datetime.now().strftime('%Y%m%d_%H%M%S')
                        img_path = str(CAPTCHA_DIR / img_name)
                        img.screenshot(path=img_path)
                        self.log("验证码已截图(img): " + img_name)
                        break
                except:
                    pass

            # 2. 找 canvas 标签
            if not img_path:
                canvases = self._page.query_selector_all('canvas')
                for canvas in canvases:
                    try:
                        img_name = 'browser_captcha_%s.png' % datetime.now().strftime('%Y%m%d_%H%M%S')
                        img_path = str(CAPTCHA_DIR / img_name)
                        canvas.screenshot(path=img_path)
                        self.log("验证码已截图(canvas): " + img_name)
                        break
                    except:
                        pass

            # 3. 找有验证码背景图的元素
            if not img_path:
                elements = self._page.query_selector_all('[style*="background"]')
                for el in elements:
                    try:
                        style = el.get_attribute('style') or ''
                        if any(kw in style.lower() for kw in ['verify', 'captcha', 'code']):
                            img_name = 'browser_captcha_%s.png' % datetime.now().strftime('%Y%m%d_%H%M%S')
                            img_path = str(CAPTCHA_DIR / img_name)
                            el.screenshot(path=img_path)
                            self.log("验证码已截图(背景图): " + img_name)
                            break
                    except:
                        pass

            # 4. 截图整个页面（兜底）
            if not img_path:
                img_name = 'browser_page_%s.png' % datetime.now().strftime('%Y%m%d_%H%M%S')
                img_path = str(CAPTCHA_DIR / img_name)
                self._page.screenshot(path=img_path)
                self.log("未找到验证码图片，已截图整个页面（请手动识别验证码位置）")

            return {"success": True, "image_path": img_path, "image_name": Path(img_path).name}
        except Exception as e:
            self.log("浏览器获取验证码异常: " + str(e))
            self._close_browser()
            return {"success": False, "error": str(e)}

    def browser_submit_captcha(self, code, auth_header=None, token_path=None):
        """在浏览器中填写验证码并点击登录，捕获登录结果（在浏览器线程里运行）"""
        return self._send_browser_command({
            'type': 'submit_captcha',
            'code': code,
            'auth_header': auth_header,
            'token_path': token_path,
        })

    def _browser_submit_captcha_impl(self, code, auth_header=None, token_path=None):
        """浏览器提交验证码的实际实现"""
        try:
            if not hasattr(self, '_page') or not self._page:
                return {"success": False, "error": "请先调用 browser_get_captcha"}

            # 重新填写账号密码（确保 React state 正确，避免受控组件值丢失）
            self.log(f"【调试】提交前重新确认账号密码: 账号={getattr(self, '_browser_username', '')}, 密码={getattr(self, '_browser_password', '')}")
            try:
                all_inputs = self._page.query_selector_all('input')
                for el in all_inputs:
                    try:
                        inp_type = (el.get_attribute('type') or 'text').lower()
                        ph = (el.get_attribute('placeholder') or '').lower()
                        id_attr = (el.get_attribute('id') or '').lower()
                        if inp_type == 'password':
                            current_val = el.input_value()
                            expected_pwd = getattr(self, '_browser_password', '')
                            self.log(f"【调试】密码框当前值={current_val}, 期望值={expected_pwd}")
                            if current_val != expected_pwd:
                                el.click()
                                el.fill('')
                                el.type(expected_pwd, delay=30)
                                self.log(f"【调试】已重新填写密码, 新值={el.input_value()}")
                        elif inp_type in ('text', 'email', 'tel') and '验证码' not in ph and 'code' not in ph:
                            current_val = el.input_value()
                            expected_user = getattr(self, '_browser_username', '')
                            self.log(f"【调试】账号框当前值={current_val}, 期望值={expected_user}")
                            if current_val != expected_user:
                                el.click()
                                el.fill('')
                                el.type(expected_user, delay=30)
                                self.log(f"【调试】已重新填写账号, 新值={el.input_value()}")
                    except Exception as e:
                        self.log(f"【调试】处理输入框出错: {str(e)[:50]}")
            except Exception as e:
                self.log(f"重新填写账号密码时出错: {str(e)[:50]}")

            self.log("填写验证码: " + code)
            # 验证码：优先 placeholder 包含"验证码"，排除账号输入框
            code_inputs = self._page.query_selector_all('input')
            code_filled = False

            for el in code_inputs:
                try:
                    inp_type = (el.get_attribute('type') or 'text').lower()
                    ph = (el.get_attribute('placeholder') or '').lower()
                    nm = (el.get_attribute('name') or '').lower()
                    id_attr = (el.get_attribute('id') or '').lower()
                    if inp_type in ('text', 'tel') and inp_type != 'password':
                        if any(kw in ph for kw in ['验证码', 'code', 'verify']) or any(kw in nm for kw in ['code', 'verify', 'captcha']) or 'verify' in id_attr or 'code' in id_attr:
                            el.click()
                            el.type(code, delay=50)
                            code_filled = True
                            self.log("已填写验证码（type模拟输入，选择器匹配）")
                            break
                except:
                    pass

            # 如果没找到，用最后一个 text 输入框（通常验证码在最后）
            if not code_filled:
                text_inputs = [el for el in code_inputs if (el.get_attribute('type') or 'text').lower() in ('text', 'tel')]
                if text_inputs:
                    try:
                        text_inputs[-1].click()
                        text_inputs[-1].type(code, delay=50)
                        code_filled = True
                        self.log("已填写验证码（type模拟输入，默认最后一个text输入框）")
                    except:
                        pass

            if not code_filled:
                self.log("警告：未找到验证码输入框，尝试直接点击登录")

            # 记录当前URL，用于检测登录跳转
            old_url = self._page.url
            self.log(f"当前页面URL: {old_url[:80]}")

            # 点击登录前最终验证
            try:
                final_inputs = self._page.query_selector_all('input')
                for i, el in enumerate(final_inputs):
                    inp_type = (el.get_attribute('type') or 'text').lower()
                    ph = (el.get_attribute('placeholder') or '')
                    val = el.input_value()
                    self.log(f"【调试】点击登录前 输入框[{i}] type={inp_type}, placeholder={ph}, value={val}")
            except Exception as e:
                self.log(f"【调试】最终验证出错: {str(e)[:50]}")

            # 点击登录按钮
            login_clicked = False
            for sel in [
                'button:has-text("登录")',
                'button:has-text("登 录")',
                'button:has-text("立即登录")',
                'button.login-btn',
                'button[type="submit"]',
                '.login-btn',
                '#login-btn',
                'input[type="submit"]',
            ]:
                try:
                    btn = self._page.query_selector(sel)
                    if btn:
                        btn.click()
                        login_clicked = True
                        self.log(f"已点击登录按钮（选择器: {sel}）")
                        break
                except:
                    pass

            if not login_clicked:
                # 尝试按回车键提交
                try:
                    self._page.keyboard.press('Enter')
                    login_clicked = True
                    self.log("未找到登录按钮，已按回车键提交")
                except:
                    pass

            # 等待登录响应（最多等待15秒，检测URL变化或登录响应）
            self.log("等待登录响应...")
            for i in range(15):
                self._page.wait_for_timeout(1000)
                # 检测URL变化（登录成功通常会跳转）
                new_url = self._page.url
                if new_url != old_url and 'login' not in new_url.lower():
                    self.log(f"检测到页面跳转: {new_url[:80]}")
                    self._login_success = True
                    break
                # 检测是否有登录响应
                if getattr(self, '_login_result', None):
                    self.log("检测到登录响应")
                    break
                # 检测页面是否有错误提示
                try:
                    error_text = self._page.evaluate("""() => {
                        const els = document.querySelectorAll('.error, .alert, .message, .tip, .toast');
                        for (const el of els) {
                            if (el.offsetParent !== null && el.textContent.trim()) {
                                return el.textContent.trim().substring(0, 100);
                            }
                        }
                        return null;
                    }""")
                    if error_text and any(kw in error_text for kw in ['错误', '失败', '不存在', '密码', '验证码']):
                        self.log(f"检测到错误提示: {error_text}")
                        break
                except:
                    pass

            # 从多个来源获取登录结果
            result = getattr(self, '_login_result', None)

            # 1. 从 localStorage 获取 token
            if not result or not get_nested_value(result, token_path or "data.accessToken"):
                try:
                    tokens = self._page.evaluate("""() => {
                        const results = [];
                        for (const k of Object.keys(localStorage)) {
                            const v = localStorage.getItem(k);
                            if (v && v.length > 20 && /^[A-Za-z0-9+/=._-]+$/.test(v)) {
                                results.push({key: k, value: v});
                            }
                        }
                        for (const k of Object.keys(sessionStorage)) {
                            const v = sessionStorage.getItem(k);
                            if (v && v.length > 20 && /^[A-Za-z0-9+/=._-]+$/.test(v)) {
                                results.push({key: k, value: v});
                            }
                        }
                        return results;
                    }""")
                    if tokens:
                        self.log(f"从存储中找到 {len(tokens)} 个可能的token")
                        # 优先找包含 token/access/auth 的 key
                        for t in tokens:
                            if any(kw in t['key'].lower() for kw in ['token', 'access', 'auth', 'jwt']):
                                result = {"code": 200, "data": {"access_token": t['value'], "accessToken": t['value']}}
                                self.log(f"使用存储中的token: {t['key']}")
                                break
                        # 如果没找到，用第一个
                        if not result and tokens:
                            result = {"code": 200, "data": {"access_token": tokens[0]['value'], "accessToken": tokens[0]['value']}}
                            self.log(f"使用存储中的第一个token: {tokens[0]['key']}")
                except Exception as e:
                    self.log(f"从存储获取token失败: {str(e)[:50]}")

            # 2. 从 cookie 获取 token
            if not result or not get_nested_value(result, token_path or "data.accessToken"):
                try:
                    cookies = self._page.context.cookies()
                    for c in cookies:
                        if any(kw in c['name'].lower() for kw in ['token', 'access', 'auth', 'session', 'jwt']):
                            if len(c['value']) > 20:
                                result = {"code": 200, "data": {"access_token": c['value'], "accessToken": c['value']}}
                                self.log(f"从cookie获取token: {c['name']}")
                                break
                except:
                    pass

            self._close_browser()

            if not result:
                return {"success": False, "error": "未捕获到登录响应，请检查网络或账号密码"}

            code_val = result.get("code")
            token = get_nested_value(result, token_path or "data.accessToken") or \
                    get_nested_value(result, "data.access_token") or \
                    get_nested_value(result, "token")

            if code_val not in (1, 200, "1", "200") and not token:
                msg = result.get("msg") or result.get("message") or "未知错误"
                self.log("登录失败: " + str(msg))
                return {"success": False, "error": str(msg), "raw_response": result}

            if not token:
                return {"success": False, "error": "登录响应中没有token", "raw_response": result}

            self.log("登录成功! token: " + str(token)[:20] + "...")
            self.session.headers[auth_header or "access-token"] = str(token)
            # 清理可能冲突的其他认证头
            for h in ["access-token", "Authorization"]:
                if h != (auth_header or "access-token"):
                    self.session.headers.pop(h, None)

            return {"success": True, "access_token": str(token), "user_info": result.get("data", {})}
        except Exception as e:
            self.log("浏览器登录异常: " + str(e))
            self._close_browser()
            return {"success": False, "error": str(e)}

    def _close_browser(self):
        """关闭浏览器"""
        try:
            if hasattr(self, '_browser') and self._browser:
                self._browser.close()
        except:
            pass
        try:
            if hasattr(self, '_pw') and self._pw:
                self._pw.stop()
        except:
            pass
        self._page = None
        self._browser = None
        self._pw = None

    def get_logs(self):
        """获取日志"""
        return self.logs


# ==================== 自动抓包引擎 ====================

class Sniffer:
    """自动抓包引擎：用 Playwright 打开浏览器，监听网络请求，自动分析生成厂商配置"""
    def __init__(self):
        self._pw = None
        self._browser = None
        self._page = None
        self._thread = None
        self._requests = []
        self._logs = []
        self._status = "idle"
        self._status_msg = ""
        self._login_config = None
        self._data_apis = {}
        self._data_pages = []
        self._vendor_name = ""
        self._login_url = ""
        self._username = ""
        self._password = ""
        self._api_base = ""
        self._lock = threading.RLock()
        self._responses = []  # 缓存响应数据，用于分析数据路径

    def _log(self, msg):
        """记录日志"""
        time_str = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._logs.append(f"[{time_str}] {msg}")
            if len(self._logs) > 200:
                self._logs = self._logs[-200:]

    def start(self, name, login_url, username, password, data_pages=None):
        if self._status not in ("idle", "done", "error"):
            return {"success": False, "error": "抓包正在进行中，请先停止"}
        if not HAS_PLAYWRIGHT:
            return {"success": False, "error": "Playwright未安装，请运行: pip install playwright && playwright install"}
        self._vendor_name = name
        self._login_url = login_url
        self._username = username
        self._password = password
        self._data_pages = data_pages or []
        self._requests = []
        self._responses = []
        self._logs = []
        self._login_config = None
        self._data_apis = {}
        self._status = "starting"
        self._status_msg = "正在启动浏览器..."
        self._log("开始抓包：" + name)
        self._log("登录页：" + login_url)
        self._log("数据页面：" + ", ".join([p.get("name","") for p in self._data_pages]))
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return {"success": True}

    def _run(self):
        try:
            from urllib.parse import urlparse
            self._log("正在启动 Playwright 浏览器...")
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=False)
            self._page = self._browser.new_page()
            self._page.on("request", self._on_request)
            self._page.on("response", self._on_response)
            parsed = urlparse(self._login_url)
            self._api_base = f"{parsed.scheme}://{parsed.netloc}"
            self._status = "waiting_login"
            self._status_msg = "浏览器已打开，正在加载登录页，请在浏览器中手动登录"
            self._log("浏览器已打开，正在加载登录页...")
            try:
                self._page.goto(self._login_url, wait_until="domcontentloaded", timeout=30000)
                self._log("登录页加载完成")
            except Exception as e:
                self._log(f"页面加载警告: {str(e)[:100]}（继续尝试）")
            # 等待页面渲染完成（SPA应用需要等JS渲染）
            self._log("等待页面渲染...")
            try:
                self._page.wait_for_selector('input', timeout=15000)
                self._log("页面渲染完成，找到输入框")
            except Exception:
                self._log("等待输入框超时，继续尝试填写")
            # 尝试自动填写账号密码
            try:
                filled_user = False
                user_selectors = [
                    'input[name*="user"]', 'input[name*="name"]', 'input[name*="account"]',
                    'input[placeholder*="账号"]', 'input[placeholder*="用户名"]', 'input[placeholder*="账号"]',
                    'input[placeholder*="手机"]', 'input[placeholder*="邮箱"]',
                    'input[type="text"]', 'input[type="tel"]', 'input:not([type])',
                ]
                for sel in user_selectors:
                    try:
                        el = self._page.query_selector(sel)
                        if el and el.is_visible():
                            el.click()
                            time.sleep(0.3)
                            el.fill(self._username)
                            filled_user = True
                            self._log(f"已自动填写账号（选择器: {sel}）")
                            break
                    except Exception:
                        continue
                if not filled_user:
                    self._log("未能自动填写账号，请手动填写")
                filled_pwd = False
                pwd_selectors = [
                    'input[name*="pass"]', 'input[name*="pwd"]',
                    'input[placeholder*="密码"]', 'input[placeholder*="口令"]',
                    'input[type="password"]',
                ]
                for sel in pwd_selectors:
                    try:
                        el = self._page.query_selector(sel)
                        if el and el.is_visible():
                            el.click()
                            time.sleep(0.3)
                            el.fill(self._password)
                            filled_pwd = True
                            self._log(f"已自动填写密码（选择器: {sel}）")
                            break
                    except Exception:
                        continue
                if not filled_pwd:
                    self._log("未能自动填写密码，请手动填写")
            except Exception as e:
                self._log(f"自动填写失败: {str(e)[:100]}，请手动填写")
            self._log("等待用户在浏览器中登录（可手动输入账号密码验证码，点登录）...")
            # 记录初始URL，用于判断页面跳转
            initial_url = self._page.url
            # 等待登录成功（最多等180秒）：通过API响应识别 或 页面跳转识别 或 手动确认
            # 注意：必须用 page.wait_for_timeout() 而不是 time.sleep()，否则 Playwright 事件循环会停止
            for i in range(90):
                try:
                    self._page.wait_for_timeout(2000)
                except Exception:
                    time.sleep(2)
                if self._status == "login_success":
                    break
                if self._status == "error":
                    return
                # 检查页面是否跳转（登录成功后通常会跳转到其他页面）
                try:
                    current_url = self._page.url
                    if current_url != initial_url and 'login' not in current_url.lower():
                        self._log(f"检测到页面跳转: {current_url}")
                        if self._status != "login_success":
                            self._status = "login_success"
                            self._status_msg = f"登录成功（检测到页面跳转）"
                            self._log("登录成功（检测到页面跳转）")
                            if not self._login_config:
                                self._login_config = {
                                    "url": "/user/login",
                                    "method": "POST",
                                    "content_type": "json",
                                    "username_field": "username",
                                    "password_field": "password",
                                    "token_path": "data.access_token",
                                    "auth_header": "Authorization",
                                    "auth_prefix": "Bearer ",
                                    "has_captcha": False,
                                }
                                self._log("未识别到登录接口，使用默认配置，请在厂商管理中修改")
                        break
                except Exception:
                    pass
            if self._status != "login_success":
                self._log("等待登录超时，请检查是否登录成功")
                self._status_msg = "等待登录超时，请在浏览器中完成登录后点击'我已登录'按钮"
                return
            # 登录成功后，自动访问数据页面
            self._status = "capturing"
            self._status_msg = "登录成功，正在自动访问数据页面..."
            self._log("登录成功！开始自动访问数据页面...")
            for page_info in self._data_pages:
                page_name = page_info.get("name", "")
                page_url = page_info.get("url", "")
                if not page_url:
                    continue
                self._log(f"访问数据页面: {page_name} - {page_url}")
                try:
                    # 确保页面对象有效
                    self._page.bring_to_front()
                    self._page.goto(page_url, wait_until="domcontentloaded", timeout=20000)
                    # 等待页面渲染和API请求完成
                    self._page.wait_for_timeout(2000)
                    try:
                        self._page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    self._page.wait_for_timeout(2000)
                    self._log(f"页面 {page_name} 加载完成")
                except Exception as e:
                    self._log(f"访问 {page_name} 失败: {str(e)[:150]}")
            self._status = "waiting_data"
            self._status_msg = "数据页面访问完成，点击'捕获数据接口'分析，或直接'生成厂商配置'"
            self._log("所有数据页面访问完成，可以点击'捕获数据接口'或'生成厂商配置'")
        except Exception as e:
            self._status = "error"
            self._status_msg = f"启动失败: {str(e)[:200]}"
            self._log(f"启动失败: {str(e)[:200]}")

    def _on_request(self, request):
        try:
            url = request.url
            if any(url.endswith(ext) for ext in ['.png','.jpg','.jpeg','.gif','.css','.js','.ico','.svg','.woff','.woff2','.ttf']):
                return
            if 'captcha' in url.lower() or 'verify' in url.lower():
                return
            req_data = {"url": url, "method": request.method, "post_data": request.post_data or "",
                        "resource_type": request.resource_type, "time": datetime.now().strftime("%H:%M:%S")}
            with self._lock:
                self._requests.append(req_data)
                if len(self._requests) > 100:
                    self._requests = self._requests[-100:]
        except Exception:
            pass

    def _on_response(self, response):
        try:
            url = response.url
            if any(url.endswith(ext) for ext in ['.png','.jpg','.jpeg','.gif','.css','.js','.ico','.svg','.woff','.woff2','.ttf']):
                return
            if response.status != 200:
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                body = response.json()
            except Exception:
                return
            # 缓存响应数据（用于分析数据路径）
            with self._lock:
                self._responses.append({"url": url, "body": body})
                if len(self._responses) > 50:
                    self._responses = self._responses[-50:]
            if self._status == "waiting_login":
                login_cfg = self._analyze_login_response(url, response.request.method, body)
                if login_cfg:
                    from urllib.parse import urlparse
                    # 1. 用登录请求的真实域名更新 api_base（而不是用登录页URL）
                    parsed = urlparse(url)
                    real_api_base = f"{parsed.scheme}://{parsed.netloc}"
                    if real_api_base != self._api_base:
                        self._api_base = real_api_base
                        self._log(f"  自动识别API地址: {real_api_base}")
                    
                    # 2. 检测是否有验证码（从登录请求的post_data中判断）
                    has_captcha = False
                    captcha_cfg = {}
                    with self._lock:
                        for req in reversed(self._requests):
                            if req["url"] == url and req["method"] == response.request.method:
                                if req["post_data"]:
                                    try:
                                        pd = json.loads(req["post_data"])
                                        for k in pd.keys():
                                            if any(kw in k.lower() for kw in ['captcha','verify','code','verkey','verifycode','verify_code']):
                                                has_captcha = True
                                                break
                                    except Exception:
                                        pass
                                break
                    
                    # 3. 从捕获的请求中识别验证码配置
                    if has_captcha:
                        captcha_cfg = self._detect_captcha_config()
                        if captcha_cfg:
                            login_cfg["captcha"] = captcha_cfg
                            self._log(f"  自动识别验证码配置: {captcha_cfg.get('key_url','直接图片')}")
                    
                    login_cfg["has_captcha"] = has_captcha
                    self._login_config = login_cfg
                    self._status = "login_success"
                    self._status_msg = f"登录成功！已识别登录接口: {url}"
                    self._log(f"登录成功！已识别登录接口: {url}")
                    self._log(f"  方法: {response.request.method}, token路径: {login_cfg.get('token_path','')}")
                    if has_captcha:
                        self._log(f"  检测到验证码，已自动配置")
        except Exception:
            pass

    def _detect_captcha_config(self):
        """从捕获的请求中自动识别验证码配置"""
        from urllib.parse import urlparse, parse_qs
        key_url = None
        img_url = None
        verkey_field = "verifyKey"
        code_field = "verifyCode"
        
        with self._lock:
            for req in self._requests:
                url = req["url"]
                url_lower = url.lower()
                # 识别 key 接口（返回 verkey 的接口）
                if any(kw in url_lower for kw in ['verify/key', 'captcha/key', 'getkey', 'verkey']):
                    parsed = urlparse(url)
                    key_url = parsed.path
                    if parsed.query:
                        key_url += "?" + parsed.query
                # 识别 img 接口（返回验证码图片的接口）
                if any(kw in url_lower for kw in ['verify/img', 'captcha/img', 'captcha.jpg', 'captcha.png', 'verify.jpg', 'verify.png', 'getcaptcha', 'code.jpg', 'code.png']):
                    parsed = urlparse(url)
                    img_path = parsed.path
                    # 检查 URL 参数中是否有 verkey
                    qs = parse_qs(parsed.query)
                    if 'verkey' in qs or 'verKey' in qs:
                        img_url = img_path + "?time={time}&verKey={verkey}"
                    else:
                        img_url = img_path
        
        # 判断模式
        if key_url and img_url:
            # key+img 模式
            return {"key_url": key_url, "img_url": img_url, "verkey_field": verkey_field, "code_field": code_field}
        elif img_url:
            # 直接图片模式
            return {"url": img_url, "verkey_field": verkey_field, "code_field": code_field}
        elif key_url:
            # 只有 key，用默认 img 路径
            return {"key_url": key_url, "img_url": "/verify/img?time={time}&verKey={verkey}", "verkey_field": verkey_field, "code_field": code_field}
        
        # 没识别到，用默认的 key+img 模式（大多数游戏平台都是这种）
        return {"key_url": "/verify/key", "img_url": "/verify/img?time={time}&verKey={verkey}", "verkey_field": verkey_field, "code_field": code_field}

    def _analyze_login_response(self, url, method, body):
        from urllib.parse import urlparse
        url_lower = url.lower()
        is_login_url = any(kw in url_lower for kw in ['login','auth','signin','token','dologin','checklogin','validate','authenticate'])
        token_path = self._find_token_path(body)
        if not token_path:
            return None
        # 登录URL 或者 响应里有token且URL包含api/接口关键字
        if not is_login_url and 'token' not in token_path.lower():
            # 如果不是登录URL但响应里有明显的token，也认为是登录响应
            if not any(kw in url_lower for kw in ['api','interface','user','account']):
                return None
        parsed = urlparse(url)
        api_path = parsed.path
        if parsed.query:
            api_path += "?" + parsed.query
        content_type = "json"
        username_field = "username"
        password_field = "password"
        with self._lock:
            for req in reversed(self._requests):
                if req["url"] == url and req["method"] == method:
                    if req["post_data"]:
                        try:
                            pd = json.loads(req["post_data"])
                            for k in pd.keys():
                                if any(kw in k.lower() for kw in ['user','name','account','login','mobile','phone','email']):
                                    username_field = k
                                    break
                            for k in pd.keys():
                                if any(kw in k.lower() for kw in ['pass','pwd','secret','password']):
                                    password_field = k
                                    break
                        except Exception:
                            content_type = "form"
                    break
        auth_header = "access-token"
        auth_prefix = ""
        if 'access_token' in token_path.lower() or 'jwt' in token_path.lower() or 'bearer' in token_path.lower():
            auth_header = "Authorization"
            auth_prefix = "Bearer "
        return {"url": api_path, "method": method, "content_type": content_type,
                "username_field": username_field, "password_field": password_field,
                "token_path": token_path, "auth_header": auth_header, "auth_prefix": auth_prefix,
                "has_captcha": False}

    def _find_token_path(self, obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                path = f"{prefix}.{k}" if prefix else k
                kl = k.lower()
                # 常见的 token 字段名
                token_keys = ['token','accesstoken','access_token','jwt','auth_token','bearer',
                              'id_token','refreshtoken','refresh_token','sessionid','session_id',
                              'ticket','auth','securitytoken','security_token']
                if kl in token_keys:
                    if isinstance(v, str) and len(v) > 10:
                        return path
                found = self._find_token_path(v, path)
                if found:
                    return found
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                found = self._find_token_path(item, f"{prefix}[{i}]")
                if found:
                    return found
        return None

    def confirm_login(self):
        """用户手动确认登录成功（当自动检测失败时使用）"""
        if self._status != "waiting_login":
            return {"success": False, "error": "当前不是等待登录状态（当前状态: %s）" % self._status}
        self._status = "login_success"
        self._status_msg = "用户手动确认登录成功"
        self._log("用户手动确认登录成功")
        if not self._login_config:
            self._login_config = {
                "url": "/user/login",
                "method": "POST",
                "content_type": "json",
                "username_field": "username",
                "password_field": "password",
                "token_path": "data.access_token",
                "auth_header": "Authorization",
                "auth_prefix": "Bearer ",
                "has_captcha": False,
            }
            self._log("未自动识别到登录接口，使用默认配置，请在厂商管理中修改")
        return {"success": True}

    def _find_data_path(self, body, prefix=""):
        """从响应JSON中递归查找包含数组的字段路径（数据路径）"""
        if isinstance(body, dict):
            for k, v in body.items():
                path = f"{prefix}.{k}" if prefix else k
                if isinstance(v, list) and len(v) > 0:
                    # 找到数组，返回路径
                    return path
                if isinstance(v, dict):
                    result = self._find_data_path(v, path)
                    if result:
                        return result
        return None

    def _find_total_path(self, body, prefix=""):
        """从响应JSON中递归查找总数字段"""
        if isinstance(body, dict):
            for k, v in body.items():
                path = f"{prefix}.{k}" if prefix else k
                kl = k.lower()
                if kl in ['total', 'totalcount', 'total_count', 'count', 'sum'] and isinstance(v, (int, float)):
                    return path
                if isinstance(v, dict):
                    result = self._find_total_path(v, path)
                    if result:
                        return result
        return None

    def capture_data(self):
        if self._status not in ("login_success", "waiting_data", "capturing"):
            return {"success": False, "error": "请先完成登录"}
        self._status = "waiting_data"
        self._status_msg = "正在分析数据接口..."
        self._log("开始分析数据接口...")
        from urllib.parse import urlparse
        data_apis = {}
        skipped = 0
        with self._lock:
            for req in reversed(self._requests):
                url = req["url"]
                if any(kw in url.lower() for kw in ['login','auth','signin','token','captcha','verify','logout']):
                    skipped += 1
                    continue
                # 放宽 resource_type 过滤：除了静态资源，都尝试识别
                if req["resource_type"] not in ('xhr', 'fetch', 'other', 'document'):
                    skipped += 1
                    continue
                parsed = urlparse(url)
                api_path = parsed.path
                # PHP 后台（index.php?g=&m=&a= 等）：接口靠 query 区分，保留完整 URL 用于识别与抓取
                is_php_backend = ('.php' in api_path) and bool(parsed.query)
                # 跳过页面本身（document 类型且非 PHP 后台、路径不含 api）
                if req["resource_type"] == 'document' and not is_php_backend and '/api/' not in api_path:
                    skipped += 1
                    continue
                # PHP 后台：用完整 URL（含 query）作为接口标识，才能区分不同数据接口
                if is_php_backend:
                    api_path = url
                type_name = self._guess_data_type(api_path)
                if type_name and type_name not in data_apis:
                    # 从响应中自动识别数据路径和总数字段
                    data_path = "rows"
                    total_path = "total"
                    with self._lock:
                        for resp in reversed(self._responses):
                            if api_path in resp["url"]:
                                found_data = self._find_data_path(resp["body"])
                                found_total = self._find_total_path(resp["body"])
                                if found_data:
                                    data_path = found_data
                                if found_total:
                                    total_path = found_total
                                break
                    data_apis[type_name] = {"path": api_path, "method": req["method"],
                        "page_param": "pageNum", "page_size_param": "pageSize",
                        "data_path": data_path, "total_path": total_path}
                    self._log(f"  识别到数据接口: {type_name} -> {api_path} (data_path={data_path})")
        self._data_apis = data_apis
        if data_apis:
            self._status_msg = f"已识别{len(data_apis)}个数据接口：{', '.join(data_apis.keys())}"
        else:
            self._status_msg = f"未识别到数据接口（共{len(self._requests)}个请求，跳过{skipped}个），请在浏览器中点击数据页面后再试"
        return {"success": True, "count": len(data_apis), "apis": list(data_apis.keys()),
                "total_requests": len(self._requests), "skipped": skipped}

    def _guess_data_type(self, url):
        # 兼容 PHP 后台（ThinkPHP 风格 index.php?g=&m=&a=）：接口靠 query 区分，
        # 因此把 path + query 一起用于关键词判断，避免只看 path 导致全部无法识别
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            pl = (parsed.path + '?' + parsed.query).lower()
        except Exception:
            pl = url.lower()
        if any(kw in pl for kw in ['user','player','member','account','memberlist','users']): return "玩家账号"
        if any(kw in pl for kw in ['role','character','roles','rol']): return "角色列表"
        if any(kw in pl for kw in ['recharge','pay','order','charge','payment','recharges']): return "充值记录"
        if any(kw in pl for kw in ['game','product','games']): return "游戏列表"
        if any(kw in pl for kw in ['income','revenue','earn','profit']): return "收入统计"
        if any(kw in pl for kw in ['list','page','query','search','index']): return "数据列表"
        return None

    def generate_vendor(self):
        if not self._login_config:
            return {"success": False, "error": "未识别到登录接口，请先完成登录"}
        # 如果数据接口为空，自动尝试捕获
        if not self._data_apis:
            self._log("正在自动捕获数据接口...")
            result = self.capture_data()
            if result.get("success"):
                self._log(f"捕获完成：共{result.get('total_requests',0)}个请求，识别到{result.get('count',0)}个数据接口")
                if result.get("apis"):
                    self._log("  识别到的接口：" + "、".join(result["apis"]))
                else:
                    self._log("  警告：未识别到数据接口，生成的配置可能需要手动补充")
        existing = None
        for v in storage.get_vendors():
            if v["name"] == self._vendor_name:
                existing = v
                break
        # 若本次未识别到数据接口且该厂商已有配置，保留原有 data_apis，避免把已有接口配置清空
        save_data_apis = self._data_apis
        if existing and not save_data_apis:
            save_data_apis = existing.get("data_apis") or {}
        login_config = self._login_config.copy()
        if existing:
            storage.update_vendor(existing["id"], name=self._vendor_name, login_url=self._login_url,
                api_base_url=self._api_base, login_config=json.dumps(login_config, ensure_ascii=False),
                data_apis=json.dumps(save_data_apis, ensure_ascii=False), remark="自动抓包生成")
            vid = existing["id"]
            action = "更新"
        else:
            vid = storage.add_vendor(name=self._vendor_name, login_url=self._login_url,
                api_base_url=self._api_base, login_config=json.dumps(login_config, ensure_ascii=False),
                data_apis=json.dumps(save_data_apis, ensure_ascii=False), remark="自动抓包生成")
            action = "添加"
        self._status = "done"
        api_count = len(save_data_apis)
        warning = ""
        if api_count == 0:
            warning = "未识别到数据接口，仅保存了登录配置；请重新抓包并在各数据页面停留片刻，或在厂商管理中手动补充数据接口配置"
            self._status_msg = f"厂商配置已{action}！ID={vid}，但未识别到数据接口（需手动补充）"
        else:
            self._status_msg = f"厂商配置已{action}！ID={vid}，识别到{api_count}个数据接口，可在厂商管理中查看和修改"
        return {"success": True, "id": vid, "action": action,
                "data_api_count": api_count, "warning": warning}

    def get_status(self):
        with self._lock:
            logs = list(self._logs)
        return {"status": self._status, "message": self._status_msg, "vendor_name": self._vendor_name,
                "login_config": self._login_config,
                "data_apis": list(self._data_apis.keys()) if self._data_apis else [],
                "request_count": len(self._requests), "logs": logs}

    def get_requests(self):
        with self._lock:
            return list(self._requests)

    def stop(self):
        self._log("正在停止抓包...")
        try:
            if self._page:
                self._page.close()
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception as e:
            self._log(f"关闭浏览器时出错: {str(e)[:100]}")
        self._page = None
        self._browser = None
        self._pw = None
        self._thread = None
        self._status = "idle"
        self._status_msg = "已停止"
        self._log("抓包已停止")
        return {"success": True}


# 全局实例
crawler = ApiCrawler()
sniffer = Sniffer()
