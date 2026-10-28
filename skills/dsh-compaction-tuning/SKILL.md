---
name: dsh-compaction-tuning
description: "DSH のセッションが数 step ごとにコンパクション（自動圧縮）を繰り返す状態を、実測から原因特定して設定で止める。圧縮回数 / 圧縮に溶ける時間 / 閾値トークン数を出し、compaction-basic の headroom・thresholdRatio・retainRatio とモデルの maxTokens を調整する。「すぐコンパクションする」「圧縮が多すぎる」「context がすぐ埋まる」「要約ばかり走る」と言われたとき、または長いセッションが圧縮で止まるときに使う。"
---

# コンパクション多発の診断と停止

**まず測る。しきい値を勘でいじらない。** DSH の自動圧縮は 1 回 80〜120 秒かかるので、
5 step ごとに走っていると稼働の 1/4 が要約に消える。

## 1. 測る

```sh
python3 scripts/compaction_report.py <session-id or session.v4.jsonl.zstd>
# rdsh 環境なら: rdsh-session-timing <session-id の先頭8文字>
```

見る値:

| 値 | 異常 | 意味 |
|---|---|---|
| 圧縮回数 | ≥ 5、または steps/圧縮 < 15 | 閾値が低すぎる、または着地点が高すぎる |
| 圧縮の所要時間 | 1 回 60 秒超 | 要約モデルが重い / 要約出力が巨大 |
| 圧縮前トークン | 毎回ほぼ同値（例 65〜70k） | 閾値が固定で、そこにすぐ戻っている |
| 圧縮後トークン | 閾値の 80% 以上 | 保持テール + 要約 + システムで埋まっている |

「圧縮前 ≈ 圧縮後 ≈ 閾値」なら**ドゥームループ**。設定で必ず直る。

## 2. 閾値の式を知る

`@deepseek-ai/dsh-compaction-basic` の実装（`lib/index.js` の `resolveCompactSpec`）:

```
messageBudget   = contextWindow - reserved
pressureBudget  = messageBudget - headroom
thresholdTokens = floor(min(contextWindow * thresholdRatio, pressureBudget))
retainTokens    = messageBudget * retainRatio     # 圧縮後に残すテール
```

- `reserved` = **そのセッションの直前リクエストの `maxTokens`**（`session.requestHeader().config.maxTokens`）。
  モデル設定の `maxTokens` がそのまま予約になり、出力予算ではない。
- 既定値: `headroomTokens = 65536`、`thresholdRatio = 0.8`、`retainRatio = 0.16`、
  `maxTokens`（要約の上限）は headroom と同じ。

**典型的な罠**: contextWindow 262,144 / maxTokens 131,000 のモデルだと

```
262144 - 131000 - 65536 = 65,608
```

262k の窓なのに会話に使えるのは 65k だけ。しかも圧縮後は
「システム + ツール 約 23k + 保持テール 131k×0.16 ≈ 21k + 要約 6〜18k」= 50〜64k に着地するので、
2〜4 step で再発火する。

## 3. 直す（プロファイルの patch 2 か所）

`~/.dsh/profiles/<profile>/cordis.patch.yml` に追記する。patch は行の config を**丸ごと差し替える**ので、
その行に必要なキーは全部書く。

```yaml
# 1) 予約出力を現実的な値に（これが本体。131000 → 16384 で閾値が 3 倍になる）
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      myprovider:
        models:
          - id: my-model
            contextWindow: 262144
            maxTokens: 16384

# 2) 圧縮ポリシー（対象モデルだけ。他モデルは既定のまま）
- id: compaction-basic
  name: "@deepseek-ai/dsh-compaction-basic"
  config:
    modelPolicies:
      - provider: myprovider
        model: my-model
        maxTokens: 16384     # 要約の出力上限も兼ねる
        headroomTokens: 16384
        thresholdRatio: 0.85
        retainRatio: 0.04
```

結果: 閾値 `min(262144×0.85, 262144−16384−16384) = 209,715`、保持テール約 9.8k。
175 step で 33 回だった圧縮が数回になる。

守ること:

- `retainRatio < thresholdRatio`。破るとプラグインがロード時に落ちる（圧縮だけ無効になる）。
- `headroom + reserved < contextWindow`。サーバの実際の n_ctx を超える閾値にしない。
- モデルの `maxTokens` は実測出力の 4 倍以上残す（実測 1,600/step なら 16k で十分）。
- 別プロバイダに影響させたくないなら、必ず `modelPolicies` で対象を限定する。

## 4. 反映と検証

- `dsh-hmr` が `cordis.patch.yml` を watch して `reconcileProfile()` するので、touch で再起動なしに反映される。
  効かない環境ではプロセス再起動（設定は起動時に読む版もある）。
- **既存セッションは効きが弱い**: `requestHeader` に最初のリクエストの `maxTokens` が焼き付いているため、
  予約は古い値のまま。閾値は `min(contextWindow×ratio, contextWindow − 旧予約 − headroom)` までしか上がらない。
  完全に効かせるには新規セッションで回す。
- 検証は新しいセッションで `compaction_report.py` を再実行し、回数と圧縮前トークンを見る。

## 5. 設定を触らない選択肢

- **モデルを大きな窓のものに変える**: 1M 窓 / maxTokens 384k なら閾値は約 550k になり、実質圧縮は起きない。
- **プラグインで `maxTokens` を丸める**: `agent/request` waterfall で config を差し替えると予約だけ下げられる
  （compaction の閾値そのものを差し替えるフックは無い）。headroom 65,536 は plugin から動かせないため、
  設定版より天井が低い。

## 6. 直したあと

- 要約が依然 8〜18KB と巨大なら、policy の `maxTokens` を下げる（要約の出力上限）。
- 圧縮回数が減っても速度が戻らないなら、原因は別（step 数・モデル）。`session-speed` の手順へ。
