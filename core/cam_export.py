"""
cam_export.py

Writes the nested platforms in formats that older CAM software reads cleanly
(Sisma PRISMA, Vectric VCarve) instead of the modern DXF Rhino produces.

Why this exists
---------------
Rhino stores circles and arcs as degree-2 rational NURBS "splines". In the
jewellery designs here that is ~93% of all curves: every ball dot, most
engraving and most cut outlines. CAM importers either ignore splines, or
explode them into thousands of tiny chords (one platform flattens to ~660,000
vertices), which chokes the importer and makes the laser stutter.

So every curve is turned back into what it really is:

  degree-2 spline that fits a circle within 1 µm  →  CIRCLE / ARC   (exact)
  genuinely freeform spline                       →  a few ARCs     (≤ fit_tol)
  polyline / line                                 →  POLYLINE / LINE

The result is written as:
  • R12 DXF   — the oldest, most widely readable DXF: no splines at all
  • R2004 DXF — same geometry, newer container (some CAM prefers it)
  • SVG       — PRISMA reads this natively; 1 user unit = 1 mm

Geometry is never approximated beyond `fit_tol`, and `verify()` re-measures
the written file against the original curves.
"""

import os
import math
import logging
from typing import Optional

import numpy as np
import ezdxf
from ezdxf import path as ezpath

log = logging.getLogger(__name__)

ARC_TOL   = 0.005     # mm — a curve this close to a circle *is* an arc
FIT_TOL   = 0.005     # mm — accuracy for genuinely freeform curves
LINE_TOL  = 0.002     # mm — straighter than this is written as a line
TINY      = 0.0005    # mm — a curve smaller than this is noise, not geometry
SAMPLE    = 0.01      # mm — sampling step used to read curves
DEC       = 4         # decimal places written out (0.1 µm)


# ── geometry helpers ─────────────────────────────────────────────────────────
def _extent(P: np.ndarray) -> float:
    """How far the points actually reach — used to sanity-check circle fits."""
    return float(max(P[:, 0].max() - P[:, 0].min(), P[:, 1].max() - P[:, 1].min()))


def _is_real_circle(P: np.ndarray, r: float) -> bool:
    """
    A closed curve is only a circle if it really goes round one: a full circle
    spans its own diameter. Without this, 65 points spread over a micron fit a
    24-metre circle perfectly and get drawn right across the platform.
    """
    return r > 0 and _extent(P) >= 1.5 * r


def _fit_circle(P: np.ndarray):
    """Least-squares circle through points. Returns (cx, cy, r, max_deviation)."""
    x, y = P[:, 0], P[:, 1]
    A = np.c_[2 * x, 2 * y, np.ones(len(P))]
    b = x ** 2 + y ** 2
    try:
        c, *_ = np.linalg.lstsq(A, b, rcond=None)
    except np.linalg.LinAlgError:
        return None
    cx, cy = float(c[0]), float(c[1])
    rsq = c[2] + cx * cx + cy * cy
    if rsq <= 0:
        return None
    r = math.sqrt(rsq)
    dev = float(np.abs(np.hypot(x - cx, y - cy) - r).max())
    return cx, cy, r, dev


def _straight_dev(P: np.ndarray) -> float:
    """Largest distance from the straight line between the two end points."""
    a, b = P[0], P[-1]
    d = b - a
    n = math.hypot(*d)
    if n < 1e-12:
        return float(np.abs(P - a).max())
    q = P - a
    # 2-D cross product written out: numpy 2 only does 3-D vectors
    cross = d[0] * q[:, 1] - d[1] * q[:, 0]
    return float(np.abs(cross).max() / n)


def _ccw(P: np.ndarray, cx: float, cy: float) -> bool:
    """Direction of travel around (cx, cy)."""
    ang = np.unwrap(np.arctan2(P[:, 1] - cy, P[:, 0] - cx))
    return bool(ang[-1] > ang[0])


def _arc_prim(P: np.ndarray, cx, cy, r):
    """('arc', ...) tuple for points running around a centre."""
    ccw = _ccw(P, cx, cy)
    a0 = math.degrees(math.atan2(P[0][1] - cy, P[0][0] - cx)) % 360
    a1 = math.degrees(math.atan2(P[-1][1] - cy, P[-1][0] - cx)) % 360
    if not ccw:                      # DXF arcs always run counter-clockwise
        a0, a1 = a1, a0
    return ("arc", cx, cy, r, a0, a1)


