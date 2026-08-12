"""Tests for the provenance module. No GEE, no network."""
import json
import pytest
from provenance import (
    Provenance, SiteRecord, DatasetRecord, ThresholdRecord,
    DataSufficiency, UNIVERSAL_CAVEATS,
)

SITE = SiteRecord(
    name="Ad Duwayhi gold mine", country="Saudi Arabia",
    lat=22.29799, lon=43.26475, buffer_km=6.0,
    coordinate_provenance="Mindat, secondary, visually confirmed, NOT surveyed.",
    seasonality="hyper_arid",
)


class TestSiteRecord:
    def test_requires_coordinate_provenance(self):
        """A coordinate with no stated origin will be mistaken for surveyed."""
        with pytest.raises(ValueError, match="coordinate_provenance"):
            SiteRecord(name="X", country="Y", lat=0, lon=0, buffer_km=1,
                       coordinate_provenance="   ")


class TestOutcomeRequired:
    def test_record_without_outcome_refuses_to_write(self):
        p = Provenance("SEI").set_site(SITE)
        with pytest.raises(ValueError, match="no outcome"):
            p.to_dict()

    def test_record_without_site_refuses(self):
        p = Provenance("SEI").set_value(0.05, "fraction")
        with pytest.raises(ValueError, match="no site"):
            p.to_dict()


class TestOutcomeTypes:
    def test_ok_carries_value(self):
        d = (Provenance("SEI", "Stage 2").set_site(SITE)
             .set_value(0.0479, "fraction_of_observed_aoi", area_ha=533.9)
             .to_dict())
        assert d["result"]["status"] == "OK"
        assert d["result"]["value"] == 0.0479
        assert d["result"]["area_ha"] == 533.9

    def test_not_applicable_has_no_value(self):
        d = (Provenance("VLI").set_site(SITE)
             .set_not_applicable("Baseline NDVI 0.055 below 0.15 floor.")
             .to_dict())
        assert d["result"]["status"] == "NOT APPLICABLE"
        assert d["result"]["value"] is None
        assert "0.055" in d["result"]["reason"]

    def test_insufficient_data_distinct_from_not_applicable(self):
        """
        'Nothing here to measure' and 'we could not see' are different
        findings. Collapsing them would hide a data gap behind a real result.
        """
        na = (Provenance("VLI").set_site(SITE)
              .set_not_applicable("no vegetation").to_dict())
        idd = (Provenance("VLI").set_site(SITE)
               .set_insufficient_data("cloud").to_dict())
        assert na["result"]["status"] != idd["result"]["status"]


class TestThresholds:
    def test_derived_threshold_recorded(self):
        t = ThresholdRecord(
            name="SEI_dBSI", value=0.00861,
            derivation="median + 2 * robust_sigma, per site",
            inputs={"median": 0.00097, "robust_sigma": 0.00382, "k": 2.0})
        d = (Provenance("SEI").set_site(SITE).add_threshold(t)
             .set_value(0.0479, "fraction").to_dict())
        assert d["thresholds"][0]["inputs"]["robust_sigma"] == 0.00382
        assert "arbitrary_thresholds_present" not in d

    def test_arbitrary_threshold_is_flagged_and_annotated(self):
        """
        The 10% atmospheric flagging cut has no statistical basis. It must
        announce itself rather than sit in a table looking derived.
        """
        t = ThresholdRecord(
            name="AQI_flag_pct", value=10.0,
            derivation="Chosen by hand.", is_arbitrary=True)
        assert "ARBITRARY" in t.derivation
        d = (Provenance("AQI").set_site(SITE).add_threshold(t)
             .set_value(11.6, "percent_vs_background").to_dict())
        assert d["arbitrary_thresholds_present"] == ["AQI_flag_pct"]


class TestSufficiency:
    def test_coverage_fraction(self):
        s = DataSufficiency(observed_ha=11146.1, nominal_ha=11146.1,
                            verdict="SUFFICIENT")
        assert s.coverage_fraction == pytest.approx(1.0)
        d = (Provenance("SEI").set_site(SITE).set_sufficiency(s)
             .set_value(0.0479, "fraction").to_dict())
        assert d["data_sufficiency"]["coverage_fraction"] == 1.0

    def test_partial_coverage_visible(self):
        s = DataSufficiency(observed_ha=5000.0, nominal_ha=11146.1,
                            verdict="PARTIAL")
        assert s.coverage_fraction == pytest.approx(0.4486, abs=1e-4)


class TestCaveats:
    def test_universal_caveats_always_present(self):
        d = (Provenance("SEI").set_site(SITE)
             .set_value(0.0479, "fraction").to_dict())
        for c in UNIVERSAL_CAVEATS:
            assert c in d["caveats"]

    def test_attribution_caveat_cannot_be_dropped(self):
        """The single most important thing these numbers do NOT establish."""
        d = (Provenance("SEI").set_site(SITE)
             .set_value(0.0479, "fraction").to_dict())
        joined = " ".join(d["caveats"])
        assert "does not attribute" in joined
        assert "No legal" in joined

    def test_extra_caveats_append_not_replace(self):
        d = (Provenance("SEI").set_site(SITE)
             .caveat("Baseline 2015 postdates mine start 2016; measures "
                     "expansion, not total impact.")
             .set_value(0.0479, "fraction").to_dict())
        assert len(d["caveats"]) == len(UNIVERSAL_CAVEATS) + 1


class TestRoundTrip:
    def test_full_record_serialises(self, tmp_path):
        p = (Provenance("SEI", "Stage 2")
             .set_site(SITE)
             .set_project("example-project-id")
             .add_dataset(DatasetRecord(
                 collection_id="COPERNICUS/S2_SR_HARMONIZED", role="optical",
                 scenes_used=82, date_start="2025-01-01", date_end="2025-12-31",
                 filters=["CLOUDY_PIXEL_PERCENTAGE < 40"],
                 masking="SCL classes 3,8,9,10,11"))
             .add_dataset(DatasetRecord(
                 collection_id="COPERNICUS/S1_GRD", role="radar",
                 scenes_used=42, filters=["IW", "ASCENDING"],
                 notes="focal_median radius=50 m"))
             .set_epochs(baseline={"year": 2018}, current={"year": 2025},
                         rule="low_cloud_annual",
                         justification="Hyper-arid; dry-season rule from "
                                       "Sahelian literature not applicable.")
             .add_threshold(ThresholdRecord(
                 name="SEI_dBSI", value=0.00668,
                 derivation="median + 2 * robust_sigma",
                 inputs={"median": -0.00094, "robust_sigma": 0.00381}))
             .set_sufficiency(DataSufficiency(11146.1, 11146.1, "SUFFICIENT"))
             .set_method(scale_m=20, robust_sigma="(p84-p16)/2", k_sigma=2.0)
             .set_value(0.0499, "fraction_of_observed_aoi", area_ha=556.5))
        path = p.write(str(tmp_path / "r.json"))
        back = json.load(open(path, encoding="utf-8"))
        assert back["result"]["value"] == 0.0499
        assert len(back["datasets"]) == 2
        assert back["site"]["seasonality"] == "hyper_arid"
        assert "NOT surveyed" in back["site"]["coordinate_provenance"]

    def test_summary_line_matches_disk(self):
        p = (Provenance("SEI").set_site(SITE)
             .set_value(0.0479, "fraction", area_ha=533.9))
        line = p.summary_line()
        assert "OK" in line and "533.9" in line

    def test_summary_line_for_gated(self):
        p = (Provenance("VLI").set_site(SITE)
             .set_not_applicable("no vegetation to lose"))
        assert "NOT APPLICABLE" in p.summary_line()
