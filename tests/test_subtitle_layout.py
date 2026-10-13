from __future__ import annotations

import re
import copy
import json
import sqlite3
from contextlib import closing
import threading
import unittest
from unittest import mock

from subtitle_tool.common import MT_INPUT_BUDGET, ToolError
from subtitle_tool.config import Settings
from subtitle_tool.engine import Engine
from subtitle_tool.api import Bailian
from subtitle_tool.store import Store
from subtitle_tool.main import summary
from subtitle_tool.subtitles import layout_warnings, make_fragment, mt_cache_key, mt_input_size, normalize, plan_fragments, plan_reference_units, render_srt, translation_context, wrap
from tests.fixtures import FakeService, PipelineFixture, result, response, timed_raw
from tests import fixtures


def sources_of(raw):
    return normalize(raw, 0, "0000", preserve_sentences=True)[0]


class WrappingRegressionTests(unittest.TestCase):
    def assert_preserved(self, source, lines):
        self.assertEqual(re.sub(r"\s", "", "".join(lines)), re.sub(r"\s", "", source))

    def test_period_after_twenty_two_han_characters_stays_with_text(self):
        text = "字" * 22 + "。"
        lines = wrap(text, 44)
        self.assertEqual(lines, [text])
        cue = {"begin_ms": 0, "end_ms": 1000, "text": text, "language": "zh", "decision": "original"}
        self.assertIn((text + "\r\n").encode(), render_srt([cue], Settings()))
        self.assertIn(("字" * 22 + "\r\n。").encode(), render_srt([cue], Settings(), subtitle_version=0))

    def test_english_two_word_tail_is_balanced_without_breaking_words(self):
        text = "One two three four five six seven eight nine ten"
        lines = wrap(text, 42)
        self.assertEqual(len(lines), 2)
        self.assertGreater(len(lines[-1].split()), 2)
        self.assert_preserved(text, lines)

    def test_short_english_sentence_also_avoids_a_two_word_tail(self):
        text = "alpha bravo delta charlie delta"
        lines = wrap(text, 24)
        self.assertEqual(len(lines), 2)
        self.assertGreater(len(lines[-1].split()), 2)
        self.assert_preserved(text, lines)

    def test_mixed_text_uses_display_width(self):
        text = "字幕" * 8 + " Python API " + "字幕" * 8
        lines = wrap(text, 44)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(sum(2 if ord(c) > 127 else 1 for c in line) <= 44 for line in lines))
        self.assert_preserved(text, lines)

    def test_brackets_and_quotes_obey_break_rules(self):
        for text in ["字" * 21 + "（字幕）", "字" * 21 + "“字幕。”", 'A short sentence with (several quoted words).']:
            with self.subTest(text=text):
                lines = wrap(text, 22)
                self.assertTrue(all(line[0] not in "。，！？）”" and line[-1] not in "（“(" for line in lines))
                self.assert_preserved(text, lines)

    def test_long_word_is_preserved_as_an_indivisible_unit(self):
        text = "supercalifragilisticexpialidocious"
        self.assertEqual(wrap(text, 12), [text])

    def test_combining_marks_do_not_take_capacity_or_start_a_line(self):
        text = "e\u0301" * 12
        self.assertEqual(wrap(text, 12), [text])


