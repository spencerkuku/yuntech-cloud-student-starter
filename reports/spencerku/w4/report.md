# W4 繳交：第一筆事件可送入、查詢並顯示

> 本報告依 `reports/W04_繳交範本.md` 建立。部署前不填寫實測結果；不得貼上權杖、私鑰或完整帳號 ID。

## 基本資料

- 組名：第二組　組內代號：spencerku
- 部署的 commit（前 7 碼）：`fd06e10`

## 1. `/health` 的回應

貼上部署後執行 `curl -s http://<主機現在的公開位址>/health` 的原始輸出：

```text
{"status":"ok","service":"inspection","version":"fd06e10fabc1cec79945facdb95fc85b57301ad6","started_at":"2026-09-29T02:52:06Z","auth_configured":true}
```

- [x] `version` 開頭等於上面的 commit
- [x] `auth_configured` 是 `true`

## 2. 拒絕矩陣 7 列

貼上 `practice/spencerku/w4/tests/run_w04_matrix.py` 實際跑出的完整輸出：

```text
version=fd06e10fabc1cec79945facdb95fc85b57301ad6
{"body":{"device_id":"g02-d01","event_id":"g02-spencerku-0001","note":"inspection ok","observed_at":"2026-09-29T10:00:00+08:00","received_at":"2026-09-29T02:52:21Z","type":"status"},"case":1,"name":"reporter legal event","status":201}
{"body":{"error":"unauthorized"},"case":2,"name":"no token","status":401}
{"body":{"error":"forbidden"},"case":3,"name":"operator sends event","status":403}
{"body":{"error":"must be an ISO 8601 timestamp with timezone","field":"observed_at"},"case":4,"name":"observed_at without timezone","status":400}
{"body":{"error":"event_id already exists","field":"event_id"},"case":5,"name":"duplicate event","status":409}
{"body":{"error":"forbidden"},"case":6,"name":"reporter reads list","status":403}
{"body":{"events":[{"device_id":"g02-d01","event_id":"g02-spencerku-0001","note":"inspection ok","observed_at":"2026-09-29T10:00:00+08:00","received_at":"2026-09-29T02:52:21Z","type":"status"}]},"case":7,"name":"operator reads list","status":200}
```

- [x] 開頭的 `version` 和第 1 段相同
- [x] #1 回應裡有 `event_id`（組內代號開頭）和 `received_at`
- [x] #4 回應的 `field` 是 `observed_at`
- [x] 回應裡沒有權杖

有哪一列和 README 的預期不同？原因是什麼？（全部相同就寫「無」）無。

## 3. 顯示頁截圖（另外上傳）

檔名：`W04_顯示頁_spencerku.png`（待上傳）

- [ ] 看得到第 2 段 #1 那筆的 `event_id` 和 `received_at`（待瀏覽器截圖確認）
- [ ] 權杖輸入框已清空（待瀏覽器截圖確認）

## 4. 審查 Copilot 的程式

| 四件事 | Copilot 第一版 | 沒過的話，你請它怎麼改 |
| --- | --- | --- |
| 1. 先確認是誰，才看內容 | 過 | `test_unauthenticated_request_is_rejected_before_body_validation` 驗證未帶權杖時先回 401。 |
| 2. 權杖、整筆事件不進日誌和錯誤回應 | 過 | `log_message` 不記錄請求內容，錯誤回應只含 `error` 與必要的 `field`。 |
| 3. 顯示頁用 `textContent`，權杖不放網址或瀏覽器 | 過 | 離線測試確認頁面使用 `textContent`，且沒有 `innerHTML`、`localStorage` 或 URL token。 |
| 4. 你的 3 筆測資真的有被讀進去 | 過 | `test_w04_service.py` 以 `Path` 讀取三個 fixture 並驗證成功與拒絕欄位。 |

## 5. 用自己的話說

一筆事件送到 `/events` 後，服務先檢查 Bearer 權杖，沒有權杖或權杖錯誤就回 401。權杖正確後再檢查角色，operator 送事件或 reporter 查詢會回 403。reporter 通過授權後，服務檢查 Content-Type、本文大小、JSON 結構、欄位格式與 `observed_at` 時區，錯誤時回 400。格式正確再檢查 `event_id` 是否重複，重複回 409，否則加入 `received_at` 並回 201。
