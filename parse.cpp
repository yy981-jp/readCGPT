#include <iostream>
#include <fstream>
#include <nlohmann/json.hpp>

#include "parse.h"
#include "def.h"


void parseJsonData(const std::string& log) {
	std::string text;
	json logJson = json::parse(log);
	for (json messages: logJson) {
		std::ofstream("test.txt") << messages["mapping"] << "\n\n\n";
		// text += messages["message"]["content"]["parts"][0].get<std::string>() + "\n";
	}
	// std::ofstream("test.txt") << text;
	
	exit(100);
}