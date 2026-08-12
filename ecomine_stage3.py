"""
EcoMine Observatory - STAGE 3: supervised footprint classifier
===============================================================
Random Forest on hand-labelled polygons, with honest evaluation.

WHAT THIS SOLVES
----------------
Every figure produced so far measures "change inside a box that contains a
mine". It does not attribute that change to mining. Bare rock, wadi beds,
sabkha, roads, quarries and construction all produce the same spectral and
radar signature. This stage is the only thing that closes that gap.

WHY THE METRICS ARE WHAT THEY ARE
---------------------------------
Overall accuracy is meaningless here. The mine occupies roughly 5% of the AOI,
so a classifier that answers "not mine" to every pixel scores 95% and is
useless. This module reports PRECISION, RECALL and IoU on the mine class, plus
the full confusion matrix, exactly as EuroMineNet does.

  precision = of pixels called mine, how many really are   -> false alarms
  recall    = of real mine pixels, how many were found     -> misses
  IoU       = intersection over union on the mine class    -> the honest single number
  F1        = harmonic mean of precision and recall

PREREQUISITE - YOUR WORK, NOT THE CODE'S
----------------------------------------
A GEE FeatureCollection asset of hand-drawn polygons with an integer property
named `class`:

    class = 1  MINE      : pit floor, benches, waste rock, leach pads, plant
    class = 0  NOT MINE  : and this is the part that decides everything

HARD NEGATIVES ARE THE WHOLE GAME. Empty desert teaches the classifier almost
nothing, because empty desert is trivially separable. What stops it labelling
every bright bare surface a mine is:
    - natural bare rock and outcrops
    - wadi beds and drainage channels
    - sabkha / salt flats
    - dirt roads and tracks
    - any nearby quarry or construction site

Aim for roughly 3x as many class=0 polygons as class=1, because the negative
class covers far more varied terrain.

RUN
---
    python stage3.py --labels users/YOU/ecomine_labels_ad_duwayhi
    python stage3.py --labels ... --trees 200 --test-split 0.3 --export-geojson
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import ee


# ==============================================================================
# CONFIGURATION
# ==============================================================================

# The AOI is chosen at run time with --site, from the shared SITES registry,
# so the classifier is always evaluated over the same box its labels cover.
# A hardcoded site here was the cause of a real bug: labels drawn over Kangala
# in South Africa would have been evaluated against an AOI in Saudi Arabia,
# and the two never intersect.
from ecomine_step1 import SITES

DEFAULT_SITE = "kangala_east"

YEAR = 2025
CLASS_PROPERTY = "class"
SCALE = 10          # Sentinel-2 native resolution
DEFAULT_TREES = 150
DEFAULT_TEST_SPLIT = 0.3
RANDOM_SEED = 42    # fixed so results are reproducible

#: Minimum polygons per class before training is allowed to proceed.
#: Below this the confusion matrix is too sparse to mean anything.
MIN_POLYGONS_PER_CLASS = 20


def init_ee() -> str:
    proj = open(".env").read().split("=", 1)[1].strip()
    ee.Initialize(project=proj)
    return proj


# ==============================================================================
# FEATURE STACK
# ==============================================================================

def mask_s2(img):
    scl = img.select("SCL")
    bad = (scl.eq(3).Or(scl.eq(8)).Or(scl.eq(9))
           .Or(scl.eq(10)).Or(scl.eq(11)))
    return img.updateMask(bad.Not()).divide(10000)


def build_features(aoi):
    """
    Multi-sensor feature stack. Every band here is a variable the Random Forest
    can split on. Radar is included deliberately: it measures geometric
    roughness rather than spectral reflectance, so it can separate cases the
    optical bands confuse.
    """
    s2 = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
          .filterBounds(aoi)
          .filterDate(f"{YEAR}-01-01", f"{YEAR}-12-31")
          .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 40))
          .map(mask_s2)
          .median()
          .clip(aoi))

    ndvi = s2.normalizedDifference(["B8", "B4"]).rename("NDVI")
    ndwi = s2.normalizedDifference(["B3", "B8"]).rename("NDWI")
    mndwi = s2.normalizedDifference(["B3", "B11"]).rename("MNDWI")
    ndmi = s2.normalizedDifference(["B8", "B11"]).rename("NDMI")
    bsi = s2.expression(
        "((S+R)-(N+B))/((S+R)+(N+B))",
        {"S": s2.select("B11"), "R": s2.select("B4"),
         "N": s2.select("B8"), "B": s2.select("B2")}).rename("BSI")

    # Radar: ASCENDING only. Mixing orbit passes mixes viewing geometry and
    # produces texture artefacts that look like real signal.
    s1 = (ee.ImageCollection("COPERNICUS/S1_GRD")
          .filterBounds(aoi)
          .filterDate(f"{YEAR}-01-01", f"{YEAR}-12-31")
          .filter(ee.Filter.eq("instrumentMode", "IW"))
          .filter(ee.Filter.eq("orbitProperties_pass", "ASCENDING")))
    vv = s1.select("VV").median().focal_median(
        radius=50, units="meters").rename("VV").clip(aoi)
    vh = s1.select("VH").median().focal_median(
        radius=50, units="meters").rename("VH").clip(aoi)
    vvvh = vv.subtract(vh).rename("VV_VH")   # dB domain: difference = ratio

    # Terrain. Expected to be weak at this site (near-flat, 2.7 deg median
    # slope) and SRTM predates the mine by 16 years, so it cannot see the pit.
    # Included anyway: Random Forest ignores uninformative features, and the
    # variable-importance output will show empirically whether it helped.
    dem = ee.Image("USGS/SRTMGL1_003").clip(aoi)
    slope = ee.Terrain.slope(dem).rename("slope")
    elev = dem.rename("elev")

    return ee.Image.cat([
        s2.select(["B2", "B3", "B4", "B8", "B11", "B12"]),
        ndvi, ndwi, mndwi, ndmi, bsi,
        vv, vh, vvvh,
        elev, slope,
    ])


# ==============================================================================
# LABEL VALIDATION
# ==============================================================================

def validate_labels(labels, class_prop):
    """
    Refuse to train on labels that cannot support a meaningful evaluation.
    A confusion matrix from 5 polygons is not a result, it is decoration.
    """
    n_total = labels.size().getInfo()
    n_mine = labels.filter(ee.Filter.eq(class_prop, 1)).size().getInfo()
    n_not = labels.filter(ee.Filter.eq(class_prop, 0)).size().getInfo()

    print(f"\nLabel asset: {n_total} features")
    print(f"  class=1 (mine)     : {n_mine}")
    print(f"  class=0 (not mine) : {n_not}")

    problems = []
    if n_mine + n_not != n_total:
        problems.append(
            f"{n_total - n_mine - n_not} features have a '{class_prop}' value "
            "that is neither 0 nor 1. Check the field is INTEGER, not string.")
    if n_mine < MIN_POLYGONS_PER_CLASS:
        problems.append(
            f"Only {n_mine} mine polygons; {MIN_POLYGONS_PER_CLASS} is the "
            "minimum for a defensible confusion matrix.")
    if n_not < MIN_POLYGONS_PER_CLASS:
        problems.append(
            f"Only {n_not} not-mine polygons; {MIN_POLYGONS_PER_CLASS} minimum.")
    if n_not and n_mine and n_not / n_mine < 1.5:
        problems.append(
            f"Ratio not-mine:mine is {n_not/n_mine:.1f}:1. Aim for ~3:1 — the "
            "negative class must cover far more varied terrain (bare rock, "
            "wadi, sabkha, roads, quarries) or the classifier will call every "
            "bright bare surface a mine.")

    if problems:
        print("\nLABEL PROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        print("\nFix the label asset and re-run. Training on these labels "
              "would produce a number that looks like an accuracy but isn't.")
        sys.exit(1)

    print("  label check: OK")
    return {"total": n_total, "mine": n_mine, "not_mine": n_not}


# ==============================================================================
# METRICS
# ==============================================================================

def mine_class_metrics(cm_list):
    """
    Precision, recall, F1 and IoU for the MINE class from a 2x2 confusion
    matrix ordered [[TN, FP], [FN, TP]] (GEE orders by class value 0 then 1).

    These are computed here rather than taken from ee.ConfusionMatrix.accuracy()
    because overall accuracy on a 5%-positive problem is actively misleading.
    """
    tn, fp = cm_list[0][0], cm_list[0][1]
    fn, tp = cm_list[1][0], cm_list[1][1]

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) else 0.0)
    iou = tp / (tp + fp + fn) if (tp + fp + fn) else 0.0
    overall = (tp + tn) / (tp + tn + fp + fn) if (tp + tn + fp + fn) else 0.0

    return {
        "true_negative": tn, "false_positive": fp,
        "false_negative": fn, "true_positive": tp,
        "precision_mine": round(precision, 4),
        "recall_mine": round(recall, 4),
        "f1_mine": round(f1, 4),
        "iou_mine": round(iou, 4),
        "overall_accuracy": round(overall, 4),
    }


def print_report(m, n_train, n_test):
    print("\n" + "=" * 58)
    print("CONFUSION MATRIX (test set, held out)")
    print("=" * 58)
    print(f"{'':>14}{'pred: not mine':>16}{'pred: mine':>14}")
    print(f"{'true: not mine':>14}{m['true_negative']:>16}{m['false_positive']:>14}")
    print(f"{'true: mine':>14}{m['false_negative']:>16}{m['true_positive']:>14}")

    print("\n" + "=" * 58)
    print("MINE-CLASS METRICS  (the numbers that matter)")
    print("=" * 58)
    print(f"  Precision : {m['precision_mine']:.4f}   "
          f"of pixels called mine, {m['precision_mine']*100:.1f}% really are")
    print(f"  Recall    : {m['recall_mine']:.4f}   "
          f"of real mine pixels, {m['recall_mine']*100:.1f}% were found")
    print(f"  F1        : {m['f1_mine']:.4f}")
    print(f"  IoU       : {m['iou_mine']:.4f}   <-- quote THIS one")
    print(f"\n  train samples: {n_train}   test samples: {n_test}")

    print("\n" + "-" * 58)
    print(f"  Overall accuracy: {m['overall_accuracy']:.4f}  "
          "<-- DO NOT QUOTE THIS")
    print("  With mining at ~5% of pixels, a classifier answering 'not mine'")
    print("  to everything scores ~0.95. Overall accuracy cannot distinguish")
    print("  a working model from a useless one on this problem.")
    print("-" * 58)


def interpret(m):
    """Plain-language reading of the two failure modes."""
    print("\nREADING THE RESULT")
    p, r = m["precision_mine"], m["recall_mine"]
    if p < 0.6:
        print(f"  Low precision ({p:.2f}): many false alarms. The classifier is")
        print("  labelling non-mine surfaces as mine. Most likely cause is too")
        print("  few HARD NEGATIVES — add polygons over natural bare rock,")
        print("  wadi beds, sabkha, roads and quarries, then retrain.")
    if r < 0.6:
        print(f"  Low recall ({r:.2f}): the classifier is missing real mine")
        print("  area. Likely causes: too few mine polygons, or they cover only")
        print("  one facies (e.g. all pit floor, no waste rock or leach pads).")
        print("  Add polygons spanning every visually distinct mine surface.")
    if p >= 0.6 and r >= 0.6:
        print(f"  Balanced result (P={p:.2f}, R={r:.2f}). Report IoU as the")
        print("  headline figure alongside both components.")
    print("\n  Reminder: these metrics describe performance ON YOUR LABELS at")
    print("  THIS SITE. They do not transfer to another site without new")
    print("  labels and re-evaluation there.")


# ==============================================================================
# MAIN
# ==============================================================================

def run(labels_asset, site_key, trees, test_split, export_geojson, out_json):
    proj = init_ee()
    site = SITES[site_key]
    aoi = ee.Geometry.Point([site.lon, site.lat]).buffer(
        site.buffer_km * 1000)

    print("=" * 58)
    print("EcoMine Observatory - Stage 3: supervised classifier")
    print("=" * 58)
    print(f"Site    : {site.name}, {site.country}")
    print(f"Project : {proj}")
    print(f"Labels  : {labels_asset}")

    labels = ee.FeatureCollection(labels_asset)
    counts = validate_labels(labels, CLASS_PROPERTY)

    print("\nBuilding feature stack...")
    features = build_features(aoi)
    band_names = features.bandNames().getInfo()
    print(f"  {len(band_names)} features: {band_names}")

    print("\nSampling training pixels from polygons...")
    samples = features.sampleRegions(
        collection=labels,
        properties=[CLASS_PROPERTY],
        scale=SCALE,
        tileScale=4,
        geometries=False,
    )
    n_samples = samples.size().getInfo()
    print(f"  {n_samples} labelled pixels extracted")
    if n_samples < 100:
        print("\n  Too few samples for a meaningful split. Draw larger or more "
              "polygons.")
        sys.exit(1)

    # Deterministic train/test split on a seeded random column.
    samples = samples.randomColumn("rnd", RANDOM_SEED)
    train = samples.filter(ee.Filter.gte("rnd", test_split))
    test = samples.filter(ee.Filter.lt("rnd", test_split))
    n_train, n_test = train.size().getInfo(), test.size().getInfo()
    print(f"  train {n_train} / test {n_test}  (split {1-test_split:.0%}/{test_split:.0%})")

    print(f"\nTraining Random Forest ({trees} trees)...")
    clf = ee.Classifier.smileRandomForest(
        numberOfTrees=trees, seed=RANDOM_SEED).train(
        features=train,
        classProperty=CLASS_PROPERTY,
        inputProperties=band_names,
    )

    # Evaluate on the HELD-OUT set. Training-set accuracy is not a result.
    validated = test.classify(clf)
    cm = validated.errorMatrix(CLASS_PROPERTY, "classification")
    cm_list = cm.array().getInfo()
    metrics = mine_class_metrics(cm_list)

    print_report(metrics, n_train, n_test)
    interpret(metrics)

    # Variable importance: shows empirically which sensors earned their place.
    try:
        imp = clf.explain().get("importance").getInfo()
        ranked = sorted(imp.items(), key=lambda kv: kv[1], reverse=True)
        total = sum(imp.values()) or 1
        print("\nVARIABLE IMPORTANCE (which sensors actually contributed)")
        for name, val in ranked:
            bar = "#" * int(40 * val / ranked[0][1])
            print(f"  {name:>8} {val/total*100:5.1f}%  {bar}")
    except Exception as e:
        imp = None
        print(f"\n  variable importance unavailable: {str(e)[:120]}")

    # Classify the full AOI and measure the predicted footprint.
    print("\nClassifying full AOI...")
    footprint = features.classify(clf).rename("mine").clip(aoi)
    area = footprint.eq(1).multiply(ee.Image.pixelArea()).reduceRegion(
        ee.Reducer.sum(), aoi, SCALE, maxPixels=int(1e10),
        bestEffort=True).getInfo().get("mine", 0)
    ha = area / 1e4
    aoi_ha = aoi.area(maxError=1).getInfo() / 1e4
    print(f"  predicted mine footprint: {ha:,.1f} ha "
          f"({ha/aoi_ha*100:.2f}% of {aoi_ha:,.0f} ha AOI)")

    record = {
        "tool": "EcoMine Observatory - Stage 3",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "site": {"key": site_key, "name": site.name, "country": site.country,
                 "lat": site.lat, "lon": site.lon, "buffer_km": site.buffer_km,
                 "coordinate_provenance": site.coordinate_provenance},
        "gee_project": proj,
        "labels_asset": labels_asset,
        "label_counts": counts,
        "features": band_names,
        "classifier": {
            "type": "smileRandomForest",
            "trees": trees,
            "seed": RANDOM_SEED,
            "scale_m": SCALE,
            "test_split": test_split,
            "n_train_px": n_train,
            "n_test_px": n_test,
        },
        "confusion_matrix_TN_FP_FN_TP": cm_list,
        "metrics": metrics,
        "variable_importance": imp,
        "predicted_footprint_ha": round(ha, 2),
        "aoi_ha": round(aoi_ha, 1),
        "limitations": [
            "Metrics describe performance on THESE labels at THIS site. They "
            "do not transfer to other sites without new labels.",
            "Overall accuracy is reported but must not be quoted: with mining "
            "at ~5% of pixels it cannot distinguish a working model from a "
            "trivial one.",
            "Labels are the author's own interpretation of high-resolution "
            "imagery, not ground truth. Field verification is a separate step.",
            "Site coordinates are from a secondary source and are not surveyed.",
            "A predicted footprint is a screening output for expert review. No "
            "legal or compliance conclusion follows from it.",
        ],
    }
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, ensure_ascii=False)
    print(f"\nProvenance + metrics written to {out_json}")

    if export_geojson:
        vec = footprint.selfMask().reduceToVectors(
            geometry=aoi, scale=SCALE, maxPixels=int(1e10),
            geometryType="polygon", labelProperty="mine")
        task = ee.batch.Export.table.toDrive(
            collection=vec,
            description="ecomine_footprint_ad_duwayhi",
            fileFormat="GeoJSON")
        task.start()
        print("Export task started -> Google Drive "
              "(ecomine_footprint_ad_duwayhi.geojson)")
        print("Track it at https://code.earthengine.google.com/tasks")


def main():
    p = argparse.ArgumentParser(
        description="EcoMine Stage 3 - supervised footprint classifier")
    p.add_argument("--labels", required=True,
                   help="GEE FeatureCollection asset ID of labelled polygons")
    p.add_argument("--site", default=DEFAULT_SITE, choices=list(SITES),
                   help="AOI site; MUST match where the labels were drawn")
    p.add_argument("--trees", type=int, default=DEFAULT_TREES)
    p.add_argument("--test-split", type=float, default=DEFAULT_TEST_SPLIT)
    p.add_argument("--export-geojson", action="store_true",
                   help="export the predicted footprint to Drive as GeoJSON")
    p.add_argument("--out-json", default="stage3_metrics.json")
    a = p.parse_args()
    run(a.labels, a.site, a.trees, a.test_split, a.export_geojson, a.out_json)


if __name__ == "__main__":
    main()
