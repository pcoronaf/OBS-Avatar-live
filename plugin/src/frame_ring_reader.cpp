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

#include "frame_ring_reader.hpp"
#include "platform_clock.hpp"

#include <atomic>
#include <chrono>
#include <cerrno>
#include <cstring>
#include <thread>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace avatar {

namespace {

// The ring lives in memory shared with another process, so plain C++ atomics
// cannot be used on it directly (std::atomic_ref is C++20). Aligned 32/64-bit
// volatile loads are single instructions on x86-64; the fences order them
// against the bulk copy (seqlock pattern, see PROTOCOL.md §2).
inline uint64_t load_u64(const uint8_t *p)
{
	return *reinterpret_cast<const volatile uint64_t *>(p);
}
inline uint32_t load_u32(const uint8_t *p)
{
	return *reinterpret_cast<const volatile uint32_t *>(p);
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
#endif

} // namespace

std::string ring_mapping_name(const std::string &session)
{
#ifdef _WIN32
	return "Local\\AvatarLive_" + session + "_frames";
#else
	return "/avatarlive_" + session + "_frames";
#endif
}

std::string ring_event_name(const std::string &session)
{
#ifdef _WIN32
	return "Local\\AvatarLive_" + session + "_ready";
#else
	(void)session;
	return std::string();
#endif
}

const char *read_result_name(ReadResult r)
{
	switch (r) {
	case ReadResult::NotOpen:
		return "not_open";
	case ReadResult::NoNewFrame:
		return "no_new_frame";
	case ReadResult::NewFrame:
		return "new_frame";
	case ReadResult::Busy:
		return "busy";
	case ReadResult::Rejected:
		return "rejected";
	}
	return "unknown";
}

FrameRingReader::~FrameRingReader()
{
	close();
}

bool FrameRingReader::open(const std::string &session, std::string *err)
{
	close();
	if (session.empty() || session.size() > 64) {
		if (err)
			*err = "invalid session token";
		return false;
	}
	for (char c : session) {
		bool ok = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
		if (!ok) {
			if (err)
				*err = "invalid session token";
			return false;
		}
	}

#ifdef _WIN32
	std::wstring name = widen(ring_mapping_name(session));
	HANDLE mapping = OpenFileMappingW(FILE_MAP_READ, FALSE, name.c_str());
	if (!mapping) {
		if (err)
			*err = "OpenFileMappingW failed (" + std::to_string(GetLastError()) + ")";
		return false;
	}
	void *view = MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 0);
	if (!view) {
		if (err)
			*err = "MapViewOfFile failed (" + std::to_string(GetLastError()) + ")";
		CloseHandle(mapping);
		return false;
	}
	MEMORY_BASIC_INFORMATION mbi = {};
	if (VirtualQuery(view, &mbi, sizeof(mbi)) == 0) {
		UnmapViewOfFile(view);
		CloseHandle(mapping);
		if (err)
			*err = "VirtualQuery failed";
		return false;
	}
	mapping_ = mapping;
	base_ = static_cast<const uint8_t *>(view);
	mapped_size_ = mbi.RegionSize;
	// The event is optional: without it the reader still works by polling.
	std::wstring ev = widen(ring_event_name(session));
	event_ = OpenEventW(SYNCHRONIZE, FALSE, ev.c_str());
#else
	std::string name = ring_mapping_name(session);
	int fd = shm_open(name.c_str(), O_RDONLY, 0);
	if (fd < 0) {
		if (err)
			*err = "shm_open failed: " + std::string(std::strerror(errno));
		return false;
	}
	struct stat st;
	if (fstat(fd, &st) != 0 || st.st_size < static_cast<off_t>(ring::kHeaderSize)) {
		::close(fd);
		if (err)
			*err = "shared memory too small";
		return false;
	}
	void *view = mmap(nullptr, static_cast<size_t>(st.st_size), PROT_READ, MAP_SHARED, fd, 0);
	if (view == MAP_FAILED) {
		::close(fd);
		if (err)
			*err = "mmap failed: " + std::string(std::strerror(errno));
		return false;
	}
	fd_ = fd;
	base_ = static_cast<const uint8_t *>(view);
	mapped_size_ = static_cast<size_t>(st.st_size);
#endif

