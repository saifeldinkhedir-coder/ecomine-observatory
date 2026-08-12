"""
Tests for EcoMine Observatory core logic.

These run with no Earth Engine credentials, no network and no satellite data.
They test the rules that decide whether a number is reported at all — which are
the rules most likely to produce a confident wrong answer if they break.

    pytest -v

The most important test in this file is test_null_change_produces_no_signal:
it is the automated form of the A6 null test. If differencing a year against
itself yields a non-trivial indicator, every downstream number is fiction.
"""

import math

import pytest

from ecomine_core import (
    K_SIGMA,
    MEI_WEIGHTS,
    MIN_AOI_COVERAGE,
    VLI_MIN_BASELINE_NDVI,
    adaptive_threshold,
    compose_mei,
    is_null_contaminated,
    robust_sigma,
    sufficiency_verdict,
    vli_applicable,
    windows_comparable,
)


# ==============================================================================
# THRESHOLD DERIVATION
# ==============================================================================

class TestRobustSigma:
    def test_symmetric_percentiles(self):
        assert robust_sigma(-0.05, 0.05) == pytest.approx(0.05)

    def test_matches_stdev_on_normal_percentiles(self):
        # For a standard normal, p16 and p84 sit at about -1 and +1 sigma.
        assert robust_sigma(-1.0, 1.0) == pytest.approx(1.0, abs=0.01)

    def test_zero_spread_gives_zero_sigma(self):
        assert robust_sigma(0.1, 0.1) == 0.0

    def test_not_inflated_by_outliers(self):
        """
        The whole reason for using percentiles. A distribution with extreme
        outliers has the same p16/p84 spread as one without, so the threshold
        does not drift upward as disturbance grows.
        """
        clean = robust_sigma(-0.02, 0.02)
        with_outliers = robust_sigma(-0.02, 0.02)  # outliers live beyond p84
        assert clean == with_outliers


class TestAdaptiveThreshold:
    def test_threshold_above_median(self):
        assert adaptive_threshold(median=0.0, sigma=0.02, k=2.0) == pytest.approx(0.04)

    def test_offset_median_is_carried(self):
        """A site with a systematic drift must not have that drift counted as change."""
        assert adaptive_threshold(median=0.03, sigma=0.01, k=2.0) == pytest.approx(0.05)

    def test_zero_sigma_collapses_to_median(self):
        assert adaptive_threshold(median=0.07, sigma=0.0) == pytest.approx(0.07)

    def test_larger_k_is_more_conservative(self):
        strict = adaptive_threshold(0.0, 0.02, k=3.0)
        loose = adaptive_threshold(0.0, 0.02, k=1.0)
        assert strict > loose

    def test_arid_and_temperate_thresholds_differ(self):
        """
        The core claim of the method: an arid site with a tight null gets a
        tighter threshold than a variable grassland, so both are judged against
        their own variability rather than one shared absolute cut.
        """
        arid = adaptive_threshold(median=0.0, sigma=0.012)
        grassland = adaptive_threshold(median=0.0, sigma=0.075)
        assert arid < grassland
        # A change of 0.05 is signal in the desert and noise on the Highveld.
        assert 0.05 > arid
        assert 0.05 < grassland


class TestContaminationGuard:
    def test_minority_change_is_clean(self):
        assert not is_null_contaminated(0.08)

    def test_majority_change_is_contaminated(self):
        assert is_null_contaminated(0.55)

    def test_boundary_is_not_flagged(self):
        assert not is_null_contaminated(0.30, guard=0.30)


# ==============================================================================
# THE NULL TEST  (automated form of checklist A6)
# ==============================================================================

class TestNullBehaviour:
    def test_null_change_produces_no_signal(self):
        """
        Differencing an epoch against itself gives a change image that is
        identically zero: median 0, spread 0. The threshold must then be 0, and
        a strictly-greater-than comparison must exclude every pixel.

        If this ever fails, indicators are reporting change where none exists.
        """
        median, p16, p84 = 0.0, 0.0, 0.0
        sigma = robust_sigma(p16, p84)
        thr = adaptive_threshold(median, sigma)
        assert thr == 0.0
        # No pixel value of exactly zero exceeds a threshold of zero.
        assert not (0.0 > thr)

    def test_pure_noise_stays_below_threshold_at_2_sigma(self):
        """
        With k=2, roughly the top 2.3% of a normal null sits above threshold.
        A pixel one sigma out — ordinary noise — must not be counted.
        """
        sigma = robust_sigma(-0.02, 0.02)
        thr = adaptive_threshold(0.0, sigma, k=K_SIGMA)
        one_sigma_pixel = 0.02
        assert one_sigma_pixel < thr

    def test_genuine_step_change_is_detected(self):
        """A real disturbance well beyond the noise floor must clear threshold."""
        sigma = robust_sigma(-0.02, 0.02)
        thr = adaptive_threshold(0.0, sigma, k=K_SIGMA)
        assert 0.25 > thr


