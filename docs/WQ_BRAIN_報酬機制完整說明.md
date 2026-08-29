# WorldQuant BRAIN 顧問報酬機制 — 官方文件整理

**整理日期**：2026-08-23
**資料來源範圍**：
1. 平台 Learn → Documentation 全站 84 篇（11 個分區，逐篇檢視）
2. 平台 Learn → FAQ（導向 support.worldquantbrain.com 說明中心，7 個相關分類共 347 篇，含付費/Genius/Power Pool/Osmosis/Glossary）
3. 站內 Messages → Announcements 全部 133 則（日期範圍 2026-04-10 ～ 2026-08-21）
4. 站內 Messages → Notifications 全部 28 則
5. 平台頁面本身：Consultant Leaderboard、Genius Status / Leaderboard、Power Pool Leaderboard、Profile → Performance

**標註約定**
- 每條後面用 `［出處｜日期］` 標示。日期是該文件/公告的最後更新日或發布日。
- 文件沒有明說的，一律寫「**未載明**」，不做推論。
- 文件之間矛盾的，兩個版本都列出並標日期，放在第五章。

---

## 一、每日基礎報酬（Base Payment，accrued daily）

### 1.1 定義與基本規則

| 項目 | 內容 | 出處 |
|---|---|---|
| 定義 | 你每天提交的 Alpha 與 SuperAlpha 所能賺到的金額；設計目的是依「每天投入的品質與數量」給付 | FAQ《What is a Base payment (accrued daily)? How is it calculated?》｜2026-06-18 |
| 計入上限 | **每天前 4 個 Alpha + 前 1 個 SuperAlpha** 才計入計算 | 同上 |
| Alpha 與 SuperAlpha | 用**同一套方法**計算，但**獎金池分開**（所以兩層都提交可以最大化每日收入） | FAQ《What is the compensation for SuperAlpha?》｜2026-08-10 |
| 契約依據 | 詳細金額規則在個人顧問合約的 Annex | FAQ《What is a Base payment...》｜2026-06-18 |
| 平台上哪裡看 | Profile → Performance 頁，分 Regular / Super / Total 三欄，可看 Displayed Period / Yesterday / Current period / Previous period / YTD / Total | 平台 Performance 頁實查｜2026-08-23 |
| 官方口徑的量級 | Starter Pack 寫「Earn up to **$120 per day**（Activity-based）」 | Doc《*Read this First* - Starter Pack》｜未標日期 |

> ⚠️ **完整公式未載明。** 官方沒有公開 base payment 的數學算式（各因子的權重、相乘或相加、如何換算成美元金額都沒寫）。只公開了「由哪些因子構成」與「每個因子怎麼提高」。

### 1.2 組成因子

官方最新一篇（2026-08-09）說是「5 個 components」，但列出的第 4 項「Scaling」其實是機制而非因子；其他兩篇（2026-06-18 / 06-19）明確說是「4 個 components」。兩種說法並陳於第五章。

**因子一：Quantity Factor（數量因子）**

| 規則 | 出處 |
|---|---|
| 每天提交越多 Alpha / SuperAlpha 越高，但有上限 | FAQ《Actionable tips to analyze and attempt to improve your consultant payments》｜2026-08-09 |
| 上限：**每日 4 個 Alpha**、**每日 1 個 SuperAlpha**、**每月 5 個 D0 Alpha** | 同上 |
| 顧問一般每日提交上限亦為 4（未跨過 Power Pool 三個月門檻者） | Doc《Power Pool Alphas》｜未標日期 |
| 取值範圍：**未載明** | — |

**因子二：Quality Factor（品質因子）**

| 規則 | 出處 |
|---|---|
| 取當日所提交全部 Alpha 的 quality factor **平均值** | Doc《WorldQuant Challenge》｜未標日期（註：此篇是 User 版規則，非顧問版，見第六章） |
| 影響來源：IS 期間的表現 —— 高 Sharpe、高 fitness、與 BRAIN 全池及自己其他 Alpha 的低相關 | FAQ《Actionable tips...》｜2026-08-09 |
| 多樣化：使用更大的 universe（如 TOP3000）、不同 region、兩種 delay 都做 | 同上 |
| **MDelay**（Delay 因子）：D1 與 D0 的值**自 2023-01-09 起相等** | 同上 |
| **MRegion**（Region 因子）：US、EUR、ASI 三區的值**自 2022-05-20 起相等** | 同上 |
| Theme 會**乘上** Quality Factor（見 1.3） | FAQ《Will my base payment change, if I submit Theme Alphas?》｜2026-06-18 |
| Prod correlation 仍是 base payment 的計算因素之一，低相關通常賺得多 | FAQ《How do Power Pool Alphas affect compensation?》｜2026-03-16 |
| MDelay / MRegion 的實際數值：**未載明** | — |

**因子三：Self Growth Factor（自我成長因子）**

| 規則 | 出處 |
|---|---|
| 衡量你「隨時間變好」的程度 —— 每天比之前更進步，payout 越高 | FAQ《Actionable tips...》｜2026-08-09 |
| 具體檢視方式：計算近幾週已提交 Alpha 的平均表現（Sharpe、correlation 等），逐區域比較，看是否在變好 | 同上 |
| 另一項：每日提交量是否穩定（持平或遞增） | 同上 |
| 警告：不要為了通過相關性檢查而**故意弄差** Alpha 的表現 | 同上 |
| 計算窗口長度、如何量化「進步」：**未載明** | — |

**因子四：Value Factor（價值因子）**

| 規則 | 出處 |
|---|---|
| 定義：近期提交的 Alpha 組合（combo）在 OS 期間的表現，考慮三件事 —— (a) 個別 Alpha 表現、(b) 近期提交的多樣性、(c) 相對你過去提交與其他顧問提交的獨特性 | FAQ《What is the Value Factor?》｜2026-07-31 |
| **取值範圍 0 ～ 1，平均值 0.5** | 同上 |
| 同時是 base payment **和** quarterly payment 的組成 | 同上 |
| 更新頻率：見第五章（文件寫法與實際公告不一致） | — |
| 提高方式：Alpha 在 sub-universe 或 super-universe 至少保留 **70% 的 Sharpe**；turnover < 30%、margin > 4 bps；**絕不為了通過相關性檢查而塞雜訊** | FAQ《How can you earn more Base payment?》｜2026-08-22 |
| 評估邏輯：把你當季的新 Alpha 組合起來，套上 turnover 等交易限制，在隱藏的 OS 期間檢查三件事 —— combo 本身好不好？加進你自己的池子有沒有加分？加進 BRAIN 全池有沒有加分？ | 同上 |
| 集中度懲罰：若 50% 以上的 Alpha 集中在單一 region / 相似 turnover / 相同資料欄位 / 彼此高相關，可能降低 Value Factor | FAQ《Actionable tips...》｜2026-08-09 |
| 若 3 個月滾動窗口內提交天數 **少於 20 天**，Value Factor 會**重設為最低值** | FAQ《Does number of alpha submissions per month impact value factor?》｜2026-08-08 |
| 單月不提交不影響（因為看的是 3 個月窗口） | 同上 |
| 精確公式：**未載明** | — |

**機制（非因子）：Quantity 與 Quality 的相對縮放**

- Quantity Factor 與 Quality Factor 都會**相對於其他顧問做正規化縮放**，這是一場相對競賽。［FAQ《Will my base payment change, if I submit Theme Alphas?》｜2026-06-18］
- 所以：其他顧問當天提交較少、品質較低或相關性較高時，你比較容易賺得多。［FAQ《Actionable tips...》｜2026-08-09］
- 若很多顧問都做 Theme Alpha，大家都拿到乘數，縮放後可能拉平，base payment 不見得會變。［FAQ《If my base payment has not changed after submitting Theme Alphas...》｜2026-06-19］

### 1.3 乘數（Multipliers）— 全部作用在 Quality Factor 上

**（a）Theme 乘數**

| 規則 | 出處 |
|---|---|
| Theme 只影響 base payment 的 Quality Factor，**不影響 quarterly payment** | FAQ《Does the Theme affect my quarterly payment?》｜2025-01-24 |
| Theme 通常持續 1～3 週，週一（US EST）開始，隔週日結束 | Doc《Overview of Themes》｜未標日期 |
| 同時符合多個 Theme 時：**最終乘數 = 各乘數總和 − Theme 數量 + 1** | Doc《Multiplier Rules》｜未標日期；FAQ《If I submit an Alpha that is eligible for a 2x Pyramid theme multiplier and another 2x theme...》｜2026-03-16 |
| 例：USA Region Theme 3x + Dataset Theme 5x → (3+5) − 2 + 1 = **7x** | Doc《Multiplier Rules》 |
| 例：Pyramid 2x + Theme 2x → 2+2−2+1 = **3x** | FAQ｜2026-03-16 |
| 只用 Theme 資料集的 Alpha 拿到的 Quality Factor **比**混用 Theme 與非 Theme 資料集的**更多** | FAQ《Will my base payment change, if I submit Theme Alphas?》｜2026-06-18 |
| 怎麼確認有沒有套上：模擬後在 IS Testing Status 會顯示「Theme <名稱> matches with multiplier of <倍數>」，不符則顯示 warning | FAQ《How do I know if my Alpha has qualified for the Theme multiplier?》｜2025-01-24 |
| 有提交至少一個 theme Alpha 的顧問，每日 base payout 明顯較高（不含 Thematic Power Pool Competition） | FAQ《Actionable tips...》｜2026-08-09 |

