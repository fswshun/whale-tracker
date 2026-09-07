#!/usr/bin/env python3
"""
tracker.py — クジラ資金フロー自動追跡（GitHub Actions 常駐用）

1回の実行で:
  - config.json の本体ウォレットを、活動のある全チェーンで取得（初回はチェーン自動探索）
  - 新しい tx を分類 → data/events.jsonl に追記（重複なし）
  - 子ウォレット（未知EOAへのガス種銭 / トークン送金）を自動検出 → data/wallets.json に登録
  - 現在残高を取得・USD換算 → data/holdings.json（holdings_every_n_runs 回に1回更新）
  - しきい値以上の新イベントを Telegram に送信
  - docs/index.html を再生成（GitHub Pages で閲覧）

データ取得元（すべて無料枠）:
  Robinhood Chain / Ethereum / Base / Arbitrum → Blockscout 公開インスタンス（キー不要）
  BSC → NodeReal MegaNode（無料プラン、NODEREAL_KEY 必須）
  価格 → DexScreener（キー不要）＋ Blockscout の exchange_rate

必要な環境変数（GitHub Secrets）:
  NODEREAL_KEY     https://dashboard.nodereal.io で作る無料キー（BSC 用）
  BLOCKSCOUT_KEY   推奨。https://dev.blockscout.com の無料 PRO キー（100K credits/日・5 RPS）。無いと公開インスタンスの 429 に当たりやすい
  TG_TOKEN, TG_CHAT  Telegram 通知（無ければ通知だけスキップ）
  DRY_RUN=1        ローカル確認用: 通知もファイル保存もしない
"""
import json, os, re, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
import portfolio as P

ROOT = Path(__file__).resolve().parent
DATA, DOCS = ROOT / "data", ROOT / "docs"
DATA.mkdir(exist_ok=True); DOCS.mkdir(exist_ok=True)

CFG = json.load(open(ROOT / "config.json"))
NODEREAL_KEY = os.getenv("NODEREAL_KEY", "")
BLOCKSCOUT_KEY = os.getenv("BLOCKSCOUT_KEY", "")   # 推奨。dev.blockscout.com の無料 PRO キー（proapi_…）。無いと各インスタンスの公開APIを叩き 429 になりやすい
BLOCKSCOUT_PRO = "https://api.blockscout.com"
TG_TOKEN, TG_CHAT = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
DRY_RUN = os.getenv("DRY_RUN", "") not in ("", "0")

WETH_ETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
CHAINS = {
    "robinhood": {"name": "Robinhood Chain", "provider": "blockscout", "base": "https://robinhoodchain.blockscout.com", "chain_id": 4663, "rpcs": [],
                  "native": "ETH", "dex": "robinhood", "wnative": "", "native_ref": ("ethereum", WETH_ETH)},
    "bsc":       {"name": "BSC", "provider": "nodereal", "rpc": "https://bsc-mainnet.nodereal.io/v1/{key}",
                  "native": "BNB", "dex": "bsc", "wnative": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"},
    "ethereum":  {"name": "Ethereum", "provider": "blockscout", "base": "https://eth.blockscout.com", "chain_id": 1, "rpcs": ["https://eth.llamarpc.com", "https://cloudflare-eth.com"],
                  "native": "ETH", "dex": "ethereum", "wnative": WETH_ETH},
    "base":      {"name": "Base", "provider": "blockscout", "base": "https://base.blockscout.com", "chain_id": 8453, "rpcs": ["https://mainnet.base.org", "https://base.llamarpc.com"],
                  "native": "ETH", "dex": "base", "wnative": "0x4200000000000000000000000000000000000006"},
    "arbitrum":  {"name": "Arbitrum", "provider": "blockscout", "base": "https://arbitrum.blockscout.com", "chain_id": 42161, "rpcs": ["https://arb1.arbitrum.io/rpc"],
                  "native": "ETH", "dex": "arbitrum", "wnative": "0x82af49447d8a07e3bd95bd0d56f35241523fbbe2"},
}
STABLES = {"USDC", "USDT", "USDG", "USD1", "DAI", "FDUSD", "BUSD"}
# 本物のステーブルだけ $1 固定。偽 USDT 等のエアドロップ（アドレスポイズニング）を $ 換算しないための一覧
KNOWN_STABLES = {
    ("bsc", "0x55d398326f99059ff775485246999027b3197955"), ("bsc", "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"),
    ("bsc", "0xe9e7cea3dedca5984780bafc599bd69add087d56"), ("bsc", "0xc5f0f7b66764f6ec8c8dff7ba683102295e16409"),
    ("bsc", "0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d"), ("bsc", "0x1af3f329e8be154074d8769d1ffa4ee058b1dbc3"),
    ("ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7"), ("ethereum", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"),
    ("ethereum", "0x6b175474e89094c44da98b954eedeac495271d0f"), ("ethereum", "0xe343167631d89b6ffc58b88d6b7fb0228795491d"),
    ("ethereum", "0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d"),
    ("base", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"),
    ("arbitrum", "0xaf88d065e77c8cc2239327c5edb3a432268e5831"), ("arbitrum", "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"),
    ("robinhood", "0x5fc5360d0400a0fd4f2af552add042d716f1d168"),
}
MIN_LIQ_USD = 5000     # DexScreener の流動性がこれ未満のペアの価格は使わない（偽トークン・ゴミ価格対策）
BUY_ALERT_USD = float(CFG.get("buy_alert_usd", 5000))        # 買いはこの額から即時 Telegram
MOVE_MIN_USD = float(CFG.get("move_min_usd", 5000))          # 一覧・まとめに載せる最小額
PRICE_ALERT_PCT = float(CFG.get("price_move_alert_pct", 5))  # 1時間でリスク資産がこの%動いたらまとめを送る
DAILY_HOUR_UTC = int(CFG.get("daily_report_hour_utc", 0))    # 日次レポート時刻（0 UTC = 9:00 JST）
CTX = {"chains": CHAINS, "known_stables": KNOWN_STABLES}
RUN_BUDGET_S = float(CFG.get("run_budget_minutes", 18)) * 60   # これを超えたら残りは次回に回して保存だけ行う（Actions timeout 対策）
PRICE_CACHE_H = 24     # DexScreener が落ちている時に使う前回価格の有効時間
T0 = time.time()
def over_budget(): return time.time() - T0 > RUN_BUDGET_S
THRESHOLD = float(CFG.get("threshold_usd", 10000))
GAS_SEED_USD = float(CFG.get("gas_seed_usd", 20))        # これ未満のネイティブ送金を未知EOAへ → 新ウォレット開設シグナル
BUY_MATCH_HOURS = int(CFG.get("buy_match_hours", 168))    # 受取前この時間内（7日）に同じ相手へ原資を払っていれば「購入」
DUST_USD = float(CFG.get("dust_usd", 50))                 # これ未満の受取はダスト扱い（通知しない）
INITIAL_LOOKBACK_H = float(CFG.get("initial_lookback_hours", 120))   # 初回・新ウォレット追加時の遡り時間（全チェーン）
HOLDINGS_EVERY = int(CFG.get("holdings_every_n_runs", 4))            # 残高更新の間隔（実行回数）
SECONDARY_EVERY = int(CFG.get("secondary_chains_every_n_runs", 4))   # 副次チェーン(ETH/Base/Arb)の取得間隔（実行回数）
PRIMARY_CHAINS = set(CFG.get("primary_chains", ["robinhood", "bsc"]))    # 毎回取得するチェーン
NOTIFY_MAX_AGE_H = float(CFG.get("notify_max_age_hours", 48))        # これより古いイベントは通知しない（初回バックフィル対策）
CHILD_MAX_AGE_D = float(CFG.get("child_detect_max_age_days", 30))    # これより古い送金からは子ウォレットを起こさない
SERVICE_PREFIX = re.compile(r"^0x00aa", re.I)
BLOCKSCOUT_PAGE = 10000
NR_HEAD_MARGIN = int(CFG.get("nodereal_head_margin_blocks", 40))   # BSC 先頭からこのブロック数だけ手前まで取得（次回に持ち越し）

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
                  "Accept": "application/json"})   # Blockscout の Cloudflare は素の requests UA を 403 にする

def log(*a): print(*a, flush=True)
def warn(*a): print("警告:", *a, file=sys.stderr, flush=True)

# ------------------------------------------------------------ 永続データ
def jload(p, default):
    return json.load(open(p)) if p.exists() else default

wallets = jload(DATA / "wallets.json", {})        # addr -> {role,parent,first_seen,chains}
seen_tx = set(jload(DATA / "seen_tx.json", []))
cursor = jload(DATA / "cursor.json", {})          # f"{chain}:{addr}" -> last block
state = jload(DATA / "state.json", {"run_count": 0})
pending_eoa = jload(DATA / "pending_eoa.json", [])  # EOA判定が取れなかった候補（次回再判定）
events = [json.loads(l) for l in open(DATA / "events.jsonl")] if (DATA / "events.jsonl").exists() else []
snapshots = P.load_snapshots(DATA / "snapshots.jsonl")   # クラスター合算の残高履歴（1時間ごと、7日超は日次）
labels = {k.lower(): v for k, v in CFG.get("labels", {}).items()}

for a in CFG["main_wallets"]:
    wallets.setdefault(a.lower(), {"role": "本体", "parent": None, "first_seen": None, "chains": []})

def is_watched(a): return bool(a) and a.lower() in wallets
def is_service(a): return bool(a) and (a.lower() in labels or bool(SERVICE_PREFIX.match(a)))
def short(a): return a[:6] + "…" + a[-4:] if a else ""
def label(a):
    a = (a or "").lower()
    if a in labels: return labels[a]
    if a in wallets: return f"{wallets[a]['role']} {short(a)}"
    if SERVICE_PREFIX.match(a): return f"0x00AA系サービス {short(a)}"
    return short(a)
def child_role(parent_role): return {"本体": "子", "子": "孫", "孫": "曾孫"}.get(parent_role, "子孫")
BURN = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000001",
        "0x00000000000000000000000000000000deadbeef", "0xdead000000000000000042069420694206942069"}
