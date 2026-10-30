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
# 天界デッキランキング(上位の人が天界で使っているデッキ)
DECKS_FILE = ROOT / "data" / "decks.json"
DECK_DAYS = int(os.environ.get("DECK_DAYS", "7"))      # これより古い試合は数えない
DECK_TOP = int(os.environ.get("DECK_TOP", "400"))      # 1つのランキングに載せるデッキ数(カードで絞り込めるよう多めに)
TENKAI_LEAGUE = 7                                      # 天界のリーグ番号(対戦ログの leagueNumber)
# 使い手ランキング(全カード)。上位の人それぞれが、どのカードを直近の試合の何割で使っているか
USERS_FILE = ROOT / "data" / "users.json"
USER_DECKS = 2                                         # 1人につき残すデッキの数(よく使う順)
# 一度20勝した人の記録。対戦ログは直近25試合しか見えないので、見つけたら残しておく
ACHIEVERS_FILE = ROOT / "data" / "achievers.json"

# ---- 20勝チャレンジ ----
# 対戦ログのイベント識別子(eventTag)と目標の勝利数。CR2 の collect.py と同じ値。
# 来年のチャレンジが始まったら、ここを新しい年の値に書き換える。
CHALLENGE = {"id": "crl20-2026", "label": "20勝達成", "eventTag": "#2CJUG2VL", "wins": 20}

API_BASE = "https://proxy.royaleapi.dev/v1"


RETRIES = 3        # 一時的な失敗(混雑・通信エラー)は数回やり直す
TIMEOUT = 15       # 1回の通信を待つ秒数

# 1回の集計にかけてよい時間(秒)。GitHub Actions は45分で打ち切るので、
# 中継サーバーが重い日でも保存まで辿り着けるよう、これを過ぎたら
# 残りの人は新しく調べずに前回の判定を使う。
TIME_BUDGET = float(os.environ.get("SPARKY_TIME_BUDGET", str(35 * 60)))
STARTED = time.time()


def over_budget() -> bool:
    return time.time() - STARTED > TIME_BUDGET
RETRY_WAIT = 3     # やり直すまでの秒数(回数に応じて伸ばす)


def api_get(path: str, token: str):
    """APIを叩いてJSONを返す。取得できなければ None。

    中継サーバーはたまに 429 / 5xx / タイムアウトを返す。
    1回の失敗で諦めると、その地域や人がランキングから消えるので、少し待ってやり直す。
    """
    for attempt in range(1, RETRIES + 1):
        result, retry = _api_get_once(path, token)
        if not retry:
            return result
        if attempt < RETRIES:
            time.sleep(RETRY_WAIT * attempt)
    print(f"  諦めました: {path}", file=sys.stderr)
    return None


def _api_get_once(path: str, token: str):
    """1回だけ叩く。(結果, やり直すべきか) を返す。"""
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
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return json.loads(res.read().decode("utf-8")), False
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
            return None, False
        if e.code == 429:
            print(f"  失敗: レート制限 ({path})", file=sys.stderr)
            return None, True
        print(f"  失敗: APIエラー {e.code} ({path})", file=sys.stderr)
        return None, e.code >= 500
    except urllib.error.URLError as e:
        print(f"  失敗: 通信エラー {e.reason} ({path})", file=sys.stderr)
        return None, True
    except (TimeoutError, ConnectionError, OSError) as e:
        # 読み込み途中のタイムアウトは URLError にならず素で飛んでくる。
        # 前はここで丸ごと落ちていた
        print(f"  失敗: 通信エラー {type(e).__name__} ({path})", file=sys.stderr)
        return None, True
    except json.JSONDecodeError:
        print(f"  失敗: 返答がJSONではありません ({path})", file=sys.stderr)
        return None, True


