import concurrent.futures
import tempfile
import unittest
from pathlib import Path

try:
    from billing import Billing, Conflict, Provider
except ImportError:
    Billing = None


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Billing, 'Billing service is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local = Path(self.tmp.name) / 'local.db'
        self.provider = Provider(Path(self.tmp.name) / 'provider.db')
        self.app = Billing(self.local, self.provider)

    def test_replay_is_durable(self):
        first = self.app.subscribe('request-1', 'alice', 1500)
        self.assertEqual(first, Billing(self.local, self.provider).subscribe('request-1', 'alice', 1500))
        self.app.drain()
        self.assertEqual(self.provider.charge_count(), 1)
        self.assertTrue(self.app.entitled('alice'))

    def test_payload_conflict(self):
        self.app.subscribe('same', 'alice', 1500)
        with self.assertRaises(Conflict):
            self.app.subscribe('same', 'alice', 1600)

    def test_concurrent_retries(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.app.subscribe('same', 'alice', 1500), range(24)))
        self.assertEqual(len({r['subscription_id'] for r in results}), 1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.app.drain(), range(4)))
        self.assertEqual(self.provider.charge_count(), 1)

    def test_crash_after_provider_commit(self):
        self.app.subscribe('crash', 'alice', 1500)
        with self.assertRaises(RuntimeError):
            self.app.drain(crash_after_provider=True)
        self.assertEqual(self.provider.charge_count(), 1)
        Billing(self.local, Provider(self.provider.path)).drain()
        self.assertEqual(self.provider.charge_count(), 1)
        self.assertTrue(self.app.entitled('alice'))

    def test_out_of_order_events_and_duplicates(self):
        result = self.app.subscribe('events', 'alice', 1500)
        self.app.drain()
        sid = result['subscription_id']
        newer = self.provider.set_status(sid, 'canceled')
        self.assertTrue(self.app.receive(newer))
        self.assertFalse(self.app.receive(newer))
        self.assertFalse(self.app.receive({'event_id': 'old', 'subscription_id': sid, 'version': 1, 'status': 'active'}))
        self.assertFalse(self.app.entitled('alice'))

    def test_dropped_event_repaired_by_snapshot(self):
        result = self.app.subscribe('drop', 'alice', 1500)
        self.app.drain()
        self.provider.set_status(result['subscription_id'], 'canceled')
        self.assertTrue(self.app.entitled('alice'))
        self.app.reconcile()
        self.assertFalse(self.app.entitled('alice'))

    def test_retry_preserves_pending_outbox(self):
        self.app.subscribe('retry', 'alice', 1500)
        self.provider.available = False
        self.assertEqual(self.app.drain(), 0)
        self.provider.available = True
        self.assertEqual(self.app.drain(), 1)
        self.assertTrue(self.app.entitled('alice'))

    def test_local_charge_recorded_atomically_after_recovery(self):
        result = self.app.subscribe('charge', 'alice', 1500)
        with self.assertRaises(RuntimeError):
            self.app.drain(crash_after_provider=True)
        self.assertEqual(self.app.charges(result['subscription_id']), [])
        self.app.drain()
        records = self.app.charges(result['subscription_id'])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['amount'], 1500)
        self.app.drain()
        self.assertEqual(self.app.charges(result['subscription_id']), records)

    def test_usage_late_arrival_duplicates_and_conflicts(self):
        sid = self.app.subscribe('usage', 'alice', 1500)['subscription_id']
        self.app.drain()
        late = dict(event_id='u1', subscription_id=sid, sequence=1, units=3)
        newer = dict(event_id='u2', subscription_id=sid, sequence=2, units=7)
        self.assertTrue(self.app.record_usage(newer))
        self.assertTrue(self.app.record_usage(late))
        self.assertFalse(self.app.record_usage(newer))
        self.assertEqual(Billing(self.local, self.provider).usage_total(sid), 10)
        with self.assertRaises(Conflict):
            self.app.record_usage(dict(newer, units=9))
        self.assertEqual(self.app.usage_total(sid), 10)
        for invalid in [dict(late, event_id='bad', units=-1), dict(late, units=True), dict(late, sequence=0), dict(late, subscription_id='missing')]:
            with self.assertRaises(ValueError):
                self.app.record_usage(invalid)
        self.assertTrue(self.app.entitled('alice'))

    def test_invalid_inputs(self):
        for args in [('', 'alice', 100), ('x', '', 100), ('x', 'alice', -1), ('x', 'alice', True), ('x', 'alice', 1.5)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.app.subscribe(*args)
        with self.assertRaises(ValueError):
            self.app.receive({'event_id': 'bad'})


if __name__ == '__main__':
    unittest.main()
