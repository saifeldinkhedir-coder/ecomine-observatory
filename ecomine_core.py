"""
EcoMine Observatory — core logic (NO external dependencies)
============================================================

Every decision rule in EcoMine that does not need a satellite image lives here:
threshold derivation, data-sufficiency verdicts, applicability gates, and
composite-index assembly.

The point of this module is testability. These are the rules that decide whether
a number gets reported at all, and they must be verifiable without Earth Engine
credentials, without network access, and without waiting on a server-side
reduction. Everything here is pure: same inputs, same outputs, no I/O.

Author: Seifeldin M.G. Alkhedir (ORCID 0000-0003-0821-2991)
Licence: GPL-3.0
"""

from __future__ import annotations

from typing import Optional


# ==============================================================================
# CONSTANTS  (single source of truth — imported by the GEE modules)
# ==============================================================================

#: Multiplier on the robust sigma when deriving a change threshold.
#: 2.0 approximates a 95% one-sided cut under a normal null.
K_SIGMA = 2.0

#: Mean baseline NDVI below which a vegetation-loss indicator is meaningless.
VLI_MIN_BASELINE_NDVI = 0.15

#: Disturbed fraction above which the robust null is contaminated by signal.
NULL_CONTAMINATION_WARN = 0.30

#: Minimum clear optical observations per pixel for a defensible indicator.
MIN_S2_OBSERVATIONS = 3

#: Minimum fraction of AOI clearing MIN_S2_OBSERVATIONS.
MIN_AOI_COVERAGE = 0.70

#: Minimum radar scenes for the S1 layer to be produced.
MIN_S1_SCENES = 4

#: MNDWI above this is treated as water-like.
WATER_MNDWI_MIN = 0.0

#: MEI component weights. Equal weights are an ARBITRARY editorial choice.
# Equal weights are an arbitrary editorial choice, declared as such in every
# MEI output. RDI (radar) is listed so that when the optional radar component
# is present, compose_mei renormalises over whichever components actually
# returned a value. There is no empirical basis for weighting a hectare of
# radar-detected roughness change equally with a hectare of vegetation loss;
# this is a screening summary, not a physical quantity.
MEI_WEIGHTS = {"VLI": 1 / 3, "SEI": 1 / 3, "WDI": 1 / 3, "RDI": 1 / 3}


# ==============================================================================
# THRESHOLD DERIVATION
# ==============================================================================

def robust_sigma(p16: float, p84: float) -> float:
    """
    Robust spread estimator: half the 16th-to-84th percentile range.

    On a normal distribution this equals the standard deviation, but unlike the
    standard deviation it is not inflated by the disturbed pixels — which are
    precisely the outliers we are trying to detect. Using an ordinary stdev here
    would let the signal raise its own detection threshold.
    """
    return (p84 - p16) / 2.0


def adaptive_threshold(median: float, sigma: float, k: float = K_SIGMA) -> float:
    """
    Site-specific change threshold: median + k * robust_sigma.

    Expressed in units of the site's own natural variability, which is what makes
    an arid site and a grassland site comparable. A fixed absolute threshold
    would be noise at one and signal at the other.
    """
    return median + k * sigma


def is_null_contaminated(exceed_fraction: float,
                         guard: float = NULL_CONTAMINATION_WARN) -> bool:
    """
    True when so much of the AOI exceeds threshold that the null distribution
    was estimated from contaminated data.

    When this fires the reported value is an UNDER-estimate: the signal inflated
    the threshold that was used to detect it. The remedy is a larger AOI, never
    a smaller k.
    """
    return exceed_fraction > guard


# ==============================================================================
# APPLICABILITY GATES
# ==============================================================================

