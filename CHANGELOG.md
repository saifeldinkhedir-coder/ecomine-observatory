# Changelog

All notable changes to EcoMine Observatory are recorded here. This project
follows [semantic versioning](https://semver.org). While the major version is
0, the public interface (site registry, indicator definitions, output schema)
may still change between minor releases.

## [0.1.0] — 2026-08-11

First public release. The tools run end to end on Google Earth Engine and
produce change indicators, a composite index, and — new in this release — a
first supervised-classifier accuracy figure.

### Added
- **Stage 1** — site registry and provenance (`ecomine_step1.py`, `provenance.py`).
- **Stage 2** — change indicators SEI, VLI, WDI and the composite MEI, with a
  per-site adaptive threshold (median + 2·robust_sigma). `ecomine_stage2.py`.
- **Stage 3** — supervised Random Forest footprint classifier that reports
  precision, recall and IoU on the mine class from hand-drawn label polygons.
  `ecomine_stage3.py`.
- **Stage 4** — SQLite change store and alert engine (`monitoring.py`).
- **Stage 5** — Streamlit interface, six pages (`app.py`).
- **Legal screening** — `legal_screening.py` maps each measured indicator to
  the regulatory frameworks it may be *relevant to*, so a qualified reviewer
  knows where to look. It issues no verdicts: every flag declares itself not a
  finding, names the kind of expert who should read it, and a verdict-language
  guard runs over the output and over all 13 curated framework entries in the
  test suite. MEI is never screened; an unmeasured indicator produces no flag.
- **PDF reports** — `report.py` renders a Stage 2 provenance JSON as a
  one-page A4 screening report, carrying the no-attribution banner, the
  NOT APPLICABLE explanation, the partial-composite warning and the
  limitations list into the artefact that actually gets forwarded and cited.
  Pure-Python (reportlab), so `pip install -r requirements.txt` suffices.
- **Stage 5b** — `ingest.py`, the bridge that feeds Stage 2 indicator output
  into the monitoring store and runs the alert engine over it. Closes the gap
  where the alert engine was tested but had no real data flowing into it.
- Optional **RDI** (Radar Disturbance Index) as a fourth MEI component, behind
  `--radar`. SRTM terrain is deliberately excluded: it is single-epoch, so it
  contributes no baseline-vs-current change.
- Site diagnostic tool with a deliberately disabled contamination detector
  (`site_check.py`), pending labelled ground truth.
- Two visually confirmed sites: Ad Duwayhi (Saudi Arabia) and Kangala
  (South Africa), plus pit-tight and eastern-pit variants of Kangala.
- Labelling guides for QGIS and Google Earth Pro (Arabic).
- 141 passing tests across core, provenance, monitoring, ingest, report
  and legal-screening modules.

### Verified results (see docs/RESULTS_LOG.md)
- Ad Duwayhi SEI cross-validated between Sentinel-2 (4.99%) and Landsat 8/9
  (4.79%), agreeing within 4%.
- Kangala AOI-size finding: confining the AOI to the pit collapses VLI to
  NOT APPLICABLE, showing the wide-AOI VLI was dominated by surrounding
  farmland, not mining. A computable MEI is not automatically a valid one.
- First Stage 3 accuracy figure at Kangala east: IoU 0.970 on the mine class,
  single-site, terrain- and radar-driven.

### Known limitations
- Several thresholds (WDI area/percent floors, MEI equal weights, ratio floor)
  are hand-chosen, not derived. Each is flagged as arbitrary in provenance.
- The Stage 3 figure is single-site and uses a pixel-level train/test split;
  it has not been tested for transfer to another site.
- MEI integrates only the optical SEI; SRTM terrain and Sentinel-1 radar are
  proven to work but not yet composite-index components.
- Site coordinates are visually confirmed, not surveyed.

### Not included
- No legal or compliance conclusions. Every number is a screening indicator
  over an area of interest and is not attributed to any operator or activity.
