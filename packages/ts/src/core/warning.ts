/**
 * 気象庁防災情報XML（新潟県の警報・注意報）取得モジュール。
 *
 * 気象庁防災情報XML配信サービス（PULL型 Atom フィード）から、
 * 新潟県（府県コード 150000）の気象特別警報・警報・注意報電文（VPWW53）を
 * 取得・パースし、**府県 / 一次細分区域 / 市町村等をまとめた地域 / 市町村** の
 * 4 階層それぞれについて、警報・注意報の種別・状態（発表/継続/解除）・
 * 対象地域を返す。
 *
 * データ源（2 段構成）:
 *   1. 高頻度フィード（毎分更新・直近10分以上の入電）
 *      https://www.data.jma.go.jp/developer/xml/feed/extra.xml
 *   2. 電文 XML（フィード内の <entry> から URL を取得してダウンロード）
 *      https://www.data.jma.go.jp/developer/xml/data/YYYYMMDDhhmmss_連番_VPWW53_150000.xml
 *
 * 利用条件: 気象庁防災情報XML配信（https://www.data.jma.go.jp/developer/xml/feed/）
 * 公共データ利用規約 第1.0版（https://www.jma.go.jp/jma/kishou/info/coment.html）
 * 出典表示必須。加工したことを明示すること。1日10GB以上のダウンロードはアクセス遮断。
 */

import { XMLParser } from "fast-xml-parser";

export const SOURCE_TEXT = "出典:気象庁";
export const SOURCE_URL = "https://www.data.jma.go.jp/developer/xml/feed/extra.xml";

export const NIIGATA_PREF_CODE = "150000";
export const EXTRA_FEED_URL = "https://www.data.jma.go.jp/developer/xml/feed/extra.xml";

/** 警報・注意報を含む随時発表フィード（毎分更新・直近10分以上）。 */
export const DEFAULT_TTL = 60.0;

export const USER_AGENT = "nic/0.1 (+https://github.com/Cinnamobot/nic)";

/** 状態（<Status>）の正規化 */
export const STATUS_NONE = "発表警報・注意報はなし";
const STATUS_MAP: Record<string, string> = {
  発表: "発表",
  継続: "継続",
  解除: "解除",
  特別警報から警報に切り替え: "特別警報から警報に切り替え",
  警報から注意報に切り替え: "警報から注意報に切り替え",
  注意報解除: "解除",
};

/** Warning type 属性 → 階層 */
const LEVEL_LABELS: Record<string, string> = {
  "気象警報・注意報（府県予報区等）": "府県",
  "気象警報・注意報（一次細分区域等）": "一次細分",
  "気象警報・注意報（市町村等をまとめた地域等）": "地域",
  "気象警報・注意報（市町村等）": "市町村",
};

export class WarningError extends Error {}
export class WarningFetchError extends WarningError {}
export class WarningParseError extends WarningError {}
export class WarningNotFoundError extends WarningError {}

/** 警報・注意報の 1 種別。 */
export interface WarningKind {
  name: string; // 種別名 (例: "大雨注意報")
  code: string; // 気象コード (例: "10")
  status: string; // 状態（"発表" / "継続" / "解除" / "発表警報・注意報はなし"）
}

/** 警報・注意報の対象地域。 */
export interface WarningArea {
  name: string; // 地域名 (例: "中越" / "十日町市")
  code: string; // エリアコード (例: "150020" / "1521000")
  kinds: WarningKind[];
}

/** 府県/一次細分/地域/市町村の 1 階層。 */
export interface WarningLevel {
  level: string; // "府県" / "一次細分" / "地域" / "市町村"
  typeLabel: string; // Warning type 属性の原文
  areas: WarningArea[];
}

/** 新潟県の警報・注意報 1 電文分の取得結果。 */
export interface WarningData {
  title: string;
  headline: string;
  infoType: string;
  reportDatetime: Date; // JST タイムゾーン付き
  editorialOffice: string;
  messageKind: string; // 電文種別（"VPWW53" など）
  messageUrl: string;
  levels: WarningLevel[]; // 府県 → 一次細分 → 地域 → 市町村 の順
  fetchedAt: Date;
  source: string;
  sourceUrl: string;
}

