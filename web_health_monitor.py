"""
============================================================================
 Web Health Monitor
 ----------------------------------------------------------------------------
 Concepts : Threading + Logging          <-- do NOT change
 GUI      : PyQt5
 Level    : Advanced
============================================================================

 REAL-WORLD PROBLEM
 ------------------
 Websites and online services go down without warning. A shop's checkout page
 stops responding, an API starts timing out, a company site slows to a crawl.
 The owner usually finds out only when a customer complains, which is far too
 late.

 This program watches a list of URLs continuously. Every few seconds it checks
 each one, records whether it responded and how long it took, keeps a running
 uptime percentage, and raises an alert the moment a site goes down. Every
 check is written to a log file so the history can be reviewed afterwards.

 THREADING
 ---------
 Checking sites one after another would be slow: if five sites each take two
 seconds to reply, a sequential round takes ten seconds. Worse, the window
 would freeze for that whole time.

 Two levels of threading solve this:

   1. A background WORKER THREAD runs the monitoring loop, so the GUI never
      freezes and stays clickable while checks are in progress.

   2. Inside each round, a THREAD POOL checks every URL AT THE SAME TIME, so
      a round takes about as long as the single slowest site rather than the
      sum of them all. This is I/O-bound work (waiting on the network), which
      is exactly the case where Python threads help despite the GIL.

 LOGGING
 -------
 The logging module is used instead of print(). Every check is recorded with a
 timestamp and a severity level:
   INFO     - the site responded normally
   WARNING  - the site responded but slowly (above the latency threshold)
   ERROR    - the site is DOWN (bad status code, timeout or connection error)
 Logs go to a file (web_health_monitor.log) and to the panel inside the window,
 so problems can be reviewed long after they happened.
============================================================================
"""

import logging
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QApplication, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

LOG_FILE = "web_health_monitor.log"
DEFAULT_TIMEOUT = 8          # seconds to wait for a reply
SLOW_THRESHOLD_MS = 1500     # anything slower than this is "SLOW"


# ===========================================================================
#  PART 1: LOGGING SETUP
# ===========================================================================
def setup_logging():
    """Configure the logging module to write to a file and to the console.

    A named logger is used (rather than the root logger) so the application's
    messages stay separate from any library logging.
    """
    logger = logging.getLogger("WebHealthMonitor")
    logger.setLevel(logging.INFO)

    if logger.handlers:          # avoid adding duplicate handlers on re-run
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


log = setup_logging()


# ===========================================================================
#  PART 2: CORE LOGIC  (no GUI code here, so it is easy to read and test)
# ===========================================================================
@dataclass
class CheckResult:
    """The outcome of checking one URL once."""
    url: str
    is_up: bool
    status_code: int = 0
    latency_ms: float = 0.0
    error: str = ""
    checked_at: datetime = field(default_factory=datetime.now)

    @property
    def state(self):
        """UP, SLOW or DOWN - used for colouring the table."""
        if not self.is_up:
            return "DOWN"
        return "SLOW" if self.latency_ms > SLOW_THRESHOLD_MS else "UP"


@dataclass
class SiteStats:
    """Running totals for one URL across all the checks so far."""
    url: str
    total_checks: int = 0
    successful_checks: int = 0
    total_latency_ms: float = 0.0
    last_result: CheckResult = None
    consecutive_failures: int = 0

    @property
    def uptime_percent(self):
        if self.total_checks == 0:
            return 0.0
        return (self.successful_checks / self.total_checks) * 100

    @property
    def average_latency_ms(self):
        if self.successful_checks == 0:
            return 0.0
        return self.total_latency_ms / self.successful_checks

    def record(self, result):
        """Add one check result and return True if this is a NEW failure.

        A "new failure" means the site was up on the previous check and has
        just gone down, which is the moment an alert should be raised.
        """
        was_up = self.last_result.is_up if self.last_result else True
        self.total_checks += 1
        if result.is_up:
            self.successful_checks += 1
            self.total_latency_ms += result.latency_ms
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
        self.last_result = result
        return was_up and not result.is_up


def normalise_url(url):
    """Add https:// if the user typed a bare domain such as 'example.com'."""
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def check_url(url, timeout=DEFAULT_TIMEOUT):
    """Check a single URL and measure how long it took to respond.

    Returns a CheckResult. Any network problem is caught and turned into a
    failed result rather than being allowed to crash the monitoring loop.
    """
    url = normalise_url(url)
    start = time.perf_counter()
    # Some sites reject requests without a browser-like User-Agent.
    request = urllib.request.Request(url, headers={"User-Agent": "WebHealthMonitor/1.0"})
    context = ssl.create_default_context()

    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            latency = (time.perf_counter() - start) * 1000
            code = response.getcode()
            # 2xx and 3xx are treated as healthy; 4xx/5xx are failures.
            healthy = 200 <= code < 400
            return CheckResult(url, healthy, code, latency,
                               "" if healthy else f"HTTP {code}")

    except urllib.error.HTTPError as e:
        latency = (time.perf_counter() - start) * 1000
        return CheckResult(url, False, e.code, latency, f"HTTP {e.code}")
    except urllib.error.URLError as e:
        latency = (time.perf_counter() - start) * 1000
        return CheckResult(url, False, 0, latency, f"Unreachable: {e.reason}")
    except Exception as e:                      # timeouts, SSL problems, etc.
        latency = (time.perf_counter() - start) * 1000
        return CheckResult(url, False, 0, latency, str(e))


