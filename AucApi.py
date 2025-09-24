# AucApi.py
# Lightweight port of auction API calls.
# All functions return {'status': 'success'|'error', 'message': ...}
# Replace api_address in your config or env.

import requests
import json
from typing import Any, Dict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime
import logging
from config import api_address  # reuse your config or set environment variable

logger = logging.getLogger("AucApi")
session = requests.Session()
retries = Retry(total=1, backoff_factor=0.3, status_forcelist=[500,502,503,504])
session.mount("http://", HTTPAdapter(max_retries=retries))
session.mount("https://", HTTPAdapter(max_retries=retries))

def _post(endpoint: str, payload: Dict[str, Any], timeout: int = 10) -> Dict[str, Any]:
    url = api_address.rstrip('/') + '/' + endpoint.lstrip('/')
    headers = {'Content-Type': 'application/json'}

    # ---- DEV FALLBACK ----
    if "auction.example.com" in api_address:
        logger.info("[MOCK] POST %s with payload=%s", endpoint, payload)
        return {
            'status': 'success',
            'message': {
                'endpoint': endpoint,
                'payload': payload,
                'mocked': True,
                'bid_id': 'MOCK-BID-123',
                'timestamp': datetime.utcnow().isoformat() + "Z"
            }
        }
    # ----------------------

    try:
        r = session.post(url, headers=headers, data=json.dumps(payload), timeout=timeout)
        logger.debug("POST %s => %s", url, r.status_code)
        if r.status_code == 200:
            return {'status': 'success', 'message': r.json()}
        else:
            return {'status': 'error', 'message': f"Status {r.status_code}", 'details': r.text}
    except Exception as e:
        logger.exception("Error calling %s", url)
        return {'status': 'error', 'message': str(e)}


def _get(endpoint: str, timeout: int = 10) -> Dict[str, Any]:
    url = api_address.rstrip('/') + '/' + endpoint.lstrip('/')

    # ---- DEV FALLBACK ----
    # If api_address is still the placeholder, return synthetic auction data
    if "auction.example.com" in api_address:
        return {
            'status': 'success',
            'message': {
                "AUC_1": {
                    "AUCTION_ID": "AUC_1",
                    "AUCTION_ITEMS": {
                        1: {
                            "ITEM_ID": 1,
                            "BEST_PRICE": 100,
                            "THRESHOLD_PRICE1": 500
                        }
                    },
                    "MIN_BID_AMOUNT": 10,
                    "MAX_BID_AMOUNT": 1000
                }
            }
        }
    # ----------------------

    try:
        r = session.get(url, timeout=timeout)
        logger.debug("GET %s => %s", url, r.status_code)
        if r.status_code == 200:
            return {'status': 'success', 'message': r.json()}
        else:
            return {'status': 'error', 'message': f"Status {r.status_code}", 'details': r.text}
    except Exception as e:
        logger.exception("Error calling %s", url)
        return {'status': 'error', 'message': str(e)}


# --- Public functions used by the distributor and player ---

def get_all_live_auction_data() -> Dict[str, Any]:
    """Call API endpoint used previously to fetch live auctions."""
    # Matches the previous GetLiveAuctionsDetails call shape used by AuctionInfoDistributor.
    return _get("GetLiveAuctionsDetails")

def get_current_bidding_details(auction_id: str) -> Dict[str, Any]:
    payload = {"auctionIds": [auction_id], "userId": "15863"}
    return _post("Current_Bidding", payload)

def submit_bid(auction_id: str, item_id: int, bid_amount: float, user_id: str, min_bid_amount: float = None) -> Dict[str, Any]:
    payload = {
        "auctionId": auction_id,
        "itemId": item_id,
        "userId": user_id,
        "bidAmount": bid_amount
    }
    if min_bid_amount is not None:
        payload["minBidAmount"] = min_bid_amount
    return _post("SubmitAuctionBid", payload)

def submit_auto_bid(auction_id: str, item_id: int, auto_bid_amount: float, min_bid_amount: float, user_id: str) -> Dict[str, Any]:
    payload = {
        "auctionId": auction_id,
        "itemId": item_id,
        "autoBidAmount": auto_bid_amount,
        "minBidAmount": min_bid_amount,
        "userId": user_id
    }
    return _post("SubmitAutoBid", payload)

def get_auction_time_from_snapshot(snapshot: dict, auction_id: str) -> Dict[str, Any]:
    """Helper to compute start/expiry times from a live snapshot (similar to old get_auction_time)."""
    try:
        auction = snapshot[auction_id]
        return {'status': 'success', 'message': {
            'start_time': auction['AUCTION_VISIBLE_DATE_TIME'],
            'expiry_time': auction['AUCTION_EXPIRY_DATE_TIME']
        }}
    except Exception as e:
        logger.exception("get_auction_time_from_snapshot")
        return {'status': 'error', 'message': str(e)}
