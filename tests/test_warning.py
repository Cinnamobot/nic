"""nic.core.warning のユニットテスト。"""

from __future__ import annotations

import threading
from datetime import datetime

import httpx
import pytest

from nic.core.warning import (
    DEFAULT_TTL,
    NIIGATA_PREF_CODE,
    SOURCE_TEXT,
    WarningArea,
    WarningClient,
    WarningData,
    WarningFetchError,
    WarningKind,
    WarningLevel,
    WarningNotFoundError,
    WarningParseError,
    _normalize_status,
    find_niigata_message_url,
    get_niigata_warnings,
    list_message_urls,
    parse_warning_xml,
)

# ---------------------------------------------------------------------------
# サンプルデータ
# ---------------------------------------------------------------------------

# 実データ（20260831050211_0_VPWW53_150000.xml）の構造を模した Atom フィード
SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>気象庁防災情報XML（高頻度・随時）</title>
  <updated>2026-08-31T05:02:30Z</updated>
  <entry>
    <title>気象特別警報・警報・注意報</title>
    <updated>2026-08-31T05:02:10Z</updated>
    <link type="application/xml" href="https://www.data.jma.go.jp/developer/xml/data/20260831050211_0_VPWW53_150000.xml"/>
    <author><name>新潟地方気象台</name></author>
  </entry>
  <entry>
    <title>気象特別警報・警報・注意報</title>
    <updated>2026-08-31T04:56:00Z</updated>
    <link type="application/xml" href="https://www.data.jma.go.jp/developer/xml/data/20260831045622_0_VPWW53_150000.xml"/>
    <author><name>新潟地方気象台</name></author>
  </entry>
  <entry>
    <title>気象特別警報・警報・注意報</title>
    <updated>2026-08-31T04:55:00Z</updated>
    <link type="application/xml" href="https://www.data.jma.go.jp/developer/xml/data/20260831045510_0_VPWW53_150000.xml"/>
    <author><name>新潟地方気象台</name></author>
  </entry>
  <entry>
    <title>気象警報・注意報</title>
    <updated>2026-08-31T04:54:00Z</updated>
    <link type="application/xml" href="https://www.data.jma.go.jp/developer/xml/data/20260831045400_0_VPWW54_150000.xml"/>
    <author><name>新潟地方気象台</name></author>
  </entry>
  <entry>
    <title>気象警報・注意報</title>
    <updated>2026-08-31T04:50:00Z</updated>
    <link type="application/xml" href="https://www.data.jma.go.jp/developer/xml/data/20260831045000_0_VPWW54_130000.xml"/>
    <author><name>長野地方気象台</name></author>
  </entry>
  <entry>
    <title>府県天気予報</title>
    <updated>2026-08-31T04:00:00Z</updated>
    <link type="application/xml" href="https://www.data.jma.go.jp/developer/xml/data/20260831040000_0_VFVO50_150000.xml"/>
    <author><name>新潟地方気象台</name></author>
  </entry>
