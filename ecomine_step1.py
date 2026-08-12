"""
EcoMine Observatory — STEP 1: minimal multi-sensor demonstration
================================================================

Open-source tool for mining footprint + environmental impact monitoring.
Author: Seifeldin M.G. Alkhedir (ORCID 0000-0003-0821-2991)
Licence: GPL-3.0
Stack: Google Earth Engine + geemap (opengeos) — all pixel compute is server-side.

WHAT THIS SCRIPT PRODUCES
-------------------------
  1. A Sentinel-2 dry-season (or low-cloud annual) surface-reflectance composite
  2. A Sentinel-1 C-band radar composite (VV, VH, VV/VH) for the same window
  3. Core spectral indices: NDVI, NDWI, MNDWI, BSI, NDMI
  4. A CANDIDATE SURFACE-DISTURBANCE SCREEN (multi-sensor, transparent thresholds)
  5. A dNDVI vegetation-change layer (baseline -> current) = VLI input
  6. An interactive geemap/leafmap map + a machine-readable provenance record

WHAT THIS SCRIPT DOES *NOT* PRODUCE  (honesty principle, brief S3.2 / S8)
-------------------------------------------------------------------------
  * It does NOT produce a validated mining footprint. The disturbance screen is
    an unsupervised, threshold-based CANDIDATE layer. Bare rock, sabkha, wadi
    beds, quarries, roads and construction all trigger it. Calling its output a
    "mine" without ground truth or hand-labelled training data is exactly the
    over-claim the project is built to avoid. Supervised Random Forest is
    Stage 1b, once labels exist (see train_rf_from_labels() below).
  * It does NOT emit legal conclusions. Indicators point toward frameworks that
    warrant expert review; they never state non-compliance.
  * It does NOT interpolate or guess. Where observations are too few, the pixel
    is masked and the region is reported as INSUFFICIENT DATA.

SCOPE
-----
Configured demonstration sites are in Saudi Arabia and South Africa. Adding a
site means adding an entry to SITES with honest coordinate provenance and a
seasonality class; nothing else in the pipeline is site-specific.

REQUIREMENTS
------------
    pip install earthengine-api geemap
    earthengine authenticate

RUN
---
    python ecomine_step1.py                      # default site, writes HTML map
    python ecomine_step1.py --site witbank_coal  # another preset
    python ecomine_step1.py --list               # show presets
"""

from __future__ import annotations

import argparse
import json
import sys
import math
from dataclasses import dataclass
from typing import Optional

import ee
import geemap

from provenance import (
    Provenance,
    SiteRecord,
    DatasetRecord,
    ThresholdRecord,
    DataSufficiency,
)
from ecomine_core import (
    MIN_S2_OBSERVATIONS,
    MIN_AOI_COVERAGE,
    MIN_S1_SCENES,
    sufficiency_verdict,
)


# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================

#: Google Cloud project registered for Earth Engine. Read from .env or the
#: environment so that a personal project ID is never committed. See .env.example.
import os as _os
from pathlib import Path as _Path


def _read_project_id() -> str:
    _env = _Path(__file__).parent / ".env"
    if _env.exists():
        for _line in _env.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line.startswith("EE_PROJECT="):
                return _line.split("=", 1)[1].strip().strip("\"'")
    return _os.environ.get("EE_PROJECT", "")


EE_PROJECT = _read_project_id()

# Sufficiency thresholds (MIN_S2_OBSERVATIONS, MIN_AOI_COVERAGE, MIN_S1_SCENES)
# are defined once in ecomine_core and imported above. Do not redefine them here
# — two copies of a threshold is two answers to the same question.


@dataclass
class Site:
    """A monitored site and everything needed to justify how it was imaged."""

    key: str
    name: str
    country: str
    lat: float
    lon: float
    buffer_km: float
    commodity: str
    #: 'seasonal'   -> a real wet/dry cycle exists; use the dry-season window
    #: 'hyper_arid' -> no meaningful wet season; use a low-cloud annual composite
    seasonality: str
    #: Inclusive month range of the END of the dry season. None if hyper-arid.
    dry_season_months: Optional[tuple[int, int]]
    #: Why these coordinates are where they are, and how good they are.
    coordinate_provenance: str
    notes: str = ""


