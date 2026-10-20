from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .common import (PROCESSING_VERSION, SUBTITLE_VERSION, TRANSLATION_STRATEGY_VERSION,
                     ToolError, atomic_json, output_key, validate_workspace_id)
from .config import Settings
from .review import ReviewDecision, decision_origin
from .policy import current_execution_snapshot

TERMINAL = {"completed", "skipped", "cancelled"}
STATES = {
    "queued": "排队", "probing": "检查媒体", "extracting": "提取音频", "uploading": "上传音频",
    "submitting": "提交识别", "recognizing": "识别中", "downloading_result": "下载结果",
    "segmenting": "整理字幕", "translating": "翻译", "writing": "写入字幕", "completed": "完成",
    "skipped": "已跳过", "failed": "失败", "cancelled": "已停止", "submission_unknown": "提交结果未知",
    "needs_review": "需要确认",
    "reference_translating": "参考翻译", "subtitle_translating": "字幕翻译", "layout_adjusting": "排版调整",
}


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
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(job_id, cue_id));
            CREATE TABLE IF NOT EXISTS mt_cache (key TEXT PRIMARY KEY, text TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS subtitle_sources (
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, sentence_id TEXT NOT NULL,
                record TEXT NOT NULL, PRIMARY KEY(job_id,sentence_id));
            CREATE TABLE IF NOT EXISTS mt_references (
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, cue_id TEXT NOT NULL,
                begin_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, text TEXT NOT NULL,
                language TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
                translated TEXT NOT NULL DEFAULT '', source_lang TEXT NOT NULL DEFAULT 'auto',
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(job_id,cue_id));
            CREATE TABLE IF NOT EXISTS counters (
                job_id TEXT NOT NULL, name TEXT NOT NULL, value INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(job_id, name));
        """)
        if "metadata" not in {r[1] for r in self.db.execute("PRAGMA table_info(cues)")}:
            self.db.execute("ALTER TABLE cues ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
        self.db.commit()

    def enqueue(self, source: Path, region: str = "beijing", overwrite=False, output_dir: Path | None = None,
                *, workspace_id="", account="", settings=None):
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
            settings = settings or Settings()
            manifest.update(subtitle_version=SUBTITLE_VERSION, translation_strategy_version=TRANSLATION_STRATEGY_VERSION,
                            execution_snapshot=current_execution_snapshot(),
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
            if job["state"] in {"completed", "skipped"}:
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
            for name in ["publication_intent", "expected_srt_sha256"]:
                manifest.pop(name, None)
        else:
            self.db.execute("UPDATE jobs SET manifest=json_remove(manifest,'$.publication_intent','$.expected_srt_sha256') WHERE id=?",
                            (job_id,))

    @staticmethod
    def resolve_output(job, directory=None):
        output = (Path(directory) / Path(job["source"]).with_suffix(".srt").name
                  if directory is not None else Path(job["output"]))
        resolved = output.resolve()
        if directory is None and not output.is_absolute() and output_key(resolved) != job["output_key"]:
            raise ToolError("旧任务的相对输出路径无法确认；请用 --output-dir 明确指定目标目录。", "output_path_required")
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
        """Fix an unambiguous legacy path only when preparing this task to run."""
        with self.lock, self.db:
            job = self.job(job_id)
            output = self.resolve_output(job)
            self._check_output_conflicts(job, output)
            if str(output) != job["output"] or output_key(output) != job["output_key"]:
                self._save_output(job_id, output, job["manifest"])
            return output

    def jobs(self):
        with self.lock:
            return [self.job(r[0]) for r in self.db.execute("SELECT id FROM jobs ORDER BY created")]

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
            self._replace_records("cues", job_id, cues)

    def _replace_records(self, table, job_id, records):
        columns = ["cue_id", "begin_ms", "end_ms", "text", "language", "decision", "reason",
                   "translated", "source_lang", "attempts", "error"]
        defaults = {"translated": "", "source_lang": "auto", "attempts": 0, "error": ""}
        rows = []
        for record in records:
            metadata = {k: v for k, v in record.items() if k not in columns + ["job_id", "metadata"]}
            rows.append((job_id, *(record.get(k, defaults.get(k)) for k in columns), json.dumps(metadata, ensure_ascii=False)))
        self.db.execute("DELETE FROM " + table + " WHERE job_id=?", (job_id,))
        self.db.executemany("INSERT INTO " + table + "(job_id," + ",".join(columns) + ",metadata) "
                            "VALUES(" + ",".join("?" for _ in range(len(columns) + 2)) + ")", rows)

    def initialize_subtitles(self, job_id, sources, cues, manifest):
        with self.lock, self.db:
            self._initialize_subtitles(job_id, sources, cues, manifest)

    def _initialize_subtitles(self, job_id, sources, cues, manifest):
        self._clear_publication(job_id, manifest)
        self.db.execute("DELETE FROM subtitle_sources WHERE job_id=?", (job_id,))
        self.db.executemany("INSERT INTO subtitle_sources VALUES(?,?,?)",
                            [(job_id, s["sentence_id"], json.dumps(s, ensure_ascii=False)) for s in sources])
        self.db.execute("DELETE FROM mt_references WHERE job_id=?", (job_id,))
        self._replace_records("cues", job_id, cues)
        self.db.execute("UPDATE jobs SET manifest=? WHERE id=?", (json.dumps(manifest, ensure_ascii=False), job_id))

    def check_reflow_output(self, job_id, output, overwrite):
        with self.lock:
            job = self.job(job_id)
            output = Path(output).resolve()
            self._check_output_conflicts(job, output)
            if output.exists() and not overwrite:
                raise ToolError("重排目标 SRT 已存在，必须明确搭配 --overwrite；原文件已保留。", "overwrite_required")

    def snapshot(self, job_id, path):
        """Private local task snapshot, including text; never use as an ordinary report."""
        with self.lock:
            atomic_json(path, {"job": self.job(job_id), "sources": self.sources(job_id),
                               "cues": self.cues(job_id), "references": self.references(job_id),
                               "counts": self.counts(job_id)})

    def commit_reflow(self, job_id, sources, cues, manifest, output):
        """Atomically replace local subtitle inputs and the explicit resume marker."""
        with self.lock, self.db:
            output = Path(output).resolve()
            self.check_reflow_output(job_id, output, manifest["overwrite"])
            self._initialize_subtitles(job_id, sources, cues, manifest)
            self.db.execute("UPDATE jobs SET output=?,output_key=?,state='queued',error='',error_code='',"
                            "request_id='',updated=? WHERE id=?", (str(output), output_key(output), time.time(), job_id))

    def save_layout(self, job_id, cues, manifest, *, references=None):
        """Commit range changes and their resume marker in one SQLite transaction."""
        with self.lock, self.db:
            self._clear_publication(job_id, manifest)
            self._replace_records("cues", job_id, cues)
            if references is not None:
                self._replace_records("mt_references", job_id, references)
            self.db.execute("UPDATE jobs SET manifest=? WHERE id=?", (json.dumps(manifest, ensure_ascii=False), job_id))

    def sources(self, job_id):
        with self.lock:
            rows = [json.loads(r[0]) for r in self.db.execute("SELECT record FROM subtitle_sources WHERE job_id=?", (job_id,))]
            for record in rows:
                decision_origin(record)
            return sorted(rows, key=lambda s: (s["begin_ms"], s["end_ms"], s["sentence_id"]))

    def _records(self, table, job_id):
        result = []
        for row in self.db.execute("SELECT * FROM " + table + " WHERE job_id=? ORDER BY begin_ms,end_ms,cue_id", (job_id,)):
            values = dict(row)
            metadata = json.loads(values.pop("metadata"))
            record = {**metadata, **values}
            decision_origin(record)
            result.append(record)
        return result

    def cues(self, job_id):
        with self.lock:
            return self._records("cues", job_id)

    def references(self, job_id):
        with self.lock:
            return self._records("mt_references", job_id)

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
            if sources:
                source = next((s for s in sources if s["sentence_id"] == cue.get("sentence_id")), None)
                if source is None:
                    raise ToolError("待复核字幕缺少完整句来源。", "cache_mismatch")
                source.update(decision.values())
                self.db.execute("UPDATE subtitle_sources SET record=? WHERE job_id=? AND sentence_id=?",
                                (json.dumps(source, ensure_ascii=False), job_id, source["sentence_id"]))
                for item in cues:
                    if item.get("sentence_id") == source["sentence_id"]:
                        item.update(decision.values())
                    # Neighbor context can change as well as the reviewed sentence.
                    item.update(translated="", error="")
                self.db.execute("DELETE FROM mt_references WHERE job_id=?", (job_id,))
            else:
                # Old tasks without sources retain their per-cue review behavior.
                cue.update(decision.values(), translated="", error="")
            manifest = job["manifest"]
            for name in ["references_planned", "layout_planned", "layout_complete", "layout_round",
                         "expected_srt_sha256", "publication_intent", "no_speech", "cue_count", "output_settings"]:
                manifest.pop(name, None)
            self._replace_records("cues", job_id, cues)
            self.db.execute("UPDATE jobs SET manifest=?,updated=? WHERE id=?",
                            (json.dumps(manifest, ensure_ascii=False), time.time(), job_id))

    def translation_update(self, job_id, cue_ids, *, reference_stage=False, attempt=False,
                           translated=None, error="", cache_key=None, counts=None):
        """Persist one translation group's records, cache and counters together."""
        if cache_key is not None and translated is None:
            raise ValueError("Translation cache requires text")
        table = "mt_references" if reference_stage else "cues"
        assignments, values = ["error=?"], [error]
        if attempt:
            assignments.append("attempts=attempts+1")
        if translated is not None:
            assignments.append("translated=?")
            values.append(translated)
        with self.lock, self.db:
            if translated is not None:
                self._clear_publication(job_id)
            self.db.executemany("UPDATE " + table + " SET " + ",".join(assignments) + " WHERE job_id=? AND cue_id=?",
                                [(*values, job_id, cue_id) for cue_id in cue_ids])
            if cache_key is not None:
                self.db.execute("INSERT OR REPLACE INTO mt_cache VALUES(?,?)", (cache_key, translated))
            for name, amount in (counts or {}).items():
                self._count(job_id, name, amount)

    def translation_batch_update(self, job_id, translations, *, reference_stage=False,
                                 cache_key=None, cache_value=None, counts=None):
        """Commit distinct cue translations, the complete batch cache and counters atomically."""
        if (not translations or any(not isinstance(text, str) or not text.strip() for text in translations.values()) or
                (cache_key is None) != (cache_value is None)):
            raise ValueError("Invalid translation batch")
        table = "mt_references" if reference_stage else "cues"
        with self.lock, self.db:
            self._clear_publication(job_id)
            for cue_id, text in translations.items():
                changed = self.db.execute("UPDATE " + table + " SET translated=?,error='' WHERE job_id=? AND cue_id=?",
                                          (text, job_id, cue_id))
                if changed.rowcount != 1:
                    raise ToolError("批量翻译的字幕记录不完整，结果未保存。", "cache_mismatch")
            if cache_key is not None:
                self.db.execute("INSERT OR REPLACE INTO mt_cache VALUES(?,?)", (cache_key, cache_value))
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