</feed>
"""

# 実データ（20260831050211_0_VPWW53_150000.xml）の構造を模した電文 XML
SAMPLE_WARNING_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Report xmlns="http://xml.kishou.go.jp/jmaxml1/" xmlns:jmx="http://xml.kishou.go.jp/jmaxml1/" xmlns:jmx_add="http://xml.kishou.go.jp/jmaxml1/addition1/">
<Control>
<Title>気象特別警報・警報・注意報</Title>
<DateTime>2026-08-31T05:02:10Z</DateTime>
<Status>通常</Status>
<EditorialOffice>新潟地方気象台</EditorialOffice>
<PublishingOffice>新潟地方気象台</PublishingOffice>
</Control>
<Head xmlns="http://xml.kishou.go.jp/jmaxml1/informationBasis1/">
<Title>新潟県気象警報・注意報</Title>
<ReportDateTime>2026-08-31T14:02:00+09:00</ReportDateTime>
<TargetDateTime>2026-08-31T14:02:00+09:00</TargetDateTime>
<EventID/>
<InfoType>発表</InfoType>
<Serial/>
<InfoKind>気象警報・注意報</InfoKind>
<InfoKindVersion>1.1_2</InfoKindVersion>
<Headline>
<Text>中越、上越では、土砂災害や落雷に注意してください。</Text>
<Information type="気象警報・注意報（府県予報区等）">
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code></Kind>
<Areas codeType="気象情報／府県予報区・細分区域等">
<Area><Name>新潟県</Name><Code>150000</Code></Area>
</Areas>
</Item>
</Information>
<Information type="気象警報・注意報（一次細分区域等）">
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code></Kind>
<Areas codeType="気象情報／府県予報区・細分区域等">
<Area><Name>中越</Name><Code>150020</Code></Area>
</Areas>
</Item>
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code></Kind>
<Areas codeType="気象情報／府県予報区・細分区域等">
<Area><Name>上越</Name><Code>150030</Code></Area>
</Areas>
</Item>
</Information>
<Information type="気象警報・注意報（市町村等をまとめた地域等）">
<Item>
<Kind><Name>雷注意報</Name><Code>14</Code></Kind>
<Areas codeType="気象情報／府県予報区・細分区域等">
<Area><Name>十日町地域</Name><Code>150026</Code></Area>
</Areas>
</Item>
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code></Kind>
<Areas codeType="気象情報／府県予報区・細分区域等">
<Area><Name>上越市</Name><Code>150031</Code></Area>
</Areas>
</Item>
</Information>
<Information type="気象警報・注意報（市町村等）">
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code></Kind>
<Areas codeType="気象情報／府県予報区・細分区域等">
<Area><Name>十日町市</Name><Code>1521000</Code></Area>
</Areas>
</Item>
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code></Kind>
<Areas codeType="気象情報／府県予報区・細分区域等">
<Area><Name>糸魚川市</Name><Code>1521600</Code></Area>
</Areas>
</Item>
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code></Kind>
<Areas codeType="気象情報／府県予報区・細分区域等">
<Area><Name>妙高市</Name><Code>1521700</Code></Area>
</Areas>
</Item>
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code></Kind>
<Areas codeType="気象情報／府県予報区・細分区域等">
<Area><Name>上越市</Name><Code>1522200</Code></Area>
</Areas>
</Item>
</Information>
</Headline>
</Head>
<Body xmlns="http://xml.kishou.go.jp/jmaxml1/body/meteorology1/" xmlns:jmx_eb="http://xml.kishou.go.jp/jmaxml1/elementBasis1/">
<Notice>【大雨注意報・雷注意報】発表</Notice>
<Warning type="気象警報・注意報（府県予報区等）">
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code><Status>発表</Status></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code><Status>発表</Status></Kind>
<Area><Name>新潟県</Name><Code>150000</Code></Area>
<ChangeStatus>変化無</ChangeStatus>
<FullStatus>一部</FullStatus>
<EditingMark>0</EditingMark>
</Item>
</Warning>
<Warning type="気象警報・注意報（一次細分区域等）">
<Item>
<Kind><Status>発表警報・注意報はなし</Status></Kind>
<Area><Name>下越</Name><Code>150010</Code></Area>
</Item>
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code><Status>継続</Status></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code><Status>継続</Status></Kind>
<Area><Name>中越</Name><Code>150020</Code></Area>
<ChangeStatus>変化無</ChangeStatus>
<FullStatus>一部</FullStatus>
<EditingMark>0</EditingMark>
</Item>
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code><Status>継続</Status></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code><Status>継続</Status></Kind>
<Area><Name>上越</Name><Code>150030</Code></Area>
<ChangeStatus>変化無</ChangeStatus>
<FullStatus>全域</FullStatus>
<EditingMark>0</EditingMark>
</Item>
<Item>
<Kind><Status>発表警報・注意報はなし</Status></Kind>
<Area><Name>佐渡</Name><Code>150040</Code></Area>
</Item>
</Warning>
<Warning type="気象警報・注意報（市町村等をまとめた地域等）">
<Item>
<Kind><Status>発表警報・注意報はなし</Status></Kind>
<Area><Name>新発田地域</Name><Code>150011</Code></Area>
</Item>
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code><Status>継続</Status></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code><Status>継続</Status></Kind>
<Area><Name>十日町地域</Name><Code>150026</Code></Area>
</Item>
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code><Status>継続</Status></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code><Status>継続</Status></Kind>
<Area><Name>上越市</Name><Code>150031</Code></Area>
</Item>
<Item>
<Kind><Status>発表警報・注意報はなし</Status></Kind>
<Area><Name>佐渡</Name><Code>150040</Code></Area>
</Item>
</Warning>
<Warning type="気象警報・注意報（市町村等）">
<Item>
<Kind><Status>発表警報・注意報はなし</Status></Kind>
<Area><Name>新潟市</Name><Code>1510000</Code></Area>
</Item>
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code><Status>継続</Status></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code><Status>継続</Status></Kind>
<Area><Name>十日町市</Name><Code>1521000</Code></Area>
</Item>
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code><Status>継続</Status></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code><Status>継続</Status></Kind>
<Area><Name>糸魚川市</Name><Code>1521600</Code></Area>
</Item>
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code><Status>継続</Status></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code><Status>継続</Status></Kind>
<Area><Name>妙高市</Name><Code>1521700</Code></Area>
</Item>
<Item>
<Kind><Name>大雨注意報</Name><Code>10</Code><Status>継続</Status></Kind>
<Kind><Name>雷注意報</Name><Code>14</Code><Status>継続</Status></Kind>
<Area><Name>上越市</Name><Code>1522200</Code></Area>
</Item>
<Item>
<Kind><Status>発表警報・注意報はなし</Status></Kind>
<Area><Name>村上市</Name><Code>1521200</Code></Area>
</Item>
</Warning>
</Body>
</Report>
"""

