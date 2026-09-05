# whale-tracker

クジラウォレットの資金フローを 15 分ごとに自動追跡し、Telegram に通知、GitHub Pages に台帳 HTML を公開します。

- 本体ウォレットの送受信を Robinhood Chain / BSC（＋活動があれば Ethereum / Base / Arbitrum）で取得
- 未知の EOA へのガス種銭・トークン送金を検出したら、その子ウォレットを自動で監視に追加（子・孫・曾孫…）
- 売却バッチ（TWAP）、内部移動、購入（クロスチェーン含む）、代金戻り、新ウォレットを分類
- 現在残高を全チェーン合算して USD 換算し、「売った量が本体保有の何%か」を表示

## データ取得元（すべて無料枠）

| チェーン | 取得元 | キー |
|---|---|---|
| Robinhood Chain / Ethereum / Base / Arbitrum | Blockscout PRO API（`api.blockscout.com`、1キーで全チェーン） | `BLOCKSCOUT_KEY`（無料枠 100K credits/日・5 RPS） |
| BSC | NodeReal MegaNode（BNB Chain 公式推奨の BscScan 後継） | `NODEREAL_KEY`（無料プラン 10M CU/月） |
| 価格 | DexScreener ＋ Blockscout の exchange_rate | 不要 |

- Etherscan API は 2026 年時点で BSC / Base が有料プラン限定になったため使いません
- `BLOCKSCOUT_KEY` が無い場合は各チェーンの公開インスタンス（`robinhoodchain.blockscout.com` 等）を叩きますが、公開 API は 429（Too many requests）が頻発するので実運用ではキー必須

## セットアップ

1. このフォルダを GitHub の新規リポジトリ（例: `whale-tracker`、**public**）に push する
   - GitHub Pages は無料プランでは public リポジトリのみ。台帳 HTML（ウォレットアドレスと分類結果）が公開されることに注意。非公開にしたい場合は Pages を使わず `docs/index.html` をダウンロードして見る
2. リポジトリの **Settings → Secrets and variables → Actions** に以下を登録
   | Secret | 内容 |
   |---|---|
   | `NODEREAL_KEY` | https://dashboard.nodereal.io で無料登録 → API Key を作成（BNB Chain / mainnet）→ URL `https://bsc-mainnet.nodereal.io/v1/<ここ>` の `<ここ>` 部分 |
   | `BLOCKSCOUT_KEY` | https://dev.blockscout.com で無料登録 → Create API Key → `proapi_…` で始まるキー（表示は1回だけ） |
   | `TG_TOKEN` | Telegram の @BotFather で `/newbot` して得るトークン |
   | `TG_CHAT` | 通知先チャット ID。ボットに何か1通送ってから `https://api.telegram.org/bot<TG_TOKEN>/getUpdates` をブラウザで開き `"chat":{"id":123456789` の数字 |
3. **Settings → Pages** で Source を `Deploy from a branch`、Branch を `main` / `/docs` にする
4. `config.json` の `pages_url` を自分の Pages URL に書き換える
5. **Actions** タブで `whale-tracker` を開き **Run workflow** を1回押す（以後は 15 分ごとに自動）

### ローカルで動作確認する場合

```bash
pip install -r requirements.txt
NODEREAL_KEY=xxxx BLOCKSCOUT_KEY=proapi_xxxx DRY_RUN=1 python tracker.py
```

`DRY_RUN=1` は通知もファイル保存もせず、通知文をコンソールに出し `docs/index.dryrun.html` を作ります。

## ファイル

| ファイル | 役割 |
|---|---|
| `tracker.py` | 本体。取得 → 分類 → 子ウォレット検出 → 残高 → Telegram → HTML |
| `html_report.py` | `docs/index.html` の生成 |
| `import_csv.py` | Blockscout / BscScan の CSV エクスポートを取り込む（過去分の初期投入・オフライン確認用） |
| `config.json` | 本体ウォレット・しきい値・相手先ラベル・手動価格 |
| `data/` | 状態（監視ウォレット一覧、処理済み tx、カーソル、イベント台帳、残高スナップショット、EOA判定保留）。Actions が自動でコミット |
| `docs/index.html` | 台帳 HTML（GitHub Pages で公開） |

## 調整できるところ（config.json）

- `threshold_usd`: 通知・主要イベントのしきい値（既定 10,000）
- `labels`: 相手先アドレスに名前を付ける。ここに載せたアドレスは「サービス」扱いになり、子ウォレットとして誤登録されない
- `manual_prices`: DexScreener に無い銘柄の価格を手で指定 `{"STRATTON": 0.002}`
- `chains`: 空にすると初回実行で自動探索。手で `["robinhood","bsc"]` と書いてもよい
- `initial_lookback_hours`: 初回・新ウォレット追加時に何時間遡るか（既定 120）
- `holdings_every_n_runs`: 残高更新の間隔（既定 4 ＝ 1時間ごと。無料枠節約）
- `secondary_chains_every_n_runs`: Ethereum / Base / Arbitrum の取得間隔（既定 4 ＝ 1時間ごと）。`primary_chains`（既定 robinhood, bsc）は毎回
- `notify_max_age_hours`: これより古いイベントは通知しない（既定 48。初回バックフィルの通知洪水防止）
- `child_detect_max_age_days`: これより古い送金からは子ウォレットを起こさない（既定 30）

## 判定ルール

- 売却＝トークン OUT ＋ 同一 tx でネイティブ/ステーブル IN。代金が内部 tx で戻るものも捕捉
- 購入(クロスチェーン)＝サービス（`0x00AA…` 等）から受取、かつ 7 日以内に監視ウォレットのどれかが同じ相手へ原資を送金。原資が見つからなければ「受取(原資未確認)」（黄）
- 新ウォレット＝未知 EOA へ $20 未満のネイティブ送金（ガス種銭）、または未知 EOA への単純トークン送金。検出した瞬間に監視へ追加し、親子関係を記録。EOA かどうか判定できなかった相手は `data/pending_eoa.json` に保留し次回再判定
- 本体保有比＝売却数量 ÷（売却後の本体残高＋売却数量）

## 初回に出やすいエラー

- `警告: NODEREAL_KEY 未設定` → Secrets の登録漏れ（BSC が取れない）
- `HTTP 429 'Too many requests…'` → `BLOCKSCOUT_KEY` 未設定で公開インスタンスの制限に当たっている
- `nr_getAssetTransfers: {...}` のエラー → NodeReal 側の制限。ログをそのまま貼ってください
- `取得失敗（次回に持ち越し）` → Blockscout が一時的に 500/403 を返した。カーソルは進めないので次回に自動で追いつく
- 価格が「未取得」のまま → DexScreener に上場していない銘柄。`manual_prices` に手で入れる
- 子ウォレットが増えすぎる → DEX ルーターやブリッジを EOA と誤判定している場合。そのアドレスを `labels` に入れれば止まる

エラーはそのままログを貼ってもらえれば直します。
