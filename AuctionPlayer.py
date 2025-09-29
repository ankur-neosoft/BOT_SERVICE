# AuctionPlayer.py
import zmq
import time
import logging
import random
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
from config import bot_user_ids, AUCTION_EXTENSION_SECONDS, AUCTION_EXTENSION_THRESHOLD_SEC, BOT_MAX_PERCENT_OF_THRESHOLD

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuctionPlayer")

DECISION = DecisionService()

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

def process_snapshot(snapshot):
    """
    Called whenever we receive the latest published snapshot (snapshot is dict of auctions)
    Strategy:
      - For each auction in snapshot, if a bot is assigned to that auction we will decide and place a bid
      - If no bot assigned and you want to auto-assign 1 bot per auction, you can call assign_bot.
    """
    for auc_id, auc in snapshot.items():
        # assign a bot if none assigned (UAT)
        if not is_bot_assigned(auc_id):
            bot = random.choice(bot_user_ids)
            assign_bot(auc_id, str(bot))
            logger.info("Assigned bot %s to auction %s", bot, auc_id)

        bot_id = assigned_bot_for(auc_id)
        if not bot_id:
            continue

        # skip if auction-level bots_disabled
        if is_bots_disabled(auc_id):
            logger.info("Auction %s: bots_disabled -> skipping", auc_id)
            continue

        # prepare a DecideRequest-like object expected by DecisionService.decide()
        class R: pass
        r = R()
        # choose first item id robustly
        item_keys = list(auc.get('AUCTION_ITEMS', {}).keys()) if auc.get('AUCTION_ITEMS') else []
        r.item_id = item_keys[0] if item_keys else 1
        r.auction_id = auc_id
        r.bot_user_id = bot_id

        # Build antecedent_features & auction_state minimally for DecisionService
        r.antecedent_features = {
            "recent_bid_rate": random.uniform(0.1, 1.0),
            "phase_ratio": random.uniform(0.0, 1.0),
            "time_left_pct": 0.5
        }

        item_block = _fetch_item_block(auc, r.item_id)
        r.auction_state = {
            "best_price": item_block.get("BEST_PRICE", auc.get("BEST_PRICE", 0)),
            "threshold_price": item_block.get("THRESHOLD_PRICE1", auc.get("THRESHOLD_PRICE1", 0))
        }
        r.min_inc_price = auc.get("MIN_BID_AMOUNT", item_block.get("MIN_BID_AMOUNT", 1))
        r.max_inc_price = auc.get("MAX_BID_AMOUNT", item_block.get("MAX_BID_AMOUNT", r.min_inc_price * 1000))
        r.threshold_price = r.auction_state.get('threshold_price', 0)
        r.step_index = 0

        # fetch persisted state for this auction/bot (or default)
        state = get_bot_state(auc_id, bot_id) or {
            "persona_current": None,
            "num_bids_placed": 0,
            "budget_remaining": 100000
        }

        # Decision
        decision = DECISION.decide(r, state)
        if decision.get("status") != "ok":
            logger.error("Decision failed for %s: %s", auc_id, decision)
            continue

        bid_amount = float(decision["bid_amount"])
        delay = int(decision.get("delay_seconds") or 1)

        # Determine best_price defensively
        try:
            best_price = float(item_block.get("BEST_PRICE", auc.get("BEST_PRICE", 0)))
        except Exception:
            best_price = 0.0

        # Obtain threshold and max_allowed from snapshot (authoritative)
        try:
            threshold_price = None
            if "THRESHOLD_PRICE1" in auc:
                threshold_price = float(auc.get("THRESHOLD_PRICE1") or 0)
            elif "THRESHOLD_PRICE1" in item_block:
                threshold_price = float(item_block.get("THRESHOLD_PRICE1") or 0)
        except Exception:
            threshold_price = None

        # Extract max_allowed_price from snapshot using AucApi helper
        max_allowed_price = extract_max_allowed_price_from_snapshot(snapshot, auc_id)
        # If API didn't provide an explicit max, fallback to threshold * percent
        if max_allowed_price is None and threshold_price:
            try:
                max_allowed_price = float(threshold_price) * float(BOT_MAX_PERCENT_OF_THRESHOLD)
            except Exception:
                max_allowed_price = None

        # Pre-submit safety gate: if proposed resulting price would exceed max_allowed_price -> disable bots
        resulting_price = best_price + bid_amount
        if max_allowed_price is not None and resulting_price >= max_allowed_price:
            set_bots_disabled(auc_id, True)
            logger.info("Auction %s: proposed bot bid (%.2f -> resulting %.2f) exceeds max_allowed_price %.2f. Disabling bots.",
                        auc_id, bid_amount, resulting_price, max_allowed_price)
            continue

        logger.info("Auction %s: bot %s will bid %s after %s sec (persona=%s)",
                    auc_id, bot_id, bid_amount, delay, decision.get("persona"))

        # schedule (simple) - sleep then submit (in real system use async scheduler / job queue)
        # persist scheduled marker
        st = state.copy()
        st["next_scheduled"] = (datetime.utcnow() + timedelta(seconds=delay + 5)).isoformat() + "Z"
        persist_bot_state(auc_id, bot_id, st)

        time.sleep(delay)

        # Re-check bots_disabled after wait
        if is_bots_disabled(auc_id):
            logger.info("Auction %s: bots disabled after wait - abort submit", auc_id)
            st = get_bot_state(auc_id, bot_id) or {}
            st["next_scheduled"] = None
            persist_bot_state(auc_id, bot_id, st)
            continue

        # Submit
        submit_resp = submit_bid(auction_id=auc_id, item_id=r.item_id, bid_amount=bid_amount, user_id=bot_id)
        logger.info("Submit bid response: %s", submit_resp)

        # persist updated state returned by decision service (if provided) or update internal counters
        updated_state = decision.get("updated_state", state)
        updated_state["num_bids_placed"] = updated_state.get("num_bids_placed", 0) + 1
        updated_state["last_action_time"] = datetime.utcnow().isoformat() + "Z"
        updated_state["next_scheduled"] = None
        persist_bot_state(auc_id, bot_id, updated_state)

        # Post-submit: if success, possibly extend expiry and maybe disable bots if cap reached
        if submit_resp.get("status") == "success":
            # compute expiry (prefer authoritative override then snapshot)
            expiry_iso = get_expiry_override(auc_id) or auc.get("AUCTION_EXPIRY_DATE_TIME") or auc.get("EXPIRY")
            expiry_dt = None
            try:
                expiry_dt = parse(expiry_iso) if expiry_iso else None
            except Exception:
                expiry_dt = None

            if expiry_dt:
                secs_left = (expiry_dt - datetime.utcnow()).total_seconds()
                if secs_left <= AUCTION_EXTENSION_THRESHOLD_SEC:
                    new_expiry = expiry_dt + timedelta(seconds=AUCTION_EXTENSION_SECONDS)
                    set_expiry_override(auc_id, new_expiry.isoformat() + "Z")
                    logger.info("Auction %s: expiry extended by %s seconds -> %s (local override)",
                                auc_id, AUCTION_EXTENSION_SECONDS, new_expiry.isoformat() + "Z")

            # recompute resulting price (best_price may be slightly stale; we use our last computed)
            latest_resulting = resulting_price
            if max_allowed_price is not None and latest_resulting >= max_allowed_price:
                set_bots_disabled(auc_id, True)
                logger.info("Auction %s: bots disabled after reaching configured cap (%.2f)", auc_id, latest_resulting)

        else:
            logger.warning("Auction %s: bid submit failed: %s", auc_id, submit_resp)
            # clear scheduling marker if submit failed
            st = get_bot_state(auc_id, bot_id) or {}
            st["next_scheduled"] = None
            persist_bot_state(auc_id, bot_id, st)


def main():
    ctx = zmq.Context()
    sock = ctx.socket(zmq.SUB)
    sock.setsockopt(zmq.SUBSCRIBE, b"")  # subscribe to everything
    sock.setsockopt(zmq.CONFLATE, 1)     # keep only latest message
    sock.connect("tcp://localhost:9505")
    logger.info("Player connected to tcp://localhost:9505")

    while True:
        try:
            snapshot = sock.recv_json()  # blocking
            process_snapshot(snapshot)
        except Exception:
            logger.exception("Player loop error")
            time.sleep(1)

if __name__ == "__main__":
    main()
