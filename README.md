# Spot Price Inverter Automation

Automatically controls a **Deye LV 3-Phase Hybrid Inverter** via Home Assistant based on day-ahead electricity spot prices from the Slovak electricity market (OKTE).

---

## What it does (non-technical)

Electricity on the wholesale market is priced every 15 minutes. Most of the time the price is positive — you pay to use electricity from the grid, and you get paid for what you sell. Occasionally — typically at solar/wind peaks — the price goes **negative**: you would have to *pay* to export.

This automation tracks those prices and switches the inverter into one of three modes for every 15-minute window. The battery **never** sells to grid and **never** charges from grid — it only ever exchanges energy with PV (charge) and home loads (discharge):

| Market situation | What happens |
|---|---|
| **Negative price** (exporting costs you) | Inverter blocks all export (`Zero Export To CT`), Smart Load turns ON, battery target SOC = 100 % so PV charges the battery instead of going to grid |
| **Positive price**, more negatives still ahead today, **and PV is producing** | "Export First" mode preserves the battery via the `max(current SOC, 26)` SOC ratchet so it stays available to absorb the upcoming negative window's PV; solar surplus exports to grid |
| **Positive price**, no more negatives today **OR** before sunrise / after sunset | `Zero Export To CT` + Solar Sell — battery freely discharges to home loads down to the 26 % floor; solar surplus still exports |

The middle case is the only one where the inverter is in `Export First`; the SOC ratchet makes sure even then the battery cannot be sold to grid. Overnight always falls into the third case (PV = 0), so any leftover battery from the previous day is used to power the home through the night.

Every day at **16:00** the script downloads **tomorrow's** 96 price slots from OKTE's public API and stores them as `schedule_next.json`, then re-asserts safe defaults on Programs 1–5 (the fallback ToU programs). From then on, every 15 minutes (15 seconds before each slot boundary) the script reads the upcoming slot, the current battery SOC, and current PV power, then reconfigures **only Program 6** for that single slot — Programs 1–5 are left at their safety-net values and are never modified by the per-slot apply. Program 6 was identified by empirical testing as the program this firmware actually applies when all six ToU programs share start time `00:00:00` (the highest-numbered one wins the tiebreak), so it is the one that controls real behavior. When the day rolls over at midnight, the apply routine automatically promotes `schedule_next.json` to the active `schedule.json` — no separate midnight cron is needed.

---

## Technical overview

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Home Assistant                           │
│                                                                 │
│  ┌──────────────┐                ┌────────────────────────────┐ │
│  │  Automation  │                │  Automation                │ │
│  │  "Fetch"     │                │  "Apply"                   │ │
│  │  16:00 daily │                │  HH:14:45 / :29:45 /       │ │
│  │              │                │       :44:45 / :59:45      │ │
│  └──────┬───────┘                └──────────────┬─────────────┘ │
│         │ shell_command                         │ shell_command │
│         ▼                                       ▼               │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │               spot_automation.py                        │    │
│  │                                                         │    │
│  │   fetch ──► OKTE API (TOMORROW) ──► schedule_next.json  │    │
│  │           ──► reset Programs 1–5 to safe defaults       │    │
│  │                                                         │    │
│  │   apply ──► target_time = now + 30 s                    │    │
│  │             ├─ if schedule.json covers it → use it      │    │
│  │             └─ else if schedule_next.json covers it     │    │
│  │                  → rename next → active (rollover)      │    │
│  │                                                         │    │
│  │             price < 0       → negative                  │    │
│  │                                Program 6 SOC = 100      │    │
│  │             price ≥ 0 AND   → positive_export           │    │
│  │             negs ahead AND    Program 6 SOC =           │    │
│  │             PV > 100 W        max(current %, 26)        │    │
│  │             otherwise       → positive_self_consume     │    │
│  │                                Program 6 SOC = 26       │    │
│  │             (ToU always Enabled; no grid charging)      │    │
│  └─────────────────────────────────────────────────────────┘    │
│                              │                                  │
│                    HA REST API  (Bearer token)                  │
│                              │                                  │
│  ┌───────────────────────────▼─────────────────────────────┐    │
│  │              Deye LV 3-Phase Hybrid Inverter            │    │
│  │  select.deye_inverter_work_mode                         │    │
│  │  select.deye_inverter_time_of_use                       │    │
│  │  number.deye_inverter_program_6_soc       (active)      │    │
│  │  number.deye_inverter_program_{1..5}_soc  (safety net)  │    │
│  │  ...                                                    │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘

External:
  OKTE API  https://isot.okte.sk/api/v1/dam/results
  Returns 96 × 15-min periods per day, each with a price in EUR/MWh
