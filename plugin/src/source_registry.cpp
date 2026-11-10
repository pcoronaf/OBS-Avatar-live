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

#include "source_registry.hpp"

#include <algorithm>
#include <mutex>

namespace avatar {

namespace {
struct Item {
	obs_weak_source_t *weak;
	std::shared_ptr<EngineController> controller;
};
std::mutex g_mutex;
std::vector<Item> g_items;
} // namespace

SourceEntry::SourceEntry(const SourceEntry &o) : name(o.name), controller(o.controller), weak(o.weak)
{
	if (weak)
		obs_weak_source_addref(weak);
}

SourceEntry &SourceEntry::operator=(const SourceEntry &o)
{
	if (this != &o) {
		if (o.weak)
			obs_weak_source_addref(o.weak);
		if (weak)
			obs_weak_source_release(weak);
		name = o.name;
		controller = o.controller;
		weak = o.weak;
	}
	return *this;
}

SourceEntry::~SourceEntry()
{
	if (weak)
		obs_weak_source_release(weak);
}

void registry_add(obs_source_t *source, std::shared_ptr<EngineController> controller)
{
	std::lock_guard<std::mutex> lock(g_mutex);
	g_items.push_back({obs_source_get_weak_source(source), std::move(controller)});
}

void registry_remove(obs_source_t *source)
{
	std::lock_guard<std::mutex> lock(g_mutex);
	for (auto it = g_items.begin(); it != g_items.end();) {
		if (obs_weak_source_references_source(it->weak, source)) {
			obs_weak_source_release(it->weak);
			it = g_items.erase(it);
		} else {
			++it;
		}
	}
}

std::vector<SourceEntry> registry_snapshot()
{
	std::vector<SourceEntry> out;
	std::lock_guard<std::mutex> lock(g_mutex);
	for (auto &item : g_items) {
		obs_source_t *src = obs_weak_source_get_source(item.weak);
		if (!src)
			continue;
		SourceEntry e;
		e.name = obs_source_get_name(src) ? obs_source_get_name(src) : "";
		e.controller = item.controller;
		obs_weak_source_addref(item.weak);
		e.weak = item.weak;
		obs_source_release(src);
		out.push_back(e);
	}
	return out;
}

} // namespace avatar
