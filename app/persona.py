from typing import Dict
import random, hashlib

# Persona parameters (tunable)
PERSONA_PARAMS = {
    'patient':    {'time_mean':18, 'time_jitter':12, 'amount_jitter_pct':5,  'aggression':0.25},
    'aggressive': {'time_mean':6,  'time_jitter':4,  'amount_jitter_pct':12, 'aggression':0.8},
    'bluffer':    {'time_mean':7,  'time_jitter':5,  'amount_jitter_pct':20, 'aggression':0.5},
    'snipe':      {'time_mean':3,  'time_jitter':2,  'amount_jitter_pct':6,  'aggression':0.6},
}

def _seeded_rng(bot_id: str, auction_id: str, step_index: int = 0):
    s = f"{bot_id}:{auction_id}:{step_index}"
    seed = int(hashlib.sha256(s.encode('utf-8')).hexdigest()[:16], 16) & 0xffffffff
    return random.Random(seed)

def apply_amount(persona: str, base_amount: float, auction_features: Dict, bot_id: str, auction_id: str, step_index: int = 0) -> float:
    """
    Persona applies a deterministic, seeded jitter and a small aggression bias.
    Returns a rounded amount (not yet quantized to min_inc).
    """
    params = PERSONA_PARAMS.get(persona, PERSONA_PARAMS['patient'])
    rng = _seeded_rng(bot_id, auction_id, step_index)
    jitter_pct = params.get('amount_jitter_pct', 5)
    phase_ratio = float(auction_features.get('phase_ratio', 0.0))
    aggression = params.get('aggression', 0.0)
    bias = aggression * phase_ratio  # more aggressive as phase advances
    pct = rng.uniform(-jitter_pct, jitter_pct)/100.0 + bias
    new_amount = max(1.0, round(base_amount * (1.0 + pct), 2))
    return new_amount

def apply_delay(persona: str, base_delay: int, auction_features: Dict, bot_id: str, auction_id: str, step_index: int = 0) -> int:
    """
    Persona returns a seeded 'human-like' delay in seconds.
    """
    params = PERSONA_PARAMS.get(persona, PERSONA_PARAMS['patient'])
    rng = _seeded_rng(bot_id, auction_id, step_index + 1000)
    mean = params.get('time_mean', 10)
    jitter = params.get('time_jitter', 5)
    # exponential-like spacing for human-like waits + gaussian jitter
    try:
        exp = int(rng.expovariate(1.0 / max(1.0, (base_delay + mean)/2.0)))
    except Exception:
        exp = int(max(1, (base_delay + mean) // 2))
    gauss = int(round(rng.gauss(0, jitter/2.0)))
    delay = max(1, exp + gauss)
    return delay
