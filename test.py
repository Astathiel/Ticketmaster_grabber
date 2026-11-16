import time
import random
import logging
import winsound
from dataclasses import dataclass
from typing import Optional, Tuple, List
from playwright.sync_api import sync_playwright, Browser, Page, Locator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)

@dataclass
class TestSniperConfig:
    cdp_endpoint: str = "http://127.0.0.1:9222"
    target_url_keyword: str = "ticketmaster.fi"
    ticket_quantity: int = 2
    min_jitter_seconds: float = 3.0
    max_jitter_seconds: float = 5.5
    exclude_keywords: Tuple[str, ...] = (
        "vip", "platinum", "hot ticket", "paketti",
        "k18", "k-18", "kenttä", "permanto", "seisomapaikka",
        "golden circle", "pyörätuoli", "saattaja", "esteetön"
    )
    preferred_keywords: Tuple[str, ...] = ("peruslippu", "istumapaikat", "katsomo", "paikka", "normaali")


class BrowserConnector:
    def __init__(self, config: TestSniperConfig):
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
            locator.scroll_into_view_if_needed(timeout=1500)
            box = locator.bounding_box()
            if box:
                target_x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
                target_y = box["y"] + box["height"] * random.uniform(0.25, 0.75)
                page.mouse.move(target_x, target_y, steps=random.randint(10, 20))
                time.sleep(random.uniform(0.08, 0.20))
                page.mouse.click(target_x, target_y)
                return True
            locator.click()
            return True
        except Exception:
            return False


class BasketAlert:
    @staticmethod
    def trigger_alarm() -> None:
        logging.info("TEST SUCCESS: 2 tickets locked in basket! Check your Chrome window.")
        for _ in range(4):
            winsound.Beep(1200, 300)
            winsound.Beep(1600, 300)
            time.sleep(0.1)


