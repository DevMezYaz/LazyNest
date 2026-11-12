"""
nesting_engine.py  —  LazyNest v4 (true-shape nesting)

How it works
  • Every design is its TRUE outline (see hull_extractor). Nothing is boxed.
  • Margin = minimum distance between the outlines of two pieces. Each piece
    is offset outward by margin/2, so two offset shapes that touch are
    exactly `margin` apart.
  • No-Fit Polygons (NFP) give, for a pair of shapes, every position where
    they would collide. They are built from convex parts of each outline
    (Minkowski sums of convex pieces are cheap) and cached per design pair
    and relative rotation, so repeated designs cost nothing extra.
  • For each piece we compute the feasible region
        platform-fit rectangle  −  ∪ NFP(placed pieces)
    and choose the best vertex of that region (tightest, gravity-packed),
    trying every allowed rotation.
  • An order search (priority-tier aware) runs for a user-chosen time budget
    and keeps the best layout (fewest platforms, then most compact).
  • Groups are placed as sets: every member of a set lands on one platform.
  • Overflow lets pieces hang over the platform edges by N mm on EACH side
    (left and right, bottom and top); it is heavily penalised so it is only
    used when it actually saves space.
  • Final layouts are validated with exact geometry.
"""

import math
import time
from collections import OrderedDict
import random
import logging
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Callable

import numpy as np
import pyclipper
import shapely
from shapely.geometry import Polygon, MultiPolygon, box
from shapely.affinity import translate, rotate as sh_rotate
from shapely.ops import unary_union

log = logging.getLogger(__name__)

SCALE         = 1000     # clipper integer units per mm (1 µm)
SIMPLIFY_TOL  = 0.01     # mm — outline simplification (kept conservative)
CONVEX_TOL    = 0.02     # mm — max concavity depth absorbed into a convex part
MAX_PARTS     = 20       # convex parts per design — collision cost grows with
                         # the SQUARE of this, so very wiggly outlines (prongs,
                         # scallops) are merged more coarsely instead of
                         # grinding the nester to a halt
MERGE_STEPS   = (CONVEX_TOL, 0.05, 0.1, 0.2, 0.35, 0.5, 0.8, 1.2)
VALIDATE_TOL  = 0.01     # mm — tolerance for the final exact check
OVERFLOW_PEN  = 1e7      # score penalty per mm of overflow used

QUALITY_SECONDS = {"quick": 5.0, "balanced": 30.0, "thorough": 120.0}


# ── Public data structures ───────────────────────────────────────────────────
@dataclass
class NestItem:
    design_id:   str
    design_name: str
    polygon:     Polygon          # true shape (outline minus through-holes),
                                  # centred on the outline centroid, unrotated
    rotation:    float = 0.0
    placed:      bool  = False
    x:           float = 0.0      # platform position of the outline centroid
    y:           float = 0.0
    platform:    int   = -1
    hull_cx:     float = 0.0      # centroid of the outline in source DXF coordinates
    hull_cy:     float = 0.0
    group_name:  str   = ""
    set_index:   int   = -1

    @property
    def placed_polygon(self) -> Polygon:
        p = sh_rotate(self.polygon, self.rotation, origin=(0, 0)) \
            if self.rotation else self.polygon
        return translate(p, self.x, self.y)

    @property
    def area(self) -> float:
        return self.polygon.area


class NestResults(list):
    """List of PlacementResult plus the pieces that could not be placed."""
    def __init__(self, *a):
        super().__init__(*a)
        self.unplaced: List[str] = []     # design names, one entry per piece
        self.layouts_tried: int = 0
        self.seconds: float = 0.0


@dataclass
class PlacementResult:
    platform_index: int
    placements:     List[NestItem]
    platform_w:     float
    platform_h:     float
    efficiency:     float          # % of nominal platform area covered by outlines
    piece_count:    int
    used_area:      float = 0.0    # mm² of metal (outlines minus holes)
    overflow_x:     float = 0.0    # mm actually used past the left/right edges
    overflow_y:     float = 0.0    # mm actually used past the bottom/top edges


@dataclass
class NestRequest:
    design_id:   str
    design_name: str
    polygon:     Polygon                 # solid outline (source coordinates)
    quantity:    int
    priority:    int = 0
    holes:       Optional[object] = None # through-holes geometry (source coords)
    sym_order:   int = 1                 # shape repeats every 360/sym_order degrees


@dataclass
class GroupRequest:
    name:     str
    members:  List[NestRequest]          # quantity on each member = count per set
    sets:     int
    priority: int = 0


@dataclass
class NestingJob:
    # Legacy form: (design_id, design_name, polygon, qty). Converted to requests.
    items:          List[Tuple] = field(default_factory=list)
    platform_w:     float = 110.0
    platform_h:     float = 52.0
    margin_mm:      float = 0.6
    rotations:      List[float] = field(default_factory=lambda: [0.0, 180.0])
    requests:       List[NestRequest]  = field(default_factory=list)
    groups:         List[GroupRequest] = field(default_factory=list)
    edge_clearance: float = 0.0
    overflow_x:     float = 0.0          # allowed spill on EACH side, left+right (mm)
    overflow_y:     float = 0.0          # allowed spill on EACH side, bottom+top (mm)
    allow_holes:    bool  = False
    time_limit:     float = 5.0          # seconds of optimisation

    def all_requests(self) -> List[NestRequest]:
        reqs = list(self.requests)
        for it in self.items:
            did, name, poly, qty = it[:4]
            reqs.append(NestRequest(did, name, poly, qty))
        return reqs


