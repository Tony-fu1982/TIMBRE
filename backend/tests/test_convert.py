"""转换模块自测（issue 15–19）。全部自制夹具、不触网。
加密夹具用「独立加密方向」构造（TEA 加密器 / 掩码逆变换 / AES-ECB），与解密实现互为交叉验证。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import sqlite3
import struct
import subprocess
from pathlib import Path

import pytest
from Crypto.Cipher import AES

from app import ffmedia
from app.convert import converter, keydb, kgm, kgg, kwm, ncm, qmc
from app.convert.tables import KGM1, KGM2, KGM_MEND, VPR, xor_mask
from app.errors import AppError
from app.jobs import JobContext

M = 0xFFFFFFFF


# ---------- 通用音频夹具 ----------

def _fake_mp3(frames: int = 40) -> bytes:
    """合法 MPEG1-Layer3 128kbps/44100Hz 帧头 + 零填充（帧长 417）。"""
    return b"".join(b"\xff\xfb\x90\x00" + b"\x00" * 413 for _ in range(frames))


def _fake_flac() -> bytes:
    return b"fLaC" + b"\x00" + b"\x00\x00\x22" + b"\x00" * 34 + b"\x00" * 8


def _real_mp3(path: Path, seconds: float = 0.5) -> bytes:
    subprocess.run(
        [ffmedia.FFMPEG, "-y", "-f", "lavfi", "-i",
         f"sine=frequency=440:sample_rate=44100:duration={seconds}",
         "-c:a", "libmp3lame", "-q:a", "9", str(path)],
        check=True, capture_output=True)
    return path.read_bytes()


# ---------- TEA 独立加密方向 ----------

def _tea_enc_block(block: bytes, words) -> bytes:
    left, right = struct.unpack(">II", block)
    w0, w1, w2, w3 = words
    total = 0
    for _ in range(16):
        total = (total + 0x9E3779B9) & M
        left = (left + (((right << 4) + w0) ^ (right + total) ^ ((right >> 5) + w1))) & M
        right = (right + (((left << 4) + w2) ^ (left + total) ^ ((left >> 5) + w3))) & M
    return struct.pack(">II", left, right)


def _tea_wrap(payload: bytes, key: bytes) -> bytes:
    """tencent_tea 的逆：头(1+pad+2) + payload + 7 零尾，CBC 变体加密。"""
    pad = (-(10 + len(payload))) % 8
    plain = bytes([pad]) + b"\xa5" * pad + b"\x5a\xa5" + payload + b"\x00" * 7
    assert len(plain) % 8 == 0
    words = struct.unpack(">4I", key)
    out = bytearray()
    prev_cipher = prev_decoded = bytes(8)
    for off in range(0, len(plain), 8):
        decoded = bytes(a ^ b for a, b in zip(plain[off:off + 8], prev_cipher))
        block = _tea_enc_block(decoded, words)
        cipher = bytes(a ^ b for a, b in zip(block, prev_decoded))
        out += cipher
        prev_cipher, prev_decoded = cipher, decoded
    return bytes(out)


def _tan_key(prefix: bytes) -> bytes:
    key = bytearray(16)
    for i in range(8):
        key[i * 2] = math.floor(abs(math.tan(106 + i * 0.1)) * 100) & 255
        key[i * 2 + 1] = prefix[i]
    return bytes(key)


def _make_raw(body: bytes = b"B" * 16) -> bytes:
    prefix = bytes(range(1, 9))
    return prefix + _tea_wrap(body, _tan_key(prefix))


# ---------- TEA / deriveKey ----------

def test_tea_roundtrip():
    key = bytes(range(16))
    payload = b"hello tea payload 1234567890"
    assert qmc.tencent_tea(_tea_wrap(payload, key), key) == payload


def test_tea_bad_padding():
    key = bytes(range(16))
    cipher = bytearray(_tea_wrap(b"x" * 16, key))
    cipher[-1] ^= 1  # 破坏零尾
    with pytest.raises(AppError) as e:
        qmc.tencent_tea(bytes(cipher), key)
    assert e.value.code == "DECRYPT_FAILED"


def test_derive_key_v1():
    body = b"K" * 24
    raw = _make_raw(body)
    assert qmc.derive_key(base64.b64encode(raw)) == raw[:8] + body


def test_derive_key_encv2():
    raw = _make_raw(b"V" * 16)
    s = base64.b64encode(raw)  # ascii
    d1 = _tea_wrap(s, qmc.GARBLE_2)
    raw1 = _tea_wrap(d1, qmc.GARBLE_1)
    ekey = base64.b64encode(qmc.ENCV2_PREFIX + raw1)
    assert qmc.derive_key(ekey) == raw[:8] + b"V" * 16


def test_derive_key_bad_b64():
    with pytest.raises(AppError):
        qmc.derive_key("!!!not-base64!!!")


def test_derive_key_allow_raw():
    raw = b"R" * 32  # TEA 校验必失败 → allow_raw 回退
    assert qmc.derive_key(base64.b64encode(raw), allow_raw=True) == raw
    with pytest.raises(AppError):
        qmc.derive_key(base64.b64encode(raw))


# ---------- QMC 三种 cipher ----------

def test_qmc_static_roundtrip_small_and_large():
    for frames in (40, 1000):  # 1000 帧跨越 32768 周期边界
        mp3 = _fake_mp3(frames)
        enc = qmc.qmc_transform(mp3, None)
        assert enc != mp3
        assert qmc.qmc_transform(enc, None) == mp3
        out, fmt = qmc.decrypt(enc, "song.qmc0")
        assert (out, fmt) == (mp3, "mp3")


def test_qmc_map_roundtrip():
    # 无尾标签的 map 密文没有密钥来源，decrypt 只能走静态 mask（正确行为）；
    # map 算法本身用变换对合验证，带密钥的 decrypt 路径见 QTag/内嵌密钥用例。
    mp3 = _fake_mp3(300)
    key = bytes((i * 13 + 5) & 255 for i in range(112))
    enc = qmc.qmc_transform(mp3, key)
    assert enc != mp3
    assert qmc.qmc_transform(enc, key) == mp3


def test_qmc_map_keylen_boundary():
    mp3 = _fake_mp3(4)
    key300 = bytes(range(256)) + bytes(range(44))
    assert qmc.qmc_transform(qmc.qmc_transform(mp3, key300), key300) == mp3
    with pytest.raises(AppError) as e:
        qmc.qmc_transform(mp3, bytes(301))
    assert e.value.code == "UNSUPPORTED_FORMAT"


def test_qmc_rc4_roundtrip():
    mp3 = _fake_mp3(300)  # >128B 前导 + 多个 5120 分段
    key = bytes((i * 7 + 3) % 251 + 1 for i in range(700))
    enc = qmc.qmc_transform(mp3, key)
    assert enc != mp3
    assert qmc.qmc_transform(enc, key) == mp3


def test_qmc_qtag_map_roundtrip():
    mp3 = _fake_mp3(60)
    raw = _make_raw(b"M" * 16)  # 派生 24B → map
    key = qmc.derive_key(base64.b64encode(raw))
    enc = qmc.qmc_transform(mp3, key)
    tag = base64.b64encode(raw) + b",12345,0"
    file = enc + tag + struct.pack(">I", len(tag)) + b"QTag"
    out, fmt = qmc.decrypt(file, "song.qmc0")
    assert (out, fmt) == (mp3, "mp3")


def test_qmc_qtag_rc4_roundtrip():
    mp3 = _fake_mp3(300)
    raw = _make_raw(b"R" * 600)  # 派生 608B → RC4
    key = qmc.derive_key(base64.b64encode(raw))
    assert len(key) >= 512
    enc = qmc.qmc_transform(mp3, key)
    tag = base64.b64encode(raw) + b",9,0"
    file = enc + tag + struct.pack(">I", len(tag)) + b"QTag"
    out, fmt = qmc.decrypt(file, "song.mgg1")
    assert (out, fmt) == (mp3, "mp3")


def test_qmc_stag_unsupported():
    tag = b"12345,0,mid"
    enc = qmc.qmc_transform(_fake_mp3(4), None)  # 密文不可嗅探，才会走到尾标签分支
    file = enc + tag + struct.pack(">I", len(tag)) + b"STag"
    with pytest.raises(AppError) as e:
        qmc.decrypt(file, "song.qmc0")
    assert e.value.code == "UNSUPPORTED_FORMAT"


def test_qmc_untagged_embedded_key():
    mp3 = _fake_mp3(60)
    raw = _make_raw(b"U" * 16)
    ekey_b64 = base64.b64encode(raw)
    key = qmc.derive_key(ekey_b64, allow_raw=True)
    enc = qmc.qmc_transform(mp3, key)
    file = enc + ekey_b64 + struct.pack("<I", len(ekey_b64))
    out, fmt = qmc.decrypt(file, "song.qmc0")
    assert (out, fmt) == (mp3, "mp3")


def test_qmc_plain_passthrough():
    mp3 = _fake_mp3(4)
    assert qmc.decrypt(mp3, "song.qmc0") == (mp3, "mp3")


def test_qmc_garbage_fails():
    with pytest.raises(AppError) as e:
        qmc.decrypt(b"\x01\x02\x03\x04" * 100, "song.qmc0")
    assert e.value.code == "DECRYPT_FAILED"


# ---------- NCM ----------

def _make_ncm(audio: bytes, key: bytes = b"0123456789abcdef") -> bytes:
    key_data = ncm.KEY_PREFIX + key
    pad = 16 - len(key_data) % 16
    key_cipher = AES.new(ncm.CORE_KEY, AES.MODE_ECB).encrypt(key_data + bytes([pad]) * pad)
    key_region = bytes(b ^ 0x64 for b in key_cipher)
    meta, cover = b'{"fake":1}', b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    perm = list(range(256))
    j = 0
    for i in range(256):
        j = (j + perm[i] + key[i % len(key)]) & 255
        perm[i], perm[j] = perm[j], perm[i]
    mask = bytearray(256)
    for i in range(256):
        n = (i + 1) & 255
        mask[i] = perm[(perm[n] + perm[(perm[n] + n) & 255]) & 255]
    full = bytes(mask) * (len(audio) // 256 + 1)
    out = bytearray(b"CTENFDAM\x00\x00")
    out += struct.pack("<I", len(key_region)) + key_region
    out += struct.pack("<I", len(meta)) + meta
    out += b"\x00" * 5
    out += struct.pack("<II", len(cover), len(cover)) + cover
    out += xor_mask(audio, full)
    return bytes(out)


def test_ncm_roundtrip_mp3():
    mp3 = _fake_mp3(60)
    out, fmt = ncm.decrypt(_make_ncm(mp3))
    assert (out, fmt) == (mp3, "mp3")


def test_ncm_roundtrip_flac():
    flac = _fake_flac()
    out, fmt = ncm.decrypt(_make_ncm(flac))
    assert (out, fmt) == (flac, "flac")


def test_ncm_bad_magic():
    with pytest.raises(AppError) as e:
        ncm.decrypt(b"NOTNCM!!" + b"\x00" * 64)
    assert e.value.code == "UNSUPPORTED_FORMAT"


def test_ncm_bad_key_len():
    data = bytearray(_make_ncm(_fake_mp3(4)))
    struct.pack_into("<I", data, 10, 0)
    with pytest.raises(AppError) as e:
        ncm.decrypt(bytes(data))
    assert e.value.code == "DECRYPT_FAILED"


def test_ncm_truncated():
    with pytest.raises(AppError) as e:
        ncm.decrypt(_make_ncm(_fake_mp3(4))[:20])
    assert e.value.code == "DECRYPT_FAILED"


def test_ncm_no_audio():
    with pytest.raises(AppError) as e:
        ncm.decrypt(_make_ncm(b""))  # 封面之后没有音频
    assert e.value.code == "DECRYPT_FAILED"


# ---------- 酷狗容器夹具 ----------

def _kugou_md5_ref(data: bytes) -> bytes:
    d = hashlib.md5(data).digest()
    return bytes([d[14], d[15], d[12], d[13], d[10], d[11], d[8], d[9],
                  d[6], d[7], d[4], d[5], d[2], d[3], d[0], d[1]])


def _kgm_file(cipher: bytes, version: int, *, magic: bytes = kgm.KGM_MAGIC,
              key_id: bytes = b"", slot: int = 1, offset: int = 1024) -> bytes:
    h = bytearray(offset)
    h[:16] = magic
    struct.pack_into("<I", h, 16, offset)
    struct.pack_into("<I", h, 20, version)
    struct.pack_into("<I", h, 24, slot)
    h[28:44] = b"0123456789abcdef"
    h[44:60] = b"fedcba9876543210"
    if version == 5 and key_id and offset >= 72 + len(key_id):
        struct.pack_into("<I", h, 68, len(key_id))
        h[72:72 + len(key_id)] = key_id
    return bytes(h) + cipher


def _legacy_encrypt(plain: bytes, header: bytes) -> bytes:
    """参考 while 循环原样（独立于实现的 DP 展开）；g(x)=x^((x&15)<<4) 是对合。"""
    own = header[28:44] + b"\x00"
    vpr = header[:16] == kgm.VPR_MAGIC
    out = bytearray(len(plain))
    for i in range(len(plain)):
        position, mask = i // 16, 0
        while position >= 17:
            mask ^= KGM1[position % 272]
            position //= 16
            mask ^= KGM2[position % 272]
            position //= 16
        mask ^= KGM_MEND[i % 272]
        m = mask ^ ((mask & 15) << 4) ^ (VPR[i % 17] if vpr else 0)
        v = plain[i] ^ m
        out[i] = (v ^ ((v & 15) << 4)) ^ own[i % 17]
    return bytes(out)


def _v3_encrypt(plain: bytes, header: bytes) -> bytes:
    slot = _kugou_md5_ref(b"\x6c\x2c\x2f\x27")
    file_key = _kugou_md5_ref(header[44:60]) + b"\x6b"
    out = bytearray(len(plain))
    for i in range(len(plain)):
        idx = (i & 255) ^ ((i >> 8) & 255) ^ ((i >> 16) & 255) ^ ((i >> 24) & 255)
        v = plain[i] ^ slot[i % 16] ^ idx
        out[i] = (v ^ ((v & 15) << 4)) ^ file_key[i % 17]
    return bytes(out)


@pytest.mark.parametrize("version", [1, 2, 4])
def test_kgm_legacy_versions(version):
    mp3 = _fake_mp3(300)  # 跨 272/16 进制位递归多层
    file = _kgm_file(b"", version)
    header = file[:1024]
    cipher = _legacy_encrypt(mp3, header)
    out, fmt = kgm.decrypt(_kgm_file(cipher, version), "song.kgm", None)
    assert (out, fmt) == (mp3, "mp3")


def test_kgm_v3_slot1():
    mp3 = _fake_mp3(60)
    header = _kgm_file(b"", 3)[:1024]
    cipher = _v3_encrypt(mp3, header)
    out, fmt = kgm.decrypt(_kgm_file(cipher, 3), "song.kgm", None)
    assert (out, fmt) == (mp3, "mp3")


def test_kgm_v3_legacy_fallback():
    mp3 = _fake_mp3(60)
    header = _kgm_file(b"", 3)[:1024]
    cipher = _legacy_encrypt(mp3, header)  # 版本 3 但用旧掩码
    out, fmt = kgm.decrypt(_kgm_file(cipher, 3), "song.kgm", None)
    assert (out, fmt) == (mp3, "mp3")


def test_kgma_and_vpr():
    mp3 = _fake_mp3(60)
    for magic, name in ((kgm.KGM_MAGIC, "song.kgma"), (kgm.VPR_MAGIC, "song.vpr")):
        header = _kgm_file(b"", 2, magic=magic)[:1024]
        cipher = _legacy_encrypt(mp3, header)
        out, fmt = kgm.decrypt(_kgm_file(cipher, 2, magic=magic), name, None)
        assert (out, fmt) == (mp3, "mp3")


def test_kgg_delegates_kgm():
    mp3 = _fake_mp3(60)
    header = _kgm_file(b"", 2)[:1024]
    file = _kgm_file(_legacy_encrypt(mp3, header), 2)
    assert kgg.decrypt(file, None) == (mp3, "mp3")


def test_kgm_plain_passthrough():
    mp3 = _fake_mp3(4)
    assert kgm.decrypt(mp3, "song.kgm", None) == (mp3, "mp3")


def test_kgm_bad_magic():
    with pytest.raises(AppError) as e:
        kgm.decrypt(b"\x00" * 128, "song.kgm", None)
    assert e.value.code == "UNSUPPORTED_FORMAT"


def test_kgm_unsupported_version():
    file = _kgm_file(_fake_mp3(4), 6)
    with pytest.raises(AppError) as e:
        kgm.decrypt(file, "song.kgm", None)
    assert e.value.code == "UNSUPPORTED_FORMAT"


def test_kgm_v3_bad_slot():
    file = _kgm_file(_fake_mp3(4), 3, slot=2)
    with pytest.raises(AppError) as e:
        kgm.decrypt(file, "song.kgm", None)
    assert e.value.code == "UNSUPPORTED_FORMAT"


def test_kgm_v5_needs_keydb():
    file = _kgm_file(_fake_mp3(4), 5, key_id=b"abc123")
    with pytest.raises(AppError) as e:
        kgm.decrypt(file, "song.kgm", None)
    assert e.value.code == "KEY_DB_UNAVAILABLE"


def test_kgm_v5_bad_key_id():
    file = _kgm_file(_fake_mp3(4), 5, key_id=b"bad id!")
    with pytest.raises(AppError) as e:
        kgm.decrypt(file, "song.kgm", None)
    assert e.value.code == "DECRYPT_FAILED"


def test_kgm_v5_short_offset():
    file = _kgm_file(_fake_mp3(4), 5, key_id=b"abc", offset=70)
    with pytest.raises(AppError) as e:
        kgm.decrypt(file, "song.kgm", None)
    assert e.value.code == "DECRYPT_FAILED"


# ---------- kwm ----------

def _kwm_file(plain: bytes, key: bytes = b"K" * 32) -> bytes:
    """独立加密方向：1024 头 + 32 字节循环异或；明文含对齐零区供密钥恢复。"""
    body = bytes(b ^ key[i & 31] for i, b in enumerate(plain))
    return b"\x00" * 1024 + body


def test_kwm_mp3_roundtrip():
    plain = b"\xff\xfb\x90\x00" + b"\x00" * 28 + b"\x00" * 64 + _fake_mp3(4)
    assert kwm.decrypt(_kwm_file(plain), "song.kwm") == (plain, "mp3")


def test_kwm_flac_roundtrip():
    plain = _fake_flac() + b"\x00" * 14 + b"\x00" * 64 + b"\x00" * 32
    assert kwm.decrypt(_kwm_file(plain), "song.kwm") == (plain, "flac")


def test_kwm_leading_silence():
    plain = b"\x00" * 64 + _fake_mp3(4)  # 前导静音，嗅探前剥零
    assert kwm.decrypt(_kwm_file(plain), "song.kwm") == (plain, "mp3")


def test_kwm_short_file():
    with pytest.raises(AppError) as e:
        kwm.decrypt(b"\x00" * 100, "song.kwm")
    assert e.value.code == "DECRYPT_FAILED"


# ---------- keydb ----------

def _plain_db(rows) -> bytes:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA page_size=1024")
    conn.execute("CREATE TABLE ShareFileItems(EncryptionKeyId TEXT, EncryptionKey TEXT)")
    conn.executemany("INSERT INTO ShareFileItems VALUES(?,?)", rows)
    conn.commit()
    data = conn.serialize()
    conn.close()
    return data


def _encrypt_db(plain: bytes) -> bytes:
    enc = bytearray(plain)
    k, v = keydb.page_material(1)
    cipher = AES.new(k, AES.MODE_CBC, v).encrypt(plain[16:1024])
    enc[0:8] = b"\x00" * 8
    enc[8:16] = cipher[:8]
    enc[24:1024] = cipher[8:]  # enc[16:24] 保留明文头字段
    for page in range(2, len(plain) // 1024 + 1):
        s = (page - 1) * 1024
        k, v = keydb.page_material(page)
        enc[s:s + 1024] = AES.new(k, AES.MODE_CBC, v).encrypt(plain[s:s + 1024])
    return bytes(enc)


def _keydb_rows():
    ekey = base64.b64encode(_make_raw(b"D" * 16)).decode()
    rows = [("hash_a\x00\x00", ekey), ("hash_b", ekey), ("hash_empty", ""), ("hash_null", None)]
    rows += [(f"filler_{i}", ekey) for i in range(80)]  # 撑到多页
    return rows, ekey


def test_keydb_encrypted_roundtrip(tmp_path):
    rows, ekey = _keydb_rows()
    plain = _plain_db(rows)
    assert len(plain) > 1024 and len(plain) % 1024 == 0
    # 参考只校验页大小(BE@16)与保留区字段(LE@20)；日志版本字节 18/19 不参与
    assert struct.unpack_from(">H", plain, 16)[0] == 1024
    assert struct.unpack_from("<I", plain, 20)[0] == 0x20204000
    p = tmp_path / "kgmusicv3.db"
    p.write_bytes(_encrypt_db(plain))
    with keydb.KeyDB(p) as kdb:
        assert kdb.lookup("hash_a") == ekey.encode()  # 尾随 NUL 已剥
        assert kdb.lookup("hash_b") == ekey.encode()
        assert kdb.lookup("hash_empty") is None
        assert kdb.lookup("hash_null") is None
        assert kdb.lookup("missing") is None
    assert kdb.lookup("hash_a") is None  # close 后清空


def test_keydb_plain_passthrough(tmp_path):
    rows, ekey = _keydb_rows()
    p = tmp_path / "kgmusicv3.db"
    p.write_bytes(_plain_db(rows))
    with keydb.KeyDB(p) as kdb:
        assert kdb.lookup("hash_b") == ekey.encode()


def test_keydb_wal_rejected(tmp_path):
    rows, _ = _keydb_rows()
    p = tmp_path / "kgmusicv3.db"
    p.write_bytes(_encrypt_db(_plain_db(rows)))
    Path(str(p) + "-wal").write_bytes(b"\x00" * 64)
    with pytest.raises(AppError) as e:
        keydb.KeyDB(p)
    assert e.value.code == "KEY_DB_UNAVAILABLE" and "正在更新" in e.value.message


def test_keydb_small_wal_ok(tmp_path):
    rows, ekey = _keydb_rows()
    p = tmp_path / "kgmusicv3.db"
    p.write_bytes(_encrypt_db(_plain_db(rows)))
    Path(str(p) + "-wal").write_bytes(b"\x00" * 32)
    with keydb.KeyDB(p) as kdb:
        assert kdb.lookup("hash_b") == ekey.encode()


def test_keydb_bad_header_fields(tmp_path):
    rows, _ = _keydb_rows()
    enc = bytearray(_encrypt_db(_plain_db(rows)))
    p = tmp_path / "kgmusicv3.db"
    bad = bytearray(enc)
    struct.pack_into(">H", bad, 16, 2048)
    p.write_bytes(bytes(bad))
    with pytest.raises(AppError) as e:
        keydb.KeyDB(p)
    assert "加密版本尚未支持" in e.value.message
    bad = bytearray(enc)
    struct.pack_into("<I", bad, 20, 0)
    p.write_bytes(bytes(bad))
    with pytest.raises(AppError):
        keydb.KeyDB(p)


def test_keydb_empty_file(tmp_path):
    p = tmp_path / "kgmusicv3.db"
    p.write_bytes(b"")
    with pytest.raises(AppError) as e:
        keydb.KeyDB(p)
    assert e.value.code == "KEY_DB_UNAVAILABLE"


def test_find_key_dbs(tmp_path, monkeypatch):
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    songs = tmp_path / "songs"
    songs.mkdir()
    src = songs / "a.kgm"
    src.write_bytes(b"x")
    near = songs / "kgmusicv3.db"
    near.write_bytes(b"x")
    deep = appdata / "KuGou8" / "d1" / "d2" / "d3"
    deep.mkdir(parents=True)
    (deep / "KGMusicV3.db").write_bytes(b"x")  # 大小写不敏感
    too_deep = deep / "d4"
    too_deep.mkdir()
    (too_deep / "kgmusicv3.db").write_bytes(b"x")
    found = keydb.find_key_dbs(str(src))
    assert found[0] == near  # 源目录优先
    assert deep / "KGMusicV3.db" in found
    assert too_deep / "kgmusicv3.db" not in found  # 深度 3 截止


# ---------- detect_format ----------

def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_detect_magics(tmp_path):
    assert converter.detect_format(_write(tmp_path, "a.ncm", b"CTENFDAM" + b"\x00" * 64)) == "ncm"
    assert converter.detect_format(_write(tmp_path, "a.kgm", kgm.KGM_MAGIC + b"\x00" * 64)) == "kgm"
    assert converter.detect_format(_write(tmp_path, "a.kgma", kgm.KGM_MAGIC + b"\x00" * 64)) == "kgma"
    assert converter.detect_format(_write(tmp_path, "a.kgg", kgm.KGM_MAGIC + b"\x00" * 64)) == "kgg"
    assert converter.detect_format(_write(tmp_path, "a.vpr", kgm.VPR_MAGIC + b"\x00" * 64)) == "vpr"
    assert converter.detect_format(_write(tmp_path, "a.bin", kgm.VPR_MAGIC + b"\x00" * 64)) == "vpr"
    assert converter.detect_format(_write(tmp_path, "a.kwm", _kwm_file(_fake_mp3(2)))) == "kwm"


def test_detect_qmc_and_plain(tmp_path):
    tag = b"ekey,1,0"
    qtag = b"\x00" * 32 + tag + struct.pack(">I", len(tag)) + b"QTag"
    assert converter.detect_format(_write(tmp_path, "a.qmc0", qtag)) == "qmc"
    assert converter.detect_format(_write(tmp_path, "a.xyz", qtag)) == "qmc"
    assert converter.detect_format(_write(tmp_path, "a.mflac1", b"\x00" * 64)) == "qmc"
    assert converter.detect_format(_write(tmp_path, "a.kgm", _fake_mp3(2))) == "mp3"  # 加密扩展名放行
    assert converter.detect_format(_write(tmp_path, "a.flac", _fake_flac())) == "flac"
    assert converter.detect_format(_write(tmp_path, "a.wav", b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 32)) == "wav"
    assert converter.detect_format(_write(tmp_path, "a.ogg", b"OggS\x00" + b"\x00" * 32)) == "ogg"
    assert converter.detect_format(_write(tmp_path, "a.m4a", b"\x00\x00\x00\x20ftyp" + b"\x00" * 32)) == "m4a"
    assert converter.detect_format(_write(tmp_path, "a.mp3", b"\x00" * 64)) == "mp3"  # 扩展名兜底
    with pytest.raises(AppError) as e:
        converter.detect_format(_write(tmp_path, "a.xyz", b"\x00" * 64))
    assert e.value.code == "UNSUPPORTED_FORMAT"


# ---------- converter.run 集成 ----------

def _ctx(db, bus, tmp_path, src, target="mp3", name=None):
    job = db.insert_job("convert", str(src), {"target": target, "source_name": name or src.name})
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    ctx = JobContext(job["id"], job, db, asyncio.Event(), work,
                     {"download_dir": str(tmp_path / "out")}, None, None, bus)
    return job, ctx


def _success(db, job):
    j = db.get_job(job["id"])
    assert j["status"] == "success", j
    out = Path(j["output_path"])
    assert out.is_file() and out.stat().st_size > 0
    return out


async def test_convert_plain_wav_to_mp3(db, bus, tmp_path, tone_wav):
    src = tone_wav()
    job, ctx = _ctx(db, bus, tmp_path, src, name="测试歌曲.wav")
    await converter.run(job, ctx)
    out = _success(db, job)
    assert out.suffix == ".mp3" and out.name.startswith("测试歌曲_")
    assert ncm.audio_format(out.read_bytes()) == "mp3"


async def test_convert_plain_wav_to_wav(db, bus, tmp_path, tone_wav):
    src = tone_wav()
    job, ctx = _ctx(db, bus, tmp_path, src, target="wav")
    await converter.run(job, ctx)
    out = _success(db, job)
    data = out.read_bytes()
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    assert struct.unpack_from("<I", data, 4)[0] == len(data) - 8


async def test_convert_ncm_to_wav(db, bus, tmp_path):
    mp3 = _real_mp3(tmp_path / "src.mp3")
    src = _write(tmp_path, "歌曲.ncm", _make_ncm(mp3))
    job, ctx = _ctx(db, bus, tmp_path, src, target="wav", name="歌曲.ncm")
    await converter.run(job, ctx)
    out = _success(db, job)
    assert out.suffix == ".wav"


async def test_convert_qmc_to_mp3(db, bus, tmp_path):
    mp3 = _real_mp3(tmp_path / "src.mp3")
    enc = qmc.qmc_transform(mp3, None)
    src = _write(tmp_path, "歌曲.qmc0", enc)
    job, ctx = _ctx(db, bus, tmp_path, src)
    await converter.run(job, ctx)
    assert _success(db, job).suffix == ".mp3"


async def test_convert_kwm_to_mp3(db, bus, tmp_path):
    mp3 = _real_mp3(tmp_path / "src.mp3")
    plain = mp3 + b"\x00" * ((-len(mp3)) % 32) + b"\x00" * 64  # 尾部对齐零区供密钥恢复
    src = _write(tmp_path, "歌曲.kwm", _kwm_file(plain))
    job, ctx = _ctx(db, bus, tmp_path, src)
    await converter.run(job, ctx)
    assert _success(db, job).suffix == ".mp3"


async def test_convert_kgm_v2_never_touches_keydb(db, bus, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("v2 不应触碰密钥库")
    monkeypatch.setattr(keydb, "find_key_dbs", boom)
    monkeypatch.setattr(keydb, "KeyDB", boom)
    mp3 = _real_mp3(tmp_path / "src.mp3")
    header = _kgm_file(b"", 2)[:1024]
    src = _write(tmp_path, "歌曲.kgm", _kgm_file(_legacy_encrypt(mp3, header), 2))
    job, ctx = _ctx(db, bus, tmp_path, src)
    await converter.run(job, ctx)
    assert _success(db, job).suffix == ".mp3"


async def test_convert_kgm_v5_with_keydb(db, bus, tmp_path, monkeypatch):
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    raw = _make_raw(b"E" * 16)
    ekey = base64.b64encode(raw)
    key = qmc.derive_key(ekey, allow_raw=True)
    rows = [("testhash123", ekey.decode())]
    kdb_path = tmp_path / "kgmusicv3.db"
    kdb_path.write_bytes(_encrypt_db(_plain_db(rows)))
    mp3 = _real_mp3(tmp_path / "src.mp3")
    cipher = qmc.qmc_transform(mp3, key)
    src = _write(tmp_path, "歌曲.kgg", _kgm_file(cipher, 5, key_id=b"testhash123"))
    job, ctx = _ctx(db, bus, tmp_path, src)
    await converter.run(job, ctx)
    assert _success(db, job).suffix == ".mp3"


async def test_convert_kgm_v5_no_keydb(db, bus, tmp_path, monkeypatch):
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    src = _write(tmp_path, "歌曲.kgm", _kgm_file(_fake_mp3(4), 5, key_id=b"abc123"))
    job, ctx = _ctx(db, bus, tmp_path, src)
    with pytest.raises(AppError) as e:
        await converter.run(job, ctx)
    assert e.value.code == "KEY_DB_UNAVAILABLE" and "未找到" in e.value.message


async def test_convert_kgm_v5_key_missing_in_db(db, bus, tmp_path, monkeypatch):
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    rows, _ = _keydb_rows()
    (tmp_path / "kgmusicv3.db").write_bytes(_encrypt_db(_plain_db(rows)))
    src = _write(tmp_path, "歌曲.kgm", _kgm_file(_fake_mp3(4), 5, key_id=b"not_in_db"))
    job, ctx = _ctx(db, bus, tmp_path, src)
    with pytest.raises(AppError) as e:
        await converter.run(job, ctx)
    assert e.value.code == "KEY_DB_UNAVAILABLE" and "没有这首歌曲的密钥" in e.value.message


async def test_convert_kgm_v5_wal_busy(db, bus, tmp_path, monkeypatch):
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    rows, ekey = _keydb_rows()
    kdb_path = tmp_path / "kgmusicv3.db"
    kdb_path.write_bytes(_encrypt_db(_plain_db(rows)))
    Path(str(kdb_path) + "-wal").write_bytes(b"\x00" * 64)
    src = _write(tmp_path, "歌曲.kgm", _kgm_file(_fake_mp3(4), 5, key_id=b"hash_a"))
    job, ctx = _ctx(db, bus, tmp_path, src)
    with pytest.raises(AppError) as e:
        await converter.run(job, ctx)
    assert e.value.code == "KEY_DB_UNAVAILABLE" and "正在更新" in e.value.message


async def test_convert_unsupported_ext(db, bus, tmp_path):
    src = _write(tmp_path, "a.xyz", b"\x00" * 64)
    job, ctx = _ctx(db, bus, tmp_path, src)
    with pytest.raises(AppError) as e:
        await converter.run(job, ctx)
    assert e.value.code == "UNSUPPORTED_FORMAT"


async def test_convert_bad_target(db, bus, tmp_path, tone_wav):
    src = tone_wav()
    job, ctx = _ctx(db, bus, tmp_path, src, target="flac")
    with pytest.raises(AppError) as e:
        await converter.run(job, ctx)
    assert e.value.code == "UNSUPPORTED_FORMAT"


async def test_convert_missing_input(db, bus, tmp_path):
    job, ctx = _ctx(db, bus, tmp_path, tmp_path / "nope.mp3")
    with pytest.raises(AppError) as e:
        await converter.run(job, ctx)
    assert e.value.code == "NOT_FOUND"


async def test_convert_cancelled(db, bus, tmp_path, tone_wav):
    src = tone_wav()
    job, ctx = _ctx(db, bus, tmp_path, src)
    ctx.cancel.set()
    with pytest.raises(AppError) as e:
        await converter.run(job, ctx)
    assert e.value.code == "CANCELLED"