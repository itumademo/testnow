#!/usr/bin/env python3
"""
株式適時開示レポート自動配信システム（1ファイル版）

TDnet一次ソース（やのしんAPI）から適時開示を取得し、
好悪材料を自動分類してLINEに通知する。

使い方:
    python main.py                   # 前営業日のレポート
    python main.py --date 20260409   # 指定日のレポート
    python main.py --test            # LINE送信せずstdout出力

必要な環境変数:
    LINE_CHANNEL_TOKEN  ... LINE Messaging APIのチャネルアクセストークン（長期）

セットアップ:
    1. pip install httpx
    2. LINE DevelopersでMessaging APIチャネル作成 → トークン発行
    3. 公式アカウントを自分のLINEで友だち追加
    4. GitHub Secretsに LINE_CHANNEL_TOKEN を登録
"""

import os
import sys
import argparse
import httpx
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 設定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

JST = timezone(timedelta(hours=9))
TDNET_API_BASE = "https://webapi.yanoshin.jp/webapi/tdnet/list"
LINE_CHANNEL_TOKEN = os.environ.get("LINE_CHANNEL_TOKEN", "")

# ─── 好材料キーワード ───
POSITIVE_KEYWORDS = [
    "上方修正", "増額修正", "増益", "最高益", "黒字転換", "黒字化",
    "増配", "復配", "自己株式の取得", "自社株買い", "株式分割",
    "公開買付け", "TOB", "MBO", "子会社化", "業務提携", "資本提携",
    "合弁", "事業譲受",
    "受注", "大型受注", "新製品", "特許", "新技術",
    "承認", "薬事承認", "治験", "臨床試験", "症例数到達",
    "採択", "補助金", "助成金",
    "大量保有", "株主提案",
    "新規上場", "市場変更",
]

# ─── 悪材料キーワード ───
NEGATIVE_KEYWORDS = [
    "下方修正", "減額修正", "減益", "赤字", "赤字転落", "損失",
    "減配", "無配", "配当見送り",
    "債務超過", "第三者委員会", "行政処分", "特設注意",
    "監理銘柄", "上場廃止", "内部統制",
    "公募増資", "新株予約権", "新株式発行", "第三者割当",
    "訴訟", "不正", "改ざん", "横領",
]

# ─── 即撤退レベル ───
CRITICAL_KEYWORDS = [
    "第三者委員会", "不正会計", "粉飾", "監理銘柄",
    "上場廃止", "債務超過", "内部統制の重大な不備",
]

