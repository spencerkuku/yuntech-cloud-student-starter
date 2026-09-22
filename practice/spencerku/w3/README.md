# W3 本機部署材料

這個目錄保存 W3 的個人操作材料。真正的 AWS 建立、停止與回收都需要在自己的 Learner Lab 中互動確認。

## 使用方式

1. 複製 `w3.env.example` 為 `.local/w3.env`，填入 T1 已核對的非秘密資料。
2. 先執行 `bash ../../../scripts/verify-aws.sh`。
3. 檢查並提交部署 commit，然後執行 `bash practice/w3/deploy/up.sh`。
4. T3 故障測試完成後，執行 `bash practice/w3/deploy/down.sh` 回收，或執行 `bash practice/w3/deploy/down.sh --stop` 保留停止中的主機。

`.local/` 內的設定、資源 ID、user data 與 SSH 私鑰不得提交。`w3.env` 不得放入憑證或私鑰內容，只能放路徑與已核對的資源參數。

## 必填設定

| 變數 | 用途 |
|---|---|
| `AWS_REGION` | Learner Lab 區域 |
| `VPC_ID` / `SUBNET_ID` / `AMI_ID` | T1 核對的網路與 AL2023 x86_64 AMI |
| `SOURCE_CIDR` | Codespace 出口 IPv4 的 `/32` |
| `GROUP_NAME` / `OWNER_CODE` | 課程標籤 |
| `KEY_NAME` / `PUBLIC_KEY_FILE` | 匯入 AWS 的 ed25519 公鑰 |
| `DEPLOY_COMMIT` | 要部署的已提交 commit，預設為 `HEAD` |

腳本會建立一個專用 Security Group、一個匯入的 key pair 與一台 `t3.micro`。根磁碟為加密 gp3，主機終止時刪除，IMDSv2 設為 required。