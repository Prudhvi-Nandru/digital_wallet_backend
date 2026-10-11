from pathlib import Path

def test_money_is_bigint_and_ledger_is_immutable():
    schema=Path('app/schema.sql').read_text().lower()
    assert 'balance_paise bigint' in schema
    assert 'amount_paise bigint' in schema
    assert 'before update or delete on ledger_entries' in schema

def test_required_routes_exist():
    src=Path('app/main.py').read_text()
    for route in ['/users','/users/{user_id}/wallets','/wallets/{wallet_id}/balance','/wallets/{wallet_id}/statement','/transfers/topup','/transfers/withdraw','/transfers/{transfer_id}/reverse','/admin/reconciliation/run','/admin/reconciliation/runs/{run_id}','/admin/risk/queue','/admin/risk/{transfer_id}/decision','/users/{user_id}/statement/ask']:
        assert route in src

def test_idempotency_hash_and_locking_implemented():
    logic=Path('app/logic.py').read_text()
    assert 'hashlib.sha256' in logic
    assert 'FOR UPDATE' in logic
    assert 'ORDER BY id FOR UPDATE' in logic
