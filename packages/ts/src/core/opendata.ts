/**
 * 新潟県オープンデータカタログ取得モジュール。
 *
 * 新潟県が公開するオープンデータ（統計・観光など）を取得する。
 *
 * データ源（取得優先順）:
 *   1. CKAN API: https://ckan.pref.niigata.lg.jp/api/3/action/...
 *      （2026-08 現在 DNS 解決不可のため実環境ではほぼ常にフォールバックへ移行する）
 *   2. 新潟県公式サイトのオープンデータ一覧 CSV（実在する代替データ源）
 *      https://www.pref.niigata.lg.jp/site/opendata/
 *   3. 内蔵サンプルデータ（最終フォールバック。オフラインでも動作）
 *
 * 取得できる内容:
 *   - データセット一覧（カタログ全体の検索・分野・形式による絞り込み）
 *   - 統計データ（人口時系列データ: 市町村別・年月別の人口）
 *   - 観光データ（道の駅一覧: 駅名・路線名・所在地・電話番号）
 *
 * 出典: 新潟県（オープンデータ）、新潟県統計課（人口）、新潟県道路管理課（道の駅）。
 * 全レスポンスに source / source_url を含めて出典を明記する。
 *
 * 利用条件: 新潟県オープンデータ利用規約（https://www.pref.niigata.lg.jp/sec/userguide/od-kiyaku.html）
 */

import { decodeText, parseCsvRows, toInt } from "./util.js";

export const SOURCE_TEXT = "出典:新潟県オープンデータ";
export const LICENSE_TEXT = "新潟県オープンデータ利用規約";
export const LICENSE_URL = "https://www.pref.niigata.lg.jp/sec/userguide/od-kiyaku.html";

/** タスク想定の CKAN カタログ API ベース URL（2026-08 現在 DNS 解決不可）。 */
export const CKAN_BASE_URL = "https://ckan.pref.niigata.lg.jp";

/** 新潟県オープンデータの公式ページ（実在する代替データ源の入り口）。 */
export const OPEN_DATA_PAGE_URL = "https://www.pref.niigata.lg.jp/site/opendata/";

/** 人口時系列データ(市町村別) の掲載ページ（新潟県統計課）。 */
export const POPULATION_PAGE_URL = "https://www.pref.niigata.lg.jp/site/tokei/1282075307357.html";

/** 新潟県道の駅の掲載ページ（新潟県道路管理課）。 */
export const MICHINO_EKI_PAGE_URL = "https://www.pref.niigata.lg.jp/dourokanri/1202317264067.html";

export const DEFAULT_TTL = 3600.0;
export const USER_AGENT = "nic/0.1 (+https://github.com/Cinnamobot/nic)";

export class OpenDataError extends Error {}
export class OpenDataFetchError extends OpenDataError {}
export class OpenDataParseError extends OpenDataError {}
export class OpenDataNotFoundError extends OpenDataError {}

/** カタログ上の 1 データセット。 */
export interface Dataset {
  id: string;
  name: string;
  category: string;
  description: string;
  fields: string;
  fiscalYear: string;
  updateFrequency: string;
  format: string;
  url: string;
  department: string;
  source: string;
  sourceUrl: string;
}

/** 人口時系列データの 1 レコード。 */
export interface PopulationRecord {
  date: string;
  municipalityCode: string;
  municipalityName: string;
  total: number;
  male: number;
  female: number;
  source: string;
  sourceUrl: string;
}

/** 道の駅 1 件。 */
export interface MichiNoEki {
  id: number;
  name: string;
  route: string;
  address: string;
  phone: string;
  source: string;
  sourceUrl: string;
}

// ---------------------------------------------------------------------------
// 内蔵サンプルデータ（最終フォールバック用）
// ---------------------------------------------------------------------------

