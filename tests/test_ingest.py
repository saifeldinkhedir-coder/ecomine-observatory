"""
Tests for the Stage 2 -> monitoring bridge (ingest.py).

These lock in the honesty behaviours that matter: a non-computable indicator
must never become a number, a composite with shifting components must never
raise an alert, and unrecognised input must be refused rather than stored.
"""

import json

import pytest

from ingest import (
    ALERTABLE,
    area_of,
    ingest,
    alerts_pending,
    observed_at_from,
    site_key_from,
    to_observations,
)


def make_doc(date="2025-09-28", vli=0.02, sei=0.011, wdi=0.010,
             vli_status="OK", site_name="Kangala Coal Mine"):
    return {
        "tool": "EcoMine Observatory — Stage 2",
        "site": {"name": site_name, "country": "South Africa",
                 "lat": -26.188, "lon": 28.670, "buffer_km": 4.0},
        "epochs": {
            "baseline": {"start": "2016-08-01", "end": "2016-09-28"},
            "current": {"start": f"{date[:4]}-08-01", "end": date},
        },
        "indicators": {
            "VLI": {"indicator": "VLI", "status": vli_status,
                    "value": vli if vli_status == "OK" else None,
                    "hectares_lost": 125.3 if vli_status == "OK" else None,
                    "reason": None if vli_status == "OK" else "below NDVI floor"},
            "SEI": {"indicator": "SEI", "status": "OK", "value": sei,
                    "hectares_exposed": 58.2},
            "WDI": {"indicator": "WDI", "status": "OK", "value": wdi,
                    "water_hectares_gained": 48.2},
            "MEI": {"indicator": "MEI", "status": "OK", "value": 0.0137},
        },
    }


@pytest.fixture
def tmp_env(tmp_path):
    jf = tmp_path / "s2.json"
    db = tmp_path / "m.db"
    return str(jf), str(db)


# --- reading -----------------------------------------------------------------

def test_missing_file_is_refused(tmp_env):
    _, db = tmp_env
    with pytest.raises(FileNotFoundError):
        ingest("definitely_missing.json", db, quiet=True)


def test_unrecognised_json_is_refused(tmp_env):
    """Junk must not be silently written into the monitoring database."""
    jf, db = tmp_env
    with open(jf, "w") as fh:
        json.dump({"something": "else"}, fh)
    with pytest.raises(ValueError):
        ingest(jf, db, quiet=True)


def test_observed_at_is_imagery_window_not_run_time():
    """Two runs of the same epochs describe one observation, not two."""
    assert observed_at_from(make_doc("2024-09-28")) == "2024-09-28"


def test_missing_epoch_end_is_refused():
    doc = make_doc()
    doc["epochs"]["current"].pop("end")
    with pytest.raises(ValueError):
        observed_at_from(doc)


def test_site_key_override_wins():
    assert site_key_from(make_doc(), "kangala") == "kangala"


def test_site_key_falls_back_to_slug():
    key = site_key_from(make_doc(site_name="Kangala Coal Mine"))
    assert " " not in key and key.startswith("kangala")


def test_site_key_without_name_is_refused():
    doc = make_doc()
    doc["site"] = {}
    with pytest.raises(ValueError):
        site_key_from(doc)


# --- the honesty rules -------------------------------------------------------

def test_not_applicable_is_null_not_zero():
    """
    A zero would assert 'no change'. The truth is 'not measurable'. This is the
    same distinction the VLI vegetation gate makes in Stage 2.
    """
    obs = to_observations(make_doc(vli_status="NOT APPLICABLE"), "k", "p.json")
    vli = next(o for o in obs if o.indicator == "VLI")
    assert vli.value is None
    assert vli.status == "NOT APPLICABLE"
    assert vli.area_ha is None


def test_insufficient_data_is_null_not_zero():
    obs = to_observations(make_doc(vli_status="INSUFFICIENT DATA"), "k", "p")
    vli = next(o for o in obs if o.indicator == "VLI")
    assert vli.value is None


