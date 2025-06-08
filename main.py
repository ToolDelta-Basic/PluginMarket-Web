from http.cookies import SimpleCookie
import os
import sys
import git
import shutil
import time
import colorama
import requests
import signal
import threading
import datetime
import random
import string
import json
import multiprocessing
import ipaddress
import hashlib
import uuid
import sqlite3
from logger import Logger
from queue import Full
from apscheduler.schedulers.background import BackgroundScheduler
from flask_cors import CORS
from flask import Flask, jsonify, render_template, request, g, make_response
from waitress import serve
from exception import OutPutQueueFullError
from typing import Dict
from flask_sockets import Sockets, Rule
from geventwebsocket.handler import WebSocketHandler
from gevent.pywsgi import WSGIServer
from openai import OpenAI

# 全局锁和事件
IOLock: threading.RLock = threading.RLock()
ThreadEvent: threading.Event = threading.Event()

class GitRepositoryManager:
    def __init__(self, logger, update_interval=60, cleanup_days=30):
        self.repositories = {}
        self.update_interval = update_interval  # 更新间隔，单位为分钟
        self.cleanup_days = cleanup_days
        self.ssh_private_key_path = None  # 用于SSH私有仓库的身份验证
        self.scheduler = BackgroundScheduler()
        self.logger = logger
        self.ws_client = None

    def add_repository(self, repo_id, repo_url, clone_path, update_interval=None):
        if repo_id in self.repositories:
            self.logger.warn("仓库", f"仓库 {repo_id} 已经存在，跳过添加。")
            return

        repo_config = {
            "repo_id": repo_id,
            "repo_url": repo_url,
            "clone_path": clone_path,
            "update_interval": update_interval or self.update_interval,
            "last_updated": None
        }

        self.repositories[repo_id] = repo_config

        # 初始化克隆仓库
        self.clone_repository(repo_id)

        # 设置定时更新任务
        self.setup_update_job(repo_id, repo_config["update_interval"])

        self.logger.info("仓库", f"成功添加仓库 {repo_id}")

    def clone_repository(self, repo_id):
        repo_config = self.repositories.get(repo_id)
        if not repo_config:
            self.logger.erro("仓库", f"仓库 {repo_id} 未找到。")
            return

        repo_url = repo_config["repo_url"]
        clone_path = repo_config["clone_path"]

        try:
            # 检查克隆路径是否存在且不为空
            if os.path.exists(clone_path) and os.listdir(clone_path):
                # 路径存在且不为空，直接初始化仓库
                self.logger.info("仓库", f"仓库 {repo_id} 已存在，跳过克隆。")
                repo_config["repo_obj"] = git.Repo(clone_path)
                repo_config["last_updated"] = datetime.datetime.now()
                return
            else:
                # 路径不存在或为空，克隆仓库
                if os.path.exists(clone_path):
                    self.logger.info("仓库", f"删除旧仓库 {clone_path}")
                    shutil.rmtree(clone_path)
                os.makedirs(clone_path, exist_ok=True)

                # 根据URL类型选择克隆方法
                if repo_url.startswith("git@"):
                    if not self.ssh_private_key_path:
                        self.logger.erro("缺少SSH私钥路径，无法克隆私有仓库。")
                        return
                    # 设置SSH私钥
                    self._setup_ssh_key()
                    repo = git.Repo.clone_from(repo_url, clone_path)
                else:
                    repo = git.Repo.clone_from(repo_url, clone_path)

                repo_config["repo_obj"] = repo
                repo_config["last_updated"] = datetime.datetime.now()
                self.logger.info("仓库", f"成功克隆仓库 {repo_id}")
        except Exception as e:
            self.logger.erro("仓库", f"克隆仓库 {repo_id} 失败: {str(e)}")
            # 清理可能存在的部分克隆文件
            if os.path.exists(clone_path):
                shutil.rmtree(clone_path)

    def update_repository(self, repo_id):
        repo_config = self.repositories.get(repo_id)
        if not repo_config:
            self.logger.erro("仓库", f"仓库 {repo_id} 未找到。")
            return

        repo = repo_config.get("repo_obj")
        if not repo:
            self.logger.erro("仓库", f"仓库 {repo_id} 尚未初始化。")
            return

        try:
            # 获取远程仓库
            origin = repo.remotes.origin
            origin.fetch()
            # 获取最新commit
            latest_commit = origin.refs.main.commit
            last_commit = repo.commit('HEAD')

            # 检查是否有更新
            if latest_commit.hexsha == last_commit.hexsha:
                self.logger.info("仓库", f"仓库 {repo_id} 已是最新版本。")
                return

            # 拉取最新代码
            repo.heads.main.checkout(True)
            self.logger.info("仓库", f"成功更新仓库 {repo_id}")

            # 通知客户端更新
            self.notify_client_update(repo_id)

            repo_config["last_updated"] = datetime.datetime.now()
        except git.exc.Giterro as e:
            self.logger.erro("仓库", f"更新仓库 {repo_id} 失败: {str(e)}")

    def setup_update_job(self, repo_id, interval_minutes):
        repo_config = self.repositories.get(repo_id)
        if not repo_config:
            self.logger.erro("仓库", f"仓库 {repo_id} 未找到。")
            return

        def update_job():
            self.update_repository(repo_id)

        # 创建定时任务
        if not self.scheduler.running:
            self.scheduler.add_job(update_job, 'interval', minutes=interval_minutes)
            self.scheduler.start()
            self.logger.info("仓库", f"仓库 {repo_id} 设置了每 {interval_minutes} 分钟更新一次的任务。")
        else:
            self.logger.warn("仓库", f"调度器已经在运行，仓库 {repo_id} 的更新任务已存在")

    def cleanup_repositories(self):
        current_date = datetime.datetime.now()
        for repo_id, repo_config in self.repositories.items():
            last_updated = repo_config["last_updated"]
            if last_updated:
                days_diff = (current_date - last_updated).days
                if days_diff > self.cleanup_days:
                    self.logger.info("仓库", f"仓库 {repo_id} 未在 {self.cleanup_days} 天内更新，进行清理。")
                    if os.path.exists(repo_config["clone_path"]):
                        shutil.rmtree(repo_config["clone_path"])
                        self.logger.info("仓库", f"成功删除本地仓库 {repo_config['clone_path']}")
                    del self.repositories[repo_id]
                    # 取消相关定时任务
                    if repo_id in [job.id for job in self.scheduler.get_jobs()]:
                        self.scheduler.remove_job(repo_id)

    def notify_client_update(self, repo_id):
        if not self.ws_client:
            # self.logger.warn("WebSocket客户端未初始化，无法通知客户端。")
            return

        try:
            notification = {
                "type": "repository_updated",
                "repo_id": repo_id,
                "timestamp": datetime.datetime.now().isoformat()
            }
            self.ws_client.send(json.dumps(notification))
            self.logger.info("仓库", f"成功通知客户端仓库 {repo_id} 已更新。")
        except Exception as e:
            self.logger.erro("仓库", f"通知客户端失败: {str(e)}")

    def _setup_ssh_key(self):
        if not self.ssh_private_key_path:
            return

        if not os.path.exists(self.ssh_private_key_path):
            self.logger.erro("SSH私钥文件路径不存在。")
            return

        os.environ['GIT_SSH_COMMAND'] = "仓库", f"ssh -i {self.ssh_private_key_path}"

    def __del__(self):
        for repo_config in self.repositories.values():
            if repo_config.get("repo_obj"):
                repo_config["repo_obj"].close()
        if self.scheduler and self.scheduler.running:
            self.logger.info("正在关闭定时任务调度器...")
            self.scheduler.shutdown(wait=False)
            self.logger.info("定时任务调度器已关闭。")
        self.logger.info("GitRepositoryManager 释放资源。")

class NSockets(Sockets):
    def __init__(self, app=None):
        super().__init__(app)

    def add_url_rule(self, rule, _, f, **options):
       self.url_map.add(Rule(rule, endpoint=f, websocket=True))

