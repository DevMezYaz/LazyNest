# LazyNest v2.4

Nesting for jewellery DXF files — packs pieces by their true cut outline, not
by bounding boxes, and exports for Rhino and for PRISMA / Solanki.

## Downloads

| File | For |
|---|---|
| `LazyNest-v2.4-Windows.exe` | Windows 10/11 — download and run, nothing to install |
| `LazyNest-v2.4-macOS-arm64.zip` | Mac with Apple Silicon (M1–M4) — unzip and run |

On an Intel Mac, run it from source instead: `pip install -r requirements.txt`
then `python app.py`.

Both apps are self-contained: no Python, no setup. LazyNest keeps its settings
in a `lazynest_data` folder next to the program.

**Windows.** SmartScreen says "Windows protected your PC" because the file is
not code-signed: **More info → Run anyway**.

**macOS.** The app is not signed or notarised either, and anything downloaded
from the internet is quarantined, so macOS may say it "is damaged and can't be
opened". It is not damaged — clear the quarantine flag once:

```bash
xattr -dr com.apple.quarantine /Applications/LazyNest.app
```

Or right-click the app → **Open** → **Open**.

## What's new in 2.4

**Heavy CAD files no longer freeze the app.** Files whose cut outline is left a
hair open (a few endpoints with 0.5–0.9 mm gaps) used to fall back to merging
every layer into a 34,000-vertex blob, which turned one shape comparison into
3 seconds and a 26-design job into roughly an hour of grinding.

- Gaps up to 2 mm in the cut outline are now closed automatically, but only
  between two loose ends that are each other's nearest neighbour, and only when
  it genuinely recovers the shape.
- Outlines with lots of fine detail (prongs, scallops) are matched with fewer,
  slightly larger collision parts. Parts only ever grow outward, so the gap you
  set is never undercut.
- Measured on a 26-design job: shape comparison **3 s → 0.005 s**, nesting
  **~1 hour → 20 s**, export **137 s → 43 s**.

**CAM export correctness.**

- Pieces rotated 180° are stored by DXF in a mirrored coordinate system; their
  circles and arcs were being written up to **425 mm out of place**. Fixed.
- Specks of noise in source files (curves 0.001 mm across) could fit a
  24-metre-radius circle and be drawn across the platform. Now rejected.
- Export accuracy is measured against the original every time and reported in
  the status bar — typically **within 5 µm**.
- Fixed a crash on newer NumPy ("Both input arrays must be 3-dimensional
  vectors") that aborted the export.

**Interface.**

- One **Export** button writes everything: the Rhino DXF, the PRISMA / Solanki
  set (R12 DXF, R2004 DXF, SVG) and both Excel reports.
- The mouse wheel no longer changes quantities, platform sizes or dropdowns —
  it scrolls. Quantities have − / + buttons.
- The job queue keeps the order you added things in and can be dragged to
  reorder; it no longer jumps to the top when you add a design.
- Window works down to 900 × 560 without overlapping panels.

## Where files are written

```
<output folder>/2026-11-12/Rhino/<job>.dxf
<output folder>/2026-11-12/Prisma & Solanki/<job>_R12.dxf  (+ _R2004.dxf, .svg)
<report folder>/2026-11-12.xlsx        the day's jobs, plus Summary and Design usage
<report folder>/LazyNest_Master.xlsx   cumulative across every day
```

## Building it yourself

```bash
pip install -r requirements.txt
python app.py
```

A GitHub Actions workflow (`.github/workflows/build.yml`) builds the Windows
`.exe` and the macOS app automatically — run it from the **Actions** tab, or
push a `v*` tag.
