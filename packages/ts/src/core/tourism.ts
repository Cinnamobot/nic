/**
 * 新潟県観光スポット取得モジュール。
 *
 * 新潟県の観光スポット（温泉・文化施設・集客施設）を、機械判読可能な
 * 公式データ源から取得する。スポット・イベントの機械判読可能データは
 * 公式には存在しない（調査レポート参照）ため、以下の代替データ源を
 * 組み合わせてスポット一覧を構築する。
 *
 * データ源（取得優先順）:
 *   1. 新潟市 CKAN API（https://opendata.city.niigata.lg.jp/api/3/action/package_search）
 *   2. 新潟市観光入込客数 CSV（UTF-8 BOM 付き）
 *   3. 新潟市 GIS 温泉利用許可施設 CSV（緯度経度・泉質付き、24 件）
 *   4. 国土数値情報 P33 集客施設データ（Shapefile / DBF、Shift-JIS、2014年度版）
 *
 * 出典: 新潟市オープンデータ（CC-BY）/ 国土数値情報（国土交通省）
 * 全レスポンスに source / source_url を含めて出典を明記する。
 *
 * 利用条件:
 *   - 新潟市: クリエイティブ・コモンズ 表示（CC-BY）
 *     https://creativecommons.org/licenses/by/4.0/deed.ja
 *   - 国土数値情報: 国土数値情報利用約款（出典明記で無償利用・加工・再配布可）
 *     https://nlftp.mlit.go.jp/ksj/other/agreement.html
 */

import iconv from "iconv-lite";
import { inflateSync, inflateRawSync } from "node:zlib";
import { decodeText, parseCsvRows, toFloat, toInt } from "./util.js";

export const SOURCE_TEXT = "出典:新潟市オープンデータ";
export const SOURCE_URL = "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/";

export const CKAN_BASE_URL = "http://opendata.city.niigata.lg.jp/api/3/action/package_search";
export const P33_SOURCE_TEXT = "出典:国土数値情報（国土交通省）";
export const P33_SOURCE_URL = "https://nlftp.mlit.go.jp/ksj/gml/gisdata.html";
export const P33_ZIP_URL = "https://nlftp.mlit.go.jp/ksj/gml/data/P33/P33-14/P33-14_15_GML.zip";

/** GIS 温泉利用許可施設 CSV の実体 URL（新潟市オープンデータ） */
export const ONSEN_CSV_URL =
  "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-gis/od-gis_kankobunspo/od-gis_onseninst.files/od_gis_10096_onseninstitution.csv";

/** 観光入込客数 CSV の実体 URL（新潟市オープンデータ、年次更新） */
export const IRIKOMI_CSV_URL =
  "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-kankou/od-irikomidata.files/irikomidataR6.csv";

export const DEFAULT_TTL = 3600.0;
export const USER_AGENT = "nic/0.1 (+https://github.com/Cinnamobot/nic)";

/** 国土数値情報 P33 の施設区分コード → 日本語名 */
export const P33_FACILITY_TYPES: Record<string, string> = {
  "1": "映画館",
  "2": "公会堂",
  "3": "劇場",
  "4": "展示場",
  "5": "体育館・観覧場",
  "6": "その他",
};

export class TourismError extends Error {}
export class TourismFetchError extends TourismError {}
export class TourismParseError extends TourismError {}
export class TourismNotFoundError extends TourismError {}

/** 新潟市 CKAN カタログ上の観光関連データセット 1 件。 */
export interface TourismDataset {
  id: string;
  name: string;
  title: string;
  description: string;
  license: string;
  licenseUrl: string;
  updatedAt: string;
  url: string;
  resources: string[];
  source: string;
  sourceUrl: string;
}

/** 観光入込客数の 1 年分レコード。 */
export interface TourismStat {
  year: number;
  eraYear: string;
  total: number | null;
  eventTotal: number | null;
  spotTotal: number | null;
  nature: number | null;
  historyCulture: number | null;
  onsenHealth: number | null;
  sportsRecreation: number | null;
  urbanTourism: number | null;
  other: number | null;
  source: string;
  sourceUrl: string;
}

/** 観光スポット 1 件（温泉・文化施設・集客施設の共通レコード）。 */
export interface Spot {
  id: string;
  name: string;
  category: string;
  lat: number | null;
  lon: number | null;
  address: string;
  phone: string;
  url: string;
  description: string;
  source: string;
  sourceUrl: string;
}

// ---------------------------------------------------------------------------
// 内蔵サンプルデータ（オフライン用フォールバック）
// ---------------------------------------------------------------------------

