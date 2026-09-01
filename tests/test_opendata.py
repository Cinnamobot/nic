"""nic.core.opendata のユニットテスト。"""

from __future__ import annotations

import threading

import httpx
import pytest

from nic.core.opendata import (
    CKAN_BASE_URL,
    MICHINO_EKI_PAGE_URL,
    OPEN_DATA_PAGE_URL,
    POPULATION_PAGE_URL,
    Dataset,
    MichiNoEki,
    OpenDataClient,
    OpenDataError,
    OpenDataFetchError,
    OpenDataNotFoundError,
    OpenDataParseError,
    PopulationRecord,
    SOURCE_TEXT,
    _filter_datasets,
    _normalize_category,
    _normalize_format,
    _normalize_frequency,
    _parse_csv_rows,
    _parse_michinoeki_html,
    _parse_population_csv,
    _sample_dataset,
)


# ---------------------------------------------------------------------------
# フィクスチャ / サンプルデータ
# ---------------------------------------------------------------------------

# オープンデータ一覧 CSV（実際の形式: CP932・11 カラム）
SAMPLE_CATALOG_CSV = """№,所属名,データ名,分類（内容）,データ概要,主な項目,作成年度・時点,更新頻度,データ形式,掲載URL,担当係
234,統計課,人口時系列データ(市町村別),・ 人口・世帯,大正９年からの市町村別人口データを掲載。,新潟県の人口総数、各歳人口合計、男女別数。,R5,毎月,1 CSV,https://www.pref.niigata.lg.jp/site/tokei/1282075307357.html,４統計情報班
731,道路管理課,新潟県道の駅,・0 運輸・観光,県内道の駅の名簿,名称、路線名、所在地、電話番号,R4,不定期,2 Excel,http://www.pref.niigata.lg.jp/dourokanri/1202317264067.html,計画・安全対策係
848,観光企画課,観光統計,・0 運輸・観光,県内観光入込客数等の統計,観光入込客数、宿泊者数 等,R4,毎年,4 PDF,https://www.pref.niigata.lg.jp/sec/kankokikaku/1245960085415.html,観光振興係
"""

# オープンデータ一覧ページ（CSV へのリンクを含む HTML）
SAMPLE_OPEN_DATA_PAGE = """<!DOCTYPE html><html><body>
<h1>新潟県オープンデータ</h1>
<a href="/uploaded/attachment/395584.csv">新潟県オープンデータ一覧 [その他のファイル／229KB]</a>
<a href="/uploaded/attachment/395583.xlsx">新潟県オープンデータ一覧 [Excelファイル／1.2MB]</a>
</body></html>"""

# 人口時系列データ CSV（実際の形式: CP932）
SAMPLE_POPULATION_CSV = """年月日,市町村CD,郡CD,市町村名,人口総数,男計,女計,0歳,1歳,2歳
2024/10/1 0:00,15201,0,新潟市,772425,372208,400217,4565,4645,4532
2024/10/1 0:00,15202,0,長岡市,258131,124938,133193,1862,1966,1892
2024/10/1 0:00,15204,0,三条市,93335,44951,48384,838,871,851
2024/10/1 0:00,15000,,県計,2488364,1209833,1278531,22877,23898,23334
2024/10/1 0:00,15201,0,新潟市,772425,372208,400217,4565,4645,4532
"""

# 人口時系列データ CSV（コンパクト形式: 団体コード/都道府県名・市区町村名/総数/男/女）
SAMPLE_POPULATION_CSV_COMPACT = """年月日,団体コード,都道府県名・市区町村名,総数,男,女,基準,出所
2026/8/1 0:00,150002,新潟県,2044257,994598,1049659,県推計人口,総人口(常住)
2026/8/1 0:00,151009,新潟市,753876,362334,391542,県推計人口,総人口(常住)
2026/8/1 0:00,152021,長岡市,250144,120934,129210,県推計人口,総人口(常住)
2026/8/1 0:00,152041,三条市,90835,43702,47133,県推計人口,総人口(常住)
"""

