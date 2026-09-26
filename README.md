# SRF 無限轉倉網格策略介紹

網頁：`index.html`（原檔：`SRF 無限轉倉網格.html`）

第 7 幕「漲跌回放」的資料 `const RP` 由 `python tools\gen_rp.py` 產生（讀 `D:\trade_SRF\data\0050_daily.csv`，規則同 `compare_etf.py --up-grid loss --up-trigger 0.02`，另加「上線時已在市價之上的買線不回補」），輸出整行直接取代 `index.html` 裡的 `const RP = ...;`；`--legacy` 可逐位元重現舊版資料以核對邏輯。