SITES: dict[str, Site] = {
    "ad_duwayhi": Site(
        key="ad_duwayhi",
        name="Ad Duwayhi gold mine (open pit)",
        country="Saudi Arabia",
        lat=22.29799,
        lon=43.26475,
        buffer_km=6.0,
        commodity="gold (open pit + heap leach)",
        seasonality="hyper_arid",
        dry_season_months=None,
        coordinate_provenance=(
            "Mindat (secondary source), VISUALLY CONFIRMED against Esri World "
            "Imagery on 1 Aug 2026: pit, benches, waste rock and leach pads all "
            "fall inside the AOI, occupying ~5-8% of it. NOT SURVEYED. Replace "
            "with the MIM cadastre or a Ma'aden disclosure before publication. "
            "An earlier estimate of 22.44, 41.55 was wrong by ~176 km and is "
            "recorded here so the error is not repeated."
        ),
        notes=(
            "Open-pit workings and heap-leach pads are unambiguous at Sentinel-2 "
            "10 m, which makes this the appropriate site for the Stage 1 go/no-go "
            "decision — unlike the largely underground Mahd adh Dhahab. "
            "Hyper-arid: the dry-season rule does not apply, and VLI is expected "
            "to return NOT APPLICABLE."
        ),
    ),
    "mahd_adh_dhahab": Site(
        key="mahd_adh_dhahab",
        name="Mahd adh Dhahab gold mine",
        country="Saudi Arabia",
        lat=23.4950,
        lon=40.8560,
        buffer_km=5.0,
        commodity="gold (underground + surface works)",
        seasonality="hyper_arid",
        dry_season_months=None,
        coordinate_provenance=(
            "APPROXIMATE centroid derived from the Mahd adh Dhahab township "
            "location. NOT surveyed. Must be replaced with an authoritative "
            "concession boundary (Ministry of Industry & Mineral Resources / "
            "Ma'aden disclosure) before any publication or validation claim."
        ),
        notes=(
            "Hyper-arid: no wet season to wait out, so the S2b dry-season rule "
            "does not apply and is explicitly disabled. Near-permanent clear "
            "skies mean Sentinel-1 adds texture information here, not cloud "
            "penetration — the radar cloud advantage cannot be demonstrated at "
            "this site. Strong NDVI/BSI contrast against desert background."
        ),
    ),
    "kangala": Site(
        key="kangala",
        name="Kangala Coal Mine (opencast, Delmas)",
        country="South Africa",
        lat=-26.188176,
        lon=28.669960,
        buffer_km=4.0,
        commodity="thermal coal (opencast)",
        seasonality="seasonal",
        dry_season_months=(8, 9),
        coordinate_provenance=(
            "User-confirmed from Google Maps on 8 Aug 2026 and VISUALLY "
            "VERIFIED against Esri imagery: dark opencast coal pits, mine-water "
            "ponds, processing stockpiles and a labelled coal discard dump, "
            "with NO adjacent power station or ash dam. This is the project's "
            "first visually confirmed South African site, and the first site "
            "anywhere where all three MEI components (VLI, WDI, SEI) activate. "
            "Distinct from the witbank_coal and west_rand_tailings entries "
            "below, whose coordinates remain APPROXIMATE and unverified."
        ),
        notes=(
            "First operating coal 2015, so Sentinel-2 (from 2015) and Landsat 8 "
            "both span its full pre-mining-to-present history — a clean "
            "baseline, unlike Ad Duwayhi where the pre-2013 archive fails. "
            "CAVEAT for VLI: roughly half the 4 km AOI is centre-pivot and "
            "dryland farmland, so VLI here measures vegetation change that MIXES "
            "crop cycles with mining-related loss. Report it as vegetation "
            "change, not as natural-cover loss, or tighten/mask the AOI to the "
            "pit before making a VLI claim. WDI and SEI are cleaner. Verified "
            "site diagnostic (site_check.py, 8 Aug 2026): MEI 3/3 computable, "
            "VLI mean NDVI 0.197, WDI 51.9 ha water (2.87%), industrial-glare "
            "0.4% and heat excess 4.4 C — i.e. clean of the power-station "
            "signature that ruled out Kriel."
        ),
    ),
    "kangala_pit": Site(
        key="kangala_pit",
        name="Kangala Coal Mine — pit-tight AOI (Delmas)",
        country="South Africa",
        lat=-26.191217,
        lon=28.672936,
        buffer_km=2.0,
        commodity="thermal coal (opencast)",
        seasonality="seasonal",
        dry_season_months=(8, 9),
        coordinate_provenance=(
            "Bounding-box centre of TEN pit-outline points the user placed in "
            "Google Earth Pro on 9 Aug 2026. The pit measures ~1.25 km N-S by "
            "~1.03 km E-W; the farthest point is 665 m from this centre. "
            "Buffer set to 2.0 km so the pit occupies ~11% of the AOI - the "
            "same comfortable minority as Ad Duwayhi (5-8%), keeping the "
            "adaptive-threshold assumption safe while excluding most of the "
            "surrounding farmland that contaminates VLI in the wider 4 km "
            "'kangala' AOI. (A 1.5 km buffer would put the pit at ~20%, closer "
            "to the 30% contamination limit, so 2.0 km is the safer choice now "
            "that ten points show the pit is larger than the first four "
            "suggested.)"
        ),
        notes=(
            "PURPOSE: a controlled comparison against the 4 km 'kangala' AOI. "
            "If VLI falls sharply here, that demonstrates the wider VLI was "
            "dominated by crop-cycle change, not mining vegetation loss - a "
            "result worth reporting: the adaptive threshold alone is not "
            "enough; the AOI must also be confined to the target disturbance. "
            "SEI should hold or rise (pit is a larger share of a smaller box). "
            "Still a screen, not attribution: even pit-tight, this does not "
            "prove the change is mining rather than, e.g., haul-road works."
        ),
    ),
    "kangala_east": Site(
        key="kangala_east",
        name="Kangala Coal Mine - eastern pit (labelled area)",
        country="South Africa",
        lat=-26.157243,
        lon=28.779002,
        buffer_km=2.6,
        commodity="thermal coal (opencast)",
        seasonality="seasonal",
        dry_season_months=(8, 9),
        coordinate_provenance=(
            "Bounding-box centre of the Stage 3 training polygons the user "
            "drew in QGIS on 10 Aug 2026, over the eastern part of the Kangala "
            "complex (white benches and pit visible in Esri imagery). This is "
            "~11 km ENE of the kangala_pit centroid used for the Stage 2 MEI "
            "comparison - a DIFFERENT pit in the same complex. Recorded as its "
            "own site so the Stage 3 classifier is evaluated over exactly the "
            "AOI its labels cover, not over the Stage 2 pit. Labels span ~1.2 "
            "km from this centre; 1.5 km buffer covers them with margin."
        ),
        notes=(
            "PURPOSE: the AOI for the project's first supervised classifier "
            "run. 16 mine polygons (class=1) and 17 not-mine polygons "
            "(class=0) as first drawn - the mine class is below the 20-polygon "
            "floor stage3 enforces, so a few more mine polygons are needed "
            "before training. Not-mine polygons deliberately include the "
            "surrounding farmland, the signal that contaminated VLI in the "
            "wide AOI. This site exists to MEASURE the classifier, so its "
            "centre must track the labels, not the other way round."
        ),
    ),
    "witbank_coal": Site(
        key="witbank_coal",
        name="eMalahleni (Witbank) coalfield — opencast complex",
        country="South Africa",
        lat=-25.9300,
        lon=29.2200,
        buffer_km=8.0,
        commodity="coal (opencast)",
        seasonality="seasonal",
        dry_season_months=(8, 9),  # end of Highveld dry winter, pre-Oct rains
        coordinate_provenance=(
            "APPROXIMATE centroid of the eMalahleni opencast coal belt. This is "
            "an AREA, not a single operator's licence. Individual concession "
            "boundaries must come from the DMRE cadastre before any site- or "
            "operator-level statement is made."
        ),
        notes=(
            "Summer cloud (Oct-Mar) is genuine, so this is the site where "
            "Sentinel-1's cloud penetration can actually be demonstrated. "
            "Well-documented acid mine drainage and water-quality literature "
            "gives a citable impact context for WDI. Recommended for the "
            "public demo if the goal is to show the multi-sensor argument."
        ),
    ),
    "west_rand_tailings": Site(
        key="west_rand_tailings",
        name="West Rand gold tailings complex",
        country="South Africa",
        lat=-26.2500,
        lon=27.8500,
        buffer_km=7.0,
        commodity="gold tailings / reprocessing",
        seasonality="seasonal",
        dry_season_months=(8, 9),
        coordinate_provenance=(
            "APPROXIMATE centroid of the West Rand tailings storage area. "
            "Requires authoritative boundary data before publication."
        ),
        notes=(
            "Tailings dams give a strong NDWI/MNDWI signal and a decades-long "
            "Landsat archive. Best site for exercising WDI rather than VLI."
        ),
    ),
}

