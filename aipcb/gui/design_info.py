"""Side panel of the schematic tab: 電路說明 (what the circuit does, block by block) and 規格書 (device spec sheet).

Source: design.yaml — the optional `spec:` section written by the designer
    spec:
      summary: "一段話說明這個裝置"
      blocks:   {block_name: "白話說明" | {title: "...", text: "..."}}     # one entry per schematic block
      sections: {"電源 Power": {"輸入": "USB-C 5 V", ...}, ...}           # spec-sheet tables (value: str | list)
and the optional `requirements:` section (product requirements document, 設計需求書)
    requirements:
      purpose: "..."                     users: [...]          scenarios: [...]
      functional / non_functional:       # list of {id, text, priority: 必要|建議|選配, design: "how it is met",
                                         #          verify: "sim 01 / test method"} — "sim NN" shows that sim's result
      constraints: [...]   out_of_scope: [...]   acceptance: [...]
      source: "where the requirements come from"      product: {key: value}   # whole-product spec summary
      product_requirements: [... same items + scope: "Board 1 | Board 2 | 機構 | 韌體"]
      deviations: [{item, source, design, reason}]     # where the design departs from the source / open items
plus what the design already knows: notes, parts per block, board / rules, key ICs, and the verification state
(ERC / DRC / checks / SPICE / AI review) from out/.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

import yaml
from PySide6.QtWidgets import QTabWidget, QTextBrowser

PASSIVE = ("R", "C", "L", "FB", "H", "TP", "MH")
CSS = """
<style>
 body { font-size: 13px; }
 h2 { margin: 2px 0 4px 0; } h3 { margin: 14px 0 4px 0; color: #1e4f8f; } h4 { margin: 10px 0 2px 0; }
 table { border-collapse: collapse; margin: 2px 0 6px 0; }
 td, th { border: 1px solid #c9ccd1; padding: 3px 6px; vertical-align: top; }
 th { background: #eef1f5; text-align: left; }
 .k { color: #555; white-space: nowrap; } .muted { color: #777; }
 .ok { color: #1f9d55; font-weight: bold; } .bad { color: #d64545; font-weight: bold; } .warn { color: #c98a0b; font-weight: bold; }
</style>
"""


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _prefix(ref: str) -> str:
    m = re.match(r"[A-Za-z#]+", ref)
    return m.group(0).upper() if m else ref


def _refkey(ref):
    m = re.match(r"([A-Za-z#]+)(\d*)", ref)
    return (m.group(1), int(m.group(2) or 0)) if m else (ref, 0)


def _json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _notes_html(notes: str) -> str:
    """design.yaml notes -> headings (unindented lines) + bullets ('- ' lines, continuation lines appended)"""
    out, items = [], []

    def flush():
        if items:
            out.append("<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>")
            items.clear()
    for line in (notes or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("- "):
            items.append(s[2:])
        elif not line.startswith(" "):
            flush(); out.append(f"<h4>{_e(s)}</h4>")
        elif items:
            items[-1] += " " + s
        else:
            out.append(f"<p>{_e(s)}</p>")
    flush()
    return "".join(out)


def _table(rows, head=None, widths=None) -> str:
    """widths: {column: px} — Qt rich text ignores nowrap, so short CJK labels (必要) get a fixed column width"""
    widths = widths or {}
    h = "<tr>" + "".join(f"<th{f' width={widths[i]}' if i in widths else ''}>{_e(c)}</th>"
                         for i, c in enumerate(head)) + "</tr>" if head else ""
    return "<table width='100%'>" + h + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
                                               for r in rows) + "</table>"


def _val(v) -> str:
    if isinstance(v, (list, tuple)):
        return "<br>".join(_e(x) for x in v)
    if isinstance(v, dict):
        return "<br>".join(f"<span class='k'>{_e(k)}</span>: {_e(x)}" for k, x in v.items())
    return _e(v)


def describe(raw: dict) -> str:
    """電路說明 tab"""
    spec = raw.get("spec") or {}
    parts = raw.get("parts") or {}
    out = [CSS, f"<h2>{_e(raw.get('title', raw.get('project', '')))}</h2>",
           f"<p class='muted'>版次 rev {_e(raw.get('rev', 'A'))} · {len(parts)} 個元件 · "
           f"{len(raw.get('nets') or {})} 個網路</p>"]
    if spec.get("summary"):
        out.append(f"<p>{_e(spec['summary'])}</p>")
    else:
        out.append("<p class='muted'>（design.yaml 尚未寫 <code>spec.summary</code>：請 Claude 補上裝置說明與規格書）</p>")

    by_block: dict[str, list] = {}
    for ref, p in parts.items():
        by_block.setdefault((p or {}).get("block") or "（未分區塊）", []).append(str(ref))
    blocks = spec.get("blocks") or {}
    order = [b for b in blocks if b in by_block] + sorted(b for b in by_block if b not in blocks)
    out.append("<h3>功能區塊 Blocks</h3><p class='muted'>對應原理圖上的虛線框（框名 = 區塊名稱，大寫）。</p>")
    for b in order:
        info = blocks.get(b)
        title, text = (info.get("title", ""), info.get("text", "")) if isinstance(info, dict) else ("", info or "")
        out.append(f"<h4>{_e(b.upper())}{(' — ' + _e(title)) if title else ''}</h4>")
        if text:
            out.append(f"<p>{_e(text)}</p>")
        rows = []
        for ref in sorted(by_block[b], key=_refkey):
            p = parts[ref] or {}
            name = f"<b>{_e(ref)}</b>" if _prefix(ref) not in PASSIVE else _e(ref)
            maker = f" <span class='muted'>({_e(p['manufacturer'])})</span>" if p.get("manufacturer") else ""
            rows.append([name, _e(p.get("value", "")) + maker, _e(p.get("description", ""))])
        out.append(_table(rows, ["元件", "數值 / 型號", "說明"]))

    if raw.get("notes"):
        out.append("<h3>設計筆記 Design notes</h3>" + _notes_html(raw["notes"]))
    return "".join(out)


def spec_sheet(raw: dict, out_dir: Path) -> str:
    """規格書 tab"""
    spec = raw.get("spec") or {}
    parts = raw.get("parts") or {}
    out = [CSS, f"<h2>規格書 — {_e(raw.get('title', raw.get('project', '')))}</h2>",
           f"<p class='muted'>rev {_e(raw.get('rev', 'A'))} · {_e(raw.get('company', ''))}</p>"]
    for sec, body in (spec.get("sections") or {}).items():
        out.append(f"<h3>{_e(sec)}</h3>")
        if isinstance(body, dict):
            out.append(_table([[f"<span class='k'>{_e(k)}</span>", _val(v)] for k, v in body.items()]))
        else:
            out.append(f"<p>{_val(body)}</p>")
    if not spec.get("sections"):
        out.append("<p class='muted'>（design.yaml 尚未寫 <code>spec.sections</code>；以下為由設計資料自動整理的規格）</p>")

    b = {"width": "?", "height": "?", "layers": 2, "thickness": 1.6, "corner_radius": 0, "fab": "jlcpcb"}
    b.update(raw.get("board") or {})
    r = raw.get("rules") or {}
    via = r.get("via", [0.6, 0.3])
    out.append("<h3>電路板 PCB</h3>" + _table([
        ["<span class='k'>尺寸</span>", f"{_e(b['width'])} × {_e(b['height'])} mm，圓角 R{_e(b['corner_radius'])}"],
        ["<span class='k'>層數 / 板厚</span>", f"{_e(b['layers'])} 層 / {_e(b['thickness'])} mm"],
        ["<span class='k'>製造商規格</span>", _e(str(b["fab"]).upper())],
        ["<span class='k'>線寬 / 間距</span>", f"訊號 {_e(r.get('track', 0.2))} mm · 電源 {_e(r.get('power_track', 0.4))} mm · "
                                             f"間距 {_e(r.get('clearance', 0.2))} mm"],
        ["<span class='k'>導通孔</span>", f"Ø{_e(via[0])} / 鑽孔 {_e(via[1])} mm"],
        ["<span class='k'>電源網路</span>", _e(", ".join(map(str, r.get("power_nets") or [])) or "—")],
    ]))

    key = [ref for ref in parts if _prefix(ref) not in PASSIVE and not str((parts[ref] or {}).get("footprint", "")).startswith("MountingHole")]
    rows = []
    for ref in sorted(key, key=_refkey):
        p = parts[ref] or {}
        rows.append([f"<b>{_e(ref)}</b>", _e(p.get("value", "")), _e(p.get("mpn", "")), _e(p.get("manufacturer", "")),
                     _e(p.get("block", ""))])
    out.append("<h3>主要元件 Key components</h3>" + _table(rows, ["位號", "數值", "料號 MPN", "製造商", "區塊"]))

    # ---- verification state from out/
    st = _json(out_dir / "status.json") or {}
    chk = _json(out_dir / "checks.json") or {}
    drc = chk.get("drc") or {}
    rv = _json(out_dir / "review.json") or {}
    sims = _json(out_dir / "sim" / "summary.json") or []

    def mark(ok, text):
        return f"<span class='{'ok' if ok else 'bad'}'>{'✓' if ok else '✗'} {_e(text)}</span>"
    rows = []
    erc = (st.get("erc") or {}).get("state")
    if erc:
        rows.append(["ERC 電氣規則", mark(erc == "ok", erc)])
    if drc:
        ok = drc.get("errors") == 0 and drc.get("unconnected") == 0
        rows.append(["DRC 設計規則", mark(ok, f"錯誤 {drc.get('errors')} · 未連接 {drc.get('unconnected')}")])
    if chk:
        c = chk.get("counts", {})
        rows.append(["自動檢查", mark(chk.get("auto_pass"), f"error {c.get('error', 0)} · warning {c.get('warning', 0)}")])
    if sims:
        n_ok = sum(1 for s in sims if s.get("ok"))
        rows.append(["SPICE 模擬", mark(n_ok == len(sims), f"{n_ok}/{len(sims)} 通過")])
    if rv:
        v = rv.get("verdict", "")
        cls = {"PASS": "ok", "CONDITIONAL": "warn"}.get(v, "bad")
        rows.append(["AI 審核", f"<span class='{cls}'>{_e(v)}</span> <span class='muted'>{_e(rv.get('time', ''))}</span>"])
    out.append("<h3>驗證狀態 Verification</h3>" + (_table(rows) if rows else "<p class='muted'>尚未建置</p>"))
    if sims:
        out.append(_table([[mark(s.get("ok"), s["name"]), _e(s.get("title", ""))] for s in sims], ["模擬", "驗證內容"]))
    return "".join(out)


PRIO_COLOR = {"必要": "#d64545", "建議": "#c98a0b", "選配": "#5a7bd6"}


def _sim_marks(text: str, sims: list) -> str:
    """'sim 01' / 'sim 03' in a verify string -> live PASS/FAIL badges from out/sim/summary.json"""
    out = []
    for num in re.findall(r"(?:sim|模擬)\s*(\d{2})", str(text or ""), flags=re.I):
        s = next((x for x in sims if x["name"].startswith(num)), None)
        if s is None:
            out.append(f"<span class='muted'>sim {num}：尚未執行</span>")
        else:
            out.append(f"<span class='{'ok' if s.get('ok') else 'bad'}'>{'✓ PASS' if s.get('ok') else '✗ FAIL'}</span> "
                       f"<span class='muted'>{_e(s['name'])}</span>")
    return "<br>".join(out)


def requirements(raw: dict, out_dir: Path) -> str:
    """設計需求書 tab"""
    req = raw.get("requirements") or {}
    out = [CSS, f"<h2>設計需求書 — {_e(raw.get('title', raw.get('project', '')))}</h2>",
           f"<p class='muted'>rev {_e(raw.get('rev', 'A'))} · 需求 → 設計 → 驗證 對照</p>"]
    if not req:
        out.append("<p class='muted'>（design.yaml 尚未寫 <code>requirements:</code>：請 Claude 補上產品設計需求書）</p>")
        return "".join(out)
    sims = _json(out_dir / "sim" / "summary.json") or []

    def lst(title, items):
        if items:
            if isinstance(items, str):
                items = [items]
            out.append(f"<h3>{title}</h3><ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>")
    if req.get("source"):
        out.append(f"<p class='muted'>來源：{_e(req['source'])}</p>")
    if req.get("purpose"):
        out.append(f"<h3>目的 Purpose</h3><p>{_e(req['purpose'])}</p>")
    if req.get("product"):
        out.append("<h3>整機規格摘要 Product</h3>" +
                   _table([[f"<span class='k'>{_e(k)}</span>", _val(v)] for k, v in req["product"].items()]))
    lst("使用者 Users", req.get("users"))
    lst("使用情境 Scenarios", req.get("scenarios"))
    for key, title in (("product_requirements", "整機需求與分工 System requirements"),
                       ("functional", "功能需求 Functional requirements"),
                       ("non_functional", "非功能需求 Non-functional requirements")):
        items = req.get(key) or []
        if not items:
            continue
        scoped = any(it.get("scope") for it in items)
        rows = []
        for it in items:
            pr = str(it.get("priority", ""))
            badge = f"<span style='color:{PRIO_COLOR.get(pr, '#555')};font-weight:bold'>{_e(pr)}</span>"
            ver = _e(it.get("verify", ""))
            sm = _sim_marks(it.get("verify", ""), sims)
            row = [f"<b>{_e(it.get('id', ''))}</b>", _e(it.get("text", ""))]
            if scoped:
                sc = str(it.get("scope", ""))
                row.append(f"<b>{_e(sc)}</b>" if "2" in sc else _e(sc))
            rows.append(row + [badge, _e(it.get("design", "")), ver + (f"<br>{sm}" if sm else "")])
        head = ["編號", "需求"] + (["範圍"] if scoped else []) + ["優先", "設計對應", "驗證"]
        out.append(f"<h3>{title}</h3>" + _table(rows, head, widths={0: 64, head.index("優先"): 40}))
    if req.get("deviations"):
        out.append("<h3>與原始需求的差異 / 待決事項 Deviations</h3>" + _table(
            [[f"<b>{_e(d.get('item', ''))}</b>", _e(d.get("source", "")), _e(d.get("design", "")), _e(d.get("reason", ""))]
             for d in req["deviations"]], ["項目", "原始需求", "目前設計", "理由 / 待辦"]))
    lst("限制條件 Constraints", req.get("constraints"))
    lst("不在範圍內 Out of scope", req.get("out_of_scope"))
    lst("驗收標準 Acceptance", req.get("acceptance"))
    return "".join(out)


class DesignInfoPanel(QTabWidget):
    def __init__(self):
        super().__init__()
        self.desc = QTextBrowser(); self.desc.setOpenExternalLinks(True)
        self.spec = QTextBrowser(); self.spec.setOpenExternalLinks(True)
        self.addTab(self.desc, "電路說明 Description")
        self.addTab(self.spec, "規格書 Spec")
        self.req = QTextBrowser(); self.req.setOpenExternalLinks(True)
        self.addTab(self.req, "設計需求書 Requirements")
        self._key = None

    def load(self, project_dir: Path):
        out = project_dir / "out"
        files = [project_dir / "design.yaml", out / "status.json", out / "checks.json", out / "review.json",
                 out / "sim" / "summary.json"]
        key = tuple((str(f), f.stat().st_mtime if f.exists() else 0) for f in files)
        if key == self._key:
            return
        self._key = key
        try:
            raw = yaml.safe_load(files[0].read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001
            self.desc.setHtml(f"<p>無法讀取 design.yaml：{_e(e)}</p>"); self.spec.clear(); self.req.clear()
            return
        for w, h in ((self.desc, describe(raw)), (self.spec, spec_sheet(raw, out)), (self.req, requirements(raw, out))):
            pos = w.verticalScrollBar().value()
            w.setHtml(h)
            w.verticalScrollBar().setValue(pos)
