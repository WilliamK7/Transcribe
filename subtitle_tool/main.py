from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import signal
import sys
import threading
import time
from pathlib import Path

from .common import ASR_MODEL, ToolError, validate_workspace_id
from .config import Config
from .engine import Engine
from .instance import ProcessLock
from .magpie_process import MagpieProcess
from .reports import ReportWriter
from .store import Store
from .lifecycle import STATES, contract as lifecycle_contract, describe as describe_lifecycle
from .review import MT_LANGUAGES, ReviewDecision


def parser():
    result = argparse.ArgumentParser(description="命令行字幕工具（magpie 网关／北京百炼 ASR）")
    result.add_argument("files", nargs="*", help="本地视频／音频文件，逐个处理")
    actions = result.add_mutually_exclusive_group()
    actions.add_argument("--resume", metavar="JOB_ID", help="恢复本地任务")
    actions.add_argument("--reflow", metavar="JOB_ID", help="仅从完整本地转写重排字幕，覆盖须搭配 --overwrite")
    actions.add_argument("--retime", metavar="JOB_ID", help="复用最终字幕和译文，仅升级显示时间；覆盖须搭配 --overwrite")
    actions.add_argument("--review", metavar="JOB_ID", help="终端确认音轨、声道或逐条语种后继续")
    actions.add_argument("--jobs", action="store_true", help="列出任务（每行 JSON）")
    actions.add_argument('--state-machine', action='store_true', help='输出任务状态契约 JSON，无需配置或任务库')
    actions.add_argument("--export-archive", metavar="JOB_ID", help="校验并导出归档字幕，须指定 --output-dir")
    actions.add_argument("--cancel", metavar="JOB_ID", help="停止已保存的本地任务")
    actions.add_argument('--configure-key', action='store_true', help='隐藏输入百炼 Key，保存到 data/key.txt')
    actions.add_argument('--check-mt', action='store_true', help='用合成文本在线检查 magpie 字幕组')
    actions.add_argument("--self-test", action="store_true", help="仅执行本机诊断，不访问云端")
    result.add_argument("--workspace-id", help="本次新任务使用的北京 Workspace ID（域名标签）")
    result.add_argument("--bind-task-id", help="给提交未知的任务绑定已知云端 ID，需 --resume")
    result.add_argument("--allow-resubmit", action="store_true", help="明确接受未知提交可能重复计费的重发，需 --resume")
    result.add_argument("--overwrite", action="store_true", help="先备份再覆盖现有字幕")
    result.add_argument("--track", type=int, help="ffprobe 返回的音轨索引")
    result.add_argument("--channel", choices=["mix", "left", "right"], help="明确混音／首声道／第二声道")
    result.add_argument("--output-dir", type=Path, help="显式选择输出目录，默认视频同目录")
    result.add_argument("--non-interactive", action="store_true", help="不询问；遇歧义持久化待复核状态并非零退出")
    result.add_argument("--report", type=Path, help="保存结构化结果，不包含 Key、上传链接或转写文字")
    return result


def ask(prompt):
    print(prompt, file=sys.stderr, end="", flush=True)
    try:
        return input().strip()
    except EOFError:
        return "q"


class Progress:
    def __init__(self):
        self.lock = threading.Lock()
        self.previous = {}
        self.uploads = {}

    def __call__(self, job_id, stage, done=None, total=None):
        with self.lock:
            now = time.monotonic()
            if stage == "uploading" and isinstance(done, int) and total:
                start, last = self.uploads.get(job_id, (now, 0))
                if done == 0 or done < last:
                    start = now
                self.uploads[job_id] = (start, done)
                speed = done / max(0.001, now - start)
                message = f"上传 {done:,}/{total:,} 字节，{speed / 1024:.1f} KiB/s"
                previous = self.previous.get(job_id)
                if done != total and previous and previous[0] == stage and now - previous[1] < 0.5:
                    return
            elif stage == "audio_ready":
                method = "直接复制" if done["method"] == "copy" else "16kHz 单声道 FLAC"
                message = f"音频：{method}，{done['codec']}/{done['container']}，{done['segments']} 段，{done['bytes']:,} 字节；{done['reason']}"
            elif stage == "retry":
                message = f"网络重试 {done}/{total}，有限退避"
            elif stage == 'mt_v6_progress':
                names = {'glossary': '术语分析', 'waiting': '等待前序术语', 'translate': '字幕翻译',
                         'review': '译文复核', 'judge': '疑点裁决'}
                parts = [f"{names[name]} {count} 组" for name, count in done['stages'].items() if name in names]
                message = f"翻译进行中：{'、'.join(parts)}；当前阶段最长 {done['elapsed']} 秒"
            elif stage == "subtitle_translating" and total is not None:
                message = f"{STATES[stage]}／已处理 {done}/{total}"
            elif stage == "layout_adjusting" and total is not None:
                message = f"排版调整 {done}/{total}"
            elif stage == "recognizing" and done:
                message = "识别：" + str(done)
            else:
                message = STATES.get(stage, stage)
            previous = self.previous.get(job_id)
            if previous and previous[2] == message:
                return
            print(f"[{job_id}] {message}", file=sys.stderr, flush=True)
            self.previous[job_id] = (stage, now, message)