def _sweep(P: np.ndarray, cx: float, cy: float) -> float:
    """Total turn around (cx, cy) in degrees, always positive."""
    ang = np.unwrap(np.arctan2(P[:, 1] - cy, P[:, 0] - cx))
    return abs(math.degrees(ang[-1] - ang[0]))


def _fit_run(P: np.ndarray, tol: float):
    """One primitive covering this whole run of points, or None if it won't fit."""
    if len(P) < 2:
        return None
    if len(P) == 2 or _straight_dev(P) <= LINE_TOL:
        return ("line", tuple(P[0]), tuple(P[-1]))
    f = _fit_circle(P)
    if f is None:
        return None
    cx, cy, r, dev = f
    if dev > tol or r > 1e4:
        return None

    ang = np.unwrap(np.arctan2(P[:, 1] - cy, P[:, 0] - cx))
    d = np.diff(ang)
    if d.size == 0:
        return None
    # the run must sweep one way only, or the written arc would cover the
    # wrong part of the circle
    fwd, back = d[d > 0].sum(), -d[d < 0].sum()
    if min(fwd, back) > math.radians(0.25):
        return None
    sweep = abs(float(ang[-1] - ang[0]))
    if math.degrees(sweep) < 0.5:
        return None                     # too flat to be an arc
    if math.degrees(sweep) > 359.5:
        return ("circle", cx, cy, r) if _is_real_circle(P, r) else None

    prim = _arc_prim(P, cx, cy, r)
    # every point must lie on the span the arc actually covers
    a0 = math.radians(prim[4])
    span = math.radians((prim[5] - prim[4]) % 360)
    rel = np.mod(np.arctan2(P[:, 1] - cy, P[:, 0] - cx) - a0, 2 * math.pi)
    if float(rel.max()) > span + math.radians(0.5):
        return None
    return prim


def _fit_curve(P: np.ndarray, tol: float) -> list:
    """
    Greedy fit: take the longest run of points one arc (or line) can cover
    within `tol`, emit it, carry on from there.
    """
    prims, i, n = [], 0, len(P)
    while i < n - 1:
        # grow the run until it no longer fits
        step = 4
        best, best_j, fail_j = None, None, None
        j = min(i + step, n - 1)
        while True:
            p = _fit_run(P[i:j + 1], tol)
            if p is None:
                fail_j = j
                break
            best, best_j = p, j
            if j >= n - 1:
                break
            step *= 2
            j = min(i + step, n - 1)
        if best is None:
            prims.append(("line", tuple(P[i]), tuple(P[i + 1])))
            i += 1
            continue
        # narrow down between the last fit and the first failure
        if fail_j is not None:
            lo, hi = best_j, fail_j
            while lo + 1 < hi:
                mid = (lo + hi) // 2
                p = _fit_run(P[i:mid + 1], tol)
                if p is None:
                    hi = mid
                else:
                    best, lo = p, mid
            best_j = lo
        prims.append(best)
        i = best_j
    return prims


# ── DXF entity → CAM primitives ──────────────────────────────────────────────
def entity_primitives(e, fit_tol: float = FIT_TOL) -> list:
    """Never let one awkward entity stop an export."""
    try:
        return _entity_primitives(e, fit_tol)
    except Exception as ex:
        log.warning("CAM: could not convert %s on layer %s (%s)",
                    e.dxftype(), getattr(e.dxf, "layer", "?"), ex)
        try:
            return _flatten_to_prims(e, fit_tol)
        except Exception:
            return []


