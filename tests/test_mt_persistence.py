"""Private stage storage and atomic group publication regressions."""
import sqlite3

from subtitle_tool.common import ToolError
from tests.fixtures import SavedResultFixture, result


class StagePersistenceTests(SavedResultFixture):
    def test_completed_stage_and_all_cues_commit_atomically(self):
        self.save_result()
        cue = self.store.cues(self.job_id)[0]
        manifest = self.store.job(self.job_id)['manifest']
        manifest['mt_route_snapshot'] = {'entries': [{'id': 'qiniu_free'}]}
        self.store.update(self.job_id, manifest=manifest)
        record = {'translations': {'0': '完整译文'}, 'backend': 'qiniu_free', 'request_model': 'qiniu/conatus-free'}
        self.store.db.execute("CREATE TRIGGER reject_stage BEFORE INSERT ON mt_stages "
                              "BEGIN SELECT RAISE(ABORT,'synthetic rejection'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.commit_mt_stage(self.job_id, 'a' * 64, 'align', record,
                                       translations={cue['cue_id']: '完整译文'}, origin='qiniu_free')
        self.assertFalse(self.store.cues(self.job_id)[0]['translated'])
        self.assertIsNone(self.store.mt_stage_entry(self.job_id, 'a' * 64))
        self.store.db.execute('DROP TRIGGER reject_stage')
        self.store.commit_mt_stage(self.job_id, 'a' * 64, 'align', record,
                                   translations={cue['cue_id']: '完整译文'}, origin='qiniu_free')
        self.assertEqual(self.store.cues(self.job_id)[0]['translated'], '完整译文')
        self.assertEqual(self.store.mt_stage_entry(self.job_id, 'a' * 64),
                         {'stage': 'align', 'record': record})

    def test_stages_and_cues_survive_reopen(self):
        self.save_result()
        cues = self.store.cues(self.job_id)
        self.store.commit_mt_stage(self.job_id, 'b' * 64, 'draft', {'text': 'private original'})
        self.store.close()
        from subtitle_tool.store import Store
        self.store = Store(self.config.data / 'jobs.sqlite')
        self.assertEqual(self.store.cues(self.job_id), cues)
        self.store.replace_cues(self.job_id, cues)
        self.assertEqual(self.store.cues(self.job_id), cues)
        self.assertEqual(self.store.mt_stage_entry(self.job_id, 'b' * 64),
                         {'stage': 'draft', 'record': {'text': 'private original'}})

    def test_group_update_preserves_metadata_and_rolls_back_missing_cue(self):
        self.save_result(result(('First.', 'en'), ('Second.', 'en')))
        cues = self.store.cues(self.job_id)
        origin = self.store.job(self.job_id)['manifest']['mt_route_snapshot']['entries'][0]['id']
        values = {cue['cue_id']: '完整译文' for cue in cues}
        options = dict(origin=origin, counts={'mt_subtitle_completed': 2},
                       cache_key='synthetic-cache', cache_value='synthetic-value')
        with self.assertRaises(ToolError):
            self.store.commit_mt_stage(self.job_id, 'c' * 64, 'final', {},
                                       translations={**values, 'missing-cue': '译文'}, **options)
        self.assertEqual(self.store.cues(self.job_id), cues)
        self.assertIsNone(self.store.cache_get('synthetic-cache'))
        self.assertIsNone(self.store.mt_stage_entry(self.job_id, 'c' * 64))
        self.assertEqual(self.store.counts(self.job_id), {})
        self.store.commit_mt_stage(self.job_id, 'c' * 64, 'final', {}, translations=values, **options)
        for before, after in zip(cues, self.store.cues(self.job_id)):
            self.assertEqual(after, {**before, 'translated': '完整译文', 'mt_origin': origin, 'mt_stage_key': 'c' * 64})
        self.assertEqual(self.store.counts(self.job_id), {'mt_subtitle_completed': 2})
        self.assertEqual(self.store.cache_get('synthetic-cache'), 'synthetic-value')
