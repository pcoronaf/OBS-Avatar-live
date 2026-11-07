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

// "Avatar Live" input source (spec §6). The source never runs ML: it starts
// the out-of-process engine, sends it the session config and presents the
// newest frame from the shared-memory ring without blocking the render thread.

#include "avatar_source.hpp"
#include "avatar_render.hpp"
#include "engine_controller.hpp"
#include "platform_clock.hpp"
#include "source_registry.hpp"

#include <plugin-support.h>
#include <util/platform.h>

#include <atomic>
#include <cinttypes>
#include <cstdio>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#define T_(key) obs_module_text(key)

namespace avatar {

namespace {

constexpr uint64_t kMs = 1000000ull;
constexpr uint64_t kHeartbeatTimeoutNs = 2000 * kMs;

struct EngineChoice {
	const char *id;
	const char *text_key;
	const char *drive_mode;
	bool available;
};

const EngineChoice kEngines[] = {
	{"lite_adaptive", "Engine.LiteAdaptive", "webcam", true},
	{"lite_cpu_audio", "Engine.LiteCpuAudio", "audio", true},
	{"lite_cpu_swap", "Engine.LiteCpuSwap", "webcam", true},
	{"accelerated_swap", "Engine.AcceleratedSwap", "webcam", true},
	{"synthetic_head", "Engine.SyntheticHead", "audio", false}, // phase 4, not available
};

const char *kUserProfiles[] = {"auto", "cpu_only", "intel_optimized", "max_performance", "low_power"};
const char *kUserProfileKeys[] = {"UserProfile.Auto", "UserProfile.CpuOnly", "UserProfile.IntelOptimized",
				  "UserProfile.MaxPerformance", "UserProfile.LowPower"};

struct Resolution {
	const char *id;
	int w, h;
};
const Resolution kResolutions[] = {{"1280x720", 1280, 720},
				   {"1920x1080", 1920, 1080},
				   {"960x540", 960, 540},
				   {"640x360", 640, 360}};

const char *kPositions[] = {"bottom_left", "bottom_right", "top_left", "top_right"};
const char *kPositionKeys[] = {"Disclosure.BottomLeft", "Disclosure.BottomRight", "Disclosure.TopLeft",
			       "Disclosure.TopRight"};

void parse_resolution(const char *id, int &w, int &h)
{
	w = 1280;
	h = 720;
	if (!id)
		return;
	for (const auto &r : kResolutions)
		if (strcmp(r.id, id) == 0) {
			w = r.w;
			h = r.h;
			return;
		}
}

const EngineChoice *find_engine(const char *id)
{
	for (const auto &e : kEngines)
		if (id && strcmp(e.id, id) == 0)
			return &e;
	return &kEngines[0];
}

bool valid_profile_id(const char *id)
{
	if (!id || !*id)
		return false;
	size_t n = 0;
	for (const char *p = id; *p; p++, n++) {
		const char c = *p;
		if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' ||
		      c == '-'))
			return false;
	}
	return n <= 64;
}

std::string color_to_hex(long long abgr)
{
	// OBS color properties store 0xAABBGGRR.
	char buf[8];
	snprintf(buf, sizeof(buf), "#%02X%02X%02X", unsigned(abgr & 0xFF), unsigned((abgr >> 8) & 0xFF),
		 unsigned((abgr >> 16) & 0xFF));
	return buf;
}

json::Value obs_to_json(obs_data_t *data)
{
	json::Value v;
	const char *text = obs_data_get_json(data);
	if (!text || !json::Value::parse(text, v))
		return json::Value::object();
	return v;
}

std::string fmt_num(const json::Value &v, const char *suffix = "", int decimals = 1)
{
	if (!v.is_number())
		return "-";
	char buf[48];
	snprintf(buf, sizeof(buf), "%.*f%s", decimals, v.as_number(), suffix);
	return buf;
}

std::string default_engine_path()
{
#ifdef _WIN32
	const char *rel = "engine/avatar-engine.exe";
#else
	const char *rel = "engine/avatar-engine";
#endif
	char *p = obs_module_file(rel);
	std::string out = p ? p : "";
	bfree(p);
	return out;
}

struct AvatarSource {
	obs_source_t *source = nullptr;
	std::shared_ptr<EngineController> ctl;
	RenderState render;

