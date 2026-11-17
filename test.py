import time
import random
import logging
import winsound
from dataclasses import dataclass
from typing import Optional, List, Tuple
from playwright.sync_api import sync_playwright, Browser, Page, Locator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)

@dataclass
class MapSniperConfig:
    cdp_endpoint: str = "http://127.0.0.1:9222"
    target_url_keyword: str = "ticketmaster.fi"
    ticket_quantity: int = 2
    min_jitter_seconds: float = 2.5
    max_jitter_seconds: float = 4.8
    exclude_keywords: Tuple[str, ...] = ("vip", "platinum", "pyörätuoli", "k18", "k-18")


class BrowserConnector:
    def __init__(self, config: MapSniperConfig):
        self.config = config
        self.playwright = None
        self.browser: Optional[Browser] = None

    def connect_and_get_page(self) -> Page:
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.connect_over_cdp(self.config.cdp_endpoint)

        if not self.browser.contexts:
            raise RuntimeError("No active browser found on CDP.")

        default_context = self.browser.contexts[0]
        for page in default_context.pages:
            if self.config.target_url_keyword in page.url.lower():
                logging.info(f"Attached to active tab: {page.url}")
                page.bring_to_front()
                return page

        raise RuntimeError(f"Could not find an open tab matching '{self.config.target_url_keyword}'.")

    def close(self) -> None:
        if self.playwright:
            self.playwright.stop()


class HumanInteractor:
    @staticmethod
    def move_and_click_point(page: Page, target_x: float, target_y: float) -> bool:
        try:
            page.mouse.move(target_x, target_y, steps=random.randint(8, 18))
            time.sleep(random.uniform(0.06, 0.16))
            page.mouse.click(target_x, target_y)
            return True
        except Exception:
            return False

    @staticmethod
    def move_and_click(page: Page, locator: Locator) -> bool:
        try:
            locator.wait_for(state="visible", timeout=2500)
            box = locator.bounding_box()
            if box:
                target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                return HumanInteractor.move_and_click_point(page, target_x, target_y)
            locator.click()
            return True
        except Exception:
            return False


class BasketAlert:
    @staticmethod
    def trigger_alarm() -> None:
        logging.info("MAP SUCCESS: 2 SEATS LOCKED IN BASKET! PROCEED TO CHECKOUT!")
        for _ in range(8):
            winsound.Beep(1200, 300)
            winsound.Beep(1600, 300)
            time.sleep(0.08)