const SAMPLE_DATASETS: TourismDataset[] = [
  {
    id: "16a13911-06c9-4339-aec6-30c092846c83",
    name: "opendata-kankou_od-irikomidata",
    title: "新潟市観光入込客数",
    description: "年別・分類別の観光入込客数",
    license: "クリエイティブ・コモンズ 表示",
    licenseUrl: "https://creativecommons.org/licenses/by/4.0/deed.ja",
    updatedAt: "2026-03-04T06:03:28.768642",
    url: "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-kankou/od-irikomidata.html",
    resources: [IRIKOMI_CSV_URL],
    source: SOURCE_TEXT,
    sourceUrl: CKAN_BASE_URL,
  },
  {
    id: "83958165-3b29-426d-abc3-c3bb519d893a",
    name: "opendata-kankou_od-citywifi",
    title: "Niigata City Free Wi-Fi利用可能施設一覧",
    description: "観光客向け Free Wi-Fi 利用可能施設の一覧",
    license: "クリエイティブ・コモンズ 表示",
    licenseUrl: "https://creativecommons.org/licenses/by/4.0/deed.ja",
    updatedAt: "2026-07-06T04:02:56.219033",
    url: "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-kankou/od-citywifi.html",
    resources: [
      "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-kankou/od-citywifi.files/od-city_wifi_ichiran_20240401.csv",
    ],
    source: SOURCE_TEXT,
    sourceUrl: CKAN_BASE_URL,
  },
  {
    id: "sample-onsen",
    name: "od-gis_kankobunspo_od-gis_onseninst",
    title: "GIS 温泉利用を許可した施設",
    description: "温泉利用許可施設（緯度経度・泉質付き）",
    license: "クリエイティブ・コモンズ 表示",
    licenseUrl: "https://creativecommons.org/licenses/by/4.0/deed.ja",
    updatedAt: "2023-03-29T00:00:00",
    url: "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-gis/od-gis_kankobunspo/od-gis_onseninst.html",
    resources: [ONSEN_CSV_URL],
    source: SOURCE_TEXT,
    sourceUrl: CKAN_BASE_URL,
  },
];

const SAMPLE_IRIKOMI: TourismStat[] = [
  { year: 2023, eraYear: "令和5", total: 15557, eventTotal: 4382, spotTotal: 11175, nature: 419, historyCulture: 3100, onsenHealth: 818, sportsRecreation: 1792, urbanTourism: 5046, other: 0, source: SOURCE_TEXT, sourceUrl: IRIKOMI_CSV_URL },
  { year: 2024, eraYear: "令和6", total: 16019, eventTotal: 4591, spotTotal: 11428, nature: 425, historyCulture: 3044, onsenHealth: 861, sportsRecreation: 2026, urbanTourism: 5072, other: 0, source: SOURCE_TEXT, sourceUrl: IRIKOMI_CSV_URL },
];

const SAMPLE_ONSEN_SPOTS: Spot[] = [
  { id: "onsen-28", name: "ほてる大橋館の湯", category: "温泉", lat: 37.7380947, lon: 138.8398538, address: "新潟市西蒲区岩室温泉340-甲", phone: "0256-82-4125", url: "", description: "岩室温泉（含硫黄－ナトリウム･カルシウム－塩化物泉）", source: SOURCE_TEXT, sourceUrl: ONSEN_CSV_URL },
  { id: "onsen-39", name: "多宝温泉　だいろの湯", category: "温泉", lat: 37.7280278, lon: 138.837374, address: "新潟市西蒲区石瀬3250", phone: "0256-82-1126", url: "", description: "多宝温泉だいろの湯（含硫黄－ナトリウム・カルシウム－塩化物泉、他）", source: SOURCE_TEXT, sourceUrl: ONSEN_CSV_URL },
];

const SAMPLE_P33_SPOTS: Spot[] = [
  { id: "p33-1", name: "シネ・ウインド", category: "集客施設（映画館）", lat: 37.915809210064, lon: 139.05391640076, address: "新潟市中央区八千代2-1-1（1F）", phone: "025-243-5530", url: "http://cinewind.com/", description: "映画館", source: P33_SOURCE_TEXT, sourceUrl: P33_ZIP_URL },
  { id: "p33-10", name: "川前公民館", category: "集客施設（公会堂）", lat: null, lon: null, address: "燕市中川597-1", phone: "0256-63-9310", url: "", description: "公会堂", source: P33_SOURCE_TEXT, sourceUrl: P33_ZIP_URL },
];

interface CacheEntry {
  data: unknown;
  expiresAt: number;
}

async function httpGet(
  url: string,
  timeoutMs: number,
  headers: Record<string, string>,
): Promise<Uint8Array> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(url, {
      headers: { "User-Agent": USER_AGENT, ...headers },
      signal: controller.signal,
      redirect: "follow",
    });
    if (resp.status !== 200) {
      throw new TourismFetchError(`HTTP ${resp.status} で取得できませんでした: ${url}`);
    }
    return new Uint8Array(await resp.arrayBuffer());
  } catch (e) {
    if (e instanceof TourismError) throw e;
    throw new TourismFetchError(`HTTP 取得に失敗しました: ${url} (${(e as Error).message})`);
  } finally {
    clearTimeout(timer);
  }
}

/** 新潟県観光データ取得クライアント。 */
export class TourismClient {
  ttl: number;
  timeout: number;
  fallbackToSample: boolean;
  private cache = new Map<string, CacheEntry>();
  private warningsList: string[] = [];

  constructor(options: { ttl?: number; timeout?: number; fallbackToSample?: boolean } = {}) {
    this.ttl = options.ttl ?? DEFAULT_TTL;
    this.timeout = options.timeout ?? 30.0;
    this.fallbackToSample = options.fallbackToSample ?? true;
  }

