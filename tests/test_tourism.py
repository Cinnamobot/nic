"""nic.core.tourism のユニットテスト。

新潟市 CKAN API・観光入込客数 CSV・温泉 GIS CSV・国土数値情報 P33
（Shapefile/DBF）の各データ源をモック（httpx.Client 差し替え）で検証する。
"""

from __future__ import annotations

import io
import struct
import threading
import zipfile

import httpx
import pytest

from nic.core.tourism import (
    CKAN_BASE_URL,
    IRIKOMI_CSV_URL,
    ONSEN_CSV_URL,
    P33_SOURCE_TEXT,
    P33_ZIP_URL,
    SOURCE_TEXT,
    Spot,
    TourismClient,
    TourismDataset,
    TourismError,
    TourismFetchError,
    TourismNotFoundError,
    TourismParseError,
    TourismStat,
    _is_tourism_package,
    _parse_irikomi_csv,
    _parse_onsen_csv,
    parse_p33_zip,
)


# ---------------------------------------------------------------------------
# サンプルデータ（実データの形式に基づく）
# ---------------------------------------------------------------------------

# 新潟市 CKAN package_search レスポンス（観光検索・実際の形式）
SAMPLE_CKAN_RESPONSE = {
    "success": True,
    "result": {
        "count": 2,
        "results": [
            {
                "id": "16a13911-06c9-4339-aec6-30c092846c83",
                "name": "opendata-kankou_od-irikomidata",
                "title": "新潟市観光入込客数",
                "notes": "",
                "url": "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-kankou/od-irikomidata.html",
                "license_title": "クリエイティブ・コモンズ 表示",
                "license_url": "http://www.opendefinition.org/licenses/cc-by",
                "metadata_modified": "2026-03-04T06:03:28.768642",
                "resources": [
                    {
                        "url": "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-kankou/od-irikomidata.files/irikomidataR6.csv",
                        "format": "CSV",
                    }
                ],
                "tags": [{"name": "OD_観光"}],
            },
            {
                "id": "83958165-3b29-426d-abc3-c3bb519d893a",
                "name": "opendata-kankou_od-citywifi",
                "title": "Niigata City Free Wi-Fi利用可能施設一覧",
                "notes": "",
                "url": "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-kankou/od-citywifi.html",
                "license_title": "クリエイティブ・コモンズ 表示",
                "license_url": "http://www.opendefinition.org/licenses/cc-by",
                "metadata_modified": "2026-07-06T04:02:56.219033",
                "resources": [
                    {
                        "url": "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-kankou/od-citywifi.files/od-city_wifi_ichiran_20240401.csv",
                        "format": "CSV",
                    }
                ],
                "tags": [],
            },
        ],
    },
}

# カタログ全体（観光関連パッケージ判定用）: GIS 温泉は「観光」タグ検索にヒットしない
SAMPLE_CKAN_ALL_RESPONSE = {
    "success": True,
    "result": {
        "count": 3,
        "results": [
            {
                "id": "onsen-pkg-01",
                "name": "od-gis_kankobunspo_od-gis_onseninst",
                "title": "GIS　温泉利用を許可した施設",
                "notes": "温泉利用許可施設（緯度経度・泉質付き）",
                "url": "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-gis/od-gis_kankobunspo/od-gis_onseninst.html",
                "license_title": "クリエイティブ・コモンズ 表示",
                "metadata_modified": "2023-03-29T01:14:56.612035",
                "resources": [
                    {
                        "url": "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-gis/od-gis_kankobunspo/od-gis_onseninst.files/od_gis_10096_onseninstitution.csv",
                        "format": "CSV",
                    }
                ],
                "tags": [],
            },
            {
                "id": "other-pkg-01",
                "name": "opendata-tetsuduki_od-gomibunbetsuhyou",
                "title": "ごみ分別早見表",
                "notes": "ごみの分別方法",
                "url": "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-tetsuduki/od-gomibunbetsuhyou.html",
                "license_title": "クリエイティブ・コモンズ 表示",
                "metadata_modified": "2023-03-29T01:14:40.066779",
                "resources": [],
                "tags": [],
            },
        ],
    },
}

