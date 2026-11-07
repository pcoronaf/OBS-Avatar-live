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

// Control channel client (docs/PROTOCOL.md §3). No libobs dependency.
// A worker thread owns all reads; writes are serialized by a mutex. Replies are
// matched to requests by id; unsolicited events go to the event callback.
// Callbacks are invoked on the worker thread and must not block for long.

#pragma once

#include "json_lite.hpp"

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <map>
#include <mutex>
#include <string>
#include <thread>

namespace avatar {

constexpr int kProtocolVersion = 1;
constexpr uint32_t kMaxMessageBytes = 1u << 20; // 1 MiB

std::string control_pipe_name(const std::string &session);

struct ReplyResult {
	bool ok = false;
	json::Value payload; // reply payload when ok
	std::string error_code;
	std::string error_message;
};

class IpcClient {
public:
	using ReplyCallback = std::function<void(const ReplyResult &)>;
	using EventCallback = std::function<void(const std::string &event, const json::Value &payload)>;
	using DisconnectCallback = std::function<void(const std::string &reason)>;

	IpcClient();
	~IpcClient();
	IpcClient(const IpcClient &) = delete;
	IpcClient &operator=(const IpcClient &) = delete;

	// Callbacks must be set before connect().
	void set_event_callback(EventCallback cb) { on_event_ = std::move(cb); }
	void set_disconnect_callback(DisconnectCallback cb) { on_disconnect_ = std::move(cb); }

	// Retries until the server pipe exists or timeout elapses. If expected_server_pid
	// is non-zero (Windows), the pipe server process id must match it, which
	// prevents another process from squatting the pipe name.
	bool connect(const std::string &session, uint32_t timeout_ms, unsigned long expected_server_pid,
		     std::string *err);
	void disconnect();
	bool connected() const { return connected_.load(); }
	// Permanently aborts: interrupts a connect() retry loop and fails pending
	// requests (waking blocked request() calls). Thread-safe; used on shutdown.
	void abort();

	// Asynchronous request. cb (optional) runs on the worker thread, or with
	// ok=false/"disconnected" if the connection drops first. Returns request id (0 on failure).
	uint64_t send_request(const std::string &type, const json::Value &payload, ReplyCallback cb = nullptr);

	// Blocking request with timeout. Never call this from the OBS render/video thread.
	ReplyResult request(const std::string &type, const json::Value &payload, uint32_t timeout_ms);

	// Sends hello and validates protocol version. Returns the hello reply payload.
	bool handshake(const std::string &auth_token, const std::string &client_version, json::Value &hello_reply,
		       uint32_t timeout_ms, std::string *err);

private:
	bool write_message(const std::string &body);
	bool read_exact(uint8_t *buf, size_t len);
	void reader_loop();
	void fail_all_pending(const std::string &reason);
	void close_handle();

	std::atomic<bool> connected_{false};
	std::atomic<bool> stopping_{false};
	std::atomic<bool> aborted_{false};
	std::atomic<uint64_t> next_id_{1};
	std::thread reader_;
	std::mutex write_mutex_;
	std::mutex pending_mutex_;
	std::map<uint64_t, ReplyCallback> pending_;
	EventCallback on_event_;
	DisconnectCallback on_disconnect_;
#ifdef _WIN32
	void *pipe_ = nullptr;       // HANDLE (overlapped)
	void *stop_event_ = nullptr; // HANDLE, manual reset
#else
	int fd_ = -1;
#endif
};

} // namespace avatar