const SAMPLE_DATASETS: Dataset[] = [
  {
    id: "234",
    name: "人口時系列データ(市町村別)",
    category: "人口・世帯",
    description: "大正９年からの市町村別人口データを掲載。",
    fields: "新潟県の人口総数、各歳人口合計、男女別数。",
    fiscalYear: "R5",
    updateFrequency: "毎月",
    format: "CSV",
    url: POPULATION_PAGE_URL,
    department: "統計課",
    source: SOURCE_TEXT,
    sourceUrl: OPEN_DATA_PAGE_URL,
  },
  {
    id: "731",
    name: "新潟県道の駅",
    category: "運輸・観光",
    description: "県内道の駅の名簿",
    fields: "名称、路線名、所在地、電話番号",
    fiscalYear: "R4",
    updateFrequency: "不定期",
    format: "Excel",
    url: MICHINO_EKI_PAGE_URL,
    department: "道路管理課",
    source: SOURCE_TEXT,
    sourceUrl: OPEN_DATA_PAGE_URL,
  },
  {
    id: "848",
    name: "観光統計",
    category: "運輸・観光",
    description: "県内観光入込客数等の統計",
    fields: "観光入込客数、宿泊者数 等",
    fiscalYear: "R4",
    updateFrequency: "毎年",
    format: "PDF",
    url: "https://www.pref.niigata.lg.jp/sec/kankokikaku/1245960085415.html",
    department: "観光企画課",
    source: SOURCE_TEXT,
    sourceUrl: OPEN_DATA_PAGE_URL,
  },
  {
    id: "927",
    name: "新潟県の税金などを納付することができる金融機関の窓口",
    category: "行財政",
    description: "県税等を納付できる金融機関の窓口一覧",
    fields: "金融機関名、窓口 等",
    fiscalYear: "R4",
    updateFrequency: "随時",
    format: "CSV",
    url: "https://www.pref.niigata.lg.jp/sec/suitoukanri/1356773325235.html",
    department: "税務課",
    source: SOURCE_TEXT,
    sourceUrl: OPEN_DATA_PAGE_URL,
  },
  {
    id: "687",
    name: "水揚情報",
    category: "農林水産業",
    description: "県内主要港の水揚量・金額",
    fields: "魚種、水揚量、金額 等",
    fiscalYear: "R4",
    updateFrequency: "随時",
    format: "CSV",
    url: "https://www.pref.niigata.lg.jp/site/suisan-kenkyu/mizuage.html",
    department: "水産課",
    source: SOURCE_TEXT,
    sourceUrl: OPEN_DATA_PAGE_URL,
  },
];

const SAMPLE_POPULATION: PopulationRecord[] = [
  { date: "2024/10/1 0:00", municipalityCode: "15201", municipalityName: "新潟市", total: 772425, male: 372208, female: 400217, source: SOURCE_TEXT, sourceUrl: POPULATION_PAGE_URL },
  { date: "2024/10/1 0:00", municipalityCode: "15202", municipalityName: "長岡市", total: 258131, male: 124938, female: 133193, source: SOURCE_TEXT, sourceUrl: POPULATION_PAGE_URL },
  { date: "2024/10/1 0:00", municipalityCode: "15204", municipalityName: "三条市", total: 93335, male: 44951, female: 48384, source: SOURCE_TEXT, sourceUrl: POPULATION_PAGE_URL },
  { date: "2024/10/1 0:00", municipalityCode: "15222", municipalityName: "上越市", total: 180014, male: 85837, female: 94177, source: SOURCE_TEXT, sourceUrl: POPULATION_PAGE_URL },
  { date: "2024/10/1 0:00", municipalityCode: "15225", municipalityName: "魚沼市", total: 32483, male: 15776, female: 16707, source: SOURCE_TEXT, sourceUrl: POPULATION_PAGE_URL },
];

