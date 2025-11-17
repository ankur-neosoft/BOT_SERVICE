"""
ws_adapter.py (final)

- Logs in via session cookie
- Opens WebSocket with cookie header (single persistent connection used for receiving snapshots)
- Joins auction(s)
- Converts incoming WS messages to AuctionPlayer snapshot shape
- Calls process_external_snapshot(snapshot)
- Monkey-patches AucApi.submit_bid AND AuctionPlayer.submit_bid to send bids via ephemeral websocket
  (open -> send -> await response -> close), with an optional pre-check against current_bid.
"""

import json
import time
import threading
import uuid
import logging
import requests
import websocket  # pip install websocket-client

# Import the AuctionPlayer snapshot entry point and module
from AuctionPlayer import process_external_snapshot
import AuctionPlayer
import AucApi  # we will monkey-patch submit_bid here

# ---------- CONFIG - adjust as needed ----------
HTTP_BASE = "http://localhost:8000"                # web_click_for_steel base (used for login only)
LOGIN_PATH = "/CFS/master/login"                   # login endpoint
WS_BASE = "ws://localhost:8000"                    # ws base (ws:// or wss://)
WS_JOIN_PATH = "/CFS/ws/auction/join/"             # from routing.py

USERNAME = "bansari"                               # change to your bot username
PASSWORD = "12345"                                 # change to your bot password
AUCTION_PK = "423d9642-fad7-43a2-ba3a-503199225809"  # auction primary key to join
REQUEST_ID = str(uuid.uuid4())
# -------------------------------------------------

logger = logging.getLogger("ws_adapter")
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())

# global websocket object (live ws connection). Set in _on_open.
_global_ws = None
_global_ws_lock = threading.Lock()


def login_and_get_session(username, password):
    s = requests.Session()
    login_url = HTTP_BASE + LOGIN_PATH

    # Try POST JSON (your login endpoint is POST-only)
    payload = {"username": username, "password": password}
    r = s.post(login_url, json=payload, allow_redirects=True, timeout=10)

    if r.status_code not in (200, 302):
        raise RuntimeError(f"Login failed: {r.status_code} {r.text[:400]}")

    sessionid = s.cookies.get("cfs-sessionid") or s.cookies.get("sessionid")
    if not sessionid:
        raise RuntimeError("No session cookie received after login")

    # debug: print cookies returned so we know exact names
    logger.info("Login cookies: %s", s.cookies.get_dict())
    print("Cookies:", s.cookies.get_dict())

    logger.info("Logged in successfully, session cookie found.")
    return s


def _send_via_ws(payload: dict) -> bool:
    """
    Thread-safe send helper (uses persistent main WS). Returns True on success, False otherwise.
    This remains useful for non-ephemeral sends if you want to use the main connection.
    """
    global _global_ws
    with _global_ws_lock:
        if _global_ws is None:
            logger.error("WebSocket is not connected - cannot send payload")
            return False
        try:
            # _global_ws is the live websocket connection (set in _on_open)
            _global_ws.send(json.dumps(payload))
            logger.info("WS SENT: %s", payload)
            return True
        except Exception as e:
            logger.exception("Failed to send via WS: %s", e)
            # mark socket dead so reconnect loop (if any) can recreate it
            try:
                _global_ws.close()
            except Exception:
                pass
            _global_ws = None
            return False


# Keep the original small ws submit as fallback (unused when we patch to ephemeral)
def _ws_submit_bid(auction_id: str, item_id: int, bid_amount: float, user_id: str = None, min_bid_amount: float = None):
    payload = {
        "action": "place_bid",
        "product_id": item_id,
        "bid": float(bid_amount)
    }
    ok = _send_via_ws(payload)
    if ok:
        # mimic AucApi response shape
        return {"status": "success", "message": {"via": "ws", "payload": payload}}
    else:
        return {"status": "error", "message": "ws-send-failed"}


