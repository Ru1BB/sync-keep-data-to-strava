import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import app as application


class CalendarRangeTests(unittest.TestCase):
    def test_china_day_boundary(self):
        now = datetime(2026, 10, 13, 6, tzinfo=timezone.utc)
        self.assertEqual(application._history_cutoff(now), '2026-08-12T16:00:00+00:00')

    def test_short_month_and_year_boundary(self):
        cases = [
            (datetime(2026, 4, 30, tzinfo=timezone.utc), '2026-02-27T16:00:00+00:00'),
            (datetime(2026, 1, 31, tzinfo=timezone.utc), '2025-11-29T16:00:00+00:00'),
            (datetime(2024, 4, 30, tzinfo=timezone.utc), '2024-02-28T16:00:00+00:00'),
        ]
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(application._history_cutoff(now), expected)


class HistoryWorkflowTests(unittest.TestCase):
    cutoff = '2026-08-12T16:00:00+00:00'

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        directory = Path(self.directory.name)
        self.paths = patch.multiple(application, DATA_DIR=directory, DB_PATH=directory / 'test.sqlite3', _db_initialized=False)
        self.paths.start()
        self.secret = patch.dict(application.app.config, {'SECRET_KEY': 'isolated-history-tests'})
        self.secret.start()
        self.range = patch.object(application, '_history_cutoff', return_value=self.cutoff)
        self.range.start()
        self.token = patch.object(application, '_token', return_value='synthetic-token')
        self.token.start()
        application._init_db()
        application._db_initialized = True
        application._set_setting('watermark', '2026-10-13T00:00:00+00:00')
        self.client = application.app.test_client()
        with self.client.session_transaction() as session:
            session['logged_in'] = True
        self.samples = {
            'boundary': self.cutoff,
            'recent': '2026-09-20T06:00:00+00:00',
            'too-old': '2026-08-12T15:59:59+00:00',
            'successful': '2026-08-25T06:00:00+00:00',
        }
        with application._db() as conn:
            conn.execute(
                "INSERT INTO activities(source_key,keep_id,keep_sport,sport_type,name,started_at,distance,duration,preview_json,state,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'success',?)",
                ('running:successful', 'successful', 'running', 'Run', 'Run', self.samples['successful'], 1000, 300, '{}', application._now_iso()),
            )

    def tearDown(self):
        self.token.stop()
        self.range.stop()
        self.secret.stop()
        self.paths.stop()
        self.directory.cleanup()

    def records(self, token, sport, cutoff):
        if sport != 'running':
            return []
        return [
            {'raw': {'id': name, 'doneDate': int(datetime.fromisoformat(stamp).timestamp() * 1000)}, 'sport': 'running'}
            for name, stamp in self.samples.items()
        ]

    def detail(self, item, token):
        name = item['raw']['id']
        return {
            'source_key': 'running:' + name, 'keep_id': name, 'keep_sport': 'running',
            'sport_type': 'Run', 'name': 'Run', 'started_at': self.samples[name],
            'distance': 1000, 'duration': 300, 'calories': None, 'detail': {},
        }

    def check(self):
        with patch.object(application, '_keep_records', side_effect=self.records), patch.object(application, '_record_from_keep', side_effect=self.detail):
            return self.client.post('/api/check', json={})

    def test_history_before_activation_is_previewed_and_can_be_selected(self):
        response = self.check()
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['history_since'], self.cutoff)
        self.assertEqual({row['source_key'] for row in payload['activities']}, {'running:boundary', 'running:recent'})
        self.assertEqual(application._get_setting('watermark'), '2026-10-13T00:00:00+00:00')
        with patch.object(application.threading, 'Thread') as thread:
            result = self.client.post('/api/sync', json={'activity_ids': ['running:boundary']})
            self.assertEqual(result.status_code, 202)
            thread.return_value.start.assert_called_once()
        with application._db() as conn:
            row = conn.execute("SELECT preview_json FROM activities WHERE source_key='running:boundary'").fetchone()
            self.assertEqual(json.loads(row['preview_json']), {'full_id': 'boundary'})

    def test_successful_records_are_not_reuploaded(self):
        self.check()
        with patch.object(application.threading, 'Thread') as thread:
            response = self.client.post('/api/sync', json={'activity_ids': ['running:successful']})
            self.assertEqual(response.status_code, 409)
            thread.assert_not_called()

    def test_detail_failures_are_reported(self):
        with patch.object(application, '_keep_records', side_effect=self.records), patch.object(application, '_record_from_keep', side_effect=ValueError('synthetic failure')):
            response = self.client.post('/api/check', json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['warnings'])

    def test_pagination_reaches_the_requested_history_boundary(self):
        boundary_ms = int(datetime.fromisoformat(self.cutoff).timestamp() * 1000)
        pages = [
            {'data': {'records': [{'logs': [{'stats': {'id': 'recent'}}]}], 'lastTimestamp': boundary_ms + 1000}},
            {'data': {'records': [{'logs': [{'stats': {'id': 'boundary'}}]}], 'lastTimestamp': boundary_ms - 1}},
        ]
        def response(*args, **kwargs):
            from unittest.mock import Mock
            result = Mock(ok=True)
            result.json.return_value = pages.pop(0)
            return result
        with patch.object(application.requests, 'get', side_effect=response) as get, patch.object(application.time, 'sleep'):
            records = application._keep_records('synthetic-token', 'running', self.cutoff)
        self.assertEqual(get.call_count, 2)
        self.assertEqual([item['raw']['id'] for item in records], ['recent', 'boundary'])


if __name__ == '__main__':
    unittest.main()
