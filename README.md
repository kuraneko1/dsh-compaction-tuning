# dsh-compaction-tuning

DSH（DeepSeek Harness）のセッションが **数 step ごとにコンパクション（自動圧縮）を繰り返す**状態を、
実測から原因特定して設定で止めるスキルです。

## 症状

- 会話が数 step 進むたびに「圧縮中」になり、1 回 1〜2 分止まる
- 長時間セッションの稼働の 1/4 が要約に消える（実測: 213 分中 54 分・33 回）
- 圧縮してもすぐまた圧縮する（要約直後のコンテキストが閾値の近くまで戻っている）
- コンテキストウィンドウは大きいはずなのに、少し喋ると圧縮される

## 原因

`@deepseek-ai/dsh-compaction-basic` の閾値は次の式で決まります。

```
thresholdTokens = min(contextWindow * thresholdRatio,
                      contextWindow - reserved - headroomTokens)
```

ここで `reserved` は**そのセッションの直前リクエストの `maxTokens`**（出力予算ではなく予約）。
既定値は `headroomTokens = 65536`、`thresholdRatio = 0.8`、`retainRatio = 0.16`。

たとえば contextWindow 262,144 / maxTokens 131,000 のモデルでは

```
262144 - 131000 - 65536 = 65,608
```

262k の窓でも会話に使えるのは 65k だけ。しかも圧縮後は
「システム + ツール約 23k + 保持テール約 21k + 要約 6〜18k」= 50〜64k に着地するため、
数 step でまた閾値に達します。これが圧縮のドゥームループです。

## 直し方

プロファイルの `cordis.patch.yml` を 2 か所だけ変えます。

1. モデルの `maxTokens` を現実的な値に下げる（例: 131,000 → 16,384）。予約が減って閾値が上がります。
2. `compaction-basic` に `modelPolicies` を追加し、対象モデルだけ
   `headroomTokens` を下げ、`thresholdRatio` を上げ、`retainRatio` を下げる。

この例だと閾値は 65,608 → 209,715（3.2 倍）、保持テールは 21k → 約 9.8k になります。
詳しい手順・守るべき制約・検証方法は `skills/dsh-compaction-tuning/SKILL.md` にあります。

## インストール

```sh
git clone https://github.com/kuraneko1/dsh-compaction-tuning
cp -r dsh-compaction-tuning/skills/dsh-compaction-tuning ~/.dsh/skills/
```

DSH を再起動（またはスキル一覧の再読込）すると `dsh-compaction-tuning` が使えるようになります。
Codex など他のエージェントでも、`SKILL.md` をそのままスキルとして置けば動きます。

## 使い方

エージェントに「このセッション、すぐコンパクションするんだけど」と伝えるか、
`$dsh-compaction-tuning` を明示します。まず計測し、閾値の実数を出してから設定を直します。

## 計測スクリプト

```sh
python3 scripts/compaction_report.py <session-id>
python3 scripts/compaction_report.py ~/.dsh/sessions/<workspace>/session-<id>/session.v4.jsonl.zstd
```

出力: 圧縮回数・合計/平均所要時間・圧縮前後のトークン・閾値が一定かどうか。

## 実測例

| | 変更前 | 変更後 |
|---|---|---|
| 閾値トークン | 65,608 | 209,715 |
| 保持テール | 約 21k | 約 9.8k |
| 175 step あたりの圧縮回数 | 33 回（54 分） | 数回の見込み |

## 注意

- 既存セッションには最初のリクエストの `maxTokens` が焼き付いているため、効きが弱いです。新規セッションで確認してください。
- `retainRatio` は `thresholdRatio` より小さくしないとプラグインがロード時に落ちます。
- サーバの実際のコンテキスト長を超える閾値にしないでください。
- このスキルは DSH の設定を書き換えます。変更前の `cordis.patch.yml` を控えておくのが安全です。