def vli_applicable(mean_baseline_ndvi: Optional[float],
                   gate: float = VLI_MIN_BASELINE_NDVI) -> tuple[bool, str]:
    """
    Whether a vegetation-loss indicator carries meaning at this site.

    In hyper-arid terrain there is no vegetation to lose, and a VLI computed
    anyway would measure aridity. Returning NOT APPLICABLE is the correct
    scientific answer, not a failure.
    """
    if mean_baseline_ndvi is None:
        return False, "INSUFFICIENT DATA: no baseline NDVI observations"
    if mean_baseline_ndvi < gate:
        return False, (
            f"NOT APPLICABLE: mean baseline NDVI {mean_baseline_ndvi:.3f} is "
            f"below the vegetation floor of {gate}. There is effectively no "
            "vegetation cover to lose at this site."
        )
    return True, "OK"


# ==============================================================================
# DATA SUFFICIENCY
# ==============================================================================

def sufficiency_verdict(n_s2_scenes: int, aoi_coverage: float,
                        n_s1_scenes: int,
                        min_coverage: float = MIN_AOI_COVERAGE,
                        min_s1: int = MIN_S1_SCENES) -> dict:
    """
    Decide whether a site-year is reportable. Reports the gap; never fills it.

    Returns a dict with a verdict string and the two usability flags. A verdict
    beginning with INSUFFICIENT means no indicator should be emitted at all.
    """
    optical_ok = n_s2_scenes > 0 and aoi_coverage >= min_coverage
    radar_ok = n_s1_scenes >= min_s1

    if optical_ok and radar_ok:
        verdict = "SUFFICIENT"
    elif radar_ok and not optical_ok:
        verdict = "PARTIAL — radar only; optical INSUFFICIENT DATA"
    elif optical_ok and not radar_ok:
        verdict = "PARTIAL — optical only; radar INSUFFICIENT DATA"
    else:
        verdict = "INSUFFICIENT DATA — no indicator should be reported"

    return {
        "verdict": verdict,
        "optical_usable": optical_ok,
        "radar_usable": radar_ok,
        "s2_scenes": n_s2_scenes,
        "s1_scenes": n_s1_scenes,
        "aoi_coverage": round(aoi_coverage, 4),
    }


def windows_comparable(baseline_rule: str, current_rule: str) -> tuple[bool, str]:
    """
    Two epochs may only be differenced if they were selected the same way.

    Differencing a dry-season baseline against an annual composite measures
    phenology, not surface change — and it would do so silently, producing a
    confident and entirely spurious indicator.
    """
    if baseline_rule != current_rule:
        return False, (
            f"Incomparable epochs: baseline used '{baseline_rule}' but current "
            f"used '{current_rule}'. The difference would be dominated by "
            "seasonality rather than surface change."
        )
    return True, "OK"


# ==============================================================================
# COMPOSITE INDEX
# ==============================================================================

def compose_mei(components: dict[str, float],
                excluded: Optional[dict[str, str]] = None,
                weights: Optional[dict[str, float]] = None) -> dict:
    """
    Weighted mean of whichever indicators returned a value, renormalised over
    those present.

    An MEI built from one component is a DIFFERENT QUANTITY from one built from
    three. The output therefore always declares which components went in, which
    were excluded and why, and whether renormalisation occurred. Presenting
    these as the same number across sites would be misleading.
    """
    excluded = excluded or {}
    weights = weights or MEI_WEIGHTS

    if not components:
        return {
            "status": "INSUFFICIENT DATA",
            "value": None,
            "reason": "no component indicator returned a value",
            "components_included": {},
            "components_excluded": excluded,
        }

    w_total = sum(weights[k] for k in components)
    value = sum(weights[k] * v for k, v in components.items()) / w_total

    return {
        "status": "OK" if not excluded else "PARTIAL",
        "value": round(value, 4),
        "components_included": dict(components),
        "components_excluded": excluded,
        "weights_used": {k: weights[k] for k in components},
        "weights_renormalised": bool(excluded),
        "caveat": (
            "Equal weighting is an arbitrary editorial choice with no empirical "
            "basis. There is no established exchange rate between vegetation "
            "loss and water disturbance. MEI is a screening summary with no "
            "physical units and must not be compared across sites whose "
            "component sets differ."
        ),
    }
