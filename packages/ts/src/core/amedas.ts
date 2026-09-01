/**
 * 気象庁アメダス（新潟県）データ取得モジュール。
 *
 * 気象庁が提供する「最新の気象データ」CSV ファイル（機械判読データ）から、
 * 新潟県内のアメダス観測所の積雪・気温・降水量を取得する。
 *
 * データ源:
 *   - 1時間降水量: https://www.data.jma.go.jp/stats/data/mdrr/pre_rct/alltable/pre1h00_rct.csv
 *   - 最高気温:    https://www.data.jma.go.jp/stats/data/mdrr/tem_rct/alltable/mxtemsadext00_rct.csv
 *   - 最低気温:    https://www.data.jma.go.jp/stats/data/mdrr/tem_rct/alltable/mntemsadext00_rct.csv
 *   - 現在の積雪:  https://www.data.jma.go.jp/stats/data/mdrr/snc_rct/alltable/snc00_rct.csv
 *     （積雪系 CSV は夏季は提供休止のため 404 になる場合がある）
 *
 * 出典: 気象庁「最新の気象データ」CSVダウンロード
 * https://www.data.jma.go.jp/stats/data/mdrr/docs/csv_dl_readme.html
 */

import iconv from "iconv-lite";

export const SOURCE_TEXT = "出典:気象庁";
export const SOURCE_URL =
  "https://www.data.jma.go.jp/stats/data/mdrr/docs/csv_dl_readme.html";

const _BASE = "https://www.data.jma.go.jp/stats/data/mdrr";

/** 気象要素別の CSV 取得 URL。 */
export const CSV_URLS: Record<AmedasElementValue, string> = {
  precipitation: `${_BASE}/pre_rct/alltable/pre1h00_rct.csv`,
  max_temp: `${_BASE}/tem_rct/alltable/mxtemsadext00_rct.csv`,
  min_temp: `${_BASE}/tem_rct/alltable/mntemsadext00_rct.csv`,
  snow: `${_BASE}/snc_rct/alltable/snc00_rct.csv`,
};

/** 品質情報コード（気象庁「品質情報について」より） */
export const QUALITY_CODES: Record<number, string> = {
  1: "資料なし、未報告",
  2: "利用不適値",
  3: "疑問値",
  4: "資料不足値",
  5: "準正常値",
  8: "正常値",
};

export type AmedasElementValue = "precipitation" | "max_temp" | "min_temp" | "snow";

/** 取得可能な気象要素。 */
export const AMEDAS_ELEMENTS: AmedasElementValue[] = [
  "precipitation",
  "max_temp",
  "min_temp",
  "snow",
];

export class AmedasError extends Error {}
export class AmedasFetchError extends AmedasError {}
export class AmedasParseError extends AmedasError {}
export class AmedasStationNotFoundError extends AmedasError {}

/** アメダス観測所。 */
export interface Station {
  code: string; // 観測所番号 (例: "54232")
  name: string; // 地点名 (例: "新潟")
  lat: number; // 緯度 (度)
  lon: number; // 経度 (度)
  altitude: number; // 標高 (m)
  stationType: string; // 観測所種別 A/B/C
  elements: string; // 観測要素コード (例: "11112010")
}

/** 1 観測所の観測値。 */
export interface Observation {
  station: Station;
  value: number | null; // 観測値（欠測時 null）
  quality: number | null; // 品質情報コード
  qualityText: string; // 品質情報の説明
  observedAt: Date; // 観測日時 (UTC)
  source: string;
}

/** 取得結果一式。 */
export interface AmedasData {
  element: AmedasElementValue;
  observations: Observation[];
  fetchedAt: Date;
  source: string;
}

/**
 * 新潟県内のアメダス観測所一覧（44 観測所）
 * 出典: 気象庁アメダス観測所位置データ（amedastable.json）より新潟県分を抽出
 */
