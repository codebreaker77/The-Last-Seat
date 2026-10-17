"""Durable outbox publisher, at-least-once payment consumer, expiry scheduler."""
import json
import logging
import os
import threading
import time

import pika

from app.db import initialize, pool
from app.service import event_lock, maintain, settle_payment

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('seat.worker')
QUEUE = 'seat.payments'


def broker():
    params = pika.URLParameters(os.environ['RABBITMQ_URL'])
    params.heartbeat = 30
    params.blocked_connection_timeout = 5
    params.socket_timeout = 5
    conn = pika.BlockingConnection(params)
    channel = conn.channel()
    channel.queue_declare(queue=QUEUE, durable=True)
    return conn, channel


def publish_forever():
    while True:
        connection = None
        try:
            connection, channel = broker()
            channel.confirm_delivery()
            while True:
                with pool.connection() as conn:
                    rows = conn.execute('''SELECT * FROM outbox WHERE published_at IS NULL
                        AND due_at<=clock_timestamp() ORDER BY id LIMIT 50 FOR UPDATE SKIP LOCKED''').fetchall()
                    for row in rows:
                        channel.basic_publish(exchange='', routing_key=QUEUE,
                            body=json.dumps({'payment_id': str(row['payment_id'])}), mandatory=True,
                            properties=pika.BasicProperties(delivery_mode=2, content_type='application/json'))
                        # Crash after publish and before commit may duplicate: settlement is idempotent.
                        conn.execute('UPDATE outbox SET published_at=clock_timestamp() WHERE id=%s', (row['id'],))
                connection.process_data_events(time_limit=.2)
        except Exception:
            log.exception('Publisher disconnected; durable outbox retained, retrying')
            time.sleep(2)
        finally:
            if connection and connection.is_open:
                connection.close()


def expire_forever():
    while True:
        try:
            with pool.connection() as conn:
                events = conn.execute('''SELECT DISTINCT e.id FROM events e
                    WHERE EXISTS (SELECT 1 FROM reservations r WHERE r.event_id=e.id AND r.status='held' AND r.expires_at<=clock_timestamp())
                    OR EXISTS (SELECT 1 FROM waitlist w WHERE w.event_id=e.id AND w.status='waiting')''').fetchall()
            for event in events:
                with pool.connection() as conn:
                    maintain(conn, event_lock(conn, event['id']))
        except Exception:
            log.exception('Expiry pass failed; retrying next tick')
        time.sleep(1)


def consume_forever():
    while True:
        connection = None
        try:
            connection, channel = broker()
            channel.basic_qos(prefetch_count=16)

            def receive(ch, method, properties, body):
                try:
                    payment_id = json.loads(body)['payment_id']
                    with pool.connection() as conn:
                        settle_payment(conn, payment_id)
                    ch.basic_ack(method.delivery_tag)
                except (ValueError, KeyError, json.JSONDecodeError):
                    log.exception('Invalid message discarded')
                    ch.basic_reject(method.delivery_tag, requeue=False)
                except Exception:
                    log.exception('Settlement failed; retrying delivery')
                    ch.basic_nack(method.delivery_tag, requeue=True)
                    time.sleep(.5)

            channel.basic_consume(queue=QUEUE, on_message_callback=receive)
            log.info('Payment consumer ready')
            channel.start_consuming()
        except Exception:
            log.exception('Consumer disconnected; reconnecting')
            time.sleep(2)
        finally:
            if connection and connection.is_open:
                connection.close()


if __name__ == '__main__':
    initialize()
    threading.Thread(target=publish_forever, daemon=True).start()
    threading.Thread(target=expire_forever, daemon=True).start()
    consume_forever()
