"""Independent source, subject, lexical and semantic task recall with provenance."""
from concurrent.futures import ThreadPoolExecutor
import math
import re

from core.config import get_embedding_config
from core.llm_client import embed_text
from memory import workflow_state as store
from memory.workflow import stable_id


def normalized(value):
    return re.sub(r'\s+', '', str(value or '')).casefold().strip('，。！？!?')


def lexical(left, right):
    a, b = normalized(left), normalized(right)
    if a == b:
        return 1.0
    aa = {a[i:i + 2] for i in range(len(a) - 1)}
    bb = {b[i:i + 2] for i in range(len(b) - 1)}
    return len(aa & bb) / max(1, len(aa | bb))


def instance_conflict(left, right):
    """Only explicit disjoint dates/numbered rounds are hard conflicts, not wording."""
    def anchors(value):
        text = normalized(value)
        dates = set(re.findall(r'\d{4}-\d{2}-\d{2}', text))
        rounds = set(re.findall(r'第\d+(?:周|轮|次|期)', text))
        return dates, rounds
    a, b = anchors(left), anchors(right)
    return any(x and y and x.isdisjoint(y) for x, y in zip(a, b))


def identity_text(card):
    return ' | '.join(str(card.get(k) or '') for k in ('subject', 'task', 'instance'))


def cosine(a, b):
    if len(a) != len(b) or not a or not all(math.isfinite(x) for x in [*a, *b]):
        raise ValueError('invalid embedding shape or values')
    norm = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b)) / norm if norm else 0.0


def vector_rank(space_id, value, cards):
    config = get_embedding_config()
    contract = stable_id(config.base_url, config.model, config.dimension, 'task-identity-v1')
    # Current extraction resolves the task from preceding messages. Progress does not pollute the identity vector.
    query = embed_text(identity_text(value.model_dump()))
    cached = {item['id']: item for item in store.list_items(space_id, 'task_identity_vector')}
    def score(item):
        record, card = item
        text = identity_text(card)
        fingerprint = stable_id(contract, text)
        old = cached.get(record.id)
        if old and old['fingerprint'] == fingerprint:
            vector = old['embedding']
        else:
            vector = embed_text(text)
            cosine(query, vector)
            store.put(space_id, 'task_identity_vector', record.id,
                      {'id': record.id, 'fingerprint': fingerprint, 'embedding': vector})
        return record.id, cosine(query, vector)
    with ThreadPoolExecutor(max_workers=4) as pool:
        return sorted(pool.map(score, cards), key=lambda pair: pair[1], reverse=True)


def recall(space_id, value, cards, *, limit=30):
    if not cards:
        return [], {'channels': {}, 'vector_status': 'empty', 'candidates': []}
    subject = [(r.id, lexical(value.task, c['task'])) for r, c in cards if normalized(c['subject']) == normalized(value.subject)]
    task = [(r.id, lexical(value.task, c['task'])) for r, c in cards]
    source = [(r.id, 1.0) for r, c in cards if value.antecedent_note_id and
              any(s['note_id'] == value.antecedent_note_id for s in c['sources'])]
    channels = {'source': source, 'subject': sorted(subject, key=lambda p:p[1], reverse=True)[:30],
                'task': sorted((p for p in task if p[1] > 0), key=lambda p:p[1], reverse=True)[:30]}
    vector_status = 'ready'
    try:
        channels['vector'] = vector_rank(space_id, value, cards)[:30]
    except (RuntimeError, ValueError, OSError) as exc:
        # Explicit degradation, never silently claim vector recall succeeded.
        channels['vector'] = []
        vector_status = 'unavailable:' + type(exc).__name__
    scores, reasons = {}, {}
    for channel, hits in channels.items():
        for rank, (key, score) in enumerate(hits, 1):
            scores[key] = scores.get(key, 0) + (3 if channel == 'source' else 1) / (60 + rank)
            reasons.setdefault(key, {})[channel] = score
    ranked = sorted(scores, key=lambda k: ('source' in reasons[k], scores[k]), reverse=True)
    # Preserve all explicit references even when one message created several tasks.
    selected = list(dict.fromkeys([key for key, _ in source] + ranked[:limit]))
    by_id = {r.id: r for r, _ in cards}
    audit = {'channels': channels, 'vector_status': vector_status,
             'candidates': [{'id': key, 'rrf': scores[key], 'signals': reasons[key]} for key in selected]}
    return [by_id[key] for key in selected], audit
