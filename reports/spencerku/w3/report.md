# W3 個人增量報告

- 週次／組別／組內代號／Git 版本：W3／第二組／spencerku／`b97830496311cf3058b10cba039ac3620b9ece8b`
- 審查者與核准時間：組員審查／簽核依本次個人交付決定不納入。
- AWS 區域／帳號末四碼／成本預估：`us-east-1`／6293／依當期 EC2、EBS、公有 IPv4 計價
- 核對的 VPC／公有子網／AZ／IGW 路由／AMI／來源 `/32`：`vpc-02a8c89fb0f1218bd`／`subnet-05a4d007d05263e33`／`us-east-1a`／`rtb-0915479ef9389a480` 的 `0.0.0.0/0 -> igw-08f1866d81eb0b8d6`／`ami-0b2c9d1f3edcfd709`／`223.138.247.174/32`

## 部署計畫與資源

- 預計建立：1 個 SG、1 個匯入 key pair、1 台 `t3.micro`。
- 第一輪資源：instance `i-006c04a8cbe12ac8a`、SG `sg-0c7c2dfec9c4619db`、key pair `w03-second-group-spencerku`。
- 網路暴露：只允許來源 `/32` 的 TCP 22、80；inspection 只聽 `127.0.0.1:8080`。
- 磁碟／主機設定：加密 gp3、DeleteOnTermination、IMDSv2 required。
- 計畫修正與審查意見：

## T2 五層觀測

| 層級 | UTC 第一次觀測時間 | 實際結果／證據 |
|---|---|---|
| instance running | 時間未記錄 | instance running；後續服務驗證成功 |
| status checks 2/2 | 時間未記錄 | `instance-status-ok` waiter 通過 |
| cloud-init 完成 | 2026-09-22T02:30:20Z | `status: done` |
| nginx 80／inspection 8080 listening | 2026-09-22T02:30:20Z | nginx `0.0.0.0:80`、inspection `127.0.0.1:8080`，兩者 active |
| `/health` 200 且版本相符 | 2026-09-22T02:18:25Z | HTTP 200；version 為部署 SHA |

Early curl：部署中斷前未測。後續健康檢查為 exit `0`、HTTP `200`、耗時 `0.811077s`，回應 `status=ok`、`service=inspection`、version 為部署 SHA。

重啟保留主機的補充觀測（不是原始 T2 首次觀測）：2026-09-22T02:59:04Z 以新公有 IP `34.237.243.33` 連線；SSH 主機指紋為 `SHA256:RPHlWjZNxasBwvahheaSnB6K3dZC3qk7+OScrAtptxo`。原始 running、2/2 與 early curl 的首次時間已無法從當時中斷的流程回溯。

## T3 故障測試

| 故障 | 預測 | 實際 | 原因／恢復結果 |
|---|---|---|---|
| 移除 SG 的 80 規則 | 連線逾時、HTTP 000 | exit `28`、HTTP `000`、`8.002652s` | SG 擋在 HTTP 連線前；恢復後 exit `0`、HTTP `200`、`0.556675s` |
| 停止 inspection service | nginx 回 502 | exit `0`、HTTP `502`、`0.484824s` | nginx active、inspection inactive；啟動後 exit `0`、HTTP `200`、`0.596091s` |
| 停止 nginx（組內示範） | inspection 仍正常，但 80 無 listener，預期連線拒絕 | 2026-09-22T02:59:04Z；exit `7`、HTTP `000`、`0.247500s`；inspection active、nginx inactive | nginx 停在入口層；啟動後 nginx／inspection active，exit `0`、HTTP `200`、`1.201933s` |

每次 curl 都記錄 exit code 與 HTTP status；HTTP `000` 代表沒有收到 HTTP 回應。

## T4 回收、重建與保留

- 第一輪五項讀回不存在的證據與 UTC 時間：instance `i-006c04a8cbe12ac8a` terminated；EBS／ENI `NotFound`；SG `sg-0c7c2dfec9c4619db` 與 key pair 已由腳本刪除並完成讀回。時間未記錄。
- 第二輪 instance／SG／key pair／EBS／ENI ID：`i-0870ca48f9cf0b117`／`sg-066aecfefbeeaa449`／`w03-second-group-spencerku`／`vol-0afe8a55750eec4ff`／`eni-05773a0f513db233c`。
- 第二輪重建耗時：70 秒，從執行 `up.sh` 到 `/health` 200 且版本驗證成功。
- 保留主機狀態：`stopped`；下週處理人：spencerku／第二組。
- 精確資源與回收方法：第一輪依 resources.json 的 instance ID terminate、SG ID delete、key name delete；第二輪以 `down.sh --stop` 停止，保留 SG、key pair、EBS 與 ENI。

## 請求旅程與反思

Codespace 的 `223.138.247.174/32` 先經 IGW 與預設路由到 EC2，SG 只允許 TCP 80／22，nginx 收到 80 後轉送至主機內的 `127.0.0.1:8080` inspection。移除 SG 的 80 規則時請求停在防火牆，沒有 HTTP 回應；inspection 停止時請求仍到達 nginx，因此 nginx 回 `502`。

## AI 協助、成本與未測

- AI 協助內容與本人驗證／修改：協助建立可重入的 up/down 腳本、T1 核對與測試流程；本人核對 AWS 資源、健康頁、故障結果與回收結果。
- 成本觀察與不確定性：停止主機不計運算費，但 EBS、公有 IPv4 與保留資源可能計費，依當期官方價格為準。
- 未測部分／阻塞／下一步：原始 instance running／2/2 的第一次觀測時間、原始 early curl 與部署前組員簽核無法由既有紀錄回溯；T3(c) 已補測，第二輪重建耗時已重新量得 70 秒，且新主機已恢復 stopped。下週啟動保留主機並重新核對公有 IP／SG。