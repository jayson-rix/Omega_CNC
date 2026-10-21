"""
OMEGA SAWLINK Pertici library stripper
======================================

Strips a Softech V6 XML job down to a minimal Pertici P104 PUMA-Export
``.ncw`` text file that references the Pertici machine's *on-machine*
macro / tool library by name.

Differences vs. the full ``pertici_transformer.transform_pertici``
pipeline:

* No tool resolution, no profile-DB / machinings-library overrides —
  the Pertici operator's macro library already holds the full recipe;
  we only emit the ``WToolID`` name reference and the X / Y position.
* ``WToolID`` is set to the V6 ``WComment`` verbatim.  The operator
  arranges the on-machine macro library so each macro name matches a
  WComment string in the upstream V6 file.
* Each ``:WORK`` block carries only WNo, WType, WX1, WY1, WToolID,
  WComment.  All other parameters (WPriority, WBroach, WRotation,
  WAngle, WDT0/1/2 triplets, WPangle*, WPtrans*, WW1-4, etc.) are
  stripped.
* WType mapping: V6 ``C`` / ``L`` / ``R`` pass through (no drill-vs-
  mill split, since we have no tool info in stripped mode);
  V6 ``NOTCH`` → Pertici ``S`` (saw block).
* Only bars / cuts that contain at least one ``Work`` survive — pure
  stock cuts are skipped entirely.
* :OPTIONS / :JOB / :BAR / :CUT header scaffolding preserved verbatim
  from the full Pertici pipeline so the file loads cleanly in
  Pertici CAM.
"""

from __future__ import annotations

import datetime

try:
    from .parser import Job, Bar, Cut, Work  # package import
except ImportError:  # script / PyInstaller flat layout
    from parser import Job, Bar, Cut, Work  # type: ignore[no-redef]


# ── Header / preamble ────────────────────────────────────────────────

_NCW_HEADER_TMPL = (
    "// NCW-PUMA-Export of  Omega CNC Processor\n"
    "// Created : {created}\n"
    "// PP version : 1.07\n"
    ":OPTIONS\n"
    "OScale   = 1\n"
    "OSecSurf = 0\n"
    "OSecIntr = 0\n"
    "OSecExtr = 0\n"
    "\n\n"
)


# ── Helpers (self-contained) ─────────────────────────────────────────

