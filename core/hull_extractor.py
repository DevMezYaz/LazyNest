"""
hull_extractor.py
Reads a DXF file and returns the TRUE outline of the design (the shape the
laser actually cuts), used as the nesting footprint. The whole file is one
rigid unit.

Outline source, in priority order:
  1. Closed loops built from the outer-cut layer ('0.5_Out', or any layer
     whose name contains 'out'). Spline / arc / line segments are chained
     end-to-end (with a small snapping tolerance) into closed loops.
  2. Closed loops built from ALL geometry in the file (outermost region).
  3. Convex hull of all geometry (flagged as quality='hull').

Holes: closed loops on inner-cut layers ('0.5_In' or any layer containing
'in' as a word part like '_In') that lie inside the outline are kept as
polygon holes, so small pieces can optionally be nested inside them.
"""

import re
import logging
from typing import List, Optional

import numpy as np
import shapely
import ezdxf
from ezdxf import path as ezpath
from shapely.geometry import (LineString, Polygon, MultiPolygon,
                              GeometryCollection, box)
from shapely.ops import polygonize, unary_union, snap
from shapely import wkb

log = logging.getLogger(__name__)

FLATTEN_TOL = 0.005    # mm — max deviation when flattening cut outlines
COARSE_TOL  = 0.05     # mm — flattening for non-cut layers (engraving etc.)
SNAP_TOL    = 0.02     # mm — segment ends this close are treated as one point
MAX_BRIDGE  = 2.0      # mm — a cut outline with gaps up to this is still closed
MIN_FACE    = 0.01     # mm² — ignore sliver faces from overlapping curves

OUTER_CANDIDATES = ['0.5_Out', '0.5_out', 'OUT', 'Out', 'out',
                    'CUT_OUT', 'cut_out', 'OUTER', 'outer', 'OUTLINE', 'outline']
_INNER_RE = re.compile(r'(^|[_\-\s.])(in|inner|inside|hole)s?$', re.IGNORECASE)


# ── Entity → polylines ────────────────────────────────────────────────────────
def _entity_paths(entity, tol: float = FLATTEN_TOL) -> List[List[tuple]]:
    """Flatten one entity into point lists (one per sub-path)."""
    t = entity.dxftype()
    out = []
    try:
        if t == 'INSERT':
            for e in entity.virtual_entities():
                out.extend(_entity_paths(e, tol))
            return out
        if t in ('TEXT', 'MTEXT', 'POINT', 'DIMENSION', 'HATCH', 'SOLID',
                 'IMAGE', 'VIEWPORT', 'ATTDEF', 'ATTRIB'):
            return out
        p = ezpath.make_path(entity)
        for sp in p.sub_paths():
            pts = [(float(v.x), float(v.y)) for v in sp.flattening(tol)]
            if len(pts) >= 2:
                out.append(pts)
    except Exception as ex:
        log.debug("Skipping entity %s: %s", t, ex)
    return out


def _bridge_gaps(lines: List[LineString], max_gap: float) -> tuple:
    """
    Some CAD exports leave the cut outline a hair open — a few endpoints with
    no neighbour. Join each loose end to its nearest loose end with a short
    straight bridge. Returns (extra lines, how many, the longest bridge).
    """
    ends, owner = [], []
    for i, ls in enumerate(lines):
        ends.append(ls.coords[0]); owner.append(i)
        ends.append(ls.coords[-1]); owner.append(i)
    if len(ends) < 2:
        return [], 0, 0.0
    E = np.asarray(ends, dtype=float)
    d = np.hypot(E[:, 0][:, None] - E[:, 0][None, :],
                 E[:, 1][:, None] - E[:, 1][None, :])
    np.fill_diagonal(d, np.inf)
    loose = [i for i in range(len(E)) if d[i].min() > SNAP_TOL]
    if len(loose) < 2:
        return [], 0, 0.0

    # only join a loose end to another loose end, and only when the two agree
    # that they are each other's nearest — anything else invents geometry
    sub = d[np.ix_(loose, loose)]
    np.fill_diagonal(sub, np.inf)
    nearest = {loose[k]: loose[int(np.argmin(sub[k]))] for k in range(len(loose))}
    bridges, done, longest = [], set(), 0.0
    for i, j in nearest.items():
        if i in done or j in done or nearest.get(j) != i or owner[i] == owner[j]:
            continue
        gap = float(d[i][j])
        if gap > max_gap:
            continue
        bridges.append(LineString([tuple(E[i]), tuple(E[j])]))
        done.add(i); done.add(j)
        longest = max(longest, gap)
    return bridges, len(bridges), longest


