"""Extractive session capsules preserve provenance, privacy and raw access."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from trace_engine.session_context import read_session_context
from trace_engine.retrieval import read_context
from trace_engine.storage.timeline_db import TimelineDB


class SessionContextTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'timeline.db'
        self.clock=datetime(2026,10,24,12,tzinfo=timezone(timedelta(hours=-3)))
        self.ts=self.clock.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()-3600
        self.db=TimelineDB(str(self.path));self.addCleanup(self.db.close)

    def read(self,**kw):
        return read_session_context(self.path,time_window='ayer',now=self.clock,**kw)

    def event(self,text,offset=1,source='ocr',app='editor.exe',title='Proyecto Sol'):
        self.db.insert(source,app,title,text,self.ts+offset)

    def window(self,app='editor.exe',title='Proyecto Sol',offset=0,end=100):
        identifier=self.db.start_window_session(app,title,self.ts+offset)
        if end is not None:self.db.end_window_session(identifier,self.ts+end)
        return identifier

    def test_session_expansion_preserves_facts_without_requiring_each_to_match_topic(self):
        self.window();self.event('Proyecto Sol aprobado.');self.event('Presupuesto: 840.',2)
        value=self.read(query='Proyecto Sol',max_bytes=4000)
        self.assertEqual(value['context_kind'],'historical_sessions')
        self.assertEqual(value['sessions'][0]['event_count'],2)
        self.assertEqual({e['text'] for e in value['events']},{'Proyecto Sol aprobado.','Presupuesto: 840.'})
        self.assertFalse(value['current_screen'])

    def test_dedup_is_exact_complete_text_only_with_count_and_last_observed_time(self):
        self.window()
        for n in range(14):self.event('18 archivos. Código ORION-318.',n+1)
        value=self.read(max_bytes=4000)
        self.assertEqual(value['sessions'][0]['event_count'],14)
        self.assertEqual(len(value['events']),1)
        self.assertEqual(value['events'][0]['repeat_count'],14)
        self.assertNotEqual(value['events'][0]['observed_at'],value['events'][0]['last_observed_at'])

    def test_identical_prefixes_of_long_distinct_records_are_not_deduplicated(self):
        self.window();self.event('A'*400+' FINAL-UNO.');self.event('A'*400+' FINAL-DOS.',2)
        value=self.read(max_bytes=4000)
        self.assertEqual(len(value['events']),2)
        self.assertEqual({e['tail']['text'][-10:] for e in value['events']},{'FINAL-UNO.','FINAL-DOS.'})

    def test_tail_is_an_exact_suffix_even_after_byte_trimming(self):
        self.window();text='経過情報。'*200+' 最終コード：星-928。';self.event(text)
        row=self.read(max_bytes=1400)['events'][0]
        tail=row['tail']
        self.assertEqual(text[tail['offset']:],tail['text'])
        self.assertTrue(row['truncated'])
        self.assertIn('星-928',tail['text'])
        raw=read_context(self.path,time_window='ayer',now=self.clock,event_id=row['event_id'],offset=tail['offset'])
        self.assertEqual(raw['events'][0]['text'],tail['text'])

    def test_focus_retrieves_the_middle_with_exact_unicode_character_offset(self):
        self.window();text='説明。'*160+'予約番号：星-642。'+'補足。'*160;self.event(text)
        row=self.read(focus='予約番号',max_bytes=1400)['events'][0]
        self.assertGreater(row['offset'],0)
        self.assertTrue(row['text'].startswith('予約番号：星-642。'))
        self.assertEqual(text[row['offset']:row['offset']+len(row['text'])],row['text'])

    def test_time_source_and_privacy_filters_also_apply_to_expanded_session(self):
        self.window();self.event('VISIBLE');self.event('SECRET CLIP',2,source='clipboard')
        self.event('PRIVATE',3,app='PRIVATE.EXE')
        self.event('TODAY',86401)
        value=self.read(sources=['ocr'],excluded_apps=['private.exe'],max_bytes=4000)
        self.assertEqual([e['text'] for e in value['events']],['VISIBLE'])
        self.assertEqual(value['sessions'][0]['event_count'],1)

    def test_separate_sessions_never_collapse_into_one_record(self):
        self.window(offset=0,end=10);self.event('Aviso idéntico.',1)
        self.window(offset=20,end=30);self.event('Aviso idéntico.',21)
        value=self.read(max_bytes=4000)
        self.assertEqual(len(value['sessions']),2)
        self.assertEqual(len(value['events']),2)

    def test_old_abandoned_session_does_not_claim_later_observations(self):
        self.window(offset=-3600,end=None);self.event('Dato posterior.')
        row=self.read()['sessions'][0]
        self.assertTrue(row['ref'].startswith('e.'))
        self.assertFalse(row['end_confirmed'])
        self.assertIsNone(row['observed_end'])

    def test_abandoned_session_stops_at_next_foreground_session_without_merging(self):
        old=self.window(offset=0,end=None);self.event('Primer dato.',1)
        new=self.window(offset=20,end=40);self.event('Otro dato.',21)
        value=self.read(max_bytes=4000)
        counts={s['ref']:s['event_count'] for s in value['sessions']}
        self.assertEqual(counts,{'w.'+str(old):1,'w.'+str(new):1})
        self.assertEqual(len(value['events']),2)

    def test_missing_database_does_not_create_one(self):
        missing=Path(self.folder.name)/'missing.db'
        result=read_session_context(missing)
        self.assertEqual(result['reason'],'history_not_recorded')
        self.assertFalse(missing.exists())

    def test_escaped_unicode_and_control_characters_obey_serialized_byte_budget(self):
        self.window(title='\x00\"日'*80)
        for n in range(8):self.event('\x00\"\n日'*200,offset=n+1,title='\x00\"日'*80)
        for budget in (1400,1900,4000):
            with self.subTest(budget=budget):
                value=self.read(max_bytes=budget)
                self.assertLessEqual(len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()),budget)

    def test_retention_and_audit_clear_leave_no_cached_session_text(self):
        self.window();self.event('RETENIDO')
        self.assertTrue(self.read()['events'])
        self.db.clear_all_records()
        self.assertFalse(self.read()['events'])
        self.assertEqual(self.read()['reason'],'no_matching_sessions')

    def test_cancellation_propagates(self):
        self.window();self.event('Dato')
        def cancel():raise InterruptedError('cancelled')
        with self.assertRaises(InterruptedError):self.read(checkpoint=cancel)

    def test_deadline_returns_honest_partial_not_false_empty(self):
        self.window();self.event('Dato')
        with patch('trace_engine.session_context.time.monotonic',side_effect=[0,100,100,100]):
            result=self.read(budget_ms=20)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['reason'],'query_budget_exceeded')

    def test_more_sessions_have_a_query_scoped_continuation(self):
        for n in range(5):
            self.window(offset=n*120,end=n*120+60);self.event('Sondeo '+str(n),offset=n*120+1)
        value=self.read(max_bytes=4000)
        self.assertTrue(value['partial'])
        self.assertIn('next_cursor',value)
        next_page=self.read(max_bytes=4000,cursor=value['next_cursor'])
        self.assertTrue(next_page['events'])
        with self.assertRaises(ValueError):self.read(query='otro',cursor=value['next_cursor'])
        with self.assertRaises(ValueError):read_context(self.path,time_window='ayer',now=self.clock,cursor=value['next_cursor'])

    def test_invalid_filters_are_rejected(self):
        for arguments in ({'query':'x'*161},{'sources':[]},{'sources':['screen']},{'focus':42}):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):self.read(**arguments)

    def test_mcp_exposes_the_same_synthesis_with_raw_continuation_still_available(self):
        from trace_engine.mcp import timeline_mcp_server as server
        self.window();self.event('Clave: LUNA-927.')
        args={'time_hint':'intervalo','start_time':'2026-10-23T00:00:00-03:00',
              'end_time':'2026-10-24T00:00:00-03:00','synthesis':True,'query':'LUNA'}
        with patch.object(server,'_get_db',return_value=self.db):
            value=json.loads(server.call_timeline_tool('get_context',args))
        self.assertEqual(value['context_kind'],'historical_sessions')
        self.assertEqual(value['events'][0]['text'],'Clave: LUNA-927.')

    def test_linked_work_reuses_actual_refs_without_claiming_observation_contents(self):
        from trace_engine.work_context import record_operation,finish_task
        self.window();self.event('Aviso LUNA-482.')
        identifier=self.db._conn.execute('SELECT id FROM timeline').fetchone()[0]
        task='t.'+uuid.uuid4().hex
        record_operation(self.path,task_ref=task,ordinal=1,event_ids=[identifier],app_name='editor.exe',
            observation={'action':'read','status':'completed','path':'C:/Notas/luna.txt','directory':False,
                         'after':[1,2,10,4],'observed_at':self.ts+5})
        finish_task(self.path,task,'completed',now=self.ts+6)
        value=self.read(max_bytes=4000)
        self.assertEqual(value['events'][0]['work_refs'],[task])
        self.assertEqual(value['tasks'][0]['ref'],task)
        self.assertTrue(value['documents'][0]['requires_filesystem_check'])
        self.assertNotIn('content',value['documents'][0])

    def test_cursor_freezes_day_for_inline_task_links_across_midnight(self):
        from trace_engine.work_context import record_operation,finish_task
        for n in range(5):
            self.window(offset=n*120,end=n*120+60)
            self.event('Dato '+str(n),offset=n*120+1)
        task='t.'+uuid.uuid4().hex
        event=self.db._conn.execute('SELECT MIN(id) FROM timeline').fetchone()[0]
        record_operation(self.path,task_ref=task,ordinal=1,event_ids=[event],app_name='editor.exe',
            observation={'action':'read','status':'completed','path':'C:/Notas/luna.txt','directory':False,
                         'after':[1,2,10,4],'observed_at':self.ts+5})
        finish_task(self.path,task,'completed',now=self.ts+6)
        clock=self.clock.replace(hour=23,minute=50)
        first=read_session_context(self.path,time_window='ayer',now=clock,max_bytes=4000)
        second=read_session_context(self.path,time_window='ayer',now=clock+timedelta(minutes=20),
                                    cursor=first['next_cursor'],max_bytes=4000)
        self.assertEqual(second['tasks'][0]['ref'],task)


if __name__=='__main__':unittest.main()
