"""
portfolio.py — クラスター（本体＋子＋孫）全体の資産推移と、増減の分解（値動き / 利確 / 買い）

用語
  リスク資産(risk)  : アルトコイン全般
  準現金(quasi)     : ETH / BNB（ネイティブ・ラップド）
  現金(cash)        : 本物のステーブル（KNOWN_STABLES）
  利確              : リスク資産の売り（スワップで準現金/現金/別銘柄へ）＋ クラスター外への流出
  買い              : リスク資産のスワップ買い・クロスチェーン購入

時点（cutoff）は 00:00 UTC = 09:00 JST。日次レポートもこの時刻基準。
"""
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
DAY = 86400
ROLE_ORDER = {"本体": 0, "子": 1, "孫": 2, "曾孫": 3}
SWAP_KINDS = {"購入(スワップ)", "売却(スワップ)", "売却(スワップ・代金不明)", "スワップ"}
SERVICE_IN_KINDS = {"購入(クロスチェーン)", "受取(原資未確認)", "サービスから受取"}
NOISE_KINDS = {"ダスト", "なりすまし(偽送金)", "コントラクト呼出"}
DEX_URL = "https://dexscreener.com/{dex}/{contract}"

# ------------------------------------------------------------ 表示ヘルパ
def jst(ts): return datetime.fromtimestamp(ts, JST)
def fmt_usd(v, signed=False):
    if v is None: return "—"
    s = "+" if (signed and v > 0) else ("−" if v < 0 else "")
    a = abs(v)
    if a >= 1e9: body = f"${a / 1e9:.2f}B"
    elif a >= 1e6: body = f"${a / 1e6:.2f}M"
    elif a >= 1e4: body = f"${a / 1e3:.0f}K"
    else: body = f"${a:,.0f}"
    return s + body
def fmt_qty(v):
    a = abs(v)
    if a >= 1e8: return f"{v / 1e8:,.2f}億枚"
    if a >= 1e4: return f"{v / 1e4:,.1f}万枚".replace(".0万", "万")
    if a >= 1000: return f"{v:,.0f}枚"
    return f"{v:,.4g}"
def fmt_pct(v): return "—" if v is None else f"{v:+.1f}%".replace("-", "−")

def role_names(wallets):
    """アドレス → 「本体 / 子1 / 子2 / 孫1 …」。アドレスは表に出さない"""
    order = sorted(wallets, key=lambda w: (ROLE_ORDER.get(wallets[w]["role"], 9), wallets[w].get("first_seen") or "", w))
    cnt = defaultdict(int); names = {}
    n_main = sum(1 for w in wallets if wallets[w]["role"] == "本体")
    for w in order:
        r = wallets[w]["role"]; cnt[r] += 1
        names[w] = ("本体" if n_main == 1 else f"本体{cnt[r]}") if r == "本体" else f"{r}{cnt[r]}"
    return names

# ------------------------------------------------------------ 分類
def bucket_of(chain, contract, ctx):
    c = (contract or "").lower()
    if c == "native": return "quasi"
    ch = ctx["chains"].get(chain, {})
    if c and c == (ch.get("wnative") or "").lower(): return "quasi"
    if (chain, c) in ctx["known_stables"]: return "cash"
    return "risk"

def flow_of(e, ctx):
    """buy / sell / out / in / cash / internal / noise / other"""
    k = e["kind"]; b = bucket_of(e["chain"], e["contract"], ctx)
    if k in NOISE_KINDS: return "noise"
    if k in ("内部移動", "新ウォレット開設(ガス種銭)"): return "internal"
    if k in SWAP_KINDS:
        if e["dir"] == "IN": return "buy" if b == "risk" else "cash"
        return "sell" if b == "risk" else "cash"
    if k in SERVICE_IN_KINDS: return "buy" if b == "risk" else "cash"
    if k == "サービスへ送金": return "out" if b == "risk" else "cash"
    if k in ("外部へ送金", "バーン"): return "out"
    if k == "受取": return "in"
    return "other"