MESSAGE_URL = "https://www.data.jma.go.jp/developer/xml/data/20260831050211_0_VPWW53_150000.xml"


def _xml_bytes(text: str) -> bytes:
    return text.encode("utf-8")


# ---------------------------------------------------------------------------
# フィクスチャ（httpx.Client モック）
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_http(monkeypatch):
    """httpx.Client をモックして応答を差し替えるフィクスチャ。"""

    class FakeResponse:
        def __init__(self, content: bytes, status_code: int = 200):
            self.content = content
            self.status_code = status_code

    class FakeHTTPX:
        def __init__(self):
            self.responses: dict[str, FakeResponse] = {}
            self.calls: list[str] = []

        def get(self, url, **kwargs):
            self.calls.append(url)
            resp = self.responses.get(url)
            if resp is None:
                resp = FakeResponse(b"", 404)
            return resp

        def close(self):
            pass

    fake = FakeHTTPX()
    monkeypatch.setattr("nic.core.warning.httpx.Client", lambda *a, **k: fake)
    return fake


# ---------------------------------------------------------------------------
# フィードパース
# ---------------------------------------------------------------------------


class TestFeedParse:
    def test_find_newest_niigata_message_url(self):
        """新潟県の最新の警報・注意報電文 URL を返す（VPWW53 優先）。"""
        url = find_niigata_message_url(SAMPLE_FEED)
        assert url == MESSAGE_URL

    def test_find_accepts_bytes(self):
        url = find_niigata_message_url(_xml_bytes(SAMPLE_FEED))
        assert url == MESSAGE_URL

    def test_find_returns_none_when_no_niigata_entry(self):
        feed = SAMPLE_FEED.replace("_150000.xml", "_130000.xml")
        assert find_niigata_message_url(feed) is None

    def test_find_relative_href_resolved(self):
        """相対 URL はフィードの URL を基準に絶対 URL へ解決される。"""
        feed = SAMPLE_FEED.replace(
            "https://www.data.jma.go.jp/developer/xml/data/20260831050211_0_VPWW53_150000.xml",
            "../data/20260831050211_0_VPWW53_150000.xml",
        )
        url = find_niigata_message_url(
            feed, base_url="https://www.data.jma.go.jp/developer/xml/feed/extra.xml"
        )
        assert url == MESSAGE_URL

    def test_find_invalid_feed(self):
        with pytest.raises(WarningParseError):
            find_niigata_message_url(b"<not-xml")

    def test_list_message_urls_order(self):
        """新潟県の電文 URL を新しい順（フィード出現順）に列挙する。"""
        urls = list_message_urls(SAMPLE_FEED, limit=10)
        assert urls[0] == MESSAGE_URL
        assert len(urls) == 4  # VPWW53 ×3 + VPWW54 ×1（他県・天気予報は除外）
        assert all("150000.xml" in u for u in urls)


