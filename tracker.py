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

CODE_DIR = Path(__file__).resolve().parent
ROOT = Path(os.getenv("WHALE_ROOT") or CODE_DIR).resolve()   # 設定・データ・docs の置き場（複数クジラ運用ではデータ用リポジトリ）
DATA, DOCS = ROOT / "data", ROOT / "docs"
def L(a):
    """アドレス正規化: EVM(0x…)は小文字、Solana(base58)は大文字小文字を保持"""
    a = a or ""
    return a.lower() if a.startswith("0x") else a

DATA.mkdir(exist_ok=True); DOCS.mkdir(exist_ok=True)

CFG = json.load(open(ROOT / "config.json"))
NODEREAL_KEY = os.getenv("NODEREAL_KEY", "")
HELIUS_KEY = os.getenv("HELIUS_KEY", "")           # Solana 用（https://dashboard.helius.dev の無料キー）
ALCHEMY_BNB_KEY = os.getenv("ALCHEMY_BNB_KEY", "")  # BSC 用の代替（https://dashboard.alchemy.com、BNB Smart Chain の App キー）。NodeReal が無い時に使う
WHALE_NAME = os.getenv("WHALE_NAME", "")           # 複数クジラ運用時の名前（config.json の name でも可）
BLOCKSCOUT_KEY = os.getenv("BLOCKSCOUT_KEY", "")   # 推奨。dev.blockscout.com の無料 PRO キー（proapi_…）。無いと各インスタンスの公開APIを叩き 429 になりやすい
BLOCKSCOUT_PRO = "https://api.blockscout.com"
TG_TOKEN, TG_CHAT = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
DRY_RUN = os.getenv("DRY_RUN", "") not in ("", "0")

WETH_ETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
CHAINS = {
    "robinhood": {"name": "Robinhood Chain", "provider": "blockscout", "base": "https://robinhoodchain.blockscout.com", "chain_id": 4663, "rpcs": [],
                  "native": "ETH", "dex": "robinhood", "wnative": "", "native_ref": ("ethereum", WETH_ETH)},
    "bsc":       {"name": "BSC", "provider": "nodereal", "rpc": "https://bsc-mainnet.nodereal.io/v1/{key}", "alchemy": "https://bnb-mainnet.g.alchemy.com/v2/{key}",
                  "native": "BNB", "dex": "bsc", "wnative": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"},
    "ethereum":  {"name": "Ethereum", "provider": "blockscout", "base": "https://eth.blockscout.com", "chain_id": 1, "rpcs": ["https://eth.llamarpc.com", "https://cloudflare-eth.com"],
                  "native": "ETH", "dex": "ethereum", "wnative": WETH_ETH},
    "base":      {"name": "Base", "provider": "blockscout", "base": "https://base.blockscout.com", "chain_id": 8453, "rpcs": ["https://mainnet.base.org", "https://base.llamarpc.com"],
                  "native": "ETH", "dex": "base", "wnative": "0x4200000000000000000000000000000000000006"},
    "arbitrum":  {"name": "Arbitrum", "provider": "blockscout", "base": "https://arbitrum.blockscout.com", "chain_id": 42161, "rpcs": ["https://arb1.arbitrum.io/rpc"],
                  "native": "ETH", "dex": "arbitrum", "wnative": "0x82af49447d8a07e3bd95bd0d56f35241523fbbe2"},
    "solana":    {"name": "Solana", "provider": "helius", "rpc": "https://mainnet.helius-rpc.com/?api-key={key}", "api": "https://api.helius.xyz/v0",
                  "native": "SOL", "dex": "solana", "wnative": "So11111111111111111111111111111111111111112"},
}
SOL_SYSTEM = "11111111111111111111111111111111"
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
    ("solana", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"), ("solana", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"),
}
MIN_LIQ_USD = 5000     # DexScreener の流動性がこれ未満のペアの価格は使わない（偽トークン・ゴミ価格対策）
BUY_ALERT_USD = float(CFG.get("buy_alert_usd", 5000))        # 買いはこの額から即時 Telegram（実行をまたぐ分割買いの累計でも可）
BUY_LIST_MIN_USD = float(CFG.get("buy_list_min_usd", 5000))  # 台帳の買い一覧・新規銘柄の成績に載せる最小額（$1,000 は低すぎる → $5,000。2026-09-08）
BUY_ACCUM_H = float(CFG.get("buy_accum_hours", 24))          # 分割買いを累計する時間窓
MOVE_MIN_USD = float(CFG.get("move_min_usd", 5000))          # 一覧・まとめに載せる最小額
PRICE_ALERT_PCT = float(CFG.get("price_move_alert_pct", 5))  # 1時間でリスク資産がこの%動いたらまとめを送る
DAILY_HOUR_UTC = int(CFG.get("daily_report_hour_utc", 15))   # 1日の締め＝日次レポート時刻（15 UTC = 24:00 JST。1日1通、1日の終わりに）
SELL_ALERT_USD = float(CFG.get("sell_alert_usd", 100000))    # 目立つ売り・外部流出はこの額から即時 Telegram
DAILY_BIG_USD = float(CFG.get("daily_big_move_usd", 100000))  # 日次レポートに載せる「大きな動き」（買い / 売り / 値動き）の最小額
P.CUT_OFF = DAILY_HOUR_UTC * 3600                             # portfolio の時点計算（締め時刻）と共有
PEER_REPOS = CFG.get("peer_repos", ["fswshun/whale-tracker", "fswshun/whale-unipcs", "fswshun/whale-avast", "fswshun/whale-kyle"])   # 同時買い検出の相手
CROSS_MIN_USD = float(CFG.get("cross_buy_min_usd", 5000))    # 同時買いに数える最小額（1人あたり）
NEW_TOKEN_H = float(CFG.get("new_token_hours", 24))            # 上場からこの時間以内の銘柄を執行サービス経由で少額受け取った場合は「新規トークン受取」（エアドロップ疑い）
NEW_TOKEN_MAX_USD = float(CFG.get("new_token_max_usd", 20000))
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
PRIMARY_CHAINS = set(CFG.get("primary_chains", ["robinhood", "bsc", "solana"]))    # 毎回取得するチェーン
WHALE_NAME = WHALE_NAME or CFG.get("name", "") or "Nachi"   # 名前の無いリポ（第1クジラ）は「Nachi」（2026-09-11 藤沼さん指定）。通知の頭に必ず付く
NOTIFY_MAX_AGE_H = float(CFG.get("notify_max_age_hours", 48))        # これより古いイベントは通知しない（初回バックフィル対策）
CHILD_MAX_AGE_D = float(CFG.get("child_detect_max_age_days", 30))    # これより古い送金からは子ウォレットを起こさない
MAX_WALLETS = int(CFG.get("max_wallets", 40))                        # クラスターの上限（超えたら自動追加を止めて警告）
MAX_CHILDREN_PER_RUN = int(CFG.get("max_children_per_wallet_per_run", 8))   # 1回の実行で1つの親から起こす子の上限（分配ボット対策）
MIN_SEED_NATIVE = {"solana": float(CFG.get("solana_min_seed_sol", 0.2)), "default": 0.0}   # 種銭と見なす最小ネイティブ量（Solana はボットの少額撒きを除外）
SOLANA_MAX_DEPTH = int(CFG.get("solana_max_depth", 1))                # Solana で子ウォレットを追う深さ（1=本体の直接の子まで）
SERVICE_PREFIX = re.compile(r"^0x00aa", re.I)
BLOCKSCOUT_PAGE = 10000
HOLDINGS_MAX_PAGES = int(CFG.get("holdings_max_pages", 25))   # 残高一覧のページ上限（スパムトークンが多い口座で本物が切り捨てられるのを防ぐ）
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
snapshots, _artifacts = P.drop_artifact_snapshots(snapshots)   # 索引障害でポジションが一時的に消えた時点を除去（前後に同枚数で存在するもの）
token_meta = jload(DATA / "token_meta.json", {})          # "solana:mint" -> {symbol, decimals}（Solana はイベントにシンボルが無いため）
labels = {L(k): v for k, v in CFG.get("labels", {}).items()}
SERVICES = {k: v for k, v in jload(CODE_DIR / "services.json", {}).items() if not k.startswith("_")}   # 共有サービス/ボット（コードリポで一元管理）
labels.update({L(k): v for k, v in SERVICES.items()})

for a in CFG["main_wallets"]:
    wallets.setdefault(L(a), {"role": "本体", "parent": None, "first_seen": None, "chains": []})

def is_watched(a): return bool(a) and L(a) in wallets
def is_service(a): return bool(a) and (L(a) in labels or bool(SERVICE_PREFIX.match(a)))
def short(a): return a[:6] + "…" + a[-4:] if a else ""
def label(a):
    a = L(a or "")
    if a in labels: return labels[a]
    if a in wallets: return f"{wallets[a]['role']} {short(a)}"
    if SERVICE_PREFIX.match(a): return f"0x00AA系サービス {short(a)}"
    return short(a)
def child_role(parent_role): return {"本体": "子", "子": "孫", "孫": "曾孫"}.get(parent_role, "子孫")
BURN = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000001",
        "0x00000000000000000000000000000000deadbeef", "0xdead000000000000000042069420694206942069"}
def is_burn(a): return L(a or "") in BURN
def is_lookalike(a):
    """監視ウォレットと先頭6桁・末尾3桁が同じ別アドレス＝アドレスポイズニングの偽物"""
    a = L(a or "")
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

