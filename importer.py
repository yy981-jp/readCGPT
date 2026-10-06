#!/usr/bin/env python3
"""会話ログJSON(ChatGPT等のエクスポート, mappingツリー構造) → SQLite3

使い方:
    python import_conversations.py conversations.json output.db

入力:
  - トップレベルは会話オブジェクトの配列(単一オブジェクトも許容)
  - 各会話は mapping: { node_id: {id, message, parent} } というツリー構造
  - "children" キーは**無い前提**で処理する(エクスポート形式によっては無いため、
    parent ポインタから children を自前で再構築する。たとえ children キーが
    あっても信用せず無視する)。

出力スキーマ(viewer.py が前提とするもの):
  conversations(conversation_id PK, conversation_template_id, title,
                create_time, update_time, current_node, default_model_slug,
                is_archived, is_starred, is_study_mode, raw_json)
  messages(node_id PK, conversation_id, parent_id, role, author_name,
           content_type, content_text, create_time, model_slug,
           has_message, is_mainline, mainline_order)
  mainline_view: メインラインだけを順番に読むビュー
"""
import argparse
import json
import re
import sqlite3
import sys


# ---------------------------------------------------------------- DB schema
def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            conversation_id TEXT PRIMARY KEY,
            conversation_template_id TEXT,
            title TEXT,
            create_time REAL,
            update_time REAL,
            current_node TEXT,
            default_model_slug TEXT,
            is_archived INTEGER,
            is_starred INTEGER,
            is_study_mode INTEGER,
            raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            node_id TEXT PRIMARY KEY,
            conversation_id TEXT,
            parent_id TEXT,
            role TEXT,
            author_name TEXT,
            content_type TEXT,
            content_text TEXT,
            create_time REAL,
            model_slug TEXT,
            has_message INTEGER,
            is_mainline INTEGER,
            mainline_order INTEGER
        );
        DROP VIEW IF EXISTS mainline_view;
        CREATE VIEW mainline_view AS
            SELECT conversation_id, mainline_order, role, author_name, content_text,
                   create_time, node_id, parent_id
            FROM messages
            WHERE is_mainline = 1
            ORDER BY conversation_id, mainline_order;
        """
    )
    conn.commit()


# ---------------------------------------------------------------- JSON parsing
# JSON文字列中で有効なエスケープは \" \\ \/ \b \f \n \r \t \u の9種類だけ。
# エクスポート/変換ツールの不具合で、Windowsのパス(C:\Users\...)などの
# バックスラッシュがエスケープされずにそのまま出力されることがあり、
# その場合 json.load は "Invalid \escape" で失敗する。
# \X (X が上記以外) を見つけたら「本来は \\X(=リテラルな \ の後に X)だった」
# とみなして \\X に直す、という最小限の自動修復を行う。
_ESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)
_VALID_ESCAPES = set('"\\/bfnrtu')


def _fix_escape(m):
    c = m.group(1)
    if c in _VALID_ESCAPES:
        return m.group(0)
    return "\\\\" + c


def repair_invalid_escapes(text):
    return _ESCAPE_RE.sub(_fix_escape, text)


# \b(バックスペース)・\f(改ページ)は自然な会話文にはまず出てこない制御文字。
# 本来 Windows のパス区切り '\' だったものが、\b や \f という"有効なJSONエスケープ"に
# 誤認識されて直後の文字(b/f)が消えた結果である可能性が極めて高いので、
# 「バックスラッシュ + その文字」だったはずの姿へ機械的に戻す。
# \t \n \r は会話文中の正当な改行・タブとして大量に出現するため、同じ扱いはできない
# (無条件に戻すと、本物の改行まで壊れてしまう)。
_stray_fix_count = 0


def fix_stray_control_chars(text):
    global _stray_fix_count
    if text and ("\x08" in text or "\x0c" in text):
        _stray_fix_count += text.count("\x08") + text.count("\x0c")
        text = text.replace("\x08", "\\b").replace("\x0c", "\\f")
    return text


def load_json_robust(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        if "escape" not in e.msg.lower():
            raise
        print(
            f"警告: JSONの解析に失敗しました({e})。"
            "不正なエスケープ(例: Windowsパスのバックスラッシュ)の可能性があるため、自動修復して再試行します...",
            file=sys.stderr,
        )
        fixed = repair_invalid_escapes(text)
        try:
            data = json.loads(fixed)
        except json.JSONDecodeError as e2:
            raise SystemExit(
                f"自動修復後もJSONを解析できませんでした: {e2}\n"
                "ファイルが途中で切れている、またはエスケープ以外の理由で壊れている可能性があります。"
            ) from e2
        print("自動修復して読み込みました。", file=sys.stderr)
        return data


# ---------------------------------------------------------------- JSON helpers
def extract_text(content):
    """message.content から本文テキストを取り出す。parts は文字列/dictが混在しうる。"""
    if not content:
        return ""
    parts = content.get("parts")
    if parts is None:
        # code 実行結果など parts を持たない content_type 向けの保険
        t = content.get("text")
        return t if isinstance(t, str) else ""
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict):
            t = p.get("text")
            if isinstance(t, str):
                out.append(t)
            # image_asset_pointer など、テキストでないパートは無視
    return "\n".join(out)


def sanitize_mapping(mapping):
    """最低限の構造チェックのみ。children キーの有無は要求しない(無い形式がある)。"""
    clean = {}
    for k, v in mapping.items():
        if isinstance(v, dict) and "parent" in v:
            clean[k] = v
    return clean


def build_children(mapping):
    """parent ポインタから children を再構築する。mapping 自体の children キーは信用しない。"""
    children = {}
    roots = []
    for node_id, node in mapping.items():
        parent = node.get("parent")
        if parent is not None and parent in mapping:
            children.setdefault(parent, []).append(node_id)
        else:
            roots.append(node_id)
    return children, roots


def compute_depths(mapping, children, roots):
    """全ノードの部分木の深さを反復的なポストオーダー走査で計算する(再帰は使わない:
    数千ノード規模の会話で RecursionError が起きるため)。

    重要: 走査は必ず「本当のルート」(roots)から始めて親→子の順で下りていく。
    mapping.keys() の出現順(JSON内での並び)は親子関係の順序を保証しないため、
    もし出現順を無条件に起点として使うと、子ノードが親より先にキーとして
    現れた場合に子を root だと誤認し、後で親からその子の深さを引こうとして
    KeyError になる(実際に起きたバグ)。roots を優先した上で、万一ルートから
    辿り着けない孤立ノード(壊れた循環参照など)が残っていた場合のみ、
    最後の保険として mapping.keys() 側から拾う。"""
    visited_global = set()
    order = []

    def walk_from(start):
        if start in visited_global:
            return
        stack2 = [start]
        local_order = []
        while stack2:
            x = stack2.pop()
            if x in visited_global:
                continue
            visited_global.add(x)
            local_order.append(x)
            for c in children.get(x, []):
                if c not in visited_global:
                    stack2.append(c)
        order.extend(local_order)

    for start in roots:
        walk_from(start)
    # 保険: roots から辿れなかった孤立ノード(壊れた循環参照など)も拾っておく
    for start in mapping.keys():
        walk_from(start)

    depth = {}
    for node_id in reversed(order):
        ch = children.get(node_id, [])
        # depth.get(c, 0): 万一それでも未計算のノードを参照してしまっても
        # (理論上あり得ないはずだが)例外で会話全体を失わないための保険
        depth[node_id] = 1 + max((depth.get(c, 0) for c in ch), default=0)
    return depth


def pick_mainline(mapping, children, roots, depth):
    """各分岐点で次の1手を選ぶ(優先順位: 1.部分木最大深さ 2.create_timeが新しい
    3.children配列内で最後に見つかった方)。roots が複数ある場合は、最も深い
    root を会話の起点として採用する。"""
    if not roots:
        return []
    root = max(roots, key=lambda r: depth.get(r, 0))

    def ctime(node_id):
        node = mapping.get(node_id)
        msg = node.get("message") if node else None
        if msg and isinstance(msg.get("create_time"), (int, float)):
            return msg["create_time"]
        return float("-inf")

    path = [root]
    cur = root
    while True:
        ch = children.get(cur, [])
        if not ch:
            break
        best = None
        for idx, c in enumerate(ch):
            key = (depth.get(c, 0), ctime(c), idx)
            if best is None or key >= best[0]:
                best = (key, c)
        cur = best[1]
        path.append(cur)
    return path


# ---------------------------------------------------------------- main import
def process_conversation(cur, conv):
    cid = conv.get("conversation_id") or conv.get("id")
    if not cid:
        print("skip: conversation_id が無い会話をスキップしました", file=sys.stderr)
        return 0

    raw_mapping = conv.get("mapping") or {}
    mapping = sanitize_mapping(raw_mapping)
    children, roots = build_children(mapping)
    depth = compute_depths(mapping, children, roots)
    main_path = pick_mainline(mapping, children, roots, depth)
    mainline_order = {node_id: i for i, node_id in enumerate(main_path)}

    cur.execute(
        """INSERT OR REPLACE INTO conversations
           (conversation_id, conversation_template_id, title, create_time, update_time,
            current_node, default_model_slug, is_archived, is_starred, is_study_mode, raw_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            cid,
            conv.get("conversation_template_id"),
            fix_stray_control_chars(conv.get("title")),
            conv.get("create_time"),
            conv.get("update_time"),
            conv.get("current_node"),
            conv.get("default_model_slug"),
            int(bool(conv.get("is_archived"))),
            int(bool(conv.get("is_starred"))),
            int(bool(conv.get("is_study_mode"))),
            json.dumps(conv, ensure_ascii=False),
        ),
    )

    rows = []
    n_msg = 0
    for node_id, node in mapping.items():
        msg = node.get("message")
        has_message = msg is not None
        role = author_name = content_type = content_text = create_time = model_slug = None
        if has_message:
            author = msg.get("author") or {}
            role = author.get("role")
            author_name = author.get("name")
            content = msg.get("content") or {}
            content_type = content.get("content_type")
            content_text = fix_stray_control_chars(extract_text(content))
            create_time = msg.get("create_time")
            model_slug = (msg.get("metadata") or {}).get("model_slug")
            if content_text:
                n_msg += 1
        rows.append(
            (
                node_id,
                cid,
                node.get("parent"),
                role,
                author_name,
                content_type,
                content_text,
                create_time,
                model_slug,
                int(has_message),
                int(node_id in mainline_order),
                mainline_order.get(node_id),
            )
        )
    cur.executemany(
        """INSERT OR REPLACE INTO messages
           (node_id, conversation_id, parent_id, role, author_name, content_type,
            content_text, create_time, model_slug, has_message, is_mainline, mainline_order)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return n_msg


def process_json(json_path, db_path):
    data = load_json_robust(json_path)
    if isinstance(data, dict):
        data = [data]

    conn = sqlite3.connect(db_path)
    init_db(conn)
    cur = conn.cursor()

    total_msg = 0
    n_conv = 0
    n_err = 0
    for i, conv in enumerate(data, 1):
        try:
            total_msg += process_conversation(cur, conv)
            n_conv += 1
        except Exception as e:  # noqa: BLE001
            n_err += 1
            cid = conv.get("conversation_id") or conv.get("id") or f"index {i}"
            print(
                f"警告: 会話 {cid} の処理中にエラー、スキップします: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )
        if i % 200 == 0:
            conn.commit()
            print(f"  {i} / {len(data)} 件処理済み...", flush=True)

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, is_mainline, mainline_order)"
    )
    conn.commit()
    conn.close()
    if _stray_fix_count:
        print(
            f"(参考: \\b / \\f の誤認識によるバックスラッシュ欠落を {_stray_fix_count} 箇所、自動修正しました)"
        )
    print(f"完了: 会話 {n_conv} 件(失敗 {n_err} 件) / メッセージ本文 {total_msg} 件 -> {db_path}")


def main():
    ap = argparse.ArgumentParser(description="会話ログJSON -> SQLite")
    ap.add_argument("json_path", help="入力のconversations.json")
    ap.add_argument("db_path", nargs="?", default="output.db", help="出力DBファイル(省略時 output.db)")
    args = ap.parse_args()
    process_json(args.json_path, args.db_path)


if __name__ == "__main__":
    main()
    