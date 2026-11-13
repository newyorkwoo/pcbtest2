"""Design flow orchestration. Every stage records its result in out/status.json (read by the GUI)."""
from __future__ import annotations

import datetime as _dt
import json
import time
import traceback
from pathlib import Path

import yaml

from . import checks, config, fab, library, pcb, placement, router, schematic, simulate, spec, zonefix

STAGES = ["library", "validate", "schematic", "erc", "placement", "routing", "pcb", "drc", "simulation",
          "checks", "previews", "review", "fab"]


def project_dir(name_or_path: str) -> Path:
    p = Path(name_or_path)
    if p.is_dir():
        return p.resolve()
    return (config.PROJECTS / name_or_path).resolve()


def _out(pd: Path) -> Path:
    o = pd / "out"
    o.mkdir(parents=True, exist_ok=True)
    return o


def log(pd: Path, msg: str) -> None:
    line = f"[{_dt.datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(_out(pd) / "pipeline.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _status(pd: Path) -> dict:
    p = _out(pd) / "status.json"
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}


def set_status(pd: Path, stage: str, state: str, summary: dict | str | None = None) -> None:
    st = _status(pd)
    st[stage] = {"state": state, "time": _dt.datetime.now().isoformat(timespec="seconds"), "summary": summary}
    (_out(pd) / "status.json").write_text(json.dumps(st, indent=1, default=str))


def stage(name):
    def deco(fn):
        def wrapper(pd, *a, **kw):
            pd = project_dir(str(pd))
            set_status(pd, name, "running")
            log(pd, f"▶ {name}")
            t = time.time()
            try:
                res = fn(pd, *a, **kw)
            except Exception as e:  # noqa: BLE001
                set_status(pd, name, "fail", {"exception": str(e), "trace": traceback.format_exc()[-1500:]})
                log(pd, f"✗ {name}: {e}")
                raise
            ok = res.get("ok", True) if isinstance(res, dict) else True
            short = {k: v for k, v in res.items() if k not in ("violations", "unconnected_items", "parity", "findings",
                                                             "positions", "segments", "vias", "files")} \
                if isinstance(res, dict) else res
            set_status(pd, name, "ok" if ok else "fail", short)
            log(pd, f"{'✓' if ok else '✗'} {name} ({time.time() - t:.1f}s)")
            return res
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


# ------------------------------------------------------------------ stages
@stage("library")
def build_library(pd: Path) -> dict:
    """Generate AI-defined custom symbols/footprints declared in design.yaml."""
    raw = yaml.safe_load((pd / "design.yaml").read_text(encoding="utf-8")) or {}
    made = []
    cs = raw.get("custom_symbols") or {}
    if cs:
        lib = pd / "lib" / f"{cs.get('lib', 'aipcb_custom')}.kicad_sym"
        syms = []
        for name, s in (cs.get("parts") or {}).items():
            pins = [{"number": str(p[0]), "name": str(p[1]), "type": p[2], "side": p[3]} if isinstance(p, list) else p
                    for p in s["pins"]]
            syms.append(library.make_symbol(name, pins, ref=s.get("ref", "U"), description=s.get("description", ""),
                                            footprint=s.get("footprint", ""), datasheet=s.get("datasheet", ""),
                                            keywords=s.get("keywords", "")))
            made.append(f"symbol {cs.get('lib', 'aipcb_custom')}:{name}")
        library.write_symbol_lib(lib, syms)
    cf = raw.get("custom_footprints") or {}
    if cf:
        lib_dir = pd / "lib" / f"{cf.get('lib', 'aipcb_custom')}.pretty"
        for name, f in (cf.get("parts") or {}).items():
            if f.get("type", "dual_row") == "dual_row":
                node = library.make_dual_row_footprint(name, f["pins"], f["pitch"], f["span"], f["pad_w"], f["pad_h"],
                                                       f["body_w"], f["body_l"], tuple(f["exposed_pad"]) if f.get("exposed_pad") else None,
                                                       f.get("description", ""))
                library.write_footprint(lib_dir, node)
                made.append(f"footprint {cf.get('lib', 'aipcb_custom')}:{name}")
            elif f["type"] in ("tht", "pads"):
                node = library.make_tht_footprint(name, f["pads"], tuple(f["courtyard"]),
                                                  tuple(f["body"]) if f.get("body") else None, f.get("description", ""))
                library.write_footprint(lib_dir, node)
                made.append(f"footprint {cf.get('lib', 'aipcb_custom')}:{name}")
    return {"ok": True, "generated": made}


def load(pd: Path, check=True) -> spec.Design:
    return spec.load(pd / "design.yaml", check=check)


@stage("validate")
def validate(pd: Path) -> dict:
    d = load(pd)
    return spec.summary(d)


