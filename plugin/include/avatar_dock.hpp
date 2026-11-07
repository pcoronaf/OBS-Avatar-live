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

// Optional "Avatar Live Control" dock (Qt6 + obs-frontend-api).
// Built only when ENABLE_QT and ENABLE_FRONTEND_API are ON; the source works without it.

#pragma once

namespace avatar {
void dock_create();
void dock_destroy();
} // namespace avatar
