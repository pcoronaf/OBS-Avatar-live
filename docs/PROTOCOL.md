# Avatar Live — IPC contract (protocol v1)

This document is the single source of truth shared by the OBS plugin
(`plugin/`, C++) and the engine (`engine/`, Python). Any change here must bump
`protocol_version` / ring `version` and be applied to both sides.

All integers are little-endian. Target: Windows x64 (x86-64 TSO memory model).

## 1. Naming

The plugin creates a random 128-bit hex `session` token (32 chars) and an
independent random `auth_token` (32 hex chars), then launches:

```
avatar-engine.exe serve --session <session> --parent-pid <obs_pid>
```

with the environment variable `AVATAR_LIVE_AUTH_TOKEN=<auth_token>` (never on the
command line, so it is not visible in process listings).

| Object | Windows name | POSIX (dev/test only) |
|---|---|---|
| Control pipe | `\\.\pipe\AvatarLive_<session>_ctl` | Unix socket `$TMPDIR/avatarlive_<session>_ctl.sock` (mode 0600) |
| Frame ring (shared memory) | `Local\AvatarLive_<session>_frames` | `/avatarlive_<session>_frames` (`shm_open`, mode 0600) |
| Frame-ready event | `Local\AvatarLive_<session>_ready` (auto-reset) | none — reader polls `latest_sequence` from `video_tick` |

The engine is the **server/writer** (creates pipe, mapping and event). The
plugin is the **client/reader** (opens them). The pipe is created with
`PIPE_REJECT_REMOTE_CLIENTS` and a DACL granting access only to the current
user SID and SYSTEM. The engine exits when `--parent-pid` dies.

## 2. Frame ring (shared memory)

Total size = `header_size + slot_count * (slot_header_size + slot_data_capacity)`.

### Ring header (256 bytes, offset 0)

| Off | Type | Field | Notes |
|---|---|---|---|
| 0 | u32 | magic | `0x4C415641` (`"AVAL"` as LE bytes `41 56 41 4C`) |
| 4 | u32 | version | `1` |
| 8 | u32 | header_size | `256` |
| 12 | u32 | slot_count | `4` (3–8 allowed) |
| 16 | u32 | slot_header_size | `128` |
| 20 | u32 | reserved0 | 0 |
| 24 | u64 | slot_data_capacity | bytes per slot, multiple of 64 |
| 32 | u32 | max_width | |
| 36 | u32 | max_height | |
| 40 | u32 | latest_slot | index of newest complete slot; `0xFFFFFFFF` = none |
| 44 | u32 | writer_pid | |
| 48 | u64 | latest_sequence | sequence stored in `latest_slot`; 0 = none |
| 56 | u64 | generation_id | current generation; frames with another id are stale |
| 64 | u64 | heartbeat_ns | writer monotonic clock, updated ≥ every 100 ms |
| 72 | u32 | ring_flags | bit0 engine_running, bit1 paused, bit2 degraded |
| 76..255 | — | reserved | zero |

### Slot `i` at offset `header_size + i * (slot_header_size + slot_data_capacity)`

Slot header (128 bytes) followed by pixel data (`data_size` bytes, rows of `stride` bytes).

| Off | Type | Field | Notes |
|---|---|---|---|
| 0 | u64 | seqlock | odd = being written, even = stable |
| 8 | u64 | sequence | monotonically increasing frame counter, starts at 1 |
| 16 | u64 | timestamp_ns | monotonic ns (`QueryPerformanceCounter` based; Python `time.perf_counter_ns()`) when the frame was composed |
| 24 | u64 | generation_id | session generation (see `FrameDescriptor.generation_id`) |
| 32 | u32 | width | |
| 36 | u32 | height | |
| 40 | u32 | stride | bytes per row, ≥ width*4 |
| 44 | u32 | pixel_format | `1` = BGRA8 (only value in v1) |
| 48 | u32 | frame_flags | bit0 synthetic_label (MUST be 1), bit1 repeated (no new inference result), bit2 degraded/stale, bit3 paused/neutral |
| 52 | u32 | processing_latency_us | capture→compose latency (0 if not applicable) |
| 56 | u32 | data_size | `stride * height` |
| 60..127 | — | reserved | zero |

### Writer algorithm

