# EVENT_SCHEMA.md

All pipeline events share a common envelope and are published to a single
Redis Stream (`events`) keyed by `type`. JSON Schemas live under
`services/events/schemas/` and are validated at publish time.

## Envelope

```json
{
  "event_id":  "string (uuid v4)",
  "type":      "string (see table below)",
  "store_id":  "string",
  "camera_id": "string | null",
  "ts":        "string (RFC 3339, with timezone)",
  "session_id":"string | null",
  "track_id":  "integer | null",
  "role":      "customer | staff | unknown",
  "payload":   "object (type-specific)"
}
```

## Event types

| `type` | When emitted | `payload` fields |
|---|---|---|
| `person_entered` | track crosses entry tripwire inward | `direction`, `line_id` |
| `person_exited` | track crosses entry tripwire outward | `direction`, `line_id`, `session_duration_s` |
| `zone_entered` | track's foot-point enters a zone polygon | `zone_id`, `first_visit_in_session` (bool) |
| `zone_dwell` | track's foot-point leaves a zone polygon | `zone_id`, `dwell_s` |
| `checkout_observed` | track enters `cash_counter` zone and stays > 5 s | `zone_id`, `queue_position` |
| `pos_receipt` | POS ingester publishes a finalised receipt | `invoice_number`, `salesperson_id`, `total_amount`, `item_count`, `payment_mode` |
| `group_detected` | ≥ 2 tracks enter within 2 s and remain co-located | `group_id`, `member_track_ids` |
| `health_warning` | ingester anomaly (frame drop, empty detection window) | `source`, `reason` |

## Ordering & delivery guarantees

- **At-least-once** within Redis Streams; consumers use group + idempotency
  on `event_id`.
- Events for a given `session_id` are guaranteed monotonically
  non-decreasing by `ts` because they are produced by the same ingest
  worker.
- The aggregator tolerates POS receipts arriving out of order with respect
  to detection events.

## Backwards compatibility

The envelope is versioned implicitly by additive evolution: consumers must
ignore unknown fields, producers must not change existing field semantics.
A breaking change requires a new stream name (`events.v2`).
