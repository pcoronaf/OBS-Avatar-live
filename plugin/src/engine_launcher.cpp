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

#include "engine_launcher.hpp"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstring>
#include <thread>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <bcrypt.h>
#pragma comment(lib, "bcrypt.lib")
#else
#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <fcntl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#ifdef __linux__
#include <sys/prctl.h>
#endif
extern char **environ;
#endif

namespace avatar {

namespace {

bool ends_with(const std::string &s, const std::string &suffix)
{
	if (s.size() < suffix.size())
		return false;
	return std::equal(suffix.rbegin(), suffix.rend(), s.rbegin(), [](char a, char b) {
		return std::tolower((unsigned char)a) == std::tolower((unsigned char)b);
	});
}

std::string dirname_of(const std::string &p)
{
	size_t pos = p.find_last_of("/\\");
	if (pos == std::string::npos)
		return ".";
	if (pos == 0)
		return p.substr(0, 1);
	return p.substr(0, pos);
}

std::string basename_of(const std::string &p)
{
	size_t pos = p.find_last_of("/\\");
	return pos == std::string::npos ? p : p.substr(pos + 1);
}

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

// Quote one argument following the CommandLineToArgvW rules.
std::wstring quote_arg(const std::wstring &arg)
{
	if (!arg.empty() && arg.find_first_of(L" \t\n\v\"") == std::wstring::npos)
		return arg;
	std::wstring out = L"\"";
	for (auto it = arg.begin();; ++it) {
		size_t backslashes = 0;
		while (it != arg.end() && *it == L'\\') {
			++it;
			++backslashes;
		}
		if (it == arg.end()) {
			out.append(backslashes * 2, L'\\');
			break;
		} else if (*it == L'"') {
			out.append(backslashes * 2 + 1, L'\\');
			out.push_back(*it);
		} else {
			out.append(backslashes, L'\\');
			out.push_back(*it);
		}
	}
	out.push_back(L'"');
	return out;
}

// Copy of the current environment without any inherited token, plus ours.
std::vector<wchar_t> build_environment_block(const std::string &token)
{
	std::vector<std::wstring> vars;
	LPWCH env = GetEnvironmentStringsW();
	if (env) {
		for (LPWCH p = env; *p; p += wcslen(p) + 1) {
			std::wstring v(p);
			if (_wcsnicmp(v.c_str(), L"AVATAR_LIVE_AUTH_TOKEN=", 23) == 0)
				continue;
			vars.push_back(std::move(v));
		}
		FreeEnvironmentStringsW(env);
	}
	vars.push_back(L"AVATAR_LIVE_AUTH_TOKEN=" + widen(token));
	// CreateProcess expects the block sorted (case-insensitive, "=C:" entries first).
	std::sort(vars.begin(), vars.end(),
		  [](const std::wstring &a, const std::wstring &b) { return _wcsicmp(a.c_str(), b.c_str()) < 0; });
	std::vector<wchar_t> block;
	for (auto &v : vars) {
		block.insert(block.end(), v.begin(), v.end());
		block.push_back(L'\0');
	}
	block.push_back(L'\0');
	return block;
}
#endif

} // namespace

std::string random_hex(size_t bytes)
{
	std::vector<unsigned char> buf(bytes);
#ifdef _WIN32
	if (!BCRYPT_SUCCESS(BCryptGenRandom(nullptr, buf.data(), static_cast<ULONG>(buf.size()),
					    BCRYPT_USE_SYSTEM_PREFERRED_RNG)))
		return std::string();
#else
	FILE *f = std::fopen("/dev/urandom", "rb");
	if (!f)
		return std::string();
	size_t n = std::fread(buf.data(), 1, buf.size(), f);
	std::fclose(f);
	if (n != buf.size())
		return std::string();
#endif
	static const char *hex = "0123456789abcdef";
	std::string out;
	out.reserve(bytes * 2);
	for (unsigned char c : buf) {
		out.push_back(hex[c >> 4]);
		out.push_back(hex[c & 0xF]);
	}
	return out;
}

unsigned long current_process_id()
{
#ifdef _WIN32
	return GetCurrentProcessId();
#else
	return static_cast<unsigned long>(getpid());
#endif
}

LaunchCommand build_launch_command(const LaunchOptions &opts, unsigned long parent_pid)
{
	LaunchCommand cmd;
	const std::string &path = opts.engine_path;
	const bool dev_mode = !opts.python_exe.empty() || ends_with(path, ".py");
	if (dev_mode) {
		// path may be .../engine/avatar_engine/main.py, .../engine/avatar_engine, or .../engine
		std::string dir = path;
		if (ends_with(path, ".py"))
			dir = dirname_of(path);
		if (basename_of(dir) == "avatar_engine")
			dir = dirname_of(dir);
		cmd.working_dir = dir;
		cmd.argv.push_back(opts.python_exe.empty() ?
#ifdef _WIN32
							   std::string("python")
#else
							   std::string("python3")
#endif
							   : opts.python_exe);
		cmd.argv.push_back("-m");
		cmd.argv.push_back("avatar_engine");
	} else {
		cmd.working_dir = dirname_of(path);
		cmd.argv.push_back(path);
	}
	cmd.argv.push_back("serve");
	cmd.argv.push_back("--session");
	cmd.argv.push_back(opts.session);
	cmd.argv.push_back("--parent-pid");
	cmd.argv.push_back(std::to_string(parent_pid));
	for (auto &a : opts.extra_args)
		cmd.argv.push_back(a);
	return cmd;
}

EngineProcess::EngineProcess() = default;

EngineProcess::~EngineProcess()
{
	terminate(0);
}

bool EngineProcess::start(const LaunchOptions &opts, std::string *err)
{
	terminate(0);
	if (opts.session.empty() || opts.auth_token.empty()) {
		if (err)
			*err = "missing session or auth token";
		return false;
	}
	LaunchCommand cmd = build_launch_command(opts, current_process_id());

#ifdef _WIN32
	std::wstring cmdline;
	for (size_t i = 0; i < cmd.argv.size(); i++) {
		if (i)
			cmdline += L' ';
		cmdline += quote_arg(widen(cmd.argv[i]));
	}
	std::vector<wchar_t> cmdbuf(cmdline.begin(), cmdline.end());
	cmdbuf.push_back(L'\0');
	std::vector<wchar_t> envblock = build_environment_block(opts.auth_token);
	std::wstring cwd = widen(cmd.working_dir);

	HANDLE job = CreateJobObjectW(nullptr, nullptr);
	if (!job) {
		if (err)
			*err = "CreateJobObjectW failed (" + std::to_string(GetLastError()) + ")";
		return false;
	}
	JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {};
	limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
	SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits));

	STARTUPINFOW si = {};
	si.cb = sizeof(si);
	PROCESS_INFORMATION pi = {};
	// In dev mode a relative "python" is searched on PATH (lpApplicationName = NULL).
	BOOL ok = CreateProcessW(nullptr, cmdbuf.data(), nullptr, nullptr, FALSE,
				 CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT, envblock.data(),
				 cwd.empty() ? nullptr : cwd.c_str(), &si, &pi);
	if (!ok) {
		DWORD e = GetLastError();
		CloseHandle(job);
		if (err)
			*err = "CreateProcessW failed (" + std::to_string(e) + ")";
		return false;
	}
	if (!AssignProcessToJobObject(job, pi.hProcess)) {
		DWORD e = GetLastError();
		TerminateProcess(pi.hProcess, 1);
		CloseHandle(pi.hThread);
		CloseHandle(pi.hProcess);
		CloseHandle(job);
		if (err)
			*err = "AssignProcessToJobObject failed (" + std::to_string(e) + ")";
		return false;
	}
	ResumeThread(pi.hThread);
	CloseHandle(pi.hThread);
	process_ = pi.hProcess;
	job_ = job;
	pid_ = pi.dwProcessId;
	exit_code_ = 0;
	return true;