```

### Data flow

1. **16:00 daily** — `fetch` runs, downloads **tomorrow's** prices from OKTE API
   - 96 × 15-min periods, each with `price` in EUR/MWh
   - Saves to `schedule_next.json` — does **not** touch the active `schedule.json`
   - Re-asserts safe defaults on ToU Programs 1–5 (see "Program layout" below)

2. **Every 15 min, at HH:14:45 / :29:45 / :44:45 / :59:45** — `apply` runs
   - Computes `target_time = now + 30 s` (the slot about to begin)
   - If `schedule.json` doesn't cover `target_time` but `schedule_next.json` does
     → rename `schedule_next.json` → `schedule.json` (midnight rollover)
   - Reads the upcoming slot's price
   - Reconfigures the inverter for that single slot

### Per-slot decision logic

```
price < 0  →  mode = "negative"
              work_mode      = "Zero Export To CT"
              energy_pattern = "Load First"
              export_surplus = OFF                  block all export
              smart_load     = ON                   consume surplus locally
              time_of_use    = Enabled
              Program 6 SOC  = 100                  PV can fully charge battery - not really used/needed in "Zero Export To CT"

price ≥ 0 AND has_remaining_negatives_today AND PV > 100 W
           →  mode = "positive_export"
              work_mode      = "Export First"       in Deye docs "Selling First"
              energy_pattern = "Load First"
              export_surplus = ON                   sell solar surplus
              smart_load     = OFF
              time_of_use    = Enabled
              Program 6 SOC  = max(current %, 26)   freeze battery at current
                                                    level so the upcoming
                                                    negative slot can absorb
                                                    PV; never below 26 %; 
                                                    this prevent discharging
                                                    of battery

otherwise (price ≥ 0 — overnight, post-sunset, or no more negs today)
           →  mode = "positive_self_consume"
              work_mode      = "Zero Export To CT"  battery cannot export
              energy_pattern = "Load First"
              export_surplus = ON                   Solar Sell — only PV
                                                    surplus exports
              smart_load     = ON
              time_of_use    = Enabled
              Program 6 SOC  = 26                   battery freely discharges
                                                    to home loads down to 26 %
