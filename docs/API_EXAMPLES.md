# curl walkthrough

These examples use a POSIX shell and `jq`. Windows users can use the README PowerShell example or `/docs`.

```sh
BASE=http://127.0.0.1:18080
EVENT=00000000-0000-0000-0000-000000000001
TOKEN=$(curl -fsS "$BASE/users" -H 'Content-Type: application/json' -d '{"name":"Student"}' | jq -r .token)
curl -fsS "$BASE/events/$EVENT/seats"
BOOKING=$(curl -fsS "$BASE/events/$EVENT/reservations" \
  -H "Authorization: Bearer $TOKEN" -H 'Idempotency-Key: student-booking-001' \
  -H 'Content-Type: application/json' -d '{"seat_number":1}')
RID=$(printf '%s' "$BOOKING" | jq -r .reservation.id)
PAYMENT=$(curl -fsS "$BASE/reservations/$RID/payments" \
  -H "Authorization: Bearer $TOKEN" -H 'Idempotency-Key: student-payment-001' \
  -H 'Content-Type: application/json' -d '{"outcome":"success","delay_seconds":2}')
PID=$(printf '%s' "$PAYMENT" | jq -r .payment.id)
curl -fsS "$BASE/payments/$PID" -H "Authorization: Bearer $TOKEN"
# Poll until succeeded, then acknowledge confirmation:
curl -fsS -X POST "$BASE/reservations/$RID/confirm" \
  -H "Authorization: Bearer $TOKEN" -H 'Idempotency-Key: student-confirm-001'
curl -fsS "$BASE/reservations" -H "Authorization: Bearer $TOKEN"
curl -fsS -X POST "$BASE/reservations/$RID/cancel" \
  -H "Authorization: Bearer $TOKEN" -H 'Idempotency-Key: student-cancel-001'
```

For any seat and automatic queueing send `{}` to the booking endpoint. When full the response contains `waitlist` with its ID/status. Poll `GET /events/{id}/waitlist` using the same bearer token. A waiting entry includes position; an allocated entry includes its new `reservation_id`. Pay for that hold before its expiry. To leave while waiting, POST `/events/{id}/waitlist/cancel` with a fresh key. An allocated entry must be released by reservation cancellation.

Read `ADMIN_KEY` from your private `.env` into a shell variable, then create a scheduled event:

```sh
curl -fsS "$BASE/events" -H "X-Admin-Key: $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"6 PM concert","capacity":500,"hold_seconds":120,"opens_at":"2026-10-18T18:00:00+05:30"}'
```

Set `outcome` to `failure` for a failed payment. To see expiry compensation, create an event with `hold_seconds: 2` and submit payment with `delay_seconds: 4`. Its payment ends `refunded`; the reservation stays `expired`. No real card details or money are involved.
