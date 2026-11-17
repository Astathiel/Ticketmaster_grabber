# Ticketmaster Seat Reservation Sniper

A Python-based automation utility designed to assist with securing ticket reservations on Ticketmaster Finland (`ticketmaster.fi`).

Rather than running an isolated headless browser that triggers anti-bot telemetry (e.g., DataDome, Akamai), this tool attaches directly to an existing, authenticated Google Chrome instance via the Chrome DevTools Protocol (CDP). It automates interactive SVG seat map navigation, finds adjacent available seats while filtering out unwanted ticket tiers, applies randomized human-like cursor movement, and sounds an audio alarm once seats are secured in the checkout basket.

---

## Architecture Overview

`index.py` is structured into modular components:

1. **Session Attachment (`BrowserConnector`):** Binds to an active Chrome browser listening on `127.0.0.1:9222` and locates the target `ticketmaster.fi` tab without invalidating active sessions or virtual waiting room cookies (Queue-it).
2. **Behavioral Emulation (`HumanInteractor`):** Calculates randomized bounding-box coordinates and performs multi-step interpolated mouse movements prior to dispatching click events.
3. **Seat Map Poller (`TicketPoller`):**
   - Navigates from artist/music pages to the event page if needed.
   - Activates the **View seat map** / **Näytä pohjakartta** tab.
   - Scans available blue venue sections and locates adjacent available seats matching `ticket_quantity` while filtering out `exclude_keywords` (e.g., VIP, K-18, standing/floor, wheelchair).
   - Verifies the selected ticket summary before clicking **Get Tickets** / **Hae liput** / **Osta liput**.
4. **Alert Notification (`BasketAlert`):** Monitors URL and DOM state changes for checkout transitions and triggers an alternating multi-tone Windows system alert (`winsound`).

> **Fast Variant (`index_fast.py`):** Provides the same seat map flow using `FastInteractor` and `FastTicketPoller`, replacing interpolated mouse movement with direct clicks for lower latency.

---

## Directory Structure

```text
Ticketmaster_grabber/
├── .venv/                   # Python virtual environment (gitignored)
├── index.py                 # Primary sniper (human-like mouse movement + SVG map flow)
├── index_fast.py            # Low-latency variant (direct clicks, FastInteractor)
├── requirements.txt         # Python dependencies (Playwright)
├── LICENSE                  # MIT License
└── readme.md                # Project documentation and execution guide
```

---

## Prerequisites

- **Operating System:** Windows 10 / 11 (uses `winsound` for audio alerts)
- **Runtime:** Python 3.10+
- **Browser:** Google Chrome
- **Dependencies:** `Playwright` (listed in `requirements.txt`)

---

## Installation

1. Open PowerShell or Command Prompt in the project root directory:

   ```powershell
   cd "Where you downloaded the folder"\Ticketmaster_grabber
   ```

2. Initialize and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   _(If using Command Prompt, execute `.\.venv\Scripts\activate.bat`)_

3. Upgrade `pip` and install dependencies:

   ```powershell
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   ```

   _Note: Running `playwright install` to download standalone browser binaries is not required, as the tool attaches to your native Chrome installation via CDP._

---

## Operational Guide

### Phase 1: Launch Chrome with Remote Debugging

Before executing any script, Chrome must be launched with remote debugging enabled and bound to a dedicated user profile directory. This keeps your debugging session isolated from daily browsing while persisting authentication cookies across runs.

1. Ensure all standard Chrome instances are fully closed:

   ```powershell
   taskkill /F /IM chrome.exe
   ```

2. Launch Chrome via PowerShell:

   ```powershell
   "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\chrome_tm_profile" "https://www.ticketmaster.fi"
   ```

   _Note: Replace the path to `chrome.exe` with your actual Chrome installation directory if it differs from the default, and replace the URL with the event you want to snipe._
   _(Or run the same string in Command Prompt / Windows Run `Win + R`)_

3. Verify the CDP listener is active:

   ```powershell
   curl http://127.0.0.1:9222/json/version
   ```

   The endpoint should return a JSON response containing browser metadata and a valid `webSocketDebuggerUrl`.

