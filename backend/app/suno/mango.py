"""Suno Mango 加密音频解密（SPEC §4.7 / issue 03）。

算法逐条移植参考实现 studio-mango-audio.cjs（decryptStudioMangoAudio）：
- userKey = SHA-256(UTF8(authToken))，登录态页内路径的 authToken 是 Clerk Bearer token；
- rights.key / rights.iv = base64(nonce[12B] + AES-GCM 密文 + tag[16B])，AAD = UTF8(contentId)；
- 解包得 contentKey（16/24/32B）与计数器 IV（16B），媒体用 AES-CTR（128 位计数器）解密。
authToken 只参与哈希，绝不写入日志、结果或异常消息。
"""
from __future__ import annotations

import base64
import hashlib
import re

from Crypto.Cipher import AES

from ..errors import AppError

_B64_BAD = re.compile(r"[^A-Za-z0-9+/]")


def _b64(value: str) -> bytes:
    """参考实现同款 base64 校验：urlsafe 归一、补位、拒绝非法字符/畸形 padding。"""
    s = str(value or "").strip().replace("-", "+").replace("_", "/")
    core = s.rstrip("=")
    pad = len(s) - len(core)
    if not core or pad > 2 or "=" in core or _B64_BAD.search(core) or len(core) % 4 == 1:
        raise AppError("MANGO_DECRYPT_FAILED", "rights 字段不是合法的 base64。")
    s = core + "=" * ((4 - len(core) % 4) % 4)
    try:
        return base64.b64decode(s, validate=True)
    except Exception:
        raise AppError("MANGO_DECRYPT_FAILED", "rights 字段 base64 解码失败。")


def unwrap_rights(rights: dict, content_id: str, auth_token: str) -> tuple[bytes, bytes]:
    """用登录态 token 解包 rights 响应，返回 (contentKey, counterIV)。失败抛 MANGO_* AppError。"""
    if not auth_token:
        raise AppError("NOT_LOGGED_IN")
    if not content_id:
        raise AppError("MANGO_DECRYPT_FAILED", "缺少 contentId，无法解密。")
    if not isinstance(rights, dict):
        rights = {}
    key_b64, iv_b64 = str(rights.get("key") or ""), str(rights.get("iv") or "")
    if not key_b64 or not iv_b64:
        raise AppError("MANGO_RIGHTS_MISSING", "rights 响应缺少 key/iv 字段。")
    user_key = hashlib.sha256(auth_token.encode("utf-8")).digest()
    aad = content_id.encode("utf-8")
    unwrapped = []
    for b64 in (key_b64, iv_b64):
        wrapped = _b64(b64)
        if len(wrapped) <= 28:  # 12B nonce + 至少 1B 明文 + 16B tag
            raise AppError("MANGO_DECRYPT_FAILED", "rights 包裹值长度无效。")
        try:
            cipher = AES.new(user_key, AES.MODE_GCM, nonce=wrapped[:12])
            cipher.update(aad)
            unwrapped.append(cipher.decrypt_and_verify(wrapped[12:-16], wrapped[-16:]))
        except ValueError:
            raise AppError("MANGO_DECRYPT_FAILED", "rights 解包失败：密钥与登录态不匹配或数据已损坏。")
    content_key, counter_iv = unwrapped
    if len(content_key) not in (16, 24, 32):
        raise AppError("MANGO_DECRYPT_FAILED", "解出的内容密钥长度无效。")
    if len(counter_iv) != 16:
        raise AppError("MANGO_DECRYPT_FAILED", "解出的计数器 IV 长度无效。")
    return content_key, counter_iv


def decrypt_media(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-CTR 解密媒体字节（128 位大端计数器，与 WebCrypto {counter,length:128} 一致）。"""
    if not data:
        raise AppError("MANGO_DECRYPT_FAILED", "加密音频为空。")
    try:
        cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=int.from_bytes(iv, "big"))
        return cipher.decrypt(data)
    except ValueError:
        raise AppError("MANGO_DECRYPT_FAILED", "音频解密失败。")
