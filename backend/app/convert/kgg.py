"""KGG 解密（issue 19，部分支持）。施工：convert 代理。
KGG 与 KGM 共用容器 magic/版本分支（doc 04 同一 KgmWasm 路径），复用 kgm.decrypt。
「部分支持」= 仅已验证版本 1–5；未知版本 → UNSUPPORTED_FORMAT；v5 同样需要本地密钥库。"""
from __future__ import annotations

from . import kgm


def decrypt(data: bytes, keydb) -> tuple[bytes, str]:
    return kgm.decrypt(data, "audio.kgg", keydb)