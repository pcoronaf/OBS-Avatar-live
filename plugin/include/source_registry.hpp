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

// Process-wide list of live Avatar Live sources, used by the dock to reach a
// source's EngineController without touching OBS objects across threads.

#pragma once

#include "engine_controller.hpp"

#include <obs.h>

#include <memory>
#include <string>
#include <vector>

namespace avatar {

struct SourceEntry {
	std::string name;
	std::shared_ptr<EngineController> controller;
	obs_weak_source_t *weak = nullptr; // owned reference; released by ~SourceEntry
	SourceEntry() = default;
	SourceEntry(const SourceEntry &o);
	SourceEntry &operator=(const SourceEntry &o);
	~SourceEntry();
};

void registry_add(obs_source_t *source, std::shared_ptr<EngineController> controller);
void registry_remove(obs_source_t *source);
std::vector<SourceEntry> registry_snapshot();

} // namespace avatar
