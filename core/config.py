"""
config.py

Application configuration. Persisted to config.json.
Also handles Rhino executable detection and launch.
"""

import os
import sys
import json
import subprocess
import logging
from dataclasses import dataclass, asdict

log = logging.getLogger(__name__)

CONFIG_FILE = "config.json"

# Common Rhino install paths to probe (Windows + Mac)
RHINO_SEARCH_PATHS = [
    # Windows
    r"C:\Program Files\Rhino 8\System\Rhino.exe",
    r"C:\Program Files\Rhino 7\System\Rhino.exe",
    r"C:\Program Files\Rhino 6\System\Rhino.exe",
    r"C:\Program Files (x86)\Rhino 8\System\Rhino.exe",
    # Mac
    "/Applications/RhinoWIP.app/Contents/MacOS/Rhinoceros",
    "/Applications/Rhino 8.app/Contents/MacOS/Rhinoceros",
    "/Applications/Rhino 7.app/Contents/MacOS/Rhinoceros",
    "/Applications/Rhino 6.app/Contents/MacOS/Rhinoceros",
    "/Applications/Rhinoceros 5.app/Contents/MacOS/Rhinoceros",
]


@dataclass
class AppConfig:
    library_folder:  str   = ""
    output_folder:   str   = ""
    report_folder:   str   = ""
    last_platform_w: float = 110.0
    last_platform_h: float = 52.0
    last_margin_mm:  float = 0.6
    last_rotation:   str   = "0+180"
    last_keep_groups: bool  = True
    rhino_path:      str   = ""   # empty = not found / not configured
    window_w:        int   = 1100
    window_h:        int   = 720
    last_job_id:     str   = ""
    theme:           str   = "light"      # "light" | "dark"


RHINO_FOLDER = "Rhino"
CAM_FOLDER   = "Prisma & Solanki"


def output_folders(base: str, when=None) -> dict:
    """
    <output folder>/
        2026-10-27/              ← the day's work
            Rhino/               ← our normal DXFs
            Prisma & Solanki/    ← R12 DXF, R2004 DXF and SVG
    """
    from datetime import datetime
    day = (when or datetime.now()).strftime("%Y-%m-%d")
    root = os.path.join(native_path(base), day)
    return {"root": root,
            "rhino": os.path.join(root, RHINO_FOLDER),
            "cam": os.path.join(root, CAM_FOLDER)}


def native_path(p: str) -> str:
    """Absolute path with the OS's own separators (Qt hands back '/' on Windows)."""
    if not p:
        return p
    return os.path.normpath(os.path.abspath(os.path.expanduser(p.strip())))


class ConfigManager:
    def __init__(self, config_dir: str):
        self.config_dir  = config_dir
        self.config_path = os.path.join(config_dir, CONFIG_FILE)
        self.cfg = AppConfig()
        self._load()
        if not self.cfg.rhino_path:
            self._detect_rhino()

    def _load(self):
        if not os.path.isfile(self.config_path):
            return
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for k, v in data.items():
                if hasattr(self.cfg, k):
                    setattr(self.cfg, k, v)
        except Exception as ex:
            log.warning("Could not load config: %s", ex)

    def save(self):
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(asdict(self.cfg), f, indent=2)
        except Exception as ex:
            log.error("Could not save config: %s", ex)

    def _detect_rhino(self):
        for path in RHINO_SEARCH_PATHS:
            if os.path.isfile(path):
                self.cfg.rhino_path = path
                self.save()
                log.info("Rhino found at: %s", path)
                return
        log.info("Rhino not auto-detected")

    # ──────────────────────────────────────────────────────
    # Rhino launcher
    # ──────────────────────────────────────────────────────

    def open_in_rhino(self, file_paths) -> bool:
        """
        Open one or more DXF files in Rhino in a single session.
        Accepts a string (single file) or list of strings (multiple files).
        All files are passed as arguments to one Rhino process — they open
        as separate views/tabs in the same window.
        Returns True if launched, False otherwise. Never raises.
        """
        try:
            # Normalise to list
            if isinstance(file_paths, str):
                file_paths = [file_paths]
            # Filter to files that actually exist
            valid = [native_path(p) for p in file_paths if p and os.path.isfile(p)]
            if not valid:
                log.debug("open_in_rhino: no valid files found")
                return False

            rhino = self.cfg.rhino_path
            if not rhino or not os.path.isfile(rhino):
                # Fall back to OS default for the first file only
                self._open_with_default(valid[0])
                return False

            if sys.platform == 'darwin':
                # macOS 'open -a App file1 file2 ...' opens all in one instance
                app_bundle = rhino
                for _ in range(3):
                    app_bundle = os.path.dirname(app_bundle)
                subprocess.Popen(['open', '-a', app_bundle] + valid,
                                  close_fds=True)
            else:
                # Windows: Rhino.exe file1.dxf file2.dxf ...
                # All files open in one Rhino window
                CREATE_NO_WINDOW = 0x08000000
                subprocess.Popen(
                    [rhino] + valid,
                    close_fds=True,
                    creationflags=CREATE_NO_WINDOW,
                )

            log.info("Opened %d file(s) in Rhino", len(valid))
            return True

        except Exception as ex:
            log.debug("open_in_rhino failed (non-fatal): %s", ex)
            return False

    def _open_with_default(self, file_path: str):
        """Try OS default app — silently does nothing on failure."""
        try:
            file_path = native_path(file_path)
            if sys.platform == 'darwin':
                subprocess.Popen(['open', file_path], close_fds=True)
            elif sys.platform == 'win32':
                os.startfile(file_path)
            else:
                subprocess.Popen(['xdg-open', file_path], close_fds=True)
        except Exception as ex:
            log.debug("_open_with_default failed: %s", ex)

    def open_folder(self, folder_path: str) -> bool:
        """
        Open a folder in the system file manager. Returns False if the folder
        does not exist or could not be opened.

        Windows note: Explorer ignores paths with forward slashes (which is
        what Qt's folder picker returns) and silently opens 'Documents'
        instead — so the path is always converted to native form first.
        """
        try:
            folder_path = native_path(folder_path)
            if not folder_path or not os.path.isdir(folder_path):
                return False
            if sys.platform == 'darwin':
                subprocess.Popen(['open', folder_path], close_fds=True)
            elif sys.platform == 'win32':
                os.startfile(folder_path)
            else:
                subprocess.Popen(['xdg-open', folder_path], close_fds=True)
            return True
        except Exception as ex:
            log.debug("open_folder failed: %s", ex)
            return False