4. **Authenticate:** In the newly opened Chrome window, log in to your Ticketmaster account. This ensures you do not encounter login or two-factor authentication prompts when the reservation is locked.

   > **Security Note:** While `--remote-debugging-port=9222` is active, any local process on your machine can connect to that browser profile. Close this debugging Chrome window when you are done.

---

### Phase 2: Run the Sniper

1. Navigate to the event page (or artist page) in the debugging Chrome window.
2. If placed in a Queue-it waiting room, wait manually until the queue passes and the event page loads.
3. Start the primary script (or `index_fast.py` for direct clicks):

   ```powershell
   python index.py
   ```

4. On each poll cycle, `TicketPoller.attempt_seat_lock()` will:
   - Check if tickets are already in the checkout basket (`is_in_basket()`).
   - Click through from an artist/music page to the event page if needed.
   - Ensure the **View seat map** / **Näytä pohjakartta** tab is active.
   - Scan available blue venue sections (`find_blue_zones()`) and search for adjacent available seats (`find_adjacent_seat_ids()`).
   - Skip or clear any seats/zones matching `exclude_keywords` (e.g., `vip`, `platinum`, `k18`, `kenttä`, `pyörätuoli`).
   - Verify the selected ticket count matches `ticket_quantity` and click **Get Tickets** / **Hae liput** / **Osta liput**.
   - Sleep briefly between poll attempts (`min_poll_sleep_seconds` to `max_poll_sleep_seconds`) until tickets are locked.

---

### Phase 3: Checkout Handoff

The moment `is_in_basket()` confirms a checkout URL or checkout button:

1. The polling loop halts immediately to prevent overwriting the active cart.
2. `BasketAlert.trigger_alarm()` plays an alternating multi-tone alert through Windows system audio.
3. Switch to the open Chrome window and complete payment details within the active reservation countdown timer.

---

## Configuration Reference

`index.py` uses the `SniperConfig` `@dataclass` at the top of the file for customization:

| Parameter                | Type              | Default                                                     | Description                                                                        |
| :----------------------- | :---------------- | :---------------------------------------------------------- | :--------------------------------------------------------------------------------- |
| `cdp_endpoint`           | `str`             | `"http://127.0.0.1:9222"`                                   | Local CDP connection URL.                                                          |
| `target_url_keyword`     | `str`             | `"ticketmaster.fi"`                                         | Substring used to identify the target browser tab.                                 |
| `ticket_quantity`        | `int`             | `2`                                                         | Number of adjacent seats to select.                                                |
| `min_jitter_seconds`     | `float`           | `3.6`                                                       | Configured jitter lower bound in `SniperConfig` (unused by `run_loop()`).          |
| `max_jitter_seconds`     | `float`           | `7.2`                                                       | Configured jitter upper bound in `SniperConfig` (unused by `run_loop()`).          |
| `min_poll_sleep_seconds` | `float`           | `0.001`                                                     | Minimum delay (seconds) between polling cycles in `run_loop()`.                    |
| `max_poll_sleep_seconds` | `float`           | `0.002`                                                     | Maximum delay (seconds) between polling cycles in `run_loop()`.                    |
| `exclude_keywords`       | `Tuple[str, ...]` | `("vip", "platinum", "paketti", "k18", ...)`                | Text filters used to skip VIP, 18+, standing/floor, or wheelchair/accessible slots.|
| `preferred_keywords`     | `Tuple[str, ...]` | `("peruslippu", "istumapaikat", "katsomo", "paikka", ...)`  | Preferred ticket tier keywords defined in `SniperConfig`.                          |

---

## Debugging and Development

- **Logic Debugging:** When inspecting DOM selectors, SVG section keys, or seat counts, place breakpoints inside `attempt_seat_lock()` in `index.py` (or `index_fast.py`) and attach your IDE debugger.
- **Live Monitoring:** Operational events, skipped zones/seats, poll attempts, and sleep intervals are logged via standard Python `logging` (`HH:MM:SS [LEVEL] message`).