DEFAULT_SITE = "kangala"  # only visually confirmed site with full MEI

#: Screening thresholds. Deliberately explicit and editable — the whole point is
#: that a reviewer can see and change every number that shapes the output.
THRESHOLDS = {
    "ndvi_bare_max": 0.20,   # below this = effectively unvegetated
    "bsi_exposed_min": 0.05,  # above this = exposed mineral surface
    "ndmi_dry_max": 0.00,    # below this = dry surface
    "vv_rough_min": -13.0,   # dB; above this = rough / disturbed backscatter
    "dndvi_loss_min": 0.10,  # baseline-minus-current loss counted as vegetation loss
}


# ==============================================================================
# 2. EARTH ENGINE INITIALISATION
# ==============================================================================

def init_ee(project: str = EE_PROJECT) -> None:
    """Initialise Earth Engine, prompting for authentication only if needed."""
    try:
        ee.Initialize(project=project)
    except Exception:
        ee.Authenticate()
        ee.Initialize(project=project)


# ==============================================================================
# 3. AREA OF INTEREST AND TEMPORAL WINDOW
# ==============================================================================

def build_aoi(site: Site) -> ee.Geometry:
    """Circular AOI around the site centroid, buffered to buffer_km."""
    return ee.Geometry.Point([site.lon, site.lat]).buffer(site.buffer_km * 1000)