```

The "PV > 100 W" check uses `sensor.deye_inverter_pv_power` (the inverter's
combined PV1+PV2 reading). The threshold lives in `config.yaml` under
`program.pv_detection_threshold`.

`has_remaining_negatives_today` is a forward-looking check on the active
schedule: it returns `True` if any slot starting after the current target time,
on the same local-time calendar day, has a negative price. The "today"
boundary is local-day; cross-midnight negatives on tomorrow do not count.

Battery charges from solar only — `program_6_charging` is always `Disabled`.
Time of Use is always Enabled; **Program 6 is the active program** (its `time`
is fixed at `00:00:00`). Each apply rewrites Program 6's SOC threshold and
toggles work_mode.

### Program layout (ToU 1–6)

All six ToU programs share start time `00:00:00`. On this firmware the
**highest-numbered** program with a "passed" start time wins the tiebreak — so
Program 6 is always selected during normal operation, and Programs 1–5 sit
underneath as a fallback.

| Program | Role     | Start time | Power | SOC | Charging | Updated by |
|---------|----------|-----------:|------:|----:|----------|------------|
| 1–5     | Safety net | `00:00:00` | 2000 W | 26 % | Disabled | `fetch` (daily, 16:00) |
| 6       | Active     | `00:00:00` | `discharge_power` | per-slot | Disabled | `apply` (every 15 min) |

If something ever leaves Program 6 in a bad state, the inverter falls back to
one of 1–5 — battery floored at 26 %, no grid charging, modest discharge — which
is a benign state. The daily re-write of 1–5 by `fetch` defends against drift
from manual UI edits, integrations, or reboots.

> **Note:** `Zero Export To CT` requires an external CT clamp installed (manual section 3.6).
> If you don't have one, change `work_mode` under `negative` to `Zero Export To Load`
> (backup loads only — home circuits won't be powered by the inverter).

### Schedule file lifecycle

```
D-1 16:00   fetch    → writes schedule_next.json (delivery_day = D)
D-1 17:00   apply    → uses schedule.json (D-1's slots, still active)
...
D-1 23:59:45 apply   → schedule.json doesn't cover target_time (00:00 D)
                        schedule_next.json does → rename next → active
D    00:14:45 apply  → uses schedule.json (D's slots)
...
D    16:00   fetch   → writes schedule_next.json (delivery_day = D+1)
```

This keeps the *current* day's schedule intact across the 16:00 fetch — the
old single-file design used to no-op the whole 16:00–24:00 window because the
file had been overwritten with tomorrow's prices.

### File reference

```
el-spot/
├── config.yaml             # HA credentials, entity IDs, modes
├── spot_automation.py      # Main script (fetch / apply / status)
├── deye_spot_el_prices_package.yaml  # HA package: shell_commands + 2 automations
├── requirements.txt        # Python deps: requests, pyyaml
├── schedule.json           # Active day's schedule (generated, not committed)
└── schedule_next.json      # Pending next-day schedule (generated, not committed)
```

### OKTE API

- **Endpoint:** `GET https://isot.okte.sk/api/v1/dam/results`
- **Params:** `deliveryDayFrom`, `deliveryDayTo` (ISO 8601 date)
- **Returns:** JSON array of 96 objects (one per 15-min MTU period)
- **Key fields:** `price` (EUR/MWh), `deliveryStart`, `deliveryEnd` (UTC ISO 8601), `period` (1–96)
- **Publishes:** no later than 15:30 CET per official OKTE FAQ — the 16:00 fetch has a 30-min safety margin

---

## Setup

### 1. Copy files to Home Assistant

Place the whole directory at `/config/spot_automation/` on your HA host (e.g. via Samba share or SSH).

```
/config/spot_automation/
├── config.yaml
├── spot_automation.py
└── requirements.txt
```

### 2. Create a virtual environment and install dependencies

```bash
cd /config/spot_automation
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

The venv keeps dependencies isolated from the system Python.

### 3. Edit `config.yaml`

```yaml
ha:
  url: "http://homeassistant.local:8123"
  token: "YOUR_TOKEN"   # HA → Profile → Long-Lived Access Tokens
```

Find your exact Deye entity IDs in **HA → Developer Tools → States** and search for `deye`. Update the entity IDs under the `deye:` section.

Verify that the option strings match your inverter's exact labels:

| Setting | Value the script writes | Where to check |
|---|---|---|
| `work_mode` | `Zero Export To CT` / `Export First` | `select.deye_inverter_work_mode` → `options` attribute |
| `energy_pattern` | `Load First` | `select.deye_inverter_energy_pattern` → `options` |
| `time_of_use` | `Enabled` | `select.deye_inverter_time_of_use` → `options` |
| `program_6_charging` (and 1–5) | `Disabled` | `select.deye_inverter_program_6_charging` → `options` |

Adjust `program.discharge_power` (W) and `program.target_soc_floor` (%) to
taste. The default floor is **26 %**, chosen as a 1 % safety margin above this
battery's hardware shutdown threshold of 25 %. Setting the floor at or below
25 % would let the inverter try to discharge through the shutdown point. If
your battery's low-SOC cutoff is different, set the floor to `cutoff + 1` (or
higher).

The `safe_programs` block defines the fallback values written to Programs 1–5
once a day. The defaults (`time=00:00:00`, `power=2000`, `soc=26`,
`charging=Disabled`) are deliberately conservative — change them only if you
understand the safety implications. The `soc=26` here is for the same reason
as the `target_soc_floor` above (1 % above the 25 % battery shutdown).

> **Firmware note on tiebreaks.** This automation assumes the
> *highest-numbered* ToU program wins when multiple programs share start time
> `00:00:00`. Verify on your inverter before running in production: temporarily
> set Program 5 to a clearly different SOC (e.g. 80 %) and watch which value
> the inverter actually applies. If your firmware picks the lowest-numbered
> program instead, swap the roles of Program 1 and Program 6 in `config.yaml`
> (active = 1, safety net = 2–6).

### 4. Enable HA packages

In `configuration.yaml` (https://www.home-assistant.io/docs/configuration/packages/#create-a-packages-folder):

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Copy `deye_spot_el_prices_package.yaml` into `config/packages/` and reload HA
configuration. The filename becomes the package name in HA, so keep it (or
rename to whatever you'd like the package to be called).

### 5. Test manually

```bash
cd /config/spot_automation

# Download tomorrow's prices (writes schedule_next.json)
venv/bin/python spot_automation.py fetch

# Preview both schedules (active + pending)
venv/bin/python spot_automation.py status

# Apply inverter mode for the upcoming 15-min slot
venv/bin/python spot_automation.py apply
```

---

## Troubleshooting

**"OKTE returned empty response"** — Prices for the next day are published daily at no later than 15:30 on the following trading day. Running `fetch` before that will fail. The automation runs at 16:00 to avoid this; manual retries respect the configured backoff.

**"No schedule covers <time>"** — Either you've never run `fetch`, or both `schedule.json` and `schedule_next.json` are stale (e.g., a fetch failure left them outdated). Run `fetch` and wait for the next slot boundary.

**Inverter entities not found / service call fails** — Double-check entity IDs in `config.yaml`. Use HA Developer Tools → Template to test: `{{ states('select.your_entity_id') }}`.

**Wrong work mode / ToU option strings** — In HA Developer Tools → States, click the relevant select entity and check the `options` attribute for the exact strings your inverter exposes. Update `config.yaml` accordingly.

**Apply fires at the wrong second** — Confirm your HA host clock is accurate (NTP). The cron triggers at second 45 of minutes 14/29/44/59; the script then targets `now + 30 s` to land safely inside the upcoming slot.
