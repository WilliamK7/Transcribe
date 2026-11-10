r"""Manage channel maintenance; run with .venv\Scripts\python.exe."""
import argparse
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subtitle_tool.common import Cancelled, ToolError, atomic_json
from subtitle_tool.config import Config
from subtitle_tool.instance import ProcessLock
from subtitle_tool.mt_catalog import read_registry
from subtitle_tool.mt_channel_policy import load_policy
from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_maintenance import ChannelMaintenance


def main():
    parser = argparse.ArgumentParser(description='管理已指定渠道；后台仅在字幕任务运行期间工作。')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--status', action='store_true', help='只读本机状态，不联网、不推理')
    action.add_argument('--enable', action='store_true', help='启用配置清单中的渠道维护，不立即推理')
    action.add_argument('--disable', action='store_true', help='暂停渠道维护，保留现有分组')
    action.add_argument('--once', action='store_true', help='按配置预算执行一次维护')
    action.add_argument('--add-provider', metavar='ID', help='显式登记已有的 Magpie 渠道，不立即推理')
    args = parser.parse_args()
    config = Config(read_only=args.status)
    policy = load_policy(config)
    control = MTControl(threading.Event())
    lock, manager = None, None
    try:
        if not args.status:
            lock = ProcessLock(config.data)
            if not lock.acquire():
                raise ToolError('另一个字幕任务正在运行。', 'busy')
        if args.enable or args.disable or args.add_provider:
            if args.add_provider:
                owner = args.add_provider
                if owner in policy['excluded_providers']:
                    raise ToolError('该渠道已明确排除，不能加入字幕流程。', 'mt_provider_excluded')
                import re
                if not re.fullmatch(r'[A-Za-z0-9_.-]+', owner) or owner.startswith('transcribe-bailian-trial'):
                    raise ToolError('渠道 ID 无效；百炼试用由额度适配器维护。', 'mt_config')
                providers = {p['id']: p for p in read_registry(config.settings)['providers']}
                if owner not in providers:
                    raise ToolError('请先在 Magpie 中接入该渠道。', 'mt_config')
                policy['providers'].setdefault(owner, {'adapter': 'registry', 'preferred': [], 'include': ['*'],
                    'exclude': [], 'billing': ['free', 'commercial'], 'max_candidates': {'free': 3, 'commercial': 3}})
            else:
                policy['enabled'] = bool(args.enable)
            path = config.data / 'mt-channels.json'
            if path.exists():
                backup = config.data / 'gateway-backups' / ('maintenance-' + str(time.time_ns()))
                backup.mkdir(parents=True)
                (backup / path.name).write_bytes(path.read_bytes())
            atomic_json(path, policy)
        manager = ChannelMaintenance(config, control)
        result = manager.refresh() if args.once else manager.status()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        if manager:
            manager.close()
        if lock:
            lock.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ToolError, Cancelled) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
