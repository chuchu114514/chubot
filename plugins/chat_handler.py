from nonebot import on_message
from nonebot.adapters.onebot.v11 import Message, Bot, Event, GroupMessageEvent, PrivateMessageEvent, MessageSegment
from typing import List, Dict, Callable, Optional
import asyncio
#import json
import traceback
import random
import re
import os
import time
import math
from .common import *
# from .labeler import get_message_label, calculate_convolution_score

# === 消息批处理缓冲区 ===
# 结构: { session_id: { "msgs": [...], "timer": asyncio.Task, "bot": Bot, "event": Event } }
_pm_buffer: Dict[str, dict] = {}
_group_buffer: Dict[str, dict] = {}
_group_last_reply: Dict[str, float] = {}  # 记录每个群上次回复的时间戳

# 触发条件：第一条之后再收到 N 条，或等待 T 秒
_PM_BATCH_COUNT = 5   
_PM_BATCH_TIMEOUT = 10.0

_GROUP_BATCH_COUNT = 5
_GROUP_BATCH_TIMEOUT = 10.0

# priority=15, block=True
# 只有 tool_handler 不处理的消息才会到这里
chat_handler = on_message(priority=15, block=True)

def is_bot_mentioned(event: Event, raw_msg: str) -> bool:
    """判断是否提到了机器人（名字或@）"""
    return event.is_tome() or any(name in raw_msg.lower() for name in ["plana", "普拉娜", "什亭", "匣子"])

