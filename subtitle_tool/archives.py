"""Read and verify immutable local archives."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .common import ToolError
from .publication import write_srt
from .reports import ReportWriter


def read_archive(config, job_id):
    try:
        if not isinstance(job_id, str) or re.fullmatch(r'[0-9a-f]{32}', job_id) is None:
            raise ValueError()
        root = config.data / 'archives'
        folder = root / job_id
        if folder.resolve().parent != root.resolve() or folder.is_symlink():
            raise ValueError()
        index = json.loads((folder / 'index.json').read_bytes())
        if (not isinstance(index, dict) or set(index) != {'format_version', 'job_id', 'files'} or
                type(index['format_version']) is not int or index['format_version'] != 1 or
                index['job_id'] != job_id or not isinstance(index['files'], dict) or
                not {'task.json', 'subtitle.srt'} <= set(index['files'])):
            raise ValueError()
        files = {}
        for name, digest in index['files'].items():
            if (name not in {'task.json', 'subtitle.srt'} and
                    re.fullmatch(r'raw-[A-Za-z0-9_-]{1,80}\.json', name) is None):
                raise ValueError()
            path = folder / name
            if path.resolve().parent != folder.resolve() or path.is_symlink():
                raise ValueError()
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError()
            files[name] = content
        task = json.loads(files['task.json'])
        if task['job']['id'] != job_id:
            raise ValueError()
        manifest = task['job']['manifest']
        expected = {'task.json', 'subtitle.srt'}
        for segment in manifest['segments']:
            identity = segment['id']
            if not isinstance(identity, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identity) is None:
                raise ValueError()
            expected.add('raw-' + identity + '.json')
        if (set(files) != expected or
                hashlib.sha256(files['subtitle.srt']).hexdigest() != manifest['expected_srt_sha256']):
            raise ValueError()
        return task, files
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        raise ToolError('归档缺失、校验失败或结构不完整；已保留原资料。', 'archive_invalid') from None


def export_archive(config, job_id, directory, *, overwrite=False):
    if directory is None:
        raise ToolError('导出归档须用 --output-dir 指定目录。', 'output_path_required')
    _, files = read_archive(config, job_id)
    output = Path(directory).resolve() / (job_id + '.srt')
    ReportWriter(config, output).validate()
    output.parent.mkdir(parents=True, exist_ok=True)
    write_srt(output, files['subtitle.srt'], overwrite)
    return {'job_id': job_id, 'state': 'archived', 'output': str(output), 'cloud_calls': 0}
