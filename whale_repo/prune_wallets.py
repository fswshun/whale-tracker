#!/usr/bin/env python3
"""prune_wallets.py — クラスターから共有サービス/ボットとその子孫、深すぎる Solana ウォレットを外す。
   python whale_repo/prune_wallets.py <WHALE_ROOT> [--solana-max-depth 1]
   イベントも該当ウォレット分を落とし、残高スナップショットは次回実行で作り直す（削除）"""
import json, sys, argparse
from pathlib import Path
ap = argparse.ArgumentParser(); ap.add_argument("root"); ap.add_argument("--solana-max-depth", type=int, default=1); a = ap.parse_args()
root = Path(a.root); data = root / "data"; code = Path(__file__).resolve().parent.parent
services = {k for k in json.load(open(code / "services.json")) if not k.startswith("_")}
W = json.load(open(data / "wallets.json"))
def depth(x):
    d = 0
    while W.get(x, {}).get("parent"): x = W[x]["parent"]; d += 1
    return d
def tainted(x):
    while x:
        if x in services: return True
        x = W.get(x, {}).get("parent")
    return False
drop = {x for x in W if W[x]["role"] != "本体" and (tainted(x) or (not x.startswith("0x") and depth(x) > a.solana_max_depth))}
for x in drop: W.pop(x)
json.dump(W, open(data / "wallets.json", "w"), indent=2, ensure_ascii=False)
n_ev = 0
if (data / "events.jsonl").exists():
    ev = [json.loads(l) for l in open(data / "events.jsonl")]; keep = [e for e in ev if e["wallet"] in W]; n_ev = len(ev) - len(keep)
    with open(data / "events.jsonl", "w") as f:
        for e in keep: f.write(json.dumps(e, ensure_ascii=False) + "\n")
cur = json.load(open(data / "cursor.json")) if (data / "cursor.json").exists() else {}
cur = {k: v for k, v in cur.items() if k.split(":", 1)[1] in W}; json.dump(cur, open(data / "cursor.json", "w"), indent=2)
for f in ("snapshots.jsonl", "holdings.json", "pending_eoa.json"):
    if (data / f).exists(): (data / f).unlink()
print(f"削除ウォレット {len(drop)} / 残り {len(W)} / 落としたイベント {n_ev}")
