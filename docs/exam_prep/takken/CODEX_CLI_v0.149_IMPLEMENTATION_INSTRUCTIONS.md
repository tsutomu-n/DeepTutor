# Codex CLI v0.149 実装指示: 宅建 ExamPrep

## 目的

DeepTutor に宅建向け `exam_prep` を追加する。既存 `mastery_path` は変更せず、最初の開発単位 **PR-1 Domain / migration** だけを実装する。

この作業の目的は予測AIや推薦AIを先に作ることではない。宅建 ExamPrep の測定Vertical Sliceを支える、再現可能で壊れにくいDomain/Data基盤を作ることである。

## 基準

- Repository: `tsutomu-n/DeepTutor`
- Base branch: `main`
- Base commit at requirements freeze: `23ad80d1bf76b61da322fb2dd657978c56ade342` (v1.5.12)
- Requirements bundle: `docs/exam_prep/takken/DeepTutor_Takken_ExamPrep_Requirements_Final_2026-08-23.zip`
- Bundle SHA-256: `48be5edcc4595d685517dc271256c6b9719167b2ee68d9435dafed19bea9481b`
- Target agent: Codex CLI v0.149

`main` が上記commitから進んでいる場合は、現在の `main` を優先して既存構造を再確認すること。要件の意味を勝手に変更して追従しない。衝突や前提崩れがあれば実装を止め、PR本文にBlockerとして記録する。

## 最初に読むもの

1. Repository root の `AGENTS.md`
2. ZIPを一時ディレクトリへ展開する
3. 展開後、最低限次を読む
   - `README.md`
   - `docs/01_結論・前提・Owner対応.md`
   - `docs/02_要件定義.md`
   - `docs/04_ドメイン・状態・アーキテクチャ.md`
   - `docs/06_コンテンツ・権利・Anchor.md`
   - `docs/09_実装計画.md`
   - `docs/10_運用・リスク・停止条件.md`
   - `management/requirements.csv`
   - `management/acceptance_tests.csv`
   - `reference/models.py`
   - `reference/sqlite_schema.sql`
4. 現行RepositoryのCapability、Storage、Config、Testの実装規約を調査する

ZIPの参照実装は仕様の補助であり、Repositoryの現行規約より優先されない。`reference/*.py` をそのままコピーしない。

## 今回の実装範囲: PR-1のみ

### 実装する

- `exam_prep` のDomain package
- 宅建ExamSpecを表現できるDomain model
- ExamTargetと状態Enum
- Item provenance / content rights metadata
- Exposure / Attempt / Time のappend-only event model
- Protected Anchor lifecycleを表現するmodel
- Readiness計算の入力に必要な最低限の型
- 永続化Repository interface
- Repository規約に適合する初期migration / schema
- Event replayを可能にする順序キーまたは同等の仕組み
- Idempotencyを保証するために必要な一意制約
- Unit test / schema test / migration test

### 実装しない

- `P(pass)` またはMonte Carlo forecast
- IRT / CAT
- FSRS integration
- Contextual Bandit
- Knowledge Point × Actionの個人GainModel
- LLM grading
- Recommendation policy
- 自動 `SKIP`
- 自動 `STOP`
- Web UI
- 新しい公開API
- `mastery_path` の挙動変更
- `deeptutor/learning/*` の意味変更
- 公式宅建問題本文のRepository追加

「ついでに作れる」は範囲追加の理由にしない。

## Architecture制約

1. 既存 `mastery_path` と `learning/` をFallbackとして残す。
2. ExamPrep固有状態を `LearningProgress` へ押し込まない。
3. Attempt / Exposure / Time / Anchor consumptionの一次記録はappend-onlyとする。
4. Readiness等の派生状態は一次Eventから再構築可能にする。
5. Measurement eligibilityはClient入力で確定できない設計にする。
6. Protected Anchorは、一度問題文を配信した時点で再利用不可にできる状態遷移を表現する。
7. `UNGRADABLE`, `INVALID_ITEM`, `REVIEW_REQUIRED` を能力Evidenceとして扱わない前提を型/制約で壊しにくくする。
8. Audit対象Eventを安易なcascade deleteで消せるSchemaにしない。
9. SQLiteを採用する場合もApplication層をSQLite APIへ直接密結合させず、Repository境界を設ける。
10. Enum/状態を一つの万能`status`へ統合しない。

