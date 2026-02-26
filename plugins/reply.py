from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent
from .common import *
from .tool_handler import tool_handler
from .chat_handler import chat_handler

# 1. 被动监听器 (优先级99，不阻断，记录群聊背景)
passive_listener = on_message(priority=99, block=False)

# 2. 重置指令
reset_cmd = on_command("重置", aliases={"clear", "忘了吧", "重启什亭之匣"}, priority=5, block=True)

# 3. Tool Handler (已经在 tool_handler.py 注册 priority=10)
# 4. Chat Handler (已经在 chat_handler.py 注册 priority=15)

# ---  被动监听 ---
@passive_listener.handle()
async def _(event: GroupMessageEvent):
    gid = f"group_{event.group_id}"
    if gid not in passive_buffer: passive_buffer[gid] = deque(maxlen=PASSIVE_BUFFER_SIZE)
    raw = event.get_plaintext().strip()
    if raw and not raw.startswith("#"):
        name = event.sender.card or event.sender.nickname
        passive_buffer[gid].append(f"[{name}]: {raw}")

# --- 重置功能 ---
@reset_cmd.handle()
async def _(event: Event):
    sid = get_session_id(event)
    for store in [user_sessions, user_configs, passive_buffer, recording_state, user_active_files]:
        if sid in store: del store[sid]
    await reset_cmd.finish("已格式化... 老师，让我们重新开始。（期待地看着你）")
