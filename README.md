# NIC — Niigata Information Connector

**新潟県の情報（気象・統計・観光・防災・オープンデータ）にアクセスする CLI / MCP ツールキット**。

新潟県の公式データ源（気象庁・新潟県・新潟市・国土交通省）に直接アクセスし、
必要な情報だけを最小トークンで AI エージェントへ届ける。
**CLI（`ngt`）** と **MCP（`ngt-mcp`）** の 2 つのインターフェースを持つ。

> システム名は NIC。コマンド名は Python 版と共通の `ngt` / `ngt-mcp` で統一している。

```
あなたのAIアシスタント（Claude / ChatGPT / Cursor など）
        │  「新潟の積雪は？」「十日町に警報は？」「雨の日のおすすめは？」
        ▼
┌─────────────────────────────────────┐
│  NIC（この基盤）                     │
│  ・新潟県の気象・防災・観光・統計データ │
│  ・出典明記つき・常に最新・高速キャッシュ│
└─────────────────────────────────────┘
        │  公式データ源（気象庁・新潟県・新潟市・国土交通省）
        ▼
   新潟のリアルな今（実データ）
```

---

## パッケージ構成（Monorepo）

| パッケージ | 言語 | 内容 | インストール |
|---|---|---|---|
| [`packages/py/`](packages/py/) | Python 3.13+ | SDK（ライブラリ）+ CLI + MCP | `uv sync` / `pip install` |
| [`packages/ts/`](packages/ts/) | TypeScript (Node 18+) | CLI + MCP（`npx` で実行可） | `npm install @cinnamobot/nic` |

同じコア設計（キャッシュ・エラー処理・出典管理を一元化）を 2 言語で提供する。

### Python 版（SDK としても利用可）

```bash
cd packages/py
uv sync
ngt weather --station 長岡      # CLI
ngt-mcp                          # MCP サーバー
```

```python
# ライブラリとしても利用可能
from nic.core.amedas import AmedasClient

with AmedasClient() as client:
    data = client.fetch_precipitation(codes=["54232", "54841"])
```

### TypeScript 版（npx で即利用）

```bash
# CLI（インストール不要）※ -p でパッケージ指定し、コマンド名 ngt を明示する
npx -y -p @cinnamobot/nic ngt weather --station 長岡

# MCP サーバー
npx -y -p @cinnamobot/nic ngt-mcp
```

```bash
cd packages/ts
npm install
npm run build    # tsc でビルド
npm test         # vitest（52 テスト）
```

---

## データ源とライセンス

| データ源 | 内容 | ライセンス / 利用条件 |
|---|---|---|
| 気象庁「最新の気象データ」CSV | アメダス（積雪・気温・降水量） | 気象庁ウェブサイト利用規約（出典表示必須） |
| 気象庁防災情報XML配信 | 警報・注意報電文（VPWW53/VPWW54） | 公共データ利用規約 第1.0版 |
| 新潟県オープンデータ | データセット一覧・人口・道の駅 | 新潟県オープンデータ利用規約（出典表示） |
| 新潟市オープンデータ | 観光入込客数・温泉GIS・観光データセット | クリエイティブ・コモンズ 表示（CC-BY） |
| 国土数値情報（国土交通省） | 集客施設 P33（2014年度版） | 国土数値情報利用約款（出典明記で無償利用可） |

- 本ツール（NIC）自体は **MIT License** で公開。
- 取得・表示するデータの権利は各データ源に帰属し、各利用条件に従う必要がある。
- 本ツールの出力（CLI / MCP）には必ず出典が含まれており、各データ源の利用条件（出典表示）を満たすことを意図している。

各パッケージの詳細（コマンドリファレンス・データカバレッジ・セットアップ）は
[`packages/py/README.md`](packages/py/README.md) と [`packages/ts/README.md`](packages/ts/README.md) を参照。

## 開発

```bash
# Python 版
cd packages/py && uv sync && uv run pytest

# TypeScript 版
cd packages/ts && npm install && npm test
```

ブランチ運用: `main`（本番）← `develop`（開発）← `feature/*`（作業）
