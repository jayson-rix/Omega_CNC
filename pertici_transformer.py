"""
pertici_transformer.py  —  Softech V6 XML  →  Pertici P104 `.ncw` text

The Pertici NCW format is a flat key=value text file with four block
types (`:OPTIONS`, `:JOB`, `:BAR`, `:CUT`, `:WORK`) and a `// PUMA`
header preamble.  Schema is **near-identical to V6**, so the
transformer is essentially a serializer that:

  * remaps V6 WType codes  (C → C or D ; L → L ; R → R ; NOTCH → S)
  * names tools by the user-supplied tool **code** (not a number)
  * emits Pertici-only extras (WPriority, WBroach, WDT0/1/2 D/F/M)
    when present in the per-WComment machining override library.

WType selection rule (C vs D):
  V6 C (hole) is rendered as `D` when the selected tool is a
  DRILL / STEP_DRILL, and as `C` when the selected tool is a
  END_MILL / MILL_CUTTER.  WComment steering ([D]/[M]) flows
  through the normal `pick_drill_for_hole` selector.

NOTCH (V6) → S (ncw):
  V6's `WSawPlaneX/Z` and `WPAngleX` become `WPTransX/Z` and
  `WPAngleX`.  All other S-only fields default to the values seen
  in PERTICI-supplied reference `.ncw` files (e.g. `Transom-6935`):
    WX1=0, WY1=0, WX2=BWidth, WY2=0, WPAngleZ=-90, WSide=7.
  Every field is overridable per WComment via the machining library
  (so users tune blade width / Z-offset / face per profile).

Pertici-only override fields (read from `mach['fields']`):
    wpriority   wbroach   wrotation
    wdt0_d wdt0_f wdt0_m   wdt1_d wdt1_f wdt1_m   wdt2_d wdt2_f wdt2_m
  …plus the standard V6 set (wside, wdepth, wheight, ww1-4, wangle).
  Mecal / FOM ignore these extras; only Pertici reads them.

Tool naming:
  WToolID is a free-text string matching a tool name in the Pertici
  machine's tool library (`21MX102_tools.xml`).  The user types the
  exact name into the tool's `code` field in tools.json — e.g.
  "Mill 8L", "Drill 2.77", "Saw120" — and the transformer renders
  it verbatim.  No name mangling.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Tuple

from parser import Job, Bar, Cut, Work
from transformer import (
    _strip_prefix, pick_drill_for_hole, pick_mill_for_size, pick_blade,
    _load_tools, _load_profiles, load_config,
)


# ── Header / preamble ────────────────────────────────────────────────────────

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


# ── Formatting helpers ───────────────────────────────────────────────────────

def _fmt(value) -> str:
    """Number formatter: integers without trailing dot, floats with up to
    four decimals, trailing zeros stripped."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "0"
    if f == int(f):
        return str(int(f))
    s = f"{f:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def _q(s) -> str:
    """Quote a string for `.ncw` (no escaping — Pertici NCW has no
    escape rules; we strip embedded quotes defensively)."""
    if s is None:
        return '""'
    return '"' + str(s).replace('"', "'") + '"'


# ── Per-WComment override lookup ─────────────────────────────────────────────

def _machining_for(work: Work, prof: Dict[str, Any]) -> Dict[str, Any] | None:
    """Look up the machining-library entry by WComment (or stripped form).
    Checks the shared `machinings` bucket (used by Mecal / FOM) first; if
    no entry matches, falls back **per-key** to a Pertici-specific bucket
    `machinings_pertici` so users who keep Pertici overrides separate
    aren't silently ignored when a different machine also has entries."""
    raw = (work.wcomment or "").strip()
    _, rest = _strip_prefix(raw)
    for db in ((prof or {}).get("machinings") or {},
               (prof or {}).get("machinings_pertici") or {}):
        if not db:
            continue
        if raw and raw in db:
            return db[raw]
        if rest and rest in db:
            return db[rest]
    return None


