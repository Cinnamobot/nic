import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { buildProgram, VERSION } from "./index.js";
import iconv from "iconv-lite";
import type { Command } from "commander";

// 実データ形式に合わせたサンプル（Shift_JIS）
const SAMPLE_PRE1H_CSV = `観測所番号,都道府県,地点,国際地点番号,現在時刻(年),現在時刻(月),現在時刻(日),現在時刻(時),現在時刻(分),現在値(mm),現在値の品質情報
54232,新潟県,新潟（ニイガタ）,,2026,08,31,14,10,0.0,8
54841,新潟県,湯沢（ユザワ）,,2026,08,31,14,10,2.5,8
`;

const SAMPLE_MXTEM_CSV = `観測所番号,都道府県,地点,国際地点番号,現在時刻(年),現在時刻(月),現在時刻(日),現在時刻(時),現在時刻(分),31日の最高気温(℃),31日の最高気温の品質情報
54232,新潟県,新潟（ニイガタ）,,2026,08,31,13,00,18.9,8
54841,新潟県,湯沢（ユザワ）,,2026,08,31,13,00,22.5,8
`;

const SAMPLE_MNTEM_CSV = `観測所番号,都道府県,地点,国際地点番号,現在時刻(年),現在時刻(月),現在時刻(日),現在時刻(時),現在時刻(分),31日の最低気温(℃),31日の最低気温の品質情報
54232,新潟県,新潟（ニイガタ）,,2026,08,31,13,00,12.3,8
54841,新潟県,湯沢（ユザワ）,,2026,08,31,13,00,15.1,8
`;

const SAMPLE_SNOW_CSV = `観測所番号,都道府県,地点,国際地点番号,現在時刻(年),現在時刻(月),現在時刻(日),現在時刻(時),現在時刻(分),現在の積雪の深さ(cm),現在の積雪の深さの品質情報
54232,新潟県,新潟（ニイガタ）,,2026,01,15,09,00,12,8
54841,新潟県,湯沢（ユザワ）,,2026,01,15,09,00,210,8
`;

function encodeCsv(text: string): Uint8Array {
  return new Uint8Array(iconv.encode(text, "cp932"));
}

function run(
  program: Command,
  args: string[],
): Promise<{ stdout: string; stderr: string; exitCode: number | null }> {
  return new Promise((resolve) => {
    const origStdoutWrite = process.stdout.write.bind(process.stdout);
    const origStderrWrite = process.stderr.write.bind(process.stderr);
    const origExit = process.exit.bind(process);
    let stdout = "";
    let stderr = "";
    let exitCode: number | null = null;

    process.stdout.write = ((chunk: unknown) => {
      stdout += String(chunk);
      return true;
    }) as typeof process.stdout.write;
    process.stderr.write = ((chunk: unknown) => {
      stderr += String(chunk);
      return true;
    }) as typeof process.stderr.write;
    process.exit = ((code?: number) => {
      exitCode = code ?? 0;
      throw new Error(`EXIT:${code}`);
    }) as typeof process.exit;

    program
      .parseAsync(["node", "nic", ...args])
      .then(() => {
        process.stdout.write = origStdoutWrite;
        process.stderr.write = origStderrWrite;
        process.exit = origExit;
        resolve({ stdout, stderr, exitCode });
      })
      .catch((e) => {
        process.stdout.write = origStdoutWrite;
        process.stderr.write = origStderrWrite;
        process.exit = origExit;
        // process.exit による中断を exitCode として扱う
        const m = String((e as Error).message).match(/^EXIT:(\d+)$/);
        if (m) {
          resolve({ stdout, stderr, exitCode: Number(m[1]) });
        } else {
          throw e;
        }
      });
  });
}

describe("CLI", () => {
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

  const csvResponse = (body: Uint8Array) =>
    new Response(body, { status: 200 }) as unknown as Response;

  it("shows help with subcommands", async () => {
    const program = buildProgram();
    const { stdout } = await run(program, ["--help"]);
    expect(stdout).toContain("snow");
    expect(stdout).toContain("weather");
    expect(stdout).toContain("warning");
    expect(stdout).toContain("tour");
    expect(stdout).toContain("stats");
    expect(stdout).toContain("search");
  });

  it("shows version", async () => {
    const program = buildProgram();
    const { stdout } = await run(program, ["--version"]);
    expect(stdout).toContain(VERSION);
  });

  it("snow outputs table", async () => {
    fetchMock.mockResolvedValue(csvResponse(encodeCsv(SAMPLE_SNOW_CSV)));
    const program = buildProgram();
    const { stdout } = await run(program, ["snow", "--limit", "2"]);
    expect(stdout).toContain("積雪情報");
    expect(stdout).toContain("新潟");
    expect(stdout).toContain("湯沢");
  });

  it("weather outputs table with stations", async () => {
    const bodies = [
      encodeCsv(SAMPLE_PRE1H_CSV), // precipitation
      encodeCsv(SAMPLE_MXTEM_CSV), // max_temp
      encodeCsv(SAMPLE_MNTEM_CSV), // min_temp
    ];
    let call = 0;
    fetchMock.mockImplementation(() => {
      const body = bodies[Math.min(call, bodies.length - 1)];
      call++;
      return Promise.resolve(csvResponse(body));
    });
    const program = buildProgram();
    const { stdout } = await run(program, ["weather", "--station", "新潟,湯沢"]);
    expect(stdout).toContain("新潟");
    expect(stdout).toContain("湯沢");
    expect(stdout).toContain("18.9");
    expect(stdout).toContain("22.5");
  });

  it("weather JSON output", async () => {
    const bodies = [
      encodeCsv(SAMPLE_PRE1H_CSV),
      encodeCsv(SAMPLE_MXTEM_CSV),
      encodeCsv(SAMPLE_MNTEM_CSV),
    ];
    let call = 0;
    fetchMock.mockImplementation(() => {
      const body = bodies[Math.min(call, bodies.length - 1)];
      call++;
      return Promise.resolve(csvResponse(body));
    });
    const program = buildProgram();
    const { stdout } = await run(program, ["weather", "--json"]);
    const parsed = JSON.parse(stdout);
    expect(parsed.element).toBe("temperature_precipitation");
    expect(parsed.records.length).toBe(2);
    expect(parsed.records[0].station_name).toBe("新潟");
    expect(parsed.source).toBe("出典:気象庁");
  });

  it("invalid station name exits with code 2", async () => {
    const program = buildProgram();
    const { stderr, exitCode } = await run(program, ["snow", "--station", "東京"]);
    expect(exitCode).toBe(2);
    expect(stderr).toContain("観測所「東京」は見つかりません");
  });

  it("tour conflicting flags exits with code 2", async () => {
    const program = buildProgram();
    const { stderr, exitCode } = await run(program, ["tour", "--spots", "--onsen"]);
    expect(exitCode).toBe(2);
    expect(stderr).toContain("同時に指定できません");
  });

  it("search finds stations", async () => {
    // オープンデータ系は 500 でサンプルフォールバック、アメダスはローカル検索
    fetchMock.mockResolvedValue(new Response(new Uint8Array(), { status: 500 }) as unknown as Response);
    const program = buildProgram();
    const { stdout } = await run(program, ["search", "長岡"]);
    expect(stdout).toContain("長岡");
    expect(stdout).toContain("54501");
  });
});
