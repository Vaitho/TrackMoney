"""Browser automatico (Playwright) per le pagine che caricano il prezzo con JavaScript."""
import asyncio
import logging

import urlguard

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_playwright = None
_browser = None
_lock = asyncio.Lock()
_slots = asyncio.Semaphore(2)  # massimo 2 pagine aperte insieme


def is_installed() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


async def _ensure_browser():
    global _playwright, _browser
    async with _lock:
        if _browser is None or not _browser.is_connected():
            from playwright.async_api import async_playwright

            if _playwright is None:
                _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(headless=True)
    return _browser


async def _guard_route(route) -> None:
    """Blocca ogni richiesta (anche quelle fatte dalla pagina stessa) verso indirizzi non pubblici."""
    url = route.request.url
    try:
        if not url.startswith(("data:", "blob:", "about:")):
            await urlguard.ensure_public(url)
        await route.continue_()
    except Exception:  # BlockedUrl o qualsiasi altro problema: nel dubbio non si scarica
        await route.abort()


async def get_html(url: str, timeout_ms: int = 30000, check_urls: bool = True) -> str:
    browser = await _ensure_browser()
    async with _slots:
        context = await browser.new_context(user_agent=USER_AGENT, locale="it-IT")
        try:
            if check_urls:
                await context.route("**/*", _guard_route)
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # alcuni siti non smettono mai di caricare: va bene lo stesso
                pass
            return await page.content()
        finally:
            await context.close()


async def shutdown() -> None:
    global _playwright, _browser
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None
