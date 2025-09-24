"""
Lightweight fuzzy-style helper to compute bid category from simple numeric inputs.
No external fuzzy lib required. Produces a crisp category and numeric score.

Inputs:
 - price_to_cover (0..1)  : proportion of remaining distance covered (0 low -> 1 high)
 - active_users   (0..1)  : normalized active user count (0 low -> 1 high)
 - auction_phase   (0..1) : phase ratio of auction (0 start -> 1 end)

Output:
 - category: 'low' | 'medium' | 'high'
 - score: numeric 0..1 (higher -> more aggressive)
"""
from typing import Dict, Tuple

def _membership_low(x: float) -> float:
    if x <= 0.2: return 1.0
    if x >= 0.5: return 0.0
    return (0.5 - x) / (0.5 - 0.2)

def _membership_medium(x: float) -> float:
    if 0.2 < x < 0.5:
        return (x - 0.2) / (0.5 - 0.2)
    if 0.5 <= x <= 0.8:
        return (0.8 - x) / (0.8 - 0.5)
    return 0.0

def _membership_high(x: float) -> float:
    if x <= 0.5: return 0.0
    if x >= 0.8: return 1.0
    return (x - 0.5) / (0.8 - 0.5)

def compute_bid_category(inputs: Dict[str, float]) -> Tuple[str, float]:
    """
    inputs: expects normalized keys: price_to_cover, active_users, auction_phase in [0,1]
    returns (category, score) where category in {'low','medium','high'} and score in [0,1]
    """
    p = max(0.0, min(1.0, float(inputs.get('price_to_cover', 0.0))))
    u = max(0.0, min(1.0, float(inputs.get('active_users', 0.0))))
    ph = max(0.0, min(1.0, float(inputs.get('auction_phase', 0.0))))

    # compute membership strengths
    p_low = _membership_low(p); p_med = _membership_medium(p); p_high = _membership_high(p)
    u_low = _membership_low(u); u_med = _membership_medium(u); u_high = _membership_high(u)
    ph_start = 1.0 - ph  # start high when ph small
    ph_mid = (1.0 - abs(ph - 0.5)*2.0) if 0.0 <= ph <= 1.0 else 0.0
    ph_end = ph

    # Rule examples (simplified):
    # If price high AND users high AND phase end => bid high
    # If price medium AND users medium => bid medium
    # If price low AND phase start => bid low
    # We'll compute activation for each category via weighted OR of rules

    # Activation for 'high' bids
    high_activation = max(
        min(p_high, u_high, ph_end),
        min(p_high, u_med, ph_mid),
        min(p_med, u_high, ph_end)
    )

    # Activation for 'medium' bids
    medium_activation = max(
        min(p_med, u_med, ph_mid),
        min(p_high, u_low, ph_mid),
        min(p_low, u_high, ph_mid)
    )

    # Activation for 'low' bids
    low_activation = max(
        min(p_low, u_low, ph_start),
        min(p_low, u_med, ph_mid),
        min(p_med, u_low, ph_start)
    )

    # Normalize activations
    total = high_activation + medium_activation + low_activation
    if total <= 0:
        # default safe fallback
        return 'low', 0.0

    # Weighted score in [0,1] (low=0..0.33 mid=0.33..0.66 high=0.66..1)
    high_w = high_activation / total
    mid_w = medium_activation / total
    low_w = low_activation / total

    score = low_w * 0.15 + mid_w * 0.5 + high_w * 0.85  # crisp numeric
    # choose category by highest activation (deterministic)
    cat_map = [('high', high_activation), ('medium', medium_activation), ('low', low_activation)]
    cat_map.sort(key=lambda t: (-t[1], t[0]))  # deterministic tie-breaker
    category = cat_map[0][0]
    return category, float(round(score, 4))
