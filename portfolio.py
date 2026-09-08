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

def L(a):
    """アドレス正規化: EVM(0x…)は小文字、Solana(base58)は大文字小文字を保持"""
    a = a or ""
    return a.lower() if a.startswith("0x") else a
JST = timezone(timedelta(hours=9))
DAY = 86400
CUT_OFF = 0          # 1日の締め時刻（UTC 0:00 からの秒数）。tracker が config の daily_report_hour_utc から設定する（15*3600 = 24:00 JST）
def next_cut(ts): return int(((ts - CUT_OFF) // DAY + 1) * DAY + CUT_OFF)      # ts より後の最初の締め時刻
def last_cut(ts): return int(((ts - CUT_OFF) // DAY) * DAY + CUT_OFF)          # ts 以前の最後の締め時刻
def cut_date(c): return datetime.fromtimestamp(c - 1, JST)                      # 締め時刻が属する日（24:00 JST 締めなら前日扱い）
def cut_label(c):
    j = datetime.fromtimestamp(c, JST)
    return cut_date(c).strftime("%-m/%-d") + " 24:00" if (j.hour, j.minute) == (0, 0) else j.strftime("%-m/%-d %H:%M")
def cut_desc():
    j = datetime.fromtimestamp(CUT_OFF, timezone.utc).astimezone(JST)
    return "24:00 JST" if (j.hour, j.minute) == (0, 0) else j.strftime("%H:%M JST")
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
    if a >= 1e16: return f"{v:.3g}枚"          # 異常供給トークン（1e28 枚など）は指数表記
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
        names[w] = ("本体" if n_main == 1 else ("本体SOL" if not w.startswith("0x") else "本体EVM")) if r == "本体" else f"{r}{cnt[r]}"
    return names

# ------------------------------------------------------------ 分類
def bucket_of(chain, contract, ctx):
    c = L(contract or "")
    if c == "native": return "quasi"
    ch = ctx["chains"].get(chain, {})
    if c and c == L(ch.get("wnative") or ""): return "quasi"
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
    if k in ("受取", "受取(新規トークン)"): return "in"
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

def reconstruct_backwards(snap, events, target_ts, ctx):
    """記録開始前の時点を推定する: 最古のスナップショットから、その間のイベント（数量）を逆算して巻き戻す。
    価格は不明なので snap の価格（無ければイベント処理時の価格）を流用 → 値動きは含まれない（approx=True）"""
    import copy
    pos = copy.deepcopy(snap["pos"])
    evs = sorted([e for e in events if target_ts < e["ts"] <= snap["ts"]], key=lambda e: -e["ts"])
    for e in evs:
        f = flow_of(e, ctx)
        if f in ("internal", "noise"): continue
        k = f"{e['chain']}:{L(e['contract'] or '')}"
        if k not in pos:
            b = bucket_of(e["chain"], e["contract"], ctx)
            if b == "risk" and not e.get("price"): continue
            pos[k] = {"sym": e["token"], "amt": 0.0, "px": e.get("price"), "usd": 0.0, "b": b}
        pos[k]["amt"] += -e["amount"] if e["dir"] == "IN" else e["amount"]
    for k in list(pos):
        p = pos[k]
        if p["amt"] <= 1e-9: pos.pop(k); continue
        p["amt"] = round(p["amt"], 8); p["usd"] = p["amt"] * p["px"] if p.get("px") else 0.0
        if p["b"] == "risk" and p["usd"] < 100: pos.pop(k)
    tot = {"total": 0.0, "risk": 0.0, "quasi": 0.0, "cash": 0.0}
    for p in pos.values(): tot["total"] += p["usd"]; tot[p["b"]] += p["usd"]
    return {"ts": int(target_ts), "time": datetime.fromtimestamp(target_ts, timezone.utc).isoformat(timespec="seconds"), **{k: round(v, 2) for k, v in tot.items()},
            "by_wallet": {}, "pos": pos, "approx": True}

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

def drop_artifact_snapshots(snaps, min_usd=1000.0, tol=0.001):
    """データ元の索引障害でポジションが一時的に消えた時点を落とす。
       判定＝前後の時点に同じ枚数（誤差0.1%以内）で存在する $1,000 以上の銘柄が、その時点だけ消えている
       （売って同じ枚数を買い戻すことは実質ありえないので、消失は取得漏れと断定できる）"""
    if len(snaps) < 3: return snaps, []
    drop = []
    for i in range(1, len(snaps) - 1):
        a, b, c = snaps[i - 1], snaps[i], snaps[i + 1]
        if any(s.get("approx") for s in (a, b, c)): continue
        lost = 0.0
        for k, pa in a["pos"].items():
            pc = c["pos"].get(k)
            if not pc or (pa.get("usd") or 0) < min_usd: continue
            if (b["pos"].get(k, {}).get("amt") or 0) > 0: continue
            if abs(pc["amt"] - pa["amt"]) / max(pa["amt"], 1e-9) < tol: lost += pa["usd"]
        if lost >= min_usd: drop.append((b["ts"], lost))
    if not drop: return snaps, []
    bad = {ts for ts, _ in drop}
    return [s for s in snaps if s["ts"] not in bad], drop

def prune_snapshots(snaps, keep_hourly_days=7):
    """直近7日は全部、それより前は 00:00 UTC 直前の1本だけ残す"""
    if not snaps: return snaps
    now = snaps[-1]["ts"]; keep = []; seen_cut = set()
    for s in reversed(snaps):
        if now - s["ts"] <= keep_hourly_days * DAY or s.get("approx") or (s["ts"] - CUT_OFF) % DAY == 0: keep.append(s); continue   # 推定点・締め時刻ちょうどの点は常に残す
        cut = next_cut(s["ts"])                 # この時点が属する日の締め時刻
        if cut not in seen_cut: seen_cut.add(cut); keep.append(s)
    return sorted(keep, key=lambda s: s["ts"])

def save_snapshots(path, snaps):
    with open(path, "w") as f:
        for s in snaps: f.write(json.dumps(s, ensure_ascii=False) + "\n")

def cutoff_points(snaps, n=30):
    """1日の締め時刻（CUT_OFF、既定 24:00 JST）ごとの時点 + 現在。各時点はその締めに最も近いスナップショット（6時間前〜30分後）。
       記録開始前の推定点（approx）は締め時刻に関係なくそのまま時点として残す"""
    if not snaps: return []
    pts = [{"ts": s["ts"], "label": jst(s["ts"]).strftime("%-m/%-d %H:%M") + "(推定)", "date": cut_date(next_cut(s["ts"])).strftime("%Y-%m-%d"), "snap": s, "cut": True}
           for s in snaps if s.get("approx")]
    real = [s for s in snaps if not s.get("approx")]
    if real:
        first, last = real[0]["ts"], real[-1]["ts"]
        c = next_cut(first)
        if c - first > 6 * 3600:   # 記録開始から最初の締めまで 6 時間以上あく場合だけ「開始」時点を置く（初日のレポート用）
            pts.append({"ts": first, "label": jst(first).strftime("開始 %-m/%-d %H:%M"), "date": jst(first).strftime("%Y-%m-%d"), "snap": real[0]})
        while c <= last + 1800:
            cand = [s for s in real if c - 6 * 3600 <= s["ts"] <= c + 1800]
            if cand:
                s = min(cand, key=lambda s: abs(s["ts"] - c))
                pts.append({"ts": c, "label": cut_label(c), "date": cut_date(c).strftime("%Y-%m-%d"), "snap": s, "cut": True})
            c += DAY
    pts.sort(key=lambda p: p["ts"])
    if not pts or pts[-1]["snap"]["ts"] != snaps[-1]["ts"]:
        pts.append({"ts": snaps[-1]["ts"], "label": "現在", "date": "now", "snap": snaps[-1]})
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
            key = (w, e["chain"], L(e["contract"] or ""))
            a = agg.setdefault(key, {"wallet": w, "chain": e["chain"], "contract": L(e["contract"] or ""), "sym": e["token"], "amount": 0.0, "usd": 0.0,
                                     "counter_usd": 0.0, "n": 0, "other": defaultdict(float), "first": e["ts"], "last": e["ts"], "funding": 0.0, "note": ""})
            a["amount"] += e["amount"]; a["usd"] += e.get("usd") or 0.0; a["n"] += 1
            a["first"] = min(a["first"], e["ts"]); a["last"] = max(a["last"], e["ts"])
            if e.get("funding_usd"): a["funding"] += e["funding_usd"]; a["note"] = e.get("funding_note", "")
            for l in legs:
                if l["dir"] != e["dir"] and l["amount"] > 0:
                    a["other"][l["token"]] += l["amount"]; a["counter_usd"] += l.get("usd") or 0.0   # 相手足の時価＝支払額/受取額
    out = []
    for a in agg.values():
        a["other"] = dict(a["other"])
        a["value"] = a["counter_usd"] if a["counter_usd"] > 0 else a["usd"]     # 表示・集計に使う金額（支払/受取ベース、無ければ銘柄側時価）
        a["unit"] = a["value"] / a["amount"] if a["amount"] else None
        if a["value"] >= min_usd: out.append(a)
    return sorted(out, key=lambda a: -a["value"])

def other_leg_text(a):
    if a["other"]: return "、".join(f"{fmt_qty(v)} {k}" for k, v in sorted(a["other"].items(), key=lambda kv: -kv[1])[:2]) + (f" ≈ {fmt_usd(a['counter_usd'])}" if a.get("counter_usd") else "")
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
            movers.append({"sym": p["sym"], "chain": k.split(":")[0], "usd": d, "pct": (q["px"] / p["px"] - 1) * 100, "hold": q["usd"]})
    evs = [e for e in events if t0 < e["ts"] <= t1]
    buys = group_trades(evs, "buy", ctx); sells = group_trades(evs, "sell", ctx)
    outs = group_trades([e for e in evs if bucket_of(e["chain"], e["contract"], ctx) == "risk"], "out", ctx)
    ins = group_trades([e for e in evs if bucket_of(e["chain"], e["contract"], ctx) == "risk" and (e.get("usd") or 0) >= min_usd
                        and f"{e['chain']}:{L(e['contract'] or '')}" in s1["pos"]], "in", ctx)
    cash_outs = group_trades([e for e in evs if bucket_of(e["chain"], e["contract"], ctx) != "risk" and e["kind"] in ("外部へ送金", "バーン")], "out", ctx)
    internal = [e for e in evs if flow_of(e, ctx) == "internal" and e["dir"] == "OUT" and (e.get("usd") or 0) >= min_usd]
    B_val, S_val = sum(a["usd"] for a in buys), sum(a["usd"] for a in sells)          # 銘柄側の時価（リスク資産に入った/出た額）
    B, S = sum(a["value"] for a in buys), sum(a["value"] for a in sells)              # 支払った額 / 受け取った額
    O, I = sum(a["value"] for a in outs), sum(a["value"] for a in ins)
    exec_cost = (B_val - B) + (S - S_val)          # 約定コスト（スリッページ・価格インパクト）。負＝損
    risk0, risk1 = s0["risk"], s1["risk"]
    resid = risk1 - risk0 - price - B_val + S_val + O - I
    # 銘柄ごとの 枚数・単価・評価額 の前後比較（リスク資産）。評価額の増減を 値動き分 と 枚数増減分 に分ける
    positions = []
    for k in set(s0["pos"]) | set(s1["pos"]):
        a, b = s0["pos"].get(k), s1["pos"].get(k)
        if (a or b)["b"] != "risk": continue
        amt0, amt1 = (a["amt"] if a else 0.0), (b["amt"] if b else 0.0)
        px0, px1 = (a.get("px") if a else None), (b.get("px") if b else None)
        usd0, usd1 = (a["usd"] if a else 0.0), (b["usd"] if b else 0.0)
        pe = amt0 * (px1 - px0) if (px0 and px1) else 0.0
        positions.append({"key": k, "sym": (b or a)["sym"], "chain": k.split(":")[0], "contract": k.split(":")[1],
                          "amt0": amt0, "amt1": amt1, "px0": px0, "px1": px1, "usd0": usd0, "usd1": usd1,
                          "price_effect": pe, "qty_effect": usd1 - usd0 - pe, "status": "新規" if not a else ("全売却" if not b or amt1 <= 0 else "")})
    positions.sort(key=lambda x: -max(x["usd0"], x["usd1"]))
    return {"t0": t0, "t1": t1, "label0": p0["label"], "label1": p1["label"], "approx": bool(s0.get("approx") or s1.get("approx")),
            "total0": s0["total"], "total1": s1["total"], "risk0": risk0, "risk1": risk1,
            "quasi0": s0["quasi"], "quasi1": s1["quasi"], "cash0": s0["cash"], "cash1": s1["cash"],
            "price": price, "buys": B, "sells": S, "out": O, "in": I, "resid": resid, "exec_cost": exec_cost, "buys_val": B_val, "sells_val": S_val,
            "realized": S + O, "realized_pct": ((S + O) / risk0 * 100) if risk0 else None,
            "total_pct": ((s1["total"] / s0["total"] - 1) * 100) if s0["total"] else None,
            "risk_pct": ((risk1 / risk0 - 1) * 100) if risk0 else None,
            "min_usd": min_usd, "movers": sorted(movers, key=lambda m: -abs(m["usd"])), "buy_list": buys, "sell_list": sells,
            "out_list": outs, "in_list": ins, "cash_out_list": cash_outs, "internal": internal, "n_events": len(evs), "positions": positions}

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
CHAIN_SHORT = {"robinhood": "Robinhood", "bsc": "BSC", "solana": "Solana", "ethereum": "Ethereum", "base": "Base", "arbitrum": "Arbitrum"}
def chain_short(chain): return CHAIN_SHORT.get(chain, chain)
def symc(sym, chain): return f"{sym}（{chain_short(chain)}）"   # 銘柄名は常にチェーン付きで表示

def share_text(value, ctx, big_pct=10.0):
    """総資産に対する比率。$1.3M の買いが総資産の 13% なら「かなり大きい」と分かる（藤沼さん要望）。10% 以上は「大口」を付ける"""
    tot = ctx.get("total_usd")
    if not tot or not value: return ""
    pct = value / tot * 100
    return f"（総資産の {pct:.1f}%{'・大口' if pct >= big_pct else ''}）"

def buy_alert_lines(buys, names, ctx):
    lines = []
    for a in buys:
        who = names.get(a["wallet"], a["wallet"][:6]); t = jst(a["last"]).strftime("%m-%d %H:%M")
        times = (f"、{a['n']}回" if a["n"] > 1 else "") + ("（分割買いの累計）" if a.get("accum") else "")
        unit = f"、平均 ${a['unit']:.4g}/枚" if a.get("unit") else ""
        lines.append(f"🟢 買い  {who}  {symc(a['sym'], a['chain'])} {fmt_qty(a['amount'])} ≈ {fmt_usd(a['value'])}{share_text(a['value'], ctx)}\n"
                     f"   支払 {other_leg_text(a)}{times}{unit}  {t} JST")
        q = ctx.get("quote")
        if q:
            px, liq = q(a["chain"], a["contract"])
            if px: lines.append(f"   いま ${px:.4g}/枚" + (f"（取得比 {(px / a['unit'] - 1) * 100:+.0f}%）" if a.get("unit") else "") + (f"　流動性 {fmt_usd(liq)}" if liq else ""))
        link = dex_link(a["chain"], a["contract"], ctx)
        if link: lines.append(f"   {link}")
    return lines

def sell_alert_lines(sells, outs, names, ctx):
    """目立つ売り（$100K 以上）の即時通知。買いと同じ体裁。
       外部流出（クラスター外のウォレット・取引所などへ送金）は以後追えず売却リスクが高いので、安全側に倒して売り扱い（藤沼さん判断 2026-09-08）"""
    lines = []
    for a in sells:
        who = names.get(a["wallet"], a["wallet"][:6]); t = jst(a["last"]).strftime("%m-%d %H:%M")
        times = f"、{a['n']}回" if a["n"] > 1 else ""
        lines.append(f"🔻 売り  {who}  {symc(a['sym'], a['chain'])} {fmt_qty(a['amount'])} ≈ {fmt_usd(a['value'])}{share_text(a['value'], ctx)}\n"
                     f"   受取 {other_leg_text(a)}{times}  {t} JST")
        link = dex_link(a["chain"], a["contract"], ctx)
        if link: lines.append(f"   {link}")
    for a in outs:
        who = names.get(a["wallet"], a["wallet"][:6]); t = jst(a["last"]).strftime("%m-%d %H:%M")
        lines.append(f"🔻 売り扱い（外部流出）  {who}  {symc(a['sym'], a['chain'])} {fmt_qty(a['amount'])} ≈ {fmt_usd(a['value'])}{share_text(a['value'], ctx)}\n"
                     f"   クラスター外へ送金（取引所等の可能性、以後追跡不能）  {t} JST")
        link = dex_link(a["chain"], a["contract"], ctx)
        if link: lines.append(f"   {link}")
    return lines

def daily_report_text(br, names, ctx, day_label, big_usd=100000.0, pages=""):
    """1日1通の日次レポート: 総資産の推移 と $100K 以上の大きな動き（買い / 売り / 値動き）だけ。細かい内訳は載せない"""
    L = [f"📊 日次レポート {day_label}（{br['label0']} → {br['label1']}）",
         f"総資産 {fmt_usd(br['total1'])}（{fmt_usd(br['total1'] - br['total0'], True)} / {fmt_pct(br['total_pct'])}）　リスク {fmt_usd(br['risk1'])}　準現金 {fmt_usd(br['quasi1'])}　現金 {fmt_usd(br['cash1'])}",
         f"リスク資産 {fmt_usd(br['risk0'])} → {fmt_usd(br['risk1'])}：値動き {fmt_usd(br['price'], True)} / 利確 {fmt_usd(-br['realized'], True)} / 買い {fmt_usd(br['buys'], True)}"]
    buys = [a for a in br["buy_list"] if a["value"] >= big_usd]
    sells = [a for a in br["sell_list"] if a["value"] >= big_usd]
    outs = [a for a in br["out_list"] + br["cash_out_list"] if a["value"] >= big_usd]      # 外部流出は売り扱い（以後追跡不能）
    ups = [m for m in br["movers"] if m["usd"] >= big_usd]; downs = [m for m in br["movers"] if m["usd"] <= -big_usd]
    sctx = {**ctx, "total_usd": br["total0"]}     # 比率の分母は期首の総資産
    def item(a, tag=""): return f"{names.get(a['wallet'], '?')} {symc(a['sym'], a['chain'])} {fmt_qty(a['amount'])} ≈ {fmt_usd(a['value'])}{share_text(a['value'], sctx)}{tag}"
    if buys: L.append("🟢 大きな買い: " + "、".join(item(a) for a in buys[:5]))
    if sells or outs: L.append("🔻 大きな売り: " + "、".join([item(a) for a in sells[:5]] + [item(a, "［外部流出→売り扱い］") for a in outs[:5]]))
    if downs: L.append("📉 大きな値下がり: " + "、".join(f"{symc(m['sym'], m.get('chain', ''))} {m['pct']:+.1f}%（{fmt_usd(m['usd'], True)}）" for m in downs[:5]))
    if ups: L.append("📈 大きな値上がり: " + "、".join(f"{symc(m['sym'], m.get('chain', ''))} {m['pct']:+.1f}%（{fmt_usd(m['usd'], True)}）" for m in ups[:5]))
    if not (buys or sells or outs or ups or downs): L.append(f"{fmt_usd(big_usd)} 以上の大きな動きはなし")
    if pages and "<" not in pages: L.append(f"詳細: {pages}")
    return "\n".join(L)

def digest_text(br, names, ctx, title, pages="", max_items=4):
    L = [title, f"総資産 {fmt_usd(br['total1'])}（{fmt_usd(br['total1'] - br['total0'], True)} / {fmt_pct(br['total_pct'])}）"
              f"　リスク {fmt_usd(br['risk1'])}　準現金 {fmt_usd(br['quasi1'])}　現金 {fmt_usd(br['cash1'])}"]
    L.append(f"リスク資産 {fmt_usd(br['risk0'])} → {fmt_usd(br['risk1'])}：値動き {fmt_usd(br['price'], True)} / 利確 {fmt_usd(-br['realized'], True)}"
             f"（{br['realized_pct']:.1f}%）/ 買い {fmt_usd(br['buys'], True)}" + (f" / 流入 {fmt_usd(br['in'], True)}" if br["in"] else "")
             + (f" / 約定コスト {fmt_usd(br['exec_cost'], True)}" if abs(br["exec_cost"]) >= 0.005 * max(br["risk0"], 1) else "")
             + (f" / 誤差 {fmt_usd(br['resid'], True)}" if abs(br["resid"]) >= 0.02 * max(br["risk0"], 1) else ""))
    m = br.get("min_usd", 0); sells = [a for a in br["sell_list"] if a["value"] >= m]; outs = [a for a in br["out_list"] + br["cash_out_list"] if a["value"] >= m]; buys = [a for a in br["buy_list"] if a["value"] >= m]
    if sells:
        L.append("🔻 利確: " + "、".join(f"{names.get(a['wallet'], '?')} {symc(a['sym'], a['chain'])} {fmt_qty(a['amount'])} → {other_leg_text(a)}" for a in sells[:max_items]))
    if outs:
        L.append("📤 外部流出: " + "、".join(f"{names.get(a['wallet'], '?')} {symc(a['sym'], a['chain'])} {fmt_qty(a['amount'])} ({fmt_usd(a['value'])})" for a in outs[:max_items]))
    if buys:
        L.append("🟢 買い: " + "、".join(f"{names.get(a['wallet'], '?')} {symc(a['sym'], a['chain'])} {fmt_qty(a['amount'])} ({fmt_usd(a['value'])})" for a in buys[:max_items]))
    mv = [m for m in br["movers"] if abs(m["usd"]) >= 0.01 * max(br["risk0"], 1)][:max_items]
    if mv: L.append("📈 値動き: " + "、".join(f"{symc(m['sym'], m.get('chain', ''))} {m['pct']:+.1f}% ({fmt_usd(m['usd'], True)})" for m in mv))
    if br["internal"]:
        L.append("↔ 内部移動: " + "、".join(f"{names.get(e['wallet'], '?')}→{names.get(e['cp'], '外')} {e['token']} {fmt_qty(e['amount'])}" for e in br["internal"][:3]))
    if pages and "<" not in pages: L.append(f"詳細: {pages}")
    return "\n".join(L)


# ------------------------------------------------------------ 新規購入銘柄の成績（別出し）
def new_positions(events, snaps, ctx, days=14, min_cost=5000.0):
    """直近 days 日に買ったリスク銘柄ごとの成績。平均取得単価（支払額÷枚数）と現在価格の比較、売却済み分、時点ごとの価格推移"""
    if not snaps: return []
    now = snaps[-1]; since = now["ts"] - days * DAY
    evs = [e for e in events if e["ts"] > since]
    buys = group_trades(evs, "buy", ctx); sells = group_trades(evs, "sell", ctx)
    agg = {}
    for a in buys:
        k = f"{a['chain']}:{a['contract']}"
        g = agg.setdefault(k, {"key": k, "chain": a["chain"], "contract": a["contract"], "sym": a["sym"], "qty": 0.0, "cost": 0.0, "n": 0,
                               "first": a["first"], "last": a["last"], "wallets": set(), "sold_qty": 0.0, "proceeds": 0.0})
        g["qty"] += a["amount"]; g["cost"] += a["value"]; g["n"] += a["n"]; g["wallets"].add(a["wallet"])
        g["first"] = min(g["first"], a["first"]); g["last"] = max(g["last"], a["last"])
    for a in sells:
        k = f"{a['chain']}:{a['contract']}"
        if k in agg and a["last"] >= agg[k]["first"]: agg[k]["sold_qty"] += a["amount"]; agg[k]["proceeds"] += a["value"]
    pts = cutoff_points(snaps, n=60); quote = ctx.get("quote")
    out = []
    for k, g in agg.items():
        if g["cost"] < min_cost or g["qty"] <= 0: continue
        g["avg"] = g["cost"] / g["qty"]
        cur = now["pos"].get(k); px = cur["px"] if cur and cur.get("px") else None
        liq = None
        if quote:
            qpx, qliq = quote(g["chain"], g["contract"]); px = px or qpx; liq = qliq
        g["px"] = px; g["liq"] = liq
        g["held"] = cur["amt"] if cur else 0.0; g["value"] = g["held"] * px if px else None
        g["pnl_pct"] = (px / g["avg"] - 1) * 100 if px and g["avg"] else None
        g["sold_pct"] = min(g["sold_qty"] / g["qty"] * 100, 100) if g["qty"] else 0.0
        # 買ってからの時点ごとの価格
        path = [(p["label"], p["snap"]["pos"].get(k, {}).get("px")) for p in pts if p["ts"] >= g["first"] - 1800]
        g["path"] = [(l, v) for l, v in path if v]
        prev = next((v for l, v in reversed(g["path"][:-1]) if v), None) if len(g["path"]) >= 2 else None
        g["vs_prev_pct"] = (px / prev - 1) * 100 if px and prev else None
        g["wallets"] = sorted(g["wallets"]); out.append(g)
    return sorted(out, key=lambda g: -g["cost"])

def new_positions_text(rows, names, max_items=8):
    if not rows: return ""
    L = ["🆕 新規銘柄の成績（直近14日に買った銘柄、平均取得比）:"]
    for g in rows[:max_items]:
        who = "/".join(names.get(w, "?") for w in g["wallets"])
        pnl = fmt_pct(g["pnl_pct"]) if g["pnl_pct"] is not None else "価格なし"
        sold = f"、{g['sold_pct']:.0f}%売却済" if g["sold_pct"] >= 1 else ""
        L.append(f"  {symc(g['sym'], g['chain'])} {pnl}（{who} 支払 {fmt_usd(g['cost'])}、平均 ${g['avg']:.4g} → いま ${g['px']:.4g}{sold}）" if g["px"] else f"  {symc(g['sym'], g['chain'])} 価格取得不可（支払 {fmt_usd(g['cost'])}）")
    return "\n".join(L)


# ------------------------------------------------------------ 銘柄別の日次推移（前日比 と 開始日比 の両方）
def token_matrix(snaps, ctx, top_n=20, extra_keys=(), n_points=45):
    """時点ごとの 枚数/単価/評価額 を銘柄別に並べる。対象＝現在の評価額上位 top_n のリスク銘柄 ＋ extra_keys（新規購入銘柄など）"""
    pts = cutoff_points(snaps, n=n_points)
    if not pts: return {"points": [], "rows": [], "totals": []}
    now = pts[-1]["snap"]
    top = sorted([k for k, p in now["pos"].items() if p["b"] == "risk"], key=lambda k: -now["pos"][k]["usd"])[:top_n]
    keys = list(dict.fromkeys(top + [k for k in extra_keys if k not in top]))
    rows = []
    for k in keys:
        cells = []
        for p in pts:
            q = p["snap"]["pos"].get(k)
            cells.append({"amt": q["amt"], "px": q.get("px"), "usd": q["usd"]} if q else None)
        sym = next((c["sym"] for c in [p["snap"]["pos"].get(k) for p in pts] if c), k)
        rows.append({"key": k, "sym": sym, "chain": k.split(":")[0], "contract": k.split(":")[1], "cells": cells, "now_usd": (now["pos"].get(k) or {}).get("usd", 0.0)})
    rows.sort(key=lambda r: -r["now_usd"])
    totals = [{"total": p["snap"]["total"], "risk": p["snap"]["risk"]} for p in pts]
    return {"points": [{"label": p["label"], "ts": p["ts"], "approx": bool(p["snap"].get("approx"))} for p in pts], "rows": rows, "totals": totals}
