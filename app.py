# -*- coding: utf-8 -*-
"""
FastAPI 后端服务（通用多厂商爬虫平台）

提供：
- 厂商管理：增删改查厂商API配置
- 来源管理：增删改查来源（关联厂商）
- 登录：根据厂商配置自动登录，获取token
- 抓取：根据厂商配置自动翻页抓取数据
- 自动抓包：用浏览器自动捕获登录和数据API，生成厂商配置
- 数据查看：查看爬取的数据
"""
import json
import time
import os
from pathlib import Path
from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from storage import storage
from crawler import crawler, sniffer, get_nested_value, HAS_PLAYWRIGHT

# MySQL 存储支持（与主项目 game-platform 共用数据库）
try:
    from mysql_storage import MySQLStorage
    # 从环境变量读取 MySQL 配置，默认使用本地配置
    mysql_storage = MySQLStorage(
        host=os.getenv('MYSQL_HOST', 'localhost'),
        port=int(os.getenv('MYSQL_PORT', '3306')),
        user=os.getenv('MYSQL_USER', 'root'),
        password=os.getenv('MYSQL_PASSWORD', 'root'),
        database=os.getenv('MYSQL_DATABASE', 'game_platform'),
    )
    MYSQL_ENABLED = True
    print("✅ MySQL 存储已启用，数据将同步写入 game_platform 数据库")
except Exception as e:
    mysql_storage = None
    MYSQL_ENABLED = False
    print(f"⚠️ MySQL 存储未启用: {str(e)[:100]}，将仅使用 SQLite")

# 更新策略和定时任务
try:
    from update_strategy import UpdateStrategy
    from scheduler import CrawlerScheduler
    update_strategy = UpdateStrategy(storage=storage, mysql_storage=mysql_storage)
    crawler_scheduler = CrawlerScheduler(
        update_strategy=update_strategy,
        crawler=crawler,
        storage=storage,
        mysql_storage=mysql_storage,
    )
    SCHEDULER_ENABLED = True
    print("✅ 更新策略和定时任务模块已加载")
except Exception as e:
    update_strategy = None
    crawler_scheduler = None
    SCHEDULER_ENABLED = False
    print(f"⚠️ 更新策略和定时任务模块加载失败: {str(e)[:100]}")

app = FastAPI(title="通用爬虫平台")

# 静态文件目录
STATIC_DIR = Path(__file__).resolve().parent / "static"
CAPTCHA_DIR = Path(__file__).resolve().parent / "captcha"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ==================== 首页 ====================

