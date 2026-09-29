# W3 Sprint 個人增量報告

## 1. 基本資訊
- 週次：W3
- 組別：solo
- 成員：copilot
- Git 版本：`5668999c303fe434c68d6e0430d38054cb396c9c`
- AWS 帳號末四碼：`5912`
- AWS 區域：`us-east-1`
- Codespace 出口 IP：`207.46.224.87/32`
- 相關資源腳本： [deploy/up.sh](../../deploy/up.sh), [deploy/down.sh](../../deploy/down.sh)

## 2. 計畫與修正（含審查者）
### 計畫
T1 的部署計畫為：
1. 確認 Learner Lab 帳號、區域與 default VPC / subnet / route table。
2. 確認 Codespace 對外 IPv4，轉為 `/32`，作為來源 CIDR。
3. 使用 AL2023 x86_64 AMI、t3.micro、預設 public subnet、專用 SG、匯入 key pair。
4. 以同一個 Git commit 建立 user data，避免未提交修改混入。
5. 先讓服務正常運作，再做 T3 故障驗證，最後 T4 清理與保留。

### 審查者
本次為單人實作，審查者即為我本人依照 Lab 契約逐項比對；未在任何 AWS create 操作前繞過核對。所有執行前都先確認：帳號、區域、路由、SG、AMI、IP /32 與回收方式。

### 修正
實際執行過程中，T4 的回收清單第一次指向已終止的第一台，因此已重新確認 W3 標記實例並更新保留清單，最後保留的是第二台 `i-07feea5f7fc4fbcb6`。這一修正是基於 AWS 目前實際狀態，而不是依名稱批量刪除。

## 3. 五層時間與 early curl
這週實際有記錄到的關鍵時間／結果如下：

| 層次 | 實際時間 / 結果 | 證據 |
|---|---|---|
| EC2 啟動 | `2026-09-28T17:31:47+00:00` | `LaunchTime` from AWS describe-instances |
| early curl（初次健康檢查） | 在 instance running 後立即發出，前 5–6 次失敗，隨後回應 200 | `Health probe failed before service readiness (attempt 1/30)...` `Health OK...` |
| service ready | `2026-09-28T17:32:36Z` | `/health` JSON `started_at` |
| nginx / inspection 正常 | `HTTP/1.1 200 OK` | curl 回應 `Server: nginx` 並回傳 JSON |
| 最終版本確認 | commit SHA 等於 `5668999c303fe434c68d6e0430d38054cb396c9c` | `/health` JSON `version` 同 SHA |

> 這些是本次已確認的實際證據；cloud-init 的精確時間沒有在這次工作中單獨記錄到，所以沒有偽造為「精確到秒」的 T1–T5 時序表。

## 4. 故障表： (a) SG 擋住 / (b) 後端停止
| 故障 | 預測 | 實際 | 原因 | 恢復後結果 |
|---|---|---|---|---|
| (a) 移除 SG 的 80 規則 | 連線應 timeout，因為來源 IP 不再被放行 | `curl: (28) Connection timed out after 8002 milliseconds`，無 HTTP 回應 | SG 已過濾 TCP 80 | 重新加入相同的 `207.46.224.87/32` 80 規則後，`curl_exit_after_restore=0`，返回 `HTTP/1.1 200 OK` |
| (b) 停止 inspection 服務 | nginx 仍在，但後端會失效，請求會回 502 | `HTTP/1.1 502 Bad Gateway` 且 `Server: nginx` | nginx 存在，但 upstream inspection service 已停 | 重新啟動 `inspection.service` 後，回復 `HTTP/1.1 200 OK` |

## 5. 回收讀回
### 第 1 台：已回收
- 實例：`i-0d17d22b0e63f9acd`
- 狀態：`terminated`
- 相關清單：
  - SG: `sg-03e6a7ca428a69305`
  - key pair: `w03-solo-copilot-key`
  - root volume: `vol-09409b8632c969d9c`
  - ENI: `eni-053b11f856ac38e92`

### 第 2 台：保留中
- 實例：`i-07feea5f7fc4fbcb6`
- 狀態：`stopped`
- 相關清單：
  - SG: `sg-0c1fb79bc03db1298`
  - key pair: `w03-solo-copilot-key`
  - root volume: `vol-0d0e93f4d50cf0d22`
  - ENI: `eni-0f2c18f6ca2079ccf`

## 6. 保留清單（保留哪一台、為什麼、下週誰處理）
- 保留哪一台：`i-07feea5f7fc4fbcb6`
- 為什麼：這是第二輪重建後的實例，且已經停機保留，符合 W3 要求的「保留一台已停止的主機」。
- 下週誰處理：本人（copilot）處理 W4 啟動與重新驗證；提醒下週 public IP 會改變，需重新核對 SG /32。

## 7. 請求旅程（用自己的話）
一個請求的流程大致是：
- 我的 Codespace 先從外部發出 HTTP 請求，源 IP 是 `207.46.224.87/32`。
- 這個 IP 透過 IGW 與 route table 進入 default public subnet。
- SG 只允許 TCP 22 和 80，且來源是我的 `/32`。
- 進到 EC2 後，nginx 監聽 port 80，將請求轉發到 `127.0.0.1:8080`。
- inspection service 會回傳 JSON，包括 `status`, `service`, `version`, `started_at`。
- 如果 SG 被移除或 inspection stop 掉，請求就會在不同層中斷，從而出現 timeout 或 502。

## 8. 報告範圍與交付狀態
- 已依照 Lab 要求整理報告，且確實保留在 repo 中： [reports/W3/W3-sprint-report.md](W3-sprint-report.md)
- 已實作完整流程的腳本： [deploy/up.sh](../../deploy/up.sh) 及 [deploy/down.sh](../../deploy/down.sh)
- 未提交密碼、完整帳號 ID、私鑰、簽名網址
- 未把未實際測試的項目偽裝為成功

## 9. 成本與後續
- 本週費用以 Learner Lab 的 EC2 / EBS / 公有 IPv4 計價為準；保留主機已停機為 `stopped`，以符合 W3 保留要求。
- 下週重啟前，需重新核對公網位址與 SG 規則，因為啟動後 public IP 可能改變。
