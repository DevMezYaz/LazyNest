"""
job_manager.py

Manages batch jobs: saved job configs, priority queue, master CSV log.
"""

import os
import csv
import json
import time
import logging
from dataclasses import dataclass, field, asdict, fields
from typing import Dict, List, Optional
from datetime import datetime

log = logging.getLogger(__name__)

JOBS_FILE  = "jobs.json"
LOG_FILE   = "nesting_log.csv"

LOG_COLUMNS = [
    "date",
    "time",
    "job_name",
    "platform_w_mm",
    "platform_h_mm",
    "margin_mm",
    "rotation_mode",
    "total_pieces",
    "platforms_used",
    "avg_efficiency_pct",
    "min_efficiency_pct",
    "max_efficiency_pct",
    "edge_clearance_mm",
    "overflow_x_mm",
    "overflow_y_mm",
    "holes_nesting",
    "groups_kept_together",
    "metal_area_mm2",           # total outline area placed (outlines minus holes)
    "platform_area_mm2",
    "total_platform_area_mm2",
    "designs_summary",          # "Ring A×12, Pendant B×6"
    "output_folder",
    "output_files",             # comma-separated filenames
    "pieces_per_platform",      # "P1:8, P2:7, P3:7"
    "efficiency_per_platform",  # "P1:82%, P2:79%, P3:76%"
    "unplaced_pieces",
]


@dataclass
class JobItem:
    design_id:   str
    design_name: str
    quantity:    int
    priority:    int = 0    # higher = placed first; default 0 = normal
    group_id:    str = ""   # set → this row is a group; quantity = number of sets
    member_qty:  Dict[str, int] = field(default_factory=dict)
    # member_qty: per-set quantities for THIS job only; empty = the group's own

    @property
    def is_group(self) -> bool:
        return bool(self.group_id)


ROTATION_MODES = {
    "0":             ("0° only",               [0.0]),
    "0+180":         ("0° / 180°",             [0.0, 180.0]),
    "0+90+180+270":  ("Every 90°",             [0.0, 90.0, 180.0, 270.0]),
    "free":          ("Free (every 15°)",      [float(a) for a in range(0, 360, 15)]),
}

QUALITY_MODES = {
    "quick":    ("Quick · ~5 s",     5.0),
    "balanced": ("Balanced · ~30 s", 30.0),
    "thorough": ("Thorough · ~2 min", 120.0),
    "nonstop":  ("Nonstop · until you press Stop", float("inf")),
}


@dataclass
class SavedJob:
    job_id:       str
    name:         str
    platform_w:   float = 110.0
    platform_h:   float = 52.0
    margin_mm:    float = 0.6
    rotation_mode: str  = "0+180"   # key of ROTATION_MODES
    items:        List[JobItem] = field(default_factory=list)
    created_at:   str = ""
    last_run:     str = ""
    edge_clearance:   float = 0.0
    overflow_enabled: bool  = False
    overflow_x:       float = 0.0   # mm allowed past the right edge
    overflow_y:       float = 0.0   # mm allowed past the top edge
    allow_holes:      bool  = False
    keep_groups:      bool  = True   # one set of a group stays on one platform
    quality:          str   = "quick"

    def rotation_angles(self):
        return list(ROTATION_MODES.get(self.rotation_mode, ROTATION_MODES["0+180"])[1])

    def effective_overflow(self):
        if not self.overflow_enabled:
            return 0.0, 0.0
        return max(0.0, self.overflow_x), max(0.0, self.overflow_y)


