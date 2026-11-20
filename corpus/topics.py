"""Manage research topics without changing the library's configured defaults."""
import argparse
import json
import re

SYMBOLS = {'', 'triangle', 'circle', 'square', 'diamond', 'star', 'cross', 'hexagon', 'pentagon',
           'triangle-down', 'x', 'ring', 'hourglass', 'moon', 'heart', 'bolt', 'drop'}
# Okabe-Ito colors (minus orange and black), chosen to stay apart with color blindness; see static/app.css.
COLORS = {'', 'vermillion', 'yellow', 'green', 'sky', 'blue', 'purple'}


def list_topics(db):
    return [dict(row) for row in db.execute('SELECT id,label,description,relevance_criteria,symbol,color FROM fields ORDER BY rowid')]


def save_topic(db, incoming):
    if not isinstance(incoming, dict):
        raise ValueError('A topic must be an object')
    topic = {}
    old = db.execute('SELECT relevance_criteria,symbol,color FROM fields WHERE id=?', (incoming.get('id','') if isinstance(incoming.get('id',''),str) else '',)).fetchone()
    kept = ('relevance_criteria', 'symbol', 'color')
    for key in ('id', 'label', 'description', 'relevance_criteria', 'symbol', 'color'):
        value = incoming.get(key, old[key] if key in kept and old else '')
        if not isinstance(value, str):
            raise ValueError(key + ' must be text')
        topic[key] = value.strip()
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', topic['id']):
        raise ValueError('Topic ID must contain 1–80 letters, numbers, hyphens, or underscores')
    if not 1 <= len(topic['label']) <= 120:
        raise ValueError('Topic name must contain 1–120 characters')
    if len(topic['description']) > 20000:
        raise ValueError('Topic description must contain at most 20,000 characters')
    if len(topic['relevance_criteria']) > 20000:
        raise ValueError('Relevance criteria must contain at most 20,000 characters')
    if topic['symbol'] not in SYMBOLS:
        raise ValueError('Unknown project symbol')
    if topic['color'] not in COLORS:
        raise ValueError('Unknown topic color')
    db.execute(
        'INSERT INTO fields(id,label,description,relevance_criteria,symbol,color) VALUES(?,?,?,?,?,?) '
        'ON CONFLICT(id) DO UPDATE SET label=excluded.label,description=excluded.description,relevance_criteria=excluded.relevance_criteria,symbol=excluded.symbol,color=excluded.color',
        (topic['id'], topic['label'], topic['description'], topic['relevance_criteria'], topic['symbol'], topic['color']),
    )
    from .review import prepare_pending
    prepare_pending(db)
    return topic


def main():
    from .config import Settings
    from .db import connection, initialize

    parser = argparse.ArgumentParser(description='Add or update a research topic from a JSON file')
    parser.add_argument('file', type=argparse.FileType('r', encoding='utf-8'))
    args = parser.parse_args()
    with args.file:
        topic = json.load(args.file)
    settings = Settings()
    initialize(settings)
    with connection(settings) as db:
        saved = save_topic(db, topic)
    print('Saved topic: ' + saved['label'] + ' (' + saved['id'] + ')')


if __name__ == '__main__':
    main()
