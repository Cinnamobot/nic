"""新潟県観光スポット取得モジュール。

新潟県の観光スポット（温泉・文化施設・集客施設）を、機械判読可能な
公式データ源から取得する。スポット・イベントの機械判読可能データは
公式には存在しない（調査レポート参照）ため、以下の代替データ源を
組み合わせてスポット一覧を構築する。

データ源（取得優先順）:
  1. 新潟市 CKAN API（https://opendata.city.niigata.lg.jp/api/3/action/package_search、
     失敗時は HTTP 版にフォールバック）
     観光関連データセット一覧を取得する。データセット自体のカタログ情報
     （タイトル・リソース URL・ライセンス・更新日）を返す。
  2. 新潟市観光入込客数 CSV（CKAN のリソース実体、UTF-8 BOM 付き）
     年別・分類別の入込客数統計をパースする。
  3. 新潟市 GIS 温泉利用許可施設 CSV（緯度経度・泉質付き、24 件）
     温泉スポットとしてパースする。
  4. 国土数値情報 P33 集客施設データ（Shapefile / DBF、Shift-JIS、2014年度版）
     映画館・公会堂・劇場等 359 施設をスポットとして統合する。
     座標は SHP ファイル（Point, JGD2000 緯度経度）から読み取る。

出典: 新潟市オープンデータ（CC-BY）/ 国土数値情報（国土交通省）
全レスポンスに source / source_url を含めて出典を明記する。

利用条件:
  - 新潟市: クリエイティブ・コモンズ 表示（CC-BY）
    https://creativecommons.org/licenses/by/4.0/deed.ja
  - 国土数値情報: 国土数値情報利用約款（出典明記で無償利用・加工・再配布可）
    https://nlftp.mlit.go.jp/ksj/other/agreement.html
"""

from __future__ import annotations

import csv
import io
import re
import struct
import threading
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

SOURCE_TEXT = "出典:新潟市オープンデータ"
"""出典表示テキスト（表示・返却データに必ず含める）。"""

SOURCE_URL = "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/"
"""新潟市オープンデータのポータルページ。"""

CKAN_BASE_URL = "http://opendata.city.niigata.lg.jp/api/3/action/package_search"
"""新潟市 CKAN API（package_search エンドポイント）。認証不要。"""

P33_SOURCE_TEXT = "出典:国土数値情報（国土交通省）"
"""国土数値情報の出典表示テキスト。"""

P33_SOURCE_URL = "https://nlftp.mlit.go.jp/ksj/gml/gisdata.html"
"""国土数値情報ダウンロードサービスのページ。"""

P33_ZIP_URL = "https://nlftp.mlit.go.jp/ksj/gml/data/P33/P33-14/P33-14_15_GML.zip"
"""国土数値情報 P33 集客施設データ（新潟県分・2014年度）の ZIP URL。"""

# CKAN の package_search で観光関連データセットを検索するクエリ。
# 新潟市 CKAN は「観光」のタグ検索で 2 件（入込客数・Free Wi-Fi）がヒットし、
# GIS 系（温泉・文化施設等）はタグが異なるため、カタログ全体を取得して
# 観光関連パッケージ名（opendata-kankou / od-gis_kankobunspo）で絞り込む。
CKAN_TOURISM_QUERY = "観光"
CKAN_ROWS = 1000

# 観光関連パッケージ名の判定パターン（新潟市 CKAN の実パッケージ名）
_TOURISM_PACKAGE_PREFIXES = (
    "opendata-kankou_",
    "od-gis_kankobunspo_",
)
_TOURISM_KEYWORDS = ("観光", "温泉", "入込", "集客", "海水浴", "美術館", "博物館", "水族館", "遺跡")

# GIS 温泉利用許可施設 CSV の実体 URL（新潟市オープンデータ）
ONSEN_CSV_URL = (
    "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/"
    "opendata-gis/od-gis_kankobunspo/od-gis_onseninst.files/od_gis_10096_onseninstitution.csv"
)
"""GIS 温泉利用許可施設 CSV（緯度経度・泉質付き、24 件）。"""

# 観光入込客数 CSV の実体 URL（新潟市オープンデータ、年次更新）
IRIKOMI_CSV_URL = (
    "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/"
    "opendata-kankou/od-irikomidata.files/irikomidataR6.csv"
)
"""新潟市観光入込客数 CSV（2010〜令和6年、年別・分類別）。"""

DEFAULT_TTL = 3600.0
"""キャッシュ有効時間（秒）。統計・GIS データは年次〜不定期更新のため 1 時間が妥当。"""

USER_AGENT = "nic/0.1 (+https://github.com/Cinnamobot/nic)"

# 国土数値情報 P33 の施設区分コード → 日本語名
# 出典: 国土数値情報 P33 集客施設データ 仕様書（P33-14_GML データ仕様書 第1.1版）
P33_FACILITY_TYPES: dict[str, str] = {
    "1": "映画館",
    "2": "公会堂",
    "3": "劇場",
    "4": "展示場",
    "5": "体育館・観覧場",
    "6": "その他",
}


class TourismError(Exception):
    """観光データ取得に関する基底エラー。"""


class TourismFetchError(TourismError):
    """データ取得（通信・HTTP エラー）に失敗した場合のエラー。"""


class TourismParseError(TourismError):
    """取得データのパースに失敗した場合のエラー。"""


class TourismNotFoundError(TourismError):
    """要求されたデータが見つからない場合のエラー。"""


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TourismDataset:
    """新潟市 CKAN カタログ上の観光関連データセット 1 件。"""

    id: str  # CKAN パッケージ ID
    name: str  # パッケージ名（機械判読用 ID）
    title: str  # データセット名
    description: str  # 概要
    license: str  # ライセンス名（例: クリエイティブ・コモンズ 表示）
    license_url: str  # ライセンス URL
    updated_at: str  # 最終更新日時（ISO8601）
    url: str  # データセットの公開ページ
    resources: tuple[str, ...] = ()  # リソース（実データ）URL 一覧
    source: str = SOURCE_TEXT
    source_url: str = CKAN_BASE_URL


