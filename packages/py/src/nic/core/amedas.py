"""気象庁アメダス（新潟県）データ取得モジュール。

気象庁が提供する「最新の気象データ」CSV ファイル（機械判読データ）から、
新潟県内のアメダス観測所の積雪・気温・降水量を取得する。

データ源:
  - 1時間降水量: https://www.data.jma.go.jp/stats/data/mdrr/pre_rct/alltable/pre1h00_rct.csv
  - 最高気温:    https://www.data.jma.go.jp/stats/data/mdrr/tem_rct/alltable/mxtemsadext00_rct.csv
  - 最低気温:    https://www.data.jma.go.jp/stats/data/mdrr/tem_rct/alltable/mntemsadext00_rct.csv
  - 現在の積雪:  https://www.data.jma.go.jp/stats/data/mdrr/snc_rct/alltable/snc00_rct.csv
    （積雪系 CSV は夏季は提供休止のため 404 になる場合がある）

出典: 気象庁「最新の気象データ」CSVダウンロード
https://www.data.jma.go.jp/stats/data/mdrr/docs/csv_dl_readme.html
"""

from __future__ import annotations

import csv
import io
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

import httpx

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

SOURCE_TEXT = "出典:気象庁"
"""出典表示テキスト（表示・返却データに必ず含める）。"""

SOURCE_URL = "https://www.data.jma.go.jp/stats/data/mdrr/docs/csv_dl_readme.html"

_BASE = "https://www.data.jma.go.jp/stats/data/mdrr"

CSV_URLS: dict[str, str] = {
    "precipitation": f"{_BASE}/pre_rct/alltable/pre1h00_rct.csv",
    "max_temp": f"{_BASE}/tem_rct/alltable/mxtemsadext00_rct.csv",
    "min_temp": f"{_BASE}/tem_rct/alltable/mntemsadext00_rct.csv",
    "snow": f"{_BASE}/snc_rct/alltable/snc00_rct.csv",
}
"""気象要素別の CSV 取得 URL。"""

# 品質情報コード（気象庁「品質情報について」より）
QUALITY_CODES: dict[int, str] = {
    1: "資料なし、未報告",
    2: "利用不適値",
    3: "疑問値",
    4: "資料不足値",
    5: "準正常値",
    8: "正常値",
}


class AmedasError(Exception):
    """アメダスデータ取得に関する基底エラー。"""


class AmedasFetchError(AmedasError):
    """CSV の取得に失敗した場合のエラー。"""


class AmedasParseError(AmedasError):
    """CSV のパースに失敗した場合のエラー。"""


class AmedasStationNotFoundError(AmedasError):
    """指定した観測所番号が新潟県内に存在しない場合のエラー。"""


class AmedasElement(Enum):
    """取得可能な気象要素。"""

    PRECIPITATION = "precipitation"  # 1時間降水量 (mm)
    MAX_TEMP = "max_temp"  # 当日の最高気温 (℃)
    MIN_TEMP = "min_temp"  # 当日の最低気温 (℃)
    SNOW = "snow"  # 現在の積雪 (cm)


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Station:
    """アメダス観測所。"""

    code: str  # 観測所番号 (例: "54232")
    name: str  # 地点名 (例: "新潟")
    lat: float  # 緯度 (度)
    lon: float  # 経度 (度)
    altitude: int  # 標高 (m)
    station_type: str  # 観測所種別 A/B/C
    elements: str  # 観測要素コード (例: "11112010")


@dataclass
class Observation:
    """1 観測所の観測値。"""

    station: Station
    value: float | None  # 観測値（欠測時 None）
    quality: int | None  # 品質情報コード
    quality_text: str  # 品質情報の説明
    observed_at: datetime  # 観測日時 (UTC)
    source: str = SOURCE_TEXT


@dataclass
class AmedasData:
    """取得結果一式。"""

    element: AmedasElement
    observations: list[Observation]
    fetched_at: datetime
    source: str = SOURCE_TEXT


# ---------------------------------------------------------------------------
# 新潟県内のアメダス観測所一覧（44 観測所）
# 出典: 気象庁アメダス観測所位置データ（amedastable.json）より新潟県分を抽出
# ---------------------------------------------------------------------------

