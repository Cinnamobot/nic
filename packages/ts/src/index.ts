/**
 * NIC (Niigata Information Connector) — ライブラリエントリポイント。
 *
 * 新潟県の情報（気象・統計・観光・防災・オープンデータ）にアクセスする
 * CLI / MCP ツールキット。コアクライアントをライブラリとしても公開する。
 */

// amedas
export {
  AmedasClient,
  AmedasError,
  AmedasFetchError,
  AmedasParseError,
  AmedasStationNotFoundError,
  CSV_URLS,
  NIIGATA_STATIONS,
  QUALITY_CODES,
  SOURCE_TEXT as AMEDAS_SOURCE_TEXT,
  SOURCE_URL as AMEDAS_SOURCE_URL,
  STATION_NAME_TO_CODE,
  parseCsvBytes,
  rowToObservation,
  type AmedasData,
  type AmedasElementValue,
  type Observation,
  type Station,
} from "./core/amedas.js";

// warning
export {
  DEFAULT_TTL as WARNING_DEFAULT_TTL,
  EXTRA_FEED_URL,
  NIIGATA_PREF_CODE,
  SOURCE_TEXT as WARNING_SOURCE_TEXT,
  SOURCE_URL as WARNING_SOURCE_URL,
  STATUS_NONE,
  USER_AGENT as WARNING_USER_AGENT,
  WarningClient,
  WarningError,
  WarningFetchError,
  WarningNotFoundError,
  WarningParseError,
  activeKinds,
  findNiigataMessageUrl,
  getActiveAreas,
  getAreas,
  getNiigataWarnings,
  hasWarning,
  listMessageUrls,
  parseWarningXml,
  statusSummary,
  summary,
  type WarningArea,
  type WarningData,
  type WarningKind,
  type WarningLevel,
} from "./core/warning.js";

// opendata
export {
  CKAN_BASE_URL as PREF_CKAN_BASE_URL,
  DEFAULT_TTL as OPEN_DATA_DEFAULT_TTL,
  LICENSE_TEXT,
  LICENSE_URL,
  MICHINO_EKI_PAGE_URL,
  OPEN_DATA_PAGE_URL,
  POPULATION_PAGE_URL,
  SOURCE_TEXT as OPEN_DATA_SOURCE_TEXT,
  USER_AGENT as OPEN_DATA_USER_AGENT,
  OpenDataClient,
  OpenDataError,
  OpenDataFetchError,
  OpenDataNotFoundError,
  OpenDataParseError,
  getDatasets,
  getPopulation,
  getTourism,
  parseMichinoekiHtml,
  parsePopulationCsv,
  type Dataset,
  type MichiNoEki,
  type PopulationRecord,
} from "./core/opendata.js";

// tourism
export {
  CKAN_BASE_URL,
  DEFAULT_TTL as TOURISM_DEFAULT_TTL,
  IRIKOMI_CSV_URL,
  ONSEN_CSV_URL,
  P33_FACILITY_TYPES,
  P33_SOURCE_TEXT,
  P33_SOURCE_URL,
  P33_ZIP_URL,
  SOURCE_TEXT as TOURISM_SOURCE_TEXT,
  SOURCE_URL as TOURISM_SOURCE_URL,
  USER_AGENT as TOURISM_USER_AGENT,
  TourismClient,
  TourismError,
  TourismFetchError,
  TourismNotFoundError,
  TourismParseError,
  getTourismDatasets,
  getTourismSpots,
  getTourismStats,
  parseIrikomiCsv,
  parseOnsenCsv,
  parseP33Zip,
  parseZip,
  type Spot,
  type TourismDataset,
  type TourismStat,
} from "./core/tourism.js";

// util
export {
  decodeText,
  displayWidth,
  formatNumber,
  haversineKm,
  pad,
  parseCsvLine,
  parseCsvRows,
  toFloat,
  toInt,
  toNumber,
} from "./core/util.js";

export const VERSION = "0.1.0";