def build_window(site: Site, year: int) -> dict:
    """
    Choose the imaging window for a site-year, and record *why*.

    Seasonal sites use the end-of-dry-season months (brief S2b constraint 1).
    Hyper-arid sites have no wet season to avoid, so a full-year low-cloud
    composite is used instead and the deviation is recorded in provenance.
    """
    if site.seasonality == "seasonal" and site.dry_season_months:
        m0, m1 = site.dry_season_months
        return {
            "start": f"{year}-{m0:02d}-01",
            "end": f"{year}-{m1:02d}-28",
            "rule": "end_of_dry_season",
            "justification": (
                f"Months {m0}-{m1} are the end of the dry season at this site. "
                "Senescent vegetation minimises spectral obscuration of bare "
                "and disturbed surfaces (brief S2b, constraint 1)."
            ),
        }
    return {
        "start": f"{year}-01-01",
        "end": f"{year}-12-31",
        "rule": "low_cloud_annual",
        "justification": (
            "Site is hyper-arid with no meaningful wet season. The end-of-dry-"
            "season rule derived from Sahelian and tropical studies is NOT "
            "applicable here and was deliberately not applied. A full-year "
            "low-cloud composite is used instead."
        ),
    }


# ==============================================================================
# 4. SENTINEL-2 — OPTICAL
# ==============================================================================

def mask_s2_clouds(img: ee.Image) -> ee.Image:
    """Mask cloud, shadow, cirrus and snow using the Scene Classification Layer."""
    scl = img.select("SCL")
    bad = (
        scl.eq(3)    # cloud shadow
        .Or(scl.eq(8))   # cloud, medium probability
        .Or(scl.eq(9))   # cloud, high probability
        .Or(scl.eq(10))  # thin cirrus
        .Or(scl.eq(11))  # snow / ice
    )
    return img.updateMask(bad.Not()).divide(10000).copyProperties(
        img, ["system:time_start"]
    )


def s2_collection(aoi: ee.Geometry, start: str, end: str) -> ee.ImageCollection:
    return (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 40))
        .map(mask_s2_clouds)
    )


def add_indices(img: ee.Image) -> ee.Image:
    """
    Core spectral indices (brief S5.1).

    NDVI  = (NIR - RED) / (NIR + RED)                     vegetation
    NDWI  = (GREEN - NIR) / (GREEN + NIR)                 open water (McFeeters)
    MNDWI = (GREEN - SWIR1) / (GREEN + SWIR1)             water incl. turbid ponds
    BSI   = ((SWIR1+RED) - (NIR+BLUE)) / ((SWIR1+RED) + (NIR+BLUE))   bare soil
    NDMI  = (NIR - SWIR1) / (NIR + SWIR1)                 surface moisture
    """
    ndvi = img.normalizedDifference(["B8", "B4"]).rename("NDVI")
    ndwi = img.normalizedDifference(["B3", "B8"]).rename("NDWI")
    mndwi = img.normalizedDifference(["B3", "B11"]).rename("MNDWI")
    ndmi = img.normalizedDifference(["B8", "B11"]).rename("NDMI")
    bsi = img.expression(
        "((SWIR + RED) - (NIR + BLUE)) / ((SWIR + RED) + (NIR + BLUE))",
        {
            "SWIR": img.select("B11"),
            "RED": img.select("B4"),
            "NIR": img.select("B8"),
            "BLUE": img.select("B2"),
        },
    ).rename("BSI")
    return img.addBands([ndvi, ndwi, mndwi, ndmi, bsi])


