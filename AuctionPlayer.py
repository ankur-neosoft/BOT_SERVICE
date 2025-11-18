# AuctionPlayer.py
import zmq
import time
import logging
import random
import threading
from datetime import datetime, timedelta
from dateutil.parser import parse

from app.services.decision_service import DecisionService
from AucApi import submit_bid, get_current_bidding_details, extract_max_allowed_price_from_snapshot, get_auction_time_from_snapshot
from state_store import (
    get_bot_state, persist_bot_state,
    is_bot_assigned, assign_bot, assigned_bot_for,
    set_bots_disabled, is_bots_disabled,
    set_expiry_override, get_expiry_override
)
import AucApi
from config import bot_user_ids, AUCTION_EXTENSION_SECONDS, AUCTION_EXTENSION_THRESHOLD_SEC, BOT_MAX_PERCENT_OF_THRESHOLD

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuctionPlayer")

DECISION = DecisionService()

MAX_WAIT = 25  # maximum wait (seconds) allowed for scheduled bidding

# Running workers registry to avoid duplicate workers for same (auction:item)
_WORKER_LOCK = threading.Lock()
_RUNNING_WORKERS = set()  # set of item_assign_key strings (f"{auction_id}:{item_id}")


def process_external_snapshot(snapshot):
    """
    Adapter entrypoint: accept a snapshot (same shape AuctionPlayer expects)
    and run the existing process_snapshot() logic.
    WS adapter will call this directly.
    """
    try:
        process_snapshot(snapshot)
    except Exception:
        logger.exception("Error in process_external_snapshot")


def _fetch_item_block(auc, item_id):
    """
    Robustly find the item block inside snapshot for given item_id.
    Accepts numeric or string keys.
    """
    items = auc.get('AUCTION_ITEMS') or {}
    # try direct key
    try:
        if item_id in items:
            return items[item_id]
        # try stringified
        if str(item_id) in items:
            return items[str(item_id)]
        # try integer conversion (keys sometimes strings)
        for k, v in items.items():
            try:
                if int(k) == int(item_id):
                    return v
            except Exception:
                pass
    except Exception:
        pass
    # fallback: return first item block if present
    if isinstance(items, dict) and items:
        return next(iter(items.values()))
    return {}


def _safe_get_current_best(auc_id, item_id, fallback):
    """
    Try to get the current best price from the live API. If that fails, return fallback.
    The AucApi.get_current_bidding_details should return a structure with 'best_price' or equivalent.
    """
    try:
        if hasattr(AucApi, "get_current_bidding_details"):
            details = get_current_bidding_details(auc_id, item_id)
            if isinstance(details, dict):
                # common keys: 'best_price', 'current_bid', 'current_highest', 'current_price'
                for key in ("best_price", "current_bid", "current_highest", "current_price"):
                    if key in details:
                        return float(details[key] or 0)
            if isinstance(details, (int, float)):
                return float(details)
    except Exception:
        logger.debug("Could not fetch live current bid for %s:%s (falling back).", auc_id, item_id, exc_info=True)
    return float(fallback or 0.0)


def _choose_dynamic_bid(best_price, threshold_price, min_allowed):
    """
    Simple dynamic bid picker — larger jumps when far from threshold,
    finer when near. Adds randomness so behavior isn't predictable.
    """
    try:
        best = float(best_price or 0.0)
        thresh = float(threshold_price or 0.0)
        min_inc = float(min_allowed or 1.0)
    except Exception:
        best, thresh, min_inc = float(best_price or 0), float(threshold_price or 0), float(min_allowed or 1)

    if thresh <= best + min_inc:
        if random.random() < 0.2:
            step = min_inc * random.randint(2, 6)
        else:
            step = min_inc * random.randint(1, 4)
        final = round(best + step, 2)
        return max(final, round(best + min_inc, 2))

    remaining = max(0.0, thresh - best)
    frac = (random.random() ** 1.2)
    frac = 0.05 + frac * 0.45
    if random.random() < 0.12:
        frac = 0.2 + random.random() * 0.6

    desired_jump = remaining * frac
    increments_needed = max(1, int(round(desired_jump / min_inc)))
    if random.random() < 0.25:
        increments_needed += random.randint(0, max(1, int(0.2 * increments_needed)))

    candidate = best + increments_needed * min_inc
    candidate = round(max(candidate, best + min_inc), 2)

    if random.random() < 0.05:
        abs_frac = 0.6 + random.random() * 0.35
        abs_price = round(best + remaining * abs_frac, 2)
        candidate = min(abs_price, thresh)

    if candidate > thresh:
        candidate = thresh

    return candidate