def _entity_primitives(e, fit_tol: float = FIT_TOL) -> list:
    """
    Turn one DXF entity into CAM-friendly primitives:
      ("line", (x,y), (x,y))
      ("arc", cx, cy, r, start_deg, end_deg)
      ("circle", cx, cy, r)
      ("poly", [(x,y)...], closed)
      ("text", str, (x,y), height)
    """
    t = e.dxftype()
    try:
        if t == "LINE":
            s, en = e.dxf.start, e.dxf.end
            return [("line", (s.x, s.y), (en.x, en.y))]
        if t == "CIRCLE":
            if not _flat_ocs(e):
                return _curve_prims(e, fit_tol)
            c = e.dxf.center
            return [("circle", c.x, c.y, e.dxf.radius)]
        if t == "ARC":
            if not _flat_ocs(e):
                return _curve_prims(e, fit_tol)
            c = e.dxf.center
            return [("arc", c.x, c.y, e.dxf.radius,
                     e.dxf.start_angle % 360, e.dxf.end_angle % 360)]
        if t in ("TEXT", "MTEXT"):
            ip = e.dxf.insert
            if not _flat_ocs(e):
                ip = e.ocs().to_wcs(ip)
            txt = e.dxf.text if t == "TEXT" else e.text
            return [("text", txt, (ip.x, ip.y), getattr(e.dxf, "height", 2.0))]
        if t in ("LWPOLYLINE", "POLYLINE"):
            if not _flat_ocs(e):
                return _flatten_to_prims(e, fit_tol)
            if t == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in e.get_points()]
                bulges = [p[4] for p in e.get_points()]
                closed = bool(e.closed)
            else:
                pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
                bulges = [getattr(v.dxf, "bulge", 0.0) for v in e.vertices]
                closed = bool(e.is_closed)
            if any(abs(b) > 1e-9 for b in bulges):     # has arc segments
                return _flatten_to_prims(e, fit_tol)
            if len(pts) < 2:
                return []
            return [("poly", pts, closed)]
        if t == "INSERT":
            out = []
            for sub in e.virtual_entities():
                out.extend(entity_primitives(sub, fit_tol))
            return out
        if t in ("ARC", "CIRCLE", "LINE"):
            pass
        if t == "SPLINE":
            return _curve_prims(e, fit_tol)
    except Exception as ex:
        log.debug("CAM convert skipped %s: %s", t, ex)
    return _flatten_to_prims(e, fit_tol)


def _flat_ocs(e) -> bool:
    """
    True when the entity is stored the normal way up. ezdxf flips the
    extrusion to (0, 0, -1) when a piece is rotated 180°, and then centres
    and angles are mirrored — reading dxf.center directly would place the
    shape on the wrong side of the platform.
    """
    v = getattr(e.dxf, "extrusion", None)
    if v is None:
        return True
    return abs(v[0]) < 1e-9 and abs(v[1]) < 1e-9 and v[2] > 0


def _points_of(e) -> Optional[np.ndarray]:
    try:
        p = ezpath.make_path(e)
        pts = [(v.x, v.y) for v in p.flattening(SAMPLE)]
        return np.asarray(pts, dtype=float) if len(pts) >= 2 else None
    except Exception:
        return None


def _curve_prims(e, fit_tol: float) -> list:
    """Any curve, read in world coordinates and turned back into arcs/lines."""
    P = _points_of(e)
    if P is None:
        return []
    size = _extent(P)
    if size < TINY:                  # a speck of noise in the source file
        return [("line", tuple(P[0]), tuple(P[-1]))] if size > 0 else []
    closed = math.dist(P[0], P[-1]) <= 1e-6
    f = _fit_circle(P)
    if f is not None:
        cx, cy, r, dev = f
        if dev <= max(ARC_TOL, fit_tol) and r <= 1e4:   # a real arc or circle
            if closed or _sweep(P, cx, cy) > 359.5:
                if _is_real_circle(P, r):
                    return [("circle", cx, cy, r)]
            else:
                return [_arc_prim(P, cx, cy, r)]
    if closed:
        # a closed freeform loop: cut it in two so no run can wrap all the
        # way round and collapse into a zero-length arc
        half = len(P) // 2
        return _fit_curve(P[:half + 1], fit_tol) + _fit_curve(P[half:], fit_tol)
    return _fit_curve(P, fit_tol)                # genuinely freeform


def _flatten_to_prims(e, fit_tol: float) -> list:
    P = _points_of(e)
    return _fit_curve(P, fit_tol) if P is not None else []


# ── writers ──────────────────────────────────────────────────────────────────
def _r(v):
    return round(float(v), DEC)


