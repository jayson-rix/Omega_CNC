"""
tool_resolver.py — shared incompatible-tool detection + override resolution.

All three CNC transformers (Mecal / FOM / Pertici) auto-select a tool from the
per-machine tool library via the type-aware pickers in `transformer.py`
(`pick_drill_for_hole`, `pick_mill_for_size`, `pick_blade`).  Those pickers are
deliberately non-blocking: when nothing truly fits they fall back to a
"last-resort" tool so a job is never silently dropped.  The side effect is that
a small hole can end up driven by an oversized end-mill (e.g. a 2.77 mm hole
assigned the 6 mm END MILL at tool code "1").

This module adds, in one shared place so the behaviour is identical across all
machines:

  1. `detect_tool_issues(...)` — a read-only pass over a parsed Job that
     classifies every machining's auto-picked tool and returns a *batched*,
     de-duplicated list of `ToolIssue`s (one per distinct WComment that resolves
     to an incompatible / last-resort tool).  Operations that already carry an
     explicit machining-library tool override, or for which the caller has
     already supplied a session override, are not reported.

  2. `comment_key(work)` — the canonical batch key (stripped WComment) shared by
     detection, the override dialog, and the per-transformer application hooks.

The interactive prompt and the application of the chosen overrides live in
`main.py` (GUI) and in each transformer's emit site respectively; this module
holds only the pure, headlessly-testable logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from parser import Job, Work
from transformer import (
    pick_drill_for_hole, pick_mill_for_size, pick_blade,
    _MILL_TYPES, _DRILL_TYPES, _BLADE_TYPES, _f, _strip_prefix,
    _load_tools, _load_profiles,
)

# ── Quality levels ────────────────────────────────────────────────────────────
Q_OK           = "ok"            # exact or acceptable fitting match
Q_INCOMPATIBLE = "incompatible"  # a tool was chosen but it does not fit
Q_NONE         = "none"          # no candidate at all (library left blank)

_FLOAT_TOL = 1e-6


@dataclass
class ToolIssue:
    """One batched incompatible-tool problem, keyed by WComment."""
    comment_key:     str
    wtype:           str
    requested_dia:   Optional[float]
    requested_depth: Optional[float]
    chosen_code:     str
    chosen_label:    str
    quality:         str
    reason:          str
    count:           int = 1
    candidates:      List[Dict[str, str]] = field(default_factory=list)

    def headline(self) -> str:
        dia = "" if self.requested_dia is None else f"Ø{_num(self.requested_dia)}"
        kind = {"C": "hole", "L": "slot", "R": "pocket",
                "NOTCH": "notch"}.get(self.wtype, self.wtype)
        where = f" ×{self.count}" if self.count > 1 else ""
        return f'"{self.comment_key}" ({kind} {dia}){where}'.replace("()", "")


# ── Small helpers ─────────────────────────────────────────────────────────────

def _num(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def comment_key(work: Work) -> str:
    """Canonical batch key for a work: the WComment trimmed of whitespace."""
    return (work.wcomment or "").strip()


def _ov(mach: Optional[Dict[str, Any]], name: str, default):
    """Read a machining-library field override (`mach['fields'][name]`)."""
    if not mach:
        return default
    fields = mach.get("fields") or {}
    v = fields.get(name)
    return default if v in (None, "") else v


def _machining_for(work: Work, prof: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Look up the per-WComment machining override.

    Checks every per-machine library bucket the transformers consult — the
    shared `machinings`, FOM-specific `machinings_fom`, and Pertici-specific
    `machinings_pertici` — so detection skips exactly the operations a
    transformer will treat as explicitly pinned (otherwise a Pertici-only
    pinned tool would be falsely flagged as an issue).
    """
    raw = (work.wcomment or "").strip()
    _, rest = _strip_prefix(raw)
    for db in ((prof or {}).get("machinings") or {},
               (prof or {}).get("machinings_fom") or {},
               (prof or {}).get("machinings_pertici") or {}):
        if raw in db:
            return db[raw]
        if rest and rest in db:
            return db[rest]
    return None