# ---------------------------------------------------------------------------
# 電文 XML パース
# ---------------------------------------------------------------------------


class TestWarningParse:
    def test_parse_levels_order(self):
        """4 階層が 府県→一次細分→地域→市町村 の順に並ぶ。"""
        data = parse_warning_xml(_xml_bytes(SAMPLE_WARNING_XML), message_url=MESSAGE_URL)
        assert [lv.level for lv in data.levels] == ["府県", "一次細分", "地域", "市町村"]

    def test_parse_metadata(self):
        """発表日時・見出し・出典などのメタデータが取得できる。"""
        data = parse_warning_xml(_xml_bytes(SAMPLE_WARNING_XML), message_url=MESSAGE_URL)
        assert data.title == "新潟県気象警報・注意報"
        assert data.headline == "中越、上越では、土砂災害や落雷に注意してください。"
        assert data.info_type == "発表"
        assert data.editorial_office == "新潟地方気象台"
        assert data.message_kind == "VPWW53"
        assert data.message_url == MESSAGE_URL
        assert data.source == SOURCE_TEXT
        assert data.report_datetime == datetime(2026, 8, 31, 14, 2, 0, tzinfo=__import__("datetime").timezone(__import__("datetime").timedelta(hours=9)))
        assert data.report_datetime.tzinfo is not None

    def test_parse_prefecture_level(self):
        """府県階層で種別・状態・対象地域が取得できる。"""
        data = parse_warning_xml(_xml_bytes(SAMPLE_WARNING_XML))
        pref = data.level("府県")
        assert pref is not None
        assert len(pref.areas) == 1
        area = pref.areas[0]
        assert area.name == "新潟県"
        assert area.code == NIIGATA_PREF_CODE
        assert [(k.name, k.code, k.status) for k in area.kinds] == [
            ("大雨注意報", "10", "発表"),
            ("雷注意報", "14", "発表"),
        ]

    def test_parse_subdivision_level(self):
        """一次細分階層で「なし」エリアと発表エリアを判別できる。"""
        data = parse_warning_xml(_xml_bytes(SAMPLE_WARNING_XML))
        sub = data.level("一次細分")
        assert sub is not None
        by_name = {a.name: a for a in sub.areas}
        assert by_name["下越"].status_summary == "発表警報・注意報はなし"
        assert by_name["下越"].has_warning is False
        assert by_name["中越"].has_warning is True
        assert by_name["中越"].status_summary == "大雨注意報 継続、雷注意報 継続"
        assert [a.name for a in sub.active_areas] == ["中越", "上越"]

    def test_parse_municipality_level(self):
        """市町村階層の個別市町村ごとの種別が取得できる。"""
        data = parse_warning_xml(_xml_bytes(SAMPLE_WARNING_XML))
        mun = data.level("市町村")
        assert mun is not None
        active = {a.name: a for a in mun.active_areas}
        assert set(active) == {"十日町市", "糸魚川市", "妙高市", "上越市"}
        assert active["十日町市"].code == "1521000"
        assert [(k.name, k.status) for k in active["十日町市"].kinds] == [
            ("大雨注意報", "継続"),
            ("雷注意報", "継続"),
        ]

    def test_parse_accepts_string(self):
        data = parse_warning_xml(SAMPLE_WARNING_XML)
        assert data.level("府県") is not None

    def test_parse_invalid_xml(self):
        with pytest.raises(WarningParseError):
            parse_warning_xml(b"<Report><unclosed>")

    def test_parse_missing_warning_section(self):
        """<Warning> が無い XML はパースエラー。"""
        no_body = SAMPLE_WARNING_XML.replace("<Body", "<NotBody").replace("</Body>", "</NotBody>")
        with pytest.raises(WarningParseError):
            parse_warning_xml(no_body)

    def test_data_helpers(self):
        """WarningData のヘルパー（active_kinds / summary / get_areas）。"""
        data = parse_warning_xml(_xml_bytes(SAMPLE_WARNING_XML))
        assert [(k.name, k.status) for k in data.active_kinds] == [
            ("大雨注意報", "発表"),
            ("雷注意報", "発表"),
        ]
        assert data.summary == "大雨注意報 発表、雷注意報 発表"
        assert data.level("存在しない階層") is None
        assert data.get_areas("存在しない階層") == ()
        assert data.get_active_areas("存在しない階層") == ()


