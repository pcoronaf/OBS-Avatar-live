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

// GPU-side presentation of avatar frames. All functions except
// render_destroy() must be called from the OBS graphics thread (video_render).

#pragma once

#include <graphics/graphics.h>

#include <cstdint>
#include <vector>

namespace avatar {

enum class DisplayMode : int {
	Idle = 0,     // no session requested: neutral dark frame
	Live = 1,     // fresh frame from the engine
	Degraded = 2, // session requested but frames are stale / engine down: striped warning frame
};

struct RenderState {
	gs_texture_t *live = nullptr;
	uint32_t live_w = 0;
	uint32_t live_h = 0;
	gs_texture_t *placeholder = nullptr;
	uint32_t placeholder_w = 0;
	uint32_t placeholder_h = 0;
	DisplayMode placeholder_mode = DisplayMode::Idle;
};

// Uploads a tightly packed BGRA frame, (re)creating the dynamic texture if needed.
void render_upload(RenderState &rs, const uint8_t *bgra, uint32_t w, uint32_t h);
// Draws the live texture scaled to cx*cy. Returns false if there is no texture.
bool render_draw_live(RenderState &rs, uint32_t cx, uint32_t cy);
// Draws the idle / degraded placeholder scaled to cx*cy.
void render_draw_placeholder(RenderState &rs, DisplayMode mode, uint32_t cx, uint32_t cy);
// Releases textures; enters the graphics context itself. Safe from any thread.
void render_destroy(RenderState &rs);

// CPU generation of the placeholder pattern (exposed for tests).
void make_placeholder(std::vector<uint8_t> &out, DisplayMode mode, uint32_t w, uint32_t h);

} // namespace avatar
