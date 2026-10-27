"""Links between observed evidence, actual filesystem operations and tasks.

No conversation classification, content duplication, filesystem search or model.
The host supplies executor observations; locations returned here are historical
candidates, never current file handles or permission to execute an operation.
"""
from contextlib import closing
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .temporal import period, stamp
from .retrieval import _json, _prefix

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_tasks (
 ref TEXT PRIMARY KEY, started REAL NOT NULL, updated REAL NOT NULL,
 status TEXT NOT NULL, app_name TEXT NOT NULL, expected_steps INTEGER
);
CREATE INDEX IF NOT EXISTS idx_work_tasks_time ON work_tasks(updated);
CREATE TABLE IF NOT EXISTS work_documents (
 ref TEXT PRIMARY KEY, path TEXT NOT NULL, path_key TEXT NOT NULL,
 device TEXT NOT NULL, file_id TEXT NOT NULL, size INTEGER NOT NULL,
 mtime_ns INTEGER NOT NULL, directory INTEGER NOT NULL,
 verified_at REAL NOT NULL, state TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_documents_path ON work_documents(path_key,verified_at);
CREATE INDEX IF NOT EXISTS idx_work_documents_identity ON work_documents(device,file_id,verified_at);
CREATE TABLE IF NOT EXISTS work_operations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_ref TEXT NOT NULL,
 ordinal INTEGER NOT NULL, observed_at REAL NOT NULL, action TEXT NOT NULL,
 status TEXT NOT NULL, document_ref TEXT, path TEXT, destination TEXT,
 native_step INTEGER, directory INTEGER, UNIQUE(task_ref,ordinal)
);
CREATE INDEX IF NOT EXISTS idx_work_operations_document ON work_operations(document_ref,observed_at);
CREATE VIRTUAL TABLE IF NOT EXISTS work_operations_fts USING fts5(
 path,destination,content='work_operations',content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS work_operation_insert AFTER INSERT ON work_operations BEGIN
 INSERT INTO work_operations_fts(rowid,path,destination) VALUES(new.id,new.path,new.destination);
END;
CREATE TRIGGER IF NOT EXISTS work_operation_delete AFTER DELETE ON work_operations BEGIN
 INSERT INTO work_operations_fts(work_operations_fts,rowid,path,destination)
 VALUES('delete',old.id,old.path,old.destination);
END;
CREATE TABLE IF NOT EXISTS work_evidence (
 task_ref TEXT NOT NULL, event_id INTEGER NOT NULL,
 PRIMARY KEY(task_ref,event_id)
);
CREATE INDEX IF NOT EXISTS idx_work_evidence_event ON work_evidence(event_id);
CREATE TRIGGER IF NOT EXISTS work_task_delete AFTER DELETE ON work_tasks BEGIN
 DELETE FROM work_operations WHERE task_ref=old.ref;
 DELETE FROM work_evidence WHERE task_ref=old.ref;
END;
CREATE TRIGGER IF NOT EXISTS work_event_delete AFTER DELETE ON timeline BEGIN
 DELETE FROM work_evidence WHERE event_id=old.id;
END;
"""

STATUSES = {'recording','completed','incomplete','cancelled','interrupted'}
ACTIONS = {'read','list','search','write','move','delete','mkdir'}


def _identity(value):
    return (isinstance(value,(tuple,list)) and len(value)==4
            and all(type(v) is int and v>=0 for v in value))


def _ref(value, kinds=('t','d')):
    if not isinstance(value,str) or len(value)!=34 or value[1]!='.' or value[0] not in kinds:
        raise ValueError('Invalid historical work reference')
    if uuid.UUID(hex=value[2:]).hex != value[2:]:
        raise ValueError('Noncanonical historical reference')
    return value


def _privacy(excluded_apps, column='app_name'):
    identities=tuple(sorted({str(a).strip().casefold().removesuffix('.exe') for a in excluded_apps}))
    expression=f"CASE WHEN substr(lower(trim({column})),-4)='.exe' THEN substr(lower(trim({column})),1,length(trim({column}))-4) ELSE lower(trim({column})) END"
    clause=(f" AND trim(COALESCE({column},''))<>'' AND {expression} NOT IN ("+
            ','.join('?' for _ in identities)+')') if identities else ''
    return clause,identities


def _connect(path, *, write=False):
    connection=sqlite3.connect(Path(path).resolve().as_uri()+('?mode=rw' if write else '?mode=ro'),
                               uri=True,timeout=.025)
    try:
        connection.execute('PRAGMA cache_size=-512')
        if write and connection.execute('PRAGMA journal_mode').fetchone()[0]=='wal':
            # Optional metadata, not user file contents. WAL/NORMAL avoids a disk
            # sync per step; an OS/power failure may lose the newest metadata.
            connection.execute('PRAGMA synchronous=NORMAL')
        elif not write:
            connection.execute('PRAGMA query_only=ON')
    except BaseException:
        connection.close()
        raise
    return connection


def _document(db, observation):
    """Reuse identity only from a completed executor observation, not OCR text."""
    path=observation['destination'] if observation['action']=='move' else observation['path']
    before,after=observation.get('before'),observation.get('after')
    deleted=observation['action']=='delete'
    identity=before if deleted else after
    if not _identity(identity):
        return None
    device,file_id,size,mtime=identity
    key=os.path.normcase(os.path.normpath(observation['path']))
    previous=db.execute("SELECT ref,device,file_id FROM work_documents WHERE path_key=? "
        "AND state IN ('present','location_uncertain') ORDER BY verified_at DESC LIMIT 1",(key,)).fetchone()
    reference=None
    compare=before if observation['action'] in {'write','move','delete'} else after
    if previous and _identity(compare) and previous[1:]==(str(compare[0]),str(compare[1])):
        reference=previous[0]
    if reference is None and observation['action']!='write':
        row=db.execute("SELECT ref FROM work_documents WHERE device=? AND file_id=? "
            "AND state IN ('present','location_uncertain') ORDER BY verified_at DESC LIMIT 1",
            (str(device),str(file_id))).fetchone()
        reference=row[0] if row else None
    if reference is None:
        reference='d.'+uuid.uuid4().hex
    if previous and previous[0]!=reference:
        db.execute("UPDATE work_documents SET state='replaced' WHERE ref=?",(previous[0],))
    db.execute('INSERT INTO work_documents VALUES (?,?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(ref) DO UPDATE SET path=excluded.path,path_key=excluded.path_key,'
        'device=excluded.device,file_id=excluded.file_id,size=excluded.size,mtime_ns=excluded.mtime_ns,'
        'directory=excluded.directory,verified_at=excluded.verified_at,state=excluded.state',
        (reference,path,os.path.normcase(os.path.normpath(path)),str(device),str(file_id),size,mtime,
         int(observation['directory']),observation['observed_at'],'deleted' if deleted else 'present'))
    if observation['directory'] and observation['action'] in {'move','delete'}:
        # Do not invent child locations after a parent change. A later real read
        # can establish them; indexed exact/prefix metadata has no file scan.
        prefix=(key.rstrip(os.sep)+os.sep).replace('^','^^').replace('%','^%').replace('_','^_')+'%'
        db.execute('UPDATE work_documents SET state=? WHERE path_key LIKE ? ESCAPE \'^\' AND ref<>?',
            ('deleted' if deleted else 'location_uncertain',prefix,reference))
    return reference


def record_operation(db_path, *, task_ref, ordinal, observation, event_ids=(), app_name='', expected_steps=None,
                     excluded_apps=()):
    """Append the existing executor ledger atomically; no file contents/prompts."""
    _ref(task_ref,('t',))
    if type(ordinal) is not int or not 1<=ordinal<=128 or observation.get('action') not in ACTIONS:
        raise ValueError('Invalid work operation')
    timestamp=observation.get('observed_at')
    if type(timestamp) not in (int,float) or not math.isfinite(timestamp):
        raise ValueError('Invalid operation observation time')
    if len(event_ids)>32 or any(type(i) is not int or i<=0 for i in event_ids):
        raise ValueError('Too many or invalid evidence links')
    status=observation.get('status','completed')
    if status not in {'completed','error','denied','cancelled','disabled','not_executed'}:
        raise ValueError('Invalid executor status')
    if status=='completed':
        for field in ('path','destination'):
            value=observation.get(field)
            if field=='destination' and value is None:continue
            if not isinstance(value,str) or len(value)>1024 or not Path(value).is_absolute():
                raise ValueError('Work paths require bounded, verified absolute locations')
        if type(observation.get('directory')) is not bool:
            raise ValueError('Document type must come from the executor')
    with closing(_connect(db_path,write=True)) as db, db:
        db.execute('INSERT INTO work_tasks VALUES (?,?,?,?,?,?) ON CONFLICT(ref) DO UPDATE '
            'SET updated=excluded.updated,status=excluded.status',
            (task_ref,timestamp,timestamp,'recording',app_name[:160],expected_steps))
        document=_document(db,observation) if status=='completed' else None
        recorded_path=observation.get('path') if status=='completed' else None
        if status!='completed' and observation.get('historical_document_ref'):
            reference=_ref(observation['historical_document_ref'],('d',))
            privacy,values=_privacy(excluded_apps,'t.app_name')
            # Link an attempt only to a previously visible, real observation.
            # Never promote a failed proposal to a new verified location.
            previous=db.execute('SELECT o.path,o.destination,o.action FROM work_operations o '
                'JOIN work_tasks t ON t.ref=o.task_ref WHERE o.document_ref=? AND o.status=\'completed\''+
                privacy+' AND (o.path=? OR o.destination=?) ORDER BY o.observed_at DESC,o.id DESC LIMIT 1',
                (reference,*values,observation.get('path'),observation.get('path'))).fetchone()
            if previous:
                document=reference
                recorded_path=observation['path']
        cursor=db.execute('INSERT INTO work_operations(task_ref,ordinal,observed_at,action,status,'
            'document_ref,path,destination,native_step,directory) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (task_ref,ordinal,timestamp,observation['action'],status,document,
             recorded_path,
             observation.get('destination') if status=='completed' else None,observation.get('native_step'),
             int(observation.get('directory',False))))
        privacy,values=_privacy(excluded_apps)
        db.executemany('INSERT OR IGNORE INTO work_evidence SELECT ?,id FROM timeline WHERE id=?'+privacy,
                       [(task_ref,i,*values) for i in event_ids])
        return {'task_ref':task_ref,'operation_id':cursor.lastrowid,
                **({'document_ref':document} if document else {})}


def finish_task(db_path, task_ref, status, *, now=None):
    _ref(task_ref,('t',))
    if status not in STATUSES-{'recording'}:
        raise ValueError('Invalid final task status')
    with closing(_connect(db_path,write=True)) as db, db:
        db.execute('UPDATE work_tasks SET status=?,updated=? WHERE ref=?',
                   (status,time.time() if now is None else now,task_ref))


def evidence_task_refs(db, event_id, excluded_apps=()):
    privacy,values=_privacy(excluded_apps,'t.app_name')
    return [row[0] for row in db.execute('SELECT t.ref FROM work_evidence e JOIN work_tasks t '
        'ON t.ref=e.task_ref WHERE e.event_id=?'+privacy+' ORDER BY t.updated DESC,t.ref LIMIT 2',
        (event_id,*values))]


def _path_field(path):
    # Never pass an abbreviated string as an executable path.
    if len(_json(path).encode())<=350:
        return {'path':path}
    return {'path_preview':_prefix(path,200),'path_truncated':True}


def read_work_context(db_path, *, time_window='recent', query='', related_ref=None,
                      start_time=None,end_time=None,offset=0,max_bytes=1900,now=None,
                      excluded_apps=(),checkpoint=None,budget_ms=100):
    """Read a bounded page of actual tasks and document-location candidates."""
    if not isinstance(query,str) or len(query)>160 or type(offset) is not int or not 0<=offset<=100000:
        raise ValueError('Invalid work query')
    max_bytes=max(1400,min(4000,int(max_bytes)))
    if related_ref is not None:
        if isinstance(related_ref,str) and related_ref.startswith('e.'):
            identifier=related_ref[2:]
            if not identifier.isascii() or not identifier.isdecimal() or not 0<int(identifier)<2**63:
                raise ValueError('Invalid evidence reference')
        else:
            _ref(related_ref)
    start,finish,clock=period(time_window,now=now,start_time=start_time,end_time=end_time)
    result={'status':'completed','context_kind':'historical_work','current_screen':False,
        'period':{'start':stamp(start,clock),'end_exclusive':stamp(finish,clock)},'tasks':[],
        'documents':[],'partial':False,
        'evidence_rule':'Recorded tool steps only. Paths are historical candidates: recheck with filesystem. '
                        'Links mean retrieved in the same task, not a semantic match or permission.'}
    if not Path(db_path).is_file():
        return dict(result,reason='history_not_recorded')
    if checkpoint:checkpoint()
    deadline=time.monotonic()+max(20,min(150,budget_ms))/1000
    expired=False
    def progress():
        nonlocal expired
        if checkpoint:checkpoint()
        expired=time.monotonic()>=deadline
        return int(expired)
    try:
        with closing(_connect(db_path)) as db:
            db.set_progress_handler(progress,1000)
            db.execute('BEGIN')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='work_tasks'").fetchone():
                return dict(result,reason='work_not_recorded')
            privacy,values=_privacy(excluded_apps,'t.app_name')
            event_privacy,event_values=_privacy(excluded_apps,'v.app_name')
            where='t.updated>=? AND t.updated<?'+privacy
            params=[start,finish,*values]
            if related_ref:
                if related_ref.startswith('t.'):
                    where+=' AND t.ref=?';params.append(related_ref)
                elif related_ref.startswith('d.'):
                    where+=' AND t.ref IN (SELECT task_ref FROM work_operations WHERE document_ref=?)'
                    params.append(related_ref)
                else:
                    where+=' AND t.ref IN (SELECT e.task_ref FROM work_evidence e JOIN timeline v ON v.id=e.event_id '
                    where+='WHERE e.event_id=?'+event_privacy+')'
                    params.extend([int(related_ref[2:]),*event_values])
            if query:
                where+=' AND t.ref IN (SELECT o.task_ref FROM work_operations o WHERE '
                where+='(o.id IN (SELECT rowid FROM work_operations_fts WHERE work_operations_fts MATCH ?) '
                where+='OR instr(lower(o.path),lower(?))>0 OR instr(lower(o.destination),lower(?))>0))'
                params.extend(['"'+query.replace('"','""')+'"',query,query])
            if progress():raise sqlite3.OperationalError('Historical work query deadline exceeded')
            step_page=bool(related_ref and related_ref.startswith('t.'))
            tasks=db.execute('SELECT t.ref,t.updated,t.status,t.expected_steps FROM work_tasks t WHERE '+where+
                ' ORDER BY t.updated DESC,t.ref LIMIT 3 OFFSET ?',(*params,0 if step_page else offset)).fetchall()
            for reference,updated,status,expected in tasks[:2]:
                operations=db.execute('SELECT id,action,status,document_ref,path,destination FROM work_operations '
                    'WHERE task_ref=? ORDER BY ordinal '+('ASC' if step_page else 'DESC')+' LIMIT 4 OFFSET ?',
                    (reference,offset if step_page else 0)).fetchall()
                task={'ref':reference,'recorded_at':stamp(updated,clock),
                      'status':'unfinished' if status=='recording' else status,
                      'completion_scope':'recorded_tool_steps','steps':[]}
                if expected is not None:task['expected_steps']=expected
                steps=operations[:3] if step_page else list(reversed(operations[:3]))
                for identifier,action,op_status,document,path,destination in steps:
                    step={'id':identifier,'action':action,'status':op_status}
                    if document:step['document_ref']=document
                    if document and op_status!='completed':step['subject_kind']='historical_candidate'
                    if path:step.update(_path_field(path))
                    if destination:
                        field=_path_field(destination)
                        step.update({('destination' if k=='path' else 'destination_'+k):v for k,v in field.items()})
                    task['steps'].append(step)
                if len(operations)>3:task['steps_partial']=True
                evidence=db.execute('SELECT e.event_id FROM work_evidence e JOIN timeline v ON v.id=e.event_id '
                    'WHERE e.task_ref=?'+event_privacy+' ORDER BY e.event_id DESC LIMIT 4',
                    (reference,*event_values)).fetchall()
                if evidence:task['evidence_refs']=['e.'+str(e[0]) for e in evidence[:3]]
                result['tasks'].append(task)
                if related_ref and related_ref.startswith('d.'):
                    document_refs=[related_ref]
                else:document_refs=list(dict.fromkeys(op[3] for op in operations if op[3]))[:2]
                for document in document_refs:
                    if any(d['ref']==document for d in result['documents']):continue
                    row=db.execute('SELECT o.path,o.destination,o.observed_at,o.action,o.directory '
                        'FROM work_operations o JOIN work_tasks t ON t.ref=o.task_ref '
                        'WHERE o.document_ref=? AND o.status=\'completed\''+privacy+
                        ' ORDER BY o.observed_at DESC,o.id DESC LIMIT 1',(document,*values)).fetchone()
                    if row:
                        path,destination,verified,action,directory=row
                        if action=='move':path=destination
                        state='deleted' if action=='delete' else 'present'
                        result['documents'].append({'ref':document,**_path_field(path),
                            'verified_at':stamp(verified,clock),'last_recorded_state':state,
                            'directory':bool(directory),'requires_filesystem_check':True})
            result['partial']=len(tasks)>2 or any(t.get('steps_partial') for t in result['tasks'])
            if len(tasks)>2:result['next_offset']=offset+2
            if step_page and len(tasks)>0:
                result['pagination_scope']='task_steps'
                if result['partial']:result['next_offset']=offset+len(result['tasks'][0]['steps'])
            # Metadata is disposable context; never truncate an actual path or
            # silently present the last few steps as a complete task history.
            while len(_json(result).encode())>max_bytes and result['documents']:
                result['documents'].pop();result['partial']=True
            while len(_json(result).encode())>max_bytes and result['tasks']:
                task=result['tasks'][-1]
                if len(task['steps'])>1:
                    task['steps'].pop(-1 if step_page else 0)
                    task['steps_partial']=True;result['partial']=True
                    if step_page:result['next_offset']=offset+len(task['steps'])
                elif len(result['tasks'])>1:
                    result['tasks'].pop();result.update(partial=True,next_offset=offset+len(result['tasks']))
                else:
                    step=task['steps'][0] if task['steps'] else None
                    if step:
                        for field in ('path','destination'):
                            if field in step:
                                step[field+'_preview']=_prefix(step.pop(field),100)
                                step[field+'_truncated']=True
                        task.pop('evidence_refs',None)
                    if len(_json(result).encode())>max_bytes:
                        result['tasks']=[];result['reason']='output_budget_exceeded'
                    result['partial']=True
                    break
            if not tasks:result['reason']='no_matching_tasks'
    except sqlite3.OperationalError:
        if checkpoint:checkpoint()
        if not expired:raise
        result.update(status='partial',partial=True,reason='query_budget_exceeded')
    if checkpoint:checkpoint()
    return result
