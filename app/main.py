"""Mini payment ledger API.

Features: accounts, deposits, atomic transfers, idempotent retries, ledger history.
"""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, model_validator

from app import db


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Payment Ledger API", lifespan=lifespan)


def get_conn():
    conn = db.get_conn()
    try:
        yield conn
    finally:
        conn.close()


# ---------- request models ----------
class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    initial_balance_paise: int = Field(default=0, ge=0)


class Deposit(BaseModel):
    amount_paise: int = Field(gt=0)


class TransferCreate(BaseModel):
    from_account: int
    to_account: int
    amount_paise: int = Field(gt=0)

    @model_validator(mode="after")
    def accounts_differ(self):
        if self.from_account == self.to_account:
            raise ValueError("from_account and to_account must be different")
        return self


# ---------- helpers ----------
def _get_account(conn, account_id: int):
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"account {account_id} not found")
    return row


def _transfer_json(row, replayed: bool) -> dict:
    return {
        "transfer_id": row["id"],
        "from_account": row["from_account"],
        "to_account": row["to_account"],
        "amount_paise": row["amount_paise"],
        "replayed": replayed,
    }


# ---------- endpoints ----------
@app.post("/accounts", status_code=201)
def create_account(body: AccountCreate, conn=Depends(get_conn)):
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO accounts (name, balance_paise) VALUES (?, ?)",
            (body.name, body.initial_balance_paise),
        )
        account_id = cur.lastrowid
        if body.initial_balance_paise > 0:
            conn.execute(
                "INSERT INTO ledger_entries (account_id, delta_paise, kind) VALUES (?, ?, 'deposit')",
                (account_id, body.initial_balance_paise),
            )
    return {"id": account_id, "name": body.name, "balance_paise": body.initial_balance_paise}


@app.get("/accounts/{account_id}")
def get_account(account_id: int, conn=Depends(get_conn)):
    row = _get_account(conn, account_id)
    return {"id": row["id"], "name": row["name"], "balance_paise": row["balance_paise"]}


@app.post("/accounts/{account_id}/deposit")
def deposit(account_id: int, body: Deposit, conn=Depends(get_conn)):
    with db.transaction(conn):
        _get_account(conn, account_id)
        conn.execute(
            "UPDATE accounts SET balance_paise = balance_paise + ? WHERE id = ?",
            (body.amount_paise, account_id),
        )
        conn.execute(
            "INSERT INTO ledger_entries (account_id, delta_paise, kind) VALUES (?, ?, 'deposit')",
            (account_id, body.amount_paise),
        )
        balance = conn.execute(
            "SELECT balance_paise FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()["balance_paise"]
    return {"id": account_id, "balance_paise": balance}


@app.post("/transfers")
def create_transfer(
    body: TransferCreate,
    idempotency_key: str = Header(..., min_length=8, max_length=128),
    conn=Depends(get_conn),
):
    """Move money atomically. Retrying with the same Idempotency-Key never double-charges."""
    with db.transaction(conn):
        existing = conn.execute(
            "SELECT * FROM transfers WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            same_request = (
                existing["from_account"] == body.from_account
                and existing["to_account"] == body.to_account
                and existing["amount_paise"] == body.amount_paise
            )
            if not same_request:
                raise HTTPException(409, "Idempotency-Key was already used with a different request")
            return _transfer_json(existing, replayed=True)

        sender = _get_account(conn, body.from_account)
        _get_account(conn, body.to_account)
        if sender["balance_paise"] < body.amount_paise:
            raise HTTPException(422, "insufficient funds")

        cur = conn.execute(
            "INSERT INTO transfers (idempotency_key, from_account, to_account, amount_paise) "
            "VALUES (?, ?, ?, ?)",
            (idempotency_key, body.from_account, body.to_account, body.amount_paise),
        )
        transfer_id = cur.lastrowid
        conn.execute(
            "UPDATE accounts SET balance_paise = balance_paise - ? WHERE id = ?",
            (body.amount_paise, body.from_account),
        )
        conn.execute(
            "UPDATE accounts SET balance_paise = balance_paise + ? WHERE id = ?",
            (body.amount_paise, body.to_account),
        )
        conn.executemany(
            "INSERT INTO ledger_entries (account_id, transfer_id, delta_paise, kind) VALUES (?, ?, ?, ?)",
            [
                (body.from_account, transfer_id, -body.amount_paise, "transfer_out"),
                (body.to_account, transfer_id, body.amount_paise, "transfer_in"),
            ],
        )
        row = conn.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
    return _transfer_json(row, replayed=False)


@app.get("/accounts/{account_id}/ledger")
def ledger(account_id: int, limit: int = 50, conn=Depends(get_conn)):
    _get_account(conn, account_id)
    rows = conn.execute(
        "SELECT id, transfer_id, delta_paise, kind, created_at FROM ledger_entries "
        "WHERE account_id = ? ORDER BY id DESC LIMIT ?",
        (account_id, max(1, min(limit, 200))),
    ).fetchall()
    return [dict(r) for r in rows]
