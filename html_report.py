"""docs/index.html を生成する。tracker.py から呼ばれる。

上段: 資産レポート（総資産・リスク資産の推移、日ごとの増減分解、買い/利確の大きな動き）
下段: 従来の詳細台帳（折りたたみ）
"""
import json
from collections import defaultdict
from datetime import datetime, timezone
from html import escape as esc

import portfolio as P

CSS = """
:root{--bg:#f9f9f7;--paper:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--mute:#898781;--line:#e1e0d9;--axis:#c3c2b7;--ring:rgba(11,11,11,.10);
--s1:#2a78d6;--s2:#eb6834;--good:#006300;--bad:#d03b3b;--sell:#B8412A;--buy:#1E7A5A;--move:#2F55C7;--sellbg:#FBEDE9;--buybg:#E6F3EE;--movebg:#E8EDFB;--warnbg:#FFF4D6;--warn:#7A5A00;--hold:#7A6A2E}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI","Hiragino Sans","Noto Sans JP",sans-serif;font-size:14px;line-height:1.55}
header{padding:20px 28px 8px;display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:10px}header h1{margin:0;font-size:21px;font-weight:600}header h1 small{font-weight:400;color:var(--ink2);font-size:13px;margin-left:10px}
main{max-width:1080px;margin:0 auto;padding:0 28px 40px}
.panel{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:18px}.panel h2{margin:0 0 10px;font-size:13px;font-weight:600;color:var(--ink2)}
.hero{display:flex;flex-wrap:wrap;gap:18px 36px;align-items:flex-end}.hero .big{font-size:48px;font-weight:600;line-height:1.05}.hero .lbl{font-size:12px;color:var(--ink2)}.hero .delta{font-size:14px;margin-top:4px}
.tiles{display:flex;flex-wrap:wrap;gap:12px 28px}.tile .v{font-size:20px;font-weight:600}.tile .lbl{font-size:12px;color:var(--ink2)}
.up{color:var(--good)}.down{color:var(--bad)}.sub{color:var(--ink2);font-size:12px}.mute{color:var(--mute)}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}.tab{border:1px solid var(--line);background:var(--paper);border-radius:999px;padding:4px 12px;font-size:13px;cursor:pointer;color:var(--ink2)}.tab[aria-selected=true]{background:var(--ink);color:#fff;border-color:var(--ink)}
.bridge{display:grid;grid-template-columns:auto 1fr auto;gap:6px 14px;align-items:center;max-width:640px;margin:8px 0 14px}.bridge .k{color:var(--ink2)}.bridge .v{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}.bridge .bar{height:8px;background:#EDF0F4;border-radius:3px;position:relative;overflow:hidden}.bridge .bar i{position:absolute;top:0;height:100%;display:block}
.bridge .tot{border-top:1px solid var(--line);padding-top:6px}
table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;font-weight:500;color:var(--ink2);padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}td{padding:6px 8px;border-bottom:1px solid #EEF1F5;white-space:nowrap;font-variant-numeric:tabular-nums}td.r,th.r{text-align:right}td.w{white-space:normal}
.k{display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px}.k.sell{background:var(--sellbg);color:var(--sell)}.k.move{background:var(--movebg);color:var(--move)}.k.buy{background:var(--buybg);color:var(--buy)}.k.warn{background:var(--warnbg);color:var(--warn)}.k.recv{background:#F1F3F6;color:var(--ink2)}
.tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;margin-left:5px;background:#F1F3F6;color:var(--ink2)}
h3{font-size:13px;font-weight:600;margin:14px 0 6px}details summary{cursor:pointer;color:var(--ink2);font-size:13px;padding:6px 0}
.chart{position:relative}.chart svg{width:100%;height:auto;display:block}.legend{display:flex;gap:18px;font-size:12px;color:var(--ink2);margin:6px 0 0}.legend i{display:inline-block;width:18px;height:0;border-top:2px solid;vertical-align:middle;margin-right:6px}
.tip{position:absolute;pointer-events:none;background:var(--paper);border:1px solid var(--line);border-radius:6px;padding:6px 10px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.08);display:none;min-width:150px}.tip b{font-size:14px}.tip .row{display:flex;justify-content:space-between;gap:12px}.tip .key{display:inline-block;width:14px;border-top:2px solid;vertical-align:middle;margin-right:6px}
.old{margin-top:8px}.old .grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}@media(max-width:900px){.old .grid2{grid-template-columns:1fr}.hero .big{font-size:36px}}
tr.sell td:first-child{box-shadow:inset 3px 0 0 var(--sell)}tr.move td:first-child{box-shadow:inset 3px 0 0 var(--move)}tr.buy td:first-child{box-shadow:inset 3px 0 0 var(--buy)}tr.warn td:first-child{box-shadow:inset 3px 0 0 #D9A400}
.minor li{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px solid #EEF1F5}.minor ul{list-style:none;margin:0;padding:0}
a{color:var(--s1)}
"""

KIND_CLASS = {"売却": "sell", "内部移動": "move", "購入": "buy", "新ウォレット": "buy", "受取(原資未確認)": "warn", "受取(新規トークン)": "warn", "サービスへ送金": "move", "なりすまし": "recv", "バーン": "recv"}
def kcls(kind):
    for k, c in KIND_CLASS.items():
        if kind.startswith(k): return c
    return "recv"
