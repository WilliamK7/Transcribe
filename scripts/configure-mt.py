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


def main():
    parser = argparse.ArgumentParser(description='验收并迁移项目托管的 magpie 三角色组；自动备份。')
    parser.add_argument('--apply', action='store_true', help='将已验收候选应用到角色组')
    parser.add_argument('--discover-openrouter', action='store_true', help='验收至多两个明确的免费型号')
    parser.add_argument('--refresh-trial', action='store_true', help='刷新百炼试用库存并补充候选')
    args = parser.parse_args()
    config = Config()
    lock = ProcessLock(config.data)
    if not lock.acquire():
        raise ToolError('另一个字幕任务正在运行。', 'busy')
    manager = None
    try:
        setup = GatewaySetup(config, MTControl(threading.Event()))
        manager = setup.manager
        raw = manager._registry()
        evaluations, free = {}, []
        judge = 'transcribe-qiniu-paid/z-ai/glm-5.3-flash'
        for provider in raw['providers']:
            if provider['id'] not in {'transcribe-bailian', 'transcribe-qiniu-paid', 'transcribe-qiniu-free'} or provider.get('off'):
                continue
            for model in provider.get('models', []):
                member = provider['id'] + '/' + model
                independent = 'transcribe-bailian/qwen3.8-flash' if member == judge else judge
                result = setup.evaluate(member, independent)
                if result is not None:
                    evaluations[member] = result
                    if provider['id'] == 'transcribe-qiniu-free':
                        free.append(member)
                    print(json.dumps({'model': model, 'roles': result['roles'],
                        'non_thinking': result.get('non_thinking')}, ensure_ascii=False), flush=True)
        if args.discover_openrouter:
            provider = next((p for p in raw['providers'] if p['id'] == 'transcribe-openrouter-free'), None)
            if provider and not provider.get('off'):
                for model in setup.openrouter_candidates()[:2]:
                    manager._command('provider', 'models', provider['id'], '+' + model)
                    member = provider['id'] + '/' + model
                    result = setup.evaluate(member, judge)
                    evaluations[member] = result
                    free.append(member)
                    print(json.dumps({'model': model, 'roles': result['roles']}, ensure_ascii=False), flush=True)
        if args.apply:
            print(json.dumps(setup.apply(evaluations, free), ensure_ascii=False), flush=True)
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
