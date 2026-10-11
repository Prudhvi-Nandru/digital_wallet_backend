# PostgreSQL isolation lab

Run `docker compose exec db psql -U wallet -d wallet` in two terminals. These are reproducible commands; capture actual output from your local PostgreSQL run before treating the lab as empirically completed.

## Read Committed
Session A: `BEGIN ISOLATION LEVEL READ COMMITTED; SELECT balance_paise FROM accounts WHERE id=1;`
Session B: `UPDATE accounts SET balance_paise=balance_paise+1 WHERE id=1; COMMIT;`
Session A: repeat the SELECT. It sees B's committed value because each statement receives a fresh snapshot. Then `ROLLBACK;`.

## Repeatable Read
Session A: `BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT balance_paise FROM accounts WHERE id=1;`
Session B updates and commits as above. Session A repeats the SELECT and sees its original snapshot. `ROLLBACK;`.

## Serializable write skew
Create two rows representing a shared invariant; in each session begin SERIALIZABLE, read both rows, and update a different row based on the read. Commit one session first; the other may fail with SQLSTATE `40001` serialization_failure and must be retried as a whole transaction.

## Repeatable Read with join
Session A begins REPEATABLE READ and selects account joined to ledger entries. Session B commits a change. Repeating the join in A returns the same snapshot for both tables; transaction-level snapshot applies to joins too.

## Transfer isolation choice
Transfers use PostgreSQL READ COMMITTED plus `SELECT ... FOR UPDATE` on both accounts in ascending ID order. The balance is re-read under lock; ledger entries and balance updates share one transaction. This serializes spending from the same wallet without the extra serialization failures/retries of SERIALIZABLE. Serializable is viable but costs throughput under contention and requires retrying SQLSTATE 40001.
