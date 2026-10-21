"""
main.py — Omega CNC Processor Desktop Application
Modern customtkinter UI wrapping the V6 → Camquix XML transformation pipeline.
"""

import json
import copy
import math
import os
import re
import shutil
import sys
import threading
import traceback

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox

try:
    from PIL import Image, ImageTk
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

# Resolve asset/config paths whether running from source or as a PyInstaller bundle
if getattr(sys, "frozen", False):
    _BASE_DIR = sys._MEIPASS
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(_BASE_DIR, "config.json")
LOGO_PATH   = os.path.join(_BASE_DIR, "omega_logo.jpg")
BANNER_PATH = os.path.join(_BASE_DIR, "footer_banner.jpg")

# profiles.json and tools.json live next to the exe (writable), not inside the bundle.
# On the very first run after a fresh install the writable copies don't exist yet —
# bootstrap them from the bundled defaults so tool selection works immediately.
if getattr(sys, "frozen", False):
    _PROFS_DIR = os.path.dirname(sys.executable)
else:
    _PROFS_DIR = _BASE_DIR

TOOLS_PATH    = os.path.join(_PROFS_DIR, "tools.json")

# Every CNC machine owns a completely isolated profile and DXF library.
MACHINE_LIBRARIES_DIR = os.path.join(_PROFS_DIR, "machine_libraries")

# Bootstrap tools.json from bundle if missing next to the exe
_bundled_tools = os.path.join(_BASE_DIR, "tools.json")
if not os.path.exists(TOOLS_PATH) and os.path.exists(_bundled_tools):
    import shutil as _shutil
    try:
        _shutil.copy2(_bundled_tools, TOOLS_PATH)
    except OSError:
        pass  # read-only FS — transformer falls back to config defaults

from parser import parse_v6, Job
from transformer import transform_mecal
from fom_transformer import transform_fom
from fom_library_transformer import transform_fom_library
from mecal_library_transformer import transform_mecal_library
from pertici_library_transformer import transform_pertici_library
from pertici_transformer import transform_pertici
from tool_resolver import detect_tool_issues
from writer import write_output
from stubs import generate_stub
from dxf_geometry import (
    fit_scale as dxf_fit_scale,
    load_dxf_file,
    parse_dxf_text,
    primitive_bounds,
)


# ── App constants ─────────────────────────────────────────────────────────────

APP_TITLE    = "OMEGA CNC-zee"
APP_TAGLINE  = "SMART SOLUTIONS FOR SMART MACHINES"
APP_VERSION  = "1.8.9"

# ── Machine catalog ───────────────────────────────────────────────────────────
# Manufacturer → list of supported machine models.
MACHINE_CATALOG: dict[str, list[str]] = {
    "Emmegi":  ["Comet S6", "Comet S4", "Phantomatic T3A"],
    "Fenstek": ["F2", "T200", "T200L", "M4", "CMC-3500", "CMC4-2500"],
    "FOM":     ["Dali 40", "Dali 70", "FMC 120", "FMC 230", "FMC 340",
                "FMC 370", "FMC 440", "FMC 470", "Argo 40", "Argo 70"],
    "Mecal":   ["MC305 Kosmos", "MC305 Gianos", "MC307 Falcon", "MC309 Nike"],
    "Ozgenc":  ["MRT X4", "MRT 600 Pro"],
    "Pertici": ["P104"],
    "Yilmaz":  ["AIM 4310"],
}

# Manufacturers whose CNC output IS implemented end-to-end.
# All other manufacturers still produce a stub placeholder file.
IMPLEMENTED_MANUFACTURERS = {"Mecal", "FOM", "Pertici"}

# Per-machine model implementation gate.  When the manufacturer is
# implemented but a particular model is not, we still drop a stub so the
# user gets a clear "not configured for this model" message.
IMPLEMENTED_MODELS: dict[str, set[str]] = {
    "Mecal":   {"MC305 Kosmos", "MC305 Gianos"},
    "FOM":     {"FMC 370", "FMC 340"},
    "Pertici": {"P104"},
}

DEFAULT_MANUFACTURER = "Mecal"
DEFAULT_MACHINE      = "MC305 Kosmos"

# Reserved WSide key used to hold the face/angle/ANG configuration that the
# transformer stamps into every STD_NOTCH (T5) work block for this profile.
# Notches in the V6 file have no WSide reference of their own, so we attach
# one synthetic entry per profile and let the user edit it like any other
# face. See ProfileEditorWindow and transformer._build_work for usage.
NOTCH_WSIDE_KEY = "N"
TEAMVIEWER_URL       = "https://www.teamviewer.com/en/download/"
SUPPORT_EMAIL        = "jayson@omegawindows.co.nz"


def _machine_key(manufacturer: str, machine: str) -> str:
    """Stable identifier used as JSON key — e.g. 'Mecal | MC305 Kosmos'."""
    return f"{manufacturer} | {machine}"


def _split_machine_key(key: str) -> tuple[str, str]:
    """Inverse of _machine_key.  Returns (manufacturer, machine)."""
    if " | " in key:
        m, n = key.split(" | ", 1)
        return m.strip(), n.strip()
    return DEFAULT_MANUFACTURER, key.strip()


def _all_machine_keys() -> list[str]:
    """Every machine_key from the catalog, in display order."""
    out = []
    for mfr in MACHINE_CATALOG:
        for mac in MACHINE_CATALOG[mfr]:
            out.append(_machine_key(mfr, mac))
    return out


# ── v1.5.1: Per-machine profile-key composition ──────────────────────────────
# Profile keys remain composite "machine_key::pid" inside each isolated
# machine file, preventing imported legacy IDs from ever crossing machines.
PROFILE_KEY_SEP = "::"


def _compose_pkey(mkey: str, pid: str) -> str:
    return f"{mkey}{PROFILE_KEY_SEP}{pid}"


def _decompose_pkey(key: str) -> tuple[str | None, str]:
    """Returns (machine_key | None, pid).  Bare keys → (None, key)."""
    if PROFILE_KEY_SEP in key:
        mk, _, pid = key.partition(PROFILE_KEY_SEP)
        return (mk or None, pid)
    return (None, key)


def _machine_dir_slug(mkey: str) -> str:
    """Filesystem-safe slug for a machine key, used for per-machine DXF
    image folders.  e.g. 'Mecal | MC305 Kosmos' → 'Mecal_MC305_Kosmos'."""
    out = []
    for ch in mkey:
        if ch.isalnum() or ch in "-.":
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out)
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_")


def machine_library_dir(manufacturer: str, machine: str) -> str:
    return os.path.join(
        MACHINE_LIBRARIES_DIR,
        _machine_dir_slug(_machine_key(manufacturer, machine)),
    )


def machine_profiles_path(manufacturer: str, machine: str) -> str:
    return os.path.join(machine_library_dir(manufacturer, machine), "profiles.json")


def machine_dxf_library_dir(manufacturer: str, machine: str) -> str:
    return os.path.join(machine_library_dir(manufacturer, machine), "dxf_library")


SUITES = (
    ("300", "300 Residential", (("300", "General"), ("301", "Awning Window"), ("303", "Sliding Window"), ("309", "Hinged Doors"), ("311", "Sliding Door"), ("312", "Alpha Sliding Door"), ("313", "Bifold Door"), ("315", "Retrofit"), ("397", "Coupling"))),
    ("400", "400 Thermal", (("401", "Awning Window"), ("402", "Replacement Window"), ("409", "Hinged Door"), ("411", "Sliding Door"), ("413", "Bifold Door"), ("415", "Roof Light"))),
    ("500", "500 Architectural", (("501", "Awning Window"), ("509", "Hinged Door"), ("512", "Sliding Door"), ("513", "Bifold Door"), ("519", "Conservatory"), ("521", "Pivot Door"))),
    ("600", "600 Commercial", (("601", "40mm Awning Window"), ("610", "75mm Shopfront"), ("611", "Sliding Door Shopfront"), ("612", "SG Shopfront"), ("615", "100mm DG Shopfront"), ("622", "106mm Flushglaze"), ("633", "159mm Flushglaze"), ("650", "Pacific Door"), ("653", "Commercial Bifold"), ("654", "Top Hung Slider"))),
    ("700", "700 Thermal Commercial", (("701", "56mm Thermal Awning Window"), ("717", "100mm Flushglaze Thermal Commercial"), ("722", "120mm Flushglaze Thermal Commercial"), ("732", "150mm Flushglaze Thermal Commercial"))),
    ("800", "800 Thermal Residential", (("801", "Window"), ("809", "Hinged / Bifold Door"), ("811", "Sliding Door"), ("812", "Alpha Sliding Door"), ("813", "Bifold Door"), ("821", "LT TH Awning Window"), ("823", "LT TH Sliding Door"))),
)


def infer_suite_type(profile_id: str) -> tuple[str | None, str | None]:
    """Return suite/type inferred from an ID such as J811016.

    Catalogued types retain their exact code. Other valid 3xx–8xx prefixes
    fall back to that suite's General folder (for example 31819 → 300/300).
    """
    match = re.search(r"(?<!\d)([3-8]\d{2})", profile_id or "")
    if not match:
        return None, None
    raw_type = match.group(1)
    for suite, _name, types in SUITES:
        if any(code == raw_type for code, _ in types):
            return suite, raw_type
    suite_code = f"{raw_type[0]}00"
    if any(code == suite_code for code, _name, _types in SUITES):
        return suite_code, suite_code
    return None, None


def canonical_type_code(type_code: str | None) -> str | None:
    """Return the storage grouping for a type (813 shares the 809 folder)."""
    return "809" if type_code == "813" else type_code


def suite_filter_types(suite_code: str) -> list[tuple[str, str]]:
    """Return a suite's selectable types, including a General fallback."""
    for code, _name, types in SUITES:
        if code != suite_code:
            continue
        entries = list(types)
        if not any(type_code == code for type_code, _ in entries):
            entries.insert(0, (code, "General"))
        return entries
    return []


def _safe_folder_name(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", value).strip().rstrip(".")


def suite_type_folder(profile_id: str) -> tuple[str, str] | None:
    """Return named suite/type folders for a profile ID."""
    suite_code, raw_type = infer_suite_type(profile_id)
    storage_type = canonical_type_code(raw_type)
    if not suite_code or not storage_type:
        return None
    suite_name = next(
        (name for code, name, _types in SUITES if code == suite_code),
        suite_code,
    )
    if storage_type == "809":
        type_name = "Hinged - Bifold Door"
    else:
        type_name = "General" if storage_type == suite_code else next(
            (name for _suite, _name, types in SUITES
             for code, name in types if code == storage_type),
            "General",
        )
    return (
        _safe_folder_name(suite_name),
        _safe_folder_name(f"{storage_type} {type_name}"),
    )


def canonical_dxf_path(profile_id: str,
                       manufacturer: str = DEFAULT_MANUFACTURER,
                       machine: str = DEFAULT_MACHINE) -> str | None:
    """Return the canonical suite/type DXF path, or None for unknown IDs."""
    folders = suite_type_folder(profile_id)
    if not folders:
        return None
    safe_profile_id = _safe_folder_name(profile_id)
    return os.path.join(
        machine_dxf_library_dir(manufacturer, machine),
        folders[0], folders[1], f"{safe_profile_id}.dxf",
    )


def profile_matches_filter(profile_id: str, description: str, query: str,
                           suite: str = "", type_code: str = "") -> bool:
    """Pure library filter used by the desktop profile list."""
    found_suite, found_type = infer_suite_type(profile_id)
    haystack = f"{profile_id} {description}".lower()
    return ((not query or query.lower() in haystack)
            and (not suite or found_suite == suite)
            and (not type_code or found_type == type_code))


def parse_ascii_dxf(text: str) -> list[tuple[str, tuple[float, ...]]]:
    """Parse visible model-space DXF entities into drawable primitives.

    Kept as the text-based compatibility entry point for tests and callers;
    production file loading uses ``load_dxf_file`` so binary DXF is supported.
    """
    return parse_dxf_text(text)


def _bootstrap_dxf_library_dirs() -> None:
    for manufacturer, machines in MACHINE_CATALOG.items():
        for machine in machines:
            root = machine_library_dir(manufacturer, machine)
            profiles_path = machine_profiles_path(manufacturer, machine)
            try:
                os.makedirs(root, exist_ok=True)
                if not os.path.exists(profiles_path):
                    _atomic_write_json(profiles_path, {
                        "_comment": [
                            "Omega CNC Processor — machine profile database.",
                            "Records are created only by Save SIDE or Save Selected Machining.",
                        ],
                        "profiles": {},
                    })
            except OSError:
                continue
            created: set[tuple[str, str]] = set()
            for suite, _name, _types in SUITES:
                for type_code, _type_name in suite_filter_types(suite):
                    folders = suite_type_folder(f"J{type_code}000")
                    if not folders or folders in created:
                        continue
                    created.add(folders)
                    try:
                        os.makedirs(os.path.join(
                            machine_dxf_library_dir(manufacturer, machine),
                            *folders,
                        ), exist_ok=True)
                    except OSError:
                        pass


def _packaging_smoke_check() -> list[str]:
    """Return errors found in a freshly bootstrapped packaged library tree."""
    errors: list[str] = []
    expected_folders = {
        suite_type_folder(f"J{type_code}000")
        for suite, _name, _types in SUITES
        for type_code, _label in suite_filter_types(suite)
    }
    expected_folders.discard(None)

    for manufacturer, machines in MACHINE_CATALOG.items():
        for machine in machines:
            profiles_path = machine_profiles_path(manufacturer, machine)
            try:
                with open(profiles_path, encoding="utf-8") as fh:
                    profiles = json.load(fh).get("profiles")
            except (OSError, ValueError, AttributeError) as exc:
                errors.append(f"{profiles_path}: {exc}")
                continue
            if profiles != {}:
                errors.append(f"{profiles_path}: profiles must be empty")

            dxf_root = machine_dxf_library_dir(manufacturer, machine)
            actual_folders = {
                (
                    os.path.relpath(path, dxf_root).split(os.sep, 1)[0],
                    os.path.relpath(path, dxf_root).split(os.sep, 1)[1],
                )
                for suite_path in (
                    os.path.join(dxf_root, suite_name)
                    for suite_name in os.listdir(dxf_root)
                )
                if os.path.isdir(suite_path)
                for path in (
                    os.path.join(suite_path, type_name)
                    for type_name in os.listdir(suite_path)
                )
                if os.path.isdir(path)
            } if os.path.isdir(dxf_root) else set()
            if actual_folders != expected_folders:
                missing = sorted(expected_folders - actual_folders)
                extra = sorted(actual_folders - expected_folders)
                errors.append(
                    f"{dxf_root}: missing={missing!r}, extra={extra!r}"
                )
    return errors


# ── Tools.json storage with migration ─────────────────────────────────────────
#
# v1.3 schema (typed entries, free-form code, no T-prefix needed):
#     {"machines": {"Mecal | MC305 Kosmos": {"tools": {
#         "<code>": {"type": "DRILL"|"STEP_DRILL"|"END_MILL"|"MILL_CUTTER"|
#                            "HORIZONTAL_BLADE"|"VERTICAL_BLADE"|
#                            "T_SLOT_BLADE"|"TAP"|"DISABLED",
#                    "code": "<code>",
#                    "description": "...",
#                    "fields": {...type-specific fields...}}, ...}}}}
#
# Type-specific fields:
#     DRILL              — drill_diameter, length_under_holder
#     STEP_DRILL         — outer_dia, inner_dia, step_height, length_under_holder
#     END_MILL/MILL_CUTTER — shank_dia, cutter_dia, cutter_height,
#                            max_step_milling, max_step_drilling,
#                            length_under_holder
#     HORIZONTAL_BLADE   — blade_dia, blade_thick, h_cut_limit, v_cut_limit
#     VERTICAL_BLADE     — blade_dia, blade_thick, h_cut_limit, v_cut_limit
#     T_SLOT_BLADE       — blade_dia, blade_thick, shank_dia,
#                            length_under_holder_to_underside
#
# Older schemas (auto-migrated on first read):
#     v1.2:  {"machines": {<key>: {"tools": {"T3": {"type":"mill","diameter":3,
#            "description":"…","diameter_max":…}}}}}
#     v1.0:  {"tools": {"T3": {...}}}  →  goes under DEFAULT machine.

# Canonical type tokens used by selectors and UI.
TOOL_TYPES = [
    "DRILL", "STEP_DRILL",
    "END_MILL", "MILL_CUTTER",
    "HORIZONTAL_BLADE", "VERTICAL_BLADE", "T_SLOT_BLADE",
    "TAP", "DISABLED",
]

# Per-type editable field labels (label, var-suffix). Order is the order
# shown in the Tool Library editor.
TOOL_TYPE_FIELDS: dict[str, list[tuple[str, str]]] = {
    "DRILL": [
        ("Drill diameter (mm)",                "drill_diameter"),
        ("Length under tool holder (mm)",      "length_under_holder"),
    ],
    "STEP_DRILL": [
        ("Outer drill diameter (mm)",          "outer_dia"),
        ("Inner drill diameter (mm)",          "inner_dia"),
        ("Height of step (mm)",                "step_height"),
        ("Length under tool holder (mm)",      "length_under_holder"),
    ],
    "END_MILL": [
        ("Shank diameter (mm)",                "shank_dia"),
        ("Cutter diameter (mm)",               "cutter_dia"),
        ("Cutter height (mm)",                 "cutter_height"),
        ("Max step when milling (mm)",         "max_step_milling"),
        ("Max step when drilling (mm)",        "max_step_drilling"),
        ("Length under tool holder (mm)",      "length_under_holder"),
    ],
    "MILL_CUTTER": [
        ("Shank diameter (mm)",                "shank_dia"),
        ("Cutter diameter (mm)",               "cutter_dia"),
        ("Cutter height (mm)",                 "cutter_height"),
        ("Max step when milling (mm)",         "max_step_milling"),
        ("Max step when drilling (mm)",        "max_step_drilling"),
        ("Length under tool holder (mm)",      "length_under_holder"),
    ],
    "HORIZONTAL_BLADE": [
        ("Blade diameter (mm)",                "blade_dia"),
        ("Blade thickness (mm)",               "blade_thick"),
        ("Horizontal cut limit (mm)",          "h_cut_limit"),
        ("Vertical cut limit (mm)",            "v_cut_limit"),
    ],
    "VERTICAL_BLADE": [
        ("Blade diameter (mm)",                "blade_dia"),
        ("Blade thickness (mm)",               "blade_thick"),
        ("Horizontal cut limit (mm)",          "h_cut_limit"),
        ("Vertical cut limit (mm)",            "v_cut_limit"),
    ],
    "T_SLOT_BLADE": [
        ("Blade diameter (mm)",                "blade_dia"),
        ("Blade thickness (mm)",               "blade_thick"),
        ("Shank diameter (mm)",                "shank_dia"),
        ("Length holder→blade underside (mm)", "length_under_holder_to_underside"),
    ],
    "TAP":      [],
    "DISABLED": [],
}


def _migrate_legacy_tool(code: str, raw: dict) -> dict:
    """
    Convert a v1.0/v1.2 flat-shape tool entry into the v1.3 typed shape.
    Already-new entries pass through unchanged.

    Legacy fields:
        type:           "mill" | "drill" | "step_drill" | "saw" | "tap" |
                        "disabled"
        diameter:       primary diameter (drill / pilot / blade / mill cutter)
        diameter_max:   only on step_drill — counter-bore diameter
        description:    free-text label
    """
    if not isinstance(raw, dict):
        return {"type": "DISABLED", "code": code,
                "description": "", "fields": {}}

    # Already in new shape?
    if isinstance(raw.get("fields"), dict) and \
            isinstance(raw.get("type"), str) and \
            raw["type"] in TOOL_TYPES:
        out = dict(raw)
        out.setdefault("code", code)
        out.setdefault("description", "")
        return out

    legacy_type = (raw.get("type") or "").lower()
    diameter     = raw.get("diameter")
    diameter_max = raw.get("diameter_max")
    description  = raw.get("description", "") or ""

    fields: dict = {}
    if legacy_type == "drill":
        new_type = "DRILL"
        if diameter is not None:
            fields["drill_diameter"] = diameter
    elif legacy_type == "step_drill":
        new_type = "STEP_DRILL"
        # Old v1.2 stored pilot in "diameter" and counter-bore in
        # "diameter_max".  Per the v1.3 spec, "outer_dia" is the bigger
        # bore at the top (counter-bore) and "inner_dia" is the smaller
        # pilot at the tip.
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
            fields["blade_dia"]  = diameter
            fields["blade_thick"] = 3        # safe default
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

    return {
        "type":        new_type,
        "code":        code,
        "description": description,
        "fields":      fields,
    }


def _migrate_machine_tools(tools: dict) -> dict:
    """Migrate every entry in a machine's tool dict to the v1.3 shape."""
    out: dict = {}
    for code, entry in (tools or {}).items():
        if code.startswith("_"):
            continue
        out[code] = _migrate_legacy_tool(code, entry)
    return out


def _read_tools_doc() -> dict:
    """Load tools.json, migrating legacy schema if needed.  Returns the full doc."""
    if not os.path.exists(TOOLS_PATH):
        return {"_comment": [], "machines": {}}
    try:
        with open(TOOLS_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return {"_comment": [], "machines": {}}

    # Already per-machine schema (any flavour)?
    if "machines" in raw and isinstance(raw["machines"], dict):
        # Migrate every machine's tool entries to the v1.3 typed shape.
        for mkey, blob in raw["machines"].items():
            if isinstance(blob, dict):
                blob["tools"] = _migrate_machine_tools(blob.get("tools") or {})
        return raw

    # v1.0 legacy migration: top-level "tools" → DEFAULT machine
    legacy_tools = _migrate_machine_tools(raw.get("tools") or {})
    return {
        "_comment": raw.get("_comment", []),
        "machines": {
            _machine_key(DEFAULT_MANUFACTURER, DEFAULT_MACHINE): {
                "tools": legacy_tools,
            },
        },
    }


def _atomic_write_json(path: str, doc: dict) -> None:
    """
    Write `doc` to `path` atomically — write to a sibling temp file, fsync,
    then os.replace.  Prevents JSON corruption if another reader (the
    background generation thread) opens the file mid-write or if the process
    dies between truncate and flush.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=4)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except (OSError, AttributeError):
            pass
    os.replace(tmp, path)


def _write_tools_doc(doc: dict) -> None:
    _atomic_write_json(TOOLS_PATH, doc)


def load_tools_for_machine(manufacturer: str, machine: str) -> dict:
    """Return the tool dict for one machine ('T3': {...}, …).  Empty if none."""
    doc = _read_tools_doc()
    key = _machine_key(manufacturer, machine)
    return {k: v for k, v in (doc.get("machines", {})
                              .get(key, {})
                              .get("tools", {}) or {}).items()
            if not k.startswith("_")}


def save_tools_for_machine(manufacturer: str, machine: str,
                           tools: dict) -> None:
    doc = _read_tools_doc()
    doc.setdefault("machines", {})
    doc["machines"][_machine_key(manufacturer, machine)] = {"tools": tools}
    if not doc.get("_comment"):
        doc["_comment"] = [
            "Omega CNC Processor — Tool Library.",
            "Tools are stored per machine (manufacturer | model).",
            "Edit via the Tool Library in the application.",
        ]
    _write_tools_doc(doc)


# ── Profiles.json: ensure each profile carries its machine_key ────────────────

def _migrate_profiles_par1(doc: dict) -> dict:
    """
    v1.3.1 migration: PAR1 is now WHeight raw (the STD_*.wrk macros own
    the depth math), so any legacy `par1_formula`, `pocket_par1_formula`,
    `hole_par1_offset` or `slot_par1_offset` keys are stripped from the
    in-memory profiles.  They will disappear from disk on the next save.

    v1.5 migration: WX1 / WY1 are stripped from machining `fields`,
    `constants`, and `varying_fields` because position cannot live in a
    per-profile library — it varies per cut and is always taken from the
    V6 file at conversion time.
    """
    profs = doc.get("profiles") or {}
    dead_keys = (
        "par1_formula", "pocket_par1_formula",
        "hole_par1_offset", "slot_par1_offset",
    )
    for prof in profs.values():
        if not isinstance(prof, dict):
            continue
        for ws in (prof.get("wsides") or {}).values():
            if not isinstance(ws, dict):
                continue
            for k in dead_keys:
                ws.pop(k, None)
        # v1.5: clean any wx1/wy1 left behind in machining libraries
        machs = prof.get("machinings") or {}
        if isinstance(machs, dict):
            for m in machs.values():
                if not isinstance(m, dict):
                    continue
                for sub in ("fields", "constants"):
                    sd = m.get(sub)
                    if isinstance(sd, dict):
                        sd.pop("wx1", None)
                        sd.pop("wy1", None)
                vf = m.get("varying_fields")
                if isinstance(vf, list):
                    m["varying_fields"] = [
                        f for f in vf if f not in ("wx1", "wy1")
                    ]

    # Legacy tagged entries become composite. Untagged entries are accepted
    # only during import/load and adopted by that selected machine.
    profiles_raw = doc.get("profiles") or {}
    migrated: dict = {}
    for k, v in profiles_raw.items():
        if k.startswith("_") or PROFILE_KEY_SEP in k:
            migrated[k] = v
            continue
        if isinstance(v, dict) and v.get("machine"):
            new_key = _compose_pkey(v["machine"], k)
            v.pop("machine", None)
            migrated[new_key] = v
        else:
            migrated[k] = v
    doc["profiles"] = migrated
    return doc


def _read_profiles_doc(manufacturer: str = DEFAULT_MANUFACTURER,
                       machine: str = DEFAULT_MACHINE) -> dict:
    path = machine_profiles_path(manufacturer, machine)
    if not os.path.exists(path):
        return {"_comment": [], "profiles": {}}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return _migrate_profiles_par1(json.load(fh))
    except Exception:
        return {"_comment": [], "profiles": {}}


def _write_profiles_doc(doc: dict,
                        manufacturer: str = DEFAULT_MANUFACTURER,
                        machine: str = DEFAULT_MACHINE) -> None:
    _atomic_write_json(machine_profiles_path(manufacturer, machine), doc)


def normalize_profiles_for_machine(doc: dict, mkey: str) -> dict:
    """Adopt bare/current-machine records and discard foreign machine records."""
    normalized: dict = {}
    for key, value in (doc.get("profiles") or {}).items():
        if key.startswith("_") or not isinstance(value, dict):
            continue
        owner, pid = _decompose_pkey(key)
        if owner is None and value.get("machine"):
            owner = value["machine"]
        if owner not in (None, mkey):
            continue
        clean = copy.deepcopy(value)
        clean.pop("machine", None)
        normalized[_compose_pkey(mkey, pid)] = clean
    return {
        "_comment": doc.get("_comment", [
            "Omega CNC Processor — machine profile database.",
        ]),
        "profiles": normalized,
    }


def export_machine_library_bundle(bundle_path: str, manufacturer: str,
                                  machine: str) -> None:
    """Write only the selected machine's tools, profiles, and DXFs."""
    import zipfile
    mkey = _machine_key(manufacturer, machine)
    profiles_path = machine_profiles_path(manufacturer, machine)
    dxf_root = machine_dxf_library_dir(manufacturer, machine)
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("tools.json", json.dumps({
            "machines": {
                mkey: {
                    "tools": load_tools_for_machine(manufacturer, machine),
                },
            },
        }, indent=4))
        if os.path.exists(profiles_path):
            zf.write(profiles_path, arcname="profiles.json")
        if os.path.isdir(dxf_root):
            for folder, _dirs, files in os.walk(dxf_root):
                for filename in files:
                    if not filename.lower().endswith(".dxf"):
                        continue
                    source = os.path.join(folder, filename)
                    relative = os.path.relpath(source, dxf_root)
                    zf.write(source, arcname=os.path.join(
                        "dxf_library", relative,
                    ))


def import_machine_library_bundle(bundle_path: str, manufacturer: str,
                                  machine: str) -> None:
    """Restore a bundle strictly beneath the selected machine's root."""
    import zipfile, io
    mkey = _machine_key(manufacturer, machine)
    profiles_path = machine_profiles_path(manufacturer, machine)
    library_root = machine_library_dir(manufacturer, machine)
    with zipfile.ZipFile(bundle_path) as zf:
        names = zf.namelist()
        if "tools.json" in names:
            with zf.open("tools.json") as fh:
                data = json.load(io.TextIOWrapper(fh, "utf-8"))
            machines = data.get("machines") or {}
            source = machines.get(mkey)
            if source is None and len(machines) == 1:
                source = next(iter(machines.values()))
            imported_tools = (
                source.get("tools", {}) if isinstance(source, dict)
                else data.get("tools", {})
            )
            save_tools_for_machine(manufacturer, machine, imported_tools)
        if "profiles.json" in names:
            with zf.open("profiles.json") as fh:
                data = json.load(io.TextIOWrapper(fh, "utf-8"))
            _atomic_write_json(
                profiles_path, normalize_profiles_for_machine(data, mkey),
            )
        root_abs = os.path.abspath(library_root)
        for name in names:
            normalized = name.replace("\\", "/")
            if (not normalized.startswith("dxf_library/")
                    or not normalized.lower().endswith(".dxf")):
                continue
            target = os.path.abspath(os.path.join(
                library_root, *normalized.split("/"),
            ))
            if os.path.commonpath([root_abs, target]) != root_abs:
                raise ValueError("Unsafe DXF path in imported bundle.")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(name) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)


# ── Window helpers ────────────────────────────────────────────────────────────

def _maximize_window(win) -> None:
    """Maximise a Tk/Toplevel/CTk window cross-platform."""
    try:
        win.update_idletasks()
        if sys.platform == "win32":
            win.state("zoomed")
        else:
            try:
                win.attributes("-zoomed", True)
            except Exception:
                # Fall back to fitting screen size
                w = win.winfo_screenwidth()
                h = win.winfo_screenheight()
                win.geometry(f"{w}x{h}+0+0")
    except Exception:
        pass


def _focus_window(win) -> None:
    """Bring a Toplevel to the front and maximise it.

    v1.5: maximise FIRST, then raise.  After maximise the window manager
    can re-stack and the popup ends up behind the main window — toggling
    `-topmost` briefly forces it to the very top of the z-order, then we
    release it 250 ms later so it behaves like a normal window again.
    """
    try:
        win.deiconify()
        _maximize_window(win)
        win.lift()
        win.focus_force()
        try:
            win.attributes("-topmost", True)
            win.after(250, lambda: _safe_clear_topmost(win))
        except Exception:
            pass
    except Exception:
        pass


def _safe_clear_topmost(win) -> None:
    try:
        win.attributes("-topmost", False)
    except Exception:
        pass

# Appearance
ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

# Palette
# ── Light theme — matches omegawindows.co.nz (white bg + Omega blue) ────────
# Brand: primary #0399DD, accent #0094D8, white, black text.
CLR_SIDEBAR   = "#EEF2F6"   # soft grey sidebar
CLR_BG        = "#E5EAF0"   # workspace tint — light grey (not bright white)
CLR_CARD      = "#F7F9FB"   # cards: very pale grey, easier on the eyes
CLR_CARD_ALT  = "#E8EDF3"   # subtle "input field" / "tile" fill
CLR_ACCENT    = "#0399DD"   # Omega primary
CLR_ACCENT_HV = "#027CB4"   # 15% darker for hover
CLR_ACCENT_SOFT = "#E6F5FC" # accent @ ~10% alpha → flat tint for nav highlight
CLR_SUCCESS   = "#16a34a"
CLR_WARN      = "#d97706"
CLR_ERROR     = "#dc2626"
CLR_TEXT      = "#0F172A"   # near-black, body text
CLR_TEXT_INV  = "#FFFFFF"   # text on accent surfaces
CLR_MUTED     = "#64748b"   # secondary labels
CLR_BORDER    = "#CBD5E1"   # hairlines on light grey
CLR_FOOTER    = "#EEF2F6"   # soft grey footer strip

FONT_TITLE   = ("Segoe UI Semibold", 18)
FONT_SECTION = ("Segoe UI Semibold", 12)
FONT_LABEL   = ("Segoe UI", 11)
FONT_VALUE   = ("Segoe UI Semibold", 11)
FONT_MONO    = ("Consolas", 10)
FONT_SMALL   = ("Segoe UI", 10)


# ── Helper: simple modal "pick one" dialog ────────────────────────────────────

class _ChoiceDialog(ctk.CTkToplevel):
    """Modal dropdown picker — set self.result to the chosen string or None."""

    def __init__(self, master, title: str, prompt: str, options: list[str]):
        super().__init__(master)
        self.title(title)
        self.geometry("460x220")
        self.configure(fg_color=CLR_BG)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self.result: str | None = None
        self._var = ctk.StringVar(value=options[0] if options else "")

        ctk.CTkLabel(self, text=prompt, font=FONT_LABEL,
                     text_color=CLR_TEXT, wraplength=420, justify="left"
                     ).pack(anchor="w", padx=20, pady=(20, 10))

        ctk.CTkComboBox(
            self, variable=self._var, values=options,
            state="readonly", height=34, font=FONT_LABEL,
            fg_color=CLR_CARD_ALT, border_color=CLR_BORDER,
            button_color=CLR_BORDER, dropdown_fg_color=CLR_CARD,
        ).pack(fill="x", padx=20)

        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(side="bottom", fill="x", padx=20, pady=20)
        ctk.CTkButton(bf, text="Cancel", height=32, width=110,
                      font=FONT_SMALL,
                      fg_color="transparent", border_width=1,
                      border_color=CLR_BORDER, text_color=CLR_TEXT,
                      hover_color=CLR_CARD_ALT,
                      command=self._cancel).pack(side="right", padx=(8, 0))
        ctk.CTkButton(bf, text="OK", height=32, width=110,
                      font=("Segoe UI", 10, "bold"),
                      fg_color=CLR_ACCENT, hover_color=CLR_ACCENT_HV,
                      command=self._ok).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._cancel())
        self.after(50, self.lift)
        self.wait_window()

    def _ok(self):
        self.result = self._var.get()
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


