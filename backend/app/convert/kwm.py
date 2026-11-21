"""KWM 解密（酷我音乐老版格式）。移植 music-geshizhuanhuan-main/music_unlock/formats/kwm.py。
1024 字节头 + 32 字节循环异或密钥；密钥通过「相邻相同块=明文全零区」候选 + 容器嗅探恢复。"""
from __future__ import annotations

from ..errors import AppError
from ..suno.common import sniff_container

HEADER_LEN = 1024
KEY_LEN = 32
MAX_SCAN_CHUNKS = 2048  # 密钥恢复扫描的 32 字节块数上限


def _container(head: bytes) -> str:
    kind = sniff_container(head)
    return {"mp4": "m4a"}.get(kind, kind)


def _try_key(data: bytes, key: bytes) -> str:
    """用候选密钥解密文件头若干字节并嗅探容器；先剥前导零（静音）。"""
    probe = bytes(b ^ key[i & (KEY_LEN - 1)]
                  for i, b in enumerate(data[HEADER_LEN:HEADER_LEN + 4096]))
    return _container(probe.lstrip(b"\x00")[:64])


def decrypt(data: bytes, name: str) -> tuple[bytes, str]:
    if len(data) < HEADER_LEN + KEY_LEN * 2:
        raise AppError("DECRYPT_FAILED", f"{name}: KWM 文件过短。")
    body = data[HEADER_LEN:]

    candidates: list[bytes] = []
    # 候选 1：相邻两个相同的 32 字节块（对应明文全零区，密文即密钥）
    prev = body[:KEY_LEN]
    for i in range(1, min(MAX_SCAN_CHUNKS, len(body) // KEY_LEN)):
        chunk = body[i * KEY_LEN:(i + 1) * KEY_LEN]
        if chunk == prev:
            candidates.append(chunk)
        prev = chunk
    # 候选 2：最后一块前后 16 字节交换（老工具回退方案）
    candidates.append(prev[KEY_LEN // 2:] + prev[:KEY_LEN // 2])

    chosen: bytes | None = None
    for key in candidates:
        if _try_key(data, key) != "unknown":
            chosen = key
            break
    if chosen is None:  # 扩大范围：前 64 块全部当候选
        for i in range(min(64, len(body) // KEY_LEN)):
            key = body[i * KEY_LEN:(i + 1) * KEY_LEN]
            if _try_key(data, key) != "unknown":
                chosen = key
                break
    if chosen is None:
        raise AppError("DECRYPT_FAILED", f"{name}: KWM 密钥恢复失败，无法识别音频容器。")

    payload = bytes(b ^ chosen[i & (KEY_LEN - 1)] for i, b in enumerate(body))
    ext = _container(payload.lstrip(b"\x00")[:64])
    if ext == "unknown":
        raise AppError("DECRYPT_FAILED", f"{name}: KWM 解密结果不是受支持的音频格式。")
    return payload, ext
