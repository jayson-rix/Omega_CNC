"""
fom_transformer.py — Softech V6 → FOM FMC 370 / FMC 340 XML

Output shape (verified against the user-supplied reference samples
56875 / 57047 in CHANGES/FOM):

    <?xml version="1.0" encoding="iso-8859-1"?>
    <JOB>
      <VER><MJ>1</MJ><MN>0</MN></VER>
      <HEAD/>
      <BODY>
        <BAR>
          <BRAN>OMEGA</BRAN><SYST>OMEGA</SYST>
          <CODE>{job_num}</CODE>
          <DICL>{bidentno}</DICL><DOCL>{bidentno}</DOCL>
          <LEN>{blength}</LEN><LENR>{leftover}</LENR>
          <H>{bheight}</H><MLT>1</MLT>
          <CUT>...</CUT>
        </BAR>
      </BODY>
    </JOB>

Each cut carries a <MACHININGS> child holding zero or more <MACHINING/>
elements.  WCODE map (V6 → FOM):
    C → #0    drill / hole
    L → #2    slot
    R → #1    rectangular pocket
    NOTCH → #5

Geometry mapping (verified):
    OFFSET   = WX1
    OFFSETY  = WY1
    OFFSETZ  = WHeight   (no sign flip)
    THICK    = WDepth    (PIANO START = 0)
    FACE     = WSide
    VERPERC  = 1 (milling/drilling) ; 0 (notch)
    ANGLE    = 0 normally ; 90 / 270 for notch (head / tail)

    C: VAR1 = WW1 / 2     (radius); VAR2-4 = 0
    L: VAR1 = WW2         (slot length); VAR2 = WW1; VAR3-4 = 0
    R: VAR1 = WW2; VAR2 = WW1; VAR3 = WW3 (radius); VAR4 = 0
    NOTCH: VAR1 = WSawNotchWidth (best-effort pass-through)

Cut geometry:
    ANGL = 90 + CAngleLH ; ANGR = 90 + CAngleRH
    AB1  = 90 + CAngleLV ; AB2  = 90 + CAngleRV
    IL   = CLength       (inner length)
    OL   = CLength - H/tan(CAngleLH) - H/tan(CAngleRH)
    BCOD = CComNo
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Tuple

from parser import Job, Bar, Cut, Work
from transformer import (
    _normalise_date, _fmt, _strip_prefix,
    pick_drill_for_hole, pick_mill_for_size, pick_blade,
    _load_tools, _load_profiles, load_config,
)


# ── WCODE map ────────────────────────────────────────────────────────────────

_WCODE_MAP = {
    "C":     "#0",
    "L":     "#2",
    "R":     "#1",
    "NOTCH": "#5",
}


# ── WSide → FOM FACE map ──────────────────────────────────────────────────────
# V6 <WSide> values map to FOM FACE codes (per client spec):
#   WSide 1 → FACE 1  (TOP)
#   WSide 7 → FACE 1  (special rotated top face)
#   WSide 2 → FACE 2  (BACK)
#   WSide 3 → FACE 3  (FRONT)
#   WSide 6 → FACE 4  (BOTTOM)
# FACE 5/6/7 are not used.
_WSIDE_FACE_MAP = {
    "1": "1",
    "7": "1",
    "2": "2",
    "3": "3",
    "6": "4",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _miter_cutback(height: float, angle_deg: float) -> float:
    """
    Length lost on one end due to a horizontal miter cut.
    For a square cut (angle=90) the cutback is 0.  For a 45° miter on a
    profile of height H the cutback is H.
    """
    a = float(angle_deg or 90)
    if abs(a) < 0.01 or abs(a - 180) < 0.01:
        return 0.0
    return height / math.tan(math.radians(a))


def _machining_for(work: Work, prof: Dict[str, Any]) -> Dict[str, Any] | None:
    """
    Look up the machining override entry by WComment (or stripped form).

    The override library is shared with Mecal (`prof['machinings']`); FOM has
    no separate library to keep the user's per-WComment overrides in one
    place across machines.  Falls back to the legacy `machinings_fom` key if
    a user happens to have it.
    """
    db = (prof or {}).get("machinings") or (prof or {}).get("machinings_fom") or {}
    raw = (work.wcomment or "").strip()
    if raw in db:
        return db[raw]
    _, rest = _strip_prefix(raw)
    if rest and rest in db:
        return db[rest]
    return None


def _ov(mach: Dict[str, Any] | None, name: str, default):
    """Read an override field from `mach['fields']` or fall back to default."""
    if not mach:
        return default
    fields = mach.get("fields") or {}
    v = fields.get(name)
    return default if v in (None, "") else v


# ── Build a single MACHINING element ────────────────────────────────────────

def _build_machining(work: Work, cut: Cut, bar: Bar,
                     prof: Dict[str, Any], tools_db: Dict[str, Any],
                     warnings: List[str],
                     tool_overrides: Dict[str, str] | None = None) -> str:
    wcode = _WCODE_MAP.get(work.wtype)
    if wcode is None:
        warnings.append(
            f"  WARNING: FOM — unknown WType '{work.wtype}' on CComNo="
            f"{cut.ccomno}; skipped."
        )
        return ""

    # WComment-prefix steering
    prefix, _ = _strip_prefix(work.wcomment or "")
    mach = _machining_for(work, prof)

    # Effective V6 fields (machining library overrides)
    eff_wside   = str(_ov(mach, "wside",  work.wside))
    eff_wx1     = float(_ov(mach, "wx1",    work.wx1))
    eff_wy1     = float(_ov(mach, "wy1",    work.wy1))
    eff_wdepth  = float(_ov(mach, "wdepth", work.wdepth))
    eff_wheight = float(_ov(mach, "wheight", work.wheight))
    eff_ww1     = float(_ov(mach, "ww1", work.ww1) or 0)
    eff_ww2     = float(_ov(mach, "ww2", work.ww2) or 0)
    eff_ww3     = float(_ov(mach, "ww3", work.ww3) or 0)

    # NOTCH: geometry comes from the WSaw* fields, not from WX1/WY1 etc.
    if work.wtype == "NOTCH":
        var1 = work.wsaw_notch_width
        var2 = 0.0
        var3 = 0.0
        var4 = 0.0
        offset_x = work.wsaw_plane_x
        offset_y = 0.0
        offset_z = 0.0
        face     = "5"
        verperc  = 0
        # Head end uses ANGLE=90; tail end uses ANGLE=270.
        is_head  = work.wsaw_plane_x < (cut.clength / 2)
        angle    = 90 if is_head else 270
        tool_t   = pick_blade(tools_db) or ""
        if not tool_t:
            warnings.append(
                "  WARNING: FOM NOTCH requires a blade in the tool library "
                "(HORIZONTAL_BLADE / VERTICAL_BLADE / T_SLOT_BLADE)."
            )
        cilinlav = 3   # blades typically use cylinder index 3
    else:
        # Standard mapping
        if work.wtype == "C":
            var1 = (eff_ww1 or 0) / 2.0   # radius
            var2 = 0.0
            var3 = 0.0
            var4 = 0.0
            tool_t = pick_drill_for_hole(eff_ww1, eff_wdepth, tools_db,
                                         prefix=prefix) or ""
            cilinlav = 1
        elif work.wtype == "L":
            var1 = eff_ww2
            var2 = eff_ww1
            var3 = 0.0
            var4 = 0.0
            tool_t = pick_mill_for_size(eff_ww2, tools_db,
                                        prefix=prefix) or ""
            cilinlav = 1
        elif work.wtype == "R":
            var1 = eff_ww2
            var2 = eff_ww1
            var3 = eff_ww3
            var4 = 0.0
            tool_t = pick_mill_for_size(eff_ww3 * 2 if eff_ww3 else None,
                                        tools_db, prefix=prefix) or ""
            cilinlav = 1
        else:
            return ""

        offset_x = eff_wx1
        offset_y = eff_wy1
        offset_z = eff_wheight     # NO sign flip per spec
        # Map V6 WSide → FOM FACE (e.g. WSide 6 BOTTOM → FACE 4).
        wside_key = str(eff_wside).strip()
        face = _WSIDE_FACE_MAP.get(wside_key)
        if face is None:
            face = wside_key
            warnings.append(
                f"  WARNING: FOM WSide={eff_wside} on CComNo={cut.ccomno} "
                f"has no FACE mapping; passed through as FACE={face}."
            )
        # On the BOTTOM face (FACE 4) V6 reports OFFSETY from the opposite
        # datum, so negative values must be shifted up by the extrusion's
        # Y dimension (profile height H / max_y) to land on the correct edge.
        if face == "4" and offset_y < 0:
            offset_y = offset_y + float(bar.bheight or 0)
        verperc  = 1
        angle    = 0

        if not tool_t:
            warnings.append(
                f"  WARNING: FOM WType={work.wtype} on CComNo={cut.ccomno} "
                f"— no matching tool in library; CODUTENSILE left blank."
            )

    # Allow per-machining override of the FOM tool number.  A saved
    # machining-library tool wins; otherwise a session override chosen in
    # the batched incompatible-tool prompt (keyed by WComment) applies.
    if mach and mach.get("tool") not in (None, "", 0):
        tool_t = str(mach["tool"])
    elif tool_overrides:
        _ck = (work.wcomment or "").strip()
        if _ck in tool_overrides:
            tool_t = str(tool_overrides[_ck])

    p1z = bar.bheight   # Face P1 Z = bar height (per reference sample)
    return (
        f'          <MACHINING WCODE="{wcode}" \n'
        f'            VAR1="{_fmt(var1)}" VAR2="{_fmt(var2)}" '
        f'VAR3="{_fmt(var3)}" VAR4="{_fmt(var4)}"\n'
        f'            VERPERC="{verperc}" ANGLE="{_fmt(angle)}" '
        f'OFFSET="{_fmt(offset_x)}" OFFSETY="{_fmt(offset_y)}" '
        f'OFFSETZ="{_fmt(offset_z)}"\n'
        f'            FACE="{face}" P1X="0" P1Y="0" P1Z="{_fmt(p1z)}" '
        f'P2X="0" P2Y="0" P2Z="0" \n'
        f'            EMPTYGROVE="0" ORDINEESECSRG="99999" '
        f'ORDINEESECFIN="99999" \n'
        f'            CODUTENSILE="{tool_t}" CILINLAV="{cilinlav}" '
        f'OFFSETFIN="0" CODEUTEFIN="-1"\n'
        f'            CILLAVFIN="0">\n'
        f'            <PIANI>\n'
        f'           <PIANO START="0" THICK="{_fmt(eff_wdepth)}"/>\n'
        f'            </PIANI>\n'
        f'          </MACHINING>'
    )


# ── Build a single CUT ───────────────────────────────────────────────────────

def _build_cut(cut: Cut, bar: Bar, prof: Dict[str, Any],
               tools_db: Dict[str, Any], warnings: List[str],
               tool_overrides: Dict[str, str] | None = None) -> str:
    """Build a <CUT> element.  Includes <MACHININGS> when works are present."""

    angl = 90 + cut.canglelh
    angr = 90 + cut.canglerh
    ab1  = 90 + cut.canglelv
    ab2  = 90 + cut.canglerv

    h = bar.bheight
    il = cut.clength
    ol = il - _miter_cutback(h, cut.canglelh) - _miter_cutback(h, cut.canglerh)
    if ol < 0:
        ol = il

    machinings = ""
    if cut.works:
        items = []
        for w in cut.works:
            xml = _build_machining(w, cut, bar, prof, tools_db, warnings,
                                   tool_overrides)
            if xml:
                items.append(xml)
        if items:
            inner = "\n".join(items)
            machinings = (
                f"        <MACHININGS>\n"
                f"{inner}\n"
                f"        </MACHININGS>\n"
            )

    return (
        f"      <CUT>\n"
        f"        <ANGL>{_fmt(angl)}</ANGL>\n"
        f"        <ANGR>{_fmt(angr)}</ANGR>\n"
        f"        <AB1>{_fmt(ab1)}</AB1>\n"
        f"        <AB2>{_fmt(ab2)}</AB2>\n"
        f"        <OL>{_fmt(ol)}</OL>\n"
        f"        <IL>{_fmt(il)}</IL>\n"
        f"        <H>{_fmt(h)}</H>\n"
        f"        <TRML></TRML>\n"
        f"        <TAL></TAL>\n"
        f"        <TRMR></TRMR>\n"
        f"        <TAR></TAR>\n"
        f"        <BCOD>{cut.ccomno}</BCOD>\n"
        f"{machinings}"
        f"      </CUT>"
    )


# ── Build a single BAR ───────────────────────────────────────────────────────

def _build_bar(bar: Bar, job: Job, profiles: Dict[str, Any],
               tools_db: Dict[str, Any], warnings: List[str],
               tool_overrides: Dict[str, str] | None = None) -> str:
    prof = profiles.get(bar.bidentno) or {}
    if prof:
        bar.bheight = prof.get("max_y", bar.bheight)
        bar.bwidth  = prof.get("max_z", bar.bwidth)
    else:
        warnings.append(
            f"  WARNING: Profile '{bar.bidentno}' not in profiles.json — "
            f"using V6 dimensions for FOM bar header."
        )

    # Sum of inner-length values; FOM LENR = stock leftover.
    used = sum(c.clength for c in bar.cuts)
    lenr = max(0.0, bar.blength - used)

    cuts_xml = "\n".join(
        _build_cut(c, bar, prof, tools_db, warnings, tool_overrides)
        for c in bar.cuts
    )

    return (
        f"    <BAR>\n"
        f"      <BRAN>OMEGA</BRAN>\n"
        f"      <SYST>OMEGA</SYST>\n"
        f"      <CODE>{bar.bidentno}</CODE>\n"
        f"      <DICL>{bar.bsurface}</DICL>\n"
        f"      <DOCL>{bar.bsurface}</DOCL>\n"
        f"      <LEN>{_fmt(bar.blength)}</LEN>\n"
        f"      <LENR>{_fmt(lenr)}</LENR>\n"
        f"      <H>{_fmt(bar.bheight)}</H>\n"
        f"      <MLT>1</MLT>\n"
        f"{cuts_xml}\n"
        f"    </BAR>"
    )


# ── Public API ───────────────────────────────────────────────────────────────

def transform_fom(job: Job, config_path: str, profiles_path: str | None,
                  tools_path: str | None,
                  machine_key: str | None = None,
                  tool_overrides: Dict[str, str] | None = None,
                  ) -> Tuple[str, List[str]]:
    """
    Render a Job into the FOM FMC 370 / FMC 340 XML format.
    Returns (xml_string, warnings).
    """
    warnings: List[str] = []
    cfg      = load_config(config_path)
    profiles = _load_profiles(config_path, profiles_path,
                              machine_key=machine_key)
    tools_db = _load_tools(config_path, tools_path, machine_key=machine_key)

    bars_xml = "\n".join(
        _build_bar(b, job, profiles, tools_db, warnings, tool_overrides)
        for b in job.bars
    )

    xml = (
        '<?xml version="1.0" encoding="iso-8859-1"?>\n'
        '<JOB>\n'
        '  <VER>\n'
        '    <MJ>1</MJ>\n'
        '    <MN>0</MN>\n'
        '  </VER>\n'
        '  <HEAD></HEAD>\n'
        '  <BODY>\n'
        f'{bars_xml}\n'
        '  </BODY>\n'
        '</JOB>\n'
    )
    return xml, warnings
