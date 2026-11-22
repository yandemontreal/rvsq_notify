# rvsq_notify

Monitors [Rendez-vous santé Québec](https://www.rvsq.gouv.qc.ca/) for open appointments with your family doctor and alerts you by Telegram and/or email.

It logs in with your RAMQ credentials, opens the appointment search for your doctor, tries the configured services in order, and reports every slot it can see in the weekly calendar. **It never books, moves or cancels anything** — you get the details and decide.

> Use at your own risk. Automated access may conflict with the site's terms of use. Polling uses a randomized interval and a normal Chrome profile to behave as closely to a human as possible, but there is no guarantee of continued access.

## Features

- Headless by default, using real Google Chrome through Playwright.
- Passes Cloudflare on the RVSQ landing and search pages (desktop User-Agent, persistent profile, no automation flags).
- Logs in with RAMQ number, card sequence number, name and date of birth.
- Selects your doctor's tile, checks services in order (`Suivi`, `Consultation semi-urgente`, `Consultation urgente`), stops at the first with availability.
- Opens each clinic card and collects **all** slots across the visible weeks, with clinic name, address, phone, date, time and slot count.
- Notifies via Telegram (message capped and grouped by clinic) and email (complete list).
- Randomized sleep between checks (`CHECK_INTERVAL_MIN_MINUTES` .. `CHECK_INTERVAL_MAX_MINUTES`).
- `--learn` mode: if a result page is not the known "no availability" state, saves the page, notifies and stops so the parser can be updated.
- Optional diagnostics: trace zips, screenshots, HTML dumps.

## Requirements

- macOS (paths assume `/Applications/Google Chrome.app` for the User-Agent version).
- Python 3.11+.
- Google Chrome installed.

## Install

```bash
git clone <your-repo-url> rvsq_notify
cd rvsq_notify
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`run.sh` does the venv setup on first use and forwards arguments:

```bash
./run.sh --test
```

## Configure

Copy `.env.example` to `.env` and fill it in:

```bash
cp .env.example .env
```

### RVSQ credentials

| Variable | Description |
| --- | --- |
| `RAMQ_NUMBER` | Health insurance number |
| `SPECIAL_NUMBER` | Card sequence number (2 digits) |
| `FIRST_NAME`, `LAST_NAME` | Name on the card |
| `DATE_OF_BIRTH` | `YYYY-MM-DD` |
| `DOCTOR_NAME` | Doctor shown on the landing tile, e.g. `ROBERT TREMBLAY` |
| `SERVICE_REASONS` | Ordered list, comma separated (default: `Suivi,Consultation semi-urgente,Consultation urgente`) |
| `POSTAL_CODE`, `PERIMETER_KM` | Only used by the "nearby clinic" flow |

### Notifications

| Variable | Description |
| --- | --- |
| `TELEGRAM_ENABLED` | `true` to enable |
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather), format `123456789:AA...` |
| `TELEGRAM_CHAT_ID` | Send any message to your bot, then read `message.chat.id` from `https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `EMAIL_ENABLED`, `EMAIL_*` | SMTP settings and recipient |

The bot token format is validated at startup, so a placeholder fails fast.

### Scheduling

| Variable | Description |
| --- | --- |
| `CHECK_INTERVAL_MIN_MINUTES` | Minimum sleep between checks (default 45) |
| `CHECK_INTERVAL_MAX_MINUTES` | Maximum sleep between checks (default 90) |
| `TRACE_DIR` | Where diagnostics are written (default `./traces`) |
| `BROWSER_PROFILE_DIR` | Persistent Chrome profile (default `./.browser_profile`) |

## Usage

Runtime behaviour is controlled by CLI flags, not env vars:

```bash
./run.sh                 # run continuously, headless
./run.sh --test          # single check, then exit
./run.sh --headed        # show the browser window
./run.sh --debug         # verbose logs, screenshots and page HTML dumps
./run.sh --traces        # save Playwright trace zips and screenshots
./run.sh --learn         # capture an unknown availability page, notify, stop
./run.sh --notify-test   # send a sample notification
```

Default run writes no artifacts. `--debug` and `--traces` are for troubleshooting, and failure screenshots are always kept.

## How it works

1. Opens the RVSQ rendez-vous app with a persistent Chrome profile. In headless mode it presents a normal desktop Chrome User-Agent, because `HeadlessChrome` is rejected by Cloudflare.
2. Logs in by filling the exact ASP.NET WebForms fields, dismissing the cookie modal, then checks whether the form is still present to detect rejection (the alert text is logged).
3. Clicks the doctor tile on the landing view to open the criteria screen, selects a service reason and searches.
4. If the "no availability" alert is absent, clicks each clinic card, waits for the weekly calendar, and reads `td.h-Day[data-date]` columns and their `.h-TimeButton[data-time][data-ids]` slots, paging with the next-week button. Times are rendered in `America/Toronto`.
5. Sends one notification per distinct set of slots, then sleeps a random interval and repeats.

## Learn mode

The parser was derived from the site's own calendar rendering. If RVSQ changes the availability layout, run `./run.sh --learn`. On the first page that is not the known "no availability" state it writes `traces/learn_<service>_<timestamp>.html`, sends a notification with the path, and stops. Send that file back to update the parser.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Stuck on "Un instant…" | Cloudflare challenge. Use real Chrome (`channel="chrome"`), keep headless with the UA override, or run `--headed`. |
| "Login rejected by RVSQ: Attention! …" | Credentials do not match a RAMQ file; verify the five identity fields. |
| No slots ever reported | Confirm `DOCTOR_NAME` matches the landing tile text. |
| Telegram 404 | Placeholder or invalid bot token; regenerate with @BotFather. |
| Telegram 400 | Message too long; the notifier caps it, but check the logged API response. |
| Periodically challenged | The persistent profile in `BROWSER_PROFILE_DIR` holds the clearance cookie; do not delete it unnecessarily. |

## Project layout

| File | Purpose |
| --- | --- |
| `main.py` | CLI flags, scheduler loop, notification dispatch |
| `rvsq_checker.py` | Playwright login, search, calendar slot collection, learn capture |
| `notifier.py` | Telegram and email delivery |
| `config.py` | Environment configuration and validation |
| `run.sh` | venv bootstrap and argument passthrough |
| `.env.example` | Configuration template |

`.env`, `venv/`, `traces/` and `.browser_profile/` are intentionally ignored by git.
