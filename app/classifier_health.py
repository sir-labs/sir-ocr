import time
from . import db

with db.connect() as c:
    row = c.execute('SELECT heartbeat,state FROM classification_worker WHERE id=1').fetchone()
raise SystemExit(0 if row and time.time()-row[0]<45 and row[1]!='unavailable' else 1)
