from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from .common import ToolError, atomic_json, output_key


class ReportWriter:
    """Protect business files at preflight and again immediately before atomic report writes."""

    def __init__(self, config, path, *, inputs=(), output_dir=None, job_id=None, self_test=False):
        self.config, self.path = config, Path(path)
        self.inputs = [Path(p) for p in inputs]
        self.output_dir, self.job_id, self.self_test = output_dir, job_id, self_test

    @staticmethod
    def _absolute(path):
        return os.path.normcase(os.path.abspath(path)).casefold()

    def _job_paths(self):
        database = self.config.data / "jobs.sqlite"
        try:
            database.stat()
        except FileNotFoundError:
            return []
        # Self-test and preflight must not recover jobs, migrate schemas or read the Key.
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            return connection.execute("SELECT id,source,output,json_extract(manifest,'$.backup'),"
                                      "json_extract(manifest,'$.publication_intent.backup') FROM jobs").fetchall()

    def validate(self):
        try:
            self._validate()
        except (OSError, ValueError, RuntimeError, TypeError, KeyError, sqlite3.Error) as error:
            raise ToolError("报告目标与受保护文件冲突，或无法可靠核实路径；请选择其他位置。", "report_conflict") from error

    def _validate(self):
        if os.name == "nt" and ":" in str(self.path)[len(self.path.drive):]:
            # An ADS belongs to its base file even when the named stream does not exist yet.
            raise ValueError()
        target = self.path.resolve()
        target_key = output_key(target)
        try:
            target_stat = self.path.stat()
        except FileNotFoundError:
            target_stat = None
        if target_stat and not stat.S_ISREG(target_stat.st_mode):
            raise ValueError()
        default = self.config.data / "self-test.json"
        default_allowed = (self.self_test and self._absolute(self.path) == self._absolute(default) and
                           target_key == self._absolute(self.config.data.resolve() / "self-test.json"))

        def protect(path, *, directory=False):
            path = Path(path)
            resolved = path.resolve()
            key = output_key(resolved)
            if key == target_key or directory and target_key.startswith(key.rstrip("\\/") + os.sep):
                raise ValueError()
            if directory and self._absolute(self.path).startswith(self._absolute(path).rstrip("\\/") + os.sep):
                raise ValueError()
            # Includes Windows short names, hard links and other aliases of existing files.
            if target_stat:
                try:
                    if self.path.samefile(path):
                        raise ValueError()
                except FileNotFoundError:
                    pass

        protected = list(self.inputs)
        for source in self.inputs:
            protected.append((Path(self.output_dir) if self.output_dir is not None else source.parent) /
                             source.with_suffix(".srt").name)
        for job_id, source, output, backup, planned_backup in self._job_paths():
            protected.extend([Path(source), Path(output)])
            if backup:
                protected.append(Path(backup))
            if planned_backup:
                protected.append(Path(planned_backup))
            if job_id == self.job_id and self.output_dir is not None:
                protected.append(Path(self.output_dir) / Path(source).with_suffix(".srt").name)
        # Existing SRT backups remain protected even after manifest.backup advances.
        for output in list(protected):
            if output.suffix.lower() == ".srt":
                try:
                    protected.extend(p for p in output.parent.iterdir()
                                     if p.name.casefold().startswith(output.name.casefold() + ".bak."))
                except FileNotFoundError:
                    pass
        for path in protected:
            protect(path)

        for root in [self.config.data, self.config.root / ".build" / "reflow"]:
            if not (default_allowed and root == self.config.data):
                protect(root, directory=True)
            if target_stat is None or not root.exists():
                continue
            # Metadata only: detect a report outside private roots hard-linked to a private file.
            def walk_error(error):
                raise error
            for directory, dirs, files in os.walk(root, onerror=walk_error, followlinks=False):
                for name in dirs:
                    protect(Path(directory) / name, directory=True)
                for name in files:
                    path = Path(directory) / name
                    if default_allowed and self._absolute(path) == self._absolute(default):
                        continue
                    protect(path)

    def write(self, value):
        self.validate()
        atomic_json(self.path, value)