	std::mutex frame_mutex; // video_tick <-> video_render (both graphics thread today; cheap insurance)
	std::vector<uint8_t> staging;
	uint32_t staging_w = 0, staging_h = 0;
	bool staging_new = false;
	std::vector<uint8_t> scratch;

	std::atomic<int> mode{static_cast<int>(DisplayMode::Idle)};
	std::atomic<uint32_t> out_w{1280}, out_h{720};
	std::atomic<uint64_t> stale_ns{500 * kMs};
	bool have_frame = false;
	uint64_t last_frame_ts = 0;
	bool frozen = false;

	std::mutex msg_mutex;
	std::string message; // feedback from the last user action
	std::string engine_path_in_use;

	void set_message(const std::string &m)
	{
		std::lock_guard<std::mutex> lock(msg_mutex);
		message = m;
	}
	std::string get_message()
	{
		std::lock_guard<std::mutex> lock(msg_mutex);
		return message;
	}
};

// ---------------------------------------------------------------------------
// Profile creation result marshalled to the UI thread

struct ProfileTask {
	obs_weak_source_t *weak;
	bool ok;
	bool deleted;
	std::string profile_id;
	std::string message;
};

void profile_task_ui(void *param)
{
	std::unique_ptr<ProfileTask> t(static_cast<ProfileTask *>(param));
	obs_source_t *src = obs_weak_source_get_source(t->weak);
	obs_weak_source_release(t->weak);
	if (!src)
		return;
	auto *s = static_cast<AvatarSource *>(obs_obj_get_data(src));
	if (t->ok) {
		obs_data_t *d = obs_data_create();
		obs_data_set_string(d, "profile_id", t->deleted ? "" : t->profile_id.c_str());
		obs_source_update(src, d);
		obs_data_release(d);
	}
	if (s)
		s->set_message(t->message);
	obs_source_update_properties(src);
	obs_source_release(src);
}

void queue_profile_result(obs_source_t *source, const ReplyResult &r, bool deleted)
{
	auto *t = new ProfileTask();
	t->weak = obs_source_get_weak_source(source);
	t->ok = r.ok;
	t->deleted = deleted;
	if (r.ok) {
		t->profile_id = r.payload["profile_id"].as_string("");
		std::string msg = deleted ? T_("Msg.ProfileDeleted") : T_("Msg.ProfileCreated");
		const json::Value &warnings = r.payload["warnings"];
		for (size_t i = 0; i < warnings.size(); i++)
			msg += "\n- " + warnings.at(i).as_string("");
		if (!deleted && !valid_profile_id(t->profile_id.c_str())) {
			t->ok = false;
			msg = "engine returned an invalid profile_id";
		}
		t->message = msg;
	} else {
		t->message = std::string(T_("Msg.ProfileFailed")) + " " + r.error_code + ": " + r.error_message;
	}
	obs_queue_task(OBS_TASK_UI, profile_task_ui, t, false);
}

// ---------------------------------------------------------------------------
// obs_source_info callbacks

const char *avatar_get_name(void *)
{
	return T_("AvatarLive");
}

void apply_settings(AvatarSource *s, obs_data_t *settings)
{
	int w, h;
	parse_resolution(obs_data_get_string(settings, "resolution"), w, h);
	s->out_w = static_cast<uint32_t>(w);
	s->out_h = static_cast<uint32_t>(h);
	long long stale = obs_data_get_int(settings, "stale_frame_ms");
	if (stale < 100 || stale > 5000)
		stale = 500;
	s->stale_ns = static_cast<uint64_t>(stale) * kMs;

	std::string path = obs_data_get_string(settings, "engine_path");
	if (path.empty())
		path = default_engine_path();
	std::string python = obs_data_get_string(settings, "engine_python");
	s->engine_path_in_use = path;
	s->ctl->set_engine(path, python);

	s->ctl->set_session_config(build_session_config(settings));
}

void *avatar_create(obs_data_t *settings, obs_source_t *source)
{
	auto *s = new AvatarSource();
	s->source = source;
	s->ctl = std::make_shared<EngineController>(
		[](int level, const std::string &msg) {
			int l = level >= EngineController::Error     ? LOG_ERROR
				: level == EngineController::Warning ? LOG_WARNING
				: level == EngineController::Info    ? LOG_INFO
								     : LOG_DEBUG;
			obs_log(l, "%s", msg.c_str());
		},
		PLUGIN_VERSION);
	apply_settings(s, settings);
	registry_add(source, s->ctl);
	return s;
}

