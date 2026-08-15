# TJM 実装計画

更新: 2026-08-15_12:52 (Asia/Tokyo)

## 1. 状態

- 計画状態: 未完了（中核と配布可能段階は完了、外部製品受入を継続中）
- 現在のチェックポイント: CP-15 製品受入（外部受入待ち）
- 作業ブランチ: `tjm/implementation`
- 基準: DeepTutor v1.5.8 / `44fa7a1552b88f9d8ce2c22259128a15ae2eb0c8`
- リポジトリルート: `/home/tn/projects/DeepTutor`
- Draft PR: [#1 Add TJM exam learning platform](https://github.com/tsutomu-n/DeepTutor/pull/1)（`tjm/implementation` → `main`、Draft）
- 過去の判定: 2026-08-02にCP-04〜CP-12の実装・検証・push・Draft PR作成をもって完成と判定した。
- 判定の撤回: 2026-08-03の追加監査で、レビュー後編集、サーバー時間、期限確定、復習キュー、廃止版、配布物、永続化の契約不備を再現したため、製品完成判定を撤回した。CP-04〜CP-12の作業履歴は監査証跡として保持する。
- GitHub境界: PR #1はDraftのまま維持する。`main`へのmerge、Ready for review、release、publish、deploymentは行わない。
- 実行状態の正本: 本書と`IMPLEMENTATION_PLAN.ai.json`。`.codex/SP_STATE.md`は実行環境の書込制約で旧CP-11のまま更新できないため、現在状態の根拠に使わない。

### 1.1 完成段階

| 段階 | 完成条件 | 現状 |
| --- | --- | --- |
| A. 中核実装 | 正式問題、採点、回答、期限、復習、廃止版、合否、利用者分離の契約と自動テストが成立 | 完了 |
| B. 配布可能 | clean build/wheel install、Docker再作成、永続化、backup/restore、CIが再現可能 | 完了 |
| C. 製品受入 | 日本語UI、操作型E2E、音声自動検証、実端末、権利確認済み宅建問題での完走 | 自動化可能部分は完了、Pixel 9a・権利確認済み実データは外部待ち |
| D. ユーザー判断 | Draft PRで証拠を確認し、merge・公開・配備をユーザーが判断 | 未実施 |

## 2. 目的

DeepTutor の単一フォークに、試験固有の問題数、分野名、制限時間をコードへ固定しない択一試験学習機能 TJM を追加する。最初の運用対象は宅建とするが、試験定義と問題データは取り込み・SQLite・API境界で差し替え可能にする。

完成条件は次の機能が実データ経路で接続され、ダミー応答や未接続UIを残さず、Web画面だけでも継続利用できることである。

- 通常演習と試験モード
- 問題版管理、取り込み、人間レビュー、公開・廃止
- 決定論的採点と提出前の正解・解説非開示
- 回答履歴、自信度、回答時間、ヒント使用履歴
- 復習キューと履歴分析
- 問題・選択肢・解説の読み上げ
- 音声認識候補の確認後だけ確定する音声回答

## 3. 確認した現状

- CP-04開始時点では`HEAD`、`origin/main`、指定基準commitがすべて`44fa7a15`で一致し、基準commitとの差分はなかった。現在は`44fa7a15`を基準にTJM差分を同一forkへ積み上げている。
- 適用される指示は `/home/tn/AGENTS.md` とリポジトリ直下 `AGENTS.md`。`/home/tn/projects/AGENTS.md` は存在しない。
- Python要件は `>=3.11,<3.14`。CIは3.11、3.12、3.13を必須、3.14をbest-effortとしている。
- Webの正本は `web/package-lock.json`（lockfileVersion 3）。CIとDockerはNode 22と `npm ci --legacy-peer-deps` を使う。
- 変更前はPython lockfileが存在しなかった。今回`uv.lock`を追加し、TJM音声extraを含む解決を固定した。Dockerの`requirements.txt`経路は引き続き範囲指定である。
- 既存のQuestion NotebookはAI生成Quizの保存用で、正式問題の不変版、公開審査、試験提出、決定論的採点の正本ではない。
- 既存認証はHTTP依存関係からrequest-local user contextを設定し、`PathService`がadminと一般ユーザーの保存先を分離する。
- 既存の `/api/v1/voice/tts` と `/api/v1/voice/stt` を維持し、交換可能なsherpa-onnx/edge-tts adapter、自己配信VAD、自動終話、回答確認をTJMへ追加した。通常チャットの`MediaRecorder`経路も変更していない。

## 4. 変更してはならない境界

1. 正式な正解、採点結果、公開状態、回答履歴はLLM出力から更新しない。
2. 公開済み問題版は不変とし、訂正は新しい版を作って審査・公開する。
3. 試験中の問題取得・回答保存応答には、正解、解説、採点結果を含めない。
4. 問題順はattempt作成時に確定して保存し、再読込で変えない。
5. 回答時間はクライアント申告だけを正本にせず、サーバー時刻とイベント時刻を保存する。
6. 音声認識文字列は回答確定APIと分離し、確認操作後だけ選択肢として保存する。
7. 読み上げ開始前に録音を停止し、読み上げ終了後もユーザー操作なしに回答を確定しない。
8. 音声、AI、外部providerの障害で通常の画面操作を阻害しない。
9. 宅建の分野名、問題数、制限時間、合格点をPython/TypeScript定数へ固定しない。
10. TJMの正式データを既存AI Quizの`notebook_entries`へ混在させない。

## 5. 現行の責任分離

### 5.1 保存

正式問題を全利用者へ一貫して提供しつつ学習履歴を分離するため、セッション履歴DBとは別に次の2つのSQLiteを使う。

- deployment共有の `data/system/tjm/catalog.db`: admin管理の試験定義、問題、正式正解、版、取り込み、レビュー監査
- ユーザーごとの `PathService` 配下 `tjm_learning.db`: attempt、固定出題順、回答イベント、復習キュー

一般ユーザーのリクエストから共有catalogへ書き込む経路は作らない。共有catalogの公開済み版を参照して出題し、回答・履歴はrequest-local user contextで解決したDBだけへ保存する。両DBはそれぞれ明示的なmigration版を持つ。

次の概念を持つ。

- `schema_migrations`: 明示的なスキーマ版
- `exam_definitions`: 試験名、時間、出題数、分野配分などのデータ駆動設定
- `questions`: 問題の安定IDと試験所属
- `question_versions`: 本文、選択肢、正式正解、解説、ヒント、分野、状態、内容hash
- `review_events`: draft/rejected/published/retiredの監査履歴
- `import_batches`: 取り込み元、件数、行別エラー、実行者
- `attempts`: practice/exam/review、開始・提出・集計、最大点・公式点と根拠資料・個人目標・採点方式を開始時に固定する試験設定snapshot
- `attempt_items`: 出題順、固定された問題版、表示・回答時刻
- `answer_events`: 選択、自信度、ヒント、音声候補、音声確認、変更履歴
- `exam_preferences`: 利用者別の個人目標、由来、明示的な未設定状態
- `review_queue`: 復習理由、優先度、次回予定、解消状態
- `review_attempt_queue_links`: 復習attempt開始時に対象だったqueue行IDの不変snapshot

共有catalogと利用者別learning DBは別SQLiteであり、一つのtransactionでは廃止状態を全利用者へ反映できない。この制約を隠さず、各利用者のlist/start/read/write/finalize/history/analytics入口でcatalog状態を照合し、問題版の扱いとpending復習のdismissを利用者DBへ単調に永続化する。

### 5.2 API

FastAPIへ `/api/v1/tjm` を追加する。通常利用APIは既存`require_auth`、取り込み・レビュー・公開APIは`require_admin`で保護する。

主な契約は次のとおり。

- 試験定義: `GET/POST/PATCH /exams`
- 公式点と根拠資料: `PUT /exams/{exam_id}/official-passing-score`
- 利用者別目標: `GET /exam-preferences`、`PUT /exam-preferences/{exam_id}`
- 取り込み: `POST /imports`、`GET /imports/{id}`
- 人間レビュー: `GET /review/questions`、`PATCH /review/questions/{version_id}`、`POST /review/questions/{version_id}/publish|reject|retire|classify-retirement`。手動retireは`reason=invalid_content`を必須とし、通常の`superseded`は置換版のpublish transactionだけが記録する。移行前から存在する理由不明のretired版だけは、人間が同一問題の後続版IDを指定した時に明示分類できる。
- 演習・試験: `POST /attempts`、`GET /attempts/{id}`、`POST /attempts/{id}/answers`、`POST /attempts/{id}/submit`
- ヒント: `POST /attempts/{id}/items/{position}/hint`
- 音声候補記録: `POST /attempts/{id}/items/{position}/voice-candidate`
- 復習: `GET /review/queue`、`POST /review/attempts`
- 履歴・分析: `GET /history`、`GET /analytics`

### 5.3 Web

Next.jsへ `/tjm` を追加し、次を単一の利用導線にまとめる。

- 演習開始、試験開始、回答、提出、結果
- 自信度、経過時間、ヒント
- 復習キュー、履歴、分野・自信度・時間・ヒント別分析
- JSON/JSONL/CSV取り込みとadminレビュー
- 問題読み上げと確認付き音声回答
- 公式合否と個人目標達成を別々に表示し、公式点の根拠資料を参照可能にする。未設定・不整合・未知の契約は推測せず「未判定」とする。

Webは正式正解をローカル判定せず、practiceの確定応答またはexam提出応答だけを表示する。

## 6. 取り込み契約

- 受理形式: UTF-8 JSON、JSONL、CSV。
- 必須項目: 試験識別子、問題安定ID、問題本文、2件以上の一意な選択肢、選択肢keyとしての正解、分野。
- 任意項目: 解説、ヒント、出典、外部版、メタデータ。
- 重複安定ID、空本文、重複選択肢key、存在しない正解key、不正UTF-8、行ごとの型不一致はfail-closedとする。
- 取り込み成功はdraft版作成まで。公開は別のadmin操作と監査イベントを必須にする。
- 同一内容hashの再取り込みは新しい版を増やさず、batch結果に重複として記録する。

## 7. チェックポイント

### CP-04 変更前ベースラインの確定

状態: 完了

完了条件:

- 指定commitとfork差分、適用AGENTS、依存・lockfile・CI・Docker手順を記録する。
- PythonとWeb依存を再現する。
- Python全テスト、Ruff、Web node tests、ESLint、TypeScript、Next production build、Python package buildを実行する。
- 公式Dockerfileのproduction image buildを完了するか、再現可能な外部要因を停止条件として記録する。
- 既存失敗とコマンド誤用を区別して本書とJSONへ反映する。

### CP-05 TJMドメインとSQLite契約

依存: CP-04

状態: 完了

完了条件:

- 独立SQLiteとmigration、試験定義、問題安定ID・不変版・状態遷移をTDDで実装する。
- 採点関数は問題版の正解keyと確定回答だけを比較し、LLM/providerを参照しない。
- 公開版の上書き、無審査公開、不正な正解key、duplicateを拒否するテストを通す。
- admin/non-adminのユーザー別DB分離をテストする。

### CP-06 取り込みと人間レビュー

依存: CP-05

状態: 完了

完了条件:

- JSON/JSONL/CSVの全行検証、batch結果、draft作成を実装する。
- adminだけが編集・公開・却下・廃止できる。
- 公開中に正式正解を変更せず、新版作成が必要であることをAPIテストで固定する。
- 問題候補やAI批評を正式版へ直接昇格させる経路を作らない。

### CP-07 通常演習・試験・履歴

依存: CP-05、CP-06

状態: 完了

完了条件:

- data-drivenな試験定義から固定出題順のattemptを作る。
- practiceは回答確定後、examは提出後だけ採点情報を返す。
- 自信度、回答時間、選択変更、ヒント履歴をappend-onlyイベントで保存する。
- 二重提出、期限超過、未回答、再読込、別ユーザーattempt参照をテストする。

### CP-08 復習と履歴分析

依存: CP-07

状態: 完了

完了条件:

- 不正解、低自信、ヒント使用、遅い正解を説明可能な規則で復習候補にする。
- 分野別正答率、自信度較正、回答時間、ヒント使用、推移を集計する。
- 空データ、部分回答、廃止問題版を含む履歴でも壊れない。

### CP-09 Web学習導線

依存: CP-06、CP-07、CP-08

状態: 完了

完了条件:

- `/tjm` で取り込みから公開、演習、試験、復習、履歴分析まで操作できる。
- exam提出前のHTML/JSON/クライアントstateに正解・解説がないことをテストする。
- keyboard、狭幅、再読込、API失敗時の復旧経路を検証する。
- 新規UI文言は既存i18n方針に従い、追加lint warningを残さない。

### CP-10 音声

依存: CP-09

状態: 完了

完了条件:

- 既存TTS経路を再利用し、provider失敗時は画面継続を保証する。
- edge-ttsは交換可能adapter候補として評価し、唯一の経路にはしない。
- `ricky0123/vad`を第一候補として開始・終了検出を実測し、採用または不採用理由と代替を記録する。
- sherpa-onnxの日本語modelをローカルで測定できるadapter/診断コマンドと結果契約を用意する。
- 音声候補は確認ダイアログ後だけ回答保存し、取消時は正式回答を変更しない。
- TTS中はmicrophone trackを停止し、exam提出前に正解・解説を読み上げない。

### CP-11 全体検証と安全性監査

依存: CP-05〜CP-10

状態: 完了

完了条件:

- 変更箇所の単体・API・Web tests、全Python tests、Ruff、Web node tests、ESLint、TypeScript、Next build、Python build、Docker buildを通す。
- auth有効/無効、admin/user、ユーザー別DB、再起動後SQLite、音声なしの導線を検証する。
- TODO、仮成功、ダミーデータ、正解漏えい、LLM採点経路を検索で監査する。
- 計画とJSONを実証結果で更新する。

### CP-12 GitHub反映

依存: CP-11

状態: 完了

完了条件:

- 変更を意味のあるまとまりでcommitする。
- `tjm/implementation`をpushする。
- 変更、検証結果、既存/残存リスクを記したDraft PRを作る。
- `main`へmergeしない。

### CP-13 中核整合性の修復

依存: CP-12後の追加監査

状態: 完了

完了条件:

- 公開版を表す`version`とdraft編集世代`content_revision`を分離し、レビューを不変revisionへ結び付け、publish transaction内で現在revisionと再照合する。
- 初回表示、初回・最終回答、提出をサーバーUTC時刻で保存し、正式時間とclient診断値を分離する。
- 回答、音声確定、提出、復習完了の再送をidempotency keyとDB一意制約で二重適用しない。
- 期限到来時の現在回答をGET・書込み・提出・履歴の各経路から冪等に自動確定・採点する。
- 復習attemptは開始時のqueue行IDだけをsnapshotし、後発理由や他attemptを完了させない。
- 廃止理由を`superseded`と`invalid_content`に分け、履歴保持、新規出題、得点・分析、pending復習の扱いをテストで固定する。
- `official_passing_score`と利用者別`practice_target_score`を分け、未確定の公式合否を推測しない。
- 別利用者のattempt/履歴/復習操作、非adminの問題管理、提出前answer key漏洩をnegative testで拒否する。

### CP-14 配布と永続化

依存: CP-13

状態: 完了

完了条件:

- source Docker経路でclean build、`/tjm` smoke、container再作成後のcatalogとadmin/一般ユーザー履歴保持を実測する。
- GHCR composeとrelease workflowをfork所有imageまたはrepository動的名にし、`data` root全体を永続化する。実publishは行わない。
- SQLiteの整合したoffline backup/restore手順を用意し、復元後のcatalogと履歴一致を検証する。
- `prepare_web_package`からwheel build、clean install、`deeptutor start`、`/tjm` smokeまでを再現する。
- PR用CIにTJM対象test、migration、Web lint/type/build、操作型E2E、Docker smokeを組み込む。
- 依存脆弱性はadvisory、導入差分、production到達性、修正可否、期限付きリスク受容で判定する。`npm audit fix --force`は行わない。

### CP-15 製品受入

依存: CP-13、CP-14

状態: 内部自動検証完了、外部受入待ち

完了条件:

- TJM利用導線を日本語化し、固定文言のi18n lint全体無効化を解消する。
- 合成日本語問題50問の取り込み、レビュー、公開、本番形式完走、復習、永続化を自動検証する。合成問題を実問題と表示しない。
- VAD、音声候補、取消、確定、二重送信、TTS中mic無効化、障害後の画面回答を再実行可能な自動テストにする。
- Pixel 9a等の対象実端末で10問、権利確認済み宅建問題50問以上の受入は、必要な外部操作・データが提供された時点で実施する。

### CP-16 ユーザーリリース判断

依存: CP-15

状態: 未着手

完了条件:

- 変更、検証証拠、未確認の外部受入、依存リスクをDraft PR #1に同期する。
- PRはDraftのままユーザーへ引き渡す。merge、Ready化、release、publish、deploymentはユーザーの別判断とする。

## 8. CP-04 証拠台帳

| 項目 | コマンド/証拠 | 結果 |
| --- | --- | --- |
| root/HEAD | `git rev-parse --show-toplevel`; `git rev-parse HEAD` | root一致、`44fa7a15…` |
| fork差分 | `git diff 44fa7a15..HEAD`; `git diff 44fa7a15..origin/main` | 差分なし |
| Python | `uv venv --python 3.12 .venv` | Python 3.12.12 |
| Python依存 | `uv pip install -r requirements/server.txt -r requirements/partners.txt pytest pytest-asyncio ruff==0.16.0 mypy` | 170 packages導入成功。lockfileなし |
| Python tests | venv PATH有効で `pytest -q tests deeptutor/learning/tests` | 3555 passed, 8 skipped, 205 warnings |
| Python初回誤用 | `.venv/bin/pytest ...` | 1 failed。テスト内`python`がPATHになくexit 127。venv PATH有効化で解消 |
| Ruff | `ruff check .`; `ruff format --check .` | 成功、1134 files formatted |
| mypy | `mypy deeptutor deeptutor_cli` | 既存契約失敗。Python 3.11設定でNumPy 2.5.1のPython 3.12 type statementを解析 |
| Web依存 | Node 24.14.0で `npm ci --legacy-peer-deps` | 774 packages導入成功。CI/DockerはNode 22 |
| Web node tests | `npm run test:node` | 366 passed |
| ESLint | `npm run lint` | exit 0、既存warning 56、error 0 |
| TypeScript | `npx tsc --noEmit` | 成功 |
| Next build | `npm run build` | 成功、57 routes。caniuse-lite 8か月古い警告 |
| Python build | `uv build` | sdist/wheel成功。license table廃止予定とWeb package asset警告 |
| npm audit | `npm audit --json` | 10件: low 1 / moderate 4 / high 5 / critical 0 |
| Docker既定target | `docker build --progress=plain -t deeptutor-tjm:cp04 .` | 成功。Dockerfile末尾のdevelopment target、714,288,009 bytes |
| Docker production | `docker build --progress=plain --target production -t deeptutor-tjm:cp04-production .` | 成功、450,313,679 bytes |
| Docker smoke | production imageでversionとapp import | Python 3.11.15、Node 22.23.2、DeepTutor 1.5.8、`DeepTutor API` |

## 9. CP-05 証拠台帳

| 項目 | コマンド/証拠 | 結果 |
| --- | --- | --- |
| 保存先分離 | `PathService.get_tjm_learning_db()`、`get_tjm_catalog_db()` | catalogは`data/system/tjm/catalog.db`、履歴はユーザーroot別`user/tjm_learning.db` |
| migration | `CatalogStore`、`LearningStore` | catalog v1/v2、learning v1。未知の新schemaを黙って上書きしない基盤 |
| 公開版保護 | SQLite triggerと`CatalogService` | 正式内容のUPDATE/DELETE拒否、公開前review必須、旧正式版は新版公開時にretired |
| 入力検証 | `ExamSpec`、`QuestionVersionDraft` | 不正時間・問題数・分野配分、2択未満、重複key、存在しない正解keyを拒否 |
| 採点 | `grade_responses()` | answer keyと確定回答だけを比較。外部AI/provider依存なし |
| 対象検証 | `pytest -q tests/tjm tests/services/test_path_service.py tests/multi_user/test_identity_and_paths.py` | 26 passed |
| 静的検査 | CP-05対象への`ruff check`、`ruff format --check` | 成功、8 files formatted |

## 10. CP-06 証拠台帳

| 項目 | コマンド/証拠 | 結果 |
| --- | --- | --- |
| 形式 | `ImportService` | UTF-8 JSON array、JSONL、CSV（JSON列）を共通domain validatorへ接続 |
| fail-closed | valid+invalid混在、batch内stable ID重複、不正UTF-8、不正document | 問題版0件、failed `import_batches`だけを監査保存 |
| dedupe | 同一内容の再取り込み | 新版を作らず`duplicate_rows`へ計上 |
| 公開境界 | import成功後のstatus | 全件draft。公開は別のreview/publish APIのみ |
| admin workflow | `/api/v1/tjm/exams`、`/imports`、`/review/questions` | 既存`require_admin`で作成・編集・審査・公開・却下・廃止を保護 |
| 対象検証 | `pytest -q tests/tjm tests/api/test_tjm_router.py` | 31 passed |
| 静的検査 | CP-06対象への`ruff check`、`ruff format --check` | 成功、11 files formatted |

## 11. CP-07 証拠台帳

| 項目 | コマンド/証拠 | 結果 |
| --- | --- | --- |
| 試験active化 | `CatalogService.activate_exam()` | 公開問題数とdata-driven blueprint不足を拒否 |
| 固定出題 | `AttemptService.start_attempt()` | version IDとposition、exam revision snapshotをユーザー別SQLiteへ保存し再起動後も一致 |
| 回答イベント | `record_answer()`、`use_hint()` | selected/confidence/confirmed/hintをappend-only保存。server時刻とclient elapsedを併記 |
| 開示制御 | practice/exam API tests | practiceは確定後、examはsubmit後だけ正解・解説・is_correctを返す |
| 終端制御 | deadline、submit | 期限後回答拒否、expired提出、二重提出拒否 |
| 分離 | Alice/Bob別`LearningStore` | 別ユーザーDBからattempt IDを参照できない |
| 対象検証 | `pytest -q tests/tjm tests/api/test_tjm_router.py` | 41 passed |
| 静的検査 | CP-07対象への`ruff check`、`ruff format --check` | 成功、12 files formatted |

## 12. CP-08 証拠台帳

| 項目 | コマンド/証拠 | 結果 |
| --- | --- | --- |
| 復習規則 | `ReviewPolicy` | incorrect、low_confidence、hint_used、slow_correctを別理由・優先度で保存 |
| 復習導線 | `list_review_queue()`、`start_review_attempt()` | pending版を理由付き表示し、exam IDとlimitからreview attemptを作成 |
| 集計 | `analytics()` | overall、分野別、自信度3帯、平均時間、hint率、attempt推移 |
| 境界値 | 空履歴、部分回答、retired版 | 例外なく定義済みnull/0を返し、履歴版で再集計 |
| API | `/review/queue`、`/review/attempts`、`/analytics` | request-local `AttemptService`へ接続 |
| 対象検証 | `pytest -q tests/tjm tests/api/test_tjm_router.py` | 45 passed |
| 静的検査 | CP-08対象への`ruff check`、`ruff format --check` | 成功、13 files formatted |

## 13. CP-09 証拠台帳

| 項目 | コマンド/証拠 | 結果 |
| --- | --- | --- |
| Web導線 | `/tjm`、`TjmWorkspace`、`tjm-api.ts` | 演習・試験、復習、分析、admin試験定義・取り込み・レビュー・公開を実APIへ接続 |
| 正解非開示 | `normalizeAttemptForClient()`とAPI tests | in-progress examの正解・解説・採点fieldをserverとclient境界の双方で除去 |
| 再読込 | `sessionStorage`のattempt IDと`GET /attempts/{id}` | 出題順と回答済み状態をSQLiteから復元。ローカル採点なし |
| 管理証跡 | `reviewed_by`、`reviewed_at`、`review_note` | review eventを版レスポンスへ投影し、画面からreview後publish可能 |
| 並列初期化 | 16 thread同時`CatalogStore` test | migrationを単一writer transaction化し、並列初期APIの二重DDLを防止 |
| Web tests | `npm run test:node`; `npx tsc --noEmit` | 371 passed、型検査成功 |
| Python tests | `pytest -q tests/tjm tests/api/test_tjm_router.py` | 46 passed後に並列migration回帰testを追加。CP-11の全件検証にも収録済み |
| lint | `npm run lint` | error 0、warning 56。変更前の既存warning数と同じ |
| Next build | `npm run build` | 成功、`/tjm`を含む58 routes |
| 実ブラウザ | `agent-browser`、1280px/375px | learner空状態とadmin全formを表示。TJM初期API全200、console errorなし、狭幅タブ欠けなし |

## 14. CP-10 証拠台帳

| 項目 | コマンド/証拠 | 結果 |
| --- | --- | --- |
| 音声回答境界 | `record_voice_candidate()`、`confirm_voice_candidate()`、`cancel_voice_candidate()` | candidate/cancelでは正式回答を変更せず、最新かつ未解決の候補をconfirmした時だけ`confirmed_option_key`を更新 |
| 認識のfail-closed | 明示的な数字・日本語序数・完全一致keyの決定論的mapping | 「一番か二番」のような複数候補は`proposed_option_key=null`とし確定拒否 |
| API非開示 | exam音声candidate/confirm API tests | 提出前レスポンスに正解・解説・`is_correct`なし。二重confirmは409 |
| VAD | `@ricky0123/vad-web` 0.0.30、Silero v5 | `predev`/`prebuild`でworklet/model/ONNX WASMの4 assetを`/vad/`へ自己配信。CDN依存なし |
| 実音声E2E | Chromium fake microphone + 6秒日本語WAV | VAD model load、実発話開始、終話、mic停止、STT 200、候補Bダイアログ、cancel無変更、再認識後confirm保存まで実ブラウザで成功 |
| VAD競合回帰 | 停止直前callbackの実ブラウザ再現 | `vadActiveRef`で停止済み世代のcallbackを無視し、確認中の表示をidleへ修正 |
| ローカルSTT | `voice-local` extra、`SherpaOnnxSTTAdapter`、`sherpa_diagnostic --runs 3` | sherpa-onnx 1.13.4、ReazonSpeech int8約162MB。3.0秒音声を「選択肢の二番を選びます」と完全転写 |
| STT実測 | Linux x86_64、CPU 4 thread | cold 1.549秒 / RTF 0.516、warm 0.067〜0.077秒 / RTF 0.022〜0.026。modelはprocess内cache |
| TTS | 既存provider経路 + `voice-edge` extra | edge-tts 7.2.8を交換可能なonline adapterとして追加。唯一経路や自動fallbackにはしていない |
| 読み上げ境界 | `useTjmVoice.speak()`、実ブラウザのTTS未設定400 | 読み上げ前にmic停止。in-progress examは問題文・選択肢だけを構成。失敗を画面内表示し選択操作は継続可能 |
| Web検証 | `npm run test:node`; lint; TypeScript; build | 374 passed、lint error 0 / 既存warning 56、型検査成功、58 routes build成功 |
| Python対象検証 | CP-10 TestCommand | 98 passed、205 warnings。Ruff対象検査と`git diff --check`成功 |
| 狭幅 | 実ブラウザ375x812 screenshot | 読み上げ・音声・画面回答を同一画面に保持し、画面回答で継続可能 |

## 15. CP-11 証拠台帳

| 項目 | コマンド/証拠 | 結果 |
| --- | --- | --- |
| 契約監査 | 通常`POST /attempts`へ`mode=review`を送る失敗テスト | 復習キューを迂回できた不備を検出。通常開始をpractice/examだけに制限し、reviewは`POST /review/attempts`だけに固定 |
| 計画差分 | `PATCH /api/v1/tjm/exams/{exam_id}`のAPI/serviceテスト | 計画済み更新APIの欠落を検出して追加。draftだけを置換でき、ID変更とactive試験の変更を拒否 |
| Python全テスト | venv PATH有効で `pytest -q tests deeptutor/learning/tests` | 3611 passed、8 skipped、211 warnings、34.87秒 |
| Python静的検査 | `ruff check .`、変更17ファイルの`ruff format --check` | 成功 |
| TJM型検査 | Python 3.12指定で変更Python 10ファイルを`mypy` | 成功。全体mypyはCP-04と同じNumPy stub/Python 3.11設定の1件だけ失敗 |
| Python lock/build | `uv lock --check`; `uv build` | 383 packagesのlock整合、sdist/wheel成功。wheelへTJMと音声adapterを収録 |
| Web全テスト | `npm run test:node` | 374 passed |
| Web静的検査 | `npm run lint`; `npx tsc --noEmit` | lint error 0 / 既存warning 56、型検査成功 |
| Web build | `npm run build` | Next.js 16.2.12で成功、`/tjm`を含む58 routes、VAD 4 assetを自己配信 |
| 依存安全性 | `npm audit --json`前後比較 | 10件から5件へ低減。low 0 / moderate 2 / high 3 / critical 0 |
| Bun比較 | Bun 1.3.14で`bun install --dry-run --frozen-lockfile` | npm lockからの移行時にも`bun.lock`を書き出す挙動とCI/Docker差分を確認。二重lockを残さずnpm/Node 22を正本として維持 |
| Docker build | `docker build --target production -t deeptutor-tjm:cp11-production .` | 成功、image 456,783,066 bytes、Node 22.23.2、DeepTutor 1.5.8 |
| Docker smoke | production containerを起動してbackend `/`、frontend `/tjm`、healthを確認 | API応答、TJM HTTP 200、container healthy、TJM 24 routes、VAD 4 asset同梱。任意sherpa/edge extraは本体imageへ未同梱 |
| 最終検索監査 | TODO/仮成功/dummy/LLM採点/秘密情報/正解開示と`git diff --check` | 完成扱いを妨げる仮実装・LLM採点経路・提出前正解開示を検出せず、差分空白検査成功 |

## 16. CP-12 証拠台帳

| 項目 | コマンド/証拠 | 結果 |
| --- | --- | --- |
| 実装commit | `git commit -m "feat: add TJM exam learning platform"` | `80339c862d11c9432ca9c20be7b8b6bff6c98536` |
| branch push | `git push -u origin tjm/implementation` | `origin/tjm/implementation`へtracking付きで成功 |
| Draft PR | GitHub PR #1 | `tjm/implementation`から`main`へのDraftとして作成。merge未実施 |

## 16.1 CP-13 証拠台帳（完了）

| 項目 | コマンド/証拠 | 結果 |
| --- | --- | --- |
| 完成判定撤回 | 本書と`IMPLEMENTATION_PLAN.ai.json` | CP-04〜CP-12の履歴を保持し、状態を未完了、CP-13、`completion_claim_allowed=false`へ同期 |
| Catalog migration v3 | legacy v1/v2 DBから`CatalogStore`を再初期化 | current内容をrevision 1へbackfill、旧review eventを保持、review bindingは推測作成せず0件 |
| revision監査 | `question_version_revisions`、`review_bindings`、SQLite trigger | draft編集ごとにimmutable snapshotを作成し、reviewを`question_version_id + content_revision + content_hash`へbinding |
| stale review | review後に本文・正解を変更してpublish | `current revision must be reviewed`で拒否し、再review後だけpublish成功 |
| hash対象の回帰 | 選択肢順、分野、解説、ヒント、出典を各1項目だけ変更 | 5ケースすべてrevision増分、review失効、publish拒否 |
| transaction競合 | edit transaction中に別threadでpublish | edit commit後のcurrent revisionを再照合し、旧reviewによるpublishを拒否 |
| legacy公開版 | v2で公開済みの問題をv3へmigration | current bindingを推測せず出題・試験有効化から除外し、人間の再review後だけ復帰。Admin UIにも再review対象として表示 |
| legacy帰属 | v2の`created_at`と`updated_at`が異なるcurrent内容 | 実編集者を復元できないため`legacy-unknown`、記録時刻を`updated_at`として保存し、原作者への誤帰属を回避 |
| 監査・identity不変性 | review event、question、question versionへの直接SQL更新/削除 | legacyを含むreview eventの更新・削除と、review済み問題のexam/stable identity差替えをSQLite triggerで拒否 |
| 対象検証 | `pytest -q tests/tjm/test_storage.py tests/tjm/test_domain.py tests/api/test_tjm_router.py` | 42 passed、211 warnings。rejected版、legacy公開版、全hash対象、監査・identity不変条件を含む |
| 関連検証 | `pytest -q tests/tjm tests/api/test_tjm_router.py` | 66 passed、211 warnings |
| Web検証 | `npm run test:node`; `npm exec -- tsc --noEmit`; 対象`eslint` | 377 passed、型検査・lint成功。legacy公開版だけを再review対象へ含め、編集・reject・publishを許可しないaction matrixをbehavior testで固定 |
| 静的検査 | 対象`ruff check`、`ruff format --check`、JSON parse、`git diff --check` | 成功 |
| Catalog修復commit | `b48301dc fix: bind TJM reviews to content revisions` | `origin/tjm/implementation`へpush済み。PR #1はDraftのまま |
| Learning migration v2 | legacy v1 DBから`LearningStore`を再初期化 | `first/final`回答時刻、server/client時間、`learning_commands`を追加。旧client時間だけを診断値へ移し、旧server時刻は推測せず`NULL` |
| 正式計時 | `POST /attempts/{id}/items/{position}/open`、`BEGIN IMMEDIATE`後のUTC採時 | open応答までWebで本文・選択肢を隠し、初回表示と初回確定の差を`server_elapsed_ms`へ保存。client値は診断値に限定 |
| 期限確定 | GET、回答、ヒント、音声、提出、履歴、分析、復習入口 | `now >= deadline`で同一transactionの決定論的採点を一度だけ実行し、`submitted_at=deadline_at`で`expired`へ確定 |
| command冪等性 | `learning_commands`のprimary key、request hash、保存済みresponse | 回答・ヒント・音声候補/確定/取消・提出・通常/復習開始をexact replay。同じkeyの別payloadを409。catalog/queue変化より先に開始responseを再生 |
| Web再送 | `TjmCommandLedger`、`Idempotency-Key` | logical actionのkeyとbodyを成功まで固定し、pending commandを同一tabの`sessionStorage`へ保存。再読込後も同一要求を再送 |
| 回答変更境界 | serviceと`canAnswerTjmItem()` | practice/reviewは正解開示後に不変、examは提出前に変更可。音声処理中の別問題移動・画面操作をロック |
| 音声・期限競合 | 録音開始時attempt/position ref、期限0 GET polling | STT結果を録音開始問題へ固定し、期限時にmic/TTSと確認modalを閉じる。open/command 409はGETで最終状態を回復 |
| Learning対象検証 | `pytest -q tests/tjm/test_storage.py tests/tjm/test_attempts.py tests/tjm/test_review_analytics.py tests/api/test_tjm_router.py` | 49 passed、211 warnings。期限ちょうど、並行GET/submit、response loss replay、service再起動、開始前提変化を含む |
| TJM全体回帰 | `pytest -q tests/tjm tests/api/test_tjm_router.py` | 83 passed、211 warnings |
| Web回帰 | `npm run test:node`; `npx tsc --noEmit`; 対象`eslint` | 387 passed、型検査・lint成功。transport、再読込ledger、操作可否のbehavior testを含む |
| 独立レビュー追補 | 音声停止失敗、React Strict Mode、open中の問題移動 | mic停止失敗時も提出・期限GETを継続し、hook mount状態をsetupごとに復帰。server open応答までは前後・解答一覧・提出移動を禁止 |
| Learning静的検査 | 対象`ruff check`、`ruff format --check`、`git diff --check` | 成功 |
| Catalog/Learning migration | Catalog v4、Learning v3 | `retirement_reason`、`retired_at`、置換版ID、itemのcatalog disposition、queue解消理由・主体、`review_attempt_queue_links`を追加。legacy値は推測せず`NULL`/`unchecked`を保持 |
| 復習snapshot | 同一問題の開始時理由、開始後理由、二タブ並行開始・提出、途中dismiss、submit再送 | 各attemptは開始時に存在した行IDだけをlink。最初の提出だけがpending行へ解消attemptを記録し、後発行とterminal行を変更しない。放置tabを永久lockする排他的claimは導入しない |
| 廃止理由 | 手動`invalid_content`、置換publishの`superseded`、後日の誤問判明 | `superseded`は開始済みattemptと過去分析を有効のまま、新規出題・復習だけを停止。`superseded -> invalid_content`以外の逆向き変更をSQLiteで拒否 |
| 利用者DB整合 | catalogは共有、learning DBは利用者別 | 全利用者DBをretire transactionで原子的に更新できない事実を明記し、各利用者操作入口で単調な遅延同期を実施。pending旧版queueを理由付きdismissへ永続化 |
| invalid content | 回答済み履歴、開始済みattempt、分析、Web操作 | answer eventと保存済みraw得点を保持し、新規回答を409、正式正解fieldを非表示、該当itemを採点・分析から除外。画面・音声入力も無効化 |
| 冪等再送の失効反映 | invalid化前の回答・提出・開始responseを同じkeyで再送 | command副作用と保存responseは不変とし、通常・`superseded`時はexact responseを返す。`invalid_content`/理由不明廃止だけ失効fieldを安全に再投影し、正式正解・解説・旧eligible状態を再露出しない |
| 管理画面の廃止操作 | published版の`invalid_content`化、legacy理由不明版の明示分類 | 公開問題を画面から理由付きで無効化できる。理由不明の旧retired版は、同一問題の有効かつ後の版IDを人間が指定した時だけ`superseded`へ分類し、監査eventを追記。逆向き・draft参照はserviceとSQLite triggerの双方で拒否 |
| 無効得点の表示 | 最終結果、Recent attempts | 保存済みraw得点は保持するが「Historical raw score」「Content invalidated」と明示し、現在有効な正式結果に見せない |
| Review/retirement回帰 | `pytest -q tests/tjm tests/api/test_tjm_router.py` | 97 passed、211 warnings。migration、二タブ並行、後発理由、途中dismiss、legacy明示分類、冪等再送の正解再露出防止、raw履歴保持、invalid除外を含む |
| Review/retirement Web回帰 | `npm run test:node`; `npx tsc --noEmit`; 対象`eslint` | 389 passed、型検査・lint成功。無効問題の画面・音声回答停止、raw得点警告、管理画面の無効化・旧廃止分類を含む |
| TJM利用者path解決 | `get_attempt_service`自身の認証依存、request userとworkspace rootの一致検査 | 利用者Context欠落は500、利用者workspace解決・DB初期化失敗は503とし、汎用`PathService`のadmin/default fallbackをTJM学習APIでは使用しない。auth無効時のlocal adminと通常user DBを回帰確認 |
| Learning migration v4 | active exam insert/update trigger | 同一利用者DB・同一試験でactive exam中の新規exam/practice/reviewをSQLite境界でも拒否。移行前から重複していたactive examは推測で変更せず保持し、新規重複だけを拒否 |
| active exam境界 | start、既存practice/review・旧exam、GET、冪等再送、history、analytics、review queue | 既存practice/reviewを永久lockにしないためexam開始自体は許可し、active exam自身以外は旧examを含め試験中だけ操作と保存済みfeedbackを409/非表示にする。試験提出または期限確定後に再開。同時exam開始は`BEGIN IMMEDIATE`下で1件だけ成功 |
| 認可・横断漏洩回帰 | `pytest -q tests/tjm tests/api/test_tjm_router.py` | 112 passed、211 warnings。利用者path失敗時のadmin DB非作成、auth無効互換、試験中の正解field非露出、旧exam直接GET・submit再送、履歴・分析・復習queue遮断、期限ちょうどの直接/並行確定、legacy migrationを含む |
| 認可静的検査 | 対象`ruff check`、`git diff --check` | 成功 |
| 認可独立レビュー | P0/P1再監査、期限競合と旧exam直接経路の追補 | 追補2件を修正後、未解決P0/P1なし。reviewer側では既存`CatalogStore`並行初期化のWAL lockを3回中1回観測したが、本作業側の同一test 10回は全成功。CP-14/最終gateで再発時はflakyとして放置せず停止・修復する |
| Cookie更新のOrigin契約 | `require_authenticated_write_same_origin`、`require_admin_same_origin` | 認証有効かつCookieだけを使う全TJM mutation 21本で、raw header上の単一Originと具体的allowlistの完全一致を必須化。単一の認証済みBearerとauth無効構成は互換維持し、重複Origin、欠落、`null`、suffix/path/scheme違いを403にする |
| credentialed CORS | `_build_cors_settings()`、実`CORSMiddleware` test | 認証有効時の設定値`*`/`null`を起動時の設定エラーにし、共有origin helperからも除外。Cookie付き未許可Originの通常要求にACAOを付けず、preflightを400にすることを実middlewareで確認 |
| CORS設定保存 | `PUT /api/v1/settings/network` | 認証有効時は`*`/`null`を保存前に422で拒否し、backend/frontend portと既存allowlistを変更しない。禁止設定の遅延保存による次回起動不能を防止 |
| Next proxy互換 | 実repoのNext dev server、Chrome、echo backendでJSON POST・bodyless POST・PATCH・multipart POST | frontend Origin、Cookie、body、methodがFastAPI側まで保持されることを確認。実測環境はNode 24/Chrome 150であり、Node 22 DockerとTLS reverse proxyはCP-14で再確認する |
| CSRF/CORS回帰 | `pytest -q tests/tjm tests/api/test_tjm_router.py tests/api/test_cors_settings.py tests/api/test_settings_router.py tests/api/test_auth_contextvar.py tests/api/test_auth_logout_cookie.py` | 229 passed、215 warnings。管理者・学習者Cookie、Bearer、auth無効、全21 mutation route集合、実CORS middleware、設定保存拒否、採点・結果群を含む。対象`ruff check`、`ruff format --check`、`git diff --check`も成功 |
| CSRF/CORS最終独立レビュー | 共通unsafe-origin判定、effective auth、保存前拒否、全mutation集合の再監査 | 未解決P0/P1と設定/API回帰なし。reviewer focused再実行11 passed |
| 採点設定migration | Catalog v5、Learning v5 | 公式点と根拠資料を一体管理し、利用者別`exam_preferences`、不変snapshot schema v2、DB直接書込み用の検証・不変triggerを追加。不正なv4履歴はmigrationをversion 4のままrollbackし、推測修復しない |
| 公式点と個人目標 | domain、SQLite、API | 公式点は検証可能な根拠資料と同時に設定し、未知なら推測しない。個人目標は利用者DBへ保存し、明示的`NULL`をtombstoneとして保持。legacy `pass_score`は公式値へ昇格せず、妥当な整数だけ個人目標候補にする |
| attempt採点snapshot | `snapshot_schema_version=2` | 最大点、公式点・出典、個人目標・由来、`unit_correct`採点方式を開始時に固定する。後日の設定変更、問題廃止、再起動で過去のraw得点を改変しない |
| 決定論的result契約 | `result.official`、`result.practice_target` | 公式合否と個人目標達成を分離。`official_score_unavailable`、`practice_target_unset`、`mode_not_eligible`、`content_invalidated`、`incomplete_score_scope`、`legacy_score_ambiguous`を明示し、AIを参照しない |
| DB境界 | migration preflight、validator UDF、SQLite trigger | 不正出典JSON/URL、経路化不能exam ID、確定済みattemptの直接作成・identity/期限/score/snapshot/item/event改変、問題数不足、時刻矛盾を拒否。`recursive_triggers=ON`で`REPLACE`迂回も拒否 |
| Web fail-closed | `normalizeAttemptForClient()` | 未知のmode/status/snapshot/scoring policy、snapshotと閾値・出典・目標の不一致、item全数・廃止状態・件数・正誤矛盾を肯定結果として表示しない。後日invalid化された正規履歴だけは境界内のraw得点を保持し、両結果を未判定にする |
| 採点・結果回帰 | Python統合、Web node tests、TypeScript、対象ESLint、Ruff、差分検査 | Python 229 passed、215 warnings、Web 407 passed。型検査、対象lint、`ruff check`、`ruff format --check`、`git diff --check`成功 |
| 採点・結果独立レビュー | storage監査、cross-layer再現、修正後のfocused再実行 | 未解決P0/P1なし。reviewer側でもPython 229 passed、Web 407 passed、Ruff、format、TypeScript、対象ESLint、差分検査を確認。未知runtime契約、snapshot/result drift、item/得点矛盾、確定後invalid化raw履歴をREDで修復 |
| clean publisher検証 | cleanな`737e130d`から差分を同期したpublisher clone、Python 3.13 | Python 229 passed、215 warnings、Web 407 passed、TypeScript、対象ESLint、Ruff、format、JSON parse、`git diff --check`成功 |
| 採点・結果commit | `f2366799 feat: separate official and practice results` | `origin/tjm/implementation`へpush済み。PR #1はDraftのまま、merge・Ready化・release未実施 |
| 音声候補DB境界 | Learning migration v6、candidate/confirm/cancelのSQLite参照制約 | 候補はattempt・position・版・未解決状態へ結び付き、別問題・旧候補・二重確定・直接SQL迂回を拒否。独立再監査で未解決P0/P1/P2なし |
| 最終中核回帰 | `pytest -q` | 3811 passed、8 skipped、9 warnings、44.16秒。既知のCatalog並行初期化失敗は再発せず |

## 16.2 CP-14 証拠台帳（完了）

| 項目 | コマンド/証拠 | 結果 |
| --- | --- | --- |
| Web依存再現 | `npm ci --legacy-peer-deps`、Node 24.14.0 | 781 packages、`npm audit` 0件。`eslint-plugin-react-hooks` 7.1.1で発生した95 errorsを再現し、Next 16.2.12を維持したまま7.0.1へoverride固定 |
| Web全検証 | Node 24とDocker内Node 22.23.2でnode test、TypeScript、ESLint | 両環境で426 tests、型検査、TJM対象lint成功。最終lockでfull ESLintもerror 0 / warning 56 |
| production Web build | Node 22.23.2のDocker build | `/tjm`を含む58 routes、VAD自己配信asset、`npm audit` 0件 |
| wheel内容 | `scripts/validate_tjm_wheel.py`、`twine check` | packaged Next server/static、TJM Python/Web、VAD資産を検証。SHA256 `1befa5bb5075514a83bbb8c8a403a5e258302afd483559c0804a2bc2078bcf77` |
| wheel隔離導入 | Python 3.13.12の新規venvへwheel導入、`pip check` | 135 packages、依存整合成功。read-only CWDから`deeptutor start --home`の初回起動、再起動、offline backupを別homeへ復元して成功 |
| runtime home | runtime settings、setup persona seed、logging configのcall-time path解決 | CLI import前のCWDへ固定されず、明示`--home`配下だけへ設定・ログ・TJM dataを書き込む回帰test成功 |
| Docker amd64 | `deeptutor:tjm-final-20260815-amd64` | `linux/amd64`、image ID `sha256:09a13ef975e4456b753b11eb9d4e6c9749621bdadbaf8ead58e538fa1f7c7401` |
| Docker永続化 | production containerの初回起動、再起動、offline backup、別volume復元 | catalog、admin/一般利用者履歴、frontend `/api` proxyを含めて成功。`docker stop`成功、container消滅、API/frontend両port停止をfail-closedで確認 |
| compose契約 | source/GHCR/Podman/development composeの正規化 | data root全体の永続化、loopback既定、fork/local image、PocketBase `/pb_data`、Next proxy説明を統一。base、base+dev、GHCR、`compose.yaml`の4構成を検証 |
| release fail-closed | Docker/PyPI workflow | repository variableで外部公開を明示許可制にし、Dockerはtag/version・main・同一SHAの`Test Summary`・arm64 preflightを要求し、自動`latest`を抑止。PyPIも同一SHAの`Test Summary`成功と未使用versionを公開前に要求 |
| workflow静的検査 | actionlint v1.7.8、PyYAML parse、compose/release契約test | 3 workflowすべて成功、compose/release契約9 tests成功。docs・Docker teardownを含む局所回帰は25 tests成功 |
| commit/push | `a89a2531`、`08a1a9f6`、`f6cf077a`、`fb70f654`を`origin/tjm/implementation`へpush | 成功。remote CIで検証したcode headは`fb70f6543818531e97ce06bc1a748628120a3ef9`。PR #1はDraftのままで、merge、Ready化、release、publishは未実施 |
| remote CI | [Tests run 31862293486](https://github.com/tsutomu-n/DeepTutor/actions/runs/31862293486)、exact SHA `fb70f6543818531e97ce06bc1a748628120a3ef9` | `pull_request` runを2026-08-15T03:37:02Z〜03:40:40Zに実行。Ruff、Compose、Web、E2E、Python import/test 3.11〜3.14、wheel、amd64 Docker、`Test Summary`の必須15 jobsがすべて成功。arm64 preflightはPRでは設計どおりskip |
| remote CI失敗修復 | [run 31860640081](https://github.com/tsutomu-n/DeepTutor/actions/runs/31860640081)、[run 31861620876](https://github.com/tsutomu-n/DeepTutor/actions/runs/31861620876) | 初回はcontainer UID 1000所有・0600設定をhost `copytree`が読めず失敗し、`f6cf077a`でoffline root helper copyとfail-closed cleanupへ修正。次はrestart前のhost `chmod`が既存UID 1000 data rootで失敗し、`fb70f654`で新規dirだけchmod、既存dirはroot entrypointへ委ねる契約へ修正。ローカルDocker実測と最終remote runでinitial/restart/backup/restoreを確認 |
| arm64 | `workflow_dispatch verify_arm64=true` | 未実行の任意preflight。GHCR multi-arch releaseを許可する場合は成功を必須とする |

## 16.3 CP-15 証拠台帳（内部完了、外部待ち）

| 項目 | コマンド/証拠 | 結果 |
| --- | --- | --- |
| 日本語UI | `web/i18n/tjm.ts`、`web/locales/ja/tjm.json`、固定namespace test | 学習、試験、結果、復習、分析、管理、音声失敗を日本語化し、宅建固有defaultを持たない |
| 操作型E2E | real Chromiumで`npm run test:e2e:tjm` | 7/7成功、55.8秒。取り込み・人間レビュー・公開・演習・試験・復習・履歴・mobileを実APIで通過 |
| 音声自動受入 | Chromium fake microphone + 自己配信Silero VAD | 実VAD経路、終話、STT候補、取消、再認識、確認後確定、TTS中mic停止、音声障害後の画面回答を検証。VAD実経路は22.7秒 |
| 外部受入 | Pixel 9aで10問、権利確認済み宅建問題50問以上 | 未実施。端末操作と正当な問題・正式出典・対応する合格点根拠が提供されるまで完成判定しない |

## 17. 既存リスクと今回の扱い

- catalogは共有DB、learningは利用者別DBであるため、問題廃止と全利用者queue取消を単一transactionで即時反映する構造ではない。現在の契約は「各利用者の次のTJM操作より前に必ず同期し、その応答では旧版を有効扱いしない」である。管理操作直後に全利用者DBを物理更新する要件へ変える場合は、DB topologyまたは調整jobの別設計を停止条件として扱う。
- 認可の独立監査で確認したTJM利用者pathのadmin fallbackと、active exam中に別attempt/historyから同一問題のfeedbackを取得する経路は閉じた。ただしこれは同時API経路の遮断であり、利用者が試験前に知った正解、保存済み画面、別端末の記憶を防ぐ試験監督機能ではない。移行前から同一試験に複数のactive examがあるDBは推測で片方を終了せず保持し、新規開始だけをfail-closedにした。汎用`get_path_service()`のfallbackはTJM外に残る。
- cookie認証の全TJM更新routeは単一かつ明示allowlist上のOriginを必須とし、認証有効時のCORS `*`/`null`設定も拒否した。ただし同じ保護をTJM外の更新routeへ一括適用したとは主張しない。Node 22のproduction DockerではNext proxyがOrigin、Cookie、method、bodyを保持したが、実TLS reverse proxyは未確認である。Originが保持されない場合は検査を緩めず配備構成を修正する。
- 認証有効時もlocalhost frontend用のOriginは既定allowlistに残る。composeのhost bindはloopback既定へ修正したが、remote-only配備でlocalhostを信頼しないCORS policyは明示設定が必要である。
- 最終lockの`npm audit`は0件である。Next 16.2.12を維持しつつ、`eslint-plugin-react-hooks` 7.1.1が既存Web全体へ追加した95 lint errorsを避けるため、動作実績のある7.0.1をoverride固定した。将来更新時はfull ESLint 0 errorsとaudit 0を同時に再検証する。
- CP-04時点ではPython lockfileがなく範囲依存の最新値を解決した。現在は`uv.lock`を追加したが、Dockerのrequirements経路はまだ同一lockを消費しない。
- Dockerも `pip install -r requirements.txt` で当日最新を解決し、Python 3.11環境ではローカルPython 3.12環境と一部の解決版が異なる。production build成功は確認したが、将来の同一解決を保証する証拠ではない。
- mypyの現行コマンドは設定と最新stubが不整合である。TJM追加コードは局所型検査を通し、全体契約修復は独立変更として判断する。
- `CatalogStore`並行初期化testは独立review環境で3回中1回WAL lockを観測した一方、本作業環境の再実行10回は全成功で再現しなかった。今回差分が触れていないCatalog共通connection上の未確定flakyとして記録し、CP-14または最終全gateで再発した場合は既存失敗扱いで通過させない。
- ローカルNode 24とCI/Docker Node 22の双方で426 node tests、TypeScript、対象lintを実測し、Node 22 production buildも通した。今後もNode 22を配布の正本とする。
- 実宅建問題データはリポジトリに存在しない。著作権・正確性を推測せず、取り込み・審査機能の完成後に人間が正当なデータを投入する。
- 実宅建データに対応する公式合格点と根拠資料も同梱していないため、現時点の実データでは`official_score_unavailable`になる。これは汎用の採点機能欠陥ではなく、権利・正確性を確認した実データ受入れの外部待ちである。専用の年度fieldは未実装であり、「年度別合格点を実装済み」とは扱わない。
- SQLite境界には採点・履歴の主要不変条件を実装したが、local DBへ直接SQLを書ける主体による空文字attempt ID作成は拒否していない。通常APIは`att_<uuid>`を生成し採点改変には直結しないためP2として残し、DBを悪意ある同一OS利用者から守る耐タンパー性は主張しない。
- legacy整数`pass_score`の元の意味が公式点か利用者目標かは復元できない。正式出典なしに公式値へ昇格させず、利用者が変更・明示解除できる個人目標候補としてだけ遅延移行する判断を採った。この意味論を望まない運用では、利用者が目標を`NULL`へ設定すれば再出現しない。
- TJM詳細画面、管理導線、結果、音声失敗文言は固定日本語namespaceへ移行済みである。DeepTutor全体の既存翻訳warning 56件はTJM完成範囲ではないが、full ESLintはerror 0を維持する。
- sherpa ReazonSpeech int8 modelは約162MBでApache-2.0、実測対象CPUでは実時間未満だったが、modelファイル自体は同梱しない。管理者が正当な配布元から取得してpathを設定する。
- `edge-tts`はMicrosoft Edgeのonline音声serviceを使い、packageはLGPLv3である。可用性・規約・network依存があるため、既存TTS providerを残し自動fallbackにしない。
- VAD packageは自己配信できるが、実microphone可否・permission・AudioWorkletはbrowser/device依存である。Chromium fake microphoneで実経路を検証し、失敗時は画面回答へ戻れるようにした。実端末・主要ブラウザ全組合せの認証は配備側の受入試験として残る。
- arm64 Dockerはlocal builderで実行できず、GitHub ActionsのQEMU preflightも未実行である。amd64配布は実測済みだが、multi-arch GHCR公開は同一SHAのarm64 preflight成功をrelease workflowで必須化した。
- PyPIの`deeptutor==1.5.8`と同名wheel filenameは既に公開済みで、同一filenameは再利用できない。`ENABLE_PYPI_RELEASE`は既定無効とし、未使用versionと既存project権限を確認するかfork固有distribution名を決めるまで公開しない。
- forkのworkflowを有効化後、exact SHA `fb70f6543818531e97ce06bc1a748628120a3ef9`の必須jobsと`Test Summary`は成功した。使用中の`actions/checkout@v4`、`docker/build-push-action@v6`、`docker/setup-buildx-action@v3`にはNode 20 runtime廃止warningがあり、runnerがNode 24で強制実行している。今回の成功を損なうerrorではないが、各actionの対応版が提供された時点で更新して再検証する。

## 18. 追加監査の再現証拠

2026-08-03の追加監査は、cleanな`28747af3590065dc3a47f6abbacc1b84f6cc8037`に対して実施した。既存の対象テストは`53 passed`だったが、次を一時SQLite DBで再現した。

- review後に正解AからBへ編集しても、旧reviewのままBをpublishできる。
- `elapsed_ms=0`と未来client時刻を受理し、analytics平均が`0.0`になる。
- 期限後のGETとhistoryが`in_progress`、`submitted_at=null`のままになる。
- 復習開始後に追加されたqueue理由が、古いreview attempt提出で完了する。
- retired版がpending queueと新規review attemptに残る。
- GHCR composeは上流imageと部分mountを使い、TJM catalogと一般ユーザー履歴を永続化しない。
- 手元のwheelはTJM Python codeを含むが、packaged Next `server.js`と静的資産を含まない。
- PR #1はDraft/Openだが、GitHub上のworkflow run、check、reviewはいずれも0件である。

## 19. 修正継続対象と停止境界

次は停止条件ではない。失敗をREDとして固定し、原因を修正して再試験する。

- wheelのWeb資産欠落、clean install失敗、Docker再作成後のデータ消失
- 単体/API/E2E/CI/build/lint/型検査の失敗、新規ベースライン回帰
- 期限、レビュー、復習、日本語UI、自動音声検証の不備
- 局所修正または根拠付きリスク受容が可能な依存監査結果

次の境界でのみ作業を停止し、必要最小限の判断または外部操作を依頼する。

- 既存実データを保つ非破壊migrationを設計できず、backupと復旧経路を確保しても安全に進められない。
- GitHub Actionsがpush後もworkflowを認識せずowner側の有効化・権限変更が必要になる、またはGHCR/PyPI公開、本番配備、mainへのmerge、Ready化が必要になる。
- API key、認証情報、秘密情報、高額または有償の外部APIが必要になる。
- PyPI公開前に、既存`deeptutor` projectの権限と未使用versionを使うか、fork固有distribution名へ変更するかのユーザー判断が必要になる。
- 実宅建データで公式合否を受入検証する段階で、権利確認済み問題、正式出典、対応する合格点の根拠をユーザーから受け取る必要がある。汎用の公式点・個人目標・決定論的結果機能の実装は、この提供待ちを停止理由にしない。
- Pixel 9a等の対象実端末で、マイク、スピーカー、イヤホン、実browserを人間が操作する必要がある。
- 目的、基盤、既存ユーザーデータの前提を変える重大な設計分岐が発生し、実コードから合理的に決められない。

上記以外の局所・可逆な実装判断は、テストと本計画を更新しながら自律的に進める。
