from __future__ import annotations

import html as htmllib
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from .fetcher import FetchBlocked, Fetcher, Page, _default_resolver, host_allowed, ip_is_public

_LOCK = threading.Lock()
SKIP_TYPES = ("image", "media", "font")


class BrowserUnavailable(FetchBlocked):
    pass


@dataclass
class Browser:
    cfg: object
    min_delay: float = 3.0
    resolver: object = None
    transport: object = None
    sleep: object = time.sleep
    clock: object = time.monotonic
    pages: int = 0
    _pw: object = None
    _browser: object = None
    _context: object = None
    _domain: str = ""
    _held: bool = False
    _public: dict = field(default_factory=dict)
    _last: dict = field(default_factory=dict)

    def __enter__(self):
        if not self.cfg.enabled:
            raise BrowserUnavailable("مرورگر نامرئی در تنظیمات خاموش است ([browser] enabled).")
        if not _LOCK.acquire(timeout=900):
            raise BrowserUnavailable("مرورگر نامرئی همین حالا در اختیار کار دیگری است؛ کمی بعد دوباره امتحان کنید.")
        self._held = True
        try:
            self._start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _launch_options(self) -> list[dict]:
        if self.cfg.executable_path:
            return [{"executable_path": self.cfg.executable_path}]
        order = [self.cfg.channel] + [c for c in ("msedge", "chrome", "chromium") if c != self.cfg.channel]
        return [{} if c == "chromium" else {"channel": c} for c in order]

    def _start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise BrowserUnavailable("بسته Playwright نصب نیست؛ نصب‌کننده سامانه را دوباره اجرا کنید.") from None
        self._pw = sync_playwright().start()
        errors = []
        for opts in self._launch_options():
            try:
                self._browser = self._pw.chromium.launch(headless=True, **opts)
                break
            except Exception as exc:
                errors.append(str(exc).strip().splitlines()[0][:160] if str(exc).strip() else exc.__class__.__name__)
        if self._browser is None:
            raise BrowserUnavailable("مرورگر Edge یا Chrome روی این رایانه پیدا نشد یا باز نشد. Edge را نصب یا به‌روز کنید، "
                                     "یا در [browser] نشانی executable_path را بدهید. " + "؛ ".join(errors[:2]))
        opts = {"accept_downloads": False, "locale": "fa-IR", "service_workers": "block",
                "viewport": {"width": 1366, "height": 900}}
        if self.cfg.user_agent:
            opts["user_agent"] = self.cfg.user_agent
        self._context = self._browser.new_context(**opts)
        self._context.route("**/*", self._route)

    def close(self) -> None:
        for name in ("_context", "_browser"):
            obj = getattr(self, name)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
                setattr(self, name, None)
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        if self._held:
            self._held = False
            _LOCK.release()

    def _host_public(self, host: str, port: int) -> bool:
        key = (host, port)
        if key not in self._public:
            try:
                ips = (self.resolver or _default_resolver)(host, port)
            except OSError:
                ips = []
            self._public[key] = bool(ips) and all(ip_is_public(ip) for ip in ips)
        return self._public[key]

    def _route(self, route) -> None:
        req = route.request
        parts = urllib.parse.urlsplit(req.url)
        if parts.scheme in ("data", "blob"):
            route.continue_()
            return
        if parts.scheme not in ("http", "https") or parts.username or parts.password or req.resource_type in SKIP_TYPES:
            route.abort()
            return
        host = (parts.hostname or "").lower()
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            route.abort()
            return
        if not host or port not in (80, 443):
            route.abort()
            return
        try:
            main = req.is_navigation_request() and req.frame == req.frame.page.main_frame
        except Exception:
            main = False
        if main and not host_allowed(host, self._domain):
            route.abort()
            return
        if (self.transport is None or self.resolver is not None) and not self._host_public(host, port):
            route.abort()
            return
        if self.transport is not None:
            res = self.transport(req.url)
            if res is None:
                route.abort()
                return
            status, ctype, body = res
            route.fulfill(status=status, content_type=ctype, body=body.encode("utf-8") if isinstance(body, str) else body)
            return
        route.continue_()

    def _wait(self, host: str, delay: float | None = None) -> None:
        last = self._last.get(host)
        if last is not None:
            remaining = max(self.min_delay, float(delay or 0)) - (self.clock() - last)
            if remaining > 0:
                self.sleep(remaining)
        self._last[host] = self.clock()

    def render(self, url: str, domain: str, ready: str | None = None, collect: str | None = None,
               scrolls: int | None = None, delay: float | None = None) -> Page:
        if self._context is None:
            raise BrowserUnavailable("مرورگر نامرئی باز نیست.")
        guard = Fetcher("BornaMarketBot", resolver=self.resolver)
        if self.transport is None or self.resolver is not None:
            url = guard.check_url(url, domain)
        else:
            host = urllib.parse.urlsplit(url).hostname or ""
            if urllib.parse.urlsplit(url).scheme not in ("http", "https") or not host_allowed(host, domain):
                raise FetchBlocked(f"نشانی «{host or url}» بیرون از دامنه ثبت‌شده منبع ({domain}) است.")
        self._domain = domain
        self._wait(urllib.parse.urlsplit(url).hostname or "", delay)
        timeout = self.cfg.timeout_seconds * 1000
        page = self._context.new_page()
        frags: list[str] = []
        seen: set[str] = set()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            status = resp.status if resp is not None else 0
            if status >= 400:
                raise FetchBlocked(f"سایت پاسخ HTTP {status} داد.", status=status)
            if ready:
                try:
                    page.wait_for_selector(ready, timeout=min(timeout, 20_000))
                except Exception:
                    pass

            def grab():
                if collect:
                    for h in page.eval_on_selector_all(collect, "els => els.map(e => e.outerHTML)"):
                        if h not in seen:
                            seen.add(h)
                            frags.append(h)
            grab()
            for _ in range(self.cfg.max_scrolls if scrolls is None else scrolls):
                page.mouse.wheel(0, 2400)
                page.wait_for_timeout(self.cfg.scroll_pause_ms)
                grab()
            html = page.content()
            final = page.url
        except FetchBlocked:
            raise
        except Exception as exc:
            first = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else exc.__class__.__name__
            raise FetchBlocked(f"مرورگر نامرئی صفحه را باز نکرد: {first}") from None
        finally:
            try:
                page.close()
            except Exception:
                pass
        if collect and frags:
            title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
            head = htmllib.escape(htmllib.unescape(title.group(1).strip())) if title else ""
            html = ('<!doctype html><html lang="fa"><head><meta charset="utf-8"><title>' + head + "</title></head><body>"
                    + "\n".join(frags) + "</body></html>")
        body = html.encode("utf-8")
        if len(body) > self.cfg.max_mb * 1024 * 1024:
            raise FetchBlocked("حجم صفحه بیش از سقف مجاز است.")
        self.pages += 1
        return Page(final, status, "text/html", body, "utf-8")