export const NIIGATA_STATIONS: Record<string, Station> = {
  "54012": { code: "54012", name: "粟島", lat: 38.465, lon: 139.2533, altitude: 4, stationType: "C", elements: "11112010" },
  "54041": { code: "54041", name: "弾崎", lat: 38.33, lon: 138.5117, altitude: 58, stationType: "C", elements: "11112010" },
  "54056": { code: "54056", name: "高根", lat: 38.33, lon: 139.6033, altitude: 85, stationType: "C", elements: "01000000" },
  "54086": { code: "54086", name: "村上", lat: 38.2267, lon: 139.4783, altitude: 10, stationType: "C", elements: "11112010" },
  "54097": { code: "54097", name: "三面", lat: 38.2467, lon: 139.605, altitude: 45, stationType: "C", elements: "01000000" },
  "54157": { code: "54157", name: "相川", lat: 38.0283, lon: 138.24, altitude: 6, stationType: "B", elements: "11111111" },
  "54166": { code: "54166", name: "両津", lat: 38.0733, lon: 138.44, altitude: 2, stationType: "C", elements: "11112010" },
  "54181": { code: "54181", name: "中条", lat: 38.0767, lon: 139.3883, altitude: 14, stationType: "C", elements: "11112010" },
  "54191": { code: "54191", name: "下関", lat: 38.0917, lon: 139.5633, altitude: 33, stationType: "C", elements: "11112110" },
  "54232": { code: "54232", name: "新潟", lat: 37.8933, lon: 139.0183, altitude: 4, stationType: "A", elements: "11111111" },
  "54236": { code: "54236", name: "松浜", lat: 37.955, lon: 139.1117, altitude: 1, stationType: "C", elements: "11110100" },
  "54271": { code: "54271", name: "羽茂", lat: 37.8417, lon: 138.3133, altitude: 11, stationType: "C", elements: "11112010" },
  "54296": { code: "54296", name: "新津", lat: 37.7917, lon: 139.0867, altitude: 3, stationType: "C", elements: "11112110" },
  "54301": { code: "54301", name: "瓢湖", lat: 37.8333, lon: 139.2367, altitude: 9, stationType: "C", elements: "01000000" },
  "54311": { code: "54311", name: "赤谷", lat: 37.835, lon: 139.415, altitude: 135, stationType: "C", elements: "01000000" },
  "54341": { code: "54341", name: "巻", lat: 37.7683, lon: 138.9133, altitude: 2, stationType: "C", elements: "11112010" },
  "54387": { code: "54387", name: "寺泊", lat: 37.64, lon: 138.7667, altitude: 44, stationType: "C", elements: "11112010" },
  "54396": { code: "54396", name: "三条", lat: 37.64, lon: 138.955, altitude: 9, stationType: "C", elements: "11112010" },
  "54406": { code: "54406", name: "村松", lat: 37.6967, lon: 139.1883, altitude: 25, stationType: "C", elements: "01000000" },
  "54421": { code: "54421", name: "津川", lat: 37.6717, lon: 139.4467, altitude: 100, stationType: "C", elements: "11112110" },
  "54462": { code: "54462", name: "宮寄上", lat: 37.58, lon: 139.14, altitude: 125, stationType: "C", elements: "01000000" },
  "54472": { code: "54472", name: "室谷", lat: 37.55, lon: 139.37, altitude: 200, stationType: "C", elements: "01000000" },
  "54501": { code: "54501", name: "長岡", lat: 37.45, lon: 138.8233, altitude: 23, stationType: "C", elements: "11112110" },
  "54506": { code: "54506", name: "栃尾", lat: 37.4783, lon: 138.9917, altitude: 61, stationType: "C", elements: "01000000" },
  "54541": { code: "54541", name: "柏崎", lat: 37.3517, lon: 138.5533, altitude: 7, stationType: "C", elements: "11112110" },
  "54566": { code: "54566", name: "守門", lat: 37.3467, lon: 139.0433, altitude: 222, stationType: "C", elements: "11112110" },
  "54586": { code: "54586", name: "大潟", lat: 37.225, lon: 138.325, altitude: 13, stationType: "C", elements: "11112010" },
  "54606": { code: "54606", name: "小国", lat: 37.2917, lon: 138.7017, altitude: 83, stationType: "C", elements: "01000000" },
  "54616": { code: "54616", name: "小出", lat: 37.2267, lon: 138.9633, altitude: 98, stationType: "C", elements: "11112110" },
  "54621": { code: "54621", name: "大湯", lat: 37.205, lon: 139.0617, altitude: 240, stationType: "C", elements: "01000000" },
  "54651": { code: "54651", name: "高田", lat: 37.1067, lon: 138.2467, altitude: 13, stationType: "B", elements: "11111111" },
  "54661": { code: "54661", name: "安塚", lat: 37.1067, lon: 138.4567, altitude: 126, stationType: "C", elements: "11112110" },
  "54666": { code: "54666", name: "川谷", lat: 37.2, lon: 138.5167, altitude: 206, stationType: "C", elements: "01000000" },
  "54671": { code: "54671", name: "松代", lat: 37.1317, lon: 138.6067, altitude: 210, stationType: "C", elements: "01000000" },
  "54676": { code: "54676", name: "十日町", lat: 37.1433, lon: 138.7267, altitude: 170, stationType: "C", elements: "11112110" },
  "54711": { code: "54711", name: "糸魚川", lat: 37.0433, lon: 137.875, altitude: 8, stationType: "C", elements: "11112010" },
  "54721": { code: "54721", name: "能生", lat: 37.0833, lon: 138.0233, altitude: 55, stationType: "C", elements: "11112110" },
  "54737": { code: "54737", name: "筒方", lat: 37.03, lon: 138.3433, altitude: 255, stationType: "C", elements: "01000000" },
  "54761": { code: "54761", name: "塩沢", lat: 37.0383, lon: 138.8467, altitude: 195, stationType: "C", elements: "01000000" },
  "54816": { code: "54816", name: "関山", lat: 36.9333, lon: 138.2217, altitude: 350, stationType: "C", elements: "11112110" },
  "54836": { code: "54836", name: "津南", lat: 36.9967, lon: 138.6833, altitude: 452, stationType: "C", elements: "11112110" },
  "54841": { code: "54841", name: "湯沢", lat: 36.9417, lon: 138.81, altitude: 340, stationType: "C", elements: "11112110" },
  "54876": { code: "54876", name: "平岩", lat: 36.88, lon: 137.8667, altitude: 281, stationType: "C", elements: "01000000" },
  "54892": { code: "54892", name: "樽本", lat: 36.89, lon: 138.275, altitude: 633, stationType: "C", elements: "01000000" },
};

