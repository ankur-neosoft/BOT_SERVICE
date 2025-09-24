from fastapi import APIRouter, HTTPException, Depends
from typing import Any, Dict
from app.schemas import DecideRequest, DecideResponse, ReplayRequest
from app.services.decision_service import DecisionService
from state_store import is_bot_assigned, assign_bot, get_bot_state, persist_bot_state

router = APIRouter()

# Instantiate a service (could be replaced with DI later)
decision_service = DecisionService()

@router.post("/decide", response_model=DecideResponse)
def decide(req: DecideRequest):
    try:
        # ensure single bot assignment
        if not is_bot_assigned(req.auction_id):
            assign_bot(req.auction_id, req.bot_user_id)

        # fetch state for bookkeeping (service will not mutate global store directly except persist)
        state = get_bot_state(req.auction_id, req.bot_user_id)
        if state is None:
            assign_bot(req.auction_id, req.bot_user_id)
            state = get_bot_state(req.auction_id, req.bot_user_id)

        result = decision_service.decide(
            req,
            state=state
        )

        # persist updated state returned by service (keeps persistence concerns here)
        persist_bot_state(req.auction_id, req.bot_user_id, result.get("updated_state", state))

        # strip updated_state from response payload
        resp = result.copy()
        resp.pop("updated_state", None)
        return resp

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/replay")
def admin_replay(req: ReplayRequest):
    # Admin-level deterministic replay of a decision
    return decision_service.replay(req.decision_inputs)
