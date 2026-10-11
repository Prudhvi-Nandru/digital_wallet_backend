import hashlib, json, uuid
from datetime import date
from psycopg.types.json import Jsonb
from fastapi import HTTPException

SYSTEM = {}
def init_system(conn):
    with conn.cursor() as cur:
        for name, typ in [('Cash-in account','cash_in'),('Cash-out account','cash_out'),('Fee revenue account','fee_revenue')]:
            cur.execute("SELECT id FROM accounts WHERE account_type=%s AND user_id IS NULL ORDER BY id LIMIT 1",(typ,)); row=cur.fetchone()
            if row: SYSTEM[typ]=row[0]
            else:
                cur.execute("INSERT INTO accounts(user_id,name,account_type) VALUES(NULL,%s,%s) RETURNING id",(name,typ)); SYSTEM[typ]=cur.fetchone()[0]

def idempotent(conn, user_id, key, body, operation):
    if not key: raise HTTPException(400, detail={'code':'IDEMPOTENCY_KEY_REQUIRED','message':'Idempotency-Key header is required','request_id':str(uuid.uuid4())})
    try: key_uuid=uuid.UUID(key)
    except ValueError: raise HTTPException(400, detail={'code':'INVALID_IDEMPOTENCY_KEY','message':'Idempotency-Key must be a UUID','request_id':str(uuid.uuid4())})
    raw=json.dumps(body,sort_keys=True,separators=(',',':')).encode(); digest=hashlib.sha256(raw).hexdigest()
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute('INSERT INTO idempotency_keys(user_id,key,request_hash) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id',(user_id,key_uuid,digest)); fresh=cur.fetchone()
            if not fresh:
                cur.execute('SELECT request_hash,status,response_code,response_body FROM idempotency_keys WHERE user_id IS NOT DISTINCT FROM %s AND key=%s FOR UPDATE',(user_id,key_uuid)); old=cur.fetchone()
                if old[0] != digest: raise HTTPException(422,detail={'code':'IDEMPOTENCY_KEY_REUSED','message':'Key was already used with a different request body','request_id':str(uuid.uuid4())})
                if old[1]=='running': raise HTTPException(409,detail={'code':'REQUEST_IN_PROGRESS','message':'Retry shortly','request_id':str(uuid.uuid4())})
                return old[2], old[3]
            code, response=operation(conn)
            with conn.cursor() as cur: cur.execute("UPDATE idempotency_keys SET status='completed',response_code=%s,response_body=%s WHERE id=%s",(code,Jsonb(response),fresh[0]))
            return code,response

def locked_accounts(cur, ids):
    ids=sorted(set(int(i) for i in ids))
    cur.execute('SELECT id,balance_paise,account_type,user_id FROM accounts WHERE id = ANY(%s) ORDER BY id FOR UPDATE',(ids,))
    rows={r[0]:r for r in cur.fetchall()}
    if len(rows)!=len(ids): raise HTTPException(404,detail={'code':'ACCOUNT_NOT_FOUND','message':'Account not found','request_id':str(uuid.uuid4())})
    return rows

def risk_check(cur, source_id, destination_id, amount, user_id):
    fired=[]
    cur.execute("SELECT threshold,window_seconds,action FROM risk_rules WHERE rule_key='velocity' AND enabled")
    r=cur.fetchone()
    if r:
        cur.execute("SELECT count(*) FROM transfers WHERE source_account_id=%s AND created_at>now()-(%s * interval '1 second') AND status IN ('completed','held','flagged')",(source_id,r[1]))
        if cur.fetchone()[0] >= r[0]: fired.append(('velocity',r[2]))
    cur.execute("SELECT threshold,action FROM risk_rules WHERE rule_key='amount_spike' AND enabled")
    r=cur.fetchone()
    if r:
        cur.execute("SELECT COALESCE(avg(amount_paise),0) FROM transfers WHERE source_account_id=%s AND created_at>=now()-interval '30 days' AND status='completed'",(source_id,)); avg=cur.fetchone()[0]
        if avg and amount > r[0]*avg: fired.append(('amount_spike',r[1]))
    if destination_id:
        cur.execute("SELECT threshold,action FROM risk_rules WHERE rule_key='new_payee_large' AND enabled")
        r=cur.fetchone()
        if r and amount>r[0]:
            cur.execute("SELECT 1 FROM transfers WHERE source_account_id=%s AND destination_account_id=%s AND status='completed' LIMIT 1",(source_id,destination_id))
            if not cur.fetchone(): fired.append(('new_payee_large',r[1]))
    return fired

