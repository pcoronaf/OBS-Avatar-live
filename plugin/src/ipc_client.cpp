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

#include "ipc_client.hpp"
#include "platform_clock.hpp"

#include <chrono>
#include <cstring>
#include <memory>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#else
#include <cerrno>
#include <cstdlib>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#endif

namespace avatar {

namespace {

#ifdef _WIN32
std::wstring widen(const std::string &s)
{
	if (s.empty())
		return std::wstring();
	int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
	std::wstring w(static_cast<size_t>(n), L'\0');
	MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), w.data(), n);
	return w;
}
#endif

bool valid_session(const std::string &s)
{
	if (s.empty() || s.size() > 64)
		return false;
	for (char c : s)
		if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')))
			return false;
	return true;
}

} // namespace

std::string control_pipe_name(const std::string &session)
{
#ifdef _WIN32
	return "\\\\.\\pipe\\AvatarLive_" + session + "_ctl";
#else
	const char *tmp = std::getenv("TMPDIR");
	std::string dir = (tmp && *tmp) ? tmp : "/tmp";
	if (!dir.empty() && dir.back() == '/')
		dir.pop_back();
	return dir + "/avatarlive_" + session + "_ctl.sock";
#endif
}

IpcClient::IpcClient() = default;

IpcClient::~IpcClient()
{
	disconnect();
}

bool IpcClient::connect(const std::string &session, uint32_t timeout_ms, unsigned long expected_server_pid,
			std::string *err)
{
	disconnect();
	if (aborted_) {
		if (err)
			*err = "aborted";
		return false;
	}
	if (!valid_session(session)) {
		if (err)
			*err = "invalid session token";
		return false;
	}
	const uint64_t deadline = monotonic_ns() + uint64_t(timeout_ms) * 1000000ull;

#ifdef _WIN32
	std::wstring name = widen(control_pipe_name(session));
	HANDLE h = INVALID_HANDLE_VALUE;
	for (;;) {
		// SECURITY_IDENTIFICATION: the server may identify but not impersonate us.
		h = CreateFileW(name.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_EXISTING,
				FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, nullptr);
		if (h != INVALID_HANDLE_VALUE)
			break;
		DWORD e = GetLastError();
		if (monotonic_ns() >= deadline || aborted_) {
			if (err)
				*err = "pipe connect timeout (" + std::to_string(e) + ")";
			return false;
		}
		if (e == ERROR_PIPE_BUSY) {
			WaitNamedPipeW(name.c_str(), 200);
		} else if (e == ERROR_FILE_NOT_FOUND) {
			Sleep(50); // engine still starting
		} else {
			if (err)
				*err = "CreateFileW(pipe) failed (" + std::to_string(e) + ")";
			return false;
		}
	}
	if (expected_server_pid != 0) {
		ULONG server_pid = 0;
		if (!GetNamedPipeServerProcessId(h, &server_pid) || server_pid != expected_server_pid) {
			CloseHandle(h);
			if (err)
				*err = "pipe server process does not match launched engine";
			return false;
		}
	}
	pipe_ = h;
	stop_event_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
	if (!stop_event_) {
		close_handle();
		if (err)
			*err = "CreateEventW failed";
		return false;
	}
#else
	(void)expected_server_pid; // could be checked with SO_PEERCRED; dev-only transport
	std::string path = control_pipe_name(session);
	sockaddr_un addr{};
	if (path.size() >= sizeof(addr.sun_path)) {
		if (err)
			*err = "socket path too long";
		return false;
	}
	addr.sun_family = AF_UNIX;
	std::memcpy(addr.sun_path, path.c_str(), path.size() + 1);
	for (;;) {
		int fd = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
		if (fd < 0) {
			if (err)
				*err = "socket() failed";
			return false;
		}
		if (::connect(fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) == 0) {
			fd_ = fd;
			break;
		}
		int e = errno;
		::close(fd);
		if (monotonic_ns() >= deadline || aborted_ || (e != ENOENT && e != ECONNREFUSED && e != EAGAIN)) {
			if (err)
				*err = "connect failed: " + std::string(std::strerror(e));
			return false;
		}
		std::this_thread::sleep_for(std::chrono::milliseconds(50));
	}
#endif

	stopping_ = false;
	connected_ = true;
	reader_ = std::thread([this] { reader_loop(); });
	return true;
}

void IpcClient::abort()
{
	aborted_ = true;
	fail_all_pending("aborted");
}

void IpcClient::close_handle()
{
#ifdef _WIN32
	if (pipe_)
		CloseHandle(static_cast<HANDLE>(pipe_));
	if (stop_event_)
		CloseHandle(static_cast<HANDLE>(stop_event_));
	pipe_ = nullptr;
	stop_event_ = nullptr;
#else
	if (fd_ >= 0)
		::close(fd_);
	fd_ = -1;
#endif
}

