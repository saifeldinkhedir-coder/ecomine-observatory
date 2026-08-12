"""
EcoMine Observatory — PDF report generator.

WHAT THIS IS FOR
----------------
Stage 2 writes a provenance JSON. That file is complete and honest but nobody
reads raw JSON in a meeting. This module renders the same content as a PDF that
a regulator, supervisor or journalist can actually read.

THE DESIGN RULE THAT MATTERS
----------------------------
The report renders ONLY what is in the JSON. It computes nothing, infers
nothing, and fills no gaps. If an indicator is NOT APPLICABLE, the report says
NOT APPLICABLE; it never prints a zero, a dash that reads like a zero, or a
sentence that implies the value was small. A report that quietly turns "not
measurable" into "no impact" would be the most damaging failure this tool could
have, because a PDF is the artefact that gets forwarded and cited.

Every number is carried WITH its caveat. The limitations block travels on the
same page flow as the numbers, not in an appendix nobody reaches, so a figure
cannot be lifted out of the report without its qualification. The screening
disclaimer is repeated in the footer of every page for the same reason.

RUN
---
    python report.py --json ecomine_stage2_indicators.json
    python report.py --json ecomine_stage2_indicators.json --out site_report.pdf
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

# ==============================================================================
# CONSTANTS
# ==============================================================================

FOOTER_NOTE = (
    "Screening indicator over an area of interest containing a mine. "
    "Not attributed to any operator or activity. Not a compliance finding."
)

# Human-readable names. Kept here rather than invented per report so two reports
# never label the same indicator differently.
INDICATOR_NAMES = {
    "VLI": "Vegetation Loss Index",
    "SEI": "Surface Exposure Index",
    "WDI": "Water Disturbance Index",
    "RDI": "Radar Disturbance Index",
    "MEI": "Mining Environmental Index (composite)",
}

# Where each indicator keeps its area figure. A single lookup would silently
# print a blank for most of them.
AREA_KEYS = (
    ("hectares_lost", "vegetation lost"),
    ("hectares_exposed", "surface exposed"),
    ("water_hectares_gained", "water area gained"),
    ("hectares_disturbed", "ground disturbed"),
)


# ==============================================================================
# STYLES
# ==============================================================================

def _styles() -> dict:
    ss = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "t", parent=ss["Title"], fontSize=17, leading=21, spaceAfter=2 * mm),
        "sub": ParagraphStyle(
            "s", parent=ss["Normal"], fontSize=9.5, leading=13,
            textColor=colors.HexColor("#555555"), spaceAfter=5 * mm),
        "h2": ParagraphStyle(
            "h2", parent=ss["Heading2"], fontSize=12, leading=15,
            spaceBefore=5 * mm, spaceAfter=2 * mm,
            textColor=colors.HexColor("#1a1a1a")),
        "body": ParagraphStyle(
            "b", parent=ss["Normal"], fontSize=9.5, leading=13.5,
            alignment=TA_LEFT, spaceAfter=2 * mm),
        "small": ParagraphStyle(
            "sm", parent=ss["Normal"], fontSize=8.3, leading=11.5,
            textColor=colors.HexColor("#444444"), spaceAfter=1.5 * mm),
        "caveat": ParagraphStyle(
            "cv", parent=ss["Normal"], fontSize=8.6, leading=12,
            textColor=colors.HexColor("#7a3b00"), spaceAfter=2 * mm),
    }


# ==============================================================================
# HELPERS — every one of these refuses to invent a value
# ==============================================================================

def fmt_value(ind: dict) -> str:
    """
    Render an indicator's value. A non-OK indicator returns its status, never a
    number and never a bare dash that could be read as zero.
    """
    status = ind.get("status", "UNKNOWN")
    v = ind.get("value")
    # A status that carries no number (NOT APPLICABLE, INSUFFICIENT DATA) must
    # never render as a figure. A status that DOES carry one (PARTIAL) must not
    # hide it, or the reader cannot see what was actually computed.
    if v is None:
        return status if status != "OK" else "no value recorded"
    if status == "OK":
        return f"{v:.4f}"
    return f"{v:.4f} ({status})"


def fmt_area(ind: dict) -> str:
    if ind.get("value") is None:
        return "—"
    for key, _label in AREA_KEYS:
        if ind.get(key) is not None:
            return f"{ind[key]:,.1f} ha"
    return "—"


def area_label(ind: dict) -> str:
    for key, label in AREA_KEYS:
        if ind.get(key) is not None:
            return label
    return ""


def status_colour(status: str) -> colors.Color:
    return {
        "OK": colors.HexColor("#1d6f42"),
        "NOT APPLICABLE": colors.HexColor("#8a6d00"),
        "INSUFFICIENT DATA": colors.HexColor("#8a2b2b"),
    }.get(status, colors.HexColor("#444444"))


def safe(doc: dict, *path: str, default: Any = None) -> Any:
    cur: Any = doc
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


# ==============================================================================
# SECTIONS
# ==============================================================================

def header_block(doc: dict, st: dict) -> list:
    site = doc.get("site", {})
    name = site.get("name", "unnamed site")
    country = site.get("country", "")
    base = safe(doc, "epochs", "baseline", default={}) or {}
    cur = safe(doc, "epochs", "current", default={}) or {}
    generated = doc.get("generated_utc", "")

    flow = [
        Paragraph("EcoMine Observatory — Screening Report", st["title"]),
        Paragraph(
            f"{name}{', ' + country if country else ''} &nbsp;·&nbsp; "
            f"epochs {base.get('start', '?')} → {cur.get('end', '?')} "
            f"&nbsp;·&nbsp; generated {generated[:19].replace('T', ' ')} UTC",
            st["sub"]),
    ]

    lat, lon = site.get("lat"), site.get("lon")
    buf = site.get("buffer_km")
    rows = [["Area of interest",
             f"{lat}, {lon} · {buf} km radius" if lat is not None else "—"]]

    rule = cur.get("rule")
    if rule:
        rows.append(["Season rule", str(rule).replace("_", " ")])

    suff = doc.get("sufficiency_current_epoch", {})
    if suff:
        rows.append(["Imagery sufficiency",
                     f"{suff.get('verdict', '—')} "
                     f"({suff.get('s2_scenes', '?')} optical, "
                     f"{suff.get('s1_scenes', '?')} radar scenes)"])

    t = Table(rows, colWidths=[42 * mm, 118 * mm])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8.8),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#e0e0e0")),
    ]))
    flow.append(t)
    return flow


def indicators_table(doc: dict, st: dict) -> list:
    inds = doc.get("indicators", {})
    if not inds:
        return [Paragraph("No indicators in this file.", st["body"])]

    header = ["Indicator", "Value", "Area", "Status"]
    rows = [header]
    styles = []

    order = [k for k in ("VLI", "SEI", "WDI", "RDI", "MEI") if k in inds]
    order += [k for k in inds if k not in order]

    for i, key in enumerate(order, start=1):
        ind = inds[key]
        status = ind.get("status", "UNKNOWN")
        rows.append([
            f"{key} — {INDICATOR_NAMES.get(key, '')}",
            fmt_value(ind),
            fmt_area(ind),
            status,
        ])
        styles.append(("TEXTCOLOR", (3, i), (3, i), status_colour(status)))
        if key == "MEI":
            styles.append(("BACKGROUND", (0, i), (-1, i),
                           colors.HexColor("#f4f4f2")))

    t = Table(rows, colWidths=[62 * mm, 34 * mm, 28 * mm, 36 * mm],
              repeatRows=1)
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.7, colors.HexColor("#333333")),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#e0e0e0")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ] + styles))

    flow = [Paragraph("Indicators", st["h2"]), t]

    # Any indicator that is not OK gets its reason spelled out immediately
    # under the table. A status word alone invites the reader to assume zero.
    notes = []
    for key in order:
        ind = inds[key]
        status = ind.get("status")
        if status == "OK":
            continue
        reason = ind.get("reason") or ind.get("caveat") or ""
        # The closing sentence must match what the status actually means.
        # A PARTIAL composite WAS measured, just from fewer components; saying
        # it "could not be measured" would be false, and saying nothing would
        # let a reader treat it as equivalent to a full composite.
        if status == "PARTIAL":
            tail = ("This value was computed from fewer components than a full "
                    "composite and is therefore a different quantity; it must "
                    "not be compared with a full-component figure.")
        elif ind.get("value") is None:
            tail = ("This is not a value of zero; it means the quantity could "
                    "not be measured here.")
        else:
            tail = ("Read this status before using the value.")
        notes.append(f"<b>{key} — {status}.</b> {reason} {tail}")
    for n in notes:
        flow.append(Paragraph(n, st["caveat"]))

    return flow


def interpretation_block(doc: dict, st: dict) -> list:
    """Each indicator's own interpretation string, verbatim from the JSON."""
    inds = doc.get("indicators", {})
    flow = [Paragraph("What each number means", st["h2"])]
    any_written = False
    for key in ("VLI", "SEI", "WDI", "RDI", "MEI"):
        ind = inds.get(key)
        if not ind:
            continue
        text = ind.get("interpretation") or ind.get("caveat")
        if not text:
            continue
        any_written = True
        flow.append(Paragraph(f"<b>{key}.</b> {text}", st["small"]))
    if not any_written:
        flow.append(Paragraph(
            "No interpretation strings were recorded in this file.",
            st["small"]))
    return flow


