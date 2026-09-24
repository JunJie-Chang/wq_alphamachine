# BRAIN 顧問規則 — 權威數值速查

來源：使用者提供的官方文件整理（2026-08-23 版）
- `Osmosis_分配規則.md`
- `Power_Pool_Alpha_規則.md`
- `WQ_BRAIN_報酬機制完整說明.md`

> 這份是**機器行為的依據**。凡是 `alphamachine/config.py` 裡的門檻與這裡衝突，
> 以這裡為準。網路搜尋來的數字一律不可信 —— 見下方「已推翻的錯誤」。

## 1. 顧問提交門檻（非 CHN，D1）

| 條件 | D1 | D0 |
|---|---|---|
| Sharpe | **> 1.58** | > 2.69 |
| Fitness | **> 1.0** | > 1.5 |
| Turnover | 1% ~ 70% | 同 |
| 單一股票最大權重 | < 10% | 同 |
| Self-correlation | < 0.7（或 Sharpe 高 10%） | 同 |
| Prod-correlation | < 0.7（或 Sharpe 高 10%） | 同 |
| Bias test | 不得失敗 | 同 |

**CHN 例外**：D1 Sharpe ≥ 2.08、Returns ≥ 8%、Fitness ≥ 1.0；
D0 Sharpe ≥ 3.5、Returns ≥ 12%、Fitness ≥ 1.5。

### Sub-universe 公式
- `TOPXXX` universe：
  `subuniverse_sharpe >= 0.75 * sqrt(subuniverse_size / alpha_universe_size) * alpha_sharpe`
- 非 TOPXXX：`subuniverse_sharpe >= ratio * alpha_sharpe`
  （ASI MINVOL1M = 0.295、USA ILLIQUID_MINVOL1M = 0.41、EUR ILLIQUID_MINVOL1M = 0.355）

### IS Ladder Sharpe 門檻

| 年數 | D1 | D0 |
|---|---|---|
| FAIL_THRESHOLD | 1.59 | 2.69 |
| 2–5 年 | 2.38 | 3.96 |
| 6 年 | 2.22 | 3.64 |
| 7 年 | 2.06 | 3.33 |
| 8 年 | 1.90 | 3.17 |
| 9 年 | 1.74 | 2.85 |
| 10 年 | 1.59 | 2.69 |

turnover < 30% 時，PASS 門檻乘以 **0.85**（FAIL_THRESHOLD 不乘）。

## 2. 每日 / 每季提交規則（顧問）

- **每天只有前 4 個 Alpha + 前 1 個 SuperAlpha 計入 base payment**
- Quarterly payment 資格：**該季至少 20 個不同日期**，每天至少提交 1 個
- **3 個月滾動窗口內提交天數 < 20 天 → Value Factor 直接設為最低值**

> 所以最佳策略是「**每天送、每天 1–4 個**」，不是攢一批一次送。
> 天數比數量重要。

## 3. Quality Factor（影響每日 base payment）

- 來源：IS 表現 —— 高 Sharpe、高 fitness、與 BRAIN 池及自己其他 Alpha 的**低相關**
- **universe 越大越好**（如 TOP3000）—— 顧問制度與 User Challenge **相反**
- **MDelay：D1 與 D0 等值**（2023-01-09 起）
- **MRegion：US / EUR / ASI 等值**（2022-05-20 起）
- 乘數全部作用在 Quality Factor 上

### 乘數
- **Dynamic Pyramid Multiplier**：範圍 **1.0 ~ 2.0**，pyramid = region × delay × dataset category，
  **使用率越低 → 乘數越高**，每日變動
- **Theme 乘數**：多 theme 同時符合時 `最終乘數 = 各乘數總和 − theme 數 + 1`
- **Osmosis Daily Rank 乘數**：範圍 **1 ~ 2**，每日更新
- 純 Power Pool Alpha 的 theme 乘數固定 **1x**

## 4. Value Factor（影響季度報酬）