class SegmentationTests(unittest.TestCase):
    def assert_ranges(self, source, cues):
        self.assertEqual(re.sub(r"\s", "", source["text"]), re.sub(r"\s", "", "".join(c["text"] for c in cues)))
        for cue in cues:
            self.assertGreaterEqual(cue["end_ms"] - cue["begin_ms"], 1000)
            self.assertEqual(cue["begin_ms"], source["words"][cue["word_start"]]["begin_ms"])
            self.assertEqual(cue["end_ms"], source["words"][cue["word_end"] - 1]["end_ms"])
        self.assertEqual(len({c["cue_id"] for c in cues}), len(cues))

    def test_text_volume_splits_a_sentence_shorter_than_seven_seconds(self):
        source = sources_of(timed_raw([f"word{i}" for i in range(24)]))[0]
        cues = plan_fragments(source, Settings())
        self.assertGreater(len(cues), 1)
        self.assert_ranges(source, cues)
        self.assertTrue(all(len(wrap(c["text"], 42)) <= 2 for c in cues))
        self.assertEqual(cues, plan_fragments(source, Settings()))

    def test_reference_expansion_triggers_splitting_without_slicing_translation(self):
        source = sources_of(timed_raw(["alpha", "bravo", "charlie", "delta"], word_ms=1000))[0]
        self.assertEqual(len(plan_fragments(source, Settings())), 1)
        cues = plan_fragments(source, Settings(), reference="中" * 70)
        self.assertEqual(len(cues), 2)
        self.assert_ranges(source, cues)
        self.assertTrue(all(c["translated"] == "" for c in cues))

    def test_sentence_punctuation_and_pause_precede_comma_and_normal_boundaries(self):
        for tokens, pauses, expected in [
            (["word"] * 4 + ["word."] + ["word"] * 7, {}, 5),
            (["word"] * 12, {5: 400}, 5),
            (["word"] * 4 + ["word,"] + ["word"] * 7, {}, 5),
        ]:
            source = sources_of(timed_raw(tokens, word_ms=250, pauses=pauses))[0]
            cues = plan_fragments(source, Settings(), force=True)
            self.assertEqual(cues[0]["word_end"], expected)
            self.assert_ranges(source, cues)

    def test_equal_priority_chooses_balanced_word_volume(self):
        source = sources_of(timed_raw(["word"] * 12, word_ms=250))[0]
        self.assertEqual(plan_fragments(source, Settings(), force=True)[0]["word_end"], 6)

    def test_missing_invalid_or_incomplete_words_retain_source_and_timing(self):
        raw = timed_raw(["word"] * 30, word_ms=150)
        for variant in ["missing", "overlap", "incomplete", "punctuation"]:
            damaged = copy.deepcopy(raw)
            sentence = damaged["transcripts"][0]["sentences"][0]
            if variant == "missing":
                sentence.pop("words")
            elif variant == "overlap":
                sentence["words"][1]["begin_time"] = 1
            elif variant == "incomplete":
                sentence["words"].pop()
            else:
                sentence["words"][0]["punctuation"] = None
            source = sources_of(damaged)[0]
            cue = plan_fragments(source, Settings())[0]
            self.assertEqual((cue["begin_ms"], cue["end_ms"], cue["text"]), (0, 4500, sentence["text"]))
            self.assertTrue(layout_warnings([cue], Settings()))
            self.assertEqual(source["words"], [])

    def test_repaired_point_and_original_subsecond_sentence_do_not_get_fake_word_times(self):
        raw = {"transcripts": [{"sentences": [fixtures.point_sentence(1000)]}]}
        saved = copy.deepcopy(raw)
        source = sources_of(raw)[0]
        self.assertEqual(raw, saved)
        self.assertEqual(source["words"], [])
        self.assertEqual(source["original_timings"][0]["end_ms"], 1000)
        self.assertEqual(source["end_ms"], 2000)
        source = sources_of(timed_raw(["word"] * 30, word_ms=20))[0]
        self.assertEqual(len(plan_fragments(source, Settings())), 1)
        self.assertEqual(source["end_ms"], 600)

    def test_indivisible_long_word_keeps_content_and_warns(self):
        source = sources_of(timed_raw(["x" * 100], word_ms=2000))[0]
        cues = plan_fragments(source, Settings())
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "x" * 100)
        self.assertTrue(layout_warnings(cues, Settings()))

    def test_asr_subtokens_do_not_split_inside_an_english_word(self):
        raw = timed_raw(["abcdefgh", "ijklmnop"], word_ms=1500)
        raw["transcripts"][0]["sentences"][0]["text"] = "abcdefghijklmnop"
        source = sources_of(raw)[0]
        self.assertEqual(len(plan_fragments(source, Settings(), force=True)), 1)


