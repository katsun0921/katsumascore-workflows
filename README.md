# katsumascore-workflows

KatsumaScoreの各リポジトリで共通に使う、GitHub Actionsの再利用可能ワークフローを置く。
呼び出し側の公開リポジトリ（katsumascore-front・vod_scraping_api）からも使えるよう、このリポジトリは公開にしている。秘密値は置かない。

## release-tag（`.github/workflows/release-tag.yml`）

PRが対象のブランチにマージされたら、次の版のタグ（`vMAJOR.MINOR.PATCH`）とGitHub Releaseを作る。

### 使い方

[examples/release-tag-caller.yml](examples/release-tag-caller.yml) を、呼び出し側の `.github/workflows/release-tag.yml` に置く。

- `uses:` の `@` の後ろは、このリポジトリのコミットSHA（40桁）で固定する。更新するときは、呼び出し側のPRでSHAを上げる。
- 起動は `pull_request_target`（`types: [closed]`）にする。`pull_request` では、forkやDependabotのPRで `GITHUB_TOKEN` が読み取り専用になり、作れない。共通ワークフローは呼び出し側のコードを実行しないので、PRのコードは動かない。
- 呼び出し側のジョブに `permissions: contents: write` を付ける。`concurrency` は付けない（共通ワークフローのジョブが持つ）。

| 入力 | 既定 | 意味 |
|---|---|---|
| `branch` | `main` | 版を打つ対象のブランチ。`on.pull_request_target.branches` と同じにする |
| `major-label` | `major` | 1桁目を上げるPRのラベル名 |
| `minor-label` | `minor` | 2桁目を上げるPRのラベル名 |
| `first-version` | `v1.0.0` | 正式な版のタグがまだないときの最初の版 |

### 動き

- 既定はパッチを上げる（`v1.0.0`→`v1.0.1`）。`minor-label`で2桁目、`major-label`で1桁目を上げる（両方あれば1桁目）。**ラベルはマージする前に付ける。**
- 数えるのは `vMAJOR.MINOR.PATCH` の形のタグだけ（`v1.1.0-rc1`・`latest`は数えない）。数字で比べる（`v1.10.0`は`v1.9.0`より新しい）。注釈付きタグは、指すコミットで照合する。
- 対象のコミットに正式な版のタグがあれば、新しい版は作らない。Releaseがなければ、そのタグで作る（再実行で直る）。
- タグは先に、上書きなしで作る。同名のタグが別のコミットを指していたら、Releaseを作らずに失敗させる。
- 同名のReleaseが下書きのままなら、公開されていないので失敗させる（公開か削除してから再実行する）。
- 公開済みの最新より古い版を補うときは、`Latest` を巻き戻さない（`--latest=false`）。
- 続けてマージした実行は、リポジトリごとに順に待つ（最大100件）。順序はマージ順と限らない。
  - 後にマージした実行が先に版を打ったときは、古いコミットに新しい版を作らずに終わる（その変更は新しい版に含まれている）。
  - その古いPRに`minor-label`・`major-label`があったときは、桁上げが失われるので実行を失敗にする。必要なら手動で版を作る。
  - 最新の版と対象のコミットの履歴が分かれている（ブランチの書き換えなど）ときは、失敗にする。

## dependabot-auto-merge（`.github/workflows/dependabot-auto-merge.yml`）

DependabotのPRを、試験が通った後に、パッチ・マイナー更新なら自動でマージする。

### 使い方

[examples/dependabot-auto-merge-caller.yml](examples/dependabot-auto-merge-caller.yml) のジョブを、呼び出し側のPRで動く試験のworkflowに足す。

- 試験のジョブの後（`needs`）に呼ぶ。呼び出し側のジョブに `permissions: contents: write`・`pull-requests: write` を付ける。
- `deploy-paths` の検証と変更ファイルの判定は、マージする前に行う（誤りがあればマージせずに失敗する）。`deploy` は、今回このワークフローがマージしたときだけ `true` になる。マージの応答が得られず結果が不明なとき（タイムアウト・5xx）は、マージ済みに見えても失敗にして、デプロイの確認を求める。再実行で、すでにマージ済みのPRを見つけたときは `false` なので、デプロイが要るときは呼び出し側のデプロイを手動で起動する。
- マージ後にデプロイするリポジトリは、`outputs.deploy` を見て、呼び出し側のジョブでデプロイのworkflowを起動する（`actions: write` は、そのジョブにだけ付ける）。

| 入力 | 既定 | 意味 |
|---|---|---|
| `merge-method` | `merge` | マージの方法（`merge`・`squash`・`rebase`） |
| `deploy-paths` | 空 | デプロイが要る変更ファイルのパス（`grep -E`の正規表現）。空なら、マージしたら常に `deploy` が `true` |

| 出力 | 意味 |
|---|---|
| `merged` | マージしたら `'true'` |
| `deploy` | マージし、かつ `deploy-paths` に当たる変更を含むなら `'true'` |

### 動き

- `update-type` が `version-update:semver-patch`・`version-update:semver-minor` のときだけマージする。メジャー更新と、種類が分からない更新はマージしない（人が確かめる）。
- 試験の後に別のコミットが積まれていたら、マージしない（`--match-head-commit`）。
- コミットがDependabotのものかは `dependabot/fetch-metadata` が確かめる。
- **GITHUB_TOKENでのマージは、別のworkflowを起動しない。** mainへのpushで動くデプロイや、release-tagは、自動マージでは動かない。デプロイは上のとおり呼び出し側で起動する。版のタグは、次に人がマージしたときの版に含まれる。

## 開発

```bash
uv run mypy
uv run pytest
```