void avatar_destroy(void *data)
{
	auto *s = static_cast<AvatarSource *>(data);
	registry_remove(s->source);
	s->ctl->shutdown(); // bounded; the Job object guarantees the engine dies with OBS
	render_destroy(s->render);
	delete s;
}

void avatar_update(void *data, obs_data_t *settings)
{
	apply_settings(static_cast<AvatarSource *>(data), settings);
}

uint32_t avatar_get_width(void *data)
{
	return static_cast<AvatarSource *>(data)->out_w;
}

uint32_t avatar_get_height(void *data)
{
	return static_cast<AvatarSource *>(data)->out_h;
}

void avatar_video_tick(void *data, float)
{
	auto *s = static_cast<AvatarSource *>(data);
	FrameInfo info;
	// Non-blocking seqlock copy (see PROTOCOL.md §2); never waits on the engine.
	if (s->ctl->try_read_frame(s->scratch, info) == ReadResult::NewFrame) {
		{
			std::lock_guard<std::mutex> lock(s->frame_mutex);
			s->staging.swap(s->scratch);
			s->staging_w = info.width;
			s->staging_h = info.height;
			s->staging_new = true;
		}
		s->have_frame = true;
		s->last_frame_ts = info.timestamp_ns;
	}

	DisplayMode mode;
	if (!s->ctl->session_requested()) {
		s->have_frame = false;
		mode = DisplayMode::Idle;
	} else {
		const uint64_t now = monotonic_ns();
		const uint64_t hb = s->ctl->heartbeat_ns();
		const uint64_t frame_age = (s->have_frame && now > s->last_frame_ts) ? now - s->last_frame_ts : 0;
		const uint64_t hb_age = (hb && now > hb) ? now - hb : 0;
		const bool fresh = s->have_frame && frame_age <= s->stale_ns.load() && hb != 0 &&
				   hb_age <= kHeartbeatTimeoutNs;
		// Never keep presenting an old face as if it were current (spec §5 E2a).
		mode = fresh ? DisplayMode::Live : DisplayMode::Degraded;
	}
	s->mode = static_cast<int>(mode);
}

void avatar_video_render(void *data, gs_effect_t *)
{
	auto *s = static_cast<AvatarSource *>(data);
	{
		std::lock_guard<std::mutex> lock(s->frame_mutex);
		if (s->staging_new) {
			render_upload(s->render, s->staging.data(), s->staging_w, s->staging_h);
			s->staging_new = false;
		}
	}
	const uint32_t cx = s->out_w, cy = s->out_h;
	const auto mode = static_cast<DisplayMode>(s->mode.load());
	if (mode == DisplayMode::Live && render_draw_live(s->render, cx, cy))
		return;
	render_draw_placeholder(s->render, mode == DisplayMode::Live ? DisplayMode::Degraded : mode, cx, cy);
}

// ---------------------------------------------------------------------------
// Properties

std::string status_text(AvatarSource *s)
{
	ControllerStatus st = s->ctl->status();
	const json::Value &m = st.metrics;
	std::string t;
	t += std::string(T_("Status.State")) + ": " + st.state;
	if (!st.detail.empty())
		t += " (" + st.detail + ")";
	t += "\n" + std::string(T_("Status.Engine")) + ": " +
	     (st.engine_version.empty() ? std::string("-") : st.engine_version) + "  [" + s->engine_path_in_use + "]";
	t += "\n" + std::string(T_("Status.Provider")) + ": " +
	     m["execution_provider_selected"].as_string(st.provider.empty() ? "-" : st.provider) + " / " +
	     m["provider_device"].as_string("-");
	t += "\n" + std::string(T_("Status.UniqueFps")) + ": " + fmt_num(m["face_swap_unique_fps"]) + "   " +
	     T_("Status.InferenceFps") + ": " + fmt_num(m["inference_fps"]) + "   " + T_("Status.OutputFps") + ": " +
	     fmt_num(m["output_fps"]);
	t += "\n" + std::string(T_("Status.Repeated")) + ": " + fmt_num(m["repeated_frame_pct"], " %") +
	     "   P50/P95: " + fmt_num(m["capture_to_output_p50_ms"], " ms", 0) + " / " +
	     fmt_num(m["capture_to_output_p95_ms"], " ms", 0);
	t += "\nCPU: " + fmt_num(m["cpu_utilization_pct"], " %", 0) + "   RAM: " + fmt_num(m["ram_mb"], " MB", 0) +
	     "   " + T_("Status.Restarts") + ": " + std::to_string(st.restarts);
	if (!st.fallback_reason.empty())
		t += "\n" + std::string(T_("Status.Fallback")) + ": " + st.fallback_reason;
	if (!st.last_error.empty())
		t += "\n" + std::string(T_("Status.Error")) + ": " + st.last_error;
	std::string msg = s->get_message();
	if (!msg.empty())
		t += "\n> " + msg;
	t += "\n" + std::string(T_("Status.SyntheticNotice"));
	return t;
}

