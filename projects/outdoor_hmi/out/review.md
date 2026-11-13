# AI Design Review — CONDITIONAL

用目前引擎完整跑過一次流程（--keep，零件位置不變）：ERC 0、DRC 0/0、幾何鋪銅檢查通過、模擬全部通過。上一版的阻擋項「兩個 AI 繪製的封裝」已用原廠圖獨立核對，全部符合。剩下唯一的條件是 EC12D 固定腳插入橢圓槽的實體試裝；試裝 OK 就可以改判 PASS。

- **[WARNING] 剩下一項：實體試裝（EC12D 固定腳槽）** — SW6 與 D4 的孔位/焊盤已逐項對照原廠圖（Alps 圖號 No.5、Broadcom DS100 Fig.1/Fig.12），全部符合；EC12D 本體外框已修正為 12.5x11.7。唯一無法從圖面確認的是：原廠固定腳孔畫的是矩形，我們做成 2.05x2.15 的橢圓槽。請用 out/testfit 的 1:1 列印搭配實物確認。
- **[WARNING] FPC 連接器沒有 LCSC 庫存** — Hirose FH12-26S-0.5SH(55)、CSMW-LR00-ATV0E、Molex 53261-0271、XAL4020、TDK CGA X8R、Yageo AC 電阻需從 DigiKey/Mouser 採購。
- **[WARNING] SOS 硬體 3 秒判定的誤差（未變）** — 受 74AUP1G17 VT+ 分布影響為 2.14–3.98 s，韌體必須自行確認按住 3 秒。
- **[INFO] 引擎更新後重建，結果不變** — 用 --keep 重建（位置逐位元相同），套用新的 router（T 形接點焊接、SOT-23-6 GND 預先扇出）與幾何鋪銅檢查：DRC 0/0、幾何檢查通過、4 個模擬通過、checks auto_pass。J4/H3 間隙 0.12 mm 只是低於擺件器 0.4 mm 的間距餘裕，KiCad courtyard 檢查正常。
- **[INFO] 沿用前一版審核的已驗證項目** — TPS61022 與 R40 的真負載斷開處理、PAM8302AASCR、Molex PicoBlade（-40 °C）、Broadcom LED 極性、所有 MPN/LCSC 對照、Board 1 介面需求，相較前一版都沒有變動。