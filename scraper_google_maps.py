#!/usr/bin/env python3
"""
Google Maps 自家焙煎珈琲店スクレイパー v2
追加取得項目: 営業時間・カテゴリ・写真URL・支払方法・サービス

実行方法:
  python scraper_google_maps.py --pref 東京都 --dry-run
  python scraper_google_maps.py
"""

import argparse
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from tqdm import tqdm
    
load_dotenv()

# ============================================================
# 設定
# ============================================================
DATABASE_URL  = os.environ.get("DATABASE_URL", "")
OUTPUT_DIR    = Path("./output")
CONFIDENCE    = 0.75
SCROLL_PAUSE  = 1.5
PAGE_PAUSE    = 1.2   # 追加取得があるので少し長めに
MAX_SCROLL    = 30
HEADLESS      = True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

PREFECTURES = [
    "北海道","青森県","岩手県","宮城県","秋田県","山形県","福島県",
    "茨城県","栃木県","群馬県","埼玉県","千葉県","東京都","神奈川県",
    "新潟県","富山県","石川県","福井県","山梨県","長野県",
    "岐阜県","静岡県","愛知県","三重県",
    "滋賀県","京都府","大阪府","兵庫県","奈良県","和歌山県",
    "鳥取県","島根県","岡山県","広島県","山口県",
    "徳島県","香川県","愛媛県","高知県",
    "福岡県","佐賀県","長崎県","熊本県","大分県","宮崎県","鹿児島県","沖縄県",
]

SEARCH_KEYWORDS = [
    "自家焙煎 カフェ",        # 席あり自家焙煎カフェ
    "コーヒー豆 自家焙煎",    # 豆販売メインの焙煎所・直売所
    "コーヒーロースター",      # ロースタリー全般（英語系・スペシャルティ系）
]

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--lang=ja-JP",
]

PROGRESS_FILE = OUTPUT_DIR / "progress.json"

# ============================================================
# 進捗管理（途中停止 → 再開対応）
# ============================================================
def load_progress() -> set:
    if PROGRESS_FILE.exists():
        return set(json.loads(PROGRESS_FILE.read_text(encoding="utf-8")))
    return set()

def save_progress(done: set):
    PROGRESS_FILE.write_text(json.dumps(list(done)), encoding="utf-8")

