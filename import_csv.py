#!/usr/bin/env python3
"""
import_csv.py — 手元の CSV エクスポートを events.jsonl に取り込む（過去分の初期投入・オフライン確認用）

  python import_csv.py --chain robinhood blockscout_token_transfers_0x....csv ...
  python import_csv.py --chain bsc bscscan_txlist.csv bscscan_tokentx.csv
  python import_csv.py --render          # data/ から docs/index.html だけ作り直す

対応: Blockscout token-transfers CSV（TxHash,BlockNumber,UnixTimestamp,FromAddress,...）
      BscScan/Etherscan の Transactions CSV（Transaction Hash,...,Amount,Value (USD)）
      BscScan/Etherscan の ERC-20 Token Transfers CSV（Txhash,Blockno,UnixTimestamp,...,TokenValue,TokenSymbol）
ウォレット（本体/子）の登録は config.json と data/wallets.json に従う。未知EOAへの送金は子として自動登録。
"""
import argparse, csv, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

import tracker as T

def parse_ts(s):
    s = str(s).strip().replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try: return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError: pass
    return int(float(s))

def load(path, chain):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f); cols = rd.fieldnames or []
        for r in rd:
            if "TokensTransferred" in cols:  # Blockscout token transfers
                dec = int(r.get("TokenDecimals") or 18)
                rows.append(dict(chain=chain, tx=r["TxHash"], block=int(r["BlockNumber"]), ts=parse_ts(r["UnixTimestamp"]),
                                 frm=r["FromAddress"].lower(), to=r["ToAddress"].lower(), token=r["TokenSymbol"], contract=r["TokenContractAddress"].lower(),
                                 amount=int(r["TokensTransferred"]) / 10 ** dec, native=False, gas=float(r.get("TransactionFee") or 0) / 1e18,
                                 is_contract_call=float(r.get("TransactionFee") or 0) / 1e18 > 1e-4))
            elif "TokenValue" in cols or "Value" in cols and "TokenSymbol" in cols:  # Etherscan ERC-20
                val = (r.get("TokenValue") or r.get("Value") or "0").replace(",", "")
                rows.append(dict(chain=chain, tx=r.get("Txhash") or r.get("Transaction Hash"), block=int(r.get("Blockno") or 0), ts=parse_ts(r.get("UnixTimestamp") or r.get("DateTime (UTC)")),
                                 frm=r["From"].lower(), to=r["To"].lower(), token=r.get("TokenSymbol") or "?", contract=(r.get("ContractAddress") or "").lower(),
                                 amount=float(val), native=False, gas=0))
            elif "Amount" in cols:  # Etherscan transactions list
                m = re.match(r"([\d.,]+)\s*(\w+)", r["Amount"]); amt = float(m.group(1).replace(",", "")) if m else 0.0
                if amt == 0 and r.get("Method", "").lower() == "transfer":  # トークン送金の外側 tx: ネイティブ行としては不要
                    continue
                rows.append(dict(chain=chain, tx=r["Transaction Hash"], block=int(r["Blockno"]), ts=parse_ts(r["DateTime (UTC)"]),
                                 frm=r["From"].lower(), to=r["To"].lower(), token=T.CHAINS[chain]["native"], contract="native",
                                 amount=amt, native=True, gas=float(r.get("Txn Fee") or 0), is_contract_call=r.get("Method", "").lower() not in ("transfer", ""),
                                 usd_hint=float(r.get("Value (USD)", "0").replace("$", "").replace(",", "") or 0)))
    return rows

def ingest(rows):
    from collections import defaultdict
    new = []
    # 所有者は「行に登場する監視ウォレット」
    contract_like = {r["to"] for r in rows if r.get("is_contract_call") and r["to"]}
    for a in contract_like:
        if a not in T.labels and not T.is_watched(a): T.labels[a] = f"DEX/コントラクト(推定) {a[:6]}…{a[-4:]}"
    seen = set(); by_tx = defaultdict(list)
    for r in rows:
        k = (r["tx"], r["frm"], r["to"], r["contract"], round(r["amount"], 9))
        if k in seen: continue
        seen.add(k); by_tx[r["tx"]].append(r)
    for tx, trs in sorted(by_tx.items(), key=lambda kv: kv[1][0]["ts"]):
        owners = {a for r in trs for a in (r["frm"], r["to"]) if T.is_watched(a)}
        for w in owners:
            if f"{trs[0]['chain']}:{tx}:{w}" in T.seen_tx: continue
            T.seen_tx.add(f"{trs[0]['chain']}:{tx}:{w}")
            kind, outs, ins, cp = T.classify_tx(trs[0]["chain"], w, trs)
            for r in outs + ins:
                px = T.price(r["chain"], r["token"], r["contract"]) if not r.get("usd_hint") else r["usd_hint"] / r["amount"]
                ev = dict(time=datetime.fromtimestamp(r["ts"], timezone.utc).isoformat(), ts=r["ts"], chain=r["chain"], wallet=w, tx=tx, kind=kind,
                          dir="OUT" if r in outs else "IN", token=r["token"], contract=r["contract"], amount=r["amount"], price=px,
                          usd=r["amount"] * px if px else None, cp=cp, cp_label=T.label(cp) if cp else "")
                if ev["dir"] == "IN" and ev["usd"] is not None and ev["usd"] < T.DUST_USD and not T.is_watched(cp):
                    ev["kind"] = "ダスト"
                if ev["kind"] == "外部へ送金" and cp and T.wallets[w]["role"] in ("本体", "子"):
                    seed = r["native"] and (ev["usd"] or 0) < T.GAS_SEED_USD
                    if seed or (not r["native"] and not r.get("is_contract_call")):
                        T.wallets[cp] = {"role": "子" if T.wallets[w]["role"] == "本体" else "孫", "parent": w, "first_seen": ev["time"], "chains": [r["chain"]]}
                        ev["kind"] = "新ウォレット開設(ガス種銭)" if seed else "内部移動"; ev["cp_label"] = T.label(cp)
                new.append(ev)
    T.events.extend(new)
    T.resolve_purchases(T.events)
    return new

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--chain", default="robinhood", choices=T.CHAINS.keys())
    ap.add_argument("files", nargs="*"); ap.add_argument("--render", action="store_true"); a = ap.parse_args()
    if a.files:
        rows = [r for p in a.files for r in load(p, a.chain)]
        new = ingest(rows)
        json.dump(T.wallets, open(T.DATA / "wallets.json", "w"), indent=2, ensure_ascii=False)
        json.dump(sorted(T.seen_tx), open(T.DATA / "seen_tx.json", "w"))
        with open(T.DATA / "events.jsonl", "w") as f:
            for e in sorted(T.events, key=lambda e: e["ts"]): f.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"取り込み {len(new)} 件 / 監視ウォレット {len(T.wallets)}")
    hold = T.jload(T.DATA / "holdings.json", {"holdings": {}})["holdings"]
    T.html(hold)
    print("docs/index.html 生成")