const SAMPLE_MICHINO_EKI: MichiNoEki[] = [
  { id: 1, name: "豊栄", route: "一般国道7号", address: "新潟市北区木崎字切尾山3644-乙", phone: "025-388-2700", source: SOURCE_TEXT, sourceUrl: MICHINO_EKI_PAGE_URL },
  { id: 2, name: "加治川（さくらの里）", route: "一般国道7号", address: "新発田市横岡1147", phone: "0254-33-3175", source: SOURCE_TEXT, sourceUrl: MICHINO_EKI_PAGE_URL },
  { id: 3, name: "神林", route: "一般国道7号", address: "村上市牧目584", phone: "0254-66-6326", source: SOURCE_TEXT, sourceUrl: MICHINO_EKI_PAGE_URL },
  { id: 4, name: "朝日（まほろば）", route: "一般国道7号", address: "村上市猿沢1212", phone: "0254-72-0300", source: SOURCE_TEXT, sourceUrl: MICHINO_EKI_PAGE_URL },
  { id: 5, name: "新潟ふるさと村", route: "一般国道8号", address: "新潟市西区山田2307", phone: "025-230-3030", source: SOURCE_TEXT, sourceUrl: MICHINO_EKI_PAGE_URL },
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
      throw new OpenDataFetchError(`HTTP ${resp.status} で取得できませんでした: ${url}`);
    }
    return new Uint8Array(await resp.arrayBuffer());
  } catch (e) {
    if (e instanceof OpenDataError) throw e;
    throw new OpenDataFetchError(`HTTP 取得に失敗しました: ${url} (${(e as Error).message})`);
  } finally {
    clearTimeout(timer);
  }
}

/** 新潟県オープンデータカタログ取得クライアント。 */
export class OpenDataClient {
  ttl: number;
  timeout: number;
  fallbackToSample: boolean;
  private cache = new Map<string, CacheEntry>();
  private warningsList: string[] = [];

  constructor(options: { ttl?: number; timeout?: number; fallbackToSample?: boolean } = {}) {
    this.ttl = options.ttl ?? DEFAULT_TTL;
    this.timeout = options.timeout ?? 15.0;
    this.fallbackToSample = options.fallbackToSample ?? true;
  }

  /** 直近の取得で発生したフォールバック状況の説明一覧。 */
  get warnings(): string[] {
    return [...this.warningsList];
  }

  /** データセット一覧を取得・絞り込みする。 */
  async getDatasets(options: {
    query?: string | null;
    category?: string | null;
    dataFormat?: string | null;
    force?: boolean;
  } = {}): Promise<Dataset[]> {
    const force = options.force ?? false;
    let datasets = this.getCached("datasets", force) as Dataset[] | null;
    if (datasets === null) {
      datasets = await this.fetchDatasets();
      this.putCache("datasets", datasets);
    }
    return filterDatasets(datasets, {
      query: options.query ?? null,
      category: options.category ?? null,
      dataFormat: options.dataFormat ?? null,
    });
  }

  async searchDatasets(query: string, options: { limit?: number; force?: boolean } = {}): Promise<Dataset[]> {
    const datasets = await this.getDatasets({ query, force: options.force });
    return datasets.slice(0, options.limit ?? 20);
  }

  /** 統計データ（人口時系列データ）を取得する。 */
  async getPopulation(options: { municipality?: string | null; force?: boolean } = {}): Promise<PopulationRecord[]> {
    const force = options.force ?? false;
    let records = this.getCached("population", force) as PopulationRecord[] | null;
    if (records === null) {
      records = await this.fetchPopulation();
      this.putCache("population", records);
    }
    if (options.municipality) {
      const m = options.municipality;
      records = records.filter((r) => r.municipalityName.includes(m));
    }
    return records;
  }