@dataclass(frozen=True)
class TourismStat:
    """観光入込客数の 1 年分レコード。"""

    year: int  # 西暦
    era_year: str  # 和暦（例: 令和6）
    total: int | None = None  # 観光入込客数合計（千人）
    event_total: int | None = None  # 行祭事・イベント合計（千人）
    spot_total: int | None = None  # 観光地点合計（千人）
    nature: int | None = None  # 自然（千人）
    history_culture: int | None = None  # 歴史・文化（千人）
    onsen_health: int | None = None  # 温泉・健康（千人）
    sports_recreation: int | None = None  # スポーツ・レクリエーション（千人）
    urban_tourism: int | None = None  # 都市型観光（千人）
    other: int | None = None  # その他（千人）
    source: str = SOURCE_TEXT
    source_url: str = IRIKOMI_CSV_URL


@dataclass(frozen=True)
class Spot:
    """観光スポット 1 件（温泉・文化施設・集客施設の共通レコード）。"""

    id: str  # データ源内で一意な ID
    name: str  # スポット名（施設名）
    category: str  # 区分（温泉 / 美術館 / 集客施設など）
    lat: float | None  # 緯度（度）。不明時 None
    lon: float | None  # 経度（度）。不明時 None
    address: str = ""  # 住所
    phone: str = ""  # 電話番号
    url: str = ""  # 公式 URL（あれば）
    description: str = ""  # 補足説明（泉質・営業時間など）
    source: str = SOURCE_TEXT
    source_url: str = SOURCE_URL


# ---------------------------------------------------------------------------
# 内蔵サンプルデータ（オフライン用フォールバック）
# ---------------------------------------------------------------------------
# 出典: 新潟市オープンデータ / 国土数値情報（抜粋・構造化）
# 実データが取得できない環境（オフライン等）でも動作させるための最小限データ。

_SAMPLE_DATASETS: list[dict[str, object]] = [
    {
        "id": "16a13911-06c9-4339-aec6-30c092846c83",
        "name": "opendata-kankou_od-irikomidata",
        "title": "新潟市観光入込客数",
        "description": "年別・分類別の観光入込客数",
        "license": "クリエイティブ・コモンズ 表示",
        "license_url": "https://creativecommons.org/licenses/by/4.0/deed.ja",
        "updated_at": "2026-03-04T06:03:28.768642",
        "url": "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-kankou/od-irikomidata.html",
        "resources": [IRIKOMI_CSV_URL],
    },
    {
        "id": "83958165-3b29-426d-abc3-c3bb519d893a",
        "name": "opendata-kankou_od-citywifi",
        "title": "Niigata City Free Wi-Fi利用可能施設一覧",
        "description": "観光客向け Free Wi-Fi 利用可能施設の一覧",
        "license": "クリエイティブ・コモンズ 表示",
        "license_url": "https://creativecommons.org/licenses/by/4.0/deed.ja",
        "updated_at": "2026-07-06T04:02:56.219033",
        "url": "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-kankou/od-citywifi.html",
        "resources": [
            "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-kankou/od-citywifi.files/od-city_wifi_ichiran_20240401.csv",
        ],
    },
    {
        "id": "sample-onsen",
        "name": "od-gis_kankobunspo_od-gis_onseninst",
        "title": "GIS 温泉利用を許可した施設",
        "description": "温泉利用許可施設（緯度経度・泉質付き）",
        "license": "クリエイティブ・コモンズ 表示",
        "license_url": "https://creativecommons.org/licenses/by/4.0/deed.ja",
        "updated_at": "2023-03-29T00:00:00",
        "url": "https://www.city.niigata.lg.jp/shisei/seisaku/it/open-data/opendata-gis/od-gis_kankobunspo/od-gis_onseninst.html",
        "resources": [ONSEN_CSV_URL],
    },
]

_SAMPLE_IRIKOMI: list[dict[str, object]] = [
    {
        "year": 2023,
        "era_year": "令和5",
        "total": 15557,
        "event_total": 4382,
        "spot_total": 11175,
        "nature": 419,
        "history_culture": 3100,
        "onsen_health": 818,
        "sports_recreation": 1792,
        "urban_tourism": 5046,
        "other": 0,
    },
    {
        "year": 2024,
        "era_year": "令和6",
        "total": 16019,
        "event_total": 4591,
        "spot_total": 11428,
        "nature": 425,
        "history_culture": 3044,
        "onsen_health": 861,
        "sports_recreation": 2026,
        "urban_tourism": 5072,
        "other": 0,
    },
]

_SAMPLE_ONSEN_SPOTS: list[dict[str, object]] = [
    {
        "id": "onsen-28",
        "name": "ほてる大橋館の湯",
        "category": "温泉",
        "lat": 37.7380947,
        "lon": 138.8398538,
        "address": "新潟市西蒲区岩室温泉340-甲",
        "phone": "0256-82-4125",
        "url": "",
        "description": "岩室温泉（含硫黄－ナトリウム･カルシウム－塩化物泉）",
    },
    {
        "id": "onsen-39",
        "name": "多宝温泉　だいろの湯",
        "category": "温泉",
        "lat": 37.7280278,
        "lon": 138.837374,
        "address": "新潟市西蒲区石瀬3250",
        "phone": "0256-82-1126",
        "url": "",
        "description": "多宝温泉だいろの湯（含硫黄－ナトリウム・カルシウム－塩化物泉、他）",
    },
]

_SAMPLE_P33_SPOTS: list[dict[str, object]] = [
    {
        "id": "p33-1",
        "name": "シネ・ウインド",
        "category": "集客施設（映画館）",
        "lat": 37.915809210064,
        "lon": 139.05391640076,
        "address": "新潟市中央区八千代2-1-1（1F）",
        "phone": "025-243-5530",
        "url": "http://cinewind.com/",
        "description": "映画館",
    },
    {
        "id": "p33-10",
        "name": "川前公民館",
        "category": "集客施設（公会堂）",
        "lat": None,
        "lon": None,
        "address": "燕市中川597-1",
        "phone": "0256-63-9310",
        "url": "",
        "description": "公会堂",
    },
]


# ---------------------------------------------------------------------------
# キャッシュ付きクライアント
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    """キャッシュエントリ。"""

    data: Any
    expires_at: float


