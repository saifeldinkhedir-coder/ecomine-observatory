"""
EcoMine Observatory - provenance records
=========================================
Design principle 3.2 states that every indicator must be provenance-bound and
that missing data must return an explicit "insufficient data" rather than a
guess. The working scripts honour the second half of that but not the first:
they print to a terminal and write nothing a reviewer, a co-author, or a
future version of you could audit.

This module closes that gap. It is deliberately dependency-free and knows
nothing about Earth Engine, so it can be unit-tested offline and reused by
every stage.

WHAT A PROVENANCE RECORD MUST CONTAIN
-------------------------------------
Not just the number. A number without its derivation is an assertion, not a
measurement. Each record carries:

  what      - the indicator and its value, or the reason there is no value
  where     - site, coordinates, AND the honesty of those coordinates
  when      - the epochs compared and the rule used to select imagery
  from what - dataset IDs, scene counts, masking applied
  how       - every threshold, and how it was derived
  how good  - observed area vs nominal area; whether assumptions held
  caveats   - what this number does NOT establish

The last field matters most. Every figure this project produces measures change
inside a box that contains a mine; none of them attributes that change to
mining. A record that omits this invites the reader to over-read it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional


TOOL = "EcoMine Observatory"
LICENCE = "GPL-3.0"
AUTHOR = "Seifeldin M.G. Alkhedir"
ORCID = "0000-0003-0821-2991"


# ==============================================================================
# STRUCTURED PIECES
# ==============================================================================

@dataclass
class SiteRecord:
    name: str
    country: str
    lat: float
    lon: float
    buffer_km: float
    coordinate_provenance: str
    seasonality: str = "unknown"

    def __post_init__(self):
        # A coordinate whose origin is unstated will eventually be treated as
        # surveyed by someone. Force the question at construction time.
        if not self.coordinate_provenance.strip():
            raise ValueError(
                "coordinate_provenance is required. State where the "
                "coordinates came from and how reliable they are.")


@dataclass
class DatasetRecord:
    """One satellite source as it was actually used, not as intended."""
    collection_id: str
    role: str                      # e.g. "optical", "radar", "terrain"
    scenes_used: Optional[int] = None
    date_start: Optional[str] = None
    date_end: Optional[str] = None
    filters: list[str] = field(default_factory=list)
    masking: Optional[str] = None
    notes: Optional[str] = None


@dataclass
class ThresholdRecord:
    """
    A threshold and its derivation. 'How was this number chosen?' is the first
    question a reviewer asks, and an arbitrary threshold must say so.
    """
    name: str
    value: float
    derivation: str
    is_arbitrary: bool = False
    inputs: dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        if self.is_arbitrary and "arbitrar" not in self.derivation.lower():
            self.derivation += (
                " [ARBITRARY: not derived from a distribution and carrying no "
                "statistical significance; a different value would change the "
                "outcome.]")


@dataclass
class DataSufficiency:
    observed_ha: float
    nominal_ha: float
    verdict: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def coverage_fraction(self) -> float:
        return self.observed_ha / self.nominal_ha if self.nominal_ha else 0.0


# ==============================================================================
# THE RECORD
# ==============================================================================

#: Caveats that apply to every screening output this project produces.
#: These are not boilerplate to be trimmed — each corresponds to a specific
#: thing the numbers cannot establish.
UNIVERSAL_CAVEATS = [
    "This measures change inside an area of interest that contains a mine. It "
    "does not attribute that change to mining. Roads, quarries, construction, "
    "drought and natural drift produce the same signal.",
    "This is a screening output for expert review. No legal, regulatory or "
    "compliance conclusion follows from it.",
    "No accuracy figure applies unless one is stated explicitly in this record. "
    "Accuracies reported in the literature were obtained with supervised "
    "classifiers trained on hand-labelled data and do not transfer here.",
]


class Provenance:
    """
    Accumulates a full derivation record, then writes it as JSON.

    Usage:
        p = Provenance("SEI", stage="Stage 2")
        p.set_site(site)
        p.add_dataset(DatasetRecord(...))
        p.set_epochs(baseline={...}, current={...}, rule="low_cloud_annual",
                     justification="...")
        p.add_threshold(ThresholdRecord(...))
        p.set_sufficiency(DataSufficiency(...))
        p.set_value(0.0479, unit="fraction_of_observed_aoi",
                    area_ha=533.9)
        p.write("sei_provenance.json")

    Or, when there is no value:
        p.set_not_applicable("Baseline NDVI 0.055 below the 0.15 vegetation "
                             "floor; nothing to lose at this site.")
    """

    def __init__(self, indicator: str, stage: str = ""):
        self.indicator = indicator
        self.stage = stage
        self.generated_utc = datetime.now(timezone.utc).isoformat()
        self.site: Optional[SiteRecord] = None
        self.datasets: list[DatasetRecord] = []
        self.epochs: dict[str, Any] = {}
        self.thresholds: list[ThresholdRecord] = []
        self.sufficiency: Optional[DataSufficiency] = None
        self.status: Optional[str] = None
        self.value: Optional[float] = None
        self.unit: Optional[str] = None
        self.area_ha: Optional[float] = None
        self.reason: Optional[str] = None
        self.method: dict[str, Any] = {}
        self.warnings: list[str] = []
        self.extra_caveats: list[str] = []
        self.gee_project: Optional[str] = None

    # -- setters ---------------------------------------------------------

    def set_site(self, site: SiteRecord) -> "Provenance":
        self.site = site
        return self

    def set_project(self, project_id: str) -> "Provenance":
        self.gee_project = project_id
        return self

    def add_dataset(self, ds: DatasetRecord) -> "Provenance":
        self.datasets.append(ds)
        return self

    def set_epochs(self, baseline: dict, current: dict, rule: str,
                   justification: str) -> "Provenance":
        self.epochs = {
            "baseline": baseline,
            "current": current,
            "selection_rule": rule,
            "justification": justification,
        }
        return self

    def add_threshold(self, t: ThresholdRecord) -> "Provenance":
        self.thresholds.append(t)
        return self

    def set_sufficiency(self, s: DataSufficiency) -> "Provenance":
        self.sufficiency = s
        return self

    def set_method(self, **kwargs) -> "Provenance":
        self.method.update(kwargs)
        return self

    def warn(self, message: str) -> "Provenance":
        self.warnings.append(message)
        return self

    def caveat(self, message: str) -> "Provenance":
        self.extra_caveats.append(message)
        return self

    # -- outcomes (mutually exclusive) -----------------------------------

    def set_value(self, value: float, unit: str,
                  area_ha: Optional[float] = None) -> "Provenance":
        self.status = "OK"
        self.value = value
        self.unit = unit
        self.area_ha = area_ha
        return self

    def set_not_applicable(self, reason: str) -> "Provenance":
        """The indicator cannot carry meaning here. This is a finding."""
        self.status = "NOT APPLICABLE"
        self.reason = reason
        self.value = None
        return self

    def set_insufficient_data(self, reason: str) -> "Provenance":
        """We could not see. Distinct from NOT APPLICABLE — never conflate."""
        self.status = "INSUFFICIENT DATA"
        self.reason = reason
        self.value = None
        return self

    # -- output ----------------------------------------------------------

    def to_dict(self) -> dict:
        if self.status is None:
            raise ValueError(
                f"Provenance for '{self.indicator}' has no outcome. Call "
                "set_value, set_not_applicable or set_insufficient_data "
                "before writing — a record without an outcome is incomplete.")
        if self.site is None:
            raise ValueError(
                f"Provenance for '{self.indicator}' has no site.")

        rec = {
            "tool": TOOL,
            "stage": self.stage,
            "indicator": self.indicator,
            "generated_utc": self.generated_utc,
            "author": {"name": AUTHOR, "orcid": ORCID},
            "licence": LICENCE,
            "gee_project": self.gee_project,
            "site": asdict(self.site),
            "result": {
                "status": self.status,
                "value": self.value,
                "unit": self.unit,
                "area_ha": self.area_ha,
                "reason": self.reason,
            },
            "epochs": self.epochs,
            "datasets": [asdict(d) for d in self.datasets],
            "method": self.method,
            "thresholds": [asdict(t) for t in self.thresholds],
            "warnings": self.warnings,
            "caveats": UNIVERSAL_CAVEATS + self.extra_caveats,
        }
        if self.sufficiency:
            rec["data_sufficiency"] = {
                **asdict(self.sufficiency),
                "coverage_fraction": round(
                    self.sufficiency.coverage_fraction, 4),
            }
        # Surface arbitrary thresholds at the top level so they cannot be
        # missed by someone skimming the record.
        arb = [t.name for t in self.thresholds if t.is_arbitrary]
        if arb:
            rec["arbitrary_thresholds_present"] = arb
        return rec

    def write(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)
        return path

    def summary_line(self) -> str:
        """One line for the terminal, matching what was written to disk."""
        if self.status == "OK":
            v = f"{self.value:.4f} {self.unit or ''}".strip()
            a = f"  ({self.area_ha:,.1f} ha)" if self.area_ha else ""
            return f"{self.indicator}: {self.status}  {v}{a}"
        return f"{self.indicator}: {self.status} — {self.reason}"