# 人口時系列データの掲載ページ（CSV リンク含む HTML）
SAMPLE_POPULATION_PAGE = """<!DOCTYPE html><html><body>
<h1>人口時系列データ(市町村別)</h1>
<a href="/uploaded/attachment/453933.CSV">2024年分 CSV</a>
<a href="/uploaded/attachment/453934.CSV">2023年分 CSV</a>
</body></html>"""

# 道の駅ページ（実際のテーブル形式）
SAMPLE_MICHINO_EKI_HTML = """<!DOCTYPE html><html><body>
<h1>新潟県道の駅</h1>
<table>
<thead><tr><th>番号</th><th>駅名</th><th>路線名</th><th>所在地</th><th>電話番号</th></tr></thead>
<tbody>
<tr><td>1</td><td>豊栄</td><td>一般国道7号</td><td>新潟市北区木崎字切尾山3644-乙</td><td>025-388-2700</td></tr>
<tr><td>2</td><td>加治川（さくらの里）</td><td>一般国道7号</td><td>新発田市横岡1147</td><td>0254-33-3175</td></tr>
<tr><td>3</td><td>神林</td><td>一般国道7号</td><td>村上市牧目584</td><td>0254-66-6326</td></tr>
</tbody>
</table>
</body></html>"""

# CKAN API の成功レスポンス（package_search）
SAMPLE_CKAN_RESPONSE = """{
  "success": true,
  "result": {
    "count": 2,
    "results": [
      {
        "id": "ckan-001",
        "name": "population-time-series",
        "title": "人口時系列データ(市町村別)",
        "notes": "大正９年からの市町村別人口データ",
        "url": "https://www.pref.niigata.lg.jp/site/tokei/1282075307357.html",
        "extras": [
          {"key": "分野", "value": "人口・世帯"},
          {"key": "作成年度・時点", "value": "R5"},
          {"key": "更新頻度", "value": "毎月"},
          {"key": "所属名", "value": "統計課"}
        ],
        "resources": [{"format": "CSV"}]
      },
      {
        "id": "ckan-002",
        "name": "michinoeki",
        "title": "新潟県道の駅",
        "notes": "県内道の駅の名簿",
        "url": "http://www.pref.niigata.lg.jp/dourokanri/1202317264067.html",
        "extras": [],
        "resources": [{"format": "Excel"}]
      }
    ]
  }
}
"""


def _encode(text: str) -> bytes:
    """テスト用: テキストを CP932 にエンコードする。"""
    return text.encode("cp932")


def _make_catalog_csv_utf8() -> bytes:
    """UTF-8 版の一覧 CSV（文字コード判別テスト用）。"""
    return SAMPLE_CATALOG_CSV.encode("utf-8")


@pytest.fixture
def mock_client(monkeypatch):
    """httpx.Client をモックして URL ごとの応答を差し替えるフィクスチャ。"""

    class FakeResponse:
        def __init__(self, content: bytes, status_code: int = 200):
            self.content = content
            self.status_code = status_code

    class FakeHTTPX:
        def __init__(self):
            self.responses: dict[str, FakeResponse] = {}

        def get(self, url, **kwargs):
            resp = self.responses.get(url)
            if resp is None:
                resp = FakeResponse(b"", 404)
            return resp

        def close(self):
            pass

    fake = FakeHTTPX()

    def fake_client_factory(*args, **kwargs):
        return fake

    monkeypatch.setattr("nic.core.opendata.httpx.Client", fake_client_factory)
    return fake


def _register_catalog(mock, encoding: str = "cp932") -> None:
    """一覧 CSV をモックに登録する。"""
    mock.responses[
        f"{CKAN_BASE_URL}/api/3/action/package_search?rows=1000"
    ] = type("R", (), {"content": b"", "status_code": 404})()
    mock.responses[OPEN_DATA_PAGE_URL] = type(
        "R", (), {"content": SAMPLE_OPEN_DATA_PAGE.encode("utf-8"), "status_code": 200}
    )()
    mock.responses[
        "https://www.pref.niigata.lg.jp/uploaded/attachment/395584.csv"
    ] = type(
        "R",
        (),
        {
            "content": SAMPLE_CATALOG_CSV.encode(encoding),
            "status_code": 200,
        },
    )()


