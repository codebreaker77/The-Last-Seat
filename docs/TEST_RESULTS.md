# Verification results

Measured on 2026-10-17 using Docker Desktop/WSL2 on Windows, with 8 virtual CPUs and 7.72 GiB available to Docker. All services shared that machine, including the load generator. Python 3.12, two Uvicorn API processes, PostgreSQL 17, Redis 7, RabbitMQ 4, Nginx 1.28. Dependency versions are captured in `requirements.lock`.

## Integration tests

`docker compose --profile tools run --rm test`: **13 passed in 19.37 seconds**, against real services rather than mocked persistence or queues. [Raw summary](results/integration.txt).

Coverage includes 30 simultaneous attempts for one seat (one 201, 29 conflicts), 16 concurrent replays of one booking key (one reservation ID), changed-body key rejection, automatic expiry without a status read, FIFO queue promotion/skipping cancelled entries, no queue jumping, successful/failed/late payments, confirmed cancellation/refund, ownership checks, scheduled opening, invalid inputs, duplicate RabbitMQ delivery, cancellation/payment races, per-user active-seat limits, and direct SQL uniqueness enforcement.

## Full booking load

Command: `docker compose --profile tools run --rm load`. [Raw measured JSON](results/load.json).

| Measurement | Result |
| --- | --- |
| Total booking HTTP requests | 10,000 |
| Maximum client concurrency | 100 |
| Seats in the event | 500 |
| 201 reservation responses | 500 |
| 202 waiting responses | 9,500 |
| Active reservations / distinct occupied seats | 500 / 500 |
| Durable waiting entries | 9,500 |
| Unexpected responses / transport errors | 0 |
| Duration | 110.837 seconds |
| Throughput | 90.22 requests/second |
| Median / p95 / p99 latency | 975.00 / 1,873.13 / 2,288.60 ms |

The generator prepared 10,000 independent users directly in PostgreSQL before timing began. Each timed request used HTTP through Nginx, actual bearer authentication, a distinct idempotency key, PostgreSQL transactions, and queue allocation. Its post-run SQL checks verified capacity and seat uniqueness before releasing its own test holds/queue. A 3,600-second hold duration prevents expiry from changing counts during this capacity measurement. Short expiry and payment flows are tested separately.

This measures 10,000 total requests with 100 in flight, **not 10,000 simultaneous clients**, registration throughput, payment throughput, or sustained production capacity. It is one local run, without geographic/network latency or failover. The per-event lock intentionally limits hot-event throughput; the result supports correctness at the measured load, not an unlimited scaling claim. CI uses a smaller 1,000-request/50-concurrent run, still asserting 500 unique seats plus the remaining waiting students.

## Outage checks

`python scripts/resilience.py` stops this stack's Redis and RabbitMQ, verifies SQL fallback and durable pending payment, restores both, waits for payment confirmation, and restarts the worker. Results are saved in [resilience.json](results/resilience.json). Run only in a demo/test environment because it deliberately interrupts those services.

All four checks passed: Redis SQL fallback, durable pending payment during broker outage, automatic confirmation after broker recovery, and confirmation retained across worker restart. The API `/docs`, `/openapi.json`, and `/health/ready` also returned 200 through Nginx on the deployed local port.

## Reproduce

```sh
python scripts/setup.py
docker compose up --build -d
docker compose --profile tools run --rm test
docker compose --profile tools run --rm load
python scripts/resilience.py
```

The default proxy binds `127.0.0.1:18080`. Every run uses fresh event IDs and students. The full-load JSON is overwritten by subsequent full-load runs; preserve it separately if comparing environments.