class StatsManager:
    """插件统计管理器（下载次数、评分）"""
    def __init__(self, workpath: str, logger: Logger = None):
        self.logger = logger
        self.workpath = workpath
        self.stats_db = os.path.join(workpath, "plugin_stats.db")
        self._init_db()
    
    def _init_db(self):
        """初始化数据库"""
        with sqlite3.connect(self.stats_db) as conn:
            c = conn.cursor()
            # 创建插件下载表
            c.execute('''CREATE TABLE IF NOT EXISTS plugin_downloads (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         plugin_id TEXT NOT NULL,
                         user_id TEXT NOT NULL,
                         timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                         )''')
            
            # 创建整合包下载表
            c.execute('''CREATE TABLE IF NOT EXISTS package_downloads (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         package_id TEXT NOT NULL,
                         user_id TEXT NOT NULL,
                         timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                         )''')
            
            # 创建插件评分表
            c.execute('''CREATE TABLE IF NOT EXISTS plugin_ratings (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         plugin_id TEXT NOT NULL,
                         rating INTEGER NOT NULL,
                         timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                         )''')
            
            # 创建索引以提高查询性能
            c.execute('''CREATE INDEX IF NOT EXISTS idx_plugin_id ON plugin_downloads (plugin_id)''')
            c.execute('''CREATE INDEX IF NOT EXISTS idx_package_id ON package_downloads (package_id)''')
            c.execute('''CREATE INDEX IF NOT EXISTS idx_rating_plugin_id ON plugin_ratings (plugin_id)''')
            conn.commit()
    
    def record_plugin_download(self, plugin_id, user_id):
        """记录插件下载"""
        with sqlite3.connect(self.stats_db) as conn:
            c = conn.cursor()
            c.execute('''INSERT INTO plugin_downloads (plugin_id, user_id) 
                         VALUES (?, ?)''', (plugin_id, user_id))
            conn.commit()
    
    def record_package_download(self, package_id, user_id):
        """记录整合包下载"""
        with sqlite3.connect(self.stats_db) as conn:
            c = conn.cursor()
            c.execute('''INSERT INTO package_downloads (package_id, user_id) 
                         VALUES (?, ?)''', (package_id, user_id))
            conn.commit()
        
    def record_plugin_rating(self, plugin_id: str, rating: int):
        """记录插件评分（动态更新数据）"""
        try:
            if not 1 <= rating <= 5:
                return False
                
            with sqlite3.connect(self.stats_db) as conn:
                c = conn.cursor()
                c.execute("""
                    INSERT INTO plugin_ratings (plugin_id, rating)
                    VALUES (?, ?)
                """, (plugin_id, rating))
                conn.commit()
            return True
        except Exception as e:
            if self.logger:
                self.logger.erro("数据", f"记录插件评分失败: {str(e)}")
            return False
    
    def get_plugin_stats(self, plugin_id: str) -> dict:
        """获取插件统计信息"""
        stats = {
            "download_count": 0,
            "avg_rating": 0.0,
            "rating_count": 0
        }
        
        try:
            with sqlite3.connect(self.stats_db) as conn:
                c = conn.cursor()
                
                # 获取下载次数 - 使用 COUNT(*) 而不是不存在的 download_count 列
                c.execute("SELECT COUNT(*) FROM plugin_downloads WHERE plugin_id = ?", (plugin_id,))
                if (row := c.fetchone()):
                    stats["download_count"] = row[0]
                
                # 获取评分信息
                c.execute("SELECT rating FROM plugin_ratings WHERE plugin_id = ?", (plugin_id,))
                ratings = [row[0] for row in c.fetchall()]
                
                if ratings:
                    stats["rating_count"] = len(ratings)
                    stats["avg_rating"] = round(sum(ratings) / len(ratings), 1)
        except Exception as e:
            if self.logger:
                self.logger.erro("数据", f"获取插件统计信息失败: {str(e)}")
        
        return stats
    
    def get_package_downloads(self, package_id: str) -> int:
        """获取整合包下载次数"""
        try:
            with sqlite3.connect(self.stats_db) as conn:
                c = conn.cursor()
                # 使用 COUNT(*) 而不是不存在的 download_count 列
                c.execute("SELECT COUNT(*) FROM package_downloads WHERE package_id = ?", (package_id,))
                if (row := c.fetchone()):
                    return row[0]
        except Exception as e:
            if self.logger:
                self.logger.erro("数据", f"获取整合包下载次数失败: {str(e)}")
        return 0
    
    def get_weekly_downloads(self) -> int:
        """获取最近7天的总下载量（包括插件和整合包）"""
        try:
            # 计算7天前的日期时间
            seven_days_ago = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
            
            with sqlite3.connect(self.stats_db) as conn:
                c = conn.cursor()
                
                # 统计插件下载量
                c.execute("""
                    SELECT COUNT(*) 
                    FROM plugin_downloads 
                    WHERE timestamp >= ?
                """, (seven_days_ago,))
                plugin_downloads = c.fetchone()[0] or 0
                
                # 统计整合包下载量
                c.execute("""
                    SELECT COUNT(*) 
                    FROM package_downloads 
                    WHERE timestamp >= ?
                """, (seven_days_ago,))
                package_downloads = c.fetchone()[0] or 0
                
                return plugin_downloads + package_downloads
        except Exception as e:
            if self.logger:
                self.logger.erro("数据", f"获取周下载量失败: {str(e)}")
            return 0
    
    def get_total_plugin_downloads(self) -> int:
        """获取所有插件总下载量"""
        try:
            with sqlite3.connect(self.stats_db) as conn:
                c = conn.cursor()
                # 使用 COUNT(*) 而不是 SUM(download_count)
                c.execute("SELECT COUNT(*) FROM plugin_downloads")
                if (result := c.fetchone()):
                    return result[0] or 0
        except Exception as e:
            if self.logger:
                self.logger.erro("数据", f"获取总下载量失败: {str(e)}")
        return 0
    
    def get_all_plugin_stats(self) -> Dict[str, Dict]:
        """获取所有插件的统计信息（插件ID到统计信息的映射）"""
        stats_dict = {}
        try:
            with sqlite3.connect(self.stats_db) as conn:
                c = conn.cursor()
                
                # 获取所有插件的下载次数
                c.execute("""
                    SELECT plugin_id, COUNT(*) as download_count
                    FROM plugin_downloads
                    GROUP BY plugin_id
                """)
                for plugin_id, count in c.fetchall():
                    stats_dict[plugin_id] = {
                        "download_count": count,
                        "avg_rating": 0.0,
                        "rating_count": 0
                    }
                
                # 获取所有插件的评分数据
                c.execute("""
                    SELECT plugin_id, 
                           COUNT(*) as rating_count,
                           AVG(rating) as avg_rating
                    FROM plugin_ratings
                    GROUP BY plugin_id
                """)
                for plugin_id, rating_count, avg_rating in c.fetchall():
                    if plugin_id in stats_dict:
                        stats_dict[plugin_id]["rating_count"] = rating_count
                        stats_dict[plugin_id]["avg_rating"] = round(avg_rating, 1)
                    else:
                        stats_dict[plugin_id] = {
                            "download_count": 0,
                            "avg_rating": round(avg_rating, 1),
                            "rating_count": rating_count
                        }
                
                # 添加没有下载但有评分的插件
                c.execute("SELECT DISTINCT plugin_id FROM plugin_ratings")
                for (plugin_id,) in c.fetchall():
                    if plugin_id not in stats_dict:
                        stats_dict[plugin_id] = {
                            "download_count": 0,
                            "avg_rating": 0.0,
                            "rating_count": 0
                        }
        except Exception as e:
            if self.logger:
                self.logger.erro("数据", f"获取全量插件统计失败: {str(e)}")
        
        return stats_dict