#else
	// Everything the child needs is prepared before fork(): OBS is multi-threaded,
	// so the child may only call async-signal-safe functions until exec.
	const unsigned long parent = current_process_id();
	std::vector<char *> argv;
	for (auto &a : cmd.argv)
		argv.push_back(const_cast<char *>(a.c_str()));
	argv.push_back(nullptr);
	std::vector<std::string> env_storage;
	for (char **e = environ; e && *e; e++)
		if (std::strncmp(*e, "AVATAR_LIVE_AUTH_TOKEN=", 23) != 0)
			env_storage.emplace_back(*e);
	env_storage.push_back("AVATAR_LIVE_AUTH_TOKEN=" + opts.auth_token);
	std::vector<char *> envp;
	for (auto &e : env_storage)
		envp.push_back(const_cast<char *>(e.c_str()));
	envp.push_back(nullptr);
	const char *cwd = cmd.working_dir.empty() ? nullptr : cmd.working_dir.c_str();

	pid_t child = fork();
	if (child < 0) {
		if (err)
			*err = "fork failed: " + std::string(std::strerror(errno));
		return false;
	}
	if (child == 0) {
#ifdef __linux__
		prctl(PR_SET_PDEATHSIG, SIGTERM); // mimic the Windows job object
		if (static_cast<unsigned long>(getppid()) != parent)
			_exit(127);
#else
		(void)parent;
#endif
		if (cwd && chdir(cwd) != 0)
			_exit(126);
		environ = envp.data();
		execvp(argv[0], argv.data());
		_exit(127);
	}
	child_ = child;
	pid_ = static_cast<unsigned long>(child);
	exit_code_ = 0;
	return true;
