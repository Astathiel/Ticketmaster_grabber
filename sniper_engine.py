import ctypes
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, fields
from typing import Callable, Dict, List, Optional, Tuple

from playwright.sync_api import Browser, Locator, Page, sync_playwright

try:
    import winsound
except ImportError:  # pragma: no cover - non-Windows fallback
    winsound = None

UNDER_18_EXCLUDE_KEYWORDS: Tuple[str, ...] = (
    "k18",
    "k-18",
    "k 18",
    "18+",
    "anniskelu",
    "ikäraja 18",
)
VIP_EXCLUDE_KEYWORDS: Tuple[str, ...] = ("vip", "platinum", "paketti")
STANDING_EXCLUDE_KEYWORDS: Tuple[str, ...] = ("kenttä", "permanto", "seisomapaikka")
ACCESSIBLE_EXCLUDE_KEYWORDS: Tuple[str, ...] = ("pyörätuoli", "saattaja", "esteetön")

DEFAULT_PREFERRED_KEYWORDS = "peruslippu, istumapaikat, katsomo, paikka, normaali"


def _parse_keyword_csv(raw: str) -> Tuple[str, ...]:
    """Split comma/semicolon/newline separated keywords into unique lowercase tokens."""
    seen = set()
    result: List[str] = []
    normalized = raw.replace(";", ",").replace("\n", ",")
    for part in normalized.split(","):
        token = part.strip().lower()
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return tuple(result)


@dataclass
class SniperConfig:
    """Configuration driving Chrome attachment, search, seat filtering, and polling."""

    cdp_endpoint: str = "http://127.0.0.1:9222"
    profile_dir: str = r"C:\chrome_tm_profile"
    target_url_keyword: str = "ticketmaster.fi"
    search_query: str = ""
    ticket_quantity: int = 2
    adjacent_only: bool = True
    under_18_only: bool = True
    exclude_vip: bool = True
    exclude_standing: bool = True
    exclude_accessible: bool = True
    custom_exclude_keywords: str = ""
    custom_preferred_keywords: str = DEFAULT_PREFERRED_KEYWORDS
    fast_mode: bool = False
    browser_mode: str = "docked"  # "docked" | "webview" | "external"
    min_poll_sleep_seconds: float = 0.05
    max_poll_sleep_seconds: float = 0.15

    def get_exclude_keywords(self) -> Tuple[str, ...]:
        """Return deduplicated lowercase exclusion keywords from toggles + custom input."""
        tokens: List[str] = []
        if self.exclude_vip:
            tokens.extend(VIP_EXCLUDE_KEYWORDS)
        if self.under_18_only:
            tokens.extend(UNDER_18_EXCLUDE_KEYWORDS)
        if self.exclude_standing:
            tokens.extend(STANDING_EXCLUDE_KEYWORDS)
        if self.exclude_accessible:
            tokens.extend(ACCESSIBLE_EXCLUDE_KEYWORDS)
        tokens.extend(_parse_keyword_csv(self.custom_exclude_keywords))

        seen = set()
        deduped: List[str] = []
        for token in tokens:
            if token not in seen:
                seen.add(token)
                deduped.append(token)
        return tuple(deduped)

    def get_preferred_keywords(self) -> Tuple[str, ...]:
        """Return deduplicated lowercase preferred keywords."""
        return _parse_keyword_csv(self.custom_preferred_keywords)

    def to_dict(self) -> Dict[str, object]:
        """Serialize configuration to a JSON-friendly dictionary."""
        return {
            "cdp_endpoint": self.cdp_endpoint,
            "profile_dir": self.profile_dir,
            "target_url_keyword": self.target_url_keyword,
            "search_query": self.search_query,
            "ticket_quantity": self.ticket_quantity,
            "adjacent_only": self.adjacent_only,
            "under_18_only": self.under_18_only,
            "exclude_vip": self.exclude_vip,
            "exclude_standing": self.exclude_standing,
            "exclude_accessible": self.exclude_accessible,
            "custom_exclude_keywords": self.custom_exclude_keywords,
            "custom_preferred_keywords": self.custom_preferred_keywords,
            "fast_mode": self.fast_mode,
            "browser_mode": self.browser_mode,
            "min_poll_sleep_seconds": self.min_poll_sleep_seconds,
            "max_poll_sleep_seconds": self.max_poll_sleep_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "SniperConfig":
        """Construct a SniperConfig from a dictionary, ignoring unknown fields."""
        if not isinstance(data, dict):
            return cls()

        valid_names = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_names}
        cfg = cls(**filtered)
        cfg.ticket_quantity = max(1, min(10, int(cfg.ticket_quantity)))
        cfg.adjacent_only = bool(cfg.adjacent_only)
        cfg.under_18_only = bool(cfg.under_18_only)
        cfg.exclude_vip = bool(cfg.exclude_vip)
        cfg.exclude_standing = bool(cfg.exclude_standing)
        cfg.exclude_accessible = bool(cfg.exclude_accessible)
        cfg.fast_mode = bool(cfg.fast_mode)
        cfg.min_poll_sleep_seconds = max(0.001, float(cfg.min_poll_sleep_seconds))
        cfg.max_poll_sleep_seconds = max(
            cfg.min_poll_sleep_seconds, float(cfg.max_poll_sleep_seconds)
        )
        if cfg.browser_mode not in ("docked", "webview", "external"):
            cfg.browser_mode = "docked"
        return cfg

    def save_to_file(self, path: str) -> None:
        """Persist settings JSON to disk."""
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)

    @classmethod
    def load_from_file(cls, path: str) -> "SniperConfig":
        """Load settings JSON from disk, falling back to defaults if missing/invalid."""
        if not os.path.exists(path):
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return cls.from_dict(json.load(fh))
        except Exception:
            return cls()


