"""docs/index.html を生成する。tracker.py から呼ばれる。"""
from collections import defaultdict
from datetime import datetime, timezone
from html import escape as esc

CSS = """
:root{--bg:#E9EDF2;--paper:#fff;--ink:#1C2430;--ink2:#5B6675;--line:#D3DAE3;--sell:#B8412A;--buy:#1E7A5A;--move:#2F55C7;--hold:#7A6A2E;--sellbg:#FBEDE9;--buybg:#E6F3EE;--movebg:#E8EDFB;--warnbg:#FFF4D6;--warn:#7A5A00}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:"Hiragino Sans","Noto Sans JP","Yu Gothic",system-ui,sans-serif;font-size:14px;line-height:1.55;font-variant-numeric:tabular-nums}
header{padding:20px 28px 12px;display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:10px}header h1{margin:0;font-size:21px;font-weight:600}header h1 small{font-weight:400;color:var(--ink2);font-size:13px;margin-left:10px}
.chip{display:inline-block;padding:3px 10px;border:1px solid var(--line);border-radius:999px;background:var(--paper);font-size:12px;margin-left:4px}
main{display:grid;grid-template-columns:310px 1fr;gap:18px;padding:0 28px 40px}@media(max-width:900px){main{grid-template-columns:1fr}}
.panel{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:18px}.panel h2{margin:0 0 10px;font-size:13px;font-weight:600;color:var(--ink2)}
.wallet{padding:10px 0;border-top:1px solid var(--line)}.wallet:first-of-type{border-top:0;padding-top:0}.addr{font-size:13px;color:var(--ink2)}.total{font-size:23px;font-weight:600;margin:2px 0 8px}
.hold{display:grid;grid-template-columns:auto 1fr auto;gap:8px 10px;font-size:13px;align-items:center}.bar{height:6px;background:#EDF0F4;border-radius:3px;overflow:hidden}.bar i{display:block;height:100%;background:var(--hold);opacity:.75}.v{text-align:right}
.tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;margin-left:5px;background:#F1F3F6;color:var(--ink2)}
table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;font-weight:500;color:var(--ink2);padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}td{padding:6px 8px;border-bottom:1px solid #EEF1F5;white-space:nowrap}td.r,th.r{text-align:right}
tr.sell td:first-child{box-shadow:inset 3px 0 0 var(--sell)}tr.move td:first-child{box-shadow:inset 3px 0 0 var(--move)}tr.buy td:first-child{box-shadow:inset 3px 0 0 var(--buy)}tr.warn td:first-child{box-shadow:inset 3px 0 0 #D9A400}
.k{display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px}.k.sell{background:var(--sellbg);color:var(--sell)}.k.move{background:var(--movebg);color:var(--move)}.k.buy{background:var(--buybg);color:var(--buy)}.k.warn{background:var(--warnbg);color:var(--warn)}.k.recv{background:#F1F3F6;color:var(--ink2)}
.ba{display:inline-flex;align-items:center;gap:6px}.ba .bar{width:80px;height:8px;position:relative}.ba .bar i{position:absolute;left:0;top:0;height:100%;background:#B9C3D1;opacity:1}.ba .bar b{position:absolute;right:0;top:0;height:100%;background:var(--sell);opacity:.7}
.sub{color:var(--ink2);font-size:12px}.minor li{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px solid #EEF1F5}.minor ul{list-style:none;margin:0;padding:0}
details summary{cursor:pointer;color:var(--ink2);font-size:13px;padding:6px 0}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}@media(max-width:1100px){.grid2{grid-template-columns:1fr}}
"""

KIND_CLASS = {"売却": "sell", "内部移動": "move", "購入": "buy", "新ウォレット": "buy", "受取(原資未確認)": "warn", "サービスへ送金": "move", "なりすまし": "recv", "バーン": "recv"}

def kcls(kind):
    for k, c in KIND_CLASS.items():
        if kind.startswith(k): return c
    return "recv"

def usd(v): return "—" if v in (None, 0) else f"${v:,.0f}"
def num(v): return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.4g}"

