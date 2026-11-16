"""Run a complete fault/recovery scenario without leaving local state behind."""
import tempfile
from pathlib import Path
from billing import Billing, Provider


def main():
    with tempfile.TemporaryDirectory() as folder:
        provider = Provider(Path(folder) / 'provider.db')
        app = Billing(Path(folder) / 'billing.db', provider)
        first = app.subscribe('checkout-42', 'alice', 1500)
        assert first == app.subscribe('checkout-42', 'alice', 1500)
        print('1. Duplicate checkout returns the original subscription.')
        try:
            app.drain(crash_after_provider=True)
        except RuntimeError:
            print('2. Simulated crash: provider committed; local outbox remains pending.')
        app = Billing(app.path, Provider(provider.path))
        app.drain()
        assert provider.charge_count() == 1 and app.entitled('alice')
        print('3. Restart + retry: exactly one provider charge; entitlement enabled.')
        provider.set_status(first['subscription_id'], 'canceled')
        assert app.entitled('alice')
        print('4. Cancellation webhook dropped: local entitlement is temporarily stale.')
        app.reconcile()
        assert not app.entitled('alice')
        print('5. Snapshot reconciliation repairs entitlement.')
        sid = first['subscription_id']
        newer = dict(event_id='usage-2', subscription_id=sid, sequence=2, units=7)
        older = dict(event_id='usage-1', subscription_id=sid, sequence=1, units=3)
        app.record_usage(newer)
        app.record_usage(older)
        app.record_usage(newer)
        assert app.usage_total(sid) == 10 and len(app.charges(sid)) == 1
        print('6. Out-of-order usage + duplicate: total 10 units; one local charge. All assertions passed.')


if __name__ == '__main__':
    main()