def is_burn(a): return (a or "").lower() in BURN
def is_lookalike(a):
    """監視ウォレットと先頭6桁・末尾3桁が同じ別アドレス＝アドレスポイズニングの偽物"""
    a = (a or "").lower()
    return any(w != a and w[:6] == a[:6] and w[-3:] == a[-3:] for w in wallets)

# ------------------------------------------------------------ HTTP 共通
API_CALLS = defaultdict(int)   # ホスト別の呼び出し回数（無料枠の見積もり用。実行末尾でログ）
def http_json(method, url, retries=3, timeout=30, **kw):
    """JSON を返す。失敗（429 / 5xx / Cloudflare challenge / 例外）は退避して再試行、最終的に None"""
    last = ""
    for i in range(retries):
        try:
            API_CALLS[url.split("/")[2].split("?")[0]] += 1
            r = S.request(method, url, timeout=timeout, **kw)
            if r.status_code == 429:
                last = f"HTTP 429 {r.text[:80]!r}"; time.sleep(6 * (i + 1)); continue
            if r.status_code in (500, 502, 503, 504) or r.headers.get("cf-mitigated") == "challenge":
                last = f"HTTP {r.status_code} {r.text[:80]!r}"; time.sleep(2 * (i + 1)); continue
            if not r.ok: last = f"HTTP {r.status_code} {r.text[:120]!r}"; time.sleep(1 + i); continue
            return r.json()
        except Exception as e:
            last = repr(e); time.sleep(3 * (i + 1))
    warn(f"{method} {url.split('?')[0]} 失敗: {last}")
    return None

# ------------------------------------------------------------ Blockscout（Etherscan互換 + v2）
def bs_compat_url(chain, params):
    """Etherscan互換API の (url, params)。PRO キーがあれば統一ホスト、無ければ各インスタンス"""
    if BLOCKSCOUT_KEY:
        return BLOCKSCOUT_PRO + "/v2/api", {"chain_id": CHAINS[chain]["chain_id"], "apikey": BLOCKSCOUT_KEY, **params}
    return CHAINS[chain]["base"] + "/api", dict(params)

def bs_v2_url(chain, path, params=None):
    if BLOCKSCOUT_KEY:
        return f"{BLOCKSCOUT_PRO}/{CHAINS[chain]['chain_id']}/api/v2{path}", {"apikey": BLOCKSCOUT_KEY, **(params or {})}
    return CHAINS[chain]["base"] + "/api/v2" + path, dict(params or {})

def bs_compat(chain, params):
    """互換API。成功→list/str、'該当なし'→[]、失敗→None"""
    time.sleep(0.4)
    url, p = bs_compat_url(chain, params)
    j = http_json("GET", url, params=p)
    if j is None or not isinstance(j, dict): return None
    st, msg, res = str(j.get("status")), str(j.get("message", "")), j.get("result")
    if st == "1": return res if res is not None else []
    if "No transactions" in msg or "No internal" in msg or res == []: return []
    if st == "2" and isinstance(res, list): return res     # 内部tx未処理分ありの部分成功
    warn(f"{chain} compat {params.get('action')}: {msg} {str(res)[:80]}")
    return None

