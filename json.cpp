std::string extractJsonData(const std::string &html) {
	std::size_t start = html.find("var jsonData = ");
	if (start == std::string::npos) throw std::runtime_error("jsonDataの開始が見つからない");

	start = html.find('[', start);
	if (start == std::string::npos) throw std::runtime_error("配列開始 [ が見つからない");

	// jsonData以降で "]" があって、少し進んだ先に "var " が来るところを探す
	std::size_t end = start;
	while (true) {
		end = html.find(']', end + 1);
		if (end == std::string::npos) throw std::runtime_error("終わりの ] が見つからない");

		// ] から数文字スキップして "var " が来るか見る
		std::size_t lookahead = html.find_first_not_of(" \n\r\t;", end + 1);
		if (lookahead != std::string::npos && html.substr(lookahead, 20).contains("var assetsJson")) {
			break; // ここで切ればちょうど良い
		}
	}

	std::cout << "JSON部分を抽出中...\n";
	return html.substr(start, end - start + 1); // [] の部分だけ抜き出す
}
