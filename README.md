# claude-codex

Claude Codeの操作画面から、ローカルの変換プロキシを通して別のモデルを使うための非公式ツールです。Windows向けランチャー、ccproxy-apiへのパッチ、モデル設定、オフラインの単体テストを公開しています。

**開発者向けのソースプレビューです。** 新しいPCへの本導入、実アカウントでの全経路、macOS・Linuxでの動作は未検証です。Anthropic、OpenAI、各CLI開発元の公式製品ではなく、承認や提携を示すものではありません。

English: An unofficial Windows launcher and local protocol gateway for using the Claude Code interface with other model backends. This is a developer source preview, not a turnkey release. Claude Code, Codex CLI, accounts and subscription credentials are not included.

## 何をするものか

Claude Code形式の要求をローカルのccproxyで変換し、選択したバックエンドへ渡します。モデルとaliasの正本は `runtime/models.json` です。既定バックエンドは `gpt`、モデルは `gpt-6.1-sol`。利用できるモデルは利用者の契約とプロバイダーの提供状況に依存します。alias名がClaudeモデルの利用を保証するわけではありません。

GPT経路のWeb検索はOpenAI経路で扱う設計です。任意バックエンドの定義も含みますが、すべての認証・接続経路を検証したものではありません。意図しない別プロバイダーへ切り替えず、利用する経路を確認してください。

公開用設定は通常の権限確認を有効にし、確認省略や個人用プラグインの有効化を既定にしていません。既存利用者の設定を自動的に安全なものへ直す機能ではありません。

## 前提

- Windows、PowerShell 5.1、Python 3.12、uvを想定しています。
- Claude Code CLIと、選択した経路に必要なCLI・契約・認証は利用者側で用意します。
- パッチはccproxy-api **0.2.10** 向けです。別の版への互換性は保証しません。既に変更されたccproxyへ無確認で重ねないでください。
- Python開発依存は `pyproject.toml` と `uv.lock`、任意のnativeランチャーは `launchers/native/Cargo.toml` と `Cargo.lock` で管理します。
- CLI本体、認証情報、個人のルール・スキル・会話ログは同梱しません。

## 最初は確認だけ行う

リポジトリのルートから実行します。`-Apply` を付けない場合は変更予定の場所をJSONで表示して終了し、前提コマンドの実行、認証、インストール、書込みは行いません。

```powershell
powershell -NoProfile -File scripts/install.ps1
```

依存を用意した開発環境では、次のオフライン検査を実行できます。

```powershell
python scripts/generate-model-configs.py --check
python -m pytest tests/ --ignore=tests/integration/ -q
cargo build --release --locked --offline --manifest-path launchers/native/Cargo.toml
```

Cargoのオフラインビルドには、依存のキャッシュとWindowsのビルドツールが必要です。単体テストの成功は実アカウントでの動作やインストール成功を保証しません。

## 子モデルを明示して使う

導入済みの別環境で、対応モデルと effort を確認し、必要な子モデルだけ起動時に指定します。

```powershell
claude-codex --list-subagent-models
claude-codex --subagent deepseek=high --subagent glm-thinking=max
```

無指定では、子モデル指定機能が別 provider へ自動変更しません。Agent 定義の注入は新規セッション限定で、resume/continue/fork には対応しません。明示子セッションでは親履歴の fork を拒否し、session、定義、model、effort の整合を照合します。

`python scripts/resolve-subagent.py deepseek=high` は Workflow 向けの事前検査です。実際の Workflow 呼出しを拘束するものではなく、呼出側で明示的な利用者指示と model/effort を確認します。別 provider へ渡す情報は機密を含まない自己完結の作業文脈に限定してください。

## 本導入の影響

**本導入は明示的に `-Apply` を付けた場合だけ動きます。** このプレビューでは日常環境への導入を推奨しません。まず別のWindowsユーザーまたは使い捨てVMで、対象パス・既存ファイル・バックアップを確認してください。

インストーラーは次を行います。

- ユーザー環境変数 `CLAUDE_CODEX_PROXY_TOKEN` を必要に応じて作成します。
- 利用者のuv tool環境のccproxyへパッチを適用します。`-HomeDir` だけではパッチ先やユーザー環境変数を隔離できません。
- `bin` にランチャーと `claude` ラッパーを配置します。PATHによっては既存コマンドの選択が変わります。
- `.claude-codex` と `.claude-codex-proxy` を作成します。既存の `.claude` のskills、rules、commands、agents、plugins、CLAUDE.md、memoryへの共有リンクを作る場合があります。共有先の編集は元環境へも影響します。
- `-RestartProxy` を併用すると実プロキシへ影響します。

確認済みの別環境でのみ `powershell -NoProfile -File scripts/install.ps1 -Apply` を使ってください。導入が済んだ環境では `claude-codex` がランチャーです。起動前にプロキシがloopbackに限定されていることを確認します。認証操作やサービスへの要求は、各提供元の利用条件と利用者の承認に従ってください。

## Install conflict recovery

競合で停止した場合は、表示された対象とバックアップを比較し、利用者の変更を先に保全します。`-Force` は既存ファイルの保護を解除しません。特定ファイルだけの取り込みを確認した場合は、UTF-8のJSONに `targets`（対象の絶対パス配列）と `reason`（確認した理由）を記載し、`-AdoptionFile` へ渡す仕組みがあります。差分を確認せず実行しないでください。

自動アンインストールや一括ロールバックは用意していません。導入後の復旧はインストール記録とバックアップを照合し、対象を選んで戻します。共有リンクの参照先を削除しないでください。実導入・復旧の検証は未完了です。

## 利用条件と安全性

[Anthropicのゲートウェイ文書](https://code.claude.com/docs/en/llm-gateway)では非Claudeモデルへのルーティングはサポート対象外とされています。MITライセンスは第三者サービスの利用権や商標使用の許可を与えません。許可された認証経路と用途を確認してください。認証や利用枠の回避、共有アカウントの提供を目的としません。

プロキシはloopbackで使い、LANやインターネットへ公開しないでください。トークン、OAuthファイル、会話ログをIssueへ貼らないでください。脆弱性の扱いは [SECURITY.md](SECURITY.md) を参照してください。

## ライセンスと開発

自作部分は [MIT](LICENSE)。第三者由来コードには [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) の権利表示と条件が適用されます。変更提案と検証方法は [CONTRIBUTING.md](CONTRIBUTING.md) にまとめています。
