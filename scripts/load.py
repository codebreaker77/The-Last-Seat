"""Bounded-concurrency real HTTP load; prepare identities outside the timed phase."""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import time
import uuid

import httpx
import psycopg


async def main(args):
    base = os.environ.get('BASE_URL', 'http://localhost:18080')
    with httpx.Client(base_url=base, timeout=30) as client:
        response = client.post('/events', json={'name': '10,000 student load run', 'capacity': 500, 'hold_seconds': 3600},
                               headers={'X-Admin-Key': os.environ['ADMIN_KEY']})
        response.raise_for_status()
        event = response.json()['id']
    tokens = [str(uuid.uuid4()) + str(uuid.uuid4()) for _ in range(args.requests)]
    with psycopg.connect(os.environ['DATABASE_URL']) as conn:
        with conn.cursor().copy('COPY users(id,name,token_hash) FROM STDIN') as copy:
            for token in tokens:
                copy.write_row((uuid.uuid4(), 'Load test identity', hashlib.sha256(token.encode()).hexdigest()))
    counts, latencies, errors = Counter(), [], []
    cursor = iter(enumerate(tokens))
    async with httpx.AsyncClient(base_url=base, timeout=60, limits=httpx.Limits(max_connections=args.concurrency)) as client:
        async def worker():
            for index, token in cursor:
                before = time.perf_counter()
                try:
                    result = await client.post(f'/events/{event}/reservations', json={'join_waitlist': True},
                        headers={'Authorization': f'Bearer {token}', 'Idempotency-Key': f'load-{event}-{index}'})
                    counts[str(result.status_code)] += 1
                    if result.status_code not in (201, 202):
                        errors.append(result.text[:200])
                except Exception as exc:
                    counts['transport_error'] += 1
                    errors.append(str(exc))
                latencies.append((time.perf_counter() - before) * 1000)
        started = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(args.concurrency)))
        elapsed = time.perf_counter() - started
    with psycopg.connect(os.environ['DATABASE_URL']) as conn:
        active = conn.execute("SELECT count(*),count(DISTINCT seat_number) FROM reservations WHERE event_id=%s AND status IN ('held','confirmed')", (event,)).fetchone()
        waiting = conn.execute("SELECT count(*) FROM waitlist WHERE event_id=%s AND status='waiting'", (event,)).fetchone()[0]
    latencies.sort()
    result = {'timestamp_utc': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
              'environment': platform.platform(), 'requests': args.requests, 'concurrency': args.concurrency,
              'event_id': event, 'duration_seconds': round(elapsed, 3), 'requests_per_second': round(args.requests / elapsed, 2),
              'status_counts': dict(counts), 'latency_ms': {'p50': round(statistics.median(latencies), 2),
              'p95': round(latencies[int(len(latencies)*.95)-1], 2), 'p99': round(latencies[int(len(latencies)*.99)-1], 2)},
              'active_reservations': active[0], 'distinct_occupied_seats': active[1], 'waiting': waiting,
              'errors': errors[:10], 'passed': counts['201'] == 500 and counts['202'] == args.requests-500 and active == (500,500) and waiting == args.requests-500 and not errors}
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    # Leave history but release load-test holds/queue so demo and future runs stay lightweight.
    with psycopg.connect(os.environ['DATABASE_URL']) as conn:
        conn.execute('SELECT id FROM events WHERE id=%s FOR UPDATE', (event,))
        conn.execute("UPDATE waitlist SET status='cancelled' WHERE event_id=%s AND status='waiting'", (event,))
        conn.execute("UPDATE reservations SET status='cancelled' WHERE event_id=%s AND status='held'", (event,))
    if not result['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--requests', type=int, default=10000)
    parser.add_argument('--concurrency', type=int, default=100)
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.requests < 500 or args.concurrency < 1:
        parser.error('requests must be >=500 and concurrency >=1')
    asyncio.run(main(args))
