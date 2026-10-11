import os,time,logging
from .db import connection
from .logic import reconcile,init_system
logging.basicConfig(level=logging.INFO)
interval=int(os.getenv('RECONCILIATION_CRON_SECONDS','86400'))
while True:
    try:
        with connection() as conn:
            with conn.transaction(): init_system(conn)
            try: logging.info('nightly reconciliation: %s',reconcile(conn))
            except Exception: logging.exception('reconciliation skipped/failed (another worker may hold lock)')
    except Exception: logging.exception('worker iteration failed')
    time.sleep(interval)
