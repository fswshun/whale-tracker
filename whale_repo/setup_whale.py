#!/usr/bin/env python3
"""
setup_whale.py — クジラ1人分のデータ用リポジトリを GitHub に作って稼働させる

  python whale_repo/setup_whale.py --name unipcs \
      --wallet 0x0a6e... --wallet 2heJbC32... \
      --blockscout proapi_xxx --helius xxx --nodereal xxx \
      --tg-token xxx --tg-chat 123 --gh-token ghp_xxx [--owner fswshun] [--repo whale-unipcs]

やること: リポジトリ作成 → 初期ファイル push → Secrets 登録 → Pages 有効化 → 初回実行
必要: pip install pynacl requests
"""
import argparse, base64, json, os, subprocess, sys, tempfile, time
from pathlib import Path
import requests

ap = argparse.ArgumentParser()
ap.add_argument("--name", required=True); ap.add_argument("--wallet", action="append", required=True)
ap.add_argument("--blockscout", default=""); ap.add_argument("--helius", default=""); ap.add_argument("--nodereal", default=""); ap.add_argument("--alchemy", default="")
ap.add_argument("--tg-token", default=""); ap.add_argument("--tg-chat", default="")
ap.add_argument("--gh-token", required=True); ap.add_argument("--owner", default="fswshun"); ap.add_argument("--repo", default="")
ap.add_argument("--code-repo", default="fswshun/whale-tracker")
a = ap.parse_args()
repo = a.repo or f"whale-{a.name}"
H = {"Authorization": f"Bearer {a.gh_token}", "Accept": "application/vnd.github+json"}
API = "https://api.github.com"
tmpl = Path(__file__).resolve().parent

# 1) リポジトリ作成（public: Pages 無料の条件）
r = requests.get(f"{API}/repos/{a.owner}/{repo}", headers=H, timeout=30)
if r.status_code == 404:
    r = requests.post(f"{API}/user/repos", headers=H, json={"name": repo, "private": False, "auto_init": False, "description": f"whale-tracker data: {a.name}"}, timeout=30)
    print("repo create:", r.status_code, r.json().get("html_url") or r.text[:200]); time.sleep(3)
else:
    print("repo exists:", r.json().get("html_url"))

# 2) 初期ファイルを push
d = Path(tempfile.mkdtemp(prefix=f"whale-{a.name}-"))
(d / "data").mkdir(); (d / "docs").mkdir(); (d / ".github" / "workflows").mkdir(parents=True)
(d / ".github" / "workflows" / "track.yml").write_text((tmpl / "track.yml").read_text().replace("fswshun/whale-tracker", a.code_repo))
cfg = {"name": a.name, "main_wallets": a.wallet, "chains": [], "threshold_usd": 10000, "buy_alert_usd": 50000, "move_min_usd": 5000, "buy_list_min_usd": 5000,
       "price_move_alert_pct": 5, "daily_report_hour_utc": 15, "pages_url": f"https://{a.owner}.github.io/{repo}/", "initial_lookback_hours": 120,
       "holdings_every_n_runs": 6, "secondary_chains_every_n_runs": 6, "primary_chains": ["robinhood", "bsc", "solana"], "notify_max_age_hours": 48,
       "child_detect_max_age_days": 30, "labels": {}, "manual_prices": {}}
(d / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
(d / "data" / ".gitkeep").write_text(""); (d / "docs" / "index.html").write_text(f"<!doctype html><meta charset=utf-8><title>{a.name} 資産レポート</title><p>初回実行待ち…</p>")
(d / ".gitignore").write_text("__pycache__/\ncode/\ndocs/index.dryrun.html\n")
(d / "README.md").write_text(f"# whale-{a.name}\n\n{a.name} のクジラ追跡データ。コードは https://github.com/{a.code_repo} を毎回取得して実行。\n\n台帳: https://{a.owner}.github.io/{repo}/\n")
auth = base64.b64encode(f"x-access-token:{a.gh_token}".encode()).decode()
def git(*args): return subprocess.run(["git", "-c", f"http.extraheader=AUTHORIZATION: basic {auth}", *args], cwd=d, capture_output=True, text=True)
git("init", "-q", "-b", "main"); git("add", "-A")
subprocess.run(["git", "-c", "user.name=whale-setup", "-c", "user.email=setup@users.noreply.github.com", "commit", "-qm", f"init {a.name}"], cwd=d)
git("remote", "add", "origin", f"https://github.com/{a.owner}/{repo}.git")
p = git("push", "-u", "origin", "main"); print("push:", "OK" if p.returncode == 0 else p.stderr[-300:])

# 3) Secrets
from nacl import encoding, public
pk = requests.get(f"{API}/repos/{a.owner}/{repo}/actions/secrets/public-key", headers=H, timeout=30).json()
box = public.SealedBox(public.PublicKey(pk["key"].encode(), encoding.Base64Encoder()))
for name, val in [("BLOCKSCOUT_KEY", a.blockscout), ("HELIUS_KEY", a.helius), ("NODEREAL_KEY", a.nodereal), ("ALCHEMY_BNB_KEY", a.alchemy), ("TG_TOKEN", a.tg_token), ("TG_CHAT", a.tg_chat)]:
    if not val: continue
    enc = base64.b64encode(box.encrypt(val.encode())).decode()
    r = requests.put(f"{API}/repos/{a.owner}/{repo}/actions/secrets/{name}", headers=H, json={"encrypted_value": enc, "key_id": pk["key_id"]}, timeout=30)
    print("secret", name, r.status_code)

# 4) Pages
r = requests.post(f"{API}/repos/{a.owner}/{repo}/pages", headers=H, json={"source": {"branch": "main", "path": "/docs"}}, timeout=30)
print("pages:", r.status_code, (r.json().get("html_url") if r.headers.get("content-type", "").startswith("application/json") else "")[:80])

# 5) 初回実行
time.sleep(5)
r = requests.post(f"{API}/repos/{a.owner}/{repo}/actions/workflows/track.yml/dispatches", headers=H, json={"ref": "main"}, timeout=30)
print("dispatch:", r.status_code, "→ https://github.com/%s/%s/actions" % (a.owner, repo))
