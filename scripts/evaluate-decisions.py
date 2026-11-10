r"""Synthetic benchmark via Magpie; no production policy writes."""
import argparse
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subtitle_tool.common import Cancelled, ToolError, atomic_json
from subtitle_tool.config import Config
from subtitle_tool.instance import ProcessLock
from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_decisions import DEFAULT_MODELS, evaluate
from subtitle_tool.mt_diagnostics import safe_model_id


def main():
    parser = argparse.ArgumentParser(description='独立评测 32 个合成字幕质量样本，不修改生产复核。')
    parser.add_argument('--models', default=','.join(DEFAULT_MODELS), help='逗号分隔的 Magpie System One 成员 ID')
    args = parser.parse_args()
    models = tuple(dict.fromkeys(m.strip() for m in args.models.split(',') if m.strip()))
    if not models or len(models) > 8 or any(safe_model_id(m) != m for m in models):
        parser.error('需要 1～8 个有效的网关模型 ID')
    config = Config()
    lock = ProcessLock(config.data)
    if not lock.acquire():
        raise ToolError('另一个字幕任务正在运行。', 'busy')
    try:
        report = evaluate(config, MTControl(threading.Event()), models,
                          on_progress=lambda row: print(json.dumps(row, ensure_ascii=False), flush=True))
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        path = config.root / '.build' / 'mt-diagnostics' / ('decisions-' + stamp + '.json')
        atomic_json(path, report)
        print(json.dumps({'report': str(path), 'summaries': {m: v['summary'] for m, v in report['models'].items()}},
                         ensure_ascii=False), flush=True)
    finally:
        lock.close()


if __name__ == '__main__':
    try:
        main()
    except (ToolError, Cancelled) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
