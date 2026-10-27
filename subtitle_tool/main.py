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

from .common import ASR_MODEL, DISPLAY_TIMING_VERSION, MT_MODEL, ToolError, validate_workspace_id
from .config import Config, Settings
from .engine import Engine
from .instance import ProcessLock
from .mt_backend import configured_mt
from .mt_fallback import fallback_policy, saved_fallback
from .policy import display_timing_version, load_policy
from .reports import ReportWriter
from .store import STATES, Store
from .review import MT_LANGUAGES, ReviewDecision


def parser():
    result = argparse.ArgumentParser(description="百炼命令行视频字幕工具（北京工作空间）")
    result.add_argument("files", nargs="*", help="本地视频／音频文件，逐个处理")
    actions = result.add_mutually_exclusive_group()
    actions.add_argument("--resume", metavar="JOB_ID", help="恢复本地任务")
    actions.add_argument("--reflow", metavar="JOB_ID", help="仅从完整本地转写重排字幕，覆盖须搭配 --overwrite")
    actions.add_argument("--retime", metavar="JOB_ID", help="复用最终字幕和译文，仅升级显示时间；覆盖须搭配 --overwrite")
    actions.add_argument("--review", metavar="JOB_ID", help="终端确认音轨、声道或逐条语种后继续")
    actions.add_argument("--jobs", action="store_true", help="列出任务（每行 JSON）")
    actions.add_argument("--cancel", metavar="JOB_ID", help="停止已保存的本地任务")
    actions.add_argument("--configure", action="store_true", help="配置 Workspace ID 和隐藏输入 Key")
    actions.add_argument("--configure-key", action="store_true", help="隐藏输入 Key，明文保存到 data/key.txt")
    actions.add_argument("--self-test", action="store_true", help="仅执行本机诊断，不访问云端")
    result.add_argument("--workspace-id", help="本次新任务使用的北京 Workspace ID（域名标签）")
    result.add_argument("--bind-task-id", help="给提交未知的任务绑定已知云端 ID，需 --resume")
    result.add_argument("--allow-resubmit", action="store_true", help="明确接受未知提交可能重复计费的重发，需 --resume")
    result.add_argument("--overwrite", action="store_true", help="先备份再覆盖现有字幕")
    result.add_argument("--track", type=int, help="ffprobe 返回的音轨索引")
    result.add_argument("--channel", choices=["mix", "left", "right"], help="明确混音／首声道／第二声道")
    result.add_argument("--output-dir", type=Path, help="显式选择输出目录，默认视频同目录")
    result.add_argument("--non-interactive", action="store_true", help="不询问；遇歧义持久化待复核状态并非零退出")
    result.add_argument('--mt-fallback', choices=['none', 'bailian'],
                        help='OpenRouter 的百炼后备；新任务及恢复默认启用，百炼可能产生费用')
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
            elif stage == "mt_service_wait":
                message = f"翻译服务暂不可用，共享等待 {done:.1f} 秒后先尝试一个请求"
            elif stage == 'mt_fallback':
                reasons = {'quota_exhausted': '服务端观测免费额度不足，本轮剩余组转百炼',
                           'quota_local_empty': '本地免费可用余量归零，本轮剩余组转百炼',
                           'format_exhausted': '译文结构或覆盖校验失败，当前原组转百炼',
                           'retry_after': '要求等待超过 300 秒，当前组转百炼'}
                failure = {'deadline': '仍超时', 'timeout': '上游仍超时', 'network': '网络连接仍失败',
                           'rate_limit_exceeded': '仍被限流', 'provider_overloaded': '上游仍过载',
                           'provider_unavailable': '上游仍不可用'}.get(done['category'], '临时服务故障仍未恢复')
                message = 'OpenRouter ' + reasons.get(done['reason'],
                    f"本请求已尝试 {done['attempt']}/4 次，{failure}；当前组转百炼") + '（可能产生费用）'
            elif stage == "mt_cooldown":
                message = f"翻译限流：共享冷却增加 {done:.2f} 秒，请求间隔 {total:.2f} 秒"
            elif stage == "mt_quota_wait":
                message = f"翻译配额等待：约 {done:.2f} 秒，滚动窗口最多 {total} 次请求"
            elif stage == "mt_recovery":
                message = (f"翻译提速：请求间隔 {done:.2f} → {total:.2f} 秒" if total else
                           f"翻译提速：请求间隔 {done:.2f} 秒，恢复无额外间隔")
            elif stage == 'mt_split':
                message = (f"批量译文校验失败：缩小翻译请求，{done} 片按 {done // 2}＋{done - done // 2} 片顺序重试；"
                           "字幕分段和时间保持不变")
            elif stage in {"translating", "reference_translating", "subtitle_translating"} and total is not None:
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
            engine.apply_review(job_id, cue["cue_id"], ReviewDecision("original" if choice == "o" else "translate", source_lang))
    engine.finish_review(job_id)
    return True


