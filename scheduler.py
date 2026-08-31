# -*- coding: utf-8 -*-
"""
定时任务管理模块：使用 APScheduler 实现定时自动更新

定时任务：
1. 玩家账号：每天凌晨2:00更新
2. 充值明细：每天2:00/8:00/14:00/20:00更新
3. 角色信息：每天凌晨3:00更新

特点：
- 错峰更新：避免在厂商业务高峰期更新
- 增量更新：只爬上次更新之后的数据
- 请求限流：每次请求间隔3-5秒
- 失败重试：失败后延迟重试
- 智能降级：检测到429错误时自动降低频率
"""
import json
import time
import threading
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


class CrawlerScheduler:
    """爬虫定时任务管理类"""

    def __init__(self, update_strategy, crawler, storage, mysql_storage=None):
        self.update_strategy = update_strategy  # 更新策略
        self.crawler = crawler  # 爬虫实例
        self.storage = storage  # SQLite存储
        self.mysql_storage = mysql_storage  # MySQL存储
        self.scheduler = BackgroundScheduler(timezone='Asia/Shanghai')
        self.is_running = False
        self.update_lock = threading.Lock()  # 更新锁，防止并发冲突
        self.running_tasks = set()  # 正在运行的任务

    def start(self):
        """启动定时任务"""
        if self.is_running:
            print("⚠️ 定时任务已在运行中")
            return

        # 玩家账号：每天凌晨2:00更新
        self.scheduler.add_job(
            self._auto_update_player,
            trigger=CronTrigger(hour=2, minute=0, timezone='Asia/Shanghai'),
            id='auto_update_player',
            name='自动更新玩家账号',
            replace_existing=True,
        )

        # 充值明细：每天2:00/8:00/14:00/20:00更新
        self.scheduler.add_job(
            self._auto_update_recharge,
            trigger=CronTrigger(hour='2,8,14,20', minute=0, timezone='Asia/Shanghai'),
            id='auto_update_recharge',
            name='自动更新充值明细',
            replace_existing=True,
        )

        # 角色信息：每天凌晨3:00更新
        self.scheduler.add_job(
            self._auto_update_role,
            trigger=CronTrigger(hour=3, minute=0, timezone='Asia/Shanghai'),
            id='auto_update_role',
            name='自动更新角色信息',
            replace_existing=True,
        )

        # 失败重试检查：每30分钟检查一次
        self.scheduler.add_job(
            self._check_failed_tasks,
            trigger=CronTrigger(minute='*/30', timezone='Asia/Shanghai'),
            id='check_failed_tasks',
            name='检查失败任务并重试',
            replace_existing=True,
        )

        self.scheduler.start()
        self.is_running = True
        print("✅ 定时任务已启动")
        print("   - 玩家账号：每天 02:00 更新")
        print("   - 充值明细：每天 02:00/08:00/14:00/20:00 更新")
        print("   - 角色信息：每天 03:00 更新")
        print("   - 失败重试：每30分钟检查一次")

    def stop(self):
        """停止定时任务"""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            self.is_running = False
            print("⏹️ 定时任务已停止")

    def get_jobs(self):
        """获取所有定时任务"""
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append({
                'id': job.id,
                'name': job.name,
                'next_run_time': job.next_run_time.strftime('%Y-%m-%d %H:%M:%S') if job.next_run_time else None,
                'trigger': str(job.trigger),
            })
        return jobs

    # ---------- 自动更新任务 ----------
    def _auto_update_player(self):
        """自动更新玩家账号"""
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🔄 开始自动更新玩家账号...")
        self._auto_update_by_type('player')

    def _auto_update_recharge(self):
        """自动更新充值明细"""
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🔄 开始自动更新充值明细...")
        self._auto_update_by_type('recharge')

    def _auto_update_role(self):
        """自动更新角色信息"""
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🔄 开始自动更新角色信息...")
        self._auto_update_by_type('role')

    def _auto_update_by_type(self, data_type):
        """按数据类型自动更新所有来源"""
        # 获取需要更新的来源
        sources_to_update = self.update_strategy.get_sources_to_update(data_type)
        print(f"   需要更新的来源数量: {len(sources_to_update)}")

        for item in sources_to_update:
            source = item['source']
            vendor = item['vendor']
            source_id = source.get('id')

            # 检查是否正在运行
            task_key = f"{source_id}_{data_type}"
            if task_key in self.running_tasks:
                print(f"   ⏭️ 来源 {source_id} 数据类型 {data_type} 正在更新中，跳过")
                continue

            # 执行更新
            try:
                self._execute_update(source, vendor, data_type, is_manual=False)
            except Exception as e:
                print(f"   ❌ 来源 {source_id} 更新失败: {str(e)[:100]}")
                self.update_strategy.record_update_failure(source_id, data_type, str(e))

            # 请求间隔
            self.update_strategy.wait_before_request()

        print(f"✅ {data_type} 数据类型自动更新完成")

    # ---------- 手动触发更新 ----------
    def manual_update(self, source_id, data_type):
        """
        手动触发更新
        
        Returns:
            dict: {success, message, task_id}
        """
        # 检查冷却时间
        can_trigger, reason = self.update_strategy.can_manual_trigger(source_id, data_type)
        if not can_trigger:
            return {'success': False, 'message': reason}

        # 检查是否正在运行
        task_key = f"{source_id}_{data_type}"
        if task_key in self.running_tasks:
            return {'success': False, 'message': '该来源正在更新中，请稍候再试'}

        # 获取来源和厂商配置
        source = self.storage.get_source(source_id)
        if not source:
            return {'success': False, 'message': '来源不存在'}

        vendor = self.storage.get_vendor(source.get('vendor_id', 0))
        if not vendor:
            return {'success': False, 'message': '厂商配置不存在'}

        # 记录手动触发时间
        self.update_strategy.record_manual_trigger(source_id, data_type)

        # 在后台线程执行更新
        thread = threading.Thread(
            target=self._execute_update,
            args=(source, vendor, data_type, True),
            daemon=True,
        )
        thread.start()

        return {
            'success': True,
            'message': '更新任务已启动，请在操作日志中查看进度',
            'task_id': task_key,
        }

    # ---------- 执行更新 ----------
    def _execute_update(self, source, vendor, data_type, is_manual=False):
        """执行更新（核心逻辑）"""
        source_id = source.get('id')
        task_key = f"{source_id}_{data_type}"

        with self.update_lock:
            self.running_tasks.add(task_key)

        try:
            print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🚀 开始更新来源 {source_id} ({source.get('name', source.get('username'))}) - {data_type}")

            # 获取增量更新时间范围
            start_time, end_time = self.update_strategy.get_incremental_time_range(
                source_id, data_type, is_manual
            )
            print(f"   时间范围: {start_time} ~ {end_time}")

            # 数据类型映射到厂商配置中的接口名
            type_mapping = {
                'player': ['玩家账号', '用户列表', 'game_user_list'],
                'recharge': ['充值明细', '充值记录', 'game_recharge_list'],
                'role': ['角色信息', '角色列表', 'game_role_list'],
            }

            # 查找对应的数据接口配置
            data_apis = vendor.get('data_apis', {})
            if isinstance(data_apis, str):
                try:
                    data_apis = json.loads(data_apis)
                except Exception:
                    data_apis = {}

            api_name = None
            api_config = None
            for name, config in data_apis.items():
                for keyword in type_mapping.get(data_type, []):
                    if keyword in name:
                        api_name = name
                        api_config = config
                        break
                if api_name:
                    break

            if not api_name or not api_config:
                raise Exception(f"厂商 {vendor.get('name')} 未配置 {data_type} 数据接口")

            print(f"   数据接口: {api_name}")

            # 获取访问令牌
            access_token = source.get('access_token')
            if not access_token:
                print("   未找到访问令牌，尝试登录...")
                # 这里需要调用登录逻辑，暂时跳过
                # access_token = self._login(source, vendor)
                # if not access_token:
                #     raise Exception("登录失败，无法获取访问令牌")
                raise Exception("未找到访问令牌，请先在登录页面登录")

            # 构建请求参数
            api_path = api_config.get('path', '')
            method = api_config.get('method', 'GET')
            page_param = api_config.get('page_param', 'pageNum')
            page_size_param = api_config.get('page_size_param', 'pageSize')
            data_path = api_config.get('data_path', 'data.list')
            total_path = api_config.get('total_path', 'data.total')

            # 分页爬取
            all_data = []
            page = 1
            page_size = 50
            max_pages = 10  # 最多爬10页

            while page <= max_pages:
                print(f"   正在爬取第 {page} 页...")

                # 构建参数
                params = {
                    page_param: page,
                    page_size_param: page_size,
                }

                # 添加时间范围参数（如果接口支持）
                if api_config.get('start_time_param'):
                    params[api_config['start_time_param']] = start_time
                if api_config.get('end_time_param'):
                    params[api_config['end_time_param']] = end_time

                # 发送请求
                try:
                    result = self.crawler.fetch_data(
                        api_path=api_path,
                        access_token=access_token,
                        params=params,
                        max_pages=1,
                        source_id=source_id,
                        task_name=api_name,
                        api_base_url=source.get('api_base_url') or vendor.get('api_base_url'),
                        data_api_config=api_config,
                    )

                    if not result.get('success'):
                        raise Exception(f"请求失败: {result.get('error', '未知错误')}")

                    page_data = result.get('data', [])
                    if not page_data:
                        print(f"   第 {page} 页无数据，停止爬取")
                        break

                    all_data.extend(page_data)
                    print(f"   第 {page} 页获取 {len(page_data)} 条数据，累计 {len(all_data)} 条")

                    # 检查是否还有下一页
                    total = result.get('total', 0)
                    if total and len(all_data) >= total:
                        print(f"   已获取全部数据（共 {total} 条）")
                        break

                    page += 1

                    # 请求间隔
                    self.update_strategy.wait_before_request()

                except Exception as e:
                    error_msg = str(e)
                    print(f"   ❌ 第 {page} 页爬取失败: {error_msg[:100]}")
                    self.update_strategy.record_update_failure(source_id, data_type, error_msg)
                    raise

            # 保存数据到MySQL
            if self.mysql_storage and all_data:
                print(f"   正在保存 {len(all_data)} 条数据到MySQL...")
                vendor_id = source.get('vendor_id')
                agent_user_id = source.get('agent_user_id')
                save_result = self.mysql_storage.save_crawl_data(
                    source_id=source_id,
                    vendor_id=vendor_id,
                    agent_user_id=agent_user_id,
                    data_type=data_type,
                    data_list=all_data,
                )
                print(f"   ✅ 保存完成: 总计{save_result['total']}条, 新增{save_result['inserted']}条, 更新{save_result['updated']}条")

            # 记录更新成功
            self.update_strategy.record_update_success(source_id, data_type)
            print(f"✅ 来源 {source_id} - {data_type} 更新完成，共获取 {len(all_data)} 条数据")

            return all_data

        except Exception as e:
            error_msg = str(e)
            print(f"❌ 来源 {source_id} - {data_type} 更新失败: {error_msg[:200]}")
            self.update_strategy.record_update_failure(source_id, data_type, error_msg)
            raise

        finally:
            self.running_tasks.discard(task_key)

    # ---------- 失败重试检查 ----------
    def _check_failed_tasks(self):
        """检查失败任务并重试"""
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🔍 检查失败任务...")

        # 遍历所有来源，检查是否有失败但未暂停的任务
        sources = self.storage.get_sources()
        retry_count = 0

        for source in sources:
            source_id = source.get('id')
            if not source_id:
                continue

            for data_type in ['player', 'recharge', 'role']:
                key = f"{source_id}_{data_type}"
                fail_count = self.update_strategy.state['fail_count'].get(key, 0)
                paused = self.update_strategy.state['paused'].get(key, False)

                # 有失败但未暂停，且超过重试延迟时间，尝试重试
                if fail_count > 0 and not paused:
                    last_update = self.update_strategy.state['last_update'].get(key, 0)
                    if time.time() - last_update > self.update_strategy.retry_delay:
                        print(f"   🔄 重试来源 {source_id} - {data_type}（失败次数: {fail_count}）")
                        try:
                            vendor = self.storage.get_vendor(source.get('vendor_id', 0))
                            if vendor:
                                self._execute_update(source, vendor, data_type, is_manual=False)
                                retry_count += 1
                        except Exception as e:
                            print(f"   ❌ 重试失败: {str(e)[:100]}")

        if retry_count > 0:
            print(f"✅ 失败任务重试完成，共重试 {retry_count} 个任务")
        else:
            print("   暂无需要重试的失败任务")

    # ---------- 恢复暂停的来源 ----------
    def resume_source(self, source_id, data_type):
        """恢复暂停的来源"""
        self.update_strategy.resume_source(source_id, data_type)
        return {'success': True, 'message': f'来源 {source_id} - {data_type} 已恢复自动更新'}
