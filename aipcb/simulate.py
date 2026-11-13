"""SPICE simulation through the libngspice shared library bundled with KiCad.

Simulation decks live in projects/<p>/sim/*.cir.  Optional directives in comments:
    * @plot v(out) v(in)          vectors to plot (default: all node voltages)
    * @expect <meas> <min> <max>  pass/fail check on a .measure result
    * @title Some description
Each deck is run in a child process (ngspice may abort the process on fatal errors).
Results: out/sim/<name>.json and out/sim/<name>.svg
"""
from __future__ import annotations

import ctypes
import json
import math
import re
import subprocess
import sys
from pathlib import Path

from . import config


# ----------------------------------------------------------- child process
class _VecInfo(ctypes.Structure):
    _fields_ = [("vname", ctypes.c_char_p), ("vtype", ctypes.c_int), ("vflags", ctypes.c_short),
                ("vrealdata", ctypes.POINTER(ctypes.c_double)), ("vcompdata", ctypes.POINTER(ctypes.c_double)),
                ("vlength", ctypes.c_int)]


def _child(cir_path: str, out_path: str) -> None:
    lib = ctypes.CDLL(str(config.NGSPICE_LIB))
    out_lines: list[str] = []
    PRINT = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
    STAT = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
    EXIT = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_bool, ctypes.c_bool, ctypes.c_int, ctypes.c_void_p)
    BGR = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_bool, ctypes.c_int, ctypes.c_void_p)

    def _p(s, i, u):
        out_lines.append(s.decode(errors="replace")); return 0
    cb_p, cb_s = PRINT(_p), STAT(lambda s, i, u: 0)
    cb_e, cb_b = EXIT(lambda *a: 0), BGR(lambda *a: 0)
    lib.ngSpice_Init(cb_p, cb_s, cb_e, None, None, cb_b, None)
    lines = Path(cir_path).read_text().splitlines()
    if not any(l.strip().lower() == ".end" for l in lines):
        lines.append(".end")
    arr = (ctypes.c_char_p * (len(lines) + 1))(*[l.encode() for l in lines], None)
    lib.ngSpice_Circ(arr)
    lib.ngSpice_Command(b"run")
    lib.ngSpice_CurPlot.restype = ctypes.c_char_p
    lib.ngSpice_AllVecs.restype = ctypes.POINTER(ctypes.c_char_p)
    lib.ngGet_Vec_Info.restype = ctypes.POINTER(_VecInfo)
    plot = lib.ngSpice_CurPlot()
    vecs = {}
    names = lib.ngSpice_AllVecs(plot)
    i = 0
    while names and names[i]:
        nm = names[i].decode(); i += 1
        info = lib.ngGet_Vec_Info(f"{plot.decode()}.{nm}".encode())
        if not info:
            continue
        v = info.contents
        n = v.vlength
        if v.vrealdata:
            vecs[nm] = [v.vrealdata[k] for k in range(n)]
        elif v.vcompdata:
            vecs[nm] = {"re": [v.vcompdata[2 * k] for k in range(n)], "im": [v.vcompdata[2 * k + 1] for k in range(n)]}
    Path(out_path).write_text(json.dumps({"plot": plot.decode(), "vectors": vecs, "log": out_lines}))


# ----------------------------------------------------------- parent side
def _directives(text: str) -> dict:
    d = {"plot": [], "expect": [], "title": "", "bandwidth": []}
    for line in text.splitlines():
        m = re.match(r"\*\s*@(\w+)\s*(.*)", line.strip())
        if not m:
            continue
        k, v = m.group(1).lower(), m.group(2).strip()
        if k == "plot":
            d["plot"] += v.split()
        elif k == "expect":
            parts = v.split()
            if len(parts) >= 3:
                d["expect"].append({"meas": parts[0].lower(), "min": float(parts[1]), "max": float(parts[2])})
        elif k == "title":
            d["title"] = v
        elif k == "bandwidth":
            d["bandwidth"] += v.split()
    return d


def _parse_measures(log: list[str]) -> dict:
    out = {}
    for l in log:
        m = re.match(r"^(?:stdout\s+)?(\w+)\s*=\s*([-+0-9.eE]+)", l.strip())
        if m:
            try:
                out[m.group(1).lower()] = float(m.group(2))
            except ValueError:
                pass
    return out


