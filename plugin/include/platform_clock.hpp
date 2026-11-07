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

#pragma once

#include <cstdint>

namespace avatar {

// Monotonic nanoseconds on the same clock the engine uses for timestamp_ns:
// Windows: QueryPerformanceCounter (== Python time.perf_counter_ns()).
// POSIX:   CLOCK_MONOTONIC (== Python time.perf_counter_ns() on Linux).
uint64_t monotonic_ns();

} // namespace avatar
