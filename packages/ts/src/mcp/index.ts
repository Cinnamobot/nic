#!/usr/bin/env node
/**
 * MCP サーバー: 新潟県データを MCP ツールとして公開する。
 *
 * `npx nic-mcp` で起動し、stdio トランスポートで
 * MCP クライアント（Claude Desktop / Cursor 等）に以下の 7 ツールを提供する:
 *
 * - get_snow_info:         積雪情報（気象庁アメダス、新潟県内観測所）
 * - get_weather_info:      気温（最高・最低）と降水量（気象庁アメダス）
 * - get_niigata_stats:     統計・オープンデータ（人口・道の駅・データセット一覧）
 * - get_tourist_spots:     観光スポット（温泉・集客施設、新潟市オープンデータ等）
 * - get_tour_recommendation: おすすめ観光ルート（スポット統合 + 入込客数・雨情報）
 * - get_warning_info:      警報・注意報（気象庁防災情報XML、府県〜市町村 4 階層）
 * - search_niigata_data:   全データ横断検索（観測所・人口・道の駅・データセット）
 *
 * 実装は MCP 公式 SDK（@modelcontextprotocol/sdk）を使用し、コア
 * （nic/core/amedas・opendata・tourism・warning）のクライアントを
 * そのままツールとして公開する。全ツールのレスポンスには出典
 * （source / source_url）を含める。
 *
 * データ取得はコア側の TTL 付きキャッシュに従う（アメダス 300 秒 /
 * オープンデータ 3600 秒 / 観光 3600 秒 / 防災 60 秒）。force を指定すると
 * キャッシュを無視して再取得する。
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  type Tool,
} from "@modelcontextprotocol/sdk/types.js";

import { AmedasClient, SOURCE_TEXT as AMEDAS_SOURCE, SOURCE_URL as AMEDAS_SOURCE_URL, type AmedasData, type Observation, type Station } from "../core/amedas.js";
import { OpenDataClient, OpenDataError, type Dataset, type MichiNoEki, type PopulationRecord } from "../core/opendata.js";
import { SOURCE_TEXT as TOURISM_SOURCE, SOURCE_URL as TOURISM_SOURCE_URL, TourismClient, type Spot, type TourismDataset, type TourismStat } from "../core/tourism.js";
import { NIIGATA_PREF_CODE, SOURCE_TEXT as WARNING_SOURCE, SOURCE_URL as WARNING_SOURCE_URL, WarningClient, type WarningArea, type WarningKind, type WarningLevel } from "../core/warning.js";
import { getActiveAreas, statusSummary, summary as warningSummary } from "../core/warning.js";
import { haversineKm } from "../core/util.js";

import pkg from "../../package.json" with { type: "json" };

export const VERSION: string = pkg.version;

// アメダス / オープンデータ共通のキャッシュ TTL（秒）
const MCP_TTL = 300.0;
// 警報・注意報（防災情報）のキャッシュ TTL（秒）。フィードは毎分更新されるため 60 秒。
const MCP_WARNING_TTL = 60.0;

// 各ツールのデフォルト表示件数（結果が巨大になりすぎないよう制限する）
const DEFAULT_LIMIT = 50;

// ---------------------------------------------------------------------------
// JSON 変換ヘルパー（コアのデータ型 → プレーンオブジェクト）
// ---------------------------------------------------------------------------

function formatUtc(dt: Date): string {
  return dt.toISOString();
}

function observationJson(obs: Observation): Record<string, unknown> {
  return {
    station_code: obs.station.code,
    station_name: obs.station.name,
    value: obs.value,
    unit: "cm",
    quality: obs.quality,
    quality_text: obs.qualityText,
    observed_at: formatUtc(obs.observedAt),
  };
}

function datasetJson(d: Dataset): Record<string, unknown> {
  return {
    id: d.id,
    name: d.name,
    category: d.category,
    description: d.description,
    fields: d.fields,
    fiscal_year: d.fiscalYear,
    update_frequency: d.updateFrequency,
    format: d.format,
    url: d.url,
    department: d.department,
  };
}

function populationJson(r: PopulationRecord): Record<string, unknown> {
  return {
    date: r.date,
    municipality_code: r.municipalityCode,
    municipality_name: r.municipalityName,
    total: r.total,
    male: r.male,
    female: r.female,
  };
}

function michinoekiJson(st: MichiNoEki): Record<string, unknown> {
  return {
    id: st.id,
    name: st.name,
    route: st.route,
    address: st.address,
    phone: st.phone,
  };
}

function opendataWarnings(client: OpenDataClient): string[] {
  return [...client.warnings];
}

function tourismWarnings(client: TourismClient): string[] {
  return [...client.warnings];
}

function spotJson(s: Spot): Record<string, unknown> {
  return {
    id: s.id,
    name: s.name,
    category: s.category,
    lat: s.lat,
    lon: s.lon,
    address: s.address,
    phone: s.phone,
    url: s.url,
    description: s.description,
    source: s.source,
    source_url: s.sourceUrl,
  };
}

function tourDatasetJson(d: TourismDataset): Record<string, unknown> {
  return {
    id: d.id,
    name: d.name,
    title: d.title,
    description: d.description,
    license: d.license,
    license_url: d.licenseUrl,
    updated_at: d.updatedAt,
    url: d.url,
    resources: [...d.resources],
    source: d.source,
    source_url: d.sourceUrl,
  };
}

function tourStatJson(s: TourismStat): Record<string, unknown> {
  return {
    year: s.year,
    era_year: s.eraYear,
    total: s.total,
    event_total: s.eventTotal,
    spot_total: s.spotTotal,
    nature: s.nature,
    history_culture: s.historyCulture,
    onsen_health: s.onsenHealth,
    sports_recreation: s.sportsRecreation,
    urban_tourism: s.urbanTourism,
    other: s.other,
    source: s.source,
    source_url: s.sourceUrl,
  };
}

function kindJson(k: WarningKind): Record<string, unknown> {
  return {
    name: k.name,
    code: k.code,
    status: k.status,
  };
}

function areaJson(a: WarningArea): Record<string, unknown> {
  return {
    name: a.name,
    code: a.code,
    kinds: a.kinds.map(kindJson),
    status_summary: statusSummary(a),
  };
}

function levelJson(lv: WarningLevel): Record<string, unknown> {
  return {
    level: lv.level,
    type_label: lv.typeLabel,
    areas: lv.areas.map(areaJson),
  };
}

// ---------------------------------------------------------------------------
// ツール定義
// ---------------------------------------------------------------------------

type ToolHandler = (args: Record<string, unknown>) => Promise<Record<string, unknown>>;

const TOOL_DEFINITIONS: Array<{ name: string; description: string; inputSchema: Record<string, unknown>; handler: ToolHandler }> = [
  {
    name: "get_snow_info",
    description:
      "新潟県内のアメダス観測所の現在の積雪深（cm）を取得する。" +
      "観測所コードを指定しない場合は県内全 44 観測所、指定した場合はその観測所のみを返す。" +
      "積雪データは気象庁が冬季のみ提供しており、夏季（概ね5〜9月）はエラーになる。",
    inputSchema: {
      type: "object",
      properties: {
        station_codes: {
          type: "array",
          items: { type: "string" },
          description: "観測所番号のリスト（例: [\"54841\", \"54232\"]）。省略時は新潟県内の全観測所。",
        },
        limit: { type: "number", description: "返す観測所の最大件数（積雪の多い順）。", default: DEFAULT_LIMIT },
        force: { type: "boolean", description: "True ならキャッシュを無視して再取得。", default: false },
      },
    },
    handler: async (args) => {
      const codes = normalizeCodes(args.station_codes);
      const limit = normalizeLimit(args.limit);
      const force = Boolean(args.force);
      const client = new AmedasClient({ ttl: MCP_TTL });
      try {
        const data = await client.fetch("snow", { codes, force });
        const observations = data.observations
          .filter((o) => o.value !== null)
          .sort((a, b) => (b.value ?? 0) - (a.value ?? 0));
        return {
          element: "snow",
          unit: "cm",
          observations: observations.slice(0, limit).map((o, idx) => ({
            ...observationJson(o),
            rank: idx + 1,
          })),
          fetched_at: formatUtc(data.fetchedAt),
          source: AMEDAS_SOURCE,
          source_url: AMEDAS_SOURCE_URL,
        };
      } finally {
        // クライアントはメモリキャッシュのみで外部リソースを保持しない
      }
    },
  },
  {
    name: "get_weather_info",
    description:
      "新潟県内のアメダス観測所の気温（当日の最高・最低）と 1 時間降水量を取得する。" +
      "観測所コードを指定しない場合は新潟県内の全観測所、指定した場合はその観測所のみを返す。",
    inputSchema: {
      type: "object",
      properties: {
        station_codes: {
          type: "array",
          items: { type: "string" },
          description: "観測所番号のリスト（例: [\"54232\"]）。省略時は新潟県内の全観測所。",
        },
        limit: { type: "number", description: "返す観測所の最大件数。", default: DEFAULT_LIMIT },
        force: { type: "boolean", description: "True ならキャッシュを無視して再取得。", default: false },
      },
    },
    handler: async (args) => {
      const codes = normalizeCodes(args.station_codes);
      const limit = normalizeLimit(args.limit);
      const force = Boolean(args.force);
      const elements = ["max_temp", "min_temp", "precipitation"] as const;
      const client = new AmedasClient({ ttl: MCP_TTL });
      const datas: AmedasData[] = [];
      for (const e of elements) {
        datas.push(await client.fetch(e, { codes, force }));
      }
      const byCode = new Map<string, Map<string, Observation>>();
      for (const data of datas) {
        for (const obs of data.observations) {
          let m = byCode.get(obs.station.code);
          if (!m) {
            m = new Map();
            byCode.set(obs.station.code, m);
          }
          m.set(data.element, obs);
        }
      }
      const records: Record<string, unknown>[] = [];
      for (const [code, obsMap] of byCode) {
        const rec: Record<string, unknown> = { station_code: code };
        const firstObs = obsMap.values().next().value as Observation | undefined;
        if (firstObs) rec.station_name = firstObs.station.name;
        for (const e of elements) {
          const obs = obsMap.get(e);
          rec[e] = obs ? obs.value : null;
        }
        records.push(rec);
      }
      return {
        element: "temperature_precipitation",
        unit: { max_temp: "℃", min_temp: "℃", precipitation: "mm" },
        records: records.slice(0, limit),
        fetched_at: formatUtc(datas[0].fetchedAt),
        source: AMEDAS_SOURCE,
        source_url: AMEDAS_SOURCE_URL,
      };
    },
  },
  {
    name: "get_niigata_stats",
    description:
      "新潟県の統計・オープンデータを取得する。data_type で内容を選択する:\n" +
      "- \"datasets\": オープンデータカタログのデータセット一覧（query / category / data_format で絞り込み可）\n" +
      "- \"population\": 人口時系列データ（市町村別、municipality で市町村名を絞り込み可）\n" +
      "- \"michinoeki\": 道の駅一覧（駅名・路線名・所在地・電話番号）",
    inputSchema: {
      type: "object",
      properties: {
        data_type: { type: "string", enum: ["datasets", "population", "michinoeki"], default: "datasets" },
        query: { type: "string", description: "データセット名・概要のキーワード検索（data_type=\"datasets\" 時）。" },
        category: { type: "string", description: "データセットの分類（内容）で絞り込み（例: \"運輸・観光\"）。" },
        data_format: { type: "string", description: "データセットの形式で絞り込み（例: \"CSV\", \"Excel\"）。" },
        municipality: { type: "string", description: "人口データの市町村名で絞り込み（例: \"新潟市\"）。" },
        limit: { type: "number", description: "返す最大件数。", default: DEFAULT_LIMIT },
        force: { type: "boolean", description: "True ならキャッシュを無視して再取得。", default: false },
      },
    },
    handler: async (args) => {
      const dataType = typeof args.data_type === "string" ? args.data_type : "datasets";
      const limit = normalizeLimit(args.limit);
      const force = Boolean(args.force);
      const client = new OpenDataClient({ ttl: MCP_TTL });
      try {
        if (dataType === "population") {
          const records = await client.getPopulation({
            municipality: typeof args.municipality === "string" ? args.municipality : null,
            force,
          });
          return {
            type: "population",
            records: records.slice(0, limit).map(populationJson),
            source: records.length > 0 ? records[0].source : null,
            source_url: records.length > 0 ? records[0].sourceUrl : null,
            warnings: opendataWarnings(client),
          };
        }
        if (dataType === "michinoeki") {
          const stations = await client.getTourism({ force });
          return {
            type: "michinoeki",
            stations: stations.slice(0, limit).map(michinoekiJson),
            source: stations.length > 0 ? stations[0].source : null,
            source_url: stations.length > 0 ? stations[0].sourceUrl : null,
            warnings: opendataWarnings(client),
          };
        }
        if (dataType !== "datasets") {
          throw new Error(
            `data_type は 'datasets' / 'population' / 'michinoeki' のいずれかを指定してください (got: ${JSON.stringify(dataType)})`,
          );
        }
        const datasets = await client.getDatasets({
          query: typeof args.query === "string" ? args.query : null,
          category: typeof args.category === "string" ? args.category : null,
          dataFormat: typeof args.data_format === "string" ? args.data_format : null,
          force,
        });
        return {
          type: "datasets",
          datasets: datasets.slice(0, limit).map(datasetJson),
          count: datasets.length,
          source: datasets.length > 0 ? datasets[0].source : null,
          source_url: datasets.length > 0 ? datasets[0].sourceUrl : null,
          warnings: opendataWarnings(client),
        };
      } finally {
        // クライアントはメモリキャッシュのみで外部リソースを保持しない
      }
    },
  },
  {
    name: "search_niigata_data",
    description:
      "新潟県の全データをキーワードで横断検索する。" +
      "対象は アメダス観測所（名前・コード）/ 人口（市町村名）/ 道の駅（駅名・所在地）/ " +
      "オープンデータデータセット（名前・分類・概要）の 4 種類。" +
      "例: \"湯沢\", \"新潟市\", \"道の駅\", \"観光\"。",
    inputSchema: {
      type: "object",
      properties: {
        keyword: { type: "string", description: "検索キーワード（観測所名・市町村名・データ名など）。" },
        limit: { type: "number", description: "カテゴリごとの最大表示件数。", default: DEFAULT_LIMIT },
        force: { type: "boolean", description: "True ならキャッシュを無視して再取得。", default: false },
      },
      required: ["keyword"],
    },
    handler: async (args) => {
      const keyword = typeof args.keyword === "string" ? args.keyword : "";
      if (!keyword) throw new Error("keyword を指定してください");
      const limit = normalizeLimit(args.limit);
      const force = Boolean(args.force);
      const results: Record<string, unknown> = {};

      // 1. 観測所（アメダス）: 名前・番号の部分一致
      const stationsHit: Record<string, unknown>[] = [];
      const aclient = new AmedasClient({ ttl: MCP_TTL });
      for (const st of aclient.getStations()) {
        if (st.name.includes(keyword) || st.code.includes(keyword)) {
          stationsHit.push({
            station_code: st.code,
            station_name: st.name,
            lat: st.lat,
            lon: st.lon,
            altitude: st.altitude,
            station_type: st.stationType,
          });
        }
      }
      results.stations = stationsHit.slice(0, limit);

      // 2. オープンデータ（人口・道の駅・データセット）
      const oclient = new OpenDataClient({ ttl: MCP_TTL });
      try {
        const pop = await oclient.getPopulation({ force });
        results.population = pop
          .filter((r) => r.municipalityName.includes(keyword) || r.municipalityCode.includes(keyword))
          .slice(0, limit)
          .map(populationJson);
      } catch (e) {
        results.population_error = (e as Error).message;
      }
      try {
        const michi = await oclient.getTourism({ force });
        results.michinoeki = michi
          .filter((s) => s.name.includes(keyword) || s.address.includes(keyword))
          .slice(0, limit)
          .map(michinoekiJson);
      } catch (e) {
        results.michinoeki_error = (e as Error).message;
      }
      try {
        const ds = await oclient.getDatasets({ force });
        results.datasets = ds
          .filter((d) =>
            d.name.includes(keyword) ||
            d.category.includes(keyword) ||
            d.description.includes(keyword) ||
            d.fields.includes(keyword),
          )
          .slice(0, limit)
          .map(datasetJson);
      } catch (e) {
        results.datasets_error = (e as Error).message;
      }
      const warnings = opendataWarnings(oclient);

      return {
        keyword,
        stations: results.stations ?? [],
        population: results.population ?? [],
        michinoeki: results.michinoeki ?? [],
        datasets: results.datasets ?? [],
        errors: ["population_error", "michinoeki_error", "datasets_error"]
          .filter((k) => k in results)
          .map((k) => String(results[k])),
        warnings,
      };
    },
  },
  {
    name: "get_tourist_spots",
    description:
      "新潟県内の観光スポット一覧を取得する。" +
      "対象は 温泉（新潟市 GIS 温泉利用許可施設、泉質・緯度経度付き）と " +
      "集客施設（国土数値情報 P33 映画館・公会堂・劇場等、2014年度版）の 2 系統。" +
      "category で区分（例: \"温泉\", \"集客施設（映画館）\"）を、" +
      "keyword でスポット名・住所・説明の部分一致検索ができる。" +
      "データ源が取得できない場合は内蔵サンプルデータにフォールバックする。",
    inputSchema: {
      type: "object",
      properties: {
        category: { type: "string", description: "スポット区分で絞り込み（例: \"温泉\", \"集客施設（映画館）\"）。" },
        keyword: { type: "string", description: "スポット名・住所・説明への部分一致検索語。" },
        limit: { type: "number", description: "返すスポットの最大件数。", default: DEFAULT_LIMIT },
        force: { type: "boolean", description: "True ならキャッシュを無視して再取得。", default: false },
      },
    },
    handler: async (args) => {
      const category = typeof args.category === "string" ? args.category : null;
      const keyword = typeof args.keyword === "string" ? args.keyword : null;
      const limit = normalizeLimit(args.limit);
      const force = Boolean(args.force);
      const client = new TourismClient({ ttl: MCP_TTL });
      const spots = await client.getSpots({ category, force });
      const filtered = keyword
        ? spots.filter(
            (s) =>
              s.name.includes(keyword) ||
              s.address.includes(keyword) ||
              s.description.includes(keyword) ||
              s.category.includes(keyword),
          )
        : spots;
      return {
        type: "tourist_spots",
        spots: filtered.slice(0, limit).map(spotJson),
        count: filtered.length,
        source: TOURISM_SOURCE,
        source_url: TOURISM_SOURCE_URL,
        warnings: tourismWarnings(client),
      };
    },
  },
  {
    name: "get_tour_recommendation",
    description:
      "おすすめ観光ルート・観光スポットの推薦情報を取得する。" +
      "新潟県内の観光スポット（温泉・集客施設）から選抜し、" +
      "観光入込客数の多い年・市町村の傾向、および" +
      "対象地域（area 指定時）の当日雨情報（アメダス1時間降水量）を組み合わせて返す。" +
      "area には地域名（例: \"十日町市\"）または観測所名（例: \"湯沢\"）を指定できる。",
    inputSchema: {
      type: "object",
      properties: {
        area: { type: "string", description: "対象地域名（スポット住所 / 観測所名への部分一致）。" },
        limit: { type: "number", description: "返すスポットの最大件数。", default: DEFAULT_LIMIT },
        force: { type: "boolean", description: "True ならキャッシュを無視して再取得。", default: false },
      },
    },
    handler: async (args) => {
      const area = typeof args.area === "string" ? args.area : null;
      const limit = normalizeLimit(args.limit);
      const force = Boolean(args.force);
      const tourism = new TourismClient({ ttl: MCP_TTL });
      const spots = await tourism.getSpots({ force });
      const stats = await tourism.getIrikomi({ force });
      const tourismWarningsList = tourismWarnings(tourism);

      let filtered = spots;
      if (area) {
        const kw = area.trim();
        filtered = spots.filter(
          (s) =>
            kw.includes(s.name) ||
            kw.includes(s.address) ||
            kw.includes(s.description) ||
            kw.includes(s.category),
        );
      }

      // 推定点数: カテゴリ情報量（description の長さ）で安定して並べる
      const scored = [...filtered].sort((a, b) => {
        const lenDiff = (b.description ?? "").length - (a.description ?? "").length;
        if (lenDiff !== 0) return lenDiff;
        return a.name.localeCompare(b.name, "ja");
      });

      // 雨情報: area を観測所名（またはコード）として解決し、アメダス1時間降水量を取得
      const rain: Record<string, unknown> = { precipitation: null, note: null };
      let station: Station | null = null;
      if (area) {
        const aclient = new AmedasClient({ ttl: MCP_TTL });
        station = resolveStationByNameOrCode(aclient, area);
        if (station !== null) {
          try {
            const data = await aclient.fetch("precipitation", {
              codes: [station.code],
              force,
            });
            const obs = data.observations.filter((o) => o.value !== null);
            if (obs.length > 0) {
              rain.station_code = obs[0].station.code;
              rain.station_name = obs[0].station.name;
              rain.precipitation = obs[0].value;
              rain.unit = "mm";
              rain.observed_at = formatUtc(obs[0].observedAt);
              rain.source = AMEDAS_SOURCE;
              rain.source_url = AMEDAS_SOURCE_URL;
            } else {
              rain.note = `${area} に該当する観測所の観測値がありません。雨情報なし（アメダス観測所例: 新潟=54232, 湯沢=54841）`;
            }
          } catch (e) {
            rain.note = `雨情報を取得できませんでした（${(e as Error).message}）`;
          }
        } else {
          rain.note = `雨情報を取得できませんでした（観測所が見つかりません: ${area}）`;
        }
      }

      // 雨が降っている場合、観測所の周辺スポットを上位に持ってくる
      if (
        station !== null &&
        typeof rain.precipitation === "number" &&
        rain.precipitation >= 1.0
      ) {
        scored.sort((a, b) => {
          const da =
            a.lat !== null && a.lon !== null
              ? haversineKm(station.lat, station.lon, a.lat, a.lon)
              : 1e9;
          const db =
            b.lat !== null && b.lon !== null
              ? haversineKm(station.lat, station.lon, b.lat, b.lon)
              : 1e9;
          return da - db;
        });
      }

      const statsSorted = [...stats].sort((a, b) => b.year - a.year);
      const latest = statsSorted.length > 0 ? statsSorted[0] : null;
      const busiest = stats.reduce(
        (max, s) => ((s.total ?? 0) > (max?.total ?? 0) ? s : max),
        null as TourismStat | null,
      );

      return {
        type: "tour_recommendation",
        area: area ?? null,
        spots: scored.slice(0, limit).map(spotJson),
        stats: {
          latest_year: latest ? latest.year : null,
          latest_total: latest ? latest.total : null,
          busiest_year: busiest ? busiest.year : null,
          busiest_total: busiest ? busiest.total : null,
          records: statsSorted.slice(0, 5).map(tourStatJson),
          source: latest ? latest.source : TOURISM_SOURCE,
          source_url: latest ? latest.sourceUrl : TOURISM_SOURCE_URL,
        },
        rain,
        source: TOURISM_SOURCE,
        source_url: TOURISM_SOURCE_URL,
        warnings: tourismWarningsList,
      };
    },
  },
  {
    name: "get_warning_info",
    description:
      "新潟県（府県コード 150000）の現在の警報・注意報を取得する。" +
      "気象庁防災情報XML（VPWW53/VPWW54 電文）から、" +
      "府県 → 一次細分区域 → 市町村等をまとめた地域 → 市町村 の 4 階層それぞれの" +
      "警報・注意報の種別（大雨・雷・波浪など）と状態（発表/継続/解除）を返す。" +
      "level で階層を絞り込み（\"府県\" / \"一次細分\" / \"地域\" / \"市町村\"）、" +
      "active_only=True で発表のある地域のみに絞り込める。",
    inputSchema: {
      type: "object",
      properties: {
        level: { type: "string", enum: ["府県", "一次細分", "地域", "市町村"], default: "府県" },
        active_only: { type: "boolean", description: "True なら警報・注意報が発表されている地域のみ返す。", default: false },
        force: { type: "boolean", description: "True ならキャッシュを無視して再取得。", default: false },
      },
    },
    handler: async (args) => {
      const level = typeof args.level === "string" ? args.level : "府県";
      const activeOnly = Boolean(args.active_only);
      const force = Boolean(args.force);
      const validLevels = ["府県", "一次細分", "地域", "市町村"];
      if (!validLevels.includes(level)) {
        throw new Error(
          `level は ${validLevels.join("/")} のいずれかを指定してください (got: ${JSON.stringify(level)})`,
        );
      }
      const client = new WarningClient({ ttl: MCP_WARNING_TTL });
      const data = await client.fetch({ force });
      const target = data.levels.find((l) => l.level === level);
      const areas = target
        ? activeOnly
          ? getActiveAreas(data, level)
          : target.areas
        : [];
      return {
        prefecture_code: NIIGATA_PREF_CODE,
        level,
        active_only: activeOnly,
        title: data.title,
        headline: data.headline,
        info_type: data.infoType,
        report_datetime: formatUtc(data.reportDatetime),
        editorial_office: data.editorialOffice,
        message_kind: data.messageKind,
        message_url: data.messageUrl,
        summary: warningSummary(data),
        levels: data.levels.map(levelJson),
        areas: areas.map(areaJson),
        source: WARNING_SOURCE,
        source_url: WARNING_SOURCE_URL,
      };
    },
  },
];

function resolveStationByNameOrCode(client: AmedasClient, area: string): Station | null {
  const kw = area.trim();
  for (const st of client.getStations()) {
    if (st.code === kw || st.name.includes(kw)) return st;
  }
  return null;
}

function normalizeCodes(value: unknown): string[] | null {
  if (Array.isArray(value)) {
    const codes = value.filter((v): v is string => typeof v === "string" && v.length > 0);
    return codes.length > 0 ? codes : null;
  }
  return null;
}

function normalizeLimit(value: unknown): number {
  const n = typeof value === "number" ? value : DEFAULT_LIMIT;
  if (!Number.isFinite(n)) return DEFAULT_LIMIT;
  return Math.max(1, Math.min(200, Math.trunc(n)));
}

// ---------------------------------------------------------------------------
// MCP サーバー起動
// ---------------------------------------------------------------------------

function toolToDefinition(t: (typeof TOOL_DEFINITIONS)[number]): Tool {
  return {
    name: t.name,
    description: t.description,
    inputSchema: t.inputSchema as Tool["inputSchema"],
  };
}

export async function main(): Promise<void> {
  const server = new Server(
    {
      name: "nic", // MCP サーバー名（NIC システム名）
      version: VERSION,
    },
    {
      capabilities: {
        tools: {},
      },
      instructions:
        "新潟県の情報（気象・統計・観光・防災・交通・オープンデータ）を提供する MCP サーバー。" +
        "アメダス観測所のコードは 5 桁数字（例: 54232=新潟, 54841=湯沢）。" +
        "ツールのレスポンスには必ず出典（source / source_url）が含まれるため、回答の際は出典を明記すること。",
    },
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: TOOL_DEFINITIONS.map(toolToDefinition),
  }));

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const name = request.params.name;
    const args = (request.params.arguments ?? {}) as Record<string, unknown>;
    const tool = TOOL_DEFINITIONS.find((t) => t.name === name);
    if (!tool) {
      return {
        content: [{ type: "text", text: `不明なツール: ${name}` }],
        isError: true,
      };
    }
    try {
      const result = await tool.handler(args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      return {
        content: [{ type: "text", text: `エラー: ${message}` }],
        isError: true,
      };
    }
  });

  const transport = new StdioServerTransport();
  await server.connect(transport);
}

// bin（dist/mcp/index.js）から直接実行された場合のエントリポイント
const isDirectRun = ((): boolean => {
  const arg1 = process.argv[1];
  if (!arg1) return true; // argv[1] が無い環境（シェル経由の実行）は直接実行とみなす
  // 実行されたスクリプトの basename が自分自身（index.js）なら直接実行
  const selfBase = import.meta.url.split("/").pop() ?? "";
  const argBase = arg1.replace(/\\/g, "/").split("/").pop() ?? "";
  return selfBase === argBase;
})();
if (isDirectRun) {
  main().catch((e) => {
    process.stderr.write(`MCP サーバー起動エラー: ${(e as Error).message}\n`);
    process.exit(1);
  });
}