	if (!validate_header(err)) {
		close();
		return false;
	}
	last_sequence_ = 0;
	last_generation_ = 0;
	return true;
}

bool FrameRingReader::validate_header(std::string *err)
{
	auto fail = [&](const char *m) {
		if (err)
			*err = m;
		return false;
	};
	if (mapped_size_ < ring::kHeaderSize)
		return fail("mapping smaller than ring header");
	if (load_u32(base_ + ring::kOffMagic) != ring::kMagic)
		return fail("bad ring magic");
	if (load_u32(base_ + ring::kOffVersion) != ring::kVersion)
		return fail("unsupported ring version");
	if (load_u32(base_ + ring::kOffHeaderSize) != ring::kHeaderSize)
		return fail("unexpected ring header size");
	if (load_u32(base_ + ring::kOffSlotHeaderSize) != ring::kSlotHeaderSize)
		return fail("unexpected slot header size");
	slot_count_ = load_u32(base_ + ring::kOffSlotCount);
	if (slot_count_ < 3 || slot_count_ > 8)
		return fail("slot_count out of range");
	slot_capacity_ = load_u64(base_ + ring::kOffSlotDataCapacity);
	if (slot_capacity_ == 0 || slot_capacity_ % 64 != 0 || slot_capacity_ > (64ull << 20))
		return fail("bad slot capacity");
	max_width_ = load_u32(base_ + ring::kOffMaxWidth);
	max_height_ = load_u32(base_ + ring::kOffMaxHeight);
	if (max_width_ == 0 || max_height_ == 0 || max_width_ > 8192 || max_height_ > 8192)
		return fail("bad max dimensions");
	const uint64_t total = ring::kHeaderSize + uint64_t(slot_count_) * (ring::kSlotHeaderSize + slot_capacity_);
	if (total > mapped_size_)
		return fail("mapping smaller than declared ring size");
	return true;
}

void FrameRingReader::close()
{
#ifdef _WIN32
	if (base_)
		UnmapViewOfFile(const_cast<uint8_t *>(base_));
	if (mapping_)
		CloseHandle(static_cast<HANDLE>(mapping_));
	if (event_)
		CloseHandle(static_cast<HANDLE>(event_));
	mapping_ = nullptr;
	event_ = nullptr;
#else
	if (base_)
		munmap(const_cast<uint8_t *>(base_), mapped_size_);
	if (fd_ >= 0)
		::close(fd_);
	fd_ = -1;
#endif
	base_ = nullptr;
	mapped_size_ = 0;
	slot_count_ = 0;
}

uint64_t FrameRingReader::heartbeat_ns() const
{
	return base_ ? load_u64(base_ + ring::kOffHeartbeatNs) : 0;
}

uint32_t FrameRingReader::ring_flags() const
{
	return base_ ? load_u32(base_ + ring::kOffRingFlags) : 0;
}

uint64_t FrameRingReader::generation_id() const
{
	return base_ ? load_u64(base_ + ring::kOffGenerationId) : 0;
}

uint32_t FrameRingReader::writer_pid() const
{
	return base_ ? load_u32(base_ + ring::kOffWriterPid) : 0;
}

