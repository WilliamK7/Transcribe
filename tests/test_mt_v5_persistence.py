"""Private stage storage and atomic group publication regressions."""
import sqlite3

from tests.fixtures import SavedResultFixture


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

    def test_stage_records_are_private_and_survive_database_reopen(self):
        self.save_result()
        self.store.commit_mt_stage(self.job_id, 'b' * 64, 'draft', {'text': 'private original'})
        self.store.close()
        from subtitle_tool.store import Store
        self.store = Store(self.config.data / 'jobs.sqlite')
        self.assertEqual(self.store.mt_stage_entry(self.job_id, 'b' * 64),
                         {'stage': 'draft', 'record': {'text': 'private original'}})
