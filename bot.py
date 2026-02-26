import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


# 初始化 NoneBot
nonebot.init()

# 注册适配器 (这一步是为了让它能听懂QQ消息)
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

# 加载插件
# 1. 告诉它去当前目录下的 plugins 文件夹里找代码
nonebot.load_plugins("plugins")

# 2. 从 pyproject.toml 中加载插件
nonebot.load_from_toml("pyproject.toml")


if __name__ == "__main__":
    nonebot.run()