# ─── ノイズ除外 ───
NOISE_KEYWORDS = [
    "コーポレート・ガバナンスに関する報告書",
    "独立役員届出書",
    "定款の一部変更",
    "内部統制システムの構築に関する基本方針",
    "ストックオプション",
    "役員の異動",
    "資本準備金の額の減少",
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# データ構造
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class Disclosure:
    """適時開示1件分"""
    company_name: str
    title: str
    url: str
    disclosed_at: str
    stock_code: str = ""
    sentiment: str = "neutral"
    matched_keywords: list = field(default_factory=list)
    is_noise: bool = False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TDNet取得・分類
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def classify(d: Disclosure):
    """開示タイトルからセンチメント分類"""
    title = d.title

    for kw in NOISE_KEYWORDS:
        if kw in title:
            d.is_noise = True
            d.sentiment = "noise"
            return

    for kw in CRITICAL_KEYWORDS:
        if kw in title:
            d.sentiment = "critical"
            d.matched_keywords.append(kw)
            return

    pos = [kw for kw in POSITIVE_KEYWORDS if kw in title]
    neg = [kw for kw in NEGATIVE_KEYWORDS if kw in title]

    if neg:
        d.sentiment = "negative"
        d.matched_keywords = neg
    elif pos:
        d.sentiment = "positive"
        d.matched_keywords = pos
    else:
        d.sentiment = "neutral"


def parse_rss(xml_text: str) -> list:
    """RSS XMLをパースしてDisclosureリストに変換"""
    disclosures = []
    try:
        root = ET.fromstring(xml_text)
        channel = root.find("channel")
        if channel is None:
            return []

        for item in channel.findall("item"):
            raw_title = item.findtext("title", "")
            link = item.findtext("link", "")
            pub_date = item.findtext("pubDate", "")

            parts = raw_title.split(":", 1)
            company = parts[0].strip() if len(parts) > 1 else ""
            title = parts[1].strip() if len(parts) > 1 else raw_title.strip()

            d = Disclosure(
                company_name=company,
                title=title,
                url=link,
                disclosed_at=pub_date,
            )
            classify(d)
            disclosures.append(d)
    except ET.ParseError as e:
        print(f"[TDNet] XML parse error: {e}")

    return disclosures


def fetch_disclosures(date_str: str) -> dict:
    """指定日の適時開示を取得→分類"""
    client = httpx.Client(
        timeout=30,
        headers={"User-Agent": "StockMorningReport/1.0 (personal-use)"},
    )

    # まずJSON形式を試す
    url = f"{TDNET_API_BASE}/{date_str}.json"
    print(f"[TDNet] Fetching: {url}")

    disclosures = []

    try:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
        items = data if isinstance(data, list) else data.get("items", [])

        for item in items:
            d = Disclosure(
                company_name=item.get("company_name", ""),
                title=item.get("title", ""),
                url=item.get("document_url", item.get("url", "")),
                disclosed_at=item.get("disclosed_date", ""),
                stock_code=item.get("company_code", ""),
            )
            classify(d)
            disclosures.append(d)

    except Exception as e:
        print(f"[TDNet] JSON failed ({e}), trying RSS...")
        # RSSにフォールバック
        try:
            rss_url = f"{TDNET_API_BASE}/{date_str}.rss"
            resp = client.get(rss_url)
            resp.raise_for_status()
            disclosures = parse_rss(resp.text)
        except Exception as e2:
            print(f"[TDNet] RSS also failed: {e2}")

    client.close()

    # 分類
    result = {"critical": [], "positive": [], "negative": [], "neutral": []}
    noise_count = 0

    for d in disclosures:
        if d.is_noise:
            noise_count += 1
            continue
        if d.sentiment in result:
            result[d.sentiment].append(d)

    print(f"\n[TDNet] === 分類結果 ===")
    print(f"  🚨 危険: {len(result['critical'])}件")
    print(f"  🟢 好材料: {len(result['positive'])}件")
    print(f"  🔴 悪材料: {len(result['negative'])}件")
    print(f"  ⚪ その他: {len(result['neutral'])}件")
    print(f"  🗑️ ノイズ除外: {noise_count}件")

    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# レポート整形
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def categorize_positives(disclosures: list) -> dict:
    """好材料をサブカテゴリに分類"""
    cats = {
        "業績（上方修正・最高益）": [],
        "株主還元（増配・自社株買い・分割）": [],
        "M&A・提携": [],
        "受注・新製品・技術": [],
        "バイオ・医療": [],
        "補助金・採択": [],
        "大量保有": [],
        "その他好材料": [],
    }

    biz_kw = {"上方修正", "増額修正", "増益", "最高益", "黒字転換", "黒字化"}
    ret_kw = {"増配", "復配", "自己株式の取得", "自社株買い", "株式分割"}
    ma_kw = {"公開買付け", "TOB", "MBO", "子会社化", "業務提携", "資本提携", "合弁", "事業譲受"}
    ord_kw = {"受注", "大型受注", "新製品", "特許", "新技術"}
    bio_kw = {"承認", "薬事承認", "治験", "臨床試験", "症例数到達"}
    sub_kw = {"採択", "補助金", "助成金"}
    hld_kw = {"大量保有", "株主提案"}

    for d in disclosures:
        kws = set(d.matched_keywords)
        if kws & biz_kw:
            cats["業績（上方修正・最高益）"].append(d)
        elif kws & ret_kw:
            cats["株主還元（増配・自社株買い・分割）"].append(d)
        elif kws & ma_kw:
            cats["M&A・提携"].append(d)
        elif kws & ord_kw:
            cats["受注・新製品・技術"].append(d)
        elif kws & bio_kw:
            cats["バイオ・医療"].append(d)
        elif kws & sub_kw:
            cats["補助金・採択"].append(d)
        elif kws & hld_kw:
            cats["大量保有"].append(d)
        else:
            cats["その他好材料"].append(d)

    return {k: v for k, v in cats.items() if v}


def format_report(classified: dict, date_str: str) -> str:
    """レポートをLINE用テキストに整形"""
    dt = datetime.strptime(date_str, "%Y%m%d")
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    date_disp = f"{dt.year}/{dt.month}/{dt.day}({weekdays[dt.weekday()]})"

    lines = []
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append(f"📊 適時開示レポート {date_disp}")
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append("※ TDnet一次ソースから自動取得")
    lines.append("")

    # 🚨 危険
    criticals = classified.get("critical", [])
    if criticals:
        lines.append("🚨🚨 即撤退レベル 🚨🚨")
        for d in criticals:
            kw = "・".join(d.matched_keywords)
            lines.append(f"⚠️ {d.company_name}")
            lines.append(f"  {d.title}")
            lines.append(f"  検知: {kw}")
            lines.append("")

    # 🟢 好材料
    positives = classified.get("positive", [])
    if positives:
        lines.append(f"🟢 好材料（{len(positives)}件）")
        lines.append("──────────────")
        categories = categorize_positives(positives)
        for cat_name, items in categories.items():
            lines.append(f"\n【{cat_name}】")
            for d in items:
                code = f"<{d.stock_code}> " if d.stock_code else ""
                lines.append(f"• {code}{d.company_name}")
                lines.append(f"  {d.title}")
        lines.append("")

    # 🔴 悪材料
    negatives = classified.get("negative", [])
    if negatives:
        lines.append(f"🔴 悪材料（{len(negatives)}件）")
        lines.append("──────────────")
        for d in negatives:
            kw = "・".join(d.matched_keywords)
            code = f"<{d.stock_code}> " if d.stock_code else ""
            lines.append(f"• {code}{d.company_name}")
            lines.append(f"  {d.title}")
            lines.append(f"  [{kw}]")
        lines.append("")

    # 統計
    total = sum(len(v) for v in classified.values())
    lines.append("───── 統計 ─────")
    lines.append(f"好材料: {len(positives)} / 悪材料: {len(negatives)} / その他: {len(classified.get('neutral', []))}")
    lines.append(f"合計（ノイズ除外後）: {total}件")
    lines.append("━━━━━━━━━━━━━━━━")

    return "\n".join(lines)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LINE送信（ブロードキャスト方式・USER_ID不要）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def send_line(text: str) -> bool:
    """友だち全員にブロードキャスト送信"""
    if not LINE_CHANNEL_TOKEN:
        print("[LINE] ⚠️ TOKEN未設定。stdout出力のみ。")
        print("=" * 50)
        print(text)
        print("=" * 50)
        return False

    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}",
        "Content-Type": "application/json",
    }

    # 長文分割（LINE上限5000文字）
    chunks = split_message(text, 5000)

    client = httpx.Client(timeout=30)
    for i, chunk in enumerate(chunks[:5]):
        payload = {"messages": [{"type": "text", "text": chunk}]}
        try:
            resp = client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                print(f"[LINE] ✅ {i+1}/{len(chunks)} 送信成功")
            else:
                print(f"[LINE] ❌ Error {resp.status_code}: {resp.text}")
                client.close()
                return False
        except Exception as e:
            print(f"[LINE] ❌ Exception: {e}")
            client.close()
            return False

    client.close()
    return True


def split_message(text: str, limit: int) -> list:
    """メッセージを上限に合わせて分割"""
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)
    return chunks


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# メイン
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    parser = argparse.ArgumentParser(description="株式適時開示レポート自動配信")
    parser.add_argument("--date", type=str, default=None, help="対象日（YYYYMMDD）")
    parser.add_argument("--test", action="store_true", help="LINE送信せずstdout出力")
    args = parser.parse_args()

    # 日付決定
    if args.date:
        target_date = args.date
    else:
        now = datetime.now(JST)
        target = now - timedelta(days=1)
        while target.weekday() >= 5:
            target -= timedelta(days=1)
        target_date = target.strftime("%Y%m%d")

    print(f"[Main] 対象日: {target_date}")

    # 取得・分類
    classified = fetch_disclosures(target_date)

    # 整形
    report = format_report(classified, target_date)

    # 送信
    if args.test:
        print("\n[TEST MODE] LINE送信スキップ\n")
        print(report)
    else:
        success = send_line(report)
        if not success:
            print(report)


if __name__ == "__main__":
    main()