def test_mei_is_never_alertable():
    """
    MEI weights are hand-chosen and its component set can change between runs,
    so a 3-of-3 and a 4-of-4 MEI are different quantities. Alerting on it would
    produce alerts whose meaning shifts.
    """
    assert "MEI" not in ALERTABLE


def test_mei_is_still_recorded(tmp_env):
    jf, db = tmp_env
    with open(jf, "w") as fh:
        json.dump(make_doc(), fh)
    summary = ingest(jf, db, site_override="k", quiet=True)
    assert "MEI" in summary["not_alertable"]
    assert "MEI" not in summary["tested"]
    assert summary["ingested"] == 4


def test_area_lookup_covers_every_indicator():
    """
    Each indicator stores its area under a different key. A single lookup would
    silently record area_ha=None for most of them.
    """
    assert area_of({"hectares_lost": 1.0}) == 1.0
    assert area_of({"hectares_exposed": 2.0}) == 2.0
    assert area_of({"water_hectares_gained": 3.0}) == 3.0
    assert area_of({"hectares_disturbed": 4.0}) == 4.0
    assert area_of({"value": 0.5}) is None


# --- the alert chain ---------------------------------------------------------

def _ingest_series(jf, db, values, site="k"):
    """Ingest a series of (date, vli, sei) and return the last summary."""
    last = None
    for date, vli, sei in values:
        with open(jf, "w") as fh:
            json.dump(make_doc(date=date, vli=vli, sei=sei), fh)
        last = ingest(jf, db, site_override=site, quiet=True)
    return last


def test_no_baseline_reports_insufficient_not_all_clear(tmp_env):
    """
    With no baseline the engine must say INSUFFICIENT_DATA, not NONE. NONE
    would mean "tested, nothing wrong"; the truth is "cannot test yet". The
    same distinction the indicators themselves make.
    """
    jf, db = tmp_env
    summary = _ingest_series(jf, db, [("2020-09-28", 0.02, 0.011)])
    assert summary["baselines_updated"] == []
    assert all(v == "INSUFFICIENT_DATA" for v in summary["tested"].values())
    assert alerts_pending(db) == []


def test_baseline_forms_then_single_breach_is_only_watch(tmp_env):
    """One outlier must not raise an alert; confirmation is required."""
    jf, db = tmp_env
    stable = [(f"{y}-09-28", 0.020, 0.011) for y in range(2018, 2024)]
    _ingest_series(jf, db, stable)
    jump = _ingest_series(jf, db, [("2024-09-28", 0.20, 0.15)])
    assert jump["tested"]["VLI"] == "WATCH"
    assert alerts_pending(db) == []


def test_two_consecutive_breaches_raise_and_persist_an_alert(tmp_env):
    jf, db = tmp_env
    stable = [(f"{y}-09-28", 0.020, 0.011) for y in range(2018, 2024)]
    _ingest_series(jf, db, stable)
    _ingest_series(jf, db, [("2024-09-28", 0.20, 0.15)])
    second = _ingest_series(jf, db, [("2025-09-28", 0.22, 0.16)])
    assert second["tested"]["VLI"] == "ALERT"
    stored = alerts_pending(db)
    assert any(a["indicator"] == "VLI" and a["level"] == "ALERT"
               for a in stored)


def test_unbreached_indicator_stays_quiet(tmp_env):
    """WDI is left stable throughout and must never fire."""
    jf, db = tmp_env
    stable = [(f"{y}-09-28", 0.020, 0.011) for y in range(2018, 2024)]
    _ingest_series(jf, db, stable)
    out = _ingest_series(jf, db, [("2024-09-28", 0.20, 0.15)])
    assert out["tested"]["WDI"] == "NONE"


def test_alerts_pending_filters_by_site(tmp_env):
    jf, db = tmp_env
    stable = [(f"{y}-09-28", 0.020, 0.011) for y in range(2018, 2024)]
    _ingest_series(jf, db, stable, site="site_a")
    _ingest_series(jf, db, [("2024-09-28", 0.20, 0.15)], site="site_a")
    _ingest_series(jf, db, [("2025-09-28", 0.22, 0.16)], site="site_a")
    assert alerts_pending(db, site_key="site_a")
    assert alerts_pending(db, site_key="site_b") == []