bool consent_modified(obs_properties_t *props, obs_property_t *, obs_data_t *settings)
{
	const bool rights = obs_data_get_bool(settings, "consent_rights");
	const bool self = obs_data_get_bool(settings, "consent_subject_is_self");
	obs_property_set_enabled(obs_properties_get(props, "create_profile"), rights);
	obs_property_set_visible(obs_properties_get(props, "consent_reference"), !self);
	return true;
}

bool on_create_profile(obs_properties_t *, obs_property_t *, void *data)
{
	auto *s = static_cast<AvatarSource *>(data);
	obs_data_t *settings = obs_source_get_settings(s->source);
	const bool rights = obs_data_get_bool(settings, "consent_rights");
	const bool self = obs_data_get_bool(settings, "consent_subject_is_self");
	std::string photo = obs_data_get_string(settings, "photo_path");
	std::string name = obs_data_get_string(settings, "display_name");
	std::string reference = obs_data_get_string(settings, "consent_reference");
	obs_data_release(settings);

	// Spec §8: explicit confirmation of the right to use the photo is mandatory.
	if (!rights) {
		s->set_message(T_("Msg.ConsentRequired"));
		return true;
	}
	if (!self && reference.empty()) {
		s->set_message(T_("Msg.ThirdPartyConsentRequired"));
		return true;
	}
	if (photo.empty()) {
		s->set_message(T_("Msg.PhotoRequired"));
		return true;
	}
	json::Value consent = json::Value::object();
	consent.set("rights_confirmed", true);
	consent.set("subject_is_self", self);
	if (!self)
		consent.set("third_party_consent_reference", reference);
	json::Value payload = json::Value::object();
	payload.set("photo_path", photo);
	payload.set("display_name", name.empty() ? std::string("Avatar") : name);
	payload.set("consent", consent);

	obs_source_t *source = s->source;
	s->set_message(T_("Msg.CreatingProfile"));
	s->ctl->create_profile(payload, [source](const ReplyResult &r) { queue_profile_result(source, r, false); });
	return true;
}

bool on_delete_profile(obs_properties_t *, obs_property_t *, void *data)
{
	auto *s = static_cast<AvatarSource *>(data);
	obs_data_t *settings = obs_source_get_settings(s->source);
	std::string id = obs_data_get_string(settings, "profile_id");
	obs_data_release(settings);
	if (!valid_profile_id(id.c_str())) {
		s->set_message(T_("Msg.NoProfile"));
		return true;
	}
	obs_source_t *source = s->source;
	s->ctl->delete_profile(id, [source](const ReplyResult &r) { queue_profile_result(source, r, true); });
	return true;
}

bool on_start(obs_properties_t *, obs_property_t *, void *data)
{
	auto *s = static_cast<AvatarSource *>(data);
	obs_data_t *settings = obs_source_get_settings(s->source);
	const bool ok = valid_profile_id(obs_data_get_string(settings, "profile_id"));
	const EngineChoice *engine = find_engine(obs_data_get_string(settings, "engine"));
	obs_data_release(settings);
	if (!engine->available) {
		s->set_message(T_("Msg.EngineUnavailable"));
		return true;
	}
	if (!ok) {
		s->set_message(T_("Msg.NoProfile"));
		return true;
	}
	s->frozen = false;
	s->set_message(T_("Msg.Starting"));
	s->ctl->start_session();
	return true;
}

bool on_stop(obs_properties_t *, obs_property_t *, void *data)
{
	auto *s = static_cast<AvatarSource *>(data);
	s->ctl->stop_session();
	s->set_message(T_("Msg.Stopped"));
	return true;
}

bool on_pause(obs_properties_t *, obs_property_t *, void *data)
{
	static_cast<AvatarSource *>(data)->ctl->pause();
	return true;
}

