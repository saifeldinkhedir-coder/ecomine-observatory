# EcoMine Observatory

**Open-source, multi-sensor screening tools for the environmental footprint of mining in the Middle East and Africa.**

Built on Google Earth Engine and the [opengeos](https://github.com/opengeos) stack (`geemap`, `leafmap`). All pixel computation happens on Earth Engine servers, so the tools run on ordinary hardware with no GPU.

Author: Seifeldin Alkhedir ([ORCID 0000-0003-0821-2991](https://orcid.org/0000-0003-0821-2991)), University of Khartoum. Licence: GPL-3.0. Version: 0.1.0.

> **Status: v0.1.0 — early release, single-site validation.** The tools run
> and produce maps, change indicators, and — as of this release — a first
> supervised-classifier accuracy figure. That figure (IoU 0.97 on the mine
> class) is measured at **one site only** (Kangala, South Africa) and has not
> been tested for transfer to any other site. Several thresholds are still
> hand-chosen rather than derived from data, and are declared as such in the
> provenance output. Treat every number as a screening result, not a verdict,
> and read the caveats in `docs/RESULTS_LOG.md` before citing anything.

---

## What this is

A set of transparent, reproducible tools that:

- Pull imagery from **five sensors** for a defined area: **Sentinel-2** (10 m
  optical), **Sentinel-1** (C-band radar, cloud-piercing), **Landsat 8/9**
  (30 m optical, used for cross-validation), **SRTM** (terrain), and
  **Sentinel-5P** (atmospheric - *regional screening only*, since TROPOMI's
  5.5 x 3.5 km pixel is larger than a single mine site; the regional protocol
  was verified in earlier work but its code is **not shipped in v0.1.0**)
- Compute core spectral indices — NDVI, NDWI, MNDWI, BSI, NDMI — plus radar
  features (VV, VH, VV−VH) and terrain features (elevation, slope), 16 in all
- Produce **change indicators** between two epochs: vegetation loss (VLI),
  surface exposure (SEI), water disturbance (WDI), an optional radar
  disturbance index (RDI, `--radar`), and a composite summary (MEI)
- Attach **machine-readable provenance** to every number: which sensor, which dates, which threshold, how much of the area was actually observed
- Say **"insufficient data"** and stop, rather than filling a gap with a plausible-looking value

## What this is *not*

This section comes before the features on purpose.

- **It is not a mine detector.** The disturbance layer is an unsupervised, threshold-based *candidate screen*. Bare rock, sabkha, wadi beds, quarries, roads and construction sites all trigger it.
- **It does not attribute change to mining.** It measures change inside an area that contains a mine. Drought, agriculture, urban growth and road building produce the same signals. Attribution needs a trained classifier and ground truth.
- **It makes no legal or compliance finding.** Indicators may point toward frameworks that warrant expert review. They never state that anyone has broken a rule.
- **It is not a surveillance tool.** The framing is environmental impact — vegetation, water, exposed surface. It is not designed to inventory who is mining where, and it should not be used that way.
- **It carries no accuracy claim.** Published accuracies of 80–98% for artisanal-mining detection come from *supervised classifiers trained on hand-labelled data* at specific sites. Those numbers belong to those studies and do not transfer to this tool.

---

## Install

```bash
git clone https://github.com/saifeldinkhedir-coder/ecomine-observatory.git
cd ecomine-observatory
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
earthengine authenticate
```

Then set `EE_PROJECT` in `check_setup.py` and `ecomine_step1.py` to your registered Earth Engine Cloud project ID.

## Check your setup first

```bash
python check_setup.py
```

Verifies Python version, packages, authentication, live server access and Sentinel data availability. It stops at the first failure and tells you exactly what to do about it. Run this before the demo — it turns a confusing traceback into a specific instruction.

## Demo

```bash
python ecomine_step1.py --list                    # available sites
python ecomine_step1.py --site ad_duwayhi         # imagery + indices + screen
python ecomine_stage2.py --site ad_duwayhi \
    --baseline-year 2018 --current-year 2025      # change indicators
python ecomine_stage3.py --labels <your-gee-asset> # classifier + IoU
```

Stage 3 needs hand-labelled polygons; see `docs/LABELLING_GUIDE_AR.md`.

## Interface

```bash
streamlit run app.py
```

Six sections covering results, sensors, method, the Stage 3 requirement,
monitoring, and a provenance-record viewer. The honesty layer is rendered in the
UI rather than left in JSON files: an indicator that returns NOT APPLICABLE or
INSUFFICIENT DATA displays that verdict and its reason in place of a number, so
a gated indicator can never be misread as a zero.

## Choosing a site

```bash
python site_check.py               # all candidates
python site_check.py --site kriel  # one
```

Run this **before** committing to a site. It answers one question per candidate:
*will this site actually activate the indicators I need?* It checks the
applicability gates first — mean baseline NDVI for VLI, water-like area for WDI —
and writes a map for visual coordinate confirmation. Two lessons paid for in this
project justify it: an estimated coordinate was wrong by 176 km, and Ad Duwayhi
turned out unable to compute MEI at all, both discovered only after work had been
built on them. Ad Duwayhi is included in the candidate list as a control on the
diagnostic itself: VLI *must* fail there, and if the script says otherwise the
script is wrong.

Both write an interactive HTML map plus a JSON provenance record. For the visual walkthrough, open `notebooks/ecomine_stage1.ipynb`.

## Reports and continuous monitoring

Stage 2 writes a provenance JSON. Two tools consume it.

**A PDF screening report** — the artefact that gets forwarded and cited, so it
carries the caveats with it rather than leaving them behind in the JSON:

```bash
python report.py --json ecomine_stage2_indicators.json --out site_report.pdf
```

Every page is banded with the no-attribution statement. A NOT APPLICABLE
indicator is printed as those words plus the reason, never as zero, and a
partial MEI states that it is a different quantity from a full-component one.

**Monitoring ingest** — feeds the same JSON into the time-series store and runs
the alert engine over it. Run it after each Stage 2 run and a real series
accumulates:

```bash
python ingest.py --json ecomine_stage2_indicators.json --site kangala
python ingest.py --list-alerts
```

The engine will not alert until a site has enough observations for a baseline,
and then requires two consecutive breaches before raising anything, with a
cooldown after. A single outlier produces a WATCH, not an alert. MEI is recorded
but never alerted on: its weights are hand-chosen and its component set can
change between runs, so an alert on it would not mean a consistent thing.

Scheduling and delivery are deliberately left to you — `ingest.py` exposes
alerts as data (`--list-alerts`) rather than shipping an email sender that has
never sent an email. Credentials do not belong in this repository.

## Legal screening — what it is and what it refuses to be

```bash
python legal_screening.py --json ecomine_stage2_indicators.json
python legal_screening.py --list-frameworks
```

This layer answers one narrow question: given that an indicator moved, which
regulatory frameworks is that signal *relevant to*, so a qualified person knows
where to look? It maps a signal to reading material and to the kind of expert
who should read it.

It does not determine compliance, and it never states that anyone violated,
breached or failed anything. It cannot: the underlying indicator is a change
fraction inside an area of interest containing a mine, computed from satellite
pixels, attributed to nobody, with no permit, no agreed baseline and no site
visit behind it. Turning that into a compliance claim would be an accusation
the data cannot support.

The restriction is enforced in code rather than only promised in prose. Every
flag carries `is_verdict: false` and names a human reviewer; a forbidden-language
guard runs over the generated output; and the test suite runs that same guard
over all thirteen curated framework entries, so an edit that phrases one as an
obligation fails the build. Screening thresholds are hand-chosen and declared
arbitrary — they control how many readings a reviewer is pointed at, nothing
more. An absence of flags is explicitly not a clean bill of health.

## Tests

```bash
pytest -v
```

141 tests. The suite covers the decision logic that determines whether a number is reported at all — thresholds, sufficiency verdicts, applicability gates, composite assembly — plus the alert engine's confirmation and cooldown behaviour, the caveats that must survive into a PDF, and a guard that fails the build if any legal-screening text is phrased as a verdict. It needs no Earth Engine credentials and no network.

---

## How thresholds are derived

The methodological core, and the part most worth scrutinising.

Relative change (ΔNDVI ÷ baseline) breaks in arid terrain: where baseline NDVI is 0.08, a trivial 0.04 shift reads as a 50% loss, and the indicator ends up measuring aridity rather than mining. Fixed absolute thresholds fail the other way — 0.10 is noise in Highveld grassland and real signal in hyper-arid desert.

So thresholds are derived from **each site's own noise floor**:

1. Compute the change image across the area of interest.
2. Estimate a robust null distribution from it — median for centre, `(p84 − p16) / 2` for spread. Robust statistics, because disturbed pixels are exactly the outliers that would otherwise inflate the threshold used to detect them.
3. Threshold = `median + k × robust_sigma`, with `k = 2.0`.
4. Count only pixels exceeding that site-specific threshold.

Results are then comparable across biomes, because each is expressed in units of its own natural variability.

**Stated assumption:** disturbance must be a *minority* of the area of interest. Above roughly 30%, the null is contaminated by the signal, the threshold inflates, and the indicator **under-reports**. The code warns when this happens. The remedy is a larger buffer, never a smaller `k`.

### Applicability gates

An indicator that cannot work at a site says so instead of returning a confident number. Vegetation Loss is gated on baseline NDVI: below 0.15 there is effectively nothing to lose, and the indicator returns **NOT APPLICABLE**. At the hyper-arid Saudi sites this gate is expected to fire, and that is the correct scientific answer.

### On the composite index

Equal component weights are an **arbitrary editorial choice**, not an empirical finding. There is no established exchange rate between a hectare of vegetation loss and a hectare of water disturbance. The composite is a screening summary with no physical units, and it must not be compared across sites whose component sets differ.

---

## Verified results

Run against live Earth Engine at Ad Duwayhi gold mine, Saudi Arabia
(22.29799, 43.26475) on 1 August 2026.

| Source | Period | Resolution | SEI | Area |
|---|---|---|---|---|
| Landsat 8/9 | 2015→2024 | 30 m | 4.79% | 533.9 ha |
| Sentinel-2 | 2018→2025 | 10 m | 4.99% | 556.5 ha |

Two different satellites, two resolutions, two periods — **4% apart**. Site
sigma agreed to three decimal places (0.00382 vs 0.00381).

**Verification tests, all passed:**

| Test | Method | Result |
|---|---|---|
| Null | One epoch differenced against itself | Exactly zero in every field |
| Control site | Identical AOI 50 km west, no mine | 0.62% vs 4.99% — **8:1** |
| Control (atmospheric) | Same offset, Sentinel-5P | SO₂ fell from +11.6% to +0.5% |
| Applicability gate | Baseline NDVI 0.055 | VLI returned NOT APPLICABLE |
| Reproducibility | Repeat runs | Identical figures |

The 0.62% is not an error — it is the natural-variability floor for this
environment, and is quotable as such.

**Sensors proven:** Sentinel-2 (optical), Sentinel-1 (C-band radar), Landsat
8/9 (optical), SRTM (terrain), Sentinel-5P (atmospheric, *regional screening
only* — TROPOMI's 5.5 × 3.5 km pixel cannot resolve a single site).

**Ruled out:** Landsat 5/7 archive — a same-sensor comparison returned a ΔBSI
median of −0.00879, implying the whole area became *less* exposed while a mine
was being dug. The calibration drift (0.009) exceeds the signal being sought
(0.008). **The site-adaptive threshold corrects for site noise but not for
sensor calibration offset**, and where the two are comparable it silently
under-reports. PlanetScope — the NICFI free programme ended 23 January 2025 and
its coverage never included Saudi Arabia.

Every number in this section measures change inside an area that contains a
mine; none *attributes* that change to mining. Attribution needs the supervised
classifier below and site-specific ground truth — see the Stage 3 result.

### Second site: Kangala Coal Mine, South Africa (9 August 2026)

A visually confirmed opencast coal mine at Delmas (−26.188, 28.670), clean of
the power-station and ash-dam infrastructure that ruled out a nearby candidate
(Kriel). It is the first site where all three MEI components can activate, and
it produced the project's second major methodological result — on how the size
of the area of interest changes what the indicators measure.

| Indicator | Wide AOI (4 km, licence centre) | Tight AOI (2 km, pit only) |
|---|---|---|
| VLI | 0.0253 (125.3 ha) | **NOT APPLICABLE** |
| SEI | 0.0117 (58.2 ha) | 0.0156 (19.3 ha) |
| WDI | 0.0106 (48.2 ha) | 0.0093 (11.3 ha) |
| MEI | 0.0159 (3/3) | 0.0124 (2/3) |

Confining the AOI to the pit **collapsed VLI to NOT APPLICABLE** (baseline NDVI
inside the pit is 0.143, below the vegetation floor). The 125 ha of "vegetation
loss" in the wide box was therefore almost entirely surrounding crop-cycle
change, not mining. SEI *rose* once the pit was a larger share of a smaller box.

Two findings follow, both documented in `docs/RESULTS_LOG.md`:

- **A computable MEI is not a valid one.** The wide-box 3/3 MEI was real
  arithmetic built on an agricultural artefact; the honest value is 2/3.
- **The adaptive threshold is necessary but not sufficient.** It corrects for
  site noise, but it cannot separate mining disturbance from non-mining
  disturbance (farming) inside the same AOI. The AOI must also be confined to
  the target — the two techniques together are what give a clean signal.

There is a paradox here worth stating: VLI needs natural vegetation *inside* the
mining area to be meaningful, but that vegetation is the first thing mining
removes. A full 3/3 MEI may therefore be rare or misleading in real mines, and a
partial 2/3 MEI (SEI + WDI) should be treated as the normal case, not the
exception.

### First accuracy figure: supervised classifier at Kangala (11 August 2026)

The first result in the project with a precision, recall and IoU. A random
forest was trained on 102 hand-drawn polygons over the eastern Kangala pit —
29 mine (class 1) and 73 not-mine (class 0), the negatives deliberately
covering the surrounding farmland, roads, bare soil and ponds that a
change-only indicator confuses with mining. 70,261 labelled pixels, split
70/30 into training and a held-out test set.

| Metric (mine class, held-out test) | Value |
|---|---|
| IoU (headline) | 0.970 |
| Precision | 0.983 |
| Recall | 0.987 |
| F1 | 0.985 |

Overall accuracy was 0.9986 and is **not** quoted: with mining at ~4% of
pixels, a classifier answering "not mine" to everything already scores ~0.95,
so overall accuracy cannot tell a working model from a useless one here. IoU on
the mine class is the honest headline.

The variable-importance ranking was the most informative part: **terrain and
radar, not the optical bands, carried the signal** — elevation (12%), slope
(9%) and the three Sentinel-1 radar features (20% combined) outranked every
Sentinel-2 band. The pit is low and its benches are rough, so SRTM elevation
and SAR roughness discriminate it better than colour. This is empirical support
for using more than the optical sensors.

Three limits are stated plainly, because a 0.97 on a first run invites
over-reading:

- **One site, not the tool.** The figure is for the eastern Kangala pit only.
  Training and test polygons come from the same small area, so the model may
  have *memorised* this site rather than *learned* mining in general. It is not
  shown to transfer; a second independent site is the real test.
- **Random pixel split likely flatters.** Neighbouring pixels from one polygon
  can land in both train and test (spatial leakage), inflating IoU. A
  polygon-level split would give a more honest number and is planned.
- **Screening, not attribution of blame.** The classifier separates mine-like
  surface from its surroundings; it does not establish who caused it or whether
  any rule was broken.

Full run, confusion matrix and provenance are in `docs/RESULTS_LOG.md`.

### First accuracy figure: Stage 3 classifier, Kangala (11 August 2026)

The project's first supervised-classifier result, and the first number that
distinguishes mine pixels from their surroundings rather than just measuring
change inside an area. A Random Forest was trained on 102 hand-drawn polygons
(29 mine, 73 not-mine) over the eastern Kangala pit, on a 16-band stack of
Sentinel-2, Sentinel-1 and SRTM terrain features.

| Metric (mine class) | Value |
|---|---|
| IoU (headline) | 0.970 |
| Precision | 0.983 |
| Recall | 0.987 |
| F1 | 0.985 |

Overall accuracy was 0.9986 but is **not** the figure to quote — with mining at
~4% of pixels, a model that answers "not mine" to everything scores ~0.95, so
overall accuracy cannot tell a working model from a useless one. IoU on the mine
class is the honest headline.

The variable-importance ranking was the informative part: **terrain and radar,
not the optical bands, carried the discrimination** — elevation (12%), slope
(9%) and the three Sentinel-1 radar features (20% combined) outranked every
optical index. The pit is low and its benches are rough, so those are the
signals that separate it here.

Three caveats travel with this number, all in `docs/RESULTS_LOG.md`: it is a
**single-site** result (train and test are from the same small area, so the
model may have memorised this pit rather than learned "mine" in general); a
pixel-level train/test split can leak spatially and flatter the score; and the
figure has not been tested for transfer to any other site. The next validation
step is exactly that transfer test — running the classifier on a second,
independent site.

## Roadmap

| Stage | Scope | Status |
|---|---|---|
| 1 | Multi-sensor imagery + indices + interactive map | ✅ verified against live GEE |
| 2 | Change indicators with adaptive thresholds | ✅ SEI cross-validated; first full MEI at Kangala; AOI-size finding |
| 3 | Supervised footprint classifier + precision/recall/IoU | ✅ first figure at Kangala (IoU 0.97, single site) |
| 4 | Continuous monitoring loop | ✅ built, tested, and fed by `ingest.py` |
| 5 | Streamlit application + PDF reports | ✅ built (6 pages, `report.py`) |
| 6 | Framework-relevance screening | ✅ `legal_screening.py` — screening only, no verdicts |
| 6b | Ground-truth upload platform | not started |
| 7 | Multi-site generalisation test | next — the real test of Stage 3 |

**Nearest priorities.** (1) Re-test the Stage 3 classifier on a second,
independent site (Ad Duwayhi) with its own labels — a 0.97 IoU on one site is
not evidence the tool generalises, and this is the single most useful thing
left to do. (2) Replace the random pixel split with a polygon-level split,
which removes spatial leakage and yields a more honest accuracy figure. (3)
Run the optional radar index (`--radar`) on a site with good Sentinel-1
coverage in both epochs and record whether it adds anything beyond SEI.

A note on what was deliberately *not* done: SRTM terrain was the strongest
single feature in the Stage 3 classifier, but it is **not** folded into MEI.
The DEM is a single ~2000 epoch, so it contributes no baseline-versus-current
change; the classifier leaned on it as a static location fingerprint of one
pit, which is site memorisation rather than a mining signal. Forcing it into a
composite of change fractions would repeat the arithmetic-on-an-artefact error
that the wide-AOI VLI exposed. Sentinel-1 radar does earn a place, because
roughness genuinely changes between epochs — hence `--radar`.

Accuracy is reported as **precision, recall and IoU on the mine class** — never
overall accuracy, which is misleading where the positive class is a few percent
of pixels.

---

## Licence and attribution

GPL-3.0. See [`LICENSE`](LICENSE).

Author: Seifeldin Alkhedir, University of Khartoum ([ORCID 0000-0003-0821-2991](https://orcid.org/0000-0003-0821-2991)).

Built on `geemap` and `leafmap` by Qiusheng Wu (MIT). Sentinel data courtesy of the Copernicus Programme / ESA. Where the [EuroMineNet](https://github.com/AI4RS/EuroMineNet) benchmark is used, its code is MIT and its dataset CC-BY 4.0; both require attribution under their own terms.

## Citation

See [`CITATION.cff`](CITATION.cff). Please cite the version you used (v0.1.0), and cite it as early-stage research software: its one accuracy figure is from a single site and is not shown to generalise.

## Contributing

Issues and pull requests welcome. Two things will be asked of any contribution that touches an indicator: that it carries provenance, and that it fails loudly rather than guessing when data is missing.
