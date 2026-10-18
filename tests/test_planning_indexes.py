from __future__ import annotations

import copy
import random
import re
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from subtitle_tool import subtitles
from subtitle_tool.config import Config, Settings
from subtitle_tool.engine import Engine
from subtitle_tool.store import Store
from tests.fixtures import FakeService, PipelineFixture, response, timed_raw


def legacy_word_break(source, lo, hi):
    """Keep the original slicing/scanning algorithm as an equivalence oracle."""
    words, text = source.get("words", []), source["text"]
    choices = []
    for at in range(lo + 1, hi):
        if (words[at - 1]["end_ms"] - words[lo]["begin_ms"] < 1000 or
                words[hi - 1]["end_ms"] - words[at]["begin_ms"] < 1000):
            continue
        char = words[at - 1]["char_end"]
        if not subtitles.legal_break(text, char) or not subtitles.word_boundary(text, char):
            continue
        before = text[words[lo]["char_start"]:char].rstrip()
        stripped = before.rstrip("\"'”’）)]}」』】»›")
        sentence_end = bool(re.search(r"[。！？.!?]$", stripped))
        pause = words[at]["begin_ms"] - words[at - 1]["end_ms"] >= 400
        clause_end = bool(re.search(r"[，,；;：:]$", stripped))
        priority = 0 if sentence_end or pause else 1 if clause_end else 2
        after = text[words[at]["char_start"]:words[hi - 1]["char_end"]]
        choices.append((priority, abs(subtitles.display_width(before) - subtitles.display_width(after)), at))
    return min(choices)[2] if choices else None


class LegacyWordBreakIndex:
    def __init__(self, source):
        self.source = source

    def best(self, lo, hi):
        return legacy_word_break(self.source, lo, hi)


def indexed_source(tokens, *, separator=" ", durations=None, pauses=None):
    """Synthetic source with monotonic positive word times and explicit character ranges."""
    words, pieces, char, time = [], [], 0, 1234
    for at, token in enumerate(tokens):
        if at:
            pieces.append(separator)
            char += len(separator)
        time += (pauses or {}).get(at, 0)
        duration = durations[at] if durations else 1000
        words.append(dict(begin_ms=time, end_ms=time + duration, char_start=char, char_end=char + len(token)))
        pieces.append(token)
        char += len(token)
        time += duration
    return dict(sentence_id="0000:00000000", text="".join(pieces), words=words,
                begin_ms=1234, end_ms=time, language="en", decision="translate", reason="ASR", source_lang="auto")


