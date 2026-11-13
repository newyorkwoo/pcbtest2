"""Symbol / footprint library access + custom part generation.

Library ids look like "Device:R" (symbol) or "Resistor_SMD:R_0603_1608Metric" (footprint).
Project-local libraries (projects/<p>/lib/<nick>.kicad_sym, <nick>.pretty) take precedence.
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import config
from .sexpr import Sym, dumps, find, find_all, head, parse_file, prop, value

_extra_sym_dirs: list[Path] = []
_extra_fp_dirs: list[Path] = []


def set_project_libs(project_dir: Path | None) -> None:
    """Register a project's ./lib folder (searched before the KiCad stock libraries)."""
    _extra_sym_dirs.clear()
    _extra_fp_dirs.clear()
    if project_dir:
        lib = Path(project_dir) / "lib"
        _extra_sym_dirs.append(lib)
        _extra_fp_dirs.append(lib)
    _load_symlib.cache_clear()
    _load_fp.cache_clear()


# ================================================================ symbols
@dataclass
class Pin:
    number: str
    name: str
    etype: str          # input/output/bidirectional/power_in/power_out/passive/...
    x: float            # connection point, symbol coords (y up)
    y: float
    angle: int          # direction from connection point toward the body
    length: float
    unit: int
    hidden: bool = False