def _label_for(code: str, tools_db: Dict[str, Any]) -> str:
    entry = tools_db.get(str(code)) if code else None
    if not isinstance(entry, dict):
        return str(code)
    typ  = entry.get("type", "?")
    desc = entry.get("description") or entry.get("code") or ""
    dia  = (_f(entry, "drill_diameter") or _f(entry, "outer_dia")
            or _f(entry, "cutter_dia"))
    dia_s = f" Ø{_num(dia)}" if dia is not None else ""
    desc_s = f" {desc}" if desc else ""
    return f"{code} — {typ}{desc_s}{dia_s}".strip()


def _candidates(wtype: str, tools_db: Dict[str, Any]) -> List[Dict[str, str]]:
    """Enabled tools appropriate for the work type, for the override dropdown."""
    if wtype == "C":
        allowed = _DRILL_TYPES | _MILL_TYPES
    elif wtype in ("L", "R"):
        allowed = set(_MILL_TYPES)
    elif wtype == "NOTCH":
        allowed = set(_BLADE_TYPES)
    else:
        allowed = _DRILL_TYPES | _MILL_TYPES | _BLADE_TYPES
    out: List[Dict[str, str]] = []
    for code, v in (tools_db or {}).items():
        if not isinstance(v, dict):
            continue
        if v.get("type") in ("DISABLED", "TAP"):
            continue
        if v.get("type") not in allowed:
            continue
        out.append({"code": str(code), "label": _label_for(str(code), tools_db)})
    return out


def classify(wtype: str, diameter: Optional[float], depth: Optional[float],
             code: Optional[str], tools_db: Dict[str, Any]) -> str:
    """Judge whether the auto-picked tool genuinely fits the operation."""
    if not code:
        return Q_NONE
    entry = tools_db.get(str(code))
    if not isinstance(entry, dict):
        # A legacy numeric fallback code (e.g. "3"/"5") not present in the
        # typed library — cannot be validated, treat as incompatible.
        return Q_INCOMPATIBLE
    typ = entry.get("type")

    if wtype == "NOTCH":
        return Q_OK if typ in _BLADE_TYPES else Q_INCOMPATIBLE

    if diameter is None:
        return Q_OK  # nothing to validate against

    if wtype == "C":
        if typ == "DRILL":
            d = _f(entry, "drill_diameter")
            return Q_OK if d is not None and abs(d - diameter) <= _FLOAT_TOL \
                else Q_INCOMPATIBLE
        if typ == "STEP_DRILL":
            o, i = _f(entry, "outer_dia"), _f(entry, "inner_dia")
            ok = (o is not None and abs(o - diameter) <= _FLOAT_TOL) or \
                 (i is not None and abs(i - diameter) <= _FLOAT_TOL)
            return Q_OK if ok else Q_INCOMPATIBLE
        if typ in _MILL_TYPES:
            cd = _f(entry, "cutter_dia")
            if cd is None or cd > diameter + _FLOAT_TOL:
                return Q_INCOMPATIBLE
            if depth is not None:
                ch = _f(entry, "cutter_height")
                if ch is not None and ch < depth - _FLOAT_TOL:
                    return Q_INCOMPATIBLE
            return Q_OK
        return Q_INCOMPATIBLE

    # L (slot) / R (pocket) — a mill cutter must not be wider than the feature
    if wtype in ("L", "R"):
        if typ not in _MILL_TYPES:
            return Q_INCOMPATIBLE
        cd = _f(entry, "cutter_dia")
        if cd is None or cd > diameter + _FLOAT_TOL:
            return Q_INCOMPATIBLE
        return Q_OK

    return Q_OK