**（b）Dynamic Pyramid Multiplier（動態金字塔乘數）**

| 規則 | 出處 |
|---|---|
| 本身就是一個 Theme，作用於 base fee 的 quality factor | Doc《Overview of Themes》；FAQ《How does the Dynamic Pyramid Multiplier impact payments?》｜2026-06-19 |
| 由該 pyramid 的資料在**全 BRAIN Alpha 中的使用率**決定；**使用率越低 → 乘數越高** | FAQ《How's the Dynamic Pyramid Multiplier computed?》｜2026-07-11 |
| 最低乘數 = 最被使用的 pyramid | FAQ《Does a pyramid with the lowest multiplier mean it is the most utilized?》｜2026-03-24 |
| **每日可能變動** | FAQ《How often does the Dynamic Pyramid Multiplier update?》｜2026-06-01 |
| 在哪看：Data 頁看全部 pyramid 的乘數；模擬後在結果裡看該 Alpha 適用的乘數；Genius Status 頁有完整矩陣 | FAQ《Where is the Dynamic Pyramid Multiplier visible?》｜2026-06-01；平台實查｜2026-08-23 |
| 與 Dataset Value Add Score 的差別：兩者都反映使用率，pyramid 乘數是 theme 因子，用來「補充」value add score | FAQ《How does the Dynamic Pyramid Multiplier differ from the dataset value add score?》｜2026-06-01 |
| Pyramid 定義：region × delay × dataset category，例 USA-D1-analyst；在該 pyramid 提交滿 **3 個 Alpha** 才算「formulated」 | Doc《💡 Brain Genius》；Glossary｜2026-08-20 |
| 乘數與 value factor 不同概念：Dataset Value Score 講的是資料使用不足，**不要跟 Value Factor 搞混** | Doc《Understanding Data in BRAIN》；FAQ《What is the Value Factor?》｜2026-07-31 |

**2026-08-23 平台上的即時 Pyramid 乘數（節錄，區間 1.0 ～ 2.0）**
［出處：平台 Genius → Status → Pyramid theme multipliers｜實查 2026-08-23］

- 最高（2.0）：DEU/GBR 的 Earnings、Fundamental、Imbalance、Institutions、Other、Sentiment、Short Interest、Social Media 等
- USA 區大致落在 1.0 ～ 1.7（Analyst 1.7、Other 1.7、Sentiment 1.7、Model 1.6、News 1.6、Option 1.6、Fundamental 1.5、Insiders 1.5、Price Volume 1.5、Short Interest 1.4、Institutions 1.3、Macro/Risk 1.1、Imbalance 1.0）
- GLB 大致 1.0 ～ 1.4；ASI/CHN/KOR/TWN/IND/MEA 落在 1.0 ～ 1.9
- 完整矩陣為 16 個 region×delay 欄 × 17 個 category 列，數值每日變動，此處僅為當日快照

**（c）Osmosis：Daily Osmosis Rank 乘數**

| 規則 | 出處 |
|---|---|
| Osmosis combo 表現會**作為 daily base payment 的 quality factor 乘數** | Doc《Osmosis Allocation Guide for Consultants》｜未標日期 |
| 計算方式：以「兩個星期日前」鎖定的配點，對每個 scope（region×delay）算出一天的 PNL（取自兩年前的 OS 期間），在全體顧問中排名得到 scope rank，再取你所有 scope rank 的**平均**，即 Daily Osmosis Rank | 同上 |
| **乘數範圍 1 ～ 2，每日更新** | 同上 |
| 平均只計入你有配點的 scope，不是所有可能 scope | 同上 |
| 表現與 scope 數量不成正比，但多樣化有助於降低回撤 | 同上 |
| Leaderboard 上的定義：「Rank of yesterday's returns of Osmosis Combo across all consultants, updated daily using Sunday's weights」 | 平台 Consultant Leaderboard 欄位提示｜實查 2026-08-23 |
| 有做 Osmosis 配點的顧問，每日 base payout 持續高於沒做的人 | FAQ《Actionable tips...》｜2026-08-09 |
| rank → 1～2 之間乘數的**換算方式：未載明** | — |

**Osmosis 配點資格條件**
［Doc《Osmosis Allocation Guide for Consultants》｜未標日期］

- 至少 **3 個 scope**（region×delay，範圍受你的 Genius 等級限制）
- 每個 scope 內要**剛好 100,000 點**，分配給**至少 10 個 Alpha**
- 單一 Alpha 沒有最低點數限制，但每個 scope 總和必須等於 100,000
- 可配點對象：regular / Atom / Power Pool / SuperAlpha 全部都行，提交日期不限
- **鎖定時點：每週日 23:59 EST**；鎖定後 7 天才開始用於每日 combo 計算（例：2026-02-08 週日鎖定 → 用於 2/16～2/22）
- 隨時可改，改幾次都沒有懲罰，但只有週日晚上那一版會被採用
- 工具：Alphas → Submitted 頁可直接內聯編輯 Osmosis Points［公告｜2026-04-29］；「Osmosis Scale」按鈕可一鍵把所有合格 scope 縮放到 100,000 點（至少要有 10 個 Alpha 已配點才會啟用）［公告｜2026-06-25］

**（d）Power Pool Alpha 對 base payment 的影響**

- Power Pool Alpha 也計入每日 4 個 Alpha 的配額、base payment 計算、Genius 資格與 tie-breaker、value factor 與 weight 的計算。［FAQ《How do Power Pool Alphas affect compensation?》｜2026-03-16］
- 若 Power Pool Alpha 拿到高乘數，可以改善 Quality Factor，進而可能提高 base payment。［同上］
- 但**純 Power Pool Alpha 不會拿到一般 Theme 的乘數**；符合 Power Pool theme 時乘數固定為 **1x**。［FAQ《Do pure Power Pool Alphas get multipliers from other (regular) themes?》｜2026-06-09；《Overview of Power Pool Alphas Thematic Competition》｜2026-08-09］

---

## 二、季度報酬（Quarterly Payment）與 Genius 等級

### 2.1 Quarterly Payment 本身

| 項目 | 內容 | 出處 |
|---|---|---|
| 定義 | 針對**上一個日曆季**提交的 Alpha 與 SuperAlpha 給付 | FAQ《What is the Quarterly Payment?》｜2026-08-17 |
| **資格門檻** | 該季內**至少 20 個不同日期**、每天至少提交 1 個 Alpha，且這些 Alpha 在該季內**未被 decommissioned** | 同上；FAQ《Can you earn quarterly payment if your weight and value factor is zero?》｜2026-08-11 |
| 金額上限 | 最高的顧問可拿 **US$25,000 / 季** | 同上 |
| 金額下限 | 合格顧問最低 **US$100**（即使 weight 與 value factor 都是 0） | 同上 |
| 獎金池 | 池子規模隨參與顧問人數縮放 | 同上 |
| 與 base payment 的差異 | Quarterly 把 Alpha 與 SuperAlpha **合併計算**；base payment 是**分開**算 | 同上 |
| 與 base payment 的相同點 | 池子隨人數縮放、不同季可能不同贏家、Value Factor 兩邊都用 | 同上 |
| 主要決定因素 | (1) **Value Factor**（新提交 Alpha 組合的 OS 表現）、(2) **Weight**（所有 Alpha + SuperAlpha 的權重）。兩者**影響力相近** | FAQ《How much impact do Weight and ValueFactor (OS performance) have on a Quarterly Payment?》｜2026-08-15 |
| 精確公式 | **未載明** | — |

### 2.2 兩個組成成分

**Weight（權重）**

| 規則 | 出處 |
|---|---|
| 計算方式：整季每天把你所有 Alpha 的 Net Weight 加總，再取全季**日平均** | FAQ《How can you earn more Quarterly Payment?》｜2026-08-16 |
| 提高方式：提交更多 Alpha、至少 20 個不同日期提交、用高 value score 的資料集、做在 multiplier theme 裡 | 同上 |
| 獨特且高表現的 Alpha 比較容易拿到 weight | 同上 |
| 若全季平均 Net Weight 為負或 0，仍可能拿到 $100 的最低季度給付 | 同上 |
| 官方口徑：「Quant 要好幾年才能累積出可觀的 weight，要有耐心」 | 同上 |
| **只有 ACTIVE 狀態的 Alpha 會累積 weight 並計入季度給付**；DECOMMISSIONED 的不會 | Doc《Parameters in the Simulation results》｜未標日期 |
| ACTIVE → DECOMMISSIONED 的觸發：所依賴的資料集下架，或 OS 期間長期表現不佳，或 WorldQuant 自行決定 | 同上 |
| 狀態顯示 OS-Pending 的 Alpha **也可以**累積 weight | FAQ《How can some consultants have high weight but fewer alphas on the Consultant leaderboard?》｜2025-01-24 |
| SuperAlpha 的 weight 與 Alpha 疊加計入 | FAQ《Do you include SuperAlpha in quarterly payout calculation?》｜2026-06-26 |
| 更新頻率 | 每月更新［FAQ《How is the consultant leaderboard calculated?》｜2026-08-08］ |
| Weight 的**實際計算公式：未載明** | — |

