"""
Tests for the PDF report generator (report.py).

A PDF is the artefact that gets forwarded, printed and cited, often by someone
who never sees the JSON. So the properties worth locking in are not cosmetic:
a NOT APPLICABLE indicator must never render as a number, a partial composite
must carry its warning, and the no-attribution statement must be present. If a
future edit breaks one of those, the report becomes more dangerous than no
report at all.
"""

import json

import pytest

# report.py imports reportlab at module level, and these tests read the PDF back
# with pypdf. Skip cleanly when either is absent instead of aborting collection
# for the whole suite.
pytest.importorskip("reportlab", reason="reportlab generates the PDF")
pypdf = pytest.importorskip("pypdf", reason="pypdf reads the PDF back")

from report import (  # noqa: E402  (must follow the importorskip guards)
    area_label,
    build_report,
    fmt_area,
    fmt_value,
    safe,
    status_colour,
)


# --- fixtures ----------------------------------------------------------------

def make_doc(vli_status="OK", mei_status="OK"):
    vli_ok = vli_status == "OK"
    return {
        "tool": "EcoMine Observatory — Stage 2",
        "generated_utc": "2026-08-11T10:00:00+00:00",
        "site": {"name": "Kangala Coal Mine", "country": "South Africa",
                 "lat": -26.191217, "lon": 28.672936, "buffer_km": 2.0,
                 "coordinate_provenance": "Ten pit-outline points, visually confirmed."},
        "epochs": {
            "baseline": {"start": "2016-08-01", "end": "2016-09-28",
                         "rule": "end_of_dry_season"},
            "current": {"start": "2025-08-01", "end": "2025-09-28",
                        "rule": "end_of_dry_season"},
        },
        "sufficiency_current_epoch": {"verdict": "SUFFICIENT", "s2_scenes": 22,
                                      "s1_scenes": 9},
        "method": {"threshold_rule": "median + K_SIGMA * robust_sigma, per site",
                   "K_SIGMA": 2.0, "scale_m": 20,
                   "vli_applicability_gate_ndvi": 0.15,
                   "mei_weights": {"VLI": 0.3333, "SEI": 0.3333, "WDI": 0.3333}},
        "indicators": {
            "VLI": ({"indicator": "VLI", "status": "OK", "value": 0.0253,
                     "hectares_lost": 125.33}
                    if vli_ok else
                    {"indicator": "VLI", "status": "NOT APPLICABLE",
                     "reason": "mean baseline NDVI 0.143 is below the floor of 0.15"}),
            "SEI": {"indicator": "SEI", "status": "OK", "value": 0.0156,
                    "hectares_exposed": 19.33,
                    "interpretation": "Bare-surface index rose beyond the noise floor."},
            "WDI": {"indicator": "WDI", "status": "OK", "value": 0.0093,
                    "water_hectares_gained": 11.3,
                    "interpretation": "Total water-area change."},
            "MEI": {"indicator": "MEI", "status": mei_status, "value": 0.0124,
                    "components_included": {"SEI": 0.0156, "WDI": 0.0093},
                    "components_excluded": ({"VLI": "NOT APPLICABLE"}
                                            if mei_status == "PARTIAL" else {}),
                    "weights_renormalised": mei_status == "PARTIAL",
                    "caveat": "Equal weighting is an arbitrary editorial choice."},
        },
        "warnings": [],
        "limitations": [
            "Change inside an AOI containing a mine is NOT attributed to mining.",
            "MEI weights are an arbitrary editorial choice.",
            "No legal or compliance conclusion is expressed or implied.",
        ],
    }


def render(tmp_path, doc, name="r.pdf"):
    jf = tmp_path / "s2.json"
    jf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / name
    build_report(str(jf), str(out))
    return out


def text_of(pdf_path):
    reader = pypdf.PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() for page in reader.pages)


# --- formatting helpers ------------------------------------------------------

def test_not_applicable_never_formats_as_a_number():
    """The whole point of the gate: absence of a value, not a value of zero."""
    out = fmt_value({"status": "NOT APPLICABLE"})
    assert "0" not in out
    assert "NOT APPLICABLE" in out.upper()


def test_insufficient_data_never_formats_as_a_number():
    out = fmt_value({"status": "INSUFFICIENT DATA"})
    assert "0" not in out


def test_ok_value_is_rendered():
    assert "0.0156" in fmt_value({"status": "OK", "value": 0.0156})


def test_area_is_dash_when_absent():
    assert fmt_area({"status": "OK", "value": 0.01}) == "—"


def test_area_label_follows_the_area_key_present():
    """
    Each indicator stores its area under a different key, so the label is keyed
    off which area field is actually present, not off the indicator name. With
    no area field there is no label to give.
    """
    assert "lost" in area_label({"hectares_lost": 1.0}).lower()
    assert "exposed" in area_label({"hectares_exposed": 1.0}).lower()
    assert area_label({"indicator": "VLI"}) == ""


def test_status_colour_distinguishes_states():
    assert status_colour("OK") != status_colour("NOT APPLICABLE")
    assert status_colour("INSUFFICIENT DATA") != status_colour("OK")


def test_safe_returns_default_for_missing_path():
    assert safe({"a": {"b": 1}}, "a", "z", default="fallback") == "fallback"
    assert safe({"a": {"b": 1}}, "a", "b") == 1


# --- the rendered PDF --------------------------------------------------------

def test_report_builds_a_readable_pdf(tmp_path):
    out = render(tmp_path, make_doc())
    assert out.exists() and out.stat().st_size > 2000
    assert "EcoMine Observatory" in text_of(out)


def test_no_attribution_statement_is_always_present(tmp_path):
    """
    The single most important line in the document. A screening figure that
    travels without it can be read as an accusation.
    """
    body = text_of(render(tmp_path, make_doc()))
    assert "not attributed" in body.lower()
    assert "compliance" in body.lower()


def test_not_applicable_reaches_the_pdf_as_words_not_zero(tmp_path):
    body = text_of(render(tmp_path, make_doc(vli_status="NOT APPLICABLE")))
    assert "NOT APPLICABLE" in body
    # the explanatory sentence must survive too
    assert "not a value of zero" in body.lower()


def test_partial_mei_carries_its_warning(tmp_path):
    """
    A 2-of-3 MEI is a different quantity from a 3-of-3 one. Without the warning
    a reader would compare them directly.
    """
    body = text_of(render(tmp_path, make_doc(vli_status="NOT APPLICABLE",
                                             mei_status="PARTIAL")))
    assert "PARTIAL" in body
    assert "different quantity" in body.lower()


def test_arbitrary_weights_are_declared(tmp_path):
    body = text_of(render(tmp_path, make_doc()))
    assert "arbitrary" in body.lower()


def test_limitations_are_present(tmp_path):
    body = text_of(render(tmp_path, make_doc()))
    assert "Limitations" in body
    assert "unsurveyed" in body.lower() or "not attributed" in body.lower()


def test_coordinate_provenance_is_carried(tmp_path):
    """A coordinate without its provenance is an unfalsifiable claim."""
    body = text_of(render(tmp_path, make_doc()))
    assert "visually confirmed" in body.lower()


def test_report_states_it_does_not_recompute(tmp_path):
    """
    The PDF renders a JSON; it must not look like an independent calculation.
    """
    body = text_of(render(tmp_path, make_doc()))
    assert "without recomputation" in body.lower()


def test_missing_json_is_refused(tmp_path):
    with pytest.raises((FileNotFoundError, OSError)):
        build_report(str(tmp_path / "nope.json"), str(tmp_path / "o.pdf"))
