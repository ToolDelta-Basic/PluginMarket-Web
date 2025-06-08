import os
import zipfile
import json
import logging
import requests
import websockets
import asyncio
import random
import string
import datetime
from typing import Optional, Dict, Any

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class ClientConfig:
    def __init__(self, server_url: str = "http://127.0.0.1:8112"):
        self.server_url = server_url
        self.user_id: str = None
        self.device_fingerprint: str = None
        self.websocket_url = f"ws://{server_url.split('http://')[1].split('/')[0]}/api/socket"

    def generate_user_id(self) -> str:
        """生成6位大写字母的客户端ID"""
        return ''.join(random.choices(string.ascii_uppercase, k=6))

    def generate_device_fingerprint(self) -> str:
        """生成设备指纹"""
        return ''.join(random.choices(string.ascii_letters + string.digits, k=32))

class HTTPClient:
    def __init__(self, config: ClientConfig):
        self.config = config
        self.session = requests.Session()
        
    def _get_full_url(self, endpoint: str) -> str:
        """获取完整的API URL"""
        return f"{self.config.server_url}/{endpoint.lstrip('/')}"

    def _add_headers(self, headers: Dict[str, str] = None) -> Dict[str, str]:
        """添加默认headers"""
        default_headers = {
            'Content-Type': 'application/json',
            'X-Custom-ID': self.config.user_id
        }
        if headers:
            default_headers.update(headers)
        return default_headers

    def _make_request(self, method: str, endpoint: str, data: Dict[str, Any] = None, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """封装requests请求"""
        try:
            response = self.session.request(
                method=method,
                url=self._get_full_url(endpoint),
                json=data,
                params=params,
                headers=self._add_headers()
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"请求失败: {str(e)}")
            raise

    # Example API调用函数
    def get_plugins(self) -> Dict[str, Any]:
        """获取所有插件信息"""
        return self._make_request('GET', 'api/plugins')

    def get_packages(self) -> Dict[str, Any]:
        """获取所有整合包信息"""
        return self._make_request('GET', 'api/packages')

    def download_plugin(self, plugin_id: str) -> Dict[str, Any]:
        """记录插件下载"""
        data = {'user_id': self.config.user_id}
        return self._make_request('POST', f'api/plugin/download/{plugin_id}', data=data)

    def rate_plugin(self, plugin_id: str, rating: int) -> Dict[str, Any]:
        """为插件评分"""
        data = {'user_id': self.config.user_id, 'rating': rating}
        return self._make_request('POST', f'api/plugin/rate/{plugin_id}', data=data)

class WebSocketClient:
    def __init__(self, config: ClientConfig):
        self.config = config
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.loop = asyncio.get_event_loop()

    def save_received_file(self):
        if not self.current_file:
            return
        download_dir = os.path.join("downloads", self.current_file['id'])
        os.makedirs(download_dir, exist_ok=True)
        file_path = os.path.join(download_dir, f"{self.current_file['id']}.zip")
        with open(file_path, 'wb') as f:
            f.write(self.current_file['data'])
        try:
            # 解压文件
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(download_dir)
            os.remove(file_path)
            logger.info(f"文件 {self.current_file['id']} 已保存到 {download_dir}")
        except Exception as e:
            logger.error(f"解压文件失败: {str(e)}")

    async def connect(self) -> None:
        """建立WebSocket连接"""
        async with websockets.connect(self.config.websocket_url) as ws:
            self.ws = ws
            await self._register_client()
            await self._listen_messages()
    
    async def _register_client(self) -> None:
        """注册客户端"""
        if self.config.user_id is None:
            self.config.user_id = self.config.generate_user_id()
            logger.info(f"生成新的客户端ID: {self.config.user_id}")
        
        register_message = {
            'user_id': self.config.user_id
        }
        await self.ws.send(json.dumps(register_message))
        
        response = await self.ws.recv()
        logger.info(f"收到响应: {response}")

    async def _listen_messages(self):
        try:
            async for message in self.ws:
                if isinstance(message, str):
                    msg = json.loads(message)
                    if msg.get("type") == "file_start":
                        file_id = msg["file_id"]
                        total_size = msg["size"]
                        self.current_file = {"id": file_id, "received": 0, "data": b''}
                        logger.info(f"开始接收文件 {file_id}, 总大小: {total_size} bytes")
                    elif msg.get("type") == "file_end":
                        if self.current_file:
                            self.save_received_file()
                            logger.info(f"完成接收文件 {self.current_file['id']}")
                            self.current_file = None
                    else:
                        logger.info(f"接收消息: {message}")
                elif isinstance(message, bytes):
                    if self.current_file:
                        self.current_file["received"] += len(message)
                        self.current_file["data"] += message
                        progress = (self.current_file["received"] / total_size) * 100
                        logger.info(f"接收进度: {progress}%")
            else:
                logger.info("WebSocket连接已关闭")
        except websockets.ConnectionClosed:
            logger.info("WebSocket连接已关闭")
        except Exception as e:
            logger.error(f"WebSocket错误: {str(e)}")

    async def send_message(self, message: Dict[str, Any]) -> None:
        """发送消息"""
        if self.ws:
            await self.ws.send(json.dumps(message))
            logger.info(f"发送消息: {message}")
        else:
            logger.error("WebSocket未连接")

class Client:
    def __init__(self):
        self.config = ClientConfig()
        self.http_client = HTTPClient(self.config)
        self.ws_client = WebSocketClient(self.config)
        
    def generate_ids(self) -> None:
        """生成必要的ID"""
        if not self.config.user_id:
            self.config.user_id = self.config.generate_user_id()
            logger.info(f"生成客户端ID: {self.config.user_id}")
        
        if not self.config.device_fingerprint:
            self.config.device_fingerprint = self.config.generate_device_fingerprint()
            logger.info(f"生成设备指纹: {self.config.device_fingerprint}")

    async def connect_websocket(self) -> None:
        """连接WebSocket"""
        await self.ws_client.connect()

# 使用示例：
if __name__ == "__main__":
    # 初始化客户端
    client = Client()
    client.generate_ids()
    try:
        asyncio.get_event_loop().run_until_complete(client.connect_websocket())
    except Exception as e:
        logger.error(f"WebSocket连接错误: {str(e)}")