def _bandwidth(vecs: dict, node: str) -> dict:
    key = next((k for k in vecs if k.lower() in (node.lower(), f"v({node.lower()})")), None)
    f = vecs.get("frequency")
    if key is None or f is None:
        return {}
    f = f["re"] if isinstance(f, dict) else f
    v = vecs[key]
    db = [20 * math.log10(max(1e-30, math.hypot(a, b))) for a, b in zip(v["re"], v["im"])] if isinstance(v, dict) else list(v)
    g = max(db)
    k = db.index(g)

    def cross(rng):
        for i in rng:
            j = i + (1 if rng.step > 0 else -1)
            if 0 <= j < len(db) and (db[i] - (g - 3)) * (db[j] - (g - 3)) <= 0 and db[i] != db[j]:
                t_ = (g - 3 - db[i]) / (db[j] - db[i])
                return 10 ** (math.log10(f[i]) + t_ * (math.log10(f[j]) - math.log10(f[i])))
        return None
    out = {"gmax": g}
    lo, hi = cross(range(k, 0, -1)), cross(range(k, len(db) - 1, 1))
    if lo is not None:
        out["f_low"] = lo
    if hi is not None:
        out["f_high"] = hi
    return out


def run_deck(cir: Path, out_dir: Path, timeout: int = 120) -> dict:
    cir = Path(cir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / f"{cir.stem}.raw.json"
    proc = subprocess.run([sys.executable, "-m", "aipcb.simulate", str(cir), str(raw)],
                          capture_output=True, text=True, timeout=timeout, cwd=str(config.ROOT))
    text = cir.read_text()
    dirs = _directives(text)
    res = {"name": cir.stem, "title": dirs["title"], "ok": False, "measures": {}, "checks": [], "error": None}
    if not raw.exists():
        res["error"] = (proc.stderr or proc.stdout)[-2000:] or "ngspice crashed"
        return res
    data = json.loads(raw.read_text())
    raw.unlink()
    log = data["log"]
    res["measures"] = _parse_measures(log)
    errs = [l for l in log if re.search(r"error|fatal|singular|timestep too small", l, re.I)
            and "no error" not in l.lower()]
    res["log_errors"] = errs[:20]
    vecs = data["vectors"]
    res["vectors"] = sorted(vecs)
    for node in dirs["bandwidth"]:          # -3 dB band edges computed from the AC sweep
        res["measures"].update(_bandwidth(vecs, node))
    res["plot"] = data["plot"]
    passed = True
    for e in dirs["expect"]:
        val = res["measures"].get(e["meas"])
        ok = val is not None and e["min"] <= val <= e["max"]
        passed &= ok
        res["checks"].append({**e, "value": val, "pass": ok})
    res["ok"] = bool(vecs) and passed and not any("fatal" in l.lower() for l in errs)
    (out_dir / f"{cir.stem}.json").write_text(json.dumps({**res, "data": vecs}))
    svg = plot_svg(vecs, dirs["plot"], dirs["title"] or cir.stem)
    (out_dir / f"{cir.stem}.svg").write_text(svg)
    res["svg"] = str(out_dir / f"{cir.stem}.svg")
    return res


def run_all(project_dir: Path) -> list[dict]:
    sim_dir = Path(project_dir) / "sim"
    out = Path(project_dir) / "out" / "sim"
    results = [run_deck(c, out) for c in sorted(sim_dir.glob("*.cir"))]
    (out / "summary.json").write_text(json.dumps(results, indent=1)) if results else None
    return results


# ----------------------------------------------------------- SVG plot
_COLORS = ["#2f6fdf", "#e0592a", "#1f9d6b", "#a24fd1", "#c79a0f", "#d1366f"]


def _fmt(v: float) -> str:
    a = abs(v)
    for s, m in (("G", 1e9), ("M", 1e6), ("k", 1e3), ("", 1), ("m", 1e-3), ("µ", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if a >= m * 0.999 or (s == "p"):
            return f"{v / m:.3g}{s}"
    return f"{v:.3g}"


def plot_svg(vecs: dict, want: list[str], title: str, w: int = 760, h: int = 420) -> str:
    xname = next((k for k in ("frequency", "time", "v-sweep", "i-sweep") if k in vecs), None)
    if xname is None:
        xname = next((k for k in vecs if "sweep" in k), None)
    if xname is None:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="80"><text x="10" y="40">{title}: no sweep vector (op point)</text></svg>'
    xv = vecs[xname]
    if isinstance(xv, dict):
        xv = xv["re"]
    logx = xname == "frequency"
    lut = {k.lower(): k for k in vecs}

    def resolve(n):
        n = n.lower()
        bare = n[2:-1] if n.startswith("v(") and n.endswith(")") else n
        return lut.get(n) or lut.get(bare)
    names = [resolve(n) for n in (want or []) if resolve(n)]
    names = names or [k for k in vecs if k not in (xname, "frequency", "time") and not k.startswith(("i(", "@"))
                      and not k.endswith("#branch")][:6]
    series = []
    for n in names:
        v = vecs[n]
        if isinstance(v, dict):
            v = [20 * math.log10(max(1e-30, math.hypot(a, b))) for a, b in zip(v["re"], v["im"])]
            label = f"{n} (dB)"
        else:
            label = n
        series.append((label, v))
    if not series:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="80"><text x="10" y="40">{title}: nothing to plot</text></svg>'
    X = [math.log10(max(x, 1e-30)) for x in xv] if logx else list(xv)
    ys = [y for _, s in series for y in s if math.isfinite(y)]
    y0, y1 = min(ys), max(ys)
    if y1 - y0 < 1e-12:
        y0 -= 1; y1 += 1
    pad = (y1 - y0) * 0.08
    y0 -= pad; y1 += pad
    x0, x1 = min(X), max(X)
    if x1 - x0 < 1e-30:
        x1 = x0 + 1
    L, R, T, B = 70, 20, 36, 50
    pw, ph = w - L - R, h - T - B

    def sx(x): return L + (x - x0) / (x1 - x0) * pw
    def sy(y): return T + (1 - (y - y0) / (y1 - y0)) * ph
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
           f'font-family="Helvetica,Arial,sans-serif" font-size="11">',
           f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
           f'<text x="{L}" y="22" font-size="14" font-weight="600" fill="#222">{title}</text>']
    for k in range(6):
        yv = y0 + (y1 - y0) * k / 5
        out.append(f'<line x1="{L}" x2="{L + pw}" y1="{sy(yv):.1f}" y2="{sy(yv):.1f}" stroke="#e6e6e6"/>')
        out.append(f'<text x="{L - 6}" y="{sy(yv) + 4:.1f}" text-anchor="end" fill="#555">{_fmt(yv)}</text>')
    if logx:
        for dec in range(math.floor(x0), math.ceil(x1) + 1):
            if x0 <= dec <= x1:
                out.append(f'<line y1="{T}" y2="{T + ph}" x1="{sx(dec):.1f}" x2="{sx(dec):.1f}" stroke="#e6e6e6"/>')
                out.append(f'<text x="{sx(dec):.1f}" y="{T + ph + 16}" text-anchor="middle" fill="#555">{_fmt(10 ** dec)}Hz</text>')
    else:
        for k in range(6):
            xv_ = x0 + (x1 - x0) * k / 5
            out.append(f'<line y1="{T}" y2="{T + ph}" x1="{sx(xv_):.1f}" x2="{sx(xv_):.1f}" stroke="#e6e6e6"/>')
            unit = "s" if xname == "time" else ""
            out.append(f'<text x="{sx(xv_):.1f}" y="{T + ph + 16}" text-anchor="middle" fill="#555">{_fmt(xv_)}{unit}</text>')
    out.append(f'<rect x="{L}" y="{T}" width="{pw}" height="{ph}" fill="none" stroke="#999"/>')
    step = max(1, len(X) // 1500)
    for i, (label, s) in enumerate(series):
        pts = " ".join(f"{sx(X[k]):.1f},{sy(s[k]):.1f}" for k in range(0, min(len(X), len(s)), step) if math.isfinite(s[k]))
        c = _COLORS[i % len(_COLORS)]
        out.append(f'<polyline fill="none" stroke="{c}" stroke-width="1.6" points="{pts}"/>')
        out.append(f'<rect x="{L + 10 + i * 120}" y="{h - 18}" width="12" height="3" fill="{c}"/>'
                   f'<text x="{L + 26 + i * 120}" y="{h - 13}" fill="#333">{label}</text>')
    out.append(f'<text x="{L + pw}" y="{h - 13}" text-anchor="end" fill="#555">{xname}</text></svg>')
    return "\n".join(out)


if __name__ == "__main__":
    _child(sys.argv[1], sys.argv[2])
