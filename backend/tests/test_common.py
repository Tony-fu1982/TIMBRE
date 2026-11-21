"""URL 白名单 / 候选优先级 / 魔数嗅探（issue 01/02/03 接缝）。"""
from __future__ import annotations

import pytest

from app.errors import AppError
from app.suno import common

UUID = "12345678-1234-1234-1234-123456789abc"


@pytest.mark.parametrize("url", [
    f"https://suno.com/song/{UUID}",
    f"https://www.suno.com/song/{UUID}",
    "https://suno.com/s/AbCdEf12",
])
def test_valid_urls(url):
    assert common.validate_suno_url(url) == url


@pytest.mark.parametrize("url", [
    f"http://suno.com/song/{UUID}",                      # 非 https
    f"https://suno.com.evil.com/song/{UUID}",            # 后缀 spoof
    f"https://evil-suno.com/song/{UUID}",
    f"https://user:pass@suno.com/song/{UUID}",           # userinfo
    f"https://suno.com:8443/song/{UUID}",                # 非默认端口
    f"https://auth.suno.com/song/{UUID}",                # 登录域不是歌曲页
    "https://suno.com/",                                 # 非歌曲路径
    "https://suno.com/create",
    "",
])
def test_invalid_urls(url):
    with pytest.raises(AppError) as e:
        common.validate_suno_url(url)
    assert e.value.code == "INVALID_URL"


def test_extract_song_id():
    assert common.extract_song_id(f"https://suno.com/song/{UUID}") == UUID
    html = ('<a href="https://suno.com/song/00000000-0000-0000-0000-000000000000">推荐</a>'
            f'<link rel="canonical" href="https://suno.com/song/{UUID}">')
    assert common.extract_song_id(html) == UUID
    html2 = f'<meta property="og:url" content="https://suno.com/song/{UUID.upper()}"/>'
    assert common.extract_song_id(html2) == UUID
    with pytest.raises(AppError):
        common.extract_song_id("<html>nothing</html>")


def test_extract_song_id_offsite_skipped():
    """SPEC §4.7：提取 songId 的每个 URL 都过 §4.1 校验；站外 canonical/正文链接跳过。"""
    html = (f'<link rel="canonical" href="https://evil.com/song/{UUID}">'
            f'<meta property="og:url" content="https://suno.com/song/{UUID}"/>')
    assert common.extract_song_id(html) == UUID
    with pytest.raises(AppError):
        common.extract_song_id(f'<a href="https://evil.com/song/{UUID}">x</a>')
    with pytest.raises(AppError):  # 只认绝对 https URL（相对 canonical 不再支持）
        common.extract_song_id(f'<link rel="canonical" href="/song/{UUID}">')


def test_merge_candidates_priority_and_dedupe():
    clip = {
        "audio_url": "https://cdn1.suno.ai/x.mp3",
        "media_urls": [
            {"url": "https://cdn1.suno.ai/x.mp3", "content_type": "audio/mpeg"},  # 与 legacy 去重
            {"url": "https://cdn1.suno.ai/x.m4a", "content_type": "audio/m4a-opus",
             "encoding": "aes-ctr", "delivery": "progressive"},
            {"url": "https://cdn1.suno.ai/x.webm", "content_type": "audio/webm-opus",
             "encoding": "aes-ctr", "delivery": "progressive"},
            {"url": "https://cdn1.suno.ai/api/forbidden"},  # 弃用
            {"url": "https://cdn1.suno.ai/x.aac", "content_type": "audio/aac"},
        ],
    }
    cands = common.merge_candidates(clip)
    urls = [c["url"] for c in cands]
    assert "https://cdn1.suno.ai/api/forbidden" not in urls
    assert len(urls) == len(set(urls)) == 4
    assert urls[0].endswith(".m4a") and cands[0]["encrypted"]
    assert urls[1].endswith(".webm")
    assert urls[2].endswith(".mp3")
    assert urls[3].endswith(".aac")


def test_sniff_container():
    assert common.sniff_container(b"\x00\x00\x00\x18ftypM4A ") == "mp4"
    assert common.sniff_container(b"\x1a\x45\xdf\xa3rest") == "webm"
    assert common.sniff_container(b"OggS\x00\x02") == "ogg"
    assert common.sniff_container(b"ID3\x04\x00") == "mp3"
    assert common.sniff_container(b"\xff\xfb\x90\x00") == "mp3"
    assert common.sniff_container(b"fLaC\x00\x00") == "flac"
    assert common.sniff_container(b"RIFF\x24\x08\x00\x00WAVEfmt ") == "wav"
    assert common.sniff_container(b"garbage!") == "unknown"


def test_cdn_whitelist():
    assert common.cdn_allowed("https://cdn1.suno.ai/x.mp3")
    assert common.cdn_allowed("https://d2lwuy8qc234o3.cloudfront.net/1/clip/x.m4a")
    assert not common.cdn_allowed("https://evil.com/x.mp3")
    assert not common.cdn_allowed("https://suno.ai.evil.com/x.mp3")
    assert not common.cdn_allowed("file:///C:/x.mp3")
    assert not common.cdn_allowed("http://cdn1.suno.ai/x.mp3")          # 仅 https
    assert not common.cdn_allowed("https://evil.cloudfront.net/x.mp3")  # 不开整个 cloudfront
    assert not common.cdn_allowed("https://user:pass@cdn1.suno.ai/x.mp3")


def _mock_transport(monkeypatch, handler):
    import httpx
    real = httpx.AsyncClient
    monkeypatch.setattr(common.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


async def test_download_cdn_redirect_within_whitelist(tmp_path, monkeypatch):
    import httpx
    payload = b"\x00" * 600

    def handler(request):
        if request.url.host == "cdn1.suno.ai":
            return httpx.Response(302, headers={"location": "https://cdn2.suno.ai/x.mp3"})
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=payload)

    _mock_transport(monkeypatch, handler)
    dest = await common.download_cdn("https://cdn1.suno.ai/x.mp3", tmp_path / "o.mp3")
    assert dest.read_bytes() == payload


async def test_download_cdn_redirect_escape_blocked(tmp_path, monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(302, headers={"location": "https://evil.com/x.mp3"})

    _mock_transport(monkeypatch, handler)
    with pytest.raises(AppError) as ei:
        await common.download_cdn("https://cdn1.suno.ai/x.mp3", tmp_path / "o.mp3")
    assert ei.value.code == "INVALID_URL"
    assert not (tmp_path / "o.mp3").exists()
