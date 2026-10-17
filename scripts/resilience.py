"""Run from the repo root on the host. Temporarily stops only this stack's services."""
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request
import uuid

BASE = 'http://127.0.0.1:' + os.environ.get('PORT', '18080')


def request(path, body=None, token=None, admin=None):
    headers = {'Content-Type': 'application/json', 'Idempotency-Key': str(uuid.uuid4())}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    if admin:
        headers['X-Admin-Key'] = admin
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, headers=headers), timeout=20) as response:
        return json.load(response)


def compose(*args):
    subprocess.run(['docker', 'compose', *args], check=True)


def main():
    global BASE
    settings = dict(line.split('=', 1) for line in Path('.env').read_text().splitlines() if '=' in line)
    BASE = 'http://127.0.0.1:' + os.environ.get('PORT', settings.get('PORT', '18080'))
    token = request('/users', {'name': 'Resilience test'})['token']
    eid = request('/events', {'name': 'Outage recovery test', 'capacity': 1}, admin=settings['ADMIN_KEY'])['id']
    rid = request(f'/events/{eid}/reservations', {}, token)['reservation']['id']
    results = {}
    try:
        compose('stop', 'rabbitmq', 'redis')
        # Cached reads fall back to SQL and writes persist even while broker is offline.
        results['redis_fallback'] = request(f'/events/{eid}/seats')['available'] == 0
        pid = request(f'/reservations/{rid}/payments', {}, token)['payment']['id']
        time.sleep(2)
        results['broker_outage_job_durable'] = request(f'/payments/{pid}', token=token)['status'] == 'pending'
    finally:
        compose('start', 'rabbitmq', 'redis')
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        payment = request(f'/payments/{pid}', token=token)
        if payment['status'] == 'succeeded':
            break
        time.sleep(1)
    results['broker_recovery_confirms'] = payment['status'] == 'succeeded'
    compose('restart', 'worker')
    results['confirmation_survives_worker_restart'] = request(f'/reservations/{rid}', token=token)['status'] == 'confirmed'
    request(f'/reservations/{rid}/cancel', {}, token)
    Path('docs/results').mkdir(parents=True, exist_ok=True)
    Path('docs/results/resilience.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))
    if not all(results.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
