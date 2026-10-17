"""Black-box HTTP tests against real PostgreSQL, Redis, RabbitMQ and Nginx."""
import concurrent.futures
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pika
import psycopg
import pytest

BASE = os.environ.get('BASE_URL', 'http://localhost:18080')
DB = os.environ['DATABASE_URL']
client = httpx.Client(base_url=BASE, timeout=30, limits=httpx.Limits(max_connections=100))


def user():
    result = client.post('/users', json={'name': f'Test {uuid.uuid4()} '})
    assert result.status_code == 201, result.text
    body = result.json()
    return {'Authorization': f"Bearer {body['token']}"}


def event(capacity=1, hold_seconds=120, **kw):
    result = client.post('/events', json=dict(name=f'Test {uuid.uuid4()}', capacity=capacity, hold_seconds=hold_seconds, **kw),
                         headers={'X-Admin-Key': os.environ['ADMIN_KEY']})
    assert result.status_code == 201, result.text
    return result.json()['id']


def post(path, who, body=None, key=None):
    return client.post(path, json=body, headers={**who, 'Idempotency-Key': key or str(uuid.uuid4())})


def book(eid, who, **kw):
    return post(f'/events/{eid}/reservations', who, kw)


def wait_for(path, who, expected, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.get(path, headers=who)
        assert result.status_code == 200, result.text
        if result.json()['status'] == expected:
            return result.json()
        time.sleep(.15)
    pytest.fail(f'{path} did not reach {expected}: {result.text}')


def test_same_seat_race_has_exactly_one_owner():
    eid = event()
    people = [user() for _ in range(30)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        responses = list(executor.map(lambda u: book(eid, u, seat_number=1), people))
    assert sum(r.status_code == 201 for r in responses) == 1
    assert sum(r.status_code == 409 for r in responses) == 29
    with psycopg.connect(DB) as conn:
        assert conn.execute("SELECT count(*) FROM reservations WHERE event_id=%s AND status IN ('held','confirmed')", (eid,)).fetchone()[0] == 1


def test_concurrent_idempotency_replays_and_rejects_changed_payload():
    eid, who, key = event(), user(), str(uuid.uuid4())
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        replies = list(executor.map(lambda _: post(f'/events/{eid}/reservations', who, {}, key), range(16)))
    assert all(r.status_code == 201 for r in replies)
    assert len({r.json()['reservation']['id'] for r in replies}) == 1
    assert post(f'/events/{eid}/reservations', who, {'seat_number': 1}, key).status_code == 409


def test_expiry_reallocates_and_old_holder_cannot_pay():
    eid, first, second = event(hold_seconds=2), user(), user()
    rid = book(eid, first).json()['reservation']['id']
    assert book(eid, second).status_code == 202
    wait_for(f'/reservations/{rid}', first, 'expired')
    row = client.get(f'/events/{eid}/waitlist', headers=second).json()
    assert row['status'] == 'allocated'
    assert row['reservation_id'] != rid
    assert post(f'/reservations/{rid}/payments', first, {}).status_code == 409


def test_waitlist_fifo_cancel_and_no_queue_jumping():
    eid = event()
    owner, a, b, c, newcomer = [user() for _ in range(5)]
    rid = book(eid, owner).json()['reservation']['id']
    for who in (a, b, c):
        assert book(eid, who).status_code == 202
    assert client.get(f'/events/{eid}/waitlist', headers=c).json()['position'] == 3
    assert post(f'/events/{eid}/waitlist/cancel', a).status_code == 200
    assert post(f'/reservations/{rid}/cancel', owner).status_code == 200
    assert client.get(f'/events/{eid}/waitlist', headers=b).json()['status'] == 'allocated'
    assert client.get(f'/events/{eid}/waitlist', headers=c).json()['position'] == 1
    assert book(eid, newcomer).status_code == 202


@pytest.mark.parametrize('outcome,expected,booking_state', [('success','succeeded','confirmed'), ('failure','failed','payment_failed')])
def test_async_payment_and_idempotent_retry(outcome, expected, booking_state):
    eid, who = event(), user()
    rid = book(eid, who).json()['reservation']['id']
    key = str(uuid.uuid4())
    path = f'/reservations/{rid}/payments'
    one = post(path, who, {'outcome': outcome}, key)
    two = post(path, who, {'outcome': outcome}, key)
    assert one.status_code == two.status_code == 202
    assert one.json() == two.json()
    pid = one.json()['payment']['id']
    wait_for(f'/payments/{pid}', who, expected)
    wait_for(f'/reservations/{rid}', who, booking_state)
    assert post(f'/reservations/{rid}/confirm', who).status_code == (200 if outcome == 'success' else 409)
    # Real broker duplicate delivery after commit must leave state unchanged.
    conn = pika.BlockingConnection(pika.URLParameters(os.environ['RABBITMQ_URL']))
    ch = conn.channel()
    ch.basic_publish(exchange='', routing_key='seat.payments', body='{"payment_id":"' + pid + '"}', properties=pika.BasicProperties(delivery_mode=2))
    conn.close()
    time.sleep(.4)
    assert client.get(f'/payments/{pid}', headers=who).json()['status'] == expected
    assert post(path, who, {'outcome': outcome}).status_code == 409


def test_delayed_success_after_reallocation_is_refunded():
    eid, first, second = event(hold_seconds=2), user(), user()
    rid = book(eid, first).json()['reservation']['id']
    pid = post(f'/reservations/{rid}/payments', first, {'delay_seconds': 4}).json()['payment']['id']
    assert book(eid, second).status_code == 202
    wait_for(f'/payments/{pid}', first, 'refunded')
    assert client.get(f'/reservations/{rid}', headers=first).json()['status'] == 'expired'
    # No confirmation was resurrected by the late callback.
    with psycopg.connect(DB) as conn:
        assert conn.execute("SELECT count(*) FROM reservations WHERE event_id=%s AND status='confirmed'", (eid,)).fetchone()[0] == 0


def test_cancel_confirmed_refunds_and_releases():
    eid, who, next_user = event(), user(), user()
    rid = book(eid, who).json()['reservation']['id']
    pid = post(f'/reservations/{rid}/payments', who, {}).json()['payment']['id']
    wait_for(f'/payments/{pid}', who, 'succeeded')
    assert post(f'/reservations/{rid}/cancel', who).json()['reservation']['status'] == 'cancelled'
    assert client.get(f'/payments/{pid}', headers=who).json()['status'] == 'refunded'
    assert book(eid, next_user).status_code == 201


def test_ownership_validation_history_and_opening_time():
    eid, owner, stranger = event(), user(), user()
    rid = book(eid, owner).json()['reservation']['id']
    for path in (f'/reservations/{rid}/cancel', f'/reservations/{rid}/payments'):
        assert post(path, stranger, {}).status_code == 404
    assert client.get(f'/reservations/{rid}', headers=stranger).status_code == 404
    assert client.get('/reservations', headers=owner).json()[0]['id'] == rid
    assert client.get('/reservations', headers={'Authorization': 'Bearer invalid'}).status_code == 401
    assert client.post('/events', json={'name': 'bad'}, headers={'X-Admin-Key': 'bad'}).status_code == 403
    future = event(opens_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    assert book(future, owner).status_code == 409
    assert book(event(), owner, seat_number=999).status_code == 422
    assert post(f'/reservations/{rid}/payments', owner, {'outcome': 'invalid'}).status_code == 422


def test_cache_and_worker_automatic_expiry_without_reads():
    eid, who = event(hold_seconds=1), user()
    rid = book(eid, who).json()['reservation']['id']
    time.sleep(3)
    with psycopg.connect(DB) as conn:
        assert conn.execute('SELECT status FROM reservations WHERE id=%s', (rid,)).fetchone()[0] == 'expired'
    client.get(f'/events/{eid}/seats')
    result = client.get(f'/events/{eid}/seats')
    assert result.headers['X-Cache'] == 'HIT'
    assert result.json()['available'] == 1


def test_database_constraint_rejects_bypass_double_booking():
    eid, who = event(), user()
    rid = book(eid, who).json()['reservation']['id']
    with psycopg.connect(DB) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO reservations SELECT %s,event_id,seat_number,user_id,'held',expires_at,created_at,updated_at FROM reservations WHERE id=%s", (uuid.uuid4(), rid))


def test_cancel_and_success_payment_race_never_resurrects_booking():
    eid, who, next_user = event(), user(), user()
    rid = book(eid, who).json()['reservation']['id']
    pid = post(f'/reservations/{rid}/payments', who, {'delay_seconds': 1}).json()['payment']['id']
    assert post(f'/reservations/{rid}/cancel', who).status_code == 200
    replacement = book(eid, next_user)
    assert replacement.status_code == 201
    wait_for(f'/payments/{pid}', who, 'refunded')
    assert client.get(f'/reservations/{rid}', headers=who).json()['status'] == 'cancelled'
    assert client.get(f"/reservations/{replacement.json()['reservation']['id']}", headers=next_user).json()['status'] == 'held'


def test_one_active_booking_per_user_even_with_different_keys():
    eid, who = event(capacity=3), user()
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        replies = list(executor.map(lambda _: book(eid, who), range(10)))
    assert sum(r.status_code == 201 for r in replies) == 1
    assert sum(r.status_code == 409 for r in replies) == 9
