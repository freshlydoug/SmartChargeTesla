# SmartChargeTesla

Automatically manages a **Tesla Powerwall** during **EON Next Kraken** EV dispatch windows, so when you charge your electric vehicle on the cheapest possible rate during a Smart dispatch the Powerwall charges at the same time.

Designed for the EON Next **Drive Smart** tariff (formerly Next Drive v5.1), which offers off-schedule cheap-rate windows dispatched via the Kraken SmartFlex platform. Works alongside a **myenergi Zappi** charger, which is used to confirm that the EV is actually drawing power before activating Powerwall protection.

---

## How it works

```
EON Next Kraken API
        │  polls every 60 s for planned dispatch windows
        ▼
  SQLite database  ◄──────────────────────────────────────┐
        │                                                  │
        ▼                                                  │
  dispatch_action_loop (every 60 s)                       │
        │                                                  │
        ├─ Dispatch window active?                         │
        │   └─ YES: check myenergi Zappi                  │
        │       ├─ Zappi charging? → Powerwall BACKUP 100% │
        │       │   └─ Dispatch or Zappi ends → revert just before 30min slot ends  │
        │       └─ Zappi idle    → revert to TIME_BASED   │
        │                                                  │
        └─ Google Calendar invite sent when charging confirmed
```

**Powerwall modes used:**

| Situation | Mode | Reserve |
|-----------|------|---------|
| EV charging during dispatch window | BACKUP | 100% |
| Outside dispatch window / EV idle | TIME_BASED_CONTROL | configurable (default 20%) |
| 00:00–06:00 UK (cheap rate hours) | no change | — |

The service also:
- Prunes stale dispatch records when Kraken withdraws or reschedules windows
- Extends BACKUP by 29 minutes if the Zappi is still charging when a window ends
- Sends and updates Google Calendar invites as dispatch windows are confirmed, rescheduled, or completed
- Performs a midnight reset to TIME_BASED_CONTROL so the cheap overnight rate (00:00–06:00) is never blocked

---

## Hardware requirements

| Component | Notes |
|-----------|-------|
| Tesla Powerwall 2 or 3 | Local gateway or cloud access |
| myenergi Zappi | Any generation; hub required for API access |
| EON Next Drive Smart tariff | Kraken SmartFlex dispatch service must be enrolled |
| Raspberry Pi (or similar) | Runs continuously; Pi 3/4/5 and Pi Zero 2W all work |

---

## Prerequisites

- Python 3.11 or later
- EON Next account enrolled in Drive Smart / KrakenFlex EV dispatch
- myenergi hub connected to the internet
- Tesla account (free owner API — no Fleet API developer account needed)
- Gmail account with an App Password for optional calendar invites

---

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/SmartChargeTesla.git
cd SmartChargeTesla
pip install -r requirements.txt
```

Copy the example config and fill in your credentials:

```bash
cp config.example.yaml config.yaml
nano config.yaml
```

---

## Configuration

See [`config.example.yaml`](config.example.yaml) for a fully annotated template.

### Required sections

#### EON Next

```yaml
eon_next:
  email: "you@example.com"
  password: "your-eon-password"
  import_account: "A-XXXXXXXX"   # from EON Next app or portal
```

Your import account number is on your EON Next bill or in the app under
**Account → Electricity → Account number**.

#### myenergi Zappi

```yaml
myenergi:
  hub_serial: "12345678"   # Hub serial (starts with 10 or 11)
  api_key: "abcdef12"      # From myenergi app: Settings → myenergi hub → Advanced
```

#### Tesla Powerwall

```yaml
tesla:
  account_email: "you@tesla.com"
  account_password: "your-tesla-password"
```

**One-time authentication step** — run this interactively before starting the service
(it opens a browser for Tesla SSO):

```bash
python3 -c "
import pypowerwall
pypowerwall.Powerwall('', password='YOUR_PASSWORD', email='YOUR_EMAIL',
                      cloudmode=True, authpath='.')
"
```

This caches the auth token in `.pypowerwall.json`. The service then runs unattended.

### Optional: Google Calendar invites

Requires a **Gmail App Password** (not your account password):
Google Account → Security → 2-Step Verification → App passwords

```yaml
gcal:
  smtp_user: "sender@gmail.com"
  smtp_pass: "xxxx xxxx xxxx xxxx"   # 16-char app password
  invite_to: "your-calendar@gmail.com"
  cheap_rate_pence: 6.19
  standard_rate_pence: 26.073
