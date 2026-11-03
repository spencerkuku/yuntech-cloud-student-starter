# W9 只讀事件與照片檢查工具

本工具由教師提供，學生不用自行開發。於小組指定成員的Codespace執行；本機筆電繼續負責ESP32燒錄、配網與觸發。

工具查現有HTTPS收據與照片API，不需AWS帳號keys，不連SQL、不建立事件、不發MQTT，也不替學生上傳照片。

## 第一次使用

```bash
python3 -m pip install -r tools/requirements-w09.txt
mkdir -p .local
cp labs/09-edge-cloud/config.example.json .local/w09-check.json
```

編輯 `.local/w09-check.json` 三欄，填本組HTTPS入口、預期device_id、信任CA檔路徑。相對CA路徑以設定檔所在目錄為準；例如設定中的 `w09-api-ca.pem` 應放在 `.local/w09-api-ca.pem`。公用CA須由課程既有可信管道取得，不是伺服器私鑰。

設定檔只接受這三欄，不放權杖。device與operator是不同角色；工具會分別以隱藏輸入詢問，權杖不放命令列。若課程已在受保護環境設定 `W09_DEVICE_TOKEN`、`W09_OPERATOR_TOKEN`，也可沿用；不要執行列印環境變數的命令。

```bash
python3 tools/w09_check.py --config .local/w09-check.json --event-id g03-cam01-A7-1
```

上例事件ID只是教學例子，改用本組剛觸發的ID。HTTPS保留CA／主機名稱驗證，工具拒絕HTTP或重新導向；不要用關閉驗證修連線。

## 工具能確認什麼

- 收據的event_id、device_id符合預期，event_stored=true。
- 同事件照片ready、授權讀回為可解碼JPEG。
- 本地SHA-256與收據、照片回應標頭一致。
- 將照片存到工作目錄 `.local/w09-check/<event_id>.jpg`，可以在Codespace檔案區點開；不需提交或上傳此檔。

工具成功只代表上面兩段通訊／內容核對完成。**它不判定整組通過，也不自動檢查持續警報。** 助教還要看現場新事件、照片內容與ALARM持續期間沒有新增。事件發生時間需對照板上快照／既有事件頁，現行收據沒有完整時間欄位。

失敗時工具只顯示固定的錯誤類別與HTTP狀態，不印秘密或原始回應本文。修正環境或程式後可再查；工具不自動重試或改資料。本工具的離線測試通過不代表每組雲端服務或完整硬體已通過。