class WordBreakIndexTests(unittest.TestCase):
    def assert_breaks_match(self, source, ranges=None):
        index = subtitles._WordBreakIndex(source)
        count = len(source["words"])
        ranges = ranges or [(lo, hi) for lo in range(count) for hi in range(lo + 1, count + 1)]
        for lo, hi in ranges:
            with self.subTest(text=source["text"], lo=lo, hi=hi):
                self.assertEqual(index.best(lo, hi), legacy_word_break(source, lo, hi))

    def test_unicode_whitespace_and_closing_punctuation_in_all_subranges(self):
        cases = [
            (["hello", "字幕，", "ＡＰＩ", "e\u0301", "👩\u200d💻", "✈\ufe0f", "done.”", "next"], " "),
            (["字。", "）", "）", "甲", "乙", "丙", "丁"], ""),
            (["a.\n”", "b", "c", "d", "e", "f"], " "),
            (["a. \t”", "b,』", "c", "d", "e", "f"], "\u3000\t "),
            (["字", "\u0301", "字", "\u200d", "字", "\ufe0f", "字", "字"], ""),
            (["(open", "close)", "'quote", "end'", "next:", "last"], " " * 100),
            (["un", "break", "able"], ""),
        ]
        for tokens, separator in cases:
            self.assert_breaks_match(indexed_source(tokens, separator=separator))
        self.assertIsNone(subtitles._WordBreakIndex(indexed_source([])).best(0, 0))
        self.assertIsNone(subtitles._WordBreakIndex(indexed_source(["un", "break", "able"], separator="")).best(0, 3))

    def test_tie_pause_and_minimum_duration_thresholds(self):
        self.assertEqual(subtitles._WordBreakIndex(indexed_source(["a"] * 5)).best(0, 5), 2)
        for pause, expected in [(399, 4), (400, 3)]:
            source = indexed_source(["word"] * 8, durations=[500] * 8, pauses={3: pause})
            self.assertEqual(subtitles._WordBreakIndex(source).best(0, 8), expected)
            self.assert_breaks_match(source)
        for durations, expected in [([500, 499, 500, 500], None), ([500] * 4, 2),
                                    ([500, 500, 500, 499], None)]:
            source = indexed_source(["word"] * 4, durations=durations)
            self.assertEqual(subtitles._WordBreakIndex(source).best(0, 4), expected)
        source = indexed_source(["word"] * 8)
        source["text"] = "word " + source["text"]
        for word in source["words"]:
            word["char_start"] += 5
            word["char_end"] += 5
        # The right slice must retain whitespace even when its stored range includes it.
        source["words"][4]["char_start"] -= 1
        self.assert_breaks_match(source)

    def test_fixed_seed_word_timelines_match_for_full_and_internal_ranges(self):
        rng = random.Random(2652110)
        tokens = ["alpha", "can't", "well-known", "字", "全角Ａ", "e\u0301", "👩\u200d💻", "✈\ufe0f",
                  "word.”", "word,", "字；", "（字", "字）", "a.\n”", "a. ”", "a;』"]
        for _ in range(100):
            count = rng.randrange(3, 25)
            source = indexed_source(rng.choices(tokens, k=count),
                                    separator=rng.choice(["", " ", "\t", "\u3000", " \n ", " " * 17]),
                                    durations=rng.choices([1, 199, 499, 500, 999, 1000, 1001], k=count),
                                    pauses={i: rng.choice([0, 399, 400, 700]) for i in range(count)})
            ranges = [(0, count)]
            for _ in range(12):
                lo = rng.randrange(count)
                ranges.append((lo, rng.randrange(lo + 1, count + 1)))
            self.assert_breaks_match(source, ranges)

    def test_complete_reference_initial_and_additional_layout_records_match(self):
        settings = Settings(zh_line_width=22, other_line_width=42)
        sources = [indexed_source(["alpha", "字。”", "e\u0301", "👩\u200d💻", "word,"] * 8),
                   indexed_source(["word" + str(i) + "x" * 95 for i in range(80)], durations=[500] * 80)]
        saved = copy.deepcopy(sources)
        for source in sources:
            def planned():
                references = subtitles.plan_reference_units([source])
                first = subtitles.plan_fragments(source, settings, reference="参考" * 40)
                later = [part for cue in first for part in subtitles.plan_fragments(
                    source, settings, cue=cue, reference="译" * 100, layout_round=1, force=True)]
                return references, first, later
            with mock.patch.object(subtitles, "_WordBreakIndex", LegacyWordBreakIndex):
                expected = planned()
            self.assertEqual(planned(), expected)
        self.assertEqual(sources, saved)

    def test_index_is_lazy_and_reused_only_within_each_planning_call(self):
        short = indexed_source(["short", "sentence"])
        long = indexed_source(["word"] * 40)
        over_budget = indexed_source(["x" * 100] * 80)
        index_type = subtitles._WordBreakIndex
        with mock.patch.object(subtitles, "_WordBreakIndex", wraps=index_type) as build:
            subtitles.plan_fragments(short, Settings())
            subtitles.plan_reference_units([short])
            build.assert_not_called()
            self.assertGreater(len(subtitles.plan_fragments(long, Settings())), 2)
            self.assertEqual(build.call_count, 1)
            build.reset_mock()
            self.assertGreater(len(subtitles.plan_reference_units([over_budget, copy.deepcopy(over_budget)])), 2)
            self.assertEqual(build.call_count, 2)


