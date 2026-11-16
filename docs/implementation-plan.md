# Implementation plan

1. Write failure-oriented service tests before implementation: retries, concurrency, payload conflict, post-provider crash, out-of-order events, dropped-event repair, outages, invalid data.
2. Implement two independent SQLite stores, transactional request/outbox creation, provider deduplication, event inbox, and entitlement projection.
3. Add a failing HTTP contract test; implement the loopback API and explicit worker/reconciler CLI.
4. Run the complete suite and deterministic crash/recovery demo. Document schema, guarantees, and operational limits; add Python 3.11/3.12 CI.

Verification record: the initial eight tests failed because the service was absent. After the core implementation all eight passed. The HTTP contract test then failed because the adapter was absent. The completed suite passes eleven tests, including a real ephemeral loopback HTTP server. Demo assertions verify one charge across retry/restart and repair of a dropped cancellation.

5. Add local charge receipts and usage accounting. New tests first demonstrated absent APIs, then verified atomic crash recovery, late usage, replay/conflict protection, invalid deltas, and durable totals.
