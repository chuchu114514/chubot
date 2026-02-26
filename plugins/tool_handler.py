from nonebot import on_message, on_command
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, PrivateMessageEvent, MessageSegment
from typing import List, Dict, Callable, Optional
import re
import asyncio
import traceback
import uuid
from nonebot.exception import FinishedException, StopPropagation
from .common import *

# 注册为 priority=10, block=False (避免 skip 失效，手动控制 finish)
tool_handler = on_message(priority=10, block=False)

# 辅助函数
async def analyze_task_complexity(prompt: str) -> int:
    """
    [决策层] 分析任务复杂度，返回建议的并发核心数 (2~20)
    """
    decision_prompt = (
        f"任务：'{prompt}'\n"
        "判断：为了全面分析该问题，需要几个不同的视角（核心数）？\n"
        "规则：\n"
        "1. 简单问题（如日常问候、简单查询）：返回 5以内的数\n"
        "2. 中等问题（如代码解释、剧情回顾）：返回 10以内的数字\n"
        "3. 复杂问题（如哲学探讨、长篇创作、多角度分析）：返回 20以内的整数\n"
        "4. JSON格式：{\"reason\": \"...\", \"count\": 3}\n"
        "5. Count 必须在 2 到 20 之间。\n"
    )
    
    try:
        # 用最快最便宜的模型做决策
        res = await get_json_decision(decision_prompt, model_id=MODEL_INSTANT)
        count = int(res.get("count", 3))
        # 限制范围，防止模型发癫开100个核把API刷爆
        return max(2, min(count, 20))
    except:
        return 3 # 默认 fallback

async def execute_cluster_workflow(send_temp_func: Callable, user_prompt: str, cluster_size: int, history: List[Dict], use_pro: bool = False) -> str:
    """[并行模式]"""
    worker_model = MODEL_SMART if use_pro else MODEL_FAST
    mode_name = "Pro全功率" if use_pro else "常规"
    
    plan_prompt = (
        f"任务：'{user_prompt}'\n"
        f"目标：将该任务拆解为 {cluster_size} 个独立的子视角或子任务。\n"
        "要求：输出一个纯 JSON 字符串列表（List[str]）。不要 key-value 对象。\n"
        "\n"
        "【范例】\n"
        "任务：评价《原神》的剧情\n"
        "输出：[\"现在你是一个来自米哈游的来自米哈游的专业的游戏设计师，为了帮我理解《原神》的剧情，从世界观设定角度分析原神剧情并生成报告\", \"现在你是一个来自米哈游的专业的角色形象分析家，为了帮我理解《原神》的剧情，请分析原神角色的塑造和成长\", \"现在你是一个来自米哈游的专业的小说家兼导演，为了帮我理解《原神》的剧情，请探讨原神剧情中的叙事节奏问题\"]\n"
        "\n"
        "请输出："
    )
    try:
        prompts = await get_json_decision(plan_prompt, model_id=MODEL_SMART)
        if not isinstance(prompts, list): prompts = [user_prompt] * cluster_size
    except: prompts = [user_prompt] * cluster_size
    prompts = prompts[:cluster_size]

    await send_temp_func(f" [什亭之匣-{mode_name}] 启动 {len(prompts)} 核心并发...")
    tasks = [query_llm([{"role": "user", "content": p}], temperature=0.7, model_id=worker_model) for p in prompts]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    summary = "=== 运算结果 ===\n" + "\n".join([f"【核心{i}】{r}" for i, r in enumerate(results)])
    summary += "\n请**保持普拉娜性格**，汇总回答。"
    return await query_llm(history + [{"role": "user", "content": summary}], model_id=MODEL_SMART)

