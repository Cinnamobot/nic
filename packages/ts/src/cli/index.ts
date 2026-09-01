#!/usr/bin/env node
/**
 * nic — 新潟県の情報（気象・河川・観光・交通・統計など）にアクセスする CLI ツール。
 *
 * サブコマンド:
 *   snow     積雪情報（ランキング・観測所指定）
 *   weather  気温・天気情報
 *   tour     観光情報（スポット・天気×おすすめ・温泉・入込客数）
 *   warning  警報・注意報一覧（府県/一次細分/地域/市町村別）
 *   stats    統計・オープンデータ
 *   search   全データ横断検索
 *
 * 共通オプション:
 *   --json   出力を JSON にする（表形式の代わり）
 *   --force  キャッシュを無視して再取得
 */

import { Command, Option } from "commander";
import { displayWidth, haversineKm, pad } from "../core/util.js";
import { AmedasClient, AmedasError, AmedasStationNotFoundError, NIIGATA_STATIONS, SOURCE_TEXT as AMEDAS_SOURCE, SOURCE_URL as AMEDAS_SOURCE_URL, type AmedasData, type Observation } from "../core/amedas.js";
import { OpenDataClient, OpenDataError, type Dataset, type PopulationRecord } from "../core/opendata.js";
import { TourismClient, TourismError, SOURCE_TEXT as TOURISM_SOURCE, SOURCE_URL as TOURISM_SOURCE_URL, type Spot, type TourismStat } from "../core/tourism.js";
import { WarningClient, WarningError, SOURCE_TEXT as WARNING_SOURCE, type WarningArea, type WarningData } from "../core/warning.js";
import { getAreas, hasWarning, statusSummary, summary as warningSummary } from "../core/warning.js";

import pkg from "../../package.json" with { type: "json" };

export const VERSION: string = pkg.version;

/** 表形式の最大表示件数（--limit 未指定時の既定値） */
const DEFAULT_LIMIT = 20;

/** アメダス / オープンデータ共通のキャッシュ TTL（秒） */
const CLI_TTL = 300.0;

// 気象要素表示名（表・JSON の要素名として利用）
const ELEMENT_LABELS: Record<string, string> = {
  snow: "積雪",
  precipitation: "1時間降水量",
  max_temp: "最高気温",
  min_temp: "最低気温",
};

/** 観測所名 → コード の対応表（--station の名前解決用） */
const STATION_NAME_TO_CODE: Record<string, string> = Object.fromEntries(
  Object.values(NIIGATA_STATIONS).map((s) => [s.name, s.code]),
);

interface CommonFlags {
  json: boolean;
  force: boolean;
}

let COMMON: CommonFlags = { json: false, force: false };

/** 出力ストリームのエンコーディングを UTF-8 に固定する（Windows 文字化け対策）。 */
function reconfigureStdioUtf8(): void {
  // Node.js の stdout/stderr は通常 UTF-8。Windows の cp932 コンソール向けに
  // process.stdout のエンコーディングを明示する（可能な場合のみ）。
  try {
    process.stdout.setDefaultEncoding("utf-8");
    process.stderr.setDefaultEncoding("utf-8");
  } catch {
    // リダイレクト時など設定不能な環境では無視
  }
}

reconfigureStdioUtf8();

// ---------------------------------------------------------------------------
// 出力ヘルパー
// ---------------------------------------------------------------------------

function emitJson(payload: unknown): void {
  process.stdout.write(JSON.stringify(payload, (key, value) => {
    if (value instanceof Date) return value.toISOString();
    return value;
  }, 2) + "\n");
}

/** 観測日時を UTC ISO8601 文字列に変換する（JSON 出力用。UTC のまま）。 */
function formatObservedAt(dt: Date): string {
  return dt.toISOString();
}

/** 観測日時を JST（Asia/Tokyo = UTC+9）ISO8601 文字列に変換する（表出力用）。 */
function formatObservedAtJst(dt: Date): string {
  const jst = new Date(dt.getTime() + 9 * 60 * 60 * 1000);
  // ISO8601 形式で +09:00 オフセットを付与
  return jst.toISOString().replace("Z", "+09:00");
}

/** ヘッダ + 行の表をターミナルで読みやすい形式で出力する。 */
function renderTable(headers: string[], rows: string[][]): void {
  if (rows.length === 0) {
    process.stdout.write("（該当データがありません）\n");
    return;
  }
  const widths = headers.map((h) => displayWidth(h));
  for (const row of rows) {
    row.forEach((cell, i) => {
      if (i < widths.length) widths[i] = Math.max(widths[i], displayWidth(cell));
    });
  }
  process.stdout.write(headers.map((h, i) => pad(h, widths[i])).join("  ") + "\n");
  process.stdout.write(widths.map((w) => "-".repeat(w)).join("  ") + "\n");
  for (const row of rows) {
    process.stdout.write(
      row.map((cell, i) => pad(cell, widths[i])).join("  ").replace(/\s+$/, "") + "\n",
    );
  }
}

function printWarnings(warnings: string[]): void {
  for (const w of warnings) {
    process.stderr.write(`注: ${w}\n`);
  }
}

// ---------------------------------------------------------------------------
// 観測所の解決
// ---------------------------------------------------------------------------

function resolveStation(token: string): string {
  if (token in NIIGATA_STATIONS) return token;
  if (/^\d+$/.test(token)) {
    process.stderr.write(`エラー: 新潟県内に観測所番号 ${token} は存在しません。\n`);
    process.stderr.write("ヒント: ngt search で観測所名からコードを確認できます（例: ngt search 長岡）。\n");
    process.exit(2);
  }
  const code = STATION_NAME_TO_CODE[token];
  if (code) return code;
  process.stderr.write(`エラー: 観測所「${token}」は見つかりません（観測所コードまたは観測所名で指定してください）。\n`);
  process.stderr.write("ヒント: ngt search で観測所名からコードを確認できます（例: ngt search 長岡）。\n");
  process.exit(2);
}