class ContextTests(unittest.TestCase):
    def sources(self):
        sources = sources_of(result(*[(f"Sentence number {i}.", "en") for i in range(7)]))
        return [{**s, "reference": f"参考{i}"} for i, s in enumerate(sources)]

    def test_two_neighbors_each_side_excluding_unsplit_self(self):
        sources = self.sources()
        cue = make_fragment(sources[3])
        context = translation_context(cue, sources)
        self.assertEqual({p["source"] for p in context}, {sources[i]["text"] for i in [1, 2, 4, 5]})
        self.assertNotIn(sources[3]["text"], [p["source"] for p in context])

    def test_parent_reference_is_first_for_a_split_fragment(self):
        source = sources_of(timed_raw(["word"] * 24))[0]
        source["reference"] = "整句参考"
        cue = plan_fragments(source, Settings())[0]
        self.assertEqual(translation_context(cue, [source]), [{"source": source["text"], "target": "整句参考"}])

    def test_gap_language_decision_and_manual_override_stop_expansion(self):
        for mode in ["gap", "language", "original", "manual", "source_lang", "unknown"]:
            sources = self.sources()
            if mode == "gap":
                for source in sources[3:]:
                    source["begin_ms"] += 4901
                    source["end_ms"] += 4901
            elif mode == "language":
                sources[2]["language"] = "ja"
            elif mode == "original":
                sources[2]["decision"] = "original"
            elif mode == "manual":
                sources[2]["reason"] = "用户在终端确认"
            elif mode == "source_lang":
                sources[2]["source_lang"] = "English"
            else:
                sources[2]["language"] = sources[3]["language"] = ""
            selected = [p["source"] for p in translation_context(make_fragment(sources[3]), sources)]
            self.assertNotIn(sources[2]["text"], selected)
            self.assertNotIn(sources[1]["text"], selected)

    def test_cache_keys_include_stage_strategy_and_actual_context(self):
        values = [mt_cache_key("Hello", strategy_version=1, stage="reference"),
                  mt_cache_key("Hello", strategy_version=1, stage="subtitle"),
                  mt_cache_key("Hello", strategy_version=2, stage="subtitle"),
                  mt_cache_key("Hello", strategy_version=1, stage="subtitle", tm_list=[{"source": "one", "target": "一"}]),
                  mt_cache_key("Hello", strategy_version=1, stage="subtitle", tm_list=[{"source": "two", "target": "二"}]),
                  mt_cache_key("Hello", strategy_version=1, stage="subtitle", asr_language="fr")]
        self.assertEqual(len(set(values)), len(values))

    def test_budget_drops_complete_entries_and_remains_below_official_limit(self):
        sources = self.sources()
        sources[2]["reference"] = "中" * 3000
        context = translation_context(make_fragment(sources[3]), sources)
        self.assertNotIn(sources[2]["text"], [p["source"] for p in context])
        self.assertEqual(next(p for p in context if p["source"] == sources[4]["text"])["target"], "参考4")
        self.assertLessEqual(mt_input_size(sources[3]["text"], tm_list=context), MT_INPUT_BUDGET)
        self.assertTrue(0 < MT_INPUT_BUDGET < 8192)

    def test_overlong_reference_uses_reliable_ranges_or_recoverable_error(self):
        source = sources_of(timed_raw(["x" * 1000] * 8, word_ms=500))[0]
        units = plan_reference_units([source])
        self.assertGreater(len(units), 1)
        self.assertEqual(re.sub(r"\s", "", source["text"]), "".join(re.sub(r"\s", "", c["text"]) for c in units))
        self.assertTrue(all(mt_input_size(c["text"]) <= MT_INPUT_BUDGET for c in units))
        source["words"] = []
        with self.assertRaises(ToolError) as caught:
            plan_reference_units([source])
        self.assertEqual(caught.exception.code, "mt_input_budget")


