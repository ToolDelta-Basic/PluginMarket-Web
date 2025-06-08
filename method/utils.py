import requests

# 要访问的GitHub JSON文件URL
url = "https://raw.githubusercontent.com/ToolDelta-Basic/PluginMarket/main/market_tree.json"

try:
    # 发送HTTP GET请求
    response = requests.get(url)
    
    # 检查HTTP请求是否成功
    if response.status_code == 200:
        # 将内容转换为JSON格式
        data = response.json()
        
        # 示例：打印MarketVersion
        print("MarketVersion:", data.get("MarketVersion"))
        
        # 示例：打印所有插件信息
        plugins = data.get("MarketPlugins", {})
        for plugin_name, plugin_info in plugins.items():
            print(f"插件名: {plugin_info.get('name', plugin_name)}")
            print(f"作者: {plugin_info.get('author', '未知')}")
            print(f"版本: {plugin_info.get('version', '未知')}")
            print("-" * 30)
    else:
        print(f"无法获取内容，状态码: {response.status_code}")
except requests.exceptions.RequestException as e:
    print(f"请求出错: {e}")