"""
EcoMine Observatory - Streamlit interface
==========================================
Wraps Stages 1-4 behind a UI a non-coder can use.

DESIGN PRINCIPLE THAT SHAPES EVERY SCREEN
-----------------------------------------
The honesty layer must be visible in the interface, not buried in a JSON file
nobody opens. A user who reads only this UI must come away knowing:

    - what the number is
    - that it is a SCREENING signal, not a detection
    - that change inside the AOI is NOT attributed to mining
    - which thresholds were chosen by hand rather than derived
    - where the data was insufficient

Numbers are never shown without their status. An indicator that returns
NOT APPLICABLE or INSUFFICIENT DATA displays that verdict with its reason,
never a blank or a zero.

RUN
---
    pip install streamlit
    streamlit run app.py
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="EcoMine Observatory", page_icon="⛏",
                   layout="wide", initial_sidebar_state="expanded")


# ==============================================================================
# STATUS PRESENTATION
# ==============================================================================

STATUS_STYLE = {
    "OK": ("✅", "#1a7f37"),
    "ACTIVE": ("✅", "#1a7f37"),
    "PARTIAL": ("⚠️", "#9a6700"),
    "WATCH": ("⚠️", "#9a6700"),
    "ALERT": ("🔴", "#cf222e"),
    "NOT APPLICABLE": ("⊘", "#6e7781"),
    "NO SIGNAL": ("⊘", "#6e7781"),
    "INSUFFICIENT DATA": ("❔", "#6e7781"),
    "NOT COMPUTABLE": ("⊘", "#6e7781"),
}


def status_badge(status: str) -> str:
    icon, colour = STATUS_STYLE.get(status, ("•", "#6e7781"))
    return (f'<span style="color:{colour};font-weight:600">'
            f'{icon} {status}</span>')


def indicator_card(name: str, status: str, value=None, unit: str = "",
                   area_ha=None, reason: str = "", help_text: str = ""):
    """
    One indicator. A value is shown only when the status is OK — otherwise the
    verdict and its reason take the number's place, so a gated indicator can
    never be misread as a zero.
    """
    with st.container(border=True):
        st.markdown(f"**{name}** &nbsp; {status_badge(status)}",
                    unsafe_allow_html=True)
        if status == "OK" and value is not None:
            st.metric(label=unit or "value", value=f"{value:.4f}",
                      delta=f"{area_ha:,.1f} ha" if area_ha else None,
                      delta_color="off")
        else:
            st.markdown(f"<span style='color:#6e7781'>{reason or status}</span>",
                        unsafe_allow_html=True)
        if help_text:
            st.caption(help_text)


# ==============================================================================
# PERSISTENT BANNER
# ==============================================================================

def honesty_banner():
    st.warning(
        "**Screening tool — not a detector.** Everything here measures change "
        "inside an area that contains a mine. It does **not** attribute that "
        "change to mining: bare rock, wadi beds, quarries, roads, construction "
        "and drought all produce the same signal. No legal or compliance "
        "conclusion follows from any number on this page.",
        icon="⚠️")


# ==============================================================================
# PAGES
# ==============================================================================

def page_overview():
    st.title("EcoMine Observatory")
    st.caption("Multi-sensor environmental screening for mining footprints · "
               "GPL-3.0 · pre-release")
    honesty_banner()

    st.subheader("Verified results")
    st.caption("Ad Duwayhi gold mine, Saudi Arabia (22.29799, 43.26475) — "
               "run against live Earth Engine, 1 August 2026")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("SEI — Sentinel-2", "4.99%", "556.5 ha", delta_color="off")
        st.caption("2018 → 2025 · 10 m")
    with c2:
        st.metric("SEI — Landsat 8/9", "4.79%", "533.9 ha", delta_color="off")
        st.caption("2015 → 2024 · 30 m")
    with c3:
        st.metric("Agreement", "4%", "cross-satellite", delta_color="off")
        st.caption("Two satellites, two resolutions, two periods")

    st.subheader("Verification tests")
    st.dataframe(
        {
            "Test": ["Null", "Control site", "Control (atmospheric)",
                     "Applicability gate", "Reproducibility"],
            "Method": [
                "One epoch differenced against itself",
                "Identical AOI 50 km west, no mine",
                "Same offset, Sentinel-5P",
                "Baseline NDVI 0.055 vs 0.15 floor",
                "Repeat runs",
            ],
            "Result": [
                "Exactly zero in every field",
                "0.62% vs 4.99% — 8:1",
                "SO₂ fell +11.6% → +0.5%",
                "VLI returned NOT APPLICABLE",
                "Identical figures",
            ],
        },
        hide_index=True, use_container_width=True)

    st.info(
        "**The 0.62% control result is not an error.** It is the "
        "natural-variability floor for this environment, and is quotable as "
        "such.", icon="ℹ️")

    st.subheader("What is still missing")
    st.error(
        "**No accuracy figure exists.** There are no reference polygons yet, "
        "so there is no precision, recall or IoU. Stage 3 is the only thing "
        "that produces one — and it needs hand-drawn polygons that no code "
        "can substitute for.", icon="🚧")


def page_sensors():
    st.title("Sensors")
    honesty_banner()

    st.subheader("Proven")
    st.dataframe(
        {
            "Sensor": ["Sentinel-2", "Sentinel-1", "Landsat 8/9",
                       "SRTM DEM", "Sentinel-5P"],
            "Role": ["optical, 10 m", "C-band radar, 10 m", "optical, 30 m",
                     "terrain, 30 m", "atmospheric, 5.5×3.5 km"],
            "What it established": [
                "BSI separates mine from desert · SEI 4.99%",
                "Independent physical signal — geometric scattering",
                "Cross-validation · SEI 4.79%",
                "Pre-mining topographic baseline (SRTM predates mine)",
                "Regional screening ONLY — cannot resolve one site",
            ],
        },
        hide_index=True, use_container_width=True)

    st.subheader("Ruled out — with reasons")
    with st.container(border=True):
        st.markdown("**Landsat 5/7 archive**")
        st.markdown(
            "A same-sensor comparison (2010→2022) returned a ΔBSI median of "
            "**−0.00879** — implying the whole area became *less* exposed "
            "while a mine was being dug. Physically impossible. Coverage was "
            "not the cause: both composites observed 11,146 ha of 11,146 ha.")
        st.markdown(
            "The calibration drift (0.009) **exceeds the signal being sought** "
            "(0.008). The site-adaptive threshold corrects for site noise but "
            "**not** for sensor calibration offset, so it raises itself above "
            "the drift and buries the real signal beneath it.")
        st.caption("This is a documented method limit, not just a data problem.")

    with st.container(border=True):
        st.markdown("**PlanetScope**")
        st.markdown(
            "The NICFI free programme ended 23 January 2025, and its coverage "
            "was the tropical-forest belt — never Saudi Arabia. The design "
            "constraint it was meant to address (small artisanal sites below "
            "Sentinel-2's 10 m) is therefore **currently unaddressed**.")

    st.subheader("Sentinel-5P scale limit")
    st.warning(
        "TROPOMI resolution is ~5.5 × 3.5 km. A 12 km-wide site AOI falls "
        "inside **one to four pixels**. No emission can be attributed to a "
        "specific facility. The implemented method is a *regional* comparison "
        "— a 25 km zone against an 80–120 km reference ring — reported as a "
        "screening flag. Roads, haulage traffic, settlements and diesel "
        "generators share the same pixels.", icon="⚠️")

    st.markdown(
        "**2024 result:** SO₂ +11.6%, NO₂ +5.1%, CO +2.1% vs regional "
        "background. Control site 50 km west: +0.5%, −7.3%, +0.8%. The spatial "
        "contrast argues against a retrieval artefact — desert brightness is "
        "identical at both. **But the 10% flagging threshold is arbitrary**, "
        "not derived from a distribution.")


def page_method():
    st.title("Method")
    honesty_banner()

    st.subheader("Site-adaptive thresholding")
    st.markdown(
        "Relative change (ΔNDVI ÷ baseline) breaks in arid terrain: where "
        "baseline NDVI is 0.08, a trivial 0.04 shift reads as 50% loss, and "
        "the indicator measures **aridity rather than mining**. Fixed absolute "
        "thresholds fail the other way — 0.10 is noise on the Highveld and "
        "real signal in hyper-arid desert.")

    st.code(
        "sigma     = (p84 - p16) / 2      # robust: outliers are the signal\n"
        "threshold = median + 2 * sigma   # derived per site, not chosen",
        language="python")

    st.success(
        "**Proof it matters.** Measured site sigma was 0.0038, giving a "
        "threshold of 0.0067. The **0.05 absolute threshold common in the "
        "literature would have detected nothing at all.**", icon="✅")

    st.info(
        "**Stated assumption.** Disturbance must be a *minority* of the AOI. "
        "Above ~30% the null distribution is contaminated by the signal and "
        "the indicator under-reports. The remedy is a larger buffer, never a "
        "smaller k.", icon="ℹ️")

    st.subheader("Why overall accuracy is not reported")
    st.error(
        "Mining occupies ~5% of pixels. A classifier answering *not mine* to "
        "everything scores **0.95 overall accuracy and 0.00 IoU**. This was "
        "verified numerically in the test suite. Stage 3 reports "
        "**precision, recall and IoU on the mine class**, as EuroMineNet does.",
        icon="⚠️")


def page_monitoring():
    st.title("Continuous monitoring")
    honesty_banner()

    st.error(
        "**Not yet wired to live data.** The engine below is built and tested "
        "(27 tests), but alerting on the *unsupervised screening layer* would "
        "produce alerts nobody can defend. It must be connected to the Stage 3 "
        "supervised classifier output before any alert reaches a person.",
        icon="🚧")

    st.subheader("Three guards against alert fatigue")
    for title, body in [
        ("Significance",
         "A change must exceed the site's **own measured noise floor**, from "
         "that site's observed history. A stable site gets a tight test, a "
         "variable one gets a loose test, automatically."),
        ("Persistence",
         "A single anomalous observation is usually an artefact — residual "
         "cloud, unusual view angle, bad composite. Two consecutive breaching "
         "observations are required before anything alerts."),
        ("Standing-condition suppression",
         "A mine that expands and stays expanded is **one event**. Once "
         "alerted, the condition stays suppressed until the breach run ends "
         "or the value moves materially again. Elapsed time is deliberately "
         "**not** a release condition — releasing on time re-alerts the same "
         "standing condition forever."),
    ]:
        with st.container(border=True):
            st.markdown(f"**{title}**")
            st.markdown(body)

    st.caption(
        "The third guard was corrected after a simulation produced 5 alerts "
        "for 2 real events while all 26 unit tests passed. Unit tests check "
        "rules in isolation; simulation reveals how they interact over time.")


def page_stage3():
    st.title("Stage 3 — the classifier")
    honesty_banner()

    st.warning(
        "**This is the gate.** Every figure this project holds carries the "
        "same caveat: *change inside a box that contains a mine*. Stage 3 is "
        "the only thing that removes it.", icon="🔑")

    st.subheader("What is required — and it is your work, not the code's")
    st.markdown(
        "A GEE FeatureCollection of hand-drawn polygons with an **integer** "
        "property named `class`.")

    c1, c2 = st.columns(2)
    with c1:
        with st.container(border=True):
            st.markdown("**`class = 1` — mine** · 50–150 polygons")
            st.markdown(
                "- pit floor\n- benches\n- waste rock\n- leach pads\n"
                "- plant area")
            st.caption(
                "Spread them across every visually distinct surface. Polygons "
                "covering only the pit floor teach the classifier one surface "
                "and it will miss the rest.")
    with c2:
        with st.container(border=True):
            st.markdown("**`class = 0` — not mine** · 150–300 polygons")
            st.markdown(
                "- natural bare rock\n- wadi beds\n- sabkha / salt flats\n"
                "- dirt roads\n- nearby quarries or construction")
            st.caption(
                "**Hard negatives decide everything.** Empty desert is "
                "trivially separable and teaches almost nothing. These are "
                "what stop the classifier calling every bright bare surface "
                "a mine.")

    st.info("Target ratio ≈ **3:1** not-mine to mine — the negative class "
            "covers far more varied terrain.", icon="ℹ️")

    st.code(
        "python ecomine_stage3.py --labels projects/YOUR/assets/labels",
        language="bash")

    st.caption("Full walkthrough in `docs/LABELLING_GUIDE_AR.md`.")


def page_provenance():
    st.title("Provenance")
    honesty_banner()

    st.markdown(
        "Every indicator writes a machine-readable derivation record. A number "
        "without its derivation is an assertion, not a measurement.")

    up = st.file_uploader("Open a provenance record", type="json")
    if up:
        rec = json.load(up)
        res = rec.get("result", {})
        st.subheader(f"{rec.get('indicator','?')} — {res.get('status','?')}")
        if res.get("status") == "OK":
            st.metric(res.get("unit", "value"), res.get("value"))
        else:
            st.markdown(f"*{res.get('reason','')}*")

        if rec.get("arbitrary_thresholds_present"):
            st.warning(
                "**Arbitrary thresholds in this record:** "
                + ", ".join(rec["arbitrary_thresholds_present"])
                + " — chosen by hand, not derived from a distribution. A "
                "different value would change the outcome.", icon="⚠️")

        for c in rec.get("caveats", []):
            st.caption(f"• {c}")
        with st.expander("Full record"):
            st.json(rec)
    else:
        example = Path("example_sei_provenance.json")
        if example.exists():
            st.caption("No file selected. Showing the bundled example.")
            with st.expander("Example record"):
                st.json(json.loads(example.read_text(encoding="utf-8")))


# ==============================================================================
# ROUTER
# ==============================================================================

PAGES = {
    "Overview": page_overview,
    "Sensors": page_sensors,
    "Method": page_method,
    "Stage 3 — classifier": page_stage3,
    "Continuous monitoring": page_monitoring,
    "Provenance": page_provenance,
}


def main():
    with st.sidebar:
        st.title("⛏ EcoMine")
        st.caption("Observatory · pre-release")
        choice = st.radio("Section", list(PAGES), label_visibility="collapsed")
        st.divider()
        st.caption(
            "**Status:** Stages 1–2 verified against live Earth Engine. "
            "Stage 3 code ready, awaiting labelled polygons. Stage 4 built and "
            "tested, not yet wired to live data.")
        st.caption("GPL-3.0 · ORCID 0000-0003-0821-2991")
    PAGES[choice]()


if __name__ == "__main__":
    main()
