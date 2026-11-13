# AI Design Review — CONDITIONAL

電路與 PCB 在自動驗證上全部通過（ERC/DRC/佈線/模擬），電源、USB、MCU、RTC、音訊的設計檢查也沒有發現錯誤。但 U6 RDA5807M 是 AI 產生的符號，腳位尚未對照原廠 datasheet 確認，LCSC 料號也需要人工核對，所以判定為「有條件通過」：可以打樣 PCB，正式量產或交付組裝前必須先完成以下 error 項。

- **[ERROR] RDA5807M 符號為 AI 產生，腳位未經原廠 datasheet 逐腳核對** — 目前腳位(1 SDA,2 SCLK,3 GND,4 FMIN,5 GND,6 RCLK,7 VDD,8 GND,9 ROUT,10 LOUT)來自記憶，需以 RDA5807M datasheet 封裝圖逐腳確認；若有誤，FM 功能全失。這是送廠前唯一的硬性阻擋項。
- **[WARNING] LCSC 料號需在 JLCPCB 零件庫確認** — 料號依記憶填寫，下單前需逐一搜尋確認庫存與封裝。D3 BAT54C、BT1 CR1220 座、U6 RDA5807M、J4 JST-SH、SW4-6 尚無 LCSC 料號，需補上或改為自行焊接。
- **[WARNING] 拔除 USB 時 VSYS 約有 9 ms 掉到 3.0 V** — 模擬 01：P-MOS 閘極要等 VBUS 經 R5 放電才導通（R5 已由 100k 改為 10k，原本約 90 ms）。這段期間 3.3 V 軌約 2.9 V，ESP32 可能觸發 brown-out。可接受；若要根除，改用理想二極體或負載開關 IC。
- **[WARNING] 電池電壓低於 3.33 V 時 3.3 V 軌脫離穩壓** — 模擬 02：韌體應在 VBAT_SENSE (IO34) 換算約 3.4 V 時關機。電池必須自帶保護板 (PCM)，因為板上沒有過放保護。
- **[WARNING] OLED 模組腳位順序因廠商而異** — J3 依 GND-VCC-SCL-SDA 設計。部分 SSD1306 模組是 VCC-GND，下單模組時要確認，否則接反會燒毀。
- **[WARNING] VSYS 與 +3V3 有一段走線縮到 0.2 mm** — 拆線重佈後的退讓結果。0.2 mm、1 oz 外層約可承載 0.6 A，對 ESP32 峰值 0.5 A 屬邊界值但可用。
- **[INFO] 已通過項目** — ERC 0；DRC 0 錯誤、0 未連接（含原理圖一致性）；48 個網路 100% 佈通；4 個 SPICE 測試全部通過；去耦電容都緊貼腳位；I2C 位址無衝突 (0x10/0x11, 0x51, 0x3C)；USB-C CC 5.1k 與 USBLC6 ESD 都有；ESP32 天線在板緣，禁佈區淨空；自動燒錄時序已驗證；電池座與喇叭座改用不同規格，防止插錯。
- **[INFO] 設計限制（非錯誤）** — 藍牙音訊經 ESP32 8-bit DAC 輸出，音質有限；若重視音質，改用 I2S 放大器（例如 MAX98357A）。CH340C 由 3.3 V 供電，所以燒錄時電源開關必須打開。