"""Yogibo店舗分析への認証とCSV ver.3取得を担当する共通処理。"""

import csv
import io
import os
import urllib.parse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

BASE = "https://staff.yogibo.jp"
REPORT_PATH = "/manage/report/shop_report_analyze2.php"


class NoDataYetError(Exception):
    """営業中などの理由で、対象日のCSVがまだ空であることを示す。"""


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"GitHub Secret {name} が設定されていません")
    return value


def login(page) -> None:
    """店舗分析ページへ直接アクセスし、ログインフォームが出た場合だけ認証する。"""
    url = f"{BASE}{REPORT_PATH}?action=input&store_type=0&period=d"
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2_000)
    if page.locator("input[type='password']").count() == 0:
        return

    user_selector = (
        "input[type='email'], input[name*='mail'], input[name*='user'], "
        "input[name*='login'], input[name*='id'], input[type='text']"
    )
    page.locator(user_selector).first.fill(_required_env("YOGIBO_STAFF_ID"))
    page.locator("input[type='password']").first.fill(_required_env("YOGIBO_STAFF_PASSWORD"))
    submit = page.locator("button[type='submit'], input[type='submit'], button:has-text('ログイン')")
    if submit.count() == 0:
        raise RuntimeError("ログインボタンが見つかりません")
    submit.first.click()
    try:
        page.wait_for_load_state("networkidle", timeout=25_000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(2_000)

    if page.locator("input[type='password']").count():
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1_500)
    if page.locator("input[type='password']").count():
        raise RuntimeError("ログイン失敗：ID・パスワードを確認してください")


def fetch_report_csv(
    page,
    start_ymd: str,
    end_ymd: str | None = None,
    button: str = "CSV ver.3",
    period: str = "d",
) -> bytes:
    """GETフォームを直接呼び、指定期間・表示形式のCSV ver.3を取得する。"""
    if period not in {"d", "w", "m"}:
        raise ValueError(f"未対応の表示形式です: {period}")
    end_ymd = end_ymd or start_ymd
    query = {
        "action": "input",
        "sdate": start_ymd,
        "edate": end_ymd,
        "store_type": "0",
        "period": period,
        "btn": button,
    }
    url = f"{BASE}{REPORT_PATH}?{urllib.parse.urlencode(query)}"
    response = page.request.get(url, timeout=120_000)
    if response.status != 200:
        raise RuntimeError(f"CSV取得失敗：HTTP {response.status}")
    return response.body()


def parse_report_csv(raw: bytes) -> list[dict[str, str]]:
    """CSV ver.3を辞書の配列にする。引用符内のカンマにも対応する。"""
    text = raw.decode("cp932", errors="replace").lstrip("\ufeff")
    rows = list(csv.DictReader(io.StringIO(text)))
    rows = [row for row in rows if any(str(value or "").strip() for value in row.values())]
    if not rows:
        raise NoDataYetError("対象日の店舗分析CSVはまだ0行です。データ確定後に再実行してください")

    required = {
        "日付", "店舗コード", "店舗名", "受注金額(税抜)",
        "座数", "客数", "品数", "CVR", "客単価",
    }
    missing = required.difference(rows[0].keys())
    if missing:
        raise RuntimeError(f"CSVの列構成が変わった可能性があります。不足列: {sorted(missing)}")
    return rows
