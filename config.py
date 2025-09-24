# config.py
import os
from typing import Dict, List
from dotenv import load_dotenv   # <-- add this

# Load .env file if present
load_dotenv()

# ---------- Basic runtime config ----------
# Base address of the external auction HTTP API (used by AucApi.py)
api_address = os.getenv("AUCTION_API_ADDRESS", "https://auction.example.com/api")

# Bot accounts (CSV in env or list here). These are the "bot user ids" used by AuctionPlayer.
_bot_csv = os.getenv("BOT_USER_IDS", "bot1,bot2,bot3")
bot_user_ids: List[str] = [i.strip() for i in _bot_csv.split(",") if i.strip()]

# Distributor config (port to publish auction snapshots, poll interval seconds)
auction_info_distributor_config = {
    "data_port": int(os.getenv("DISTRIBUTOR_PORT", "9505")),
    "time_between_api_calls": int(os.getenv("DISTRIBUTOR_POLL_SEC", "3")),
}

# ---------- Persona selection config ----------
PERSONAS = ["patient", "aggressive", "bluffer", "snipe"]

# Starting weight-matrix for persona scoring (tune via simulator)
WEIGHT_MATRIX: Dict[str, Dict[str, float]] = {
    "patient":   {"phase_ratio": 0.1, "time_left_pct": 0.6,  "recent_bid_rate": -0.2, "last_bid_secs": 0.5, "leader_dominance": -0.3, "volatility": 0.0},
    "aggressive":{"phase_ratio": 0.3, "time_left_pct": 0.2,  "recent_bid_rate": 0.5,  "last_bid_secs": -0.2,"leader_dominance": 0.1,  "volatility": 0.1},
    "bluffer":   {"phase_ratio": 0.4, "time_left_pct": 0.1,  "recent_bid_rate": 0.3,  "last_bid_secs": 0.3, "leader_dominance": 0.2,  "volatility": 0.5},
    "snipe":     {"phase_ratio": 0.7, "time_left_pct": -0.7, "recent_bid_rate": -0.4, "last_bid_secs": 0.3, "leader_dominance": -0.1, "volatility": 0.0},
}

# ---------- Tunables ----------
MAX_BIDS_PER_AUCTION = int(os.getenv("MAX_BIDS_PER_AUCTION", "6"))
BOT_THROTTLE_PER_MIN = int(os.getenv("BOT_THROTTLE_PER_MIN", "10"))
BUDGET_DEFAULT = float(os.getenv("BUDGET_DEFAULT", "10000"))
HYSTERESIS_WINDOW_SEC = int(os.getenv("HYSTERESIS_WINDOW_SEC", "30"))

# ---------- Flags ----------
# Production: SHOW_BOTS_TO_USERS should be True. UAT: you may set False if you hide from users.
SHOW_BOTS_TO_USERS = os.getenv("SHOW_BOTS_TO_USERS", "True").lower() in ("1","true","yes")
ALLOW_HIDDEN_BOTS_IN_UAT = os.getenv("ALLOW_HIDDEN_BOTS_IN_UAT", "True").lower() in ("1","true","yes")
