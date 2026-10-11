# Digital Wallet Backend

FastAPI + PostgreSQL 15 backend for INR wallet transfers. Amounts are integer paise (`BIGINT`), and every completed transfer creates immutable double-entry ledger rows. The money path uses visible SQL and row locks rather than ORM abstractions.

## Run

```bash
cp .env.example .env
docker compose up --build
```

- Swagger UI: http://localhost:8000/docs
- Health: http://localhost:8000/health
- PostgreSQL: localhost:5432 (`wallet` / `wallet` for local development only)
- The worker runs reconciliation on its configured interval (default 24 hours).

Schema is initialized from `app/schema.sql` on a fresh database volume. To reset local data, use `docker compose down -v` and then start again. Do not use sample credentials or header-based identity in production.

## Authentication note

For a runnable assignment scaffold, routes use `X-User-Id` and admin routes use `X-Admin: true`. These are **development-only identity stubs, not authentication**. Replace with verified JWT/OIDC claims and role-based authorization before deployment. Admin actions currently do not establish a real operator identity; production must record the authenticated admin principal.

## API overview

All POST requests require `Idempotency-Key: <UUID>`. Amounts are paise.

| Method | Path | Purpose |
|---|---|---|
| POST | `/users` | Create user |
| POST | `/users/{user_id}/wallets` | Open wallet |
| GET | `/wallets/{wallet_id}/balance` | Read balance |
| GET | `/wallets/{wallet_id}/statement?limit=50&offset=0` | Paginated entries newest-first |
| POST | `/transfers/topup` | Cash-in to wallet |
| POST | `/transfers` | Wallet-to-wallet transfer |
| POST | `/transfers/withdraw` | Wallet to cash-out account |
| POST | `/transfers/{transfer_id}/reverse` | Append a reversal transfer |
| GET | `/transfers/{transfer_id}` | Transfer, ledger entries, status history |
| POST | `/admin/reconciliation/run` | Run reconciliation (202) |
| GET | `/admin/reconciliation/runs/{run_id}` | Report and issues |
| GET | `/admin/risk/queue` | Held/flagged queue |
| POST | `/admin/risk/{transfer_id}/decision` | Release/reject a held transfer |
| POST | `/users/{user_id}/statement/ask` | Statement question endpoint |

Example:
```bash
curl -X POST http://localhost:8000/users \
 -H 'Content-Type: application/json' -H 'Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000' \
 -d '{"name":"Asha","email":"asha@example.com","phone":"+919999999999","kyc_status":"verified"}'
```
For a user's wallet calls, pass `-H 'X-User-Id: 1'`. For admin endpoints, pass `-H 'X-Admin: true'`.

## Correctness design

- READ COMMITTED plus account-row `FOR UPDATE` locks ordered by account ID. The balance check, transfer row, ledger entries and balance updates are in one transaction.
- Idempotency uses SHA-256 over canonical JSON, a UUID key, a unique index scoped to user/admin-null scope, and persisted response code/body. Replays return the stored response; mismatched body gets 422.
- Ledger rows have a PostgreSQL trigger forbidding updates/deletes.
- Suspicious peer transfers are held before posting any ledger rows; a flagged-but-allowed transfer posts money and leaves a risk flag.
- Reconciliation checks per-transfer balance, stored account balances, overdrafts, completed transfers without entries and global ledger net. It records issues and does not repair data. A database lock row excludes overlapping runs.
- Statement Q&A uses a deterministic parser and parameterized ledger query; it does not let a model produce SQL. The starter supports monthly sent/received/largest/count summaries. It is not yet a full LLM-based date-range/counterparty parser.

## Fraud rules
The `risk_rules` table contains configurable thresholds. Defaults mirror the supplied brief: velocity over 5 transfers in 10 minutes (hold), amount over 5x 30-day average (hold), and first transfer to a new payee above ₹10,000 (flag and allow). ₹10,000 = 1,000,000 paise.

## Tests and seed data

```bash
pytest -q
# After the API/database are running:
docker compose exec api python scripts/seed.py
```
The seed script targets 1,000 users, 2,000 wallets and 100,000 transfers, with balanced ledger rows. It can take a while. The included fast tests validate schema/API contracts; a real PostgreSQL integration test suite is still needed to prove the 100-request stampede, exact concurrent idempotency replay, corruption acceptance test, and repeat reconciliation 20 times in CI. Run those before treating this as production-ready.

## Design trade-offs / next steps

- Stored balances improve reads but duplicate ledger-derived state; nightly reconciliation detects drift.
- Row locks serialize activity on hot accounts; use bounded retry/backoff for transient deadlocks/serialization errors.
- OFFSET pagination is simple but expensive for large offsets; keyset pagination on `(created_at,id)` is the next optimization.
- Production hardening: real auth, rate limits, request-size limits, structured logs/metrics, secret management, admin identity, external LLM adapter with mock tests, migration runner/version table, and full concurrency/integration CI.
- Sharding/consistent hashing is intentionally stretch scope, not implemented.

See `docs/schema.md` and `docs/isolation.md` for design and isolation exercises.