class GitHubPluginMonitor:
    """监控GitHub上的插件数据文件，定期更新插件和整合包信息，并使用DeepSeek API进行分类和生成描述"""
    
    def __init__(self, github_url: str, deepseek_api_key: str, update_interval: int = 300, logger: Logger = None, stats_manager: StatsManager = None):
        """
        初始化GitHub插件监控
        
        :param github_url: GitHub RAW文件的URL
        :param deepseek_api_key: DeepSeek API密钥
        :param update_interval: 更新间隔（秒），默认5分钟
        :param logger: 日志记录器
        """
        self.github_url = github_url
        self.deepseek_api_key = deepseek_api_key
        self.update_interval = update_interval
        self.stats_manager = stats_manager
        self.logger = logger
        self.market_version = "未知"
        self.source_name = "未知"
        self.greetings = ""
        self.plugins = {}  # 存储所有插件信息的字典
        self.packages = {}  # 存储所有整合包信息的字典
        self.plugins_count = 0
        self.packages_count = 0
        self.last_updated = None
        self.last_etag = None
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.category_cache_file = "plugin_categories.json"
        self.description_cache_file = "plugin_descriptions.json"
        self.category_cache = {}
        self.description_cache = {}
        
        # 初始化 DeepSeek 客户端
        self.deepseek_client = OpenAI(
            api_key=self.deepseek_api_key,
            base_url="https://api.deepseek.com"
        )
        
        # 加载缓存
        self._load_category_cache()
        self._load_description_cache()
        
        # 初始化时立即获取一次数据
        self._fetch_plugin_data()
    
    def _load_category_cache(self):
        """加载分类缓存文件"""
        try:
            if os.path.exists(self.category_cache_file):
                with open(self.category_cache_file, 'r', encoding='utf-8') as f:
                    self.category_cache = json.load(f)
                if self.logger:
                    self.logger.info("分类", f"已加载 {len(self.category_cache)} 条分类缓存")
            else:
                self.category_cache = {}
        except Exception as e:
            if self.logger:
                self.logger.erro("分类", f"加载分类缓存失败: {str(e)}")
            self.category_cache = {}
    
    def _load_description_cache(self):
        """加载描述缓存文件"""
        try:
            if os.path.exists(self.description_cache_file):
                with open(self.description_cache_file, 'r', encoding='utf-8') as f:
                    self.description_cache = json.load(f)
                if self.logger:
                    self.logger.info("描述", f"已加载 {len(self.description_cache)} 条描述缓存")
            else:
                self.description_cache = {}
        except Exception as e:
            if self.logger:
                self.logger.erro("描述", f"加载描述缓存失败: {str(e)}")
            self.description_cache = {}
    
    def _save_category_cache(self):
        """保存分类缓存文件"""
        try:
            with open(self.category_cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.category_cache, f, ensure_ascii=False, indent=2)
            if self.logger:
                self.logger.info("分类", "分类缓存已保存")
        except Exception as e:
            if self.logger:
                self.logger.erro("分类", f"保存分类缓存失败: {str(e)}")
    
    def _save_description_cache(self):
        """保存描述缓存文件"""
        try:
            with open(self.description_cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.description_cache, f, ensure_ascii=False, indent=2)
            if self.logger:
                self.logger.info("描述", "描述缓存已保存")
        except Exception as e:
            if self.logger:
                self.logger.erro("描述", f"保存描述缓存失败: {str(e)}")
    
    def _get_plugin_hash(self, plugin_info: dict) -> str:
        """计算插件信息的哈希值，用于检测变更"""
        data_str = f"{plugin_info['name']}_{plugin_info['author']}_{plugin_info['version']}"
        return hashlib.md5(data_str.encode('utf-8')).hexdigest()
    
    def _classify_plugin(self, plugin_id: str, plugin_info: dict):
        """使用DeepSeek API对插件进行分类"""
        # 检查是否已有分类且信息未变更
        current_hash = self._get_plugin_hash(plugin_info)
        cached_data = self.category_cache.get(plugin_id)
        
        if cached_data and cached_data.get('hash') == current_hash:
            if self.logger:
                self.logger.info("分类", f"使用缓存分类: {plugin_id} -> {cached_data['category']}")
            return cached_data['category']
        
        try:
            # 构建提示词
            prompt = f"""
            请根据以下插件信息将其分类到最合适的类别：
            
            插件名称: {plugin_info['name']}
            作者: {plugin_info['author']}
            版本: {plugin_info['version']}
            
            可选的分类类别：
            1. 实用工具 - 服务器管理、备份、维护等工具
            2. 游戏玩法 - 添加新游戏机制、小游戏等
            3. 经济系统 - 经济、交易、商店相关
            4. 防作弊 - 反作弊、安全防护
            5. 前置库 - 其他插件依赖的基础库
            6. 娱乐 - 音乐、聊天、社交等娱乐功能
            7. 管理 - 玩家管理、权限控制
            8. 其他 - 不适合以上类别的插件
            
            请只返回类别名称，不要包含其他内容。
            """
            
            # 调用 DeepSeek API
            response = self.deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "你是一个插件分类助手，根据插件描述将其分类到最合适的类别。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=50,
                stream=False
            )
            
            # 提取分类结果
            category = response.choices[0].message.content.strip()
            
            # 清理API响应
            category = category.replace('类别:', '').replace('分类:', '').strip()
            
            # 验证分类是否有效
            valid_categories = ["实用工具", "游戏玩法", "经济系统", "防作弊", 
                               "前置库", "娱乐", "管理", "其他"]
            
            # 检查是否为有效分类
            if category not in valid_categories:
                # 尝试提取有效分类
                for valid_cat in valid_categories:
                    if valid_cat in category:
                        category = valid_cat
                        break
                else:
                    category = "其他"
            
            # 更新缓存
            self.category_cache[plugin_id] = {
                "category": category,
                "hash": current_hash,
                "last_updated": datetime.datetime.now().isoformat()
            }
            
            if self.logger:
                self.logger.succ("分类", f"插件 '{plugin_id}' 分类为: {category}")
            
            return category
        except Exception as e:
            if self.logger:
                self.logger.erro("分类", f"插件 '{plugin_id}' 分类失败: {str(e)}")
            return "其他"
    
    def _generate_description(self, plugin_id: str, plugin_info: dict):
        """使用DeepSeek API生成插件描述"""
        # 检查是否已有描述且信息未变更
        current_hash = self._get_plugin_hash(plugin_info)
        cached_data = self.description_cache.get(plugin_id)
        
        if cached_data and cached_data.get('hash') == current_hash:
            if self.logger:
                self.logger.info("描述", f"使用缓存描述: {plugin_id}")
            return cached_data['description']
        
        try:
            # 构建提示词
            prompt = f"""
            请为以下插件生成一段简洁、吸引人的中文描述：
            
            插件名称: {plugin_info['name']}
            作者: {plugin_info['author']}
            版本: {plugin_info['version']}
            
            要求：
            1. 描述应突出插件的核心功能和用途
            2. 长度在50-100字之间
            3. 语言流畅自然，适合插件市场的展示
            4. 使用第二人称（如"您"）增加亲切感
            """
            
            # 调用 DeepSeek API
            response = self.deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "你是一个插件描述生成助手，根据插件信息生成吸引人的描述。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=200,
                stream=False
            )
            
            # 提取描述结果
            description = response.choices[0].message.content.strip()
            
            # 清理API响应
            description = description.replace('描述:', '').replace('插件描述:', '').strip()
            
            # 更新缓存
            self.description_cache[plugin_id] = {
                "description": description,
                "hash": current_hash,
                "last_updated": datetime.datetime.now().isoformat()
            }
            
            if self.logger:
                self.logger.succ("描述", f"插件 '{plugin_id}' 描述生成成功")
            
            return description
        except Exception as e:
            if self.logger:
                self.logger.erro("描述", f"插件 '{plugin_id}' 描述生成失败: {str(e)}")
            return "该插件暂无详细描述"
    
    def start(self):
        """启动监控线程"""
        if self.thread and self.thread.is_alive():
            return
            
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._monitor_loop,
            name="GitHubPluginMonitor",
            daemon=True
        )
        self.thread.start()
        if self.logger:
            self.logger.succ("启动", f"GitHub插件监控已启动，URL: {self.github_url}")
    
    def stop(self):
        """停止监控线程"""
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        if self.logger:
            self.logger.info("停止", "GitHub插件监控已停止")
    
    def get_market_version(self) -> str:
        """获取市场版本号"""
        with self.lock:
            return self.market_version
    
    def get_source_name(self) -> str:
        """获取来源名称"""
        with self.lock:
            return self.source_name
    
    def get_greetings(self) -> str:
        """获取欢迎语"""
        with self.lock:
            return self.greetings
    
    def get_plugins(self) -> dict:
        """获取所有插件信息"""
        with self.lock:
            return self.plugins.copy()  # 返回副本以避免直接修改
    
    def get_packages(self) -> dict:
        """获取所有整合包信息"""
        with self.lock:
            return self.packages.copy()  # 返回副本以避免直接修改
    
    def get_plugins_count(self) -> int:
        """获取插件数量"""
        with self.lock:
            return self.plugins_count
    
    def get_packages_count(self) -> int:
        """获取整合包数量"""
        with self.lock:
            return self.packages_count
    
    def get_last_updated(self) -> str:
        """获取最后更新时间"""
        with self.lock:
            return self.last_updated.isoformat() if self.last_updated else "从未更新"
    
    def _monitor_loop(self):
        """监控循环"""
        while not self.stop_event.is_set():
            try:
                self._fetch_plugin_data()
                
                # 保存缓存
                self._save_category_cache()
                self._save_description_cache()
            except Exception as e:
                if self.logger:
                    self.logger.erro("监控", f"获取插件数据失败: {str(e)}")
            
            # 等待下次更新
            self.stop_event.wait(self.update_interval)
    
    def _fetch_plugin_data(self):
        """从GitHub获取并解析插件数据"""
        headers = {}
        if self.last_etag:
            headers['If-None-Match'] = self.last_etag
        
        try:
            response = requests.get(
                self.github_url,
                headers=headers,
                timeout=10
            )
            
            # 处理304 Not Modified
            if response.status_code == 304:
                if self.logger:
                    self.logger.info("监控", "插件数据未变更")
                return
                
            # 检查响应状态
            if response.status_code != 200:
                if self.logger:
                    self.logger.erro("监控", f"获取插件数据失败，HTTP状态码: {response.status_code}")
                return
                
            # 更新ETag
            self.last_etag = response.headers.get('ETag')
            
            # 解析JSON数据
            try:
                data = response.json()
                
                # 提取基本信息
                market_version = data.get("MarketVersion", "未知")
                source_name = data.get("SourceName", "未知")
                greetings = data.get("Greetings", "")
                
                # 提取插件信息
                plugins = data.get("MarketPlugins", {})
                
                # 更新插件信息字典
                plugins_info = {}
                for plugin_id, plugin_info in plugins.items():
                    # 确保每个插件都有基本字段
                    plugin_data = {
                        "id": plugin_id,
                        "name": plugin_info.get("name", plugin_id),
                        "author": plugin_info.get("author", "未知"),
                        "version": plugin_info.get("version", "未知"),
                        "plugin_type": plugin_info.get("plugin-type", "classic"),
                        "description": plugin_info.get("description", ""),
                        "is_new": False,  # 默认不是新插件
                        "downloads": self.stats_manager.get_plugin_stats(plugin_id)["download_count"]
                    }
                    
                    # 检查是否是新插件
                    if plugin_id not in self.plugins:
                        plugin_data["is_new"] = True
                        if self.logger:
                            self.logger.info("插件", f"发现新插件: {plugin_data['name']} (ID: {plugin_id})")
                    
                    plugins_info[plugin_id] = plugin_data
                
                # 提取整合包信息
                packages = data.get("Packages", {})
                
                # 更新整合包信息字典
                packages_info = {}
                for package_id, package_info in packages.items():
                    # 确保每个整合包都有基本字段
                    packages_info[package_id] = {
                        "id": package_id,
                        "name": package_id,
                        "author": package_info.get("author", "未知"),
                        "version": package_info.get("version", "未知"),
                        "description": package_info.get("description", ""),
                        "plugin_ids": package_info.get("plugin-ids", []),
                        "plugin_count": len(package_info.get("plugin-ids", [])),
                        "downloads": self.stats_manager.get_package_downloads(package_id)
                    }
                
                # 更新状态
                with self.lock:
                    self.market_version = market_version
                    self.source_name = source_name
                    self.greetings = greetings
                    self.plugins = plugins_info
                    self.packages = packages_info
                    self.plugins_count = len(plugins_info)
                    self.packages_count = len(packages_info)
                    self.last_updated = datetime.datetime.now()
                
                if self.logger:
                    self.logger.succ("更新", 
                        f"插件数据已更新，版本: {market_version}, "
                        f"插件: {self.plugins_count}, 整合包: {self.packages_count}")
                
                # 对新插件进行分类和生成描述
                self._process_new_plugins()
            
            except json.JSONDecodeError as e:
                if self.logger:
                    self.logger.erro("解析", f"插件JSON数据解析失败: {str(e)}")
            except KeyError as e:
                if self.logger:
                    self.logger.erro("解析", f"JSON结构缺失关键字段: {str(e)}")
        
        except requests.exceptions.RequestException as e:
            if self.logger:
                self.logger.erro("网络", f"连接GitHub失败: {str(e)}")
        except Exception as e:
            if self.logger:
                self.logger.erro("异常", f"获取插件数据时发生意外错误: {str(e)}")
    
    def _process_new_plugins(self):
        """处理新插件：分类和生成描述"""
        new_plugins = []
        
        with self.lock:
            for plugin_id, plugin_info in self.plugins.items():
                # 只处理新插件
                if plugin_info["is_new"]:
                    new_plugins.append((plugin_id, plugin_info))
        
        if not new_plugins:
            if self.logger:
                self.logger.info("处理", "没有需要处理的新插件")
            return
        
        if self.logger:
            self.logger.info("处理", f"开始处理 {len(new_plugins)} 个新插件...")
        
        # 使用线程池并发处理请求
        threads = []
        for plugin_id, plugin_info in new_plugins:
            thread = threading.Thread(
                target=self._process_single_plugin,
                args=(plugin_id, plugin_info)
            )
            thread.start()
            threads.append(thread)
            
            # 控制并发量，避免触发API限制
            time.sleep(0)
        
        # 等待所有线程完成
        for thread in threads:
            thread.join()
        
        if self.logger:
            self.logger.succ("处理", f"已完成 {len(new_plugins)} 个新插件的处理")
    
    def _process_single_plugin(self, plugin_id: str, plugin_info: dict):
        """处理单个插件：分类和生成描述"""
        try:
            # 分类
            category = self._classify_plugin(plugin_id, plugin_info)
            
            # 生成描述
            description = self._generate_description(plugin_id, plugin_info)
            
            with self.lock:
                if plugin_id in self.plugins:
                    self.plugins[plugin_id]["category"] = category
                    self.plugins[plugin_id]["description"] = description
                    self.plugins[plugin_id]["is_new"] = False  # 标记为已处理
        except Exception as e:
            if self.logger:
                self.logger.erro("处理", f"插件 '{plugin_id}' 处理失败: {str(e)}")

