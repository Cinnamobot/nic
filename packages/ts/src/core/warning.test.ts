import { describe, expect, it } from "vitest";
import {
  findNiigataMessageUrl,
  listMessageUrls,
  parseWarningXml,
  WarningParseError,
  WarningArea,
  WarningData,
  STATUS_NONE,
  summary,
  activeKinds,
  hasWarning,
  statusSummary,
  getAreas,
  getActiveAreas,
} from "../core/warning.js";

const SAMPLE_FEED = `<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" lang="ja">
  <title>高頻度（随時）</title>
  <entry>
    <title>気象警報・注意報</title>
    <link href="https://www.data.jma.go.jp/developer/xml/data/20260901030746_0_VPWW54_474000.xml"/>
  </entry>
  <entry>
    <title>気象特別警報・警報・注意報</title>
    <link href="https://www.data.jma.go.jp/developer/xml/data/20260901010540_0_VPWW53_150000.xml"/>
  </entry>
</feed>
`;

const SAMPLE_MESSAGE = `<?xml version="1.0" encoding="UTF-8"?>
<Report xmlns="http://xml.kishou.go.jp/jmaxml1/" xmlns:jmx="http://xml.kishou.go.jp/jmaxml1/">
  <Control>
    <Title>気象特別警報・警報・注意報</Title>
    <DateTime>2026-09-01T01:05:39Z</DateTime>
    <Status>通常</Status>
    <EditorialOffice>新潟地方気象台</EditorialOffice>
    <PublishingOffice>新潟地方気象台</PublishingOffice>
  </Control>
  <Head xmlns="http://xml.kishou.go.jp/jmaxml1/informationBasis1/">
    <Title>新潟県気象警報・注意報</Title>
    <ReportDateTime>2026-09-01T10:05:00+09:00</ReportDateTime>
    <TargetDateTime>2026-09-01T10:05:00+09:00</TargetDateTime>
    <InfoType>発表</InfoType>
    <Headline>
      <Text>中越、上越では、落雷に注意してください。</Text>
    </Headline>
  </Head>
  <Body xmlns="http://xml.kishou.go.jp/jmaxml1/body/meteorology1/">
    <Warning type="気象警報・注意報（府県予報区等）">
      <Item>
        <Kind>
          <Name>雷注意報</Name>
          <Code>14</Code>
          <Status>発表</Status>
        </Kind>
        <Area>
          <Name>新潟県</Name>
          <Code>150000</Code>
        </Area>
      </Item>
    </Warning>
    <Warning type="気象警報・注意報（市町村等）">
      <Item>
        <Kind>
          <Name>雷注意報</Name>
          <Code>14</Code>
          <Status>発表</Status>
        </Kind>
        <Area>
          <Name>新潟市</Name>
          <Code>1520100</Code>
        </Area>
      </Item>
    </Warning>
  </Body>
</Report>
`;

describe("findNiigataMessageUrl", () => {
  it("finds the VPWW53 message for Niigata pref", () => {
    const url = findNiigataMessageUrl(SAMPLE_FEED);
    expect(url).toBe(
      "https://www.data.jma.go.jp/developer/xml/data/20260901010540_0_VPWW53_150000.xml",
    );
  });

  it("returns null when no Niigata message", () => {
    const feed = SAMPLE_FEED.replace("VPWW53_150000", "VPWW53_474000");
    expect(findNiigataMessageUrl(feed)).toBeNull();
  });

  it("raises for non-feed XML", () => {
    expect(() => findNiigataMessageUrl("<html></html>")).toThrow(WarningParseError);
  });
});

describe("listMessageUrls", () => {
  it("lists all Niigata message URLs", () => {
    const urls = listMessageUrls(SAMPLE_FEED);
    expect(urls).toHaveLength(1);
    expect(urls[0]).toContain("VPWW53_150000.xml");
  });
});

describe("parseWarningXml", () => {
  it("parses 4-level warnings", () => {
    const data = parseWarningXml(SAMPLE_MESSAGE);
    expect(data.title).toBe("新潟県気象警報・注意報");
    expect(data.editorialOffice).toBe("新潟地方気象台");
    expect(data.infoType).toBe("発表");
    expect(data.headline).toContain("落雷");
    expect(data.messageKind).toBe("VPWW53");

    const levels = data.levels;
    expect(levels).toHaveLength(2);
    expect(levels[0].level).toBe("府県");
    expect(levels[0].areas[0].name).toBe("新潟県");
    expect(levels[0].areas[0].kinds[0].name).toBe("雷注意報");
    expect(levels[0].areas[0].kinds[0].status).toBe("発表");
    expect(levels[1].level).toBe("市町村");
  });

  it("raises when no Warning section", () => {
    const xml = SAMPLE_MESSAGE.replace(/<Body.*?<\/Body>/s, "<Body></Body>");
    expect(() => parseWarningXml(xml)).toThrow(WarningParseError);
  });
});

describe("WarningData helpers", () => {
  const data: WarningData = parseWarningXml(SAMPLE_MESSAGE);

  it("getAreas returns areas for level", () => {
    expect(getAreas(data, "府県")).toHaveLength(1);
    expect(getAreas(data, "一次細分")).toHaveLength(0);
  });

  it("getActiveAreas filters active only", () => {
    expect(getActiveAreas(data, "府県")).toHaveLength(1);
  });

  it("hasWarning / statusSummary", () => {
    const area: WarningArea = data.levels[0].areas[0];
    expect(hasWarning(area)).toBe(true);
    expect(statusSummary(area)).toBe("雷注意報 発表");
  });

  it("statusSummary for no-warning area", () => {
    const area: WarningArea = { name: "新潟県", code: "150000", kinds: [] };
    expect(statusSummary(area)).toBe(STATUS_NONE);
  });

  it("activeKinds and summary", () => {
    expect(activeKinds(data)).toHaveLength(1);
    expect(activeKinds(data)[0].name).toBe("雷注意報");
    expect(summary(data)).toBe("雷注意報 発表");
  });
});
