"""
ws_adapter.py (final, patched)

- Logs in via session cookie (requests.Session)
- Starts a persistent WS for receiving snapshots (auto-reconnect)
- Converts join/update messages to AuctionPlayer snapshot shape (caches item metadata)
- Monkey-patches AucApi.submit_bid and AuctionPlayer.submit_bid to use ephemeral WS per bid
  (open -> send -> await response -> close) with:
    - per-auction rate limiting
    - pre-check against current live bid
    - caching of MIN_BID_AMOUNT to avoid fallback to 1
"""

import json
import time
import threading
import uuid
import logging
import random
import requests
import websocket  # pip install websocket-client

# Import AuctionPlayer and AucApi
from AuctionPlayer import process_external_snapshot
import AuctionPlayer
import AucApi  # we will monkey-patch submit_bid here

# ---------- CONFIG ----------
HTTP_BASE = "http://localhost:8000"
LOGIN_PATH = "/CFS/master/login"
WS_BASE = "ws://localhost:8000"
WS_JOIN_PATH = "/CFS/ws/auction/join/"

USERNAME = "bansari"
PASSWORD = "12345"
AUCTION_PK = "423d9642-fad7-43a2-ba3a-503199225809"
REQUEST_ID = str(uuid.uuid4())

# Ephemeral submit tuning
EPHEMERAL_CONNECT_TIMEOUT = 4     # seconds to connect
EPHEMERAL_RECV_TIMEOUT = 4        # seconds to wait for response
EPHEMERAL_MIN_INTERVAL = 0.8      # seconds between ephemeral submits per auction
EPHEMERAL_JITTER = 0.25           # random jitter added

# Persistent WS ping settings (must satisfy ping_interval > ping_timeout)
PERSISTENT_PING_INTERVAL = 30
PERSISTENT_PING_TIMEOUT = 10

logger = logging.getLogger("ws_adapter")
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())

# global persistent websocket object (live ws connection). Set in _on_open.
_global_ws = None
_global_ws_lock = threading.Lock()

# Cache last-known per-auction item metadata so updates missing fields don't regress min_inc
_LAST_ITEM_MAP = {}  # auction_id -> {item_id: item_block}
# Track last ephemeral submit times per auction (rate-limit)
_LAST_EPHEMERAL_SUBMIT = {}  # auction_id -> last_ts
_LAST_EPHEMERAL_LOCK = threading.Lock()


def login_and_get_session(username, password):
    s = requests.Session()
    login_url = HTTP_BASE + LOGIN_PATH
    payload = {"username": username, "password": password}
    r = s.post(login_url, json=payload, allow_redirects=True, timeout=10)
    if r.status_code not in (200, 302):
        raise RuntimeError(f"Login failed: {r.status_code} {r.text[:400]}")
    sessionid = r.json()
    if not sessionid['Your Token']:
        raise RuntimeError("No session cookie received after login")
    logger.info("Login cookies: %s", s.cookies.get_dict())
    print("Cookies:", s.cookies.get_dict())
    logger.info("Logged in successfully, session cookie found.")
    return s


def _send_via_ws(payload: dict) -> bool:
    """
    Use persistent WS to send a payload (not used for ephemeral submits but kept as a helper).
    """
    global _global_ws
    with _global_ws_lock:
        if _global_ws is None:
            logger.error("WebSocket is not connected - cannot send payload")
            return False
        try:
            _global_ws.send(json.dumps(payload))
            logger.info("WS SENT (persistent): %s", payload)
            return True
        except Exception:
            logger.exception("Failed to send via persistent WS - marking dead")
            try:
                _global_ws.close()
            except Exception:
                pass
            _global_ws = None
            return False