class TourismClient:
    """新潟県観光データ取得クライアント。

    新潟市 CKAN API・GIS 温泉 CSV・観光入込客数 CSV・国土数値情報 P33 を
    取得し、TTL 付きインメモリキャッシュで保持する。

    Attributes:
        ttl: キャッシュ有効時間（秒）。デフォルト 3600 秒。
        timeout: HTTP リクエストのタイムアウト（秒）。
        fallback_to_sample: 全データ源が失敗したとき内蔵サンプルに
            フォールバックするか（デフォルト True）。
    """

    def __init__(
        self,
        *,
        ttl: float = DEFAULT_TTL,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
        fallback_to_sample: bool = True,
    ) -> None:
        self.ttl = ttl
        self.timeout = timeout
        self.fallback_to_sample = fallback_to_sample
        self._client = client
        self._owns_client = client is None
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()
        self._warnings: list[str] = []
        """直近の取得で発生したフォールバック状況（データ源の失敗理由など）の記録。"""

    def close(self) -> None:
        """内部生成した httpx.Client を閉じる。"""
        if self._owns_client and self._client is not None:
            self._client.close()

    def __enter__(self) -> "TourismClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def warnings(self) -> list[str]:
        """直近の取得で発生したフォールバック状況の説明一覧。"""
        return list(self._warnings)

    # -- 公開 API -----------------------------------------------------------

    def get_datasets(
        self, *, query: str | None = None, force: bool = False
    ) -> list[TourismDataset]:
        """新潟市 CKAN から観光関連データセット一覧を取得する。

        CKAN の package_search で「観光」を検索した結果に加え、
        カタログ全体から観光関連パッケージ（opendata-kankou_* /
        od-gis_kankobunspo_*）を収集して返す。

        Args:
            query: データセット名・概要に対する部分一致検索語（None で観光関連全件）。
            force: True ならキャッシュを無視して再取得。

        Returns:
            TourismDataset のリスト（出典・ライセンス付き）。

        Raises:
            TourismFetchError: 全データ源の取得に失敗し、フォールバックも無効な場合。
        """
        datasets = self._get_cached("datasets", force)
        if datasets is None:
            datasets = self._fetch_datasets()
            self._put_cache("datasets", datasets)
        if query:
            q = query.strip()
            datasets = [
                d
                for d in datasets
                if q in d.title or q in d.name or q in d.description
            ]
        return datasets

    def get_irikomi(
        self, *, year: int | None = None, force: bool = False
    ) -> list[TourismStat]:
        """観光入込客数（新潟市、年別・分類別）を取得する。

        Args:
            year: 西暦年で絞り込み（例: 2024）。None で全件。
            force: True ならキャッシュを無視して再取得。

        Returns:
            TourismStat のリスト。取得できない場合は内蔵サンプルを返す。
        """
        stats = self._get_cached("irikomi", force)
        if stats is None:
            stats = self._fetch_irikomi()
            self._put_cache("irikomi", stats)
        if year is not None:
            stats = [s for s in stats if s.year == year]
        return stats

    def get_onsen_spots(self, *, force: bool = False) -> list[Spot]:
        """温泉スポット（新潟市 GIS 温泉利用許可施設）を取得する。

        Args:
            force: True ならキャッシュを無視して再取得。

        Returns:
            温泉スポットのリスト（緯度経度・泉質付き）。取得できない場合は
            内蔵サンプルを返す。
        """
        spots = self._get_cached("onsen", force)
        if spots is None:
            spots = self._fetch_onsen_spots()
            self._put_cache("onsen", spots)
        return spots

    def get_p33_spots(self, *, force: bool = False) -> list[Spot]:
        """国土数値情報 P33 集客施設をスポットとして取得する。

        Args:
            force: True ならキャッシュを無視して再取得。

        Returns:
            集客施設スポットのリスト（2014年度版・359 件想定）。
            取得できない場合は内蔵サンプルを返す。
        """
        spots = self._get_cached("p33", force)
        if spots is None:
            spots = self._fetch_p33_spots()
            self._put_cache("p33", spots)
        return spots

    def get_spots(
        self,
        *,
        category: str | None = None,
        include_onsen: bool = True,
        include_p33: bool = True,
        force: bool = False,
    ) -> list[Spot]:
        """観光スポット一覧を取得する（温泉 + 集客施設の統合）。

        データ源ごとに別々のキャッシュキーで取得し、統合して返す。
        データ源の一部が失敗しても、成功した分は返す（warnings に記録）。

        Args:
            category: 区分で絞り込み（例: "温泉", "集客施設"）。None で全件。
            include_onsen: 温泉スポットを含めるか（デフォルト True）。
            include_p33: 国土数値情報 P33 集客施設を含めるか（デフォルト True）。
            force: True ならキャッシュを無視して再取得。

        Returns:
            統合された Spot のリスト。

        Raises:
            TourismFetchError: 全データ源が失敗し、フォールバックも無効な場合。
        """
        spots: list[Spot] = []
        if include_onsen:
            try:
                spots.extend(self.get_onsen_spots(force=force))
            except TourismError as e:
                if not self.fallback_to_sample:
                    self._warn(f"温泉データを取得できませんでした（{e}）。")
                    raise
                self._warn(f"温泉データを取得できませんでした（{e}）。")
        if include_p33:
            try:
                spots.extend(self.get_p33_spots(force=force))
            except TourismError as e:
                if not self.fallback_to_sample:
                    self._warn(f"集客施設データを取得できませんでした（{e}）。")
                    raise
                self._warn(f"集客施設データを取得できませんでした（{e}）。")
        if not spots and self.fallback_to_sample:
            # 全データ源が失敗した場合の最終フォールバック
            spots = [
                *_sample_onsen_spots(),
                *_sample_p33_spots(),
            ]
            self._warn(
                "外部データ源を利用できなかったため、内蔵サンプルデータを返します。"
            )
        if not spots and not self.fallback_to_sample:
            raise TourismFetchError(
                "観光スポットを取得できませんでした（温泉・集客施設データ源がすべて失敗）"
            )
        if category:
            spots = [s for s in spots if s.category == category.strip()]
        return spots

    # -- 内部実装 -----------------------------------------------------------

    def _get_cached(self, key: str, force: bool) -> Any:
        if force:
            return None
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and entry.expires_at > time.monotonic():
                return entry.data
        return None

    def _put_cache(self, key: str, data: Any) -> None:
        with self._lock:
            self._cache[key] = _CacheEntry(
                data=data, expires_at=time.monotonic() + self.ttl
            )

    def _warn(self, message: str) -> None:
        self._warnings.append(message)

    def _download(self, url: str, *, binary: bool = False) -> bytes:
        """URL からバイト列を取得する（ヘッダ付き・リダイレクト追従）。"""
        client = self._client
        if client is None:
            client = httpx.Client(timeout=self.timeout, follow_redirects=True)
            self._client = client
            self._owns_client = True
        headers = {"User-Agent": USER_AGENT}
        if not binary:
            headers["Accept"] = "text/csv,application/json,application/octet-stream,*/*"
        try:
            resp = client.get(url, headers=headers)
        except httpx.HTTPError as e:
            raise TourismFetchError(f"HTTP 取得に失敗しました: {url} ({e})") from e
        if resp.status_code != 200:
            raise TourismFetchError(
                f"HTTP {resp.status_code} で取得できませんでした: {url}"
            )
        return resp.content

    # -- データ源 1: 新潟市 CKAN API ------------------------------------------

    def _fetch_datasets(self) -> list[TourismDataset]:
        """CKAN package_search で観光関連データセット一覧を取得する。失敗時サンプル。"""
        datasets = self._fetch_from_ckan()
        if datasets:
            return datasets
        if self.fallback_to_sample:
            self._warn(
                "新潟市 CKAN API を利用できなかったため、内蔵サンプル（3 件）を返します。"
            )
            return [_sample_dataset(d) for d in _SAMPLE_DATASETS]
        raise TourismFetchError(
            "新潟市 CKAN API から観光データセットを取得できませんでした"
        )

    def _fetch_from_ckan(self) -> list[TourismDataset] | None:
        """CKAN API から観光関連パッケージを収集する。失敗時 None。"""
        # 1) タグ検索（q=観光）
        packages: list[dict[str, Any]] = []
        try:
            raw = self._download(f"{CKAN_BASE_URL}?q={_urlencode(CKAN_TOURISM_QUERY)}&rows={CKAN_ROWS}")
            payload = _parse_json(raw)
            if isinstance(payload, dict) and payload.get("success") is True:
                result = payload.get("result")
                if isinstance(result, dict):
                    packages.extend(result.get("results") or [])
        except (TourismFetchError, TourismParseError) as e:
            self._warn(f"CKAN 検索に失敗しました（{e}）。")
            return None

        # 2) カタログ全体から観光関連パッケージを収集
        if len(packages) < 50:  # タグ検索結果が少ない場合は全体も取得
            try:
                raw = self._download(f"{CKAN_BASE_URL}?rows={CKAN_ROWS}")
                payload = _parse_json(raw)
                if isinstance(payload, dict) and payload.get("success") is True:
                    result = payload.get("result")
                    if isinstance(result, dict):
                        for p in result.get("results") or []:
                            if _is_tourism_package(p):
                                packages.append(p)
            except (TourismFetchError, TourismParseError) as e:
                self._warn(f"CKAN カタログ全体の取得に失敗しました（{e}）。")

        # 重複（同名パッケージ）を除去
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for p in packages:
            pid = str(p.get("id", ""))
            if pid and pid not in seen:
                seen.add(pid)
                unique.append(p)

        if not unique:
            self._warn("CKAN API に観光関連データセットが見つかりませんでした。")
            return None
        return [_parse_ckan_package(p) for p in unique]

    # -- データ源 2: 観光入込客数 CSV -----------------------------------------

    def _fetch_irikomi(self) -> list[TourismStat]:
        """観光入込客数 CSV を取得・パースする。失敗時サンプル。"""
        try:
            raw = self._download(IRIKOMI_CSV_URL)
            stats = _parse_irikomi_csv(raw, source_url=IRIKOMI_CSV_URL)
            if stats:
                self._warn(f"観光入込客数を取得しました: {IRIKOMI_CSV_URL}（{len(stats)} 年分）")
                return stats
        except (TourismFetchError, TourismParseError) as e:
            self._warn(f"観光入込客数 CSV の取得に失敗しました（{e}）。")
        if self.fallback_to_sample:
            self._warn("観光入込客数を外部取得できなかったため、内蔵サンプルを返します。")
            return [_sample_irikomi(d) for d in _SAMPLE_IRIKOMI]
        raise TourismFetchError("観光入込客数を取得できませんでした")

    # -- データ源 3: GIS 温泉利用許可施設 CSV ---------------------------------

    def _fetch_onsen_spots(self) -> list[Spot]:
        """GIS 温泉利用許可施設 CSV を取得・パースする。失敗時サンプル。"""
        try:
            raw = self._download(ONSEN_CSV_URL)
            spots = _parse_onsen_csv(raw, source_url=ONSEN_CSV_URL)
            if spots:
                self._warn(
                    f"温泉施設を取得しました: {ONSEN_CSV_URL}（{len(spots)} 件）"
                )
                return spots
        except (TourismFetchError, TourismParseError) as e:
            self._warn(f"温泉施設 CSV の取得に失敗しました（{e}）。")
        if self.fallback_to_sample:
            self._warn("温泉施設を外部取得できなかったため、内蔵サンプルを返します。")
            return _sample_onsen_spots()
        raise TourismFetchError("温泉施設を取得できませんでした")

    # -- データ源 4: 国土数値情報 P33 集客施設 ---------------------------------

    def _fetch_p33_spots(self) -> list[Spot]:
        """国土数値情報 P33 集客施設（Shapefile/DBF + SHP）を取得・パースする。"""
        try:
            raw = self._download(P33_ZIP_URL, binary=True)
            spots = parse_p33_zip(raw, source_url=P33_ZIP_URL)
            if spots:
                self._warn(
                    f"国土数値情報 P33 集客施設を取得しました: {P33_ZIP_URL}（{len(spots)} 件）"
                )
                return spots
        except (TourismFetchError, TourismParseError) as e:
            self._warn(f"国土数値情報 P33 の取得に失敗しました（{e}）。")
        if self.fallback_to_sample:
            self._warn("国土数値情報 P33 を外部取得できなかったため、内蔵サンプルを返します。")
            return _sample_p33_spots()
        raise TourismFetchError("国土数値情報 P33 集客施設を取得できませんでした")


