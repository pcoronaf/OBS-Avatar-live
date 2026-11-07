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

// Starts and supervises the out-of-process engine (ADR-001). No libobs dependency.
// Windows: the child is placed in a Job object with KILL_ON_JOB_CLOSE so it can
// never outlive OBS. The auth token is passed only through the environment.

#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace avatar {

// Cryptographically random hex string (2*bytes chars). Empty string on failure.
std::string random_hex(size_t bytes);

struct LaunchOptions {
	std::string engine_path; // avatar-engine.exe, or a .py / directory in dev mode
	std::string python_exe;  // non-empty => dev mode: <python> -m avatar_engine serve ...
	std::string session;     // 32 hex chars
	std::string auth_token;  // 32 hex chars, passed as AVATAR_LIVE_AUTH_TOKEN
	std::vector<std::string> extra_args;
};

// Resolved command for a launch (exposed for tests / diagnostics).
struct LaunchCommand {
	std::vector<std::string> argv;
	std::string working_dir;
};

LaunchCommand build_launch_command(const LaunchOptions &opts, unsigned long parent_pid);

unsigned long current_process_id();

class EngineProcess {
public:
	EngineProcess();
	~EngineProcess();
	EngineProcess(const EngineProcess &) = delete;
	EngineProcess &operator=(const EngineProcess &) = delete;

	bool start(const LaunchOptions &opts, std::string *err);
	bool running(); // non-blocking; reaps the child if it exited
	int exit_code() const { return exit_code_; }
	unsigned long pid() const { return pid_; }
	// Waits up to wait_ms for a voluntary exit, then kills the process.
	void terminate(uint32_t wait_ms);

private:
	unsigned long pid_ = 0;
	int exit_code_ = 0;
#ifdef _WIN32
	void *process_ = nullptr; // HANDLE
	void *job_ = nullptr;     // HANDLE
#else
	int child_ = -1;
#endif
};

} // namespace avatar
