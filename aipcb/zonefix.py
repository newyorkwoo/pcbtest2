"""Closed-loop copper-pour repair.

After KiCad has filled the GND zones (DRC --refill-zones --save-board), read the filled
polygons back, find islands that contain no via / through-hole pad (so they are tied to the
rest of GND only by luck or not at all) and drop a stitching via inside each one at a
location the router certifies as clearance-safe.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .placement import ORIGIN
from .router import LAYERS, Route, Router, _poly_mask
from .sexpr import find, find_all, parse_file, value
from .spec import Design


def _router_with_routes(d: Design, positions: dict, routes: dict) -> Router:
    r = Router(d, positions, log=lambda m: None)
    for s in routes.get("segments", []):
        n = r.net_id[s["net"]]
        r._commit(Route(n, [(LAYERS.index(s["layer"]), s["x1"], s["y1"], s["x2"], s["y2"], s["w"])], []))
    for v in routes.get("vias", []):
        r._commit(Route(r.net_id[v["net"]], [], [(v["x"], v["y"])]))
    return r


def _area(pts):
    xs = np.array([p[0] for p in pts]); ys = np.array([p[1] for p in pts])
    return 0.5 * abs(np.dot(xs, np.roll(ys, 1)) - np.dot(ys, np.roll(xs, 1)))


def _inside(poly, x, y) -> bool:
    return bool(_poly_mask(np.array([[x]]), np.array([[y]]), poly)[0, 0])


def fix_islands(d: Design, positions: dict, routes: dict, net: str = "GND",
                external: list[dict] | None = None) -> list[dict]:
    """Connectivity-aware pour repair.  Builds a graph of {pour islands, vias, GND pads, GND tracks},
    finds components not attached to the main plane and stitches each one with a via placed where
    the opposite layer belongs to the main component.

    `external`: the `detached` list of kicad_geomcheck (shape-exact). When given it decides which copper is
    detached instead of this module's sample-point graph, which can accept a pad sitting in a clearance hole
    whose thermal spokes were never generated."""
    pcb = parse_file(d.project_dir / f"{d.name}.kicad_pcb")
    islands = []                                    # (layer, pts, area)
    for z in find_all(pcb, "zone"):
        zn = value(z, "net_name") or value(z, "net")      # KiCad 10 saves (net "/GND")
        if zn not in (net, "/" + net):
            continue
        for fp in find_all(z, "filled_polygon"):
            pts = [(xy[1], xy[2]) for xy in find_all(find(fp, "pts"), "xy")]
            if len(pts) >= 3:
                islands.append((value(fp, "layer"), pts, _area(pts)))
    if not islands:
        return []
    r = _router_with_routes(d, positions, routes)
    nid = r.net_id[net]
    nodes = [("isl", i) for i in range(len(islands))]
    parent = list(range(10_000))

    def f(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a

    def u(a, b):
        parent[f(a)] = f(b)

    def islands_at(x, y, layers):
        return [i for i, (L, pts, _) in enumerate(islands) if L in layers and _inside(pts, x, y)]

    points = []    # (node index, x, y, layers)  -- exact contact points (track ends, centres)
    ring = []      # (node index, x, y, layers)  -- samples just outside pads/vias (thermal spokes touch here)
    import math as _m

    def ring_pts(cx, cy, hw, hh, ang):
        out = []
        c, s = _m.cos(_m.radians(ang)), _m.sin(_m.radians(ang))
        for lx, ly in ((hw, 0), (-hw, 0), (0, hh), (0, -hh), (hw, hh), (-hw, hh), (hw, -hh), (-hw, -hh)):
            out.append((cx + lx * c + ly * s, cy - lx * s + ly * c))
        return out
    for v in routes.get("vias", []):
        if v["net"] == net:
            k = len(nodes); nodes.append(("via", v)); points.append((k, v["x"], v["y"], ("F.Cu", "B.Cu")))
            for x, y in ring_pts(v["x"], v["y"], r.via_d / 2 + 0.06, r.via_d / 2 + 0.06, 0):
                ring.append((k, x, y, ("F.Cu", "B.Cu")))
    for p in r.net_pads(nid):
        k = len(nodes); nodes.append(("pad", p))
        lay = tuple(LAYERS[l] for l in p["layers"])
        points.append((k, p["x"], p["y"], lay))
        cx, cy, hw, hh, ang, _, _ = p["obj"].geom
        for x, y in ring_pts(cx, cy, hw + 0.06, hh + 0.06, ang):
            ring.append((k, x, y, lay))
    for s in routes.get("segments", []):
        if s["net"] == net:
            k = len(nodes); nodes.append(("seg", s))
            points.append((k, s["x1"], s["y1"], (s["layer"],))); points.append((k, s["x2"], s["y2"], (s["layer"],)))
    if len(nodes) >= len(parent):
        parent = list(range(len(nodes) + 10))
    for k, x, y, layers in points + ring:
        for i in islands_at(x, y, layers):
            u(k, i)
    # items touching each other (track ends on pads / vias / other tracks)
    for a in range(len(points)):
        ka, xa, ya, la = points[a]
        for b in range(a + 1, len(points)):
            kb, xb, yb, lb = points[b]
            if ka != kb and set(la) & set(lb) and abs(xa - xb) < 0.02 and abs(ya - yb) < 0.02:
                u(ka, kb)
    for p in r.net_pads(nid):        # track starting inside a pad
        o = p["obj"]
        for k, x, y, layers in points:
            if nodes[k][0] == "seg" and set(layers) & {LAYERS[l] for l in p["layers"]}:
                if float(r._dist(o, np.array([[x]]), np.array([[y]]))[0, 0]) <= 1e-6:
                    u(k, next(kk for kk, n_ in enumerate(nodes) if n_[0] == "pad" and n_[1] is p))
    main = f(max(range(len(islands)), key=lambda i: islands[i][2]))
    comps: dict[int, list[int]] = {}
    for k in range(len(nodes)):
        comps.setdefault(f(k), []).append(k)
    groups = []    # (island indices, pad dicts, via dicts) per detached component
    if external is not None:
        pad_by_name = {f"{p['ref']}.{p['pad']}": p for p in r.net_pads(nid)}
        claimed: set[int] = set()
        for c in external:
            isl = []
            for e in c.get("islands", []):
                for i, (L, pts, area) in enumerate(islands):
                    if i in claimed or L != e["layer"] or abs(area - e["area_mm2"]) > max(0.05, 0.02 * area):
                        continue
                    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                    if abs((min(xs) + max(xs)) / 2 - e["at"][0]) < 0.1 and abs((min(ys) + max(ys)) / 2 - e["at"][1]) < 0.1:
                        isl.append(i); claimed.add(i); break
            pads = [pad_by_name[n] for n in c.get("pads", []) if n in pad_by_name]
            groups.append((isl, pads, []))
        main_isl = [i for i in range(len(islands)) if i not in claimed]
    else:
        for root, members in comps.items():
            if root == main:
                continue
            groups.append(([k for k in members if nodes[k][0] == "isl"],
                           [nodes[k][1] for k in members if nodes[k][0] == "pad"],
                           [nodes[k][1] for k in members if nodes[k][0] == "via"]))
        main_isl = [i for i in range(len(islands)) if f(i) == main]
    _, via_ok, _ = r.masks(nid, r.hw0)
    ii, jj = np.meshgrid(np.arange(r.nx), np.arange(r.ny))
    GX = ORIGIN + ii * r.g; GY = ORIGIN + jj * r.g
    erode = int(np.ceil((r.via_d / 2 + 0.05) / r.g))
    main_mask = {"F.Cu": np.zeros(GX.shape, bool), "B.Cu": np.zeros(GX.shape, bool)}
    for i in main_isl:
        L, pts, _ = islands[i]
        main_mask[L] |= _poly_mask(GX, GY, pts)
    main_mask = {L: ~r._dilate(~m, erode) for L, m in main_mask.items()}
    added = []
    for isl, pads, vias_g in groups:
        if not isl and not pads:
            continue
        best = None
        for k in isl:
            L, pts, area = islands[k]
            other = "B.Cu" if L == "F.Cu" else "F.Cu"
            cand = ~r._dilate(~_poly_mask(GX, GY, pts), erode) & via_ok & main_mask[other]
            if cand.any():
                js, is_ = np.nonzero(cand)
                cx = float(np.mean([p[0] for p in pts])); cy = float(np.mean([p[1] for p in pts]))
                q = int(np.argmin((GX[js, is_] - cx) ** 2 + (GY[js, is_] - cy) ** 2))
                best = (float(GX[js[q], is_[q]]), float(GY[js[q], is_[q]]), L, area)
                break
        if best is None:
            # no spot for a via: run a GND trace from this component's copper into the main plane
            walk, via_m, _ = r.masks(nid, r.hw0)
            src_m = {"F.Cu": np.zeros(GX.shape, bool), "B.Cu": np.zeros(GX.shape, bool)}
            for k in isl:
                L, pts, _ = islands[k]
                src_m[L] |= ~r._dilate(~_poly_mask(GX, GY, pts), int(np.ceil(r.hw0 / r.g)))
            for obj in pads:            # bare pad with no pour around it: start from the pad core
                for c in r._pad_cells(obj):
                    L = LAYERS[c // r.N]; cc = c % r.N
                    if r.pad_core.reshape(2, -1)[c // r.N][cc] == nid:
                        src_m[L][cc // r.nx, cc % r.nx] = True
            for obj in vias_g:
                i, j = int(round((obj["x"] - ORIGIN) / r.g)), int(round((obj["y"] - ORIGIN) / r.g))
                src_m["F.Cu"][j, i] = src_m["B.Cu"][j, i] = True
            sources, targets = set(), set()
            ys_, xs_ = np.nonzero(src_m["F.Cu"] | src_m["B.Cu"])
            if not len(xs_):
                continue
            cx, cy = float(np.mean(xs_)), float(np.mean(ys_))
            win = (max(0, int(xs_.min()) - 150), max(0, int(ys_.min()) - 150),
                   min(r.nx - 1, int(xs_.max()) + 150), min(r.ny - 1, int(ys_.max()) + 150))
            for li, L in enumerate(LAYERS):
                js, is_ = np.nonzero(src_m[L])
                sources |= {li * r.N + j * r.nx + i for j, i in zip(js, is_)}
                tm = main_mask[L].copy()
                tm[:win[1], :] = False; tm[win[3] + 1:, :] = False; tm[:, :win[0]] = False; tm[:, win[2] + 1:] = False
                js, is_ = np.nonzero(tm)
                targets |= {li * r.N + j * r.nx + i for j, i in zip(js, is_)}
            if not targets:
                continue
            tgt_xy = (ORIGIN + cx * r.g, ORIGIN + cy * r.g)
            path = r._astar(nid, sources, targets, tgt_xy, walk, via_m, None, (1.0, 1.0), 1.0, 400000, win)
            if not path:
                continue
            rt = r._path_to_route(nid, path, r.hw0)
            r._commit(rt)
            for L, x1, y1, x2, y2, w in rt.segs:
                routes["segments"].append({"net": net, "layer": LAYERS[L], "x1": round(x1, 4), "y1": round(y1, 4),
                                           "x2": round(x2, 4), "y2": round(y2, 4), "w": round(w, 4)})
            for vx, vy in rt.vias:
                routes["vias"].append({"net": net, "x": round(vx, 4), "y": round(vy, 4), "d": r.via_d, "drill": r.via_drill})
            _, via_ok, _ = r.masks(nid, r.hw0)
            added.append({"trace_mm": round(sum(((s[3] - s[1]) ** 2 + (s[4] - s[2]) ** 2) ** 0.5 for s in rt.segs), 2),
                          "vias": len(rt.vias), "component_pads": [f"{p['ref']}.{p['pad']}" for p in pads]})
            continue
        x, y = round(best[0], 4), round(best[1], 4)
        routes["vias"].append({"net": net, "x": x, "y": y, "d": r.via_d, "drill": r.via_drill})
        r._commit(Route(nid, [], [(x, y)]))
        _, via_ok, _ = r.masks(nid, r.hw0)
        added.append({"x": x, "y": y, "island_layer": best[2], "island_area": round(best[3], 2),
                      "component_pads": [f"{p['ref']}.{p['pad']}" for p in pads]})
    if added:
        (d.project_dir / "out" / "routes.json").write_text(json.dumps(routes, indent=0))
    return added