# ── Clipper helpers ──────────────────────────────────────────────────────────
def _ring_to_int(coords) -> np.ndarray:
    return np.round(np.asarray(coords, dtype=float) * SCALE).astype(np.int64)


def _clip(subj, clip, op, fill=pyclipper.PFT_NONZERO):
    pc = pyclipper.Pyclipper()
    if subj:
        pc.AddPaths([p.tolist() if isinstance(p, np.ndarray) else p for p in subj],
                    pyclipper.PT_SUBJECT, True)
    if clip:
        pc.AddPaths([p.tolist() if isinstance(p, np.ndarray) else p for p in clip],
                    pyclipper.PT_CLIP, True)
    try:
        res = pc.Execute(op, fill, fill)
    except pyclipper.ClipperException:
        return []
    return [np.asarray(r, dtype=np.int64) for r in res if len(r) >= 3]


def _union(paths):
    return _clip(paths, None, pyclipper.CT_UNION)


def _rot_int(paths, deg):
    if deg % 360 == 0:
        return paths
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    if abs(c) < 1e-12: c = 0.0
    if abs(s) < 1e-12: s = 0.0
    m = np.array([[c, s], [-s, c]])
    return [np.round(p @ m).astype(np.int64) for p in paths]


# ── Exact convex hull / Minkowski (GEOS convex_hull is unreliable on thin,
#    nearly-collinear point sets, so we do these ourselves) ──────────────────
def _hull(points) -> np.ndarray:
    """Convex hull (Andrew's monotone chain), CCW, no repeated end point."""
    P = np.unique(np.asarray(points, dtype=float), axis=0)
    if len(P) < 3:
        return P
    P = P[np.lexsort((P[:, 1], P[:, 0]))]

    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2:
                o, a = out[-2], out[-1]
                if (a[0] - o[0]) * (p[1] - o[1]) - (a[1] - o[1]) * (p[0] - o[0]) <= 0:
                    out.pop()
                else:
                    break
            out.append(p)
        return out

    pl = [tuple(p) for p in P]
    lower = half(pl)
    upper = half(reversed(pl))
    return np.array(lower[:-1] + upper[:-1], dtype=float)


