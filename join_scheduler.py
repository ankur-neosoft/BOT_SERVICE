# join_scheduler.py
"""
Schedule joins for auctions from TRAN_AUCTION table and start ws_adapter.start_ws_adapter
for each auction at its auction_start_date.

Usage:
    python join_scheduler.py

Make sure ws_adapter.py is in the same directory or on PYTHONPATH.
"""

import os
import time
import logging
from datetime import datetime, timezone
import multiprocessing
import threading

import psycopg2
import psycopg2.extras

# --- CONFIG (use your provided values) ---
SECRET_KEY = "123"
DEV_DB_NAME = "cfs_dev"
DEV_DB_USER = "postgres"
DEV_DB_PASSWORD = "postgres"
DEV_DB_HOST = "localhost"
DEV_DB_PORT = "5432"

DB_DSN = {
    "dbname": DEV_DB_NAME,
    "user": DEV_DB_USER,
    "password": DEV_DB_PASSWORD,
    "host": DEV_DB_HOST,
    "port": DEV_DB_PORT,
}

# ws_adapter credentials (these must match what ws_adapter expects)
WS_USERNAME = "bansari"    # change if needed
WS_PASSWORD = "12345"      # change if needed

# If DB datetimes are naive, assume this timezone (default: UTC). Change if DB uses local time.
ASSUME_DB_TZ = timezone.utc

# Query to fetch auctions
SELECT_SQL = """
SELECT
    "AUCTION_ID",
    "AUCTION_START_DATE",
    "AUCTION_EXPIRY_DATE",
    "INSPECTION_DATE",
    "AUCTION_VISIBLE_DATE"
FROM public."TRAN_AUCTION"
ORDER BY "AUCTION_ID" ASC;
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("join_scheduler")


def db_connect():
    return psycopg2.connect(**DB_DSN)


def fetch_auctions():
    conn = db_connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(SELECT_SQL)
            rows = cur.fetchall()
            return rows
    finally:
        conn.close()


def _normalize_dt(dt):
    """
    Ensure dt is a timezone-aware datetime. If naive, assume ASSUME_DB_TZ.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ASSUME_DB_TZ)
    return dt.astimezone(timezone.utc)


def spawn_ws_adapter_process(auction_id_str):
    """
    Spawn a separate process that imports ws_adapter and calls start_ws_adapter.
    We import inside the child process to avoid importing ws_adapter (and starting anything)
    in the parent scheduler process.
    """
    def target(auc_id):
        # child process code
        try:
            import ws_adapter
        except Exception as e:
            logger.exception("Child: failed importing ws_adapter: %s", e)
            return
        logger.info("Child: starting ws_adapter for auction %s", auc_id)
        try:
            # call start_ws_adapter with credentials and auction_pk
            ws_adapter.start_ws_adapter(WS_USERNAME, WS_PASSWORD, auction_pk=str(auc_id))
        except Exception:
            logger.exception("Child: ws_adapter.start_ws_adapter raised an exception")
    p = multiprocessing.Process(target=target, args=(auction_id_str,), daemon=False)
    p.start()
    logger.info("Spawned subprocess pid=%s for auction %s", p.pid, auction_id_str)
    return p


def schedule_join(auction_id, start_dt):
    now = datetime.now(timezone.utc)
    if start_dt is None:
        logger.warning("Auction %s has no start date; skipping", auction_id)
        return

    # normalize to timezone-aware (utc)
    start_dt = _normalize_dt(start_dt).astimezone(timezone.utc)

    if start_dt <= now:
        logger.info("Auction %s start time %s <= now (%s) -> joining immediately", auction_id, start_dt.isoformat(), now.isoformat())
        spawn_ws_adapter_process(auction_id)
    else:
        delta = (start_dt - now).total_seconds()
        logger.info("Scheduling join for auction %s at %s (in %.1f seconds)", auction_id, start_dt.isoformat(), delta)

        # use a timer to spawn the process at the right time
        def timer_callback():
            logger.info("Timer fired - joining auction %s (scheduled %s)", auction_id, start_dt.isoformat())
            spawn_ws_adapter_process(auction_id)

        t = threading.Timer(delta, timer_callback)
        t.daemon = True
        t.start()


def main():
    try:
        rows = fetch_auctions()
    except Exception as e:
        logger.exception("Failed to fetch auctions from DB: %s", e)
        return

    if not rows:
        logger.info("No auctions found in TRAN_AUCTION")
        return

    logger.info("Found %d auctions; scheduling joins...", len(rows))
    for r in rows:
        auc_id = r.get("AUCTION_ID")
        start_dt = r.get("AUCTION_START_DATE")
        # log other datetime columns for visibility
        logger.debug("Auction %s: start=%s expiry=%s inspection=%s visible=%s",
                     auc_id, start_dt, r.get("AUCTION_EXPIRY_DATE"), r.get("INSPECTION_DATE"), r.get("AUCTION_VISIBLE_DATE"))
        schedule_join(auc_id, start_dt)

    logger.info("Scheduler configured. Waiting for scheduled joins. Press Ctrl-C to stop.")
    try:
        # main thread idle loop to keep script alive for timers
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Scheduler shutting down (Ctrl-C)")

if __name__ == "__main__":
    main()