def usd(v): return "—" if v in (None, 0) else f"${v:,.0f}"
def num(v): return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.4g}"
def cls_delta(v): return "up" if v > 0 else ("down" if v < 0 else "")
def jst(ts): return P.jst(ts).strftime("%m-%d %H:%M")

# ------------------------------------------------------------ 折れ線（総資産・リスク資産）
def nice_ticks(vmax, n=4):
    if vmax <= 0: return [0]
    raw = vmax / n; mag = 10 ** int(f"{raw:e}".split("e")[1]); step = mag
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= raw: step = m * mag; break
    return [i * step for i in range(int(vmax // step) + 2)]

def chart_svg(points):
    """2系列の折れ線。W=720,H=260。データは JS 用にも埋め込む"""
    if len(points) < 2: return "<div class='sub'>推移グラフは時点が2つ以上たまってから表示します（次の締め時刻以降）。</div>"
    W, H, L, R, T, B = 720, 260, 64, 120, 16, 34
    xs = [p["ts"] for p in points]; tot = [p["snap"]["total"] for p in points]; rsk = [p["snap"]["risk"] for p in points]
    vmax = max(tot + rsk) * 1.08; ticks = nice_ticks(vmax); ymax = ticks[-1]
    def X(i): return L + (W - L - R) * (i / (len(points) - 1))
    def Y(v): return T + (H - T - B) * (1 - v / ymax)
    grid = "".join(f"<line x1='{L}' x2='{W - R}' y1='{Y(t):.1f}' y2='{Y(t):.1f}' stroke='var(--line)' stroke-width='1'/>"
                   f"<text x='{L - 8}' y='{Y(t) + 4:.1f}' text-anchor='end' font-size='11' fill='var(--mute)'>{P.fmt_usd(t)}</text>" for t in ticks)
    xl = "".join(f"<text x='{X(i):.1f}' y='{H - 12}' text-anchor='middle' font-size='11' fill='var(--mute)'>{esc(p['label'].replace(' 9:00', ''))}</text>"
                 for i, p in enumerate(points) if len(points) <= 10 or i % max(1, len(points) // 8) == 0 or i == len(points) - 1)
    def path(vals): return " ".join(f"{'M' if i == 0 else 'L'}{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
    # 端ラベル（衝突したら上下に逃がして引き出し線）
    y1, y2 = Y(tot[-1]), Y(rsk[-1]); lab1, lab2 = y1, y2
    if abs(y1 - y2) < 16: mid = (y1 + y2) / 2; lab1, lab2 = mid - 9, mid + 9
    xe = X(len(points) - 1)
    def endlabel(y, ly, text, color):
        lead = f"<line x1='{xe + 6}' x2='{xe + 14}' y1='{y:.1f}' y2='{ly:.1f}' stroke='{color}' stroke-width='1'/>" if abs(y - ly) > 2 else ""
        return lead + f"<text x='{xe + 16}' y='{ly + 4:.1f}' font-size='12' fill='var(--ink)'>{esc(text)}</text>"
    svg = f"""<svg viewBox='0 0 {W} {H}' role='img' aria-label='総資産とリスク資産の推移'>
<line x1='{L}' x2='{W - R}' y1='{Y(0):.1f}' y2='{Y(0):.1f}' stroke='var(--axis)' stroke-width='1'/>{grid}{xl}
<path d='{path(tot)}' fill='none' stroke='var(--s1)' stroke-width='2' stroke-linejoin='round' stroke-linecap='round'/>
<path d='{path(rsk)}' fill='none' stroke='var(--s2)' stroke-width='2' stroke-linejoin='round' stroke-linecap='round'/>
<circle cx='{xe:.1f}' cy='{y1:.1f}' r='5' fill='var(--s1)' stroke='var(--paper)' stroke-width='2'/><circle cx='{xe:.1f}' cy='{y2:.1f}' r='5' fill='var(--s2)' stroke='var(--paper)' stroke-width='2'/>
{endlabel(y1, lab1, '総資産 ' + P.fmt_usd(tot[-1]), 'var(--s1)')}{endlabel(y2, lab2, 'リスク ' + P.fmt_usd(rsk[-1]), 'var(--s2)')}
<line id='xh' x1='0' x2='0' y1='{T}' y2='{H - B}' stroke='var(--axis)' stroke-width='1' style='display:none'/>
<rect id='hit' x='{L}' y='{T}' width='{W - L - R}' height='{H - T - B}' fill='transparent'/>
</svg>"""
    data = [{"label": p["label"], "x": round(X(i), 1), "total": p["snap"]["total"], "risk": p["snap"]["risk"], "quasi": p["snap"]["quasi"], "cash": p["snap"]["cash"]} for i, p in enumerate(points)]
    legend = "<div class='legend'><span><i style='border-color:var(--s1)'></i>総資産（現金・準現金込み）</span><span><i style='border-color:var(--s2)'></i>リスク資産（アルトのみ）</span></div>"
    table = "<details><summary>表で見る</summary><table><thead><tr><th>時点</th><th class='r'>総資産</th><th class='r'>リスク資産</th><th class='r'>準現金</th><th class='r'>現金</th></tr></thead><tbody>" + "".join(
        f"<tr><td>{esc(p['label'])}</td><td class='r'>{usd(p['snap']['total'])}</td><td class='r'>{usd(p['snap']['risk'])}</td><td class='r'>{usd(p['snap']['quasi'])}</td><td class='r'>{usd(p['snap']['cash'])}</td></tr>" for p in reversed(points)) + "</tbody></table></details>"
    return f"<div class='chart' id='chart'>{svg}<div class='tip' id='tip'></div></div>{legend}{table}<script id='cdata' type='application/json'>{json.dumps(data, ensure_ascii=False)}</script>"

CHART_JS = """
(function(){var el=document.getElementById('cdata');if(!el)return;var d=JSON.parse(el.textContent);var svg=document.querySelector('#chart svg'),hit=document.getElementById('hit'),xh=document.getElementById('xh'),tip=document.getElementById('tip');if(!svg||!hit)return;
function fmt(v){return v>=1e6?'$'+(v/1e6).toFixed(2)+'M':v>=1e4?'$'+Math.round(v/1e3)+'K':'$'+Math.round(v).toLocaleString();}
function row(c,name,v){var r=document.createElement('div');r.className='row';var a=document.createElement('span');var k=document.createElement('i');k.className='key';k.style.borderColor=c;a.appendChild(k);a.appendChild(document.createTextNode(name));var b=document.createElement('b');b.textContent=fmt(v);r.appendChild(a);r.appendChild(b);return r;}
function show(ev){var pt=svg.createSVGPoint();pt.x=ev.clientX;pt.y=ev.clientY;var p=pt.matrixTransform(svg.getScreenCTM().inverse());var best=0,bd=1e9;d.forEach(function(q,i){var dd=Math.abs(q.x-p.x);if(dd<bd){bd=dd;best=i;}});var q=d[best];
xh.setAttribute('x1',q.x);xh.setAttribute('x2',q.x);xh.style.display='';tip.textContent='';var h=document.createElement('div');h.className='sub';h.textContent=q.label;tip.appendChild(h);tip.appendChild(row('#2a78d6','総資産',q.total));tip.appendChild(row('#eb6834','リスク資産',q.risk));var s=document.createElement('div');s.className='sub';s.textContent='準現金 '+fmt(q.quasi)+' / 現金 '+fmt(q.cash);tip.appendChild(s);
var box=svg.getBoundingClientRect();var sx=box.width/720;var left=q.x*sx+12;if(left+180>box.width)left=q.x*sx-190;tip.style.left=left+'px';tip.style.top=(p.y*sx-10)+'px';tip.style.display='block';}
hit.addEventListener('pointermove',show);hit.addEventListener('pointerleave',function(){xh.style.display='none';tip.style.display='none';});})();
(function(){var tabs=document.querySelectorAll('.tab:not(.mtab)');tabs.forEach(function(t){t.addEventListener('click',function(){tabs.forEach(function(u){u.setAttribute('aria-selected','false');});t.setAttribute('aria-selected','true');document.querySelectorAll('.day').forEach(function(dv){dv.hidden=(dv.id!==t.dataset.target);});});});
var mt=document.querySelectorAll('.mtab');mt.forEach(function(t){t.addEventListener('click',function(){mt.forEach(function(u){u.setAttribute('aria-selected','false');});t.setAttribute('aria-selected','true');document.querySelectorAll('.mode').forEach(function(dv){dv.hidden=(dv.id!==t.dataset.target);});});});})();
"""

# ------------------------------------------------------------ 日ごとの増減パネル
def bridge_panel(br, names, ctx, idx, min_usd, buy_min=5000.0):
    def who(w): return esc(names.get(w, "?"))
    def trades_table(rows, kind):
        if not rows: return "<div class='sub'>なし</div>"
        head = {"buy": "<th>時刻(JST)</th><th>誰</th><th>銘柄</th><th class='r'>数量</th><th class='r'>金額</th><th>支払い</th><th></th>",
                "sell": "<th>時刻(JST)</th><th>誰</th><th>銘柄</th><th class='r'>数量</th><th class='r'>金額</th><th>受け取り</th><th></th>",
                "out": "<th>時刻(JST)</th><th>誰</th><th>銘柄</th><th class='r'>数量</th><th class='r'>金額</th><th>先</th><th></th>"}[kind]
        body = ""
        for a in rows:
            link = P.dex_link(a["chain"], a["contract"], ctx); lk = f"<a href='{esc(link)}' target='_blank' rel='noopener'>DexScreener</a>" if link else ""
            other = "外部" if kind == "out" else P.other_leg_text(a)
            times = f" ×{a['n']}" if a["n"] > 1 else ""
            unit = f"<div class='sub'>${a['unit']:.4g}/枚</div>" if a.get("unit") else ""
            body += (f"<tr class='{'buy' if kind == 'buy' else 'sell'}'><td>{jst(a['last'])}{esc(times)}</td><td>{who(a['wallet'])}</td><td>{esc(P.symc(a['sym'], a['chain']))}</td>"
                     f"<td class='r'>{esc(P.fmt_qty(a['amount']))}</td><td class='r'>{usd(a['value'])}{unit}</td><td class='w'>{esc(other)}</td><td>{lk}</td></tr>")
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    risk0 = max(br["risk0"], 1.0); scale = max(abs(br["price"]), br["buys"], br["realized"], br["in"], abs(br["resid"]), abs(br.get("exec_cost", 0)), 1.0)
    def bar(v, color):
        w = min(abs(v) / scale * 100, 100)
        return f"<span class='bar'><i style='left:{0 if v >= 0 else 100 - w:.0f}%;width:{w:.0f}%;background:{color}'></i></span>"
    rows = [("値動き（前時点の保有 × 価格差）", br["price"], "var(--s1)"),
            (f"利確（売り {P.fmt_usd(br['sells'])} ＋ 外部流出 {P.fmt_usd(br['out'])}）", -br["realized"], "var(--sell)"),
            ("買い", br["buys"], "var(--buy)")]
    if br["in"]: rows.append(("外部からの流入", br["in"], "var(--hold)"))
    if abs(br.get("exec_cost", 0)) >= 0.005 * risk0: rows.append(("約定コスト（スリッページ・価格インパクト）", br["exec_cost"], "var(--warn)"))
    if abs(br["resid"]) >= 0.005 * risk0: rows.append(("その他・誤差（エアドロップ、価格取得漏れ）", br["resid"], "var(--mute)"))
    grid = "".join(f"<span class='k'>{esc(k)}</span>{bar(v, c)}<span class='v {cls_delta(v)}'>{P.fmt_usd(v, True)}</span>" for k, v, c in rows)
    realized_pct = f"（利確率 {br['realized_pct']:.1f}%）" if br["realized_pct"] else ""
    head = (f"<div style='font-size:16px;font-weight:600'>{esc(br['label0'])} {P.fmt_usd(br['total0'])} → {esc(br['label1'])} {P.fmt_usd(br['total1'])}"
            f" <span class='{cls_delta(br['total1'] - br['total0'])}'>{P.fmt_usd(br['total1'] - br['total0'], True)}（{P.fmt_pct(br['total_pct'])}）</span></div>"
            f"<div class='sub'>リスク資産 {P.fmt_usd(br['risk0'])} → {P.fmt_usd(br['risk1'])}（{P.fmt_pct(br['risk_pct'])}）{realized_pct}　準現金 {P.fmt_usd(br['quasi1'])}　現金 {P.fmt_usd(br['cash1'])}</div>")
    if br.get("approx"): head += "<div class='sub' style='color:var(--warn)'>※ 記録開始前の時点は、その後の取引を数量で逆算した推定値。当時の価格が不明なため 9/6 朝の価格で評価しており、この期間の「値動き」は含まれません。</div>"
    movers = [m for m in br["movers"] if abs(m["usd"]) >= min_usd][:8]
    mv = ("<table><thead><tr><th>銘柄</th><th class='r'>価格変化</th><th class='r'>寄与額</th><th class='r'>現在評価</th></tr></thead><tbody>" + "".join(
        f"<tr><td>{esc(P.symc(m['sym'], m.get('chain', '')))}</td><td class='r {cls_delta(m['pct'])}'>{m['pct']:+.1f}%</td><td class='r {cls_delta(m['usd'])}'>{P.fmt_usd(m['usd'], True)}</td><td class='r'>{usd(m['hold'])}</td></tr>" for m in movers) + "</tbody></table>") if movers else "<div class='sub'>大きな値動きなし</div>"
    internal = ("<details><summary>内部移動（本体⇔子⇔孫、総資産は不変） " + str(len(br["internal"])) + " 件</summary><div class='sub'>" + "、".join(
        f"{jst(e['ts'])} {who(e['wallet'])}→{who(e['cp'])} {esc(e['token'])} {esc(P.fmt_qty(e['amount']))} ({usd(e.get('usd'))})" for e in br["internal"][:20]) + "</div></details>") if br["internal"] else ""
    # 銘柄ごとの前後比較
    pos = [x for x in br.get("positions", []) if max(x["usd0"], x["usd1"]) >= min_usd]
    def pxs(v): return ("$%.4g" % v) if v else "—"
    def prow(x):
        dq = x["amt1"] - x["amt0"]; dqs = f"<span class='{cls_delta(dq)}'>{'+' if dq > 0 else ''}{P.fmt_qty(dq) if dq else '±0'}</span>" if abs(dq) > 1e-9 else "<span class='mute'>±0</span>"
        ppct = ((x["px1"] / x["px0"] - 1) * 100) if (x["px0"] and x["px1"]) else None
        st = f" <span class='tag'>{x['status']}</span>" if x["status"] else ""
        return (f"<tr><td>{esc(P.symc(x['sym'], x['chain']))}{st}</td><td class='r'>{esc(P.fmt_qty(x['amt0']))} → {esc(P.fmt_qty(x['amt1']))}</td><td class='r'>{dqs}</td>"
                f"<td class='r'>{pxs(x['px0'])} → {pxs(x['px1'])}</td><td class='r {cls_delta(ppct or 0)}'>{P.fmt_pct(ppct)}</td>"
                f"<td class='r'>{usd(x['usd0'])} → {usd(x['usd1'])}</td><td class='r {cls_delta(x['usd1'] - x['usd0'])}'>{P.fmt_usd(x['usd1'] - x['usd0'], True)}</td>"
                f"<td class='r {cls_delta(x['price_effect'])}'>{P.fmt_usd(x['price_effect'], True)}</td><td class='r {cls_delta(x['qty_effect'])}'>{P.fmt_usd(x['qty_effect'], True)}</td></tr>")
    ptable = ("<div style='overflow-x:auto'><table><thead><tr><th>銘柄</th><th class='r'>枚数 前 → 後</th><th class='r'>枚数増減</th><th class='r'>単価 前 → 後</th><th class='r'>価格変化</th>"
              "<th class='r'>評価額 前 → 後</th><th class='r'>評価額増減</th><th class='r'>うち値動き</th><th class='r'>うち売買</th></tr></thead><tbody>"
              + "".join(prow(x) for x in pos[:25]) + "</tbody></table></div>"
              + (f"<div class='sub'>ほか {len(pos) - 25} 銘柄（${min_usd:,.0f} 以上）</div>" if len(pos) > 25 else "")) if pos else "<div class='sub'>対象なし</div>"
    return (f"<div class='day' id='day-{idx}'{'' if idx == 0 else ' hidden'}>{head}<div class='bridge'>{grid}"
            f"<span class='k tot'>リスク資産の増減 合計</span><span class='tot'></span><span class='v tot {cls_delta(br['risk1'] - br['risk0'])}'>{P.fmt_usd(br['risk1'] - br['risk0'], True)}</span></div>"
            f"<h3>🟢 買い（${buy_min:,.0f} 以上、金額＝支払った額。同一銘柄の分割買いは合算）</h3>{trades_table([a for a in br['buy_list'] if a['value'] >= buy_min], 'buy')}"
            f"<h3>🔻 利確 ＝ 売り（金額＝受け取った額）</h3>{trades_table([a for a in br['sell_list'] if a['value'] >= min_usd], 'sell')}"
            f"<h3>📤 外部流出（クラスター外のアドレスへ）</h3>{trades_table([a for a in br['out_list'] + br['cash_out_list'] if a['value'] >= min_usd], 'out')}"
            f"<h3>📈 値動きの寄与（上位）</h3>{mv}"
            f"<h3>📋 銘柄ごとの枚数・単価・評価額（{esc(br['label0'])} → {esc(br['label1'])}、${min_usd:,.0f} 以上）</h3>{ptable}{internal}</div>")

def chain_breakdown(snap, chains):
    by = defaultdict(float)
    for k, p in snap["pos"].items(): by[k.split(":")[0]] += p["usd"]
    parts = [f"{esc(chains.get(c, {}).get('name', c))} <b>{P.fmt_usd(v)}</b>" for c, v in sorted(by.items(), key=lambda kv: -kv[1]) if v >= 1]
    return "　".join(parts) if parts else "—"

# ------------------------------------------------------------ 本体
def render(cfg, chains, wallets, events, holdings, batch_list, threshold, label, out_path, holdings_updated=None, timeline=None):
    now = datetime.now(timezone.utc)
    tl = timeline or {}; pts = tl.get("points") or []; bridges = tl.get("bridges") or []; names = tl.get("names") or {}; ctx = tl.get("ctx") or {"chains": chains, "known_stables": set()}
    min_usd = float(tl.get("min_usd") or 5000); cut = tl.get("cut_desc") or "24:00 JST"
    # --- 上段: ヒーロー ---
    cur = pts[-1]["snap"] if pts else None
    if cur:
        prev = bridges[-1] if bridges else None
        delta = f"<div class='delta {cls_delta(prev['total1'] - prev['total0'])}'>{P.fmt_usd(prev['total1'] - prev['total0'], True)}（{P.fmt_pct(prev['total_pct'])}） {esc(prev['label0'])} 比</div>" if prev else "<div class='delta sub'>記録開始（増減は次の時点から）</div>"
        n_w = len(wallets)
        hero = (f"<section class='panel hero'><div><div class='lbl'>総資産（本体＋子＋孫、全チェーン合算）</div><div class='big'>{P.fmt_usd(cur['total'])}</div>{delta}</div>"
                f"<div class='tiles'><div class='tile'><div class='v'>{P.fmt_usd(cur['risk'])}</div><div class='lbl'>リスク資産（アルト）</div></div>"
                f"<div class='tile'><div class='v'>{P.fmt_usd(cur['quasi'])}</div><div class='lbl'>準現金（ETH・BNB）</div></div>"
                f"<div class='tile'><div class='v'>{P.fmt_usd(cur['cash'])}</div><div class='lbl'>現金（ステーブル）</div></div>"
                f"<div class='tile'><div class='v'>{n_w}</div><div class='lbl'>監視ウォレット</div></div></div>"
                f"<div class='sub' style='width:100%'>本体アドレス: " + "　".join(f"<code style='font-size:12px'>{esc(a)}</code>（{'Solana' if not a.startswith('0x') else 'EVM'}）" for a in cfg.get("main_wallets", [])) + "</div>"
                f"<div class='sub' style='width:100%'>チェーン別: {chain_breakdown(cur, chains)}</div>"
                f"<div class='sub' style='width:100%'>残高時点 {jst(cur['ts'])} JST　評価は DexScreener/Blockscout の現在値（流動性の薄い銘柄は実際に売れる額より大きく出ます）</div>"
                + (f"<div class='sub' style='width:100%;color:var(--warn)'>※ {len(tl.get('stale') or [])} 件のウォレット・チェーンで残高を取得できず、前回の保有数量に現在価格を掛けて表示しています（データ元の一時障害）。</div>" if tl.get("stale") else "")
                + "</section>")
    else:
        hero = "<section class='panel hero'><div class='sub'>残高スナップショットがまだありません。次回の実行で作成されます。</div></section>"
    chart = f"<section class='panel'><h2>資産の推移（毎日 {esc(cut)} 時点）</h2>{chart_svg(pts)}</section>"
    # --- 日ごとの増減 ---
    if bridges:
        tabs = "".join(f"<button class='tab' data-target='day-{i}' aria-selected='{'true' if i == 0 else 'false'}'>{esc(br['label1'])}</button>" for i, br in enumerate(reversed(bridges)))
        panels = "".join(bridge_panel(br, names, ctx, i, min_usd, float(tl.get("buy_list_min_usd") or 5000)) for i, br in enumerate(reversed(bridges)))
        days = f"<section class='panel'><h2>時点ごとの増減（値動き / 利確 / 買い に分解）</h2><div class='tabs'>{tabs}</div>{panels}</section>"
    else:
        days = f"<section class='panel'><h2>時点ごとの増減</h2><div class='sub'>次の締め（{esc(cut)}）から、前日比の分解（値動き / 利確 / 買い）を表示します。</div></section>"
    # --- 新規購入銘柄の成績（別出し） ---
    newpos = tl.get("new_positions") or []
    if newpos:
        def nrow(g):
            who = "/".join(esc(names.get(w, "?")) for w in g["wallets"]); link = P.dex_link(g["chain"], g["contract"], ctx)
            pnl = f"<span class='{cls_delta(g['pnl_pct'])}'>{P.fmt_pct(g['pnl_pct'])}</span>" if g["pnl_pct"] is not None else "<span class='mute'>—</span>"
            vs = f"<span class='{cls_delta(g['vs_prev_pct'])}'>{P.fmt_pct(g['vs_prev_pct'])}</span>" if g["vs_prev_pct"] is not None else "<span class='mute'>—</span>"
            path = " → ".join(f"{esc(l)} ${v:.4g}" for l, v in g["path"][-4:]) if g["path"] else ""
            sold = f"<div class='sub'>{g['sold_pct']:.0f}% 売却済（{usd(g['proceeds'])}）</div>" if g["sold_pct"] >= 1 else ""
            return (f"<tr class='buy'><td>{esc(P.symc(g['sym'], g['chain']))}<div class='sub'>{who}</div></td>"
                    f"<td>{jst(g['first'])}<div class='sub'>{g['n']}回、最終 {jst(g['last'])[6:]}</div></td>"
                    f"<td class='r'>{esc(P.fmt_qty(g['qty']))}<div class='sub'>{usd(g['cost'])}</div></td>"
                    f"<td class='r'>${g['avg']:.4g}</td><td class='r'>{('$%.4g' % g['px']) if g['px'] else '—'}</td><td class='r'>{pnl}</td><td class='r'>{vs}</td>"
                    f"<td class='r'>{esc(P.fmt_qty(g['held']))}<div class='sub'>{usd(g['value'])}</div>{sold}</td>"
                    f"<td class='r'>{P.fmt_usd(g['liq']) if g['liq'] else '—'}</td><td class='w sub'>{path}</td>"
                    f"<td>{('<a href=' + chr(39) + esc(link) + chr(39) + ' target=_blank rel=noopener>DexScreener</a>') if link else ''}</td></tr>")
        newpos_html = (f"<section class='panel'><h2>🆕 新規購入銘柄の成績（直近14日に買った銘柄・支払 ${float(tl.get('buy_list_min_usd') or 5000):,.0f} 以上・平均取得単価に対する現在の損益）</h2><div style='overflow-x:auto'>"
                       "<table><thead><tr><th>銘柄 / 買い手</th><th>初回買い(JST)</th><th class='r'>買った枚数 / 支払額</th><th class='r'>平均取得</th><th class='r'>いま</th><th class='r'>損益</th><th class='r'>前時点比</th><th class='r'>現在保有 / 評価</th><th class='r'>流動性</th><th>買ってからの価格</th><th></th></tr></thead><tbody>"
                       + "".join(nrow(g) for g in newpos[:30]) + "</tbody></table></div>"
                       "<div class='sub'>平均取得＝支払った ETH/BNB の時価 ÷ 受け取った枚数（スリッページ込みの実質単価）。損益＝いまの価格 ÷ 平均取得 − 1。既存の大型銘柄（PONS 等）の推移は上のタブへ。</div></section>")
    else:
        newpos_html = ""
    # --- 銘柄別の日次推移（前日比 + 開始日比） ---
    mx = tl.get("matrix") or {}
    matrix_html = ""
    if mx.get("points") and len(mx["points"]) >= 2:
        P_ = mx["points"]; n = len(P_)
        def pct(a, b): return ((a / b - 1) * 100) if (a is not None and b) else None
        def dtxt(v, unit=""):   # 差分（$ or 枚）
            if v is None: return "—"
            return P.fmt_usd(v, True) if unit == "$" else (("+" if v > 0 else "") + (P.fmt_qty(v) if abs(v) >= 1000 else f"{v:,.4g}"))
        def sub(dprev, dstart):
            a = f"前日 <span class='{cls_delta(dprev or 0)}'>{P.fmt_pct(dprev)}</span>" if dprev is not None else "前日 —"
            b = f"累計 <span class='{cls_delta(dstart or 0)}'>{P.fmt_pct(dstart)}</span>" if dstart is not None else "累計 —"
            return f"<div class='sub'>{a} · {b}</div>"
        def table(mode):
            head = "".join(f"<th class='r'>{esc(pt['label'])}{' <span class=mute>推定</span>' if pt['approx'] else ''}</th>" for pt in P_)
            body = ""
            if mode == "usd":
                for name, key in (("総資産", "total"), ("リスク資産", "risk")):
                    vals = [tt[key] for tt in mx["totals"]]
                    cells = "".join(f"<td class='r'><b>{P.fmt_usd(v)}</b>{sub(pct(v, vals[i-1]) if i else None, pct(v, vals[0]) if i else None)}</td>" for i, v in enumerate(vals))
                    body += f"<tr><td><b>{name}</b></td>{cells}</tr>"
            for r in mx["rows"]:
                cells = ""; first = next((c for c in r["cells"] if c), None)
                for i, c in enumerate(r["cells"]):
                    if not c: cells += "<td class='r mute'>—</td>"; continue
                    prev = next((r["cells"][j] for j in range(i - 1, -1, -1) if r["cells"][j]), None) if i else None
                    if mode == "usd":
                        v = c["usd"]; cells += f"<td class='r'>{P.fmt_usd(v)}{sub(pct(v, prev['usd']) if prev else None, pct(v, first['usd']) if (first and first is not c) else None)}</td>"
                    elif mode == "px":
                        v = c.get("px"); cells += (f"<td class='r'>${v:.4g}{sub(pct(v, prev.get('px')) if prev else None, pct(v, first.get('px')) if (first and first is not c) else None)}</td>" if v else "<td class='r mute'>—</td>")
                    else:
                        v = c["amt"]; d1 = (v - prev["amt"]) if prev else None; d0 = (v - first["amt"]) if (first and first is not c) else None
                        cells += f"<td class='r'>{esc(P.fmt_qty(v))}<div class='sub'>前日 {esc(dtxt(d1))} · 累計 {esc(dtxt(d0))}</div></td>"
                body += f"<tr><td>{esc(P.symc(r['sym'], r['chain']))}</td>{cells}</tr>"
            return f"<div class='mode' id='mode-{mode}'{'' if mode == 'usd' else ' hidden'} style='overflow-x:auto'><table><thead><tr><th>銘柄</th>{head}</tr></thead><tbody>{body}</tbody></table></div>"
        matrix_html = (f"<section class='panel'><h2>銘柄別の日次推移（各セルに 前日比 と 開始日からの累計 を表示。毎日 {esc(cut)} 時点）</h2>"
                       "<div class='tabs'><button class='tab mtab' data-target='mode-usd' aria-selected='true'>評価額</button><button class='tab mtab' data-target='mode-px' aria-selected='false'>単価</button><button class='tab mtab' data-target='mode-amt' aria-selected='false'>枚数</button></div>"
                       + table("usd") + table("px") + table("amt")
                       + f"<div class='sub'>対象＝現在の評価額上位 {len([r for r in mx['rows']])} 銘柄＋直近14日の新規購入銘柄。「推定」の列は記録開始前を取引から逆算したもので単価は 9/6 朝の値。累計の起点は {esc(P_[0]['label'])}。</div></section>")
    # --- 現在のポジション（クラスター合算、上位） ---
    pos_rows = ""
    if cur:
        top = sorted([p for p in cur["pos"].values() if p["b"] == "risk"], key=lambda p: -p["usd"])[:15]
        keyed = sorted([(k, p) for k, p in cur["pos"].items() if p["b"] == "risk"], key=lambda kv: -kv[1]["usd"])[:15]
        pos_rows = "<section class='panel'><h2>いま持っているリスク資産（クラスター合算・上位15）</h2><table><thead><tr><th>銘柄</th><th class='r'>数量</th><th class='r'>単価</th><th class='r'>評価額</th><th class='r'>比率</th></tr></thead><tbody>" + "".join(
            f"<tr><td>{esc(P.symc(p['sym'], k.split(':')[0]))}</td><td class='r'>{esc(P.fmt_qty(p['amt']))}</td><td class='r'>{('$%.4g' % p['px']) if p.get('px') else '—'}</td><td class='r'>{usd(p['usd'])}</td><td class='r'>{p['usd'] / cur['risk'] * 100:.1f}%</td></tr>" for k, p in keyed) + "</tbody></table></section>"
    # --- 評価から除外した銘柄（異常トークン・売れないエアドロップ） ---
    def who(w): return esc(names.get(w) or label(w))
    exc = []
    for key, h in holdings.items():
        ch, w = key.split(":")
        for c, v in h.items():
            if v.get("excluded"): exc.append((ch, w, v))
    exc.sort(key=lambda x: -(x[2].get("usd_raw") or 0))
    excluded_html = ("<section class='panel'><h2>⚠️ 評価から除外した銘柄（総資産に含めていません）</h2><div style='overflow-x:auto'><table><thead><tr><th>銘柄</th><th>誰</th><th class='r'>枚数</th><th class='r'>名目評価額</th><th>除外理由</th></tr></thead><tbody>"
                     + "".join(f"<tr class='warn'><td>{esc(P.symc(v.get('symbol') or '?', ch))}</td><td>{who(w)}</td><td class='r'>{esc(P.fmt_qty(v['amount']))}</td><td class='r'>{usd(v.get('usd_raw'))}</td><td class='w'>{esc(v['excluded'])}</td></tr>" for ch, w, v in exc[:30])
                     + "</tbody></table></div><div class='sub'>枚数×価格の名目値が、総供給・時価総額・流動性・入手経路（買っていない受取のみ）と矛盾する銘柄。売って現金化できる根拠が無いため 0 評価にしている。</div></section>") if exc else ""
    # --- 下段: 従来の台帳（折りたたみ） ---
    by_wallet = defaultdict(list)
    for key, h in holdings.items():
        ch, w = key.split(":")
        for s, v in h.items(): by_wallet[w].append({"sym": v.get("symbol") or s, "chain": ch, **v})
    left = []
    order = sorted(wallets, key=lambda w: ({"本体": 0, "子": 1, "孫": 2}.get(wallets[w]["role"], 3), w))
    for w in order:
        items = sorted(by_wallet.get(w, []), key=lambda x: -(x.get("usd") or 0)); tot = sum(x.get("usd") or 0 for x in items)
        big = [x for x in items if (x.get("usd") or 0) >= threshold]
        rows = "".join(f"<div>{esc(P.symc(x['sym'], x['chain']))} {usd(x.get('usd'))} <span class='sub'>{num(x['amount'])}枚</span></div>" for x in big[:20])
        left.append(f"<div style='margin-bottom:10px'><b>{who(w)}</b> <span class='sub'>{usd(tot)}</span><div class='sub'>{rows}</div></div>")
    evs = sorted(events, key=lambda e: e["ts"], reverse=True)
    major = [e for e in evs if ((e.get("usd") or 0) >= threshold or e["kind"].startswith("新ウォレット")) and not e["kind"].startswith("売却") and e["kind"] not in P.NOISE_KINDS]
    erows = "".join(f"<tr class='{kcls(e['kind'])}'><td>{jst(e['ts'])}<span class='tag'>{chains[e['chain']]['name']}</span></td><td>{who(e['wallet'])}</td>"
                    f"<td><span class='k {kcls(e['kind'])}'>{esc(e['kind'])}</span></td><td>{e['dir']}</td><td>{esc(P.symc(e['token'], e['chain']))}</td><td class='r'>{num(e['amount'])}</td><td class='r'>{usd(e.get('usd'))}</td><td>{esc(e.get('cp_label') or '')}</td></tr>" for e in major[:200])
    brows = "".join(f"<tr class='sell'><td>{jst(int(datetime.fromisoformat(b['start']).timestamp()))} – {b['end'][11:16]}</td><td>{who(b['wallet'])}</td><td>{esc(P.symc(b['token'], b['chain']))}</td><td class='r'>{b['n']}</td><td class='r'>{num(b['amount'])}</td><td class='r'>{usd(b['usd'])}</td></tr>"
                    for b in sorted(batch_list, key=lambda b: b["start"], reverse=True)[:60] if b["usd"] >= threshold)
    old = (f"<details class='old'><summary>詳細台帳（従来表示：ウォレット別保有・売却バッチ・主要イベント）</summary>"
           f"<div class='grid2' style='margin-top:10px'><section class='panel'><h2>ウォレット別保有（${threshold:,.0f} 以上）</h2>{''.join(left)}</section>"
           f"<section class='panel'><h2>売却バッチ（30分以内の連続スワップを合算）</h2><table><thead><tr><th>開始(JST) – 終了(UTC)</th><th>誰</th><th>銘柄</th><th class='r'>回数</th><th class='r'>数量</th><th class='r'>USD</th></tr></thead><tbody>{brows or '<tr><td colspan=6 class=sub>なし</td></tr>'}</tbody></table></section></div>"
           f"<section class='panel'><h2>主要イベント（${threshold:,.0f} 以上・新ウォレット）</h2><div style='overflow-x:auto'><table><thead><tr><th>時刻(JST)</th><th>誰</th><th>種別</th><th>方向</th><th>銘柄</th><th class='r'>数量</th><th class='r'>USD</th><th>相手</th></tr></thead><tbody>{erows or '<tr><td colspan=8 class=sub>なし</td></tr>'}</tbody></table></div></section></details>")
    rules = ("<section class='panel'><h2>読み方</h2><div class='sub'>"
             f"<p>リスク資産＝アルトコイン。準現金＝ETH・BNB。現金＝本物のステーブル。時点は毎日 {esc(cut)} の締めと現在。</p>"
             "<p>値動き＝前時点の保有数量 × 価格差。利確＝リスク資産を売って受け取った額（ETH・USDC・別銘柄）＋クラスター外へ送った額。買い＝リスク資産を買うのに支払った額（スワップ・クロスチェーン購入）。約定コスト＝支払った額と受け取った銘柄の時価の差（流動性の薄い銘柄を大量に買うと大きくなる）。誤差＝この分解で説明できない残り（エアドロップ、価格取得漏れなど）。</p>"
             "<p>本体・子・孫の間の移動は総資産を変えないので内部移動として折りたたみ。$5,000 未満の動きは集計には含むが一覧には出さない。</p></div></section>")
    doc = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{esc(tl.get("name") or "クジラ")} 資産レポート</title><style>{CSS}</style></head><body>
<header><h1>{esc(tl.get("name") or "クジラ")} 資産レポート <small>本体 {sum(1 for w in wallets if wallets[w]['role'] == '本体')} ＋ 自動検出 {sum(1 for w in wallets if wallets[w]['role'] != '本体')} ウォレット</small></h1>
<div class="sub">更新 {P.jst(int(now.timestamp())).strftime('%m-%d %H:%M')} JST（10分ごと）</div></header>
<main>{hero}{chart}{matrix_html}{days}{pos_rows}{newpos_html}{excluded_html}{rules}{old}</main><script>{CHART_JS}</script></body></html>"""
    out_path.write_text(doc, encoding="utf-8")