def render(cfg, chains, wallets, events, holdings, batch_list, threshold, label, out_path, holdings_updated=None):
    now = datetime.now(timezone.utc)
    # --- 保有: ウォレット別に全チェーン合算、しきい値以上を表示 ---
    by_wallet = defaultdict(list)
    for key, h in holdings.items():
        ch, w = key.split(":")
        for s, v in h.items():
            by_wallet[w].append({"sym": v.get("symbol") or s, "chain": ch, **v})
    main_tot = 0.0
    left = []
    order = sorted(wallets, key=lambda w: ({"本体": 0, "子": 1, "孫": 2}.get(wallets[w]["role"], 3), w))
    for w in order:
        items = sorted(by_wallet.get(w, []), key=lambda x: -(x.get("usd") or 0))
        tot = sum(x.get("usd") or 0 for x in items)
        if wallets[w]["role"] == "本体": main_tot += tot
        big = [x for x in items if (x.get("usd") or 0) >= threshold]
        small = [x for x in items if x not in big and x["amount"] > 0]
        mx = max([x.get("usd") or 0 for x in big] or [1])
        rows = "".join(
            f"<span>{esc(x['sym'])}<span class='tag'>{chains[x['chain']]['name']}</span></span>"
            f"<span class='bar'><i style='width:{(x.get('usd') or 0) / mx * 100:.0f}%'></i></span>"
            f"<span class='v'>{usd(x.get('usd'))}<div class='sub'>{num(x['amount'])}枚 {('@$%.4g' % x['price']) if x.get('price') else '価格未取得'}</div></span>"
            for x in big)
        small_html = ""
        if small:
            small_html = f"<details><summary>小型・価格未取得 {len(small)}銘柄</summary><div class='sub'>" + "、".join(
                f"{esc(x['sym'])} {num(x['amount'])}枚{(' ' + usd(x.get('usd'))) if x.get('usd') else ''}" for x in small[:40]) + "</div></details>"
        chain_tags = "".join(f"<span class='tag'>{chains[c]['name']}</span>" for c in wallets[w]["chains"])
        parent = f"<div class='sub'>親: {esc(label(wallets[w]['parent']))}　初出 {wallets[w]['first_seen'][:16] if wallets[w]['first_seen'] else ''}</div>" if wallets[w]["parent"] else ""
        left.append(f"<div class='wallet'><div class='addr'>{esc(wallets[w]['role'])} <span title='{w}'>{w[:6]}…{w[-4:]}</span>{chain_tags}</div>"
                    f"<div class='total'>{usd(tot)}</div>{parent}<div class='hold'>{rows}</div>{small_html}</div>")

    # --- 売却バッチ ---
    def main_holding(contract):
        return sum(x["amount"] for w in wallets if wallets[w]["role"] == "本体" for x in by_wallet.get(w, []) if x.get("contract") == contract)
    brows = []
    for b in sorted(batch_list, key=lambda b: b["start"], reverse=True)[:60]:
        if b["usd"] < threshold: continue
        base = main_holding(b.get("contract") or b["token"]); pct = f"{b['amount'] / (base + b['amount']) * 100:.1f}%" if base else "—"
        brows.append(f"<tr class='sell'><td>{b['start'][5:16]} – {b['end'][11:16]}</td><td>{esc(label(b['wallet']))}</td><td>{esc(b['token'])}<span class='tag'>{chains[b['chain']]['name']}</span></td>"
                     f"<td class='r'>{b['n']}</td><td class='r'>{num(b['amount'])}</td><td class='r'>{usd(b['usd'])}</td><td class='r'>{pct}</td></tr>")
    # --- 主要イベント ---
    erows = []
    evs = sorted(events, key=lambda e: e["ts"], reverse=True)
    major = [e for e in evs if ((e.get("usd") or 0) >= threshold or e["kind"].startswith("新ウォレット")) and not e["kind"].startswith("売却")]
    for e in major[:200]:
        extra = f"<div class='sub'>原資 {usd(e['funding_usd'])}</div>" if e.get("funding_usd") else ""
        erows.append(f"<tr class='{kcls(e['kind'])}'><td>{e['time'][5:16]}<span class='tag'>{chains[e['chain']]['name']}</span></td><td>{esc(label(e['wallet']))}</td>"
                     f"<td><span class='k {kcls(e['kind'])}'>{esc(e['kind'])}</span>{extra}</td><td>{e['dir']}</td><td>{esc(e['token'])}</td>"
                     f"<td class='r'>{num(e['amount'])}</td><td class='r'>{usd(e.get('usd'))}</td><td>{esc(e.get('cp_label') or '')}</td></tr>")
    # --- 小口 ---
    minor = [e for e in evs if e not in major and e["kind"] not in ("ダスト", "なりすまし(偽送金)") and not e["kind"].startswith("売却")][:300]
    g = defaultdict(lambda: {"n": 0, "amt": 0.0, "usd": 0.0, "last": ""})
    for e in minor:
        k = (e["wallet"], e["kind"], e["token"], e["dir"], e["chain"]); g[k]["n"] += 1; g[k]["amt"] += e["amount"]; g[k]["usd"] += e.get("usd") or 0; g[k]["last"] = max(g[k]["last"], e["time"])
    mrows = "".join(f"<li><span><span class='k {kcls(k[1])}'>{esc(k[1])}</span> {esc(k[2])} {num(v['amt'])}枚 ×{v['n']}{(' ' + usd(v['usd'])) if v['usd'] else ''}</span><span class='sub'>{v['last'][5:16]} {esc(label(k[0]))} {chains[k[4]]['name']}</span></li>"
                    for k, v in sorted(g.items(), key=lambda kv: kv[1]["last"], reverse=True)[:60])
    dust_n = sum(1 for e in evs if e["kind"] in ("ダスト", "なりすまし(偽送金)"))
    # --- ラベル ---
    lab_rows = "".join(f"<span title='{a}'>{a[:6]}…{a[-4:]}</span><span class='sub' style='white-space:normal'>{esc(l)}</span>" for a, l in cfg.get("labels", {}).items())
    chain_chips = "".join(f"<span class='chip'>{chains[c]['name']}</span>" for c in cfg.get("chains", []))
    doc = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>クジラ資金フロー台帳</title><style>{CSS}</style></head><body>
