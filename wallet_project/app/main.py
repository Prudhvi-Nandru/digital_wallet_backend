import uuid, os, json
from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
from .db import connection
from .logic import init_system, idempotent, create_transfer, SYSTEM, reconcile
from .llm import MockStatementParser

app=FastAPI(title='Digital Wallet API',version='1.0.0',description='Double-entry INR wallet backend')

class UserIn(BaseModel): name:str; email:str; phone:str; kyc_status:str='pending'
class WalletIn(BaseModel): name:str='Main wallet'
class TransferIn(BaseModel): source_wallet_id:int; destination_wallet_id:int; amount_paise:int=Field(gt=0); note:str=''
class TopupIn(BaseModel): wallet_id:int; amount_paise:int=Field(gt=0); note:str=''
class WithdrawIn(BaseModel): wallet_id:int; amount_paise:int=Field(gt=0); note:str=''
class DecisionIn(BaseModel): decision:str; reason:str=''
class ReverseIn(BaseModel): reason:str=''
class AskIn(BaseModel): question:str

def err(code,message,status=400): return HTTPException(status_code=status,detail={'code':code,'message':message,'request_id':str(uuid.uuid4())})
def user_id_header(x_user_id:Optional[int]):
    if not x_user_id: raise err('AUTH_REQUIRED','Provide X-User-Id header',401)
    return x_user_id
def ensure_user(conn, uid):
    with conn.cursor() as cur:
        cur.execute('SELECT 1 FROM users WHERE id=%s', (uid,))
        if not cur.fetchone():
            raise err('USER_NOT_FOUND', 'User not found', 404)
def post_result(result): return JSONResponse(status_code=result[0],content=result[1])

@app.on_event('startup')
def startup():
    with connection() as conn:
        with conn.transaction(): init_system(conn)

@app.get('/health')
def health():
    with connection() as conn:
        with conn.cursor() as cur: cur.execute('SELECT 1'); cur.fetchone()
    return {'status':'ok'}

@app.post('/users')
def create_user(payload:UserIn, idempotency_key:Optional[str]=Header(None,alias='Idempotency-Key')):
    body=payload.model_dump()
    with connection() as conn:
        def op(c):
            with c.cursor() as cur:
                if payload.kyc_status not in ('pending','verified','rejected'): raise err('INVALID_KYC_STATUS','Invalid KYC status',422)
                cur.execute('INSERT INTO users(name,email,phone,kyc_status) VALUES(%s,%s,%s,%s) RETURNING id,created_at',(payload.name,payload.email,payload.phone,payload.kyc_status)); row=cur.fetchone()
                return 201,{'user_id':row[0],'name':payload.name,'email':payload.email,'phone':payload.phone,'kyc_status':payload.kyc_status,'created_at':row[1].isoformat()}
        return post_result(idempotent(conn,None,idempotency_key,body,op))

@app.post('/users/{user_id}/wallets')
def open_wallet(user_id:int,payload:WalletIn,idempotency_key:Optional[str]=Header(None,alias='Idempotency-Key'),x_user_id:Optional[int]=Header(None)):
    uid=user_id_header(x_user_id)
    if uid!=user_id: raise err('FORBIDDEN','Cannot open wallet for another user',403)
    with connection() as conn:
        ensure_user(conn,uid)
        def op(c):
            with c.cursor() as cur:
                cur.execute("INSERT INTO accounts(user_id,name,account_type) VALUES(%s,%s,'wallet') RETURNING id,created_at",(uid,payload.name)); r=cur.fetchone()
                return 201,{'wallet_id':r[0],'name':payload.name,'currency':'INR','balance_paise':0,'created_at':r[1].isoformat()}
        return post_result(idempotent(conn,uid,idempotency_key,payload.model_dump(),op))

