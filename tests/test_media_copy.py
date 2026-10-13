from __future__ import annotations

import array
import json
import os
import threading
import unittest
import wave
from unittest import mock

from subtitle_tool.media import Media, run_process
from subtitle_tool.common import NeedsReview
from tests.test_core import ROOT, TemporaryTest


@unittest.skipUnless(os.name == "nt" and (ROOT / "tools/ffmpeg.exe").exists(), "Pinned FFmpeg required")
class CompressedMediaTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.media = Media(ROOT, threading.Event())

    def generate(self, codec, suffix, *, stereo=False):
        path = self.path / ("中文 多点 & (1)." + codec + suffix)
        args = [self.media.executable("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                ("sine=frequency=440:duration=3:sample_rate=48000" if codec == "libvorbis" else
                 r"aevalsrc=if(lt(t\,0.75)\,0\,sin(2*PI*440*t)):s=48000:d=3")]
        if stereo:
            args += ["-ac", "2"]
        run_process(args + ["-c:a", codec, path], self.media.cancel)
        return path

    def decoded(self, path, track=0):
        output, _ = run_process([self.media.executable("ffmpeg"), "-v", "error", "-i", path,
                                 "-map", f"0:{track}", "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"], self.media.cancel)
        return output

    def packet_hashes(self, path):
        out, _ = run_process([self.media.executable("ffprobe"), "-v", "error", "-select_streams", "a:0",
                             "-show_packets", "-show_data_hash", "sha256", "-show_entries", "packet=data_hash",
                             "-of", "json", path], self.media.cancel)
        return [p["data_hash"] for p in json.loads(out)["packets"]]

    def test_copy_all_supported_codecs_without_reencoding_or_sample_changes(self):
        for encoder, suffix in [("aac", ".m4a"), ("libmp3lame", ".mp3"), ("flac", ".flac"),
                                ("libopus", ".ogg"), ("libvorbis", ".ogg")]:
            with self.subTest(encoder=encoder):
                source = self.generate(encoder, suffix)
                info = self.media.probe(source)
                track, _ = self.media.select(info)
                directory = self.path / ("cache-" + encoder)
                with mock.patch.object(self.media, "extract", side_effect=AssertionError("must copy")):
                    plan, segments = self.media.prepare(source, directory, info, track, "ask")
                copied = directory / segments[0]["audio"]
                self.assertEqual(plan["method"], "copy")
                self.assertEqual(self.packet_hashes(source), self.packet_hashes(copied))
                self.assertEqual(self.decoded(source), self.decoded(copied))
                self.assertEqual(plan["source_timeline"]["skip_samples"], plan["extracted_timeline"]["skip_samples"])

    def test_stereo_requires_choice_first_channel_copies_other_choices_convert(self):
        source = self.generate("aac", ".m4a", stereo=True)
        info = self.media.probe(source)
        with self.assertRaises(NeedsReview):
            self.media.select(info)
        for channel, method in [("left", "copy"), ("right", "flac"), ("mix", "flac")]:
            track, _ = self.media.select(info, channel=channel)
            plan, segments = self.media.prepare(source, self.path / channel, info, track, channel)
            self.assertEqual(plan["method"], method)
            audio = self.media.probe(self.path / channel / segments[0]["audio"])["tracks"][0]
            self.assertEqual(audio["channels"], 2 if channel == "left" else 1)
            if method == "flac":
                self.assertEqual(audio["sample_rate"], "16000")

    def test_aac_offset_and_priming_match_decoded_source(self):
        source = self.path / "delayed.mp4"
        run_process([self.media.executable("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                     "color=c=black:s=160x90:r=10:d=5", "-itsoffset", "1.25", "-f", "lavfi", "-i",
                     "sine=frequency=440:duration=3:sample_rate=48000", "-c:v", "mpeg4", "-c:a", "aac", source], self.media.cancel)
        info = self.media.probe(source)
        track, _ = self.media.select(info)
        directory = self.path / "cache"
        plan, segments = self.media.prepare(source, directory, info, track, "ask")
        out, _ = run_process([self.media.executable("ffprobe"), "-v", "error", "-select_streams", "a:0",
                             "-show_frames", "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", source], self.media.cancel)
        decoded_start = float(json.loads(out)["frames"][0]["best_effort_timestamp_time"])
        self.assertEqual(plan["audio_offset_ms"], round(decoded_start * 1000) - info["origin_ms"])
        self.assertEqual(plan["method"], "copy")
        self.assertEqual(self.decoded(source, track), self.decoded(directory / segments[0]["audio"]))

    def test_precise_flac_segments_retain_every_pcm_sample(self):
        source = self.path / "pcm.wav"
        payload = array.array("h", ((i * 197) % 32000 - 16000 for i in range(16000 * 4 + 9))).tobytes()
        with wave.open(str(source), "wb") as writer:
            writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            writer.writeframes(payload)
        info = self.media.probe(source)
        track, _ = self.media.select(info)
        directory = self.path / "cache"
        with mock.patch("subtitle_tool.media.MAX_ASR_SECONDS", 1.5):
            plan, segments = self.media.prepare(source, directory, info, track, "ask", 100000)
        self.assertEqual(plan["method"], "flac")
        self.assertGreater(len(segments), 1)
        joined, start = b"", 0
        for segment in segments:
            self.assertEqual(segment["start_frame"], start)
            self.assertEqual(segment["offset_ms"], start * 1000 // 16000)
            self.assertLessEqual(segment["bytes"], 100000)
            audio = self.decoded(directory / segment["audio"])
            self.assertEqual(len(audio), segment["sample_count"] * 2)
            joined += audio
            start += segment["sample_count"]
        self.assertEqual(joined, payload)
        self.assertFalse(list(directory.glob("*.wav")))

    def test_discontinuous_timestamps_fall_back_and_preserve_gap(self):
        source = self.path / "gap.mkv"
        run_process([self.media.executable("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                     "sine=frequency=440:duration=3:sample_rate=16000", "-af",
                     r"asetpts=PTS+gte(T\,1)*0.5/TB", "-c:a", "flac", source], self.media.cancel)
        info = self.media.probe(source)
        track, _ = self.media.select(info)
        directory = self.path / "cache"
        plan, segments = self.media.prepare(source, directory, info, track, "ask")
        self.assertEqual(plan["method"], "flac")
        self.assertFalse(plan["source_timeline"]["continuous"])
        self.assertAlmostEqual(segments[0]["sample_count"] / 16000, 3.5, delta=0.03)