def review(config, store, engine, job_id):
    job = store.job(job_id)
    manifest = job["manifest"]
    if job["error_code"] in {"audio_track", "audio_channel"}:
        info, selected = engine.review_media(job_id)
        for track in info["tracks"]:
            tags = track.get("tags", {})
            print(f"音轨 {track['index']}：{track.get('codec_name', '?')}，{track.get('channels', '?')} 声道，"
                  f"语言 {tags.get('language', '?')}，起始 {track.get('start_time', '?')} 秒", file=sys.stderr)
        if job["error_code"] == "audio_track":
            while True:
                answer = ask("选择音轨索引（q 暂停）：")
                if answer.lower() == "q":
                    return False
                if answer.isascii() and answer.isdigit() and int(answer) in {t["index"] for t in info["tracks"]}:
                    selected = int(answer)
                    break
                print("请输入列表中的音轨索引。", file=sys.stderr)
        track = next(t for t in info["tracks"] if t["index"] == selected)
        channel = manifest.get("channel", config.settings.channel)
        if int(track.get("channels", 1)) > 1 and channel == "ask":
            while True:
                channel = ask("声道（left 首声道／right 第二声道／mix 明确混音／q 暂停）：").lower()
                if channel == "q":
                    return False
                if channel in {"left", "right", "mix"}:
                    break
                print("请输入 left、right 或 mix。", file=sys.stderr)
        engine.select_input(job_id, selected, None if channel == "ask" else channel)
    else:
        uncertain = [c for c in store.cues(job_id) if c["decision"] == "uncertain"]
        if not uncertain:
            raise ToolError("此任务没有待确认的音轨／声道／语种。请用 --resume 重试当前阶段。", "review")
        for cue in uncertain:
            if not any(c["cue_id"] == cue["cue_id"] and c["decision"] == "uncertain" for c in store.cues(job_id)):
                continue
            print(f"字幕 {cue['cue_id']}（ASR 语种 {cue['language'] or '缺失'}，{cue['reason']}）：\n{cue['text']}", file=sys.stderr)
            while True:
                choice = ask("o 保留原文／t 翻译为中文／q 暂停：").lower()
                if choice == "q":
                    return False
                if choice in {"o", "t"}:
                    break
                print("请输入 o、t 或 q。", file=sys.stderr)
            source_lang = "auto"
            if choice == "t":
                while True:
                    source_lang = ask("MT 源语言（回车 auto，或接口名称如 English/Japanese；q 暂停）：") or "auto"
                    if source_lang == "q":
                        return False
                    if source_lang in MT_LANGUAGES:
                        break
                    print("请使用 auto 或受支持的接口语言名称，不能传 ASR 的 en/ja 标签。", file=sys.stderr)
            store.apply_review(job_id, cue["cue_id"], ReviewDecision("original" if choice == "o" else "translate", source_lang))
    engine.finish_review(job_id)
    return True


def lifecycle_status(job):
    """Inspect saved compatibility without keys, filesystem changes or cloud calls."""
    from .policy import load_policy
    from .mt_pool import saved_route
    reflow_required = False
    try:
        load_policy(job['manifest'])
        if 'mt_route_snapshot' in job['manifest']:
            saved_route(job['manifest'])
    except ToolError as error:
        reflow_required = error.code != 'job_archived'
    return describe_lifecycle(job, reflow_required=reflow_required)