def _tool_name(tool_code: str | None,
               tools_db: Dict[str, Any]) -> str:
    """Resolve a tools_db key to the **free-text tool name** that goes
    into Pertici's `WToolID`.  Falls back to the raw key when the entry
    has no `code` field (defensive — `_load_tools` already defaults
    `code` to the key during migration)."""
    if not tool_code:
        return ""
    entry = tools_db.get(tool_code)
    if isinstance(entry, dict):
        name = entry.get("code")
        if name not in (None, ""):
            return str(name)
    return str(tool_code)


def _ov(mach: Dict[str, Any] | None, name: str, default):
    """Read an override field from `mach['fields']` or fall back."""
    if not mach:
        return default
    fields = mach.get("fields") or {}
    v = fields.get(name)
    return default if v in (None, "") else v


def _ov_or_none(mach: Dict[str, Any] | None, name: str):
    """Same as `_ov` but returns Python `None` when missing (so callers
    can decide whether to emit a line at all)."""
    if not mach:
        return None
    fields = mach.get("fields") or {}
    v = fields.get(name)
    return None if v in (None, "") else v


# ── Build a single :WORK block ───────────────────────────────────────────────

def _build_notch_block(work: Work, cut: Cut, bar: Bar,
                       mach: Dict[str, Any] | None,
                       tools_db: Dict[str, Any],
                       w_no: int, warnings: List[str],
                       tool_overrides: Dict[str, str] | None = None) -> str:
    """Pertici S-type (saw / notch).  Field layout mirrors the supplied
    reference samples; every field is overridable per WComment."""
    blade_key = pick_blade(tools_db) or ""
    if not blade_key:
        warnings.append(
            "  WARNING: Pertici NOTCH (WType=S) requires a blade in the "
            "tool library (HORIZONTAL_BLADE / VERTICAL_BLADE / "
            "T_SLOT_BLADE); WToolID left blank."
        )
    blade_name = _tool_name(blade_key, tools_db)
    # Per-machining override is the literal tool name (free text the
    # user typed in the editor).  Honour it verbatim; otherwise a session
    # override chosen in the batched incompatible-tool prompt (keyed by
    # WComment, a tools_db code) applies.
    if mach and mach.get("tool") not in (None, "", 0):
        blade_name = str(mach["tool"])
    elif tool_overrides:
        _ck = (work.wcomment or "").strip()
        if _ck in tool_overrides:
            blade_name = _tool_name(str(tool_overrides[_ck]), tools_db)

    # Defaults match the reference sample (Transom-6935 J35807 HEAD):
    #   WW1=1, WW2=blade_width, WW3=bheight, WW4=0
    #   WPAngleX=0, WPAngleZ=-90
    #   WPTransX=wsaw_plane_x, WPTransY=0, WPTransZ=wsaw_plane_z
    #   WSide=7
    eff_ww1   = float(_ov(mach, "ww1", 1))
    eff_ww2   = float(_ov(mach, "ww2", 0))
    eff_ww3   = float(_ov(mach, "ww3", bar.bheight or 0))
    eff_ww4   = float(_ov(mach, "ww4", 0))
    # V6 carries the saw-blade tilt as WPAngleX on the work — pass it
    # through as the Pertici WPAngleX default.  WPAngleZ has no V6
    # equivalent; -90 matches the reference sample (Transom-6935).
    wp_ang_x  = float(_ov(mach, "wpangle_x", work.wp_angle_x))
    wp_ang_z  = float(_ov(mach, "wpangle_z", -90))
    wp_trans_x = float(_ov(mach, "wptrans_x", work.wsaw_plane_x))
    wp_trans_y = float(_ov(mach, "wptrans_y", 0))
    wp_trans_z = float(_ov(mach, "wptrans_z", work.wsaw_plane_z))
    w_priority = int(_ov(mach, "wpriority", 0))
    w_side     = int(_ov(mach, "wside", 7))
    w_rot      = int(_ov(mach, "wrotation", 0))

    lines = [
        "  :WORK",
        f'  WType     = "S"',
        f"  WNo       = {w_no}",
        f'  WComment  = {_q(work.wcomment)}',
        f"  WPriority = {_fmt(w_priority)}",
        f"  WSide     = {_fmt(w_side)}",
        f"  WRotation = {_fmt(w_rot)}",
        f"  WX1       = 0",
        f"  WY1       = 0",
        f"  WX2       = {_fmt(bar.bwidth)}",
        f"  WY2       = 0",
        f"  WHeight   = 0",
        f"  WDepth    = 0",
        f"  WW1       = {_fmt(eff_ww1)}",
        f"  WW2       = {_fmt(eff_ww2)}",
        f"  WW3       = {_fmt(eff_ww3)}",
        f"  WW4       = {_fmt(eff_ww4)}",
        f"  WAngle    = 0",
        f"  WPAngleX  = {_fmt(wp_ang_x)}",
        f"  WPAngleZ  = {_fmt(wp_ang_z)}",
        f"  WPTransX  = {_fmt(wp_trans_x)}",
        f"  WPTransY  = {_fmt(wp_trans_y)}",
        f"  WPTransZ  = {_fmt(wp_trans_z)}",
        f'  WToolID  = {_q(blade_name)}',
    ]
    return "\n".join(lines) + "\n\n"


