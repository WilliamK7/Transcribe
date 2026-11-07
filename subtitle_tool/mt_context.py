"""Target ranges and source context; only optional evidence may be budget-trimmed."""
from __future__ import annotations

from .subtitles import source_context


def target_span(unit):
    return (unit.get('char_start', unit['cues'][0]['char_start']),
            unit.get('char_end', unit['cues'][-1]['char_end']))


def sentence_context(units, positions=None):
    """Keep word-aligned portions of split sources, nearest portion on each side required.

    Chunking is for evidence retention only, never for creating translation targets.
    Distant chunks grow geometrically to keep the number of budget-trimming steps
    logarithmic in source length. A long word stays intact even if it exceeds the chunk size.
    """
    ranked = []
    for number, unit in enumerate(units):
        source = unit['source']
        text, words = source['text'], source.get('words', [])
        start, end = target_span(unit)
        position = positions[source['sentence_id']] if positions is not None else number
        for side, lo, hi in [('before', 0, start), ('after', end, len(text))]:
            if not text[lo:hi].strip():
                continue
            # Use original word endpoints, preserving intervening whitespace.
            boundaries = [lo, *(word['char_end'] for word in words if lo < word['char_end'] < hi), hi]
            ranges, first, chunk_size = [], lo, 64
            if side == 'before':
                last = hi
                for boundary in reversed(boundaries[:-1]):
                    if last - boundary >= chunk_size or boundary == lo:
                        ranges.append((boundary, last))
                        last = boundary
                        chunk_size *= 2
            else:
                for boundary in boundaries[1:]:
                    if boundary - first >= chunk_size or boundary == hi:
                        ranges.append((first, boundary))
                        first = boundary
                        chunk_size *= 2
            for distance, (left, right) in enumerate(ranges):
                if text[left:right].strip():
                    ranked.append((distance, number, side, {
                        'kind': 'same_sentence', 'required': distance == 0,
                        'position': side, 'sentence_id': source['sentence_id'], 'source_position': position,
                        'char_start': left, 'char_end': right, 'text': text[left:right]}))
    return [item for _, _, _, item in sorted(ranked, key=lambda row: row[:3])]


def group_context(sources, units, positions):
    siblings = sentence_context(units, positions)
    neighbors = source_context(sources, positions[units[0]['source']['sentence_id']],
                               positions[units[-1]['source']['sentence_id']])
    return siblings + neighbors


def trim_context(context):
    """Return a new list with its least important optional entry removed, or None."""
    for index in range(len(context) - 1, -1, -1):
        item = context[index]
        if not isinstance(item, dict) or not item.get('required'):
            return context[:index] + context[index + 1:]
    return None


def valid_context_subset(original, chosen):
    if not isinstance(chosen, list):
        return False
    remaining = iter(original)
    return (all(any(item == candidate for candidate in remaining) for item in chosen) and
            all(not isinstance(item, dict) or not item.get('required') or item in chosen for item in original))