def bs_compat_all(chain, params):
    """互換APIのページング（offset 上限 10000 × 最大 5 ページ）"""
    out = []
    for page in range(1, 6):
        res = bs_compat(chain, {**params, "page": page, "offset": BLOCKSCOUT_PAGE})
        if res is None:
            if page > 1: warn(f"{chain} {params.get('action')}: {page}ページ目が取れないため {len(out)} 行で打ち切り"); return out   # PRO API は page×offset ≤ 10000
            return None
        out.extend(res)
        if len(res) < BLOCKSCOUT_PAGE: break
    return out

def bs_v2(chain, path, params=None):
    time.sleep(0.4)
    url, p = bs_v2_url(chain, path, params)
    return http_json("GET", url, params=p)

def bs_latest_block(chain):
    url, p = bs_compat_url(chain, {"module": "block", "action": "eth_block_number"})
    j = http_json("GET", url, params=p)
    try: return int(j["result"], 16)
    except Exception: pass
    j = bs_v2(chain, "/main-page/blocks")
    try: return int(j[0]["height"])
    except Exception: return None

def bs_lookback_blocks(chain, hours):
    j = bs_v2(chain, "/stats")
    try: sec = float(j["average_block_time"]) / 1000
    except Exception: sec = 2.0
    return int(hours * 3600 / max(sec, 0.1))

def bs_rows(chain, addr, since_block):
    rows = []
    base = {"module": "account", "address": addr, "startblock": since_block, "endblock": 99999999, "sort": "asc"}
    tok = bs_compat_all(chain, {**base, "action": "tokentx"})
    txs = bs_compat_all(chain, {**base, "action": "txlist"})
    itx = bs_compat_all(chain, {**base, "action": "txlistinternal"})
    if tok is None or txs is None or itx is None: return None
    for t in tok:
        dec = int(t.get("tokenDecimal") or 18)
        rows.append(dict(chain=chain, tx=t["hash"], block=int(t["blockNumber"]), ts=int(t["timeStamp"]),
                         frm=t["from"].lower(), to=(t.get("to") or "").lower(), token=t.get("tokenSymbol") or "?",
                         contract=t["contractAddress"].lower(), amount=int(t["value"]) / 10 ** dec, native=False))
    for t in txs:
        if str(t.get("isError", "0")) == "1" or str(t.get("txreceipt_status", "1")) == "0": continue
        v = int(t.get("value") or 0); inp = t.get("input") or "0x"
        row = dict(chain=chain, tx=t["hash"], block=int(t["blockNumber"]), ts=int(t["timeStamp"]),
                   frm=t["from"].lower(), to=(t.get("to") or "").lower(), token=CHAINS[chain]["native"],
                   contract="native", amount=v / 1e18, native=True,
                   method=(t.get("functionName") or t.get("methodId") or ""), is_contract_call=inp not in ("0x", ""))
        if v > 0 or row["is_contract_call"]: rows.append(row)
    for t in itx:
        v = int(t.get("value") or 0)
        if v > 0 and str(t.get("isError", "0")) != "1":
            rows.append(dict(chain=chain, tx=t.get("hash") or t.get("transactionHash"), block=int(t["blockNumber"]), ts=int(t["timeStamp"]),
                             frm=t["from"].lower(), to=(t.get("to") or "").lower(), token=CHAINS[chain]["native"],
                             contract="native", amount=v / 1e18, native=True, internal=True))
    senders = {t["hash"]: t["from"].lower() for t in txs}
    for r in rows: r["tx_from"] = senders.get(r["tx"])
    return rows

def bs_is_contract(chain, addr):
    j = bs_v2(chain, f"/addresses/{addr}")
    if isinstance(j, dict) and "is_contract" in j: return bool(j["is_contract"])
    return None

def bs_holdings(chain, addr):
    """{contract: {symbol, amount, contract, price}}。ネイティブは key "native"。
    ※ キーはシンボルでなくコントラクト（同名の偽トークンに本物が上書きされるのを防ぐ）"""
    h = {}
    j = bs_v2(chain, f"/addresses/{addr}")
    if isinstance(j, dict) and j.get("coin_balance") is not None:
        h["native"] = {"symbol": CHAINS[chain]["native"], "amount": int(j["coin_balance"]) / 1e18, "contract": "native",
                       "price": float(j["exchange_rate"]) if j.get("exchange_rate") else None}
    else:
        bal = bs_compat(chain, {"module": "account", "action": "balance", "address": addr, "tag": "latest"})
        if isinstance(bal, str) and bal.isdigit():
            h["native"] = {"symbol": CHAINS[chain]["native"], "amount": int(bal) / 1e18, "contract": "native", "price": None}
    params, pages = {"type": "ERC-20"}, 0
    for _ in range(12):
        j = bs_v2(chain, f"/addresses/{addr}/tokens", params)
        if not isinstance(j, dict) or "items" not in j:
            time.sleep(3); j = bs_v2(chain, f"/addresses/{addr}/tokens", params)      # 1回だけ再試行
            if not isinstance(j, dict) or "items" not in j: break
        pages += 1
        for it in j["items"]:
            tk = it["token"]; dec = int(tk.get("decimals") or 18)
            amt = int(it["value"]) / 10 ** dec; c = (tk.get("address") or tk.get("address_hash") or "").lower()
            if amt > 0 and c:
                h[c] = {"symbol": tk.get("symbol") or "?", "amount": amt, "contract": c,
                        "price": float(tk["exchange_rate"]) if tk.get("exchange_rate") else None}
        if not j.get("next_page_params"): break
        params = {"type": "ERC-20", **j["next_page_params"]}
    else:
        warn(f"{chain} {short(addr)}: トークンが12ページ超、以降は切り捨て")
    if pages == 0:   # v2 が落ちている時は tokentx 全履歴の差引で代用
        warn(f"{chain} v2 tokens 取得不可 → tokentx 差引で代用 {short(addr)}")
        net = defaultdict(float); meta = {}
        for t in bs_compat_all(chain, {"module": "account", "action": "tokentx", "address": addr, "startblock": 0, "endblock": 99999999, "sort": "asc"}) or []:
            dec = int(t.get("tokenDecimal") or 18); k = t["contractAddress"].lower(); v = int(t["value"]) / 10 ** dec
            net[k] += v if t["to"].lower() == addr.lower() else -v; meta[k] = t.get("tokenSymbol") or "?"
        for k, v in net.items():
            if v > 1e-9: h[k] = {"symbol": meta[k], "amount": v, "contract": k, "price": None}
    else:
        try: bs_reconcile_holdings(chain, addr, h)
        except Exception as e: warn(f"{chain} {short(addr)} 残高補完でエラー: {e!r}")
    return h