# ---------------------------------------------------------------------------
# パース補助関数
# ---------------------------------------------------------------------------


def _decode_text(raw: bytes) -> str:
    """UTF-8(BOM 付き含む) / CP932 を自動判別してテキストにデコードする。"""
    for enc in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise TourismParseError("文字コードを判別できませんでした（UTF-8 / CP932 以外）")


def _parse_csv_rows(text: str) -> list[list[str]]:
    """CSV テキストを行リストにパースする。"""
    try:
        return [row for row in csv.reader(io.StringIO(text)) if row and any(c.strip() for c in row)]
    except csv.Error as e:
        raise TourismParseError(f"CSV のパースに失敗しました: {e}") from e


def _parse_json(raw: bytes) -> Any:
    """JSON バイト列をパースする。"""
    import json

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise TourismParseError(f"JSON のパースに失敗しました: {e}") from e


def _urlencode(value: str) -> str:
    """クエリ文字列用に URL エンコードする。"""
    from urllib.parse import quote

    return quote(value, safe="")


def _to_int(value: str | int | float | None) -> int | None:
    """CSV の数値文字列を int に変換する。空・不正値は None。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(float(s.replace(",", "")))
    except ValueError:
        return None


def _to_float(value: str | int | float | None) -> float | None:
    """CSV の数値文字列を float に変換する。空・不正値は None。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# CKAN レスポンスのパース