def s2_composite(aoi: ee.Geometry, start: str, end: str) -> dict:
    """Median composite plus the per-pixel clear-observation count."""
    coll = s2_collection(aoi, start, end).map(add_indices)
    n_scenes = coll.size()
    obs_count = coll.select("B4").count().rename("s2_obs_count")
    composite = coll.median().clip(aoi)
    sufficient = obs_count.gte(MIN_S2_OBSERVATIONS)
    return {
        "composite": composite.updateMask(sufficient).clip(aoi),
        "obs_count": obs_count.clip(aoi),
        "sufficient_mask": sufficient.clip(aoi),
        "n_scenes": n_scenes,
    }


# ==============================================================================
# 5. SENTINEL-1 — RADAR  (multi-sensor non-negotiable, brief S5.1)
# ==============================================================================

def s1_composite(aoi: ee.Geometry, start: str, end: str) -> dict:
    """
    C-band GRD composite. Radar is unaffected by cloud, so it carries the
    disturbance signal wherever the optical composite is masked out.
    A 50 m focal median suppresses speckle without needing a GPU.
    """
    base = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(aoi)
        .filterDate(start, end)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
    )
    # Prefer whichever orbit pass has more scenes; mixing passes mixes geometry.
    asc = base.filter(ee.Filter.eq("orbitProperties_pass", "ASCENDING"))
    desc = base.filter(ee.Filter.eq("orbitProperties_pass", "DESCENDING"))
    coll = ee.ImageCollection(
        ee.Algorithms.If(asc.size().gte(desc.size()), asc, desc)
    )
    n_scenes = coll.size()

    # focal_median takes radius/units directly. The kernel= form
    # (focal_median(kernel=ee.Kernel.circle(...))) silently fails to produce a
    # usable band — verified against the live API on 1 Aug 2026.
    vv = coll.select("VV").median().focal_median(
        radius=50, units="meters").rename("VV")
    vh = coll.select("VH").median().focal_median(
        radius=50, units="meters").rename("VH")
    ratio = vv.subtract(vh).rename("VV_VH_diff_dB")  # dB domain: difference = ratio

    return {
        "composite": ee.Image.cat([vv, vh, ratio]).clip(aoi),
        "n_scenes": n_scenes,
        "pass_used": ee.Algorithms.If(
            asc.size().gte(desc.size()), "ASCENDING", "DESCENDING"
        ),
    }


# ==============================================================================
# 6. DATA SUFFICIENCY GATE  (honesty principle, brief S3.2)
# ==============================================================================

def assess_sufficiency(s2: dict, s1: dict, aoi: ee.Geometry) -> dict:
    """
    Decide whether the run is defensible. Returns a verdict, never a repair.
    Nothing here fills a gap; it only reports how large the gap is.
    """
    coverage = (
        s2["sufficient_mask"]
        .unmask(0)
        .reduceRegion(
            reducer=ee.Reducer.mean(), geometry=aoi, scale=20, maxPixels=1e9,
            bestEffort=True,
        )
        .get("s2_obs_count")
    )
    coverage_val = ee.Number(coverage).getInfo() or 0.0
    n_s2 = s2["n_scenes"].getInfo()
    n_s1 = s1["n_scenes"].getInfo()

    # Verdict logic lives in ecomine_core so it can be unit-tested without GEE.
    result = sufficiency_verdict(
        n_s2_scenes=n_s2, aoi_coverage=coverage_val, n_s1_scenes=n_s1
    )
    result.update({
        "aoi_fraction_with_min_observations": round(coverage_val, 3),
        "min_s2_observations_required": MIN_S2_OBSERVATIONS,
        "min_aoi_coverage_required": MIN_AOI_COVERAGE,
        "min_s1_scenes_required": MIN_S1_SCENES,
    })
    return result


# ==============================================================================
# 7. CANDIDATE DISTURBANCE SCREEN  (NOT a footprint — see module docstring)
# ==============================================================================