def _build_work_block(work: Work, cut: Cut, bar: Bar,
                      prof: Dict[str, Any], tools_db: Dict[str, Any],
                      w_no: int, warnings: List[str],
                      tool_overrides: Dict[str, str] | None = None) -> str:
    """Build one :WORK block.  Returns "" to skip the work entirely."""
    prefix, _ = _strip_prefix(work.wcomment or "")
    mach = _machining_for(work, prof)

    if work.wtype == "NOTCH":
        return _build_notch_block(work, cut, bar, mach, tools_db,
                                  w_no, warnings, tool_overrides)

    eff_wside   = int(_ov(mach, "wside",   work.wside) or 0)
    eff_wdepth  = float(_ov(mach, "wdepth",  work.wdepth)  or 0)
    eff_wheight = float(_ov(mach, "wheight", work.wheight) or 0)
    eff_ww1     = float(_ov(mach, "ww1", work.ww1) or 0)
    eff_ww2     = float(_ov(mach, "ww2", work.ww2) or 0)
    eff_ww3     = float(_ov(mach, "ww3", work.ww3) or 0)

    # Tool selection — V6 C may resolve to either a drill (→ ncw D)
    # or a mill cutter (→ ncw C).  Resolve to the auto-picked tool's
    # type first; if the user provided a literal override name in the
    # machining library and it matches an entry in tools_db, re-derive
    # the C-vs-D class from THAT tool's type so the emitted WType
    # always agrees with the tool actually being driven.
    if work.wtype == "C":
        tool_key = pick_drill_for_hole(eff_ww1, eff_wdepth, tools_db,
                                       prefix=prefix)
    elif work.wtype == "L":
        tool_key = pick_mill_for_size(eff_ww2, tools_db, prefix=prefix)
    elif work.wtype == "R":
        tool_key = pick_mill_for_size(
            (eff_ww3 * 2) if eff_ww3 else None, tools_db, prefix=prefix)
    else:
        warnings.append(
            f"  WARNING: Pertici — unknown WType '{work.wtype}' on "
            f"CComNo={cut.ccomno}; skipped."
        )
        return ""

    # Session tool override from the batched incompatible-tool prompt
    # (keyed by WComment).  Only applies when the machining library does
    # not already pin a literal tool; the override code is a tools_db key,
    # so the WToolID name and the C/D-type decision below follow from it.
    if tool_overrides and not (mach and mach.get("tool") not in (None, "", 0)):
        _ck = (work.wcomment or "").strip()
        if _ck in tool_overrides:
            tool_key = str(tool_overrides[_ck])

    if not tool_key:
        warnings.append(
            f"  WARNING: Pertici WType={work.wtype} on CComNo={cut.ccomno} "
            f"— no matching tool in library; WToolID left blank."
        )

    # Resolve the WToolID name string.  Order of precedence:
    #   1. mach['tool']  (literal override — free text, wins)
    #   2. tools_db[tool_key].code  (auto-picked tool's declared name)
    #   3. tool_key  (defensive — should already equal `code`)
    tool_name = _tool_name(tool_key, tools_db)
    override_tool = None
    if mach and mach.get("tool") not in (None, "", 0):
        override_tool = str(mach["tool"])
        tool_name = override_tool

    # Determine the effective tool's TYPE for the V6.C → C/D decision.
    # If the override name matches an entry in tools_db (by key OR by
    # code), use that entry's type; otherwise fall back to the
    # auto-picked tool's type.  This guarantees the emitted WType
    # always agrees with the tool actually being driven.
    eff_entry = tools_db.get(tool_key) if tool_key else None
    if override_tool:
        if override_tool in tools_db:
            eff_entry = tools_db[override_tool]
        else:
            for _k, _v in tools_db.items():
                if isinstance(_v, dict) and \
                        str(_v.get("code") or _k) == override_tool:
                    eff_entry = _v
                    break
    eff_ttype = (eff_entry or {}).get("type") if eff_entry else None

    if work.wtype == "C":
        ncw_type = "D" if eff_ttype in ("DRILL", "STEP_DRILL") else "C"
    elif work.wtype == "L":
        ncw_type = "L"
    else:  # R
        ncw_type = "R"

    # Pertici-only extras (defaults match the supplied reference .ncw).
    w_priority = int(_ov(mach, "wpriority", 0))
    w_broach   = int(_ov(mach, "wbroach",   0))
    w_rotation = int(_ov(mach, "wrotation", 0))
    w_angle    = float(_ov(mach, "wangle",  work.wcomment and 180 or 0))
    # WAngle default 180 for L/R follows the reference sample; users
    # can override per-WComment.  Use 0 when no slot/pocket override
    # exists for raw V6 holes — most C/D blocks omit WAngle anyway.
    if ncw_type in ("C", "D"):
        w_angle = float(_ov(mach, "wangle", 0))

    lines: List[str] = [
        "  :WORK",
        f"  WPriority = {_fmt(w_priority)}",
        f'  WType     = "{ncw_type}"',
        f"  WNo       = {w_no}",
        f'  WComment  = {_q(work.wcomment)}',
        f"  WSide     = {_fmt(eff_wside)}",
        f"  WRotation = {_fmt(w_rotation)}",
        f"  WX1       = {_fmt(work.wx1)}",
        f"  WY1       = {_fmt(work.wy1)}",
        f"  WW1       = {_fmt(eff_ww1)}",
    ]
    if ncw_type in ("L", "R"):
        lines.append(f"  WW2       = {_fmt(eff_ww2)}")
    if ncw_type == "R":
        lines.append(f"  WW3       = {_fmt(eff_ww3)}")
    if ncw_type in ("L", "R"):
        lines.append(f"  WAngle    = {_fmt(w_angle)}")
    lines.append(f"  WBroach   = {_fmt(w_broach)}")

    # Angled machinings (e.g. a tilted "sill drain" drill).  V6 carries
    # the approach tilt as WPAngleX on the work itself; the saw (S-type)
    # branch already passes it through, and the same is true for any
    # C/D/L/R work.  Emit the WPAngle/WPTrans block ONLY when the work is
    # actually angled (or a per-WComment override supplies a tilt), so
    # straight holes stay byte-identical to previous output.  This is
    # general: every angled machining in the V6 file is honoured, with no
    # per-machining hardcoding.  WPAngleZ / WPTransY have no V6 source
    # (default 0); WPTransX / WPTransZ pass through the saw-plane fields
    # when present (0 for plain holes).
    ov_wpax = _ov_or_none(mach, "wpangle_x")
    ov_wpaz = _ov_or_none(mach, "wpangle_z")
    ov_wptx = _ov_or_none(mach, "wptrans_x")
    ov_wpty = _ov_or_none(mach, "wptrans_y")
    ov_wptz = _ov_or_none(mach, "wptrans_z")
    has_tilt_override = any(
        v is not None
        for v in (ov_wpax, ov_wpaz, ov_wptx, ov_wpty, ov_wptz)
    )
    if work.wp_angle_x or has_tilt_override:
        wp_ang_x   = float(ov_wpax if ov_wpax is not None else work.wp_angle_x)
        wp_ang_z   = float(ov_wpaz if ov_wpaz is not None else 0)
        wp_trans_x = float(ov_wptx if ov_wptx is not None else work.wsaw_plane_x)
        wp_trans_y = float(ov_wpty if ov_wpty is not None else 0)
        wp_trans_z = float(ov_wptz if ov_wptz is not None else work.wsaw_plane_z)
        lines += [
            f"  WPAngleX  = {_fmt(wp_ang_x)}",
            f"  WPAngleZ  = {_fmt(wp_ang_z)}",
            f"  WPTransX  = {_fmt(wp_trans_x)}",
            f"  WPTransY  = {_fmt(wp_trans_y)}",
            f"  WPTransZ  = {_fmt(wp_trans_z)}",
        ]

    lines += [
        f'  WToolID  = {_q(tool_name)}',
        f"  WHeight   = {_fmt(eff_wheight)}",
        f"  WDepth    = {_fmt(eff_wdepth)}",
    ]

    # Optional WDT0/1/2 D/F/M.  Emit a triplet only when at least one
    # of its three fields is set in the machining library (matches the
    # reference samples where some works have only WDT0, others have
    # all three).
    for idx in (0, 1, 2):
        d_v = _ov_or_none(mach, f"wdt{idx}_d")
        f_v = _ov_or_none(mach, f"wdt{idx}_f")
        m_v = _ov_or_none(mach, f"wdt{idx}_m")
        if d_v is None and f_v is None and m_v is None:
            continue
        lines.append(f"  WDT{idx}D     = {_fmt(d_v if d_v is not None else 0)}")
        lines.append(f"  WDT{idx}F     = {_fmt(f_v if f_v is not None else 1)}")
        lines.append(f"  WDT{idx}M     = {_fmt(m_v if m_v is not None else 0)}")

    return "\n".join(lines) + "\n\n"


