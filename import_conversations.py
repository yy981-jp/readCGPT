#!/usr/bin/env python3
"""
会話ログJSON(複数会話が入った配列)をSQLite3に格納するスクリプト。

- 各会話の mapping (node_id -> {message, parent, ...}) を messages テーブルに正規化して保存する。
- 選ばれなかった分岐も含め、全ノードを保存する。
- 「メインライン」= 各分岐点で以下の優先順位で選んだ子ノードを辿った経路:
    1. その子を根とする部分木の最大深さが大きい方
    2. 同じ深さなら create_time が新しい方
    3. create_time も同じなら、子ノードリスト内で後に出現した方
  を is_mainline / mainline_order としてマークする。

使い方:
    python3 import_conversations.py conversations.json output.db
"""

import json
import sqlite3
import sys
from pathlib import Path


SCHEMA = """
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
    conversation_id TEXT NOT NULL,
    parent_id TEXT,
    role TEXT,
    author_name TEXT,
    content_type TEXT,
    content_text TEXT,
    create_time REAL,
    model_slug TEXT,
    has_message INTEGER NOT NULL DEFAULT 0,
    is_mainline INTEGER NOT NULL DEFAULT 0,
    mainline_order INTEGER,
    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id);
CREATE INDEX IF NOT EXISTS idx_messages_mainline
    ON messages(conversation_id, mainline_order);

-- DBビューアでメインラインだけをサクッと読むためのビュー
CREATE VIEW IF NOT EXISTS mainline_view AS
SELECT
    conversation_id,
    mainline_order,
    role,
    author_name,
    content_text,
    create_time,
    node_id,
    parent_id
FROM messages
WHERE is_mainline = 1
ORDER BY conversation_id, mainline_order;
"""


def extract_content_text(message: dict) -> tuple[str | None, str | None]:
    """message.content から content_type と 結合済みテキストを取り出す。"""
    if not message:
        return None, None
    content = message.get("content") or {}
    content_type = content.get("content_type")
    parts = content.get("parts")
    if isinstance(parts, list):
        # parts は文字列以外(dict等マルチモーダル)が混ざる場合もあるので文字列化して結合
        text_parts = [p if isinstance(p, str) else json.dumps(p, ensure_ascii=False) for p in parts]
        text = "\n".join(text_parts)
    else:
        text = None
    return content_type, text


def build_children_index(mapping: dict) -> dict:
    """parent_id -> [child_node_id, ...] (mapping内の出現順を保持)"""
    children: dict[str, list[str]] = {}
    for node_id, node in mapping.items():
        parent = node.get("parent")
        if parent is not None:
            children.setdefault(parent, []).append(node_id)
    return children


def compute_depths(mapping: dict, children: dict) -> dict:
    """
    各ノードを根とする部分木の最大深さ(葉=1)を計算する。

    会話ログは数千ノード規模になり得るため、再帰(RecursionError の原因)は使わず、
    明示的なスタックで「行きがけ順に訪問予約 → 帰りがけ順に値確定」の
    反復的なポストオーダー走査を行う。
    """
    depth_cache: dict[str, int] = {}
    # (node_id, is_second_visit) のスタック。
    # 1回目の訪問で子をスタックに積み、2回目の訪問(全子の depth 確定後)で自分の depth を確定する。
    stack: list[tuple[str, bool]] = [(node_id, False) for node_id in mapping]

    while stack:
        node_id, second_visit = stack.pop()
        if node_id in depth_cache:
            continue
        kids = children.get(node_id, [])
        if second_visit or not kids:
            depth_cache[node_id] = 1 + max((depth_cache.get(c, 0) for c in kids), default=0)
        else:
            stack.append((node_id, True))
            for c in kids:
                if c not in depth_cache:
                    stack.append((c, False))

    return depth_cache


def find_root(mapping: dict) -> str | None:
    for node_id, node in mapping.items():
        if node.get("parent") is None:
            return node_id
    return None


def pick_next_mainline_child(children_ids: list[str], mapping: dict, depths: dict) -> str:
    """
    優先順位:
      1. 部分木の最大深さが大きい
      2. create_time が新しい
      3. children_ids 内で後に出現した方(= 同点なら最後の要素)
    """
    def sort_key(idx_and_id):
        idx, node_id = idx_and_id
        node = mapping[node_id]
        msg = node.get("message") or {}
        create_time = msg.get("create_time")
        if create_time is None:
            create_time = float("-inf")
        return (depths[node_id], create_time, idx)

    indexed = list(enumerate(children_ids))
    best_idx, best_id = max(indexed, key=sort_key)
    return best_id


def compute_mainline(mapping: dict, children: dict) -> set[str]:
    """ルートから辿ったメインラインのノードIDを順序付きリストで返す。"""
    root = find_root(mapping)
    if root is None:
        return []

    depths = compute_depths(mapping, children)

    mainline = [root]
    current = root
    while True:
        kids = children.get(current, [])
        if not kids:
            break
        current = pick_next_mainline_child(kids, mapping, depths)
        mainline.append(current)
    return mainline


def import_conversations(json_path: str, db_path: str) -> None:
    with open(json_path, "r", encoding="utf-8") as f:
        conversations = json.load(f)

    if isinstance(conversations, dict):
        # 単一会話が渡された場合も許容する
        conversations = [conversations]

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    cur = conn.cursor()

    for conv in conversations:
        conv_id = conv.get("conversation_id") or conv.get("id")
        mapping = conv.get("mapping") or {}

        cur.execute(
            """
            INSERT OR REPLACE INTO conversations
            (conversation_id, conversation_template_id, title, create_time, update_time,
             current_node, default_model_slug, is_archived, is_starred, is_study_mode, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conv_id,
                conv.get("conversation_template_id"),
                conv.get("title"),
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

        children = build_children_index(mapping)
        mainline_ids = compute_mainline(mapping, children)
        mainline_order = {node_id: i for i, node_id in enumerate(mainline_ids)}

        for node_id, node in mapping.items():
            message = node.get("message")
            author = (message or {}).get("author") or {}
            content_type, content_text = extract_content_text(message)

            cur.execute(
                """
                INSERT OR REPLACE INTO messages
                (node_id, conversation_id, parent_id, role, author_name,
                 content_type, content_text, create_time, model_slug,
                 has_message, is_mainline, mainline_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    conv_id,
                    node.get("parent"),
                    author.get("role"),
                    author.get("name"),
                    content_type,
                    content_text,
                    (message or {}).get("create_time"),
                    (message or {}).get("metadata", {}).get("model_slug") if message else None,
                    int(message is not None),
                    int(node_id in mainline_order),
                    mainline_order.get(node_id),
                ),
            )

    conn.commit()
    conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("使い方: python3 import_conversations.py <入力JSON> <出力DB>")
        sys.exit(1)

    src, dst = sys.argv[1], sys.argv[2]
    if not Path(src).exists():
        print(f"入力ファイルが見つかりません: {src}")
        sys.exit(1)

    import_conversations(src, dst)
    print(f"完了: {dst} に格納しました")