def bs_compat_all(chain, params, info=None):
    """互換APIのページング（offset 上限 10000 × 最大 5 ページ）。
       全件取れなかった場合は info["truncated"]=True を立てる（打ち切られた履歴から残高を再構成しないため）"""
    out = []
    for page in range(1, 6):
        res = bs_compat(chain, {**params, "page": page, "offset": BLOCKSCOUT_PAGE})
        if res is None:
            if page > 1:
                warn(f"{chain} {params.get('action')}: {page}ページ目が取れないため {len(out)} 行で打ち切り")   # PRO API は page×offset ≤ 10000
                if info is not None: info["truncated"] = True
                return out
            return None
        out.extend(res)
        if len(res) < BLOCKSCOUT_PAGE: return out
    if info is not None: info["truncated"] = True      # 5ページ全部埋まった＝まだ続きがある
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
                         frm=L(t["from"]), to=L(t.get("to") or ""), token=t.get("tokenSymbol") or "?",
                         contract=L(t["contractAddress"]), amount=int(t["value"]) / 10 ** dec, native=False))
    for t in txs:
        if str(t.get("isError", "0")) == "1" or str(t.get("txreceipt_status", "1")) == "0": continue
        v = int(t.get("value") or 0); inp = t.get("input") or "0x"
        row = dict(chain=chain, tx=t["hash"], block=int(t["blockNumber"]), ts=int(t["timeStamp"]),
                   frm=L(t["from"]), to=L(t.get("to") or ""), token=CHAINS[chain]["native"],
                   contract="native", amount=v / 1e18, native=True,
                   method=(t.get("functionName") or t.get("methodId") or ""), is_contract_call=inp not in ("0x", ""))
        if v > 0 or row["is_contract_call"]: rows.append(row)
    for t in itx:
        v = int(t.get("value") or 0)
        if v > 0 and str(t.get("isError", "0")) != "1":
            rows.append(dict(chain=chain, tx=t.get("hash") or t.get("transactionHash"), block=int(t["blockNumber"]), ts=int(t["timeStamp"]),
                             frm=L(t["from"]), to=L(t.get("to") or ""), token=CHAINS[chain]["native"],
                             contract="native", amount=v / 1e18, native=True, internal=True))
    senders = {t["hash"]: L(t["from"]) for t in txs}
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
    for _ in range(HOLDINGS_MAX_PAGES):
        j = bs_v2(chain, f"/addresses/{addr}/tokens", params)
        if not isinstance(j, dict) or "items" not in j:
            time.sleep(3); j = bs_v2(chain, f"/addresses/{addr}/tokens", params)      # 1回だけ再試行
            if not isinstance(j, dict) or "items" not in j: break
        pages += 1
        for it in j["items"]:
            tk = it["token"]; dec = int(tk.get("decimals") or 18)
            amt = int(it["value"]) / 10 ** dec; c = L(tk.get("address") or tk.get("address_hash") or "")
            if amt > 0 and c:
                h[c] = {"symbol": tk.get("symbol") or "?", "amount": amt, "contract": c,
                        "price": float(tk["exchange_rate"]) if tk.get("exchange_rate") else None}
        if not j.get("next_page_params"): break
        params = {"type": "ERC-20", **j["next_page_params"]}
    else:
        warn(f"{chain} {short(addr)}: トークンが {HOLDINGS_MAX_PAGES} ページ超、以降は切り捨て（config の holdings_max_pages）")
    if pages == 0:   # v2 が落ちている時は tokentx 全履歴の差引で代用（履歴が全件取れる小口ウォレットのみ）
        warn(f"{chain} v2 tokens 取得不可 → tokentx 差引で代用 {short(addr)}")
        info = {}
        rows = bs_compat_all(chain, {"module": "account", "action": "tokentx", "address": addr, "startblock": 0, "endblock": 99999999, "sort": "asc"}, info)
        if rows is None or info.get("truncated"):
            # 履歴が1万行で打ち切られると新しい銘柄が丸ごと欠け、保有が「全売却」に見える（kyle の AMC/AI 事故）
            warn(f"{chain} {short(addr)}: トークン一覧も全履歴も取れないため今回の残高更新は見送り（前回値を持ち越す）")
            return None
        net = defaultdict(float); meta = {}
        for t in rows:
            dec = int(t.get("tokenDecimal") or 18); k = L(t["contractAddress"]); v = int(t["value"]) / 10 ** dec
            net[k] += v if L(t["to"]) == L(addr) else -v; meta[k] = t.get("tokenSymbol") or "?"
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

def bs_reconcile_holdings(chain, addr, h, limit=40):
    """Blockscout の残高インデックスが欠落する銘柄（例: Base の O）を、tokentx の差引 → 価格あり → balanceOf で補完"""
    net = defaultdict(float); meta = {}
    for t in bs_compat_all(chain, {"module": "account", "action": "tokentx", "address": addr, "startblock": 0, "endblock": 99999999, "sort": "desc"}) or []:
        dec = int(t.get("tokenDecimal") or 18); k = L(t["contractAddress"]); v = int(t["value"]) / 10 ** dec
        net[k] += v if L(t["to"]) == L(addr) else -v; meta[k] = (t.get("tokenSymbol") or "?", dec)
    missing = [k for k, v in net.items() if v > 1e-6 and (k not in h or h[k]["amount"] <= 0)]
    if not missing: return
    prefetch_prices(chain, missing)
    priced = [k for k in missing if _px.get((chain, k))]             # 価格の付く（=流動性のある）銘柄だけ確認
    priced.sort(key=lambda k: -(_px[(chain, k)] * max(net[k], 0)))   # 金額の大きい順（上限に当たっても大口を取りこぼさない）
    for k in priced[:limit]:
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
def bsc_url(chain):
    """BSC の JSON-RPC 接続先。NodeReal 優先、無ければ Alchemy"""
    if NODEREAL_KEY: return CHAINS[chain]["rpc"].format(key=NODEREAL_KEY)
    if ALCHEMY_BNB_KEY: return CHAINS[chain]["alchemy"].format(key=ALCHEMY_BNB_KEY)
    return None
def use_alchemy(): return not NODEREAL_KEY and bool(ALCHEMY_BNB_KEY)
ALCHEMY_METHODS = {"nr_getAssetTransfers": "alchemy_getAssetTransfers", "nr_getTokenHoldings": None}

