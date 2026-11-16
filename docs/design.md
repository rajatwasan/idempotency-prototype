# Design: billing at an unreliable boundary

## Invariants

1. A request key names exactly one canonical `(customer, amount)` payload and immutable acceptance response.
2. Subscription creation, entitlement initialization, request receipt, and charge intent commit together.
3. Every retry uses the subscription UUID as the provider operation ID. Provider payment state lives in a separate database and commits independently.
4. A local worker acknowledges its outbox only in the transaction that applies the provider result.
5. Entitlements follow the highest applied provider version, regardless of delivery order.
6. Every accepted event ID identifies one payload; payload substitution raises a conflict.

## Data model

- `requests(key PK, payload, response)` — durable API deduplication ledger.
- `subscriptions(id PK, customer, amount, status, version)` — local aggregate.
- `entitlements(subscription_id PK/FK, enabled)` — access projection.
- `outbox(id PK, subscription_id FK, amount, done, attempts, last_error)` — charge intent and retry diagnostics.
- `charges(provider_operation PK, subscription_id UNIQUE/FK, amount)` — local provider receipt, committed with acknowledgement.
- `usage_events(event_id PK, subscription_id FK, sequence, units, payload)` — additive accounting; late sequences are accepted.
- `inbox(event_id PK, payload)` — event deduplication and conflict detection.
- Provider `payments(operation PK, subscription_id UNIQUE, amount, status, version)` — external effects and authoritative snapshot.

SQLite `BEGIN IMMEDIATE` serializes local mutations and protects read/check/write sequences. Connections are short-lived and independent per operation, with a 30-second busy timeout. External calls never hold a local lock. Two workers may read the same pending row; the provider ledger prevents duplicate effects and the local conditional completion update counts only the first acknowledgement.

## Recovery

The explicit crash hook occurs after the provider commits and before any local acknowledgement. Reopening both databases reproduces a restart. Outbox replay obtains the same provider operation and applies its latest state. Provider outages retain pending rows. Reconciliation feeds authoritative current snapshots into the same inbox/version gate used by events, repairing missing intermediate deliveries without replaying every event.

## Scope decisions

A local CLI worker makes retries inspectable and avoids background timing in tests. The provider is a fake external boundary rather than an in-process dictionary. SQLite is appropriate for an executable single-host prototype. Production extensions and contract limitations are documented in the README rather than implied by the demo.

Usage deltas are independent of state versions. Sequence is metadata, never a stale-event gate. Durable usage totals are derived from the ledger; provider status governs access. Missing usage needs upstream replay, while subscription snapshots repair state events.
