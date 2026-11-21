"""页内请求执行器（SPEC §4.3）：window.__gj 随用随装；策略全在 Python 层。
只读 = GET + 白名单查询 POST（feed/v3、mango/rights、c/check）；副作用 POST 绝不自动重放。"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid

from .errors import AppError, SunoAPIError

PAGE_RUNTIME = r"""
() => {
  if (window.__gj) return true;
  const aborts = (window.__gjAborts = window.__gjAborts || {});
  window.__gj = {
    async getToken() {
      try {
        const s = window.Clerk && window.Clerk.session;
        if (s) return await s.getToken() || '';
      } catch (e) {}
      // 2026-11 站点漂移：window.Clerk 不再注入，改走 Clerk FAPI cookie 链取 JWT。
      // auth.suno.com 持有 active session（clerk.suno.com 实测 sessions 为空，作备域）。
      // tokens 响应现版直接 {jwt}，旧版 {response:{jwt}}，两形态兼容。
      for (const host of ['https://auth.suno.com', 'https://clerk.suno.com']) {
        try {
          const cr = await fetch(host + '/v1/client', { credentials: 'include', redirect: 'error' });
          if (!cr.ok) continue;
          const cj = await cr.json();
          const s = (((cj.response || cj).sessions) || []).find(x => x && x.status === 'active');
          if (!s) continue;
          const tr = await fetch(host + '/v1/client/sessions/' + s.id + '/tokens',
                                 { method: 'POST', credentials: 'include', redirect: 'error' });
          if (!tr.ok) continue;
          const tj = await tr.json();
          const jwt = (tj.response || tj).jwt || tj.jwt;
          if (jwt) return jwt;
        } catch (e) {}
      }
      return '';
    },
    async request({ method, path, body, timeoutMs = 20000, reqId = '', token = '', anonymous = false }) {
      let deviceId = '';
      try { deviceId = JSON.parse(localStorage.getItem('ajs_anonymous_id') || '""'); } catch (e) {}
      if (!/^[A-Za-z0-9_-]{6,128}$/.test(deviceId)) deviceId = crypto.randomUUID();
      const headers = {
        'Accept': 'application/json',
        'device-id': deviceId,
        'browser-token': JSON.stringify({ token: btoa(JSON.stringify({ timestamp: Date.now() })) }),
      };
      if (!anonymous) headers['Authorization'] = 'Bearer ' + token;
      if (body !== undefined && body !== null) headers['Content-Type'] = 'application/json';
      const ctrl = new AbortController();
      if (reqId) aborts[reqId] = ctrl;
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      try {
        // 2026-11 站点漂移：studio-api-prod 网关对用户类 API 已废弃（带新 token 仍 401），
        // 站点整体迁到主站同源 /a/api/*（Bearer 实测 200）。调用方 path 约定 /api/... 不变，
        // 此处统一加 /a 前缀；page 由 check_page 保证在 suno.com，相对路径即同源。
        const res = await fetch('/a' + path, {
          method, headers, credentials: anonymous ? 'omit' : 'include',
          body: (body === undefined || body === null) ? undefined : JSON.stringify(body),
          signal: ctrl.signal,
        });
        const text = await res.text();
        let json = null; try { json = JSON.parse(text); } catch (e) {}
        return { ok: res.ok, status: res.status, json, text: text.slice(0, 2000),
                 retryAfter: res.headers.get('retry-after') || '' };
      } catch (e) {
        const aborted = ctrl.signal.aborted;
        return { ok: false, status: 0, json: null,
                 text: aborted ? 'TIMEOUT_OR_ABORTED' : String(e && e.message || e) };
      } finally {
        clearTimeout(timer);
        if (reqId && aborts[reqId] === ctrl) delete aborts[reqId];
      }
    },
    abort(reqId) {
      const c = aborts[reqId];
      if (c) { try { c.abort('CANCELLED'); } catch (e) {} delete aborts[reqId]; return true; }
      return false;
    },
  };
  return true;
}
"""

READONLY_POST_PREFIXES = ("/api/feed/v3", "/api/mango/rights", "/api/c/check")
BACKOFF_S = (2, 6, 10)  # SPEC §4.3：2/6/10s，最多 3 次（覆盖 doc 08 的 14s 尾巴）


def is_readonly(method: str, path: str) -> bool:
    if method.upper() == "GET":
        return True
    # 前缀须落在路径分量边界上，防 /api/feed/v3evil 之类混入白名单
    return method.upper() == "POST" and any(
        path == p or path.startswith(p + "/") or path.startswith(p + "?")
        for p in READONLY_POST_PREFIXES)


def _jwt_exp(tok: str) -> float:
    """解析 JWT exp 并提前 15s 刷新；解析失败回 0（调用方落保守缓存）。"""
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) - 15 if exp else 0.0
    except Exception:
        return 0.0


class PageFetch:
    TIMEOUT_MS = 20_000

    def __init__(self, session):
        self.session = session
        self._token, self._token_exp = "", 0.0

    async def token(self, force: bool = False) -> str:
        # 2026-11 漂移：Clerk JWT 有效期约 60s，旧 300s 缓存必撞 401；改按 exp-15s 缓存，
        # exp 解析失败落 50s 保守值。401 强制刷新分支保持不变。
        now = time.time()
        if not force and self._token and now < self._token_exp:
            return self._token
        page = await self.session.ensure()
        await self.session.check_page(page)
        await page.evaluate(PAGE_RUNTIME)
        self._token = await page.evaluate("async () => window.__gj.getToken()")
        if not self._token:
            raise SunoAPIError("NOT_LOGGED_IN", 401)
        self._token_exp = _jwt_exp(self._token) or (now + 50)
        return self._token

    async def request(self, method: str, path: str, body=None, *,
                      timeout_ms: int = TIMEOUT_MS, cancel_event=None,
                      anonymous: bool = False) -> dict:
        """返回 {ok,status,json,text,retryAfter}；失败抛 SunoAPIError（handler 决定语义）。
        渲染轮询等长任务自行传大 timeout_ms；取消经页内 AbortRegistry 即时生效。
        anonymous=True：不带 Authorization、credentials omit、401 不刷 token（匿名 rights，SPEC §4.7）。"""
        method = method.upper()
        readonly = is_readonly(method, path)
        page = await self.session.ensure()
        await self.session.check_page(page)
        await page.evaluate(PAGE_RUNTIME)  # 幂等，防刷新后丢失

        refreshed = False
        get_retried = False
        backoff_i = 0
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise AppError("CANCELLED")
            req_id = uuid.uuid4().hex[:12]
            token = "" if anonymous else await self.token(force=refreshed)
            watcher = None
            if cancel_event is not None:
                async def _watch():
                    await cancel_event.wait()
                    try:
                        await page.evaluate("(id) => window.__gj.abort(id)", req_id)
                    except Exception:
                        pass
                watcher = asyncio.create_task(_watch())
            try:
                res = await page.evaluate(
                    "async (a) => window.__gj.request(a)",
                    {"method": method, "path": path, "body": body, "timeoutMs": timeout_ms,
                     "reqId": req_id, "token": token, "anonymous": anonymous},
                )
            except Exception as exc:
                code = "NETWORK_ERROR" if readonly else "NETWORK_RESULT_UNKNOWN"
                raise SunoAPIError(code, 0) from exc
            finally:
                if watcher:
                    watcher.cancel()
            if cancel_event is not None and cancel_event.is_set():
                if not readonly and not res["status"]:
                    raise SunoAPIError("NETWORK_RESULT_UNKNOWN", 0)
                if readonly:
                    raise AppError("CANCELLED")

            if res["ok"]:
                return res
            status = res["status"]
            if status == 401:
                if readonly and not refreshed and not anonymous:
                    refreshed = True  # 强制刷新 token 重试一次（SPEC §4.3）
                    continue
                raise SunoAPIError("NOT_LOGGED_IN", 401)
            if status == 429:
                if readonly and backoff_i < len(BACKOFF_S):
                    try:
                        ra = float(res.get("retryAfter") or 0)
                    except ValueError:
                        ra = 0.0
                    delay = ra if ra > 0 else BACKOFF_S[backoff_i]  # SPEC §4.3：优先遵循 Retry-After
                    backoff_i += 1
                    await asyncio.sleep(delay)
                    continue
                raise SunoAPIError("RATE_LIMITED", 429)
            if status > 0:
                raise SunoAPIError("SUNO_HTTP_ERROR", status)
            # status=0 传输层失败/超时
            if method == "GET" and not get_retried:
                get_retried = True  # GET 传输失败重试一次（SPEC §4.3）
                continue
            if not readonly:
                raise SunoAPIError("NETWORK_RESULT_UNKNOWN", 0)
            raise SunoAPIError("NETWORK_ERROR", 0)