class PlanningEquivalencePipelineTests(PipelineFixture):
    def run_comparison(self, name):
        raw = timed_raw(["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"], word_ms=1000)
        raw["transcripts"][0]["sentences"].append(dict(begin_time=8000, end_time=8900,
                                                      text="Neighbor sentence.", language="en"))
        fake = FakeService(raw)
        failed = False

        def request(method, url, **kwargs):
            nonlocal failed
            if not url.endswith("/chat/completions"):
                return FakeService.request(fake, method, url, **kwargs)
            fake.calls.append((method, url, kwargs))
            body = kwargs["json"]
            if body["messages"][0]["content"] == "charlie" and not failed:
                failed = True
                return response({"code": "Unauthorized"}, 403)
            text = "中" * (50 if body["translation_options"].get("tm_list") else 10)
            return response({"choices": [{"finish_reason": "stop", "message": {"content": text}}]})

        fake.request = request
        settings = Settings(zh_line_width=22, other_line_width=42)
        config = Config(self.path / name)
        config.settings.workspace_id = "offline-space"
        config.settings.mt_workers = 1
        config.set_key("offline-test-key-no-cloud-authorization")
        with closing(Store(self.path / (name + ".sqlite"))) as store:
            with mock.patch("subtitle_tool.store.uuid.uuid4") as identity:
                identity.return_value.hex = "1" * 32
                job_id = store.enqueue(self.source, workspace_id="offline-space", settings=settings,
                                       output_dir=self.path / name)
            saved_layouts, checkpoints = [], []
            save = store.save_layout
            interrupted = False

            def save_layout(job_id, cues, manifest, **kwargs):
                nonlocal interrupted
                save(job_id, cues, manifest, **kwargs)
                saved_layouts.append(copy.deepcopy((cues, kwargs.get("references"), manifest.get("layout_round"))))
                if manifest.get("layout_round") == 1 and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt()

            with mock.patch("requests.request", side_effect=fake.request), \
                    mock.patch("requests.post", side_effect=fake.upload), \
                    mock.patch.dict("subtitle_tool.api._MT_PACERS", clear=True), \
                    mock.patch.object(store, "save_layout", side_effect=save_layout):
                for attempt, expected in enumerate(["cancelled", "failed", "completed", "completed"]):
                    if attempt in (1, 2):
                        store.update(job_id, "queued")
                    self.assertEqual(Engine(config, store).run(job_id), expected)
                    counts = {k: v for k, v in store.counts(job_id).items() if not k.endswith("_ms")}
                    checkpoints.append((copy.deepcopy(store.cues(job_id)), copy.deepcopy(store.references(job_id)),
                                        counts, len(fake.named("/chat/completions"))))
            manifest = store.job(job_id)["manifest"]
            self.assertEqual(manifest["layout_round"], 2)
            self.assertTrue(manifest["warnings"])
            self.assertEqual([checkpoint[3] for checkpoint in checkpoints], [4, 11, 17, 17])
            content = Path(store.job(job_id)["output"]).read_bytes()
            cache = [tuple(row) for row in store.db.execute("SELECT key,text FROM mt_cache ORDER BY key")]
            return dict(layouts=saved_layouts, checkpoints=checkpoints, srt=content, cache=cache,
                        warnings=manifest["warnings"], sources=store.sources(job_id),
                        requests=[call[2]["json"] for call in fake.named("/chat/completions")])

    def test_old_and_indexed_planning_preserve_pipeline_requests_cache_and_resume(self):
        with mock.patch.object(subtitles, "_WordBreakIndex", LegacyWordBreakIndex):
            expected = self.run_comparison("legacy")
        self.assertEqual(self.run_comparison("indexed"), expected)


if __name__ == "__main__":
    unittest.main()
