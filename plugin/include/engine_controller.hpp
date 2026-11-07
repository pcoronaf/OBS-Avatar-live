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

// Owns the engine process, control connection and frame ring for one source.
// A supervisor thread launches / connects / restarts the engine with backoff
// (target recovery < 10 s) so OBS threads never wait on the engine.
// No libobs dependency; logging goes through a callback.

#pragma once

#include "engine_launcher.hpp"
#include "frame_ring_reader.hpp"
#include "ipc_client.hpp"
#include "json_lite.hpp"

#include <atomic>
#include <condition_variable>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace avatar {

struct ControllerStatus {
	std::string state =
		"stopped"; // stopped|launching|connecting|idle|starting|running|paused|degraded|restarting|error
	std::string detail;
	std::string last_error;
	std::string fallback_reason;
	std::string engine_version;
	std::string provider;
	json::Value metrics;          // last Metrics object (PROTOCOL.md §4), null if none
	json::Value devices;          // last list_devices reply, null if none
	uint64_t devices_version = 0; // bumped on every list_devices reply
	json::Value engines;          // engines advertised by hello
	bool engine_alive = false;
	bool connected = false;
	bool session_wanted = false;
	bool session_active = false;
	uint64_t generation_id = 0;
	unsigned restarts = 0;
};

class EngineController {
public:
	enum LogLevel { Debug = 0, Info = 1, Warning = 2, Error = 3 };
	using LogFn = std::function<void(int level, const std::string &msg)>;
	using ResultFn = std::function<void(const ReplyResult &)>;

	explicit EngineController(LogFn log, std::string client_version);
	~EngineController();
	EngineController(const EngineController &) = delete;
	EngineController &operator=(const EngineController &) = delete;

	// Applied at the next (re)launch.
	void set_engine(const std::string &engine_path, const std::string &python_exe);
	// Session configuration (contracts/config.schema.json). If a session is active
	// and the config changed, the session is restarted.
	void set_session_config(const json::Value &config);

	void start_session();
	void stop_session();
	void pause();
	void resume();
	void freeze_neutral(bool enabled);
	void set_quality(const json::Value &payload);
	void request_devices();
	// Requests the device list and waits (bounded) for the reply; true if it arrived.
	bool refresh_devices(uint32_t timeout_ms);
	// Profile operations launch the engine if needed and run once connected.
	void create_profile(const json::Value &payload, ResultFn cb);
	void delete_profile(const std::string &profile_id, ResultFn cb);

	// Stops the supervisor and the engine. Bounded (~2 s worst case). Idempotent.
	void shutdown();

	ControllerStatus status() const;
	// Cheap, lock-free: whether the user asked for a session (for video_tick).
	bool session_requested() const { return session_requested_.load(); }

	// Non-blocking frame access for the OBS video thread. Returns Busy if the
	// supervisor is (re)opening the ring at this moment.
	ReadResult try_read_frame(std::vector<uint8_t> &dst, FrameInfo &info);
	// Writer heartbeat (monotonic ns), 0 if the ring is not open.
	uint64_t heartbeat_ns();

private:
	using Op = std::function<void(IpcClient &)>;

	void run();
	void enqueue(Op op, bool launch_engine);
	void log(int level, const std::string &msg);
	bool launch_and_connect();
	void teardown_engine(const std::string &reason, bool schedule_restart);
	void on_event(const std::string &event, const json::Value &payload);
	void send_start_session(IpcClient &c);
	void set_state(const std::string &state, const std::string &detail = std::string());
	void set_error(const std::string &err);

	LogFn log_;
	std::string client_version_;

	mutable std::mutex mutex_; // protects everything below except ring_
	std::condition_variable cv_;
	ControllerStatus status_;
	LaunchOptions launch_;
	json::Value config_;
	bool config_dirty_ = false;
	bool engine_wanted_ = false;
	bool session_wanted_ = false;
	bool session_active_ = false;
	bool start_inflight_ = false;
	bool stop_inflight_ = false;
	bool disconnected_flag_ = false;
	bool shutting_down_ = false;
	std::deque<Op> ops_;
	uint64_t next_launch_ns_ = 0;
	uint64_t backoff_ms_ = 500;
	uint64_t connected_since_ns_ = 0;
	uint64_t last_metrics_poll_ns_ = 0;

	std::mutex ring_mutex_;
	FrameRingReader ring_;
	std::atomic<bool> ring_open_{false};

	// Owned and used only by the supervisor thread (and shutdown() after join).
	EngineProcess process_;
	IpcClient ipc_;
	std::string session_;
	bool had_process_ = false;

	std::atomic<bool> session_requested_{false};
	std::thread thread_;
	std::atomic<bool> stopped_{false};
};

} // namespace avatar
