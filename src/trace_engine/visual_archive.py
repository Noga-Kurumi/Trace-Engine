"""Encrypted, size-bounded TRACE image files with an index in the timeline."""
from contextlib import closing
from dataclasses import dataclass
import logging
from pathlib import Path
import sqlite3
import stat
from time import time
from uuid import UUID, uuid4

from trace_engine.protection import protect_for_current_user, unprotect_for_current_user

logger = logging.getLogger(__name__)

MAX_JPEG_BYTES = 384 * 1024
MAX_ARCHIVE_BYTES = 1024**3
_POINT_MARGIN = 120
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS trace_visual (
    capture_id TEXT PRIMARY KEY,
    session_id INTEGER NOT NULL,
    captured_at REAL NOT NULL,
    size INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS trace_visual_time ON trace_visual(captured_at);
CREATE INDEX IF NOT EXISTS trace_visual_session ON trace_visual(session_id);
'''


@dataclass(frozen=True)
class Snapshot:
    capture_id: str
    captured_at: float
    app: str
    title: str


def _is_redirected(path):
    try:
        attributes = path.lstat()
    except FileNotFoundError:
        return False
    return (stat.S_ISLNK(attributes.st_mode)
            or getattr(attributes, 'st_reparse_tag', None) == stat.IO_REPARSE_TAG_MOUNT_POINT)


class VisualArchive:
    """Encrypted JPEG files associated with the timeline's foreground sessions."""

    def __init__(self, db_path, *, seal=protect_for_current_user, decrypt=unprotect_for_current_user):
        self.seal, self.decrypt = seal, decrypt
        self.db_path = Path(db_path).absolute()
        self.root = self.db_path.with_name(self.db_path.stem + '-visual')

    def _connection(self):
        return sqlite3.connect(self.db_path.as_uri() + '?mode=rw', uri=True, timeout=2)

    def _file(self, identifier):
        if UUID(hex=identifier).hex != identifier:
            raise ValueError('Invalid visual capture identity')
        return self.root / (identifier + '.trace')

    def record(self, session_id, captured_at, jpeg):
        """Persist only ciphertext and report whether the capture was committed."""
        if not 0 < len(jpeg) <= MAX_JPEG_BYTES or not session_id:
            return False
        blob = self.seal(jpeg)
        identifier = uuid4().hex
        path = self._file(identifier)
        created = False
        with closing(self._connection()) as db:
            db.executescript(_SCHEMA)
            try:
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    session = db.execute('SELECT 1 FROM window_sessions WHERE id=? AND ended_at IS NULL',
                                         (session_id,)).fetchone()
                    if not session:
                        return False
                    self.root.mkdir(exist_ok=True)
                    if _is_redirected(self.root):
                        raise ValueError('Unexpected visual archive directory')
                    if not self._make_room(db, len(blob)):
                        return False
                    with path.open('xb') as output:
                        created = True
                        output.write(blob)
                    db.execute('INSERT INTO trace_visual VALUES (?,?,?,?)',
                               (identifier, session_id, captured_at, len(blob)))
            except BaseException:
                if created:
                    path.unlink(missing_ok=True)
                raise
        return True

    def _remove(self, db, identifier):
        try:
            self._file(identifier).unlink(missing_ok=True)
        except (OSError, ValueError):
            logger.warning('Could not remove an expired TRACE image', exc_info=True)
            return False
        db.execute('DELETE FROM trace_visual WHERE capture_id=?', (identifier,))
        return True

    def _make_room(self, db, incoming):
        total = db.execute('SELECT COALESCE(SUM(size),0) FROM trace_visual').fetchone()[0]
        if total + incoming <= MAX_ARCHIVE_BYTES:
            return True
        for identifier, size in db.execute('SELECT capture_id, size FROM trace_visual ORDER BY captured_at').fetchall():
            if self._remove(db, identifier):
                total -= size
            if total + incoming <= MAX_ARCHIVE_BYTES:
                return True
        return False

    def prune(self, cutoff):
        if not self.root.is_dir() or _is_redirected(self.root):
            return
        with closing(self._connection()) as db:
            db.executescript(_SCHEMA)
            with db:
                db.execute('BEGIN IMMEDIATE')
                rows = db.execute('SELECT v.capture_id FROM trace_visual v LEFT JOIN window_sessions w '
                                  'ON v.session_id=w.id WHERE v.captured_at<? OR w.id IS NULL', (cutoff,)).fetchall()
                for (identifier,) in rows:
                    self._remove(db, identifier)
                tracked = {row[0] for row in db.execute('SELECT capture_id FROM trace_visual')}
                for path in self.root.glob('*.trace'):
                    try:
                        if path.stem not in tracked and self._file(path.stem) == path:
                            path.unlink(missing_ok=True)
                    except (OSError, ValueError):
                        logger.debug('Skipping unrelated or unavailable TRACE archive entry')

    def select(self, start, end, *, point=None, not_before=0, excluded_apps=()):
        """One nearest observation for a point, or up to three distributed in a range."""
        if not self.root.is_dir() or _is_redirected(self.root):
            return []
        excluded = tuple({str(app).strip().lower().removesuffix('.exe') for app in excluded_apps})
        sql = ('SELECT v.capture_id,v.captured_at,w.app_name,w.window_title FROM trace_visual v '
               'JOIN window_sessions w ON w.id=v.session_id WHERE v.captured_at>=? ')
        arguments = [not_before]
        if excluded:
            sql += ('AND CASE WHEN lower(w.app_name) LIKE \'%.exe\' THEN substr(lower(w.app_name),1,length(w.app_name)-4) '
                    'ELSE lower(w.app_name) END NOT IN (' + ','.join('?' for _ in excluded) + ') ')
            arguments.extend(excluded)
        with closing(self._connection()) as db:
            if point is not None:
                # An abandoned open session is only evidence near its capture.
                active = '(w.started_at<=? AND w.ended_at>?)'
                row = db.execute(sql + f'AND (v.captured_at BETWEEN ? AND ? OR {active}) '
                    f'ORDER BY {active} DESC, ABS(v.captured_at-?),v.captured_at DESC LIMIT 1',
                    [*arguments, point - _POINT_MARGIN, point + _POINT_MARGIN, point, point,
                     point, point, point]).fetchone()
                return [Snapshot(*row)] if row else []
            rows = db.execute(sql + 'AND v.captured_at>=? AND v.captured_at<? ORDER BY v.captured_at',
                              [*arguments, start, end]).fetchall()
        if len(rows) > 3:
            rows = [rows[0], rows[len(rows) // 2], rows[-1]]
        return [Snapshot(*row) for row in rows]

    def read(self, snapshot):
        path = self._file(snapshot.capture_id)
        if _is_redirected(self.root) or _is_redirected(path):
            raise ValueError('Unexpected TRACE archive location')
        with path.open('rb') as source:
            payload = source.read(MAX_JPEG_BYTES + 4096)
        if len(payload) >= MAX_JPEG_BYTES + 4096:
            raise ValueError('Oversized TRACE capture')
        jpeg = self.decrypt(payload)
        if not jpeg.startswith(b'\xff\xd8') or len(jpeg) > MAX_JPEG_BYTES:
            raise ValueError('Invalid JPEG in TRACE archive')
        return jpeg


def prune_visual_archive(db_path, retention_hours):
    try:
        VisualArchive(db_path).prune(time() - float(retention_hours) * 3600)
    except (OSError, sqlite3.Error, ValueError):
        logger.warning('Could not clean up the TRACE visual archive', exc_info=True)
