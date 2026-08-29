# Power Pool Alpha 規則說明

> 來源:WorldQuant BRAIN 官方文件 *Power Pool Alphas*

Power Pool Alpha 是 WorldQuant BRAIN 平台上的一類**較簡單、規模較小但品質較高的 Alpha 策略**,設計目的是幫助研究顧問(consultant)在 Genius 評分、競賽和主題活動中取得好成績。它與「Regular Alpha」「ATOM Alpha」為不同分類,同一個 Alpha 可能同時符合多種分類。

## 1. 入選資格標準

一個 Alpha 要被認定為 Power Pool Alpha,必須同時滿足以下條件:

- **Sharpe ratio ≥ 1.0**
- 使用的**獨特運算子(operator)數量 ≤ 8**(含重複使用的運算子;`ts_backfill` 與 `group_backfill` 不計入)
- 使用的**獨特資料欄位數量 ≤ 3**(不含分組欄位,如 country、industry、subindustry、currency、market、sector、exchange)
- **Power Pool 相關性 < 0.5**
- 通過**換手率測試(Turnover test)**
- 通過**子樣本測試(Sub-universe test)**
- 通過**穩健樣本測試(Robust-universe test,適用情況下)**

> **備註**:若該 Alpha 與其他 Power Pool Alpha 的自相關性(Self-Correlation)超過 0.5,則其 Sharpe 必須比相關性最高的那個 Alpha 高出至少 10%,才符合提交資格。

## 2. 分類組合邏輯

| 組合 | 條件 |
|---|---|
| Power Pool + Regular | 符合 Power Pool 標準,且同時通過所有其他常規 Alpha 的表現測試 |
| Power Pool + ATOM | 只使用單一資料集(dataset)的資料,且除 IS Ladder 測試外通過其他所有測試 |
| Power Pool + ATOM + Regular | 同時滿足以上兩種組合的全部條件 |

## 3. 主題競賽(Power Pool Thematic Competition)

- 若要提交**未通過常規 Alpha 測試的「純 Power Pool Alpha」**,該 Alpha 必須符合當前開放的 Power Pool 主題(Theme)。
- 要參加某個 Power Pool 主題排行榜競賽,該主題下至少需要標記 **5 個**符合條件的 Power Pool Alpha。

## 4. 提交配額限制(首次提交滿 3 個月後生效)

- 每位顧問每個**日曆月**最多可提交 **10 個純 Power Pool Alpha**,限單一範圍(scope)內:
  - USA D0 / D1
  - EUR D0 / D1
  - ASI D1
  - GLB D1
  - OTHER(JPN、CHN、HKG、TWN、KOR、AMR、IND)D0 / D1
- 每天最多提交 **1 個純 Power Pool Alpha**
- 被歸類為 [Power Pool + ATOM]、[Power Pool + Regular] 或 [Power Pool + ATOM + Regular] 的 Alpha **不計入**上述兩項限制
- 「純 Power Pool Alpha」是指未達到 ATOM 或 Regular Alpha 提交標準的 Alpha,且必須符合當前 Power Pool 主題

**每日提交範例**:
- 1 個純 Power Pool Alpha(符合當前主題)
- 1 個 [Power Pool + ATOM] Alpha
- 合計:2 個 Power Pool 相關提交

> 尚未滿 3 個月的顧問,適用標準提交限制:每天最多 4 個 Alpha 提交。

## 5. 描述規範(Description)

提交 Power Pool Alpha 時,必須在模擬結果底部 **PROPERTIES** 區域,以「Idea + Rationale」模板撰寫至少 **100 字元**的描述,否則不符合 Power Pool 資格。

**範例描述:**

> **Idea**:在正常市場條件下,若一股票被大量放空,其反彈(reversion)機率可能提升。但在極端情況下,若市場放空共識極高(反映為極端高或低的放空利息水準),跟隨該趨勢可能反而更有利。
>
> **Rationale for data used**:`shrt3_bar` 是向量型資料欄位,代表借券需求,數值越高代表需求越高。
>
> **Rationale for operators used**:
> - `vec_avg()`:計算 `shrt3_bar` 當日平均值
> - Conditional operator:區分正常情況與極端情況
> - `ts_backfill`:處理缺失值(NaN),透過視覺化工具檢查資料覆蓋率後判斷

## 6. Power Pool 標籤管理

- 在 Alphas 頁面的 **Submitted** 分頁可查看已提交的 Power Pool Alpha 清單。
- **移除標籤**:進入 Submitted 分頁,開啟該 Alpha 說明,在 Properties 區域點擊「PowerPoolSelected」標籤旁的叉號即可移除。
  - 注意:移除標籤後,該 Alpha 仍會留在自相關性池(self-correlation pool)中,新提交的 Power Pool Alpha 仍會與它比對相關性。
- **重新標記**:在 Properties 區域的 Tags 下拉選單中,重新選擇「PowerPoolSelected」即可。

## 7. 建立 Power Pool Alpha 的技巧

- 標記前先檢視 **Merged Performance**
- 探索**低換手率**且流動性較小(small)的 universe,有助於提升扣費後(After Cost)表現
- 檢視現有 Alpha 池,找出符合資格的訊號
- 善用新推出的 **Investability Constrained PNL** 工具
- 讓整個 Power Pool 組合保持**多樣性**:涵蓋不同資料集、想法、運算子、universe,甚至一定程度上不同的換手率,以確保樣本外(OS)表現穩健
- 建立一定規模的池子後,移除與池內其他 Alpha **高度相關**的策略,可能提升組合表現並降低 Alpha 數量懲罰
- 如有需要,可向研究顧問(research advisor)諮詢針對性建議
</content>