class ChromeProfileManager:
    """Discovers Chrome, probes CDP readiness, and launches dedicated Chrome profiles."""

    @staticmethod
    def find_chrome_executable() -> Optional[str]:
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return shutil.which("chrome") or shutil.which("chrome.exe")

    @staticmethod
    def parse_cdp_port(cdp_endpoint: str) -> int:
        parsed = urllib.parse.urlparse(cdp_endpoint)
        return parsed.port or 9222

    @staticmethod
    def is_cdp_active(cdp_endpoint: str) -> Tuple[bool, str]:
        url = f"{cdp_endpoint.rstrip('/')}/json/version"
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status != 200:
                    return False, f"HTTP {resp.status}"
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                browser_name = payload.get("Browser", "Chrome/CDP")
                return True, str(browser_name)
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def get_open_tabs(cdp_endpoint: str) -> List[Dict[str, str]]:
        url = f"{cdp_endpoint.rstrip('/')}/json/list"
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                if isinstance(data, list):
                    return [item for item in data if item.get("type") == "page"]
        except Exception:
            pass
        return []

    @classmethod
    def launch_chrome_profile(
        cls,
        profile_dir: str,
        port: int = 9222,
        start_url: str = "https://www.ticketmaster.fi",
        app_mode: bool = True,
    ) -> subprocess.Popen:
        chrome_exe = cls.find_chrome_executable()
        if not chrome_exe:
            raise FileNotFoundError(
                "Could not locate Google Chrome (chrome.exe) in standard Windows directories."
            )

        os.makedirs(profile_dir, exist_ok=True)
        cmd = [
            chrome_exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if app_mode:
            cmd.append(f"--app={start_url}")
        else:
            cmd.append(start_url)

        logging.info("Launching Chrome profile '%s' on CDP port %d...", profile_dir, port)
        return subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class Win32ChromeDocker:
    """Embeds and resizes a genuine Chrome window inside a Qt/Tk container HWND on Windows."""

    GWL_STYLE = -16
    WS_CHILD = 0x40000000
    WS_VISIBLE = 0x10000000
    WS_POPUP = 0x80000000
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000
    WS_MINIMIZEBOX = 0x00020000
    WS_MAXIMIZEBOX = 0x00010000
    WS_SYSMENU = 0x00080000
    WS_OVERLAPPEDWINDOW = 0x00CF0000

    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOZORDER = 0x0004
    SWP_FRAMECHANGED = 0x0020
    SWP_SHOWWINDOW = 0x0040

    @staticmethod
    def _is_windows() -> bool:
        return sys.platform == "win32" and hasattr(ctypes, "windll")

    @classmethod
    def find_chrome_window(
        cls,
        title_keywords: Tuple[str, ...] = ("ticketmaster", "chrome"),
        pid: Optional[int] = None,
    ) -> Optional[int]:
        if not cls._is_windows():
            return None

        user32 = ctypes.windll.user32
        from ctypes import wintypes

        matches: List[Tuple[int, int, str]] = []
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            class_buf = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, class_buf, 128)
            if class_buf.value != "Chrome_WidgetWin_1":
                return True

            title_len = user32.GetWindowTextLengthW(hwnd)
            title_buf = ctypes.create_unicode_buffer(title_len + 1)
            user32.GetWindowTextW(hwnd, title_buf, title_len + 1)
            title = title_buf.value.strip()
            if not title:
                return True

            win_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
            matches.append((int(hwnd), int(win_pid.value), title))
            return True

        user32.EnumWindows(enum_proc(callback), 0)

        if pid is not None:
            for hwnd, win_pid, title in matches:
                if win_pid == pid and any(k in title.lower() for k in title_keywords):
                    return hwnd
            for hwnd, win_pid, _title in matches:
                if win_pid == pid:
                    return hwnd

        for kw in title_keywords:
            for hwnd, _win_pid, title in matches:
                if kw.lower() in title.lower():
                    return hwnd

        return None

    @classmethod
    def dock_window(
        cls, chrome_hwnd: int, container_hwnd: int, width: int, height: int
    ) -> Optional[int]:
        """Reparent chrome_hwnd as a borderless child of container_hwnd and return old style."""
        if not cls._is_windows() or not chrome_hwnd or not container_hwnd:
            return None

        user32 = ctypes.windll.user32
        old_style = user32.GetWindowLongW(chrome_hwnd, cls.GWL_STYLE)
        new_style = (
            old_style
            & ~(
                cls.WS_POPUP
                | cls.WS_CAPTION
                | cls.WS_THICKFRAME
                | cls.WS_MINIMIZEBOX
                | cls.WS_MAXIMIZEBOX
                | cls.WS_SYSMENU
            )
        ) | cls.WS_CHILD | cls.WS_VISIBLE
        user32.SetWindowLongW(chrome_hwnd, cls.GWL_STYLE, new_style)
        user32.SetParent(chrome_hwnd, container_hwnd)
        user32.SetWindowPos(
            chrome_hwnd,
            0,
            0,
            0,
            max(1, int(width)),
            max(1, int(height)),
            cls.SWP_NOZORDER | cls.SWP_FRAMECHANGED | cls.SWP_SHOWWINDOW,
        )
        user32.MoveWindow(chrome_hwnd, 0, 0, max(1, int(width)), max(1, int(height)), True)
        return int(old_style)

    @classmethod
    def resize_docked(cls, chrome_hwnd: int, width: int, height: int) -> bool:
        if not cls._is_windows() or not chrome_hwnd:
            return False
        user32 = ctypes.windll.user32
        if not user32.IsWindow(chrome_hwnd):
            return False
        return bool(
            user32.MoveWindow(
                chrome_hwnd, 0, 0, max(1, int(width)), max(1, int(height)), True
            )
        )

    @classmethod
    def undock_window(
        cls, chrome_hwnd: int, original_style: Optional[int] = None
    ) -> bool:
        if not cls._is_windows() or not chrome_hwnd:
            return False
        user32 = ctypes.windll.user32
        if not user32.IsWindow(chrome_hwnd):
            return False
        user32.SetParent(chrome_hwnd, 0)
        style = (
            original_style
            if original_style is not None
            else (cls.WS_OVERLAPPEDWINDOW | cls.WS_VISIBLE)
        )
        user32.SetWindowLongW(chrome_hwnd, cls.GWL_STYLE, style | cls.WS_VISIBLE)
        user32.SetWindowPos(
            chrome_hwnd,
            0,
            80,
            80,
            1100,
            800,
            cls.SWP_NOZORDER | cls.SWP_FRAMECHANGED | cls.SWP_SHOWWINDOW,
        )
        return True