class PersistenceTests(fixtures.TemporaryTest):
    def test_existing_schema_and_successful_translation_remain_readable(self):
        path = self.path / "old.sqlite"
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript("""
                CREATE TABLE cues (job_id TEXT, cue_id TEXT, begin_ms INTEGER, end_ms INTEGER,
                    text TEXT, language TEXT, decision TEXT, reason TEXT, translated TEXT DEFAULT '',
                    source_lang TEXT DEFAULT 'auto', attempts INTEGER DEFAULT 0, error TEXT DEFAULT '',
                    PRIMARY KEY(job_id,cue_id));
                INSERT INTO cues VALUES('old','stable',10,1000,'Saved source','en','translate','ASR',
                                        '已完成译文','English',2,'');
            """)
        store = Store(path)
        try:
            cue = store.cues("old")[0]
            self.assertEqual((cue["cue_id"], cue["translated"], cue["attempts"]), ("stable", "已完成译文", 2))
            self.assertEqual(store.references("old"), [])
            self.assertEqual(store.sources("old"), [])
        finally:
            store.close()

    def test_layout_transaction_rolls_back_cues_and_manifest_together(self):
        source = self.path / "source.mkv"
        source.write_bytes(b"test")
        store = Store(self.path / "jobs.sqlite")
        try:
            job_id = store.enqueue(source, workspace_id="offline-space")
            manifest = store.job(job_id)["manifest"]
            sources = sources_of(timed_raw(["word"] * 12))
            cues = [make_fragment(s) for s in sources]
            store.initialize_subtitles(job_id, sources, cues, manifest)
            saved_cues = store.cues(job_id)
            broken = copy.deepcopy(cues)
            broken[0].pop("text")
            revised_manifest = {**manifest, "layout_round": 1}
            with self.assertRaises(sqlite3.IntegrityError):
                store.save_layout(job_id, broken, revised_manifest)
            self.assertEqual(store.cues(job_id), saved_cues)
            self.assertEqual(store.job(job_id)["manifest"], manifest)
        finally:
            store.close()

    def test_tm_list_uses_official_single_message_shape_and_budget_guard(self):
        cloud = Bailian("offline-key", "beijing", threading.Event(), workspace_id="offline-space", access_mode="workspace")
        memory = [{"source": "A previous sentence.", "target": "上一句。"}]
        with mock.patch.object(cloud.http, "json", return_value={"choices": [{"finish_reason": "stop", "message": {"content": "繁體中文"}}]}) as request:
            text, _ = cloud.translate("A fragment.", "English", tm_list=memory)
            self.assertEqual(text, "繁体中文")
            body = request.call_args.kwargs["json"]
            self.assertEqual(body["messages"], [{"role": "user", "content": "A fragment."}])
            self.assertEqual(body["translation_options"], {"source_lang": "English", "target_lang": "Chinese", "tm_list": memory})
            request.reset_mock()
            with self.assertRaises(ToolError):
                cloud.translate("x" * 6000, tm_list=[])
            request.assert_not_called()