# ------------------------------------------------------------ スナップショット
def build_snapshot(holdings_doc, ctx):
    """holdings.json → クラスター合算の1時点。価格の無い/極小のリスク銘柄は落とす"""
    pos, byw = {}, {}
    for key, h in holdings_doc.get("holdings", {}).items():
        ch, w = key.split(":")
        for c, v in h.items():
            b = bucket_of(ch, v.get("contract"), ctx); usd = v.get("usd") or 0.0
            if b == "risk" and usd < 100: continue
            k = f"{ch}:{v.get('contract')}"
            p = pos.setdefault(k, {"sym": v.get("symbol") or c, "amt": 0.0, "px": v.get("price"), "usd": 0.0, "b": b})
            p["amt"] += v["amount"]; p["usd"] += usd
            ww = byw.setdefault(w, {"total": 0.0, "risk": 0.0}); ww["total"] += usd; ww["risk"] += usd if b == "risk" else 0.0
    tot = {"total": 0.0, "risk": 0.0, "quasi": 0.0, "cash": 0.0}
    for p in pos.values(): tot["total"] += p["usd"]; tot[p["b"]] += p["usd"]
    upd = holdings_doc.get("updated")
    ts = int(datetime.fromisoformat(upd).timestamp()) if upd else int(datetime.now(timezone.utc).timestamp())
    return {"ts": ts, "time": datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds"), **{k: round(v, 2) for k, v in tot.items()},
            "by_wallet": {w: {k: round(v, 2) for k, v in d.items()} for w, d in byw.items()}, "pos": pos}

def load_snapshots(path):
    if not path.exists(): return []
    out = []
    for l in open(path):
        l = l.strip()
        if l:
            try: out.append(json.loads(l))
            except json.JSONDecodeError: pass
    out.sort(key=lambda s: s["ts"])
    return out

