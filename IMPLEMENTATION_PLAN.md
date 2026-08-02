# TJM 実装計画

更新: 2026-08-02_22:45 (Asia/Tokyo)

## 1. 状態

- 計画状態: 実行中
- 現在のチェックポイント: CP-12 GitHub反映
- 作業ブランチ: `tjm/implementation`
- 基準: DeepTutor v1.5.8 / `44fa7a1552b88f9d8ce2c22259128a15ae2eb0c8`
- リポジトリルート: `/home/tn/projects/DeepTutor`
- 完了宣言: まだ行わない。CP-04〜CP-11は完了したが、commit、push、Draft PRが未完了である。

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

- `HEAD`、`origin/main`、指定基準commitはすべて `44fa7a15` で一致し、基準commitとの差分はない。
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

## 5. 予定する責任分離

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
- `attempts`: practice/exam/review、開始・提出・集計・試験設定snapshot
- `attempt_items`: 出題順、固定された問題版、表示・回答時刻
- `answer_events`: 選択、自信度、ヒント、音声候補、音声確認、変更履歴
- `review_queue`: 復習理由、優先度、次回予定、解消状態

### 5.2 API

FastAPIへ `/api/v1/tjm` を追加する。通常利用APIは既存`require_auth`、取り込み・レビュー・公開APIは`require_admin`で保護する。

主な契約は次のとおり。

- 試験定義: `GET/POST/PATCH /exams`
- 取り込み: `POST /imports`、`GET /imports/{id}`
- 人間レビュー: `GET /review/questions`、`PATCH /review/questions/{version_id}`、`POST /review/questions/{version_id}/publish|reject|retire`
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

状態: 実行中

完了条件:

- 変更を意味のあるまとまりでcommitする。
- `tjm/implementation`をpushする。
- 変更、検証結果、既存/残存リスクを記したDraft PRを作る。
- `main`へmergeしない。

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

## 16. 既存リスクと今回の扱い

- npm auditは10件から5件へ低減した。Nextを16.2.12へ更新して認証proxy回避の既知範囲を外した。残るhigh 3件はNext 16.2.12内のPostCSS 8.4.31とsharp 0.34.5、moderate 2件はExcelJS 4.4.0内のuuid 8.3.2とその親packageである。npm提示の自動修正はNext 9.3.3またはExcelJS 3.4.0への破壊的downgradeなので適用しない。
- CP-04時点ではPython lockfileがなく範囲依存の最新値を解決した。現在は`uv.lock`を追加したが、Dockerのrequirements経路はまだ同一lockを消費しない。
- Dockerも `pip install -r requirements.txt` で当日最新を解決し、Python 3.11環境ではローカルPython 3.12環境と一部の解決版が異なる。production build成功は確認したが、将来の同一解決を保証する証拠ではない。
- mypyの現行コマンドは設定と最新stubが不整合である。TJM追加コードは局所型検査を通し、全体契約修復は独立変更として判断する。
- ローカルNode 24とCI/Docker Node 22が異なる。Docker buildをNode 22の正本とし、TJM Web検証でもNode 22経路を残す。
- 実宅建問題データはリポジトリに存在しない。著作権・正確性を推測せず、取り込み・審査機能の完成後に人間が正当なデータを投入する。
- TJM詳細画面の固定UI文言は現時点で英語を正本とし、global navigationだけ既存en/zh catalogへ追加した。機能境界とは分離しているが、日本語UIを配布要件にする場合はlocale追加が必要である。
- sherpa ReazonSpeech int8 modelは約162MBでApache-2.0、実測対象CPUでは実時間未満だったが、modelファイル自体は同梱しない。管理者が正当な配布元から取得してpathを設定する。
- `edge-tts`はMicrosoft Edgeのonline音声serviceを使い、packageはLGPLv3である。可用性・規約・network依存があるため、既存TTS providerを残し自動fallbackにしない。
- VAD packageは自己配信できるが、実microphone可否・permission・AudioWorkletはbrowser/device依存である。Chromium fake microphoneで実経路を検証し、失敗時は画面回答へ戻れるようにした。実端末・主要ブラウザ全組合せの認証は配備側の受入試験として残る。

## 17. 重大な停止条件

- 指定commitと実際の正本が一致しなくなり、未確認の上流変更が混入した。
- 既存認証/ユーザー分離とTJM保存要件が両立せず、schema/auth方針の変更が必要になった。
- 正式問題データの利用許諾や出典が不明なまま、実データ同梱が必要になった。
- ローカル音声modelの配布ライセンス、サイズ、対応CPUが配布形態を決める重大分岐になった。
- 外部書込み、秘密情報、実環境deploy、高額API利用、破壊的migrationが必要になった。

上記以外の局所・可逆な実装判断は、テストと本計画を更新しながら自律的に進める。
