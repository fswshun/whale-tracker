# whale-tracker 引き継ぎメモ（2026-09-07 時点）

## 構成
- **コード**: このリポジトリ（fswshun/whale-tracker）。`tracker.py`（取得→分類→通知→HTML）、`portfolio.py`（資産推移・増減分解・通知文）、`html_report.py`（ページ）、`services.json`（共有サービス/ボットのアドレス、全クジラ共通）
- **クジラごとのデータリポ**（ワークフローが毎回このリポのコードを checkout して実行、`WHALE_ROOT`）:
  - 本体 0xDE52…28C1 → このリポ自身（data/ docs/）: https://fswshun.github.io/whale-tracker/
  - unipcs → fswshun/whale-unipcs（EVM 0x0a6e… / SOL 2heJ…）: https://fswshun.github.io/whale-unipcs/
  - avast → fswshun/whale-avast（EVM 0xcc0c…(EIP-7702) / SOL 8xL8…）: https://fswshun.github.io/whale-avast/
  - kyle → fswshun/whale-kyle（EVM 0x39b3… / SOL EJ1i…）: https://fswshun.github.io/whale-kyle/
  - まとめ: https://fswshun.github.io/ （リポ fswshun/fswshun.github.io）
- **起動**: cron-job.org（藤沼さんアカウント）の4ジョブが10分ごとに GitHub の workflow_dispatch を叩く（GitHub 自身の cron は当てにならない）。ジョブID 8400025/8400072/8400073/8400074
- **データ源（全部無料枠）**: Blockscout PRO（Robinhood 4663 / ETH / Base / Arb）、NodeReal（本体・unipcs の BSC）、Alchemy（avast・kyle の BSC、internal tx 非対応）、Helius（Solana）、DexScreener（価格）
- **キー**: 各リポの GitHub Secrets。ローカル検証用の値は `keys.local.json`（gitignore 済み）

## 通知（Telegram @fswshun_whale_bot、chat 479438233、`[名前]` 付き）
- 🟢 買い ≥$5,000 即時（支払額ベース・平均取得・いまの価格・流動性・DexScreener）
- 🚨🚨 複数クジラが同日に同一銘柄を ≥$5,000 ずつ購入（最後に買ったクジラのリポが送信、人数が増えたら再送。同じ送り主から複数クジラへ30分以内の少額配布は除外）
- 🕐 1時間まとめ（動きがあった時だけ）、📊 毎朝 9:00 JST 日次（前日比の分解＋新規銘柄成績）

## 判定ルールの要点（罠の記録）
- 残高はコントラクトでキー管理（同名偽トークン対策）。Blockscout/Alchemy の一覧は漏れるので、取引履歴→価格あり→balanceOf で補完
- なりすまし: 本人が署名していない偽 Transfer（価格の無いトークン）。Solana のガスレス送金は本物扱い
- 購入(クロスチェーン): 7日以内に支払った相手からの受取（執行サービス 0x00AA系＝本体専用、0xb92f…＝unipcs/avast/kyle 共通）
- 受取(新規トークン): 上場24h以内・$20k未満・執行サービス経由・直前2hに支払い無し → エアドロップ疑い、買いに数えない
- Solana: 子は本体直下のみ、種銭 0.2 SOL 以上、共有ボット（services.json）は追わない、親ごと8件/実行・全体40件上限
- 実行は10分ごと。5分にすると無料枠を超える。1回の実行は3〜7分

## ローカルでの作業手順
1. `git pull`（Actions がデータをコミットしているので必ず先に）
2. 修正 → `python3 -m py_compile tracker.py portfolio.py html_report.py`
3. 検証: `DRY_RUN=1 BLOCKSCOUT_KEY=… NODEREAL_KEY=… HELIUS_KEY=… python3 tracker.py`（他クジラは `WHALE_ROOT=<そのリポの clone>`）
4. commit → push（各クジラのリポは次の実行で自動的に最新コードを使う）
5. GitHub 操作（Secrets/Pages/dispatch）は classic PAT（9/13 失効、要再発行）。起動用の細粒度 PAT は cron-job.org 側に設定済み（無期限）
