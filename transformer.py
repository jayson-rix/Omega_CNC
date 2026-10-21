"""
transformer.py — Camquix / Mecal XML transformer
Converts a parsed V6 Job into an XML string matching the Camquix schema.
"""

import json
import os
import re
from typing import Dict, Any

from parser import Job, Bar, Cut, Work


# ── Config loader ─────────────────────────────────────────────────────────────

def load_config(config_path: str) -> Dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    # Strip comment keys before returning
    return {k: v for k, v in raw.items() if not k.startswith("_")}


# ── Date normaliser ───────────────────────────────────────────────────────────

def _normalise_date(raw: str) -> str:
    """
    Ensure date is strictly YYYY-MM-DD (zero-padded).
    Accepts YYYY-M-D, YYYY-MM-DD, DD/MM/YYYY, etc.
    Falls back to raw string if the format is unrecognisable.
    """
    raw = raw.strip()
    # Already correct ISO format
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    # YYYY-M-D  (what V6 Info2 often produces)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # DD/MM/YYYY
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    # Return as-is if unrecognised (better than crashing)
    return raw


# ── Number formatting helper ──────────────────────────────────────────────────

def _fmt(value: float) -> str:
    """Format a number: omit decimal point if whole number."""
    if value == int(value):
        return str(int(value))
    return f"{value:.4f}".rstrip("0").rstrip(".")


# ── Face / side mapping ───────────────────────────────────────────────────────

def _face_points(point_mode: str, bwidth: float, bheight: float) -> tuple:
    """Return (X, Y, Z) for the Face/Points element."""
    m = point_mode
    if m == "top":
        return (0, bheight, bwidth)
    elif m == "bottom":
        return (0, 0, bwidth)
    elif m == "front":
        return (0, 0, 0)
    elif m == "back":
        return (0, bheight, 0)
    elif m == "side-front":
        # Used for vertical profiles (jambs): reference point at (0, MaxZ, 0)
        return (0, bwidth, 0)
    elif m == "side-back":
        return (0, bwidth, bheight)
    else:
        return (0, 0, 0)


# ── Work element builder ──────────────────────────────────────────────────────

def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else "" for line in text.splitlines())


