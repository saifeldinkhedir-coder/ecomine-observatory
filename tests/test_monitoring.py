"""
Tests for the continuous-monitoring decision logic.

No Earth Engine, no network. These test the rules that decide whether a person
gets woken up, which is exactly the logic that must not be wrong.
"""
import pytest

from monitoring import (
    AlertEngine, AlertLevel, MonitoringStore, Observation,
    MIN_HISTORY_FOR_BASELINE, establish_baseline, robust_sigma, summarise,
)


@pytest.fixture
def store(tmp_path):
    s = MonitoringStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def obs(date, value, status="OK", site="ad_duwayhi", ind="SEI"):
    return Observation(site_key=site, indicator=ind, observed_at=date,
                       value=value, status=status)


def seed(store, dates_values, **kw):
    for d, v in dates_values:
        store.record(obs(d, v, **kw))


# ==============================================================================
# ROBUST SIGMA
# ==============================================================================

class TestRobustSigma:
    def test_flat_series_zero(self):
        assert robust_sigma([0.05] * 10) == 0.0

    def test_single_value(self):
        assert robust_sigma([0.05]) == 0.0

    def test_not_inflated_by_one_outlier(self):
        """
        The point of a robust estimator: an anomaly must not raise the bar
        against its own detection.
        """
        clean = [0.05, 0.051, 0.049, 0.05, 0.052, 0.048, 0.05, 0.051]
        spiked = clean + [0.40]
        assert robust_sigma(spiked) < robust_sigma(clean) * 3


# ==============================================================================
# STORE
# ==============================================================================

class TestStore:
    def test_record_and_read_back(self, store):
        assert store.record(obs("2025-01-15", 0.048)) is True
        h = store.history("ad_duwayhi", "SEI")
        assert len(h) == 1 and h[0].value == 0.048

    def test_idempotent_same_date(self, store):
        """Re-running a scheduled task must not duplicate rows."""
        store.record(obs("2025-01-15", 0.048))
        assert store.record(obs("2025-01-15", 0.048)) is False
        assert len(store.history("ad_duwayhi", "SEI")) == 1

    def test_history_is_chronological(self, store):
        seed(store, [("2025-03-01", 0.05), ("2025-01-01", 0.04),
                     ("2025-02-01", 0.045)])
        dates = [o.observed_at for o in store.history("ad_duwayhi", "SEI")]
        assert dates == sorted(dates)

    def test_unusable_observations_excluded_by_default(self, store):
        seed(store, [("2025-01-01", 0.05)])
        store.record(obs("2025-01-15", None, "INSUFFICIENT DATA"))
        assert len(store.history("ad_duwayhi", "SEI")) == 1
        assert len(store.history("ad_duwayhi", "SEI", only_ok=False)) == 2

    def test_sites_and_indicators_isolated(self, store):
        seed(store, [("2025-01-01", 0.05)])
        seed(store, [("2025-01-01", 0.90)], site="witbank")
        assert store.history("ad_duwayhi", "SEI")[0].value == 0.05
        assert store.history("witbank", "SEI")[0].value == 0.90


# ==============================================================================
# BASELINE
# ==============================================================================

class TestBaseline:
    def test_refuses_short_history(self, store):
        """A baseline from two points is not a baseline."""
        seed(store, [("2025-01-01", 0.05), ("2025-01-15", 0.051)])
        assert establish_baseline(store, "ad_duwayhi", "SEI") is None

    def test_builds_at_minimum(self, store):
        seed(store, [(f"2025-0{i}-01", 0.05) for i in range(1, 5)])
        b = establish_baseline(store, "ad_duwayhi", "SEI")
        assert b is not None
        assert b.n_observations == MIN_HISTORY_FOR_BASELINE

    def test_ignores_unusable_observations(self, store):
        seed(store, [(f"2025-0{i}-01", 0.05) for i in range(1, 5)])
        store.record(obs("2025-05-01", None, "INSUFFICIENT DATA"))
        b = establish_baseline(store, "ad_duwayhi", "SEI")
        assert b.n_observations == 4


# ==============================================================================
# CHANGE TEST — the core
# ==============================================================================

