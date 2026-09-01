"""nic.mcp.server のユニットテスト。

mcp SDK のインメモリ転送（create_connected_server_and_client_session）を
使って FastMCP サーバーに接続し、ツール一覧・ツール呼び出し・
コア関数のフェイクによる引数伝播を検証する。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent, Tool

from nic.core.amedas import (
    AmedasData,
    AmedasElement,
    AmedasFetchError,
    Observation,
    Station,
)
from nic.core.opendata import Dataset, MichiNoEki, OpenDataError, PopulationRecord
from nic.core.tourism import (
    P33_SOURCE_TEXT,
    SOURCE_TEXT as TOURISM_SOURCE_TEXT,
    Spot,
    TourismClient,
    TourismDataset,
    TourismError,
    TourismStat,
)
from nic.core.warning import (
    NIIGATA_PREF_CODE,
    SOURCE_TEXT as WARNING_SOURCE_TEXT,
    WarningArea,
    WarningData,
    WarningError,
    WarningKind,
    WarningLevel,
)
from nic.mcp.server import (
    MCP_TTL,
    mcp,
)

pytestmark = pytest.mark.anyio

# ---------------------------------------------------------------------------
# サンプルデータ
# ---------------------------------------------------------------------------

_ST_NIIGATA = Station("54232", "新潟", 37.8933, 139.0183, 4, "A", "11111111")
_ST_YUZAWA = Station("54841", "湯沢", 36.9417, 138.8100, 340, "C", "11112110")

_OBS_TIME = datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)


def _obs(station: Station, value: float | None, quality: int = 8) -> Observation:
    return Observation(
        station=station,
        value=value,
        quality=quality,
        quality_text="正常値" if quality == 8 else "疑問値",
        observed_at=_OBS_TIME,
    )


def _amedas_data(element: AmedasElement) -> AmedasData:
    return AmedasData(
        element=element,
        observations=[
            _obs(_ST_NIIGATA, 12.0),
            _obs(_ST_YUZAWA, 210.0),
        ],
        fetched_at=_OBS_TIME,
    )


def _datasets() -> list[Dataset]:
    return [
        Dataset(
            id="234",
            name="人口時系列データ(市町村別)",
            category="人口・世帯",
            description="大正９年からの市町村別人口データを掲載。",
            fields="新潟県の人口総数、各歳人口合計、男女別数。",
            fiscal_year="R5",
            update_frequency="毎月",
            format="CSV",
            url="https://www.pref.niigata.lg.jp/site/tokei/1282075307357.html",
            department="統計課",
        ),
        Dataset(
            id="731",
            name="新潟県道の駅",
            category="運輸・観光",
            description="県内道の駅の名簿",
            fields="名称、路線名、所在地、電話番号",
            fiscal_year="R4",
            update_frequency="不定期",
            format="Excel",
            url="http://www.pref.niigata.lg.jp/dourokanri/1202317264067.html",
            department="道路管理課",
        ),
    ]


def _population() -> list[PopulationRecord]:
    return [
        PopulationRecord(
            date="2024/10/1 0:00",
            municipality_code="15201",
            municipality_name="新潟市",
            total=772425,
            male=372208,
            female=400217,
        ),
        PopulationRecord(
            date="2024/10/1 0:00",
            municipality_code="15204",
            municipality_name="三条市",
            total=93335,
            male=44951,
            female=48384,
        ),
    ]


def _michinoeki() -> list[MichiNoEki]:
    return [
        MichiNoEki(
            id=1,
            name="豊栄",
            route="一般国道7号",
            address="新潟市北区木崎字切尾山3644-乙",
            phone="025-388-2700",
        ),
        MichiNoEki(
            id=2,
            name="加治川（さくらの里）",
            route="一般国道7号",
            address="新発田市横岡1147",
            phone="0254-33-3175",
        ),
    ]


def _tourist_spots() -> list[Spot]:
    return [
        Spot(
            id="onsen-28",
            name="ほてる大橋館の湯",
            category="温泉",
            lat=37.7380947,
            lon=138.8398538,
            address="新潟市西蒲区岩室温泉340-甲",
            phone="0256-82-4125",
            url="",
            description="岩室温泉（含硫黄－ナトリウム･カルシウム－塩化物泉）",
            source=TOURISM_SOURCE_TEXT,
            source_url="https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/",
        ),
        Spot(
            id="p33-1",
            name="シネ・ウインド",
            category="集客施設（映画館）",
            lat=37.915809210064,
            lon=139.05391640076,
            address="新潟市中央区八千代2-1-1（1F）",
            phone="025-243-5530",
            url="http://cinewind.com/",
            description="映画館",
            source=P33_SOURCE_TEXT,
            source_url="https://nlftp.mlit.go.jp/ksj/gml/gisdata.html",
        ),
        Spot(
            id="p33-10",
            name="川前公民館",
            category="集客施設（公会堂）",
            lat=None,
            lon=None,
            address="燕市中川597-1",
            phone="0256-63-9310",
            url="",
            description="公会堂",
            source=P33_SOURCE_TEXT,
            source_url="https://nlftp.mlit.go.jp/ksj/gml/gisdata.html",
        ),
    ]


def _tour_stats() -> list[TourismStat]:
    return [
        TourismStat(
            year=2024,
            era_year="令和6",
            total=16019,
            event_total=4591,
            spot_total=11428,
            nature=425,
            history_culture=3044,
            onsen_health=861,
            sports_recreation=2026,
            urban_tourism=5072,
            other=0,
            source=TOURISM_SOURCE_TEXT,
            source_url="https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-kankou/od-irikomidata.html",
        ),
        TourismStat(
            year=2023,
            era_year="令和5",
            total=15557,
            event_total=4382,
            spot_total=11175,
            nature=419,
            history_culture=3100,
            onsen_health=818,
            sports_recreation=1792,
            urban_tourism=5046,
            other=0,
            source=TOURISM_SOURCE_TEXT,
            source_url="https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-kankou/od-irikomidata.html",
        ),
    ]


def _tour_datasets() -> list[TourismDataset]:
    return [
        TourismDataset(
            id="16a13911-06c9-4339-aec6-30c092846c83",
            name="opendata-kankou_od-irikomidata",
            title="新潟市観光入込客数",
            description="年別・分類別の観光入込客数",
            license="クリエイティブ・コモンズ 表示",
            license_url="http://www.opendefinition.org/licenses/cc-by",
            updated_at="2026-03-04T06:03:28.768642",
            url="https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-kankou/od-irikomidata.html",
            resources=(
                "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-kankou/od-irikomidata.files/irikomidataR6.csv",
            ),
            source=TOURISM_SOURCE_TEXT,
            source_url="http://opendata.city.niigata.lg.jp/api/3/action/package_search",
        ),
    ]


def _warning_data() -> WarningData:
    pref_kinds = (
        WarningKind(name="大雨注意報", code="10", status="継続"),
        WarningKind(name="雷注意報", code="14", status="継続"),
    )
    none_kind = WarningKind(name="", code="", status="発表警報・注意報はなし")
    return WarningData(
        title="新潟県気象警報・注意報",
        headline="中越、上越では、土砂災害や落雷に注意してください。",
        info_type="発表",
        report_datetime=datetime(2026, 8, 31, 5, 2, tzinfo=timezone.utc),
        editorial_office="新潟地方気象台",
        message_kind="VPWW53",
        message_url="https://www.data.jma.go.jp/developer/xml/data/20260831050211_0_VPWW53_150000.xml",
        levels=(
            WarningLevel(
                level="府県",
                type_label="気象警報・注意報（府県予報区等）",
                areas=(
                    WarningArea(
                        name="新潟県",
                        code=NIIGATA_PREF_CODE,
                        kinds=pref_kinds,
                    ),
                ),
            ),
            WarningLevel(
                level="一次細分",
                type_label="気象警報・注意報（一次細分区域等）",
                areas=(
                    WarningArea(
                        name="中越", code="150020", kinds=pref_kinds
                    ),
                    WarningArea(
                        name="上越",
                        code="150030",
                        kinds=(
                            WarningKind(name="大雨注意報", code="10", status="解除"),
                        ),
                    ),
                ),
            ),
            WarningLevel(
                level="市町村",
                type_label="気象警報・注意報（市町村等）",
                areas=(
                    WarningArea(
                        name="新潟市", code="1520100", kinds=pref_kinds
                    ),
                    WarningArea(
                        name="上越市",
                        code="1522200",
                        kinds=(none_kind,),
                    ),
                ),
            ),
        ),
        fetched_at=datetime(2026, 8, 31, 5, 2, 30, tzinfo=timezone.utc),
        source=WARNING_SOURCE_TEXT,
        source_url="https://www.data.jma.go.jp/developer/xml/feed/extra.xml",
    )


# ---------------------------------------------------------------------------
# フェイククライアント
# ---------------------------------------------------------------------------


class FakeAmedasClient:
    """AmedasClient のフェイク（fetch / get_stations の呼び出し記録付き）。"""

    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def get_stations(self):
        return [_ST_NIIGATA, _ST_YUZAWA]

    def fetch(self, element: AmedasElement, *, codes=None, force: bool = False) -> AmedasData:
        self.calls.append({"element": element, "codes": codes, "force": force})
        if self.error is not None:
            raise self.error
        data = _amedas_data(element)
        if codes is not None:
            data.observations = [o for o in data.observations if o.station.code in codes]
        return data


class FakeOpenDataClient:
    """OpenDataClient のフェイク（呼び出し記録付き）。"""

    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.calls: list[dict] = []
        self.warnings = ["外部データ源を利用できなかったため、内蔵サンプルデータを返します。"]

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def get_datasets(self, *, query=None, category=None, data_format=None, force=False):
        self.calls.append(
            {"method": "get_datasets", "query": query, "category": category,
             "data_format": data_format, "force": force}
        )
        if self.error is not None:
            raise self.error
        ds = _datasets()
        if query:
            ds = [d for d in ds if query in d.name or query in d.description]
        if category:
            ds = [d for d in ds if category in d.category]
        if data_format:
            ds = [d for d in ds if d.format.lower() == data_format.lower()]
        return ds

    def get_population(self, *, municipality=None, force=False):
        self.calls.append(
            {"method": "get_population", "municipality": municipality, "force": force}
        )
        if self.error is not None:
            raise self.error
        records = _population()
        if municipality:
            records = [r for r in records if municipality in r.municipality_name]
        return records

    def get_tourism(self, *, force=False):
        self.calls.append({"method": "get_tourism", "force": force})
        if self.error is not None:
            raise self.error
        return _michinoeki()


class FakeTourismClient:
    """TourismClient のフェイク（呼び出し記録付き）。"""

    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.calls: list[dict] = []
        self.warnings = ["外部データ源を利用できなかったため、内蔵サンプルデータを返します。"]

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def get_spots(self, *, category=None, include_onsen=True, include_p33=True, force=False):
        self.calls.append(
            {"method": "get_spots", "category": category, "force": force}
        )
        if self.error is not None:
            raise self.error
        spots = _tourist_spots()
        if category:
            spots = [s for s in spots if s.category == category]
        return spots

    def get_irikomi(self, *, year=None, force=False):
        self.calls.append({"method": "get_irikomi", "year": year, "force": force})
        if self.error is not None:
            raise self.error
        stats = _tour_stats()
        if year is not None:
            stats = [s for s in stats if s.year == year]
        return stats

    def get_datasets(self, *, query=None, force=False):
        self.calls.append({"method": "get_datasets", "query": query, "force": force})
        if self.error is not None:
            raise self.error
        ds = _tour_datasets()
        if query:
            ds = [d for d in ds if query in d.title or query in d.name]
        return ds


class FakeWarningClient:
    """WarningClient のフェイク（fetch の呼び出し記録付き）。"""

    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def fetch(self, *, force: bool = False, feed_url: str = ""):
        self.calls.append({"force": force})
        if self.error is not None:
            raise self.error
        return _warning_data()


@pytest.fixture
def fake_amedas(monkeypatch):
    fake = FakeAmedasClient()
    monkeypatch.setattr("nic.mcp.server.AmedasClient", lambda **kwargs: fake)
    return fake


@pytest.fixture
def fake_opendata(monkeypatch):
    fake = FakeOpenDataClient()
    monkeypatch.setattr("nic.mcp.server.OpenDataClient", lambda **kwargs: fake)
    return fake


@pytest.fixture
def fake_tourism(monkeypatch):
    fake = FakeTourismClient()
    monkeypatch.setattr("nic.mcp.server.TourismClient", lambda **kwargs: fake)
    return fake


@pytest.fixture
def fake_warning(monkeypatch):
    fake = FakeWarningClient()
    monkeypatch.setattr("nic.mcp.server.WarningClient", lambda **kwargs: fake)
    return fake


# ---------------------------------------------------------------------------
# 接続ヘルパー
# ---------------------------------------------------------------------------


def _text_of(result: CallToolResult) -> str:
    """CallToolResult のテキストコンテンツを 1 つの文字列に連結する。"""
    parts = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
        else:
            parts.append(str(block))
    return "\n".join(parts)


def _json_of(result: CallToolResult) -> dict:
    """CallToolResult のテキストコンテンツを JSON として解釈する。"""
    return json.loads(_text_of(result))


# ---------------------------------------------------------------------------
# ツール一覧・スキーマ
# ---------------------------------------------------------------------------


class TestToolRegistry:
    async def test_lists_seven_tools(self):
        """7 ツールが登録され、一覧で取得できる。"""
        async with create_connected_server_and_client_session(mcp) as session:
            tools_result = await session.list_tools()
        tools = tools_result.tools
        names = {t.name for t in tools}
        assert names == {
            "get_snow_info",
            "get_weather_info",
            "get_niigata_stats",
            "get_tourist_spots",
            "get_tour_recommendation",
            "get_warning_info",
            "search_niigata_data",
        }

    async def test_tool_descriptions_and_input_schemas(self):
        """各ツールに説明文と入力スキーマが設定されている。"""
        async with create_connected_server_and_client_session(mcp) as session:
            tools = (await session.list_tools()).tools
        by_name = {t.name: t for t in tools}

        snow: Tool = by_name["get_snow_info"]
        assert "積雪" in snow.description
        assert "冬季" in snow.description
        schema = snow.inputSchema
        station_schema = schema["properties"]["station_codes"]
        # 省略可能な list[str] は anyOf [array, null] として表現される
        any_of = station_schema.get("anyOf")
        if any_of is not None:
            assert any(t.get("type") == "array" for t in any_of)
        else:
            assert station_schema["type"] == "array"
        assert schema["properties"]["limit"]["default"] == 50
        assert schema["properties"]["force"]["type"] == "boolean"

        weather: Tool = by_name["get_weather_info"]
        assert "気温" in weather.description and "降水量" in weather.description

        stats: Tool = by_name["get_niigata_stats"]
        assert "人口" in stats.description and "道の駅" in stats.description
        stats_schema = stats.inputSchema
        assert stats_schema["properties"]["data_type"]["default"] == "datasets"
        assert "municipality" in stats_schema["properties"]
        assert "data_format" in stats_schema["properties"]

        search: Tool = by_name["search_niigata_data"]
        assert "横断検索" in search.description
        search_schema = search.inputSchema
        assert search_schema["required"] == ["keyword"]
        assert search_schema["properties"]["keyword"]["type"] == "string"

        spots: Tool = by_name["get_tourist_spots"]
        assert "観光スポット" in spots.description
        assert "温泉" in spots.description
        spots_schema = spots.inputSchema
        category_schema = spots_schema["properties"]["category"]
        any_of = category_schema.get("anyOf")
        if any_of is not None:
            assert any(t.get("type") == "string" for t in any_of)
        else:
            assert category_schema["type"] == "string"
        assert "keyword" in spots_schema["properties"]
        assert spots_schema["properties"]["limit"]["default"] == 50

        tour: Tool = by_name["get_tour_recommendation"]
        assert "観光ルート" in tour.description
        tour_schema = tour.inputSchema
        area_schema = tour_schema["properties"]["area"]
        any_of = area_schema.get("anyOf")
        if any_of is not None:
            assert any(t.get("type") == "string" for t in any_of)
        else:
            assert area_schema["type"] == "string"
        assert "limit" in tour_schema["properties"]

        warning: Tool = by_name["get_warning_info"]
        assert "警報" in warning.description and "注意報" in warning.description
        warning_schema = warning.inputSchema
        assert warning_schema["properties"]["level"]["default"] == "府県"
        assert warning_schema["properties"]["active_only"]["type"] == "boolean"
        assert warning_schema["properties"]["force"]["type"] == "boolean"


# ---------------------------------------------------------------------------
# get_snow_info
# ---------------------------------------------------------------------------


class TestGetSnowInfo:
    async def test_returns_snow_ranking(self, fake_amedas):
        """全観測所の積雪を多い順（ランキング付き）で返す。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool("get_snow_info", {})
        assert not result.isError
        payload = _json_of(result)
        assert payload["element"] == "snow"
        assert payload["unit"] == "cm"
        assert payload["source"] == "出典:気象庁"
        assert len(payload["observations"]) == 2
        assert payload["observations"][0]["station_name"] == "湯沢"
        assert payload["observations"][0]["rank"] == 1
        assert payload["observations"][0]["value"] == 210.0
        assert fake_amedas.calls[0]["element"] is AmedasElement.SNOW
        assert fake_amedas.calls[0]["codes"] is None

    async def test_station_codes_passed_to_core(self, fake_amedas):
        """station_codes がコアに伝播する。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool(
                "get_snow_info", {"station_codes": ["54232", "54841"]}
            )
        assert not result.isError
        assert fake_amedas.calls[0]["codes"] == ["54232", "54841"]
        payload = _json_of(result)
        assert len(payload["observations"]) == 2

    async def test_force_propagated(self, fake_amedas):
        """force がコアに伝播する。"""
        async with create_connected_server_and_client_session(mcp) as session:
            await session.call_tool("get_snow_info", {"force": True})
        assert fake_amedas.calls[0]["force"] is True

    async def test_missing_values_excluded(self, fake_amedas, monkeypatch):
        """欠測（value=None）は結果から除外される。"""
        data = AmedasData(
            element=AmedasElement.SNOW,
            observations=[
                _obs(_ST_NIIGATA, None, quality=1),
                _obs(_ST_YUZAWA, 210.0),
            ],
            fetched_at=_OBS_TIME,
        )
        fake = FakeAmedasClient()
        fake.fetch = lambda element, codes=None, force=False: data
        monkeypatch.setattr("nic.mcp.server.AmedasClient", lambda **kwargs: fake)
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool("get_snow_info", {})
        assert not result.isError
        payload = _json_of(result)
        assert len(payload["observations"]) == 1
        assert payload["observations"][0]["station_name"] == "湯沢"

    async def test_core_error_is_error_result(self, fake_amedas):
        """コアのエラー（夏季の 404 など）は isError=True で返る。"""
        fake_amedas.error = AmedasFetchError(
            "気象庁 CSV が取得できません (HTTP 404)。提供休止の可能性"
        )
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool("get_snow_info", {})
        assert result.isError
        assert "404" in _text_of(result)


# ---------------------------------------------------------------------------
# get_weather_info
# ---------------------------------------------------------------------------


class TestGetWeatherInfo:
    async def test_returns_weather_records(self, fake_amedas):
        """最高・最低気温と降水量を観測所ごとに返す。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool("get_weather_info", {})
        assert not result.isError
        payload = _json_of(result)
        assert payload["element"] == "temperature_precipitation"
        assert payload["unit"]["max_temp"] == "℃"
        assert payload["source"] == "出典:気象庁"
        assert len(payload["records"]) == 2
        rec = payload["records"][0]
        assert rec["station_code"] in ("54232", "54841")
        assert rec["max_temp"] == 12.0
        assert rec["min_temp"] == 12.0
        assert rec["precipitation"] == 12.0
        # 3 要素を取得している
        elements = [c["element"] for c in fake_amedas.calls]
        assert AmedasElement.MAX_TEMP in elements
        assert AmedasElement.MIN_TEMP in elements
        assert AmedasElement.PRECIPITATION in elements

    async def test_station_codes_passed_to_core(self, fake_amedas):
        """station_codes が全要素の取得に伝播する。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool(
                "get_weather_info", {"station_codes": ["54232"]}
            )
        assert not result.isError
        assert all(c["codes"] == ["54232"] for c in fake_amedas.calls)
        payload = _json_of(result)
        assert len(payload["records"]) == 1

    async def test_force_propagated(self, fake_amedas):
        async with create_connected_server_and_client_session(mcp) as session:
            await session.call_tool("get_weather_info", {"force": True})
        assert all(c["force"] is True for c in fake_amedas.calls)


# ---------------------------------------------------------------------------
# get_niigata_stats
# ---------------------------------------------------------------------------


class TestGetNiigataStats:
    async def test_default_datasets(self, fake_opendata):
        """デフォルトでデータセット一覧を返す。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool("get_niigata_stats", {})
        assert not result.isError
        payload = _json_of(result)
        assert payload["type"] == "datasets"
        assert payload["count"] == 2
        assert payload["datasets"][0]["name"] == "人口時系列データ(市町村別)"
        assert fake_opendata.calls[0]["method"] == "get_datasets"

    async def test_datasets_filters(self, fake_opendata):
        """query / category / data_format がコアに伝播する。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool(
                "get_niigata_stats",
                {"data_type": "datasets", "query": "道の駅", "category": "運輸・観光",
                 "data_format": "Excel"},
            )
        assert not result.isError
        call = fake_opendata.calls[0]
        assert call["query"] == "道の駅"
        assert call["category"] == "運輸・観光"
        assert call["data_format"] == "Excel"
        payload = _json_of(result)
        assert len(payload["datasets"]) == 1
        assert payload["datasets"][0]["name"] == "新潟県道の駅"

    async def test_population(self, fake_opendata):
        """人口時系列データを返す。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool(
                "get_niigata_stats", {"data_type": "population"}
            )
        assert not result.isError
        payload = _json_of(result)
        assert payload["type"] == "population"
        assert payload["records"][0]["municipality_name"] == "新潟市"
        assert payload["records"][0]["total"] == 772425
        assert fake_opendata.calls[0]["method"] == "get_population"
        assert fake_opendata.calls[0]["municipality"] is None

    async def test_population_municipality(self, fake_opendata):
        """municipality がコアに伝播する。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool(
                "get_niigata_stats", {"data_type": "population", "municipality": "三条"}
            )
        assert not result.isError
        assert fake_opendata.calls[0]["municipality"] == "三条"
        payload = _json_of(result)
        assert len(payload["records"]) == 1
        assert payload["records"][0]["municipality_name"] == "三条市"

    async def test_michinoeki(self, fake_opendata):
        """道の駅一覧を返す。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool(
                "get_niigata_stats", {"data_type": "michinoeki"}
            )
        assert not result.isError
        payload = _json_of(result)
        assert payload["type"] == "michinoeki"
        assert payload["stations"][0]["name"] == "豊栄"
        assert fake_opendata.calls[0]["method"] == "get_tourism"

    async def test_invalid_data_type_is_error(self, fake_opendata):
        """不正な data_type は isError=True で返る。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool(
                "get_niigata_stats", {"data_type": "invalid"}
            )
        assert result.isError
        assert "datasets" in _text_of(result)

    async def test_force_propagated(self, fake_opendata):
        async with create_connected_server_and_client_session(mcp) as session:
            await session.call_tool("get_niigata_stats", {"force": True})
        assert fake_opendata.calls[0]["force"] is True

    async def test_core_error_is_error_result(self, fake_opendata):
        """コアのエラーは isError=True で返る。"""
        fake_opendata.error = OpenDataError("データを取得できませんでした")
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool("get_niigata_stats", {})
        assert result.isError
        assert "データを取得できませんでした" in _text_of(result)


# ---------------------------------------------------------------------------
# search_niigata_data
# ---------------------------------------------------------------------------


class TestSearchNiigataData:
    async def test_searches_all_categories(self, fake_amedas, fake_opendata):
        """キーワードで 4 カテゴリを横断検索する。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool("search_niigata_data", {"keyword": "新潟"})
        assert not result.isError
        payload = _json_of(result)
        assert payload["keyword"] == "新潟"
        # 新潟: 観測所（新潟）+ 人口（新潟市）+ 道の駅住所（新潟市北区...）
        assert len(payload["stations"]) >= 1
        assert len(payload["population"]) >= 1
        assert len(payload["michinoeki"]) >= 1
        assert fake_opendata.calls[0]["method"] == "get_population"

    async def test_keyword_required_by_schema(self):
        """keyword は必須スキーマ。"""
        async with create_connected_server_and_client_session(mcp) as session:
            tools = (await session.list_tools()).tools
        schema = next(t for t in tools if t.name == "search_niigata_data").inputSchema
        assert schema["required"] == ["keyword"]

    async def test_force_propagated(self, fake_amedas, fake_opendata):
        """force がオープンデータ取得に伝播する。"""
        async with create_connected_server_and_client_session(mcp) as session:
            await session.call_tool("search_niigata_data", {"keyword": "湯沢", "force": True})
        assert all(c["force"] is True for c in fake_opendata.calls)

    async def test_partial_opendata_failure(self, fake_amedas, monkeypatch):
        """オープンデータの一部が失敗しても観測所の結果は返る。"""
        fake = FakeOpenDataClient()
        fake.get_datasets = lambda **kwargs: (_ for _ in ()).throw(
            OpenDataError("カタログ取得に失敗")
        )
        monkeypatch.setattr("nic.mcp.server.OpenDataClient", lambda **kwargs: fake)
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool("search_niigata_data", {"keyword": "湯沢"})
        assert not result.isError
        payload = _json_of(result)
        assert len(payload["stations"]) >= 1
        assert payload["errors"]  # エラーが errors に含まれる


