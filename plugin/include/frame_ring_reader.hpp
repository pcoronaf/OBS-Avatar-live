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

// Reader side of the shared-memory frame ring described in docs/PROTOCOL.md §2.
// No libobs dependency. try_read() never blocks.

#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace avatar {

namespace ring {
constexpr uint32_t kMagic = 0x4C415641u; // "AVAL"
constexpr uint32_t kVersion = 1;
constexpr uint32_t kHeaderSize = 256;
constexpr uint32_t kSlotHeaderSize = 128;
constexpr uint32_t kNoSlot = 0xFFFFFFFFu;
constexpr uint32_t kPixelFormatBGRA8 = 1;

// Ring header offsets
constexpr size_t kOffMagic = 0;
constexpr size_t kOffVersion = 4;
constexpr size_t kOffHeaderSize = 8;
constexpr size_t kOffSlotCount = 12;
constexpr size_t kOffSlotHeaderSize = 16;
constexpr size_t kOffSlotDataCapacity = 24;
constexpr size_t kOffMaxWidth = 32;
constexpr size_t kOffMaxHeight = 36;
constexpr size_t kOffLatestSlot = 40;
constexpr size_t kOffWriterPid = 44;
constexpr size_t kOffLatestSequence = 48;
constexpr size_t kOffGenerationId = 56;
constexpr size_t kOffHeartbeatNs = 64;
constexpr size_t kOffRingFlags = 72;

// Slot header offsets
constexpr size_t kSlotOffSeqlock = 0;
constexpr size_t kSlotOffSequence = 8;
constexpr size_t kSlotOffTimestampNs = 16;
constexpr size_t kSlotOffGenerationId = 24;
constexpr size_t kSlotOffWidth = 32;
constexpr size_t kSlotOffHeight = 36;
constexpr size_t kSlotOffStride = 40;
constexpr size_t kSlotOffPixelFormat = 44;
constexpr size_t kSlotOffFrameFlags = 48;
constexpr size_t kSlotOffLatencyUs = 52;
constexpr size_t kSlotOffDataSize = 56;

// ring_flags
constexpr uint32_t kRingEngineRunning = 1u << 0;
constexpr uint32_t kRingPaused = 1u << 1;
constexpr uint32_t kRingDegraded = 1u << 2;

// frame_flags
constexpr uint32_t kFrameSyntheticLabel = 1u << 0;
constexpr uint32_t kFrameRepeated = 1u << 1;
constexpr uint32_t kFrameDegraded = 1u << 2;
constexpr uint32_t kFramePaused = 1u << 3;
} // namespace ring

std::string ring_mapping_name(const std::string &session); // platform specific object name
std::string ring_event_name(const std::string &session);   // Windows only (empty on POSIX)

struct FrameInfo {
	uint64_t sequence = 0;
	uint64_t timestamp_ns = 0;
	uint64_t generation_id = 0;
	uint32_t width = 0;
	uint32_t height = 0;
	uint32_t stride = 0;
	uint32_t pixel_format = 0;
	uint32_t flags = 0;
	uint32_t processing_latency_us = 0;
};

enum class ReadResult {
	NotOpen,
	NoNewFrame,
	NewFrame,
	Busy,     // slot being written or torn copy; try again next tick
	Rejected, // failed validation (format, generation, label, size)
};

const char *read_result_name(ReadResult r);

class FrameRingReader {
public:
	FrameRingReader() = default;
	~FrameRingReader();
	FrameRingReader(const FrameRingReader &) = delete;
	FrameRingReader &operator=(const FrameRingReader &) = delete;

	bool open(const std::string &session, std::string *err = nullptr);
	void close();
	bool is_open() const { return base_ != nullptr; }

	// Copies the newest complete frame (if newer than the last one consumed)
	// into dst as tightly packed BGRA (width*4 bytes per row). Never blocks.
	ReadResult try_read(std::vector<uint8_t> &dst, FrameInfo &info);

	// Waits for the writer's ready event (Windows) or polls (POSIX).
	// Only for tools/tests and worker threads, never the OBS render thread.
	bool wait_ready(uint32_t timeout_ms);

	uint64_t heartbeat_ns() const;
	uint32_t ring_flags() const;
	uint64_t generation_id() const;
	uint32_t writer_pid() const;
	uint32_t max_width() const { return max_width_; }
	uint32_t max_height() const { return max_height_; }
	uint64_t last_sequence() const { return last_sequence_; }
	uint64_t rejected_count() const { return rejected_; }
	uint64_t torn_count() const { return torn_; }

private:
	bool validate_header(std::string *err);

	const uint8_t *base_ = nullptr;
	size_t mapped_size_ = 0;
	uint32_t slot_count_ = 0;
	uint64_t slot_capacity_ = 0;
	uint32_t max_width_ = 0;
	uint32_t max_height_ = 0;
	uint64_t last_sequence_ = 0;
	uint64_t last_generation_ = 0;
	uint64_t rejected_ = 0;
	uint64_t torn_ = 0;
#ifdef _WIN32
	void *mapping_ = nullptr; // HANDLE
	void *event_ = nullptr;   // HANDLE
#else
	int fd_ = -1;
#endif
};

} // namespace avatar
