# The Last Seat

A concert booking backend for **500 seats and 10,000 competing students**. Python/FastAPI, PostgreSQL, Redis, RabbitMQ, and Nginx; the complete stack runs with Docker Compose. Transactions and database constraints prevent double booking across API processes. Holds expire automatically, and late payments never reclaim a released seat.

Implements the CodeChef-VIT Promotional Cycle 2026-27 [backend task](docs/TASK.md), including its bonus features. This folder is a standalone repository with origin `https://github.com/codebreaker77/The-Last-Seat.git`.

## Run

```sh
python scripts/setup.py
docker compose up --build -d
```

Open **[interactive API docs](http://127.0.0.1:18080/docs)**. Check `http://127.0.0.1:18080/health/ready`. Requires Docker with Compose, Python 3.9+ for the dependency-free setup script, and port 18080. Application Python runs inside containers. First startup downloads images and waits for service health checks.

Default event: `00000000-0000-0000-0000-000000000001`, 500 seats, 120-second holds, open immediately. Admin-created events can specify a real 6 PM opening with a timezone-aware timestamp. Generated credentials stay in ignored `.env`.

## Task coverage

| Requirement | Implementation |
| --- | --- |
| View, reserve, confirm | Seat API, temporary holds, payment-backed confirmation |
| No double booking | PostgreSQL event row lock and partial unique seat/user indexes |
| Expiring reservations | One-second worker sweep and inline deadline checks |
| Cancellation/reallocation | Atomic release, simulated refund, FIFO promotion |
| Successful/failed/delayed payments | Async simulation; late success refunded without resurrecting a hold |
| Repeated/concurrent requests | Stored idempotency responses, payload conflict detection, DB constraints |
| History/status | Owner-authorized reservation, payment, history and queue APIs |
| Redis bonus | One-second availability cache; graceful SQL fallback |
| Waiting queue bonus | Durable FIFO queue and automatic temporary seat allocation |
| Async broker bonus | RabbitMQ, durable queue, publisher confirms, transactional outbox |
| Idempotency bonus | Booking, payment, confirmation, cancellation and leaving queue |
| Reverse proxy bonus | Nginx, internal networking, request size limit |
| Deployment bonus | Local six-service Docker deployment; [server deployment guide](docs/DEPLOYMENT.md) |
| Deliverables | Docker, OpenAPI, examples, ERD/workflow, concurrency explanation, real load results |

## API

Register with `POST /users` to receive a random bearer token; retain it securely. Other students cannot access your bookings/payments. This is demo identity issuance, not verified college authentication. Event creation requires `X-Admin-Key` from `.env`. Booking mutations require an `Idempotency-Key` of 8-128 characters. Retry using the same key and request; another body/route with that key returns 409.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/users` | Register `{ "name": "Student" }`; token returned once |
| GET | `/events` | Latest 100 events |
| POST | `/events` | Admin creates capacity, hold duration and opening |
| GET | `/events/{id}/seats` | Availability, at most one second stale |
| POST | `/events/{id}/reservations` | `{}` for any seat; optional `seat_number`, `join_waitlist` |
| GET | `/reservations?limit=50&offset=0` | Own booking history |
| GET | `/reservations/{id}` | Current state, reconciling expiry |
| POST | `/reservations/{id}/payments` | `{ "outcome": "success", "delay_seconds": 0 }` |
| GET | `/payments/{id}` | Pending/succeeded/failed/refunded |
| POST | `/reservations/{id}/confirm` | Acknowledge payment-backed confirmation |
| POST | `/reservations/{id}/cancel` | Cancel; simulated refund if needed |
| GET | `/events/{id}/waitlist` | Own position or allocated reservation ID |
| POST | `/events/{id}/waitlist/cancel` | Leave waiting queue |
| GET | `/health/live`, `/health/ready` | Process and DB readiness |

Booking returns **201** with `reservation`, **202** with `waitlist` when full, or **409** for conflicts or an early opening. Disable queuing with `join_waitlist: false`. One active seat per user per event is an explicit assumption. A specific-seat conflict returns 409 without queuing. Admin event creation accepts `capacity` 1-10,000, `hold_seconds` 1-3,600, and timezone-aware `opens_at`.

Payments return **202**, then the worker finalizes them. Successful settlement automatically confirms an unexpired hold; `confirm` cannot bypass payment. Poll payment or reservation status. Use `outcome: "failure"` to release a hold or `delay_seconds` up to 3,600 to simulate latency. Late/cancelled successful payments become simulated refunds. Terminal reservations stay terminal; book again with a new key. One payment attempt per reservation.

## Example (PowerShell)

```powershell
$base = 'http://127.0.0.1:18080'
$event = '00000000-0000-0000-0000-000000000001'
$student = Invoke-RestMethod "$base/users" -Method Post -ContentType 'application/json' -Body '{"name":"Student"}'
$headers = @{ Authorization = "Bearer $($student.token)"; 'Idempotency-Key' = [guid]::NewGuid().ToString() }
$booking = Invoke-RestMethod "$base/events/$event/reservations" -Method Post -Headers $headers -ContentType 'application/json' -Body '{}'
$rid = $booking.reservation.id
$headers['Idempotency-Key'] = [guid]::NewGuid().ToString()
$payment = Invoke-RestMethod "$base/reservations/$rid/payments" -Method Post -Headers $headers -ContentType 'application/json' -Body '{"outcome":"success","delay_seconds":2}'
Invoke-RestMethod "$base/payments/$($payment.payment.id)" -Headers $headers
# Poll this GET until status becomes succeeded.
Invoke-RestMethod "$base/reservations/$rid" -Headers $headers
$headers['Idempotency-Key'] = [guid]::NewGuid().ToString()
Invoke-RestMethod "$base/reservations/$rid/cancel" -Method Post -Headers $headers
```

See [curl examples](docs/API_EXAMPLES.md). Replays return the original response snapshot; use GET for current state. Business rejections are replayed too; use a new key for a new attempt after conditions change.

## Verification and design

```sh
docker compose --profile tools run --rm test
docker compose --profile tools run --rm load
python scripts/resilience.py
```

Tests use real HTTP and services: booking races, concurrent key replay, fairness, expiry, failed/late payments, ownership, broker redelivery and direct SQL constraint enforcement. The load harness sends 10,000 booking requests with 100 in flight and verifies 500 distinct occupied seats plus 9,500 queued students. The outage test stops/restarts Redis and RabbitMQ and restarts the worker. See [measured results](docs/TEST_RESULTS.md).

Read [architecture, ERD and workflow](docs/ARCHITECTURE.md) and [deployment](docs/DEPLOYMENT.md). An event row lock serializes one concert's critical section, favoring correctness and simple fairness; results quantify its throughput. The tested deployment is local Docker. Public cloud hosting, real identity verification, real payments/refunds, notifications, HA and production metrics are outside this demo.
