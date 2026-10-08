import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

API_BASE = "https://api-global-community.plaync.com/aion2_global"
WEB_BASE = "https://aion2.plaync.com"
SEEN_FILE = Path("seen.json")
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

HEADERS = {
    "User-Agent": "AION2-Remind/1.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.7",
    "Origin": WEB_BASE,
}

BOARDS = [
    {
        "key": "notice",
        "aliases": ["notice_ja", "notice_jp"],
        "list_url": f"{WEB_BASE}/ja-jp/board/notice/list",
        "view_path": "/ja-jp/board/notice/view",
    },
    {
        "key": "update",
        "aliases": ["update_ja", "update_jp"],
        "list_url": f"{WEB_BASE}/ja-jp/board/update/list",
        "view_path": "/ja-jp/board/update/view",
    },
]


def request_text(url, *, accept="application/json", timeout=20):
    headers = dict(HEADERS)
    headers["Accept"] = accept
    headers["Referer"] = WEB_BASE + "/"
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def request_json(url):
    return json.loads(request_text(url))


def load_seen():
    if not SEEN_FILE.exists():
        return []

    try:
        value = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except Exception:
        return []


def save_seen(items):
    SEEN_FILE.write_text(
        json.dumps(items[-400:], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def article_url(board, article_id):
    return f"{WEB_BASE}{board['view_path']}?articleId={article_id}"


def normalize_api_item(board, item):
    article_id = str(item.get("id") or "").strip()
    title = html.unescape(str(item.get("title") or "")).strip()
    timestamps = item.get("timestamps") or {}

    if not article_id or not title:
        return None

    return {
        "id": f"{board['key']}:{article_id}",
        "article_id": article_id,
        "board": board["key"],
        "title": title,
        "url": article_url(board, article_id),
        "posted_at": timestamps.get("postedAt") or "",
    }


def fetch_board_api(board):
    last_error = None

    for alias in board["aliases"]:
        params = urllib.parse.urlencode(
            {
                "isVote": "true",
                "moreSize": 30,
                "moreDirection": "BEFORE",
                "previousArticleId": "0",
            }
        )
        url = f"{API_BASE}/board/{alias}/article/search/moreArticle?{params}"

        try:
            data = request_json(url)
            items = []

            for raw in data.get("contentList") or []:
                item = normalize_api_item(board, raw)
                if item:
                    items.append(item)

            if items:
                print(f"{board['key']}: API({alias}) から {len(items)} 件取得")
                return items

            last_error = RuntimeError(f"{alias}: contentList が空です")
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"API取得に失敗: {last_error}")


class ArticleLinkParser(HTMLParser):
    def __init__(self, view_path):
        super().__init__()
        self.view_path = view_path
        self.current_href = None
        self.current_text = []
        self.items = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return

        href = dict(attrs).get("href", "")
        if self.view_path in href and "articleId=" in href:
            self.current_href = href
            self.current_text = []

    def handle_data(self, data):
        if self.current_href is not None:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() != "a" or self.current_href is None:
            return

        title = re.sub(r"\s+", " ", "".join(self.current_text)).strip()
        self.items.append((self.current_href, title))
        self.current_href = None
        self.current_text = []


def fetch_board_html(board):
    source = request_text(
        board["list_url"],
        accept="text/html,application/xhtml+xml",
    )

    parser = ArticleLinkParser(board["view_path"])
    parser.feed(source)

    items = []
    used = set()

    for href, title in parser.items:
        match = re.search(r"[?&]articleId=([0-9a-fA-F]+)", href)
        if not match or not title:
            continue

        article_id = match.group(1)
        item_id = f"{board['key']}:{article_id}"

        if item_id in used:
            continue

        used.add(item_id)

        items.append(
            {
                "id": item_id,
                "article_id": article_id,
                "board": board["key"],
                "title": html.unescape(title),
                "url": article_url(board, article_id),
                "posted_at": "",
            }
        )

    if not items:
        raise RuntimeError("HTMLから記事を取得できませんでした")

    print(f"{board['key']}: HTMLから {len(items)} 件取得")
    return items[:30]


def fetch_board(board):
    try:
        return fetch_board_api(board)
    except Exception as api_error:
        print(f"{board['key']}: API失敗 ({api_error})")
        print(f"{board['key']}: HTML取得へフォールバック")
        return fetch_board_html(board)


def classify(item):
    title = item["title"].lower()

    if item["board"] == "update":
        return "🔄 アップデート", 0x2ECC71

    maintenance_words = (
        "メンテ",
        "maintenance",
        "臨時点検",
        "緊急点検",
        "定期点検",
    )
    trouble_words = (
        "障害",
        "不具合",
        "問題",
        "接続",
        "エラー",
        "error",
    )

    if any(word in title for word in maintenance_words):
        return "🔧 メンテナンス", 0x3498DB

    if any(word in title for word in trouble_words):
        return "⚠️ 障害・不具合", 0xE74C3C

    return "📢 お知らせ", 0x9B59B6


def post_discord(item):
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL が設定されていません")

    category, color = classify(item)

    fields = [
        {
            "name": "🔗 詳細",
            "value": f"[AION2公式サイトで見る]({item['url']})",
            "inline": False,
        }
    ]

    if item.get("posted_at"):
        fields.append(
            {
                "name": "掲載日時",
                "value": str(item["posted_at"]),
                "inline": False,
            }
        )

    payload = {
        "username": "AION2リマイン",
        "embeds": [
            {
                "title": item["title"],
                "url": item["url"],
                "description": f"### {category}\nAION2公式サイトに新しい情報が掲載されました。",
                "color": color,
                "fields": fields,
                "footer": {"text": "AION2 公式ニュース"},
            }
        ],
    }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "AION2-Remind/1.0",
            "Accept": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        if response.status not in (200, 204):
            raise RuntimeError(f"Discord投稿失敗: HTTP {response.status}")


def send_test():
    item = {
        "id": "test",
        "article_id": "test",
        "board": "notice",
        "title": "AION2リマイン 動作テスト",
        "url": "https://aion2.plaync.com/ja-jp/board/notice/list",
        "posted_at": "",
    }
    post_discord(item)
    print("Discordテスト投稿に成功しました")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    if args.test:
        send_test()
        return

    all_items = []

    for board in BOARDS:
        try:
            all_items.extend(fetch_board(board))
        except Exception as exc:
            print(f"{board['key']} の取得に失敗: {exc}", file=sys.stderr)
            raise

    # API/listは新しい順。重複を除去して現在値を作る。
    unique = {}
    for item in all_items:
        unique[item["id"]] = item

    items = list(unique.values())
    current_ids = [item["id"] for item in items]
    seen = load_seen()

    if not seen:
        print("初回実行です。現在の記事を既読として登録します。")
        save_seen(current_ids)
        return

    new_items = [item for item in items if item["id"] not in seen]

    if not new_items:
        print("新着情報はありません")
        save_seen(list(dict.fromkeys(seen + current_ids)))
        return

    print(f"新着情報: {len(new_items)} 件")

    # 各ボードの取得順は新しい順なので、Discordには古いものから順番に流す。
    for item in reversed(new_items):
        print(f"Discordへ投稿: {item['title']}")
        post_discord(item)

    save_seen(list(dict.fromkeys(seen + current_ids)))


if __name__ == "__main__":
    main()