def disturbance_screen(s2_img: ee.Image, s1_img: ee.Image) -> ee.Image:
    """
    Multi-sensor screening layer for candidate disturbed mineral surfaces.

    Optical criteria : low NDVI AND high BSI AND low NDMI
    Radar criterion  : elevated VV backscatter (rough, disturbed ground)

    Output band 'screen_score' is 0-4 = how many independent criteria agree.
    Higher agreement is more likely to be genuine disturbance, but NONE of it
    is validated. Natural bare rock, sabkha, wadi beds, quarries, roads and
    construction sites all satisfy these criteria. This layer is a starting
    point for labelling, not an answer.
    """
    t = THRESHOLDS
    low_veg = s2_img.select("NDVI").lt(t["ndvi_bare_max"])
    exposed = s2_img.select("BSI").gt(t["bsi_exposed_min"])
    dry = s2_img.select("NDMI").lt(t["ndmi_dry_max"])
    rough = s1_img.select("VV").gt(t["vv_rough_min"])

    score = (
        low_veg.unmask(0)
        .add(exposed.unmask(0))
        .add(dry.unmask(0))
        .add(rough.unmask(0))
        .rename("screen_score")
    )
    # Only keep pixels where at least one sensor actually observed something.
    observed = s2_img.select("NDVI").mask().Or(s1_img.select("VV").mask())
    return score.updateMask(observed)


def train_rf_from_labels(
    features: ee.Image, labels_asset: str, aoi: ee.Geometry, class_property: str = "class"
) -> Optional[ee.Image]:
    """
    STAGE 1b HOOK — supervised Random Forest, brief S4 footprint layer [CORE].

    Deliberately not called by the demo: without hand-labelled polygons there
    is nothing to train on, and an unsupervised threshold dressed up as a
    classifier would be precisely the over-claim this project rejects.

    To activate: hand-digitise mine / not-mine polygons in QGIS, upload as a
    GEE FeatureCollection asset, and pass its ID here.
    """
    if not labels_asset:
        return None
    labels = ee.FeatureCollection(labels_asset)
    training = features.sampleRegions(
        collection=labels, properties=[class_property], scale=10, tileScale=4
    )
    classifier = ee.Classifier.smileRandomForest(numberOfTrees=100).train(
        features=training,
        classProperty=class_property,
        inputProperties=features.bandNames(),
    )
    return features.classify(classifier).rename("rf_footprint").clip(aoi)


# ==============================================================================
# 8. IMPACT — dNDVI / VEGETATION LOSS INPUT (VLI, brief S5.2)
# ==============================================================================

def delta_ndvi(baseline: ee.Image, current: ee.Image) -> ee.Image:
    """
    Baseline NDVI minus current NDVI. Positive = vegetation lost since baseline.
    Masked wherever either epoch lacked sufficient observations — a change value
    computed from an absent baseline is not a measurement.
    """
    d = baseline.select("NDVI").subtract(current.select("NDVI")).rename("dNDVI")
    return d.updateMask(baseline.select("NDVI").mask().And(current.select("NDVI").mask()))


def vegetation_loss_mask(dndvi: ee.Image) -> ee.Image:
    return dndvi.gt(THRESHOLDS["dndvi_loss_min"]).selfMask().rename("veg_loss")


# ==============================================================================
# 9. PROVENANCE RECORD
# ==============================================================================

