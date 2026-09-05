"""Yogibo店舗分析CSVを取得し、Google Sheetsの売上管理DBへ保存する。"""

import argparse
import csv
import io
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path

import gspread
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

LOGIN_URL = "https://staff.yogibo.jp/manage/"
REPORT_URL = "https://staff.yogibo.jp/manage/report/shop_report_analyze2.php"
DB_SHEET_ID = "1yJdfZj-zq9ilbe7e2h4kDllhH-e092hWAps6uJgf2CU"


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"GitHub Secret {name} が設定されていません")
    return value


def fill_first_visible(page, selectors, value):
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count()):
            item = locator.nth(index)
            if item.is_visible():
                item.fill(value)
                return
    raise RuntimeError(f"入力欄が見つかりません: {selectors}")


def login(page):
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
    fill_first_visible(
        page,
        [
            'input[name="login_id"]', 'input[name="user_id"]',
            'input[name="username"]', 'input[type="email"]',
            'input[type="text"]',
        ],
        required_env("YOGIBO_STAFF_ID"),
    )
    fill_first_visible(
        page,
        ['input[name="password"]', 'input[type="password"]'],
        required_env("YOGIBO_STAFF_PASSWORD"),
    )
    submit = page.locator('button[type="submit"], input[type="submit"]')
    if submit.count() == 0:
        raise RuntimeError("ログインボタンが見つかりません")
    submit.first.click()
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    if page.locator('input[type="password"]').count() and page.locator('input[type="password"]').first.is_visible():
        raise RuntimeError("ログインできませんでした。ID・パスワードまたは追加認証を確認してください")


def set_date_range(page, start: date, end: date):
    candidates = []
    inputs = page.locator("input")
    for index in range(inputs.count()):
        item = inputs.nth(index)
        if not item.is_visible():
            continue
        value = item.input_value()
        name = (item.get_attribute("name") or "") + (item.get_attribute("id") or "")
        if re.fullmatch(r"\d{8}", value or "") or re.search(r"date|start|end|from|to|期間", name, re.I):
            candidates.append(item)
    if len(candidates) < 2:
        raise RuntimeError("期間入力欄を2つ特定できませんでした")
    candidates[0].fill(start.strftime("%Y%m%d"))
    candidates[1].fill(end.strftime("%Y%m%d"))


def select_daily_format(page):
    selects = page.locator("select")
    for index in range(selects.count()):
        select = selects.nth(index)
        if not select.is_visible():
            continue
        labels = select.locator("option").all_text_contents()
        for label in labels:
            if "日別" in label:
                select.select_option(label=label)
                return
    raise RuntimeError("表示形式「日別」が見つかりませんでした")


def download_csv(page, start: date, end: date, output: Path):
    page.goto(REPORT_URL, wait_until="domcontentloaded", timeout=60_000)
    set_date_range(page, start, end)
    select_daily_format(page)
    page.wait_for_timeout(1_000)

    csv_link = page.get_by_text("CSV詳細", exact=True)
    if csv_link.count() == 0:
        raise RuntimeError("「CSV詳細」が見つかりませんでした")
    with page.expect_download(timeout=60_000) as event:
        csv_link.first.click()
    event.value.save_as(output)
    if output.stat().st_size == 0:
        raise RuntimeError("ダウンロードされたCSVが空です")


def decode_csv(raw: bytes):
    for encoding in ("utf-8-sig", "cp932", "utf-8"):
        try:
            return list(csv.reader(io.StringIO(raw.decode(encoding))))
        except UnicodeDecodeError:
            continue
    raise RuntimeError("CSVの文字コードを判定できませんでした")


def save_sheet_tab(client, tab_name: str, csv_path: Path):
    book = client.open_by_key(DB_SHEET_ID)
    values = decode_csv(csv_path.read_bytes())
    try:
        sheet = book.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        sheet = book.add_worksheet(
            title=tab_name,
            rows=max(len(values) + 10, 1000),
            cols=max(max(len(row) for row in values), 30),
        )
    sheet.clear()
    sheet.update(range_name="A1", values=values)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", help="今年CSVの最終日 YYYY-MM-DD。省略時は昨日")
    parser.add_argument("--debug-dir", default="debug")
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = end.replace(day=1)
    prev_start = start - timedelta(weeks=52)
    prev_end = end - timedelta(weeks=52)
    debug_dir = Path(args.debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)
    now_csv = debug_dir / "sales_now.csv"
    prev_csv = debug_dir / "sales_prev.csv"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(accept_downloads=True, locale="ja-JP", timezone_id="Asia/Tokyo")
        try:
            login(page)
            download_csv(page, start, end, now_csv)
            download_csv(page, prev_start, prev_end, prev_csv)
        except Exception:
            page.screenshot(path=str(debug_dir / "failure.png"), full_page=True)
            (debug_dir / "failure_url.txt").write_text(page.url, encoding="utf-8")
            raise
        finally:
            browser.close()

    credentials = json.loads(required_env("GCP_SERVICE_ACCOUNT_JSON"))
    client = gspread.service_account_from_dict(credentials)
    save_sheet_tab(client, "sales_now_raw", now_csv)
    save_sheet_tab(client, "sales_prev_raw", prev_csv)
    print(f"保存完了: 今年 {start}〜{end} / 前年 {prev_start}〜{prev_end}")


if __name__ == "__main__":
    main()
