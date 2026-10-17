CREATE TABLE IF NOT EXISTS users (
 id uuid PRIMARY KEY, name text NOT NULL, token_hash text UNIQUE NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS events (
 id uuid PRIMARY KEY, name text NOT NULL, capacity integer NOT NULL CHECK(capacity > 0),
 hold_seconds integer NOT NULL CHECK(hold_seconds > 0), opens_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS seats (
 event_id uuid REFERENCES events ON DELETE CASCADE, number integer NOT NULL,
 PRIMARY KEY(event_id,number)
);
CREATE TABLE IF NOT EXISTS reservations (
 id uuid PRIMARY KEY, event_id uuid NOT NULL REFERENCES events,
 seat_number integer NOT NULL, user_id uuid NOT NULL REFERENCES users,
 status text NOT NULL CHECK(status IN ('held','confirmed','cancelled','expired','payment_failed')),
 expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(event_id,seat_number) REFERENCES seats(event_id,number)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_owner_per_seat ON reservations(event_id,seat_number)
 WHERE status IN ('held','confirmed');
CREATE UNIQUE INDEX IF NOT EXISTS one_seat_per_user ON reservations(event_id,user_id)
 WHERE status IN ('held','confirmed');
CREATE INDEX IF NOT EXISTS expiry_scan ON reservations(event_id,expires_at) WHERE status='held';
CREATE INDEX IF NOT EXISTS history_lookup ON reservations(user_id,created_at DESC);
CREATE TABLE IF NOT EXISTS payments (
 id uuid PRIMARY KEY, reservation_id uuid NOT NULL UNIQUE REFERENCES reservations,
 outcome text NOT NULL CHECK(outcome IN ('success','failure')),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','succeeded','failed','refunded')),
 due_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS outbox (
 id bigserial PRIMARY KEY, payment_id uuid NOT NULL UNIQUE REFERENCES payments,
 due_at timestamptz NOT NULL, published_at timestamptz
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(due_at) WHERE published_at IS NULL;
CREATE TABLE IF NOT EXISTS waitlist (
 id bigserial PRIMARY KEY, event_id uuid NOT NULL REFERENCES events,
 user_id uuid NOT NULL REFERENCES users, status text NOT NULL DEFAULT 'waiting'
 CHECK(status IN ('waiting','allocated','cancelled')),
 reservation_id uuid REFERENCES reservations, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX IF NOT EXISTS one_waiting_entry ON waitlist(event_id,user_id) WHERE status='waiting';
CREATE INDEX IF NOT EXISTS waitlist_fifo ON waitlist(event_id,id) WHERE status='waiting';
CREATE TABLE IF NOT EXISTS idempotency (
 user_id uuid REFERENCES users, key text NOT NULL, fingerprint text NOT NULL,
 code integer NOT NULL, response jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(user_id,key)
);