interface CacheEntry {
  data: WarningData;
  expiresAt: number;
}

/** 気象庁防災情報XML（新潟県の警報・注意報）取得クライアント。 */
export class WarningClient {
  ttl: number;
  timeout: number;
  private cache = new Map<string, CacheEntry>();

  constructor(options: { ttl?: number; timeout?: number } = {}) {
    this.ttl = options.ttl ?? DEFAULT_TTL;
    this.timeout = options.timeout ?? 15.0;
  }

  /** 新潟県の最新の警報・注意報を取得する。 */
  async fetch(options: { force?: boolean; feedUrl?: string } = {}): Promise<WarningData> {
    const force = options.force ?? false;
    const feedUrl = options.feedUrl ?? EXTRA_FEED_URL;
    let data = this.getCached(force);
    if (data === null) {
      data = await this.fetchAndParse(feedUrl);
      this.putCache(data);
    }
    return data;
  }

  async fetchPrefecture(options: { force?: boolean } = {}): Promise<WarningData> {
    return this.fetch(options);
  }

  async listLevels(options: { force?: boolean } = {}): Promise<WarningLevel[]> {
    return (await this.fetch(options)).levels;
  }

  private getCached(force: boolean): WarningData | null {
    if (force) return null;
    const entry = this.cache.get("warning");
    if (entry && entry.expiresAt > Date.now()) return entry.data;
    return null;
  }

  private putCache(data: WarningData): void {
    this.cache.set("warning", { data, expiresAt: Date.now() + this.ttl * 1000 });
  }

  private async download(url: string): Promise<Uint8Array> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout * 1000);
    try {
      const resp = await fetch(url, {
        headers: { "User-Agent": USER_AGENT, Accept: "application/xml,text/xml,*/*" },
        signal: controller.signal,
        redirect: "follow",
      });
      if (resp.status !== 200) {
        throw new WarningFetchError(`HTTP ${resp.status} で取得できませんでした: ${url}`);
      }
      return new Uint8Array(await resp.arrayBuffer());
    } catch (e) {
      if (e instanceof WarningError) throw e;
      throw new WarningFetchError(`HTTP 取得に失敗しました: ${url} (${(e as Error).message})`);
    } finally {
      clearTimeout(timer);
    }
  }

  private async fetchAndParse(feedUrl: string): Promise<WarningData> {
    const feedRaw = await this.download(feedUrl);
    const messageUrl = findNiigataMessageUrl(feedRaw, feedUrl);
    if (messageUrl === null) {
      throw new WarningNotFoundError(
        `フィード ${feedUrl} に新潟県（${NIIGATA_PREF_CODE}）の警報・注意報電文（VPWW53/VPWW54）が見つかりませんでした`,
      );
    }
    // SSRF 対策: 電文 XML の取得先を気象庁公式ドメインに限定する
    let parsed: URL;
    try {
      parsed = new URL(messageUrl);
    } catch {
      throw new WarningFetchError(`電文 XML の URL が不正です: ${messageUrl}`);
    }
    if (!parsed.protocol.startsWith("http") || !parsed.hostname.endsWith(".data.jma.go.jp")) {
      throw new WarningFetchError(
        `電文 XML の取得先が気象庁ドメインではありません: ${messageUrl}`,
      );
    }
    const messageRaw = await this.download(messageUrl);
    return parseWarningXml(messageRaw, messageUrl);
  }
}

/** 新潟県の最新の警報・注意報を 1 コールで取得する（キャッシュ付き）。 */
export async function getNiigataWarnings(options: {
  ttl?: number;
  timeout?: number;
  force?: boolean;
} = {}): Promise<WarningData> {
  const client = new WarningClient({ ttl: options.ttl, timeout: options.timeout });
  try {
    return await client.fetch({ force: options.force });
  } finally {
    // キャッシュはクライアントごとに保持されるため、シンプルな呼び出し用は
    // 使い捨てクライアントで取得する。
  }
}

// ---------------------------------------------------------------------------
// XML パース（fast-xml-parser）
// ---------------------------------------------------------------------------

