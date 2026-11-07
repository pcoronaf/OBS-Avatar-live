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

#include "json_lite.hpp"

#include <obs-module.h>

namespace avatar {

constexpr const char *kSourceId = "avatar_live_source";

void register_avatar_source();

// Builds the session config (contracts/config.schema.json) from source settings.
json::Value build_session_config(obs_data_t *settings);

} // namespace avatar