NIIGATA_STATIONS: dict[str, Station] = {
    "54012": Station("54012", "粟島", 38.4650, 139.2533, 4, "C", "11112010"),
    "54041": Station("54041", "弾崎", 38.3300, 138.5117, 58, "C", "11112010"),
    "54056": Station("54056", "高根", 38.3300, 139.6033, 85, "C", "01000000"),
    "54086": Station("54086", "村上", 38.2267, 139.4783, 10, "C", "11112010"),
    "54097": Station("54097", "三面", 38.2467, 139.6050, 45, "C", "01000000"),
    "54157": Station("54157", "相川", 38.0283, 138.2400, 6, "B", "11111111"),
    "54166": Station("54166", "両津", 38.0733, 138.4400, 2, "C", "11112010"),
    "54181": Station("54181", "中条", 38.0767, 139.3883, 14, "C", "11112010"),
    "54191": Station("54191", "下関", 38.0917, 139.5633, 33, "C", "11112110"),
    "54232": Station("54232", "新潟", 37.8933, 139.0183, 4, "A", "11111111"),
    "54236": Station("54236", "松浜", 37.9550, 139.1117, 1, "C", "11110100"),
    "54271": Station("54271", "羽茂", 37.8417, 138.3133, 11, "C", "11112010"),
    "54296": Station("54296", "新津", 37.7917, 139.0867, 3, "C", "11112110"),
    "54301": Station("54301", "瓢湖", 37.8333, 139.2367, 9, "C", "01000000"),
    "54311": Station("54311", "赤谷", 37.8350, 139.4150, 135, "C", "01000000"),
    "54341": Station("54341", "巻", 37.7683, 138.9133, 2, "C", "11112010"),
    "54387": Station("54387", "寺泊", 37.6400, 138.7667, 44, "C", "11112010"),
    "54396": Station("54396", "三条", 37.6400, 138.9550, 9, "C", "11112010"),
    "54406": Station("54406", "村松", 37.6967, 139.1883, 25, "C", "01000000"),
    "54421": Station("54421", "津川", 37.6717, 139.4467, 100, "C", "11112110"),
    "54462": Station("54462", "宮寄上", 37.5800, 139.1400, 125, "C", "01000000"),
    "54472": Station("54472", "室谷", 37.5500, 139.3700, 200, "C", "01000000"),
    "54501": Station("54501", "長岡", 37.4500, 138.8233, 23, "C", "11112110"),
    "54506": Station("54506", "栃尾", 37.4783, 138.9917, 61, "C", "01000000"),
    "54541": Station("54541", "柏崎", 37.3517, 138.5533, 7, "C", "11112110"),
    "54566": Station("54566", "守門", 37.3467, 139.0433, 222, "C", "11112110"),
    "54586": Station("54586", "大潟", 37.2250, 138.3250, 13, "C", "11112010"),
    "54606": Station("54606", "小国", 37.2917, 138.7017, 83, "C", "01000000"),
    "54616": Station("54616", "小出", 37.2267, 138.9633, 98, "C", "11112110"),
    "54621": Station("54621", "大湯", 37.2050, 139.0617, 240, "C", "01000000"),
    "54651": Station("54651", "高田", 37.1067, 138.2467, 13, "B", "11111111"),
    "54661": Station("54661", "安塚", 37.1067, 138.4567, 126, "C", "11112110"),
    "54666": Station("54666", "川谷", 37.2000, 138.5167, 206, "C", "01000000"),
    "54671": Station("54671", "松代", 37.1317, 138.6067, 210, "C", "01000000"),
    "54676": Station("54676", "十日町", 37.1433, 138.7267, 170, "C", "11112110"),
    "54711": Station("54711", "糸魚川", 37.0433, 137.8750, 8, "C", "11112010"),
    "54721": Station("54721", "能生", 37.0833, 138.0233, 55, "C", "11112110"),
    "54737": Station("54737", "筒方", 37.0300, 138.3433, 255, "C", "01000000"),
    "54761": Station("54761", "塩沢", 37.0383, 138.8467, 195, "C", "01000000"),
    "54816": Station("54816", "関山", 36.9333, 138.2217, 350, "C", "11112110"),
    "54836": Station("54836", "津南", 36.9967, 138.6833, 452, "C", "11112110"),
    "54841": Station("54841", "湯沢", 36.9417, 138.8100, 340, "C", "11112110"),
    "54876": Station("54876", "平岩", 36.8800, 137.8667, 281, "C", "01000000"),
    "54892": Station("54892", "樽本", 36.8900, 138.2750, 633, "C", "01000000"),
}


