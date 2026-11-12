"""
report.py

Excel job report (LazyNest_JobLog.xlsx) — one workbook that grows over time:

  • Summary       one row per export, a real Excel table (filters, frozen
                  header, numeric %, colour scale, links to folder + details)
  • <run sheet>   one sheet per export: KPI tiles, settings, designs table,
                  platforms table (with links to each DXF) and a picture of
                  every platform layout
  • Design usage  running totals per design across all exports

Also renders platform layout PNGs (used in the report).
"""

import os
import io
import logging
from datetime import datetime
from typing import List, Dict

log = logging.getLogger(__name__)

REPORT_FILENAME = "LazyNest_JobLog.xlsx"      # legacy single-file name
MASTER_FILENAME = "LazyNest_Master.xlsx"      # every job ever, cumulative


def daily_report_path(report_dir: str, when=None) -> str:
    """One workbook per day: 2026-10-27.xlsx"""
    return os.path.join(report_dir, f"{(when or datetime.now()):%Y-%m-%d}.xlsx")


def master_report_path(report_dir: str) -> str:
    return os.path.join(report_dir, MASTER_FILENAME)

# Shared design colours (fill, stroke) — also used by the on-screen viewer
DESIGN_COLORS = [
    ("#FFD3E3", "#E0206A"),
    ("#D4E6FF", "#2D6CC0"),
    ("#D2F2E6", "#138A62"),
    ("#FFEBC7", "#C27400"),
    ("#E6DAFF", "#6E47C2"),
    ("#FFDCCD", "#C24E1B"),
    ("#CFEFF4", "#00778C"),
    ("#F1D7D7", "#A83232"),
    ("#E3F0C8", "#5D7F12"),
    ("#EDE0D0", "#7A5A36"),
]

ACCENT = "E0206A"
INK    = "1C1B1F"
MUTED  = "6B6870"
LINE   = "E4E1E6"
SOFT   = "F7F5F8"


def colour_map_for(results) -> Dict[str, tuple]:
    cmap, i = {}, 0
    for r in results:
        for it in r.placements:
            if it.design_id not in cmap:
                cmap[it.design_id] = DESIGN_COLORS[i % len(DESIGN_COLORS)]
                i += 1
    return cmap


