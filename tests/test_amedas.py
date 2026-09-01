"""nic.core.amedas のユニットテスト。"""

from __future__ import annotations

import threading

import httpx
import pytest

from nic.core.amedas import (
    AmedasClient,
    AmedasElement,
    AmedasFetchError,
    AmedasParseError,
    AmedasStationNotFoundError,
    NIIGATA_STATIONS,
    Observation,
    QUALITY_CODES,
    SOURCE_TEXT,
    _JST_OFFSET,
    _parse_csv,
    parse_csv_bytes,
)


# ---------------------------------------------------------------------------
# フィクスチャ / サンプルデータ
# ---------------------------------------------------------------------------

# 実データ形式に合わせたサンプル（Shift_JIS エンコードした CSV）
# 1行目: ヘッダ / 2行目以降: データ（新潟県2地点 + 他県1地点）
SAMPLE_PRE1H_CSV = """観測所番号,都道府県,地点,国際地点番号,現在時刻(年),現在時刻(月),現在時刻(日),現在時刻(時),現在時刻(分),現在値(mm),現在値の品質情報,31日の最大値(mm),31日の最大値の品質情報,31日の最大値起時（時）(まで),31日の最大値起時（分）(まで),31日の最大値起時(まで)の品質情報,極値更新,10年未満での極値更新,30日までの観測史上1位の値(mm),30日までの観測史上1位の値の品質情報,30日までの観測史上1位の値の年,30日までの観測史上1位の値の月,30日までの観測史上1位の値の日,30日までの8月の1位の値(mm),30日までの8月の1位の値の品質情報,30日までの8月の1位の値の年,30日までの8月の1位の値の月,30日までの8月の1位の値の日,統計開始年
54232,新潟県,新潟（ニイガタ）,,2026,08,31,14,10,0.0,8,0.0,4,,,,,,45,8,1996,08,18,45,8,1996,08,18,1978
54841,新潟県,湯沢（ユザワ）,,2026,08,31,14,10,0.5,8,0.5,4,,,,,,94.5,8,2022,08,03,94.5,8,2022,08,03,1981
11001,北海道 宗谷地方,宗谷岬（ソウヤミサキ）,,2026,08,31,14,10,0.0,8,0.0,4,,,,,,45,8,1996,08,18,45,8,1996,08,18,1978
"""

SAMPLE_MXTEM_CSV = """観測所番号,都道府県,地点,国際地点番号,現在時刻(年),現在時刻(月),現在時刻(日),現在時刻(時),現在時刻(分),31日の最高気温(℃),31日の最高気温の品質情報,31日の最高気温起時（時）,31日の最高気温起時（分）,31日の最高気温起時の品質情報,平年差（℃）,前日差（℃）,該当旬（月）,該当旬（旬）,極値更新,10年未満での極値更新,今年最高,今年の最高気温（℃)（30日まで）,今年の最高気温（30日まで）の品質情報,今年の最高気温（30日まで）を観測した起日（年）,今年の最高気温（30日まで）を観測した起日（月）,今年の最高気温（30日まで）を観測した起日（日）,30日までの観測史上1位の値（℃）,30日までの観測史上1位の値の品質情報,30日までの観測史上1位の値を観測した起日（年）,30日までの観測史上1位の値を観測した起日（月）,30日までの観測史上1位の値を観測した起日（日）,30日までの8月の1位の値,30日までの8月の1位の値の品質情報,30日までの8月の1位の値の起日（年）,30日までの8月の1位の値の起日（月）,30日までの8月の1位の値の起日（日）,統計開始年
54232,新潟県,新潟（ニイガタ）,,2026,08,31,13,00,18.9,8,12,16,4,,,,,,,0,29.4,4,2026,08,19,31.9,8,2000,08,01,31.9,8,2000,08,01,1978
54841,新潟県,湯沢（ユザワ）,,2026,08,31,13,00,22.5,8,11,30,4,,,,,,,0,30.1,4,2026,08,19,32.0,8,1994,08,08,32.0,8,1994,08,08,1981
"""