def prune_snapshots(snaps, keep_hourly_days=7):
    """直近7日は全部、それより前は 00:00 UTC 直前の1本だけ残す"""
    if not snaps: return snaps
    now = snaps[-1]["ts"]; keep = []; seen_cut = set()
    for s in reversed(snaps):
        if now - s["ts"] <= keep_hourly_days * DAY: keep.append(s); continue
        cut = (s["ts"] // DAY + 1) * DAY        # この時点が属する日の次の 00:00 UTC
        if cut not in seen_cut: seen_cut.add(cut); keep.append(s)
    return sorted(keep, key=lambda s: s["ts"])

def save_snapshots(path, snaps):
    with open(path, "w") as f:
        for s in snaps: f.write(json.dumps(s, ensure_ascii=False) + "\n")

def cutoff_points(snaps, n=30):
    """00:00 UTC(9:00 JST) ごとの時点 + 現在。各時点はその直前(6時間以内)のスナップショット"""
    if not snaps: return []
    first, last = snaps[0]["ts"], snaps[-1]["ts"]; pts = []
    c = (first // DAY + 1) * DAY
    if c - first > 900:   # 記録開始が 00:00 UTC ちょうどでない場合は「開始」時点を置く（初日の日次レポート用）
        pts.append({"ts": first, "label": jst(first).strftime("開始 %-m/%-d %H:%M"), "date": jst(first).strftime("%Y-%m-%d"), "snap": snaps[0]})
    while c <= last + 900:
        cand = [s for s in snaps if c - 6 * 3600 <= s["ts"] <= c + 900]
        if cand:
            s = min(cand, key=lambda s: abs(s["ts"] - c))
            pts.append({"ts": c, "label": jst(c).strftime("%-m/%-d 9:00"), "date": jst(c).strftime("%Y-%m-%d"), "snap": s})
        c += DAY
    if not pts or pts[-1]["snap"]["ts"] != last:
        pts.append({"ts": last, "label": "現在", "date": "now", "snap": snaps[-1]})
    return pts[-n:]

# ------------------------------------------------------------ 増減の分解
def group_trades(evs, flow_sel, ctx, min_usd=0.0):
    """同一ウォレット・同一銘柄の売買を束ねる。相手足（何で払ったか / 何を受け取ったか）も付ける"""
    by_tx = defaultdict(list)
    for e in evs: by_tx[(e["wallet"], e["tx"])].append(e)
    agg = {}
    for (w, tx), legs in by_tx.items():
        for e in legs:
            if flow_of(e, ctx) != flow_sel: continue
            key = (w, e["chain"], (e["contract"] or "").lower())
            a = agg.setdefault(key, {"wallet": w, "chain": e["chain"], "contract": (e["contract"] or "").lower(), "sym": e["token"], "amount": 0.0, "usd": 0.0,
                                     "n": 0, "other": defaultdict(float), "first": e["ts"], "last": e["ts"], "funding": 0.0, "note": ""})
            a["amount"] += e["amount"]; a["usd"] += e.get("usd") or 0.0; a["n"] += 1
            a["first"] = min(a["first"], e["ts"]); a["last"] = max(a["last"], e["ts"])
            if e.get("funding_usd"): a["funding"] += e["funding_usd"]; a["note"] = e.get("funding_note", "")
            for l in legs:
                if l["dir"] != e["dir"] and l["amount"] > 0: a["other"][l["token"]] += l["amount"]
    out = [a for a in agg.values() if a["usd"] >= min_usd]
    for a in out: a["other"] = dict(a["other"])
    return sorted(out, key=lambda a: -a["usd"])

def other_leg_text(a):
    if a["other"]: return "、".join(f"{fmt_qty(v)} {k}" for k, v in sorted(a["other"].items(), key=lambda kv: -kv[1])[:2])
    if a["funding"]: return f"原資 {fmt_usd(a['funding'])}（{a['note'][:40]}）" if a["note"] else f"原資 {fmt_usd(a['funding'])}"
    return "相手不明"

def bridge(p0, p1, events, ctx, min_usd=5000.0):
    """2時点間のリスク資産の増減を 値動き / 買い / 利確(売り+流出) / 流入 / 誤差 に分解"""
    s0, s1 = p0["snap"], p1["snap"]; t0, t1 = s0["ts"], s1["ts"]
    price, movers = 0.0, []
    for k, p in s0["pos"].items():
        if p["b"] != "risk" or not p.get("px"): continue
        q = s1["pos"].get(k)
        if q and q.get("px"):
            d = p["amt"] * (q["px"] - p["px"]); price += d
            movers.append({"sym": p["sym"], "usd": d, "pct": (q["px"] / p["px"] - 1) * 100, "hold": q["usd"]})
    evs = [e for e in events if t0 < e["ts"] <= t1]
    buys = group_trades(evs, "buy", ctx); sells = group_trades(evs, "sell", ctx)
    outs = group_trades([e for e in evs if bucket_of(e["chain"], e["contract"], ctx) == "risk"], "out", ctx)
    ins = group_trades([e for e in evs if bucket_of(e["chain"], e["contract"], ctx) == "risk" and (e.get("usd") or 0) >= min_usd
                        and f"{e['chain']}:{(e['contract'] or '').lower()}" in s1["pos"]], "in", ctx)
    cash_outs = group_trades([e for e in evs if bucket_of(e["chain"], e["contract"], ctx) != "risk" and e["kind"] in ("外部へ送金", "バーン")], "out", ctx)
    internal = [e for e in evs if flow_of(e, ctx) == "internal" and e["dir"] == "OUT" and (e.get("usd") or 0) >= min_usd]
    B, S, O, I = (sum(a["usd"] for a in buys), sum(a["usd"] for a in sells), sum(a["usd"] for a in outs), sum(a["usd"] for a in ins))
    risk0, risk1 = s0["risk"], s1["risk"]
    resid = risk1 - risk0 - price - B + S + O - I
    return {"t0": t0, "t1": t1, "label0": p0["label"], "label1": p1["label"],
            "total0": s0["total"], "total1": s1["total"], "risk0": risk0, "risk1": risk1,
            "quasi0": s0["quasi"], "quasi1": s1["quasi"], "cash0": s0["cash"], "cash1": s1["cash"],
            "price": price, "buys": B, "sells": S, "out": O, "in": I, "resid": resid,
            "realized": S + O, "realized_pct": ((S + O) / risk0 * 100) if risk0 else None,
            "total_pct": ((s1["total"] / s0["total"] - 1) * 100) if s0["total"] else None,
            "risk_pct": ((risk1 / risk0 - 1) * 100) if risk0 else None,
            "min_usd": min_usd, "movers": sorted(movers, key=lambda m: -abs(m["usd"])), "buy_list": buys, "sell_list": sells,
            "out_list": outs, "in_list": ins, "cash_out_list": cash_outs, "internal": internal, "n_events": len(evs)}

def notable(br, min_usd=5000.0, price_pct=5.0):
    """1時間まとめを送るべきか：利確/買い/流出が min_usd 以上、または値動きがリスク資産の price_pct% 以上"""
    if br["realized"] >= min_usd or br["buys"] >= min_usd or br["out"] >= min_usd: return True
    if br["risk0"] and abs(br["price"]) / br["risk0"] * 100 >= price_pct: return True
    return False

# ------------------------------------------------------------ 文章化（Telegram）
def dex_link(chain, contract, ctx):
    dex = ctx["chains"].get(chain, {}).get("dex")
    return DEX_URL.format(dex=dex, contract=contract) if dex and contract and contract != "native" else ""

def chain_name(chain, ctx): return ctx["chains"].get(chain, {}).get("name", chain)

def buy_alert_lines(buys, names, ctx):
    lines = []
    for a in buys:
        who = names.get(a["wallet"], a["wallet"][:6]); t = jst(a["last"]).strftime("%m-%d %H:%M")
        times = f"（{a['n']}回）" if a["n"] > 1 else ""
        lines.append(f"🟢 買い  {who}  {a['sym']} {fmt_qty(a['amount'])} ≈ {fmt_usd(a['usd'])}\n"
                     f"   支払 {other_leg_text(a)}{times}  {t} JST  {chain_name(a['chain'], ctx)}")
        link = dex_link(a["chain"], a["contract"], ctx)
        if link: lines.append(f"   {link}")
    return lines

def digest_text(br, names, ctx, title, pages="", max_items=4):
    L = [title, f"総資産 {fmt_usd(br['total1'])}（{fmt_usd(br['total1'] - br['total0'], True)} / {fmt_pct(br['total_pct'])}）"
              f"　リスク {fmt_usd(br['risk1'])}　準現金 {fmt_usd(br['quasi1'])}　現金 {fmt_usd(br['cash1'])}"]
    L.append(f"リスク資産 {fmt_usd(br['risk0'])} → {fmt_usd(br['risk1'])}：値動き {fmt_usd(br['price'], True)} / 利確 {fmt_usd(-br['realized'], True)}"
             f"（{br['realized_pct']:.1f}%）/ 買い {fmt_usd(br['buys'], True)}" + (f" / 流入 {fmt_usd(br['in'], True)}" if br["in"] else "")
             + (f" / 誤差 {fmt_usd(br['resid'], True)}" if abs(br["resid"]) >= 0.02 * max(br["risk0"], 1) else ""))
    m = br.get("min_usd", 0); sells = [a for a in br["sell_list"] if a["usd"] >= m]; outs = [a for a in br["out_list"] + br["cash_out_list"] if a["usd"] >= m]; buys = [a for a in br["buy_list"] if a["usd"] >= m]
    if sells:
        L.append("🔻 利確: " + "、".join(f"{names.get(a['wallet'], '?')} {a['sym']} {fmt_qty(a['amount'])} → {other_leg_text(a)} ({fmt_usd(a['usd'])})" for a in sells[:max_items]))
    if outs:
        L.append("📤 外部流出: " + "、".join(f"{names.get(a['wallet'], '?')} {a['sym']} {fmt_qty(a['amount'])} ({fmt_usd(a['usd'])})" for a in outs[:max_items]))
    if buys:
        L.append("🟢 買い: " + "、".join(f"{names.get(a['wallet'], '?')} {a['sym']} {fmt_qty(a['amount'])} ({fmt_usd(a['usd'])})" for a in buys[:max_items]))
    mv = [m for m in br["movers"] if abs(m["usd"]) >= 0.01 * max(br["risk0"], 1)][:max_items]
    if mv: L.append("📈 値動き: " + "、".join(f"{m['sym']} {m['pct']:+.1f}% ({fmt_usd(m['usd'], True)})" for m in mv))
    if br["internal"]:
        L.append("↔ 内部移動: " + "、".join(f"{names.get(e['wallet'], '?')}→{names.get(e['cp'], '外')} {e['token']} {fmt_qty(e['amount'])}" for e in br["internal"][:3]))
    if pages and "<" not in pages: L.append(f"詳細: {pages}")
    return "\n".join(L)
