# NIC — Niigata Information Connector (TypeScript)

新潟県の情報（気象・統計・観光・防災・オープンデータ）にアクセスする **CLI / MCP ツールキット**。
新潟県の公式データ源（気象庁・新潟県・新潟市・国土交通省）に直接アクセスし、
必要な情報だけを最小トークンで AI エージェントへ届ける。

> 本パッケージは Python 版（`app-tech/nic`）の TypeScript 移植版。
> `npx` でインストールでき、AI エージェントのツールとしてそのまま使える。

---

## インストール

```bash
# グローバル（またはプロジェクト）にインストール
npm install -g @cinnamobot/nic

# npx で直接実行（インストール不要）
npx @cinnamobot/nic weather --station 長岡
```

### CLI（`nic`）

```bash
nic weather --station 長岡   # 気温・降水量（気象庁アメダス）
nic snow --rank              # 積雪ランキング（冬季のみ）
nic warning --level 市町村   # 警報・注意報（気象庁防災情報XML）
nic tour --onsen             # 温泉スポット（新潟市オープンデータ）
nic tour --irikomi           # 観光入込客数
nic stats --population       # 人口（新潟県オープンデータ）
nic search 湯沢              # 全データ横断検索

# 共通オプション
nic weather --station 長岡 --json   # JSON 出力（AI 向け）
nic weather --force                 # キャッシュ無視で再取得
```

- 出力はプレーンな表形式（AI がパースしやすく、装飾なし）
- `--json` で機械可読出力
- エラー時は「なぜ失敗したか」をヒント付きで返す（例: 積雪データは冬季のみ提供）

### MCP サーバー（`nic-mcp`）

MCP（Model Context Protocol）対応クライアント（Claude Desktop / Cursor 等）から使える。
`@cinnamobot/nic` をインストールすると `nic-mcp` バイナリが使えるようになる。

```jsonc
// claude_desktop_config.json
{
  "mcpServers": {
    "nic": {
      "command": "npx",
      "args": ["-y", "@cinnamobot/nic", "mcp"]
    }
  }
}
```

または `nic-mcp` バイナリを直接指定（npm でインストール済みの場合）:

```jsonc
{
  "mcpServers": {
    "nic": {
      "command": "nic-mcp"
    }
  }
}
```

提供ツール（7 つ）:

| ツール | 内容 |
|---|---|
| `get_snow_info` | 積雪情報（全 44 観測所 or 指定） |
| `get_weather_info` | 最高・最低気温・1 時間降水量 |
| `get_warning_info` | 警報・注意報（府県〜市町村 4 階層） |
| `get_tourist_spots` | 観光スポット一覧（温泉・集客施設） |
| `get_tour_recommendation` | 天気×おすすめ観光 |
| `get_niigata_stats` | 統計・オープンデータ（人口・道の駅・データセット） |
| `search_niigata_data` | 全データ横断検索 |

※ `npx -y @cinnamobot/nic mcp` はパッケージ内の `nic-mcp` バイナリを起動する。
詳細は [package.json](package.json) の `bin` を参照。

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

---

## 開発

```bash
npm install
npm run build     # tsc で dist/ にビルド
npm test          # vitest（52 テスト）
npm run typecheck # 型チェックのみ
```

### プロジェクト構成

```
src/
  core/           データ取得層（キャッシュ・エラー処理・出典管理を一元化）
    amedas.ts     気象庁アメダス（積雪・気温・降水量）
    warning.ts    気象庁防災情報XML（警報・注意報）
    opendata.ts   新潟県オープンデータ（統計・道の駅）
    tourism.ts    観光スポット（温泉・集客施設・入込客数・P33 Shapefile）
  cli/            CLI エントリポイント（commander）
  mcp/            MCP サーバーエントリポイント（@modelcontextprotocol/sdk）
  index.ts        ライブラリ公開エントリ
```

## 開発メモ

- 新潟県 CKAN API（`ckan.pref.niigata.lg.jp`）は 2026-08 現在 DNS 解決不可のため、
  公式サイトの CSV へ自動フォールバックする（実行時「注:」で通知）。
- 積雪データは夏季（概ね 5〜9 月）提供休止のため、`snow` は 404 エラー + ヒントを返す。
- 気象庁 CSV は Shift_JIS のため `iconv-lite` でデコードする。
- P33 集客施設は Shapefile（DBF + SHP）を ZIP 内から直接パースする（依存なし・軽量）。
