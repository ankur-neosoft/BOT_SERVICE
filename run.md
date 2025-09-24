# Run instructions

1. Create venv:
   python -m venv .venv
   source .venv/bin/activate

2. Install:
   pip install -r requirements.txt

3. Run server:
   uvicorn app.main:app --reload --port 8080

4. Example decide POST (curl):
   curl -X POST "http://localhost:8080/decide" -H "Content-Type: application/json" -d '{
     "auction_id":"auc123","item_id":"i1","bot_user_id":"bot1",
     "antecedent_features":{"phase_ratio":0.2,"time_left_pct":0.6,"recent_bid_rate":0.5,"last_bid_secs":0.1,"leader_dominance":0.2,"volatility":0.1},
     "auction_state":{"best_price":120},
     "min_inc_price":5,"max_inc_price":500,"threshold_price":500,
     "step_index":0
   }'
