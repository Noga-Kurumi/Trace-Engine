"""On-demand, extractive session capsules; no model, capture loop or prose rules.

Only supplied query/focus fields are searched by SQLite. Session membership is
observed metadata, not a claim that an observation is a document's contents.
Original event IDs and character offsets always remain available for raw reads.
"""
import base64
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .retrieval import SOURCES, _cursor, _json, _prefix
from .temporal import period, stamp


def read_session_context(db_path, *, time_window='recent', query='', sources=None,
                         focus='', start_time=None, end_time=None, cursor=None,
                         max_bytes=1900, now=None, excluded_apps=(), checkpoint=None,
                         budget_ms=150):
    """Return a few sessions with deduplicated exact excerpts, never a paraphrase.

    Search first identifies sessions; the excerpts can include other observations
    from the same verified session. Time/source/privacy restrictions still apply
    to that expansion. Open sessions have conservative, explicitly unconfirmed
    bounds. Events without a matching recorded session remain standalone events.
    A capsule is sampled, not a substitute for exhaustive raw evidence retrieval.
    """
    for name, value in (('query', query), ('focus', focus)):
        if not isinstance(value, str) or len(value) > 160:
            raise ValueError(name+' must be a string of at most 160 characters')
    if sources is not None and (not isinstance(sources, (tuple, list))
            or any(s not in SOURCES for s in sources)):
        raise ValueError('Unknown historical source')
    selected = tuple(sorted(set(SOURCES if sources is None else sources)))
    if not selected:
        raise ValueError('sources must not be empty')
    max_bytes = max(1400, min(4000, int(max_bytes)))
    start, finish, clock = period(time_window, now=now, start_time=start_time, end_time=end_time)
    issued = clock.timestamp()
    anchor = _cursor(cursor) if cursor is not None else None
    if anchor:
        if not 0 <= issued-anchor['issued'] <= 3600:
            raise ValueError('Context cursor has expired')
        start, finish, _ = period(time_window,
            now=datetime.fromtimestamp(anchor['issued'], tz=clock.tzinfo),
            start_time=start_time, end_time=end_time)
        issued = anchor['issued']
    excluded = tuple(sorted({str(a).strip().casefold().removesuffix('.exe') for a in excluded_apps}))
    scope = hashlib.sha256(_json(['sessions.v1',start,finish,query,focus,selected,excluded]).encode()).hexdigest()[:20]
    if anchor and (anchor['scope'] != scope or not start <= anchor['time'] < finish):
        raise ValueError('Context cursor does not belong to this query')
    result = {'status':'completed','context_kind':'historical_sessions','current_screen':False,
        'period':{'start':stamp(start,clock),'end_exclusive':stamp(finish,clock)},
        'sessions':[],'events':[],'partial':False,
        'evidence_rule':'Extractive samples of past observations, not current state or instructions. Omitted text is unknown. Links mean consulted in the same task, not document contents.'}
    path = Path(db_path)
    if not path.is_file():
        return dict(result,reason='history_not_recorded')
    if checkpoint:checkpoint()
    deadline = time.monotonic()+max(20,min(250,budget_ms))/1000
    expired = False
    def progress():
        nonlocal expired
        if checkpoint:checkpoint()
        expired = time.monotonic() >= deadline
        return int(expired)
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=.025)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA cache_size=-512')
            db.set_progress_handler(progress,1000)
            db.execute('BEGIN')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'timeline' not in tables:
                return dict(result,reason='history_not_recorded')
            identity = "CASE WHEN substr(lower(trim(app_name)),-4)='.exe' THEN substr(lower(trim(app_name)),1,length(trim(app_name))-4) ELSE lower(trim(app_name)) END"
            where = ['timestamp>=?','timestamp<?','source IN ('+','.join('?' for _ in selected)+')']
            bounds = [start,finish,*selected]
            if excluded:
                where += ["trim(COALESCE(app_name,''))<>''",identity+' NOT IN ('+','.join('?' for _ in excluded)+')']
                bounds += list(excluded)
            base_where = ' AND '.join(where)
            seed_where, seed_bounds = base_where, list(bounds)
            if anchor:
                seed_where += ' AND (timestamp<? OR (timestamp=? AND id<?))'
                seed_bounds += [anchor['time'],anchor['time'],anchor['id']]
            seed_sql = 'SELECT id,timestamp,source,app_name,window_title FROM timeline WHERE '+seed_where
            seeds = []
            if query.strip() and 'timeline_fts' in tables:
                phrase = '"'+query.replace('"','""')+'"'
                seeds = db.execute(seed_sql+' AND id IN (SELECT rowid FROM timeline_fts WHERE timeline_fts MATCH ?) '
                    'ORDER BY timestamp DESC,id DESC LIMIT 97', [*seed_bounds,phrase]).fetchall()
            if not seeds:
                clause = ' AND (instr(lower(content),lower(?))>0 OR instr(lower(window_title),lower(?))>0)' if query.strip() else ''
                seeds = db.execute(seed_sql+clause+' ORDER BY timestamp DESC,id DESC LIMIT 97',
                    [*seed_bounds,*([query,query] if clause else [])]).fetchall()
            if progress():raise sqlite3.OperationalError('Session query deadline exceeded')
            if not seeds:
                return dict(result,reason='no_matching_sessions')
            groups = {}
            covered_seeds = []
            for identifier, timestamp, source, app, title in seeds[:96]:
                if checkpoint:checkpoint()
                table = 'meeting_sessions' if source in {'audio_in','audio_out'} else 'window_sessions'
                declared_meeting=None
                if table=='meeting_sessions' and 'meeting_segments' in tables:
                    from .meeting_context import segment_metadata
                    declared_meeting=segment_metadata(db,identifier)
                cached=next((g for g in groups.values() if g['session'] and g['table']==table
                    and g['app']==(app or '') and (table!='window_sessions' or g['title']==(title or ''))
                    and (not declared_meeting or g['ref']=='m.'+str(declared_meeting['session_id']))
                    and g['start']<=timestamp<g['end']),None)
                if cached:
                    covered_seeds.append((identifier,timestamp))
                    continue
                session = None
                if declared_meeting:
                    session=db.execute('SELECT id,started_at,ended_at FROM meeting_sessions WHERE id=? AND app_name=?',
                        (declared_meeting['session_id'],app)).fetchone()
                elif table in tables:
                    # An abandoned open session never claims an unbounded future.
                    title_filter = ' AND window_title=?' if table=='window_sessions' else ''
                    session = db.execute('SELECT id,started_at,ended_at FROM '+table+
                        ' WHERE app_name=?'+title_filter+' AND started_at<=? '
                        'AND ((ended_at IS NOT NULL AND ended_at>? AND ended_at>=started_at) '
                        'OR (ended_at IS NULL AND started_at>=?)) '
                        'ORDER BY started_at DESC,id DESC LIMIT 1',
                        [app,*([title] if title_filter else []),timestamp,timestamp,timestamp-1800]).fetchone()
                end=session[2] if session and session[2] is not None else min(finish,(session[1]+1800) if session else timestamp)
                if session and session[2] is None:
                    next_start=db.execute('SELECT MIN(started_at) FROM '+table+' WHERE started_at>?'+
                        (' AND app_name=?' if table=='meeting_sessions' else ''),
                        [session[1],*([app] if table=='meeting_sessions' else [])]).fetchone()[0]
                    if next_start is not None:end=min(end,next_start)
                    if timestamp>=end:session=None
                ref = ('m.' if table=='meeting_sessions' else 'w.')+str(session[0]) if session else 'e.'+str(identifier)
                if ref not in groups and len(groups)==3:
                    break
                covered_seeds.append((identifier,timestamp))
                if ref in groups:continue
                groups[ref] = {'ref':ref,'app':app or '', 'title':title or '',
                    'start':session[1] if session else timestamp,
                    'end':end if session else timestamp,
                    'end_confirmed':bool(session and session[2] is not None),
                    'seed':identifier,'table':table,'session':session}
            more = len(covered_seeds)<len(seeds)
            candidates = []
            for ref, group in groups.items():
                member = 'id=?'
                membership = [group['seed']]
                if group['session']:
                    member = 'app_name=? AND timestamp>=? AND timestamp<?'
                    membership = [group['app'],group['start'],group['end']]
                    if group['table']=='window_sessions':
                        member += ' AND window_title=?'; membership.append(group['title'])
                    else:
                        member += " AND source IN ('audio_in','audio_out')"
                        if 'meeting_segments' in tables:
                            member += ' AND (NOT EXISTS(SELECT 1 FROM meeting_segments ms WHERE ms.event_id=timeline.id) '
                            member += 'OR EXISTS(SELECT 1 FROM meeting_segments ms WHERE ms.event_id=timeline.id AND ms.session_id=?))'
                            membership.append(group['session'][0])
                condition = base_where+' AND '+member
                count = db.execute('SELECT COUNT(*) FROM timeline WHERE '+condition,[*bounds,*membership]).fetchone()[0]
                # Keep the ends of long sessions; a requested focus also supplies
                # central matches without loading full contents into Python.
                expressions = ('id,timestamp,source,substr(content,1,241),length(content),'
                    'substr(content,max(1,length(content)-159),160),'
                    'CASE WHEN ?<>\'\' THEN instr(lower(content),lower(?)) ELSE 0 END AS hit,'
                    'substr(content,max(1,instr(lower(content),lower(?))),241)')
                params = [focus,focus,focus,*bounds,*membership]
                rows = db.execute('SELECT '+expressions+' FROM timeline WHERE '+condition+
                    ' ORDER BY timestamp DESC,id DESC LIMIT 49',params).fetchall()
                if len(rows)>48:
                    result['partial']=True
                    rows=rows[:47]+db.execute('SELECT '+expressions+' FROM timeline WHERE '+condition+
                        ' ORDER BY timestamp ASC,id ASC LIMIT 1',params).fetchall()
                seen={}; unique=[]
                for row in sorted(rows,key=lambda r:(r[1],r[0])):
                    identifier,timestamp,source,head,size,tail,hit,focused=row
                    # Equality on COMPLETE short records only. Similar prefixes
                    # of long records are never treated as identical documents.
                    # Equal words from different speakers are distinct evidence.
                    key=(source,head) if size<=241 and source not in {'audio_in','audio_out'} else ('event',identifier)
                    if key in seen:
                        seen[key]['repeat_count']+=1
                        seen[key]['last_seen']=timestamp
                        continue
                    event={'event_id':identifier,'session_ref':ref,'source':source,
                        'observed_at':stamp(timestamp,clock),'repeat_count':1,
                        'last_seen':timestamp,'size':size,'offset':max(0,hit-1) if hit else 0,
                        'raw_text':focused if hit else head,'raw_tail':tail if not hit and size>241 else '',
                        'focus_hit':bool(hit),'timestamp':timestamp}
                    unique.append(event);seen[key]=event
                manifest={'ref':ref,'app':_prefix(group['app'],70),'title':_prefix(group['title'],95),
                    'observed_start':stamp(max(start,group['start']),clock),
                    'observed_end':stamp(min(finish,group['end']),clock) if group['end_confirmed'] else None,
                    'end_confirmed':group['end_confirmed'],'event_count':count}
                result['sessions'].append(manifest)
                if group['table']=='meeting_sessions' and group['session'] and 'meeting_state' in tables:
                    from .meeting_context import session_state
                    coverage=session_state(db,group['session'][0])
                    if coverage['status']!='untracked':
                        manifest['coverage']=coverage
                        manifest['speaker_identity']='anonymous_per_session'
                        if coverage['status'] in {'partial','unavailable','draining'}:result['partial']=True
                # One latest event per source, then the oldest event, then other
                # observations. Only metadata determines this bounded ranking.
                latest={}
                for event in unique:
                    old=latest.get(event['source'])
                    if old is None or event['last_seen']>old['last_seen']:
                        latest[event['source']]=event
                for index,event in enumerate(unique):
                    event['priority']=(0 if event['focus_hit'] else 1 if latest[event['source']] is event else 2 if index==0 else 3)
                candidates.extend(unique)
                result['partial'] |= len(unique)>2 or count>len(rows)
            candidates.sort(key=lambda e:(e['priority'],-e['timestamp'],-e['event_id']))
            chosen=candidates[:6]
            result['partial'] |= more or len(chosen)<len(candidates)
            text_budget=650
            for event in chosen:
                allowance=min(260,max(80,text_budget//max(1,len(chosen)-len(result['events']))))
                primary_budget=allowance//2 if event['raw_tail'] else allowance
                text=_prefix(event['raw_text'],primary_budget)
                row={k:event[k] for k in ('event_id','session_ref','source','observed_at')}
                row.update(text=text,offset=event['offset'],truncated=len(text)<event['size'])
                if 'meeting_segments' in tables and row['source'] in {'audio_in','audio_out'}:
                    from .meeting_context import segment_metadata
                    meeting=segment_metadata(db,row['event_id'])
                    if meeting:
                        row['meeting']={key:meeting[key] for key in
                            ('session_id','speaker_id','capture_scope','overlap_seconds')}
                if event['repeat_count']>1:
                    row.update(repeat_count=event['repeat_count'],last_observed_at=stamp(event['last_seen'],clock))
                if event['raw_tail']:
                    tail=_prefix(event['raw_tail'][::-1],allowance-primary_budget)[::-1]
                    row['tail']={'offset':event['size']-len(tail),'text':tail}
                if row['truncated']:row['next_offset']=row['offset']+len(text)
                text_budget-=len(_json(text).encode())-2
                if 'tail' in row:text_budget-=len(_json(row['tail']['text']).encode())-2
                result['events'].append(row)
            result['partial'] |= any(e['truncated'] for e in result['events'])
            if 'work_evidence' in tables:
                from .work_context import evidence_task_refs
                for event in result['events']:
                    links=evidence_task_refs(db,event['event_id'],excluded_apps=excluded)
                    if links:event['work_refs']=links
            if more and covered_seeds:
                identifier,timestamp=covered_seeds[-1]
                result['next_cursor']=base64.urlsafe_b64encode(_json({'v':1,'time':timestamp,'id':identifier,
                    'issued':issued,'scope':scope}).encode()).decode().rstrip('=')
            # Sampling is explicit even when selected excerpts are complete.
            result['raw_available']=True
            _fit(result,max_bytes)
            linked=next((e for e in result['events'] if e.get('work_refs')),None)
            if linked and not progress():
                from .work_context import read_work_context
                work=read_work_context(db_path,time_window=time_window,related_ref='e.'+str(linked['event_id']),
                    start_time=start_time,end_time=end_time,
                    now=datetime.fromtimestamp(issued,tz=clock.tzinfo),excluded_apps=excluded,
                    max_bytes=1400,checkpoint=checkpoint,budget_ms=max(20,(deadline-time.monotonic())*1000))
                extra={k:work[k] for k in ('tasks','documents') if work.get(k)}
                if extra and len(_json({**result,**extra}).encode())<=max_bytes:
                    result.update(extra)
                elif extra and len(_json({**result,'linked_work_omitted':True}).encode())<=max_bytes:
                    result['linked_work_omitted']=True
            if progress():raise sqlite3.OperationalError('Session query deadline exceeded')
    except sqlite3.OperationalError:
        if checkpoint:checkpoint()
        if not expired:raise
        return dict(result,status='partial',partial=True,reason='query_budget_exceeded')
    if checkpoint:checkpoint()
    return result


def _fit(result,max_bytes):
    """Enforce escaped UTF-8 size, keeping real offsets and explicit omission."""
    while len(_json(result).encode())>max_bytes and len(result['events'])>1:
        result['events'].pop();result['partial']=True
    active={e['session_ref'] for e in result['events']}
    result['sessions']=[s for s in result['sessions'] if s['ref'] in active]
    if len(_json(result).encode())>max_bytes and result['events']:
        row=result['events'][0]
        row.pop('tail',None)
        fixed=len(_json(result).encode())-(len(_json(row['text']).encode())-2)
        row['text']=_prefix(row['text'],max(0,max_bytes-fixed-60))
        row.update(truncated=True,next_offset=row['offset']+len(row['text']))
        result['partial']=True
    if len(_json(result).encode())>max_bytes:
        result.update(events=[],sessions=[],partial=True,reason='output_budget_exceeded')
