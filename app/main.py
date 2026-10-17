import hashlib
import json
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Literal

import redis
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.db import initialize, pool
from app.service import active_booking, as_json, event_lock, idempotent, maintain, reserve

cache = redis.Redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'), socket_connect_timeout=.2, socket_timeout=.2)


@asynccontextmanager
async def lifespan(app):
    initialize()
    yield
    pool.close()


app = FastAPI(title='The Last Seat', version='1.0.0', lifespan=lifespan,
              description='Concurrent concert booking. Register for a bearer token, then use Idempotency-Key on mutations. Payments are simulated.')


class UserInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class EventInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    capacity: int = Field(default=500, ge=1, le=10000)
    hold_seconds: int = Field(default=120, ge=1, le=3600)
    opens_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReservationInput(BaseModel):
    seat_number: int | None = Field(default=None, ge=1, le=10000)
    join_waitlist: bool = True


class PaymentInput(BaseModel):
    outcome: Literal['success', 'failure'] = 'success'
    delay_seconds: int = Field(default=0, ge=0, le=3600)


def current_user(authorization: Annotated[str, Header()]):
    if not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Use Authorization: Bearer <token>')
    digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
    with pool.connection() as conn:
        user = conn.execute('SELECT id,name FROM users WHERE token_hash=%s', (digest,)).fetchone()
    if not user:
        raise HTTPException(401, 'Invalid bearer token')
    return user


User = Annotated[dict, Depends(current_user)]
Key = Annotated[str, Header(alias='Idempotency-Key', min_length=8, max_length=128)]


def run(user, key, route, payload, action):
    with pool.connection() as conn:
        code, data = idempotent(conn, user['id'], key, route, payload, lambda: action(conn))
    return JSONResponse(status_code=code, content=as_json(data))


def owned(conn, reservation_id, user):
    booking = conn.execute('SELECT * FROM reservations WHERE id=%s AND user_id=%s', (reservation_id, user['id'])).fetchone()
    return booking


@app.get('/health/live', tags=['Operations'])
def live():
    return {'status': 'ok'}


@app.get('/health/ready', tags=['Operations'])
def ready():
    try:
        with pool.connection() as conn:
            conn.execute('SELECT 1')
    except Exception:
        raise HTTPException(503, 'PostgreSQL unavailable')
    # Redis is optional; payment jobs survive RabbitMQ downtime in the outbox.
    return {'status': 'ready'}


@app.post('/users', status_code=201, tags=['Identity'])
def create_user(body: UserInput):
    token = secrets.token_urlsafe(32)
    with pool.connection() as conn:
        row = conn.execute('INSERT INTO users(id,name,token_hash) VALUES(%s,%s,%s) RETURNING id,name',
                           (uuid.uuid4(), body.name, hashlib.sha256(token.encode()).hexdigest())).fetchone()
    return {**row, 'token': token}


@app.post('/events', status_code=201, tags=['Events'])
def create_event(body: EventInput, x_admin_key: Annotated[str, Header()]):
    expected = os.environ.get('ADMIN_KEY', '')
    if not expected or not secrets.compare_digest(x_admin_key, expected):
        raise HTTPException(403, 'Invalid admin key')
    if body.opens_at.tzinfo is None:
        raise HTTPException(422, 'opens_at must include a timezone')
    with pool.connection() as conn:
        event = conn.execute('INSERT INTO events(id,name,capacity,hold_seconds,opens_at) VALUES(%s,%s,%s,%s,%s) RETURNING *',
                             (uuid.uuid4(), body.name, body.capacity, body.hold_seconds, body.opens_at)).fetchone()
        conn.execute('INSERT INTO seats SELECT %s,generate_series(1,%s)', (event['id'], body.capacity))
    return event


@app.get('/events', tags=['Events'])
def events():
    with pool.connection() as conn:
        return conn.execute('SELECT * FROM events ORDER BY created_at DESC LIMIT 100').fetchall()


