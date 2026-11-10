"""Actual gateway and underlying model identities through persistence and fallback."""
import json
import threading
from pathlib import Path
from subtitle_tool.common import ToolError, atomic_json
from subtitle_tool.engine import Engine
from subtitle_tool.mt_identity import VSR_MEMBER
from subtitle_tool.mt_pipeline import validate_provenance
from tests.mt_fixtures import PoolPipelineFixture


class IdentityPipelineTests(PoolPipelineFixture):
    def resume_old_production_stage(self, stage):
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        manifest['mt_route_snapshot']['format_version'] = 5
        manifest['mt_route_snapshot'].pop('identities')
        self.store.update(self.job_id, manifest=manifest)
        self.review_issue = stage == 'judge'
        self.cancel_after = stage
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        records = self.store.mt_stages(self.job_id)
        self.assertIn(stage, {row['stage'] for row in records})
        self.assertNotIn('final', {row['stage'] for row in records})
        for row in records:
            row['record'].pop('resolved_model', None)
            self.store.commit_mt_stage(self.job_id, row['key'], row['stage'], row['record'])
        production = next(row for row in records if row['stage'] == stage)
        self.cancel_after = None
        self.review_issue = False
        count = len(self.calls)
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed', self.store.job(self.job_id)['error'])
        self.assertNotIn(stage, [name for name, _, _ in self.calls[count:]])
        final = next(row['record'] for row in self.store.mt_stages(self.job_id) if row['stage'] == 'final')
        self.assertEqual(final['production_stage_key'], production['key'])
        self.assertEqual(final['resolved_model'], stage + '-model')
        self.assertEqual(self.store.mt_stage_entry(self.job_id, production['key'])['record'], production['record'])
        validate_provenance(Engine(self.config, self.store), self.job_id, manifest)
        count = len(self.calls)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(len(self.calls), count)

    def test_resume_old_draft_then_validate_new_final_without_retranslation(self):
        self.resume_old_production_stage('translate')

    def test_resume_old_correction_then_validate_new_final_without_regeneration(self):
        self.resume_old_production_stage('judge')

    def dynamic_route(self, header):
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        manifest['mt_route_snapshot']['identities'] = {VSR_MEMBER: {
            'models': ['qwen3.8-27b'], 'admitted': ['qwen3.8-27b'], 'dynamic': True, 'billing': 'free'}}
        self.store.update(self.job_id, manifest=manifest)
        fixed = self.actual_model
        self.actual_model = lambda stage, request: VSR_MEMBER if stage == 'translate' and request == 'group/transcribe' else fixed(stage, request)
        self.response_headers = lambda stage, request: header

    def test_dynamic_translation_persists_both_identities_and_recovers_without_cloud(self):
        self.dynamic_route({'x-vsr-selected-model': 'qwen/qwen3.8-27b'})
        self.assertEqual(self.run_pool(), 'completed')
        stages = self.store.mt_stages(self.job_id)
        for stage in ('translate', 'final'):
            record = next(row['record'] for row in stages if row['stage'] == stage)
            self.assertEqual(record['actual_model'], VSR_MEMBER)
            self.assertEqual(record['resolved_model'], 'qwen3.8-27b')
        count = len(self.calls)
        self.response_headers = lambda *args: {}
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(len(self.calls), count)

    def test_new_final_does_not_supply_missing_dynamic_production_identity(self):
        self.dynamic_route({'x-vsr-selected-model': 'qwen/qwen3.8-27b'})
        self.assertEqual(self.run_pool(), 'completed')
        production = next(row for row in self.store.mt_stages(self.job_id) if row['stage'] == 'translate')
        production['record'].pop('resolved_model')
        self.store.commit_mt_stage(self.job_id, production['key'], production['stage'], production['record'])
        manifest = self.store.job(self.job_id)['manifest']
        with self.assertRaises(ToolError) as caught:
            validate_provenance(Engine(self.config, self.store), self.job_id, manifest)
        self.assertEqual(caught.exception.code, 'cache_mismatch')

    def test_missing_identity_uses_commercial_fallback(self):
        self.dynamic_route({})
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([data['_model'] for stage, _, data in self.calls if stage == 'translate'],
                         ['group/transcribe', 'group/transcribe-paid'])

    def test_unadmitted_underlying_model_uses_commercial_fallback(self):
        self.dynamic_route({'x-vsr-selected-model': 'qwen/unadmitted'})
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(len([1 for stage, _, _ in self.calls if stage == 'translate']), 2)

    def test_listed_but_unobserved_backend_is_not_assumed_qualified(self):
        self.dynamic_route({'x-vsr-selected-model': 'qwen/qwen3.8-27b'})
        manifest = self.store.job(self.job_id)['manifest']
        manifest['mt_route_snapshot']['identities'][VSR_MEMBER]['admitted'] = []
        self.store.update(self.job_id, manifest=manifest)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(len([1 for stage, _, _ in self.calls if stage == 'translate']), 2)

    def test_new_live_dynamic_member_does_not_prevent_old_task_fallback(self):
        self.dynamic_route({'x-vsr-selected-model': 'qwen/qwen3.8-27b'})
        manifest = self.store.job(self.job_id)['manifest']
        identities = manifest['mt_route_snapshot'].pop('identities')
        manifest['mt_route_snapshot']['format_version'] = 5
        self.store.update(self.job_id, manifest=manifest)
        atomic_json(self.config.data / 'gateway-catalog.json', {'version': 1, 'models': {}, 'identities': identities})
        path = Path(self.config.settings.magpie_data_dir) / 'providers.json'
        raw = json.loads(path.read_bytes())
        raw['providers'].append({'id': 'vllm-sr'})
        next(g for g in raw['groups'] if g['id'] == 'transcribe-free')['members'] = [VSR_MEMBER]
        atomic_json(path, raw)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([data['_model'] for stage, _, data in self.calls if stage == 'translate'],
                         ['group/transcribe', 'group/transcribe-paid'])

    def test_reviewer_using_translation_backend_is_rejected(self):
        self.dynamic_route({'x-vsr-selected-model': 'qwen/qwen3.8-27b'})
        actual = self.actual_model
        self.actual_model = lambda stage, request: ('other/qwen3.8-27b' if stage == 'review' and
            request == 'group/transcribe-review' else actual(stage, request))
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([data['_model'] for stage, _, data in self.calls if stage == 'review'],
                         ['group/transcribe-review', 'group/transcribe-review-paid'])

    def test_old_fixed_records_recover_but_dynamic_records_without_proof_remain_untrusted(self):
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        manifest['mt_route_snapshot']['format_version'] = 5
        manifest['mt_route_snapshot'].pop('identities')
        self.store.update(self.job_id, manifest=manifest)
        self.assertEqual(self.run_pool(), 'completed')
        for row in self.store.mt_stages(self.job_id):
            row['record'].pop('resolved_model', None)
            self.store.commit_mt_stage(self.job_id, row['key'], row['stage'], row['record'])
        count = len(self.calls)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(len(self.calls), count)
        for row in self.store.mt_stages(self.job_id):
            if row['stage'] in ('translate', 'final'):
                row['record']['actual_model'] = VSR_MEMBER
                self.store.commit_mt_stage(self.job_id, row['key'], row['stage'], row['record'])
        with self.assertRaises(ToolError):
            validate_provenance(Engine(self.config, self.store), self.job_id, manifest)
        self.assertTrue(self.store.mt_stages(self.job_id))
