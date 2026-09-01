import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import {
  TourismClient,
  TourismFetchError,
  TourismParseError,
  parseIrikomiCsv,
  parseOnsenCsv,
  parseP33Zip,
  parseZip,
  SOURCE_TEXT,
  P33_SOURCE_TEXT,
  type TourismStat,
} from "../core/tourism.js";
import iconv from "iconv-lite";

const SAMPLE_IRIKOMI_CSV = `年[西暦],年[和暦],観光入込客数合計[千人],行祭事・イベント合計[千人],観光地点合計[千人],観光地点合計の自然[千人],観光地点合計の歴史・文化[千人],観光地点合計の温泉・健康[千人],観光地点合計のスポーツ・レクリエーション[千人],観光地点合計の都市型観光[千人],観光地点合計のその他[千人]
2023,令和5,15557,4382,11175,419,3100,818,1792,5046,0
2024,令和6,16019,4591,11428,425,3044,861,2026,5072,0
`;

const SAMPLE_ONSEN_CSV = `longitude,latitude,SAUID,SAFIELD000,SAFIELD001,SAFIELD002,SAFIELD003,SAFIELD004,SAFIELD005
138.8398538,37.7380947,1,ほてる大橋館の湯,953-0011,新潟市西蒲区岩室温泉340-甲,0256-82-4125,岩室温泉,含硫黄－ナトリウム･カルシウム－塩化物泉
138.837374,37.7280278,2,多宝温泉　だいろの湯,953-0011,新潟市西蒲区石瀬3250,0256-82-1126,多宝温泉だいろの湯,含硫黄－ナトリウム・カルシウム－塩化物泉
`;

function encodeText(text: string, enc: string): Uint8Array {
  return new Uint8Array(iconv.encode(text, enc));
}

describe("parseIrikomiCsv", () => {
  it("parses year stats", () => {
    const stats: TourismStat[] = parseIrikomiCsv(
      encodeText(SAMPLE_IRIKOMI_CSV, "utf-8"),
      "http://example",
    );
    expect(stats).toHaveLength(2);
    expect(stats[0].year).toBe(2023);
    expect(stats[0].eraYear).toBe("令和5");
    expect(stats[0].total).toBe(15557);
    expect(stats[1].year).toBe(2024);
    expect(stats[1].urbanTourism).toBe(5072);
  });
});

describe("parseOnsenCsv", () => {
  it("parses onsen spots with coordinates", () => {
    const spots = parseOnsenCsv(encodeText(SAMPLE_ONSEN_CSV, "utf-8"), "http://example");
    expect(spots).toHaveLength(2);
    expect(spots[0].name).toBe("ほてる大橋館の湯");
    expect(spots[0].category).toBe("温泉");
    expect(spots[0].lat).toBeCloseTo(37.7380947);
    expect(spots[0].lon).toBeCloseTo(138.8398538);
    expect(spots[0].description).toContain("含硫黄");
    expect(spots[0].id).toBe("onsen-1");
  });
});

describe("ZIP parser (non-compressed)", () => {
  it("parses stored ZIP entries", () => {
    // 非圧縮 ZIP を最小構成で構築（local header + central directory + EOCD）
    const name = Buffer.from("test.txt");
    const content = Buffer.from("hello zip");
    const localHeader = Buffer.alloc(30);
    localHeader.writeUInt32LE(0x04034b50, 0);
    localHeader.writeUInt16LE(20, 4); // version
    localHeader.writeUInt16LE(0, 6); // flags
    localHeader.writeUInt16LE(0, 8); // method: stored
    localHeader.writeUInt32LE(0, 10); // crc (dummy)
    localHeader.writeUInt32LE(content.length, 14);
    localHeader.writeUInt32LE(content.length, 18);
    localHeader.writeUInt16LE(name.length, 26);
    localHeader.writeUInt16LE(0, 28);
    const local = Buffer.concat([localHeader, name, content]);

    const cdHeader = Buffer.alloc(46);
    cdHeader.writeUInt32LE(0x02014b50, 0);
    cdHeader.writeUInt16LE(20, 4);
    cdHeader.writeUInt16LE(20, 6);
    cdHeader.writeUInt16LE(0, 8); // method
    cdHeader.writeUInt32LE(0, 16); // crc
    cdHeader.writeUInt32LE(content.length, 20);
    cdHeader.writeUInt32LE(content.length, 24);
    cdHeader.writeUInt16LE(name.length, 28);
    cdHeader.writeUInt16LE(0, 30);
    cdHeader.writeUInt16LE(0, 32);
    cdHeader.writeUInt16LE(0, 34);
    cdHeader.writeUInt16LE(0, 36);
    cdHeader.writeUInt32LE(0, 38);
    cdHeader.writeUInt32LE(0, 42); // local header offset
    const central = Buffer.concat([cdHeader, name]);

    const eocd = Buffer.alloc(22);
    eocd.writeUInt32LE(0x06054b50, 0);
    eocd.writeUInt16LE(0, 4);
    eocd.writeUInt16LE(0, 6);
    eocd.writeUInt16LE(1, 8);
    eocd.writeUInt16LE(1, 10);
    eocd.writeUInt32LE(central.length, 12);
    eocd.writeUInt32LE(local.length, 16);

    const zip = Buffer.concat([local, central, eocd]);
    const entries = parseZip(new Uint8Array(zip));
    expect(entries).toHaveLength(1);
    expect(entries[0].name).toBe("test.txt");
    expect(new TextDecoder().decode(entries[0].data)).toBe("hello zip");
  });
});

describe("TourismClient (fetch mocked)", () => {
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
    new Response(body, { status }) as unknown as Response;

  it("falls back to sample spots when fetch fails", async () => {
    fetchMock.mockResolvedValue(okResponse(new Uint8Array(), 500));
    const client = new TourismClient({ fallbackToSample: true });
    const spots = await client.getSpots();
    expect(spots.length).toBeGreaterThan(0);
    expect(client.warnings.length).toBeGreaterThan(0);
  });

  it("throws when fallback disabled", async () => {
    fetchMock.mockResolvedValue(okResponse(new Uint8Array(), 500));
    const client = new TourismClient({ fallbackToSample: false });
    await expect(client.getSpots()).rejects.toThrow(TourismFetchError);
  });

  it("filters spots by category", async () => {
    fetchMock.mockResolvedValue(okResponse(new Uint8Array(), 500));
    const client = new TourismClient({ fallbackToSample: true });
    const spots = await client.getSpots({ category: "温泉" });
    expect(spots.length).toBeGreaterThan(0);
    expect(spots.every((s) => s.category === "温泉")).toBe(true);
  });

  it("parses irikomi from CSV URL", async () => {
    fetchMock.mockResolvedValue(okResponse(encodeText(SAMPLE_IRIKOMI_CSV, "utf-8")));
    const client = new TourismClient({ fallbackToSample: false });
    const stats = await client.getIrikomi({ year: 2024 });
    expect(stats).toHaveLength(1);
    expect(stats[0].total).toBe(16019);
  });

  it("parses onsen from CSV URL", async () => {
    fetchMock.mockResolvedValue(okResponse(encodeText(SAMPLE_ONSEN_CSV, "utf-8")));
    const client = new TourismClient({ fallbackToSample: false });
    const spots = await client.getOnsenSpots();
    expect(spots).toHaveLength(2);
    expect(spots[0].category).toBe("温泉");
  });
});
