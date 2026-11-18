"""Local encrypted call clips and a keyed index of recognized speech."""
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import logging
from pathlib import Path
import re
import secrets
import sqlite3
import time
import unicodedata
import zlib

import numpy as np
from trace_engine.speech_activity import RATE

from trace_engine.opus import TAG as _OPUS_TAG, bundled_opus
from trace_engine.protection import protect_for_current_user, unprotect_for_current_user


logger = logging.getLogger(__name__)
MAX_SAMPLES = 13 * RATE
MAX_ARCHIVE_BYTES = 512 * 1024**2
_PCM_PLANAR = b'TCA1'
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS trace_call_clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    audio BLOB NOT NULL,
    size INTEGER NOT NULL,
    transcript BLOB,
    speaker TEXT NOT NULL,
    retry_after REAL NOT NULL DEFAULT 0,
    UNIQUE (session_id, source, sequence)
);
CREATE INDEX IF NOT EXISTS trace_call_clip_range ON trace_call_clips(start, end);
CREATE INDEX IF NOT EXISTS trace_call_pending ON trace_call_clips(start) WHERE transcript IS NULL;
CREATE INDEX IF NOT EXISTS trace_call_sizes ON trace_call_clips(size);
CREATE TABLE IF NOT EXISTS trace_call_terms (
    term BLOB NOT NULL,
    clip INTEGER NOT NULL REFERENCES trace_call_clips(id) ON DELETE CASCADE,
    PRIMARY KEY(term, clip)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS trace_call_terms_clip ON trace_call_terms(clip);
CREATE TABLE IF NOT EXISTS trace_call_key (id INTEGER PRIMARY KEY CHECK(id=1), value BLOB NOT NULL);
CREATE TRIGGER IF NOT EXISTS trace_call_session_deleted AFTER DELETE ON meeting_sessions BEGIN
    DELETE FROM trace_call_terms WHERE clip IN (SELECT id FROM trace_call_clips WHERE session_id=old.id);
    DELETE FROM trace_call_clips WHERE session_id=old.id;
END;
'''


def encode_pcm(pcm):
    # Contiguous high bytes compress well without dropping PCM16 information.
    return _PCM_PLANAR + zlib.compress(pcm.view('u1').reshape(-1, 2).T.tobytes(), level=1)


def decode_pcm(encoded):
    planar = encoded.startswith(_PCM_PLANAR)
    decoder = zlib.decompressobj()
    pcm = decoder.decompress(encoded[len(_PCM_PLANAR):] if planar else encoded, MAX_SAMPLES * 2 + 1)
    if not decoder.eof or len(pcm) > MAX_SAMPLES * 2 or len(pcm) % 2:
        raise ValueError('Invalid TRACE call clip')
    if planar:
        pcm = np.frombuffer(pcm, 'u1').reshape(2, -1).T.tobytes()
    return np.frombuffer(pcm, '<i2')


def _words(text):
    # Text search over the explicit tool query and stored ASR output only.
    normalized = unicodedata.normalize('NFKD', text.casefold())
    normalized = ''.join(char for char in normalized if not unicodedata.combining(char))
    words = re.findall(r'[\u3040-\u30ff\u3400-\u9fff]+|[^\W_\u3040-\u30ff\u3400-\u9fff]+', normalized)
    terms = set()
    for word in words:
        if '\u3040' <= word[0] <= '\u9fff' and re.fullmatch(r'[\u3040-\u30ff\u3400-\u9fff]+', word):
            terms.update(word)
            terms.update(word[index:index + 2] for index in range(len(word) - 1))
        else:
            terms.add(word)
    return terms


def _identity(app):
    return app.strip().casefold().removesuffix('.exe')


@dataclass(frozen=True)
class CallClip:
    id: int
    session_id: int
    source: str
    start: float
    end: float
    speaker: str
    app: str
    channel: str
    indexed: bool


class CallArchive:
    """Encrypted audio, cached transcripts and keyed search on a TimelineDB path."""

    def __init__(self, db_path, *, capacity=MAX_ARCHIVE_BYTES, seal=protect_for_current_user,
                 decrypt=unprotect_for_current_user, opus=bundled_opus):
        self.path = Path(db_path).absolute()
        self.capacity = capacity
        self.seal, self.decrypt = seal, decrypt
        self._opus = opus

    @contextmanager
    def _db(self, *, write=False):
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=.5)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            with db:
                if write:
                    db.execute('BEGIN IMMEDIATE')
                yield db

    def initialize(self):
        with self._db() as db:
            db.executescript(_SCHEMA)

    @staticmethod
    def _exists(db):
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='trace_call_clips'").fetchone())

    def record(self, segment, *, excluded=lambda app: False):
        samples = segment.audio
        if not 0 < len(samples) <= MAX_SAMPLES or segment.source not in ('audio_in', 'audio_out'):
            return False
        pcm = (np.clip(samples, -1, 1) * 32767).astype('<i2')
        codec = self._opus()
        try:
            encoded = codec.encode(pcm) if codec else encode_pcm(pcm)
        except ValueError:
            logger.warning('Opus could not encode a TRACE clip; preserving it as compressed PCM', exc_info=True)
            encoded = encode_pcm(pcm)
        audio = self.seal(encoded)
        if len(audio) > self.capacity:
            return False
        with self._db(write=True) as db:
            session = db.execute('SELECT started_at,ended_at,app_name FROM meeting_sessions WHERE id=?',
                                 (segment.session_id,)).fetchone()
            if (not session or excluded(session[2]) or not session[0] - .25 <= segment.started_at < segment.ended_at
                    or session[1] is not None and segment.ended_at > session[1] + .25):
                return False
            if db.execute('SELECT 1 FROM trace_call_clips WHERE session_id=? AND source=? AND sequence=?',
                          (segment.session_id, segment.source, segment.sequence)).fetchone():
                return True
            size = db.execute('SELECT COALESCE(SUM(size),0) FROM trace_call_clips').fetchone()[0]
            if size + len(audio) > self.capacity:
                for clip, length in db.execute('SELECT id,size FROM trace_call_clips ORDER BY start,id').fetchall():
                    db.execute('DELETE FROM trace_call_clips WHERE id=?', (clip,))
                    size -= length
                    if size + len(audio) <= self.capacity:
                        break
            db.execute('INSERT OR IGNORE INTO trace_call_clips(session_id,source,sequence,start,end,audio,size,speaker) '
                       'VALUES(?,?,?,?,?,?,?,?)', (segment.session_id, segment.source, segment.sequence,
                       segment.started_at, segment.ended_at, audio, len(audio), segment.speaker_id))
        return True

    def prune(self, cutoff):
        if self.path.is_file():
            with self._db(write=True) as db:
                if self._exists(db):
                    db.execute('DELETE FROM trace_call_clips WHERE end<?', (cutoff,))

    def read_audio(self, clip):
        with self._db() as db:
            row = db.execute('SELECT audio FROM trace_call_clips WHERE id=?', (clip.id,)).fetchone()
        if not row:
            return None
        encoded = self.decrypt(row[0])
        if encoded.startswith(_OPUS_TAG):
            codec = self._opus() or bundled_opus()
            if codec is None:
                raise ValueError('Cannot decode a TRACE Opus clip without the codec')
            pcm = codec.decode(encoded)
        else:
            pcm = decode_pcm(encoded)
        # NativeASR rounds back to PCM16; a small offset preserves the integer.
        audio = pcm.astype(np.float32)
        return (audio + np.sign(audio) * .25) / 32767

    def transcript(self, clip):
        with self._db() as db:
            row = db.execute('SELECT transcript FROM trace_call_clips WHERE id=?', (clip.id,)).fetchone()
        return self.decrypt(row[0]).decode('utf-8') if row and row[0] is not None else None

    def _tokens(self, db, text, *, create=False):
        row = db.execute('SELECT value FROM trace_call_key WHERE id=1').fetchone()
        if row is None:
            if not create:
                return ()
            key = secrets.token_bytes(32)
            db.execute('INSERT INTO trace_call_key VALUES(1,?)', (self.seal(key),))
        else:
            key = self.decrypt(row[0])
        return tuple(hmac.digest(key, word.encode('utf-8'), hashlib.sha256)[:16] for word in _words(text))

    def save_transcript(self, clip, text):
        text = text.strip()[:4000]
        with self._db(write=True) as db:
            inserted = db.execute('UPDATE trace_call_clips SET transcript=? WHERE id=? AND transcript IS NULL',
                                  (self.seal(text.encode('utf-8')), clip.id)).rowcount
            if inserted and text:
                db.executemany('INSERT INTO trace_call_terms(term,clip) VALUES(?,?)',
                               ((term, clip.id) for term in self._tokens(db, text, create=True)))
        return bool(inserted)

    def defer_retry(self, clip, seconds):
        with self._db(write=True) as db:
            db.execute('UPDATE trace_call_clips SET retry_after=? WHERE id=? AND transcript IS NULL',
                       (time.time() + seconds, clip.id))

    def select(self, start, end, *, apps=(), excluded=(), pending=False, indexed=False, oldest=False,
               topic='', limit=8, point=None, sources=('audio_in', 'audio_out')):
        if not self.path.is_file():
            return [], 0
        with self._db() as db:
            if not self._exists(db):
                return [], 0
            identity = "CASE WHEN lower(s.app_name) LIKE '%.exe' THEN substr(lower(s.app_name),1,length(s.app_name)-4) ELSE lower(s.app_name) END"
            where = ['c.end>?', 'c.start<?', 'c.source IN (' + ','.join('?' for _ in sources) + ')']
            params = [start, end, *sources]
            for names, operator in ((apps, 'IN'), (excluded, 'NOT IN')):
                normalized = tuple(_identity(name) for name in names)
                if normalized:
                    where.append(f'{identity} {operator} (' + ','.join('?' for _ in normalized) + ')')
                    params.extend(normalized)
            if pending:
                where.extend(('c.transcript IS NULL', 'c.retry_after<=?'))
                params.append(time.time())
            if indexed:
                where.append('c.transcript IS NOT NULL')
            if topic:
                tokens = self._tokens(db, topic)
                if not tokens:
                    return [], 0
                where.append('c.id IN (SELECT clip FROM trace_call_terms WHERE term IN (' + ','.join('?' for _ in tokens)
                             + ') GROUP BY clip HAVING count(*)=?)')
                params.extend((*tokens, len(tokens)))
            clause = ' FROM trace_call_clips c JOIN meeting_sessions s ON s.id=c.session_id WHERE ' + ' AND '.join(where)
            count = db.execute('SELECT count(*)' + clause, params).fetchone()[0]
            order = 'c.start,c.id' if oldest else 'c.start DESC,c.id DESC'
            if point is not None:
                order = 'max(c.start-?,?-c.end,0),c.start DESC,c.id DESC'
                params.extend((point, point))
            rows = db.execute('SELECT c.id,c.session_id,c.source,c.start,c.end,c.speaker,s.app_name,s.channel_name,'
                              'c.transcript IS NOT NULL' + clause + ' ORDER BY ' + order + ' LIMIT ?',
                              (*params, limit)).fetchall()
        return [CallClip(*row) for row in rows], count