**Value Factor**：定義與提高方式同 1.2 因子四。另補充：
- Value Factor 由 Alpha 與 SuperAlpha 表現共同決定，兩者**貢獻相等**（若兩者都有）。［FAQ《What is the compensation for SuperAlpha?》｜2026-08-10］
- 高 base payment **不保證**高 quarterly payment：base payment 的 Quality Factor 主要看 **IS** 表現（你看得到），quarterly 的 ValueFactor 看 **OS** 表現（你看不到）。［FAQ《If I have earned high Base Payment(s), does it guarantee a high Quarterly Payment?》｜2026-06-07］
- 提交很多 Alpha **不保證**高 quarterly payment —— 這是相對排名，別人用更少 Alpha 拿到更好的 OS 表現，你的排名就會較差。［FAQ《Does submitting many alphas guarantee a high Quarterly Payment?》｜2025-01-24］

**提高 OS 表現（即 Value Factor）的官方建議**
［FAQ《How can I improve my OS performance and thus the ValueFactor Component?》｜2026-07-30］
- 聚焦簡單但實作紮實的想法，刪掉沒有意義或不驅動表現的部分
- 處理缺失或異常資料
- **不要把多個不相關的 Alpha 混在一個 Alpha 裡** —— ValueFactor 本來就會用最佳方式組合你的單一想法 Alpha（考慮兩兩獨特性與近期表現），你事先混好反而讓它做不到，還會放大共同風險
- 10 年 IS 中自行保留 2 年當評估期，用剩下 8 年選 Alpha
- 驗證換掉 operator、欄位、常數、region、universe、設定後表現是否仍保留
- 需要時**不要害怕把 turnover 提高到 30% 以上**，以充分展現想法的 Sharpe

### 2.3 Genius 等級

**四個等級與福利**
［Doc《💡 Brain Genius》｜未標日期；FAQ《What is the BRAIN Genius program?》｜2026-08-19］

| | Gold（新顧問預設） | Expert | Master | Grand Master |
|---|---|---|---|---|
| Research Regions | USA, ASI, EUR, GLB, IND, CHN, GBR, DEU | 跨區更多 Pyramid | ＋AMR, JPN | ＋HKG, KOR, TW |
| 資料欄位存取 | 50% | 61% | 78% | 100% |
| Operator 存取 | 45% | 63% | 78% | 100% |
| SuperAlpha 成分來源（需先滿 100 次 Alpha 提交） | 只能用自己的 Alpha | ＋同校顧問 | ＋同國顧問 | ＋全球所有顧問 |
| 生物辨識登入間隔 | 4 小時 | 6 小時 | 12 小時 | 24 小時 |
| **名額比例** | **70%** | **20%** | **8%** | **2%** |
| 季度給付量級 | 未載明 | 未載明 | 可達 **$2,000 以上** | 可達 **$8,000 以上**（80 倍或更多的提升） | 

- 名額比例的分母：**上一個日曆季有提交、且符合季度給付資格的顧問人數**（即「用掉至少 4% 的每日 4 Alpha + 1 SuperAlpha 季度額度」的人）。［Doc《💡 Brain Genius》］
- 更高等級的顧問更容易被 WorldQuant 考慮實習與正職機會，Master / Grand Master 尤其。［同上］

**評定週期**
- 每個日曆季重新評定；變更在每季第一週生效（自 2025-01-01 起）。［Doc《💡 Brain Genius》；FAQ《How often are these levels evaluated?》｜2026-08-04］
- 對應關係（公告確認）：**Q2 2026 提交的 Alpha → 決定 Q3 2026 的 Genius 等級 → Q3 2026 提交的 Alpha 依 Q3 等級累積季度給付**。［公告《🏆Genius Levels Refreshed!》｜2026-07-15；同名公告｜2026-04-13］
- Q2 2026 的季度給付金額於 2026-08-20 上線；Q1 2026 的於 2026-05-28 上線。［公告｜2026-08-20、2026-05-28］

**三項資格條件（Eligibility Criteria）**
［Doc《💡 Brain Genius》；FAQ《How do I qualify for the higher levels?》｜2026-08-22］
1. 每季提交最低數量的 **signals**（Alpha 與 SuperAlpha 都算）
2. 完成最低數量的 **pyramids**（region × delay × dataset category，每個 pyramid 需至少 3 個 Alpha）
3. 達到最低的 **Combined Alpha Performance**（OS Sharpe）

- 這三項是**門檻，不是排名**。剛好過關和大幅超過是一樣的，所以相對容易達成；真正的競爭在 tie-breaker。［Doc《💡 Brain Genius》］
- 門檻每季公告，且承諾在前一季先公布。［同上］

**2026-Q3（2026-07-01 ～ 2026-09-30）的實際門檻**
［出處：平台 Genius → Status 頁｜實查 2026-08-23］

| 條件 | Gold | Expert | Master | Grand Master |
|---|---|---|---|---|
| Signals | （預設等級） | **20** | **120** | **220** |
| Pyramids Completed | （預設等級） | **10** | **30** | **60** |
| Combined Alpha Performance | 頁面上**未顯示數值**（狀態為「Pending computation, refreshed monthly」） | 同左 | 同左 | 同左 |

**Combined Alpha Performance**
［Doc《💡 Brain Genius》；FAQ《What is Combined Alpha Performance?》｜2026-08-19］
- Genius dashboard 上有**四個**指標，取**最大值**作為 Genius 資格判定依據：
  1. 你提交的全部顧問 Alpha 的 combined OS 表現
  2. 你提交、且**被 WQ 選中**的 Alpha 的 combined OS 表現
  3. 你提交的全部 **Power Pool** Alpha 的 combined OS 表現
  4. **Combined Osmosis Performance**
- 每 4～6 週更新一次
- 季末最後一個月提交的 Alpha 可能**來不及**計入該季的 Combined Alpha Performance（計算要好幾週），但仍計入該季的另外兩項資格條件與 value factor，並計入**下一季**的 Combined Alpha Performance
- Combined Osmosis Performance：把每個星期日的配點所產生的 after-cost PNL 串接起來，算出該季的 after-cost Sharpe；需在前 3 個月內**至少 6 週**有正確配點才有資格取得此指標。［Doc《Osmosis Allocation Guide》］
- Combined Osmosis Performance 用**滾動 3 個月**的短窗口評估，所以就算配點沒變，月與月之間也會有明顯波動；其他 combined 指標用較長的滾動期。［FAQ《Can Combined Osmosis performance change significantly...》｜2026-08-10］
- 2026-08-14 更新的那一輪，Combined Osmosis Performance 的截止日是 2026-07-31，其餘 combined 指標是 2026-06-30。［公告｜2026-08-14］

**Tie-breaker（超額報名時的排序）**
［Doc《💡 Brain Genius》；FAQ《What happens if more consultants qualify for a level than there are available spots?》｜2026-08-13］
用以下各項**排名加總**：
1. 每個 Alpha 的平均相異 Operator 數（**越低越好**）
2. 每個 Alpha 的平均相異 Field 數（**越低越好**）
3. 相異 Operator 總數（**越高越好**）
4. 相異 Field 總數（**越高越好**）
5. **Maximum Simulation Streak**：該季結束或延續到下一季的最佳連續模擬天數；streak **不會**每季歸零
6. **Community leader**：
   - Forum：季末時已發布的貼文/留言獲得的按讚數（貼文或留言長度至少 100 字元）
   - Referral：該季成功推薦的人數（「成功」= 推薦人已符合累積推薦費的資格）

- `ts_backfill` 與 `group_backfill` **不計入**第 1、3 項的 operator 計數。［同上；公告《⚙️ Update - ts_backfill and group_backfill for consultants》｜2026-05-22］
- **Streak Freeze**：每季 1 次，自動用在該季第一個漏掉的模擬日；未用完不遞延；不追溯保護 2026-08-06 功能上線前中斷的 streak。［Doc《💡 Brain Genius》；公告｜2026-08-06］

**Genius Community Activity 計分細則**
［FAQ《What is the Genius Community Activity criteria?》｜2026-08-22］

| 類別 | 動作 | 分數 | 權重 |
|---|---|---|---|
| Referrals | 當季用你的推薦碼註冊 | 每人 0.5 | **20%** |
| | 當季成為顧問 | 每人 1 | |
| | 當季成為「成功推薦」 | 每人 2 | |
| Forum | 當季發布的**留言**當季獲讚（長度 ≥100 字元） | 10–19 讚：0.1 / 20–49 讚：0.2 / >50 讚：0.5 | **20%** |
| | 當季發布的**貼文**當季獲讚（長度 ≥100 字元） | 10–19 讚：1 / 20–49 讚：2 / >50 讚：5 | |
| Events | 參加 Consultant Opportunity Webinar（至少 30 分鐘） | 每場 2 | **60%** |
| | 參加其他團體 Webinar（非一對一，至少 30 分鐘） | 每場 1 | |
| | 參加線下 meetup | 每場 1 | |

- 分數會跨類別正規化並縮放，最終在 Genius leaderboard 上呈現為 **0～100** 的數值
- 每日 3AM EST 更新；Webinar 出席在會後 7 天內更新；**必須用 BRAIN 上註冊的同一個 email 報名**才會計分
- 每季前幾週分數會波動（活動場次少）

