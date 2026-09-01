# -*- coding: utf-8 -*-
"""
SQLite -> MySQL 迁移脚本

把爬虫主存储从 SQLite（data/crawler.db）迁移到 MySQL（game_crawler 库）。
迁移的表：vendors / sources / crawl_tasks / crawl_data

用法：
    python migrate_to_mysql.py            # 全量迁移（目标表先清空再插入）
    python migrate_to_mysql.py --dry-run  # 只统计不写入

MySQL 连接参数（环境变量）：
    CRAWLER_MYSQL_HOST / PORT / USER / PASSWORD / DB（默认 game_crawler）
"""
import os
import sys
import sqlite3
import pymysql
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SQLITE_PATH = BASE_DIR / "data" / "crawler.db"

MYSQL_HOST = os.getenv('CRAWLER_MYSQL_HOST', 'localhost')
MYSQL_PORT = int(os.getenv('CRAWLER_MYSQL_PORT', '3306'))
MYSQL_USER = os.getenv('CRAWLER_MYSQL_USER', 'root')
MYSQL_PASSWORD = os.getenv('CRAWLER_MYSQL_PASSWORD', 'root')
MYSQL_DB = os.getenv('CRAWLER_MYSQL_DB', 'game_crawler')

DRY_RUN = '--dry-run' in sys.argv


def get_sqlite():
    conn = sqlite3.connect(str(SQLITE_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def get_mysql():
    conn = pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER, password=MYSQL_PASSWORD,
        database=MYSQL_DB, charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
    return conn


def ensure_db():
    """确保数据库存在"""
    conn = pymysql.connect(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
                           password=MYSQL_PASSWORD, charset='utf8mb4')
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS `%s` DEFAULT CHARACTER SET utf8mb4 "
                        "COLLATE utf8mb4_unicode_ci" % MYSQL_DB)
        conn.commit()
    finally:
        conn.close()


def migrate_table(mysql, name, columns, rows):
    """迁移一张表：先清空目标表，再全量插入"""
    if DRY_RUN:
        return len(rows)
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with mysql.cursor() as cur:
        cur.execute(f"DELETE FROM `{name}`")
        for r in rows:
            cur.execute(f"INSERT INTO `{name}` ({cols}) VALUES ({placeholders})",
                        [r[c] for c in columns])
    return len(rows)


def main():
    if not SQLITE_PATH.exists():
        print(f"❌ SQLite 数据库不存在: {SQLITE_PATH}")
        return
    if DRY_RUN:
        print("🔍 演练模式：只统计不写入")

    sl = get_sqlite()
    ensure_db()
    # 先建表（复用 storage_mysql 的建表逻辑），再迁移
    from storage_mysql import Storage
    Storage()
    my = get_mysql()

    # vendors
    vendors = [dict(r) for r in sl.execute(
        "SELECT id, name, login_url, api_base_url, login_config, data_apis, remark, created_at FROM vendors ORDER BY id")]
    print(f"vendors: {len(vendors)} 条")
    migrate_table(my, 'vendors', ['id', 'name', 'login_url', 'api_base_url',
                                  'login_config', 'data_apis', 'remark', 'created_at'], vendors)

    # sources
    sources = [dict(r) for r in sl.execute(
        "SELECT id, vendor_id, name, url, username, password, access_token, api_base_url, api_list, auto_config, created_at FROM sources ORDER BY id")]
    print(f"sources: {len(sources)} 条")
    migrate_table(my, 'sources', ['id', 'vendor_id', 'name', 'url', 'username', 'password',
                                  'access_token', 'api_base_url', 'api_list', 'auto_config', 'created_at'], sources)

    # crawl_tasks
    tasks = [dict(r) for r in sl.execute(
        "SELECT id, source_id, source_url, task_name, table_selector, row_count, columns, created_at FROM crawl_tasks ORDER BY id")]
    print(f"crawl_tasks: {len(tasks)} 条")
    migrate_table(my, 'crawl_tasks', ['id', 'source_id', 'source_url', 'task_name',
                                      'table_selector', 'row_count', 'columns', 'created_at'], tasks)

    # crawl_data（分批，避免一次性占内存）
    sl.row_factory = sqlite3.Row
    total = sl.execute("SELECT COUNT(*) c FROM crawl_data").fetchone()["c"]
    print(f"crawl_data: {total} 条（分批迁移）")
    batch = 500
    migrated = 0
    offset = 0
    while True:
        rows = [dict(r) for r in sl.execute(
            "SELECT id, task_id, row_index, data_json FROM crawl_data ORDER BY id LIMIT ? OFFSET ?",
            (batch, offset))]
        if not rows:
            break
        if not DRY_RUN:
            with my.cursor() as cur:
                for r in rows:
                    cur.execute(
                        "INSERT INTO crawl_data (id, task_id, row_index, data_json) VALUES (%s,%s,%s,%s)",
                        (r['id'], r['task_id'], r['row_index'], r['data_json']))
        migrated += len(rows)
        offset += batch
        if DRY_RUN:
            break  # 演练模式只统计第一页
    print(f"crawl_data 迁移: {migrated} 条")

    my.commit()
    sl.close()
    my.close()

    # 汇总
    print("✅ 迁移完成" if not DRY_RUN else "🔍 演练完成（未写入）")


if __name__ == '__main__':
    main()
