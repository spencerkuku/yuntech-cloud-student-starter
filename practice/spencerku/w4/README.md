# W4 個人實作

本目錄是第二組 `spencerku` 的 W4 個人實作，服務程式與測試不修改根目錄 W3 檔案。

## 本機測試

```bash
python3 -m unittest discover -s practice/spencerku/w4/tests -v
python3 -m py_compile practice/spencerku/w4/app/service.py practice/spencerku/w4/tests/test_w04_service.py practice/spencerku/w4/tests/run_w04_matrix.py
bash -n practice/spencerku/w4/deploy/deploy.sh
```

測試 fixtures 位於 `tests/fixtures/`，服務資料只存在記憶體中。測試用的秘密檔和部署設定放在 `.local/`，不提交 Git。

## 部署

複製 `w4.env.example` 為 `.local/w4.env`，填入已核對的原 W3 instance ID、SSH 使用者、私鑰路徑與已 commit 的 SHA；另準備權限為 600 的 `.local/app.env`。部署腳本會先執行 `scripts/verify-aws.sh`，再查詢該 instance 的目前公開 IP，顯示目標主機與 commit，必須輸入 `APPROVE` 才會執行。

```bash
bash practice/spencerku/w4/deploy/deploy.sh
```

## 拒絕矩陣

```bash
python3 practice/spencerku/w4/tests/run_w04_matrix.py
```

輸出可放進 W4 報告前，先確認沒有權杖、私鑰或完整帳號 ID。