class ContextPipelineTests(PipelineFixture):
    def intercept_mt(self, fake, handler):
        original = fake.request
        def request(method, url, **kwargs):
            if url.endswith("/chat/completions"):
                custom = handler(kwargs["json"])
                if custom is not None:
                    fake.calls.append((method, url, kwargs))
                    return custom
            return original(method, url, **kwargs)
        fake.request = request

    @staticmethod
    def mt_response(text):
        return response({"id": "offline-mt", "choices": [{"finish_reason": "stop", "message": {"content": text}}]})

    def test_two_stages_neighbors_concurrent_results_and_language_isolation(self):
        fake = FakeService(result(("First sentence.", "en"), ("Second sentence.", "en"), ("今日は晴れです。", "ja")))
        self.assertEqual(self.run_fake(fake), "completed")
        calls = [c[2]["json"] for c in fake.named("/chat/completions")]
        self.assertEqual(len(calls), 5)
        references = self.store.references(self.job_id)
        self.assertEqual(len(references), 3)
        self.assertTrue(all(r["translated"].startswith("繁体译文：") for r in references))
        for call in calls:
            self.assertEqual(len(call["messages"]), 1)
            context = call["translation_options"].get("tm_list", [])
            if context:
                text = call["messages"][0]["content"]
                expected = "Second sentence." if text.startswith("First") else "First sentence."
                self.assertEqual(context, [{"source": expected, "target": "繁体译文：" + expected}])
        cues = self.store.cues(self.job_id)
        self.assertEqual([c["text"] for c in cues], ["First sentence.", "Second sentence.", "今日は晴れです。"])
        self.assertTrue(all(c["translated"] == "繁体译文：" + c["text"] for c in cues))

    def test_split_cues_request_parent_reference_and_preserve_all_words(self):
        fake = FakeService(timed_raw([f"word{i}" for i in range(24)], word_ms=150))
        self.assertEqual(self.run_fake(fake), "completed")
        source = self.store.sources(self.job_id)[0]
        reference = self.store.references(self.job_id)[0]
        cues = self.store.cues(self.job_id)
        self.assertGreater(len(cues), 1)
        self.assertEqual(" ".join(c["text"] for c in cues), source["text"])
        calls = fake.named("/chat/completions")
        self.assertEqual(calls[0][2]["json"]["messages"][0]["content"], source["text"])
        for call in calls[1:]:
            self.assertEqual(call[2]["json"]["translation_options"]["tm_list"][0],
                             {"source": source["text"], "target": reference["translated"]})

    def test_reference_failure_saves_first_stage_and_resume_only_fills_missing(self):
        fake = FakeService(result(("First sentence.", "en"), ("Second sentence.", "en")))
        fake.mt_failure.add("Second sentence.")
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertTrue(self.store.references(self.job_id)[0]["translated"])
        self.assertFalse(any(c["translated"] for c in self.store.cues(self.job_id)))
        self.assertFalse(self.source.with_suffix(".srt").exists())
        fake.mt_failure.clear()
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(len(fake.named("/chat/completions")), 5)
        self.assertEqual(len(fake.named("/asr/transcription")), 1)

    def test_final_failure_reuses_fixed_references_and_successful_final_translation(self):
        fake = FakeService(result(("First sentence.", "en"), ("Second sentence.", "en")))
        failing = [True]
        def handler(body):
            if failing[0] and body["messages"][0]["content"].startswith("Second") and body["translation_options"].get("tm_list"):
                return response({"code": "Unauthorized"}, 403)
            return None
        self.intercept_mt(fake, handler)
        self.assertEqual(self.run_fake(fake), "failed")
        saved = self.store.cues(self.job_id)[0]["translated"]
        self.assertTrue(saved)
        self.assertTrue(all(r["translated"] for r in self.store.references(self.job_id)))
        failing[0] = False
        (self.config.data / "cache" / self.job_id / "raw-0000.json").unlink()
        fake.query_failure = True
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(self.store.cues(self.job_id)[0]["translated"], saved)
        calls = [c[2]["json"] for c in fake.named("/chat/completions")]
        retried = [c for c in calls if c["messages"][0]["content"].startswith("Second") and c["translation_options"].get("tm_list")]
        self.assertEqual(len(calls), 5)
        self.assertEqual(retried[0], retried[1])

    def test_cancellation_at_either_stage_resumes_without_repeating_successes(self):
        for stage in ["reference_translating", "subtitle_translating"]:
            with self.subTest(stage=stage):
                self.config.settings.mt_workers = 1
                fake = FakeService(result(("First sentence.", "en"), ("Second sentence.", "en")))
                cancel = threading.Event()
                current = [""]
                def progress(job_id, name, done=None, total=None):
                    current[0] = name
                def handler(body):
                    if current[0] == stage and body["messages"][0]["content"].startswith("First"):
                        cancel.set()
                    return None
                self.intercept_mt(fake, handler)
                with mock.patch("requests.request", side_effect=fake.request), mock.patch("requests.post", side_effect=fake.upload):
                    self.assertEqual(Engine(self.config, self.store, cancel, progress).run(self.job_id), "cancelled")
                saved = self.store.references(self.job_id) if stage == "reference_translating" else self.store.cues(self.job_id)
                self.assertTrue(saved[0]["translated"])
                self.store.update(self.job_id, "queued")
                self.assertEqual(self.run_fake(fake), "completed")
                self.assertEqual(len(fake.named("/chat/completions")), 4)
                if stage == "reference_translating":
                    self.source.with_suffix(".srt").unlink()
                    manifest = self.store.job(self.job_id)["manifest"]
                    for key in ["normalized", "references_planned", "layout_planned", "layout_complete", "expected_srt_sha256"]:
                        manifest.pop(key, None)
                    self.store.update(self.job_id, "queued", manifest)
                    # Use different inputs so the second scenario exercises interruption, not cache hits.
                    self.store.db.execute("DELETE FROM mt_cache")
                    self.store.db.commit()

    def test_two_extra_layout_rounds_then_warning_without_unbounded_requests(self):
        raw = timed_raw(["alpha", "bravo", "charlie", "delta"], word_ms=1200)
        raw["transcripts"][0]["sentences"].append({"begin_time": 4800, "end_time": 4900, "text": "Next sentence.", "language": "en"})
        fake = FakeService(raw)
        self.intercept_mt(fake, lambda body: self.mt_response("中" * (50 if body["translation_options"].get("tm_list") else 10)))
        self.assertEqual(self.run_fake(fake), "completed")
        manifest = self.store.job(self.job_id)["manifest"]
        self.assertEqual(manifest["layout_round"], 2)
        self.assertTrue(manifest["warnings"])
        self.assertEqual(len(fake.named("/chat/completions")), 10)
        cues = self.store.cues(self.job_id)
        self.assertEqual([c["text"] for c in cues[:4]], ["alpha", "bravo", "charlie", "delta"])
        self.assertTrue(all(c["translated"] == "中" * 50 for c in cues))

    def test_interrupt_after_layout_transaction_resumes_current_ranges_only(self):
        raw = timed_raw(["alpha", "bravo", "charlie", "delta"], word_ms=1200)
        raw["transcripts"][0]["sentences"].append({"begin_time": 4800, "end_time": 4900, "text": "Next sentence.", "language": "en"})
        fake = FakeService(raw)
        self.intercept_mt(fake, lambda body: self.mt_response("中" * (50 if body["translation_options"].get("tm_list") else 10)))
        save = self.store.save_layout
        def interrupt(job_id, cues, manifest, **kwargs):
            save(job_id, cues, manifest, **kwargs)
            if manifest.get("layout_round") == 1:
                raise KeyboardInterrupt()
        with mock.patch.object(self.store, "save_layout", side_effect=interrupt):
            self.assertEqual(self.run_fake(fake), "cancelled")
        saved = self.store.cues(self.job_id)
        self.assertEqual(len(saved), 3)
        self.assertTrue(saved[-1]["translated"])
        self.assertFalse(self.source.with_suffix(".srt").exists())
        self.store.update(self.job_id, "queued")
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(len(fake.named("/chat/completions")), 10)
        self.assertEqual(self.store.job(self.job_id)["manifest"]["layout_round"], 2)

    def test_failure_in_last_adjustment_round_resumes_only_missing_translation(self):
        raw = timed_raw(["alpha", "bravo", "charlie", "delta"], word_ms=1200)
        raw["transcripts"][0]["sentences"].append({"begin_time": 4800, "end_time": 4900, "text": "Next sentence.", "language": "en"})
        fake = FakeService(raw)
        failing = [True]
        def handler(body):
            if failing[0] and body["messages"][0]["content"] == "charlie":
                return response({"code": "Unauthorized"}, 403)
            return self.mt_response("中" * (50 if body["translation_options"].get("tm_list") else 10))
        self.intercept_mt(fake, handler)
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["manifest"]["layout_round"], 2)
        self.assertFalse(self.source.with_suffix(".srt").exists())
        successful = {c["cue_id"]: c["translated"] for c in self.store.cues(self.job_id) if c["translated"]}
        failing[0] = False
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertTrue(all(c["translated"] == successful[c["cue_id"]] for c in self.store.cues(self.job_id) if c["cue_id"] in successful))
        self.assertEqual(len(fake.named("/chat/completions")), 11)

    def test_review_updates_parent_decision_and_mt_source_language(self):
        fake = FakeService(result(("OK", "")))
        self.assertEqual(self.run_fake(fake), "needs_review")
        cue = self.store.cues(self.job_id)[0]
        self.store.cue_update(self.job_id, cue["cue_id"], decision="translate", source_lang="English", reason="用户在终端确认")
        self.store.update(self.job_id, "queued")
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(self.store.sources(self.job_id)[0]["source_lang"], "English")
        self.assertEqual(len(fake.named("/chat/completions")), 1)
        self.assertEqual(fake.named("/chat/completions")[0][2]["json"]["translation_options"]["source_lang"], "English")

    def test_missing_reference_records_cannot_skip_necessary_stage(self):
        fake = FakeService(result(("First sentence.", "en"), ("Second sentence.", "en")))
        fake.mt_failure.add("Second sentence.")
        self.assertEqual(self.run_fake(fake), "failed")
        with self.store.db:
            self.store.db.execute("DELETE FROM mt_references WHERE job_id=?", (self.job_id,))
        before = len(fake.named("/chat/completions"))
        fake.mt_failure.clear()
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "cache_mismatch")
        self.assertEqual(len(fake.named("/chat/completions")), before)

    def test_chinese_zero_mt_settings_snapshot_and_srt_commit(self):
        text = "字" * 22 + "。"
        fake = FakeService(result((text, "zh")))
        self.config.settings.zh_line_width = 12
        self.config.settings.chinese_first = True
        self.assertEqual(self.run_fake(fake), "completed")
        self.assertEqual(fake.named("/chat/completions"), [])
        content = self.source.with_suffix(".srt").read_bytes()
        self.assertFalse(content.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", content.replace(b"\r\n", b""))
        self.assertIn((text + "\r\n").encode(), content)
        report = summary(self.store, self.job_id)
        self.assertNotIn(text, json.dumps(report, ensure_ascii=False))
        self.assertEqual(report["subtitle_version"], 2)

    def test_new_flow_overwrite_backs_up_existing_srt(self):
        output = self.source.with_suffix(".srt")
        output.write_bytes(b"previous subtitle")
        manifest = self.store.job(self.job_id)["manifest"]
        manifest["overwrite"] = True
        self.store.update(self.job_id, manifest=manifest)
        self.assertEqual(self.run_fake(FakeService(result(("欢迎使用。", "zh")))), "completed")
        backups = list(output.parent.glob(output.name + ".bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"previous subtitle")
        self.assertIn("欢迎使用。".encode(), output.read_bytes())

    def test_over_budget_without_safe_words_fails_before_mt_and_keeps_resume_data(self):
        fake = FakeService(result(("x" * 6000, "en")))
        self.assertEqual(self.run_fake(fake), "failed")
        self.assertEqual(self.store.job(self.job_id)["error_code"], "mt_input_budget")
        self.assertEqual(fake.named("/chat/completions"), [])
        self.assertTrue(self.store.sources(self.job_id))
        self.assertFalse(self.source.with_suffix(".srt").exists())


if __name__ == "__main__":
    unittest.main()