class BrowserConnector:
    """Attaches Playwright to an active Chromium CDP endpoint and selects the Ticketmaster tab."""

    def __init__(self, config: SniperConfig):
        self.config = config
        self.playwright = None
        self.browser: Optional[Browser] = None

    def connect_and_get_page(self, open_if_missing: bool = True) -> Page:
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.connect_over_cdp(self.config.cdp_endpoint)

        if not self.browser.contexts:
            raise RuntimeError("No active browser context found on CDP.")

        default_context = self.browser.contexts[0]
        for page in default_context.pages:
            if self.config.target_url_keyword.lower() in page.url.lower():
                logging.info("Attached to active tab: %s", page.url)
                page.bring_to_front()
                return page

        if open_if_missing:
            page = (
                default_context.pages[0]
                if default_context.pages
                else default_context.new_page()
            )
            target_url = f"https://www.{self.config.target_url_keyword.strip('/')}"
            logging.info("No Ticketmaster tab open yet; navigating to %s", target_url)
            page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
            page.bring_to_front()
            return page

        raise RuntimeError(
            f"Could not find an open tab matching '{self.config.target_url_keyword}'."
        )

    def close(self) -> None:
        if self.playwright:
            try:
                self.playwright.stop()
            finally:
                self.playwright = None
                self.browser = None


class HumanInteractor:
    @staticmethod
    def move_and_click_point(page: Page, target_x: float, target_y: float) -> bool:
        try:
            page.mouse.move(target_x, target_y, steps=random.randint(10, 20))
            time.sleep(random.uniform(0.06, 0.18))
            page.mouse.click(target_x, target_y)
            return True
        except Exception:
            return False

    @staticmethod
    def move_and_click(page: Page, locator: Locator) -> bool:
        try:
            locator.wait_for(state="visible", timeout=2500)
            locator.scroll_into_view_if_needed(timeout=1500)
            box = locator.bounding_box()
            if box:
                target_x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
                target_y = box["y"] + box["height"] * random.uniform(0.25, 0.75)
                return HumanInteractor.move_and_click_point(page, target_x, target_y)
            locator.click()
            return True
        except Exception:
            return False


class FastInteractor:
    @staticmethod
    def click_point(page: Page, target_x: float, target_y: float) -> bool:
        try:
            page.mouse.click(target_x, target_y)
            return True
        except Exception:
            return False

    @staticmethod
    def click_locator(locator: Locator) -> bool:
        try:
            locator.click(timeout=1000)
            return True
        except Exception:
            return False


class BasketAlert:
    @staticmethod
    def trigger_alarm(stop_event: Optional[threading.Event] = None) -> None:
        logging.info("TICKETS LOCKED IN BASKET! COMPLETE CHECKOUT NOW!")
        if winsound is None:
            return
        for _ in range(6):
            if stop_event and stop_event.is_set():
                break
            winsound.Beep(1200, 300)
            winsound.Beep(1600, 300)
            time.sleep(0.08)


