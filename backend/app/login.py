"""登录状态机（SPEC §4.2）：页内探针是唯一权威；500ms 轮询/10min 上限。
2026-11 站点漂移：探针从同源 /api/session/ 迁到 Clerk FAPI active-session（见 LOGIN_CHECK）。"""
from __future__ import annotations

import asyncio

from .chrome_session import is_suno_url
from .events import EventBus

# 2026-11 站点漂移：同源 /api/session/ 302→404 恒不可达；/a/api/session/ 匿名也 200 且无
# user 字段——两者都不能当权威。改 fetch Clerk FAPI /v1/client 的 cookie session 列表：
# 存在 active session = 已登录，account 取 session user 邮箱。auth.suno.com 持有 active
# session（clerk.suno.com 实测 sessions 为空，作备域）。200 = 探针可达；未登录也 200
# （sessions 空），恰好区分「没登录」与「网络坏」。
LOGIN_CHECK = """async () => {
  if (!['https://suno.com', 'https://www.suno.com'].includes(location.origin)) return null;
  let err = 'network';
  for (const host of ['https://auth.suno.com', 'https://clerk.suno.com']) {
    try {
      const r = await fetch(host + '/v1/client', { credentials: 'include', redirect: 'error' });
      if (!r.ok) { err = 'http ' + r.status; continue; }
      const j = await r.json();
      const s = (((j.response || j).sessions) || []).find(x => x && x.status === 'active');
      const u = (s && s.user) || null;
      const email = u && Array.isArray(u.email_addresses) && u.email_addresses[0]
        ? u.email_addresses[0].email_address : '';
      return { ready: true, loggedIn: !!s, account: s ? (email || u.id || '') : '', httpStatus: r.status };
    } catch (e) { err = String(e); }
  }
  return { ready: false, loggedIn: false, error: err };
}"""


class LoginFlow:
    def __init__(self, session, db, bus: EventBus):
        self.session, self.db, self.bus = session, db, bus
        row = db.session()
        self.state = "logged_in" if row["state"] == "logged_in" else "not_logged_in"
        self.account = (row["account"] or "") if self.state == "logged_in" else ""
        self._poller: asyncio.Task | None = None
        self._recheck: asyncio.Task | None = None

    def _save(self) -> None:
        self.db.set_session(self.state, self.account)
        self.bus.publish("session", {"state": self.state, "account": self.account})

    async def start_login(self) -> dict:
        """POST /api/session/login：可见窗口打开登录页，启动轮询。"""
        if self._poller and not self._poller.done():
            self._poller.cancel()
        self.state = "login_open"
        self._save()
        try:
            page = await self.session.ensure(visible=True)
            if not is_suno_url(page.url, allow_auth=True):
                await page.goto("https://suno.com/login", wait_until="domcontentloaded", timeout=30000)
        except Exception:
            self.state, self.account = "not_logged_in", ""
            self._save()
            raise
        try:
            await page.bring_to_front()
        except Exception:
            pass
        self.state = "waiting_manual"
        self._save()
        self._poller = asyncio.create_task(self._poll(page))
        return {"state": "waiting_manual"}

    async def _probe(self, page) -> dict | None:
        try:
            if not is_suno_url(page.url):
                return None
            return await page.evaluate(LOGIN_CHECK)
        except Exception:
            return None

    async def _verify(self, page) -> bool:
        await asyncio.sleep(1.0)  # 等 Clerk 会话稳定
        r = await self._probe(page)
        return bool(r and r.get("loggedIn"))

    async def _success(self, account: str) -> None:
        self.account = account or self.account
        self.state = "logged_in"
        await self.session.minimize()  # 收窗不杀进程，登录态留在隔离 profile
        self._save()

    async def _poll(self, page, interval: float = 0.5, deadline_s: float = 600) -> None:
        deadline = asyncio.get_event_loop().time() + deadline_s
        unreachable = 0
        while asyncio.get_event_loop().time() < deadline and self.state == "waiting_manual":
            res = await self._probe(page)
            if res is None and is_suno_url(getattr(page, "url", ""), allow_auth=True) and not is_suno_url(page.url):
                await asyncio.sleep(interval)  # 手动认证页允许停留，返回 Suno 后才探针
                continue
            if res is None or not res.get("ready"):  # 500 等非 200/401 不算权威答复（SPEC §4.1）
                unreachable += 1
                if unreachable >= 3:  # 连续不可达≈用户关窗 → 复检兜底
                    break
                await asyncio.sleep(interval)
                continue
            unreachable = 0
            if res.get("loggedIn"):
                self.state = "verifying"
                try:
                    ok = await self._verify(page)
                except Exception:
                    ok = False  # 复检抛错不能把状态卡在 verifying
                if ok:
                    await self._success(res.get("account", ""))
                else:
                    self.state = "not_logged_in"
                    self._save()
                return
            await asyncio.sleep(interval)
        # 超时或页面不可达：换合规页复检一次（SPEC：检测后必复检）
        if self.state in ("waiting_manual", "verifying"):
            try:
                page2 = await self.session.ensure()
                if await self._verify(page2):
                    r = await self._probe(page2)
                    await self._success((r or {}).get("account", ""))
                    return
            except Exception:
                pass
            self.state = "not_logged_in"
            self._save()

    async def recheck(self) -> dict:
        """惰性复检（GET /api/session/status / 任务遇 401 后调用）。不乐观缓存。"""
        if self.state == "logged_in":
            try:
                page = await self.session.ensure()
                r = await self._probe(page)
                if r is not None and r.get("ready") and not r.get("loggedIn"):
                    self.state = "not_logged_in"
                    self.account = ""
                    self._save()
            except Exception:
                pass  # Chrome 暂不可达不改写已确认的登录事实
        return {"state": self.state, "account": self.account}

    def mark_expired(self) -> None:
        """其他 Suno API 遇 401 → 触发会话复检（SPEC §4.1：只读探针确认 401 才降级，不直接改写）。"""
        if self.state == "logged_in" and (self._recheck is None or self._recheck.done()):
            try:
                self._recheck = asyncio.create_task(self.recheck())
            except RuntimeError:
                pass  # 无事件循环：留给下一次惰性复检（GET /api/session/status）
