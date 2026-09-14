import time
from . import db
with db.connect() as c:
    row=c.execute('SELECT heartbeat FROM worker WHERE id=1').fetchone()
raise SystemExit(0 if row and time.time()-row[0]<30 else 1)