class TicketPoller:
    def __init__(self, page: Page, config: TestSniperConfig):
        self.page = page
        self.config = config
        self.stepper_set_to_target: bool = False

    def inspect_visible_buttons(self) -> List[str]:
        buttons = self.page.locator("button:visible, a[role='button']:visible")
        visible_labels: List[str] = []
        count = min(buttons.count(), 25)
        for i in range(count):
            try:
                txt = buttons.nth(i).inner_text().strip().replace("\n", " ")
                aria = buttons.nth(i).get_attribute("aria-label") or ""
                label = txt if txt else f"[aria:{aria}]"
                if label and label != "[aria:]":
                    visible_labels.append(label)
            except Exception:
                pass
        return visible_labels

    def is_in_basket(self) -> bool:
        current_url = self.page.url.lower()
        if any(token in current_url for token in ("checkout", "ostoskori", "cart", "reserve")):
            return True

        checkout_button = self.page.locator(
            "button:has-text('Siirry maksamaan'), "
            "button:has-text('Siirry kassalle'), "
            "a:has-text('Siirry kassalle'), "
            "button:has-text('Tilaa nyt'), "
            "button:has-text('Osta liput'), "
            "button:has-text('Varaa'), "
            "[data-bdd='offer-card-buy-button'], "
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
            logging.info("On artist/listing page. Clicking through to the Event page...")
            HumanInteractor.move_and_click(self.page, event_links.first)
            time.sleep(random.uniform(2.0, 3.2))
            return True
        return False

    def dismiss_search_mode_gate(self) -> bool:
        best_available_btn = self.page.locator(
            "button:has-text('Näytä parhaat saatavilla'), "
            "a:has-text('Näytä parhaat saatavilla'), "
            "[role='button']:has-text('Näytä parhaat saatavilla'), "
            "button:has-text('Show Best Available')"
        ).first

        if best_available_btn.is_visible():
            logging.info("Detected search mode gate! Clicking 'Näytä parhaat saatavilla'...")
            if HumanInteractor.move_and_click(self.page, best_available_btn):
                self.stepper_set_to_target = False
                time.sleep(random.uniform(1.5, 2.4))
                return True
        return False

    def increment_ticket_stepper(self) -> bool:
        plus_buttons = self.page.locator(
            "button[aria-label*='Lisää' i], "
            "button[aria-label*='Increase' i], "
            "button[aria-label*='plus' i], "
            "button[data-bdd*='increment' i], "
            "button[data-testid*='increment' i], "
            "button:has(svg path[d*='M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z']), "
            "button:has(svg)"
        )

        total_btns = plus_buttons.count()
        for i in range(total_btns):
            btn = plus_buttons.nth(i)
            if not btn.is_visible() or not btn.is_enabled():
                continue

            row_container = btn.locator(
                "xpath=ancestor::*[contains(., '€') or contains(., 'Peruslippu') or self::li][1]"
            )
            row_text = ""
            try:
                if row_container.count() > 0:
                    row_text = row_container.inner_text().lower()
                else:
                    row_text = btn.locator("xpath=ancestor::div[3]").inner_text().lower()
            except Exception:
                continue

            if not row_text or "€" not in row_text:
                continue

            if any(bad in row_text for bad in self.config.exclude_keywords):
                continue

            btn_aria = (btn.get_attribute("aria-label") or "").lower()
            if any(minus_word in btn_aria for minus_word in ("vähennä", "poista", "decrease", "minus", "less")):
                continue

            parent_box = btn.locator("xpath=..").bounding_box()
            btn_box = btn.bounding_box()
            if parent_box and btn_box:
                if btn_box["x"] < parent_box["x"] + (parent_box["width"] * 0.4):
                    continue

            logging.info(f"Found valid ticket stepper row: {row_text.splitlines()[0] if row_text else 'Ticket Row'}")
            for click_num in range(self.config.ticket_quantity):
                logging.info(f"Clicking '+' stepper ({click_num + 1}/{self.config.ticket_quantity})...")
                HumanInteractor.move_and_click(self.page, btn)
                time.sleep(random.uniform(0.25, 0.45))

            self.stepper_set_to_target = True
            return True

        return False

    def attempt_seat_lock(self) -> bool:
        snapshot = self.inspect_visible_buttons()
        logging.info(f"Visible interactive buttons on page ({len(snapshot)}): {snapshot[:10]}")

        if self.is_in_basket():
            return True

        if self.navigate_from_artist_to_event_if_needed():
            return False

        self.dismiss_search_mode_gate()

        if not self.stepper_set_to_target:
            self.increment_ticket_stepper()

        search_buttons = self.page.locator(
            "button:has-text('Etsi lippuja'), "
            "button:has-text('Etsi'), "
            "button:has-text('Hae liput'), "
            "button:has-text('Näytä liput'), "
            "button:has-text('Lisää ostoskoriin'), "
            "button:has-text('Valitse paikat'), "
            "button:has-text('Varaa liput'), "
            "button:has-text('Jatka'), "
            "button:has-text('Seuraava'), "
            "button:has-text('Find Tickets'), "
            "button:has-text('Get Tickets'), "
            "button[data-bdd='offer-card-buy-button'], "
            "button[data-bdd='quick-picks-buy-button'], "
            "button[data-testid='findTicketsBtn']"
        )

        count = search_buttons.count()
        for i in range(count):
            btn = search_buttons.nth(i)
            if not btn.is_visible() or not btn.is_enabled():
                continue

            parent_text = ""
            try:
                parent_text = btn.locator("xpath=ancestor::*[self::li or self::div][1]").inner_text().lower()
            except Exception:
                pass

            if any(bad_word in parent_text for bad_word in self.config.exclude_keywords):
                logging.info(f"Skipping excluded ticket tier: {parent_text[:40]}...")
                continue

            btn_label = ""
            try:
                btn_label = btn.inner_text().strip()
            except Exception:
                pass

            logging.info(f"Clicking action button '{btn_label or 'Submit'}' (#{i + 1})...")
            if HumanInteractor.move_and_click(self.page, btn):
                time.sleep(random.uniform(2.2, 3.4))
                if self.is_in_basket():
                    return True
                self.stepper_set_to_target = False

        retry_toggle = self.page.locator(
            "button:has-text('Hae uudelleen'), "
            "button:has-text('Muuta hakua'), "
            "button:has-text('Search Again'), "
            "[data-bdd='qp-filter-button']"
        ).first

        if retry_toggle.is_visible():
            HumanInteractor.move_and_click(self.page, retry_toggle)
            self.stepper_set_to_target = False

        return False

    def run_loop(self) -> None:
        attempt = 1
        logging.info("Starting TEST polling loop...")
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
            logging.info(f"No lock yet. Waiting {sleep_duration:.2f}s")
            time.sleep(sleep_duration)
            attempt += 1


if __name__ == "__main__":
    config = TestSniperConfig()
    connector = BrowserConnector(config)
    try:
        active_page = connector.connect_and_get_page()
        poller = TicketPoller(active_page, config)
        poller.run_loop()
    except KeyboardInterrupt:
        logging.info("Test polling stopped by user.")
    finally:
        connector.close()