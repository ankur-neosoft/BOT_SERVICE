from typing import Dict, Any
from app.chooser import select_persona
from app.hybrid_core import compute_base_amount, estimate_base_delay
from app.persona import apply_amount, apply_delay
from app.fuzzy_logic import compute_bid_category
from app.utils import decision_inputs_hash, now_iso

class DecisionService:
    """
    Single place to orchestrate a decision.
    Returns a dict with the response payload and updated_state (optional).
    """

    def __init__(self, agent_name: str = "AutoPlayer-v2"):
        self.agent = agent_name

    def decide(self, req_obj, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        req_obj: Pydantic request (DecideRequest)
        state: current bot state dict (mutable copy OK)
        """
        # Persona selection if not set
        if state.get("persona_current") is None:
            persona = select_persona(req_obj.antecedent_features, req_obj.bot_user_id, req_obj.auction_id)
            state["persona_current"] = persona
        else:
            persona = state["persona_current"]

        # fuzzy category to influence amount
        fuzzy_inputs = {
            "price_to_cover": req_obj.antecedent_features.get("phase_ratio", 0.0),
            "active_users": req_obj.antecedent_features.get("recent_bid_rate", 0.0),
            "auction_phase": req_obj.antecedent_features.get("time_left_pct", 0.5),
        }
        category, fscore = compute_bid_category(fuzzy_inputs)

        # compute base amount/delay
        base_amount = compute_base_amount({
            "best_price": req_obj.auction_state.get("best_price", 0),
            "threshold_price": req_obj.threshold_price,
            "min_inc_price": req_obj.min_inc_price,
            "recent_bid_rate": req_obj.antecedent_features.get("recent_bid_rate", 1.0),
            "phase_ratio": req_obj.antecedent_features.get("phase_ratio", 0.0),
        })
        base_delay = estimate_base_delay({
            "recent_bid_rate": req_obj.antecedent_features.get("recent_bid_rate", 1.0),
            "time_left_pct": req_obj.antecedent_features.get("time_left_pct", 0.5),
        })

        # persona-driven adjustments
        amount = apply_amount(persona, base_amount * (1.0 + (fscore - 0.5) * 0.4), req_obj.antecedent_features, req_obj.bot_user_id, req_obj.auction_id, req_obj.step_index)
        delay = apply_delay(persona, base_delay, req_obj.antecedent_features, req_obj.bot_user_id, req_obj.auction_id, req_obj.step_index)

        # safety: quantize to min_inc and enforce max_inc
        min_inc = float(req_obj.min_inc_price)
        max_inc = float(req_obj.max_inc_price)
        quantized = max(min_inc, round((amount + 0.0) / min_inc) * min_inc)
        if quantized > max_inc:
            quantized = max_inc

        # update bookkeeping fields in state
        state["num_bids_placed"] = state.get("num_bids_placed", 0) + 1
        state["last_action_time"] = now_iso()
        state["budget_remaining"] = state.get("budget_remaining", 10000) - quantized

        metadata = {
            "is_automated": True,
            "agent": self.agent,
            "persona": persona,
            "fuzzy_category": category,
            "fuzzy_score": fscore,
            "decision_inputs_hash": decision_inputs_hash({
                "auction_id": req_obj.auction_id,
                "antecedent_features": req_obj.antecedent_features,
                "auction_state": req_obj.auction_state,
                "min_inc_price": req_obj.min_inc_price,
                "max_inc_price": req_obj.max_inc_price,
                "threshold_price": req_obj.threshold_price,
            }),
            "generated_at": now_iso()
        }

        return {
            "status": "ok",
            "persona": persona,
            "bid_amount": quantized,
            "delay_seconds": delay,
            "metadata": metadata,
            "updated_state": state  # up to caller to persist
        }

    def replay(self, decision_inputs: Dict[str, Any]) -> Dict[str, Any]:
        """
        Deterministic replay endpoint used by admin.
        decision_inputs should contain same keys as used in /decide request.
        """
        class SimpleReq: pass
        r = SimpleReq()
        r.auction_id = decision_inputs.get("auction_id", "auc")
        r.item_id = decision_inputs.get("item_id", "i1")
        r.bot_user_id = decision_inputs.get("bot_user_id", "bot")
        r.antecedent_features = decision_inputs.get("antecedent_features", {})
        r.auction_state = decision_inputs.get("auction_state", {})
        r.min_inc_price = decision_inputs.get("min_inc_price", 1)
        r.max_inc_price = decision_inputs.get("max_inc_price", 10000)
        r.threshold_price = decision_inputs.get("threshold_price", r.auction_state.get("threshold_price", 0))
        r.step_index = decision_inputs.get("step_index", 0)

        # call the same decide flow but with a fresh state copy
        initial_state = decision_inputs.get("state", {
            "persona_current": None,
            "num_bids_placed": 0,
            "budget_remaining": 10000,
        })
        resp = self.decide(r, state=initial_state)
        # remove updated_state for replay return (or keep if you want)
        updated = resp.get("updated_state")
        resp.pop("updated_state", None)
        resp["replayed_state"] = updated
        return resp