def check_many(urls, timeout=DEFAULT_TIMEOUT, max_workers=10):
    """Check several URLs CONCURRENTLY using a thread pool.

    Because each check spends nearly all its time waiting on the network
    (I/O-bound), threads give a large speed-up here: a whole round takes about
    as long as the slowest single site instead of the sum of them all.
    """
    if not urls:
        return []
    workers = min(max_workers, len(urls))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda u: check_url(u, timeout), urls))


def log_result(result):
    """Write one check result to the log at the appropriate severity level."""
    if not result.is_up:
        log.error("DOWN  | %s | %s | %.0f ms",
                  result.url, result.error, result.latency_ms)
    elif result.latency_ms > SLOW_THRESHOLD_MS:
        log.warning("SLOW  | %s | HTTP %d | %.0f ms",
                    result.url, result.status_code, result.latency_ms)
    else:
        log.info("UP    | %s | HTTP %d | %.0f ms",
                 result.url, result.status_code, result.latency_ms)


# ===========================================================================
#  PART 3: BACKGROUND WORKER THREAD
#  The monitoring loop runs here so the window never freezes while waiting
#  for slow websites to reply.
# ===========================================================================
class MonitorWorker(QThread):
    round_finished = pyqtSignal(list)     # list[CheckResult]
    alert_raised = pyqtSignal(str, str)   # url, error message
    message = pyqtSignal(str)             # status line text

    def __init__(self, urls, interval_seconds, stats):
        super().__init__()
        self.urls = list(urls)
        self.interval = interval_seconds
        self.stats = stats                # shared dict: url -> SiteStats
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        log.info("Monitoring started for %d site(s), every %d s",
                 len(self.urls), self.interval)
        while self._running:
            self.message.emit(f"Checking {len(self.urls)} site(s)...")
            started = time.perf_counter()

            results = check_many(self.urls)          # concurrent thread pool

            for result in results:
                log_result(result)
                stat = self.stats.setdefault(result.url, SiteStats(result.url))
                newly_failed = stat.record(result)
                if newly_failed:
                    log.error("ALERT | %s has gone DOWN (%s)",
                              result.url, result.error)
                    self.alert_raised.emit(result.url, result.error)

            elapsed = time.perf_counter() - started
            self.round_finished.emit(results)
            self.message.emit(f"Round finished in {elapsed:.1f}s \u2014 "
                              f"next check in {self.interval}s")

            # Sleep in small steps so Stop responds quickly.
            for _ in range(self.interval * 10):
                if not self._running:
                    break
                self.msleep(100)

        log.info("Monitoring stopped")
        self.message.emit("Monitoring stopped.")


