# Web Health Monitor

A Python desktop application that monitors multiple websites **concurrently using threads**, records uptime and latency through the **logging** module, and raises an alert the moment a site goes down — built with a **PyQt5** GUI.

- **Concepts:** Threading + Logging
- **GUI Toolkit:** PyQt5
- **Level:** Advanced

---

## What it does

Websites go down without warning. A shop's checkout page stops responding, an API starts timing out, a company site slows to a crawl — and the owner usually finds out only when a customer complains.

This app watches a list of URLs continuously. Every few seconds it checks each one, records whether it responded and how long it took, keeps a running uptime percentage, and pops up an alert the instant a site goes down. Every check is written to a log file so the history can be reviewed later.

Each site is shown as **UP** (green), **SLOW** (amber, slower than 1.5 s) or **DOWN** (red).

## Threading

Checking sites one after another would be slow: five sites taking two seconds each means a ten-second round, with the window frozen the whole time. Two levels of threading fix this:

1. **A background worker thread** runs the monitoring loop, so the GUI never freezes and stays clickable while checks are in progress.
2. **A thread pool** checks every URL *at the same time*, so a round takes about as long as the single slowest site rather than the sum of them all.

This is I/O-bound work (waiting on the network), which is exactly where Python threads help despite the GIL. Measured on five deliberately slow endpoints (~1 s each):

| Method | Time |
|---|---|
| Sequential | 5.15 s |
| Concurrent (thread pool) | 1.15 s |
| **Speed-up** | **4.5×** |

## Logging

The `logging` module is used instead of `print()`. Every check is recorded with a timestamp and a severity level:

- `INFO` — the site responded normally
- `WARNING` — the site responded but slowly
- `ERROR` — the site is **DOWN** (bad status code, timeout or connection error)

Logs are written to `web_health_monitor.log` **and** shown live in a panel inside the window. Sample output:

```
2026-07-20 11:49:28 | INFO    | UP    | https://example.com | HTTP 200 | 103 ms
2026-07-20 11:49:28 | ERROR   | DOWN  | https://broken.site | HTTP 500 | 101 ms
2026-07-20 11:49:28 | WARNING | SLOW  | https://slow.site   | HTTP 200 | 2097 ms
```

## Features

- Add and remove URLs to monitor (bare domains like `example.com` are auto-completed to `https://`)
- Adjustable check interval (5 s to 1 hour)
- **Check Once Now** for a single immediate round, or **Start Monitoring** for continuous checks
- Live table showing status, HTTP code, latency, uptime percentage and last-checked time
- Colour-coded rows: green UP, amber SLOW, red DOWN
- Running uptime percentage per site (e.g. `75% (3/4)`)
- Pop-up alert the moment a site changes from up to down (not repeated every round)
- Live activity log inside the window, plus a permanent log file
- Responsive UI with a working Stop button; the worker thread is shut down cleanly on close
- Handles timeouts, unreachable hosts, bad SSL, duplicate entries and empty input without crashing

## Requirements

- **Python 3.8+**
- **PyQt5**

```bash
pip install PyQt5
```

> On macOS with Homebrew Python you may need:
> ```bash
> python -m pip install PyQt5 --break-system-packages
> ```

Networking uses `urllib` from the standard library, so **PyQt5 is the only dependency**.

## How to run

```bash
python web_health_monitor.py
```

Then: type a website → **Add** → set the interval → **Start Monitoring** (or **Check Once Now** for a single round).

## Screenshots

**Monitoring several sites — healthy sites in green with their latency and uptime, a failed site in red, and every check recorded in the activity log:**

![Monitoring view](screenshot1.png)

**An alert is raised the moment a site goes down, with the reason shown and logged:**

![Down alert](screenshot2.png)

> Note the evidence of concurrency in these screenshots: every site in a round is logged at the *same second*, and the status bar reports the whole round finishing in **1.4 s** even though the slowest single site took **1369 ms**. The round takes about as long as the slowest site, not the sum of them all.

## Testing

The application was tested against a local test server with deliberately healthy, failing and slow endpoints, confirming:

- Healthy sites (HTTP 200) are reported **UP**
- Failing sites (HTTP 500) are reported **DOWN** with the error recorded
- Unreachable hosts are handled gracefully instead of crashing
- Slow responses (> 1.5 s) are correctly flagged **SLOW**
- The thread pool gives a genuine **4.5× speed-up** over sequential checking
- Uptime maths is accurate (3 of 4 successful checks → 75%)
- Alerts fire **only** on the up→down transition, not repeatedly while a site stays down
- The consecutive-failure counter resets on recovery
- All three log levels (INFO / WARNING / ERROR) are written to the log file
- The GUI populates the table with correct states and colours, starts and stops the worker thread cleanly, and rejects duplicate URLs

## How I used AI

I used AI (Claude) as a learning and debugging aid — for example to understand why threads help for I/O-bound network calls despite the GIL, and how to run a monitoring loop on a background thread so a PyQt5 window stays responsive. I reviewed and tested the logic myself; the design decisions and final code are my own.

## Project structure

```
web-health-monitor/
├── web_health_monitor.py   # main application (logging, core logic, worker thread, GUI)
├── README.md
├── screenshot1.png
└── screenshot2.png
```
