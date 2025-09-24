from typing import Dict
from config import WEIGHT_MATRIX, PERSONAS

def normalize_features(raw: Dict[str, float]) -> Dict[str, float]:
    # clamp features to [0,1] (assume upstream normalization arrives roughly in [0,1])
    out = {}
    for k, v in raw.items():
        try:
            fv = float(v)
        except Exception:
            fv = 0.0
        out[k] = max(0.0, min(1.0, fv))
    return out

def score_persona(features: Dict[str, float], persona: str) -> float:
    weights = WEIGHT_MATRIX.get(persona, {})
    s = 0.0
    for fname, fval in features.items():
        w = weights.get(fname, 0.0)
        s += w * fval
    return s

def select_persona(features_raw: Dict[str, float], bot_id: str, auction_id: str) -> str:
    features = normalize_features(features_raw)
    scores = {p: score_persona(features, p) for p in PERSONAS}
    # deterministic tie-breaker: highest score, then lexicographic persona name
    sorted_items = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    chosen = sorted_items[0][0]
    return chosen