@stage("schematic")
def make_schematic(pd: Path) -> dict:
    d = load(pd)
    if not d.ok():
        return {"ok": False, "errors": d.errors}
    p = schematic.generate(d)
    pcb.write_project_file(d)
    return {"ok": True, "file": str(p)}


@stage("erc")
def erc(pd: Path) -> dict:
    d = load(pd, check=False)
    r = checks.run_erc(d)
    (_out(pd) / "erc_summary.json").write_text(json.dumps(r, indent=1))
    return r


@stage("placement")
def place(pd: Path, seed: int = 1, tries: int = 3, moves: int = 30000, keep=None, unlock=()) -> dict:
    """keep: None (only `place.lock` parts), True (every previously placed part) or a list of refs."""
    d = load(pd)
    r = placement.place(d, seed=seed, tries=tries, moves=moves, keep=keep, unlock=unlock)
    # problems = real collisions (legalize failures, fixed/locked courtyard overlaps); r["overlap"] is the placer's
    # own cost with its part_spacing margin and stays informational
    return {"ok": not r["problems"], **{k: (round(v, 2) if isinstance(v, float) else v)
                                                                 for k, v in r.items() if k != "positions"}}


@stage("routing")
def route(pd: Path, gnd_mode: str = "pour") -> dict:
    d = load(pd)
    pos = json.loads((_out(pd) / "placement.json").read_text())["positions"]
    r = router.route(d, pos, log=lambda m: log(pd, m), gnd_mode=gnd_mode,
                     passes=int(d.rules.get("route_passes", 4)))
    st = r["stats"]
    # an open GND pin is a missing connection just like a failed net (DRC reports it as unconnected)
    return {"ok": not st["failed"] and not st.get("gnd_unconnected"), **st}


@stage("pcb")
def make_pcb(pd: Path) -> dict:
    d = load(pd)
    pos = json.loads((_out(pd) / "placement.json").read_text())["positions"]
    rp = _out(pd) / "routes.json"
    routes = json.loads(rp.read_text()) if rp.exists() else None
    p = pcb.generate(d, pos, routes)
    return {"ok": True, "file": str(p)}


@stage("drc")
def drc(pd: Path) -> dict:
    d = load(pd, check=False)
    r = checks.run_drc(d)
    g = checks.geom_check(d)
    fixes = []
    for _ in range(3):   # closed loop: stitch unanchored GND pour islands, rebuild, re-check
        kicad_open = any("Zone" in " ".join(v["items"]) for v in r.get("unconnected_items", []))
        detached = [c for n in g.get("nets", []) if n["net"] == "GND" for c in n["detached"]]
        if not kicad_open and not detached:
            break
        pos = json.loads((_out(pd) / "placement.json").read_text())["positions"]
        routes = json.loads((_out(pd) / "routes.json").read_text())
        # the shape-exact check knows which copper is really detached; zonefix's own graph is the fallback
        added = zonefix.fix_islands(d, pos, routes, external=detached or None)
        if not added:
            break
        fixes += added
        log(pd, f"  zone repair: +{len(added)} stitches")
        pcb.generate(d, pos, routes)
        r = checks.run_drc(d)
        g = checks.geom_check(d)
    r["zone_repairs"] = fixes
    r["geom"] = g
    if not g.get("ok", True):
        n_bad = sum(len(n["detached"]) for n in g.get("nets", [])) or 1
        r["ok"] = False
        r["errors"] = r.get("errors", 0) + n_bad
        r.setdefault("by_type", {})["geom_detached_copper"] = n_bad
        log(pd, f"  geometric pour check: {n_bad} detached GND component(s) remain")
    (_out(pd) / "drc_summary.json").write_text(json.dumps(r, indent=1))
    return r


@stage("simulation")
def simulate_all(pd: Path) -> dict:
    res = simulate.run_all(pd)
    return {"ok": all(r["ok"] for r in res), "count": len(res),
            "results": [{k: r.get(k) for k in ("name", "ok", "measures", "checks", "error")} for r in res]}


@stage("checks")
def run_checks(pd: Path) -> dict:
    d = load(pd, check=True)
    r = checks.full_report(d)
    return {"ok": r["auto_pass"], **{k: r[k] for k in ("counts", "auto_pass", "erc", "drc")}}


@stage("previews")
def make_previews(pd: Path) -> dict:
    d = load(pd, check=False)
    return {"ok": True, **fab.previews(d)}