@chat_handler.handle()
async def _(bot: Bot, event: Event):
    raw_msg = event.get_plaintext().strip()
    session_id = get_session_id(event)
    debug_log("Flow", f"Chat Handler: Received msg from {session_id}. Raw: {raw_msg[:20]}")

    # === 离散卷积回复意愿逻辑 (全局评估) - 已移除 ===
    # media_types = {"image", "mface", "sticker", "face", "video", "record", "flash"}
    # has_media = any(s.type in media_types for s in event.message)
    # 
    # if not (has_media or len(raw_msg) < 1):
    #     label = await get_message_label(raw_msg)
    #     weight = 10 ** label
    #     if session_id not in message_queues:
    #         message_queues[session_id] = deque()
    #     message_queues[session_id].append((weight, time.time()))
    #     if len(message_queues[session_id]) > 50:
    #         message_queues[session_id].popleft()

    # score = calculate_convolution_score(session_id)
    # debug_log("Convolution", f"Session: {session_id}, Score: {score:.2f}, Msg: {raw_msg[:20]}")
    
    # === 私聊文件接收 ===
    if isinstance(event, PrivateMessageEvent):
        for seg in event.message:
            if seg.type == "file":
                file_data = seg.data
                # filename 可能在 "name" 或 "file" 字段
                filename = file_data.get("name") or file_data.get("file", "unknown")
                file_id = file_data.get("file_id") or file_data.get("id", "")
                debug_log("File-Recv", f"收到文件: {filename}, file_id: {file_id}, data: {file_data}")
                
                if not (filename.lower().endswith(".txt") or filename.lower().endswith(".py")):
                    await bot.send(event, f"老师，请确认您的文件是txt或py格式。")
                    return
                
                # 成功提示
                ok_msg = f"已接收文件: {filename}\n使用 /{filename} 可启用/禁用该文件的上下文\n使用 /文件列表 查看所有文件"
                user_dir = USER_FILES_DIR / str(event.user_id)
                user_dir.mkdir(parents=True, exist_ok=True)
                save_path = user_dir / filename
                
                # 尝试通过 get_file API 获取文件
                if file_id:
                    try:
                        file_info = await bot.call_api("get_file", file_id=file_id)
                        debug_log("File-API", f"get_file 返回: {file_info}")
                        
                        # 优先使用本地路径
                        local_path = file_info.get("file", "")
                        if local_path and os.path.exists(local_path):
                            import shutil
                            shutil.copy2(local_path, save_path)
                            debug_log("File-Copy", f"从本地路径复制: {local_path} -> {save_path}")
                            await bot.send(event, MessageSegment.reply(event.message_id) + ok_msg)
                            return
                        
                        # 其次使用 URL 下载
                        file_url = file_info.get("url", "")
                        if file_url and file_url.startswith("http"):
                            result = await download_file(file_url, str(event.user_id), filename)
                            if result:
                                await bot.send(event, MessageSegment.reply(event.message_id) + ok_msg)
                            else:
                                await bot.send(event, "文件下载失败...")
                            return
                        
                        # 尝试 base64
                        b64_data = file_info.get("base64", "")
                        if b64_data:
                            import base64 as b64mod
                            save_path.write_bytes(b64mod.b64decode(b64_data))
                            debug_log("File-B64", f"从base64保存文件: {save_path}")
                            await bot.send(event, MessageSegment.reply(event.message_id) + ok_msg)
                            return
                            
                    except Exception as e:
                        debug_log("File-API-Err", f"get_file 失败: {e}")
                
                # 最后尝试 seg.data 里的 url
                file_url = file_data.get("url", "")
                if file_url and file_url.startswith("http"):
                    result = await download_file(file_url, str(event.user_id), filename)
                    if result:
                        await bot.send(event, MessageSegment.reply(event.message_id) + ok_msg)
                    else:
                        await bot.send(event, "文件下载失败...")
                else:
                    await bot.send(event, "无法获取文件下载地址，请检查日志...")
                return
    
    # === 私聊斜杠命令 (文件管理) ===
    if isinstance(event, PrivateMessageEvent) and raw_msg.startswith("/"):
        cmd = raw_msg[1:].strip()
        
        if cmd == "文件列表":
            user_dir = USER_FILES_DIR / str(event.user_id)
            if not user_dir.exists() or not any(user_dir.iterdir()):
                await bot.send(event, "老师，您还没有发送过文件。")
                return
            
            active = user_active_files.get(session_id, set())
            lines = []
            for f in sorted(user_dir.iterdir()):
                if f.is_file():
                    status = "✅ 已启用" if f.name in active else "⬜ 未启用"
                    size_kb = f.stat().st_size / 1024
                    lines.append(f"{status} {f.name} ({size_kb:.1f}KB)")
            
            await bot.send(event, "📁 文件列表：\n" + "\n".join(lines) + "\n\n使用 /文件名 切换启用状态")
            return
        
        # /文件名.txt — 切换文件启用/禁用
        if cmd:
            user_dir = USER_FILES_DIR / str(event.user_id)
            target_file = user_dir / cmd
            if target_file.exists() and target_file.is_file():
                if session_id not in user_active_files:
                    user_active_files[session_id] = set()
                
                if cmd in user_active_files[session_id]:
                    user_active_files[session_id].discard(cmd)
                    await bot.send(event, f"已禁用文件上下文: {cmd}")
                else:
                    user_active_files[session_id].add(cmd)
                    await bot.send(event, f"已启用文件上下文: {cmd}\n后续对话将包含该文件内容。")
                return
            # 如果文件不存在，不拦截，让消息继续走正常聊天流程
    
    # === 触发条件检查 ===
    is_mentioned = is_bot_mentioned(event, raw_msg)
    should_reply = is_mentioned or isinstance(event, PrivateMessageEvent)
    
    # 特殊规则：忽略特定用户的强制触发 (3042548976)
    if event.user_id == 3042548976:
        debug_log("Flow", f"User 3042548976 triggered, but ignoring as per request.")
        should_reply = False

    debug_log("Flow", f"should_reply={should_reply} (mentioned={is_mentioned})")
    
    if not should_reply:
        # 非特定群组且没有 @ 机器人，记录到背景音缓冲区
        if isinstance(event, GroupMessageEvent):
            sid = get_session_id(event)
            debug_log("Flow", f"非目标群且未艾特，入背景缓冲区: {sid}")
            if sid not in passive_buffer:
                passive_buffer[sid] = deque(maxlen=PASSIVE_BUFFER_SIZE)
            
            sender_name = event.sender.card or event.sender.nickname or str(event.user_id)
            msg_text = event.get_plaintext().strip()
            if msg_text:
                passive_buffer[sid].append(f"[{sender_name}]: {msg_text}")
        return

    # === 私聊消息批处理 ===
    # # 开头的指令消息不进入批处理缓冲区，直接处理
    if isinstance(event, PrivateMessageEvent) and not raw_msg.startswith("#"):
        sid = get_session_id(event)
        if sid in _pm_buffer:
            # 已有缓冲区：追加消息
            buf = _pm_buffer[sid]
            buf["msgs"].append((bot, event))
            debug_log("PM-Batch", f"缓冲区追加第 {len(buf['msgs'])} 条消息，session={sid}")
            # 达到数量阈值、或提到了机器人时立即触发
            if len(buf["msgs"]) >= _PM_BATCH_COUNT or is_mentioned:
                debug_log("PM-Batch", f"触发条件达成 (Mention={is_mentioned}), 立即触发批处理")
                if not buf["timer"].done():
                    buf["timer"].cancel()
                asyncio.create_task(_flush_pm_buffer(sid))
            return
        else:
            # 第一条消息：建立缓冲区，启动定时器，不回复
            debug_log("PM-Batch", f"收到第一条消息，建立缓冲区，session={sid}")
            timer_task = asyncio.create_task(_pm_batch_timer(sid))
            _pm_buffer[sid] = {
                "msgs": [(bot, event)],
                "timer": timer_task,
            }
            return

    session_id = get_session_id(event)
    
    # 初始化
    if session_id not in user_sessions: 
        # 注意这里用了 Chat 专用 prompt，里面有关于 <sep> 的 instruction
        user_sessions[session_id] = [system_prompt(is_chat_mode=True)] 
    if session_id not in user_configs: 
        user_configs[session_id] = {"model": MODEL_SMART}
        
    history = user_sessions[session_id]

    # 非私聊（群聊 @机器人）直接走正常流程，不进入批处理

    # === 构建上下文 ===
    # 提取引用和图片
    current_imgs = [s.data["url"] for s in event.message if s.type == "image"]
    reply_txt = ""
    reply_imgs = []
    forward_imgs = []
    
    if event.reply:
        debug_log("Reply-Debug", f"Found reply message, segments: {len(event.reply.message)}")
        reply_txt = f"【引用】：{event.reply.message.extract_plain_text()}"
        for seg in event.reply.message:
            debug_log("Reply-Seg", f"Segment type: {seg.type}, data: {seg.data}")
            if seg.type == "image":
                url = seg.data.get("url")
                debug_log("Reply-Image", f"Found image URL: {url}")
                if url: reply_imgs.append(url)
            elif seg.type == "forward":
                # 引用的消息是转发消息，递归提取其内容
                fwd_id = seg.data.get("id", "")
                if fwd_id:
                    fwd_result = await extract_forward_messages(bot, fwd_id)
                    reply_txt += f"\n【引用的聊天记录】：\n{fwd_result['text']}\n【引用的聊天记录结束】\n"
                    reply_imgs.extend(fwd_result['images'])
                    debug_log("Reply-Forward", f"Extracted {len(fwd_result['images'])} images from forward")
        debug_log("Reply-Summary", f"Total reply images: {len(reply_imgs)}")

    # === 提取转发消息（聊天记录）===
    forward_txt = ""
    for seg in event.message:
        if seg.type == "forward":
            fwd_id = seg.data.get("id", "")
            if fwd_id:
                fwd_result = await extract_forward_messages(bot, fwd_id)
                forward_txt += f"\n【聊天记录】：\n{fwd_result['text']}\n【聊天记录结束】\n"
                forward_imgs.extend(fwd_result['images'])

    # 构造当前用户消息
    sender_name = "未知用户"
    if True:#isinstance(event, GroupMessageEvent):
        sender_name = event.sender.card or event.sender.nickname or str(event.user_id)
        if event.user_id == ADMIN_QQ: prefix = f"<now_time:>{datetime.now()}</now_time><admin>{sender_name}老师</admin>"
        else: prefix = f"<now_time:>{datetime.now()}</now_time><user>{sender_name}</user>"
    #else:
    #    sender_name = event.sender.nickname or "老师"
    #    prefix = f"【<now_time:>{datetime.now()}</now_time><老师>{sender_name}老师</老师>】"

    msg_id = getattr(event, 'message_id', 'unknown')
    full_txt = f"<request_by>{sender_name}</request_by> [ID:{msg_id}]" + prefix + ("说：" if not reply_txt else "回复：") + reply_txt + raw_msg + forward_txt
    
    user_content = []
    if full_txt: user_content.append({"type": "text", "text": full_txt})
    all_img_urls = reply_imgs + forward_imgs + current_imgs
    debug_log("Image-Summary", f"reply_imgs: {len(reply_imgs)}, forward_imgs: {len(forward_imgs)}, current_imgs: {len(current_imgs)}")
    if all_img_urls:
        img_contents = await make_image_content(all_img_urls)
        user_content.extend(img_contents)
        debug_log("Image-B64", f"成功转换 {len(img_contents)}/{len(all_img_urls)} 张图片为base64")

    if not user_content: return 

    final_msg = {
        "role": "user", 
        "content": user_content if (len(user_content) > 1 or user_content[0]["type"] != "text") else user_content[0]["text"]
    }
    debug_log("Final-Message", f"Message structure: role={final_msg['role']}, content_type={type(final_msg['content'])}, content_items={len(user_content)}")
    if isinstance(final_msg['content'], list):
        for i, item in enumerate(final_msg['content']):
            debug_log("Final-Message-Item", f"Item {i}: type={item.get('type')}, has_url={'image_url' in item}")
    history.append(final_msg)

    # === 注入成员列表 (用于 @提及参考，追加在 final_msg 之后) ===
    recent_members = {}
    if isinstance(event, GroupMessageEvent):
        gid = str(event.group_id)
        sid = f"group_{gid}"
        if sid in passive_buffer:
            for entry in list(passive_buffer[sid])[-20:]:
                m = re.match(r"\[(.*?)]: .*", entry)
                if m: recent_members[m.group(1)] = "?"  # 背景音没 QQ 号
        recent_members[sender_name] = str(event.user_id)
    
    members_str = "\n".join([f"- {name}" + (f" (QQ: {qq})" if qq != "?" else "") for name, qq in recent_members.items()])
    if members_str:
        history.append({"role": "system", "content": f"【当前群内活跃成员参考列表】:\n{members_str}\n\n你可以使用 <AT:QQ号> 来 @ 他们。"})
    
    await _do_reply(bot, event, session_id, history)


