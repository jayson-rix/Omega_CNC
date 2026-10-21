"""
OMEGA SAWLINK Mecal library stripper
====================================

Strips a Softech V6 XML job down to a minimal Camquix / Mecal XML that
references the Mecal machine's *on-machine* library by name.

Differences vs. the full ``transformer.transform_mecal`` pipeline:

* No tool resolution, no face mapping, no profile DB required — the Mecal
  CNC already holds the full recipe for each machining op inside its own
  on-machine library; we only emit the ``<Name>`` reference and the
  X-position offset.
* Each surviving ``Work`` block is reduced to ``ANG`` / ``Name`` and a
  single ``<Parameters>`` block only — no ``<Face>``, ``<Tool>``, or
  ``<Position*>`` blocks.
  - ``Name``  = V6 ``WComment``
  - ``ANG``   = 0 for every face EXCEPT FACE 4. V6 machining names carry a
    trailing face token (… ``F1`` / ``F2`` / ``F3`` / ``F4``); a name ending
    in ``F4`` is machined upside-down on the Mecal, so it emits ``ANG`` 180.
  - ``<Parameters>`` carries ``PAR1`` whose ``<Value>`` = V6 ``WX1``
    (the X-position offset, passed to the on-machine macro)
* Only bars / pieces that contain at least one ``Work`` survive —
  pure stock cuts are skipped entirely.
* Bar header uses V6 ``BHeight`` / ``BWidth`` directly for
  ``<MaxY>`` / ``<MaxZ>`` (no profile DB lookup).

Suitable for Mecal MC305 Kosmos and MC305 Gianos (identical input
format).
"""

from __future__ import annotations

import re

try:
    from .parser import Job  # package import
except ImportError:  # script / PyInstaller flat layout
    from parser import Job  # type: ignore[no-redef]


# ── Helpers (kept local so this module is self-contained) ─────────────

