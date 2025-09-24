# AuctionPlayer.py
import zmq
import time
import logging
import random
from app.services.decision_service import DecisionService
from AucApi import submit_bid, get_current_bidding_details
from state_store import get_bot_state, persist_bot_state, is_bot_assigned, assign_bot, assigned_bot_for
from config import bot_user_ids

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuctionPlayer")

DECISION = DecisionService()

def process_snapshot(snapshot):
    """
    Called whenever we receive the latest published snapshot (snapshot is dict of auctions)
    Strategy:
      - For each auction in snapshot, if a bot is assigned to that auction we will decide and place a bid
      - If no bot assigned and you want to auto-assign 1 bot per auction, you can call assign_bot.
    """
    for auc_id, auc in snapshot.items():
        # choose a single bot to operate this auction (UAT: assign randomly if unassigned)
        if not is_bot_assigned(auc_id):
            bot = random.choice(bot_user_ids)
            assign_bot(auc_id, str(bot))
            logger.info("Assigned bot %s to auction %s", bot, auc_id)

        bot_id = assigned_bot_for(auc_id)
        if not bot_id:
            continue

        # build a DecideRequest-like object expected by DecisionService.decide()
        class R: pass
        r = R()
        r.auction_id = auc_id
        r.item_id = list(auc.get('AUCTION_ITEMS', {}).keys())[0] if auc.get('AUCTION_ITEMS') else 1
        r.bot_user_id = bot_id
        # Build antecedent_features & auction_state minimally for DecisionService
        # Example features - adapt to whatever your DecisionService expects
        r.antecedent_features = {
            "recent_bid_rate": random.uniform(0.1, 1.0),
            "phase_ratio": random.uniform(0.0, 1.0),
            "time_left_pct": 0.5
        }
        r.auction_state = {
            "best_price": auc.get("AUCTION_ITEMS", {}).get(r.item_id, {}).get("BEST_PRICE", 0),
            "threshold_price": auc.get("AUCTION_ITEMS", {}).get(r.item_id, {}).get("THRESHOLD_PRICE1", 0)
        }
        r.min_inc_price = auc.get("MIN_BID_AMOUNT", 1)
        r.max_inc_price = auc.get("MAX_BID_AMOUNT", r.min_inc_price * 1000)
        r.threshold_price = r.auction_state.get('threshold_price', 0)
        r.step_index = 0

        # fetch persisted state for this auction/bot (or get a fresh one)
        state = get_bot_state(auc_id, bot_id) or {
            "persona_current": None,
            "num_bids_placed": 0,
            "budget_remaining": 100000
        }

        decision = DECISION.decide(r, state)
        if decision.get("status") != "ok":
            logger.error("Decision failed for %s: %s", auc_id, decision)
            continue

        bid_amount = decision["bid_amount"]
        delay = decision["delay_seconds"] or 1

        logger.info("Auction %s: bot %s will bid %s after %s sec (persona=%s)",
                    auc_id, bot_id, bid_amount, delay, decision.get("persona"))

        # schedule (simple) - sleep then submit (in real system use async scheduler / job queue)
        time.sleep(delay)
        submit_resp = submit_bid(auction_id=auc_id, item_id=r.item_id, bid_amount=bid_amount, user_id=bot_id)
        logger.info("Submit bid response: %s", submit_resp)

        # persist updated state returned by decision service
        updated_state = decision.get("updated_state", state)
        persist_bot_state(auc_id, bot_id, updated_state)

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
        except Exception as e:
            logger.exception("Player loop error")
            time.sleep(1)

if __name__ == "__main__":
    main()