def _choose_human_pause():
    raw = random.expovariate(1/3)  # mean ~3s
    pause = max(0.8, min(6.0, raw + random.uniform(-0.3, 0.3)))
    return round(pause, 2)


def _acquire_worker(item_assign_key):
    with _WORKER_LOCK:
        if item_assign_key in _RUNNING_WORKERS:
            return False
        _RUNNING_WORKERS.add(item_assign_key)
        return True


def _release_worker(item_assign_key):
    with _WORKER_LOCK:
        _RUNNING_WORKERS.discard(item_assign_key)


def process_item(auc_id, auc, item_key):
    """
    Long-running worker that handles bidding flow for a single (auction, item).
    It assumes the reservation was already acquired by process_snapshot().
    """
    item_assign_key = f"{auc_id}:{item_key}"

    logger.info("Worker started for %s", item_assign_key)

    try:
        # Per-item assignment: assign bot for this specific (auction:item) if not assigned yet
        if not is_bot_assigned(item_assign_key):
            bot = random.choice(bot_user_ids)
            assign_bot(item_assign_key, str(bot))
            logger.info("Assigned bot %s to item %s", bot, item_assign_key)

        bot_id = assigned_bot_for(item_assign_key)
        if not bot_id:
            logger.warning("No bot assigned for %s - aborting worker", item_assign_key)
            return

        # Main long-running loop
        while True:
            # stop if auction-level bots disabled
            if is_bots_disabled(auc_id):
                logger.info("Auction %s: bots_disabled -> worker for %s exiting", auc_id, item_assign_key)
                return

            item_block = _fetch_item_block(auc, item_key)
            snapshot_best = float(item_block.get("BEST_PRICE", auc.get("BEST_PRICE", 0)))
            snapshot_threshold = float(item_block.get("THRESHOLD_PRICE1", item_block.get("THRESHOLD_PRICE", auc.get("THRESHOLD_PRICE1", auc.get("THRESHOLD_PRICE", 0) or 0))))
            min_allowed = float(item_block.get("MIN_BID_AMOUNT", auc.get("MIN_BID_AMOUNT", 1)))

            best_now = _safe_get_current_best(auc_id, item_key, snapshot_best)

            if snapshot_threshold and best_now >= snapshot_threshold:
                logger.info("Auction %s item %s: best_now %.2f >= threshold %.2f -> worker stopping", auc_id, item_key, best_now, snapshot_threshold)
                return

            class R: pass
            r = R()
            r.item_id = item_key
            r.auction_id = auc_id
            r.bot_user_id = bot_id
            r.antecedent_features = {
                "recent_bid_rate": random.uniform(0.1, 1.0),
                "phase_ratio": random.uniform(0.0, 1.0),
                "time_left_pct": 0.5
            }
            r.auction_state = {"best_price": best_now, "threshold_price": snapshot_threshold}
            r.min_inc_price = min_allowed
            r.max_inc_price = float(auc.get("MAX_BID_AMOUNT", item_block.get("MAX_BID_AMOUNT", r.min_inc_price * 1000)))
            r.threshold_price = r.auction_state.get('threshold_price', 0)
            r.step_index = 0

            state = get_bot_state(item_assign_key, bot_id) or {
                "persona_current": None,
                "num_bids_placed": 0,
                "budget_remaining": 100000
            }

            decision = DECISION.decide(r, state)
            if decision.get("status") != "ok":
                logger.error("Decision failed for %s item %s: %s", auc_id, item_key, decision)
                time.sleep(_choose_human_pause())
                continue

            decision_bid = decision.get("bid_amount")
            decision_delay = int(decision.get("delay_seconds") or 1)
            delay = min(decision_delay, MAX_WAIT)

            if decision_bid is not None:
                try:
                    dbf = float(decision_bid)
                    if dbf <= best_now:
                        proposed = round(best_now + dbf, 2)
                    else:
                        proposed = round(dbf, 2)
                except Exception:
                    proposed = _choose_dynamic_bid(best_now, snapshot_threshold, min_allowed)
            else:
                proposed = _choose_dynamic_bid(best_now, snapshot_threshold, min_allowed)

            if proposed <= best_now:
                proposed = round(best_now + min_allowed, 2)

            try:
                if min_allowed > 0:
                    multiples = int((proposed - best_now) / min_allowed)
                    if multiples < 1:
                        multiples = 1
                    proposed = round(best_now + multiples * min_allowed, 2)
            except Exception:
                pass

            resulting_price = float(proposed)

            max_allowed_price = extract_max_allowed_price_from_snapshot({auc_id: auc}, auc_id)
            if max_allowed_price is None and snapshot_threshold:
                try:
                    max_allowed_price = float(snapshot_threshold) * float(BOT_MAX_PERCENT_OF_THRESHOLD)
                except Exception:
                    max_allowed_price = None

            if max_allowed_price is not None and resulting_price >= max_allowed_price:
                set_bots_disabled(auc_id, True)
                logger.info("Auction %s item %s: proposed bid %.2f >= cap %.2f -> disabling bots and stopping worker", auc_id, item_key, resulting_price, max_allowed_price)
                return

            logger.info("Auction %s item %s: bot %s proposing bid=%.2f (best_now=%.2f, min_inc=%.2f) delay=%s persona=%s",
                        auc_id, item_key, bot_id, resulting_price, best_now, min_allowed, delay, decision.get("persona"))

            st = state.copy()
            st["next_scheduled"] = (datetime.utcnow() + timedelta(seconds=delay + 5)).isoformat() + "Z"
            st["decision_timestamp"] = datetime.utcnow().isoformat() + "Z"
            st["decision_best_price"] = float(best_now)
            st["decision_final_bid"] = float(resulting_price)
            persist_bot_state(item_assign_key, bot_id, st)

            time.sleep(delay)

            live_before_submit = _safe_get_current_best(auc_id, item_key, best_now)

            if live_before_submit >= resulting_price:
                resulting_price = round(live_before_submit + min_allowed, 2)

            if max_allowed_price is not None and resulting_price >= max_allowed_price:
                set_bots_disabled(auc_id, True)
                logger.info("Auction %s item %s: post-adjust resulting_price %.2f >= cap %.2f -> disabling bots & stopping", auc_id, item_key, resulting_price, max_allowed_price)
                return

            submit_resp = submit_bid(auction_id=auc_id, item_id=item_key, bid_amount=resulting_price, user_id=bot_id)
            logger.info("Submit bid response (auction %s item %s): %s", auc_id, item_key, submit_resp)

            updated_state = decision.get("updated_state", state)
            updated_state["num_bids_placed"] = updated_state.get("num_bids_placed", 0) + 1
            updated_state["last_action_time"] = datetime.utcnow().isoformat() + "Z"
            updated_state["next_scheduled"] = None

            if submit_resp.get("status") == "success":
                updated_state["last_bot_bid_amount"] = float(resulting_price)
                updated_state["last_seen_external_best"] = float(_safe_get_current_best(auc_id, item_key, resulting_price))
            else:
                updated_state["last_bot_bid_amount"] = updated_state.get("last_bot_bid_amount")

            persist_bot_state(item_assign_key, bot_id, updated_state)

            latest_best = _safe_get_current_best(auc_id, item_key, resulting_price)
            if snapshot_threshold and latest_best >= snapshot_threshold:
                logger.info("Auction %s item %s: reached threshold (best %.2f >= threshold %.2f) -> stopping worker", auc_id, item_key, latest_best, snapshot_threshold)
                return

            pause = _choose_human_pause()
            time.sleep(pause)

    except Exception:
        logger.exception("Error in worker for %s - exiting", item_assign_key)
    finally:
        _release_worker(item_assign_key)
        logger.info("Worker released for %s", item_assign_key)


