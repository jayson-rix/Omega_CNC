"""DXF geometry extraction for the desktop profile viewer.

The viewer intentionally treats drawing coordinates as millimetres.  ezdxf
handles DXF structure, block expansion, OCS transforms, bulges, splines,
ellipses, and the other curve types; this module flattens that geometry into
short 2D line segments that Tk can draw crisply at any zoom level.
"""

from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Iterable

import ezdxf
from ezdxf import disassemble, recover


Primitive = tuple[str, tuple[float, ...]]
MAX_FLATTENING_DISTANCE_MM = 0.05


def _line(a, b) -> Primitive | None:
    x1, y1 = float(a.x), float(a.y)
    x2, y2 = float(b.x), float(b.y)
    if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
        return None
    if math.hypot(x2 - x1, y2 - y1) <= 1e-9:
        return None
    return ("line", (x1, y1, x2, y2))


def _mesh_lines(mesh) -> Iterable[Primitive]:
    """Yield the visible edges of mesh-based DXF entities."""
    seen: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    for face in mesh.faces_as_vertices():
        if len(face) < 2:
            continue
        for index, start in enumerate(face):
            end = face[(index + 1) % len(face)]
            a = (round(float(start.x), 9), round(float(start.y), 9))
            b = (round(float(end.x), 9), round(float(end.y), 9))
            edge = tuple(sorted((a, b)))
            if edge in seen:
                continue
            seen.add(edge)
            segment = _line(start, end)
            if segment is not None:
                yield segment


def primitives_from_document(document) -> list[Primitive]:
    """Flatten all model-space geometry into 2D line segments.

    ``recursive_decompose`` resolves INSERT/BLOCK and DIMENSION virtual
    entities. ``to_primitives`` supports the broad ezdxf entity set, including
    LINE, (LW)POLYLINE with bulges, ARC, CIRCLE, ELLIPSE, SPLINE, SOLID,
    TRACE, 3DFACE, MESH, and polyface/polygon meshes.
    """
    result: list[Primitive] = []
    entities = disassemble.recursive_decompose(document.modelspace())
    for primitive in disassemble.to_primitives(
        entities,
        max_flattening_distance=MAX_FLATTENING_DISTANCE_MM,
    ):
        mesh = primitive.mesh
        if mesh is not None:
            result.extend(_mesh_lines(mesh))
            continue
        vertices = list(primitive.vertices())
        for start, end in zip(vertices, vertices[1:]):
            segment = _line(start, end)
            if segment is not None:
                result.append(segment)
    return result


def parse_dxf_text(text: str) -> list[Primitive]:
    """Parse an ASCII DXF string; used by focused regression tests."""
    document = ezdxf.read(io.StringIO(text))
    return primitives_from_document(document)


def load_dxf_file(path: str | Path) -> list[Primitive]:
    """Load an ASCII or binary DXF file and return drawable model geometry."""
    source = str(path)
    try:
        document = ezdxf.readfile(source)
    except ezdxf.DXFStructureError:
        document, _auditor = recover.readfile(source)
    return primitives_from_document(document)


def primitive_bounds(
    primitives: Iterable[Primitive],
) -> tuple[float, float, float, float] | None:
    """Return exact bounds for the flattened geometry."""
    points: list[tuple[float, float]] = []
    for kind, values in primitives:
        if kind == "line":
            points.extend((
                (values[0], values[1]),
                (values[2], values[3]),
            ))
    if not points:
        return None
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def fit_scale(
    bounds: tuple[float, float, float, float],
    canvas_width: float,
    canvas_height: float,
    padding: float = 30.0,
) -> float:
    """Scale any drawing bounds to the available canvas without cropping."""
    x0, y0, x1, y1 = bounds
    width = max(x1 - x0, 0.001)
    height = max(y1 - y0, 0.001)
    usable_width = max(float(canvas_width) - padding, 1.0)
    usable_height = max(float(canvas_height) - padding, 1.0)
    return max(0.001, min(usable_width / width, usable_height / height))
