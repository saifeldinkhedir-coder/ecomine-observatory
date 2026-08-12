"""
Tests for the legal screening layer.

The point of this suite is one guarantee: the layer points a reader toward
frameworks and experts, and never toward a conclusion about compliance. Most of
these tests exist to make that guarantee fail loudly if a future edit erodes it,
including an edit to the framework text itself.
"""

import json

import pytest

from legal_screening import (
    FORBIDDEN_VERDICT_LANGUAGE,
    FRAMEWORKS,
    INDICATOR_FRAMEWORKS,
    SCREENING_DISCLAIMER,
    ScreeningFlag,
    VerdictLanguageError,
    assert_no_verdict_language,
    frameworks_for,
    screen,
    screen_indicator,
)


def doc(vli=0.0253, sei=0.0117, wdi=0.0106,
        vli_status="OK", country="South Africa"):
    return {
        "site": {"name": "Kangala Coal Mine", "country": country},
        "indicators": {
            "VLI": {"indicator": "VLI", "status": vli_status,
                    "value": vli if vli_status == "OK" else None,
                    "reason": None if vli_status == "OK" else "below NDVI floor"},
            "SEI": {"indicator": "SEI", "status": "OK", "value": sei},
            "WDI": {"indicator": "WDI", "status": "OK", "value": wdi},
            "MEI": {"indicator": "MEI", "status": "OK", "value": 0.0159},
        },
    }


# --- the guarantee ------------------------------------------------------------

def test_guard_catches_verdict_language():
    for bad in ("The operator violated Article 5.",
                "This site is non-compliant.",
                "The mine is in breach of its permit.",
                "This constitutes a violation of NEMA.",
                "Enforcement action is warranted.",
                "The operator failed to comply."):
        with pytest.raises(VerdictLanguageError):
            assert_no_verdict_language(bad)


def test_guard_allows_relevance_language():
    for ok in ("This change may be relevant to IFC PS6.",
               "Warrants expert review by an ecologist.",
               "A reviewer may wish to consult the National Water Act."):
        assert_no_verdict_language(ok)


@pytest.mark.parametrize("key", sorted(FRAMEWORKS))
def test_every_framework_entry_is_free_of_verdict_language(key):
    """
    The curated text is the most likely place for an accusatory phrasing to
    creep in, so it is checked field by field.
    """
    fw = FRAMEWORKS[key]
    for fieldname in ("name", "relevance", "reviewer", "reference"):
        assert_no_verdict_language(getattr(fw, fieldname), f"{key}.{fieldname}")


def test_disclaimer_itself_is_clean():
    assert_no_verdict_language(SCREENING_DISCLAIMER, "disclaimer")


def test_screening_output_is_checked_on_the_way_out():
    """screen() runs the guard over its own serialised result."""
    res = screen(doc())
    assert_no_verdict_language(json.dumps(res, ensure_ascii=False))


def test_result_declares_it_is_not_a_compliance_assessment():
    res = screen(doc())
    assert res["is_compliance_assessment"] is False
    assert "not a compliance assessment" in res["disclaimer"].lower()


def test_every_flag_declares_itself_not_a_verdict():
    res = screen(doc())
    assert res["flags"]
    for f in res["flags"]:
        assert f["is_verdict"] is False
        assert f["requires_expert_review"] is True
        assert "not a finding" in f["note"].lower()


def test_every_flag_names_a_human_reviewer():
    """A flag that does not say who should read it is an unfinished thought."""
    for f in screen(doc())["flags"]:
        assert f["reviewer"] and len(f["reviewer"]) > 3


def test_forbidden_list_is_not_empty():
    """Guard against someone emptying the list and silently disabling the check."""
    assert len(FORBIDDEN_VERDICT_LANGUAGE) > 10
    assert "violation" in FORBIDDEN_VERDICT_LANGUAGE


# --- thresholds and honesty ---------------------------------------------------

def test_thresholds_are_declared_arbitrary():
    for f in screen(doc())["flags"]:
        assert "ARBITRARY" in f["threshold_basis"]
    assert "hand-chosen" in screen(doc())["threshold_note"]


def test_below_threshold_produces_no_flag():
    res = screen(doc(vli=0.001, sei=0.001, wdi=0.0001))
    assert res["flag_count"] == 0


def test_no_flags_is_not_a_clean_bill_of_health(capsys):
    from legal_screening import print_screening
    print_screening(screen(doc(vli=0.001, sei=0.001, wdi=0.0001)))
    out = capsys.readouterr().out.lower()
    assert "not a statement that nothing is happening" in out
    assert "clean bill of health" in out


def test_unmeasured_indicator_produces_no_flag():
    """
    Absence of measurement must not become a signal in its own right.
    """
    res = screen(doc(vli_status="NOT APPLICABLE"))
    assert all(f["indicator"] != "VLI" for f in res["flags"])
    assert any(nm["indicator"] == "VLI"
               for nm in res["indicators_not_measured"])


def test_unmeasured_indicator_is_reported_not_hidden():
    res = screen(doc(vli_status="INSUFFICIENT DATA"))
    nm = [x for x in res["indicators_not_measured"] if x["indicator"] == "VLI"]
    assert nm and nm[0]["status"] == "INSUFFICIENT DATA"


def test_mei_is_never_screened():
    """
    A composite whose component set can change between runs would point a
    reviewer at a framework on the strength of an unstable definition.
    """
    assert "MEI" not in INDICATOR_FRAMEWORKS
    res = screen(doc())
    assert all(f["indicator"] != "MEI" for f in res["flags"])


# --- jurisdiction filtering ---------------------------------------------------

def test_national_frameworks_are_filtered_by_country():
    za = {f["framework_key"] for f in screen(doc(country="South Africa"))["flags"]}
    sa = {f["framework_key"] for f in screen(doc(country="Saudi Arabia"))["flags"]}
    assert "za_nema" in za and "za_nema" not in sa
    assert "sa_mining_law" in sa and "sa_mining_law" not in za


def test_international_frameworks_appear_for_any_country():
    for country in ("South Africa", "Saudi Arabia", "Sudan"):
        keys = {f["framework_key"] for f in screen(doc(country=country))["flags"]}
        assert "ifc_ps6" in keys


def test_sudan_is_screened_with_the_same_neutrality():
    """
    The tool takes no position on any actor. A Sudanese site screens exactly
    like any other.
    """
    res = screen(doc(country="Sudan"))
    assert res["flag_count"] > 0
    assert res["is_compliance_assessment"] is False
    assert_no_verdict_language(json.dumps(res, ensure_ascii=False))


def test_frameworks_for_unknown_indicator_is_empty():
    assert frameworks_for("NOT_AN_INDICATOR") == []


# --- structure ----------------------------------------------------------------

def test_screen_indicator_returns_flag_objects():
    flags = screen_indicator("SEI", {"status": "OK", "value": 0.05},
                             country="South Africa")
    assert flags and all(isinstance(f, ScreeningFlag) for f in flags)


def test_non_stage2_document_is_refused():
    with pytest.raises(ValueError):
        screen({"something": "else"})


def test_every_mapped_framework_key_exists():
    """A typo in the mapping would silently drop a framework at runtime."""
    for spec in INDICATOR_FRAMEWORKS.values():
        for key in spec["frameworks"]:
            assert key in FRAMEWORKS, f"unknown framework key: {key}"
