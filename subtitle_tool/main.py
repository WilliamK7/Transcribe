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

from .common import ASR_MODEL, MT_MODEL, ToolError, atomic_json, validate_workspace_id
from .config import Config
from .engine import Engine
from .instance import ProcessLock
from .media import Media
from .store import STATES, TERMINAL, Store

MT_LANGUAGES = {"auto", "Chinese", "English", "Japanese", "German", "Korean", "Russian", "French",
                "Portuguese", "Arabic", "Italian", "Spanish", "Hindi", "Indonesian", "Thai", "Turkish",
                "Ukrainian", "Vietnamese", "Czech", "Danish", "Filipino", "Finnish", "Icelandic", "Malay",
                "Norwegian", "Polish", "Swedish"}


def parser():
    result = argparse.ArgumentParser(description="百炼命令行视频字幕工具（北京工作空间）")
    result.add_argument("files", nargs="*", help="本地视频／音频文件，逐个处理")
    actions = result.add_mutually_exclusive_group()
    actions.add_argument("--resume", metavar="JOB_ID", help="恢复本地任务")
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
            elif stage == "translating" and total is not None:
                message = f"翻译／已处理字幕 {done}/{total}"
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
        info = manifest.get("media")
        if not info:
            info = Media(config.root, engine.cancel).probe(Path(job["source"]))
            manifest["media"] = info
            store.update(job_id, manifest=manifest)
        for track in info["tracks"]:
            tags = track.get("tags", {})
            print(f"音轨 {track['index']}：{track.get('codec_name', '?')}，{track.get('channels', '?')} 声道，"
                  f"语言 {tags.get('language', '?')}，起始 {track.get('start_time', '?')} 秒", file=sys.stderr)
        selected = manifest.get("track")
        if job["error_code"] == "audio_track":
            while True:
                answer = ask("选择音轨索引（q 暂停）：")
                if answer.lower() == "q":
                    return False
                if answer.isascii() and answer.isdigit() and int(answer) in {t["index"] for t in info["tracks"]}:
                    selected = int(answer)
                    break
                print("请输入列表中的音轨索引。", file=sys.stderr)
        else:
            selected, _ = Media(config.root, engine.cancel).select(info, selected, "mix")
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
            store.cue_update(job_id, cue["cue_id"], decision="original" if choice == "o" else "translate",
                             source_lang=source_lang, reason="用户在终端确认", translated="", error="")
    store.update(job_id, "queued")
    return True


def summary(store, job_id):
    job = store.job(job_id)
    manifest = job["manifest"]
    item = {"job_id": job_id, "state": job["state"], "source": job["source"], "output": job["output"],
            "counts": store.counts(job_id), "error_code": job["error_code"], "request_id": job["request_id"],
            "region": manifest["region"], "workspace_id": manifest.get("workspace_id", ""),
            "access_mode": manifest.get("access_mode", "legacy_public"),
            "processing_version": manifest.get("processing_version", 1), "cue_count": len(store.cues(job_id)),
            "no_speech": manifest.get("no_speech", False), "output_sha256": manifest.get("expected_srt_sha256")}
    item["models"] = {"asr": ASR_MODEL, "mt": MT_MODEL}
    item["task_ids"] = [s["task_id"] for s in manifest["segments"] if s.get("task_id")]
    if manifest.get("media_plan"):
        item["audio"] = {k: manifest["media_plan"][k] for k in ["method", "codec", "container", "audio_offset_ms"]}
        item["audio"]["segments"] = len(manifest["segments"])
        item["audio"]["bytes"] = sum(s.get("bytes", 0) for s in manifest["segments"])
    return item


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
    elif job["state"] in {"failed", "cancelled", "queued"}:
        print("恢复命令：" + command, file=sys.stderr)


def main(argv=None):
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    action = args.resume or args.review or args.jobs or args.cancel or args.configure or args.configure_key or args.self_test
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
    config = Config()
    if args.workspace_id is not None:
        config.settings.workspace_id = validate_workspace_id(args.workspace_id)
    interactive = not args.non_interactive and sys.stdin is not None and sys.stdin.isatty()
    lock = ProcessLock(config.data)
    if not lock.acquire():
        raise ToolError("任务库已被其他 CLI 进程使用；请在该终端 Ctrl+C 停止后重试。", "instance_busy")
    store, current_job = None, None
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
        store = Store(config.data / "jobs.sqlite")
        store.recover()
        progress = Progress()
        engine = Engine(config, store, cancel, progress)
        if args.jobs:
            report = [summary(store, j["id"]) for j in store.jobs()]
            for item in report:
                print(json.dumps(item, ensure_ascii=False))
            if args.report:
                atomic_json(args.report, report)
            return 0
        if args.cancel:
            job = store.job(args.cancel)
            if job["state"] not in TERMINAL:
                store.update(args.cancel, "cancelled", error="用户停止本地任务；已提交的云端任务仍可能计费。", code="cancelled")
            item = summary(store, args.cancel)
            print(json.dumps(item, ensure_ascii=False))
            if args.report:
                atomic_json(args.report, [item])
            return 0

        def interrupt(signum, frame):
            if cancel.is_set() or not processing:
                raise KeyboardInterrupt
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
        ids = [args.resume or args.review] if args.resume or args.review else []
        if ids:
            current_job = ids[0]
            job = store.job(current_job)
            if args.workspace_id is not None and (job["manifest"].get("access_mode") != "workspace" or
                    job["manifest"].get("workspace_id") != config.settings.workspace_id):
                raise ToolError("恢复沿用任务原接入方式和工作空间；--workspace-id 不能迁移旧任务。", "cache_mismatch")
            engine.select_input(current_job, args.track, args.channel)
            if args.overwrite or args.output_dir:
                store.change_output(current_job, args.output_dir, args.overwrite)
            if args.resume and job["state"] == "cancelled":
                ambiguous = any(s.get("submitting") and not s.get("task_id") for s in job["manifest"]["segments"])
                store.update(current_job, "submission_unknown" if ambiguous else "queued")
            if args.bind_task_id or args.allow_resubmit:
                engine.resolve_unknown(current_job, args.bind_task_id, args.allow_resubmit)
        else:
            workspace = validate_workspace_id(config.settings.workspace_id)
            account = hashlib.sha256(config.get_key().encode()).hexdigest()
            for source in args.files:
                job_id = store.enqueue(Path(source), "beijing", args.overwrite, args.output_dir,
                                       workspace_id=workspace, account=account)
                engine.select_input(job_id, args.track, args.channel)
                if job_id not in ids:
                    ids.append(job_id)
        report = []
        for job_id in ids:
            current_job = job_id
            if cancel.is_set():
                break
            if args.review:
                if store.job(job_id)["state"] != "needs_review":
                    raise ToolError("--review 需要待复核任务；其他状态请使用 --resume。", "review")
                state = "needs_review"
            else:
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
            atomic_json(args.report, report)
        return 130 if cancel.is_set() else (0 if all(i["state"] in {"completed", "skipped"} for i in report) else 2)
    except KeyboardInterrupt:
        cancel.set()
        if store and current_job:
            job = store.job(current_job)
            ambiguous = any(s.get("submitting") and not s.get("task_id") for s in job["manifest"]["segments"])
            if job["state"] not in {"completed", "skipped"}:
                store.update(current_job, "submission_unknown" if ambiguous else "cancelled", code="submission_unknown" if ambiguous else "cancelled")
            hint(store, current_job)
            item = summary(store, current_job)
            print(json.dumps(item, ensure_ascii=False))
            if args.report:
                atomic_json(args.report, [item])
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