def build_provenance(site: Site, baseline_win: dict, current_win: dict,
                     sufficiency: dict, baseline_year: int, current_year: int,
                     n_s1: int = 0) -> dict:
    """
    Full derivation record, built with the shared provenance module so that
    every stage emits the same structure and the same non-removable caveats.
    """
    prov = (Provenance("surface_disturbance_screen", "Stage 1")
            .set_site(SiteRecord(
                name=site.name, country=site.country,
                lat=site.lat, lon=site.lon, buffer_km=site.buffer_km,
                coordinate_provenance=site.coordinate_provenance,
                seasonality=site.seasonality))
            .set_project(EE_PROJECT)
            .add_dataset(DatasetRecord(
                collection_id="COPERNICUS/S2_SR_HARMONIZED", role="optical",
                scenes_used=sufficiency.get("s2_scenes"),
                date_start=current_win["start"], date_end=current_win["end"],
                filters=["CLOUDY_PIXEL_PERCENTAGE < 40"],
                masking="SCL classes 3, 8, 9, 10, 11 removed"))
            .add_dataset(DatasetRecord(
                collection_id="COPERNICUS/S1_GRD", role="radar",
                scenes_used=sufficiency.get("s1_scenes", n_s1),
                date_start=current_win["start"], date_end=current_win["end"],
                filters=["instrumentMode=IW", "VV+VH", "single orbit pass"],
                notes="50 m focal median despeckle; display range -20..+5 dB"))
            .set_epochs(
                baseline={"year": baseline_year, **baseline_win},
                current={"year": current_year, **current_win},
                rule=current_win["rule"],
                justification=current_win["justification"])
            .set_method(
                scale_m=20,
                screen_type="unsupervised multi-criteria threshold",
                criteria="NDVI low AND BSI high AND NDMI low AND VV rough",
                min_s2_observations=MIN_S2_OBSERVATIONS,
                min_aoi_coverage=MIN_AOI_COVERAGE,
                min_s1_scenes=MIN_S1_SCENES))

    for name, value in THRESHOLDS.items():
        prov.add_threshold(ThresholdRecord(
            name=name, value=value,
            derivation=("Hand-chosen screening cut, editable in THRESHOLDS. "
                        "Not derived from a distribution."),
            is_arbitrary=True))

    prov.caveat(
        "The disturbance screen is UNSUPERVISED and UNVALIDATED. It is not a "
        "mining footprint and carries no accuracy figure. Bare rock, sabkha, "
        "wadi beds, quarries, roads and construction all satisfy the same "
        "criteria. Run Stage 3 with hand-labelled polygons for a real "
        "classifier and a real precision/recall/IoU figure.")

    if sufficiency["verdict"].startswith("PARTIAL"):
        prov.warn(f"Partial data: {sufficiency['verdict']}")

    coverage = sufficiency.get("aoi_fraction_with_min_observations", 0)
    nominal = math.pi * (site.buffer_km * 1000) ** 2 / 1e4
    prov.set_sufficiency(DataSufficiency(
        observed_ha=round(nominal * coverage, 1),
        nominal_ha=round(nominal, 1),
        verdict=sufficiency["verdict"],
        detail=sufficiency))

    prov.set_value(coverage, "aoi_fraction_observed")
    return prov.to_dict()


# ==============================================================================
# 10. MAP ASSEMBLY
# ==============================================================================

def build_map(site: Site, aoi: ee.Geometry, s2_cur: dict, s1_cur: dict,
              screen: ee.Image, dndvi: ee.Image, s2_base: dict) -> geemap.Map:
    m = geemap.Map(center=[site.lat, site.lon], zoom=12)
    m.add_basemap("SATELLITE")

    cur = s2_cur["composite"]

    m.addLayer(cur, {"bands": ["B4", "B3", "B2"], "min": 0, "max": 0.3},
               "S2 true colour (current)")
    m.addLayer(cur, {"bands": ["B12", "B8", "B4"], "min": 0, "max": 0.4},
               "S2 SWIR composite (current)", False)
    m.addLayer(cur.select("NDVI"), {"min": -0.2, "max": 0.6,
               "palette": ["8c510a", "f6e8c3", "01665e"]}, "NDVI", False)
    m.addLayer(cur.select("BSI"), {"min": -0.3, "max": 0.4,
               "palette": ["01665e", "f6e8c3", "8c510a"]}, "BSI", False)
    m.addLayer(cur.select("MNDWI"), {"min": -0.5, "max": 0.5,
               "palette": ["8c510a", "ffffff", "2166ac"]}, "MNDWI", False)
    m.addLayer(cur.select("NDMI"), {"min": -0.5, "max": 0.5,
               "palette": ["a6611a", "f5f5f5", "018571"]}, "NDMI", False)

    # Display range verified in the field. The intuitive -25..0 range renders a
    # contrastless layer that looks EMPTY: disturbed mine ground backscatters
    # strongly and its values approach and exceed 0 dB, so everything saturates
    # white while the smooth desert saturates black. Measured VV at Ad Duwayhi
    # spans -26.7 to +4.1 dB.
    m.addLayer(s1_cur["composite"].select("VV"), {"min": -20, "max": 5},
               "S1 VV backscatter (dB)", False)
    m.addLayer(s1_cur["composite"].select("VH"), {"min": -28, "max": -2},
               "S1 VH backscatter (dB)", False)

    m.addLayer(screen, {"min": 0, "max": 4,
               "palette": ["000000", "3b4cc0", "f7f7f7", "f4a582", "b2182b"]},
               "CANDIDATE disturbance screen (0-4, UNVALIDATED)")

    m.addLayer(dndvi, {"min": -0.3, "max": 0.3,
               "palette": ["2166ac", "f7f7f7", "b2182b"]},
               "dNDVI (red = vegetation lost)")
    m.addLayer(vegetation_loss_mask(dndvi), {"palette": ["b2182b"]},
               "Vegetation loss > threshold", False)

    # Where the optical record is too thin to trust — shown, not hidden.
    insufficient = s2_cur["sufficient_mask"].Not().selfMask()
    m.addLayer(insufficient, {"palette": ["ff00ff"]},
               "INSUFFICIENT DATA (optical)", False)

    m.addLayer(ee.Image().paint(ee.FeatureCollection([ee.Feature(aoi)]), 0, 2),
               {"palette": ["ffff00"]}, "AOI boundary")

    m.add_text(
        f"{site.name} ({site.country}) — screening output, not a validated footprint",
        position="bottomleft",
    )
    return m