bool on_resume(obs_properties_t *, obs_property_t *, void *data)
{
	static_cast<AvatarSource *>(data)->ctl->resume();
	return true;
}

bool on_freeze(obs_properties_t *, obs_property_t *, void *data)
{
	auto *s = static_cast<AvatarSource *>(data);
	s->frozen = !s->frozen;
	s->ctl->freeze_neutral(s->frozen);
	s->set_message(s->frozen ? T_("Msg.Frozen") : T_("Msg.Unfrozen"));
	return true;
}

bool on_refresh_devices(obs_properties_t *, obs_property_t *, void *data)
{
	static_cast<AvatarSource *>(data)->ctl->request_devices();
	return true;
}

bool on_refresh_status(obs_properties_t *, obs_property_t *, void *)
{
	return true; // returning true rebuilds the properties view with fresh status
}

void add_device_list(obs_properties_t *grp, const char *name, const char *text, const json::Value &devices)
{
	obs_property_t *p = obs_properties_add_list(grp, name, text, OBS_COMBO_TYPE_EDITABLE, OBS_COMBO_FORMAT_STRING);
	obs_property_list_add_string(p, T_("Device.Default"), "");
	for (size_t i = 0; i < devices.size(); i++) {
		const json::Value &d = devices.at(i);
		std::string id = d["id"].is_number() ? std::to_string(d["id"].as_int()) : d["id"].as_string("");
		std::string label = d["name"].as_string(id);
		obs_property_list_add_string(p, label.c_str(), id.c_str());
	}
}