class TestChangeTest:
    def _engine(self, store, **kw):
        seed(store, [("2025-01-01", 0.050), ("2025-01-15", 0.051),
                     ("2025-02-01", 0.049), ("2025-02-15", 0.050),
                     ("2025-03-01", 0.052), ("2025-03-15", 0.048)])
        establish_baseline(store, "ad_duwayhi", "SEI")
        return AlertEngine(store, **kw)

    def test_stable_value_no_alert(self, store):
        e = self._engine(store)
        t = e.evaluate(obs("2025-04-01", 0.0505))
        assert t.level == AlertLevel.NONE

    def test_no_baseline_is_insufficient_not_alert(self, store):
        """Absence of a baseline must never be read as absence of change."""
        e = AlertEngine(store)
        t = e.evaluate(obs("2025-04-01", 0.90))
        assert t.level == AlertLevel.INSUFFICIENT
        assert "No baseline" in t.reason

    def test_unusable_observation_is_insufficient(self, store):
        e = self._engine(store)
        t = e.evaluate(obs("2025-04-01", None, "INSUFFICIENT DATA"))
        assert t.level == AlertLevel.INSUFFICIENT
        assert "nothing is inferred" in t.reason

    def test_not_applicable_never_alerts(self, store):
        """A gated indicator (e.g. VLI in desert) must not trigger anything."""
        e = self._engine(store)
        t = e.evaluate(obs("2025-04-01", None, "NOT APPLICABLE"))
        assert t.level == AlertLevel.INSUFFICIENT

    def test_single_spike_is_watch_not_alert(self, store):
        """One excursion is more often an artefact than an event."""
        e = self._engine(store)
        t = e.process(obs("2025-04-01", 0.30))
        assert t.level == AlertLevel.WATCH
        assert t.consecutive_breaches == 1

    def test_persistent_breach_alerts(self, store):
        e = self._engine(store)
        assert e.process(obs("2025-04-01", 0.30)).level == AlertLevel.WATCH
        t = e.process(obs("2025-04-15", 0.31))
        assert t.level == AlertLevel.ALERT
        assert t.consecutive_breaches == 2
        assert t.deviation_sigmas > 2

    def test_breach_then_return_resets_counter(self, store):
        """A gap in the breach sequence must reset persistence."""
        e = self._engine(store)
        e.process(obs("2025-04-01", 0.30))
        e.process(obs("2025-04-15", 0.050))     # back to normal
        t = e.process(obs("2025-05-01", 0.30))
        assert t.level == AlertLevel.WATCH
        assert t.consecutive_breaches == 1

    def test_decrease_ignored_when_watching_increase(self, store):
        """SEI growth matters; a drop is not mine expansion."""
        e = self._engine(store, direction="increase")
        assert e.evaluate(obs("2025-04-01", 0.001)).level == AlertLevel.NONE

    def test_decrease_detected_in_both_mode(self, store):
        e = self._engine(store, direction="both")
        e.process(obs("2025-04-01", 0.001))
        assert e.process(obs("2025-04-15", 0.001)).level == AlertLevel.ALERT


# ==============================================================================
# COOLDOWN
# ==============================================================================

class TestCooldown:
    def _alerted(self, store, **kw):
        seed(store, [("2025-01-01", 0.050), ("2025-01-15", 0.051),
                     ("2025-02-01", 0.049), ("2025-02-15", 0.050)])
        establish_baseline(store, "ad_duwayhi", "SEI")
        e = AlertEngine(store, **kw)
        e.process(obs("2025-03-01", 0.30))
        assert e.process(obs("2025-03-15", 0.31)).level == AlertLevel.ALERT
        return e

    def test_standing_condition_suppressed(self, store):
        """
        Mine expansion persists for months. Re-alerting every 10 days on the
        same standing condition is how people learn to ignore alerts.
        """
        e = self._alerted(store)
        t = e.process(obs("2025-04-01", 0.315))
        assert t.level == AlertLevel.WATCH
        assert "cooldown" in t.reason.lower()

    def test_material_move_escapes_cooldown(self, store):
        """A mine that doubles during cooldown must not be silenced."""
        e = self._alerted(store)
        t = e.process(obs("2025-04-01", 0.65))
        assert t.level == AlertLevel.ALERT

    def test_standing_condition_stays_suppressed_over_time(self, store):
        """
        Elapsed time is NOT a release condition.

        This test originally asserted the opposite — that cooldown expires
        after N days. A realistic simulation showed that assumption re-alerts
        the same standing condition once per cooldown period forever, which is
        precisely the alert fatigue the guard exists to prevent. A mine that
        expands and stays expanded is ONE event.
        """
        e = self._alerted(store, cooldown_days=30)
        for date, val in [("2025-06-01", 0.32), ("2025-09-01", 0.325),
                          ("2026-01-01", 0.33)]:
            assert e.process(obs(date, val)).level == AlertLevel.WATCH

    def test_run_ends_then_new_breach_alerts_again(self, store):
        """The way out of suppression is the run ending, not time passing."""
        e = self._alerted(store, cooldown_days=30)
        e.process(obs("2025-04-01", 0.050))      # run ends
        e.process(obs("2025-04-15", 0.30))       # new run begins
        t = e.process(obs("2025-05-01", 0.31))   # persistent again
        assert t.level == AlertLevel.ALERT


# ==============================================================================
# EDGE CASES
# ==============================================================================

class TestEdgeCases:
    def test_zero_sigma_history_does_not_crash(self, store):
        """A perfectly flat history gives no noise floor. Must not divide by 0."""
        seed(store, [(f"2025-0{i}-01", 0.05) for i in range(1, 5)])
        establish_baseline(store, "ad_duwayhi", "SEI")
        e = AlertEngine(store)
        t = e.evaluate(obs("2025-05-01", 0.05))
        assert t.level == AlertLevel.NONE
        t2 = e.evaluate(obs("2025-05-01", 0.20))
        assert t2.level in (AlertLevel.WATCH, AlertLevel.ALERT)

    def test_frozen_baseline_flag_persists(self, store):
        seed(store, [(f"2025-0{i}-01", 0.05) for i in range(1, 5)])
        establish_baseline(store, "ad_duwayhi", "SEI", freeze=True)
        assert store.get_baseline("ad_duwayhi", "SEI").frozen is True

    def test_summary_counts_unusable(self, store):
        seed(store, [("2025-01-01", 0.05), ("2025-02-01", 0.051)])
        store.record(obs("2025-03-01", None, "INSUFFICIENT DATA"))
        s = summarise(store, "ad_duwayhi", "SEI")
        assert s["n_observations"] == 3
        assert s["n_usable"] == 2
        assert s["n_unusable"] == 1