SAMPLE_SNC_CSV = """観測所番号,都道府県,地点,国際地点番号,現在時刻(年),現在時刻(月),現在時刻(日),現在時刻(時),現在時刻(分),現在の積雪の深さ(cm),現在の積雪の深さの品質情報,現在の積雪の深さの平年比（%）,日平年値,日平年値の品質情報,日平年値の現象なし情報,極値更新,10年未満での極値更新,昨冬までの観測史上1位の値（cm）,昨冬までの観測史上1位の値の品質情報,昨冬までの観測史上1位の値観測時の年,昨冬までの観測史上1位の値観測時の月,昨冬までの観測史上1位の値観測時の日,昨冬までの月の1位の値（cm）,昨冬までの月の1位の値の品質情報,昨冬までの月の1位の値観測時の年,昨冬までの月の1位の値観測時の月,昨冬までの月の1位の値観測時の日,統計開始年
54232,新潟県,新潟（ニイガタ）,,2026,01,15,09,00,12,8,100,10,8,0,,,88,8,2025,01,25,58,8,2021,01,12,1978
54841,新潟県,湯沢（ユザワ）,,2026,01,15,09,00,210,8,150,140,8,0,,,310,8,2025,02,10,290,8,2025,01,10,1981
"""


def _encode(csv_text: str) -> bytes:
    """CSV テキストを Shift_JIS にエンコードする。"""
    return csv_text.encode("shift_jis")


@pytest.fixture
def mock_client(monkeypatch):
    """httpx.Client をモックして応答を差し替えるフィクスチャ。"""

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

    monkeypatch.setattr("nic.core.amedas.httpx.Client", fake_client_factory)
    return fake


# ---------------------------------------------------------------------------
# 観測所一覧
# ---------------------------------------------------------------------------


class TestStations:
    def test_niigata_stations_count(self):
        """新潟県の観測所は 44 地点ある。"""
        assert len(NIIGATA_STATIONS) == 44

    def test_known_stations(self):
        """主要観測所が含まれる。"""
        for code in ("54232", "54501", "54841", "54711"):
            assert code in NIIGATA_STATIONS

    def test_station_fields(self):
        """観測所の座標・標高が正しく設定されている。"""
        st = NIIGATA_STATIONS["54232"]  # 新潟
        assert st.name == "新潟"
        assert 37.8 < st.lat < 37.9
        assert 139.0 < st.lon < 139.1
        assert st.altitude == 4

    def test_get_stations(self):
        client = AmedasClient()
        assert len(client.get_stations()) == 44

    def test_get_station_not_found(self):
        client = AmedasClient()
        with pytest.raises(AmedasStationNotFoundError):
            client.get_station("00000")


# ---------------------------------------------------------------------------
# CSV パース
# ---------------------------------------------------------------------------


class TestParse:
    def test_parse_csv_shift_jis(self):
        """Shift_JIS CSV を正しくパースできる。"""
        rows = parse_csv_bytes(_encode(SAMPLE_PRE1H_CSV))
        assert len(rows) == 3  # ヘッダ除く 3 行

    def test_parse_csv_headers_removed(self):
        """1行目のヘッダが除去される。"""
        rows = parse_csv_bytes(_encode(SAMPLE_PRE1H_CSV))
        assert rows[0][0] == "54232"

    def test_parse_csv_empty(self):
        with pytest.raises(AmedasParseError):
            parse_csv_bytes(b"")

    def test_parse_csv_invalid_encoding(self):
        with pytest.raises(AmedasParseError):
            parse_csv_bytes(b"\xff\xfe\x00invalid")


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------


