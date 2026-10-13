from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import unittest

from subtitle_tool.config import Blob, Config
from subtitle_tool.instance import ProcessLock
from subtitle_tool.media import Media, run_process
from tests.test_core import ROOT, TemporaryTest


class InstanceTests(TemporaryTest):
    def test_exclusive_lock_and_release(self):
        first, second = ProcessLock(self.path), ProcessLock(self.path)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.close()
        self.assertTrue(second.acquire())
        second.close()


@unittest.skipUnless(os.name == "nt", "Windows integration required")
class WindowsTests(TemporaryTest):
    def test_real_dpapi_legacy_key_migration(self):
        config = Config(self.path)
        legacy = config.data / "key.dpapi"
        # Produce old-format ciphertext only in this migration test.
        data = b"offline-legacy-key"
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        source, target = Blob(len(data), buffer), Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt.CryptProtectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
        crypt.CryptProtectData.restype = ctypes.c_int
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        self.assertTrue(crypt.CryptProtectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)))
        try:
            legacy.write_bytes(ctypes.string_at(target.pbData, target.cbData))
        finally:
            kernel.LocalFree(target.pbData)
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.get_key(), "offline-legacy-key")
        self.assertFalse(legacy.exists())
        self.assertEqual((config.data / "key.txt").read_bytes(), b"offline-legacy-key")

    def test_exclusive_lock_in_another_process(self):
        first = ProcessLock(self.path)
        self.assertTrue(first.acquire())
        script = "import sys; from pathlib import Path; from subtitle_tool.instance import ProcessLock; lock=ProcessLock(Path(sys.argv[1])); ok=lock.acquire(); lock.close(); sys.exit(0 if ok else 2)"
        try:
            process = subprocess.run([sys.executable, "-c", script, str(self.path)], cwd=ROOT, capture_output=True, timeout=15, shell=False)
            self.assertEqual(process.returncode, 2)
        finally:
            first.close()
        process = subprocess.run([sys.executable, "-c", script, str(self.path)], cwd=ROOT, capture_output=True, timeout=15, shell=False)
        self.assertEqual(process.returncode, 0)

    @unittest.skipUnless((ROOT / "tools/ffmpeg.exe").exists(), "FFmpeg fixture required")
    def test_real_media_positive_start_offset_is_retained(self):
        sample = self.path / "delayed.mkv"
        cancel = threading.Event()
        media = Media(ROOT, cancel)
        run_process([media.executable("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                     "color=c=black:s=160x90:r=10:d=5", "-itsoffset", "1.25", "-f", "lavfi", "-i",
                     "sine=frequency=440:duration=3:sample_rate=16000", "-c:v", "ffv1", "-c:a", "pcm_s16le", sample], cancel)
        info = media.probe(sample)
        track, offset = media.select(info)
        self.assertEqual(offset, 1250)
        audio = self.path / "audio.wav"
        media.extract(sample, audio, track, "mix")
        import wave
        with wave.open(str(audio), "rb") as reader:
            self.assertAlmostEqual(reader.getnframes() / reader.getframerate(), 3, delta=0.02)


if __name__ == "__main__":
    unittest.main()
