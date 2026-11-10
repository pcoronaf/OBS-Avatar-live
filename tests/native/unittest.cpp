/*
OBS Avatar Live
Copyright (C) 2026 OBS Avatar Live contributors

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License along
with this program. If not, see <https://www.gnu.org/licenses/>
*/

// Unit tests for the libobs-independent plugin core (no framework needed).

#include "engine_launcher.hpp"
#include "frame_ring_reader.hpp"
#include "ipc_client.hpp"
#include "json_lite.hpp"

#include <clocale>
#include <cstdio>
#include <string>

namespace {
int failures = 0;
#define CHECK(cond)                                                                \
	do {                                                                       \
		if (!(cond)) {                                                     \
			std::fprintf(stderr, "%s:%d CHECK failed: %s\n", __FILE__, __LINE__, #cond); \
			failures++;                                                \
		}                                                                  \
	} while (0)

void test_json_roundtrip()
{
	json::Value v;
	std::string err;
	CHECK(json::Value::parse(
		R"({"v":1,"id":42,"type":"reply","ok":true,"payload":{"a":[1,2.5,"x\u00e9\n"],"b":null,"c":false}})", v,
		&err));
	CHECK(v["v"].as_int() == 1);
	CHECK(v["id"].as_uint() == 42);
	CHECK(v["payload"]["a"].at(1).as_number() == 2.5);
	CHECK(v["payload"]["a"].at(2).as_string() == "x\xc3\xa9\n");
	CHECK(v["payload"]["b"].is_null());
	CHECK(v["missing"]["deep"].is_null());
	json::Value back;
	CHECK(json::Value::parse(v.dump(), back));
	CHECK(back.dump() == v.dump());
	CHECK(!json::Value::parse("{\"a\":}", v));
	CHECK(!json::Value::parse("[1,2", v));
	CHECK(!json::Value::parse("{} x", v));
	std::string deep(100, '[');
	CHECK(!json::Value::parse(deep, v));
	CHECK(json::Value::parse("\"\\ud83d\\ude00\"", v) && v.as_string() == "\xf0\x9f\x98\x80");
	CHECK(json::Value(1234567890123ull).dump() == "1234567890123");
}

void test_json_locale_independent()
{
	// es-ES and similar locales use ',' as decimal separator.
	const char *loc = std::setlocale(LC_NUMERIC, "es_ES.UTF-8");
	json::Value v;
	CHECK(json::Value::parse("{\"x\":0.25}", v) && v["x"].as_number() == 0.25);
	CHECK(json::Value(0.25).dump() == "0.25");
	if (loc)
		std::setlocale(LC_NUMERIC, "C");
}

void test_tokens_and_names()
{
	std::string a = avatar::random_hex(16), b = avatar::random_hex(16);
	CHECK(a.size() == 32 && b.size() == 32 && a != b);
	CHECK(avatar::control_pipe_name("ab").find("avatarlive_ab_ctl") != std::string::npos ||
	      avatar::control_pipe_name("ab").find("AvatarLive_ab_ctl") != std::string::npos);
	avatar::FrameRingReader r;
	std::string err;
	CHECK(!r.open("../etc", &err)); // only hex session tokens accepted
}

void test_launch_command()
{
	avatar::LaunchOptions o;
	o.session = "00ff";
	o.auth_token = "deadbeefcafebabe0123456789abcdef";
	o.engine_path = "/opt/x/engine/avatar_engine/main.py";
	auto c = avatar::build_launch_command(o, 77);
	CHECK(c.working_dir == "/opt/x/engine");
	CHECK(c.argv.size() == 8 && c.argv[1] == "-m" && c.argv[2] == "avatar_engine" && c.argv[3] == "serve");
	CHECK(c.argv[5] == "00ff" && c.argv[7] == "77");
	o.engine_path = "C:\\OBS\\data\\engine\\avatar-engine.exe";
	c = avatar::build_launch_command(o, 1);
	CHECK(c.argv[0] == o.engine_path && c.working_dir == "C:\\OBS\\data\\engine" && c.argv[1] == "serve");
	for (auto &arg : c.argv)
		CHECK(arg.find(o.auth_token) == std::string::npos); // token never on cmdline
}
} // namespace

int main()
{
	test_json_roundtrip();
	test_json_locale_independent();
	test_tokens_and_names();
	test_launch_command();
	if (failures) {
		std::fprintf(stderr, "%d failure(s)\n", failures);
		return 1;
	}
	std::printf("all native unit tests passed\n");
	return 0;
}