class JobManager:
    def __init__(self, config_dir: str):
        self.config_dir = config_dir
        self.jobs_path  = os.path.join(config_dir, JOBS_FILE)
        self.log_path   = os.path.join(config_dir, LOG_FILE)
        self.jobs: Dict[str, SavedJob] = {}
        self._load()

    # ──────────────────────────────────────────────────────
    # Persistence
    # ──────────────────────────────────────────────────────

    def _load(self):
        if not os.path.isfile(self.jobs_path):
            return
        try:
            with open(self.jobs_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            job_keys = {f.name for f in fields(SavedJob)} - {'items'}
            item_keys = {f.name for f in fields(JobItem)}
            for jd in data.get('jobs', []):
                items = [JobItem(**{k: v for k, v in it.items() if k in item_keys})
                         for it in jd.pop('items', [])]
                j = SavedJob(**{k: v for k, v in jd.items() if k in job_keys}, items=items)
                self.jobs[j.job_id] = j
        except Exception as ex:
            log.warning("Could not load jobs: %s", ex)

    def save(self):
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            data = {'jobs': []}
            for j in self.jobs.values():
                jd = asdict(j)
                data['jobs'].append(jd)
            with open(self.jobs_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as ex:
            log.error("Could not save jobs: %s", ex)

    # ──────────────────────────────────────────────────────
    # Job CRUD
    # ──────────────────────────────────────────────────────

    def create_job(self, name: str, platform_w: float = 110.0,
                   platform_h: float = 52.0, margin_mm: float = 0.6,
                   rotation_mode: str = "0+180") -> SavedJob:
        job_id = f"job_{int(time.time() * 1000)}"
        now    = datetime.now().isoformat(timespec='seconds')
        job = SavedJob(
            job_id=job_id,
            name=name,
            platform_w=platform_w,
            platform_h=platform_h,
            margin_mm=margin_mm,
            rotation_mode=rotation_mode,
            created_at=now,
        )
        self.jobs[job_id] = job
        self.save()
        return job

    def update_job(self, job_id: str, **kwargs):
        if job_id not in self.jobs:
            return
        j = self.jobs[job_id]
        for k, v in kwargs.items():
            if hasattr(j, k):
                setattr(j, k, v)
        self.save()

    def delete_job(self, job_id: str):
        self.jobs.pop(job_id, None)
        self.save()

    def duplicate_job(self, job_id: str, new_name: str) -> Optional[SavedJob]:
        if job_id not in self.jobs:
            return None
        src = self.jobs[job_id]
        new_job = self.create_job(new_name)
        for f in fields(SavedJob):
            if f.name not in ('job_id', 'name', 'created_at', 'last_run', 'items'):
                setattr(new_job, f.name, getattr(src, f.name))
        new_job.items = [JobItem(**asdict(it)) for it in src.items]
        self.save()
        return new_job

    def all_jobs(self) -> List[SavedJob]:
        return sorted(self.jobs.values(), key=lambda j: j.name.lower())

    # ──────────────────────────────────────────────────────
    # Job items (priority-aware)
    # ──────────────────────────────────────────────────────

    def set_items(self, job_id: str, items: List[JobItem]):
        if job_id in self.jobs:
            self.jobs[job_id].items = items
            self.save()

    def rename_design_refs(self, old_id: str, new_id: str, new_name: str = None):
        """Keep saved jobs pointing at a design after its file was renamed."""
        changed = False
        for j in self.jobs.values():
            for it in j.items:
                if not it.group_id and it.design_id == old_id:
                    it.design_id = new_id
                    if new_name:
                        it.design_name = new_name
                    changed = True
        if changed:
            self.save()

    def sorted_items(self, job_id: str) -> List[JobItem]:
        """Return job items sorted by priority descending."""
        if job_id not in self.jobs:
            return []
        return sorted(self.jobs[job_id].items,
                       key=lambda it: it.priority, reverse=True)

    # ──────────────────────────────────────────────────────
    # Master CSV log
    # ──────────────────────────────────────────────────────

    def _ensure_log_header(self):
        if not os.path.isfile(self.log_path):
            os.makedirs(self.config_dir, exist_ok=True)
            with open(self.log_path, 'w', newline='', encoding='utf-8') as f:
                csv.DictWriter(f, fieldnames=LOG_COLUMNS).writeheader()

    def append_log(self, job: SavedJob,
                   results,                # List[PlacementResult]
                   output_files: List[str],
                   unplaced: int = 0):
        """
        Append one row to the master CSV log.
        Called after every successful export.
        """
        try:
            self._ensure_log_header()
            self._upgrade_log_header()

            now = datetime.now()

            total_pieces   = sum(r.piece_count for r in results)
            efficiencies   = [r.efficiency for r in results]
            avg_eff  = round(sum(efficiencies) / len(efficiencies), 1) if efficiencies else 0
            min_eff  = round(min(efficiencies), 1) if efficiencies else 0
            max_eff  = round(max(efficiencies), 1) if efficiencies else 0
            plat_area = job.platform_w * job.platform_h
            metal = sum(getattr(r, 'used_area', 0.0) for r in results)
            ox, oy = job.effective_overflow()

            qty_map: Dict[str, int] = {}
            for item in job.items:
                qty_map[item.design_name] = qty_map.get(item.design_name, 0) + item.quantity
            designs_summary = ", ".join(f"{n}×{q}" for n, q in qty_map.items())

            pieces_per = ", ".join(
                f"P{r.platform_index+1}:{r.piece_count}" for r in results)
            eff_per    = ", ".join(
                f"P{r.platform_index+1}:{r.efficiency}%" for r in results)

            output_files_str = ", ".join(os.path.basename(p) for p in output_files)
            output_dir = os.path.dirname(output_files[0]) if output_files else ""

            row = {
                "date":                    now.strftime("%Y-%m-%d"),
                "time":                    now.strftime("%H:%M:%S"),
                "job_name":                job.name,
                "platform_w_mm":           job.platform_w,
                "platform_h_mm":           job.platform_h,
                "margin_mm":               job.margin_mm,
                "rotation_mode":           job.rotation_mode,
                "total_pieces":            total_pieces,
                "platforms_used":          len(results),
                "avg_efficiency_pct":      avg_eff,
                "min_efficiency_pct":      min_eff,
                "max_efficiency_pct":      max_eff,
                "edge_clearance_mm":       job.edge_clearance,
                "overflow_x_mm":           ox,
                "overflow_y_mm":           oy,
                "holes_nesting":           "yes" if job.allow_holes else "no",
                "groups_kept_together":    "yes" if job.keep_groups else "no",
                "metal_area_mm2":          round(metal, 1),
                "platform_area_mm2":       round(plat_area, 1),
                "total_platform_area_mm2": round(plat_area * len(results), 1),
                "designs_summary":         designs_summary,
                "output_folder":           output_dir,
                "output_files":            output_files_str,
                "pieces_per_platform":     pieces_per,
                "efficiency_per_platform": eff_per,
                "unplaced_pieces":         unplaced,
            }

            with open(self.log_path, 'a', newline='', encoding='utf-8') as f:
                csv.DictWriter(f, fieldnames=LOG_COLUMNS).writerow(row)

            self.update_job(job.job_id, last_run=now.isoformat(timespec='seconds'))
            log.info("Log entry written to %s", self.log_path)

        except Exception as ex:
            log.error("Could not write log entry: %s", ex)

    def _upgrade_log_header(self):
        """Rewrite an old-format CSV so its columns match LOG_COLUMNS."""
        try:
            with open(self.log_path, 'r', newline='', encoding='utf-8') as f:
                rows = list(csv.DictReader(f))
            with open(self.log_path, 'r', newline='', encoding='utf-8') as f:
                first = f.readline().strip().split(',')
            if first == LOG_COLUMNS:
                return
            with open(self.log_path, 'w', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, fieldnames=LOG_COLUMNS, extrasaction='ignore')
                w.writeheader()
                for r in rows:
                    w.writerow({k: r.get(k, "") for k in LOG_COLUMNS})
        except Exception as ex:
            log.debug("Log header upgrade skipped: %s", ex)