1. `slot = (latest_slot + 1) % slot_count` (or 0 if none). Never write `latest_slot`.
2. `seqlock += 1` (odd).
3. Write pixel data, then slot header fields.
4. `seqlock += 1` (even).
5. Store `latest_sequence`, then `latest_slot`. Update `heartbeat_ns`.
6. `SetEvent(ready)` (Windows).

### Reader algorithm (never blocks the OBS render thread)

1. Read `latest_slot`; if `0xFFFFFFFF` or `latest_sequence` == last consumed → no new frame.
2. `s1 = seqlock`; if odd → skip this tick.
3. Copy header + data to a private buffer.
4. `s2 = seqlock`; if `s1 != s2` → discard (torn) and retry next tick.
5. Reject if `pixel_format != 1`, `generation_id != ring.generation_id`,
   `synthetic_label` bit is 0, or dimensions exceed `max_width/max_height`.
6. If `now - timestamp_ns > stale_ms` (default 500) or `now - heartbeat_ns > 2000 ms`,
   the plugin shows its own visible "Avatar Live: degraded / engine not responding" state
   instead of the last face.

## 3. Control channel

Framing: `u32 length` (LE, bytes of payload, max 1 MiB) + UTF-8 JSON object.

Envelope:

```json
{"v": 1, "id": 7, "type": "start_session", "payload": {}}
```

* Requests from plugin carry a positive `id`.
* Replies: `{"v":1,"id":7,"type":"reply","ok":true,"payload":{...}}` or
  `{"v":1,"id":7,"type":"reply","ok":false,"error":{"code":"...","message":"..."}}`.
* Unsolicited engine events: `{"v":1,"id":0,"type":"event","event":"<name>","payload":{...}}`.

The first request MUST be `hello`; anything else, or a wrong token, closes the
connection with error `unauthorized`.

### Requests

| type | payload | reply payload |
|---|---|---|
| `hello` | `{auth_token, client:"obs-avatar-live", client_version, protocol_version:1}` | `{engine_version, protocol_version, engines:[...], frame_ring:{name, event_name, slot_count, max_width, max_height}}` |
| `ping` | `{}` | `{now_ns}` |
| `list_devices` | `{}` | `{video:[{id,name}], audio:[{id,name}]}` |
| `list_profiles` | `{}` | `{profiles:[{profile_id, display_name, created_at}]}` |
| `create_profile` | `{photo_path, display_name, consent:{rights_confirmed:true, subject_is_self:bool, third_party_consent_reference?:str}}` | `{profile_id, warnings:[...]}` |
| `delete_profile` | `{profile_id}` | `{}` |
| `start_session` | full config object (`contracts/config.schema.json`) | `{session_id, generation_id, engine, provider}` |
| `stop_session` | `{}` | `{}` |
| `pause` / `resume` | `{}` | `{}` |
| `freeze_neutral` | `{enabled:bool}` | `{}` |
| `set_expression` | `{expression_id, intensity, duration_ms}` | `{}` |
| `set_quality` | `{profile?, cpu_threads?, detection_interval?, enhancement?}` | `{}` |
| `get_metrics` | `{}` | Metrics object (§4) |
| `shutdown` | `{}` | `{}` then engine exits |

### Events

| event | payload |
|---|---|
| `state` | `{state: "idle"/"starting"/"running"/"paused"/"degraded"/"error"/"stopped", detail}` |
| `metrics` | Metrics object, ~1 Hz while running |
| `fallback` | `{from, to, reason}` |
| `error` | `{code, message, recoverable}` |

## 4. Metrics object

```json
{
  "engine": "lite_adaptive",
  "state": "running",
  "execution_provider_selected": "CPUExecutionProvider",
  "provider_device": "CPU",
  "face_swap_unique_fps": 7.8,
  "inference_fps": 7.8,
  "output_fps": 30.0,
  "repeated_frame_pct": 74.0,
  "dropped_frames": 3,
  "capture_to_output_p50_ms": 120.0,
  "capture_to_output_p95_ms": 190.0,
  "stage_p95_ms": {"detect": 9.0, "swap": 95.0, "compose": 4.0},
  "cpu_utilization_pct": 41.0,
  "ram_mb": 610.0,
  "fallback_reason": null,
  "benchmark_timestamp": "2026-11-07T10:00:00Z",
  "quality_level": 0,
  "synthetic_label": true
}
```

Fields that do not apply to the active engine are `null`.
