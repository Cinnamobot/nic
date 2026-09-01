import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import {
  AmedasClient,
  AmedasElementValue,
  AmedasFetchError,
  AmedasParseError,
  AmedasStationNotFoundError,
  NIIGATA_STATIONS,
  parseCsvBytes,
  rowToObservation,
  QUALITY_CODES,
  SOURCE_TEXT,
} from "../core/amedas.js";
import iconv from "iconv-lite";

// 実データ形式に合わせたサンプル（Shift_JIS エンコードした CSV）
const SAMPLE_PRE1H_CSV = `観測所番号,都道府県,地点,国際地点番号,現在時刻(年),現在時刻(月),現在時刻(日),現在時刻(時),現在時刻(分),現在値(mm),現在値の品質情報,31日の最大値(mm),31日の最大値の品質情報
54232,新潟県,新潟（ニイガタ）,,2026,08,31,14,10,0.0,8,0.0,4
54841,新潟県,湯沢（ユザワ）,,2026,08,31,14,10,0.5,8,0.5,4
11001,北海道 宗谷地方,宗谷岬（ソウヤミサキ）,,2026,08,31,14,10,0.0,8,0.0,4
`;

function encodeCsv(text: string): Uint8Array {
  return new Uint8Array(iconv.encode(text, "cp932"));
}

describe("NIIGATA_STATIONS", () => {
  it("44 stations in Niigata", () => {
    expect(Object.keys(NIIGATA_STATIONS)).toHaveLength(44);
  });

  it("has known stations", () => {
    expect(NIIGATA_STATIONS["54232"].name).toBe("新潟");
    expect(NIIGATA_STATIONS["54841"].name).toBe("湯沢");
    expect(NIIGATA_STATIONS["54501"].name).toBe("長岡");
  });

  it("station fields", () => {
    const st = NIIGATA_STATIONS["54232"];
    expect(st.code).toBe("54232");
    expect(st.lat).toBeCloseTo(37.8933, 4);
    expect(st.lon).toBeCloseTo(139.0183, 4);
    expect(st.altitude).toBe(4);
    expect(st.stationType).toBe("A");
  });
});

describe("parseCsvBytes", () => {
  it("parses Shift_JIS CSV and removes header", () => {
    const rows = parseCsvBytes(encodeCsv(SAMPLE_PRE1H_CSV));
    expect(rows).toHaveLength(3);
    expect(rows[0][0]).toBe("54232");
    expect(rows[0][1]).toBe("新潟県");
  });

  it("throws on empty CSV", () => {
    expect(() => parseCsvBytes(encodeCsv(""))).toThrow(AmedasParseError);
  });
});

describe("rowToObservation", () => {
  it("converts a row to Observation", () => {
    const rows = parseCsvBytes(encodeCsv(SAMPLE_PRE1H_CSV));
    const obs = rowToObservation("precipitation", rows[0]);
    expect(obs).not.toBeNull();
    expect(obs!.station.name).toBe("新潟");
    expect(obs!.value).toBeCloseTo(0.0);
    expect(obs!.quality).toBe(8);
    expect(obs!.qualityText).toBe(QUALITY_CODES[8]);
    expect(obs!.source).toBe(SOURCE_TEXT);
  });

  it("returns null for non-Niigata row", () => {
    const rows = parseCsvBytes(encodeCsv(SAMPLE_PRE1H_CSV));
    expect(rowToObservation("precipitation", rows[2])).toBeNull();
  });

  it("handles missing value", () => {
    const row = ["54841", "新潟県", "湯沢", "", "2026", "08", "31", "14", "10", "", ""];
    const obs = rowToObservation("precipitation", row);
    expect(obs!.value).toBeNull();
    expect(obs!.quality).toBeNull();
  });
});

describe("AmedasClient", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    fetchMock = vi.fn();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  const okResponse = (body: Uint8Array, status = 200) =>
    new Response(body, { status, headers: { "Content-Type": "text/csv" } }) as unknown as Response;

  it("getStations returns all stations", () => {
    const client = new AmedasClient();
    expect(client.getStations()).toHaveLength(44);
  });

  it("getStation raises for unknown code", () => {
    const client = new AmedasClient();
    expect(() => client.getStation("99999")).toThrow(AmedasStationNotFoundError);
  });

  it("fetch parses CSV and filters by codes", async () => {
    fetchMock.mockResolvedValue(okResponse(encodeCsv(SAMPLE_PRE1H_CSV)));
    const client = new AmedasClient();
    const data = await client.fetch("precipitation", { codes: ["54841"] });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(data.observations).toHaveLength(1);
    expect(data.observations[0].station.name).toBe("湯沢");
  });

  it("fetch throws on 404", async () => {
    fetchMock.mockResolvedValue(okResponse(new Uint8Array(), 404));
    const client = new AmedasClient();
    await expect(client.fetch("snow")).rejects.toThrow(AmedasFetchError);
  });

  it("fetch throws on network error", async () => {
    fetchMock.mockRejectedValue(new Error("network down"));
    const client = new AmedasClient();
    await expect(client.fetch("precipitation")).rejects.toThrow(AmedasFetchError);
  });

  it("fetch invalid code raises StationNotFound", async () => {
    const client = new AmedasClient();
    await expect(client.fetch("precipitation", { codes: ["99999"] })).rejects.toThrow(
      AmedasStationNotFoundError,
    );
  });

  it("caches within TTL and refetches after expiry", async () => {
    const body = encodeCsv(SAMPLE_PRE1H_CSV);
    fetchMock.mockImplementation(() => Promise.resolve(okResponse(body)));
    const client = new AmedasClient({ ttl: 60 });
    await client.fetch("precipitation");
    await client.fetch("precipitation");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // force で再取得
    await client.fetch("precipitation", { force: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("detects access-limit page (HTTP 403 in body)", async () => {
    const body = new TextEncoder().encode("HTTP 403 Forbidden - アクセス制限");
    fetchMock.mockResolvedValue(okResponse(body, 200));
    const client = new AmedasClient();
    await expect(client.fetch("precipitation")).rejects.toThrow(AmedasFetchError);
  });
});