  /** 直近の取得で発生したフォールバック状況の説明一覧。 */
  get warnings(): string[] {
    return [...this.warningsList];
  }

  /** 新潟市 CKAN から観光関連データセット一覧を取得する。 */
  async getDatasets(options: { query?: string | null; force?: boolean } = {}): Promise<TourismDataset[]> {
    const force = options.force ?? false;
    let datasets = this.getCached("datasets", force) as TourismDataset[] | null;
    if (datasets === null) {
      datasets = await this.fetchDatasets();
      this.putCache("datasets", datasets);
    }
    if (options.query) {
      const q = options.query.trim();
      datasets = datasets.filter(
        (d) => d.title.includes(q) || d.name.includes(q) || d.description.includes(q),
      );
    }
    return datasets;
  }

  /** 観光入込客数（新潟市、年別・分類別）を取得する。 */
  async getIrikomi(options: { year?: number | null; force?: boolean } = {}): Promise<TourismStat[]> {
    const force = options.force ?? false;
    let stats = this.getCached("irikomi", force) as TourismStat[] | null;
    if (stats === null) {
      stats = await this.fetchIrikomi();
      this.putCache("irikomi", stats);
    }
    if (options.year !== undefined && options.year !== null) {
      stats = stats.filter((s) => s.year === options.year);
    }
    return stats;
  }

  /** 温泉スポット（新潟市 GIS 温泉利用許可施設）を取得する。 */
  async getOnsenSpots(options: { force?: boolean } = {}): Promise<Spot[]> {
    const force = options.force ?? false;
    let spots = this.getCached("onsen", force) as Spot[] | null;
    if (spots === null) {
      spots = await this.fetchOnsenSpots();
      this.putCache("onsen", spots);
    }
    return spots;
  }

  /** 国土数値情報 P33 集客施設をスポットとして取得する。 */
  async getP33Spots(options: { force?: boolean } = {}): Promise<Spot[]> {
    const force = options.force ?? false;
    let spots = this.getCached("p33", force) as Spot[] | null;
    if (spots === null) {
      spots = await this.fetchP33Spots();
      this.putCache("p33", spots);
    }
    return spots;
  }

  /** 観光スポット一覧を取得する（温泉 + 集客施設の統合）。 */
  async getSpots(options: {
    category?: string | null;
    includeOnsen?: boolean;
    includeP33?: boolean;
    force?: boolean;
  } = {}): Promise<Spot[]> {
    const force = options.force ?? false;
    const includeOnsen = options.includeOnsen ?? true;
    const includeP33 = options.includeP33 ?? true;
    const spots: Spot[] = [];
    if (includeOnsen) {
      try {
        spots.push(...(await this.getOnsenSpots({ force })));
      } catch (e) {
        this.warn(`温泉データを取得できませんでした（${(e as Error).message}）。`);
      }
    }
    if (includeP33) {
      try {
        spots.push(...(await this.getP33Spots({ force })));
      } catch (e) {
        this.warn(`集客施設データを取得できませんでした（${(e as Error).message}）。`);
      }
    }
    if (spots.length === 0 && this.fallbackToSample) {
      spots.push(...SAMPLE_ONSEN_SPOTS.map((s) => ({ ...s })));
      spots.push(...SAMPLE_P33_SPOTS.map((s) => ({ ...s })));
      this.warn("外部データ源を利用できなかったため、内蔵サンプルデータを返します。");
    }
    if (spots.length === 0 && !this.fallbackToSample) {
      throw new TourismFetchError(
        "観光スポットを取得できませんでした（温泉・集客施設データ源がすべて失敗）",
      );
    }
    if (options.category) {
      const c = options.category.trim();
      return spots.filter((s) => s.category === c);
    }
    return spots;
  }

  private getCached(key: string, force: boolean): unknown {
    if (force) return null;
    const entry = this.cache.get(key);
    if (entry && entry.expiresAt > Date.now()) return entry.data;
    return null;
  }

  private putCache(key: string, data: unknown): void {
    this.cache.set(key, { data, expiresAt: Date.now() + this.ttl * 1000 });
  }

  private warn(message: string): void {
    this.warningsList.push(message);
  }

  private async download(url: string, options: { binary?: boolean } = {}): Promise<Uint8Array> {
    const headers: Record<string, string> = {};
    if (!options.binary) {
      headers["Accept"] = "text/csv,application/json,application/octet-stream,*/*";
    }
    return httpGet(url, this.timeout * 1000, headers);
  }

  // -- データ源 1: 新潟市 CKAN API ------------------------------------------

  private async fetchDatasets(): Promise<TourismDataset[]> {
    const datasets = await this.fetchFromCkan();
    if (datasets && datasets.length > 0) return datasets;
    if (this.fallbackToSample) {
      this.warn("新潟市 CKAN API を利用できなかったため、内蔵サンプル（3 件）を返します。");
      return SAMPLE_DATASETS.map((d) => ({ ...d }));
    }
    throw new TourismFetchError("新潟市 CKAN API から観光データセットを取得できませんでした");
  }

