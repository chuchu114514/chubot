"""
模型管理模块 - 多 API 多模型 + 自动故障切换

配置文件: config/models.json
每个档次 (SMART/FAST/INSTANT) 可配置多个候选模型，
默认使用第一个，3次失败后切换下一个，直到全部用尽。
"""

import time # 加在文件顶部，一会儿算耗时用
import json
import asyncio
import httpx
from pathlib import Path
from typing import List, Dict, Optional
import traceback

# ============ 配置加载 ============

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "models.json"
_models_config: Dict[str, List[Dict]] = {}


def _load_models_config():
    """加载 models.json，启动时调用一次"""
    global _models_config
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            _models_config = json.load(f)
        _validate_config()
        print(f"\033[32m[ModelManager]\033[0m 已加载模型配置: "
              f"SMART={len(_models_config.get('SMART', []))}个, "
              f"FAST={len(_models_config.get('FAST', []))}个, "
              f"INSTANT={len(_models_config.get('INSTANT', []))}个")
    except FileNotFoundError:
        print(f"\033[31m[ModelManager-ERR]\033[0m 找不到配置文件: {_CONFIG_PATH}")
        print(f"\033[31m[ModelManager-ERR]\033[0m 请创建 config/models.json")
        _models_config = {}
    except json.JSONDecodeError as e:
        print(f"\033[31m[ModelManager-ERR]\033[0m models.json 格式错误: {e}")
        _models_config = {}


def _validate_config():
    """校验配置格式"""
    for tier in ("SMART", "FAST", "INSTANT"):
        models = _models_config.get(tier, [])
        if not models:
            print(f"\033[33m[ModelManager-WARN]\033[0m {tier} 档无可用模型!")
            continue
        for i, m in enumerate(models):
            for key in ("name", "api_base", "api_key"):
                if key not in m:
                    print(f"\033[33m[ModelManager-WARN]\033[0m {tier}[{i}] 缺少字段: {key}")


def reload_models_config():
    """运行时重新加载配置（可用于热更新）"""
    _load_models_config()


# 启动时立即加载
_load_models_config()


# ============ 模型名称常量（向后兼容）============

def _get_first_model_name(tier: str) -> str:
    """获取某档次第一个模型的 name，不存在则返回占位符"""
    models = _models_config.get(tier, [])
    if models:
        return models[0]["name"]
    return f"MISSING_{tier}_MODEL"


# 这三个变量保持和原来用法一致，其他文件 import 后可直接用
MODEL_SMART = _get_first_model_name("SMART")
MODEL_FAST = _get_first_model_name("FAST")
MODEL_INSTANT = _get_first_model_name("INSTANT")


def get_tier_models(tier: str) -> List[Dict]:
    """获取某个档次的全部候选模型列表"""
    return _models_config.get(tier, [])


def get_tier_by_model_name(model_name: str) -> Optional[str]:
    """根据模型名反查它属于哪个档次"""
    for tier in ("SMART", "FAST", "INSTANT"):
        for m in _models_config.get(tier, []):
            if m["name"] == model_name:
                return tier
    return None


# ============ 核心请求 + 故障切换 ============



def _debug_log(tag: str, content: str, exc: Exception = None):
    """带颜色和可选异常堆栈的日志打印"""
    print(f"\033[36m[DEBUG-{tag}]\033[0m {content}")
    if exc:
        # 打印红色高亮的报错堆栈，这才是猛男该看的debug信息
        print(f"\033[31m{'='*20} TRACEBACK {'='*20}")
        traceback.print_exc()
        print(f"{'='*51}\033[0m")


async def _do_single_request(
    messages: List[Dict],
    model_cfg: Dict,
    #proxy_url: str,
    temperature: float,
    max_tokens: int
) -> str:
    payload = {
        "model": model_cfg["name"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {model_cfg['api_key']}",
        "Content-Type": "application/json",
    }

    # --- 新增的详细 Debug 信息 ---
    safe_key = f"{model_cfg['api_key'][:6]}...{model_cfg['api_key'][-4:]}" if len(model_cfg.get('api_key', '')) > 10 else "***"
    _debug_log("API-REQ", f"URL: {model_cfg.get('api_base')}")
    _debug_log("API-REQ", f"Key: {safe_key} ")
    
    # 截断太长的 payload 防止刷屏
    payload_str = json.dumps(payload, ensure_ascii=False)
    _debug_log("API-REQ", f"Payload: {payload_str[:300]}{'...' if len(payload_str) > 300 else ''}")
    # -----------------------------

    proxy = None
    start_time = time.time()
    
    async with httpx.AsyncClient(proxy=None, trust_env=True, timeout=120.0) as client:
        resp = await client.post(model_cfg["api_base"], json=payload, headers=headers)
        
        cost_time = time.time() - start_time
        _debug_log("API-RESP", f"[{model_cfg['name']}] 耗时: {cost_time:.2f}s | 状态码: {resp.status_code}")
        
        if resp.status_code != 200:
            # 遇到错误不要只打印前500个字符，直接全量喷出
            _debug_log("API-ERR-BODY", f"[{model_cfg['name']}] HTTP {resp.status_code} 详细响应体: {resp.text}")
            resp.raise_for_status()
        
        return resp.json()["choices"][0]["message"]["content"]


async def query_llm_with_fallback(
    messages: List[Dict],
    tier: str = "SMART",
    temperature: float = 0.7,
    max_tokens: int = 16384,
    #proxy_url: str = "",
) -> str:
    """
    带故障切换的 LLM 请求。
    
    按顺序尝试该档次的每个候选模型，每个模型最多重试 3 次。
    3 次均失败后切换到下一个模型。全部用尽返回错误消息。
    """
    # 消息清洗
    sanitized = [m for m in messages if m.get("content") is not None]

    models = get_tier_models(tier)
    if not models:
        _debug_log("LLM-Err", f"档次 {tier} 没有配置任何模型!")
        return "老师... 该档次没有可用的模型配置..."

    for model_idx, model_cfg in enumerate(models):
        model_name = model_cfg["name"]
        _debug_log("LLM", f"尝试模型 [{model_idx+1}/{len(models)}] {model_name} | 消息数: {len(sanitized)}")

        for attempt in range(3):
            try:
                result = await _do_single_request(
                    sanitized, model_cfg, temperature, max_tokens
                )
                # 把换行符替换成空格，防止 log 看起来很乱
                _debug_log("LLM-Success", f"{model_name} 第{attempt+1}次请求成功 | 响应首部: {result[:50].replace(chr(10), ' ')}...")
                return result
            except Exception as e:
                # 把原本只打印 e 的地方换成传入 exc 对象，触发堆栈打印
                _debug_log("LLM-Err", f"[{model_name}] 第{attempt+1}次请求炸了", exc=e)
                await asyncio.sleep(1)

        _debug_log("LLM-Fallback", f"{model_name} 3次均失败，切换下一个模型...")

    # 如果 SMART 档次所有模型都失败了，自动尝试 FAST 档次
    if tier == "SMART":
        _debug_log("LLM-Fallback-Tier", "SMART 档次全部模型失效，尝试回退到 FAST 档次...")
        return await query_llm_with_fallback(
            messages=messages,
            tier="FAST",
            temperature=temperature,
            max_tokens=max_tokens,
            #proxy_url=proxy_url
        )

    return "老师... 所有模型均尝试失败了...请检查配置或网络..."