# ---------------------------------------------------------------------------


def _is_tourism_package(pkg: dict[str, Any]) -> bool:
    """CKAN パッケージが観光関連か判定する。

    パッケージ名の接頭辞（opendata-kankou_ / od-gis_kankobunspo_）または
    タイトル・タグに観光関連キーワードが含まれる場合に観光関連とみなす。
    """
    name = str(pkg.get("name", ""))
    if any(name.startswith(p) for p in _TOURISM_PACKAGE_PREFIXES):
        return True
    title = str(pkg.get("title", ""))
    if any(k in title for k in _TOURISM_KEYWORDS):
        return True
    tags = [str(t.get("name", "")) for t in pkg.get("tags", []) if isinstance(t, dict)]
    return any("観光" in t or "温泉" in t for t in tags)


def _parse_ckan_package(pkg: dict[str, Any]) -> TourismDataset:
    """CKAN のパッケージ 1 件を TourismDataset に変換する。"""
    resources = [
        str(r.get("url", ""))
        for r in pkg.get("resources", [])
        if isinstance(r, dict) and r.get("url")
    ]
    return TourismDataset(
        id=str(pkg.get("id", "")),
        name=str(pkg.get("name", "")),
        title=str(pkg.get("title", "")),
        description=str(pkg.get("notes", "")),
        license=str(pkg.get("license_title", "")),
        license_url=str(pkg.get("license_url", "") or ""),
        updated_at=str(pkg.get("metadata_modified", "")),
        url=str(pkg.get("url", "")),
        resources=tuple(resources),
        source=SOURCE_TEXT,
        source_url=CKAN_BASE_URL,
    )


# ---------------------------------------------------------------------------
# 観光入込客数 CSV のパース
# ---------------------------------------------------------------------------

_IRIKOMI_HEADER = (
    "年[西暦]",
    "年[和暦]",
    "観光入込客数合計[千人]",
    "行祭事・イベント合計[千人]",
    "観光地点合計[千人]",
    "観光地点合計の自然[千人]",
    "観光地点合計の歴史・文化[千人]",
    "観光地点合計の温泉・健康[千人]",
    "観光地点合計のスポーツ・レクリエーション[千人]",
    "観光地点合計の都市型観光[千人]",
    "観光地点合計のその他[千人]",
)


def _parse_irikomi_csv(raw: bytes, source_url: str) -> list[TourismStat]:
    """新潟市観光入込客数 CSV（UTF-8 BOM 付き・11 カラム）をパースする。

    CSV の列構成（実データ確認済み）:
      年[西暦], 年[和暦], 観光入込客数合計[千人], 行祭事・イベント合計[千人],
      観光地点合計[千人], 観光地点合計の自然[千人], 観光地点合計の歴史・文化[千人],
      観光地点合計の温泉・健康[千人], 観光地点合計のスポーツ・レクリエーション[千人],
      観光地点合計の都市型観光[千人], 観光地点合計のその他[千人]
    """
    text = _decode_text(raw)
    rows = _parse_csv_rows(text)
    if not rows:
        raise TourismParseError("観光入込客数 CSV が空です")
    header = [h.strip() for h in rows[0]]

    # ヘッダの列名からインデックスを解決する（列順の変化に強い）
    col_index: dict[str, int] = {}
    for idx, name in enumerate(header):
        key = name
        # 表記揺れ（全角/半角・改行など）を吸収する
        normalized = (
            name.replace("[千人]", "")
            .replace(" ", "")
            .replace("　", "")
            .replace("年[西暦]", "年西暦")
            .replace("年[和暦]", "年和暦")
        )
        if normalized.startswith("観光入込客数合計"):
            key = "total"
        elif normalized.startswith("行祭事"):
            key = "event_total"
        elif normalized.startswith("観光地点合計の自然"):
            key = "nature"
        elif normalized.startswith("観光地点合計の歴史"):
            key = "history_culture"
        elif normalized.startswith("観光地点合計の温泉"):
            key = "onsen_health"
        elif normalized.startswith("観光地点合計のスポーツ"):
            key = "sports_recreation"
        elif normalized.startswith("観光地点合計の都市型"):
            key = "urban_tourism"
        elif normalized.startswith("観光地点合計のその他"):
            key = "other"
        elif normalized == "観光地点合計" or normalized.startswith("観光地点合計["):
            key = "spot_total"
        elif normalized.startswith("年西暦") or normalized == "年西暦":
            key = "year"
        elif normalized.startswith("年和暦") or normalized == "年和暦":
            key = "era_year"
        if key not in col_index:
            col_index[key] = idx

    required = {"year", "total"}
    if not required.issubset(col_index):
        raise TourismParseError(
            f"観光入込客数 CSV のヘッダが想定と異なります: {header[:12]}"
        )

    stats: list[TourismStat] = []
    for row in rows[1:]:
        if len(row) <= max(col_index.values()):
            continue
        year = _to_int(row[col_index["year"]])
        if year is None:
            continue
        stats.append(
            TourismStat(
                year=year,
                era_year=row[col_index["era_year"]].strip() if "era_year" in col_index else "",
                total=_to_int(row[col_index["total"]]),
                event_total=_to_int(row[col_index["event_total"]]) if "event_total" in col_index else None,
                spot_total=_to_int(row[col_index["spot_total"]]) if "spot_total" in col_index else None,
                nature=_to_int(row[col_index["nature"]]) if "nature" in col_index else None,
                history_culture=_to_int(row[col_index["history_culture"]]) if "history_culture" in col_index else None,
                onsen_health=_to_int(row[col_index["onsen_health"]]) if "onsen_health" in col_index else None,
                sports_recreation=_to_int(row[col_index["sports_recreation"]]) if "sports_recreation" in col_index else None,
                urban_tourism=_to_int(row[col_index["urban_tourism"]]) if "urban_tourism" in col_index else None,
                other=_to_int(row[col_index["other"]]) if "other" in col_index else None,
                source=SOURCE_TEXT,
                source_url=source_url,
            )
        )
    if not stats:
        raise TourismParseError("観光入込客数 CSV に有効なデータ行がありません")
    return stats


