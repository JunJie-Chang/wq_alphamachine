# Osmosis 分配規則說明

> 來源:WorldQuant BRAIN 官方文件 *Osmosis Allocation Guide for Consultants*

## 1. 什麼是 Osmosis?

Osmosis 是你在不同**範圍(scope,即 region × delay 組合)**中,依照分配的點數建立的**個人 Alpha 組合**。平台每天計算此組合的 PNL,並將表現用於:

- 每日基本報酬的**主題乘數(theme multiplier)**
- Genius 季度報酬評估依據的 **Combined Osmosis Performance**
- 顧問排行榜上的 **Daily Osmosis Rank**

## 2. 如何分配點數

有三種操作方式:

1. **Properties 區域**:在 Alphas 頁面的 Properties 區域,為個別 Alpha 分配 Osmosis 點數
2. **Submitted Alphas 表格直接編輯**:將滑鼠移到 Osmosis Points 欄位,出現游標與編輯圖示後即可就地修改
   - ✅ 綠色勾勾:更新成功(但不代表分配數量或範圍配置正確)
   - ❗ 紅色警示:更新失敗,並附上失敗原因提示
   - 將滑鼠移到最左欄的綠色預覽圖示可查看 Alpha 表達式
3. **Osmosis Scale 一鍵縮放**:
   - 位於 Alphas 頁面 Submitted 分頁,Filter 按鈕旁
   - 僅在該範圍已分配給至少 **10 個 Alpha**,且尚未達到 100,000 點時可點擊
   - 點擊後會列出所有符合條件的範圍,顯示目前分配狀況與 Alpha 數量
   - 點選「Scale All to 100,000」即可一次縮放所有列出的範圍

## 3. 分配硬性要求(Allocation Requirements)

- 必須在**至少 3 個範圍**(依你的 Genius 等級允許的 region × delay 組合)中分配點數
- 每個範圍內,總分配點數必須**恰好等於 100,000 點**
- 每個範圍內,點數必須分配給**至少 10 個 Alpha**
- 可分配對象:
  - 所有類型的 Alpha,包括 Regular、Atom、Power Pool、SuperAlpha 等
  - 任何提交日期的 Alpha,不限最近提交
- 單個 Alpha 沒有最低點數限制,但每個範圍的總和必須等於 100,000

### 點數鎖定機制

- 點數**每週鎖定一次**:每週日 23:59(美東時間 EST)當下的分配結果,會用於**之後 7 天**的每日組合計算
- 範例:2026 年 2 月 8 日(週日)鎖定的點數 → 用於 2 月 16 日至 22 日的每日組合計算
- 可以隨時修改分配,**沒有懲罰**,但只有週日晚上鎖定當下的數值才會生效

## 4. 表現查詢入口

| 位置 | 說明 |
|---|---|
| Genius Status Page → Osmosis Allocation | 查看目前分配狀況 |
| Genius Status Page → Combined Osmosis Performance | 顯示整個 Genius 季度內,每週 Osmosis 組合串接後的長期扣費後 Sharpe |
| Consultant Leaderboard → Daily Osmosis Rank | 用兩週前週日鎖定的點數計算,若無有效分配則留空 |

### Daily Osmosis Rank(影響每日基本報酬)

1. 針對每個範圍,用**兩週前週日**鎖定的 Osmosis 點數分配,計算「某一天」(樣本外兩年前的資料)的 PNL
2. 將每個範圍的 PNL 在所有顧問間排名,得到該範圍的排名
3. 計算你所有範圍排名的**平均值**,即為 Daily Osmosis Rank
4. 此排名作為每日基本報酬的**品質係數乘數**,數值介於 **1 到 2 倍**之間,每天更新
5. 平均值只計入你**有分配點數**的範圍(不是所有可能範圍)
6. 表現不會因範圍數量而線性提升,但多元化有助於降低回撤,跨區域分散有助於投組穩健性

### Combined Osmosis Performance(影響 Genius 季度報酬)