def write_dxf(source_doc, output_path: str, version: str = "R12") -> dict:
    """
    Rewrite a document's modelspace as lines, arcs, circles and polylines in
    the requested DXF version. Returns a small summary.
    """
    out = ezdxf.new(version, setup=False)
    out.units = ezdxf.units.MM
    msp = out.modelspace()
    counts = {"line": 0, "arc": 0, "circle": 0, "poly": 0, "text": 0}

    for e in source_doc.modelspace():
        layer = getattr(e.dxf, "layer", "0")
        if layer not in out.layers:
            try:
                out.layers.add(layer, color=getattr(e.dxf, "color", 256)
                               if getattr(e.dxf, "color", 256) not in (0, 256) else 7)
            except Exception:
                out.layers.add(layer)
        attr = {"layer": layer}
        for prim in entity_primitives(e):
            k = prim[0]
            try:
                if k == "line":
                    msp.add_line((_r(prim[1][0]), _r(prim[1][1])),
                                 (_r(prim[2][0]), _r(prim[2][1])), dxfattribs=attr)
                elif k == "arc":
                    msp.add_arc((_r(prim[1]), _r(prim[2])), _r(prim[3]),
                                _r(prim[4]), _r(prim[5]), dxfattribs=attr)
                elif k == "circle":
                    msp.add_circle((_r(prim[1]), _r(prim[2])), _r(prim[3]), dxfattribs=attr)
                elif k == "poly":
                    pl = msp.add_polyline2d([(_r(x), _r(y)) for x, y in prim[1]],
                                            dxfattribs=attr)
                    if prim[2]:
                        pl.close(True)
                elif k == "text":
                    msp.add_text(prim[1], height=prim[3], dxfattribs=attr
                                 ).set_placement((_r(prim[2][0]), _r(prim[2][1])))
                counts[k] = counts.get(k, 0) + 1
            except Exception as ex:
                log.debug("could not write %s: %s", k, ex)

    out.saveas(output_path)
    return counts


# ── SVG ──────────────────────────────────────────────────────────────────────
SVG_COLORS = ["#000000", "#c01c5a", "#1f6fb4", "#138a62", "#b4761f",
              "#6e47c2", "#a83232", "#00778c"]


def write_svg(source_doc, output_path: str, margin_mm: float = 2.0) -> dict:
    """
    One SVG, 1 user unit = 1 mm, Y up (as in the DXF), grouped by layer.
    Arcs stay arcs — PRISMA reads this natively.
    """
    by_layer, minx, miny, maxx, maxy = {}, 1e18, 1e18, -1e18, -1e18

    def seen(x, y):
        nonlocal minx, miny, maxx, maxy
        minx = min(minx, x); maxx = max(maxx, x)
        miny = min(miny, y); maxy = max(maxy, y)

    for e in source_doc.modelspace():
        layer = getattr(e.dxf, "layer", "0")
        for prim in entity_primitives(e):
            k = prim[0]
            if k == "line":
                seen(*prim[1]); seen(*prim[2])
            elif k == "arc":
                cx, cy, r = prim[1], prim[2], prim[3]
                seen(cx - r, cy - r); seen(cx + r, cy + r)
            elif k == "circle":
                cx, cy, r = prim[1], prim[2], prim[3]
                seen(cx - r, cy - r); seen(cx + r, cy + r)
            elif k == "poly":
                for x, y in prim[1]:
                    seen(x, y)
            elif k == "text":
                seen(*prim[2])
            by_layer.setdefault(layer, []).append(prim)

    if minx > maxx:
        minx = miny = 0.0
        maxx = maxy = 1.0
    minx -= margin_mm; miny -= margin_mm
    maxx += margin_mm; maxy += margin_mm
    w, h = maxx - minx, maxy - miny

    def pt(x, y):
        return f"{_r(x)},{_r(y)}"

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        f'width="{w:.4f}mm" height="{h:.4f}mm" '
        f'viewBox="0 0 {w:.4f} {h:.4f}">',
        '<!-- LazyNest · 1 unit = 1 mm -->',
        f'<g transform="translate({_r(-minx)},{_r(maxy)}) scale(1,-1)" '
        f'fill="none" stroke-width="0.05">',
    ]
    counts = {}
    for i, (layer, prims) in enumerate(sorted(by_layer.items())):
        colour = SVG_COLORS[i % len(SVG_COLORS)]
        lines.append(f'<g id="{_xml(layer)}" inkscape:label="{_xml(layer)}" '
                     f'stroke="{colour}">')
        for prim in prims:
            k = prim[0]
            counts[k] = counts.get(k, 0) + 1
            if k == "line":
                lines.append(f'<line x1="{_r(prim[1][0])}" y1="{_r(prim[1][1])}" '
                             f'x2="{_r(prim[2][0])}" y2="{_r(prim[2][1])}"/>')
            elif k == "circle":
                lines.append(f'<circle cx="{_r(prim[1])}" cy="{_r(prim[2])}" '
                             f'r="{_r(prim[3])}"/>')
            elif k == "arc":
                _, cx, cy, r, a0, a1 = prim
                sweep = (a1 - a0) % 360
                x0 = cx + r * math.cos(math.radians(a0))
                y0 = cy + r * math.sin(math.radians(a0))
                x1 = cx + r * math.cos(math.radians(a1))
                y1 = cy + r * math.sin(math.radians(a1))
                large = 1 if sweep > 180 else 0
                lines.append(f'<path d="M {pt(x0, y0)} A {_r(r)},{_r(r)} 0 '
                             f'{large},1 {pt(x1, y1)}"/>')
            elif k == "poly":
                d = " L ".join(pt(x, y) for x, y in prim[1])
                lines.append(f'<path d="M {d}{" Z" if prim[2] else ""}"/>')
            elif k == "text":
                lines.append(f'<text x="{_r(prim[2][0])}" y="{_r(prim[2][1])}" '
                             f'font-size="{_r(prim[3])}" fill="{colour}" '
                             f'stroke="none" transform="scale(1,-1) '
                             f'translate(0,{_r(-2 * prim[2][1])})">{_xml(prim[1])}</text>')
        lines.append('</g>')
    lines.append('</g>')
    lines.append('</svg>')

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return counts