def _convert_join_response_to_snapshot(join_data: dict):
    """
    Robust conversion of join response into AuctionPlayer snapshot.
    Always returns a dict keyed by an auction_id string:
      { "<auction_id>": { "AUCTION_ITEMS": { pid: item_block, ... }, "MIN_BID_AMOUNT": ... } }

    If the incoming payload doesn't include an auction id, falls back to global AUCTION_PK,
    then to 'unknown_auction' so downstream code always sees the expected shape.
    """
    try:
        # Try to extract auction id from common locations
        auction_id = (
            join_data.get("auction_id")
            or join_data.get("auctionId")
            or join_data.get("auction")
            or None
        )

        products = None

        # top-level products_data
        if isinstance(join_data.get("products_data"), (list, tuple)):
            products = list(join_data["products_data"])

        # top-level products
        if products is None and isinstance(join_data.get("products"), (list, tuple)):
            products = list(join_data["products"])

        # auction_data: list or dict
        if products is None and "auction_data" in join_data:
            ad = join_data["auction_data"]
            # list form: find first element that contains products or use first element if it's a dict
            if isinstance(ad, (list, tuple)) and len(ad) > 0:
                first_with_products = None
                for el in ad:
                    if isinstance(el, dict) and "products" in el and isinstance(el["products"], (list, tuple)):
                        first_with_products = el
                        break
                if first_with_products is None:
                    # use first element if it's a dict that itself looks like a product container
                    if isinstance(ad[0], dict):
                        first_with_products = ad[0]
                if first_with_products:
                    products = first_with_products.get("products") or []
                    auction_id = auction_id or first_with_products.get("auction_id") or first_with_products.get("auctionId") or auction_id
            elif isinstance(ad, dict):
                # dict keyed by auction id -> look for a value that contains products
                found = None
                for k, v in ad.items():
                    if isinstance(v, dict) and isinstance(v.get("products"), (list, tuple)):
                        found = (k, v)
                        break
                if found:
                    k, v = found
                    products = v.get("products") or []
                    auction_id = auction_id or k
                elif isinstance(ad.get("products"), (list, tuple)):
                    products = ad.get("products")

        # nested data object with products
        if products is None and isinstance(join_data.get("data"), dict):
            dd = join_data["data"]
            if isinstance(dd.get("products"), (list, tuple)):
                products = dd["products"]
            elif isinstance(dd.get("products_data"), (list, tuple)):
                products = dd["products_data"]

        # final fallback: try to coerce any plausible products container
        if products is None:
            # try to find any list under join_data that looks like products
            for k, v in join_data.items():
                if k in ("items", "products_list") and isinstance(v, (list, tuple)):
                    products = list(v)
                    break

        # If still nothing, return empty snapshot (but keep expected dict shape)
        if not products:
            logger.debug("No products found in join_data; keys=%s", list(join_data.keys()))
            # determine fallback auction id
            aid = str(auction_id or globals().get("AUCTION_PK") or "unknown_auction")
            return {aid: {"AUCTION_ITEMS": {}}}

        # Build item_map while guarding against malformed entries
        item_map = {}
        for p in products:
            if not isinstance(p, dict):
                continue
            pid = p.get("auction_product_id") or p.get("id")
            if pid is None:
                # skip entries without an id
                continue
            try:
                raw_min = p.get("minimum_increment_price")
                min_bid = float(raw_min) if raw_min is not None else None
            except Exception:
                min_bid = None

            try:
                best_price = float(p.get("current_bid") or p.get("starting_bid") or 0)
            except Exception:
                best_price = 0.0
            try:
                threshold = float(p.get("reserve_price") or 0)
            except Exception:
                threshold = 0.0
            try:
                max_allowed = float(p.get("max_allowed_price") or 999999999)
            except Exception:
                max_allowed = 999999999

            item = {
                "ITEM_ID": pid,
                "BEST_PRICE": best_price,
                "THRESHOLD_PRICE": threshold,
                "MIN_BID_AMOUNT": min_bid if min_bid is not None else 1,
                "MAX_BID_AMOUNT": max_allowed
            }
            item_map[pid] = item

        # Determine an auction id string to key the snapshot
        auction_key = str(auction_id or globals().get("AUCTION_PK") or "unknown_auction")

        # Cache per-item metadata
        if auction_key:
            _LAST_ITEM_MAP[auction_key] = {k: v.copy() for k, v in item_map.items()}

        auction_block = {"AUCTION_ITEMS": item_map}
        # if all items share same MIN_BID_AMOUNT, include it
        if item_map:
            item_mins = {v.get("MIN_BID_AMOUNT") for v in item_map.values() if v.get("MIN_BID_AMOUNT") is not None}
            if len(item_mins) == 1:
                auction_block["MIN_BID_AMOUNT"] = next(iter(item_mins))

        return {auction_key: auction_block}

    except Exception:
        logger.exception("Error converting join response to snapshot")
        # final safe fallback
        aid = str(globals().get("AUCTION_PK") or "unknown_auction")
        return {aid: {"AUCTION_ITEMS": {}}}


