# 渡邊AM 売上管理ダッシュボード セットアップ手順

## 必要なもの
- Python 3.9 以上

## セットアップ（初回のみ）

```bash
# 1. 依存パッケージをインストール
pip install -r requirements.txt

# 2. アプリを起動
streamlit run app.py
```

ブラウザが自動で開き、`http://localhost:8501` にアクセスできます。

## 使い方

1. 左サイドバーの「代行会社」で絞り込み（任意）
2. 「店舗」で対象店舗を選択（複数可）
3. 「日別実績CSV」に `shop_d_YYYYMMDD_YYYYMMDD.csv` をアップロード
4. 期間KPI・日別グラフ・代行会社別集計が自動表示されます

## 履歴・長期分析

- アップロードした今年・前年の実績は `sales_history.csv` に自動追記されます。
- 売上目標・座数目標は `targets_history.csv` に自動追記されます。
- 同じ店舗・日付・指標を再度アップロードした場合は、最新値で更新されるため二重計上されません。
- 「期間分析」では、3月を期首として四半期（Q1〜Q4）・半期・年度を選択できます。
- 「会社別レポート」では、土曜・日曜・月曜の用途に合わせた代行会社別サマリーとTUNAG投稿文を作成できます。
- 履歴CSVは「期間分析」画面からバックアップできます。

> 注意：ローカルPCでの利用中は履歴ファイルが保持されます。Streamlit Community Cloudへ公開する場合は、再起動で消えない外部データベースへの切り替えが必要です。

## 対応CSVフォーマット
- 文字コード: Shift-JIS（UTF-8も自動判定）
- 列構成: 店舗ID, 店舗コード, 店舗名, 項目名, 日付列...


## 日次自動取得

- GitHub Actions「Yogibo store report daily import」が毎日22:30（日本時間）に動きます。
- Yogibo店舗分析のCSV ver.3から、受注金額・座数・客数・CVR・客単価・品数を取得します。
- 店舗マスタで `渡邊_A` / `渡邊_B` かつ `OPEN` の店舗だけを対象にします。
- Google Sheets「売上管理DB」の `sales_history` へ、店舗・日付・指標をキーに追記／更新します。同じ日を再実行しても二重計上されません。
- Actionsの手動実行では、`YYYYMMDD` 形式で過去日を再取得できます。
- 必要なRepository secretsは `YOGIBO_STAFF_ID`、`YOGIBO_STAFF_PASSWORD`、`GCP_SERVICE_ACCOUNT_JSON` です。