obs_properties_t *avatar_get_properties(void *data)
{
	auto *s = static_cast<AvatarSource *>(data);
	obs_properties_t *props = obs_properties_create();
	ControllerStatus st = s->ctl->status();

	// Status / control first: what the user needs during a meeting.
	obs_properties_t *ctl = obs_properties_create();
	obs_property_t *status = obs_properties_add_text(ctl, "status_info", status_text(s).c_str(), OBS_TEXT_INFO);
	obs_property_text_set_info_type(status, st.last_error.empty() ? OBS_TEXT_INFO_NORMAL : OBS_TEXT_INFO_WARNING);
	obs_properties_add_button(ctl, "start", T_("Control.Start"), on_start);
	obs_properties_add_button(ctl, "stop", T_("Control.Stop"), on_stop);
	obs_properties_add_button(ctl, "pause", T_("Control.Pause"), on_pause);
	obs_properties_add_button(ctl, "resume", T_("Control.Resume"), on_resume);
	obs_properties_add_button(ctl, "freeze", T_("Control.Freeze"), on_freeze);
	obs_properties_add_button(ctl, "refresh_status", T_("Control.RefreshStatus"), on_refresh_status);
	obs_properties_add_group(props, "grp_control", T_("Group.Control"), OBS_GROUP_NORMAL, ctl);

	// Profile + consent (spec §2.1, §8)
	obs_properties_t *prof = obs_properties_create();
	obs_properties_add_path(prof, "photo_path", T_("Profile.Photo"), OBS_PATH_FILE,
				"Images (*.png *.jpg *.jpeg *.webp)", nullptr);
	obs_properties_add_text(prof, "display_name", T_("Profile.DisplayName"), OBS_TEXT_DEFAULT);
	obs_property_t *rights = obs_properties_add_bool(prof, "consent_rights", T_("Profile.ConsentRights"));
	obs_property_set_modified_callback(rights, consent_modified);
	obs_property_t *self = obs_properties_add_bool(prof, "consent_subject_is_self", T_("Profile.SubjectIsSelf"));
	obs_property_set_modified_callback(self, consent_modified);
	obs_properties_add_text(prof, "consent_reference", T_("Profile.ConsentReference"), OBS_TEXT_DEFAULT);
	obs_properties_add_button(prof, "create_profile", T_("Profile.Create"), on_create_profile);
	obs_property_t *pid = obs_properties_add_text(prof, "profile_id", T_("Profile.Id"), OBS_TEXT_DEFAULT);
	obs_property_set_enabled(pid, false); // filled by the engine
	obs_properties_add_button(prof, "delete_profile", T_("Profile.Delete"), on_delete_profile);
	obs_properties_add_group(props, "grp_profile", T_("Group.Profile"), OBS_GROUP_NORMAL, prof);

	// Engine and inputs
	obs_properties_t *eng = obs_properties_create();
	obs_property_t *elist =
		obs_properties_add_list(eng, "engine", T_("Engine"), OBS_COMBO_TYPE_LIST, OBS_COMBO_FORMAT_STRING);
	for (size_t i = 0; i < sizeof(kEngines) / sizeof(kEngines[0]); i++) {
		obs_property_list_add_string(elist, T_(kEngines[i].text_key), kEngines[i].id);
		if (!kEngines[i].available)
			obs_property_list_item_disable(elist, i, true);
	}
	obs_property_set_long_description(elist, T_("Engine.Description"));
	obs_property_t *ulist = obs_properties_add_list(eng, "user_profile", T_("UserProfile"), OBS_COMBO_TYPE_LIST,
							OBS_COMBO_FORMAT_STRING);
	for (size_t i = 0; i < sizeof(kUserProfiles) / sizeof(kUserProfiles[0]); i++)
		obs_property_list_add_string(ulist, T_(kUserProfileKeys[i]), kUserProfiles[i]);
	add_device_list(eng, "video_input", T_("Input.Webcam"), st.devices["video"]);
	add_device_list(eng, "audio_input", T_("Input.Microphone"), st.devices["audio"]);
	obs_properties_add_button(eng, "refresh_devices", T_("Input.RefreshDevices"), on_refresh_devices);
	obs_properties_add_group(props, "grp_engine", T_("Group.Engine"), OBS_GROUP_NORMAL, eng);

	// Output framing
	obs_properties_t *out = obs_properties_create();
	obs_property_t *res = obs_properties_add_list(out, "resolution", T_("Output.Resolution"), OBS_COMBO_TYPE_LIST,
						      OBS_COMBO_FORMAT_STRING);
	for (const auto &r : kResolutions)
		obs_property_list_add_string(res, r.id, r.id);
	obs_property_t *fps =
		obs_properties_add_list(out, "fps", T_("Output.Fps"), OBS_COMBO_TYPE_LIST, OBS_COMBO_FORMAT_INT);
	for (int f : {15, 24, 30, 60})
		obs_property_list_add_int(fps, std::to_string(f).c_str(), f);
	obs_property_t *bg = obs_properties_add_list(out, "background", T_("Output.Background"), OBS_COMBO_TYPE_LIST,
						     OBS_COMBO_FORMAT_STRING);
	obs_property_list_add_string(bg, T_("Output.Background.Photo"), "photo");
	obs_property_list_add_string(bg, T_("Output.Background.Solid"), "solid");
	obs_property_list_add_string(bg, T_("Output.Background.Transparent"), "transparent");
	obs_properties_add_color(out, "background_color", T_("Output.BackgroundColor"));
	obs_properties_add_float_slider(out, "zoom", T_("Output.Zoom"), 0.5, 3.0, 0.05);
	obs_properties_add_float_slider(out, "offset_x", T_("Output.OffsetX"), -1.0, 1.0, 0.01);
	obs_properties_add_float_slider(out, "offset_y", T_("Output.OffsetY"), -1.0, 1.0, 0.01);
	obs_properties_add_group(props, "grp_output", T_("Group.Output"), OBS_GROUP_NORMAL, out);

	// Audio drive
	obs_properties_t *aud = obs_properties_create();
	obs_properties_add_float_slider(aud, "audio_sensitivity", T_("Audio.Sensitivity"), 0.1, 5.0, 0.1);
	obs_property_t *delay = obs_properties_add_int_slider(aud, "mouth_delay_ms", T_("Audio.MouthDelay"), 0, 300, 5);
	obs_property_int_set_suffix(delay, " ms");
	obs_properties_add_group(props, "grp_audio", T_("Group.Audio"), OBS_GROUP_NORMAL, aud);

	// Performance budget
	obs_properties_t *perf = obs_properties_create();
	obs_property_t *budget = obs_properties_add_int_slider(perf, "cpu_budget_pct", T_("Perf.CpuBudget"), 5, 100, 5);
	obs_property_int_set_suffix(budget, " %");
	obs_properties_add_int(perf, "max_threads", T_("Perf.MaxThreads"), 1, 64, 1);
	obs_property_t *stale = obs_properties_add_int(perf, "stale_frame_ms", T_("Perf.StaleMs"), 100, 5000, 50);
	obs_property_int_set_suffix(stale, " ms");
	obs_properties_add_group(props, "grp_perf", T_("Group.Performance"), OBS_GROUP_NORMAL, perf);

	// Disclosure label: position and size only. It cannot be disabled (spec §8, ADR-006).
	obs_properties_t *dis = obs_properties_create();
	obs_properties_add_text(dis, "disclosure_info", T_("Disclosure.Info"), OBS_TEXT_INFO);
	obs_property_t *pos = obs_properties_add_list(dis, "disclosure_position", T_("Disclosure.Position"),
						      OBS_COMBO_TYPE_LIST, OBS_COMBO_FORMAT_STRING);
	for (size_t i = 0; i < 4; i++)
		obs_property_list_add_string(pos, T_(kPositionKeys[i]), kPositions[i]);
	obs_properties_add_float_slider(dis, "disclosure_scale", T_("Disclosure.Scale"), 1.0, 4.0, 0.1);
	obs_properties_add_group(props, "grp_disclosure", T_("Group.Disclosure"), OBS_GROUP_NORMAL, dis);

	// Advanced / developer
	obs_properties_t *adv = obs_properties_create();
	obs_properties_add_path(adv, "engine_path", T_("Advanced.EnginePath"), OBS_PATH_FILE,
				"avatar-engine (*.exe *.py);;All files (*.*)", nullptr);
	obs_properties_add_text(adv, "engine_python", T_("Advanced.EnginePython"), OBS_TEXT_DEFAULT);
	obs_properties_add_group(props, "grp_advanced", T_("Group.Advanced"), OBS_GROUP_NORMAL, adv);

	// Initial enable/visibility state
	obs_data_t *settings = obs_source_get_settings(s->source);
	consent_modified(props, nullptr, settings);
	obs_data_release(settings);
	return props;
}