# ── Build a single :CUT block ────────────────────────────────────────────────

def _build_cut_block(cut: Cut, bar: Bar, *,
                     c_no: int, part_no: int, first_in_bar: bool,
                     prof: Dict[str, Any], tools_db: Dict[str, Any],
                     wno_counter: List[int], warnings: List[str],
                     tool_overrides: Dict[str, str] | None = None) -> str:
    """Render one `:CUT` block plus all its `:WORK` children."""
    cut_loss = 0 if first_in_bar else 1000
    lines = [
        " :CUT",
        f" CNo          = {c_no}",
        f" CCount       = 1",
        f' CDescription = "-"',
        f" CStation     = 0",
        f' CComNo       = {_q(cut.ccomno)}',
        f" CutStart     = 0",
        f" CutLossL     = {cut_loss}",
        f" CutLossR     = {cut_loss}",
        f' CPartNo      = {_q(cut.cpartno or "0")}',
        f" CLength      = {_fmt(cut.clength)}",
        f" CPartNo      = {part_no}",
        f" CAngleLH     = {_fmt(cut.canglelh)}",
        f" CAngleRH     = {_fmt(cut.canglerh)}",
        f" CAngleLV     = {_fmt(cut.canglelv)}",
        f" CAngleRV     = {_fmt(cut.canglerv)}",
        f" CRotation    = 0",
        f" CGenCuts     = 1",
    ]
    out = "\n".join(lines) + "\n\n"
    for w in cut.works:
        block = _build_work_block(w, cut, bar, prof, tools_db,
                                  wno_counter[0], warnings, tool_overrides)
        if block:
            out += block
            wno_counter[0] += 1
    return out


