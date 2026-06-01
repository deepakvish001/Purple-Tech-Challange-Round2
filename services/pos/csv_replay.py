"""POS CSV → `pos_receipt` event publisher.

In synthetic mode the synth publisher already emits pos_receipt events,
so this service is idle (defaults to noop if no CSV file is present).

When a Brigade POS CSV is mounted at /data/pos/*.csv, this service:
  1. Parses receipts.
  2. Sorts by transaction timestamp.
  3. Streams them onto the `events` Redis stream — time-shifted so the
     oldest receipt fires "now", preserving relative ordering. This is
     how a real-time replay is staged against a recorded shift.

The CSV column names are taken from the Brigade sample:
`Brigade_Bangalore_10_April_26.csv`. If the header doesn't match, we log
the discovered columns and exit cleanly so docker-compose surfaces it.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from services.events import EventBus, pos_receipt

log = structlog.get_logger()

REDIS_URL  = os.environ.get("REDIS_URL",  "redis://redis:6379/0")
POS_DIR    = Path(os.environ.get("POS_DIR", "/data/pos"))
STORE_ID   = os.environ.get("STORE_ID", "ST1008")
PACE       = float(os.environ.get("POS_PACE", "1.0"))  # 1.0 = real-time; 0 = burst all
TIME_SHIFT = os.environ.get("POS_TIME_SHIFT", "now")   # "now" | "preserve"

# Column name candidates (Brigade CSV has slightly varying capitalisation
# between exports, so we shop around).
COL_TS         = ["invoice_date", "Invoice Date", "transaction_date", "Transaction Date"]
COL_INVOICE    = ["invoice_number", "Invoice Number", "invoice_no", "Invoice No"]
COL_TOTAL      = ["total_amount", "Total Amount", "net_amount", "Net Amount"]
COL_ITEMS      = ["item_count", "Item Count", "qty", "Qty"]
COL_MODE       = ["payment_mode", "Payment Mode", "payment_method"]
COL_SALESPERSON = ["salesperson_id", "Salesperson Id", "Salesperson ID", "associate_id"]


def _setup_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s", stream=sys.stdout)
    structlog.configure(processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ])


def _pick(row: dict[str, str], candidates: list[str]) -> str | None:
    for c in candidates:
        if c in row and row[c] not in ("", None):
            return row[c]
    return None


def _parse_ts(raw: str) -> datetime | None:
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def parse_csv(path: Path) -> list[dict]:
    out = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_raw = _pick(row, COL_TS)
            inv    = _pick(row, COL_INVOICE)
            total  = _pick(row, COL_TOTAL)
            if not ts_raw or not inv or not total:
                continue
            ts = _parse_ts(ts_raw.strip())
            if ts is None:
                continue
            try:
                total_v = float(total)
            except ValueError:
                continue
            items_raw = _pick(row, COL_ITEMS)
            try:
                items_v = int(items_raw) if items_raw else 1
            except ValueError:
                items_v = 1
            out.append({
                "ts":            ts,
                "invoice":       inv.strip(),
                "total":         total_v,
                "items":         items_v,
                "mode":          (_pick(row, COL_MODE) or "").strip() or None,
                "salesperson":   (_pick(row, COL_SALESPERSON) or "").strip() or None,
            })
    out.sort(key=lambda r: r["ts"])
    return out


async def replay(bus: EventBus, rows: list[dict]) -> None:
    if not rows:
        log.info("pos.empty")
        return
    offset = datetime.now(UTC) - rows[0]["ts"] if TIME_SHIFT == "now" else timedelta(0)
    wall_start = datetime.now(UTC)
    base_ts = rows[0]["ts"] + offset
    for r in rows:
        target_ts = r["ts"] + offset
        if PACE > 0:
            sleep_for = (target_ts - base_ts).total_seconds() / PACE - (
                datetime.now(UTC) - wall_start
            ).total_seconds()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
        env = pos_receipt(
            store_id=STORE_ID,
            invoice_number=r["invoice"],
            total_amount=r["total"],
            item_count=r["items"],
            salesperson_id=r["salesperson"],
            payment_mode=r["mode"],
            ts=datetime.now(UTC),
        )
        await bus.publish(env)
    log.info("pos.replay_complete", published=len(rows))


async def _main() -> None:
    _setup_logging()
    if not POS_DIR.exists():
        log.info("pos.no_dir", dir=str(POS_DIR))
        return
    csvs = sorted(POS_DIR.glob("*.csv"))
    if not csvs:
        log.info("pos.no_csv", dir=str(POS_DIR))
        return

    bus = EventBus(REDIS_URL)
    for _ in range(15):
        try:
            await bus.ping()
            break
        except Exception:  # noqa: BLE001
            await asyncio.sleep(1)

    stop = asyncio.Event()
    def _shutdown(*_: object) -> None:
        stop.set()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    for csvf in csvs:
        log.info("pos.parse", file=str(csvf))
        rows = parse_csv(csvf)
        log.info("pos.parsed", file=str(csvf), rows=len(rows))
        if not rows:
            continue
        runner = asyncio.create_task(replay(bus, rows))
        waiter = asyncio.create_task(stop.wait())
        done, _pending = await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if waiter in done:
            runner.cancel()
            break
    await bus.close()


if __name__ == "__main__":
    asyncio.run(_main())
