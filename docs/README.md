# docs/ — BRAIN 規則參考

## 檔案

| 檔案 | 用途 |
|---|---|
| `Windows安裝指南.md` | **Windows 新手安裝教學** — 從裝 Git / Python、clone、裝套件到第一次連線,複製貼上即可。給拿到 repo 連結的人看 |
| `新手指南.md` | **從零開始的教學文** — 平台基本概念 + alphamachine 專案怎麼運作，附即時戰績快照。技術細節仍以 `README.md`／本目錄其他檔案為準 |
| `BRAIN_RULES.md` | **精簡版速查** — 只留機器會用到的數值與公式，加上「已推翻的錯誤」清單 |
| `WQ_BRAIN_報酬機制完整說明.md` | 完整版（740 行）— base/quarterly payment、Genius、Power Pool、Leaderboard、規則變更時間軸、文件互相牴觸處、未載明清單 |
| `Osmosis_分配規則.md` | Osmosis 配點規則與組合建構技巧 |
| `Power_Pool_Alpha_規則.md` | Power Pool 資格、分類、額度、描述規範 |

## 優先權

**這裡的數值優先於任何網路搜尋結果。** 網路上流通的門檻多半是 User 層級的
WorldQuant Challenge 數字，對顧問制度而言系統性偏鬆 —— 最貴的一個例子是
Sharpe「1.25」，顧問 D1 實際是 **1.58**，照 1.25 設閘會送出必然被拒的 alpha。

改動 `alphamachine/config.py` 的任何門檻之前，先來這裡對。

## 完整版比精簡版多的東西

精簡版刻意只留「機器讀得到的數字」。以下只在完整版裡：

- **未載明清單**（第六章，24 項）— 哪些是官方確實沒公開的，避免日後又去猜
- **文件互相牴觸處**（5.2 節，10 項）— 例如 Power Pool 當期 theme 的公告版與
  FAQ 版不一致、Osmosis 鎖定時間有三種說法
- **規則變更時間軸**（第五章）— 判斷某條規則是不是已經過期的依據
- **Leaderboard 各指標的計入範圍矩陣**（4.1 節）
- **Genius tie-breaker 六項與 Community Activity 計分細則**
- **Power Pool 歷史 theme 表**（辨識規則變動節奏）
- 每一條的出處與日期標註

## 來源

三份完整文件是使用者提供的原始檔（2026-08-23 整理），直接從
`C:\Users\user\Downloads\` 複製過來，未經改寫。`BRAIN_RULES.md` 是我從中
蒸餾的速查版，內容有取捨；有疑義時以完整版為準。

## 已知缺口

顧問合約 **Annex A** 沒有查看 —— 多節 FAQ 指向那裡才有確切金額規則。
它在 Profile → Agreements，屬於個人合約。要確認確切金額計算，那才是權威來源。
