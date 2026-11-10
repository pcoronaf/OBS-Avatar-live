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

#include "avatar_render.hpp"

#include <obs-module.h>

#include <algorithm>

namespace avatar {

void make_placeholder(std::vector<uint8_t> &out, DisplayMode mode, uint32_t w, uint32_t h)
{
	out.assign(size_t(w) * h * 4, 0);
	for (uint32_t y = 0; y < h; y++) {
		uint8_t *row = out.data() + size_t(y) * w * 4;
		for (uint32_t x = 0; x < w; x++) {
			uint8_t b = 0x12, g = 0x12, r = 0x12;
			const bool border = x < 2 || y < 2 || x + 2 >= w || y + 2 >= h;
			if (mode == DisplayMode::Degraded) {
				// Diagonal amber stripes: unmistakably "not a live picture".
				const bool stripe = ((x + y) / 12) % 2 == 0;
				if (stripe || border) {
					b = 0x00;
					g = 0x8C;
					r = 0xE6;
				} else {
					b = g = r = 0x1A;
				}
			} else if (border) {
				b = g = r = 0x30;
			}
			row[x * 4 + 0] = b;
			row[x * 4 + 1] = g;
			row[x * 4 + 2] = r;
			row[x * 4 + 3] = 0xFF;
		}
	}
}

void render_upload(RenderState &rs, const uint8_t *bgra, uint32_t w, uint32_t h)
{
	if (!bgra || w == 0 || h == 0)
		return;
	if (!rs.live || rs.live_w != w || rs.live_h != h) {
		if (rs.live)
			gs_texture_destroy(rs.live);
		rs.live = gs_texture_create(w, h, GS_BGRA, 1, nullptr, GS_DYNAMIC);
		rs.live_w = rs.live ? w : 0;
		rs.live_h = rs.live ? h : 0;
	}
	if (rs.live)
		gs_texture_set_image(rs.live, bgra, w * 4, false);
}

bool render_draw_live(RenderState &rs, uint32_t cx, uint32_t cy)
{
	if (!rs.live)
		return false;
	obs_source_draw(rs.live, 0, 0, cx, cy, false);
	return true;
}

void render_draw_placeholder(RenderState &rs, DisplayMode mode, uint32_t cx, uint32_t cy)
{
	// Generated at reduced resolution and scaled: cheap and resolution independent.
	const uint32_t pw = std::max<uint32_t>(16, std::min<uint32_t>(cx, 480));
	const uint32_t ph = std::max<uint32_t>(16, cy * pw / std::max<uint32_t>(cx, 1));
	if (!rs.placeholder || rs.placeholder_w != pw || rs.placeholder_h != ph || rs.placeholder_mode != mode) {
		if (rs.placeholder)
			gs_texture_destroy(rs.placeholder);
		std::vector<uint8_t> px;
		make_placeholder(px, mode, pw, ph);
		const uint8_t *data = px.data();
		rs.placeholder = gs_texture_create(pw, ph, GS_BGRA, 1, &data, 0);
		rs.placeholder_w = pw;
		rs.placeholder_h = ph;
		rs.placeholder_mode = mode;
	}
	if (rs.placeholder)
		obs_source_draw(rs.placeholder, 0, 0, cx, cy, false);
}

void render_destroy(RenderState &rs)
{
	if (!rs.live && !rs.placeholder)
		return;
	obs_enter_graphics();
	if (rs.live)
		gs_texture_destroy(rs.live);
	if (rs.placeholder)
		gs_texture_destroy(rs.placeholder);
	obs_leave_graphics();
	rs = RenderState();
}

} // namespace avatar
