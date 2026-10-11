import os, random
from datetime import datetime, timezone
import psycopg
from psycopg.rows import tuple_row
URL=os.getenv('DATABASE_URL','postgresql://wallet:wallet@localhost:5432/wallet')
random.seed(42)
with psycopg.connect(URL,row_factory=tuple_row) as conn:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts(name,account_type,balance_paise) VALUES('Cash-in account','cash_in',0),('Cash-out account','cash_out',0),('Fee revenue account','fee_revenue',0) ON CONFLICT DO NOTHING")
        cur.execute("SELECT id FROM accounts WHERE account_type='cash_in' AND user_id IS NULL ORDER BY id LIMIT 1"); cashin=cur.fetchone()[0]
        cur.execute("SELECT id FROM accounts WHERE account_type='cash_out' AND user_id IS NULL ORDER BY id LIMIT 1"); cashout=cur.fetchone()[0]
        cur.execute('SELECT count(*) FROM users'); count=cur.fetchone()[0]
        if count<1000:
            for i in range(count,1000): cur.execute('INSERT INTO users(name,email,phone,kyc_status) VALUES(%s,%s,%s,\'verified\') ON CONFLICT DO NOTHING',(f'Seed User {i+1}',f'seed{i+1}@example.test',f'+91{7000000000+i}'))
        cur.execute('SELECT id FROM users ORDER BY id LIMIT 1000'); users=[r[0] for r in cur.fetchall()]
        for u in users:
            cur.execute("SELECT count(*) FROM accounts WHERE user_id=%s AND account_type='wallet'",(u,)); n=cur.fetchone()[0]
            for j in range(n,2): cur.execute("INSERT INTO accounts(user_id,name,account_type) VALUES(%s,%s,'wallet')",(u,f'Wallet {j+1}'))
        cur.execute("SELECT id,user_id FROM accounts WHERE account_type='wallet' ORDER BY id LIMIT 2000"); wallets=cur.fetchall()
        if len(wallets)<2: raise RuntimeError('Could not seed wallets')
        wallet_ids=[r[0] for r in wallets]
        # Initial seed funds are balanced top-ups, one transfer and two entries per wallet.
        for aid in wallet_ids:
            cur.execute("SELECT 1 FROM ledger_entries l JOIN transfers t ON t.id=l.transfer_id WHERE l.account_id=%s LIMIT 1",(aid,))
            if not cur.fetchone():
                cur.execute("INSERT INTO transfers(transfer_type,status,amount_paise,source_account_id,destination_account_id,note) VALUES('topup','completed',100000,%s,%s,'seed opening balance') RETURNING id",(cashin,aid)); tid=cur.fetchone()[0]
                cur.execute("INSERT INTO ledger_entries(transfer_id,account_id,direction,amount_paise) VALUES(%s,%s,'debit',100000),(%s,%s,'credit',100000)",(tid,cashin,tid,aid)); cur.execute('UPDATE accounts SET balance_paise=balance_paise+100000 WHERE id=%s',(aid,))
        for i in range(100000):
            src,dst=random.sample(wallet_ids,2); amount=random.randint(1,5000)
            cur.execute('SELECT balance_paise FROM accounts WHERE id=%s FOR UPDATE',(src,)); bal=cur.fetchone()[0]
            if bal<amount: continue
            cur.execute("INSERT INTO transfers(transfer_type,status,amount_paise,source_account_id,destination_account_id,note) VALUES('peer','completed',%s,%s,%s,'seed transfer') RETURNING id",(amount,src,dst)); tid=cur.fetchone()[0]
            cur.execute("INSERT INTO ledger_entries(transfer_id,account_id,direction,amount_paise) VALUES(%s,%s,'debit',%s),(%s,%s,'credit',%s)",(tid,src,amount,tid,dst,amount))
            cur.execute('UPDATE accounts SET balance_paise=balance_paise-%s WHERE id=%s',(amount,src)); cur.execute('UPDATE accounts SET balance_paise=balance_paise+%s WHERE id=%s',(amount,dst))
    conn.commit()
print('Seed complete: up to 1,000 users, 2,000 wallets and 100,000 transfers.')
