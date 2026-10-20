from __future__ import annotations

import unittest
from unittest import mock

from subtitle_tool import subtitles
from subtitle_tool.engine import Engine
from tests.fixtures import FakeService, PipelineFixture, response, timed_raw


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
    def test_unicode_punctuation_and_word_boundaries_in_subranges(self):
        cases = [
            (["hello", "字幕，", "ＡＰＩ", "e\u0301", "👩\u200d💻", "✈\ufe0f", "done.”", "next"], " ", [7, 7, 2]),
            (["字。", "）", "）", "甲", "乙", "丙", "丁"], "", [3, 4, 3]),
            (["a.\n”", "b", "c", "d", "e", "f"], " ", [1, 3, 1]),
            (["a. \t”", "b,』", "c", "d", "e", "f"], "\u3000\t ", [2, 2, 2]),
            (["字", "\u0301", "字", "\u200d", "字", "\ufe0f", "字", "字"], "", [6, 6, 2]),
            (["(open", "close)", "'quote", "end'", "next:", "last"], " " * 100, [5, 5, 2]),
            (["un", "break", "able"], "", [None, None, None]),
        ]
        for tokens, separator, expected in cases:
            with self.subTest(tokens=tokens, separator=separator):
                index = subtitles._WordBreakIndex(indexed_source(tokens, separator=separator))
                count = len(tokens)
                self.assertEqual([index.best(lo, hi) for lo, hi in [(0, count), (1, count), (0, count - 1)]],
                                 expected)
        self.assertIsNone(subtitles._WordBreakIndex(indexed_source([])).best(0, 0))

    def test_tie_pause_and_minimum_duration_thresholds(self):
        self.assertEqual(subtitles._WordBreakIndex(indexed_source(["a"] * 5)).best(0, 5), 2)
        for pause, expected in [(399, 4), (400, 3)]:
            source = indexed_source(["word"] * 8, durations=[500] * 8, pauses={3: pause})
            self.assertEqual(subtitles._WordBreakIndex(source).best(0, 8), expected)
        for durations, expected in [([500, 499, 500, 500], None), ([500] * 4, 2),
                                    ([500, 500, 500, 499], None)]:
            source = indexed_source(["word"] * 4, durations=durations)
            self.assertEqual(subtitles._WordBreakIndex(source).best(0, 4), expected)


class ContextPositionTests(unittest.TestCase):
    @staticmethod
    def sources():
        sentences = [timed_raw([f"sentence{i}", "ending"], word_ms=1000, start=i * 2100)
                     ["transcripts"][0]["sentences"][0] for i in range(7)]
        sources = subtitles.normalize({"transcripts": [{"sentences": sentences}]}, 0, "0000",
                                      preserve_sentences=True)[0]
        return [{**source, "reference": f"参考{i}"} for i, source in enumerate(sources)]

    def test_parent_priority_distance_order_and_whole_entry_budget(self):
        sources = self.sources()
        for lo, hi, expected in [(0, 2, [2, 4, 1, 5]), (0, 1, [3, 2, 4, 1, 5]),
                                 (1, 2, [3, 4, 2, 5, 1])]:
            cue = subtitles.make_fragment(sources[3], lo, hi)
            actual = subtitles.translation_context(cue, sources, position=3)
            self.assertEqual(actual, [{"source": sources[i]["text"], "target": sources[i]["reference"]}
                                      for i in expected])
        sources[3]["reference"] = "中" * 3000
        cue = subtitles.make_fragment(sources[3], 0, 1)
        actual = subtitles.translation_context(cue, sources, position=3)
        self.assertEqual(actual, [{"source": sources[i]["text"], "target": sources[i]["reference"]}
                                  for i in [2, 4, 1, 5]])


class PlanningResumeTests(PipelineFixture):
    translation_strategy_version = 2

    def test_layout_interruption_and_translation_failure_resume_to_complete_output(self):
        raw = timed_raw(["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"], word_ms=1000)
        raw["transcripts"][0]["sentences"].append(dict(begin_time=8000, end_time=8900,
                                                     text="Neighbor sentence.", language="en"))
        fake = FakeService(raw)
        failed = interrupted = False
        self.config.settings.mt_workers = 1
        manifest = self.store.job(self.job_id)['manifest']
        manifest['subtitle_settings'].update(zh_line_width=22, other_line_width=42)
        self.store.update(self.job_id, manifest=manifest)

        def request(method, url, **kwargs):
            nonlocal failed
            if not url.endswith('/chat/completions'):
                return fake.request(method, url, **kwargs)
            fake.calls.append((method, url, kwargs))
            body = kwargs['json']
            if body['messages'][0]['content'] == 'charlie' and not failed:
                failed = True
                return response({'code': 'Unauthorized'}, 403)
            text = '中' * (50 if body['translation_options'].get('tm_list') else 10)
            return response({'choices': [{'finish_reason': 'stop', 'message': {'content': text}}]})

        save = self.store.save_layout

        def save_layout(job_id, cues, manifest, **kwargs):
            nonlocal interrupted
            save(job_id, cues, manifest, **kwargs)
            if manifest.get('layout_round') == 1 and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with mock.patch('requests.request', side_effect=request), \
                mock.patch('requests.post', side_effect=fake.upload), \
                mock.patch.object(self.store, 'save_layout', side_effect=save_layout):
            for expected, calls in [('cancelled', 4), ('failed', 11), ('completed', 17)]:
                self.store.update(self.job_id, 'queued')
                self.assertEqual(Engine(self.config, self.store).run(self.job_id), expected)
                self.assertEqual(len(fake.named('/chat/completions')), calls)
            completed = self.store.job(self.job_id)
            content = self.source.with_suffix('.srt').read_bytes()
            self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'completed')
            self.assertEqual(self.store.job(self.job_id), completed)
            self.assertEqual(self.source.with_suffix('.srt').read_bytes(), content)
            self.assertEqual(len(fake.named('/chat/completions')), 17)
        self.assertEqual(completed['manifest']['layout_round'], 2)
        self.assertTrue(completed['manifest']['warnings'])
        self.assertTrue(all(c['translated'] for c in self.store.cues(self.job_id)))


if __name__ == "__main__":
    unittest.main()