# ── Build a single :BAR block ────────────────────────────────────────────────

def _build_bar_block(bar: Bar, bar_no: int, *,
                     part_counter: List[int], cut_counter: List[int],
                     wno_counter: List[int],
                     profiles: Dict[str, Any], tools_db: Dict[str, Any],
                     warnings: List[str],
                     tool_overrides: Dict[str, str] | None = None) -> str:
    prof = profiles.get(bar.bidentno) or {}
    if not prof:
        warnings.append(
            f"  INFO: Pertici — profile '{bar.bidentno}' not in profiles.json; "
            f"using V6 bar dimensions and no per-WComment overrides."
        )

    lines = [
        ":BAR",
        f" BNo          = {bar_no}",
        f' BIdentNo     = {_q(bar.bidentno)}',
        f' BVendorID    = "Profiles"',
        f' BDescription = {_q(bar.bdescription)}',
        f' BVendor      = "Profiles"',
        f" BLength      = {_fmt(bar.blength)}",
        f" BWidth       = {_fmt(bar.bwidth)}",
        f" BHeight      = {_fmt(bar.bheight)}",
        f' BSurface     = {_q(bar.bsurface or "Standard")}',
        f" BCount       = 1",
    ]
    out = "\n".join(lines) + "\n\n"
    for i, cut in enumerate(bar.cuts):
        out += _build_cut_block(cut, bar,
                                c_no=cut_counter[0],
                                part_no=part_counter[0],
                                first_in_bar=(i == 0 and bar_no == 0),
                                prof=prof, tools_db=tools_db,
                                wno_counter=wno_counter, warnings=warnings,
                                tool_overrides=tool_overrides)
        cut_counter[0] += 1
        part_counter[0] += 1
    return out


