"""にっし～さんのcast_report_daily.pyのCSV取得方式を店舗別DBへ接続。"""

import argparse
import csv
import io
import math
import re
import time
import unicodedata
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
from playwright.sync_api import sync_playwright

import report_missing_check as staff
from store_report_daily import google_client, load_target_stores, SALES_DB_SHEET_ID, log, to_summary_cache_rows, upsert_summary_cache

TAB = "cast_activity_history"
COLUMNS = ["店舗名", "店舗コード", "代行会社", "エリア", "日付", "指標", "値"]
TEXT_COLUMNS = {"キャスト名", "ショップ名", "日付", "店舗コード", "店舗ID", "開始時刻", "終了時刻"}


def normalize_shop(name):
    value = unicodedata.normalize("NFKC", str(name or "")).lower()
    value = re.sub(r"yogibo\s*store|yogibo", "", value)
    value = re.sub(r"[\s・･]", "", value)
    return re.sub(r"店$", "", value)


def number(value):
    value = unicodedata.normalize("NFKC", str(value or "")).strip()
    value = value.replace(",", "").replace("¥", "").replace("￥", "")
    if value in {"", "-", "—"}:
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("勤務CSVに非有限数があります")
    return result


def parse_activity(raw, stores, start, end):
    text = raw.decode("cp932", errors="strict").lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not {"ショップ名", "日付", "8ステップ数"}.issubset(reader.fieldnames):
        raise RuntimeError("キャスト日別CSVの必須列がありません")
    lookup = {}
    for code, info in stores.items():
        lookup.setdefault(normalize_shop(info["店舗名"]), []).append(code)
    totals, unresolved, dates = {}, set(), set()
    for row in reader:
        if not any(str(v or "").strip() for v in row.values()):
            continue
        day = datetime.strptime(row["日付"].strip(), "%Y/%m/%d").date()
        if not start <= day <= end:
            raise RuntimeError("取得CSVに指定期間外の日付があります")
        dates.add(day)
        code = str(row.get("店舗コード", "")).strip()
        if code and code not in stores:
            continue
        if not code:
            matches = lookup.get(normalize_shop(row["ショップ名"]), [])
            if len(matches) != 1:
                unresolved.add(row["ショップ名"])
                continue
            code = matches[0]
        for metric, raw_value in row.items():
            if not metric or metric in TEXT_COLUMNS:
                continue
            canonical = unicodedata.normalize("NFKC", metric).strip()
            # 全数値列を保存。比率や時刻は合算しない。
            if "%" in str(raw_value) or "率" in canonical:
                continue
            value = number(raw_value)
            if value is None:
                continue
            key = (code, day.isoformat(), canonical)
            totals[key] = totals.get(key, 0.0) + value
    if not totals or end not in dates:
        raise RuntimeError(f"キャストCSVの最終日 {end} は取得できていません")
    output = []
    for (code, day, metric), value in totals.items():
        info = stores[code]
        output.append([info["店舗名"], code, info["代行会社"], info["エリア"], day, metric, value])
    log(f"キャスト集計: {len(totals)}指標行 / 担当外・名称未解決 {len(unresolved)}ショップ")
    # 個人名は保存・ログ出力しない。
    return output


def fetch_csv(page, start, end):
    # 出典: yogibo-dashboard/scripts/cast_report_daily.py::fetch_daily_csv
    query = urllib.parse.urlencode({"action": "input", "sdate": start.isoformat(),
                                  "edate": end.isoformat(), "daily_csv_download": "CSV_日別"})
    response = page.request.get(f"{staff.BASE}/manage/cast/cast_report.php?{query}", timeout=120000)
    if response.status != 200:
        raise RuntimeError(f"キャストCSV取得失敗: HTTP {response.status}")
    return response.body()


def retry(operation):
    for attempt in range(4):
        try:
            return operation()
        except gspread.exceptions.APIError as exc:
            if exc.response.status_code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise
            time.sleep(2 ** attempt)


def save(client, incoming, ranges):
    book = client.open_by_key(SALES_DB_SHEET_ID)
    try:
        sheet = book.worksheet(TAB)
    except gspread.WorksheetNotFound:
        sheet = book.add_worksheet(title=TAB, rows=1000, cols=7)
    values = retry(sheet.get_all_values)
    values = [r for r in values if any(str(v).strip() for v in r)]
    if values and values[0][:7] != COLUMNS:
        raise RuntimeError("cast_activity_historyの列構成が一致しません")
    merged = {}
    for row in values[1:]:
        row = (row + [""] * 7)[:7]
        # 再取得範囲は全置換。訂正・削除された実績も残さない。
        if any(a.isoformat() <= row[4] <= b.isoformat() for a, b in ranges):
            continue
        merged[(row[1], row[4], row[5])] = row
    for row in incoming:
        merged[(row[1], row[4], row[5])] = [str(v) for v in row]
    output = [COLUMNS] + [merged[k] for k in sorted(merged)]
    old_count = len(values)
    if sheet.row_count < len(output) + 10:
        retry(lambda: sheet.resize(rows=len(output) + 10))
    for offset in range(0, len(output), 3000):
        retry(lambda offset=offset: sheet.update(range_name=f"A{offset + 1}",
              values=output[offset:offset + 3000], value_input_option="RAW"))
    if old_count > len(output):
        retry(lambda: sheet.batch_clear([f"A{len(output) + 1}:G{old_count}"]))
    log(f"キャスト店舗別保存完了: {len(incoming)}行 / 当年・前年データ")


def save_summaries(client, incoming, stores, ranges):
    output = []
    for period, (start, end) in zip(("cast_mtd", "cast_mtd_prev"), ranges):
        grouped = {}
        for row in incoming:
            if not start.isoformat() <= row[4] <= end.isoformat():
                continue
            result = grouped.setdefault(row[1], {"店舗コード": row[1]})
            result[row[5]] = result.get(row[5], 0) + float(row[6])
        output.extend(to_summary_cache_rows(list(grouped.values()), stores, period,
                                           start.strftime("%Y%m%d"), end.strftime("%Y%m%d")))
    retry(lambda: upsert_summary_cache(client, output))
    log(f"キャストTOP集計保存完了: {len(output)}行")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", help="YYYY-MM-DD。省略時は月初")
    parser.add_argument("--end", help="YYYY-MM-DD。省略時は前日")
    args = parser.parse_args()
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)
    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else end.replace(day=1)
    if start > end:
        raise ValueError("開始日は終了日以前を指定してください")
    ranges = [(start, end), (start - timedelta(weeks=52), end - timedelta(weeks=52))]
    client = google_client()
    stores = load_target_stores(client)
    incoming = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="ja-JP", timezone_id="Asia/Tokyo")
        page = context.new_page()
        try:
            staff.login(page)
            for a, b in ranges:
                log(f"キャスト日別取得: {a}〜{b}")
                incoming.extend(parse_activity(fetch_csv(page, a, b), stores, a, b))
        finally:
            browser.close()
    save(client, incoming, ranges)
    save_summaries(client, incoming, stores, ranges)


if __name__ == "__main__":
    main()