  private async fetchFromCkan(): Promise<TourismDataset[] | null> {
    const packages: Record<string, unknown>[] = [];
    // 1) タグ検索（q=観光）
    try {
      const q = encodeURIComponent("観光");
      const raw = await this.download(`${CKAN_BASE_URL}?q=${q}&rows=1000`);
      const payload = JSON.parse(new TextDecoder("utf-8").decode(raw)) as Record<string, unknown>;
      if (payload.success === true) {
        const result = payload.result as Record<string, unknown> | null;
        if (result && Array.isArray(result.results)) {
          packages.push(...(result.results as Record<string, unknown>[]));
        }
      }
    } catch (e) {
      this.warn(`CKAN 検索に失敗しました（${(e as Error).message}）。`);
      return null;
    }
    // 2) カタログ全体から観光関連パッケージを収集
    if (packages.length < 50) {
      try {
        const raw = await this.download(`${CKAN_BASE_URL}?rows=1000`);
        const payload = JSON.parse(new TextDecoder("utf-8").decode(raw)) as Record<string, unknown>;
        if (payload.success === true) {
          const result = payload.result as Record<string, unknown> | null;
          if (result && Array.isArray(result.results)) {
            for (const p of result.results as Record<string, unknown>[]) {
              if (isTourismPackage(p)) packages.push(p);
            }
          }
        }
      } catch (e) {
        this.warn(`CKAN カタログ全体の取得に失敗しました（${(e as Error).message}）。`);
      }
    }
    // 重複（同名パッケージ）を除去
    const seen = new Set<string>();
    const unique: Record<string, unknown>[] = [];
    for (const p of packages) {
      const pid = String(p.id ?? "");
      if (pid && !seen.has(pid)) {
        seen.add(pid);
        unique.push(p);
      }
    }
    if (unique.length === 0) {
      this.warn("CKAN API に観光関連データセットが見つかりませんでした。");
      return null;
    }
    return unique.map((p) => parseCkanPackage(p));
  }

  // -- データ源 2: 観光入込客数 CSV -----------------------------------------

  private async fetchIrikomi(): Promise<TourismStat[]> {
    try {
      const raw = await this.download(IRIKOMI_CSV_URL);
      const stats = parseIrikomiCsv(raw, IRIKOMI_CSV_URL);
      if (stats.length > 0) {
        this.warn(`観光入込客数を取得しました: ${IRIKOMI_CSV_URL}（${stats.length} 年分）`);
        return stats;
      }
    } catch (e) {
      this.warn(`観光入込客数 CSV の取得に失敗しました（${(e as Error).message}）。`);
    }
    if (this.fallbackToSample) {
      this.warn("観光入込客数を外部取得できなかったため、内蔵サンプルを返します。");
      return SAMPLE_IRIKOMI.map((s) => ({ ...s }));
    }
    throw new TourismFetchError("観光入込客数を取得できませんでした");
  }

  // -- データ源 3: GIS 温泉利用許可施設 CSV ---------------------------------

  private async fetchOnsenSpots(): Promise<Spot[]> {
    try {
      const raw = await this.download(ONSEN_CSV_URL);
      const spots = parseOnsenCsv(raw, ONSEN_CSV_URL);
      if (spots.length > 0) {
        this.warn(`温泉施設を取得しました: ${ONSEN_CSV_URL}（${spots.length} 件）`);
        return spots;
      }
    } catch (e) {
      this.warn(`温泉施設 CSV の取得に失敗しました（${(e as Error).message}）。`);
    }
    if (this.fallbackToSample) {
      this.warn("温泉施設を外部取得できなかったため、内蔵サンプルを返します。");
      return SAMPLE_ONSEN_SPOTS.map((s) => ({ ...s }));
    }
    throw new TourismFetchError("温泉施設を取得できませんでした");
  }

  // -- データ源 4: 国土数値情報 P33 集客施設 ---------------------------------

  private async fetchP33Spots(): Promise<Spot[]> {
    try {
      const raw = await this.download(P33_ZIP_URL, { binary: true });
      const spots = parseP33Zip(raw, P33_ZIP_URL);
      if (spots.length > 0) {
        this.warn(`国土数値情報 P33 集客施設を取得しました: ${P33_ZIP_URL}（${spots.length} 件）`);
        return spots;
      }
    } catch (e) {
      this.warn(`国土数値情報 P33 の取得に失敗しました（${(e as Error).message}）。`);
    }
    if (this.fallbackToSample) {
      this.warn("国土数値情報 P33 を外部取得できなかったため、内蔵サンプルを返します。");
      return SAMPLE_P33_SPOTS.map((s) => ({ ...s }));
    }
    throw new TourismFetchError("国土数値情報 P33 集客施設を取得できませんでした");
  }
}

// ---------------------------------------------------------------------------
// CKAN レスポンスのパース
// ---------------------------------------------------------------------------

const TOURISM_PACKAGE_PREFIXES = ["opendata-kankou_", "od-gis_kankobunspo_"];
const TOURISM_KEYWORDS = ["観光", "温泉", "入込", "集客", "海水浴", "美術館", "博物館", "水族館", "遺跡"];