# ---------------------------------------------------------------------------
# データ構造・定数
# ---------------------------------------------------------------------------


class TestConstants:
    def test_source_text(self):
        assert "新潟県" in SOURCE_TEXT

    def test_error_hierarchy(self):
        assert issubclass(OpenDataFetchError, OpenDataError)
        assert issubclass(OpenDataParseError, OpenDataError)
        assert issubclass(OpenDataNotFoundError, OpenDataError)

    def test_dataclasses_have_source(self):
        d = Dataset(id="1", name="x", category="y", description="", fields="",
                    fiscal_year="", update_frequency="", format="", url="")
        assert d.source == SOURCE_TEXT
        m = MichiNoEki(id=1, name="x", route="y", address="z", phone="0")
        assert m.source == SOURCE_TEXT
        p = PopulationRecord(date="d", municipality_code="1",
                             municipality_name="n", total=1, male=1, female=1)
        assert p.source == SOURCE_TEXT


# ---------------------------------------------------------------------------
# 正規化・パース補助
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_category(self):
        assert _normalize_category("・0 運輸・観光") == "運輸・観光"
        assert _normalize_category("・ 人口・世帯") == "人口・世帯"
        assert _normalize_category("1 国土・気象") == "国土・気象"
        assert _normalize_category("その他") == "その他"

    def test_format(self):
        assert _normalize_format("4 PDF") == "PDF"
        assert _normalize_format("1 CSV") == "CSV"
        assert _normalize_format("2 Excel") == "Excel"
        assert _normalize_format("PDF") == "PDF"

    def test_frequency(self):
        assert _normalize_frequency("毎月") == "毎月"
        assert _normalize_frequency("毎年") == "毎年"
        assert _normalize_frequency("随時") == "随時"
        assert _normalize_frequency("不定期") == "不定期"
        assert _normalize_frequency("更新なし") == "更新なし"
        assert _normalize_frequency("四半期ごと") == "四半期"
        assert _normalize_frequency("２年ごと") == "その他"

    def test_parse_csv_rows(self):
        rows = _parse_csv_rows(SAMPLE_CATALOG_CSV)
        assert len(rows) == 4  # ヘッダ + 3 データ行


# ---------------------------------------------------------------------------
# データセット一覧（CKAN → 一覧 CSV → サンプルのフォールバック）
# ---------------------------------------------------------------------------