def _build_work(work: Work, cut: Cut, bar: Bar, cfg: Dict[str, Any],
                prof: Dict[str, Any], tools_db: Dict[str, Any],
                warnings: list,
                tool_overrides: Dict[str, str] | None = None) -> str:
    """
    Build one or more <Work> XML blocks.  When the matching machining
    library entry sets `steps` > 1 the same Work block is replicated that
    many times with the depth PAR (PAR3 for C, PAR4 for L, PAR5 for R)
    incrementing from depth/N → depth.  Returns an empty string to skip.
    """

    mecal = cfg["mecal"]
    face_map: Dict[str, Any] = mecal["face_map"]
    tools: Dict[str, Any] = mecal["tools"]   # legacy fallback codes
    # Global offsets — may be overridden per WSide via profiles.json
    _g_depth_offset:        float = mecal["depth_offset"]
    _g_hole_par1_offset:    float = mecal["hole_par1_offset"]
    _g_slot_par1_offset:    float = mecal["slot_par1_offset"]
    pocket_par1_offset:     float = mecal.get("pocket_par1_offset", 0.0)

    # ── Detect WComment prefix steering for tool selection ────────────────────
    wcomment_prefix, _wc_rest = _strip_prefix(work.wcomment or "")

    # Pre-compute the matching machining override entry by WComment so that
    # both NOTCH (PAR7-9) and C/L/R can read its `steps` and field overrides.
    mach_db  = prof.get("machinings") or {}
    # Look up by raw WComment first, then by stripped (prefix-removed) form.
    mach     = mach_db.get((work.wcomment or "").strip())
    if not mach and _wc_rest:
        mach = mach_db.get(_wc_rest)
    mach_fields_all = (mach or {}).get("fields") or {}

    # ── NOTCH: blade notch at head or tail — no face reference needed ──────────
    if work.wtype == "NOTCH":
        macro    = "STD_NOTCH"
        # New v1.3 behaviour: pick a blade from the tool library (no T5 hard-code).
        tool_t = pick_blade(tools_db)
        # Honour an explicit machining-library tool first, else a session
        # override chosen in the batched incompatible-tool prompt (both keyed
        # by WComment); otherwise keep the auto-picked blade.
        if mach and mach.get("tool") not in (None, "", 0):
            tool_t = str(mach["tool"])
        elif tool_overrides:
            _ck = (work.wcomment or "").strip()
            if _ck in tool_overrides:
                tool_t = str(tool_overrides[_ck])
        if tool_t is None:
            tool_t = tools.get("STD_NOTCH", 5)
            warnings.append(
                "  WARNING: NOTCH operation requires a blade in the tool "
                "library (HORIZONTAL_BLADE / VERTICAL_BLADE / T_SLOT_BLADE). "
                "None found — using fallback code "
                f"'{tool_t}'."
            )
        # v1.3.2: depth/limit check against the chosen blade's geometry.
        # PAR1 carries the horizontal cut depth (distance from end of bar
        # to the cut), and bar.bheight - PAR2 is the vertical depth (from
        # top of bar down to the cut).
        _is_head = work.wsaw_plane_x < (cut.clength / 2)
        _horiz   = work.wsaw_plane_x if _is_head \
                   else (cut.clength - work.wsaw_plane_x)
        _vert    = bar.bheight - work.wsaw_plane_z
        _validate_tool_limits(
            tools_db.get(str(tool_t)),
            wtype="NOTCH",
            horiz_required=_horiz,
            vert_required=_vert,
            tool_code=tool_t,
            warnings=warnings,
        )
        # Head/tail is determined by position along the bar, NOT by WPAngleX.
        # Mecal convention (v1.5.2 fix): PAR5=0 → Head end, PAR5=1 → Tail end.
        # If WSawPlaneX is in the first half of the cut → head end (PAR5=0).
        # If WSawPlaneX is in the second half → tail end (PAR5=1), measure from tail.
        is_head  = work.wsaw_plane_x < (cut.clength / 2)
        par5     = 0 if is_head else 1
        par1     = work.wsaw_plane_x if is_head else (cut.clength - work.wsaw_plane_x)
        par2     = work.wsaw_plane_z
        par3     = work.wsaw_notch_width
        par4     = 1                       # 1=Horizontal (default; 3=Vertical rarely)

        # PAR7/8/9 — editable per machining library entry, default 0/0/0.
        # Stored on the machining entry as fields.par7 / par8 / par9.
        def _par(name, default=0):
            v = mach_fields_all.get(name)
            try:
                return float(v) if v not in (None, "") else default
            except (TypeError, ValueError):
                return default
        par7 = _par("par7", 0)
        par8 = _par("par8", 0)
        par9 = _par("par9", 0)

        params = f"""          <Parameters>
            <Name>PAR1</Name>
            <Value>{_fmt(par1)}</Value>
          </Parameters>
          <Parameters>
            <Name>PAR2</Name>
            <Value>{_fmt(par2)}</Value>
          </Parameters>
          <Parameters>
            <Name>PAR3</Name>
            <Value>{_fmt(par3)}</Value>
          </Parameters>
          <Parameters>
            <Name>PAR4</Name>
            <Value>{par4}</Value>
          </Parameters>
          <Parameters>
            <Name>PAR5</Name>
            <Value>{par5}</Value>
          </Parameters>
          <Parameters>
            <Name>PAR6</Name>
            <Value>0</Value>
          </Parameters>
          <Parameters>
            <Name>PAR7</Name>
            <Value>{_fmt(par7)}</Value>
          </Parameters>
          <Parameters>
            <Name>PAR8</Name>
            <Value>{_fmt(par8)}</Value>
          </Parameters>
          <Parameters>
            <Name>PAR9</Name>
            <Value>{_fmt(par9)}</Value>
          </Parameters>"""
        # Notch face/angle/ANG are taken from the profile's reserved "N"
        # WSide entry if the user has configured one (added automatically
        # by auto-sync / scan-from-job and editable in the Profile Editor).
        # If absent, fall back to the historical defaults (ANG=180, all zeros)
        # so existing profiles behave exactly as before.
        notch_ws = (prof.get("wsides") or {}).get("N") or {}
        n_ang     = notch_ws.get("ang",     180)
        n_angle_a = notch_ws.get("angle_a", 0)
        n_angle_c = notch_ws.get("angle_c", 0)
        n_fx      = notch_ws.get("face_x",  0)
        n_fy      = notch_ws.get("face_y",  0)
        n_fz      = notch_ws.get("face_z",  0)
        n_px      = notch_ws.get("pos_x",   0)
        n_py      = notch_ws.get("pos_y_const", 0)
        n_pz      = notch_ws.get("pos_z",   0)
        return f"""        <Work>
          <ANG>{_fmt(n_ang)}</ANG>
          <Name>{macro}</Name>
          <Face>
            <Points>
              <X>{_fmt(n_fx)}</X>
              <Y>{_fmt(n_fy)}</Y>
              <Z>{_fmt(n_fz)}</Z>
            </Points>
            <AngleA>{_fmt(n_angle_a)}</AngleA>
            <AngleC>{_fmt(n_angle_c)}</AngleC>
          </Face>
          <PositionX>{_fmt(n_px)}</PositionX>
          <PositionY>{_fmt(n_py)}</PositionY>
          <PositionZ>{_fmt(n_pz)}</PositionZ>
          <Tool>
            <T>{tool_t}</T>
          </Tool>
{params}
        </Work>"""

    # ── Standard face lookup (C, L, R) ────────────────────────────────────────
    # ── Machining override lookup (by WComment) ──────────────────────────────
    # The profile's "machinings" library lets the user override the actual V6
    # work field values per machining (keyed by WComment): WSide, WHeight,
    # WDepth, WW1, WW2, WW3.  Any field set to None in the entry falls
    # through to the V6 value untouched.
    #
    # v1.5: WX1 and WY1 are no longer overrideable — they are per-cut
    # positions (vary with bar profile and quote) and are always taken
    # straight from the V6 file.  `_ov("wx1", work.wx1)` therefore always
    # returns the V6 value because `mach_fields` no longer carries those
    # keys.
    #
    # Reuse the prefix-aware lookup performed at the top of this function
    # (raw WComment first, then prefix-stripped form), so [SD]/[D]/[M]/[N]
    # comments still find their library entries.
    mach_key    = (work.wcomment or "").strip() or _wc_rest
    mach_fields = mach_fields_all

    def _ov(name: str, default):
        v = mach_fields.get(name)
        return default if v is None else v

    eff_wside   = str(_ov("wside",   work.wside))
    eff_wx1     = _ov("wx1",     work.wx1)
    eff_wy1     = _ov("wy1",     work.wy1)
    eff_wdepth  = _ov("wdepth",  work.wdepth)
    eff_wheight = _ov("wheight", work.wheight)
    eff_ww1     = _ov("ww1",     work.ww1)
    eff_ww2     = _ov("ww2",     work.ww2)
    eff_ww3     = _ov("ww3",     work.ww3)

    if mach and mach_fields.get("wside") is not None and \
            eff_wside not in (prof.get("wsides") or {}) and \
            eff_wside not in face_map:
        warnings.append(
            f"  WARNING: Machining '{mach_key}' on profile "
            f"'{bar.bidentno}' requests WSide={eff_wside} but that side is "
            f"not defined. Falling back to WSide={work.wside}."
        )
        eff_wside = str(work.wside)

    wside_key = eff_wside

    if wside_key not in face_map:
        warnings.append(
            f"  WARNING: Unknown WSide={wside_key} in CComNo={cut.ccomno} "
            f"— skipping work."
        )
        return ""

    # ── Face resolution: profile database takes priority over generic face_map ──
    #
    # If this profile has an explicit wside entry in profiles.json, use those
    # absolute values directly — they are copied from a known-good Camquix file
    # and are guaranteed to match what CAM3D expects for this DXF.
    #
    # If not, fall back to the generic face_map in config.json (computed from
    # BHeight/BWidth) and emit a warning so the user knows to add the profile.

    wside_db = (prof.get("wsides") or {}).get(wside_key)

    if wside_db:
        # ── Database path: fully explicit, no computation ──────────────────────
        ang     = wside_db.get("ang", 180)
        angle_a = wside_db["angle_a"]
        angle_c = wside_db["angle_c"]
        px      = wside_db.get("face_x", 0)
        py      = wside_db.get("face_y", 0)
        pz      = wside_db.get("face_z", 0)
    else:
        # ── Fallback path: generic face_map (may be incorrect for this profile) ─
        face_cfg = face_map[wside_key]
        ang     = face_cfg.get("ang", 180)
        angle_a = face_cfg["angle_a"]
        angle_c = face_cfg["angle_c"]
        px, py, pz = _face_points(face_cfg["point_mode"], bar.bwidth, bar.bheight)

        if prof:
            # Profile is in the database but this WSide is not defined yet
            warnings.append(
                f"  WARNING: Profile '{bar.bidentno}' WSide={work.wside} "
                f"(CComNo={cut.ccomno}) has no entry in profiles.json. "
                f"Using generic face_map — face position may be wrong. "
                f"Add a 'wsides' entry for WSide={work.wside} to fix."
            )

    # Resolve effective offsets.
    # Precedence (highest first): machining override → WSide override → global.
    _ws = wside_db or {}
    def _eff(field: str, ws_default):
        if mach and mach.get(field) is not None:
            return mach[field]
        return ws_default

    depth_offset = _eff(
        "depth_offset",
        _ws["depth_offset"] if "depth_offset" in _ws else _g_depth_offset,
    )
    hole_par1_offset = _eff(
        "hole_par1_offset",
        _ws["hole_par1_offset"] if "hole_par1_offset" in _ws else _g_hole_par1_offset,
    )
    slot_par1_offset = _eff(
        "slot_par1_offset",
        _ws["slot_par1_offset"] if "slot_par1_offset" in _ws else _g_slot_par1_offset,
    )

    pos_x = eff_wx1
    # v1.3 spec: PositionZ is always 0 for C / L / R — actual cut depth is
    # carried by the depth PAR (PAR3 / PAR4 / PAR5).
    pos_z = 0

    # PositionY formula — stored per WSide in the database
    pos_y_formula = (wside_db or {}).get("pos_y", "wy1")
    if pos_y_formula == "max_y - wy1":
        pos_y = bar.bheight - eff_wy1
    elif pos_y_formula == "max_z - wy1":
        pos_y = bar.bwidth - eff_wy1
    else:
        pos_y = eff_wy1

    # v1.3.1: PAR1 = -WHeight (sign flipped) for HOLE / SLOT / POCKET.
    # The STD_HOLE.wrk / STD_SLOT.wrk / STD_POCKET.wrk macros apply the
    # depth / start-point internally via L(L1) / L(L*); the only
    # transform the preprocessor does is the sign flip.
    #
    # NOTE — same rule applies to any future macros that consume L(L1)
    # for the start point on a face/side, including STD_KEYHOLE and
    # STD_DRILL.  When their emission paths are added, set
    #     par1 = -eff_wheight
    # exactly like the three macros below.  Do NOT introduce a PAR1
    # formula or offset for them.

    # ── Tool pre-resolution + limit check + auto-step (v1.3.2) ───────────────
    # Resolve the tool ONCE here (the per-WType branches inside _emit will
    # re-pick the same one) so we can:
    #   1. validate its geometric limits against this operation, and
    #   2. derive the step count from its max_step_milling/drilling field.
    if work.wtype == "C":
        _base_tool = pick_drill_for_hole(eff_ww1, eff_wdepth, tools_db,
                                         prefix=wcomment_prefix)
        _required_dia = eff_ww1
    elif work.wtype == "L":
        _base_tool = pick_mill_for_size(eff_ww2, tools_db,
                                        prefix=wcomment_prefix)
        _required_dia = eff_ww2
    elif work.wtype == "R":
        _required_dia = (eff_ww3 * 2) if eff_ww3 else None
        _base_tool = pick_mill_for_size(_required_dia, tools_db,
                                        prefix=wcomment_prefix)
    else:
        _base_tool = None
        _required_dia = None

    # Machining-library tool override (last-wins) — validate the actual
    # tool that will be emitted, not the auto-picked default.
    _final_code = _base_tool
    if mach and mach.get("tool") not in (None, "", 0):
        _final_code = str(mach["tool"])
    elif tool_overrides:
        _ck = (work.wcomment or "").strip()
        if _ck in tool_overrides:
            _final_code = str(tool_overrides[_ck])
    _tool_entry = tools_db.get(str(_final_code)) if _final_code else None

    _validate_tool_limits(
        _tool_entry,
        wtype=work.wtype,
        w_depth=eff_wdepth,
        required_dia=_required_dia,
        tool_code=_final_code,
        warnings=warnings,
    )

    # ── Steps replication (v1.3.2 — auto from tool, manual override wins) ────
    # The depth PAR (C: PAR3, L: PAR4, R: PAR5) increases from depth/N → depth
    # across N replicated Work blocks.  N comes from:
    #   1. mach['steps'] when explicitly > 1 (manual override), else
    #   2. ceil(WDepth / max_step_*) computed from the chosen tool, else 1.
    try:
        _manual_steps = int((mach or {}).get("steps", 0) or 0)
    except (TypeError, ValueError):
        _manual_steps = 0
    if _manual_steps > 1:
        n_steps = _manual_steps
    else:
        n_steps = _auto_step_count(
            _tool_entry,
            wtype=work.wtype,
            required_dia=_required_dia,
            w_depth=eff_wdepth,
        )
    if n_steps < 1:
        n_steps = 1

    # ── Build a single Work block; depth_par_value is the per-step depth ─────
    def _emit(depth_par_value: float | None) -> str:
        if work.wtype == "C":
            macro  = "STD_HOLE"
            tool_t = pick_drill_for_hole(eff_ww1, eff_wdepth, tools_db,
                                         prefix=wcomment_prefix)
            if tool_t is None:
                tool_t = tools.get("STD_HOLE", 3)
                warnings.append(
                    f"  WARNING: WType=C (hole) — no DRILL/STEP_DRILL/END_MILL "
                    f"in tool library matches Ø{eff_ww1}. Using fallback "
                    f"code '{tool_t}'."
                )
            # v1.3.1: PAR1 = -WHeight (sign flipped).  Per Mecal CMD spec,
            # WHeight=-28 → PAR1=28 and vice versa — that's the only
            # transform.  STD_HOLE.wrk handles the rest via L(L1).
            par1 = -eff_wheight
            par2 = eff_ww1
            par3 = depth_par_value if depth_par_value is not None else eff_wdepth
            params = (
                f"          <Parameters>\n"
                f"            <Name>PAR1</Name>\n"
                f"            <Value>{_fmt(par1)}</Value>\n"
                f"          </Parameters>\n"
                f"          <Parameters>\n"
                f"            <Name>PAR2</Name>\n"
                f"            <Value>{_fmt(par2)}</Value>\n"
                f"          </Parameters>\n"
                f"          <Parameters>\n"
                f"            <Name>PAR3</Name>\n"
                f"            <Value>{_fmt(par3)}</Value>\n"
                f"          </Parameters>"
            )

        elif work.wtype == "L":
            macro  = "STD_SLOT"
            tool_t = pick_mill_for_size(eff_ww2, tools_db,
                                        prefix=wcomment_prefix)
            if tool_t is None:
                tool_t = tools.get("STD_SLOT", 3)
                warnings.append(
                    f"  WARNING: WType=L (slot) — no END_MILL/MILL_CUTTER "
                    f"in tool library fits width {eff_ww2}. Using fallback "
                    f"code '{tool_t}'."
                )
            # v1.3.1: PAR1 = -WHeight (sign flipped).  Per Mecal CMD spec,
            # WHeight=-28 → PAR1=28 and vice versa.  STD_SLOT.wrk owns
            # the start point on the face via L(L1).
            par1 = -eff_wheight
            par2 = eff_ww1
            par3 = eff_ww2
            par4 = depth_par_value if depth_par_value is not None else eff_wdepth
            params = (
                f"          <Parameters>\n"
                f"            <Name>PAR1</Name>\n"
                f"            <Value>{_fmt(par1)}</Value>\n"
                f"          </Parameters>\n"
                f"          <Parameters>\n"
                f"            <Name>PAR2</Name>\n"
                f"            <Value>{_fmt(par2)}</Value>\n"
                f"          </Parameters>\n"
                f"          <Parameters>\n"
                f"            <Name>PAR3</Name>\n"
                f"            <Value>{_fmt(par3)}</Value>\n"
                f"          </Parameters>\n"
                f"          <Parameters>\n"
                f"            <Name>PAR4</Name>\n"
                f"            <Value>{_fmt(par4)}</Value>\n"
                f"          </Parameters>\n"
                f"          <Parameters>\n"
                f"            <Name>PAR5</Name>\n"
                f"            <Value>1</Value>\n"
                f"          </Parameters>"
            )

        elif work.wtype == "R":
            macro  = "STD_POCKET"
            tool_t = pick_mill_for_size(eff_ww3 * 2 if eff_ww3 else None,
                                        tools_db, prefix=wcomment_prefix)
            if tool_t is None:
                tool_t = tools.get("STD_POCKET", 5)
                warnings.append(
                    f"  WARNING: WType=R (pocket) — no END_MILL/MILL_CUTTER "
                    f"in tool library fits corner radius {eff_ww3}. Using "
                    f"fallback code '{tool_t}'."
                )
            # v1.3.1: PAR1 = -WHeight (sign flipped).  Per Mecal CMD spec,
            # WHeight=-28 → PAR1=28 and vice versa.  STD_POCKET.wrk owns
            # the depth math via L(L1).
            par1 = -eff_wheight
            par2 = eff_ww1
            par3 = eff_ww2
            par4 = eff_ww3
            par5 = depth_par_value if depth_par_value is not None else eff_wdepth
            params = (
                f"          <Parameters>\n"
                f"            <Name>PAR1</Name>\n"
                f"            <Value>{_fmt(par1)}</Value>\n"
                f"          </Parameters>\n"
                f"          <Parameters>\n"
                f"            <Name>PAR2</Name>\n"
                f"            <Value>{_fmt(par2)}</Value>\n"
                f"          </Parameters>\n"
                f"          <Parameters>\n"
                f"            <Name>PAR3</Name>\n"
                f"            <Value>{_fmt(par3)}</Value>\n"
                f"          </Parameters>\n"
                f"          <Parameters>\n"
                f"            <Name>PAR4</Name>\n"
                f"            <Value>{_fmt(par4)}</Value>\n"
                f"          </Parameters>\n"
                f"          <Parameters>\n"
                f"            <Name>PAR5</Name>\n"
                f"            <Value>{_fmt(par5)}</Value>\n"
                f"          </Parameters>\n"
                f"          <Parameters>\n"
                f"            <Name>PAR6</Name>\n"
                f"            <Value>1</Value>\n"
                f"          </Parameters>"
            )

        else:
            return ""

        # Tool override from machining library (last-wins) — explicit code.
        # A saved machining-library tool wins; otherwise a session override
        # chosen in the batched incompatible-tool prompt (keyed by WComment)
        # applies.
        eff_tool = tool_t
        if mach and mach.get("tool") not in (None, "", 0):
            eff_tool = str(mach["tool"])
        elif tool_overrides:
            _ck = (work.wcomment or "").strip()
            if _ck in tool_overrides:
                eff_tool = str(tool_overrides[_ck])

        return (
            f"        <Work>\n"
            f"          <ANG>{ang}</ANG>\n"
            f"          <Name>{macro}</Name>\n"
            f"          <Face>\n"
            f"            <Points>\n"
            f"              <X>{_fmt(px)}</X>\n"
            f"              <Y>{_fmt(py)}</Y>\n"
            f"              <Z>{_fmt(pz)}</Z>\n"
            f"            </Points>\n"
            f"            <AngleA>{angle_a}</AngleA>\n"
            f"            <AngleC>{angle_c}</AngleC>\n"
            f"          </Face>\n"
            f"          <PositionX>{_fmt(pos_x)}</PositionX>\n"
            f"          <PositionY>{_fmt(pos_y)}</PositionY>\n"
            f"          <PositionZ>{_fmt(pos_z)}</PositionZ>\n"
            f"          <Tool>\n"
            f"            <T>{eff_tool}</T>\n"
            f"          </Tool>\n"
            f"{params}\n"
            f"        </Work>"
        )

    if work.wtype not in ("C", "L", "R"):
        warnings.append(
            f"  WARNING: CComNo={cut.ccomno} — unrecognised WType='{work.wtype}' "
            f"(expected 'C', 'L', 'R', or 'NOTCH'). This operation has been SKIPPED. "
            f"Check the status log and verify the V6 file."
        )
        return ""

    if n_steps <= 1:
        return _emit(None)

    # Replicate the Work block N times with stepped depth
    blocks = []
    for i in range(1, n_steps + 1):
        blocks.append(_emit(eff_wdepth * i / n_steps))
    return "\n".join(blocks)