function splitStations(station: string | undefined): string[] | null {
  if (!station) return null;
  const tokens = station
    .replace(/ /g, ",")
    .split(",")
    .map((t) => t.trim())
    .filter((t) => t !== "");
  const codes = tokens.map(resolveStation);
  return codes.length > 0 ? codes : null;
}

// ---------------------------------------------------------------------------
// データ変換ヘルパー（JSON 用）
// ---------------------------------------------------------------------------

function observationJson(obs: Observation): Record<string, unknown> {
  return {
    station_code: obs.station.code,
    station_name: obs.station.name,
    value: obs.value,
    quality: obs.quality,
    quality_text: obs.qualityText,
    observed_at: formatObservedAt(obs.observedAt),
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

function michinoekiJson(st: { id: number; name: string; route: string; address: string; phone: string }): Record<string, unknown> {
  return {
    id: st.id,
    name: st.name,
    route: st.route,
    address: st.address,
    phone: st.phone,
  };
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

// ---------------------------------------------------------------------------
// tour: 観光情報
// ---------------------------------------------------------------------------

// 天気×おすすめ: 観測所名 → 推奨カテゴリ名（雨の日の屋内・温泉向きの分類）
const WEATHER_RECOMMENDATION: Record<string, string> = {
  湯沢: "温泉", 塩沢: "温泉", 十日町: "温泉", 津南: "温泉", 安塚: "温泉", 松代: "温泉",
  大湯: "温泉", 糸魚川: "温泉", 能生: "温泉", 平岩: "温泉", 関山: "温泉", 守門: "温泉",
  小出: "温泉", 小国: "温泉", 栃尾: "温泉", 樽本: "温泉", 赤谷: "温泉", 津川: "温泉",
  室谷: "温泉", 高根: "温泉", 三面: "温泉", 下関: "温泉", 宮寄上: "温泉", 筒方: "温泉",
  川谷: "温泉",
  新潟: "集客施設", 新津: "集客施設", 巻: "集客施設", 三条: "集客施設", 村松: "集客施設",
  中条: "集客施設", 長岡: "集客施設", 高田: "集客施設", 大潟: "集客施設", 村上: "集客施設",
  松浜: "集客施設", 瓢湖: "集客施設", 弾崎: "集客施設", 羽茂: "集客施設", 相川: "集客施設",
  両津: "集客施設", 粟島: "集客施設", 寺泊: "集客施設", 柏崎: "集客施設",
};

// 推奨スポットを「雨が降っている観測所から半径何 km 以内」に限定するか
const RECOMMEND_RADIUS_KM = 40.0;

// 推奨カテゴリ → スポットのマッチングキーワード（区分の前方一致）
const TOUR_RECOMMEND_KEYWORDS: Record<string, string[]> = {
  温泉: ["温泉"],
  集客施設: ["集客施設", "映画館", "劇場", "公会堂", "展示場", "体育館"],
};

function recommendedSpots(spots: Spot[], kinds: string[]): Spot[] {
  return spots.filter((s) => kinds.some((kw) => s.category.startsWith(kw)));
}

// 観光入込客数の分類名（表示用ラベル）
const IRIKOMI_COLUMN_LABELS: Record<string, string> = {
  total: "観光入込客数合計",
  event_total: "行祭事・イベント合計",
  spot_total: "観光地点合計",
  nature: "自然",
  history_culture: "歴史・文化",
  onsen_health: "温泉・健康",
  sports_recreation: "スポーツ・レクリエーション",
  urban_tourism: "都市型観光",
  other: "その他",
};

// 観測所名 → 観測所コード（--station の名前解決用）
// 天気×おすすめ: 観測所名 → 推奨カテゴリ名（雨の日の屋内・温泉向きの分類）

function tourWarnings(client: TourismClient): void {
  printWarnings(client.warnings);
}

// ---------------------------------------------------------------------------
// warning: 警報・注意報
// ---------------------------------------------------------------------------

const WARNING_LEVEL_CHOICES = ["府県", "一次細分", "地域", "市町村"] as const;

// 警報・注意報の表示順（種別の重複表示を避けるための並び替え基準）
const WARNING_KIND_ORDER: string[] = [
  "特別警報", "暴風", "大雨", "洪水", "高潮", "波浪", "大雪", "暴風雪",
  "雷", "融雪", "濃霧", "乾燥", "なだれ", "低温", "霜", "着氷", "着雪",
];

function warningAreaRows(areas: WarningArea[]): string[][] {
  return areas.map((a) => {
    if (hasWarning(a)) {
      const kinds = a.kinds
        .filter((k) => k.status !== "発表警報・注意報はなし")
        .map((k) => `${k.name} ${k.status}`)
        .join("、");
      return [a.name, a.code, kinds];
    }
    return [a.name, a.code, statusSummary(a)];
  });
}

function warningKindRows(areas: WarningArea[]): string[][] {
  const seen = new Set<string>();
  const rows: string[][] = [];
  for (const a of areas) {
    for (const k of a.kinds) {
      if (k.status === "発表警報・注意報はなし") continue;
      const key = `${k.name}|${k.status}`;
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push([k.name, k.status, a.name]);
    }
  }
  return rows;
}

function warningFallbackSummary(data: WarningData): string {
  return warningSummary(data);
}

// ---------------------------------------------------------------------------
// snow: 積雪情報
// ---------------------------------------------------------------------------

function sortObservations(data: AmedasData): Observation[] {
  return data.observations
    .filter((o) => o.value !== null)
    .sort((a, b) => (b.value ?? 0) - (a.value ?? 0));
}

// ---------------------------------------------------------------------------
// コマンド定義
// ---------------------------------------------------------------------------

export function buildProgram(): Command {
  const program = new Command();

  program
    .name("ngt")
    .description("新潟県の情報（気象・河川・観光・交通・統計など）にアクセスする CLI ツール。")
    .version(VERSION, "-v, --version", "バージョン情報を表示する。");

  // 共通オプションをグローバルに設定
  program
    .option("--json", "出力を JSON 形式にする。")
    .option("--force", "キャッシュを無視して再取得する。");

  const commonFlags = (
    globalOpts: { json?: boolean; force?: boolean },
    localOpts: { json?: boolean; force?: boolean },
  ): CommonFlags => {
    return {
      json: Boolean(globalOpts.json) || Boolean(localOpts.json),
      force: Boolean(globalOpts.force) || Boolean(localOpts.force),
    };
  };
  const globalOpts = (): { json?: boolean; force?: boolean } =>
    program.opts<{ json?: boolean; force?: boolean }>();

  // ---- mcp: MCP サーバー起動 ----------------------------------------------
  program
    .command("mcp")
    .description("MCP サーバーを stdio で起動する（nic-mcp と同じ）。")
    .action(async () => {
      const { main: mcpMain } = await import("../mcp/index.js");
      await mcpMain();
    });

  // ---- tour: 観光情報 -----------------------------------------------------
  program
    .command("tour")
    .description("観光情報を表示する（スポット・天気×おすすめ・温泉・入込客数）。")
    .option("--spots", "観光スポット一覧を表示する（温泉 + 集客施設）。")
    .option("--onsen", "温泉スポット一覧を表示する。")
    .option("--category <category>", "スポットの区分で絞り込み（例: 温泉, 集客施設）。")
    .option("--weather", "観光スポットと気象情報（アメダス）を組み合わせて表示する。")
    .option("--recommend", "天気に合わせたおすすめスポットを表示する（--weather と同時指定）。")
    .option("--irikomi", "観光入込客数（年別・分類別）を表示する。")
    .option("--year <year>", "入込客数の年（例: 2024）。未指定は全件。", (v) => Number(v))
    .addOption(new Option("-n, --limit <n>", "表示件数。").default(DEFAULT_LIMIT).argParser((v) => {
      const n = Number(v);
      if (!Number.isInteger(n) || n < 1 || n > 200) {
        process.stderr.write("エラー: --limit は 1〜200 の整数で指定してください\n");
        process.exit(2);
      }
      return n;
    }))
    .option("--json", "出力を JSON 形式にする。")
    .option("--force", "キャッシュを無視して再取得する。")
    .action(async (opts) => {
      COMMON = commonFlags(globalOpts(), opts);
      const flags = [opts.spots, opts.onsen, opts.weather, opts.irikomi].filter(Boolean);
      if (flags.length > 1) {
        process.stderr.write("エラー: --spots / --onsen / --weather / --irikomi は同時に指定できません\n");
        process.exit(2);
      }
      if (opts.recommend && !opts.weather) {
        process.stderr.write("エラー: --recommend は --weather と同時に指定してください\n");
        process.exit(2);
      }
      if (opts.year !== undefined && !opts.irikomi) {
        process.stderr.write("エラー: --year は --irikomi と同時に指定してください\n");
        process.exit(2);
      }
      if (opts.irikomi) {
        await tourIrikomi(opts.year, opts.limit);
        return;
      }
      if (opts.onsen) {
        await tourOnsen(opts.limit);
        return;
      }
      if (opts.weather) {
        await tourWeather(opts.recommend, opts.limit);
        return;
      }
      await tourSpots(opts.category, opts.limit);
    });

  // ---- warning: 警報・注意報 ---------------------------------------------
  program
    .command("warning")
    .alias("warn")
    .description("新潟県の警報・注意報一覧を表示する（気象庁防災情報XML）。")
    .option("-l, --level <level>", "表示する階層（府県 / 一次細分 / 地域 / 市町村）。未指定は府県。")
    .option("-a, --area <area>", "地域名で絞り込み（例: 中越, 十日町市）。部分一致。")
    .option("--json", "出力を JSON 形式にする。")
    .option("--force", "キャッシュを無視して再取得する。")
    .action(async (opts) => {
      COMMON = commonFlags(globalOpts(), opts);
      const levelName = opts.level ?? "府県";
      if (!WARNING_LEVEL_CHOICES.includes(levelName)) {
        process.stderr.write(`エラー: --level は ${WARNING_LEVEL_CHOICES.join(", ")} のいずれかを指定してください\n`);
        process.exit(2);
      }
      const client = new WarningClient({ ttl: 60.0 });
      try {
        const data = await client.fetch({ force: COMMON.force });
        let areas = getAreas(data, levelName);
        if (opts.area) {
          areas = areas.filter((a) => a.name.includes(opts.area));
        }
        if (COMMON.json) {
          emitJson({
            type: "warning",
            level: levelName,
            title: data.title,
            headline: data.headline,
            info_type: data.infoType,
            report_datetime: formatObservedAt(data.reportDatetime),
            editorial_office: data.editorialOffice,
            summary: warningFallbackSummary(data),
            areas: areas.map((a) => ({
              name: a.name,
              code: a.code,
              status: statusSummary(a),
              active: hasWarning(a),
            })),
            source: WARNING_SOURCE,
            source_url: data.sourceUrl,
            message_url: data.messageUrl,
          });
          return;
        }
        process.stdout.write(`警報・注意報一覧（${levelName}階層、${WARNING_SOURCE.replace("出典:", "")}）\n`);
        process.stdout.write(`発表日時: ${formatObservedAtJst(data.reportDatetime)} / ${data.editorialOffice}\n`);
        if (data.headline) process.stdout.write(`${data.headline}\n`);
        if (areas.length > 0) {
          renderTable(["地域名", "コード", "警報・注意報"], warningAreaRows(areas));
        } else {
          process.stdout.write("（該当データがありません）\n");
        }
        process.stdout.write(`${WARNING_SOURCE}（公共データ利用規約 第1.0版）\n`);
      } catch (e) {
        if (e instanceof WarningError) {
          process.stderr.write(`エラー: ${e.message}\n`);
          process.stderr.write("ヒント: データ源（気象庁防災情報XML）が一時的に利用できない可能性があります。時間をおいて再試行してください。\n");
          process.exit(1);
        }
        throw e;
      }
    });

  // ---- snow: 積雪情報 -----------------------------------------------------
  program
    .command("snow")
    .description("積雪情報を表示する（気象庁アメダス）。")
    .option("--rank", "積雪の多い順のランキングを表示する。")
    .option("-s, --station <station>", "観測所コードまたは観測所名（例: 54841, 湯沢）。カンマ・空白区切りで複数指定可。")
    .addOption(new Option("-n, --limit <n>", "表示件数（ランキング時の上限）。").default(DEFAULT_LIMIT).argParser((v) => {
      const n = Number(v);
      if (!Number.isInteger(n) || n < 1 || n > 100) {
        process.stderr.write("エラー: --limit は 1〜100 の整数で指定してください\n");
        process.exit(2);
      }
      return n;
    }))
    .option("--json", "出力を JSON 形式にする。")
    .option("--force", "キャッシュを無視して再取得する。")
    .action(async (opts) => {
      COMMON = commonFlags(globalOpts(), opts);
      const client = new AmedasClient({ ttl: CLI_TTL });
      try {
        const data = await client.fetch("snow", {
          codes: splitStations(opts.station),
          force: COMMON.force,
        });
        const observations = sortObservations(data);
        if (COMMON.json) {
          emitJson({
            element: "snow",
            element_label: ELEMENT_LABELS.snow,
            unit: "cm",
            observations: observations.slice(0, opts.limit).map((o, idx) => ({
              ...observationJson(o),
              rank: idx + 1,
            })),
            fetched_at: formatObservedAt(data.fetchedAt),
            source: AMEDAS_SOURCE,
            source_url: AMEDAS_SOURCE_URL,
          });
          return;
        }
        process.stdout.write(`積雪情報（${AMEDAS_SOURCE.replace("出典:", "")}、単位: cm）\n`);
        if (opts.station) {
          process.stdout.write(`観測所: ${(splitStations(opts.station) ?? []).join(", ")}\n`);
        }
        if (opts.rank) {
          process.stdout.write(`順位は積雪の多い順（上位 ${Math.min(opts.limit, observations.length)} 地点）:\n`);
        } else {
          process.stdout.write(`観測時刻: ${formatObservedAtJst(data.fetchedAt)}\n`);
        }
        const rows = observations.slice(0, opts.limit).map((o, idx) => [
          String(idx + 1),
          o.station.name,
          o.station.code,
          o.value !== null ? o.value.toFixed(1) : "-",
          o.qualityText,
          formatObservedAtJst(o.observedAt),
        ]);
        renderTable(["順位", "観測所", "コード", "積雪(cm)", "品質", "観測時刻"], rows);
        process.stdout.write(`${AMEDAS_SOURCE} / 気象庁「最新の気象データ」CSV\n`);
      } catch (e) {
        if (e instanceof AmedasError) {
          process.stderr.write(`エラー: ${e.message}\n`);
          if (e instanceof AmedasStationNotFoundError) {
            process.stderr.write("ヒント: ngt search で観測所名からコードを確認できます（例: ngt search 長岡）。\n");
          } else {
            process.stderr.write("ヒント: 積雪データは冬季のみ提供されています（夏季は提供休止）。気温・降水量は weather コマンドで確認できます。\n");
          }
          process.exit(1);
        }
        throw e;
      }
    });

  // ---- weather: 気温・降水量 ---------------------------------------------
  program
    .command("weather")
    .description("気温（最高・最低）と降水量を表示する（気象庁アメダス）。")
    .option("-s, --station <station>", "観測所コードまたは観測所名（例: 54232, 長岡）。カンマ・空白区切りで複数指定可。")
    .addOption(new Option("-n, --limit <n>", "表示件数。").default(DEFAULT_LIMIT).argParser((v) => {
      const n = Number(v);
      if (!Number.isInteger(n) || n < 1 || n > 100) {
        process.stderr.write("エラー: --limit は 1〜100 の整数で指定してください\n");
        process.exit(2);
      }
      return n;
    }))
    .option("--json", "出力を JSON 形式にする。")
    .option("--force", "キャッシュを無視して再取得する。")
    .action(async (opts) => {
      COMMON = commonFlags(globalOpts(), opts);
      const elements = ["max_temp", "min_temp", "precipitation"] as const;
      const client = new AmedasClient({ ttl: CLI_TTL });
      try {
        const codes = splitStations(opts.station);
        const datas = [];
        for (const e of elements) {
          datas.push(await client.fetch(e, { codes, force: COMMON.force }));
        }
        // 観測所コード → 要素ごとの観測値
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
        if (COMMON.json) {
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
          emitJson({
            element: "temperature_precipitation",
            unit: { max_temp: "℃", min_temp: "℃", precipitation: "mm" },
            records,
            fetched_at: formatObservedAt(datas[0].fetchedAt),
            source: AMEDAS_SOURCE,
            source_url: AMEDAS_SOURCE_URL,
          });
          return;
        }
        process.stdout.write(`気温・降水量（${AMEDAS_SOURCE.replace("出典:", "")}）\n`);
        process.stdout.write(`観測時刻: ${formatObservedAtJst(datas[0].fetchedAt)}\n`);
        const rows: string[][] = [];
        for (const [code, obsMap] of byCode) {
          const maxObs = obsMap.get("max_temp");
          const minObs = obsMap.get("min_temp");
          const preObs = obsMap.get("precipitation");
          const name = maxObs?.station.name ?? minObs?.station.name ?? preObs?.station.name ?? code;
          rows.push([
            name,
            code,
            maxObs && maxObs.value !== null ? maxObs.value.toFixed(1) : "-",
            minObs && minObs.value !== null ? minObs.value.toFixed(1) : "-",
            preObs && preObs.value !== null ? preObs.value.toFixed(1) : "-",
          ]);
        }
        renderTable(["観測所", "コード", "最高気温(℃)", "最低気温(℃)", "1時間降水量(mm)"], rows.slice(0, opts.limit));
        process.stdout.write(`${AMEDAS_SOURCE} / 気象庁「最新の気象データ」CSV\n`);
      } catch (e) {
        if (e instanceof AmedasError) {
          process.stderr.write(`エラー: ${e.message}\n`);
          if (e instanceof AmedasStationNotFoundError) {
            process.stderr.write("ヒント: ngt search で観測所名からコードを確認できます（例: ngt search 長岡）。\n");
          } else {
            process.stderr.write("ヒント: データ源が一時的に利用できない可能性があります。時間をおいて再試行してください。\n");
          }
          process.exit(1);
        }
        throw e;
      }
    });

  // ---- stats: 統計・オープンデータ ----------------------------------------
  program
    .command("stats")
    .description("統計・オープンデータを表示する（新潟県オープンデータ）。")
    .option("--datasets", "オープンデータカタログのデータセット一覧を表示する。")
    .option("--population", "人口時系列データ（市町村別）を表示する。")
    .option("--tourism", "道の駅一覧（観光）を表示する。")
    .option("--category <category>", "データセットの分類（内容）で絞り込み（例: 運輸・観光）。")
    .option("--format <format>", "データセットの形式で絞り込み（例: CSV, Excel）。")
    .option("-q, --query <query>", "データセット名・概要のキーワード検索。")
    .option("-m, --municipality <municipality>", "人口データの市町村名で絞り込み（例: 新潟市）。")
    .addOption(new Option("-n, --limit <n>", "表示件数。").default(DEFAULT_LIMIT).argParser((v) => {
      const n = Number(v);
      if (!Number.isInteger(n) || n < 1 || n > 200) {
        process.stderr.write("エラー: --limit は 1〜200 の整数で指定してください\n");
        process.exit(2);
      }
      return n;
    }))
    .option("--json", "出力を JSON 形式にする。")
    .option("--force", "キャッシュを無視して再取得する。")
    .action(async (opts) => {
      COMMON = commonFlags(globalOpts(), opts);
      const flags = [opts.datasets, opts.population, opts.tourism].filter(Boolean);
      if (flags.length > 1) {
        process.stderr.write("エラー: --datasets / --population / --tourism は同時に指定できません\n");
        process.exit(2);
      }
      const client = new OpenDataClient({ ttl: CLI_TTL });
      try {
        if (opts.population) {
          const records = await client.getPopulation({ municipality: opts.municipality, force: COMMON.force });
          printWarnings(client.warnings);
          if (COMMON.json) {
            emitJson({
              type: "population",
              records: records.slice(0, opts.limit).map(populationJson),
              source: records.length > 0 ? records[0].source : null,
              source_url: records.length > 0 ? records[0].sourceUrl : null,
            });
            return;
          }
          process.stdout.write("人口時系列データ（市町村別、人口総数）\n");
          if (records.length > 0) {
            process.stdout.write(`${records[0].source} / ${records[0].sourceUrl}\n`);
          } else {
            process.stdout.write("出典: データなし\n");
          }
          const rows = records.slice(0, opts.limit).map((r) => [
            r.date,
            r.municipalityName,
            r.total.toLocaleString("en-US"),
            r.male.toLocaleString("en-US"),
            r.female.toLocaleString("en-US"),
          ]);
          renderTable(["年月日", "市町村", "人口総数", "男", "女"], rows);
          return;
        }
        if (opts.tourism) {
          const stations = await client.getTourism({ force: COMMON.force });
          printWarnings(client.warnings);
          if (COMMON.json) {
            emitJson({
              type: "michinoeki",
              stations: stations.slice(0, opts.limit).map(michinoekiJson),
              source: stations.length > 0 ? stations[0].source : null,
              source_url: stations.length > 0 ? stations[0].sourceUrl : null,
            });
            return;
          }
          process.stdout.write("道の駅一覧（新潟県）\n");
          if (stations.length > 0) {
            process.stdout.write(`${stations[0].source} / ${stations[0].sourceUrl}\n`);
          } else {
            process.stdout.write("出典: データなし\n");
          }
          const rows = stations.slice(0, opts.limit).map((s) => [
            String(s.id), s.name, s.route, s.address, s.phone,
          ]);
          renderTable(["番号", "駅名", "路線名", "所在地", "電話番号"], rows);
          return;
        }
        // デフォルト: データセット一覧
        const ds = await client.getDatasets({
          query: opts.query,
          category: opts.category,
          dataFormat: opts.format,
          force: COMMON.force,
        });
        printWarnings(client.warnings);
        if (COMMON.json) {
          emitJson({
            type: "datasets",
            datasets: ds.slice(0, opts.limit).map(datasetJson),
            count: ds.length,
            source: ds.length > 0 ? ds[0].source : null,
          });
          return;
        }
        process.stdout.write(`オープンデータカタログ（${ds.length} 件中 先頭 ${Math.min(opts.limit, ds.length)} 件）\n`);
        const rows = ds.slice(0, opts.limit).map((d) => [
          d.id, d.name, d.category, d.format, d.updateFrequency, d.fiscalYear,
        ]);
        renderTable(["№", "データ名", "分類", "形式", "更新頻度", "年度"], rows);
        process.stdout.write(ds.length > 0 ? `${ds[0].source}\n` : "出典: データなし\n");
      } catch (e) {
        if (e instanceof OpenDataError) {
          process.stderr.write(`エラー: ${e.message}\n`);
          process.exit(1);
        }
        throw e;
      }
    });

  // ---- search: 全データ横断検索 -------------------------------------------
  program
    .command("search")
    .description("全データを横断検索する（観測所・人口・道の駅・データセット）。")
    .argument("<keyword>", "検索キーワード（観測所名・市町村名・データ名など）。")
    .addOption(new Option("-n, --limit <n>", "表示件数。").default(DEFAULT_LIMIT).argParser((v) => {
      const n = Number(v);
      if (!Number.isInteger(n) || n < 1 || n > 100) {
        process.stderr.write("エラー: --limit は 1〜100 の整数で指定してください\n");
        process.exit(2);
      }
      return n;
    }))
    .option("--json", "出力を JSON 形式にする。")
    .option("--force", "キャッシュを無視して再取得する。")
    .action(async (keyword, opts) => {
      COMMON = commonFlags(globalOpts(), opts);
      const results: Record<string, unknown> = {};

      // 1. 観測所（アメダス）: 名前・番号の部分一致
      const stationsHit: Record<string, unknown>[] = [];
      const aclient = new AmedasClient({ ttl: CLI_TTL });
      for (const st of aclient.getStations()) {
        if (keyword.includes(st.name) || keyword.includes(st.code)) {
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
      results.stations = stationsHit;

      // 2. オープンデータ（人口・道の駅・データセット）
      const oclient = new OpenDataClient({ ttl: CLI_TTL });
      try {
        const pop = await oclient.getPopulation({ force: COMMON.force });
        results.population = pop
          .filter((r) => keyword.includes(r.municipalityName) || keyword.includes(r.municipalityCode))
          .map(populationJson);
        results.population_source = pop.length > 0 ? pop[0].source : null;
        results.population_source_url = pop.length > 0 ? pop[0].sourceUrl : null;
      } catch (e) {
        results.population_error = (e as Error).message;
      }
      try {
        const michi = await oclient.getTourism({ force: COMMON.force });
        results.michinoeki = michi
          .filter((s) => keyword.includes(s.name) || keyword.includes(s.address))
          .map(michinoekiJson);
        results.michinoeki_source = michi.length > 0 ? michi[0].source : null;
        results.michinoeki_source_url = michi.length > 0 ? michi[0].sourceUrl : null;
      } catch (e) {
        results.michinoeki_error = (e as Error).message;
      }
      try {
        const ds = await oclient.getDatasets({ force: COMMON.force });
        results.datasets = ds
          .filter((d) =>
            keyword.includes(d.name) ||
            keyword.includes(d.category) ||
            keyword.includes(d.description) ||
            keyword.includes(d.fields),
          )
          .map(datasetJson);
        results.datasets_source = ds.length > 0 ? ds[0].source : null;
      } catch (e) {
        results.datasets_error = (e as Error).message;
      }
      printWarnings(oclient.warnings);

      const stations = results.stations as Record<string, unknown>[];
      const population = results.population as Record<string, unknown>[] ?? [];
      const michinoeki = results.michinoeki as Record<string, unknown>[] ?? [];
      const datasets = results.datasets as Record<string, unknown>[] ?? [];
      const errors = ["population_error", "michinoeki_error", "datasets_error"]
        .filter((k) => k in results)
        .map((k) => String(results[k]));

      if (COMMON.json) {
        emitJson({
          keyword,
          count: {
            stations: stations.length,
            population: population.length,
            michinoeki: michinoeki.length,
            datasets: datasets.length,
          },
          stations: stations.slice(0, opts.limit),
          population: population.slice(0, opts.limit),
          michinoeki: michinoeki.slice(0, opts.limit),
          datasets: datasets.slice(0, opts.limit),
          source: {
            stations: AMEDAS_SOURCE,
            population: results.population_source ?? null,
            michinoeki: results.michinoeki_source ?? null,
            datasets: results.datasets_source ?? null,
          },
          errors,
        });
        return;
      }

      const total = stations.length + population.length + michinoeki.length + datasets.length;
      process.stdout.write(`検索キーワード: ${keyword}（ヒット ${total} 件）\n`);

      if (stations.length > 0) {
        process.stdout.write(`\n■ アメダス観測所（${stations.length} 件）\n`);
        renderTable(
          ["コード", "観測所", "緯度", "経度", "標高(m)", "種別"],
          stations.slice(0, opts.limit).map((s) => [
            String(s.station_code),
            String(s.station_name),
            (s.lat as number).toFixed(4),
            (s.lon as number).toFixed(4),
            String(s.altitude),
            String(s.station_type),
          ]),
        );
      }
      if (population.length > 0) {
        const shown = Math.min(opts.limit, population.length);
        process.stdout.write(`\n■ 人口（${population.length} 件中 先頭 ${shown} 件）\n`);
        renderTable(
          ["年月日", "市町村", "人口総数"],
          population.slice(0, opts.limit).map((p) => [
            String(p.date),
            String(p.municipality_name),
            (p.total as number).toLocaleString("en-US"),
          ]),
        );
      }
      if (michinoeki.length > 0) {
        process.stdout.write(`\n■ 道の駅（${michinoeki.length} 件）\n`);
        renderTable(
          ["番号", "駅名", "所在地", "電話番号"],
          michinoeki.slice(0, opts.limit).map((m) => [
            String(m.id), String(m.name), String(m.address), String(m.phone),
          ]),
        );
      }
      if (datasets.length > 0) {
        process.stdout.write(`\n■ データセット（${datasets.length} 件）\n`);
        renderTable(
          ["№", "データ名", "分類", "形式"],
          datasets.slice(0, opts.limit).map((d) => [
            String(d.id), String(d.name), String(d.category), String(d.format),
          ]),
        );
      }
      for (const key of ["population_error", "michinoeki_error", "datasets_error"]) {
        if (key in results) process.stderr.write(`注: ${String(results[key])}\n`);
      }
      if (total === 0) {
        process.stderr.write("ヒットしませんでした。観測所名（例: 湯沢）・市町村名（例: 長岡）・キーワード（例: 観光）などをお試しください。\n");
        process.exit(1);
      }
    });

  return program;
}

// ---------------------------------------------------------------------------
// tour サブコマンドの実装
// ---------------------------------------------------------------------------

async function tourSpots(category: string | undefined, limit: number): Promise<void> {
  const client = new TourismClient({ ttl: CLI_TTL });
  try {
    const spots = await client.getSpots({ category: category ?? null, force: COMMON.force });
    tourWarnings(client);
    if (COMMON.json) {
      emitJson({
        type: "spots",
        category: category ?? null,
        spots: spots.slice(0, limit).map(spotJson),
        count: spots.length,
        source: TOURISM_SOURCE,
        source_url: TOURISM_SOURCE_URL,
      });
      return;
    }
    if (category) {
      process.stdout.write(`観光スポット一覧（区分: ${category}）\n`);
    } else {
      process.stdout.write(`観光スポット一覧（${spots.length} 件中 先頭 ${Math.min(limit, spots.length)} 件）\n`);
    }
    const rows = spots.slice(0, limit).map((s, idx) => [
      String(idx + 1), s.name, s.category, s.address, s.phone, s.url,
    ]);
    renderTable(["№", "スポット名", "区分", "所在地", "電話番号", "URL"], rows);
    process.stdout.write(`${TOURISM_SOURCE} / 国土数値情報（国土交通省）\n`);
  } catch (e) {
    if (e instanceof TourismError) {
      process.stderr.write(`エラー: ${e.message}\n`);
      process.stderr.write("ヒント: データ源が一時的に利用できない可能性があります。時間をおいて再試行してください。\n");
      process.exit(1);
    }
    throw e;
  }
}

async function tourOnsen(limit: number): Promise<void> {
  const client = new TourismClient({ ttl: CLI_TTL });
  try {
    const spots = await client.getOnsenSpots({ force: COMMON.force });
    tourWarnings(client);
    if (COMMON.json) {
      emitJson({
        type: "onsen",
        spots: spots.slice(0, limit).map(spotJson),
        count: spots.length,
        source: TOURISM_SOURCE,
        source_url: TOURISM_SOURCE_URL,
      });
      return;
    }
    process.stdout.write(`温泉スポット一覧（${spots.length} 件中 先頭 ${Math.min(limit, spots.length)} 件）\n`);
    const rows = spots.slice(0, limit).map((s, idx) => {
      let description = s.description;
      if (description.startsWith("（") && description.endsWith("）")) {
        description = description.slice(1, -1);
      }
      return [String(idx + 1), s.name, s.address, s.phone, description];
    });
    renderTable(["№", "温泉名", "所在地", "電話番号", "泉質・備考"], rows);
    process.stdout.write(`${TOURISM_SOURCE}（CC-BY）\n`);
  } catch (e) {
    if (e instanceof TourismError) {
      process.stderr.write(`エラー: ${e.message}\n`);
      process.stderr.write("ヒント: データ源が一時的に利用できない可能性があります。時間をおいて再試行してください。\n");
      process.exit(1);
    }
    throw e;
  }
}

async function tourWeather(recommend: boolean, limit: number): Promise<void> {
  const tclient = new TourismClient({ ttl: CLI_TTL });
  const aclient = new AmedasClient({ ttl: CLI_TTL });
  try {
    const [spots, data] = await Promise.all([
      tclient.getSpots({ force: COMMON.force }),
      aclient.fetch("precipitation", { force: COMMON.force }),
    ]);
    tourWarnings(tclient);

    // 降水量の多い観測所（雨が降っている地域）を抽出
    const rainy: Array<{ name: string; lat: number; lon: number }> = [];
    for (const o of data.observations) {
      if (o.value !== null && o.value >= 1.0) {
        rainy.push({ name: o.station.name, lat: o.station.lat, lon: o.station.lon });
      }
    }

    if (COMMON.json) {
      let recommendations: Record<string, unknown>[] = [];
      if (recommend) {
        const shown = new Set<string>();
        for (const st of rainy) {
          const group = WEATHER_RECOMMENDATION[st.name];
          if (!group) continue;
          const keywords = TOUR_RECOMMEND_KEYWORDS[group] ?? [group];
          for (const s of recommendedSpots(spots, keywords)) {
            if (shown.has(s.id)) continue;
            shown.add(s.id);
            let dist: number | null = null;
            if (s.lat !== null && s.lon !== null) {
              dist = haversineKm(st.lat, st.lon, s.lat, s.lon);
              if (dist > RECOMMEND_RADIUS_KM) continue;
            }
            recommendations.push({
              spot: spotJson(s),
              reason: `雨が降っている${st.name}方面は${group}がおすすめ`,
              distance_km: dist !== null ? Math.round(dist * 10) / 10 : null,
            });
          }
        }
      }
      emitJson({
        type: recommend ? "weather_recommend" : "weather",
        observed_at: formatObservedAt(data.fetchedAt),
        rainy_stations: rainy.map((st) => st.name),
        spots: spots.slice(0, limit).map(spotJson),
        recommendations: recommend ? recommendations : null,
        source: TOURISM_SOURCE,
        source_url: TOURISM_SOURCE_URL,
      });
      return;
    }

    process.stdout.write(`観光スポットと天気（${AMEDAS_SOURCE.replace("出典:", "")}）\n`);
    process.stdout.write(`観測時刻: ${formatObservedAtJst(data.fetchedAt)}\n`);
    process.stdout.write(
      `雨が降っている地域: ${rainy.length > 0 ? rainy.map((st) => st.name).join(", ") : "なし"}\n`,
    );
    const rows = spots.slice(0, limit).map((s, idx) => [
      String(idx + 1), s.name, s.category, s.address, s.phone, s.url,
    ]);
    renderTable(["№", "スポット名", "区分", "所在地", "電話番号", "URL"], rows);
    process.stdout.write(`${TOURISM_SOURCE} / 国土数値情報（国土交通省）\n`);
    if (recommend) {
      tourRecommendTable(spots, rainy);
    }
  } catch (e) {
    if (e instanceof TourismError || e instanceof AmedasError) {
      process.stderr.write(`エラー: ${(e as Error).message}\n`);
      process.stderr.write("ヒント: データ源が一時的に利用できない可能性があります。時間をおいて再試行してください。\n");
      process.exit(1);
    }
    throw e;
  }
}

function tourRecommendTable(spots: Spot[], rainy: Array<{ name: string; lat: number; lon: number }>): void {
  process.stdout.write("\n■ 天気に合わせたおすすめスポット\n");
  const shown = new Set<string>();
  for (const st of rainy) {
    const group = WEATHER_RECOMMENDATION[st.name];
    if (!group) continue;
    const keywords = TOUR_RECOMMEND_KEYWORDS[group] ?? [group];
    for (const s of recommendedSpots(spots, keywords)) {
      if (shown.has(s.id)) continue;
      if (s.lat !== null && s.lon !== null) {
        const dist = haversineKm(st.lat, st.lon, s.lat, s.lon);
        if (dist > RECOMMEND_RADIUS_KM) continue;
        process.stdout.write(`・${s.name}（${s.category}）— 雨が降っている${st.name}方面は${group}がおすすめ（約${Math.round(dist)}km）\n`);
      } else {
        process.stdout.write(`・${s.name}（${s.category}）— 雨が降っている${st.name}方面は${group}がおすすめ\n`);
      }
      shown.add(s.id);
    }
  }
  if (shown.size === 0) {
    process.stdout.write("（雨が降っている地域の周辺におすすめスポットが見つかりませんでした）\n");
  }
}

async function tourIrikomi(year: number | undefined, limit: number): Promise<void> {
  const client = new TourismClient({ ttl: CLI_TTL });
  try {
    const stats = await client.getIrikomi({ year: year ?? null, force: COMMON.force });
    tourWarnings(client);
    if (COMMON.json) {
      emitJson({
        type: "irikomi",
        year: year ?? null,
        stats: stats.slice(0, limit).map(tourStatJson),
        source: TOURISM_SOURCE,
        source_url: TOURISM_SOURCE_URL,
      });
      return;
    }
    process.stdout.write("観光入込客数（新潟市、年別・分類別）\n");
    const rows = stats.slice(0, limit).map((s) => [
      String(s.year),
      s.eraYear,
      s.total !== null ? s.total.toLocaleString("en-US") : "-",
      s.eventTotal !== null ? s.eventTotal.toLocaleString("en-US") : "-",
      s.spotTotal !== null ? s.spotTotal.toLocaleString("en-US") : "-",
    ]);
    renderTable(["年", "和暦", "入込客数合計(千人)", "行祭事・イベント(千人)", "観光地点合計(千人)"], rows);
    process.stdout.write(`${TOURISM_SOURCE}（CC-BY）\n`);
  } catch (e) {
    if (e instanceof TourismError) {
      process.stderr.write(`エラー: ${e.message}\n`);
      process.stderr.write("ヒント: データ源が一時的に利用できない可能性があります。時間をおいて再試行してください。\n");
      process.exit(1);
    }
    throw e;
  }
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

// ---------------------------------------------------------------------------
// エントリポイント
// ---------------------------------------------------------------------------

export async function main(argv: string[] = process.argv): Promise<void> {
  const program = buildProgram();
  await program.parseAsync(argv);
}

// bin（dist/cli/index.js）から直接実行された場合のエントリポイント
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
    process.stderr.write(`エラー: ${(e as Error).message}\n`);
    process.exit(1);
  });
}
