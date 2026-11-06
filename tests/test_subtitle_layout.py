from __future__ import annotations

import re
import copy
import sqlite3

import unittest

from subtitle_tool.config import Settings
from subtitle_tool.store import Store
from subtitle_tool.subtitles import display_width, layout_warnings, make_fragment, normalize, plan_fragments, render_srt, source_capacity, wrap
from tests.fixtures import timed_raw
from tests import fixtures


def sources_of(raw):
    return normalize(raw, 0, "0000")[0]


class WrappingRegressionTests(unittest.TestCase):
    @staticmethod
    def cue(text, language, translated=''):
        return dict(cue_id='test', begin_ms=1234, end_ms=6234, text=text, language=language,
                    decision='translate' if translated else 'original', translated=translated)

    def test_wider_defaults_reduce_lines_and_preserve_text_and_timing(self):
        narrow = Settings(zh_line_width=22, other_line_width=42)
        cases = [('zh', '字' * 30), ('yue', '字' * 30), ('en', ' '.join(['word'] * 12)),
                 ('ja', 'あ' * 30), ('zh', '字幕' * 8 + ' Python API ' + '字幕' * 8)]
        for language, text in cases:
            with self.subTest(language=language, text=text):
                cue = self.cue(text, language)
                wide = render_srt([cue], Settings()).decode('utf-8').splitlines()
                narrow_rows = render_srt([cue], narrow).decode('utf-8').splitlines()
                self.assertEqual(wide[2:-1], [text])
                self.assertGreater(len(narrow_rows), len(wide))
                self.assertEqual(wide[1], '00:00:01,234 --> 00:00:06,234')

    def test_source_and_translation_each_fit_two_lines(self):
        cue = self.cue(' '.join(['word'] * 24), 'en', '译' * 70)
        settings = Settings()
        source = wrap(cue['text'], settings.other_line_width)
        target = wrap(cue['translated'], settings.zh_line_width * 2)
        self.assertEqual((len(source), len(target)), (2, 2))
        rows = render_srt([cue], settings).decode('utf-8').splitlines()
        self.assertEqual(rows[2:-1], source + target)
        self.assertEqual(' '.join(source), cue['text'])
        self.assertEqual(''.join(target), cue['translated'])
        self.assertEqual(layout_warnings([cue], settings), [])

    def test_wrapping_and_warnings_follow_configured_line_widths(self):
        narrow = Settings(zh_line_width=22, other_line_width=42)
        for language, count, expected in [('zh', 88, 2), ('zh', 89, 3),
                                          ('en', 34, 2), ('en', 35, 3),
                                          ('translation', 88, 2), ('translation', 89, 3)]:
            with self.subTest(language=language, count=count):
                text = ' '.join(['word'] * count) if language == 'en' else '字' * count
                cue = self.cue('Short source.', 'en', text) if language == 'translation' else self.cue(text, language)
                width = Settings().zh_line_width * 2 if language == 'translation' else source_capacity(cue, Settings())
                lines = wrap(text, width)
                self.assertEqual(len(lines), expected)
                self.assertTrue(all(display_width(line) <= width for line in lines))
                self.assertEqual(bool(layout_warnings([cue], Settings())), expected > 2)
                self.assertTrue(layout_warnings([cue], narrow))

    def assert_preserved(self, source, lines):
        self.assertEqual(re.sub(r"\s", "", "".join(lines)), re.sub(r"\s", "", source))

    def test_period_after_twenty_two_han_characters_stays_with_text(self):
        text = "字" * 22 + "。"
        lines = wrap(text, 44)
        self.assertEqual(lines, [text])
        cue = {"begin_ms": 0, "end_ms": 1000, "text": text, "language": "zh", "decision": "original"}
        settings = Settings(zh_line_width=22, other_line_width=42)
        self.assertIn((text + "\r\n").encode(), render_srt([cue], settings))

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
    def test_source_capacity_reduces_splits_but_still_splits_overflow(self):
        narrow = self.settings
        for language, count, word_ms in [('zh', 60, 50), ('en', 24, 150), ('zh', 100, 50), ('en', 40, 100)]:
            with self.subTest(language=language, count=count):
                tokens = ['字' if language == 'zh' else 'word'] * count
                raw = timed_raw(tokens, language=language, word_ms=word_ms, start=1234)
                source = normalize(raw, 0, '0000')[0][0]
                cues = plan_fragments(source, Settings())
                if count in {60, 24}:
                    self.assertEqual(len(cues), 1)
                    self.assertGreater(len(plan_fragments(source, narrow)), 1)
                else:
                    self.assertGreater(len(cues), 1)
                self.assert_ranges(source, cues)
                self.assertEqual(layout_warnings(cues, Settings()), [])

    def test_reference_capacity_controls_fragment_count(self):
        raw = timed_raw(['alpha', 'bravo', 'charlie', 'delta'], word_ms=1200, start=1234)
        source = normalize(raw, 0, '0000')[0][0]
        narrow = self.settings
        self.assertGreater(len(plan_fragments(source, narrow, reference='中' * 70)), 1)
        self.assertEqual(len(plan_fragments(source, Settings(), reference='中' * 70)), 1)
        cues = plan_fragments(source, Settings(), reference='中' * 100)
        self.assertGreater(len(cues), 1)
        self.assert_ranges(source, cues)
        self.assertTrue(all(c['translated'] == '' for c in cues))

    def test_seven_second_split_threshold(self):
        for word_ms, expected in [(700, 1), (701, 2)]:
            with self.subTest(word_ms=word_ms):
                raw = timed_raw(['word'] * 10, word_ms=word_ms, start=1234)
                source = normalize(raw, 0, '0000')[0][0]
                cues = plan_fragments(source, Settings())
                self.assertEqual(len(cues), expected)
                self.assert_ranges(source, cues)

    def setUp(self):
        self.settings = Settings(zh_line_width=22, other_line_width=42)

    def assert_ranges(self, source, cues):
        self.assertEqual(re.sub(r"\s", "", source["text"]), re.sub(r"\s", "", "".join(c["text"] for c in cues)))
        for cue in cues:
            self.assertGreaterEqual(cue["end_ms"] - cue["begin_ms"], 1000)
            self.assertEqual(cue["begin_ms"], source["words"][cue["word_start"]]["begin_ms"])
            self.assertEqual(cue["end_ms"], source["words"][cue["word_end"] - 1]["end_ms"])
        self.assertEqual(len({c["cue_id"] for c in cues}), len(cues))

    def test_text_volume_splits_a_sentence_shorter_than_seven_seconds(self):
        source = sources_of(timed_raw([f"word{i}" for i in range(24)]))[0]
        cues = plan_fragments(source, self.settings)
        self.assertGreater(len(cues), 1)
        self.assert_ranges(source, cues)
        self.assertTrue(all(len(wrap(c["text"], 42)) <= 2 for c in cues))
        self.assertEqual(cues, plan_fragments(source, self.settings))

    def test_reference_expansion_triggers_splitting_without_slicing_translation(self):
        source = sources_of(timed_raw(["alpha", "bravo", "charlie", "delta"], word_ms=1000))[0]
        self.assertEqual(len(plan_fragments(source, self.settings)), 1)
        cues = plan_fragments(source, self.settings, reference="中" * 70)
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
            cues = plan_fragments(source, self.settings, force=True)
            self.assertEqual(cues[0]["word_end"], expected)
            self.assert_ranges(source, cues)

    def test_equal_priority_chooses_balanced_word_volume(self):
        source = sources_of(timed_raw(["word"] * 12, word_ms=250))[0]
        self.assertEqual(plan_fragments(source, self.settings, force=True)[0]["word_end"], 6)

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
            cue = plan_fragments(source, self.settings)[0]
            self.assertEqual((cue["begin_ms"], cue["end_ms"], cue["text"]), (0, 4500, sentence["text"]))
            self.assertTrue(layout_warnings([cue], self.settings))
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
        self.assertEqual(len(plan_fragments(source, self.settings)), 1)
        self.assertEqual(source["end_ms"], 600)

    def test_indivisible_long_word_keeps_content_and_warns(self):
        source = sources_of(timed_raw(["x" * 100], word_ms=2000))[0]
        cues = plan_fragments(source, self.settings)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "x" * 100)
        self.assertTrue(layout_warnings(cues, self.settings))

    def test_asr_subtokens_do_not_split_inside_an_english_word(self):
        raw = timed_raw(["abcdefgh", "ijklmnop"], word_ms=1500)
        raw["transcripts"][0]["sentences"][0]["text"] = "abcdefghijklmnop"
        source = sources_of(raw)[0]
        self.assertEqual(len(plan_fragments(source, self.settings, force=True)), 1)


class PersistenceTests(fixtures.TemporaryTest):

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


if __name__ == "__main__":
    unittest.main()