<header><h1>クジラ資金フロー台帳 <small>本体 {len([w for w in wallets if wallets[w]['role']=='本体'])}件 ＋ 自動検出 {len([w for w in wallets if wallets[w]['role']!='本体'])}件</small></h1>
<div class="sub">しきい値 ${threshold:,.0f} {chain_chips} 更新 {now:%m-%d %H:%M} UTC</div></header>
<main><aside>
<section class="panel"><h2>現在の保有（全チェーン合算・${threshold:,.0f}以上を表示）{(" ・残高更新 " + holdings_updated[5:16].replace("T"," ") + " UTC") if holdings_updated else ""}</h2><div class="sub" style="margin-bottom:8px">本体合計 <b style="color:var(--ink);font-size:18px">{usd(main_tot)}</b></div>{''.join(left)}</section>
<section class="panel"><h2>相手先の名寄せ</h2><div class="hold" style="grid-template-columns:auto 1fr">{lab_rows}<span>0x00AA…</span><span class="sub">共通接頭辞の複数アドレス＝同一サービスとして扱う</span></div></section>
</aside><div>
<section class="panel"><h2>売却バッチ（同一ウォレット・同一銘柄で30分以内の連続スワップを合算）</h2>
<table><thead><tr><th>開始 – 終了 (UTC)</th><th>ウォレット</th><th>銘柄</th><th class="r">回数</th><th class="r">数量</th><th class="r">USD</th><th class="r">本体保有比</th></tr></thead><tbody>{''.join(brows) or '<tr><td colspan=7 class=sub>まだ売却バッチはありません</td></tr>'}</tbody></table></section>
<section class="panel"><h2>主要イベント（${threshold:,.0f}以上・新ウォレット。個々の売却スワップは上の売却バッチに集約）</h2>
<table><thead><tr><th>時刻</th><th>ウォレット</th><th>種別</th><th>方向</th><th>銘柄</th><th class="r">数量</th><th class="r">USD</th><th>相手</th></tr></thead><tbody>{''.join(erows) or '<tr><td colspan=8 class=sub>まだイベントはありません</td></tr>'}</tbody></table></section>
<div class="grid2"><section class="panel minor"><h2>小口（${threshold:,.0f}未満）</h2><ul>{mrows}</ul><div class="sub" style="margin-top:8px">ダスト・エアドロップ {dust_n} 件は非表示</div></section>
<section class="panel"><h2>判定ルール</h2><div class="sub">
<p>売却＝トークンOUT＋同一txでネイティブ/ステーブルIN。代金が内部txで戻るものも捕捉。</p>
<p>購入(クロスチェーン)＝サービスから受取、かつ7日以内に監視ウォレットのどれかが同じ相手へ原資を送金。原資が見つからなければ「受取(原資未確認)」（黄）。</p>
<p>新ウォレット＝未知EOAへ$20未満のネイティブ送金（ガス種銭）、または未知EOAへのトークン送金。検出した瞬間に監視へ追加し、親子関係を記録。</p>
<p>本体保有比＝売却数量 ÷（売却後の本体残高＋売却数量）。</p></div></section></div>
</div></main></body></html>"""
    out_path.write_text(doc, encoding="utf-8")