# ---------------------------------------------------------------------------
# GIS 温泉利用許可施設 CSV のパース
# ---------------------------------------------------------------------------


def _parse_onsen_csv(raw: bytes, source_url: str) -> list[Spot]:
    """新潟市 GIS 温泉利用許可施設 CSV をパースする。

    実データの列構成（ヘッダ 10 カラム）:
      longitude, latitude, SAUID, SAFIELD000(施設名), SAFIELD001(郵便番号),
      SAFIELD002(住所), SAFIELD003(電話番号), SAFIELD004(温泉名),
      SAFIELD005(泉質) ...
    """
    text = _decode_text(raw)
    rows = _parse_csv_rows(text)
    if not rows:
        raise TourismParseError("温泉 CSV が空です")
    header = [h.strip() for h in rows[0]]

    # 列名（SAFIELD000〜）から意味を推定する
    def _find(*names: str) -> int | None:
        for name in names:
            for idx, h in enumerate(header):
                if h == name:
                    return idx
        return None

    idx_lon = _find("longitude", "経度", "X")
    idx_lat = _find("latitude", "緯度", "Y")
    idx_name = _find("SAFIELD000", "名称", "施設名", "名前")
    idx_addr = _find("SAFIELD002", "住所", "所在地", "SAFIELD001")
    idx_phone = _find("SAFIELD003", "電話番号", "電話", "SAFIELD002")
    idx_onsen = _find("SAFIELD004", "温泉名", "源泉名")
    idx_quality = _find("SAFIELD005", "泉質")
    if idx_name is None or idx_lon is None or idx_lat is None:
        raise TourismParseError(
            f"温泉 CSV のヘッダが想定と異なります: {header[:12]}"
        )

    spots: list[Spot] = []
    for row in rows[1:]:
        if len(row) <= max(i for i in (idx_name, idx_lon, idx_lat) if i is not None):
            continue
        name = row[idx_name].strip()
        if not name:
            continue
        lat = _to_float(row[idx_lat])
        lon = _to_float(row[idx_lon])
        if lat is None or lon is None:
            continue
        onsen_name = row[idx_onsen].strip() if idx_onsen is not None and len(row) > idx_onsen else ""
        quality = row[idx_quality].strip() if idx_quality is not None and len(row) > idx_quality else ""
        description_parts = [p for p in (onsen_name, quality) if p]
        spots.append(
            Spot(
                id=f"onsen-{row[2].strip() if len(row) > 2 else len(spots) + 1}",
                name=name,
                category="温泉",
                lat=lat,
                lon=lon,
                address=row[idx_addr].strip() if idx_addr is not None and len(row) > idx_addr else "",
                phone=row[idx_phone].strip() if idx_phone is not None and len(row) > idx_phone else "",
                url="",
                description="（".join(description_parts) + "）" if description_parts else "",
                source=SOURCE_TEXT,
                source_url=source_url,
            )
        )
    if not spots:
        raise TourismParseError("温泉 CSV に有効なデータ行がありません")
    return spots


# ---------------------------------------------------------------------------
# 国土数値情報 P33（Shapefile / DBF）のパース
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DbfField:
    """DBF フィールド定義。"""

    name: str
    type: str  # C=文字, N=数値, L=論理, D=日付
    length: int
    decimal: int


def _parse_dbf_header(raw: bytes) -> tuple[list[_DbfField], int, int, int]:
    """DBF ヘッダをパースして (フィールド定義, レコード数, ヘッダサイズ, レコードサイズ) を返す。"""
    if len(raw) < 32:
        raise TourismParseError("DBF ヘッダが短すぎます")
    try:
        num_records, header_size, record_size = struct.unpack_from("<IHH", raw, 4)
    except struct.error as e:
        raise TourismParseError(f"DBF ヘッダのパースに失敗しました: {e}") from e
    if header_size < 32:
        raise TourismParseError(f"DBF ヘッダサイズが不正です: {header_size}")
    n_fields = (header_size - 32) // 32
    fields: list[_DbfField] = []
    for i in range(n_fields):
        off = 32 + i * 32
        if off + 32 > len(raw):
            break
        name_bytes = raw[off : off + 11].split(b"\x00")[0]
        ftype = chr(raw[off + 11])
        flen = raw[off + 16]
        fdec = raw[off + 17]
        try:
            name = name_bytes.decode("cp932")
        except UnicodeDecodeError:
            name = name_bytes.decode("cp932", errors="replace")
        fields.append(_DbfField(name=name, type=ftype, length=flen, decimal=fdec))
    return fields, num_records, header_size, record_size