# ==============================================================================
# 11. MAIN
# ==============================================================================

def run(site_key: str, baseline_year: int, current_year: int,
        out_html: str, out_provenance: str) -> None:
    site = SITES[site_key]
    init_ee()

    aoi = build_aoi(site)
    base_win = build_window(site, baseline_year)
    cur_win = build_window(site, current_year)

    print("\nEcoMine Observatory — Step 1")
    print(f"Site        : {site.name}, {site.country}")
    print(f"Commodity   : {site.commodity}")
    print(f"Window rule : {cur_win['rule']}")
    print(f"  -> {cur_win['justification']}\n")

    s2_base = s2_composite(aoi, base_win["start"], base_win["end"])
    s2_cur = s2_composite(aoi, cur_win["start"], cur_win["end"])
    s1_cur = s1_composite(aoi, cur_win["start"], cur_win["end"])

    sufficiency = assess_sufficiency(s2_cur, s1_cur, aoi)
    print("Data sufficiency check")
    for k, v in sufficiency.items():
        print(f"  {k:38s}: {v}")

    if sufficiency["verdict"].startswith("INSUFFICIENT"):
        print(
            "\nINSUFFICIENT DATA for this site-year. No indicators produced.\n"
            "This is the intended behaviour: widen the window, relax the cloud "
            "filter, or choose another year. The tool does not fill gaps.\n"
        )
        sys.exit(1)

    screen = disturbance_screen(s2_cur["composite"], s1_cur["composite"])
    dndvi = delta_ndvi(s2_base["composite"], s2_cur["composite"])

    prov = build_provenance(site, base_win, cur_win, sufficiency,
                            baseline_year, current_year)
    with open(out_provenance, "w", encoding="utf-8") as fh:
        json.dump(prov, fh, indent=2, ensure_ascii=False)
    print(f"\nProvenance written to {out_provenance}")

    m = build_map(site, aoi, s2_cur, s1_cur, screen, dndvi, s2_base)
    m.to_html(out_html, title=f"EcoMine Observatory — {site.name}")
    print(f"Interactive map written to {out_html}")
    print(
        "\nREMINDER: the disturbance layer is a CANDIDATE screen. It is not a "
        "mining footprint and carries no accuracy figure. Hand-label a sample "
        "and run train_rf_from_labels() before making any detection claim.\n"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="EcoMine Observatory — Step 1 demo")
    p.add_argument("--site", default=DEFAULT_SITE, choices=list(SITES))
    p.add_argument("--baseline-year", type=int, default=2018)
    p.add_argument("--current-year", type=int, default=2025)
    p.add_argument("--out-html", default="ecomine_step1_map.html")
    p.add_argument("--out-provenance", default="ecomine_step1_provenance.json")
    p.add_argument("--list", action="store_true", help="list preset sites and exit")
    args = p.parse_args()

    if args.list:
        for k, s in SITES.items():
            print(f"\n{k}\n  {s.name} — {s.country} ({s.commodity})")
            print(f"  {s.lat:.4f}, {s.lon:.4f}  buffer {s.buffer_km} km"
                  f"  [{s.seasonality}]")
            print(f"  {s.notes}")
        return

    run(args.site, args.baseline_year, args.current_year,
        args.out_html, args.out_provenance)


if __name__ == "__main__":
    main()