/** 観測所名 → コード の対応表。 */
export const STATION_NAME_TO_CODE: Record<string, string> = Object.fromEntries(
  Object.values(NIIGATA_STATIONS).map((s) => [s.name, s.code]),
);

/** JST (UTC+9) を UTC に戻すためのミリ秒。 */
const JST_OFFSET_MS = 9 * 60 * 60 * 1000;

/** 気象庁 CSV のヘッダ行判定（先頭セルが「観測所番号」等の場合にスキップ）。 */
const CSV_HEADER_MARKERS = ["観測所番号", "統計開始年"];

interface CacheEntry {
  data: AmedasData;
  expiresAt: number;
}

/** 簡易 HTTP クライアント（fetch ラッパー）。 */
async function httpGet(
  url: string,
  timeoutMs: number,
  headers: Record<string, string>,
): Promise<Uint8Array> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(url, {
      headers: { "User-Agent": "nic/0.1 (+https://github.com/Cinnamobot/nic)", ...headers },
      signal: controller.signal,
      redirect: "follow",
    });
    if (resp.status === 404) {
      throw new AmedasFetchError(
        `気象庁 CSV が取得できません (HTTP 404)。この要素は現在提供休止中の可能性があります: ${url}`,
      );
    }
    if (resp.status !== 200) {
      throw new AmedasFetchError(
        `気象庁 CSV の取得に失敗しました (HTTP ${resp.status}): ${url}`,
      );
    }
    const raw = new Uint8Array(await resp.arrayBuffer());
    // 気象庁は取得上限（1日10GB）超過時に HTTP 403 ではなく「アクセス制限」を
    // 示すエラーページを返す場合があるため、200 でも実データかどうかを検証する。
    const head = new TextDecoder("utf-8", { fatal: false }).decode(raw.slice(0, 512));
    if (raw.length === 0 || head.includes("HTTP 403") || head.includes("Forbidden")) {
      throw new AmedasFetchError(
        `気象庁 CSV が取得できません（アクセス制限の可能性）。取得量が制限（1日10GB）に近い場合は時間をおいて再試行してください: ${url}`,
      );
    }
    return raw;
  } catch (e) {
    if (e instanceof AmedasError) throw e;
    throw new AmedasFetchError(`気象庁 CSV の取得に失敗しました: ${url} (${(e as Error).message})`);
  } finally {
    clearTimeout(timer);
  }
}

/** 気象庁アメダス CSV を取得・キャッシュするクライアント。 */
export class AmedasClient {
  ttl: number;
  timeout: number;
  private cache = new Map<AmedasElementValue, CacheEntry>();

  constructor(options: { ttl?: number; timeout?: number } = {}) {
    this.ttl = options.ttl ?? 300.0;
    this.timeout = options.timeout ?? 15.0;
  }

  /** 新潟県内の全観測所一覧を返す。 */
  getStations(): Station[] {
    return Object.values(NIIGATA_STATIONS);
  }

  /** 観測所番号から観測所を返す。存在しなければエラー。 */
  getStation(code: string): Station {
    const st = NIIGATA_STATIONS[code];
    if (!st) {
      throw new AmedasStationNotFoundError(
        `新潟県内に観測所番号 ${code} は存在しません`,
      );
    }
    return st;
  }

  /** 指定要素のアメダスデータを取得する。 */
  async fetch(
    element: AmedasElementValue,
    options: { codes?: string[] | null; force?: boolean } = {},
  ): Promise<AmedasData> {
    const codes = options.codes;
    const force = options.force ?? false;
    if (codes !== undefined && codes !== null) {
      // 部分取得の場合は事前に観測所の存在を検証する
      for (const c of codes) this.getStation(c);
    }
    let data = this.getCached(element, force);
    if (data === null) {
      data = await this.fetchAndParse(element);
      this.putCache(element, data);
    }
    if (codes && codes.length > 0) {
      const wanted = new Set(codes);
      data = { ...data, observations: data.observations.filter((o) => wanted.has(o.station.code)) };
    }
    return data;
  }

