"""Durable meeting provenance. Late ASR results retain their capture session."""
import math
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meeting_segments (
 event_id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, source TEXT NOT NULL,
 sequence INTEGER NOT NULL, captured_start REAL NOT NULL, captured_end REAL NOT NULL,
 processed_at REAL NOT NULL, capture_scope TEXT NOT NULL, speaker_id TEXT NOT NULL,
 overlap_seconds REAL NOT NULL DEFAULT 0, UNIQUE(session_id,source,sequence)
);
CREATE INDEX IF NOT EXISTS idx_meeting_segments_session ON meeting_segments(session_id,captured_start,event_id);
CREATE TABLE IF NOT EXISTS meeting_state (
 session_id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'recording',
 transcribed_segments INTEGER NOT NULL DEFAULT 0, gap_count INTEGER NOT NULL DEFAULT 0,
 dropped_seconds REAL NOT NULL DEFAULT 0, pending_segments INTEGER NOT NULL DEFAULT 0,
 last_reason TEXT, updated_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS meeting_segment_event_delete AFTER DELETE ON timeline BEGIN
 DELETE FROM meeting_segments WHERE event_id=old.id;
END;
CREATE TRIGGER IF NOT EXISTS meeting_session_records_delete AFTER DELETE ON meeting_sessions BEGIN
 DELETE FROM timeline WHERE id IN (SELECT event_id FROM meeting_segments WHERE session_id=old.id);
 DELETE FROM meeting_segments WHERE session_id=old.id;
 DELETE FROM meeting_state WHERE session_id=old.id;
END;
"""


def state(db,session_id,*,status=None,reason=None,dropped_seconds=0,pending_delta=0):
    if type(session_id) is not int or session_id<=0:raise ValueError('Invalid meeting session')
    if status not in {None,'recording','draining','finished','partial','unavailable'}:raise ValueError('Invalid meeting status')
    if reason is not None and (not isinstance(reason,str) or not 1<=len(reason)<=80):raise ValueError('Invalid meeting reason')
    if not math.isfinite(dropped_seconds) or dropped_seconds<0:raise ValueError('Invalid dropped duration')
    if type(pending_delta) is not int or not -32 <= pending_delta <= 32:
        raise ValueError('Invalid pending segment count')
    with db._lock,db._conn:
        if not db._conn.execute('SELECT 1 FROM meeting_sessions WHERE id=?',(session_id,)).fetchone():return False
        now=time.time()
        db._conn.execute('INSERT OR IGNORE INTO meeting_state(session_id,updated_at) VALUES(?,?)',(session_id,now))
        db._conn.execute("UPDATE meeting_state SET status=CASE WHEN ? IS NOT NULL OR gap_count>0 THEN 'partial' "
            "WHEN COALESCE(?,status)='draining' AND MAX(0,pending_segments+?)=0 THEN 'finished' "
            "ELSE COALESCE(?,status) END,gap_count=gap_count+?,dropped_seconds=dropped_seconds+?,"
            'pending_segments=MAX(0,pending_segments+?),'
            'last_reason=COALESCE(?,last_reason),updated_at=? WHERE session_id=?',
            (reason,status,pending_delta,status,int(reason is not None),dropped_seconds,pending_delta,reason,now,session_id))
        return True


def insert_segment(db,segment,text):
    if segment.source not in {'audio_in','audio_out'}:raise ValueError('Invalid meeting audio source')
    if not isinstance(text,str) or not text.strip():return False
    if len(text)>20000:raise ValueError('Meeting transcript exceeds segment budget')
    if (not math.isfinite(segment.started_at) or not math.isfinite(segment.ended_at)
            or not 0<segment.ended_at-segment.started_at<=30):raise ValueError('Invalid capture interval')
    if type(segment.sequence) is not int or segment.sequence<1:raise ValueError('Invalid segment sequence')
    if type(segment.session_id) is not int or segment.session_id <= 0:
        raise ValueError('Invalid meeting session')
    if (not math.isfinite(segment.overlap_seconds)
            or not 0 <= segment.overlap_seconds < segment.ended_at - segment.started_at):
        raise ValueError('Invalid overlap duration')
    if segment.speaker_id not in {'microphone','remote_unknown',*(f'speaker_{n}' for n in range(1,17))}:
        raise ValueError('Invalid anonymous speaker ID')
    if segment.capture_scope not in {'application_tree','microphone','synthetic'}:raise ValueError('Invalid capture scope')
    with db._lock,db._conn:
        session=db._conn.execute('SELECT started_at,ended_at,app_name,channel_name FROM meeting_sessions WHERE id=?',
                                (segment.session_id,)).fetchone()
        if not session:return False  # audit deletion/retention must not resurrect audio
        begin,end,app,channel=session
        allowed=getattr(db,'meeting_record_allowed',lambda app:True)
        if not allowed(app):return False
        if segment.started_at<begin-.25 or end is not None and segment.ended_at>end+.25:return False
        existing=db._conn.execute('SELECT event_id FROM meeting_segments WHERE session_id=? AND source=? AND sequence=?',
            (segment.session_id,segment.source,segment.sequence)).fetchone()
        if existing:return True
        cursor=db._conn.execute('INSERT INTO timeline(timestamp,source,app_name,window_title,content) VALUES(?,?,?,?,?)',
            (segment.started_at,segment.source,app,channel or '',text.strip()))
        now=time.time()
        db._conn.execute('INSERT INTO meeting_segments VALUES(?,?,?,?,?,?,?,?,?,?)',
            (cursor.lastrowid,segment.session_id,segment.source,segment.sequence,segment.started_at,
             segment.ended_at,now,segment.capture_scope,segment.speaker_id,segment.overlap_seconds))
        db._conn.execute('INSERT OR IGNORE INTO meeting_state(session_id,updated_at) VALUES(?,?)',(segment.session_id,now))
        db._conn.execute('UPDATE meeting_state SET transcribed_segments=transcribed_segments+1,updated_at=? WHERE session_id=?',
                        (now,segment.session_id))
        return True


def segment_metadata(connection,event_id):
    row=connection.execute('SELECT session_id,captured_start,captured_end,capture_scope,speaker_id,overlap_seconds '
                           'FROM meeting_segments WHERE event_id=?',(event_id,)).fetchone()
    if not row:return None
    return dict(zip(('session_id','captured_start','captured_end','capture_scope','speaker_id','overlap_seconds'),row))


def session_state(connection,session_id):
    row=connection.execute('SELECT status,transcribed_segments,gap_count,dropped_seconds,pending_segments,last_reason '
                           'FROM meeting_state WHERE session_id=?',(session_id,)).fetchone()
    return dict(zip(('status','transcribed_segments','gap_count','dropped_seconds','pending_segments','last_reason'),row)) if row else {'status':'untracked'}
