"""Physical wiring diagram (point-to-point wire list) from the generated board.

Every part is taken at its real board position with its real pads; each net becomes the shortest set of
pin-to-pin wires that joins all its pads (minimum spanning tree), each with a wire colour. Useful as a
hand-wiring guide for a prototype and as a "which pin goes to which pin" reference.

    wire_list(d) -> {"board": [x0, y0, x1, y1], "parts": {...}, "wires": [...], "nets": {...}}
    write_csv(d)  -> out/wiring_list.csv
"""
from __future__ import annotations

import csv
import math
import re
from pathlib import Path

from .schematic import is_ground, supply_nets
from .sexpr import find, find_all, head, parse_file
from .spec import Design

# (name, hex) — black for ground, reds for rails, then a fixed signal palette (readable on white)
SIGNAL_COLORS = [("藍", "#1f5fd1"), ("綠", "#1d9a3c"), ("紫", "#8a3fd1"), ("棕", "#8b5a2b"), ("青", "#0e9aa7"),
                 ("粉紅", "#e0529c"), ("黃", "#d4a000"), ("灰", "#7a7f87"), ("深藍", "#1b2f8f"), ("橄欖綠", "#6b8e23")]
RAIL_COLORS = [("紅", "#d62828"), ("橙", "#f07800"), ("暗紅", "#9b1d20"), ("洋紅", "#c2185b"), ("橙紅", "#e8590c")]


def _rot(x, y, deg):
    """KiCad footprint rotation (counter-clockwise on screen, y down)"""
    a = math.radians(deg)
    return x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a)


def _bare(net: str) -> str:
    return net[1:] if net.startswith("/") else net


def wire_colors(nets, supply):
    out = {}
    rails = sorted(n for n in nets if n in supply and not is_ground(n))
    rails.sort(key=lambda n: (not n.startswith("+3"), n))       # +3V3 gets plain red
    for i, n in enumerate(rails):
        out[n] = RAIL_COLORS[i % len(RAIL_COLORS)]
    sig = sorted(n for n in nets if n not in out and not is_ground(n))
    for i, n in enumerate(sig):
        out[n] = SIGNAL_COLORS[i % len(SIGNAL_COLORS)]
    for n in nets:
        if is_ground(n):
            out[n] = ("黑", "#111111")
    return out


def _mst(points):
    """Prim's minimum spanning tree over [(x, y), ...] -> [(i, j), ...]"""
    n = len(points)
    if n < 2:
        return []
    inside = {0}
    best = {j: (math.dist(points[0], points[j]), 0) for j in range(1, n)}
    edges = []
    while best:
        j = min(best, key=lambda k: best[k][0])
        _, i = best.pop(j)
        edges.append((i, j))
        inside.add(j)
        for k in best:
            dk = math.dist(points[j], points[k])
            if dk < best[k][0]:
                best[k] = (dk, j)
    return edges


