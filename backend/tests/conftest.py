"""共享夹具：tmp 数据库、事件总线、ffmpeg 正弦波音频。测试不触网、不开真 Chrome。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import config, ffmedia
from app.db import Database
from app.events import EventBus


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def tone_wav(tmp_path):
    def make(name: str = "tone.wav", sr: int = 44100, seconds: float = 2.0) -> Path:
        p = tmp_path / name
        subprocess.run(
            [ffmedia.FFMPEG, "-y", "-f", "lavfi", "-i",
             f"sine=frequency=440:sample_rate={sr}:duration={seconds}",
             "-c:a", "pcm_s16le", str(p)],
            check=True, capture_output=True)
        return p
    return make


@pytest.fixture
def isolate_env(tmp_path, monkeypatch):
    """把 .env / 数据目录指到 tmp，避免测试污染真实运行时。"""
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "TMP_DIR", tmp_path / "data" / "tmp")
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "data" / "tmp" / "uploads")
    (tmp_path / "data" / "tmp" / "uploads").mkdir(parents=True)
    return tmp_path