# ── Layout rendering ─────────────────────────────────────────────────────────
def render_platform_png(result, colour_map: Dict[str, tuple],
                        overflow_x: float = 0.0, overflow_y: float = 0.0,
                        px_per_mm: float = 8.0, dark: bool = False) -> bytes:
    """PNG bytes of one platform layout."""
    from PIL import Image, ImageDraw

    W, H = result.platform_w, result.platform_h
    TW, TH = W + 2 * overflow_x, H + 2 * overflow_y
    pad = 14
    s = px_per_mm * 2          # draw at 2× then downsample for smooth edges
    iw, ih = int(TW * s + 2 * pad * 2), int(TH * s + 2 * pad * 2)
    bg, plate = ("#16151A", "#1F1D24") if dark else ("#FFFFFF", "#FAF9FB")
    img = Image.new("RGB", (iw, ih), bg)
    d = ImageDraw.Draw(img)
    ox, oy = pad * 2, pad * 2

    def tr(x, y):
        """mm (platform origin) → pixels; the overflow band frames the plate."""
        return (ox + (x + overflow_x) * s, oy + (H + overflow_y - y) * s)

    if overflow_x > 0 or overflow_y > 0:
        x0, y0 = tr(-overflow_x, H + overflow_y)
        x1, y1 = tr(W + overflow_x, -overflow_y)
        _dashed_rect(d, x0, y0, x1, y1, "#C9A227", width=2, dash=14)
    x0, y0 = tr(0, H); x1, y1 = tr(W, 0)
    d.rectangle([x0, y0, x1, y1], fill=plate, outline="#E0206A", width=4)

    for it in result.placements:
        fill, stroke = colour_map.get(it.design_id, ("#EEEEEE", "#888888"))
        g = it.placed_polygon
        polys = [g] if g.geom_type == 'Polygon' else list(getattr(g, 'geoms', []))
        for p in polys:
            d.polygon([tr(x, y) for x, y in p.exterior.coords], fill=fill, outline=stroke, width=3)
            for hole in p.interiors:
                d.polygon([tr(x, y) for x, y in hole.coords], fill=plate, outline=stroke, width=2)

    img = img.resize((iw // 2, ih // 2), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _dashed_rect(d, x0, y0, x1, y1, colour, width=2, dash=10):
    for (ax, ay, bx, by) in ((x0, y0, x1, y0), (x1, y0, x1, y1),
                             (x1, y1, x0, y1), (x0, y1, x0, y0)):
        length = max(abs(bx - ax), abs(by - ay))
        n = max(1, int(length // dash))
        for k in range(0, n, 2):
            t0, t1 = k / n, min(1.0, (k + 1) / n)
            d.line([(ax + (bx - ax) * t0, ay + (by - ay) * t0),
                    (ax + (bx - ax) * t1, ay + (by - ay) * t1)], fill=colour, width=width)


# ── Excel report ─────────────────────────────────────────────────────────────
SUMMARY_COLUMNS = [
    ("Date", 11, None), ("Time", 9, None), ("Job", 26, None),
    ("Platforms", 10, "0"), ("Pieces placed", 12, "0"), ("Pieces requested", 12, "0"),
    ("Unplaced", 10, "0"), ("Metal used", 11, "0.0%"), ("Best platform", 12, "0.0%"),
    ("Worst platform", 12, "0.0%"), ("Metal area mm²", 14, "#,##0.0"),
    ("Platform W mm", 12, "0.0"), ("Platform H mm", 12, "0.0"), ("Margin mm", 10, "0.00"),
    ("Edge clearance mm", 14, "0.00"), ("Overflow X mm", 12, "0.0"), ("Overflow Y mm", 12, "0.0"),
    ("Rotation", 14, None), ("Holes nesting", 11, None), ("Search", 11, None),
    ("Output folder", 36, None), ("Details", 16, None),
]


def write_report(report_dir: str, job, results, queue_items, library,
                 written_files: List[str], out_dir: str,
                 unplaced: List[str] = None, run_seconds: float = 0.0) -> tuple:
    """
    Append this export to the report workbook. Returns (ok, message, path).
    """
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.formatting.rule import ColorScaleRule
        from openpyxl.drawing.image import Image as XLImage
    except Exception as ex:
        return (False, f"openpyxl not available: {ex}", "")

    os.makedirs(report_dir, exist_ok=True)
    now = datetime.now()
    path = daily_report_path(report_dir, now)
    unplaced = unplaced or []

    try:
        wb = openpyxl.load_workbook(path) if os.path.isfile(path) else openpyxl.Workbook()
    except Exception as ex:
        return (False, f"Could not open existing report: {ex}", path)
    if "Sheet" in wb.sheetnames and len(wb.sheetnames) == 1 and \
            wb["Sheet"].max_row == 1 and wb["Sheet"]["A1"].value is None:
        del wb["Sheet"]

    # Styles
    f_title = Font(name="Calibri", size=18, bold=True, color=INK)
    f_sub   = Font(name="Calibri", size=10, color=MUTED)
    f_h     = Font(name="Calibri", size=11, bold=True, color=INK)
    f_th    = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    f_kpi   = Font(name="Calibri", size=20, bold=True, color=ACCENT)
    f_kpil  = Font(name="Calibri", size=9, color=MUTED)
    f_link  = Font(name="Calibri", size=10, color="2D6CC0", underline="single")
    fill_th   = PatternFill("solid", fgColor=ACCENT)
    fill_soft = PatternFill("solid", fgColor=SOFT)
    thin = Side(style="thin", color=LINE)
    box  = Border(left=thin, right=thin, top=thin, bottom=thin)
    bottom = Border(bottom=thin)
    left = Alignment(horizontal="left", vertical="center")
    center = Alignment(horizontal="center", vertical="center")

    # ── Numbers ──
    W, H = job.platform_w, job.platform_h
    plat_area = W * H
    n_plat = len(results)
    placed = sum(r.piece_count for r in results)
    requested = placed + len(unplaced)
    metal = sum(r.used_area for r in results)
    used_frac = metal / (plat_area * n_plat) if n_plat and plat_area else 0.0
    effs = [r.efficiency / 100.0 for r in results]
    ox, oy = job.effective_overflow()
    from .job_manager import ROTATION_MODES, QUALITY_MODES
    rot_label = ROTATION_MODES.get(job.rotation_mode, (job.rotation_mode,))[0]
    q_label = QUALITY_MODES.get(job.quality, (job.quality,))[0].split(" ·")[0]

    # ── Run sheet ──
    base = f"{now:%y%m%d-%H%M} {job.name}"
    for ch in '[]:*?/\\':
        base = base.replace(ch, "-")
    name = base[:31]
    k = 2
    while name in wb.sheetnames:
        suffix = f" ({k})"
        name = base[:31 - len(suffix)] + suffix
        k += 1
    ws = wb.create_sheet(name)
    ws.sheet_view.showGridLines = False
    for col, w in zip("ABCDEFGHI", (24, 14, 14, 14, 16, 16, 40, 4, 4)):
        ws.column_dimensions[col].width = w

    ws["A1"] = "LazyNest · Nesting report"; ws["A1"].font = f_title
    ws["A2"] = f"{job.name}   ·   {now:%d %b %Y, %H:%M}"; ws["A2"].font = f_sub
    ws.row_dimensions[1].height = 28

    # KPI tiles (row 4-5)
    kpis = [("Platforms", n_plat, "0"), ("Pieces placed", placed, "0"),
            ("Metal used", used_frac, "0.0%"), ("Unplaced", len(unplaced), "0")]
    for i, (lbl, val, fmt) in enumerate(kpis):
        c = ws.cell(row=4, column=1 + i, value=val)
        c.font = f_kpi if not (lbl == "Unplaced" and val) else Font(size=20, bold=True, color="C0392B")
        c.number_format = fmt; c.alignment = left; c.fill = fill_soft
        l = ws.cell(row=5, column=1 + i, value=lbl.upper())
        l.font = f_kpil; l.alignment = left; l.fill = fill_soft
    ws.row_dimensions[4].height = 30

    def section(row, title):
        c = ws.cell(row=row, column=1, value=title.upper())
        c.font = Font(size=10, bold=True, color=ACCENT)
        for col in range(1, 8):
            ws.cell(row=row, column=col).border = bottom
        return row + 1

    def header(row, labels):
        for i, lbl in enumerate(labels):
            c = ws.cell(row=row, column=1 + i, value=lbl)
            c.font = f_th; c.fill = fill_th; c.alignment = center
        return row + 1

    # Settings
    r = section(7, "Settings")
    settings = [
        ("Platform", f"{W:g} × {H:g} mm"), ("Margin between outlines", f"{job.margin_mm:g} mm"),
        ("Edge clearance", f"{job.edge_clearance:g} mm"),
        ("Overflow (right / top)", f"{ox:g} / {oy:g} mm" if (ox or oy) else "Off"),
        ("Rotation", rot_label), ("Nest inside holes", "Yes" if job.allow_holes else "No"),
        ("Keep group sets together", "Yes" if getattr(job, "keep_groups", True) else "No"),
        ("Search", f"{q_label} ({run_seconds:.0f} s)" if run_seconds else q_label),
        ("Output folder", out_dir),
    ]
    for lbl, val in settings:
        ws.cell(row=r, column=1, value=lbl).font = Font(size=10, color=MUTED)
        c = ws.cell(row=r, column=2, value=val); c.font = Font(size=10, color=INK)
        if lbl == "Output folder" and out_dir:
            c.hyperlink = "file:///" + out_dir.replace("\\", "/").lstrip("/")
            c.font = f_link
        r += 1

    # Designs
    cmap = colour_map_for(results)
    r = section(r + 1, "Designs")
    r = header(r, ["Design", "Requested", "Placed", "Size (mm)", "Area / piece mm²",
                   "Total area mm²", "Share of metal"])
    placed_by: Dict[str, int] = {}
    for res in results:
        for it in res.placements:
            placed_by[it.design_id] = placed_by.get(it.design_id, 0) + 1
    design_rows = _design_rows(queue_items, library)
    first_design_row = r
    for did, dname, req in design_rows:
        e = library.designs.get(did) if library else None
        area = e.area_mm2 if e and e.area_mm2 else 0.0
        pl = placed_by.get(did, 0)
        vals = [dname, req, pl, f"{e.width_mm:.1f} × {e.height_mm:.1f}" if e else "—",
                area, area * pl, (area * pl / metal) if metal else 0]
        fmts = [None, "0", "0", None, "#,##0.0", "#,##0.0", "0.0%"]
        for i, (v, fm) in enumerate(zip(vals, fmts)):
            c = ws.cell(row=r, column=1 + i, value=v); c.border = box
            c.font = Font(size=10, color=INK)
            if fm: c.number_format = fm
        sw = cmap.get(did)
        if sw:
            ws.cell(row=r, column=1).fill = PatternFill("solid", fgColor=sw[0].lstrip("#"))
        if pl < req:
            ws.cell(row=r, column=3).font = Font(size=10, bold=True, color="C0392B")
        r += 1
    if r == first_design_row:
        r += 1

    # Platforms
    r = section(r + 1, "Platforms")
    r = header(r, ["Platform", "Pieces", "Metal used", "Metal area mm²", "Overflow used",
                   "DXF file", "Designs on platform"])
    files_by_idx = {}
    for fpath in written_files:
        for res in results:
            if os.path.basename(fpath).endswith(f"_platform_{res.platform_index + 1:02d}.dxf"):
                files_by_idx[res.platform_index] = fpath
    if not files_by_idx and written_files:
        # one file holds every platform — point all rows at it
        for res in results:
            files_by_idx[res.platform_index] = written_files[0]
    plat_first = r
    for res in results:
        counts: Dict[str, int] = {}
        for it in res.placements:
            counts[it.design_name] = counts.get(it.design_name, 0) + 1
        ovf = []
        if res.overflow_x > 0.001: ovf.append(f"→ {res.overflow_x:.2f} mm")
        if res.overflow_y > 0.001: ovf.append(f"↑ {res.overflow_y:.2f} mm")
        fpath = files_by_idx.get(res.platform_index, "")
        vals = [res.platform_index + 1, res.piece_count, res.efficiency / 100.0, res.used_area,
                ", ".join(ovf) or "—", os.path.basename(fpath) if fpath else "—",
                ", ".join(f"{n} ×{c}" for n, c in sorted(counts.items()))]
        fmts = ["0", "0", "0.0%", "#,##0.0", None, None, None]
        for i, (v, fm) in enumerate(zip(vals, fmts)):
            c = ws.cell(row=r, column=1 + i, value=v); c.border = box
            c.font = Font(size=10, color=INK); c.alignment = Alignment(vertical="center", wrap_text=(i == 6))
            if fm: c.number_format = fm
        if fpath:
            c = ws.cell(row=r, column=6)
            c.hyperlink = "file:///" + fpath.replace("\\", "/").lstrip("/")
            c.font = f_link
        r += 1
    if n_plat:
        ws.conditional_formatting.add(
            f"C{plat_first}:C{r - 1}",
            ColorScaleRule(start_type="num", start_value=0.4, start_color="F8D7DA",
                           mid_type="num", mid_value=0.65, mid_color="FFF3CD",
                           end_type="num", end_value=0.85, end_color="D4EDDA"))
    # Total row
    tot = [("TOTAL", None), (placed, "0"), (used_frac, "0.0%"), (metal, "#,##0.0")]
    for i, (v, fm) in enumerate(tot):
        c = ws.cell(row=r, column=1 + i, value=v); c.font = Font(size=10, bold=True, color=INK)
        c.border = Border(top=Side(style="medium", color=INK))
        if fm: c.number_format = fm
    r += 1

    if unplaced:
        r += 1
        c = ws.cell(row=r, column=1, value="Did not fit: " + ", ".join(
            f"{n} ×{unplaced.count(n)}" for n in sorted(set(unplaced))))
        c.font = Font(size=10, bold=True, color="C0392B")
        r += 1

    # Layout pictures
    r = section(r + 1, "Layouts")
    try:
        img_row = r
        for i, res in enumerate(results):
            # ~440 px wide so two pictures sit side by side (columns A–D, E–G)
            png = render_platform_png(res, cmap, ox, oy,
                                      px_per_mm=max(1.5, min(8.0, 420.0 / (W + 2 * ox))))
            xl = XLImage(io.BytesIO(png))
            col = "A" if i % 2 == 0 else "E"
            ws.cell(row=img_row, column=1 if i % 2 == 0 else 5,
                    value=f"Platform {res.platform_index + 1}  ·  {res.efficiency:.1f}%").font = f_h
            ws.add_image(xl, f"{col}{img_row + 1}")
            if i % 2 == 1 or i == len(results) - 1:
                rows_needed = int(xl.height / 20) + 3
                img_row += rows_needed
        r = img_row
    except Exception as ex:
        log.warning("Could not embed layout images: %s", ex)

    ws.sheet_properties.tabColor = ACCENT

    row_values = _summary_values(job, now, n_plat, placed, requested, len(unplaced),
                                 used_frac, max(effs) if effs else 0,
                                 min(effs) if effs else 0, metal, ox, oy,
                                 rot_label, q_label, out_dir)

    # ── the day's own Summary and Design usage, kept at the end ──
    _append_summary(wb, row_values, out_dir, name)
    _update_usage(wb, results, library, now)
    _order_sheets(wb, name)

    try:
        wb.save(path)
    except PermissionError:
        return (False, f"Report not saved — close {os.path.basename(path)} in Excel "
                       f"and export again.", path)
    except Exception as ex:
        return (False, f"Report not saved: {ex}", path)

    # ── the cumulative workbook for the whole shop ──
    try:
        _write_master(report_dir, row_values, out_dir, results, library, now)
    except PermissionError:
        return (True, f"Day report saved; {MASTER_FILENAME} is open in Excel "
                      f"and was not updated.", path)
    except Exception as ex:
        log.warning("master report failed: %s", ex)
        return (True, f"Day report saved; cumulative file failed: {ex}", path)
    return (True, "Report updated", path)


def _order_sheets(wb, newest: str):
    """Job sheets first (newest last), then Summary, then Design usage."""
    tail = ["Summary", "Design usage"]

    def key(sheet):
        if sheet.title in tail:
            return (2, tail.index(sheet.title))
        return (1, 1 if sheet.title == newest else 0)

    wb._sheets.sort(key=key)
    wb.active = 0


def _design_rows(queue_items, library):
    """[(design_id, name, requested)] — group rows expand into their recipe."""
    rows: Dict[str, list] = {}
    for it in queue_items:
        if getattr(it, 'group_id', ""):
            grp = library.groups.get(it.group_id) if library else None
            if not grp:
                continue
            override = getattr(it, 'member_qty', None) or {}
            for did, per_set in grp.members():
                qty = int(override.get(did, per_set))
                if qty <= 0:
                    continue
                e = library.designs.get(did)
                nm = e.display_name if e else did
                rows.setdefault(did, [did, nm, 0])[2] += qty * it.quantity
        else:
            rows.setdefault(it.design_id, [it.design_id, it.design_name, 0])[2] += it.quantity
    return [tuple(v) for v in rows.values()]


def _summary_values(job, now, n_plat, placed, requested, n_unpl, used_frac,
                    best, worst, metal, ox, oy, rot_label, q_label, out_dir) -> list:
    """One Summary row, in SUMMARY_COLUMNS order (without the two links)."""
    return [now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), job.name, n_plat, placed,
            requested, n_unpl, used_frac, best, worst, metal, job.platform_w,
            job.platform_h, job.margin_mm, job.edge_clearance, ox, oy, rot_label,
            "Yes" if job.allow_holes else "No", q_label, out_dir, "Open sheet →"]


def _summary_sheet(wb, title="Summary"):
    """The Summary sheet, created at the end of the workbook if missing."""
    from openpyxl.styles import Font, Alignment
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.utils import get_column_letter

    if title in wb.sheetnames:
        return wb[title]
    ws = wb.create_sheet(title)
    ws.sheet_view.showGridLines = False
    for i, (label, width, _) in enumerate(SUMMARY_COLUMNS, start=1):
        c = ws.cell(row=1, column=i, value=label)
        c.font = Font(bold=True, color="FFFFFF")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "D2"
    ws.sheet_properties.tabColor = INK
    ws.conditional_formatting.add(
        "H2:H5000",
        ColorScaleRule(start_type="num", start_value=0.4, start_color="F8D7DA",
                       mid_type="num", mid_value=0.65, mid_color="FFF3CD",
                       end_type="num", end_value=0.85, end_color="D4EDDA"))
    return ws


def _append_summary(wb, values, out_dir, sheet_name=None, table="RunLog"):
    from openpyxl.styles import Font
    from openpyxl.worksheet.table import Table, TableStyleInfo
    from openpyxl.utils import get_column_letter

    ncol = len(SUMMARY_COLUMNS)
    ws = _summary_sheet(wb)
    row = ws.max_row + 1 if (ws.max_row > 1 or ws["A2"].value) else 2
    for i, (v, (_, _, fmt)) in enumerate(zip(values, SUMMARY_COLUMNS), start=1):
        c = ws.cell(row=row, column=i, value=v)
        if fmt:
            c.number_format = fmt
    if out_dir:
        c = ws.cell(row=row, column=ncol - 1)
        c.hyperlink = "file:///" + out_dir.replace("\\", "/").lstrip("/")
        c.font = Font(color="2D6CC0", underline="single")
    c = ws.cell(row=row, column=ncol)
    if sheet_name:
        c.hyperlink = f"#'{sheet_name}'!A1"
        c.font = Font(color="2D6CC0", underline="single")
    else:
        c.value = ""
    if values[6]:
        ws.cell(row=row, column=7).font = Font(bold=True, color="C0392B")

    ref = f"A1:{get_column_letter(ncol)}{row}"
    if table in ws.tables:
        ws.tables[table].ref = ref
        ws.tables[table].autoFilter.ref = ref
    else:
        t = Table(displayName=table, ref=ref)
        t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        ws.add_table(t)


def _write_master(report_dir, row_values, out_dir, results, library, now):
    """Cumulative workbook: every run ever, plus total design usage."""
    import openpyxl
    path = master_report_path(report_dir)
    wb = openpyxl.load_workbook(path) if os.path.isfile(path) else openpyxl.Workbook()
    if "Sheet" in wb.sheetnames and wb["Sheet"].max_row == 1 \
            and wb["Sheet"]["A1"].value is None:
        del wb["Sheet"]
    _append_summary(wb, row_values, out_dir, sheet_name=None, table="AllRuns")
    _update_usage(wb, results, library, now)
    _order_sheets(wb, "")
    wb.save(path)


def _update_usage(wb, results, library, now):
    from openpyxl.styles import Font, Alignment
    from openpyxl.worksheet.table import Table, TableStyleInfo
    from openpyxl.utils import get_column_letter

    cols = [("Design", 28, None), ("Exports", 10, "0"), ("Pieces nested", 14, "0"),
            ("Metal area mm²", 16, "#,##0.0"), ("Last used", 14, None)]
    data: Dict[str, list] = {}
    if "Design usage" in wb.sheetnames:
        old = wb["Design usage"]
        for row in old.iter_rows(min_row=2, values_only=True):
            if row and row[0]:
                data[row[0]] = [row[0], row[1] or 0, row[2] or 0, row[3] or 0.0, row[4] or ""]
        del wb["Design usage"]
    this_run: Dict[str, list] = {}
    for r in results:
        for it in r.placements:
            e = this_run.setdefault(it.design_name, [0, 0.0])
            e[0] += 1
            e[1] += it.area
    for nm, (cnt, area) in this_run.items():
        d = data.setdefault(nm, [nm, 0, 0, 0.0, ""])
        d[1] += 1; d[2] += cnt; d[3] += area; d[4] = now.strftime("%Y-%m-%d")

    ws = wb.create_sheet("Design usage", 1)
    ws.sheet_view.showGridLines = False
    for i, (label, width, _) in enumerate(cols, start=1):
        c = ws.cell(row=1, column=i, value=label)
        c.font = Font(bold=True, color="FFFFFF")
        c.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(i)].width = width
    rows = sorted(data.values(), key=lambda v: (-v[2], v[0]))
    for ri, v in enumerate(rows, start=2):
        for ci, (val, (_, _, fmt)) in enumerate(zip(v, cols), start=1):
            c = ws.cell(row=ri, column=ci, value=val)
            if fmt:
                c.number_format = fmt
    last = max(2, len(rows) + 1)
    t = Table(displayName="DesignUsage", ref=f"A1:E{last}")
    t.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
    ws.add_table(t)
    ws.freeze_panes = "A2"