# ── Helper: parse external tool files ─────────────────────────────────────────

def _parse_tool_file(path: str) -> dict:
    """
    Best-effort parser for tool libraries from JSON / XML / TXT / HTML.

    JSON / XML are parsed structurally.  TXT / HTML use a heuristic regex
    that picks up T-numbers and millimetre diameters.

    Returns {"T3": {"type": str, "diameter": float, "description": str}, …}.
    """
    ext = os.path.splitext(path)[1].lower()

    # ── JSON ───────────────────────────────────────────────────────────────
    if ext == ".json":
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        # Native Omega tools-export schema
        if "tools" in raw and isinstance(raw["tools"], dict):
            return {k: v for k, v in raw["tools"].items()
                    if not k.startswith("_")}
        # Per-machine schema — flatten the first machine
        if "machines" in raw and isinstance(raw["machines"], dict):
            for entry in raw["machines"].values():
                t = entry.get("tools") or {}
                if t:
                    return {k: v for k, v in t.items()
                            if not k.startswith("_")}
        # Plain {T3: {...}} dict
        if isinstance(raw, dict) and any(
            isinstance(k, str) and k.startswith("T") for k in raw
        ):
            return {k: v for k, v in raw.items()
                    if isinstance(k, str) and k.startswith("T")}
        return {}

    # ── XML (CAM3D Tools.xml shape) ────────────────────────────────────────
    if ext == ".xml":
        import xml.etree.ElementTree as ET
        tools: dict = {}
        try:
            tree = ET.parse(path)
        except ET.ParseError as exc:
            raise ValueError(f"XML parse error: {exc}")
        root = tree.getroot()
        for elem in root.iter():
            tag = (elem.tag or "").lower()
            # Look for elements that have a T-slot attribute or text
            slot = (elem.attrib.get("Slot")
                    or elem.attrib.get("Number")
                    or elem.attrib.get("Tool")
                    or elem.attrib.get("Position"))
            if not slot:
                continue
            slot = str(slot).strip().upper()
            if not slot.startswith("T"):
                slot = f"T{slot.lstrip('T')}"
            if not slot[1:].isdigit():
                continue
            diam_raw = (elem.attrib.get("Diameter")
                        or elem.attrib.get("Dia"))
            diameter = None
            if diam_raw:
                try:
                    diameter = float(str(diam_raw).replace(",", "."))
                except ValueError:
                    diameter = None
            entry: dict = {
                "type": (elem.attrib.get("Type") or "drill").lower(),
                "description": (elem.attrib.get("Description")
                                or elem.attrib.get("Name") or "").strip(),
            }
            if diameter is not None:
                entry["diameter"] = diameter
            tools[slot] = entry
        return tools

    # ── TXT / HTML — regex heuristic ───────────────────────────────────────
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if ext in (".html", ".htm"):
        # Strip tags so we can scan plain text
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;|&#160;", " ", text)

    tools: dict = {}
    # Match "T3 ... 6mm" / "T12: drill 8.5 mm" etc. — slot then diameter
    # within the next 60 chars.
    pattern = re.compile(
        r"\bT(\d{1,3})\b[^\n\r]{0,80}?(\d+(?:[.,]\d+)?)\s*mm",
        re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        slot = f"T{m.group(1)}"
        if slot in tools:
            continue
        try:
            diameter = float(m.group(2).replace(",", "."))
        except ValueError:
            continue
        tools[slot] = {
            "type": "drill",
            "diameter": diameter,
            "description": "",
        }
    return tools


# ── Per-WComment machining override helpers ───────────────────────────────────
# These are used both by the auto-sync on file load (App._auto_sync_profiles)
# and by the explicit Scan from Job action (ProfileEditorWindow._scan_from_job),
# so they live at module scope to be reachable from either class.

# Editable V6 fields per WType template.  WW2 only applies to slots and
# pockets; WW3 (corner radius) only to pockets.  NOTCH carries PAR7-9
# (inclination reference + Y- / Y+ width reduction) but no V6 geometry.
MACH_FIELDS_BY_WTYPE = {
    # v1.5: WX1 / WY1 removed.  These are per-cut positions that vary with
    # the bar profile and quote, so they cannot live in a per-profile
    # machining library — they always pass through from the V6 file.
    "C":     ["wside", "wdepth", "wheight", "ww1"],
    "L":     ["wside", "wdepth", "wheight", "ww1", "ww2"],
    "R":     ["wside", "wdepth", "wheight", "ww1", "ww2", "ww3"],
    "NOTCH": ["par7", "par8", "par9"],
}


def normalize_machining_entry(entry: dict) -> dict:
    """Migrate any legacy machining entry shape to {wtype, fields, steps, …}.

    v1.5: WX1 / WY1 are stripped from `fields`, `constants`, and
    `varying_fields` so legacy entries written by v1.3.x are cleaned on
    first read.  Position is always pass-through from the V6 file.
    """
    def _strip_xy(d):
        if isinstance(d, dict):
            d.pop("wx1", None)
            d.pop("wy1", None)
        return d

    if isinstance(entry, dict) and "fields" in entry and entry.get("wtype"):
        out = dict(entry)
        out["fields"]    = _strip_xy(dict(out.get("fields") or {}))
        out["constants"] = _strip_xy(dict(out.get("constants") or {}))
        out["varying_fields"] = [
            f for f in (out.get("varying_fields") or [])
            if f not in ("wx1", "wy1")
        ]
        # Backfill v1.3 additions
        if "steps" not in out:
            out["steps"] = 1
        return out
    e = entry or {}
    fields: dict = {}
    legacy_wside = e.get("wside")
    if legacy_wside not in (None, ""):
        fields["wside"] = legacy_wside
    return {
        "wtype":          e.get("wtype") or "C",
        "fields":         fields,
        "steps":          e.get("steps", 1),
        "constants":      _strip_xy(dict(e.get("constants") or {})),
        "varying_fields": [
            f for f in (e.get("varying_fields") or [])
            if f not in ("wx1", "wy1")
        ],
        "seen_count":     e.get("seen_count", 0),
    }


def apply_machining_occurrences(db: dict, job, machining_pids: set,
                                current_mkey: str | None = None):
    """
    Scan *job* for per-(profile, WComment) machining occurrences and update
    each profile's `machinings` library:
      * pre-fill `fields` with constants observed across all instances
      * mark fields that vary across instances as `varying_fields`
        (these become locked / V6-pass-through at conversion time)
      * preserve any user-entered overrides on existing entries

    Returns (added_count, refreshed_count).
    """
    if job is None:
        return 0, 0

    # Collect occurrences per profile, per WComment
    mach_occ: dict = {}
    for bar in job.bars:
        pid = (bar.bidentno or "").strip()
        if not pid or pid not in machining_pids:
            continue
        mach_occ.setdefault(pid, {})
        for cut in bar.cuts:
            for work in cut.works:
                if work.wtype == "NOTCH":
                    continue
                wc = (work.wcomment or "").strip()
                if not wc:
                    continue
                # v1.5: WX1 / WY1 deliberately omitted — they're per-cut
                # positions, not profile attributes.  See MACH_FIELDS_BY_WTYPE.
                mach_occ[pid].setdefault(wc, []).append({
                    "wtype":   work.wtype,
                    "wside":   str(work.wside),
                    "wdepth":  work.wdepth,
                    "wheight": work.wheight,
                    "ww1":     work.ww1,
                    "ww2":     work.ww2,
                    "ww3":     work.ww3,
                })

    added = 0
    refreshed = 0
    for pid, by_wc in mach_occ.items():
        # Only the selected machine's composite profile may be enriched.
        if current_mkey:
            key = _compose_pkey(current_mkey, pid)
        else:
            key = pid
        if key not in db:
            continue
        db_machs = db[key].setdefault("machinings", {})
        for wc, occurrences in by_wc.items():
            wtypes_seen = {o["wtype"] for o in occurrences
                           if o["wtype"] in ("C", "L", "R")}
            if not wtypes_seen:
                continue   # unknown WType — skip (warned at convert)
            wtype    = next(iter(wtypes_seen))
            relevant = MACH_FIELDS_BY_WTYPE.get(wtype, [])

            constants: dict = {}
            varying:   list = []
            for f in relevant:
                vals = {o[f] for o in occurrences}
                if len(vals) == 1:
                    constants[f] = next(iter(vals))
                else:
                    varying.append(f)

            if wc in db_machs:
                entry = normalize_machining_entry(db_machs[wc])
                entry["wtype"]          = wtype
                entry["constants"]      = constants
                entry["varying_fields"] = varying
                entry["seen_count"]     = len(occurrences)
                cur_fields = entry.get("fields") or {}
                new_fields: dict = {}
                for f in relevant:
                    if f in varying:
                        new_fields[f] = None  # locked → V6 pass-through
                    elif f in cur_fields and cur_fields[f] is not None:
                        new_fields[f] = cur_fields[f]  # keep user value
                    else:
                        new_fields[f] = constants.get(f)
                entry["fields"] = new_fields
                # Preserve any user-set step count (defaults to 1)
                entry["steps"]  = entry.get("steps", 1) or 1
                db_machs[wc]    = entry
                refreshed += 1
            else:
                new_fields = {
                    f: (None if f in varying else constants.get(f))
                    for f in relevant
                }
                db_machs[wc] = {
                    "wtype":          wtype,
                    "fields":         new_fields,
                    "steps":          1,
                    "constants":      constants,
                    "varying_fields": varying,
                    "seen_count":     len(occurrences),
                }
                added += 1

    return added, refreshed


# ── Main Application ──────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title(f"{APP_TITLE}  v{APP_VERSION}")
        self.minsize(980, 680)
        self.resizable(True, True)
        self.configure(fg_color=CLR_BG)

        # Centre on screen as a fallback geometry, then maximise.
        self.update_idletasks()
        w, h = 1100, 740
        x = (self.winfo_screenwidth()  - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.after(50, lambda: _maximize_window(self))

        # State
        self._job: Job | None = None
        # Last successfully parsed V6 job retained for Profile Library scans.
        # Standard Conversion resets _job after generation, but that must not
        # discard the profile metadata the user just loaded.
        self._profile_job: Job | None = None
        self._input_path    = ctk.StringVar()
        self._output_folder = ctk.StringVar(
            value=(r"C:\CAM3D\Cms"
                   if sys.platform == "win32"
                   else os.path.expanduser("~"))
        )

        # Manufacturer / Machine selection — drives both tool library lookup
        # and which CNC output format is generated.
        self._manufacturer = ctk.StringVar(value=DEFAULT_MANUFACTURER)
        self._machine      = ctk.StringVar(value=DEFAULT_MACHINE)

        # Machine Library Conversion tab uses its own manufacturer /
        # machine selection so picking a target on tab 2 doesn't disturb
        # the Standard Conversion tab. v1.7 ships the FOM FMC 370
        # stripper only; other machines fall through to a "coming soon"
        # message at strip time.
        self._lib_manufacturer = ctk.StringVar(value="FOM")
        self._lib_machine      = ctk.StringVar(value="FMC 370")

        # Image references (must be kept alive on self to avoid GC)
        self._logo_ctk    = None
        self._banner_ctk  = None
        self._icon_photo  = None

        self._load_images()
        self.bind_all("<Control-o>", lambda e: self._browse_input())
        self._build_ui()

    # ── Image loading ──────────────────────────────────────────────────────────

    def _load_images(self):
        """Load the Omega logo and footer banner using Pillow if available."""
        if not _PIL_AVAILABLE:
            return

        # ── Window icon (32×32 from logo) ─────────────────────────────────────
        if os.path.exists(LOGO_PATH):
            try:
                raw = Image.open(LOGO_PATH).convert("RGBA")
                icon = raw.resize((32, 32), Image.LANCZOS)
                self._icon_photo = ImageTk.PhotoImage(icon)
                self.iconphoto(True, self._icon_photo)
            except Exception:
                pass

        # ── Sidebar logo (178 px wide, proportional height) ───────────────────
        if os.path.exists(LOGO_PATH):
            try:
                img  = Image.open(LOGO_PATH).convert("RGBA")
                lw, lh = img.size
                tw   = 178
                th   = max(1, int(lh * tw / lw))
                self._logo_ctk = ctk.CTkImage(img, size=(tw, th))
            except Exception:
                pass

        # ── Footer banner (height fixed at 62 px, width proportional) ─────────
        if os.path.exists(BANNER_PATH):
            try:
                img  = Image.open(BANNER_PATH).convert("RGBA")
                bw, bh = img.size
                th   = 62
                tw   = max(1, int(bw * th / bh))
                self._banner_ctk = ctk.CTkImage(img, size=(tw, th))
            except Exception:
                pass

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Sidebar ──────────────────────────────────────────────────────────
        sidebar = ctk.CTkFrame(self, width=232, corner_radius=0,
                               fg_color=CLR_SIDEBAR)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self._build_sidebar(sidebar)

        # Hairline divider between sidebar and main area.
        ctk.CTkFrame(self, width=1, fg_color=CLR_BORDER,
                     corner_radius=0).pack(side="left", fill="y")

        # ── Main area ────────────────────────────────────────────────────────
        main = ctk.CTkFrame(self, corner_radius=0, fg_color=CLR_BG)
        main.pack(side="left", fill="both", expand=True)

        # Top bar
        self._build_topbar(main)

        # Footer banner — packed BEFORE body so it claims space at the bottom
        self._build_footer(main)

        # ── Tabs ─────────────────────────────────────────────────────────────
        # Tab 1: existing Standard Conversion flow (load V6 → emit CNC file).
        # Tab 2: placeholder for the upcoming Machine Library Conversion.
        tabs = ctk.CTkTabview(
            main,
            fg_color=CLR_BG,
            segmented_button_fg_color=CLR_CARD_ALT,
            segmented_button_selected_color=CLR_ACCENT,
            segmented_button_selected_hover_color=CLR_ACCENT_HV,
            segmented_button_unselected_color=CLR_CARD_ALT,
            segmented_button_unselected_hover_color=CLR_BORDER,
            text_color=CLR_TEXT,
            text_color_disabled=CLR_MUTED,
        )
        tabs.pack(fill="both", expand=True, padx=28, pady=(14, 0))

        tab_standard = tabs.add("Standard Conversion")
        tab_library  = tabs.add("Machine Library Conversion")
        tabs.set("Standard Conversion")

        # ── Tab 1: standard conversion ───────────────────────────────────────
        body = ctk.CTkFrame(tab_standard, corner_radius=0, fg_color=CLR_BG)
        body.pack(fill="both", expand=True, pady=(8, 0))

        left = ctk.CTkFrame(body, corner_radius=0, fg_color=CLR_BG)
        left.pack(side="left", fill="both", expand=True, padx=(0, 18))

        self._build_input_card(left)
        self._build_summary_card(left)
        self._build_output_card(left)

        right = ctk.CTkFrame(body, width=340, corner_radius=0, fg_color=CLR_BG)
        right.pack(side="right", fill="both")
        right.pack_propagate(False)
        self._build_log_panel(right)

        # ── Tab 2: machine library conversion (placeholder) ──────────────────
        self._build_machine_library_tab(tab_library)

    # ── Machine Library Conversion (placeholder) ──────────────────────────────

    def _build_machine_library_tab(self, parent):
        """Machine Library Conversion — per-machine library stripper.

        Mirrors the layout of the Standard Conversion tab: Input File →
        Machine Selection → Output → Result. v1.7 ships the FOM FMC 370
        stripper; other machines/models fall through to a "coming soon"
        stub so the user can still see the workflow.
        """
        # Two-column body to match Standard tab (left: cards, right: result).
        body = ctk.CTkFrame(parent, corner_radius=0, fg_color=CLR_BG)
        body.pack(fill="both", expand=True, padx=20, pady=20)

        left = ctk.CTkFrame(body, corner_radius=0, fg_color=CLR_BG)
        left.pack(side="left", fill="both", expand=True)

        right = ctk.CTkFrame(body, width=340, corner_radius=0,
                             fg_color=CLR_BG)
        right.pack(side="right", fill="both", padx=(14, 0))
        right.pack_propagate(False)

        # ── Input File card ─────────────────────────────────────────
        in_body = self._section(left, "Input File",
                                subtitle="Softech V6 XML")
        in_row = ctk.CTkFrame(in_body, fg_color="transparent")
        in_row.pack(fill="x")
        ctk.CTkEntry(
            in_row, textvariable=self._input_path,
            placeholder_text="Select a Softech V6 XML file…",
            height=40, font=FONT_LABEL,
            fg_color=CLR_CARD_ALT, border_color=CLR_BORDER,
            text_color=CLR_TEXT,
        ).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(in_row, text="Browse", width=88, height=40,
                      font=FONT_SMALL,
                      fg_color="transparent", border_width=1,
                      border_color=CLR_BORDER, text_color=CLR_TEXT,
                      hover_color=CLR_CARD_ALT,
                      command=self._browse_input
                      ).pack(side="left", padx=(10, 0))
        ctk.CTkButton(in_row, text="Load", width=80, height=40,
                      font=("Segoe UI Semibold", 11),
                      fg_color=CLR_ACCENT, hover_color=CLR_ACCENT_HV,
                      text_color=CLR_TEXT_INV,
                      command=self._load_file
                      ).pack(side="left", padx=(6, 0))

        self._lib_job_label = ctk.CTkLabel(
            in_body,
            text="No V6 file loaded yet.",
            font=FONT_SMALL, text_color=CLR_MUTED,
            anchor="w", justify="left",
        )
        self._lib_job_label.pack(fill="x", pady=(10, 0))

        # ── Machine Selection card ──────────────────────────────────
        sel_body = self._section(left, "Machine Selection",
                                 subtitle="Target CNC for library output")
        label_w = 120

        mfr_row = ctk.CTkFrame(sel_body, fg_color="transparent")
        mfr_row.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(mfr_row, text="Manufacturer", width=label_w,
                     anchor="w", font=FONT_LABEL,
                     text_color=CLR_MUTED).pack(side="left")
        self._lib_mfr_combo = ctk.CTkComboBox(
            mfr_row, variable=self._lib_manufacturer,
            values=list(MACHINE_CATALOG.keys()),
            state="readonly", width=220, height=38,
            font=FONT_LABEL,
            fg_color=CLR_CARD_ALT, border_color=CLR_BORDER,
            button_color=CLR_ACCENT, button_hover_color=CLR_ACCENT_HV,
            dropdown_fg_color=CLR_CARD, dropdown_text_color=CLR_TEXT,
            text_color=CLR_TEXT,
            command=self._on_lib_manufacturer_change,
        )
        self._lib_mfr_combo.pack(side="left")

        mac_row = ctk.CTkFrame(sel_body, fg_color="transparent")
        mac_row.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(mac_row, text="Machine", width=label_w,
                     anchor="w", font=FONT_LABEL,
                     text_color=CLR_MUTED).pack(side="left")
        self._lib_machine_combo = ctk.CTkComboBox(
            mac_row, variable=self._lib_machine,
            values=MACHINE_CATALOG[self._lib_manufacturer.get()],
            state="readonly", width=260, height=38,
            font=FONT_LABEL,
            fg_color=CLR_CARD_ALT, border_color=CLR_BORDER,
            button_color=CLR_ACCENT, button_hover_color=CLR_ACCENT_HV,
            dropdown_fg_color=CLR_CARD, dropdown_text_color=CLR_TEXT,
            text_color=CLR_TEXT,
        )
        self._lib_machine_combo.pack(side="left")

        # v1.7.3: "support coming soon" help text removed (all three
        # currently-supported strippers — FOM, Mecal, Pertici — are
        # discoverable directly from the machine dropdown).

        # ── Output card ──────────────────────────────────────────────
        # v1.7.3: card explicitly sized to match the Input File card
        # height; the Strip & Export button is given a real fixed
        # height (60 px) via an inner frame + propagate(False) trick so
        # customtkinter can no longer collapse it around its label.
        out_body = self._section(left, "Output",
                                 subtitle="Destination folder")
        folder_row = ctk.CTkFrame(out_body, fg_color="transparent")
        folder_row.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(folder_row, text="Output folder", width=label_w,
                     anchor="w", font=FONT_LABEL,
                     text_color=CLR_MUTED).pack(side="left")
        ctk.CTkEntry(folder_row, textvariable=self._output_folder,
                     height=40, font=FONT_LABEL,
                     fg_color=CLR_CARD_ALT, border_color=CLR_BORDER,
                     text_color=CLR_TEXT,
                     ).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(folder_row, text="Browse", width=88, height=40,
                      font=FONT_SMALL,
                      fg_color="transparent", border_width=1,
                      border_color=CLR_BORDER, text_color=CLR_TEXT,
                      hover_color=CLR_CARD_ALT,
                      command=self._browse_output
                      ).pack(side="left", padx=(10, 0))

        # Fixed-height host frame so the button can't be squashed by
        # pack's "shrink to content" behaviour on customtkinter.
        btn_host = ctk.CTkFrame(out_body, height=60, fg_color="transparent")
        btn_host.pack(fill="x", pady=(6, 0))
        btn_host.pack_propagate(False)
        self._btn_strip = ctk.CTkButton(
            btn_host,
            text="▶   Strip & Export Library XML",
            font=("Segoe UI Semibold", 14),
            fg_color=CLR_ACCENT, hover_color=CLR_ACCENT_HV,
            text_color=CLR_TEXT_INV,
            corner_radius=10,
            command=self._strip_library,
        )
        self._btn_strip.pack(fill="both", expand=True)

        # ── Result panel (right column) ─────────────────────────────
        res = ctk.CTkFrame(right, fg_color=CLR_CARD, corner_radius=12,
                           border_width=1, border_color=CLR_BORDER)
        res.pack(fill="both", expand=True)

        ctk.CTkLabel(res, text="Result", font=FONT_SECTION,
                     text_color=CLR_TEXT, anchor="w"
                     ).pack(fill="x", padx=20, pady=(16, 6))

        self._lib_result = ctk.CTkTextbox(
            res, font=("Consolas", 10),
            fg_color=CLR_CARD_ALT, text_color=CLR_TEXT,
            border_width=1, border_color=CLR_BORDER,
            wrap="word", activate_scrollbars=True,
        )
        self._lib_result.pack(fill="both", expand=True,
                              padx=20, pady=(0, 18))
        self._lib_result.insert(
            "1.0",
            "Load a Softech V6 XML, pick the target machine, then press "
            "“Strip & Export Library XML”.\n",
        )
        self._lib_result.configure(state="disabled")

    # ── Library tab actions ────────────────────────────────────────────────────

    def _lib_log(self, text: str) -> None:
        self._lib_result.configure(state="normal")
        self._lib_result.insert("end", text + "\n")
        self._lib_result.see("end")
        self._lib_result.configure(state="disabled")

    def _refresh_library_job_label(self) -> None:
        """Keep the Library tab's source-job summary in sync with the
        currently loaded job (load happens on whichever tab the user
        used)."""
        if not hasattr(self, "_lib_job_label"):
            return
        if self._job is None:
            self._lib_job_label.configure(
                text="No V6 file loaded yet.",
                text_color=CLR_MUTED,
            )
            return
        bars = len(self._job.bars)
        cuts = sum(len(b.cuts) for b in self._job.bars)
        ops  = sum(len(c.works) for b in self._job.bars for c in b.cuts)
        self._lib_job_label.configure(
            text=(f"✓  Job {self._job.job_num} — "
                  f"{bars} bars · {cuts} cuts · {ops} machining ops"),
            text_color=CLR_TEXT,
        )

    def _on_lib_manufacturer_change(self, _value=None):
        """Refresh the model dropdown when the Library tab's
        manufacturer changes."""
        machines = MACHINE_CATALOG.get(self._lib_manufacturer.get(), [])
        self._lib_machine_combo.configure(values=machines)
        if machines and self._lib_machine.get() not in machines:
            self._lib_machine.set(machines[0])

    def _strip_library(self) -> None:
        if self._job is None:
            messagebox.showwarning(
                "No file loaded",
                "Load a Softech V6 XML first.", parent=self,
            )
            return
        out_folder = self._output_folder.get().strip()
        if not out_folder:
            messagebox.showwarning(
                "No output folder",
                "Pick a destination folder.", parent=self,
            )
            return
        if not os.path.isdir(out_folder):
            messagebox.showerror(
                "Folder not found",
                f"Output folder does not exist:\n{out_folder}",
                parent=self,
            )
            return

        mfr = self._lib_manufacturer.get()
        mac = self._lib_machine.get()
        # Routing table for library strippers. v1.7.1 adds Mecal MC305
        # Kosmos / Gianos in addition to FOM FMC 370. Everything else
        # logs a "coming soon" message so the rest of the catalog is
        # still discoverable.
        is_fom_370     = (mfr == "FOM"     and mac == "FMC 370")
        is_mecal_lib   = (mfr == "Mecal"   and mac in ("MC305 Kosmos",
                                                        "MC305 Gianos"))
        is_pertici_104 = (mfr == "Pertici" and mac == "P104")

        self._btn_strip.configure(state="disabled", text="Stripping…")
        try:
            if not (is_fom_370 or is_mecal_lib or is_pertici_104):
                self._lib_log("─" * 60)
                self._lib_log(
                    f"⚠ {mfr} {mac} library stripper not yet implemented."
                )
                self._lib_log(
                    "  Currently available: FOM FMC 370, "
                    "Mecal MC305 Kosmos/Gianos, Pertici P104."
                )
                return

            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")

            if is_fom_370:
                xml, warnings, stats = transform_fom_library(self._job)
                fname    = f"FOM_{ts}.xml"
                encoding = "iso-8859-1"
                stats_line = (
                    f"  Bars: {stats['bars_out']}/{stats['bars_in']} kept   "
                    f"Cuts: {stats['cuts_out']}/{stats['cuts_in']} kept   "
                    f"Machinings emitted: {stats['machinings_out']}"
                )
            elif is_mecal_lib:
                xml, warnings, stats = transform_mecal_library(self._job)
                fname    = f"MECAL_{ts}.xml"
                encoding = "iso-8859-1"
                stats_line = (
                    f"  Bars: {stats['bars_out']}/{stats['bars_in']} kept   "
                    f"Pieces: {stats['pieces_out']}/{stats['pieces_in']} kept   "
                    f"Works emitted: {stats['works_out']}"
                )
            else:  # is_pertici_104
                xml, warnings, stats = transform_pertici_library(self._job)
                fname    = f"PERTICI_{ts}.ncw"
                encoding = "utf-8"
                stats_line = (
                    f"  Bars: {stats['bars_out']}/{stats['bars_in']} kept   "
                    f"Cuts: {stats['cuts_out']}/{stats['cuts_in']} kept   "
                    f"Works emitted: {stats['works_out']}"
                )

            out_path = os.path.join(out_folder, fname)
            with open(out_path, "w", encoding=encoding,
                      errors="replace") as f:
                f.write(xml)

            self._lib_log("─" * 60)
            self._lib_log(f"✓ {mfr} {mac} library XML written:")
            self._lib_log(f"  {out_path}")
            self._lib_log(stats_line)
            for w in warnings:
                self._lib_log(f"  ⚠ {w}")
        except Exception:
            self._lib_log("─" * 60)
            self._lib_log("✗ ERROR while stripping:")
            self._lib_log(traceback.format_exc())
            messagebox.showerror(
                "Strip failed",
                "See the Result panel for the traceback.",
                parent=self,
            )
        finally:
            self._btn_strip.configure(
                state="normal",
                text="▶   Strip & Export Library XML",
            )

    # ── Footer banner ──────────────────────────────────────────────────────────

    def _build_footer(self, parent):
        # Thin hairline above the footer for visual separation.
        ctk.CTkFrame(parent, height=1, fg_color=CLR_BORDER,
                     corner_radius=0).pack(side="bottom", fill="x")

        footer = ctk.CTkFrame(parent, corner_radius=0, fg_color=CLR_FOOTER,
                              height=72)
        footer.pack(side="bottom", fill="x")
        footer.pack_propagate(False)

        if self._banner_ctk is not None:
            ctk.CTkLabel(
                footer,
                image=self._banner_ctk,
                text="",
                fg_color="transparent",
            ).pack(side="left", padx=20)

        ctk.CTkLabel(
            footer,
            text="Omega Windows & Doors  |  by McKechnie®",
            font=FONT_SMALL,
            text_color=CLR_MUTED,
            fg_color="transparent",
        ).pack(side="right", padx=20)

    # ── Sidebar ───────────────────────────────────────────────────────────────

    def _build_sidebar(self, parent):
        # Logo block
        logo_frame = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        logo_frame.pack(fill="x", padx=22, pady=(26, 18))

        if self._logo_ctk is not None:
            ctk.CTkLabel(
                logo_frame,
                image=self._logo_ctk,
                text="",
                fg_color="transparent",
            ).pack(anchor="w")
        else:
            # Fallback wordmark — keeps brand recognisable when image missing.
            ctk.CTkLabel(logo_frame, text="OMEGA",
                         font=("Segoe UI Black", 22),
                         text_color=CLR_TEXT).pack(anchor="w")
            ctk.CTkLabel(logo_frame, text="WINDOWS + DOORS",
                         font=("Segoe UI", 8, "bold"),
                         text_color=CLR_ACCENT).pack(anchor="w")

        ctk.CTkLabel(logo_frame,
                     text=f"CNC-zee  ·  v{APP_VERSION}",
                     font=FONT_SMALL, text_color=CLR_MUTED
                     ).pack(anchor="w", pady=(6, 0))

        # Divider
        ctk.CTkFrame(parent, height=1, fg_color=CLR_BORDER,
                     corner_radius=0).pack(fill="x", padx=16, pady=(8, 16))

        # Nav items — store rows so we can later toggle "active" state cleanly.
        self._nav_rows: list[tuple[ctk.CTkFrame, ctk.CTkLabel]] = []
        for icon, label, active, cmd in [
            ("📂", "Load File",       False, self._browse_input),
            ("🔧", "Generate",        True,  self._generate),
            ("📋", "Profile Library", False, self._open_profile_editor),
            ("🔩", "Tool Library",    False, self._open_tool_library),
            ("⬇",  "Import Database", False, self._import_database),
            ("⬆",  "Export Database", False, self._export_database),
            ("ℹ",  "About",           False, self._show_about),
            ("❓", "Help",            False, self._show_help),
        ]:
            row = ctk.CTkFrame(
                parent,
                fg_color=CLR_ACCENT_SOFT if active else "transparent",
                corner_radius=8,
            )
            row.pack(fill="x", padx=12, pady=2)
            lbl = ctk.CTkLabel(
                row, text=f"  {icon}   {label}",
                font=FONT_LABEL,
                text_color=CLR_ACCENT if active else CLR_TEXT,
                anchor="w", cursor="hand2",
            )
            lbl.pack(fill="x", padx=10, pady=9)
            lbl.bind("<Button-1>", lambda e, c=cmd: c())
            row.bind("<Button-1>", lambda e, c=cmd: c())

            # Subtle hover: light accent tint on entry, restore on leave.
            base_fg = CLR_ACCENT_SOFT if active else "transparent"
            base_tx = CLR_ACCENT if active else CLR_TEXT
            def _enter(e, r=row, l=lbl, a=active):
                if not a:
                    r.configure(fg_color=CLR_CARD_ALT)
            def _leave(e, r=row, l=lbl, bg=base_fg, tx=base_tx):
                r.configure(fg_color=bg)
                l.configure(text_color=tx)
            for w in (row, lbl):
                w.bind("<Enter>", _enter)
                w.bind("<Leave>", _leave)

            self._nav_rows.append((row, lbl))

        # Spacer
        ctk.CTkFrame(parent, fg_color="transparent").pack(fill="both",
                                                           expand=True)

        # Bottom: keyboard shortcut hint
        ctk.CTkLabel(parent, text="Ctrl + O    Open file",
                     font=("Segoe UI", 9), text_color=CLR_MUTED
                     ).pack(pady=(0, 22))

    # ── Top bar ───────────────────────────────────────────────────────────────

    def _build_topbar(self, parent):
        bar = ctk.CTkFrame(parent, height=72, corner_radius=0,
                           fg_color=CLR_CARD)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        # Hairline under topbar
        ctk.CTkFrame(parent, height=1, fg_color=CLR_BORDER,
                     corner_radius=0).pack(fill="x")

        # ── Centred title block: title + tagline stacked ─────────────────────
        title_block = ctk.CTkFrame(bar, fg_color="transparent")
        title_block.place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkLabel(title_block, text=APP_TITLE,
                     font=FONT_TITLE,
                     text_color=CLR_TEXT).pack(anchor="center")
        ctk.CTkLabel(title_block, text=APP_TAGLINE,
                     font=("Segoe UI", 10, "bold"),
                     text_color=CLR_ACCENT).pack(anchor="center", pady=(2, 0))

    # ── Cards ─────────────────────────────────────────────────────────────────

    def _section(self, parent, title: str,
                 subtitle: str | None = None) -> ctk.CTkFrame:
        """Titled section card — flat white with hairline border, no
        header divider (clean modern style)."""
        card = ctk.CTkFrame(parent, fg_color=CLR_CARD, corner_radius=12,
                            border_width=1, border_color=CLR_BORDER)
        card.pack(fill="x", pady=(0, 14))

        header = ctk.CTkFrame(card, fg_color="transparent", corner_radius=0)
        header.pack(fill="x", padx=20, pady=(16, 10))

        ctk.CTkLabel(header, text=title, font=FONT_SECTION,
                     text_color=CLR_TEXT).pack(side="left")
        if subtitle:
            ctk.CTkLabel(header, text=subtitle, font=FONT_SMALL,
                         text_color=CLR_MUTED).pack(side="right")

        body = ctk.CTkFrame(card, fg_color="transparent", corner_radius=0)
        body.pack(fill="x", padx=20, pady=(0, 18))
        return body

    # ── Input card ────────────────────────────────────────────────────────────

    def _build_input_card(self, parent):
        body = self._section(parent, "Input File",
                             subtitle="Softech V6 XML")

        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x")

        self._entry_input = ctk.CTkEntry(
            row, textvariable=self._input_path,
            placeholder_text="Select a Softech V6 XML file…",
            height=40, font=FONT_LABEL,
            fg_color=CLR_CARD_ALT, border_color=CLR_BORDER,
            text_color=CLR_TEXT,
        )
        self._entry_input.pack(side="left", fill="x", expand=True)
        self._entry_input.bind("<Return>", lambda e: self._load_file())

        ctk.CTkButton(row, text="Browse", width=88, height=40,
                      font=FONT_SMALL,
                      fg_color="transparent", border_width=1,
                      border_color=CLR_BORDER, text_color=CLR_TEXT,
                      hover_color=CLR_CARD_ALT,
                      command=self._browse_input
                      ).pack(side="left", padx=(10, 0))

        ctk.CTkButton(row, text="Load", width=80, height=40,
                      font=("Segoe UI Semibold", 11),
                      fg_color=CLR_ACCENT, hover_color=CLR_ACCENT_HV,
                      text_color=CLR_TEXT_INV,
                      command=self._load_file).pack(side="left", padx=(6, 0))

    # ── Summary card ──────────────────────────────────────────────────────────

    def _build_summary_card(self, parent):
        body = self._section(parent, "Job Summary",
                             subtitle="Parsed metrics")

        # Single compact row of 6 tiles — much shorter than the previous
        # 2-row grid, leaves more room for the Output card / Generate button.
        grid = ctk.CTkFrame(body, fg_color="transparent")
        grid.pack(fill="x")

        self._stat_job    = self._stat_tile(grid, "Job number", "—", 0)
        self._stat_date   = self._stat_tile(grid, "Date",       "—", 1)
        self._stat_bars   = self._stat_tile(grid, "Bars",       "—", 2)
        self._stat_pieces = self._stat_tile(grid, "Pieces",     "—", 3)
        self._stat_cpb    = self._stat_tile(grid, "Cuts / bar", "—", 4)
        self._stat_ops    = self._stat_tile(grid, "Mach. ops",  "—", 5)

        grid.columnconfigure((0, 1, 2, 3, 4, 5), weight=1)

    def _stat_tile(self, parent, label: str, value: str,
                   col: int) -> ctk.CTkLabel:
        tile = ctk.CTkFrame(parent, fg_color=CLR_CARD_ALT, corner_radius=8,
                            border_width=0)
        tile.grid(row=0, column=col,
                  padx=(0 if col == 0 else 6, 0),
                  pady=0, sticky="nsew")

        ctk.CTkLabel(tile, text=label.upper(),
                     font=("Segoe UI", 8, "bold"),
                     text_color=CLR_MUTED
                     ).pack(pady=(8, 0), padx=10, anchor="w")
        val_lbl = ctk.CTkLabel(tile, text=value,
                               font=("Segoe UI Semibold", 15),
                               text_color=CLR_TEXT)
        val_lbl.pack(pady=(0, 8), padx=10, anchor="w")
        return val_lbl

    # ── Output card ───────────────────────────────────────────────────────────

    def _build_output_card(self, parent):
        body = self._section(parent, "Output Settings",
                             subtitle="Target machine & destination")

        label_w = 120

        # Manufacturer row
        mfr_row = ctk.CTkFrame(body, fg_color="transparent")
        mfr_row.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(mfr_row, text="Manufacturer", width=label_w,
                     anchor="w", font=FONT_LABEL,
                     text_color=CLR_MUTED).pack(side="left")
        self._mfr_combo = ctk.CTkComboBox(
            mfr_row, variable=self._manufacturer,
            values=list(MACHINE_CATALOG.keys()),
            state="readonly", width=220, height=38,
            font=FONT_LABEL,
            fg_color=CLR_CARD_ALT, border_color=CLR_BORDER,
            button_color=CLR_ACCENT, button_hover_color=CLR_ACCENT_HV,
            dropdown_fg_color=CLR_CARD, dropdown_text_color=CLR_TEXT,
            text_color=CLR_TEXT,
            command=self._on_manufacturer_change,
        )
        self._mfr_combo.pack(side="left")

        # Machine row
        mac_row = ctk.CTkFrame(body, fg_color="transparent")
        mac_row.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(mac_row, text="Machine", width=label_w,
                     anchor="w", font=FONT_LABEL,
                     text_color=CLR_MUTED).pack(side="left")
        self._machine_combo = ctk.CTkComboBox(
            mac_row, variable=self._machine,
            values=MACHINE_CATALOG[self._manufacturer.get()],
            state="readonly", width=260, height=38,
            font=FONT_LABEL,
            fg_color=CLR_CARD_ALT, border_color=CLR_BORDER,
            button_color=CLR_ACCENT, button_hover_color=CLR_ACCENT_HV,
            dropdown_fg_color=CLR_CARD, dropdown_text_color=CLR_TEXT,
            text_color=CLR_TEXT,
        )
        self._machine_combo.pack(side="left")

        folder_row = ctk.CTkFrame(body, fg_color="transparent")
        folder_row.pack(fill="x", pady=(0, 18))
        ctk.CTkLabel(folder_row, text="Output folder", width=label_w,
                     anchor="w", font=FONT_LABEL,
                     text_color=CLR_MUTED).pack(side="left")
        ctk.CTkEntry(folder_row, textvariable=self._output_folder,
                     height=38, font=FONT_LABEL,
                     fg_color=CLR_CARD_ALT, border_color=CLR_BORDER,
                     text_color=CLR_TEXT,
                     ).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(folder_row, text="Browse", width=88, height=38,
                      font=FONT_SMALL,
                      fg_color="transparent", border_width=1,
                      border_color=CLR_BORDER, text_color=CLR_TEXT,
                      hover_color=CLR_CARD_ALT,
                      command=self._browse_output
                      ).pack(side="left", padx=(10, 0))

        self._btn_generate = ctk.CTkButton(
            body,
            text="▶   Generate Output",
            height=48,
            font=("Segoe UI Semibold", 13),
            fg_color=CLR_ACCENT,
            hover_color=CLR_ACCENT_HV,
            text_color=CLR_TEXT_INV,
            corner_radius=10,
            command=self._generate,
        )
        self._btn_generate.pack(fill="x")

    # ── Log panel ─────────────────────────────────────────────────────────────

    def _build_log_panel(self, parent):
        card = ctk.CTkFrame(parent, fg_color=CLR_CARD, corner_radius=12,
                            border_width=1, border_color=CLR_BORDER)
        card.pack(fill="both", expand=True)

        hdr = ctk.CTkFrame(card, fg_color="transparent", corner_radius=0)
        hdr.pack(fill="x", padx=20, pady=(16, 10))
        ctk.CTkLabel(hdr, text="Status / Log", font=FONT_SECTION,
                     text_color=CLR_TEXT).pack(side="left")
        ctk.CTkButton(hdr, text="Clear", width=64, height=28,
                      font=FONT_SMALL,
                      fg_color="transparent", border_width=1,
                      border_color=CLR_BORDER, text_color=CLR_MUTED,
                      hover_color=CLR_CARD_ALT,
                      command=self._clear_log).pack(side="right")

        # Light-themed log surface: very pale tint, dark text. Code colours
        # are darker / higher-contrast variants suitable for white backgrounds.
        self._log = ctk.CTkTextbox(
            card,
            font=FONT_MONO,
            fg_color=CLR_CARD_ALT,
            text_color=CLR_TEXT,
            border_width=0,
            corner_radius=0,
            wrap="word",
            activate_scrollbars=True,
        )
        self._log.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self._log._textbox.tag_configure("ok",    foreground=CLR_SUCCESS)
        self._log._textbox.tag_configure("warn",  foreground=CLR_WARN)
        self._log._textbox.tag_configure("error", foreground=CLR_ERROR)
        self._log._textbox.tag_configure("info",  foreground=CLR_ACCENT)
        self._log._textbox.tag_configure("dim",   foreground=CLR_MUTED)

        self._log.configure(state="disabled")

    # ── File operations ───────────────────────────────────────────────────────

    def _browse_input(self):
        path = filedialog.askopenfilename(
            title="Select Softech V6 XML file",
            filetypes=[("XML files", "*.xml"), ("All files", "*.*")],
        )
        if path:
            self._input_path.set(path)
            self._load_file()

    def _browse_output(self):
        folder = filedialog.askdirectory(title="Select output folder")
        if folder:
            self._output_folder.set(folder)

    # ── Load / parse ──────────────────────────────────────────────────────────

    def _load_file(self):
        path = self._input_path.get().strip()
        if not path:
            self._log_line("No file selected.", "warn")
            return
        if not os.path.isfile(path):
            self._log_line(f"File not found: {path}", "error")
            return

        self._log_line(f"Loading: {os.path.basename(path)}", "info")
        try:
            job = parse_v6(path)
        except ValueError as exc:
            self._log_line(f"ERROR: {exc}", "error")
            messagebox.showerror("Parse Error", str(exc), parent=self)
            return
        except Exception:
            self._log_line(f"Unexpected error while parsing.", "error")
            messagebox.showerror("Error", traceback.format_exc(), parent=self)
            return

        self._job = job
        self._profile_job = job
        self._refresh_library_job_label()
        if hasattr(self, "_prof_win") and self._prof_win.winfo_exists():
            self._prof_win.set_job(job)
        cpb = (f"{job.piece_count / job.bar_count:.1f}"
               if job.bar_count else "—")

        self._stat_job.configure(text=job.job_num or "—")
        self._stat_date.configure(text=job.date or "—")
        self._stat_bars.configure(text=str(job.bar_count))
        self._stat_pieces.configure(text=str(job.piece_count))
        self._stat_cpb.configure(text=cpb)
        self._stat_ops.configure(text=str(job.operation_count))

        self._log_line(
            f"✓ Parsed OK — {job.bar_count} bars, "
            f"{job.piece_count} pieces, "
            f"{job.operation_count} operations.", "ok"
        )

        # XML profile metadata, SIDEs, and machinings remain draft-only.

    # ── Generate ──────────────────────────────────────────────────────────────

    def _prompt_tool_overrides(self, issues) -> dict:
        """Show ONE batched dialog listing every machining whose auto-selected
        tool is incompatible / missing, letting the operator pick a suitable
        replacement per WComment.  Returns {comment_key: tool_code}; an empty
        dict means "keep all auto-selected".  Degrades to {} on any error."""
        overrides: dict = {}
        try:
            dlg = ctk.CTkToplevel(self)
            dlg.title("Incompatible tools detected")
            dlg.transient(self)
            dlg.geometry("780x540")
            dlg.grab_set()

            ctk.CTkLabel(
                dlg,
                text=(f"{len(issues)} machining type(s) auto-selected a tool "
                      "that does not fit.\nChoose a replacement tool or keep "
                      "the auto-selection."),
                justify="left", font=("Segoe UI Semibold", 14),
            ).pack(anchor="w", padx=16, pady=(14, 6))

            body = ctk.CTkScrollableFrame(dlg, width=740, height=390)
            body.pack(fill="both", expand=True, padx=12, pady=6)

            AUTO = "↳ Keep auto-selected"
            row_state = []   # (issue, var, {display: code})
            for it in issues:
                card = ctk.CTkFrame(body)
                card.pack(fill="x", padx=4, pady=6)
                ctk.CTkLabel(
                    card, text=it.headline(),
                    font=("Segoe UI Semibold", 13), justify="left",
                ).pack(anchor="w", padx=10, pady=(8, 0))
                ctk.CTkLabel(
                    card, text=it.reason, justify="left",
                    text_color="#B45309", wraplength=700,
                ).pack(anchor="w", padx=10, pady=(0, 4))

                disp_to_code = {AUTO: None}
                options = [AUTO]
                for c in it.candidates:
                    disp_to_code[c["label"]] = c["code"]
                    options.append(c["label"])
                var = tk.StringVar(value=AUTO)
                ctk.CTkOptionMenu(
                    card, values=options, variable=var, width=700,
                    dynamic_resizing=False,
                ).pack(anchor="w", padx=10, pady=(0, 10))
                row_state.append((it, var, disp_to_code))

            result = {"ok": False}

            def _apply():
                result["ok"] = True
                dlg.destroy()

            def _cancel():
                dlg.destroy()

            btns = ctk.CTkFrame(dlg, fg_color="transparent")
            btns.pack(fill="x", padx=16, pady=(4, 14))
            ctk.CTkButton(btns, text="Use selected tools",
                          command=_apply).pack(side="right", padx=(8, 0))
            ctk.CTkButton(btns, text="Keep all auto", fg_color="gray",
                          command=_cancel).pack(side="right")

            dlg.protocol("WM_DELETE_WINDOW", _cancel)
            self.wait_window(dlg)

            if result["ok"]:
                for it, var, disp_to_code in row_state:
                    code = disp_to_code.get(var.get())
                    if code:
                        overrides[it.comment_key] = str(code)
        except Exception:
            self._log_line(
                "  WARNING: tool-override dialog failed; using auto-selected "
                "tools.", "warn",
            )
            self._log_line(traceback.format_exc(), "dim")
            return {}
        return overrides

    def _generate(self):
        if self._job is None:
            messagebox.showwarning("No file loaded",
                                   "Please load a V6 XML file first.",
                                   parent=self)
            return
        out_folder = self._output_folder.get().strip()
        if not out_folder:
            messagebox.showwarning("No output folder",
                                   "Please select an output folder.",
                                   parent=self)
            return

        manufacturer = self._manufacturer.get()
        machine      = self._machine.get()
        mkey         = _machine_key(manufacturer, machine)
        profiles_path = machine_profiles_path(manufacturer, machine)

        # Incompatible-tool pre-flight: for the implemented CNC pipelines,
        # scan the job for machinings whose auto-selected tool doesn't fit and
        # let the operator pick replacements in ONE batched prompt before the
        # output is generated.  Anything that fails here degrades silently to
        # the auto-selected tools (the prior behaviour).
        tool_overrides: dict = {}
        impl_models = IMPLEMENTED_MODELS.get(manufacturer, set())
        is_impl = (
            manufacturer in IMPLEMENTED_MANUFACTURERS
            and (not impl_models or machine in impl_models)
            and manufacturer in ("Mecal", "FOM", "Pertici")
        )
        if is_impl:
            try:
                issues = detect_tool_issues(
                    self._job, CONFIG_PATH, profiles_path, TOOLS_PATH,
                    machine_key=mkey,
                )
                if issues:
                    n_ops = sum(i.count for i in issues)
                    self._log_line(
                        f"  {len(issues)} machining type(s) ({n_ops} op(s)) "
                        "use an incompatible auto-selected tool — review the "
                        "prompt.", "warn",
                    )
                    tool_overrides = self._prompt_tool_overrides(issues)
                    if tool_overrides:
                        self._log_line(
                            f"  Applying {len(tool_overrides)} tool "
                            "override(s).", "info",
                        )
            except Exception:
                self._log_line(
                    "  WARNING: tool-compatibility check failed; continuing "
                    "with auto-selected tools.", "warn",
                )
                self._log_line(traceback.format_exc(), "dim")

        self._btn_generate.configure(state="disabled",
                                     text="Generating…")
        self._log_line(f"Generating output for {mkey}…", "info")

        threading.Thread(
            target=self._generate_task,
            args=(self._job, manufacturer, machine, mkey, out_folder,
                  tool_overrides),
            daemon=True,
        ).start()

    def _generate_task(self, job: Job, manufacturer: str, machine: str,
                       machine_key: str, out_folder: str,
                       tool_overrides: dict | None = None):
        try:
            implemented_models = IMPLEMENTED_MODELS.get(manufacturer, set())
            is_implemented = (
                manufacturer in IMPLEMENTED_MANUFACTURERS
                and (not implemented_models or machine in implemented_models)
            )

            if is_implemented and manufacturer == "Mecal":
                xml, warnings = transform_mecal(
                    job, CONFIG_PATH,
                    machine_profiles_path(manufacturer, machine), TOOLS_PATH,
                    machine_key=machine_key,
                    tool_overrides=tool_overrides,
                )
                for w in warnings:
                    self.after(0, self._log_line, w, "warn")
                out_path = write_output(xml, out_folder, job.job_num)
                self.after(0, self._on_success,
                           f"✓ Written: {out_path}", out_path)

            elif is_implemented and manufacturer == "FOM":
                xml, warnings = transform_fom(
                    job, CONFIG_PATH,
                    machine_profiles_path(manufacturer, machine), TOOLS_PATH,
                    machine_key=machine_key,
                    tool_overrides=tool_overrides,
                )
                for w in warnings:
                    self.after(0, self._log_line, w, "warn")
                out_path = write_output(xml, out_folder, job.job_num)
                self.after(0, self._on_success,
                           f"✓ Written: {out_path}", out_path)

            elif is_implemented and manufacturer == "Pertici":
                # Pertici emits a flat key=value text file (.ncw), not XML.
                # Bypass writer.write_output (which assumes .xml) and
                # write the .ncw directly with UTF-8 encoding.
                ncw, warnings = transform_pertici(
                    job, CONFIG_PATH,
                    machine_profiles_path(manufacturer, machine), TOOLS_PATH,
                    machine_key=machine_key,
                    tool_overrides=tool_overrides,
                )
                for w in warnings:
                    self.after(0, self._log_line, w, "warn")
                if not job.job_num:
                    raise ValueError(
                        "Job number is empty — cannot determine output "
                        "filename."
                    )
                out_path = os.path.join(out_folder, f"{job.job_num}.ncw")
                # Pertici machines expect LF line endings — pin newline
                # explicitly so Windows builds don't silently emit CRLF.
                with open(out_path, "w", encoding="utf-8",
                          errors="replace", newline="\n") as f:
                    f.write(ncw)
                self.after(0, self._on_success,
                           f"✓ Written: {out_path}", out_path)

            else:
                out_path = generate_stub(manufacturer, job.job_num, job.date,
                                         out_folder)
                msg = (
                    f"{manufacturer} ({machine}) output format is not yet "
                    f"configured.\n\n"
                    f"A placeholder file has been written to:\n{out_path}\n\n"
                    "To enable this machine, provide a sample output XML file "
                    "from your machine software."
                )
                self.after(0, self._on_stub, msg, out_path)

        except Exception:
            self.after(0, self._on_error, traceback.format_exc())

    def _reset_for_next_job(self):
        """Reset UI to ready state so a new job can be loaded immediately."""
        self._job = None
        self._refresh_library_job_label()
        self._input_path.set("")
        self._stat_job.configure(text="—")
        self._stat_date.configure(text="—")
        self._stat_bars.configure(text="—")
        self._stat_pieces.configure(text="—")
        self._stat_cpb.configure(text="—")
        self._stat_ops.configure(text="—")
        self._btn_generate.configure(state="normal",
                                     text="▶   Generate Output XML")
        self._log_line("─" * 38, "dim")
        self._log_line("Ready — load another file to continue.", "info")

    def _on_success(self, log_msg: str, path: str):
        self._log_line(log_msg, "ok")
        messagebox.showinfo("Success",
                            f"Output written successfully:\n\n{path}",
                            parent=self)
        self._reset_for_next_job()

    def _on_stub(self, msg: str, path: str):
        self._log_line(f"INFO: {path}", "warn")
        messagebox.showinfo("Not Yet Configured", msg, parent=self)
        self._reset_for_next_job()

    def _on_error(self, tb: str):
        self._log_line("ERROR — see details below:", "error")
        self._log_line(tb, "error")
        self._btn_generate.configure(state="normal",
                                     text="▶   Generate Output XML")
        messagebox.showerror("Generation Failed",
                             "An error occurred during generation.\n\n"
                             "Check the status log for details.",
                             parent=self)

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _log_line(self, text: str, tag: str = ""):
        self._log.configure(state="normal")
        txt = self._log._textbox
        if txt.index("end-1c") != "1.0":
            txt.insert("end", "\n")
        txt.insert("end", text, tag)
        txt.see("end")
        self._log.configure(state="disabled")

    def _clear_log(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")

    # ── Profile Library ───────────────────────────────────────────────────────

    def _open_profile_editor(self):
        """Open the Profile Library editor window (full-screen)."""
        if hasattr(self, "_prof_win") and self._prof_win.winfo_exists():
            self._prof_win.set_job(self._profile_job)
            _focus_window(self._prof_win)
            return
        self._prof_win = ProfileEditorWindow(
            self, job=self._profile_job,
            initial_manufacturer=self._manufacturer.get(),
            initial_machine=self._machine.get(),
        )

    def _open_tool_library(self):
        """Open the Tool Library editor window (full-screen)."""
        if hasattr(self, "_tool_win") and self._tool_win.winfo_exists():
            _focus_window(self._tool_win)
            return
        self._tool_win = ToolLibraryWindow(
            self,
            initial_manufacturer=self._manufacturer.get(),
            initial_machine=self._machine.get(),
        )

    # ── Manufacturer / machine selection ──────────────────────────────────────

    def _on_manufacturer_change(self, _value=None):
        """When manufacturer changes, refresh the machine dropdown values."""
        machines = MACHINE_CATALOG.get(self._manufacturer.get(), [])
        self._machine_combo.configure(values=machines)
        if machines and self._machine.get() not in machines:
            self._machine.set(machines[0])

    # ── Import / export databases ─────────────────────────────────────────────

    def _import_database(self):
        """Import tools plus profiles for the currently selected machine."""
        path = filedialog.askopenfilename(
            title="Import database",
            filetypes=[
                ("Database bundle (.zip)", "*.zip"),
                ("Tools or Profiles JSON", "*.json"),
                ("All files", "*.*"),
            ],
            parent=self,
        )
        if not path:
            return

        try:
            manufacturer = self._manufacturer.get()
            machine = self._machine.get()
            mkey = _machine_key(manufacturer, machine)
            profiles_path = machine_profiles_path(
                manufacturer, machine,
            )
            library_root = machine_library_dir(manufacturer, machine)
            if path.lower().endswith(".zip"):
                import_machine_library_bundle(
                    path, manufacturer, machine,
                )
                self._log_line(f"Imported database bundle: {path}", "ok")
            else:
                # Single-file JSON — sniff which one it is
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if "machines" in raw or "tools" in raw:
                    machines = raw.get("machines") or {}
                    source = machines.get(mkey)
                    if source is None and len(machines) == 1:
                        source = next(iter(machines.values()))
                    imported_tools = (
                        source.get("tools", {}) if isinstance(source, dict)
                        else raw.get("tools", {})
                    )
                    save_tools_for_machine(
                        manufacturer, machine, imported_tools,
                    )
                    self._log_line(f"Imported tools database: {path}", "ok")
                elif "profiles" in raw:
                    raw = normalize_profiles_for_machine(raw, mkey)
                    _atomic_write_json(profiles_path, raw)
                    self._log_line(f"Imported profile database: {path}", "ok")
                else:
                    raise ValueError(
                        "File doesn't look like a tools or profiles database."
                    )
            messagebox.showinfo("Import complete",
                                "Database imported successfully.",
                                parent=self)
        except Exception as exc:
            self._log_line(f"Import failed: {exc}", "error")
            messagebox.showerror("Import failed", str(exc), parent=self)

    def _export_database(self):
        """Export tools plus profiles for the currently selected machine."""
        path = filedialog.asksaveasfilename(
            title="Export database bundle",
            defaultextension=".zip",
            filetypes=[("Database bundle (.zip)", "*.zip")],
            initialfile="omega_cnc_database.zip",
            parent=self,
        )
        if not path:
            return
        try:
            manufacturer = self._manufacturer.get()
            machine = self._machine.get()
            export_machine_library_bundle(path, manufacturer, machine)
            self._log_line(f"Exported database to: {path}", "ok")
            messagebox.showinfo("Export complete",
                                f"Database exported to:\n{path}",
                                parent=self)
        except Exception as exc:
            self._log_line(f"Export failed: {exc}", "error")
            messagebox.showerror("Export failed", str(exc), parent=self)

    # ── About / Help ──────────────────────────────────────────────────────────

    def _show_about(self):
        impl = ", ".join(sorted(IMPLEMENTED_MANUFACTURERS))
        stubs = ", ".join(m for m in MACHINE_CATALOG
                          if m not in IMPLEMENTED_MANUFACTURERS)
        messagebox.showinfo(
            f"About {APP_TITLE}",
            f"{APP_TITLE}  v{APP_VERSION}\n"
            "Omega Windows & Doors  |  by McKechnie\n\n"
            "V6 → CNC Processor.\n"
            "Converts Softech V6 XML job files into the\n"
            "format required by your CNC machine software.\n\n"
            f"Implemented end-to-end: {impl}\n"
            f"Stub-only (sample files needed): {stubs}\n\n"
            "Tool libraries and profiles are stored per machine.\n"
            "Use the Profile Library and Tool Library to edit them.",
            parent=self,
        )

    def _show_help(self):
        """Help dialog — TeamViewer link + direct contact."""
        win = ctk.CTkToplevel(self)
        win.title("Help \u2014 Omega CNC Processor")
        win.geometry("540x320")
        win.configure(fg_color=CLR_BG)
        win.transient(self)

        body = ctk.CTkFrame(win, fg_color=CLR_CARD, corner_radius=10)
        body.pack(fill="both", expand=True, padx=20, pady=20)

        ctk.CTkLabel(body, text="Help & Support", font=FONT_TITLE,
                     text_color=CLR_TEXT).pack(anchor="w",
                                               padx=24, pady=(20, 8))

        ctk.CTkLabel(body,
                     text="Need a hand?  Use one of the options below.",
                     font=FONT_LABEL, text_color=CLR_MUTED
                     ).pack(anchor="w", padx=24, pady=(0, 18))

        # TeamViewer
        tv_row = ctk.CTkFrame(body, fg_color=CLR_CARD_ALT, corner_radius=8)
        tv_row.pack(fill="x", padx=24, pady=6)
        ctk.CTkLabel(tv_row, text="Remote support  (TeamViewer)",
                     font=FONT_VALUE, text_color=CLR_TEXT
                     ).pack(anchor="w", padx=14, pady=(10, 2))
        ctk.CTkLabel(tv_row, text=TEAMVIEWER_URL,
                     font=FONT_SMALL, text_color="#7dd3fc",
                     cursor="hand2"
                     ).pack(anchor="w", padx=14, pady=(0, 4))
        ctk.CTkButton(tv_row, text="Open TeamViewer download",
                      height=30, width=220, font=FONT_SMALL,
                      fg_color=CLR_ACCENT, hover_color=CLR_ACCENT_HV,
                      command=lambda: self._open_url(TEAMVIEWER_URL)
                      ).pack(anchor="w", padx=14, pady=(0, 12))

        # Email
        em_row = ctk.CTkFrame(body, fg_color=CLR_CARD_ALT, corner_radius=8)
        em_row.pack(fill="x", padx=24, pady=6)
        ctk.CTkLabel(em_row, text="Direct contact",
                     font=FONT_VALUE, text_color=CLR_TEXT
                     ).pack(anchor="w", padx=14, pady=(10, 2))
        ctk.CTkLabel(em_row, text=SUPPORT_EMAIL,
                     font=FONT_SMALL, text_color="#7dd3fc"
                     ).pack(anchor="w", padx=14, pady=(0, 4))
        ctk.CTkButton(em_row, text=f"Email {SUPPORT_EMAIL}",
                      height=30, width=260, font=FONT_SMALL,
                      fg_color=CLR_ACCENT, hover_color=CLR_ACCENT_HV,
                      command=lambda: self._open_url(f"mailto:{SUPPORT_EMAIL}")
                      ).pack(anchor="w", padx=14, pady=(0, 12))

        win.after(60, lambda: _focus_window(win))

    def _open_url(self, url: str):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as exc:
            messagebox.showerror("Could not open link", str(exc), parent=self)


# ── Profile Editor window ─────────────────────────────────────────────────────

class ProfileEditorWindow(ctk.CTkToplevel):
    """
    Standalone editor for profiles.json.

    Left panel  — scrollable list of profiles.
    Right panel — Profile Details form + per-WSide face/angle form.

    Each section is written only by its own explicit save button.
    """

    # Formula options stored in profiles.json → human label shown in dropdown
    _POS_Y_MAP = {
        "wy1":          "WY1 direct  (default — most profiles)",
        "max_y - wy1":  "MaxY \u2212 WY1  (flip Y axis)",
        "max_z - wy1":  "MaxZ \u2212 WY1  (flip Z axis)",
    }

    # v1.3.1: PAR1 formula maps removed — PAR1 is now WHeight raw and
    # the STD_*.wrk macros own the depth math.

    def __init__(self, master, job=None,
                 initial_manufacturer: str = DEFAULT_MANUFACTURER,
                 initial_machine: str = DEFAULT_MACHINE):
        super().__init__(master)
        self.title("Profile Library \u2014 Omega CNC Processor")
        self.geometry("1020x660")
        self.minsize(900, 600)
        self.configure(fg_color=CLR_BG)
        self.resizable(True, True)
        self.after(60, lambda: _focus_window(self))

        # ── in-memory state ───────────────────────────────────────────────────
        self._data: dict = {}          # key → profile dict (ALL machines)
        self._job  = job               # current loaded Job (may be None)
        self._sel_prof:  str | None = None
        self._sel_wside: str | None = None

        # Per-window manufacturer / machine selection — controls which
        # profiles are listed and which machine new profiles are tagged with.
        self._mfr     = ctk.StringVar(value=initial_manufacturer)
        self._machine = ctk.StringVar(value=initial_machine)

        # ── profile StringVars ─────────────────────────────────────────────────
        self._pf_id    = ctk.StringVar()
        self._pf_desc  = ctk.StringVar()
        self._pf_max_y = ctk.StringVar()
        self._pf_max_z = ctk.StringVar()
        self._pf_mirr  = ctk.StringVar(value="0")

        # ── WSide StringVars ───────────────────────────────────────────────────
        self._ws_ang   = ctk.StringVar()
        self._ws_aa    = ctk.StringVar()
        self._ws_ac    = ctk.StringVar()
        self._ws_fx    = ctk.StringVar()
        self._ws_fy    = ctk.StringVar()
        self._ws_fz    = ctk.StringVar()
        self._ws_posy  = ctk.StringVar()
        # v1.3.1: PAR1 formula / Hole+Slot PAR1 offset vars removed.
        self._ws_posz_off  = ctk.StringVar()   # overrides depth_offset

        # ── Machining (per-WComment) editor state ──────────────────────────────
        # Library of per-machining overrides keyed by V6 WComment. Each entry
        # is bound to a single WType (C/L/R) and stores per-V6-field overrides
        # under "fields". A field set to None (or omitted) means "pass through
        # the V6 value" — that is also how varying / locked fields behave.
        self._sel_mach:   str | None = None
        self._mc_field_vars: dict = {
            name: ctk.StringVar() for name in
            # v1.5: WX1 / WY1 removed — see MACH_FIELDS_BY_WTYPE.
            ("wside", "wdepth", "wheight",
             "ww1", "ww2", "ww3",
             "par7", "par8", "par9")
        }
        # New v1.3 — per-machining step replication count (C/L/R only).
        self._mc_steps_var = ctk.StringVar(value="1")

        self._build_ui()
        self._load_profiles()

        # v1.5.2: when a job is loaded, populate detected machining
        # occurrences in memory so they're visible in the editor
        # without auto-persisting to profiles.json.  The user must
        # click the appropriate explicit save button to commit them.
        # v1.7.4: also re-runs on machine change, auto-selects the
        # first profile so machinings are visible without an extra
        # click, and surfaces errors to stderr instead of swallowing.
        self._enrich_machinings_in_memory(auto_select_first=True)

    def set_job(self, job) -> None:
        """Synchronize a newly loaded V6 job into an existing editor window."""
        changed = job is not self._job
        self._job = job
        self._btn_scan_job.configure(
            state="normal" if job is not None else "disabled",
        )
        if changed and job is not None:
            self._enrich_machinings_in_memory(auto_select_first=True)

    # ── I/O ───────────────────────────────────────────────────────────────────

    def _load_profiles(self):
        path = machine_profiles_path(self._mfr.get(), self._machine.get())
        mkey = self._current_mkey()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = _migrate_profiles_par1(json.load(fh))
                self._data = {}
                for key, value in raw.get("profiles", {}).items():
                    if key.startswith("_"):
                        continue
                    owner, pid = _decompose_pkey(key)
                    if owner not in (None, mkey):
                        continue
                    self._data[_compose_pkey(mkey, pid)] = value
            except Exception as exc:
                messagebox.showerror("Load Error",
                                     f"Could not read this machine's profiles.json:\n{exc}",
                                     parent=self)
                self._data = {}
        else:
            self._data = {}
        # Keep the disk-backed baseline separate from scan/enrichment drafts.
        # Section save buttons update this baseline selectively, preventing a
        # SIDE save from persisting unsaved machining detections.
        self._persisted_data = copy.deepcopy(self._data)
        self._refresh_profile_list()

    def _write_profiles(self) -> bool:
        doc = {
            "_comment": [
                "Omega CNC Processor — Profile Database.",
                "Edit via Profile Library in the application.",
            ],
            "profiles": self._persisted_data,
        }
        try:
            _atomic_write_json(
                machine_profiles_path(self._mfr.get(), self._machine.get()),
                doc,
            )
            return True
        except Exception as exc:
            messagebox.showerror("Save Error",
                                 f"Could not write profiles.json:\n{exc}",
                                 parent=self)
            return False

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Left: profile list ────────────────────────────────────────────────
        left = ctk.CTkFrame(self, width=270, fg_color=CLR_SIDEBAR,
                            corner_radius=0)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        ctk.CTkLabel(left, text="MACHINE",
                     font=("Segoe UI", 10, "bold"),
                     text_color=CLR_MUTED).pack(anchor="w", padx=16,
                                                pady=(18, 6))

        # Manufacturer dropdown
        ctk.CTkComboBox(
            left, variable=self._mfr,
            values=list(MACHINE_CATALOG.keys()),
            state="readonly", height=32, font=FONT_SMALL,
            fg_color=CLR_CARD_ALT, border_color=CLR_BORDER,
            button_color=CLR_BORDER, dropdown_fg_color=CLR_CARD,
            command=self._on_mfr_change,
        ).pack(fill="x", padx=12, pady=(0, 6))

        # Machine dropdown
        self._machine_combo = ctk.CTkComboBox(
            left, variable=self._machine,
            values=MACHINE_CATALOG[self._mfr.get()],
            state="readonly", height=32, font=FONT_SMALL,
            fg_color=CLR_CARD_ALT, border_color=CLR_BORDER,
            button_color=CLR_BORDER, dropdown_fg_color=CLR_CARD,
            command=self._on_machine_change,
        )
        self._machine_combo.pack(fill="x", padx=12, pady=(0, 14))

        ctk.CTkFrame(left, height=1, fg_color=CLR_BORDER,
                     corner_radius=0).pack(fill="x", padx=16, pady=(0, 6))

        ctk.CTkLabel(left, text="DXF LIBRARY",
                     font=("Segoe UI", 10, "bold"),
                     text_color=CLR_MUTED).pack(anchor="w", padx=16,
                                                 pady=(6, 4))
        self._library_search = ctk.StringVar()
        self._library_suite = ctk.StringVar(value="All suites")
        self._library_type = ctk.StringVar(value="All types")
        search = ctk.CTkEntry(left, textvariable=self._library_search,
                              placeholder_text="Search ID or description",
                              height=30, font=FONT_SMALL, fg_color=CLR_CARD_ALT,
                              border_color=CLR_BORDER)
        search.pack(fill="x", padx=12, pady=(0, 5))
        search.bind("<KeyRelease>", lambda _e: self._refresh_profile_list())
        ctk.CTkComboBox(left, variable=self._library_suite,
                         values=["All suites"] + [f"{c} — {n}" for c, n, _t in SUITES],
                         state="readonly", height=30, font=FONT_SMALL,
                         command=self._on_library_suite).pack(fill="x", padx=12, pady=(0, 5))
        self._library_type_combo = ctk.CTkComboBox(
            left, variable=self._library_type, values=["All types"],
            state="readonly", height=30, font=FONT_SMALL,
            command=lambda _v: self._refresh_profile_list())
        self._library_type_combo.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkFrame(left, height=1, fg_color=CLR_BORDER,
                     corner_radius=0).pack(fill="x", padx=16)

        self._list_scroll = ctk.CTkScrollableFrame(left, fg_color="transparent",
                                                   corner_radius=0)
        self._list_scroll.pack(fill="both", expand=True, padx=8, pady=8)

        # Buttons at bottom of left panel
        bf = ctk.CTkFrame(left, fg_color="transparent")
        bf.pack(fill="x", padx=10, pady=(0, 14))

        self._btn_scan_job = ctk.CTkButton(
            bf, text="\U0001f50d  Scan from Job",
            height=32, font=FONT_SMALL,
            fg_color=CLR_WARN, hover_color="#b45309",
            text_color="#1c1917",
            state="normal" if self._job is not None else "disabled",
            command=self._scan_from_job,
        )
        self._btn_scan_job.pack(fill="x", pady=(0, 6))

        ctk.CTkButton(bf, text="+ New Profile", height=32, font=FONT_SMALL,
                      fg_color=CLR_ACCENT, hover_color=CLR_ACCENT_HV,
                      command=self._add_profile).pack(fill="x", pady=(0, 6))

        self._btn_del_prof = ctk.CTkButton(
            bf, text="Delete Profile", height=32, font=FONT_SMALL,
            fg_color="#7f1d1d", hover_color="#991b1b", text_color=CLR_TEXT,
            state="disabled", command=self._delete_profile,
        )
        self._btn_del_prof.pack(fill="x")

        # ── Right: editor area ────────────────────────────────────────────────
        right = ctk.CTkFrame(self, fg_color=CLR_BG, corner_radius=0)
        right.pack(side="left", fill="both", expand=True)

        # Header bar
        hdr = ctk.CTkFrame(right, height=50, fg_color=CLR_CARD,
                           corner_radius=0)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text="Profile Editor",
                     font=("Segoe UI", 13, "bold"),
                     text_color=CLR_TEXT).pack(side="left", padx=20)

        # ── Body: scrollable editor on the left, pinned DXF preview on
        # the right.  The preview lives outside the scrollable area so
        # it stays visible while the user scrolls through machinings,
        # and uses the previously-wasted horizontal space.
        body = ctk.CTkFrame(right, fg_color=CLR_BG, corner_radius=0)
        body.pack(fill="both", expand=True)

        preview_col = ctk.CTkFrame(body, fg_color=CLR_BG,
                                   width=480, corner_radius=0)
        preview_col.pack(side="right", fill="y", padx=(0, 20), pady=16)
        preview_col.pack_propagate(False)

        scr = ctk.CTkScrollableFrame(body, fg_color=CLR_BG, corner_radius=0)
        scr.pack(side="left", fill="both", expand=True,
                 padx=(20, 12), pady=16)
        self._right = scr

        self._build_dxf_preview_panel(preview_col)
        self._build_profile_section(scr)
        self._build_wside_section(scr)
        self._build_machining_section(scr)

    # ── Profile details section ───────────────────────────────────────────────

    def _build_profile_section(self, parent):
        card = self._card(parent, "Profile Details")
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=16, pady=(0, 14))

        # Row 1: ID + Description
        r1 = ctk.CTkFrame(body, fg_color="transparent")
        r1.pack(fill="x", pady=(0, 10))
        self._fld(r1, "Profile No  (V6 BIdentNo)", self._pf_id,
                  side="left", width=170, padx=(0, 20))
        self._fld(r1, "Description", self._pf_desc,
                  side="left", expand=True)

        # Row 2: MaxY + MaxZ + Mirror
        r2 = ctk.CTkFrame(body, fg_color="transparent")
        r2.pack(fill="x")
        self._fld(r2, "MaxY", self._pf_max_y,
                  side="left", width=100, padx=(0, 20))
        self._fld(r2, "MaxZ", self._pf_max_z,
                  side="left", width=100, padx=(0, 20))

        mf = ctk.CTkFrame(r2, fg_color="transparent")
        mf.pack(side="left")
        ctk.CTkLabel(mf, text="DXF Mirror", font=FONT_LABEL,
                     text_color=CLR_MUTED).pack(anchor="w")
        ctk.CTkComboBox(mf, variable=self._pf_mirr, values=["0", "1"],
                        state="readonly", width=80, height=34,
                        font=FONT_LABEL, fg_color=CLR_CARD_ALT,
                        border_color=CLR_BORDER, button_color=CLR_BORDER,
                        dropdown_fg_color=CLR_CARD).pack(anchor="w")
        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.pack(fill="x", pady=(12, 0))
        ctk.CTkButton(actions, text="Save Profile Details", height=30,
                      font=FONT_SMALL, fg_color=CLR_SUCCESS,
                      command=self._save_profile_details).pack(side="left")
        ctk.CTkButton(actions, text="Clear", height=30, width=65,
                      font=FONT_SMALL, fg_color=CLR_CARD_ALT,
                      command=self._clear_profile_form).pack(side="left", padx=8)

    # ── DXF preview panel (pinned right column) ───────────────────────────────

    def _build_dxf_preview_panel(self, parent):
        """Large pinned DXF preview that lives in the right column of
        the editor body — outside the scrollable section, so it stays
        visible while the user scrolls through WSides / machinings.

        Vector geometry is rendered directly on a tk.Canvas so mouse-wheel
        zoom and click-drag pan remain crisp at every scale.
        """
        card = ctk.CTkFrame(parent, fg_color=CLR_CARD,
                            corner_radius=10, border_width=1,
                            border_color=CLR_BORDER)
        card.pack(fill="both", expand=True)

        # ── Header: title + profile-code caption ─────────────────────────
        hdr = ctk.CTkFrame(card, fg_color="transparent")
        hdr.pack(fill="x", padx=16, pady=(12, 6))
        ctk.CTkLabel(hdr, text="DXF Preview",
                     font=("Segoe UI", 13, "bold"),
                     text_color=CLR_TEXT).pack(side="left")
        self._dxf_preview_caption = ctk.CTkLabel(
            hdr, text="", font=FONT_SMALL, text_color=CLR_MUTED,
        )
        self._dxf_preview_caption.pack(side="right")

        # ── Zoom toolbar ─────────────────────────────────────────────────
        tb = ctk.CTkFrame(card, fg_color="transparent")
        tb.pack(fill="x", padx=16, pady=(0, 6))

        btn_kw = dict(width=32, height=26, font=FONT_SMALL,
                      fg_color=CLR_CARD_ALT, hover_color=CLR_BORDER,
                      text_color=CLR_TEXT, corner_radius=6)
        ctk.CTkButton(tb, text="\u2212", command=self._dxf_zoom_out,
                      **btn_kw).pack(side="left", padx=(0, 4))
        ctk.CTkButton(tb, text="+", command=self._dxf_zoom_in,
                      **btn_kw).pack(side="left", padx=(0, 4))
        ctk.CTkButton(tb, text="Fit", command=self._dxf_zoom_fit,
                      width=44, height=26, font=FONT_SMALL,
                      fg_color=CLR_CARD_ALT, hover_color=CLR_BORDER,
                      text_color=CLR_TEXT, corner_radius=6,
                      ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(tb, text="Import DXF", command=self._import_dxf,
                      width=82, height=26, font=FONT_SMALL,
                      fg_color=CLR_CARD_ALT, hover_color=CLR_BORDER,
                      text_color=CLR_TEXT, corner_radius=6).pack(side="left", padx=(0, 8))
        self._dxf_zoom_lbl = ctk.CTkLabel(
            tb, text="—", font=FONT_SMALL, text_color=CLR_MUTED,
        )
        self._dxf_zoom_lbl.pack(side="left")
        ctk.CTkLabel(
            tb, text="(scroll to zoom · drag to pan)",
            font=FONT_SMALL, text_color=CLR_MUTED,
        ).pack(side="right")

        # ── Canvas holder ────────────────────────────────────────────────
        # tk.Canvas provides a scalable vector surface plus click-drag pan.
        holder = ctk.CTkFrame(card, fg_color=CLR_CARD_ALT, corner_radius=8)
        holder.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        canvas_bg = "#111827"
        self._dxf_canvas = tk.Canvas(
            holder, bg=canvas_bg, highlightthickness=0, bd=0,
            cursor="hand2",
        )
        self._dxf_canvas.pack(fill="both", expand=True, padx=8, pady=8)

        # Placeholder text shown when no DXF is loaded.
        self._dxf_canvas_text_id = self._dxf_canvas.create_text(
            10, 10, anchor="nw",
            text="(select a profile)",
            fill="#94a3b8", font=("Segoe UI", 11),
        )
        # Vector geometry and viewport state (DXF units are millimetres).
        self._dxf_primitives = []
        self._dxf_extents = None
        self._dxf_fit_scale    = 1.0     # scale that fits the canvas
        self._dxf_zoom_mult    = 1.0     # user-applied multiplier
        self._dxf_load_token   = 0       # discards stale background results
        self._dxf_preview_holder = holder

        # Bindings: wheel (Win/Mac) + Button-4/5 (Linux) zoom, drag pan,
        # and Configure for re-fit when the panel resizes.
        self._dxf_canvas.bind("<MouseWheel>",  self._dxf_on_wheel)
        self._dxf_canvas.bind("<Button-4>",    self._dxf_on_wheel)
        self._dxf_canvas.bind("<Button-5>",    self._dxf_on_wheel)
        self._dxf_canvas.bind("<ButtonPress-1>", self._dxf_pan_start)
        self._dxf_canvas.bind("<B1-Motion>",     self._dxf_pan_drag)
        self._dxf_canvas.bind("<Configure>",     self._dxf_on_resize)

    # ── WSide section ─────────────────────────────────────────────────────────

    def _build_wside_section(self, parent):
        card = self._card(parent, "Face / WSide Entries", padless=True)

        # Selector row
        sel_row = ctk.CTkFrame(card, fg_color="transparent")
        sel_row.pack(fill="x", padx=16, pady=(0, 8))

        self._ws_tabs = ctk.CTkFrame(sel_row, fg_color="transparent")
        self._ws_tabs.pack(side="left", fill="x", expand=True)

        self._ws_empty_lbl = ctk.CTkLabel(
            self._ws_tabs,
            text="No WSide entries yet. Click '+ Add WSide' to begin.",
            font=FONT_SMALL, text_color=CLR_MUTED,
        )
        self._ws_empty_lbl.pack(anchor="w")

        btn_col = ctk.CTkFrame(sel_row, fg_color="transparent")
        btn_col.pack(side="right")
        ctk.CTkButton(btn_col, text="+ Add WSide",
                      width=106, height=28, font=FONT_SMALL,
                      fg_color=CLR_ACCENT, hover_color=CLR_ACCENT_HV,
                      command=self._add_wside).pack(pady=(0, 4))
        self._btn_del_ws = ctk.CTkButton(
            btn_col, text="Remove",
            width=106, height=28, font=FONT_SMALL,
            fg_color="transparent", border_width=1,
            border_color="#7f1d1d", text_color="#f87171",
            hover_color="#1c0606", state="disabled",
            command=self._delete_wside,
        )
        self._btn_del_ws.pack()

        # WSide detail form (shown when a WSide is selected)
        self._ws_form_wrap = ctk.CTkFrame(card, fg_color=CLR_CARD_ALT,
                                          corner_radius=8)
        self._ws_form_wrap.pack(fill="x", padx=16, pady=(0, 14))

        self._ws_placeholder = ctk.CTkLabel(
            self._ws_form_wrap,
            text="Select a WSide tab above to edit its face parameters.",
            font=FONT_SMALL, text_color=CLR_MUTED,
        )
        self._ws_placeholder.pack(pady=18)

        self._ws_fields_frame = ctk.CTkFrame(self._ws_form_wrap,
                                             fg_color="transparent")
        # not packed until a WSide is selected

        r1 = ctk.CTkFrame(self._ws_fields_frame, fg_color="transparent")
        r1.pack(fill="x", padx=14, pady=(12, 8))
        self._fld(r1, "ANG",    self._ws_ang, side="left",
                  width=80, padx=(0, 20))
        self._fld(r1, "AngleA", self._ws_aa,  side="left",
                  width=80, padx=(0, 20))
        self._fld(r1, "AngleC", self._ws_ac,  side="left", width=80)

        r2 = ctk.CTkFrame(self._ws_fields_frame, fg_color="transparent")
        r2.pack(fill="x", padx=14, pady=(0, 8))
        self._fld(r2, "Face X", self._ws_fx, side="left",
                  width=80, padx=(0, 20))
        self._fld(r2, "Face Y", self._ws_fy, side="left",
                  width=80, padx=(0, 20))
        self._fld(r2, "Face Z", self._ws_fz, side="left", width=80)

        r3 = ctk.CTkFrame(self._ws_fields_frame, fg_color="transparent")
        r3.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(r3, text="PositionY formula", font=FONT_LABEL,
                     text_color=CLR_MUTED).pack(anchor="w")
        ctk.CTkComboBox(
            r3, variable=self._ws_posy,
            values=list(self._POS_Y_MAP.values()),
            state="readonly", width=340, height=34, font=FONT_LABEL,
            fg_color=CLR_CARD_ALT, border_color=CLR_BORDER,
            button_color=CLR_BORDER, dropdown_fg_color=CLR_CARD,
        ).pack(anchor="w")

        # v1.3.1: PAR1 formulas and Hole/Slot PAR1 offsets removed —
        # HOLE / SLOT / POCKET now pass WHeight through verbatim because
        # the corresponding STD_*.wrk macros handle the start point on
        # the face/side internally via L(L1) / L(L*).  Only the
        # PositionZ depth_offset remains user-configurable per WSide.
        ctk.CTkLabel(self._ws_fields_frame,
                     text="Offset overrides  (blank \u2192 uses global config value)",
                     font=FONT_LABEL, text_color=CLR_MUTED,
                     ).pack(anchor="w", padx=14, pady=(4, 2))

        r5 = ctk.CTkFrame(self._ws_fields_frame, fg_color="transparent")
        r5.pack(fill="x", padx=14, pady=(0, 4))
        self._fld(r5, "PositionZ offset", self._ws_posz_off,
                  side="left", width=110)

        # Seen-values info row — populated from job scan; reference only
        self._ws_seen_frame = ctk.CTkFrame(self._ws_fields_frame,
                                           fg_color=CLR_CARD, corner_radius=6)
        self._ws_seen_frame.pack(fill="x", padx=14, pady=(4, 14))
        self._ws_seen_lbl = ctk.CTkLabel(
            self._ws_seen_frame,
            text="",
            font=FONT_SMALL, text_color=CLR_MUTED,
            justify="left", anchor="w",
        )
        self._ws_seen_lbl.pack(anchor="w", padx=10, pady=6)
        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(anchor="e", padx=16, pady=(0, 14))
        ctk.CTkButton(actions, text="Save Selected SIDE / WSide", height=30,
                      font=FONT_SMALL, fg_color=CLR_SUCCESS,
                      command=self._save_wside).pack(side="left")
        ctk.CTkButton(actions, text="Clear", height=30, width=65,
                      font=FONT_SMALL, fg_color=CLR_CARD_ALT,
                      command=self._clear_wside_form).pack(side="left", padx=8)

    # ── Machining (per-WComment) section ──────────────────────────────────────

    # Field labels shown in the read-only "constants" / "varying" rows
    # AND in the editable grid above.
    _MACH_FIELD_LABELS = {
        "wtype":   "WType",
        "wside":   "WSide",
        # v1.5: WX1 / WY1 removed — they're per-cut positions.
        "wdepth":  "WDepth",
        "wheight": "WHeight",
        "ww1":     "WW1",
        "ww2":     "WW2",
        "ww3":     "WW3",
        "par7":    "PAR7",
        "par8":    "PAR8",
        "par9":    "PAR9",
    }

    # Class-level aliases that delegate to the module-level helpers (which
    # are also used by the App auto-sync on file load).
    _MACH_FIELDS_BY_WTYPE = MACH_FIELDS_BY_WTYPE

    _WTYPE_LABELS = {
        "C":     "C  \u2014 Hole",
        "L":     "L  \u2014 Slot",
        "R":     "R  \u2014 Pocket",
        "NOTCH": "NOTCH  \u2014 Saw",
    }

    @staticmethod
    def _normalize_mach(entry: dict) -> dict:
        return normalize_machining_entry(entry)

    def _build_machining_section(self, parent):
        card = self._card(parent, "Machinings  (per-WComment overrides)",
                          padless=True)

        sel_row = ctk.CTkFrame(card, fg_color="transparent")
        sel_row.pack(fill="x", padx=16, pady=(0, 8))

        # Scrollable list of WComments on the left side of the row, buttons
        # on the right.  CTkScrollableFrame is used because comments can be
        # long strings and there may be many entries per profile.
        self._mc_list = ctk.CTkScrollableFrame(
            sel_row, fg_color=CLR_CARD_ALT, corner_radius=6,
            height=120,
        )
        self._mc_list.pack(side="left", fill="both", expand=True)

        self._mc_empty_lbl = ctk.CTkLabel(
            self._mc_list,
            text=("No machining entries yet. Click '+ Add Machining' to "
                  "define one, or run 'Scan from Job' to populate from "
                  "the loaded job."),
            font=FONT_SMALL, text_color=CLR_MUTED,
            wraplength=440, justify="left",
        )
        self._mc_empty_lbl.pack(anchor="w", padx=10, pady=10)

        btn_col = ctk.CTkFrame(sel_row, fg_color="transparent")
        btn_col.pack(side="right", padx=(8, 0))
        ctk.CTkButton(btn_col, text="+ Add Machining",
                      width=140, height=28, font=FONT_SMALL,
                      fg_color=CLR_ACCENT, hover_color=CLR_ACCENT_HV,
                      command=self._add_machining).pack(pady=(0, 4))
        self._btn_del_mc = ctk.CTkButton(
            btn_col, text="Remove",
            width=140, height=28, font=FONT_SMALL,
            fg_color="transparent", border_width=1,
            border_color="#7f1d1d", text_color="#f87171",
            hover_color="#1c0606", state="disabled",
            command=self._delete_machining,
        )
        self._btn_del_mc.pack()

        # Editor card
        self._mc_form_wrap = ctk.CTkFrame(card, fg_color=CLR_CARD_ALT,
                                          corner_radius=8)
        self._mc_form_wrap.pack(fill="x", padx=16, pady=(0, 14))

        self._mc_placeholder = ctk.CTkLabel(
            self._mc_form_wrap,
            text=("Select a machining above to edit its overrides, or add "
                  "one to begin."),
            font=FONT_SMALL, text_color=CLR_MUTED,
        )
        self._mc_placeholder.pack(pady=18)

        self._mc_fields_frame = ctk.CTkFrame(self._mc_form_wrap,
                                             fg_color="transparent")
        # not packed until a machining is selected

        # Header row: WComment (left) + WType badge (right)
        head_row = ctk.CTkFrame(self._mc_fields_frame, fg_color="transparent")
        head_row.pack(fill="x", padx=14, pady=(12, 2))
        self._mc_header_lbl = ctk.CTkLabel(
            head_row, text="", font=FONT_SECTION,
            text_color=CLR_TEXT, anchor="w", justify="left",
        )
        self._mc_header_lbl.pack(side="left")
        self._mc_wtype_lbl = ctk.CTkLabel(
            head_row, text="", font=FONT_LABEL,
            text_color=CLR_MUTED, anchor="e", justify="right",
        )
        self._mc_wtype_lbl.pack(side="right")

        self._mc_seen_lbl = ctk.CTkLabel(
            self._mc_fields_frame, text="", font=FONT_SMALL,
            text_color=CLR_MUTED, anchor="w", justify="left",
        )
        self._mc_seen_lbl.pack(anchor="w", padx=14, pady=(0, 8))

        ctk.CTkLabel(
            self._mc_fields_frame,
            text=("Field overrides  (blank \u2192 V6 value passes through; "
                  "locked fields are ignored)"),
            font=FONT_LABEL, text_color=CLR_MUTED,
        ).pack(anchor="w", padx=14, pady=(4, 4))

        # Two-column grid of editable fields
        grid_wrap = ctk.CTkFrame(self._mc_fields_frame, fg_color="transparent")
        grid_wrap.pack(fill="x", padx=14, pady=(0, 8))

        self._mc_field_widgets: dict = {}
        # v1.5: WX1 / WY1 removed — see MACH_FIELDS_BY_WTYPE.
        all_fields = ("wside", "wdepth",
                      "wheight", "ww1", "ww2", "ww3",
                      "par7", "par8", "par9")
        for i, name in enumerate(all_fields):
            row, col = divmod(i, 2)
            cell = ctk.CTkFrame(grid_wrap, fg_color="transparent")
            cell.grid(row=row, column=col, padx=(0, 24), pady=4, sticky="w")
            ctk.CTkLabel(
                cell, text=self._MACH_FIELD_LABELS[name],
                font=FONT_LABEL, text_color=CLR_MUTED,
                width=70, anchor="w",
            ).pack(side="left")
            var = self._mc_field_vars[name]
            if name == "wside":
                widget = ctk.CTkComboBox(
                    cell, variable=var, values=[""],
                    state="readonly", width=130, height=30, font=FONT_LABEL,
                    fg_color=CLR_CARD_ALT, border_color=CLR_BORDER,
                    button_color=CLR_BORDER, dropdown_fg_color=CLR_CARD,
                )
            else:
                widget = ctk.CTkEntry(
                    cell, textvariable=var, width=130, height=30,
                    font=FONT_LABEL, fg_color=CLR_CARD_ALT,
                    border_color=CLR_BORDER,
                )
            widget.pack(side="left", padx=(6, 8))
            locked_lbl = ctk.CTkLabel(
                cell, text="", font=FONT_SMALL, text_color=CLR_WARN,
                width=110, anchor="w",
            )
            locked_lbl.pack(side="left")
            self._mc_field_widgets[name] = {
                "cell":       cell,
                "widget":     widget,
                "locked_lbl": locked_lbl,
            }

        # Steps row — replicate this Work block N times with stepped depth
        # (PAR3 for C, PAR4 for L, PAR5 for R).  Hidden for NOTCH.
        self._mc_steps_row = ctk.CTkFrame(self._mc_fields_frame,
                                          fg_color="transparent")
        self._mc_steps_row.pack(fill="x", padx=14, pady=(4, 8))
        ctk.CTkLabel(
            self._mc_steps_row, text="Steps",
            font=FONT_LABEL, text_color=CLR_MUTED,
            width=70, anchor="w",
        ).pack(side="left")
        self._mc_steps_widget = ctk.CTkEntry(
            self._mc_steps_row, textvariable=self._mc_steps_var,
            width=80, height=30, font=FONT_LABEL,
            fg_color=CLR_CARD_ALT, border_color=CLR_BORDER,
        )
        self._mc_steps_widget.pack(side="left", padx=(6, 8))
        ctk.CTkLabel(
            self._mc_steps_row,
            text=("Replicate the Work block N times with stepped depth "
                  "(depth/N, 2·depth/N, …, depth)."),
            font=FONT_SMALL, text_color=CLR_MUTED,
            anchor="w", justify="left", wraplength=420,
        ).pack(side="left")

        # Read-only constants & varying-field display
        info_box = ctk.CTkFrame(self._mc_fields_frame,
                                fg_color=CLR_CARD, corner_radius=6)
        info_box.pack(fill="x", padx=14, pady=(8, 14))
        self._mc_const_lbl = ctk.CTkLabel(
            info_box, text="", font=FONT_SMALL, text_color=CLR_MUTED,
            justify="left", anchor="w", wraplength=620,
        )
        self._mc_const_lbl.pack(anchor="w", padx=10, pady=(8, 2))
        self._mc_var_lbl = ctk.CTkLabel(
            info_box, text="", font=FONT_SMALL, text_color=CLR_WARN,
            justify="left", anchor="w", wraplength=620,
        )
        self._mc_var_lbl.pack(anchor="w", padx=10, pady=(0, 8))
        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(anchor="e", padx=16, pady=(0, 14))
        ctk.CTkButton(actions, text="Save Selected Machining", height=30,
                      font=FONT_SMALL, fg_color=CLR_SUCCESS,
                      command=self._save_machining).pack(side="left")
        ctk.CTkButton(actions, text="Clear", height=30, width=65,
                      font=FONT_SMALL, fg_color=CLR_CARD_ALT,
                      command=self._clear_machining_form).pack(side="left", padx=8)

    # ── Machining list / selection / form helpers ─────────────────────────────

    def _refresh_mach_list(self, machinings: dict):
        for w in self._mc_list.winfo_children():
            w.destroy()
        if not machinings:
            self._mc_empty_lbl = ctk.CTkLabel(
                self._mc_list,
                text=("No machining entries yet. Click '+ Add Machining' "
                      "to define one, or run 'Scan from Job' to populate "
                      "from the loaded job."),
                font=FONT_SMALL, text_color=CLR_MUTED,
                wraplength=440, justify="left",
            )
            self._mc_empty_lbl.pack(anchor="w", padx=10, pady=10)
            return
        for key in sorted(machinings.keys()):
            entry = machinings[key] or {}
            wt    = entry.get("wtype", "")
            seen  = entry.get("seen_count")
            label = key
            if wt:
                label = f"[{wt}]  {label}"
            if seen:
                label += f"   ({seen}\u00d7)"
            is_sel = (key == self._sel_mach)
            ctk.CTkButton(
                self._mc_list, text=label, anchor="w", height=28,
                font=FONT_SMALL,
                fg_color=CLR_ACCENT if is_sel else CLR_CARD,
                hover_color=CLR_ACCENT_HV,
                text_color=CLR_TEXT,
                command=lambda k=key: self._select_machining(k),
            ).pack(fill="x", pady=2, padx=4)

    def _select_machining(self, key: str):
        if not self._sel_prof:
            return
        self._sel_mach = key
        prof = self._data.get(self._sel_prof) or {}
        machinings = prof.setdefault("machinings", {})
        entry = self._normalize_mach(machinings.get(key, {}))
        machinings[key] = entry  # write back any migration

        wtype    = entry.get("wtype") or "C"
        fields   = entry.get("fields") or {}
        varying  = set(entry.get("varying_fields") or [])
        # NOTCH only edits par7-9; C/L/R edit V6 geometry.  In both cases
        # WSide is permitted as the visible header field.
        if wtype == "NOTCH":
            relevant = list(self._MACH_FIELDS_BY_WTYPE.get("NOTCH", []))
        else:
            relevant = list(self._MACH_FIELDS_BY_WTYPE.get(wtype, ["wside"]))

        # Header and badges
        self._mc_header_lbl.configure(text=f"WComment:  {key}")
        self._mc_wtype_lbl.configure(
            text=f"WType:  {self._WTYPE_LABELS.get(wtype, wtype)}   (locked)"
        )

        # Steps row visibility — only meaningful for C/L/R
        steps_val = entry.get("steps", 1) or 1
        self._mc_steps_var.set(str(int(steps_val)))
        if wtype in ("C", "L", "R"):
            self._mc_steps_row.pack(fill="x", padx=14, pady=(4, 8),
                                    before=self._mc_const_lbl.master)
        else:
            self._mc_steps_row.pack_forget()
        seen = entry.get("seen_count")
        if seen:
            seen_txt = f"Seen in loaded job: {seen}\u00d7"
        else:
            seen_txt = "Not seen in current job (manual entry)"
        self._mc_seen_lbl.configure(text=seen_txt)

        # WSide combo values = blank + this profile's defined wsides
        ws_keys = [k for k in sorted((prof.get("wsides") or {}).keys())
                   if k != NOTCH_WSIDE_KEY]
        self._mc_field_widgets["wside"]["widget"].configure(
            values=[""] + ws_keys
        )

        def _v(x):
            return "" if x in (None, "") else str(x)

        # Show/hide fields per WType, and apply locked state for varying fields
        for name, info in self._mc_field_widgets.items():
            if name not in relevant:
                info["cell"].grid_remove()
                self._mc_field_vars[name].set("")
                info["locked_lbl"].configure(text="")
                continue
            info["cell"].grid()
            if name in varying:
                # Varying across instances → locked, V6 value passes through
                self._mc_field_vars[name].set("")
                info["locked_lbl"].configure(text="locked \u2014 from V6")
                info["widget"].configure(state="disabled")
            else:
                self._mc_field_vars[name].set(_v(fields.get(name)))
                info["locked_lbl"].configure(text="")
                if name == "wside":
                    info["widget"].configure(state="readonly")
                else:
                    info["widget"].configure(state="normal")

        # Constants & varying display
        consts = entry.get("constants") or {}
        if consts:
            const_parts = [
                f"{self._MACH_FIELD_LABELS.get(k, k)}={consts[k]}"
                for k in consts
            ]
            self._mc_const_lbl.configure(
                text="Constant across instances  \u2014  "
                     + "  |  ".join(const_parts)
            )
        else:
            self._mc_const_lbl.configure(
                text="Constant across instances  \u2014  (none recorded yet; "
                     "run 'Scan from Job' to populate)"
            )

        if varying:
            var_parts = [self._MACH_FIELD_LABELS.get(k, k)
                         for k in sorted(varying)]
            self._mc_var_lbl.configure(
                text="Varies per instance (locked, V6 value passes through)  "
                     "\u2014  " + ", ".join(var_parts)
            )
        else:
            self._mc_var_lbl.configure(
                text="Varies per instance  \u2014  (none — all fields are "
                     "constant or unset)"
            )

        self._btn_del_mc.configure(state="normal")
        self._show_mach_form()
        self._refresh_mach_list(machinings)

    def _show_mach_form(self):
        self._mc_placeholder.pack_forget()
        self._mc_fields_frame.pack(fill="x")

    def _hide_mach_form(self):
        self._mc_fields_frame.pack_forget()
        self._mc_placeholder.pack(pady=18)
        self._btn_del_mc.configure(state="disabled")

    def _add_machining(self):
        if not self._sel_prof:
            messagebox.showwarning("No profile selected",
                                   "Select or create a profile first.",
                                   parent=self)
            return
        dlg = ctk.CTkInputDialog(
            text="WComment for the new machining entry:",
            title="Add Machining",
        )
        wc = (dlg.get_input() or "").strip()
        if not wc:
            return
        machs = self._data[self._sel_prof].setdefault("machinings", {})
        if wc in machs:
            # Already exists — just open it
            self._refresh_mach_list(machs)
            self._select_machining(wc)
            return

        dlg2 = ctk.CTkInputDialog(
            text=("WType template for this machining:\n"
                  "  C = Hole\n  L = Slot\n  R = Pocket\n"
                  "  N = Notch  (saw cut, edits PAR7-9)"),
            title="Add Machining \u2014 WType",
        )
        wt_raw = (dlg2.get_input() or "").strip().upper()
        wt = "NOTCH" if wt_raw in ("N", "NOTCH") else wt_raw
        if wt not in ("C", "L", "R", "NOTCH"):
            messagebox.showwarning(
                "Invalid WType",
                "WType must be C (Hole), L (Slot), R (Pocket), or N (Notch).",
                parent=self,
            )
            return

        # Pre-fill empty (None) for every field this WType uses — the user
        # then fills in the values they want overridden, or leaves blank to
        # let the V6 value pass through.
        new_fields = {f: None for f in self._MACH_FIELDS_BY_WTYPE[wt]}
        machs[wc] = {
            "wtype":          wt,
            "fields":         new_fields,
            "steps":          1,
            "constants":      {},
            "varying_fields": [],
            "seen_count":     0,
        }
        self._refresh_mach_list(machs)
        self._select_machining(wc)

    def _delete_machining(self):
        if not self._sel_prof or not self._sel_mach:
            return
        selected = self._sel_mach
        machs = self._data[self._sel_prof].get("machinings") or {}
        machs.pop(selected, None)
        persisted = self._persisted_data.get(self._sel_prof)
        if persisted is not None:
            (persisted.get("machinings") or {}).pop(selected, None)
        self._sel_mach = None
        self._refresh_mach_list(machs)
        self._hide_mach_form()
        self._write_profiles()

    # ── Helpers: widget factories ─────────────────────────────────────────────

    def _card(self, parent, title: str, padless: bool = False) -> ctk.CTkFrame:
        """Titled section card; returns the card frame."""
        card = ctk.CTkFrame(parent, fg_color=CLR_CARD, corner_radius=10)
        card.pack(fill="x", pady=(0, 14))

        hdr = ctk.CTkFrame(card, fg_color="transparent")
        hdr.pack(fill="x", padx=16, pady=(12, 0))
        ctk.CTkLabel(hdr, text=title, font=FONT_SECTION,
                     text_color=CLR_TEXT).pack(side="left")
        ctk.CTkFrame(card, height=1, fg_color=CLR_BORDER,
                     corner_radius=0).pack(fill="x", padx=16, pady=8)
        return card

    def _fld(self, parent, label: str, var,
             side="top", width=None, expand=False, padx=0):
        """Labelled entry field."""
        f = ctk.CTkFrame(parent, fg_color="transparent")
        if side == "left":
            f.pack(side="left", padx=padx,
                   expand=expand, fill="x" if expand else "none")
        else:
            f.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(f, text=label, font=FONT_LABEL,
                     text_color=CLR_MUTED).pack(anchor="w")
        kw: dict = dict(textvariable=var, height=34, font=FONT_LABEL,
                        fg_color=CLR_CARD_ALT, border_color=CLR_BORDER)
        if width:
            kw["width"] = width
        e = ctk.CTkEntry(f, **kw)
        if expand:
            e.pack(fill="x")
        else:
            e.pack(anchor="w")

    # ── Profile list ──────────────────────────────────────────────────────────

    # ── Manufacturer / machine selectors ──────────────────────────────────────

    def _on_mfr_change(self, _value=None):
        machines = MACHINE_CATALOG.get(self._mfr.get(), [])
        self._machine_combo.configure(values=machines)
        if machines and self._machine.get() not in machines:
            self._machine.set(machines[0])
        # v1.7.4: re-enrich machinings for the newly selected machine,
        # then refresh the list (enrichment helper also refreshes).
        self._load_profiles()
        self._enrich_machinings_in_memory(auto_select_first=True)

    def _on_machine_change(self, _value=None):
        self._load_profiles()
        self._enrich_machinings_in_memory(auto_select_first=True)

    def _on_library_suite(self, _value=None):
        suite = self._library_suite.get().split(" ", 1)[0]
        types = ["All types"]
        types += [f"{t} — {n}" for t, n in suite_filter_types(suite)]
        self._library_type.set("All types")
        self._library_type_combo.configure(values=types)
        self._refresh_profile_list()

    def _apply_scan_to_data(self) -> tuple[int, int, int, int, set[str]]:
        """v1.7.5: shared implementation for the auto-enrich on editor
        open / mfr / machine change AND the explicit 'Scan from Job'
        button.  Mutates ``self._data`` in memory only — never writes
        to disk; the user must explicitly save the completed section.

        For each profile (BIdentNo) the loaded V6 job touches, auto-creates
        an in-memory profile with BHeight/BWidth mapped to MaxY/MaxZ,
        bootstraps the WSide list from observed work data, and then calls
        :func:`apply_machining_occurrences` to fill the machinings
        library.

        Returns
        -------
        (added_profiles, added_wsides, added_machinings,
         skipped_no_machining_bars, job_keys)
            ``job_keys`` is the set of self._data keys the loaded job
            touched (after stub creation), so callers can pick a smart
            auto-select target."""
        job_keys: set[str] = set()
        if self._job is None:
            return (0, 0, 0, 0, job_keys)

        # Collect: pid → wside_key → first-seen geometry, plus profile
        # dimensions/description from the XML bar.
        found: dict[str, dict[str, dict]] = {}
        profile_info: dict[str, dict] = {}
        no_machining_pids: set[str] = set()
        machining_pids: set[str] = set()
        for bar in self._job.bars:
            pid = (bar.bidentno or "").strip()
            if not pid:
                continue
            profile_info.setdefault(pid, {
                "description": (bar.bdescription or "").strip(),
                "max_y": round(bar.bheight, 4),
                "max_z": round(bar.bwidth, 4),
            })
            found.setdefault(pid, {})
            if any(c.works for c in bar.cuts):
                machining_pids.add(pid)
            else:
                no_machining_pids.add(pid)

        mkey = self._current_mkey()

        def _existing_key(p: str) -> str | None:
            c = _compose_pkey(mkey, p)
            if c in self._data:
                return c
            return None

        for bar in self._job.bars:
            pid = (bar.bidentno or "").strip()
            if not pid:
                continue
            for cut in bar.cuts:
                for work in cut.works:
                    if work.wtype == "NOTCH":
                        # One synthetic notch WSide per profile.
                        if NOTCH_WSIDE_KEY not in found[pid]:
                            found[pid][NOTCH_WSIDE_KEY] = {"_notch": True}
                        continue
                    ws_key = str(work.wside)
                    if ws_key not in found[pid]:
                        found[pid][ws_key] = {
                            "wheight": work.wheight,
                            "wdepth":  work.wdepth,
                        }

        added_profiles = 0
        added_wsides   = 0
        for pid, wsides in found.items():
            info = profile_info[pid]
            key = _existing_key(pid)
            if key is None:
                key = _compose_pkey(mkey, pid)
                self._data[key] = {
                    "description": info["description"],
                    "max_y": info["max_y"],
                    "max_z": info["max_z"],
                    "dxf_mirror": 0,
                    "wsides": {},
                }
                added_profiles += 1
            else:
                profile = self._data[key]
                if not (profile.get("description") or "").strip():
                    profile["description"] = info["description"]
                if not profile.get("max_y"):
                    profile["max_y"] = info["max_y"]
                if not profile.get("max_z"):
                    profile["max_z"] = info["max_z"]
            job_keys.add(key)

            db_wsides = self._data[key].setdefault("wsides", {})
            for ws_key, seen in wsides.items():
                if ws_key in db_wsides:
                    continue
                if seen.get("_notch"):
                    db_wsides[ws_key] = {
                        "ang": 180, "angle_a": 0, "angle_c": 0,
                        "face_x": 0, "face_y": 0, "face_z": 0,
                        "_notch": True,
                    }
                else:
                    db_wsides[ws_key] = {
                        "ang": 180, "angle_a": 0, "angle_c": 0,
                        "face_x": 0, "face_y": 0, "face_z": 0,
                        "pos_y": "wy1",
                        "seen_wheight": seen["wheight"],
                        "seen_wdepth":  seen["wdepth"],
                    }
                added_wsides += 1

        added_machinings, _refreshed = apply_machining_occurrences(
            self._data, self._job, machining_pids, mkey,
        )
        return (added_profiles, added_wsides, added_machinings,
                len(no_machining_pids - machining_pids), job_keys)

    def _enrich_machinings_in_memory(self, auto_select_first: bool = False):
        """v1.7.5: auto-create stub profile entries from the loaded V6
        job (in-memory only — not persisted until explicitly saved), apply
        detected machining occurrences, and prefer auto-selecting a
        profile that is actually in the loaded job.

        Pre-v1.7.5 only called :func:`apply_machining_occurrences`,
        which silently skipped any V6 pid that wasn't already in the
        library — so the very common workflow "load a job with a new
        profile, open the editor" showed an empty machinings panel.
        v1.7.5 calls the same helper as the explicit 'Scan from Job'
        button so the user sees real data without an extra click.

        Errors are logged to stderr (no longer silently swallowed)."""
        job_keys: set[str] = set()
        if self._job is not None:
            try:
                _ap, _aw, _am, _sk, job_keys = self._apply_scan_to_data()
            except Exception as exc:
                import traceback, sys
                print(f"[ProfileEditor] machining enrichment failed: "
                      f"{exc!r}", file=sys.stderr)
                traceback.print_exc()
        self._refresh_profile_list()
        if auto_select_first:
            keys = self._profiles_for_current_machine()
            if keys:
                # v1.7.5: prefer a profile that's actually in the loaded
                # job over the alphabetical first profile in the entire
                # library, so the user lands on a profile that shows the
                # machinings we just detected — not an unrelated one
                # whose name happens to sort first.
                in_job = [k for k in keys if k in job_keys]
                pool   = in_job if in_job else keys
                first  = sorted(pool, key=lambda k: _decompose_pkey(k)[1])[0]
                self._select_profile(first)

    def _current_mkey(self) -> str:
        return _machine_key(self._mfr.get(), self._machine.get())

    def _profiles_for_current_machine(self) -> list[str]:
        """Return only profiles owned by the selected machine."""
        mk = self._current_mkey()
        out = []
        for key in self._data:
            if key.startswith("_"):
                continue
            m, _ = _decompose_pkey(key)
            if m == mk:
                out.append(key)
        return out

    def _refresh_profile_list(self):
        for w in self._list_scroll.winfo_children():
            w.destroy()
        keys = self._profiles_for_current_machine()
        if not keys:
            ctk.CTkLabel(
                self._list_scroll,
                text=f"No profiles for\n{self._current_mkey()} yet.",
                font=FONT_SMALL, text_color=CLR_MUTED, justify="left",
            ).pack(pady=10, padx=10, anchor="w")
            return
        suite = self._library_suite.get().split(" ", 1)[0] if self._library_suite.get() != "All suites" else ""
        type_code = self._library_type.get().split(" ", 1)[0] if self._library_type.get() != "All types" else ""
        query = self._library_search.get().strip()
        keys = [k for k in keys if profile_matches_filter(
            _decompose_pkey(k)[1], self._data[k].get("description", ""),
            query, suite, type_code)]
        # Group by suite and canonical storage type before profile ID. This
        # keeps 809 and 813 together even when 811 IDs sort between them.
        def group_sort_key(profile_key):
            profile_id = _decompose_pkey(profile_key)[1]
            found_suite, found_type = infer_suite_type(profile_id)
            return (
                found_suite or "999",
                canonical_type_code(found_type) or "999",
                profile_id,
            )

        previous_group = None
        for key in sorted(keys, key=group_sort_key):
            pid  = _decompose_pkey(key)[1]
            raw_suite, raw_type = infer_suite_type(pid)
            group = (raw_suite, canonical_type_code(raw_type))
            if group != previous_group:
                suite_name = next((name for code, name, _ in SUITES if code == group[0]), "Unclassified")
                type_name = (
                    "Hinged / Bifold Door"
                    if group[1] == "809"
                    else next(
                        (name for _s, _n, ts in SUITES
                         for code, name in ts if code == group[1]),
                        "Other",
                    )
                )
                ctk.CTkLabel(self._list_scroll, text=f"▾ {suite_name} / {group[1] or 'Other'} {type_name}",
                             font=("Segoe UI", 10, "bold"), text_color=CLR_MUTED).pack(anchor="w", pady=(8, 2))
                previous_group = group
            desc = self._data[key].get("description", "")
            label = f"{pid}\n{desc}" if desc else pid
            is_sel = (key == self._sel_prof)
            ctk.CTkButton(
                self._list_scroll,
                text=label, anchor="w", font=FONT_SMALL, height=50,
                fg_color=CLR_ACCENT if is_sel else CLR_CARD_ALT,
                hover_color=CLR_ACCENT_HV if is_sel else CLR_BORDER,
                text_color=CLR_TEXT,
                command=lambda k=key: self._select_profile(k),
            ).pack(fill="x", pady=2)

    def _select_profile(self, key: str):
        self._sel_prof  = key
        self._sel_wside = None
        prof = self._data[key]

        # The id field shows just the pid (the bit after "::") so the
        # user edits the V6 BIdentNo, not the storage key.
        pid = _decompose_pkey(key)[1]
        self._pf_id.set(pid)
        self._pf_desc.set(prof.get("description", ""))
        self._pf_max_y.set(str(prof.get("max_y", "")))
        self._pf_max_z.set(str(prof.get("max_z", "")))
        self._pf_mirr.set(str(prof.get("dxf_mirror", 0)))

        self._btn_del_prof.configure(state="normal")
        self._refresh_profile_list()
        self._refresh_wside_tabs(prof.get("wsides") or {})
        self._hide_wside_form()
        self._sel_mach = None
        self._refresh_mach_list(prof.get("machinings") or {})
        self._hide_mach_form()
        self._load_dxf_preview(pid)

    # ── DXF vector preview ────────────────────────────────────────────────────

    def _import_dxf(self):
        if not self._sel_prof:
            messagebox.showwarning("No profile selected", "Select a profile before importing a DXF.", parent=self)
            return
        pid = _decompose_pkey(self._sel_prof)[1]
        destination = canonical_dxf_path(
            pid, self._mfr.get(), self._machine.get(),
        )
        if not destination:
            messagebox.showwarning("Unknown suite", f"Could not infer a catalog suite/type from '{pid}'.", parent=self)
            return
        source = filedialog.askopenfilename(parent=self, title="Import DXF",
                                            filetypes=[("DXF files", "*.dxf *.DXF")])
        if not source: return
        try:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(source, destination)
            self._load_dxf_preview(pid)
        except OSError as exc:
            messagebox.showerror("Import DXF", f"Could not import DXF:\n{exc}", parent=self)

    def _load_dxf_preview(self, profile_code):
        if not hasattr(self, "_dxf_canvas"):
            return
        self._dxf_load_token += 1
        load_token = self._dxf_load_token
        path = canonical_dxf_path(
            profile_code, self._mfr.get(), self._machine.get(),
        )
        self._dxf_preview_caption.configure(text=profile_code or "")
        if not path or not os.path.isfile(path):
            self._dxf_primitives, self._dxf_extents = [], None
            self._dxf_show_message(f"No DXF for {profile_code}\nUse Import DXF to add it to the suite library.")
            return

        self._dxf_primitives, self._dxf_extents = [], None
        self._dxf_show_message(f"Loading DXF for {profile_code}…")

        def load_in_background():
            try:
                primitives = load_dxf_file(path)
                extents = self._dxf_bounds(primitives)
                if not extents:
                    raise ValueError("no drawable model-space geometry")
                result = (primitives, extents, None)
            except Exception as exc:
                result = ([], None, str(exc))
            try:
                self.after(
                    0,
                    lambda: self._finish_dxf_preview(
                        load_token, profile_code, result,
                    ),
                )
            except (RuntimeError, tk.TclError):
                pass

        threading.Thread(
            target=load_in_background,
            name=f"dxf-preview-{profile_code}",
            daemon=True,
        ).start()

    def _finish_dxf_preview(self, load_token, profile_code, result):
        """Apply only the newest background DXF parse result to the canvas."""
        if load_token != self._dxf_load_token or not self.winfo_exists():
            return
        primitives, extents, error = result
        if error:
            self._dxf_primitives, self._dxf_extents = [], None
            self._dxf_show_message(f"Could not parse DXF:\n{error}")
            return
        self._dxf_primitives = primitives
        self._dxf_extents = extents
        x0, y0, x1, y1 = self._dxf_extents
        self._dxf_preview_caption.configure(text=f"{profile_code}  •  {x1-x0:.2f} × {y1-y0:.2f} mm")
        self._dxf_zoom_mult = 1.0
        self._dxf_redraw()

    @staticmethod
    def _dxf_bounds(primitives):
        return primitive_bounds(primitives)

    def _dxf_redraw(self):
        if not self._dxf_extents: return
        c = self._dxf_canvas; c.delete("all"); c.update_idletasks()
        cw, ch = max(c.winfo_width(), 1), max(c.winfo_height(), 1)
        x0, y0, x1, y1 = self._dxf_extents
        w, h = max(x1-x0, .001), max(y1-y0, .001)
        self._dxf_fit_scale = dxf_fit_scale(
            self._dxf_extents, cw, ch,
        )
        scale = self._dxf_fit_scale * self._dxf_zoom_mult
        draw_w = max(cw, w * scale + 30)
        draw_h = max(ch, h * scale + 30)
        ox, oy = (draw_w-w*scale)/2-x0*scale, (draw_h-h*scale)/2+y1*scale
        def point(x, y): return ox+x*scale, oy-y*scale
        stroke_width = max(
            1.4, min(2.4, 1.4 * math.sqrt(self._dxf_zoom_mult))
        )
        for kind, v in self._dxf_primitives:
            if kind == "line":
                c.create_line(
                    *point(v[0], v[1]), *point(v[2], v[3]),
                    fill="#e2e8f0", width=stroke_width,
                )
            elif kind == "circle":
                a, b = point(v[0]-v[2], v[1]-v[2]); d, e = point(v[0]+v[2], v[1]+v[2])
                c.create_oval(
                    a, e, d, b, outline="#7dd3fc", width=stroke_width,
                )
            elif kind == "arc":
                a, b = point(v[0]-v[2], v[1]-v[2]); d, e = point(v[0]+v[2], v[1]+v[2])
                c.create_arc(
                    a, e, d, b, start=v[3],
                    extent=(v[4]-v[3]) % 360, style="arc",
                    outline="#7dd3fc", width=stroke_width,
                )
        c.config(scrollregion=(0, 0, draw_w, draw_h))
        self._dxf_zoom_lbl.configure(text=f"{int(self._dxf_zoom_mult * 100)}%")

    def _dxf_show_message(self, text):
        c = self._dxf_canvas; c.delete("all")
        c.create_text(max(c.winfo_width(), 1)//2, max(c.winfo_height(), 1)//2, text=text,
                      fill="#cbd5e1", font=("Segoe UI", 11), justify="center")
        self._dxf_zoom_lbl.configure(text="—")

    def _dxf_set_zoom(self, mult):
        if not self._dxf_extents:
            return
        self._dxf_zoom_mult = max(.25, min(float(mult), 16.0))
        self._dxf_redraw()

    def _dxf_zoom_out(self):
        self._dxf_set_zoom(self._dxf_zoom_mult / 1.25)

    def _dxf_zoom_in(self):
        self._dxf_set_zoom(self._dxf_zoom_mult * 1.25)

    def _dxf_zoom_fit(self):
        if not self._dxf_extents:
            return
        self._dxf_zoom_mult = 1.0
        self._dxf_redraw()

    def _dxf_on_wheel(self, event):
        if not self._dxf_extents: return
        self._dxf_set_zoom(self._dxf_zoom_mult * (1.15 if (getattr(event, "delta", 0) > 0 or getattr(event, "num", None) == 4) else 1/1.15))
    def _dxf_pan_start(self, event):
        if self._dxf_extents: self._dxf_canvas.scan_mark(event.x, event.y)
    def _dxf_pan_drag(self, event):
        if self._dxf_extents: self._dxf_canvas.scan_dragto(event.x, event.y, gain=1)
    def _dxf_on_resize(self, _event=None):
        if self._dxf_extents: self._dxf_redraw()

    # ── WSide tabs ────────────────────────────────────────────────────────────

    def _refresh_wside_tabs(self, wsides: dict):
        for w in self._ws_tabs.winfo_children():
            w.destroy()
        if not wsides:
            self._ws_empty_lbl = ctk.CTkLabel(
                self._ws_tabs,
                text="No WSide entries yet. Click '+ Add WSide' to begin.",
                font=FONT_SMALL, text_color=CLR_MUTED,
            )
            self._ws_empty_lbl.pack(anchor="w")
            return
        # Numeric WSides first (1..7), then the synthetic "N" notch entry last.
        def _ws_sort_key(k: str) -> tuple:
            if k == NOTCH_WSIDE_KEY:
                return (2, 0)
            return (0, int(k)) if k.isdigit() else (1, 0)

        for key in sorted(wsides, key=_ws_sort_key):
            is_sel = (key == self._sel_wside)
            label = (f"Notch ({NOTCH_WSIDE_KEY})"
                     if key == NOTCH_WSIDE_KEY else f"WSide {key}")
            width = 110 if key == NOTCH_WSIDE_KEY else 94
            ctk.CTkButton(
                self._ws_tabs,
                text=label, width=width, height=30,
                font=FONT_SMALL,
                fg_color=CLR_ACCENT if is_sel else CLR_CARD,
                hover_color=CLR_ACCENT_HV,
                text_color=CLR_TEXT,
                command=lambda k=key: self._select_wside(k),
            ).pack(side="left", padx=(0, 6))

    def _select_wside(self, key: str):
        self._sel_wside = key
        wsides = (self._data.get(self._sel_prof) or {}).get("wsides") or {}
        ws = wsides.get(key, {})

        self._ws_ang.set(str(ws.get("ang", 180)))
        self._ws_aa.set(str(ws.get("angle_a", 0)))
        self._ws_ac.set(str(ws.get("angle_c", 0)))
        self._ws_fx.set(str(ws.get("face_x", 0)))
        self._ws_fy.set(str(ws.get("face_y", 0)))
        self._ws_fz.set(str(ws.get("face_z", 0)))

        raw_posy = ws.get("pos_y", "wy1")
        self._ws_posy.set(
            self._POS_Y_MAP.get(raw_posy,
                                self._POS_Y_MAP["wy1"])
        )

        # v1.3.1: par1_formula / pocket_par1_formula / hole_par1_offset /
        # slot_par1_offset are no longer read — HOLE / SLOT / POCKET pass
        # WHeight raw and the .wrk macros handle the start point.
        def _off(v):
            return "" if v is None else str(v)

        self._ws_posz_off.set(_off(ws.get("depth_offset")))

        # Show seen WHeight/WDepth hint if recorded from a job scan
        seen_wh = ws.get("seen_wheight")
        seen_wd = ws.get("seen_wdepth")
        if seen_wh is not None or seen_wd is not None:
            parts = []
            if seen_wh is not None:
                parts.append(f"WHeight: {seen_wh}")
            if seen_wd is not None:
                parts.append(f"WDepth: {seen_wd}")
            self._ws_seen_lbl.configure(
                text="From job scan \u2014 " + "  |  ".join(parts)
            )
            self._ws_seen_frame.pack(fill="x", padx=14, pady=(0, 14))
        else:
            self._ws_seen_frame.pack_forget()

        self._btn_del_ws.configure(state="normal")
        self._show_wside_form()
        self._refresh_wside_tabs(wsides)

    def _show_wside_form(self):
        self._ws_placeholder.pack_forget()
        self._ws_fields_frame.pack(fill="x")

    def _hide_wside_form(self):
        self._ws_fields_frame.pack_forget()
        self._ws_placeholder.pack(pady=18)
        self._btn_del_ws.configure(state="disabled")

    # ── CRUD actions ──────────────────────────────────────────────────────────

    def _scan_from_job(self):
        """
        Scan the loaded job for all profile IDs and WSide numbers, then
        auto-create any missing entries in the database (with default values).
        Existing entries are left untouched.

        v1.7.5: bulk logic lives in :meth:`_apply_scan_to_data` and is
        shared with the auto-enrich on editor open / mfr or machine
        change.  This method just calls the helper and surfaces a
        summary messagebox.
        """
        if self._job is None:
            return

        (added_profiles, added_wsides, added_machinings,
         skipped_no_machining, _job_keys) = self._apply_scan_to_data()

        self._refresh_profile_list()
        # If a profile is currently selected, refresh its machining list too
        if self._sel_prof and self._sel_prof in self._data:
            self._refresh_mach_list(
                self._data[self._sel_prof].get("machinings") or {}
            )

        msg_lines = [
            "Scan complete.",
            "",
            f"  Profiles added: {added_profiles}",
            f"  WSide entries added: {added_wsides}",
            f"  Machining entries added: {added_machinings}",
        ]
        if skipped_no_machining:
            msg_lines.append(
                f"  Profiles with no machining ops: {skipped_no_machining}"
            )
        msg_lines += [
            "",
            "Fill in the face coordinates for each new WSide entry,",
            "then use the matching explicit save button for each section.",
        ]
        messagebox.showinfo("Scan from Job", "\n".join(msg_lines), parent=self)

    def _add_profile(self):
        dlg = ctk.CTkInputDialog(
            text="Enter the V6 Profile No (BIdentNo):",
            title="New Profile",
        )
        pid = (dlg.get_input() or "").strip()
        if not pid:
            return
        mkey = self._current_mkey()
        key  = _compose_pkey(mkey, pid)
        if key in self._data:
            messagebox.showwarning(
                "Duplicate",
                f"Profile '{pid}' already exists for {mkey}.",
                parent=self,
            )
            return
        self._data[key] = {
            "description": "",
            "max_y": 0,
            "max_z": 0,
            "dxf_mirror": 0,
            "wsides": {},
        }
        self._refresh_profile_list()
        self._select_profile(key)

    def _delete_profile(self):
        if not self._sel_prof:
            return
        pid = _decompose_pkey(self._sel_prof)[1]
        if not messagebox.askyesno(
            "Confirm Delete",
            f"Delete profile '{pid}'?\nThis cannot be undone.",
            parent=self,
        ):
            return
        deleted_key = self._sel_prof
        del self._data[deleted_key]
        self._persisted_data.pop(deleted_key, None)
        dxf_path = canonical_dxf_path(
            pid, self._mfr.get(), self._machine.get(),
        )
        if dxf_path and os.path.isfile(dxf_path):
            try:
                os.remove(dxf_path)
            except OSError as exc:
                messagebox.showerror(
                    "Delete DXF",
                    f"Profile was removed, but its DXF could not be deleted:\n{exc}",
                    parent=self,
                )
        self._sel_prof  = None
        self._sel_wside = None
        self._sel_mach  = None
        self._btn_del_prof.configure(state="disabled")
        self._refresh_profile_list()
        self._refresh_mach_list({})
        self._hide_mach_form()
        self._write_profiles()

    def _add_wside(self):
        if not self._sel_prof:
            messagebox.showwarning("No profile selected",
                                   "Select or create a profile first.",
                                   parent=self)
            return
        dlg = ctk.CTkInputDialog(
            text=("WSide number (1\u20137), or 'N' for the notch / end-mill "
                  "face:"),
            title="Add WSide",
        )
        raw = (dlg.get_input() or "").strip()
        if not raw:
            return
        is_notch = raw.upper() == NOTCH_WSIDE_KEY
        if is_notch:
            key = NOTCH_WSIDE_KEY
        elif raw.isdigit() and 1 <= int(raw) <= 7:
            key = raw
        else:
            messagebox.showwarning(
                "Invalid",
                "WSide must be 1\u20137 or 'N' (notch / end-mill face).",
                parent=self,
            )
            return
        wsides = self._data[self._sel_prof].setdefault("wsides", {})
        if key not in wsides:
            if is_notch:
                wsides[key] = {
                    "ang": 180, "angle_a": 0, "angle_c": 0,
                    "face_x": 0, "face_y": 0, "face_z": 0,
                    "_notch": True,
                }
            else:
                wsides[key] = {
                    "ang": 180, "angle_a": 0, "angle_c": 0,
                    "face_x": 0, "face_y": 0, "face_z": 0,
                    "pos_y": "wy1",
                }
        self._refresh_wside_tabs(wsides)
        self._select_wside(key)

    def _delete_wside(self):
        if not self._sel_prof or not self._sel_wside:
            return
        selected = self._sel_wside
        wsides = self._data[self._sel_prof].get("wsides", {})
        wsides.pop(selected, None)
        persisted = self._persisted_data.get(self._sel_prof)
        if persisted is not None:
            (persisted.get("wsides") or {}).pop(selected, None)
        self._sel_wside = None
        self._refresh_wside_tabs(wsides)
        self._hide_wside_form()
        self._write_profiles()

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save_current(self):
        if not self._sel_prof:
            messagebox.showwarning("Nothing to save",
                                   "Select or create a profile first.",
                                   parent=self)
            return

        new_pid = self._pf_id.get().strip()
        if not new_pid:
            messagebox.showwarning("Validation error",
                                   "Profile No cannot be empty.", parent=self)
            return

        # Storage key is composite "machine_key::pid".
        mkey    = self._current_mkey()
        new_key = _compose_pkey(mkey, new_pid)
        if new_key != self._sel_prof and new_key in self._data:
            messagebox.showwarning(
                "Duplicate",
                f"Profile '{new_pid}' already exists for {mkey}.",
                parent=self,
            )
            return

        # Validate numerics
        try:
            max_y = float(self._pf_max_y.get())
            max_z = float(self._pf_max_z.get())
        except ValueError:
            messagebox.showwarning("Validation error",
                                   "MaxY and MaxZ must be numbers.", parent=self)
            return
        try:
            dxf_mirror = int(self._pf_mirr.get())
        except ValueError:
            dxf_mirror = 0

        # Pop under old key, update, re-insert under new key
        prof = self._data.pop(self._sel_prof)
        prof["description"] = self._pf_desc.get().strip()
        prof["max_y"]       = max_y
        prof["max_z"]       = max_z
        prof["dxf_mirror"]  = dxf_mirror
        # v1.5.1: the composite storage key now carries the machine
        # assignment, so drop any stale "machine" field on the value.
        prof.pop("machine", None)

        # Write back WSide form if one is selected
        if self._sel_wside:
            try:
                ang     = int(self._ws_ang.get())
                angle_a = float(self._ws_aa.get())
                angle_c = float(self._ws_ac.get())
                face_x  = float(self._ws_fx.get())
                face_y  = float(self._ws_fy.get())
                face_z  = float(self._ws_fz.get())
            except ValueError:
                messagebox.showwarning(
                    "Validation error",
                    "All WSide fields must be numbers.", parent=self,
                )
                self._data[self._sel_prof] = prof
                return

            # Notch ("N") entries only carry face/angle/ANG — skip the
            # pos_y / par1 formula / offset fields entirely so they don't
            # leak into the JSON or interfere with future scans. The rest
            # of the save flow (rename, _write_profiles, refresh) still
            # runs below so editing Profile No while N is selected works.
            if self._sel_wside == NOTCH_WSIDE_KEY:
                entry = {
                    "ang":     ang,
                    "angle_a": angle_a,
                    "angle_c": angle_c,
                    "face_x":  face_x,
                    "face_y":  face_y,
                    "face_z":  face_z,
                    "_notch":  True,
                }
            else:
                # Reverse map display label → formula key
                rev_posy = {v: k for k, v in self._POS_Y_MAP.items()}
                pos_y = rev_posy.get(self._ws_posy.get(), "wy1")

                # v1.3.1: par1_formula / hole_par1_offset / slot_par1_offset
                # are no longer written.  The .wrk macros own the depth math.

                # Parse the only surviving per-WSide offset (blank → use global)
                def _parse_off(raw: str):
                    raw = raw.strip()
                    if raw == "":
                        return None
                    try:
                        return float(raw)
                    except ValueError:
                        return None

                posz_off = _parse_off(self._ws_posz_off.get())

                # Preserve seen_wheight / seen_wdepth if already in the entry
                existing = prof.get("wsides", {}).get(self._sel_wside, {})
                entry = {
                    "ang":     ang,
                    "angle_a": angle_a,
                    "angle_c": angle_c,
                    "face_x":  face_x,
                    "face_y":  face_y,
                    "face_z":  face_z,
                    "pos_y":   pos_y,
                }
                if posz_off is not None:
                    entry["depth_offset"] = posz_off
                if "seen_wheight" in existing:
                    entry["seen_wheight"] = existing["seen_wheight"]
                if "seen_wdepth" in existing:
                    entry["seen_wdepth"] = existing["seen_wdepth"]

            prof.setdefault("wsides", {})[self._sel_wside] = entry

        # Write back currently-selected machining override entry, if any
        if self._sel_mach:
            machs    = prof.setdefault("machinings", {})
            existing = self._normalize_mach(machs.get(self._sel_mach, {}))
            wtype    = existing.get("wtype") or "C"
            relevant = self._MACH_FIELDS_BY_WTYPE.get(wtype, [])
            varying  = set(existing.get("varying_fields") or [])

            def _parse_field(name: str, raw: str):
                raw = (raw or "").strip()
                if raw == "":
                    return None
                if name == "wside":
                    return raw  # keep as string
                try:
                    return float(raw)
                except ValueError:
                    return None

            new_fields: dict = {}
            for name in relevant:
                if name in varying:
                    # Locked: V6 value always passes through
                    new_fields[name] = None
                else:
                    new_fields[name] = _parse_field(
                        name, self._mc_field_vars[name].get()
                    )

            existing["fields"] = new_fields

            # Steps (C/L/R only) — replicate the Work block N times
            steps_raw = (self._mc_steps_var.get() or "1").strip()
            try:
                steps_val = max(1, int(float(steps_raw)))
            except ValueError:
                steps_val = 1
            existing["steps"] = steps_val if wtype in ("C", "L", "R") else 1

            machs[self._sel_mach] = existing

        self._data[new_key] = prof
        self._sel_prof      = new_key

        if self._write_profiles():
            messagebox.showinfo(
                "Saved",
                f"Profile '{new_pid}' saved to profiles.json for {mkey}.",
                parent=self,
            )
            self._refresh_profile_list()

    def _clear_profile_form(self):
        for v in (self._pf_id, self._pf_desc, self._pf_max_y, self._pf_max_z): v.set("")
        self._pf_mirr.set("0")

    def _clear_wside_form(self):
        for v in (self._ws_ang, self._ws_aa, self._ws_ac, self._ws_fx, self._ws_fy, self._ws_fz, self._ws_posz_off): v.set("")
        self._ws_posy.set(self._POS_Y_MAP["wy1"])

    def _clear_machining_form(self):
        for v in self._mc_field_vars.values(): v.set("")
        self._mc_steps_var.set("1")

    def _save_profile_details(self):
        """Update draft metadata without creating a disk record."""
        if not self._sel_prof: return
        old_key = self._sel_prof
        pid = self._pf_id.get().strip()
        try: y, z = float(self._pf_max_y.get()), float(self._pf_max_z.get())
        except ValueError: messagebox.showwarning("Validation error", "MaxY and MaxZ must be numbers.", parent=self); return
        key = _compose_pkey(self._current_mkey(), pid)
        if not pid or (key != old_key and key in self._data):
            messagebox.showwarning("Validation error", "Profile No is required and must be unique.", parent=self); return
        p = self._data.pop(old_key)
        p.update(description=self._pf_desc.get().strip(), max_y=y, max_z=z, dxf_mirror=int(self._pf_mirr.get() or 0))
        self._data[key] = p
        was_persisted = old_key in self._persisted_data
        if was_persisted:
            persisted = self._persisted_data.pop(old_key)
            persisted.update(
                description=p["description"], max_y=p["max_y"],
                max_z=p["max_z"], dxf_mirror=p["dxf_mirror"],
            )
            self._persisted_data[key] = persisted
            old_pid = _decompose_pkey(old_key)[1]
            if old_pid != pid:
                old_dxf = canonical_dxf_path(
                    old_pid, self._mfr.get(), self._machine.get(),
                )
                new_dxf = canonical_dxf_path(
                    pid, self._mfr.get(), self._machine.get(),
                )
                if old_dxf and new_dxf and os.path.isfile(old_dxf):
                    try:
                        os.makedirs(os.path.dirname(new_dxf), exist_ok=True)
                        os.replace(old_dxf, new_dxf)
                    except OSError as exc:
                        messagebox.showerror(
                            "Rename DXF",
                            f"Profile was renamed, but its DXF could not be moved:\n{exc}",
                            parent=self,
                        )
        self._sel_prof = key
        if was_persisted:
            self._write_profiles()
        self._refresh_profile_list()

    def _ensure_persisted_profile(self) -> dict:
        """Create the selected machine's record from draft XML metadata."""
        draft = self._data[self._sel_prof]
        return self._persisted_data.setdefault(self._sel_prof, {
            "description": draft.get("description", ""),
            "max_y": draft.get("max_y", 0),
            "max_z": draft.get("max_z", 0),
            "dxf_mirror": draft.get("dxf_mirror", 0),
            "wsides": {},
            "machinings": {},
        })

    def _save_wside(self):
        """Save only the selected SIDE / WSide."""
        if not self._sel_prof or not self._sel_wside: return
        try: values = (int(self._ws_ang.get()), float(self._ws_aa.get()), float(self._ws_ac.get()), float(self._ws_fx.get()), float(self._ws_fy.get()), float(self._ws_fz.get()))
        except ValueError: messagebox.showwarning("Validation error", "All WSide fields must be numbers.", parent=self); return
        k = self._sel_wside; e = dict(zip(("ang","angle_a","angle_c","face_x","face_y","face_z"), values))
        if k == NOTCH_WSIDE_KEY: e["_notch"] = True
        else:
            e["pos_y"] = {v:k for k,v in self._POS_Y_MAP.items()}.get(self._ws_posy.get(), "wy1")
            raw = self._ws_posz_off.get().strip()
            if raw:
                try: e["depth_offset"] = float(raw)
                except ValueError: messagebox.showwarning("Validation error", "PositionZ offset must be a number.", parent=self); return
        self._data[self._sel_prof].setdefault("wsides", {})[k] = e
        self._ensure_persisted_profile().setdefault("wsides", {})[k] = copy.deepcopy(e)
        if self._write_profiles(): self._select_wside(k)

    def _save_machining(self):
        """Save only the selected machining override."""
        if not self._sel_prof or not self._sel_mach: return
        machs = self._data[self._sel_prof].setdefault("machinings", {})
        e = self._normalize_mach(machs.get(self._sel_mach, {})); wt = e.get("wtype", "C"); fields = {}
        for n in self._MACH_FIELDS_BY_WTYPE.get(wt, []):
            raw = self._mc_field_vars[n].get().strip()
            if not raw or n in set(e.get("varying_fields") or []): fields[n] = None
            elif n == "wside": fields[n] = raw
            else:
                try: fields[n] = float(raw)
                except ValueError: messagebox.showwarning("Validation error", f"{n} must be a number.", parent=self); return
        e["fields"] = fields
        try: e["steps"] = max(1, int(float(self._mc_steps_var.get() or 1))) if wt in ("C","L","R") else 1
        except ValueError: e["steps"] = 1
        machs[self._sel_mach] = e
        self._ensure_persisted_profile().setdefault(
            "machinings", {},
        )[self._sel_mach] = copy.deepcopy(e)
        if self._write_profiles(): self._select_machining(self._sel_mach)


# ── Tool Library window ───────────────────────────────────────────────────────

class ToolLibraryWindow(ctk.CTkToplevel):
    """
    Editor for tools.json (v1.3 typed schema).

    Each tool entry has:
        type        — DRILL / STEP_DRILL / END_MILL / MILL_CUTTER /
                      HORIZONTAL_BLADE / VERTICAL_BLADE / T_SLOT_BLADE /
                      TAP / DISABLED
        code        — free-form string; used verbatim in the machine output
        description — free text label
        fields      — type-specific dict (rendered by the dynamic editor below)
    """

    def __init__(self, master,
                 initial_manufacturer: str = DEFAULT_MANUFACTURER,
                 initial_machine: str = DEFAULT_MACHINE):
        super().__init__(master)
        self.title("Tool Library \u2014 Omega CNC Processor")
        self.geometry("980x700")
        self.minsize(880, 600)
        self.configure(fg_color=CLR_BG)
        self.resizable(True, True)
        self.after(60, lambda: _focus_window(self))

        # Per-machine tool dict (code → {type, code, description, fields});
        # reloaded each time the machine selector changes.  All persistence
        # goes through the per-machine helpers in this module.  Migration
        # from the v1.2 flat schema happens transparently in _read_tools_doc.
        self._data: dict = {}
        self._sel:  str | None = None

        self._mfr     = ctk.StringVar(value=initial_manufacturer)
        self._machine = ctk.StringVar(value=initial_machine)

        self._tl_code  = ctk.StringVar()
        self._tl_type  = ctk.StringVar(value="DRILL")
        self._tl_desc  = ctk.StringVar()
        # Per-type field StringVars, lazily populated by _rebuild_field_grid.
        self._field_vars: dict[str, ctk.StringVar] = {}
        # Container that holds the dynamic field grid (set in _build_form).
        self._field_holder = None

        self._build_ui()
        self._load()

    # ── I/O ───────────────────────────────────────────────────────────────────

    def _load(self):
        try:
            self._data = load_tools_for_machine(self._mfr.get(),
                                                self._machine.get())
        except Exception as exc:
            messagebox.showerror("Load Error",
                                 f"Could not read tools.json:\n{exc}",
                                 parent=self)
            self._data = {}
        self._sel = None
        if hasattr(self, "_btn_del"):
            self._btn_del.configure(state="disabled")
        self._refresh_list()
        self._update_machine_label()

    def _save_file(self) -> bool:
        try:
            save_tools_for_machine(self._mfr.get(), self._machine.get(),
                                   self._data)
            return True
        except Exception as exc:
            messagebox.showerror("Save Error",
                                 f"Could not write tools.json:\n{exc}",
                                 parent=self)
            return False

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Left: machine selector + tool list
        left = ctk.CTkFrame(self, width=240, fg_color=CLR_SIDEBAR,
                            corner_radius=0)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        ctk.CTkLabel(left, text="MACHINE",
                     font=("Segoe UI", 10, "bold"),
                     text_color=CLR_MUTED).pack(anchor="w", padx=16,
                                                pady=(18, 6))

        ctk.CTkComboBox(
            left, variable=self._mfr,
            values=list(MACHINE_CATALOG.keys()),
            state="readonly", height=32, font=FONT_SMALL,
            fg_color=CLR_CARD_ALT, border_color=CLR_BORDER,
            button_color=CLR_BORDER, dropdown_fg_color=CLR_CARD,
            command=self._on_mfr_change,
        ).pack(fill="x", padx=12, pady=(0, 6))

        self._machine_combo = ctk.CTkComboBox(
            left, variable=self._machine,
            values=MACHINE_CATALOG[self._mfr.get()],
            state="readonly", height=32, font=FONT_SMALL,
            fg_color=CLR_CARD_ALT, border_color=CLR_BORDER,
            button_color=CLR_BORDER, dropdown_fg_color=CLR_CARD,
            command=lambda _v=None: self._load(),
        )
        self._machine_combo.pack(fill="x", padx=12, pady=(0, 14))

        ctk.CTkFrame(left, height=1, fg_color=CLR_BORDER,
                     corner_radius=0).pack(fill="x", padx=16, pady=(0, 6))

        ctk.CTkLabel(left, text="TOOLS",
                     font=("Segoe UI", 10, "bold"),
                     text_color=CLR_MUTED).pack(anchor="w", padx=16,
                                                pady=(6, 6))
        ctk.CTkFrame(left, height=1, fg_color=CLR_BORDER,
                     corner_radius=0).pack(fill="x", padx=16)

        self._list_scroll = ctk.CTkScrollableFrame(
            left, fg_color="transparent", corner_radius=0,
        )
        self._list_scroll.pack(fill="both", expand=True, padx=8, pady=8)

        bf = ctk.CTkFrame(left, fg_color="transparent")
        bf.pack(fill="x", padx=10, pady=(0, 14))
        ctk.CTkButton(bf, text="+ Add Tool", height=32, font=FONT_SMALL,
                      fg_color=CLR_ACCENT, hover_color=CLR_ACCENT_HV,
                      command=self._add_tool).pack(fill="x", pady=(0, 6))
        self._btn_del = ctk.CTkButton(
            bf, text="Delete Tool", height=32, font=FONT_SMALL,
            fg_color="#7f1d1d", hover_color="#991b1b",
            text_color=CLR_TEXT, state="disabled",
            command=self._delete_tool,
        )
        self._btn_del.pack(fill="x")

        # Right: editor
        right = ctk.CTkFrame(self, fg_color=CLR_BG, corner_radius=0)
        right.pack(side="left", fill="both", expand=True)

        hdr = ctk.CTkFrame(right, height=50, fg_color=CLR_CARD,
                           corner_radius=0)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        self._machine_label = ctk.CTkLabel(
            hdr, text="Tool Editor", font=("Segoe UI", 13, "bold"),
            text_color=CLR_TEXT,
        )
        self._machine_label.pack(side="left", padx=20)
        ctk.CTkButton(hdr, text="\U0001f4be  Save Changes",
                      width=148, height=32, font=FONT_SMALL,
                      fg_color=CLR_SUCCESS, hover_color="#16a34a",
                      text_color="#052e16",
                      command=self._save_current).pack(side="right", padx=14)

        # Toolbar: Copy from… / Import / Export
        toolbar = ctk.CTkFrame(right, fg_color=CLR_CARD_ALT, corner_radius=0)
        toolbar.pack(fill="x")
        ctk.CTkButton(
            toolbar, text="\u2398  Copy from another machine\u2026",
            height=30, width=240, font=FONT_SMALL,
            fg_color="transparent", border_width=1,
            border_color=CLR_BORDER, text_color=CLR_TEXT,
            hover_color=CLR_CARD,
            command=self._copy_from_machine,
        ).pack(side="left", padx=(14, 6), pady=8)
        ctk.CTkButton(
            toolbar, text="\u2b07  Import\u2026", height=30, width=110,
            font=FONT_SMALL,
            fg_color="transparent", border_width=1,
            border_color=CLR_BORDER, text_color=CLR_TEXT,
            hover_color=CLR_CARD,
            command=self._import_tools,
        ).pack(side="left", padx=6, pady=8)
        ctk.CTkButton(
            toolbar, text="\u2b06  Export\u2026", height=30, width=110,
            font=FONT_SMALL,
            fg_color="transparent", border_width=1,
            border_color=CLR_BORDER, text_color=CLR_TEXT,
            hover_color=CLR_CARD,
            command=self._export_tools,
        ).pack(side="left", padx=6, pady=8)

        scr = ctk.CTkScrollableFrame(right, fg_color=CLR_BG, corner_radius=0)
        scr.pack(fill="both", expand=True, padx=20, pady=16)

        self._build_form(scr)

    # ── Header / machine dropdowns ────────────────────────────────────────────

    def _on_mfr_change(self, _value=None):
        machines = MACHINE_CATALOG.get(self._mfr.get(), [])
        self._machine_combo.configure(values=machines)
        if machines and self._machine.get() not in machines:
            self._machine.set(machines[0])
        self._load()

    def _update_machine_label(self):
        if hasattr(self, "_machine_label"):
            self._machine_label.configure(
                text=f"Tool Editor  \u2014  {self._mfr.get()} / {self._machine.get()}"
            )

    # ── Copy from another machine ─────────────────────────────────────────────

    def _copy_from_machine(self):
        """Copy the entire tool list from another machine into this one."""
        keys = [k for k in _all_machine_keys()
                if k != _machine_key(self._mfr.get(), self._machine.get())]
        if not keys:
            messagebox.showinfo("No other machines",
                                "Nothing to copy from.", parent=self)
            return

        src = _ChoiceDialog(self, title="Copy tools from\u2026",
                            prompt="Select source machine:",
                            options=keys).result
        if not src:
            return

        src_mfr, src_mac = _split_machine_key(src)
        src_tools = load_tools_for_machine(src_mfr, src_mac)
        if not src_tools:
            messagebox.showinfo(
                "Source empty",
                f"{src} has no tools defined.", parent=self,
            )
            return

        mode = _ChoiceDialog(
            self, title="Copy mode",
            prompt=(
                f"{src} has {len(src_tools)} tool(s).\n"
                "How should they be applied?"
            ),
            options=[
                "Merge — keep my tools, only add new ones",
                "Replace — discard my tools and use the source list",
            ],
        ).result
        if not mode:
            return

        if mode.startswith("Replace"):
            self._data = dict(src_tools)
        else:
            for k, v in src_tools.items():
                self._data.setdefault(k, v)

        self._sel = None
        self._btn_del.configure(state="disabled")
        if self._save_file():
            self._refresh_list()
            messagebox.showinfo(
                "Copied",
                f"Copied {len(src_tools)} tool(s) from {src}.",
                parent=self,
            )

    # ── Import / export ───────────────────────────────────────────────────────

    def _import_tools(self):
        """Import a tool list from JSON / XML / TXT / HTML."""
        path = filedialog.askopenfilename(
            title="Import tool library",
            filetypes=[
                ("Supported", "*.json *.xml *.txt *.html *.htm"),
                ("JSON", "*.json"),
                ("CAM3D / generic XML", "*.xml"),
                ("Text or HTML", "*.txt *.html *.htm"),
                ("All files", "*.*"),
            ],
            parent=self,
        )
        if not path:
            return
        try:
            tools = _parse_tool_file(path)
        except Exception as exc:
            messagebox.showerror("Import failed", str(exc), parent=self)
            return
        if not tools:
            messagebox.showwarning(
                "Nothing imported",
                "Could not find any T-numbered tools in that file.\n"
                "JSON and CAM3D-style XML import most reliably.",
                parent=self,
            )
            return

        mode = _ChoiceDialog(
            self, title="Import mode",
            prompt=(
                f"Found {len(tools)} tool(s) in:\n"
                f"{os.path.basename(path)}\n\n"
                "How should they be applied?"
            ),
            options=[
                "Merge — keep my tools, only add new ones",
                "Replace — discard my tools and use the imported list",
            ],
        ).result
        if not mode:
            return

        if mode.startswith("Replace"):
            self._data = dict(tools)
        else:
            for k, v in tools.items():
                self._data.setdefault(k, v)

        self._sel = None
        self._btn_del.configure(state="disabled")
        if self._save_file():
            self._refresh_list()
            messagebox.showinfo(
                "Imported",
                f"Imported {len(tools)} tool(s) into "
                f"{self._mfr.get()} / {self._machine.get()}.",
                parent=self,
            )

    def _export_tools(self):
        """Export the current machine's tool list as a JSON file."""
        suggested = (f"tools_{self._mfr.get()}_{self._machine.get()}.json"
                     .replace(" ", "_").replace("/", "_"))
        path = filedialog.asksaveasfilename(
            title="Export tool library",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialfile=suggested,
            parent=self,
        )
        if not path:
            return
        doc = {
            "_comment": [
                "Omega CNC Processor — Tool Library export.",
                f"Source machine: {self._mfr.get()} | {self._machine.get()}",
            ],
            "tools": self._data,
        }
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=4)
            messagebox.showinfo("Exported",
                                f"Tool library exported to:\n{path}",
                                parent=self)
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc), parent=self)

    def _build_form(self, parent):
        card = ctk.CTkFrame(parent, fg_color=CLR_CARD, corner_radius=10)
        card.pack(fill="x", pady=(0, 12))

        hdr = ctk.CTkFrame(card, fg_color="transparent")
        hdr.pack(fill="x", padx=16, pady=(12, 0))
        ctk.CTkLabel(hdr, text="Tool Details", font=FONT_SECTION,
                     text_color=CLR_TEXT).pack(side="left")
        ctk.CTkFrame(card, height=1, fg_color=CLR_BORDER,
                     corner_radius=0).pack(fill="x", padx=16, pady=8)

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=16, pady=(0, 14))

        # Row 1: code + type
        r1 = ctk.CTkFrame(body, fg_color="transparent")
        r1.pack(fill="x", pady=(0, 10))
        self._fld(r1, "Tool Code  (free-form)", self._tl_code,
                  side="left", width=160, padx=(0, 20))

        tf = ctk.CTkFrame(r1, fg_color="transparent")
        tf.pack(side="left")
        ctk.CTkLabel(tf, text="Type", font=FONT_LABEL,
                     text_color=CLR_MUTED).pack(anchor="w")
        ctk.CTkComboBox(tf, variable=self._tl_type,
                        values=TOOL_TYPES,
                        state="readonly", width=180, height=34,
                        font=FONT_LABEL, fg_color=CLR_CARD_ALT,
                        border_color=CLR_BORDER, button_color=CLR_BORDER,
                        dropdown_fg_color=CLR_CARD,
                        command=self._on_type_change).pack(anchor="w")

        # Row 2: description (full width)
        r2 = ctk.CTkFrame(body, fg_color="transparent")
        r2.pack(fill="x", pady=(0, 8))
        self._fld(r2, "Description", self._tl_desc, side="left", expand=True)

        # Dynamic per-type field grid
        self._field_holder = ctk.CTkFrame(card, fg_color="transparent")
        self._field_holder.pack(fill="x", padx=16, pady=(0, 14))
        self._rebuild_field_grid(self._tl_type.get())

        # Helper note
        note = ctk.CTkFrame(parent, fg_color=CLR_CARD_ALT, corner_radius=8)
        note.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(
            note,
            text=(
                "Tool selection is type-aware: drills/step-drills satisfy "
                "holes, end-mills/mill-cutters satisfy slots & pockets, "
                "blades satisfy notches.  TAP and DISABLED are never "
                "auto-selected.  Steer manually with WComment prefixes "
                "[SD], [D], [M], [N]."
            ),
            font=FONT_SMALL, text_color=CLR_MUTED,
            wraplength=720, justify="left",
        ).pack(anchor="w", padx=14, pady=10)

    def _on_type_change(self, _value=None):
        """Combo callback — rebuild the per-type field grid."""
        self._rebuild_field_grid(self._tl_type.get())

    def _rebuild_field_grid(self, tool_type: str):
        """
        Replace the dynamic field grid with widgets matching `tool_type`.
        Preserves any field values that the new layout still uses.
        """
        if self._field_holder is None:
            return

        # Capture current values
        prior = {k: v.get() for k, v in self._field_vars.items()}

        for w in self._field_holder.winfo_children():
            w.destroy()
        self._field_vars = {}

        spec = TOOL_TYPE_FIELDS.get(tool_type, [])
        if not spec:
            ctk.CTkLabel(
                self._field_holder,
                text="(No additional fields for this tool type.)",
                font=FONT_SMALL, text_color=CLR_MUTED,
            ).pack(anchor="w", pady=(4, 0))
            return

        # Two-column grid
        grid = ctk.CTkFrame(self._field_holder, fg_color="transparent")
        grid.pack(fill="x")
        for col in range(2):
            grid.grid_columnconfigure(col, weight=1, uniform="tlcol")

        for i, (label, key) in enumerate(spec):
            row, col = divmod(i, 2)
            cell = ctk.CTkFrame(grid, fg_color="transparent")
            cell.grid(row=row, column=col, sticky="ew",
                      padx=(0, 12) if col == 0 else 0, pady=(0, 8))
            ctk.CTkLabel(cell, text=label, font=FONT_LABEL,
                         text_color=CLR_MUTED).pack(anchor="w")
            var = ctk.StringVar(value=prior.get(key, ""))
            self._field_vars[key] = var
            ctk.CTkEntry(cell, textvariable=var, height=34,
                         font=FONT_LABEL, fg_color=CLR_CARD_ALT,
                         border_color=CLR_BORDER).pack(fill="x")

    def _fld(self, parent, label, var, side="top",
             width=None, expand=False, padx=0):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        if side == "left":
            f.pack(side="left", padx=padx,
                   expand=expand, fill="x" if expand else "none")
        else:
            f.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(f, text=label, font=FONT_LABEL,
                     text_color=CLR_MUTED).pack(anchor="w")
        kw: dict = dict(textvariable=var, height=34, font=FONT_LABEL,
                        fg_color=CLR_CARD_ALT, border_color=CLR_BORDER)
        if width:
            kw["width"] = width
        e = ctk.CTkEntry(f, **kw)
        if expand:
            e.pack(fill="x")
        else:
            e.pack(anchor="w")

    # ── List ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _summary_for(entry: dict) -> str:
        """One-line description of a tool entry for the list."""
        ttype = entry.get("type", "")
        f = entry.get("fields") or {}
        if ttype == "DRILL":
            return f"DRILL  Ø{f.get('drill_diameter', '?')}"
        if ttype == "STEP_DRILL":
            return f"STEP_DRILL  {f.get('inner_dia', '?')} / {f.get('outer_dia', '?')}"
        if ttype in ("END_MILL", "MILL_CUTTER"):
            return f"{ttype}  Ø{f.get('cutter_dia', '?')}"
        if ttype in ("HORIZONTAL_BLADE", "VERTICAL_BLADE", "T_SLOT_BLADE"):
            return f"{ttype}  Ø{f.get('blade_dia', '?')} × {f.get('blade_thick', '?')}"
        if ttype == "TAP":
            return f"TAP  {f.get('drill_diameter', '?')}"
        if ttype == "DISABLED":
            return "DISABLED"
        return ttype or "(unknown)"

    def _refresh_list(self):
        for w in self._list_scroll.winfo_children():
            w.destroy()
        if not self._data:
            ctk.CTkLabel(self._list_scroll, text="No tools yet.",
                         font=FONT_SMALL, text_color=CLR_MUTED).pack(pady=10)
            return

        def _sort_key(code: str):
            # Numeric sort: try the trailing digits, fall back to text.
            digits = "".join(ch for ch in code if ch.isdigit())
            try:
                return (0, int(digits)) if digits else (1, code)
            except ValueError:
                return (1, code)

        for key in sorted(self._data, key=_sort_key):
            t = self._data[key]
            label = f"{key}   {self._summary_for(t)}"
            is_sel = key == self._sel
            ctk.CTkButton(
                self._list_scroll,
                text=label, anchor="w", font=FONT_SMALL, height=40,
                fg_color=CLR_ACCENT if is_sel else CLR_CARD_ALT,
                hover_color=CLR_ACCENT_HV if is_sel else CLR_BORDER,
                text_color=CLR_TEXT,
                command=lambda k=key: self._select(k),
            ).pack(fill="x", pady=2)

    def _select(self, key: str):
        self._sel = key
        t = self._data[key]
        self._tl_code.set(key)
        ttype = t.get("type", "DRILL")
        if ttype not in TOOL_TYPES:
            ttype = "DRILL"
        self._tl_type.set(ttype)
        self._tl_desc.set(t.get("description", ""))
        # Rebuild field grid for this type, then prime its values.
        self._rebuild_field_grid(ttype)
        fields = t.get("fields") or {}
        for k, var in self._field_vars.items():
            v = fields.get(k)
            var.set("" if v is None else str(v))
        self._btn_del.configure(state="normal")
        self._refresh_list()

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def _add_tool(self):
        dlg = ctk.CTkInputDialog(
            text="Tool code  (free-form, e.g. T1, T22, 6, BLADE-A):",
            title="Add Tool",
        )
        key = (dlg.get_input() or "").strip()
        if not key:
            return
        if key in self._data:
            messagebox.showwarning("Duplicate",
                                   f"Tool '{key}' already exists.",
                                   parent=self)
            return
        self._data[key] = {
            "type": "DRILL",
            "code": key,
            "description": "",
            "fields": {},
        }
        self._refresh_list()
        self._select(key)

    def _delete_tool(self):
        if not self._sel:
            return
        if not messagebox.askyesno("Confirm Delete",
                                   f"Delete tool '{self._sel}'?",
                                   parent=self):
            return
        del self._data[self._sel]
        self._sel = None
        self._btn_del.configure(state="disabled")
        self._refresh_list()
        self._save_file()

    def _save_current(self):
        code = self._tl_code.get().strip()
        if not code:
            messagebox.showwarning("Validation",
                                   "Tool code is required.",
                                   parent=self)
            return

        tool_type = self._tl_type.get()
        if tool_type not in TOOL_TYPES:
            messagebox.showwarning("Validation",
                                   f"Unknown tool type '{tool_type}'.",
                                   parent=self)
            return

        # Parse all type-specific fields; any non-empty value must be numeric.
        fields: dict = {}
        for label, key in TOOL_TYPE_FIELDS.get(tool_type, []):
            raw = self._field_vars.get(key, ctk.StringVar()).get().strip()
            if not raw:
                continue
            try:
                fields[key] = float(raw)
            except ValueError:
                messagebox.showwarning(
                    "Validation",
                    f"'{label}' must be a number (got '{raw}').",
                    parent=self,
                )
                return

        # Rename if code changed
        if self._sel and self._sel != code:
            self._data.pop(self._sel, None)

        self._data[code] = {
            "type":        tool_type,
            "code":        code,
            "description": self._tl_desc.get().strip(),
            "fields":      fields,
        }
        self._sel = code

        if self._save_file():
            messagebox.showinfo("Saved",
                                f"Tool '{code}' saved to tools.json.",
                                parent=self)
            self._refresh_list()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _bootstrap_dxf_library_dirs()
    if "--packaging-smoke-check" in sys.argv:
        smoke_log = os.environ.get("OMEGA_PACKAGING_SMOKE_LOG", "")
        try:
            smoke_errors = _packaging_smoke_check()
            smoke_result = "\n".join(smoke_errors)
        except Exception:
            smoke_result = traceback.format_exc()
        if smoke_log:
            try:
                with open(smoke_log, "w", encoding="utf-8") as fh:
                    fh.write(smoke_result or "OK")
            except OSError:
                pass
        raise SystemExit(1 if smoke_result else 0)
    else:
        app = App()
        app.mainloop()