function isTourismPackage(pkg: Record<string, unknown>): boolean {
  const name = String(pkg.name ?? "");
  if (TOURISM_PACKAGE_PREFIXES.some((p) => name.startsWith(p))) return true;
  const title = String(pkg.title ?? "");
  if (TOURISM_KEYWORDS.some((k) => title.includes(k))) return true;
  const tags = Array.isArray(pkg.tags)
    ? pkg.tags.map((t) => String((t as Record<string, unknown>).name ?? ""))
    : [];
  return tags.some((t) => t.includes("観光") || t.includes("温泉"));
}

function parseCkanPackage(pkg: Record<string, unknown>): TourismDataset {
  const resources = Array.isArray(pkg.resources)
    ? pkg.resources
        .filter((r): r is Record<string, unknown> => typeof r === "object" && r !== null && Boolean((r as Record<string, unknown>).url))
        .map((r) => String(r.url))
    : [];
  return {
    id: String(pkg.id ?? ""),
    name: String(pkg.name ?? ""),
    title: String(pkg.title ?? ""),
    description: String(pkg.notes ?? ""),
    license: String(pkg.license_title ?? ""),
    licenseUrl: String(pkg.license_url ?? ""),
    updatedAt: String(pkg.metadata_modified ?? ""),
    url: String(pkg.url ?? ""),
    resources,
    source: SOURCE_TEXT,
    sourceUrl: CKAN_BASE_URL,
  };
}

// ---------------------------------------------------------------------------
// 観光入込客数 CSV のパース
// ---------------------------------------------------------------------------

/** 新潟市観光入込客数 CSV（UTF-8 BOM 付き・11 カラム）をパースする。 */
export function parseIrikomiCsv(raw: Uint8Array, sourceUrl: string): TourismStat[] {
  const text = decodeText(raw);
  const rows = parseCsvRows(text);
  if (rows.length === 0) throw new TourismParseError("観光入込客数 CSV が空です");
  const header = rows[0].map((h) => h.trim());

  // ヘッダの列名からインデックスを解決する（列順の変化に強い）
  const colIndex: Record<string, number> = {};
  for (let idx = 0; idx < header.length; idx++) {
    const name = header[idx];
    const normalized = name
      .replace(/\[千人\]/g, "")
      .replace(/ /g, "")
      .replace(/　/g, "")
      .replace("年[西暦]", "年西暦")
      .replace("年[和暦]", "年和暦");
    let key: string | null = null;
    if (normalized.startsWith("観光入込客数合計")) key = "total";
    else if (normalized.startsWith("行祭事")) key = "event_total";
    else if (normalized.startsWith("観光地点合計の自然")) key = "nature";
    else if (normalized.startsWith("観光地点合計の歴史")) key = "history_culture";
    else if (normalized.startsWith("観光地点合計の温泉")) key = "onsen_health";
    else if (normalized.startsWith("観光地点合計のスポーツ")) key = "sports_recreation";
    else if (normalized.startsWith("観光地点合計の都市型")) key = "urban_tourism";
    else if (normalized.startsWith("観光地点合計のその他")) key = "other";
    else if (normalized === "観光地点合計" || normalized.startsWith("観光地点合計[")) key = "spot_total";
    else if (normalized.startsWith("年西暦") || normalized === "年西暦") key = "year";
    else if (normalized.startsWith("年和暦") || normalized === "年和暦") key = "era_year";
    if (key && !(key in colIndex)) colIndex[key] = idx;
  }

  if (!("year" in colIndex) || !("total" in colIndex)) {
    throw new TourismParseError(`観光入込客数 CSV のヘッダが想定と異なります: ${header.slice(0, 12).join(",")}`);
  }

  const stats: TourismStat[] = [];
  const maxIdx = Math.max(...Object.values(colIndex));
  for (const row of rows.slice(1)) {
    if (row.length <= maxIdx) continue;
    const year = toInt(row[colIndex.year]);
    if (year === null) continue;
    const get = (key: string): number | null =>
      key in colIndex && row.length > colIndex[key] ? toInt(row[colIndex[key]]) : null;
    stats.push({
      year,
      eraYear: "era_year" in colIndex ? row[colIndex.era_year].trim() : "",
      total: get("total"),
      eventTotal: get("event_total"),
      spotTotal: get("spot_total"),
      nature: get("nature"),
      historyCulture: get("history_culture"),
      onsenHealth: get("onsen_health"),
      sportsRecreation: get("sports_recreation"),
      urbanTourism: get("urban_tourism"),
      other: get("other"),
      source: SOURCE_TEXT,
      sourceUrl,
    });
  }
  if (stats.length === 0) {
    throw new TourismParseError("観光入込客数 CSV に有効なデータ行がありません");
  }
  return stats;
}

// ---------------------------------------------------------------------------
// GIS 温泉利用許可施設 CSV のパース
// ---------------------------------------------------------------------------

