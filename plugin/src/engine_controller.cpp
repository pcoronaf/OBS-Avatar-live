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

#include "engine_controller.hpp"
#include "platform_clock.hpp"

#include <algorithm>
#include <chrono>

namespace avatar {

namespace {
constexpr uint64_t kMs = 1000000ull;
constexpr uint32_t kConnectTimeoutMs = 6000;
constexpr uint32_t kHelloTimeoutMs = 3000;
constexpr uint64_t kMaxBackoffMs = 4000; // keeps recovery under the 10 s target
constexpr uint64_t kStableResetMs = 30000;
constexpr uint64_t kMetricsPollMs = 1000;
} // namespace

EngineController::EngineController(LogFn log, std::string client_version)
	: log_(std::move(log)),
	  client_version_(std::move(client_version))
{
	ipc_.set_event_callback([this](const std::string &ev, const json::Value &p) { on_event(ev, p); });
	ipc_.set_disconnect_callback([this](const std::string &reason) {
		std::lock_guard<std::mutex> lock(mutex_);
		disconnected_flag_ = true;
		status_.detail = "control channel lost: " + reason;
		cv_.notify_all();
	});
	thread_ = std::thread([this] { run(); });
}

EngineController::~EngineController()
{
	shutdown();
}

void EngineController::log(int level, const std::string &msg)
{
	if (log_)
		log_(level, msg);
}

void EngineController::set_state(const std::string &state, const std::string &detail)
{
	// mutex_ must be held
	status_.state = state;
	status_.detail = detail;
}

void EngineController::set_error(const std::string &err)
{
	// mutex_ must be held
	status_.last_error = err;
}

void EngineController::set_engine(const std::string &engine_path, const std::string &python_exe)
{
	std::lock_guard<std::mutex> lock(mutex_);
	launch_.engine_path = engine_path;
	launch_.python_exe = python_exe;
}

void EngineController::set_session_config(const json::Value &config)
{
	std::lock_guard<std::mutex> lock(mutex_);
	if (config.dump() == config_.dump())
		return;
	config_ = config;
	if (session_active_ || start_inflight_)
		config_dirty_ = true;
	cv_.notify_all();
}

void EngineController::start_session()
{
	std::lock_guard<std::mutex> lock(mutex_);
	if (shutting_down_)
		return;
	engine_wanted_ = true;
	session_wanted_ = true;
	session_requested_ = true;
	status_.last_error.clear();
	if (!process_.pid())
		next_launch_ns_ = 0; // launch immediately on explicit user action
	cv_.notify_all();
}

void EngineController::stop_session()
{
	std::lock_guard<std::mutex> lock(mutex_);
	session_wanted_ = false;
	session_requested_ = false;
	cv_.notify_all();
}

void EngineController::enqueue(Op op, bool launch_engine)
{
	std::lock_guard<std::mutex> lock(mutex_);
	if (shutting_down_)
		return;
	// Session commands (pause, freeze...) are meaningless without a live engine:
	// drop them instead of replaying stale commands after a later launch.
	if (!launch_engine && !ipc_.connected())
		return;
	if (launch_engine && !engine_wanted_) {
		engine_wanted_ = true;
		next_launch_ns_ = 0;
	}
	ops_.push_back(std::move(op));
	cv_.notify_all();
}

void EngineController::pause()
{
	enqueue([](IpcClient &c) { c.send_request("pause", json::Value::object()); }, false);
}

void EngineController::resume()
{
	enqueue([](IpcClient &c) { c.send_request("resume", json::Value::object()); }, false);
}

void EngineController::freeze_neutral(bool enabled)
{
	enqueue(
		[enabled](IpcClient &c) {
			json::Value p = json::Value::object();
			p.set("enabled", enabled);
			c.send_request("freeze_neutral", p);
		},
		false);
}

void EngineController::set_quality(const json::Value &payload)
{
	enqueue([payload](IpcClient &c) { c.send_request("set_quality", payload); }, false);
}

void EngineController::request_devices()
{
	enqueue(
		[this](IpcClient &c) {
			c.send_request("list_devices", json::Value::object(), [this](const ReplyResult &r) {
				if (!r.ok)
					return;
				std::lock_guard<std::mutex> lock(mutex_);
				status_.devices = r.payload;
			});
		},
		true);
}

void EngineController::create_profile(const json::Value &payload, ResultFn cb)
{
	enqueue([payload, cb](IpcClient &c) { c.send_request("create_profile", payload, cb); }, true);
}

void EngineController::delete_profile(const std::string &profile_id, ResultFn cb)
{
	json::Value p = json::Value::object();
	p.set("profile_id", profile_id);
	enqueue([p, cb](IpcClient &c) { c.send_request("delete_profile", p, cb); }, true);
}

ControllerStatus EngineController::status() const
{
	std::lock_guard<std::mutex> lock(mutex_);
	ControllerStatus s = status_;
	s.session_wanted = session_wanted_;
	s.session_active = session_active_;
	s.connected = ipc_.connected();
	return s;
}

ReadResult EngineController::try_read_frame(std::vector<uint8_t> &dst, FrameInfo &info)
{
	if (!ring_open_.load())
		return ReadResult::NotOpen;
	std::unique_lock<std::mutex> lock(ring_mutex_, std::try_to_lock);
	if (!lock.owns_lock())
		return ReadResult::Busy;
	return ring_.try_read(dst, info);
}

uint64_t EngineController::heartbeat_ns()
{
	if (!ring_open_.load())
		return 0;
	std::unique_lock<std::mutex> lock(ring_mutex_, std::try_to_lock);
	if (!lock.owns_lock())
		return 0;
	return ring_.heartbeat_ns();
}

void EngineController::on_event(const std::string &event, const json::Value &payload)
{
	std::lock_guard<std::mutex> lock(mutex_);
	if (event == "state") {
		status_.state = payload["state"].as_string(status_.state);
		status_.detail = payload["detail"].as_string("");
	} else if (event == "metrics") {
		status_.metrics = payload;
		status_.provider = payload["execution_provider_selected"].as_string(status_.provider);
		if (payload["fallback_reason"].is_string())
			status_.fallback_reason = payload["fallback_reason"].as_string();
	} else if (event == "fallback") {
		status_.fallback_reason = payload["from"].as_string("?") + " -> " + payload["to"].as_string("?") +
					  ": " + payload["reason"].as_string("");
	} else if (event == "error") {
		status_.last_error = payload["code"].as_string("error") + ": " + payload["message"].as_string("");
	}
}

void EngineController::send_start_session(IpcClient &c)
{
	// mutex_ must NOT be held: a failed write invokes the callback synchronously.
	json::Value cfg;
	{
		std::lock_guard<std::mutex> lock(mutex_);
		start_inflight_ = true;
		config_dirty_ = false;
		set_state("starting");
		cfg = config_;
	}
	c.send_request("start_session", cfg, [this](const ReplyResult &r) {
		std::lock_guard<std::mutex> lock(mutex_);
		start_inflight_ = false;
		if (r.ok) {
			session_active_ = true;
			status_.generation_id = r.payload["generation_id"].as_uint();
			status_.provider = r.payload["provider"].as_string(status_.provider);
			set_state("running");
		} else {
			// Do not retry automatically: a bad config/profile would loop forever.
			session_wanted_ = false;
			session_requested_ = false;
			set_error("start_session failed: " + r.error_code + " " + r.error_message);
			set_state("error", status_.last_error);
		}
		cv_.notify_all();
	});
}

bool EngineController::launch_and_connect()
{
	// Called on the supervisor thread without mutex_ held.
	LaunchOptions opts;
	{
		std::lock_guard<std::mutex> lock(mutex_);
		opts = launch_;
		set_state("launching");
	}
	opts.session = random_hex(16);
	opts.auth_token = random_hex(16);
	if (opts.session.empty() || opts.auth_token.empty()) {
		std::lock_guard<std::mutex> lock(mutex_);
		set_error("could not generate random tokens");
		return false;
	}
	if (opts.engine_path.empty()) {
		std::lock_guard<std::mutex> lock(mutex_);
		set_error("engine path not configured");
		return false;
	}

	std::string err;
	if (!process_.start(opts, &err)) {
		log(Error, "engine launch failed: " + err);
		std::lock_guard<std::mutex> lock(mutex_);
		set_error("engine launch failed: " + err);
		return false;
	}
	had_process_ = true;
	session_ = opts.session;
	log(Info, "engine started, pid " + std::to_string(process_.pid()));
	{
		std::lock_guard<std::mutex> lock(mutex_);
		status_.engine_alive = true;
		disconnected_flag_ = false;
		set_state("connecting");
	}

#ifdef _WIN32
	const unsigned long expected_pid = opts.python_exe.empty() ? process_.pid() : 0;
	// In dev mode a launcher (py.exe / venv shim) may host the real server process.
#else
	const unsigned long expected_pid = 0;
#endif
	if (!ipc_.connect(session_, kConnectTimeoutMs, expected_pid, &err)) {
		log(Error, "engine connect failed: " + err);
		std::lock_guard<std::mutex> lock(mutex_);
		set_error("engine connect failed: " + err);
		return false;
	}
	json::Value hello;
	if (!ipc_.handshake(opts.auth_token, client_version_, hello, kHelloTimeoutMs, &err)) {
		log(Error, err);
		std::lock_guard<std::mutex> lock(mutex_);
		set_error(err);
		return false;
	}
	{
		std::lock_guard<std::mutex> lock(mutex_);
		status_.engine_version = hello["engine_version"].as_string("");
		status_.engines = hello["engines"];
		connected_since_ns_ = monotonic_ns();
		set_state("idle");
	}
	log(Info, "connected to engine " + hello["engine_version"].as_string("?"));
	return true;
}

void EngineController::teardown_engine(const std::string &reason, bool schedule_restart)
{
	// Supervisor thread (or shutdown after join), mutex_ not held.
	{
		std::lock_guard<std::mutex> rl(ring_mutex_);
		ring_open_ = false;
		ring_.close();
	}
	ipc_.disconnect();
	process_.terminate(schedule_restart ? 0 : 1000);
	std::lock_guard<std::mutex> lock(mutex_);
	session_active_ = false;
	start_inflight_ = false;
	stop_inflight_ = false;
	disconnected_flag_ = false;
	status_.engine_alive = false;
	status_.metrics = json::Value();
	if (schedule_restart) {
		status_.restarts++;
		next_launch_ns_ = monotonic_ns() + backoff_ms_ * kMs;
		backoff_ms_ = std::min<uint64_t>(backoff_ms_ * 2, kMaxBackoffMs);
		set_state("restarting", reason);
		log(Warning, "engine lost (" + reason + "), restarting");
	} else {
		set_state("stopped", reason);
	}
}

void EngineController::run()
{
	for (;;) {
		bool want_engine;
		{
			std::unique_lock<std::mutex> lock(mutex_);
			cv_.wait_for(lock, std::chrono::milliseconds(100));
			if (shutting_down_)
				break;
			want_engine = engine_wanted_;
		}
		if (!want_engine)
			continue;

		const uint64_t now = monotonic_ns();

		// 1. Process liveness / launch.
		if (!process_.running()) {
			if (had_process_) {
				had_process_ = false;
				teardown_engine("engine exited with code " + std::to_string(process_.exit_code()),
						true);
				continue;
			}
			uint64_t next;
			{
				std::lock_guard<std::mutex> lock(mutex_);
				next = next_launch_ns_;
			}
			if (now < next)
				continue;
			if (!launch_and_connect()) {
				const bool started = had_process_;
				had_process_ = false;
				if (started) {
					teardown_engine("startup failed", true);
				} else {
					std::lock_guard<std::mutex> lock(mutex_);
					next_launch_ns_ = now + backoff_ms_ * kMs;
					backoff_ms_ = std::min<uint64_t>(backoff_ms_ * 2, kMaxBackoffMs);
					set_state("error", status_.last_error);
				}
			}
			continue;
		}

		// 2. Control channel health.
		bool lost;
		{
			std::lock_guard<std::mutex> lock(mutex_);
			lost = disconnected_flag_ || !ipc_.connected();
		}
		if (lost) {
			had_process_ = false;
			teardown_engine("control channel lost", true);
			continue;
		}

		// 3. Frame ring.
		if (!ring_open_.load()) {
			std::lock_guard<std::mutex> rl(ring_mutex_);
			std::string err;
			if (ring_.open(session_, &err))
				ring_open_ = true;
		}

		// 4. Queued operations and session state machine. Decide under the
		// lock, send without it (reply callbacks take mutex_).
		std::deque<Op> ops;
		bool do_start = false, do_stop = false;
		{
			std::lock_guard<std::mutex> lock(mutex_);
			ops.swap(ops_);
			if (connected_since_ns_ && now - connected_since_ns_ > kStableResetMs * kMs)
				backoff_ms_ = 500;

			if (session_wanted_ && !session_active_ && !start_inflight_ && !stop_inflight_) {
				do_start = true;
			} else if ((!session_wanted_ || config_dirty_) && session_active_ && !stop_inflight_) {
				stop_inflight_ = true;
				do_stop = true;
			}
		}
		if (do_start)
			send_start_session(ipc_);
		if (do_stop) {
			ipc_.send_request("stop_session", json::Value::object(), [this](const ReplyResult &) {
				std::lock_guard<std::mutex> l(mutex_);
				stop_inflight_ = false;
				session_active_ = false;
				status_.metrics = json::Value();
				set_state("idle");
				cv_.notify_all();
			});
		}
		for (auto &op : ops)
			op(ipc_);

		// 5. Metrics polling (events also arrive asynchronously).
		bool poll = false;
		{
			std::lock_guard<std::mutex> lock(mutex_);
			if (session_active_ && now - last_metrics_poll_ns_ > kMetricsPollMs * kMs) {
				last_metrics_poll_ns_ = now;
				poll = true;
			}
		}
		if (poll) {
			ipc_.send_request("get_metrics", json::Value::object(), [this](const ReplyResult &r) {
				if (!r.ok)
					return;
				std::lock_guard<std::mutex> lock(mutex_);
				if (!session_active_)
					return;
				status_.metrics = r.payload;
				status_.provider = r.payload["execution_provider_selected"].as_string(status_.provider);
				if (r.payload["fallback_reason"].is_string())
					status_.fallback_reason = r.payload["fallback_reason"].as_string();
			});
		}
	}
}

void EngineController::shutdown()
{
	if (stopped_.exchange(true))
		return;
	{
		std::lock_guard<std::mutex> lock(mutex_);
		shutting_down_ = true;
		ops_.clear();
		cv_.notify_all();
	}
	// Ask a connected engine to stop before aborting the channel (bounded).
	// If the supervisor is still connecting, abort() interrupts it instead.
	const bool was_connected = ipc_.connected();
	if (was_connected) {
		ipc_.request("stop_session", json::Value::object(), 500);
		ipc_.request("shutdown", json::Value::object(), 500);
	}
	ipc_.abort();
	if (thread_.joinable())
		thread_.join();
	// The Job object (Windows) / SIGTERM (POSIX) guarantees termination below.
	{
		std::lock_guard<std::mutex> rl(ring_mutex_);
		ring_open_ = false;
		ring_.close();
	}
	ipc_.disconnect();
	process_.terminate(1000);
	std::lock_guard<std::mutex> lock(mutex_);
	session_active_ = false;
	session_requested_ = false;
	status_.engine_alive = false;
	set_state("stopped");
}

} // namespace avatar
