"""
library.py

Manages the design library:
- Scans a folder for .dxf files
- Caches hull data per file (invalidated when mtime changes)
- Stores user-defined aliases and design groups
- Persists to library.json alongside the executable
"""

import os
import json
import time
import logging
from dataclasses import dataclass, field, asdict, fields
from typing import Dict, List, Optional

from shapely.affinity import rotate as _rotate, translate as _translate

from .hull_extractor import extract_hull, geom_to_hex, geom_from_hex

SYM_TOL = 0.01     # mm — how exactly a rotated copy must match the original


def symmetry_order(shape, tol: float = SYM_TOL) -> int:
    """
    Largest k (of 24, 12, 8, 6, 4, 3, 2) for which rotating the shape by 360/k
    gives the same shape. 1 = no rotational symmetry. Angles that differ only
    by this symmetry are identical placements, so the nester can skip them.
    """
    if shape is None or shape.is_empty:
        return 1
    try:
        c = shape.centroid
        p = _translate(shape, -c.x, -c.y).simplify(tol / 4)
        ref = p.exterior if p.geom_type == 'Polygon' else p.boundary
        for k in (24, 12, 8, 6, 4, 3, 2):
            q = _rotate(p, 360.0 / k, origin=(0, 0))
            qb = q.exterior if q.geom_type == 'Polygon' else q.boundary
            if ref.hausdorff_distance(qb) <= tol:
                return k
    except Exception as ex:
        log.debug("symmetry test failed: %s", ex)
    return 1

log = logging.getLogger(__name__)

LIBRARY_FILE  = "library.json"
CACHE_VERSION = 3      # bump to force a re-scan when outline extraction changes


@dataclass
class DesignEntry:
    design_id:    str          # stable ID = relative path from library folder
    filename:     str          # full absolute path
    alias:        str          # user-friendly name (defaults to stem)
    width_mm:     float = 0.0
    height_mm:    float = 0.0
    layers:       List[str] = field(default_factory=list)
    entities:     int = 0
    hull_source:  str = ""
    mtime:        float = 0.0  # file modification time at last scan
    hull_pts:     List[List[float]] = field(default_factory=list)   # legacy
    error:        Optional[str] = None
    outline_wkb:  str = ""     # true outline (solid), WKB hex
    holes_wkb:    str = ""     # through-holes inside the outline, WKB hex
    area_mm2:     float = 0.0  # metal area (outline minus holes)
    quality:      str = ""     # 'exact' | 'all-layers' | 'hull'
    sym_order:    int = 1      # k = shape is unchanged when rotated by 360/k
    version:      int = 0

    @property
    def display_name(self) -> str:
        return self.alias or os.path.splitext(os.path.basename(self.filename))[0]


@dataclass
class DesignGroup:
    """
    A set of designs that is always nested together on one platform, with a
    quantity per design — e.g. a size series: A×1, B×6, C×2, D×2.
    The job queue holds the number of SETS of this recipe.
    """
    group_id:   str
    name:       str
    design_ids: List[str] = field(default_factory=list)
    counts:     Dict[str, int] = field(default_factory=dict)   # design_id → per set

    def __post_init__(self):
        # Older libraries repeated a design_id to mean "twice per set"
        if not self.counts:
            self.counts = {}
            for did in self.design_ids:
                self.counts[did] = self.counts.get(did, 0) + 1
        seen, uniq = set(), []
        for did in self.design_ids:
            if did not in seen:
                seen.add(did)
                uniq.append(did)
        self.design_ids = uniq
        for did in uniq:
            self.counts.setdefault(did, 1)
        for did in list(self.counts):
            if did not in seen:
                del self.counts[did]

    def members(self):
        """[(design_id, per-set quantity)] in display order."""
        return [(did, max(0, int(self.counts.get(did, 1)))) for did in self.design_ids]

    @property
    def pieces_per_set(self) -> int:
        return sum(c for _, c in self.members())