**降級**
- 會失去部分資料集、operator 與功能存取權。［Doc《💡 Brain Genius》］
- 降級後可能無法重新模擬用到已失去存取權的欄位/operator 的舊 Alpha，但仍可把它們用在新的 SuperAlpha 裡。［同上］
- 要回到高等級，必須在後續某一季達到「當時的」門檻。［同上］

**Genius 與季度給付的關係**
- Genius 等級決定季度給付的**區間上下限**；weight 與 value factor 決定你在該區間裡的位置。［FAQ《How do I get a higher quarterly payment within my Genius level's range?》｜2026-07-31］
- Value Factor 每 4～6 週更新，所以用來算季度給付的 value factor 會在季末後 4～6 週才看得到。［同上］
- 各等級的具體區間上下限（Gold / Expert）：**未載明**。只有 Master「$2,000 以上」與 Grand Master「$8,000 以上」的量級敘述。

---

## 三、Power Pool

### 3.1 資格條件（Alpha 層級）

**文件版**［Doc《Power Pool Alphas》｜未標日期］
- Sharpe ≥ 1.0
- 相異 operator 數（含重複使用）**≤ 8**（`ts_backfill` 與 `group_backfill` 不計）
- 相異資料欄位數 **≤ 3**（不含 grouping 欄位：country, industry, subindustry, currency, market, sector, exchange）
- Power Pool Correlation < 0.5
- Turnover 測試 PASS
- Sub-universe 測試 PASS
- Robust-universe 測試（適用時）PASS
- 若與自己的 Power Pool Alpha 自相關 > 0.5，則 Sharpe 必須比最相關的那個高 10% 才算合格

**FAQ 最新版（更完整）**［FAQ《Which Alphas are eligible for Power Pool Alphas Thematic Competition?》｜2026-08-13］
- 以上全部，另加：
- **必須符合至少一個進行中的 Power Pool theme**（模擬後會顯示 match 訊息）
- Turnover 介於 1%～70%（含）
- 除非 theme 規則要求，否則 risk-handled neutralization 非必要
- **Alpha 描述必須依規定格式撰寫**，否則不具備 Power Pool 獎金資格

**Power Pool Alpha 豁免的提交檢查**［同上］
- Prod Correlation
- 與 Power Pool 以外自有 Alpha 的自相關
- Fitness 門檻
- IS Ladder

### 3.2 Regular vs Pure Power Pool 的路由邏輯

［FAQ《What is required for Regular vs. Power Pool submission?》｜2026-08-11］

| 檢查項 | Regular Alpha | Pure Power Pool Alpha |
|---|---|---|
| Sharpe | 必須 PASS（N/√250，N=1.25–2.6 視 region/delay） | ≥1.0 即可；介於 1.0 與 Regular 門檻之間為 **WARNING（非 FAIL）** |
| Fitness / Concentrated Weight / IS Ladder Sharpe | 必須 PASS | **不要求** —— 失敗只是 WARNING |
| Robust Universe test | 必須 PASS | 必須 PASS |
| Turnover、Sub-Universe Sharpe（全區域） | 必須 PASS | 必須 PASS，**無豁免** |
| 相關性檢查 | Self-Corr 與 Prod Corr 必須 PASS（<0.7，或 Sharpe 高 10%） | Self-Corr 與 Prod Corr **完全豁免**；改為必須通過 Power Pool Correlation（對自己的 PP 池 <0.5，或 Sharpe 高 10%） |
| 結構限制 | ≤64 operators | ≤8 operators、≤3 unique datafields（排除支援欄位 country/industry/subindustry/currency/market/sector/exchange/split/adjfactor，與支援 operator ts_backfill、group_backfill） |
| Theme 比對 | 不需要 | **必須**符合至少 1 個進行中的 Power Pool theme（region/delay/universe/dataset 範圍 + 額外條件） |
| 描述格式 | 自由 | 必須用 PowerPool 範本（Idea / Rationale for data / Rationale for operators）才有獎金資格 |
| 每日/每月配額 | 4/日 | 1/日、每月 10（同一 scope，首次 PP 提交滿 3 個月後生效）；[PowerPool+ATOM] 或 [PowerPool+Regular] 不受限 |

**分類組合**［Doc《Power Pool Alphas》］
- 通過其他表現相關測試 → **[Power Pool + Regular]**
- 只用一個資料集且除 IS Ladder 外全過 → **[Power Pool + ATOM]**
- [PP+ATOM] 又通過全部表現測試 → **[Power Pool + ATOM + Regular]**

**「Pure Power Pool submission does not match any Power Pool Theme」錯誤的官方解釋**
［FAQ｜2026-08-22］
- 意思是：你的 Alpha **結構上**合乎 Pure Power Pool 資格，但**不符合當前進行中 theme 的具體條件**。非 Bug，是設計 —— 非主題型的 pure Power Pool 提交被刻意封鎖。
- 最常見錯誤：只顧著把 Alpha 做「純」（operator 少、欄位少），沒檢查設定是否落在 live theme 的範圍內。要對照 theme 公告檢查三件事：
  1. **Region / Delay / Universe** —— theme 通常限定特定組合（如 GLB/D1/TOPDIV100、USA/D1/TOP3000），超出範圍就不可能 match
  2. **限定資料集** —— 有些 theme 只允許固定清單的 dataset ID，用清單外的欄位就 fail，即使是 pv1 這種常見資料集
  3. **額外 theme 條件** —— 如 Robust Universe Sharpe > 1、risk-handled/neutralization 要求等
- 就算你根本沒打算投 Power Pool（例如在別的 region 提交），也可能看到這個錯誤 —— 因為你的 Alpha 結構上仍符合 Pure Power Pool 且未通過 Regular，就會預設落入 Power Pool theme 檢查。
- **好消息**：如果你的 Power Pool Alpha 以 PASS（不是 WARNING）通過全部 Regular 提交測試，仍可以用 Regular 身分提交 —— theme 不符只擋 Pure Power Pool 這條路。

### 3.3 提交配額

［Doc《Power Pool Alphas》；FAQ《Is there a limit to number of daily submissions...》｜2026-07-17］
- **首次提交 Power Pool Alpha 滿 3 個月後**適用：
  - 每月最多 **10 個 pure Power Pool Alpha**，限**單一 scope**：USA D0&D1 / EUR D0&D1 / ASI D1 / GLB D1 / OTHER（JPN, CHN, HKG, TWN, KOR, AMR, IND）D0&D1
  - 每日最多 **1 個 pure Power Pool Alpha**
  - [PP+ATOM]、[PP+Regular]、[PP+ATOM+Regular] **不計入**這兩個限制
  - 範例：1 個 pure PP（符合 theme）+ 1 個 [PP+ATOM] = 當日 2 個 PP 相關提交，仍在配額內
- **未滿 3 個月者**：適用一般顧問提交上限，每日最多 4 個 Alpha
- Pure Power Pool 提交**必須**符合 Power Pool Theme

### 3.4 計分與獎金分配

［FAQ《Overview of Power Pool Alphas Thematic Competition》｜2026-08-09；《What is the scoring criteria...》｜2026-03-16；《How many winners will be declared...》｜2026-03-16；《Key dates...》｜2026-08-06］

- **賽制**：已從舊的「每月、按 region」改為**按 theme**。每個 theme 有自己的規則與 leaderboard，不再有 USA/EUR/ASI 的區域分榜。
- **計分**：依你在該 theme 的 Power Pool Alpha 組合的 **OS After-Cost Combo IR** 排名。
  - Leaderboard 欄位（實查 2026-08-23）：Rank、User、**Tagged Power Pool Alphas**、**Merged PNL**、**BC Combo IR**（Before-Cost Information Ratio of thematic Power Pool Alphas）、**AC Combo IR**（After-Cost Information Ratio）、Country/Region
- **得獎最低門檻**：該 theme leaderboard 內至少 **5 個** 已 tag、且符合該 theme 的 Power Pool Alpha，並且在 theme 結束快照時仍是 tagged 狀態。這 5 個可以是 theme 期間之前提交的。沒有「每月最低提交數」的要求。
- **獎金池**：每月全部 Power Pool Theme 合計 **最高 US$15,000**，最多 **80 人**得獎。
  - 若某月有 2 個 theme，各最多 40 名，合計 80 名
  - 每個 theme 的實際名次與金額在該月 theme 公告時說明
- **實際名次獎金結構**（2026-04 起多次公告一致）：

| 名次 | 金額 |
|---|---|
| 1st | $600 |
| 2nd | $500 |
| 3rd | $400 |
| 4th–8th | $300 each |
| 9th–10th | $250 each |
| 11th–15th | $200 each |
| 16th–20th | $150 each |
| 21st–30th | $125 each |
| 31st–40th | $100 each |

  ［公告｜2026-08-17、2026-08-07、2026-07-26、2026-07-12、2026-06-28、2026-06-14、2026-05-31、2026-04-12］
  註：2026-04 的「All regions/D1 Power Pool Apr'26 2」該 theme 池子是 **US$7,500**（非 15,000），名次結構相同。［公告｜2026-04-12、2026-04-15］