def _convert_join_response_to_snapshot(join_data: dict):
    """
    Convert server 'auction_data' payload -> AuctionPlayer snapshot format.
    """
    try:
        auction_id = join_data.get("auction_id")
        adata = join_data.get("auction_data", [])
        if not adata:
            return {}
        first = adata[0]
        products = first.get("products", [])
        item_map = {}
        for p in products:
            pid = p.get("auction_product_id")
            # adapt this filter as needed; earlier you filtered pid == 6 in tests
            # we'll include all products by default
            if pid is not None:
                item_map[pid] = {
                    "ITEM_ID": pid,
                    "BEST_PRICE": float(p.get("current_bid") or p.get("starting_bid") or 0),
                    "THRESHOLD_PRICE": float(p.get("reserve_price") or 0),
                    "MIN_BID_AMOUNT": float(p.get("minimum_increment_price") or 1),
                    "MAX_BID_AMOUNT": float(p.get("max_allowed_price") or 999999999)
                }

        # If every item uses the same MIN_BID_AMOUNT we can set an auction-level MIN_BID_AMOUNT.
        auction_block = {"AUCTION_ITEMS": item_map}
        # compute common item-level min if items exist
        if item_map:
            item_mins = {v.get("MIN_BID_AMOUNT") for v in item_map.values() if v.get("MIN_BID_AMOUNT") is not None}
            if len(item_mins) == 1:
                auction_block["MIN_BID_AMOUNT"] = next(iter(item_mins))
        logger.debug("Converted join -> snapshot: %s", item_map)
        return {auction_id: auction_block}
    except Exception:
        logger.exception("Error converting join response to snapshot")
        return {}


def _convert_update_to_snapshot(update_msg: dict):
    """
    Convert an 'update_bid' message into a snapshot.
    """
    try:
        product = update_msg.get("product") or {}
        pid = product.get("auction_product_id") or product.get("id")
        # Get auction id from update message; fall back to global AUCTION_PK to avoid None.
        auction_id = update_msg.get("auction_id") or update_msg.get("auctionId") or AUCTION_PK
        item_block = {
            "ITEM_ID": pid,
            "BEST_PRICE": float(product.get("current_bid") or product.get("starting_bid") or 0),
            "THRESHOLD_PRICE": float(product.get("reserve_price") or 0),
            "MIN_BID_AMOUNT": float(product.get("minimum_increment_price") or 1),
            "MAX_BID_AMOUNT": float(product.get("max_allowed_price") or 999999999)
        }
        snapshot = {
            auction_id: {
                "AUCTION_ITEMS": {
                    pid: item_block
                }
            }
        }
        # Set auction-level MIN_BID_AMOUNT equal to the item min (do NOT force 1)
        snapshot[auction_id]["MIN_BID_AMOUNT"] = item_block["MIN_BID_AMOUNT"]
        return snapshot
    except Exception:
        logger.exception("Error converting update to snapshot")
        return {}


