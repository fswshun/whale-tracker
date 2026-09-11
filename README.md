# whale-tracker

クジラウォレット群（本体＋自動検出した子・孫）の資産を 10 分ごとに追跡し、**買い（$5,000〜）と目立つ売り（$100,000〜）は即時 Telegram**、1日1通の日次レポート（24:00 JST）で総資産の推移と大きな動きを通知、GitHub Pages に資産レポートを公開します。

- 本体ウォレットの送受信を Robinhood Chain / BSC（毎回）＋ Ethereum / Base（1時間ごと）で取得
- 未知の EOA へのガス種銭・トークン送金を検出したら、その子ウォレットを自動で監視に追加（子・孫・曾孫…）
- 資産を **リスク資産（アルト）/ 準現金（ETH・BNB）/ 現金（ステーブル）** に分け、クラスター合算の推移を毎日の締め（24:00 JST）時点で記録
- 時点間の増減を **値動き / 利確（売り＋外部流出）/ 買い / 誤差** に分解して表示（`portfolio.py`）
- 表示は「本体 / 子1 / 子2 / 孫1」のみ。アドレスは出さない

### Telegram 通知の種類

すべての通知の先頭に `【クジラ名】` が付く（名前の無い第1クジラは「Nachi」）。

| 種類 | タイミング | 内容 |
|---|---|---|
| 🟢 買い | 検出した実行で即時（10分ごとの実行＋GitHub の遅延で、オンチェーンから 5〜20 分後） | `buy_alert_usd`（既定 $5,000）以上のリスク資産の買い。同一銘柄の分割買いは1行に集約し、**実行をまたぐ分割買いも 24 時間（`buy_accum_hours`）累計して $5,000 に達した時点で通知**（$1,200 × 5 回など）。支払い元・平均取得・DexScreener リンク付き |
| 🔻 売り | 即時 | `sell_alert_usd`（既定 $100,000）以上の目立つ売り。クラスター外への流出（取引所等）は以後追えず売却リスクが高いので**売り扱い**。買い・売りとも「総資産の何%か」を併記（10% 以上は「大口」） |
| 🚨🚨 同時買い | 即時 | 複数クジラが同日に同一銘柄を `cross_buy_min_usd`（既定 $5,000）以上ずつ購入 |
| 🆕 新ウォレット | 即時 | ガス種銭で新しい子/孫を検出 |
| 📊 日次レポート | 1日1通、締め時刻（`daily_report_hour_utc`、既定 15 UTC ＝ 24:00 JST） | その日の総資産の推移（値動き / 利確 / 買い の合計）と、`daily_big_move_usd`（既定 $100,000）以上の大きな動きだけ（買い / 売り・流出 / 値上がり / 値下がり） |

1時間ごとの「まとめ」と日次内の「新規銘柄の成績」は 2026-09-08 に廃止（台帳ページで見る）。

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
   | `HELIUS_KEY` | Solana 用。https://dashboard.helius.dev で無料登録 → API Key（Mainnet） |
   | `NODEREAL_KEY` | https://dashboard.nodereal.io で無料登録 → API Key を作成（BNB Chain / mainnet）→ URL `https://bsc-mainnet.nodereal.io/v1/<ここ>` の `<ここ>` 部分 |
   | `BLOCKSCOUT_KEY` | https://dev.blockscout.com で無料登録 → Create API Key → `proapi_…` で始まるキー（表示は1回だけ） |
   | `TG_TOKEN` | Telegram の @BotFather で `/newbot` して得るトークン |
   | `TG_CHAT` | 通知先チャット ID。ボットに何か1通送ってから `https://api.telegram.org/bot<TG_TOKEN>/getUpdates` をブラウザで開き `"chat":{"id":123456789` の数字 |
   | `TG_EXTRA`（任意） | 他の人にも同じ通知を送る場合の追加宛先。`[{"token":"<相手の Bot トークン>","chat":"<相手の chat_id>"}]` の JSON 配列。登録は `whale_repo/set_secret.py`（`.venv` に pynacl が必要） |
3. **Settings → Pages** で Source を `Deploy from a branch`、Branch を `main` / `/docs` にする
4. `config.json` の `pages_url` を自分の Pages URL に書き換える
5. **Actions** タブで `whale-tracker` を開き **Run workflow** を1回押す（以後は 15 分ごとに自動）

### ローカルで動作確認する場合

```bash
pip install -r requirements.txt
NODEREAL_KEY=xxxx BLOCKSCOUT_KEY=proapi_xxxx DRY_RUN=1 python tracker.py
```

`DRY_RUN=1` は通知もファイル保存もせず、通知文をコンソールに出し `docs/index.dryrun.html` を作ります。

## 複数のクジラを追う（1人＝1リポジトリ）

コードはこのリポジトリ1つ。クジラごとに **データ用リポジトリ**（`whale-<name>`：config.json / data / docs / ワークフロー）を持ち、ワークフローが毎回このリポジトリのコードを取得して実行します。修正はここに push すれば全員に行き渡ります。

- 立ち上げ: `python whale_repo/setup_whale.py --name unipcs --wallet 0x… --wallet <Solanaアドレス> --blockscout proapi_… --helius … --nodereal … --tg-token … --tg-chat … --gh-token ghp_…`（リポ作成 → push → Secrets → Pages → 初回実行まで自動）
- API キーはクジラごとに別アカウントで取得すると無料枠が人数分になる（Blockscout PRO 10万credits/日、NodeReal 1,000万CU/月、Helius 100万credits/月）
- Telegram は同じボット・同じチャットに `【name】` を頭に付けて送る（`config.json` の `name`）
- ローカル確認: `WHALE_ROOT=/path/to/whale-unipcs DRY_RUN=1 python tracker.py`

