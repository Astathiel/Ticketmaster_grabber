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
class SniperConfig:
    cdp_endpoint: str = "http://127.0.0.1:9222"
    target_url_keyword: str = "ticketmaster.fi"
    ticket_quantity: int = 2
    min_jitter_seconds: float = 3.6
    max_jitter_seconds: float = 7.2
    exclude_keywords: Tuple[str, ...] = (
        "vip", "platinum", "hot ticket", "paketti",
        "k18", "k-18", "k 18", "18+", "anniskelu", "ikäraja 18",
        "kenttä", "permanto", "seisomapaikka",
        "golden circle", "pyörätuoli", "saattaja", "esteetön"
    )
    preferred_keywords: Tuple[str, ...] = (
        "peruslippu", "istumapaikat", "katsomo", "paikka", "normaali"
    )


class BrowserConnector:
    def __init__(self, config: SniperConfig):
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


class BasketAlert:
    @staticmethod
    def trigger_alarm() -> None:
        logging.info("FOO FIGHTERS TICKETS LOCKED IN BASKET! COMPLETE CHECKOUT NOW!")
        for _ in range(8):
            winsound.Beep(1200, 350)
            winsound.Beep(1600, 350)
            time.sleep(0.1)


class TicketPoller:
    def __init__(self, page: Page, config: SniperConfig):
        self.page = page
        self.config = config
        self.stepper_set_to_target: bool = False
        self.restricted_zone_keys: set = set()

    def is_in_basket(self) -> bool:
        current_url = self.page.url.lower()
        if any(token in current_url for token in ("checkout", "ostoskori", "cart", "reserve")):
            return True

        checkout_button = self.page.locator(
            "button:has-text('Siirry maksamaan'), "
            "button:has-text('Siirry kassalle'), "
            "a:has-text('Siirry kassalle'), "
            "button:has-text('Tilaa nyt'), "
            "[data-bdd='basket-checkout-button']"
        ).first
        return checkout_button.is_visible()

    def navigate_from_artist_to_event_if_needed(self) -> bool:
        if "/artist/" not in self.page.url.lower() and "/music/" not in self.page.url.lower():
            return False

        event_links = self.page.locator(
            "a:has-text('Katso liput'), "
            "a:has-text('Löydä liput'), "
            "a:has-text('Osta liput'), "
            "a[data-testid='event-list-link']"
        )
        if event_links.first.is_visible():
            logging.info("On artist page. Clicking through to the Foo Fighters Event page...")
            HumanInteractor.move_and_click(self.page, event_links.first)
            time.sleep(random.uniform(2.0, 3.2))
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
        return self.page.evaluate(
            """({ excludeKeywords }) => {
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

                    const zoneText = [
                        el.getAttribute("data-section-name"),
                        el.getAttribute("aria-label"),
                        el.getAttribute("title"),
                    ].filter(Boolean).join(" ").toLowerCase();
                    if (excludeKeywords.some(k => zoneText.includes(k))) continue;

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
            }""",
            {"excludeKeywords": list(self.config.exclude_keywords)},
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
                        const isAvailable =
                            isClickableOnMap &&
                            seat.hasAttribute("type") &&
                            fill === "rgb(42, 85, 217)" &&
                            !excludeKeywords.some(k => seatMeta.includes(k));

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

    def _click_seat_ids(self, seat_ids: List[str]) -> Tuple[int, bool]:
        logging.info(f"Found {len(seat_ids)} adjacent available seats to select.")
        selected = 0
        for seat_id in seat_ids:
            seat = self.page.locator(f"svg circle[id='{seat_id}']")
            if not seat.is_visible():
                continue

            box = seat.bounding_box()
            if not box:
                continue

            seat_name = seat.get_attribute("data-seat-name") or seat_id
            logging.info(f"Clicking adjacent seat {selected + 1}/{self.config.ticket_quantity} (seat {seat_name})...")
            if HumanInteractor.move_and_click_point(
                self.page,
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2,
            ):
                selected += 1
                time.sleep(random.uniform(0.45, 0.75))

                details = self.get_selected_ticket_details()
                matched_excluded = [kw for kw in self.config.exclude_keywords if kw in details["text"]]
                if matched_excluded:
                    logging.warning(
                        f"Selected seat '{seat_name}' is restricted ({matched_excluded}). Clearing selection..."
                    )
                    self.clear_selected_tickets()
                    return 0, True
        return selected, False

    def select_two_seats(self) -> int:
        if self.is_map_zoomed_in():
            logging.info("Map is currently zoomed in. Resetting zoom to inspect blue zones...")
            self.click_reset_zoom()

        zones = self.find_blue_zones()
        if not zones and self.click_reset_zoom():
            zones = self.find_blue_zones()
        logging.info(f"Detected {len(zones)} blue zone(s) on venue map.")
        tried_keys = set()

        for zone in zones:
            if zone["key"] in tried_keys or zone["key"] in self.restricted_zone_keys:
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
                clicked, was_restricted = self._click_seat_ids(seat_ids)
                if was_restricted:
                    self.restricted_zone_keys.add(zone["key"])
                    logging.info(f"Marked zone '{zone_label}' ({zone['key']}) as restricted; will skip in future scans.")
                    self.click_reset_zoom()
                    continue
                if clicked >= self.config.ticket_quantity:
                    return clicked
                logging.info(f"Zone '{zone_label}' selection incomplete. Resetting zoom...")
                self.click_reset_zoom()
                continue

            logging.info(f"Zone '{zone_label}' has no {self.config.ticket_quantity} adjacent seats. Resetting zoom...")
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
            HumanInteractor.move_and_click(self.page, remove_btns.first)
            time.sleep(0.6)

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

    def attempt_seat_lock(self) -> bool:
        if self.is_in_basket():
            return True

        if self.navigate_from_artist_to_event_if_needed():
            return False

        self.ensure_seat_map_tab_active()

        details = self.get_selected_ticket_details()
        if details["count"] > 0:
            matched_excluded = [kw for kw in self.config.exclude_keywords if kw in details["text"]]
            if matched_excluded:
                logging.warning(
                    f"Existing selection contains excluded label(s) {matched_excluded}. Clearing selection..."
                )
                self.clear_selected_tickets()
                details = self.get_selected_ticket_details()

        if details["count"] < self.config.ticket_quantity:
            if details["count"] > 0:
                logging.info(f"Only {details['count']} ticket(s) currently selected. Clearing partial selection...")
                self.clear_selected_tickets()
            picked = self.select_two_seats()
            if picked < self.config.ticket_quantity:
                logging.info(f"Only found {picked} adjacent seats. Waiting for next scan...")
                return False

        verified = self.get_selected_ticket_details()
        matched_excluded = [kw for kw in self.config.exclude_keywords if kw in verified["text"]]
        if matched_excluded:
            logging.warning(
                f"Selected tickets contain excluded label(s) {matched_excluded} (e.g. K-18 / restricted). "
                "Clearing selection and skipping 'Get Tickets'..."
            )
            self.clear_selected_tickets()
            self.click_reset_zoom()
            return False

        if verified["count"] != self.config.ticket_quantity:
            logging.warning(
                f"'Your selection' shows x{verified['count']} instead of x{self.config.ticket_quantity}. "
                "Clearing selection and not clicking 'Get Tickets'."
            )
            if verified["count"] > 0:
                self.clear_selected_tickets()
            return False

        logging.info(f"Verified selection count: x{verified['count']} (all-ages / non-excluded). Proceeding to 'Get Tickets'...")
        if self.click_get_tickets():
            if self.is_in_basket():
                return True

        return False

    def run_loop(self) -> None:
        attempt = 1
        logging.info("Starting production polling loop...")
        while True:
            logging.info(f"Poll attempt {attempt} on {self.page.url}")
            locked = self.attempt_seat_lock()

            if locked:
                BasketAlert.trigger_alarm()
                break

            sleep_duration = random.uniform(
                self.config.min_jitter_seconds,
                self.config.max_jitter_seconds
            )
            logging.info(f"No tickets yet. Waiting {sleep_duration:.2f}s")
            time.sleep(sleep_duration)
            attempt += 1


if __name__ == "__main__":
    config = SniperConfig()
    connector = BrowserConnector(config)
    try:
        active_page = connector.connect_and_get_page()
        poller = TicketPoller(active_page, config)
        poller.run_loop()
    except KeyboardInterrupt:
        logging.info("Polling stopped by user.")
    finally:
        connector.close()