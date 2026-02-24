<!-- markdownlint-disable MD033 -->
<!-- markdownlint-disable MD041 -->

<div align="center">

# 🤖 AstrBot 能動的チャットプラグイン (Plus Fork)

[繁體中文](README.md) | [English](README_EN.md) | 日本語

</div>

<p align="center">
  <img src="https://img.shields.io/badge/License-AGPL_3.0-blue.svg" alt="License: AGPL-3.0">
  <img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/AstrBot-v4.8.0+-orange.svg" alt="AstrBot v4.8.0+">
</p>

<p align="center">
  <img src="logo.png" width="200" alt="logo" />
</p>

---

[AstrBot](https://github.com/AstrBotDevs/AstrBot) 向けの能動的メッセージプラグインです。セッションの沈黙後、ランダムな間隔でコンテキストを認識し、ペルソナに合致した動的感情を含む会話を Bot が能動的に開始します。

## 🙏 原作者への謝辞

本プロジェクトは [DBJD-CR/astrbot_plugin_proactive_chat](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat) をベースに改変したものです。原作者 **DBJD-CR** および協力者の素晴らしい仕事に感謝します。原プロジェクトは、マルチセッション対応、永続化、おやすみモード、TTS 統合、分割返信など、完全な能動的メッセージフレームワークを提供しています。

> コアコンセプトを気に入っていただけたら、ぜひ原リポジトリに ⭐ Star をお願いします。

## ✨ 本 Fork の新機能

### 1. モジュール化リファクタリング + パフォーマンス最適化

元の 2500 行以上の `main.py` を明確なモジュール構造に分割し、包括的なパフォーマンス改善を実施：

| モジュール | 責務 |
| :--- | :--- |
| `core/utils.py` | ユーティリティ関数（おやすみ判定、UMO 解析、ログフォーマット） |
| `core/config.py` | 設定管理（検証、セッション設定取得、バックアップ） |
| `core/scheduler.py` | スケジューリングロジック（加重ランダム間隔、時間帯ルールマッチング） |
| `core/messaging.py` | メッセージ送信（デコレータフック、分割返信、履歴サニタイズ） |
| `main.py` | プラグインエントリポイント（ライフサイクル、イベント、コアオーケストレーション） |

パフォーマンス最適化のハイライト：
- プラグインクラスに `__slots__` を使用してメモリオーバーヘッドを削減
- プライベート/グループメッセージハンドラを共通の `_handle_message()` に統合、約 80% の重複コードを排除
- `_add_scheduled_job`、`_cancel_timer`、`_call_llm` などのヘルパーメソッドを抽出して冗長性を削減
- `_is_chat_allowed` が事前取得した設定を受け入れ、`check_and_chat` での重複クエリを回避
- 正規表現のプリコンパイル、`frozenset` 定数、非同期が不要な関数の同期化
- 開発者向けの詳細なコードコメントを全体に追加

### 2. template_list による動的セッション管理

`private_sessions` と `group_sessions` を 5 つの固定スロット（`session_1`..`session_5`）から AstrBot の `template_list` 型に変換：

- セッション数の制限なし — 自由に追加/削除可能
- 設定 JSON が約 2500 行から約 660 行に削減（74% 削減）
- WebUI の読み込みが高速化、操作がスムーズに

### 3. schedule_rules — 時間帯別加重ランダムスケジューリング

すべての `schedule_settings` に `schedule_rules`（`template_list` 型）を追加。時間帯ごとのトリガー間隔の加重ランダム分布を設定可能：

- 各ルールに `start_hour`、`end_hour`、`interval_weights` を設定
- `interval_weights` フォーマット：`"20-30:0.2,30-50:0.5,50-90:0.3"`（分:重み）
- 現在の時間帯にマッチするルールで加重ランダム間隔を選択；マッチしない場合はグローバル最小/最大間隔にフォールバック
- 日をまたぐ時間帯に対応（例：22-6）

## 🌟 原プロジェクトから継承した機能

- マルチセッション対応（個人チャット + グループチャット、完全分離）
- グローバル + 個別セッション設定システム
- 沈黙時間ベースのタイマートリガー
- 自動能動的メッセージ（ユーザー入力不要で開始）
- コンテキスト認識 + 完全なペルソナ対応
- 動的感情（未返信カウンター）
- セッション永続化（再起動後のタスク復元）
- おやすみモード
- TTS 音声統合
- 分割返信（入力間隔シミュレーション）
- デコレータフック（スタンプ/感情プラグインと互換）
- 高度な設定可能（WebUI ベース、コード変更不要）

### 4. livingmemory 統合

[astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) とのオプション統合 — 能動的メッセージ生成時に関連する長期記憶を検索し、system prompt に注入することで、よりパーソナライズされた文脈豊かな会話を実現します。

- `context_aware_settings.memory_top_k` で検索数を制御（0 で無効化）
- 完全にオプション：livingmemory 未インストールでもエラーなく動作
- 検索クエリ優先順位：コンテキストタスクの hint/reason → 現在時刻にフォールバック

## 🚀 インストール

1. 本リポジトリから `.zip` をダウンロードし、AstrBot WebUI の「ファイルからインストール」で導入
2. コア依存関係 `APScheduler` と `aiofiles` は通常 AstrBot に同梱
3. WebUI → プラグイン設定で、対象セッションと能動的メッセージの動機を設定
4. 保存して使用開始

## 📂 プロジェクト構造

```
astrbot_plugin_proactive_chat/
├── core/                      # コアモジュール
│   ├── __init__.py            # モジュールエクスポート
│   ├── config.py              # 設定管理
│   ├── context_predictor.py   # コンテキスト認識スケジューリング（LLM 予測）
│   ├── llm_helpers.py         # LLM ヘルパー（リクエスト準備、記憶検索、LLM 呼び出し）
│   ├── messaging.py           # メッセージ送信
│   ├── scheduler.py           # スケジューリングロジック
│   ├── send.py                # 能動的メッセージ送信（TTS / テキスト / 分割）
│   └── utils.py               # ユーティリティ
├── main.py                    # プラグインエントリポイント
├── _conf_schema.json          # 設定スキーマ定義
├── metadata.yaml              # プラグインメタデータ
├── requirements.txt           # 依存関係
├── CHANGELOG.md               # 更新ログ
└── LICENSE                    # AGPL-3.0
```

## 🌐 プラットフォーム対応

| プラットフォーム | 対応状況 |
| :--- | :--- |
| QQ 個人 (aiocqhttp) | ✅ 完全対応 |
| Telegram | ❓ 理論上対応 |
| 飛書 | ❓ 理論上対応 |

## 📄 ライセンス

GNU Affero General Public License v3.0 — [LICENSE](LICENSE) を参照。

## 💖 関連リンク

- 原プロジェクト：[DBJD-CR/astrbot_plugin_proactive_chat](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat)
- AstrBot：[AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)
