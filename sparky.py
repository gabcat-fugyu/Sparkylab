"""リーダーボードから「スパーキー使い」を抜き出してランキングにする。

日本と世界(グローバル)の2つを集計する。

やっていること:
  1. /locations から日本のロケーションIDを探す(世界は "global" 固定)
  2. それぞれの上位プレイヤー(既定1000人)を取得
  3. 1人ずつ対戦ログを見て、直近の試合のうち何割でスパーキーを使ったか数える
  4. 半分以上で使っていた人だけを残し、レートの高い順に並べて保存

日本の上位は世界の上位にも入っているので、対戦ログの結果はタグ単位で
使い回してAPIの呼び出し回数を節約する。

このファイル1つで完結する(他のスクリプトに依存しない)。

必要な環境変数:
    CR_API_TOKEN  developer.clashroyale.com で発行したトークン
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# 対象のカード。名前で判定する
TARGET_CARD = os.environ.get("TARGET_CARD", "Sparky")

# 各リーダーボードで何人まで調べるか
SCAN_LIMIT = int(os.environ.get("SPARKY_SCAN", "1000"))

# 「使い」と認める下限。0.5 なら直近の半分以上で使っていれば該当
THRESHOLD = float(os.environ.get("SPARKY_THRESHOLD", "0.5"))

# APIを叩く間隔(秒)。短くすると速いがレート制限に当たりやすい
DELAY = float(os.environ.get("SPARKY_DELAY", "0.25"))

ROOT = Path(__file__).parent
OUT_FILE = ROOT / "data" / "sparky.json"

API_BASE = "https://proxy.royaleapi.dev/v1"


def api_get(path: str, token: str):
    """APIを叩いてJSONを返す。取得できなければ None。"""
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            # RoyaleAPIの中継サーバーはCloudflareの背後にあり、Pythonの
            # デフォルトUser-Agent だと bot 扱いで弾かれる
            # (error_1010 / browser_signature_banned)。
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        if e.code == 403:
            print("  --- 403の詳細 ---")
            print(f"  リクエストURL: {API_BASE}{path}")
            print(f"  APIが返した中身: {body[:500]}")
            print("  ------------------")
            raise SystemExit(
                "403: トークン/IP設定、またはCloudflareのbot判定が原因の可能性があります"
            )
        if e.code == 404:
            return None
        if e.code == 429:
            print("  スキップ: レート制限に達しました", file=sys.stderr)
            return None
        print(f"  スキップ: APIエラー {e.code} ({path})", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  スキップ: 通信エラー {e.reason} ({path})", file=sys.stderr)
        return None


def save_json(path: Path, data):
    """JSONを整形して書き出す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def battle_result(battle: dict, tag: str):
    """その試合が勝ちかどうかを返す。判定できなければ None。"""
    team = battle.get("team") or []
    opponent = battle.get("opponent") or []
    if not team or not opponent:
        return None
    me = next(
        (p for p in team if str(p.get("tag", "")).lstrip("#").upper() == tag), team[0]
    )
    my_crowns = me.get("crowns")
    opp_crowns = opponent[0].get("crowns")
    if my_crowns is None or opp_crowns is None:
        return None
    if my_crowns == opp_crowns:
        return None  # 引き分けは勝率の母数から外す
    return my_crowns > opp_crowns



def norm(s) -> str:
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


TARGET_KEY = norm(TARGET_CARD)


def find_location(token, country_code="JP"):
    """国コードからロケーションIDを探す。見つからなければ None。"""
    data = api_get("/locations?limit=1000", token)
    for item in (data or {}).get("items", []):
        if item.get("isCountry") and item.get("countryCode") == country_code:
            print(f"  {item.get('name')} = id {item.get('id')}")
            return item.get("id")
    return None


def fetch_leaderboard(loc_id, token, limit):
    """上位プレイヤーを取得する。

    ランキングのエンドポイントはゲームの仕様変更で変わってきているので、
    候補を順に試して最初に返ってきたものを使う。
    """
    candidates = [
        f"/locations/{loc_id}/pathoflegend/players?limit={limit}",
        f"/locations/{loc_id}/rankings/players?limit={limit}",
    ]
    for path in candidates:
        data = api_get(path, token)
        items = (data or {}).get("items") or []
        if items:
            print(f"  {path.split('?')[0]} から {len(items)}人")
            return items, path.split("?")[0]
    return [], None


def entry_rating(entry):
    """リーダーボードの項目からレート相当の数値を取り出す。"""
    for key in ("eloRating", "rating", "trophies"):
        v = entry.get(key)
        if isinstance(v, int):
            return v, key
    return None, None


def my_side(battle, tag):
    """その試合の自分側の情報を返す。"""
    team = battle.get("team") or []
    return next(
        (p for p in team if str(p.get("tag", "")).lstrip("#").upper() == tag),
        team[0] if team else {},
    )


