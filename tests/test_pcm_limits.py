from __future__ import annotations

import os
import struct
import threading
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.media import Media
from tests.fixtures import ROOT, TemporaryTest

UINT32_MAX = 2**32 - 1
SAFE_PCM_BYTES = UINT32_MAX - 4096
MAX_SAMPLES = SAFE_PCM_BYTES // 2


class PcmLimitTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.media = Media(ROOT, threading.Event())

    def wav(self, name="audio.wav", frames=32009):
        path = self.path / name
        with wave.open(str(path), "wb") as writer:
            writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            writer.writeframes(b"\x10\x01" * frames)
        return path

    def test_pcm_sample_capacity_boundary_uses_selected_track(self):
        for samples in [MAX_SAMPLES - 1, MAX_SAMPLES, MAX_SAMPLES + 1]:
            info = {"duration_ms": 200_000_000, "tracks": [
                {"index": 7, "duration_ts": samples, "time_base": "1/16000"}]}
            if samples <= MAX_SAMPLES:
                self.media.check_pcm_capacity(info, 7)
            else:
                with self.assertRaises(ToolError) as caught:
                    self.media.check_pcm_capacity(info, 7)
                self.assertEqual(caught.exception.code, "media_pcm_limit")

    def test_missing_track_range_uses_container_and_rejects_before_ffmpeg(self):
        info = {"duration_ms": 200_000_000, "tracks": [{"index": 7}]}
        with mock.patch.object(self.media, "probe", return_value=info), mock.patch("subtitle_tool.media.run_process") as process:
            with self.assertRaises(ToolError) as caught:
                self.media.extract(self.path / "input.mkv", self.path / "audio.wav", 7, "mix")
            self.assertEqual(caught.exception.code, "media_pcm_limit")
            process.assert_not_called()

    def test_actual_file_size_guard_runs_before_wave_reader(self):
        path = self.wav()
        with mock.patch.object(Path, "stat", return_value=SimpleNamespace(st_size=UINT32_MAX + 1)), \
                mock.patch("subtitle_tool.media.wave.open") as reader:
            with self.assertRaises(ToolError) as caught:
                self.media.split(path, self.path)
            self.assertEqual(caught.exception.code, "media_pcm_limit")
            reader.assert_not_called()

    def test_underestimated_extraction_cannot_replace_accepted_audio(self):
        target = self.wav()
        before = target.read_bytes()
        info = {"duration_ms": 1000, "tracks": [{"index": 7}]}
        original_stat = Path.stat
        def size(path, *args, **kwargs):
            if path.name.endswith(".partial.wav"):
                return SimpleNamespace(st_size=UINT32_MAX + 1)
            return original_stat(path, *args, **kwargs)
        def extract(args, *unused, **kwargs):
            Path(args[-1]).write_bytes(before)
        with mock.patch.object(self.media, "probe", return_value=info), \
                mock.patch("subtitle_tool.media.run_process", side_effect=extract), \
                mock.patch.object(Path, "stat", new=size):
            with self.assertRaises(ToolError) as caught:
                self.media.extract(self.path / "input.mkv", target, 7, "mix")
        self.assertEqual(caught.exception.code, "media_pcm_limit")
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(target.with_suffix(".partial.wav").exists())

    def test_wave_declarations_must_fit_actual_file_and_safe_data_capacity(self):
        original = self.wav().read_bytes()
        for kind in ["large_riff", "large_data", "truncated", "trailing", "rf64"]:
            path = self.path / (kind + ".wav")
            content = bytearray(original)
            if kind == "large_riff":
                struct.pack_into("<I", content, 4, UINT32_MAX)
            elif kind == "large_data":
                struct.pack_into("<I", content, 40, UINT32_MAX - 1)
            elif kind == "truncated":
                content = content[:-2]
            elif kind == "trailing":
                content.extend(b"unaccounted tail")
            else:
                content[:4] = b"RF64"
            path.write_bytes(content)
            with self.subTest(kind=kind), self.assertRaises(ToolError) as caught:
                self.media.split(path, self.path)
            self.assertEqual(caught.exception.code, "media_pcm_limit" if kind in {"large_riff", "large_data", "rf64"} else "media")

    def test_short_reads_never_create_a_claimed_complete_segment(self):
        path = self.wav(frames=16000 * 4 + 9)
        original_open = wave.open
        for boundary in ["window", "payload"]:
            (self.path / boundary).mkdir()
            def opened(file, mode):
                reader = original_open(file, mode)
                if mode == "rb":
                    read = reader.readframes
                    calls = []
                    def short(amount):
                        calls.append(amount)
                        data = read(amount)
                        return data[:-2] if boundary == "window" or len(calls) == 2 else data
                    reader.readframes = short
                return reader
            with self.subTest(boundary=boundary), mock.patch("subtitle_tool.media.wave.open", side_effect=opened):
                with self.assertRaises(ToolError) as caught:
                    self.media.split(path, self.path / boundary, limit_bytes=40000)
                self.assertEqual(caught.exception.code, "media")
            self.assertFalse(list((self.path / boundary).glob("audio-*.wav")))

    def test_sparse_oversized_riff_is_rejected_without_reading_payload(self):
        if os.name != "nt":
            return
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        path = self.path / "oversized.wav"
        with path.open("wb") as file:
            returned = wintypes.DWORD()
            self.assertTrue(kernel.DeviceIoControl(msvcrt.get_osfhandle(file.fileno()), 0x900C4,
                                                   None, 0, None, 0, ctypes.byref(returned), None))
            file.write(b"RIFF" + struct.pack("<I", UINT32_MAX) + b"WAVE")
            file.seek(16000 * 2 * 38 * 3600)
            file.write(b"\0")
        with self.assertRaises(ToolError) as caught:
            self.media.split(path, self.path)
        self.assertEqual(caught.exception.code, "media_pcm_limit")
