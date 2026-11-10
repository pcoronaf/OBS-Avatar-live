"""Microphone selection on Windows, where every device appears once per host API."""

import numpy as np
import pytest

from avatar_engine.audio import capture as cap

HOSTAPIS = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}, {"name": "Windows WDM-KS"}]
NAME = "Microphone Array (Realtek(R) Audio)"


def dev(name, hostapi, ch=2, rate=48000.0):
    return {"name": name, "hostapi": hostapi, "max_input_channels": ch, "default_samplerate": rate}


class FakeSD:
    def __init__(self):
        self.devices = [
            dev("Microsoft Sound Mapper - Input", 0),
            dev(NAME[:31], 0, rate=44100.0),  # MME truncates to 31 chars
            dev("Primary Sound Capture Driver", 1),
            dev(NAME, 1),
            dev(NAME, 2),  # index 4: WASAPI
            dev("Headset Microphone (USB Audio)", 2, ch=1, rate=16000.0),
            dev("Speakers (Realtek(R) Audio)", 2, ch=0),
            dev(NAME, 3),
        ]
        self.opened = []
        self.fail_hostapis = set()

    def query_hostapis(self):
        return HOSTAPIS

    def query_devices(self, kind=None):
        if kind == "input":
            return self.devices[4]
        return self.devices

    def check_input_settings(self, device=None, samplerate=None, channels=None, dtype=None):
        if self.devices[device]["hostapi"] == 2:
            raise ValueError("WASAPI shared mode: invalid sample rate")

    def InputStream(self, samplerate, channels, dtype, blocksize, device, callback, latency):
        if self.devices[device]["hostapi"] in self.fail_hostapis:
            raise RuntimeError("device busy")
        self.opened.append((device, samplerate, channels))
        return object()


def test_each_microphone_listed_once():
    names = [d["name"] for d in cap.list_input_devices(FakeSD())]
    assert names == [NAME, "Headset Microphone (USB Audio)"]  # aliases, duplicates and outputs dropped


def test_name_prefers_wasapi_at_native_rate():
    sd = FakeSD()
    m = cap.MicrophoneCapture(lambda *a: None, NAME, sd=sd)
    assert sd.opened == [(4, 48000.0, 2)] and m.device_info["hostapi"] == "Windows WASAPI"


def test_falls_back_to_other_host_api():
    sd = FakeSD()
    sd.fail_hostapis = {2}
    cap.MicrophoneCapture(lambda *a: None, NAME, sd=sd)
    assert sd.opened == [(1, 16000.0, 1)]  # MME (truncated name) accepts 16 kHz mono directly


def test_unknown_microphone_is_a_clear_error():
    with pytest.raises(cap.AudioUnavailable, match="not found"):
        cap.MicrophoneCapture(lambda *a: None, "Nope", sd=FakeSD())


def test_resampler_48k_to_16k_streaming():
    r = cap._Resampler(48000, 16000)
    t = np.arange(48000) / 48000
    x = (np.sin(2 * np.pi * 300 * t) * 10000).astype(np.int16)
    out = np.concatenate([r(x[i:i + 960]) for i in range(0, len(x), 960)])
    assert abs(len(out) - 16000) <= 2
    ref = np.sin(2 * np.pi * 300 * np.arange(len(out)) / 16000) * 10000
    assert np.corrcoef(out[100:-100], ref[100:-100])[0, 1] > 0.99
