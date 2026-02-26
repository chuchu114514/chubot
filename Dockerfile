FROM python:3.10-slim

WORKDIR /app

# 把 ffmpeg 也加上，很多媒体插件都要用
RUN apt-get update && \
    apt-get install -y ca-certificates ffmpeg && \
    update-ca-certificates

# 核心依赖：NoneBot2 和 OneBot 适配器
RUN pip install --no-cache-dir nonebot2[fastapi] nonebot-adapter-onebot httpx

# 把你要的三个插件全在这里装上
RUN pip install nonebot-plugin-status nonebot-plugin-multincm nonebot-plugin-fakemsg

# 将当前文件夹（包含你的 bot.py 和 plugins）复制进容器
COPY . .

# 启动命令
CMD ["python", "bot.py"]