void IpcClient::disconnect()
{
	stopping_ = true;
#ifdef _WIN32
	if (stop_event_)
		SetEvent(static_cast<HANDLE>(stop_event_));
#else
	if (fd_ >= 0)
		::shutdown(fd_, SHUT_RDWR);
#endif
	if (reader_.joinable()) {
		if (reader_.get_id() == std::this_thread::get_id())
			reader_.detach(); // called from a callback; thread exits on its own
		else
			reader_.join();
	}
	connected_ = false;
	{
		std::lock_guard<std::mutex> lock(write_mutex_);
		close_handle();
	}
	fail_all_pending("disconnected");
	stopping_ = false;
}

void IpcClient::fail_all_pending(const std::string &reason)
{
	std::map<uint64_t, ReplyCallback> pending;
	{
		std::lock_guard<std::mutex> lock(pending_mutex_);
		pending.swap(pending_);
	}
	ReplyResult r;
	r.ok = false;
	r.error_code = reason;
	r.error_message = reason;
	for (auto &kv : pending)
		if (kv.second)
			kv.second(r);
}

bool IpcClient::write_message(const std::string &body)
{
	if (body.size() > kMaxMessageBytes)
		return false;
	std::string frame;
	frame.resize(4 + body.size());
	const uint32_t len = static_cast<uint32_t>(body.size());
	frame[0] = static_cast<char>(len & 0xFF);
	frame[1] = static_cast<char>((len >> 8) & 0xFF);
	frame[2] = static_cast<char>((len >> 16) & 0xFF);
	frame[3] = static_cast<char>((len >> 24) & 0xFF);
	std::memcpy(&frame[4], body.data(), body.size());

	std::lock_guard<std::mutex> lock(write_mutex_);
	if (!connected_)
		return false;
#ifdef _WIN32
	HANDLE h = static_cast<HANDLE>(pipe_);
	if (!h)
		return false;
	HANDLE ev = CreateEventW(nullptr, TRUE, FALSE, nullptr);
	if (!ev)
		return false;
	size_t off = 0;
	bool ok = true;
	while (off < frame.size() && ok) {
		OVERLAPPED ov = {};
		ov.hEvent = ev;
		ResetEvent(ev);
		DWORD written = 0;
		DWORD chunk = static_cast<DWORD>(frame.size() - off);
		if (!WriteFile(h, frame.data() + off, chunk, &written, &ov)) {
			if (GetLastError() != ERROR_IO_PENDING) {
				ok = false;
				break;
			}
			if (WaitForSingleObject(ev, 2000) != WAIT_OBJECT_0) {
				CancelIoEx(h, &ov);
				GetOverlappedResult(h, &ov, &written, TRUE);
				ok = false;
				break;
			}
			if (!GetOverlappedResult(h, &ov, &written, FALSE)) {
				ok = false;
				break;
			}
		}
		off += written;
		if (written == 0)
			ok = false;
	}
	CloseHandle(ev);
	return ok;
#else
	size_t off = 0;
	while (off < frame.size()) {
		ssize_t n = ::send(fd_, frame.data() + off, frame.size() - off, MSG_NOSIGNAL);
		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0)
			return false;
		off += static_cast<size_t>(n);
	}
	return true;
#endif
}

bool IpcClient::read_exact(uint8_t *buf, size_t len)
{
	size_t off = 0;
#ifdef _WIN32
	HANDLE h = static_cast<HANDLE>(pipe_);
	HANDLE ev = CreateEventW(nullptr, TRUE, FALSE, nullptr);
	if (!ev)
		return false;
	bool ok = true;
	while (off < len) {
		OVERLAPPED ov = {};
		ov.hEvent = ev;
		ResetEvent(ev);
		DWORD n = 0;
		if (!ReadFile(h, buf + off, static_cast<DWORD>(len - off), &n, &ov)) {
			DWORD e = GetLastError();
			if (e != ERROR_IO_PENDING) {
				ok = false;
				break;
			}
			HANDLE handles[2] = {ev, static_cast<HANDLE>(stop_event_)};
			DWORD w = WaitForMultipleObjects(2, handles, FALSE, INFINITE);
			if (w != WAIT_OBJECT_0) {
				CancelIoEx(h, &ov);
				GetOverlappedResult(h, &ov, &n, TRUE);
				ok = false;
				break;
			}
			if (!GetOverlappedResult(h, &ov, &n, FALSE)) {
				ok = false;
				break;
			}
		}
		if (n == 0) {
			ok = false;
			break;
		}
		off += n;
	}
	CloseHandle(ev);
	return ok;
#else
	while (off < len) {
		if (stopping_)
			return false;
		pollfd p{fd_, POLLIN, 0};
		int r = ::poll(&p, 1, 100);
		if (r < 0 && errno == EINTR)
			continue;
		if (r < 0)
			return false;
		if (r == 0)
			continue;
		ssize_t n = ::recv(fd_, buf + off, len - off, 0);
		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0)
			return false;
		off += static_cast<size_t>(n);
	}
	return true;
#endif
}

