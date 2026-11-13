"""Tool / library locations. Override with environment variables if KiCad lives elsewhere."""
from __future__ import annotations

import os
import platform
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"


def _default_kicad() -> tuple[Path, Path, Path]:
    sysname = platform.system()
    if sysname == "Darwin":
        app = Path("/Applications/KiCad/KiCad.app/Contents")
        return app / "MacOS/kicad-cli", app / "SharedSupport", app / "PlugIns/sim/libngspice.dylib"
    if sysname == "Windows":
        base = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "KiCad/10.0"
        return base / "bin/kicad-cli.exe", base / "share/kicad", base / "bin/ngspice.dll"
    return Path("/usr/bin/kicad-cli"), Path("/usr/share/kicad"), Path("/usr/lib/x86_64-linux-gnu/libngspice.so.0")


_cli, _share, _spice = _default_kicad()
KICAD_CLI = Path(os.environ.get("AIPCB_KICAD_CLI", _cli))
KICAD_SHARE = Path(os.environ.get("AIPCB_KICAD_SHARE", _share))
NGSPICE_LIB = Path(os.environ.get("AIPCB_NGSPICE", _spice))
SYMBOL_DIR = KICAD_SHARE / "symbols"
FOOTPRINT_DIR = KICAD_SHARE / "footprints"
MODEL_VAR = "${KICAD10_3DMODEL_DIR}"
