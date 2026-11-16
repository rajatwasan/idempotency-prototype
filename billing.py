"""Durable billing boundary: stdlib only, two independent SQLite databases."""
import json
import sqlite3
import uuid
from contextlib import contextmanager


class Conflict(ValueError):
    """An idempotency key was reused for a different operation."""


@contextmanager
def transaction(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def identifier(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 200


class Provider:
    """Fake external service with its own durable idempotency ledger."""
    def __init__(self, path):
        self.path = path
        self.available = True
        with transaction(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS payments (operation TEXT PRIMARY KEY, subscription_id TEXT UNIQUE, amount INTEGER, status TEXT, version INTEGER)')

    def charge(self, operation, sid, amount):
        if not self.available:
            raise ConnectionError('provider unavailable')
        with transaction(self.path) as db:
            row = db.execute('SELECT * FROM payments WHERE operation=?', (operation,)).fetchone()
            if row:
                if row['subscription_id'] != sid or row['amount'] != amount:
                    raise Conflict('provider operation payload mismatch')
            else:
                db.execute('INSERT INTO payments VALUES (?,?,?, ?,1)', (operation, sid, amount, 'active'))
            row = db.execute('SELECT * FROM payments WHERE operation=?', (operation,)).fetchone()
            return self.event(row)

    @staticmethod
    def event(row):
        return dict(event_id=f"{row['subscription_id']}:{row['version']}", subscription_id=row['subscription_id'], status=row['status'], version=row['version'])

    def set_status(self, sid, status):
        if status not in ('active', 'canceled'):
            raise ValueError('unsupported status')
        with transaction(self.path) as db:
            if not db.execute('SELECT 1 FROM payments WHERE subscription_id=?', (sid,)).fetchone():
                raise ValueError('unknown subscription')
            db.execute('UPDATE payments SET status=?, version=version+1 WHERE subscription_id=?', (status, sid))
            return self.event(db.execute('SELECT * FROM payments WHERE subscription_id=?', (sid,)).fetchone())

    def snapshots(self):
        with transaction(self.path) as db:
            return [self.event(row) for row in db.execute('SELECT * FROM payments')]

    def charge_count(self):
        with transaction(self.path) as db:
            return db.execute('SELECT count(*) FROM payments').fetchone()[0]


class Billing:
    def __init__(self, path, provider):
        self.path, self.provider = path, provider
        with transaction(path) as db:
            for sql in [
                'CREATE TABLE IF NOT EXISTS subscriptions (id TEXT PRIMARY KEY, customer TEXT NOT NULL, amount INTEGER NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL)',
                'CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, payload TEXT NOT NULL, response TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, subscription_id TEXT REFERENCES subscriptions(id), amount INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT)',
                'CREATE TABLE IF NOT EXISTS charges (provider_operation TEXT PRIMARY KEY, subscription_id TEXT UNIQUE REFERENCES subscriptions(id), amount INTEGER NOT NULL)',
                'CREATE TABLE IF NOT EXISTS usage_events (event_id TEXT PRIMARY KEY, subscription_id TEXT REFERENCES subscriptions(id), sequence INTEGER NOT NULL, units INTEGER NOT NULL, payload TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS inbox (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS entitlements (subscription_id TEXT PRIMARY KEY REFERENCES subscriptions(id), enabled INTEGER NOT NULL)',
            ]:
                db.execute(sql)

    def subscribe(self, key, customer, amount):
        if not identifier(key) or not identifier(customer) or type(amount) is not int or not 0 < amount <= 100000000:
            raise ValueError('key/customer must be nonempty strings <=200 characters; amount must be integer cents in 1..100000000')
        payload = json.dumps([customer, amount], separators=(',', ':'))
        with transaction(self.path) as db:
            row = db.execute('SELECT * FROM requests WHERE key=?', (key,)).fetchone()
            if row:
                if row['payload'] != payload:
                    raise Conflict('idempotency key already used with different payload')
                return json.loads(row['response'])
            sid = str(uuid.uuid4())
            response = dict(subscription_id=sid, status='pending')
            db.execute('INSERT INTO subscriptions VALUES (?,?,?, ?,0)', (sid, customer, amount, 'pending'))
            db.execute('INSERT INTO entitlements VALUES (?,0)', (sid,))
            db.execute('INSERT INTO requests VALUES (?,?,?)', (key, payload, json.dumps(response)))
            db.execute('INSERT INTO outbox (id,subscription_id,amount) VALUES (?,?,?)', (sid, sid, amount))
            return response

    def drain(self, crash_after_provider=False):
        with transaction(self.path) as db:
            work = list(db.execute('SELECT * FROM outbox WHERE done=0'))
        completed = 0
        for item in work:
            try:
                event = self.provider.charge(item['id'], item['subscription_id'], item['amount'])
            except ConnectionError as exc:
                with transaction(self.path) as db:
                    db.execute('UPDATE outbox SET attempts=attempts+1,last_error=? WHERE id=?', (str(exc), item['id']))
                continue
            if crash_after_provider:
                raise RuntimeError('simulated crash after provider commit, before local commit')
            with transaction(self.path) as db:
                self._receive(db, event)
                db.execute('INSERT OR IGNORE INTO charges VALUES (?,?,?)', (item['id'], item['subscription_id'], item['amount']))
                completed += db.execute('UPDATE outbox SET done=1,attempts=attempts+1,last_error=NULL WHERE id=? AND done=0', (item['id'],)).rowcount
        return completed

    @staticmethod
    def _receive(db, event):
        if not isinstance(event, dict) or not all(identifier(event.get(k)) for k in ('event_id', 'subscription_id')) or type(event.get('version')) is not int or event['version'] < 1 or event.get('status') not in ('active', 'canceled'):
            raise ValueError('invalid event')
        encoded = json.dumps(event, sort_keys=True)
        previous = db.execute('SELECT payload FROM inbox WHERE event_id=?', (event['event_id'],)).fetchone()
        if previous:
            if previous['payload'] != encoded:
                raise Conflict('event ID payload mismatch')
            return False
        row = db.execute('SELECT version FROM subscriptions WHERE id=?', (event['subscription_id'],)).fetchone()
        if row is None:
            raise ValueError('unknown subscription')
        db.execute('INSERT INTO inbox VALUES (?,?)', (event['event_id'], encoded))
        if event['version'] <= row['version']:
            return False
        db.execute('UPDATE subscriptions SET status=?,version=? WHERE id=?', (event['status'], event['version'], event['subscription_id']))
        db.execute('UPDATE entitlements SET enabled=? WHERE subscription_id=?', (event['status'] == 'active', event['subscription_id']))
        return True

    def receive(self, event):
        with transaction(self.path) as db:
            return self._receive(db, event)

    def reconcile(self):
        return sum(self.receive(event) for event in self.provider.snapshots())

    def entitled(self, customer):
        with transaction(self.path) as db:
            return bool(db.execute('SELECT 1 FROM entitlements e JOIN subscriptions s ON s.id=e.subscription_id WHERE s.customer=? AND e.enabled=1', (customer,)).fetchone())

    def charges(self, subscription_id):
        with transaction(self.path) as db:
            return [dict(row) for row in db.execute('SELECT * FROM charges WHERE subscription_id=?', (subscription_id,))]

    def record_usage(self, event):
        """Append independent deltas; sequence is metadata, never a stale-event gate."""
        if not isinstance(event, dict) or not all(identifier(event.get(k)) for k in ('event_id', 'subscription_id')) or type(event.get('sequence')) is not int or event['sequence'] < 1 or type(event.get('units')) is not int or not 0 < event['units'] <= 100000000:
            raise ValueError('invalid usage event')
        encoded = json.dumps(event, sort_keys=True)
        with transaction(self.path) as db:
            previous = db.execute('SELECT payload FROM usage_events WHERE event_id=?', (event['event_id'],)).fetchone()
            if previous:
                if previous['payload'] != encoded:
                    raise Conflict('usage event ID payload mismatch')
                return False
            if not db.execute('SELECT 1 FROM subscriptions WHERE id=?', (event['subscription_id'],)).fetchone():
                raise ValueError('unknown subscription')
            db.execute('INSERT INTO usage_events VALUES (?,?,?,?,?)', (event['event_id'], event['subscription_id'], event['sequence'], event['units'], encoded))
            return True

    def usage_total(self, subscription_id):
        with transaction(self.path) as db:
            return db.execute('SELECT COALESCE(SUM(units),0) FROM usage_events WHERE subscription_id=?', (subscription_id,)).fetchone()[0]