  /** 観光データ（道の駅一覧）を取得する。 */
  async getTourism(options: { force?: boolean } = {}): Promise<MichiNoEki[]> {
    const force = options.force ?? false;
    let stations = this.getCached("michinoeki", force) as MichiNoEki[] | null;
    if (stations === null) {
      stations = await this.fetchMichinoeki();
      this.putCache("michinoeki", stations);
    }
    return stations;
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

  private async download(url: string): Promise<Uint8Array> {
    return httpGet(url, this.timeout * 1000, {
      Accept: "text/csv,text/html,application/json,*/*",
    });
  }

  // -- データ源 1: CKAN API ------------------------------------------------

  private async fetchFromCkan(): Promise<Dataset[] | null> {
    const url = `${CKAN_BASE_URL}/api/3/action/package_search?rows=1000`;
    let payload: unknown;
    try {
      const raw = await this.download(url);
      payload = JSON.parse(new TextDecoder("utf-8").decode(raw));
    } catch (e) {
      this.warn(`CKAN API を利用できません（${(e as Error).message}）。公式サイトの一覧へフォールバックします。`);
      return null;
    }
    if (typeof payload !== "object" || payload === null || (payload as Record<string, unknown>).success !== true) {
      this.warn("CKAN API がエラー応答を返しました。公式サイトの一覧へフォールバックします。");
      return null;
    }
    const result = (payload as Record<string, unknown>).result;
    if (typeof result !== "object" || result === null) return null;
    const packages = (result as Record<string, unknown>).results;
    if (!Array.isArray(packages)) return null;

    const datasets: Dataset[] = [];
    for (const pkg of packages) {
      if (typeof pkg !== "object" || pkg === null) continue;
      const p = pkg as Record<string, unknown>;
      const extras: Record<string, string> = {};
      if (Array.isArray(p.extras)) {
        for (const e of p.extras) {
          if (typeof e !== "object" || e === null) continue;
          const ee = e as Record<string, unknown>;
          extras[String(ee.key ?? "")] = String(ee.value ?? "");
        }
      }
      let fmt = "";
      const resources = p.resources;
      if (Array.isArray(resources) && resources.length > 0 && typeof resources[0] === "object" && resources[0] !== null) {
        fmt = String((resources[0] as Record<string, unknown>).format ?? "").trim();
      }
      datasets.push({
        id: String(p.id ?? ""),
        name: String(p.title ?? p.name ?? ""),
        category: extras["分野"] ?? extras["category"] ?? "",
        description: String(p.notes ?? ""),
        fields: extras["主な項目"] ?? "",
        fiscalYear: extras["作成年度・時点"] ?? extras["fiscal_year"] ?? "",
        updateFrequency: extras["更新頻度"] ?? extras["update_frequency"] ?? "",
        format: fmt || (extras["データ形式"] ?? ""),
        url: String(p.url ?? ""),
        department: extras["所属名"] ?? "",
        source: SOURCE_TEXT,
        sourceUrl: CKAN_BASE_URL,
      });
    }
    return datasets;
  }

  // -- データ源 2: 公式サイトのオープンデータ一覧 CSV ------------------------

  private async fetchCatalogCsvUrl(): Promise<string | null> {
    let html: string;
    try {
      const raw = await this.download(OPEN_DATA_PAGE_URL);
      html = new TextDecoder("utf-8").decode(raw);
    } catch (e) {
      this.warn(`オープンデータ一覧ページを取得できません（${(e as Error).message}）。`);
      return null;
    }
    const m = html.match(/href="([^"]*uploaded\/attachment\/[^"]*\.csv)"/i);
    if (!m) {
      this.warn("オープンデータ一覧ページに CSV リンクが見つかりませんでした。");
      return null;
    }
    return new URL(m[1], OPEN_DATA_PAGE_URL).toString();
  }

  private parseCatalogCsv(raw: Uint8Array): Dataset[] {
    const text = decodeText(raw);
    const rows = parseCsvRows(text);
    if (rows.length === 0) throw new OpenDataParseError("一覧 CSV が空です");
    const header = rows[0].map((h) => h.trim());
    const dataRows = header.includes("データ名") ? rows.slice(1) : rows;
    const datasets: Dataset[] = [];
    for (const row of dataRows) {
      if (row.length < 9) continue;
      const num = row[0].trim();
      if (!/^\d+$/.test(num)) continue;
      let url = row[9].trim();
      if (url.startsWith("/")) url = new URL(url, OPEN_DATA_PAGE_URL).toString();
      if (!url.startsWith("http")) url = "";
      datasets.push({
        id: num,
        name: row[2].trim(),
        category: normalizeCategory(row[3]),
        description: row[4].trim(),
        fields: row[5].trim(),
        fiscalYear: normalizeFiscalYear(row[6]),
        updateFrequency: normalizeFrequency(row[7]),
        format: normalizeFormat(row[8]),
        url,
        department: row.length > 1 ? row[1].trim() : "",
        source: SOURCE_TEXT,
        sourceUrl: OPEN_DATA_PAGE_URL,
      });
    }
    return datasets;
  }

  private async fetchDatasets(): Promise<Dataset[]> {
    // 1. CKAN API
    const ckan = await this.fetchFromCkan();
    if (ckan && ckan.length > 0) return ckan;
    // 2. 公式サイトの一覧 CSV
    const csvUrl = await this.fetchCatalogCsvUrl();
    if (csvUrl) {
      try {
        const raw = await this.download(csvUrl);
        const datasets = this.parseCatalogCsv(raw);
        if (datasets.length > 0) {
          this.warn(`CKAN API が利用できないため、公式サイトの一覧 CSV を使用しました: ${csvUrl}`);
          return datasets;
        }
      } catch (e) {
        this.warn(`一覧 CSV の取得に失敗しました（${(e as Error).message}）。`);
      }
    }
    // 3. 内蔵サンプル
    if (this.fallbackToSample) {
      this.warn("外部データ源を利用できなかったため、内蔵サンプルデータ（5 件）を返します。");
      return SAMPLE_DATASETS.map((d) => ({ ...d }));
    }
    throw new OpenDataFetchError(
      "新潟県オープンデータカタログ（CKAN API・公式サイト一覧）からデータを取得できませんでした",
    );
  }

  private async fetchPopulation(): Promise<PopulationRecord[]> {
    const csvUrls: string[] = [];
    try {
      const raw = await this.download(POPULATION_PAGE_URL);
      const html = new TextDecoder("utf-8").decode(raw);
      const seen = new Set<string>();
      for (const m of html.matchAll(/href="([^"]*uploaded\/attachment\/[^"]*\.csv)"/gi)) {
        const url = new URL(m[1], POPULATION_PAGE_URL).toString();
        if (!seen.has(url)) {
          seen.add(url);
          csvUrls.push(url);
        }
      }
    } catch (e) {
      this.warn(`人口時系列データのページを取得できません（${(e as Error).message}）。`);
    }
    const allRecords: PopulationRecord[] = [];
    const seenGlobal = new Set<string>();
    for (const url of csvUrls) {
      try {
        const raw = await this.download(url);
        const records = parsePopulationCsv(raw, url);
        if (records.length > 0) {
          const unique: PopulationRecord[] = [];
          for (const rec of records) {
            const key = `${rec.municipalityCode}|${rec.date.trim()}`;
            if (seenGlobal.has(key)) continue;
            seenGlobal.add(key);
            unique.push(rec);
          }
          this.warn(`人口時系列データを取得しました: ${url}（${unique.length} 行）`);
          allRecords.push(...unique);
        }
      } catch (e) {
        this.warn(`人口 CSV の取得に失敗しました（${url}: ${(e as Error).message}）。`);
      }
    }
    if (allRecords.length > 0) {
      return sortPopulationNewestFirst(allRecords);
    }
    if (this.fallbackToSample) {
      this.warn("人口時系列データを外部取得できなかったため、内蔵サンプルを返します。");
      return SAMPLE_POPULATION.map((r) => ({ ...r }));
    }
    throw new OpenDataFetchError("人口時系列データを取得できませんでした");
  }

  private async fetchMichinoeki(): Promise<MichiNoEki[]> {
    try {
      const raw = await this.download(MICHINO_EKI_PAGE_URL);
      const html = new TextDecoder("utf-8").decode(raw);
      const stations = parseMichinoekiHtml(html, MICHINO_EKI_PAGE_URL);
      if (stations.length > 0) {
        this.warn(`道の駅一覧を取得しました: ${MICHINO_EKI_PAGE_URL}`);
        return stations;
      }
    } catch (e) {
      this.warn(`道の駅のページを取得できません（${(e as Error).message}）。`);
    }
    if (this.fallbackToSample) {
      this.warn("道の駅一覧を外部取得できなかったため、内蔵サンプルを返します。");
      return SAMPLE_MICHINO_EKI.map((s) => ({ ...s }));
    }
    throw new OpenDataFetchError("道の駅一覧を取得できませんでした");
  }
}

// ---------------------------------------------------------------------------
// パース補助関数
// ---------------------------------------------------------------------------

function normalizeCategory(raw: string): string {
  const s = raw.trim();
  const m = s.match(/^(?:・\s*)?\d*\s*(.*)$/);
  return m ? m[1] : s;
}

function normalizeFormat(raw: string): string {
  const s = raw.trim();
  const m = s.match(/^\d+\s*(.*)$/);
  return m ? m[1] : s;
}

function normalizeFiscalYear(raw: string): string {
  const s = raw.trim();
  const m = s.match(/^(H\d{1,2}|R\d{1,2}|S\d{1,2})/i);
  return m ? m[1].toUpperCase() : s;
}

function normalizeFrequency(raw: string): string {
  const s = raw.trim();
  if (["毎月", "月１回", "月1回", "１分"].some((k) => s.includes(k))) return "毎月";
  if (s.includes("毎週")) return "毎週";
  if (["毎年", "年１回", "年1回", "年２回", "年2回"].some((k) => s.includes(k))) return "毎年";
  if (["随時", "都度", "届出時"].some((k) => s.includes(k))) return "随時";
  if (["なし", "しない", "廃止"].some((k) => s.includes(k))) return "更新なし";
  if (s.includes("不定期")) return "不定期";
  if (s.includes("四半期")) return "四半期";
  return s ? "その他" : "不明";
}

/** 人口時系列データ CSV をパースする（2 種類のレイアウトに対応）。 */
export function parsePopulationCsv(raw: Uint8Array, sourceUrl: string): PopulationRecord[] {
  const text = decodeText(raw);
  const rows = parseCsvRows(text);
  if (rows.length === 0) throw new OpenDataParseError("人口 CSV が空です");
  const header = rows[0].map((h) => h.trim());

  // レイアウト A（広形式）
  const layoutA = findIndexes(header, {
    date: "年月日",
    code: "市町村CD",
    name: "市町村名",
    total: "人口総数",
    male: "男計",
    female: "女計",
  });
  // レイアウト B（コンパクト形式）
  const layoutB = findIndexes(header, {
    date: "年月日",
    code: "団体コード",
    name: "都道府県名・市区町村名",
    total: "総数",
    male: "男",
    female: "女",
  });
  const layout = layoutA ?? layoutB;
  if (layout === null) {
    throw new OpenDataParseError(`人口 CSV のヘッダが想定と異なります: ${header.slice(0, 8).join(",")}`);
  }
  const codeLen = layoutA !== null ? 5 : 6;

  const records: PopulationRecord[] = [];
  const seen = new Set<string>();
  for (const row of rows.slice(1)) {
    if (row.length <= Math.max(...Object.values(layout))) continue;
    const code = row[layout.code].trim();
    if (code.length !== codeLen || !/^\d+$/.test(code)) continue; // 集計行（県計など）を除外
    const name = row[layout.name].trim();
    if (!name || (name.includes("県") && name.length <= 3)) continue; // 「新潟県」等の集計行を除外
    const key = `${code}|${row[layout.date].trim()}`;
    if (seen.has(key)) continue;
    const total = toInt(row[layout.total]);
    const male = toInt(row[layout.male]);
    const female = toInt(row[layout.female]);
    if (total === null || male === null || female === null) continue;
    seen.add(key);
    records.push({
      date: row[layout.date].trim(),
      municipalityCode: code,
      municipalityName: name,
      total,
      male,
      female,
      source: SOURCE_TEXT,
      sourceUrl,
    });
  }
  return records;
}

function findIndexes(
  header: string[],
  wanted: Record<string, string>,
): Record<string, number> | null {
  const result: Record<string, number> = {};
  for (const [key, label] of Object.entries(wanted)) {
    const idx = header.indexOf(label);
    if (idx === -1) return null;
    result[key] = idx;
  }
  return result;
}

const MICHINOEKI_ROW_RE = /<tr>\s*<td[^>]*>(\d+)<\/td>\s*<td[^>]*>(.*?)<\/td>\s*<td[^>]*>(.*?)<\/td>\s*<td[^>]*>(.*?)<\/td>\s*<td[^>]*>(.*?)<\/td>\s*<\/tr>/gi;

/** 道の駅ページの HTML テーブル（番号/駅名/路線名/所在地/電話番号）をパースする。 */
export function parseMichinoekiHtml(html: string, sourceUrl: string): MichiNoEki[] {
  const stations: MichiNoEki[] = [];
  for (const m of html.matchAll(MICHINOEKI_ROW_RE)) {
    const cells = m.slice(1, 6).map((c) => c.replace(/<[^>]+>/g, "").trim());
    if (!cells[1]) continue;
    const sid = Number(cells[0]);
    if (!Number.isInteger(sid)) continue;
    stations.push({
      id: sid,
      name: cells[1],
      route: cells[2],
      address: cells[3],
      phone: cells[4],
      source: SOURCE_TEXT,
      sourceUrl,
    });
  }
  if (stations.length === 0) {
    throw new OpenDataParseError("道の駅のテーブルが見つかりませんでした");
  }
  return stations;
}

// ---------------------------------------------------------------------------
// フィルタ・並べ替え
// ---------------------------------------------------------------------------

function filterDatasets(
  datasets: Dataset[],
  options: { query: string | null; category: string | null; dataFormat: string | null },
): Dataset[] {
  let result = datasets;
  if (options.query) {
    const q = options.query.trim();
    result = result.filter(
      (d) =>
        d.name.includes(q) ||
        d.description.includes(q) ||
        d.category.includes(q) ||
        d.fields.includes(q),
    );
  }
  if (options.category) {
    const c = options.category.trim();
    result = result.filter((d) => d.category.includes(c));
  }
  if (options.dataFormat) {
    const f = options.dataFormat.trim().toLowerCase();
    result = result.filter((d) => d.format.toLowerCase() === f);
  }
  return result;
}

function sortPopulationNewestFirst(records: PopulationRecord[]): PopulationRecord[] {
  const key = (r: PopulationRecord): [string, string] => {
    const m = r.date.trim().match(/(\d{4})\/(\d{1,2})\/(\d{1,2})/);
    if (m) {
      const y = m[1];
      const mo = String(Number(m[2])).padStart(2, "0");
      const d = String(Number(m[3])).padStart(2, "0");
      return [`${y}-${mo}-${d}`, r.municipalityCode];
    }
    return [r.date.trim(), r.municipalityCode];
  };
  return [...records].sort((a, b) => {
    const ka = key(a);
    const kb = key(b);
    return kb[0] < ka[0] ? -1 : kb[0] > ka[0] ? 1 : kb[1] < ka[1] ? -1 : kb[1] > ka[1] ? 1 : 0;
  });
}

// ---------------------------------------------------------------------------
// モジュール関数（シンプルな利用向け）
// ---------------------------------------------------------------------------

export async function getDatasets(options: {
  query?: string | null;
  category?: string | null;
  dataFormat?: string | null;
  ttl?: number;
  timeout?: number;
  fallbackToSample?: boolean;
} = {}): Promise<Dataset[]> {
  const client = new OpenDataClient({
    ttl: options.ttl,
    timeout: options.timeout,
    fallbackToSample: options.fallbackToSample,
  });
  return client.getDatasets({
    query: options.query,
    category: options.category,
    dataFormat: options.dataFormat,
  });
}

export async function getPopulation(options: {
  municipality?: string | null;
  ttl?: number;
  timeout?: number;
  fallbackToSample?: boolean;
} = {}): Promise<PopulationRecord[]> {
  const client = new OpenDataClient({
    ttl: options.ttl,
    timeout: options.timeout,
    fallbackToSample: options.fallbackToSample,
  });
  return client.getPopulation({ municipality: options.municipality });
}

export async function getTourism(options: {
  ttl?: number;
  timeout?: number;
  fallbackToSample?: boolean;
} = {}): Promise<MichiNoEki[]> {
  const client = new OpenDataClient({
    ttl: options.ttl,
    timeout: options.timeout,
    fallbackToSample: options.fallbackToSample,
  });
  return client.getTourism();
}
