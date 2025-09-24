from pydantic import BaseModel
from typing import Dict, Any, Optional

class DecideRequest(BaseModel):
    auction_id: str
    item_id: str
    bot_user_id: str
    antecedent_features: Dict[str, float]
    auction_state: Dict[str, Any]
    min_inc_price: float
    max_inc_price: float
    threshold_price: float
    step_index: Optional[int] = 0

class DecideResponse(BaseModel):
    status: str
    persona: Optional[str] = None
    bid_amount: Optional[float] = None
    delay_seconds: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None

class ReplayRequest(BaseModel):
    decision_inputs: Dict[str, Any]