def wire_list(d: Design, pcb_path: Path | None = None) -> dict:
    pcb_path = Path(pcb_path or d.project_dir / f"{d.name}.kicad_pcb")
    tree = parse_file(pcb_path)
    pin_names = {}
    for ref, part in d.parts.items():
        if part.symbol:
            for p in part.symbol.pins:
                pin_names[(ref, p.number)] = p.name

    # board outline = bbox of Edge.Cuts graphics
    xs, ys = [], []
    for g in tree[1:]:
        if isinstance(g, list) and head(g) in ("gr_line", "gr_rect", "gr_arc") and \
                str((find(g, "layer") or [None, ""])[1]) == "Edge.Cuts":
            for key in ("start", "end", "mid"):
                c = find(g, key)
                if c:
                    xs.append(float(c[1])); ys.append(float(c[2]))
    board = [min(xs), min(ys), max(xs), max(ys)] if xs else None

    parts, pads_by_net = {}, {}
    for fp in find_all(tree, "footprint"):
        at = find(fp, "at")
        fx, fy, fr = float(at[1]), float(at[2]), float(at[3]) if len(at) > 3 else 0.0
        side = "B" if str(find(fp, "layer")[1]).startswith("B") else "F"
        ref = next((str(p[2]) for p in find_all(fp, "property") if str(p[1]) == "Reference"), "?")
        value = next((str(p[2]) for p in find_all(fp, "property") if str(p[1]) == "Value"), "")
        outline, fab = [], []
        for g in fp[2:]:
            if not isinstance(g, list) or head(g) not in ("fp_line", "fp_rect", "fp_poly", "fp_circle"):
                continue
            lay = str((find(g, "layer") or [None, ""])[1])
            if lay.endswith("Fab"):
                tgt = fab
            elif lay.endswith("CrtYd"):
                tgt = outline
            else:
                continue
            for key in ("start", "end", "center"):
                c = find(g, key)
                if c:
                    tgt.append(_rot(float(c[1]), float(c[2]), fr))
            pts = find(g, "pts")
            if pts:
                tgt += [_rot(float(p[1]), float(p[2]), fr) for p in find_all(pts, "xy")]
        pads = []
        for pad in find_all(fp, "pad"):
            num = str(pad[1])
            pa = find(pad, "at")
            px, py = _rot(float(pa[1]), float(pa[2]), fr)
            pr = (float(pa[3]) if len(pa) > 3 else 0.0)
            sz = find(pad, "size")
            w, h = float(sz[1]), float(sz[2])
            if round(pr / 90) % 2:                 # pad angle in the file is absolute: swap size at 90/270
                w, h = h, w
            netn = find(pad, "net")
            net = _bare(str(netn[-1])) if netn else None
            if net and net.startswith("unconnected-"):
                net = None
            x, y = fx + px, fy + py
            pads.append({"num": num, "x": round(x, 3), "y": round(y, 3), "w": w, "h": h, "net": net,
                         "name": pin_names.get((ref, num), "")})
            if net and num:
                pads_by_net.setdefault(net, []).append((ref, num, x, y))
        if outline:
            ox = [fx + p[0] for p in outline]; oy = [fy + p[1] for p in outline]
            bbox = [min(ox), min(oy), max(ox), max(oy)]
        else:
            bbox = [min(p["x"] - p["w"] / 2 for p in pads), min(p["y"] - p["h"] / 2 for p in pads),
                    max(p["x"] + p["w"] / 2 for p in pads), max(p["y"] + p["h"] / 2 for p in pads)] if pads else \
                [fx - 1, fy - 1, fx + 1, fy + 1]
        # the physical body (fab outline + pads): what a wire must go around; the courtyard adds keep-out margin
        bx = [fx + q[0] for q in fab] + [p["x"] - p["w"] / 2 for p in pads] + [p["x"] + p["w"] / 2 for p in pads]
        by = [fy + q[1] for q in fab] + [p["y"] - p["h"] / 2 for p in pads] + [p["y"] + p["h"] / 2 for p in pads]
        body = [min(bx), min(by), max(bx), max(by)] if bx else bbox
        parts[ref] = {"body": [round(v, 3) for v in body], "value": value, "side": side, "x": fx, "y": fy, "rot": fr, "bbox": [round(v, 3) for v in bbox],
                      "pads": pads, "block": d.parts[ref].block if ref in d.parts else ""}

    supply = supply_nets(d)
    colors = wire_colors(pads_by_net, supply)
    wires, nets = [], {}
    for net in sorted(pads_by_net, key=lambda n: (is_ground(n), n not in supply, n)):
        # one wire per joint: pads of one part that share a net (e.g. several GND pads) count once
        # only if they are the same physical point; otherwise they are joined like any other pin
        pts = pads_by_net[net]
        cname, chex = colors[net]
        nets[net] = {"color": chex, "color_name": cname, "pins": [f"{r}.{p}" for r, p, *_ in pts],
                     "supply": net in supply, "ground": is_ground(net)}
        for i, j in _mst([(x, y) for _, _, x, y in pts]):
            a, b = pts[i], pts[j]
            if (a[0], a[1]) > (b[0], b[1]):
                a, b = b, a
            wires.append({"net": net, "color": chex, "color_name": cname,
                          "from": f"{a[0]}.{a[1]}", "from_name": pin_names.get((a[0], a[1]), ""),
                          "to": f"{b[0]}.{b[1]}", "to_name": pin_names.get((b[0], b[1]), ""),
                          "x1": round(a[2], 3), "y1": round(a[3], 3), "x2": round(b[2], 3), "y2": round(b[3], 3),
                          "length": round(math.dist(a[2:], b[2:]), 1)})
    for i, w in enumerate(wires, 1):
        w["no"] = i
    data = {"board": board, "parts": parts, "wires": wires, "nets": nets}
    route_wires(data)
    return data


