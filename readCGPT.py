import json
import sqlite3


def get_longest_path_from_root(mapping, root_id):
	depth_cache = {}

	def dfs(node_id, visited):
		if node_id in visited:
			return (0, [])

		if node_id in depth_cache:
			return depth_cache[node_id]

		visited.add(node_id)

		node = mapping[node_id]
		children = node.get("children", [])

		max_depth = 0
		best_path = []

		for child in children:
			if child not in mapping:
				continue

			depth, path = dfs(child, visited)

			if depth > max_depth:
				max_depth = depth
				best_path = path

		result = (max_depth + 1, [node_id] + best_path)
		depth_cache[node_id] = result

		visited.remove(node_id)
		return result

	_, path = dfs(root_id, set())
	return path

def sanitize_mapping(mapping):
	clean = {}

	for k, v in mapping.items():
		if not isinstance(v, dict):
			continue

		# 最低限の構造チェック
		if "parent" not in v or "children" not in v:
			continue

		clean[k] = v

	return clean

def find_roots(mapping):
	roots = []

	for k, v in mapping.items():
		if v.get("parent") is None or v.get("parent") not in mapping:
			roots.append(k)

	return roots

def extract_text(content):
	if not content:
		return ""

	parts = content.get("parts", [])
	result = []

	for p in parts:
		if isinstance(p, str):
			result.append(p)
		elif isinstance(p, dict):
			if p.get("type") == "text":
				result.append(p.get("text", ""))
			# 他のタイプ（imageとか）は無視

	return "\n".join(result)


# -----------------------------
# 最長パスを取得（深さ優先）
# -----------------------------
def get_longest_path(mapping):
	depth_cache = {}

	def dfs(node_id):
		if node_id in depth_cache:
			return depth_cache[node_id]

		node = mapping[node_id]
		children = node.get("children", [])

		if not children:
			depth_cache[node_id] = (1, [node_id])
			return depth_cache[node_id]

		max_depth = 0
		best_path = []

		for child in children:
			if child not in mapping:
				continue

			depth, path = dfs(child)
			if depth > max_depth:
				max_depth = depth
				best_path = path

		result = (max_depth + 1, [node_id] + best_path)
		depth_cache[node_id] = result
		return result

	# root探す（parentがNoneのやつ）
	root_id = None
	for k, v in mapping.items():
		if v.get("parent") is None:
			root_id = k
			break

	if root_id is None:
		raise Exception("rootが見つからない")

	_, path = dfs(root_id)
	return path


# -----------------------------
# DB初期化
# -----------------------------
def init_db(conn):
	cur = conn.cursor()

	cur.execute("""
	CREATE TABLE IF NOT EXISTS conversations (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		title TEXT,
		create_time REAL,
		update_time REAL
	)
	""")

	cur.execute("""
	CREATE TABLE IF NOT EXISTS messages (
		id TEXT PRIMARY KEY,
		conversation_id INTEGER,
		parent_id TEXT,
		role TEXT,
		content TEXT,
		create_time REAL,
		order_index INTEGER
	)
	""")

	conn.commit()


# -----------------------------
# JSON → DB
# -----------------------------
def process_json(json_path, db_path="chat.db"):
	conn = sqlite3.connect(db_path)
	init_db(conn)
	cur = conn.cursor()

	with open(json_path, "r", encoding="utf-8") as f:
		data = json.load(f)

	for conv in data:
		# conversation登録
		cur.execute("""
		INSERT INTO conversations (title, create_time, update_time)
		VALUES (?, ?, ?)
		""", (
			conv.get("title"),
			conv.get("create_time"),
			conv.get("update_time")
		))

		conversation_id = cur.lastrowid

		mapping = sanitize_mapping(conv["mapping"])

		roots = find_roots(mapping)

		best_path = []
		best_len = 0

		for root in roots:
			try:
				path = get_longest_path_from_root(mapping, root)

				if len(path) > best_len:
					best_len = len(path)
					best_path = path

			except Exception as e:
				print(f"skip broken root: {root}", e)

		path_ids = best_path

		order_index = 0

		for node_id in path_ids:
			node = mapping[node_id]
			msg = node.get("message")

			if msg is None:
				continue

			role = msg["author"]["role"]

			content = extract_text(msg.get("content", {}))

			create_time = msg.get("create_time")

			cur.execute("""
			INSERT OR IGNORE INTO messages
			(id, conversation_id, parent_id, role, content, create_time, order_index)
			VALUES (?, ?, ?, ?, ?, ?, ?)
			""", (
				node_id,
				conversation_id,
				node.get("parent"),
				role,
				content,
				create_time,
				order_index
			))

			order_index += 1

	conn.commit()
	conn.close()


# -----------------------------
# 実行
# -----------------------------
if __name__ == "__main__":
	process_json("data.json")