# ── Piece builder ─────────────────────────────────────────────────────────────

def _build_piece(cut: Cut, bar: Bar, cfg: Dict[str, Any],
                 prof: Dict[str, Any], tools_db: Dict[str, Any],
                 warnings: list,
                 tool_overrides: Dict[str, str] | None = None) -> str:
    mecal = cfg["mecal"]
    material = mecal["material"]

    # Cut angles pass through directly from V6.
    # CAngleLH/CAngleRH = horizontal saw angle (45 = miter, 90 = square).
    # CAngleLV/CAngleRV = vertical deviation from square (0 = square).
    sxb = cut.canglelh
    dxb = cut.canglerh
    sxc = 90 + cut.canglelv
    dxc = 90 + cut.canglerv

    work_blocks = []
    for work in cut.works:
        block = _build_work(work, cut, bar, cfg, prof, tools_db, warnings,
                            tool_overrides)
        if block:
            work_blocks.append(block)

    works_xml = "\n".join(work_blocks)

    return f"""      <Piece>
        <PieceNumber>{bar.bidentno}</PieceNumber>
        <BarCode>{cut.ccomno}</BarCode>
        <Cut>
          <sxB>{_fmt(sxb)}</sxB>
          <dxB>{_fmt(dxb)}</dxB>
          <sxC>{_fmt(sxc)}</sxC>
          <dxC>{_fmt(dxc)}</dxC>
          <Length>{_fmt(cut.clength)}</Length>
        </Cut>
        <CtrsX>0</CtrsX>
        <CtrsYp>0</CtrsYp>
        <CtrsYm>0</CtrsYm>
        <CtrsZ>0</CtrsZ>
        <Material>{material}</Material>
        <Mirror>0</Mirror>
        <Iso></Iso>
        <Extra_Pie></Extra_Pie>
{works_xml}
      </Piece>"""


