# -*- coding: utf-8 -*-
"""
数据库存储模块（存储适配器）

默认使用 SQLite：嵌入式数据库，无需安装、复制即备份，与主项目完全隔离。
当环境变量 CRAWLER_STORAGE=mysql 时，自动切换为 MySQL 存储（见 storage_mysql.py），
两者接口完全一致，crawler.py / app.py 无需改动。

数据按"任务"组织：每次爬取 = 一个任务，每行数据都有记录。
"""
import os
import json
import sqlite3
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "crawler.db"   # 数据库文件


class Storage:
    """爬虫数据库操作类"""

    def __init__(self, db_path=None):
        self.db_path = str(db_path or DB_PATH)
        # 确保 data 目录存在
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ---------- 内部：连接与建表 ----------
    def _connect(self):
        """建立数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row   # 让查询结果可以按字段名取值
        return conn

    def _init_db(self):
        """初始化建表（不存在则创建）"""
        with self._connect() as conn:
            conn.executescript("""
                -- 厂商配置表：每个厂商一套API配置
                CREATE TABLE IF NOT EXISTS vendors (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    name          TEXT,                -- 厂商名称（如 熊猫游、游梦）
                    login_url     TEXT,                -- 登录页面URL
                    api_base_url  TEXT,                -- API基地址
                    login_config  TEXT,                -- 登录配置(JSON)
                    data_apis     TEXT,                -- 数据接口配置(JSON)
                    remark        TEXT,                -- 备注
                    created_at    TEXT                 -- 创建时间
                );

                -- 爬虫来源表：每个网址/账号 = 一个来源
                CREATE TABLE IF NOT EXISTS sources (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    vendor_id    INTEGER DEFAULT 0,   -- 所属厂商ID
                    name         TEXT,                -- 来源名称（如 熊猫游-代充渠道A）
                    url          TEXT,                -- 登录网址
                    username     TEXT,                -- 账号
                    password     TEXT,                -- 密码
                    access_token TEXT,                -- API访问令牌（抓包自动提取）
                    api_base_url TEXT,                -- API基地址（覆盖厂商配置，留空用厂商的）
                    api_list     TEXT,                -- 该来源支持的数据类型列表(JSON)（覆盖厂商配置）
                    auto_config  TEXT,                -- 自动爬取配置(JSON)
                    created_at   TEXT                 -- 创建时间
                );

                -- 爬取任务表：一次爬取 = 一条记录
                CREATE TABLE IF NOT EXISTS crawl_tasks (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id     INTEGER DEFAULT 0,  -- 所属来源ID(0=未归属)
                    source_url    TEXT,                -- 数据来源网址
                    task_name     TEXT DEFAULT '默认任务',  -- 任务名（注册用户/充值明细等）
                    table_selector TEXT,               -- 使用的表格选择器
                    row_count     INTEGER DEFAULT 0,   -- 本次爬取行数
                    created_at    TEXT                 -- 爬取时间
                );

                -- 爬取数据明细表：每行数据 = 一条记录
                CREATE TABLE IF NOT EXISTS crawl_data (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id    INTEGER,                -- 所属任务ID
                    row_index  INTEGER,                -- 行序号
                    data_json  TEXT,                   -- 该行数据(JSON格式)
                    FOREIGN KEY (task_id) REFERENCES crawl_tasks(id)
                );

                -- 索引：加快按任务查询数据的速度
                CREATE INDEX IF NOT EXISTS idx_data_task ON crawl_data (task_id);
            """)
            # 兼容旧库：crawl_tasks 若缺少 source_id / columns 列则补上
            cols = [r[1] for r in conn.execute("PRAGMA table_info(crawl_tasks)").fetchall()]
            if "source_id" not in cols:
                conn.execute("ALTER TABLE crawl_tasks ADD COLUMN source_id INTEGER DEFAULT 0")
            if "columns" not in cols:
                conn.execute("ALTER TABLE crawl_tasks ADD COLUMN columns TEXT DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_source ON crawl_tasks (source_id)")
            # 兼容旧库：sources 表补 access_token / api_base_url 列
            scols = [r[1] for r in conn.execute("PRAGMA table_info(sources)").fetchall()]
            if "access_token" not in scols:
                conn.execute("ALTER TABLE sources ADD COLUMN access_token TEXT DEFAULT ''")
            if "api_base_url" not in scols:
                conn.execute("ALTER TABLE sources ADD COLUMN api_base_url TEXT DEFAULT ''")
            if "api_list" not in scols:
                conn.execute("ALTER TABLE sources ADD COLUMN api_list TEXT DEFAULT ''")
            if "vendor_id" not in scols:
                conn.execute("ALTER TABLE sources ADD COLUMN vendor_id INTEGER DEFAULT 0")
            conn.commit()

            # 预设厂商：熊猫游、游梦（如果不存在）
            cur = conn.execute("SELECT COUNT(*) as c FROM vendors")
            if cur.fetchone()["c"] == 0:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                # 熊猫游
                jd_login = json.dumps({
                    "method": "POST", "url": "/api/auth/login_user_name", "content_type": "json",
                    "username_field": "userName", "password_field": "password",
                    "has_captcha": True,
                    "captcha": {"key_url": "/verify/key", "img_url": "/verify/img?time={time}&verKey={verkey}",
                                "verkey_field": "verKey", "code_field": "verifyCode"},
                    "token_path": "data.accessToken", "auth_header": "access-token", "auth_prefix": ""
                }, ensure_ascii=False)
                jd_apis = json.dumps({
                    "玩家账号": {"path": "/api/cps_info/game_user_list", "method": "POST", "content_type": "form",
                                "page_param": "pageNo", "page_size_param": "pageSize", "page_size": 50,
                                "data_path": "data.dataList.list", "total_path": "data.dataList.totalCount"},
                    "充值明细": {"path": "/api/cps_info/game_recharge_list", "method": "POST", "content_type": "form",
                                "page_param": "pageNo", "page_size_param": "pageSize", "page_size": 50,
                                "data_path": "data.dataList.list", "total_path": "data.dataList.totalCount"},
                    "角色信息": {"path": "/api/cps_info/game_role_list", "method": "POST", "content_type": "form",
                                "page_param": "pageNo", "page_size_param": "pageSize", "page_size": 50,
                                "data_path": "data.dataList.list", "total_path": "data.dataList.totalCount"},
                }, ensure_ascii=False)
                conn.execute("INSERT INTO vendors (name, login_url, api_base_url, login_config, data_apis, created_at) VALUES (?,?,?,?,?,?)",
                            ("熊猫游(jinyou68)", "https://tg3.jinyou68.com/user/login", "https://tg-admin-api.jinyou68.com", jd_login, jd_apis, now))
                # 游梦
                ym_login = json.dumps({
                    "method": "POST", "url": "/auth/login", "content_type": "json",
                    "username_field": "username", "password_field": "password",
                    "has_captcha": False,
                    "token_path": "data.access_token", "auth_header": "Authorization", "auth_prefix": "Bearer "
                }, ensure_ascii=False)
                ym_apis = json.dumps({
                    "玩家账号": {"path": "/gamesdk/userlist/list", "method": "GET", "content_type": "query",
                                "page_param": "pageNum", "page_size_param": "pageSize", "page_size": 50,
                                "data_path": "rows", "total_path": "total"},
                    "充值明细": {"path": "/gamesdk/recharge/list", "method": "GET", "content_type": "query",
                                "page_param": "pageNum", "page_size_param": "pageSize", "page_size": 50,
                                "data_path": "rows", "total_path": "total", "extra_params": {"isSummary": "0"}},
                }, ensure_ascii=False)
                conn.execute("INSERT INTO vendors (name, login_url, api_base_url, login_config, data_apis, created_at) VALUES (?,?,?,?,?,?)",
                            ("游梦(ymhd)", "https://sso.ymhd.top/login", "https://sso.ymhd.top/prod-api", ym_login, ym_apis, now))
                conn.commit()

    # ---------- 来源管理 ----------
    def add_source(self, name: str, username: str, password: str,
                   vendor_id: int = 0, access_token: str = "") -> int:
        """新增一个来源（关联厂商+账号密码），返回来源ID"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sources (vendor_id, name, username, password, access_token, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (vendor_id, name, username, password, access_token, now),
            )
            conn.commit()
            return cur.lastrowid

    def update_source(self, source_id: int, name: str = None,
                      username: str = None, password: str = None,
                      vendor_id: int = None) -> bool:
        """更新来源信息（只更新传入的非空字段）"""
        fields, vals = [], []
        for col, val in [("name", name), ("username", username),
                         ("password", password), ("vendor_id", vendor_id)]:
            if val is not None:
                fields.append(f"{col}=?")
                vals.append(val)
        if not fields:
            return False
        vals.append(source_id)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE sources SET {', '.join(fields)} WHERE id=?", vals)
            conn.commit()
            return cur.rowcount > 0

    def save_auto_config(self, source_id: int, auto_config: dict) -> bool:
        """保存某来源的自动爬取配置"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sources SET auto_config=? WHERE id=?",
                (json.dumps(auto_config, ensure_ascii=False), source_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def save_access_token(self, source_id: int, access_token: str, api_base_url: str = "") -> bool:
        """保存某来源的 API 访问令牌和 API 基地址"""
        with self._connect() as conn:
            if api_base_url:
                cur = conn.execute(
                    "UPDATE sources SET access_token=?, api_base_url=? WHERE id=?",
                    (access_token, api_base_url, source_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE sources SET access_token=? WHERE id=?",
                    (access_token, source_id),
                )
            conn.commit()
            return cur.rowcount > 0

    def get_sources(self):
        """查询所有来源"""
        with self._connect() as conn:
            # 先查所有厂商，用于关联名称
            vendors = {v["id"]: v["name"] for v in self.get_vendors()}
            cur = conn.execute(
                "SELECT id, vendor_id, name, url, username, password, access_token, api_base_url, api_list, auto_config, created_at "
                "FROM sources ORDER BY id")
            out = []
            for r in cur.fetchall():
                d = dict(r)
                d["vendor_name"] = vendors.get(d.get("vendor_id", 0), "")
                try:
                    d["auto_config"] = json.loads(d.get("auto_config") or "{}")
                except Exception:
                    d["auto_config"] = {}
                try:
                    d["api_list"] = json.loads(d.get("api_list") or "{}")
                except Exception:
                    d["api_list"] = {}
                out.append(d)
            return out

    def get_source(self, source_id: int):
        """查询单个来源"""
        for s in self.get_sources():
            if s["id"] == source_id:
                return s
        return None

    def delete_source(self, source_id: int) -> bool:
        """删除一个来源及其全部数据"""
        with self._connect() as conn:
            # 先删该来源所有任务及其数据
            task_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM crawl_tasks WHERE source_id=?", (source_id,)).fetchall()]
            for tid in task_ids:
                conn.execute("DELETE FROM crawl_data WHERE task_id=?", (tid,))
            conn.execute("DELETE FROM crawl_tasks WHERE source_id=?", (source_id,))
            cur = conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
            conn.commit()
            return cur.rowcount > 0

    # ---------- 厂商管理 ----------
    def add_vendor(self, name: str, login_url: str = "", api_base_url: str = "",
                   login_config: str = "", data_apis: str = "", remark: str = "") -> int:
        """新增厂商，返回厂商ID"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO vendors (name, login_url, api_base_url, login_config, data_apis, remark, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (name, login_url, api_base_url, login_config, data_apis, remark, now))
            conn.commit()
            return cur.lastrowid

    def update_vendor(self, vendor_id: int, name: str = None, login_url: str = None,
                      api_base_url: str = None, login_config: str = None,
                      data_apis: str = None, remark: str = None) -> bool:
        """更新厂商信息"""
        fields, vals = [], []
        for col, val in [("name", name), ("login_url", login_url), ("api_base_url", api_base_url),
                         ("login_config", login_config), ("data_apis", data_apis), ("remark", remark)]:
            if val is not None:
                fields.append(f"{col}=?")
                vals.append(val)
        if not fields:
            return False
        vals.append(vendor_id)
        with self._connect() as conn:
            cur = conn.execute(f"UPDATE vendors SET {', '.join(fields)} WHERE id=?", vals)
            conn.commit()
            return cur.rowcount > 0

    def get_vendors(self):
        """查询所有厂商"""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT id, name, login_url, api_base_url, login_config, data_apis, remark, created_at "
                "FROM vendors ORDER BY id")
            out = []
            for r in cur.fetchall():
                d = dict(r)
                for key in ["login_config", "data_apis"]:
                    try:
                        d[key] = json.loads(d.get(key) or "{}")
                    except Exception:
                        d[key] = {}
                out.append(d)
            return out

    def get_vendor(self, vendor_id: int):
        """查询单个厂商"""
        for v in self.get_vendors():
            if v["id"] == vendor_id:
                return v
        return None

    def delete_vendor(self, vendor_id: int) -> bool:
        """删除厂商（同时把关联来源的vendor_id置0）"""
        with self._connect() as conn:
            conn.execute("UPDATE sources SET vendor_id=0 WHERE vendor_id=?", (vendor_id,))
            cur = conn.execute("DELETE FROM vendors WHERE id=?", (vendor_id,))
            conn.commit()
            return cur.rowcount > 0

    # ---------- 写入 ----------
    def save_task(self, rows: list, source_url: str = "",
                  task_name: str = "默认任务", table_selector: str = "table",
                  source_id: int = 0, columns: list = None) -> int:
        """
        保存一次爬取结果：
        - 创建一条任务记录
        - 把每一行数据以 JSON 形式写入明细表
        - 保存列名（columns）
        返回 task_id（任务ID）
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        columns_json = json.dumps(columns or [], ensure_ascii=False)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO crawl_tasks (source_id, source_url, task_name, table_selector, row_count, columns, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (source_id, source_url, task_name, table_selector, len(rows), columns_json, now),
            )
            task_id = cur.lastrowid
            for i, row in enumerate(rows):
                conn.execute(
                    "INSERT INTO crawl_data (task_id, row_index, data_json) VALUES (?,?,?)",
                    (task_id, i, json.dumps(row, ensure_ascii=False)),
                )
            conn.commit()
        return task_id

    # ---------- 查询 ----------
    def get_tasks(self, limit: int = 50, source_id: int = None):
        """查询最近的任务列表（可按来源过滤）"""
        sql = ("SELECT id, source_id, source_url, task_name, table_selector, row_count, columns, created_at "
               "FROM crawl_tasks ")
        params = []
        if source_id is not None:
            sql += "WHERE source_id=? "
            params.append(source_id)
        sql += "ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            result = []
            for r in cur.fetchall():
                d = dict(r)
                try:
                    d["columns"] = json.loads(d.get("columns") or "[]")
                except Exception:
                    d["columns"] = []
                result.append(d)
            return result

    def get_task_rows(self, task_id: int, limit: int = 10000):
        """查询某个任务的所有数据行，返回 {columns, rows}"""
        with self._connect() as conn:
            # 先查任务的列名
            cur = conn.execute("SELECT columns FROM crawl_tasks WHERE id = ?", (task_id,))
            r = cur.fetchone()
            columns = []
            if r and r["columns"]:
                try:
                    columns = json.loads(r["columns"])
                except Exception:
                    columns = []
            # 再查数据行
            cur = conn.execute(
                "SELECT row_index, data_json FROM crawl_data WHERE task_id = ? ORDER BY row_index LIMIT ?",
                (task_id, limit),
            )
            rows = []
            for r in cur.fetchall():
                try:
                    rows.append(json.loads(r["data_json"]))
                except Exception:
                    rows.append({"原始数据": r["data_json"]})
            return {"columns": columns, "rows": rows}

    def get_task(self, task_id: int):
        """查询单个任务信息"""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT id, source_id, source_url, task_name, table_selector, row_count, created_at "
                "FROM crawl_tasks WHERE id = ?",
                (task_id,),
            )
            r = cur.fetchone()
            return dict(r) if r else None

    def get_total_counts(self):
        """总览统计：任务数 / 数据总行数（可按来源过滤）"""
        with self._connect() as conn:
            tasks = conn.execute("SELECT COUNT(*) c FROM crawl_tasks").fetchone()["c"]
            rows = conn.execute("SELECT COUNT(*) c FROM crawl_data").fetchone()["c"]
            sources = conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
            return {"tasks": tasks, "rows": rows, "sources": sources}

    # ---------- 删除 ----------
    def delete_task(self, task_id: int) -> bool:
        """删除一个任务及其数据（按任务ID）"""
        with self._connect() as conn:
            conn.execute("DELETE FROM crawl_data WHERE task_id = ?", (task_id,))
            cur = conn.execute("DELETE FROM crawl_tasks WHERE id = ?", (task_id,))
            conn.commit()
            return cur.rowcount > 0


# 全局单例（供 crawler / app 共用）
# 存储后端由环境变量 CRAWLER_STORAGE 决定：mysql -> MySQL（game_crawler 库），否则 -> SQLite
if os.getenv('CRAWLER_STORAGE', '').strip().lower() == 'mysql':
    from storage_mysql import Storage as _MySQLStorage
    storage = _MySQLStorage()
    print("🗄️ 爬虫主存储：MySQL")
else:
    storage = Storage()
    print("🗄️ 爬虫主存储：SQLite")