def summary(store, job_id):
    job = store.job(job_id)
    manifest = job["manifest"]
    item = {"job_id": job_id, "state": job["state"], "source": job["source"], "output": job["output"],
            "counts": store.counts(job_id), "error_code": job["error_code"], "request_id": job["request_id"],
            "region": manifest["region"], "workspace_id": manifest.get("workspace_id", ""),
            "access_mode": manifest.get("access_mode"),
            "processing_version": manifest.get("processing_version"), "cue_count": len(store.cues(job_id)),
            "subtitle_version": manifest.get("subtitle_version", 0),
            "display_timing_version": manifest.get("display_timing_version", 0),
            "display_timing_adjustments": manifest.get("display_timing_adjustments", []),
            "translation_strategy_version": manifest.get("translation_strategy_version", 0),
            "layout_round": manifest.get("layout_round", 0), "warnings": manifest.get("warnings", []),
            "no_speech": manifest.get("no_speech", False), "output_sha256": manifest.get("expected_srt_sha256")}
    if 'mt_route_snapshot' in manifest and not manifest.get('archived'):
        from .mt_pool import saved_route
        from .policy import load_policy
        try:
            load_policy(manifest)
            route = saved_route(manifest)
        except ToolError:
            # Historical results remain listable for explicit reflow.
            mt = {'provider': None, 'model': None}
            item['mt_route_status'] = 'reflow_required'
        else:
            mt = next(entry['backend'] for entry in route['entries'] if entry['id'] == 'magpie')
            item['mt_route_status'] = 'current'
            item['mt_policy_version'] = mt['policy_version']
            item['gateway_session_prefix'] = 'transcribe-' + job_id + '-'
            item['mt_route'] = {'strategy': route['routing']['strategy'],
                                'entries': [{'id': entry['id'], 'tier': entry['tier'],
                                             'model': entry['backend']['model']}
                                            for entry in route['entries']]}
    else:
        mt = manifest.get("mt_snapshot", {})
    item["models"] = {"asr": manifest.get("contract", {}).get("asr", ASR_MODEL), "mt": mt.get("model", manifest.get("contract", {}).get("mt"))}
    item["mt_provider"] = mt.get("provider")
    item["task_ids"] = [s["task_id"] for s in manifest["segments"] if s.get("task_id")]
    counts = item["counts"]
    item["timings"] = {"run_seconds": counts.get("run_ms", 0) / 1000,
                       "stages_seconds": {k[6:-3]: v / 1000 for k, v in counts.items()
                                          if k.startswith("stage_") and k.endswith("_ms")}}
    if manifest.get("reflow"):
        item["reflow"] = {k: manifest["reflow"][k] for k in ["prepared_at", "from_subtitle_version"]}
    if manifest.get("retime"):
        item["retime"] = {k: manifest["retime"][k] for k in ["prepared_at", "from_display_timing_version"]}
    if manifest.get("media_plan"):
        item["audio"] = {k: manifest["media_plan"][k] for k in ["method", "codec", "container", "audio_offset_ms"]}
        item["audio"]["segments"] = len(manifest["segments"])
        item["audio"]["bytes"] = sum(s.get("bytes", 0) for s in manifest["segments"])
    item['lifecycle'] = lifecycle_status(job)
    return item


def hint(store, job_id):
    job = store.job(job_id)
    if job["error"]:
        print(job["error"], file=sys.stderr)
    for action in lifecycle_status(job)['next_actions']:
        arguments = list(action['argv'])
        for option, value in action['required_options'].items():
            arguments.extend([option, value])
        print(action['label'] + '：.\\scripts\\transcribe.ps1 ' + ' '.join(arguments), file=sys.stderr)