## 宅建2026の固定条件

BundleにあるExamSpecを正本とする。コードに配点・日付・閾値を散在させない。

- `GENERAL_50`: 50問 / 120分
- `REGISTERED_45`: 45問 / 110分
- Exam date: `2026-10-18`
- Legal effective-date basis: `2026-04-01`

`40/50`および`35/45`は製品上の初期Readinessヒューリスティックであり、公式合格点でも合格保証でもない。PR-1では判定Engineを実装しない。

## 推奨作業手順

1. `git status` と現在のHEADを記録する。
2. `AGENTS.md` と近接実装を読む。
3. Requirements ZIPのSHA-256を検証して展開する。
4. `management/requirements.csv` からPR-1対象Requirement IDを抽出する。
5. 既存の永続化・Config・Pydantic/Test規約を確認する。
6. 変更予定ファイルを列挙し、PR-1範囲外が混じっていないことを確認する。
7. Domain modelを実装する。
8. Persistence interfaceとmigration/schemaを実装する。
9. Testを追加する。
10. 対象testに加え、既存のlearning/mastery系testを実行して回帰がないことを確認する。
11. Format/lint/type-checkはRepositoryで既に定義された方法を使用する。
12. 差分を自己レビューし、PR-1外の変更を取り除く。
13. PR本文に実行したcommand、test結果、未確認事項、既知の制約を記載して停止する。

## 必須Test観点

最低限、次を自動Testで証明する。

- GENERAL_50とREGISTERED_45を混同しない
- ExamSpec version/effective dateを保持できる
- Eventの順序を一意に再現できる
- 同一Idempotency keyの二重記録を防止できる
- 同一deliveryの二重回答を防止できるSchemaまたはDomain制約がある
- `UNGRADABLE`等をmeasurement evidenceへ昇格させない
- Protected Anchorを `PROTECTED -> EXPIRED` へ遷移できる
- Anchor consumption履歴を後から消して未見状態へ戻せない
- 外部露出申告とSystem観測Exposureを区別できる
- Time observationのmeasurement type / confidenceを保持できる
- Migrationを空DBへ適用できる
- Migration適用後の制約が実際に動く
- EventからProjectionを再構築するために必要な情報が失われない
- 既存`mastery_path`のtestが回帰しない

## STOP条件

次のいずれかに該当した場合、推測で埋めず実装を停止する。

- 現行Repositoryのstorage/migration方式とBundleの前提が根本的に衝突する
- `main` の変更によりCapability境界が変わっている
- Requirement間に実装結果を左右する矛盾がある
- 宅建問題本文をRepositoryへ追加しないとTestできない設計になった
- PR-1を成立させるために`learning/*`の意味変更が必要になった
- Data deletion/retention方針を確定しないと不可逆なSchema判断になる

停止時は、Blocker、影響するRequirement ID、選択肢、推奨を簡潔に残す。勝手に仕様を補完しない。

## 完了条件

PR-1は次をすべて満たしたときだけ完了とする。

- PR-1対象Requirementに実装または明示的なNot-applicable理由がある
- 新規Domain/Data testがpass
- Migration testがpass
- 既存learning/mastery testがpass
- Lint/format/type-checkがRepository基準を満たす
- 公式宅建問題本文をcommitしていない
- `P(pass)`, Recommendation, SKIP/STOPを実装していない
- `mastery_path`の意味を変更していない
- 変更差分がPR-1だけに閉じている
- PR本文に検証結果と残課題が記録されている

## Codexへの最終指示

PR-1を実装し、Testを実行し、差分を自己レビューしてPRを作成するところまで行う。PRをmergeしない。PR-2以降へ進まない。

要件にない機能を追加せず、確認できないことを推測で埋めない。既存設計を壊す必要が出た場合は実装を止める。