# ---------------------------------------------------------------------------
# get_tourist_spots
# ---------------------------------------------------------------------------


class TestGetTouristSpots:
    async def test_returns_spots(self, fake_tourism):
        """観光スポット一覧（温泉 + 集客施設）を返す。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool("get_tourist_spots", {})
        assert not result.isError
        payload = _json_of(result)
        assert payload["type"] == "tourist_spots"
        assert payload["count"] == 3
        assert len(payload["spots"]) == 3
        first = payload["spots"][0]
        assert first["name"] == "ほてる大橋館の湯"
        assert first["category"] == "温泉"
        assert first["lat"] == 37.7380947
        assert "source" in first
        assert fake_tourism.calls[0]["method"] == "get_spots"
        assert fake_tourism.calls[0]["category"] is None

    async def test_category_filter_passed_to_core(self, fake_tourism):
        """category がコアに伝播し、結果も絞り込まれる。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool(
                "get_tourist_spots", {"category": "温泉"}
            )
        assert not result.isError
        assert fake_tourism.calls[0]["category"] == "温泉"
        payload = _json_of(result)
        assert payload["count"] == 1
        assert all(s["category"] == "温泉" for s in payload["spots"])

    async def test_keyword_filter(self, fake_tourism):
        """keyword でスポット名・住所・説明を部分一致検索する。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool(
                "get_tourist_spots", {"keyword": "湯"}
            )
        assert not result.isError
        payload = _json_of(result)
        assert payload["count"] == 1
        assert payload["spots"][0]["name"] == "ほてる大橋館の湯"

    async def test_limit(self, fake_tourism):
        """limit で件数を制限できる。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool("get_tourist_spots", {"limit": 2})
        assert not result.isError
        payload = _json_of(result)
        assert len(payload["spots"]) == 2
        assert payload["count"] == 3  # 制限は返却のみに適用

    async def test_force_propagated(self, fake_tourism):
        """force がコアに伝播する。"""
        async with create_connected_server_and_client_session(mcp) as session:
            await session.call_tool("get_tourist_spots", {"force": True})
        assert fake_tourism.calls[0]["force"] is True

    async def test_core_error_is_error_result(self, fake_tourism):
        """コアのエラーは isError=True で返る。"""
        fake_tourism.error = TourismError("観光データを取得できませんでした")
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool("get_tourist_spots", {})
        assert result.isError
        assert "観光データを取得できませんでした" in _text_of(result)


