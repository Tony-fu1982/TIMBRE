"""路径与常量。所有运行时数据在 <repo>/data/（git 忽略）。"""
from __future__ import annotations

import ctypes
import os
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
ENV_PATH = DATA / ".env"
PROFILE_DIR = DATA / "chrome-profile"
TMP_DIR = DATA / "tmp"
UPLOAD_DIR = TMP_DIR / "uploads"

DEBUG_PORT = 9222
SUNO_API = "https://studio-api-prod.suno.com"
SUNO_HOSTS = {"suno.com", "www.suno.com"}

# 用户指令：Gemini→DeepSeek（deepseek4.1falsh1017，归一化拼写）；千问默认 qwen3.8-omni-flash。
AI_DEFAULTS = {
    "deepseek": {"model": "deepseek4.1-flash-1017", "base_url": "https://api.deepseek.com/v1"},
    "qwen": {"model": "qwen3.8-omni-flash", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
}
DEFAULT_PROVIDER = "deepseek"  # 文本生成活动 provider；DNA 音频分析固定走 qwen


def ensure_dirs() -> None:
    for d in (DATA, TMP_DIR, UPLOAD_DIR, PROFILE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def system_download_dir() -> str:
    """Windows 已知文件夹 FOLDERID_Downloads（尊重用户重定向）；失败回退 ~/Downloads。"""
    try:
        class GUID(ctypes.Structure):
            _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]

        g = GUID(0x374DE290, 0x123F, 0x4565, (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
        p = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) == 0:
            v = p.value
            ctypes.windll.ole32.CoTaskMemFree(p)
            if v:
                return v
    except Exception:
        pass
    return str(Path.home() / "Downloads")


def read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def write_env(updates: dict[str, str]) -> None:
    """原子写 .env（临时文件+rename）。key 只进这里，永不回显。"""
    cur = read_env()
    cur.update({k: v for k, v in updates.items() if v is not None})
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = ENV_PATH.with_name(f".env.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text("".join(f"{k}={v}\n" for k, v in cur.items()), encoding="utf-8")
    os.replace(tmp, ENV_PATH)


def ai_config(provider: str) -> dict:
    """合并 .env 与默认值；返回 {provider, model, base_url, api_key}（api_key 仅内部用）。"""
    env = read_env()
    d = AI_DEFAULTS[provider]
    p = provider.upper()
    base = (env.get(f"{p}_BASE_URL") or d["base_url"]).rstrip("/")
    model = env.get(f"{p}_MODEL") or d["model"]
    return {
        "provider": provider,
        "model": model,
        "base_url": base,
        "api_key": env.get(f"{p}_API_KEY", ""),
    }