const NS_A = "http://www.w3.org/2005/Atom";
const NS_J = "http://xml.kishou.go.jp/jmaxml1/";
const NS_H = "http://xml.kishou.go.jp/jmaxml1/informationBasis1/";
const NS_M = "http://xml.kishou.go.jp/jmaxml1/body/meteorology1/";

/** fast-xml-parser のタグ名を短縮名に正規化する。 */
function normalizeTag(tag: string): string {
  // fast-xml-parser は属性付きタグを "tagName@_attr" の形で返す場合がある
  const base = tag.split("@")[0];
  // 名前空間プレフィックス（a: / j: / h: / m:）を取り除いたローカル名を返す
  const colon = base.lastIndexOf(":");
  return colon >= 0 ? base.slice(colon + 1) : base;
}

function parseXml(raw: Uint8Array | string, label: string): Record<string, unknown> {
  const parser = new XMLParser({
    ignoreAttributes: false,
    attributeNamePrefix: "@_",
    removeNSPrefix: false,
    parseTagValue: false,
    parseAttributeValue: false,
    trimValues: true,
    isArray: (name) => name === "entry" || name === "Item" || name === "Kind" || name === "Warning",
  });
  let text: string;
  if (raw instanceof Uint8Array) {
    text = new TextDecoder("utf-8").decode(raw);
  } else {
    text = raw;
  }
  let parsed: unknown;
  try {
    parsed = parser.parse(text);
  } catch (e) {
    throw new WarningParseError(`${label} XML のパースに失敗しました: ${(e as Error).message}`);
  }
  if (typeof parsed !== "object" || parsed === null) {
    throw new WarningParseError(`${label} XML のパース結果が不正です`);
  }
  return parsed as Record<string, unknown>;
}

/** ルート要素名の短縮名を返す（なければ null）。XML 宣言（?xml）はスキップする。 */
function rootTag(root: Record<string, unknown>): string | null {
  const keys = Object.keys(root).filter((k) => !k.startsWith("?"));
  return keys.length > 0 ? normalizeTag(keys[0]) : null;
}

/** オブジェクトの子要素をタグ名（短縮名・プレフィックス付きの両対応）で探す。 */
function findChild(
  obj: Record<string, unknown> | undefined | null,
  nsTag: string,
): Record<string, unknown> | null {
  if (!obj) return null;
  const wanted = normalizeTag(nsTag);
  for (const [key, value] of Object.entries(obj)) {
    if (normalizeTag(key) === wanted && typeof value === "object" && value !== null) {
      return value as Record<string, unknown>;
    }
  }
  return null;
}

function findChildren(
  obj: Record<string, unknown> | undefined | null,
  nsTag: string,
): Record<string, unknown>[] {
  if (!obj) return [];
  const wanted = normalizeTag(nsTag);
  const result: Record<string, unknown>[] = [];
  for (const [key, value] of Object.entries(obj)) {
    if (normalizeTag(key) !== wanted) continue;
    if (Array.isArray(value)) {
      for (const v of value) {
        if (typeof v === "object" && v !== null) result.push(v as Record<string, unknown>);
      }
    } else if (typeof value === "object" && value !== null) {
      result.push(value as Record<string, unknown>);
    }
  }
  return result;
}

/** 子要素のテキストを返す（なければ null）。パスは "/" 区切りで指定できる。 */
function textOf(
  obj: Record<string, unknown> | undefined | null,
  nsTag: string,
): string | null {
  let current: Record<string, unknown> | null | undefined = obj;
  const parts = nsTag.split("/");
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i];
    const last = i === parts.length - 1;
    if (current === null || current === undefined) return null;
    const wanted = normalizeTag(part);
    // 最終要素で、値が文字列の場合は直接返す（Atom title など）
    if (last) {
      for (const [key, value] of Object.entries(current)) {
        if (normalizeTag(key) === wanted) {
          if (typeof value === "string") return value.trim();
          if (typeof value === "object" && value !== null) {
            const inner = (value as Record<string, unknown>)["#text"];
            if (typeof inner === "string") return inner.trim();
          }
          return null;
        }
      }
      return null;
    }
    current = findChild(current, part);
  }
  return null;
}