def _convert_update_to_snapshot(update_msg: dict):
    """
    Convert 'update_bid' message into a snapshot. Use cached MIN_BID_AMOUNT when update omits it.
    """
    try:
        product = update_msg.get("product") or {}
        pid = product.get("auction_product_id") or product.get("id")
        auction_id = update_msg.get("auction_id") or update_msg.get("auctionId") or AUCTION_PK
        raw_min = product.get("minimum_increment_price")
        if raw_min is None:
            # fallback to cached value if present
            try:
                cached = _LAST_ITEM_MAP.get(auction_id, {})
                min_bid = cached.get(pid, {}).get("MIN_BID_AMOUNT", 1)
            except Exception:
                min_bid = 1
        else:
            min_bid = float(raw_min)
        item_block = {
            "ITEM_ID": pid,
            "BEST_PRICE": float(product.get("current_bid") or product.get("starting_bid") or 0),
            "THRESHOLD_PRICE": float(product.get("reserve_price") or 0),
            "MIN_BID_AMOUNT": min_bid,
            "MAX_BID_AMOUNT": float(product.get("max_allowed_price") or 999999999)
        }
        # Update cache
        if auction_id not in _LAST_ITEM_MAP:
            _LAST_ITEM_MAP[auction_id] = {}
        _LAST_ITEM_MAP[auction_id][pid] = item_block.copy()

        snapshot = {
            auction_id: {
                "AUCTION_ITEMS": {
                    pid: item_block
                }
            }
        }
        snapshot[auction_id]["MIN_BID_AMOUNT"] = item_block["MIN_BID_AMOUNT"]
        return snapshot
    except Exception:
        logger.exception("Error converting update to snapshot")
        return {}


# WebSocket callbacks
def _on_open(ws):
    global _global_ws
    with _global_ws_lock:
        _global_ws = ws
    logger.info("WS open. Sending join_auction for auction=%s", AUCTION_PK)
    payload = {"pk": AUCTION_PK, "action": "join_auction", "request_id": str(uuid.uuid4())}
    try:
        ws.send(json.dumps(payload))
    except Exception:
        logger.exception("Failed to send join payload on open")


def _on_message(ws, message):
    try:
        data = json.loads(message)
    except Exception:
        logger.debug("Non-json WS message: %s", message)
        return
    if data.get("status") == "success" and "auction_data" in data:
        logger.info("Join confirmed for auction: %s", data.get("auction_id"))
        snap = _convert_join_response_to_snapshot(data)
        if snap:
            process_external_snapshot(snap)
        return
    action = data.get("action") or data.get("type")
    if action == "update_bid" or data.get("event") == "update_bid":
        snap = _convert_update_to_snapshot(data)
        if snap:
            process_external_snapshot(snap)
        return
    if action == "bid_response" or data.get("status") == "bid_response":
        logger.info("Bid response from server: %s", data)
        return
    logger.debug("Unhandled WS message: %s", data)


def _on_error(ws, error):
    logger.error("WS error: %s", error)


def _on_close(ws, code, reason):
    logger.info("WS closed: %s %s", code, reason)
    global _global_ws
    with _global_ws_lock:
        _global_ws = None