@app.get('/wallets/{wallet_id}/balance')
def balance(wallet_id:int,x_user_id:Optional[int]=Header(None)):
    uid=user_id_header(x_user_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,balance_paise,currency FROM accounts WHERE id=%s AND user_id=%s AND account_type='wallet'",(wallet_id,uid)); r=cur.fetchone()
            if not r: raise err('WALLET_NOT_FOUND','Wallet not found',404)
            return {'wallet_id':r[0],'balance_paise':r[1],'currency':r[2]}

@app.get('/wallets/{wallet_id}/statement')
def statement(wallet_id:int,limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0),x_user_id:Optional[int]=Header(None)):
    uid=user_id_header(x_user_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM accounts WHERE id=%s AND user_id=%s AND account_type='wallet'",(wallet_id,uid))
            if not cur.fetchone(): raise err('WALLET_NOT_FOUND','Wallet not found',404)
            cur.execute('SELECT l.id,l.transfer_id,l.direction,l.amount_paise,l.created_at,t.transfer_type,t.status,t.note FROM ledger_entries l JOIN transfers t ON t.id=l.transfer_id WHERE l.account_id=%s ORDER BY l.created_at DESC,l.id DESC LIMIT %s OFFSET %s',(wallet_id,limit,offset))
            rows=cur.fetchall()
            return {'wallet_id':wallet_id,'limit':limit,'offset':offset,'entries':[{'entry_id':r[0],'transfer_id':r[1],'direction':r[2],'amount_paise':r[3],'created_at':r[4].isoformat(),'transfer_type':r[5],'status':r[6],'note':r[7]} for r in rows]}

@app.post('/transfers/topup')
def topup(p:TopupIn,idempotency_key:Optional[str]=Header(None,alias='Idempotency-Key'),x_user_id:Optional[int]=Header(None)):
    uid=user_id_header(x_user_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM accounts WHERE id=%s AND user_id=%s AND account_type='wallet'",(p.wallet_id,uid))
            if not cur.fetchone(): raise err('WALLET_NOT_FOUND','Wallet not found',404)
        def op(c): return create_transfer(c,user_id=uid,source_id=SYSTEM['cash_in'],destination_id=p.wallet_id,amount=p.amount_paise,transfer_type='topup',note=p.note)
        return post_result(idempotent(conn,uid,idempotency_key,p.model_dump(),op))

@app.post('/transfers')
def transfer(p:TransferIn,idempotency_key:Optional[str]=Header(None,alias='Idempotency-Key'),x_user_id:Optional[int]=Header(None)):
    uid=user_id_header(x_user_id)
    with connection() as conn:
        def op(c):
            return create_transfer(c,user_id=uid,source_id=p.source_wallet_id,destination_id=p.destination_wallet_id,amount=p.amount_paise,transfer_type='peer',note=p.note)
        return post_result(idempotent(conn,uid,idempotency_key,p.model_dump(),op))

@app.post('/transfers/withdraw')
def withdraw(p:WithdrawIn,idempotency_key:Optional[str]=Header(None,alias='Idempotency-Key'),x_user_id:Optional[int]=Header(None)):
    uid=user_id_header(x_user_id)
    with connection() as conn:
        def op(c): return create_transfer(c,user_id=uid,source_id=p.wallet_id,destination_id=SYSTEM['cash_out'],amount=p.amount_paise,transfer_type='withdrawal',note=p.note)
        return post_result(idempotent(conn,uid,idempotency_key,p.model_dump(),op))

@app.get('/transfers/{transfer_id}')
def get_transfer(transfer_id:int,x_user_id:Optional[int]=Header(None)):
    uid=user_id_header(x_user_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT t.id,t.transfer_type,t.status,t.amount_paise,t.created_at,t.note,t.source_account_id,t.destination_account_id FROM transfers t WHERE t.id=%s AND (t.created_by=%s OR EXISTS(SELECT 1 FROM accounts a WHERE a.id IN (t.source_account_id,t.destination_account_id) AND a.user_id=%s))',(transfer_id,uid,uid)); t=cur.fetchone()
            if not t: raise err('TRANSFER_NOT_FOUND','Transfer not found',404)
            cur.execute('SELECT id,account_id,direction,amount_paise FROM ledger_entries WHERE transfer_id=%s ORDER BY id',(transfer_id,)); entries=cur.fetchall()
            cur.execute('SELECT old_status,new_status,changed_by,reason,created_at FROM transfer_status_audit WHERE transfer_id=%s ORDER BY id',(transfer_id,)); audit=cur.fetchall()
            return {'transfer_id':t[0],'type':t[1],'status':t[2],'amount_paise':t[3],'created_at':t[4].isoformat(),'note':t[5],'entries':[{'entry_id':r[0],'account_id':r[1],'direction':r[2],'amount_paise':r[3]} for r in entries],'status_history':[{'old_status':r[0],'new_status':r[1],'changed_by':r[2],'reason':r[3],'created_at':r[4].isoformat()} for r in audit]}

@app.post('/transfers/{transfer_id}/reverse')
def reverse_transfer(transfer_id:int,p:ReverseIn,idempotency_key:Optional[str]=Header(None,alias='Idempotency-Key'),x_user_id:Optional[int]=Header(None)):
    uid=user_id_header(x_user_id)
    with connection() as conn:
        def op(c):
            with c.cursor() as cur:
                cur.execute("SELECT source_account_id,destination_account_id,amount_paise,status,transfer_type FROM transfers WHERE id=%s FOR UPDATE",(transfer_id,)); r=cur.fetchone()
                if not r: raise err('TRANSFER_NOT_FOUND','Transfer not found',404)
                if r[3]!='completed': raise err('TRANSFER_NOT_REVERSIBLE','Only completed transfers can be reversed',409)
                cur.execute('SELECT 1 FROM transfers WHERE reversal_of=%s',(transfer_id,))
                if cur.fetchone(): raise err('ALREADY_REVERSED','Transfer already reversed',409)
                code,body=create_transfer(c,user_id=uid,source_id=r[1],destination_id=r[0],amount=r[2],transfer_type='reversal',note=p.reason,reversal_of=transfer_id)
                cur.execute("UPDATE transfers SET status='reversed' WHERE id=%s",(transfer_id,)); cur.execute("INSERT INTO transfer_status_audit(transfer_id,old_status,new_status,changed_by,reason) VALUES(%s,'completed','reversed',%s,%s)",(transfer_id,uid,p.reason))
                return code,body
        return post_result(idempotent(conn,uid,idempotency_key,{'transfer_id':transfer_id,**p.model_dump()},op))

@app.post('/admin/reconciliation/run')
def run_reconciliation(idempotency_key:Optional[str]=Header(None,alias='Idempotency-Key'),x_admin:Optional[str]=Header(None)):
    if x_admin!='true': raise err('ADMIN_REQUIRED','Set X-Admin: true',403)
    with connection() as conn:
        result=idempotent(conn,None,idempotency_key,{'action':'reconciliation'},lambda c:(202,reconcile(c)))
        return post_result(result)

@app.get('/admin/reconciliation/runs/{run_id}')
def reconciliation_report(run_id:int,x_admin:Optional[str]=Header(None)):
    if x_admin!='true': raise err('ADMIN_REQUIRED','Set X-Admin: true',403)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id,started_at,ended_at,status,issue_count,checks FROM reconciliation_runs WHERE id=%s',(run_id,)); r=cur.fetchone()
            if not r: raise err('RUN_NOT_FOUND','Reconciliation run not found',404)
            cur.execute('SELECT check_name,object_id,expected,actual FROM reconciliation_issues WHERE run_id=%s ORDER BY id',(run_id,)); issues=cur.fetchall()
            return {'run_id':r[0],'started_at':r[1].isoformat(),'ended_at':r[2].isoformat() if r[2] else None,'status':r[3],'issue_count':r[4],'checks':r[5],'issues':[{'check':x[0],'object_id':x[1],'expected':x[2],'actual':x[3]} for x in issues]}

@app.get('/admin/risk/queue')
def risk_queue(x_admin:Optional[str]=Header(None)):
    if x_admin!='true': raise err('ADMIN_REQUIRED','Set X-Admin: true',403)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT t.id,t.status,t.amount_paise,t.source_account_id,t.destination_account_id,t.created_at,COALESCE(string_agg(r.rule_key,','),'') FROM transfers t LEFT JOIN risk_flags r ON r.transfer_id=t.id WHERE t.status IN ('held','flagged') GROUP BY t.id ORDER BY t.created_at")
            return {'items':[{'transfer_id':r[0],'status':r[1],'amount_paise':r[2],'source_account_id':r[3],'destination_account_id':r[4],'created_at':r[5].isoformat(),'rules':r[6].split(',') if r[6] else []} for r in cur.fetchall()]}

@app.post('/admin/risk/{transfer_id}/decision')
def risk_decision(transfer_id:int,p:DecisionIn,idempotency_key:Optional[str]=Header(None,alias='Idempotency-Key'),x_admin:Optional[str]=Header(None)):
    if x_admin!='true': raise err('ADMIN_REQUIRED','Set X-Admin: true',403)
    if p.decision not in ('release','reverse'): raise err('INVALID_DECISION','decision must be release or reverse',422)
    with connection() as conn:
        def op(c):
            with c.cursor() as cur:
                cur.execute('SELECT status,source_account_id,destination_account_id,amount_paise,created_by FROM transfers WHERE id=%s FOR UPDATE',(transfer_id,)); r=cur.fetchone()
                if not r or r[0]!='held': raise err('NOT_HELD','Transfer is not held',409)
                if p.decision=='release':
                    rows=__import__('app.logic',fromlist=['locked_accounts']).locked_accounts(cur,[r[1],r[2]])
                    if rows[r[1]][2]=='wallet' and rows[r[1]][1]<r[3]: raise err('INSUFFICIENT_FUNDS','Source wallet no longer has funds',422)
                    cur.execute("INSERT INTO ledger_entries(transfer_id,account_id,direction,amount_paise) VALUES(%s,%s,'debit',%s),(%s,%s,'credit',%s)",(transfer_id,r[1],r[3],transfer_id,r[2],r[3]))
                    cur.execute('UPDATE accounts SET balance_paise=balance_paise-%s WHERE id=%s',(r[3],r[1])); cur.execute('UPDATE accounts SET balance_paise=balance_paise+%s WHERE id=%s',(r[3],r[2])); new='completed'
                else: new='rejected'
                cur.execute('UPDATE transfers SET status=%s WHERE id=%s',(new,transfer_id)); cur.execute('INSERT INTO transfer_status_audit(transfer_id,old_status,new_status,changed_by,reason) VALUES(%s,\'held\',%s,NULL,%s)',(transfer_id,new,p.reason))
                return 200,{'transfer_id':transfer_id,'status':new}
        return post_result(idempotent(conn,None,idempotency_key,{'transfer_id':transfer_id,**p.model_dump()},op))

@app.post('/users/{user_id}/statement/ask')
def statement_ask(user_id:int,p:AskIn,idempotency_key:Optional[str]=Header(None,alias='Idempotency-Key'),x_user_id:Optional[int]=Header(None)):
    uid=user_id_header(x_user_id)
    if uid!=user_id: raise err('FORBIDDEN','Can only ask about your own statement',403)
    with connection() as conn:
        def op(c):
            parser = MockStatementParser()
            spec = parser.parse_question_json(p.question)
            if spec.get('type') not in {1, 2, 3, 4}:
                return 200, {'answer': 'I can only answer total sent to a person, total received, largest transfer, or number of transfers in a date range.', 'supported': False, 'transfer_ids': []}

            intent = parser.parse_question(p.question)
            with c.cursor() as cur:
                cur.execute("SELECT id FROM accounts WHERE user_id=%s AND account_type='wallet'", (uid,))
                wallet_ids = [r[0] for r in cur.fetchall()]
                if not wallet_ids:
                    return 200, {'answer': 'No wallet data found.', 'supported': True, 'transfer_ids': []}

                clauses = [
                    "t.status IN ('completed','reversed')",
                    't.created_at >= %s',
                    't.created_at < %s',
                    '(t.source_account_id=ANY(%s) OR t.destination_account_id=ANY(%s))',
                ]
                params = [intent.start, intent.end_exclusive, wallet_ids, wallet_ids]

                if intent.kind == 'sent':
                    clauses.append('t.source_account_id=ANY(%s)')
                    params.append(wallet_ids)
                elif intent.kind == 'received':
                    clauses.append('t.destination_account_id=ANY(%s)')
                    params.append(wallet_ids)

                if intent.counterparty:
                    like = '%' + intent.counterparty.strip() + '%'
                    if intent.kind == 'sent':
                        clauses.append("EXISTS (SELECT 1 FROM accounts ca JOIN users cu ON cu.id=ca.user_id WHERE ca.id=t.destination_account_id AND lower(cu.name) LIKE lower(%s))")
                    else:
                        clauses.append("EXISTS (SELECT 1 FROM accounts ca JOIN users cu ON cu.id=ca.user_id WHERE ca.id=t.source_account_id AND lower(cu.name) LIKE lower(%s))")
                    params.append(like)

                sql = 'SELECT t.id,t.amount_paise,t.created_at FROM transfers t WHERE ' + ' AND '.join(clauses) + ' ORDER BY t.created_at DESC,t.id DESC'
                cur.execute(sql, params)
                rows = cur.fetchall()

                if intent.kind == 'largest':
                    rows = sorted(rows, key=lambda r: r[1], reverse=True)[:1]

                total = sum(r[1] for r in rows)
                if intent.kind == 'count':
                    answer = f"You had {len(rows)} transfers from {intent.start} through {intent.end_exclusive - timedelta(days=1)}."
                elif intent.kind == 'largest':
                    answer = f"Your largest transfer was {rows[0][1]} paise (transfer {rows[0][0]})." if rows else 'No transfers found for that period.'
                else:
                    answer = f"Total {intent.kind} was {total} paise across {len(rows)} transfers."

                return 200, {
                    'answer': answer,
                    'supported': True,
                    'period': {'from': str(intent.start), 'to_exclusive': str(intent.end_exclusive)},
                    'counterparty': intent.counterparty,
                    'transfer_ids': [r[0] for r in rows],
                    'total_paise': total,
                    'parser': 'mock/deterministic; parameterized SQL only',
                }
        return post_result(idempotent(conn,uid,idempotency_key,p.model_dump(),op))

@app.exception_handler(HTTPException)
def http_error_handler(request,exc):
    detail=exc.detail if isinstance(exc.detail,dict) else {'code':'REQUEST_ERROR','message':str(exc.detail),'request_id':str(uuid.uuid4())}
    return JSONResponse(status_code=exc.status_code,content=detail)