# ── Bar builder ───────────────────────────────────────────────────────────────

def _build_bar(bar: Bar, bar_number: int, cfg: Dict[str, Any],
               profiles: Dict[str, Any], tools_db: Dict[str, Any],
               warnings: list,
               tool_overrides: Dict[str, str] | None = None):
    """
    Build a <Bar> XML block containing only cuts that have machining works.
    Cuts with empty Works are saw-only and excluded from the CNC output.
    Returns (xml_string, included_count, skipped_count), or None if no
    cuts in this bar have any works.
    """
    mecal = cfg["mecal"]
    material = mecal["material"]

    dxf_name = f"{bar.bidentno}.dxf"

    # Look up profile in the database to get correct MaxY/MaxZ.
    # V6 BHeight/BWidth can be swapped or mismatched vs the DXF library
    # that CAM3D uses — the profile database provides the authoritative values.
    prof = profiles.get(bar.bidentno) or {}
    if prof:
        max_y = prof["max_y"]
        max_z = prof["max_z"]
        # Override bar dimensions so face coordinates in _build_work are correct
        bar.bheight = max_y
        bar.bwidth  = max_z
    else:
        max_y = bar.bheight
        max_z = bar.bwidth
        warnings.append(
            f"  WARNING: Profile '{bar.bidentno}' not found in profiles.json. "
            f"Using V6 dimensions (MaxY={max_y}, MaxZ={max_z}) — face positions "
            f"may be incorrect. Add this profile to profiles.json to fix."
        )

    dxf_mirror = prof.get("dxf_mirror", 0)

    pieces = []
    skipped = 0
    for cut in bar.cuts:
        if not cut.works:
            skipped += 1
            continue
        pieces.append(_build_piece(cut, bar, cfg, prof, tools_db, warnings,
                                   tool_overrides))

    if not pieces:
        return None, 0, skipped

    pieces_xml = "\n".join(pieces)
    xml = f"""    <Bar>
      <DXF_Name>{dxf_name}</DXF_Name>
      <BarNumber>{bar_number}</BarNumber>
      <MaxY>{_fmt(max_y)}</MaxY>
      <MaxZ>{_fmt(max_z)}</MaxZ>
      <Material>{material}</Material>
      <Length>{_fmt(bar.blength)}</Length>
      <DxfMirror>{dxf_mirror}</DxfMirror>
{pieces_xml}
    </Bar>"""
    return xml, len(pieces), skipped


