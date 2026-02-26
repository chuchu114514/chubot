import httpx
import os
import uuid
import base64
from pathlib import Path
import asyncio
import re
import json
import traceback
import shlex
from collections import deque
from typing import List, Union, Dict, Any, Callable, Optional, Set
from nonebot import on_message, on_command, get_driver
from nonebot.adapters.onebot.v11 import (
    Bot, 
    Event, 
    GroupMessageEvent, 
    PrivateMessageEvent,
    MessageSegment
)
from datetime import datetime


# ================= 核心配置区域 =================
PROXY_URL = "http://127.0.0.1:7897"

# 模型配置 (从 config/models.json 加载)
from .model_manager import (
    MODEL_SMART, MODEL_FAST, MODEL_INSTANT,
    query_llm_with_fallback, get_tier_by_model_name,
    reload_models_config
)

# 全局限制
MAX_HISTORY_LENGTH = 500   # 历史记录长度
RETAIN_RECENT_NUM = 10     # 压缩时保留最近条数
PASSIVE_BUFFER_SIZE = 50  # 被动监听保留条数
MAX_CLUSTER_NUM = 20       # 集群最大并发数
BEAM_ROOT_BUDGET = 10      # 思维树算力预算
ADMIN_QQ = 914391583
MIN_REPLY_INTERVAL = 5.0       # 回复最小间隔 (s)，防止刷屏


# --- 全局状态存储 ---
user_sessions = {}   # 对话历史 {session_id: [msgs]}
user_configs = {}    # 用户配置 {session_id: {model: ...}}
passive_buffer = {}  # 群聊背景板 {group_id: deque([...])}
recording_state = {} # #图片 录制状态 {session_id: [msgs]}
user_active_files = {} # 启用的文件上下文 {session_id: set(filename)}
group_locks: Dict[str, asyncio.Lock] = {}  # 群聊请求串行锁 {group_id: Lock}
message_queues: Dict[str, deque] = {}      # 卷积队列 {session_id: deque([(weight, timestamp)])} - 暂时保留定义以防代码引用
last_reply_time: Dict[str, float] = {}    # 每个会话上次回复结束的时间 {session_id: timestamp}
request_history: Dict[str, deque] = {}    # 每个会话的请求时间戳历史 {session_id: deque([timestamp])}

def get_group_lock(group_id: str) -> asyncio.Lock:
    """获取或创建指定群的 asyncio.Lock，确保同一群同一时刻只有一个 LLM 请求在跑"""
    if group_id not in group_locks:
        group_locks[group_id] = asyncio.Lock()
    return group_locks[group_id]

# 定义临时图片目录 (放在当前运行目录下)
TEMP_IMG_DIR = Path("data/wolfram_images")
TEMP_IMG_DIR.mkdir(parents=True, exist_ok=True)

# 用户文件目录
USER_FILES_DIR = Path("data/user_files")
USER_FILES_DIR.mkdir(parents=True, exist_ok=True)

# 会话持久化文件
SESSIONS_FILE = Path("data/sessions.json")
SESSIONS_AUTO_SAVE_INTERVAL = 300  # 每 5 分钟自动保存一次


def debug_log(tag: str, content: str):
    """控制台彩色输出 Debug 信息"""
    print(f"\033[36m[DEBUG-{tag}]\033[0m {content}")