def _fmt(value: float) -> str:
    """Whole numbers as int, otherwise trim trailing zeros."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v == int(v):
        return str(int(v))
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _normalise_date(raw: str) -> str:
    raw = (raw or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return raw


# ── Builders ──────────────────────────────────────────────────────────

def _build_work(work) -> str:
    name = (work.wcomment or "").strip() or "UNNAMED"
    par1 = _fmt(work.wx1)
    # FACE 4 rule: every V6 machining name carries a trailing face token
    # (… F1 / F2 / F3 / F4). Machinings on FACE 4 are run upside-down on the
    # Mecal, so they flip to <ANG>180</ANG>; every other face stays <ANG>0</ANG>.
    tokens = name.split()
    ang = 180 if tokens and tokens[-1].upper() == "F4" else 0
    return (
        "        <Work>\n"
        f"          <ANG>{ang}</ANG>\n"
        f"          <Name>{name}</Name>\n"
        "          <Parameters>\n"
        "            <Name>PAR1</Name>\n"
        f"            <Value>{par1}</Value>\n"
        "          </Parameters>\n"
        "        </Work>"
    )


def _build_piece(cut, bar) -> str | None:
    if not cut.works:
        return None
    sxb = cut.canglelh
    dxb = cut.canglerh
    sxc = 90 + cut.canglelv
    dxc = 90 + cut.canglerv
    works_xml = "\n".join(_build_work(w) for w in cut.works)
    return (
        "      <Piece>\n"
        f"        <PieceNumber>{bar.bidentno}</PieceNumber>\n"
        f"        <BarCode>{cut.ccomno}</BarCode>\n"
        "        <Cut>\n"
        f"          <sxB>{_fmt(sxb)}</sxB>\n"
        f"          <dxB>{_fmt(dxb)}</dxB>\n"
        f"          <sxC>{_fmt(sxc)}</sxC>\n"
        f"          <dxC>{_fmt(dxc)}</dxC>\n"
        f"          <Length>{_fmt(cut.clength)}</Length>\n"
        "        </Cut>\n"
        "        <CtrsX>0</CtrsX>\n"
        "        <CtrsYp>0</CtrsYp>\n"
        "        <CtrsYm>0</CtrsYm>\n"
        "        <CtrsZ>0</CtrsZ>\n"
        "        <Material>AL</Material>\n"
        "        <Mirror>0</Mirror>\n"
        "        <Iso></Iso>\n"
        "        <Extra_Pie></Extra_Pie>\n"
        f"{works_xml}\n"
        "      </Piece>"
    )


def _build_bar(bar, bar_number: int) -> tuple[str | None, int, int]:
    """Returns (xml or None, pieces_kept, pieces_skipped)."""
    pieces = []
    skipped = 0
    for cut in bar.cuts:
        block = _build_piece(cut, bar)
        if block is None:
            skipped += 1
            continue
        pieces.append(block)

    if not pieces:
        return None, 0, skipped

    pieces_xml = "\n".join(pieces)
    xml = (
        "    <Bar>\n"
        f"      <DXF_Name>{bar.bidentno}.dxf</DXF_Name>\n"
        f"      <BarNumber>{bar_number}</BarNumber>\n"
        f"      <MaxY>{_fmt(bar.bheight)}</MaxY>\n"
        f"      <MaxZ>{_fmt(bar.bwidth)}</MaxZ>\n"
        "      <Material>AL</Material>\n"
        f"      <Length>{_fmt(bar.blength)}</Length>\n"
        "      <DxfMirror>0</DxfMirror>\n"
        f"{pieces_xml}\n"
        "    </Bar>"
    )
    return xml, len(pieces), skipped


# ── Public API ────────────────────────────────────────────────────────

def transform_mecal_library(job: Job) -> tuple[str, list[str], dict]:
    """
    Transform a parsed V6 Job into a minimal Camquix / Mecal library
    reference XML.

    Returns ``(xml_string, warnings_list, stats_dict)`` where ``stats``
    contains ``bars_in``, ``bars_out``, ``pieces_in``, ``pieces_out``,
    ``works_out``.
    """
    warnings: list[str] = []
    bar_blocks: list[str] = []

    bars_in = len(job.bars)
    pieces_in = sum(len(b.cuts) for b in job.bars)
    pieces_out = 0
    works_out = 0
    bar_number = 0

    for bar in job.bars:
        if not bar.cuts:
            warnings.append(
                f"Bar BIdentNo={bar.bidentno} ({bar.bdescription}) has no "
                f"cuts — skipped."
            )
            continue

        bar_number += 1
        xml, kept, skipped = _build_bar(bar, bar_number)
        if xml is None:
            bar_number -= 1
            continue

        pieces_out += kept
        for cut in bar.cuts:
            if cut.works:
                works_out += len(cut.works)
        bar_blocks.append(xml)

    bars_xml = "\n".join(bar_blocks)
    xml = (
        '<?xml version="1.0" encoding="iso-8859-1"?>\n'
        "<NewDataSet>\n"
        "  <Order>\n"
        "    <Version>1.0</Version>\n"
        "    <Generator>CNC_Preprocessor_v1.0</Generator>\n"
        "    <Unit>mm</Unit>\n"
        f"    <Name>{job.job_num}</Name>\n"
        f"    <Date>{_normalise_date(job.date)}</Date>\n"
        f"    <DateFirstPiece>{_normalise_date(job.date)}</DateFirstPiece>\n"
        f"    <DateLastPiece>{_normalise_date(job.date)}</DateLastPiece>\n"
        f"{bars_xml}\n"
        "  </Order>\n"
        "</NewDataSet>"
    )

    stats = {
        "bars_in":    bars_in,
        "bars_out":   len(bar_blocks),
        "pieces_in":  pieces_in,
        "pieces_out": pieces_out,
        "works_out":  works_out,
    }
    return xml, warnings, stats