# ── Public API ────────────────────────────────────────────────────────────────

_VALID_TOOL_TYPES = {
    "DRILL", "STEP_DRILL", "END_MILL", "MILL_CUTTER",
    "HORIZONTAL_BLADE", "VERTICAL_BLADE", "T_SLOT_BLADE",
    "TAP", "DISABLED",
}


def _migrate_tool_entry(code: str, raw: Any) -> Dict[str, Any]:
    """
    Convert a v1.0/v1.2 legacy tool entry into the v1.3 typed shape so the
    selectors (`pick_drill_for_hole`, `pick_mill_for_size`, `pick_blade`)
    can find tools regardless of which schema version the user's tools.json
    happens to be in.

    Already-typed entries pass through unchanged (with `code`/`description`
    defaulted).
    """
    if not isinstance(raw, dict):
        return {"type": "DISABLED", "code": code, "description": "", "fields": {}}

    if isinstance(raw.get("fields"), dict) and isinstance(raw.get("type"), str) \
            and raw["type"] in _VALID_TOOL_TYPES:
        out = dict(raw)
        out.setdefault("code", code)
        out.setdefault("description", "")
        return out

    legacy_type  = (raw.get("type") or "").lower()
    diameter     = raw.get("diameter")
    diameter_max = raw.get("diameter_max")
    description  = raw.get("description", "") or ""
    fields: Dict[str, Any] = {}

    if legacy_type == "drill":
        new_type = "DRILL"
        if diameter is not None:
            fields["drill_diameter"] = diameter
    elif legacy_type == "step_drill":
        new_type = "STEP_DRILL"
        if diameter_max is not None:
            fields["outer_dia"] = diameter_max
        if diameter is not None:
            fields["inner_dia"] = diameter
    elif legacy_type == "mill":
        new_type = "END_MILL"
        if diameter is not None:
            fields["cutter_dia"] = diameter
    elif legacy_type == "saw":
        new_type = "HORIZONTAL_BLADE"
        if diameter is not None:
            fields["blade_dia"]   = diameter
            fields["blade_thick"] = 3
    elif legacy_type == "tap":
        new_type = "TAP"
        if diameter is not None:
            fields["drill_diameter"] = diameter
    elif legacy_type == "disabled":
        new_type = "DISABLED"
        if diameter is not None:
            fields["drill_diameter"] = diameter
    else:
        new_type = "DISABLED"

    return {"type": new_type, "code": code,
            "description": description, "fields": fields}


