"""ストアマネージャー日報CSVを日次取得し、売上管理DBへ保存する。"""

import argparse
import csv
import io
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
from playwright.sync_api import sync_playwright

import report_missing_check as report_api
from store_report_daily import SALES_DB_SHEET_ID, google_client, log


REPORT_PATH = "/store/manager_report_list.php"
REPORT_TAB = "manager_reports"
REPORT_COLUMNS = [
    "日付", "店舗名", "マネージャー名", "グッド！", "オポチュニティ↑",
    "個人的なこと", "改善要望",
]


def decode_csv(raw: bytes) -> list[dict[str, str]]:
    for encoding in ("utf-8-sig", "cp932", "utf-8"):
        try:
            text = raw.decode(encoding)
            rows = list(csv.DictReader(io.StringIO(text)))
            break
        except UnicodeDecodeError:
            continue
    else:
        raise RuntimeError("日報CSVの文字コードを判定できませんでした")

    required = {"日付", "店舗名", "マネージャー名", "グッド！", "オポチュニティ↑"}
    if not rows or not required.issubset(rows[0]):
        missing = required.difference(rows[0] if rows else {})
        raise RuntimeError(f"日報CSVの列構成が変わった可能性があります。不足列: {sorted(missing)}")

    result = []
    for row in rows:
        try:
            report_date = datetime.strptime(str(row.get("日付", "")).strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            try:
                report_date = datetime.strptime(str(row.get("日付", "")).strip(), "%Y/%m/%d").strftime("%Y-%m-%d")
            except ValueError:
                continue
        values = [report_date]
        values.extend(str(row.get(col, "") or "").strip() for col in REPORT_COLUMNS[1:])
        if values[1] and values[2]:
            result.append(values)
    return result


def fetch_csv(page, target_date: str, debug_dir: Path) -> bytes:
    iso = datetime.strptime(target_date, "%Y%m%d").strftime("%Y-%m-%d")
    url = (
        f"{report_api.BASE}{REPORT_PATH}?report_day_start={iso}"
        f"&report_day_end={iso}&shop_id="
    )
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(1_500)
    if page.locator("input[type='password']").count():
        raise RuntimeError("日報画面でログイン状態を確認できませんでした")

    button = page.locator(
        "input[value*='CSV'], button:has-text('CSV DL'), a:has-text('CSV DL')"
    ).first
    if button.count() == 0:
        page.screenshot(path=str(debug_dir / "manager-report-button-missing.png"), full_page=True)
        raise RuntimeError("日報画面のCSV DLボタンが見つかりません")

    with page.expect_download(timeout=120_000) as download_info:
        button.click()
    download = download_info.value
    download_path = download.path()
    if download_path is None:
        raise RuntimeError("日報CSVのダウンロードファイルを取得できませんでした")
    return Path(download_path).read_bytes()


def upsert_reports(client, incoming_rows: list[list[str]]) -> tuple[int, int]:
    book = client.open_by_key(SALES_DB_SHEET_ID)
    try:
        sheet = book.worksheet(REPORT_TAB)
    except gspread.WorksheetNotFound:
        sheet = book.add_worksheet(title=REPORT_TAB, rows=1000, cols=len(REPORT_COLUMNS))

    values = sheet.get_all_values()
    if not values or values[0] != REPORT_COLUMNS:
        sheet.clear()
        sheet.update(range_name="A1", values=[REPORT_COLUMNS])
        values = [REPORT_COLUMNS]

    row_numbers = {}
    for row_number, row in enumerate(values[1:], start=2):
        padded = (list(row) + [""] * len(REPORT_COLUMNS))[:len(REPORT_COLUMNS)]
        row_numbers[(padded[0], padded[1], padded[2])] = row_number

    deduped = {}
    for row in incoming_rows:
        deduped[(row[0], row[1], row[2])] = row

    updates, appends = [], []
    for key, row in deduped.items():
        if key in row_numbers:
            updates.append({"range": f"A{row_numbers[key]}:G{row_numbers[key]}", "values": [row]})
        else:
            appends.append(row)
    if updates:
        sheet.batch_update(updates, value_input_option="RAW")
    if appends:
        sheet.append_rows(appends, value_input_option="RAW")
    return len(appends), len(updates)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="対象日 YYYYMMDD（省略時は日本時間の前日）")
    parser.add_argument("--debug-dir", default="debug")
    args = parser.parse_args()
    target_date = args.date or (
        datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)
    ).strftime("%Y%m%d")
    datetime.strptime(target_date, "%Y%m%d")

    debug_dir = Path(args.debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ja-JP", timezone_id="Asia/Tokyo", accept_downloads=True
        )
        page = context.new_page()
        try:
            report_api.login(page)
            raw = fetch_csv(page, target_date, debug_dir)
        except Exception:
            page.screenshot(path=str(debug_dir / "manager-report-failure.png"), full_page=True)
            raise
        finally:
            browser.close()

    rows = decode_csv(raw)
    if not rows:
        log(f"日報CSVに保存可能な行がありません: {target_date}")
        return
    inserted, updated = upsert_reports(google_client(), rows)
    log(f"日報保存完了: {target_date} / {len(rows)}件 / 新規 {inserted}件 / 更新 {updated}件")


if __name__ == "__main__":
    main()
