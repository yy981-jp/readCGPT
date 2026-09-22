#!/usr/bin/env python3
"""会話ログビューア (import_conversations.py が作った SQLite DB を閲覧・検索する)

使い方:
    python viewer.py output.db
    python viewer.py output.db --port 8765 --no-browser

標準ライブラリだけで動きます。127.0.0.1 でのみ待ち受けます。
"""
import argparse
import json
import re
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

INDEX_NAME = "idx_messages_conv"
MAX_TERMS = 8


# ---------------------------------------------------------------- helpers
def to_epoch(v):
    """create_time が REAL / 数値文字列 / ISO文字列 のどれでも epoch 秒(float)にする"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    else:
        try:
            f = float(v)
        except ValueError:
            try:
                f = datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    return f / 1000.0 if f > 1e11 else f


def split_terms(q):
    seen, out = set(), []
    for t in re.split(r"\s+", (q or "").strip()):
        if t and t.casefold() not in seen:
            seen.add(t.casefold())
            out.append(t)
    return out[:MAX_TERMS]


def like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def ascii_lower(s):
    # SQLite の lower() / LIKE は ASCII のみ大小無視するので、それに合わせる
    return "".join(ch.lower() if ch < "\x80" else ch for ch in s)


def role_clause(roles):
    parts = []
    if "user" in roles:
        parts.append("role='user'")
    if "assistant" in roles:
        parts.append("role='assistant'")
    if "other" in roles:
        parts.append("role NOT IN ('user','assistant')")
    if not parts:
        return None
    return "(" + " OR ".join(parts) + ")"


# ---------------------------------------------------------------- store
class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise SystemExit(f"DBファイルが見つかりません: {self.path}")
        self.lock = threading.Lock()
        self.mtime = None
        self.convs = []
        self.by_id = {}
        self.total_messages = 0
        self.ensure_index()
        self.reload()

    def connect(self, rw=False):
        uri = self.path.as_uri() + ("" if rw else "?mode=ro")
        con = sqlite3.connect(uri, uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def ensure_index(self):
        """検索・会話表示を速くするインデックスを(無ければ)追加する。失敗しても続行。"""
        try:
            con = self.connect()
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (INDEX_NAME,)
            ).fetchone()
            con.close()
            if exists:
                return
            print("インデックスを作成しています(初回のみ)...", flush=True)
            con = self.connect(rw=True)
            con.execute(
                f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
                "ON messages(conversation_id, is_mainline, mainline_order)"
            )
            con.commit()
            con.close()
        except sqlite3.Error as e:
            print(f"(インデックス作成はスキップしました: {e})", file=sys.stderr)

    def reload(self):
        con = self.connect()
        try:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(conversations)")}
            extra = [c for c in ("is_starred", "is_archived") if c in cols]
            counts = {
                r[0]: r[1]
                for r in con.execute(
                    "SELECT conversation_id, COUNT(*) FROM messages "
                    "WHERE has_message=1 AND is_mainline=1 AND role IN ('user','assistant') "
                    "AND content_text<>'' GROUP BY conversation_id"
                )
            }
            sel = ", ".join(["conversation_id", "title", "create_time", "update_time"] + extra)
            convs = []
            for r in con.execute(f"SELECT {sel} FROM conversations"):
                convs.append(
                    {
                        "id": r["conversation_id"],
                        "title": r["title"] or "",
                        "ct": to_epoch(r["create_time"]),
                        "ut": to_epoch(r["update_time"]),
                        "n": counts.get(r["conversation_id"], 0),
                        "star": bool(r["is_starred"]) if "is_starred" in extra else False,
                        "arch": bool(r["is_archived"]) if "is_archived" in extra else False,
                    }
                )
            total = sum(counts.values())
        finally:
            con.close()
        with self.lock:
            self.convs = convs
            self.by_id = {c["id"]: c for c in convs}
            self.total_messages = total
            self.mtime = self.path.stat().st_mtime

    def refresh_if_changed(self):
        try:
            if self.path.stat().st_mtime != self.mtime:
                self.reload()
        except (OSError, sqlite3.Error):
            pass

    # ------------------------------------------------------------ list / search
    def content_hits(self, con, terms, roles):
        rc = role_clause(roles)
        if not terms or rc is None:
            return {}
        conds = ["has_message=1", rc]
        params = []
        for t in terms:
            conds.append("content_text LIKE ? ESCAPE '\\'")
            params.append("%" + like_escape(t) + "%")
        sql = (
            "SELECT conversation_id, COUNT(*), MIN(rowid) FROM messages WHERE "
            + " AND ".join(conds)
            + " GROUP BY conversation_id"
        )
        return {r[0]: (r[1], r[2]) for r in con.execute(sql, params)}

    def snippets(self, con, rowids, term):
        if not rowids:
            return {}
        ids = ",".join(str(int(x)) for x in rowids)  # 整数のみ埋め込む(変数上限回避)
        sql = (
            "SELECT rowid, node_id, is_mainline, instr(lower(content_text), ?), "
            "substr(content_text, MAX(1, instr(lower(content_text), ?) - 30), 110) "
            f"FROM messages WHERE rowid IN ({ids})"
        )
        lt = ascii_lower(term)
        out = {}
        for r in con.execute(sql, (lt, lt)):
            pos = r[3] or 1
            text = re.sub(r"\s+", " ", r[4] or "").strip()
            if pos > 31:
                text = "…" + text
            out[r[0]] = {"node": r[1], "main": bool(r[2]), "text": text}
        return out

    def list_conversations(self, q, roles, sort, order):
        self.refresh_if_changed()
        with self.lock:
            convs = list(self.convs)
        terms = split_terms(q)
        hits, snip = {}, {}
        if terms:
            folded = [t.casefold() for t in terms]
            title_hit = {c["id"] for c in convs if all(t in c["title"].casefold() for t in folded)}
            con = self.connect()
            try:
                hits = self.content_hits(con, terms, roles)
                snip = self.snippets(con, [h[1] for h in hits.values()], terms[0])
            finally:
                con.close()
            keep = title_hit | set(hits)
            convs = [c for c in convs if c["id"] in keep]
        else:
            title_hit = set()

        def key(c):
            if sort == "create":
                return (c["ct"] or 0, c["ut"] or 0)
            if sort == "count":
                return (c["n"], c["ut"] or 0)
            if sort == "hits":
                return (hits.get(c["id"], (0,))[0], c["ut"] or 0)
            if sort == "title":
                return (c["title"].casefold(),)
            return (c["ut"] or c["ct"] or 0, c["ct"] or 0)

        convs.sort(key=key, reverse=(order != "asc"))
        items = []
        for c in convs:
            it = dict(c)
            if terms:
                h = hits.get(c["id"])
                it["hits"] = h[0] if h else 0
                it["title_hit"] = c["id"] in title_hit
                s = snip.get(h[1]) if h else None
                if s:
                    it["snippet"] = s["text"]
                    it["first_node"] = s["node"]
                    it["first_main"] = s["main"]
            items.append(it)
        return {"terms": terms, "total": len(self.convs), "items": items}

    # ------------------------------------------------------------ one conversation
    def conversation(self, cid, branches):
        self.refresh_if_changed()
        meta = self.by_id.get(cid)
        if meta is None:
            return None
        con = self.connect()
        try:
            branch_count = con.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND has_message=1 "
                "AND is_mainline=0 AND content_text<>''",
                (cid,),
            ).fetchone()[0]
            cols = "node_id, parent_id, role, author_name, content_type, content_text, create_time, model_slug, is_mainline"
            if not branches:
                rows = con.execute(
                    f"SELECT {cols} FROM messages WHERE conversation_id=? AND has_message=1 "
                    "AND is_mainline=1 ORDER BY mainline_order",
                    (cid,),
                ).fetchall()
                ordered = [(r, False) for r in rows]
            else:
                rows = con.execute(
                    f"SELECT {cols}, has_message FROM messages WHERE conversation_id=? ORDER BY rowid",
                    (cid,),
                ).fetchall()
                ordered = self._tree_order(rows)
        finally:
            con.close()

        msgs = []
        for r, is_branch_start in ordered:
            text = r["content_text"]
            if text is None or not text.strip():
                continue
            msgs.append(
                {
                    "id": r["node_id"],
                    "role": r["role"] or "",
                    "name": r["author_name"] or "",
                    "type": r["content_type"] or "",
                    "text": text,
                    "t": to_epoch(r["create_time"]),
                    "model": r["model_slug"] or "",
                    "main": bool(r["is_mainline"]),
                    "bs": is_branch_start,
                }
            )
        return {
            "id": cid,
            "title": meta["title"],
            "ct": meta["ct"],
            "ut": meta["ut"],
            "branch_count": branch_count,
            "branches": bool(branches),
            "messages": msgs,
        }

    @staticmethod
    def _tree_order(rows):
        """分岐込みの表示順。各分岐点で『非メインライン(古い順) → メインライン』の順に深さ優先で辿る。"""
        nodes = {r["node_id"]: r for r in rows}
        children = {}
        roots = []
        for r in rows:
            p = r["parent_id"]
            if p is not None and p in nodes:
                children.setdefault(p, []).append(r["node_id"])
            else:
                roots.append(r["node_id"])

        def ordered_children(ids):
            side = [i for i in ids if not nodes[i]["is_mainline"]]
            main = [i for i in ids if nodes[i]["is_mainline"]]
            side.sort(key=lambda i: to_epoch(nodes[i]["create_time"]) or 0)
            return side + main

        out = []
        stack = list(reversed(ordered_children(roots)))
        while stack:
            nid = stack.pop()
            r = nodes[nid]
            if r["has_message"]:
                p = r["parent_id"]
                start = (not r["is_mainline"]) and p in children and len(children[p]) >= 2
                out.append((r, start))
            stack.extend(reversed(ordered_children(children.get(nid, []))))
        return out


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    store = None
    server_version = "ChatLogViewer/1.0"

    def log_message(self, fmt, *args):  # 静かに
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        try:
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            arg = lambda k, d="": qs.get(k, [d])[0]
            if u.path in ("/", "/index.html"):
                self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/api/conversations":
                roles = {x for x in arg("roles", "user,assistant").split(",") if x}
                res = self.store.list_conversations(arg("q"), roles, arg("sort", "update"), arg("order", "desc"))
                self._json(res)
            elif u.path == "/api/conversation":
                res = self.store.conversation(arg("id"), arg("branches") == "1")
                if res is None:
                    self._json({"error": "その会話は見つかりません"}, 404)
                else:
                    self._json(res)
            elif u.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description="会話ログビューア")
    ap.add_argument("db", help="import_conversations.py が作った SQLite DB")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    args = ap.parse_args()

    store = Store(args.db)
    Handler.store = store

    httpd = None
    for port in range(args.port, args.port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit(f"ポート {args.port} 以降が使用中です。--port で別の番号を指定してください。")

    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"{len(store.convs):,} 件の会話 / {store.total_messages:,} 件のメッセージ")
    print(f"ビューア: {url}   (終了は Ctrl+C)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")
    finally:
        httpd.server_close()


# ---------------------------------------------------------------- UI
INDEX_HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>会話ログビューア</title>
<style>
:root{
  --bg:#f3f5f6; --side:#e9edef; --paper:#fbfcfc; --ink:#1c2a32; --muted:#5f6f78; --line:#d5dce0;
  --accent:#23507b; --accent-soft:#e3ecf4; --user:#e8eff6; --mark:#ffe17a; --mark-cur:#ffb84d;
  --branch:#8656c2; --code-bg:#eef1f3; --code-ink:#1c2a32;
  --ui:"Segoe UI","Yu Gothic UI","Hiragino Kaku Gothic ProN","Hiragino Sans","Noto Sans JP",Meiryo,system-ui,sans-serif;
  --mono:"Cascadia Mono",Consolas,"SF Mono",Menlo,"Courier New",monospace;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#121619; --side:#181d20; --paper:#161a1d; --ink:#dde4e8; --muted:#8a99a2; --line:#293137;
    --accent:#86b4e3; --accent-soft:#1f2c38; --user:#1d2733; --mark:#6d5a10; --mark-cur:#9a6a12;
    --branch:#b592e6; --code-bg:#1e2428; --code-ink:#dde4e8;
  }
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{font-family:var(--ui);color:var(--ink);background:var(--bg);display:flex;font-size:14px;line-height:1.5}
button,select,input{font:inherit;color:inherit}
button{cursor:pointer}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
mark{background:var(--mark);color:inherit;border-radius:2px;padding:0 1px}
mark.cur{background:var(--mark-cur);outline:2px solid var(--mark-cur)}

/* sidebar */
.side{width:344px;flex:none;background:var(--side);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
.side-top{padding:14px 14px 10px;border-bottom:1px solid var(--line);display:grid;gap:10px}
.search{width:100%;padding:9px 12px;border:1px solid var(--line);border-radius:6px;background:var(--paper);font-size:15px}
.search:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 2px var(--accent-soft)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.chip{border:1px solid var(--line);background:transparent;border-radius:999px;padding:2px 11px;font-size:12.5px;color:var(--muted)}
.chip[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:var(--paper)}
.sel{border:1px solid var(--line);background:var(--paper);border-radius:6px;padding:3px 6px;font-size:12.5px}
.sq{border:1px solid var(--line);background:var(--paper);border-radius:6px;padding:3px 9px;font-size:12.5px}
.count{font-size:12.5px;color:var(--muted);margin-left:auto}
.list{list-style:none;margin:0;padding:0;overflow:auto;flex:1;min-height:0}
.item{display:block;padding:10px 14px 10px 12px;border-left:3px solid transparent;border-bottom:1px solid var(--line);color:inherit;text-decoration:none}
.item:hover{background:var(--accent-soft)}
.item.on{background:var(--paper);border-left-color:var(--accent)}
.item .t{display:block;font-weight:600;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;word-break:break-all}
.item .m{display:flex;gap:12px;font-size:12px;color:var(--muted);margin-top:3px;align-items:baseline}
.item .hit{color:var(--accent);font-weight:600}
.item .sn{font-size:12.5px;color:var(--muted);margin-top:4px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;word-break:break-all}
.none{padding:28px 18px;color:var(--muted)}

/* reader */
.reader{flex:1;min-width:0;overflow:auto;background:var(--paper);position:relative}
.empty{max-width:34em;margin:18vh auto 0;padding:0 24px;color:var(--muted);font-size:15px}
.empty b{display:block;color:var(--ink);font-size:18px;margin-bottom:6px}
.chead{position:sticky;top:0;z-index:5;background:var(--paper);border-bottom:1px solid var(--line);padding:14px 24px 10px}
.chead-in,.thread{max-width:940px;margin:0 auto}
.chead h1{font-size:19px;line-height:1.45;margin:0 0 4px;word-break:break-all}
.meta{display:flex;gap:18px;flex-wrap:wrap;font-size:12.5px;color:var(--muted)}
.tools{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center}
.btn{border:1px solid var(--line);background:transparent;border-radius:6px;padding:4px 11px;font-size:13px}
.btn:hover{background:var(--accent-soft)}
.btn[aria-pressed=true]{border-color:var(--branch);color:var(--branch);background:transparent;box-shadow:inset 0 0 0 1px var(--branch)}
.mnav{display:flex;gap:4px;align-items:center;margin-left:auto;font-size:12.5px;color:var(--muted)}
.mnav .btn{padding:2px 9px}
.back{display:none}

.msg{display:grid;grid-template-columns:104px minmax(0,1fr);gap:0 18px;padding:14px 24px;border-left:3px solid transparent}
.msg.user{background:var(--user);border-left-color:var(--accent)}
.msg.branch{border-left:3px dashed var(--branch)}
.msg.other .body{color:var(--muted);font-size:13px}
.gut{font-size:12px;color:var(--muted);display:flex;flex-direction:column;gap:1px;padding-top:2px;overflow-wrap:anywhere}
.gut b{color:var(--ink);font-weight:600;font-size:12.5px}
.tag{color:var(--branch);font-weight:600}
.body{font-size:15.5px;line-height:1.85;min-width:0;overflow-wrap:anywhere}
.body p{margin:.55em 0}
.body p:first-child,.body>*:first-child{margin-top:0}
.body>*:last-child{margin-bottom:0}
.body p.plain{white-space:pre-wrap}
.body .hd{font-weight:700;margin:1.1em 0 .3em;line-height:1.5}
.body .hd1{font-size:1.25em}.body .hd2{font-size:1.15em}.body .hd3{font-size:1.05em}
.body ul,.body ol{margin:.5em 0;padding-left:1.4em}
.body li{margin:.15em 0}
.body blockquote{margin:.6em 0;padding:.1em 0 .1em 1em;border-left:3px solid var(--line);color:var(--muted)}
.body hr{border:0;border-top:1px solid var(--line);margin:1em 0}
.body code{font-family:var(--mono);font-size:.88em;background:var(--code-bg);padding:.1em .35em;border-radius:4px}
.body a{color:var(--accent)}
.code{margin:.7em 0;border:1px solid var(--line);border-radius:6px;overflow:hidden;background:var(--code-bg)}
.code-h{display:flex;justify-content:space-between;align-items:center;padding:3px 10px;font-size:12px;color:var(--muted);border-bottom:1px solid var(--line)}
.code-h button{border:0;background:transparent;font-size:12px;color:var(--muted);padding:2px 6px;border-radius:4px}
.code-h button:hover{background:var(--accent-soft);color:var(--ink)}
.code pre{margin:0;padding:10px 12px;overflow:auto;line-height:1.6}
.code pre code{background:none;padding:0;font-size:13px;color:var(--code-ink);white-space:pre}
.tbl{overflow:auto;margin:.7em 0}
.tbl table{border-collapse:collapse;font-size:14px}
.tbl th,.tbl td{border:1px solid var(--line);padding:4px 10px;text-align:left}
.tbl th{background:var(--code-bg)}
.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--paper);padding:7px 16px;border-radius:6px;font-size:13px;opacity:0;pointer-events:none;transition:opacity .15s}
.toast.on{opacity:1}

@media (max-width:760px){
  .side{width:100%;border-right:0}
  body.reading .side{display:none}
  body:not(.reading) .reader{display:none}
  .back{display:inline-block}
  .msg{grid-template-columns:minmax(0,1fr);gap:4px;padding:12px 14px}
  .gut{flex-direction:row;gap:10px;flex-wrap:wrap}
  .chead{padding:10px 14px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<aside class="side" aria-label="会話一覧">
  <div class="side-top">
    <input id="q" class="search" type="search" placeholder="会話の内容・タイトルを検索(スペースでAND)" autocomplete="off" aria-label="検索">
    <div class="row" id="roles" role="group" aria-label="検索・表示する発言者">
      <button class="chip" data-role="user" aria-pressed="true">ユーザー</button>
      <button class="chip" data-role="assistant" aria-pressed="true">アシスタント</button>
      <button class="chip" data-role="other" aria-pressed="false">その他</button>
    </div>
    <div class="row">
      <select id="sort" class="sel" aria-label="並び替え">
        <option value="update">更新日時</option>
        <option value="create">作成日時</option>
        <option value="count">メッセージ数</option>
        <option value="hits">一致数</option>
        <option value="title">タイトル</option>
      </select>
      <button id="order" class="sq" aria-label="昇順・降順を切り替え">新しい順</button>
      <span id="count" class="count" aria-live="polite"></span>
    </div>
  </div>
  <ul id="list" class="list"></ul>
</aside>

<main class="reader" id="reader">
  <div id="empty" class="empty"><b>会話を選んでください</b>左の一覧から選ぶか、検索語を入力すると、一致した会話を絞り込めます。ショートカット: 「/」で検索欄に移動。</div>
  <article id="conv" hidden>
    <header class="chead"><div class="chead-in">
      <h1 id="ctitle"></h1>
      <div class="meta" id="cmeta"></div>
      <div class="tools">
        <button class="btn back" id="back">← 一覧へ</button>
        <button class="btn" id="btnBranch" aria-pressed="false" hidden></button>
        <button class="btn" id="btnCopy">Markdownでコピー</button>
        <button class="btn" id="btnSave">.mdで保存</button>
        <span class="mnav" id="mnav" hidden>
          <span id="mcount"></span>
          <button class="btn" id="mprev" aria-label="前の一致">↑</button>
          <button class="btn" id="mnext" aria-label="次の一致">↓</button>
        </span>
      </div>
    </div></header>
    <div class="thread" id="thread"></div>
  </article>
</main>
<div class="toast" id="toast" role="status"></div>

<script>
'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const reEsc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const nf = new Intl.NumberFormat('ja-JP');

/* ---------- state ---------- */
const S = { q:'', terms:[], roles:{user:true, assistant:true, other:false}, sort:'update', order:'desc',
            items:[], total:0, cur:null, conv:null, marks:[], mi:-1, listCtl:null, convCtl:null };
try { const p = JSON.parse(localStorage.getItem('chatlog-viewer') || '{}');
      if (p.sort) S.sort = p.sort; if (p.order) S.order = p.order; if (p.roles) Object.assign(S.roles, p.roles); } catch (e) {}
const savePrefs = () => { try { localStorage.setItem('chatlog-viewer', JSON.stringify({sort:S.sort, order:S.order, roles:S.roles})); } catch (e) {} };
const splitTerms = q => { const seen = new Set(), out = [];
  for (const t of q.trim().split(/[\s\u3000]+/)) { if (t && !seen.has(t.toLowerCase())) { seen.add(t.toLowerCase()); out.push(t); } }
  return out.slice(0, 8); };
const activeRoles = () => Object.keys(S.roles).filter(k => S.roles[k]);
const roleOk = r => r === 'user' ? S.roles.user : r === 'assistant' ? S.roles.assistant : S.roles.other;

/* ---------- formatting ---------- */
const pad = n => String(n).padStart(2, '0');
function fmtDate(t) { if (!t) return ''; const d = new Date(t * 1000); return `${d.getFullYear()}/${pad(d.getMonth()+1)}/${pad(d.getDate())}`; }
function fmtTime(t) { if (!t) return ''; const d = new Date(t * 1000); return `${fmtDate(t)} ${pad(d.getHours())}:${pad(d.getMinutes())}`; }
function hlHTML(text, terms) {
  if (!terms.length) return esc(text);
  const re = new RegExp(terms.slice().sort((a, b) => b.length - a.length).map(reEsc).join('|'), 'gi');
  let out = '', last = 0, m;
  while ((m = re.exec(text))) { out += esc(text.slice(last, m.index)) + '<mark>' + esc(m[0]) + '</mark>'; last = m.index + m[0].length; if (!m[0].length) re.lastIndex++; }
  return out + esc(text.slice(last));
}

/* ---------- 安全な簡易Markdown ---------- */
function fmtInline(e) {
  return e.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
          .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}
function inline(s) {
  return s.split(/(`[^`\n]+`)/g).map((p, k) => k % 2 ? '<code>' + esc(p.slice(1, -1)) + '</code>' : fmtInline(esc(p))).join('');
}
const isSep = l => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
const cells = l => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
const blockStart = (l, nx) => /^\s*```/.test(l) || /^#{1,6}\s/.test(l) || /^\s*>/.test(l) || /^\s*([-*+]|\d+[.)])\s+/.test(l)
  || /^\s*([-*_])\1{2,}\s*$/.test(l) || (l.includes('|') && nx !== undefined && isSep(nx));
function md(src, plain) {
  const L = src.replace(/\r\n?/g, '\n').split('\n'), out = [];
  let i = 0, buf = [];
  const flush = () => { if (buf.length) { const t = buf.join('\n'); if (t.trim()) out.push(plain ? '<p class="plain">' + esc(t.replace(/^\n+|\n+$/g, '')) + '</p>' : '<p>' + buf.map(inline).join('<br>') + '</p>'); buf = []; } };
  while (i < L.length) {
    const l = L[i];
    let m;
    if ((m = /^\s*```\s*([^\s`]*)/.exec(l))) {
      flush(); const code = []; i++;
      while (i < L.length && !/^\s*```\s*$/.test(L[i])) code.push(L[i++]);
      i++;
      out.push('<div class="code"><div class="code-h"><span>' + esc(m[1] || 'code') + '</span><button class="copy" type="button">コピー</button></div><pre><code>' + esc(code.join('\n')) + '</code></pre></div>');
      continue;
    }
    if (plain) { buf.push(l); i++; continue; }
    if (!l.trim()) { flush(); i++; continue; }
    if ((m = /^(#{1,6})\s+(.*)$/.exec(l))) { flush(); out.push('<div class="hd hd' + Math.min(m[1].length, 3) + '">' + inline(m[2]) + '</div>'); i++; continue; }
    if (/^\s*([-*_])\1{2,}\s*$/.test(l)) { flush(); out.push('<hr>'); i++; continue; }
    if (l.includes('|') && i + 1 < L.length && isSep(L[i + 1])) {
      flush(); const head = cells(l); i += 2; const rows = [];
      while (i < L.length && L[i].includes('|') && L[i].trim()) rows.push(cells(L[i++]));
      out.push('<div class="tbl"><table><thead><tr>' + head.map(c => '<th>' + inline(c) + '</th>').join('') + '</tr></thead><tbody>'
        + rows.map(r => '<tr>' + r.map(c => '<td>' + inline(c) + '</td>').join('') + '</tr>').join('') + '</tbody></table></div>');
      continue;
    }
    if (/^\s*>/.test(l)) { flush(); const q = []; while (i < L.length && /^\s*>/.test(L[i])) q.push(L[i++].replace(/^\s*>\s?/, '')); out.push('<blockquote>' + q.map(inline).join('<br>') + '</blockquote>'); continue; }
    if ((m = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(l))) {
      flush(); const ordered = /\d/.test(m[2]); const items = [];
      while (i < L.length) {
        const mm = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(L[i]);
        if (mm) { items.push({d: Math.min(Math.floor(mm[1].length / 2), 4), t: [mm[3]]}); i++; }
        else if (L[i].trim() && /^\s+\S/.test(L[i]) && items.length && !/^\s*```/.test(L[i])) { items[items.length - 1].t.push(L[i].trim()); i++; }
        else break;
      }
      const tag = ordered ? 'ol' : 'ul';
      out.push('<' + tag + '>' + items.map(it => '<li' + (it.d ? ' style="margin-left:' + it.d * 1.2 + 'em"' : '') + '>' + it.t.map(inline).join('<br>') + '</li>').join('') + '</' + tag + '>');
      continue;
    }
    buf.push(l); i++;
    while (i < L.length && L[i].trim() && !blockStart(L[i], L[i + 1])) buf.push(L[i++]);
    flush();
  }
  flush();
  return out.join('');
}

/* ---------- DOM上のハイライト ---------- */
function hlDom(root, terms) {
  if (!terms.length) return [];
  const re = new RegExp(terms.slice().sort((a, b) => b.length - a.length).map(reEsc).join('|'), 'gi');
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT), nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const n of nodes) {
    const t = n.nodeValue; re.lastIndex = 0;
    if (!re.test(t)) continue;
    re.lastIndex = 0;
    const frag = document.createDocumentFragment(); let last = 0, m;
    while ((m = re.exec(t))) {
      if (m.index > last) frag.append(t.slice(last, m.index));
      const mk = document.createElement('mark'); mk.textContent = m[0]; frag.append(mk);
      last = m.index + m[0].length; if (!m[0].length) re.lastIndex++;
    }
    if (last < t.length) frag.append(t.slice(last));
    n.parentNode.replaceChild(frag, n);
  }
  return Array.from(root.querySelectorAll('mark'));
}

/* ---------- API ---------- */
async function api(url, signal) {
  const r = await fetch(url, {signal});
  let j = null; try { j = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error((j && j.error) || ('HTTP ' + r.status));
  return j;
}

/* ---------- 一覧 ---------- */
function itemHref(it) {
  const p = new URLSearchParams({c: it.id});
  if (S.q) p.set('q', S.q);
  if (S.q && it.first_node) { p.set('n', it.first_node); if (it.first_main === false) p.set('b', '1'); }
  return '#' + p.toString();
}
function renderList() {
  const T = S.terms;
  $('#count').textContent = T.length ? `${nf.format(S.items.length)}件が一致` : `${nf.format(S.items.length)}件の会話`;
  if (!S.items.length) { $('#list').innerHTML = '<li class="none">' + (T.length ? '一致する会話がありません。検索語を減らすか、発言者の絞り込みを見直してください。' : '会話がありません。') + '</li>'; return; }
  $('#list').innerHTML = S.items.map(it => {
    const title = it.title || '(無題)';
    let meta = '<time>' + fmtDate(it.ut || it.ct) + '</time><span>' + nf.format(it.n) + '件の発言</span>';
    if (T.length) meta += '<span class="hit">' + (it.hits ? nf.format(it.hits) + '件一致' : 'タイトル一致') + '</span>';
    return '<li><a class="item' + (it.id === S.cur ? ' on' : '') + '" href="' + esc(itemHref(it)) + '" data-id="' + esc(it.id) + '">'
      + '<span class="t">' + (it.star ? '★ ' : '') + hlHTML(title, T) + '</span>'
      + '<span class="m">' + meta + '</span>'
      + (it.snippet ? '<span class="sn">' + hlHTML(it.snippet, T) + '</span>' : '') + '</a></li>';
  }).join('');
}
async function refreshList() {
  if (S.listCtl) S.listCtl.abort();
  const ctl = S.listCtl = new AbortController();
  S.terms = splitTerms(S.q);
  const p = new URLSearchParams({q: S.q, roles: activeRoles().join(','), sort: S.sort, order: S.order});
  try {
    const r = await api('/api/conversations?' + p, ctl.signal);
    S.items = r.items; S.total = r.total; renderList();
  } catch (e) { if (e.name !== 'AbortError') $('#list').innerHTML = '<li class="none">読み込みに失敗しました: ' + esc(e.message) + '<br>ビューアのプロセスが動いているか確認してください。</li>'; }
}

/* ---------- 会話 ---------- */
function msgHTML(m) {
  const cls = m.role === 'user' ? 'user' : m.role === 'assistant' ? 'ai' : 'other';
  const label = m.role === 'user' ? 'ユーザー' : m.role === 'assistant' ? 'アシスタント' : (m.name || m.role || '不明');
  return '<section class="msg ' + cls + (m.main ? '' : ' branch') + '" data-node="' + esc(m.id) + '">'
    + '<div class="gut"><b>' + esc(label) + '</b>' + (m.bs ? '<span class="tag">分岐</span>' : '')
    + (m.model ? '<span>' + esc(m.model) + '</span>' : '') + (m.t ? '<time>' + fmtTime(m.t) + '</time>' : '') + '</div>'
    + '<div class="body">' + md(m.text, cls !== 'ai') + '</div></section>';
}
function renderConv(scrollNode) {
  const c = S.conv; if (!c) return;
  const vis = c.messages.filter(m => roleOk(m.role));
  $('#thread').innerHTML = vis.length ? vis.map(msgHTML).join('') : '<p class="none">表示できる発言がありません。左上の発言者の絞り込みを確認してください。</p>';
  S.terms = splitTerms(S.q);
  S.marks = hlDom($('#thread'), S.terms);
  S.mi = -1;
  if (S.marks.length) {
    let start = 0;
    if (scrollNode) { const el = Array.from($('#thread').children).find(e => e.dataset.node === scrollNode); const k = el ? S.marks.findIndex(mk => el.contains(mk)) : -1; if (k >= 0) start = k; }
    gotoMark(start, false);
  } else if (scrollNode) {
    const el = Array.from($('#thread').children).find(e => e.dataset.node === scrollNode);
    if (el) el.scrollIntoView({block: 'start'});
  } else $('#reader').scrollTop = 0;
  updateNav();
}
function gotoMark(i, smooth = true) {
  if (!S.marks.length) return;
  if (S.marks[S.mi]) S.marks[S.mi].classList.remove('cur');
  S.mi = (i + S.marks.length) % S.marks.length;
  const mk = S.marks[S.mi]; mk.classList.add('cur');
  mk.scrollIntoView({block: 'center', behavior: smooth ? 'smooth' : 'auto'});
  updateNav();
}
function updateNav() {
  const has = S.terms.length > 0 && S.conv;
  $('#mnav').hidden = !has;
  if (!has) return;
  $('#mcount').textContent = S.marks.length ? `${S.mi + 1} / ${S.marks.length}` : '本文に一致なし';
  $('#mprev').disabled = $('#mnext').disabled = !S.marks.length;
}
async function openConv(id, node, branches) {
  S.cur = id;
  document.body.classList.add('reading');
  document.querySelectorAll('.item.on').forEach(e => e.classList.remove('on'));
  const on = document.querySelector('.item[data-id="' + CSS.escape(id) + '"]');
  if (on) { on.classList.add('on'); on.scrollIntoView({block: 'nearest'}); }
  $('#empty').hidden = true; $('#conv').hidden = false;
  if (S.convCtl) S.convCtl.abort();
  const ctl = S.convCtl = new AbortController();
  try {
    const c = await api('/api/conversation?id=' + encodeURIComponent(id) + '&branches=' + (branches ? 1 : 0), ctl.signal);
    S.conv = c;
    document.title = (c.title || '(無題)') + ' - 会話ログビューア';
    $('#ctitle').textContent = c.title || '(無題)';
    const shown = c.messages.filter(m => m.role === 'user' || m.role === 'assistant').length;
    $('#cmeta').innerHTML = '<span>作成 ' + fmtDate(c.ct) + '</span><span>更新 ' + fmtDate(c.ut) + '</span><span>' + nf.format(shown) + '件の発言を表示</span>';
    const bb = $('#btnBranch');
    bb.hidden = c.branch_count === 0;
    bb.textContent = '選ばれなかった分岐を表示 (' + c.branch_count + ')';
    bb.setAttribute('aria-pressed', c.branches ? 'true' : 'false');
    renderConv(node);
  } catch (e) {
    if (e.name === 'AbortError') return;
    $('#thread').innerHTML = '<p class="none">会話を読み込めませんでした: ' + esc(e.message) + '</p>';
  }
}

/* ---------- ルーティング(#c=ID&q=語&n=node&b=1) ---------- */
function hashParams(o) { const p = new URLSearchParams(); for (const k in o) if (o[k]) p.set(k, o[k]); return '#' + p.toString(); }
function nav(o) { const h = hashParams(o); if (location.hash === h) route(); else location.hash = h; }
function route() {
  const p = new URLSearchParams(location.hash.slice(1));
  const q = p.get('q') || '';
  let listP = null;
  if (q !== $('#q').value) { $('#q').value = q; S.q = q; listP = refreshList(); }
  const id = p.get('c');
  if (id) openConv(id, p.get('n'), p.get('b') === '1');
  else { S.cur = null; S.conv = null; document.body.classList.remove('reading'); $('#conv').hidden = true; $('#empty').hidden = false; document.title = '会話ログビューア';
         document.querySelectorAll('.item.on').forEach(e => e.classList.remove('on')); }
  return listP;
}
window.addEventListener('hashchange', route);

/* ---------- エクスポート ---------- */
function toMarkdown() {
  const c = S.conv; const out = ['# ' + (c.title || '(無題)'), '', '作成: ' + fmtTime(c.ct) + ' / 更新: ' + fmtTime(c.ut), ''];
  for (const m of c.messages.filter(m => roleOk(m.role))) {
    const label = m.role === 'user' ? 'ユーザー' : m.role === 'assistant' ? 'アシスタント' : (m.name || m.role);
    out.push('## ' + label + (m.main ? '' : '(分岐)') + (m.t ? '  <sub>' + fmtTime(m.t) + '</sub>' : ''), '', m.text.trim(), '');
  }
  return out.join('\n');
}
let toastT;
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('on'); clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('on'), 1600); }
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (e) {}
  const ta = document.createElement('textarea'); ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.append(ta); ta.select(); let ok = false; try { ok = document.execCommand('copy'); } catch (e) {} ta.remove(); return ok;
}

/* ---------- イベント ---------- */
let deb;
$('#q').addEventListener('input', e => {
  S.q = e.target.value; clearTimeout(deb);
  deb = setTimeout(() => {
    const p = new URLSearchParams(location.hash.slice(1)); if (S.q) p.set('q', S.q); else p.delete('q');
    history.replaceState(null, '', '#' + p.toString());
    refreshList().then(() => { if (S.conv) { S.terms = splitTerms(S.q); renderConv(); } });
  }, 220);
});
$('#q').addEventListener('keydown', e => { if (e.key === 'Escape') { e.target.value = ''; e.target.dispatchEvent(new Event('input')); } });
document.addEventListener('keydown', e => {
  if (e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) { e.preventDefault(); $('#q').focus(); $('#q').select(); }
});
$('#roles').addEventListener('click', e => {
  const b = e.target.closest('.chip'); if (!b) return;
  S.roles[b.dataset.role] = !S.roles[b.dataset.role]; b.setAttribute('aria-pressed', S.roles[b.dataset.role]);
  savePrefs(); if (S.q) refreshList(); renderConv();
});
$('#sort').addEventListener('change', e => { S.sort = e.target.value; savePrefs(); syncOrderLabel(); refreshList(); });
$('#order').addEventListener('click', () => { S.order = S.order === 'desc' ? 'asc' : 'desc'; savePrefs(); syncOrderLabel(); refreshList(); });
function syncOrderLabel() {
  const time = S.sort === 'update' || S.sort === 'create';
  $('#order').textContent = S.order === 'desc' ? (time ? '新しい順' : '多い順') : (time ? '古い順' : '少ない順');
  if (S.sort === 'title') $('#order').textContent = S.order === 'desc' ? '降順' : '昇順';
}
$('#btnBranch').addEventListener('click', () => { const p = new URLSearchParams(location.hash.slice(1)); nav({c: S.cur, q: S.q, b: S.conv.branches ? '' : '1'}); });
$('#btnCopy').addEventListener('click', async () => { toast(await copyText(toMarkdown()) ? 'Markdownをコピーしました' : 'コピーに失敗しました'); });
$('#btnSave').addEventListener('click', () => {
  const name = ((S.conv.title || 'conversation').replace(/[\\/:*?"<>|\r\n]+/g, '_').slice(0, 80) || 'conversation') + '.md';
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([toMarkdown()], {type: 'text/markdown;charset=utf-8'})); a.download = name;
  document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
});
$('#mprev').addEventListener('click', () => gotoMark(S.mi - 1));
$('#mnext').addEventListener('click', () => gotoMark(S.mi + 1));
$('#back').addEventListener('click', () => nav({q: S.q}));
$('#thread').addEventListener('click', async e => {
  const b = e.target.closest('.copy'); if (!b) return;
  const ok = await copyText(b.closest('.code').querySelector('code').textContent);
  b.textContent = ok ? 'コピーしました' : '失敗'; setTimeout(() => b.textContent = 'コピー', 1400);
});

/* ---------- 起動 ---------- */
document.querySelectorAll('#roles .chip').forEach(b => b.setAttribute('aria-pressed', S.roles[b.dataset.role]));
$('#sort').value = S.sort; syncOrderLabel();
(async () => { const lp = route(); if (!lp) await refreshList(); else await lp; if (S.cur) { const on = document.querySelector('.item.on'); if (on) on.scrollIntoView({block: 'center'}); } })();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