def _fmt(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "0"
    if f == int(f):
        return str(int(f))
    s = f"{f:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def _q(s) -> str:
    if s is None:
        return '""'
    return '"' + str(s).replace('"', "'") + '"'


def _map_wtype(v6_type: str) -> str:
    """V6 WType → Pertici WType in library-reference mode.

    Drill vs mill (Pertici's ``D`` vs ``C``) requires tool info we
    deliberately don't have here, so V6 ``C`` passes through as ``C``
    and the operator's macro library resolves the bit choice.
    """
    t = (v6_type or "").upper()
    if t == "NOTCH":
        return "S"
    if t in ("C", "L", "R"):
        return t
    return t or "C"


# ── Builders ─────────────────────────────────────────────────────────

def _build_work_block(work: Work, w_no: int,
                      warnings: list[str], bar_ident: str) -> str:
    wtype = _map_wtype(work.wtype)
    # WComment is emitted verbatim (no trim, no substitution) so the
    # operator's on-machine macro library can match it character-for-
    # character. Blank comments are emitted blank and a warning is
    # surfaced — silently substituting a placeholder would mask a
    # data-entry bug upstream.
    name = work.wcomment if work.wcomment is not None else ""
    if not name.strip():
        warnings.append(
            f"Bar {bar_ident}: work with blank WComment emitted "
            f"with empty WToolID (will not match any Pertici macro)."
        )
    lines = [
        "  :WORK",
        f"  WNo          = {w_no}",
        f'  WType        = "{wtype}"',
        f"  WX1          = {_fmt(work.wx1)}",
        f"  WY1          = {_fmt(work.wy1)}",
        f"  WToolID      = {_q(name)}",
        f"  WComment     = {_q(name)}",
    ]
    return "\n".join(lines) + "\n\n"


def _build_cut_block(cut: Cut, *, c_no: int, part_no: int,
                     first_in_bar: bool,
                     wno_counter: list[int],
                     warnings: list[str],
                     bar_ident: str) -> str | None:
    """Render one ``:CUT`` block plus its ``:WORK`` children.
    Returns None when the cut has no works (skip)."""
    if not cut.works:
        return None
    cut_loss = 0 if first_in_bar else 1000
    lines = [
        " :CUT",
        f" CNo          = {c_no}",
        " CCount       = 1",
        ' CDescription = "-"',
        " CStation     = 0",
        f' CComNo       = {_q(cut.ccomno)}',
        " CutStart     = 0",
        f" CutLossL     = {cut_loss}",
        f" CutLossR     = {cut_loss}",
        f' CPartNo      = {_q(cut.cpartno or "0")}',
        f" CLength      = {_fmt(cut.clength)}",
        f" CPartNo      = {part_no}",
        f" CAngleLH     = {_fmt(cut.canglelh)}",
        f" CAngleRH     = {_fmt(cut.canglerh)}",
        f" CAngleLV     = {_fmt(cut.canglelv)}",
        f" CAngleRV     = {_fmt(cut.canglerv)}",
        " CRotation    = 0",
        " CGenCuts     = 1",
    ]
    out = "\n".join(lines) + "\n\n"
    for w in cut.works:
        out += _build_work_block(w, wno_counter[0], warnings, bar_ident)
        wno_counter[0] += 1
    return out


def _build_bar_block(bar: Bar, bar_no: int, *,
                     part_counter: list[int], cut_counter: list[int],
                     wno_counter: list[int],
                     stats: dict,
                     warnings: list[str]) -> str | None:
    """Render one ``:BAR`` block. Returns None when the bar has no
    surviving cuts. Cut/part counters are only incremented when a cut
    is actually emitted, so surviving cuts get contiguous CNo / CPartNo
    starting at 0 / 1 even after filtering."""
    cut_blocks: list[str] = []
    for i, cut in enumerate(bar.cuts):
        if not cut.works:
            continue
        block = _build_cut_block(
            cut, c_no=cut_counter[0], part_no=part_counter[0],
            first_in_bar=(i == 0 and bar_no == 0),
            wno_counter=wno_counter,
            warnings=warnings,
            bar_ident=bar.bidentno,
        )
        if block is None:
            continue
        cut_counter[0]  += 1
        part_counter[0] += 1
        cut_blocks.append(block)
        stats["cuts_out"]  += 1
        stats["works_out"] += len(cut.works)

    if not cut_blocks:
        return None

    lines = [
        ":BAR",
        f" BNo          = {bar_no}",
        f' BIdentNo     = {_q(bar.bidentno)}',
        ' BVendorID    = "Profiles"',
        f' BDescription = {_q(bar.bdescription)}',
        ' BVendor      = "Profiles"',
        f" BLength      = {_fmt(bar.blength)}",
        f" BWidth       = {_fmt(bar.bwidth)}",
        f" BHeight      = {_fmt(bar.bheight)}",
        f' BSurface     = {_q(bar.bsurface or "Standard")}',
        " BCount       = 1",
    ]
    return "\n".join(lines) + "\n\n" + "".join(cut_blocks)


# ── Public API ───────────────────────────────────────────────────────

def transform_pertici_library(job: Job) -> tuple[str, list[str], dict]:
    """
    Transform a parsed V6 Job into a minimal Pertici P104 library-
    reference ``.ncw`` text.

    Returns ``(ncw_text, warnings_list, stats_dict)`` where stats
    contains ``bars_in``, ``bars_out``, ``cuts_in``, ``cuts_out``,
    ``works_out``.
    """
    warnings: list[str] = []
    stats = {
        "bars_in":   len(job.bars),
        "bars_out":  0,
        "cuts_in":   sum(len(b.cuts) for b in job.bars),
        "cuts_out":  0,
        "works_out": 0,
    }

    created = datetime.datetime.now().strftime("%A %d.%m.%Y - %H:%M:%S")
    out = _NCW_HEADER_TMPL.format(created=created)

    # :JOB header
    out += ":JOB\n"
    out += 'cncdriver = "1.1"\n'
    out += f'JIdentNo  = {_q(job.job_id or job.job_num)}\n'
    out += 'info      = ""\n'
    out += f'//          {_q(job.date)}\n'
    out += '//          ""\n'
    out += f'//          {_q(job.job_num)}\n'
    out += '//          ""\n'
    for i in range(10):
        out += f"Var{i}      = 0\n"
    out += "\n\n"

    part_counter = [1]
    cut_counter  = [0]
    wno_counter  = [0]
    emitted_bar_no = 0

    for bar in job.bars:
        if not bar.cuts:
            warnings.append(
                f"Bar BIdentNo={bar.bidentno} ({bar.bdescription}) "
                f"has no cuts — skipped."
            )
            continue
        block = _build_bar_block(
            bar, emitted_bar_no,
            part_counter=part_counter, cut_counter=cut_counter,
            wno_counter=wno_counter, stats=stats,
            warnings=warnings,
        )
        if block is None:
            continue
        out += block
        stats["bars_out"] += 1
        emitted_bar_no += 1

    # Trim the trailing blank line for parity with the full pipeline.
    out = out.rstrip("\n") + "\n"
    return out, warnings, stats