class TestDatasets:
    def test_fetch_from_catalog_csv(self, mock_client):
        """CKAN が 404 のとき、公式一覧 CSV から取得する。"""
        _register_catalog(mock_client)
        with OpenDataClient() as client:
            datasets = client.get_datasets()
        assert len(datasets) == 3
        by_id = {d.id: d for d in datasets}
        assert by_id["234"].name == "人口時系列データ(市町村別)"
        assert by_id["234"].category == "人口・世帯"
        assert by_id["234"].format == "CSV"
        assert by_id["234"].source == SOURCE_TEXT
        assert by_id["731"].category == "運輸・観光"  # 接頭辞除去
        assert by_id["731"].format == "Excel"
        assert by_id["848"].format == "PDF"
        assert "フォールバック" in " ".join(client.warnings) or client.warnings

    def test_fetch_from_catalog_csv_utf8(self, mock_client):
        """UTF-8 の一覧 CSV も読み取れる。"""
        _register_catalog(mock_client, encoding="utf-8")
        with OpenDataClient() as client:
            datasets = client.get_datasets()
        assert len(datasets) == 3

    def test_fetch_from_ckan(self, mock_client):
        """CKAN API が応答する場合はそれを優先する。"""
        mock_client.responses[
            f"{CKAN_BASE_URL}/api/3/action/package_search?rows=1000"
        ] = type("R", (), {"content": SAMPLE_CKAN_RESPONSE.encode("utf-8"), "status_code": 200})()
        with OpenDataClient() as client:
            datasets = client.get_datasets()
        assert len(datasets) == 2
        assert datasets[0].id == "ckan-001"
        assert datasets[0].category == "人口・世帯"
        assert datasets[0].format == "CSV"

    def test_ckan_invalid_json_falls_back(self, mock_client):
        """CKAN の応答が JSON でない場合は一覧 CSV へ。"""
        mock_client.responses[
            f"{CKAN_BASE_URL}/api/3/action/package_search?rows=1000"
        ] = type("R", (), {"content": b"<html>error</html>", "status_code": 200})()
        _register_catalog(mock_client)  # 上書き登録
        with OpenDataClient() as client:
            datasets = client.get_datasets()
        assert len(datasets) == 3

    def test_fallback_to_sample(self, mock_client):
        """全データ源が失敗したら内蔵サンプルを返す。"""
        with OpenDataClient() as client:
            datasets = client.get_datasets()
        assert len(datasets) == 5  # _SAMPLE_DATASETS
        assert any(d.name == "人口時系列データ(市町村別)" for d in datasets)
        assert all(d.source == SOURCE_TEXT for d in datasets)
        assert any("サンプル" in w for w in client.warnings)

    def test_no_fallback_raises(self, mock_client):
        """フォールバック無効時はエラーになる。"""
        with OpenDataClient(fallback_to_sample=False) as client:
            with pytest.raises(OpenDataFetchError):
                client.get_datasets()

    def test_filter(self, mock_client):
        """検索・カテゴリ・形式での絞り込み。"""
        _register_catalog(mock_client)
        with OpenDataClient() as client:
            all_ds = client.get_datasets()
            assert len(client.get_datasets(query="道の駅")) == 1
            assert len(client.get_datasets(category="運輸・観光")) == 2
            assert len(client.get_datasets(data_format="CSV")) == 1
            assert len(client.get_datasets(data_format="csv")) == 1  # 大文字小文字
            assert len(client.get_datasets(query="存在しない語")) == 0
        # 純関数でも確認
        assert len(_filter_datasets(all_ds, query="道の駅")) == 1


# ---------------------------------------------------------------------------
# 統計データ（人口）
# ---------------------------------------------------------------------------