void IpcClient::reader_loop()
{
	std::string reason = "closed";
	for (;;) {
		uint8_t hdr[4];
		if (!read_exact(hdr, 4)) {
			reason = "read failed";
			break;
		}
		const uint32_t len = uint32_t(hdr[0]) | (uint32_t(hdr[1]) << 8) | (uint32_t(hdr[2]) << 16) |
				     (uint32_t(hdr[3]) << 24);
		if (len == 0 || len > kMaxMessageBytes) {
			reason = "invalid frame length";
			break;
		}
		std::string body(len, '\0');
		if (!read_exact(reinterpret_cast<uint8_t *>(&body[0]), len)) {
			reason = "read failed";
			break;
		}
		json::Value msg;
		std::string perr;
		if (!json::Value::parse(body, msg, &perr) || !msg.is_object()) {
			reason = "malformed message: " + perr;
			break;
		}
		if (msg["v"].as_int(-1) != kProtocolVersion) {
			reason = "protocol version mismatch";
			break;
		}
		const std::string &type = msg["type"].as_string();
		if (type == "reply") {
			const uint64_t id = msg["id"].as_uint();
			ReplyCallback cb;
			{
				std::lock_guard<std::mutex> lock(pending_mutex_);
				auto it = pending_.find(id);
				if (it != pending_.end()) {
					cb = std::move(it->second);
					pending_.erase(it);
				}
			}
			if (cb) {
				ReplyResult r;
				r.ok = msg["ok"].as_bool(false);
				if (r.ok) {
					r.payload = msg["payload"];
				} else {
					r.error_code = msg["error"]["code"].as_string("error");
					r.error_message = msg["error"]["message"].as_string(r.error_code);
				}
				cb(r);
			}
		} else if (type == "event") {
			if (on_event_)
				on_event_(msg["event"].as_string(), msg["payload"]);
		}
		// Unknown types are ignored for forward compatibility.
	}

	const bool was_stopping = stopping_.load();
	connected_ = false;
	fail_all_pending("disconnected");
	if (!was_stopping && on_disconnect_)
		on_disconnect_(reason);
}

uint64_t IpcClient::send_request(const std::string &type, const json::Value &payload, ReplyCallback cb)
{
	if (!connected_ || aborted_)
		return 0;
	const uint64_t id = next_id_.fetch_add(1);
	json::Value msg = json::Value::object();
	msg.set("v", kProtocolVersion);
	msg.set("id", id);
	msg.set("type", type);
	msg.set("payload", payload.is_object() ? payload : json::Value::object());
	{
		std::lock_guard<std::mutex> lock(pending_mutex_);
		pending_[id] = std::move(cb);
	}
	if (!write_message(msg.dump())) {
		ReplyCallback failed;
		{
			std::lock_guard<std::mutex> lock(pending_mutex_);
			auto it = pending_.find(id);
			if (it != pending_.end()) {
				failed = std::move(it->second);
				pending_.erase(it);
			}
		}
		if (failed) {
			ReplyResult r;
			r.error_code = "write_failed";
			r.error_message = "could not write request";
			failed(r);
		}
		return 0;
	}
	return id;
}

ReplyResult IpcClient::request(const std::string &type, const json::Value &payload, uint32_t timeout_ms)
{
	struct Shared {
		std::mutex m;
		std::condition_variable cv;
		bool done = false;
		ReplyResult result;
	};
	auto shared = std::make_shared<Shared>();
	const uint64_t id = send_request(type, payload, [shared](const ReplyResult &r) {
		std::lock_guard<std::mutex> lock(shared->m);
		shared->result = r;
		shared->done = true;
		shared->cv.notify_all();
	});
	std::unique_lock<std::mutex> lock(shared->m);
	if (id == 0 && !shared->done) {
		ReplyResult r;
		r.error_code = "not_connected";
		r.error_message = "not connected";
		return r;
	}
	if (!shared->cv.wait_for(lock, std::chrono::milliseconds(timeout_ms), [&] { return shared->done; })) {
		lock.unlock();
		{
			std::lock_guard<std::mutex> plock(pending_mutex_);
			pending_.erase(id);
		}
		ReplyResult r;
		r.error_code = "timeout";
		r.error_message = "request '" + type + "' timed out";
		return r;
	}
	return shared->result;
}

bool IpcClient::handshake(const std::string &auth_token, const std::string &client_version, json::Value &hello_reply,
			  uint32_t timeout_ms, std::string *err)
{
	json::Value p = json::Value::object();
	p.set("auth_token", auth_token);
	p.set("client", "obs-avatar-live");
	p.set("client_version", client_version);
	p.set("protocol_version", kProtocolVersion);
	ReplyResult r = request("hello", p, timeout_ms);
	if (!r.ok) {
		if (err)
			*err = "hello failed: " + r.error_code + " " + r.error_message;
		return false;
	}
	if (r.payload["protocol_version"].as_int(-1) != kProtocolVersion) {
		if (err)
			*err = "engine protocol version mismatch";
		return false;
	}
	hello_reply = r.payload;
	return true;
}

} // namespace avatar
