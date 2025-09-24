# state_store.py - Redis-backed state store with in-memory fallback
# Usage: set REDIS_URL in your .env (example: redis://localhost:6379/0)
# Requires: pip install redis

import os
import json
from typing import Dict, Any, Optional
from datetime import datetime
import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Try to connect to Redis
_use_redis = False
_redis = None
try:
    _redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    _redis.ping()  # test connection
    _use_redis = True
    print("✅ Using Redis at", REDIS_URL)
except Exception as e:
    print("⚠️ Redis not available, falling back to in-memory store:", e)
    _use_redis = False

# In-memory fallback storage
_mem_hashes: Dict[str, Dict[str, str]] = {}
_mem_strings: Dict[str, str] = {}
_mem_sets: Dict[str, set] = {}


def _auction_key(auction_id: str) -> str:
    return f"auction:{auction_id}"


def _bot_state_key(auction_id: str, bot_id: str) -> str:
    return f"{_auction_key(auction_id)}:bot:{bot_id}"


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


# ----------------- API ----------------- #

def assign_bot(auction_id: str, bot_id: str) -> None:
    akey = _auction_key(auction_id)
    bot_key = _bot_state_key(auction_id, bot_id)

    if _use_redis:
        pipe = _redis.pipeline()
        pipe.hset(akey, "assigned_bot", bot_id)
        pipe.sadd("auctions", auction_id)
        if not _redis.exists(bot_key):
            default = {
                "persona_current": None,
                "num_bids_placed": 0,
                "last_action_time": None,
                "budget_remaining": 100000,
            }
            pipe.set(bot_key, json.dumps(default))
        pipe.execute()
    else:
        _mem_hashes.setdefault(akey, {})["assigned_bot"] = bot_id
        _mem_sets.setdefault("auctions", set()).add(auction_id)
        if bot_key not in _mem_strings:
            default = {
                "persona_current": None,
                "num_bids_placed": 0,
                "last_action_time": None,
                "budget_remaining": 100000,
            }
            _mem_strings[bot_key] = json.dumps(default)


def get_bot_state(auction_id: str, bot_id: str) -> Optional[Dict[str, Any]]:
    key = _bot_state_key(auction_id, bot_id)
    val = _redis.get(key) if _use_redis else _mem_strings.get(key)
    if not val:
        return None
    try:
        return json.loads(val)
    except Exception:
        return None


def persist_bot_state(auction_id: str, bot_id: str, state: Dict[str, Any]) -> None:
    key = _bot_state_key(auction_id, bot_id)
    val = json.dumps(state)
    if _use_redis:
        _redis.set(key, val)
    else:
        _mem_strings[key] = val


def is_bot_assigned(auction_id: str) -> bool:
    akey = _auction_key(auction_id)
    if _use_redis:
        return _redis.hexists(akey, "assigned_bot")
    return "assigned_bot" in _mem_hashes.get(akey, {})


def assigned_bot_for(auction_id: str) -> Optional[str]:
    akey = _auction_key(auction_id)
    if _use_redis:
        v = _redis.hget(akey, "assigned_bot")
    else:
        v = _mem_hashes.get(akey, {}).get("assigned_bot")
    return v if v else None


def dump_all() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if _use_redis:
        auctions = _redis.smembers("auctions")
    else:
        auctions = _mem_sets.get("auctions", set())

    for auction_id in auctions:
        akey = _auction_key(auction_id)
        if _use_redis:
            assigned = _redis.hget(akey, "assigned_bot")
            bots = {}
            pattern = f"{akey}:bot:*"
            for bot_key in _redis.scan_iter(match=pattern):
                bot_id = bot_key.split(":")[-1]
                val = _redis.get(bot_key)
                try:
                    bots[bot_id] = json.loads(val) if val else None
                except Exception:
                    bots[bot_id] = val
        else:
            assigned = _mem_hashes.get(akey, {}).get("assigned_bot")
            bots = {}
            prefix = f"{akey}:bot:"
            for bot_key, val in _mem_strings.items():
                if bot_key.startswith(prefix):
                    bot_id = bot_key.split(":")[-1]
                    try:
                        bots[bot_id] = json.loads(val) if val else None
                    except Exception:
                        bots[bot_id] = val

        out[auction_id] = {"assigned_bot": assigned, "bot_state": bots}
    return out
