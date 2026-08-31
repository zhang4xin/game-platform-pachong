# -*- coding: utf-8 -*-
"""
MySQL 存储模块：把爬取的数据存入 MySQL 数据库（与主项目 game-platform 共用）

特点：
- 与主项目共用 game_platform 数据库
- 数据按 来源/厂商/代理 隔离
- 支持增量更新（根据 unique_key 判断是否已存在）
- 支持字段映射配置（表头中文化）
"""
import json
import pymysql
from datetime import datetime
from contextlib import contextmanager


class MySQLStorage:
    """爬虫 MySQL 数据库操作类"""

    def __init__(self, host='localhost', port=3306, user='root', password='root',
                 database='game_platform', charset='utf8mb4'):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.charset = charset
        self._init_db()

    @contextmanager
    def _connect(self):
        """建立数据库连接（上下文管理器，自动关闭）"""
        conn = pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            charset=self.charset,
            cursorclass=pymysql.cursors.DictCursor
        )
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def _init_db(self):
        """初始化建表（不存在则创建）"""
        with self._connect() as conn:
            with conn.cursor() as cursor:
                # 厂商配置表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS crawl_vendor (
                        id            BIGINT PRIMARY KEY AUTO_INCREMENT,
                        name          VARCHAR(128) NOT NULL UNIQUE,
                        login_url     VARCHAR(512),
                        api_base_url  VARCHAR(512),
                        login_config  TEXT,
                        data_apis     TEXT,
                        remark        VARCHAR(512),
                        create_time   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        update_time   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        deleted       TINYINT NOT NULL DEFAULT 0,
                        INDEX idx_vendor_name (name, deleted)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='爬虫厂商配置表'
                """)

                # 来源配置表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS crawl_source (
                        id              BIGINT PRIMARY KEY AUTO_INCREMENT,
                        name            VARCHAR(128) NOT NULL,
                        url             VARCHAR(512),
                        username        VARCHAR(128) NOT NULL,
                        password        VARCHAR(256) NOT NULL,
                        vendor_id       BIGINT,
                        agent_user_id   BIGINT,
                        access_token    TEXT,
                        api_base_url    VARCHAR(512),
                        status          TINYINT NOT NULL DEFAULT 1,
                        remark          VARCHAR(512),
                        create_time     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        update_time     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        deleted         TINYINT NOT NULL DEFAULT 0,
                        INDEX idx_source_vendor (vendor_id, deleted),
                        INDEX idx_source_agent (agent_user_id, deleted),
                        INDEX idx_source_status (status, deleted)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='爬虫来源配置表'
                """)

                # 爬虫数据表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS crawl_data (
                        id              BIGINT PRIMARY KEY AUTO_INCREMENT,
                        source_id       BIGINT NOT NULL,
                        vendor_id       BIGINT,
                        agent_user_id   BIGINT,
                        data_type       VARCHAR(32) NOT NULL,
                        data_json       LONGTEXT NOT NULL,
                        unique_key      VARCHAR(256),
                        crawl_time      DATETIME,
                        create_time     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        update_time     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        deleted         TINYINT NOT NULL DEFAULT 0,
                        INDEX idx_data_source (source_id, data_type, deleted),
                        INDEX idx_data_vendor (vendor_id, data_type, deleted),
                        INDEX idx_data_agent (agent_user_id, data_type, deleted),
                        UNIQUE KEY uk_data_unique (source_id, data_type, unique_key),
                        INDEX idx_data_crawl_time (crawl_time)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='爬虫数据表'
                """)

                # 字段映射表（表头中文化）
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS crawl_field_mapping (
                        id              BIGINT PRIMARY KEY AUTO_INCREMENT,
                        vendor_id       BIGINT,
                        data_type       VARCHAR(32) NOT NULL,
                        field_name      VARCHAR(128) NOT NULL,
                        field_label     VARCHAR(128) NOT NULL,
                        field_type      VARCHAR(32) DEFAULT 'string',
                        sort            INT NOT NULL DEFAULT 0,
                        visible         TINYINT NOT NULL DEFAULT 1,
                        create_time     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        update_time     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_vendor_type_field (vendor_id, data_type, field_name),
                        INDEX idx_mapping_vendor (vendor_id, data_type, visible)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='爬虫字段映射表（表头中文化）'
                """)

                # 任务表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS crawl_task (
                        id              BIGINT PRIMARY KEY AUTO_INCREMENT,
                        source_id       BIGINT NOT NULL,
                        vendor_id       BIGINT,
                        agent_user_id   BIGINT,
                        data_type       VARCHAR(32) NOT NULL,
                        status          VARCHAR(32) NOT NULL DEFAULT 'pending',
                        total_count     INT NOT NULL DEFAULT 0,
                        success_count   INT NOT NULL DEFAULT 0,
                        fail_count      INT NOT NULL DEFAULT 0,
                        start_time      DATETIME,
                        end_time        DATETIME,
                        error_msg       TEXT,
                        trigger_type    VARCHAR(32) DEFAULT 'manual',
                        create_time     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        update_time     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_task_source (source_id, data_type),
                        INDEX idx_task_status (status),
                        INDEX idx_task_agent (agent_user_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='爬虫任务表'
                """)

    # ---------- 厂商配置 ----------
    def get_vendors(self):
        """获取所有厂商配置"""
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM crawl_vendor WHERE deleted=0 ORDER BY id")
                return cursor.fetchall()

    def get_vendor(self, vendor_id):
        """根据ID获取厂商配置"""
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM crawl_vendor WHERE id=%s AND deleted=0", (vendor_id,))
                return cursor.fetchone()

    def save_vendor(self, vendor):
        """保存厂商配置（存在则更新，不存在则插入）"""
        with self._connect() as conn:
            with conn.cursor() as cursor:
                if vendor.get('id'):
                    cursor.execute("""
                        UPDATE crawl_vendor SET name=%s, login_url=%s, api_base_url=%s,
                        login_config=%s, data_apis=%s, remark=%s WHERE id=%s
                    """, (
                        vendor.get('name'), vendor.get('login_url'), vendor.get('api_base_url'),
                        json.dumps(vendor.get('login_config', {}), ensure_ascii=False) if isinstance(vendor.get('login_config'), dict) else vendor.get('login_config'),
                        json.dumps(vendor.get('data_apis', {}), ensure_ascii=False) if isinstance(vendor.get('data_apis'), dict) else vendor.get('data_apis'),
                        vendor.get('remark'), vendor.get('id')
                    ))
                    return vendor.get('id')
                else:
                    cursor.execute("""
                        INSERT INTO crawl_vendor (name, login_url, api_base_url, login_config, data_apis, remark)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        vendor.get('name'), vendor.get('login_url'), vendor.get('api_base_url'),
                        json.dumps(vendor.get('login_config', {}), ensure_ascii=False) if isinstance(vendor.get('login_config'), dict) else vendor.get('login_config'),
                        json.dumps(vendor.get('data_apis', {}), ensure_ascii=False) if isinstance(vendor.get('data_apis'), dict) else vendor.get('data_apis'),
                        vendor.get('remark')
                    ))
                    return cursor.lastrowid

    # ---------- 来源配置 ----------
    def get_sources(self, agent_user_id=None):
        """获取所有来源配置"""
        with self._connect() as conn:
            with conn.cursor() as cursor:
                if agent_user_id:
                    cursor.execute("SELECT * FROM crawl_source WHERE deleted=0 AND agent_user_id=%s ORDER BY id", (agent_user_id,))
                else:
                    cursor.execute("SELECT * FROM crawl_source WHERE deleted=0 ORDER BY id")
                return cursor.fetchall()

    def get_source(self, source_id):
        """根据ID获取来源配置"""
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM crawl_source WHERE id=%s AND deleted=0", (source_id,))
                return cursor.fetchone()

    def save_source(self, source):
        """保存来源配置"""
        with self._connect() as conn:
            with conn.cursor() as cursor:
                if source.get('id'):
                    cursor.execute("""
                        UPDATE crawl_source SET name=%s, url=%s, username=%s, password=%s,
                        vendor_id=%s, agent_user_id=%s, access_token=%s, api_base_url=%s,
                        status=%s, remark=%s WHERE id=%s
                    """, (
                        source.get('name'), source.get('url'), source.get('username'), source.get('password'),
                        source.get('vendor_id'), source.get('agent_user_id'), source.get('access_token'),
                        source.get('api_base_url'), source.get('status', 1), source.get('remark'), source.get('id')
                    ))
                    return source.get('id')
                else:
                    cursor.execute("""
                        INSERT INTO crawl_source (name, url, username, password, vendor_id, agent_user_id,
                        access_token, api_base_url, status, remark)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        source.get('name'), source.get('url'), source.get('username'), source.get('password'),
                        source.get('vendor_id'), source.get('agent_user_id'), source.get('access_token'),
                        source.get('api_base_url'), source.get('status', 1), source.get('remark')
                    ))
                    return cursor.lastrowid

    def update_access_token(self, source_id, access_token):
        """更新来源的访问令牌"""
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE crawl_source SET access_token=%s WHERE id=%s", (access_token, source_id))

    # ---------- 爬虫数据 ----------
    def save_crawl_data(self, source_id, vendor_id, agent_user_id, data_type, data_list, unique_key_field=None):
        """
        保存爬取的数据（增量更新）
        
        Args:
            source_id: 来源ID
            vendor_id: 厂商ID
            agent_user_id: 代理用户ID
            data_type: 数据类型（player/recharge/role）
            data_list: 数据列表（字典列表）
            unique_key_field: 唯一键字段名（用于去重，如 username/order_no）
        
        Returns:
            dict: {total, success, updated, inserted}
        """
        total = len(data_list)
        success = 0
        updated = 0
        inserted = 0
        crawl_time = datetime.now()

        with self._connect() as conn:
            with conn.cursor() as cursor:
                for data in data_list:
                    try:
                        # 提取唯一键
                        unique_key = None
                        if unique_key_field and unique_key_field in data:
                            unique_key = str(data[unique_key_field])
                        elif 'username' in data:
                            unique_key = str(data['username'])
                        elif 'orderNo' in data:
                            unique_key = str(data['orderNo'])
                        elif 'order_no' in data:
                            unique_key = str(data['order_no'])
                        elif 'roleId' in data:
                            unique_key = str(data['roleId'])
                        elif 'id' in data:
                            unique_key = str(data['id'])

                        data_json = json.dumps(data, ensure_ascii=False)

                        if unique_key:
                            # 检查是否已存在
                            cursor.execute("""
                                SELECT id FROM crawl_data 
                                WHERE source_id=%s AND data_type=%s AND unique_key=%s AND deleted=0
                            """, (source_id, data_type, unique_key))
                            existing = cursor.fetchone()

                            if existing:
                                # 更新
                                cursor.execute("""
                                    UPDATE crawl_data SET data_json=%s, crawl_time=%s, vendor_id=%s, agent_user_id=%s
                                    WHERE id=%s
                                """, (data_json, crawl_time, vendor_id, agent_user_id, existing['id']))
                                updated += 1
                            else:
                                # 插入
                                cursor.execute("""
                                    INSERT INTO crawl_data (source_id, vendor_id, agent_user_id, data_type, data_json, unique_key, crawl_time)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                                """, (source_id, vendor_id, agent_user_id, data_type, data_json, unique_key, crawl_time))
                                inserted += 1
                        else:
                            # 没有唯一键，直接插入
                            cursor.execute("""
                                INSERT INTO crawl_data (source_id, vendor_id, agent_user_id, data_type, data_json, crawl_time)
                                VALUES (%s, %s, %s, %s, %s, %s)
                            """, (source_id, vendor_id, agent_user_id, data_type, data_json, crawl_time))
                            inserted += 1

                        success += 1
                    except Exception as e:
                        print(f"保存数据失败: {str(e)[:100]}")
                        continue

        return {
            'total': total,
            'success': success,
            'updated': updated,
            'inserted': inserted
        }

    def get_crawl_data(self, source_id=None, vendor_id=None, agent_user_id=None,
                       data_type=None, page=1, page_size=20, keyword=None,
                       start_time=None, end_time=None):
        """
        查询爬取的数据（分页）
        
        Returns:
            dict: {total, records, columns}
        """
        offset = (page - 1) * page_size
        conditions = ["deleted=0"]
        params = []

        if source_id:
            conditions.append("source_id=%s")
            params.append(source_id)
        if vendor_id:
            conditions.append("vendor_id=%s")
            params.append(vendor_id)
        if agent_user_id:
            conditions.append("agent_user_id=%s")
            params.append(agent_user_id)
        if data_type:
            conditions.append("data_type=%s")
            params.append(data_type)
        if start_time:
            conditions.append("crawl_time >= %s")
            params.append(start_time)
        if end_time:
            conditions.append("crawl_time <= %s")
            params.append(end_time)

        where_clause = " AND ".join(conditions)

        with self._connect() as conn:
            with conn.cursor() as cursor:
                # 查询总数
                cursor.execute(f"SELECT COUNT(*) as total FROM crawl_data WHERE {where_clause}", params)
                total = cursor.fetchone()['total']

                # 查询数据
                cursor.execute(f"""
                    SELECT * FROM crawl_data WHERE {where_clause}
                    ORDER BY crawl_time DESC, id DESC
                    LIMIT %s OFFSET %s
                """, params + [page_size, offset])
                records = cursor.fetchall()

                # 解析 data_json
                parsed_records = []
                for record in records:
                    try:
                        data = json.loads(record['data_json']) if record['data_json'] else {}
                        parsed_record = {
                            'id': record['id'],
                            'source_id': record['source_id'],
                            'vendor_id': record['vendor_id'],
                            'agent_user_id': record['agent_user_id'],
                            'data_type': record['data_type'],
                            'unique_key': record['unique_key'],
                            'crawl_time': record['crawl_time'].strftime('%Y-%m-%d %H:%M:%S') if record['crawl_time'] else None,
                        }
                        parsed_record.update(data)
                        parsed_records.append(parsed_record)
                    except Exception as e:
                        print(f"解析数据失败: {str(e)[:50]}")
                        continue

                # 获取字段映射（表头中文化）
                field_mappings = self.get_field_mapping(vendor_id=vendor_id, data_type=data_type)

                return {
                    'total': total,
                    'records': parsed_records,
                    'columns': field_mappings
                }

    # ---------- 字段映射（表头中文化） ----------
    def get_field_mapping(self, vendor_id=None, data_type=None):
        """获取字段映射配置（表头中文化）"""
        with self._connect() as conn:
            with conn.cursor() as cursor:
                conditions = ["visible=1"]
                params = []

                if data_type:
                    conditions.append("data_type=%s")
                    params.append(data_type)

                # 优先获取厂商特定配置，没有则用全局配置
                if vendor_id:
                    conditions.append("(vendor_id=%s OR vendor_id IS NULL)")
                    params.append(vendor_id)
                else:
                    conditions.append("vendor_id IS NULL")

                where_clause = " AND ".join(conditions)
                cursor.execute(f"""
                    SELECT * FROM crawl_field_mapping WHERE {where_clause}
                    ORDER BY vendor_id IS NULL, sort, id
                """, params)
                mappings = cursor.fetchall()

                # 去重（厂商配置优先）
                seen_fields = set()
                result = []
                for m in mappings:
                    if m['field_name'] not in seen_fields:
                        seen_fields.add(m['field_name'])
                        result.append({
                            'field_name': m['field_name'],
                            'field_label': m['field_label'],
                            'field_type': m['field_type'],
                            'sort': m['sort'],
                            'visible': m['visible']
                        })
                return result

    def save_field_mapping(self, vendor_id, data_type, mappings):
        """保存字段映射配置"""
        with self._connect() as conn:
            with conn.cursor() as cursor:
                # 删除旧配置
                cursor.execute("""
                    DELETE FROM crawl_field_mapping WHERE vendor_id=%s AND data_type=%s
                """, (vendor_id, data_type))

                # 插入新配置
                for i, m in enumerate(mappings):
                    cursor.execute("""
                        INSERT INTO crawl_field_mapping (vendor_id, data_type, field_name, field_label, field_type, sort, visible)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (
                        vendor_id, data_type,
                        m.get('field_name'), m.get('field_label'),
                        m.get('field_type', 'string'), i,
                        m.get('visible', 1)
                    ))

    # ---------- 任务管理 ----------
    def create_task(self, source_id, vendor_id, agent_user_id, data_type, trigger_type='manual'):
        """创建爬取任务"""
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO crawl_task (source_id, vendor_id, agent_user_id, data_type, status, trigger_type, start_time)
                    VALUES (%s, %s, %s, %s, 'running', %s, NOW())
                """, (source_id, vendor_id, agent_user_id, data_type, trigger_type))
                return cursor.lastrowid

    def update_task(self, task_id, status=None, total_count=None, success_count=None,
                    fail_count=None, error_msg=None):
        """更新任务状态"""
        with self._connect() as conn:
            with conn.cursor() as cursor:
                updates = []
                params = []
                if status:
                    updates.append("status=%s")
                    params.append(status)
                if total_count is not None:
                    updates.append("total_count=%s")
                    params.append(total_count)
                if success_count is not None:
                    updates.append("success_count=%s")
                    params.append(success_count)
                if fail_count is not None:
                    updates.append("fail_count=%s")
                    params.append(fail_count)
                if error_msg:
                    updates.append("error_msg=%s")
                    params.append(error_msg)
                if status in ('success', 'failed'):
                    updates.append("end_time=NOW()")

                if updates:
                    params.append(task_id)
                    cursor.execute(f"UPDATE crawl_task SET {', '.join(updates)} WHERE id=%s", params)

    def get_tasks(self, source_id=None, agent_user_id=None, limit=20):
        """获取任务列表"""
        with self._connect() as conn:
            with conn.cursor() as cursor:
                conditions = ["1=1"]
                params = []
                if source_id:
                    conditions.append("source_id=%s")
                    params.append(source_id)
                if agent_user_id:
                    conditions.append("agent_user_id=%s")
                    params.append(agent_user_id)

                where_clause = " AND ".join(conditions)
                cursor.execute(f"""
                    SELECT * FROM crawl_task WHERE {where_clause}
                    ORDER BY create_time DESC LIMIT %s
                """, params + [limit])
                return cursor.fetchall()
