# aipcb：由 Claude 驅動的本機 EDA

用自然語言描述電路，Claude 撰寫 `design.yaml`。之後的原理圖、元件庫、物理式擺放、自動佈線、GND 鋪銅、SPICE 模擬、ERC/DRC、送廠審核與 Gerber 輸出，全部在本機自動完成，不需要連網。

## 需求
- KiCad 10（`/Applications/KiCad`），本系統使用它的 kicad-cli、元件庫與 libngspice
- Python 3.12 虛擬環境：`uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python numpy pyyaml mcp PySide6-Essentials`

## 使用方式
| 方式 | 指令 |
|---|---|
| 在 Claude Code 中對話 | 在本資料夾開啟 Claude Code，`.mcp.json` 會自動載入 `aipcb` 工具，例如輸入「幫我設計…」或「審核 fm_radio 能否送廠」 |
| 桌面儀表板 | `.venv/bin/python -m aipcb gui fm_radio` |
| 命令列全流程 | `.venv/bin/python -m aipcb build fm_radio` |
| 送廠檔 | `.venv/bin/python -m aipcb fab fm_radio`（AI 審核 PASS 才放行；`--force` 僅限打樣） |

## 流程
`design.yaml` → 元件庫（含 AI 自訂元件）→ 規格驗證 → 原理圖 → ERC → 物理擺放（力導向 + 模擬退火 + 合法化）→ 佈線（雙層 A*、拆線重佈、GND 扇出）→ PCB → DRC（含鋪銅連通性的閉迴路修補）→ SPICE 模擬 → 設計檢查 → 預覽 → **Claude 審核** → 送廠檔

詳細的設計與審核規範見 `CLAUDE.md`。範例專案位於 `projects/fm_radio/`。
