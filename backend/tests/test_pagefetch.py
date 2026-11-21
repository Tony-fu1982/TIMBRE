"""PageFetch 接缝：匿名模式（SPEC §4.7 匿名 rights 页内化）、只读白名单边界。不触网。"""
from __future__ import annotations

import base64
import json
import time

import pytest

from app.errors import SunoAPIError
from app.pagefetch import PageFetch, is_readonly


class FakePage:
    def __init__(self, result=None):
        self.calls = []
        self.token = "fixture-token"
        self.result = result or {"ok": True, "status": 200, "json": {"key": "k"},
                                 "text": "", "retryAfter": 0}

    async def evaluate(self, expr, arg=None):
        self.calls.append((expr, arg))
        if expr == "async () => window.__gj.getToken()":
            return self.token
        if isinstance(arg, dict) and "reqId" in arg:
            return self.result
        return None


class FakeSession:
    def __init__(self, page):
        self.page = page

    async def ensure(self, visible=False):
        return self.page

    async def check_page(self, page):
        pass


def _request_args(page):
    return [a for _, a in page.calls if isinstance(a, dict) and "reqId" in a]


async def test_anonymous_skips_token_and_flags_payload():
    page = FakePage()
    pf = PageFetch(FakeSession(page))
    res = await pf.request("POST", "/api/mango/rights",
                           {"content_params": {"content_id": "x"}}, anonymous=True)
    assert res["json"] == {"key": "k"}
    arg = _request_args(page)[0]
    assert arg["anonymous"] is True and arg["token"] == ""
    assert "async () => window.__gj.getToken()" not in [e for e, _ in page.calls]  # 从未取 Clerk token


async def test_anonymous_401_no_refresh_retry():
    page = FakePage({"ok": False, "status": 401, "json": None, "text": "", "retryAfter": 0})
    pf = PageFetch(FakeSession(page))
    with pytest.raises(SunoAPIError) as ei:
        await pf.request("POST", "/api/mango/rights", {}, anonymous=True)
    assert ei.value.code == "NOT_LOGGED_IN"
    assert len(_request_args(page)) == 1  # 不刷 token 重试
    assert "async () => window.__gj.getToken()" not in [e for e, _ in page.calls]


def test_readonly_prefix_boundary():
    assert is_readonly("GET", "/api/anything")
    assert is_readonly("POST", "/api/feed/v3")
    assert is_readonly("POST", "/api/feed/v3?x=1")
    assert is_readonly("POST", "/api/feed/v3/abc")
    assert is_readonly("POST", "/api/mango/rights")
    assert is_readonly("POST", "/api/c/check")
    assert not is_readonly("POST", "/api/feed/v3evil")
    assert not is_readonly("POST", "/api/mango/rights-x")
    assert not is_readonly("POST", "/api/generate/v2-web/")


@pytest.mark.parametrize("retry_after,delays", [
    ("7", [7.0, 7.0, 7.0]),      # Retry-After 优先
    ("0.5", [0.5, 0.5, 0.5]),    # 即使小于退避表也遵循 Retry-After
    (0, [2, 6, 10]),             # 无 Retry-After → 2/6/10 退避
    (None, [2, 6, 10]),
    ("abc", [2, 6, 10]),         # 非法值按无处理
])
async def test_429_retry_after_first(monkeypatch, retry_after, delays):
    """SPEC §4.3：429 优先遵循 Retry-After，否则 2/6/10s，最多 3 次。"""
    seen = []

    async def fake_sleep(s):
        seen.append(s)

    monkeypatch.setattr("app.pagefetch.asyncio.sleep", fake_sleep)
    page = FakePage({"ok": False, "status": 429, "json": None, "text": "",
                     "retryAfter": retry_after})
    pf = PageFetch(FakeSession(page))
    with pytest.raises(SunoAPIError) as ei:
        await pf.request("GET", "/api/clip/x", anonymous=True)  # 匿名跳过 Clerk token，聚焦 429
    assert ei.value.code == "RATE_LIMITED"
    assert seen == delays
    assert len(_request_args(page)) == 4  # 首次 + 3 次重试后放弃


def _jwt(exp):
    p = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{p}.s"


def _token_calls(page):
    return sum(e == "async () => window.__gj.getToken()" for e, _ in page.calls)


async def test_token_cache_follows_jwt_exp():
    """2026-11 漂移：Clerk JWT 仅 ~60s，缓存按 exp-15s 刷新；exp 解析失败落保守缓存。"""
    page = FakePage()
    page.token = _jwt(int(time.time()) + 60)
    pf = PageFetch(FakeSession(page))
    await pf.token()
    await pf.token()
    n = _token_calls(page)
    assert n == 1  # exp 内命中缓存
    page.token = _jwt(int(time.time()) - 5)
    pf2 = PageFetch(FakeSession(page))
    await pf2.token()
    await pf2.token()
    n2 = _token_calls(page)
    assert n2 == n + 2  # 已过期，每次重取
    page.token = "not-a-jwt"
    pf3 = PageFetch(FakeSession(page))
    await pf3.token()
    await pf3.token()
    assert _token_calls(page) == n2 + 1  # 解析失败 → 50s 保守缓存
