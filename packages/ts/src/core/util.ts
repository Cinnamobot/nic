/**
 * 観光・オープンデータなどで共通利用する補助関数。
 */
import iconv from "iconv-lite";

/**
 * 全角文字を表示幅 2・半角を 1 として幅を計算する。
 */
export function displayWidth(text: string): number {
  let width = 0;
  for (const ch of text) {
    width += ch.codePointAt(0)! > 0x2e7f ? 2 : 1;
  }
  return width;
}

/**
 * 表示幅（全角=2）を考慮して右詰めパディングする。
 */
export function pad(text: string, width: number): string {
  return text + " ".repeat(Math.max(0, width - displayWidth(text)));
}

/**
 * 2 地点間の大円距離（km）。座標不明時は巨大値を返す。
 */
export function haversineKm(
  lat1: number,
  lon1: number,
  lat2: number | null,
  lon2: number | null,
): number {
  if (lat2 === null || lon2 === null) {
    return 1e9;
  }
  const r = 6371.0088; // 地球平均半径 km
  const p1 = toRadians(lat1);
  const p2 = toRadians(lat2);
  const dp = toRadians(lat2 - lat1);
  const dl = toRadians(lon2 - lon1);
  const a =
    Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * r * Math.asin(Math.sqrt(a));
}

function toRadians(deg: number): number {
  return (deg * Math.PI) / 180;
}

/**
 * 文字列の CSV セルをパースする（ダブルクォート・エスケープ対応の簡易版）。
 * 実データの CSV は引用符なしの列が多いため、行を単純分割しつつ
 * 引用符で囲まれたセル（カンマ・改行を含む）も正しく扱う。
 */
export function parseCsvLine(line: string): string[] {
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
  return cells;
}

/**
 * CSV テキストを行リストにパースする（空行・空白のみ行を除去）。
 */
export function parseCsvRows(text: string): string[][] {
  const rows: string[][] = [];
  for (const line of text.split(/\r\n|\n|\r/)) {
    const row = parseCsvLine(line);
    if (row.length > 0 && row.some((c) => c.trim() !== "")) {
      rows.push(row);
    }
  }
  return rows;
}

/**
 * CP932 / UTF-8 を自動判別してデコードする（オープンデータ・観光 CSV 用）。
 */
export function decodeText(raw: Uint8Array): string {
  // UTF-8 BOM 付きは先に判定（cp932 はほぼどんなバイト列でもデコードに成功してしまうため）
  if (raw.length >= 3 && raw[0] === 0xef && raw[1] === 0xbb && raw[2] === 0xbf) {
    return new TextDecoder("utf-8").decode(raw.subarray(3));
  }
  // 有効な UTF-8 なら UTF-8 としてデコード（cp932 誤判定の防止）
  if (isValidUtf8(raw)) {
    return new TextDecoder("utf-8").decode(raw);
  }
  return iconv.decode(Buffer.from(raw), "cp932");
}

/** バイト列が有効な UTF-8 か判定する。 */
function isValidUtf8(raw: Uint8Array): boolean {
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(raw);
    return true;
  } catch {
    return false;
  }
}

/**
 * CSV セルの数値文字列を number に変換する（空・不正値は null）。
 * カンマ区切りの桁区切り（例: "1,234"）も除去する。
 */
export function toNumber(value: string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const s = value.trim().replace(/,/g, "");
  if (!s) return null;
  const n = Number(s);
  return Number.isNaN(n) ? null : n;
}

/**
 * CSV セルの整数文字列を number に変換する（空・不正値は null）。
 */
export function toInt(value: string | null | undefined): number | null {
  const n = toNumber(value);
  return n === null ? null : Math.trunc(n);
}

/**
 * CSV セルの浮動小数点数文字列を number に変換する（空・不正値は null）。
 */
export function toFloat(value: string | null | undefined): number | null {
  return toNumber(value);
}

/**
 * 数値を表形式の文字列にする（3 桁区切り、null は "-"）。
 */
export function formatNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return "-";
  return n.toLocaleString("en-US");
}