- **一個月最多 4 個 Power Pool theme**，theme 可以在後續月份重複出現。
- **關鍵日期規則**：
  - Theme 開始日：該 theme 的 leaderboard 上線
  - Theme 結束時刻：對所有已 tag 且符合 theme 的 Alpha 做**快照**；最終 OS 分數（含 OS AC Combo IR）以此快照計算
  - Theme 進行中：IS 分數每 **6 小時**刷新一次
  - 結果公布：theme 結束後，通常配合 Opportunity Webinar
  - Theme 可能跨月開始/結束，長度可能只有 2 週 —— 一律以公告的起訖日為準
- Genius 的 Combined Power Pool 分數用的是**季末**時 tagged 的 Power Pool Alpha，**不需要**符合當時的 theme，也不管有沒有得獎資格。［FAQ《How does the Genius combined PowerPool score change with thematic competitions?》｜2026-07-27］

### 3.5 當期主題與截止日

**目前生效（2026-08-23 實查）**
- Leaderboard 上的 Board Name = **「GLB/D1 Liquid Power Pool Aug`26」**（平台 Power Pool Leaderboard 下拉選單，實查 2026-08-23）
- 內容依 2026-08-17 公告：
  > 「The ongoing Power Pool is simplified into a single **GLB Liquid theme till 30 September**. Alphas in **GLB region and TOPDIV3000 universe** qualify. Constraint of **High turnover returns ratio test no longer applies**. 總獎金池最高 US$15,000（名次結構同上）。先前的 high turnover 提交會自動轉移到這個簡化後的 theme。」
  ［公告《📢 GLB/D1 Power Pool Update》｜2026-08-17］
- **⚠️ 與說明中心 FAQ 頁面衝突**，見第五章第 5.2 節。

**近期歷史 theme（供辨識規則變動節奏）**

| Theme 名稱 | 期間 | 條件 | 公告日 |
|---|---|---|---|
| GLB/D1 Liquid Power Pool Aug`26 | 2026-08-17 ～ 2026-09-30 | GLB / D1 / TOPDIV3000；取消 High Turnover 條件 | 2026-08-17 |
| GLB/D1 Power Pool Aug`26 2 | 2026-08-10 ～ 2026-08-23 | GLB / D1 / TOPDIV3000 + 通過 High Turnover returns ratio test；禁用 model110 | 2026-08-09 |
| GLB/D1 Power Pool Aug`26 | 2026-07-27 ～ 2026-08-09 | GLB / D1 / TOPDIV3000 + neutralization ∈ {Slow, Fast, Slow+Fast, Crowding, RAM}；禁用 PV1、MODEL110（支援欄位除外） | 2026-07-26 |
| USA/D1 Power Pool July`26 2 | 2026-07-12 ～ 2026-07-26 | USA / D1 / TOP1000 + 通過 High Turnover returns ratio test；禁用 PV1 | 2026-07-12 |
| USA/D1 Power Pool July`26 | 2026-06-29 ～ 2026-07-12 | USA / D1 / TOP1000 + neutralization ∈ {Slow, Fast, Slow+Fast, Crowding, RAM, Statistical}；禁用 PV1 | 2026-06-28 |
| GLB/D1 Dataset Power Pool June`26 | 2026-06-15 ～ 2026-06-28 | GLB / D1，資料集清單見 Data 頁 Theme 勾選框 | 2026-06-14 |
| USA/D1 Fast Datasets Power Pool June`26 | 2026-06-01 ～ 2026-06-14 | USA / D1，資料集清單見 Data 頁 Theme 勾選框 | 2026-05-31 |
| All regions/D0 Power Pool May`26 | 2026-05-01 ～ 2026-05-31 | delay = 0 | 2026-04-30 |
| All regions/D1 Power Pool Apr`26 2 | 2026-04-13 ～ 2026-04-30（延長） | delay = 1 + Slow+Fast factors neutralization；KOR/IND 禁 PV1、GLB 禁 model110；池子 US$7,500 | 2026-04-12 / 04-15 |

**其他（非 Power Pool）Theme —— 影響 base payment 乘數**

| Theme | 期間 | 乘數 | 條件 | 公告日 |
|---|---|---|---|---|
| GLB High Turnover Theme | 2026-06-15 ～ 2026-08-09（8 週） | **2X**（一般高換手 Alpha）/ **3X**（符合 Investable、Liquid 或 Orthogonal 分類之一） | GLB region + Turnover > 20 + High Turnover returns ratio > 0.75 | 2026-06-14 |
| GLB HighTurnover Datasets Theme | 2026-05-25 ～ 2026-06-14（延長） | 2X | GLB region + turnover > 20% | 2026-05-24 / 06-05 |
| Orthogonal HTVR Theme | 2026-05-11 ～ 2026-05-24 | 3X | USA + Turnover>20% + (HT returns ratio>0.75 或 PnL realization<20) + 套 RAM neutralization 後仍可提交 | 2026-05-10 |
| After Cost HTVR Theme | 2026-04-27 ～ 2026-05-10 | 3X | USA + Turnover>20% + (HT returns ratio>0.75 或 PnL realization<20) + After cost Sharpe > 1.0 | 2026-04-26 |
| Investable HTVR Theme | 公告寫 Mar 13 ～ Apr 26 | 3X | USA + Turnover>20% + (HT returns ratio>0.75 或 PnL realization<20) + (Max Trade Sharpe>2.0 且 Max Trade turnover>20%) 或 (Max Position Sharpe>2.0 且 Max Position turnover>20%) | 2026-04-12 |
| MEA Region Theme | 2026-05-04 ～ 2026-05-31 | 1.5X | Alpha 在 MEA region 模擬 | 2026-05-03 |

---

## 四、各項 Leaderboard 指標

### 4.1 Consultant Leaderboard

**範圍**：使用者成為 conditional consultant 之後提交的所有 Alpha。［FAQ《How is the consultant leaderboard calculated?》｜2026-08-08］

**欄位（實查 2026-08-23，畫面可見部分）**：User｜Weight Factor｜Value Factor｜Daily Osmosis Rank｜Used Data Fields｜Regular Alphas Submitted｜Regular Alpha Mean Prod Corr.｜Regular Alpha Mean Self Corr.｜Super Alphas Submitted｜Super Alpha Mean Prod Corr.｜Super Alpha Mean Self Corr.｜University｜…（右側尚有欄位未捲動到）。可切換 Aggregate by：User / University / Country-Region

| 指標 | 官方定義 | 出處 |
|---|---|---|
| **Weight Factor** | 「Weight of all submitted Alphas」（所有已提交 Alpha 的權重） | 平台欄位提示｜實查 2026-08-23 |
| **Value Factor** | 「OS Performance of the combo of Alphas submitted last quarter」（上一季提交 Alpha 組合的 OS 表現） | 平台欄位提示｜實查 2026-08-23 |
| **Daily Osmosis Rank** | 「Rank of yesterday's returns of Osmosis Combo across all consultants, updated daily using Sunday's weights」（你的 Osmosis Combo 昨日報酬在全體顧問中的排名，每日更新，使用星期日鎖定的權重） | 平台欄位提示｜實查 2026-08-23 |
| **Used Data Fields** | 只計 Alpha，不含 SuperAlpha | FAQ《How is the consultant leaderboard calculated?》｜2026-08-08 |
| **Alphas Submitted（submissionsCount）** | 計數**不包含** 2022 年以前提交的顧問 Alpha，也不含 User 層級的 Alpha | FAQ《How can some consultants have high weight but fewer alphas...》｜2025-01-24 |

**各指標的計入範圍矩陣**［FAQ《How is the consultant leaderboard calculated?》｜2026-08-08］

| # | 指標 | 上季顧問 Alpha | 2022+ 顧問 Alpha | 2022 前顧問 Alpha | 上季 SuperAlpha | 全部 SuperAlpha | User 層 Alpha |
|---|---|---|---|---|---|---|---|
| 1 | Weight | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| 2 | Value Factor | ✓ | ✗ | ✗ | ✓ | ✗ | ✗ |
| 3 | Used data fields | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| 4 | Alphas submitted | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| 5 | Regular Alpha Self Correlation | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| 6 | Regular Alpha Prod Correlation | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| 7 | Super Alpha Self Correlation | ✗ | ✗ | ✗ | ✓ | ✓ | ✗ |
| 8 | Super Alpha Prod Correlation | ✗ | ✗ | ✗ | ✓ | ✓ | ✗ |

**已知門檻/範圍**
- Value Factor：**0 ～ 1**，平均 0.5（Doc/FAQ 明載）。截至 2026-08-23，leaderboard 上多數顧問顯示 0.50。
- Regular Alpha Prod / Self Corr：submission 門檻是 0.7（見 4.3），leaderboard 上顯示的是**平均值**，非門檻。
- Weight Factor：**沒有公開的門檻或上限**。leaderboard 上實際值從 0.00 到超過 100 都有。
- Daily Osmosis Rank：作為乘數時範圍 1～2（Doc《Osmosis Allocation Guide》）；leaderboard 上顯示的數值在 0～1 之間（實查所見 0.00 / 0.21 / 0.67），**兩者的換算關係未載明**。

### 4.2 Genius Leaderboard