def _loops_to_region(point_lists: List[List[tuple]], max_gap: float = MAX_BRIDGE,
                     info: dict = None):
    """
    Chain open segments into closed loops and return the filled region
    (union of all enclosed faces). Returns None when nothing closes.
    """
    lines = []
    for pts in point_lists:
        ls = LineString(pts)
        if ls.length > 1e-6:
            lines.append(ls)
    if not lines:
        return None

    # Snap segment endpoints onto each other so tiny gaps close
    ends = shapely.multipoints(np.array([c for ls in lines
                                         for c in (ls.coords[0], ls.coords[-1])]))
    snapped = [snap(ls, ends, SNAP_TOL) for ls in lines]

    # Close any single segment whose own ends nearly meet
    fixed = []
    for ls in snapped:
        c = list(ls.coords)
        if len(c) >= 3 and c[0] != c[-1] and \
                LineString([c[0], c[-1]]).length <= SNAP_TOL:
            c[-1] = c[0]
        fixed.append(LineString(c))

    def build(segments):
        faces = [f for f in polygonize(unary_union(segments)) if f.area > MIN_FACE]
        return _clean(unary_union(faces)) if faces else None

    region = build(fixed)
    if max_gap > 0:
        bridges, n, longest = _bridge_gaps(fixed, max_gap)
        if bridges:
            bridged = build(fixed + bridges)
            # keep it only if closing the gaps genuinely recovered more shape
            if bridged is not None and (region is None or
                                        bridged.area > region.area * 1.001):
                log.info("closed %d gap(s) up to %.2f mm in the cut outline", n, longest)
                if info is not None:
                    info["bridges"] = n
                    info["max_gap"] = round(longest, 3)
                return bridged
    return region


def _clean(geom):
    """Make valid and drop holes created by overlapping faces (union fills)."""
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        geom = geom.buffer(0)
    if isinstance(geom, GeometryCollection) and not isinstance(geom, MultiPolygon):
        polys = [g for g in geom.geoms if isinstance(g, Polygon)]
        geom = unary_union(polys) if polys else None
    return geom


def _solid(geom):
    """Fill all holes (outline region without interior cut-outs)."""
    if geom is None:
        return None
    if isinstance(geom, Polygon):
        return Polygon(geom.exterior)
    return unary_union([Polygon(p.exterior) for p in geom.geoms])


def _pick_outer_layer(layers_present) -> Optional[str]:
    for cand in OUTER_CANDIDATES:
        if cand in layers_present:
            return cand
    for lname in sorted(layers_present):
        if 'out' in lname.lower():
            return lname
    return None


