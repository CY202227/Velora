# Velora

[![CI](https://github.com/CY202227/Velora/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/CY202227/Velora/actions/workflows/ci.yml)

[简体中文](README.md) | [English](README.en.md) | 日本語

Velora は、あなたのことを覚え、本当に価値があるときだけそっと現れる個人向け AI です。長期記憶を備えた LLM デスクとして、OpenAI 互換クラウドモデルと、任意で管理できるローカルモデルをサポートします。

会話の保存、根拠を確認できる記憶の想起と訂正、圧縮されたコンテキスト、リマインダー、任意の MCP クライアントを提供します。TTS、Notion/Gmail コネクター、専用実行サンドボックスは今後の計画であり、現時点の提供機能ではありません。

## 30 秒で起動

Docker は Velora とメモリサービスを別コンテナとして起動します。ローカルの Python、Node、メモリサービスのチェックアウトは不要です。実際にモデルへ問い合わせる時だけ API キーが必要です。

```bash
copy .env.docker.example .env.docker
# ライブチャットを使う場合は .env.docker に VELORA_LLM_API_KEY を設定します。
docker compose --env-file .env.docker up --build
```

http://127.0.0.1:8030/ を開きます。デモデータを削除して停止するには次を実行します。

```bash
docker compose --env-file .env.docker down --volumes
```

Docker プロファイルでは shell、Python、ワークスペースファイル、Web 検索ツールを無効化しています。データは `velora-data` と `memory-data` の別々の volume に保存されます。

## 安全なローカル UI デモ

普段の会話を表示せずに Desk をデモするには、別のデータベースを使います。

```powershell
copy .env.demo.example .env.demo
$env:VELORA_ENV_FILE=".env.demo"
.venv\Scripts\python -m server.main
```

この設定はポート `8031` と専用の `velora_data/demo.db` を使います。メモリ sidecar、MCP、リマインダー、ローカルツール、Web 検索を無効化します。モデルキーがなくても、UI・ペルソナ・言語切り替えを確認できます。

## 1 メッセージの流れ

![Velora agent loop](assets/architecture.svg)

1. Desk がメッセージを送り、サービスはセッション、ペルソナ、直近の文脈を読み込みます。
2. 関連する長期記憶を根拠付きで想起し、利用者は直接訂正できます。
3. 直近の会話、上限付きに圧縮した履歴、検証可能な記憶をモデルへの入力にまとめます。
4. モデルは限定された回数だけツールを使えます。ツール実行後は古いストリーミング下書きを破棄します。
5. 会話を保存し、本人情報・好み・長期計画のような高信号の情報だけを長期記憶へ書き込みます。

## ローカル開発

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[memory,dev]"
copy .env.example .env
# VELORA_LLM_API_KEY / VELORA_LLM_BASE_URL / VELORA_LLM_MODEL を設定します。
.venv\Scripts\python -m server.main
```

http://127.0.0.1:8030/ を開きます。Desk を変更した場合は `cd desk && npm run build` を実行します。生成物は `server/static` から配信されます。

### 任意のローカルモデル

Desk の設定からローカル小型モデルを有効にすると、GGUF モデルと `llama-server` を使い、`127.0.0.1:8040` で起動できます。起動時に自動ダウンロードされることはありません。

### データベースとテスト

標準のデータベースは SQLite です。`VELORA_DATABASE_URL` を設定すれば MySQL も利用できます。

```powershell
.venv\Scripts\python -m pytest
```

## 安全性

shell、Python、ワークスペースファイルのツールは、Velora を起動した人と同じ OS 権限で実行されます。専用サンドボックスが利用可能になるまでは、既定で無効のままにし、信頼できるローカル利用時だけ有効化してください。

## ライセンス

MIT。詳細は [LICENSE](LICENSE) を参照してください。
