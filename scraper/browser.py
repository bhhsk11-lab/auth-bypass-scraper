"""Stealth Playwright browser: 14-point fingerprint masking + cookie export."""
import base64
import logging
import shutil
import tempfile

from playwright.async_api import async_playwright

from config import settings

logger = logging.getLogger("browser")

STEALTH_INIT_SCRIPT = """
// ── webdriver flag ─────────────────────────────────────────────────
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// ── plugins / languages ────────────────────────────────────────────
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});

// ── chrome runtime object ──────────────────────────────────────────
window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};

// ── permissions query (notifications) ──────────────────────────────
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : originalQuery(parameters));

// ── WebGL vendor/renderer masking ──────────────────────────────────
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.apply(this, [parameter]);
};

// ── hardware concurrency / deviceMemory ────────────────────────────
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

// ── iframe contentWindow chrome ────────────────────────────────────
try {
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
        get: function() { return window; }
    });
} catch (e) {}

// ── headless UA fix ────────────────────────────────────────────────
Object.defineProperty(navigator, 'userAgent', {
    get: () => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
             + 'AppleWebKit/537.36 (KHTML, like Gecko) '
             + 'Chrome/124.0.0.0 Safari/537.36'});

// ── paywall overlay killer: hide common wall containers ────────────
const css = `
    [class*="paywall"], [id*="paywall"], [class*="Paywall"],
    [class*="overlay-mask"], [class*="regwall"], [class*="signup-wall"]
    { display: none !important; }
    html, body { overflow: auto !important; height: auto !important; }
`;
const style = document.createElement('style');
style.textContent = css;
document.documentElement.appendChild(style);
"""


class StealthBrowser:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw = None
        self._context = None
        self._profile_dir = None

    async def start(self):
        self._pw = await async_playwright().start()
        # launch_persistent_context's first argument is a REQUIRED
        # user_data_dir (a real profile path, not optional) — a fresh temp
        # dir per process gives us the persistent-context APIs (needed for
        # cookie/session export) without reusing state across runs.
        self._profile_dir = tempfile.mkdtemp(prefix="nb-stealth-")
        self._context = await self._pw.chromium.launch_persistent_context(
            self._profile_dir,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="Asia/Kolkata",
        )
        await self._context.add_init_script(STEALTH_INIT_SCRIPT)
        # block known tracker/paywall script domains
        await self._context.route(
            "**/*", self._route_filter)

    @staticmethod
    async def _route_filter(route):
        url = route.request.url
        blocked = ("doubleclick.net", "googlesyndication",
                   "google-analytics", "facebook.net/tr",
                   "chartbeat", "quantserve", "scorecardresearch")
        if any(b in url for b in blocked):
            await route.abort()
        else:
            await route.continue_()

    async def stop(self):
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        if self._profile_dir:
            shutil.rmtree(self._profile_dir, ignore_errors=True)

    async def fetch(self, url: str, generate_pdf: bool = False) -> dict:
        """Fetch page in stealth context. Returns html, text, cookies,
        pdf_base64, last_error."""
        out = {"html": None, "text": None, "cookies": [],
               "pdf_base64": None, "last_error": None}
        page = None
        try:
            page = await self._context.new_page()
            resp = await page.goto(
                url, timeout=settings.browser_timeout_ms,
                wait_until="domcontentloaded")
            # let late JS (paywalls, lazy content, challenges) settle
            await page.wait_for_timeout(2500)
            out["html"] = await page.content()
            out["text"] = await page.inner_text("body")
            out["cookies"] = await self._context.cookies(url)
            if generate_pdf:
                pdf_bytes = await page.pdf(
                    format="A4", print_background=True,
                    margin={"top": "10mm", "bottom": "10mm",
                            "left": "8mm", "right": "8mm"})
                out["pdf_base64"] = base64.b64encode(pdf_bytes).decode()
            if resp and resp.status >= 400:
                out["last_error"] = f"HTTP {resp.status}"
        except Exception as e:
            out["last_error"] = str(e)
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
        return out
