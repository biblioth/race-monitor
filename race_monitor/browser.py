"""Anonymous official HK100 pages in an isolated browser; no challenge solver."""
import os
import re
import time
from urllib.parse import urlsplit


class BrowserReadError(RuntimeError):
    """Safe, page-specific diagnostics without browser state or arbitrary HTML."""


def _failure_detail(page, label, phase, response):
    try:
        challenge = bool(re.search(r"just a moment|checking your browser|attention required|verify.*human", page.title(), re.I))
    except Exception:
        challenge = False
    status = getattr(response, "status", None)
    reason = f"HTTP {status}" if isinstance(status, int) and status >= 400 else phase + "超时或网络失败"
    if challenge:
        reason += "，访问校验页未恢复"
    return f"{label}：{reason}"


def hk_browser_pages():
    from playwright.sync_api import sync_playwright, Error as PlaywrightError

    pages = []
    with sync_playwright() as p:
        # GitHub Linux runs this under Xvfb. No user's desktop or profile is used.
        browser = p.chromium.launch(headless=not bool(os.environ.get("DISPLAY")))
        try:
            context = browser.new_context()
            try:
                for label, url, expected in [("首页", "https://hk100ultra.com/", r"20\d{2}\s+(?:Edition|Events)"),
                                             ("报名页", "https://hk100ultra.com/entry/", r"ENTRY PROCEDURES")]:
                    for attempt in range(2):
                        page = context.new_page()
                        response, phase = None, "建立连接"
                        try:
                            # Wait for the real race content, rather than page-wide
                            # DOM events that can be delayed by unrelated resources.
                            response = page.goto(url, wait_until="commit", timeout=30000)
                            phase = "正文加载"
                            page.locator("body").filter(has_text=re.compile(expected, re.I)).wait_for(timeout=30000)
                            if urlsplit(page.url).hostname != "hk100ultra.com":
                                raise BrowserReadError(f"{label}：跳转到未核验域名")
                            html = page.content()
                            if len(html.encode()) > 2_000_000:
                                raise BrowserReadError(f"{label}：浏览器页面过大")
                            pages.append(html)
                            break
                        except PlaywrightError:
                            detail = _failure_detail(page, label, phase, response)
                            if attempt == 1:
                                raise BrowserReadError(f"{detail}（已尝试 2 次）") from None
                        finally:
                            page.close()
                        time.sleep(2)
            finally:
                context.close()
        finally:
            browser.close()
    return pages
