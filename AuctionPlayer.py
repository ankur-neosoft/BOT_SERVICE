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
import AucApi
from config import bot_user_ids, AUCTION_EXTENSION_SECONDS, AUCTION_EXTENSION_THRESHOLD_SEC, BOT_MAX_PERCENT_OF_THRESHOLD

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuctionPlayer")

DECISION = DecisionService()

MAX_WAIT = 25  # maximum wait (seconds) allowed for scheduled bidding


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
                # common keys: 'best_price', 'current_bid', 'current_highest'
                for key in ("best_price", "current_bid", "current_highest", "current_price"):
                    if key in details:
                        return float(details[key] or 0)
            if isinstance(details, (int, float)):
                return float(details)
    except Exception:
        logger.debug("Could not fetch live current bid for %s:%s (falling back).", auc_id, item_id, exc_info=True)
    return float(fallback or 0.0)


def process_snapshot(snapshot):
    """
    Called whenever we receive the latest published snapshot (snapshot is dict of auctions)
    Strategy:
      - For each auction in snapshot, if a bot is assigned to that auction we will decide and place a bid
      - Respect max wait time (MAX_WAIT)
      - If someone else bids during our wait, skip submitting (prevents stale submits)
      - Allow repeated bidding by the bot until threshold/cap is reached
      - NEW: process ALL items inside AUCTION_ITEMS for that auction (not just the first)
    """
    for auc_id, auc in snapshot.items():
        # Defensive: skip if auction id is falsy (None / empty). Prevent assigning bots to None.
        if not auc_id:
            logger.warning("Received snapshot with empty/invalid auction id — skipping snapshot: %s", auc)
            continue

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

        # Get all item keys; iterate over each and run decision+submit flow per item
        item_keys = list(auc.get('AUCTION_ITEMS', {}).keys()) if auc.get('AUCTION_ITEMS') else []
        if not item_keys:
            logger.debug("Auction %s: no items in snapshot -> skipping", auc_id)
            continue

        for item_key in item_keys:
            try:
                # prepare a DecideRequest-like object expected by DecisionService.decide()
                class R: pass
                r = R()
                r.item_id = item_key
                r.auction_id = auc_id
                r.bot_user_id = bot_id

                # Build antecedent_features & auction_state minimally for DecisionService
                r.antecedent_features = {
                    "recent_bid_rate": random.uniform(0.1, 1.0),
                    "phase_ratio": random.uniform(0.0, 1.0),
                    "time_left_pct": 0.5
                }

                item_block = _fetch_item_block(auc, r.item_id)
                # snapshot-level values might be under item_block or auc; prefer item_block
                snapshot_best = float(item_block.get("BEST_PRICE", auc.get("BEST_PRICE", 0)))
                # support both THRESHOLD_PRICE / THRESHOLD_PRICE1 keys
                snapshot_threshold = float(item_block.get("THRESHOLD_PRICE", item_block.get("THRESHOLD_PRICE", auc.get("THRESHOLD_PRICE", auc.get("THRESHOLD_PRICE", 0) or 0))))

                r.auction_state = {
                    "best_price": snapshot_best,
                    "threshold_price": snapshot_threshold
                }

                # Prefer item-level MIN_BID_AMOUNT (avoid auction-level defaults overriding item)
                r.min_inc_price = float(item_block.get("MIN_BID_AMOUNT", auc.get("MIN_BID_AMOUNT", 1)))
                r.max_inc_price = float(auc.get("MAX_BID_AMOUNT", item_block.get("MAX_BID_AMOUNT", r.min_inc_price * 1000)))
                r.threshold_price = r.auction_state.get('threshold_price', 0)
                r.step_index = 0

                # fetch persisted state for this auction/bot (or default)
                state = get_bot_state(auc_id, bot_id) or {
                    "persona_current": None,
                    "num_bids_placed": 0,
                    "budget_remaining": 100000,
                }

                # Now run DecisionService
                decision = DECISION.decide(r, state)
                if decision.get("status") != "ok":
                    logger.error("Decision failed for %s item %s: %s", auc_id, r.item_id, decision)
                    continue

                # Extract decision results
                bid_amount_raw = decision.get("bid_amount")
                requested_delay = int(decision.get("delay_seconds") or 1)
                # Cap requested delay to MAX_WAIT
                delay = min(requested_delay, MAX_WAIT)

                # Recompute best_price from live API to reduce stale decisions:
                best_price_at_decision = _safe_get_current_best(auc_id, r.item_id, snapshot_best)

                # Normalize decision output into a final bid price
                try:
                    bid_amt_f = float(bid_amount_raw)
                except Exception:
                    logger.warning("Invalid bid_amount from decision for auction %s item %s: %r — skipping", auc_id, r.item_id, bid_amount_raw)
                    continue

                # If decision returned an absolute that is already > best_price_at_decision treat as absolute,
                # otherwise treat as increment.
                if bid_amt_f <= best_price_at_decision:
                    final_bid = round(best_price_at_decision + bid_amt_f, 2)
                else:
                    final_bid = round(bid_amt_f, 2)

                # Ensure final_bid respects min increment
                min_allowed = float(item_block.get("MIN_BID_AMOUNT", auc.get("MIN_BID_AMOUNT", r.min_inc_price or 1)))
                if final_bid <= best_price_at_decision:
                    final_bid = round(best_price_at_decision + min_allowed, 2)

                # Quantize to multiples of min_allowed
                try:
                    if min_allowed > 0:
                        multiples = int((final_bid - best_price_at_decision) / min_allowed)
                        if multiples < 1:
                            multiples = 1
                        final_bid = round(best_price_at_decision + multiples * min_allowed, 2)
                except Exception:
                    pass

                resulting_price = final_bid

                # Compute max_allowed_price (authoritative)
                max_allowed_price = extract_max_allowed_price_from_snapshot(snapshot, auc_id)
                if max_allowed_price is None and snapshot_threshold:
                    try:
                        max_allowed_price = float(snapshot_threshold) * float(BOT_MAX_PERCENT_OF_THRESHOLD)
                    except Exception:
                        max_allowed_price = None

                # Pre-submit safety gate: if final bid would exceed cap -> disable bots
                if max_allowed_price is not None and resulting_price >= max_allowed_price:
                    set_bots_disabled(auc_id, True)
                    logger.info("Auction %s: proposed bot bid (%.2f) would exceed max_allowed_price %.2f. Disabling bots.",
                                auc_id, resulting_price, max_allowed_price)
                    continue

                logger.info("Auction %s: bot %s decided final_bid=%.2f (item=%s best_now=%.2f, min_inc=%.2f) with delay=%s sec, persona=%s",
                            auc_id, bot_id, resulting_price, r.item_id, best_price_at_decision, min_allowed, delay, decision.get("persona"))

                # persist marker about scheduled bid (so other processes can inspect)
                st = state.copy()
                st["next_scheduled"] = (datetime.utcnow() + timedelta(seconds=delay + 5)).isoformat() + "Z"
                st["decision_timestamp"] = datetime.utcnow().isoformat() + "Z"
                st["decision_best_price"] = float(best_price_at_decision)
                st["decision_final_bid"] = float(resulting_price)
                persist_bot_state(auc_id, bot_id, st)

                # Sleep for the delay (cap ensured)
                time.sleep(delay)

                # After waiting, check live best price again. If someone else bid higher AFTER our decision time,
                # the bot should NOT submit its scheduled bid (avoid stale submits).
                live_best_after = _safe_get_current_best(auc_id, r.item_id, snapshot_best)

                # If someone else placed a higher bid than the best_price_at_decision,
                # then our scheduled bid is stale and should be skipped.
                if live_best_after > float(best_price_at_decision):
                    logger.info("Auction %s: live best for item %s changed from %.2f -> %.2f during wait; skipping scheduled bid by bot %s",
                                auc_id, r.item_id, float(best_price_at_decision), float(live_best_after), bot_id)
                    # clear scheduling marker but DO NOT mark last_bot_bid_amount (we didn't place a bid)
                    st2 = get_bot_state(auc_id, bot_id) or {}
                    st2["next_scheduled"] = None
                    persist_bot_state(auc_id, bot_id, st2)
                    continue

                # Submit using the computed absolute final price
                submit_resp = submit_bid(auction_id=auc_id, item_id=r.item_id, bid_amount=resulting_price, user_id=bot_id)
                logger.info("Submit bid response (auction %s item %s): %s", auc_id, r.item_id, submit_resp)

                # persist updated state: mark that bot placed this bid.
                updated_state = decision.get("updated_state", state)
                updated_state["num_bids_placed"] = updated_state.get("num_bids_placed", 0) + 1
                updated_state["last_action_time"] = datetime.utcnow().isoformat() + "Z"
                updated_state["next_scheduled"] = None

                # Record the absolute price bot placed so we can reference it later if needed.
                if submit_resp.get("status") == "success":
                    updated_state["last_bot_bid_amount"] = float(resulting_price)
                    # We can track last_seen_external_best as well.
                    updated_state["last_seen_external_best"] = float(_safe_get_current_best(auc_id, r.item_id, resulting_price))
                else:
                    # If submit failed, preserve previous value
                    updated_state["last_bot_bid_amount"] = updated_state.get("last_bot_bid_amount")

                persist_bot_state(auc_id, bot_id, updated_state)

                # Post-submit: handle expiry extension and cap checks as before
                if submit_resp.get("status") == "success":
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

                    latest_resulting = resulting_price
                    if max_allowed_price is not None and latest_resulting >= max_allowed_price:
                        set_bots_disabled(auc_id, True)
                        logger.info("Auction %s: bots disabled after reaching configured cap (%.2f)", auc_id, latest_resulting)

                else:
                    logger.warning("Auction %s: bid submit failed for item %s: %s", auc_id, r.item_id, submit_resp)
                    # clear scheduling marker if submit failed
                    st = get_bot_state(auc_id, bot_id) or {}
                    st["next_scheduled"] = None
                    persist_bot_state(auc_id, bot_id, st)

            except Exception:
                logger.exception("Error processing auction %s item %s – continuing with next item", auc_id, item_key)
                # continue with next item

# optional main runner (commented)
# def main():
#     ctx = zmq.Context()
#     sock = ctx.socket(zmq.SUB)
#     sock.setsockopt(zmq.SUBSCRIBE, b"")  # subscribe to everything
#     sock.setsockopt(zmq.CONFLATE, 1)     # keep only latest message
#     sock.connect("tcp://localhost:9505")
#     logger.info("Player connected to tcp://localhost:9505")
#
#     while True:
#         try:
#             snapshot = sock.recv_json()  # blocking
#             process_snapshot(snapshot)
#         except Exception:
#             logger.exception("Player loop error")
#             time.sleep(1)
#
# if __name__ == "__main__":
#     main()
