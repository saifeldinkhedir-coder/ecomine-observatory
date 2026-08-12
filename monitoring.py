"""
EcoMine Observatory - continuous monitoring core (Stage 4)
===========================================================
Turns one-off snapshots into a tracked time series: a baseline per site, a
scheduled re-check, a change test, and an alert record.

This module is deliberately dependency-free (stdlib + sqlite3 only) so the
decision logic can be unit-tested without Earth Engine credentials. The GEE
side lives in `ecomine_monitor.py`, which calls into here.

WHY ALERTS ARE HARDER THAN THEY LOOK
------------------------------------
An alerting system that fires on every fluctuation is worse than no alerting:
people stop reading it, and the one real event arrives in a stream of noise.
Three guards are built in.

  1. SIGNIFICANCE. A change must exceed the site's own measured noise floor,
     not a number chosen in advance. The floor comes from the observed history
     of that site, so a stable desert site gets a tight test and a variable one
     gets a loose test, automatically.

  2. PERSISTENCE. A single anomalous observation is usually an artefact -
     residual cloud, an unusual view angle, a bad composite. By default a
     change must appear in two consecutive observations before it alerts.

  3. COOLDOWN. Mine expansion is a step change that persists for months. Once
     flagged, re-flagging the same standing condition every 10 days produces
     alert fatigue. A cooldown suppresses repeats unless the value moves
     materially again.

WHAT THIS MODULE WILL NOT DO
----------------------------
It stores and compares whatever indicator it is given. It does not know whether
that indicator is validated. Alerting on an UNVALIDATED screening layer
produces alerts nobody can defend - see the WARNING in AlertEngine.evaluate.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional


SCHEMA_VERSION = 1

#: Observations required before the noise floor is considered meaningful.
#: Below this the engine reports INSUFFICIENT_HISTORY rather than guessing.
MIN_HISTORY_FOR_BASELINE = 4

#: Consecutive breaching observations required to raise an alert.
DEFAULT_PERSISTENCE = 2

#: Days during which a repeat alert on the same standing condition is muted.
DEFAULT_COOLDOWN_DAYS = 60

#: Multiplier on the historical robust sigma. Matches the Stage 2 convention
#: so a "significant change" means the same thing everywhere in the project.
DEFAULT_K_SIGMA = 2.0

#: Relative move required for a new alert to escape cooldown.
COOLDOWN_ESCAPE_FRACTION = 0.5


class AlertLevel(str, Enum):
    NONE = "NONE"
    WATCH = "WATCH"            # breached once; not yet persistent
    ALERT = "ALERT"            # breached persistently
    INSUFFICIENT = "INSUFFICIENT_DATA"


@dataclass
class Observation:
    """One evaluation of one indicator at one site at one time."""
    site_key: str
    indicator: str
    observed_at: str           # ISO date of the imagery window end
    value: Optional[float]     # None when the indicator could not be computed
    status: str                # OK | NOT APPLICABLE | INSUFFICIENT DATA
    area_ha: Optional[float] = None
    provenance_path: Optional[str] = None
    notes: Optional[str] = None


@dataclass
class Baseline:
    site_key: str
    indicator: str
    n_observations: int
    median: float
    robust_sigma: float
    established_from: str      # ISO date of earliest observation used
    established_to: str
    frozen: bool = False       # a frozen baseline stops drifting with new data


@dataclass
class ChangeTest:
    level: AlertLevel
    value: Optional[float]
    baseline_median: Optional[float]
    threshold: Optional[float]
    deviation_sigmas: Optional[float]
    consecutive_breaches: int
    reason: str


# ==============================================================================
# STORE
# ==============================================================================

class MonitoringStore:
    """SQLite time-series store. PostGIS is the later upgrade; this is enough."""

    def __init__(self, path: str = "ecomine_monitor.db"):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._create()

    def _create(self):
        c = self.conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, value TEXT)""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_key TEXT NOT NULL,
                indicator TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                value REAL,
                status TEXT NOT NULL,
                area_ha REAL,
                provenance_path TEXT,
                notes TEXT,
                recorded_utc TEXT NOT NULL,
                UNIQUE(site_key, indicator, observed_at))""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS baselines (
                site_key TEXT NOT NULL,
                indicator TEXT NOT NULL,
                n_observations INTEGER NOT NULL,
                median REAL NOT NULL,
                robust_sigma REAL NOT NULL,
                established_from TEXT NOT NULL,
                established_to TEXT NOT NULL,
                frozen INTEGER NOT NULL DEFAULT 0,
                updated_utc TEXT NOT NULL,
                PRIMARY KEY (site_key, indicator))""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_key TEXT NOT NULL,
                indicator TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                level TEXT NOT NULL,
                value REAL,
                baseline_median REAL,
                threshold REAL,
                deviation_sigmas REAL,
                reason TEXT,
                raised_utc TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_obs ON observations"
                  "(site_key, indicator, observed_at)")
        c.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                  (str(SCHEMA_VERSION),))
        self.conn.commit()

    # -- observations ----------------------------------------------------

    def record(self, obs: Observation) -> bool:
        """Idempotent: re-running the same date does not duplicate a row."""
        try:
            self.conn.execute(
                """INSERT INTO observations
                   (site_key, indicator, observed_at, value, status, area_ha,
                    provenance_path, notes, recorded_utc)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (obs.site_key, obs.indicator, obs.observed_at, obs.value,
                 obs.status, obs.area_ha, obs.provenance_path, obs.notes,
                 datetime.now(timezone.utc).isoformat()))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def history(self, site_key: str, indicator: str,
                only_ok: bool = True) -> list[Observation]:
        q = ("SELECT * FROM observations WHERE site_key=? AND indicator=?"
             + (" AND status='OK'" if only_ok else "")
             + " ORDER BY observed_at ASC")
        return [Observation(
            site_key=r["site_key"], indicator=r["indicator"],
            observed_at=r["observed_at"], value=r["value"], status=r["status"],
            area_ha=r["area_ha"], provenance_path=r["provenance_path"],
            notes=r["notes"])
            for r in self.conn.execute(q, (site_key, indicator))]

    # -- baselines -------------------------------------------------------

    def save_baseline(self, b: Baseline):
        self.conn.execute(
            """INSERT OR REPLACE INTO baselines
               (site_key, indicator, n_observations, median, robust_sigma,
                established_from, established_to, frozen, updated_utc)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (b.site_key, b.indicator, b.n_observations, b.median,
             b.robust_sigma, b.established_from, b.established_to,
             int(b.frozen), datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    def get_baseline(self, site_key: str, indicator: str) -> Optional[Baseline]:
        r = self.conn.execute(
            "SELECT * FROM baselines WHERE site_key=? AND indicator=?",
            (site_key, indicator)).fetchone()
        if not r:
            return None
        return Baseline(
            site_key=r["site_key"], indicator=r["indicator"],
            n_observations=r["n_observations"], median=r["median"],
            robust_sigma=r["robust_sigma"],
            established_from=r["established_from"],
            established_to=r["established_to"], frozen=bool(r["frozen"]))

    # -- alerts ----------------------------------------------------------

    def raise_alert(self, site_key: str, indicator: str, observed_at: str,
                    t: ChangeTest) -> int:
        cur = self.conn.execute(
            """INSERT INTO alerts
               (site_key, indicator, observed_at, level, value,
                baseline_median, threshold, deviation_sigmas, reason,
                raised_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (site_key, indicator, observed_at, t.level.value, t.value,
             t.baseline_median, t.threshold, t.deviation_sigmas, t.reason,
             datetime.now(timezone.utc).isoformat()))
        self.conn.commit()
        return cur.lastrowid

    def last_alert(self, site_key: str, indicator: str) -> Optional[dict]:
        r = self.conn.execute(
            """SELECT * FROM alerts WHERE site_key=? AND indicator=?
               AND level=? ORDER BY observed_at DESC LIMIT 1""",
            (site_key, indicator, AlertLevel.ALERT.value)).fetchone()
        return dict(r) if r else None

    def close(self):
        self.conn.close()


# ==============================================================================
# BASELINE
# ==============================================================================

def robust_sigma(values: list[float]) -> float:
    """
    (p84 - p16) / 2 — the same robust estimator used in Stage 2, so that
    "significant" carries one meaning across the whole project. Robust because
    the anomalies we want to detect are precisely the outliers that would
    inflate an ordinary standard deviation and raise the bar against
    themselves.
    """
    if len(values) < 2:
        return 0.0
    s = sorted(values)
    n = len(s)

    def pct(p):
        k = (n - 1) * p
        lo, hi = int(k), min(int(k) + 1, n - 1)
        return s[lo] + (s[hi] - s[lo]) * (k - lo)

    return (pct(0.84) - pct(0.16)) / 2.0


def establish_baseline(store: MonitoringStore, site_key: str,
                       indicator: str, freeze: bool = False) -> Optional[Baseline]:
    """
    Build a baseline from observed history. Returns None when history is too
    short — an explicit refusal, not a baseline built from two points.
    """
    hist = [o for o in store.history(site_key, indicator)
            if o.value is not None]
    if len(hist) < MIN_HISTORY_FOR_BASELINE:
        return None
    vals = [o.value for o in hist]
    b = Baseline(
        site_key=site_key, indicator=indicator, n_observations=len(vals),
        median=statistics.median(vals), robust_sigma=robust_sigma(vals),
        established_from=hist[0].observed_at,
        established_to=hist[-1].observed_at, frozen=freeze)
    store.save_baseline(b)
    return b


# ==============================================================================
# CHANGE TEST AND ALERTING
# ==============================================================================

def describe_magnitude(value: float, baseline_median: float,
                       deviation_sigmas: Optional[float]) -> str:
    """
    Express the size of a change in terms a person can act on.

    A sigma count is the right statistical test but a poor communication: where
    a site is very stable the sigma count explodes ("+284 sigma"), which is
    arithmetically correct and tells the reader nothing actionable. The
    relative change is what conveys scale, so lead with that and report the
    sigma count as a bounded qualifier.
    """
    parts = []
    if baseline_median:
        rel = (value - baseline_median) / abs(baseline_median) * 100
        parts.append(f"{rel:+.0f}% vs baseline median "
                     f"({baseline_median:.4f} -> {value:.4f})")
    else:
        parts.append(f"value {value:.4f} against a zero baseline")

    if deviation_sigmas is not None:
        if abs(deviation_sigmas) >= 10:
            parts.append("far outside the site's historical variability "
                         f"(>{10} sigma; the site is very stable, so the "
                         "sigma count is not a useful magnitude here)")
        else:
            parts.append(f"{deviation_sigmas:+.1f} sigma")
    return "; ".join(parts) + "."


class AlertEngine:
    """
    Applies significance, persistence and cooldown to decide whether an
    observation warrants an alert.

    ⚠️ WARNING — READ BEFORE ENABLING ALERTS IN PRODUCTION.
    This engine alerts on whatever indicator it is fed. It has no way to know
    whether that indicator has been validated. Alerting on the Stage 1
    unsupervised screening layer will produce alerts that cannot be defended:
    bare rock, wadi beds, quarries, roads and construction all move that
    indicator. Wire this to the Stage 3 supervised classifier output before any
    alert is sent to a person.
    """

    def __init__(self, store: MonitoringStore,
                 k_sigma: float = DEFAULT_K_SIGMA,
                 persistence: int = DEFAULT_PERSISTENCE,
                 cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
                 direction: str = "increase"):
        self.store = store
        self.k = k_sigma
        self.persistence = persistence
        self.cooldown_days = cooldown_days
        self.direction = direction   # increase | decrease | both

    def _breaches(self, value: float, b: Baseline) -> tuple[bool, float, float]:
        if b.robust_sigma <= 0:
            # A perfectly flat history gives no noise floor. Fall back to a
            # relative test rather than dividing by zero or alerting on any
            # movement at all.
            thr = b.median * 1.10 if b.median else 0.0
            return (value > thr, thr, 0.0)
        thr_up = b.median + self.k * b.robust_sigma
        thr_dn = b.median - self.k * b.robust_sigma
        dev = (value - b.median) / b.robust_sigma
        if self.direction == "increase":
            return (value > thr_up, thr_up, dev)
        if self.direction == "decrease":
            return (value < thr_dn, thr_dn, dev)
        return (value > thr_up or value < thr_dn,
                thr_up if value > b.median else thr_dn, dev)

    def _consecutive(self, site_key: str, indicator: str,
                     b: Baseline, up_to: str) -> tuple[int, Optional[str]]:
        """
        Count breaching observations ending at `up_to`, walking backwards, and
        return the date the run started. The start date anchors the cooldown.
        """
        hist = [o for o in self.store.history(site_key, indicator)
                if o.value is not None and o.observed_at <= up_to]
        n = 0
        start = None
        for o in reversed(hist):
            if self._breaches(o.value, b)[0]:
                n += 1
                start = o.observed_at
            else:
                break
        return n, start

    def _in_cooldown(self, site_key: str, indicator: str,
                     observed_at: str, value: float,
                     breach_run_start: Optional[str] = None) -> bool:
        """
        Suppress repeat alerts on a condition that is simply still standing.

        The cooldown clock runs from the START of the current breach run, not
        from the last alert. Measuring from the last alert lets a permanent
        step change re-alert once per cooldown period forever, which is the
        alert fatigue this guard exists to prevent. A mine that expands and
        stays expanded is ONE event, not one event every 60 days.

        Two ways out: the breach run ends and a new one begins later, or the
        value moves materially again (a second, larger expansion).
        """
        last = self.store.last_alert(site_key, indicator)
        if not last:
            return False

        # Has this breach run already been alerted on? The run began at
        # breach_run_start; if the last alert falls on or after that date, the
        # alert belongs to THIS run and the condition is merely still standing.
        # If the last alert predates the run, this is a genuinely new event and
        # cooldown does not apply.
        if breach_run_start and last["observed_at"] < breach_run_start:
            return False

        # Escape if the value has moved materially since the alert already
        # raised for this run — a mine that doubles during a standing
        # condition is a new event, not the same one.
        prev_val = last["value"]
        if prev_val:
            if abs(value - prev_val) / abs(prev_val) >= COOLDOWN_ESCAPE_FRACTION:
                return False

        # This run has already been alerted and the value has not moved
        # materially, so the condition is simply still standing. Suppress
        # indefinitely: a mine that expands and stays expanded is ONE event.
        # Elapsed time is deliberately NOT a release condition — releasing on
        # elapsed time would re-alert the same standing condition once per
        # cooldown period forever, which is the alert fatigue this guard
        # exists to prevent. The run ending, or the value moving materially,
        # are the only ways out.
        #
        # cooldown_days is retained for the case where the run start cannot be
        # determined (no history), where a time-boxed suppression is the safer
        # fallback than suppressing forever.
        if breach_run_start:
            return True
        try:
            anchor = datetime.fromisoformat(last["observed_at"])
            now = datetime.fromisoformat(observed_at)
        except ValueError:
            return True
        return (now - anchor) <= timedelta(days=self.cooldown_days)

    def evaluate(self, obs: Observation) -> ChangeTest:
        if obs.status != "OK" or obs.value is None:
            return ChangeTest(AlertLevel.INSUFFICIENT, obs.value, None, None,
                              None, 0,
                              f"Observation status is '{obs.status}'. No "
                              "change test performed; nothing is inferred.")

        b = self.store.get_baseline(obs.site_key, obs.indicator)
        if b is None:
            return ChangeTest(
                AlertLevel.INSUFFICIENT, obs.value, None, None, None, 0,
                f"No baseline. At least {MIN_HISTORY_FOR_BASELINE} usable "
                "observations are required before change can be tested.")

        breached, thr, dev = self._breaches(obs.value, b)
        if not breached:
            return ChangeTest(AlertLevel.NONE, obs.value, b.median, thr, dev, 0,
                              "Within the site's own measured variability.")

        n, run_start = self._consecutive(
            obs.site_key, obs.indicator, b, obs.observed_at)

        if n < self.persistence:
            return ChangeTest(
                AlertLevel.WATCH, obs.value, b.median, thr, dev, n,
                f"Threshold breached on {n} consecutive observation(s); "
                f"{self.persistence} required. A single excursion is more "
                "often an artefact than an event.")

        if self._in_cooldown(obs.site_key, obs.indicator, obs.observed_at,
                             obs.value, run_start):
            return ChangeTest(
                AlertLevel.WATCH, obs.value, b.median, thr, dev, n,
                f"Persistent breach, but this run began {run_start} and is "
                f"within the {self.cooldown_days}-day cooldown of an alert "
                "already raised for it. The condition is still standing, not "
                "newly detected. Suppressed to avoid alert fatigue.")

        return ChangeTest(
            AlertLevel.ALERT, obs.value, b.median, thr, dev, n,
            f"Persistent breach since {run_start}: {n} consecutive "
            f"observations above {thr:.5f}. "
            f"{describe_magnitude(obs.value, b.median, dev)}")

    def process(self, obs: Observation) -> ChangeTest:
        """Record the observation, test it, and persist any alert."""
        self.store.record(obs)
        t = self.evaluate(obs)
        if t.level == AlertLevel.ALERT:
            self.store.raise_alert(obs.site_key, obs.indicator,
                                   obs.observed_at, t)
        return t


# ==============================================================================
# REPORTING
# ==============================================================================

def summarise(store: MonitoringStore, site_key: str, indicator: str) -> dict:
    hist = store.history(site_key, indicator, only_ok=False)
    ok = [o for o in hist if o.value is not None]
    b = store.get_baseline(site_key, indicator)
    return {
        "site_key": site_key,
        "indicator": indicator,
        "n_observations": len(hist),
        "n_usable": len(ok),
        "n_unusable": len(hist) - len(ok),
        "first_observed": hist[0].observed_at if hist else None,
        "last_observed": hist[-1].observed_at if hist else None,
        "baseline": asdict(b) if b else None,
        "series": [{"date": o.observed_at, "value": o.value,
                    "status": o.status, "area_ha": o.area_ha} for o in hist],
    }


def export_series(store: MonitoringStore, site_key: str, indicator: str,
                  path: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summarise(store, site_key, indicator), fh,
                  indent=2, ensure_ascii=False)
    return path
