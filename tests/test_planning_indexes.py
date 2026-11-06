from __future__ import annotations

import unittest

from subtitle_tool import subtitles


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


if __name__ == "__main__":
    unittest.main()
