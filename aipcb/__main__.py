"""CLI:  python -m aipcb <command> <project> [options]

commands: build | library | validate | schematic | erc | place | route | pcb | drc | sim | checks |
          previews | fab [--force] | status | gui | mcp

archive (one folder + .zip with the whole project and readable documents):
  export <project> [--to DIR] [--no-zip]          default DIR: ./exports
  import <folder|.zip|manifest.json> [--name NEW] [--overwrite]

placement locks (build / place):
  --keep               keep every part at its previous position; only new parts are placed
  --keep U1,J2         keep just these parts
  --unlock R5,C3       with --keep: re-place these parts
  design.yaml `place: {lock: true}` locks a part permanently; explicit place.x/y always wins
"""
from __future__ import annotations

import argparse
import json
import sys

from . import pipeline as P


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aipcb")
    ap.add_argument("command")
    ap.add_argument("project", nargs="?", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--tries", type=int, default=3)
    ap.add_argument("--moves", type=int, default=30000)
    ap.add_argument("--keep", nargs="?", const="all", default=None)
    ap.add_argument("--unlock", default="")
    ap.add_argument("--to", default=None, help="export: destination folder")
    ap.add_argument("--no-zip", action="store_true", help="export: folder only, no .zip")
    ap.add_argument("--name", default=None, help="import: save under a new project name")
    ap.add_argument("--overwrite", action="store_true", help="import: replace an existing project (it is backed up)")
    a = ap.parse_args(argv)
    if a.command in ("export", "import"):
        if not a.project:
            ap.error(f"{a.command} 需要專案名稱或路徑")
        from . import archive
        try:
            res = (archive.export_project(P.project_dir(a.project), a.to, make_zip=not a.no_zip) if a.command == "export"
                   else archive.import_project(a.project, name=a.name, overwrite=a.overwrite))
        except (OSError, ValueError) as e:
            print(f"{a.command} 失敗：{e}", file=sys.stderr)
            return 1
        print(json.dumps(res, indent=1, ensure_ascii=False))
        return 0
    keep = None if a.keep is None else (True if a.keep == "all" else [r for r in a.keep.split(",") if r])
    unlock = [r for r in a.unlock.split(",") if r]
    cmd = a.command
    if cmd == "gui":                       # no project given: the GUI reopens the last closed project
        from .gui.app import main as gui_main
        return gui_main(str(P.project_dir(a.project)) if a.project else None)
    pd = P.project_dir(a.project or "fm_radio")
    if cmd == "mcp":
        from .mcp_server import main as mcp_main
        return mcp_main()
    fn = {
        "build": lambda: P.build(pd, place_tries=a.tries, moves=a.moves, keep=keep, unlock=unlock),
        "library": lambda: P.build_library(pd),
        "validate": lambda: P.validate(pd),
        "schematic": lambda: P.make_schematic(pd),
        "erc": lambda: P.erc(pd),
        "place": lambda: P.place(pd, seed=a.seed, tries=a.tries, moves=a.moves, keep=keep, unlock=unlock),
        "route": lambda: P.route(pd),
        "pcb": lambda: P.make_pcb(pd),
        "drc": lambda: P.drc(pd),
        "sim": lambda: P.simulate_all(pd),
        "checks": lambda: P.run_checks(pd),
        "previews": lambda: P.make_previews(pd),
        "fab": lambda: P.export_fab(pd, force=a.force),
        "status": lambda: P._status(pd),
        "review-package": lambda: P.review_package(pd),
    }.get(cmd)
    if fn is None:
        ap.error(f"unknown command {cmd}")
    res = fn()
    print(json.dumps(res, indent=1, default=str, ensure_ascii=False)[:20000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