@app.get("/")
def index():
    from fastapi.responses import Response
    content = (STATIC_DIR / "index.html").read_bytes()
    return Response(content=content, media_type="text/html",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# ==================== 工具函数 ====================

def get_effective_vendor(source):
    """获取来源关联的厂商配置"""
    vendor_id = source.get("vendor_id", 0)
    if not vendor_id:
        return None
    return storage.get_vendor(vendor_id)


# ==================== 厂商管理 ====================

class VendorReq(BaseModel):
    name: str
    login_url: str = ""
    api_base_url: str = ""
    login_config: dict = {}
    data_apis: dict = {}
    remark: str = ""


@app.get("/api/vendors")
def api_vendors():
    """厂商列表"""
    return {"success": True, "vendors": storage.get_vendors()}


@app.get("/api/vendors/{vendor_id}")
def api_get_vendor(vendor_id: int):
    """获取单个厂商详情"""
    v = storage.get_vendor(vendor_id)
    return {"success": True, "vendor": v}


@app.post("/api/vendors")
def api_add_vendor(req: VendorReq):
    """新增厂商"""
    vid = storage.add_vendor(
        name=req.name, login_url=req.login_url, api_base_url=req.api_base_url,
        login_config=json.dumps(req.login_config, ensure_ascii=False),
        data_apis=json.dumps(req.data_apis, ensure_ascii=False),
        remark=req.remark,
    )
    return {"success": True, "id": vid}


@app.put("/api/vendors/{vendor_id}")
def api_update_vendor(vendor_id: int, req: VendorReq):
    """编辑厂商"""
    storage.update_vendor(
        vendor_id, name=req.name, login_url=req.login_url, api_base_url=req.api_base_url,
        login_config=json.dumps(req.login_config, ensure_ascii=False),
        data_apis=json.dumps(req.data_apis, ensure_ascii=False),
        remark=req.remark,
    )
    return {"success": True}


@app.delete("/api/vendors/{vendor_id}")
def api_del_vendor(vendor_id: int):
    """删除厂商"""
    storage.delete_vendor(vendor_id)
    return {"success": True}


# ==================== 来源管理 ====================

class SourceReq(BaseModel):
    name: str
    username: str
    password: str
    vendor_id: int = 0


@app.get("/api/sources")
def api_sources():
    """来源列表"""
    return {"success": True, "sources": storage.get_sources()}


@app.post("/api/sources")
def api_add_source(req: SourceReq):
    """新增来源"""
    sid = storage.add_source(req.name, req.username, req.password, vendor_id=req.vendor_id)
    return {"success": True, "id": sid}


@app.put("/api/sources/{source_id}")
def api_update_source(source_id: int, req: SourceReq):
    """编辑来源"""
    storage.update_source(source_id, name=req.name, username=req.username,
                          password=req.password, vendor_id=req.vendor_id)
    return {"success": True}


@app.delete("/api/sources/{source_id}")
def api_del_source(source_id: int):
    """删除来源"""
    storage.delete_source(source_id)
    return {"success": True}


@app.get("/api/sources/{source_id}")
def api_get_source(source_id: int):
    """获取单个来源详情（含厂商配置）"""
    src = storage.get_source(source_id)
    if not src:
        return {"success": False, "error": "来源不存在"}
    vendor = get_effective_vendor(src)
    src["vendor"] = vendor
    return {"success": True, "source": src}


# ==================== 验证码 ====================

@app.post("/api/sources/{source_id}/captcha")
def api_get_captcha(source_id: int):
    """获取验证码图片"""
    src = storage.get_source(source_id)
    if not src:
        return {"success": False, "error": "来源不存在"}
    vendor = get_effective_vendor(src)
    login_cfg = vendor.get("login_config", {}) if vendor else {}
    cap_cfg = login_cfg.get("captcha") if login_cfg else None
    # login_url 优先用来源的 url，没有就用厂商的 login_url
    effective_login_url = src.get("url") or (vendor.get("login_url") if vendor else None)
    result = crawler.get_captcha(
        api_base_url=vendor.get("api_base_url") if vendor else None,
        captcha_config=cap_cfg,
        login_url=effective_login_url,
    )
    if result.get("success"):
        result["source_id"] = source_id
    return result


@app.get("/api/captcha/image/{image_name}")
def get_captcha_image(image_name: str):
    """获取验证码图片"""
    img_path = CAPTCHA_DIR / image_name
    if img_path.exists():
        return FileResponse(str(img_path))
    return JSONResponse(status_code=404, content={"error": "图片不存在"})


# ==================== 登录 ====================

class LoginReq(BaseModel):
    verifyCode: str
    verkey: str = ""


@app.post("/api/sources/{source_id}/login")
def api_login(source_id: int, req: LoginReq):
    """登录厂商后台（根据厂商配置自动适配）"""
    src = storage.get_source(source_id)
    if not src:
        return {"success": False, "error": "来源不存在"}

    vendor = get_effective_vendor(src)
    login_cfg = vendor.get("login_config", {}) if vendor else None
    api_base = src.get("api_base_url") or (vendor.get("api_base_url") if vendor else None)
    # login_url 优先用来源的 url，没有就用厂商的 login_url
    effective_login_url = src.get("url") or (vendor.get("login_url") if vendor else None)

    verify_code = (req.verifyCode or "").strip()
    has_captcha = login_cfg.get("has_captcha", False) if login_cfg else True

    if has_captcha and verify_code:
        verkey = req.verkey or src.get("access_token", "")
        if not verkey:
            return {"success": False, "error": "请先获取验证码"}
        result = crawler.login(
            src["username"], src["password"], verkey, verify_code,
            api_base_url=api_base, login_url=effective_login_url, login_config=login_cfg,
        )
    else:
        # 无验证码直接登录
        result = crawler.login(
            src["username"], src["password"],
            api_base_url=api_base, login_url=effective_login_url, login_config=login_cfg,
        )

    if result.get("success"):
        storage.save_access_token(source_id, result["access_token"], api_base or "")
        result["source_id"] = source_id

    return result


# ==================== 浏览器登录（备用） ====================

class BrowserCaptchaReq(BaseModel):
    manual_input: bool = False


@app.post("/api/sources/{source_id}/browser-captcha")
def api_browser_captcha(source_id: int, req: BrowserCaptchaReq = None):
    """用浏览器打开登录页，填写账号密码，截图验证码
    manual_input=True 时不自动填写账号密码，等待用户在浏览器里手动输入（60秒）
    """
    src = storage.get_source(source_id)
    if not src:
        return {"success": False, "error": "来源不存在"}
    manual = req.manual_input if req else False
    # 获取登录URL：优先使用来源的url，如果为空则使用厂商配置的login_url
    login_url = src.get("url")
    if not login_url:
        vendor = get_effective_vendor(src)
        if vendor:
            login_url = vendor.get("login_url")
            print(f"[调试] 来源url为空，使用厂商login_url: {login_url}")
    if not login_url:
        return {"success": False, "error": "未找到登录URL，请在来源管理或厂商管理中配置登录页URL"}
    print(f"[调试] 最终使用的登录URL: {login_url}")
    result = crawler.browser_get_captcha(src["username"], src["password"], login_url=login_url, manual_input=manual)
    return result


class BrowserLoginReq(BaseModel):
    code: str


@app.post("/api/sources/{source_id}/browser-login")
def api_browser_login(source_id: int, req: BrowserLoginReq):
    """在浏览器中填写验证码并登录"""
    src = storage.get_source(source_id)
    vendor = get_effective_vendor(src) if src else None
    login_cfg = vendor.get("login_config", {}) if vendor else {}
    auth_header = login_cfg.get("auth_header") if login_cfg else None
    token_path = login_cfg.get("token_path") if login_cfg else None
    result = crawler.browser_submit_captcha(req.code, auth_header=auth_header, token_path=token_path)
    if result.get("success") and src:
        api_base = src.get("api_base_url") or (vendor.get("api_base_url") if vendor else "")
        storage.save_access_token(source_id, result["access_token"], api_base)
        result["source_id"] = source_id
    return result


# ==================== 抓取数据 ====================

class FetchReq(BaseModel):
    api_path: str
    intervalDate: str = ""
    userLogin: str = ""
    max_pages: int = 20
    task_name: str = "API抓取"


@app.post("/api/sources/{source_id}/fetch")
def api_fetch(source_id: int, req: FetchReq):
    """抓取数据（根据厂商配置自动适配）"""
    src = storage.get_source(source_id)
    if not src:
        return {"success": False, "error": "来源不存在"}

    access_token = src.get("access_token", "")
    if not access_token or len(access_token) < 20:
        return {"success": False, "error": "该来源未登录或 access_token 无效，请先登录"}

    vendor = get_effective_vendor(src)
    api_base = src.get("api_base_url") or (vendor.get("api_base_url") if vendor else None)

    # 从厂商配置中找到对应数据接口的配置
    data_apis = vendor.get("data_apis", {}) if vendor else {}
    api_cfg = None
    for name, path in data_apis.items():
        if isinstance(path, dict):
            if path.get("path") == req.api_path:
                api_cfg = path
                break
        elif path == req.api_path:
            api_cfg = {"path": req.api_path}
            break

    # 构造参数
    params = {}
    if req.intervalDate:
        params["intervalDate"] = req.intervalDate
    if req.userLogin:
        params["userLogin"] = req.userLogin

    result = crawler.fetch_data(
        api_path=req.api_path,
        access_token=access_token,
        params=params,
        max_pages=req.max_pages,
        source_id=source_id,
        task_name=req.task_name,
        api_base_url=api_base,
        data_api_config=api_cfg,
    )

    # 同步到 MySQL（如果启用）
    if MYSQL_ENABLED and result.get('success') and result.get('data'):
        try:
            source = storage.get_source(source_id)
            vendor_id = source.get('vendor_id') if source else None
            agent_user_id = source.get('agent_user_id') if source else None
            # 按接口路径判断数据类型，避免任务名含中文导致误判
            path_l = (req.api_path or '').lower()
            if any(kw in path_l for kw in ['user', 'player', 'member', 'account']):
                data_type = 'player'
            elif any(kw in path_l for kw in ['recharge', 'order']):
                data_type = 'recharge'
            else:
                data_type = 'role'
            sync_result = mysql_storage.save_crawl_data(
                source_id=source_id,
                vendor_id=vendor_id,
                agent_user_id=agent_user_id,
                data_type=data_type,
                data_list=result['data'],
            )
            print(f"✅ 数据已同步到 MySQL: 总计{sync_result['total']}条, 新增{sync_result['inserted']}条, 更新{sync_result['updated']}条")
        except Exception as e:
            print(f"⚠️ MySQL同步失败: {str(e)[:100]}")

    return result


# ==================== MySQL 数据查询（表头中文化） ====================

class CrawlDataQueryReq(BaseModel):
    source_id: int = None
    vendor_id: int = None
    agent_user_id: int = None
    data_type: str = "player"
    page: int = 1
    page_size: int = 20
    keyword: str = ""
    start_time: str = ""
    end_time: str = ""


@app.get("/api/crawler/data")
def api_crawler_data(
    source_id: int = Query(None),
    vendor_id: int = Query(None),
    agent_user_id: int = Query(None),
    data_type: str = Query("player"),
    page: int = Query(1),
    page_size: int = Query(20),
    keyword: str = Query(""),
    start_time: str = Query(""),
    end_time: str = Query(""),
):
    """
    查询爬虫数据（表头中文化）
    
    数据类型：player=玩家账号, recharge=充值明细, role=角色信息
    返回：总数、数据列表、中文表头配置
    """
    if not MYSQL_ENABLED:
        return {"success": False, "error": "MySQL未启用，请先配置MySQL连接"}

    try:
        result = mysql_storage.get_crawl_data(
            source_id=source_id,
            vendor_id=vendor_id,
            agent_user_id=agent_user_id,
            data_type=data_type,
            page=page,
            page_size=page_size,
            keyword=keyword,
            start_time=start_time if start_time else None,
            end_time=end_time if end_time else None,
        )
        return {
            "success": True,
            "total": result['total'],
            "records": result['records'],
            "columns": result['columns'],  # 中文表头配置
            "page": page,
            "page_size": page_size,
        }
    except Exception as e:
        return {"success": False, "error": f"查询失败: {str(e)[:200]}"}


@app.get("/api/crawler/columns")
def api_crawler_columns(
    vendor_id: int = Query(None),
    data_type: str = Query("player"),
):
    """获取字段映射配置（中文表头）"""
    if not MYSQL_ENABLED:
        return {"success": False, "error": "MySQL未启用"}

    try:
        columns = mysql_storage.get_field_mapping(vendor_id=vendor_id, data_type=data_type)
        return {"success": True, "columns": columns}
    except Exception as e:
        return {"success": False, "error": f"获取字段映射失败: {str(e)[:200]}"}


class FieldMappingReq(BaseModel):
    vendor_id: int = None
    data_type: str
    mappings: list


@app.post("/api/crawler/columns")
def api_save_crawler_columns(req: FieldMappingReq):
    """保存字段映射配置（中文表头）"""
    if not MYSQL_ENABLED:
        return {"success": False, "error": "MySQL未启用"}

    try:
        mysql_storage.save_field_mapping(
            vendor_id=req.vendor_id,
            data_type=req.data_type,
            mappings=req.mappings,
        )
        return {"success": True, "message": "字段映射保存成功"}
    except Exception as e:
        return {"success": False, "error": f"保存字段映射失败: {str(e)[:200]}"}


# ==================== MySQL 同步管理 ====================

@app.post("/api/crawler/sync/all")
def api_sync_all_to_mysql():
    """把所有 SQLite 数据同步到 MySQL"""
    if not MYSQL_ENABLED:
        return {"success": False, "error": "MySQL未启用"}

    try:
        # 同步厂商配置
        vendors = storage.get_vendors()
        vendor_count = 0
        for v in vendors:
            mysql_storage.save_vendor(v)
            vendor_count += 1

        # 同步来源配置
        sources = storage.get_sources()
        source_count = 0
        for s in sources:
            mysql_storage.save_source(s)
            source_count += 1

        return {
            "success": True,
            "message": "同步完成",
            "vendors_synced": vendor_count,
            "sources_synced": source_count,
        }
    except Exception as e:
        return {"success": False, "error": f"同步失败: {str(e)[:200]}"}


@app.get("/api/crawler/mysql/status")
def api_mysql_status():
    """获取 MySQL 连接状态"""
    return {
        "success": True,
        "mysql_enabled": MYSQL_ENABLED,
        "message": "MySQL已启用" if MYSQL_ENABLED else "MySQL未启用，请配置环境变量 MYSQL_HOST/MYSQL_PORT/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DATABASE",
    }


# ==================== 爬虫任务管理 ====================

@app.get("/api/crawler/tasks")
def api_crawler_tasks(
    source_id: int = Query(None),
    agent_user_id: int = Query(None),
    limit: int = Query(20),
):
    """获取爬虫任务列表"""
    if not MYSQL_ENABLED:
        return {"success": False, "error": "MySQL未启用"}

    try:
        tasks = mysql_storage.get_tasks(source_id=source_id, agent_user_id=agent_user_id, limit=limit)
        return {"success": True, "tasks": tasks}
    except Exception as e:
        return {"success": False, "error": f"获取任务列表失败: {str(e)[:200]}"}


# ==================== 数据查看 ====================

@app.get("/api/sources/{source_id}/data")
def api_source_data(source_id: int, limit: int = 50):
    """获取某来源的爬取任务列表"""
    tasks = storage.get_tasks(source_id=source_id, limit=limit)
    return {"success": True, "tasks": tasks}


@app.get("/api/sources/{source_id}/data/{task_id}")
def api_task_data(source_id: int, task_id: int):
    """获取某任务的详细数据（含列名）"""
    result = storage.get_task_rows(task_id)
    return {"success": True, "columns": result.get("columns", []), "rows": result.get("rows", [])}


@app.delete("/api/data/{task_id}")
def api_del_data(task_id: int):
    """删除某条爬取任务及其数据"""
    storage.delete_task(task_id)
    return {"success": True}


# ==================== 默认API列表（兼容旧代码） ====================

@app.get("/api/api-list")
def api_api_list():
    """默认数据接口列表"""
    from crawler import API_LIST
    return {"success": True, "apis": API_LIST}


# ==================== 日志 ====================

@app.get("/api/logs")
def api_logs():
    """获取操作日志"""
    return {"success": True, "logs": crawler.get_logs()}


# ==================== 自动抓包 ====================

class SnifferStartReq(BaseModel):
    name: str
    login_url: str
    username: str
    password: str
    data_pages: list = []


@app.post("/api/sniffer/start")
def api_sniffer_start(req: SnifferStartReq):
    """启动抓包：打开浏览器，访问登录页，开始监听"""
    return sniffer.start(req.name, req.login_url, req.username, req.password, req.data_pages)


@app.get("/api/sniffer/status")
def api_sniffer_status():
    """获取抓包状态"""
    return sniffer.get_status()


@app.get("/api/sniffer/requests")
def api_sniffer_requests():
    """获取捕获的请求列表"""
    return {"success": True, "requests": sniffer.get_requests()}


@app.post("/api/sniffer/capture-data")
def api_sniffer_capture_data():
    """捕获数据接口：分析最近的请求，识别数据接口"""
    return sniffer.capture_data()


@app.post("/api/sniffer/confirm-login")
def api_sniffer_confirm_login():
    """用户手动确认登录成功（当自动检测失败时使用）"""
    return sniffer.confirm_login()


@app.post("/api/sniffer/generate")
def api_sniffer_generate():
    """生成厂商配置，保存到数据库（同名则更新）"""
    return sniffer.generate_vendor()


@app.post("/api/sniffer/stop")
def api_sniffer_stop():
    """停止抓包，关闭浏览器"""
    return sniffer.stop()


# ==================== 智能更新策略（手动触发+定时任务） ====================

class ManualUpdateReq(BaseModel):
    source_id: int
    data_type: str = "player"  # player=玩家账号, recharge=充值明细, role=角色信息


@app.post("/api/crawler/update/manual")
def api_manual_update(req: ManualUpdateReq):
    """
    手动触发更新（带冷却时间1小时，只爬最近24小时增量数据）
    
    平台点击"更新数据"按钮时调用此接口
    """
    if not SCHEDULER_ENABLED:
        return {"success": False, "error": "更新策略模块未启用"}

    try:
        result = crawler_scheduler.manual_update(req.source_id, req.data_type)
        return result
    except Exception as e:
        return {"success": False, "error": f"触发更新失败: {str(e)[:200]}"}


@app.get("/api/crawler/update/status")
def api_update_status(source_id: int = Query(None)):
    """获取更新状态（上次更新时间、失败次数、是否暂停、下次更新时间等）"""
    if not SCHEDULER_ENABLED:
        return {"success": False, "error": "更新策略模块未启用"}

    try:
        status = update_strategy.get_update_status(source_id=source_id)
        # 获取定时任务列表
        jobs = []
        if crawler_scheduler.is_running:
            jobs = crawler_scheduler.get_jobs()
        return {
            "success": True,
            "status": status,
            "scheduler_running": crawler_scheduler.is_running,
            "scheduled_jobs": jobs,
            "update_intervals": {
                "player": "24小时（每天凌晨2:00）",
                "recharge": "6小时（2:00/8:00/14:00/20:00）",
                "role": "24小时（每天凌晨3:00）",
            },
            "manual_cooldown": "1小时",
            "request_interval": "3-5秒（随机）",
        }
    except Exception as e:
        return {"success": False, "error": f"获取更新状态失败: {str(e)[:200]}"}


class ResumeSourceReq(BaseModel):
    source_id: int
    data_type: str


@app.post("/api/crawler/update/resume")
def api_resume_source(req: ResumeSourceReq):
    """恢复暂停的来源（连续失败3次后会自动暂停，可手动恢复）"""
    if not SCHEDULER_ENABLED:
        return {"success": False, "error": "更新策略模块未启用"}

    try:
        result = crawler_scheduler.resume_source(req.source_id, req.data_type)
        return result
    except Exception as e:
        return {"success": False, "error": f"恢复来源失败: {str(e)[:200]}"}


@app.post("/api/crawler/scheduler/start")
def api_start_scheduler():
    """启动定时任务"""
    if not SCHEDULER_ENABLED:
        return {"success": False, "error": "定时任务模块未启用"}

    try:
        if crawler_scheduler.is_running:
            return {"success": True, "message": "定时任务已在运行中"}
        crawler_scheduler.start()
        return {"success": True, "message": "定时任务已启动"}
    except Exception as e:
        return {"success": False, "error": f"启动定时任务失败: {str(e)[:200]}"}


@app.post("/api/crawler/scheduler/stop")
def api_stop_scheduler():
    """停止定时任务"""
    if not SCHEDULER_ENABLED:
        return {"success": False, "error": "定时任务模块未启用"}

    try:
        if not crawler_scheduler.is_running:
            return {"success": True, "message": "定时任务未在运行"}
        crawler_scheduler.stop()
        return {"success": True, "message": "定时任务已停止"}
    except Exception as e:
        return {"success": False, "error": f"停止定时任务失败: {str(e)[:200]}"}


if __name__ == "__main__":
    import uvicorn
    # 启动定时任务
    if SCHEDULER_ENABLED:
        try:
            crawler_scheduler.start()
        except Exception as e:
            print(f"⚠️ 定时任务启动失败: {str(e)[:100]}")
    uvicorn.run(app, host="0.0.0.0", port=8000)