@dataclass
class Symbol:
    lib_id: str
    node: list          # flattened s-expr (named "Lib:Name")
    pins: list[Pin]
    units: int
    props: dict = field(default_factory=dict)
    is_power: bool = False

    def bbox(self, unit: int | None = None) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) in symbol coords incl. pins + graphics."""
        xs, ys = [], []
        for p in self.pins:
            if unit is None or p.unit in (0, unit):
                xs.append(p.x); ys.append(p.y)
        for sub in find_all(self.node, "symbol"):
            u = _unit_of(str(sub[1]))
            if unit is not None and u not in (0, unit):
                continue
            for g in sub[2:]:
                if not isinstance(g, list):
                    continue
                for key in ("start", "end", "center", "at"):
                    c = find(g, key)
                    if c is not None and len(c) >= 3:
                        xs.append(c[1]); ys.append(c[2])
                pts = find(g, "pts")
                if pts:
                    for xy in find_all(pts, "xy"):
                        xs.append(xy[1]); ys.append(xy[2])
                if head(g) == "circle":
                    c, r = find(g, "center"), value(g, "radius", 0)
                    if c:
                        xs += [c[1] - r, c[1] + r]; ys += [c[2] - r, c[2] + r]
        if not xs:
            return (-2.54, -2.54, 2.54, 2.54)
        return (min(xs), min(ys), max(xs), max(ys))


def _unit_of(subname: str) -> int:
    m = re.search(r"_(\d+)_(\d+)$", subname)
    return int(m.group(1)) if m else 0


def _style_of(subname: str) -> int:
    m = re.search(r"_(\d+)_(\d+)$", subname)
    return int(m.group(2)) if m else 1


def _sym_lib_path(nick: str) -> Path | None:
    for d in _extra_sym_dirs:
        p = d / f"{nick}.kicad_sym"
        if p.exists():
            return p
    p = config.SYMBOL_DIR / f"{nick}.kicad_sym"
    return p if p.exists() else None


@lru_cache(maxsize=64)
def _load_symlib(path: str) -> dict[str, list]:
    tree = parse_file(path)
    return {str(s[1]): s for s in find_all(tree, "symbol")}


def _flatten(syms: dict[str, list], name: str) -> list:
    node = syms[name]
    parent = value(node, "extends")
    if parent is None:
        return copy.deepcopy(node)
    base = _flatten(syms, str(parent))
    out = [Sym("symbol"), name]
    child_props = {c[1]: c for c in find_all(node, "property")}
    for c in base[2:]:
        if head(c) == "property" and c[1] in child_props:
            out.append(copy.deepcopy(child_props.pop(c[1])))
        elif head(c) == "symbol":
            sub = copy.deepcopy(c)
            sub[1] = re.sub(r"^.*(_\d+_\d+)$", lambda m: name + m.group(1), str(sub[1]))
            out.append(sub)
        else:
            out.append(copy.deepcopy(c))
    # remaining child-only properties go before the unit sub-symbols
    idx = next((i for i, c in enumerate(out) if head(c) == "symbol"), len(out))
    for c in child_props.values():
        out.insert(idx, copy.deepcopy(c)); idx += 1
    return out


def _pin_from(node: list, unit: int) -> Pin:
    at = find(node, "at")
    hidden = any(isinstance(x, Sym) and x == "hide" for x in node) or value(node, "hide") == "yes"
    return Pin(
        number=str(value(node, "number", "")),
        name=str(value(node, "name", "")),
        etype=str(node[1]),
        x=float(at[1]), y=float(at[2]), angle=int(at[3]) if len(at) > 3 else 0,
        length=float(value(node, "length", 2.54)),
        unit=unit, hidden=hidden,
    )


def get_symbol(lib_id: str) -> Symbol:
    nick, name = lib_id.split(":", 1)
    path = _sym_lib_path(nick)
    if not path:
        raise KeyError(f"symbol library '{nick}' not found")
    syms = _load_symlib(str(path))
    if name not in syms:
        close = [k for k in syms if name.lower() in k.lower()][:10]
        raise KeyError(f"symbol '{lib_id}' not found. Similar: {close}")
    node = _flatten(syms, name)
    node[1] = lib_id
    pins: list[Pin] = []
    units = 1
    for sub in find_all(node, "symbol"):
        u, st = _unit_of(str(sub[1])), _style_of(str(sub[1]))
        units = max(units, u)
        if st not in (0, 1):
            continue
        for p in find_all(sub, "pin"):
            pins.append(_pin_from(p, u))
    props = {c[1]: c[2] for c in find_all(node, "property")}
    is_power = find(node, "power") is not None
    return Symbol(lib_id, node, pins, units, props, is_power)


def search_symbols(query: str, limit: int = 40) -> list[dict]:
    q = [t.lower() for t in query.split()]
    out = []
    dirs = list(_extra_sym_dirs) + [config.SYMBOL_DIR]
    for d in dirs:
        for f in sorted(d.glob("*.kicad_sym")):
            text = f.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'^\t\(symbol "([^"]+)"', text, re.M):
                name = m.group(1)
                seg = text[m.start(): m.start() + 3000]
                desc = re.search(r'\(property "Description" "([^"]*)"', seg)
                kw = re.search(r'\(property "ki_keywords" "([^"]*)"', seg)
                hay = f"{f.stem}:{name} {desc.group(1) if desc else ''} {kw.group(1) if kw else ''}".lower()
                if all(t in hay for t in q):
                    out.append({"lib_id": f"{f.stem}:{name}", "description": desc.group(1) if desc else ""})
                    if len(out) >= limit:
                        return out
    return out


# ============================================================== footprints
@dataclass
class Pad:
    number: str
    kind: str            # smd / thru_hole / np_thru_hole / connect
    shape: str           # rect / roundrect / oval / circle / custom / trapezoid
    x: float
    y: float
    angle: float
    w: float
    h: float
    layers: list[str]
    drill: float = 0.0
    drill_w: float = 0.0
    drill_h: float = 0.0
    rratio: float = 0.0

    @property
    def copper(self) -> list[str]:
        out = []
        for l in self.layers:
            if l in ("*.Cu", "F&B.Cu"):
                out += ["F.Cu", "B.Cu"]
            elif l.endswith(".Cu"):
                out.append(l)
        return sorted(set(out))


@dataclass
class Footprint:
    lib_id: str
    node: list
    pads: list[Pad]
    courtyard: tuple[float, float, float, float]   # local bbox
    keepouts: list[dict]                            # [{layers, poly:[(x,y)], tracks, vias}]
    court_rects: list = field(default_factory=list)  # courtyard decomposed into rectangles

    def pad_numbers(self) -> set[str]:
        return {p.number for p in self.pads if p.number}


def _fp_path(lib_id: str) -> Path | None:
    nick, name = lib_id.split(":", 1)
    for d in _extra_fp_dirs + [config.FOOTPRINT_DIR]:
        p = d / f"{nick}.pretty" / f"{name}.kicad_mod"
        if p.exists():
            return p
    return None


@lru_cache(maxsize=512)
def _load_fp(path: str) -> list:
    return parse_file(path)


def _shape_bbox(g: list) -> list[tuple[float, float]]:
    if head(g) == "fp_circle":
        c, e = find(g, "center"), find(g, "end")
        if c is not None and e is not None:
            r = math.hypot(e[1] - c[1], e[2] - c[2])
            return [(c[1] - r, c[2] - r), (c[1] + r, c[2] + r)]
    pts = []
    for key in ("start", "end", "center", "mid"):
        c = find(g, key)
        if c is not None:
            pts.append((c[1], c[2]))
    p = find(g, "pts")
    if p:
        pts += [(xy[1], xy[2]) for xy in find_all(p, "xy")]
    return pts


def get_footprint(lib_id: str) -> Footprint:
    path = _fp_path(lib_id)
    if not path:
        raise KeyError(f"footprint '{lib_id}' not found")
    node = copy.deepcopy(_load_fp(str(path)))
    pads: list[Pad] = []
    for p in find_all(node, "pad"):
        at = find(p, "at"); size = find(p, "size"); layers = find(p, "layers")
        drill = find(p, "drill")
        d = dw = dh = 0.0
        if drill is not None:
            nums = [x for x in drill[1:] if isinstance(x, (int, float))]
            if any(isinstance(x, Sym) and x == "oval" for x in drill) and len(nums) >= 2:
                dw, dh = nums[0], nums[1]; d = min(dw, dh)
            elif nums:
                d = dw = dh = nums[0]
        pads.append(Pad(
            number=str(p[1]), kind=str(p[2]), shape=str(p[3]),
            x=float(at[1]), y=float(at[2]), angle=float(at[3]) if len(at) > 3 else 0.0,
            w=float(size[1]), h=float(size[2]),
            layers=[str(x) for x in layers[1:]], drill=d, drill_w=dw, drill_h=dh,
            rratio=float(value(p, "roundrect_rratio", 0.0)),
        ))
    pts = []
    for g in node:
        if head(g) in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"):
            if value(g, "layer") in ("F.CrtYd",):
                pts += _shape_bbox(g)
    if not pts:   # fall back to pads + fab
        for g in node:
            if head(g) in ("fp_line", "fp_rect", "fp_poly", "fp_circle") and value(g, "layer") == "F.Fab":
                pts += _shape_bbox(g)
        for pd in pads:
            pts += [(pd.x - pd.w / 2, pd.y - pd.h / 2), (pd.x + pd.w / 2, pd.y + pd.h / 2)]
    xs = [p[0] for p in pts] or [-1, 1]
    ys = [p[1] for p in pts] or [-1, 1]
    rects = _courtyard_rects(node, (min(xs), min(ys), max(xs), max(ys)))
    keepouts = []
    for z in find_all(node, "zone"):
        ko = find(z, "keepout")
        poly = find(z, "polygon")
        if ko is None or poly is None:
            continue
        lay = find(z, "layers") or find(z, "layer")
        layers = [str(x) for x in lay[1:]] if lay else ["F.Cu", "B.Cu"]
        if any(l in ("*.Cu", "F&B.Cu") for l in layers):
            layers = ["F.Cu", "B.Cu"]
        keepouts.append({
            "layers": [l for l in layers if l.endswith(".Cu")],
            "poly": [(xy[1], xy[2]) for xy in find_all(find(poly, "pts"), "xy")],
            "tracks": value(ko, "tracks") == "not_allowed",
            "vias": value(ko, "vias") == "not_allowed",
            "pour": value(ko, "copperpour") == "not_allowed",
        })
    return Footprint(lib_id, node, pads, (min(xs), min(ys), max(xs), max(ys)), keepouts, rects)


def _courtyard_rects(node: list, bbox, res: float = 0.25) -> list[tuple[float, float, float, float]]:
    """Decompose a (possibly non-rectangular) courtyard outline into a few rectangles.
    Edges may be unordered: inside-ness uses the even-odd crossing rule."""
    edges = []
    for g in node:
        if value(g, "layer") != "F.CrtYd":
            continue
        h = head(g)
        if h in ("fp_line", "fp_arc"):
            s, e = find(g, "start"), find(g, "end")
            edges.append(((s[1], s[2]), (e[1], e[2])))
        elif h == "fp_rect":
            s, e = find(g, "start"), find(g, "end")
            a, b, c, d = s[1], s[2], e[1], e[2]
            edges += [((a, b), (c, b)), ((c, b), (c, d)), ((c, d), (a, d)), ((a, d), (a, b))]
        elif h == "fp_poly":
            p = [(xy[1], xy[2]) for xy in find_all(find(g, "pts"), "xy")]
            edges += list(zip(p, p[1:] + p[:1]))
    x0, y0, x1, y1 = bbox
    if len(edges) < 3 or (x1 - x0) * (y1 - y0) < 30:      # small/simple parts: bbox is exact enough
        return [bbox]
    nx = max(1, int(math.ceil((x1 - x0) / res)))
    ny = max(1, int(math.ceil((y1 - y0) / res)))
    rows = []
    for j in range(ny):
        y = y0 + (j + 0.5) * res
        xs = sorted(ax + (y - ay) * (bx - ax) / (by - ay) for (ax, ay), (bx, by) in edges if (ay > y) != (by > y))
        runs = [(round(xs[k], 3), round(xs[k + 1], 3)) for k in range(0, len(xs) - 1, 2)]
        rows.append(tuple(runs))
    rects = []
    j = 0
    while j < ny:
        k = j
        while k + 1 < ny and rows[k + 1] == rows[j]:
            k += 1
        for a, b in rows[j]:
            rects.append((a, y0 + j * res, b, min(y1, y0 + (k + 1) * res)))
        j = k + 1
    return rects or [bbox]


def search_footprints(query: str, limit: int = 40) -> list[str]:
    q = [t.lower() for t in query.split()]
    out = []
    for d in _extra_fp_dirs + [config.FOOTPRINT_DIR]:
        for lib in sorted(d.glob("*.pretty")):
            for f in sorted(lib.glob("*.kicad_mod")):
                lid = f"{lib.stem}:{f.stem}"
                if all(t in lid.lower() for t in q):
                    out.append(lid)
                    if len(out) >= limit:
                        return out
    return out


# ======================================================= custom part generation
def _font(size=1.27):
    return [Sym("effects"), [Sym("font"), [Sym("size"), size, size]]]


def _hidden_font(size=1.27):
    return [Sym("effects"), [Sym("font"), [Sym("size"), size, size]], [Sym("hide"), Sym("yes")]]


def make_symbol(name: str, pins: list[dict], ref: str = "U", description: str = "",
                footprint: str = "", datasheet: str = "", keywords: str = "") -> list:
    """Build a box symbol. pins: [{number, name, type, side: left|right|top|bottom}]"""
    sides = {"left": [], "right": [], "top": [], "bottom": []}
    for p in pins:
        sides[p.get("side", "left")].append(p)
    rows = max(len(sides["left"]), len(sides["right"]), 1)
    cols = max(len(sides["top"]), len(sides["bottom"]), 1)
    longest = max([len(p["name"]) for p in pins] + [4])
    half_w = max(5.08, math.ceil((longest * 1.27 * 0.75 + 2.54 + (cols - 1) * 1.27) / 2.54) * 2.54)
    half_h = math.ceil((rows + 1) * 2.54 / 2 / 2.54) * 2.54
    pl = 2.54
    gfx = [Sym("symbol"), f"{name}_0_1",
           [Sym("rectangle"), [Sym("start"), -half_w, half_h], [Sym("end"), half_w, -half_h],
            [Sym("stroke"), [Sym("width"), 0.254], [Sym("type"), Sym("default")]],
            [Sym("fill"), [Sym("type"), Sym("background")]]]]
    unit = [Sym("symbol"), f"{name}_1_1"]

    def add(p, x, y, ang):
        unit.append([Sym("pin"), Sym(p.get("type", "passive")), Sym("line"),
                     [Sym("at"), round(x, 3), round(y, 3), ang], [Sym("length"), pl],
                     [Sym("name"), p["name"], _font()], [Sym("number"), str(p["number"]), _font()]])

    for i, p in enumerate(sides["left"]):
        add(p, -half_w - pl, half_h - 2.54 * (i + 1), 0)
    for i, p in enumerate(sides["right"]):
        add(p, half_w + pl, half_h - 2.54 * (i + 1), 180)
    n = len(sides["top"])
    for i, p in enumerate(sides["top"]):
        add(p, (i - (n - 1) / 2) * 2.54, half_h + pl, 270)
    n = len(sides["bottom"])
    for i, p in enumerate(sides["bottom"]):
        add(p, (i - (n - 1) / 2) * 2.54, -half_h - pl, 90)

    def P(k, v, x, y, hide=False):
        return [Sym("property"), k, v, [Sym("at"), x, y, 0], _hidden_font() if hide else _font()]

    return [Sym("symbol"), name,
            [Sym("pin_names"), [Sym("offset"), 1.016]],
            [Sym("exclude_from_sim"), Sym("no")], [Sym("in_bom"), Sym("yes")], [Sym("on_board"), Sym("yes")],
            P("Reference", ref, -half_w, half_h + 1.27),
            P("Value", name, half_w - 5, half_h + 1.27),
            P("Footprint", footprint, 0, -half_h - 3.81, True),
            P("Datasheet", datasheet or "~", 0, 0, True),
            P("Description", description, 0, 0, True),
            P("ki_keywords", keywords, 0, 0, True),
            gfx, unit]


def write_symbol_lib(path: Path, symbols: list[list]) -> None:
    """Create or update a .kicad_sym, replacing symbols with the same name."""
    path = Path(path)
    if path.exists():
        tree = parse_file(path)
        names = {str(s[1]) for s in symbols}
        tree = [c for c in tree if not (head(c) == "symbol" and str(c[1]) in names)]
    else:
        tree = [Sym("kicad_symbol_lib"), [Sym("version"), 20241209],
                [Sym("generator"), "aipcb"], [Sym("generator_version"), "9.0"]]
    tree += symbols
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(tree) + "\n", encoding="utf-8")
    _load_symlib.cache_clear()


def _line(x1, y1, x2, y2, layer, w):
    return [Sym("fp_line"), [Sym("start"), round(x1, 4), round(y1, 4)], [Sym("end"), round(x2, 4), round(y2, 4)],
            [Sym("stroke"), [Sym("width"), w], [Sym("type"), Sym("solid")]], [Sym("layer"), layer]]


def make_dual_row_footprint(name: str, pins: int, pitch: float, span: float, pad_w: float, pad_h: float,
                            body_w: float, body_l: float, exposed_pad: tuple[float, float] | None = None,
                            description: str = "") -> list:
    """Gull-wing dual-row SMD (SOIC/MSOP/TSSOP/SOP).  span = pad-centre to pad-centre across rows.
    Pin 1 top-left, counter-clockwise numbering (standard)."""
    per = pins // 2
    pads = []
    for i in range(per):
        y = (i - (per - 1) / 2) * pitch
        pads.append((str(i + 1), -span / 2, y))
        pads.append((str(pins - i), span / 2, y))
    node = [Sym("footprint"), name, [Sym("version"), 20241229], [Sym("generator"), "aipcb"],
            [Sym("layer"), "F.Cu"], [Sym("descr"), description], [Sym("attr"), Sym("smd")]]
    node.append([Sym("property"), "Reference", "REF**", [Sym("at"), 0, -body_l / 2 - 1.5, 0],
                 [Sym("layer"), "F.SilkS"], [Sym("effects"), [Sym("font"), [Sym("size"), 1, 1], [Sym("thickness"), 0.15]]]])
    node.append([Sym("property"), "Value", name, [Sym("at"), 0, body_l / 2 + 1.5, 0],
                 [Sym("layer"), "F.Fab"], [Sym("effects"), [Sym("font"), [Sym("size"), 1, 1], [Sym("thickness"), 0.15]]]])
    bw, bl = body_w / 2, body_l / 2
    for a, b, c, d in ((-bw, -bl, bw, -bl), (bw, -bl, bw, bl), (bw, bl, -bw, bl), (-bw, bl, -bw, -bl)):
        node.append(_line(a, b, c, d, "F.Fab", 0.1))
    # silk: top & bottom edges only (clear of pads) + pin-1 marker
    node.append(_line(-bw, -bl - 0.11, bw, -bl - 0.11, "F.SilkS", 0.12))
    node.append(_line(-bw, bl + 0.11, bw, bl + 0.11, "F.SilkS", 0.12))
    node.append(_line(-span / 2 - pad_w / 2, -bl - 0.11, -bw, -bl - 0.11, "F.SilkS", 0.12))
    cx = span / 2 + pad_w / 2 + 0.25
    cy = max(bl, (per - 1) / 2 * pitch + pad_h / 2) + 0.25
    for a, b, c, d in ((-cx, -cy, cx, -cy), (cx, -cy, cx, cy), (cx, cy, -cx, cy), (-cx, cy, -cx, -cy)):
        node.append(_line(a, b, c, d, "F.CrtYd", 0.05))
    for num, x, y in pads:
        node.append([Sym("pad"), num, Sym("smd"), Sym("roundrect"), [Sym("at"), round(x, 4), round(y, 4)],
                     [Sym("size"), pad_w, pad_h], [Sym("layers"), "F.Cu", "F.Mask", "F.Paste"],
                     [Sym("roundrect_rratio"), 0.25]])
    if exposed_pad:
        node.append([Sym("pad"), str(pins + 1), Sym("smd"), Sym("rect"), [Sym("at"), 0, 0],
                     [Sym("size"), exposed_pad[0], exposed_pad[1]], [Sym("layers"), "F.Cu", "F.Mask", "F.Paste"]])
    return node


def make_tht_footprint(name: str, pads: list, courtyard: tuple[float, float, float, float],
                       body: tuple[float, float, float, float] | None = None, description: str = "") -> list:
    """Footprint from explicit pad coordinates (e.g. read off a datasheet land-pattern / hole drawing).
    pads: dicts {number, x, y, drill | [drill_w, drill_h], size | [w, h], shape: circle|rect|oval}
          or SMD pads {number, x, y, size: [w, h], smd: true}.
    courtyard / body: (x1, y1, x2, y2) in mm, footprint origin = part reference point (e.g. shaft centre)."""
    all_smd = all(p.get("smd") for p in pads)
    node = [Sym("footprint"), name, [Sym("version"), 20241229], [Sym("generator"), "aipcb"],
            [Sym("layer"), "F.Cu"], [Sym("descr"), description], [Sym("attr"), Sym("smd" if all_smd else "through_hole")]]
    x1, y1, x2, y2 = courtyard
    node.append([Sym("property"), "Reference", "REF**", [Sym("at"), 0, y1 - 1.2, 0],
                 [Sym("layer"), "F.SilkS"], [Sym("effects"), [Sym("font"), [Sym("size"), 1, 1], [Sym("thickness"), 0.15]]]])
    node.append([Sym("property"), "Value", name, [Sym("at"), 0, y2 + 1.2, 0],
                 [Sym("layer"), "F.Fab"], [Sym("effects"), [Sym("font"), [Sym("size"), 1, 1], [Sym("thickness"), 0.15]]]])
    for a, b, c, d in ((x1, y1, x2, y1), (x2, y1, x2, y2), (x2, y2, x1, y2), (x1, y2, x1, y1)):
        node.append(_line(a, b, c, d, "F.CrtYd", 0.05))
    if body:
        bx1, by1, bx2, by2 = body
        for a, b, c, d in ((bx1, by1, bx2, by1), (bx2, by1, bx2, by2), (bx2, by2, bx1, by2), (bx1, by2, bx1, by1)):
            node.append(_line(a, b, c, d, "F.Fab", 0.1))
            node.append(_line(a, b, c, d, "F.SilkS", 0.12))
    for p in pads:
        if p.get("smd"):                     # surface pad: {number, x, y, size: [w, h], smd: true}
            w, h = p["size"] if isinstance(p["size"], (list, tuple)) else (p["size"], p["size"])
            node.append([Sym("pad"), str(p["number"]), Sym("smd"), Sym(p.get("shape", "rect")),
                         [Sym("at"), round(float(p["x"]), 4), round(float(p["y"]), 4)],
                         [Sym("size"), float(w), float(h)], [Sym("layers"), "F.Cu", "F.Mask", "F.Paste"]])
            continue
        dr = p["drill"]
        size = p.get("size") or ((dr[0] + 0.6, dr[1] + 0.6) if isinstance(dr, list) else (dr + 0.6, dr + 0.6))
        size = size if isinstance(size, (list, tuple)) else (size, size)
        shape = p.get("shape") or ("oval" if isinstance(dr, list) else "circle")
        drill = [Sym("drill"), Sym("oval"), dr[0], dr[1]] if isinstance(dr, list) else [Sym("drill"), dr]
        node.append([Sym("pad"), str(p["number"]), Sym("thru_hole"), Sym(shape),
                     [Sym("at"), round(float(p["x"]), 4), round(float(p["y"]), 4)],
                     [Sym("size"), float(size[0]), float(size[1])], drill, [Sym("layers"), "*.Cu", "*.Mask"]])
    return node


def write_footprint(lib_dir: Path, node: list) -> Path:
    lib_dir = Path(lib_dir)
    lib_dir.mkdir(parents=True, exist_ok=True)
    p = lib_dir / f"{node[1]}.kicad_mod"
    p.write_text(dumps(node) + "\n", encoding="utf-8")
    _load_fp.cache_clear()
    return p
