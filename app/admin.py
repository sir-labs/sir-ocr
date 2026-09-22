"""Host-only maintenance. Stop both workers before deletion; no HTTP delete endpoint."""
import argparse
import fcntl
import json
import shutil
from contextlib import ExitStack
from . import db, config as cfg
parser=argparse.ArgumentParser()
commands=parser.add_subparsers(dest='command',required=True)
commands.add_parser('list')
delete=commands.add_parser('delete')
delete.add_argument('result_key')
delete.add_argument('--confirm',required=True,help='Repeat the complete result key.')
args=parser.parse_args()
db.init()
if args.command=='list':
    with db.connect() as c:
        for row in c.execute('SELECT key,state,page_count,created FROM results ORDER BY created'):
            print(json.dumps(dict(row)))
else:
    if args.confirm!=args.result_key:
        raise SystemExit('Confirmation does not match.')
    with ExitStack() as stack:
        for name in ('worker.lock','classifier.lock'):
            lock=stack.enter_context((cfg.DATA/name).open('a'))
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit('Stop the OCR worker and classifier first.')
        with db.connect(True) as c:
            row=c.execute('SELECT key FROM results WHERE key=?',(args.result_key,)).fetchone()
            if not row:
                raise SystemExit('Unknown result key.')
            c.execute('DELETE FROM jobs WHERE result_key=?',(row[0],))
            c.execute('DELETE FROM pages WHERE result_key=?',(row[0],))
            c.execute('DELETE FROM results WHERE key=?',(row[0],))
            shutil.rmtree(cfg.DATA/'results'/row[0],ignore_errors=False)
        print('Deleted document and all job capabilities for that document.')