/** 新潟市 GIS 温泉利用許可施設 CSV をパースする。 */
export function parseOnsenCsv(raw: Uint8Array, sourceUrl: string): Spot[] {
  const text = decodeText(raw);
  const rows = parseCsvRows(text);
  if (rows.length === 0) throw new TourismParseError("温泉 CSV が空です");
  const header = rows[0].map((h) => h.trim());

  const find = (...names: string[]): number | null => {
    for (const name of names) {
      const idx = header.indexOf(name);
      if (idx !== -1) return idx;
    }
    return null;
  };
  const idxLon = find("longitude", "経度", "X");
  const idxLat = find("latitude", "緯度", "Y");
  const idxName = find("SAFIELD000", "名称", "施設名", "名前");
  const idxAddr = find("SAFIELD002", "住所", "所在地", "SAFIELD001");
  const idxPhone = find("SAFIELD003", "電話番号", "電話", "SAFIELD002");
  const idxOnsen = find("SAFIELD004", "温泉名", "源泉名");
  const idxQuality = find("SAFIELD005", "泉質");
  if (idxName === null || idxLon === null || idxLat === null) {
    throw new TourismParseError(`温泉 CSV のヘッダが想定と異なります: ${header.slice(0, 12).join(",")}`);
  }

  const spots: Spot[] = [];
  const maxIdx = Math.max(idxName, idxLon, idxLat);
  for (let r = 1; r < rows.length; r++) {
    const row = rows[r];
    if (row.length <= maxIdx) continue;
    const name = row[idxName].trim();
    if (!name) continue;
    const lat = toFloat(row[idxLat]);
    const lon = toFloat(row[idxLon]);
    if (lat === null || lon === null) continue;
    const onsenName = idxOnsen !== null && row.length > idxOnsen ? row[idxOnsen].trim() : "";
    const quality = idxQuality !== null && row.length > idxQuality ? row[idxQuality].trim() : "";
    const parts = [onsenName, quality].filter((p) => p !== "");
    const description = parts.length > 0 ? `（${parts.join("、")}）` : "";
    spots.push({
      id: row.length > 2 && row[2].trim() ? `onsen-${row[2].trim()}` : `onsen-${r}`,
      name,
      category: "温泉",
      lat,
      lon,
      address: idxAddr !== null && row.length > idxAddr ? row[idxAddr].trim() : "",
      phone: idxPhone !== null && row.length > idxPhone ? row[idxPhone].trim() : "",
      url: "",
      description,
      source: SOURCE_TEXT,
      sourceUrl,
    });
  }
  if (spots.length === 0) {
    throw new TourismParseError("温泉 CSV に有効なデータ行がありません");
  }
  return spots;
}

// ---------------------------------------------------------------------------
// 国土数値情報 P33（Shapefile / DBF）のパース
// ---------------------------------------------------------------------------

interface DbfField {
  name: string;
  type: string; // C=文字, N=数値, L=論理, D=日付
  length: number;
  decimal: number;
}

function parseDbfHeader(raw: Uint8Array): { fields: DbfField[]; numRecords: number; headerSize: number; recordSize: number } {
  if (raw.length < 32) throw new TourismParseError("DBF ヘッダが短すぎます");
  const dv = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
  const numRecords = dv.getUint32(4, true);
  const headerSize = dv.getUint16(8, true);
  const recordSize = dv.getUint16(10, true);
  if (headerSize < 32) throw new TourismParseError(`DBF ヘッダサイズが不正です: ${headerSize}`);
  const nFields = Math.floor((headerSize - 32) / 32);
  const fields: DbfField[] = [];
  for (let i = 0; i < nFields; i++) {
    const off = 32 + i * 32;
    if (off + 32 > raw.length) break;
    let nameLen = 0;
    while (nameLen < 11 && raw[off + nameLen] !== 0) nameLen++;
    const nameBytes = raw.slice(off, off + nameLen);
    let name: string;
    try {
      name = iconv.decode(Buffer.from(nameBytes), "cp932");
    } catch {
      name = new TextDecoder("utf-8").decode(nameBytes);
    }
    const ftype = String.fromCharCode(raw[off + 11]);
    const flen = raw[off + 16];
    const fdec = raw[off + 17];
    fields.push({ name, type: ftype, length: flen, decimal: fdec });
  }
  return { fields, numRecords, headerSize, recordSize };
}

function parseDbfRecord(raw: Uint8Array, fields: DbfField[]): string[] {
  const values: string[] = [];
  let pos = 0;
  for (const f of fields) {
    const chunk = raw.slice(pos, pos + f.length);
    if (f.type === "C") {
      try {
        values.push(iconv.decode(Buffer.from(chunk), "cp932").trim());
      } catch {
        values.push(new TextDecoder("utf-8").decode(chunk).trim());
      }
    } else {
      values.push(new TextDecoder("ascii").decode(chunk).trim());
    }
    pos += f.length;
  }
  return values;
}