# ---------------------------------------------------------------------------
# キャッシュ付きクライアント
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    """キャッシュエントリ。"""

    data: AmedasData
    expires_at: float


class AmedasClient:
    """気象庁アメダス CSV を取得・キャッシュするクライアント。

    TTL 付きインメモリキャッシュを持ち、同一要素への短時間の再取得を
    抑制する（気象庁サーバー負荷軽減のため）。

    Attributes:
        ttl: キャッシュ有効時間（秒）。デフォルト 300 秒。
        timeout: HTTP リクエストのタイムアウト（秒）。
    """

    def __init__(
        self,
        *,
        ttl: float = 300.0,
        timeout: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.ttl = ttl
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        """内部生成した httpx.Client を閉じる。"""
        if self._owns_client and self._client is not None:
            self._client.close()

    def __enter__(self) -> "AmedasClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- 公開 API -----------------------------------------------------------

    def get_stations(self) -> list[Station]:
        """新潟県内の全観測所一覧を返す。"""
        return list(NIIGATA_STATIONS.values())

    def get_station(self, code: str) -> Station:
        """観測所番号から観測所を返す。存在しなければエラー。"""
        try:
            return NIIGATA_STATIONS[code]
        except KeyError:
            raise AmedasStationNotFoundError(
                f"新潟県内に観測所番号 {code} は存在しません"
            ) from None

    def fetch(
        self,
        element: AmedasElement,
        *,
        codes: list[str] | None = None,
        force: bool = False,
    ) -> AmedasData:
        """指定要素のアメダスデータを取得する。

        Args:
            element: 取得する気象要素。
            codes: 対象観測所番号のリスト。None なら新潟県内の全観測所。
            force: True ならキャッシュを無視して再取得。

        Returns:
            AmedasData（観測値リスト・取得日時・出典を含む）。

        Raises:
            AmedasStationNotFoundError: 指定観測所が新潟県内にない場合。
            AmedasFetchError: HTTP 取得に失敗した場合。
            AmedasParseError: CSV パースに失敗した場合。
        """
        stations = self._resolve_stations(codes)
        data = self._get_cached(element, force)
        if data is None:
            data = self._fetch_and_parse(element)
            self._put_cache(element, data)
        if codes is not None:
            # 部分取得の場合は指定観測所のみに絞る
            wanted = {s.code for s in stations}
            data.observations = [o for o in data.observations if o.station.code in wanted]
        return data

    def fetch_precipitation(self, codes: list[str] | None = None, force: bool = False) -> AmedasData:
        """1時間降水量を取得する。"""
        return self.fetch(AmedasElement.PRECIPITATION, codes=codes, force=force)

    def fetch_temperature(self, codes: list[str] | None = None, force: bool = False) -> AmedasData:
        """当日の最高・最低気温を取得する。"""
        return self.fetch(AmedasElement.MAX_TEMP, codes=codes, force=force)

    def fetch_snow(self, codes: list[str] | None = None, force: bool = False) -> AmedasData:
        """現在の積雪深を取得する（夏季は提供休止の場合あり）。"""
        return self.fetch(AmedasElement.SNOW, codes=codes, force=force)

    # -- 内部実装 -----------------------------------------------------------

    def _resolve_stations(self, codes: list[str] | None) -> list[Station]:
        if codes is None:
            return self.get_stations()
        stations = []
        for code in codes:
            stations.append(self.get_station(code))
        return stations

    def _get_cached(self, element: AmedasElement, force: bool) -> AmedasData | None:
        if force:
            return None
        with self._lock:
            entry = self._cache.get(element.value)
            if entry is not None and entry.expires_at > time.monotonic():
                return entry.data
        return None

    def _put_cache(self, element: AmedasElement, data: AmedasData) -> None:
        with self._lock:
            self._cache[element.value] = _CacheEntry(
                data=data, expires_at=time.monotonic() + self.ttl
            )

    def _fetch_and_parse(self, element: AmedasElement) -> AmedasData:
        url = CSV_URLS[element.value]
        raw = self._download(url)
        rows = _parse_csv(raw)
        observations = [self._row_to_observation(element, row) for row in rows]
        observations = [o for o in observations if o is not None]
        return AmedasData(
            element=element,
            observations=observations,
            fetched_at=datetime.now(timezone.utc),
        )

    def _download(self, url: str) -> bytes:
        """CSV をダウンロードし、Shift_JIS でデコードした行リストを返す。"""
        client = self._client
        if client is None:
            client = httpx.Client(timeout=self.timeout, follow_redirects=True)
            self._client = client
            self._owns_client = True
        try:
            resp = client.get(
                url,
                headers={
                    "User-Agent": "nic/0.1 (+https://github.com/Cinnamobot/nic)",
                    "Referer": "https://www.data.jma.go.jp/stats/data/mdrr/",
                },
            )
        except httpx.HTTPError as e:
            raise AmedasFetchError(f"気象庁 CSV の取得に失敗しました: {e}") from e
        if resp.status_code == 404:
            raise AmedasFetchError(
                f"気象庁 CSV が取得できません (HTTP 404)。"
                f"この要素は現在提供休止中の可能性があります: {url}"
            )
        if resp.status_code != 200:
            raise AmedasFetchError(
                f"気象庁 CSV の取得に失敗しました (HTTP {resp.status_code}): {url}"
            )
        # 気象庁は取得上限（1日10GB）超過時に HTTP 403 ではなく
        # 「アクセス制限」を示すエラーページを返す場合があるため、
        # 200 でも実データかどうか（先頭行のヘッダ形式）を検証する。
        if not resp.content or b"HTTP 403" in resp.content[:512] or b"Forbidden" in resp.content[:512]:
            raise AmedasFetchError(
                f"気象庁 CSV が取得できません（アクセス制限の可能性）。"
                f"取得量が制限（1日10GB）に近い場合は時間をおいて再試行してください: {url}"
            )
        return resp.content

    @staticmethod
    def _row_to_observation(
        element: AmedasElement, row: list[str]
    ) -> Observation | None:
        """CSV 1 行を Observation に変換する（新潟県外は None）。"""
        if len(row) < 11:
            return None
        code = row[0].strip()
        pref = row[1].strip()
        if pref != "新潟県" or code not in NIIGATA_STATIONS:
            return None
        station = NIIGATA_STATIONS[code]

        # 観測時刻（日本時間 JST=UTC+9 として解釈し UTC に変換）
        try:
            observed_at = datetime(
                int(row[4]), int(row[5]), int(row[6]),
                int(row[7]), int(row[8]),
                tzinfo=timezone.utc,
            ) - _JST_OFFSET
        except (ValueError, IndexError):
            observed_at = datetime.now(timezone.utc)

        value_raw = row[9].strip()
        quality_raw = row[10].strip()
        try:
            value: float | None = float(value_raw) if value_raw else None
        except ValueError:
            value = None
        try:
            quality: int | None = int(quality_raw) if quality_raw else None
        except ValueError:
            quality = None

        return Observation(
            station=station,
            value=value,
            quality=quality,
            quality_text=QUALITY_CODES.get(quality or 0, "不明"),
            observed_at=observed_at,
        )


# JST (UTC+9) を UTC に戻すための timedelta
_JST_OFFSET = __import__("datetime").timedelta(hours=9)


# ---------------------------------------------------------------------------
# モジュール関数（シンプルな利用向け）
# ---------------------------------------------------------------------------


def parse_csv_bytes(raw: bytes) -> list[list[str]]:
    """Shift_JIS の CSV バイト列をパースして行リストを返す。"""
    return _parse_csv(raw)


def _parse_csv(raw: bytes) -> list[list[str]]:
    """CSV バイト列（Shift_JIS/CRLF）をパースする。"""
    try:
        text = raw.decode("shift_jis")
    except UnicodeDecodeError as e:
        raise AmedasParseError(f"CSV の Shift_JIS デコードに失敗しました: {e}") from e
    reader = csv.reader(io.StringIO(text))
    try:
        rows = [row for row in reader if row and any(cell.strip() for cell in row)]
    except csv.Error as e:
        raise AmedasParseError(f"CSV のパースに失敗しました: {e}") from e
    if not rows:
        raise AmedasParseError("CSV が空です")
    # ヘッダ行を除く
    header = rows[0]
    if "観測所番号" in header[0] or header[0].strip().isdigit() is False:
        rows = rows[1:]
    return rows
