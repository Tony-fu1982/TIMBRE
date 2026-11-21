"""酷狗 kgmusicv3.db 解密（doc 05 §7.1）。施工：convert 代理。
每页 1024B AES-128-CBC；master hex 1d613145b247bf7f3d189672144fe4bf0000000073416c54；
页号写 master[16..20]；key=md5(master)，iv=md5(LCG seed 16B)；页1头特殊处理；
解密内容仅内存 sqlite，绝不落盘明文；WAL>32B=酷狗运行中→拒绝。
逐行移植 extracted/electron/music-client-keys.cjs（只读参考）。"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import struct
from pathlib import Path

from Crypto.Cipher import AES

from ..errors import AppError

SQLITE_HEADER = b"SQLite format 3\x00"
PAGE_SIZE = 1024
MASTER_KEY = bytes.fromhex("1d613145b247bf7f3d189672144fe4bf0000000073416c54")
MAX_DB_BYTES = 128 * 1024 * 1024


def page_material(page: int) -> tuple[bytes, bytes]:
    """页密钥/IV：master[16:20]=页号→md5 为 key；LCG 4 轮 u32→md5 为 iv。"""
    master = bytearray(MASTER_KEY)
    struct.pack_into("<I", master, 16, page)
    key = hashlib.md5(master).digest()
    seed = page + 1
    raw = bytearray(16)
    for i in range(0, 16, 4):
        value = (seed * 0x9EF4 - (seed // 0xCE26) * 0x7FFFFF07) & 0xFFFFFFFF
        seed = (value + 0x7FFFFF07) & 0xFFFFFFFF if value & 0x80000000 else value
        struct.pack_into("<I", raw, i, seed)
    return key, hashlib.md5(raw).digest()


def _decrypt_page(data: bytes, page: int) -> bytes:
    key, iv = page_material(page)
    return AES.new(key, AES.MODE_CBC, iv).decrypt(data)  # 无填充，长度恒为 16 倍数


def decrypt_database(data: bytes) -> bytes:
    """整库解密；明文 SQLite 直接放行。页1：enc[8:16] 补回密文头 8B 再解 [16:1024]。"""
    if data[:16] == SQLITE_HEADER:
        return data
    if (len(data) < PAGE_SIZE or len(data) % PAGE_SIZE
            or struct.unpack_from(">H", data, 16)[0] != PAGE_SIZE
            or struct.unpack_from("<I", data, 20)[0] != 0x20204000):
        raise AppError("KEY_DB_UNAVAILABLE", "酷狗本地数据库的加密版本尚未支持。")
    out = bytearray(data)
    first = bytearray(data[:PAGE_SIZE])
    first[16:24] = first[8:16]
    out[16:PAGE_SIZE] = _decrypt_page(bytes(first[16:]), 1)
    out[:16] = SQLITE_HEADER
    for page in range(2, len(data) // PAGE_SIZE + 1):
        s = (page - 1) * PAGE_SIZE
        out[s:s + PAGE_SIZE] = _decrypt_page(data[s:s + PAGE_SIZE], page)
    return bytes(out)


def find_key_dbs(source_path: str = "") -> list[Path]:
    """候选密钥库：上传文件所在目录优先，其次 APPDATA/LOCALAPPDATA 下酷狗目录，深度3。"""
    roots: list[Path] = []
    if source_path and os.path.isabs(source_path) and len(source_path) < 4096:
        roots.append(Path(source_path).parent)
    for env in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            roots.extend(Path(base) / name for name in ("KuGou8", "KuGou", "KugouMusic"))
    roots.append(Path.home() / "Library" / "Application Support" / "KuGou")
    found: list[Path] = []
    visited: set[str] = set()
    scanned = 0

    def visit(folder: Path, depth: int) -> None:
        nonlocal scanned
        key = str(folder)
        if key in visited or scanned > 256 or len(found) >= 16:
            return
        scanned += 1
        visited.add(key)
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False) and entry.name.lower() == "kgmusicv3.db":
                    found.append(Path(entry.path))
                elif entry.is_dir(follow_symlinks=False) and depth > 0:
                    visit(Path(entry.path), depth - 1)
            except OSError:
                continue

    for root in roots:
        visit(root, 3)
    return found


class KeyDB:
    """打开（必要时解密）用户自己的 kgmusicv3.db，仅内存查询 ekey；close 时清零。"""

    def __init__(self, db_path):
        self.path = Path(db_path)
        self._keys: dict[str, bytearray] = {}
        try:
            size = self.path.stat().st_size
        except OSError:
            raise AppError("KEY_DB_UNAVAILABLE", "无法读取酷狗本地密钥库，请关闭酷狗客户端后重试。")
        if not self.path.is_file() or size == 0 or size > MAX_DB_BYTES:
            raise AppError("KEY_DB_UNAVAILABLE", "酷狗本地密钥库大小无效。")
        wal = Path(str(self.path) + "-wal")
        try:
            if wal.stat().st_size > 32:
                raise AppError("KEY_DB_UNAVAILABLE", "酷狗密钥库正在更新，请先关闭酷狗客户端后重试。")
        except FileNotFoundError:
            pass
        plain = bytearray(decrypt_database(self.path.read_bytes()))
        try:
            conn = sqlite3.connect(":memory:")
            try:
                conn.deserialize(bytes(plain))
                rows = conn.execute(
                    "SELECT EncryptionKeyId, EncryptionKey FROM ShareFileItems "
                    "WHERE EncryptionKey IS NOT NULL AND EncryptionKey != '' LIMIT 100000"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            raise AppError("KEY_DB_UNAVAILABLE", "无法读取酷狗本地密钥库，请关闭酷狗客户端后重试。")
        finally:
            plain[:] = b"\x00" * len(plain)  # 明文仅内存，用后清零
        for kid, ekey in rows:
            if isinstance(kid, str) and isinstance(ekey, str):
                self._keys[kid.rstrip("\x00")] = bytearray(ekey, "utf-8")

    def lookup(self, key_id: str) -> bytes | None:
        value = self._keys.get(key_id)
        return bytes(value) if value is not None else None

    def close(self) -> None:
        for value in self._keys.values():
            value[:] = b"\x00" * len(value)
        self._keys.clear()

    def __enter__(self) -> "KeyDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()