/** SHP ファイルから各レコードの座標（Point / PointZ）を読み取る。 */
function readShpPoints(shpBytes: Uint8Array): Array<[number, number] | null> {
  const points: Array<[number, number] | null> = [];
  const dv = new DataView(shpBytes.buffer, shpBytes.byteOffset, shpBytes.byteLength);
  let pos = 100; // 100 バイトのファイルヘッダをスキップ
  while (pos + 8 <= shpBytes.length) {
    const contentWords = dv.getInt32(pos + 4, false);
    pos += 8;
    const contentLen = contentWords * 2;
    if (pos + contentLen > shpBytes.length) break;
    if (contentLen < 4) {
      points.push(null);
      pos += contentLen;
      continue;
    }
    const shapeType = dv.getInt32(pos, true);
    if (shapeType === 1 && contentLen >= 20) {
      // Point
      const x = dv.getFloat64(pos + 4, true);
      const y = dv.getFloat64(pos + 12, true);
      points.push([x, y]);
    } else if (shapeType === 11 && contentLen >= 28) {
      // PointZ
      const x = dv.getFloat64(pos + 4, true);
      const y = dv.getFloat64(pos + 12, true);
      points.push([x, y]);
    } else {
      points.push(null);
    }
    pos += contentLen;
  }
  return points;
}

/** P33 集客施設の DBF フィールド名 → 意味 */
const P33_FIELD_NAMES: Record<string, string> = {
  P33_001: "facility_id",
  P33_002: "city_code",
  P33_003: "pref_code",
  P33_004: "facility_type_code",
  P33_005: "facility_name",
  P33_006: "postal_code",
  P33_007: "address",
  P33_008: "telephone_number",
  P33_009: "opening_date",
  P33_010: "url",
  P33_011: "access",
  P33_012: "number_of_screens",
  P33_013: "total_number_of_seats",
  P33_014: "community_center_type",
  P33_015: "number_of_business_days",
  P33_016: "business_hours",
  P33_017: "presence_of_admission",
  P33_018: "site_area",
  P33_019: "construction_total_area",
  P33_020: "number_of_holes",
  P33_021: "maximum_seats_hall",
  P33_022: "total_seats_hall",
  P33_023: "number_of_meeting_room",
  P33_024: "number_of_exhibition_room",
  P33_041: "postal_code_flag",
};

const EMPTY_MARKERS = ["‐", "-", "無"];

/** 国土数値情報 P33 集客施設 ZIP（Shapefile）をスポット一覧に変換する。 */
export function parseP33Zip(raw: Uint8Array, sourceUrl = P33_ZIP_URL): Spot[] {
  // ZIP を解凍して DBF / SHP を取り出す
  let dbfRaw: Uint8Array | null = null;
  let shpRaw: Uint8Array | null = null;
  try {
    const entries = parseZip(raw);
    for (const entry of entries) {
      const lower = entry.name.toLowerCase();
      if (lower.endsWith(".dbf") && dbfRaw === null) dbfRaw = entry.data;
      else if (lower.endsWith(".shp") && shpRaw === null) shpRaw = entry.data;
    }
  } catch (e) {
    if (e instanceof TourismError) throw e;
    throw new TourismParseError(`P33 ZIP の解凍に失敗しました: ${(e as Error).message}`);
  }
  if (dbfRaw === null || shpRaw === null) {
    throw new TourismParseError("P33 ZIP 内に DBF/SHP が見つかりません");
  }

  const { fields, numRecords, headerSize, recordSize } = parseDbfHeader(dbfRaw);
  if (fields.length === 0 || numRecords === 0) {
    throw new TourismParseError("P33 DBF にフィールド定義またはレコードがありません");
  }

  // 必要なフィールドのインデックスを解決
  const index: Record<string, number> = {};
  fields.forEach((f, i) => {
    index[f.name] = i;
  });
  const idxId = index["P33_001"];
  const idxType = index["P33_004"];
  const idxName = index["P33_005"];
  const idxAddr = index["P33_007"];
  const idxTel = index["P33_008"];
  const idxUrl = index["P33_010"];
  const idxAccess = index["P33_011"];
  const idxBizHours = index["P33_016"];
  if (idxName === undefined) {
    throw new TourismParseError("P33 DBF に施設名称（P33_005）フィールドがありません");
  }

  const points = readShpPoints(shpRaw);
  const spots: Spot[] = [];
  const dataStart = headerSize + 1; // ヘッダ末尾の 0x0D をスキップ
  for (let i = 0; i < numRecords; i++) {
    const offset = dataStart + i * recordSize;
    if (offset + recordSize > dbfRaw.length) break;
    const record = dbfRaw.slice(offset, offset + recordSize);
    const values = parseDbfRecord(record, fields);

    const name = values[idxName];
    if (!name) continue;
    const typeCode = idxType !== undefined ? values[idxType] : "";
    const typeName = P33_FACILITY_TYPES[typeCode] ?? "";
    const spotId = idxId !== undefined && values[idxId] ? values[idxId] : String(i + 1);
    const point = i < points.length ? points[i] : null;

    // 補足説明（区分・アクセス・営業時間）
    const descriptionParts: string[] = [];
    if (typeName) descriptionParts.push(typeName);
    if (idxAccess !== undefined && values[idxAccess] && !EMPTY_MARKERS.includes(values[idxAccess])) {
      descriptionParts.push(`アクセス: ${values[idxAccess]}`);
    }
    if (idxBizHours !== undefined && values[idxBizHours] && !EMPTY_MARKERS.includes(values[idxBizHours])) {
      descriptionParts.push(`営業時間: ${values[idxBizHours]}`);
    }
    let url = idxUrl !== undefined ? values[idxUrl] : "";
    if (EMPTY_MARKERS.includes(url)) url = "";

    spots.push({
      id: `p33-${spotId}`,
      name,
      category: typeName ? `集客施設（${typeName}）` : "集客施設",
      lat: point !== null ? point[1] : null,
      lon: point !== null ? point[0] : null,
      address: idxAddr !== undefined ? values[idxAddr] : "",
      phone: idxTel !== undefined ? values[idxTel] : "",
      url,
      description: descriptionParts.join("／"),
      source: P33_SOURCE_TEXT,
      sourceUrl,
    });
  }
  if (spots.length === 0) {
    throw new TourismParseError("P33 DBF に有効なレコードがありません");
  }
  return spots;
}