void avatar_get_defaults(obs_data_t *settings)
{
	obs_data_set_default_string(settings, "engine", "lite_adaptive");
	obs_data_set_default_string(settings, "user_profile", "auto");
	obs_data_set_default_string(settings, "display_name", "Avatar");
	obs_data_set_default_bool(settings, "consent_rights", false);
	obs_data_set_default_bool(settings, "consent_subject_is_self", true);
	obs_data_set_default_string(settings, "resolution", "1280x720");
	obs_data_set_default_int(settings, "fps", 30);
	obs_data_set_default_string(settings, "background", "photo");
	obs_data_set_default_int(settings, "background_color", 0xFF202020);
	obs_data_set_default_double(settings, "zoom", 1.0);
	obs_data_set_default_double(settings, "offset_x", 0.0);
	obs_data_set_default_double(settings, "offset_y", 0.0);
	obs_data_set_default_double(settings, "audio_sensitivity", 1.0);
	obs_data_set_default_int(settings, "mouth_delay_ms", 60);
	obs_data_set_default_int(settings, "cpu_budget_pct", 60);
	obs_data_set_default_int(settings, "max_threads", 4);
	obs_data_set_default_int(settings, "stale_frame_ms", 500);
	obs_data_set_default_string(settings, "disclosure_position", "bottom_left");
	obs_data_set_default_double(settings, "disclosure_scale", 1.0);
}

} // namespace