- 範圍 **0 ~ 1**，平均 0.5
- 看的是 **OS（樣本外）**表現，base payment 的 Quality Factor 看 **IS**
- **集中度懲罰**：若 >50% 的 Alpha 集中在單一 region / 相似 turnover /
  相同資料本體 / 彼此高相關 → 降低 Value Factor
- 官方提高建議：sub/super universe 至少保留 **70% 的 Sharpe**、
  turnover **< 30%**、margin **> 4 bps**

## 5. Power Pool

- Sharpe ≥ 1.0
- 相異 operator ≤ **8**（`ts_backfill` / `group_backfill` 不計）
- 相異資料本體 ≤ **3**（不含 country/industry/subindustry/currency/market/sector/exchange）
- Power Pool 相關性 < **0.5**
- 描述必須用 Idea / Rationale 範本，至少 100 字元
- 首次提交滿 3 個月後：純 PP 每日 1 個、每月 10 個（限單一 scope）

## 6. Osmosis

- 至少 **3 個 scope**（region × delay），每個 scope 剛好 **100,000 點**，
  分配給至少 **10 個 Alpha**
- 每週日 23:59 EST 鎖定，7 天後生效

## 7. 已推翻的錯誤（網路搜尋得來的）

| 我原本以為 | 實際 | 影響 |
|---|---|---|
| Sharpe 門檻 1.25 | **顧問 D1 是 1.58** | 1.25 是 **User** 門檻。我的人工閘 1.50 **低於平台要求**，會送出必然被拒的 alpha |
| 每日 2,000 分上限 | 那是 **User Challenge**，非顧問制 | 顧問是「每天前 4 個 Alpha 計入」 |
| 每日提交 2 個較安全 | **4 個才是計入上限** | 少送等於少賺 |
| 小 universe 加分 | 顧問制是 **大 universe 較佳** | 方向相反 |
| D0 比 D1 值錢 | **2023 起等值** | 不需為了 delay 調整策略 |
| 提交總數重要 | **提交天數更重要**（<20 天/季 → Value Factor 最低） | 排程策略完全不同 |

## 8. 本專案實測擬合（既非官方、也非已推翻）

前面每一節都是從官方文件轉錄的，第 7 節是查證後推翻的。這一節是**第三類**：
本專案自己對平台行為做實測後擬合出來的數字。官方文件沒有寫，我們也沒有推翻它，
它只是「目前為止最能重現平台回報數值的解釋」。

> **這類數字不得被當成官方規範引用。** 兩層信任模型（官方文件 vs 社群逆向）
> 原本沒有涵蓋它，導致同一個常數在專案裡被兩種方式對待——這一節就是為了讓它有地方可放。

| 數字 | 內容 | 證據強度 | 目前用途 |
|---|---|---|---|
| `correlate.BRAIN_CORR_YEARS = 4` | SELF_CORRELATION 與 Power Pool 相關性是用**最後四個日曆年**的每日 PnL 變化計算，非全歷史 | commit `b8580e3`（2026-09-20）對上 11 個平台回報值到小數點後四位，**另有 1 個對不上**（SELF 回報 0.2271、算出 0.1791，至今無解釋）。全歷史視窗在每一個樣本上都低讀 0.03–0.05 | 僅 `scripts/corr_powerpool.py` 使用，且額外加 `SAFETY = 0.10` 緩衝。主 pipeline 的 `CorrelationBook` **尚未採用**，待 `scripts/verify_corr_window.py` 在當前帳本上重驗後再決定 |

**明確不適用於 `PROD_CORRELATION`。** 上述 11 個重現值全部是 SELF 或 POWER_POOL，
沒有任何一個是 PROD。PROD 是對比全平台已提交的 alpha，那些 alpha 的 PnL 根本抓不到，
所以把四年視窗套到 PROD 是沒有證據的外推。

重驗方式（離線、不花配額）：

```bash
python3 scripts/verify_corr_window.py --windows 2,3,4,5,6
```

它從 `deep_checks`（已存的 `/check` payload）讀平台自己回報的 SELF_CORRELATION，
從 `pnl` 讀序列，逐一比較各視窗的重現誤差。判定規則寫在該腳本的 docstring 裡，
是**事前**聲明的。