ReadResult FrameRingReader::try_read(std::vector<uint8_t> &dst, FrameInfo &info)
{
	if (!base_)
		return ReadResult::NotOpen;

	// A new generation (session restart inside the same engine) may restart sequences.
	const uint64_t gen = generation_id();
	if (gen != last_generation_) {
		last_generation_ = gen;
		last_sequence_ = 0;
	}

	const uint32_t slot = load_u32(base_ + ring::kOffLatestSlot);
	std::atomic_thread_fence(std::memory_order_acquire);
	if (slot == ring::kNoSlot)
		return ReadResult::NoNewFrame;
	if (slot >= slot_count_) {
		rejected_++;
		return ReadResult::Rejected;
	}
	const uint64_t latest_seq = load_u64(base_ + ring::kOffLatestSequence);
	if (latest_seq == 0 || latest_seq == last_sequence_)
		return ReadResult::NoNewFrame;

	const uint8_t *sh = base_ + ring::kHeaderSize + size_t(slot) * (ring::kSlotHeaderSize + slot_capacity_);
	const uint8_t *data = sh + ring::kSlotHeaderSize;

	const uint64_t s1 = load_u64(sh + ring::kSlotOffSeqlock);
	std::atomic_thread_fence(std::memory_order_acquire);
	if (s1 & 1u)
		return ReadResult::Busy;

	FrameInfo fi;
	fi.sequence = load_u64(sh + ring::kSlotOffSequence);
	fi.timestamp_ns = load_u64(sh + ring::kSlotOffTimestampNs);
	fi.generation_id = load_u64(sh + ring::kSlotOffGenerationId);
	fi.width = load_u32(sh + ring::kSlotOffWidth);
	fi.height = load_u32(sh + ring::kSlotOffHeight);
	fi.stride = load_u32(sh + ring::kSlotOffStride);
	fi.pixel_format = load_u32(sh + ring::kSlotOffPixelFormat);
	fi.flags = load_u32(sh + ring::kSlotOffFrameFlags);
	fi.processing_latency_us = load_u32(sh + ring::kSlotOffLatencyUs);
	const uint32_t data_size = load_u32(sh + ring::kSlotOffDataSize);

	// Validate before touching pixel data (values may be torn; re-checked by seqlock below).
	const bool dims_ok = fi.width > 0 && fi.height > 0 && fi.width <= max_width_ && fi.height <= max_height_ &&
			     fi.stride >= fi.width * 4u && uint64_t(fi.stride) * fi.height <= slot_capacity_ &&
			     uint64_t(fi.stride) * fi.height <= data_size && data_size <= slot_capacity_;
	if (!dims_ok) {
		std::atomic_thread_fence(std::memory_order_acquire);
		if (load_u64(sh + ring::kSlotOffSeqlock) != s1) {
			torn_++;
			return ReadResult::Busy;
		}
		rejected_++;
		last_sequence_ = fi.sequence;
		return ReadResult::Rejected;
	}

	if (fi.sequence <= last_sequence_)
		return ReadResult::NoNewFrame; // slot index observed before its sequence store

	const size_t row_bytes = size_t(fi.width) * 4u;
	dst.resize(row_bytes * fi.height);
	for (uint32_t y = 0; y < fi.height; y++)
		std::memcpy(dst.data() + y * row_bytes, data + size_t(y) * fi.stride, row_bytes);

	std::atomic_thread_fence(std::memory_order_acquire);
	const uint64_t s2 = load_u64(sh + ring::kSlotOffSeqlock);
	if (s1 != s2) {
		torn_++;
		return ReadResult::Busy;
	}

	// Track the sequence actually copied (latest_slot / latest_sequence are two
	// separate stores, so they can be observed mid-update).
	last_sequence_ = fi.sequence;

	if (fi.pixel_format != ring::kPixelFormatBGRA8 || !(fi.flags & ring::kFrameSyntheticLabel) ||
	    fi.generation_id != gen) {
		rejected_++;
		return ReadResult::Rejected;
	}

	info = fi;
	return ReadResult::NewFrame;
}

bool FrameRingReader::wait_ready(uint32_t timeout_ms)
{
	if (!base_)
		return false;
#ifdef _WIN32
	if (event_)
		return WaitForSingleObject(static_cast<HANDLE>(event_), timeout_ms) == WAIT_OBJECT_0;
#endif
	// Poll latest_sequence (POSIX, or Windows without event).
	const uint64_t deadline = monotonic_ns() + uint64_t(timeout_ms) * 1000000ull;
	while (monotonic_ns() < deadline) {
		if (load_u64(base_ + ring::kOffLatestSequence) != last_sequence_)
			return true;
		std::this_thread::sleep_for(std::chrono::milliseconds(2));
	}
	return false;
}

} // namespace avatar