def extract_hull(dxf_path: str, margin_mm: float = 0.0) -> dict:
    """
    Main entry point. Returns:
      {
        'polygon':     Shapely Polygon/MultiPolygon — solid outline (holes filled)
        'holes':       Shapely geometry of through-holes inside it, or None
        'bbox':        (min_x, min_y, max_x, max_y)
        'width_mm', 'height_mm', 'area_mm2'
        'layers':      list[str]
        'entities':    int
        'hull_source': str      — layer the outline came from
        'quality':     'exact' | 'all-layers' | 'hull'
        'error':       str | None
      }
    margin_mm is kept for backwards compatibility (outward offset).
    """
    result = {
        'polygon': None, 'holes': None, 'bbox': None,
        'width_mm': 0.0, 'height_mm': 0.0, 'area_mm2': 0.0,
        'layers': [], 'entities': 0, 'hull_source': '',
        'quality': '', 'bridged_gaps': 0, 'max_gap_mm': 0.0, 'error': None,
    }

    try:
        doc = ezdxf.readfile(dxf_path)
    except Exception as ex:
        result['error'] = f"Cannot open DXF: {ex}"
        return result

    msp = doc.modelspace()
    entities = list(msp)
    result['entities'] = len(entities)
    by_layer = {}
    for e in entities:
        by_layer.setdefault(getattr(e.dxf, 'layer', '0'), []).append(e)
    result['layers'] = sorted(by_layer)

    outer = _pick_outer_layer(by_layer)
    all_paths = []
    paths_by_layer = {}
    for lname, ents in by_layer.items():
        cut = lname == outer or bool(_INNER_RE.search(lname)) or outer is None
        tol = FLATTEN_TOL if cut else COARSE_TOL
        pl = []
        for e in ents:
            pl.extend(_entity_paths(e, tol))
        paths_by_layer[lname] = pl
        all_paths.extend(pl)

    region = None
    gap_info: dict = {}
    if outer:
        region = _loops_to_region(paths_by_layer[outer], info=gap_info)
        if region is not None:
            result['hull_source'] = outer
            result['quality'] = 'exact'
            result['bridged_gaps'] = gap_info.get("bridges", 0)
            result['max_gap_mm'] = gap_info.get("max_gap", 0.0)

    if region is None and all_paths:
        region = _loops_to_region(all_paths)
        if region is not None:
            result['hull_source'] = 'all layers'
            result['quality'] = 'all-layers'

    if region is None:
        pts = [p for pl in all_paths for p in pl]
        if len(pts) < 3:
            result['error'] = "No usable geometry found"
            return result
        mp = shapely.multipoints(np.asarray(pts, dtype=float))
        region = mp.convex_hull
        if region.geom_type != 'Polygon':
            region = box(*mp.bounds)
        result['hull_source'] = 'convex hull'
        result['quality'] = 'hull'

    solid = _solid(region)

    # Anything drawn outside the cut outline (rare) must still be kept clear
    if all_paths and result['quality'] != 'hull':
        grown = solid.buffer(0.06)
        xy = np.array([p for pl in all_paths for p in pl], dtype=float)
        if not shapely.contains_xy(grown, xy[:, 0], xy[:, 1]).all():
            stray = []
            for pl in all_paths:
                a = np.asarray(pl, dtype=float)
                if not shapely.contains_xy(grown, a[:, 0], a[:, 1]).all():
                    stray.append(LineString(pl))
            if stray:
                solid = _solid(unary_union([solid] + [st.buffer(0.05) for st in stray]))

    # Through-holes: inner-cut loops + holes inside the outer-layer region
    hole_parts = []
    if isinstance(region, (Polygon, MultiPolygon)):
        for p in ([region] if isinstance(region, Polygon) else region.geoms):
            hole_parts.extend(Polygon(r) for r in p.interiors)
    for lname, pl in paths_by_layer.items():
        if lname != outer and _INNER_RE.search(lname):
            r = _loops_to_region(pl)
            if r is not None:
                hole_parts.append(_solid(r))
    holes = None
    if hole_parts:
        holes = unary_union(hole_parts).intersection(solid)
        if holes.is_empty or holes.area < MIN_FACE:
            holes = None

    if margin_mm > 0:
        solid = solid.buffer(margin_mm, join_style=2)

    b = solid.bounds
    result['polygon']   = solid
    result['holes']     = holes
    result['bbox']      = b
    result['width_mm']  = round(b[2] - b[0], 4)
    result['height_mm'] = round(b[3] - b[1], 4)
    result['area_mm2']  = round(solid.area, 4)
    return result


# ── Serialisation helpers (library cache) ─────────────────────────────────────
def geom_to_hex(geom) -> str:
    return wkb.dumps(geom, hex=True) if geom is not None else ""


def geom_from_hex(s: str):
    if not s:
        return None
    try:
        return wkb.loads(s, hex=True)
    except Exception:
        return None
