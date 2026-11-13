"""Two-layer grid router (A* maze routing with rip-up & reroute).

Occupancy model per layer, on a uniform grid (default 0.1 mm):
  * hard   : board edge band, NPTH holes, footprint keepouts  -> blocked for everyone
  * pad    : copper owner net of SMD/THT pads (static)
  * halo   : cells whose centre is closer than clearance + default half-width to copper
             of some net.  Stored as count / sum / sum-of-squares so objects of several
             nets can be added & removed incrementally and "only net n" is testable.
A trace centre-line may visit a cell when every copper/halo object covering it belongs
to the same net.  Wider tracks and vias dilate the blocked mask accordingly.
"""
from __future__ import annotations

import heapq
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .placement import ORIGIN, rot_pt
from .spec import Design

LAYERS = ("F.Cu", "B.Cu")
SQ2 = math.sqrt(2.0)


@dataclass
class Obj:
    net: int
    layers: tuple[int, ...]
    kind: str          # pad / seg / via
    geom: tuple        # pad: (cx, cy, hw, hh, ang, shape)  seg: (x1,y1,x2,y2,hw)  via: (x,y,r)


@dataclass
class Route:
    net: int
    segs: list = field(default_factory=list)   # (layer, x1, y1, x2, y2, width)
    vias: list = field(default_factory=list)   # (x, y)


def _poly_mask(xs: np.ndarray, ys: np.ndarray, poly: list[tuple[float, float]]) -> np.ndarray:
    inside = np.zeros(xs.shape, dtype=bool)
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]; x2, y2 = poly[(i + 1) % n]
        cond = (y1 > ys) != (y2 > ys)
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (x2 - x1) * (ys - y1) / (y2 - y1 + 1e-12) + x1
        inside ^= cond & (xs < xint)
    return inside