async def execute_beam_workflow(send_temp_func: Callable, prompt: str, budget: int, depth: int = 0, use_pro: bool = False) -> str:
    """[束模式]"""
    model = MODEL_SMART if (depth == 0 or use_pro) else MODEL_FAST
    if budget <= 0 or depth >= 3: return await query_llm([{"role": "user", "content": prompt}], model_id=model)
    decision_prompt = (
        f"任务：'{prompt}'\n"
        f"剩余算力：{budget}\n"
        "判断：\n"
        "1. 如果任务可以直接回答，action 填 'solve'。\n"
        "2. 如果任务太大需要拆解，action 填 'delegate'，并在 sub_tasks 里列出子任务。\n"
        "\n"
        "【范例1】\n"
        "任务：1+1等于几\n"
        "JSON：{\"action\": \"solve\", \"sub_tasks\": []}\n"
        "\n"
        "【范例2】\n"
        "任务：写一份完整的游戏策划案，主题是xx\n"
        "JSON：{\"action\": \"delegate\", \"sub_tasks\": [\"现在你是一个专业的游戏策划兼游戏设计师，请以xx为主题，设计游戏核心玩法机制\", \"现在你是一个专业的小说家兼游戏策划，请以xx为主题，编写游戏世界观和剧情大纲\", \"现在你是一个专业的游戏策划兼游戏设计师，请以xx为主题，设计主要角色和技能系统\"]}\n"
        "\n"
        "请输出 JSON："
    )
    decision = await get_json_decision(decision_prompt, model_id=model)
    
    if decision.get("action") == "delegate":
        tasks = decision.get("sub_tasks", [])[:budget]
        if not tasks: return await query_llm([{"role": "user", "content": prompt}], model_id=model)
        
        await send_temp_func(f" [逻辑推演 Lv.{depth}] 解析 {len(tasks)} 子任务...")
        results = await asyncio.gather(*[execute_beam_workflow(send_temp_func, t, budget-1, depth+1, use_pro) for t in tasks], return_exceptions=True)
        return await query_llm([{"role": "user", "content": f"子任务结果：{results}\n请汇总。"}], model_id=model)
    
    return await query_llm([{"role": "user", "content": prompt}], model_id=model)


def is_sensitive_wolfram(code: str) -> bool:
    """检测是否包含敏感的文件/系统操作"""
    # 简单的关键词黑名单
    # 注意：RunProcess, Run, Import, Export, File..., Open..., DeleteFile, Rename...
    sensitive_keywords = [
        "Import", "Export", "OpenRead", "OpenWrite", "OpenAppend",
        "FilePrint", "ReadList", "ReadString", "BinaryRead",
        "DeleteFile", "RenameFile", "CopyFile", "CreateDirectory", "DeleteDirectory",
        "Run", "RunProcess", "SystemOpen", "SetDirectory", "ResetDirectory",
        "Put", "Get", "DumpSave", "Save"
    ]
    for kw in sensitive_keywords:
        # 匹配 whole word (Case sensitive in WL usually, but let's be strict)
        if re.search(r"\b" + re.escape(kw) + r"\b", code):
            return True
    return False

