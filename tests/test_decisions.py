from __future__ import annotations

import copy
import io
from unittest import mock

from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.main import review
from subtitle_tool.subtitles import make_fragment, source_context, validate_fragment
from tests.fixtures import SavedResultFixture, result, timed_raw


class DecisionTests(SavedResultFixture):
    def test_manual_origin_survives_changed_display_reason_and_reflow(self):
        sources = self.save_result(result(("OK", "")))
        old = copy.deepcopy(sources)
        old[0].update(decision="translate", source_lang="English", decision_origin="manual", reason="已确认")
        Engine.preserve_review_decisions(sources, old, [make_fragment(old[0])])
        self.assertEqual((sources[0]["decision"], sources[0]["source_lang"], sources[0]["decision_origin"]),
                         ("translate", "English", "manual"))

    def test_context_uses_origin_and_explicit_origin_overrides_legacy_reason(self):
        sources = self.save_result(result(("One.", "en"), ("Two.", "en")))
        sources[1].update(decision_origin="manual", reason="改过的显示文案")
        self.assertEqual(source_context(sources, 0), [])
        sources[1].update(decision_origin="automatic", reason="用户在终端确认")
        self.assertEqual(len(source_context(sources, 0)), 1)
        del sources[1]["decision_origin"]
        self.assertEqual(source_context(sources, 0), [])

    def test_invalid_origin_and_source_fragment_disagreement_are_rejected(self):
        source = self.save_result()[0]
        cue = make_fragment(source)
        for value in ["manual", "unexpected", None]:
            with self.subTest(value=value), self.assertRaises(ToolError) as error:
                validate_fragment(source, {**cue, "decision_origin": value})
            self.assertEqual(error.exception.code, "cache_mismatch")

    def test_review_updates_all_fragments_atomically_and_invalidates_context(self):
        from subtitle_tool.review import ReviewDecision
        sources = self.save_result(timed_raw(["first", "second"]))
        source = sources[0]
        cues = [make_fragment(source, 0, 1), make_fragment(source, 1, 2)]
        for cue in cues:
            cue["translated"] = "旧译文"
        manifest = self.store.job(self.job_id)["manifest"]
        manifest.update(references_planned=True, layout_planned=True, layout_complete=True,
                        expected_srt_sha256="obsolete", publication_intent={"old": True})
        self.store.save_layout(self.job_id, cues, manifest, references=[{**make_fragment(source), "cue_id": "reference"}])
        manifest.update(expected_srt_sha256="obsolete", publication_intent={"old": True})
        self.store.update(self.job_id, manifest=manifest)
        self.store.update(self.job_id, "needs_review", code="language_uncertain")
        apply = lambda: self.store.apply_review(self.job_id, cues[0]["cue_id"], ReviewDecision("translate", "English"))
        before = (self.store.job(self.job_id), self.store.cues(self.job_id), self.store.references(self.job_id))
        with mock.patch.object(self.store, "_replace_records", side_effect=OSError()), self.assertRaises(OSError):
            apply()
        self.assertEqual(self.store.sources(self.job_id), sources)
        self.assertEqual((self.store.job(self.job_id), self.store.cues(self.job_id), self.store.references(self.job_id)), before)
        apply()
        for record in self.store.sources(self.job_id) + self.store.cues(self.job_id):
            self.assertEqual((record["decision"], record["source_lang"], record["decision_origin"]),
                             ("translate", "English", "manual"))
            self.assertFalse(record.get("translated"))
        self.assertEqual(self.store.references(self.job_id), [])
        saved = self.store.job(self.job_id)["manifest"]
        self.assertFalse(any(k in saved for k in ["references_planned", "layout_complete", "expected_srt_sha256", "publication_intent"]))

    def test_pause_preserves_prior_confirmation_and_skips_sibling_prompts(self):
        self.save_result(result(("OK", ""), ("Hmm", "")))
        self.store.update(self.job_id, "needs_review", code="language_uncertain")
        with mock.patch("builtins.input", side_effect=["o", "q"]) as prompt, mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertFalse(review(self.config, self.store, Engine(self.config, self.store), self.job_id))
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual([c["decision"] for c in self.store.cues(self.job_id)], ["original", "uncertain"])
        self.assertEqual(self.store.job(self.job_id)["state"], "needs_review")

    def test_sibling_confirmation_requires_only_one_prompt(self):
        source = self.save_result(timed_raw(["first", "second"]))[0]
        source["decision"] = "uncertain"
        self.store.initialize_subtitles(self.job_id, [source], [make_fragment(source, 0, 1), make_fragment(source, 1, 2)],
                                        self.store.job(self.job_id)["manifest"])
        self.store.update(self.job_id, "needs_review", code="language_uncertain")
        with mock.patch("builtins.input", side_effect=["o"]) as prompt, mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertTrue(review(self.config, self.store, Engine(self.config, self.store), self.job_id))
        self.assertEqual(prompt.call_count, 1)
        self.assertTrue(all(c["decision_origin"] == "manual" for c in self.store.cues(self.job_id)))