class TestFetch:
    def test_fetch_precipitation(self, mock_client):
        """1時間降水量を取得できる。"""
        mock_client.responses[
            "https://www.data.jma.go.jp/stats/data/mdrr/pre_rct/alltable/pre1h00_rct.csv"
        ] = type("R", (), {"content": _encode(SAMPLE_PRE1H_CSV), "status_code": 200})()
        with AmedasClient() as client:
            data = client.fetch_precipitation()
        assert data.element == AmedasElement.PRECIPITATION
        assert len(data.observations) == 2  # 新潟県のみ（他県は除外）
        assert data.source == SOURCE_TEXT
        for obs in data.observations:
            assert obs.station.code in ("54232", "54841")
            assert obs.quality_text == "正常値"

    def test_fetch_temperature(self, mock_client):
        """最高気温を取得できる。"""
        mock_client.responses[
            "https://www.data.jma.go.jp/stats/data/mdrr/tem_rct/alltable/mxtemsadext00_rct.csv"
        ] = type("R", (), {"content": _encode(SAMPLE_MXTEM_CSV), "status_code": 200})()
        with AmedasClient() as client:
            data = client.fetch_temperature()
        assert len(data.observations) == 2
        new = {o.station.code: o for o in data.observations}
        assert new["54232"].value == 18.9
        assert new["54841"].value == 22.5

    def test_fetch_snow(self, mock_client):
        """積雪を取得できる。"""
        mock_client.responses[
            "https://www.data.jma.go.jp/stats/data/mdrr/snc_rct/alltable/snc00_rct.csv"
        ] = type("R", (), {"content": _encode(SAMPLE_SNC_CSV), "status_code": 200})()
        with AmedasClient() as client:
            data = client.fetch_snow()
        assert len(data.observations) == 2
        new = {o.station.code: o for o in data.observations}
        assert new["54841"].value == 210  # 湯沢 210cm
        assert new["54232"].value == 12

    def test_fetch_with_codes(self, mock_client):
        """観測所番号指定で絞り込みできる。"""
        mock_client.responses[
            "https://www.data.jma.go.jp/stats/data/mdrr/pre_rct/alltable/pre1h00_rct.csv"
        ] = type("R", (), {"content": _encode(SAMPLE_PRE1H_CSV), "status_code": 200})()
        with AmedasClient() as client:
            data = client.fetch_precipitation(codes=["54841"])
        assert len(data.observations) == 1
        assert data.observations[0].station.code == "54841"

    def test_fetch_invalid_code(self, mock_client):
        """存在しない観測所番号はエラー。"""
        with AmedasClient() as client:
            with pytest.raises(AmedasStationNotFoundError):
                client.fetch_precipitation(codes=["99999"])

    def test_fetch_404(self, mock_client):
        """404（夏季の積雪提供休止など）は明瞭なエラー。"""
        # 何も登録しない → 404 が返る
        with AmedasClient() as client:
            with pytest.raises(AmedasFetchError) as exc_info:
                client.fetch_snow()
        assert "404" in str(exc_info.value)
        assert "休止" in str(exc_info.value)

    def test_fetch_http_error(self, mock_client, monkeypatch):
        """通信エラーは AmedasFetchError に変換される。"""

        class Boom:
            def get(self, url, **kwargs):
                raise httpx.ConnectError("boom")

            def close(self):
                pass

        monkeypatch.setattr("nic.core.amedas.httpx.Client", lambda *a, **k: Boom())
        with AmedasClient() as client:
            with pytest.raises(AmedasFetchError):
                client.fetch_precipitation()

    def test_missing_value(self):
        """欠測（空欄）は value=None になる。"""
        rows = _parse_csv(_encode(SAMPLE_PRE1H_CSV))
        # 手動で value 空欄の行を作る
        row = rows[0][:]
        row[9] = ""
        obs = AmedasClient._row_to_observation(AmedasElement.PRECIPITATION, row)
        assert obs is not None
        assert obs.value is None
        assert obs.quality == 8


# ---------------------------------------------------------------------------
# キャッシュ
# ---------------------------------------------------------------------------