json::Value build_session_config(obs_data_t *settings)
{
	const EngineChoice *engine = find_engine(obs_data_get_string(settings, "engine"));
	std::string user_profile = obs_data_get_string(settings, "user_profile");
	if (user_profile.empty())
		user_profile = "auto";
	const bool cpu_only = user_profile == "cpu_only";

	obs_data_t *cfg = obs_data_create();
	obs_data_set_int(cfg, "schema_version", 2);
	obs_data_set_string(cfg, "engine", engine->id);
	obs_data_set_string(cfg, "profile_id", obs_data_get_string(settings, "profile_id"));
	obs_data_set_string(cfg, "drive_mode", engine->drive_mode);
	obs_data_set_string(cfg, "user_profile", user_profile.c_str());
	const char *video = obs_data_get_string(settings, "video_input");
	if (video && *video)
		obs_data_set_string(cfg, "video_input", video);
	const char *audio = obs_data_get_string(settings, "audio_input");
	if (audio && *audio)
		obs_data_set_string(cfg, "audio_input", audio);

	int w, h;
	parse_resolution(obs_data_get_string(settings, "resolution"), w, h);
	obs_data_t *out = obs_data_create();
	obs_data_set_int(out, "width", w);
	obs_data_set_int(out, "height", h);
	obs_data_set_int(out, "fps", obs_data_get_int(settings, "fps"));
	obs_data_set_string(out, "background", obs_data_get_string(settings, "background"));
	obs_data_set_string(out, "background_color",
			    color_to_hex(obs_data_get_int(settings, "background_color")).c_str());
	obs_data_set_double(out, "zoom", obs_data_get_double(settings, "zoom"));
	obs_data_set_double(out, "offset_x", obs_data_get_double(settings, "offset_x"));
	obs_data_set_double(out, "offset_y", obs_data_get_double(settings, "offset_y"));
	obs_data_set_obj(cfg, "output", out);
	obs_data_release(out);

	obs_data_t *inf = obs_data_create();
	std::vector<const char *> order;
	if (cpu_only || user_profile == "low_power")
		order = {"cpu"};
	else if (user_profile == "intel_optimized")
		order = {"openvino", "cpu"};
	else if (user_profile == "max_performance" && strcmp(engine->id, "accelerated_swap") == 0)
		order = {"cuda", "directml", "openvino", "cpu"};
	else
		order = {"openvino", "directml", "cpu"};
	obs_data_set_string(inf, "provider", cpu_only ? "cpu" : "auto");
	obs_data_set_bool(inf, "cpu_only", cpu_only);
	obs_data_set_bool(inf, "startup_benchmark", true);
	obs_data_set_bool(inf, "enhancement", false);
	obs_data_set_bool(inf, "adaptive_quality", true);
	long long threads = obs_data_get_int(settings, "max_threads");
	if (user_profile == "low_power" && threads > 2)
		threads = 2;
	obs_data_set_int(inf, "max_threads", threads < 1 ? 1 : threads);
	obs_data_set_double(inf, "cpu_budget_pct", (double)obs_data_get_int(settings, "cpu_budget_pct"));
	obs_data_set_obj(cfg, "inference", inf);
	obs_data_release(inf);

	obs_data_t *aud = obs_data_create();
	obs_data_set_int(aud, "sample_rate_hz", 16000);
	obs_data_set_int(aud, "frame_ms", 20);
	obs_data_set_bool(aud, "vad_enabled", true);
	obs_data_set_double(aud, "sensitivity", obs_data_get_double(settings, "audio_sensitivity"));
	obs_data_set_double(aud, "mouth_delay_ms", (double)obs_data_get_int(settings, "mouth_delay_ms"));
	obs_data_set_obj(cfg, "audio", aud);
	obs_data_release(aud);

	obs_data_t *priv = obs_data_create();
	obs_data_set_bool(priv, "local_only", true);
	obs_data_set_bool(priv, "store_audio", false);
	obs_data_set_bool(priv, "store_video", false);
	obs_data_set_bool(priv, "disclosure_overlay", true); // not user-configurable
	obs_data_set_string(priv, "disclosure_text", T_("Disclosure.Label"));
	obs_data_set_string(priv, "disclosure_position", obs_data_get_string(settings, "disclosure_position"));
	obs_data_set_double(priv, "disclosure_scale", obs_data_get_double(settings, "disclosure_scale"));
	obs_data_set_obj(cfg, "privacy", priv);
	obs_data_release(priv);

	obs_data_set_double(cfg, "stale_frame_ms", (double)obs_data_get_int(settings, "stale_frame_ms"));

	// provider_priority must be a plain JSON string array; obs_data arrays only
	// hold objects, so it is patched in via json_lite after serialization.
	json::Value v = obs_to_json(cfg);
	json::Value arr = json::Value::array();
	for (const char *p : order)
		arr.push_back(p);
	json::Value infv = v["inference"];
	infv.set("provider_priority", arr);
	v.set("inference", infv);
	obs_data_release(cfg);
	return v;
}

void register_avatar_source()
{
	struct obs_source_info info = {};
	info.id = kSourceId;
	info.type = OBS_SOURCE_TYPE_INPUT;
	info.output_flags = OBS_SOURCE_VIDEO | OBS_SOURCE_DO_NOT_DUPLICATE;
	info.icon_type = OBS_ICON_TYPE_CAMERA;
	info.get_name = avatar_get_name;
	info.create = avatar_create;
	info.destroy = avatar_destroy;
	info.update = avatar_update;
	info.get_defaults = avatar_get_defaults;
	info.get_properties = avatar_get_properties;
	info.get_width = avatar_get_width;
	info.get_height = avatar_get_height;
	info.video_tick = avatar_video_tick;
	info.video_render = avatar_video_render;
	obs_register_source(&info);
}

} // namespace avatar