class BanManager:
    """统一的封禁管理系统（整合IP和设备封禁）"""
    def __init__(self, workpath: str, logger: Logger = None):
        self.logger = logger
        self.workpath = workpath
        self.ban_db = os.path.join(workpath, "ban_manager.db")
        self.ip_ban_file = os.path.join(workpath, "ip_bans.json")
        self._init_db()
        self.lock = threading.RLock()
        self.load_ip_bans()
        
    def _init_db(self):
        """初始化数据库"""
        try:
            conn = sqlite3.connect(self.ban_db)
            c = conn.cursor()
            # 创建设备指纹表
            c.execute('''CREATE TABLE IF NOT EXISTS devices (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         fingerprint TEXT UNIQUE,
                         first_seen TEXT,
                         last_seen TEXT,
                         ip_history TEXT,
                         ban_status INTEGER DEFAULT 0,
                         ban_reason TEXT,
                         ban_expiry TEXT)''')
            
            # 创建IP封禁表
            c.execute('''CREATE TABLE IF NOT EXISTS ip_bans (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         ip TEXT UNIQUE,
                         timestamp TEXT,
                         reason TEXT,
                         duration INTEGER,
                         expires TEXT)''')
            conn.commit()
            conn.close()
        except Exception as e:
            if self.logger:
                self.logger.erro("数据", f"初始化封禁数据库失败: {str(e)}")
    
    def generate_fingerprint(self, request) -> str:
        """生成设备指纹"""
        try:
            # 收集设备特征
            features = [
                request.headers.get('User-Agent', ''),
                request.headers.get('Accept-Language', ''),
                request.headers.get('Accept-Encoding', ''),
                request.headers.get('Connection', ''),
                request.cookies.get('screen_res', ''),
                request.cookies.get('timezone', ''),
                request.cookies.get('plugins', ''),
                request.cookies.get('fonts', ''),
                request.headers.get('Sec-CH-UA', '')  # 新增客户端提示头
            ]
            
            # 生成指纹哈希
            return hashlib.sha256("|".join(features).encode()).hexdigest()
        except Exception as e:
            if self.logger:
                self.logger.erro("指纹", f"生成设备指纹失败: {str(e)}")
            return str(uuid.uuid4())
    
    def record_device(self, fingerprint: str, ip: str):
        """记录或更新设备信息"""
        try:
            conn = sqlite3.connect(self.ban_db)
            c = conn.cursor()
            
            # 检查设备是否已存在
            c.execute("SELECT * FROM devices WHERE fingerprint = ?", (fingerprint,))
            device = c.fetchone()
            
            current_time = datetime.datetime.now().isoformat()
            
            if device:
                # 更新现有设备
                ip_history = json.loads(device[4]) if device[4] else []
                if ip not in ip_history:
                    ip_history.append(ip)
                
                c.execute('''UPDATE devices 
                            SET last_seen = ?, ip_history = ?
                            WHERE fingerprint = ?''', 
                         (current_time, json.dumps(ip_history), fingerprint))
            else:
                # 插入新设备
                ip_history = [ip]
                c.execute('''INSERT INTO devices 
                            (fingerprint, first_seen, last_seen, ip_history) 
                            VALUES (?, ?, ?, ?)''', 
                         (fingerprint, current_time, current_time, json.dumps(ip_history)))
            
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            if self.logger:
                self.logger.erro("数据", f"记录设备信息失败: {str(e)}")
            return False
    
    def is_device_banned(self, fingerprint: str) -> bool:
        """检查设备是否被封禁"""
        try:
            conn = sqlite3.connect(self.ban_db)
            c = conn.cursor()
            c.execute("SELECT ban_status, ban_expiry FROM devices WHERE fingerprint = ?", (fingerprint,))
            device = c.fetchone()
            conn.close()
            
            if device:
                ban_status = device[0]
                ban_expiry = device[1]
                
                # 检查封禁是否过期
                if ban_status == 1 and ban_expiry:
                    expiry_time = datetime.datetime.fromisoformat(ban_expiry)
                    if expiry_time < datetime.datetime.now():
                        self.unban_device(fingerprint)
                        return False
                    return True
                return ban_status == 1
            return False
        except Exception as e:
            if self.logger:
                self.logger.erro("数据", f"检查设备封禁状态失败: {str(e)}")
            return False
    
    def ban_device(self, fingerprint: str, reason: str = "违规操作", duration: int = 0) -> bool:
        """封禁设备"""
        try:
            conn = sqlite3.connect(self.ban_db)
            c = conn.cursor()
            
            expiry = None
            if duration > 0:
                expiry = (datetime.datetime.now() + datetime.timedelta(seconds=duration)).isoformat()
            
            c.execute('''UPDATE devices 
                        SET ban_status = 1, ban_reason = ?, ban_expiry = ?
                        WHERE fingerprint = ?''', 
                     (reason, expiry, fingerprint))
            
            conn.commit()
            conn.close()
            
            if self.logger:
                self.logger.warn("封禁", f"已封禁设备: {fingerprint}, 原因: {reason}, 时长: {duration}秒")
            return True
        except Exception as e:
            if self.logger:
                self.logger.erro("数据", f"封禁设备失败: {str(e)}")
            return False
    
    def unban_device(self, fingerprint: str) -> bool:
        """解封设备"""
        try:
            conn = sqlite3.connect(self.ban_db)
            c = conn.cursor()
            
            c.execute('''UPDATE devices 
                        SET ban_status = 0, ban_reason = NULL, ban_expiry = NULL
                        WHERE fingerprint = ?''', 
                     (fingerprint,))
            
            conn.commit()
            conn.close()
            
            if self.logger:
                self.logger.succ("解封", f"已解封设备: {fingerprint}")
            return True
        except Exception as e:
            if self.logger:
                self.logger.erro("数据", f"解封设备失败: {str(e)}")
            return False
        
    def get_device_expiry(self, fingerprint: str) -> str:
        """获取设备的解封时间"""
        try:
            conn = sqlite3.connect(self.ban_db)
            c = conn.cursor()
            c.execute("SELECT ban_expiry FROM devices WHERE fingerprint = ?", (fingerprint,))
            expiry = c.fetchone()
            conn.close()
            if expiry and expiry[0]:
                expiry_datetime = datetime.datetime.fromisoformat(expiry[0])
                return expiry_datetime.strftime("%Y-%m-%d %H:%M:%S")
            return None
        except Exception as e:
            if self.logger:
                self.logger.erro("数据", f"获取设备解封时间失败: {str(e)}")
            return None

    def load_ip_bans(self):
        """加载IP封禁列表"""
        ip_bans = {}  # 初始化为空字典

        with self.lock:  # 使用锁确保线程安全
            ip_bans_file = self.ip_ban_file
            if os.path.exists(ip_bans_file):
                try:
                    with open(ip_bans_file, 'r') as f:
                        ip_bans = json.load(f)

                    #记录加载前的IP数量
                    original_count = len(ip_bans)
                    
                    # 清理过期的封禁
                    current_time = datetime.datetime.now()
                    expired_ips = []

                    for ip, ban_info in ip_bans.items():
                        if ban_info.get('duration', 0) > 0:
                            expires = ban_info.get('expires')
                            if expires:
                                try:
                                    expiry_time = datetime.datetime.fromisoformat(expires)
                                    if expiry_time < current_time:
                                        expired_ips.append(ip)
                                except ValueError:
                                    # matriz解析失败，也视为过期
                                    expired_ips.append(ip)

                    #从ip_bans中删除过期的IP
                    for ip in expired_ips:
                        del ip_bans[ip]

                    #记录并日志
                    cleared_count = len(expired_ips)
                    if cleared_count > 0:
                        if self.logger:
                            self.logger.info("清理", f"自动清理了{cleared_count}个过期封禁IP: {', '.join(expired_ips)}")
                    else:
                        if self.logger:
                            self.logger.info("清理", "没有过期的IP封禁需要清理")

                    # 保存更新后的IP封禁列表
                    try:
                        with open(ip_bans_file, 'w') as f:
                            json.dump(ip_bans, f, indent=2)
                        if self.logger:
                            self.logger.info("保存", f"已保存更新后的IP封禁列表，当前{len(ip_bans)}个封禁条目")
                    except Exception as e:
                        if self.logger:
                            self.logger.erro("保存", f"保存IP封禁列表失败: {str(e)}")
                        # 保持当前内存中的状态，尽管文件未保存
                except Exception as e:
                    if self.logger:
                        self.logger.erro("加载", f"加载IP封禁列表失败: {str(e)}")
                    ip_bans = {}  # 若加载失败，使用空字典
            else:
                if self.logger:
                    self.logger.info("加载", f"IP封禁文件{ip_bans_file}不存在，初始化为空")
        return ip_bans
    
    def is_ip_banned(self, ip: str) -> bool:
        """检查IP是否被封禁"""
        ip_bans = self.load_ip_bans()
        
        # 检查精确IP匹配
        if ip in ip_bans:
            return True
        
        # 检查IP范围
        for ban_ip in ip_bans:
            if '/' in ban_ip:  # CIDR范围
                try:
                    network = ipaddress.ip_network(ban_ip)
                    if ipaddress.ip_address(ip) in network:
                        return True
                except ValueError:
                    continue
        return False
    
    def ban_ip(self, ip: str, reason: str = "违规操作", duration: int = 0) -> bool:
        """封禁IP地址"""
        try:
            # 验证IP格式
            if '/' in ip:
                ipaddress.ip_network(ip)
            else:
                ipaddress.ip_address(ip)
        except ValueError:
            return False
        
        ip_bans = self.load_ip_bans()
        
        # 设置封禁信息
        ban_info = {
            "timestamp": datetime.datetime.now().isoformat(),
            "reason": reason,
            "duration": duration,  # 0表示永久封禁
            "expires": (datetime.datetime.now() + datetime.timedelta(seconds=duration)).isoformat() if duration > 0 else None
        }
        
        ip_bans[ip] = ban_info
        
        try:
            with open(self.ip_ban_file, 'w') as f:
                json.dump(ip_bans, f, indent=2)
        except Exception as e:
            if self.logger:
                self.logger.erro("保存", f"保存IP封禁列表失败: {str(e)}")
            return False
        
        if self.logger:
            self.logger.warn("封禁", f"已封禁IP: {ip}, 原因: {reason}, 时长: {duration}秒")
        return True
    
    def unban_ip(self, ip: str) -> bool:
        """解封IP地址"""
        ip_bans = self.load_ip_bans()
        if ip in ip_bans:
            del ip_bans[ip]
            try:
                with open(self.ip_ban_file, 'w') as f:
                    json.dump(ip_bans, f, indent=2)
            except Exception as e:
                if self.logger:
                    self.logger.erro("保存", f"保存IP封禁列表失败: {str(e)}")
                return False
            
            if self.logger:
                self.logger.succ("解封", f"已解封IP: {ip}")
            return True
        return False
    
    def get_ip_expiry(self, ip: str) -> str:
        """获取IP的解封时间"""
        try:
            ip_bans = self.load_ip_bans()
            if ip in ip_bans:
                expiry_datetime = datetime.datetime.fromisoformat(ip_bans[ip].get('expires'))
                return expiry_datetime.strftime("%Y-%m-%d %H:%M:%S")
            return None
        except Exception as e:
            if self.logger:
                self.logger.erro("数据", f"获取IP解封时间失败: {str(e)}")
            return None

    def reload_ip_bans(self):
        """重新加载IP封禁列表"""
        self.load_ip_bans()
        if self.logger:
            self.logger.info("重载", "IP封禁列表已重新加载")
    
    def list_ip_bans(self) -> dict:
        """获取IP封禁列表"""
        return self.load_ip_bans()
    
    def list_devices(self) -> list:
        """获取设备列表"""
        try:
            conn = sqlite3.connect(self.ban_db)
            c = conn.cursor()
            c.execute("SELECT fingerprint, first_seen, last_seen, ip_history, ban_status, ban_reason, ban_expiry FROM devices")
            devices = []
            
            for row in c.fetchall():
                devices.append({
                    "fingerprint": row[0],
                    "first_seen": row[1],
                    "last_seen": row[2],
                    "ip_history": json.loads(row[3]) if row[3] else [],
                    "ban_status": row[4],
                    "ban_reason": row[5],
                    "ban_expiry": row[6]
                })
            
            conn.close()
            return devices
        except Exception as e:
            if self.logger:
                self.logger.erro("数据", f"获取设备列表失败: {str(e)}")
            return []