def _parse_dbf_record(raw: bytes, fields: list[_DbfField]) -> list[str]:
    """DBF レコード 1 件をフィールド値リストに変換する（文字列のまま）。"""
    values: list[str] = []
    pos = 0
    for f in fields:
        chunk = raw[pos : pos + f.length]
        if f.type == "C":
            try:
                value = chunk.decode("cp932").strip()
            except UnicodeDecodeError:
                value = chunk.decode("cp932", errors="replace").strip()
        else:
            value = chunk.decode("ascii", errors="replace").strip()
        values.append(value)
        pos += f.length
    return values


def _read_shp_points(shp_bytes: bytes) -> list[tuple[float, float] | None]:
    """SHP ファイルから各レコードの座標（Point / PointZ）を読み取る。

    座標系は PRJ により GCS_JGD_2000（緯度経度・度）。レコード順は
    DBF のレコード順と一致する（Shapefile 仕様）。欠損・非 Point は None。
    """
    points: list[tuple[float, float] | None] = []
    pos = 100  # 100 バイトのファイルヘッダをスキップ
    while pos + 8 <= len(shp_bytes):
        rec_num, content_words = struct.unpack_from(">ii", shp_bytes, pos)
        pos += 8
        content_len = content_words * 2
        if pos + content_len > len(shp_bytes):
            break
        content = shp_bytes[pos : pos + content_len]
        pos += content_len
        if len(content) < 4:
            points.append(None)
            continue
        shape_type, = struct.unpack_from("<i", content, 0)
        if shape_type == 1 and len(content) >= 20:  # Point
            x, y = struct.unpack_from("<dd", content, 4)
            points.append((x, y))
        elif shape_type == 11 and len(content) >= 28:  # PointZ
            x, y, _z = struct.unpack_from("<ddd", content, 4)
            points.append((x, y))
        else:
            points.append(None)
    return points


# P33 集客施設の DBF フィールド名 → 意味
# 出典: 国土数値情報 P33 集客施設データ データ仕様書
_P33_FIELD_NAMES: dict[str, str] = {
    "P33_001": "facility_id",  # 連番
    "P33_002": "city_code",  # 市区町村コード
    "P33_003": "pref_code",  # 都道府県コード (15=新潟県)
    "P33_004": "facility_type_code",  # 施設区分コード
    "P33_005": "facility_name",  # 施設名称
    "P33_006": "postal_code",  # 郵便番号
    "P33_007": "address",  # 所在地
    "P33_008": "telephone_number",  # 電話番号
    "P33_009": "opening_date",  # 開設年月日
    "P33_010": "url",  # ホームページ
    "P33_011": "access",  # アクセス
    "P33_012": "number_of_screens",  # スクリーン数
    "P33_013": "total_number_of_seats",  # 座席数
    "P33_014": "community_center_type",  # 公民館種別
    "P33_015": "number_of_business_days",  # 年間営業日数
    "P33_016": "business_hours",  # 営業時間
    "P33_017": "presence_of_admission",  # 有料/無料
    "P33_018": "site_area",  # 敷地面積
    "P33_019": "construction_total_area",  # 建築面積
    "P33_020": "number_of_holes",  # ホール数
    "P33_021": "maximum_seats_hall",  # 最大収容人員
    "P33_022": "total_seats_hall",  # ホール座席数
    "P33_023": "number_of_meeting_room",  # 会議室数
    "P33_024": "number_of_exhibition_room",  # 展示室数
    "P33_041": "postal_code_flag",  # 郵便番号フラグ
}


