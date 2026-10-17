# Deployment

## Local running deployment

Prerequisites: Docker Engine/Desktop with Compose v2+, Python 3.9+ for credential generation, an available port 18080. No host database, Redis, RabbitMQ, or Python packages are required.

```sh
python scripts/setup.py
docker compose up --build -d
docker compose ps
curl http://127.0.0.1:18080/health/ready
```

Interactive API: <http://127.0.0.1:18080/docs>. OpenAPI: <http://127.0.0.1:18080/openapi.json>. The seeded concert UUID is `00000000-0000-0000-0000-000000000001`, with 500 seats and 120-second holds. It opens when the database is first initialized. Create another event to set a real 6 PM opening time, including UTC offset, e.g. `2026-10-18T18:00:00+05:30`.

`scripts/setup.py` creates random database, broker, and admin credentials in ignored `.env`, preserving an existing file. Keep that file private. Do not change database credentials against an existing volume without rotating the actual database role. Do not delete volumes to perform routine updates.

Stop without discarding data: `docker compose down`. Start again: `docker compose up -d`. View worker activity: `docker compose logs --tail=100 worker`. Source changes require `docker compose up --build -d`.

## Verify on your deployment

```sh
docker compose --profile tools run --rm test
docker compose --profile tools run --rm load
python scripts/resilience.py
```

Tests create isolated events in the same database. Load testing seeds identities directly into SQL outside its timed phase, then sends actual booking HTTP requests through Nginx; this avoids conflating registration with booking throughput. It records JSON under `docs/results/` and releases its test holds/queue afterward, preserving history. The outage script temporarily stops this project's broker and Redis, verifies fallback and durable payment recovery, then restores them in a `finally` block. Run it on a disposable/demo environment, not while serving real users.

## Public server

The repository includes a complete Docker deployment. A public internet deployment additionally needs a server/domain and credentials; none are stored or assumed. The local deployment is verified; it is not represented as a public hosted service.

On an Ubuntu server with Docker Compose, clone this repository, generate `.env`, and run the same commands. Keep PostgreSQL/Redis/RabbitMQ unexposed. Recommended: leave the application on loopback port 18080 and put a TLS proxy on that server using the Caddy example below. Point a domain's DNS to the server, replace the example domain, and install/run Caddy through your server's package/service management.

```caddyfile
seat.example.com {
    reverse_proxy 127.0.0.1:18080
}
```

Restrict inbound traffic to SSH and HTTPS/HTTP needed by that proxy. Configure campus identity and request quotas before inviting untrusted traffic. For an isolated LAN demonstration, set `BIND_ADDRESS=0.0.0.0` in `.env`, recreate the proxy (`docker compose up -d proxy`), and use the host's IP. Default local binding is intentional.

Back up PostgreSQL with your platform's scheduled `pg_dump` or physical backup tooling and exercise restores. Persist both named volumes. For updates, back up first, pull the desired commit and rebuild; review schema changes because this project currently bootstraps its initial schema rather than running migrations. Never use `docker compose down -v` for a routine redeploy.