# 観光入込客数 CSV（実際の形式: UTF-8 BOM・11 カラム）
SAMPLE_IRIKOMI_CSV = """年[西暦],年[和暦],観光入込客数合計[千人],行祭事・イベント合計[千人],観光地点合計[千人],観光地点合計の自然[千人],観光地点合計の歴史・文化[千人],観光地点合計の温泉・健康[千人],観光地点合計のスポーツ・レクリエーション[千人],観光地点合計の都市型観光[千人],観光地点合計のその他[千人]
2010,平成22,15307,,,,,,,,
2011,平成23,15628,5980,9647,531,2967,1091,501,4061,496
2024,令和6,16019,4591,11428,425,3044,861,2026,5072,0
"""

# 温泉 GIS CSV（実際の形式: UTF-8・10 カラム）
SAMPLE_ONSEN_CSV = """longitude,latitude,SAUID,SAFIELD000,SAFIELD001,SAFIELD002,SAFIELD003,SAFIELD004,SAFIELD005
138.7892759,37.7221012,24,旅館　丸一,953-0105,新潟市西蒲区間瀬7374番地1,0256-85-2216,間瀬田ノ浦温泉,ナトリウム－塩化物・炭酸水素塩冷鉱泉
138.8389881,37.7416192,48,新潟市岩室観光施設　いわむろや（足湯）,953-0104,新潟市西蒲区岩室温泉字下ノ郷96-1,0256-82-1066,岩室温泉,含硫黄－ナトリウム･カルシウム－塩化物泉
"""


def _dbf_bytes(fields: list[tuple[str, str, int]], rows: list[list[str]]) -> bytes:
    """テスト用 DBF（cp932・ヘッダ + レコード）を生成する。"""
    import datetime

    now = datetime.datetime.now()
    n_fields = len(fields)
    header_size = 32 + 32 * n_fields + 1
    record_size = sum(f[2] for f in fields) + 1  # 先頭の削除フラグ 1 バイト
    header = bytearray()
    header += b"\x03"  # version
    header += bytes([now.year - 1900, now.month, now.day])
    header += struct.pack("<I", len(rows))
    header += struct.pack("<H", header_size)
    header += struct.pack("<H", record_size)
    header += b"\x00" * 20  # reserved
    for name, ftype, flen in fields:
        # DBF フィールド記述子は 32 バイト: 名前(11) + 型(1) + データアドレス(4) + 長さ(1) + 小数桁(1) + 予約(14)
        header += name.encode("cp932").ljust(11, b"\x00")[:11]
        header += ftype.encode("ascii")
        header += b"\x00" * 4  # データアドレス
        header += bytes([flen, 0])
        header += b"\x00" * 14
    header += b"\x0d"  # ヘッダ終端マーカー
    body = bytearray()
    for row in rows:
        body += b"\x20"  # 削除フラグ（空白）
        for (name, ftype, flen), value in zip(fields, row):
            encoded = value.encode("cp932")[:flen].ljust(flen, b" ")
            body += encoded
    body += b"\x1a"  # DBF の EOF マーカー（実データと同様）
    return bytes(header + body)


