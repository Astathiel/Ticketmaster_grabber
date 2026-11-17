# Ticketmaster Seat Reservation Sniper

A Python-based automation utility designed to assist with securing ticket reservations on Ticketmaster Finland (`ticketmaster.fi`).

Rather than running an isolated headless browser that triggers anti-bot telemetry (e.g., DataDome, Akamai), this tool attaches directly to an existing, authenticated Google Chrome instance via the Chrome DevTools Protocol (CDP). It automates in-page interactions, handles seat selection workflows, applies randomized human-like cursor jitter, and sounds an audio alarm once seats are secured in the checkout basket.

---

## Architecture Overview

The project is structured into modular components:

1. **Session Attachment (`BrowserConnector`):** Binds to an active Chrome browser listening on `127.0.0.1:9222` and locates the relevant target tab without invalidating active sessions or virtual waiting room cookies (Queue-it).
2. **Behavioral Emulation (`HumanInteractor`):** Calculates randomized bounding-box coordinates and performs multi-step interpolated mouse movements prior to dispatching click events.
3. **Selection Strategies:**
   - **List / Quick Picks Engine (`index.py` / `test.py`):** Bypasses floor maps, dismisses search mode gates, adjusts custom quantity stepper widgets (`+` / `-`), filters out unwanted ticket tiers (e.g., wheelchair, VIP, standing pitch), and triggers automatic reservation searches.
   - **Interactive Map Engine (`map_sniper.py`):** Operates directly against SVG stadium layouts, identifies available seat nodes, clicks adjacent seats, and dispatches the checkout handoff.
4. **Alert Notification (`BasketAlert`):** Monitors URL and DOM state changes for checkout transitions and triggers an alternating multi-tone Windows system alert (`winsound`).

---

## Directory Structure

```text
Ticketmaster/
├── .venv/                   # Python virtual environment
├── index.py                 # Primary production sniper (List / Stepper flow)
├── test.py                  # Sandbox testing script with button diagnostics
├── map_sniper.py            # SVG canvas seat map sniper
└── README.md                # Project documentation and execution guide
```

---

## Prerequisites

- **Operating System:** Windows 10 / 11
- **Runtime:** Python 3.10+
- **Browser:** Google Chrome
- **Dependencies:** `playwright`

---

## Installation

1. Open PowerShell or Command Prompt in the project root directory:

   ```powershell
   cd "Where you downloaded the folder"/Ticketmaster
   ```

2. Initialize and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   _(If using Command Prompt, execute `.\.venv\Scripts\activate.bat`)_

3. Upgrade package installer and install dependencies:
   ```powershell
   python -m pip install --upgrade pip
   python -m pip install playwright
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

   _Note: Replace the path to `chrome.exe` with your actual Chrome installation directory if it differs from the default and the URL with the event you want to snipe._
   _(Or run the same string without the leading `&` in Command Prompt / Windows Run `Win + R`)_

3. Verify the CDP listener is active:

   ```powershell
   curl http://127.0.0.1:9222/json/version
   ```

   The endpoint should return a JSON response containing browser metadata and a valid `webSocketDebuggerUrl`.

4. **Authenticate:** In the newly opened Chrome window, log in to your Ticketmaster account. This ensures you do not encounter login or two-factor authentication prompts when the reservation is locked.

---

### Phase 2: Workflow Selection

#### Mode A: List & Stepper Search (`index.py`)

Used when events provide the **"Näytä parhaat saatavilla"** (Show best available) search flow or tiered category lists.

1. Navigate to the event landing page.
2. If placed in a Queue-it waiting room, wait manually until the queue passes and the event page loads.
3. Start the script:
   ```powershell
   python index.py
   ```
4. The script will:
   - Dismiss the search mode prompt if present.
   - Locate the standard seated row (e.g., `Peruslippu`) while ignoring excluded keywords (`VIP`, `K-18`, `Pyörätuoli`).
   - Increment the ticket quantity stepper to 2.
   - Click the reservation search button and poll with randomized intervals (3.6s–7.2s) until tickets land in the basket.

#### Mode B: Interactive SVG Map Selection (`map_sniper.py`)

Used when an event relies exclusively on picking specific seats from the venue diagram (`View seat map`).

1. Navigate to the event seat map so that section blocks or individual seat dots are visible.
2. Start the map sniper:
   ```powershell
   python map_sniper.py
   ```
3. The script will:
   - Verify the seat map tab is active.
   - Zoom into an available section if the overview map is shown.
   - Detect available seat circle elements (`svg circle[data-available='true']`).
   - Click 2 available seats in succession.
   - Click the activated **"Get Tickets"** button.

---

### Phase 3: Checkout Handoff

The moment either script confirms a successful reservation or detects the checkout URL path:

1. The execution loop halts immediately to prevent overwriting the active cart.
2. A high-priority audible alert sounds through the system audio.
3. Switch to the open Chrome window and complete payment details within the active reservation countdown timer.

---

## Configuration Reference

Both scripts use a `@dataclass` configuration block at the top of the file for customization:

| Parameter            | Type              | Default                   | Description                                                                        |
| :------------------- | :---------------- | :------------------------ | :--------------------------------------------------------------------------------- |
| `cdp_endpoint`       | `str`             | `"http://127.0.0.1:9222"` | Local CDP connection URL.                                                          |
| `target_url_keyword` | `str`             | `"ticketmaster.fi"`       | Substring used to identify the target browser tab.                                 |
| `ticket_quantity`    | `int`             | `2`                       | Number of tickets to select or increment.                                          |
| `min_jitter_seconds` | `float`           | `3.6`                     | Minimum delay between polling cycles.                                              |
| `max_jitter_seconds` | `float`           | `7.2`                     | Maximum delay between polling cycles.                                              |
| `exclude_keywords`   | `Tuple[str, ...]` | `("vip", "k18", ...)`     | Text filters used to skip premium tiers, restricted sections, or wheelchair slots. |
| `preferred_keywords` | `Tuple[str, ...]` | `("peruslippu", ...)`     | Keywords prioritized when evaluating ticket tiers.                                 |

---

## Debugging and Development

- **Logic Debugging:** When inspecting DOM selectors or element counts, place breakpoints inside `attempt_seat_lock()` (`index.py`) or `attempt_map_lock()` (`map_sniper.py`) and attach your IDE debugger.
- **Live Thread Monitoring:** Operational events, poll attempts, jitter intervals, and button snapshots are streamed via standard Python `logging`.
