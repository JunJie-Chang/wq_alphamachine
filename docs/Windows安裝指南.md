# Windows 安裝指南(新手版)

這份文件教你在一台**什麼都沒裝**的 Windows 電腦上,把 alphamachine 跑起來。
照順序複製貼上指令即可。每一步都有「怎麼確認成功」。

> **怎麼讀這份文件:** 你現在看到的是 GitHub 網頁,不需要下載任何東西。
> 灰色框框裡的指令,**滑鼠移到框框右上角會出現「複製」按鈕**,按下去,
> 再到 PowerShell 視窗按右鍵貼上(或 Ctrl+V),按 Enter 執行。
> 一次做一步,做完確認成功再往下。

> 你需要先有:**WorldQuant BRAIN 的帳號**(email + 密碼)。沒有帳號,程式只能跑
> 離線測試,不能連平台。

---

## 步驟 0 — 打開 PowerShell

按 `Win` 鍵 → 輸入 `PowerShell` → 按 Enter。看到藍色(或黑色)視窗就對了。
以下所有指令都貼在這個視窗裡執行。

## 步驟 1 — 安裝 Git 和 Python

Windows 10/11 內建 `winget`,一行一行貼:

```powershell
winget install --id Git.Git -e
winget install --id Python.Python.3.12 -e
```

**裝完之後,把 PowerShell 視窗整個關掉,再重新開一個**(不然系統找不到剛裝的程式)。

確認成功:

```powershell
git --version
python --version
```

應該看到 `git version 2.x` 和 `Python 3.12.x`(需要 3.10 以上)。

> 如果 `python --version` 跳出 Microsoft Store,或顯示找不到:
> 到 https://www.python.org/downloads/ 手動下載安裝,
> **安裝畫面第一頁一定要勾 `Add python.exe to PATH`**,然後重開 PowerShell。
>
> 如果 `winget` 本身找不到,就兩個都去官網手動裝:
> Git → https://git-scm.com/download/win(一路 Next 即可)

## 步驟 2 — 下載專案

```powershell
cd $HOME\Documents
git clone https://github.com/JunJie-Chang/wq_alphamachine.git
cd wq_alphamachine
```

確認成功:`dir` 應該看得到 `alphamachine`、`scripts`、`docs`、`README.md`。

> 如果是私有 repo,`git clone` 會跳出登入視窗,用被邀請的 GitHub 帳號登入即可。

## 步驟 3 — 建立虛擬環境並安裝套件

虛擬環境 = 這個專案專用的 Python 套件資料夾,不會弄亂你電腦其他東西。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

成功的話,PowerShell 每一行最前面會多出 `(.venv)`。

> **如果 Activate 出現紅字「無法載入檔案,因為這個系統上已停用指令碼執行」:**
>
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```
> 按 `Y` 確認,再重跑 `.venv\Scripts\Activate.ps1`。
>
> **以後每次開新的 PowerShell,都要先 `cd` 到專案資料夾,再跑一次
> `.venv\Scripts\Activate.ps1`**,才能用 `python -m alphamachine`。

## 步驟 4 — 填入帳號密碼

```powershell
copy .env.example .env
notepad .env
```

記事本打開後,改成你自己的:

```
WQ_EMAIL=你的BRAIN登入email
WQ_PASSWORD=你的BRAIN密碼
```

- 等號兩邊**不要有空格、不要加引號**。
- 存檔(Ctrl+S)後關掉。
- `.env` 已被 git 忽略,不會被上傳。**絕對不要把它傳給別人或貼到聊天室。**
- 如果存檔時記事本把檔名變成 `.env.txt`,用「另存新檔 → 存檔類型選『所有檔案』」
  存成 `.env`。

## 步驟 5 — 離線自我檢查(不連網、不耗額度)

```powershell
python scripts\smoke_test.py
```

最後一行出現 `ALL CHECKS PASSED` 就表示環境沒問題。

## 步驟 6 — 第一次連線

```powershell
python -m alphamachine catalog
```

這會登入 BRAIN 並下載資料欄位清單(檔案大、要等幾分鐘,正常)。

**第一次登入可能需要生物辨識/身分驗證(Persona)**:程式會印出一個網址,
用手機或有鏡頭的電腦打開,跟著畫面完成驗證,程式會自己接著往下跑。
**驗證有每日次數限制,失敗就先停手,不要連續重試。**

之後的流程(掃描、精煉、提交)請讀 `README.md` 的 Part 4 與
`docs/新手指南.md`。**所有指令把 `python3` 換成 `python` 即可。**

```powershell
python -m alphamachine --concurrency 4 scan --budget 50
python -m alphamachine gatelog --passing
python -m alphamachine submit --n 3          # 乾跑,不會真的送出
```

> ⚠️ `submit ... --live` 才會**真的提交**到 WorldQuant,而且不能撤回。
> 新手請先全部用乾跑確認結果,再跟學長確認後才加 `--live`。

---

## Windows 的已知差異

| 項目 | 說明 |
|---|---|
| `python3` | Windows 上請一律用 `python`。 |
| `cp` / `nohup` / `&` | Windows 沒有。複製用 `copy`;長時間執行就開一個 PowerShell 視窗讓它跑,不要關。 |
| `scripts\*.sh`(status.sh、progress.sh…) | 是 bash 腳本,PowerShell 不能直接執行。要用的話在 **Git Bash**(裝 Git 時一起裝好)裡跑:`bash scripts/status.sh`。 |
| `scripts\autoloop.py` | 內部寫死呼叫 `python3`,在 Windows 上會失敗。**先不要用**,等修正。 |
| 路徑 | 指令裡用 `\`(例如 `scripts\smoke_test.py`),`/` 通常也能用。 |

## 常見錯誤

| 訊息 | 原因 / 解法 |
|---|---|
| `'git' is not recognized` / `'python' is not recognized` | 裝完沒有重開 PowerShell,或 Python 沒勾 Add to PATH。重開視窗;不行就重裝並勾選。 |
| `No module named alphamachine` | 你不在專案資料夾。先 `cd $HOME\Documents\wq_alphamachine`。 |
| `No module named requests` / `numpy` / `yaml` | 沒啟用虛擬環境。跑 `.venv\Scripts\Activate.ps1`,再 `pip install -r requirements.txt`。 |
| 登入一直失敗 | `.env` 密碼打錯,或在網站上改過密碼。**修正 `.env`,不要重複重試**,重試會被平台鎖。 |
| 想更新到最新版 | `git pull`,然後 `pip install -r requirements.txt`。 |

## 一頁速查:每次開工

```powershell
cd $HOME\Documents\wq_alphamachine
.venv\Scripts\Activate.ps1
git pull
python -m alphamachine report
```