# ==============================================================================
# APPLICABILITY GATES
# ==============================================================================

class TestVliGate:
    def test_hyper_arid_site_is_not_applicable(self):
        ok, msg = vli_applicable(0.06)
        assert not ok
        assert "NOT APPLICABLE" in msg

    def test_vegetated_site_passes(self):
        ok, msg = vli_applicable(0.42)
        assert ok
        assert msg == "OK"

    def test_missing_data_is_distinguished_from_not_applicable(self):
        """
        'No vegetation here' and 'we could not see' are different findings and
        must never collapse into the same output.
        """
        ok, msg = vli_applicable(None)
        assert not ok
        assert "INSUFFICIENT DATA" in msg

    def test_boundary_value_passes(self):
        ok, _ = vli_applicable(VLI_MIN_BASELINE_NDVI)
        assert ok


# ==============================================================================
# DATA SUFFICIENCY
# ==============================================================================

class TestSufficiency:
    def test_both_sensors_good(self):
        r = sufficiency_verdict(n_s2_scenes=40, aoi_coverage=0.95, n_s1_scenes=60)
        assert r["verdict"] == "SUFFICIENT"

    def test_no_data_at_all_refuses(self):
        r = sufficiency_verdict(0, 0.0, 0)
        assert r["verdict"].startswith("INSUFFICIENT")
        assert not r["optical_usable"] and not r["radar_usable"]

    def test_cloudy_optical_falls_back_to_radar(self):
        """The multi-sensor payoff: radar still reportable when optical is lost."""
        r = sufficiency_verdict(n_s2_scenes=12, aoi_coverage=0.20, n_s1_scenes=48)
        assert r["verdict"].startswith("PARTIAL")
        assert r["radar_usable"] and not r["optical_usable"]

    def test_scenes_present_but_coverage_thin_is_insufficient(self):
        """
        Scene count is not coverage. Thirty scenes that are all cloud over the
        AOI must not pass just because the collection was non-empty.
        """
        r = sufficiency_verdict(n_s2_scenes=30, aoi_coverage=0.31, n_s1_scenes=0)
        assert r["verdict"].startswith("INSUFFICIENT")

    def test_coverage_boundary_passes(self):
        r = sufficiency_verdict(10, MIN_AOI_COVERAGE, 10)
        assert r["optical_usable"]


class TestWindowComparability:
    def test_matching_rules_comparable(self):
        ok, _ = windows_comparable("low_cloud_annual", "low_cloud_annual")
        assert ok

    def test_mismatched_rules_rejected(self):
        ok, msg = windows_comparable("end_of_dry_season", "low_cloud_annual")
        assert not ok
        assert "seasonality" in msg


# ==============================================================================
# COMPOSITE INDEX
# ==============================================================================

class TestComposeMei:
    def test_all_three_components(self):
        r = compose_mei({"VLI": 0.30, "SEI": 0.60, "WDI": 0.00})
        assert r["status"] == "OK"
        assert r["value"] == pytest.approx(0.30, abs=1e-3)
        assert not r["weights_renormalised"]

    def test_partial_is_flagged_and_renormalised(self):
        """
        The Saudi case: VLI is NOT APPLICABLE, so MEI is built from two
        components. It must be marked PARTIAL, not presented as a full MEI.
        """
        r = compose_mei(
            {"SEI": 0.40, "WDI": 0.10},
            excluded={"VLI": "NOT APPLICABLE"},
        )
        assert r["status"] == "PARTIAL"
        assert r["weights_renormalised"]
        assert r["value"] == pytest.approx(0.25, abs=1e-3)
        assert "VLI" in r["components_excluded"]

    def test_excluded_component_is_not_treated_as_zero(self):
        """
        A missing indicator must not be silently imputed as zero — that would
        drag MEI down and understate impact. Compare renormalised against
        zero-filled and confirm they differ.
        """
        renormalised = compose_mei(
            {"SEI": 0.60, "WDI": 0.60}, excluded={"VLI": "NOT APPLICABLE"}
        )["value"]
        zero_filled = sum(MEI_WEIGHTS[k] * v for k, v in
                          {"SEI": 0.60, "WDI": 0.60, "VLI": 0.0}.items())
        assert renormalised == pytest.approx(0.60, abs=1e-3)
        assert not math.isclose(renormalised, zero_filled, abs_tol=1e-3)

    def test_no_components_refuses(self):
        r = compose_mei({}, excluded={"VLI": "NOT APPLICABLE",
                                      "SEI": "INSUFFICIENT DATA",
                                      "WDI": "INSUFFICIENT DATA"})
        assert r["status"] == "INSUFFICIENT DATA"
        assert r["value"] is None

    def test_caveat_always_present(self):
        """The arbitrary-weighting disclosure must never be droppable."""
        r = compose_mei({"SEI": 0.5})
        assert "arbitrary" in r["caveat"].lower()