# ---------------------------------------------------------------------------
# get_tour_recommendation
# ---------------------------------------------------------------------------


class TestGetTourRecommendation:
    async def test_returns_recommendation(self, fake_tourism):
        """推薦スポットと入込客数傾向を返す。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool("get_tour_recommendation", {})
        assert not result.isError
        payload = _json_of(result)
        assert payload["type"] == "tour_recommendation"
        assert payload["area"] is None
        assert len(payload["spots"]) == 3
        assert payload["stats"]["latest_year"] == 2024
        assert payload["stats"]["latest_total"] == 16019
        assert payload["stats"]["busiest_year"] == 2024
        assert payload["stats"]["records"][0]["era_year"] == "令和6"
        assert payload["rain"]["precipitation"] is None
        assert "source" in payload
        # 観光クライアントで get_spots と get_irikomi を呼ぶ
        methods = [c["method"] for c in fake_tourism.calls]
        assert "get_spots" in methods and "get_irikomi" in methods

    async def test_area_filters_spots(self, fake_tourism):
        """area 指定でスポットが絞り込まれる。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool(
                "get_tour_recommendation", {"area": "燕市"}
            )
        assert not result.isError
        payload = _json_of(result)
        assert payload["area"] == "燕市"
        assert len(payload["spots"]) == 1
        assert payload["spots"][0]["name"] == "川前公民館"

    async def test_force_propagated(self, fake_tourism, fake_amedas):
        """force がコアの観光取得に伝播する。"""
        async with create_connected_server_and_client_session(mcp) as session:
            await session.call_tool("get_tour_recommendation", {"force": True})
        assert all(c["force"] is True for c in fake_tourism.calls)

    async def test_tourism_error_is_error_result(self, fake_tourism):
        """コアのエラーは isError=True で返る。"""
        fake_tourism.error = TourismError("観光データを取得できませんでした")
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool("get_tour_recommendation", {})
        assert result.isError
        assert "観光データを取得できませんでした" in _text_of(result)