@app.get('/events/{event_id}/seats', tags=['Events'])
def seats(event_id: uuid.UUID):
    cache_key = f'seats:{event_id}'
    try:
        cached = cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached), headers={'X-Cache': 'HIT'})
    except redis.RedisError:
        pass
    with pool.connection() as conn:
        if not conn.execute('SELECT id FROM events WHERE id=%s', (event_id,)).fetchone():
            raise HTTPException(404, 'Event not found')
        rows = conn.execute('''SELECT s.number,COALESCE(r.status,'available') AS status FROM seats s
            LEFT JOIN reservations r ON r.event_id=s.event_id AND r.seat_number=s.number
              AND (r.status='confirmed' OR (r.status='held' AND r.expires_at>clock_timestamp()))
            WHERE s.event_id=%s ORDER BY s.number''', (event_id,)).fetchall()
    result = {'event_id': str(event_id), 'available': sum(r['status'] == 'available' for r in rows),
              'seats': rows, 'cache_max_age_seconds': 1}
    try:
        cache.setex(cache_key, 1, json.dumps(result))
    except redis.RedisError:
        pass
    return JSONResponse(content=result, headers={'X-Cache': 'MISS'})


@app.post('/events/{event_id}/reservations', tags=['Bookings'])
def book(event_id: uuid.UUID, body: ReservationInput, user: User, key: Key):
    def action(conn):
        event = event_lock(conn, event_id)
        if not event:
            return 404, {'detail': 'Event not found'}
        maintain(conn, event)
        if event['opens_at'] > conn.execute('SELECT clock_timestamp() AS now').fetchone()['now']:
            return 409, {'detail': 'Booking has not opened', 'opens_at': event['opens_at']}
        existing = active_booking(conn, event_id, user['id'])
        if existing:
            return 409, {'detail': 'You already hold a seat for this event', 'reservation': existing}
        waiting = conn.execute("SELECT * FROM waitlist WHERE event_id=%s AND user_id=%s AND status='waiting'", (event_id, user['id'])).fetchone()
        if waiting:
            return 202, {'waitlist': waiting}
        if body.seat_number and body.seat_number > event['capacity']:
            return 422, {'detail': 'Seat outside event capacity'}
        booking = reserve(conn, event, user['id'], body.seat_number)
        if booking:
            return 201, {'reservation': booking}
        # A specific-seat conflict is not event capacity exhaustion.
        if body.seat_number or not body.join_waitlist:
            return 409, {'detail': 'Seat unavailable'}
        entry = conn.execute('INSERT INTO waitlist(event_id,user_id) VALUES(%s,%s) RETURNING *', (event_id, user['id'])).fetchone()
        return 202, {'waitlist': entry}
    return run(user, key, f'book:{event_id}', body.model_dump(), action)