def main(argv=None):
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    action = (args.resume or args.reflow or args.retime or args.review or args.jobs or args.state_machine or args.cancel or
              args.check_mt or args.self_test or args.configure_key or
              args.export_archive)
    if not args.files and not action:
        argument_parser.print_help()
        return 0
    if args.files and action:
        raise ToolError("处理文件与管理／恢复命令不能同时使用。", "input")
    if (args.bind_task_id or args.allow_resubmit) and not args.resume:
        raise ToolError("绑定或确认重发必须指定 --resume JOB_ID。", "input")
    if args.bind_task_id and args.allow_resubmit:
        raise ToolError("绑定已知 ID 与重新提交只能选择一种。", "input")
    if args.track is not None and args.track < 0:
        raise ToolError("音轨索引不能为负。", "input")
    if (args.reflow or args.retime) and (args.track is not None or args.channel is not None):
        raise ToolError("重排／校时沿用已保存的识别输入，不能指定音轨／声道。", "input_committed")
    if args.state_machine:
        if (args.report is not None or args.output_dir is not None or args.workspace_id is not None or
                args.track is not None or args.channel is not None or args.overwrite):
            raise ToolError('--state-machine 仅输出 stdout JSON，不能搭配任务设置或 --report。', 'input')
        print(json.dumps(lifecycle_contract(), ensure_ascii=False))
        return 0
    stdout_self_test = args.self_test and args.report is None
    config = Config(read_only=stdout_self_test)
    if args.workspace_id is not None:
        config.settings.workspace_id = validate_workspace_id(args.workspace_id)
    interactive = not args.non_interactive and sys.stdin is not None and sys.stdin.isatty()
    lock = None if stdout_self_test else ProcessLock(config.data)
    if lock is not None and not lock.acquire():
        raise ToolError("任务库已被其他 CLI 进程使用；请在该终端 Ctrl+C 停止后重试。", "instance_busy")
    store, current_job, report_writer = None, None, None
    cancel = threading.Event()
    magpie = MagpieProcess(config.settings)
    previous_signal = None
    processing = False
    try:
        if args.self_test:
            from .diagnostics import self_test
            result = self_test(config, args.report)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.configure_key:
            if not interactive:
                raise ToolError('请在交互终端配置百炼 Key，或设置 DASHSCOPE_API_KEY。', 'credentials')
            config.set_key(getpass.getpass('北京百炼 API Key（隐藏输入）：', stream=sys.stderr))
            print('Key 已保存到 data/key.txt；DASHSCOPE_API_KEY 环境变量仍优先。', file=sys.stderr)
            return 0
        if args.check_mt:
            from .mt_backend import key_fingerprint
            from .mt_pool import configured_route
            from .mt_pipeline import check_pool
            identity = {'region': 'beijing', 'workspace_id': validate_workspace_id(config.settings.workspace_id),
                        'access_mode': 'workspace', 'account': key_fingerprint(config.get_key())}
            route = configured_route(config, identity)
            print(json.dumps(check_pool(config, identity, route, cancel, magpie=magpie), ensure_ascii=False))
            return 0
        if args.report:
            protected_inputs = list(args.files)
            if args.export_archive and args.output_dir is not None:
                protected_inputs.append(args.output_dir / (args.export_archive + '.srt'))
            report_writer = ReportWriter(config, args.report, inputs=protected_inputs, output_dir=args.output_dir,
                                         job_id=args.resume or args.reflow or args.retime or args.review or args.export_archive)
            report_writer.validate()
        store = Store(config.data / "jobs.sqlite")
        progress = Progress()
        engine = Engine(config, store, cancel, progress, magpie=magpie)
        if args.export_archive:
            from .archives import export_archive
            item = export_archive(config, args.export_archive, args.output_dir, overwrite=args.overwrite)
            print(json.dumps(item, ensure_ascii=False))
            if report_writer:
                report_writer.write([item])
            return 0
        if args.jobs:
            report = [summary(store, j["id"]) for j in store.jobs()]
            for item in report:
                print(json.dumps(item, ensure_ascii=False))
            if args.report:
                report_writer.write(report)
            return 0
        if args.cancel:
            engine.cancel_job(args.cancel)
            item = summary(store, args.cancel)
            print(json.dumps(item, ensure_ascii=False))
            if args.report:
                report_writer.write([item])
            return 0

        engine.recover_jobs()

        def interrupt(signum, frame):
            if not processing:
                raise KeyboardInterrupt
            if engine.mt_runtime_active:
                engine.request_cancel(force=cancel.is_set())
            elif cancel.is_set():
                raise KeyboardInterrupt
            else:
                cancel.set()
            print("正在保存状态并停止本地处理；已提交的云端任务仍可能计费。", file=sys.stderr, flush=True)

        def run_job(job_id):
            nonlocal processing
            processing = True
            try:
                return engine.run(job_id)
            finally:
                processing = False

        if threading.current_thread() is threading.main_thread():
            previous_signal = signal.signal(signal.SIGINT, interrupt)
        selected = args.resume or args.review or args.reflow or args.retime
        ids = [selected] if selected else []
        if ids:
            current_job = ids[0]
            job = store.job(current_job)
            if args.workspace_id is not None and (job["manifest"].get("access_mode") != "workspace" or
                    job["manifest"].get("workspace_id") != config.settings.workspace_id):
                raise ToolError("恢复沿用任务原接入方式和工作空间；--workspace-id 不能迁移旧任务。", "cache_mismatch")
            if args.reflow:
                current_job = engine.reflow(current_job, overwrite=args.overwrite, output_dir=args.output_dir)
                ids = [current_job]
            elif args.retime:
                engine.retime(current_job, overwrite=args.overwrite, output_dir=args.output_dir)
            elif args.resume:
                engine.resume(current_job, track=args.track, channel=args.channel, output_dir=args.output_dir,
                              overwrite=args.overwrite, task_id=args.bind_task_id, allow_resubmit=args.allow_resubmit)
            else:
                engine.select_input(current_job, args.track, args.channel)
                if args.overwrite or args.output_dir:
                    store.change_output(current_job, args.output_dir, args.overwrite)
        else:
            workspace = validate_workspace_id(config.settings.workspace_id)
            key = config.get_key()
            account = hashlib.sha256(key.encode()).hexdigest()
            identity = {'region': 'beijing', 'workspace_id': workspace, 'access_mode': 'workspace', 'account': account}
            from .mt_pool import configured_route
            route = configured_route(config, identity)
            for source in args.files:
                job_id = store.enqueue(Path(source), overwrite=args.overwrite, output_dir=args.output_dir,
                                       workspace_id=workspace, account=account, settings=config.settings,
                                       mt_route_snapshot=route)
                engine.select_input(job_id, args.track, args.channel)
                if job_id not in ids:
                    ids.append(job_id)
        report = []
        for job_id in ids:
            current_job = job_id
            if cancel.is_set():
                break
            manifest = store.job(job_id)['manifest']
            if args.review:
                if store.job(job_id)["state"] != "needs_review":
                    raise ToolError("--review 需要待复核任务；其他状态请使用 --resume。", "review")
                state = "needs_review"
            else:
                if manifest.get('retime'):
                    print('仅校时：使用本地已保存的字幕和译文，无需调用云端。',
                          file=sys.stderr, flush=True)
                state = run_job(job_id)
            while state == "needs_review" and interactive and not cancel.is_set():
                if not review(config, store, engine, job_id):
                    break
                state = run_job(job_id)
            if state not in {"completed", "skipped"}:
                hint(store, job_id)
            item = summary(store, job_id)
            report.append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)
        if args.report:
            report_writer.write(report)
        return 130 if cancel.is_set() else (0 if all(i["state"] in {"completed", "skipped"} for i in report) else 2)
    except KeyboardInterrupt:
        cancel.set()
        if store and current_job:
            engine.cancel_job(current_job, interrupted=True)
            hint(store, current_job)
            item = summary(store, current_job)
            print(json.dumps(item, ensure_ascii=False))
            if report_writer:
                report_writer.write([item])
        return 130
    finally:
        try:
            magpie.close()
        finally:
            if previous_signal is not None:
                signal.signal(signal.SIGINT, previous_signal)
            if store:
                store.close()
            if lock is not None:
                lock.close()


def entry():
    try:
        result = main()
    except ToolError as error:
        print(str(error), file=sys.stderr)
        result = 2
    except KeyboardInterrupt:
        result = 130
    except Exception as error:
        # Ordinary logs must not echo secrets, transcripts or signed URLs from exception text.
        print("启动失败：" + type(error).__name__ + "。请检查本地环境和 data 设置。", file=sys.stderr)
        result = 3
    raise SystemExit(result)


if __name__ == "__main__":
    entry()
