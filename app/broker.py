"""RabbitMQ work queue that wakes the worker. SQLite stays the source of truth:
a lost message is recovered by the worker's startup resync and fallback poll."""
import logging
import pika
from . import config as cfg

QUEUE = 'ocr.jobs'
log = logging.getLogger('broker')

def params():
    p = pika.URLParameters(cfg.AMQP_URL)
    # No heartbeats: the worker blocks in GPU work between iterations and acks before it, so
    # heartbeats would only drop the connection on every long job. A stopped or killed broker
    # still closes the socket (verified: docker stop and docker kill both raise at once).
    # ponytail: a frozen-but-alive broker goes undetected; the worker keeps running jobs via the
    # SQLite poll every OCR_POLL_SECONDS until restart. Add heartbeats plus a watchdog if that matters.
    p.heartbeat = 0
    p.blocked_connection_timeout = 10
    p.socket_timeout = 5
    return p

def declare(ch):
    # Queue arguments are immutable once declared; API and worker must share this.
    ch.exchange_declare('ocr.dlx', 'direct', durable=True)
    ch.queue_declare('ocr.jobs.dead', durable=True, arguments={'x-max-length': 1000})
    ch.queue_bind('ocr.jobs.dead', 'ocr.dlx', 'ocr.jobs.dead')
    ch.queue_declare(QUEUE, durable=True, arguments={
        'x-queue-type': 'quorum', 'x-delivery-limit': 5,
        'x-dead-letter-exchange': 'ocr.dlx', 'x-dead-letter-routing-key': 'ocr.jobs.dead'})

def send(ch, key):
    ch.basic_publish('', QUEUE, key.encode(), pika.BasicProperties(delivery_mode=2), mandatory=True)

def publish(key):
    """Call only after the 'queued' row is committed. Never fails the request."""
    if not cfg.AMQP_URL:
        return
    try:
        with pika.BlockingConnection(params()) as con:
            ch = con.channel()
            declare(ch)
            ch.confirm_delivery()
            send(ch, key)
    except Exception as e:
        log.warning('publish_failed key=%s error=%s', key[:12], type(e).__name__)

def resync(ch, queued_keys):
    """Rebuild the queue from SQLite, oldest first, so it matches queue_position.
    Purge before reading: a publish racing in between becomes a skipped duplicate, not a loss."""
    ch.queue_purge(QUEUE)
    for key in queued_keys():
        send(ch, key)