class PluginMarketWeb:
    name: str = "插件市场"
    version: str = "v0.1 - Bate"
    workpath: str = os.getcwd()
    
    def __init__(self, web_address: tuple = ("127.0.0.1", 8111), api_address: tuple = ("127.0.0.1", 8112)) -> None:
        # 创建主日志记录器
        self.logger: Logger = Logger(4096, 3000)
        self.logger.succ("启动", f"初始化插件市场服务: {self.name} {self.version}")
        
        self.web_addr: tuple = web_address
        self.api_addr: tuple = api_address
       
        # 初始化仓库管理器，设置更新间隔为每小时，清理未更新30天的仓库
        git_manager = GitRepositoryManager(
            logger=self.logger,
            update_interval=60,  # 每分钟更新一次
            cleanup_days=30
        )
        public_clone_path = os.path.join(self.workpath, "public-repo")
        git_manager.add_repository(
            repo_id="PluginMarket",
            repo_url="https://github.com/ToolDelta-Basic/PluginMarket.git",
            clone_path=public_clone_path,
            update_interval=15  # 每15分钟更新一次
        )

        # 创建统计管理器
        self.stats_manager = StatsManager(
            workpath=self.workpath,
            logger=self.logger
        )

        self.plugin_monitor = GitHubPluginMonitor(
            github_url="https://raw.githubusercontent.com/ToolDelta-Basic/PluginMarket/main/market_tree.json",
            deepseek_api_key="sk-67430e0fccad41248ecdf071784d4c18",  # 替换为你的实际 API 密钥
            update_interval=600,  # 10分钟更新一次
            logger=self.logger,
            stats_manager=self.stats_manager
        )
        self.plugin_monitor.start()

        # 创建统一的封禁管理器
        self.ban_manager = BanManager(
            workpath=self.workpath,
            logger=self.logger
        )
        
        # 创建跨进程日志队列
        self.log_queue = multiprocessing.Queue(-1)
        self.processes = {}
        self.restart_attempts = {"Web": 0, "Api": 0}
        self.max_restarts = 5
        self.restart_delays = {"Web": 1, "Api": 1}
        
        # 启动日志监听线程
        self._start_log_listener()
        
        # 注册信号处理器
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGHUP, self._signal_handler)
        
        # 创建并启动服务进程
        self._create_processes()
        
        # 启动进程监控
        self._monitor_processes()

    def __del__(self):
        """析构函数确保资源释放"""
        if hasattr(self, 'processes'):
            self._shutdown()

    def _create_processes(self):
        """创建服务进程"""
        self.logger.info("启动", "正在创建服务进程...")
        self.processes = {
            "Web": multiprocessing.Process(
                target=self._run_service,
                name="Web服务进程",
                args=("Web", self._create_web_app, self.web_addr),
                daemon=True
            ),
            "Api": multiprocessing.Process(
                target=self._run_service,
                name="API服务进程",
                args=("Api", self._create_api_app, self.api_addr),
                daemon=True
            ),
        }
        
        for name, process in self.processes.items():
            process.start()
            self.logger.succ("启动", f"[{name}] 服务进程已启动 (PID: {process.pid})")

    def _signal_handler(self, signum, frame):
        """统一信号处理"""
        signals = {
            signal.SIGINT: "SIGINT",
            signal.SIGTERM: "SIGTERM",
            signal.SIGHUP: "SIGHUP"
        }
        self.logger.succ("停止", f"收到终止信号({signals.get(signum, signum)})，正在关闭服务...")
        self._shutdown()

    def _shutdown(self):
        """安全关闭所有服务"""
        # 发送终止信号到日志队列
        try:
            self.log_queue.put(None, timeout=1)
        except Full:
            pass
        
        # 终止所有进程
        for name, process in self.processes.items():
            if process.is_alive():
                self.logger.info("停止", f"终止[{name}] 服务 (PID: {process.pid})...")
                process.terminate()
                
                # 等待进程结束
                process.join(timeout=3)
                if process.is_alive():
                    self.logger.warn("强制", f"强制终止[{name}] 服务 (PID: {process.pid})")
                    process.kill()
        
        # 确保日志队列清空
        self._drain_log_queue()
        self.logger.succ("停止", "服务已安全关闭")
        ThreadEvent.set()

    def _drain_log_queue(self):
        """清空日志队列"""
        while not self.log_queue.empty():
            try:
                self.log_queue.get_nowait()
            except:
                break

    def _start_log_listener(self):
        """启动日志监听线程"""
        def listener():
            self.logger.info("日志", "日志监听线程已启动")
            while True:
                try:
                    record = self.log_queue.get()
                    if record is None:  # 终止信号
                        self.logger.info("日志", "日志监听线程收到终止信号")
                        break
                    
                    level, title, message = record
                    log_methods = {
                        "INFO": self.logger.info,
                        "WARN": self.logger.warn,
                        "ERRO": self.logger.erro,
                        "SUCC": self.logger.succ,
                        "FAIL": self.logger.fail,
                        "DEBUG": self.logger.info,
                    }
                    
                    log_method = log_methods.get(level, self.logger.info)
                    try:
                        log_method(title=title, message=message)
                    except OutPutQueueFullError:
                        # 如果日志队列满，简单打印到控制台
                        with IOLock:
                            sys.stdout.write(f"[{level}] {title}: {message}\n")
                            sys.stdout.flush()
                    except Exception as e:
                        with IOLock:
                            sys.stdout.write(f"日志处理错误: {str(e)}\n")
                            sys.stdout.flush()
                except (KeyboardInterrupt, SystemExit):
                    break
                except Exception as e:
                    with IOLock:
                        sys.stdout.write(f"日志监听错误: {str(e)}\n")
                        sys.stdout.flush()
        
        self.listener_thread = threading.Thread(
            target=listener,
            name="日志监听线程",
            daemon=True
        )
        self.listener_thread.start()
    
    def _create_base_app(self, service_name: str):
        """创建基础Flask应用"""
        return Flask(
            import_name=f"PluginMarket-{service_name}",
            static_folder=f"{self.workpath}/src/static", 
            template_folder=f"{self.workpath}/src/template"
        )

    def _create_web_app(self, log_func):
        """创建Web应用"""
        app = self._create_base_app("Web")
        CORS(app)
        # 在请求开始时存储请求信息
        @app.before_request
        def store_request_info():
            ip = request.remote_addr
            g.request_info = {
                'start_time': time.time(),
                'method': request.method,
                'path': request.path,
                'remote_addr': ip
            }
            
            # 检查IP是否被封禁
            if self.ban_manager.is_ip_banned(ip):
                log_func("WARN", "封禁", 
                         f"来自被封禁IP的访问: {ip} 尝试访问 {request.path}")
                return render_template(
                    'event', 
                    title="访问被拒绝",
                    error_code=403, 
                    error_msg=f"您的IP地址已被封禁，解封时间:{self.ban_manager.get_ip_expiry(ip)}"
                ), 403
            
            # 生成设备指纹
            fingerprint = self.ban_manager.generate_fingerprint(request)
            g.fingerprint = fingerprint
            
            # 检查设备是否被封禁
            if self.ban_manager.is_device_banned(fingerprint):
                log_func("WARN", "封禁", 
                         f"来自被封禁设备的访问: {fingerprint} (IP: {ip}) 尝试访问 {request.path}")
                return render_template(
                    'event', 
                    title="访问被拒绝",
                    error_code=403, 
                    error_msg=f"您的设备已被封禁，解封时间:{self.ban_manager.get_device_expiry(fingerprint)}"
                ), 403
            
            # 记录设备信息
            self.ban_manager.record_device(fingerprint, ip)
            
            # 设置指纹Cookie
            response = make_response()
            response.set_cookie('device_fingerprint', fingerprint, max_age=365*24*3600, httponly=True, samesite='Strict')
            # return response
        
        # 在请求结束后记录完整的日志（包含状态码）
        @app.after_request
        def log_request_info(response):
            # 从全局g对象获取请求信息
            request_info = getattr(g, 'request_info', {})
            if not request_info:
                return response
                
            # 计算请求耗时
            elapsed = time.time() - request_info.get('start_time', time.time())
            elapsed_str = f"{elapsed:.3f}s"
            
            # 获取响应状态码
            status_code = response.status_code
            status_color = colorama.Fore.GREEN
            if status_code >= 400:
                status_color = colorama.Fore.RED
            elif status_code >= 300:
                status_color = colorama.Fore.YELLOW
            
            # 记录完整日志（包含状态码和耗时）
            log_func("INFO", "请求", 
                f"{colorama.Fore.CYAN}{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
                f"{request_info.get('method', ''):>6} | "
                f"{request_info.get('remote_addr', ''):15} | "
                f"{request_info.get('path', '')} | "
                f"{status_color}{status_code} | "
                f"{elapsed_str}{colorama.Style.RESET_ALL}")
            
            return response
        
        @app.route('/')
        def home():
            plugins_count = self.plugin_monitor.get_plugins_count()
            packages_count = self.plugin_monitor.get_packages_count()
            return render_template('index.html', title=self.name, version=self.version, plugin_count=plugins_count, packages_count=packages_count)
        
        @app.route('/plugin')
        def plugin():
            # 获取插件市场数据
            market_version = self.plugin_monitor.get_market_version()
            source_name = self.plugin_monitor.get_source_name()
            greetings = self.plugin_monitor.get_greetings()
            plugins_count = self.plugin_monitor.get_plugins_count()
            packages_count = self.plugin_monitor.get_packages_count()
            last_updated = self.plugin_monitor.get_last_updated()

            # 获取所有插件信息（转换为列表）
            plugins = list(self.plugin_monitor.get_plugins().values())
            
            # 获取所有整合包信息（转换为列表）
            packages = list(self.plugin_monitor.get_packages().values())
            
            return render_template(
                'plugin.html',
                market_version=market_version,
                source_name=source_name,
                greetings=greetings,
                plugins=plugins,
                packages=packages,
                plugins_count=plugins_count,
                packages_count=packages_count,
                last_updated=last_updated,
            )

        @app.route('/error')
        def error():
            code = request.args.get('code')
            message = request.args.get('message')
            if message is None:
                message = ""
            return render_template(
                'event', 
                title="PluginMarket - Web Error",
                error_code=code, 
                error_msg=message
            ), code

        @app.errorhandler(Exception)
        def handle_errors(error):
            error_code = getattr(error, 'code', 500)
            error_msg = getattr(error, 'description', '服务器内部错误')
            
            # 使用自定义日志记录
            log_func("ERRO", "错误", f"Web服务 {error_code}: {error_msg}")
            
            return render_template(
                'event', 
                title="PluginMarket - Web Error",
                error_code=error_code, 
                error_msg=error_msg
            ), error_code
        
        return app
    
    def _create_api_app(self, log_func):
        """创建API应用"""
        app = self._create_base_app("Api")
        CORS(app)
        sockets = NSockets(app)  # 添加WebSocket支持
        self.clients = {}
        # 在请求开始时存储请求信息
        @app.before_request
        def store_request_info():
            ip = request.remote_addr
            g.request_info = {
                'start_time': time.time(),
                'method': request.method,
                'path': request.path,
                'remote_addr': ip
            }
            
            # 检查IP是否被封禁（除封禁管理API外）
            if not request.path.startswith('/api/admin/ipban') and not request.path.startswith('/api/admin/device'):
                if self.ban_manager.is_ip_banned(ip):
                    log_func("WARN", "封禁", 
                             f"来自被封禁IP的API访问: {ip} 尝试访问 {request.path}")
                    return jsonify({
                        "status": "error",
                        "code": 403,
                        "message": f"您的设备已被封禁，解封时间:{self.ban_manager.get_ip_expiry(ip)}"
                    }), 403
            
            # 生成设备指纹
            fingerprint = self.ban_manager.generate_fingerprint(request)
            g.fingerprint = fingerprint
            
            # 检查设备是否被封禁
            if self.ban_manager.is_device_banned(fingerprint):
                log_func("WARN", "封禁", 
                         f"来自被封禁设备的API访问: {fingerprint} (IP: {ip}) 尝试访问 {request.path}")
                return jsonify({
                    "status": "error",
                    "code": 403,
                    "message": f"您的设备已被封禁，解封时间:{self.ban_manager.get_device_expiry(fingerprint)}"
                }), 403
            
            # 记录设备信息
            self.ban_manager.record_device(fingerprint, ip)
            
            # 设置指纹Cookie
            response = make_response()
            response.set_cookie('device_fingerprint', fingerprint, max_age=365*24*3600, httponly=True, samesite='Strict')
            # return response
        
        # 在请求结束后记录完整的日志（包含状态码）
        @app.after_request
        def log_request_info(response):
            # 从全局g对象获取请求信息
            request_info = getattr(g, 'request_info', {})
            if not request_info:
                return response
                
            # 计算请求耗时
            elapsed = time.time() - request_info.get('start_time', time.time())
            elapsed_str = f"{elapsed:.3f}s"
            
            # 获取响应状态码
            status_code = response.status_code
            status_color = colorama.Fore.GREEN
            if status_code >= 400:
                status_color = colorama.Fore.RED
            elif status_code >= 300:
                status_color = colorama.Fore.YELLOW
            
            # 记录完整日志（包含状态码和耗时）
            log_func("INFO", "请求", 
                f"{colorama.Fore.CYAN}{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
                f"{request_info.get('method', ''):>6} | "
                f"{request_info.get('remote_addr', ''):15} | "
                f"{request_info.get('path', '')} | "
                f"{status_color}{status_code} | "
                f"{elapsed_str}{colorama.Style.RESET_ALL}")
            
            return response
        class WebSocketManager:
            """管理WebSocket连接和消息发送"""
            def __init__(self, log_func):
                self.clients = {}
                self.log_func = log_func
                self.lock = threading.RLock()
            
            def add_client(self, ws_client, addr, user_id):
                """添加新的WebSocket客户端，使用客户端提供的user_id"""
                # 验证ID格式（6位大写字母）
                if len(user_id) != 6 or not user_id.isalpha() or not user_id.isupper():
                    self.log_func("WARN", "通信", 
                                f"{addr} | 无效ID格式: {user_id}")
                    return None, "ID格式错误，需要6位大写字母"
                
                with self.lock:
                    if user_id in self.clients:
                        self.log_func("WARN", "通信", 
                                    f"{addr} | ID已被使用: {user_id}")
                        return None, "ID已被使用"
                    
                    self.clients[user_id] = {
                        "ws": ws_client,
                        "addr": addr,
                        "last_active": time.time(),
                        "created": time.time()
                    }
                
                self.log_func("INFO", "通信", 
                            f"{addr} | {user_id} | 新的客户端接入")
                return user_id, None
            
            def remove_client(self, client_id):
                """移除客户端"""
                with self.lock:
                    if client_id in self.clients:
                        addr = self.clients[client_id]["addr"]
                        del self.clients[client_id]
                        self.log_func("INFO", "通信", 
                                    f"{addr} | {client_id} | 客户端断开")
                        return True
                return False
            
            def send_message(self, client_id, message):
                """向特定客户端发送消息"""
                with self.lock:
                    client = self.clients.get(client_id)
                    if not client:
                        return False, "客户端不存在"
                    
                    try:
                        client["ws"].send(message)
                        client["last_active"] = time.time()
                        self.log_func("DEBUG", "通信", 
                                    f"{client['addr']} | {client_id} | 发送消息: {message[:100]}")
                        return True, "消息发送成功"
                    except Exception as e:
                        return False, f"发送失败: {str(e)}"
            
            def broadcast(self, message, exclude=None):
                """广播消息给所有客户端"""
                exclude = exclude or []
                results = []
                with self.lock:
                    for client_id, client in list(self.clients.items()):
                        if client_id in exclude:
                            continue
                        try:
                            client["ws"].send(message)
                            client["last_active"] = time.time()
                            results.append((client_id, True, "发送成功"))
                            self.log_func("DEBUG", "通信", 
                                        f"{client['addr']} | {client_id} | 广播消息: {message[:100]}")
                        except Exception as e:
                            results.append((client_id, False, f"发送失败: {str(e)}"))
                return results
            
            def get_client_info(self, client_id):
                """获取客户端信息"""
                with self.lock:
                    client = self.clients.get(client_id)
                    if client:
                        return {
                            "client_id": client_id,
                            "address": client["addr"],
                            "created": datetime.datetime.fromtimestamp(client["created"]).isoformat(),
                            "last_active": datetime.datetime.fromtimestamp(client["last_active"]).isoformat(),
                            "duration": int(time.time() - client["created"])
                        }
                return None
            
            def list_clients(self):
                """列出所有客户端信息"""
                with self.lock:
                    return [
                        self.get_client_info(client_id)
                        for client_id in self.clients.keys()
                    ]
            
            def _generate_short_id(self):
                """生成随机的6位字符串ID"""
                return ''.join(random.choices(string.ascii_letters + string.digits, k=6)).upper()

            def get_client_id_by_fingerprint(self, fingerprint):
                """根据设备指纹获取客户端ID"""
                with self.lock:
                    for client_id, client_info in self.clients.items():
                        # 这里假设我们在客户端信息中存储了设备指纹
                        if client_info.get('fingerprint') == fingerprint:
                            return client_id
                return None

        # 在_create_api_app函数内初始化WebSocket管理器
        ws_manager = WebSocketManager(log_func)

        @app.route('/api/plugins', methods=['POST', 'GET', 'OPTIONS'])
        def api_plugins():
            """获取所有插件信息"""
            return jsonify({
                "status": "success",
                "market_version": self.plugin_monitor.get_market_version(),
                "source_name": self.plugin_monitor.get_source_name(),
                "greetings": self.plugin_monitor.get_greetings(),
                "plugins_count": self.plugin_monitor.get_plugins_count(),
                "packages_count": self.plugin_monitor.get_packages_count(),
                "last_updated": self.plugin_monitor.get_last_updated(),
                "plugins": self.plugin_monitor.get_plugins()
            })

        @app.route('/api/packages', methods=['POST', 'GET', 'OPTIONS'])
        def api_packages():
            """获取所有整合包信息"""
            return jsonify({
                "status": "success",
                "market_version": self.plugin_monitor.get_market_version(),
                "source_name": self.plugin_monitor.get_source_name(),
                "packages_count": self.plugin_monitor.get_packages_count(),
                "last_updated": self.plugin_monitor.get_last_updated(),
                "packages": self.plugin_monitor.get_packages()
            })
        
        @app.route('/api/packages/<package_id>', methods=['POST', 'GET', 'OPTIONS'])
        def api_package_detail(package_id):
            """获取特定整合包的详细信息"""
            packages = self.plugin_monitor.get_packages()
            package = packages.get(package_id)
            
            if package:
                # 获取整合包中所有插件的详细信息
                plugins = self.plugin_monitor.get_plugins()
                package_plugins = {}
                for plugin_id in package.get("plugin_ids", []):
                    if plugin_id in plugins:
                        package_plugins[plugin_id] = plugins[plugin_id]
                
                return jsonify({
                    "status": "success",
                    "package": package,
                    "plugins": package_plugins
                })
            else:
                return jsonify({
                    "status": "error",
                    "message": f"未找到ID为 {package_id} 的整合包"
                }), 404

        @app.route('/api/status', methods=['POST', 'GET', 'OPTIONS'])
        def api_status():
            return jsonify({
                "status": "Running",
                "name": self.name,
                "version": self.version,
                "services": {
                    "Web": f"http://{self.web_addr[0]}:{self.web_addr[1]}",
                    "Api": f"http://{self.api_addr[0]}:{self.api_addr[1]}"
                }
            })
        
        def push_file_to_client(user_id, file_id, file_data):
            """同步方式推送文件给客户端"""
            if user_id not in ws_manager.clients:
                log_func("ERRO", "文件传输", f"用户ID {user_id} 未连接")
                return False
                
            client = ws_manager.clients[user_id]
            ws_client = client["ws"]
            
            try:
                # 1. 发送文件开始信号
                start_msg = json.dumps({
                    "type": "file_start",
                    "file_id": file_id,
                    "size": len(file_data),
                    "chunks": (len(file_data) // 4096) + 1
                })
                ws_client.send(start_msg)
                
                # 2. 分块发送文件数据
                chunk_size = 4096
                for i in range(0, len(file_data), chunk_size):
                    chunk = file_data[i:i+chunk_size]
                    ws_client.send(chunk)
                    
                # 3. 发送文件结束信号
                end_msg = json.dumps({
                    "type": "file_end",
                    "file_id": file_id
                })
                ws_client.send(end_msg)
                
                log_func("INFO", "传输", f"成功向 {user_id} 推送文件 {file_id}")
                return True
                
            except Exception as e:
                log_func("ERRO", "传输", f"向 {user_id} 推送文件失败: {str(e)}")
                return False
            
        # 在 api_plugin_download 路由中使用修复后的函数
        @app.route('/api/plugin/download/<plugin_id>', methods=['POST'])
        def api_plugin_download(plugin_id):
            """记录插件下载并推送文件"""
            try:
                data = request.get_json()
                user_id = data.get('user_id')
                
                # 验证用户ID
                if not user_id or len(user_id) != 6 or not user_id.isalpha() or not user_id.isupper():
                    return jsonify({
                        "status": "error",
                        "message": "无效的用户ID格式"
                    }), 400
                
                if user_id not in ws_manager.clients:
                    return jsonify({
                        "status": "error",
                        "message": "未被注册的ID"
                    }), 400

                if plugin_id not in self.plugin_monitor.get_plugins():
                    return jsonify({
                        "status": "error",
                        "message": f"未找到插件 {plugin_id}"
                    }), 404


                plugins = self.plugin_monitor.get_plugins()
                plugin = plugins.get(plugin_id)
                # 查找插件文件夹
                plugin_folder = os.path.join(self.workpath, 'public-repo', plugin["name"])
                if not os.path.exists(plugin_folder):
                    return jsonify({"status": "error", "message": "插件文件夹不存在"}), 500

                # 压缩文件夹
                zip_path = shutil.make_archive(plugin_id, 'zip', plugin_folder)
                with open(zip_path, 'rb') as f:
                    file_data = f.read()
                os.remove(zip_path)

                # 通过WebSocket发送文件给客户端
                if not push_file_to_client(user_id, plugin_id, file_data):
                    return jsonify({"status": "error", "message": "文件推送失败"}), 500

                self.stats_manager.record_plugin_download(plugin_id, user_id)
                return jsonify({"status": "success", "message": "插件下载成功"}), 200
            except Exception as e:
                return jsonify({"status": "error", "message": f"记录下载失败: {str(e)}"}), 500
        
        # 修改整合包下载API - 同样使用user_id
        @app.route('/api/package/download/<package_id>', methods=['POST'])
        def api_package_download(package_id):
            """记录整合包下载"""
            try:
                # 获取请求中的用户ID
                data = request.get_json()
                user_id = data.get('user_id')  # 改为使用user_id
                
                # 验证用户ID格式
                if not user_id or len(user_id) != 6 or not user_id.isalpha() or not user_id.isupper():
                    return jsonify({
                        "status": "error",
                        "message": "无效的用户ID格式，需要6位大写字母"
                    }), 400
                if user_id not in ws_manager.clients:
                    return jsonify({
                        "status": "error",
                        "message": "未被注册的ID，请前往ToolDelta确认ID是否正确"
                    }), 400
                if package_id in self.plugin_monitor.get_packages():
                    # 传递用户ID给统计管理器
                    self.stats_manager.record_package_download(package_id, user_id)
                    return jsonify({
                        "status": "success",
                        "message": f"整合包 {package_id} 下载记录成功"
                    })
                else:
                    return jsonify({
                        "status": "error",
                        "message": f"未找到ID为 {package_id} 的整合包"
                    }), 404
            except Exception as e:
                return jsonify({
                    "status": "error",
                    "message": f"记录下载失败: {str(e)}"
                }), 500

        
        @app.route('/api/plugin/stats/<plugin_id>', methods=['GET'])
        def api_plugin_stats(plugin_id):
            """获取插件统计信息（带安全默认值）"""
            if plugin_id in self.plugin_monitor.get_plugins():
                stats = self.stats_manager.get_plugin_stats(plugin_id)
                # 确保返回的数据结构与前端期望一致
                return jsonify({
                    "status": "success",
                    "stats": {
                        "downloads": stats.get("download_count", 0),
                        "avg_rating": stats.get("avg_rating", 0.0),
                        "rating_count": stats.get("rating_count", 0)
                    }
                })
            else:
                # 插件不存在时返回默认值
                return jsonify({
                    "status": "success",
                    "stats": {
                        "downloads": 0,
                        "avg_rating": 0.0,
                        "rating_count": 0
                    }
                })

        @app.route('/api/plugin/rate/<plugin_id>', methods=['POST'])
        def api_plugin_rate(plugin_id):
            """记录插件评分（带安全返回）"""
            data = request.get_json()
            rating = data.get('rating')
            
            try:
                rating = int(rating)
                if rating < 1 or rating > 5:
                    return jsonify({
                        "status": "error",
                        "message": "评分必须在1-5之间",
                        "new_avg_rating": 0.0
                    }), 400
                
                # 检查插件是否存在
                if plugin_id not in self.plugin_monitor.get_plugins():
                    return jsonify({
                        "status": "error",
                        "message": f"未找到ID为 {plugin_id} 的插件",
                        "new_avg_rating": 0.0
                    }), 404
                
                # 记录评分
                self.stats_manager.record_plugin_rating(plugin_id, rating)
                
                # 获取更新后的统计信息
                stats = self.stats_manager.get_plugin_stats(plugin_id)
                new_avg_rating = stats.get("avg_rating", 0.0)
                
                return jsonify({
                    "status": "success",
                    "message": f"插件 {plugin_id} 评分记录成功",
                    "new_avg_rating": new_avg_rating
                })
            except ValueError:
                return jsonify({
                    "status": "error",
                    "message": "无效的评分格式",
                    "new_avg_rating": 0.0
                }), 400
            except Exception as e:
                return jsonify({
                    "status": "error",
                    "message": f"记录评分失败: {str(e)}",
                    "new_avg_rating": 0.0
                }), 500

        # 添加周下载量API
        @app.route('/api/stats/weekly_downloads', methods=['GET'])
        def api_weekly_downloads():
            """获取周下载量统计"""
            try:
                weekly_downloads = self.stats_manager.get_weekly_downloads()
                return jsonify({
                    "status": "success",
                    "weekly_downloads": weekly_downloads
                })
            except Exception as e:
                return jsonify({
                    "status": "error",
                    "message": f"获取周下载量失败: {str(e)}",
                    "weekly_downloads": 0
                }), 500

        # 修改总下载量API名称（从api_weekly_downloads改为api_total_downloads）
        @app.route('/api/stats/total_downloads', methods=['GET'])
        def api_total_downloads():
            """获取所有插件总下载量"""
            try:
                total_downloads = self.stats_manager.get_total_plugin_downloads()
                return jsonify({
                    "status": "success",
                    "total_downloads": total_downloads
                })
            except Exception as e:
                return jsonify({
                    "status": "error",
                    "message": f"获取总下载量失败: {str(e)}",
                    "total_downloads": 0
                }), 500

        
        @app.route('/api/package/downloads/<package_id>', methods=['GET'])
        def api_package_downloads(package_id):
            """获取整合包下载次数"""
            if package_id in self.plugin_monitor.get_packages():
                downloads = self.stats_manager.get_package_downloads(package_id)
                return jsonify({
                    "status": "success",
                    "downloads": downloads
                })
            else:
                return jsonify({
                    "status": "error",
                    "message": f"未找到ID为 {package_id} 的整合包"
                }), 404
        
        # 添加通过API发送消息的功能（使用client_id）
        @app.route('/api/socket/send', methods=['POST'])
        def api_socket_send():
            """通过API向WebSocket客户端发送消息"""
            data = request.get_json()
            client_id = data.get('client_id')
            message = data.get('message')
            
            if not client_id or not message:
                return jsonify({
                    "status": "error",
                    "message": "缺少client_id或message参数"
                }), 400
            
            success, result_msg = ws_manager.send_message(client_id, json.dumps({
                "type": "api_message",
                "from": "server_api",
                "timestamp": datetime.datetime.now().isoformat(),
                "content": message
            }))
            
            if success:
                return jsonify({
                    "status": "success",
                    "message": result_msg
                })
            else:
                return jsonify({
                    "status": "error",
                    "message": result_msg
                }), 400

        # 添加广播消息功能
        @app.route('/api/socket/broadcast', methods=['POST'])
        def api_socket_broadcast():
            """向所有WebSocket客户端广播消息"""
            data = request.get_json()
            message = data.get('message')
            
            if not message:
                return jsonify({
                    "status": "error",
                    "message": "缺少message参数"
                }), 400
            
            results = ws_manager.broadcast(json.dumps({
                "type": "broadcast",
                "from": "server",
                "timestamp": datetime.datetime.now().isoformat(),
                "content": message
            }))
            
            success_count = sum(1 for r in results if r[1])
            return jsonify({
                "status": "success",
                "message": f"已向{len(results)}个客户端发送广播",
                "success_count": success_count,
                "failed_count": len(results) - success_count,
                "results": [{
                    "client_id": r[0],
                    "success": r[1],
                    "message": r[2]
                } for r in results]
            })

        # 在API路由中添加新的端点
        @app.route('/api/socket/client_id', methods=['GET'])
        def api_get_client_id():
            """获取当前设备的WebSocket客户端ID"""
            try:
                # 从Cookie获取设备指纹
                fingerprint = request.cookies.get('device_fingerprint')
                if not fingerprint:
                    return jsonify({
                        "status": "error",
                        "message": "无法获取设备指纹"
                    }), 400
                
                # 通过指纹获取客户端ID
                client_id = ws_manager.get_client_id_by_fingerprint(fingerprint)
                
                if client_id:
                    return jsonify({
                        "status": "success",
                        "client_id": client_id
                    })
                else:
                    return jsonify({
                        "status": "error",
                        "message": "未找到活跃的WebSocket连接"
                    }), 404
                    
            except Exception as e:
                return jsonify({
                    "status": "error",
                    "message": f"获取客户端ID失败: {str(e)}"
                }), 500


        # 添加获取客户端信息功能
        @app.route('/api/socket/client/<client_id>', methods=['GET'])
        def api_socket_client_info(client_id):
            """获取特定WebSocket客户端信息"""
            client_info = ws_manager.get_client_info(client_id)
            if client_info:
                return jsonify({
                    "status": "success",
                    "client": client_info
                })
            else:
                return jsonify({
                    "status": "error",
                    "message": "客户端不存在或已断开"
                }), 404

        @app.route('/api/open_api', methods=['GET'])
        def get_routes():
            routes = []
            for rule in app.url_map.iter_rules():
                routes.append({
                    'endpoint': rule.endpoint,
                    'url': rule.rule,
                    'methods': [method for method in rule.methods]
                })
            return render_template("open_api.html", routes=routes)

        # IP封禁管理API
        @app.route('/api/admin/ipban/list', methods=['GET'])
        def api_ipban_list():
            """获取封禁IP列表"""
            # 这里应添加身份验证
            bans = self.ban_manager.list_ip_bans()
            return jsonify({
                "status": "success",
                "banned_ips": bans
            })
        
        @app.route('/api/admin/ipban/ban', methods=['POST', 'GET'])
        def api_ipban_add():
            """封禁IP地址"""
            # 这里应添加身份验证
            data = request.get_json()
            ip = data.get('ip')
            reason = data.get('reason', '违规操作')
            duration = data.get('duration', 0)  # 0表示永久封禁
            
            if not ip:
                return jsonify({
                    "status": "error",
                    "message": "缺少IP参数"
                }), 400
            
            if self.ban_manager.ban_ip(ip, reason, duration):
                return jsonify({
                    "status": "success",
                    "message": f"IP {ip} 已封禁"
                })
            else:
                return jsonify({
                    "status": "error",
                    "message": f"无效的IP地址: {ip}"
                }), 400
        
        @app.route('/api/admin/ipban/unban', methods=['POST', 'GET'])
        def api_ipban_remove():
            """解封IP地址"""
            # 这里应添加身份验证
            data = request.get_json()
            ip = data.get('ip')
            
            if not ip:
                return jsonify({
                    "status": "error",
                    "message": "缺少IP参数"
                }), 400
            
            if self.ban_manager.unban_ip(ip):
                return jsonify({
                    "status": "success",
                    "message": f"IP {ip} 已解封"
                })
            else:
                return jsonify({
                    "status": "error",
                    "message": f"IP {ip} 不在封禁列表中"
                }), 404
        
        @app.route('/api/admin/ipban/reload', methods=['POST', 'GET'])
        def api_ipban_reload():
            """重新加载IP封禁列表"""
            # 这里应添加身份验证
            self.ban_manager.reload_ip_bans()
            return jsonify({
                "status": "success",
                "message": "IP封禁列表已重新加载"
            })
        
        # 设备封禁管理API
        @app.route('/api/admin/device/list', methods=['GET'])
        def api_device_list():
            """获取设备列表"""
            # 这里应添加身份验证
            devices = self.ban_manager.list_devices()
            return jsonify({
                "status": "success",
                "devices": devices
            })
        
        @app.route('/api/admin/device/ban', methods=['POST', 'GET'])
        def api_device_ban():
            """封禁设备"""
            # 这里应添加身份验证
            data = request.get_json()
            fingerprint = data.get('fingerprint')
            reason = data.get('reason', '违规操作')
            duration = data.get('duration', 0)  # 0表示永久封禁
            
            if not fingerprint:
                return jsonify({
                    "status": "error",
                    "message": "缺少设备指纹参数"
                }), 400
            
            if self.ban_manager.ban_device(fingerprint, reason, duration):
                return jsonify({
                    "status": "success",
                    "message": f"设备 {fingerprint} 已封禁"
                })
            else:
                return jsonify({
                    "status": "error",
                    "message": "封禁设备失败"
                }), 500
        
        @app.route('/api/admin/device/unban', methods=['POST', 'GET'])
        def api_device_unban():
            """解封设备"""
            # 这里应添加身份验证
            data = request.get_json()
            fingerprint = data.get('fingerprint')
            
            if not fingerprint:
                return jsonify({
                    "status": "error",
                    "message": "缺少设备指纹参数"
                }), 400
            
            if self.ban_manager.unban_device(fingerprint):
                return jsonify({
                    "status": "success",
                    "message": f"设备 {fingerprint} 已解封"
                })
            else:
                return jsonify({
                    "status": "error",
                    "message": "解封设备失败"
                }), 500

        def generate_short_id():
            """生成一个随机的6位字符串ID"""
            return ''.join(random.choices(string.ascii_letters + string.digits, k=6)).upper()

        @app.errorhandler(404)
        def api_not_found(error):
            return jsonify({
                "status": "error",
                "code": 404,
                "message": "API endpoint not found"
            }), 404
        
        @app.errorhandler(500)
        def api_internal_error(error):
            return jsonify({
                "status": "error",
                "code": 500,
                "message": "Internal server error"
            }), 500
        
        @app.errorhandler(Exception)
        def handle_errors(error):
            error_code = getattr(error, 'code', 500)
            error_msg = getattr(error, 'description', '服务器内部错误')
            
            # 使用自定义日志记录
            log_func("ERRO", "错误", f"API服务 {error_code}: {error_msg}")
            
            return jsonify({
                "status": "error",
                "code": error_code,
                "message": error_msg
            }), error_code
    

        # 修改WebSocket连接处理函数
        @sockets.route('/api/socket')
        def api_socket(ws_client):
            """WebSocket端点"""
            addr = f"{ws_client.environ['REMOTE_ADDR']}:{ws_client.environ['REMOTE_PORT']}"
            
            # 获取设备指纹
            try:
                # 从Cookie获取设备指纹
                fingerprint = ws_client.environ.get('HTTP_COOKIE', '')
                if fingerprint:
                    # 解析Cookie
                    cookies = SimpleCookie()
                    cookies.load(fingerprint)
                    fingerprint = cookies.get('device_fingerprint').value if 'device_fingerprint' in cookies else None
            except:  # noqa: E722
                fingerprint = None
            
            try:
                # 等待客户端发送第一条消息（包含user_id）
                init_message = ws_client.receive()
                if init_message is None:
                    log_func("WARN", "通信", f"{addr} | 未提供ID，关闭连接")
                    ws_client.close()
                    return
                
                # 解析客户端发送的初始消息
                try:
                    init_data = json.loads(init_message)
                    user_id = init_data.get('user_id')
                    
                    if not user_id:
                        log_func("WARN", "通信", f"{addr} | 未提供ID，关闭连接")
                        ws_client.close()
                        return
                except json.JSONDecodeError:
                    log_func("WARN", "通信", f"{addr} | 无效的初始消息格式")
                    ws_client.close()
                    return
                
                # 添加客户端（使用客户端提供的user_id）
                client_id, error = ws_manager.add_client(ws_client, addr, user_id)
                if error:
                    # 发送错误消息
                    ws_client.send(json.dumps({
                        "type": "error",
                        "message": error
                    }))
                    log_func("WARN", "通信", f"{addr} | 添加客户端失败: {error}")
                    ws_client.close()
                    return
                
                # 在客户端信息中存储设备指纹
                with ws_manager.lock:
                    if client_id in ws_manager.clients:
                        ws_manager.clients[client_id]['fingerprint'] = fingerprint
                
                # 发送欢迎消息
                welcome_msg = json.dumps({
                    "type": "welcome",
                    "client_id": client_id,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "message": "已成功连接到WebSocket服务"
                })
                ws_client.send(welcome_msg)
                
                # 保持连接活跃
                while not ws_client.closed:
                    message = ws_client.receive()
                    if message is None:
                        continue
                    
                    # 记录收到的消息
                    log_func("DEBUG", "通信", 
                            f"{addr} | {client_id} | 收到消息: {message[:100]}")
            
            except Exception as e:
                log_func("ERROR", "通信", 
                        f"{addr} | {client_id if 'client_id' in locals() else '未知'} | 通信异常 -> {str(e)}")
            finally:
                # 移除客户端
                if 'client_id' in locals():
                    ws_manager.remove_client(client_id)

        return app

    def _run_service(self, service_name: str, app_creator, address: tuple):
        """运行服务进程"""
        def log(level, title, message):
            try:
                self.log_queue.put((level, title, f"[{service_name}] {message}"), timeout=0.1)
            except Full:
                # 如果队列满，直接打印到控制台
                with IOLock:
                    sys.stdout.write(f"[{level}] {title}: [{service_name}] {message}\n")
                    sys.stdout.flush()
        
        try:
            # 创建应用
            app = app_creator(log)
            
            # 确保使用的是gevent的 сервер
            if service_name == "Api":
                log("SUCC", "启动", f"启动带有WebSocket支持的API服务: ws://{address[0]}:{address[1]}")
                
                # 创建支持WebSocket的服务器
                server = WSGIServer(
                    (address[0], address[1]),
                    app,
                    handler_class=WebSocketHandler
                )
                server.serve_forever()
            else:
                # Web服务继续使用Waitress
                log("SUCC", "启动", f"服务已启动: http://{address[0]}:{address[1]}")
                serve(
                    app, 
                    host=address[0], 
                    port=address[1],
                    threads=4,
                    channel_timeout=60,
                )
                
        except Exception as e:
            log("ERRO", "错误", f"服务启动失败: {str(e)}")
            sys.exit(1)
    
    def _restart_process(self, name: str):
        """重启指定服务进程"""
        if self.restart_attempts[name] >= self.max_restarts:
            self.logger.erro(
                "重启", 
                f"[{name}] 服务已达最大重启次数({self.max_restarts})，停止尝试"
            )
            return False
        
        self.restart_attempts[name] += 1
        delay = self.restart_delays[name]
        
        self.logger.warn("重启", f"[{name}] 服务等待{delay}秒后重启...")
        time.sleep(delay)
        
        # 指数退避策略
        self.restart_delays[name] = min(delay * 2, 60)  # 最大延迟60秒
        
        self.logger.warn("重启", f"[{name}] 正在重启 {name} 服务...")
        self.processes[name] = multiprocessing.Process(
            target=self._run_service,
            name=f"[{name}] 服务进程",
            args=(name, 
                self._create_web_app if name == "Web" else self._create_api_app,
                self.web_addr if name == "Web" else self.api_addr),
            daemon=True
        )
        self.processes[name].start()
        
        if self.processes[name].is_alive():
            self.logger.succ("重启", f"[{name}] 服务重启成功 (PID: {self.processes[name].pid})")
            return True
        else:
            self.logger.erro("重启", f"[{name}] 服务重启失败")
            return False

    def _monitor_processes(self):
        """监控服务进程状态"""
        self.logger.info("监控", "进程监控已启动")
        try:
            while any(p.is_alive() for p in self.processes.values()):
                time.sleep(0.5)
                
                # 检查进程状态
                for name, process in self.processes.items():
                    if not process.is_alive():
                        exitcode = process.exitcode
                        if exitcode != 0:
                            self.logger.erro(
                                "异常", 
                                f"[{name}] 服务异常退出 (代码: {exitcode})"
                            )
                        
                        # 尝试重启进程
                        if not self._restart_process(name):
                            # 如果重启失败且达到最大次数，检查是否需要完全关闭
                            if all(not p.is_alive() for p in self.processes.values()):
                                self.logger.erro("故障", "所有服务已停止，系统即将关闭")
                                self._shutdown()
                                return
        except KeyboardInterrupt:
            self._shutdown()
        except Exception as e:
            self.logger.erro("监控", f"进程监控异常: {str(e)}")
            self._shutdown()
        finally:
            self._shutdown()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    PluginMarketWeb()