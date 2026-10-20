import threading

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_account(client, name, balance):
    r = client.post("/accounts", json={"name": name, "initial_balance_paise": balance})
    assert r.status_code == 201
    return r.json()["id"]


def balance(client, account_id):
    return client.get(f"/accounts/{account_id}").json()["balance_paise"]


def test_transfer_moves_money_and_writes_ledger(client):
    a, b = make_account(client, "A", 1000), make_account(client, "B", 0)
    r = client.post("/transfers", headers={"Idempotency-Key": "key-00000001"},
                    json={"from_account": a, "to_account": b, "amount_paise": 400})
    assert r.status_code == 200 and r.json()["replayed"] is False
    assert (balance(client, a), balance(client, b)) == (600, 400)
    assert client.get(f"/accounts/{a}/ledger").json()[0]["delta_paise"] == -400
    assert client.get(f"/accounts/{b}/ledger").json()[0]["delta_paise"] == 400


def test_same_idempotency_key_does_not_double_charge(client):
    a, b = make_account(client, "A", 1000), make_account(client, "B", 0)
    payload = {"from_account": a, "to_account": b, "amount_paise": 300}
    h = {"Idempotency-Key": "key-00000002"}
    first = client.post("/transfers", headers=h, json=payload)
    second = client.post("/transfers", headers=h, json=payload)
    assert first.json()["transfer_id"] == second.json()["transfer_id"]
    assert second.json()["replayed"] is True
    assert balance(client, a) == 700


def test_reusing_key_with_different_request_is_rejected(client):
    a, b = make_account(client, "A", 1000), make_account(client, "B", 0)
    h = {"Idempotency-Key": "key-00000003"}
    client.post("/transfers", headers=h, json={"from_account": a, "to_account": b, "amount_paise": 100})
    r = client.post("/transfers", headers=h, json={"from_account": a, "to_account": b, "amount_paise": 200})
    assert r.status_code == 409


def test_insufficient_funds_changes_nothing(client):
    a, b = make_account(client, "A", 100), make_account(client, "B", 0)
    r = client.post("/transfers", headers={"Idempotency-Key": "key-00000004"},
                    json={"from_account": a, "to_account": b, "amount_paise": 500})
    assert r.status_code == 422
    assert (balance(client, a), balance(client, b)) == (100, 0)


def test_validation_errors(client):
    a = make_account(client, "A", 100)
    h = {"Idempotency-Key": "key-00000005"}
    assert client.post("/transfers", headers=h, json={"from_account": a, "to_account": a, "amount_paise": 10}).status_code == 422
    assert client.post("/transfers", headers=h, json={"from_account": a, "to_account": 999, "amount_paise": -5}).status_code == 422
    assert client.post("/transfers", json={"from_account": a, "to_account": 999, "amount_paise": 5}).status_code == 422
    assert client.post("/transfers", headers=h, json={"from_account": a, "to_account": 999, "amount_paise": 5}).status_code == 404


def test_concurrent_transfers_never_overspend(client):
    """20 threads each try to send 100 from an account holding 1000: exactly 10 may succeed."""
    a, b = make_account(client, "A", 1000), make_account(client, "B", 0)
    results = []

    def worker(i):
        with TestClient(app) as c:
            r = c.post("/transfers", headers={"Idempotency-Key": f"concurrent-key-{i:04d}"},
                       json={"from_account": a, "to_account": b, "amount_paise": 100})
            results.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert results.count(200) == 10
    assert results.count(422) == 10
    assert (balance(client, a), balance(client, b)) == (0, 1000)
