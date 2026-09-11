#!/usr/bin/env python3
"""GitHub Actions の Secret を複数リポジトリへ一括登録する（値はファイルから読み、コマンド行やログに出さない）。

使い方:
  .venv/bin/python whale_repo/set_secret.py --name TG_EXTRA --value-file /path/to/tg_extra.json
  .venv/bin/python whale_repo/set_secret.py --name TG_EXTRA --value-file … --repos whale-tracker,whale-unipcs

- GitHub のトークンは keys.local.json の github_classic_pat_* を使う（repo スコープ）
- 暗号化は GitHub 指定の libsodium sealed box（pynacl）。`python3 -m venv .venv && .venv/bin/pip install pynacl requests`
- TG_EXTRA の値の形: [{"token": "<BotFather のトークン>", "chat": "<chat_id>"}]（複数可）
"""
import argparse, base64, json, sys
from pathlib import Path

import requests
from nacl import encoding, public

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPOS = ["whale-tracker", "whale-unipcs", "whale-avast", "whale-kyle"]
API = "https://api.github.com"

ap = argparse.ArgumentParser()
ap.add_argument("--name", required=True, help="Secret 名（例: TG_EXTRA）")
ap.add_argument("--value-file", required=True, help="値を書いたファイル（内容をそのまま登録）")
ap.add_argument("--repos", default=",".join(DEFAULT_REPOS), help="カンマ区切りのリポ名")
ap.add_argument("--owner", default="fswshun")
ap.add_argument("--delete", action="store_true", help="登録ではなく削除する")
a = ap.parse_args()

keys = json.load(open(ROOT / "keys.local.json"))
pat = next((v for k, v in keys.items() if k.startswith("github_classic_pat")), None)
if isinstance(pat, dict): pat = pat.get("token") or next(iter(pat.values()))
if not pat: sys.exit("keys.local.json に github_classic_pat_* が無い")
H = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json", "User-Agent": "whale-tracker-setup"}
value = Path(a.value_file).read_text().strip()
if a.name == "TG_EXTRA" and not a.delete:
    lst = json.loads(value)                      # 形式チェック（壊れた JSON を登録すると全クジラの通知が警告だらけになる）
    assert isinstance(lst, list) and all(d.get("token") and d.get("chat") for d in lst), "TG_EXTRA は [{token, chat}, ...] の配列"

for repo in [r.strip() for r in a.repos.split(",") if r.strip()]:
    url = f"{API}/repos/{a.owner}/{repo}/actions/secrets/{a.name}"
    if a.delete:
        r = requests.delete(url, headers=H, timeout=30); print(f"{repo}: delete {a.name} → {r.status_code}"); continue
    pk = requests.get(f"{API}/repos/{a.owner}/{repo}/actions/secrets/public-key", headers=H, timeout=30)
    if not pk.ok: print(f"{repo}: public-key 取得失敗 {pk.status_code} {pk.text[:100]}"); continue
    pk = pk.json()
    box = public.SealedBox(public.PublicKey(pk["key"].encode(), encoding.Base64Encoder()))
    enc = base64.b64encode(box.encrypt(value.encode())).decode()
    r = requests.put(url, headers=H, json={"encrypted_value": enc, "key_id": pk["key_id"]}, timeout=30)
    print(f"{repo}: set {a.name} → {'OK' if r.status_code in (201, 204) else f'NG {r.status_code} {r.text[:100]}'}")
