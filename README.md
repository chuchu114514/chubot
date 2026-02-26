# nonebot

这个目录可以单独作为一个仓库推送。

## 推荐提交内容
- `bot.py`
- `wolfram_server.py`
- `plugins/`
- `pyproject.toml`
- `Dockerfile`
- `config/models.example.json`

## 不建议提交内容
- `.env`、`.env.*`
- `config/models.json`
- `data/` 下运行态数据（会话、图片、用户文件等）
- `__pycache__/`、`*.pyc`

## 首次使用
1. 复制配置模板：
   - `cp config/models.example.json config/models.json`
2. 写入你的 API 配置到 `config/models.json`
3. 配置 `.env`（按你的 OneBot/NapCat 接入参数）
4. 启动：
   - 本地：`python bot.py`
   - Docker：`docker build -t my-nonebot . && docker run --rm my-nonebot`

## 仅推送 nonebot 目录（在当前大仓库内）
在仓库根目录执行：

```bash
git add nonebot/bot.py nonebot/wolfram_server.py nonebot/plugins nonebot/pyproject.toml nonebot/Dockerfile nonebot/.gitignore nonebot/README.md nonebot/config/models.example.json
```

如果你要把 `nonebot` 变成独立仓库，进入 `nonebot` 后 `git init` 再关联远端。
