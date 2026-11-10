r"""Run from the project root with .venv\Scripts\python.exe."""
import argparse
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subtitle_tool.common import Cancelled, ToolError
from subtitle_tool.config import Config
from subtitle_tool.instance import ProcessLock
from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_gateway_setup import GatewaySetup
from subtitle_tool.mt_catalog import Catalog, offer_billing, read_registry
from subtitle_tool.mt_identity import dynamic_model
from subtitle_tool.mt_channel_policy import load_policy


def main():
    parser = argparse.ArgumentParser(description='验收并更新项目托管的 magpie 三角色组；自动备份。')
    parser.add_argument('--apply', action='store_true', help='将已验收候选应用到角色组')
    parser.add_argument('--providers', help='逗号分隔的已登记渠道 ID；默认读取 data/mt-channels.json')
    parser.add_argument('--list-candidates', action='store_true', help='只读目录预览，不推理、不写验收缓存或分组')
    parser.add_argument('--refresh-trial', action='store_true', help='刷新百炼试用库存并补充候选')
    args = parser.parse_args()
    if args.list_candidates and (args.apply or args.refresh_trial):
        parser.error('--list-candidates 不能与会验收或修改配置的参数同时使用')
    config = Config(read_only=args.list_candidates)
    policy = load_policy(config)
    providers = tuple(dict.fromkeys(p.strip() for p in args.providers.split(',') if p.strip())) if args.providers is not None else tuple(policy['providers'])
    if set(providers) - set(policy['providers']):
        parser.error('请先通过 manage-mt.py --add-provider 登记渠道，或编辑 data/mt-channels.json')
    control = MTControl(threading.Event())
    catalog = Catalog(config, control)
    discovered = catalog.discover(providers, policy=policy) if providers else {'version': 1, 'models': {}, 'identities': {}}
    if args.list_candidates:
        print(json.dumps({'candidates': discovered['identities'], 'needs_exposure': sorted(catalog.pending),
                          'skipped': catalog.skipped}, ensure_ascii=False))
        return 0
    lock = ProcessLock(config.data)
    if not lock.acquire():
        raise ToolError('另一个字幕任务正在运行。', 'busy')
    manager = None
    try:
        setup = GatewaySetup(config, control)
        manager = setup.manager
        setup.catalog['models'].update(discovered['models'])
        setup.catalog['identities'].update(discovered['identities'])
        raw = read_registry(config.settings, setup.catalog, policy=policy)
        by_id = {p['id']: p for p in raw['providers']}
        candidates = set(discovered['identities'])
        for member in setup.current_members():
            if member.split('/', 1)[0].startswith('transcribe-bailian-trial'):
                continue
            if not dynamic_model(member) or member in setup.catalog['identities']:
                candidates.add(member)
        evaluations, free = {}, []
        unavailable = []
        for member in sorted(candidates):
            owner, name = member.split('/', 1)
            if owner in setup.policy['excluded_providers']:
                unavailable.append(member)
                continue
            provider = by_id.get(owner)
            if not provider or provider.get('off') or name in provider.get('modelsOff', []):
                continue
            print(json.dumps({'evaluating': member}), flush=True)
            if member in catalog.pending:
                from subtitle_tool.mt_maintenance import ChannelMaintenance
                exposure = ChannelMaintenance(config, control, trial=manager)
                try:
                    exposure.expose(member)
                finally:
                    exposure.close()
            result = setup.evaluate(member, setup.independent_judge(member))
            if result is None:
                continue
            if not result['roles']:
                unavailable.append(member)
            evaluations[member] = result
            billing = offer_billing(provider, name)
            if billing == 'free':
                free.append(member)
            print(json.dumps({'model': member, 'billing': billing, 'roles': result['roles'],
                'non_thinking': result.get('non_thinking'), 'failures': result.get('failures', []),
                'retry_after': result.get('retry_after')}, ensure_ascii=False), flush=True)
        print(json.dumps({'skipped': catalog.skipped}, ensure_ascii=False), flush=True)
        from subtitle_tool.common import atomic_json
        from datetime import datetime, timezone
        report = {'version': 1, 'suite': 3, 'evaluated_at': datetime.now(timezone.utc).isoformat(),
                  'evaluations': evaluations, 'free': free, 'skipped': catalog.skipped, 'unavailable': unavailable,
                  'offers': {m: {'billing': offer_billing(by_id[m.split('/', 1)[0]], m.split('/', 1)[1]),
                                  'entitlement_usage': 'unknown', 'cash_spend': 'unknown'} for m in evaluations}}
        report_path = config.root / '.build' / 'mt-diagnostics' / 'channel-admission-latest.json'
        atomic_json(report_path, report)
        if args.apply:
            applied = setup.apply(evaluations, free)
            report['application'] = applied
            atomic_json(report_path, report)
            print(json.dumps(applied, ensure_ascii=False), flush=True)
        if args.refresh_trial:
            manager.refresh()
            print(json.dumps({'trial': manager.state.get('pool_status'),
                'models': {m: r['status'] for m, r in manager.state['models'].items()}}, ensure_ascii=False))
        return 0
    finally:
        if manager:
            manager.close()
        lock.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ToolError, Cancelled) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