# ---------------------------------------------------------------------------
# get_warning_info
# ---------------------------------------------------------------------------


class TestGetWarningInfo:
    async def test_returns_prefecture_warnings(self, fake_warning):
        """デフォルト（府県階層）の警報・注意報を返す。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool("get_warning_info", {})
        assert not result.isError
        payload = _json_of(result)
        assert payload["prefecture_code"] == "150000"
        assert payload["level"] == "府県"
        assert payload["title"] == "新潟県気象警報・注意報"
        assert payload["headline"].startswith("中越、上越では")
        assert payload["summary"] == "大雨注意報 継続、雷注意報 継続"
        assert payload["source"] == "出典:気象庁"
        assert len(payload["areas"]) == 1
        assert payload["areas"][0]["name"] == "新潟県"
        assert payload["areas"][0]["kinds"][0]["name"] == "大雨注意報"
        assert payload["areas"][0]["kinds"][0]["status"] == "継続"
        # 4 階層すべてが levels に含まれる
        assert [lv["level"] for lv in payload["levels"]] == [
            "府県", "一次細分", "市町村",
        ]
        assert fake_warning.calls[0]["force"] is False

    async def test_municipality_level(self, fake_warning):
        """level="市町村" で市町村階層の全エリアを返す。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool(
                "get_warning_info", {"level": "市町村"}
            )
        assert not result.isError
        payload = _json_of(result)
        assert payload["level"] == "市町村"
        assert len(payload["areas"]) == 2
        assert payload["areas"][0]["name"] == "新潟市"

    async def test_active_only(self, fake_warning):
        """active_only=True で発表のある地域のみ返す。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool(
                "get_warning_info", {"level": "市町村", "active_only": True}
            )
        assert not result.isError
        payload = _json_of(result)
        assert payload["active_only"] is True
        # 上越市は「発表警報・注意報はなし」なので除外される
        assert [a["name"] for a in payload["areas"]] == ["新潟市"]

    async def test_force_propagated(self, fake_warning):
        """force がコアに伝播する。"""
        async with create_connected_server_and_client_session(mcp) as session:
            await session.call_tool("get_warning_info", {"force": True})
        assert fake_warning.calls[0]["force"] is True

    async def test_invalid_level_is_error(self, fake_warning):
        """不正な level は isError=True で返る。"""
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool(
                "get_warning_info", {"level": "invalid"}
            )
        assert result.isError
        assert "府県" in _text_of(result)

    async def test_core_error_is_error_result(self, fake_warning):
        """コアのエラーは isError=True で返る。"""
        fake_warning.error = WarningError("フィードに新潟県の電文が見つかりません")
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool("get_warning_info", {})
        assert result.isError
        assert "フィード" in _text_of(result)


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


class TestMain:
    def test_server_name(self):
        """FastMCP サーバーの名前が設定されている。"""
        assert mcp.name == "nic"

    def test_main_run_stdio(self, monkeypatch):
        """main() は stdio トランスポートで起動する。"""
        calls = []

        def fake_run(self, transport: str = "stdio", **kwargs):
            calls.append(transport)
            return None

        monkeypatch.setattr("nic.mcp.server.FastMCP.run", fake_run)
        from nic.mcp.server import main

        main()
        assert calls == ["stdio"]
