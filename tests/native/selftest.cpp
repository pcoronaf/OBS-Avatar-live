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

// Interop self-test for the plugin's IPC layer, driven by the Python test suite.
//
//   avatar_ipc_selftest read-ring --session S --frames N --timeout-ms T
//       prints one JSON line per newly consumed frame:
//       {"sequence","width","height","flags","generation_id","checksum"}
//       checksum = sum of visible pixel bytes (width*4 per row) mod 2^32.
//   avatar_ipc_selftest ctl --session S --token TOKEN
//       hello, ping, get_metrics; prints each reply as a JSON line.
//   avatar_ipc_selftest controller --engine PATH [--timeout-ms T]
//       drives EngineController end to end: launch, start_session, read frames,
//       survive an engine exit (restart with backoff), read frames again.
//       Prints a JSON summary line.
//
// Exit code 0 on success, 1 on failure, 2 on usage error.

#include "engine_controller.hpp"
#include "frame_ring_reader.hpp"
#include "ipc_client.hpp"
#include "json_lite.hpp"
#include "platform_clock.hpp"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <thread>
#include <cstring>
#include <string>
#include <vector>

using namespace avatar;

namespace {

int usage()
{
	std::fprintf(stderr, "usage: avatar_ipc_selftest read-ring --session S [--frames N] [--timeout-ms T]\n"
			     "       avatar_ipc_selftest ctl --session S --token TOKEN [--timeout-ms T]\n"
			     "       avatar_ipc_selftest controller --engine PATH [--timeout-ms T]\n");
	return 2;
}

const char *arg_value(int argc, char **argv, const char *name, const char *def)
{
	for (int i = 2; i + 1 < argc; i++)
		if (std::strcmp(argv[i], name) == 0)
			return argv[i + 1];
	return def;
}

int read_ring(const std::string &session, long frames, long timeout_ms)
{
	FrameRingReader reader;
	std::string err;
	const uint64_t deadline = monotonic_ns() + uint64_t(timeout_ms) * 1000000ull;
	while (!reader.open(session, &err)) {
		if (monotonic_ns() >= deadline) {
			std::fprintf(stderr, "open failed: %s\n", err.c_str());
			return 1;
		}
		std::this_thread::sleep_for(std::chrono::milliseconds(20));
	}
	std::vector<uint8_t> buf;
	long got = 0;
	while (got < frames) {
		if (monotonic_ns() >= deadline) {
			std::fprintf(stderr, "timeout after %ld frames (rejected=%llu torn=%llu)\n", got,
				     (unsigned long long)reader.rejected_count(),
				     (unsigned long long)reader.torn_count());
			return 1;
		}
		FrameInfo info;
		ReadResult r = reader.try_read(buf, info);
		if (r != ReadResult::NewFrame) {
			reader.wait_ready(20);
			continue;
		}
		uint32_t checksum = 0;
		for (uint8_t b : buf)
			checksum += b;
		json::Value line = json::Value::object();
		line.set("sequence", info.sequence);
		line.set("width", info.width);
		line.set("height", info.height);
		line.set("flags", info.flags);
		line.set("generation_id", info.generation_id);
		line.set("checksum", checksum);
		std::printf("%s\n", line.dump().c_str());
		std::fflush(stdout);
		got++;
	}
	return 0;
}

int ctl(const std::string &session, const std::string &token, long timeout_ms)
{
	IpcClient client;
	std::string err;
	if (!client.connect(session, static_cast<uint32_t>(timeout_ms), 0, &err)) {
		std::fprintf(stderr, "connect failed: %s\n", err.c_str());
		return 1;
	}
	json::Value hello;
	if (!client.handshake(token, "selftest", hello, static_cast<uint32_t>(timeout_ms), &err)) {
		std::fprintf(stderr, "%s\n", err.c_str());
		return 1;
	}
	json::Value line = json::Value::object();
	line.set("type", "hello");
	line.set("payload", hello);
	std::printf("%s\n", line.dump().c_str());

	for (const char *type : {"ping", "get_metrics"}) {
		ReplyResult r = client.request(type, json::Value::object(), static_cast<uint32_t>(timeout_ms));
		if (!r.ok) {
			std::fprintf(stderr, "%s failed: %s %s\n", type, r.error_code.c_str(), r.error_message.c_str());
			return 1;
		}
		json::Value l = json::Value::object();
		l.set("type", type);
		l.set("payload", r.payload);
		std::printf("%s\n", l.dump().c_str());
	}
	std::fflush(stdout);
	client.disconnect();
	return 0;
}

int controller(const std::string &engine, long timeout_ms)
{
	EngineController ctl([](int level,
				const std::string &m) { std::fprintf(stderr, "[%d] %s\n", level, m.c_str()); },
			     "selftest");
	ctl.set_engine(engine, "");
	json::Value cfg = json::Value::object();
	cfg.set("schema_version", 2);
	cfg.set("engine", "lite_cpu_audio");
	cfg.set("profile_id", "selftest");
	cfg.set("drive_mode", "audio");
	ctl.set_session_config(cfg);
	ctl.start_session();

	const uint64_t start = monotonic_ns();
	const uint64_t deadline = start + uint64_t(timeout_ms) * 1000000ull;
	std::vector<uint8_t> buf;
	long before = 0, after = 0;
	unsigned restarts_seen = 0;
	uint64_t lost_at = 0, recovered_ms = 0;
	while (monotonic_ns() < deadline) {
		FrameInfo info;
		ReadResult r = ctl.try_read_frame(buf, info);
		ControllerStatus st = ctl.status();
		if (st.restarts > restarts_seen) {
			restarts_seen = st.restarts;
			if (!lost_at)
				lost_at = monotonic_ns();
		}
		if (r == ReadResult::NewFrame) {
			if (restarts_seen == 0) {
				before++;
			} else {
				after++;
				if (!recovered_ms)
					recovered_ms = (monotonic_ns() - lost_at) / 1000000ull;
			}
		}
		if (after >= 5)
			break;
		std::this_thread::sleep_for(std::chrono::milliseconds(5));
	}
	ControllerStatus st = ctl.status();
	ctl.shutdown();
	json::Value line = json::Value::object();
	line.set("frames_before", before);
	line.set("frames_after", after);
	line.set("restarts", restarts_seen);
	line.set("recovery_ms", recovered_ms);
	line.set("state", st.state);
	line.set("last_error", st.last_error);
	std::printf("%s\n", line.dump().c_str());
	std::fflush(stdout);
	return (before > 0 && after >= 5 && restarts_seen >= 1) ? 0 : 1;
}

} // namespace

int main(int argc, char **argv)
{
	if (argc < 2)
		return usage();
	const std::string cmd = argv[1];
	const long timeout_ms = std::strtol(arg_value(argc, argv, "--timeout-ms", "5000"), nullptr, 10);
	if (cmd == "controller") {
		const char *engine = arg_value(argc, argv, "--engine", nullptr);
		return engine ? controller(engine, timeout_ms) : usage();
	}
	const char *session = arg_value(argc, argv, "--session", nullptr);
	if (!session)
		return usage();
	if (cmd == "read-ring")
		return read_ring(session, std::strtol(arg_value(argc, argv, "--frames", "1"), nullptr, 10), timeout_ms);
	if (cmd == "ctl") {
		const char *token = arg_value(argc, argv, "--token", nullptr);
		if (!token)
			return usage();
		return ctl(session, token, timeout_ms);
	}
	return usage();
}
