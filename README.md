# nonebot

这是一个基于 NoneBot2 + OneBot V11 的自定义机器人目录，可独立作为仓库维护。

## 1. 环境要求
- Python 3.10+
- 能接入 OneBot V11（例如 NapCat）
- Linux/macOS/WSL（Windows 也可，命令按实际环境调整）

## 2. 快速开始（本地运行）
在 `nonebot` 目录执行：

```bash
pip install "nonebot2[fastapi]" nonebot-adapter-onebot nonebot-plugin-status nonebot-plugin-multincm nonebot-plugin-fakemsg httpx
cp .env.example .env
cp config/models.example.json config/models.json
python bot.py
```

## 3. 配置说明

### 3.1 OneBot / NoneBot 基础配置
编辑 `.env`（从 `.env.example` 复制）：
- `HOST` / `PORT`: 监听地址与端口
- `ONEBOT_ACCESS_TOKEN`: 与 OneBot 服务端一致
- `SUPERUSERS`: 超级用户 QQ 列表

### 3.2 模型配置
编辑 `config/models.json`：
- 三个分组：`SMART` / `FAST` / `INSTANT`
- 每个模型需要：`name`、`api_base`、`api_key`

程序会按分组顺序自动故障切换（同组单模型失败会重试并回退到下一个）。

### 3.3 Wolfram 服务（可选）
如果你启用了 Wolfram 相关能力，可单独启动：

```bash
python wolfram_server.py
```

常用环境变量：
- `WOLFRAM_SERVER_HOST`（默认 `0.0.0.0`）
- `WOLFRAM_SERVER_PORT`（默认 `9876`）
- `WOLFRAM_EXECUTABLE`（WolframKernel 路径）
- `WOLFRAM_PASSWORD_FILE`（授权文件路径）

## 4. Docker 运行

```bash
docker build -t my-nonebot .
docker run --rm -it \
  --env-file .env \
  -v $(pwd)/config:/app/config \
  -v $(pwd)/data:/app/data \
  my-nonebot
```

## 5. 常见问题
- 启动报连接 OneBot 失败：检查 OneBot 服务是否启动、端口和 Token 是否一致。
- 模型请求报 401/403：检查 `config/models.json` 的 `api_key` 是否正确。
- 插件导入失败：确认依赖已安装，或重新执行快速开始中的 `pip install`。

## 6. 仓库提交建议

### 推荐提交
- `bot.py`
- `wolfram_server.py`
- `plugins/`
- `pyproject.toml`
- `Dockerfile`
- `.gitignore`
- `.env.example`
- `config/models.example.json`
- `README.md`

### 不建议提交
- `.env`、`.env.*`
- `config/models.json`
- `data/` 下运行态数据（会话、图片、用户文件等）
- `__pycache__/`、`*.pyc`

## 7. 独立仓库推送

```bash
cd /root/my_bot/nonebot
git init
git add .
git commit -m "init: custom nonebot bot"
git branch -M main
git remote add origin <your-repo-url>
git push -u origin main
```

## 8. 一键提交并推送

项目根目录提供了 `push.sh`，运行后只会询问一次 commit 内容，然后自动执行 `git add -A`、`git commit`、`git push`。

```bash
cd /root/my_bot/nonebot
chmod +x push.sh
./push.sh
```

首次使用前请确保：
- 已完成 `git init`
- 已配置 `origin`
- 已配置 `git user.name` 和 `git user.email`
