from typing import Dict

def compute_base_amount(auction_state: Dict) -> float:
    """
    Base amount heuristic: a small fraction of remaining distance, bounded by min_inc and a cap.
    auction_state expected keys: best_price, threshold_price, min_inc_price
    """
    best_price = float(auction_state.get('best_price', 0.0))
    threshold = float(auction_state.get('threshold_price', best_price + 1000))
    min_inc = float(auction_state.get('min_inc_price', 1.0))
    dist = max(0.0, threshold - best_price)

    if dist <= 5 * min_inc:
        base = min_inc
    else:
        # 5% of remaining distance but limited to 10*min_inc max
        base = min(max(min_inc, dist * 0.05), 10 * min_inc)
    return round(base, 2)

def estimate_base_delay(auction_state: Dict) -> int:
    """
    Base delay heuristic: inverse of activity and influenced by time left.
    recent_bid_rate roughly >0 means more active auctions -> shorter delays.
    """
    recent_rate = float(auction_state.get('recent_bid_rate', 1.0))
    time_left_pct = float(auction_state.get('time_left_pct', 0.5))
    base = max(1, int((1.0 / max(0.1, recent_rate)) * (1.0 + (1.0 - time_left_pct) * 2.0) * 10))
    return base
