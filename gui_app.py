import logging
import os
import sys
import threading
import time
import urllib.parse
from typing import Optional

from sniper_engine import (
    BrowserConnector,
    ChromeProfileManager,
    SniperConfig,
    UnifiedTicketPoller,
    Win32ChromeDocker,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BASE_DIR, "gui_settings.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

try:
    from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, QTimer, QUrl, Signal
    from PySide6.QtGui import QFont, QTextCursor
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFrame,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QRadioButton,
        QScrollArea,
        QSpinBox,
        QSplitter,
        QStackedWidget,
        QVBoxLayout,
        QWidget,
    )

    HAS_PYSIDE6 = True
except ImportError:  # pragma: no cover - fallback path
    HAS_PYSIDE6 = False


DARK_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #11161f;
    color: #e6edf3;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 13px;
}
QScrollArea {
    border: none;
    background-color: #11161f;
}
QGroupBox {
    background-color: #18202c;
    border: 1px solid #283446;
    border-radius: 8px;
    margin-top: 14px;
    padding: 12px 10px 10px 10px;
    font-weight: 600;
    color: #7cc4ff;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
}
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {
    background-color: #0e131b;
    border: 1px solid #303d52;
    border-radius: 6px;
    padding: 6px 8px;
    color: #f0f6fc;
    selection-background-color: #026cdf;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 1px solid #3b9eff;
}
QLineEdit[readOnly="true"] {
    background-color: #131a24;
    color: #9bb0c9;
}
QPushButton {
    background-color: #212c3d;
    border: 1px solid #34445c;
    border-radius: 6px;
    padding: 7px 12px;
    color: #e6edf3;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #2a3950;
    border-color: #4b6285;
}
QPushButton:disabled {
    background-color: #171e29;
    color: #657388;
    border-color: #232c3b;
}
QPushButton#primaryBtn {
    background-color: #026cdf;
    border: 1px solid #2689f3;
    color: #ffffff;
}
QPushButton#primaryBtn:hover {
    background-color: #0a7bf5;
}
QPushButton#searchBtn {
    background-color: #0f8a4c;
    border: 1px solid #18ad62;
    color: #ffffff;
    font-size: 14px;
    padding: 11px 14px;
}
QPushButton#searchBtn:hover {
    background-color: #13a35a;
}
QPushButton#stopBtn {
    background-color: #c93434;
    border: 1px solid #e04f4f;
    color: #ffffff;
    font-size: 14px;
    padding: 11px 14px;
}
QPushButton#stopBtn:hover {
    background-color: #de3e3e;
}
QCheckBox, QRadioButton {
    spacing: 8px;
    padding: 2px 0;
}
QSplitter::handle {
    background-color: #253142;
}
"""


if HAS_PYSIDE6:

    class QtLogEmitter(QObject):
        message = Signal(str)

    class QtLogHandler(logging.Handler):
        """Routes Python logging records safely onto the Qt main thread."""

        def __init__(self, emitter: QtLogEmitter):
            super().__init__()
            self.emitter = emitter
            self.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
            )

        def emit(self, record: logging.LogRecord) -> None:
            try:
                msg = self.format(record)
                self.emitter.message.emit(msg)
            except Exception:
                pass

    class CdpProbeWorker(QThread):
        """Non-blocking background check for CDP availability and active tab URL."""

        probed = Signal(bool, str, str)

        def __init__(self, cdp_endpoint: str, target_keyword: str, parent=None):
            super().__init__(parent)
            self.cdp_endpoint = cdp_endpoint
            self.target_keyword = target_keyword

        def run(self) -> None:
            active, info = ChromeProfileManager.is_cdp_active(self.cdp_endpoint)
            active_url = ""
            if active:
                tabs = ChromeProfileManager.get_open_tabs(self.cdp_endpoint)
                for tab in tabs:
                    url = tab.get("url", "")
                    if self.target_keyword.lower() in url.lower():
                        active_url = url
                        break
                if not active_url and tabs:
                    active_url = tabs[0].get("url", "")
            self.probed.emit(active, info, active_url)

    class CdpActionWorker(QThread):
        """Executes one-off browser actions (find event, go to URL, back, reload) via CDP."""

        finished_action = Signal(bool, str)

        def __init__(
            self,
            config: SniperConfig,
            action: str,
            payload: str = "",
            parent=None,
        ):
            super().__init__(parent)
            self.config = config
            self.action = action
            self.payload = payload

        def run(self) -> None:
            connector = BrowserConnector(self.config)
            try:
                page = connector.connect_and_get_page(open_if_missing=True)
                poller = UnifiedTicketPoller(page, self.config)
                if self.action == "find_event":
                    moved = poller.search_and_navigate_to_event(self.payload)
                    self.finished_action.emit(
                        True,
                        "Event search navigation finished."
                        if moved
                        else "Loaded/checked page for event.",
                    )
                elif self.action == "goto":
                    url = self.payload.strip()
                    if not url.lower().startswith(("http://", "https://")):
                        url = "https://" + url
                    page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    self.finished_action.emit(True, f"Navigated to {url}")
                elif self.action == "back":
                    page.go_back(wait_until="domcontentloaded", timeout=15000)
                    self.finished_action.emit(True, "Navigated back.")
                elif self.action == "reload":
                    page.reload(wait_until="domcontentloaded", timeout=20000)
                    self.finished_action.emit(True, "Reloaded page.")
                else:
                    self.finished_action.emit(False, f"Unknown action: {self.action}")
            except Exception as exc:
                logging.error("Browser action '%s' failed: %s", self.action, exc)
                self.finished_action.emit(False, str(exc))
            finally:
                connector.close()

    class SniperWorker(QThread):
        """Runs Playwright CDP attachment + UnifiedTicketPoller loop on its own thread."""

        status_changed = Signal(str, str)
        tickets_locked = Signal()
        error_occurred = Signal(str)
        finished_polling = Signal(bool)

        def __init__(self, config: SniperConfig, parent=None):
            super().__init__(parent)
            self.config = config
            self.stop_event = threading.Event()

        def request_stop(self) -> None:
            self.stop_event.set()

        def run(self) -> None:
            connector = BrowserConnector(self.config)
            locked = False
            try:
                self.status_changed.emit("CONNECTING", "Attaching to browser via CDP...")
                page = connector.connect_and_get_page(open_if_missing=True)
                poller = UnifiedTicketPoller(page, self.config)

                if self.config.search_query.strip():
                    current = page.url.lower()
                    if (
                        "checkout" not in current
                        and not poller.is_in_basket()
                        and not poller.is_on_matching_event_page(self.config.search_query)
                    ):
                        self.status_changed.emit(
                            "SEARCHING",
                            f"Searching event: {self.config.search_query.strip()}",
                        )
                        poller.search_and_navigate_to_event(self.config.search_query)

                locked = poller.run_loop(
                    stop_event=self.stop_event,
                    on_status=lambda state, text: self.status_changed.emit(state, text),
                    on_locked=lambda: self.tickets_locked.emit(),
                )
            except Exception as exc:
                logging.error("Sniper worker error: %s", exc)
                self.error_occurred.emit(str(exc))
            finally:
                connector.close()
                self.finished_polling.emit(locked)

    class BrowserHostWidget(QWidget):
        """Native container widget that hosts and resizes a docked Win32 Chrome HWND."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
            self.docked_hwnd: Optional[int] = None
            self.original_style: Optional[int] = None

            layout = QVBoxLayout(self)
            layout.setContentsMargins(24, 24, 24, 24)
            self.placeholder_label = QLabel(
                "Integrated Ticketmaster Browser Area\n\n"
                "Click 'Open Chrome Profile -> ticketmaster.fi' on the left to launch\n"
                "an authenticated Chrome profile and embed it directly here.\n\n"
                "You can also switch Browser Mode to 'Embedded WebView (Qt)'\n"
                "to use a built-in Qt Chromium viewport."
            )
            self.placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.placeholder_label.setStyleSheet(
                "color: #8fa3bd; font-size: 14px; background-color: #0d121a; "
                "border: 1px dashed #2d3c52; border-radius: 8px; padding: 28px;"
            )
            layout.addWidget(self.placeholder_label)

        def physical_size(self) -> tuple[int, int]:
            ratio = float(self.devicePixelRatioF() or 1.0)
            return max(1, int(self.width() * ratio)), max(1, int(self.height() * ratio))

        def set_docked_hwnd(
            self, hwnd: Optional[int], original_style: Optional[int] = None
        ) -> None:
            self.docked_hwnd = hwnd
            self.original_style = original_style
            self.placeholder_label.setVisible(hwnd is None)
            if hwnd:
                w, h = self.physical_size()
                Win32ChromeDocker.resize_docked(hwnd, w, h)

        def resizeEvent(self, event) -> None:
            super().resizeEvent(event)
            if self.docked_hwnd:
                w, h = self.physical_size()
                Win32ChromeDocker.resize_docked(self.docked_hwnd, w, h)

    class TicketmasterSniperWindow(QMainWindow):
        """Main split-screen GUI prototype for Ticketmaster search and seat sniping."""

        def __init__(self, settings_path: str = SETTINGS_PATH):
            super().__init__()
            self.settings_path = settings_path
            self.config = SniperConfig.load_from_file(settings_path)
            self.chrome_process = None
            self.sniper_worker: Optional[SniperWorker] = None
            self.action_worker: Optional[CdpActionWorker] = None
            self.probe_worker: Optional[CdpProbeWorker] = None
            self.webview = None
            self._dock_attempts_remaining = 0

            self.setWindowTitle("Ticketmaster Search & Seat Sniper — GUI Prototype")
            self.resize(1380, 880)
            self.setStyleSheet(DARK_STYLESHEET)

            self._init_logging()
            self._build_ui()
            self._apply_config_to_ui()
            self._setup_timers()

        def _init_logging(self) -> None:
            self.log_emitter = QtLogEmitter(self)
            self.log_emitter.message.connect(self._append_log)
            self.log_handler = QtLogHandler(self.log_emitter)
            logging.getLogger().addHandler(self.log_handler)

        def _build_ui(self) -> None:
            central = QWidget()
            self.setCentralWidget(central)
            root_layout = QHBoxLayout(central)
            root_layout.setContentsMargins(10, 10, 10, 10)

            main_splitter = QSplitter(Qt.Orientation.Horizontal)
            root_layout.addWidget(main_splitter)

            # Left Sidebar (Scrollable Controls)
            sidebar_scroll = QScrollArea()
            sidebar_scroll.setWidgetResizable(True)
            sidebar_scroll.setMinimumWidth(390)
            sidebar_scroll.setMaximumWidth(480)

            sidebar_widget = QWidget()
            sidebar_layout = QVBoxLayout(sidebar_widget)
            sidebar_layout.setContentsMargins(6, 6, 10, 6)
            sidebar_layout.setSpacing(10)
            sidebar_scroll.setWidget(sidebar_widget)

            header_label = QLabel("Ticketmaster Seat Sniper")
            header_label.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
            header_label.setStyleSheet("color: #ffffff; padding: 2px 0;")
            sidebar_layout.addWidget(header_label)

            # 1. Chrome Profile & Browser Group
            profile_group = QGroupBox("1. Chrome Profile & Browser")
            profile_layout = QVBoxLayout(profile_group)
            profile_layout.setSpacing(8)

            profile_row = QHBoxLayout()
            self.profile_edit = QLineEdit()
            self.profile_edit.setPlaceholderText(r"C:\chrome_tm_profile")
            self.profile_edit.setToolTip(
                "Chrome --user-data-dir path. Edit path or click 'Open Chrome Profile' below."
            )
            self.browse_profile_btn = QPushButton("Browse...")
            self.browse_profile_btn.clicked.connect(self._on_browse_profile)
            profile_row.addWidget(self.profile_edit, 1)
            profile_row.addWidget(self.browse_profile_btn)
            profile_layout.addWidget(QLabel("Chrome Profile Folder:"))
            profile_layout.addLayout(profile_row)

            mode_row = QHBoxLayout()
            mode_row.addWidget(QLabel("Browser Mode:"))
            self.browser_mode_combo = QComboBox()
            self.browser_mode_combo.addItem("Docked Chrome (Inside App)", "docked")
            self.browser_mode_combo.addItem("Embedded WebView (Qt)", "webview")
            self.browser_mode_combo.addItem("External Chrome Window", "external")
            self.browser_mode_combo.currentIndexChanged.connect(
                self._on_browser_mode_changed
            )
            mode_row.addWidget(self.browser_mode_combo, 1)
            profile_layout.addLayout(mode_row)

            self.open_chrome_btn = QPushButton("Open Chrome Profile -> ticketmaster.fi")
            self.open_chrome_btn.setObjectName("primaryBtn")
            self.open_chrome_btn.clicked.connect(self._on_open_chrome_clicked)
            profile_layout.addWidget(self.open_chrome_btn)

            dock_row = QHBoxLayout()
            self.dock_btn = QPushButton("Dock / Re-attach")
            self.dock_btn.clicked.connect(self._on_dock_clicked)
            self.undock_btn = QPushButton("Pop Out")
            self.undock_btn.clicked.connect(self._on_undock_clicked)
            dock_row.addWidget(self.dock_btn)
            dock_row.addWidget(self.undock_btn)
            profile_layout.addLayout(dock_row)

            self.cdp_status_label = QLabel("● Disconnected (CDP :9222)")
            self.cdp_status_label.setStyleSheet("color: #f47067; font-weight: 600;")
            profile_layout.addWidget(self.cdp_status_label)
            sidebar_layout.addWidget(profile_group)

            # 2. Band / Concert Search Group
            search_group = QGroupBox("2. Band / Concert Search")
            search_layout = QVBoxLayout(search_group)
            search_layout.setSpacing(8)

            self.band_edit = QLineEdit()
            self.band_edit.setPlaceholderText(
                "Band, artist, concert name, or ticketmaster.fi event URL..."
            )
            self.band_edit.returnPressed.connect(self._on_find_event_clicked)
            search_layout.addWidget(self.band_edit)

            self.find_event_btn = QPushButton("Navigate / Find Event on Ticketmaster")
            self.find_event_btn.clicked.connect(self._on_find_event_clicked)
            search_layout.addWidget(self.find_event_btn)
            sidebar_layout.addWidget(search_group)

            # 3. Ticket Configuration Group
            config_group = QGroupBox("3. Ticket & Filter Config")
            config_layout = QVBoxLayout(config_group)
            config_layout.setSpacing(7)

            qty_row = QHBoxLayout()
            qty_row.addWidget(QLabel("How many tickets:"))
            self.qty_spin = QSpinBox()
            self.qty_spin.setRange(1, 10)
            self.qty_spin.valueChanged.connect(self._on_config_ui_changed)
            qty_row.addStretch()
            qty_row.addWidget(self.qty_spin)
            config_layout.addLayout(qty_row)

            self.adjacent_chk = QCheckBox("Next to each other (adjacent seats)")
            self.adjacent_chk.stateChanged.connect(self._on_config_ui_changed)
            config_layout.addWidget(self.adjacent_chk)

            self.under18_chk = QCheckBox("Under 18 / All-Ages only (exclude K-18)")
            self.under18_chk.stateChanged.connect(self._on_config_ui_changed)
            config_layout.addWidget(self.under18_chk)

            self.exclude_vip_chk = QCheckBox("Exclude VIP / Platinum packages")
            self.exclude_vip_chk.stateChanged.connect(self._on_config_ui_changed)
            config_layout.addWidget(self.exclude_vip_chk)

            self.exclude_standing_chk = QCheckBox(
                "Seated only (exclude floor / kenttä / standing)"
            )
            self.exclude_standing_chk.stateChanged.connect(self._on_config_ui_changed)
            config_layout.addWidget(self.exclude_standing_chk)

            self.exclude_accessible_chk = QCheckBox(
                "Exclude wheelchair / accessible only"
            )
            self.exclude_accessible_chk.stateChanged.connect(self._on_config_ui_changed)
            config_layout.addWidget(self.exclude_accessible_chk)

            config_layout.addWidget(QLabel("Preferred Keywords (comma-separated):"))
            self.preferred_edit = QLineEdit()
            self.preferred_edit.setPlaceholderText("peruslippu, istumapaikat, katsomo")
            self.preferred_edit.textChanged.connect(self._on_config_ui_changed)
            config_layout.addWidget(self.preferred_edit)

            config_layout.addWidget(QLabel("Custom Restricted Keywords (comma-separated):"))
            self.restricted_edit = QLineEdit()
            self.restricted_edit.setPlaceholderText("e.g. näkörajoite, parveke, rajoitettu")
            self.restricted_edit.textChanged.connect(self._on_config_ui_changed)
            config_layout.addWidget(self.restricted_edit)

            config_layout.addWidget(QLabel("Active Excluded Keywords:"))
            self.compiled_exclude_preview = QLineEdit()
            self.compiled_exclude_preview.setReadOnly(True)
            config_layout.addWidget(self.compiled_exclude_preview)

            speed_row = QHBoxLayout()
            speed_row.addWidget(QLabel("Interaction:"))
            self.human_radio = QRadioButton("Human Mouse")
            self.fast_radio = QRadioButton("Fast Direct")
            self.human_radio.toggled.connect(self._on_config_ui_changed)
            speed_row.addWidget(self.human_radio)
            speed_row.addWidget(self.fast_radio)
            speed_row.addStretch()
            config_layout.addLayout(speed_row)

            sidebar_layout.addWidget(config_group)

            # 4. Action / Execution Group
            action_group = QGroupBox("4. Sniper Execution")
            action_layout = QVBoxLayout(action_group)
            action_layout.setSpacing(8)

            self.search_tickets_btn = QPushButton("Search for Ticket")
            self.search_tickets_btn.setObjectName("searchBtn")
            self.search_tickets_btn.clicked.connect(self._on_toggle_search)
            action_layout.addWidget(self.search_tickets_btn)

            self.status_banner = QLabel("IDLE — Ready to attach & search")
            self.status_banner.setWordWrap(True)
            self.status_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.status_banner.setStyleSheet(
                "background-color: #101722; border: 1px solid #2b384b; "
                "border-radius: 6px; padding: 8px; font-weight: 600; color: #9ecbff;"
            )
            action_layout.addWidget(self.status_banner)
            sidebar_layout.addWidget(action_group)
            sidebar_layout.addStretch()

            # Right Pane: Browser + Live Log Console
            right_splitter = QSplitter(Qt.Orientation.Vertical)

            browser_panel = QWidget()
            browser_layout = QVBoxLayout(browser_panel)
            browser_layout.setContentsMargins(0, 0, 0, 0)
            browser_layout.setSpacing(6)

            nav_bar = QHBoxLayout()
            self.back_btn = QPushButton("◀")
            self.back_btn.setFixedWidth(34)
            self.back_btn.clicked.connect(self._on_nav_back)
            self.reload_btn = QPushButton("↻")
            self.reload_btn.setFixedWidth(34)
            self.reload_btn.clicked.connect(self._on_nav_reload)
            self.url_bar = QLineEdit("https://www.ticketmaster.fi")
            self.url_bar.returnPressed.connect(self._on_nav_go)
            self.go_btn = QPushButton("Go")
            self.go_btn.clicked.connect(self._on_nav_go)
            self.home_btn = QPushButton("ticketmaster.fi")
            self.home_btn.clicked.connect(self._on_nav_home)

            nav_bar.addWidget(self.back_btn)
            nav_bar.addWidget(self.reload_btn)
            nav_bar.addWidget(self.url_bar, 1)
            nav_bar.addWidget(self.go_btn)
            nav_bar.addWidget(self.home_btn)
            browser_layout.addLayout(nav_bar)

            self.browser_stack = QStackedWidget()
            self.browser_host = BrowserHostWidget()
            self.browser_stack.addWidget(self.browser_host)  # index 0: docked/external

            self.webview_placeholder = QWidget()
            wv_layout = QVBoxLayout(self.webview_placeholder)
            wv_layout.setContentsMargins(0, 0, 0, 0)
            self.browser_stack.addWidget(self.webview_placeholder)  # index 1: Qt WebView

            browser_layout.addWidget(self.browser_stack, 1)
            right_splitter.addWidget(browser_panel)

            # Bottom Log Panel
            log_panel = QFrame()
            log_panel.setStyleSheet(
                "QFrame { background-color: #141b25; border: 1px solid #263244; border-radius: 6px; }"
            )
            log_layout = QVBoxLayout(log_panel)
            log_layout.setContentsMargins(8, 6, 8, 8)
            log_layout.setSpacing(4)

            log_header = QHBoxLayout()
            log_title = QLabel("Live Sniper Log")
            log_title.setStyleSheet("font-weight: 600; color: #7cc4ff; border: none;")
            self.clear_log_btn = QPushButton("Clear Log")
            self.clear_log_btn.clicked.connect(self._on_clear_log)
            log_header.addWidget(log_title)
            log_header.addStretch()
            log_header.addWidget(self.clear_log_btn)
            log_layout.addLayout(log_header)

            self.log_output = QPlainTextEdit()
            self.log_output.setReadOnly(True)
            self.log_output.setMaximumBlockCount(1000)
            self.log_output.setFont(QFont("Consolas", 10))
            self.log_output.setStyleSheet(
                "background-color: #0b0f16; border: 1px solid #222d3d; color: #d5e0ee;"
            )
            log_layout.addWidget(self.log_output, 1)
            right_splitter.addWidget(log_panel)

            right_splitter.setStretchFactor(0, 4)
            right_splitter.setStretchFactor(1, 1)
            right_splitter.setSizes([640, 200])

            main_splitter.addWidget(sidebar_scroll)
            main_splitter.addWidget(right_splitter)
            main_splitter.setStretchFactor(0, 0)
            main_splitter.setStretchFactor(1, 1)
            main_splitter.setSizes([420, 940])

        def _setup_timers(self) -> None:
            self.cdp_timer = QTimer(self)
            self.cdp_timer.setInterval(2000)
            self.cdp_timer.timeout.connect(self._trigger_cdp_probe)
            self.cdp_timer.start()

            self.dock_retry_timer = QTimer(self)
            self.dock_retry_timer.setInterval(500)
            self.dock_retry_timer.timeout.connect(self._retry_dock_chrome)

        def _apply_config_to_ui(self) -> None:
            self.profile_edit.setText(self.config.profile_dir)
            idx = self.browser_mode_combo.findData(self.config.browser_mode)
            if idx >= 0:
                self.browser_mode_combo.setCurrentIndex(idx)
            self.band_edit.setText(self.config.search_query)
            self.qty_spin.setValue(self.config.ticket_quantity)
            self.adjacent_chk.setChecked(self.config.adjacent_only)
            self.under18_chk.setChecked(self.config.under_18_only)
            self.exclude_vip_chk.setChecked(self.config.exclude_vip)
            self.exclude_standing_chk.setChecked(self.config.exclude_standing)
            self.exclude_accessible_chk.setChecked(self.config.exclude_accessible)
            self.preferred_edit.setText(self.config.custom_preferred_keywords)
            self.restricted_edit.setText(self.config.custom_exclude_keywords)
            if self.config.fast_mode:
                self.fast_radio.setChecked(True)
            else:
                self.human_radio.setChecked(True)
            self._update_compiled_preview()
            self._sync_browser_mode_view()

        def _collect_config_from_ui(self) -> SniperConfig:
            self.config.profile_dir = self.profile_edit.text().strip() or r"C:\chrome_tm_profile"
            self.config.browser_mode = str(self.browser_mode_combo.currentData() or "docked")
            self.config.search_query = self.band_edit.text().strip()
            self.config.ticket_quantity = int(self.qty_spin.value())
            self.config.adjacent_only = self.adjacent_chk.isChecked()
            self.config.under_18_only = self.under18_chk.isChecked()
            self.config.exclude_vip = self.exclude_vip_chk.isChecked()
            self.config.exclude_standing = self.exclude_standing_chk.isChecked()
            self.config.exclude_accessible = self.exclude_accessible_chk.isChecked()
            self.config.custom_preferred_keywords = self.preferred_edit.text().strip()
            self.config.custom_exclude_keywords = self.restricted_edit.text().strip()
            self.config.fast_mode = self.fast_radio.isChecked()
            return self.config

        def _update_compiled_preview(self) -> None:
            cfg = self._collect_config_from_ui()
            excluded = ", ".join(cfg.get_exclude_keywords()) or "(none)"
            self.compiled_exclude_preview.setText(excluded)

        def _on_config_ui_changed(self) -> None:
            self._update_compiled_preview()
            self._save_settings()

        def _save_settings(self) -> None:
            try:
                self._collect_config_from_ui().save_to_file(self.settings_path)
            except Exception as exc:
                logging.debug("Could not save settings: %s", exc)

        def _append_log(self, text: str) -> None:
            self.log_output.appendPlainText(text)
            self.log_output.moveCursor(QTextCursor.MoveOperation.End)

        def _on_clear_log(self) -> None:
            self.log_output.clear()

        def _on_browse_profile(self) -> None:
            chosen = QFileDialog.getExistingDirectory(
                self, "Select Chrome Profile Folder", self.profile_edit.text() or "C:\\"
            )
            if chosen:
                self.profile_edit.setText(os.path.normpath(chosen))
                self._save_settings()

        def _on_browser_mode_changed(self) -> None:
            self._save_settings()
            self._sync_browser_mode_view()

        def _sync_browser_mode_view(self) -> None:
            mode = self.browser_mode_combo.currentData()
            if mode in ("webview", "external") and self.browser_host.docked_hwnd:
                self._on_undock_clicked()
            if mode == "webview":
                self._ensure_webview_initialized()
                self.browser_stack.setCurrentIndex(1)
                self.dock_btn.setEnabled(False)
                self.undock_btn.setEnabled(False)
                self.open_chrome_btn.setText("Open ticketmaster.fi in Embedded WebView")
            else:
                self.browser_stack.setCurrentIndex(0)
                self.dock_btn.setEnabled(mode == "docked")
                self.undock_btn.setEnabled(mode == "docked")
                self.open_chrome_btn.setText("Open Chrome Profile -> ticketmaster.fi")

        def _ensure_webview_initialized(self) -> None:
            if self.webview is not None:
                return
            port = ChromeProfileManager.parse_cdp_port(self.config.cdp_endpoint)
            if "QTWEBENGINE_REMOTE_DEBUGGING" not in os.environ:
                os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = str(port)
            try:
                from PySide6.QtWebEngineCore import QWebEngineProfile
                from PySide6.QtWebEngineWidgets import QWebEngineView
            except Exception as exc:
                logging.error("QtWebEngineWidgets unavailable: %s", exc)
                QMessageBox.warning(
                    self,
                    "Embedded WebView Unavailable",
                    f"Could not load QtWebEngineWidgets:\n{exc}\n\n"
                    "Switch back to 'Docked Chrome (Inside App)' instead.",
                )
                return

            profile_path = os.path.join(
                self.profile_edit.text().strip() or r"C:\chrome_tm_profile",
                "qt_webview_profile",
            )
            os.makedirs(profile_path, exist_ok=True)
            profile = QWebEngineProfile("TicketmasterProfile", self)
            profile.setPersistentStoragePath(profile_path)
            profile.setCachePath(os.path.join(profile_path, "cache"))

            self.webview = QWebEngineView(self.webview_placeholder)
            from PySide6.QtWebEngineCore import QWebEnginePage

            page = QWebEnginePage(profile, self.webview)
            self.webview.setPage(page)
            self.webview.urlChanged.connect(
                lambda qurl: self.url_bar.setText(qurl.toString())
            )
            self.webview_placeholder.layout().addWidget(self.webview)
            self.webview.setUrl(QUrl("https://www.ticketmaster.fi"))
            logging.info(
                "Initialized embedded QtWebEngineView (CDP remote debugging port %s).",
                os.environ.get("QTWEBENGINE_REMOTE_DEBUGGING"),
            )

        def _on_open_chrome_clicked(self) -> None:
            cfg = self._collect_config_from_ui()
            self._save_settings()
            if cfg.browser_mode == "webview":
                self._sync_browser_mode_view()
                if self.webview is not None:
                    self.webview.setUrl(QUrl("https://www.ticketmaster.fi"))
                return

            port = ChromeProfileManager.parse_cdp_port(cfg.cdp_endpoint)
            active, _ = ChromeProfileManager.is_cdp_active(cfg.cdp_endpoint)
            if active:
                logging.info("CDP port %d is already active.", port)
                if cfg.browser_mode == "docked":
                    self._on_dock_clicked()
                return

            try:
                self.chrome_process = ChromeProfileManager.launch_chrome_profile(
                    profile_dir=cfg.profile_dir,
                    port=port,
                    start_url="https://www.ticketmaster.fi",
                    app_mode=(cfg.browser_mode == "docked"),
                )
                self._set_status(
                    "LAUNCHING",
                    f"Launching Chrome profile ({cfg.profile_dir})...",
                )
                if cfg.browser_mode == "docked":
                    self._dock_attempts_remaining = 14
                    self.dock_retry_timer.start()
            except Exception as exc:
                logging.error("Failed to launch Chrome profile: %s", exc)
                QMessageBox.critical(self, "Chrome Launch Failed", str(exc))

        def _retry_dock_chrome(self) -> None:
            if self._dock_attempts_remaining <= 0:
                self.dock_retry_timer.stop()
                logging.warning(
                    "Timed out auto-docking Chrome window. Click 'Dock / Re-attach' once Chrome appears."
                )
                return
            self._dock_attempts_remaining -= 1
            if self._try_dock_chrome_window():
                self.dock_retry_timer.stop()

        def _try_dock_chrome_window(self) -> bool:
            pid = self.chrome_process.pid if self.chrome_process else None
            hwnd = Win32ChromeDocker.find_chrome_window(
                title_keywords=("ticketmaster", "chrome"),
                pid=pid,
            )
            if not hwnd:
                return False

            w, h = self.browser_host.physical_size()
            old_style = Win32ChromeDocker.dock_window(
                hwnd, int(self.browser_host.winId()), w, h
            )
            if old_style is None:
                return False
            self.browser_host.set_docked_hwnd(hwnd, old_style)
            logging.info("Docked Chrome window (HWND=%s) inside application.", hwnd)
            self._set_status("CONNECTED", "Chrome profile docked & ready.")
            return True

        def _on_dock_clicked(self) -> None:
            if not self._try_dock_chrome_window():
                QMessageBox.information(
                    self,
                    "Chrome Window Not Found",
                    "Could not locate an open Ticketmaster Chrome window yet.\n"
                    "Click 'Open Chrome Profile -> ticketmaster.fi' first and wait for it to load.",
                )

        def _on_undock_clicked(self) -> None:
            if self.browser_host.docked_hwnd:
                Win32ChromeDocker.undock_window(
                    self.browser_host.docked_hwnd, self.browser_host.original_style
                )
                logging.info("Popped Chrome window out to standalone window.")
                self.browser_host.set_docked_hwnd(None, None)

        def _trigger_cdp_probe(self) -> None:
            if self.probe_worker and self.probe_worker.isRunning():
                return
            self.probe_worker = CdpProbeWorker(
                self.config.cdp_endpoint, self.config.target_url_keyword, self
            )
            self.probe_worker.probed.connect(self._on_cdp_probed)
            self.probe_worker.start()

        def _on_cdp_probed(self, active: bool, info: str, active_url: str) -> None:
            port = ChromeProfileManager.parse_cdp_port(self.config.cdp_endpoint)
            if active:
                self.cdp_status_label.setText(f"● Connected ({info} :{port})")
                self.cdp_status_label.setStyleSheet("color: #3fb950; font-weight: 600;")
                if active_url and not self.url_bar.hasFocus():
                    self.url_bar.setText(active_url)
            else:
                self.cdp_status_label.setText(f"● Disconnected (CDP :{port})")
                self.cdp_status_label.setStyleSheet("color: #f47067; font-weight: 600;")

        def _run_browser_action(self, action: str, payload: str = "") -> None:
            if self.action_worker and self.action_worker.isRunning():
                logging.info("Wait for current browser action to complete...")
                return
            cfg = self._collect_config_from_ui()
            self._save_settings()
            self.action_worker = CdpActionWorker(cfg, action, payload, self)
            self.action_worker.finished_action.connect(self._on_browser_action_done)
            self.action_worker.start()

        def _on_browser_action_done(self, ok: bool, message: str) -> None:
            if ok:
                self._set_status("READY", message)
            else:
                self._set_status("ERROR", f"Browser action failed: {message}")

        def _on_find_event_clicked(self) -> None:
            query = self.band_edit.text().strip()
            if not query:
                QMessageBox.information(
                    self,
                    "Enter Band or Concert",
                    "Type a band name, concert title, or Ticketmaster event URL first.",
                )
                return

            if (
                self.browser_mode_combo.currentData() == "webview"
                and self.webview is not None
                and not ChromeProfileManager.is_cdp_active(self.config.cdp_endpoint)[0]
            ):
                if query.lower().startswith(("http://", "https://")):
                    self.webview.setUrl(QUrl(query))
                else:
                    self.webview.setUrl(QUrl("https://www.ticketmaster.fi"))
                    search_url = QUrl(
                        f"https://www.ticketmaster.fi/search?q={urllib.parse.quote_plus(query)}"
                    )
                    QTimer.singleShot(
                        400, lambda url=search_url: self.webview.setUrl(url)
                    )
                return

            self._set_status("SEARCHING", f"Finding event for '{query}'...")
            self._run_browser_action("find_event", query)

        def _on_nav_go(self) -> None:
            url = self.url_bar.text().strip()
            if not url:
                return
            if (
                self.browser_mode_combo.currentData() == "webview"
                and self.webview is not None
            ):
                if not url.lower().startswith(("http://", "https://")):
                    url = "https://" + url
                self.webview.setUrl(QUrl(url))
            else:
                self._run_browser_action("goto", url)

        def _on_nav_home(self) -> None:
            self.url_bar.setText("https://www.ticketmaster.fi")
            self._on_nav_go()

        def _on_nav_back(self) -> None:
            if (
                self.browser_mode_combo.currentData() == "webview"
                and self.webview is not None
            ):
                self.webview.back()
            else:
                self._run_browser_action("back")

        def _on_nav_reload(self) -> None:
            if (
                self.browser_mode_combo.currentData() == "webview"
                and self.webview is not None
            ):
                self.webview.reload()
            else:
                self._run_browser_action("reload")

        def _on_toggle_search(self) -> None:
            if self.sniper_worker and self.sniper_worker.isRunning():
                self.search_tickets_btn.setEnabled(False)
                self.search_tickets_btn.setText("Stopping...")
                self.sniper_worker.request_stop()
                return

            cfg = self._collect_config_from_ui()
            self._save_settings()

            active, _ = ChromeProfileManager.is_cdp_active(cfg.cdp_endpoint)
            if not active:
                QMessageBox.warning(
                    self,
                    "Browser Not Connected",
                    "CDP port 9222 is not active yet.\n\n"
                    "Click 'Open Chrome Profile -> ticketmaster.fi' (or switch to Embedded WebView) first.",
                )
                return

            self.sniper_worker = SniperWorker(cfg, self)
            self.sniper_worker.status_changed.connect(self._set_status)
            self.sniper_worker.tickets_locked.connect(self._on_tickets_locked)
            self.sniper_worker.error_occurred.connect(self._on_worker_error)
            self.sniper_worker.finished_polling.connect(self._on_worker_finished)

            self.search_tickets_btn.setObjectName("stopBtn")
            self.search_tickets_btn.setStyle(self.search_tickets_btn.style())
            self.search_tickets_btn.setText("Stop Searching")
            self.sniper_worker.start()

        def _set_status(self, state: str, text: str) -> None:
            self.status_banner.setText(text)
            if state == "LOCKED":
                self.status_banner.setStyleSheet(
                    "background-color: #0e4429; border: 2px solid #2ea043; "
                    "border-radius: 6px; padding: 8px; font-weight: 700; color: #ffffff;"
                )
            elif state == "ERROR":
                self.status_banner.setStyleSheet(
                    "background-color: #49171a; border: 1px solid #f47067; "
                    "border-radius: 6px; padding: 8px; font-weight: 600; color: #ffdcd7;"
                )
            elif state in ("SCANNING", "SEARCHING", "CONNECTING"):
                self.status_banner.setStyleSheet(
                    "background-color: #112d4e; border: 1px solid #388bfd; "
                    "border-radius: 6px; padding: 8px; font-weight: 600; color: #cce5ff;"
                )
            else:
                self.status_banner.setStyleSheet(
                    "background-color: #101722; border: 1px solid #2b384b; "
                    "border-radius: 6px; padding: 8px; font-weight: 600; color: #9ecbff;"
                )

        def _on_tickets_locked(self) -> None:
            self._set_status(
                "LOCKED",
                "TICKETS LOCKED IN BASKET! Complete checkout inside the browser!",
            )
            self.raise_()
            self.activateWindow()

        def _on_worker_error(self, message: str) -> None:
            self._set_status("ERROR", f"Error: {message}")

        def _on_worker_finished(self, locked: bool) -> None:
            self.search_tickets_btn.setEnabled(True)
            self.search_tickets_btn.setObjectName("searchBtn")
            self.search_tickets_btn.setStyle(self.search_tickets_btn.style())
            self.search_tickets_btn.setText("Search for Ticket")
            if not locked and "Error:" not in self.status_banner.text():
                self._set_status("IDLE", "IDLE — Search stopped.")

        def closeEvent(self, event) -> None:
            self.cdp_timer.stop()
            self.dock_retry_timer.stop()
            self._save_settings()
            if self.sniper_worker and self.sniper_worker.isRunning():
                self.sniper_worker.request_stop()
                self.sniper_worker.wait(2000)
            if self.action_worker and self.action_worker.isRunning():
                self.action_worker.wait(1500)
            if self.probe_worker and self.probe_worker.isRunning():
                self.probe_worker.wait(1500)
            if self.browser_host.docked_hwnd:
                Win32ChromeDocker.undock_window(
                    self.browser_host.docked_hwnd, self.browser_host.original_style
                )
                self.browser_host.set_docked_hwnd(None, None)
            logging.getLogger().removeHandler(self.log_handler)
            super().closeEvent(event)


