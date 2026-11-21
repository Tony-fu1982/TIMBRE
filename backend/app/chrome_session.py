"""真 Chrome 会话（SPEC §4.1）：固定 9222 回环 + 隔离 profile；复用须验证进程归属；退出只断连不杀 Chrome。"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psutil

from . import config
from .errors import AppError

_REG_KEYS = [
    (r"HKCU\Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", ""),
    (r"HKLM\Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", ""),
    (r"HKLM\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", ""),
    (r"HKCU\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", ""),
    (r"HKCU\Software\Clients\StartMenuInternet\Google Chrome\shell\open\command", ""),
    (r"HKLM\Software\Clients\StartMenuInternet\Google Chrome\shell\open\command", ""),
]


def _exe_from_reg(value: str) -> str:
    m = re.search(r'"([^"]+\.exe)"', value) or re.search(r"([a-zA-Z]:\\.+?\.exe)(?:\s|$)", value)
    return m.group(1) if m else ""


def find_chrome() -> str:
    """注册表 6 键 → 3 固定路径 → PATH（doc 08 §1.1）。"""
    try:
        import winreg

        roots = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
        for full, _ in _REG_KEYS:
            root, sub = full.split("\\", 1)
            try:
                with winreg.OpenKey(roots[root], sub) as k:
                    v = winreg.QueryValueEx(k, "")[0]
                exe = _exe_from_reg(str(v)) if "command" in sub else str(v).strip('"')
                if exe and Path(exe).is_file():
                    return exe
            except OSError:
                continue
    except ImportError:
        pass
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            p = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if p.is_file():
                return str(p)
    return shutil.which("chrome") or ""


def is_suno_url(url: str | None, allow_auth: bool = False) -> bool:
    """SPEC §4.1：HTTPS、默认端口、无 userinfo、主机精确 suno.com|www.suno.com。
    allow_auth 仅手动登录期间放行 auth.suno.com。"""
    if not url:
        return False
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    if u.scheme != "https" or u.username is not None or u.password is not None:
        return False
    if u.port not in (None, 443):
        return False
    host = (u.hostname or "").lower()
    if host in config.SUNO_HOSTS:
        return True
    return allow_auth and host == "auth.suno.com"


def _listening_pid(port: int) -> int | None:
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == port and c.status == "LISTEN" and c.pid:
                return c.pid
    except (psutil.AccessDenied, OSError):
        return None
    return None


def _owner_is_ours(pid: int, port: int, profile: Path) -> bool:
    """SPEC §4.1：监听进程必须是 Chrome + 匹配调试端口 + 归一化 profile 路径。"""
    try:
        p = psutil.Process(pid)
        if "chrome" not in p.name().lower():
            return False
        cmd = p.cmdline()
        if f"--remote-debugging-port={port}" not in cmd:
            return False
        for a in cmd:
            if a.startswith("--user-data-dir="):
                got = Path(a.split("=", 1)[1].strip('"')).resolve()
                return str(got).lower() == str(profile.resolve()).lower()
        return False
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


class ChromeSession:
    def __init__(self, profile_dir: Path = config.PROFILE_DIR, port: int = config.DEBUG_PORT):
        self.profile = Path(profile_dir)
        self.port = port
        self._pw = None
        self._browser = None
        self._page = None

    # ---- 端口/启动 ----
    async def port_alive(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=1.5) as c:
                r = await c.get(f"http://127.0.0.1:{self.port}/json/version")
                return r.status_code == 200
        except httpx.HTTPError:
            return False

    def _check_owner(self) -> None:
        pid = _listening_pid(self.port)
        if pid is None or not _owner_is_ours(pid, self.port, self.profile):
            raise AppError("PORT_IN_USE")

    async def _launch(self, visible: bool) -> None:
        exe = find_chrome()
        if not exe:
            raise AppError("CHROME_NOT_FOUND")
        self.profile.mkdir(parents=True, exist_ok=True)
        args = [
            exe,
            f"--remote-debugging-port={self.port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self.profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
        ]
        if visible:
            args += ["--window-position=260,140", "--window-size=1400,900", "--new-window", "https://suno.com"]
        else:
            args += ["about:blank"]
        subprocess.Popen(
            args,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = asyncio.get_event_loop().time() + 15.0
        while asyncio.get_event_loop().time() < deadline:
            if await self.port_alive():
                self._check_owner()  # 就绪后再验归属（启动竞态 fail-closed）
                return
            await asyncio.sleep(0.25)
        raise AppError("PORT_TIMEOUT")

    # ---- 页面 ----
    async def _pick_page(self):
        ctx = self._browser.contexts[0]
        for p in ctx.pages:
            if is_suno_url(p.url):
                return p
        for p in ctx.pages:
            if p.url in ("about:blank", "chrome://newtab/", ""):
                await p.goto("https://suno.com", wait_until="domcontentloaded", timeout=30000)
                return p
        p = await ctx.new_page()
        await p.goto("https://suno.com", wait_until="domcontentloaded", timeout=30000)
        return p

    async def ensure(self, visible: bool = False):
        """幂等：端口不在→启动；未连→CDP 连接；无可用页→选页。绝不关闭用户 Chrome。"""
        if not await self.port_alive():
            await self._launch(visible)
        else:
            self._check_owner()
        if self._browser is None or not self._browser.is_connected():
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}")
        if self._page is None or self._page.is_closed():
            self._page = await self._pick_page()
        if visible:
            await self._set_window_state("normal")
            try:
                await self._page.bring_to_front()
            except Exception:
                pass
        return self._page

    async def check_page(self, page, allow_auth: bool = False) -> None:
        """每次业务脚本前复查主框架 URL；越界即中止（SPEC §4.1）。"""
        if page.is_closed() or not is_suno_url(page.url, allow_auth=allow_auth):
            raise AppError("PAGE_OFF_SITE")

    async def _set_window_state(self, state: str) -> None:
        try:
            cdp = await self._page.context.new_cdp_session(self._page)
            info = await cdp.send("Browser.getWindowForTarget")
            await cdp.send("Browser.setWindowBounds", {"windowId": info["windowId"], "bounds": {"windowState": state}})
            await cdp.detach()
        except Exception:
            pass  # 窗口态失败不影响会话事实

    async def minimize(self) -> None:
        await self._set_window_state("minimized")

    async def disconnect(self) -> None:
        """应用退出：只断 CDP 连接。playwright 对 connect_over_cdp 的 close() 仅断开，
        不调用 CDP Browser.close，Chrome 进程与登录态保留（SPEC §1/§4.1）。"""
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        finally:
            self._browser = None
            self._page = None
            if self._pw:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
                self._pw = None