def rpc_balance_of(chain, contract, addr):
    """ERC-20 balanceOf をチェーンに直接問い合わせる（Blockscout PRO の json-rpc → 公開 RPC の順）。失敗は None"""
    data = "0x70a08231" + addr[2:].rjust(64, "0")
    urls = ([f"{BLOCKSCOUT_PRO}/{CHAINS[chain]['chain_id']}/json-rpc?apikey={BLOCKSCOUT_KEY}"] if BLOCKSCOUT_KEY else []) + CHAINS[chain].get("rpcs", [])
    for url in urls:
        j = http_json("POST", url, retries=1, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": contract, "data": data}, "latest"]})
        res = j.get("result") if isinstance(j, dict) else None
        if isinstance(res, str) and res.startswith("0x") and len(res) > 2:
            try: return int(res, 16)
            except ValueError: pass
    return None

def bs_reconcile_holdings(chain, addr, h, limit=25):
    """Blockscout の残高インデックスが欠落する銘柄（例: Base の O）を、tokentx の差引 → 価格あり → balanceOf で補完"""
    net = defaultdict(float); meta = {}
    for t in bs_compat_all(chain, {"module": "account", "action": "tokentx", "address": addr, "startblock": 0, "endblock": 99999999, "sort": "desc"}) or []:
        dec = int(t.get("tokenDecimal") or 18); k = t["contractAddress"].lower(); v = int(t["value"]) / 10 ** dec
        net[k] += v if t["to"].lower() == addr.lower() else -v; meta[k] = (t.get("tokenSymbol") or "?", dec)
    missing = [k for k, v in net.items() if v > 1e-6 and (k not in h or h[k]["amount"] <= 0)]
    if not missing: return
    prefetch_prices(chain, missing)
    priced = [k for k in missing if _px.get((chain, k))][:limit]     # 価格の付く（=流動性のある）銘柄だけ確認
    for k in priced:
        raw = rpc_balance_of(chain, k, addr)
        if raw:
            sym, dec = meta[k]; h[k] = {"symbol": sym, "amount": raw / 10 ** dec, "contract": k, "price": _px.get((chain, k))}
            log(f"  残高補完 {chain} {short(addr)} {sym} {raw / 10 ** dec:,.4g}（Blockscout 索引に無く balanceOf で確認）")
        time.sleep(0.2)

def bs_has_activity(chain, addr):
    res = bs_compat(chain, {"module": "account", "action": "txlist", "address": addr, "page": 1, "offset": 1, "sort": "desc"})
    if res: return True
    res = bs_compat(chain, {"module": "account", "action": "tokentx", "address": addr, "page": 1, "offset": 1, "sort": "desc"})
    return bool(res)

# ------------------------------------------------------------ NodeReal（BSC）
def nr_rpc(chain, method, params):
    if not NODEREAL_KEY: return None
    j = http_json("POST", CHAINS[chain]["rpc"].format(key=NODEREAL_KEY), json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if not isinstance(j, dict): return None
    if "error" in j: warn(f"{chain} {method}: {j['error']}"); return None
    return j.get("result")

def hx(v):
    """hex / 10進文字列 / 数値 → int"""
    if v is None: return None
    if isinstance(v, (int, float)): return int(v)
    s = str(v).strip()
    return int(s, 16) if s.startswith("0x") else int(float(s))

def nr_ts(v):
    if v is None: return 0
    s = str(v)
    if s.startswith("0x"): return int(s, 16)
    if s.isdigit(): return int(s)
    try: return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception: return 0

_nr_sample_logged = False
def nr_transfers(chain, addr, from_block, to_block):
    """nr_getAssetTransfers を from/to 両方向・100k ブロック窓・pageKey で全件取得。失敗→None"""
    global _nr_sample_logged
    out = []
    for side in ("fromAddress", "toAddress"):
        lo = from_block
        while lo <= to_block:
            hi = min(lo + 99_999, to_block); page_key = None
            while True:
                p = {"category": ["external", "internal", "20"], "fromBlock": hex(lo), "toBlock": hex(hi), "order": "asc", "maxCount": "0x3E8", side: addr}
                if page_key: p["pageKey"] = page_key
                res = nr_rpc(chain, "nr_getAssetTransfers", [p])
                if res is None: return None
                trs = res.get("transfers") or []
                if trs and not _nr_sample_logged: log("NodeReal transfer サンプル:", json.dumps(trs[0])[:400]); _nr_sample_logged = True
                out.extend(trs)
                page_key = res.get("pageKey")
                if not page_key or not trs: break
            lo = hi + 1
    return out

def nr_rows(chain, addr, from_block, to_block):
    trs = nr_transfers(chain, addr, from_block, to_block)
    if trs is None: return None
    rows, seen = [], set()
    for t in trs:
        if str(t.get("receiptsStatus", "1")) == "0": continue
        cat = t.get("category"); frm = (t.get("from") or "").lower(); to = (t.get("to") or "").lower()
        raw = t.get("value"); dec = hx(t.get("decimal")) if t.get("decimal") is not None else 18
        try: amount = hx(raw) / 10 ** dec if isinstance(raw, str) and raw.startswith("0x") else float(raw or 0)
        except Exception: amount = 0.0
        key = (t.get("hash"), cat, frm, to, round(amount, 12))
        if key in seen: continue
        seen.add(key)
        base = dict(chain=chain, tx=t.get("hash"), block=hx(t.get("blockNum")) or 0, ts=nr_ts(t.get("blockTimeStamp") or t.get("blockTimestamp")), frm=frm, to=to, amount=amount)
        if cat == "20":
            rows.append(dict(base, token=t.get("asset") or "?", contract=(t.get("contractAddress") or "").lower(), native=False))
        elif cat == "internal":
            if amount > 0: rows.append(dict(base, token=CHAINS[chain]["native"], contract="native", native=True, internal=True))
        else:
            if amount > 0: rows.append(dict(base, token=CHAINS[chain]["native"], contract="native", native=True))
    # 各 tx の送信者と input を見て「本人発か」「コントラクト呼出か」を補う（新規 tx は少数なので都度取得）
    for tx in {r["tx"] for r in rows if r["tx"]}:
        t = nr_rpc(chain, "eth_getTransactionByHash", [tx])
        if not isinstance(t, dict): continue
        for r in rows:
            if r["tx"] == tx: r["tx_from"] = (t.get("from") or "").lower()
        inp = t.get("input") or "0x"
        if (t.get("from") or "").lower() == addr.lower() and inp not in ("0x", ""):
            blk = hx(t.get("blockNumber")) or 0; ts = next((r["ts"] for r in rows if r["tx"] == tx), 0)
            rows.append(dict(chain=chain, tx=tx, block=blk, ts=ts, frm=addr.lower(), to=(t.get("to") or "").lower(), token=CHAINS[chain]["native"],
                             contract="native", amount=0.0, native=True, is_contract_call=True, method=inp[:10]))
    return rows

def nr_block_number(chain): return hx(nr_rpc(chain, "eth_blockNumber", []))

def nr_lookback_blocks(chain, latest, hours):
    """直近1万ブロックの平均ブロック時間から遡りブロック数を出す"""
    a = nr_rpc(chain, "eth_getBlockByNumber", [hex(latest), False]); b = nr_rpc(chain, "eth_getBlockByNumber", [hex(latest - 10000), False])
    try: sec = (hx(a["timestamp"]) - hx(b["timestamp"])) / 10000
    except Exception: sec = 0.75
    return int(hours * 3600 / max(sec, 0.1))

def nr_is_contract(chain, addr):
    code = nr_rpc(chain, "eth_getCode", [addr, "latest"])
    if code is None: return None
    return code not in ("0x", "")

def nr_holdings(chain, addr):
    h = {}
    bal = nr_rpc(chain, "eth_getBalance", [addr, "latest"])
    if bal is not None: h["native"] = {"symbol": CHAINS[chain]["native"], "amount": hx(bal) / 1e18, "contract": "native", "price": None}
    page = 1
    while page <= 10:
        res = nr_rpc(chain, "nr_getTokenHoldings", [addr, hex(page), "0x64"])
        if not isinstance(res, dict): break
        det = res.get("details") or []
        for d in det:
            dec = hx(d.get("tokenDecimals")) if d.get("tokenDecimals") is not None else 18
            amt = (hx(d.get("tokenBalance")) or 0) / 10 ** dec
            c = (d.get("tokenAddress") or "").lower()
            if amt > 0 and c: h[c] = {"symbol": d.get("tokenSymbol") or "?", "amount": amt, "contract": c, "price": None}
        if len(det) < 100 or page * 100 >= (hx(res.get("totalCount")) or 0): break
        page += 1
    return h

def nr_has_activity(chain, addr):
    latest = nr_block_number(chain)
    if not latest: return None
    lb = nr_lookback_blocks(chain, latest, INITIAL_LOOKBACK_H)
    trs = nr_transfers(chain, addr, max(0, latest - lb), latest)
    return None if trs is None else bool(trs)

# ------------------------------------------------------------ プロバイダ振り分け
def provider(chain): return CHAINS[chain]["provider"]
def chain_available(chain): return provider(chain) != "nodereal" or bool(NODEREAL_KEY)

def fetch_rows(chain, addr):
    """新規行を取得し (rows, new_cursor) を返す。失敗なら (None, None) でカーソルは進めない"""
    key = f"{chain}:{addr}"
    if provider(chain) == "blockscout":
        if key in cursor: since = cursor[key] + 1
        else:
            latest = bs_latest_block(chain)
            if not latest: return None, None
            since = max(0, latest - bs_lookback_blocks(chain, INITIAL_LOOKBACK_H))
        rows = bs_rows(chain, addr, since)
        if rows is None: return None, None
        return rows, (max(r["block"] for r in rows) if rows else cursor.get(key, since - 1))
    latest = nr_block_number(chain)
    if not latest: return None, None
    head = latest - NR_HEAD_MARGIN          # NodeReal のインデクサは先頭から数ブロック遅れる（"blockNum not reached" 対策）
    if key in cursor: lo = cursor[key] + 1
    else: lo = max(0, head - nr_lookback_blocks(chain, latest, INITIAL_LOOKBACK_H))
    if lo > head: return [], cursor.get(key)
    rows = nr_rows(chain, addr, lo, head)
    if rows is None: return None, None
    return rows, head

def is_contract(chain, addr):
    for _ in range(2):
        r = bs_is_contract(chain, addr) if provider(chain) == "blockscout" else nr_is_contract(chain, addr)
        if r is not None: return r
        time.sleep(2)
    return None

def fetch_holdings(chain, addr): return bs_holdings(chain, addr) if provider(chain) == "blockscout" else nr_holdings(chain, addr)
def has_activity(chain, addr): return bs_has_activity(chain, addr) if provider(chain) == "blockscout" else nr_has_activity(chain, addr)

# ------------------------------------------------------------ 価格
_px = {}                                                   # (chain, contract) -> price or None（この実行内のキャッシュ）
_liq = {}                                                  # (chain, contract) -> DexScreener 流動性 USD
CTX["quote"] = lambda ch, c: (_px.get((ch, (c or "").lower())), _liq.get((ch, (c or "").lower())))
price_cache = jload(DATA / "prices.json", {})              # "chain:contract" -> {"px": .., "ts": ..}（前回価格。API 不調時の保険）

def _pair_price(p):
    liq = float((p.get("liquidity") or {}).get("usd") or 0)
    return (liq, float(p["priceUsd"])) if liq >= MIN_LIQ_USD and p.get("priceUsd") else (liq, None)

def _set_px(chain, contract, px, fetched=True):
    key = (chain, contract)
    if px is None and fetched is False:                  # 取得失敗 → 前回価格で代用
        old = price_cache.get(f"{chain}:{contract}")
        if old and old.get("px") and time.time() - old.get("ts", 0) < PRICE_CACHE_H * 3600: px = old["px"]
    _px[key] = px
    if px is not None and fetched: price_cache[f"{chain}:{contract}"] = {"px": px, "ts": int(time.time())}

def prefetch_prices(chain, contracts):
    """DexScreener の tokens/v1 は 30 アドレスまで一括可。呼び出し回数を減らすため先にまとめて取る"""
    need = sorted({(c or "").lower() for c in contracts if c and c != "native" and (chain, (c or "").lower()) not in _px})
    for i in range(0, len(need), 30):
        chunk = need[i:i + 30]; cs = set(chunk)
        j = http_json("GET", f"https://api.dexscreener.com/tokens/v1/{CHAINS[chain]['dex']}/{','.join(chunk)}", retries=3)
        if not isinstance(j, list):
            for c in chunk: _set_px(chain, c, None, fetched=False)
            continue
        best = {}
        for p in j:
            addr = ((p.get("baseToken") or {}).get("address") or "").lower()
            if addr in cs:
                liq, px = _pair_price(p)
                if addr not in best or liq > best[addr][0]: best[addr] = (liq, px)
        for c in chunk: _set_px(chain, c, best.get(c, (0, None))[1]); _liq[(chain, c)] = best.get(c, (0, None))[0]
        time.sleep(0.3)

def price(chain, symbol, contract):
    mp = CFG.get("manual_prices", {})
    if symbol in mp: return float(mp[symbol])
    if symbol in STABLES and (chain, (contract or "").lower()) in KNOWN_STABLES: return 1.0
    c = CHAINS[chain]
    if contract == "native":
        if c.get("wnative"): chain, contract = chain, c["wnative"]
        elif c.get("native_ref"): chain, contract = c["native_ref"]
        else: return None
    if not contract: return None
    contract = contract.lower(); key = (chain, contract)
    if key in _px: return _px[key]
    j = http_json("GET", f"https://api.dexscreener.com/tokens/v1/{CHAINS[chain]['dex']}/{contract}", retries=2)
    if not isinstance(j, list): _set_px(chain, contract, None, fetched=False); return _px[key]
    px = None
    try:
        if j: liq, px = _pair_price(max(j, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))); _liq[key] = liq
    except Exception: px = None
    _set_px(chain, contract, px)
    return px

# ------------------------------------------------------------ 分類
def classify_tx(chain, owner, trs):
    outs = [r for r in trs if r["frm"] == owner and r["amount"] > 0]
    ins = [r for r in trs if r["to"] == owner and r["amount"] > 0]
    # 本人が署名していない tx でトークンだけが「出ていく」＝スキャムトークンの偽 Transfer（アドレスポイズニング）
    if outs and not ins and all(not r["native"] and r.get("tx_from") != owner for r in outs):
        cp = next((r["to"] for r in outs), "")
        return "なりすまし(偽送金)", [], [], cp
    if outs and all(is_burn(r["to"]) for r in outs) and not ins:
        return "バーン", outs, [], outs[0]["to"]
    tok_out = [r for r in outs if not r["native"]]; tok_in = [r for r in ins if not r["native"]]
    nat_out = [r for r in outs if r["native"]]; nat_in = [r for r in ins if r["native"]]
    token_contracts = {r["contract"] for r in tok_out + tok_in}
    # 単なる transfer() はトークンコントラクト自身への呼出なので「スワップ等の呼出」から除く
    calls = [r for r in trs if r.get("is_contract_call") and r["to"] not in token_contracts]
    cps = {(r["to"] if r["frm"] == owner else r["frm"]) for r in outs + ins if (r["to"] if r["frm"] == owner else r["frm"]) != owner}
    cp = max(cps, key=lambda a: (is_watched(a), is_service(a))) if cps else (calls[0]["to"] if calls else "")
    if tok_out and (nat_in or tok_in):
        kind = "売却(スワップ)" if not tok_in or any(r["token"] in STABLES for r in tok_in) else "スワップ"
    elif tok_in and (nat_out or any(r["token"] in STABLES for r in tok_out)):
        kind = "購入(スワップ)"
    elif tok_out and calls and not is_watched(cp):
        kind = "売却(スワップ・代金不明)"
    elif tok_out or nat_out:
        if is_watched(cp): kind = "内部移動"
        elif is_service(cp): kind = "サービスへ送金"
        else: kind = "外部へ送金"
    elif tok_in or nat_in:
        if is_watched(cp): kind = "内部移動"
        elif is_service(cp): kind = "サービスから受取"
        else: kind = "受取"
    else:
        kind = "コントラクト呼出"
    return kind, outs, ins, cp

def resolve_purchases(evs):
    """サービスからの受取に対し、全チェーン横断で「同じ相手へ原資を払ったか」を後付けで判定"""
    pays = [e for e in evs if e["dir"] == "OUT" and (e["token"] in STABLES or e["contract"] == "native") and is_service(e["cp"])]
    for e in evs:
        if e["kind"] in ("サービスから受取", "受取(原資未確認)", "購入(クロスチェーン)"):
            paid = [p for p in pays if p["cp"] == e["cp"] and 0 <= e["ts"] - p["ts"] <= BUY_MATCH_HOURS * 3600]
            if paid:
                e["kind"] = "購入(クロスチェーン)"; e["funding_usd"] = sum(p["usd"] or 0 for p in paid)
                e["funding_note"] = "、".join(f"{p['time'][5:16]} {p['amount']:.4g} {p['token']}" for p in paid)
            else:
                e["kind"] = "受取(原資未確認)"

# ------------------------------------------------------------ 子ウォレット登録
def register_child(chain, parent, cp, first_seen, queue):
    wallets[cp] = {"role": child_role(wallets[parent]["role"]), "parent": parent, "first_seen": first_seen, "chains": [chain]}
    queue.append(cp); log(f"  🆕 監視追加 {wallets[cp]['role']} {cp} (親 {short(parent)}, {chain})")

def try_register(chain, parent, cp, ev, queue):
    """未知アドレスが EOA なら子として登録。判定不能なら pending に積んで次回再判定"""
    r = is_contract(chain, cp)
    if r is False: register_child(chain, parent, cp, ev["time"], queue); return True
    if r is None and not any(p["addr"] == cp for p in pending_eoa):
        pending_eoa.append({"chain": chain, "addr": cp, "parent": parent, "first_seen": ev["time"]}); warn(f"EOA判定不能 → 保留 {cp}")
    return False

# ------------------------------------------------------------ メイン処理
def run():
    new_events = []; now_ts = time.time()
    state["run_count"] = state.get("run_count", 0) + 1
    # 初回チェーン探索（NodeReal キー無しなら BSC 抜きで探索し、結果は保存しない）
    active = list(CFG.get("chains") or [])
    if not active:
        for ch in CHAINS:
            if not chain_available(ch): warn(f"{ch}: キー未設定のため探索スキップ"); continue
            if any(has_activity(ch, a) for a in CFG["main_wallets"]): active.append(ch)
        log("活動のあるチェーン:", active)
        if all(chain_available(ch) for ch in CHAINS):
            CFG["chains"] = active
            if not DRY_RUN: json.dump(CFG, open(ROOT / "config.json", "w"), indent=2, ensure_ascii=False)
    active = [ch for ch in active if chain_available(ch)]
    # 保留中の EOA 判定を再試行
    queue = list(wallets.keys())
    for p in list(pending_eoa):
        if p["addr"] in wallets: pending_eoa.remove(p); continue
        r = is_contract(p["chain"], p["addr"])
        if r is False: register_child(p["chain"], p["parent"], p["addr"], p["first_seen"], queue); pending_eoa.remove(p)
        elif r is True: pending_eoa.remove(p)
    processed = set()
    poll = [ch for ch in active if ch in PRIMARY_CHAINS or state["run_count"] % SECONDARY_EVERY == 0]
    log("今回取得するチェーン:", poll)
    while queue:
        w = queue.pop(0)
        if w in processed: continue
        processed.add(w)
        for ch in poll:
            key = f"{ch}:{w}"
            if over_budget(): warn(f"時間予算 {RUN_BUDGET_S/60:.0f} 分超過 → {ch} {short(w)} 以降は次回に持ち越し"); continue
            rows, new_cur = fetch_rows(ch, w)
            if rows is None: warn(f"{ch} {short(w)}: 取得失敗（次回に持ち越し）"); continue
            if rows and ch not in wallets[w]["chains"]: wallets[w]["chains"].append(ch)
            prefetch_prices(ch, [r["contract"] for r in rows if not r["native"]] + ([CHAINS[ch]["wnative"]] if CHAINS[ch].get("wnative") else []))
            by_tx = defaultdict(list)
            for r in rows: by_tx[r["tx"]].append(r)
            n_new = 0
            for tx, trs in sorted(by_tx.items(), key=lambda kv: kv[1][0]["ts"]):
                if f"{ch}:{tx}:{w}" in seen_tx: continue
                seen_tx.add(f"{ch}:{tx}:{w}"); n_new += 1
                kind, outs, ins, cp = classify_tx(ch, w, trs)
                for r in outs + ins:
                    px = price(ch, r["token"], r["contract"]); usd = r["amount"] * px if px else None
                    d = "OUT" if r in outs else "IN"
                    ev = dict(time=datetime.fromtimestamp(r["ts"], timezone.utc).isoformat(), ts=r["ts"], chain=ch, wallet=w, tx=tx,
                              kind=kind, dir=d, token=r["token"], contract=r["contract"], amount=r["amount"], price=px, usd=usd,
                              cp=cp, cp_label=label(cp) if cp else "")
                    if d == "IN" and usd is not None and usd < DUST_USD and not is_watched(cp):
                        ev["kind"] = "ダスト"
                    if d == "IN" and not r["native"] and not is_watched(cp) and not is_service(cp) and (not r["token"].isascii() or usd is None and r["token"].upper() in ("BNB", "ETH", "WBNB", "WETH", "USDT", "USDC")):
                        ev["kind"] = "ダスト"     # 偽ネイティブ/偽ステーブルのばら撒き（アドレスポイズニング）
                    if d == "IN" and is_lookalike(cp): ev["kind"] = "ダスト"; ev["cp_label"] = f"なりすまし {short(cp)}"
                    # 新ウォレット検出: 未知EOAへのガス種銭 or 単純トークン送金（スワップは分類段階で除外済み）
                    if ev["kind"] == "外部へ送金" and cp and d == "OUT" and (now_ts - r["ts"]) < CHILD_MAX_AGE_D * 86400 and not is_burn(cp) and not is_lookalike(cp):
                        seed = r["native"] and (usd or 0) < GAS_SEED_USD
                        if seed or not r["native"]:
                            if try_register(ch, w, cp, ev, queue):
                                ev["kind"] = "新ウォレット開設(ガス種銭)" if seed else "内部移動"; ev["cp_label"] = label(cp)
                    new_events.append(ev)
            if new_cur is not None: cursor[key] = new_cur
            log(f"{ch:9s} {label(w):22s} 行 {len(rows):4d} / 新規tx {n_new}")
            time.sleep(0.3)
    events.extend(new_events)
    resolve_purchases(events)
    # 残高（HOLDINGS_EVERY 回に1回、または初回・新規ウォレット追加時）
    prev = jload(DATA / "holdings.json", {"updated": None, "holdings": {}})
    need = state["run_count"] % HOLDINGS_EVERY == 1 or not prev["holdings"] or prev.get("version") != 2 or any(f"{ch}:{w}" not in prev["holdings"] for w in wallets for ch in active if wallets[w]["chains"] or wallets[w]["role"] == "本体")
    nowdt = datetime.now(timezone.utc); today = nowdt.strftime("%Y-%m-%d")
    state["daily_due"] = state.get("last_daily") != today and nowdt.hour >= DAILY_HOUR_UTC
    if state["daily_due"]: need = True                                   # 日次レポートの時点は必ず実残高で
    if need and over_budget(): warn("時間予算超過 → 残高更新は次回"); need = False
    if need:
        holdings = {}
        for w in wallets:
            for ch in active:
                if ch not in wallets[w]["chains"] and wallets[w]["role"] != "本体": continue
                h = fetch_holdings(ch, w)
                prefetch_prices(ch, [v["contract"] for v in h.values() if not v.get("price")])
                for s, v in h.items():
                    v["price"] = v["price"] or price(ch, v["symbol"], v["contract"]); v["usd"] = v["amount"] * v["price"] if v["price"] else None
                holdings[f"{ch}:{w}"] = h
                time.sleep(0.3)
        holdings_doc = {"version": 2, "updated": datetime.now(timezone.utc).isoformat(), "holdings": holdings}
    else:
        holdings_doc = prev; holdings = prev["holdings"]
    state["holdings_refreshed"] = bool(need)
    if need or (not snapshots and holdings_doc.get("holdings")):
        snapshots.append(P.build_snapshot(holdings_doc, CTX)); snapshots[:] = P.prune_snapshots(snapshots)
    # 保存
    if not DRY_RUN:
        json.dump(wallets, open(DATA / "wallets.json", "w"), indent=2, ensure_ascii=False)
        json.dump(sorted(seen_tx), open(DATA / "seen_tx.json", "w"))
        json.dump(cursor, open(DATA / "cursor.json", "w"), indent=2)
        json.dump(state, open(DATA / "state.json", "w"), indent=2)
        json.dump(pending_eoa, open(DATA / "pending_eoa.json", "w"), indent=2)
        with open(DATA / "events.jsonl", "w") as f:
            for e in sorted(events, key=lambda e: e["ts"]): f.write(json.dumps(e, ensure_ascii=False) + "\n")
        json.dump(holdings_doc, open(DATA / "holdings.json", "w"), indent=2, ensure_ascii=False)
        json.dump(price_cache, open(DATA / "prices.json", "w"))
        P.save_snapshots(DATA / "snapshots.jsonl", snapshots)
    return new_events, holdings_doc

# ------------------------------------------------------------ 集計（バッチ・比率）
def batches(evs, gap=1800):
    out = []
    sells = sorted([e for e in evs if e["kind"].startswith("売却") and e["dir"] == "OUT"], key=lambda e: e["ts"])
    groups = defaultdict(list)
    for e in sells: groups[(e["chain"], e["wallet"], e["contract"])].append(e)
    for (ch, w, c), g in groups.items():
        cur = []
        for e in g:
            if cur and e["ts"] - cur[-1]["ts"] > gap: out.append(cur); cur = []
            cur.append(e)
        if cur: out.append(cur)
    return [dict(chain=b[0]["chain"], wallet=b[0]["wallet"], token=b[0]["token"], contract=b[0]["contract"], n=len(b), amount=sum(e["amount"] for e in b),
                 usd=sum(e["usd"] or 0 for e in b), start=b[0]["time"], end=b[-1]["time"]) for b in out]

def main_holding_of(contract, holdings):
    """本体ウォレットの当該コントラクト（native 含む）の保有枚数（全チェーン合算）"""
    tot = 0.0
    for k, h in holdings.items():
        w = k.split(":")[1]
        if wallets.get(w, {}).get("role") == "本体" and contract in h: tot += h[contract]["amount"]
    return tot

# ------------------------------------------------------------ Telegram
def tg(text):
    if DRY_RUN: log("[DRY_RUN] Telegram:\n" + text); return
    if not (TG_TOKEN and TG_CHAT): warn("TG_TOKEN/TG_CHAT 未設定のため通知スキップ"); return
    for i in range(0, len(text), 3800):
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": TG_CHAT, "text": text[i:i + 3800], "disable_web_page_preview": True}, timeout=20)
        if not r.ok: warn("Telegram 送信失敗:", r.status_code, r.text[:200])

def notify(new_events, holdings):
    """(1) 買い ≥ BUY_ALERT_USD と新ウォレット検出は即時
       (2) 残高更新した回（1時間ごと）は前回時点との差を「まとめ」で（動きがあった時だけ）
       (3) 日次（DAILY_HOUR_UTC）は前日 9:00 JST 比の分解を必ず送る"""
    names = P.role_names(wallets); pages = CFG.get("pages_url", "")
    cutoff = time.time() - NOTIFY_MAX_AGE_H * 3600
    recent = [e for e in new_events if e["ts"] >= cutoff]
    lines = []
    buys = P.group_trades(recent, "buy", CTX, BUY_ALERT_USD)
    if buys: lines += P.buy_alert_lines(buys, names, CTX)
    for e in recent:
        if e["kind"] == "新ウォレット開設(ガス種銭)":
            lines.append(f"🆕 新ウォレット {names.get(e['cp'], '?')} を検出（{names.get(e['wallet'], '?')} から種銭 {e['amount']:.4f} {e['token']}）→ 監視に追加")
    if lines:
        if pages and "<" not in pages: lines.append(f"詳細: {pages}")
        tg("\n".join(lines))
    else:
        log("即時通知なし")
    # 日次
    if state.get("daily_due"):
        pts = P.cutoff_points(snapshots)
        if len(pts) >= 2:
            br = P.bridge(pts[-2], pts[-1], events, CTX, MOVE_MIN_USD)
            body = P.digest_text(br, names, CTX, f"📊 日次レポート {P.jst(pts[-1]['ts']).strftime('%m/%d %H:%M')} JST（{pts[-2]['label']} → {pts[-1]['label']}）", "")
            npt = P.new_positions_text(P.new_positions(events, snapshots, CTX, days=int(CFG.get("new_positions_days", 14)), min_cost=float(CFG.get("buy_list_min_usd", 1000))), names)
            tg(body + ("\n" + npt if npt else "") + (f"\n詳細: {pages}" if pages and "<" not in pages else ""))
        else:
            tg(f"📊 日次レポート: 記録開始。明日 9:00 JST から前日比（値動き / 利確 / 買い）を送ります。現在の総資産 {P.fmt_usd(snapshots[-1]['total']) if snapshots else '—'}")
        state["last_daily"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return
    # 1時間まとめ（残高を更新した回だけ、動きがあった時だけ）
    if state.get("holdings_refreshed") and len(snapshots) >= 2:
        p0 = {"label": P.jst(snapshots[-2]["ts"]).strftime("%H:%M"), "snap": snapshots[-2]}
        p1 = {"label": P.jst(snapshots[-1]["ts"]).strftime("%H:%M"), "snap": snapshots[-1]}
        br = P.bridge(p0, p1, events, CTX, MOVE_MIN_USD)
        if P.notable(br, MOVE_MIN_USD, PRICE_ALERT_PCT):
            tg(P.digest_text(br, names, CTX, f"🕐 まとめ {p0['label']}→{p1['label']} JST", pages))
        else:
            log(f"1時間まとめ: 動きなし（利確 {br['realized']:.0f} / 買い {br['buys']:.0f} / 値動き {br['price']:.0f}）")

# ------------------------------------------------------------ HTML
def html(holdings_doc):
    from html_report import render
    hd = holdings_doc["holdings"] if isinstance(holdings_doc, dict) and "holdings" in holdings_doc else holdings_doc
    upd = holdings_doc.get("updated") if isinstance(holdings_doc, dict) else None
    out = DOCS / ("index.dryrun.html" if DRY_RUN else "index.html")
    pts = P.cutoff_points(snapshots, n=45)
    bridges = [P.bridge(pts[i - 1], pts[i], events, CTX, MOVE_MIN_USD) for i in range(1, len(pts))]
    newpos = P.new_positions(events, snapshots, CTX, days=int(CFG.get("new_positions_days", 14)), min_cost=float(CFG.get("buy_list_min_usd", 1000)))
    for g in newpos:   # 現在価格が無い銘柄は DexScreener で補う
        if not g["px"]:
            g["px"] = price(g["chain"], g["sym"], g["contract"]); g["liq"] = _liq.get((g["chain"], g["contract"]))
            if g["px"]: g["pnl_pct"] = (g["px"] / g["avg"] - 1) * 100; g["value"] = g["held"] * g["px"]
    matrix = P.token_matrix(snapshots, CTX, top_n=int(CFG.get("matrix_top_n", 20)), extra_keys=[g["key"] for g in newpos])
    timeline = {"points": pts, "bridges": bridges, "names": P.role_names(wallets), "ctx": CTX, "min_usd": MOVE_MIN_USD, "buy_list_min_usd": float(CFG.get("buy_list_min_usd", 1000)), "new_positions": newpos, "matrix": matrix}
    render(CFG, CHAINS, wallets, events, hd, batches(events), THRESHOLD, label, out, holdings_updated=upd, timeline=timeline)
    log("HTML 生成:", out)

if __name__ == "__main__":
    if not NODEREAL_KEY: warn("NODEREAL_KEY 未設定（BSC は取得できません）")
    if not BLOCKSCOUT_KEY: warn("BLOCKSCOUT_KEY 未設定（公開インスタンスの API を使うため 429 が出やすくなります）")
    if DRY_RUN: log("DRY_RUN: 通知・保存なし")
    new_events, holdings_doc = run()
    log(f"新イベント {len(new_events)} 件 / 監視ウォレット {len(wallets)} / 保留EOA {len(pending_eoa)} / 所要 {(time.time()-T0)/60:.1f} 分")
    log("API呼び出し数: " + ", ".join(f"{h} {n}" for h, n in sorted(API_CALLS.items(), key=lambda kv: -kv[1])))
    if DRY_RUN:
        for e in sorted(new_events, key=lambda e: e["ts"])[-40:]:
            log(f"  {e['time'][:16]} {e['chain']:9s} {label(e['wallet']):14s} {e['kind']:14s} {e['dir']} {e['token']:12s} {e['amount']:>14.6g} {('$%.0f' % e['usd']) if e['usd'] is not None else '$?':>9s} ← {e['cp_label'][:40]}")
    try:
        notify(new_events, holdings_doc["holdings"])
    finally:
        if not DRY_RUN:   # notify() が更新する last_daily を必ず保存（run() の保存は notify 前なので）
            for k in ("daily_due", "holdings_refreshed"): state.pop(k, None)
            json.dump(state, open(DATA / "state.json", "w"), indent=2)
    html(holdings_doc)