- 每週日的 Osmosis 分配會產生扣費後 PNL,所有週日的扣費後 PNL 串接起來,計算出當季的扣費後 **Sharpe**,即為 Combined Osmosis Performance
- 每 **4–6 週**更新一次
- 需要在過去 3 個月內,至少有 **6 週**的正確分配,才有資格獲得此指標
- Genius 資格審核採用以下四者的**最大值**:
  - Combined Alpha Performance
  - Combined Selected Alphas Performance
  - Combined Power Pool Alpha Performance
  - Combined Osmosis Performance

## 5. 為什麼表現會逐月波動?

即使**完全沒有更動**Osmosis 分配,Combined Osmosis Performance 仍可能出現顯著變化,原因在於:

- 該指標採用**短期滾動 3 個月**視窗評估,樣本窗口短,天然容易產生高離散度的結果
- 其他 Combined Alpha Performance 類指標採用**較長的滾動期**評估,因此波動模式不同

**因應建議**:

- 月度間出現波動屬正常現象,尤其在短評估窗口下更明顯
- 應專注於**持續、穩定、高品質**的分配,而非對短期表現波動做出反應
- 觀察整個季度的表現趨勢,才能更準確評估組合的實際效果

## 6. 建立優質 Osmosis 組合的實用技巧

- **從最佳 Alpha 開始**:優先選擇具**獨特想法**、有強經濟邏輯的 Alpha,不要把點數浪費在缺乏經濟意義、隨機拼湊表達式的策略上
- **跨範圍多元化**:盡量超過最低 3 個範圍;混合主要範圍(如 USA、GLB)與你表現強勁的其他範圍;優先配置在機會說明會(opportunity webinar)中被提及的重點區域;隨時檢查 Osmosis Allocation 表中還剩多少點數待分配
- **跨資料集多元化**:避免集中在特定資料集或類別,涵蓋多個資料集與類別以降低同時大幅回撤的風險
- **奧卡姆剃刀原則**:若多個提交使用相同資料集與相同想法,優先保留邏輯最簡單、經濟意義最強的那一個
- **跨運算子與中性化方式多元化**:每種運算子或中性化方法都有其特性,過度集中會導致所有 Alpha 同步回撤,拖累 Combined Osmosis Performance
- **控制波動性**:過度集中的組合容易產生大幅波動,應追求穩健正向的回報與合理風險水準
- **避免頻繁劇烈調整**:採用漸進式調整,以「週」而非「天」為單位評估效果,才能追蹤每次調整的實際影響
- 使用 **SuperAlpha selection table** 分析大量 Alpha 池,協助你在資料集、資料集類別、換手率區間與 Alpha 想法上做出多元分散
- 為重要的 Alpha 加上**自訂標籤**,方便日後快速篩選
- **排除**經 investability constraint 調整後 Sharpe 下降約 **60% 以上**的 Alpha,除非:
  - 該 Alpha 在你的選集中具有獨特性,或
  - 該 Alpha 基於不同的資料集
- **按換手率分桶**管理:<10%、10–20%、>20% 三個區間;換手率超過 20% 的 Alpha,除非其 investability-constrained Sharpe 依然穩健,否則應避免納入
- 對於先前未開啟 MaxTradeOn 提交的 Alpha,重新模擬並開啟 MaxTradeOn;若表現依然令人滿意才保留
- 使用 Alpha 清單檢視組內**相關性**,剔除彼此高度相關的 Alpha
- 檢視個別 Alpha 的 PnL 曲線,移除與其他 Alpha **同步回撤**的策略
- 對篩選後的候選集套用 SuperAlpha,評估在納入 investability constraint 與 MaxTradeOn 後的**組合 Sharpe**
- 可以考慮納入 Power Pool 或 Atom 分類出現**之前**開發的高品質舊有(legacy)Alpha,Osmosis 選集不限於新型 Alpha 類型
- 整體而言,納入**更多高品質 Alpha**通常有助於提升 Osmosis 組合表現
</content>