import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import {
  OpenDataClient,
  OpenDataFetchError,
  OpenDataParseError,
  SOURCE_TEXT,
  parsePopulationCsv,
  parseMichinoekiHtml,
  type Dataset,
  type PopulationRecord,
} from "../core/opendata.js";
import iconv from "iconv-lite";

const SAMPLE_POP_CSV = `年月日,市町村CD,市町村名,人口総数,男計,女計,0歳,1歳
2024/10/1 0:00,15201,新潟市,772425,372208,400217,5399,5538
2024/10/1 0:00,15202,長岡市,258131,124938,133193,1687,1735
2024/10/1 0:00,15000,新潟県,2150525,1038213,1112312,13977,14368
`;

const SAMPLE_MICHINOEKI_HTML = `<html><body><table>
<tr><td>1</td><td>豊栄</td><td>一般国道7号</td><td>新潟市北区木崎字切尾山3644-乙</td><td>025-388-2700</td></tr>
<tr><td>2</td><td>加治川（さくらの里）</td><td>一般国道7号</td><td>新発田市横岡1147</td><td>0254-33-3175</td></tr>
</table></body></html>`;

function encodeCsv(text: string, enc: string): Uint8Array {
  return new Uint8Array(iconv.encode(text, enc));
}

describe("parsePopulationCsv", () => {
  it("parses layout A (wide format) and skips prefecture row", () => {
    const records = parsePopulationCsv(encodeCsv(SAMPLE_POP_CSV, "cp932"), "http://example");
    expect(records).toHaveLength(2);
    expect(records[0].municipalityName).toBe("新潟市");
    expect(records[0].total).toBe(772425);
    expect(records[1].municipalityName).toBe("長岡市");
    // 県計（15000）は除外
    expect(records.some((r) => r.municipalityName === "新潟県")).toBe(false);
  });

  it("parses layout B (compact format)", () => {
    const csv = `年月日,団体コード,都道府県名・市区町村名,総数,男,女,基準,出所
2024/10/1 0:00,152010,新潟市,772425,372208,400217,人口,住民基本台帳
`;
    const records = parsePopulationCsv(encodeCsv(csv, "cp932"), "http://example");
    expect(records).toHaveLength(1);
    expect(records[0].municipalityCode).toBe("152010");
  });

  it("raises for unexpected header", () => {
    const csv = "foo,bar,baz\n1,2,3\n";
    expect(() => parsePopulationCsv(encodeCsv(csv, "utf-8"), "http://example")).toThrow(
      OpenDataParseError,
    );
  });
});

describe("parseMichinoekiHtml", () => {
  it("parses table rows", () => {
    const stations = parseMichinoekiHtml(SAMPLE_MICHINOEKI_HTML, "http://example");
    expect(stations).toHaveLength(2);
    expect(stations[0].name).toBe("豊栄");
    expect(stations[0].phone).toBe("025-388-2700");
  });

  it("raises when no table", () => {
    expect(() => parseMichinoekiHtml("<html></html>", "http://example")).toThrow(
      OpenDataParseError,
    );
  });
});

describe("OpenDataClient (fetch mocked)", () => {
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

  it("falls back to sample data when all sources fail", async () => {
    fetchMock.mockResolvedValue(okResponse(new Uint8Array(), 500));
    const client = new OpenDataClient({ fallbackToSample: true });
    const datasets = await client.getDatasets();
    expect(datasets.length).toBeGreaterThan(0);
    expect(client.warnings.length).toBeGreaterThan(0);
    expect(datasets[0].source).toBe(SOURCE_TEXT);
  });

  it("throws when fallback disabled and all sources fail", async () => {
    fetchMock.mockResolvedValue(okResponse(new Uint8Array(), 500));
    const client = new OpenDataClient({ fallbackToSample: false });
    await expect(client.getDatasets()).rejects.toThrow(OpenDataFetchError);
  });

  it("filters datasets by query and category", async () => {
    fetchMock.mockResolvedValue(okResponse(new Uint8Array(), 500));
    const client = new OpenDataClient({ fallbackToSample: true });
    const ds = await client.getDatasets({ query: "人口" });
    expect(ds.length).toBeGreaterThan(0);
    expect(ds.every((d) => d.name.includes("人口"))).toBe(true);
  });

  it("parses CKAN success response", async () => {
    const payload = {
      success: true,
      result: {
        results: [
          {
            id: "abc",
            title: "人口時系列データ",
            notes: "概要",
            extras: [{ key: "分野", value: "人口・世帯" }],
            resources: [{ format: "CSV" }],
          },
        ],
      },
    };
    fetchMock.mockResolvedValue(
      okResponse(new Uint8Array(new TextEncoder().encode(JSON.stringify(payload)))),
    );
    const client = new OpenDataClient({ fallbackToSample: false });
    const datasets = await client.getDatasets();
    expect(datasets).toHaveLength(1);
    expect(datasets[0].name).toBe("人口時系列データ");
    expect(datasets[0].category).toBe("人口・世帯");
    expect(datasets[0].format).toBe("CSV");
  });
});
