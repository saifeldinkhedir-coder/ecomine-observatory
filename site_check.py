"""
EcoMine Observatory - candidate site diagnostic
================================================
Run BEFORE committing to a site. Answers one question per candidate:

    "Will this site actually activate the indicators I need?"

WHY THIS EXISTS
---------------
Two lessons paid for in this project:

  1. An estimated coordinate was wrong by 176 km at Ad Duwayhi. Every number
     computed over the wrong box would have been meaningless and internally
     consistent, which is the dangerous combination. Coordinates get visually
     confirmed before anything is built on them.

  2. Ad Duwayhi cannot compute MEI at all: VLI is gated off (no vegetation)
     and WDI has no surface water to measure. That was discovered after the
     pipeline was built. This script checks the applicability gates FIRST, so
     a site is rejected in two minutes rather than after a week of work.

WHAT IT REPORTS PER SITE
------------------------
    NOTE ON THRESHOLDS
    The WDI screening gates (5 ha AND 0.5% of AOI) are hand-chosen to separate
    reflective-surface noise from real water on the sites seen so far. They are
    a screening heuristic, not a derived quantity, and would be recorded as
    arbitrary in a provenance record. Revisit them as more sites are added.

    scene availability     - is there enough imagery at all
    mean baseline NDVI     - does VLI clear its 0.15 vegetation floor
    water-like area        - does WDI have anything to measure
    BSI contrast           - is the mine separable from its surroundings
    seasonality handling   - which imaging window rule applies and why

It writes a map for visual confirmation and prints a verdict per indicator.
It computes no change indicators: this is site selection, not analysis.

RUN
---
    python site_check.py                    # all candidates
    python site_check.py --site kriel       # one
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import ee
import geemap

from ecomine_core import VLI_MIN_BASELINE_NDVI, WATER_MNDWI_MIN


# ==============================================================================
# CANDIDATES
# ==============================================================================
# Every entry states where its coordinates came from and how much to trust
# them. A coordinate with no stated provenance eventually gets treated as
# surveyed by somebody.

CANDIDATES = {
    "kriel": {
        "name": "Kriel Colliery (opencast + underground)",
        "country": "South Africa",
        "lat": -26.249091,
        "lon": 29.205038,
        "buffer_km": 8.0,
        "commodity": "bituminous coal",
        "seasonality": "seasonal",
        "dry_season_months": (8, 9),
        "coordinate_provenance": (
            "South African Coal Desktop Study, Annexure Q (Centre for "
            "Environmental Rights), which gives Lat 26.249091S / Long "
            "29.205038E for Kriel Colliery. A published document rather than a "
            "map estimate, but NOT surveyed and NOT visually confirmed yet."
        ),
        "why": (
            "Highveld grassland should clear the VLI vegetation floor. "
            "Opencast coal pits commonly hold pit water, which WDI needs. "
            "Genuine summer cloud season (Oct-Mar) is where Sentinel-1's "
            "cloud penetration can finally be demonstrated - it cannot be at "
            "Ad Duwayhi, where skies are near-permanently clear."
        ),
    },
    "koornfontein": {
        "name": "Koornfontein Coal Mine (opencast + underground)",
        "country": "South Africa",
        "lat": -26.0999993,
        "lon": 29.4245782,
        "buffer_km": 8.0,
        "commodity": "bituminous coal",
        "seasonality": "seasonal",
        "dry_season_months": (8, 9),
        "coordinate_provenance": (
            "Global Energy Monitor wiki, explicitly labelled APPROXIMATE. "
            "Lower confidence than Kriel. Treat as a starting point for "
            "visual search, not as a location."
        ),
        "why": "Alternative Witbank-coalfield candidate if Kriel disappoints.",
    },
    "isibonelo": {
        "name": "Isibonelo Colliery (opencast thermal coal) [COORDS WRONG]",
        "country": "South Africa",
        "lat": -26.35,
        "lon": 29.10,
        "buffer_km": 6.0,
        "commodity": "thermal coal",
        "seasonality": "seasonal",
        "dry_season_months": (8, 9),
        "coordinate_provenance": (
            "WRONG LOCATION - DO NOT USE. The placeholder (-26.35, 29.10) was "
            "visually checked on 8 Aug 2026 and lands on a poultry/livestock "
            "farm surrounded by cropland, not a mine: long white barn roofs, "
            "no pit, no benches. The real Isibonelo is elsewhere. Retained in "
            "the list only as a reminder that a placeholder from a text "
            "description is a guess, not a location, and must be visually "
            "confirmed - exactly as the 176 km Ad Duwayhi error taught."
        ),
        "why": (
            "SUPERSEDED by Kangala, which is visually confirmed. Correct these "
            "coordinates from Google Maps before any use, or remove the entry."
        ),
    },
    "kangala": {
        "name": "Kangala Coal Mine (opencast)",
        "country": "South Africa",
        "lat": -26.188176,
        "lon": 28.669960,
        "buffer_km": 4.0,
        "commodity": "thermal coal",
        "seasonality": "seasonal",
        "dry_season_months": (8, 9),
        "coordinate_provenance": (
            "User-confirmed from Google Maps on 8 Aug 2026 (Kangala Coal Mine, "
            "Delmas). VISUALLY VERIFIED: dark opencast coal pits, two mine-water "
            "ponds, processing stockpiles, NO adjacent power station or ash dam "
            "- a clean standalone opencast, unlike Kriel. Earlier placeholder "
            "(-26.25, 28.85) was ~20 km off and is discarded. Buffer tightened "
            "to 4 km to reduce the surrounding farmland captured in the AOI."
        ),
        "why": (
            "Newer standalone opencast (first coal 2015), so Sentinel-2 and "
            "Landsat both span its full pre-mining-to-present history. "
            "Visually confirmed clean of the power-station/ash-dam "
            "contamination that ruled out Kriel."
        ),
    },
    "ad_duwayhi": {
        "name": "Ad Duwayhi gold mine (reference - already characterised)",
        "country": "Saudi Arabia",
        "lat": 22.29799,
        "lon": 43.26475,
        "buffer_km": 6.0,
        "commodity": "gold",
        "seasonality": "hyper_arid",
        "dry_season_months": None,
        "coordinate_provenance": (
            "Mindat, VISUALLY CONFIRMED 1 Aug 2026 against Esri World "
            "Imagery. Not surveyed."
        ),
        "why": (
            "Included as a control for this diagnostic itself: we already know "
            "VLI must fail and WDI must find nothing here. If the script says "
            "otherwise, the script is wrong."
        ),
    },
}


def init_ee() -> str:
    env = Path(".env")
    if not env.exists():
        sys.exit("No .env file. Create one containing EE_PROJECT=your-id")
    proj = ""
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("EE_PROJECT="):
            proj = line.split("=", 1)[1].strip().strip("\"'")
    if not proj:
        sys.exit("EE_PROJECT not set in .env")
    ee.Initialize(project=proj)
    return proj


# ==============================================================================
# IMAGERY
# ==============================================================================

def window_for(site: dict, year: int) -> dict:
    """
    Seasonal sites use the end-of-dry-season window; hyper-arid sites do not,
    because there is no wet season to wait out and the rule would discard most
    usable imagery for no gain. The deviation is recorded, never silent.
    """
    if site["seasonality"] == "seasonal" and site["dry_season_months"]:
        m0, m1 = site["dry_season_months"]
        return {
            "start": f"{year}-{m0:02d}-01",
            "end": f"{year}-{m1:02d}-28",
            "rule": "end_of_dry_season",
            "why": (f"Months {m0}-{m1} are the end of the dry season here. "
                    "Senescent vegetation minimises spectral obscuration of "
                    "disturbed ground."),
        }
    return {
        "start": f"{year}-01-01",
        "end": f"{year}-12-31",
        "rule": "low_cloud_annual",
        "why": ("Hyper-arid: no meaningful wet season, so the seasonal rule "
                "from Sahelian/tropical literature does not apply."),
    }


def mask_s2(img):
    scl = img.select("SCL")
    bad = (scl.eq(3).Or(scl.eq(8)).Or(scl.eq(9))
           .Or(scl.eq(10)).Or(scl.eq(11)))
    return img.updateMask(bad.Not()).divide(10000)


def composite(aoi, start, end):
    coll = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(aoi).filterDate(start, end)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 40))
            .map(mask_s2))
    n = coll.size().getInfo()
    if n == 0:
        return None, 0
    im = coll.median().clip(aoi)
    ndvi = im.normalizedDifference(["B8", "B4"]).rename("NDVI")
    mndwi = im.normalizedDifference(["B3", "B11"]).rename("MNDWI")
    bsi = im.expression(
        "((S+R)-(N+B))/((S+R)+(N+B))",
        {"S": im.select("B11"), "R": im.select("B4"),
         "N": im.select("B8"), "B": im.select("B2")}).rename("BSI")
    return im.addBands([ndvi, mndwi, bsi]), n


def industrial_signature(aoi, year):
    """
    Detect the two industrial signatures that contaminated Kriel, so no future
    site is adopted on numbers alone without this being flagged.

    Both were found by eye at Kriel and cost real analysis time; this turns
    that lesson into an automatic check.

      HEAT  - a coal-fired power station runs hot. Landsat surface temperature
              (ST_B10) shows a persistent anomaly a mine pit does not produce.
              We report the 98th-percentile land-surface temperature and how
              far it sits above the AOI median. A large gap means a combustion
              source (power station, smelter, coal-seam fire).

      GLARE - an ash or tailings dam is anomalously bright and highly
              reflective in SWIR, far more than disturbed rock. We measure the
              fraction of the AOI that is both very bright (high visible
              reflectance) AND high-SWIR, the signature of fine light-coloured
              waste rather than excavated ground.

    Neither is a mining signal. A site where either is high is an
    industrial-complex AOI, not a clean mine, and MEI computed over it will
    measure power-plant waste, not mining impact.
    """
    # --- HEAT: Landsat 8/9 Collection 2 Level-2 surface temperature ---
    def scale_lst(img):
        # ST_B10 scaling to kelvin, then to celsius, masked to good quality
        st = img.select("ST_B10").multiply(0.00341802).add(149.0).subtract(273.15)
        return st.rename("LST").updateMask(
            img.select("ST_B10").gt(0))

    lst_coll = (ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
                .merge(ee.ImageCollection("LANDSAT/LC09/C02/T1_L2"))
                .filterBounds(aoi)
                .filterDate(f"{year}-01-01", f"{year}-12-31")
                .filter(ee.Filter.lt("CLOUD_COVER", 40))
                .map(scale_lst))
    n_lst = lst_coll.size().getInfo()
    heat = {"scenes": n_lst, "lst_p50": None, "lst_p98": None,
            "lst_excess": None}
    if n_lst > 0:
        lst = lst_coll.select("LST").median().clip(aoi)
        r = lst.reduceRegion(
            ee.Reducer.percentile([50, 98]), aoi, 30,
            maxPixels=int(1e9), bestEffort=True).getInfo()
        heat["lst_p50"] = r.get("LST_p50")
        heat["lst_p98"] = r.get("LST_p98")
        if heat["lst_p50"] is not None and heat["lst_p98"] is not None:
            heat["lst_excess"] = heat["lst_p98"] - heat["lst_p50"]

    # --- GLARE: bright + high-SWIR fraction from the same S2 composite ---
    s2 = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
          .filterBounds(aoi).filterDate(f"{year}-01-01", f"{year}-12-31")
          .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 40))
          .map(mask_s2).median().clip(aoi))
    # very bright in visible AND high SWIR: fine light waste, not dark coal or
    # ordinary excavated rock
    bright = s2.select("B4").gt(0.30).And(s2.select("B11").gt(0.30))
    px = ee.Image.pixelArea()
    ga = ee.Image.cat([
        bright.multiply(px).rename("glare_m2"),
        s2.select("B4").mask().multiply(px).rename("obs_m2"),
    ]).reduceRegion(ee.Reducer.sum(), aoi, 20,
                    maxPixels=int(1e9), bestEffort=True).getInfo()
    glare_ha = (ga.get("glare_m2") or 0) / 1e4
    obs_ha = (ga.get("obs_m2") or 0) / 1e4
    glare_pct = glare_ha / obs_ha * 100 if obs_ha else 0

    return {"heat": heat, "glare_ha": glare_ha, "glare_pct": glare_pct}


# ==============================================================================
# DIAGNOSTIC
# ==============================================================================

def diagnose(key: str, site: dict, year: int, write_map: bool) -> dict:
    aoi = ee.Geometry.Point([site["lon"], site["lat"]]).buffer(
        site["buffer_km"] * 1000)
    win = window_for(site, year)

    print("\n" + "=" * 66)
    print(f"{site['name']}")
    print(f"{site['country']}  |  {site['lat']}, {site['lon']}  "
          f"|  {site['buffer_km']} km buffer")
    print("=" * 66)
    print(f"coordinates : {site['coordinate_provenance']}")
    print(f"window rule : {win['rule']}")
    print(f"              {win['why']}")

    img, n = composite(aoi, win["start"], win["end"])
    if img is None:
        print(f"\n  INSUFFICIENT DATA: 0 Sentinel-2 scenes in "
              f"{win['start']}..{win['end']}")
        return {"key": key, "usable": False}

    s1n = (ee.ImageCollection("COPERNICUS/S1_GRD")
           .filterBounds(aoi).filterDate(win["start"], win["end"])
           .filter(ee.Filter.eq("instrumentMode", "IW")).size().getInfo())
    print(f"\nimagery     : {n} Sentinel-2 scenes, {s1n} Sentinel-1 scenes")

    # Three separate, simple reductions rather than one combined reducer.
    # Combining mean().forBand() with percentile and sum across bands is a
    # construction that only fails at getInfo() time, and a diagnostic that
    # crashes is worse than one that costs an extra request.
    px = ee.Image.pixelArea()

    means = img.select(["NDVI", "BSI"]).reduceRegion(
        ee.Reducer.mean(), aoi, 20,
        maxPixels=int(1e9), bestEffort=True).getInfo()

    pcts = img.select("BSI").reduceRegion(
        ee.Reducer.percentile([50, 95]), aoi, 20,
        maxPixels=int(1e9), bestEffort=True).getInfo()

    areas = ee.Image.cat([
        img.select("MNDWI").gt(WATER_MNDWI_MIN).unmask(0)
           .multiply(px).rename("water_m2"),
        img.select("NDVI").mask().multiply(px).rename("obs_m2"),
    ]).reduceRegion(
        ee.Reducer.sum(), aoi, 20,
        maxPixels=int(1e9), bestEffort=True).getInfo()

    ndvi_mean = means.get("NDVI")
    bsi_p50 = pcts.get("BSI_p50")
    bsi_p95 = pcts.get("BSI_p95")
    water_ha = (areas.get("water_m2") or 0) / 1e4
    obs_ha = (areas.get("obs_m2") or 0) / 1e4

    result = {"key": key, "usable": True, "s2": n, "s1": s1n,
              "ndvi_mean": ndvi_mean, "water_ha": water_ha,
              "obs_ha": obs_ha, "bsi_p50": bsi_p50, "bsi_p95": bsi_p95}

    print("\nINDICATOR APPLICABILITY")
    print("-" * 66)

    # VLI
    if ndvi_mean is None:
        print("  VLI  UNKNOWN        no NDVI observations")
        result["vli"] = "UNKNOWN"
    elif ndvi_mean >= VLI_MIN_BASELINE_NDVI:
        print(f"  VLI  ACTIVE         mean NDVI {ndvi_mean:.4f} "
              f">= {VLI_MIN_BASELINE_NDVI} floor")
        result["vli"] = "ACTIVE"
    else:
        print(f"  VLI  NOT APPLICABLE mean NDVI {ndvi_mean:.4f} "
              f"< {VLI_MIN_BASELINE_NDVI} floor - no vegetation to lose")
        result["vli"] = "NOT APPLICABLE"

    # WDI
    # Two gates, both required. A fixed absolute floor alone is wrong: at
    # Ad Duwayhi 1.3 ha of highly reflective plant surfaces cleared a 1 ha
    # floor and wrongly reported water in a hyper-arid cyanide-leach mine with
    # no surface water at all. The relative gate rejects that: 0.01% of the AOI
    # is noise, while Kriel's 2.44% is a real signal. The absolute gate guards
    # the other direction, so a tiny AOI cannot turn a few wet pixels into a
    # large-looking percentage.
    water_pct = water_ha / obs_ha * 100 if obs_ha else 0
    WDI_MIN_HA = 5.0        # below this, treat as reflective-surface noise
    WDI_MIN_PCT = 0.5       # AND must be a non-trivial fraction of the AOI
    if water_ha < WDI_MIN_HA or water_pct < WDI_MIN_PCT:
        print(f"  WDI  NO SIGNAL      {water_ha:.1f} ha water-like "
              f"({water_pct:.2f}%) - below {WDI_MIN_HA} ha / {WDI_MIN_PCT}% "
              "floor; likely reflective surfaces, not water")
        result["wdi"] = "NO SIGNAL"
    else:
        print(f"  WDI  ACTIVE         {water_ha:,.1f} ha water-like "
              f"({water_pct:.2f}% of observed)")
        result["wdi"] = "ACTIVE"

    # SEI always works, but report the contrast available
    if bsi_p50 is not None and bsi_p95 is not None:
        spread = bsi_p95 - bsi_p50
        print(f"  SEI  ACTIVE         BSI p50 {bsi_p50:+.4f}, "
              f"p95 {bsi_p95:+.4f}, spread {spread:.4f}")
        result["sei"] = "ACTIVE"

    # MEI
    active = sum(1 for k in ("vli", "wdi", "sei") if result.get(k) == "ACTIVE")
    print("-" * 66)
    if active >= 2:
        print(f"  MEI  COMPUTABLE     {active}/3 components active")
        result["mei"] = f"COMPUTABLE ({active}/3)"
    else:
        print(f"  MEI  NOT COMPUTABLE only {active}/3 components active; "
              "needs at least 2")
        result["mei"] = "NOT COMPUTABLE"

    # --- INDUSTRIAL SIGNATURES (DORMANT - reports numbers, issues NO verdict) ---
    #
    # STATUS: dormant by design. An earlier version of this block issued a
    # CONTAMINATED / clean verdict and FAILED BOTH of its own controls: it
    # passed Kriel (a power station + ash dam, known dirty) as clean, and
    # flagged Ad Duwayhi (open desert, known clean) as contaminated at 37.9%
    # "glare" - because bright desert sand reads the same as light industrial
    # waste, and an annual-median surface temperature washes out the point-
    # source heat of a power station.
    #
    # The lesson: "industrial contamination" is not a single spectral class.
    # Ash dams look like sabkha and sand; smelters look like dark rock. A
    # reliable detector needs a supervised classifier trained on labelled
    # ground truth - the kind of field data expected from SCMR (the Sudanese
    # Mineral Resources Company) and comparable sources. Until that arrives,
    # this block ONLY reports raw numbers as context for the human doing the
    # visual check. It sets no flag and drives no decision.
    #
    # DO NOT re-enable a verdict here without validating it against BOTH
    # controls first: Kriel must read dirty and Ad Duwayhi must read clean.
    ind = industrial_signature(aoi, year)
    result["industrial"] = ind
    result["contaminated"] = None   # unknown by automated means - see note

    print("\nINDUSTRIAL SIGNATURES (context only - NO automated verdict)")
    print("-" * 66)
    h = ind["heat"]
    if h["scenes"] == 0 or h["lst_excess"] is None:
        print("  HEAT   no usable Landsat thermal statistic")
    else:
        print(f"  HEAT   p98 {h['lst_p98']:.1f}C, {h['lst_excess']:.1f}C "
              "above median")
    print(f"  GLARE  {ind['glare_ha']:,.0f} ha bright+high-SWIR "
          f"({ind['glare_pct']:.1f}%)")
    print("-" * 66)
    print("  These numbers are context for VISUAL confirmation, not a verdict.")
    print("  Automated contamination detection is deferred until labelled")
    print("  ground-truth data (e.g. SCMR field records) allows a supervised")
    print("  classifier. Bright desert and ash dams are not separable by a")
    print("  fixed threshold - this was verified against both controls.")

    if write_map:
        m = geemap.Map(center=[site["lat"], site["lon"]], zoom=12)
        m.add_basemap("SATELLITE")
        m.addLayer(img, {"bands": ["B4", "B3", "B2"], "min": 0, "max": 0.3},
                   "S2 true colour")
        m.addLayer(img.select("BSI"), {"min": -0.3, "max": 0.4,
                   "palette": ["01665e", "f6e8c3", "8c510a"]}, "BSI", False)
        m.addLayer(img.select("NDVI"), {"min": -0.2, "max": 0.8,
                   "palette": ["8c510a", "f6e8c3", "01665e"]}, "NDVI", False)
        m.addLayer(img.select("MNDWI").gt(WATER_MNDWI_MIN).selfMask(),
                   {"palette": ["2166ac"]}, "water-like", False)
        m.addLayer(ee.Image().paint(
            ee.FeatureCollection([ee.Feature(aoi)]), 0, 2),
            {"palette": ["ffff00"]}, "AOI")
        out = f"sitecheck_{key}.html"
        m.to_html(out, title=site["name"])
        print(f"\n  map written: {out}")
        print("  CONFIRM VISUALLY before using this site for anything.")

    return result


def main():
    p = argparse.ArgumentParser(description="EcoMine candidate site diagnostic")
    p.add_argument("--site", choices=list(CANDIDATES), default=None)
    p.add_argument("--year", type=int, default=2024)
    p.add_argument("--no-map", action="store_true")
    a = p.parse_args()

    proj = init_ee()
    print(f"EcoMine site diagnostic  |  project {proj}  |  year {a.year}")

    keys = [a.site] if a.site else list(CANDIDATES)
    results = [diagnose(k, CANDIDATES[k], a.year, not a.no_map) for k in keys]

    print("\n" + "=" * 66)
    print("SUMMARY")
    print("=" * 66)
    print(f"{'SITE':<14}{'VLI':<15}{'WDI':<11}{'MEI':<18}")
    print("-" * 66)
    for r in results:
        if not r.get("usable"):
            print(f"{r['key']:<14}{'INSUFFICIENT DATA':<50}")
            continue
        print(f"{r['key']:<14}{r.get('vli','-'):<15}"
              f"{r.get('wdi','-'):<11}{r.get('mei','-'):<18}")
    print("\nA site is only worth adopting if BOTH hold:")
    print("  1. MEI is computable")
    print("  2. coordinates AND surroundings visually confirmed on the map")
    print("     (is it a clean mine, or a mine tangled with a power station,")
    print("      ash dam, smelter or town? the eye decides this, not the tool)")
    print("\nAutomated industrial-contamination detection is DEFERRED until")
    print("labelled ground-truth data is available. It failed both controls")
    print("when tried (passed dirty Kriel, flagged clean Ad Duwayhi), so no")
    print("automated CLEAN/DIRTY verdict is issued. Visual confirmation is")
    print("currently the only reliable filter - as it was for Kriel.")


if __name__ == "__main__":
    main()