def parse_p33_zip(raw: bytes, source_url: str = P33_ZIP_URL) -> list[Spot]:
    """国土数値情報 P33 集客施設 ZIP（Shapefile）をスポット一覧に変換する。

    ZIP 内のファイル（実データ確認済み）:
      - P33-14_15.dbf: 属性テーブル（Shift-JIS・25 フィールド・359 レコード）
      - P33-14_15.shp: 点形状（Point, JGD2000 緯度経度）
      - P33-14_15.prj: 座標系定義（GCS_JGD_2000）

    Args:
        raw: ZIP のバイト列。
        source_url: 出典 URL（データセットの source_url に設定）。

    Returns:
        集客施設スポットのリスト（359 件想定）。

    Raises:
        TourismParseError: ZIP 内に DBF/SHP がない、またはパースに失敗した場合。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            dbf_names = [n for n in zf.namelist() if n.lower().endswith(".dbf")]
            shp_names = [n for n in zf.namelist() if n.lower().endswith(".shp")]
            if not dbf_names or not shp_names:
                raise TourismParseError(
                    f"P33 ZIP 内に DBF/SHP が見つかりません: {zf.namelist()}"
                )
            dbf_raw = zf.read(dbf_names[0])
            shp_raw = zf.read(shp_names[0])
    except zipfile.BadZipFile as e:
        raise TourismParseError(f"P33 ZIP の解凍に失敗しました: {e}") from e

    fields, num_records, header_size, record_size = _parse_dbf_header(dbf_raw)
    if not fields or num_records == 0:
        raise TourismParseError("P33 DBF にフィールド定義またはレコードがありません")

    # 必要なフィールドのインデックスを解決
    index = {f.name: i for i, f in enumerate(fields)}
    idx_id = index.get("P33_001")
    idx_type = index.get("P33_004")
    idx_name = index.get("P33_005")
    idx_addr = index.get("P33_007")
    idx_tel = index.get("P33_008")
    idx_url = index.get("P33_010")
    idx_access = index.get("P33_011")
    idx_biz_hours = index.get("P33_016")
    if idx_name is None:
        raise TourismParseError("P33 DBF に施設名称（P33_005）フィールドがありません")

    points = _read_shp_points(shp_raw)
    spots: list[Spot] = []
    data_start = header_size + 1  # ヘッダ末尾の 0x0D をスキップ
    for i in range(num_records):
        offset = data_start + i * record_size
        if offset + record_size > len(dbf_raw):
            break
        record = dbf_raw[offset : offset + record_size]
        values = _parse_dbf_record(record, fields)

        name = values[idx_name]
        if not name:
            continue
        type_code = values[idx_type] if idx_type is not None else ""
        type_name = P33_FACILITY_TYPES.get(type_code, "")
        spot_id = values[idx_id] if idx_id is not None else str(i + 1)
        point = points[i] if i < len(points) else None

        # 補足説明（区分・アクセス・営業時間）
        description_parts = [type_name]
        if idx_access is not None and values[idx_access] and values[idx_access] not in ("‐", "-", "無"):
            description_parts.append(f"アクセス: {values[idx_access]}")
        if idx_biz_hours is not None and values[idx_biz_hours] and values[idx_biz_hours] not in ("‐", "-", "無"):
            description_parts.append(f"営業時間: {values[idx_biz_hours]}")
        url = values[idx_url] if idx_url is not None else ""
        if url in ("‐", "-", "無"):
            url = ""

        spots.append(
            Spot(
                id=f"p33-{spot_id}",
                name=name,
                category=f"集客施設（{type_name}）" if type_name else "集客施設",
                lat=point[1] if point else None,
                lon=point[0] if point else None,
                address=values[idx_addr] if idx_addr is not None else "",
                phone=values[idx_tel] if idx_tel is not None else "",
                url=url,
                description="／".join(p for p in description_parts if p),
                source=P33_SOURCE_TEXT,
                source_url=source_url,
            )
        )
    if not spots:
        raise TourismParseError("P33 DBF に有効なレコードがありません")
    return spots


# ---------------------------------------------------------------------------
# サンプル変換
# ---------------------------------------------------------------------------


def _sample_dataset(d: dict[str, object]) -> TourismDataset:
    return TourismDataset(
        id=str(d["id"]),
        name=str(d["name"]),
        title=str(d["title"]),
        description=str(d["description"]),
        license=str(d["license"]),
        license_url=str(d["license_url"]),
        updated_at=str(d["updated_at"]),
        url=str(d["url"]),
        resources=tuple(str(r) for r in d.get("resources", ())),
        source=SOURCE_TEXT,
        source_url=CKAN_BASE_URL,
    )


def _sample_irikomi(d: dict[str, object]) -> TourismStat:
    return TourismStat(
        year=int(d["year"]),
        era_year=str(d["era_year"]),
        total=int(d["total"]) if d.get("total") is not None else None,
        event_total=int(d["event_total"]) if d.get("event_total") is not None else None,
        spot_total=int(d["spot_total"]) if d.get("spot_total") is not None else None,
        nature=int(d["nature"]) if d.get("nature") is not None else None,
        history_culture=int(d["history_culture"]) if d.get("history_culture") is not None else None,
        onsen_health=int(d["onsen_health"]) if d.get("onsen_health") is not None else None,
        sports_recreation=int(d["sports_recreation"]) if d.get("sports_recreation") is not None else None,
        urban_tourism=int(d["urban_tourism"]) if d.get("urban_tourism") is not None else None,
        other=int(d["other"]) if d.get("other") is not None else None,
        source=SOURCE_TEXT,
        source_url=IRIKOMI_CSV_URL,
    )


def _sample_onsen_spots() -> list[Spot]:
    return [
        Spot(
            id=str(d["id"]),
            name=str(d["name"]),
            category=str(d["category"]),
            lat=float(d["lat"]) if d.get("lat") is not None else None,
            lon=float(d["lon"]) if d.get("lon") is not None else None,
            address=str(d.get("address", "")),
            phone=str(d.get("phone", "")),
            url=str(d.get("url", "")),
            description=str(d.get("description", "")),
            source=SOURCE_TEXT,
            source_url=ONSEN_CSV_URL,
        )
        for d in _SAMPLE_ONSEN_SPOTS
    ]


def _sample_p33_spots() -> list[Spot]:
    return [
        Spot(
            id=str(d["id"]),
            name=str(d["name"]),
            category=str(d["category"]),
            lat=float(d["lat"]) if d.get("lat") is not None else None,
            lon=float(d["lon"]) if d.get("lon") is not None else None,
            address=str(d.get("address", "")),
            phone=str(d.get("phone", "")),
            url=str(d.get("url", "")),
            description=str(d.get("description", "")),
            source=P33_SOURCE_TEXT,
            source_url=P33_ZIP_URL,
        )
        for d in _SAMPLE_P33_SPOTS
    ]


# ---------------------------------------------------------------------------
# モジュール関数（シンプルな利用向け）
# ---------------------------------------------------------------------------


def get_tourism_datasets(
    *, query: str | None = None, ttl: float = DEFAULT_TTL, timeout: float = 30.0,
    fallback_to_sample: bool = True,
) -> list[TourismDataset]:
    """観光関連データセット一覧を 1 コールで取得する（キャッシュ付き）。"""
    with TourismClient(ttl=ttl, timeout=timeout, fallback_to_sample=fallback_to_sample) as client:
        return client.get_datasets(query=query)


def get_tourism_stats(
    *, year: int | None = None, ttl: float = DEFAULT_TTL, timeout: float = 30.0,
    fallback_to_sample: bool = True,
) -> list[TourismStat]:
    """観光入込客数統計を 1 コールで取得する（キャッシュ付き）。"""
    with TourismClient(ttl=ttl, timeout=timeout, fallback_to_sample=fallback_to_sample) as client:
        return client.get_irikomi(year=year)


def get_tourism_spots(
    *,
    category: str | None = None,
    include_onsen: bool = True,
    include_p33: bool = True,
    ttl: float = DEFAULT_TTL,
    timeout: float = 30.0,
    fallback_to_sample: bool = True,
) -> list[Spot]:
    """観光スポット一覧を 1 コールで取得する（温泉 + 集客施設の統合）。"""
    with TourismClient(ttl=ttl, timeout=timeout, fallback_to_sample=fallback_to_sample) as client:
        return client.get_spots(
            category=category,
            include_onsen=include_onsen,
            include_p33=include_p33,
        )