@tool_handler.handle()
async def _(bot: Bot, event: Event):
    raw_msg = event.get_plaintext().strip()
    session_id = get_session_id(event)
    
    # 临时消息回调
    temp_ids = []
    async def send_temp(txt):
        try:
            r = await tool_handler.send(txt)
            if isinstance(r, dict): temp_ids.append(r['message_id'])
        except: pass

    # === 0. #图片 录制模式及指令 ===
    if raw_msg == "#图片":
        if session_id in recording_state:
            # 结束录制
            msgs = recording_state.pop(session_id)
            if session_id not in user_sessions: user_sessions[session_id] = [system_prompt()]
            user_sessions[session_id].extend(msgs)
            await bot.send(event, f"已接收 {len(msgs)} 条图文数据。正在解析...")
            # 构造触发器，这里不return，往下走逻辑（或者重新触发）
            # 为了简单，我们手动触发一次 mock 的 msg
            raw_msg = "【录制结束分析】" 
        else:
            # 开始录制
            recording_state[session_id] = []
            await bot.send(event, " 视觉模块开启，老师。我会监听您的图片内容及需要的命令。再次输入 #图片 以结束。")
            return # 阻断

    # 录制中：拦截所有消息并存入
    if session_id in recording_state:
        # 获取图片
        current_imgs = [s.data["url"] for s in event.message if s.type == "image"]
        # 获取引用
        reply_txt = ""
        if event.reply:
            reply_txt = f"【引用】：{event.reply.message.extract_plain_text()}"
        
        content = []
        if reply_txt + raw_msg: content.append({"type": "text", "text": reply_txt + raw_msg})
        if current_imgs:
            img_contents = await make_image_content(current_imgs)
            content.extend(img_contents)
        
        if content: recording_state[session_id].append({"role": "user", "content": content})
        return # 阻断

    # === 1. 判断是否归我管 ===
    # 1. 以 # 开头的指令
    # 2. 【录制结束分析】
    # 3. 潜在的自动 Wolfram 
    
    is_explicit_command = raw_msg.startswith("#") or raw_msg == "【录制结束分析】"
    
    # 复杂指令正则
    patterns = {
        "cluster_pro": r"#llm并行pro\s*\{(\d+)\}\s*(.*)",
        "auto_cluster_pro": r"#自动llm并行pro\s*(.*)",
        "beam_pro": r"#自动llm束pro\s*(.*)",
        "cluster": r"#llm并行\s*\{(\d+)\}\s*(.*)",
        "auto_cluster": r"#自动llm并行\s*(.*)",
        "beam": r"#自动llm束\s*(.*)",
        "wolfram_code": r"#mma\s*(.*)",
        "wolfram_nlp_pro": r"#计算pro\s*(.*)",
        "wolfram_nlp": r"#计算\s*(.*)",
 
    }
    matches = {k: re.search(v, raw_msg, re.DOTALL) for k, v in patterns.items()}
    has_match = any(matches.values())

    # 快捷指令
    if raw_msg.lower() == "#切pro":
        if session_id not in user_configs: user_configs[session_id] = {}
        user_configs[session_id]["model"] = MODEL_SMART
        await tool_handler.finish(" 了解，什亭之匣进入高能耗状态。")
    if raw_msg.lower() == "#切flash":
        if session_id not in user_configs: user_configs[session_id] = {}
        user_configs[session_id]["model"] = MODEL_FAST
        await tool_handler.finish(" 了解，什亭之匣进入快速响应状态。")
    if raw_msg.lower() == "#切instant":
        if session_id not in user_configs: user_configs[session_id] = {}
        user_configs[session_id]["model"] = MODEL_INSTANT
        await tool_handler.finish(" 了解，什亭之匣进入立即响应状态。")
    
    if raw_msg.startswith("#总结"):
        await bot.send(event, " 正在整理日志...")
        if session_id not in user_sessions: user_sessions[session_id] = [system_prompt()]
        msgs = user_sessions[session_id][1:]
        summary = await query_llm([{"role":"system","content":"总结对话。"},{"role":"user","content":str(msgs)}], model_id=MODEL_FAST)
        await send_smart_reply(bot, event, f"【摘要】\n{summary}")
        # 更新历史
        user_sessions[session_id] = [system_prompt(), {"role":"system","content":f"历史摘要：{summary}"}]
        return

    # 初始化历史
    if session_id not in user_sessions: 
        user_sessions[session_id] = [system_prompt()]
    if session_id not in user_configs: 
        user_configs[session_id] = {"model": MODEL_SMART}

    history = user_sessions[session_id]
    
    # 如果没有匹配到任何显式指令，且不是录制中，直接跳过到 Chat Handler
    if not is_explicit_command and not has_match:
        return
            
    # 到这里，说明要么是显式指令，要么是自动检测到了 Wolfram
    
    # === 构建上下文 (为了让工具能看到之前的对话) ===
    # 提取引用和图片 (同 reply.py)
    current_imgs = [s.data["url"] for s in event.message if s.type == "image"]
    reply_txt = ""
    reply_imgs = []
    forward_imgs = []
    
    if event.reply:
        debug_log("Tool-Reply-Debug", f"Found reply message, segments: {len(event.reply.message)}")
        reply_txt = f"【老师引用了之前的消息：{event.reply.message.extract_plain_text()}】\n"
        for seg in event.reply.message:
            debug_log("Tool-Reply-Seg", f"Segment type: {seg.type}, data: {seg.data}")
            if seg.type == "image":
                url = seg.data.get("url")
                debug_log("Tool-Reply-Image", f"Found image URL: {url}")
                if url: reply_imgs.append(url)
            elif seg.type == "forward":
                # 引用的消息是转发消息，递归提取其内容
                fwd_id = seg.data.get("id", "")
                if fwd_id:
                    fwd_result = await extract_forward_messages(bot, fwd_id)
                    reply_txt += f"\n【引用的聊天记录】：\n{fwd_result['text']}\n【引用的聊天记录结束】\n"
                    reply_imgs.extend(fwd_result['images'])
                    debug_log("Tool-Reply-Forward", f"Extracted {len(fwd_result['images'])} images from forward")
        if reply_imgs: reply_txt += f" (引用中包含 {len(reply_imgs)} 张图片)"
        debug_log("Tool-Reply-Summary", f"Total reply images: {len(reply_imgs)}")
    
    # === 提取转发消息（聊天记录）===
    forward_txt = ""
    for seg in event.message:
        if seg.type == "forward":
            fwd_id = seg.data.get("id", "")
            if fwd_id:
                fwd_result = await extract_forward_messages(bot, fwd_id)
                forward_txt += f"\n【聊天记录】：\n{fwd_result['text']}\n【聊天记录结束】\n"
                forward_imgs.extend(fwd_result['images'])
    
    # 记录用户消息进历史 (仅在非【录制结束分析】时)
    if raw_msg != "【录制结束分析】":
        user_content = []
        msg_text = reply_txt + raw_msg + forward_txt
        if msg_text: user_content.append({"type": "text", "text": msg_text})
        all_img_urls = reply_imgs + forward_imgs + current_imgs
        debug_log("Tool-Image-Summary", f"reply_imgs: {len(reply_imgs)}, forward_imgs: {len(forward_imgs)}, current_imgs: {len(current_imgs)}")
        if all_img_urls:
            img_contents = await make_image_content(all_img_urls)
            user_content.extend(img_contents)
            debug_log("Tool-Image-B64", f"成功转换 {len(img_contents)}/{len(all_img_urls)} 张图片为base64")
            
        if user_content:
            final_msg = {
                "role": "user", 
                "content": user_content if (len(user_content) > 1 or user_content[0]["type"] != "text") else user_content[0]["text"]
            }
            history.append(final_msg)

    has_media = bool(reply_imgs or forward_imgs or current_imgs)
    try:
        final_resp = "" 
        wolfram_image = None
        
        # === 处理各种指令 ===
        # 如果包含媒体，则禁用所有 Wolfram 相关指令 (如 #计算, #wolfram 等)
        if has_media:
             # 清除 Wolfram 相关的匹配
             matches["wolfram_code"] = None
             matches["wolfram_nlp_pro"] = None
             matches["wolfram_nlp"] = None
             auto_wolfram_result = None
             # 如果禁用 Wolfram 后没有任何指令匹配，且不是录制分析，则直接返回
             if not any(matches.values()) and raw_msg != "【录制结束分析】":
                 return

        if matches["cluster_pro"]:
            n, p = matches["cluster_pro"].groups()
            await send_temp(f" [Pro全功率] 遵命，强制启动 {n} 核心...")
            final_resp = await execute_cluster_workflow(send_temp, p, min(int(n), MAX_CLUSTER_NUM), history, True)
            
        elif matches["auto_cluster_pro"]:
            p = matches["auto_cluster_pro"].group(1)
            await send_temp(" [A.R.O.N.A] 正在评估战况复杂度...")
            dynamic_n = await analyze_task_complexity(p)
            await send_temp(f" [智能决策] 判定完毕，将启动 {dynamic_n} 个Pro并行")
            final_resp = await execute_cluster_workflow(send_temp, p, dynamic_n, history, True)
            
        elif matches["cluster"]:
            n, p = matches["cluster"].groups()
            await send_temp(f" [什亭之匣] 启动 {n} 核心...")
            final_resp = await execute_cluster_workflow(send_temp, p, min(int(n), MAX_CLUSTER_NUM), history, False)
            
        elif matches["auto_cluster"]:
            p = matches["auto_cluster"].group(1)
            await send_temp(" [什亭之匣] 正在分配算力...")
            dynamic_n = await analyze_task_complexity(p)
            await send_temp(f" [智能决策] 判定需要 {dynamic_n} 个视角，正在启动LLM并行...")
            final_resp = await execute_cluster_workflow(send_temp, p, dynamic_n, history, False)
            
        elif matches["beam"]:
            await send_temp(" 推演中...")
            final_resp = await execute_beam_workflow(send_temp, matches["beam"].group(1), BEAM_ROOT_BUDGET, False)
            
        elif matches["wolfram_code"]:
            code = matches["wolfram_code"].group(1)
            # 权限检查
            if is_sensitive_wolfram(code) and event.user_id != ADMIN_QQ:
                 await tool_handler.finish(" [权限拒绝] 敏感文件操作仅限管理员使用。")
            
            res_data = await run_wolfram_code(code)
            final_resp = f"Wolfram 结果：\n{res_data['text']}"
            wolfram_image = res_data["image"]

        elif matches["wolfram_nlp_pro"]:
            p = matches["wolfram_nlp_pro"].group(1)
            await send_temp(" [Pro] 计算中...")
            code = await query_llm([{"role":"user","content":f"You are a wolfram code bot.Try to realize what I need and convert to only Wolfram code to run: {p}"}], model_id=MODEL_SMART)
            code = code.replace("`", "").strip()
            
            # NLP 转换后的也要检查 (虽然是 LLM 生成的，但为了安全还是查一下)
            if is_sensitive_wolfram(code) and event.user_id != ADMIN_QQ:
                 await tool_handler.finish(" [权限拒绝] 生成的代码包含敏感操作，已拦截。")

            res_data = await run_wolfram_code(code)
            final_resp = await query_llm(history + [{"role":"user","content":f"Wolfram Code: {code}\nResult: {res_data['text']}\nExplain it."}], model_id=MODEL_SMART)
            wolfram_image = res_data["image"]

        elif matches["wolfram_nlp"]:
            p = matches["wolfram_nlp"].group(1)
            await send_temp(" [Flash] 计算中...")
            code = await query_llm([{"role":"user","content":f"You are a wolfram code bot.Try to realize what I need and convert to only Wolfram code to run: {p}"}], model_id=MODEL_FAST)
            code = code.replace("`", "").strip()

            if is_sensitive_wolfram(code) and event.user_id != ADMIN_QQ:
                 await tool_handler.finish(" [权限拒绝] 生成的代码包含敏感操作，已拦截。")
                 
            res_data = await run_wolfram_code(code)
            final_resp = await query_llm(history + [{"role":"user","content":f"Result: {res_data['text']}\nSummarize."}], model_id=MODEL_SMART)
            wolfram_image = res_data["image"]


        
        elif raw_msg == "【录制结束分析】":
             # 录制结束模式下，直接调用 LLM 回复整个上下文
             final_resp = await query_llm(history, model_id=get_user_model(session_id))
        
        # === 统一发送结果 ===
        # 清除临时消息
        for mid in temp_ids:
            try: await bot.delete_msg(message_id=mid)
            except: pass
            
        record_content = final_resp + (" [已发送数学图表]" if wolfram_image else "")
        user_sessions[session_id].append({"role": "assistant", "content": record_content})
        
        if len(user_sessions[session_id]) > MAX_HISTORY_LENGTH:
            user_sessions[session_id] = await compress_history(user_sessions[session_id])
            
        # 使用合并转发
        await send_smart_reply(bot, event, final_resp, image_path=wolfram_image, reply_message_id=event.message_id)
        
        # 回复完成后，清空卷积队列
        if session_id in message_queues:
            message_queues[session_id].clear()
            debug_log("Convolution", f"Cleared queue after tool reply for session {session_id}")
        
        # 阻断后续 Chat Handler (因为已经回复了)
        raise StopPropagation()

    except FinishedException:
        raise # 正常结束信号，抛出
    except StopPropagation:
        raise # 传播阻断信号，抛出
    except Exception as e:
        traceback.print_exc()
        # 报错了也要阻断，防止 chat handler 回复
        await bot.send(event, f"老师... 什亭之匣报错了... \n{str(e)}")
        raise StopPropagation()