def _run_tk_fallback() -> int:  # pragma: no cover - fallback when PySide6 missing
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    cfg = SniperConfig.load_from_file(SETTINGS_PATH)
    root = tk.Tk()
    root.title("Ticketmaster Seat Sniper (Tkinter Fallback)")
    root.geometry("920x700")

    stop_event = threading.Event()
    worker_thread: Optional[threading.Thread] = None

    frame = ttk.Frame(root, padding=12)
    frame.pack(fill=tk.BOTH, expand=True)

    profile_var = tk.StringVar(value=cfg.profile_dir)
    band_var = tk.StringVar(value=cfg.search_query)
    qty_var = tk.IntVar(value=cfg.ticket_quantity)
    adj_var = tk.BooleanVar(value=cfg.adjacent_only)
    u18_var = tk.BooleanVar(value=cfg.under_18_only)
    pref_var = tk.StringVar(value=cfg.custom_preferred_keywords)
    restr_var = tk.StringVar(value=cfg.custom_exclude_keywords)

    ttk.Label(frame, text="Chrome Profile Folder:").pack(anchor=tk.W)
    p_row = ttk.Frame(frame)
    p_row.pack(fill=tk.X, pady=4)
    ttk.Entry(p_row, textvariable=profile_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
    ttk.Button(
        p_row,
        text="Browse",
        command=lambda: profile_var.set(filedialog.askdirectory() or profile_var.get()),
    ).pack(side=tk.LEFT, padx=4)
    ttk.Button(
        p_row,
        text="Open Chrome -> ticketmaster.fi",
        command=lambda: ChromeProfileManager.launch_chrome_profile(
            profile_var.get(), app_mode=False
        ),
    ).pack(side=tk.LEFT)

    ttk.Label(frame, text="Band / Concert / URL:").pack(anchor=tk.W, pady=(8, 0))
    ttk.Entry(frame, textvariable=band_var).pack(fill=tk.X, pady=4)

    ttk.Label(frame, text="How many tickets:").pack(anchor=tk.W)
    ttk.Spinbox(frame, from_=1, to=10, textvariable=qty_var, width=6).pack(anchor=tk.W)
    ttk.Checkbutton(frame, text="Next to each other", variable=adj_var).pack(anchor=tk.W)
    ttk.Checkbutton(frame, text="Under 18 / No K-18", variable=u18_var).pack(anchor=tk.W)

    ttk.Label(frame, text="Preferred Keywords:").pack(anchor=tk.W, pady=(6, 0))
    ttk.Entry(frame, textvariable=pref_var).pack(fill=tk.X)
    ttk.Label(frame, text="Custom Restricted Keywords:").pack(anchor=tk.W, pady=(6, 0))
    ttk.Entry(frame, textvariable=restr_var).pack(fill=tk.X)

    log_box = scrolledtext.ScrolledText(frame, height=18)
    log_box.pack(fill=tk.BOTH, expand=True, pady=8)

    def start_stop():
        nonlocal worker_thread
        if worker_thread and worker_thread.is_alive():
            stop_event.set()
            return
        stop_event.clear()
        run_cfg = SniperConfig(
            profile_dir=profile_var.get(),
            search_query=band_var.get(),
            ticket_quantity=int(qty_var.get()),
            adjacent_only=adj_var.get(),
            under_18_only=u18_var.get(),
            custom_preferred_keywords=pref_var.get(),
            custom_exclude_keywords=restr_var.get(),
        )
        run_cfg.save_to_file(SETTINGS_PATH)

        def run():
            connector = BrowserConnector(run_cfg)
            try:
                page = connector.connect_and_get_page(open_if_missing=True)
                UnifiedTicketPoller(page, run_cfg).run_loop(stop_event)
            except Exception as exc:
                messagebox.showerror("Error", str(exc))
            finally:
                connector.close()

        worker_thread = threading.Thread(target=run, daemon=True)
        worker_thread.start()

    ttk.Button(frame, text="Search for Ticket / Stop", command=start_stop).pack(
        fill=tk.X, pady=4
    )
    root.mainloop()
    return 0


def main() -> int:
    if not HAS_PYSIDE6:
        return _run_tk_fallback()

    if QApplication.instance() is None:
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication(sys.argv)
    window = TicketmasterSniperWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