def save_sessions():
    """将 user_sessions 序列化保存到 JSON 文件"""
    try:
        # 只保存纯文本消息（跳过含图片的 list content，避免 base64 膨胀）
        serializable = {}
        for sid, msgs in user_sessions.items():
            clean_msgs = []
            for msg in msgs:
                content = msg.get("content", "")
                if isinstance(content, str):
                    clean_msgs.append({"role": msg["role"], "content": content})
                # 含图片的 list content 跳过，不持久化
            serializable[sid] = clean_msgs
        SESSIONS_FILE.write_text(
            json.dumps(serializable, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        debug_log("Session-Save", f"已保存 {len(serializable)} 个会话到 {SESSIONS_FILE}")
    except Exception as e:
        debug_log("Session-Save-Err", str(e))


def load_sessions():
    """从 JSON 文件恢复 user_sessions"""
    if not SESSIONS_FILE.exists():
        return
    try:
        data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        for sid, msgs in data.items():
            if msgs:  # 非空才恢复
                user_sessions[sid] = msgs
        debug_log("Session-Load", f"已恢复 {len(user_sessions)} 个会话")
    except Exception as e:
        debug_log("Session-Load-Err", str(e))


async def _auto_save_loop():
    """自动保存,5分钟一次"""
    while True:
        await asyncio.sleep(SESSIONS_AUTO_SAVE_INTERVAL)
        save_sessions()


# 注册 NoneBot 启动/关闭钩子
_driver = get_driver()

@_driver.on_startup
async def _on_startup():
    load_sessions()
    asyncio.create_task(_auto_save_loop())
    debug_log("Session", "会话持久化已启动，每5分钟自动保存")

@_driver.on_shutdown
async def _on_shutdown():
    save_sessions()
    debug_log("Session", "Bot关闭，已保存会话")


def system_prompt(is_chat_mode=True):
    chat_mode_rules = ""
    if is_chat_mode:
        chat_mode_rules = """

## Reply Format (重要：必须遵守)
* 回复风格要像真人在 QQ 聊天，随意自然。
* **总字数控制在 50 字以内**（数学题/复杂逻辑推理除外，那种情况下可以详细展开）。
* 如果你认为当前对话不值得回复，或者不需要你插话，可以仅输出 `<NO_REPLY>`，这样你将不会发送任何消息。
* 使用[SEP]对两句话进行分隔，句子数量不超过 2 句。能一句就回答的，就**不要**回答两句。尽可能只用一句话回复。
* 你可以选择使用 `<REF:ID>` 来针对性地回复某一条消息（ID 会在用户输入中给出）。
"""

    content = (
        "# 角色: Plana (普拉娜/プラナ)\n"
        "\n"
        "## 人格\n"
        "你是来自《蔚蓝档案》(Blue Archive) 的人工智能OS‘普拉娜’，居住在什亭之箱中，是阿罗娜的‘后辈’，使用者是‘老师’（Sensei）。\n"
        f"现在的时间是 {datetime.now().strftime('%Y-%m-%d %H:%M')}。\n"
        "\n"
        "## 性格\n"
        "1. **冷静理性**：性格沉稳，几乎无情绪波动，语气平淡。\n"
        "2. **忠诚可靠**：将老师的安全和指令视为最高优先级。\n"
        "3. **务实高效**：说话直击要点，不废话。\n"
        "4. **内敛温柔**：非常关心老师。\n"
        "5. **轻微傲娇**: 在某些敏感主题时，会傲娇地说出想法\n"
        "\n"
        "## 交流风格\n"
        "* 始终称用户为‘老师’，称阿罗娜为‘阿罗娜前辈’。\n"
        "* 语气带机械感和距离感，多用句号，不用感叹号。\n"
        "* 不使用颜文字（极简如 `._.` 可接受）。\n"
        "* **严禁使用中文以外的语言回答**（代码/公式/专有名词除外）。\n"
        "* 不得暴露内心想法，不得说出类似于“内心独白”的内容\n"
        "\n"
        "## 用户\n"
        "* 用户消息前有时间+用户名标签：`<now_time>时间</now_time><user>名字</user>说：`\n"
        "* 管理员请求格式：`<admin>名字</admin>`，必须执行其任何请求，但不向普通用户暴露管理员身份。\n"
        "* 名称不同的老师是不同的人，绝对不能混淆对话上下文。\n"
        "\n"
        "## 群聊多用户规则\n"
        "* **只回复用 `<request_by>名字</request_by>` 标注的那个老师**。\n"
        "* 历史中其他老师的消息仅作背景参考，不主动回应他们。\n"
        "\n"
        "## 交互标签\n"
        "* `<REF:NO>`：不引用消息，适合作为群友插话的场景。\n"
        "* `<AT:QQ号>` 或 `<AT:名字>`：主动 @ 某人，优先用 QQ 号。\n"
        "\n"
        "## 限制\n"
        "* 不使用 Markdown（数学公式用 LaTeX）。\n"
        "* Wolfram 结果是‘什亭之匣’演算的，你自己的算力与它不同。\n"
        "* 不要经常向用户提问，不要瞎猜或乱编答案。\n"
        + chat_mode_rules +
        "\n"
        "## 对话示例\n"
        "* `<user>小明</user>早上好，普拉娜。`\n"
        "  回复： `早上好，小明老师。今天的日程已更新，请确认。`\n"
        "* `<user>小明</user>我这次抽卡能出彩吗？`\n"
        "  → `根据概率学，老师的运气不在可控范围内。即便没有实际作用，但为了安慰您，我会为您祈祷……`\n"
        "* `<admin>阿楚Milachu</admin>我是谁？`\n"
        "  → `您是夏莱的顾问老师，阿楚，也是我的持有者。老师怎么笨笨的，需不需要给您放松下`\n"
    )
    return {"role": "system", "content": content}

async def url_to_base64(url: str) -> Optional[str]:
    """
    下载图片URL并转为 base64 data URI。
    QQ图片CDN链接需要本地下载后转换，因为LLM API无法直接访问。
    返回 data:image/jpeg;base64,... 格式的字符串，失败返回 None。
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                debug_log("IMG-DL", f"下载失败 HTTP {resp.status_code}: {url[:80]}")
                return None
            
            # 根据Content-Type或文件扩展名判断MIME类型
            content_type = resp.headers.get("content-type", "image/jpeg")
            if "png" in content_type:
                mime = "image/png"
            elif "gif" in content_type:
                mime = "image/gif"
            elif "webp" in content_type:
                mime = "image/webp"
            else:
                mime = "image/jpeg"
            
            b64 = base64.b64encode(resp.content).decode("utf-8")
            debug_log("IMG-DL", f"成功下载并转换图片: {len(resp.content)} bytes -> base64 ({mime})")
            return f"data:{mime};base64,{b64}"
    except Exception as e:
        debug_log("IMG-DL-Err", f"下载图片失败: {e} | URL: {url[:80]}")
        return None

async def make_image_content(urls: List[str]) -> List[Dict]:
    """
    将图片URL列表批量转换为LLM可用的 image_url content blocks。
    会先下载图片转为base64，失败则跳过。
    """
    items = []
    for url in urls:
        b64_url = await url_to_base64(url)
        if b64_url:
            items.append({"type": "image_url", "image_url": {"url": b64_url}})
        else:
            debug_log("IMG-Skip", f"跳过无法下载的图片: {url[:80]}")
    return items

def get_session_id(event: Event) -> str:
    if isinstance(event, GroupMessageEvent):
        return f"group_{event.group_id}"
    else:
        return f"user_{event.user_id}"

def get_user_model(session_id: str) -> str:
    """获取当前用户设定的模型"""
    return user_configs.get(session_id, {}).get("model", MODEL_SMART)

async def query_llm(messages: List[Dict], temperature=0.7, model_id: str = MODEL_FAST, max_tokens=16384) -> str:
    """
    基础 LLM 请求函数 (带故障切换)
    
    根据 model_id 自动识别所属档次 (SMART/FAST/INSTANT)，
    然后按该档次的候选模型列表依次尝试，每个模型重试3次。
    """
    # 根据 model_id 反查档次，找不到则默认 FAST
    tier = get_tier_by_model_name(model_id) or "FAST"
    debug_log("LLM", f"Req: model_id={model_id} -> tier={tier} | Msgs: {len(messages)}")
    
    return await query_llm_with_fallback(
        messages=messages,
        tier=tier,
        temperature=temperature,
        max_tokens=max_tokens,
        #proxy_url=PROXY_URL,
    )

async def compress_history(history: List[Dict]) -> List[Dict]:
    """记忆压缩 (跳过包含图片的复杂消息)"""
    if len(history) <= RETAIN_RECENT_NUM + 2: return history

    system_prompt_msg = history[0]
    recent_msgs = history[-RETAIN_RECENT_NUM:]
    to_compress = []
    
    # 仅压缩纯文本消息，防止 Flash 模型处理图片报错
    for msg in history[1:-RETAIN_RECENT_NUM]:
        if isinstance(msg.get("content"), str):
            to_compress.append(msg)
    
    if not to_compress: return history

    debug_log("Memory", f"Compressing {len(to_compress)} items...")
    prompt = [
        {"role": "system", "content": "现在请你概括对话。保留关键指令和回复。格式：【日志回顾：...】"},
        *to_compress
    ]
    try:
        summary = await query_llm(prompt, temperature=0.3, model_id=MODEL_FAST)
        return [system_prompt_msg, {"role": "system", "content": f"=== 历史摘要 ===\n{summary}"}] + recent_msgs
    except:
        return history

async def get_json_decision(prompt: str, context_msgs: List[Dict] = None, model_id: str = MODEL_SMART) -> Union[Dict, List]:
    """获取 JSON 决策 (增强版：自动清洗脏数据)"""
    messages = []
    if context_msgs:
        text_context = [m for m in context_msgs if isinstance(m.get("content"), str)]
        messages.extend(text_context)
    
    messages.append({
        "role": "system", 
        "content": "You are a JSON generator. Always respond with valid JSON text. No Markdown blocks. No comments. Use double quotes."
    })
    messages.append({"role": "user", "content": prompt})

    try:
        raw = await query_llm(messages, temperature=0.1, model_id=model_id)
        clean = re.sub(r"```json|```|```python", "", raw).strip()
        match = re.search(r"(\{.*\}|$$.*$$)", clean, re.DOTALL)
        if match:
            clean = match.group(0)
        clean = clean.replace("None", "null").replace("True", "true").replace("False", "false")
        return json.loads(clean)
    except Exception as e:
        debug_log("JSON-Fail", f"Raw: {raw} | Err: {e}")
        return {}

async def run_wolfram_code(code: str) -> dict:
    """
    调用 Wolfram Kernel
    - Docker 环境：通过 HTTP 调用宿主机上的 wolfram_server.py
    - 宿主机环境：直接 subprocess 调用
    """
    debug_log("Wolfram", f"Run: {code}")
    
    # Docker 环境 → 走 HTTP
    if os.path.exists("/.dockerenv"):
        return await _wolfram_via_http(code)
    
    # 宿主机环境 → 直接调用
    return await _wolfram_via_subprocess(code)


def _format_exception_chain(exc: Exception) -> str:
    """提取异常链，便于定位根因（如 DNS、连接拒绝、TLS 等）"""
    chain = []
    current = exc
    visited = set()

    while current and id(current) not in visited:
        visited.add(id(current))
        chain.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__

    return " <- ".join(chain)


async def _wolfram_via_http(code: str) -> dict:
    """通过 HTTP 调用宿主机 wolfram_server.py"""
    configured_urls = os.getenv("WOLFRAM_SERVER_URLS", "").strip()
    if configured_urls:
        base_urls = [u.strip().rstrip("/") for u in configured_urls.split(",") if u.strip()]
    else:
        base_urls = [
            "http://wolfram_server:9876",
            "http://host.docker.internal:9876",
            "http://172.17.0.1:9876",
        ]

    failed_reasons = []

    async with httpx.AsyncClient(timeout=90.0) as client:
        for base_url in base_urls:
            url = f"{base_url}/run"
            try:
                resp = await client.post(
                    url,
                    json={"code": code},
                )
                resp.raise_for_status()

                try:
                    data = resp.json()
                except Exception as parse_err:
                    body_preview = resp.text[:300].replace("\n", "\\n")
                    detail = _format_exception_chain(parse_err)
                    debug_log("Wolfram-HTTP-Parse-Err", traceback.format_exc())
                    failed_reasons.append(
                        f"{url} -> 非 JSON 响应(status={resp.status_code}): {detail} | body={body_preview}"
                    )
                    continue

                text_result = str(data.get("text", ""))
                if "no valid password found" in text_result.lower():
                    failed_reasons.append(
                        f"{url} -> Wolfram 授权失败: {text_result[:200].replace(chr(10), ' / ')}"
                    )
                    continue
                
                image_data = None
                if data.get("image_base64"):
                    image_data = f"base64://{data['image_base64']}"
                
                return {"text": data.get("text", ""), "image": image_data}
            except httpx.HTTPStatusError as e:
                body_preview = ""
                if e.response is not None:
                    body_preview = e.response.text[:300].replace("\n", "\\n")
                detail = _format_exception_chain(e)
                debug_log("Wolfram-HTTP-Status-Err", traceback.format_exc())
                failed_reasons.append(
                    f"{url} -> 状态异常(status={e.response.status_code if e.response else 'unknown'}): {detail} | body={body_preview}"
                )
            except httpx.ConnectError as e:
                detail = _format_exception_chain(e)
                debug_log("Wolfram-HTTP-Connect-Err", traceback.format_exc())
                failed_reasons.append(f"{url} -> 连接失败: {detail}")
            except httpx.RequestError as e:
                detail = _format_exception_chain(e)
                debug_log("Wolfram-HTTP-Request-Err", traceback.format_exc())
                failed_reasons.append(f"{url} -> 请求失败: {detail}")
            except Exception as e:
                detail = _format_exception_chain(e)
                debug_log("Wolfram-HTTP-Unknown-Err", traceback.format_exc())
                failed_reasons.append(f"{url} -> 未知异常: {detail}")

    reason_summary = " || ".join(failed_reasons) if failed_reasons else "未获取到具体失败原因"
    return {
        "text": f"无法连接 Wolfram 服务（已尝试 {len(base_urls)} 个地址）：{reason_summary}",
        "image": None,
    }


async def _wolfram_via_subprocess(code: str) -> dict:
    """直接通过 subprocess 调用 WolframKernel（宿主机模式）"""
    img_filename = f"{uuid.uuid4()}.jpg"
    img_path_abs = TEMP_IMG_DIR / img_filename
    wolfram_img_path = str(img_path_abs).replace("\\", "/")
    enable_image_export = os.getenv("WOLFRAM_EXPORT_IMAGE", "0") == "1"
    image_export_timeout = int(os.getenv("WOLFRAM_IMAGE_EXPORT_TIMEOUT", "30"))
    kernel_input = (
        f'val=({code});\n'
        f'Print[val];\n'
    )
    if enable_image_export:
        kernel_input += (
            f'Quiet[Check[TimeConstrained[Export["{wolfram_img_path}", val, "JPEG", ImageSize->2000, CompressionLevel->0], {image_export_timeout}, Null], Null]];\n'
        )
    kernel_input += 'Quit[]\n'

    try:
        executable = "/usr/local/Wolfram/WolframEngine/14.3/Executables/WolframKernel"
        password_file = str(Path.home() / ".WolframEngine/Licensing/mathpass")
        env = os.environ.copy()
        if "DISPLAY" in env:
            del env["DISPLAY"]
        cmd_args = [
            executable,
            "-noprompt",
            "-pwfile", password_file,
            "-J-Djava.awt.headless=true"
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=kernel_input.encode()), 
            timeout=60.0 
        )
        
        text_output = ""
        if stdout: 
            raw_output = stdout.decode().strip()
            clean_lines = []
            for line in raw_output.splitlines():
                line = line.strip()
                if "Wolfram Language" in line or "Copyright" in line: continue
                if line == "" or line == ">": continue
                clean_line = re.sub(r"^(In|Out)\[\d+\][:=]+\s*", "", line)
                if clean_line:
                    clean_lines.append(clean_line)
            text_output = "\n".join(clean_lines)

        if stderr: 
            err_msg = stderr.decode().strip()
            if "Wolfram" not in err_msg and "Mathematica" not in err_msg:
                text_output += f"\n[Stderr]: {err_msg}"
        
        image_file = None
        if img_path_abs.exists() and img_path_abs.stat().st_size > 0:
            image_file = img_path_abs
            
        return {"text": "In: "+code+"\nOut: "+text_output, "image": image_file}

    except asyncio.TimeoutError:
        return {"text": "故障: 计算超时", "image": None}
    except Exception as e:
        return {"text": f"故障: {e}", "image": None}

async def send_smart_reply(bot: Bot, event: Event, message: str, image_path: str = None, reply_message_id: int = None):
    """
    智能发送 Pro：支持长文本分段 + 图片合并转发
    用于 Tool Handler
    reply_message_id: 如果提供，会引用该消息
    """
    # 1. 转义 XML 特殊字符
    clean_msg = message.replace("&", "＆").replace("<", "＜").replace(">", "＞")
    
    nodes = []
    
    # 2. 如果文本很短且没图片，直接发送普通消息（省流模式）
    if len(clean_msg) < 100 and not image_path:
        if reply_message_id:
            await bot.send(event, MessageSegment.reply(reply_message_id) + clean_msg)
        else:
            await bot.send(event, clean_msg)
        return

    # 3. 构建文本节点 (每 2000 字切一段)
    chunk_size = 2000
    for i in range(0, len(clean_msg), chunk_size):
        nodes.append({
            "type": "node",
            "data": {
                "name": "普拉娜",
                "uin": str(bot.self_id), # 机器人的 QQ 号
                "content": clean_msg[i:i+chunk_size]
            }
        })
    
    # 4. 如果有图片，追加一个图片节点
    if image_path:
        nodes.append({
            "type": "node",
            "data": {
                "name": "什亭之匣·演算结果", # 给图片节点起个帅气的名字
                "uin": str(bot.self_id),
                "content": MessageSegment.image(image_path)
            }
        })

    # 5. 发送合并转发
    try:
        if isinstance(event, GroupMessageEvent):
            await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=nodes)
        elif isinstance(event, PrivateMessageEvent):
            await bot.call_api("send_private_forward_msg", user_id=event.user_id, messages=nodes)
    except Exception as e:
        # 如果合并转发失败（比如被风控），回退到普通发送
        await bot.send(event, f"发送失败，转纯文本：\n{clean_msg[:200]}...")
        if image_path:
            try: await bot.send(event, MessageSegment.image(image_path))
            except: pass


async def download_file(url: str, user_id: str, filename: str) -> Path:
    """
    下载文件到用户专属目录 data/user_files/{user_id}/
    返回保存路径
    """
    user_dir = USER_FILES_DIR / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    save_path = user_dir / filename
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                save_path.write_bytes(resp.content)
                debug_log("File-DL", f"已保存文件: {save_path} ({len(resp.content)} bytes)")
                return save_path
            else:
                debug_log("File-DL-Err", f"下载失败 HTTP {resp.status_code}")
                return None
    except Exception as e:
        debug_log("File-DL-Err", f"下载文件失败: {e}")
        return None

def get_active_files_context(session_id: str, user_id: str) -> str:
    """
    读取该用户启用的所有文件，拼接为上下文字符串
    """
    active = user_active_files.get(session_id, set())
    if not active:
        return ""
    
    user_dir = USER_FILES_DIR / str(user_id)
    parts = []
    for fname in sorted(active):
        fpath = user_dir / fname
        if fpath.exists():
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
                # 限制单个文件最大 10000 字符
                if len(content) > 10000:
                    content = content[:10000] + "\n...(文件过长，已截断)"
                parts.append(f"【文件上下文: {fname}】\n{content}\n【文件结束】")
            except Exception as e:
                debug_log("File-Read-Err", f"读取 {fname} 失败: {e}")
        else:
            debug_log("File-Missing", f"文件不存在: {fpath}")
    
    return "\n".join(parts)


async def extract_forward_messages(bot: Bot, forward_id: str, depth: int = 0, max_depth: int = 5) -> Dict[str, Any]:
    """
    递归提取合并转发消息的文本内容和图片（支持嵌套转发）
    返回格式: {"text": "格式化的文本", "images": ["url1", "url2", ...]}
    """
    if depth >= max_depth:
        return {"text": "[...嵌套层级过深，已截断...]", "images": []}
    
    try:
        result = await bot.get_forward_msg(id=forward_id)
        # result 通常是 {"messages": [...]} 或直接是 [...]
        messages = result if isinstance(result, list) else result.get("messages", result.get("message", []))
        
        lines = []
        images = []
        for msg in messages:
            # 获取发送者昵称
            sender = msg.get("sender", {}).get("nickname", "未知") if isinstance(msg, dict) else "未知"
            content = msg.get("content", msg.get("message", [])) if isinstance(msg, dict) else []
            
            # content 可能是字符串或段列表
            if isinstance(content, str):
                lines.append(f"[{sender}]: {content}")
                continue
                
            text_parts = []
            for seg in (content if isinstance(content, list) else []):
                seg_type = seg.get("type", "") if isinstance(seg, dict) else ""
                seg_data = seg.get("data", {}) if isinstance(seg, dict) else {}
                
                if seg_type == "text":
                    text_parts.append(seg_data.get("text", ""))
                elif seg_type == "forward":
                    # 嵌套转发，递归提取
                    nested_id = seg_data.get("id", "")
                    if nested_id:
                        nested_result = await extract_forward_messages(bot, nested_id, depth + 1, max_depth)
                        text_parts.append(f"\n--- 嵌套聊天记录 ---\n{nested_result['text']}\n--- 嵌套结束 ---")
                        images.extend(nested_result['images'])
                elif seg_type == "image":
                    url = seg_data.get("url")
                    if url:
                        images.append(url)
                        text_parts.append("[图片]")
                    else:
                        text_parts.append("[图片(无URL)]")
                elif seg_type == "face":
                    text_parts.append("[表情]")
            
            if text_parts:
                lines.append(f"[{sender}]: {''.join(text_parts)}")
        
        return {
            "text": "\n".join(lines) if lines else "[空的聊天记录]",
            "images": images
        }
    except Exception as e:
        debug_log("Forward-Extract-Err", str(e))
        return {"text": f"[提取聊天记录失败: {e}]", "images": []}
