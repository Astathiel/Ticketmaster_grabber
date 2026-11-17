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
    def move_and_click(page: Page, locator: Locator) -> bool:
        try:
            locator.wait_for(state="visible", timeout=2500)
            box = locator.bounding_box()
            if box:
                target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                page.mouse.move(target_x, target_y, steps=random.randint(8, 18))
                time.sleep(random.uniform(0.06, 0.16))
                page.mouse.click(target_x, target_y)
                return True
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
        seat_map_tab = self.page.locator(
            "button:has-text('View seat map'), "
            "button:has-text('Näytä pohjakartta'), "
            "[role='tab']:has-text('seat map')"
        ).first

        if seat_map_tab.is_visible():
            is_selected = seat_map_tab.get_attribute("aria-selected")
            if is_selected == "false":
                logging.info("Switching to 'View seat map' tab...")
                HumanInteractor.move_and_click(self.page, seat_map_tab)
                time.sleep(1.5)

    def click_available_section_if_zoomed_out(self) -> bool:
        available_sections = self.page.locator(
            "svg g[data-bdd*='section'][data-available='true'], "
            "svg path[data-bdd*='section']:not([fill='#E0E0E0']):not([fill='none']), "
            "svg [aria-label*='section' i][aria-disabled='false'], "
            "svg [aria-label*='katsomo' i]:not([aria-disabled='true'])"
        )
        if available_sections.count() > 0:
            first_sec = available_sections.first
            if first_sec.is_visible():
                logging.info("Map zoomed out. Clicking into first available seating section...")
                HumanInteractor.move_and_click(self.page, first_sec)
                time.sleep(1.8)
                return True
        return False

    def find_available_seat_elements(self) -> Locator:
        return self.page.locator(
            "svg circle[data-bdd*='seat'][data-available='true'], "
            "svg circle:not([fill*='rgb(224']):not([fill='#e0e0e0']):not([fill='transparent']), "
            "svg [role='button'][aria-label*='Seat' i]:not([aria-disabled='true']), "
            "svg [role='button'][aria-label*='Paikka' i]:not([aria-disabled='true']), "
            "svg g[data-bdd='seat-node'][data-status='available']"
        )

    def select_two_seats(self) -> int:
        available_seats = self.find_available_seat_elements()
        seat_count = available_seats.count()
        logging.info(f"Detected {seat_count} potentially available seat elements in current view.")

        if seat_count < self.config.ticket_quantity:
            self.click_available_section_if_zoomed_out()
            available_seats = self.find_available_seat_elements()
            seat_count = available_seats.count()

        selected = 0
        for i in range(seat_count):
            if selected >= self.config.ticket_quantity:
                break

            seat = available_seats.nth(i)
            if not seat.is_visible():
                continue

            aria_text = (seat.get_attribute("aria-label") or "").lower()
            if any(bad in aria_text for bad in self.config.exclude_keywords):
                continue

            logging.info(f"Clicking seat {selected + 1}/{self.config.ticket_quantity}...")
            if HumanInteractor.move_and_click(self.page, seat):
                selected += 1
                time.sleep(random.uniform(0.35, 0.65))

        return selected

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