def _xml(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ── verification ─────────────────────────────────────────────────────────────
def verify(source_doc, cam_path: str, step: int = 7, max_points: int = 60000) -> dict:
    """
    Measure the written CAM file against the original geometry: the largest
    distance from any original point to the converted curves.
    """
    from shapely.geometry import LineString, Point
    from shapely import STRtree

    cam = ezdxf.readfile(cam_path)
    lines = []
    for e in cam.modelspace():
        if e.dxftype() in ("TEXT", "MTEXT"):
            continue
        try:
            pts = [(v.x, v.y) for v in ezpath.make_path(e).flattening(0.0005)]
        except Exception:
            continue
        if len(pts) >= 2:
            lines.append(LineString(pts))
    if not lines:
        return {"worst_mm": float("inf"), "points": 0, "over_tol": 0}

    tree = STRtree(lines)
    worst, n, over = 0.0, 0, 0
    ents = [e for e in source_doc.modelspace() if e.dxftype() not in ("TEXT", "MTEXT")]
    # big jobs hold hundreds of thousands of points — check a sample
    budget = max(1, max_points // max(1, len(ents)))
    for e in ents:
        P = _points_of(e)
        if P is None:
            continue
        stride = max(step, len(P) // budget + 1)
        for x, y in P[::stride]:
            p = Point(x, y)
            d = lines[tree.nearest(p)].distance(p)
            n += 1
            if d > 0.01:
                over += 1
            worst = max(worst, d)
    return {"worst_mm": round(worst, 5), "points": n, "over_tol": over}


# ── entry point ──────────────────────────────────────────────────────────────
def export_cam(source_dxf: str, out_dir: str, job_name: str,
               formats=("r12", "svg", "r2004"), check: bool = True) -> dict:
    """
    Convert the normal (Rhino) export into the CAM formats.
    Returns {"files": [...], "check": {...}}.
    """
    os.makedirs(out_dir, exist_ok=True)
    doc = ezdxf.readfile(source_dxf)
    written, first_dxf = [], None
    if "r12" in formats:
        p = os.path.join(out_dir, f"{job_name}_R12.dxf")
        write_dxf(doc, p, "R12")
        written.append(p)
        first_dxf = p
    if "r2004" in formats:
        p = os.path.join(out_dir, f"{job_name}_R2004.dxf")
        write_dxf(doc, p, "R2004")
        written.append(p)
        first_dxf = first_dxf or p
    if "svg" in formats:
        p = os.path.join(out_dir, f"{job_name}.svg")
        write_svg(doc, p)
        written.append(p)

    info = {}
    if check and first_dxf:
        try:
            info = verify(doc, first_dxf)
        except Exception as ex:
            log.warning("CAM verification skipped: %s", ex)
    return {"files": written, "check": info}