async def _pm_batch_timer(sid: str):
    """私聊批处理定时器：等待超时后触发"""
    try:
        await asyncio.sleep(_PM_BATCH_TIMEOUT)
        debug_log("PM-Batch", f"超时触发批处理，session={sid}")
        await _flush_pm_buffer(sid)
    except asyncio.CancelledError:
        pass  # 被数量阈值取消，正常


async def _group_batch_timer(gid: str):
    """群聊批处理定时器：等待动态截止时间到达"""
    try:
        while gid in _group_buffer:
            buf = _group_buffer[gid]
            now = time.time()
            remaining = buf["deadline"] - now
            if remaining <= 0:
                break
            # 每 0.5s 检查一次 deadline，因为 deadline 会动态减小
            await asyncio.sleep(min(remaining, 0.5))
            
        if gid in _group_buffer:
            debug_log("Group-Batch", f"动态计时器超时/归零，触发批处理，group={gid}")
            await _flush_group_buffer(gid)
    except asyncio.CancelledError:
        pass


async def _flush_group_buffer(gid: str):
    """处理群聊批量消息：根据卷积分数决定回复后，合并消息并回复"""
    if gid not in _group_buffer:
        return
    buf = _group_buffer.pop(gid)
    msgs = buf["msgs"]
    if not msgs:
        return

    # 取最后一条消息的 bot/event 作为回复目标
    bot, last_event = msgs[-1]
    session_id = f"group_{gid}"

    # 速率上限检查
    now = time.time()
    last_time = last_reply_time.get(session_id, 0)
    if now - last_time < MIN_REPLY_INTERVAL:
        debug_log("Rate-Limit", f"Session {session_id} (群聊批处理) 处于冷却中，跳过回复")
        return

    debug_log("Group-Batch", f"开始处理批量群聊消息，共 {len(msgs)} 条，group={gid}")

    # 构建合并后的文本 (包含消息 ID 用于引用)
    combined_parts = []
    for b, ev in msgs:
        sender_name = ev.sender.card or ev.sender.nickname or str(ev.user_id)
        raw = ev.get_plaintext().strip()
        msg_id = getattr(ev, 'message_id', 'unknown')
        part_text = f"[ID:{msg_id}] [{sender_name}]: {raw}" if raw else f"[ID:{msg_id}] [{sender_name}]: [图片/多媒体]"
        combined_parts.append(part_text)
    
    if not combined_parts:
        return

    combined_text = "\n".join(combined_parts)
    
    # 初始化 session
    if session_id not in user_sessions:
        user_sessions[session_id] = [system_prompt(is_chat_mode=True)]
    if session_id not in user_configs:
        user_configs[session_id] = {"model": MODEL_SMART}

    history = list(user_sessions[session_id])
    
    bg_list = list(passive_buffer.get(session_id, []))
    bg_context_msg = _build_group_bg_context_msg(session_id, limit=20)
    if bg_context_msg:
        history.append(bg_context_msg)

    # 获取最近成员列表（用于 @ 别人）
    recent_members = {} # {qq: name}
    for b, ev in msgs:
        recent_members[str(ev.user_id)] = ev.sender.card or ev.sender.nickname or str(ev.user_id)
    
    # 从背景音中也提取一些
    for entry in bg_list[-10:]:
        m = re.match(r"\[(.*?)]: .*", entry)
        if m:
            # 背景音里没 QQ，只能靠名字
            pass

    members_str = "\n".join([f"- {name} (QQ: {qq})" for qq, name in recent_members.items()])
    members_info = f"【当前对话成员列表（供@提及参考）】:\n{members_str}\n\n"

    # 直接让它根据上下文回复
    full_prompt = (
        f"{members_info}"
        "【当前群聊批量消息】\n"
        f"{combined_text}\n\n"
        "任务：你现在是群聊的一员（普拉娜），请根据上述对话内容给出你的【简短回复】。\n"
        "格式要求（必须遵守）：\n"
        "1. 不要带任何前缀，直接输出回复内容。\n"
        "2. 如需分多句，用 [SEP] 分隔，不超过 3 句。\n"
        "3. 可以使用 <AT:QQ号> 来 @ 某人; 不想引用消息则加 <REF:NO>。"
    )
    
    history.append({"role": "user", "content": full_prompt})
    
    try:
        model_id = get_user_model(session_id)
        debug_log("Group-Batch", f"正在请求 LLM (model={model_id}) 进行回复...")
        final_resp = await query_llm(history, model_id=model_id, max_tokens=2000)
        final_resp = final_resp.strip()
        
        # 记录进历史 (存入合并后的消息作为 user 消息)
        user_sessions[session_id].append({"role": "user", "content": combined_text})
        
        # 使用统一的分段发送逻辑 (这里不使用引用回复，因为是批量内容)
        await _process_and_send_segments(bot, last_event, session_id, final_resp, use_reply=False)
        
        if len(user_sessions[session_id]) > MAX_HISTORY_LENGTH:
            user_sessions[session_id] = await compress_history(user_sessions[session_id])

    except Exception as e:
        debug_log("Group-Batch-Err", str(e))


