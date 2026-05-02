#!/usr/bin/env python3
"""
Spot price automation for Deye LV 3-Phase Hybrid Inverter via Home Assistant.

Commands:
  fetch   — fetch next-day OKTE prices and save schedule_next.json (run at 16:00)
  apply   — apply mode for the upcoming 15-min slot                 (run at
            HH:14:45, HH:29:45, HH:44:45, HH:59:45 — 15 s before each boundary)
  status  — print active and pending schedules with prices and target modes
"""

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.yaml")
SCHEDULE_FILE = os.path.join(SCRIPT_DIR, "schedule.json")
SCHEDULE_NEXT_FILE = os.path.join(SCRIPT_DIR, "schedule_next.json")

# Display timezone for user-facing output. Auto-handles CET ↔ CEST at DST.
LOCAL_TZ = ZoneInfo("Europe/Bratislava")


def fmt_local_hhmm(iso_str: str) -> str:
    """Convert a UTC ISO 8601 timestamp to local-time 'HH:MM'."""
    return datetime.fromisoformat(iso_str).astimezone(LOCAL_TZ).strftime("%H:%M")


# ── Config ────────────────────────────────────────────────────────────────────


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


# ── OKTE API ──────────────────────────────────────────────────────────────────


def fetch_okte_prices(target_date: date) -> list[dict]:
    date_str = target_date.strftime("%Y-%m-%d")
    url = (
        "https://isot.okte.sk/api/v1/dam/results"
        f"?deliveryDayFrom={date_str}&deliveryDayTo={date_str}"
    )
    log.info("Fetching OKTE prices for %s", date_str)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"OKTE returned empty response for {date_str}. Prices may not be published yet.")
    return data


def build_schedule(prices: list[dict]) -> list[dict]:
    schedule = []
    for p in sorted(prices, key=lambda x: x["period"]):
        price = p.get("price")
        if price is None:
            continue
        schedule.append({
            "period": p["period"],
            "delivery_start": p["deliveryStart"],
            "delivery_end": p["deliveryEnd"],
            "price": price,
        })
    return schedule


# ── Schedule persistence ──────────────────────────────────────────────────────


