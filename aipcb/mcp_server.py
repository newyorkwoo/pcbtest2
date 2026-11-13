"""MCP server exposing the aipcb EDA flow to Claude (stdio transport, runs fully offline).

Claude designs by editing projects/<name>/design.yaml (and sim/*.cir), then calls these tools.
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
from pathlib import Path

import yaml
try:                                    # MCP python SDK 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:                     # SDK 1.x
    from mcp.server.fastmcp import FastMCP

from . import config, library
from . import pipeline as P

mcp = FastMCP("aipcb", instructions=(
    "Local EDA engine (KiCad 10 + physics placer + maze router + ngspice). Workflow: create_project -> "
    "search_symbols/get_symbol_pins/search_footprints -> write design.yaml (write_design) -> validate_design -> "
    "build_board (schematic, ERC, placement, routing, PCB, DRC, sims, checks, previews) -> fix issues -> "
    "get_review_package -> submit_review -> export_fab. Net syntax REF.PIN (pin number or name)."))


def _quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = fn(*a, **kw)
    return res


def _j(obj, limit: int = 60000) -> str:
    s = json.dumps(obj, indent=1, default=str, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "\n... (truncated)"


# ------------------------------------------------------------------ projects
@mcp.tool()
def list_projects() -> str:
    """List design projects and the state of each pipeline stage."""
    out = {}
    for p in sorted(config.PROJECTS.glob("*/design.yaml")):
        st = P._status(p.parent)
        out[p.parent.name] = {k: v.get("state") for k, v in st.items()}
    return _j(out)


@mcp.tool()
def create_project(name: str, title: str = "", width_mm: float = 60, height_mm: float = 40) -> str:
    """Create projects/<name>/ with a skeleton design.yaml."""
    pd = config.PROJECTS / name
    if (pd / "design.yaml").exists():
        return f"project {name} already exists"
    (pd / "sim").mkdir(parents=True, exist_ok=True)
    skel = {"project": name, "title": title or name, "rev": "A", "notes": "",
            "board": {"width": width_mm, "height": height_mm, "corner_radius": 2, "layers": 2, "fab": "jlcpcb"},
            "rules": {"track": 0.2, "power_track": 0.4, "clearance": 0.2, "via": [0.6, 0.3], "power_nets": []},
            "parts": {}, "nets": {}, "nc": []}
    (pd / "design.yaml").write_text(yaml.safe_dump(skel, sort_keys=False, allow_unicode=True))
    return f"created {pd}/design.yaml"


@mcp.tool()
def read_design(project: str) -> str:
    """Return the project's design.yaml text."""
    return (P.project_dir(project) / "design.yaml").read_text(encoding="utf-8")


@mcp.tool()
def write_design(project: str, yaml_text: str) -> str:
    """Replace design.yaml (validated as YAML first) and run validation."""
    yaml.safe_load(yaml_text)
    pd = P.project_dir(project)
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "design.yaml").write_text(yaml_text, encoding="utf-8")
    _quiet(P.build_library, pd)
    return _j(_quiet(P.validate, pd))


@mcp.tool()
def write_simulation(project: str, name: str, netlist: str) -> str:
    """Write projects/<p>/sim/<name>.cir. Directives: '* @title', '* @plot v(x)', '* @expect meas min max',
    '* @bandwidth node'. Use .meas statements for measurements."""
    pd = P.project_dir(project)
    (pd / "sim").mkdir(exist_ok=True)
    (pd / "sim" / f"{name}.cir").write_text(netlist)
    return f"wrote sim/{name}.cir"


# ------------------------------------------------------------------ library
@mcp.tool()
def search_symbols(query: str, project: str = "", limit: int = 30) -> str:
    """Search KiCad + project symbol libraries by keywords (e.g. 'LDO 3.3', 'USB_C receptacle')."""
    library.set_project_libs(P.project_dir(project) if project else None)
    return _j(library.search_symbols(query, limit))


@mcp.tool()
def get_symbol_pins(lib_id: str, project: str = "") -> str:
    """Pins (number, name, electrical type) and default footprint of a symbol like 'Regulator_Linear:AP2112K-3.3'."""
    library.set_project_libs(P.project_dir(project) if project else None)
    s = library.get_symbol(lib_id)
    return _j({"lib_id": lib_id, "default_footprint": s.props.get("Footprint"), "description": s.props.get("Description"),
               "units": s.units, "pins": [{"number": p.number, "name": p.name, "type": p.etype, "unit": p.unit,
                                           "hidden": p.hidden} for p in s.pins]})


@mcp.tool()
def search_footprints(query: str, project: str = "", limit: int = 40) -> str:
    """Search footprints by substring tokens (e.g. 'SOT-23-5', 'R_0603', 'USB_C HRO')."""
    library.set_project_libs(P.project_dir(project) if project else None)
    return _j(library.search_footprints(query, limit))