def method_block(doc: dict, st: dict) -> list:
    m = doc.get("method", {})
    if not m:
        return []
    rows = [
        ["Threshold rule", str(m.get("threshold_rule", "—"))],
        ["Robust sigma", str(m.get("robust_sigma", "—"))],
        ["K (sigma multiplier)", str(m.get("K_SIGMA", "—"))],
        ["Analysis scale", f"{m.get('scale_m', '—')} m"],
        ["VLI applicability gate",
         f"baseline NDVI ≥ {m.get('vli_applicability_gate_ndvi', '—')}"],
    ]
    weights = m.get("mei_weights")
    if weights:
        pretty = ", ".join(f"{k} {v:.2f}" for k, v in weights.items())
        rows.append(["MEI weights", pretty])

    t = Table(rows, colWidths=[42 * mm, 118 * mm])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8.3),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#ececec")),
    ]))
    return [Paragraph("Method", st["h2"]), t]


def provenance_block(doc: dict, st: dict) -> list:
    prov = safe(doc, "site", "coordinate_provenance")
    if not prov:
        return []
    return [
        Paragraph("Coordinate provenance", st["h2"]),
        Paragraph(prov, st["small"]),
    ]


def limitations_block(doc: dict, st: dict) -> list:
    """
    Kept in the main flow, never an appendix. A number must not be liftable
    from this report without the sentence that qualifies it.
    """
    lims = doc.get("limitations", []) or []
    warns = doc.get("warnings", []) or []
    flow = [Paragraph("Limitations — read before quoting any figure", st["h2"])]
    if not lims:
        flow.append(Paragraph(
            "No limitations block was recorded in this file. That absence is "
            "itself a warning: treat these figures as unqualified.",
            st["caveat"]))
    for text in lims:
        flow.append(Paragraph(f"• {text}", st["caveat"]))
    if warns:
        flow.append(Spacer(1, 2 * mm))
        flow.append(Paragraph("Run warnings", st["h2"]))
        for w in warns:
            flow.append(Paragraph(f"• {w}", st["caveat"]))
    return flow