@app.get('/reservations', tags=['Bookings'])
def history(user: User, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    with pool.connection() as conn:
        # Worker expiry runs every second. Individual status GET also reconciles inline.
        return conn.execute('SELECT * FROM reservations WHERE user_id=%s ORDER BY created_at DESC,id LIMIT %s OFFSET %s', (user['id'], limit, offset)).fetchall()


@app.get('/reservations/{reservation_id}', tags=['Bookings'])
def status(reservation_id: uuid.UUID, user: User):
    with pool.connection() as conn:
        booking = owned(conn, reservation_id, user)
        if not booking:
            raise HTTPException(404, 'Reservation not found')
        maintain(conn, event_lock(conn, booking['event_id']))
        return owned(conn, reservation_id, user)


@app.post('/reservations/{reservation_id}/cancel', tags=['Bookings'])
def cancel(reservation_id: uuid.UUID, user: User, key: Key):
    def action(conn):
        booking = owned(conn, reservation_id, user)
        if not booking:
            return 404, {'detail': 'Reservation not found'}
        event = event_lock(conn, booking['event_id'])
        maintain(conn, event)
        conn.execute("UPDATE reservations SET status='cancelled',updated_at=clock_timestamp() WHERE id=%s AND status IN ('held','confirmed')", (reservation_id,))
        conn.execute("UPDATE payments SET status='refunded',updated_at=clock_timestamp() WHERE reservation_id=%s AND status='succeeded'", (reservation_id,))
        maintain(conn, event)
        return 200, {'reservation': owned(conn, reservation_id, user)}
    return run(user, key, f'cancel:{reservation_id}', {}, action)


@app.post('/reservations/{reservation_id}/payments', tags=['Payments'])
def pay(reservation_id: uuid.UUID, body: PaymentInput, user: User, key: Key):
    def action(conn):
        booking = owned(conn, reservation_id, user)
        if not booking:
            return 404, {'detail': 'Reservation not found'}
        maintain(conn, event_lock(conn, booking['event_id']))
        booking = owned(conn, reservation_id, user)
        previous = conn.execute('SELECT * FROM payments WHERE reservation_id=%s', (reservation_id,)).fetchone()
        if previous:
            return 409, {'detail': 'Payment already submitted; retrieve its status', 'payment': previous}
        if booking['status'] != 'held':
            return 409, {'detail': 'Reservation is no longer payable'}
        payment = conn.execute('''INSERT INTO payments(id,reservation_id,outcome,due_at)
            VALUES(%s,%s,%s,clock_timestamp()+%s*interval '1 second') RETURNING *''',
            (uuid.uuid4(), reservation_id, body.outcome, body.delay_seconds)).fetchone()
        conn.execute('INSERT INTO outbox(payment_id,due_at) VALUES(%s,%s)', (payment['id'], payment['due_at']))
        return 202, {'payment': payment}
    return run(user, key, f'payment:{reservation_id}', body.model_dump(), action)


@app.post('/reservations/{reservation_id}/confirm', tags=['Bookings'])
def confirm(reservation_id: uuid.UUID, user: User, key: Key):
    """Acknowledge payment-backed confirmation; never bypass the payment worker."""
    def action(conn):
        booking = owned(conn, reservation_id, user)
        if not booking:
            return 404, {'detail': 'Reservation not found'}
        maintain(conn, event_lock(conn, booking['event_id']))
        booking = owned(conn, reservation_id, user)
        if booking['status'] != 'confirmed':
            return 409, {'detail': 'Successful payment required before confirmation', 'reservation': booking}
        return 200, {'reservation': booking}
    return run(user, key, f'confirm:{reservation_id}', {}, action)


@app.get('/payments/{payment_id}', tags=['Payments'])
def payment_status(payment_id: uuid.UUID, user: User):
    with pool.connection() as conn:
        row = conn.execute('''SELECT p.* FROM payments p JOIN reservations r ON r.id=p.reservation_id
            WHERE p.id=%s AND r.user_id=%s''', (payment_id, user['id'])).fetchone()
        if not row:
            raise HTTPException(404, 'Payment not found')
        return row


@app.get('/events/{event_id}/waitlist', tags=['Waitlist'])
def waitlist_status(event_id: uuid.UUID, user: User):
    with pool.connection() as conn:
        event = event_lock(conn, event_id)
        if not event:
            raise HTTPException(404, 'Event not found')
        maintain(conn, event)
        return conn.execute('''SELECT w.*, (SELECT count(*) FROM waitlist before
            WHERE before.event_id=w.event_id AND before.status='waiting' AND before.id<=w.id) AS position
            FROM waitlist w WHERE w.event_id=%s AND w.user_id=%s ORDER BY id DESC LIMIT 1''', (event_id, user['id'])).fetchone()


@app.post('/events/{event_id}/waitlist/cancel', tags=['Waitlist'])
def leave_waitlist(event_id: uuid.UUID, user: User, key: Key):
    def action(conn):
        event = event_lock(conn, event_id)
        if not event:
            return 404, {'detail': 'Event not found'}
        entry = conn.execute("UPDATE waitlist SET status='cancelled' WHERE event_id=%s AND user_id=%s AND status='waiting' RETURNING *", (event_id, user['id'])).fetchone()
        return 200, {'waitlist': entry}
    return run(user, key, f'leave:{event_id}', {}, action)