# ---------------------------------------------------------------------------
# 状態の正規化
# ---------------------------------------------------------------------------


class TestStatusNormalize:
    def test_keep_announced(self):
        assert _normalize_status("発表") == "発表"

    def test_keep_continued(self):
        assert _normalize_status("継続") == "継続"

    def test_none_text(self):
        assert _normalize_status("発表警報・注意報はなし") == "発表警報・注意報はなし"

    def test_empty_is_none(self):
        assert _normalize_status("") == "発表警報・注意報はなし"

    def test_switch_mapping(self):
        assert _normalize_status("注意報解除") == "解除"
        assert _normalize_status("特別警報から警報に切り替え") == "特別警報から警報に切り替え"


# ---------------------------------------------------------------------------
# キャッシュ・取得（httpx モック使用）
# ---------------------------------------------------------------------------


class TestClientFetch:
    def _setup(self, mock_http):
        mock_http.responses["https://www.data.jma.go.jp/developer/xml/feed/extra.xml"] = type(
            "R", (), {"content": _xml_bytes(SAMPLE_FEED), "status_code": 200}
        )()
        mock_http.responses[MESSAGE_URL] = type(
            "R", (), {"content": _xml_bytes(SAMPLE_WARNING_XML), "status_code": 200}
        )()

    def test_fetch_end_to_end(self, mock_http):
        """フィード → 電文 XML の 2 段階で警報・注意報を取得する。"""
        self._setup(mock_http)
        with WarningClient() as client:
            data = client.fetch()
        assert data.message_kind == "VPWW53"
        assert [lv.level for lv in data.levels] == ["府県", "一次細分", "地域", "市町村"]
        assert mock_http.calls == [
            "https://www.data.jma.go.jp/developer/xml/feed/extra.xml",
            MESSAGE_URL,
        ]

    def test_fetch_no_niigata_entry(self, mock_http):
        """フィードに新潟県の電文が無ければ WarningNotFoundError。"""
        feed = SAMPLE_FEED.replace("_150000.xml", "_130000.xml")
        mock_http.responses[
            "https://www.data.jma.go.jp/developer/xml/feed/extra.xml"
        ] = type("R", (), {"content": _xml_bytes(feed), "status_code": 200})()
        with WarningClient() as client:
            with pytest.raises(WarningNotFoundError):
                client.fetch()

    def test_fetch_feed_404(self, mock_http):
        """フィードの取得失敗（404）は WarningFetchError。"""
        # 何も登録しない → 404
        with WarningClient() as client:
            with pytest.raises(WarningFetchError) as exc_info:
                client.fetch()
        assert "404" in str(exc_info.value)

    def test_fetch_http_error(self, mock_http, monkeypatch):
        """通信エラーは WarningFetchError に変換される。"""

        class Boom:
            def get(self, url, **kwargs):
                raise httpx.ConnectError("boom")

            def close(self):
                pass

        monkeypatch.setattr("nic.core.warning.httpx.Client", lambda *a, **k: Boom())
        with WarningClient() as client:
            with pytest.raises(WarningFetchError):
                client.fetch()

    def test_fetch_message_404(self, mock_http):
        """電文 XML の取得失敗（404）は WarningFetchError。"""
        mock_http.responses[
            "https://www.data.jma.go.jp/developer/xml/feed/extra.xml"
        ] = type("R", (), {"content": _xml_bytes(SAMPLE_FEED), "status_code": 200})()
        # MESSAGE_URL を登録しない → 404
        with WarningClient() as client:
            with pytest.raises(WarningFetchError) as exc_info:
                client.fetch()
        assert "404" in str(exc_info.value)


