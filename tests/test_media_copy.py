from __future__ import annotations

import array
import json
import os
import threading

import unittest
import wave
from fractions import Fraction
from unittest import mock

from subtitle_tool.media import Media, run_process
from subtitle_tool.common import NeedsReview, ToolError, fingerprint
from tests.fixtures import enqueue_current, ROOT, FakeService, PipelineFixture, TemporaryTest, result


class PacketTimelineTests(TemporaryTest):
    def scan(self, packets, *, rate=48000, time_base='1/48000', codec='aac', container='mov'):
        media = Media(ROOT, threading.Event())
        track = dict(index=0, sample_rate=str(rate), time_base=time_base,
                     codec_name=codec, container_format=container)
        def probe(*args, stdout, **kwargs):
            stdout.write(packets.encode('ascii'))
        with mock.patch('subtitle_tool.media.run_process', side_effect=probe):
            return media.packet_timeline(self.path / 'synthetic', track, self.path)

    def test_exact_tolerance_at_large_pts_with_distinct_sample_and_time_bases(self):
        # At this PTS, floating point subtraction would lose the one-tick distinction.
        begin = 2**60
        for gap, continuous in [(2, True), (3, False), (-2, True), (-3, False)]:
            with self.subTest(gap=gap):
                end = begin + 40 + gap
                timeline = self.scan(f'pts={begin}|duration=20\npts={begin + 20 + gap}|duration=20\n',
                                     rate=44100, time_base='1/1000')
                self.assertEqual(timeline['continuous'], continuous)
                self.assertEqual(timeline['begin_seconds'], float(Fraction(begin, 1000)))
                self.assertEqual(timeline['end_seconds'], float(Fraction(end, 1000)))
                self.assertEqual(timeline['duration_seconds'], float(Fraction(40 + gap, 1000)))

    def test_priming_padding_and_ogg_granule_rules(self):
        packets = 'pts=-1024|duration=1024|skip_samples=1024\npts=0|duration=1024|discard_padding=24\n'
        timeline = self.scan(packets)
        self.assertEqual(timeline['begin_seconds'], 0)
        self.assertEqual(timeline['duration_seconds'], float(Fraction(1000, 48000)))
        self.assertEqual((timeline['skip_samples'], timeline['discard_padding']), (1024, 24))
        ogg = self.scan(packets, codec='opus', container='ogg')
        self.assertEqual(ogg['duration_seconds'], float(Fraction(1024, 48000)))
        vorbis = self.scan('pts=0|duration=128\npts=128|duration=1024\n', codec='vorbis', container='ogg')
        self.assertEqual(vorbis['begin_seconds'], float(Fraction(128, 48000)))
        self.assertEqual(vorbis['duration_seconds'], float(Fraction(1024, 48000)))

    def test_sample_tolerance_and_nonunit_time_base_numerator(self):
        for gap, continuous in [(3, True), (4, False)]:
            timeline = self.scan(f'pts=0|duration=6\npts={6 + gap}|duration=6\n',
                                 rate=2, time_base='3/10')
            self.assertEqual(timeline['continuous'], continuous)
            self.assertEqual(timeline['duration_seconds'], float(Fraction((12 + gap) * 3, 10)))

    def test_midstream_trimming_and_invalid_packets_are_not_accepted_for_copy(self):
        for packets in ['pts=0|duration=1024\npts=1024|duration=1024|skip_samples=2\n',
                        'pts=0|duration=1024|discard_padding=2\npts=1024|duration=1024\n']:
            self.assertFalse(self.scan(packets)['continuous'])
        for packets in ['', 'pts=0|duration=-1\n', 'pts=0|duration=N/A\n', 'pts=0\n']:
            with self.subTest(packets=packets), self.assertRaises(ToolError) as error:
                self.scan(packets)
            self.assertEqual(error.exception.code, 'media_timeline')


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
                if suffix == ".ogg":
                    repeated = self.media.prepare(source, self.path / ("repeat-" + encoder), info, track, "ask")
                    self.assertEqual((plan, segments), repeated)


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


class OggRecoveryTests(PipelineFixture):
    def test_missing_ogg_cache_and_expired_references_recover_without_stale_segments(self):
        media = Media(ROOT, threading.Event())
        source = self.path / "source.ogg"
        run_process([media.executable("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                     "sine=frequency=440:duration=3:sample_rate=48000", "-c:a", "libopus", source], media.cancel)
        info = media.probe(source)
        track, _ = media.select(info)
        for case in ["new_missing", "known_task", "unknown_version"]:
            with self.subTest(case=case):
                self.job_id = enqueue_current(self.store, self.config, source, output_dir=self.path / case)
                cache = self.config.data / "cache" / self.job_id
                plan, segments = media.prepare(source, cache, info, track, "ask")
                if case == "unknown_version":
                    plan["extract_recipe_version"] = 99
                manifest = self.store.job(self.job_id)["manifest"]
                manifest.update(media=info, track=track, channel="ask", segments=segments, media_plan=plan,
                                fingerprint=fingerprint(source, media.cancel), upload_limit_bytes=plan["limit_bytes"])
                if case == "known_task":
                    segments[0]["task_id"] = "offline-known"
                (cache / segments[0]["audio"]).unlink()
                self.store.update(self.job_id, manifest=manifest)
                fake = FakeService(result(("中文字幕。", "zh")))
                self.assertEqual(self.run_fake(fake), "failed" if case == "unknown_version" else "completed")
                saved = self.store.job(self.job_id)
                if case == "unknown_version":
                    self.assertEqual(saved["error_code"], "cache_version")
                    self.assertEqual(fake.calls, [])
                    continue
                self.assertEqual(len(fake.named("/asr/transcription")), 0 if case == "known_task" else 1)
                self.assertEqual(len(fake.uploads), 0 if case == "known_task" else 1)
                self.assertEqual(saved["manifest"]["media_plan"].get("extract_recipe_version"), 2)
                self.assertTrue(saved["manifest"]["segments"][0].get("task_id"))
                self.assertFalse(saved["manifest"]["segments"][0].get("submitting"))