class TestPopulation:
    def test_fetch_population(self, mock_client):
        """人口 CSV を取得し、県計を除外して市町村のみ返す。"""
        mock_client.responses[POPULATION_PAGE_URL] = type(
            "R", (), {"content": SAMPLE_POPULATION_PAGE.encode("utf-8"), "status_code": 200}
        )()
        mock_client.responses["https://www.pref.niigata.lg.jp/uploaded/attachment/453933.CSV"] = type(
            "R", (), {"content": _encode(SAMPLE_POPULATION_CSV), "status_code": 200}
        )()
        with OpenDataClient() as client:
            records = client.get_population()
        # 新潟市(重複)・長岡市・三条市 = 3 件、県計は除外
        assert len(records) == 3
        names = {r.municipality_name for r in records}
        assert names == {"新潟市", "長岡市", "三条市"}
        niigata = [r for r in records if r.municipality_name == "新潟市"][0]
        assert niigata.total == 772425
        assert niigata.male == 372208
        assert niigata.female == 400217
        assert niigata.source == SOURCE_TEXT
        assert niigata.municipality_code == "15201"

    def test_fetch_population_municipality_filter(self, mock_client):
        """市町村名での絞り込み。"""
        mock_client.responses[POPULATION_PAGE_URL] = type(
            "R", (), {"content": SAMPLE_POPULATION_PAGE.encode("utf-8"), "status_code": 200}
        )()
        mock_client.responses["https://www.pref.niigata.lg.jp/uploaded/attachment/453933.CSV"] = type(
            "R", (), {"content": _encode(SAMPLE_POPULATION_CSV), "status_code": 200}
        )()
        with OpenDataClient() as client:
            records = client.get_population(municipality="三条")
        assert len(records) == 1
        assert records[0].municipality_name == "三条市"

    def test_population_fallback_to_sample(self, mock_client):
        """ページ取得失敗時は内蔵サンプルを返す。"""
        with OpenDataClient() as client:
            records = client.get_population()
        assert len(records) == 5
        assert any(r.municipality_name == "新潟市" for r in records)
        assert all(r.source == SOURCE_TEXT for r in records)
        assert any("サンプル" in w for w in client.warnings)

    def test_population_no_fallback_raises(self, mock_client):
        with OpenDataClient(fallback_to_sample=False) as client:
            with pytest.raises(OpenDataFetchError):
                client.get_population()

    def test_parse_population_direct(self):
        """パース関数を直接テスト。"""
        records = _parse_population_csv(
            _encode(SAMPLE_POPULATION_CSV), source_url=POPULATION_PAGE_URL
        )
        assert len(records) == 3  # 県計・重複は除外

    def test_parse_population_compact_format(self):
        """コンパクト形式（団体コード 6 桁）もパースできる。"""
        records = _parse_population_csv(
            _encode(SAMPLE_POPULATION_CSV_COMPACT), source_url=POPULATION_PAGE_URL
        )
        assert len(records) == 3  # 新潟県は除外
        by_name = {r.municipality_name: r for r in records}
        assert by_name["新潟市"].total == 753876
        assert by_name["新潟市"].municipality_code == "151009"
        assert by_name["長岡市"].total == 250144

    def test_parse_population_utf8(self):
        """UTF-8 の人口 CSV もパースできる。"""
        records = _parse_population_csv(
            SAMPLE_POPULATION_CSV_COMPACT.encode("utf-8"), source_url=POPULATION_PAGE_URL
        )
        assert len(records) == 3

    def test_sort_population_newest_first(self):
        """複数ファイルを統合した後、新しい順に並ぶ。"""
        from nic.core.opendata import _sort_population_newest_first

        records = _parse_population_csv(
            _encode(SAMPLE_POPULATION_CSV), source_url=POPULATION_PAGE_URL
        )
        sorted_records = _sort_population_newest_first(records)
        dates = [r.date for r in sorted_records]
        assert dates == sorted(dates, reverse=True)
        assert sorted_records[0].date == "2024/10/1 0:00"

    def test_parse_population_bad_header(self):
        with pytest.raises(OpenDataParseError):
            _parse_population_csv(_encode("a,b,c\n1,2,3\n"), source_url="x")


# ---------------------------------------------------------------------------
# 観光データ（道の駅）
# ---------------------------------------------------------------------------


class TestTourism:
    def test_fetch_michinoeki(self, mock_client):
        """道の駅を HTML テーブルから取得する。"""
        mock_client.responses[MICHINO_EKI_PAGE_URL] = type(
            "R", (), {"content": SAMPLE_MICHINO_EKI_HTML.encode("utf-8"), "status_code": 200}
        )()
        with OpenDataClient() as client:
            stations = client.get_tourism()
        assert len(stations) == 3
        assert stations[0].name == "豊栄"
        assert stations[0].route == "一般国道7号"
        assert stations[0].address == "新潟市北区木崎字切尾山3644-乙"
        assert stations[0].phone == "025-388-2700"
        assert stations[0].source == SOURCE_TEXT
        assert stations[1].name == "加治川（さくらの里）"

    def test_michinoeki_fallback_to_sample(self, mock_client):
        """ページ取得失敗時は内蔵サンプルを返す。"""
        with OpenDataClient() as client:
            stations = client.get_tourism()
        assert len(stations) == 5
        assert stations[0].name == "豊栄"
        assert any("サンプル" in w for w in client.warnings)

    def test_parse_michinoeki_html_direct(self):
        stations = _parse_michinoeki_html(SAMPLE_MICHINO_EKI_HTML, source_url="x")
        assert len(stations) == 3

    def test_parse_michinoeki_html_no_table(self):
        with pytest.raises(OpenDataParseError):
            _parse_michinoeki_html("<html><body>no table</body></html>", source_url="x")