class TestCache:
    def _setup(self, mock_http):
        mock_http.responses["https://www.data.jma.go.jp/developer/xml/feed/extra.xml"] = type(
            "R", (), {"content": _xml_bytes(SAMPLE_FEED), "status_code": 200}
        )()
        mock_http.responses[MESSAGE_URL] = type(
            "R", (), {"content": _xml_bytes(SAMPLE_WARNING_XML), "status_code": 200}
        )()

    def test_ttl_inside(self, mock_http):
        """TTL 内はキャッシュが使われ再取得しない。"""
        self._setup(mock_http)
        with WarningClient(ttl=300) as client:
            client.fetch()
            client.fetch()
            client.fetch()
        # フィード取得は 1 回だけ
        assert mock_http.calls.count("https://www.data.jma.go.jp/developer/xml/feed/extra.xml") == 1

    def test_ttl_expiry(self, mock_http):
        """TTL 経過後は再取得する。"""
        self._setup(mock_http)
        with WarningClient(ttl=0.05) as client:
            client.fetch()
            import time

            time.sleep(0.06)
            client.fetch()
        assert mock_http.calls.count("https://www.data.jma.go.jp/developer/xml/feed/extra.xml") == 2

    def test_force(self, mock_http):
        """force=True でキャッシュを無視する。"""
        self._setup(mock_http)
        with WarningClient(ttl=300) as client:
            client.fetch()
            client.fetch(force=True)
        assert mock_http.calls.count("https://www.data.jma.go.jp/developer/xml/feed/extra.xml") == 2

    def test_thread_safety(self, mock_http):
        """複数スレッドから同時に呼んでも安全。"""
        self._setup(mock_http)
        results: list[WarningData] = []
        errors: list[Exception] = []

        def worker():
            try:
                with WarningClient(ttl=60) as client:
                    results.append(client.fetch())
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(results) == 8
        assert all(d.message_kind == "VPWW53" for d in results)


class TestModuleFunctions:
    def test_get_niigata_warnings(self, mock_http):
        """モジュール関数は 1 コールで取得できる。"""
        mock_http.responses["https://www.data.jma.go.jp/developer/xml/feed/extra.xml"] = type(
            "R", (), {"content": _xml_bytes(SAMPLE_FEED), "status_code": 200}
        )()
        mock_http.responses[MESSAGE_URL] = type(
            "R", (), {"content": _xml_bytes(SAMPLE_WARNING_XML), "status_code": 200}
        )()
        data = get_niigata_warnings()
        assert data.source == SOURCE_TEXT
        assert data.level("府県") is not None

    def test_constants(self):
        assert DEFAULT_TTL == 60.0
        assert NIIGATA_PREF_CODE == "150000"

    def test_dataclass_defaults(self):
        """データクラスのデフォルト値（出典など）が正しい。"""
        area = WarningArea(name="新潟県", code=NIIGATA_PREF_CODE)
        assert area.kinds == ()
        assert area.has_warning is False
        assert area.status_summary == "発表警報・注意報はなし"
        level = WarningLevel(level="府県", type_label="気象警報・注意報（府県予報区等）")
        assert level.active_areas == ()
        kind = WarningKind(name="大雨注意報", code="10", status="発表")
        assert (kind.name, kind.code, kind.status) == ("大雨注意報", "10", "発表")
