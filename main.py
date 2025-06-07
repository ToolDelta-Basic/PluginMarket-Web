import os
import sys
import time
import colorama
import signal
import threading
import datetime
import json
import multiprocessing
import ipaddress
import hashlib
import uuid
import sqlite3
from logger import Logger
from queue import Full
from flask import Flask, jsonify, render_template, request, g, make_response
from waitress import serve
from exception import OutPutQueueFullError
from logger.queue_type import OutPut, InPut

# 全局锁和事件
IOLock: threading.RLock = threading.RLock()
ThreadEvent: threading.Event = threading.Event()

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

        if os.path.exists(self.ip_ban_file):
            try:
                with open(self.ip_ban_file, 'r') as f:
                    ip_bans = json.load(f)

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
                                expired_ips.append(ip)

                for ip in expired_ips:
                    del ip_bans[ip]

                if expired_ips and self.logger:
                    self.logger.info("清理", f"自动清理过期封禁IP: {', '.join(expired_ips)}")
            except Exception as e:
                if self.logger:
                    self.logger.error("加载", f"加载IP封禁列表失败: {str(e)}")
                # 若加载失败，保留空字典
                ip_bans = {}

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
    
    def __init__(self, web_address: tuple = ("0.0.0.0", 8111), api_address: tuple = ("0.0.0.0", 8112)) -> None:
        # 创建主日志记录器
        self.logger: Logger = Logger(4096, 600)
        self.logger.succ("启动", f"初始化插件市场服务: {self.name} {self.version}")
        
        self.web_addr: tuple = web_address
        self.api_addr: tuple = api_address
        
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
            )
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
            return render_template('index.html', title=self.name, version=self.version)
        
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
        
        @app.route('/api/status', methods=['GET'])
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
        
        @app.route('/api/admin/ipban/ban', methods=['POST'])
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
        
        @app.route('/api/admin/ipban/unban', methods=['POST'])
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
        
        @app.route('/api/admin/ipban/reload', methods=['POST'])
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
        
        @app.route('/api/admin/device/ban', methods=['POST'])
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
                    "message": f"封禁设备失败"
                }), 500
        
        @app.route('/api/admin/device/unban', methods=['POST'])
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
                    "message": f"解封设备失败"
                }), 500
        
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
        
        return app
    
    def _run_service(self, service_name: str, app_creator, address: tuple):
        """运行服务进程"""
        # 定义进程内部的日志函数
        def log(level, title, message):
            try:
                self.log_queue.put((level, title, f"[{service_name}] {message}"), timeout=0.1)
            except Full:
                # 如果队列满，直接打印到控制台
                with IOLock:
                    sys.stdout.write(f"[{level}] {title}: [{service_name}] {message}\n")
                    sys.stdout.flush()
        
        try:
            # 创建应用并传入日志函数
            app = app_creator(log)
            
            # 记录服务启动信息
            log("SUCC", "启动", f"服务已启动: http://{address[0]}:{address[1]}")
            
            # 使用Waitress生产服务器
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