def _shp_points(points: list[tuple[float, float] | None]) -> bytes:
    """テスト用 SHP（Point）を生成する。"""
    out = bytearray()
    # ファイルヘッダ（100 バイト）
    out += b"\x00" * 100
    for i, pt in enumerate(points, start=1):
        content = bytearray()
        if pt is None:
            content += struct.pack("<i", 0)  # NullShape
        else:
            content += struct.pack("<i", 1)  # Point
            content += struct.pack("<dd", pt[0], pt[1])
        content = bytes(content)
        out += struct.pack(">ii", i, len(content) // 2)
        out += content
    return bytes(out)


def _p33_zip_bytes() -> bytes:
    """テスト用 P33 ZIP（DBF + SHP）を生成する。"""
    fields = [
        ("P33_001", "N", 5),
        ("P33_002", "C", 5),
        ("P33_003", "C", 2),
        ("P33_004", "C", 2),
        ("P33_005", "C", 40),
        ("P33_007", "C", 60),
        ("P33_008", "C", 13),
        ("P33_010", "C", 60),
        ("P33_011", "C", 20),
    ]
    rows = [
        ["3", "15103", "15", "1", "シネ・ウインド", "新潟市中央区八千代2-1-1（1F）", "025-243-5530", "http://cinewind.com/", "‐"],
        ["148", "15216", "15", "2", "西燕公民館", "燕市花見949", "0256-62-4197", "‐", "‐"],
        ["200", "15222", "15", "5", "上越市体育館", "上越市富岡3524", "025-524-1232", "‐", "新潟駅"],
    ]
    dbf = _dbf_bytes(fields, rows)
    shp = _shp_points([(139.0539164, 37.9158092), (138.9111510, 37.6849540), None])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("P33-14_15.dbf", dbf)
        zf.writestr("P33-14_15.shp", shp)
        zf.writestr("P33-14_15.prj", 'GEOGCS["GCS_JGD_2000",...]')
    return buf.getvalue()


P33_ZIP_SAMPLE = _p33_zip_bytes()


def _encode_utf8_bom(text: str) -> bytes:
    """テスト用: UTF-8 BOM 付きエンコード。"""
    return b"\xef\xbb\xbf" + text.encode("utf-8")


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

    monkeypatch.setattr("nic.core.tourism.httpx.Client", fake_client_factory)
    return fake


def _register_ckan(mock, search_url: str | None = None, all_url: str | None = None) -> None:
    """CKAN API（検索 + 全体）をモックに登録する。"""
    if search_url is None:
        search_url = f"{CKAN_BASE_URL}?q=%E8%A6%B3%E5%85%89&rows=1000"
    if all_url is None:
        all_url = f"{CKAN_BASE_URL}?rows=1000"
    mock.responses[search_url] = type(
        "R",
        (),
        {"content": _encode_utf8_bom(""), "status_code": 404},  # 検索は 404 → 全体へ
    )()
    mock.responses[all_url] = type(
        "R",
        (),
        {"content": _encode_utf8_bom(""), "status_code": 404},
    )()


def _json_bytes(obj: object) -> bytes:
    """テスト用: dict → JSON バイト列。"""
    import json

    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# 定数・エラー階層・データ構造
# ---------------------------------------------------------------------------


class TestConstants:
    def test_source_text(self):
        assert "新潟市" in SOURCE_TEXT
        assert "国土数値情報" in P33_SOURCE_TEXT

    def test_error_hierarchy(self):
        assert issubclass(TourismFetchError, TourismError)
        assert issubclass(TourismParseError, TourismError)
        assert issubclass(TourismNotFoundError, TourismError)

    def test_dataclasses_have_source(self):
        d = TourismDataset(id="1", name="n", title="t", description="",
                           license="cc", license_url="", updated_at="",
                           url="")
        assert d.source == SOURCE_TEXT
        s = TourismStat(year=2024, era_year="令和6", total=1)
        assert s.source == SOURCE_TEXT
        sp = Spot(id="1", name="n", category="温泉", lat=1.0, lon=2.0)
        assert sp.source == SOURCE_TEXT


# ---------------------------------------------------------------------------
# CKAN データセット一覧
# ---------------------------------------------------------------------------


class TestDatasets:
    def test_fetch_from_ckan_search(self, mock_client):
        """CKAN 検索結果（観光）を取得する。"""
        url = f"{CKAN_BASE_URL}?q=%E8%A6%B3%E5%85%89&rows=1000"
        mock_client.responses[url] = type(
            "R", (), {"content": _json_bytes(SAMPLE_CKAN_RESPONSE), "status_code": 200}
        )()
        with TourismClient() as client:
            datasets = client.get_datasets()
        assert len(datasets) == 2
        by_id = {d.id: d for d in datasets}
        d = by_id["16a13911-06c9-4339-aec6-30c092846c83"]
        assert d.title == "新潟市観光入込客数"
        assert d.name == "opendata-kankou_od-irikomidata"
        assert d.license == "クリエイティブ・コモンズ 表示"
        assert d.license_url == "http://www.opendefinition.org/licenses/cc-by"
        assert d.source == SOURCE_TEXT
        assert d.source_url == CKAN_BASE_URL
        assert len(d.resources) == 1
        assert d.resources[0].endswith("irikomidataR6.csv")

    def test_fetch_from_ckan_all_includes_gis(self, mock_client):
        """検索ヒットが少ない場合はカタログ全体から観光関連パッケージも収集する。"""
        search_url = f"{CKAN_BASE_URL}?q=%E8%A6%B3%E5%85%89&rows=1000"
        all_url = f"{CKAN_BASE_URL}?rows=1000"
        mock_client.responses[search_url] = type(
            "R", (), {"content": _json_bytes(SAMPLE_CKAN_RESPONSE), "status_code": 200}
        )()
        mock_client.responses[all_url] = type(
            "R", (), {"content": _json_bytes(SAMPLE_CKAN_ALL_RESPONSE), "status_code": 200}
        )()
        with TourismClient() as client:
            datasets = client.get_datasets()
        # 検索 2 件 + 全体から観光関連 1 件（ごみ分別は除外）
        assert len(datasets) == 3
        names = {d.name for d in datasets}
        assert "od-gis_kankobunspo_od-gis_onseninst" in names
        assert "opendata-tetsuduki_od-gomibunbetsuhyou" not in names

    def test_fetch_from_ckan_deduplicates(self, mock_client):
        """検索と全体の両方に同じパッケージが現れても重複しない。"""
        search_url = f"{CKAN_BASE_URL}?q=%E8%A6%B3%E5%85%89&rows=1000"
        all_url = f"{CKAN_BASE_URL}?rows=1000"
        mock_client.responses[search_url] = type(
            "R", (), {"content": _json_bytes(SAMPLE_CKAN_RESPONSE), "status_code": 200}
        )()
        all_resp = {
            "success": True,
            "result": {"count": 2, "results": SAMPLE_CKAN_RESPONSE["result"]["results"]},
        }
        mock_client.responses[all_url] = type(
            "R", (), {"content": _json_bytes(all_resp), "status_code": 200}
        )()
        with TourismClient() as client:
            datasets = client.get_datasets()
        assert len(datasets) == 2

    def test_query_filter(self, mock_client):
        """query でタイトル・概要を絞り込める。"""
        url = f"{CKAN_BASE_URL}?q=%E8%A6%B3%E5%85%89&rows=1000"
        mock_client.responses[url] = type(
            "R", (), {"content": _json_bytes(SAMPLE_CKAN_RESPONSE), "status_code": 200}
        )()
        with TourismClient() as client:
            hit = client.get_datasets(query="Wi-Fi")
            miss = client.get_datasets(query="存在しない語")
        assert len(hit) == 1
        assert hit[0].name == "opendata-kankou_od-citywifi"
        assert len(miss) == 0

    def test_fallback_to_sample(self, mock_client):
        """全データ源が失敗したら内蔵サンプルを返す。"""
        with TourismClient() as client:
            datasets = client.get_datasets()
        assert len(datasets) == 3
        assert any(d.title == "新潟市観光入込客数" for d in datasets)
        assert all(d.source == SOURCE_TEXT for d in datasets)
        assert any("サンプル" in w for w in client.warnings)

    def test_no_fallback_raises(self, mock_client):
        """フォールバック無効時はエラーになる。"""
        with TourismClient(fallback_to_sample=False) as client:
            with pytest.raises(TourismFetchError):
                client.get_datasets()

    def test_is_tourism_package(self):
        """観光関連パッケージ判定。"""
        assert _is_tourism_package({"name": "opendata-kankou_od-irikomidata", "title": "", "tags": []})
        assert _is_tourism_package({"name": "od-gis_kankobunspo_od-gis_onseninst", "title": "", "tags": []})
        assert _is_tourism_package({"name": "x", "title": "新潟市観光入込客数", "tags": []})
        assert _is_tourism_package({"name": "x", "title": "", "tags": [{"name": "OD_観光"}]})
        assert not _is_tourism_package({"name": "opendata-tetsuduki_od-gomibunbetsuhyou", "title": "ごみ分別早見表", "tags": []})


# ---------------------------------------------------------------------------
# 観光入込客数 CSV
# ---------------------------------------------------------------------------


class TestIrikomi:
    def test_fetch_irikomi(self, mock_client):
        """入込客数 CSV（UTF-8 BOM）を取得・パースする。"""
        mock_client.responses[IRIKOMI_CSV_URL] = type(
            "R",
            (),
            {"content": _encode_utf8_bom(SAMPLE_IRIKOMI_CSV), "status_code": 200},
        )()
        with TourismClient() as client:
            stats = client.get_irikomi()
        assert len(stats) == 3
        by_year = {s.year: s for s in stats}
        r2024 = by_year[2024]
        assert r2024.era_year == "令和6"
        assert r2024.total == 16019
        assert r2024.event_total == 4591
        assert r2024.spot_total == 11428
        assert r2024.nature == 425
        assert r2024.history_culture == 3044
        assert r2024.onsen_health == 861
        assert r2024.sports_recreation == 2026
        assert r2024.urban_tourism == 5072
        assert r2024.other == 0
        assert r2024.source == SOURCE_TEXT
        # 2010 年は部分データ（空欄 = None）
        assert by_year[2010].total == 15307
        assert by_year[2010].event_total is None

    def test_year_filter(self, mock_client):
        """year で絞り込める。"""
        mock_client.responses[IRIKOMI_CSV_URL] = type(
            "R",
            (),
            {"content": _encode_utf8_bom(SAMPLE_IRIKOMI_CSV), "status_code": 200},
        )()
        with TourismClient() as client:
            stats = client.get_irikomi(year=2024)
        assert len(stats) == 1
        assert stats[0].year == 2024

    def test_irikomi_fallback_to_sample(self, mock_client):
        """取得失敗時は内蔵サンプルを返す。"""
        with TourismClient() as client:
            stats = client.get_irikomi()
        assert len(stats) == 2
        assert any(s.year == 2024 for s in stats)
        assert all(s.source == SOURCE_TEXT for s in stats)
        assert any("サンプル" in w for w in client.warnings)

    def test_parse_irikomi_direct(self):
        """パース関数を直接テスト（BOM なし UTF-8）。"""
        stats = _parse_irikomi_csv(
            SAMPLE_IRIKOMI_CSV.encode("utf-8"), source_url=IRIKOMI_CSV_URL
        )
        assert len(stats) == 3
        assert stats[0].year == 2010

    def test_parse_irikomi_bad_header(self):
        with pytest.raises(TourismParseError):
            _parse_irikomi_csv(b"a,b,c\n1,2,3\n", source_url="x")

    def test_parse_irikomi_empty(self):
        with pytest.raises(TourismParseError):
            _parse_irikomi_csv(b"", source_url="x")


# ---------------------------------------------------------------------------
# 温泉 GIS CSV
# ---------------------------------------------------------------------------


class TestOnsen:
    def test_fetch_onsen(self, mock_client):
        """温泉 GIS CSV（緯度経度・泉質付き）を取得する。"""
        mock_client.responses[ONSEN_CSV_URL] = type(
            "R",
            (),
            {"content": _encode_utf8_bom(SAMPLE_ONSEN_CSV), "status_code": 200},
        )()
        with TourismClient() as client:
            spots = client.get_onsen_spots()
        assert len(spots) == 2
        s = spots[0]
        assert s.name == "旅館　丸一"
        assert s.category == "温泉"
        assert s.lat == pytest.approx(37.7221012)
        assert s.lon == pytest.approx(138.7892759)
        assert s.address == "新潟市西蒲区間瀬7374番地1"
        assert s.phone == "0256-85-2216"
        assert "間瀬田ノ浦温泉" in s.description
        assert s.source == SOURCE_TEXT
        assert s.source_url == ONSEN_CSV_URL
        assert s.id == "onsen-24"

    def test_onsen_fallback_to_sample(self, mock_client):
        """取得失敗時は内蔵サンプルを返す。"""
        with TourismClient() as client:
            spots = client.get_onsen_spots()
        assert len(spots) == 2
        assert all(s.category == "温泉" for s in spots)
        assert any("サンプル" in w for w in client.warnings)

    def test_parse_onsen_direct(self):
        spots = _parse_onsen_csv(
            SAMPLE_ONSEN_CSV.encode("utf-8"), source_url=ONSEN_CSV_URL
        )
        assert len(spots) == 2
        assert spots[1].description == "岩室温泉（含硫黄－ナトリウム･カルシウム－塩化物泉）"

    def test_parse_onsen_bad_header(self):
        with pytest.raises(TourismParseError):
            _parse_onsen_csv(b"a,b,c\n1,2,3\n", source_url="x")


# ---------------------------------------------------------------------------
# 国土数値情報 P33（Shapefile / DBF）
# ---------------------------------------------------------------------------


class TestP33:
    def test_parse_p33_zip(self):
        """P33 ZIP（DBF cp932 + SHP Point）からスポットを生成する。"""
        spots = parse_p33_zip(P33_ZIP_SAMPLE)
        assert len(spots) == 3
        s0 = spots[0]
        assert s0.name == "シネ・ウインド"
        assert s0.category == "集客施設（映画館）"
        assert s0.lat == pytest.approx(37.9158092)
        assert s0.lon == pytest.approx(139.0539164)
        assert s0.address == "新潟市中央区八千代2-1-1（1F）"
        assert s0.phone == "025-243-5530"
        assert s0.url == "http://cinewind.com/"
        assert s0.source == P33_SOURCE_TEXT
        assert s0.source_url == P33_ZIP_URL
        # 公会堂
        s1 = spots[1]
        assert s1.category == "集客施設（公会堂）"
        # 座標なし（NullShape）のレコード
        s2 = spots[2]
        assert s2.lat is None
        assert s2.lon is None

    def test_fetch_p33(self, mock_client):
        """P33 ZIP をダウンロードしてパースする。"""
        mock_client.responses[P33_ZIP_URL] = type(
            "R", (), {"content": P33_ZIP_SAMPLE, "status_code": 200}
        )()
        with TourismClient() as client:
            spots = client.get_p33_spots()
        assert len(spots) == 3
        assert spots[0].source == P33_SOURCE_TEXT
        assert any("国土数値情報" in w for w in client.warnings)

    def test_p33_fallback_to_sample(self, mock_client):
        """取得失敗時は内蔵サンプルを返す。"""
        with TourismClient() as client:
            spots = client.get_p33_spots()
        assert len(spots) == 2
        assert all(s.source == P33_SOURCE_TEXT for s in spots)
        assert any("サンプル" in w for w in client.warnings)

    def test_p33_invalid_zip(self):
        """ZIP でないバイト列は TourismParseError。"""
        with pytest.raises(TourismParseError):
            parse_p33_zip(b"not a zip file at all........")

    def test_p33_zip_without_dbf(self):
        """DBF が入っていない ZIP は TourismParseError。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "hello")
        with pytest.raises(TourismParseError):
            parse_p33_zip(buf.getvalue())


# ---------------------------------------------------------------------------
# スポット統合
# ---------------------------------------------------------------------------


class TestSpots:
    def test_get_spots_integration(self, mock_client):
        """温泉 + P33 を統合して返す。"""
        mock_client.responses[ONSEN_CSV_URL] = type(
            "R",
            (),
            {"content": _encode_utf8_bom(SAMPLE_ONSEN_CSV), "status_code": 200},
        )()
        mock_client.responses[P33_ZIP_URL] = type(
            "R", (), {"content": P33_ZIP_SAMPLE, "status_code": 200}
        )()
        with TourismClient() as client:
            spots = client.get_spots()
        assert len(spots) == 5  # 温泉 2 + P33 3
        categories = {s.category for s in spots}
        assert "温泉" in categories
        assert any(c.startswith("集客施設") for c in categories)

    def test_get_spots_category_filter(self, mock_client):
        """category で絞り込める。"""
        mock_client.responses[ONSEN_CSV_URL] = type(
            "R",
            (),
            {"content": _encode_utf8_bom(SAMPLE_ONSEN_CSV), "status_code": 200},
        )()
        mock_client.responses[P33_ZIP_URL] = type(
            "R", (), {"content": P33_ZIP_SAMPLE, "status_code": 200}
        )()
        with TourismClient() as client:
            onsen_only = client.get_spots(category="温泉")
        assert len(onsen_only) == 2
        assert all(s.category == "温泉" for s in onsen_only)

    def test_get_spots_include_flags(self, mock_client):
        """include_onsen / include_p33 でデータ源を選択できる。"""
        mock_client.responses[P33_ZIP_URL] = type(
            "R", (), {"content": P33_ZIP_SAMPLE, "status_code": 200}
        )()
        with TourismClient() as client:
            p33_only = client.get_spots(include_onsen=False)
        assert len(p33_only) == 3
        assert all(s.source == P33_SOURCE_TEXT for s in p33_only)

    def test_get_spots_fallback_to_sample(self, mock_client):
        """全データ源失敗時はサンプル（温泉 + P33）を返す。"""
        with TourismClient() as client:
            spots = client.get_spots()
        assert len(spots) == 4  # 温泉 2 + P33 2
        assert any("サンプル" in w for w in client.warnings)

    def test_get_spots_no_fallback_raises(self, mock_client):
        """フォールバック無効かつ全失敗時はエラーになる。"""
        with TourismClient(fallback_to_sample=False) as client:
            with pytest.raises(TourismFetchError):
                client.get_spots()


# ---------------------------------------------------------------------------
# キャッシュ
# ---------------------------------------------------------------------------


class TestCache:
    def test_cache_ttl(self, mock_client):
        """TTL 内はキャッシュが使われ再取得しない。"""
        mock_client.responses[IRIKOMI_CSV_URL] = type(
            "R",
            (),
            {"content": _encode_utf8_bom(SAMPLE_IRIKOMI_CSV), "status_code": 200},
        )()
        calls = []
        original_get = mock_client.get

        def counting_get(u, **kwargs):
            calls.append(u)
            return original_get(u, **kwargs)

        mock_client.get = counting_get
        with TourismClient(ttl=300) as client:
            client.get_irikomi()
            client.get_irikomi()
            client.get_irikomi()
        assert len(calls) == 1

    def test_cache_expiry(self, mock_client):
        """TTL 経過後は再取得する。"""
        mock_client.responses[IRIKOMI_CSV_URL] = type(
            "R",
            (),
            {"content": _encode_utf8_bom(SAMPLE_IRIKOMI_CSV), "status_code": 200},
        )()
        calls = []
        original_get = mock_client.get

        def counting_get(u, **kwargs):
            calls.append(u)
            return original_get(u, **kwargs)

        mock_client.get = counting_get
        with TourismClient(ttl=0.05) as client:
            client.get_irikomi()
            import time

            time.sleep(0.06)
            client.get_irikomi()
        assert len(calls) == 2

    def test_force(self, mock_client):
        """force=True でキャッシュを無視する。"""
        mock_client.responses[IRIKOMI_CSV_URL] = type(
            "R",
            (),
            {"content": _encode_utf8_bom(SAMPLE_IRIKOMI_CSV), "status_code": 200},
        )()
        calls = []
        original_get = mock_client.get

        def counting_get(u, **kwargs):
            calls.append(u)
            return original_get(u, **kwargs)

        mock_client.get = counting_get
        with TourismClient(ttl=300) as client:
            client.get_irikomi()
            client.get_irikomi(force=True)
        assert len(calls) == 2

    def test_thread_safety(self, mock_client):
        """複数スレッドから同時に呼んでも安全。"""
        mock_client.responses[IRIKOMI_CSV_URL] = type(
            "R",
            (),
            {"content": _encode_utf8_bom(SAMPLE_IRIKOMI_CSV), "status_code": 200},
        )()
        errors = []
        results = []

        def worker():
            try:
                with TourismClient(ttl=60) as client:
                    results.append(len(client.get_irikomi()))
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
        """通信エラーは TourismFetchError に変換される。"""

        class Boom:
            def get(self, url, **kwargs):
                raise httpx.ConnectError("boom")

            def close(self):
                pass

        monkeypatch.setattr("nic.core.tourism.httpx.Client", lambda *a, **k: Boom())
        with TourismClient() as client:
            # フォールバック有効ならサンプルが返る（エラーは warnings に記録）
            stats = client.get_irikomi()
        assert len(stats) == 2
        assert any("取得" in w or "CKAN" in w for w in client.warnings)

    def test_http_error_no_fallback(self, mock_client, monkeypatch):
        """フォールバック無効時は通信エラーがそのまま出る。"""

        class Boom:
            def get(self, url, **kwargs):
                raise httpx.ConnectError("boom")

            def close(self):
                pass

        monkeypatch.setattr("nic.core.tourism.httpx.Client", lambda *a, **k: Boom())
        with TourismClient(fallback_to_sample=False) as client:
            with pytest.raises(TourismFetchError):
                client.get_irikomi()
