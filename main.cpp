#include <iostream>
#include <fstream>
#include <filesystem>
#include <string>
#include <regex>
#include <nlohmann/json.hpp>

// #include "json.cpp"
std::string extractJsonData(const std::string &html);

using json = nlohmann::json;
namespace fs = std::filesystem;

void saveChatAsText(const json &chat, int index) {
	std::string title = chat.value("title", "chat_" + std::to_string(index));
	std::string safeTitle = std::regex_replace(title, std::regex(R"([\\/:*?"<>|])"), "_");

	fs::create_directory("output");
	std::ofstream out("output/" + safeTitle + ".txt");
	if (!out) throw std::runtime_error("ファイルが開けなかった: " + safeTitle);

	const auto &mapping = chat.at("mapping");
	std::function<void(std::string)> walk;
	walk = [&](std::string id) {
		auto it = mapping.find(id);
		if (it == mapping.end()) return;

		const auto &node = it.value();
		if (node.contains("message") && !node["message"].is_null()) {
			const auto &msg = node["message"];
			std::string role = msg["author"]["role"];
			if (role == "system") return; // 表示しない

			const auto &parts = msg["content"]["parts"];
			for (const auto &part : parts) {
				out << "[" << role << "] " << part.get<std::string>() << "\n\n";
			}
		}
		if (node.contains("children")) {
			for (const auto &child : node["children"]) {
				walk(child.get<std::string>());
			}
		}
	};

	walk("client-created-root");
	std::cout << "保存完了: " << safeTitle << ".txt\n";
}

int main() {
	std::ifstream in("chat.html", std::ios::binary | std::ios::ate);
	if (!in) {
		std::cerr << "chat.htmlが開けません\n";
		return 1;
	}

	std::streamsize size = in.tellg();
	in.seekg(0);
	std::string html;
	html.reserve(static_cast<size_t>(size));
	html.assign(std::istreambuf_iterator<char>(in), {});
	in.close();

	std::cout << "読み込み完了: " << html.size() << " バイト\n";

	try {
		std::string jsonStr = extractJsonData(html);
		json chatData = json::parse(jsonStr);
		std::cout << "チャット件数: " << chatData.size() << "\n";

		for (size_t i = 0; i < chatData.size(); ++i) {
			saveChatAsText(chatData[i], static_cast<int>(i + 1));
		}

		std::cout << "すべてのチャットをoutput/に保存しました！\n";
	} catch (const std::exception &e) {
		std::cerr << "エラー: " << e.what() << "\n";
		return 1;
	}

	return 0;
}
