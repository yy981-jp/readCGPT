#include <iostream>
#include <fstream>
#include <filesystem>
#include <string>
#include <regex>
#include <nlohmann/json.hpp>

#include "extractJson.h"
#include "parse.h"

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

	parseJsonData(extractJsonData(html));
	
}
