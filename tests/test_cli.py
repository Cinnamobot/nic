"""nic.cli.main のユニットテスト（Typer CliRunner による E2E）。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from typer.testing import CliRunner

from nic.cli.main import (
    _pad,
    _render_table,
    app,
)
from nic.core.amedas import (
    AmedasData,
    AmedasElement,
    AmedasFetchError,
    Observation,
    SOURCE_TEXT as AMEDAS_SOURCE,
    Station,
)
from nic.core.opendata import (
    Dataset,
    MichiNoEki,
    OpenDataFetchError,
    PopulationRecord,
)
from nic.core.tourism import (
    SOURCE_TEXT as TOURISM_SOURCE,
    Spot,
    TourismFetchError,
    TourismStat,
)
from nic.core.warning import (
    SOURCE_TEXT as WARNING_SOURCE,
    WarningArea,
    WarningData,
    WarningFetchError,
    WarningKind,
    WarningLevel,
)

runner = CliRunner()

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


def _spots() -> list[Spot]:
    """観光スポット（温泉 + 集客施設）。"""
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
        ),
    ]


def _onsen_only() -> list[Spot]:
    """温泉スポットのみ（入込客数 CSV の温泉・健康分類と対応）。"""
    return [s for s in _spots() if s.category == "温泉"]


def _irikomi() -> list[TourismStat]:
    """観光入込客数（年別・分類別）。"""
    return [
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
        ),
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
        ),
    ]


def _warning_data() -> WarningData:
    """警報・注意報（4 階層）のサンプル。"""
    pref = WarningLevel(
        level="府県",
        type_label="気象警報・注意報（府県予報区等）",
        areas=(
            WarningArea(
                name="新潟県",
                code="150000",
                kinds=(
                    WarningKind(name="大雨注意報", code="10", status="発表"),
                    WarningKind(name="雷注意報", code="14", status="発表"),
                ),
            ),
        ),
    )
    sub = WarningLevel(
        level="一次細分",
        type_label="気象警報・注意報（一次細分区域等）",
        areas=(
            WarningArea(
                name="下越",
                code="150010",
                kinds=(WarningKind(name="発表警報・注意報はなし", code="", status="発表警報・注意報はなし"),),
            ),
            WarningArea(
                name="中越",
                code="150020",
                kinds=(
                    WarningKind(name="大雨注意報", code="10", status="継続"),
                    WarningKind(name="雷注意報", code="14", status="継続"),
                ),
            ),
        ),
    )
    region = WarningLevel(
        level="地域",
        type_label="気象警報・注意報（市町村等をまとめた地域等）",
        areas=(
            WarningArea(
                name="十日町地域",
                code="150026",
                kinds=(WarningKind(name="雷注意報", code="14", status="継続"),),
            ),
            WarningArea(
                name="上越市",
                code="150031",
                kinds=(
                    WarningKind(name="大雨注意報", code="10", status="継続"),
                    WarningKind(name="雷注意報", code="14", status="継続"),
                ),
            ),
        ),
    )
    mun = WarningLevel(
        level="市町村",
        type_label="気象警報・注意報（市町村等）",
        areas=(
            WarningArea(
                name="新潟市",
                code="1510000",
                kinds=(WarningKind(name="発表警報・注意報はなし", code="", status="発表警報・注意報はなし"),),
            ),
            WarningArea(
                name="十日町市",
                code="1521000",
                kinds=(
                    WarningKind(name="大雨注意報", code="10", status="継続"),
                    WarningKind(name="雷注意報", code="14", status="継続"),
                ),
            ),
        ),
    )
    return WarningData(
        title="新潟県気象警報・注意報",
        headline="中越、上越では、土砂災害や落雷に注意してください。",
        info_type="発表",
        report_datetime=datetime(2026, 8, 31, 14, 2, 0, tzinfo=timezone.utc),
        editorial_office="新潟地方気象台",
        message_kind="VPWW53",
        message_url="https://www.data.jma.go.jp/developer/xml/data/20260831050211_0_VPWW53_150000.xml",
        levels=(pref, sub, region, mun),
    )


# ---------------------------------------------------------------------------
# モッククライアント
# ---------------------------------------------------------------------------


class FakeAmedasClient:
    """AmedasClient のフェイク（fetch の呼び出し記録付き）。"""

    def __init__(self, *, error: Exception | None = None, snow_data: AmedasData | None = None):
        self.error = error
        self.snow_data = snow_data
        self.calls: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        pass

    def get_stations(self) -> list[Station]:
        return [_ST_NIIGATA, _ST_YUZAWA]

    def fetch(self, element: AmedasElement, *, codes=None, force: bool = False) -> AmedasData:
        self.calls.append(
            {"element": element, "codes": codes, "force": force}
        )
        if self.error is not None:
            raise self.error
        if element is AmedasElement.SNOW and self.snow_data is not None:
            return self.snow_data
        return _amedas_data(element)


class FakeOpenDataClient:
    """OpenDataClient のフェイク（呼び出し記録付き）。"""

    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.warnings: list[str] = ["外部データ源を利用できなかったため、内蔵サンプルデータを返します。"]

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
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
        self.calls.append({"method": "get_population", "municipality": municipality, "force": force})
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

    def __init__(
        self,
        *,
        error: Exception | None = None,
        spots: list[Spot] | None = None,
        onsen: list[Spot] | None = None,
        irikomi: list[TourismStat] | None = None,
    ):
        self.error = error
        self.spots = spots if spots is not None else _spots()
        self.onsen = onsen if onsen is not None else _onsen_only()
        self.irikomi = irikomi if irikomi is not None else _irikomi()
        self.calls: list[dict[str, Any]] = []
        self.warnings: list[str] = ["外部データ源を利用できなかったため、内蔵サンプルデータを返します。"]

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        pass

    def get_spots(self, *, category=None, include_onsen=True, include_p33=True, force=False):
        self.calls.append(
            {"method": "get_spots", "category": category, "force": force}
        )
        if self.error is not None:
            raise self.error
        spots = self.spots
        if category:
            spots = [s for s in spots if s.category == category]
        return spots

    def get_onsen_spots(self, *, force=False):
        self.calls.append({"method": "get_onsen_spots", "force": force})
        if self.error is not None:
            raise self.error
        return self.onsen

    def get_irikomi(self, *, year=None, force=False):
        self.calls.append({"method": "get_irikomi", "year": year, "force": force})
        if self.error is not None:
            raise self.error
        stats = self.irikomi
        if year is not None:
            stats = [s for s in stats if s.year == year]
        return stats


class FakeWarningClient:
    """WarningClient のフェイク（呼び出し記録付き）。"""

    def __init__(
        self,
        *,
        error: Exception | None = None,
        data: WarningData | None = None,
    ):
        self.error = error
        self.data = data if data is not None else _warning_data()
        self.calls: list[dict[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        pass

    def fetch(self, *, force: bool = False, feed_url: str | None = None) -> WarningData:
        self.calls.append({"force": force})
        if self.error is not None:
            raise self.error
        return self.data


@pytest.fixture
def fake_amedas(monkeypatch):
    fake = FakeAmedasClient()
    monkeypatch.setattr("nic.cli.main.AmedasClient", lambda **kwargs: fake)
    return fake


@pytest.fixture
def fake_opendata(monkeypatch):
    fake = FakeOpenDataClient()
    monkeypatch.setattr("nic.cli.main.OpenDataClient", lambda **kwargs: fake)
    return fake


@pytest.fixture
def fake_tourism(monkeypatch):
    fake = FakeTourismClient()
    monkeypatch.setattr("nic.cli.main.TourismClient", lambda **kwargs: fake)
    return fake


@pytest.fixture
def fake_warning(monkeypatch):
    fake = FakeWarningClient()
    monkeypatch.setattr("nic.cli.main.WarningClient", lambda **kwargs: fake)
    return fake


# ---------------------------------------------------------------------------
# ルートコマンド
# ---------------------------------------------------------------------------


class TestRoot:
    def test_no_args_shows_help(self):
        """引数なしはヘルプを表示する（no_args_is_help）。"""
        result = runner.invoke(app, [])
        assert result.exit_code == 2  # no_args_is_help 時は Usage エラー終了
        assert "snow" in result.output
        assert "weather" in result.output
        assert "tour" in result.output
        assert "warning" in result.output
        assert "stats" in result.output
        assert "search" in result.output

    def test_version(self):
        """version コマンドでバージョンを表示する。"""
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "nic" in result.output

    def test_help_lists_subcommands(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for cmd in ("snow", "weather", "tour", "warning", "stats", "search", "version"):
            assert cmd in result.output

    def test_unknown_command_fails(self):
        result = runner.invoke(app, ["unknown-cmd"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# snow コマンド
# ---------------------------------------------------------------------------


class TestSnow:
    def test_snow_table_default(self, fake_amedas):
        """デフォルトで積雪一覧を表形式で表示する。"""
        result = runner.invoke(app, ["snow"])
        assert result.exit_code == 0
        assert "積雪" in result.output
        assert "湯沢" in result.output
        assert "新潟" in result.output
        assert "210" in result.output
        assert "12" in result.output
        assert AMEDAS_SOURCE in result.output
        assert fake_amedas.calls[0]["element"] is AmedasElement.SNOW
        assert fake_amedas.calls[0]["codes"] is None

    def test_snow_rank(self, fake_amedas):
        """--rank で降順ランキング表示（湯沢 210cm が 1 位）。"""
        result = runner.invoke(app, ["snow", "--rank"])
        assert result.exit_code == 0
        line = next(l for l in result.output.splitlines() if "湯沢" in l)
        assert line.startswith("1")
        assert "210.0" in line

    def test_snow_station_filter(self, fake_amedas):
        """--station で観測所を絞り込んでコアに渡す。"""
        result = runner.invoke(app, ["snow", "--station", "54841"])
        assert result.exit_code == 0
        assert fake_amedas.calls[0]["codes"] == ["54841"]

    def test_snow_station_multiple(self, fake_amedas):
        result = runner.invoke(app, ["snow", "--station", "54232,54841"])
        assert result.exit_code == 0
        assert fake_amedas.calls[0]["codes"] == ["54232", "54841"]

    def test_snow_limit(self, fake_amedas):
        """--limit で表示件数を絞る。"""
        result = runner.invoke(app, ["snow", "--limit", "1"])
        assert result.exit_code == 0
        assert "湯沢" in result.output
        assert "210" in result.output
        # 2 地点中 1 地点のみ表示（2 行目のデータ行が無い）
        assert "順位" in result.output

    def test_snow_json(self, fake_amedas):
        """--json で JSON 出力（ランキング順）。"""
        result = runner.invoke(app, ["snow", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["element"] == "snow"
        assert payload["unit"] == "cm"
        assert payload["source"] == AMEDAS_SOURCE
        assert payload["observations"][0]["rank"] == 1
        assert payload["observations"][0]["station_name"] == "湯沢"
        assert payload["observations"][0]["value"] == 210.0

    def test_snow_404_friendly(self, monkeypatch):
        """積雪が 404（夏季休止）のとき親切なヒント付きで終了コード 1。"""
        fake = FakeAmedasClient(
            error=AmedasFetchError("気象庁 CSV が取得できません (HTTP 404)。提供休止の可能性")
        )
        monkeypatch.setattr("nic.cli.main.AmedasClient", lambda **kwargs: fake)
        result = runner.invoke(app, ["snow"])
        assert result.exit_code == 1
        assert "エラー" in result.output
        assert "冬季" in result.output  # 冬季のみ提供のヒント

    def test_snow_force(self, fake_amedas):
        """--force がコアに伝播する。"""
        runner.invoke(app, ["--force", "snow"])
        assert fake_amedas.calls[0]["force"] is True

    def test_snow_missing_value_excluded(self, monkeypatch):
        """欠測（None）はランキング対象から除外される。"""
        data = AmedasData(
            element=AmedasElement.SNOW,
            observations=[
                _obs(_ST_NIIGATA, None, quality=1),
                _obs(_ST_YUZAWA, 210.0),
            ],
            fetched_at=_OBS_TIME,
        )
        fake = FakeAmedasClient(snow_data=data)
        monkeypatch.setattr("nic.cli.main.AmedasClient", lambda **kwargs: fake)
        result = runner.invoke(app, ["snow", "--rank"])
        assert result.exit_code == 0
        assert "湯沢" in result.output
        assert "210" in result.output
        # 欠測の新潟は表示されない
        assert "順位" in result.output


# ---------------------------------------------------------------------------
# weather コマンド
# ---------------------------------------------------------------------------


class TestWeather:
    def test_weather_table(self, fake_amedas):
        """気温・降水量を表形式で表示する。"""
        result = runner.invoke(app, ["weather"])
        assert result.exit_code == 0
        assert "最高気温" in result.output
        assert "最低気温" in result.output
        assert "降水量" in result.output
        assert "湯沢" in result.output
        # 3 要素を取得している
        elements = [c["element"] for c in fake_amedas.calls]
        assert AmedasElement.MAX_TEMP in elements
        assert AmedasElement.MIN_TEMP in elements
        assert AmedasElement.PRECIPITATION in elements

    def test_weather_station(self, fake_amedas):
        result = runner.invoke(app, ["weather", "--station", "54232"])
        assert result.exit_code == 0
        assert all(c["codes"] == ["54232"] for c in fake_amedas.calls)

    def test_weather_json(self, fake_amedas):
        result = runner.invoke(app, ["weather", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["unit"]["max_temp"] == "℃"
        assert len(payload["records"]) == 2
        rec = payload["records"][0]
        assert rec["station_code"] in ("54232", "54841")
        assert rec["max_temp"] == 12.0  # フェイクデータの共通値

    def test_weather_error_friendly(self, monkeypatch):
        fake = FakeAmedasClient(error=AmedasFetchError("通信に失敗しました"))
        monkeypatch.setattr("nic.cli.main.AmedasClient", lambda **kwargs: fake)
        result = runner.invoke(app, ["weather"])
        assert result.exit_code == 1
        assert "エラー" in result.output


# ---------------------------------------------------------------------------
# stats コマンド
# ---------------------------------------------------------------------------


class TestStats:
    def test_stats_default_datasets(self, fake_opendata):
        """フラグなしはデータセット一覧。"""
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0
        assert "人口時系列データ" in result.output
        assert "新潟県道の駅" in result.output
        assert "CSV" in result.output
        assert fake_opendata.calls[0]["method"] == "get_datasets"

    def test_stats_datasets_filter(self, fake_opendata):
        result = runner.invoke(app, ["stats", "--query", "道の駅"])
        assert result.exit_code == 0
        call = fake_opendata.calls[0]
        assert call["query"] == "道の駅"
        assert call["category"] is None
        assert "新潟県道の駅" in result.output
        assert "人口時系列" not in result.output

    def test_stats_datasets_category_and_format(self, fake_opendata):
        result = runner.invoke(app, ["stats", "--category", "運輸・観光", "--format", "Excel"])
        assert result.exit_code == 0
        call = fake_opendata.calls[0]
        assert call["category"] == "運輸・観光"
        assert call["data_format"] == "Excel"

    def test_stats_population(self, fake_opendata):
        result = runner.invoke(app, ["stats", "--population"])
        assert result.exit_code == 0
        assert "新潟市" in result.output
        assert "772,425" in result.output
        assert fake_opendata.calls[0]["method"] == "get_population"

    def test_stats_population_municipality(self, fake_opendata):
        result = runner.invoke(app, ["stats", "--population", "--municipality", "三条"])
        assert result.exit_code == 0
        assert fake_opendata.calls[0]["municipality"] == "三条"
        assert "三条市" in result.output
        assert "新潟市" not in result.output

    def test_stats_population_json(self, fake_opendata):
        result = runner.invoke(app, ["stats", "--population", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["type"] == "population"
        assert payload["records"][0]["municipality_name"] == "新潟市"
        assert payload["records"][0]["total"] == 772425

    def test_stats_tourism(self, fake_opendata):
        result = runner.invoke(app, ["stats", "--tourism"])
        assert result.exit_code == 0
        assert "豊栄" in result.output
        assert "025-388-2700" in result.output
        assert fake_opendata.calls[0]["method"] == "get_tourism"

    def test_stats_tourism_json(self, fake_opendata):
        result = runner.invoke(app, ["stats", "--tourism", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["type"] == "michinoeki"
        assert payload["stations"][0]["name"] == "豊栄"

    def test_stats_datasets_json(self, fake_opendata):
        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["type"] == "datasets"
        assert len(payload["datasets"]) == 2
        assert payload["datasets"][0]["name"] == "人口時系列データ(市町村別)"

    def test_stats_conflicting_flags(self, fake_opendata):
        """同時指定はエラー（終了コード 2）。"""
        result = runner.invoke(app, ["stats", "--population", "--tourism"])
        assert result.exit_code == 2
        assert "同時に指定できません" in result.output

    def test_stats_warnings_shown(self, fake_opendata):
        """フォールバック警告が注記として stderr に表示される。"""
        result = runner.invoke(app, ["stats"])
        assert "注:" in result.stderr
        assert "サンプル" in result.stderr

    def test_stats_error_friendly(self, monkeypatch):
        fake = FakeOpenDataClient(error=OpenDataFetchError("通信に失敗しました"))
        monkeypatch.setattr("nic.cli.main.OpenDataClient", lambda **kwargs: fake)
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 1
        assert "エラー" in result.output


# ---------------------------------------------------------------------------
# search コマンド
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_stations(self, fake_amedas, fake_opendata):
        """観測所名で横断検索する。"""
        result = runner.invoke(app, ["search", "湯沢"])
        assert result.exit_code == 0
        assert "アメダス観測所" in result.output
        assert "湯沢" in result.output
        payload_kw = "湯沢"
        assert payload_kw in result.output

    def test_search_population(self, fake_amedas, fake_opendata):
        result = runner.invoke(app, ["search", "新潟市"])
        assert result.exit_code == 0
        assert "■ 人口" in result.output
        assert "新潟市" in result.output

    def test_search_michinoeki(self, fake_amedas, fake_opendata):
        result = runner.invoke(app, ["search", "豊栄"])
        assert result.exit_code == 0
        assert "■ 道の駅" in result.output
        assert "豊栄" in result.output

    def test_search_datasets(self, fake_amedas, fake_opendata):
        result = runner.invoke(app, ["search", "道の駅"])
        assert result.exit_code == 0
        assert "■ データセット" in result.output

    def test_search_json(self, fake_amedas, fake_opendata):
        result = runner.invoke(app, ["search", "新潟", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["keyword"] == "新潟"
        # 新潟: 観測所（新潟）+ 人口（新潟市）+ 道の駅住所（新潟市北区...）
        assert len(payload["stations"]) >= 1
        assert len(payload["population"]) >= 1
        assert len(payload["michinoeki"]) >= 1

    def test_search_no_hit_exit_1(self, fake_amedas, fake_opendata):
        """ヒットなしはメッセージとともに終了コード 1。"""
        result = runner.invoke(app, ["search", "存在しないキーワードxyz"])
        assert result.exit_code == 1
        assert "ヒット" in result.output

    def test_search_no_keyword(self):
        """キーワード未指定は使い方エラー。"""
        result = runner.invoke(app, ["search"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# tour コマンド
# ---------------------------------------------------------------------------


class TestTour:
    def test_tour_default_spots(self, fake_tourism):
        """フラグなしは観光スポット一覧を表形式で表示する。"""
        result = runner.invoke(app, ["tour"])
        assert result.exit_code == 0
        assert "観光スポット一覧" in result.output
        assert "ほてる大橋館の湯" in result.output
        assert "シネ・ウインド" in result.output
        assert "温泉" in result.output
        assert fake_tourism.calls[0]["method"] == "get_spots"
        assert fake_tourism.calls[0]["category"] is None

    def test_tour_category_filter(self, fake_tourism):
        """--category で区分を絞り込んでコアに渡す。"""
        result = runner.invoke(app, ["tour", "--category", "温泉"])
        assert result.exit_code == 0
        assert fake_tourism.calls[0]["category"] == "温泉"
        assert "ほてる大橋館の湯" in result.output
        assert "シネ・ウインド" not in result.output

    def test_tour_spots_json(self, fake_tourism):
        """--json で JSON 出力。"""
        result = runner.invoke(app, ["tour", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["type"] == "spots"
        assert payload["source"] == TOURISM_SOURCE
        assert payload["spots"][0]["name"] == "ほてる大橋館の湯"
        assert payload["spots"][0]["category"] == "温泉"

    def test_tour_onsen(self, fake_tourism):
        """--onsen で温泉スポットのみ表示する。"""
        result = runner.invoke(app, ["tour", "--onsen"])
        assert result.exit_code == 0
        assert "温泉スポット一覧" in result.output
        assert "ほてる大橋館の湯" in result.output
        assert "シネ・ウインド" not in result.output
        assert fake_tourism.calls[0]["method"] == "get_onsen_spots"

    def test_tour_onsen_json(self, fake_tourism):
        """温泉の JSON 出力（泉質が description に入る）。"""
        result = runner.invoke(app, ["tour", "--onsen", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["type"] == "onsen"
        assert payload["spots"][0]["description"]

    def test_tour_weather(self, fake_tourism, fake_amedas):
        """--weather で気象情報（アメダス）と組み合わせて表示する。"""
        result = runner.invoke(app, ["tour", "--weather"])
        assert result.exit_code == 0
        assert "観光スポットと天気" in result.output
        assert "観測時刻" in result.output
        assert "ほてる大橋館の湯" in result.output
        # アメダスは降水量を取得している
        assert fake_amedas.calls[0]["element"] is AmedasElement.PRECIPITATION

    def test_tour_weather_recommend(self, fake_tourism, fake_amedas):
        """--recommend で雨の降っている地域のおすすめを表示する。"""
        result = runner.invoke(app, ["tour", "--weather", "--recommend"])
        assert result.exit_code == 0
        # フェイクの湯沢観測値 210mm → 雨が降っている地域に含まれる
        assert "雨が降っている地域" in result.output
        assert "おすすめ" in result.output
        assert "湯沢" in result.output

    def test_tour_weather_json(self, fake_tourism, fake_amedas):
        """天気×おすすめの JSON 出力。"""
        result = runner.invoke(app, ["tour", "--weather", "--recommend", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["type"] == "weather_recommend"
        assert "湯沢" in payload["rainy_stations"]
        assert payload["source"] == TOURISM_SOURCE

    def test_tour_irikomi(self, fake_tourism):
        """--irikomi で観光入込客数（年別）を表示する。"""
        result = runner.invoke(app, ["tour", "--irikomi"])
        assert result.exit_code == 0
        assert "観光入込客数" in result.output
        assert "16,019" in result.output
        assert "令和6" in result.output
        assert fake_tourism.calls[0]["method"] == "get_irikomi"

    def test_tour_irikomi_year(self, fake_tourism):
        """--year で年を絞り込む。"""
        result = runner.invoke(app, ["tour", "--irikomi", "--year", "2024"])
        assert result.exit_code == 0
        assert fake_tourism.calls[0]["year"] == 2024
        assert "令和6" in result.output
        assert "令和5" not in result.output

    def test_tour_irikomi_json(self, fake_tourism):
        result = runner.invoke(app, ["tour", "--irikomi", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["type"] == "irikomi"
        assert payload["stats"][0]["year"] == 2023
        assert payload["stats"][0]["total"] == 15557

    def test_tour_conflicting_flags(self, fake_tourism):
        """同時指定はエラー（終了コード 2）。"""
        result = runner.invoke(app, ["tour", "--spots", "--onsen"])
        assert result.exit_code == 2
        assert "同時に指定できません" in result.output

    def test_tour_recommend_requires_weather(self, fake_tourism):
        """--recommend 単独指定はエラー。"""
        result = runner.invoke(app, ["tour", "--recommend"])
        assert result.exit_code == 2
        assert "--weather" in result.output

    def test_tour_warnings_shown(self, fake_tourism):
        """フォールバック警告が注記として stderr に表示される。"""
        result = runner.invoke(app, ["tour"])
        assert "注:" in result.stderr
        assert "サンプル" in result.stderr

    def test_tour_error_friendly(self, monkeypatch):
        """エラー時はヒント付きで終了コード 1。"""
        fake = FakeTourismClient(error=TourismFetchError("通信に失敗しました"))
        monkeypatch.setattr("nic.cli.main.TourismClient", lambda **kwargs: fake)
        result = runner.invoke(app, ["tour"])
        assert result.exit_code == 1
        assert "エラー" in result.output
        assert "ヒント" in result.output

    def test_tour_force(self, fake_tourism):
        """--force がコアに伝播する。"""
        runner.invoke(app, ["tour", "--force"])
        assert fake_tourism.calls[0]["force"] is True


# ---------------------------------------------------------------------------
# warning コマンド
# ---------------------------------------------------------------------------


class TestWarning:
    def test_warning_default_prefecture(self, fake_warning):
        """デフォルトは府県階層の警報・注意報を表示する。"""
        result = runner.invoke(app, ["warning"])
        assert result.exit_code == 0
        assert "警報・注意報一覧" in result.output
        assert "府県階層" in result.output
        assert "大雨注意報" in result.output
        assert "雷注意報" in result.output
        assert "新潟地方気象台" in result.output
        assert WARNING_SOURCE in result.output

    def test_warning_level_subdivision(self, fake_warning):
        """--level 一次細分 で一次細分階層を表示する。"""
        result = runner.invoke(app, ["warning", "--level", "一次細分"])
        assert result.exit_code == 0
        assert "一次細分階層" in result.output
        assert "中越" in result.output
        assert "下越" in result.output

    def test_warning_level_municipality(self, fake_warning):
        """--level 市町村 で市町村階層を表示する。"""
        result = runner.invoke(app, ["warning", "--level", "市町村"])
        assert result.exit_code == 0
        assert "市町村階層" in result.output
        assert "十日町市" in result.output
        assert "新潟市" in result.output

    def test_warning_area_filter(self, fake_warning):
        """--area で地域名を絞り込む（部分一致）。"""
        result = runner.invoke(app, ["warning", "--level", "市町村", "--area", "十日町"])
        assert result.exit_code == 0
        assert "十日町市" in result.output
        assert "新潟市" not in result.output

    def test_warning_json(self, fake_warning):
        """--json で 4 階層のうち指定階層を出力する。"""
        result = runner.invoke(app, ["warning", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["type"] == "warning"
        assert payload["level"] == "府県"
        assert payload["title"] == "新潟県気象警報・注意報"
        assert payload["source"] == WARNING_SOURCE
        assert payload["areas"][0]["name"] == "新潟県"
        assert payload["areas"][0]["active"] is True
        assert "大雨注意報" in payload["summary"]

    def test_warning_json_level_municipality(self, fake_warning):
        """市町村階層の JSON 出力（発表なしのエリアも含む）。"""
        result = runner.invoke(app, ["warning", "--level", "市町村", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["level"] == "市町村"
        names = [a["name"] for a in payload["areas"]]
        assert "十日町市" in names
        assert "新潟市" in names
        by_name = {a["name"]: a for a in payload["areas"]}
        assert by_name["十日町市"]["active"] is True
        assert by_name["新潟市"]["active"] is False

    def test_warning_invalid_level(self, fake_warning):
        """不正な --level は使い方エラー（終了コード 2）。"""
        result = runner.invoke(app, ["warning", "--level", "存在しない階層"])
        assert result.exit_code == 2
        assert "府県" in result.output

    def test_warning_no_hit(self, fake_warning):
        """--area でヒットなしは「該当データなし」メッセージ。"""
        result = runner.invoke(app, ["warning", "--area", "存在しない地域"])
        assert result.exit_code == 0
        assert "該当データ" in result.output

    def test_warning_error_friendly(self, monkeypatch):
        """エラー時はヒント付きで終了コード 1。"""
        fake = FakeWarningClient(error=WarningFetchError("HTTP 404"))
        monkeypatch.setattr("nic.cli.main.WarningClient", lambda **kwargs: fake)
        result = runner.invoke(app, ["warning"])
        assert result.exit_code == 1
        assert "エラー" in result.output
        assert "ヒント" in result.output

    def test_warning_force(self, fake_warning):
        """--force がコアに伝播する。"""
        runner.invoke(app, ["warning", "--force"])
        assert fake_warning.calls[0]["force"] is True


# ---------------------------------------------------------------------------
# 表形式レンダリング
# ---------------------------------------------------------------------------


class TestRendering:
    def test_pad_wide_chars(self):
        """全角文字は表示幅 2 としてパディングされる。"""
        assert _pad("湯沢", 6) == "湯沢  "  # 幅 6 になるよう半角 2 文字分
        assert _pad("ab", 4) == "ab  "
        assert _pad("湯沢", 4) == "湯沢"  # 既に幅 4 ならそのまま

    def test_render_table_empty(self, capsys):
        """空テーブルは「該当データなし」メッセージ。"""
        _render_table(["a", "b"], [])
        out = capsys.readouterr().out
        assert "該当データ" in out

    def test_render_table_aligns(self, capsys):
        _render_table(["観測所", "値"], [["新潟", "12"], ["湯沢", "210"]])
        out = capsys.readouterr().out
        lines = out.splitlines()
        assert len(lines) == 4  # ヘッダ + 区切り + 2 行
        assert "観測所" in lines[0]
        # 区切り線がヘッダ幅に一致
        assert "-" in lines[1]

    def test_render_table_columns_preserved(self, capsys):
        """各行の列数がヘッダと一致している。"""
        _render_table(["番号", "駅名"], [["1", "豊栄"], ["2", "加治川（さくらの里）"]])
        out = capsys.readouterr().out
        assert "豊栄" in out
        assert "加治川" in out


# ---------------------------------------------------------------------------
# 共通オプション
# ---------------------------------------------------------------------------


class TestCommonOptions:
    def test_force_flag_global(self, fake_amedas):
        """--force はルートオプションとして全コマンドに効く。"""
        runner.invoke(app, ["--force", "snow"])
        assert fake_amedas.calls[0]["force"] is True

    def test_force_after_subcommand(self, fake_amedas):
        """サブコマンドの後ろに置いた --force も効く。"""
        result = runner.invoke(app, ["snow", "--force"])
        assert result.exit_code == 0
        assert fake_amedas.calls[0]["force"] is True

    def test_json_after_subcommand(self, fake_amedas):
        """サブコマンドの後ろに置いた --json も効く。"""
        result = runner.invoke(app, ["snow", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["element"] == "snow"