**欄位（實查 2026-08-23）**：#（Rank）｜User｜Genius Level｜Best Level｜Signals｜Pyramids Completed｜Combined Alpha Performance｜Combined Power Pool Alpha Performance｜Combined Selected Alpha Performance｜Combined Osmosis Performance｜Community Activity

- 可切換顯示季度（2026-Q3 為當前）
- 可按 User 或 Country/Region 彙總
- Genius Status 頁另有：Tie breaker criteria 現值（Operators per Alpha、Operators used、Fields per Alpha、Fields used、Community activity、Max simulation streak）、Simulation activity 月曆（含 freeze 標記）、Submission activity、Osmosis Allocation 表（16 個 region×delay scope）、Pyramid alpha distribution 矩陣、Pyramid theme multipliers 矩陣

### 4.3 提交測試門檻（顧問版，非 CHN 區）

［Doc《Consultant Submission Tests》｜未標日期］

| 條件 | 門檻 |
|---|---|
| Fitness | D0 > 1.5；**D1 > 1** |
| Sharpe | D0 > 2.69；**D1 > 1.58** |
| Turnover | > 1% 且 < 70% |
| Weight | 單一股票最大權重 < 10% |
| Sub universe | Sharpe 需高於門檻（見下） |
| Self-correlation | PnL 序列相關 < 0.7，或 Sharpe 比相關的 Alpha 高至少 10% |
| Prod-correlation | 同上，但比對**全 BRAIN 已提交 Alpha**，非只有自己的 |
| IS Ladder Sharpe | 見下表 |
| Bias test | 不得失敗 |

**CHN 區**：D1 需 Sharpe ≥ 2.08、Returns ≥ 8%、Fitness ≥ 1.0；D0 需 Sharpe ≥ 3.5、Returns ≥ 12%、Fitness ≥ 1.5；另有 Robust universe test（robust universe 需保留至少 40% 的 returns 與 Sharpe）。

**Sub-universe 公式**
- TOPXXX universe：`subuniverse_sharpe >= 0.75 * sqrt(subuniverse_size / alpha_universe_size) * alpha_sharpe`
- 非 TOPXXX：`subuniverse_sharpe >= subuniverse_ratio * alpha_sharpe`，其中 ASI MINVOL1M = 0.295、USA ILLIQUID_MINVOL1M = 0.41、EUR ILLIQUID_MINVOL1M = 0.355

**IS Ladder Sharpe 門檻表**

| 年數 | D1 門檻 | D0 門檻 |
|---|---|---|
| FAIL_THRESHOLD | 1.59 | 2.69 |
| 2 年 | 2.38 | 3.96 |
| 3 年 | 2.38 | 3.96 |
| 4 年 | 2.38 | 3.96 |
| 5 年 | 2.38 | 3.96 |
| 6 年 | 2.22 | 3.64 |
| 7 年 | 2.06 | 3.33 |
| 8 年 | 1.90 | 3.17 |
| 9 年 | 1.74 | 2.85 |
| 10 年 | 1.59 | 2.69 |

- 若 turnover < 30%，PASS_THRESHOLDS **乘以 0.85**；FAIL_THRESHOLD 不乘。
- **Single Dataset Alpha（ATOM）** 可跳過 IS Ladder，改為只需通過「最近 2 年平均 IS Sharpe」門檻：D1 = 2.38、D0 = 3.96（同樣有 turnover<30% 的 0.85 折扣）。［Doc《❗ Single Dataset Alphas》］
- **Cluster Test**：Cluster Sharpe ≥ 1.58 即標記為 Cluster Alpha（KOR/JPS/TWN/HKG/IND/GBR/DEU 為 ≥ 1）。**僅是分類，不影響提交條件**。［Doc《Consultant Submission Tests》；公告《🆕 Introducing Cluster Test for Consultants!》｜2026-07-17］
- **GLB Alpha** 另需三個次地理區 Sharpe ≥ 1（AMER / APAC / EMEA）。［Doc《Clear these tests before submitting an Alpha》］
- **ASI Alpha** 另需 Japan Robustness Sharpe ≥ 1；ASI Robust Universe Test 需保留至少 **90%** 的 returns 與 Sharpe。［同上；Doc《Consultant Submission Tests》］
- **Fitness 公式**：`Fitness = Sharpe × sqrt( |Returns| / max(Turnover, 0.125) )`。［Doc《Parameters in the Simulation results》］
- **Fitness 評級**：D1 —— Spectacular >2.5、Excellent >2、Good >1.5、Average >1、Needs Improvement ≤1。［同上］

### 4.4 Power Pool Leaderboard

**欄位（實查 2026-08-23）**：Rank｜User｜Tagged Power Pool Alphas｜Merged PNL｜**BC Combo IR**（Before-Cost Information Ratio of thematic Power Pool Alphas）｜**AC Combo IR**（After-Cost Information Ratio of thematic Power Pool Alphas）｜Country/Region

- 以 Board Name 下拉選單切換 theme。2026-08-23 可選：GLB/D1 Liquid Power Pool Aug`26（當前）、GLB/D1 Power Pool Aug`26、USA/D1 Power Pool July`26 2、USA/D1 Power Pool July'26
- 最終排名依 **OS AC Combo IR**［FAQ《Overview of Power Pool Alphas Thematic Competition》｜2026-08-09］

### 4.5 User 層級的 WorldQuant Challenge（非顧問制度，供對照）

［Doc《WorldQuant Challenge》｜未標日期］
- 每日計分（EST），單日上限 2,000 分，通常提交 1～2 個 Alpha 即達上限
- 由 Quantity Factor 與 Quality Factor 構成，兩者都會**對當天所有提交過 Alpha 的使用者做正規化**
- User 版 Quality Factor 取決於：Universe（越小分數越高）、SelfCorrelation（越低越好）、Fitness（越高越好）、Delay（D1 貢獻比 D0 多）
- 等級：Bronze >1,000、Silver >5,000、Gold >10,000；達 10,000 分可能收到顧問邀請
- ⚠️ **注意**：這是 User 版規則，與顧問版 base payment 的 Quality Factor **不是同一套**（顧問版明確寫 universe「越大越好」、D1/D0 相等）。詳見第五章。

---

## 五、規則變更時間軸

> **範圍限制**：站內 Announcements 只保留 **133 則，最早到 2026-04-10**。2026-04 之前的規則變更公告在平台上已看不到，因此以下時間軸只涵蓋 2026-04-10 ～ 2026-08-21。更早的變更只能從文件內殘留的日期敘述推得（下方「文件內提及的歷史日期」）。

### 5.1 依日期排列的規則變更

| 日期 | 變更內容 | 出處 |
|---|---|---|
| 2026-08-21 | Value Factor 刷新（涵蓋至 2026-06-30 的 3 個月窗口） | 公告 |
| 2026-08-20 | Q2 2026 季度給付金額上線。確認：Q2 2026 提交的 signal 依 2026-04 公布的 Genius 等級累積；Q3 2026 提交的依 2026-07 初公布的等級 | 公告 |
| **2026-08-17** | **Power Pool 簡化為單一「GLB Liquid」theme，一路到 9/30；取消 High turnover returns ratio test 條件；先前的高換手提交自動轉入** | 公告 |
| 2026-08-14 | Combined Alpha Performance 刷新（含 Combined Osmosis Performance）。Osmosis 截止日 7/31，其餘 combined 指標截止日 6/30 | 公告 |
| 2026-08-09 | 推出 GLB/D1 Power Pool Aug`26 2 theme（8/10–8/23），要求通過 High Turnover returns ratio test，禁 model110 | 公告 |
| **2026-08-06** | **推出 Streak Freeze**：每季 1 次，自動用於該季第一個漏掉的模擬日，保護 Genius tie-breaker 的 streak；未用完不遞延；不追溯保護上線前中斷的 streak | 公告；Doc《💡 Brain Genius》 |
| 2026-08-03 | 為顧問開放 DEU 與 GBR 兩個新 region | 公告 |
| 2026-07-28 | Value Factor 刷新（窗口至 2026-05-31） | 公告 |
| 2026-07-26 | 推出 GLB/D1 Power Pool Aug`26（7/27–8/9），限定五種 neutralization，禁 PV1 與 MODEL110 | 公告 |
| **2026-07-17** | **推出 Cluster Test**（USA, EUR, ASI, CHN, GLB, HKG, KOR, TWN, JPN, IND）—— 只做分類，不擋提交 | 公告 |
| 2026-07-15 | Genius 等級刷新。確認：Q2 2026 提交決定 Q3 2026 等級 | 公告 |
| 2026-07-12 | 推出 USA/D1 Power Pool July`26 2（7/12–7/26） | 公告 |
| 2026-07-09 | Combined Alpha Performance 刷新 | 公告 |
| 2026-06-28 | 推出 USA/D1 Power Pool July`26（6/29–7/12），可用 Statistical neutralization | 公告 |
| **2026-06-25** | **推出 Osmosis Scale**：一鍵把所有合格 scope 縮放到 100,000 點（該 scope 需至少 10 個 Alpha 已配點） | 公告；Doc《Osmosis Allocation Guide》 |
| 2026-06-23 | Value Factor 刷新（窗口至 2026-04-30） | 公告 |
| **2026-06-18** | **為顧問開放 Statistical neutralization** 於 JPS, HKG, TWN, IND, KOR, AMR, CHN | 公告 |
| 2026-06-14 | 推出 GLB High Turnover Theme（6/15–8/9，8 週，2X／符合 Investable/Liquid/Orthogonal 則 3X）；同日推出 GLB/D1 Dataset Power Pool June`26 | 公告 |
| 2026-06-12 | Combined Alpha Performance 刷新 | 公告 |
| 2026-06-05 | GLB HighTurnover Datasets Theme 延長至 6/14 | 公告 |
| 2026-06-01 | 為全體使用者新增超過 56,000 個新資料欄位 | 公告 |
| 2026-05-31 | 推出 USA/D1 Fast Datasets Power Pool June`26（6/1–6/14） | 公告 |
| 2026-05-28 | Q1 2026 季度給付金額上線。註明：Weight 值當時尚未更新到 leaderboard，但已正確計入 stipend 計算 | 公告 |
| 2026-05-24 | 推出 GLB HighTurnover Datasets Theme（5/25–6/7，2X） | 公告 |
| **2026-05-22** | **`ts_backfill` 與 `group_backfill` 不再計入**：Genius 的平均 operator 數、Genius 的 operator 總數、Power Pool Alpha 的 operator 數 | 公告 |
| 2026-05-13 | 為顧問開放 MEA region 的新 universe；開放新資料欄位 | 公告 |
| **2026-05-12** | 為顧問推出 **Python Alphas 模擬** | 公告 |
| 2026-05-10 | 推出 Orthogonal HTVR Theme（5/11–5/24，3X） | 公告 |
| 2026-05-08 | Value Factor 刷新（窗口至 2026-03-31）；Combined Alpha Performance 刷新 | 公告 |
| 2026-05-03 | 推出 MEA Region Theme（5/4–5/31，1.5X） | 公告 |
| 2026-04-30 | 為顧問開放 MEA region；推出 All regions/D0 Power Pool May`26（整月） | 公告 |
| **2026-04-29** | **Osmosis Points 可在 Submitted Alphas 頁直接內聯編輯** | 公告；Doc《Osmosis Allocation Guide》 |
| 2026-04-26 | 推出 After Cost HTVR Theme（4/27–5/10，3X） | 公告 |
| 2026-04-23 | 新增 universe：ASI TOP500、ASI MINVOL10M、USA TOP2000、GLB MINVOL10M | 公告 |
| 2026-04-15 | All regions/D1 Power Pool Apr`26 2 延長至 4/30 | 公告 |
| 2026-04-13 | Genius 等級刷新。確認：Q1 2026 提交決定 Q2 2026 等級 | 公告 |
| 2026-04-12 | 推出 Investable HTVR Theme（3X）；推出 All regions/D1 Power Pool Apr`26 2（獎金池 US$7,500） | 公告 |
| 2026-04-10 | Value Factor 刷新（窗口至 2026-02-28） | 公告（現存最早一則） |

