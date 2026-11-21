"""Suno 下载公共层：URL 白名单、分享页解析、音频候选优先级（studio-audio-url.cjs 逐字移植）、CDN 下载。"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..chrome_session import is_suno_url
from ..errors import AppError

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# ponytail: 白名单只收 Suno 自有域 + SPEC §4.7 回退链里那一个实测 cloudfront 分发域；
# 不开整个 cloudfront.net（任何租户都可挂在该后缀下）。Suno 换 CDN 域时抓包后在此扩充。
CDN_HOST_SUFFIXES = ("suno.ai", "suno.com")
CDN_FALLBACK_HOST = "d2lwuy8qc234o3.cloudfront.net"
CDN_FALLBACK = (
    "https://d2lwuy8qc234o3.cloudfront.net/1/clip/{id}.m4a",
    "https://cdn1.suno.ai/{id}.mp3",
    "https://cdn2.suno.ai/{id}.mp3",
)
MAX_MEDIA_BYTES = 250 * 1024 * 1024
MIN_MEDIA_BYTES = 512


def validate_suno_url(url: str) -> str:
    """歌曲链接校验：https + 默认端口 + 无 userinfo + 主机精确匹配 + /song/ 或 /s/ 路径。"""
    u = (url or "").strip()
    if not is_suno_url(u):
        raise AppError("INVALID_URL")
    path = urlsplit(u).path
    if not (path.startswith("/song/") or path.startswith("/s/")):
        raise AppError("INVALID_URL")
    return u


def cdn_allowed(url: str) -> bool:
    """仅 https、无 userinfo、Suno 自有域或唯一实测回退分发域（SPEC §7:246 直连边界）。"""
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    if u.scheme != "https" or u.username or u.password:
        return False
    host = (u.hostname or "").lower()
    return (host == CDN_FALLBACK_HOST
            or any(host == s or host.endswith("." + s) for s in CDN_HOST_SUFFIXES))


async def fetch_share_html(url: str, timeout: float = 20) -> str:
    """Python 读分享页 HTML；≤5 次重定向，每一跳都过白名单（SPEC §4.7）。"""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
        for _ in range(6):
            if not is_suno_url(url):
                raise AppError("INVALID_URL", "分享链接重定向到了非 Suno 站点，已中止。")
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location", "")
                if not loc:
                    raise AppError("INVALID_URL")
                url = str(httpx.URL(url).join(loc))
                continue
            if r.status_code != 200:
                raise AppError("INVALID_URL", f"分享页返回 {r.status_code}，请确认链接有效。")
            return r.text
    raise AppError("INVALID_URL", "分享链接重定向次数过多。")


class _SongMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.canonical: list[str] = []
        self.og: list[str] = []

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "link" and "canonical" in (fields.get("rel") or "").lower().split():
            self.canonical.append(fields.get("href") or "")
        if tag == "meta" and (fields.get("property") or "").lower() == "og:url":
            self.og.append(fields.get("content") or "")


def extract_song_id(html_or_url: str) -> str:
    """只信直链、canonical 或 og:url，避免取到页面中的推荐曲目。"""
    if is_suno_url(html_or_url):
        candidates = [html_or_url]
    else:
        parser = _SongMetadata()
        parser.feed(html_or_url)
        candidates = parser.canonical + parser.og
    for url in candidates:
        if not is_suno_url(url):
            continue
        m = re.fullmatch(r"/song/([0-9a-f-]{36})/?", urlsplit(url).path, re.I)
        if m and UUID_RE.fullmatch(m.group(1)):
            return m.group(1).lower()
    raise AppError("INVALID_URL", "无法从链接或页面中解析出歌曲 ID。")


# ---- 候选合并（studio-audio-url.cjs 忠实移植）----

def _normalize_audio_url(value) -> str:
    if not isinstance(value, str):
        return ""
    v = value.strip()
    if v.startswith("//"):
        v = "https:" + v
    if not re.match(r"^https?://", v, re.I):
        return ""
    try:
        path = urlsplit(v).path.rstrip("/").lower()
    except ValueError:
        return ""
    if path == "/api/forbidden":
        return ""
    return v


def _priority(c: dict) -> int:
    try:
        path = urlsplit(c["url"]).path.lower()
    except ValueError:
        path = ""
    progressive = not c["delivery"] or c["delivery"] == "progressive"
    ct = c["contentType"]
    enc_m4a = "m4a-opus" in ct
    enc_webm = "webm-opus" in ct
    mp3 = "mp3" in ct or path.endswith(".mp3")
    browser = bool(re.search(r"m4a|webm|opus|aac", ct)) or bool(re.search(r"\.(m4a|webm|opus|aac)$", path))
    if c["encrypted"] and progressive and enc_m4a:
        return 0
    if c["encrypted"] and progressive and enc_webm:
        return 1
    if mp3:
        return 2
    if c["encrypted"] and progressive:
        return 3
    if c["encrypted"]:
        return 4
    if browser:
        return 5
    return 6


def merge_candidates(clip: dict) -> list[dict]:
    """media_urls（clip+metadata）+ 12 legacy 字段，去重合并后按优先级排序。
    返回 [{url,contentType,delivery,encoding,encrypted,source}]。"""
    if not isinstance(clip, dict):
        return []
    metadata = clip.get("metadata") if isinstance(clip.get("metadata"), dict) else {}
    by_url: dict[str, dict] = {}

    def add(url, content_type="", delivery="", encoding="", encrypted=False, source=""):
        normalized = _normalize_audio_url(url)
        if not normalized:
            return
        ct, dl, enc = str(content_type or "").lower(), str(delivery or "").lower(), str(encoding or "")
        encrypted = bool(encrypted or enc)
        if normalized in by_url:
            e = by_url[normalized]
            e["contentType"] = e["contentType"] or ct
            e["delivery"] = e["delivery"] or dl
            e["encoding"] = e["encoding"] or enc
            e["encrypted"] = e["encrypted"] or encrypted
            return
        by_url[normalized] = {"url": normalized, "contentType": ct, "delivery": dl,
                              "encoding": enc, "encrypted": encrypted, "source": source,
                              "_index": len(by_url)}

    for source_name, src in (("clip.media_urls", clip), ("metadata.media_urls", metadata)):
        items = src.get("media_urls") or src.get("mediaUrls") or []
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, str):
                add(item, source=source_name)
            elif isinstance(item, dict):
                add(item.get("url") or item.get("audio_url") or item.get("audioUrl") or "",
                    item.get("content_type") or item.get("contentType") or "",
                    item.get("delivery") or "", item.get("encoding") or "",
                    bool(item.get("encoding")), source_name)

    for source, value in (
        ("clip.stream_audio_url", clip.get("stream_audio_url")),
        ("clip.streaming_audio_url", clip.get("streaming_audio_url")),
        ("metadata.stream_audio_url", metadata.get("stream_audio_url")),
        ("metadata.streaming_audio_url", metadata.get("streaming_audio_url")),
        ("clip.audio_url", clip.get("audio_url")),
        ("clip.audioUrl", clip.get("audioUrl")),
        ("clip.audio", clip.get("audio")),
        ("clip.url", clip.get("url")),
        ("metadata.audio_url", metadata.get("audio_url")),
        ("metadata.audioUrl", metadata.get("audioUrl")),
        ("metadata.audio", metadata.get("audio")),
        ("metadata.url", metadata.get("url")),
    ):
        add(value, source=source)

    ordered = sorted(by_url.values(), key=lambda c: (_priority(c), c["_index"]))
    for c in ordered:
        c.pop("_index")
    return ordered


def sniff_container(head: bytes) -> str:
    """魔数嗅探：ftyp→mp4 / EBML→webm / OggS→ogg / ID3|0xFFEx→mp3 / fLaC→flac / RIFF..WAVE→wav。"""
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head.startswith(b"fLaC"):
        return "flac"
    if len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav"
    return "unknown"


async def download_cdn(url: str, dest: Path, timeout: float = 120, on_progress=None) -> Path:
    """白名单 CDN 流式下载：audio/*、≤250MB、≥512B（SPEC §4.7）。失败抛 AppError。

    on_progress(done_bytes, total_bytes|0)：可选同步回调，逐块上报字节进度（issue 02）。
    """
    if not cdn_allowed(url):
        raise AppError("INVALID_URL", "音频地址不在允许的 CDN 白名单内。")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    total = 0
    try:
        # 手动跟跳：每一跳都复核白名单，防开放重定向把直连引出边界（SPEC §7:246）
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
            for _ in range(6):
                if not cdn_allowed(url):
                    raise AppError("INVALID_URL", "音频地址重定向到了白名单外的主机，已中止。")
                async with c.stream("GET", url) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("location", "")
                        if not loc:
                            raise AppError("NETWORK_ERROR", "CDN 重定向缺少 Location")
                        url = str(httpx.URL(url).join(loc))
                        continue
                    if r.status_code != 200:
                        raise AppError("NETWORK_ERROR", f"CDN 返回 {r.status_code}")
                    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                    # ponytail: 缺 content-type 放行——部分 CDN 不回该头，真实容器由下游 sniff/ffmpeg 判定
                    if ctype and not ctype.startswith("audio/"):
                        raise AppError("NETWORK_ERROR", f"CDN 返回了非音频内容（{ctype}）")
                    try:
                        expected = int(r.headers.get("content-length") or 0)
                    except ValueError:
                        expected = 0
                    with open(tmp, "wb") as f:
                        async for chunk in r.aiter_bytes(65536):
                            total += len(chunk)
                            if total > MAX_MEDIA_BYTES:
                                raise AppError("NETWORK_ERROR", "音频超过 250MB 上限")
                            f.write(chunk)
                            if on_progress:
                                on_progress(total, expected)
                    break
            else:
                raise AppError("NETWORK_ERROR", "CDN 重定向次数过多")
        if total < MIN_MEDIA_BYTES:
            raise AppError("NETWORK_ERROR", "下载内容过小，疑似无效音频")
        tmp.replace(dest)
        return dest
    finally:
        tmp.unlink(missing_ok=True)