```

When the Zappi starts charging in a dispatch window, a calendar event is sent showing the window start/end time and estimated kWh. The event is updated if the window changes and trimmed to actual end time when the Zappi stops.

### Control settings

```yaml
control:
  default_reserve_pct: 20   # Battery % floor when not in BACKUP mode
```

---

## Running manually

```bash
python run.py
```

Or with a custom config path:

```bash
python run.py --config /path/to/config.yaml
```

---

## Running as a systemd service

Edit `smartcharge.service` and update `User` and `WorkingDirectory` to match your system:

```bash
sudo cp smartcharge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smartcharge
```

Check logs:

```bash
journalctl -u smartcharge -f
```

---

## Database

Dispatch windows are stored in a local SQLite database at `data/smartcharge.db`.
The database is created automatically on first run.

Schema:

```sql
CREATE TABLE dispatches (
    start TEXT,          -- UTC ISO datetime
    end TEXT,            -- UTC ISO datetime
    delta_kwh REAL,      -- expected energy (negative = import)
    type TEXT,           -- 'SMART_FLEX' or NULL for completed
    source TEXT,
    location TEXT,
    gcal_event_id TEXT,  -- calendar event UID
    gcal_sequence INT,
    fetched_at TEXT,
    UNIQUE(start, end)
);
```

---

## Project structure

```
SmartChargeTesla/
├── run.py                     Entry point
├── config.example.yaml        Configuration template
├── requirements.txt
├── smartcharge.service        systemd unit file
├── smartcharge/
│   ├── service.py             dispatch_loop + dispatch_action_loop
│   ├── apis/
│   │   ├── kraken.py          EON Next Kraken GraphQL client (dispatch queries)
│   │   ├── myenergi.py        myenergi Zappi charging-status client
│   │   └── gcal.py            Google Calendar iCalendar invite sender
│   └── db/
│       ├── schema.py          SQLite schema and migrations
│       └── store.py           DispatchStore CRUD helpers
└── data/                      SQLite database (gitignored)
```

---

## Dependencies and credits

| Library | Author | Purpose | Licence |
|---------|--------|---------|---------|
| [pypowerwall](https://github.com/jasonacox/pypowerwall) | Jason Cox | Tesla Powerwall cloud API — mode and reserve control | MIT |
| [tesla_powerwall](https://github.com/jrester/tesla_powerwall) | jrester | Local gateway API; source of Powerwall operation mode values (`"autonomous"`, `"backup"`) used in this project | MIT |
| [pymyenergi](https://github.com/cjne/pymyenergi) | cjne | myenergi hub API client — Zappi charging status | MIT |
| [requests](https://docs.python-requests.org/) | Kenneth Reitz et al. | HTTP client for Kraken GraphQL calls | Apache 2.0 |
| [PyYAML](https://pyyaml.org/) | Kirill Simonov | Configuration file parsing | MIT |
| [aiohttp](https://docs.aiohttp.org/) | aio-libs | Async HTTP (required by pymyenergi) | Apache 2.0 |
| [httpx](https://www.python-httpx.org/) | Encode | Async HTTP (required by pymyenergi batch fetch) | BSD |

### Also useful

[TeslaPy](https://github.com/tdorssers/TeslaPy) by Tim Dorssers is not a dependency of this project
but is a convenient way to generate the Tesla OAuth refresh token needed by pypowerwall if you
prefer not to use the interactive browser login:

```bash
pip install teslapy
python3 -c "import teslapy; t = teslapy.Tesla('your@email.com'); t.fetch_token()"
```

### API notices

The EON Next Kraken GraphQL API is an unofficial, undocumented interface
to the same Kraken backend used by the EON Next mobile app. No affiliation
with EON Next or Kraken Technologies is implied.

The pypowerwall and tesla_powerwall libraries use Tesla's free owner API
(not the paid Fleet API). No Tesla developer account is required.
Tesla, Powerwall, and related marks are trademarks of Tesla, Inc.

myenergi and Zappi are trademarks of myenergi Ltd.

---

## Licence

MIT — see [LICENSE](LICENSE).
