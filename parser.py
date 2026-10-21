"""
parser.py — Softech V6 XML parser
Reads a V6 job file and returns structured Python objects.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Optional


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Work:
    wtype:    str            # 'C'=hole, 'L'=slot, 'R'=rect pocket, 'NOTCH'=notch
    wside:    int            # face side (1-7); 0 for NOTCH (no face reference)
    wx1:      float          # X position (C/L/R)
    wy1:      float          # Y position (C/L/R)
    ww1:      float          # diameter (C) or length (L/R)
    ww2:      float = 0.0    # slot/pocket height
    ww3:      float = 0.0    # pocket corner radius (R only)
    wheight:  float = 0.0    # Z start depth (negative = into material)
    wdepth:   float = 0.0    # shoulder / total depth
    wcomment: str  = ""

    # Notch-specific fields (NOTCH only)
    wsaw_plane_x:    float = 0.0   # X position along bar from one end
    wsaw_plane_z:    float = 0.0   # Z face position (WSawPlaneZ)
    wsaw_notch_width: float = 0.0  # cutting width/depth of notch
    wp_angle_x:      float = 0.0   # approach angle: <0 = head end, >0 = tail end


@dataclass
class Cut:
    ccomno:   str            # barcode / component number
    cpartno:  str            # part number
    clength:  float          # cut length (mm)
    canglelh: float          # left horizontal angle
    canglerh: float          # right horizontal angle
    canglelv: float          # left vertical angle
    canglerv: float          # right vertical angle
    ccomment: str  = ""      # orientation label: H=Head, S=Sill, L=Left, R=Right
    works:    List[Work] = field(default_factory=list)


@dataclass
class Bar:
    bidentno:    str          # profile number
    bdescription: str         # profile description
    blength:     float        # bar stock length (mm)
    bwidth:      float        # width (Z)
    bheight:     float        # height (Y)
    bsurface:    str  = ""    # extrusion colour code (e.g. "PC-2")
    cuts:        List[Cut] = field(default_factory=list)


@dataclass
class Job:
    job_id:  str
    job_num: str
    date:    str
    bars:    List[Bar] = field(default_factory=list)

    @property
    def bar_count(self) -> int:
        return len(self.bars)

    @property
    def piece_count(self) -> int:
        return sum(len(b.cuts) for b in self.bars)

    @property
    def operation_count(self) -> int:
        return sum(len(c.works) for b in self.bars for c in b.cuts)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _txt(el: Optional[ET.Element], tag: str, default: str = "") -> str:
    child = el.find(tag)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def _flt(el: Optional[ET.Element], tag: str, default: float = 0.0) -> float:
    raw = _txt(el, tag, "")
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


# ── Public API ────────────────────────────────────────────────────────────────

def parse_v6(path: str) -> Job:
    """
    Parse a Softech V6 XML file and return a Job object.
    Raises ValueError if the file does not look like a V6 job file.
    """
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise ValueError(f"XML parse error: {exc}") from exc

    root = tree.getroot()

    if root.tag != "Job":
        raise ValueError(
            f"Not a valid V6 job file — expected root element <Job>, "
            f"got <{root.tag}>."
        )

    job = Job(
        job_id  = _txt(root, "JIdentNo"),
        job_num = _txt(root, "JobNum"),
        date    = _txt(root, "Info2"),
    )

    bars_el = root.find("Bars")
    if bars_el is None:
        raise ValueError("V6 file contains no <Bars> element.")

    for bar_el in bars_el.findall("Bar"):
        bar = Bar(
            bidentno     = _txt(bar_el, "BIdentNo"),
            bdescription = _txt(bar_el, "BDescription"),
            blength      = _flt(bar_el, "BLength"),
            bwidth       = _flt(bar_el, "BWidth"),
            bheight      = _flt(bar_el, "BHeight"),
            bsurface     = _txt(bar_el, "BSurface"),
        )

        cuts_el = bar_el.find("Cuts")
        if cuts_el is not None:
            for cut_el in cuts_el.findall("Cut"):
                cut = Cut(
                    ccomno   = _txt(cut_el, "CComNo"),
                    cpartno  = _txt(cut_el, "CPartNo"),
                    clength  = _flt(cut_el, "CLength"),
                    canglelh = _flt(cut_el, "CAngleLH", 90.0),
                    canglerh = _flt(cut_el, "CAngleRH", 90.0),
                    canglelv = _flt(cut_el, "CAngleLV", 0.0),
                    canglerv = _flt(cut_el, "CAngleRV", 0.0),
                    ccomment = _txt(cut_el, "CComment"),
                )

                works_el = cut_el.find("Works")
                if works_el is not None:
                    for work_el in works_el.findall("Work"):
                        wtype_raw = _txt(work_el, "WType")
                        if not wtype_raw:
                            continue
                        wtype = wtype_raw.upper()

                        if wtype == "NOTCH":
                            # Notch uses completely different fields — blade cut at head/tail
                            wp_angle_x = _flt(work_el, "WPAngleX")
                            work = Work(
                                wtype           = wtype,
                                wside           = 0,
                                wx1             = 0.0,
                                wy1             = 0.0,
                                ww1             = 0.0,
                                wcomment        = _txt(work_el, "WComment"),
                                wsaw_plane_x    = _flt(work_el, "WSawPlaneX"),
                                wsaw_plane_z    = _flt(work_el, "WSawPlaneZ"),
                                wsaw_notch_width= _flt(work_el, "WSawNotchWidth"),
                                wp_angle_x      = wp_angle_x,
                            )
                        else:
                            # C = hole, L = slot, R = rectangular pocket.
                            # WPAngleX is the approach tilt — present on
                            # angled drills (e.g. a slanted "sill drain"),
                            # not just on saw/notch works.  Read it here so
                            # any angled C/L/R machining carries its tilt
                            # downstream (0 for ordinary straight holes).
                            work = Work(
                                wtype    = wtype,
                                wside    = int(_flt(work_el, "WSide", 1)),
                                wx1      = _flt(work_el, "WX1"),
                                wy1      = _flt(work_el, "WY1"),
                                ww1      = _flt(work_el, "WW1"),
                                ww2      = _flt(work_el, "WW2"),
                                ww3      = _flt(work_el, "WW3"),
                                wheight  = _flt(work_el, "WHeight"),
                                wdepth   = _flt(work_el, "WDepth"),
                                wcomment = _txt(work_el, "WComment"),
                                wp_angle_x = _flt(work_el, "WPAngleX"),
                            )
                        cut.works.append(work)

                bar.cuts.append(cut)

        job.bars.append(bar)

    return job
