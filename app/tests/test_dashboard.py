import importlib.util
from pathlib import Path
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

source = Path(__file__).parents[1] / 'streamlit_app.py'
spec = importlib.util.spec_from_file_location('dashboard', source)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
        self.data = app.seed_demo(self.now)

    def test_seed_has_three_distinct_decisions(self):
        self.assertEqual({r['decision'] for r in self.data['receipts']}, {'fix','allow','escalate'})
        self.assertTrue(all(r['is_demo'] for r in self.data['receipts']))
        self.assertEqual(len({r['run_id'] for r in self.data['runs']}), 3)

    def test_queue_advances_and_does_not_duplicate_on_refresh(self):
        request = app.queue_demo(self.data, 'demo/guardian', 104, 'Fix + retry', self.now)
        self.assertEqual(request['status'], 'queued')
        app.tick_demo(self.data, self.now + timedelta(seconds=18))
        run = self.data['runs'][-1]
        self.assertFalse(run['finished'])
        self.assertEqual(self.data['events'][-1]['step'], 'tests_failed')
        app.tick_demo(self.data, self.now + timedelta(seconds=40))
        count = len(self.data['events'])
        app.tick_demo(self.data, self.now + timedelta(seconds=80))
        self.assertEqual(len(self.data['events']), count)
        self.assertEqual(len([r for r in self.data['receipts'] if r['run_id']==request['run_id']]), 1)
        self.assertEqual(request['status'], 'completed')

    def test_repeated_active_pr_rejected_and_new_run_after_completion(self):
        first = app.queue_demo(self.data, 'demo/guardian', 104, 'Allow decoy', self.now)
        with self.assertRaises(ValueError):
            app.queue_demo(self.data, 'demo/guardian', 104, 'Escalate', self.now)
        app.tick_demo(self.data, self.now + timedelta(seconds=20))
        second = app.queue_demo(self.data, 'demo/guardian', 104, 'Escalate', self.now + timedelta(seconds=21))
        self.assertNotEqual(first['run_id'], second['run_id'])

    def test_failed_run_never_creates_success_receipt(self):
        request = app.queue_demo(self.data, 'demo/guardian', 105, 'Failed run', self.now)
        app.tick_demo(self.data, self.now + timedelta(seconds=40))
        self.assertEqual(request['status'], 'failed')
        self.assertFalse(any(r['run_id']==request['run_id'] for r in self.data['receipts']))

    def test_invalid_identifiers_and_links_rejected(self):
        for repo in ['https://github.com/demo/repo', 'demo/repo;DROP', '<script>/x']:
            with self.assertRaises(ValueError): app.validate_pr(repo, 1)
        for prefix in ['DB.APP;DROP DATABASE X', 'DB.APP.EXTRA', 'DB-FOO.APP']:
            with self.assertRaises(ValueError): app.qualified_prefix(prefix)
        self.assertIsNone(app.safe_receipt_url('javascript:alert(1)'))
        self.assertIsNone(app.safe_receipt_url('https://github.com.evil.test/a/b/pull/1'))
        self.assertTrue(app.safe_receipt_url('https://github.com/demo/repo/pull/1#issuecomment-2'))

    def test_receipt_search_and_decision_filter(self):
        found = app.filter_receipts(self.data['receipts'], 'fixture', ['allow'])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['pr_number'], 102)
        self.assertEqual(app.filter_receipts(self.data['receipts'], 'not-present'), [])

    def test_clock_before_start_does_not_advance(self):
        app.queue_demo(self.data, 'demo/guardian', 104, 'Fix + retry', self.now)
        app.tick_demo(self.data, self.now - timedelta(seconds=1))
        self.assertEqual(self.data['runs'][-1]['next_event'], 0)

    def test_team_adapter_accepts_updated_guide_columns(self):
        class Row:
            def __init__(self, data): self.data = data
            def as_dict(self): return self.data
        class Session:
            def sql(self, sql):
                self.table = 'events' if '.PR_EVENTS ' in sql else 'receipts' if '.RECEIPTS ' in sql else 'verdicts'
                return self
            def collect(self):
                if self.table == 'events':
                    return [Row({'PR_NUMBER':pr,'STEP':'tests_passed','STATUS':'ok','DETAIL':'test','TS':stamp})
                            for pr,stamp in [(2,'2026-10-25T18:01:00+00:00'),(1,'2026-10-25T18:00:00+00:00')]]
                if self.table == 'verdicts':
                    return [Row({'PR_NUMBER':2,'DECISION':'fix','CONFIDENCE':0.98,'REASON':'log leak'})]
                return [Row({'PR_NUMBER':1,'DECISION':'allow','REASON':'fixture','CREATED_AT':'2026-10-25T18:00:01+00:00','MERGED':True})]
        with patch.object(app, 'get_session', return_value=Session()):
            data = app.read_team(None, 'DEMO.APP')
        states = {r['run_id']:r['state'] for r in data['runs']}
        self.assertEqual(states, {'pr-1':'completed','pr-2':'running'})
        self.assertEqual(data['runs'][0]['decision'], 'allow')
        self.assertEqual(data['runs'][1]['confidence'], 0.98)

    def test_manual_button_only_inserts_guide_event(self):
        sql = app.manual_event_sql('guardian.app')
        self.assertIn('GUARDIAN.APP.PR_EVENTS (PR_NUMBER,STEP,STATUS,DETAIL,TS)', sql)
        self.assertIn("'manual run requested','running'", sql)
        self.assertNotIn('waiting', sql)
        self.assertNotIn('MANUAL_REQUESTS', sql)
        self.assertEqual(sql.count('?'), 2)
        with self.assertRaises(ValueError): app.manual_event_sql('x.y;DROP')

    def test_fake_script_has_three_decisions_and_demo_markers(self):
        import sys
        sys.path.insert(0, str(source.parent))
        from fake_data import build_sql
        sql = build_sql()
        self.assertEqual(sql.count('.RECEIPTS (PR_NUMBER'), 3)
        self.assertIn('[DEMO]', sql)
        self.assertIn('WHERE NOT EXISTS', sql)
        self.assertNotIn('CREATE TABLE', sql)
        self.assertNotIn('DELETE ', sql)
        self.assertIn('CREATE TABLE', build_sql(create_demo_tables=True))
        with self.assertRaises(ValueError): build_sql('PRODUCTION.APP', True)


if __name__ == '__main__':
    unittest.main()