async def _flush_pm_buffer(sid: str):
    """将缓冲区中的所有消息合并为一次 LLM 请求"""
    if sid not in _pm_buffer:
        return
    buf = _pm_buffer.pop(sid)
    msgs = buf["msgs"]
    if not msgs:
        return

    # 取最后一条消息的 bot/event 作为回复目标（引用最后一条）
    bot, last_event = msgs[-1]
    session_id = sid

    debug_log("PM-Batch", f"开始处理批量消息，共 {len(msgs)} 条，session={sid}")

    # 初始化 session
    if session_id not in user_sessions:
        user_sessions[session_id] = [system_prompt(is_chat_mode=True)]
    if session_id not in user_configs:
        user_configs[session_id] = {"model": MODEL_SMART}

    history = user_sessions[session_id]

    # 将所有缓冲消息逐条构建 user_content，合并为一条消息
    combined_parts = []
    all_img_urls = []

    for i, (b, ev) in enumerate(msgs):
        raw = ev.get_plaintext().strip()
        current_imgs = [s.data["url"] for s in ev.message if s.type == "image"]
        reply_txt = ""
        reply_imgs = []

        if ev.reply:
            reply_txt = f"【引用】：{ev.reply.message.extract_plain_text()}"
            for seg in ev.reply.message:
                if seg.type == "image":
                    url = seg.data.get("url")
                    if url: reply_imgs.append(url)

        sender_name = ev.sender.card or ev.sender.nickname or str(ev.user_id)
        if ev.user_id == ADMIN_QQ:
            prefix = f"<now_time:>{datetime.now()}</now_time><admin>{sender_name}老师</admin>"
        else:
            prefix = f"<now_time:>{datetime.now()}</now_time><user>{sender_name}</user>"

        msg_id = getattr(ev, 'message_id', 'unknown')
        full_txt = f"<request_by>{sender_name}</request_by>" + f"[ID:{msg_id}] [{label}] " + prefix + ("说：" if not reply_txt else "回复：") + reply_txt + raw
        combined_parts.append(full_txt)
        all_img_urls.extend(reply_imgs)
        all_img_urls.extend(current_imgs)

    # 获取最近成员列表
    recent_members = {}
    for i, (b, ev) in enumerate(msgs):
        recent_members[str(ev.user_id)] = ev.sender.nickname or "老师"
    
    members_str = "\n".join([f"- {name} (QQ: {qq})" for qq, name in recent_members.items()])
    members_info = f"【当前对话成员列表】:\n{members_str}\n\n"

    combined_text = "\n".join(combined_parts)
    user_content = [{"type": "text", "text": members_info + combined_text}]
    if all_img_urls:
        img_contents = await make_image_content(all_img_urls)
        user_content.extend(img_contents)

    final_msg = {
        "role": "user",
        "content": user_content if (len(user_content) > 1 or user_content[0]["type"] != "text") else user_content[0]["text"]
    }
    history.append(final_msg)

    await _do_reply(bot, last_event, session_id, history)


