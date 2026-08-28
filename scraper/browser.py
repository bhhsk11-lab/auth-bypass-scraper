"""
Stealth Playwright browser — for JavaScript-rendered and bot-protected sites.
Uses the 'hard manipulative tricks' cheat sheet extensively.
"""
import asyncio
import logging
import random
import time
from typing import Any

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from config import settings
from scraper.bypass import BLOCKED_SCRIPT_PATTERNS, build_anti_paywall_cookies

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# STEALTH INIT SCRIPT — Neutralizes ALL automation fingerprints
# ═══════════════════════════════════════════════════════════════════════

STEALTH_INIT_SCRIPT = r"""
// ──────────────────────────────────────────────────────────────────────
// 1. webdriver flag — the #1 signal
// ──────────────────────────────────────────────────────────────────────
Object.defineProperty(Object.getPrototypeOf(navigator), 'webdriver', {
    get: () => undefined,
    configurable: true,
});

// ──────────────────────────────────────────────────────────────────────
// 2. navigator.languages — set to realistic en-US
// ──────────────────────────────────────────────────────────────────────
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en', 'en-CA'],
    configurable: true,
});

// ──────────────────────────────────────────────────────────────────────
// 3. navigator.plugins — fake Chrome plugin list
// ──────────────────────────────────────────────────────────────────────
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const plugins = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
        ];
        plugins.length = 3;
        plugins.item = (i) => plugins[i];
        plugins.namedItem = (n) => plugins.find(p => p.name === n);
        plugins.refresh = () => {};
        return plugins;
    },
    configurable: true,
});

// ──────────────────────────────────────────────────────────────────────
// 4. navigator.mimeTypes — fake realistic mime types
// ──────────────────────────────────────────────────────────────────────
Object.defineProperty(navigator, 'mimeTypes', {
    get: () => {
        const mimeTypes = [
            { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
            { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
            { type: 'application/x-google-chrome-print-preview-to-pdf', suffixes: '', description: 'Print Preview PDF' },
        ];
        mimeTypes.length = 3;
        mimeTypes.item = (i) => mimeTypes[i];
        mimeTypes.namedItem = (n) => mimeTypes.find(m => m.type === n);
        return mimeTypes;
    },
    configurable: true,
});

// ──────────────────────────────────────────────────────────────────────
// 5. chrome.runtime — fake Chrome runtime object
// ──────────────────────────────────────────────────────────────────────
window.chrome = window.chrome || {};
window.chrome.runtime = {
    connect: () => ({ onMessage: { addListener: () => {} }, onDisconnect: { addListener: () => {} } }),
    sendMessage: () => {},
    onMessage: { addListener: () => {} },
    onConnect: { addListener: () => {} },
    id: 'aafhfjgjgjgkgkgkkglglmnopqrrsstt',
};
window.chrome.loadTimes = () => ({
    requestTime: 0,
    startLoadTime: performance.now(),
    commitLoadTime: 0,
    finishDocumentLoadTime: 0,
    finishLoadTime: 0,
    firstPaintTime: 0,
    firstPaintAfterLoadTime: 0,
    navigationType: 'BackForward',
    wasFetchedViaSpdy: false,
    wasFetchedViaHttp2: false,
    wasAlternateProtocolAvailable: false,
    wasFetchedViaProxy: false,
});
window.chrome.csi = () => ({ onT: 0, offT: 0, cnv: 0 });

// ──────────────────────────────────────────────────────────────────────
// 6. navigator.hardwareConcurrency / deviceMemory
// ──────────────────────────────────────────────────────────────────────
Object.defineProperty(navigator, 'hardwareConcurrency', {
    get: () => 8,
    configurable: true,
});
Object.defineProperty(navigator, 'deviceMemory', {
    get: () => 8,
    configurable: true,
});

// ──────────────────────────────────────────────────────────────────────
// 7. navigator.maxTouchPoints — 0 for desktop
// ──────────────────────────────────────────────────────────────────────
Object.defineProperty(navigator, 'maxTouchPoints', {
    get: () => 0,
    configurable: true,
});

// ──────────────────────────────────────────────────────────────────────
// 8. navigator.connection — realistic network info
// ──────────────────────────────────────────────────────────────────────
if (navigator.connection) {
    Object.defineProperty(navigator.connection, 'effectiveType', {
        get: () => '4g',
        configurable: true,
    });
    Object.defineProperty(navigator.connection, 'downlink', {
        get: () => 10 + Math.random() * 40,
        configurable: true,
    });
}

// ──────────────────────────────────────────────────────────────────────
// 9. WebGL Vendor — spoof GPU renderer
// ──────────────────────────────────────────────────────────────────────
const getParameterOriginal = WebGLRenderingContext.prototype.getParameter;
const WEBGL_UNMASKED_VENDOR_WEBGL = 37445;
const WEBGL_UNMASKED_RENDERER_WEBGL = 37446;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === WEBGL_UNMASKED_VENDOR_WEBGL) return 'Intel Inc.';
    if (parameter === WEBGL_UNMASKED_RENDERER_WEBGL) return 'Intel(R) Iris(TM) Plus Graphics 655';
    return getParameterOriginal.call(this, parameter);
};

// ──────────────────────────────────────────────────────────────────────
// 10. screen.orientation — desktop-like
// ──────────────────────────────────────────────────────────────────────
if (window.screen) {
    Object.defineProperty(screen, 'orientation', {
        get: () => ({ type: 'landscape-primary', angle: 0 }),
        configurable: true,
    });
}

// ──────────────────────────────────────────────────────────────────────
// 11. Canvas noise — small random pixel to foil canvas fingerprinting
// ──────────────────────────────────────────────────────────────────────
const toDataURLOriginal = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(...args) {
    const ctx = this.getContext('2d');
    if (ctx) {
        const noise = Math.random() * 0.01;
        ctx.fillStyle = `rgba(0,0,0,${noise})`;
        ctx.fillRect(0, 0, 1, 1);
    }
    return toDataURLOriginal.apply(this, args);
};

// ──────────────────────────────────────────────────────────────────────
// 12. Permissions — realistic permission states
// ──────────────────────────────────────────────────────────────────────
try {
    const queryOriginal = navigator.permissions.query;
    navigator.permissions.query = (desc) => {
        switch (desc.name) {
            case 'camera': case 'microphone': case 'midi-sysex':
                return Promise.resolve({ state: 'denied', onchange: null });
            case 'notifications': case 'geolocation':
                return Promise.resolve({ state: 'denied', onchange: null });
            case 'clipboard-read': case 'clipboard-write':
                return Promise.resolve({ state: 'granted', onchange: null });
            default:
                return queryOriginal.call(navigator.permissions, desc);
        }
    };
} catch(e) {}

// ──────────────────────────────────────────────────────────────────────
// 13. Remove Playwright-specific properties
// ──────────────────────────────────────────────────────────────────────
try { delete window.__playwright; } catch(e) {}

// ──────────────────────────────────────────────────────────────────────
// 14. Battery API — spoof realistic battery
// ──────────────────────────────────────────────────────────────────────
try {
    if (navigator.getBattery) {
        navigator.getBattery().then(battery => {
            Object.defineProperty(battery, 'charging', { get: () => true });
            Object.defineProperty(battery, 'level', { get: () => 0.85 });
            Object.defineProperty(battery, 'chargingTime', { get: () => 0 });
            Object.defineProperty(battery, 'dischargingTime', { get: () => Infinity });
        });
    }
} catch(e) {}

console.log('[HackerAI-Stealth] Fingerprint masked successfully');
"""