@mcp.tool()
def get_footprint_pads(lib_id: str, project: str = "") -> str:
    """Pads (number, type, position, size, drill) and courtyard of a footprint."""
    library.set_project_libs(P.project_dir(project) if project else None)
    f = library.get_footprint(lib_id)
    return _j({"lib_id": lib_id, "courtyard": f.courtyard, "keepouts": len(f.keepouts),
               "pads": [{"number": p.number, "kind": p.kind, "shape": p.shape, "x": p.x, "y": p.y,
                         "w": p.w, "h": p.h, "drill": p.drill} for p in f.pads]})


# ------------------------------------------------------------------ flow
@mcp.tool()
def validate_design(project: str) -> str:
    """Resolve every REF.PIN, check symbols/footprints/pin-pad mapping, unconnected pins, fab limits."""
    pd = P.project_dir(project)
    _quiet(P.build_library, pd)
    return _j(_quiet(P.validate, pd))


@mcp.tool()
def build_board(project: str, placement_tries: int = 3) -> str:
    """Run the whole automatic flow: library, validate, schematic, ERC, physics placement, autorouting,
    PCB generation, DRC (+pour repair), SPICE sims, design checks, previews. Takes 1-4 minutes."""
    return _j(_quiet(P.build, P.project_dir(project), place_tries=placement_tries))


@mcp.tool()
def run_stage(project: str, stage: str) -> str:
    """Run one stage: library|validate|schematic|erc|placement|routing|pcb|drc|simulation|checks|previews."""
    pd = P.project_dir(project)
    fn = {"library": P.build_library, "validate": P.validate, "schematic": P.make_schematic, "erc": P.erc,
          "placement": P.place, "routing": P.route, "pcb": P.make_pcb, "drc": P.drc,
          "simulation": P.simulate_all, "checks": P.run_checks, "previews": P.make_previews}[stage]
    res = _quiet(fn, pd)
    if isinstance(res, dict):
        res = {k: v for k, v in res.items() if k not in ("positions", "segments", "vias")}
    return _j(res)


@mcp.tool()
def get_status(project: str) -> str:
    """Per-stage status/summary (what the GUI dashboard shows)."""
    return _j(P._status(P.project_dir(project)))


@mcp.tool()
def get_report(project: str, kind: str) -> str:
    """Detailed report: erc | drc | checks | placement | routing | simulation | log."""
    o = P.project_dir(project) / "out"
    files = {"erc": "erc_summary.json", "drc": "drc_summary.json", "checks": "checks.json",
             "placement": "placement.json", "routing": "routes.json", "simulation": "sim/summary.json",
             "log": "pipeline.log"}
    p = o / files[kind]
    if not p.exists():
        return f"no {kind} report yet"
    if kind == "log":
        return p.read_text()[-8000:]
    data = json.loads(p.read_text())
    if kind == "routing":
        data = data.get("stats")
    if kind == "placement":
        data = {k: v for k, v in data.items()}
    return _j(data)


# ------------------------------------------------------------------ review & fab
@mcp.tool()
def get_review_package(project: str) -> str:
    """Everything needed for the fab-readiness review: intent notes, full netlist with pin names, BOM,
    ERC/DRC findings, design-rule checks, simulations, placement/routing stats, review checklist."""
    return _j(_quiet(P.review_package, P.project_dir(project)), limit=120000)


@mcp.tool()
def submit_review(project: str, verdict: str, summary: str, findings_json: str = "[]") -> str:
    """Record the AI design review. verdict: PASS | CONDITIONAL | FAIL.
    findings_json: [{"severity": "error|warning|info", "title": "...", "detail": "..."}]"""
    return _j(_quiet(P.submit_review, P.project_dir(project), verdict, summary, json.loads(findings_json)))


@mcp.tool()
def export_fab(project: str, force: bool = False) -> str:
    """Gerber + drill zip, JLCPCB BOM & CPL, schematic PDF, STEP. Blocked unless checks pass and the
    review verdict is PASS (force=True overrides for prototypes)."""
    return _j(_quiet(P.export_fab, P.project_dir(project), force=force))


@mcp.tool()
def open_in_kicad(project: str, what: str = "pcb") -> str:
    """Open the schematic ('sch') or board ('pcb') in the local KiCad GUI."""
    pd = P.project_dir(project)
    name = P.load(pd, check=False).name
    f = pd / f"{name}.{'kicad_sch' if what == 'sch' else 'kicad_pcb'}"
    subprocess.Popen(["open", "-a", "KiCad", str(f)] if Path("/usr/bin/open").exists() else ["kicad", str(f)])
    return f"opened {f}"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
