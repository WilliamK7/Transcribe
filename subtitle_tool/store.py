from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .common import DISPLAY_TIMING_VERSION, PROCESSING_VERSION, SUBTITLE_VERSION, ToolError, atomic_json, output_key, validate_workspace_id
from .config import Settings
from .review import ReviewDecision, decision_origin
from .policy import current_execution_snapshot
from .mt_pool import saved_route

TERMINAL = {"completed", "skipped", "cancelled", "archived"}
STATES = {
    "archived": "已归档",
    "queued": "排队", "probing": "检查媒体", "extracting": "提取音频", "uploading": "上传音频",
    "submitting": "提交识别", "recognizing": "识别中", "downloading_result": "下载结果",
    "segmenting": "整理字幕", "writing": "写入字幕", "completed": "完成",
    "skipped": "已跳过", "failed": "失败", "cancelled": "已停止", "submission_unknown": "提交结果未知",
    "needs_review": "需要确认",
    "subtitle_translating": "字幕翻译", "layout_adjusting": "排版调整",
}
_CUE_COLUMNS = ("cue_id", "begin_ms", "end_ms", "text", "language", "decision", "reason",
                "translated", "source_lang")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, output TEXT NOT NULL, output_key TEXT NOT NULL,
                state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '',
                request_id TEXT NOT NULL DEFAULT '', manifest TEXT NOT NULL DEFAULT '{}',
                created REAL NOT NULL, updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_output ON jobs(output_key);
            CREATE TABLE IF NOT EXISTS cues (
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, cue_id TEXT NOT NULL,
                begin_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, text TEXT NOT NULL,
                language TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
                translated TEXT NOT NULL DEFAULT '', source_lang TEXT NOT NULL DEFAULT 'auto',
                metadata TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(job_id, cue_id));
            CREATE TABLE IF NOT EXISTS mt_cache (key TEXT PRIMARY KEY, text TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mt_stages (
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, stage_key TEXT NOT NULL,
                stage TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(job_id,stage_key));
            CREATE TABLE IF NOT EXISTS subtitle_sources (
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, sentence_id TEXT NOT NULL,
                record TEXT NOT NULL, PRIMARY KEY(job_id,sentence_id));
            CREATE TABLE IF NOT EXISTS counters (
                job_id TEXT NOT NULL, name TEXT NOT NULL, value INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(job_id, name));
        """)
        self.db.commit()

    def enqueue(self, source: Path, region: str = "beijing", overwrite=False, output_dir: Path | None = None,
                *, workspace_id="", account="", settings=None, mt_route_snapshot=None):
        if region != "beijing":
            raise ToolError("新任务只支持北京工作空间。", "config")
        workspace_id = validate_workspace_id(workspace_id)
        source = source.resolve()
        if not source.is_file():
            raise ToolError("所选文件不存在或不是文件：" + str(source), "input")
        if source.suffix.lower() in {".srt", ".exe", ".lnk", ".json", ".sqlite"}:
            raise ToolError("请选择视频文件。", "input")
        output = ((output_dir if output_dir is not None else source.parent) / source.with_suffix(".srt").name).resolve()
        key = output_key(output)
        with self.lock, self.db:
            rows = self.db.execute("SELECT * FROM jobs WHERE output_key=? ORDER BY created DESC", (key,)).fetchall()
            reusable = None
            for row in rows:
                if (output_key(Path(row["source"])) != output_key(source) and
                        (row["state"] not in TERMINAL or output.exists())):
                    raise ToolError("同名不同扩展视频的目标 SRT 冲突，请调整文件名或明确选择输出目录。", "output_conflict")
                if row["state"] not in TERMINAL:
                    previous = json.loads(row["manifest"])
                    if (previous.get("access_mode"), previous.get("workspace_id"), previous.get("account", "")) != ("workspace", workspace_id, account):
                        raise ToolError("该文件已有其他账号或接入方式的任务，请用 --resume 原任务 ID；不能自动迁移工作空间。", "cache_mismatch")
                    if previous.get('mt_route_snapshot') != mt_route_snapshot:
                        raise ToolError('该文件已有不同翻译池配置的未完成任务；请用 --resume 或 --reflow。', 'mt_config_changed')
                    if mt_route_snapshot is not None:
                        saved_route(previous)
                    if reusable is None:
                        reusable = row
            if reusable is not None:
                manifest = json.loads(reusable["manifest"])
                if overwrite:
                    manifest["overwrite"] = True
                self._save_output(reusable["id"], output, manifest)
                return reusable["id"]
            job_id = uuid.uuid4().hex
            now = time.time()
            manifest = {"region": region, "workspace_id": workspace_id, "access_mode": "workspace",
                        "processing_version": PROCESSING_VERSION, "account": account,
                        "overwrite": bool(overwrite), "segments": [], "warnings": []}
            if mt_route_snapshot is not None:
                manifest['mt_route_snapshot'] = mt_route_snapshot
                saved_route({**manifest, 'translation_strategy_version': 5})
            settings = settings or Settings()
            manifest.update(subtitle_version=SUBTITLE_VERSION, translation_strategy_version=5,
                            display_timing_version=DISPLAY_TIMING_VERSION,
                            execution_snapshot=current_execution_snapshot(),
                            mt_glossary_schedule='prefix-v1',
                            subtitle_settings={k: getattr(settings, k) for k in
                                               ["zh_line_width", "other_line_width", "chinese_first"]})
            self.db.execute("INSERT INTO jobs(id,source,output,output_key,state,manifest,created,updated) "
                            "VALUES(?,?,?,?,?,?,?,?)", (job_id, str(source), str(output), key, "queued",
                            json.dumps(manifest, ensure_ascii=False), now, now))
            return job_id

    def job(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ToolError("任务不存在。", "job")
            result = dict(row)
            result["manifest"] = json.loads(result["manifest"])
            return result

    def change_output(self, job_id, directory=None, overwrite=False):
        with self.lock, self.db:
            job = self.job(job_id)
            if job["state"] in {"completed", "skipped", "archived"}:
                raise ToolError("已完成任务的输出不能通过恢复修改，请新建任务。", "job")
            output = self.resolve_output(job, directory)
            self._check_output_conflicts(job, output)
            manifest = job["manifest"]
            if overwrite:
                manifest["overwrite"] = True
            if output_key(output) != job["output_key"]:
                self._clear_publication(job_id, manifest)
            self._save_output(job_id, output, manifest)

    def _clear_publication(self, job_id, manifest=None):
        if manifest is not None:
            for name in ["publication_intent", "expected_srt_sha256", "display_timing_adjustments"]:
                manifest.pop(name, None)
        else:
            self.db.execute("UPDATE jobs SET manifest=json_remove(manifest,'$.publication_intent','$.expected_srt_sha256',"
                            "'$.display_timing_adjustments') WHERE id=?",
                            (job_id,))

    @staticmethod
    def resolve_output(job, directory=None):
        output = (Path(directory) / Path(job["source"]).with_suffix(".srt").name
                  if directory is not None else Path(job["output"]))
        resolved = output.resolve()
        if directory is None and (not output.is_absolute() or output_key(resolved) != job["output_key"]):
            raise ToolError("任务的输出路径无法确认；请用 --output-dir 明确指定目标目录。", "output_path_required")
        return resolved

    def _check_output_conflicts(self, job, output):
        for row in self.db.execute("SELECT state,source FROM jobs WHERE output_key=? AND id<>?",
                                   (output_key(output), job["id"])):
            if (row["state"] not in TERMINAL or
                    (output.exists() and output_key(Path(row["source"])) != output_key(Path(job["source"])))):
                raise ToolError("目标 SRT 与其他任务冲突。", "output_conflict")

    def _save_output(self, job_id, output, manifest):
        self.db.execute("UPDATE jobs SET output=?,output_key=?,manifest=?,updated=? WHERE id=?",
                        (str(output), output_key(output), json.dumps(manifest, ensure_ascii=False), time.time(), job_id))

    def ensure_output(self, job_id):
        """Resolve and check the saved output before any work starts."""
        with self.lock, self.db:
            job = self.job(job_id)
            output = self.resolve_output(job)
            self._check_output_conflicts(job, output)
            return output

    def jobs(self):
        with self.lock:
            return [dict(row, manifest=json.loads(row["manifest"]))
                    for row in self.db.execute("SELECT * FROM jobs ORDER BY created")]

    def update(self, job_id, state=None, manifest=None, error="", code="", request_id=""):
        with self.lock, self.db:
            if manifest is not None:
                self.db.execute("UPDATE jobs SET manifest=? WHERE id=?", (json.dumps(manifest, ensure_ascii=False), job_id))
            if state is not None:
                if state not in STATES:
                    raise ValueError(state)
                self.db.execute("UPDATE jobs SET state=?, error=?, error_code=?, request_id=?, updated=? WHERE id=?",
                                (state, error, code, request_id, time.time(), job_id))

    def replace_cues(self, job_id, cues):
        with self.lock, self.db:
            self._replace_records(job_id, cues)

    def _replace_records(self, job_id, records):
        columns = _CUE_COLUMNS
        defaults = {"translated": "", "source_lang": "auto"}
        rows = []
        for record in records:
            metadata = {k: v for k, v in record.items() if k not in (*columns, "job_id", "metadata")}
            if not record.get('translated'):
                metadata.pop('mt_origin', None)
                metadata.pop('mt_stage_key', None)
            rows.append((job_id, *(record.get(k, defaults.get(k)) for k in columns), json.dumps(metadata, ensure_ascii=False)))
        self.db.execute("DELETE FROM cues WHERE job_id=?", (job_id,))
        self.db.executemany("INSERT INTO cues(job_id," + ",".join(columns) + ",metadata) "
                            "VALUES(" + ",".join("?" for _ in range(len(columns) + 2)) + ")", rows)

    def initialize_subtitles(self, job_id, sources, cues, manifest):
        with self.lock, self.db:
            self._initialize_subtitles(job_id, sources, cues, manifest)

    def _initialize_subtitles(self, job_id, sources, cues, manifest):
        manifest.pop('mt_group_plan', None)
        self._clear_publication(job_id, manifest)
        self.db.execute("DELETE FROM subtitle_sources WHERE job_id=?", (job_id,))
        self.db.executemany("INSERT INTO subtitle_sources VALUES(?,?,?)",
                            [(job_id, s["sentence_id"], json.dumps(s, ensure_ascii=False)) for s in sources])
        self._replace_records(job_id, cues)
        self.db.execute("UPDATE jobs SET manifest=? WHERE id=?", (json.dumps(manifest, ensure_ascii=False), job_id))

    def check_republish_output(self, job_id, output, overwrite, *, operation):
        with self.lock:
            job = self.job(job_id)
            output = Path(output).resolve()
            self._check_output_conflicts(job, output)
            if output.exists() and not overwrite:
                raise ToolError(operation + "目标 SRT 已存在，必须明确搭配 --overwrite；原文件已保留。", "overwrite_required")

    def snapshot(self, job_id, path):
        """Private local task snapshot, including text; never use as an ordinary report."""
        with self.lock:
            atomic_json(path, {"job": self.job(job_id), "sources": self.sources(job_id),
                               "cues": self.cues(job_id),
                               "counts": self.counts(job_id), "mt_stages": self.mt_stages(job_id)})

    def commit_reflow(self, job_id, sources, cues, manifest, output):
        """Atomically replace local subtitle inputs and the explicit resume marker."""
        with self.lock, self.db:
            output = Path(output).resolve()
            self.check_republish_output(job_id, output, manifest["overwrite"], operation="重排")
            self.db.execute('DELETE FROM mt_stages WHERE job_id=?', (job_id,))
            self._initialize_subtitles(job_id, sources, cues, manifest)
            self.db.execute("UPDATE jobs SET output=?,output_key=?,state='queued',error='',error_code='',"
                            "request_id='',updated=? WHERE id=?", (str(output), output_key(output), time.time(), job_id))

    def commit_retime(self, job_id, manifest, output):
        """Commit display policy, adjustments and publication intent without replacing evidence."""
        with self.lock, self.db:
            output = Path(output).resolve()
            self.check_republish_output(job_id, output, manifest["overwrite"], operation="校时")
            self._save_output(job_id, output, manifest)
            self.db.execute("UPDATE jobs SET state='queued',error='',error_code='',request_id='' WHERE id=?", (job_id,))

    def save_layout(self, job_id, cues, manifest):
        """Commit range changes and their resume marker in one SQLite transaction."""
        with self.lock, self.db:
            manifest.pop('mt_group_plan', None)
            self._clear_publication(job_id, manifest)
            self._replace_records(job_id, cues)
            self.db.execute("UPDATE jobs SET manifest=? WHERE id=?", (json.dumps(manifest, ensure_ascii=False), job_id))

    def save_mt_group_plan(self, job_id, manifest):
        """Freeze planning before any stage request; cues and evidence stay intact."""
        with self.lock, self.db:
            self.db.execute('UPDATE jobs SET manifest=? WHERE id=?', (json.dumps(manifest, ensure_ascii=False), job_id))

    def has_mt_stages(self, job_id):
        with self.lock:
            return self.db.execute('SELECT 1 FROM mt_stages WHERE job_id=? LIMIT 1', (job_id,)).fetchone() is not None

    def sources(self, job_id):
        with self.lock:
            rows = [json.loads(r[0]) for r in self.db.execute("SELECT record FROM subtitle_sources WHERE job_id=?", (job_id,))]
            for record in rows:
                decision_origin(record)
            return sorted(rows, key=lambda s: (s["begin_ms"], s["end_ms"], s["sentence_id"]))

    def cues(self, job_id):
        with self.lock:
            result = []
            for row in self.db.execute("SELECT job_id," + ",".join(_CUE_COLUMNS) +
                                       ",metadata FROM cues WHERE job_id=? ORDER BY begin_ms,end_ms,cue_id", (job_id,)):
                values = dict(row)
                metadata = json.loads(values.pop("metadata"))
                record = {**metadata, **values}
                decision_origin(record)
                result.append(record)
            return result

    def apply_review(self, job_id, cue_id, decision: ReviewDecision):
        """Save one confirmation and every dependent local record in one transaction."""
        with self.lock, self.db:
            job = self.job(job_id)
            if job["state"] != "needs_review":
                raise ToolError("只有待复核任务可以保存语种决定。", "review")
            cues, sources = self.cues(job_id), self.sources(job_id)
            cue = next((c for c in cues if c["cue_id"] == cue_id), None)
            if cue is None:
                raise ToolError("待复核字幕记录不存在。", "cache_mismatch")
            source = next((s for s in sources if s["sentence_id"] == cue.get("sentence_id")), None)
            if source is None:
                raise ToolError("待复核字幕缺少完整句来源。", "cache_mismatch")
            source.update(decision.values())
            self.db.execute("UPDATE subtitle_sources SET record=? WHERE job_id=? AND sentence_id=?",
                            (json.dumps(source, ensure_ascii=False), job_id, source["sentence_id"]))
            for item in cues:
                if item.get("sentence_id") == source["sentence_id"]:
                    item.update(decision.values())
                item.update(translated="")
            self.db.execute('DELETE FROM mt_stages WHERE job_id=?', (job_id,))
            manifest = job["manifest"]
            for name in ["layout_planned", "layout_complete", "layout_round", "mt_group_plan",
                         "expected_srt_sha256", "publication_intent", "display_timing_adjustments", "retime",
                         "no_speech", "cue_count"]:
                manifest.pop(name, None)
            self._replace_records(job_id, cues)
            self.db.execute("UPDATE jobs SET manifest=?,updated=? WHERE id=?",
                            (json.dumps(manifest, ensure_ascii=False), time.time(), job_id))

    def mt_stages(self, job_id):
        with self.lock:
            return [{'key': row['stage_key'], 'stage': row['stage'], 'record': json.loads(row['record'])}
                    for row in self.db.execute('SELECT stage_key,stage,record FROM mt_stages WHERE job_id=? ORDER BY stage_key',
                                               (job_id,))]

    def mt_stage_entry(self, job_id, key):
        with self.lock:
            row = self.db.execute('SELECT stage,record FROM mt_stages WHERE job_id=? AND stage_key=?', (job_id, key)).fetchone()
            return {'stage': row['stage'], 'record': json.loads(row['record'])} if row is not None else None

    def commit_mt_stage(self, job_id, key, stage, record, *, translations=None, origin=None, counts=None,
                        cache_key=None, cache_value=None):
        """Persist a complete private stage and, when present, all cue updates in one transaction."""
        if (not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key) or
                stage not in {'glossary', 'glossary_unavailable', 'draft', 'align', 'review', 'correct', 'final'} or not isinstance(record, dict) or
                (cache_key is None) != (cache_value is None) or
                translations is not None and (not translations or
                    any(not isinstance(text, str) or not text.strip() for text in translations.values()))):
            raise ValueError('Invalid MT stage')
        with self.lock, self.db:
            if translations is not None:
                if origin is not None:
                    route = self.job(job_id)['manifest'].get('mt_route_snapshot', {})
                    entries = route.get('entries', []) if isinstance(route, dict) else []
                    if (not isinstance(origin, str) or not any(
                            isinstance(entry, dict) and entry.get('id') == origin for entry in entries)):
                        raise ValueError('Invalid translation origin')
                self._clear_publication(job_id)
                # JSON merge-patch replaces the stage link and removes a stale origin when null.
                metadata = json.dumps({'mt_origin': origin, 'mt_stage_key': key}, ensure_ascii=False)
                for cue_id, text in translations.items():
                    changed = self.db.execute("UPDATE cues SET translated=?,metadata=json_patch(metadata,?) "
                                              "WHERE job_id=? AND cue_id=?", (text, metadata, job_id, cue_id))
                    if changed.rowcount != 1:
                        raise ToolError('批量翻译的字幕记录不完整，结果未保存。', 'cache_mismatch')
            self.db.execute('INSERT OR REPLACE INTO mt_stages(job_id,stage_key,stage,record) VALUES(?,?,?,?)',
                            (job_id, key, stage, json.dumps(record, ensure_ascii=False)))
            if cache_key is not None:
                self.db.execute('INSERT OR REPLACE INTO mt_cache VALUES(?,?)', (cache_key, cache_value))
            for name, amount in (counts or {}).items():
                self._count(job_id, name, amount)

    def cache_get(self, key):
        with self.lock:
            row = self.db.execute("SELECT text FROM mt_cache WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def count(self, job_id, name, amount=1):
        with self.lock, self.db:
            self._count(job_id, name, amount)

    def _count(self, job_id, name, amount):
        self.db.execute("INSERT INTO counters VALUES(?,?,?) ON CONFLICT(job_id,name) DO UPDATE SET value=value+excluded.value",
                        (job_id, name, amount))

    def counts(self, job_id):
        with self.lock:
            return {r[0]: r[1] for r in self.db.execute("SELECT name,value FROM counters WHERE job_id=?", (job_id,))}

    def close(self):
        with self.lock:
            self.db.close()
