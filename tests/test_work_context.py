"""Historical document/task links on actual isolated SQLite databases."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from trace_engine.storage.timeline_db import TimelineDB
from trace_engine.retrieval import read_context
from trace_engine.work_context import record_operation,finish_task,read_work_context


class WorkContextTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/'timeline.db'
        self.db=TimelineDB(str(self.path))
        self.addCleanup(self.db.close)
        self.clock=datetime(2026,10,24,12,tzinfo=timezone(timedelta(hours=-3)))
        self.then=self.clock.replace(hour=0).timestamp()-3600
        self.file=str(Path(self.directory.name)/'informe.txt')
        self.other=str(Path(self.directory.name)/'Archivo'/'informe.txt')
        self.task='t.'+uuid.uuid4().hex
        self.identity=(3,571,20,1790000000000000000)

    def operation(self,action='read',*,task=None,ordinal=1,when=None,app='editor.exe',
                  path=None,destination=None,before=None,after=None,status='completed',events=()):
        return record_operation(self.path,task_ref=task or self.task,ordinal=ordinal,app_name=app,
            event_ids=events,observation={'action':action,'status':status,'path':path or self.file,
                'destination':destination,'before':before,'after':after if after is not None else self.identity,
                'observed_at':self.then if when is None else when,'directory':False})

    def read(self,**kw):
        return read_work_context(self.path,time_window='ayer',now=self.clock,**kw)

    def test_evidence_task_document_link_and_move_share_a_document_id(self):
        self.db.insert('ocr','editor.exe','Aviso','Documento lunar',self.then)
        event=self.db._conn.execute('SELECT id FROM timeline').fetchone()[0]
        first=self.operation(events=(event,))
        second=self.operation('move',ordinal=2,before=self.identity,destination=self.other,events=(event,))
        self.assertEqual(first['document_ref'],second['document_ref'])
        finish_task(self.path,self.task,'completed',now=self.then+1)
        context=read_context(self.path,time_window='ayer',now=self.clock)
        self.assertEqual(context['events'][0]['work_refs'],[self.task])
        work=self.read(related_ref='e.'+str(event))
        self.assertEqual(work['documents'][0]['path'],self.other)
        self.assertTrue(work['documents'][0]['requires_filesystem_check'])
        self.assertEqual(work['tasks'][0]['status'],'completed')

    def test_atomic_overwrite_preserves_logical_document_after_inode_change(self):
        first=self.operation()
        second=self.operation('write',ordinal=2,before=self.identity,after=(3,999,30,self.identity[3]+1))
        self.assertEqual(first['document_ref'],second['document_ref'])

    def test_an_unrelated_replacement_at_same_path_gets_a_new_document(self):
        first=self.operation()
        second=self.operation(ordinal=2,after=(3,999,30,self.identity[3]+1))
        self.assertNotEqual(first['document_ref'],second['document_ref'])

    def test_deletion_is_historical_not_a_current_existence_assertion(self):
        self.operation()
        self.operation('delete',ordinal=2,before=self.identity)
        work=self.read()
        self.assertEqual(work['documents'][0]['last_recorded_state'],'deleted')
        self.assertTrue(work['documents'][0]['requires_filesystem_check'])

    def test_denial_never_creates_document_or_verified_path(self):
        self.operation('delete',status='denied')
        work=self.read()
        self.assertEqual(work['documents'],[])
        self.assertNotIn('path',work['tasks'][0]['steps'][0])

    def test_task_and_document_references_can_be_followed_without_filename_guess(self):
        link=self.operation()
        self.assertEqual(self.read(related_ref=self.task)['tasks'][0]['ref'],self.task)
        self.assertEqual(self.read(related_ref=link['document_ref'])['documents'][0]['ref'],link['document_ref'])
        absent='t.'+uuid.uuid4().hex
        self.assertEqual(self.read(related_ref=absent)['reason'],'no_matching_tasks')

    def test_current_exclusion_cannot_leak_latest_path_through_shared_document(self):
        first=self.operation()
        private='t.'+uuid.uuid4().hex
        self.operation('move',task=private,before=self.identity,destination=self.other,app='private.exe',when=self.then+1)
        work=self.read(related_ref=first['document_ref'],excluded_apps=['private.exe'])
        self.assertEqual(work['documents'][0]['path'],self.file)
        self.assertEqual([t['ref'] for t in work['tasks']],[self.task])

    def test_excluded_evidence_is_not_returned_or_used_as_an_indirect_link(self):
        self.db.insert('ocr','private.exe','Privado','Dato',self.then)
        event=self.db._conn.execute('SELECT id FROM timeline').fetchone()[0]
        self.operation(events=(event,))
        self.assertNotIn('evidence_refs',self.read(excluded_apps=['private.exe'])['tasks'][0])
        self.assertEqual(self.read(related_ref='e.'+str(event),excluded_apps=['private.exe'])['tasks'],[])

    def test_time_filter_precedes_limit_even_with_many_newer_tasks(self):
        self.operation()
        for index in range(20):
            self.operation(task='t.'+uuid.uuid4().hex,when=self.clock.timestamp()-index)
        self.assertEqual([t['ref'] for t in self.read()['tasks']],[self.task])

    def test_step_pagination_is_complete_without_repeats(self):
        for index in range(8):self.operation(ordinal=index+1,when=self.then+index)
        identifiers=[];offset=0
        for _ in range(10):
            page=self.read(related_ref=self.task,offset=offset,max_bytes=4000)
            identifiers.extend(s['id'] for s in page['tasks'][0]['steps'])
            if 'next_offset' not in page:break
            self.assertGreater(page['next_offset'],offset)
            offset=page['next_offset']
        self.assertEqual(identifiers,list(range(1,9)))

    def test_reading_metadata_never_opens_document_contents(self):
        self.operation()
        # There is deliberately no actual file at this verified historical path.
        self.assertFalse(Path(self.file).exists())
        self.assertEqual(self.read()['documents'][0]['path'],self.file)

    def test_output_cap_counts_json_escapes_and_long_unicode_paths(self):
        for index in range(3):
            self.operation(ordinal=index+1,path=str(Path(self.directory.name)/('文'*150+str(index)+'.txt')))
        for budget in (1400,1900,4000):
            result=self.read(max_bytes=budget,related_ref=self.task)
            self.assertLessEqual(len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode()),budget)
            for task in result['tasks']:
                for step in task['steps']:
                    self.assertNotIn('path',step)
                    self.assertTrue(step['path_truncated'])

    def test_retention_cleans_tasks_steps_links_and_orphan_documents(self):
        self.db.insert('ocr','editor.exe','Aviso','Dato',self.then)
        self.operation(events=(1,))
        self.db.cleanup_old_records(0)
        for table in ('work_tasks','work_operations','work_evidence','work_documents'):
            self.assertEqual(self.db._conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0,table)

    def test_explicit_audit_clear_removes_all_new_metadata(self):
        self.operation()
        self.db.clear_all_records()
        for table in ('work_tasks','work_operations','work_evidence','work_documents'):
            self.assertEqual(self.db._conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_unfinished_recording_is_never_claimed_completed(self):
        self.operation()
        self.assertEqual(self.read()['tasks'][0]['status'],'unfinished')

    def test_absent_database_is_not_created(self):
        path=self.path.with_name('absent.db')
        self.assertEqual(read_work_context(path)['reason'],'history_not_recorded')
        self.assertFalse(path.exists())

    def test_invalid_references_and_cancellation_fail_structurally(self):
        for reference in ('x.anything','t.123','e.-1'):
            with self.assertRaises(ValueError):self.read(related_ref=reference)
        class Cancelled(Exception):pass
        def checkpoint():raise Cancelled()
        with self.assertRaises(Cancelled):self.read(checkpoint=checkpoint)

    def test_invalid_observation_rolls_back_without_fake_task(self):
        with self.assertRaises(ValueError):
            self.operation(path='relative.txt')
        self.assertEqual(self.db._conn.execute('SELECT COUNT(*) FROM work_tasks').fetchone()[0],0)

    def test_human_filename_phrase_matches_punctuation_through_native_sqlite_fts(self):
        self.operation(path=str(Path(self.directory.name)/'informe_lunar.txt'))
        self.assertEqual(self.read(query='informe lunar')['tasks'][0]['ref'],self.task)

    def test_expired_query_budget_is_partial_not_a_fake_empty_task_history(self):
        self.operation()
        with patch('trace_engine.work_context.time.monotonic',side_effect=[0,*([1]*100)]):
            value=self.read()
        self.assertEqual(value['reason'],'query_budget_exceeded')
        self.assertEqual(value['status'],'partial')

    def test_public_mcp_context_supports_the_same_work_lookup(self):
        import trace_engine.mcp.timeline_mcp_server as server
        self.operation()
        with patch.object(server,'_get_db',return_value=self.db):
            result=json.loads(server.call_timeline_tool('get_context',{'work':True,'time_hint':'intervalo',
                'start_time':'2026-10-23T00:00:00-03:00','end_time':'2026-10-24T00:00:00-03:00'}))
        self.assertEqual(result['context_kind'],'historical_work')
        self.assertEqual(result['tasks'][0]['ref'],self.task)

    def test_optional_writer_uses_normal_sync_only_in_wal_and_closes(self):
        from trace_engine.work_context import _connect
        db=_connect(self.path,write=True)
        try:
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'wal')
            self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0],1)
        finally:db.close()

    def test_failed_known_document_attempt_keeps_link_without_new_verification(self):
        link=self.operation()
        failed='t.'+uuid.uuid4().hex
        record_operation(self.path,task_ref=failed,ordinal=1,app_name='editor.exe',observation={
            'action':'read','status':'error','observed_at':self.then+1,'path':self.file,
            'historical_document_ref':link['document_ref']})
        value=self.read(query='informe.txt')
        task=next(t for t in value['tasks'] if t['ref']==failed)
        self.assertEqual(task['steps'][0]['document_ref'],link['document_ref'])
        self.assertEqual(task['steps'][0]['subject_kind'],'historical_candidate')
        self.assertEqual(value['documents'][0]['verified_at'],self.read(related_ref=self.task)['documents'][0]['verified_at'])


if __name__=='__main__':unittest.main()