class Router:
    def __init__(self, d: Design, positions: dict, log=print):
        self.d = d
        self.log = log
        r = d.rules
        self.g = float(r["grid"])
        self.clear = float(r["clearance"])
        self.hw0 = float(r["track"]) / 2
        self.via_d, self.via_drill = float(r["via"][0]), float(r["via"][1])
        self.margin = 0.01
        b = d.board
        self.W, self.H = float(b["width"]), float(b["height"])
        self.nx = int(round(self.W / self.g)) + 1
        self.ny = int(round(self.H / self.g)) + 1
        self.N = self.nx * self.ny
        shape = (2, self.ny, self.nx)
        self.hard = np.zeros(shape, dtype=bool)
        self.pad_owner = np.zeros(shape, dtype=np.int32)
        self.pad_core = np.zeros(shape, dtype=np.int32)   # pad cells deep enough that a track centred there stays inside
        self.pad_cnt = np.zeros(shape, dtype=np.int32)    # pad halos
        self.pad_sum = np.zeros(shape, dtype=np.int64)
        self.pad_sq = np.zeros(shape, dtype=np.int64)
        self.r_cnt = np.zeros(shape, dtype=np.int32)      # route copper + halos
        self.r_sum = np.zeros(shape, dtype=np.int64)
        self.r_sq = np.zeros(shape, dtype=np.int64)
        self.hole_cnt = np.zeros(shape[1:], dtype=np.int32)    # forbids new via centres near drilled holes
        self.hole_gap = 0.25
        self.net_names = list(d.nets)
        self.net_id = {n: i + 1 for i, n in enumerate(self.net_names)}
        self.routes: dict[int, Route] = {}
        self.route_objs: dict[int, list[Obj]] = {}
        self.pads: list[dict] = []          # abs pad info for connectivity
        self.positions = positions
        self._build_static()

    # ================================================================ geometry
    def cell_xy(self, i, j):
        return ORIGIN + i * self.g, ORIGIN + j * self.g

    def _window(self, x0, y0, x1, y1):
        i0 = max(0, int(math.floor((x0 - ORIGIN) / self.g)))
        i1 = min(self.nx - 1, int(math.ceil((x1 - ORIGIN) / self.g)))
        j0 = max(0, int(math.floor((y0 - ORIGIN) / self.g)))
        j1 = min(self.ny - 1, int(math.ceil((y1 - ORIGIN) / self.g)))
        if i1 < i0 or j1 < j0:
            return None
        xs = ORIGIN + np.arange(i0, i1 + 1) * self.g
        ys = ORIGIN + np.arange(j0, j1 + 1) * self.g
        X, Y = np.meshgrid(xs, ys)
        return (slice(j0, j1 + 1), slice(i0, i1 + 1)), X, Y

    def _dist(self, o: Obj, X, Y):
        if o.kind == "pad":
            cx, cy, hw, hh, ang, shape, rr = o.geom
            dx, dy = X - cx, Y - cy
            r = math.radians(ang); c, s = math.cos(r), math.sin(r)
            lx = c * dx - s * dy
            ly = s * dx + c * dy
            if shape == "circle":
                return np.hypot(lx, ly) - hw
            if shape == "oval":
                rad = min(hw, hh)
                if hw >= hh:
                    lx = np.maximum(np.abs(lx) - (hw - rad), 0); ly = np.abs(ly)
                else:
                    ly = np.maximum(np.abs(ly) - (hh - rad), 0); lx = np.abs(lx)
                return np.hypot(lx, ly) - rad
            # rounded rectangle (rr = corner radius; 0 for plain rect)
            qx, qy = np.abs(lx) - (hw - rr), np.abs(ly) - (hh - rr)
            return np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - rr
        if o.kind == "seg":
            x1, y1, x2, y2, hw = o.geom
            vx, vy = x2 - x1, y2 - y1
            L2 = vx * vx + vy * vy
            if L2 < 1e-12:
                return np.hypot(X - x1, Y - y1) - hw
            t = np.clip(((X - x1) * vx + (Y - y1) * vy) / L2, 0, 1)
            return np.hypot(X - (x1 + t * vx), Y - (y1 + t * vy)) - hw
        x, y, r = o.geom
        return np.hypot(X - x, Y - y) - r

    def _extent(self, o: Obj) -> float:
        if o.kind == "pad":
            return math.hypot(o.geom[2], o.geom[3])
        if o.kind == "seg":
            return o.geom[4]
        return o.geom[2]

    def _bbox(self, o: Obj, grow: float):
        e = self._extent(o) + grow
        if o.kind == "seg":
            x1, y1, x2, y2, _ = o.geom
            return min(x1, x2) - e, min(y1, y2) - e, max(x1, x2) + e, max(y1, y2) + e
        return o.geom[0] - e, o.geom[1] - e, o.geom[0] + e, o.geom[1] + e

    def halo_r(self):
        return self.clear + self.hw0 + self.margin

    def _stamp(self, o: Obj, sign: int, static: bool):
        hr = self.halo_r()
        w = self._window(*self._bbox(o, hr + self.g))
        if w is None:
            return
        sl, X, Y = w
        dist = self._dist(o, X, Y)
        halo = dist <= hr
        n = o.net
        for L in o.layers:
            if static:
                if o.kind == "pad":
                    own = dist <= 1e-9
                    self.pad_owner[L][sl][own] = n
                    depth = min(self.hw0, max(0.0, min(o.geom[2], o.geom[3]) - 0.02))
                    self.pad_core[L][sl][dist <= -depth + 1e-9] = n
                self.pad_cnt[L][sl] += halo * sign
                self.pad_sum[L][sl] += halo * (sign * n)
                self.pad_sq[L][sl] += halo * (sign * n * n)
            else:
                self.r_cnt[L][sl] += halo * sign
                self.r_sum[L][sl] += halo * (sign * n)
                self.r_sq[L][sl] += halo * (sign * n * n)

    # ================================================================ setup
    def _build_static(self):
        d = self.d
        pin_net = d.pin_net()
        ec = float(d.rules["edge_clearance"]) + self.hw0
        ii, jj = np.meshgrid(np.arange(self.nx), np.arange(self.ny))
        X = ORIGIN + ii * self.g; Y = ORIGIN + jj * self.g
        de = np.minimum.reduce([X - ORIGIN, ORIGIN + self.W - X, Y - ORIGIN, ORIGIN + self.H - Y])
        edge = de < ec
        rc = float(d.board.get("corner_radius", 0))
        if rc > 0:
            for cx, cy in ((ORIGIN + rc, ORIGIN + rc), (ORIGIN + self.W - rc, ORIGIN + rc),
                           (ORIGIN + rc, ORIGIN + self.H - rc), (ORIGIN + self.W - rc, ORIGIN + self.H - rc)):
                corner = (np.abs(X - cx) <= rc + 1e-9) & (np.abs(Y - cy) <= rc + 1e-9) & \
                         ((X - cx) * (cx - ORIGIN - self.W / 2) > 0) & ((Y - cy) * (cy - ORIGIN - self.H / 2) > 0)
                edge |= corner & (np.hypot(X - cx, Y - cy) > rc - ec)
        edge[:2, :] = edge[-2:, :] = True
        edge[:, :2] = edge[:, -2:] = True
        self.hard[0] |= edge; self.hard[1] |= edge
        for k in d.board.get("keepouts", []):
            m = (X >= ORIGIN + k["x"]) & (X <= ORIGIN + k["x"] + k["w"]) & (Y >= ORIGIN + k["y"]) & (Y <= ORIGIN + k["y"] + k["h"])
            for L in (0, 1):
                self.hard[L] |= m

        for ref, part in d.parts.items():
            fp = part.footprint
            pos = self.positions.get(ref)
            if fp is None or pos is None:
                continue
            fx, fy, rot = pos["x"], pos["y"], pos["rot"]
            for pad in fp.pads:
                dx, dy = rot_pt(pad.x, pad.y, rot)
                cx, cy = fx + dx, fy + dy
                ang = pad.angle + rot
                layers = tuple(LAYERS.index(l) for l in pad.copper if l in LAYERS)
                net = pin_net.get((ref, pad.number))
                nid = self.net_id.get(net, 0) if net else 0
                if pad.drill:
                    self._stamp_hole(cx, cy, max(pad.drill_w, pad.drill_h, pad.drill), +1)
                if pad.kind == "np_thru_hole":
                    r = max(pad.drill_w, pad.drill_h, pad.drill) / 2 + max(self.clear, 0.25) + self.hw0 + self.margin
                    m = np.hypot(X - cx, Y - cy) <= r
                    self.hard[0] |= m; self.hard[1] |= m
                    continue
                if not layers:
                    continue
                shape = pad.shape if pad.shape in ("circle", "oval") else "rect"
                hw, hh = pad.w / 2, pad.h / 2
                rr = pad.rratio * min(pad.w, pad.h) if pad.shape == "roundrect" else 0.0
                if pad.kind == "thru_hole" and pad.drill:
                    hw = max(hw, pad.drill / 2); hh = max(hh, pad.drill / 2)
                if nid == 0:
                    nid = -1000 - len(self.pads)          # unconnected pad: unique pseudo-net
                o = Obj(nid, layers, "pad", (cx, cy, hw, hh, ang, shape, rr))
                self._stamp(o, +1, static=True)
                self.pads.append({"ref": ref, "pad": pad.number, "net": nid, "x": cx, "y": cy,
                                  "layers": layers, "obj": o, "tht": pad.kind == "thru_hole"})
            for ko in fp.keepouts:
                if not ko["tracks"]:
                    continue
                poly = []
                for px, py in ko["poly"]:
                    dx, dy = rot_pt(px, py, rot)
                    poly.append((fx + dx, fy + dy))
                m = _poly_mask(X, Y, poly)
                for l in ko["layers"]:
                    if l in LAYERS:
                        self.hard[LAYERS.index(l)] |= m
        self.pad_owner_flat = None

    # ================================================================ masks
    def _ok(self, cnt, sm, sq, n):
        return (cnt == 0) | ((sm == n * cnt) & (sq == n * n * cnt))

    def masks(self, n: int, hw: float, soft: bool = False):
        """Return (walk[2,ny,nx] bool, via[ny,nx] bool, penalty[2,ny,nx] bool|None)."""
        own_pad = self.pad_owner == n
        pad_ok = self._ok(self.pad_cnt, self.pad_sum, self.pad_sq, n)
        foreign_pad = (self.pad_owner != 0) & ~own_pad
        r_ok = self._ok(self.r_cnt, self.r_sum, self.r_sq, n)
        base = ~self.hard & pad_ok & ~foreign_pad
        walk = base & r_ok
        extra = hw - self.hw0
        if extra > 1e-6:
            walk = ~self._dilate(~walk, int(math.ceil(extra / self.g)))
        walk |= (self.pad_core == n) & ~self.hard
        vr = int(math.ceil((self.via_d / 2 - self.hw0) / self.g))
        vblock = self._dilate(~(base & r_ok), vr)
        near_pad = self._dilate(self.pad_owner != 0, 1)
        no_pad = ~near_pad[0] & ~near_pad[1]
        no_pad &= self.hole_cnt == 0
        via = ~vblock[0] & ~vblock[1] & no_pad
        pen = None
        if soft:
            sbase = base
            if extra > 1e-6:
                sbase = ~self._dilate(~base, int(math.ceil(extra / self.g)))
            pen = sbase & ~walk
            walk = walk | sbase
            sv = self._dilate(~base, vr)
            via = ~sv[0] & ~sv[1] & no_pad
        return walk, via, pen

    @staticmethod
    def _dilate(m: np.ndarray, r: int) -> np.ndarray:
        if r <= 0:
            return m.copy()
        out = m.copy()
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if dx * dx + dy * dy > r * r + r:
                    continue
                if dx == 0 and dy == 0:
                    continue
                sh = np.zeros_like(m)
                ys = slice(max(dy, 0), m.shape[-2] + min(dy, 0))
                yd = slice(max(-dy, 0), m.shape[-2] + min(-dy, 0))
                xs = slice(max(dx, 0), m.shape[-1] + min(dx, 0))
                xd = slice(max(-dx, 0), m.shape[-1] + min(-dx, 0))
                sh[..., ys, xs] = m[..., yd, xd]
                out |= sh
        return out

    # ================================================================ search
    def _astar(self, n, sources, targets, tgt_xy, walk, via, pen, layer_cost, via_cost, max_exp, window,
               via_target=False):
        nx, N, g = self.nx, self.N, self.g
        wf = walk.reshape(-1).tobytes()
        vf = via.reshape(-1).tobytes()
        pf = pen.reshape(-1).tobytes() if pen is not None else None
        i0, j0, i1, j1 = window
        tx, ty = tgt_xy
        tgt = targets
        moves = ((1, g), (-1, g), (nx, g), (-nx, g), (nx + 1, g * SQ2), (nx - 1, g * SQ2),
                 (-nx + 1, g * SQ2), (-nx - 1, g * SQ2))
        best = {}
        came = {}
        heap = []
        for s in sources:
            if not wf[s]:
                continue
            best[s] = 0.0
            came[s] = -1
            c = s % N
            h = math.hypot((c % nx) * g + ORIGIN - tx, (c // nx) * g + ORIGIN - ty)
            heapq.heappush(heap, (h, 0.0, s))
        exp = 0
        pen_cost = 2.0
        while heap:
            f, gc, cur = heapq.heappop(heap)
            if gc > best.get(cur, 1e18) + 1e-9:
                continue
            if (via_target and vf[cur % N]) or (not via_target and cur in tgt):
                path = []
                while cur != -1:
                    path.append(cur)
                    cur = came[cur]
                return path[::-1]
            exp += 1
            if exp > max_exp:
                return None
            L = cur // N
            c = cur - L * N
            ci, cj = c % nx, c // nx
            lc = layer_cost[L]
            par = came[cur]
            pdir = (cur - par) if par >= 0 and par // N == L else 0
            for dlt, step in moves:
                nb = cur + dlt
                nc = c + dlt
                ni, nj = nc % nx, nc // nx
                if ni < i0 or ni > i1 or nj < j0 or nj > j1 or abs(ni - ci) > 1:
                    continue
                if not wf[nb]:
                    continue
                cost = gc + step * lc
                if pdir and dlt != pdir:
                    cost += g * 0.6
                if pf is not None and pf[nb]:
                    cost += pen_cost
                if cost < best.get(nb, 1e18) - 1e-9:
                    best[nb] = cost
                    came[nb] = cur
                    h = math.hypot(ni * g + ORIGIN - tx, nj * g + ORIGIN - ty)
                    heapq.heappush(heap, (cost + h, cost, nb))
            if vf[c]:
                nb = (1 - L) * N + c
                if wf[nb]:
                    cost = gc + via_cost
                    if pf is not None and (pf[nb] or pf[cur]):
                        cost += pen_cost
                    if cost < best.get(nb, 1e18) - 1e-9:
                        best[nb] = cost
                        came[nb] = cur
                        heapq.heappush(heap, (cost + math.hypot(ci * g + ORIGIN - tx, cj * g + ORIGIN - ty), cost, nb))
        return None

    def _pad_cells(self, pad, walk_layers=None) -> set[int]:
        o = pad["obj"]
        cx, cy, hw, hh, ang, shape, rr = o.geom
        w = self._window(*self._bbox(o, 0))
        cells = set()
        if w is None:
            return cells
        sl, X, Y = w
        inside = self._dist(o, X, Y) <= 1e-9
        js, is_ = np.nonzero(inside)
        j0, i0 = sl[0].start, sl[1].start
        for L in pad["layers"]:
            for j, i in zip(js, is_):
                cells.add(L * self.N + (j + j0) * self.nx + (i + i0))
        if not cells:   # pad smaller than a grid cell: nearest cell
            i = int(round((cx - ORIGIN) / self.g)); j = int(round((cy - ORIGIN) / self.g))
            for L in pad["layers"]:
                cells.add(L * self.N + j * self.nx + i)
        return cells

    # ================================================================ commit
    def _path_to_route(self, n: int, path: list[int], hw: float) -> Route:
        N, nx = self.N, self.nx
        pts = [(c // N, (c % N) % nx, (c % N) // nx) for c in path]
        rt = Route(n)
        k = 0
        while k < len(pts) - 1:
            L = pts[k][0]
            if pts[k + 1][0] != L:
                _, i, j = pts[k]
                rt.vias.append(self.cell_xy(i, j))
                k += 1
                continue
            # run on same layer: compress collinear moves
            start = k
            while k < len(pts) - 1 and pts[k + 1][0] == L:
                k += 1
            run = pts[start:k + 1]
            seg_start = run[0]
            prev_d = None
            for a, b in zip(run, run[1:]):
                d = (b[1] - a[1], b[2] - a[2])
                if prev_d is not None and d != prev_d:
                    rt.segs.append((L, *self.cell_xy(seg_start[1], seg_start[2]), *self.cell_xy(a[1], a[2]), hw * 2))
                    seg_start = a
                prev_d = d
            if len(run) > 1:
                rt.segs.append((L, *self.cell_xy(seg_start[1], seg_start[2]), *self.cell_xy(run[-1][1], run[-1][2]), hw * 2))
        return rt

    def _stamp_hole(self, x: float, y: float, drill: float, sign: int):
        r = drill / 2 + self.via_drill / 2 + self.hole_gap + 0.02
        w = self._window(x - r, y - r, x + r, y + r)
        if w is None:
            return
        sl, X, Y = w
        self.hole_cnt[sl] += (np.hypot(X - x, Y - y) <= r) * sign

    def _commit(self, rt: Route):
        objs = []
        for L, x1, y1, x2, y2, w in rt.segs:
            objs.append(Obj(rt.net, (L,), "seg", (x1, y1, x2, y2, w / 2)))
        for x, y in rt.vias:
            objs.append(Obj(rt.net, (0, 1), "via", (x, y, self.via_d / 2)))
        for o in objs:
            self._stamp(o, +1, static=False)
            if o.kind == "via":
                self._stamp_hole(o.geom[0], o.geom[1], self.via_drill, +1)
        if rt.net in self.routes:
            self.routes[rt.net].segs += rt.segs
            self.routes[rt.net].vias += rt.vias
        else:
            self.routes[rt.net] = Route(rt.net, list(rt.segs), list(rt.vias))
        self.route_objs.setdefault(rt.net, []).extend(objs)

    def ripup(self, n: int):
        for o in self.route_objs.pop(n, []):
            self._stamp(o, -1, static=False)
            if o.kind == "via":
                self._stamp_hole(o.geom[0], o.geom[1], self.via_drill, -1)
        self.routes.pop(n, None)

    def _snapshot(self, nets) -> dict:
        snap = {}
        for n in nets:
            rt = self.routes.get(n)
            snap[n] = (list(self.route_objs.get(n, [])),
                       Route(n, list(rt.segs), list(rt.vias)) if rt else None)
        return snap

    def _restore(self, snap: dict):
        """Put nets back exactly as they were when the snapshot was taken."""
        for n, (objs, rt) in snap.items():
            self.ripup(n)
            for o in objs:
                self._stamp(o, +1, static=False)
                if o.kind == "via":
                    self._stamp_hole(o.geom[0], o.geom[1], self.via_drill, +1)
            if objs:
                self.route_objs[n] = list(objs)
            if rt:
                self.routes[n] = Route(n, list(rt.segs), list(rt.vias))

    # ================================================================ nets
    def net_pads(self, n: int) -> list[dict]:
        return [p for p in self.pads if p["net"] == n]

    def _mst(self, pads):
        if len(pads) < 2:
            return []
        inside = [0]
        rest = list(range(1, len(pads)))
        edges = []
        while rest:
            best = None
            for r in rest:
                for i in inside:
                    dd = math.hypot(pads[r]["x"] - pads[i]["x"], pads[r]["y"] - pads[i]["y"])
                    if best is None or dd < best[0]:
                        best = (dd, i, r)
            edges.append(best)
            inside.append(best[2]); rest.remove(best[2])
        return edges

    def route_net(self, n: int, hw: float, soft=False, max_exp=600000) -> tuple[bool, set]:
        pads = self.net_pads(n)
        if len(pads) < 2:
            return True, set()
        layer_cost = (1.0, 1.35)
        via_cost = 1.2
        walk, via, pen = self.masks(n, hw, soft=soft)
        tree_cells: set[int] = set()
        pending = list(range(len(pads)))
        # start at the pad with most connections nearby: just take MST order
        order = [pads[0]]
        for _, _, r in self._mst(pads):
            order.append(pads[r])
        tree_cells |= self._pad_cells(order[0])
        connected = [order[0]]
        blockers = set()
        ok = True
        for pad in order[1:]:
            tgt = self._pad_cells(pad)
            if tgt & tree_cells:
                tree_cells |= tgt; connected.append(pad); continue
            xs = [p["x"] for p in connected] + [pad["x"]]
            ys = [p["y"] for p in connected] + [pad["y"]]
            grow = 6.0
            path = None
            for attempt in range(3):
                win = (max(0, int((min(xs) - grow - ORIGIN) / self.g)), max(0, int((min(ys) - grow - ORIGIN) / self.g)),
                       min(self.nx - 1, int((max(xs) + grow - ORIGIN) / self.g)), min(self.ny - 1, int((max(ys) + grow - ORIGIN) / self.g)))
                path = self._astar(n, tree_cells, tgt, (pad["x"], pad["y"]), walk, via, pen,
                                   layer_cost, via_cost, max_exp, win)
                if path:
                    break
                grow *= 3
            seg_hw = hw
            if not path and not soft and hw > self.hw0 + 1e-9:
                # neck down only this connection (e.g. into a fine-pitch pad); the rest of the net keeps its width
                walk0, via0, _ = self.masks(n, self.hw0)
                grow0 = 6.0
                for attempt in range(3):
                    win = (max(0, int((min(xs) - grow0 - ORIGIN) / self.g)), max(0, int((min(ys) - grow0 - ORIGIN) / self.g)),
                           min(self.nx - 1, int((max(xs) + grow0 - ORIGIN) / self.g)), min(self.ny - 1, int((max(ys) + grow0 - ORIGIN) / self.g)))
                    path = self._astar(n, tree_cells, tgt, (pad["x"], pad["y"]), walk0, via0, None,
                                       layer_cost, via_cost, max_exp, win)
                    if path:
                        seg_hw = self.hw0
                        break
                    grow0 *= 3
            if not path:
                ok = False
                continue
            if soft and pen is not None:
                pf = pen.reshape(-1)
                cnt_f = self.r_cnt.reshape(-1); sum_f = self.r_sum.reshape(-1); sq_f = self.r_sq.reshape(-1)
                for c in path:
                    if not pf[c]:
                        continue
                    k = int(cnt_f[c])
                    if k and int(sq_f[c]) * k == int(sum_f[c]) ** 2:
                        blockers.add(int(sum_f[c]) // k)
                    elif k:
                        L = c // self.N; cc = c % self.N
                        for other, objs in self.route_objs.items():
                            if other != n and other not in blockers:
                                if any(L in o.layers and self._near(o, cc) for o in objs):
                                    blockers.add(other)
                blockers.discard(n)
                continue
            rt = self._path_to_route(n, path, seg_hw)
            self._commit(rt)
            walk, via, pen = self.masks(n, hw, soft=False)
            tree_cells |= set(path) | tgt
            for a, b in zip(path, path[1:]):        # via cells exist on both layers
                if a % self.N == b % self.N:
                    tree_cells.add(a); tree_cells.add(b)
            connected.append(pad)
        return ok, blockers

    def _near(self, o: Obj, cc: int) -> bool:
        i, j = cc % self.nx, cc // self.nx
        x, y = self.cell_xy(i, j)
        X = np.array([[x]]); Y = np.array([[y]])
        return float(self._dist(o, X, Y)[0, 0]) <= self.halo_r() + self.via_d

    def _flanked(self, n: int) -> list[dict]:
        """GND SMD pads sitting between two non-GND pads of the same part (fine-pitch pin rows)."""
        out = []
        for p in self.net_pads(n):
            if p["tht"]:
                continue
            nb = [q for q in self.pads if q["ref"] == p["ref"] and q is not p
                  and math.hypot(q["x"] - p["x"], q["y"] - p["y"]) < 0.8]
            if len(nb) >= 2 and all(q["net"] != n for q in nb):
                out.append(p)
        return out

    def fanout_gnd(self, n: int, hw: float, max_len: float = 4.0, only_unfanned: bool = False,
                   pads: list | None = None, trace_only: bool = False, toward: tuple | None = None) -> int:
        """Drop a stitching via next to every GND SMD pad (connects to the pour).
        trace_only: skip the via and trace straight to nearby GND copper (boxed-in pins)."""
        made = 0
        for pad in (pads if pads is not None else self.net_pads(n)):
            if pad["tht"] or (only_unfanned and pad.get("fanned")):
                continue
            if not only_unfanned and pad.get("fanned"):
                continue
            walk, via, _ = self.masks(n, hw)
            src = self._pad_cells(pad)
            # a via directly reachable on the pad's layer
            win = (max(0, int((pad["x"] - max_len - ORIGIN) / self.g)), max(0, int((pad["y"] - max_len - ORIGIN) / self.g)),
                   min(self.nx - 1, int((pad["x"] + max_len - ORIGIN) / self.g)), min(self.ny - 1, int((pad["y"] + max_len - ORIGIN) / self.g)))
            if toward is not None:
                # only search between the pad and the part centre (via under the body, escape lanes stay free)
                m = 0.6
                x0, x1 = sorted((pad["x"], toward[0])); y0, y1 = sorted((pad["y"], toward[1]))
                win = (max(0, int((x0 - m - ORIGIN) / self.g)), max(0, int((y0 - m - ORIGIN) / self.g)),
                       min(self.nx - 1, int((x1 + m - ORIGIN) / self.g)), min(self.ny - 1, int((y1 + m - ORIGIN) / self.g)))
            path = None if trace_only else self._astar(n, src, set(), (pad["x"], pad["y"]), walk, via, None,
                                                        (1.0, 1.0), 99, 40000, win, via_target=True)
            if not path:
                # boxed in (e.g. GND pin between two signal pins): trace to nearby GND copper instead
                tgt = set()
                for p2 in self.net_pads(n):
                    if p2 is not pad and (p2["tht"] or p2.get("fanned")):
                        tgt |= self._pad_cells(p2)
                for o in self.route_objs.get(n, []):
                    if o.kind == "via":
                        i = int(round((o.geom[0] - ORIGIN) / self.g)); j = int(round((o.geom[1] - ORIGIN) / self.g))
                        tgt.add(j * self.nx + i); tgt.add(self.N + j * self.nx + i)
                if not tgt:
                    continue
                near = min(tgt, key=lambda c: (((c % self.N) % self.nx) * self.g + ORIGIN - pad["x"]) ** 2 +
                           (((c % self.N) // self.nx) * self.g + ORIGIN - pad["y"]) ** 2)
                nxy = self.cell_xy((near % self.N) % self.nx, (near % self.N) // self.nx)
                win = (max(0, win[0] - 60), max(0, win[1] - 60), min(self.nx - 1, win[2] + 60), min(self.ny - 1, win[3] + 60))
                path = self._astar(n, src, tgt, nxy, walk, via, None, (1.0, 1.3), 1.2, 200000, win)
                if not path:
                    self.log(f"  GND pad {pad['ref']}.{pad['pad']} could not be connected")
                    continue
                self._commit(self._path_to_route(n, path, hw))
                pad["fanned"] = True
                made += 1
                continue
            last = path[-1] % self.N
            path = path + [((1 - path[-1] // self.N) * self.N) + last]
            rt = self._path_to_route(n, path, hw)
            self._commit(rt)
            pad["fanned"] = True
            made += 1
        return made

    def _fanout_blockers(self, n: int, pad: dict, hw: float) -> set[int]:
        walk, via, pen = self.masks(n, hw, soft=True)
        src = self._pad_cells(pad)
        m = 80
        win = (max(0, int((pad["x"] - ORIGIN) / self.g) - m), max(0, int((pad["y"] - ORIGIN) / self.g) - m),
               min(self.nx - 1, int((pad["x"] - ORIGIN) / self.g) + m), min(self.ny - 1, int((pad["y"] - ORIGIN) / self.g) + m))
        path = self._astar(n, src, set(), (pad["x"], pad["y"]), walk, via, pen, (1.0, 1.0), 99, 250000, win, via_target=True)
        if not path:   # no via site at all: soft path to any connected GND copper instead
            tgt = set()
            for p2 in self.net_pads(n):
                if p2 is not pad and (p2["tht"] or p2.get("fanned")):
                    tgt |= self._pad_cells(p2)
            for o in self.route_objs.get(n, []):
                if o.kind == "via":
                    i = int(round((o.geom[0] - ORIGIN) / self.g)); j = int(round((o.geom[1] - ORIGIN) / self.g))
                    tgt.add(j * self.nx + i); tgt.add(self.N + j * self.nx + i)
            if tgt:
                near = min(tgt, key=lambda c: (((c % self.N) % self.nx) * self.g + ORIGIN - pad["x"]) ** 2 +
                           (((c % self.N) // self.nx) * self.g + ORIGIN - pad["y"]) ** 2)
                nxy = self.cell_xy((near % self.N) % self.nx, (near % self.N) // self.nx)
                path = self._astar(n, src, tgt, nxy, walk, via, pen, (1.0, 1.3), 1.2, 250000, win)
        out = set()
        if not path:
            return out
        pf = pen.reshape(-1)
        cnt_f = self.r_cnt.reshape(-1); sum_f = self.r_sum.reshape(-1); sq_f = self.r_sq.reshape(-1)
        cells = list(path)
        # the via landing site occupies both layers: whatever route copper is there must go too
        last = path[-1] % self.N
        vr = int(math.ceil((self.via_d / 2 - self.hw0) / self.g)) + 1
        li, lj = last % self.nx, last // self.nx
        for L in (0, 1):
            for dj in range(-vr, vr + 1):
                for di in range(-vr, vr + 1):
                    if 0 <= li + di < self.nx and 0 <= lj + dj < self.ny:
                        cells.append(L * self.N + (lj + dj) * self.nx + li + di)
        for c in cells:
            if pf[c]:
                k = int(cnt_f[c])
                if k and int(sq_f[c]) * k == int(sum_f[c]) ** 2:
                    out.add(int(sum_f[c]) // k)
                elif k:
                    L = c // self.N; cc = c % self.N
                    for other, objs in self.route_objs.items():
                        if other != n and any(L in o.layers and self._near(o, cc) for o in objs):
                            out.add(other)
        out.discard(n)
        return out

    def stitch(self, n: int, pitch: float = 4.0) -> int:
        walk, via, _ = self.masks(n, self.hw0)
        vr = int(math.ceil((self.via_d + self.clear) / self.g)) + 2
        vm = ~self._dilate(~via, 6)          # stitch only in open copper the pour can actually reach
        made = 0
        step = int(pitch / self.g)
        for j in range(step // 2, self.ny, step):
            for i in range(step // 2, self.nx, step):
                if vm[j, i]:
                    x, y = self.cell_xy(i, j)
                    rt = Route(n, [], [(x, y)])
                    self._commit(rt)
                    vm[max(0, j - vr):j + vr, max(0, i - vr):i + vr] = False
                    made += 1
        return made

    # ================================================================ driver
    def run(self, max_rounds: int = 6, gnd_mode: str = "pour", priority: list[str] | None = None) -> dict:
        d = self.d
        t0 = time.time()
        gnd = [n for n in self.net_names if n.upper() in ("GND", "AGND", "DGND", "PGND")]
        pw = set(d.rules["power_nets"])

        def length(n):
            pads = self.net_pads(self.net_id[n])
            return sum(e[0] for e in self._mst(pads))
        first = list(dict.fromkeys(list(priority or []) + list(d.rules.get("route_first", []))))
        sig = [n for n in self.net_names if n not in gnd and n not in first]
        order = first + sorted([n for n in sig if n in pw], key=length) + sorted([n for n in sig if n not in pw], key=length)
        failed: list[str] = []
        ripcount: dict[str, int] = {}
        stats = {"gnd_fanout_vias": 0, "stitch_vias": 0, "gnd_unconnected": []}
        if gnd_mode == "pour":
            # fan out boxed-in GND pins first, inward under the part, before signals take their escape lanes
            for name in gnd:
                nid = self.net_id[name]
                for pad in self._flanked(nid):
                    c = self.positions.get(pad["ref"])
                    if c:
                        made = self.fanout_gnd(nid, self.hw0, pads=[pad], toward=(c["x"], c["y"]))
                        stats["gnd_fanout_vias"] += made
                        self.log(f"  GND pad {pad['ref']}.{pad['pad']}: pre-fanout "
                                 f"{'under the part' if made else 'not possible'}")
        queue = list(order)
        while queue:
            name = queue.pop(0)
            nid = self.net_id[name]
            hw = d.net_width(name) / 2
            ok, _ = self.route_net(nid, hw)
            if not ok and hw > self.hw0:
                self.ripup(nid)
                ok, _ = self.route_net(nid, self.hw0)
            if ok:
                continue
            # rip-up & reroute
            self.ripup(nid)
            _, blockers = self.route_net(nid, self.hw0, soft=True)
            blk = [self.net_names[b - 1] for b in blockers if b > 0 and ripcount.get(self.net_names[b - 1], 0) < max_rounds
                   and self.net_names[b - 1] not in gnd]
            if blk and ripcount.get(name, 0) < max_rounds:
                for b in blk:
                    self.ripup(self.net_id[b])
                    ripcount[b] = ripcount.get(b, 0) + 1
                ripcount[name] = ripcount.get(name, 0) + 1
                ok, _ = self.route_net(nid, hw)
                if not ok:
                    self.ripup(nid)
                    ok, _ = self.route_net(nid, self.hw0)
                queue = blk + queue
                if ok:
                    self.log(f"  rerouted {name} after ripping {blk}")
                    continue
            ok, _ = self.route_net(nid, self.hw0)   # keep partial routing
            failed.append(name)
            self.log(f"  FAILED {name}")
        failed = [f for f in dict.fromkeys(failed) if not self._complete(self.net_id[f])]
        for name in gnd:
            nid = self.net_id[name]
            if gnd_mode == "pour":
                hw = max(self.hw0, d.net_width(name) / 2)
                stats["gnd_fanout_vias"] += self.fanout_gnd(nid, hw)
                # boxed-in GND pins: rip up the signals in the way, fan out, re-route them
                stats["gnd_fanout_vias"] += self.fanout_gnd(nid, hw, max_len=8.0)   # wider via search first
                for pad in [p for p in self.net_pads(nid) if not p["tht"] and not p.get("fanned")]:
                    blk = self._fanout_blockers(nid, pad, hw)
                    blk = [b for b in blk if b > 0 and self.net_names[b - 1] not in gnd]
                    if not blk:
                        continue
                    snap = self._snapshot([nid] + blk)
                    for b in blk:
                        self.ripup(b)
                    made = self.fanout_gnd(nid, hw, pads=[pad])
                    lost = []
                    for b in blk:
                        bn = self.net_names[b - 1]
                        ok, _ = self.route_net(b, d.net_width(bn) / 2)
                        if not ok:
                            self.ripup(b)
                            ok, _ = self.route_net(b, self.hw0)
                        if not ok:
                            lost.append(bn)
                    if lost:
                        # the via sits in the escape lane of the neighbours (GND pin between two signal pins):
                        # put everything back and reach the GND copper with a trace instead
                        self._restore(snap)
                        pad["fanned"] = False
                        made = self.fanout_gnd(nid, hw, pads=[pad], trace_only=True)
                        if not pad.get("fanned"):
                            # third try: clear the neighbours, trace the pad to GND copper first, re-route them
                            for b in blk:
                                self.ripup(b)
                            made = self.fanout_gnd(nid, hw, pads=[pad], trace_only=True)
                            lost2 = []
                            if pad.get("fanned"):
                                for b in blk:
                                    bn = self.net_names[b - 1]
                                    ok, _ = self.route_net(b, d.net_width(bn) / 2)
                                    if not ok:
                                        self.ripup(b)
                                        ok, _ = self.route_net(b, self.hw0)
                                    if not ok:
                                        lost2.append(bn)
                            if not pad.get("fanned") or lost2:
                                self._restore(snap)
                                pad["fanned"] = False
                                made = 0
                        self.log(f"  GND pad {pad['ref']}.{pad['pad']}: via would strand {lost}; "
                                 f"{'traced to GND copper' if pad.get('fanned') else 'left to the pour'}")
                    stats["gnd_fanout_vias"] += made
                    self.log(f"  GND pad {pad['ref']}.{pad['pad']}: ripped {[self.net_names[b - 1] for b in blk]} -> "
                             f"{'ok' if pad.get('fanned') else 'still open'}")
                stats["gnd_unconnected"] = [f"{p['ref']}.{p['pad']}" for p in self.net_pads(nid)
                                            if not p["tht"] and not p.get("fanned")]
                stats["stitch_vias"] += self.stitch(nid, pitch=float(d.rules.get("stitch_pitch", 4.0)))
            else:
                ok, _ = self.route_net(nid, d.net_width(name) / 2)
                if not ok:
                    failed.append(name)
        segs = sum(len(r.segs) for r in self.routes.values())
        vias = sum(len(r.vias) for r in self.routes.values())
        wl = sum(math.hypot(s[3] - s[1], s[4] - s[2]) for r in self.routes.values() for s in r.segs)
        return {"failed": failed, "segments": segs, "vias": vias, "wirelength_mm": round(wl, 1),
                "seconds": round(time.time() - t0, 1), "nets_total": len(self.net_names), **stats}

    def _complete(self, n: int) -> bool:
        """Union-find connectivity check of the net's pads through its route copper."""
        pads = self.net_pads(n)
        if len(pads) < 2:
            return True
        objs = [p["obj"] for p in pads] + self.route_objs.get(n, [])
        parent = list(range(len(objs)))

        def f(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]; a = parent[a]
            return a
        for a in range(len(objs)):
            for b in range(a + 1, len(objs)):
                if f(a) != f(b) and set(objs[a].layers) & set(objs[b].layers) and self._touch(objs[a], objs[b]):
                    parent[f(a)] = f(b)
        return len({f(i) for i in range(len(pads))}) == 1

    def _touch(self, a: Obj, b: Obj) -> bool:
        pts = []
        if b.kind == "seg":
            pts = [(b.geom[0], b.geom[1]), (b.geom[2], b.geom[3])]
        elif b.kind == "via":
            pts = [(b.geom[0], b.geom[1])]
        else:
            pts = [(b.geom[0], b.geom[1])]
        X = np.array([[p[0] for p in pts]]); Y = np.array([[p[1] for p in pts]])
        if (self._dist(a, X, Y) <= 1e-6).any():
            return True
        if a.kind != "pad":
            pts = [(a.geom[0], a.geom[1])] + ([(a.geom[2], a.geom[3])] if a.kind == "seg" else [])
            X = np.array([[p[0] for p in pts]]); Y = np.array([[p[1] for p in pts]])
            return bool((self._dist(b, X, Y) <= 1e-6).any())
        return False

    def export(self) -> dict:
        out = {"segments": [], "vias": []}
        for n, r in self.routes.items():
            name = self.net_names[n - 1]
            for L, x1, y1, x2, y2, w in _weld_t_junctions(r.segs, r.vias):
                if abs(x1 - x2) < 1e-9 and abs(y1 - y2) < 1e-9:
                    continue
                out["segments"].append({"net": name, "layer": LAYERS[L], "x1": round(x1, 4), "y1": round(y1, 4),
                                        "x2": round(x2, 4), "y2": round(y2, 4), "w": round(w, 4)})
            for x, y in r.vias:
                out["vias"].append({"net": name, "x": round(x, 4), "y": round(y, 4),
                                    "d": self.via_d, "drill": self.via_drill})
        return out


def _weld_t_junctions(segs, vias=(), eps: float = 1e-6) -> list:
    """Turn T-junctions into shared vertices.

    The A* tree may end a branch on the middle of an existing segment (or, after a neck-down, a grid step beside
    its centreline but still inside its copper). KiCad's connectivity only counts end-to-end contacts, so such a
    branch is reported as track_dangling. Split the trunk at the projected point and, for an off-centre end, add
    a short joiner that stays inside the copper both tracks already cover."""
    segs = [tuple(s) for s in segs]
    key = lambda L, x, y: (L, round(x, 4), round(y, 4))
    count, width = {}, {}
    for L, x1, y1, x2, y2, w in segs:
        for x, y in ((x1, y1), (x2, y2)):
            k = key(L, x, y)
            count[k] = count.get(k, 0) + 1
            width[k] = w
    via_pts = {(round(x, 4), round(y, 4)) for x, y in vias}
    ends = {}   # only lone ends: not shared with another segment end and not on a via
    for (L, x, y), c in count.items():
        if c == 1 and (x, y) not in via_pts:
            ends.setdefault(L, []).append(((x, y), width[(L, x, y)]))
    splits = {}   # seg index -> list of (t, x, y)
    joiners = []
    for i, (L, x1, y1, x2, y2, w) in enumerate(segs):
        dx, dy = x2 - x1, y2 - y1
        ll = dx * dx + dy * dy
        if ll < eps:
            continue
        for (px, py), pw in ends.get(L, []):
            t = ((px - x1) * dx + (py - y1) * dy) / ll
            if t <= eps or t >= 1 - eps:
                continue
            qx, qy = x1 + t * dx, y1 + t * dy
            dist = math.hypot(px - qx, py - qy)
            if dist > w / 2 + eps:
                continue
            if (abs(px - x1) < eps and abs(py - y1) < eps) or (abs(px - x2) < eps and abs(py - y2) < eps):
                continue
            splits.setdefault(i, []).append((t, qx, qy))
            if dist > eps:
                joiners.append((L, px, py, qx, qy, min(w, pw)))
    out = []
    for i, (L, x1, y1, x2, y2, w) in enumerate(segs):
        pts = [(x1, y1)] + [(x, y) for _, x, y in sorted(splits.get(i, []))] + [(x2, y2)]
        for (a, b), (c, e) in zip(pts, pts[1:]):
            if abs(a - c) > eps or abs(b - e) > eps:
                out.append((L, a, b, c, e, w))
    return out + joiners


def route(d: Design, positions: dict, out: Path | None = None, log=print, gnd_mode: str = "pour",
          passes: int = 4) -> dict:
    """Multi-pass routing: nets that failed in a pass are routed first in the next one; best pass wins."""
    best = None
    priority: list[str] = []
    for k in range(passes):
        r = Router(d, positions, log=log)
        stats = r.run(gnd_mode=gnd_mode, priority=priority)
        stats["pass"] = k + 1
        open_gnd = stats.get("gnd_unconnected", [])
        log(f"  pass {k + 1}: {len(stats['failed'])} failed {stats['failed']}"
            + (f", GND pins open {open_gnd}" if open_gnd else "") + f"  ({stats['seconds']}s)")
        # an open GND pin is as fatal as an unrouted net (DRC unconnected): count it
        key = (len(stats["failed"]) + len(open_gnd), stats["wirelength_mm"])
        if best is None or key < best[0]:
            best = (key, r, stats)
        if not stats["failed"] and not open_gnd:
            break
        priority = list(dict.fromkeys(stats["failed"] + priority))
    _, r, stats = best
    data = r.export()
    data["stats"] = stats
    out = Path(out or d.project_dir / "out" / "routes.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=0))
    return data