async def _do_reply(bot: Bot, event: Event, session_id: str, history: list):
    """核心回复逻辑（群聊使用锁串行化请求，防止并发混淆上下文）"""
    # 速率上限检查 (全局冷却)
    now = time.time()
    last_time = last_reply_time.get(session_id, 0)
    if now - last_time < MIN_REPLY_INTERVAL:
        debug_log("Rate-Limit", f"Session {session_id} 处于全局冷却中，跳过回复 (剩余 {MIN_REPLY_INTERVAL - (now - last_time):.1f}s)")
        return

    # 速率上限检查 (滑动窗口: 30s 内至多 2 次请求)
    if session_id not in request_history:
        request_history[session_id] = deque()
    
    # 清理 30s 之前的记录
    while request_history[session_id] and now - request_history[session_id][0] > 30.0:
        request_history[session_id].popleft()
    
    if len(request_history[session_id]) >= 2:
        debug_log("Rate-Limit", f"Session {session_id} 触发 30s/2次 限制，普拉娜繁忙。历史: {[round(t-now, 1) for t in request_history[session_id]]}")
        return

    # 记录本次请求时间
    request_history[session_id].append(now)

    if isinstance(event, GroupMessageEvent):
        lock = get_group_lock(str(event.group_id))
        debug_log("Group-Lock", f"Waiting for lock, group={event.group_id}")
        async with lock:
            debug_log("Group-Lock", f"Lock acquired, group={event.group_id}")
            await _do_reply_inner(bot, event, session_id, history)
    else:
        await _do_reply_inner(bot, event, session_id, history)


