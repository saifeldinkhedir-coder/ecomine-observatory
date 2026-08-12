"""
EcoMine Observatory — Stage 5b: ingest Stage 2 output into the monitoring store.

WHAT THIS IS FOR
----------------
Stage 2 computes indicators for one site and one pair of epochs and writes them
to a provenance JSON. Stage 4 (`monitoring.py`) holds a time series, per-site
baselines, and an alert engine with consecutive-breach confirmation and a
cooldown. Until now nothing joined the two: the alert engine was tested but had
no real data flowing into it.

This module is that join. It reads a Stage 2 JSON, turns each indicator into an
`Observation`, and passes it through the alert engine. Run it after each Stage 2
run and the monitoring database accumulates a real series.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not schedule anything, and it does not send email. Scheduling belongs to
the operating system (cron, Task Scheduler) or to a GEE scheduled task, and mail
needs an SMTP account and credentials that must never live in this repository.
`alerts_pending()` exposes the alerts so an operator's own script can deliver
them however it wants. Pretending to ship a mail sender that has never sent mail
would be exactly the kind of untested claim this project avoids.

HONESTY NOTES CARRIED THROUGH
-----------------------------
An indicator that Stage 2 marked NOT APPLICABLE or INSUFFICIENT DATA is stored
with that status and a null value; it is never coerced to zero. A zero would be
a claim that nothing changed, when the truth is that nothing was measurable. The
alert engine already refuses to test a null value.

MEI is ingested but is NOT alertable by default. It is a weighted composite with
hand-chosen weights, and its component set can change between runs (a 3-of-3 and
a 4-of-4 MEI are different quantities). Alerting on a number whose definition
can shift between observations would produce alerts that mean nothing. MEI is
recorded for the record; alerts come from the component indicators.

RUN
---
    python ingest.py --json ecomine_stage2_indicators.json
    python ingest.py --json ecomine_stage2_indicators.json --site kangala
    python ingest.py --list-alerts
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from monitoring import (
    AlertEngine,
    MonitoringStore,
    Observation,
    establish_baseline,
    summarise,
)

# ==============================================================================
# CONFIGURATION
# ==============================================================================

# Indicators that may raise an alert. MEI is excluded on purpose: see the module
# docstring. This is an editorial choice and is declared as one.
ALERTABLE = ("VLI", "SEI", "WDI", "RDI")

# Where each indicator keeps its area figure in the Stage 2 JSON. Stage 2 uses a
# different key per indicator, so a single lookup would silently miss most of
# them and store area_ha=None everywhere.
AREA_KEYS = (
    "hectares_lost",        # VLI
    "hectares_exposed",     # SEI
    "water_hectares_gained",  # WDI
    "hectares_disturbed",   # RDI
)

MIN_OBS_FOR_BASELINE = 5


# ==============================================================================
# READING STAGE 2 OUTPUT
# ==============================================================================

def load_stage2(path: str) -> dict:
    """Read a Stage 2 provenance JSON, failing loudly if it is not one."""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{path} not found. Run ecomine_stage2.py first; it writes this file."
        )
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)

    if "indicators" not in doc:
        raise ValueError(
            f"{path} has no 'indicators' block. This does not look like Stage 2 "
            "output. Ingesting an unrecognised file would put junk in the "
            "monitoring database."
        )
    return doc


def site_key_from(doc: dict, override: Optional[str] = None) -> str:
    """
    Stage 2 records the site's display name and coordinates but not always the
    registry key, so the caller can pass one. Without a stable key the series
    for one site would split across several names and every baseline would be
    computed from too few points.
    """
    if override:
        return override
    site = doc.get("site", {})
    for k in ("key", "site_key"):
        if site.get(k):
            return site[k]
    name = site.get("name")
    if not name:
        raise ValueError(
            "No site key or name in the JSON. Pass --site explicitly so the "
            "series is filed under a stable key."
        )
    # Deterministic fallback: a slug of the display name.
    return name.lower().replace(" ", "_").replace("-", "_")[:40]


def observed_at_from(doc: dict) -> str:
    """
    Use the END of the current imagery window, not the run time. Two runs of the
    same epochs on different days describe the same observation; filing them
    under wall-clock dates would fabricate a change in the series.
    """
    cur = doc.get("epochs", {}).get("current", {})
    end = cur.get("end")
    if not end:
        raise ValueError(
            "No current-epoch end date in the JSON; cannot date this "
            "observation without inventing one."
        )
    return end


def area_of(ind: dict) -> Optional[float]:
    for k in AREA_KEYS:
        if ind.get(k) is not None:
            return ind[k]
    return None


def to_observations(doc: dict, site_key: str,
                    provenance_path: str) -> list[Observation]:
    """Turn every indicator in a Stage 2 JSON into an Observation."""
    observed_at = observed_at_from(doc)
    out = []
    for name, ind in doc["indicators"].items():
        status = ind.get("status", "UNKNOWN")
        # A non-OK indicator keeps a null value. Coercing to 0.0 would assert
        # "no change" where the truth is "not measurable".
        value = ind.get("value") if status == "OK" else None
        out.append(Observation(
            site_key=site_key,
            indicator=name,
            observed_at=observed_at,
            value=value,
            status=status,
            area_ha=area_of(ind) if status == "OK" else None,
            provenance_path=provenance_path,
            notes=ind.get("reason") or None,
        ))
    return out


# ==============================================================================
# INGEST
# ==============================================================================

def ingest(json_path: str, db_path: str,
           site_override: Optional[str] = None,
           quiet: bool = False) -> dict:
    """
    Read one Stage 2 JSON into the monitoring store and run the alert engine
    over the alertable indicators. Returns a summary dict.
    """
    doc = load_stage2(json_path)
    site_key = site_key_from(doc, site_override)
    observations = to_observations(doc, site_key, os.path.abspath(json_path))

    store = MonitoringStore(db_path)
    engine = AlertEngine(store)

    results, skipped = {}, {}
    try:
        for obs in observations:
            if obs.indicator not in ALERTABLE:
                # Still recorded, never alerted. MEI lands here.
                store.record(obs)
                skipped[obs.indicator] = "recorded, not alertable"
                continue
            test = engine.process(obs)
            results[obs.indicator] = test

        # A baseline can only be established once there are enough points. This
        # is why the first few runs produce no alerts and should not: an alert
        # against a baseline of one observation is noise.
        baselines = {}
        for ind in ALERTABLE:
            hist = store.history(site_key, ind)
            usable = [h for h in hist if h.value is not None]
            if len(usable) >= MIN_OBS_FOR_BASELINE:
                existing = store.get_baseline(site_key, ind)
                if existing is None or not existing.frozen:
                    b = establish_baseline(store, site_key, ind)
                    if b:
                        baselines[ind] = b
    finally:
        store.close()

    summary = {
        "site_key": site_key,
        "observed_at": observations[0].observed_at if observations else None,
        "ingested": len(observations),
        "tested": {k: v.level.value for k, v in results.items()},
        "reasons": {k: v.reason for k, v in results.items()},
        "not_alertable": skipped,
        "baselines_updated": list(baselines),
        "db": os.path.abspath(db_path),
    }

    if not quiet:
        _print_summary(summary, results)
    return summary


def _print_summary(summary: dict, results: dict) -> None:
    print("\nEcoMine Observatory — ingest into monitoring store")
    print(f"Site        : {summary['site_key']}")
    print(f"Observed at : {summary['observed_at']}")
    print(f"Indicators  : {summary['ingested']} recorded")
    print("=" * 66)
    if results:
        for ind, test in results.items():
            lvl = test.level.value
            print(f"  {ind:<5} {lvl:<12} {test.reason}")
    else:
        print("  no alertable indicator produced a testable value")
    for ind, why in summary["not_alertable"].items():
        print(f"  {ind:<5} {'—':<12} {why}")
    print("=" * 66)
    if summary["baselines_updated"]:
        print(f"baselines updated: {', '.join(summary['baselines_updated'])}")
    else:
        print(f"no baseline yet (needs >= {MIN_OBS_FOR_BASELINE} usable "
              "observations per indicator)")
    print(f"database: {summary['db']}")
    print("\nThis is a screening series over an AOI containing a mine. An alert "
          "means the site deviated from its own history, not that any operator "
          "did anything.")


# ==============================================================================
# ALERT READOUT (for an operator's own delivery script)
# ==============================================================================

def alerts_pending(db_path: str, site_key: Optional[str] = None) -> list[dict]:
    """
    Return the most recent alert per site/indicator. Deliberately returns data
    rather than sending anything: delivery needs credentials that must not live
    in this repository.
    """
    store = MonitoringStore(db_path)
    out = []
    try:
        cur = store.conn.execute(
            "SELECT site_key, indicator, observed_at, level, reason "
            "FROM alerts ORDER BY observed_at DESC")
        seen = set()
        for row in cur.fetchall():
            key = (row[0], row[1])
            if key in seen:
                continue
            seen.add(key)
            if site_key and row[0] != site_key:
                continue
            out.append({"site_key": row[0], "indicator": row[1],
                        "observed_at": row[2], "level": row[3],
                        "reason": row[4]})
    finally:
        store.close()
    return out


def series_summary(db_path: str, site_key: str) -> dict:
    """Per-indicator summary of the stored series for one site."""
    store = MonitoringStore(db_path)
    try:
        return {ind: summarise(store, site_key, ind) for ind in ALERTABLE}
    finally:
        store.close()


# ==============================================================================
# CLI
# ==============================================================================

def main():
    p = argparse.ArgumentParser(
        description="Ingest Stage 2 indicator output into the monitoring store")
    p.add_argument("--json", default="ecomine_stage2_indicators.json",
                   help="Stage 2 provenance JSON to ingest")
    p.add_argument("--db", default="ecomine_monitor.db")
    p.add_argument("--site", default=None,
                   help="registry site key; use it so repeated runs file under "
                        "one stable key")
    p.add_argument("--list-alerts", action="store_true",
                   help="print stored alerts and exit (no ingest)")
    p.add_argument("--summary", metavar="SITE_KEY", default=None,
                   help="print the stored series summary for a site and exit")
    a = p.parse_args()

    if a.list_alerts:
        alerts = alerts_pending(a.db)
        if not alerts:
            print("no alerts stored")
        for al in alerts:
            print(f"{al['observed_at']}  {al['site_key']:<16} "
                  f"{al['indicator']:<5} {al['level']:<10} {al['reason']}")
        return

    if a.summary:
        for ind, s in series_summary(a.db, a.summary).items():
            print(f"{ind}: {s}")
        return

    try:
        ingest(a.json, a.db, a.site)
    except (FileNotFoundError, ValueError) as e:
        print(f"ABORT: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
