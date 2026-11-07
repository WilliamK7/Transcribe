"""Persist group boundaries and restore targets from verified sources."""
from __future__ import annotations

import hashlib
import json
from itertools import groupby

from .common import ToolError
from .mt_quality import plan_sentence_groups
from .subtitles import validate_unit_coverage


_CUE_INPUTS = ('cue_id', 'sentence_id', 'text', 'begin_ms', 'end_ms', 'char_start', 'char_end',
               'word_start', 'word_end', 'language', 'source_lang', 'decision', 'decision_origin', 'layout_round')


def make_unit(source, members, start, end):
    first = 0 if start == 0 else members[start - 1]['char_end']
    last = len(source['text']) if end == len(members) else members[end - 1]['char_end']
    return {'source': source, 'text': source['text'][first:last], 'cues': members[start:end]}


def _fingerprint(sources, cues, groups, layout_round):
    value = {'sources': sources, 'cues': [{key: cue.get(key) for key in _CUE_INPUTS} for cue in cues],
             'groups': groups, 'layout_round': layout_round}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def save_group_plan(groups, sources, cues, layout_round):
    boundaries = [[cue['cue_id'] for unit in group for cue in unit['cues']] for group in groups]
    return {'version': 1, 'layout_round': layout_round, 'groups': boundaries,
            'fingerprint': _fingerprint(sources, cues, boundaries, layout_round)}


def restore_group_plan(saved, sources, cues, layout_round):
    """Restore each group's saved input independently of current model availability."""
    try:
        if (not isinstance(saved, dict) or set(saved) != {'version', 'layout_round', 'groups', 'fingerprint'} or
                type(saved['version']) is not int or saved['version'] != 1 or
                type(saved['layout_round']) is not int or saved['layout_round'] != layout_round or
                not isinstance(saved['groups'], list) or not isinstance(saved['fingerprint'], str) or
                saved['fingerprint'] != _fingerprint(sources, cues, saved['groups'], layout_round)):
            raise ValueError()
        by_sentence = {}
        for cue in cues:
            by_sentence.setdefault(cue['sentence_id'], []).append(cue)
        expected, members, positions, blocks = [], {}, {}, {}
        for block, source_group in enumerate(plan_sentence_groups(sources)):
            for source in source_group:
                identity = source['sentence_id']
                local = sorted(by_sentence.get(identity, []), key=lambda cue: cue['char_start'])
                validate_unit_coverage(source, local)
                members[identity] = source, local
                for index, cue in enumerate(local):
                    expected.append(cue['cue_id'])
                    positions[cue['cue_id']] = identity, index
                    blocks[cue['cue_id']] = block
        groups = saved['groups']
        if (any(not isinstance(group, list) or not group or any(not isinstance(key, str) for key in group)
                for group in groups) or [key for group in groups for key in group] != expected):
            raise ValueError()
        restored = []
        for group in groups:
            if len({blocks[key] for key in group}) != 1:
                raise ValueError()
            units = []
            for identity, keys in groupby(group, key=lambda key: positions[key][0]):
                indices = [positions[key][1] for key in keys]
                source, local = members[identity]
                units.append(make_unit(source, local, indices[0], indices[-1] + 1))
            if units[-1]['cues'][-1]['end_ms'] - units[0]['cues'][0]['begin_ms'] > 60000:
                raise ValueError()
            restored.append(units)
        return restored
    except (KeyError, TypeError, ValueError, IndexError, OverflowError, RecursionError):
        raise ToolError('保存的翻译分组与当前来源或排版不一致；请用 --reflow 重建。', 'cache_mismatch') from None
