"""Actual gateway and underlying model identities through persistence and fallback."""
from subtitle_tool.common import ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.mt_identity import VSR_MEMBER
from subtitle_tool.mt_pipeline import validate_provenance
from tests.mt_fixtures import PoolPipelineFixture


class IdentityPipelineTests(PoolPipelineFixture):



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


    def test_reviewer_using_translation_backend_is_rejected(self):
        self.dynamic_route({'x-vsr-selected-model': 'qwen/qwen3.8-27b'})
        actual = self.actual_model
        self.actual_model = lambda stage, request: ('other/qwen3.8-27b' if stage == 'review' and
            request == 'group/transcribe-review' else actual(stage, request))
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([data['_model'] for stage, _, data in self.calls if stage == 'review'],
                         ['group/transcribe-review', 'group/transcribe-review-paid'])