// ---------------------------------------------------------------------------
// ZIP パース（ストリームレス・メモリ内）
// ---------------------------------------------------------------------------

interface ZipEntry {
  name: string;
  data: Uint8Array;
}

/** 最小限の ZIP パーサ（非圧縮・Deflate のみ対応）。 */
export function parseZip(raw: Uint8Array): ZipEntry[] {
  const entries: ZipEntry[] = [];
  const dv = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
  let pos = 0;

  // End of Central Directory を探す
  let eocdPos = -1;
  for (let i = raw.length - 22; i >= 0; i--) {
    if (dv.getUint32(i, true) === 0x06054b50) {
      eocdPos = i;
      break;
    }
  }
  if (eocdPos === -1) throw new TourismParseError("ZIP の EOCD が見つかりません");

  const entryCount = dv.getUint16(eocdPos + 10, true);
  const cdOffset = dv.getUint32(eocdPos + 16, true);
  pos = cdOffset;
  for (let n = 0; n < entryCount; n++) {
    if (pos + 46 > raw.length) break;
    if (dv.getUint32(pos, true) !== 0x02014b50) break; // Central Directory シグネチャ
    const method = dv.getUint16(pos + 10, true);
    const compSize = dv.getUint32(pos + 20, true);
    const uncompSize = dv.getUint32(pos + 24, true);
    const nameLen = dv.getUint16(pos + 28, true);
    const extraLen = dv.getUint16(pos + 30, true);
    const commentLen = dv.getUint16(pos + 32, true);
    const localOffset = dv.getUint32(pos + 42, true);
    const name = new TextDecoder("utf-8").decode(raw.slice(pos + 46, pos + 46 + nameLen));
    // ローカルヘッダからデータ開始位置を計算
    const lhNameLen = dv.getUint16(localOffset + 26, true);
    const lhExtraLen = dv.getUint16(localOffset + 28, true);
    const dataStart = localOffset + 30 + lhNameLen + lhExtraLen;
    if (dataStart + compSize > raw.length) break;
    const data = raw.slice(dataStart, dataStart + compSize);
    let content: Uint8Array;
    if (method === 0) {
      content = data; // 非圧縮
    } else if (method === 8) {
      content = inflate(data, uncompSize); // Deflate
    } else {
      throw new TourismParseError(`ZIP の圧縮方式（method=${method}）には対応していません`);
    }
    entries.push({ name, data: content });
    pos += 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}

/** Deflate 圧縮データを伸長する（Node.js zlib 使用）。 */
function inflate(data: Uint8Array, _uncompressedSize: number): Uint8Array {
  try {
    const result = inflateSync(Buffer.from(data));
    return new Uint8Array(result);
  } catch {
    // 生 deflate ストリームの可能性
    const result = inflateRawSync(Buffer.from(data));
    return new Uint8Array(result);
  }
}

// ---------------------------------------------------------------------------
// モジュール関数（シンプルな利用向け）
// ---------------------------------------------------------------------------

export async function getTourismDatasets(options: {
  query?: string | null;
  ttl?: number;
  timeout?: number;
  fallbackToSample?: boolean;
} = {}): Promise<TourismDataset[]> {
  const client = new TourismClient({
    ttl: options.ttl,
    timeout: options.timeout,
    fallbackToSample: options.fallbackToSample,
  });
  return client.getDatasets({ query: options.query });
}

export async function getTourismStats(options: {
  year?: number | null;
  ttl?: number;
  timeout?: number;
  fallbackToSample?: boolean;
} = {}): Promise<TourismStat[]> {
  const client = new TourismClient({
    ttl: options.ttl,
    timeout: options.timeout,
    fallbackToSample: options.fallbackToSample,
  });
  return client.getIrikomi({ year: options.year });
}

export async function getTourismSpots(options: {
  category?: string | null;
  includeOnsen?: boolean;
  includeP33?: boolean;
  ttl?: number;
  timeout?: number;
  fallbackToSample?: boolean;
} = {}): Promise<Spot[]> {
  const client = new TourismClient({
    ttl: options.ttl,
    timeout: options.timeout,
    fallbackToSample: options.fallbackToSample,
  });
  return client.getSpots({
    category: options.category,
    includeOnsen: options.includeOnsen,
    includeP33: options.includeP33,
  });
}

// util から toFloat を再エクスポート（外部利用向け）
export { toFloat };