/** 属性値を返す（なければ null）。 */
function attrOf(
  obj: Record<string, unknown> | undefined | null,
  name: string,
): string | null {
  if (!obj) return null;
  const value = obj[`@_${name}`];
  if (typeof value === "string") return value;
  return null;
}

function parseDateTime(raw: string): Date | null {
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 電文タイトルから電文種別（VPWW53/VPWW54 等）を推定する。 */
function detectMessageKind(title: string): string {
  if (title.includes("特別警報")) return "VPWW53";
  if (title.includes("警報・注意報") || title.includes("警報")) return "VPWW54";
  return "";
}

/** <Status> のテキストを正規化する。 */
function normalizeStatus(status: string): string {
  const s = status.trim();
  if (!s) return STATUS_NONE;
  return STATUS_MAP[s] ?? s;
}

// ---------------------------------------------------------------------------
// フィードパース
// ---------------------------------------------------------------------------

/** Atom フィードから新潟県の最新の警報・注意報電文 URL を探す。 */
export function findNiigataMessageUrl(
  feedXml: Uint8Array | string,
  baseUrl = EXTRA_FEED_URL,
): string | null {
  const root = parseXml(feedXml, "フィード");
  const feed = findChild(root, "a:feed");
  if (feed === null) {
    throw new WarningParseError("フィードのルート要素が <feed> ではありません");
  }
  const entries = findChildren(feed, "a:entry");
  for (const entry of entries) {
    const title = textOf(entry, "a:title") ?? "";
    const linkEl = findChild(entry, "a:link");
    if (linkEl === null) continue;
    const href = attrOf(linkEl, "href");
    if (!href) continue;
    // 電文種別（タイトル）で判別: 気象特別警報・警報・注意報 (VPWW53) / 気象警報・注意報 (VPWW54)
    const kindMatched = ["VPWW53", "VPWW54"].some(
      (k) => title.includes(k) || href.includes(k),
    );
    if (!kindMatched) continue;
    // 府県コード（ファイル名の末尾）で新潟県を判定
    if (!href.replace(/\/+$/, "").endsWith(`_${NIIGATA_PREF_CODE}.xml`)) continue;
    return new URL(href, baseUrl).toString();
  }
  return null;
}

/** フィード中の新潟県の警報・注意報電文 URL を新しい順に列挙する。 */
export function listMessageUrls(
  feedXml: Uint8Array | string,
  baseUrl = EXTRA_FEED_URL,
  limit = 20,
): string[] {
  const root = parseXml(feedXml, "フィード");
  const feed = findChild(root, "a:feed");
  if (feed === null) {
    throw new WarningParseError("フィードのルート要素が <feed> ではありません");
  }
  const urls: string[] = [];
  for (const entry of findChildren(feed, "a:entry")) {
    const title = textOf(entry, "a:title") ?? "";
    const linkEl = findChild(entry, "a:link");
    if (linkEl === null) continue;
    const href = attrOf(linkEl, "href");
    if (!href) continue;
    const kindMatched = ["VPWW53", "VPWW54"].some(
      (k) => title.includes(k) || href.includes(k),
    );
    if (!kindMatched || !href.replace(/\/+$/, "").endsWith(`_${NIIGATA_PREF_CODE}.xml`)) {
      continue;
    }
    urls.push(new URL(href, baseUrl).toString());
    if (urls.length >= limit) break;
  }
  return urls;
}

// ---------------------------------------------------------------------------
// 電文 XML パース
// ---------------------------------------------------------------------------

/** VPWW53/VPWW54 電文 XML をパースして 4 階層の警報・注意報を返す。 */
export function parseWarningXml(
  raw: Uint8Array | string,
  messageUrl = "",
): WarningData {
  const root = parseXml(raw, "電文");
  // ルート要素（Report）を取得（fast-xml-parser は名前空間をローカル名に短縮する）
  const report = findChild(root, "j:Report") ?? findChild(root, "Report") ?? root;

  // -- 発信情報（Control） --
  const control = findChild(report, "j:Control") ?? {};
  const controlTitle = textOf(control, "j:Title") ?? "";
  const editorial = textOf(control, "j:EditorialOffice") ?? "";

  // -- 見出し情報（Head） --
  const head = findChild(report, "h:Head");
  const headline = textOf(head, "h:Headline/h:Text") ?? "";
  const infoType = textOf(head, "h:InfoType") ?? "";
  const rawDt = textOf(head, "h:ReportDateTime") ?? "";
  const reportDt = parseDateTime(rawDt);
  const title = textOf(head, "h:Title") ?? controlTitle;

  // -- 警報・注意報本体（Body/Warning） --
  const body = findChild(report, "m:Body");
  const levels: WarningLevel[] = [];
  for (const warning of findChildren(body, "m:Warning")) {
    const typeLabel = attrOf(warning, "type") ?? "";
    const levelName = LEVEL_LABELS[typeLabel] ?? typeLabel;
    const areas: WarningArea[] = [];
    for (const item of findChildren(warning, "m:Item")) {
      const area = findChild(item, "m:Area");
      if (area === null) continue;
      const name = textOf(area, "m:Name") ?? "";
      const code = textOf(area, "m:Code") ?? "";
      const kinds: WarningKind[] = [];
      for (const kind of findChildren(item, "m:Kind")) {
        kinds.push({
          name: textOf(kind, "m:Name") ?? "",
          code: textOf(kind, "m:Code") ?? "",
          status: normalizeStatus(textOf(kind, "m:Status") ?? ""),
        });
      }
      areas.push({ name, code, kinds });
    }
    levels.push({ level: levelName, typeLabel, areas });
  }

  // Warning セクションが無い（電文の形式が想定外）場合はエラーにする
  if (levels.length === 0) {
    throw new WarningParseError(
      "電文 XML に <Warning> セクションが見つかりませんでした（VPWW53/VPWW54 以外の可能性）",
    );
  }

  return {
    title,
    headline,
    infoType,
    reportDatetime: reportDt ?? new Date(),
    editorialOffice: editorial,
    messageKind: detectMessageKind(controlTitle),
    messageUrl,
    levels,
    fetchedAt: new Date(),
    source: SOURCE_TEXT,
    sourceUrl: SOURCE_URL,
  };
}

// ---------------------------------------------------------------------------
// WarningData のアクセサ（Python 版のプロパティ相当）
// ---------------------------------------------------------------------------

/** 指定階層のエリア一覧を返す（存在しなければ空配列）。 */
export function getAreas(data: WarningData, levelName: string): WarningArea[] {
  const lv = data.levels.find((l) => l.level === levelName);
  return lv ? lv.areas : [];
}

/** 指定階層で警報・注意報が発表されているエリアのみ返す。 */
export function getActiveAreas(data: WarningData, levelName: string): WarningArea[] {
  return getAreas(data, levelName).filter((a) => hasWarning(a));
}

/** この地域で 1 件以上発表（継続・解除含む）されているか。 */
export function hasWarning(area: WarningArea): boolean {
  return area.kinds.some((k) => k.status !== STATUS_NONE);
}

/** この地域の状態サマリ（例: "大雨注意報 継続" / "発表警報・注意報はなし"）。 */
export function statusSummary(area: WarningArea): string {
  const active = area.kinds.filter((k) => k.status !== STATUS_NONE);
  if (active.length === 0) return STATUS_NONE;
  return active.map((k) => `${k.name} ${k.status}`).join("、");
}

/** 府県階層で発表されている種別の一覧（重複なし・出現順）。 */
export function activeKinds(data: WarningData): WarningKind[] {
  const pref = data.levels.find((l) => l.level === "府県");
  if (!pref) return [];
  const seen = new Set<string>();
  const result: WarningKind[] = [];
  for (const area of pref.areas) {
    for (const k of area.kinds) {
      if (k.status === STATUS_NONE) continue;
      const key = `${k.code}|${k.status}`;
      if (seen.has(key)) continue;
      seen.add(key);
      result.push(k);
    }
  }
  return result;
}

/** 人間向けサマリ（例: "大雨注意報 継続、雷注意報 継続"）。 */
export function summary(data: WarningData): string {
  const kinds = activeKinds(data);
  if (kinds.length === 0) return STATUS_NONE;
  return kinds.map((k) => `${k.name} ${k.status}`).join("、");
}