async def _do_reply_inner(bot: Bot, event: Event, session_id: str, history: list):
    """实际回复逻辑"""
    try:
        # === 核心区别：字数限制 ===
        # max_tokens 设小一点，或者通过 prompt 约束
        # 并且如果是群聊的前几句，带上背景
        request_msgs = list(history)
        if isinstance(event, GroupMessageEvent):
             bg_msg = _build_group_bg_context_msg(session_id, limit=20)
             if bg_msg:
                 # 将背景音稳定插入到“最后一条 user 消息”之前，确保其作为请求上下文生效
                 insert_pos = len(request_msgs)
                 for idx in range(len(request_msgs) - 1, -1, -1):
                     if request_msgs[idx].get("role") == "user":
                         insert_pos = idx
                         break
                 request_msgs.insert(max(insert_pos, 1), bg_msg)

        # === 注入文件上下文 ===
        if isinstance(event, PrivateMessageEvent):
            files_ctx = get_active_files_context(session_id, str(event.user_id))
            if files_ctx:
                request_msgs.insert(1, {"role": "system", "content": f"以下是老师启用的参考文件内容：\n{files_ctx}"})
                debug_log("File-Context", f"已注入 {len(user_active_files.get(session_id, set()))} 个文件上下文")

        # 这里 max_tokens=300 太短了，改为 2000
        final_resp = await query_llm(request_msgs, model_id=get_user_model(session_id), max_tokens=20000)
        debug_log("Chat-Segmentation", f"Raw LLM Output: {final_resp}")

        # === 使用统一的处理与发送逻辑 ===
        await _process_and_send_segments(bot, event, session_id, final_resp, use_reply=True)
                
        if len(user_sessions[session_id]) > MAX_HISTORY_LENGTH:
            user_sessions[session_id] = await compress_history(user_sessions[session_id])

        # 回复完成后，为私聊建立新的批处理缓冲区入口（等待下一条第一条）
        # 通过清除 _pm_buffer 中的 sid（如果有残留）确保下次重新计数
        _pm_buffer.pop(session_id, None)

    except Exception as e:
        traceback.print_exc()
        await bot.send(event, "（普拉娜似乎被绊倒了...）")
        _pm_buffer.pop(session_id, None)


