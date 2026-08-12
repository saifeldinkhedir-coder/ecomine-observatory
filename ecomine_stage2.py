"""
EcoMine Observatory — STAGE 2: impact indicators + change over time
====================================================================

Companion to ecomine_step1.py (Stage 1). Moves from "where is the mine" to
"what has changed, and by how much".

Author: Seifeldin M.G. Alkhedir (ORCID 0000-0003-0821-2991) · Licence: GPL-3.0

INDICATORS PRODUCED  (brief S5.2)
---------------------------------
  VLI  Vegetation Loss Index    fraction of AOI with significant NDVI loss
  SEI  Surface Exposure Index   fraction of AOI with significant BSI gain
  WDI  Water Disturbance Index  change in water-like area + turbidity proxy
  MEI  Mining Environmental     weighted composite of the above
       Impact

Each is reported with: a value, an area in hectares, the data coverage behind
it, the threshold used, and an applicability verdict. Any of them may return
INSUFFICIENT DATA or NOT APPLICABLE instead of a number. That is a feature.

THE NORMALISATION PROBLEM, AND HOW THIS MODULE HANDLES IT
---------------------------------------------------------
Relative change (dNDVI / NDVI_baseline) is the obvious formulation and it is
wrong in arid terrain. Where baseline NDVI is ~0.08, a physically trivial
absolute change of 0.04 becomes a 50% "loss". The indicator would then measure
aridity, not mining.

Fixed absolute thresholds fail the other way: a 0.10 NDVI drop is noise in a
Highveld grassland and a real signal in hyper-arid desert. One number cannot
serve both.

This module instead derives the threshold FROM EACH SITE'S OWN NOISE FLOOR:

  1. Compute the change image (baseline minus current) across the whole AOI.
  2. Estimate a ROBUST null distribution from that image using percentiles --
     median for centre, (p84 - p16) / 2 as a robust sigma. Robust statistics are
     used because the disturbed pixels are outliers that would inflate an
     ordinary mean and standard deviation.
  3. Set threshold = median + k * robust_sigma  (k = 2.0 by default).
  4. Count only pixels exceeding that site-specific threshold.

The result is comparable across biomes because it is expressed in units of each
site's own natural variability, not in raw index units.

  ASSUMPTION, STATED PLAINLY: this requires that disturbed pixels are a MINORITY
  of the AOI. If mining covers more than roughly 30% of the AOI, the "null"
  distribution is contaminated by the signal, the threshold inflates, and the
  indicator under-reports. The module checks for this and warns. Fix by
  enlarging the AOI buffer, not by adjusting k until the answer looks right.

APPLICABILITY GATES
-------------------
An indicator that cannot work at a site should say so rather than return a
confident number. VLI is gated on baseline vegetation: where mean baseline NDVI
over the AOI is below VLI_MIN_BASELINE_NDVI, there is essentially no vegetation
to lose, and VLI is reported NOT APPLICABLE. At Ad Duwayhi and Mahd adh Dhahab
this gate is expected to fire. That is the correct scientific answer for a
hyper-arid site, and reporting a VLI there anyway would be the over-claim.

WHAT THIS MODULE DOES NOT DO
----------------------------
  * It does not attribute change to mining. It measures change inside an AOI
    that contains a mine. Drought, flood, agriculture, road building and urban
    growth all produce the same signals. Attribution needs the Stage 3
    classifier plus ground truth.
  * It emits no legal or compliance conclusion (brief S8).
  * It does not interpolate across gaps.

RUN
---
    python ecomine_stage2.py --site ad_duwayhi --baseline-year 2018 --current-year 2025
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Optional

import ee
import geemap

from ecomine_core import (
    K_SIGMA,
    VLI_MIN_BASELINE_NDVI,
    NULL_CONTAMINATION_WARN,
    WATER_MNDWI_MIN,
    MEI_WEIGHTS,
    robust_sigma,
    adaptive_threshold as core_threshold,
    is_null_contaminated,
    vli_applicable,
    windows_comparable,
    compose_mei,
)
from ecomine_step1 import (
    SITES,
    init_ee,
    build_aoi,
    build_window,
    s2_composite,
    s1_composite,
    assess_sufficiency,
)


# ==============================================================================
# CONFIGURATION
# ==============================================================================

# K_SIGMA, VLI_MIN_BASELINE_NDVI, NULL_CONTAMINATION_WARN, WATER_MNDWI_MIN and
# MEI_WEIGHTS are defined once in ecomine_core and imported above. Tune them
# there, and note that K_SIGMA changes every number downstream.

#: Analysis scale in metres. 20 m keeps reduceRegion tractable on free GEE quota
#: while staying close to Sentinel-2 native resolution.
SCALE_M = 20


# ==============================================================================
# ROBUST STATISTICS AND ADAPTIVE THRESHOLDS
# ==============================================================================

def robust_null(delta: ee.Image, aoi: ee.Geometry, band: str) -> dict:
    """
    Robust centre and spread of a change image, via percentiles.

    Returns median, robust_sigma = (p84 - p16) / 2, and the percentile values
    themselves so a reviewer can see the distribution the threshold came from.
    """
    stats = delta.select(band).reduceRegion(
        reducer=ee.Reducer.percentile([16, 50, 84]),
        geometry=aoi,
        scale=SCALE_M,
        maxPixels=1e9,
        bestEffort=True,
    ).getInfo()

    p16 = stats.get(f"{band}_p16")
    p50 = stats.get(f"{band}_p50")
    p84 = stats.get(f"{band}_p84")

    if p16 is None or p50 is None or p84 is None:
        return {"available": False}

    sigma = robust_sigma(p16, p84)
    return {
        "available": True,
        "p16": round(p16, 5),
        "median": round(p50, 5),
        "p84": round(p84, 5),
        "robust_sigma": round(sigma, 5),
    }


def adaptive_threshold(null: dict, k: float = K_SIGMA) -> Optional[float]:
    """Thin wrapper over ecomine_core.adaptive_threshold, handling absent stats."""
    if not null.get("available"):
        return None
    return core_threshold(null["median"], null["robust_sigma"], k)


def area_exceeding(delta: ee.Image, band: str, threshold: float,
                   aoi: ee.Geometry) -> dict:
    """
    Fraction of the observed AOI, and hectares, where the change exceeds the
    threshold. Denominator is OBSERVED area, not nominal AOI area -- dividing by
    nominal area would silently treat masked pixels as unchanged.
    """
    d = delta.select(band)
    exceed = d.gt(threshold)
    observed = d.mask()

    px_area = ee.Image.pixelArea()
    res = (
        ee.Image.cat([
            exceed.multiply(px_area).rename("exceed_m2"),
            observed.multiply(px_area).rename("observed_m2"),
        ])
        .reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi, scale=SCALE_M,
            maxPixels=1e9, bestEffort=True,
        )
        .getInfo()
    )

    exceed_m2 = res.get("exceed_m2") or 0.0
    observed_m2 = res.get("observed_m2") or 0.0
    if observed_m2 <= 0:
        return {"available": False, "reason": "no observed pixels"}

    return {
        "available": True,
        "fraction_of_observed": round(exceed_m2 / observed_m2, 4),
        "hectares": round(exceed_m2 / 10000.0, 2),
        "observed_hectares": round(observed_m2 / 10000.0, 2),
        "threshold_used": round(threshold, 5),
    }


# ==============================================================================
# INDICATORS
# ==============================================================================

def compute_vli(base: ee.Image, cur: ee.Image, aoi: ee.Geometry) -> dict:
    """
    Vegetation Loss Index. Change image is baseline minus current, so positive
    values are vegetation LOST.

    Gated on baseline vegetation: in hyper-arid terrain there is nothing to
    lose, and the indicator returns NOT APPLICABLE rather than a spurious number.
    """
    baseline_ndvi = base.select("NDVI").reduceRegion(
        reducer=ee.Reducer.mean(), geometry=aoi, scale=SCALE_M,
        maxPixels=1e9, bestEffort=True,
    ).get("NDVI").getInfo()

    # Gate is evaluated by the unit-tested core function, not reimplemented here.
    applicable, gate_msg = vli_applicable(baseline_ndvi)
    if not applicable:
        status = ("INSUFFICIENT DATA" if gate_msg.startswith("INSUFFICIENT")
                  else "NOT APPLICABLE")
        return {
            "indicator": "VLI",
            "status": status,
            "mean_baseline_ndvi": (round(baseline_ndvi, 4)
                                   if baseline_ndvi is not None else None),
            "gate": VLI_MIN_BASELINE_NDVI,
            "reason": gate_msg + (
                " This is the expected result in hyper-arid terrain and is not "
                "an error." if status == "NOT APPLICABLE" else ""
            ),
        }

    delta = base.select("NDVI").subtract(cur.select("NDVI")).rename("dNDVI")
    delta = delta.updateMask(base.select("NDVI").mask().And(cur.select("NDVI").mask()))

    null = robust_null(delta, aoi, "dNDVI")
    thr = adaptive_threshold(null)
    if thr is None:
        return {"indicator": "VLI", "status": "INSUFFICIENT DATA",
                "reason": "could not estimate null distribution"}

    area = area_exceeding(delta, "dNDVI", thr, aoi)
    if not area.get("available"):
        return {"indicator": "VLI", "status": "INSUFFICIENT DATA",
                "reason": area.get("reason")}

    return {
        "indicator": "VLI",
        "status": "OK",
        "value": area["fraction_of_observed"],
        "hectares_lost": area["hectares"],
        "mean_baseline_ndvi": round(baseline_ndvi, 4),
        "null_distribution": null,
        "threshold_used": area["threshold_used"],
        "observed_hectares": area["observed_hectares"],
        "interpretation": (
            "Fraction of observed AOI where NDVI fell by more than the site's "
            "own noise floor. NOT attributable to mining without Stage 3."
        ),
        "_delta_image": delta,
    }


def compute_sei(base: ee.Image, cur: ee.Image, aoi: ee.Geometry) -> dict:
    """
    Surface Exposure Index. Change image is current minus baseline, so positive
    values are bare/exposed surface GAINED.

    This is the indicator that actually carries the signal in arid terrain,
    where VLI is mute.
    """
    delta = cur.select("BSI").subtract(base.select("BSI")).rename("dBSI")
    delta = delta.updateMask(base.select("BSI").mask().And(cur.select("BSI").mask()))

    null = robust_null(delta, aoi, "dBSI")
    thr = adaptive_threshold(null)
    if thr is None:
        return {"indicator": "SEI", "status": "INSUFFICIENT DATA",
                "reason": "could not estimate null distribution"}

    area = area_exceeding(delta, "dBSI", thr, aoi)
    if not area.get("available"):
        return {"indicator": "SEI", "status": "INSUFFICIENT DATA",
                "reason": area.get("reason")}

    return {
        "indicator": "SEI",
        "status": "OK",
        "value": area["fraction_of_observed"],
        "hectares_exposed": area["hectares"],
        "null_distribution": null,
        "threshold_used": area["threshold_used"],
        "observed_hectares": area["observed_hectares"],
        "interpretation": (
            "Fraction of observed AOI where bare-surface index rose beyond the "
            "site's noise floor. Quarrying, construction, road building and "
            "drought-driven vegetation dieback produce the same signal."
        ),
        "_delta_image": delta,
    }


def compute_wdi(base: ee.Image, cur: ee.Image, aoi: ee.Geometry) -> dict:
    """
    Water Disturbance Index: change in water-like surface area between epochs,
    plus an uncalibrated turbidity proxy over current water pixels.

    Reported as a signed area change AND separate gain/loss, because a new
    tailings pond and a dried-up wadi are different phenomena that a single
    net number would hide.
    """
    base_water = base.select("MNDWI").gt(WATER_MNDWI_MIN)
    cur_water = cur.select("MNDWI").gt(WATER_MNDWI_MIN)

    both_observed = base.select("MNDWI").mask().And(cur.select("MNDWI").mask())
    base_water = base_water.updateMask(both_observed)
    cur_water = cur_water.updateMask(both_observed)

    px_area = ee.Image.pixelArea()
    res = (
        ee.Image.cat([
            base_water.multiply(px_area).rename("base_m2"),
            cur_water.multiply(px_area).rename("cur_m2"),
            cur_water.And(base_water.Not()).multiply(px_area).rename("gain_m2"),
            base_water.And(cur_water.Not()).multiply(px_area).rename("loss_m2"),
            both_observed.multiply(px_area).rename("observed_m2"),
        ])
        .reduceRegion(reducer=ee.Reducer.sum(), geometry=aoi, scale=SCALE_M,
                      maxPixels=1e9, bestEffort=True)
        .getInfo()
    )

    observed_m2 = res.get("observed_m2") or 0.0
    if observed_m2 <= 0:
        return {"indicator": "WDI", "status": "INSUFFICIENT DATA",
                "reason": "no jointly observed pixels"}

    # Turbidity proxy: red reflectance over current water. Higher red generally
    # means more suspended sediment. UNCALIBRATED -- comparable between dates at
    # one site, not comparable between sites, and not a sediment concentration.
    turb = cur.select("B4").updateMask(cur_water).reduceRegion(
        reducer=ee.Reducer.mean(), geometry=aoi, scale=SCALE_M,
        maxPixels=1e9, bestEffort=True,
    ).get("B4").getInfo()

    gain_ha = (res.get("gain_m2") or 0.0) / 10000.0
    loss_ha = (res.get("loss_m2") or 0.0) / 10000.0

    return {
        "indicator": "WDI",
        "status": "OK",
        "value": round((gain_ha + loss_ha) * 10000.0 / observed_m2, 4),
        "water_hectares_baseline": round((res.get("base_m2") or 0.0) / 10000.0, 2),
        "water_hectares_current": round((res.get("cur_m2") or 0.0) / 10000.0, 2),
        "water_hectares_gained": round(gain_ha, 2),
        "water_hectares_lost": round(loss_ha, 2),
        "turbidity_proxy_red_reflectance": (
            round(turb, 5) if turb is not None else "INSUFFICIENT DATA"
        ),
        "observed_hectares": round(observed_m2 / 10000.0, 2),
        "interpretation": (
            "Value is total water-area change (gain + loss) as a fraction of "
            "observed AOI. Turbidity proxy is UNCALIBRATED red reflectance over "
            "water: usable for date-to-date comparison at one site, not as a "
            "sediment concentration and not comparable between sites."
        ),
        "_cur_water": cur_water,
        "_gain": cur_water.And(base_water.Not()).selfMask(),
    }


def compute_rdi(base_s1: ee.Image, cur_s1: ee.Image, aoi: ee.Geometry) -> dict:
    """
    Radar Disturbance Index. Two-epoch change in Sentinel-1 VV backscatter,
    thresholded against the site's own noise floor exactly as SEI is.

    Why radar and not terrain. The Stage 3 classifier ranked SRTM elevation and
    Sentinel-1 radar as its strongest features. Radar EARNS a place in MEI:
    ground roughness genuinely changes between two epochs when a pit is dug or
    a stockpile grows, so a baseline-vs-current VV difference is a change
    fraction of the same kind as SEI and WDI. SRTM does NOT: the DEM is a single
    ~2000 epoch, so it has no baseline-vs-current change to contribute; the
    classifier leaned on it only as a static location fingerprint of one pit,
    which is site-memorisation, not a mining signal. Forcing a single-epoch
    terrain layer into a composite of change fractions would be the same
    arithmetic-on-an-artefact error caught with the wide-AOI VLI, so SRTM stays
    a context layer and is deliberately NOT an MEI component.

    RDI is OPTIONAL and OFF by default. It is included in MEI only when the
    caller passes it, and even then MEI still declares it as a component so a
    3-of-3 and a 4-of-4 index are never confused. VV change also responds to
    soil-moisture and agriculture, so like every other component this is a
    screening signal inside an AOI, not attribution.
    """
    # Positive change = backscatter INCREASE (rougher/more disturbed surface).
    delta = cur_s1.select("VV").subtract(base_s1.select("VV")).rename("dVV")
    delta = delta.updateMask(
        base_s1.select("VV").mask().And(cur_s1.select("VV").mask()))

    null = robust_null(delta, aoi, "dVV")
    thr = adaptive_threshold(null)
    if thr is None:
        return {"indicator": "RDI", "status": "INSUFFICIENT DATA",
                "reason": "could not estimate null distribution"}

    area = area_exceeding(delta, "dVV", thr, aoi)
    if not area.get("available"):
        return {"indicator": "RDI", "status": "INSUFFICIENT DATA",
                "reason": area.get("reason")}

    return {
        "indicator": "RDI",
        "status": "OK",
        "value": area["fraction_of_observed"],
        "hectares_disturbed": area["hectares"],
        "null_distribution": null,
        "threshold_used": area["threshold_used"],
        "observed_hectares": area["observed_hectares"],
        "interpretation": (
            "Fraction of observed AOI where Sentinel-1 VV backscatter rose "
            "beyond the site's own radar noise floor between the two epochs. "
            "Rising VV indicates rougher or more disturbed ground (excavation, "
            "stockpiling), but soil-moisture change and tillage raise it too, "
            "so this is a screening signal, not attribution. Radar is "
            "cloud-independent, so this works where the optical indicators are "
            "masked."
        ),
        "_delta_image": delta,
    }


def compute_mei(vli: dict, sei: dict, wdi: dict,
                rdi: Optional[dict] = None) -> dict:
    """
    Composite. Averages only the components that returned OK, and states which
    were excluded -- an MEI built from one of three components is a different
    quantity from one built from three, and must not be presented as the same.
    """
    # RDI is appended only if the caller computed it; a 3-component MEI and a
    # 4-component MEI stay distinguishable because compose_mei records exactly
    # which components went in.
    candidates = [vli, sei, wdi] + ([rdi] if rdi is not None else [])
    parts, excluded = {}, {}
    for ind in candidates:
        name = ind["indicator"]
        if ind.get("status") == "OK":
            parts[name] = ind["value"]
        else:
            excluded[name] = ind.get("status")

    # Composition and renormalisation are handled by the unit-tested core
    # function; this wrapper only labels the result.
    result = compose_mei(parts, excluded)
    result["indicator"] = "MEI"
    return result


# ==============================================================================
# CONTAMINATION CHECK
# ==============================================================================

def check_null_contamination(indicators: list[dict]) -> list[str]:
    """Warn where the disturbed fraction is large enough to bias its own null."""
    warnings = []
    for ind in indicators:
        if ind.get("status") == "OK" and is_null_contaminated(ind.get("value", 0)):
            warnings.append(
                f"{ind['indicator']}: {ind['value']:.1%} of the AOI exceeds "
                f"threshold, above the {NULL_CONTAMINATION_WARN:.0%} guard. The "
                "robust null was estimated from an AOI where change is no longer "
                "a minority, so the threshold is inflated and this value is "
                "likely an UNDER-estimate. Enlarge the AOI buffer and re-run. Do "
                "not tune K_SIGMA to compensate."
            )
    return warnings


# ==============================================================================
# MAIN
# ==============================================================================

def run(site_key: str, baseline_year: int, current_year: int,
        out_json: str, out_html: str, use_radar: bool = False) -> None:
    site = SITES[site_key]
    init_ee()
    aoi = build_aoi(site)

    base_win = build_window(site, baseline_year)
    cur_win = build_window(site, current_year)

    # Seasonal matching is not optional. Comparing a dry-season baseline against
    # an annual current composite measures phenology, not mining.
    comparable, why = windows_comparable(base_win["rule"], cur_win["rule"])
    if not comparable:
        print(f"ABORT: {why}")
        sys.exit(1)

    print("\nEcoMine Observatory — Stage 2: impact indicators")
    print(f"Site     : {site.name}, {site.country}")
    print(f"Epochs   : {baseline_year} -> {current_year}  [{cur_win['rule']}]")

    base = s2_composite(aoi, base_win["start"], base_win["end"])
    cur = s2_composite(aoi, cur_win["start"], cur_win["end"])
    s1_cur = s1_composite(aoi, cur_win["start"], cur_win["end"])

    suff_cur = assess_sufficiency(cur, s1_cur, aoi)
    print(f"\nCurrent-epoch sufficiency: {suff_cur['verdict']}")
    if suff_cur["verdict"].startswith("INSUFFICIENT"):
        print("No indicators produced. The tool does not fill gaps.")
        sys.exit(1)

    b, c = base["composite"], cur["composite"]
    vli = compute_vli(b, c, aoi)
    sei = compute_sei(b, c, aoi)
    wdi = compute_wdi(b, c, aoi)

    # RDI (radar) is opt-in. It needs a baseline-epoch S1 composite so the
    # VV change is baseline-vs-current, the same two-epoch logic as SEI/WDI.
    # SRTM terrain is deliberately NOT added: it is single-epoch, so it has
    # no change to contribute (see compute_rdi docstring).
    rdi = None
    if use_radar:
        s1_base = s1_composite(aoi, base_win["start"], base_win["end"])
        rdi = compute_rdi(s1_base, s1_cur, aoi)
    mei = compute_mei(vli, sei, wdi, rdi)

    warnings = check_null_contamination(
        [vli, sei, wdi] + ([rdi] if rdi is not None else []))

    display = [vli, sei, wdi] + ([rdi] if rdi is not None else []) + [mei]
    print("\n" + "=" * 66)
    print(f"{'INDICATOR':<8}{'STATUS':<20}{'VALUE':<12}{'AREA (ha)':<12}")
    print("=" * 66)
    for ind in display:
        val = ind.get("value", "—")
        ha = (ind.get("hectares_lost") or ind.get("hectares_exposed")
              or ind.get("water_hectares_gained")
              or ind.get("hectares_disturbed") or "—")
        print(f"{ind['indicator']:<8}{ind['status']:<20}{str(val):<12}{str(ha):<12}")
    print("=" * 66)

    for ind in ([vli, sei, wdi] + ([rdi] if rdi is not None else [])):
        if ind["status"] != "OK":
            print(f"\n{ind['indicator']} — {ind['status']}: {ind.get('reason','')}")

    for w in warnings:
        print(f"\nWARNING: {w}")

    # Strip EE objects before serialising.
    clean = {}
    for ind in display:
        clean[ind["indicator"]] = {k: v for k, v in ind.items()
                                   if not k.startswith("_")}

    provenance = {
        "tool": "EcoMine Observatory — Stage 2",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "site": {"name": site.name, "country": site.country,
                 "lat": site.lat, "lon": site.lon,
                 "buffer_km": site.buffer_km,
                 "coordinate_provenance": site.coordinate_provenance},
        "epochs": {"baseline": base_win, "current": cur_win},
        "sufficiency_current_epoch": suff_cur,
        "method": {
            "threshold_rule": "median + K_SIGMA * robust_sigma, per site",
            "robust_sigma": "(p84 - p16) / 2 of the change image over the AOI",
            "K_SIGMA": K_SIGMA,
            "scale_m": SCALE_M,
            "vli_applicability_gate_ndvi": VLI_MIN_BASELINE_NDVI,
            "water_mndwi_min": WATER_MNDWI_MIN,
            "mei_weights": MEI_WEIGHTS,
        },
        "indicators": clean,
        "warnings": warnings,
        "limitations": [
            "Change inside an AOI containing a mine is NOT attributed to mining. "
            "Drought, agriculture, construction and urban growth produce the "
            "same signals. Attribution requires the Stage 3 classifier plus "
            "ground truth.",
            "MEI weights are an arbitrary editorial choice, not an empirical "
            "finding.",
            "The turbidity proxy is uncalibrated red reflectance, not a sediment "
            "concentration.",
            "Site coordinates are approximate and unsurveyed.",
            "No legal or compliance conclusion is expressed or implied.",
        ],
    }
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(provenance, fh, indent=2, ensure_ascii=False)
    print(f"\nProvenance + indicators written to {out_json}")

    # Change map
    m = geemap.Map(center=[site.lat, site.lon], zoom=13)
    m.add_basemap("SATELLITE")
    m.addLayer(c, {"bands": ["B4", "B3", "B2"], "min": 0, "max": 0.3},
               f"S2 true colour {current_year}")
    m.addLayer(b, {"bands": ["B4", "B3", "B2"], "min": 0, "max": 0.3},
               f"S2 true colour {baseline_year}", False)

    if "_delta_image" in sei:
        m.addLayer(sei["_delta_image"], {"min": -0.2, "max": 0.2,
                   "palette": ["2166ac", "f7f7f7", "b2182b"]},
                   "dBSI (red = surface newly exposed)")
        m.addLayer(sei["_delta_image"].gt(sei["threshold_used"]).selfMask(),
                   {"palette": ["b2182b"]}, "SEI exceedance")
    if "_delta_image" in vli:
        m.addLayer(vli["_delta_image"], {"min": -0.2, "max": 0.2,
                   "palette": ["2166ac", "f7f7f7", "b2182b"]},
                   "dNDVI (red = vegetation lost)", False)
    if "_gain" in wdi:
        m.addLayer(wdi["_gain"], {"palette": ["2166ac"]},
                   "New water-like surface", False)

    insufficient = cur["sufficient_mask"].Not().selfMask()
    m.addLayer(insufficient, {"palette": ["ff00ff"]}, "INSUFFICIENT DATA", False)
    m.addLayer(ee.Image().paint(ee.FeatureCollection([ee.Feature(aoi)]), 0, 2),
               {"palette": ["ffff00"]}, "AOI")

    m.to_html(out_html, title=f"EcoMine Stage 2 — {site.name}")
    print(f"Change map written to {out_html}")
    print("\nREMINDER: these are screening indicators over an AOI. They do not "
          "attribute change to any operator or activity.\n")


def main() -> None:
    p = argparse.ArgumentParser(description="EcoMine Observatory — Stage 2")
    p.add_argument("--site", default="ad_duwayhi", choices=list(SITES))
    p.add_argument("--baseline-year", type=int, default=2018)
    p.add_argument("--current-year", type=int, default=2025)
    p.add_argument("--out-json", default="ecomine_stage2_indicators.json")
    p.add_argument("--out-html", default="ecomine_stage2_map.html")
    p.add_argument("--radar", action="store_true",
                   help="add the optional Radar Disturbance Index (RDI) as a "
                        "fourth MEI component (Sentinel-1 VV change). Off by "
                        "default; SRTM terrain is never added, see docs.")
    args = p.parse_args()
    run(args.site, args.baseline_year, args.current_year,
        args.out_json, args.out_html, use_radar=args.radar)


if __name__ == "__main__":
    main()
