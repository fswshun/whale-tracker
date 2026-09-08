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

## 通知（Telegram @fswshun_whale_bot、chat 479438233、先頭に `【名前】`。第1クジラは「メインクジラ」）
- 🟢 買い ≥$5,000 即時（支払額ベース・平均取得・いまの価格・流動性・DexScreener）。実行をまたぐ分割買いは `accumulate_buys` が `state.buy_accum` に 24h 累計し、$5,000 に達した回で「（分割買いの累計）」付きで通知。台帳の買い一覧・新規銘柄も $5,000 以上のみ（`buy_list_min_usd`、$1,000 は藤沼さんが「低すぎる」）
- 🔻 売り ≥$100,000 即時（`sell_alert_usd`）。外部流出は売り扱い（藤沼さん判断: 追跡不能＝売却リスク高、安全側に倒す）。買い・売り・日次の大きな動きに「総資産の X%」を併記、10% 以上は「大口」（`share_text`、分母は直近スナップショットの総資産）
- 🚨🚨 複数クジラが同日に同一銘柄を ≥$5,000 ずつ購入（最後に買ったクジラのリポが送信、人数が増えたら再送。同じ送り主から複数クジラへ30分以内の少額配布は除外）
- 📊 日次: 1日1通、締め 24:00 JST（`daily_report_hour_utc`=15）。総資産の推移 ＋ ≥$100,000 の大きな動き（買い / 売り・流出 / 値上がり / 値下がり）だけ（`daily_big_move_usd`）
- **廃止（2026-09-08 藤沼さん要望）**: 1時間まとめ、日次内の新規銘柄成績。締め時刻は `portfolio.CUT_OFF` で時点計算（チャート・日次・剪定）と共有。日次の送信済み判定は `state.last_daily_cut`（締め時刻の epoch）

## 判定ルールの要点（罠の記録）
- 残高はコントラクトでキー管理（同名偽トークン対策）。Blockscout/Alchemy の一覧は漏れるので、取引履歴→価格あり→balanceOf で補完
- なりすまし: 本人が署名していない偽 Transfer（価格の無いトークン）。Solana のガスレス送金は本物扱い
- 購入(クロスチェーン): 7日以内に支払った相手からの受取（執行サービス 0x00AA系＝本体専用、0xb92f…＝unipcs/avast/kyle 共通）
- 受取(新規トークン): 上場24h以内・$20k未満・執行サービス経由・直前2hに支払い無し → エアドロップ疑い、買いに数えない
- Solana: 子は本体直下のみ、種銭 0.2 SOL 以上、共有ボット（services.json）は追わない、親ごと8件/実行・全体40件上限
- **幻の送金（2026-09-07 発見）**: XXX / stonkscat / ZCAT / BREWCAT / fomocat / sue は、同じ送り主が上場直後に複数クジラへ同額（$12k 相当）を数分おきに連投する宣伝スパムで、Transfer イベントだけ発行して残高は増えない（本体の balanceOf=0）。対策 = ①素の「受取」も上場24h以内なら「受取(新規トークン)」 ②$1,000 以上の受取は `verify_phantom_receipts` が balanceOf を1回叩き、0 なら「なりすまし(偽送金)」（台帳・流入・同時買いから除外）。売った/送った形跡（同銘柄の OUT）があれば本物扱い
- **幻の全売却（2026-09-08 発見）**: Blockscout が `/addresses/{addr}/tokens` に 503 を返すと tokentx 差引にフォールバックするが、PRO API は `page×offset ≤ 10000` で履歴が打ち切られ、新しく買った銘柄が丸ごと欠ける。kyle の AMC $514K・AI $417K が消えて総資産が 37% 落ちて見えた（実際は保有継続・売却イベントも無し）。対策 = ①履歴が打ち切られたら残高更新を見送り前回値を持ち越す ②前回 $1,000 以上あった銘柄が売り記録なしに消えたら `guard_vanished_positions` が balanceOf で実在確認して復元（照会も失敗したら前回値を持ち越し）③前後の時点に同枚数で存在するのに1点だけ欠けている時点は `drop_artifact_snapshots` が履歴から自動除去 ④持ち越した時はレポート冒頭に注記。**残高の一覧APIは信用せず、消えた時はチェーンに直接聞くのが鉄則**
- **慢性的な索引漏れ**: kyle の Base の BLUECHIP（1,821万枚 ≈ $268K）は9/8の大半の時点で一覧に載らず、総資産を約 8% 過小表示していた。原因＝残高一覧のページ上限（12ページ）と、補完 `bs_reconcile_holdings` が「価格の付く欠落銘柄を先頭から25件」しか確認していなかったこと。対策＝ページ上限を 25 に、補完は**金額の大きい順**に 40 件確認。修正の反映後は総資産が一段上がるので、その回の「誤差」は一時的に大きくなる
- **評価額の妥当性検査（2026-09-09、藤沼さん指摘: unipcs の Monkey が $2.9M＝リスク資産の 12% に化けていた。FOMO では $2,000）**: Monkey は総供給 1e76 枚・decimals 0・受け取っただけ・流動性 $67K の薄いプール（Monkey/XAUt）で価格が2日で30倍動く異常トークン。**流動性で機械的に上限をかけるのは NG**（本当に買った AMC は評価/流動性 14 倍、MarsCoin 5 倍で、時価総額とは整合）。対策＝`valuation_check` が $5,000 以上のリスク保有に5つの検査: ①FDV=0 かつ mcap>0（桁あふれ）②時価総額 $200億超 ③1ペア流動性 $3億超 ④評価額 > 時価総額 ⑤総供給 1e22 枚超、＋ 買っていない受取のみ/非ASCIIシンボルで評価額が流動性の2倍超 → `usd=0`（除外）にして `excluded` に理由、台帳に「評価から除外した銘柄」として別掲。unipcs で Monkey（FDV 桁あふれ）・SPYB（mcap $3,320億の偽装）・🔶（非ASCII×流動性4倍）が除外され、PONS/USELESS/MarsCoin/AMC/BASECAT は残ることを確認済み
- 残高更新の `need` 判定は取得ループと同じ条件（`ch in wallets[w]["chains"] or 本体`）にしてある。ずらすと「欠けキーあり」で毎回残高更新が走り、Blockscout 50 回超/実行・所要 8 分・10分刻みスナップショットになる（本体で発生していた）
- 実行は10分ごと。5分にすると無料枠を超える。1回の実行は2〜5分（残高更新回は長め）

## ローカルでの作業手順
1. `git pull`（Actions がデータをコミットしているので必ず先に）
2. 修正 → `python3 -m py_compile tracker.py portfolio.py html_report.py`
3. 検証: `DRY_RUN=1 BLOCKSCOUT_KEY=… NODEREAL_KEY=… HELIUS_KEY=… python3 tracker.py`（他クジラは `WHALE_ROOT=<そのリポの clone>`）
4. commit → push（各クジラのリポは次の実行で自動的に最新コードを使う）
5. GitHub 操作（Secrets/Pages/dispatch）は classic PAT（9/13 失効、要再発行）。起動用の細粒度 PAT は cron-job.org 側に設定済み（無期限）