def process_snapshot(snapshot):
    """
    For each auction in snapshot, spawn per-item worker threads (one per (auction:item)).
    Workers are long-running and will exit when threshold/cap reached or bots disabled.
    Reservation (_acquire_worker) is done here before spawning the thread to avoid the previous race.
    """
    logger.debug("process_snapshot called with snapshot keys: %s", list(snapshot.keys()))
    for auc_id, auc in snapshot.items():
        if not auc_id:
            logger.warning("Received snapshot with empty/invalid auction id — skipping snapshot: %s", auc)
            continue

        # If auction-level bots_disabled, skip entire auction
        if is_bots_disabled(auc_id):
            logger.info("Auction %s: bots_disabled -> skipping", auc_id)
            continue

        item_keys = list(auc.get('AUCTION_ITEMS', {}).keys()) if auc.get('AUCTION_ITEMS') else []
        if not item_keys:
            logger.debug("Auction %s: no items in snapshot -> skipping", auc_id)
            continue

        for item_key in item_keys:
            item_assign_key = f"{auc_id}:{item_key}"
            # Try to acquire reservation — only spawn thread if we successfully reserved
            reserved = _acquire_worker(item_assign_key)
            if not reserved:
                logger.debug("Worker already reserved/running for %s - skipping spawn", item_assign_key)
                continue

            logger.info("Spawning worker for %s", item_assign_key)
            t = threading.Thread(target=process_item, args=(auc_id, auc, item_key), daemon=True)
            t.start()