### 5.2 文件之間互相矛盾的地方（請自行判斷現行版）

**（1）Base payment 到底有幾個 component**
- **4 個**：Quality Factor、Quantity Factor、Self Growth Factor、Value Factor
  ［FAQ《Will my base payment change, if I submit Theme Alphas?》｜2026-06-18］
  ［FAQ《If my base payment has not changed after submitting Theme Alphas?》｜2026-06-19］
- **5 個**：Quantity、Quality、Self growth、**Scaling of Quantity & Quality Factors**、Value
  ［FAQ《Actionable tips to analyze and attempt to improve your consultant payments》｜2026-08-09］
- 我的觀察（**非官方**）：第 5 項「Scaling」在其他文件裡是被描述成一種機制而非獨立因子。日期較新的是 5 個版本，但兩者並不真正衝突。

**（2）當期 Power Pool theme**
- **公告版（2026-08-17）**：簡化為單一 GLB Liquid theme，**到 9/30**，GLB + TOPDIV3000，**取消** High turnover returns ratio test
- **說明中心 FAQ 版《Current month Power Pool Themes》（2026-08-21 更新）**：仍以週為單位列表，8/24–8/30 那一列寫 `region=GLB & delay=1 & universe=TOPDIV3000 & datasets not in ['model110']`
- **平台實況（2026-08-23）**：Power Pool Leaderboard 的 Board Name 顯示「GLB/D1 Liquid Power Pool Aug`26」
- **差異點**：FAQ 表格多了「datasets not in ['model110']」這個限制，且沒有反映「到 9/30」的延長。**建議以公告 + leaderboard board name 為準，但提交前仍要看模擬結果的 theme match 訊息。**

**（3）Learn → Themes → Theme Calendar 頁面嚴重過期**
- 該頁最後一筆是 **JPN D1 Theme（公告 2024-06-23，期間 2024-06-24 ～ 2024-07-07，2x）**，完全沒有 2026 年的內容
- Advisory Theme Calendar 頁最後一筆是 **Advisory Theme - GLB Delay 1 Analyst datasets（2024-05-13 ～ 2024-06-02，2x）**
- ➜ **這兩頁已經沒有參考價值**，實際 theme 一律看站內公告與 support 網站的《Current month Power Pool Themes》

**（4）Power Pool 資格條件的三個版本**
- Doc《Clear these tests before submitting an Alpha》：列出 Sharpe≥1.0、operator≤8、datafield≤3、self-corr≤0.5、turnover 1–70%，並寫「**USA Delay 1**」
  → 這條「USA Delay 1」已經過時（現行 theme 是 GLB/D1）
- Doc《Power Pool Alphas》：多了 Turnover / Sub-universe / Robust-universe 測試需 PASS，以及 [PP+ATOM]、[PP+Regular] 的分類邏輯
- FAQ《What is required for Regular vs. Power Pool submission?》｜**2026-08-11**：最完整，明確列出哪些檢查是 FAIL、哪些只是 WARNING、哪些完全豁免
- ➜ **以 2026-08-11 的 FAQ 為現行版。**

**（5）Value Factor 更新頻率**
- 文件/FAQ 的舊文字：「Value factor refreshes **quarterly**… 到 **2023 年中**會改為 **monthly**」
  ［FAQ《What is the Value Factor?》｜2026-07-31；《How is the consultant leaderboard calculated?》｜2026-08-08 —— 兩篇都還留著這段「by mid 2023」的舊敘述］
- 公告顯示的**實際行為**：每月刷新一次，窗口是 3 個月，且有約 2 個月的落後
  - 2026-04-10 刷新 → 窗口至 2026-02-28
  - 2026-05-08 刷新 → 窗口至 2026-03-31
  - 2026-06-23 刷新 → 窗口至 2026-04-30
  - 2026-07-28 刷新 → 窗口至 2026-05-31
  - 2026-08-21 刷新 → 窗口至 2026-06-30
- 另一處寫「Value Factor 每 **4–6 週**更新」［FAQ《How do I get a higher quarterly payment within my Genius level's range?》｜2026-07-31］
- ➜ **實際觀察到的是每月一次**，但官方文字仍留著 2023 年的過渡期敘述沒清掉。

**（6）Genius 可用 region vs Osmosis scope 清單**
- Doc《💡 Brain Genius》：Gold 等級的 Research Regions = USA, ASI, EUR, GLB, IND, CHN, GBR, DEU（**沒有** KOR、TWN、MEA）
- 平台 Genius Status 頁（實查 2026-08-23，該帳號為 Gold）的 Osmosis Allocation 表列出 **16 個 scope**：USA/D1、USA/D0、GLB/D1、EUR/D1、EUR/D0、ASI/D1、CHN/D0、CHN/D1、**KOR/D1**、**TWN/D1**、IND/D1、**MEA/D1**、DEU/D1、DEU/D0、GBR/D1、GBR/D0
- ➜ 兩者不一致。Osmosis 文件說 scope 受 Genius 等級限制，但實際列出的 scope 比文件寫的 Gold region 清單多。

**（7）Streak tie-breaker 的名稱**
- FAQ / Doc：「**Maximum Simulation Streak**：該季結束或延續到下一季的最佳 streak；不會每季歸零」
- 公告《❄️Introducing Streak Freeze》｜2026-08-06：「保護你在 Genius tie-breaker（**Current Streak Length**）下的排名」
- ➜ 同一個 tie-breaker 的兩種叫法，定義（最佳 vs 當前）也不一樣。**未釐清**。

**（8）Osmosis 權重鎖定的時間差**
- Doc《Osmosis Allocation Guide》：配點於每週日 23:59 EST 鎖定，**7 天後**才用於每日 combo 計算（2/8 鎖定 → 2/16–2/22 使用）
- 同一份文件的 Daily Osmosis Rank 段落：使用「**兩個星期日前**」的配點
- Leaderboard 欄位提示：「updated daily using **Sunday's weights**」
- ➜ 三種說法。「7 天後」與「兩個星期日前」大致一致；欄位提示的「Sunday's weights」沒說是哪個星期日。

**（9）Quality Factor 的 universe 方向相反**
- User 版《WorldQuant Challenge》：「Universe（**smaller** universes get more score）」、「D1 貢獻比 D0 多」
- 顧問版 FAQ《Actionable tips...》｜2026-08-09：「Universes：**especially the larger ones**（e.g., Top 3000）」、「MDelay 的 D1 與 D0 **自 2023-01-09 起相等**」
- ➜ 這兩套是**不同制度**（User challenge vs 顧問 base payment），不是同一個 Quality Factor。顧問應以顧問版為準。

**（10）Power Pool Alpha 與每日 4 個配額的關係**
- FAQ《How do Power Pool Alphas affect compensation?》｜2026-03-16：「顧問 tag 為 power pool 的 alpha 會計入**每日 4 個 alpha 的配額**」
- FAQ《Which Alphas are eligible...》｜2026-08-13 與《Is there a limit...》｜2026-07-17：滿 3 個月後，pure PP 是**每日 1 個**、每月 10 個；[PP+ATOM]/[PP+Regular] **不計入**這兩個限制
- ➜ 兩者的關係（pure PP 的 1/日 是不是從 4/日 裡面扣）**未明確說明**。

### 5.3 文件內提及、但公告已看不到的歷史日期

- Delay factor（MDelay）：D1 與 D0 的值**自 2023-01-09 起相等**［FAQ《Actionable tips...》｜2026-08-09］
- Region factor（MRegion）：US、EUR、ASI 三區的值**自 2022-05-20 起相等**［同上］
- Weight factor 首次計入季度給付：**2023-01-01 起**。2022 Q2/Q3/Q4 三季，所有顧問的 Weight factor 視為相同［FAQ《How can you earn more Quarterly Payment?》｜2026-08-16］
- Genius 等級變更自 **2025-01-01** 起於每季第一週生效［Doc《💡 Brain Genius》］
- Genius 制度導入的過渡：Q4 2024 提交的 Alpha 仍用**舊的（無 Genius）**季度給付計算方式；Q1 2025 提交的才開始依 Genius 等級［FAQ《How do I get a higher quarterly payment within my Genius level's range?》｜2026-07-31］
- Genius Community Activity：2025-05-09 00:01 EST 之後的 webinar 與活動才計入 Genius Q2 2025 計分［FAQ《What is the Genius Community Activity criteria?》｜2026-08-22］
- Streak Freeze 上線日：**2026-08-06**［Doc《💡 Brain Genius》］

---

## 六、還沒搞懂 / 文件沒寫的

以下每一條都是**官方文件中找不到**的，不是我沒查到就是官方確實沒公開。我不用推測補。

### 6.1 每日基礎報酬
1. **完整公式未載明**。四（或五）個因子如何組合成一個金額 —— 相乘？加權相加？各因子權重是多少？完全沒有公開。
2. **各因子的取值範圍未載明**（除了 Value Factor 明載 0–1、Daily Osmosis Rank 乘數明載 1–2）。Quantity Factor、Quality Factor、Self Growth Factor 的範圍都沒寫。
3. **正規化的具體方式未載明**。只說「相對於其他顧問縮放」，沒說是百分位、z-score 還是別的。
4. **每日上限**：Starter Pack 寫「up to $120 per day」，但這個數字在 FAQ、Doc 的任何其他地方都**沒有再出現過**，也沒說是硬上限還是歷史最高值。無法確認是否為現行。
5. **Daily Osmosis Rank 如何換算成 1–2 的乘數未載明**。leaderboard 上顯示的值（0.00 / 0.21 / 0.67）與 1–2 的乘數之間關係不明。
6. **MDelay 與 MRegion 的實際數值未載明**。只知道「哪些之間相等」，不知道絕對值，也不知道未列出的 region（GLB、CHN、IND、MEA、DEU、GBR、JPN、KOR、TWN、HKG、AMR）的值。
7. **Self Growth Factor 的計算窗口與量化方式未載明**。「比之前更好」的「之前」是幾天/幾週？用哪個指標？沒寫。
8. **Quality Factor 的實際公式未載明**。只知道哪些東西會影響它。

### 6.2 季度報酬與 Genius
9. **Quarterly payment 的公式未載明**。只知道 weight 與 value factor「影響力相近」，不知道如何組合，也不知道如何映射到金額。
10. **各 Genius 等級的季度給付區間上下限未載明**。只有 Master「$2,000 以上」與 Grand Master「$8,000 以上」兩個量級敘述；Gold 與 Expert 的區間完全沒寫。
11. **Weight 的計算方式未載明**。只知道是「每日 Net Weight 加總的季平均」，但「Net Weight」本身怎麼產生（WQ 如何決定給你的 Alpha 多少權重）沒有任何說明。
12. **2026-Q3 的 Combined Alpha Performance 門檻未顯示**。Genius Status 頁上 Signals 與 Pyramids 有明確數字（20/120/220、10/30/60），但三個 Combined 指標欄位顯示「Pending computation」，沒有門檻數值。這一項到底要多少才過 Expert/Master/GM，目前查不到。
13. **Value Factor 的公式未載明**。只知道考慮個別表現、多樣性、獨特性三個面向。
14. **Tie-breaker 的排名加總方式未載明**。是等權相加？各項有權重嗎？Community leader 那一項（Forum + Referral）如何與前五項合併？沒寫。
15. **Genius 名額比例的分母定義有點模糊**：「utilized at least 4% of their quarterly limit of 4 Alphas and 1 SuperAlpha per day」—— 4% 是怎麼算的（天數？總數？）沒有進一步說明。

### 6.3 Power Pool
16. **Power Pool theme 的排程表未公開**。只能看到當期，未來的 theme 沒有預告日曆。Learn 裡的 Theme Calendar 頁已停留在 2024 年。
17. **「High Turnover returns ratio test」的定義未載明於顧問文件**。只在 theme 條件裡被引用（>0.75），但這個 ratio 怎麼算、在哪看，Doc《Getting Started with High Turnover Alphas》裡沒有明確定義段落。
18. **PPAC（Power Pool Alpha Correlation）與 Power Pool Correlation 是否為同一個東西未載明**。文件只用「Power Pool Correlation < 0.5」與「self-correlation of just your Power Pool alphas < 0.5」兩種說法。
19. **Merged PNL 欄位的定義未載明**（Power Pool Leaderboard 上有這欄，但沒有欄位提示）。
20. **Pure PP 的 1/日配額與 Regular 的 4/日配額之間的關係未明確**（見 5.2 第 10 點）。

### 6.4 其他
21. **ACTIVE → DECOMMISSIONED 的判定標準未載明**。只說「資料集下架，或 OS 期間長期表現不佳，或 WQ 自行決定」——「長期表現不佳」沒有量化。
22. **2026-04 之前的規則變更公告已無法從平台取得**。站內只保留 133 則、最早到 2026-04-10。如果你需要更早的變更紀錄（例如 Genius 制度剛上線時、Power Pool 從月賽改成 theme 賽的那次公告），平台上已經看不到了。
23. **Power Pool 從「每月按 region」改成「按 theme」的確切生效日未載明**。FAQ 說「the new format that replaces the old monthly, region-based Power Pool Monthly competition」，但沒有給日期，而現存最早的公告（2026-04-12）已經是 theme 制。
24. **顧問合約 Annex A 的內容我沒有查看**。多篇 FAQ 都指向「your consulting agreement 的 annex」才有具體金額規則 —— 那份文件在 Profile → Agreements，屬於你的個人合約，我沒有打開。**如果你要確認確切的金額計算，那裡才是權威來源。**

---

## 附錄：本次掃描的完整文件清單

**Learn → Documentation（84 篇，全部檢視過）**

| 分區 | 篇數 | 與報酬機制相關的篇章 |
|---|---|---|
| Discover BRAIN | 8 | Starter Pack（$120/日、$25,000/季）、WorldQuant Challenge（User 版計分） |
| Create Alphas | 4 | 無 |
| Examples | 3 | 無 |
| Interpret Results | 3 | **Clear these tests before submitting an Alpha**、**Parameters in the Simulation results**（ACTIVE/DECOMMISSIONED、Fitness 公式） |
| Understanding Data | 11 | Understanding Data in BRAIN（Dataset Value Score） |
| Advanced Topics | 11 | Investability Constrained Metrics（提及 value factor / weight factor） |
| Regions and Universes | 18 | 無（其中 4 篇為權限鎖定頁，只顯示等級要求） |
| **Consultant Information** | **14** | **Starting Guide、Power Pool Alphas、Consultant Submission Tests、Osmosis Allocation Guide、Single Dataset Alphas、💡 Brain Genius**（其餘 8 篇無報酬內容） |
| BRAIN API | 6 | Understanding simulation limits（模擬額度，非報酬） |
| SuperAlpha | 2 | 無 |
| **Themes** | **4** | **Overview of Themes、Multiplier Rules**（Theme Calendar 與 Advisory Theme Calendar 已過期至 2024） |

**support.worldquantbrain.com 說明中心（掃描 7 個分類共 347 篇，其中 91 篇標題含報酬關鍵字，細讀 40 篇）**
General FAQs／Research FAQs／BRAIN Genius／Getting Started for Consultants／Osmosis Competition／Power Pool Alphas Thematic Competition／Glossary

**站內訊息**：Announcements 133 則全掃（2026-04-10 ～ 2026-08-21）、Notifications 28 則全掃

**平台頁面實查**：Consultant Leaderboard（含 3 個欄位提示）、Genius Status、Genius Leaderboard、Power Pool Leaderboard（含 2 個欄位提示）、Profile → Performance

---

*本文件只做「讀取」，未對帳號做任何修改（未點擊 Mark as read、未更動任何設定或配點）。*