def save_schedule(schedule: list[dict], target_date: date, path: str) -> None:
    data = {
        "delivery_day": target_date.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schedule": schedule,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    log.info("Saved schedule to %s", path)


def load_schedule(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def get_period_for_time(schedule: list[dict], t: datetime) -> dict | None:
    for p in schedule:
        start = datetime.fromisoformat(p["delivery_start"])
        end = datetime.fromisoformat(p["delivery_end"])
        if start <= t < end:
            return p
    return None


def has_remaining_negatives_today(target_time: datetime, schedule: list[dict]) -> bool:
    """True if any slot starting AFTER target_time on the same local day has price < 0."""
    target_local_date = target_time.astimezone(LOCAL_TZ).date()
    for p in schedule:
        start = datetime.fromisoformat(p["delivery_start"])
        if start <= target_time:
            continue
        if start.astimezone(LOCAL_TZ).date() != target_local_date:
            continue
        if p["price"] < 0:
            return True
    return False


def active_schedule_for(target_time: datetime) -> dict | None:
    """
    Return the schedule that covers target_time. If schedule.json doesn't cover
    it but schedule_next.json does, promote next → active (midnight rollover).
    """
    data = load_schedule(SCHEDULE_FILE)
    if data and get_period_for_time(data["schedule"], target_time) is not None:
        return data

    next_data = load_schedule(SCHEDULE_NEXT_FILE)
    if next_data and get_period_for_time(next_data["schedule"], target_time) is not None:
        log.info("Rolling over schedule: %s now active", next_data["delivery_day"])
        os.replace(SCHEDULE_NEXT_FILE, SCHEDULE_FILE)
        return next_data

    return None


# ── Home Assistant client ─────────────────────────────────────────────────────


class HAClient:
    def __init__(self, url: str, token: str):
        self.base = url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, data: dict) -> None:
        resp = requests.post(
            f"{self.base}/api/{path}", headers=self.headers, json=data, timeout=10
        )
        resp.raise_for_status()

    def get_state(self, entity_id: str) -> dict:
        resp = requests.get(
            f"{self.base}/api/states/{entity_id}", headers=self.headers, timeout=10
        )
        resp.raise_for_status()
        return resp.json()

    def set_select(self, entity_id: str, option: str) -> None:
        log.info("  select  %s → %s", entity_id, option)
        self._post("services/select/select_option", {"entity_id": entity_id, "option": option})

    def set_switch(self, entity_id: str, on: bool) -> None:
        log.info("  switch  %s → %s", entity_id, "ON" if on else "OFF")
        self._post(f"services/switch/{'turn_on' if on else 'turn_off'}", {"entity_id": entity_id})

    def set_number(self, entity_id: str, value: float) -> None:
        log.info("  number  %s → %s", entity_id, value)
        self._post("services/number/set_value", {"entity_id": entity_id, "value": value})

    def set_time(self, entity_id: str, value: str) -> None:
        log.info("  time    %s → %s", entity_id, value)
        self._post("services/time/set_value", {"entity_id": entity_id, "time": value})


# ── Mode application ──────────────────────────────────────────────────────────


def apply_mode(ha: HAClient, config: dict, mode_name: str) -> None:
    deye = config["deye"]
    mode_cfg = config["modes"][mode_name]

    for key in ("work_mode", "energy_pattern", "time_of_use"):
        if key in deye and key in mode_cfg:
            ha.set_select(deye[key], mode_cfg[key])

    for switch_key in ("export_surplus", "smart_load"):
        if switch_key in deye and switch_key in mode_cfg:
            ha.set_switch(deye[switch_key], mode_cfg[switch_key])


def reset_safe_programs(ha: HAClient, config: dict) -> None:
    cfg = config.get("safe_programs")
    if not cfg:
        return
    tpl = cfg["entity_template"]
    v = cfg["values"]
    log.info("Reasserting safe defaults on programs %s", cfg["indexes"])
    for i in cfg["indexes"]:
        ha.set_time(tpl["time"].format(i=i), v["time"])
        ha.set_number(tpl["power"].format(i=i), v["power"])
        ha.set_number(tpl["soc"].format(i=i), v["soc"])
        ha.set_select(tpl["charging"].format(i=i), v["charging"])


# ── Commands ──────────────────────────────────────────────────────────────────


def cmd_fetch(config: dict) -> None:
    target = date.today() + timedelta(days=1)
    max_retries = config["okte"].get("fetch_retries", 3)
    retry_delay = config["okte"].get("fetch_retry_delay", 1800)

    prices = None
    for attempt in range(1, max_retries + 1):
        try:
            prices = fetch_okte_prices(target)
            break
        except ValueError as e:
            log.warning("Attempt %d/%d failed: %s", attempt, max_retries, e)
            if attempt < max_retries:
                log.info("Retrying in %d minutes...", retry_delay // 60)
                time.sleep(retry_delay)
    else:
        log.error("All %d attempts failed. Prices not available for %s.", max_retries, target)
        sys.exit(1)

    schedule = build_schedule(prices)
    save_schedule(schedule, target, SCHEDULE_NEXT_FILE)

    negatives = [p for p in schedule if p["price"] < 0]
    log.info(
        "Schedule for %s: %d negative slots, %d positive slots",
        target, len(negatives), len(schedule) - len(negatives),
    )
    if negatives:
        tz_label = datetime.now(LOCAL_TZ).strftime("%Z")
        log.info(
            "Negative windows (%s): %s",
            tz_label,
            ", ".join(
                f"{fmt_local_hhmm(p['delivery_start'])}-{fmt_local_hhmm(p['delivery_end'])} ({p['price']:.2f})"
                for p in negatives
            ),
        )

    ha = HAClient(config["ha"]["url"], config["ha"]["token"])
    reset_safe_programs(ha, config)


def cmd_apply(config: dict) -> None:
    # Cron fires 15 s before each slot boundary; aim 30 s ahead so we land
    # comfortably inside the upcoming slot.
    target_time = datetime.now(timezone.utc) + timedelta(seconds=30)

    data = active_schedule_for(target_time)
    if data is None:
        active = load_schedule(SCHEDULE_FILE)
        pending = load_schedule(SCHEDULE_NEXT_FILE)
        log.error(
            "No schedule covers %s (active=%s, next=%s). Run 'fetch'.",
            target_time.isoformat(),
            active["delivery_day"] if active else "missing",
            pending["delivery_day"] if pending else "missing",
        )
        sys.exit(1)

    period = get_period_for_time(data["schedule"], target_time)

    ha = HAClient(config["ha"]["url"], config["ha"]["token"])
    deye = config["deye"]
    program = config["program"]
    floor = program["target_soc_floor"]
    pv_threshold = program.get("pv_detection_threshold", 100)

    slot_label = (
        f"Slot {period['period']} {period['delivery_start'][11:16]}-"
        f"{period['delivery_end'][11:16]} price={period['price']:.2f}"
    )

    if period["price"] < 0:
        mode_name = "negative"
        target_soc = 100
        log.info("%s → NEGATIVE | program SOC=100 (PV-only charge)", slot_label)
    else:
        more_negs = has_remaining_negatives_today(target_time, data["schedule"])
        pv_power = float(ha.get_state(deye["pv_power"])["state"]) if more_negs else 0.0
        pv_up = pv_power > pv_threshold

        if more_negs and pv_up:
            mode_name = "positive_export"
            current_soc = float(ha.get_state(deye["battery_soc"])["state"])
            target_soc = max(current_soc, floor)
            log.info(
                "%s → POSITIVE_EXPORT | pv=%.0fW negs_ahead=True | battery=%.0f%% floor=%d → program SOC=%.0f",
                slot_label, pv_power, current_soc, floor, target_soc,
            )
        else:
            mode_name = "positive_self_consume"
            target_soc = floor
            log.info(
                "%s → POSITIVE_SELF_CONSUME | negs_ahead=%s pv=%.0fW | program SOC=%d",
                slot_label, more_negs, pv_power, target_soc,
            )

    # Program 6 stays "always-on" (time = 00:00:00); only its SOC changes per slot.
    # On this firmware, the highest-numbered program wins the tiebreak when all
    # programs share start time 00:00:00, so Program 6 is the active one.
    ha.set_time(deye["program_6_time"], "00:00:00")
    ha.set_number(deye["program_6_power"], program["discharge_power"])
    ha.set_number(deye["program_6_soc"], target_soc)
    ha.set_select(deye["program_6_charging"], "Disabled")

    apply_mode(ha, config, mode_name)
    log.info("Done.")


def cmd_status(_config: dict) -> None:
    now = datetime.now(timezone.utc)
    tz_label = datetime.now(LOCAL_TZ).strftime("%Z")
    for label, path in [("ACTIVE", SCHEDULE_FILE), ("NEXT", SCHEDULE_NEXT_FILE)]:
        data = load_schedule(path)
        if not data:
            print(f"=== {label} ===  (no file)")
            print()
            continue
        generated_local = (
            datetime.fromisoformat(data["generated_at"])
            .astimezone(LOCAL_TZ)
            .strftime("%Y-%m-%d %H:%M:%S %Z")
        )
        print(f"=== {label} schedule for {data['delivery_day']} (generated {generated_local}) ===")
        print(f"All times shown in {tz_label} ({LOCAL_TZ.key})")
        print(f"{'Period':>6}  {'Start':>5}  {'End':>5}  {'Price':>10}  Mode")
        print("-" * 78)
        for p in data["schedule"]:
            start = datetime.fromisoformat(p["delivery_start"])
            end = datetime.fromisoformat(p["delivery_end"])
            if p["price"] < 0:
                mode = "NEGATIVE (charge to 100)"
            else:
                slot_start_utc = datetime.fromisoformat(p["delivery_start"])
                negs_after = has_remaining_negatives_today(slot_start_utc, data["schedule"])
                mode = (
                    "positive_export | self_consume (PV-dependent)"
                    if negs_after
                    else "positive_self_consume"
                )
            marker = " ◄ NOW" if start <= now < end else ""
            print(f"{p['period']:>6}  {fmt_local_hhmm(p['delivery_start'])}  {fmt_local_hhmm(p['delivery_end'])}  {p['price']:>8.2f} €  {mode}{marker}")
        print()


# ── Entry point ───────────────────────────────────────────────────────────────


COMMANDS = {"fetch": cmd_fetch, "apply": cmd_apply, "status": cmd_status}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print(f"Available commands: {', '.join(COMMANDS)}")
        sys.exit(1)

    config = load_config()
    COMMANDS[sys.argv[1]](config)


if __name__ == "__main__":
    main()