def create_transfer(conn, *, user_id, source_id, destination_id, amount, transfer_type, note='', reversal_of=None):
    if amount <= 0:
        raise HTTPException(422, detail={'code': 'INVALID_AMOUNT', 'message': 'amount_paise must be positive', 'request_id': str(uuid.uuid4())})
    if source_id == destination_id:
        raise HTTPException(422, detail={'code': 'SELF_TRANSFER_NOT_ALLOWED', 'message': 'source and destination accounts must differ', 'request_id': str(uuid.uuid4())})

    with conn.cursor() as cur:
        accounts = locked_accounts(cur, [source_id, destination_id])
        source = accounts[source_id]
        dest = accounts[destination_id]
        if source[2] == 'wallet' and source[3] != user_id:
            raise HTTPException(403, detail={'code': 'FORBIDDEN', 'message': 'Source wallet does not belong to caller', 'request_id': str(uuid.uuid4())})
        if source[2] == 'wallet' and source[1] < amount:
            raise HTTPException(422, detail={'code': 'INSUFFICIENT_FUNDS', 'message': 'Wallet balance is insufficient', 'request_id': str(uuid.uuid4())})

        fired = risk_check(cur, source_id, destination_id, amount, user_id) if transfer_type == 'peer' else []
        status = 'held' if any(action == 'hold' for _, action in fired) else ('flagged' if fired else 'completed')

        cur.execute(
            'INSERT INTO transfers(transfer_type,status,amount_paise,source_account_id,destination_account_id,created_by,note,reversal_of) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,created_at',
            (transfer_type, status, amount, source_id, destination_id, user_id, note, reversal_of),
        )
        tid, created = cur.fetchone()

        if status != 'held':
            cur.execute(
                "INSERT INTO ledger_entries(transfer_id,account_id,direction,amount_paise) VALUES(%s,%s,'debit',%s),(%s,%s,'credit',%s)",
                (tid, source_id, amount, tid, destination_id, amount),
            )
            cur.execute('UPDATE accounts SET balance_paise=balance_paise-%s WHERE id=%s', (amount, source_id))
            cur.execute('UPDATE accounts SET balance_paise=balance_paise+%s WHERE id=%s', (amount, destination_id))

        cur.execute(
            "INSERT INTO transfer_status_audit(transfer_id,old_status,new_status,changed_by,reason) VALUES(%s,NULL,%s,%s,%s)",
            (tid, status, user_id, 'risk rules: ' + ','.join(rule for rule, _ in fired) if fired else 'created'),
        )
        for rule, _action in fired:
            cur.execute(
                "INSERT INTO risk_flags(transfer_id,rule_key,explanation,label) VALUES(%s,%s,%s,'review') ON CONFLICT DO NOTHING",
                (tid, rule, 'Rule fired: ' + rule),
            )

        return (201 if status == 'completed' else 202), {
            'transfer_id': tid,
            'status': status,
            'amount_paise': amount,
            'created_at': created.isoformat(),
            'risk_rules': [rule for rule, _ in fired],
        }

def reconcile(conn):
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM job_locks WHERE job_name='reconciliation' FOR UPDATE NOWAIT")
            cur.execute("INSERT INTO reconciliation_runs(started_at,status) VALUES(now(),'running') RETURNING id"); run_id=cur.fetchone()[0]
            issues=[]
            checks=[('unbalanced_transfer',"SELECT transfer_id, sum(CASE WHEN direction='debit' THEN amount_paise ELSE -amount_paise END) FROM ledger_entries GROUP BY transfer_id HAVING sum(CASE WHEN direction='debit' THEN amount_paise ELSE -amount_paise END)<>0"),('balance_mismatch',"SELECT a.id, a.balance_paise, COALESCE(sum(CASE WHEN l.direction='credit' THEN l.amount_paise ELSE -l.amount_paise END),0) FROM accounts a LEFT JOIN ledger_entries l ON l.account_id=a.id GROUP BY a.id HAVING a.balance_paise<>COALESCE(sum(CASE WHEN l.direction='credit' THEN l.amount_paise ELSE -l.amount_paise END),0)"),('overdraft',"SELECT a.id,0,COALESCE(sum(CASE WHEN l.direction='credit' THEN l.amount_paise ELSE -l.amount_paise END),0) FROM accounts a LEFT JOIN ledger_entries l ON l.account_id=a.id WHERE a.account_type='wallet' GROUP BY a.id HAVING COALESCE(sum(CASE WHEN l.direction='credit' THEN l.amount_paise ELSE -l.amount_paise END),0)<0"),('transfer_without_entries',"SELECT t.id,1,0 FROM transfers t LEFT JOIN ledger_entries l ON l.transfer_id=t.id WHERE t.status IN ('completed','flagged','reversed') GROUP BY t.id HAVING count(l.id)=0")]
            counts={}
            for name,sql in checks:
                cur.execute(sql); rows=cur.fetchall(); counts[name]=len(rows)
                for row in rows: issues.append((name,row[0],str(row[1]),str(row[2])))
            cur.execute("SELECT COALESCE(sum(CASE WHEN direction='debit' THEN amount_paise ELSE -amount_paise END),0) FROM ledger_entries"); net=cur.fetchone()[0]
            if net!=0: issues.append(('global_ledger_balance',None,'0',str(net)))
            for issue in issues: cur.execute('INSERT INTO reconciliation_issues(run_id,check_name,object_id,expected,actual) VALUES(%s,%s,%s,%s,%s)',(run_id,*issue))
            cur.execute("UPDATE reconciliation_runs SET ended_at=now(),status=%s,issue_count=%s,checks=%s WHERE id=%s",('passed' if not issues else 'issues_found',len(issues),Jsonb(counts),run_id))
            return {'run_id':run_id,'status':'passed' if not issues else 'issues_found','issue_count':len(issues),'checks':counts}
