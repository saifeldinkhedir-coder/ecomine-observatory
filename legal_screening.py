"""
EcoMine Observatory — legal and regulatory SCREENING layer.

WHAT THIS MODULE IS
-------------------
It answers one narrow question: given that an indicator moved at a site, which
regulatory frameworks is that signal *relevant to*, so a qualified person knows
where to look? That is all. It maps a signal to reading material and to the
kind of expert who should read it.

WHAT THIS MODULE IS NOT, AND MUST NEVER BECOME
----------------------------------------------
It does not determine compliance. It never states that an operator violated
anything, breached anything, is non-compliant, or that a provision was
triggered. It cannot: an indicator is a change fraction inside an area of
interest that happens to contain a mine, computed from satellite pixels, not
attributed to any actor, with no permit, no baseline agreed with a regulator,
no site visit, and no legal analysis behind it. A tool that turned that into a
compliance claim would be making an accusation it cannot support, against a
named party, in a domain where being wrong causes real harm.

This is the corrected HSAE lesson, applied from the start rather than after a
mistake. The restriction is deliberate and protects the reader, the subject of
the analysis, and the author.

The guarantee is enforced in three places, not just in this docstring:
  1. Every flag carries `is_verdict = False` and a `requires_expert_review`
     note that travels with it into any output.
  2. `FORBIDDEN_VERDICT_LANGUAGE` lists the words that must never appear in
     generated text, and `assert_no_verdict_language()` checks output against
     it. The test suite runs that check over every framework entry.
  3. Framework text is stored as "relevant to / warrants review", never as
     "requires" or "must".

HOW THRESHOLDS WORK HERE
------------------------
A framework is flagged when an indicator is OK (actually measured) and its
value clears a screening threshold. Those thresholds are HAND-CHOSEN, exactly
like the WDI area floor and the MEI weights, and are declared as arbitrary in
the output. They exist to keep the flag list short enough to be useful, not
because any of them marks a legally meaningful level. Raising or lowering one
changes which reading a reviewer is pointed at; it changes nothing about the
law.

RUN
---
    python legal_screening.py --json ecomine_stage2_indicators.json
    python legal_screening.py --json ... --country "South Africa"
    python legal_screening.py --list-frameworks
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional

# ==============================================================================
# THE GUARANTEE
# ==============================================================================

# Words and phrases that turn a screening flag into a legal claim. Generated
# text is checked against this list. The list is deliberately blunt: it is
# better to reject a harmless sentence than to let one accusatory sentence out.
FORBIDDEN_VERDICT_LANGUAGE = (
    "violation",
    "violated",
    "violates",
    "non-compliant",
    "noncompliant",
    "in breach",
    "breached",
    "breaches the",
    "illegal",
    "unlawful",
    "guilty",
    "liable",
    "offence",
    "offense",
    "infringement",
    "infringed",
    "must comply",
    "failed to comply",
    "is required to",
    "triggered article",
    "triggers article",
    "constitutes a",
    "found to be",
    "prosecut",
    "penalt",
    "sanction",
    "enforcement action",
)

# The sentence that travels with every screening output.
SCREENING_DISCLAIMER = (
    "SCREENING ONLY. This lists frameworks that the observed change may be "
    "relevant to, so that a qualified reviewer knows where to look. It is not "
    "a compliance assessment and states nothing about whether any law, "
    "standard or permit condition has been met or not met. The underlying "
    "indicator measures change inside an area of interest containing a mine "
    "and is not attributed to any operator or activity. Legal interpretation "
    "is reserved for qualified experts with access to permits, baselines and "
    "the site itself."
)


class VerdictLanguageError(ValueError):
    """Raised when generated text contains language reserved for a legal finding."""


def assert_no_verdict_language(text: str, where: str = "output") -> None:
    """
    Refuse to emit text that reads as a legal finding.

    This is a real check, not decoration: it runs over every framework entry in
    the test suite, so a future edit that phrases an entry as "operator must
    comply with..." fails the build rather than reaching a reader.
    """
    low = text.lower()
    hits = [w for w in FORBIDDEN_VERDICT_LANGUAGE if w in low]
    if hits:
        raise VerdictLanguageError(
            f"{where} contains language reserved for a legal finding: "
            f"{', '.join(sorted(set(hits)))}. This layer performs screening "
            "only; rephrase as 'relevant to' / 'warrants expert review'."
        )


# ==============================================================================
# FRAMEWORK MODEL
# ==============================================================================

@dataclass(frozen=True)
class Framework:
    """
    One instrument a signal may be relevant to.

    `relevance` says why a reviewer might care. It is phrased as relevance, not
    obligation, and is checked against FORBIDDEN_VERDICT_LANGUAGE.
    """
    key: str
    name: str
    level: str                 # international | regional | national
    jurisdiction: str          # "global" or a country name
    body: str
    relevance: str
    reviewer: str              # the kind of expert who should read this
    reference: str = ""


@dataclass
class ScreeningFlag:
    """One indicator, one framework, one reason to have a person look."""
    indicator: str
    indicator_value: Optional[float]
    indicator_status: str
    framework_key: str
    framework_name: str
    level: str
    jurisdiction: str
    relevance: str
    reviewer: str
    threshold_used: Optional[float]
    threshold_basis: str = "ARBITRARY: hand-chosen screening level, not a legal limit"
    is_verdict: bool = False
    requires_expert_review: bool = True
    note: str = field(default=(
        "Relevance flag for expert review. Not a finding of compliance or "
        "non-compliance."))


# ==============================================================================
# THE CURATED MAPPING  [CORE]
# ==============================================================================

FRAMEWORKS: dict[str, Framework] = {
    # --- international --------------------------------------------------
    "ifc_ps3": Framework(
        key="ifc_ps3",
        name="IFC Performance Standard 3 — Resource Efficiency and Pollution Prevention",
        level="international",
        jurisdiction="global",
        body="International Finance Corporation",
        relevance=(
            "Addresses pollution prevention and the release of materials to "
            "water and land. A rise in water-area or turbidity signals near a "
            "mining area is the kind of observation reviewers examine under "
            "this standard."),
        reviewer="environmental compliance specialist familiar with IFC standards",
        reference="IFC Performance Standards on Environmental and Social Sustainability (2012)",
    ),
    "ifc_ps6": Framework(
        key="ifc_ps6",
        name="IFC Performance Standard 6 — Biodiversity and Living Natural Resources",
        level="international",
        jurisdiction="global",
        body="International Finance Corporation",
        relevance=(
            "Addresses habitat conversion and loss of natural vegetation. A "
            "measured drop in vegetation cover around a mining area is the "
            "kind of observation reviewers examine under this standard."),
        reviewer="ecologist or biodiversity specialist",
        reference="IFC Performance Standards on Environmental and Social Sustainability (2012)",
    ),
    "minamata": Framework(
        key="minamata",
        name="Minamata Convention on Mercury",
        level="international",
        jurisdiction="global",
        body="UN Environment Programme",
        relevance=(
            "Concerns mercury use, notably in artisanal and small-scale gold "
            "mining. Where a site is known or suspected to involve artisanal "
            "gold processing, water-related signals are relevant background "
            "for reviewers working with this instrument. The satellite data "
            "here cannot detect mercury; this flag points to a reading, not "
            "to evidence of mercury."),
        reviewer="specialist in mercury and artisanal mining policy",
        reference="Minamata Convention on Mercury (2013), Annex C on ASGM",
    ),
    "icmm": Framework(
        key="icmm",
        name="ICMM Mining Principles",
        level="international",
        jurisdiction="global",
        body="International Council on Mining and Metals",
        relevance=(
            "Industry principles covering environmental performance and land "
            "stewardship for member companies. Relevant background where the "
            "operator is an ICMM member."),
        reviewer="mining sustainability specialist",
        reference="ICMM Mining Principles (2020)",
    ),
    "sdg6": Framework(
        key="sdg6",
        name="SDG 6 — Clean Water and Sanitation",
        level="international",
        jurisdiction="global",
        body="United Nations",
        relevance=(
            "Water-quality and water-body indicators feed national reporting "
            "under this goal. Relevant context for reporting, not an "
            "obligation on any operator."),
        reviewer="national SDG reporting focal point",
        reference="UN Sustainable Development Goal 6",
    ),
    "sdg15": Framework(
        key="sdg15",
        name="SDG 15 — Life on Land",
        level="international",
        jurisdiction="global",
        body="United Nations",
        relevance=(
            "Land degradation and vegetation-cover indicators feed national "
            "reporting under this goal."),
        reviewer="national SDG reporting focal point",
        reference="UN Sustainable Development Goal 15",
    ),
    # --- regional -------------------------------------------------------
    "amv": Framework(
        key="amv",
        name="Africa Mining Vision",
        level="regional",
        jurisdiction="Africa",
        body="African Union",
        relevance=(
            "Continental policy framework covering environmentally and "
            "socially responsible mining. Relevant context for reviewers "
            "working on African mining governance."),
        reviewer="mining policy analyst",
        reference="Africa Mining Vision (AU, 2009)",
    ),
    # --- national -------------------------------------------------------
    "za_nema": Framework(
        key="za_nema",
        name="National Environmental Management Act (NEMA)",
        level="national",
        jurisdiction="South Africa",
        body="Department of Forestry, Fisheries and the Environment",
        relevance=(
            "South Africa's framework environmental statute, covering "
            "environmental authorisation and the duty of care. Vegetation and "
            "land-cover change near a mining area is the kind of observation "
            "reviewers examine in this context."),
        reviewer="South African environmental lawyer or EAP",
        reference="Act 107 of 1998",
    ),
    "za_mprda": Framework(
        key="za_mprda",
        name="Mineral and Petroleum Resources Development Act (MPRDA)",
        level="national",
        jurisdiction="South Africa",
        body="Department of Mineral Resources and Energy",
        relevance=(
            "Governs mining rights and associated environmental obligations "
            "in South Africa, including rehabilitation. Surface-disturbance "
            "extent is relevant background for reviewers with access to the "
            "mine's approved plans."),
        reviewer="South African mining law specialist",
        reference="Act 28 of 2002",
    ),
    "za_nwa": Framework(
        key="za_nwa",
        name="National Water Act",
        level="national",
        jurisdiction="South Africa",
        body="Department of Water and Sanitation",
        relevance=(
            "Governs water use and water-resource protection in South Africa, "
            "including mine-water management. Changes in water-body extent "
            "near a mining area are relevant background here."),
        reviewer="South African water law specialist",
        reference="Act 36 of 1998",
    ),
    "sa_mining_law": Framework(
        key="sa_mining_law",
        name="Mining Investment Law and environmental regulations",
        level="national",
        jurisdiction="Saudi Arabia",
        body="Ministry of Industry and Mineral Resources",
        relevance=(
            "Governs mining activity and its environmental conditions in "
            "Saudi Arabia. Surface-disturbance extent is relevant background "
            "for reviewers with access to the licence conditions."),
        reviewer="Saudi environmental regulatory specialist",
        reference="Mining Investment Law (2020) and implementing regulations",
    ),
    "sa_env_law": Framework(
        key="sa_env_law",
        name="Environmental Law and National Centre for Environmental Compliance rules",
        level="national",
        jurisdiction="Saudi Arabia",
        body="National Centre for Environmental Compliance",
        relevance=(
            "Saudi Arabia's general environmental statute and monitoring "
            "framework. Relevant background for land and water observations."),
        reviewer="Saudi environmental regulatory specialist",
        reference="Environmental Law (2020)",
    ),
    "sd_mining": Framework(
        key="sd_mining",
        name="Mining Act and environmental regulations",
        level="national",
        jurisdiction="Sudan",
        body="Ministry of Minerals",
        relevance=(
            "Governs mining and its environmental conditions in Sudan. "
            "Included so that Sudanese sites can be screened with the same "
            "neutrality as any other; the tool takes no position on any actor."),
        reviewer="Sudanese environmental or mining law specialist",
        reference="Mining Act and implementing environmental regulations",
    ),
}


# Indicator -> candidate frameworks, with the hand-chosen screening level at
# which the flag appears. Every threshold here is ARBITRARY and declared so.
INDICATOR_FRAMEWORKS: dict[str, dict] = {
    "VLI": {
        "threshold": 0.02,     # 2% of observed AOI
        "frameworks": ["ifc_ps6", "sdg15", "za_nema", "za_mprda",
                       "sa_mining_law", "sa_env_law", "sd_mining", "amv"],
        "why": "vegetation cover fell measurably across the area of interest",
    },
    "SEI": {
        "threshold": 0.01,
        "frameworks": ["ifc_ps6", "sdg15", "za_mprda", "za_nema",
                       "sa_mining_law", "sd_mining", "amv", "icmm"],
        "why": "bare surface expanded measurably across the area of interest",
    },
    "WDI": {
        "threshold": 0.005,
        "frameworks": ["ifc_ps3", "sdg6", "za_nwa", "za_nema",
                       "sa_env_law", "sd_mining", "minamata"],
        "why": "water-body extent or turbidity changed measurably",
    },
    "RDI": {
        "threshold": 0.01,
        "frameworks": ["ifc_ps6", "za_mprda", "sa_mining_law", "sd_mining"],
        "why": "radar backscatter indicates measurable ground disturbance",
    },
    # MEI is deliberately absent. It is a weighted composite with hand-chosen
    # weights whose component set can change between runs, so pointing a
    # reviewer at a framework on the strength of it would be pointing at a
    # number whose definition is not stable.
}


# ==============================================================================
# SCREENING
# ==============================================================================

def frameworks_for(indicator: str, country: Optional[str] = None) -> list[Framework]:
    """Candidate frameworks for an indicator, optionally filtered by country."""
    spec = INDICATOR_FRAMEWORKS.get(indicator)
    if not spec:
        return []
    out = []
    for key in spec["frameworks"]:
        fw = FRAMEWORKS[key]
        if fw.level == "national" and country and fw.jurisdiction != country:
            continue
        out.append(fw)
    return out


def screen_indicator(indicator: str, ind: dict,
                     country: Optional[str] = None) -> list[ScreeningFlag]:
    """
    Produce relevance flags for one indicator.

    An indicator that was not measured produces nothing. Pointing a reviewer at
    a framework because a value could not be computed would be noise, and would
    quietly imply that absence of measurement is itself a signal.
    """
    if ind.get("status") != "OK":
        return []
    spec = INDICATOR_FRAMEWORKS.get(indicator)
    if not spec:
        return []
    value = ind.get("value")
    if value is None or value < spec["threshold"]:
        return []

    flags = []
    for fw in frameworks_for(indicator, country):
        flags.append(ScreeningFlag(
            indicator=indicator,
            indicator_value=value,
            indicator_status=ind["status"],
            framework_key=fw.key,
            framework_name=fw.name,
            level=fw.level,
            jurisdiction=fw.jurisdiction,
            relevance=fw.relevance,
            reviewer=fw.reviewer,
            threshold_used=spec["threshold"],
        ))
    return flags


def screen(doc: dict, country: Optional[str] = None) -> dict:
    """Screen a whole Stage 2 provenance document."""
    if "indicators" not in doc:
        raise ValueError(
            "No 'indicators' block; this does not look like Stage 2 output.")

    if country is None:
        country = doc.get("site", {}).get("country")

    flags, considered, not_measured = [], [], []
    for name, ind in doc["indicators"].items():
        if name not in INDICATOR_FRAMEWORKS:
            continue
        considered.append(name)
        if ind.get("status") != "OK":
            not_measured.append({"indicator": name,
                                 "status": ind.get("status"),
                                 "reason": ind.get("reason")})
            continue
        flags.extend(screen_indicator(name, ind, country))

    result = {
        "tool": "EcoMine Observatory — legal screening",
        "site": doc.get("site", {}).get("name"),
        "country": country,
        "disclaimer": SCREENING_DISCLAIMER,
        "is_compliance_assessment": False,
        "indicators_considered": considered,
        "indicators_not_measured": not_measured,
        "flags": [asdict(f) for f in flags],
        "flag_count": len(flags),
        "threshold_note": (
            "Every screening threshold in this layer is hand-chosen, exactly "
            "like the WDI area floor and the MEI weights. It controls how many "
            "readings a reviewer is pointed at. It carries no legal meaning and "
            "marks no legally significant level."),
    }
    # The guarantee, enforced on the way out.
    assert_no_verdict_language(json.dumps(result, ensure_ascii=False),
                               "screening result")
    return result


# ==============================================================================
# OUTPUT
# ==============================================================================

def print_screening(res: dict) -> None:
    print("\nEcoMine Observatory — legal and regulatory SCREENING")
    print(f"Site    : {res.get('site')}")
    print(f"Country : {res.get('country')}")
    print("=" * 70)
    print(SCREENING_DISCLAIMER)
    print("=" * 70)

    if not res["flags"]:
        print("\nNo framework flagged at the current screening thresholds.")
        print("This is NOT a statement that nothing is happening, and NOT a "
              "clean bill of health. It means no measured indicator cleared a "
              "hand-chosen screening level.")
    else:
        by_ind: dict[str, list] = {}
        for f in res["flags"]:
            by_ind.setdefault(f["indicator"], []).append(f)
        for ind, fl in by_ind.items():
            v = fl[0]["indicator_value"]
            thr = fl[0]["threshold_used"]
            print(f"\n{ind} = {v}  (screening level {thr}, arbitrary)")
            print(f"  {INDICATOR_FRAMEWORKS[ind]['why']}")
            print("  Frameworks a reviewer may wish to consult:")
            for f in fl:
                print(f"    - [{f['level']}] {f['framework_name']}")
                print(f"        reviewer: {f['reviewer']}")

    if res["indicators_not_measured"]:
        print("\nNot screened (not measured):")
        for nm in res["indicators_not_measured"]:
            print(f"  {nm['indicator']}: {nm['status']} — {nm.get('reason') or ''}")

    print("\n" + "-" * 70)
    print(res["threshold_note"])
    print("Every entry above points to reading material and to the kind of "
          "expert who should read it. None of it states that anything has been "
          "met or not met.")


# ==============================================================================
# CLI
# ==============================================================================

def main():
    p = argparse.ArgumentParser(
        description="Map Stage 2 indicators to relevant frameworks (screening only)")
    p.add_argument("--json", default="ecomine_stage2_indicators.json")
    p.add_argument("--country", default=None,
                   help="filter national frameworks; defaults to the site's country")
    p.add_argument("--out-json", default=None,
                   help="write the screening result as JSON")
    p.add_argument("--list-frameworks", action="store_true")
    a = p.parse_args()

    if a.list_frameworks:
        for fw in FRAMEWORKS.values():
            print(f"[{fw.level:<13}] {fw.jurisdiction:<14} {fw.name}")
        return

    if not os.path.exists(a.json):
        print(f"ABORT: {a.json} not found. Run ecomine_stage2.py first.")
        sys.exit(1)
    with open(a.json, encoding="utf-8") as fh:
        doc = json.load(fh)

    try:
        res = screen(doc, a.country)
    except ValueError as e:
        print(f"ABORT: {e}")
        sys.exit(1)

    print_screening(res)
    if a.out_json:
        with open(a.out_json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=2, ensure_ascii=False)
        print(f"\nScreening written to {a.out_json}")


if __name__ == "__main__":
    main()