def start_ws_adapter(username, password, auction_pk=None):
    """
    - login -> session (used for cookie header)
    - start persistent receive WS in reconnect loop
    - patch submit_bid to ephemeral submit
    """
    global _global_ws, AUCTION_PK
    if auction_pk:
        AUCTION_PK = auction_pk

    session = login_and_get_session(username, password)

    cookies_dict = session.cookies.get_dict()
    cookie_parts = [f"{k}={v}" for k, v in cookies_dict.items()]
    cookie_header = "; ".join(cookie_parts)

    origin = "http://localhost:8000"
    host = "localhost:8000"
    ws_url = WS_BASE + WS_JOIN_PATH
    headers = [
        f"Cookie: {cookie_header}",
        f"Host: {host}",
        "Connection: Upgrade",
        "Upgrade: websocket",
        "User-Agent: ws-adapter/1.0"
    ]

    logger.info("Connecting to WS: %s", ws_url)

    # ---------------- ephemeral submit implementation ----------------
    def _ephemeral_ws_submit(auction_id: str, item_id: int, bid_amount: float, user_id: str = None, min_bid_amount: float = None, timeout: int = EPHEMERAL_CONNECT_TIMEOUT):
        """
        Ephemeral websocket submit with robust live re-checks & retries.

        Behavior:
          - Rate-limit per-auction (existing logic).
          - Before each attempt, fetch live current bid (via AucApi.get_current_bidding_details if available,
            otherwise use cached _LAST_ITEM_MAP).
          - If proposed bid_amount <= live_current -> compute next_bid = live_current + min_inc and use that.
          - Attempt send; if no response or server rejects, re-check live_current and retry with updated bid.
          - Max attempts bounded (3 by default).
        """
        MAX_ATTEMPTS = 3
        RETRY_BASE = 0.25  # seconds base backoff
        attempt = 0

        # Rate-limit per-auction (existing)
        with _LAST_EPHEMERAL_LOCK:
            last = _LAST_EPHEMERAL_SUBMIT.get(auction_id, 0)
            wait_needed = EPHEMERAL_MIN_INTERVAL + random.random() * EPHEMERAL_JITTER - (time.time() - last)
            if wait_needed > 0:
                logger.debug("Rate-limit: sleeping %.3fs before ephemeral submit for auction %s", wait_needed, auction_id)
                time.sleep(wait_needed)
            _LAST_EPHEMERAL_SUBMIT[auction_id] = time.time()

        # helper to obtain live current bid and min increment
        def _fetch_live_details():
            live_current = None
            live_min_inc = None
            try:
                if hasattr(AucApi, "get_current_bidding_details"):
                    details = AucApi.get_current_bidding_details(auction_id, item_id)
                    if isinstance(details, dict):
                        # try many possible keys
                        for key in ("current_bid", "best_price", "current_highest", "current_price"):
                            if key in details and details[key] is not None:
                                live_current = float(details[key])
                                break
                        # min increment keys
                        for mkey in ("minimum_increment_price", "min_inc", "min_bid_amount", "minimum_increment"):
                            if mkey in details and details[mkey] is not None:
                                try:
                                    live_min_inc = float(details[mkey])
                                    break
                                except Exception:
                                    pass
                    elif isinstance(details, (int, float)):
                        live_current = float(details)
            except Exception:
                logger.debug("Failed to fetch live details via AucApi.get_current_bidding_details", exc_info=True)

            # fallback to cached values in _LAST_ITEM_MAP
            try:
                cached = _LAST_ITEM_MAP.get(auction_id, {})
                cached_item = cached.get(item_id) or cached.get(str(item_id))
                if live_current is None and cached_item:
                    live_current = float(cached_item.get("BEST_PRICE", 0))
                if live_min_inc is None and (min_bid_amount is not None):
                    live_min_inc = float(min_bid_amount)
                if live_min_inc is None and cached_item:
                    live_min_inc = float(cached_item.get("MIN_BID_AMOUNT", 1))
            except Exception:
                pass

            # final defaults
            if live_current is None:
                live_current = 0.0
            if live_min_inc is None:
                live_min_inc = 1.0

            return live_current, live_min_inc

        # attempt loop
        while attempt < MAX_ATTEMPTS:
            attempt += 1

            # Re-fetch live values before each attempt
            live_current, live_min_inc = _fetch_live_details()

            # Decide effective bid to send: must be > live_current
            effective_bid = float(bid_amount)
            if effective_bid <= live_current:
                # raise to next valid increment
                effective_bid = float(live_current) + float(live_min_inc)
                # round or normalize if needed (here we keep float)
                logger.debug("Adjusted proposed bid upward because live_current(%.2f) >= proposed(%.2f). New bid=%.2f (min_inc=%.2f)",
                             live_current, bid_amount, effective_bid, live_min_inc)

            payload = {"action": "place_bid", "product_id": item_id, "bid_amount": float(effective_bid)}

            # ephemeral headers (same as before)
            ephemeral_headers = [
                f"Cookie: {cookie_header}",
                f"Host: {host}",
                "Connection: Upgrade",
                "Upgrade: websocket",
                "User-Agent: ws-ephemeral/1.0"
            ]

            try:
                ws_conn = websocket.create_connection(ws_url, timeout=timeout, header=ephemeral_headers, origin=origin)
            except Exception as e:
                logger.exception("Ephemeral WS connect failed: %s", e)
                return {"status": "error", "message": f"connect-failed:{e}"}

            try:
                try:
                    ws_conn.settimeout(EPHEMERAL_RECV_TIMEOUT)
                except Exception:
                    pass

                ws_conn.send(json.dumps(payload))
                logger.info("EPHEMERAL WS SENT: %s (attempt %d/%d)", payload, attempt, MAX_ATTEMPTS)

                # Wait for a response with recv(); if none, we'll treat as retryable
                try:
                    resp = ws_conn.recv()
                except Exception as e:
                    logger.warning("Ephemeral recv failed on attempt %d: %s", attempt, e)
                    try:
                        ws_conn.close()
                    except Exception:
                        pass
                    # refresh live_current and possibly retry
                    if attempt < MAX_ATTEMPTS:
                        # small backoff with jitter
                        backoff = RETRY_BASE * (2 ** (attempt - 1)) + random.random() * 0.1
                        logger.debug("Retrying ephemeral submit after backoff %.3fs (attempt %d/%d)", backoff, attempt + 1, MAX_ATTEMPTS)
                        time.sleep(backoff)
                        continue
                    else:
                        return {"status": "error", "message": f"no-response:{e}"}

                # parse response (may be empty string or JSON)
                try:
                    resp_j = json.loads(resp) if resp else {"raw": resp}
                except Exception:
                    resp_j = {"raw": resp}

                ws_conn.close()

                # If server returned explicit error in response JSON, consider retryable
                # Here we treat absence of explicit success as retryable as well.
                # You can refine based on server's actual response contract.
                if isinstance(resp_j, dict):
                    # If server returns status:error -> maybe retry (or abort depending on message)
                    resp_status = resp_j.get("status") or resp_j.get("result") or None
                    if resp_status and str(resp_status).lower() in ("error", "failed", "false"):
                        logger.info("Server responded with error: %s (attempt %d/%d)", resp_j, attempt, MAX_ATTEMPTS)
                        if attempt < MAX_ATTEMPTS:
                            backoff = RETRY_BASE * (2 ** (attempt - 1)) + random.random() * 0.1
                            time.sleep(backoff)
                            # loop will re-fetch live_current and compute new effective_bid
                            continue
                        else:
                            return {"status": "error", "message": {"via": "ephemeral-ws", "response": resp_j, "payload": payload}}
                    # If server returns success-like payload OR we at least got something back, treat as success
                    logger.debug("Ephemeral submit response: %s", resp_j)
                    return {"status": "success", "message": {"via": "ephemeral-ws", "response": resp_j, "payload": payload}}
                else:
                    # resp_j not dict -> return as success with raw
                    return {"status": "success", "message": {"via": "ephemeral-ws", "response": {"raw": resp}, "payload": payload}}

            except Exception as e:
                try:
                    ws_conn.close()
                except Exception:
                    pass
                logger.exception("Ephemeral send/recv failed: %s", e)
                if attempt < MAX_ATTEMPTS:
                    backoff = RETRY_BASE * (2 ** (attempt - 1)) + random.random() * 0.1
                    time.sleep(backoff)
                    continue
                return {"status": "error", "message": f"sendrecv-failed:{e}"}

        # if we exhausted attempts
        return {"status": "error", "message": "max-attempts-exhausted"}


    # monkey-patch AucApi.submit_bid -> ephemeral submit
    AucApi.submit_bid = _ephemeral_ws_submit
    logger.info("Patched AucApi.submit_bid -> ephemeral websocket submit")

    # also patch AuctionPlayer.submit_bid (if present)
    try:
        if hasattr(AuctionPlayer, "submit_bid"):
            AuctionPlayer.submit_bid = lambda auction_id, item_id, bid_amount, user_id=None, min_bid_amount=None: _ephemeral_ws_submit(
                auction_id, item_id, bid_amount, user_id, min_bid_amount
            )
            logger.info("Patched AuctionPlayer.submit_bid -> ephemeral websocket submit")
        else:
            logger.info("AuctionPlayer module has no submit_bid attribute (skipping patch).")
    except Exception:
        logger.exception("Failed to patch AuctionPlayer.submit_bid")

    # persistent ws app factory (fresh each reconnect)
    def _new_ws_app():
        return websocket.WebSocketApp(ws_url,
                                      header=headers,
                                      on_open=_on_open,
                                      on_message=_on_message,
                                      on_error=_on_error,
                                      on_close=_on_close)

    # reconnect loop for persistent receive WS
    def run_loop():
        backoff = 1
        max_backoff = 30
        while True:
            try:
                ws_app = _new_ws_app()
                logger.info("Starting persistent WS (backoff=%s)", backoff)
                ws_app.run_forever(ping_interval=PERSISTENT_PING_INTERVAL,
                                   ping_timeout=PERSISTENT_PING_TIMEOUT,
                                   origin=origin)
                # connection closed - cleanup and reconnect after backoff
                with _global_ws_lock:
                    try:
                        if _global_ws:
                            _global_ws.close()
                    except Exception:
                        pass
                    _global_ws = None
                logger.warning("Persistent WS stopped; reconnecting after %s s", backoff)
                time.sleep(backoff)
                backoff = min(max_backoff, backoff * 2)
            except Exception:
                logger.exception("Persistent WS run loop error; sleeping then retrying")
                time.sleep(backoff)
                backoff = min(max_backoff, backoff * 2)

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()

    # keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping ws adapter")
        with _global_ws_lock:
            if _global_ws:
                try:
                    _global_ws.close()
                except Exception:
                    pass


if __name__ == "__main__":
    start_ws_adapter(USERNAME, PASSWORD, auction_pk=AUCTION_PK)