# ============================================================
# ブラウザ設定
# ============================================================
def make_context(playwright):
    browser = playwright.chromium.launch(headless=HEADLESS, args=LAUNCH_ARGS)
    ctx = browser.new_context(
        locale="ja-JP",
        timezone_id="Asia/Tokyo",
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    return browser, ctx

# ============================================================
# 検索 → URL収集
# ============================================================
def search_and_collect_links(page, query: str) -> list:
    url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PWTimeout:
        log.warning(f"  タイムアウト: {query}")
        return []

    try:
        page.click('button[aria-label*="同意"]', timeout=3000)
    except PWTimeout:
        pass

    try:
        page.wait_for_selector('[role="feed"]', timeout=15000)
    except PWTimeout:
        log.warning(f"  結果パネルなし: {query}")
        return []

    links = set()
    scroll_count = 0

    while scroll_count < MAX_SCROLL:
        for a in page.query_selector_all('[role="feed"] a[href*="/maps/place/"]'):
            href = a.get_attribute("href")
            if href:
                links.add(re.sub(r'\?.*$', '', href))

        if page.query_selector('[role="feed"] .HlvSq'):
            break

        feed = page.query_selector('[role="feed"]')
        if not feed:
            break

        prev = len(links)
        feed.evaluate("el => el.scrollTop += 2000")
        time.sleep(SCROLL_PAUSE)

        if len(page.query_selector_all('[role="feed"] a[href*="/maps/place/"]')) == prev:
            scroll_count += 1
            if scroll_count >= 3:
                break
        else:
            scroll_count = 0

    return list(links)

# ============================================================
# 詳細情報の抽出（v2: 営業時間・カテゴリ・写真・支払・サービス追加）
# ============================================================
def extract_shop_detail(page, url: str) -> dict | None:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_selector('h1', timeout=10000)
        time.sleep(PAGE_PAUSE)
    except PWTimeout:
        return None

    shop = {"source_url": url}

    # ── 店舗名 ────────────────────────────────────────────────
    h1 = page.query_selector('h1')
    shop["name"] = h1.inner_text().strip() if h1 else None
    if not shop["name"]:
        return None

    # ── カテゴリ ──────────────────────────────────────────────
    # 例: "カフェ" "コーヒーショップ" "焙煎業者"
    cat_el = page.query_selector('button[jsaction*="category"]')
    if not cat_el:
        cat_el = page.query_selector('[class*="DkEaL"]')   # Googleのクラス名
    if not cat_el:
        # aria-labelにカテゴリが入っているケースも
        for el in page.query_selector_all('span[aria-label]'):
            label = el.get_attribute("aria-label") or ""
            if any(k in label for k in ["カフェ","コーヒー","喫茶","焙煎"]):
                cat_el = el
                break
    shop["category"] = cat_el.inner_text().strip() if cat_el else None

    # ── 住所 ─────────────────────────────────────────────────
    addr_btn = page.query_selector('[data-item-id="address"]')
    if addr_btn:
        shop["address_raw"] = re.sub(
            r'^住所[:：\s]+', '',
            addr_btn.get_attribute("aria-label") or addr_btn.inner_text()
        ).strip()
    else:
        shop["address_raw"] = None

    if shop["address_raw"]:
        m = re.search(r'(北海道|(?:東京|京都|大阪)都?府?|.{2,3}[都道府県])', shop["address_raw"])
        shop["prefecture"] = m.group(1) if m else None
        m2 = re.search(
            r'(?:北海道|(?:東京|京都|大阪)都?府?|.{2,3}[都道府県])(.{2,6}?[市区町村郡])',
            shop["address_raw"]
        )
        shop["city"] = m2.group(1) if m2 else None
    else:
        shop["prefecture"] = shop["city"] = None

    # ── 電話番号 ──────────────────────────────────────────────
    phone_btn = page.query_selector('[data-item-id*="phone"]')
    if phone_btn:
        shop["phone"] = re.sub(
            r'^電話[:：\s]+', '',
            phone_btn.get_attribute("aria-label") or phone_btn.inner_text()
        ).strip()
    else:
        shop["phone"] = None

    # ── ウェブサイト ──────────────────────────────────────────
    web_btn = page.query_selector('[data-item-id="authority"]')
    shop["website"] = (web_btn.get_attribute("href") or web_btn.inner_text().strip()) if web_btn else None

    # ── 評価・レビュー数 ──────────────────────────────────────
    rating_el = page.query_selector('[jsaction*="rating"]') or page.query_selector('span[aria-label*="星"]')
    if rating_el:
        aria = rating_el.get_attribute("aria-label") or ""
        m = re.search(r'([\d.]+)\s*(?:つ星|stars?)', aria)
        shop["rating"] = float(m.group(1)) if m else None
        m2 = re.search(r'([\d,]+)\s*(?:件|reviews?)', aria)
        shop["review_count"] = int(m2.group(1).replace(",", "")) if m2 else None
    else:
        shop["rating"] = shop["review_count"] = None

    # ── 座標 ─────────────────────────────────────────────────
    m = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', page.url)
    if m:
        shop["lat"] = float(m.group(1))
        shop["lng"] = float(m.group(2))
    else:
        # source_urlのdata=パラメータから抽出
        m2 = re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', url)
        shop["lat"] = float(m2.group(1)) if m2 else None
        shop["lng"] = float(m2.group(2)) if m2 else None

    # ── Place ID ─────────────────────────────────────────────
    m = re.search(r'place/([^/]+)/', url)
    shop["google_place_id"] = m.group(1) if m else None

    # ── 営業時間 ──────────────────────────────────────────────
    # 営業時間ボタンをクリックして展開してから取得
    hours = {}
    try:
        hours_btn = page.query_selector('[data-item-id*="oh"]')
        if not hours_btn:
            hours_btn = page.query_selector('[aria-label*="営業時間"]')
        if hours_btn:
            hours_btn.click()
            time.sleep(0.8)
            # 展開後のテーブルから曜日・時間を取得
            rows = page.query_selector_all('table[class*="eK4R0e"] tr, [class*="WgFkxc"] tr')
            for row in rows:
                cells = row.query_selector_all('td, th')
                if len(cells) >= 2:
                    day  = cells[0].inner_text().strip()
                    time_text = cells[1].inner_text().strip()
                    if day:
                        hours[day] = time_text
            # 閉じる
            page.keyboard.press("Escape")
    except Exception:
        pass
    shop["hours"] = hours if hours else None

    # ── 写真URL ───────────────────────────────────────────────
    photos = []
    try:
        # メイン写真エリアの画像を取得
        img_els = page.query_selector_all('img[src*="googleusercontent"][src*="photo"]')
        for img in img_els[:5]:   # 最大5枚
            src = img.get_attribute("src") or ""
            if src and "googleusercontent" in src:
                # 高解像度版に変換（=w400-h300 → =w800-h600）
                src = re.sub(r'=w\d+-h\d+', '=w800-h600', src)
                photos.append(src)
    except Exception:
        pass
    shop["photos"] = photos if photos else []

    # ── 支払方法 ──────────────────────────────────────────────
    payment = []
    try:
        # 「支払い方法」セクションのテキストを取得
        all_text = page.inner_text('body')
        pay_section = re.search(
            r'支払い方法[^\n]*\n((?:[^\n]+\n){1,10})',
            all_text
        )
        if pay_section:
            lines = pay_section.group(1).strip().split('\n')
            payment = [l.strip() for l in lines if l.strip() and len(l.strip()) < 30]

        # aria-labelから支払い関連の情報を取得
        if not payment:
            for el in page.query_selector_all('[aria-label*="カード"], [aria-label*="現金"], [aria-label*="Pay"]'):
                label = el.get_attribute("aria-label") or ""
                if label and label not in payment:
                    payment.append(label.strip())
    except Exception:
        pass
    shop["payment_methods"] = payment if payment else []

    # ── サービス ──────────────────────────────────────────────
    services = []
    try:
        # サービスセクション（テイクアウト・デリバリー・イートイン等）
        service_keywords = [
            "テイクアウト", "デリバリー", "イートイン", "店内飲食",
            "持ち帰り", "宅配", "通販", "ネット注文",
            "Uber Eats", "出前館",
        ]
        all_text = page.inner_text('[class*="LTs0Rc"], [class*="E0DTEd"]')
        if all_text:
            for kw in service_keywords:
                if kw.lower() in all_text.lower():
                    services.append(kw)

        # aria-labelからサービス情報を取得
        for el in page.query_selector_all('[aria-label*="テイクアウト"], [aria-label*="デリバリー"], [aria-label*="イートイン"]'):
            label = (el.get_attribute("aria-label") or "").strip()
            if label and label not in services:
                services.append(label)
    except Exception:
        pass
    shop["services"] = services if services else []

    log.debug(f"  ✓ {shop['name']} ({shop.get('prefecture','?')}) "
              f"hours:{bool(shop['hours'])} photos:{len(shop['photos'])} "
              f"payment:{len(shop['payment_methods'])} services:{len(shop['services'])}")
    return shop

# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pref", type=str, default=None)
    parser.add_argument("--area", type=str, default=None,
                        help="検索エリア（市区町村等）。指定時は「area keyword」で検索し、prefは都道府県ラベルとして使用")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keyword", type=str, default=None)
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    target_prefs = [args.pref] if args.pref else PREFECTURES
    keywords = [args.keyword] if args.keyword else SEARCH_KEYWORDS
    # --area 指定時はそのエリアで検索、未指定時は --pref をそのまま使用
    search_area = args.area if args.area else None

    # 進捗読み込み（再開対応）
    done_queries = load_progress()
    if done_queries:
        log.info(f"進捗復元: {len(done_queries)} クエリ完了済み（スキップ）")

    if search_area:
        log.info(f"エリア指定モード: {target_prefs[0]} / {search_area}")

    # DB接続
    conn = None
    if not args.dry_run:
        if not DATABASE_URL:
            log.error("DATABASE_URL 未設定。--dry-run で実行してください。")
            raise SystemExit(1)
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)

    all_shops = []
    seen_urls = set()

    with sync_playwright() as pw:
        browser, ctx = make_context(pw)
        search_page = ctx.new_page()
        detail_page = ctx.new_page()

        try:
            for pref in tqdm(target_prefs, desc="都道府県"):
                for keyword in keywords:
                    # --area 指定時はエリア名で検索、未指定時は都道府県名で検索
                    query = f"{search_area or pref} {keyword}"

                    if query in done_queries:
                        log.info(f"スキップ（完了済み）: {query}")
                        continue

                    log.info(f"検索: {query}")
                    urls = search_and_collect_links(search_page, query)
                    new_urls = [u for u in urls if u not in seen_urls]
                    seen_urls.update(new_urls)
                    log.info(f"  → {len(new_urls)} 件の新規URL")

                    for url in tqdm(new_urls, desc=f"  {search_area or pref}", leave=False):
                        shop = extract_shop_detail(detail_page, url)
                        if not shop:
                            continue
                        # 都道府県が取れなかった場合は --pref の値をセット
                        if not shop.get("prefecture"):
                            shop["prefecture"] = pref
                        all_shops.append(shop)
                        time.sleep(0.8)

                    # 完了記録
                    done_queries.add(query)
                    save_progress(done_queries)
                    time.sleep(2.0)

        finally:
            browser.close()

    # JSON保存
    out = OUTPUT_DIR / f"shops_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(all_shops, f, ensure_ascii=False, indent=2)
    log.info(f"完了: {len(all_shops)} 件 → {out}")

    if conn:
        conn.close()

if __name__ == "__main__":
    main()
