# -*- coding: utf-8 -*-
"""
更新策略模块：实现智能更新策略，避免频繁请求触发厂商风控

策略：
1. 数据类型分级更新：玩家账号每天1次，充值明细每6小时1次，角色信息每天1次
2. 手动触发更新：带冷却时间（1小时），只爬最近24小时增量数据
3. 增量更新：只爬上次更新时间之后的新增数据
4. 请求限流：每次请求间隔3-5秒（随机）
5. 失败重试：失败后延迟30分钟重试，连续失败3次暂停自动更新
6. 智能降级：检测到429错误时自动降低更新频率
"""
import json
import time
import random
import threading
from datetime import datetime, timedelta
from pathlib import Path


class UpdateStrategy:
    """更新策略管理类"""

    def __init__(self, storage=None, mysql_storage=None):
        self.storage = storage  # SQLite存储（原有）
        self.mysql_storage = mysql_storage  # MySQL存储（新增）
        self.lock = threading.Lock()  # 线程锁，防止并发冲突

        # 更新间隔配置（秒）
        self.update_intervals = {
            'player': 24 * 3600,      # 玩家账号：24小时
            'recharge': 6 * 3600,      # 充值明细：6小时
            'role': 24 * 3600,         # 角色信息：24小时
        }

        # 手动触发冷却时间（秒）
        self.manual_cooldown = 3600  # 1小时

        # 请求间隔范围（秒）
        self.request_interval_min = 3
        self.request_interval_max = 5

        # 失败重试配置
        self.retry_delay = 30 * 60  # 30分钟
        self.max_fail_count = 3      # 连续失败3次暂停

        # 增量更新时间窗口（小时）
        self.incremental_window = 24  # 手动触发只爬最近24小时

        # 状态文件路径
        self.state_file = Path(__file__).resolve().parent / 'data' / 'update_state.json'
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        # 加载状态
        self.state = self._load_state()

    # ---------- 状态管理 ----------
    def _load_state(self):
        """加载更新状态"""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            'last_update': {},      # 上次更新时间：{source_id: {data_type: timestamp}}
            'fail_count': {},       # 失败次数：{source_id: {data_type: count}}
            'paused': {},           # 暂停状态：{source_id: {data_type: true}}
            'manual_last_trigger': {},  # 手动触发时间：{source_id: {data_type: timestamp}}
            'rate_limited': {},     # 限流状态：{source_id: {data_type: until_timestamp}}
        }

    def _save_state(self):
        """保存更新状态"""
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存更新状态失败: {str(e)[:100]}")

    def _get_source_key(self, source_id, data_type):
        """获取来源键"""
        return f"{source_id}_{data_type}"

    # ---------- 更新间隔检查 ----------
    def should_auto_update(self, source_id, data_type):
        """
        检查是否应该自动更新
        
        Returns:
            tuple: (should_update, reason)
        """
        key = self._get_source_key(source_id, data_type)

        # 检查是否暂停
        if self.state['paused'].get(key, False):
            return False, "该来源已暂停自动更新（连续失败次数过多），请手动触发或检查配置"

        # 检查是否限流
        rate_limited_until = self.state['rate_limited'].get(key, 0)
        if rate_limited_until > time.time():
            remaining = int(rate_limited_until - time.time())
            return False, f"该来源处于限流状态，还需等待 {remaining//60} 分钟"

        # 检查更新间隔
        last_update = self.state['last_update'].get(key, 0)
        interval = self.update_intervals.get(data_type, 24 * 3600)
        if time.time() - last_update < interval:
            remaining = int(interval - (time.time() - last_update))
            return False, f"距离上次更新不足 {interval//3600} 小时，还需等待 {remaining//3600}小时{remaining%3600//60}分钟"

        return True, "可以更新"

    # ---------- 手动触发冷却时间 ----------
    def can_manual_trigger(self, source_id, data_type):
        """
        检查是否可以手动触发更新
        
        Returns:
            tuple: (can_trigger, reason)
        """
        key = self._get_source_key(source_id, data_type)

        # 检查冷却时间
        last_trigger = self.state['manual_last_trigger'].get(key, 0)
        if time.time() - last_trigger < self.manual_cooldown:
            remaining = int(self.manual_cooldown - (time.time() - last_trigger))
            return False, f"手动触发冷却时间未到，还需等待 {remaining//60}分{remaining%60}秒（冷却时间1小时）"

        return True, "可以触发"

    def record_manual_trigger(self, source_id, data_type):
        """记录手动触发时间"""
        with self.lock:
            key = self._get_source_key(source_id, data_type)
            self.state['manual_last_trigger'][key] = time.time()
            self._save_state()

    # ---------- 更新时间记录 ----------
    def record_update_success(self, source_id, data_type):
        """记录更新成功"""
        with self.lock:
            key = self._get_source_key(source_id, data_type)
            self.state['last_update'][key] = time.time()
            self.state['fail_count'][key] = 0  # 重置失败次数
            self._save_state()

    def record_update_failure(self, source_id, data_type, error_msg=""):
        """记录更新失败"""
        with self.lock:
            key = self._get_source_key(source_id, data_type)
            fail_count = self.state['fail_count'].get(key, 0) + 1
            self.state['fail_count'][key] = fail_count

            # 检测429错误，触发限流
            if '429' in error_msg or '过于频繁' in error_msg or 'rate limit' in error_msg.lower():
                # 限流2小时
                self.state['rate_limited'][key] = time.time() + 2 * 3600
                print(f"⚠️ 检测到429限流，来源 {source_id} 数据类型 {data_type} 限流2小时")

            # 连续失败3次，暂停自动更新
            if fail_count >= self.max_fail_count:
                self.state['paused'][key] = True
                print(f"⚠️ 来源 {source_id} 数据类型 {data_type} 连续失败{fail_count}次，已暂停自动更新")

            self._save_state()

    def resume_source(self, source_id, data_type):
        """恢复暂停的来源"""
        with self.lock:
            key = self._get_source_key(source_id, data_type)
            self.state['paused'][key] = False
            self.state['fail_count'][key] = 0
            self.state['rate_limited'][key] = 0
            self._save_state()

    # ---------- 请求限流 ----------
    def wait_before_request(self):
        """请求前等待（随机3-5秒）"""
        wait_time = random.uniform(self.request_interval_min, self.request_interval_max)
        time.sleep(wait_time)
        return wait_time

    # ---------- 增量更新时间范围 ----------
    def get_incremental_time_range(self, source_id, data_type, is_manual=False):
        """
        获取增量更新的时间范围
        
        Returns:
            tuple: (start_time, end_time) 格式：YYYY-MM-DD HH:MM:SS
        """
        end_time = datetime.now()

        if is_manual:
            # 手动触发：只爬最近24小时
            start_time = end_time - timedelta(hours=self.incremental_window)
        else:
            # 自动更新：从上次更新时间开始
            key = self._get_source_key(source_id, data_type)
            last_update = self.state['last_update'].get(key, 0)
            if last_update:
                start_time = datetime.fromtimestamp(last_update)
                # 多往前1小时，避免边界数据遗漏
                start_time -= timedelta(hours=1)
            else:
                # 首次更新：爬最近7天
                start_time = end_time - timedelta(days=7)

        return (
            start_time.strftime('%Y-%m-%d %H:%M:%S'),
            end_time.strftime('%Y-%m-%d %H:%M:%S')
        )

    # ---------- 获取更新状态 ----------
    def get_update_status(self, source_id=None):
        """获取更新状态"""
        result = []
        for key, last_update in self.state['last_update'].items():
            parts = key.split('_', 1)
            if len(parts) != 2:
                continue
            sid, data_type = parts
            if source_id and str(sid) != str(source_id):
                continue

            fail_count = self.state['fail_count'].get(key, 0)
            paused = self.state['paused'].get(key, False)
            rate_limited_until = self.state['rate_limited'].get(key, 0)
            manual_last_trigger = self.state['manual_last_trigger'].get(key, 0)

            result.append({
                'source_id': int(sid),
                'data_type': data_type,
                'last_update': datetime.fromtimestamp(last_update).strftime('%Y-%m-%d %H:%M:%S') if last_update else '从未更新',
                'fail_count': fail_count,
                'paused': paused,
                'rate_limited': rate_limited_until > time.time(),
                'rate_limited_remaining': max(0, int(rate_limited_until - time.time())),
                'manual_cooldown_remaining': max(0, int(self.manual_cooldown - (time.time() - manual_last_trigger))),
                'next_auto_update': self._get_next_update_time(sid, data_type),
            })
        return result

    def _get_next_update_time(self, source_id, data_type):
        """获取下次自动更新时间"""
        key = self._get_source_key(source_id, data_type)
        last_update = self.state['last_update'].get(key, 0)
        interval = self.update_intervals.get(data_type, 24 * 3600)
        if not last_update:
            return '立即更新'
        next_time = last_update + interval
        if next_time < time.time():
            return '立即更新'
        return datetime.fromtimestamp(next_time).strftime('%Y-%m-%d %H:%M:%S')

    # ---------- 获取所有需要自动更新的来源 ----------
    def get_sources_to_update(self, data_type=None):
        """获取所有需要自动更新的来源"""
        if not self.storage:
            return []

        sources = self.storage.get_sources()
        result = []
        for source in sources:
            source_id = source.get('id')
            if not source_id:
                continue

            # 检查该来源支持的数据类型
            vendor = self.storage.get_vendor(source.get('vendor_id', 0))
            if not vendor:
                continue

            data_apis = vendor.get('data_apis', {})
            if isinstance(data_apis, str):
                try:
                    data_apis = json.loads(data_apis)
                except Exception:
                    data_apis = {}

            # 数据类型映射
            type_mapping = {
                'player': ['玩家账号', '用户列表', 'game_user_list'],
                'recharge': ['充值明细', '充值记录', 'game_recharge_list'],
                'role': ['角色信息', '角色列表', 'game_role_list'],
            }

            for dt, keywords in type_mapping.items():
                if data_type and dt != data_type:
                    continue

                # 检查厂商是否支持该数据类型
                supported = False
                for api_name in data_apis.keys():
                    for keyword in keywords:
                        if keyword in api_name:
                            supported = True
                            break
                    if supported:
                        break

                if not supported:
                    continue

                # 检查是否应该更新
                should_update, reason = self.should_auto_update(source_id, dt)
                if should_update:
                    result.append({
                        'source': source,
                        'vendor': vendor,
                        'data_type': dt,
                        'reason': reason,
                    })

        return result