# ==============================================================================
# DOCUMENT
# ==============================================================================

def _footer(canvas, doc_):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.2)
    canvas.setFillColor(colors.HexColor("#777777"))
    canvas.drawString(18 * mm, 12 * mm, FOOTER_NOTE)
    canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, f"page {doc_.page}")
    canvas.setStrokeColor(colors.HexColor("#dddddd"))
    canvas.setLineWidth(0.4)
    canvas.line(18 * mm, 15 * mm, A4[0] - 18 * mm, 15 * mm)
    canvas.restoreState()


def build_report(json_path: str, out_pdf: Optional[str] = None) -> str:
    """Render a Stage 2 provenance JSON as a PDF. Returns the output path."""
    if not os.path.exists(json_path):
        raise FileNotFoundError(
            f"{json_path} not found. Run ecomine_stage2.py first; it writes "
            "this file.")

    with open(json_path, encoding="utf-8") as fh:
        doc = json.load(fh)

    if "indicators" not in doc:
        raise ValueError(
            f"{json_path} has no 'indicators' block. This does not look like "
            "Stage 2 output; rendering it would produce a report about "
            "nothing.")

    if out_pdf is None:
        out_pdf = os.path.splitext(json_path)[0] + "_report.pdf"

    st = _styles()
    pdf = BaseDocTemplate(
        out_pdf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=20 * mm,
        title=f"EcoMine screening report — {safe(doc, 'site', 'name', default='site')}",
        author="EcoMine Observatory",
        subject="Screening indicators over an area of interest. Not a compliance finding.",
    )
    frame = Frame(pdf.leftMargin, pdf.bottomMargin,
                  pdf.width, pdf.height, id="main")
    pdf.addPageTemplates([
        PageTemplate(id="all", frames=[frame], onPage=_footer)])

    flow: list = []
    flow += header_block(doc, st)
    flow += indicators_table(doc, st)
    flow += interpretation_block(doc, st)
    flow += limitations_block(doc, st)
    flow += method_block(doc, st)
    flow += provenance_block(doc, st)

    flow.append(Spacer(1, 4 * mm))
    flow.append(Paragraph(
        "This report renders the contents of "
        f"<font face='Courier'>{os.path.basename(json_path)}</font> without "
        "recomputation. Any figure here can be traced to that file. "
        f"Rendered {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC.",
        st["small"]))

    pdf.build(flow)
    return out_pdf


# ==============================================================================
# CLI
# ==============================================================================

def main():
    p = argparse.ArgumentParser(
        description="Render a Stage 2 provenance JSON as a PDF report")
    p.add_argument("--json", default="ecomine_stage2_indicators.json")
    p.add_argument("--out", default=None,
                   help="output PDF path (default: alongside the JSON)")
    a = p.parse_args()
    try:
        out = build_report(a.json, a.out)
    except (FileNotFoundError, ValueError) as e:
        print(f"ABORT: {e}")
        sys.exit(1)
    print(f"Report written to {out}")
    print(FOOTER_NOTE)


if __name__ == "__main__":
    main()