def summary(store, job_id):
    job = store.job(job_id)
    manifest = job["manifest"]
    item = {"job_id": job_id, "state": job["state"], "source": job["source"], "output": job["output"],
            "counts": store.counts(job_id), "error_code": job["error_code"], "request_id": job["request_id"],
            "region": manifest["region"], "workspace_id": manifest.get("workspace_id", ""),
            "access_mode": manifest.get("access_mode", "legacy_public"),
            "processing_version": manifest.get("processing_version", 1), "cue_count": len(store.cues(job_id)),
            "subtitle_version": manifest.get("subtitle_version", 0),
            "display_timing_version": manifest.get("display_timing_version", 0),
            "display_timing_adjustments": manifest.get("display_timing_adjustments", []),
            "translation_strategy_version": manifest.get("translation_strategy_version", 0),
            "layout_round": manifest.get("layout_round", 0), "warnings": manifest.get("warnings", []),
            "no_speech": manifest.get("no_speech", False), "output_sha256": manifest.get("expected_srt_sha256")}
    mt = manifest.get("mt_snapshot", {})
    item["models"] = {"asr": manifest.get("contract", {}).get("asr", ASR_MODEL), "mt": mt.get("model", MT_MODEL)}
    item["mt_provider"] = mt.get("provider", "bailian")
    if 'mt_fallback_policy' in manifest:
        fallback = saved_fallback(manifest)
        item['mt_fallback'] = {'enabled': manifest['mt_fallback_policy']['enabled'],
                               'provider': fallback.provider, 'model': fallback.model}
    item["task_ids"] = [s["task_id"] for s in manifest["segments"] if s.get("task_id")]
    counts = item["counts"]
    item["timings"] = {"run_seconds": counts.get("run_ms", 0) / 1000,
                       "mt_cooldown_seconds": counts.get("mt_cooldown_ms", 0) / 1000,
                       "mt_shared_wait_seconds": counts.get("mt_shared_wait_ms", 0) / 1000,
                       "mt_pacing_wait_seconds": counts.get("mt_pacing_wait_ms", 0) / 1000,
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
    return item


def can_retime(store, job):
    if job['state'] != 'failed' or job['error_code'] != 'timeline':
        return False
    try:
        manifest = job['manifest']
        if display_timing_version(manifest) >= DISPLAY_TIMING_VERSION:
            return False
        cues = store.cues(job['id'])
        Engine.validate_retime_ready(manifest, cues)
        settings = Engine.retime_settings(manifest, load_policy(manifest, verify_execution=False).settings(Settings()))
        Engine.render_subtitles(cues, settings, {**manifest, 'display_timing_version': DISPLAY_TIMING_VERSION})
        return True
    except (ToolError, KeyError, ValueError, TypeError):
        return False


def hint(store, job_id):
    job = store.job(job_id)
    if job["error"]:
        print(job["error"], file=sys.stderr)
    command = f".\\scripts\\transcribe.ps1 --resume {job_id}"
    if job["state"] == "needs_review":
        print(f"已保存待复核状态：.\\scripts\\transcribe.ps1 --review {job_id}\n"
              f"非交互恢复可明确指定：{command} --track 音轨索引 --channel left（按实际选择）", file=sys.stderr)
    elif job["state"] == "submission_unknown":
        print(f"绑定已知 ID：{command} --bind-task-id 云端任务ID\n"
              f"明确接受可能重复计费后重发：{command} --allow-resubmit", file=sys.stderr)
    elif can_retime(store, job):
        print(f'译文已完成，可用 --retime 仅校时：.\\scripts\\transcribe.ps1 --retime {job_id}', file=sys.stderr)
    elif job["state"] in {"failed", "cancelled", "queued"}:
        print("恢复命令：" + command, file=sys.stderr)


def main(argv=None):
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    action = args.resume or args.reflow or args.retime or args.review or args.jobs or args.cancel or args.configure or args.configure_key or args.self_test
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
    config = Config()
    if args.mt_fallback is not None and not (args.files or args.resume or args.reflow):
        raise ToolError('--mt-fallback 仅用于新任务、--resume 或 --reflow。', 'input')
    if args.workspace_id is not None:
        config.settings.workspace_id = validate_workspace_id(args.workspace_id)
    interactive = not args.non_interactive and sys.stdin is not None and sys.stdin.isatty()
    lock = ProcessLock(config.data)
    if not lock.acquire():
        raise ToolError("任务库已被其他 CLI 进程使用；请在该终端 Ctrl+C 停止后重试。", "instance_busy")
    store, current_job, report_writer = None, None, None
    cancel = threading.Event()
    previous_signal = None
    processing = False
    try:
        if args.configure or args.configure_key:
            if not interactive:
                raise ToolError("请在交互终端配置 Workspace ID／Key；非交互运行请预先配置或使用环境变量。", "credentials")
            if args.configure:
                workspace = args.workspace_id or ask("北京 Workspace ID（不能填完整 URL）：")
                config.settings.workspace_id = validate_workspace_id(workspace)
                config.settings.region = "beijing"
            key = getpass.getpass("北京百炼 API Key（隐藏输入）：", stream=sys.stderr)
            config.set_key(key)
            config.save()
            print("配置已保存；Key 明文位于 data/key.txt，环境变量 DASHSCOPE_API_KEY 仍优先。", file=sys.stderr)
            return 0
        if args.self_test:
            from .diagnostics import self_test
            result = self_test(config, args.report or config.data / "self-test.json")
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.report:
            report_writer = ReportWriter(config, args.report, inputs=args.files, output_dir=args.output_dir,
                                         job_id=args.resume or args.reflow or args.retime or args.review)
            report_writer.validate()
        store = Store(config.data / "jobs.sqlite")
        progress = Progress()
        engine = Engine(config, store, cancel, progress)
        engine.recover_jobs()
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
                engine.reflow(current_job, overwrite=args.overwrite, output_dir=args.output_dir, mt_fallback=args.mt_fallback)
            elif args.retime:
                engine.retime(current_job, overwrite=args.overwrite, output_dir=args.output_dir)
            elif args.resume:
                engine.resume(current_job, track=args.track, channel=args.channel, output_dir=args.output_dir,
                              overwrite=args.overwrite, task_id=args.bind_task_id, allow_resubmit=args.allow_resubmit,
                              mt_fallback=args.mt_fallback)
            else:
                engine.select_input(current_job, args.track, args.channel)
                if args.overwrite or args.output_dir:
                    engine.change_output(current_job, args.output_dir, args.overwrite)
        else:
            workspace = validate_workspace_id(config.settings.workspace_id)
            key = config.get_key()
            account = hashlib.sha256(key.encode()).hexdigest()
            mt = configured_mt(config, {"region": "beijing", "workspace_id": workspace, "access_mode": "workspace"}, asr_key=key)
            policy = fallback_policy({'region': 'beijing', 'workspace_id': workspace, 'access_mode': 'workspace',
                'account': account, 'translation_strategy_version': mt.translation_strategy_version,
                'mt_snapshot': mt.snapshot()}, args.mt_fallback)
            for source in args.files:
                job_id = store.enqueue(Path(source), "beijing", args.overwrite, args.output_dir,
                                       workspace_id=workspace, account=account, settings=config.settings, mt_snapshot=mt.snapshot(),
                                       mt_fallback_policy=policy)
                engine.configure_fallback(job_id, args.mt_fallback)
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
                elif manifest.get('mt_fallback_policy', {}).get('enabled'):
                    print(f'已启用按组百炼后备（{saved_fallback(manifest).model}），可能产生费用；可用 --mt-fallback none 关闭后续后备。',
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
        if previous_signal is not None:
            signal.signal(signal.SIGINT, previous_signal)
        if store:
            store.close()
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