# ====================================================================== wire routing around the parts
GRID = 0.5          # mm
MARGIN = 4.0        # wires may run this far outside the board outline
TURN = 2.0          # cost of a bend (in grid steps)
SHARE = 8.0         # running on a cell another net already uses
NEAR = 1.5          # running right next to another net's wire
OWN = 3.0           # inside the start / end part's outline (leave it quickly)
CROSS = 60.0        # over another part's body: only when there is no way around


def route_wires(data: dict) -> None:
    """Give every wire an orthogonal path ("path": [[x, y], ...]) that goes around the part outlines,
    keeps wires of different nets apart where it can, and prefers few bends. "length" becomes the
    routed length; "straight" keeps the pin-to-pin distance."""
    import heapq

    parts = data["parts"]
    b = data["board"] or [min(p["bbox"][0] for p in parts.values()), min(p["bbox"][1] for p in parts.values()),
                          max(p["bbox"][2] for p in parts.values()), max(p["bbox"][3] for p in parts.values())]
    X0, Y0 = b[0] - MARGIN, b[1] - MARGIN
    NX = int((b[2] - b[0] + 2 * MARGIN) / GRID) + 1
    NY = int((b[3] - b[1] + 2 * MARGIN) / GRID) + 1

    def cell(x, y):
        return (min(NX - 1, max(0, int(round((x - X0) / GRID)))), min(NY - 1, max(0, int(round((y - Y0) / GRID)))))

    def xy(c):
        return (round(X0 + c[0] * GRID, 3), round(Y0 + c[1] * GRID, 3))

    owner = {}                        # cell -> ref whose outline covers it
    for ref, p in parts.items():
        x0, y0, x1, y1 = p.get("body") or p["bbox"]
        a, bb = cell(x0, y0), cell(x1, y1)
        for i in range(a[0], bb[0] + 1):
            for j in range(a[1], bb[1] + 1):
                owner.setdefault((i, j), set()).add(ref)
    used: dict[tuple, set] = {}      # cell -> nets running through it

    order = sorted(data["wires"], key=lambda w: (data["nets"][w["net"]]["ground"],
                                                 data["nets"][w["net"]]["supply"], w["length"]))
    D4 = ((1, 0), (-1, 0), (0, 1), (0, -1))
    for w in order:
        ra, rb = w["from"].split(".")[0], w["to"].split(".")[0]
        s, t = cell(w["x1"], w["y1"]), cell(w["x2"], w["y2"])
        net = w["net"]
        # search window: the two ends plus room to go around, widened if that fails
        path = None
        for pad in (8.0, 20.0, 1e9):
            lo = cell(min(w["x1"], w["x2"]) - pad, min(w["y1"], w["y2"]) - pad)
            hi = cell(max(w["x1"], w["x2"]) + pad, max(w["y1"], w["y2"]) + pad)
            path = _astar(s, t, lo, hi, owner, used, net, {ra, rb}, D4, heapq)
            if path:
                break
        if not path:                 # boxed in completely: fall back to the straight wire
            w["path"] = [[w["x1"], w["y1"]], [w["x2"], w["y2"]]]
            w["straight"] = w["length"]
            continue
        for c in path:
            used.setdefault(c, set()).add(net)
        pts = [xy(c) for c in path]
        # keep only the corners
        corners = [pts[0]]
        for i in range(1, len(pts) - 1):
            ax, ay = pts[i - 1]; bx, by = pts[i]; cx, cy = pts[i + 1]
            if (bx - ax) * (cy - by) != (by - ay) * (cx - bx):
                corners.append(pts[i])
        corners.append(pts[-1])
        poly = [[w["x1"], w["y1"]]] + [list(c) for c in corners] + [[w["x2"], w["y2"]]]
        # the pad centre -> first grid point jog is tiny; make it orthogonal too
        poly = _ortho_ends(poly)
        w["path"] = poly
        w["straight"] = w["length"]
        w["length"] = round(sum(math.dist(poly[i], poly[i + 1]) for i in range(len(poly) - 1)), 1)


