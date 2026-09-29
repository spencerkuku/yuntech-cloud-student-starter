# W3 Sprint 個人報告

- 週次／組別／成員／Git 版本：W3 / 待填 / 待填 / 待填
- 需求與架構選擇：本週是把 inspection 原型服務部署到一台 EC2 上，並保證來源 -> 路由 -> SG -> nginx -> inspection 的 HTTP 請求路徑清楚可追蹤。
- AWS 區域、帳號末四碼、核准資源範圍、成本預估：待實際 Learner Lab 驗證後填寫；未在本地重複擬造 AWS 變更或假設資源存在。
- 部署與重建步驟（不含秘密）：
  1. 先確認 default VPC / subnet / route table。
  2. 檢查 Codespace 對外 IPv4，轉成 /32。
  3. 產生 user data，並將 commit SHA 綁定到 app/version。
  4. 建立 dedicated SG、key pair、EC2 instance。
  5. 等待 instance running、status ok、cloud-init 完成與 /health 200。
  6. 若需重建，使用同一個 commit 重新執行 deploy/up.sh。
- 成功測試：輸入、預期、實際、時間、證據位置：待真實 AWS 執行後填寫；不在此處偽造成功結果。
- 拒絕／故障測試：操作、預期、實際、原因、最小修正：
  - (a) security group 拒絕 port 80 -> 預期 curl timeout / connection refused；實際待填。
  - (b) 停止 inspection 服務 -> 預期 nginx 仍在但後端 502/503；實際待填。
  - (c) 停止 nginx -> 預期連線被拒；實際待填。
- 一次請求經過哪些服務與權限檢查：
  - Codespace 出口 IPv4 -> IGW -> default route table -> default public subnet -> security group rule -> EC2 instance -> nginx -> inspection service on 127.0.0.1:8080 -> JSON /health response.
- AI 協助內容、本人驗證與修改：本次變更包括 deploy/up.sh、deploy/down.sh、W3 practice note 與此報告；所有內容都在 repo 中做過 syntax 檢查，未在 AWS 上宣稱已通過。
- 精確資源 ID 清單與回收／保留理由：待真實 Lab 執行後填寫；不得使用名稱批次刪除，必須保留在 .local/resources.json 內。
- 成本觀察與不確定性：待實際啟動後觀察 EC2/EBS 公有 IPv4 成本；保留主機時確認 stopped 狀態並保持 SG / key pair。
- 未測部分／阻塞／下一步：等待 Learner Lab 憑證與 AWS 環境準備完成後，方可執行 deploy/up.sh 與 deploy/down.sh，並補上真實 curl 證據、版本比對與回收讀回。

## Diff summary

- 新增 deploy/up.sh: 建立一台 W3 EC2 instance，透過 user-data 打包指定 commit，並完成 resource ledger 記錄。
- 新增 deploy/down.sh: 根據 .local/resources.json 停止或刪除已追蹤資源，保證不以名稱掃除大量資源。
- 新增 practice/w3/README.md: 工作流程與故障驗收筆記。
- 新增 reports/w3-sprint-report.md: 本週增量報告範本與執行準備說明。
