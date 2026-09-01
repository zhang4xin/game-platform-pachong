# -*- coding: utf-8 -*-
"""
MySQL 存储模块（爬虫主存储）

与 storage.py（SQLite）的 Storage 接口完全一致，底层使用 MySQL。
通过环境变量 CRAWLER_STORAGE=mysql 启用（见 storage.py 底部适配器）。

表结构（建在独立库 game_crawler，避免与平台 game_platform 库混用）：
- vendors        厂商配置
- sources        来源配置
- crawl_tasks    爬取任务
- crawl_data     任务明细

MySQL 连接参数（环境变量）：
- CRAWLER_MYSQL_HOST      默认 localhost
- CRAWLER_MYSQL_PORT      默认 3306
- CRAWLER_MYSQL_USER      默认 root
- CRAWLER_MYSQL_PASSWORD  默认 root
- CRAWLER_MYSQL_DB        默认 game_crawler
"""
import json
import os
import pymysql
from datetime import datetime
from contextlib import contextmanager


class Storage:
    """爬虫 MySQL 存储操作类（接口与 storage.py 的 Storage 完全一致）"""

    def __init__(self, host=None, port=None, user=None, password=None, database=None):
        self.host = host or os.getenv('CRAWLER_MYSQL_HOST', 'localhost')
        self.port = int(os.getenv('CRAWLER_MYSQL_PORT', '3306'))
        self.user = user or os.getenv('CRAWLER_MYSQL_USER', 'root')
        self.password = password or os.getenv('CRAWLER_MYSQL_PASSWORD', 'root')
        self.database = database or os.getenv('CRAWLER_MYSQL_DB', 'game_crawler')
        self._init_db()

    # ---------- 内部：连接与建库建表 ----------
    @contextmanager
    def _connect(self):
        """建立数据库连接（上下文管理器，自动提交/回滚/关闭）"""
        conn = pymysql.connect(
            host=self.host, port=self.port, user=self.user, password=self.password,
            database=self.database, charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_database(self):
        """确保数据库存在（不存在则创建）"""
        conn = pymysql.connect(
            host=self.host, port=self.port, user=self.user, password=self.password,
            charset='utf8mb4',
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE DATABASE IF NOT EXISTS `%s` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    % self.database
                )
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        """初始化建表（不存在则创建），并预置默认厂商"""
        self._ensure_database()
        with self._connect() as conn:
            with conn.cursor() as cur:
                # 厂商配置表
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS vendors (
                        id            BIGINT PRIMARY KEY AUTO_INCREMENT,
                        name          VARCHAR(128),
                        login_url     VARCHAR(512),
                        api_base_url  VARCHAR(512),
                        login_config  TEXT,
                        data_apis     TEXT,
                        remark        VARCHAR(512),
                        created_at    DATETIME DEFAULT NULL,
                        INDEX idx_vendor_name (name)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='爬虫厂商配置表'
                """)
                # 来源表
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sources (
                        id           BIGINT PRIMARY KEY AUTO_INCREMENT,
                        vendor_id    BIGINT DEFAULT 0,
                        name         VARCHAR(128),
                        url          VARCHAR(512),
                        username     VARCHAR(128),
                        password     VARCHAR(256),
                        access_token TEXT,
                        api_base_url VARCHAR(512),
                        api_list     TEXT,
                        auto_config  TEXT,
                        created_at   DATETIME DEFAULT NULL,
                        INDEX idx_source_vendor (vendor_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='爬虫来源表'
                """)
                # 任务表
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS crawl_tasks (
                        id             BIGINT PRIMARY KEY AUTO_INCREMENT,
                        source_id      BIGINT DEFAULT 0,
                        source_url     VARCHAR(512),
                        task_name      VARCHAR(128) DEFAULT '默认任务',
                        table_selector VARCHAR(64) DEFAULT 'table',
                        row_count      INT DEFAULT 0,
                        columns        TEXT,
                        created_at     DATETIME DEFAULT NULL,
                        INDEX idx_tasks_source (source_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='爬虫任务表'
                """)
                # 任务明细表
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS crawl_data (
                        id        BIGINT PRIMARY KEY AUTO_INCREMENT,
                        task_id   BIGINT,
                        row_index INT,
                        data_json TEXT,
                        INDEX idx_data_task (task_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='爬虫任务明细表'
                """)
                # 预置默认厂商：熊猫游、游梦（如果为空）
                cur.execute("SELECT COUNT(*) c FROM vendors")
                if cur.fetchone()['c'] == 0:
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
                    cur.execute(
                        "INSERT INTO vendors (name, login_url, api_base_url, login_config, data_apis, created_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        ("熊猫游(jinyou68)", "https://tg3.jinyou68.com/user/login",
                         "https://tg-admin-api.jinyou68.com", jd_login, jd_apis, now))
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
                                    "data_path": "rows", "total_path": "total",
                                    "extra_params": {"isSummary": "0"}},
                    }, ensure_ascii=False)
                    cur.execute(
                        "INSERT INTO vendors (name, login_url, api_base_url, login_config, data_apis, created_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        ("游梦(ymhd)", "https://sso.ymhd.top/login",
                         "https://sso.ymhd.top/prod-api", ym_login, ym_apis, now))

    # ---------- 来源管理 ----------
    def add_source(self, name: str, username: str, password: str,
                   vendor_id: int = 0, access_token: str = "") -> int:
        """新增一个来源，返回来源ID"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO sources (vendor_id, name, username, password, access_token, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (vendor_id, name, username, password, access_token, now))
            return cur.lastrowid

    def update_source(self, source_id: int, name: str = None,
                      username: str = None, password: str = None,
                      vendor_id: int = None) -> bool:
        """更新来源信息（只更新传入的非空字段）"""
        fields, vals = [], []
        for col, val in [("name", name), ("username", username),
                         ("password", password), ("vendor_id", vendor_id)]:
            if val is not None:
                fields.append(f"{col}=%s")
                vals.append(val)
        if not fields:
            return False
        vals.append(source_id)
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(f"UPDATE sources SET {', '.join(fields)} WHERE id=%s", vals)
            return cur.rowcount > 0

    def save_auto_config(self, source_id: int, auto_config: dict) -> bool:
        """保存某来源的自动爬取配置"""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE sources SET auto_config=%s WHERE id=%s",
                        (json.dumps(auto_config, ensure_ascii=False), source_id))
            return cur.rowcount > 0

    def save_access_token(self, source_id: int, access_token: str, api_base_url: str = "") -> bool:
        """保存某来源的 API 访问令牌和 API 基地址"""
        with self._connect() as conn:
            cur = conn.cursor()
            if api_base_url:
                cur.execute("UPDATE sources SET access_token=%s, api_base_url=%s WHERE id=%s",
                            (access_token, api_base_url, source_id))
            else:
                cur.execute("UPDATE sources SET access_token=%s WHERE id=%s",
                            (access_token, source_id))
            return cur.rowcount > 0

    def get_sources(self):
        """查询所有来源（含厂商名，解析 api_list / auto_config）"""
        with self._connect() as conn:
            cur = conn.cursor()
            vendors = {v["id"]: v["name"] for v in self.get_vendors()}
            cur.execute(
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
                if d.get("created_at") is not None and hasattr(d["created_at"], 'strftime'):
                    d["created_at"] = d["created_at"].strftime("%Y-%m-%d %H:%M:%S")
                out.append(d)
            return out

    def get_source(self, source_id: int):
        """查询单个来源"""
        for s in self.get_sources():
            if s["id"] == source_id:
                return s
        return None

    def delete_source(self, source_id: int) -> bool:
        """删除一个来源及其全部任务与明细"""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM crawl_tasks WHERE source_id=%s", (source_id,))
            task_ids = [r["id"] for r in cur.fetchall()]
            for tid in task_ids:
                cur.execute("DELETE FROM crawl_data WHERE task_id=%s", (tid,))
            cur.execute("DELETE FROM crawl_tasks WHERE source_id=%s", (source_id,))
            cur.execute("DELETE FROM sources WHERE id=%s", (source_id,))
            return cur.rowcount > 0

    # ---------- 厂商管理 ----------
    def add_vendor(self, name: str, login_url: str = "", api_base_url: str = "",
                   login_config: str = "", data_apis: str = "", remark: str = "") -> int:
        """新增厂商，返回厂商ID"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO vendors (name, login_url, api_base_url, login_config, data_apis, remark, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (name, login_url, api_base_url, login_config, data_apis, remark, now))
            return cur.lastrowid

    def update_vendor(self, vendor_id: int, name: str = None, login_url: str = None,
                      api_base_url: str = None, login_config: str = None,
                      data_apis: str = None, remark: str = None) -> bool:
        """更新厂商信息（只更新传入的非空字段）"""
        fields, vals = [], []
        for col, val in [("name", name), ("login_url", login_url), ("api_base_url", api_base_url),
                         ("login_config", login_config), ("data_apis", data_apis), ("remark", remark)]:
            if val is not None:
                fields.append(f"{col}=%s")
                vals.append(val)
        if not fields:
            return False
        vals.append(vendor_id)
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(f"UPDATE vendors SET {', '.join(fields)} WHERE id=%s", vals)
            return cur.rowcount > 0

    def get_vendors(self):
        """查询所有厂商（解析 login_config / data_apis）"""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
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
                if d.get("created_at") is not None and hasattr(d["created_at"], 'strftime'):
                    d["created_at"] = d["created_at"].strftime("%Y-%m-%d %H:%M:%S")
                out.append(d)
            return out

    def get_vendor(self, vendor_id: int):
        """查询单个厂商"""
        for v in self.get_vendors():
            if v["id"] == vendor_id:
                return v
        return None

    def delete_vendor(self, vendor_id: int) -> bool:
        """删除厂商（同时把关联来源的 vendor_id 置 0）"""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE sources SET vendor_id=0 WHERE vendor_id=%s", (vendor_id,))
            cur.execute("DELETE FROM vendors WHERE id=%s", (vendor_id,))
            return cur.rowcount > 0

    # ---------- 写入 ----------
    def save_task(self, rows: list, source_url: str = "",
                  task_name: str = "默认任务", table_selector: str = "table",
                  source_id: int = 0, columns: list = None) -> int:
        """
        保存一次爬取结果（事务）：
        - 创建一条任务记录
        - 把每一行数据以 JSON 写入明细表
        - 保存列名 columns
        返回 task_id
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        columns_json = json.dumps(columns or [], ensure_ascii=False)
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO crawl_tasks (source_id, source_url, task_name, table_selector, row_count, columns, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (source_id, source_url, task_name, table_selector, len(rows), columns_json, now))
            task_id = cur.lastrowid
            for i, row in enumerate(rows):
                cur.execute(
                    "INSERT INTO crawl_data (task_id, row_index, data_json) VALUES (%s,%s,%s)",
                    (task_id, i, json.dumps(row, ensure_ascii=False)))
        return task_id

    # ---------- 查询 ----------
    def get_tasks(self, limit: int = 50, source_id: int = None):
        """查询最近的任务列表（可按来源过滤，解析 columns）"""
        sql = ("SELECT id, source_id, source_url, task_name, table_selector, row_count, columns, created_at "
               "FROM crawl_tasks ")
        params = []
        if source_id is not None:
            sql += "WHERE source_id=%s "
            params.append(source_id)
        sql += "ORDER BY id DESC LIMIT %s"
        params.append(limit)
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            result = []
            for r in cur.fetchall():
                d = dict(r)
                try:
                    d["columns"] = json.loads(d.get("columns") or "[]")
                except Exception:
                    d["columns"] = []
                if d.get("created_at") is not None and hasattr(d["created_at"], 'strftime'):
                    d["created_at"] = d["created_at"].strftime("%Y-%m-%d %H:%M:%S")
                result.append(d)
            return result

    def get_task_rows(self, task_id: int, limit: int = 10000):
        """查询某个任务的所有数据行，返回 {columns, rows}"""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT columns FROM crawl_tasks WHERE id=%s", (task_id,))
            r = cur.fetchone()
            columns = []
            if r and r["columns"]:
                try:
                    columns = json.loads(r["columns"])
                except Exception:
                    columns = []
            cur.execute(
                "SELECT row_index, data_json FROM crawl_data WHERE task_id=%s ORDER BY row_index LIMIT %s",
                (task_id, limit))
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
            cur = conn.cursor()
            cur.execute(
                "SELECT id, source_id, source_url, task_name, table_selector, row_count, created_at "
                "FROM crawl_tasks WHERE id=%s", (task_id,))
            r = cur.fetchone()
            if not r:
                return None
            d = dict(r)
            if d.get("created_at") is not None and hasattr(d["created_at"], 'strftime'):
                d["created_at"] = d["created_at"].strftime("%Y-%m-%d %H:%M:%S")
            return d

    def get_total_counts(self):
        """总览统计：任务数 / 数据总行数 / 来源数"""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) c FROM crawl_tasks")
            tasks = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) c FROM crawl_data")
            rows = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) c FROM sources")
            sources = cur.fetchone()["c"]
            return {"tasks": tasks, "rows": rows, "sources": sources}

    # ---------- 删除 ----------
    def delete_task(self, task_id: int) -> bool:
        """删除一个任务及其明细"""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM crawl_data WHERE task_id=%s", (task_id,))
            cur.execute("DELETE FROM crawl_tasks WHERE id=%s", (task_id,))
            return cur.rowcount > 0
