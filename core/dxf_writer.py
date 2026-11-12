"""
dxf_writer.py

Takes nesting results and writes one output DXF per platform (plus a combined
file with every platform side by side for Rhino).

Every source entity is copied with ezdxf's xref loader (layers, linetypes,
text styles and blocks come along) and moved with a single rigid transform:

    world point P  →  R(rotation) · (P − outline_centroid) + (item.x, item.y)

which is exactly the transform the nesting engine used for the outline.
"""

import os
import math
import logging
from typing import List, Dict

import ezdxf
from ezdxf import colors, xref
from ezdxf.math import Matrix44

from .nesting_engine import PlacementResult, NestItem

log = logging.getLogger(__name__)

PLATFORM_LAYER    = "_PLATFORM_BOUNDARY"
OVERFLOW_LAYER    = "_PLATFORM_OVERFLOW"
PLATFORM_COLOR    = colors.RED
OVERFLOW_COLOR    = colors.YELLOW


def item_matrix(item: NestItem, x_offset: float = 0.0) -> Matrix44:
    """Source-DXF → platform transform for one placed piece."""
    return Matrix44.chain(
        Matrix44.translate(-item.hull_cx, -item.hull_cy, 0),
        Matrix44.z_rotate(math.radians(item.rotation)),
        Matrix44.translate(item.x + x_offset, item.y, 0),
    )


class _SourceCache:
    """Reads each source DXF once per export."""
    def __init__(self, source_dxf_map: Dict[str, str]):
        self.map = source_dxf_map
        self.docs = {}

    def get(self, design_id):
        if design_id in self.docs:
            return self.docs[design_id]
        path = self.map.get(design_id)
        doc = None
        if path and os.path.isfile(path):
            try:
                doc = ezdxf.readfile(path)
            except Exception as ex:
                log.error("Cannot read source DXF %s: %s", path, ex)
        else:
            log.warning("Source DXF not found for %s", design_id)
        self.docs[design_id] = doc
        return doc


def _ensure_layer(doc, name, color):
    if name not in doc.layers:
        doc.layers.add(name, color=color)