class LibraryManager:
    def __init__(self, config_dir: str):
        self.config_dir  = config_dir
        self.library_path = os.path.join(config_dir, LIBRARY_FILE)
        self.folder:   str = ""
        self.designs:  Dict[str, DesignEntry] = {}   # design_id → DesignEntry
        self.groups:   Dict[str, DesignGroup] = {}   # group_id  → DesignGroup
        self._geom_cache: Dict[str, tuple] = {}      # design_id → (outline, holes)
        self._load()

    # ──────────────────────────────────────────────────────
    # Persistence
    # ──────────────────────────────────────────────────────

    def _load(self):
        if not os.path.isfile(self.library_path):
            return
        try:
            with open(self.library_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.folder = data.get('folder', '')

            # Validate folder exists on this machine — clear designs if not
            if self.folder and not os.path.isdir(self.folder):
                log.warning("Library folder not found on this machine: %s — clearing cached designs", self.folder)
                self.folder = ''
                # Keep groups (they're just names) but clear file-based cache
                for g in data.get('groups', []):
                    grp = DesignGroup(**{k: v for k, v in g.items()
                                         if k in {f.name for f in fields(DesignGroup)}})
                    self.groups[grp.group_id] = grp
                return

            known = {f.name for f in fields(DesignEntry)}
            for d in data.get('designs', []):
                e = DesignEntry(**{k: v for k, v in d.items() if k in known})
                # Also validate each individual file still exists
                if e.filename and not os.path.isfile(e.filename):
                    log.debug("Cached design file missing, will re-scan: %s", e.filename)
                    continue
                self.designs[e.design_id] = e
            for g in data.get('groups', []):
                grp = DesignGroup(**{k: v for k, v in g.items()
                                     if k in {f.name for f in fields(DesignGroup)}})
                self.groups[grp.group_id] = grp
            self._geom_cache = {}
        except Exception as ex:
            log.warning("Could not load library: %s", ex)

    def save(self):
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            data = {
                'folder':  self.folder,
                'designs': [asdict(e) for e in self.designs.values()],
                'groups':  [asdict(g) for g in self.groups.values()],
            }
            with open(self.library_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as ex:
            log.error("Could not save library: %s", ex)

    # ──────────────────────────────────────────────────────
    # Folder scanning
    # ──────────────────────────────────────────────────────

    def set_folder(self, folder: str):
        self.folder = folder
        self.save()

    def scan(self, progress_callback=None) -> int:
        """
        Scan library folder for .dxf files.
        Extracts hull for new/changed files only.
        Returns number of designs found.
        """
        if not self.folder or not os.path.isdir(self.folder):
            return 0

        dxf_files = []
        for root, _, files in os.walk(self.folder):
            for fn in files:
                if fn.lower().endswith('.dxf'):
                    dxf_files.append(os.path.join(root, fn))

        total = len(dxf_files)
        for i, filepath in enumerate(dxf_files):
            if progress_callback:
                progress_callback(i, total, os.path.basename(filepath))

            design_id = os.path.relpath(filepath, self.folder)
            mtime     = os.path.getmtime(filepath)

            # Skip if we already have fresh hull data
            existing = self.designs.get(design_id)
            if existing and abs(existing.mtime - mtime) < 0.5 and not existing.error \
                    and existing.version == CACHE_VERSION and existing.filename == filepath:
                continue

            hull_data = extract_hull(filepath)
            self._geom_cache.pop(design_id, None)
            poly = hull_data['polygon']
            holes = hull_data['holes']
            area = 0.0
            sym = 1
            if poly is not None:
                area = poly.area - (holes.area if holes is not None else 0.0)
                shape = poly
                if holes is not None and not holes.is_empty:
                    try:
                        shape = poly.difference(holes)
                    except Exception:
                        shape = poly
                sym = symmetry_order(shape)

            alias = existing.alias if existing else os.path.splitext(
                os.path.basename(filepath))[0]

            entry = DesignEntry(
                design_id=design_id,
                filename=filepath,
                alias=alias,
                width_mm=hull_data['width_mm'],
                height_mm=hull_data['height_mm'],
                layers=hull_data['layers'],
                entities=hull_data['entities'],
                hull_source=hull_data['hull_source'],
                mtime=mtime,
                error=hull_data.get('error'),
                outline_wkb=geom_to_hex(poly),
                holes_wkb=geom_to_hex(holes),
                area_mm2=round(area, 3),
                quality=hull_data.get('quality', ''),
                sym_order=sym,
                version=CACHE_VERSION,
            )
            self.designs[design_id] = entry

        # Remove entries whose files no longer exist
        known = set(os.path.relpath(f, self.folder) for f in dxf_files)
        gone  = [did for did in self.designs if did not in known]
        for did in gone:
            del self.designs[did]

        if progress_callback:
            progress_callback(total, total, "Scan complete")

        self.save()
        return len(self.designs)

    # ──────────────────────────────────────────────────────
    # Design management
    # ──────────────────────────────────────────────────────

    def rename(self, design_id: str, new_alias: str) -> tuple:
        """
        Rename a design.
        - Always updates the display alias in the library.
        - Also renames the actual .dxf file on disk (and updates all internal paths).
        Returns (success: bool, message: str, design_id_after: str).
        """
        entry = self.designs.get(design_id)
        if not entry:
            return (False, "Design not found in library.", design_id)

        old_path = entry.filename

        # Update alias regardless
        entry.alias = new_alias

        # Attempt to rename the file on disk
        if old_path and os.path.isfile(old_path):
            folder     = os.path.dirname(old_path)
            new_fname  = new_alias.strip()
            # Ensure .dxf extension
            if not new_fname.lower().endswith('.dxf'):
                new_fname += '.dxf'
            # Sanitise filename (remove chars invalid on Windows)
            for ch in r'\/:*?"<>|':
                new_fname = new_fname.replace(ch, '_')
            new_path = os.path.join(folder, new_fname)

            if new_path == old_path:
                # Name unchanged on disk — just save alias
                self.save()
                return (True, "", design_id)

            if os.path.exists(new_path):
                # Target already exists — save alias only, don't rename file
                self.save()
                return (True, f"Alias updated. File not renamed — '{new_fname}' already exists.",
                        design_id)

            try:
                os.rename(old_path, new_path)
                # Update entry to point to new path
                new_rel = os.path.relpath(new_path, self.folder)
                del self.designs[design_id]
                entry.filename  = new_path
                entry.design_id = new_rel
                self.designs[new_rel] = entry
                self._geom_cache.pop(design_id, None)
                # Update any group references
                for grp in self.groups.values():
                    if design_id in grp.design_ids:
                        grp.design_ids = [new_rel if did == design_id else did
                                          for did in grp.design_ids]
                        if design_id in grp.counts:
                            grp.counts[new_rel] = grp.counts.pop(design_id)
                self.save()
                return (True, f"File renamed to '{new_fname}'.", new_rel)
            except OSError as ex:
                # Rename failed (permissions, cross-device, etc.) — alias still saved
                self.save()
                return (True, f"Alias updated. File rename failed: {ex}", design_id)
        self.save()
        return (True, "", design_id)

    def get_geometry(self, design_id: str):
        """(outline, holes) Shapely geometries for a design, from the cache."""
        if design_id in self._geom_cache:
            return self._geom_cache[design_id]
        entry = self.designs.get(design_id)
        if not entry:
            return (None, None)
        poly = geom_from_hex(entry.outline_wkb)
        holes = geom_from_hex(entry.holes_wkb)
        if poly is None and entry.filename and os.path.isfile(entry.filename):
            r = extract_hull(entry.filename)
            poly, holes = r['polygon'], r['holes']
        self._geom_cache[design_id] = (poly, holes)
        return (poly, holes)

    def get_polygon(self, design_id: str):
        """Solid outline polygon for a design."""
        return self.get_geometry(design_id)[0]

    def all_designs(self) -> List[DesignEntry]:
        return sorted(self.designs.values(),
                       key=lambda e: e.display_name.lower())

    # ──────────────────────────────────────────────────────
    # Groups
    # ──────────────────────────────────────────────────────

    def create_group(self, name: str, design_ids: List[str],
                     counts: Dict[str, int] = None) -> DesignGroup:
        group_id = f"grp_{int(time.time() * 1000)}"
        group = DesignGroup(group_id=group_id, name=name,
                             design_ids=list(design_ids),
                             counts=dict(counts or {}))
        self.groups[group_id] = group
        self.save()
        return group

    def update_group(self, group_id: str, name: str = None,
                     design_ids: List[str] = None, counts: Dict[str, int] = None):
        if group_id not in self.groups:
            return
        g = self.groups[group_id]
        if name is not None:
            g.name = name
        if design_ids is not None:
            g.design_ids = list(design_ids)
        if counts is not None:
            g.counts = dict(counts)
        g.__post_init__()
        self.save()

    def delete_group(self, group_id: str):
        self.groups.pop(group_id, None)
        self.save()

    def all_groups(self) -> List[DesignGroup]:
        return sorted(self.groups.values(), key=lambda g: g.name.lower())