def uses_target(battle, tag) -> bool:
    cards = my_side(battle, tag).get("cards") or []
    return any(norm(c.get("name")) == TARGET_KEY for c in cards)


def analyze(tag, token):
    """対戦ログを見て使用状況をまとめる。判定できなければ None。

    リーダーボードごとに呼び直さないよう、結果は呼び出し側でタグ単位に
    キャッシュする。
    """
    battles = api_get(f"/players/%23{tag}/battlelog", token)
    if not isinstance(battles, list) or not battles:
        return None

    total = len(battles)
    hit = wins = decided = 0
    deck = support = None

    for b in battles:
        if not uses_target(b, tag):
            continue
        hit += 1
        if deck is None:
            me = my_side(b, tag)
            deck = me.get("cards") or []
            support = me.get("supportCards") or []
        r = battle_result(b, tag)
        if r is None:
            continue
        decided += 1
        if r:
            wins += 1

    return {
        "battles": total,
        "targetBattles": hit,
        "usageRate": round(hit / total * 100, 1) if total else 0,
        "wins": wins,
        "winRate": round(wins / decided * 100, 1) if decided else None,
        "deck": deck or [],
        "supportCards": support or [],
        "qualified": total > 0 and (hit / total) >= THRESHOLD,
    }


def scan_board(board, token, cache):
    """リーダーボード1つ分を調べて、該当者のリストを返す。"""
    found = []
    started = time.time()
    fresh = 0

    for i, entry in enumerate(board, 1):
        tag = str(entry.get("tag", "")).lstrip("#").upper()
        if not tag:
            continue

        if tag in cache:
            stats = cache[tag]
        else:
            stats = analyze(tag, token)
            cache[tag] = stats
            fresh += 1
            if i < len(board):
                time.sleep(DELAY)

        if not stats or not stats["qualified"]:
            continue

        rating, rating_key = entry_rating(entry)
        found.append(
            {
                "tag": entry.get("tag"),
                "name": entry.get("name"),
                "clan": (entry.get("clan") or {}).get("name"),
                "boardRank": entry.get("rank"),
                "rating": rating,
                "ratingFrom": rating_key,
                **{k: v for k, v in stats.items() if k != "qualified"},
            }
        )
        print(f"    [{i}/{len(board)}] {entry.get('name')}: "
              f"使用率{stats['usageRate']}% / レート{rating}")

        if i % 200 == 0:
            print(f"    --- {i}人完了 ({(time.time()-started)/60:.1f}分 / 該当{len(found)}人) ---")

    # レートの高い順。レート不明の人は末尾へ
    found.sort(key=lambda p: p["rating"] if p["rating"] is not None else -1, reverse=True)
    for i, p in enumerate(found, 1):
        p["rank"] = i

    print(f"  → {len(board)}人中 {len(found)}人が該当 (新規に調べたのは{fresh}人)")
    return found


def main():
    token = os.environ.get("CR_API_TOKEN", "").strip()
    if not token:
        raise SystemExit("CR_API_TOKEN を設定してください")

    print(f"対象カード: {TARGET_CARD} / 判定: 直近の{THRESHOLD:.0%}以上で使用")

    jp_id = find_location(token)
    if jp_id is None:
        print("  警告: 日本のロケーションIDが見つかりません。世界だけ集計します")

    # 日本を先にやる。世界と重なった人は使い回せる
    targets = []
    if jp_id is not None:
        targets.append(("jp", "日本", jp_id))
    targets.append(("global", "世界", "global"))

    cache = {}
    regions = []
    started = time.time()

    for key, label, loc in targets:
        print(f"\n===== {label} =====")
        board, source = fetch_leaderboard(loc, token, SCAN_LIMIT)
        if not board:
            print(f"  リーダーボードが空でした。{label}はスキップします")
            continue
        board = board[:SCAN_LIMIT]
        players = scan_board(board, token, cache)
        regions.append(
            {
                "key": key,
                "label": label,
                "scanned": len(board),
                "source": source,
                "players": players,
            }
        )

    if not regions:
        raise SystemExit(
            "リーダーボードが取得できませんでした。"
            "シーズン切り替え中か、エンドポイントの仕様が変わった可能性があります"
        )

    save_json(
        OUT_FILE,
        {
            "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "card": TARGET_CARD,
            "threshold": THRESHOLD,
            "regions": regions,
        },
    )

    total = sum(len(r["players"]) for r in regions)
    print(
        f"\n完了: {len(regions)}地域 / 該当のべ{total}人 "
        f"/ 対戦ログを見たのは{len(cache)}人 ({(time.time()-started)/60:.1f}分)"
    )


if __name__ == "__main__":
    main()