def _draw_platform(msp, doc, result: PlacementResult, x_offset: float,
                   overflow_x: float, overflow_y: float, label: str = ""):
    w, h = result.platform_w, result.platform_h
    _ensure_layer(doc, PLATFORM_LAYER, PLATFORM_COLOR)
    msp.add_lwpolyline(
        [(x_offset, 0), (x_offset + w, 0), (x_offset + w, h), (x_offset, h)],
        close=True, dxfattribs={'layer': PLATFORM_LAYER})
    if overflow_x > 0 or overflow_y > 0:
        # Overflow reaches out on every side
        _ensure_layer(doc, OVERFLOW_LAYER, OVERFLOW_COLOR)
        x0, y0 = x_offset - overflow_x, -overflow_y
        x1, y1 = x_offset + w + overflow_x, h + overflow_y
        msp.add_lwpolyline(
            [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
            close=True, dxfattribs={'layer': OVERFLOW_LAYER, 'linetype': 'DASHED'}
            if 'DASHED' in doc.linetypes else {'layer': OVERFLOW_LAYER})
    if label:
        msp.add_text(label, height=2.0, dxfattribs={'layer': PLATFORM_LAYER}
                     ).set_placement((x_offset + w / 2, -5),
                                     align=ezdxf.enums.TextEntityAlignment.MIDDLE_CENTER)


def _place_items(out_doc, result: PlacementResult, sources: _SourceCache,
                 x_offset: float = 0.0) -> int:
    """Copy + transform every placed piece. Returns number of pieces written."""
    msp = out_doc.modelspace()
    written = 0
    for item in result.placements:
        if not item.placed:
            continue
        src = sources.get(item.design_id)
        if src is None:
            continue
        before = {e.dxf.handle for e in msp}
        try:
            xref.load_modelspace(src, out_doc)
        except Exception as ex:
            log.error("Could not import %s: %s", item.design_id, ex)
            continue
        m = item_matrix(item, x_offset)
        for e in [e for e in msp if e.dxf.handle not in before]:
            try:
                e.transform(m)
            except Exception as ex:
                # Entities that cannot be transformed (rare) are removed rather
                # than left at the wrong place.
                log.warning("Dropping untransformable %s in %s: %s",
                            e.dxftype(), item.design_id, ex)
                msp.delete_entity(e)
        written += 1
    return written


def write_platform_dxf(platform_result: PlacementResult,
                       source_dxf_map: dict,
                       output_path: str,
                       job_name: str = "",
                       overflow_x: float = 0.0,
                       overflow_y: float = 0.0,
                       _sources: _SourceCache = None) -> bool:
    try:
        out_doc = ezdxf.new('R2010', setup=True)
        out_doc.units = ezdxf.units.MM
        _draw_platform(out_doc.modelspace(), out_doc, platform_result, 0.0,
                       overflow_x, overflow_y)
        _place_items(out_doc, platform_result,
                     _sources or _SourceCache(source_dxf_map))
        out_doc.saveas(output_path)
        log.info("Saved platform DXF: %s", output_path)
        return True
    except Exception as ex:
        log.error("Failed to write DXF %s: %s", output_path, ex)
        return False


def write_combined_dxf(results: List[PlacementResult],
                       source_dxf_map: dict,
                       output_path: str,
                       job_name: str = "",
                       gap_mm: float = 10.0,
                       overflow_x: float = 0.0,
                       overflow_y: float = 0.0,
                       _sources: _SourceCache = None) -> bool:
    """All platforms side by side in one DXF (what opens in Rhino)."""
    try:
        sources = _sources or _SourceCache(source_dxf_map)
        out_doc = ezdxf.new('R2010', setup=True)
        out_doc.units = ezdxf.units.MM
        msp = out_doc.modelspace()
        for k, result in enumerate(results):
            x_off = k * (result.platform_w + 2 * overflow_x + gap_mm)
            _draw_platform(msp, out_doc, result, x_off, overflow_x, overflow_y,
                           label=f"Platform {k + 1}")
            _place_items(out_doc, result, sources, x_off)
        out_doc.saveas(output_path)
        log.info("Saved combined DXF: %s", output_path)
        return True
    except Exception as ex:
        log.error("Failed to write combined DXF %s: %s", output_path, ex)
        return False


def platform_filename(job_name: str, index: int) -> str:
    return f"{job_name}_platform_{index + 1:02d}.dxf"


def combined_filename(job_name: str) -> str:
    """One file per job: every platform laid out side by side."""
    return f"{job_name}.dxf"


def export_job(results: List[PlacementResult],
               source_dxf_map: dict,
               output_dir: str,
               job_name: str = "job",
               overflow_x: float = 0.0,
               overflow_y: float = 0.0,
               split_platforms: bool = False) -> List[str]:
    """
    Write the job as ONE DXF holding every platform side by side (the file the
    workshop actually opens). `split_platforms` additionally writes one file
    per platform. Returns the paths written, combined file first.
    """
    os.makedirs(output_dir, exist_ok=True)
    sources = _SourceCache(source_dxf_map)
    written = []
    combined = os.path.join(output_dir, combined_filename(job_name))
    if write_combined_dxf(results, source_dxf_map, combined, job_name,
                          overflow_x=overflow_x, overflow_y=overflow_y,
                          _sources=sources):
        written.append(combined)

    if split_platforms:
        for result in results:
            out_path = os.path.join(output_dir,
                                    platform_filename(job_name, result.platform_index))
            if write_platform_dxf(result, source_dxf_map, out_path, job_name,
                                  overflow_x, overflow_y, sources):
                written.append(out_path)
    return written


def verify_export(dxf_path: str, result: PlacementResult) -> float:
    """
    Re-read an exported platform DXF and return the worst distance (mm)
    between an exported cut outline and where the nesting placed it.
    Pieces whose file has no outer-cut layer are skipped.
    """
    from shapely.geometry import MultiPoint
    from .hull_extractor import _entity_paths, _loops_to_region, _pick_outer_layer
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    outer = _pick_outer_layer({e.dxf.layer for e in msp} -
                              {PLATFORM_LAYER, OVERFLOW_LAYER})
    if not outer:
        return 0.0
    pls = []
    for e in msp:
        if e.dxf.layer == outer:
            pls.extend(_entity_paths(e))
    region = _loops_to_region(pls)
    if region is None:
        return float('inf')
    boundary = region.boundary
    worst = 0.0
    for it in result.placements:
        poly = it.placed_polygon
        ext = poly.exterior if hasattr(poly, 'exterior') else \
            max(poly.geoms, key=lambda g: g.area).exterior
        if not region.buffer(0.05).contains(poly.buffer(-0.05)):
            continue     # this design's outline is not on the outer layer
        pts = MultiPoint(list(ext.coords)[::4])
        worst = max(worst, max(boundary.distance(p) for p in pts.geoms))
    return worst
