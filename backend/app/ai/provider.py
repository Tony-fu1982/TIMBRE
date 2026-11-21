"""OpenAI 兼容 chat/completions 客户端（CONTRACTS §5）。

- deepseek：文本 JSON 任务；qwen：音频 DNA + JSON（modalities/reasoning_effort/input_audio）。
- 温度 0、stream:false、response_format:json_object（SPEC §4.8 统一 480s）。
- 错误映射：NO_AI_KEY / AI_AUTH_FAILED / AI_MODEL_UNAVAILABLE / RATE_LIMITED / TIMEOUT / NETWORK_ERROR。
- key 绝不进日志/异常消息。
"""
from __future__ import annotations

import json
import re

import httpx

from .. import config
from ..errors import AppError


def load_ai_config(provider: str = config.DEFAULT_PROVIDER) -> dict:
    return config.ai_config(provider)


def save_ai_config(provider: str, model: str, api_key: str | None = None, base_url: str | None = None) -> None:
    p = provider.upper()
    updates = {f"{p}_MODEL": model}
    if base_url:
        updates[f"{p}_BASE_URL"] = base_url
    if api_key:
        updates[f"{p}_API_KEY"] = api_key
    config.write_env(updates)


def _extract_json(text: str) -> dict:
    """§4.11 fN 修复：剥 ```json 围栏 → loads → 首个配平花括号（字符串转义感知）。"""
    if not text or not text.strip():
        raise AppError("AI_RESULT_INVALID", "API 返回了空响应，请重试。")
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s).strip()
    obj = None
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        start = s.find("{")
        if start >= 0:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(s)):
                ch = s[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                elif ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(s[start : i + 1])
                        except json.JSONDecodeError:
                            obj = None
                        break
    if not isinstance(obj, dict):
        raise AppError("AI_RESULT_INVALID", "返回数据非 JSON 格式，可能模型输出被截断或格式错误。")
    return obj


async def chat_json(
    cfg: dict,
    messages: list[dict],
    *,
    audio_b64: str | None = None,
    audio_fmt: str | None = None,
    timeout: float = 480,
) -> dict:
    if not cfg.get("api_key"):
        raise AppError("NO_AI_KEY")
    url = cfg["base_url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    msgs = [dict(m) for m in messages]
    if audio_b64:
        last = msgs[-1]
        text = last["content"] if isinstance(last["content"], str) else ""
        last["content"] = [
            {"type": "text", "text": text},
            {"type": "input_audio", "input_audio": {"data": f"data:;base64,{audio_b64}", "format": audio_fmt or "mp3"}},
        ]

    payload: dict = {
        "model": cfg["model"],
        "messages": msgs,
        "temperature": 0,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    if cfg.get("provider") == "qwen":
        payload["modalities"] = ["text"]
        payload["reasoning_effort"] = "none"
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}

    for _attempt in (0, 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise AppError("TIMEOUT", "AI 请求超时，请重试或缩短音频。")
        except httpx.HTTPError as exc:
            raise AppError("NETWORK_ERROR", f"AI 网络请求失败：{type(exc).__name__}")
        if resp.status_code == 400 and "response_format" in payload and "response_format" in resp.text:
            # ponytail: 部分兼容网关不支持 response_format；提示词已约束 JSON，去掉重试一次
            payload.pop("response_format")
            continue
        break

    status = resp.status_code
    if status == 200:
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except Exception:
            raise AppError("AI_RESULT_INVALID", "API 返回结构异常，请重试。")
        if isinstance(content, list):  # omni 偶发分块 content
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return _extract_json(content if isinstance(content, str) else "")

    if status in (401, 403):
        raise AppError("AI_AUTH_FAILED")
    if status == 429:
        raise AppError("RATE_LIMITED", "AI 服务限流（429），请稍后重试。")
    if 400 <= status < 500:
        raise AppError("AI_MODEL_UNAVAILABLE", f"AI 模型请求失败（HTTP {status}），请检查模型 ID 和 API 地址。")
    raise AppError("NETWORK_ERROR", f"AI 服务暂时不可用（HTTP {status}），请稍后重试。")