def _load_tools(config_path: str,
                tools_path: str | None = None,
                machine_key: str | None = None) -> Dict[str, Any]:
    """
    Load tools.json.  Returns a dict keyed by tool slot/code string.

    Supports both the new per-machine schema (`{"machines": {<key>: {"tools":…}}}`)
    and the legacy flat schema (`{"tools": {…}}`).  Every returned entry is
    migrated to the v1.3 typed shape so downstream selectors always see
    `{type, code, description, fields}`.

    machine_key:
        Required for the new schema — selects which machine's tool library to
        load (e.g. "Mecal | MC305 Kosmos").  Ignored for the legacy schema.
    """
    if tools_path is None:
        tools_path = os.path.join(os.path.dirname(config_path), "tools.json")
    if not os.path.exists(tools_path):
        return {}
    with open(tools_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # New schema
    if isinstance(raw.get("machines"), dict):
        machines = raw["machines"]
        if machine_key and machine_key in machines:
            tools = machines[machine_key].get("tools") or {}
        elif not machine_key and machines:
            # Only fall back to the first machine when *no* key was supplied
            # (legacy callers).  When a key is supplied but missing, return
            # empty so we never silently emit another machine's tool numbers.
            first = next(iter(machines.values()))
            tools = first.get("tools") or {}
        else:
            tools = {}
    else:
        # Legacy schema
        tools = raw.get("tools") or {}

    return {
        k: _migrate_tool_entry(k, v)
        for k, v in tools.items()
        if not k.startswith("_")
    }


# ── Tool selectors (v1.3 typed schema) ────────────────────────────────────────
#
# All three selectors return the tool **code** (string, possibly with leading
# "T") as stored in tools.json.  The code is rendered verbatim into the
# <T>…</T> element of the Camquix Work block, so users may use any
# manufacturer-specific code (e.g. Mecal "T22", FOM "6").
#
# Tools whose `type` is "DISABLED" or "TAP" are never considered for
# auto-selection.

_BLADE_TYPES = {"HORIZONTAL_BLADE", "VERTICAL_BLADE", "T_SLOT_BLADE"}
_MILL_TYPES  = {"END_MILL", "MILL_CUTTER"}
_DRILL_TYPES = {"DRILL", "STEP_DRILL"}


def _f(entry: Dict[str, Any], key: str, default=None):
    """Read a typed-tool field (`entry["fields"][key]`) with a default."""
    fields = entry.get("fields") or {}
    v = fields.get(key)
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _strip_prefix(comment: str) -> tuple[str, str]:
    """
    Detect a tool-class steering prefix at the start of a WComment.
    Returns (prefix, rest_of_comment).  Prefix is one of:
        '[SD]', '[D]', '[M]', '[N]'   or empty string.
    """
    c = (comment or "").lstrip()
    for p in ("[SD]", "[D]", "[M]", "[N]"):
        if c.upper().startswith(p):
            return p, c[len(p):].strip()
    return "", c


def _validate_tool_limits(tool_entry: Dict[str, Any] | None,
                          *,
                          wtype: str,
                          w_depth: float | None = None,
                          required_dia: float | None = None,
                          horiz_required: float | None = None,
                          vert_required: float | None = None,
                          tool_code: str | int | None = None,
                          warnings: list | None = None) -> None:
    """
    Validate the chosen tool's geometric limits against the operation
    that will use it.  Emits warning strings into `warnings` (non-blocking
    — the job still proceeds) when a limit is exceeded.  Missing fields
    on the tool are silently skipped (so partially-populated tools don't
    spam warnings).

    Spec (TOOL_LIBRARY_CHANGES_FOR_ALL_MACHINES.txt + v1.5 user testing):
      HORIZONTAL_BLADE: horiz_required ≤ h_cut_limit  AND
                        vert_required  ≤ v_cut_limit
      T_SLOT_BLADE   : w_depth ≤ length_under_holder_to_underside
      DRILL          : w_depth ≤ length_under_holder
      STEP_DRILL     : w_depth ≤ length_under_holder
      END_MILL/MILL_CUTTER:
        1. w_depth ≤ length_under_holder.
        2. Shank-collision: when the cutter is NARROWER than the shank
           (cutter_dia < shank_dia → there's a step from the wide shank
           down to the narrow cutter tip), the bore is too small for the
           shank to follow into.  WARN if w_depth > cutter_height.
           When cutter_dia ≥ shank_dia (no step) the shank cannot foul
           the bore, so no warning regardless of depth.
    """
    if tool_entry is None or warnings is None:
        return
    ttype  = tool_entry.get("type")
    code   = tool_code if tool_code is not None else "?"

    def _warn(msg: str) -> None:
        warnings.append(f"  WARNING: tool '{code}' ({ttype}) {msg} — "
                        f"OPERATION **** EXCEEDS TOOL **** LIMITATIONS.")

    if ttype == "HORIZONTAL_BLADE":
        h_lim = _f(tool_entry, "h_cut_limit")
        v_lim = _f(tool_entry, "v_cut_limit")
        if h_lim is not None and horiz_required is not None \
                and horiz_required > h_lim:
            _warn(f"horizontal cut {horiz_required:g} > h_cut_limit {h_lim:g}")
        if v_lim is not None and vert_required is not None \
                and vert_required > v_lim:
            _warn(f"vertical cut {vert_required:g} > v_cut_limit {v_lim:g}")
        return

    if ttype == "VERTICAL_BLADE":
        # Same fields, axes swapped per the spec — validation deferred
        # until the converter actually emits a vertical-blade operation.
        return

    if ttype == "T_SLOT_BLADE":
        reach = _f(tool_entry, "length_under_holder_to_underside")
        if reach is not None and w_depth is not None and w_depth > reach:
            _warn(f"WDepth {w_depth:g} > holder-to-underside reach {reach:g}")
        return

    if ttype in ("DRILL", "STEP_DRILL"):
        reach = _f(tool_entry, "length_under_holder")
        if reach is not None and w_depth is not None and w_depth > reach:
            _warn(f"WDepth {w_depth:g} > length_under_holder {reach:g}")
        return

    if ttype in ("END_MILL", "MILL_CUTTER"):
        reach = _f(tool_entry, "length_under_holder")
        if reach is not None and w_depth is not None and w_depth > reach:
            _warn(f"WDepth {w_depth:g} > length_under_holder {reach:g}")
        shank_d = _f(tool_entry, "shank_dia")
        cut_h   = _f(tool_entry, "cutter_height")
        # v1.5: collision check.  When the cutter is NARROWER than the shank,
        # the bore made by the cutter is too small for the shank to follow
        # into.  The cutter_height defines how far the wide-tip section
        # reaches before stepping up to the shank, so the shank starts to
        # collide with the workpiece once WDepth exceeds cutter_height.
        # Tested with T54 (shank Ø8, cutter Ø3, height 15) cutting a 3 mm
        # hole at 20 mm depth → required(3) < shank(8) AND cutter_height(15)
        # < WDepth(20) → WARN.  When cutter ≥ shank (e.g. fluted end-mill
        # with no neck) the shank cannot foul the bore, so no warning is
        # emitted regardless of depth (still gated by length_under_holder
        # above).
        if required_dia is not None and shank_d is not None \
                and required_dia < shank_d \
                and cut_h is not None and w_depth is not None \
                and cut_h < w_depth:
            _warn(f"required Ø{required_dia:g} < shank Ø{shank_d:g} "
                  f"AND cutter_height {cut_h:g} < WDepth {w_depth:g} "
                  f"— shank will collide with workpiece")
        return


def _auto_step_count(tool_entry: Dict[str, Any] | None,
                     *, wtype: str,
                     required_dia: float | None,
                     w_depth: float | None) -> int:
    """
    Per the v1.3 step-calc spec:
      For END_MILL / MILL_CUTTER:
        if required_dia == cutter_dia → DRILL operation → use max_step_drilling
        else                          → MILL  operation → use max_step_milling
        n_steps = ceil(w_depth / max_step)   (clamped ≥ 1)
      All other tool types → 1 (single plunge / single pass).

    Applies to all WTypes that use a milling tool — holes (C),
    slots (L), and pockets (R) all get replicated when the chosen
    END_MILL/MILL_CUTTER's max-step would be exceeded by WDepth.
    """
    if tool_entry is None or w_depth is None or w_depth <= 0:
        return 1
    ttype = tool_entry.get("type")
    if ttype not in ("END_MILL", "MILL_CUTTER"):
        return 1
    # Drill-mode (single-plunge bore) only applies to true holes (WType=C)
    # where required_dia == cutter_dia.  Slots (L) and pockets (R) are
    # always milled laterally, so they must always use max_step_milling
    # even when the cutter happens to match the slot width or 2× corner
    # radius.  Without this guard, a 5mm-radius pocket cut with a 10mm
    # mill would incorrectly grab max_step_drilling and skip stepping.
    cutter_dia = _f(tool_entry, "cutter_dia")
    is_drill_mode = (
        wtype == "C"
        and cutter_dia is not None and required_dia is not None
        and abs(cutter_dia - required_dia) < 1e-6
    )
    if is_drill_mode:
        max_step = _f(tool_entry, "max_step_drilling")
    else:
        max_step = _f(tool_entry, "max_step_milling")
    if not max_step or max_step <= 0 or w_depth <= max_step:
        return 1
    import math
    return max(1, math.ceil(w_depth / max_step))


def pick_drill_for_hole(diameter: float | None, depth: float | None,
                        tools_db: Dict[str, Any],
                        prefix: str = "") -> str | None:
    """
    Choose a tool for a WType=C hole.

    Default order (no prefix):
      1. DRILL whose drill_diameter exactly matches `diameter`.
      2. STEP_DRILL whose outer_dia or inner_dia matches `diameter`.
      3. Largest END_MILL / MILL_CUTTER whose cutter_dia ≤ diameter and
         cutter_height ≥ depth (depth check skipped if no depth or no
         cutter_height set on the tool).

    Prefix steering:
      [SD] → restrict to STEP_DRILL only.
      [D]  → drills first (steps 1+2 only); fall back to mill cutter (step 3).
      [M]  → mill cutters / end mills only (skip drills entirely).
      [N]  → blades only (delegates to pick_blade).

    Returns the tool code (string), or None if no candidate found.
    """
    if not tools_db:
        return None

    if prefix == "[N]":
        return pick_blade(tools_db)

    items = [(k, v) for k, v in tools_db.items()
             if isinstance(v, dict)
             and v.get("type") not in ("DISABLED", "TAP")]

    if prefix == "[SD]":
        types_allowed = {"STEP_DRILL"}
    elif prefix == "[M]":
        types_allowed = _MILL_TYPES
    else:
        types_allowed = _DRILL_TYPES | _MILL_TYPES

    items = [(k, v) for k, v in items if v.get("type") in types_allowed]
    if not items:
        return None
    if diameter is None:
        return items[0][0]

    # 1) Exact DRILL match
    if "DRILL" in types_allowed:
        for k, v in items:
            if v.get("type") == "DRILL" and \
                    _f(v, "drill_diameter") == diameter:
                return k

    # 2) Exact STEP_DRILL match (outer or inner)
    if "STEP_DRILL" in types_allowed:
        for k, v in items:
            if v.get("type") != "STEP_DRILL":
                continue
            if _f(v, "outer_dia") == diameter or \
                    _f(v, "inner_dia") == diameter:
                return k

    # 3) Mill-cutter fallback (largest cutter_dia ≤ diameter AND
    #    cutter_height ≥ depth when known)
    mill_candidates = []
    for k, v in items:
        if v.get("type") not in _MILL_TYPES:
            continue
        cd = _f(v, "cutter_dia")
        if cd is None or cd > diameter:
            continue
        if depth is not None:
            ch = _f(v, "cutter_height")
            if ch is not None and ch < depth:
                continue
        mill_candidates.append((k, v, cd))
    if mill_candidates:
        mill_candidates.sort(key=lambda x: x[2], reverse=True)
        return mill_candidates[0][0]

    # 4) Last resort — return any candidate so the job isn't blocked
    return items[0][0]


def pick_mill_for_size(size: float | None,
                       tools_db: Dict[str, Any],
                       prefix: str = "") -> str | None:
    """
    Choose an END_MILL or MILL_CUTTER for a WType=L slot or WType=R pocket.

    Default order:
      1. Tool whose cutter_dia exactly matches `size`.
      2. Largest cutter_dia ≤ size.
      3. Smallest available — so the job isn't blocked.

    Prefix steering:
      [N] → blade lookup (rarely correct for slot/pocket but honoured).
    """
    if not tools_db:
        return None
    if prefix == "[N]":
        return pick_blade(tools_db)

    items = [(k, v) for k, v in tools_db.items()
             if isinstance(v, dict)
             and v.get("type") in _MILL_TYPES
             and v.get("type") not in ("DISABLED", "TAP")]
    if not items:
        return None
    if size is None:
        return items[0][0]

    # Exact match
    for k, v in items:
        if _f(v, "cutter_dia") == size:
            return k

    # Largest fitting
    fitting = [(k, v, _f(v, "cutter_dia"))
               for k, v in items
               if _f(v, "cutter_dia") is not None
               and _f(v, "cutter_dia") <= size]
    if fitting:
        fitting.sort(key=lambda x: x[2], reverse=True)
        return fitting[0][0]

    # Smallest available
    sized = [(k, v, _f(v, "cutter_dia"))
             for k, v in items if _f(v, "cutter_dia") is not None]
    if sized:
        sized.sort(key=lambda x: x[2])
        return sized[0][0]
    return items[0][0]


def pick_blade(tools_db: Dict[str, Any]) -> str | None:
    """First HORIZONTAL_BLADE → VERTICAL_BLADE → T_SLOT_BLADE entry."""
    if not tools_db:
        return None
    for preferred in ("HORIZONTAL_BLADE", "VERTICAL_BLADE", "T_SLOT_BLADE"):
        for k, v in tools_db.items():
            if isinstance(v, dict) and v.get("type") == preferred:
                return k
    return None


def _load_profiles(config_path: str,
                   profiles_path: str | None = None,
                   machine_key: str | None = None) -> Dict[str, Any]:
    """
    Load profiles.json.

    profiles_path, if given, is used directly (supports frozen-app exe dirs).
    If omitted, falls back to the same directory as config.json.

    machine_key:
        When given, only composite keys owned by that machine are returned.
    """
    if profiles_path is None:
        profiles_path = os.path.join(os.path.dirname(config_path),
                                     "profiles.json")
    if not os.path.exists(profiles_path):
        return {}
    with open(profiles_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    # v1.3.1: par1_formula / pocket_par1_formula / hole_par1_offset /
    # slot_par1_offset are no longer read by the transformer.  The
    # STD_*.wrk macros own the depth math, so any legacy keys still on
    # disk are simply ignored.
    all_profs = {k: v for k, v in raw.get("profiles", {}).items()
                 if not k.startswith("_")}

    # Keys are composite "machine_key::pid". Slice for the requested machine
    # and re-key under bare pid for downstream lookup.
    sep = "::"
    out: Dict[str, Any] = {}
    for k, v in all_profs.items():
        if sep in k:
            mk, _, pid = k.partition(sep)
            if not machine_key or mk == machine_key:
                out[pid] = v
    return out


def transform_mecal(job: Job, config_path: str,
                    profiles_path: str | None = None,
                    tools_path: str | None = None,
                    machine_key: str | None = None,
                    tool_overrides: Dict[str, str] | None = None) -> tuple:
    """
    Transform a parsed V6 Job to Camquix/Mecal XML.
    Only bars/pieces that contain at least one machining work are included.
    Bars with no works in any cut are saw-only and are excluded.

    machine_key:
        Identifies which Mecal machine's tool library to use
        (e.g. "Mecal | MC305 Kosmos").  When omitted, falls back to the first
        machine library found in tools.json.

    Returns (xml_string, warnings_list).
    """
    cfg      = load_config(config_path)
    profiles = _load_profiles(config_path, profiles_path,
                              machine_key=machine_key)
    tools_db = _load_tools(config_path, tools_path, machine_key=machine_key)
    warnings = []

    bar_blocks = []
    bar_number = 0           # increments only for included bars
    total_saw_only_cuts = 0
    total_saw_only_bars = 0

    for bar in job.bars:
        if not bar.cuts:
            warnings.append(
                f"  INFO: Bar BIdentNo={bar.bidentno} ({bar.bdescription}) "
                f"has no cuts — skipped."
            )
            continue

        bar_number += 1
        xml, included, skipped = _build_bar(bar, bar_number, cfg, profiles, tools_db, warnings, tool_overrides)

        if xml is None:
            # All cuts had empty Works — saw-only bar, exclude entirely
            bar_number -= 1   # don't count this bar in the sequence
            total_saw_only_bars += 1
            total_saw_only_cuts += skipped
            warnings.append(
                f"  INFO: Bar BIdentNo={bar.bidentno} ({bar.bdescription}) "
                f"— {skipped} cut(s) have no machining works. "
                f"Saw-only bar excluded from CNC output."
            )
            continue

        if skipped:
            total_saw_only_cuts += skipped
            warnings.append(
                f"  INFO: Bar BIdentNo={bar.bidentno} — {skipped} cut(s) "
                f"with no works excluded (saw-only); {included} cut(s) included."
            )

        bar_blocks.append(xml)

    bars_xml = "\n".join(bar_blocks)

    if total_saw_only_bars or total_saw_only_cuts:
        warnings.append(
            f"  SUMMARY: {total_saw_only_bars} bar(s) and "
            f"{total_saw_only_cuts} cut(s) had no machining works and were "
            f"excluded from the CNC output (saw-only). "
            f"{len(bar_blocks)} bar(s) written."
        )

    xml = f"""<?xml version="1.0" encoding="iso-8859-1"?>
<NewDataSet>
  <Order>
    <Version>1.0</Version>
    <Generator>CNC_Preprocessor_v1.0</Generator>
    <Unit>mm</Unit>
    <Name>{job.job_num}</Name>
    <Date>{_normalise_date(job.date)}</Date>
    <DateFirstPiece>{_normalise_date(job.date)}</DateFirstPiece>
    <DateLastPiece>{_normalise_date(job.date)}</DateLastPiece>
{bars_xml}
  </Order>
</NewDataSet>"""

    return xml, warnings
