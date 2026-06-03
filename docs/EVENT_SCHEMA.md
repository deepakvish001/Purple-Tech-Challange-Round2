# EVENT_SCHEMA.md

All pipeline events share a common envelope and are published to a single
Redis Stream (`events`) keyed by `type`. JSON Schemas live under
`services/events/schemas/` and are validated at publish time.

## Envelope

```json
{
  "event_id":     "string (uuid v4)",
  "type":         "string (see table below)",
  "store_id":     "string",
  "camera_id":    "string (cam_1_top | cam_2_bottom | cam_3_entry | cam_4_boh | cam_5_cash | pos | aggregator)",
  "ts":           "string (RFC 3339, with timezone)",
  "session_id":   "string | null",
  "track_id":     "string | null",
  "embedding_id": "string | null",
  "role":         "customer | staff | unknown",
  "payload":      "object (type-specific)"
}
```

`session_id` is filled by the aggregator after cross-camera
reconciliation. Ingest workers emit `null` for in-store cameras and the
CAM 3 short-lived candidate id for entry events.

## Event types

| `type` | Emitted by | `payload` fields |
|---|---|---|
| `person_entered` | cam_3_entry | `direction` ("in"), `line_id` |
| `person_exited`  | cam_3_entry | `direction` ("out"), `line_id`, `session_duration_s` |
| `zone_entered`   | cam_1_top, cam_2_bottom | `zone_id`, `first_visit_in_session` (bool) |
| `zone_dwell`     | cam_1_top, cam_2_bottom | `zone_id`, `dwell_s` |
| `checkout_observed` | cam_5_cash | `zone_id`, `queue_position` |
| `staff_observed` | cam_4_boh | `{}` |
| `pos_receipt`    | pos | `invoice_number`, `salesperson_id`, `total_amount`, `item_count`, `payment_mode` |
| `health_warning` | any ingester | `source`, `reason` |

## Ordering & delivery guarantees

- **At-least-once** within Redis Streams; consumers use group +
  idempotency on `event_id`.
- Events from a single camera worker are monotonically non-decreasing in
  `ts`.
- Events **across cameras** are not strictly ordered — the aggregator
  buffers a 30 s window and tolerates out-of-order arrivals.
- The aggregator tolerates POS receipts arriving out of order with
  respect to detection events.

## Backwards compatibility

The envelope is versioned implicitly by additive evolution: consumers
must ignore unknown fields, producers must not change existing field
semantics. A breaking change requires a new stream name (`events.v2`).