def _build_group_bg_context_msg(session_id: str, limit: int = 20) -> Optional[Dict]:
    """构造群聊背景音上下文消息。"""
    bg_list = list(passive_buffer.get(session_id, []))
    if not bg_list:
        return None

    bg_recent = bg_list[-limit:]
    return {
        "role": "system",
        "content": "【群聊背景音（最近20条你未参与的消息，仅作参考）】：\n" + "\n".join(bg_recent),
    }


async def _process_and_send_segments(bot: Bot, event: Event, session_id: str, final_resp: str, use_reply: bool = False):
    """提取标签处理和分段发送的逻辑，复用于普通回复和批量回复"""
    # === 解析是否取消回复 ===
    if "<NO_REPLY>" in final_resp:
        debug_log("Chat-Reply", "LLM 输出 <NO_REPLY>，取消本次发送。")
        return

    # === 解析回复引用控制标签 ===
    # 优先解析 <REF:ID>，如果存在则覆盖 use_reply 并指定回复目标
    ref_match = re.search(r"<REF:(\d+)>", final_resp)
    target_msg_id = event.message_id
    if ref_match:
        use_reply = True
        try:
            target_msg_id = int(ref_match.group(1))
        except ValueError:
            pass
        final_resp = re.sub(r"<REF:\d+>", "", final_resp).strip()
    elif "<REF:NO>" in final_resp:
        use_reply = False
        final_resp = final_resp.replace("<REF:NO>", "").strip()

    # === 分段发送 ===
    segments = final_resp.split("[SEP]")
    debug_log("Chat-Segmentation", f"Split into {len(segments)} segments.")
    
    if session_id in user_sessions:
        user_sessions[session_id].append({"role": "assistant", "content": final_resp.replace("[SEP]", " ")})
    
    if session_id.startswith("group_"):
        _group_last_reply[session_id.replace("group_", "")] = time.time()
    
    # 获取当前群员列表（用于名称到 QQ 的映射）
    member_map = {} # {name: qq}
    if isinstance(event, GroupMessageEvent):
        gid = str(event.group_id)
        # 从最近的历史/缓存中尝试获取映射（这里简单从当前 event 和 passive_buffer 提取）
        # 实际更完善的做法是缓存所有见过的人，这里先做简单的
        for sid, buffer in passive_buffer.items():
            if sid == f"group_{gid}":
                for entry in buffer:
                    # 匹配格式 "[Name]: Msg"
                    m = re.match(r"\[(.*?)]: .*", entry)
                    if m:
                        # 这是一个简化的映射逻辑，真实场景可能需要更精确
                        pass

    for i, seg in enumerate(segments):
        seg = seg.strip()
        if not seg: continue
        
        # 解析 @ 标签: <AT:QQ> 或 <AT:名字>
        # 将分段内部再次拆分为 文本 + AT + 文本
        msg_parts = Message()
        current_text = seg
        
        # 循环解析所有 <AT:...>
        while True:
            at_match = re.search(r"<AT:(.*?)>", current_text)
            if not at_match:
                if current_text: msg_parts.append(current_text)
                break
            
            # 添加之前的文本
            prev_text = current_text[:at_match.start()]
            if prev_text: msg_parts.append(prev_text)
            
            target = at_match.group(1).strip()
            if target.isdigit():
                msg_parts.append(MessageSegment.at(target))
            else:
                # TODO: 尝试通过名字找 QQ，找不到就保留原样
                msg_parts.append(f"@{target} ")
            
            current_text = current_text[at_match.end():]

        # 发送处理后的消息
        # 如果是第一段且没有显式 AT，且 default 需要 AT，则补上
        has_at = any(s.type == "at" for s in msg_parts)
        
        if i == 0:
            final_segment = Message()
            if use_reply:
                final_segment.append(MessageSegment.reply(target_msg_id))
            
            # 默认补 @ 逻辑：
            # 1. 是群聊
            # 2. 消息中没有显式 AT 标签
            # 3. 如果是引用回复，通常由 OneBot 段补 @，但这里我们根据需求：
            #    如果没有显式 <AT:...> 标签，且不是 <REF:ID>，则不自动 @ 任何人。
            #    为了兼容性，如果 use_reply 为 True 且是 event.message_id (即默认引用)，保留 @。
            #    如果 AI 指定了 <REF:ID>，我们只引用，由 AI 自己决定是否加 <AT:...>。
            
            if not has_at and isinstance(event, GroupMessageEvent) and use_reply and target_msg_id == event.message_id:
                final_segment.append(MessageSegment.at(event.user_id))
                final_segment.append(" ")
            
            final_segment += msg_parts
            await bot.send(event, final_segment)
        else:
            await bot.send(event, msg_parts)
        
        if i < len(segments) - 1:
            await asyncio.sleep(random.uniform(0.5, 1.5))
            
    # 记录最后一次发送完成的时间
    last_reply_time[session_id] = time.time()

    if session_id in message_queues:
        # message_queues[session_id].clear()
        # debug_log("Convolution", f"Cleared queue for session {session_id}")
        pass