def save_json(path: Path, data):
    """JSONを書き出す。スマホで読み込むので、字下げを入れずに詰めて書く。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
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


def challenge_wins(battles, tag):
    """20勝チャレンジで到達した勝利数。startingTrophies が それまでの勝ち数、
    trophyChange が今回の増分になっている。"""
    best = 0
    for b in battles:
        if b.get("eventTag") != CHALLENGE["eventTag"]:
            continue
        me = my_side(b, tag)
        start = me.get("startingTrophies")
        if not isinstance(start, int):
            continue
        change = me.get("trophyChange") if isinstance(me.get("trophyChange"), int) else 0
        best = max(best, start + max(change, 0))
    return best


def parse_time(raw):
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def deck_of(side):
    """デッキを [[カードID, 進化/ヒーロー], ...](並び順のまま) と、比べるための鍵にする。
    左3枠の evolutionLevel は 1 が進化、2 がヒーロー。同じ8枚でも進化の付け方が違えば別のデッキ。"""
    cards = []
    for i, c in enumerate(side.get("cards") or []):
        if not isinstance(c.get("id"), int):
            return None, None
        ev = c.get("evolutionLevel") if i < 3 and isinstance(c.get("evolutionLevel"), int) else 0
        cards.append([c["id"], ev])
    if len(cards) != 8:
        return None, None
    key = ",".join(f"{cid}.{ev}" for cid, ev in sorted(cards))
    return cards, key


def tenkai_decks(battles, tag, card_book):
    """天界の試合で使ったデッキ。[{k, c, t, w}, ...]"""
    since = datetime.now(timezone.utc).timestamp() - DECK_DAYS * 86400
    out = []
    for b in battles:
        if b.get("type") != "pathOfLegend" or b.get("leagueNumber") != TENKAI_LEAGUE:
            continue
        when = parse_time(b.get("battleTime"))
        if when is None or when.timestamp() < since:
            continue
        me = my_side(b, tag)
        cards, key = deck_of(me)
        if not key:
            continue
        for c in (me.get("cards") or []) + (me.get("supportCards") or []):
            if isinstance(c.get("id"), int) and c["id"] not in card_book:
                card_book[c["id"]] = {"n": c.get("name"), "e": c.get("elixirCost"), "r": c.get("rarity"), "i": c.get("iconUrls") or {}}
        tower = next((c.get("id") for c in me.get("supportCards") or [] if isinstance(c.get("id"), int)), None)
        out.append({"k": key, "c": cards, "t": tower, "w": battle_result(b, tag)})
    return out


CARD_BOOK = {}      # カードの名前と絵(デッキランキング用)
ALL_CARDS = set()   # ゲームにある全カードのID(/cards から)


def load_all_cards(token):
    """ゲームにある全カード(タワーカードも)を /cards から読む。レアリティ・コスト・絵も入っている。"""
    data = api_get("/cards", token) or {}
    for c in (data.get("items") or []) + (data.get("supportItems") or []):
        if not isinstance(c.get("id"), int):
            continue
        ALL_CARDS.add(c["id"])
        CARD_BOOK[c["id"]] = {"n": c.get("name"), "e": c.get("elixirCost"), "r": c.get("rarity"),
                              "i": c.get("iconUrls") or {}}
    print(f"  全カード: {len(data.get('items') or [])}枚 + タワー{len(data.get('supportItems') or [])}種類")


def card_usage(battles, tag):
    """直近の対戦で、カードごとに何試合使ったか。使い手ランキング(全カード)用。

    q: {カードID: [使った試合, 勝ち, 勝敗が付いた試合, 進化/ヒーローで使った試合]}
       ただし THRESHOLD(既定で半分)以上の試合で使ったカードだけ
    d: よく使うデッキ(上位 USER_DECKS 個)
    """
    per, decks, total = {}, {}, 0
    for b in battles:
        me = my_side(b, tag)
        cards, key = deck_of(me)
        if not key:
            continue
        total += 1
        for c in (me.get("cards") or []) + (me.get("supportCards") or []):
            if isinstance(c.get("id"), int) and c["id"] not in CARD_BOOK:
                CARD_BOOK[c["id"]] = {"n": c.get("name"), "e": c.get("elixirCost"), "r": c.get("rarity"), "i": c.get("iconUrls") or {}}
        tower = next((c.get("id") for c in me.get("supportCards") or [] if isinstance(c.get("id"), int)), None)
        r = battle_result(b, tag)
        for cid, ev in cards + ([[tower, 0]] if tower else []):
            v = per.setdefault(cid, [0, 0, 0, 0])
            v[0] += 1
            if r is not None:
                v[2] += 1
                v[1] += 1 if r else 0
            if ev:
                v[3] += 1
        d = decks.setdefault(key, {"c": cards, "t": tower, "n": 0, "w": 0, "l": 0})
        d["n"] += 1
        if r is True:
            d["w"] += 1
        elif r is False:
            d["l"] += 1
    if not total:
        return None
    q = {str(cid): v for cid, v in per.items() if v[0] / total >= THRESHOLD}
    top = sorted(decks.values(), key=lambda d: d["n"], reverse=True)[:USER_DECKS]
    return {"n": total, "q": q, "d": top}


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
        "challengeWins": challenge_wins(battles, tag),
        "tenkai": tenkai_decks(battles, tag, CARD_BOOK),
        "usage": card_usage(battles, tag),
    }


def scan_board(board, token, cache, previous=None):
    """リーダーボード1つ分を調べて、該当者のリストを返す。

    対戦ログが取れなかった人は、前回の集計で該当していればその結果を使う
    (通信の失敗でランキングから消えないように)。
    """
    previous = previous or {}
    skipped = 0
    found = []
    started = time.time()
    fresh = 0

    for i, entry in enumerate(board, 1):
        tag = str(entry.get("tag", "")).lstrip("#").upper()
        if not tag:
            continue

        if tag in cache:
            stats = cache[tag]
        elif over_budget():
            # 時間切れ。新しくは調べず、前回の判定があればそれを使う
            stats = previous.get(tag)
            skipped += 1
        else:
            stats = analyze(tag, token)
            if stats is None and tag in previous:
                stats = previous[tag]
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

    note = f" / 時間切れで前回の判定を使ったのは{skipped}人" if skipped else ""
    print(f"  → {len(board)}人中 {len(found)}人が該当 (新規に調べたのは{fresh}人{note})")
    return found


def deck_ranking(entries, cache):
    """上位の人たちの天界の試合から、使われているデッキを数える。"""
    decks = {}
    cards = {}           # カード別: 使った人・試合・勝敗・進化/ヒーローで使った人
    players = battles = 0
    for e in entries:
        tag = str(e.get("tag", "")).lstrip("#").upper()
        used = (cache.get(tag) or {}).get("tenkai")
        if not used:
            continue
        players += 1
        for u in used:
            battles += 1
            d = decks.setdefault(u["k"], {"cards": u["c"], "towers": {}, "battles": 0, "wins": 0,
                                          "losses": 0, "players": set()})
            d["battles"] += 1
            d["players"].add(tag)
            if u["t"]:
                d["towers"][u["t"]] = d["towers"].get(u["t"], 0) + 1
            if u["w"] is True:
                d["wins"] += 1
            elif u["w"] is False:
                d["losses"] += 1
            for cid, ev in u["c"]:
                c = cards.setdefault(cid, {"players": set(), "battles": 0, "wins": 0, "losses": 0,
                                           "evo": set(), "hero": set()})
                c["players"].add(tag)
                c["battles"] += 1
                if u["w"] is True:
                    c["wins"] += 1
                elif u["w"] is False:
                    c["losses"] += 1
                if ev == 1:
                    c["evo"].add(tag)
                elif ev == 2:
                    c["hero"].add(tag)
    card_list = sorted(
        ({"id": cid, "players": len(c["players"]), "battles": c["battles"], "wins": c["wins"], "losses": c["losses"],
          "evo": len(c["evo"]), "hero": len(c["hero"])} for cid, c in cards.items()),
        key=lambda c: (c["players"], c["battles"]), reverse=True)
    # 使っている人の多い順(1人が何試合も使っても1人)。同じなら試合数の多い順
    ranked = sorted(decks.values(), key=lambda d: (len(d["players"]), d["battles"]), reverse=True)[:DECK_TOP]
    out = []
    for d in ranked:
        tower = max(d["towers"].items(), key=lambda x: x[1])[0] if d["towers"] else None
        out.append({"cards": d["cards"], "tower": tower, "battles": d["battles"], "players": len(d["players"]),
                    "wins": d["wins"], "losses": d["losses"]})
    return {"players": players, "battles": battles, "decks": out, "cards": card_list}


def save_users(boards, cache, achieved):
    """使い手ランキング(全カード)を書き出す。日本・世界の上位の人を1人1回ずつ。
    どのカードの使い手かは q に入っている(カードごとのランキングは画面で作る)。"""
    try:
        old = {p["tag"]: p for p in json.loads(USERS_FILE.read_text(encoding="utf-8")).get("players") or []}
    except Exception:
        old = {}
    players = {}
    regions = []
    for key, label in (("jp", "日本"), ("global", "世界")):
        board = boards.get(key) or []
        if not board:
            continue
        regions.append({"key": key, "label": label, "scanned": len(board)})
        for i, e in enumerate(board, 1):
            tag = str(e.get("tag", "")).lstrip("#").upper()
            if not tag:
                continue
            rating, _ = entry_rating(e)
            p = players.get(tag)
            if p is None:
                usage = (cache.get(tag) or {}).get("usage")
                if usage is None and tag in old:          # 時間切れなどで今回見られなかった人は前回のまま
                    usage = {k: old[tag].get(k) for k in ("n", "q", "d")}
                if not usage:
                    continue
                p = players[tag] = {"tag": tag, "name": e.get("name"), "clan": (e.get("clan") or {}).get("name"),
                                    "r": {}, **usage, "a": 1 if tag in achieved else 0}
            p["r"][key] = [e.get("rank") if isinstance(e.get("rank"), int) else i, rating]
    out = list(players.values())
    used = {int(k) for p in out for k in p["q"]} | {cid for p in out for d in p["d"] for cid, _ in d["c"]}
    used |= {d["t"] for p in out for d in p["d"] if d.get("t")}
    # 全カード(誰も使っていないカードも、絞り込みや一覧に出せるように)
    cards = {str(cid): CARD_BOOK[cid] for cid in set(used) | set(ALL_CARDS) if cid in CARD_BOOK}
    try:
        for k, v in (json.loads(USERS_FILE.read_text(encoding="utf-8")).get("cards") or {}).items():
            cards.setdefault(k, v)
    except Exception:
        pass
    save_json(USERS_FILE, {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "threshold": THRESHOLD,
        "regions": regions,
        "cards": cards,
        "players": out,
    })
    print(f"  使い手ランキング: {len(out)}人 / カード{len({k for p in out for k in p['q']})}種類")


def save_decks(boards, cache):
    """天界デッキランキングを書き出す。世界TOP100 / 世界TOP1000 / 日本TOP1000"""
    rank_of = lambda e, i: e.get("rank") if isinstance(e.get("rank"), int) else i
    world = boards.get("global") or []
    defs = [
        ("world100", "世界TOP100", [e for i, e in enumerate(world, 1) if rank_of(e, i) <= 100]),
        ("world1000", "世界TOP1000", world),
        ("jp1000", "日本TOP1000", boards.get("jp") or []),
    ]
    try:
        old = {b.get("key"): b for b in json.loads(DECKS_FILE.read_text(encoding="utf-8")).get("boards") or []}
    except Exception:
        old = {}
    out = []
    for key, label, entries in defs:
        r = deck_ranking(entries, cache)
        if not r["battles"] and key in old:
            out.append({**old[key], "stale": True})      # 取れなかったときは前回のまま
            print(f"  デッキ: {label} は前回の結果を使います")
            continue
        out.append({"key": key, "label": label, **r})
        print(f"  デッキ: {label} {r['players']}人 / 天界の試合 {r['battles']} / デッキ {len(r['decks'])}種類を保存")
    used = {cid for b in out for d in b.get("decks") or [] for cid, _ in d["cards"]}
    used |= {c["id"] for b in out for c in b.get("cards") or []}
    used |= {d.get("tower") for b in out for d in b.get("decks") or [] if d.get("tower")}
    # 全カード(誰も使っていないカードも、絞り込みや一覧に出せるように)
    cards = {str(cid): CARD_BOOK[cid] for cid in set(used) | set(ALL_CARDS) if cid in CARD_BOOK}
    # 前回のカード情報も残す(前回のままのランキングが参照しているかもしれない)
    try:
        for k, v in (json.loads(DECKS_FILE.read_text(encoding="utf-8")).get("cards") or {}).items():
            cards.setdefault(k, v)
    except Exception:
        pass
    save_json(DECKS_FILE, {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "days": DECK_DAYS,
        "cards": cards,
        "boards": out,
    })


def main():
    token = os.environ.get("CR_API_TOKEN", "").strip()
    if not token:
        raise SystemExit("CR_API_TOKEN を設定してください")

    print(f"対象カード: {TARGET_CARD} / 判定: 直近の{THRESHOLD:.0%}以上で使用")

    load_all_cards(token)
    jp_id = find_location(token)
    if jp_id is None:
        print("  警告: 日本のロケーションIDが見つかりません。世界だけ集計します")

    # 日本を先にやる。世界と重なった人は使い回せる
    targets = []
    if jp_id is not None:
        targets.append(("jp", "日本", jp_id))
    targets.append(("global", "世界", "global"))

    # 前回の集計結果。取れなかった地域や人の穴埋めに使う
    old = {}
    try:
        old = json.loads(OUT_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    old_regions = {r.get("key"): r for r in old.get("regions") or []}
    stat_keys = ("battles", "targetBattles", "usageRate", "wins", "winRate", "deck", "supportCards")
    previous = {}
    for r in old_regions.values():
        for p in r.get("players") or []:
            tag = str(p.get("tag", "")).lstrip("#").upper()
            if tag:
                previous[tag] = {**{k: p.get(k) for k in stat_keys}, "qualified": True}

    cache = {}
    regions = []
    boards = {}          # デッキランキング用に、地域ごとの上位の並びを残す
    started = time.time()

    for key, label, loc in targets:
        print(f"\n===== {label} =====")
        board, source = fetch_leaderboard(loc, token, SCAN_LIMIT)
        if not board:
            if key in old_regions:
                # 一時的に取れなかっただけのことが多いので、前回の結果を残す
                print(f"  リーダーボードが取れませんでした。{label}は前回の結果を表示します")
                regions.append({**old_regions[key], "stale": True})
            else:
                print(f"  リーダーボードが空でした。{label}はスキップします")
            continue
        board = board[:SCAN_LIMIT]
        boards[key] = board
        players = scan_board(board, token, cache, previous)
        regions.append(
            {
                "key": key,
                "label": label,
                "scanned": len(board),
                "source": source,
                "players": players,
            }
        )

    if regions and all(r.get("stale") for r in regions):
        raise SystemExit("どの地域も取得できませんでした。前回の結果をそのまま残します")
    if not regions:
        raise SystemExit(
            "リーダーボードが取得できませんでした。"
            "シーズン切り替え中か、エンドポイントの仕様が変わった可能性があります"
        )

    # --- 20勝チャレンジの達成者 ---
    # 今回の対戦ログで20勝に届いていた人を記録に足し、ランキングの各人に印を付ける
    try:
        achievers = json.loads(ACHIEVERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        achievers = {}
    done = achievers.setdefault(CHALLENGE["id"], {})
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for tag, stats in cache.items():
        if stats and (stats.get("challengeWins") or 0) >= CHALLENGE["wins"] and tag not in done:
            done[tag] = now_iso
    for r in regions:
        for p in r.get("players") or []:
            tag = str(p.get("tag", "")).lstrip("#").upper()
            p.pop("challengeWins", None)
            p.pop("tenkai", None)
            p.pop("usage", None)
            p["achievements"] = [CHALLENGE["label"]] if tag in done else []
    save_json(ACHIEVERS_FILE, achievers)
    print(f"  {CHALLENGE['label']}: 記録済み {len(done)}人")

    save_json(
        OUT_FILE,
        {
            "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "card": TARGET_CARD,
            "threshold": THRESHOLD,
            "regions": regions,
        },
    )

    save_decks(boards, cache)
    save_users(boards, cache, done)

    total = sum(len(r["players"]) for r in regions)
    print(
        f"\n完了: {len(regions)}地域 / 該当のべ{total}人 "
        f"/ 対戦ログを見たのは{len(cache)}人 ({(time.time()-started)/60:.1f}分)"
    )


if __name__ == "__main__":
    main()
