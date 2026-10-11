"""Durable prompt intent. A single gateway process owns a journal."""
import json
import sqlite3
import threading
from pathlib import Path


class Journal:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS prompts (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
        self.db.commit()

    def all(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT record FROM prompts ORDER BY rowid')]

    def get(self, prompt_id):
        with self.lock:
            row = self.db.execute('SELECT record FROM prompts WHERE id=?', (prompt_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def save(self, record):
        with self.lock, self.db:
            self.db.execute('INSERT INTO prompts VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET record=excluded.record',
                            (record['id'], json.dumps(record)))

    def close(self):
        self.db.close()