  fetchPrecipitation(codes?: string[] | null, force = false): Promise<AmedasData> {
    return this.fetch("precipitation", { codes, force });
  }

  fetchTemperature(codes?: string[] | null, force = false): Promise<AmedasData> {
    return this.fetch("max_temp", { codes, force });
  }

  fetchSnow(codes?: string[] | null, force = false): Promise<AmedasData> {
    return this.fetch("snow", { codes, force });
  }

  private getCached(element: AmedasElementValue, force: boolean): AmedasData | null {
    if (force) return null;
    const entry = this.cache.get(element);
    if (entry && entry.expiresAt > Date.now()) return entry.data;
    return null;
  }

  private putCache(element: AmedasElementValue, data: AmedasData): void {
    this.cache.set(element, { data, expiresAt: Date.now() + this.ttl * 1000 });
  }

  private async fetchAndParse(element: AmedasElementValue): Promise<AmedasData> {
    const url = CSV_URLS[element];
    const raw = await httpGet(url, this.timeout * 1000, {
      Referer: "https://www.data.jma.go.jp/stats/data/mdrr/",
    });
    const rows = parseCsvBytes(raw);
    const observations: Observation[] = [];
    for (const row of rows) {
      const obs = rowToObservation(element, row);
      if (obs !== null) observations.push(obs);
    }
    return {
      element,
      observations,
      fetchedAt: new Date(),
      source: SOURCE_TEXT,
    };
  }
}

/** Shift_JIS の CSV バイト列をパースして行リストを返す（ヘッダ行を除く）。 */
export function parseCsvBytes(raw: Uint8Array): string[][] {
  let text: string;
  try {
    text = iconv.decode(Buffer.from(raw), "cp932");
  } catch (e) {
    throw new AmedasParseError(`CSV の Shift_JIS デコードに失敗しました: ${(e as Error).message}`);
  }
  const rows = parseCsvRowsRaw(text);
  if (rows.length === 0) throw new AmedasParseError("CSV が空です");
  // ヘッダ行を除く（先頭セルが「観測所番号」等の場合）
  const header = rows[0];
  const first = (header[0] ?? "").trim();
  if (CSV_HEADER_MARKERS.some((m) => first.includes(m)) || !/^\d+$/.test(first)) {
    rows.shift();
  }
  return rows;
}

function parseCsvRowsRaw(text: string): string[][] {
  const rows: string[][] = [];
  for (const line of text.split(/\r\n|\n|\r/)) {
    if (!line.trim()) continue;
    const cells: string[] = [];
    let current = "";
    let inQuotes = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (inQuotes) {
        if (ch === '"') {
          if (line[i + 1] === '"') {
            current += '"';
            i++;
          } else {
            inQuotes = false;
          }
        } else {
          current += ch;
        }
      } else if (ch === '"') {
        inQuotes = true;
      } else if (ch === ",") {
        cells.push(current);
        current = "";
      } else {
        current += ch;
      }
    }
    cells.push(current);
    rows.push(cells);
  }
  return rows;
}

/** CSV 1 行を Observation に変換する（新潟県外は null）。 */
export function rowToObservation(
  element: AmedasElementValue,
  row: string[],
): Observation | null {
  if (row.length < 11) return null;
  const code = row[0].trim();
  const pref = row[1].trim();
  if (pref !== "新潟県" || !(code in NIIGATA_STATIONS)) return null;
  const station = NIIGATA_STATIONS[code];

  // 観測時刻（日本時間 JST=UTC+9 として解釈し UTC に変換）
  let observedAt = new Date();
  const y = Number(row[4]);
  const mo = Number(row[5]);
  const d = Number(row[6]);
  const h = Number(row[7]);
  const mi = Number(row[8]);
  if ([y, mo, d, h, mi].every((n) => Number.isInteger(n) && n > 0)) {
    const jst = new Date(Date.UTC(y, mo - 1, d, h, mi));
    if (!Number.isNaN(jst.getTime())) {
      observedAt = new Date(jst.getTime() - JST_OFFSET_MS);
    }
  }

  const valueRaw = row[9].trim();
  const qualityRaw = row[10].trim();
  const value = valueRaw ? Number(valueRaw) : null;
  const quality = qualityRaw ? Number(qualityRaw) : null;
  const valueOk = value !== null && !Number.isNaN(value);
  const qualityOk = quality !== null && Number.isInteger(quality);

  return {
    station,
    value: valueOk ? value : null,
    quality: qualityOk ? quality : null,
    qualityText: qualityOk ? QUALITY_CODES[quality] ?? "不明" : "不明",
    observedAt,
    source: SOURCE_TEXT,
  };
}