class UnifiedTicketPoller:
    """Searches events and scans Ticketmaster's SVG seat map using dynamic SniperConfig."""

    def __init__(self, page: Page, config: SniperConfig):
        self.page = page
        self.config = config
        self.restricted_zone_keys: set = set()

    def _click_point(self, x: float, y: float) -> bool:
        if self.config.fast_mode:
            return FastInteractor.click_point(self.page, x, y)
        return HumanInteractor.move_and_click_point(self.page, x, y)

    def _click_locator(self, locator: Locator) -> bool:
        if self.config.fast_mode:
            return FastInteractor.click_locator(locator)
        return HumanInteractor.move_and_click(self.page, locator)

    def _wait_js(
        self, js_expr: str, arg=None, timeout_ms: int = 1200, poll_ms: int = 25
    ) -> bool:
        try:
            self.page.wait_for_function(
                js_expr, arg=arg, timeout=timeout_ms, polling=poll_ms
            )
            return True
        except Exception:
            return False

    def is_in_basket(self) -> bool:
        current_url = self.page.url.lower()
        if any(
            token in current_url
            for token in ("checkout", "ostoskori", "cart", "reserve")
        ):
            return True

        checkout_button = self.page.locator(
            "button:has-text('Siirry maksamaan'), "
            "button:has-text('Siirry kassalle'), "
            "a:has-text('Siirry kassalle'), "
            "button:has-text('Tilaa nyt'), "
            "[data-bdd='basket-checkout-button']"
        ).first
        return checkout_button.is_visible()

    def is_on_matching_event_page(self, query: str) -> bool:
        """Return True if the browser is already on an event page matching query."""
        cleaned = query.strip()
        if not cleaned:
            return False

        current_url = self.page.url.lower()
        if "/event/" not in current_url:
            return False

        if cleaned.lower().startswith(("http://", "https://")):
            return current_url.rstrip("/") == cleaned.lower().rstrip("/")

        tokens = [t for t in re.split(r"\W+", cleaned.lower()) if len(t) >= 2]
        if not tokens:
            return False

        decoded_url = urllib.parse.unquote(current_url)
        page_text = ""
        try:
            evaluated = self.page.evaluate(
                "() => (document.title + ' ' + (document.querySelector('h1')?.innerText || '')).toLowerCase()"
            )
            if isinstance(evaluated, str):
                page_text = evaluated
        except Exception:
            page_text = ""

        haystack = f"{decoded_url} {page_text}"
        return all(token in haystack for token in tokens)

    def search_and_navigate_to_event(self, query: str) -> bool:
        """Navigate directly to an event URL or return to main page and run a search."""
        cleaned = query.strip()
        if not cleaned:
            return False

        self.restricted_zone_keys.clear()

        if cleaned.lower().startswith(("http://", "https://")):
            logging.info("Navigating directly to event URL: %s", cleaned)
            self.page.goto(cleaned, wait_until="domcontentloaded", timeout=25000)
            return True

        main_url = "https://www.ticketmaster.fi"
        logging.info(
            "Navigating back to main page (%s) before searching for '%s'...",
            main_url,
            cleaned,
        )
        self.page.goto(main_url, wait_until="domcontentloaded", timeout=25000)

        search_url = (
            f"https://www.ticketmaster.fi/search?q={urllib.parse.quote_plus(cleaned)}"
        )
        logging.info("Searching Ticketmaster Finland for '%s'...", cleaned)
        self.page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
        self._wait_js(
            "() => !!document.querySelector(\"main a[href*='/event/'], main a[href*='/artist/'], a[data-testid='event-list-link']\")",
            timeout_ms=4000,
            poll_ms=100,
        )

        result_link = self.page.locator(
            "a[data-testid='event-list-link'], "
            "main a:has-text('Katso liput'), "
            "main a:has-text('Löydä liput'), "
            "main a:has-text('Osta liput'), "
            "main a[href*='/event/'], "
            "main a[href*='/artist/'], "
            "a:has-text('Katso liput'), "
            "a:has-text('Löydä liput'), "
            "a:has-text('Osta liput')"
        ).first

        if result_link.is_visible():
            logging.info("Found matching search result; opening event/artist page...")
            self._click_locator(result_link)
            self._wait_js(
                "() => !location.href.toLowerCase().includes('/search')",
                timeout_ms=4000,
                poll_ms=100,
            )
            post_url = self.page.url.lower()
            if "/artist/" in post_url or "/music/" in post_url:
                self.navigate_from_artist_to_event_if_needed()
            return True

        logging.warning("No clickable event link found yet for '%s'.", cleaned)
        return False

    def navigate_from_artist_to_event_if_needed(self) -> bool:
        if "/artist/" not in self.page.url.lower() and "/music/" not in self.page.url.lower():
            return False

        event_links = self.page.locator(
            "a:has-text('Katso liput'), "
            "a:has-text('Löydä liput'), "
            "a:has-text('Osta liput'), "
            "a[data-testid='event-list-link'], "
            "main a[href*='/event/']"
        )
        if event_links.first.is_visible():
            logging.info("On artist page. Clicking through to the Event page...")
            self._click_locator(event_links.first)
            self._wait_js(
                "() => !location.href.toLowerCase().includes('/artist/') && !location.href.toLowerCase().includes('/music/')",
                timeout_ms=2500,
            )
            return True
        return False

    def ensure_seat_map_tab_active(self) -> None:
        seat_map_btn = self.page.locator(
            "button:has-text('View seat map'), "
            "a:has-text('View seat map'), "
            "[role='button']:has-text('View seat map'), "
            "button:has-text('Näytä pohjakartta'), "
            "a:has-text('Näytä pohjakartta'), "
            "[role='tab']:has-text('seat map')"
        ).first

        if seat_map_btn.is_visible():
            is_selected = seat_map_btn.get_attribute("aria-selected")
            map_visible = self.page.locator(
                "[data-component='zoomer'], svg [data-component='svg__section']"
            ).first.is_visible()
            if is_selected != "true" and not (is_selected is None and map_visible):
                logging.info("Clicking 'View seat map'...")
                self._click_locator(seat_map_btn)
                self._wait_js(
                    "() => !!document.querySelector(\"[data-component='zoomer'], svg [data-component='svg__section']\")",
                    timeout_ms=1800,
                )

    def click_reset_zoom(self) -> bool:
        reset_btn = self.page.locator(
            "button:has-text('Reset Zoom'), "
            "button:has-text('Nollaa'), "
            "button[aria-label*='Reset Zoom' i], "
            "button[title*='Reset Zoom' i]"
        ).first
        if reset_btn.is_visible() and reset_btn.is_enabled():
            logging.info("Clicking 'Reset Zoom' to return to venue map...")
            if self._click_locator(reset_btn):
                self._wait_js(
                    """() => {
                        const inner = document.querySelector("[data-component='zoomer'] > div");
                        if (!inner) return true;
                        const m = (inner.style.transform || "").match(/scale3d\\(([^,]+),/);
                        return !m || parseFloat(m[1]) <= 1.1;
                    }""",
                    timeout_ms=1200,
                )
                return True
        return False

    def is_map_zoomed_in(self) -> bool:
        return self.page.evaluate(
            """() => {
                const inner = document.querySelector("[data-component='zoomer'] > div");
                if (!inner) return false;
                const m = (inner.style.transform || "").match(/scale3d\\(([^,]+),/);
                return m ? parseFloat(m[1]) > 1.1 : false;
            }"""
        )

    def find_blue_zones(self) -> List[dict]:
        exclude_keywords = list(self.config.get_exclude_keywords())
        preferred_keywords = list(self.config.get_preferred_keywords())
        result = self.page.evaluate(
            """({ excludeKeywords, preferredKeywords }) => {
                const seen = new Set();
                const seenSkipped = new Set();
                const zones = [];
                const skippedZones = [];
                const elements = document.querySelectorAll("svg [data-component='svg__section'][data-active='true']");

                for (const el of elements) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 8 || rect.height < 8) continue;

                    const style = getComputedStyle(el);
                    if (style.display === "none" || style.visibility === "hidden" || style.pointerEvents === "none") {
                        continue;
                    }

                    const match = style.fill.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                    if (!match) continue;
                    const [r, g, b] = [Number(match[1]), Number(match[2]), Number(match[3])];
                    const isBlue = r < 100 && g < 140 && b > 170 && b > r * 1.8;
                    if (!isBlue) continue;

                    const key =
                        el.getAttribute("data-section-id") ||
                        el.getAttribute("data-section-name") ||
                        `${Math.round(rect.left)}:${Math.round(rect.top)}`;
                    const name = el.getAttribute("data-section-name") || key;

                    const zoneText = [
                        el.getAttribute("data-section-name"),
                        el.getAttribute("aria-label"),
                        el.getAttribute("title"),
                    ].filter(Boolean).join(" ").toLowerCase();

                    const matchedExcluded = excludeKeywords.filter(k => zoneText.includes(k));
                    if (matchedExcluded.length > 0) {
                        if (!seenSkipped.has(key)) {
                            seenSkipped.add(key);
                            skippedZones.push({ name, keywords: matchedExcluded });
                        }
                        continue;
                    }

                    if (seen.has(key)) continue;
                    seen.add(key);

                    const preferredScore = preferredKeywords.reduce(
                        (acc, kw) => acc + (zoneText.includes(kw) ? 1 : 0),
                        0
                    );

                    zones.push({
                        key,
                        name,
                        x: rect.left + rect.width / 2,
                        y: rect.top + rect.height / 2,
                        preferredScore,
                    });
                }

                zones.sort((a, b) => b.preferredScore - a.preferredScore);
                return { zones, skippedZones };
            }""",
            {
                "excludeKeywords": exclude_keywords,
                "preferredKeywords": preferred_keywords,
            },
        )
        for skipped in result["skippedZones"]:
            logging.info(
                "Skipping excluded zone '%s' (matched: %s).",
                skipped["name"],
                skipped["keywords"],
            )
        return result["zones"]

    def find_seats_in_zone(
        self, quantity: int, target_zone_key: Optional[str] = None
    ) -> List[dict]:
        exclude_keywords = list(self.config.get_exclude_keywords())
        preferred_keywords = list(self.config.get_preferred_keywords())
        result = self.page.evaluate(
            """({ quantity, excludeKeywords, preferredKeywords, targetZoneKey, adjacentOnly }) => {
                const allSeats = Array.from(document.querySelectorAll("svg circle[data-component='svg__seat']"));
                const hasZoneMatch = targetZoneKey
                    ? allSeats.some(el => {
                        const sec = el.closest("[data-section-id], [data-section-name], [id]");
                        return sec && (
                            sec.getAttribute("data-section-id") === targetZoneKey ||
                            sec.getAttribute("data-section-name") === targetZoneKey ||
                            sec.getAttribute("id") === targetZoneKey
                        );
                    })
                    : false;

                const rows = new Map();
                for (const el of allSeats) {
                    if (hasZoneMatch) {
                        const sec = el.closest("[data-section-id], [data-section-name], [id]");
                        const matchesZone = sec && (
                            sec.getAttribute("data-section-id") === targetZoneKey ||
                            sec.getAttribute("data-section-name") === targetZoneKey ||
                            sec.getAttribute("id") === targetZoneKey
                        );
                        if (!matchesZone) continue;
                    }

                    const parent = el.parentElement;
                    if (!parent) continue;
                    if (!rows.has(parent)) rows.set(parent, []);
                    rows.get(parent).push(el);
                }

                const matchedKeywordsSet = new Set();
                const sampleSkippedSeats = [];
                let skippedCount = 0;
                const nonAdjacentCandidates = [];

                const inspectSeat = (seat) => {
                    const rect = seat.getBoundingClientRect();
                    const inViewport =
                        rect.width > 0 &&
                        rect.height > 0 &&
                        rect.left >= 0 &&
                        rect.top >= 0 &&
                        rect.right <= window.innerWidth &&
                        rect.bottom <= window.innerHeight;
                    const hit = inViewport
                        ? document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
                        : null;
                    const isClickableOnMap = inViewport && hit && hit.closest("[data-component='svg']") !== null;

                    const fill = getComputedStyle(seat).fill;
                    const block = seat.closest("[data-component='svg__block'], [data-section-name]");
                    const seatMeta = [
                        seat.getAttribute("aria-label"),
                        seat.getAttribute("title"),
                        seat.getAttribute("data-ticket-type"),
                        block ? block.getAttribute("data-section-name") : null,
                        block ? block.getAttribute("aria-label") : null,
                    ].filter(Boolean).join(" ").toLowerCase();

                    const matchedExcluded = excludeKeywords.filter(k => seatMeta.includes(k));
                    const isBlueSeat = isClickableOnMap && seat.hasAttribute("type") && fill === "rgb(42, 85, 217)";

                    if (isBlueSeat && matchedExcluded.length > 0) {
                        skippedCount += 1;
                        for (const kw of matchedExcluded) matchedKeywordsSet.add(kw);
                        if (sampleSkippedSeats.length < 3) {
                            sampleSkippedSeats.push(
                                seat.getAttribute("data-seat-name") || seat.getAttribute("id") || "unknown"
                            );
                        }
                    }

                    const preferredScore = preferredKeywords.reduce(
                        (acc, kw) => acc + (seatMeta.includes(kw) ? 1 : 0),
                        0
                    );

                    return {
                        isAvailable: isBlueSeat && matchedExcluded.length === 0,
                        rect,
                        preferredScore,
                    };
                };

                for (const seats of rows.values()) {
                    seats.sort((a, b) => parseFloat(a.getAttribute("cx") || "0") - parseFloat(b.getAttribute("cx") || "0"));
                    let run = [];
                    let prevSeat = null;

                    for (const seat of seats) {
                        const info = inspectSeat(seat);
                        if (!info.isAvailable) {
                            run = [];
                            prevSeat = null;
                            continue;
                        }

                        if (!adjacentOnly) {
                            const seatId = seat.getAttribute("id") || "";
                            if (seatId) {
                                nonAdjacentCandidates.push({
                                    id: seatId,
                                    name: seat.getAttribute("data-seat-name") || seatId,
                                    x: info.rect.left + info.rect.width / 2,
                                    y: info.rect.top + info.rect.height / 2,
                                    preferredScore: info.preferredScore,
                                });
                            }
                            continue;
                        }

                        if (prevSeat) {
                            const cx1 = parseFloat(prevSeat.getAttribute("cx") || "0");
                            const cx2 = parseFloat(seat.getAttribute("cx") || "0");
                            const cy1 = parseFloat(prevSeat.getAttribute("cy") || "0");
                            const cy2 = parseFloat(seat.getAttribute("cy") || "0");
                            const r = parseFloat(seat.getAttribute("r") || "14.5");
                            if (Math.hypot(cx2 - cx1, cy2 - cy1) > r * 3.5) {
                                run = [{ seat, rect: info.rect }];
                                prevSeat = seat;
                                continue;
                            }
                        }

                        run.push({ seat, rect: info.rect });
                        prevSeat = seat;

                        if (run.length === quantity) {
                            return {
                                seats: run.map(({ seat: s, rect: r }) => ({
                                    id: s.getAttribute("id") || "",
                                    name: s.getAttribute("data-seat-name") || s.getAttribute("id") || "unknown",
                                    x: r.left + r.width / 2,
                                    y: r.top + r.height / 2,
                                })).filter(s => s.id),
                                skippedCount,
                                matchedKeywords: Array.from(matchedKeywordsSet),
                                sampleSkippedSeats,
                            };
                        }
                    }
                }

                if (!adjacentOnly && nonAdjacentCandidates.length >= quantity) {
                    nonAdjacentCandidates.sort((a, b) => b.preferredScore - a.preferredScore);
                    return {
                        seats: nonAdjacentCandidates.slice(0, quantity),
                        skippedCount,
                        matchedKeywords: Array.from(matchedKeywordsSet),
                        sampleSkippedSeats,
                    };
                }

                return {
                    seats: [],
                    skippedCount,
                    matchedKeywords: Array.from(matchedKeywordsSet),
                    sampleSkippedSeats,
                };
            }""",
            {
                "quantity": quantity,
                "excludeKeywords": exclude_keywords,
                "preferredKeywords": preferred_keywords,
                "targetZoneKey": target_zone_key,
                "adjacentOnly": self.config.adjacent_only,
            },
        )
        if result["skippedCount"] > 0:
            logging.info(
                "Skipped %d excluded seat(s) (matched: %s, samples: %s).",
                result["skippedCount"],
                result["matchedKeywords"],
                result["sampleSkippedSeats"],
            )
        return result["seats"]

    def _click_seats(
        self, seats: List[dict], stop_event: Optional[threading.Event] = None
    ) -> Tuple[int, bool]:
        exclude_keywords = self.config.get_exclude_keywords()
        mode_label = "adjacent" if self.config.adjacent_only else "available"
        logging.info("Found %d %s seat(s); selecting...", len(seats), mode_label)
        selected = 0

        for seat in seats:
            if stop_event and stop_event.is_set():
                return selected, False

            seat_locator = self.page.locator(f"svg circle[id='{seat['id']}']")
            clicked = False
            if self.config.fast_mode:
                clicked = FastInteractor.click_point(self.page, seat["x"], seat["y"])
            else:
                if seat_locator.is_visible():
                    box = seat_locator.bounding_box()
                    if box:
                        clicked = HumanInteractor.move_and_click_point(
                            self.page,
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                if not clicked:
                    clicked = HumanInteractor.move_and_click_point(
                        self.page, seat["x"], seat["y"]
                    )

            if clicked:
                selected += 1
                logging.info(
                    "Clicked seat %d/%d (%s).",
                    selected,
                    self.config.ticket_quantity,
                    seat["name"],
                )
                if not self.config.fast_mode:
                    time.sleep(random.uniform(0.35, 0.65))
                    details = self.get_selected_ticket_details()
                    matched_excluded = [
                        kw for kw in exclude_keywords if kw in details["text"]
                    ]
                    if matched_excluded:
                        logging.warning(
                            "Selected seat '%s' is restricted (%s). Clearing selection...",
                            seat["name"],
                            matched_excluded,
                        )
                        self.clear_selected_tickets()
                        return 0, True

        if selected == 0:
            return 0, False

        if self.config.fast_mode:
            self._wait_js(
                """(expected) => {
                    const cart = document.querySelector("[data-testid='ticket-cart'], [data-bdd='cart-count'], #list-view");
                    const text = (cart ? cart.innerText : document.body.innerText) || "";
                    const multMatch = text.match(/[×x]\\s*(\\d+)/i);
                    if (multMatch && parseInt(multMatch[1], 10) >= expected) return true;
                    const wordMatch = text.match(/\\b(\\d+)\\s*(?:lippu|lippua|ticket|tickets)\\b/i);
                    return wordMatch ? parseInt(wordMatch[1], 10) >= expected : false;
                }""",
                arg=selected,
                timeout_ms=900,
            )
            details = self.get_selected_ticket_details()
            matched_excluded = [kw for kw in exclude_keywords if kw in details["text"]]
            if matched_excluded:
                logging.warning(
                    "Selected seats contain restricted label(s) (%s). Clearing selection...",
                    matched_excluded,
                )
                self.clear_selected_tickets()
                return 0, True

        return selected, False

    def select_seats(self, stop_event: Optional[threading.Event] = None) -> int:
        if self.is_map_zoomed_in():
            logging.info("Map is zoomed in. Resetting zoom to inspect blue zones...")
            self.click_reset_zoom()

        zones = self.find_blue_zones()
        if not zones and self.click_reset_zoom():
            zones = self.find_blue_zones()

        logging.info("Detected %d eligible blue zone(s) on venue map.", len(zones))
        tried_keys = set()

        for zone in zones:
            if stop_event and stop_event.is_set():
                return 0
            if zone["key"] in tried_keys or zone["key"] in self.restricted_zone_keys:
                continue
            tried_keys.add(zone["key"])

            zone_label = zone.get("name", zone["key"])
            logging.info("Clicking into blue zone '%s' (%s)...", zone_label, zone["key"])
            if not self._click_point(zone["x"], zone["y"]):
                continue

            if self.config.fast_mode:
                self._wait_js(
                    """() => {
                        const seats = document.querySelectorAll("svg circle[data-component='svg__seat']");
                        if (!seats.length) return false;
                        const inner = document.querySelector("[data-component='zoomer'] > div");
                        if (!inner) return true;
                        const m = (inner.style.transform || "").match(/scale3d\\(([^,]+),/);
                        return m ? parseFloat(m[1]) > 1.1 : true;
                    }""",
                    timeout_ms=1400,
                )
            else:
                time.sleep(1.6)

            seats = self.find_seats_in_zone(
                self.config.ticket_quantity,
                target_zone_key=zone["key"],
            )
            if len(seats) >= self.config.ticket_quantity:
                clicked, was_restricted = self._click_seats(seats, stop_event=stop_event)
                if was_restricted:
                    self.restricted_zone_keys.add(zone["key"])
                    logging.info(
                        "Marked zone '%s' (%s) as restricted; will skip in future scans.",
                        zone_label,
                        zone["key"],
                    )
                    self.click_reset_zoom()
                    continue
                if clicked >= self.config.ticket_quantity:
                    return clicked
                logging.info(
                    "Zone '%s' selection incomplete. Resetting zoom...", zone_label
                )
                self.click_reset_zoom()
                continue

            logging.info(
                "Zone '%s' lacks %d matching seat(s). Resetting zoom...",
                zone_label,
                self.config.ticket_quantity,
            )
            self.click_reset_zoom()

        return 0

    def get_selected_ticket_details(self) -> dict:
        return self.page.evaluate(
            """() => {
                const cart = document.querySelector("[data-testid='ticket-cart'], [data-bdd='cart-count'], #list-view");
                const text = (cart ? cart.innerText : document.body.innerText) || "";
                let count = 0;
                const multMatch = text.match(/[×x]\\s*(\\d+)/i);
                if (multMatch) {
                    count = parseInt(multMatch[1], 10);
                } else {
                    const wordMatch = text.match(/\\b(\\d+)\\s*(?:lippu|lippua|ticket|tickets)\\b/i);
                    if (wordMatch) count = parseInt(wordMatch[1], 10);
                }
                return { count, text: text.toLowerCase() };
            }"""
        )

    def clear_selected_tickets(self) -> None:
        remove_btns = self.page.locator(
            "#list-view button:has-text('Remove ticket'), "
            "#list-view button:has-text('Poista lippu'), "
            "[data-testid='ticket-cart'] button:has-text('Remove ticket'), "
            "[data-testid='ticket-cart'] button:has-text('Poista lippu'), "
            "[data-testid='ticket-cart'] button[aria-label*='Remove' i], "
            "[data-testid='ticket-cart'] button[aria-label*='Poista' i], "
            "#list-view button[aria-label*='Remove' i], "
            "#list-view button[aria-label*='Poista' i]"
        )
        while remove_btns.count() > 0 and remove_btns.first.is_visible():
            self._click_locator(remove_btns.first)
            time.sleep(0.25 if self.config.fast_mode else 0.55)

    def click_get_tickets(self) -> bool:
        get_tickets_btn = self.page.locator(
            "button:has-text('Get Tickets'), "
            "button:has-text('Hae liput'), "
            "button:has-text('Osta liput'), "
            "button[data-bdd='get-tickets-button'], "
            "button[data-testid='getTicketsBtn']"
        ).first

        if get_tickets_btn.is_visible() and get_tickets_btn.is_enabled():
            logging.info("Green 'Get Tickets' button is active! Clicking to lock basket...")
            if self._click_locator(get_tickets_btn):
                self._wait_js(
                    """() => {
                        const u = location.href.toLowerCase();
                        if (u.includes('checkout') || u.includes('ostoskori') || u.includes('cart') || u.includes('reserve')) return true;
                        return !!document.querySelector("[data-bdd='basket-checkout-button']");
                    }""",
                    timeout_ms=2800,
                )
                return True
        return False

    def attempt_seat_lock(
        self, stop_event: Optional[threading.Event] = None
    ) -> bool:
        if stop_event and stop_event.is_set():
            return False

        if self.is_in_basket():
            return True

        if self.config.search_query.strip():
            current = self.page.url.lower()
            if (
                "/event/" not in current
                and "/artist/" not in current
                and "/music/" not in current
                and "checkout" not in current
            ):
                if self.search_and_navigate_to_event(self.config.search_query):
                    return False

        if self.navigate_from_artist_to_event_if_needed():
            return False

        self.ensure_seat_map_tab_active()

        exclude_keywords = self.config.get_exclude_keywords()
        details = self.get_selected_ticket_details()
        if details["count"] > 0:
            matched_excluded = [kw for kw in exclude_keywords if kw in details["text"]]
            if matched_excluded:
                logging.warning(
                    "Existing selection contains excluded label(s) %s. Clearing...",
                    matched_excluded,
                )
                self.clear_selected_tickets()
                details = self.get_selected_ticket_details()

        if details["count"] < self.config.ticket_quantity:
            if details["count"] > 0:
                logging.info(
                    "Only %d ticket(s) selected (need %d). Clearing partial selection...",
                    details["count"],
                    self.config.ticket_quantity,
                )
                self.clear_selected_tickets()
            picked = self.select_seats(stop_event=stop_event)
            if picked < self.config.ticket_quantity:
                logging.info(
                    "Found %d/%d matching seat(s). Waiting for next scan...",
                    picked,
                    self.config.ticket_quantity,
                )
                return False

        verified = self.get_selected_ticket_details()
        matched_excluded = [kw for kw in exclude_keywords if kw in verified["text"]]
        if matched_excluded:
            logging.warning(
                "Selected tickets contain excluded label(s) %s. Clearing selection...",
                matched_excluded,
            )
            self.clear_selected_tickets()
            self.click_reset_zoom()
            return False

        if verified["count"] != self.config.ticket_quantity:
            logging.warning(
                "'Your selection' shows x%d instead of x%d. Clearing selection...",
                verified["count"],
                self.config.ticket_quantity,
            )
            if verified["count"] > 0:
                self.clear_selected_tickets()
            return False

        logging.info(
            "Verified selection count: x%d. Proceeding to 'Get Tickets'...",
            verified["count"],
        )
        if self.click_get_tickets():
            if self.is_in_basket():
                return True

        return False

    def run_loop(
        self,
        stop_event: threading.Event,
        on_status: Optional[Callable[[str, str], None]] = None,
        on_locked: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Poll until tickets are locked in basket or stop_event is set."""
        attempt = 1
        logging.info(
            "Starting seat poller (qty=%d, adjacent=%s, under18=%s, fast=%s)...",
            self.config.ticket_quantity,
            self.config.adjacent_only,
            self.config.under_18_only,
            self.config.fast_mode,
        )
        while not stop_event.is_set():
            if on_status:
                on_status("SCANNING", f"Scanning seat map (Attempt #{attempt})")
            logging.info("Poll attempt #%d on %s", attempt, self.page.url)

            try:
                locked = self.attempt_seat_lock(stop_event=stop_event)
            except Exception as exc:
                logging.error("Poll attempt #%d error: %s", attempt, exc)
                locked = False

            if locked:
                if on_status:
                    on_status("LOCKED", "TICKETS LOCKED IN BASKET! Complete checkout now!")
                if on_locked:
                    on_locked()
                BasketAlert.trigger_alarm(stop_event=stop_event)
                return True

            sleep_duration = random.uniform(
                self.config.min_poll_sleep_seconds,
                self.config.max_poll_sleep_seconds,
            )
            if stop_event.wait(timeout=sleep_duration):
                break
            attempt += 1

        if on_status:
            on_status("STOPPED", "Search stopped.")
        logging.info("Polling stopped.")
        return False