# ── Public API ───────────────────────────────────────────────────────────────

def transform_pertici(job: Job, config_path: str,
                      profiles_path: str | None = None,
                      tools_path: str | None = None,
                      machine_key: str | None = None,
                      tool_overrides: Dict[str, str] | None = None,
                      ) -> Tuple[str, List[str]]:
    """
    Render a parsed V6 Job into Pertici P104 `.ncw` text format.
    Returns (ncw_text, warnings_list).
    """
    warnings: List[str] = []
    _cfg     = load_config(config_path)
    profiles = _load_profiles(config_path, profiles_path,
                              machine_key=machine_key)
    tools_db = _load_tools(config_path, tools_path,
                           machine_key=machine_key)

    created = datetime.datetime.now().strftime("%A %d.%m.%Y - %H:%M:%S")
    out  = _NCW_HEADER_TMPL.format(created=created)

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

    part_counter = [1]   # CPartNo (1-based, increments per cut)
    cut_counter  = [0]   # CNo     (0-based, increments per cut)
    wno_counter  = [0]   # WNo     (0-based, increments per work)

    for bar_no, bar in enumerate(job.bars):
        if not bar.cuts:
            warnings.append(
                f"  INFO: Bar BIdentNo={bar.bidentno} "
                f"({bar.bdescription}) has no cuts — skipped."
            )
            continue
        out += _build_bar_block(bar, bar_no,
                                part_counter=part_counter,
                                cut_counter=cut_counter,
                                wno_counter=wno_counter,
                                profiles=profiles, tools_db=tools_db,
                                warnings=warnings,
                                tool_overrides=tool_overrides)

    return out, warnings
