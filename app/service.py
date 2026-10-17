"""All booking transitions run under the event row lock, across every replica."""
import hashlib
import json
import uuid

from psycopg.types.json import Jsonb


def as_json(value):
    return json.loads(json.dumps(value, default=str))


def event_lock(conn, event_id):
    return conn.execute('SELECT * FROM events WHERE id=%s FOR UPDATE', (event_id,)).fetchone()


def reserve(conn, event, user_id, seat_number=None):
    seat = conn.execute('''SELECT number FROM seats s WHERE event_id=%s
        AND (%s::int IS NULL OR number=%s)
        AND NOT EXISTS (SELECT 1 FROM reservations r WHERE r.event_id=s.event_id
          AND r.seat_number=s.number AND r.status IN ('held','confirmed'))
        ORDER BY number LIMIT 1''', (event['id'], seat_number, seat_number)).fetchone()
    if not seat:
        return None
    return conn.execute('''INSERT INTO reservations(id,event_id,seat_number,user_id,status,expires_at)
        VALUES(%s,%s,%s,%s,'held',clock_timestamp() + %s * interval '1 second') RETURNING *''',
        (uuid.uuid4(), event['id'], seat['number'], user_id, event['hold_seconds'])).fetchone()


def maintain(conn, event):
    """Expire stale holds and allocate free seats in durable FIFO order, atomically."""
    conn.execute("UPDATE reservations SET status='expired',updated_at=clock_timestamp() WHERE event_id=%s AND status='held' AND expires_at<=clock_timestamp()", (event['id'],))
    now = conn.execute('SELECT clock_timestamp() AS now').fetchone()['now']
    if now < event['opens_at']:
        return
    while True:
        entry = conn.execute("SELECT * FROM waitlist WHERE event_id=%s AND status='waiting' ORDER BY id LIMIT 1", (event['id'],)).fetchone()
        if not entry:
            break
        booking = reserve(conn, event, entry['user_id'])
        if not booking:
            break
        conn.execute("UPDATE waitlist SET status='allocated',reservation_id=%s WHERE id=%s", (booking['id'], entry['id']))


def active_booking(conn, event_id, user_id):
    return conn.execute("SELECT * FROM reservations WHERE event_id=%s AND user_id=%s AND status IN ('held','confirmed')", (event_id, user_id)).fetchone()


def idempotent(conn, user_id, key, route, payload, action):
    fingerprint = hashlib.sha256(json.dumps([route, payload], sort_keys=True).encode()).hexdigest()
    # Independent keys do not block one another; the lock also handles absent rows.
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (f'{user_id}:{key}',))
    previous = conn.execute('SELECT * FROM idempotency WHERE user_id=%s AND key=%s', (user_id, key)).fetchone()
    if previous:
        if previous['fingerprint'] != fingerprint:
            return 409, {'detail': 'Idempotency-Key already used with a different request'}
        return previous['code'], previous['response']
    code, response = action()
    response = as_json(response)
    conn.execute('INSERT INTO idempotency(user_id,key,fingerprint,code,response) VALUES(%s,%s,%s,%s,%s)',
                 (user_id, key, fingerprint, code, Jsonb(response)))
    return code, response


def settle_payment(conn, payment_id):
    lookup = conn.execute('''SELECT r.event_id FROM payments p JOIN reservations r ON r.id=p.reservation_id WHERE p.id=%s''', (payment_id,)).fetchone()
    if not lookup:
        return
    event = event_lock(conn, lookup['event_id'])
    maintain(conn, event)
    payment = conn.execute('SELECT * FROM payments WHERE id=%s FOR UPDATE', (payment_id,)).fetchone()
    if payment['status'] != 'pending':
        return  # RabbitMQ redelivery is safe, including after a commit-before-ack crash.
    if payment['due_at'] > conn.execute('SELECT clock_timestamp() AS now').fetchone()['now']:
        raise ValueError('payment delivered before due_at')
    booking = conn.execute('SELECT * FROM reservations WHERE id=%s', (payment['reservation_id'],)).fetchone()
    if payment['outcome'] == 'failure':
        state = 'failed'
        if booking['status'] == 'held':
            conn.execute("UPDATE reservations SET status='payment_failed',updated_at=clock_timestamp() WHERE id=%s", (booking['id'],))
    elif booking['status'] == 'held':
        changed = conn.execute("UPDATE reservations SET status='confirmed',updated_at=clock_timestamp() WHERE id=%s AND expires_at>clock_timestamp() RETURNING id", (booking['id'],)).fetchone()
        state = 'succeeded' if changed else 'refunded'
    else:
        state = 'refunded'  # Simulated compensation; late success never steals a reallocated seat.
    conn.execute('UPDATE payments SET status=%s,updated_at=clock_timestamp() WHERE id=%s', (state, payment_id))
    maintain(conn, event)