class TestCache:
    def test_cache_ttl(self, mock_client):
        """TTL 内はキャッシュが使われ再取得しない。"""
        url = "https://www.data.jma.go.jp/stats/data/mdrr/pre_rct/alltable/pre1h00_rct.csv"
        resp = type("R", (), {"content": _encode(SAMPLE_PRE1H_CSV), "status_code": 200})()
        mock_client.responses[url] = resp
        calls = []

        original_get = mock_client.get

        def counting_get(u, **kwargs):
            calls.append(u)
            return original_get(u, **kwargs)

        mock_client.get = counting_get
        with AmedasClient(ttl=300) as client:
            client.fetch_precipitation()
            client.fetch_precipitation()
            client.fetch_precipitation()
        assert len(calls) == 1  # 1回しか HTTP に行かない

    def test_cache_expiry(self, mock_client):
        """TTL 経過後は再取得する。"""
        url = "https://www.data.jma.go.jp/stats/data/mdrr/pre_rct/alltable/pre1h00_rct.csv"
        resp = type("R", (), {"content": _encode(SAMPLE_PRE1H_CSV), "status_code": 200})()
        mock_client.responses[url] = resp
        calls = []
        original_get = mock_client.get

        def counting_get(u, **kwargs):
            calls.append(u)
            return original_get(u, **kwargs)

        mock_client.get = counting_get
        with AmedasClient(ttl=0.05) as client:  # 50ms
            client.fetch_precipitation()
            import time

            time.sleep(0.06)
            client.fetch_precipitation()
        assert len(calls) == 2

    def test_force(self, mock_client):
        """force=True でキャッシュを無視する。"""
        url = "https://www.data.jma.go.jp/stats/data/mdrr/pre_rct/alltable/pre1h00_rct.csv"
        resp = type("R", (), {"content": _encode(SAMPLE_PRE1H_CSV), "status_code": 200})()
        mock_client.responses[url] = resp
        calls = []
        original_get = mock_client.get

        def counting_get(u, **kwargs):
            calls.append(u)
            return original_get(u, **kwargs)

        mock_client.get = counting_get
        with AmedasClient(ttl=300) as client:
            client.fetch_precipitation()
            client.fetch_precipitation(force=True)
        assert len(calls) == 2

    def test_thread_safety(self, mock_client):
        """複数スレッドから同時に呼んでも安全。"""
        url = "https://www.data.jma.go.jp/stats/data/mdrr/pre_rct/alltable/pre1h00_rct.csv"
        resp = type("R", (), {"content": _encode(SAMPLE_PRE1H_CSV), "status_code": 200})()
        mock_client.results = []
        mock_client.responses[url] = resp
        errors = []

        def worker():
            try:
                with AmedasClient(ttl=60) as client:
                    data = client.fetch_precipitation()
                    mock_client.results.append(len(data.observations))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(mock_client.results) == 8
        assert all(r == 2 for r in mock_client.results)


# ---------------------------------------------------------------------------
# 品質情報
# ---------------------------------------------------------------------------


class TestQuality:
    def test_quality_codes(self):
        assert QUALITY_CODES[8] == "正常値"
        assert QUALITY_CODES[1] == "資料なし、未報告"
        assert QUALITY_CODES[4] == "資料不足値"

    def test_observation_time_is_utc(self, mock_client):
        """観測時刻は JST→UTC 変換される。"""
        mock_client.responses[
            "https://www.data.jma.go.jp/stats/data/mdrr/pre_rct/alltable/pre1h00_rct.csv"
        ] = type("R", (), {"content": _encode(SAMPLE_PRE1H_CSV), "status_code": 200})()
        with AmedasClient() as client:
            data = client.fetch_precipitation()
        obs = data.observations[0]
        # CSV は 2026-08-31 14:10 JST → UTC は 05:10
        assert obs.observed_at.hour == 5
        assert obs.observed_at.minute == 10
        assert obs.observed_at.tzinfo is not None

    def test_jst_offset_exists(self):
        assert _JST_OFFSET.total_seconds() == 9 * 3600