# WebSocket callbacks
def _on_open(ws):
    """
    IMPORTANT: set the global live websocket object here so _send_via_ws() can use it.
    The 'ws' argument is the live connection (not WebSocketApp).
    """
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

    # Join response with auction_data
    if data.get("status") == "success" and "auction_data" in data:
        logger.info("Join confirmed for auction: %s", data.get("auction_id"))
        snap = _convert_join_response_to_snapshot(data)
        if snap:
            process_external_snapshot(snap)
        return

    # update_bid messages
    action = data.get("action") or data.get("type")
    if action == "update_bid" or data.get("event") == "update_bid":
        snap = _convert_update_to_snapshot(data)
        if snap:
            process_external_snapshot(snap)
        return

    # bid_response or other messages: log
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
    Start the adapter:
      - login (requests.Session) and build cookie header
      - start persistent WS for receiving snapshots
      - monkey-patch AucApi.submit_bid and AuctionPlayer.submit_bid to use ephemeral WS per bid
    """
    global _global_ws, AUCTION_PK
    if auction_pk:
        AUCTION_PK = auction_pk

    session = login_and_get_session(username, password)

    # Build cookie header from all cookies returned by the login session
    cookies_dict = session.cookies.get_dict()
    cookie_parts = [f"{k}={v}" for k, v in cookies_dict.items()]
    cookie_header = "; ".join(cookie_parts)

    # use Origin via run_forever below, don't include Origin header string to avoid mangling
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
    ws_app = websocket.WebSocketApp(ws_url,
                                    header=headers,
                                    on_open=_on_open,
                                    on_message=_on_message,
                                    on_error=_on_error,
                                    on_close=_on_close)

    # -------------------------
    # Ephemeral WS submit implementation (closure uses cookie_header, origin, ws_url)
    # -------------------------
    def _ephemeral_ws_submit(auction_id: str, item_id: int, bid_amount: float, user_id: str = None, min_bid_amount: float = None, timeout: int = 5):
        """
        Open a short-lived websocket, send single place_bid payload, wait for a single response, close.
        Performs an optional pre-check using AucApi.get_current_bidding_details to avoid wasted connections.
        Returns a dict in the shape the rest of the code expects.
        """
        payload = {"action": "place_bid", "product_id": item_id, "bid": float(bid_amount)}
        # Optional pre-check: ask live API if current bid already >= intended bid — skip if so.
        try:
            if hasattr(AucApi, "get_current_bidding_details"):
                try:
                    details = AucApi.get_current_bidding_details(auction_id, item_id)
                    # details might be dict with 'current_bid' or 'best_price'
                    if isinstance(details, dict):
                        for key in ("current_bid", "best_price", "current_highest", "current_price"):
                            if key in details:
                                current_live = float(details[key] or 0)
                                if current_live >= float(bid_amount):
                                    logger.info("Ephemeral submit: current_live(=%s) >= bid_amount(=%s) -> skipping open ws", current_live, bid_amount)
                                    return {"status": "error", "message": f"skipped-stale-bid:live_bid={current_live}"}
                                break
                    elif isinstance(details, (int, float)):
                        current_live = float(details)
                        if current_live >= float(bid_amount):
                            logger.info("Ephemeral submit: current_live(=%s) >= bid_amount(=%s) -> skipping open ws", current_live, bid_amount)
                            return {"status": "error", "message": f"skipped-stale-bid:live_bid={current_live}"}
                except Exception:
                    # pre-check failure: proceed with ephemeral submit (fail-open)
                    logger.debug("Ephemeral submit pre-check failed; proceeding to open ephemeral ws", exc_info=True)
        except Exception:
            # shouldn't happen, but be defensive
            logger.debug("AucApi.get_current_bidding_details not available or raised; proceeding")

        # Build headers for ephemeral connection (reuse cookie header)
        ephemeral_headers = [
            f"Cookie: {cookie_header}",
            f"Host: {host}",
            "Connection: Upgrade",
            "Upgrade: websocket",
            "User-Agent: ws-ephemeral/1.0"
        ]
        # Use same ws_url as join path (server should accept place_bid frames on same endpoint)
        try:
            # create_connection will do the handshake; short timeout avoids long waits
            ws_conn = websocket.create_connection(ws_url, timeout=timeout, header=ephemeral_headers, origin=origin)
        except Exception as e:
            logger.exception("Ephemeral WS connect failed: %s", e)
            return {"status": "error", "message": f"connect-failed:{e}"}

        try:
            try:
                ws_conn.settimeout(timeout)
            except Exception:
                pass
            # send the bid payload
            ws_conn.send(json.dumps(payload))
            logger.info("EPHEMERAL WS SENT: %s", payload)
            # wait for a response frame (short)
            try:
                resp = ws_conn.recv()
            except Exception as e:
                logger.warning("Ephemeral WS: no response/recv failed: %s", e)
                ws_conn.close()
                return {"status": "error", "message": f"no-response:{e}"}

            # try parse response
            try:
                resp_j = json.loads(resp)
            except Exception:
                resp_j = {"raw": resp}
            ws_conn.close()
            return {"status": "success", "message": {"via": "ephemeral-ws", "response": resp_j, "payload": payload}}
        except Exception as e:
            try:
                ws_conn.close()
            except Exception:
                pass
            logger.exception("Ephemeral WS send/recv failed: %s", e)
            return {"status": "error", "message": f"sendrecv-failed:{e}"}

    # -------------------------
    # Monkey-patch submit_bid to ephemeral implementation
    # -------------------------
    AucApi.submit_bid = _ephemeral_ws_submit
    logger.info("Patched AucApi.submit_bid -> ephemeral websocket submit")

    # ALSO patch AuctionPlayer's local submit binding (handles "from AucApi import submit_bid")
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

    # run persistent receive WS in separate thread (blocking run_forever). Provide origin to run_forever to be used in handshake.
    def run():
        # Keep reasonably short ping settings; ephemeral submits avoid keeping many sockets active.
        ws_app.run_forever(ping_interval=30, ping_timeout=10, origin=origin)

    wst = threading.Thread(target=run)
    wst.daemon = True
    wst.start()

    # keep main thread alive, adapter will feed AuctionPlayer via process_external_snapshot
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping ws adapter")
        # attempt to close live ws if present
        with _global_ws_lock:
            if _global_ws:
                try:
                    _global_ws.close()
                except Exception:
                    pass


if __name__ == "__main__":
    start_ws_adapter(USERNAME, PASSWORD, auction_pk=AUCTION_PK)