def _reason(wtype: str, diameter, code, label, quality) -> str:
    if quality == Q_NONE:
        if wtype == "NOTCH":
            return ("No saw blade in the tool library "
                    "(HORIZONTAL_BLADE / VERTICAL_BLADE / T_SLOT_BLADE).")
        return "No matching tool in the library for this operation."
    kind = {"C": "hole", "L": "slot", "R": "pocket",
            "NOTCH": "notch"}.get(wtype, wtype)
    dia = "" if diameter is None else f" Ø{_num(diameter)}"
    return (f"Auto-selected {label} is incompatible with the {kind}{dia} "
            f"— it does not fit. Pick a suitable tool or leave it as-is.")


def _auto_pick(wtype: str, diameter, depth, tools_db, prefix) -> Optional[str]:
    if wtype == "C":
        return pick_drill_for_hole(diameter, depth, tools_db, prefix=prefix)
    if wtype == "L":
        return pick_mill_for_size(diameter, tools_db, prefix=prefix)
    if wtype == "R":
        return pick_mill_for_size(diameter, tools_db, prefix=prefix)
    if wtype == "NOTCH":
        return pick_blade(tools_db)
    return None


def _requested_dia(work: Work, wtype: str, mach: Optional[Dict[str, Any]]):
    """Feature size the tool is validated against, honouring field overrides."""
    if wtype == "C":
        return float(_ov(mach, "ww1", work.ww1) or 0) or None
    if wtype == "L":
        return float(_ov(mach, "ww2", work.ww2) or 0) or None
    if wtype == "R":
        w3 = float(_ov(mach, "ww3", work.ww3) or 0)
        return (w3 * 2) if w3 else None
    return None


# ── Public detection pass ─────────────────────────────────────────────────────

def detect_tool_issues(job: Job, config_path: str,
                       profiles_path: Optional[str],
                       tools_path: Optional[str],
                       machine_key: Optional[str] = None,
                       existing_overrides: Optional[Dict[str, str]] = None,
                       ) -> List[ToolIssue]:
    """
    Walk a parsed Job and return one batched ToolIssue per distinct WComment
    whose auto-selected tool is incompatible / missing.

    Operations are NOT reported when:
      * the machining library already pins an explicit `tool`, or
      * `existing_overrides` already supplies a tool for that WComment.
    """
    overrides = existing_overrides or {}
    profiles  = _load_profiles(config_path, profiles_path,
                               machine_key=machine_key)
    tools_db  = _load_tools(config_path, tools_path, machine_key=machine_key)

    issues: Dict[str, ToolIssue] = {}

    for bar in job.bars:
        prof = profiles.get(bar.bidentno) or {}
        for cut in bar.cuts:
            for work in cut.works:
                wtype = work.wtype
                if wtype not in ("C", "L", "R", "NOTCH"):
                    continue
                key = comment_key(work)
                if key in overrides:
                    continue
                mach = _machining_for(work, prof)
                if mach and mach.get("tool") not in (None, "", 0):
                    continue  # explicit per-WComment override already pins a tool

                prefix, _ = _strip_prefix(work.wcomment or "")
                diameter  = _requested_dia(work, wtype, mach)
                depth     = float(_ov(mach, "wdepth", work.wdepth) or 0) or None

                code    = _auto_pick(wtype, diameter, depth, tools_db, prefix)
                quality = classify(wtype, diameter, depth, code, tools_db)
                if quality == Q_OK:
                    continue

                if key in issues:
                    issues[key].count += 1
                    continue

                label = _label_for(code, tools_db) if code else "(none)"
                issues[key] = ToolIssue(
                    comment_key=key,
                    wtype=wtype,
                    requested_dia=diameter,
                    requested_depth=depth,
                    chosen_code=str(code or ""),
                    chosen_label=label,
                    quality=quality,
                    reason=_reason(wtype, diameter, code, label, quality),
                    count=1,
                    candidates=_candidates(wtype, tools_db),
                )

    return list(issues.values())