# ═══════════════════════════════════════════════════════════════════════
# STEALTH BROWSER CLASS
# ═══════════════════════════════════════════════════════════════════════

class StealthBrowser:
    """Browser with complete anti-fingerprint stealth patches."""

    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def start(self):
        logger.info("Starting stealth browser...")
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=settings.browser_headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-gpu",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--window-size=1920,1080",
                "--start-maximized",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            ],
        )
        logger.info(f"Browser started: {self._browser}")

    async def create_context(self, cookies: list[dict] | None = None) -> BrowserContext:
        """Create a fresh context with realistic env + optional cookies."""
        context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="America/New_York",
            color_scheme="light",
            device_scale_factor=1,
            is_mobile=False,
            has_touch=False,
            permissions=[],
            reduced_motion="no-preference",
            forced_colors="none",
            proxy={"server": settings.proxy_url} if settings.proxy_url else None,
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )

        if not settings.proxy_url:
            # Route through Googlebot-ish referer network
            await context.set_extra_http_headers({
                "Referer": "https://www.google.com/",
            })

        # Inject the stealth init script
        await context.add_init_script(STEALTH_INIT_SCRIPT)

        # Add anti-paywall cookies
        if cookies:
            await context.add_cookies(cookies)

        return context

    async def fetch(self, url: str, cookies: list[dict] | None = None,
                    wait_selector: str | None = None,
                    generate_pdf: bool = True) -> dict:
        """
        Full navigation with human-like behavior + stealth patches.

        Returns:
            html: page HTML
            pdf_bytes: print-to-PDF bytes
            cookies: session cookies
            title: page title
        """
        context = await self.create_context(cookies)
        page = await context.new_page()

        # ── Block paywall/analytics scripts at network level ──
        blocked_count = [0]

        async def block_scripts(route):
            url_str = route.request.url
            for pattern in BLOCKED_SCRIPT_PATTERNS:
                if pattern in url_str:
                    blocked_count[0] += 1
                    await route.abort()
                    return
            # Also block all .js files from known ad/paywall CDNs
            if any(domain in url_str for domain in [
                "piano.io", "tinypass.com", "permutive.com",
                "doubleclick.net", "googlesyndication.com",
                "criteo.net", "criteo.com",
                "outbrain.com", "taboola.com",
                "chartbeat.com", "parsely.com",
            ]):
                blocked_count[0] += 1
                await route.abort()
                return
            await route.continue_()

        await page.route("**/*", block_scripts)

        # ── Navigate with human-like warmup ──
        logger.info(f"Navigating to: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=settings.browser_timeout)

            # Wait for initial render
            await page.wait_for_timeout(random.randint(1500, 3000))

            # ── Human-like scrolling ──
            viewport_height = 1080
            for scroll_step in range(random.randint(5, 10)):
                scroll_amount = random.randint(200, 600)
                await page.mouse.wheel(0, scroll_amount)
                await page.wait_for_timeout(random.randint(150, 500))
                # Occasional small circle mouse movement
                if random.random() < 0.3:
                    x = random.randint(100, 1800)
                    y = random.randint(100, 900)
                    await page.mouse.move(x, y, steps=random.randint(5, 15))

            # ── Wait for custom selector if provided ──
            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=10000)
                except Exception:
                    logger.debug(f"Selector not found: {wait_selector}")

            # ── Get HTML ──
            html = await page.content()
            title = await page.title()

            # ── Generate PDF (catches content that overlays hide) ──
            pdf_bytes = None
            if generate_pdf:
                try:
                    pdf_bytes = await page.pdf(
                        format="A4",
                        landscape=False,
                        print_background=False,
                        margin={
                            "top": "10mm",
                            "bottom": "10mm",
                            "left": "10mm",
                            "right": "10mm",
                        },
                    )
                    logger.info(f"PDF generated: {len(pdf_bytes)} bytes")
                except Exception as e:
                    logger.warning(f"PDF generation failed: {e}")

            # ── Get cookies ──
            session_cookies = await context.cookies()

            logger.info(f"Page loaded. Blocked {blocked_count[0]} scripts. "
                        f"HTML: {len(html)}b, PDF: {len(pdf_bytes) if pdf_bytes else 0}b")

            await context.close()
            return {
                "html": html,
                "pdf_bytes": pdf_bytes,
                "cookies": session_cookies,
                "title": title,
                "blocked_scripts": blocked_count[0],
            }

        except Exception as e:
            logger.error(f"Navigation failed: {e}")
            await context.close()
            raise


# ═══════════════════════════════════════════════════════════════════════
# SINGLETON MANAGER
# ═══════════════════════════════════════════════════════════════════════

_browser_instance: StealthBrowser | None = None
_browser_lock = asyncio.Lock()


async def get_browser() -> StealthBrowser:
    """Get or create the singleton browser instance."""
    global _browser_instance
    if _browser_instance is None:
        async with _browser_lock:
            if _browser_instance is None:
                _browser_instance = StealthBrowser()
                await _browser_instance.start()
    return _browser_instance