class MapSeatPoller:
    def __init__(self, page: Page, config: MapSniperConfig):
        self.page = page
        self.config = config

    def is_in_basket(self) -> bool:
        current_url = self.page.url.lower()
        if any(token in current_url for token in ("checkout", "ostoskori", "cart", "reserve")):
            return True

        checkout_button = self.page.locator(
            "button:has-text('Siirry maksamaan'), "
            "button:has-text('Siirry kassalle'), "
            "a:has-text('Siirry kassalle')"
        ).first
        return checkout_button.is_visible()

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
            map_visible = self.page.locator("[data-component='zoomer'], svg [data-component='svg__section']").first.is_visible()
            if is_selected != "true" and not (is_selected is None and map_visible):
                logging.info("Clicking 'View seat map'...")
                HumanInteractor.move_and_click(self.page, seat_map_btn)
                time.sleep(2.0)

    def click_reset_zoom(self) -> bool:
        reset_btn = self.page.locator(
            "button:has-text('Reset Zoom'), "
            "button:has-text('Nollaa'), "
            "button[aria-label*='Reset Zoom' i], "
            "button[title*='Reset Zoom' i]"
        ).first
        if reset_btn.is_visible() and reset_btn.is_enabled():
            logging.info("Clicking 'Reset Zoom' to return to venue map...")
            if HumanInteractor.move_and_click(self.page, reset_btn):
                time.sleep(1.5)
                return True
        return False

    def find_blue_zones(self) -> List[dict]:
        return self.page.evaluate(
            """() => {
                const seen = new Set();
                const zones = [];
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
                    if (seen.has(key)) continue;
                    seen.add(key);

                    zones.push({
                        key,
                        name: el.getAttribute("data-section-name") || key,
                        x: rect.left + rect.width / 2,
                        y: rect.top + rect.height / 2,
                    });
                }
                return zones;
            }"""
        )

    def _click_seat_ids(self, seat_ids: List[str]) -> int:
        logging.info(f"Found {len(seat_ids)} adjacent available seats to select.")
        selected = 0
        for seat_id in seat_ids:
            seat = self.page.locator(f"svg circle[id='{seat_id}']")
            if not seat.is_visible():
                continue

            seat_name = seat.get_attribute("data-seat-name") or seat_id
            logging.info(f"Clicking adjacent seat {selected + 1}/{self.config.ticket_quantity} (seat {seat_name})...")
            if HumanInteractor.move_and_click(self.page, seat):
                selected += 1
                time.sleep(random.uniform(0.35, 0.65))
        return selected

    def find_available_seat_elements(self) -> Locator:
        return self.page.locator(
            "svg circle[data-component='svg__seat'][type], "
            "svg circle[data-bdd*='seat'][data-available='true'], "
            "svg [role='button'][aria-label*='Seat' i]:not([aria-disabled='true']), "
            "svg [role='button'][aria-label*='Paikka' i]:not([aria-disabled='true']), "
            "svg g[data-bdd='seat-node'][data-status='available']"
        )

    def find_adjacent_seat_ids(self, quantity: int, target_zone_key: Optional[str] = None) -> List[str]:
        return self.page.evaluate(
            """({ quantity, excludeKeywords, targetZoneKey }) => {
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

                for (const seats of rows.values()) {
                    seats.sort((a, b) => parseFloat(a.getAttribute("cx") || "0") - parseFloat(b.getAttribute("cx") || "0"));
                    let run = [];
                    let prevSeat = null;

                    for (const seat of seats) {
                        const rect = seat.getBoundingClientRect();
                        const inViewport =
                            rect.width > 0 &&
                            rect.height > 0 &&
                            rect.left >= 0 &&
                            rect.top >= 0 &&
                            rect.right <= window.innerWidth &&
                            rect.bottom <= window.innerHeight;
                        const isTopmost = inViewport &&
                            document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2) === seat;

                        const fill = getComputedStyle(seat).fill;
                        const aria = (seat.getAttribute("aria-label") || "").toLowerCase();
                        const isAvailable =
                            isTopmost &&
                            seat.hasAttribute("type") &&
                            fill === "rgb(42, 85, 217)" &&
                            !excludeKeywords.some(k => aria.includes(k));

                        if (!isAvailable) {
                            run = [];
                            prevSeat = null;
                            continue;
                        }

                        if (prevSeat) {
                            const cx1 = parseFloat(prevSeat.getAttribute("cx") || "0");
                            const cx2 = parseFloat(seat.getAttribute("cx") || "0");
                            const cy1 = parseFloat(prevSeat.getAttribute("cy") || "0");
                            const cy2 = parseFloat(seat.getAttribute("cy") || "0");
                            const r = parseFloat(seat.getAttribute("r") || "14.5");
                            if (Math.hypot(cx2 - cx1, cy2 - cy1) > r * 3.5) {
                                run = [seat];
                                prevSeat = seat;
                                continue;
                            }
                        }

                        run.push(seat);
                        prevSeat = seat;

                        if (run.length === quantity) {
                            return run.map(s => s.getAttribute("id")).filter(Boolean);
                        }
                    }
                }
                return [];
            }""",
            {
                "quantity": quantity,
                "excludeKeywords": list(self.config.exclude_keywords),
                "targetZoneKey": target_zone_key,
            },
        )

    def is_map_zoomed_in(self) -> bool:
        return self.page.evaluate(
            """() => {
                const inner = document.querySelector("[data-component='zoomer'] > div");
                if (!inner) return false;
                const m = (inner.style.transform || "").match(/scale3d\\(([^,]+),/);
                return m ? parseFloat(m[1]) > 1.1 : false;
            }"""
        )

    def select_two_seats(self) -> int:
        if self.is_map_zoomed_in():
            logging.info("Map is currently zoomed in. Resetting zoom to inspect blue zones...")
            self.click_reset_zoom()

        zones = self.find_blue_zones()
        logging.info(f"Detected {len(zones)} blue zone(s) on venue map.")
        tried_keys = set()

        for zone in zones:
            if zone["key"] in tried_keys:
                continue
            tried_keys.add(zone["key"])

            zone_label = zone.get("name", zone["key"])
            logging.info(f"Clicking into blue zone '{zone_label}' ({zone['key']})...")
            if not HumanInteractor.move_and_click_point(self.page, zone["x"], zone["y"]):
                continue
            time.sleep(1.8)

            seat_ids = self.find_adjacent_seat_ids(
                self.config.ticket_quantity,
                target_zone_key=zone["key"],
            )
            if len(seat_ids) >= self.config.ticket_quantity:
                return self._click_seat_ids(seat_ids)

            logging.info(f"Zone '{zone_label}' has no {self.config.ticket_quantity} adjacent seats. Resetting zoom...")
            self.click_reset_zoom()

        return 0

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
            if HumanInteractor.move_and_click(self.page, get_tickets_btn):
                time.sleep(random.uniform(2.2, 3.5))
                return True
        return False

    def attempt_map_lock(self) -> bool:
        if self.is_in_basket():
            return True

        self.ensure_seat_map_tab_active()

        selected_badge = self.page.locator(
            "text=/\\b[1-9]\\s*lip|\\b[1-9]\\s*ticket/i"
        ).or_(self.page.locator("[data-bdd='cart-count']"))
        has_selection = selected_badge.count() > 0 or self.page.locator("button:has-text('Get Tickets'):enabled").count() > 0

        if not has_selection:
            picked = self.select_two_seats()
            if picked < self.config.ticket_quantity:
                logging.info(f"Only found {picked} seats. Zooming or repositioning...")
                return False

        input("Paused before clicking 'Get Tickets'. Press Enter to continue (or Ctrl+C to stop)...")
        if self.click_get_tickets():
            if self.is_in_basket():
                return True

        return False

    def run_loop(self) -> None:
        attempt = 1
        logging.info("Starting MAP-BASED seat selection loop...")
        while True:
            logging.info(f"Map poll attempt #{attempt} on {self.page.url}")
            locked = self.attempt_map_lock()

            if locked:
                BasketAlert.trigger_alarm()
                break

            sleep_duration = random.uniform(
                self.config.min_jitter_seconds,
                self.config.max_jitter_seconds
            )
            logging.info(f"Waiting {sleep_duration:.2f}s before next map scan...")
            time.sleep(sleep_duration)
            attempt += 1


if __name__ == "__main__":
    config = MapSniperConfig()
    connector = BrowserConnector(config)
    try:
        active_page = connector.connect_and_get_page()
        poller = MapSeatPoller(active_page, config)
        poller.run_loop()
    except KeyboardInterrupt:
        logging.info("Map polling stopped by user.")
    finally:
        connector.close()