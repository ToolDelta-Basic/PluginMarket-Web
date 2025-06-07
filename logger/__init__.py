import queue
import threading
import colorama
import datetime
import time
import sys
from exception import OutPutQueueFullError
from logger.queue_type import OutPut, InPut

IOLock: threading.RLock = threading.RLock()
_original_print: any = print

colorama.init(autoreset=True)

class Logger:
    def __init__(self, queue_maxsize: int = 1024, messages_per_minute: int = 600):
        """
        初始化 Logger 实例
        
        Args:
            queue_maxsize (int): 日志队列的最大容量，默认为 1024
            messages_per_minute (int): 每分钟输出的消息数量，默认为 600
        """
        self.logger_queue: queue.Queue = queue.Queue(queue_maxsize)
        self.out_speed: float = self.calculate_sleep_time(messages_per_minute)
        self._msg_show_thread: threading.Thread = threading.Thread(target=self.output_thread)
        self._msg_show_thread.daemon = True
        self._msg_show_thread.start()

    def build_output(self, title: str, message: str, t_r: any, m_r: any) -> str:
        """
        构建日志输出字符串
        
        Args:
            title (str): 日志标题
            message (str): 日志内容
            t_r (any): 标题的颜色控制符
            m_r (any): 内容的颜色控制符
        
        Returns:
            str: 格式化后的日志字符串
        """
        return f"[{datetime.datetime.now().strftime('%H:%M')}] {t_r} {title} {colorama.Style.RESET_ALL} {m_r}{message}{colorama.Style.RESET_ALL}"

    def info(self, title: str = "信息", message: str = None) -> bool:
        """
        记录信息级别的日志
        
        Args:
            title (str): 日志标题，默认为 "信息"
            message (str): 日志内容，默认为 None
        
        Returns:
            bool: 日志是否成功加入队列
        """
        if self.logger_queue.full():
            raise OutPutQueueFullError
        output: str = self.build_output(title=title, message=message, t_r=colorama.Back.WHITE, m_r=colorama.Fore.WHITE)
        self.put_output((datetime.datetime.now(), output))

    def warn(self, title: str = "警告", message: str = None) -> bool:
        """
        记录警告级别的日志
        
        Args:
            title (str): 日志标题，默认为 "警告"
            message (str): 日志内容，默认为 None
        
        Returns:
            bool: 日志是否成功加入队列
        """
        if self.logger_queue.full():
            raise OutPutQueueFullError
        output: str = self.build_output(title=title, message=message, t_r=colorama.Back.YELLOW, m_r=colorama.Fore.YELLOW)
        self.put_output((datetime.datetime.now(), output))

    def erro(self, title: str = "错误", message: str = None) -> bool:
        """
        记录错误级别的日志
        
        Args:
            title (str): 日志标题，默认为 "错误"
            message (str): 日志内容，默认为 None
        
        Returns:
            bool: 日志是否成功加入队列
        """
        if self.logger_queue.full():
            raise OutPutQueueFullError
        output: str = self.build_output(title=title, message=message, t_r=colorama.Back.RED, m_r=colorama.Fore.RED)
        self.put_output((datetime.datetime.now(), output))

    def fail(self, title: str = "失败", message: str = None) -> bool:
        """
        记录失败级别的日志
        
        Args:
            title (str): 日志标题，默认为 "失败"
            message (str): 日志内容，默认为 None
        
        Returns:
            bool: 日志是否成功加入队列
        """
        if self.logger_queue.full():
            raise OutPutQueueFullError
        output: str = self.build_output(title=title, message=message, t_r=colorama.Back.RED, m_r=colorama.Fore.RED)
        self.put_output((datetime.datetime.now(), output))

    def succ(self, title: str = "成功", message: str = None) -> bool:
        """
        记录成功级别的日志
        
        Args:
            title (str): 日志标题，默认为 "成功"
            message (str): 日志内容，默认为 None
        
        Returns:
            bool: 日志是否成功加入队列
        """
        if self.logger_queue.full():
            raise OutPutQueueFullError
        output: str = self.build_output(title=title, message=message, t_r=colorama.Back.GREEN, m_r=colorama.Fore.GREEN)
        self.put_output((datetime.datetime.now(), output))

    def put_output(self, structure: tuple) -> bool:
        """
        将日志结构放入输出队列
        
        Args:
            structure (tuple): 包含时间戳和日志内容的元组
        
        Returns:
            bool: 是否成功放入队列
        """
        if self.logger_queue.full():
            raise OutPutQueueFullError
        self.logger_queue.put_nowait({"type": OutPut, "structure": structure})
        return True

    def output_thread(self) -> None:
        """
        输出线程的主循环，负责从队列中取出日志并输出到控制台
        """
        while True:
            if not self.logger_queue.empty():
                time.sleep(self.out_speed)
                Message: dict = self.logger_queue.get_nowait()
                if Message["type"] == OutPut:
                    with IOLock:
                        sys.stdout.write(Message["structure"][1]+"\n")
                        sys.stdout.flush()
                elif Message["type"] == InPut:
                    val: str = input(Message["structure"][1])
                    self.logger_queue.put_nowait((Message["structure"][0], val))

    def calculate_sleep_time(self, messages_per_minute: int) -> float:
        """
        计算每分钟输出 x 个消息时，每条消息之间的 sleep 时间（秒）
        
        Args:
            messages_per_minute (int): 每分钟要输出的消息数量
        
        Returns:
            float: 每条消息之间的 sleep 时间（秒）
        """
        if messages_per_minute <= 0:
            raise ValueError("messages_per_minute 必须大于 0")
        return 60.0 / messages_per_minute