def nr_rpc(chain, method, params):
    url = bsc_url(chain)
    if not url: return None
    if use_alchemy():
        if method == "nr_getTokenHoldings": return None                      # Alchemy は別経路（alc_holdings）
        method = ALCHEMY_METHODS.get(method, method)
        if method == "alchemy_getAssetTransfers":
            p = dict(params[0]); p["category"] = [("erc20" if c == "20" else c) for c in p.get("category", []) if c != "internal"]; p["withMetadata"] = True   # Alchemy の BNB は internal 非対応
            params = [p]
    j = http_json("POST", url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
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
        cat = t.get("category"); frm = L(t.get("from") or ""); to = L(t.get("to") or "")
        if cat == "erc20": cat = "20"                                           # Alchemy 形式
        rc = t.get("rawContract") or {}
        if rc and t.get("contractAddress") is None: t["contractAddress"] = rc.get("address")
        if t.get("blockTimeStamp") is None and (t.get("metadata") or {}).get("blockTimestamp"): t["blockTimeStamp"] = t["metadata"]["blockTimestamp"]
        raw = t.get("value"); dec = hx(t.get("decimal")) if t.get("decimal") is not None else (hx(rc.get("decimal")) if rc.get("decimal") else 18)
        try: amount = hx(raw) / 10 ** dec if isinstance(raw, str) and raw.startswith("0x") else float(raw or 0)
        except Exception: amount = 0.0
        key = (t.get("hash"), cat, frm, to, round(amount, 12))
        if key in seen: continue
        seen.add(key)
        base = dict(chain=chain, tx=t.get("hash"), block=hx(t.get("blockNum")) or 0, ts=nr_ts(t.get("blockTimeStamp") or t.get("blockTimestamp")), frm=frm, to=to, amount=amount)
        if cat == "20":
            rows.append(dict(base, token=t.get("asset") or "?", contract=L(t.get("contractAddress") or ""), native=False))
        elif cat == "internal":
            if amount > 0: rows.append(dict(base, token=CHAINS[chain]["native"], contract="native", native=True, internal=True))
        else:
            if amount > 0: rows.append(dict(base, token=CHAINS[chain]["native"], contract="native", native=True))
    # 各 tx の送信者と input を見て「本人発か」「コントラクト呼出か」を補う（JSON-RPC batch で 50 件ずつ）
    txs = sorted({r["tx"] for r in rows if r["tx"]}); details = {}
    for i in range(0, len(txs), 50):
        chunk = txs[i:i + 50]
        j = http_json("POST", bsc_url(chain), json=[{"jsonrpc": "2.0", "id": n, "method": "eth_getTransactionByHash", "params": [tx]} for n, tx in enumerate(chunk)])
        if isinstance(j, list):
            for item in j:
                t = item.get("result") if isinstance(item, dict) else None
                if isinstance(t, dict) and t.get("hash"): details[t["hash"].lower()] = t
        else:   # batch 非対応なら1件ずつ
            for tx in chunk:
                t = nr_rpc(chain, "eth_getTransactionByHash", [tx])
                if isinstance(t, dict): details[tx.lower()] = t
        time.sleep(0.2)
    for tx in txs:
        t = details.get(tx.lower())
        if not isinstance(t, dict): continue
        for r in rows:
            if r["tx"] == tx: r["tx_from"] = L(t.get("from") or "")
        inp = t.get("input") or "0x"
        if L(t.get("from") or "") == L(addr) and inp not in ("0x", ""):
            blk = hx(t.get("blockNumber")) or 0; ts = next((r["ts"] for r in rows if r["tx"] == tx), 0)
            rows.append(dict(chain=chain, tx=tx, block=blk, ts=ts, frm=L(addr), to=L(t.get("to") or ""), token=CHAINS[chain]["native"],
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
    if isinstance(code, str) and code.lower().startswith("0xef0100"): return False   # EIP-7702 の委任コード＝スマートアカウント化した EOA
    return code not in ("0x", "")

def alc_holdings(chain, addr):
    h = {}
    bal = nr_rpc(chain, "eth_getBalance", [addr, "latest"])
    if bal is not None: h["native"] = {"symbol": CHAINS[chain]["native"], "amount": hx(bal) / 1e18, "contract": "native", "price": None}
    items, page_key = [], None
    for _ in range(15):                                   # 100件/ページ。pageKey で続きを取る（取り漏れ防止）
        params = [addr, "erc20"] + ([{"pageKey": page_key}] if page_key else [])
        res = nr_rpc(chain, "alchemy_getTokenBalances", params)
        if not isinstance(res, dict): break
        items += [t for t in res.get("tokenBalances") or [] if t.get("tokenBalance") not in (None, "0x0", "0x") and not t.get("error")]
        page_key = res.get("pageKey")
        if not page_key: break
    for t in items[:1500]:
        c = L(t.get("contractAddress") or ""); raw = hx(t.get("tokenBalance")) or 0
        if raw <= 0 or not c: continue
        meta = token_meta.get(f"{chain}:{c}")
        if not meta:
            m = nr_rpc(chain, "alchemy_getTokenMetadata", [c])
            if not isinstance(m, dict): continue
            meta = {"symbol": m.get("symbol") or "?", "decimals": m.get("decimals") if m.get("decimals") is not None else 18}; token_meta[f"{chain}:{c}"] = meta
        dec = int(meta.get("decimals") or 18); amt = raw / 10 ** dec
        if amt > 0: h[c] = {"symbol": meta.get("symbol") or "?", "amount": amt, "contract": c, "price": None}
    return h

def evm_reconcile_by_events(chain, addr, h, limit=40):
    """取引履歴（events）で受け取った銘柄のうち、残高一覧に無いものを balanceOf で確認して補完"""
    seen = {}
    for e in events:
        if e["chain"] == chain and e["wallet"] == addr and e["dir"] == "IN" and e["contract"] != "native": seen[L(e["contract"])] = e["token"]
    missing = [c for c in seen if c not in h]
    if not missing: return
    prefetch_prices(chain, missing)
    for c in [c for c in missing if _px.get((chain, c))][:limit]:
        res = nr_rpc(chain, "eth_call", [{"to": c, "data": "0x70a08231" + addr[2:].rjust(64, "0")}, "latest"])
        if not (isinstance(res, str) and res.startswith("0x") and len(res) > 2): continue
        raw = int(res, 16)
        if raw <= 0: continue
        meta = token_meta.get(f"{chain}:{c}")
        if not meta:
            m = nr_rpc(chain, "alchemy_getTokenMetadata", [c]) if use_alchemy() else None
            meta = {"symbol": (m or {}).get("symbol") or seen[c], "decimals": (m or {}).get("decimals") if isinstance(m, dict) and m.get("decimals") is not None else 18}
            token_meta[f"{chain}:{c}"] = meta
        dec = int(meta.get("decimals") or 18)
        h[c] = {"symbol": meta.get("symbol") or seen[c], "amount": raw / 10 ** dec, "contract": c, "price": _px.get((chain, c))}
        log(f"  残高補完 {chain} {short(addr)} {h[c]['symbol']} {raw / 10 ** dec:,.4g}（一覧に無く balanceOf で確認）")

def nr_holdings(chain, addr):
    if use_alchemy():
        h = alc_holdings(chain, addr)
        try: evm_reconcile_by_events(chain, addr, h)
        except Exception as e: warn(f"{chain} 残高補完でエラー: {e!r}")
        return h
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
            c = L(d.get("tokenAddress") or "")
            if amt > 0 and c: h[c] = {"symbol": d.get("tokenSymbol") or "?", "amount": amt, "contract": c, "price": None}
        if len(det) < 100 or page * 100 >= (hx(res.get("totalCount")) or 0): break
        page += 1
    try: evm_reconcile_by_events(chain, addr, h)
    except Exception as e: warn(f"{chain} 残高補完でエラー: {e!r}")
    return h

def nr_has_activity(chain, addr):
    latest = nr_block_number(chain)
    if not latest: return None
    lb = nr_lookback_blocks(chain, latest, INITIAL_LOOKBACK_H)
    trs = nr_transfers(chain, addr, max(0, latest - lb), latest)
    return None if trs is None else bool(trs)

# ------------------------------------------------------------ Helius（Solana）
_sym = {}   # (chain, contract) -> symbol（DexScreener/DAS から）
def hl_rpc(method, params):
    if not HELIUS_KEY: return None
    time.sleep(0.12)
    j = http_json("POST", CHAINS["solana"]["rpc"].format(key=HELIUS_KEY), json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if not isinstance(j, dict): return None
    if "error" in j: warn(f"solana {method}: {str(j['error'])[:120]}"); return None
    return j.get("result")

def hl_signatures(addr, until=None, min_ts=None, max_pages=10):
    """新しい順の署名一覧。until（署名）より新しいもの、または min_ts より新しいもの"""
    out, before = [], None
    for _ in range(max_pages):
        p = {"limit": 1000}
        if until: p["until"] = until
        if before: p["before"] = before
        res = hl_rpc("getSignaturesForAddress", [addr, p])
        if res is None: return None
        if not res: break
        for s in res:
            if min_ts and (s.get("blockTime") or 0) < min_ts: return out
            out.append(s)
        if len(res) < 1000: break
        before = res[-1]["signature"]
    return out

_hl_parsed = {}          # signature -> 解析済み tx（この実行内。ウォレット横断で100件ずつまとめて解析）
_hl_pending = {}         # wallet -> 新規署名リスト（hl_prefetch で先読み）
def hl_parse(sigs):
    """Enhanced Transactions API で解析（100署名/回、100クレジット/回）。解析済みはキャッシュ"""
    need = [x for x in dict.fromkeys(sigs) if x not in _hl_parsed]
    for i in range(0, len(need), 100):
        j = http_json("POST", f"{CHAINS['solana']['api']}/transactions?api-key={HELIUS_KEY}", json={"transactions": need[i:i + 100]}, timeout=60)
        if not isinstance(j, list): return None
        for t in j:
            if t.get("signature"): _hl_parsed[t["signature"]] = t
        time.sleep(0.15)
    return [_hl_parsed[x] for x in sigs if x in _hl_parsed]

def hl_prefetch(addrs):
    """全 Solana ウォレットの新規署名を先に集め、まとめて解析（呼び出し回数＝クレジット節約）"""
    allsigs = []
    for a in addrs:
        key = f"solana:{a}"
        sigs = hl_signatures(a, until=cursor[key]) if key in cursor else hl_signatures(a, min_ts=time.time() - INITIAL_LOOKBACK_H * 3600)
        if sigs is None: continue
        if len(sigs) >= 1000: warn(f"solana {short(a)}: 新規署名が 1000 件超 → 直近 1000 件のみ処理（高頻度ボット）")
        _hl_pending[a] = sigs[:1000]; allsigs += [x["signature"] for x in sigs[:1000]]
    if allsigs: hl_parse(allsigs)

def sym_of(chain, contract):
    return _sym.get((chain, contract)) or (token_meta.get(f"{chain}:{contract}") or {}).get("symbol") or "?"

def hl_rows(addr, parsed):
    rows = []
    for p in parsed:
        if p.get("transactionError"): continue
        ts = int(p.get("timestamp") or 0); sig = p.get("signature"); slot = int(p.get("slot") or 0); payer = p.get("feePayer") or ""
        typ = p.get("type") or ""; src = p.get("source") or ""
        for nt in p.get("nativeTransfers") or []:
            f, t = nt.get("fromUserAccount") or "", nt.get("toUserAccount") or ""
            if addr in (f, t) and (nt.get("amount") or 0) > 0:
                rows.append(dict(chain="solana", tx=sig, block=slot, ts=ts, frm=f, to=t, token="SOL", contract="native", amount=nt["amount"] / 1e9, native=True, tx_from=payer))
        for tt in p.get("tokenTransfers") or []:
            f, t = tt.get("fromUserAccount") or "", tt.get("toUserAccount") or ""
            if addr in (f, t) and (tt.get("tokenAmount") or 0) > 0:
                mint = tt.get("mint") or ""
                rows.append(dict(chain="solana", tx=sig, block=slot, ts=ts, frm=f, to=t, token=sym_of("solana", mint), contract=mint, amount=float(tt["tokenAmount"]), native=False, tx_from=payer))
        if payer == addr and typ != "TRANSFER":
            rows.append(dict(chain="solana", tx=sig, block=slot, ts=ts, frm=addr, to=src or typ, token="SOL", contract="native", amount=0.0, native=True, is_contract_call=True, method=typ, tx_from=payer))
    return rows

def fill_symbols(rows, budget=[20]):
    """Solana 行の未解決シンボルを DexScreener（prefetch 済み）→ DAS getAsset で埋める"""
    for r in rows:
        if r["chain"] != "solana" or r["token"] != "?" or r["contract"] == "native": continue
        s = sym_of("solana", r["contract"])
        if s == "?" and budget[0] > 0:
            budget[0] -= 1
            res = hl_rpc("getAsset", {"id": r["contract"]})
            if isinstance(res, dict):
                ti = res.get("token_info") or {}; md = (res.get("content") or {}).get("metadata") or {}
                s = ti.get("symbol") or md.get("symbol") or "?"
                token_meta[f"solana:{r['contract']}"] = {"symbol": s, "decimals": ti.get("decimals")}
        r["token"] = s

def hl_holdings(addr):
    h = {}
    res = hl_rpc("getAssetsByOwner", {"ownerAddress": addr, "page": 1, "limit": 1000, "displayOptions": {"showFungible": True, "showNativeBalance": True}})
    if not isinstance(res, dict): return h
    nb = res.get("nativeBalance") or {}
    if nb: h["native"] = {"symbol": "SOL", "amount": (nb.get("lamports") or 0) / 1e9, "contract": "native", "price": nb.get("price_per_sol")}
    for it in res.get("items") or []:
        if it.get("interface") not in ("FungibleToken", "FungibleAsset"): continue
        ti = it.get("token_info") or {}; dec = int(ti.get("decimals") or 0); bal = ti.get("balance") or 0
        amt = bal / 10 ** dec if dec else float(bal)
        if amt <= 0: continue
        mint = it.get("id"); sym = ti.get("symbol") or ((it.get("content") or {}).get("metadata") or {}).get("symbol") or "?"
        pi = ti.get("price_info") or {}
        h[mint] = {"symbol": sym, "amount": amt, "contract": mint, "price": pi.get("price_per_token")}
        token_meta[f"solana:{mint}"] = {"symbol": sym, "decimals": dec}
    return h

def hl_is_contract(addr):
    res = hl_rpc("getAccountInfo", [addr, {"encoding": "base64"}])
    if res is None: return None
    v = res.get("value")
    if v is None: return True                        # 存在しない（閉じた一時口座・ATA のレント払い先など）→ ウォレットとして追わない
    return bool(v.get("executable")) or v.get("owner") != SOL_SYSTEM

def hl_has_activity(addr):
    sigs = hl_signatures(addr, min_ts=time.time() - INITIAL_LOOKBACK_H * 3600, max_pages=1)
    return None if sigs is None else bool(sigs)

# ------------------------------------------------------------ プロバイダ振り分け
def provider(chain): return CHAINS[chain]["provider"]
def addr_fits(chain, a):
    """そのチェーンで有効なアドレス形式か（EVM チェーンは 0x+40桁、Solana は base58）"""
    a = a or ""
    return (not a.startswith("0x") and 32 <= len(a) <= 44) if provider(chain) == "helius" else (a.startswith("0x") and len(a) == 42)
def chain_available(chain):
    pv = provider(chain)
    return (bool(NODEREAL_KEY or ALCHEMY_BNB_KEY) if pv == "nodereal" else bool(HELIUS_KEY) if pv == "helius" else True)

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
    if provider(chain) == "helius":
        if addr in _hl_pending: sigs = _hl_pending.pop(addr)
        else:
            sigs = hl_signatures(addr, until=cursor[key], max_pages=1) if key in cursor else hl_signatures(addr, min_ts=time.time() - INITIAL_LOOKBACK_H * 3600, max_pages=1)
        if sigs is None: return None, None
        if not sigs: return [], cursor.get(key)
        parsed = hl_parse([s["signature"] for s in sigs])
        if parsed is None: return None, None
        return hl_rows(addr, parsed), sigs[0]["signature"]        # カーソル＝最新の署名
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
        pv = provider(chain)
        r = bs_is_contract(chain, addr) if pv == "blockscout" else hl_is_contract(addr) if pv == "helius" else nr_is_contract(chain, addr)
        if r is not None: return r
        time.sleep(2)
    return None

def fetch_holdings(chain, addr):
    pv = provider(chain)
    return bs_holdings(chain, addr) if pv == "blockscout" else hl_holdings(addr) if pv == "helius" else nr_holdings(chain, addr)
def has_activity(chain, addr):
    pv = provider(chain)
    return bs_has_activity(chain, addr) if pv == "blockscout" else hl_has_activity(addr) if pv == "helius" else nr_has_activity(chain, addr)

# ------------------------------------------------------------ 価格
_px = {}                                                   # (chain, contract) -> price or None（この実行内のキャッシュ）
_liq = {}                                                  # (chain, contract) -> DexScreener 流動性 USD
_mcap = {}                                                 # (chain, contract) -> (時価総額, FDV)。評価の妥当性チェック用
_created = {}                                              # (chain, contract) -> ペア作成時刻 (epoch 秒)
CTX["quote"] = lambda ch, c: (_px.get((ch, L(c or ""))), _liq.get((ch, L(c or ""))))
price_cache = jload(DATA / "prices.json", {})              # "chain:contract" -> {"px": .., "ts": ..}（前回価格。API 不調時の保険）

def _pair_price(p):
    liq = float((p.get("liquidity") or {}).get("usd") or 0)
    return (liq, float(p["priceUsd"])) if liq >= MIN_LIQ_USD and p.get("priceUsd") else (liq, None)

def _note_created(chain, contract, p):
    c = p.get("pairCreatedAt")
    if c:
        key = (chain, contract); t = int(c) // 1000
        _created[key] = min(_created.get(key, t), t)
        pc = price_cache.setdefault(f"{chain}:{contract}", {}); pc["created"] = min(pc.get("created") or t, t)

def _note_mcap(chain, contract, p):
    """採用したペアの時価総額と FDV を控える（FDV=0 で mcap>0 は桁あふれ＝総供給が異常なトークンの印）"""
    try: mc = float(p.get("marketCap") or 0); fd = float(p.get("fdv") or 0)
    except (TypeError, ValueError): return
    _mcap[(chain, contract)] = (mc, fd)
    price_cache.setdefault(f"{chain}:{contract}", {}).update({"mcap": mc, "fdv": fd})

def mcap_of(chain, contract):
    key = (chain, L(contract))
    if key in _mcap: return _mcap[key]
    pc = price_cache.get(f"{chain}:{L(contract)}") or {}
    return (pc.get("mcap"), pc.get("fdv")) if "mcap" in pc else (None, None)

def pair_created(chain, contract):
    key = (chain, L(contract))
    if key in _created: return _created[key]
    pc = price_cache.get(f"{chain}:{L(contract)}") or {}
    return pc.get("created")

def _set_px(chain, contract, px, fetched=True):
    key = (chain, contract)
    if px is None and fetched is False:                  # 取得失敗 → 前回価格で代用
        old = price_cache.get(f"{chain}:{contract}")
        if old and old.get("px") and time.time() - old.get("ts", 0) < PRICE_CACHE_H * 3600: px = old["px"]
    _px[key] = px
    if px is not None and fetched: price_cache.setdefault(f"{chain}:{contract}", {}).update({"px": px, "ts": int(time.time())})

def prefetch_prices(chain, contracts):
    """DexScreener の tokens/v1 は 30 アドレスまで一括可。呼び出し回数を減らすため先にまとめて取る"""
    need = sorted({L(c or "") for c in contracts if c and c != "native" and (chain, L(c or "")) not in _px})
    for i in range(0, len(need), 30):
        chunk = need[i:i + 30]; cs = set(chunk)
        j = http_json("GET", f"https://api.dexscreener.com/tokens/v1/{CHAINS[chain]['dex']}/{','.join(chunk)}", retries=3)
        if not isinstance(j, list):
            for c in chunk: _set_px(chain, c, None, fetched=False)
            continue
        best = {}
        for p in j:
            addr = L((p.get("baseToken") or {}).get("address") or "")
            if addr in cs:
                liq, px = _pair_price(p); _note_created(chain, addr, p)
                if addr not in best or liq > best[addr][0]: best[addr] = (liq, px); _sym[(chain, addr)] = (p.get("baseToken") or {}).get("symbol") or _sym.get((chain, addr)); _note_mcap(chain, addr, p)
        for c in chunk: _set_px(chain, c, best.get(c, (0, None))[1]); _liq[(chain, c)] = best.get(c, (0, None))[0]
        time.sleep(0.3)

def price(chain, symbol, contract):
    mp = CFG.get("manual_prices", {})
    if symbol in mp: return float(mp[symbol])
    if symbol in STABLES and (chain, L(contract or "")) in KNOWN_STABLES: return 1.0
    c = CHAINS[chain]
    if contract == "native":
        if c.get("wnative"): chain, contract = chain, c["wnative"]
        elif c.get("native_ref"): chain, contract = c["native_ref"]
        else: return None
    if not contract: return None
    contract = L(contract); key = (chain, contract)
    if key in _px: return _px[key]
    j = http_json("GET", f"https://api.dexscreener.com/tokens/v1/{CHAINS[chain]['dex']}/{contract}", retries=2)
    if not isinstance(j, list): _set_px(chain, contract, None, fetched=False); return _px[key]
    px = None
    try:
        if j:
            for p_ in j: _note_created(chain, contract, p_)
            bestp = max(j, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
            liq, px = _pair_price(bestp); _liq[key] = liq; _note_mcap(chain, contract, bestp)
    except Exception: px = None
    _set_px(chain, contract, px)
    return px

# ------------------------------------------------------------ 分類
def classify_tx(chain, owner, trs):
    outs = [r for r in trs if r["frm"] == owner and r["amount"] > 0]
    ins = [r for r in trs if r["to"] == owner and r["amount"] > 0]
    # 本人が署名していない tx でトークンだけが「出ていく」＝スキャムトークンの偽 Transfer（アドレスポイズニング）
    # ただし価格の付く本物トークン（USDC 等）の送出は、Solana のガスレス送金（手数料を別人が払う）なので本物として扱う
    if outs and not ins and all(not r["native"] and r.get("tx_from") != owner for r in outs):
        if all(price(chain, r["token"], r["contract"]) is None for r in outs):
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

_age_probe = set()
def fmt_usd_(v): return P.fmt_usd(v)
def resolve_purchases(evs):
    """相手先からの受取に対し、全チェーン横断で「同じ相手へ原資を払ったか」を後付けで判定。
       ラベル付きサービスだけでなく、7日以内にクラスターが $500 以上を払った相手（クロスチェーン執行ウォレット等）からの受取も購入扱い"""
    pays = defaultdict(list)
    for e in evs:
        if e["dir"] == "OUT" and e["cp"] and not is_watched(e["cp"]) and (e.get("usd") or 0) >= 500 and e["kind"] not in ("売却(スワップ)", "購入(スワップ)", "スワップ", "売却(スワップ・代金不明)"):
            pays[e["cp"]].append(e)
    for e in evs:
        if e["dir"] != "IN" or not e["cp"] or is_watched(e["cp"]): continue
        if e["kind"] not in ("サービスから受取", "受取(原資未確認)", "購入(クロスチェーン)", "受取"): continue
        paid = [p for p in pays.get(e["cp"], []) if 0 <= e["ts"] - p["ts"] <= BUY_MATCH_HOURS * 3600]
        if paid:
            e["kind"] = "購入(クロスチェーン)"; e["funding_usd"] = sum(p["usd"] or 0 for p in paid)
            e["funding_note"] = "、".join(f"{p['time'][5:16]} {p['amount']:.4g} {p['token']}" for p in paid[:3])
        elif e["kind"] != "受取" or is_service(e["cp"]):
            e["kind"] = "受取(原資未確認)"
    # 上場直後（NEW_TOKEN_H 以内）の銘柄を執行サービスから少額受け取ったもの、または本人が何も払っていない相手から素で受け取ったものは、
    # クジラへの宣伝エアドロップの疑い → 買いに数えない（XXX / stonkscat 等: 同じ送り主が複数クジラへ同額を連投するパターン）
    cands = [e for e in evs if e["kind"] in ("購入(クロスチェーン)", "受取(原資未確認)", "受取") and e["contract"] != "native" and (e.get("usd") or 0) < NEW_TOKEN_MAX_USD
             and e["dir"] == "IN" and not is_watched(e["cp"]) and (e["kind"] != "受取" or (e.get("usd") or 0) >= DUST_USD)]   # 素の受取は価格付き・ダスト超のみ（DexScreener 問い合わせ枠の節約）
    for e in sorted(cands, key=lambda e: -e["ts"]):          # 新しいものから上場時刻を確認（DexScreener 呼び出しは最大 40 銘柄/回）
        c = pair_created(e["chain"], e["contract"])
        if c is None and e["ts"] >= time.time() - 3 * 86400 and len(_age_probe) < 40 and e["contract"] not in _age_probe:
            _age_probe.add(e["contract"]); price(e["chain"], e["token"], e["contract"]); c = pair_created(e["chain"], e["contract"])
        if c is not None and 0 <= e["ts"] - c < NEW_TOKEN_H * 3600:
            recent = sum((p.get("usd") or 0) for p in pays.get(e["cp"], []) if 0 <= e["ts"] - p["ts"] <= 2 * 3600)
            if recent >= 0.5 * (e.get("usd") or 0) and recent > 0:
                e["note"] = f"上場 {(e['ts'] - c) / 3600:.1f} 時間後だが直前2時間に {fmt_usd_(recent)} を支払済み → 購入として扱う"; continue
            e["kind"] = "受取(新規トークン)"; e["note"] = f"上場 {(e['ts'] - c) / 3600:.1f} 時間後の少額受取（エアドロップ疑い）"

PHANTOM_CHECK_USD = float(CFG.get("phantom_check_usd", 1000))   # この額以上の「受取」は balanceOf で実在を確認する
def evm_call(chain, payload):
    """EVM の JSON-RPC を1回叩いて result（hex文字列）を返す。Blockscout 系は PRO json-rpc → 公開 RPC、BSC は NodeReal/Alchemy"""
    if provider(chain) == "nodereal":
        res = nr_rpc(chain, payload["method"], payload["params"])
        return res if isinstance(res, str) else None
    urls = ([f"{BLOCKSCOUT_PRO}/{CHAINS[chain]['chain_id']}/json-rpc?apikey={BLOCKSCOUT_KEY}"] if BLOCKSCOUT_KEY else []) + CHAINS[chain].get("rpcs", [])
    for url in urls:
        j = http_json("POST", url, retries=1, json={"jsonrpc": "2.0", "id": 1, **payload})
        res = j.get("result") if isinstance(j, dict) else None
        if isinstance(res, str) and res.startswith("0x") and len(res) > 2: return res
    return None

def balance_of(chain, contract, addr):
    """残高（生の整数）をチェーンに直接聞く。contract="native" はネイティブ残高。索引に依存しないので障害時の最後の砦。失敗は None"""
    if provider(chain) == "helius": return None
    if contract == "native": res = evm_call(chain, {"method": "eth_getBalance", "params": [addr, "latest"]})
    else: res = evm_call(chain, {"method": "eth_call", "params": [{"to": contract, "data": "0x70a08231" + addr[2:].rjust(64, "0")}, "latest"]})
    if not res: return None
    try: return int(res, 16)
    except ValueError: return None

def token_decimals(chain, contract):
    """ERC-20 decimals()。token_meta にキャッシュ。取れなければ 18"""
    if contract == "native": return 18
    m = token_meta.get(f"{chain}:{contract}") or {}
    if m.get("decimals") is not None: return int(m["decimals"])
    res = evm_call(chain, {"method": "eth_call", "params": [{"to": contract, "data": "0x313ce567"}, "latest"]})
    try: dec = int(res, 16) if res else None
    except ValueError: dec = None
    if dec is None or not 0 <= dec <= 36: return 18
    token_meta[f"{chain}:{contract}"] = {**m, "decimals": dec, "symbol": m.get("symbol") or "?"}
    return dec

def verify_phantom_receipts(new_evs):
    """本人が払っていない相手からの大きめのトークン受取が、実際の残高に反映されているかを balanceOf で確認する。
       Transfer イベントだけ発行して残高を動かさない偽トークン（幻の送金: XXX / stonkscat 等、$12k 相当が数分おきに複数クジラへ届く）は
       「なりすまし(偽送金)」に落とし、台帳・流入・同時買いのどれにも数えない。1グループ1回の eth_call なので無料枠への影響は小さい"""
    groups = defaultdict(list)
    for e in new_evs:
        if (e["dir"] == "IN" and e["kind"] in ("受取", "受取(新規トークン)") and e["contract"] != "native" and e["chain"] != "solana"
                and (e.get("usd") or 0) >= PHANTOM_CHECK_USD and e["cp"] and not is_watched(e["cp"])):
            groups[(e["chain"], e["wallet"], L(e["contract"]))].append(e)
    for (ch, w, c), lst in groups.items():
        t0 = min(e["ts"] for e in lst)
        if any(x["dir"] == "OUT" and x["wallet"] == w and x["chain"] == ch and L(x["contract"] or "") == c and x["ts"] >= t0 for x in events): continue   # 売った/送った形跡があれば本物
        raw = balance_of(ch, c, w)
        if raw is None: continue
        if raw == 0:
            for e in lst: e["kind"] = "なりすまし(偽送金)"; e["note"] = "Transfer イベントだけで残高が増えていない（幻の送金・宣伝スパム）"
            log(f"  幻の送金: {ch} {label(w)} {lst[0]['token']} ×{len(lst)}（≈{P.fmt_usd(sum(e.get('usd') or 0 for e in lst))}）→ balanceOf=0 のため なりすまし扱い")
        time.sleep(0.2)

VALUATION_CHECK_USD = float(CFG.get("valuation_check_usd", 5000))   # この額以上のリスク保有は評価の妥当性を検査する
MAX_SUPPLY = float(CFG.get("max_token_supply", 1e22))               # 総供給がこれを超えるトークンは異常（Monkey は 1e76）
MAX_MCAP = float(CFG.get("max_token_mcap_usd", 2e10))               # DEX 銘柄で時価総額 $200億超は偽装（SPYB は $3,320億）
MAX_LIQ = float(CFG.get("max_pair_liquidity_usd", 3e8))             # 1ペアの流動性 $3億超は偽装（SPYB は $15.7億）
AIRDROP_LIQ_MULT = float(CFG.get("airdrop_liq_multiple", 2.0))      # 買っていない受取のみの銘柄は、評価額が流動性のこの倍数を超えたら売れないと見なす

def token_supply(chain, contract):
    """総供給（枚）。EVM は totalSupply() をチェーンに直接問い合わせ、token_meta に永続キャッシュ。Solana・失敗は None"""
    if provider(chain) == "helius" or contract == "native": return None
    m = token_meta.get(f"{chain}:{contract}") or {}
    if m.get("supply") is not None: return m["supply"]
    res = evm_call(chain, {"method": "eth_call", "params": [{"to": contract, "data": "0x18160ddd"}, "latest"]})
    try: raw = int(res, 16) if res else None
    except ValueError: raw = None
    if raw is None: return None
    sup = raw / 10 ** token_decimals(chain, contract)
    token_meta[f"{chain}:{contract}"] = {**(token_meta.get(f"{chain}:{contract}") or m), "supply": sup, "symbol": m.get("symbol") or "?"}
    return sup

def valuation_check(chain, contract, symbol, amount, px, usd):
    """保有の評価額（枚数×価格）が現実的かを検査する。異常なら (False, 理由)。
       unipcs の Monkey（総供給 1e76・decimals 0・受け取っただけ・流動性 $67K に対し評価 $2.9M）が総資産の 12% を占めた事故の再発防止。
       流動性で機械的に上限をかけると、本当に買った AMC（評価/流動性 14 倍・時価総額 $94M）まで潰れるので、異常の兆候だけを見る"""
    c = L(contract); liq = _liq.get((chain, c)); mcap, fdv = mcap_of(chain, c)
    if mcap and fdv == 0: return False, "DexScreener の FDV が桁あふれ（総供給が異常）"
    if mcap and mcap > MAX_MCAP: return False, f"時価総額 {P.fmt_usd(mcap)} は DEX 銘柄として非現実的（偽装）"
    if liq and liq > MAX_LIQ: return False, f"流動性 {P.fmt_usd(liq)} は非現実的（偽装）"
    if mcap and usd > mcap: return False, f"評価額が時価総額 {P.fmt_usd(mcap)} を超える"
    sup = token_supply(chain, c)
    if sup and sup > MAX_SUPPLY: return False, f"総供給 {sup:.1e} 枚は異常（decimals/供給の偽装）"
    flows = [P.flow_of(e, CTX) for e in events if e["chain"] == chain and L(e["contract"] or "") == c]
    if liq and usd > AIRDROP_LIQ_MULT * liq:
        if flows and "buy" not in flows and "in" in flows: return False, f"買っていない受取のみで、評価額が流動性 {P.fmt_usd(liq)} の {usd / liq:.0f} 倍（売れない）"
        if not (symbol or "").isascii(): return False, f"非ASCIIシンボルのばら撒き銘柄で、評価額が流動性 {P.fmt_usd(liq)} の {usd / liq:.0f} 倍"
    return True, ""

def apply_prices(ch, h):
    """残高に価格と評価額を付け、リスク保有の評価が異常なら usd=0（除外）にして理由を残す"""
    prefetch_prices(ch, [v["contract"] for v in h.values() if not v.get("price")])
    for s, v in h.items():
        v["price"] = v.get("price") or price(ch, v["symbol"], v["contract"]); v["usd"] = v["amount"] * v["price"] if v["price"] else None
        v.pop("excluded", None); v.pop("usd_raw", None)
        if v["usd"] and v["usd"] >= VALUATION_CHECK_USD and P.bucket_of(ch, v["contract"], CTX) == "risk":
            try: ok, why = valuation_check(ch, v["contract"], v["symbol"], v["amount"], v["price"], v["usd"])
            except Exception as e: ok, why = True, ""; warn(f"評価チェックでエラー {ch} {v['symbol']}: {e!r}")
            if not ok:
                v["excluded"] = why; v["usd_raw"] = v["usd"]; v["usd"] = 0.0
                log(f"  評価除外 {ch} {v['symbol']} {v['amount']:.4g} 枚（名目 {P.fmt_usd(v['usd_raw'])}）: {why}")

VANISH_MIN_USD = float(CFG.get("vanish_check_usd", 1000))     # 前回これ以上あった銘柄が消えたら実在確認する
def guard_vanished_positions(chain, addr, h, old, since_ts):
    """前回の残高にあった銘柄が、売り・送金の記録なしに消えた/ゼロになったら、索引の取りこぼしを疑って
       チェーンに直接 balanceOf を聞き、実在すれば復元する。照会に失敗したら前回値を持ち越す。
       （Blockscout の索引障害で AMC・AI が「全売却」に見え、総資産が 37% 落ちて見えた事故の再発防止）"""
    if not old or provider(chain) == "helius": return
    gone = [c for c, v in old.items() if (v.get("usd") or 0) >= VANISH_MIN_USD and (h.get(c, {}).get("amount") or 0) <= 0]
    if not gone: return
    sold = {L(e["contract"] or "") for e in events
            if e["chain"] == chain and e["wallet"] == addr and e["dir"] == "OUT" and e["ts"] >= since_ts - 3600}
    for c in gone:
        if c in sold: continue                                    # 売り・送りの記録があるなら本当に減っている
        raw = balance_of(chain, c, addr)
        sym = old[c].get("symbol") or "?"
        if raw is None:
            h[c] = {**old[c], "price": None, "usd": None}
            warn(f"{chain} {short(addr)} {sym}: 一覧から消えたが残高照会も失敗 → 前回値を持ち越し")
        elif raw > 0:
            amt = raw / 10 ** token_decimals(chain, c)
            h[c] = {"symbol": sym, "amount": amt, "contract": c, "price": None}
            log(f"  索引漏れを補完 {chain} {short(addr)} {sym} {amt:,.4g}（一覧から消えていたが balanceOf で保有を確認）")
        else:
            log(f"  {chain} {short(addr)} {sym}: 残高 0 を確認（売却記録は無いが実際に無くなっている）")
        time.sleep(0.2)

# ------------------------------------------------------------ 子ウォレット登録
_children_this_run = defaultdict(int)
def register_child(chain, parent, cp, first_seen, queue):
    if len(wallets) >= MAX_WALLETS:
        warn(f"監視ウォレットが上限 {MAX_WALLETS} に達したため {short(cp)} は追加しない（config の max_wallets）"); return False
    if wallets.get(parent, {}).get("no_children"): return False
    if _children_this_run[parent] >= MAX_CHILDREN_PER_RUN:
        if _children_this_run[parent] == MAX_CHILDREN_PER_RUN:
            warn(f"{label(parent)} から起こした子が {MAX_CHILDREN_PER_RUN} を超えた → 分配ボットと判定、以後この親からは子を起こさない（wallets.json の no_children）")
            wallets[parent]["no_children"] = True
        _children_this_run[parent] += 1; return False
    _children_this_run[parent] += 1
    wallets[cp] = {"role": child_role(wallets[parent]["role"]), "parent": parent, "first_seen": first_seen, "chains": [chain]}
    queue.append(cp); log(f"  🆕 監視追加 {wallets[cp]['role']} {cp} (親 {short(parent)}, {chain})"); return True

def wallet_depth(a):
    d = 0
    while wallets.get(a, {}).get("parent"): a = wallets[a]["parent"]; d += 1
    return d

def try_register(chain, parent, cp, ev, queue):
    """未知アドレスが EOA なら子として登録。判定不能なら pending に積んで次回再判定"""
    if cp in wallets: return True                      # 同じ tx 内の複数行などで二重登録しない
    if is_service(cp): return False
    if chain == "solana" and wallet_depth(parent) + 1 > SOLANA_MAX_DEPTH: return False   # Solana はボットの連鎖が深くなりやすいので深さ制限
    r = is_contract(chain, cp)
    if r is False: return register_child(chain, parent, cp, ev["time"], queue)
    if r is None and not any(p["addr"] == cp for p in pending_eoa):
        pending_eoa.append({"chain": chain, "addr": cp, "parent": parent, "first_seen": ev["time"]}); warn(f"EOA判定不能 → 保留 {cp}")
    return False

# ------------------------------------------------------------ メイン処理
def run():
    new_events = []; now_ts = time.time()
    state["run_count"] = state.get("run_count", 0) + 1
    # 初回チェーン探索（NodeReal キー無しなら BSC 抜きで探索し、結果は保存しない）
    active = list(CFG.get("chains") or [])
    checked = set(CFG.get("chains_checked") or ([ch for ch in CHAINS if chain_available(ch)] if active else []))
    changed = False
    for ch in CHAINS:      # 未探索 かつ キーがあるチェーンだけ探索（キーが後から追加された場合もここで拾う）
        if ch in checked or ch in active: continue
        if not chain_available(ch): continue
        if any(has_activity(ch, a) for a in CFG["main_wallets"] if addr_fits(ch, a)): active.append(ch)
        checked.add(ch); changed = True
    if changed:
        log("活動のあるチェーン:", active)
        CFG["chains"] = active; CFG["chains_checked"] = sorted(checked)
        if not DRY_RUN: json.dump(CFG, open(ROOT / "config.json", "w"), indent=2, ensure_ascii=False)
    active = [ch for ch in active if chain_available(ch)]
    # 保留中の EOA 判定を再試行
    queue = list(wallets.keys())
    for p in list(pending_eoa):
        if p["addr"] in wallets: pending_eoa.remove(p); continue
        r = is_contract(p["chain"], p["addr"])
        if r is False:
            if register_child(p["chain"], p["parent"], p["addr"], p["first_seen"], queue): pending_eoa.remove(p)
        elif r is True: pending_eoa.remove(p)
    if not snapshots and not DRY_RUN and over_budget() is False:
        first_h = {}
        for w in list(wallets):
            for ch in active:
                if not addr_fits(ch, w) or (ch not in wallets[w]["chains"] and wallets[w]["role"] != "本体"): continue
                h = fetch_holdings(ch, w) or {}; apply_prices(ch, h)
                first_h[f"{ch}:{w}"] = h
        doc0 = {"version": 3, "updated": datetime.now(timezone.utc).isoformat(), "holdings": first_h}
        snapshots.append(P.build_snapshot(doc0, CTX)); json.dump(doc0, open(DATA / "holdings.json", "w"), indent=2, ensure_ascii=False)
        log(f"初回: 残高を先に取得（総資産 {P.fmt_usd(snapshots[-1]['total'])}）")
    processed = set()
    poll = [ch for ch in active if ch in PRIMARY_CHAINS or state["run_count"] % SECONDARY_EVERY == 0]
    log("今回取得するチェーン:", poll)
    if "solana" in poll and HELIUS_KEY: hl_prefetch([w for w in wallets if addr_fits("solana", w)])
    while queue:
        w = queue.pop(0)
        if w in processed: continue
        processed.add(w)
        for ch in poll:
            key = f"{ch}:{w}"
            if not addr_fits(ch, w): continue
            if over_budget(): warn(f"時間予算 {RUN_BUDGET_S/60:.0f} 分超過 → {ch} {short(w)} 以降は次回に持ち越し"); continue
            rows, new_cur = fetch_rows(ch, w)
            if rows is None: warn(f"{ch} {short(w)}: 取得失敗（次回に持ち越し）"); continue
            if rows and ch not in wallets[w]["chains"]: wallets[w]["chains"].append(ch)
            prefetch_prices(ch, [r["contract"] for r in rows if not r["native"]] + ([CHAINS[ch]["wnative"]] if CHAINS[ch].get("wnative") else []))
            if ch == "solana": fill_symbols(rows)
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
                    fake_sym = r["token"].upper() in ("BNB", "ETH", "SOL", "WBNB", "WETH", "WSOL", "USDT", "USDC") and (ch, L(r["contract"])) not in KNOWN_STABLES and L(r["contract"]) != L(CHAINS[ch].get("wnative") or "")
                    if not r["native"] and not is_watched(cp) and fake_sym:
                        ev["kind"] = "ダスト"     # 本物以外のコントラクトで USDC/SOL 等を名乗る＝偽トークン
                    elif d == "IN" and not r["native"] and not is_watched(cp) and not is_service(cp) and not r["token"].isascii():
                        ev["kind"] = "ダスト"     # 非ASCIIシンボルのばら撒き（アドレスポイズニング）
                    if d == "IN" and is_lookalike(cp): ev["kind"] = "ダスト"; ev["cp_label"] = f"なりすまし {short(cp)}"
                    # 新ウォレット検出: 未知EOAへのガス種銭 or 単純トークン送金（スワップは分類段階で除外済み）
                    if ev["kind"] == "外部へ送金" and cp and d == "OUT" and (now_ts - r["ts"]) < CHILD_MAX_AGE_D * 86400 and not is_burn(cp) and not is_lookalike(cp):
                        seed = r["native"] and (usd or 0) < GAS_SEED_USD and r["amount"] >= MIN_SEED_NATIVE.get(ch, MIN_SEED_NATIVE["default"])
                        if seed or not r["native"]:
                            if try_register(ch, w, cp, ev, queue):
                                ev["kind"] = "新ウォレット開設(ガス種銭)" if seed else "内部移動"; ev["cp_label"] = label(cp)
                    new_events.append(ev)
            if new_cur is not None: cursor[key] = new_cur
            log(f"{ch:9s} {label(w):22s} 行 {len(rows):4d} / 新規tx {n_new}")
            time.sleep(0.3)
    events.extend(new_events)
    resolve_purchases(events)
    # 直近48時間の未検証の受取を対象（なりすまし判定済みは対象外なので、2回目以降は新規分だけ eth_call が走る）
    try: verify_phantom_receipts([e for e in events if e["ts"] >= now_ts - NOTIFY_MAX_AGE_H * 3600])
    except Exception as e: warn(f"幻の送金チェックでエラー: {e!r}")
    # 残高（HOLDINGS_EVERY 回に1回、または初回・新規ウォレット追加時）
    prev = jload(DATA / "holdings.json", {"updated": None, "holdings": {}})
    # 欠けキーの判定は下の取得ループと同じ条件（そのウォレットが活動するチェーン or 本体）。ずれると毎回「欠けあり」になり残高更新が毎回走る（本体で発生していた）
    need = state["run_count"] % HOLDINGS_EVERY == 1 or not prev["holdings"] or prev.get("version") != 3 or any(f"{ch}:{w}" not in prev["holdings"] for w in wallets for ch in active if addr_fits(ch, w) and (ch in wallets[w]["chains"] or wallets[w]["role"] == "本体"))
    nowdt = datetime.now(timezone.utc)
    due = P.last_cut(nowdt.timestamp())                                  # 直近の締め時刻（既定 24:00 JST）
    last_done = state.get("last_daily_cut")
    if last_done is None and state.get("last_daily"):                    # 旧形式（UTC 日付 ＝ 00:00 UTC 締め）からの引き継ぎ
        try: last_done = int(datetime.strptime(state["last_daily"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        except Exception: last_done = 0
    state["daily_cut"] = due
    state["daily_due"] = (last_done or 0) < due                          # その締めの日次をまだ送っていなければ送る
    if state["daily_due"]: need = True                                   # 日次レポートの時点は必ず実残高で
    if need and over_budget(): warn("時間予算超過 → 残高更新は次回"); need = False
    if need:
        holdings = {}; stale = []
        prev_h = prev.get("holdings") or {}
        try: since_ts = int(datetime.fromisoformat(prev["updated"]).timestamp()) if prev.get("updated") else 0
        except Exception: since_ts = 0
        for w in wallets:
            for ch in active:
                if not addr_fits(ch, w): continue
                if ch not in wallets[w]["chains"] and wallets[w]["role"] != "本体": continue
                key = f"{ch}:{w}"; old = prev_h.get(key) or {}
                h = fetch_holdings(ch, w)
                old_usd = sum((v.get("usd") or 0) for v in old.values())
                if h is None or (not h and old_usd >= VANISH_MIN_USD):
                    # 取得できなかった時に空で上書きすると「全部売った」ように見える → 前回値を持ち越し、価格だけ付け直す
                    h = {c: {**v, "price": None, "usd": None} for c, v in old.items()}
                    if h: stale.append(key); warn(f"{ch} {short(w)}: 残高を取得できず前回値を持ち越し（{P.fmt_usd(old_usd)}）")
                else:
                    try: guard_vanished_positions(ch, w, h, old, since_ts)
                    except Exception as e: warn(f"{ch} {short(w)} 消失ポジションの確認でエラー: {e!r}")
                apply_prices(ch, h)
                holdings[key] = h
                time.sleep(0.3)
        holdings_doc = {"version": 3, "updated": datetime.now(timezone.utc).isoformat(), "holdings": holdings}
        if stale: holdings_doc["stale"] = stale
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
        keep_days = float(CFG.get("events_keep_days", 45)); cut = time.time() - keep_days * 86400
        old_ev = [e for e in events if e["ts"] < cut]
        if old_ev:
            (DATA / "archive").mkdir(exist_ok=True)
            with open(DATA / "archive" / "events_old.jsonl", "a") as f:
                for e in sorted(old_ev, key=lambda e: e["ts"]): f.write(json.dumps(e, ensure_ascii=False) + "\n")
            events[:] = [e for e in events if e["ts"] >= cut]
        with open(DATA / "events.jsonl", "w") as f:
            for e in sorted(events, key=lambda e: e["ts"]): f.write(json.dumps(e, ensure_ascii=False) + "\n")
        json.dump(holdings_doc, open(DATA / "holdings.json", "w"), indent=2, ensure_ascii=False)
        json.dump(price_cache, open(DATA / "prices.json", "w"))
        P.save_snapshots(DATA / "snapshots.jsonl", snapshots)
        json.dump(token_meta, open(DATA / "token_meta.json", "w"), ensure_ascii=False)
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
TG_EXTRA = os.getenv("TG_EXTRA", "")   # 追加の通知先（他の人の Bot に同じ通知を送る）: JSON 配列 [{"token": "<BotFather のトークン>", "chat": "<chat_id>"}]
def tg_targets():
    """通知先 [(token, chat), ...]。主＝TG_TOKEN/TG_CHAT、追加＝TG_EXTRA（GitHub Secrets、公開リポの config には書かない）"""
    out = [(TG_TOKEN, TG_CHAT)] if TG_TOKEN and TG_CHAT else []
    if TG_EXTRA.strip():
        try:
            for d in json.loads(TG_EXTRA):
                if d.get("token") and d.get("chat"): out.append((str(d["token"]), str(d["chat"])))
        except Exception as e: warn(f"TG_EXTRA の形式が不正（JSON 配列 [{{\"token\":..,\"chat\":..}}] にする）: {e!r}")
    return out

def tg(text):
    if WHALE_NAME: text = f"【{WHALE_NAME}】" + text     # 誰のクジラの通知かを毎回先頭に（藤沼さん要望: 見た瞬間に分かるように）
    if DRY_RUN: log("[DRY_RUN] Telegram:\n" + text); return
    targets = tg_targets()
    if not targets: warn("TG_TOKEN/TG_CHAT 未設定のため通知スキップ"); return
    for token, chat in targets:                      # 宛先ごとに独立して送る（片方が失敗しても他方には届く）
        for i in range(0, len(text), 3800):
            try:
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat, "text": text[i:i + 3800], "disable_web_page_preview": True}, timeout=20)
                if not r.ok: warn(f"Telegram 送信失敗 (chat {chat}):", r.status_code, r.text[:200])
            except Exception as e:
                warn(f"Telegram 送信エラー (chat {chat}): {e!r}")

def accumulate_buys(groups, now):
    """実行（10分）をまたぐ分割買いを 銘柄×ウォレット ごとに累計し、未通知分の累計が BUY_ALERT_USD に達した時点で通知対象にする。
       例: $1,200 × 5 回を 30 分かけて買った場合、従来は各実行で $5,000 未満のため通知されなかった → 5 回目で「累計 $6K・5回」として通知。
       通知した分はリセット（大口の単発買いは従来通りその回だけ）。BUY_ACCUM_H 時間動きが無い銘柄は忘れる。state.buy_accum に永続化"""
    acc = state.setdefault("buy_accum", {})
    for a in groups:
        key = f"{a['chain']}:{a['contract']}:{a['wallet']}"
        e = acc.get(key)
        if e and now - e["last"] > BUY_ACCUM_H * 3600: e = None
        if not e:
            e = acc[key] = {"wallet": a["wallet"], "chain": a["chain"], "contract": a["contract"], "sym": a["sym"], "amount": 0.0, "usd": 0.0,
                            "counter_usd": 0.0, "n": 0, "other": {}, "first": a["first"], "last": a["last"], "runs": 0, "funding": 0.0, "note": ""}
        e["amount"] += a["amount"]; e["usd"] += a["usd"]; e["counter_usd"] += a["counter_usd"]; e["n"] += a["n"]; e["runs"] += 1; e["sym"] = a["sym"]
        e["first"] = min(e["first"], a["first"]); e["last"] = max(e["last"], a["last"])
        for t, v in a["other"].items(): e["other"][t] = e["other"].get(t, 0.0) + v
        if a.get("funding"): e["funding"] += a["funding"]; e["note"] = a.get("note", "")
    out = []
    for key, e in list(acc.items()):
        if now - e["last"] > BUY_ACCUM_H * 3600: acc.pop(key); continue
        e["value"] = e["counter_usd"] if e["counter_usd"] > 0 else e["usd"]
        e["unit"] = e["value"] / e["amount"] if e["amount"] else None
        if e["value"] >= BUY_ALERT_USD:
            e["accum"] = e["runs"] > 1; out.append(acc.pop(key))
    return sorted(out, key=lambda a: -a["value"])

def notify(new_events, holdings):
    """Telegram の設計（2026-09-08 藤沼さん要望で再整理）
       (1) 即時: 買い ≥ BUY_ALERT_USD（DexScreener 付き）、目立つ売り・外部流出 ≥ SELL_ALERT_USD、新ウォレット検出、複数クジラ同時買い
       (2) 日次: 1日1通、締め時刻（既定 24:00 JST）に「総資産の推移」と「≥ DAILY_BIG_USD の大きな動き（買い / 売り / 値動き）」だけ
       ※ 1時間ごとの「まとめ」と日次内の「新規銘柄の成績」は廃止（台帳ページで見る）"""
    names = P.role_names(wallets); pages = CFG.get("pages_url", "")
    cutoff = time.time() - NOTIFY_MAX_AGE_H * 3600
    recent = [e for e in new_events if e["ts"] >= cutoff]
    CTX["total_usd"] = snapshots[-1]["total"] if snapshots else None      # 買い・売りの「総資産の何%か」の分母（直近の残高時点）
    lines = []
    buys = accumulate_buys(P.group_trades(recent, "buy", CTX, 50.0), time.time())
    if buys: lines += P.buy_alert_lines(buys, names, CTX)
    sells = P.group_trades(recent, "sell", CTX, SELL_ALERT_USD)
    outs = P.group_trades([e for e in recent if P.bucket_of(e["chain"], e["contract"], CTX) == "risk"], "out", CTX, SELL_ALERT_USD)
    if sells or outs: lines += P.sell_alert_lines(sells, outs, names, CTX)
    for e in recent:
        if e["kind"] == "新ウォレット開設(ガス種銭)":
            lines.append(f"🆕 新ウォレット {names.get(e['cp'], '?')} を検出（{names.get(e['wallet'], '?')} から種銭 {e['amount']:.4f} {e['token']}）→ 監視に追加")
    if lines:
        if pages and "<" not in pages: lines.append(f"詳細: {pages}")
        tg("\n".join(lines))
    else:
        log("即時通知なし")
    try: cross_whale_alert(pages)
    except Exception as e: warn(f"同時買い検出でエラー: {e!r}")
    # 日次（1日1通）
    if state.get("daily_due"):
        due = state["daily_cut"]; pts = P.cutoff_points(snapshots)
        p1 = next((p for p in pts if p.get("cut") and p["ts"] == due), None) or (pts[-1] if pts else None)
        prev = [p for p in pts if p1 and p["ts"] < p1["ts"] and (p.get("cut") or p["label"].startswith("開始"))]
        if p1 and prev:
            br = P.bridge(prev[-1], p1, events, CTX, MOVE_MIN_USD)
            tg(P.daily_report_text(br, names, CTX, P.cut_date(due).strftime("%-m/%-d"), DAILY_BIG_USD, pages))
        else:
            tg(f"📊 日次レポート: 記録開始。次の締め（{P.cut_desc()}）から1日の推移と大きな動きを送ります。現在の総資産 {P.fmt_usd(snapshots[-1]['total']) if snapshots else '—'}")
        state["last_daily_cut"] = due
        state["last_daily"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# ------------------------------------------------------------ 複数クジラの同時買い（超重要コール）
def cross_whale_alert(pages):
    """他のクジラのリポジトリから直近の買いを集め、同じ日(JST)に同じ銘柄を2人以上が買っていたら緊急通知。
       送るのは「その銘柄を最後に買ったクジラ」のリポジトリだけ（重複送信防止）。人数が増えたら再通知"""
    me = WHALE_NAME or "本体"
    cut = time.time() - 2 * 86400
    groups = defaultdict(dict)     # (date, chain, contract) -> whale -> {usd, amount, last, sym, who}
    deliveries = defaultdict(list) # (chain, contract, cp) -> [(whale, ts, usd)]  同じ送り主から複数クジラへの配布（エアドロップ）検出用
    def add(name, evs, names):
        for a in P.group_trades([e for e in evs if e["ts"] >= cut], "buy", CTX, CROSS_MIN_USD):
            key = (P.jst(a["last"]).strftime("%Y-%m-%d"), a["chain"], a["contract"])
            g = groups[key].setdefault(name, {"usd": 0.0, "amount": 0.0, "last": 0, "sym": a["sym"], "who": set()})
            g["usd"] += a["value"]; g["amount"] += a["amount"]; g["last"] = max(g["last"], a["last"]); g["who"].add(names.get(a["wallet"], "?"))
        for e in evs:
            if e["ts"] >= cut and e["dir"] == "IN" and e["kind"] in ("購入(クロスチェーン)", "受取(原資未確認)", "受取(新規トークン)"):
                deliveries[(e["chain"], L(e["contract"]), e["cp"])].append((name, e["ts"], e.get("usd") or 0))
    add(me, events, P.role_names(wallets))
    for repo in PEER_REPOS:
        if repo.endswith("/" + (ROOT.name if ROOT.name.startswith("whale-") else "whale-tracker")): continue
        try:
            ev = [json.loads(l) for l in http_text(f"https://raw.githubusercontent.com/{repo}/main/data/events.jsonl").splitlines() if l.strip()]
            w = json.loads(http_text(f"https://raw.githubusercontent.com/{repo}/main/data/wallets.json") or "{}")
            cfg = json.loads(http_text(f"https://raw.githubusercontent.com/{repo}/main/config.json") or "{}")
            add(cfg.get("name") or repo.split("/")[-1].replace("whale-", ""), ev, P.role_names(w))
        except Exception as e:
            warn(f"同時買い検出: {repo} の読み込み失敗 {e!r}")
    alerted = set(tuple(x) for x in state.get("cross_alerted", []))
    airdrop_like = set()
    for (chain, contract, cp), lst in deliveries.items():
        whales = sorted(set(n for n, _, _ in lst))
        if len(whales) >= 2:
            ts = sorted(t for _, t, _ in lst)
            if ts[-1] - ts[0] <= 1800 and max(u for _, _, u in lst) < NEW_TOKEN_MAX_USD: airdrop_like.add((chain, contract))
    for (date, chain, contract), by in groups.items():
        if len(by) < 2: continue
        if (chain, contract) in airdrop_like: log(f"同時買い: {next(iter(by.values()))['sym']} は同じ送り主から複数クジラへ30分以内に少額配布 → エアドロップ扱いで除外"); continue
        latest = max(by, key=lambda n: by[n]["last"])
        if latest != me: continue
        key = (date, chain, contract, len(by))
        if key in alerted: continue
        sym = next(iter(by.values()))["sym"]
        lines = [f"🚨🚨 複数クジラが同日に購入【{P.symc(sym, chain)}】{date[5:].replace('-', '/')}", f"{len(by)}人が買い（合計 {P.fmt_usd(sum(g['usd'] for g in by.values()))}）:"]
        for n, g in sorted(by.items(), key=lambda kv: kv[1]["last"]):
            lines.append(f"  {n}（{'/'.join(sorted(g['who']))}） {P.fmt_qty(g['amount'])} ≈ {P.fmt_usd(g['usd'])}  {P.jst(g['last']).strftime('%H:%M')} JST")
        link = P.dex_link(chain, contract, CTX)
        if link: lines.append(link)
        if pages and "<" not in pages: lines.append(f"詳細: {pages}")
        tg("\n".join(lines)); alerted.add(key); log(f"🚨 同時買い通知: {sym} {date} {len(by)}人")
    state["cross_alerted"] = [list(k) for k in alerted if k[0] >= P.jst(cut).strftime("%Y-%m-%d")]

def http_text(url):
    r = S.get(url, timeout=60)
    return r.text if r.ok else ""

# ------------------------------------------------------------ HTML
def html(holdings_doc):
    from html_report import render
    hd = holdings_doc["holdings"] if isinstance(holdings_doc, dict) and "holdings" in holdings_doc else holdings_doc
    upd = holdings_doc.get("updated") if isinstance(holdings_doc, dict) else None
    out = DOCS / ("index.dryrun.html" if DRY_RUN else "index.html")
    pts = P.cutoff_points(snapshots, n=45)
    bridges = [P.bridge(pts[i - 1], pts[i], events, CTX, MOVE_MIN_USD) for i in range(1, len(pts))]
    newpos = P.new_positions(events, snapshots, CTX, days=int(CFG.get("new_positions_days", 14)), min_cost=BUY_LIST_MIN_USD)
    for g in newpos:   # 現在価格が無い銘柄は DexScreener で補う
        if not g["px"]:
            g["px"] = price(g["chain"], g["sym"], g["contract"]); g["liq"] = _liq.get((g["chain"], g["contract"]))
            if g["px"]: g["pnl_pct"] = (g["px"] / g["avg"] - 1) * 100; g["value"] = g["held"] * g["px"]
    matrix = P.token_matrix(snapshots, CTX, top_n=int(CFG.get("matrix_top_n", 20)), extra_keys=[g["key"] for g in newpos])
    timeline = {"points": pts, "bridges": bridges, "names": P.role_names(wallets), "ctx": CTX, "min_usd": MOVE_MIN_USD, "buy_list_min_usd": BUY_LIST_MIN_USD, "new_positions": newpos, "matrix": matrix}
    timeline["name"] = WHALE_NAME
    timeline["stale"] = holdings_doc.get("stale") if isinstance(holdings_doc, dict) else None
    timeline["cut_desc"] = P.cut_desc()
    render(CFG, CHAINS, wallets, events, hd, batches(events), THRESHOLD, label, out, holdings_updated=upd, timeline=timeline)
    log("HTML 生成:", out)

if __name__ == "__main__":
    if not NODEREAL_KEY and not ALCHEMY_BNB_KEY: warn("NODEREAL_KEY / ALCHEMY_BNB_KEY どちらも未設定（BSC は取得できません）")
    if not HELIUS_KEY: warn("HELIUS_KEY 未設定（Solana は取得できません）")
    if not BLOCKSCOUT_KEY: warn("BLOCKSCOUT_KEY 未設定（公開インスタンスの API を使うため 429 が出やすくなります）")
    if DRY_RUN: log("DRY_RUN: 通知・保存なし")
    for ts, lost in _artifacts:
        warn(f"索引障害と判定し履歴から除去: {P.jst(ts).strftime('%m-%d %H:%M')} JST の時点（{P.fmt_usd(lost)} 分の保有が前後に同枚数で存在するのに欠落）")
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
            for k in ("daily_due", "daily_cut", "holdings_refreshed"): state.pop(k, None)
            json.dump(state, open(DATA / "state.json", "w"), indent=2)
    html(holdings_doc)
