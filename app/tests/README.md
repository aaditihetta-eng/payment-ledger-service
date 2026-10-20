# Payment Ledger API

A small backend service that models how a payments system moves money safely:
accounts, deposits, atomic transfers, idempotent retries and an auditable ledger.
Built with Python, FastAPI and SQLite.

## Why this project

Payment systems must never lose, duplicate or invent money, even when clients retry
requests or many requests arrive at once. This project focuses on those guarantees.

## Design decisions

| Problem | How it is handled |
| --- | --- |
| Float rounding errors | Amounts are stored as integer paise, never floats |
| Partial failures | Debit, credit, transfer record and ledger rows are written in one database transaction |
| Two requests spending the same balance | `BEGIN IMMEDIATE` takes the write lock before the balance is read, so concurrent transfers are serialised |
| Client retries after a timeout | Required `Idempotency-Key` header: the same key and request replays the stored result instead of charging twice |
| Key reused for a different request | Rejected with `409 Conflict` |
| Overdrafts | Checked inside the transaction; the database also has `CHECK (balance_paise >= 0)` |
| Auditability | Every balance change has a `ledger_entries` row |

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/accounts` | Create an account (optional initial balance) |
| GET | `/accounts/{id}` | Get balance |
| POST | `/accounts/{id}/deposit` | Add money |
| POST | `/transfers` | Transfer money (needs `Idempotency-Key` header) |
| GET | `/accounts/{id}/ledger` | Recent ledger entries |

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Interactive docs: http://127.0.0.1:8000/docs

Example transfer:

```bash
curl -X POST http://127.0.0.1:8000/transfers \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: order-12345-attempt-1" \
  -d '{"from_account": 1, "to_account": 2, "amount_paise": 5000}'
```

## Tests

```bash
pytest -q
```

The suite covers correct transfers, idempotent replays, key misuse, insufficient funds,
validation errors, and a concurrency test where 20 threads try to spend the same balance
and exactly the affordable number succeed.

## Known limitations and next steps

- SQLite allows one writer at a time. A production version would use PostgreSQL with row-level locking (`SELECT ... FOR UPDATE`) and a consistent lock order to avoid deadlocks.
- Failed transfers (for example insufficient funds) are not stored, so a retry with the same key is evaluated again.
- No authentication, rate limiting or currency support.
- No reversal or refund flow yet.
