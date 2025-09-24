import random, json
from .chooser import select_persona
from .hybrid_core import compute_base_amount, estimate_base_delay
from .persona import apply_amount, apply_delay

def random_scenario(seed=0):
    random.seed(seed)
    f = {
        'phase_ratio': random.random(),
        'time_left_pct': random.random(),
        'recent_bid_rate': min(3.0, random.random() * 2.0),
        'last_bid_secs': random.random(),
        'leader_dominance': random.random(),
        'volatility': random.random()
    }
    return f

def run(n=200):
    samples = []
    for i in range(n):
        f = random_scenario(i)
        persona = select_persona(f, 'bot1', f'auc{i}')
        base_amt = compute_base_amount({'best_price': 100 + (i % 50), 'threshold_price': 500, 'min_inc_price': 5, 'recent_bid_rate': f['recent_bid_rate'], 'phase_ratio': f['phase_ratio']})
        amt = apply_amount(persona, base_amt, f, 'bot1', f'auc{i}', 0)
        delay = apply_delay(persona, estimate_base_delay({'recent_bid_rate': f['recent_bid_rate'], 'time_left_pct': f['time_left_pct']}), f, 'bot1', f'auc{i}', 0)
        samples.append({'persona': persona, 'amt': amt, 'delay': delay})
    personas = {}
    for s in samples:
        personas.setdefault(s['persona'], []).append(s)
    out = {p: {'count': len(v), 'avg_amt': round(sum([x['amt'] for x in v])/len(v),2), 'avg_delay': round(sum([x['delay'] for x in v])/len(v),2)} for p,v in personas.items()}
    print(json.dumps(out, indent=2))

if __name__ == '__main__':
    run(200)