## Solana

- データ源は Helius（`HELIUS_KEY`、無料 100万credits/月・10 req/s）。署名一覧（1 credit）で新規 tx を検知し、新規分だけ Enhanced Transactions API（100 credits/回、100署名まで）で解析。残高は DAS `getAssetsByOwner`（10 credits）
- スワップ・送金・子ウォレット検出（SOL の種銭）・なりすまし判定（署名者≠本人）は EVM と同じロジック
- 準現金＝SOL/WSOL、現金＝USDC/USDT（本物のミントのみ）、価格は DexScreener の `solana`
- Solana アドレスは大文字小文字を区別するので、コード内では EVM だけ小文字化（`L()`）

## ファイル

| ファイル | 役割 |
|---|---|
| `tracker.py` | 本体。取得 → 分類 → 子ウォレット検出 → 残高 → スナップショット → Telegram → HTML |
| `portfolio.py` | クラスター合算の資産推移、増減分解（値動き / 利確 / 買い）、通知文面 |
| `html_report.py` | `docs/index.html` の生成（資産レポート＋折りたたみの詳細台帳） |
| `import_csv.py` | Blockscout / BscScan の CSV エクスポートを取り込む（過去分の初期投入・オフライン確認用） |
| `config.json` | 本体ウォレット・しきい値・相手先ラベル・手動価格 |
| `data/` | 状態（監視ウォレット一覧、処理済み tx、カーソル、イベント台帳、残高、`snapshots.jsonl`＝資産履歴、EOA判定保留）。Actions が自動でコミット |
| `docs/index.html` | 台帳 HTML（GitHub Pages で公開） |

## 調整できるところ（config.json）

- `threshold_usd`: 詳細台帳（折りたたみ）の表示しきい値（既定 10,000）
- `buy_alert_usd`: 買いの即時通知の最小額（既定 5,000。実行をまたぐ分割買いの累計でも可）
- `buy_list_min_usd`: 台帳の買い一覧・新規銘柄の成績に載せる最小額（既定 5,000。$1,000 は低すぎるため 2026-09-08 に引き上げ）
- `sell_alert_usd` / `daily_big_move_usd`: 売り・外部流出の即時通知、日次レポートの「大きな動き」の最小額（既定 100,000）
- `move_min_usd`: 台帳の売り・流出一覧に載せる最小額（既定 5,000）
- `daily_report_hour_utc`: 1日の締め＝日次レポートの時刻（既定 15 UTC = 24:00 JST。チャートの時点もこれに揃う）
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
- 受取(新規トークン)＝上場 24 時間以内の銘柄を、執行サービス経由 or 本人が何も払っていない相手から $20k 未満で受け取ったもの（クジラ向け宣伝エアドロップの疑い。買い・同時買いには数えない）
- なりすまし(偽送金)＝$1,000 以上の受取なのに `balanceOf` が 0（Transfer イベントだけで残高が動かない幻の送金）。台帳・流入・同時買いから除外。同銘柄を売った/送った形跡があれば本物扱い
- 新ウォレット＝未知 EOA へ $20 未満のネイティブ送金（ガス種銭）、または未知 EOA への単純トークン送金。検出した瞬間に監視へ追加し、親子関係を記録。EOA かどうか判定できなかった相手は `data/pending_eoa.json` に保留し次回再判定
- 本体保有比＝売却数量 ÷（売却後の本体残高＋売却数量）

## 初回に出やすいエラー

- `警告: NODEREAL_KEY 未設定` → Secrets の登録漏れ（BSC が取れない）
- `HTTP 429 'Too many requests…'` → `BLOCKSCOUT_KEY` 未設定で公開インスタンスの制限に当たっている
- `nr_getAssetTransfers: {...}` のエラー → NodeReal 側の制限。ログをそのまま貼ってください
- `取得失敗（次回に持ち越し）` → Blockscout が一時的に 500/403 を返した。カーソルは進めないので次回に自動で追いつく
- 価格が「未取得」のまま → DexScreener に上場していない銘柄。`manual_prices` に手で入れる
- 子ウォレットが増えすぎる → DEX ルーターやブリッジを EOA と誤判定している場合。そのアドレスを `labels` に入れれば止まる
- 受け取っただけのトークンが総資産の何割も占める → 異常トークン。`valuation_check` が $5,000 以上のリスク保有について 総供給（1e22 枚超）・時価総額（$200億超）・流動性（$3億超）・評価額>時価総額・DexScreener の FDV 桁あふれ・「買っていない受取のみで評価額が流動性の2倍超」を検査し、該当すれば 0 評価にして台帳の「評価から除外した銘柄」に理由付きで別掲（`valuation_check_usd` 等で調整）
- 保有銘柄が急に消えて総資産が落ちる → データ元の索引障害。前回 $1,000 以上あった銘柄が売り記録なしに消えた場合は `balanceOf` で実在を確認して復元し、確認できなければ前回値を持ち越してレポートに注記する（`vanish_check_usd` で閾値変更）

エラーはそのままログを貼ってもらえれば直します。
