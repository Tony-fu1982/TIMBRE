"""Mango 解密单测（issue 03）：pycryptodome 自制 GCM 包裹 + CTR 加密夹具，
覆盖往返、独立计数器实现交叉验证、以及各类可读失败。不触网。"""
from __future__ import annotations

import base64
import hashlib
import os

import pytest
from Crypto.Cipher import AES

from app.errors import AppError
from app.suno import mango

TOKEN = "test-clerk-token-abc123"
CONTENT_ID = "11111111-2222-3333-4444-555555555555"


def _wrap(user_key: bytes, aad: bytes, plaintext: bytes) -> str:
    nonce = os.urandom(12)
    c = AES.new(user_key, AES.MODE_GCM, nonce=nonce)
    c.update(aad)
    ct, tag = c.encrypt_and_digest(plaintext)
    return base64.b64encode(nonce + ct + tag).decode()


def make_rights(token=TOKEN, content_id=CONTENT_ID, key=None, iv=None):
    key = os.urandom(16) if key is None else key
    iv = os.urandom(16) if iv is None else iv
    uk = hashlib.sha256(token.encode()).digest()
    aad = content_id.encode()
    return {"key": _wrap(uk, aad, key), "iv": _wrap(uk, aad, iv)}, key, iv


def ctr_xor(data: bytes, key: bytes, iv: bytes) -> bytes:
    """独立实现：AES-ECB 加密递增计数块生成密钥流（WebCrypto length=128 语义），
    不用 pycryptodome CTR 模式，交叉验证 decrypt_media 的计数器构造。"""
    ecb = AES.new(key, AES.MODE_ECB)
    counter = int.from_bytes(iv, "big")
    out = bytearray()
    for off in range(0, len(data), 16):
        ks = ecb.encrypt(((counter + off // 16) % (1 << 128)).to_bytes(16, "big"))
        out.extend(b ^ k for b, k in zip(data[off:off + 16], ks))
    return bytes(out)


@pytest.mark.parametrize("keylen", [16, 24, 32])
def test_roundtrip(keylen):
    rights, key, iv = make_rights(key=os.urandom(keylen))
    media = b"ID3" + os.urandom(1000)
    enc = ctr_xor(media, key, iv)
    k2, iv2 = mango.unwrap_rights(rights, CONTENT_ID, TOKEN)
    assert (k2, iv2) == (key, iv)
    assert mango.decrypt_media(enc, k2, iv2) == media


def test_urlsafe_base64_without_padding():
    rights, key, iv = make_rights()
    for f in ("key", "iv"):
        rights[f] = rights[f].replace("+", "-").replace("/", "_").rstrip("=")
    k2, iv2 = mango.unwrap_rights(rights, CONTENT_ID, TOKEN)
    assert (k2, iv2) == (key, iv)


def test_counter_wraparound():
    iv = b"\xff" * 16  # 计数器跨越 2^128 回绕
    key = os.urandom(16)
    media = os.urandom(48)
    assert mango.decrypt_media(ctr_xor(media, key, iv), key, iv) == media


def test_wrong_token_readable_failure():
    rights, _, _ = make_rights()
    wrong = "wrong-token-xyz789"
    with pytest.raises(AppError) as e:
        mango.unwrap_rights(rights, CONTENT_ID, wrong)
    assert e.value.code == "MANGO_DECRYPT_FAILED"
    assert wrong not in e.value.message and TOKEN not in e.value.message


def test_wrong_aad():
    rights, _, _ = make_rights()
    with pytest.raises(AppError) as e:
        mango.unwrap_rights(rights, "99999999-9999-9999-9999-999999999999", TOKEN)
    assert e.value.code == "MANGO_DECRYPT_FAILED"


@pytest.mark.parametrize("rights", [{}, {"key": "abc"}, {"iv": "abc"}, None])
def test_missing_fields(rights):
    with pytest.raises(AppError) as e:
        mango.unwrap_rights(rights, CONTENT_ID, TOKEN)
    assert e.value.code == "MANGO_RIGHTS_MISSING"


def test_missing_token_and_content_id():
    rights, _, _ = make_rights()
    with pytest.raises(AppError) as e:
        mango.unwrap_rights(rights, CONTENT_ID, "")
    assert e.value.code == "NOT_LOGGED_IN"
    with pytest.raises(AppError) as e:
        mango.unwrap_rights(rights, "", TOKEN)
    assert e.value.code == "MANGO_DECRYPT_FAILED"


@pytest.mark.parametrize("bad", ["!!!", "abc", "QQ===", "QQ=QQ", "a b c"])
def test_invalid_base64(bad):
    rights, _, _ = make_rights()
    rights["key"] = bad
    with pytest.raises(AppError) as e:
        mango.unwrap_rights(rights, CONTENT_ID, TOKEN)
    assert e.value.code == "MANGO_DECRYPT_FAILED"


def test_wrapped_too_short():
    rights, _, _ = make_rights()
    rights["key"] = base64.b64encode(os.urandom(28)).decode()
    with pytest.raises(AppError) as e:
        mango.unwrap_rights(rights, CONTENT_ID, TOKEN)
    assert e.value.code == "MANGO_DECRYPT_FAILED"


@pytest.mark.parametrize("field,size", [("key", 15), ("key", 33), ("iv", 12), ("iv", 20)])
def test_invalid_unwrapped_lengths(field, size):
    rights, _, _ = make_rights(**{field: os.urandom(size)})
    with pytest.raises(AppError) as e:
        mango.unwrap_rights(rights, CONTENT_ID, TOKEN)
    assert e.value.code == "MANGO_DECRYPT_FAILED"


def test_tampered_wrapped_value():
    rights, _, _ = make_rights()
    raw = bytearray(base64.b64decode(rights["key"]))
    raw[15] ^= 0xFF  # 篡改密文 → GCM tag 校验失败
    rights["key"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(AppError) as e:
        mango.unwrap_rights(rights, CONTENT_ID, TOKEN)
    assert e.value.code == "MANGO_DECRYPT_FAILED"


def test_empty_media():
    with pytest.raises(AppError) as e:
        mango.decrypt_media(b"", os.urandom(16), os.urandom(16))
    assert e.value.code == "MANGO_DECRYPT_FAILED"
