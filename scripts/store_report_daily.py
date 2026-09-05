"""店舗分析CSVを日次取得し、売上管理DBへ重複なく保存する。"""

import argparse
import json
import os
import sys
import tomllib
import calendar
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
from playwright.sync_api import sync_playwright

import report_missing_check as report_api

SALES_DB_SHEET_ID = "1yJdfZj-zq9ilbe7e2h4kDllhH-e092hWAps6uJgf2CU"
STORE_MASTER_SHEET_ID = "1eNpYmFkubjtFEwKgnpzFp2DUGoMBYJklDCy1gGOa5Rw"
STORE_MASTER_TAB = "店舗データ"
HISTORY_TAB = "sales_history"
HISTORY_COLUMNS = ["店舗名", "店舗コード", "代行会社", "エリア", "日付", "指標", "値"]
METRICS = ["受注金額(税抜)", "座数", "客数", "CVR", "客単価", "品数"]
TARGET_AMS = {"渡邊_A", "渡邊_B"}


def log(message: str) -> None:
    print(f"[{datetime.now(ZoneInfo('Asia/Tokyo')):%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"GitHub Secret {name} が設定されていません")
    return value


def google_client():
    raw = required_env("GCP_SERVICE_ACCOUNT_JSON").strip()
    credentials = None

    def find_credentials(value):
        """TOMLの任意キーや引用文字列の中からサービスアカウント辞書を探す。"""
        if isinstance(value, dict):
            if value.get("client_email") and value.get("private_key"):
                return value
            for child in value.values():
                found = find_credentials(child)
                if found:
                    return found
        elif isinstance(value, str):
            candidate = value.strip()
            try:
                return find_credentials(json.loads(candidate))
            except (json.JSONDecodeError, TypeError):
                return None
        return None

    # GitHub Secretには、JSONファイルの中身とStreamlit用TOMLの
    # どちらが貼られていても動くようにする。
    try:
        credentials = find_credentials(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        try:
            parsed = tomllib.loads(raw)
            credentials = find_credentials(parsed)
        except (tomllib.TOMLDecodeError, json.JSONDecodeError, TypeError):
            # `GCP_SERVICE_ACCOUNT_JSON = {JSON}` のような貼り方も救済する。
            first_brace = raw.find("{")
            last_brace = raw.rfind("}")
            if first_brace >= 0 and last_brace > first_brace:
                credentials = find_credentials(json.loads(raw[first_brace:last_brace + 1]))

    if not isinstance(credentials, dict) or not credentials.get("client_email"):
        raise RuntimeError(
            "GCP_SERVICE_ACCOUNT_JSONを解析できません。JSONファイルの中身、または"
            "[gcp_service_account]で始まるStreamlit Secrets形式を設定してください"
        )
    return gspread.service_account_from_dict(credentials)


def load_target_stores(client) -> dict[str, dict[str, str]]:
    """店舗コードをキーに、渡邊_A/BかつOPENの店舗情報を返す。"""
    values = client.open_by_key(STORE_MASTER_SHEET_ID).worksheet(STORE_MASTER_TAB).get_all_values()
    stores = {}
    for source in values[1:]:
        row = list(source) + [""] * max(0, 9 - len(source))
        code, name = row[0].strip(), row[2].strip()
        am_name, agency = row[4].strip(), row[6].strip()
        status, area = row[7].strip().upper(), row[8].strip()
        if code and name and am_name in TARGET_AMS and status == "OPEN":
            stores[code] = {"店舗名": name, "代行会社": agency, "エリア": area}
    if not stores:
        raise RuntimeError("店舗マスタに『渡邊_A/B かつ OPEN』の店舗が見つかりません")
    return stores


def numeric_value(value: str) -> float:
    cleaned = str(value or "").strip().replace(",", "").replace("%", "")
    cleaned = cleaned.replace("¥", "").replace("￥", "")
    if cleaned in {"", "-", "—"}:
        return 0.0
    return float(cleaned)


def normalize_date(value: str, fallback_ymd: str) -> str:
    raw = str(value or "").strip()
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return datetime.strptime(fallback_ymd, "%Y%m%d").strftime("%Y-%m-%d")


def to_history_rows(csv_rows, target_stores, fallback_ymd):
    result = []
    skipped = set()
    for csv_row in csv_rows:
        code = str(csv_row.get("店舗コード", "")).strip()
        if code not in target_stores:
            if code and code != "-":
                skipped.add(code)
            continue
        master = target_stores[code]
        report_date = normalize_date(csv_row.get("日付", ""), fallback_ymd)
        for metric in METRICS:
            result.append([
                master["店舗名"], code, master["代行会社"], master["エリア"],
                report_date, metric, numeric_value(csv_row.get(metric, "")),
            ])
    if not result:
        raise RuntimeError("取得CSVに担当店舗のデータがありません")
    log(f"担当外として除外した店舗コード数: {len(skipped)}")
    return result


def upsert_history(client, incoming_rows) -> tuple[int, int]:
    book = client.open_by_key(SALES_DB_SHEET_ID)
    try:
        sheet = book.worksheet(HISTORY_TAB)
    except gspread.WorksheetNotFound:
        sheet = book.add_worksheet(title=HISTORY_TAB, rows=1000, cols=len(HISTORY_COLUMNS))

    values = sheet.get_all_values()
    if not values or values[0] != HISTORY_COLUMNS:
        sheet.clear()
        sheet.update(range_name="A1", values=[HISTORY_COLUMNS])
        values = [HISTORY_COLUMNS]

    # 既存行番号を索引化し、再実行時は該当6行だけ更新する。
    # 全履歴のclear→再書き込みを避けるため、データが年単位で増えても安全。
    row_numbers = {}
    for sheet_row_number, row in enumerate(values[1:], start=2):
        padded = (list(row) + [""] * len(HISTORY_COLUMNS))[:len(HISTORY_COLUMNS)]
        key = (padded[1], padded[0], padded[4], padded[5])
        row_numbers[key] = sheet_row_number

    updates = []
    additions = []
    for row in incoming_rows:
        text_row = [str(value) for value in row]
        key = (text_row[1], text_row[0], text_row[4], text_row[5])
        if key in row_numbers:
            row_number = row_numbers[key]
            updates.append((row_number, text_row))
        else:
            additions.append(text_row)

    # 月次取得は数千行になる。1行ずつのrange更新ではなく、既存表をメモリ上で
    # マージして連続範囲へまとめて書くことで、Google Sheets通信を大幅に減らす。
    if len(incoming_rows) >= 1000:
        merged = [
            (list(row) + [""] * len(HISTORY_COLUMNS))[:len(HISTORY_COLUMNS)]
            for row in values
        ]
        for row_number, text_row in updates:
            merged[row_number - 1] = text_row
        merged.extend(additions)
        if sheet.row_count < len(merged) + 10:
            sheet.resize(rows=len(merged) + 10)
        chunk_size = 5000
        for offset in range(0, len(merged), chunk_size):
            chunk = merged[offset:offset + chunk_size]
            start_row = offset + 1
            end_row = offset + len(chunk)
            sheet.update(
                range_name=f"A{start_row}:G{end_row}",
                values=chunk,
                value_input_option="RAW",
            )
    else:
        # 日次処理は変更行だけを更新し、普段の通信量を最小化する。
        if updates:
            sheet.batch_update([
                {"range": f"A{row_number}:G{row_number}", "values": [text_row]}
                for row_number, text_row in updates
            ])
        if additions:
            sheet.append_rows(additions, value_input_option="RAW")
    return len(additions), len(updates)


def month_ranges(start_month: str, end_month: str) -> list[tuple[str, str]]:
    """YYYYMMの開始月〜終了月を、月ごとのYYYYMMDD範囲へ変換する。"""
    start = datetime.strptime(start_month, "%Y%m").date().replace(day=1)
    end = datetime.strptime(end_month, "%Y%m").date().replace(day=1)
    if start > end:
        raise ValueError("開始月は終了月以前を指定してください")
    yesterday = datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)
    ranges = []
    cursor = start
    while cursor <= end and cursor <= yesterday:
        last_day = calendar.monthrange(cursor.year, cursor.month)[1]
        month_end = min(date(cursor.year, cursor.month, last_day), yesterday)
        ranges.append((cursor.strftime("%Y%m%d"), month_end.strftime("%Y%m%d")))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    if not ranges:
        raise ValueError("取得できる過去期間がありません")
    return ranges


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--date", help="対象日 YYYYMMDD。省略時は日本時間の当日")
    mode.add_argument("--start-month", help="過去一括取得の開始月 YYYYMM")
    parser.add_argument("--end-month", help="過去一括取得の終了月 YYYYMM（省略時は開始月と同じ）")
    parser.add_argument("--debug-dir", default="debug")
    args = parser.parse_args()

    if args.end_month and not args.start_month:
        parser.error("--end-monthを使う場合は--start-monthも指定してください")
    if args.start_month:
        periods = month_ranges(args.start_month, args.end_month or args.start_month)
        log(f"過去一括取得: {periods[0][0]}〜{periods[-1][1]}（{len(periods)}か月）")
    else:
        ymd = args.date or datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d")
        datetime.strptime(ymd, "%Y%m%d")
        periods = [(ymd, ymd)]
        log(f"取得対象日: {ymd}")

    required_env("YOGIBO_STAFF_ID")
    required_env("YOGIBO_STAFF_PASSWORD")
    debug_dir = Path(args.debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)
    all_csv_rows = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(locale="ja-JP", timezone_id="Asia/Tokyo")
        page = context.new_page()
        try:
            report_api.login(page)
            for start_ymd, end_ymd in periods:
                log(f"CSV取得中: {start_ymd}〜{end_ymd}")
                raw = report_api.fetch_report_csv(page, start_ymd, end_ymd)
                (debug_dir / f"store-report-{start_ymd}-{end_ymd}.csv").write_bytes(raw)
                try:
                    rows = report_api.parse_report_csv(raw)
                    all_csv_rows.extend(rows)
                    log(f"CSV取得完了: {len(rows)}行")
                except report_api.NoDataYetError as exc:
                    log(f"データなし（継続）: {exc}")
        except Exception:
            page.screenshot(path=str(debug_dir / "failure.png"), full_page=True)
            (debug_dir / "failure_url.txt").write_text(page.url, encoding="utf-8")
            raise
        finally:
            browser.close()

    if not all_csv_rows:
        log("対象期間に保存可能なデータはありませんでした")
        return

    client = google_client()
    target_stores = load_target_stores(client)
    history_rows = to_history_rows(all_csv_rows, target_stores, periods[0][0])
    inserted, updated = upsert_history(client, history_rows)
    store_days = len({(row[1], row[4]) for row in history_rows})
    log(f"保存完了: 店舗日数 {store_days} / 新規 {inserted}行 / 更新 {updated}行")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"エラー: {exc}")
        sys.exit(1)