def _poly_area(r: np.ndarray) -> float:
    x, y = r[:, 0], r[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


# ── Convex decomposition (Hertel–Mehlhorn style, with concavity tolerance) ───
def _decompose(poly: Polygon, tol: float = None) -> List[np.ndarray]:
    """Split a polygon (holes allowed) into near-convex parts (float arrays)."""
    tol = CONVEX_TOL if tol is None else tol
    tris = [t for t in shapely.constrained_delaunay_triangles(poly).geoms
            if t.area > 1e-10]
    geoms = {i: t for i, t in enumerate(tris)}
    owner: Dict[tuple, list] = {}
    for i, t in geoms.items():
        c = [(round(x, 9), round(y, 9)) for x, y in t.exterior.coords[:-1]]
        for k in range(3):
            a, b = c[k], c[(k + 1) % 3]
            owner.setdefault((a, b) if a < b else (b, a), []).append(i)
    diags = [(e, o) for e, o in owner.items() if len(o) == 2]
    diags.sort(key=lambda d: -((d[0][0][0] - d[0][1][0]) ** 2 +
                               (d[0][0][1] - d[0][1][1]) ** 2))
    parent = {i: i for i in geoms}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    changed = True
    while changed:
        changed = False
        for _, (a, b) in diags:
            ra, rb = find(a), find(b)
            if ra == rb:
                continue
            u = geoms[ra].union(geoms[rb])
            if u.geom_type != 'Polygon':
                continue
            h = _hull(u.exterior.coords[:-1])
            if _poly_area(h) - u.area < 1e-9 or \
               Polygon(h).exterior.hausdorff_distance(u.exterior) <= tol:
                parent[rb] = ra
                geoms[ra] = u
                del geoms[rb]
                changed = True
    return [_hull(g.exterior.coords[:-1]) for g in geoms.values()]


def _decompose_capped(geom, max_parts: int = MAX_PARTS) -> tuple:
    """
    Convex parts for a (Multi)Polygon, merged more coarsely when a shape is so
    wiggly that it would otherwise explode the collision cost. Parts only ever
    grow outward, so pieces can end up a touch further apart — never closer.
    Returns (parts, tolerance used).
    """
    polys = [geom] if isinstance(geom, Polygon) else \
            [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]
    polys = [g for g in polys if not g.is_empty and g.area > 1e-9]
    if not polys:
        return [], CONVEX_TOL
    share = max(6, max_parts // len(polys))
    best, used = None, CONVEX_TOL
    for tol in MERGE_STEPS:
        parts = []
        over = False
        for g in polys:
            p = _decompose(g, tol)
            parts.extend(p)
            if len(p) > share:
                over = True
        best, used = parts, tol
        if not over and len(parts) <= max_parts:
            break
    if used > CONVEX_TOL:
        log.info("outline merged at %.2f mm → %d convex parts", used, len(best))
    return best, used


def _rot_parts(parts, deg):
    if deg % 360 == 0:
        return parts
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    m = np.array([[c, s], [-s, c]])
    return [p @ m for p in parts]


class _Prepared:
    """Convex parts packed for vectorised Minkowski sums."""
    __slots__ = ("p0", "edges", "ang", "start", "length", "n")

    def __init__(self, parts: List[np.ndarray]):
        p0s, edges, angs, lens = [], [], [], []
        for p in parts:
            if len(p) < 3:
                continue
            i = int(np.lexsort((p[:, 0], p[:, 1]))[0])       # lowest y, then x
            q = np.roll(p, -i, axis=0)
            e = np.roll(q, -1, axis=0) - q
            p0s.append(q[0])
            edges.append(e)
            angs.append(np.mod(np.arctan2(e[:, 1], e[:, 0]), 2 * np.pi))
            lens.append(len(q))
        self.n = len(p0s)
        self.p0 = np.array(p0s, dtype=float).reshape(-1, 2)
        self.edges = np.concatenate(edges) if edges else np.zeros((0, 2))
        self.ang = np.concatenate(angs) if angs else np.zeros(0)
        self.length = np.array(lens, dtype=np.int64)
        self.start = np.concatenate([[0], np.cumsum(self.length)[:-1]]).astype(np.int64) \
            if lens else np.zeros(0, dtype=np.int64)


def _gather(start, length, reps_part):
    """Edge indices for each part listed in reps_part, concatenated."""
    ln = length[reps_part]
    total = int(ln.sum())
    offs = np.repeat(start[reps_part] - np.concatenate([[0], np.cumsum(ln)[:-1]]), ln)
    return np.arange(total) + offs, ln


def _minkowski_convex(A: "_Prepared", B: "_Prepared") -> List[np.ndarray]:
    """
    Union of all convex Minkowski sums A_i ⊕ B_j (B already negated), in
    clipper units. Every pair is merged at once with numpy (edge-angle merge).
    """
    if A.n == 0 or B.n == 0:
        return []
    pa = np.repeat(np.arange(A.n), B.n)
    pb = np.tile(np.arange(B.n), A.n)
    npairs = len(pa)
    ia, la = _gather(A.start, A.length, pa)
    ib, lb = _gather(B.start, B.length, pb)
    pair_a = np.repeat(np.arange(npairs), la)
    pair_b = np.repeat(np.arange(npairs), lb)
    pair = np.concatenate([pair_a, pair_b])
    ang = np.concatenate([A.ang[ia], B.ang[ib]])
    edg = np.concatenate([A.edges[ia], B.edges[ib]])
    order = np.lexsort((ang, pair))
    edg = edg[order]
    pair = pair[order]
    csum = np.cumsum(edg, axis=0)
    sizes = la + lb
    first = np.concatenate([[0], np.cumsum(sizes)[:-1]])
    base = np.repeat(csum[first] - edg[first], sizes, axis=0)
    excl = csum - edg - base                                  # exclusive cumsum per pair
    verts = excl + np.repeat(A.p0[pa] + B.p0[pb], sizes, axis=0)
    ints = np.round(verts * SCALE).astype(np.int64)
    paths = np.split(ints, np.cumsum(sizes)[:-1])
    return _union(paths)


# ── Design geometry (per job: depends on margin) ─────────────────────────────
class _Design:
    def __init__(self, uid: int, req: NestRequest, margin: float,
                 allow_holes: bool):
        self.uid = uid            # set by the caller to its index in `designs`
        self.design_id = req.design_id
        self.name = req.design_name
        poly = req.polygon
        if isinstance(poly, MultiPolygon):
            solid = unary_union([Polygon(p.exterior) for p in poly.geoms])
        else:
            solid = Polygon(poly.exterior)
        c = solid.centroid
        self.cx, self.cy = c.x, c.y
        self.raw = translate(solid, -c.x, -c.y)          # centred solid outline
        self.shape = self.raw                            # true shape (with holes)
        if req.holes is not None and not req.holes.is_empty:
            try:
                sh = self.raw.difference(translate(req.holes, -c.x, -c.y))
                if isinstance(sh, (Polygon, MultiPolygon)) and not sh.is_empty:
                    self.shape = sh
            except Exception:
                pass
        self.area = self.shape.area

        grow = margin / 2.0 + SIMPLIFY_TOL
        off = self.raw.buffer(grow, quad_segs=12).simplify(SIMPLIFY_TOL)
        # every part matters: a design may be two separate motifs
        self.parts_solid, self.merge_tol = _decompose_capped(off)

        self.parts_holed = self.parts_solid
        self.has_holes = False
        if allow_holes and req.holes is not None and not req.holes.is_empty:
            holes = translate(req.holes, -c.x, -c.y)
            # keep only holes big enough to matter (≥ 2 mm across after margin)
            useful = [h for h in (holes.geoms if hasattr(holes, 'geoms') else [holes])
                      if isinstance(h, Polygon) and
                      not h.buffer(-(margin / 2.0 + 1.0)).is_empty]
            if useful:
                holed = self.raw.difference(unary_union(useful))
                offh = holed.buffer(grow, quad_segs=12).simplify(SIMPLIFY_TOL)
                offh = _largest(offh)
                if offh.interiors:
                    self.parts_holed, _ = _decompose_capped(offh)
                    self.has_holes = True

        self.sym = max(1, int(getattr(req, "sym_order", 1) or 1))
        self._rot_cache: Dict[float, tuple] = {}
        self._rot_map: Dict[tuple, dict] = {}
        self.prep_holed = _Prepared(self.parts_holed)
        self._neg_cache: Dict[float, _Prepared] = {}

    def rot_map(self, rotations) -> dict:
        """
        {allowed rotation → the angle actually used}. A shape with k-fold
        symmetry looks identical every 360/k degrees, so those angles collapse
        onto one — same geometry, a fraction of the work.
        """
        key = tuple(rotations)
        m = self._rot_map.get(key)
        if m is None:
            period = 360.0 / self.sym
            m, seen = {}, {}
            for r in rotations:
                k = round((r % period) / 0.001)
                if k not in seen:
                    seen[k] = r
                m[r] = seen[k]
            self._rot_map[key] = m
        return m

    def neg_rotated(self, rel: float) -> "_Prepared":
        """−(solid parts rotated by rel), prepared for Minkowski sums."""
        r = self._neg_cache.get(rel)
        if r is None:
            r = _Prepared([-p for p in _rot_parts(self.parts_solid, rel)])
            self._neg_cache[rel] = r
        return r

    def rotated(self, rot: float):
        """(raw_bounds, float_parts) for a rotation — cached."""
        r = self._rot_cache.get(rot)
        if r is None:
            raw = sh_rotate(self.raw, rot, origin=(0, 0)) if rot else self.raw
            r = (raw.bounds,)
            self._rot_cache[rot] = r
        return r


def _largest(g):
    if isinstance(g, Polygon):
        return g
    polys = [p for p in getattr(g, 'geoms', []) if isinstance(p, Polygon)]
    return max(polys, key=lambda p: p.area) if polys else g


# Session-wide caches: re-running a job (or a job sharing designs) reuses the
# expensive geometry. Keys include everything the geometry depends on.
_NFP_STORE: "OrderedDict[tuple, List[np.ndarray]]" = OrderedDict()
_NFP_BYTES = [0]
_NFP_MAX_BYTES = 384 * 1024 * 1024      # keep the most recent ~384 MB of NFPs


def _store_nfp(key, paths):
    """Remember an NFP, dropping the least recently used ones when too big."""
    _NFP_STORE[key] = paths
    _NFP_BYTES[0] += sum(p.nbytes for p in paths) + 200
    while _NFP_BYTES[0] > _NFP_MAX_BYTES and len(_NFP_STORE) > 1:
        _, old = _NFP_STORE.popitem(last=False)
        _NFP_BYTES[0] -= sum(p.nbytes for p in old) + 200
_DESIGN_STORE: Dict[tuple, "_Design"] = {}


def _design_for(req: NestRequest, margin: float, allow_holes: bool) -> "_Design":
    poly = req.polygon
    hkey = req.holes.wkb if (allow_holes and req.holes is not None) else b""
    key = (req.design_id, poly.wkb, hkey, round(margin, 6), bool(allow_holes),
           SIMPLIFY_TOL, CONVEX_TOL, int(getattr(req, "sym_order", 1) or 1))
    d = _DESIGN_STORE.get(key)
    if d is None:
        d = _Design(0, req, margin, allow_holes)
        d.sig = hash(key)
        d.sym = max(1, int(getattr(req, "sym_order", 1) or 1))
        if len(_DESIGN_STORE) > 2000:
            _DESIGN_STORE.clear()
        _DESIGN_STORE[key] = d
    return d


class _NFPCache:
    """NFP(fixed A @ rotA, moving B @ rotB) cached via relative rotation."""
    def __init__(self, designs: List[_Design]):
        self.designs = designs
        self.base: Dict[tuple, List[np.ndarray]] = {}
        self.full: Dict[tuple, List[np.ndarray]] = {}
        self.lock = threading.Lock()

    def _base(self, a: int, b: int, rel: float):
        """
        NFP of design a (fixed, 0°) against design b (moving, rel°).
        Only one of each design pair is ever built: the other side is the
        point reflection of it,  N_ab(rel) = −R_rel( N_ba(−rel) ).
        """
        da, db = self.designs[a], self.designs[b]
        skey = (da.sig, db.sig, rel)
        base = _NFP_STORE.get(skey)
        if base is not None:
            _NFP_STORE.move_to_end(skey)
            return base
        # A symmetric fixed design gives the same NFP rotated:
        #   N_ab(rel + 360/k) = R(360/k) · N_ab(rel)
        if da.sym > 1:
            period = 360.0 / da.sym
            canon = round(rel % period, 6)
            if abs(canon - rel) > 1e-9:
                base = _rot_int(self._base(a, b, canon), rel - canon)
                _store_nfp(skey, base)
                return base
        mirror = (db.sig, da.sig, (-rel) % 360)
        other = _NFP_STORE.get(mirror)
        if other is not None and da.prep_holed is not None:
            _NFP_STORE.move_to_end(mirror)
            base = _rot_int([-p for p in other], rel)
        else:
            base = _minkowski_convex(da.prep_holed, db.neg_rotated(rel))
        _store_nfp(skey, base)
        return base

    def get(self, a: int, rot_a: float, b: int, rot_b: float):
        key = (a, rot_a, b, rot_b)
        r = self.full.get(key)
        if r is not None:
            return r
        rel = (rot_b - rot_a) % 360
        bkey = (a, b, rel)
        base = self.base.get(bkey)
        if base is None:
            base = self._base(a, b, rel)
            self.base[bkey] = base
        r = _rot_int(base, rot_a)
        self.full[key] = r
        return r


# ── Platform state ───────────────────────────────────────────────────────────
class _Platform:
    __slots__ = ("placed", "feas", "ext_x", "ext_y", "min_x", "min_y")

    def __init__(self):
        self.placed: List[tuple] = []        # (design_uid, rot, xi, yi, item_idx)
        self.feas: Dict[tuple, tuple] = {}   # (uid, rot) -> (paths, n_incorporated)
        self.ext_x = 0.0
        self.ext_y = 0.0
        self.min_x = 0.0
        self.min_y = 0.0

    def clone(self):
        p = _Platform()
        p.placed = list(self.placed)
        p.feas = dict(self.feas)
        p.ext_x, p.ext_y = self.ext_x, self.ext_y
        p.min_x, p.min_y = self.min_x, self.min_y
        return p


class _Packer:
    def __init__(self, job: NestingJob, designs: List[_Design], nfp: _NFPCache):
        self.job = job
        self.designs = designs
        self.nfp = nfp
        W, H = job.platform_w, job.platform_h
        ec = max(0.0, job.edge_clearance)
        self.W, self.H = W, H
        ox, oy = max(0.0, job.overflow_x), max(0.0, job.overflow_y)
        # Overflow applies to BOTH sides of an axis
        self.x_lo = -ox if ox > 0 else ec
        self.x_hi = W + ox if ox > 0 else W - ec
        self.y_lo = -oy if oy > 0 else ec
        self.y_hi = H + oy if oy > 0 else H - ec
        self.long_x = W >= H
        self.ifp: Dict[tuple, Optional[np.ndarray]] = {}
        # Angles that are identical for a symmetric design are skipped
        self.rot_maps = {d.uid: d.rot_map(job.rotations) for d in designs}
        self.rots = {d.uid: list(dict.fromkeys(self.rot_maps[d.uid].values()))
                     for d in designs}

    def _ifp(self, uid, rot):
        k = (uid, rot)
        if k in self.ifp:
            return self.ifp[k]
        b = self.designs[uid].rotated(rot)[0]
        x0 = self.x_lo - b[0]; x1 = self.x_hi - b[2]
        y0 = self.y_lo - b[1]; y1 = self.y_hi - b[3]
        if x1 < x0 - 1e-6 or y1 < y0 - 1e-6:
            r = None
        else:
            # round inward so a piece can never exceed the container
            X0 = int(math.ceil(x0 * SCALE)); X1 = int(math.floor(x1 * SCALE))
            Y0 = int(math.ceil(y0 * SCALE)); Y1 = int(math.floor(y1 * SCALE))
            X1 = max(X1, X0); Y1 = max(Y1, Y0)
            r = np.array([[X0, Y0], [X1, Y0], [X1, Y1], [X0, Y1]], dtype=np.int64)
        self.ifp[k] = r
        return r

    def feasible(self, plat: _Platform, uid, rot):
        k = (uid, rot)
        entry = plat.feas.get(k)
        if entry is None:
            ifp = self._ifp(uid, rot)
            if ifp is None:
                plat.feas[k] = ([], len(plat.placed))
                return []
            paths, n = [ifp], 0
        else:
            paths, n = entry
            if n == len(plat.placed):
                return paths
        if paths and n < len(plat.placed):
            clips = []
            for (pu, pr, px, py, _) in plat.placed[n:]:
                off = np.array([px, py], dtype=np.int64)
                for p in self.nfp.get(pu, pr, uid, rot):
                    clips.append(p + off)
            paths = _clip(paths, clips, pyclipper.CT_DIFFERENCE)
            # Remove zero-width spikes / slivers clipper can leave behind
            paths = [np.asarray(p, dtype=np.int64)
                     for p in pyclipper.CleanPolygons([q.tolist() for q in paths])
                     if len(p) >= 3]
        plat.feas[k] = (paths, len(plat.placed))
        return paths

    def _collides(self, plat: _Platform, uid, rot, xi, yi) -> bool:
        """True if (xi, yi) is strictly inside any placed piece's NFP."""
        for (pu, pr, px, py, _) in plat.placed:
            pt = (xi - px, yi - py)
            inside = 0
            for p in self.nfp.get(pu, pr, uid, rot):
                r = pyclipper.PointInPolygon(pt, p.tolist())
                if r == -1:          # on the boundary = touching = allowed
                    inside = 0
                    break
                inside ^= r
            if inside:
                return True
        return False

    def _score(self, plat, pts, b, right, top):
        """Lower is better. Gravity along the platform's long axis."""
        left, bot = pts[:, 0] + b[0], pts[:, 1] + b[1]
        if self.long_x:
            ext = np.maximum(plat.ext_x, right)
            return ext * 1000.0 + right * 10.0 + top * 0.5 + bot * 0.5
        ext = np.maximum(plat.ext_y, top)
        return ext * 1000.0 + top * 10.0 + right * 0.5 + left * 0.5

    def best_position(self, plat: _Platform, uid: int, pref: float = None):
        """
        Best (score, rot, xi, yi, right, top, left, bot) or None.
        `pref` is the rotation the search picked for this piece; it wins when
        the piece fits at that angle, otherwise every allowed angle is tried.
        """
        if pref is not None:
            pref = self.rot_maps[uid].get(pref, pref)
            r = self._best_at(plat, uid, [pref])
            if r is not None:
                return r
        return self._best_at(plat, uid, self.rots[uid])

    def _best_at(self, plat: _Platform, uid: int, rotations):
        best = None
        for rot in rotations:
            paths = self.feasible(plat, uid, rot)
            if not paths:
                continue
            pts = np.concatenate(paths).astype(float) / SCALE
            b = self.designs[uid].rotated(rot)[0]
            right, top = pts[:, 0] + b[2], pts[:, 1] + b[3]
            score = self._score(plat, pts, b, right, top)
            left, bot = pts[:, 0] + b[0], pts[:, 1] + b[1]
            over = (np.maximum(0.0, right - self.W) + np.maximum(0.0, -left) +
                    np.maximum(0.0, top - self.H) + np.maximum(0.0, -bot))
            score = score + over * OVERFLOW_PEN
            if best is not None and score.min() >= best[0]:
                continue
            # Take the best-scoring vertex that is genuinely collision-free
            for i in np.argsort(score, kind='stable')[:64]:
                if best is not None and score[i] >= best[0]:
                    break
                xi = int(round(pts[i, 0] * SCALE)); yi = int(round(pts[i, 1] * SCALE))
                if self._collides(plat, uid, rot, xi, yi):
                    continue
                best = (float(score[i]), rot, xi, yi, float(right[i]), float(top[i]),
                        float(left[i]), float(bot[i]))
                break
        return best

    def commit(self, plat: _Platform, uid, rot, xi, yi, right, top, left, bot, item_idx):
        plat.placed.append((uid, rot, xi, yi, item_idx))
        plat.ext_x = max(plat.ext_x, right)
        plat.ext_y = max(plat.ext_y, top)
        plat.min_x = min(plat.min_x, left)
        plat.min_y = min(plat.min_y, bot)

    def place_unit(self, plat: _Platform, unit, genes=None) -> Optional[_Platform]:
        """Place every piece of a unit on (a copy of) plat, or return None."""
        work = plat.clone() if len(unit) > 1 else plat
        placed_here = []
        for item_idx, uid in unit:
            r = self.best_position(work, uid, genes[item_idx] if genes else None)
            if r is None:
                return None
            _, rot, xi, yi, right, top, left, bot = r
            self.commit(work, uid, rot, xi, yi, right, top, left, bot, item_idx)
            placed_here.append(item_idx)
        return work

    def run(self, units, stop_event=None, genes=None,
            on_piece=None) -> Tuple[List[_Platform], List[int]]:
        platforms: List[_Platform] = []
        unplaced: List[int] = []
        done_pieces = 0
        for unit in units:
            if stop_event is not None and stop_event.is_set():
                raise _Stopped()
            done = False
            for pi in range(len(platforms)):
                res = self.place_unit(platforms[pi], unit, genes)
                if res is not None:
                    platforms[pi] = res
                    done = True
                    break
            if not done:
                fresh = _Platform()
                res = self.place_unit(fresh, unit, genes)
                if res is not None:
                    platforms.append(res)
                else:
                    unplaced.extend(i for i, _ in unit)
            if on_piece is not None:
                done_pieces += len(unit)
                on_piece(done_pieces, len(platforms))
        return platforms, unplaced


def _fmt_secs(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec}s" if sec < 60 else f"{sec // 60}:{sec % 60:02d}"


class _Stopped(Exception):
    """Raised inside a layout evaluation when the user presses Stop."""


def _fitness(platforms: List[_Platform], unplaced, long_x, W, H) -> float:
    n = len(platforms)
    if n == 0:
        return 1e12 * (1 + len(unplaced))
    last = platforms[-1]
    last_ext = (last.ext_x / W) if long_x else (last.ext_y / H)
    # Also reward compact earlier platforms (small weight)
    comp = sum((p.ext_x / W if long_x else p.ext_y / H) for p in platforms[:-1])
    return len(unplaced) * 1e9 + n * 1e3 + last_ext * 10 + comp * 0.1


# ── Order search ─────────────────────────────────────────────────────────────
def _mutate_tiers(order, tiers):
    """Swap / move / reverse inside a single priority tier."""
    o = list(order)
    candidates = [t for t in tiers if t[1] - t[0] >= 2]
    if not candidates:
        return o
    lo, hi = random.choice(candidates)
    op = random.random()
    i, j = sorted(random.sample(range(lo, hi), 2))
    if op < 0.45:
        o[i], o[j] = o[j], o[i]
    elif op < 0.8:
        x = o.pop(j)
        o.insert(i, x)
    else:
        o[i:j + 1] = o[i:j + 1][::-1]
    return o


# ── Main entry point ─────────────────────────────────────────────────────────
def run_nesting(job: NestingJob,
                progress_callback: Callable = None,
                best_callback: Callable = None,
                stop_event: threading.Event = None,
                stats_callback: Callable = None) -> List[PlacementResult]:
    """
    Nest all pieces. progress_callback(cur, total, msg); best_callback(results)
    is called whenever a better layout is found; stats_callback(dict) reports
    search progress a few times per second. stop_event stops the search
    within one piece placement (the best layout so far is returned).
    job.time_limit may be math.inf: search until stopped.
    """
    t_start = time.time()
    reqs = job.all_requests()
    if not reqs and not job.groups:
        return NestResults()

    def _prog(c, t, m):
        if progress_callback:
            try:
                progress_callback(c, t, m)
            except Exception:
                pass

    _prog(0, 100, "Preparing shapes…")

    # Unique designs (by design_id) → geometry
    designs: List[_Design] = []
    uid_of: Dict[str, int] = {}

    def _uid(req: NestRequest) -> int:
        if req.design_id not in uid_of:
            d = _design_for(req, job.margin_mm, job.allow_holes)
            d.uid = len(designs)
            uid_of[req.design_id] = d.uid
            designs.append(d)
        return uid_of[req.design_id]

    items: List[NestItem] = []
    units: List[Tuple[int, float, list]] = []   # (priority, area, [(item_idx, uid)])

    for req in reqs:
        if req.quantity <= 0:
            continue
        u = _uid(req)
        d = designs[u]
        for _ in range(req.quantity):
            items.append(NestItem(design_id=req.design_id, design_name=req.design_name,
                                  polygon=d.shape, hull_cx=d.cx, hull_cy=d.cy))
            units.append((req.priority, d.area, [(len(items) - 1, u)]))

    for g in job.groups:
        members = [(m, _uid(m)) for m in g.members if m.quantity > 0]
        for s in range(max(0, g.sets)):
            unit = []
            for m, u in members:
                d = designs[u]
                for _ in range(m.quantity):
                    items.append(NestItem(design_id=m.design_id, design_name=m.design_name,
                                          polygon=d.shape, hull_cx=d.cx, hull_cy=d.cy,
                                          group_name=g.name, set_index=s))
                    unit.append((len(items) - 1, u))
            unit.sort(key=lambda t: -designs[t[1]].area)
            if unit:
                units.append((g.priority, sum(designs[u].area for _, u in unit), unit))

    if not units:
        return NestResults()

    # Order: priority tiers (high first), inside a tier largest first
    units.sort(key=lambda u: (-u[0], -u[1]))
    tiers = []
    start = 0
    for i in range(1, len(units) + 1):
        if i == len(units) or units[i][0] != units[start][0]:
            tiers.append((start, i))
            start = i
    unit_lists = [u[2] for u in units]

    nfp = _NFPCache(designs)
    total_pieces = len(items)
    W, H = job.platform_w, job.platform_h
    long_x = W >= H

    def evaluate(order, genes=None, interruptible=True, on_piece=None):
        pk = _Packer(job, designs, nfp)
        plats, unpl = pk.run([unit_lists[i] for i in order],
                             stop_event if interruptible else None, genes, on_piece)
        return _fitness(plats, unpl, long_x, W, H), plats, unpl

    # Initial greedy layout
    _prog(2, 100, f"Packing {total_pieces} pieces…")
    order = list(range(len(unit_lists)))
    # The first layout always completes so there is something to return
    t_pack = time.time()
    last_report = [0.0]

    def _packing_progress(done, n_plat):
        now = time.time()
        if now - last_report[0] < 0.3 and done < total_pieces:
            return
        last_report[0] = now
        el = now - t_pack
        left = (el / done) * (total_pieces - done) if done else 0.0
        eta = f" · about {_fmt_secs(left)} left" if left > 1.5 else ""
        _prog(int(100 * done / max(1, total_pieces)), 100,
              f"Packing {total_pieces} pieces — {done} placed, "
              f"{n_plat} platform{'s' if n_plat != 1 else ''}{eta}")

    best_fit, best_plats, best_unpl = evaluate(order, interruptible=False,
                                               on_piece=_packing_progress)
    best_order = order
    # Rotation genes: None = let the placer pick. The search can pin a piece to
    # an angle, which is how interlocking patterns are discovered.
    best_genes = [None] * len(items)
    n_rot = len(job.rotations)

    def _emit_best():
        if best_callback:
            try:
                best_callback(_build_results(job, items, best_plats))
            except Exception as ex:
                log.debug("best_callback failed: %s", ex)

    _emit_best()

    # Lower bound on platform count (area based) — stop early if reached
    plat_area = W * H
    lb = max(1, math.ceil(sum(it.area for it in items) / plat_area))

    time_limit = max(0.0, job.time_limit)
    nonstop = math.isinf(time_limit)
    iters = 0
    improvements = 0
    last_improve = time.time()
    total_metal = sum(it.area for it in items)
    last_stats = [0.0]

    def _stats(force=False):
        if not stats_callback:
            return
        now = time.time()
        if not force and now - last_stats[0] < 0.25:
            return
        last_stats[0] = now
        n = len(best_plats)
        placed_area = sum(items[p[4]].area for pl in best_plats for p in pl.placed)
        try:
            stats_callback({
                "layouts": iters + 1,
                "improvements": improvements,
                "platforms": n,
                "metal_pct": 100.0 * placed_area / (n * plat_area) if n and plat_area else 0.0,
                "unplaced": len(best_unpl),
                "elapsed": now - t_start,
                "limit": time_limit,
                "since_improve": now - last_improve,
                "lower_bound": lb,
                "total_metal": total_metal,
            })
        except Exception as ex:
            log.debug("stats_callback failed: %s", ex)

    _stats(force=True)
    # Alternate starting orders for diversity (area-desc within tier is #1)
    seeds = []
    if len(unit_lists) > 2:
        for key in (lambda i: -max(designs[u].raw.bounds[2] - designs[u].raw.bounds[0]
                                   for _, u in unit_lists[i]),
                    lambda i: -max(designs[u].raw.bounds[3] - designs[u].raw.bounds[1]
                                   for _, u in unit_lists[i])):
            o = []
            for lo, hi in tiers:
                o.extend(sorted(range(lo, hi), key=key))
            seeds.append(o)

    unit_of_item = {}
    for ui, ul in enumerate(unit_lists):
        for item_idx, _ in ul:
            unit_of_item[item_idx] = ui

    def _mutate_genes(genes, plats):
        """Pin a few pieces (preferring the emptiest platform) to new angles."""
        if n_rot < 2:
            return genes
        g = list(genes)
        pool = [p[4] for p in plats[-1].placed] if plats else []
        if len(pool) < 3 and plats:
            pool += [p[4] for pl in plats for p in pl.placed]
        if not pool:
            return g
        for _ in range(random.randint(1, 3)):
            i = random.choice(pool)
            g[i] = None if random.random() < 0.25 else random.choice(job.rotations)
        return g

    def _promote(order, plats):
        """Move a unit that ended on the last platform earlier in the order."""
        if len(plats) < 2:
            return None
        last = list({unit_of_item[p[4]] for p in plats[-1].placed})
        if not last:
            return None
        u = random.choice(last)
        i = order.index(u)
        lo = next((a for a, b in tiers if a <= i < b), 0)
        if i <= lo:
            return None
        o = list(order)
        o.pop(i)
        o.insert(random.randint(lo, i - 1), u)
        return o

    current_order, current_fit, current_plats = best_order, best_fit, best_plats
    current_genes = best_genes
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        el = time.time() - t_start
        if el >= time_limit:
            break
        if not nonstop and len(best_plats) <= lb and not best_unpl and \
                time.time() - last_improve > max(2.0, time_limit * 0.25):
            break
        cand_genes = current_genes
        if seeds:
            cand = seeds.pop(0)
            cand_genes = best_genes
        else:
            use_best = random.random() >= 0.7
            base = best_order if use_best else current_order
            cand_genes = best_genes if use_best else current_genes
            plats_ref = best_plats if use_best else current_plats
            cand = None
            if n_rot > 1 and random.random() < 0.35:
                cand = list(base)
                cand_genes = _mutate_genes(cand_genes, plats_ref)
            elif random.random() < 0.5:
                cand = _promote(base, plats_ref)
            if cand is None:
                cand = _mutate_tiers(base, tiers)
                if random.random() < 0.3:
                    cand = _mutate_tiers(cand, tiers)
        try:
            fit, plats, unpl = evaluate(cand, cand_genes)
        except _Stopped:
            break
        iters += 1
        # Late-acceptance style: accept equal or slightly worse to escape plateaus
        if fit <= current_fit + 0.5:
            current_order, current_fit, current_plats = cand, fit, plats
            current_genes = cand_genes
        if fit < best_fit - 1e-9:
            best_fit, best_plats, best_unpl, best_order = fit, plats, unpl, cand
            best_genes = cand_genes
            last_improve = time.time()
            improvements += 1
            _emit_best()
            _stats(force=True)
        else:
            _stats()
    _stats(force=True)

    results = NestResults(_build_results(job, items, best_plats))
    results.unplaced = [items[i].design_name for i in best_unpl]
    results.layouts_tried = iters + 1
    results.seconds = time.time() - t_start
    _validate(results, job)
    n_unpl = len(best_unpl)
    _prog(100, 100, f"Done — {len(results)} platform(s)"
                    + (f", {n_unpl} piece(s) do not fit" if n_unpl else ""))
    log.info("Nesting: %d pieces, %d platforms, %d layouts, %.1fs",
             total_pieces, len(results), iters + 1, time.time() - t_start)
    return results


def _build_results(job, items, platforms) -> List[PlacementResult]:
    W, H = job.platform_w, job.platform_h
    results = []
    for pidx, plat in enumerate(platforms):
        placed = []
        for (_, rot, xi, yi, item_idx) in plat.placed:
            src = items[item_idx]
            it = NestItem(design_id=src.design_id, design_name=src.design_name,
                          polygon=src.polygon, rotation=rot, placed=True,
                          x=xi / SCALE, y=yi / SCALE, platform=pidx,
                          hull_cx=src.hull_cx, hull_cy=src.hull_cy,
                          group_name=src.group_name, set_index=src.set_index)
            placed.append(it)
        used = sum(it.area for it in placed)
        ox = max(0.0, plat.ext_x - W, -plat.min_x)
        oy = max(0.0, plat.ext_y - H, -plat.min_y)
        results.append(PlacementResult(
            platform_index=pidx, placements=placed, platform_w=W, platform_h=H,
            efficiency=round(100.0 * used / (W * H), 1) if W * H > 0 else 0.0,
            piece_count=len(placed), used_area=used,
            overflow_x=round(ox, 3), overflow_y=round(oy, 3)))
    return results


def _validate(results: List[PlacementResult], job: NestingJob):
    """Exact geometry check. Logs any violation (should never happen)."""
    W, H = job.platform_w, job.platform_h
    ec = job.edge_clearance
    ox, oy = max(0.0, job.overflow_x), max(0.0, job.overflow_y)
    x_lo = -ox if ox > 0 else ec
    x_hi = W + ox if ox > 0 else W - ec
    y_lo = -oy if oy > 0 else ec
    y_hi = H + oy if oy > 0 else H - ec
    container = box(x_lo - VALIDATE_TOL, y_lo - VALIDATE_TOL,
                    x_hi + VALIDATE_TOL, y_hi + VALIDATE_TOL)
    bad = 0
    for r in results:
        polys = [it.placed_polygon for it in r.placements]
        tree = shapely.STRtree(polys)
        for i, p in enumerate(polys):
            if not container.contains(p):
                bad += 1
                log.warning("Platform %d: %s outside platform",
                            r.platform_index + 1, r.placements[i].design_name)
            for j in tree.query(p.buffer(job.margin_mm)):
                if j <= i:
                    continue
                d = p.distance(polys[j])
                if d < job.margin_mm - VALIDATE_TOL:
                    bad += 1
                    log.warning("Platform %d: gap %.3f mm < margin between %s and %s",
                                r.platform_index + 1, d,
                                r.placements[i].design_name,
                                r.placements[j].design_name)
    return bad


def check_layout(results: List[PlacementResult], job: NestingJob) -> int:
    """Public wrapper: number of margin/boundary violations."""
    return _validate(results, job)


def estimate_platforms(job: NestingJob) -> int:
    """Quick area-based guess (true-shape packing typically reaches ~70–80%)."""
    reqs = job.all_requests()
    pa = ((job.platform_w + 2 * max(0.0, job.overflow_x)) *
          (job.platform_h + 2 * max(0.0, job.overflow_y)))
    if pa <= 0:
        return 0
    m = job.margin_mm
    total = 0.0
    for r in reqs:
        total += r.polygon.buffer(m / 2).area * r.quantity
    for g in job.groups:
        for mreq in g.members:
            total += mreq.polygon.buffer(m / 2).area * mreq.quantity * g.sets
    if total <= 0:
        return 0
    return max(1, math.ceil(total / (pa * 0.78)))
