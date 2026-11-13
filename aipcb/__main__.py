"""CLI:  python -m aipcb <command> <project> [options]

commands: build | library | validate | schematic | erc | place | route | pcb | drc | sim | checks |
          previews | fab [--force] | status | gui | mcp

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
    ap.add_argument("project", nargs="?", default="fm_radio")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--tries", type=int, default=3)
    ap.add_argument("--moves", type=int, default=30000)
    ap.add_argument("--keep", nargs="?", const="all", default=None)
    ap.add_argument("--unlock", default="")
    a = ap.parse_args(argv)
    keep = None if a.keep is None else (True if a.keep == "all" else [r for r in a.keep.split(",") if r])
    unlock = [r for r in a.unlock.split(",") if r]
    pd = P.project_dir(a.project)
    cmd = a.command
    if cmd == "gui":
        from .gui.app import main as gui_main
        return gui_main(str(pd))
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