# ---------------------------------------------------------------------------
# キャッシュ
# ---------------------------------------------------------------------------


class TestCache:
    def test_cache_ttl(self, mock_client):
        """TTL 内はキャッシュが使われ再取得しない。"""
        _register_catalog(mock_client)
        calls = []
        original_get = mock_client.get

        def counting_get(u, **kwargs):
            calls.append(u)
            return original_get(u, **kwargs)

        mock_client.get = counting_get
        with OpenDataClient(ttl=300) as client:
            client.get_datasets()
            client.get_datasets()
            client.get_datasets()
        # CKAN(1) + 一覧ページ(1) + CSV(1) = 3 リクエストのみ
        assert len(calls) == 3

    def test_cache_expiry(self, mock_client):
        """TTL 経過後は再取得する。"""
        _register_catalog(mock_client)
        calls = []
        original_get = mock_client.get

        def counting_get(u, **kwargs):
            calls.append(u)
            return original_get(u, **kwargs)

        mock_client.get = counting_get
        with OpenDataClient(ttl=0.05) as client:
            client.get_datasets()
            import time

            time.sleep(0.06)
            client.get_datasets()
        assert len(calls) == 6

    def test_force(self, mock_client):
        """force=True でキャッシュを無視する。"""
        _register_catalog(mock_client)
        calls = []
        original_get = mock_client.get

        def counting_get(u, **kwargs):
            calls.append(u)
            return original_get(u, **kwargs)

        mock_client.get = counting_get
        with OpenDataClient(ttl=300) as client:
            client.get_datasets()
            client.get_datasets(force=True)
        assert len(calls) == 6

    def test_thread_safety(self, mock_client):
        """複数スレッドから同時に呼んでも安全。"""
        _register_catalog(mock_client)
        errors = []
        results = []

        def worker():
            try:
                with OpenDataClient(ttl=60) as client:
                    results.append(len(client.get_datasets()))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(results) == 8
        assert all(r == 3 for r in results)


# ---------------------------------------------------------------------------
# エラー処理
# ---------------------------------------------------------------------------


class TestErrors:
    def test_http_error_is_fetch_error(self, mock_client, monkeypatch):
        """通信エラーは OpenDataFetchError に変換される。"""

        class Boom:
            def get(self, url, **kwargs):
                raise httpx.ConnectError("boom")

            def close(self):
                pass

        monkeypatch.setattr("nic.core.opendata.httpx.Client", lambda *a, **k: Boom())
        with OpenDataClient() as client:
            # フォールバック有効ならサンプルが返る（エラーは warnings に記録）
            datasets = client.get_datasets()
        assert len(datasets) == 5
        assert any("CKAN" in w or "取得" in w for w in client.warnings)

    def test_http_error_no_fallback(self, mock_client, monkeypatch):
        """フォールバック無効時は通信エラーがそのまま出る。"""

        class Boom:
            def get(self, url, **kwargs):
                raise httpx.ConnectError("boom")

            def close(self):
                pass

        monkeypatch.setattr("nic.core.opendata.httpx.Client", lambda *a, **k: Boom())
        with OpenDataClient(fallback_to_sample=False) as client:
            with pytest.raises(OpenDataFetchError):
                client.get_datasets()

    def test_sample_helpers(self):
        """サンプル変換ヘルパー。"""
        d = _sample_dataset(
            {"id": "1", "name": "n", "category": "c", "description": "d",
             "fields": "f", "fiscal_year": "R1", "update_frequency": "随時",
             "format": "CSV", "url": "http://x", "department": "課"}
        )
        assert d.id == "1"
        assert d.source == SOURCE_TEXT
