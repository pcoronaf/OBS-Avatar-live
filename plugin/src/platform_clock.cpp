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

#include "platform_clock.hpp"

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#else
#include <time.h>
#endif

namespace avatar {

uint64_t monotonic_ns()
{
#ifdef _WIN32
	static const uint64_t freq = [] {
		LARGE_INTEGER f;
		QueryPerformanceFrequency(&f);
		return static_cast<uint64_t>(f.QuadPart);
	}();
	LARGE_INTEGER c;
	QueryPerformanceCounter(&c);
	const uint64_t counter = static_cast<uint64_t>(c.QuadPart);
	// Split to avoid overflow; matches CPython's _PyTime_MulDiv rounding closely enough (<1 ns).
	return (counter / freq) * 1000000000ull + ((counter % freq) * 1000000000ull) / freq;
#else
	timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return static_cast<uint64_t>(ts.tv_sec) * 1000000000ull + static_cast<uint64_t>(ts.tv_nsec);
#endif
}

} // namespace avatar
