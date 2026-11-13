"""Geometric connectivity check of a pour net, run with KiCad's bundled Python (it needs `pcbnew`).

Usage:  <kicad python> kicad_geomcheck.py <board.kicad_pcb> [NET ...]      (prints JSON on stdout)

Independent of both KiCad's connectivity engine and zonefix's sample-point heuristic: every filled-zone
island, pad, via and track of the net is taken with its real copper shape, and two items count as connected
only when those shapes actually overlap on a shared copper layer. A thermal relief that was never generated
(starved thermal) therefore leaves the pad in the clearance hole *unconnected* here, which is exactly the case
zonefix used to accept.

Python 3.9 (KiCad bundle): keep this file free of newer syntax and of imports from the aipcb package.
"""
import json
import sys

import pcbnew

IU = 1e6  # internal units per mm


def _mm(v):
    return round(v / IU, 3)


def check(board, net):
    names = {net, "/" + net}
    copper = [lid for lid in (pcbnew.F_Cu, pcbnew.B_Cu)]
    items = []   # dict(kind, label, layers -> SHAPE, bbox, area)

    for z in board.Zones():
        if z.GetNetname() not in names:
            continue
        for lid in copper:
            if not z.GetLayerSet().Contains(lid):
                continue
            polys = z.GetFilledPolysList(lid)
            for i in range(polys.OutlineCount()):
                u = polys.UnitSet(i)
                c = u.BBox().Centre()
                items.append({"kind": "island", "layer": board.GetLayerName(lid), "shapes": {lid: u},
                              "bbox": u.BBox(), "area": u.Area() / IU / IU,
                              "at": [_mm(c.x), _mm(c.y)]})
    for p in board.GetPads():
        if p.GetNetname() not in names:
            continue
        shapes = {lid: p.GetEffectiveShape(lid) for lid in copper if p.IsOnLayer(lid)}
        if not shapes:
            continue
        pos = p.GetPosition()
        items.append({"kind": "pad", "label": "%s.%s" % (p.GetParentFootprint().GetReference(), p.GetNumber()),
                      "shapes": shapes, "bbox": p.GetBoundingBox(), "area": 0.0, "at": [_mm(pos.x), _mm(pos.y)]})
    for t in board.GetTracks():
        if t.GetNetname() not in names:
            continue
        shapes = {lid: t.GetEffectiveShape(lid) for lid in copper if t.IsOnLayer(lid)}
        pos = t.GetPosition()
        kind = "via" if t.GetClass() == "PCB_VIA" else "track"
        items.append({"kind": kind, "shapes": shapes, "bbox": t.GetBoundingBox(), "area": 0.0,
                      "at": [_mm(pos.x), _mm(pos.y)]})

    parent = list(range(len(items)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a in range(len(items)):
        A = items[a]
        for b in range(a + 1, len(items)):
            B = items[b]
            if A["kind"] == "island" and B["kind"] == "island":
                continue          # fills of one zone never overlap; different layers only meet through vias/pads
            if not A["bbox"].Intersects(B["bbox"]):
                continue
            if find(a) == find(b):
                continue
            for lid, sa in A["shapes"].items():
                sb = B["shapes"].get(lid)
                if sb is not None and sa.Collide(sb, 0):
                    parent[find(a)] = find(b)
                    break

    if not items:
        return {"net": net, "ok": True, "islands": 0, "components": 1, "detached": []}
    islands = [i for i, it in enumerate(items) if it["kind"] == "island"]
    if islands:
        main = find(max(islands, key=lambda i: items[i]["area"]))
    else:
        main = find(0)
    groups = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(i)
    detached = []
    for root, members in groups.items():
        if root == main:
            continue
        isl = [items[i] for i in members if items[i]["kind"] == "island"]
        pads = [items[i]["label"] for i in members if items[i]["kind"] == "pad"]
        detached.append({
            "islands": [{"layer": it["layer"], "area_mm2": round(it["area"], 2), "at": it["at"]} for it in isl],
            "pads": pads,
            "vias": sum(1 for i in members if items[i]["kind"] == "via"),
            "tracks": sum(1 for i in members if items[i]["kind"] == "track"),
            "at": items[members[0]]["at"],
        })
    # A floating component that carries no pad is dead copper; one with a pad is an open connection.
    return {"net": net, "ok": not detached, "islands": len(islands), "components": len(groups),
            "detached": detached}


def main(argv):
    board = pcbnew.LoadBoard(argv[1])
    nets = argv[2:] or ["GND"]
    print(json.dumps({"nets": [check(board, n) for n in nets]}))


if __name__ == "__main__":
    main(sys.argv)