#endif
}

bool EngineProcess::running()
{
#ifdef _WIN32
	if (!process_)
		return false;
	DWORD code = 0;
	if (WaitForSingleObject(static_cast<HANDLE>(process_), 0) == WAIT_TIMEOUT)
		return true;
	GetExitCodeProcess(static_cast<HANDLE>(process_), &code);
	exit_code_ = static_cast<int>(code);
	CloseHandle(static_cast<HANDLE>(process_));
	process_ = nullptr;
	if (job_) {
		CloseHandle(static_cast<HANDLE>(job_));
		job_ = nullptr;
	}
	return false;
#else
	if (child_ <= 0)
		return false;
	int status = 0;
	pid_t r = waitpid(child_, &status, WNOHANG);
	if (r == 0)
		return true;
	if (r == child_)
		exit_code_ = WIFEXITED(status) ? WEXITSTATUS(status)
					       : 128 + (WIFSIGNALED(status) ? WTERMSIG(status) : 0);
	child_ = -1;
	return false;
#endif
}

void EngineProcess::terminate(uint32_t wait_ms)
{
#ifdef _WIN32
	if (process_) {
		HANDLE p = static_cast<HANDLE>(process_);
		if (WaitForSingleObject(p, wait_ms) == WAIT_TIMEOUT) {
			TerminateProcess(p, 1);
			WaitForSingleObject(p, 2000);
		}
		DWORD code = 0;
		GetExitCodeProcess(p, &code);
		exit_code_ = static_cast<int>(code);
		CloseHandle(p);
		process_ = nullptr;
	}
	if (job_) {
		CloseHandle(static_cast<HANDLE>(job_)); // kills anything left in the job
		job_ = nullptr;
	}
#else
	if (child_ > 0) {
		const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(wait_ms);
		while (running() && std::chrono::steady_clock::now() < deadline)
			std::this_thread::sleep_for(std::chrono::milliseconds(20));
		if (child_ > 0) {
			kill(child_, SIGTERM);
			const auto hard = std::chrono::steady_clock::now() + std::chrono::milliseconds(1000);
			while (running() && std::chrono::steady_clock::now() < hard)
				std::this_thread::sleep_for(std::chrono::milliseconds(20));
			if (child_ > 0) {
				kill(child_, SIGKILL);
				int st;
				waitpid(child_, &st, 0);
				child_ = -1;
			}
		}
	}
#endif
	pid_ = 0;
}

} // namespace avatar