# ===========================================================================
#  PART 4: GRAPHICAL USER INTERFACE  (PyQt5)
# ===========================================================================
class WebHealthMonitorWindow(QMainWindow):

    COLOURS = {
        "UP":   QColor(214, 245, 214),
        "SLOW": QColor(255, 243, 205),
        "DOWN": QColor(250, 214, 214),
    }

    def __init__(self):
        super().__init__()
        self.setWindowTitle(
            "Web Health Monitor  -  Threading + Logging  (Waleed Ahmad Khan)")
        self.resize(1000, 680)

        self.urls = []
        self.stats = {}          # url -> SiteStats
        self.worker = None

        self._build_ui()

    # ---------------------------------------------------------------- layout
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # ---- add / remove sites --------------------------------------------
        add_box = QGroupBox("1. Websites to monitor")
        add_row = QHBoxLayout(add_box)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("e.g. example.com  or  https://example.com")
        self.url_edit.returnPressed.connect(self.add_url)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self.add_url)
        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(self.remove_selected)
        add_row.addWidget(self.url_edit)
        add_row.addWidget(add_btn)
        add_row.addWidget(remove_btn)
        layout.addWidget(add_box)

        # ---- controls ------------------------------------------------------
        control_box = QGroupBox("2. Monitoring")
        control_row = QHBoxLayout(control_box)
        control_row.addWidget(QLabel("Check every"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(5, 3600)
        self.interval_spin.setValue(15)
        self.interval_spin.setSuffix(" s")
        control_row.addWidget(self.interval_spin)
        self.start_btn = QPushButton("Start Monitoring")
        self.start_btn.clicked.connect(self.start_monitoring)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_monitoring)
        self.stop_btn.setEnabled(False)
        self.check_now_btn = QPushButton("Check Once Now")
        self.check_now_btn.clicked.connect(self.check_once)
        control_row.addWidget(self.start_btn)
        control_row.addWidget(self.stop_btn)
        control_row.addWidget(self.check_now_btn)
        control_row.addStretch()
        layout.addWidget(control_box)

        # ---- results table -------------------------------------------------
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["URL", "Status", "Code", "Latency", "Uptime", "Last checked"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table, stretch=2)

        # ---- live log panel ------------------------------------------------
        layout.addWidget(QLabel("Activity log (also saved to %s):" % LOG_FILE))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)     # keep memory bounded
        layout.addWidget(self.log_view, stretch=1)

        self.status = QLabel("Ready. Add a website and press \"Start Monitoring\".")
        layout.addWidget(self.status)

    # ------------------------------------------------------------ site list
    def add_url(self):
        url = normalise_url(self.url_edit.text())

        # --- edge case: empty box ---
        if not url:
            QMessageBox.warning(self, "No URL", "Please type a website address.")
            return

        # --- edge case: duplicate entry ---
        if url in self.urls:
            QMessageBox.information(self, "Already added",
                                    f"{url} is already being monitored.")
            return

        self.urls.append(url)
        self.stats[url] = SiteStats(url)
        self.url_edit.clear()
        self.refresh_table()
        self.append_log(f"Added {url}")

    def remove_selected(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Nothing selected",
                                    "Please click a row in the table first.")
            return
        url = self.table.item(row, 0).text()
        self.urls.remove(url)
        self.stats.pop(url, None)
        self.refresh_table()
        self.append_log(f"Removed {url}")

    # --------------------------------------------------------- monitoring
    def start_monitoring(self):
        # --- edge case: nothing to monitor ---
        if not self.urls:
            QMessageBox.warning(self, "No websites",
                                "Add at least one website before starting.")
            return

        self.start_btn.setEnabled(False)
        self.check_now_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.interval_spin.setEnabled(False)

        self.worker = MonitorWorker(self.urls, self.interval_spin.value(), self.stats)
        self.worker.round_finished.connect(self.on_round_finished)
        self.worker.alert_raised.connect(self.on_alert)
        self.worker.message.connect(self.status.setText)
        self.worker.start()
        self.append_log("Monitoring started")

    def stop_monitoring(self):
        if self.worker:
            self.worker.stop()
            self.worker.wait(3000)
        self.start_btn.setEnabled(True)
        self.check_now_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.interval_spin.setEnabled(True)
        self.append_log("Monitoring stopped")

    def check_once(self):
        """Run a single round immediately, without starting the loop."""
        if not self.urls:
            QMessageBox.warning(self, "No websites",
                                "Add at least one website first.")
            return
        self.status.setText("Checking once...")
        QApplication.processEvents()
        results = check_many(self.urls)
        for result in results:
            log_result(result)
            stat = self.stats.setdefault(result.url, SiteStats(result.url))
            if stat.record(result):
                self.on_alert(result.url, result.error)
        self.on_round_finished(results)
        self.status.setText("Single check complete.")

    # ----------------------------------------------------- worker signals
    def on_round_finished(self, results):
        self.refresh_table()
        for r in results:
            self.append_log(
                f"{r.state:<4} | {r.url} | "
                f"{'HTTP ' + str(r.status_code) if r.status_code else r.error} | "
                f"{r.latency_ms:.0f} ms")

    def on_alert(self, url, error):
        """Called the moment a site goes from up to down."""
        self.append_log(f"*** ALERT: {url} is DOWN ({error}) ***")
        QMessageBox.critical(self, "Site is down",
                             f"{url} has gone DOWN.\n\n{error}")

    # ------------------------------------------------------------- display
    def refresh_table(self):
        self.table.setRowCount(len(self.urls))
        for row, url in enumerate(self.urls):
            stat = self.stats.get(url)
            result = stat.last_result if stat else None

            if result is None:
                values = [url, "Not checked", "-", "-", "-", "-"]
                colour = None
            else:
                values = [
                    url,
                    result.state,
                    str(result.status_code) if result.status_code else "-",
                    f"{result.latency_ms:.0f} ms",
                    f"{stat.uptime_percent:.0f}%  ({stat.successful_checks}/{stat.total_checks})",
                    result.checked_at.strftime("%H:%M:%S"),
                ]
                colour = self.COLOURS.get(result.state)

            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if colour:
                    item.setBackground(colour)
                    item.setForeground(QColor(0, 0, 0))
                if col != 0:
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, col, item)

    def append_log(self, text):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{stamp}] {text}")

    def closeEvent(self, event):
        """Make sure the worker thread is stopped before the window closes."""
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)
        event.accept()


# ===========================================================================
#  PROGRAM ENTRY POINT
# ===========================================================================
def main():
    app = QApplication(sys.argv)
    window = WebHealthMonitorWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