@stage("review")
def submit_review(pd: Path, verdict: str, summary: str, findings: list | None = None) -> dict:
    verdict = verdict.upper()
    assert verdict in ("PASS", "FAIL", "CONDITIONAL"), "verdict must be PASS / FAIL / CONDITIONAL"
    data = {"verdict": verdict, "summary": summary, "findings": findings or [],
            "time": _dt.datetime.now().isoformat(timespec="seconds"), "reviewer": "Claude",
            "board": fab.board_fingerprint(pd)}
    (_out(pd) / "review.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    md = [f"# AI Design Review — {verdict}", "", summary, ""]
    for f in findings or []:
        md.append(f"- **[{f.get('severity', 'info').upper()}] {f.get('title', '')}** — {f.get('detail', '')}")
    (_out(pd) / "review.md").write_text("\n".join(md), encoding="utf-8")
    return {"ok": verdict == "PASS", "verdict": verdict}


@stage("fab")
def export_fab(pd: Path, force: bool = False) -> dict:
    d = load(pd, check=False)
    return fab.export(d, force=force)


def build(pd, place_tries: int = 3, moves: int = 30000, keep=None, unlock=()) -> dict:
    """Full automatic flow up to (not including) the AI review.

    keep=True re-uses the previous placement for every part already on the board (new parts and
    `unlock` refs are placed around them), so a design.yaml edit re-routes without reshuffling the board."""
    pd = project_dir(str(pd))
    out = {}
    out["library"] = build_library(pd)
    v = validate(pd)
    out["validate"] = v
    if not v["ok"]:
        return out
    out["schematic"] = make_schematic(pd)
    out["erc"] = {k: v for k, v in erc(pd).items() if k != "violations"}
    out["placement"] = place(pd, tries=place_tries, moves=moves, keep=keep, unlock=unlock)
    out["routing"] = route(pd)
    out["pcb"] = make_pcb(pd)
    out["drc"] = {k: v for k, v in drc(pd).items() if k not in ("violations", "unconnected_items", "parity")}
    out["simulation"] = simulate_all(pd)
    out["checks"] = run_checks(pd)
    out["previews"] = make_previews(pd)
    return out


def review_package(pd) -> dict:
    """Everything an AI reviewer needs to judge fab-readiness."""
    pd = project_dir(str(pd))
    o = _out(pd)

    def rd(p, default=None):
        try:
            return json.loads((o / p).read_text())
        except Exception:  # noqa: BLE001
            return default
    d = load(pd, check=True)
    parts = {r: {"value": p.value, "symbol": p.symbol_id, "footprint": p.footprint_id, "mpn": p.data.get("mpn"),
                 "lcsc": p.data.get("lcsc"), "block": p.block, "description": p.data.get("description", "")}
             for r, p in d.parts.items()}
    nets = {n: [f"{r}.{num}({next((pp.name for pp in d.parts[r].symbol.pins if pp.number == num), '')})" for r, num in pins]
            for n, pins in d.nets.items()}
    drc = rd("drc_summary.json", {})
    return {
        "project": d.name, "title": d.raw.get("title"), "notes": d.raw.get("notes", ""),
        "board": d.board, "rules": d.rules, "fab_profile": d.fab,
        "parts": parts, "nets": nets, "nc": sorted(f"{r}.{n}" for r, n in d.nc),
        "checks": rd("checks.json"), "erc_violations": (rd("erc_summary.json", {}) or {}).get("violations", [])[:60],
        "drc_violations": (drc.get("violations", []) + drc.get("unconnected_items", []) + drc.get("parity", []))[:80],
        "placement": {k: v for k, v in (rd("placement.json", {}) or {}).items() if k != "positions"},
        "routing": (rd("routes.json", {}) or {}).get("stats"),
        "simulations": rd("sim/summary.json", []),
        "previews": rd("preview/index.json", {}),
        "review_checklist": REVIEW_CHECKLIST,
    }


REVIEW_CHECKLIST = [
    "Power tree: input range, regulator dropout/headroom, current budget, thermal",
    "Battery/charger: charge current vs cell capacity, protection, power-path behaviour with/without USB",
    "Every IC supply pin decoupled, caps placed close (see checks)",
    "Pin-level correctness of every IC vs datasheet (esp. AI-generated symbols) - strapping pins, boot pins, EN/RESET RC",
    "Buses: I2C pull-ups + address conflicts, USB D+/D- orientation, UART TX/RX crossing",
    "Analog/audio/RF: signal path, coupling caps, filtering, antenna keepout respected, ground return",
    "USB-C: CC 5.1k pull-downs, VBUS ESD/protection",
    "Simulation results vs. expectations",
    "ERC clean (or every remaining warning justified)",
    "DRC clean: zero unconnected items, zero clearance/short errors, courtyard OK",
    "DFM: track/space/drill/annular within fab profile; board outline closed; mounting holes; silkscreen legible",
    "BOM: every placed part has a purchasable MPN/LCSC; footprints match the MPN package",
    "Assembly: connectors/buttons on accessible edges, polarity markings, test points/programming access",
]