def _ortho_ends(poly):
    out = [poly[0]]
    for q in poly[1:]:
        px, py = out[-1]
        if abs(q[0] - px) > 1e-6 and abs(q[1] - py) > 1e-6:
            out.append([q[0], py])
        if [round(q[0], 4), round(q[1], 4)] != [round(out[-1][0], 4), round(out[-1][1], 4)]:
            out.append(q)
    return out


def _astar(s, t, lo, hi, owner, used, net, own_refs, D4, heapq):
    """orthogonal A* with bend cost; states are (cell, direction)"""
    def h(c):
        return abs(c[0] - t[0]) + abs(c[1] - t[1])

    start = (s, -1)
    g = {start: 0.0}
    prev = {}
    pq = [(h(s), 0.0, s, -1)]
    while pq:
        f, gc, c, d = heapq.heappop(pq)
        if c == t:
            out = [c]
            st = (c, d)
            while st in prev:
                st = prev[st]
                out.append(st[0])
            return out[::-1]
        if gc > g.get((c, d), 1e18):
            continue
        for k, (dx, dy) in enumerate(D4):
            n = (c[0] + dx, c[1] + dy)
            if not (lo[0] <= n[0] <= hi[0] and lo[1] <= n[1] <= hi[1]):
                continue
            o = owner.get(n)
            step = 1.0
            if o and n != t:
                step += OWN if (o & own_refs) else CROSS
            u = used.get(n)
            if u and net not in u:
                step += SHARE
            else:
                for ex, ey in D4:
                    v = used.get((n[0] + ex, n[1] + ey))
                    if v and net not in v:
                        step += NEAR
                        break
            if d != -1 and k != d:
                step += TURN
            ng = gc + step
            st = (n, k)
            if ng < g.get(st, 1e18):
                g[st] = ng
                prev[st] = (c, d)
                heapq.heappush(pq, (ng + h(n), ng, n, k))
    return None


def write_csv(d: Design, data: dict | None = None, path: Path | None = None) -> Path:
    data = data or wire_list(d)
    path = Path(path or d.project_dir / "out" / "wiring_list.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:       # BOM so Excel opens the Chinese correctly
        wr = csv.writer(f)
        wr.writerow(["線號", "線色", "網路", "起點 (元件.腳)", "起點腳名", "終點 (元件.腳)", "終點腳名", "走線長度 mm"])
        for w in data["wires"]:
            wr.writerow([w["no"], w["color_name"], w["net"], w["from"], _plain(w["from_name"]), w["to"],
                         _plain(w["to_name"]), w["length"]])
    return path


def _plain(name: str) -> str:
    name = re.sub(r"~\{([^}]*)\}", r"/\1", str(name or ""))
    return re.sub(r"[_^]\{([^}]*)\}", r"\1", name)
