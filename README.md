# 什亭之匣 (Plana) - 自用 Nonebot2 机器人

> **「说明」**
> 本项目是个人使用的的 QQ 机器人，底子是 [Nonebot2](https://github.com/nonebot/nonebot2)。写它的初衷是想在搞定基础聊天之余，能更爽地拿 Python 搓点新活儿，最后留给自己用。代码挂上来纯属分享一下，如果想交流或看机器人demo可以来qq群`638148063`。至于为什么叫chubot是因为最开始没想着是plana的人设，做着做着才确定下来

## 1. 核心特性 (黑科技)

本机器人不仅仅是复读机，它还内置了一些奇奇怪怪的功能：

* **多级 LLM 故障切换**：内置 `SMART`、`FAST`、`INSTANT` 三档模型配置，支持多 API 自动重试与回退，甚至能从 Gemini 3 Pro 一路退化到 Flash Lite，主打一个永不掉线。
* **什亭之匣演算 (Wolfram Engine)**：通过调用wolfram engine，能处理复杂的符号运算、绘制函数图像。支持宿主机直接调用或 Docker 环境下的跨容器 HTTP 调度。
* **分布式思维模块**（不好用）：
* `#llm并行`：多核心并发解析任务，汇总多角度视角。
* `#自动llm束`：模拟思维链拆解，递归处理复杂逻辑。
* ~~利用wolfram engine进行复杂的数学任务（尚未实现）~~
* **动态上下文管理**：支持发送 `.txt` 或 `.py` 文件直接作为对话背景；支持解析合并转发的聊天记录；还会自动压缩超长历史记录。
* **群聊背景监听**：即便不艾特它，它也会悄悄记录群聊“背景音”，方便在被召唤时瞬间理解前因后果。

## 2. 快速部署

### 本地开发模式

1. **装依赖**：
```bash
pip install "nonebot2[fastapi]" nonebot-adapter-onebot nonebot-plugin-status nonebot-plugin-multincm nonebot-plugin-fakemsg httpx

```


2. **配环境**：
* 拷贝 `.env.example` 为 `.env`。
* 拷贝 `config/models.example.json` 为 `config/models.json`。


3. **跑起来**：
```bash
python bot.py

```



### Docker 容器化

如果你喜欢干净的环境，可以使用 Docker：

```bash
docker build -t plana-bot .
docker run --rm -it --env-file .env -v $(pwd)/config:/app/config -v $(pwd)/data:/app/data plana-bot

```

## 3. 配置文件指南

### .env 核心配置

* `SUPERUSERS`: 你的 QQ 号，只有你能让它执行敏感操作。
* `ONEBOT_ACCESS_TOKEN`: 和你的 OneBot 签名对上。

### models.json 模型配置

你需要配置至少一个模型，支持 OpenAI 格式的 API。别忘了把你的 API Key 塞进去。

## 4. 常用指令 (给老师的备忘录)

* `#切pro` / `#切flash` / `#切instant`：随时切换计算能耗模式。
* `#图片`：开启视觉和提示词录制模式，批量投喂图片后一并分析。
* `#计算 <内容>`：调用 Wolfram engine 进行数学演算。
* `#总结`：整理最近的聊天摘要（暂时不是普拉娜的视角）。
* `/文件列表`：查看并开启你通过私聊发给它的上下文文件。
* `重置`：格式化当前会话

## 5. 开发建议

如果你想往 `plugins/` 目录加新活儿，建议多利用 `common.py` 里的 `query_llm` 和 `debug_log`

---

**最后说一句：**
代码主要使用AI生成，后续会手工+AI微调代码细节
