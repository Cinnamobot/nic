"""新潟県オープンデータカタログ取得モジュール。

新潟県が公開するオープンデータ（統計・観光など）を取得する。

データ源（取得優先順）:
  1. CKAN API: https://ckan.pref.niigata.lg.jp/api/3/action/...
     （タスク想定のカタログ API。2026-08 現在 DNS 解決不可のため
       実環境ではほぼ常にフォールバックへ移行する）
  2. 新潟県公式サイトのオープンデータ一覧 CSV（実在する代替データ源）
     https://www.pref.niigata.lg.jp/site/opendata/
     （「新潟県オープンデータ一覧」Excel/CSV の掲載 URL が動的に変わるため、
       一覧ページから CSV のリンクを探して取得する）
  3. 内蔵サンプルデータ（最終フォールバック。オフラインでも動作）

取得できる内容:
  - データセット一覧（カタログ全体の検索・分野・形式による絞り込み）
  - 統計データ（人口時系列データ: 市町村別・年月別の人口）
  - 観光データ（道の駅一覧: 駅名・路線名・所在地・電話番号）

出典: 新潟県（オープンデータ）、新潟県統計課（人口）、新潟県道路管理課（道の駅）。
全レスポンスに source / source_url を含めて出典を明記する。

利用条件: 新潟県オープンデータ利用規約（https://www.pref.niigata.lg.jp/sec/userguide/od-kiyaku.html）
"""

from __future__ import annotations

import csv
import io
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

SOURCE_TEXT = "出典:新潟県オープンデータ"
"""出典表示テキスト（表示・返却データに必ず含める）。"""

LICENSE_TEXT = "新潟県オープンデータ利用規約"
LICENSE_URL = "https://www.pref.niigata.lg.jp/sec/userguide/od-kiyaku.html"

CKAN_BASE_URL = "https://ckan.pref.niigata.lg.jp"
"""タスク想定の CKAN カタログ API ベース URL（2026-08 現在 DNS 解決不可）。"""

OPEN_DATA_PAGE_URL = "https://www.pref.niigata.lg.jp/site/opendata/"
"""新潟県オープンデータの公式ページ（実在する代替データ源の入り口）。"""

POPULATION_PAGE_URL = "https://www.pref.niigata.lg.jp/site/tokei/1282075307357.html"
"""人口時系列データ(市町村別) の掲載ページ（新潟県統計課）。"""

MICHINO_EKI_PAGE_URL = "https://www.pref.niigata.lg.jp/dourokanri/1202317264067.html"
"""新潟県道の駅の掲載ページ（新潟県道路管理課）。"""

# 一覧ページ内の CSV リンクを探す正規表現（添付ファイルの ID は毎回変わる）
_CATALOG_CSV_PATTERN = re.compile(
    r'href="(?P<url>[^"]*uploaded/attachment/[^"]*\.csv)"', re.IGNORECASE
)

# CKAN の package_search レスポンス成功判定
_CKAN_SUCCESS = "success"

DEFAULT_TTL = 3600.0
"""キャッシュ有効時間（秒）。カタログは毎月〜不定期更新のため 1 時間が妥当。"""

USER_AGENT = "nic/0.1 (+https://github.com/Cinnamobot/nic)"


class OpenDataError(Exception):
    """新潟県オープンデータ取得に関する基底エラー。"""


class OpenDataFetchError(OpenDataError):
    """データ取得（通信・HTTP エラー）に失敗した場合のエラー。"""


class OpenDataParseError(OpenDataError):
    """取得データのパースに失敗した場合のエラー。"""


class OpenDataNotFoundError(OpenDataError):
    """要求されたデータがカタログに見つからない場合のエラー。"""


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dataset:
    """カタログ上の 1 データセット。"""

    id: str  # カタログ番号 (№)
    name: str  # データ名
    category: str  # 分類（内容）
    description: str  # データ概要
    fields: str  # 主な項目
    fiscal_year: str  # 作成年度・時点 (例: "R5")
    update_frequency: str  # 更新頻度 (例: "毎月")
    format: str  # データ形式 (CSV / Excel / PDF ...)
    url: str  # 掲載 URL
    department: str = ""  # 所属名（担当課）
    source: str = SOURCE_TEXT
    source_url: str = OPEN_DATA_PAGE_URL


@dataclass
class PopulationRecord:
    """人口時系列データの 1 レコード。"""

    date: str  # 年月日 (例: "2024/10/1 0:00")
    municipality_code: str  # 市町村コード (例: "15201")
    municipality_name: str  # 市町村名 (例: "新潟市")
    total: int  # 人口総数
    male: int  # 男計
    female: int  # 女計
    source: str = SOURCE_TEXT
    source_url: str = POPULATION_PAGE_URL


@dataclass(frozen=True)
class MichiNoEki:
    """道の駅 1 件。"""

    id: int  # 番号
    name: str  # 駅名
    route: str  # 路線名
    address: str  # 所在地
    phone: str  # 電話番号
    source: str = SOURCE_TEXT
    source_url: str = MICHINO_EKI_PAGE_URL


# ---------------------------------------------------------------------------
# 内蔵サンプルデータ（最終フォールバック用）
# ---------------------------------------------------------------------------
# 出典: 新潟県オープンデータ一覧 / 人口時系列データ / 新潟県道の駅（抜粋・構造化）
# 実データが全て取得できない環境（オフライン等）でも動作させるための
# 最小限の構造化データ。数値は公開データに基づく。

_SAMPLE_DATASETS: list[dict[str, str]] = [
    {
        "id": "234",
        "name": "人口時系列データ(市町村別)",
        "category": "人口・世帯",
        "description": "大正９年からの市町村別人口データを掲載。",
        "fields": "新潟県の人口総数、各歳人口合計、男女別数。",
        "fiscal_year": "R5",
        "update_frequency": "毎月",
        "format": "CSV",
        "url": POPULATION_PAGE_URL,
        "department": "統計課",
    },
    {
        "id": "731",
        "name": "新潟県道の駅",
        "category": "運輸・観光",
        "description": "県内道の駅の名簿",
        "fields": "名称、路線名、所在地、電話番号",
        "fiscal_year": "R4",
        "update_frequency": "不定期",
        "format": "Excel",
        "url": MICHINO_EKI_PAGE_URL,
        "department": "道路管理課",
    },
    {
        "id": "848",
        "name": "観光統計",
        "category": "運輸・観光",
        "description": "県内観光入込客数等の統計",
        "fields": "観光入込客数、宿泊者数 等",
        "fiscal_year": "R4",
        "update_frequency": "毎年",
        "format": "PDF",
        "url": "https://www.pref.niigata.lg.jp/sec/kankokikaku/1245960085415.html",
        "department": "観光企画課",
    },
    {
        "id": "927",
        "name": "新潟県の税金などを納付することができる金融機関の窓口",
        "category": "行財政",
        "description": "県税等を納付できる金融機関の窓口一覧",
        "fields": "金融機関名、窓口 等",
        "fiscal_year": "R4",
        "update_frequency": "随時",
        "format": "CSV",
        "url": "https://www.pref.niigata.lg.jp/sec/suitoukanri/1356773325235.html",
        "department": "税務課",
    },
    {
        "id": "687",
        "name": "水揚情報",
        "category": "農林水産業",
        "description": "県内主要港の水揚量・金額",
        "fields": "魚種、水揚量、金額 等",
        "fiscal_year": "R4",
        "update_frequency": "随時",
        "format": "CSV",
        "url": "https://www.pref.niigata.lg.jp/site/suisan-kenkyu/mizuage.html",
        "department": "水産課",
    },
]

_SAMPLE_POPULATION: list[dict[str, object]] = [
    {
        "date": "2024/10/1 0:00",
        "municipality_code": "15201",
        "municipality_name": "新潟市",
        "total": 772425,
        "male": 372208,
        "female": 400217,
    },
    {
        "date": "2024/10/1 0:00",
        "municipality_code": "15202",
        "municipality_name": "長岡市",
        "total": 258131,
        "male": 124938,
        "female": 133193,
    },
    {
        "date": "2024/10/1 0:00",
        "municipality_code": "15204",
        "municipality_name": "三条市",
        "total": 93335,
        "male": 44951,
        "female": 48384,
    },
    {
        "date": "2024/10/1 0:00",
        "municipality_code": "15222",
        "municipality_name": "上越市",
        "total": 180014,
        "male": 85837,
        "female": 94177,
    },
    {
        "date": "2024/10/1 0:00",
        "municipality_code": "15225",
        "municipality_name": "魚沼市",
        "total": 32483,
        "male": 15776,
        "female": 16707,
    },
]

_SAMPLE_MICHINO_EKI: list[dict[str, object]] = [
    {
        "id": 1,
        "name": "豊栄",
        "route": "一般国道7号",
        "address": "新潟市北区木崎字切尾山3644-乙",
        "phone": "025-388-2700",
    },
    {
        "id": 2,
        "name": "加治川（さくらの里）",
        "route": "一般国道7号",
        "address": "新発田市横岡1147",
        "phone": "0254-33-3175",
    },
    {
        "id": 3,
        "name": "神林",
        "route": "一般国道7号",
        "address": "村上市牧目584",
        "phone": "0254-66-6326",
    },
    {
        "id": 4,
        "name": "朝日（まほろば）",
        "route": "一般国道7号",
        "address": "村上市猿沢1212",
        "phone": "0254-72-0300",
    },
    {
        "id": 5,
        "name": "新潟ふるさと村",
        "route": "一般国道8号",
        "address": "新潟市西区山田2307",
        "phone": "025-230-3030",
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


class OpenDataClient:
    """新潟県オープンデータカタログ取得クライアント。

    CKAN API → 公式サイトの一覧 CSV → 内蔵サンプル の順にフォールバックし、
    TTL 付きインメモリキャッシュで取得結果を保持する。

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
        timeout: float = 15.0,
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
        """直近のフォールバック状況（データ源の失敗理由など）の記録。"""

    def close(self) -> None:
        """内部生成した httpx.Client を閉じる。"""
        if self._owns_client and self._client is not None:
            self._client.close()

    def __enter__(self) -> "OpenDataClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def warnings(self) -> list[str]:
        """直近の取得で発生したフォールバック状況の説明一覧。"""
        return list(self._warnings)

    # -- 公開 API -----------------------------------------------------------

    def get_datasets(
        self,
        *,
        query: str | None = None,
        category: str | None = None,
        data_format: str | None = None,
        force: bool = False,
    ) -> list[Dataset]:
        """データセット一覧を取得・絞り込みする。

        Args:
            query: データ名・概要に対する部分一致検索語（None で全件）。
            category: 分類（内容）による絞り込み（例: "運輸・観光"）。
            data_format: データ形式による絞り込み（例: "CSV", "Excel"）。
            force: True ならキャッシュを無視して再取得。

        Returns:
            条件に合致する Dataset のリスト（出典・URL 付き）。

        Raises:
            OpenDataFetchError: 全データ源の取得に失敗し、フォールバックも無効な場合。
            OpenDataParseError: 取得データのパースに失敗した場合。
        """
        datasets = self._get_cached("datasets", force)
        if datasets is None:
            datasets = self._fetch_datasets()
            self._put_cache("datasets", datasets)
        return _filter_datasets(datasets, query=query, category=category, data_format=data_format)

    def search_datasets(self, query: str, *, limit: int = 20, force: bool = False) -> list[Dataset]:
        """データセットをキーワード検索する。"""
        return self.get_datasets(query=query, force=force)[:limit]

    def get_population(
        self,
        *,
        municipality: str | None = None,
        force: bool = False,
    ) -> list[PopulationRecord]:
        """統計データ（人口時系列データ）を取得する。

        Args:
            municipality: 市町村名で絞り込み（例: "新潟市"）。None で全件。
            force: True ならキャッシュを無視して再取得。

        Returns:
            人口レコードのリスト。実データが取得できない場合は
            内蔵サンプルデータを返す（source に注記）。
        """
        records = self._get_cached("population", force)
        if records is None:
            records = self._fetch_population()
            self._put_cache("population", records)
        if municipality:
            records = [r for r in records if municipality in r.municipality_name]
        return records

    def get_tourism(
        self,
        *,
        force: bool = False,
    ) -> list[MichiNoEki]:
        """観光データ（道の駅一覧）を取得する。

        Args:
            force: True ならキャッシュを無視して再取得。

        Returns:
            道の駅一覧（42 件想定）。取得できない場合は内蔵サンプルを返す。
        """
        stations = self._get_cached("michinoeki", force)
        if stations is None:
            stations = self._fetch_michinoeki()
            self._put_cache("michinoeki", stations)
        return stations

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

    def _download(self, url: str) -> bytes:
        """URL からバイト列を取得する（ヘッダ付き・リダイレクト追従）。"""
        client = self._client
        if client is None:
            client = httpx.Client(timeout=self.timeout, follow_redirects=True)
            self._client = client
            self._owns_client = True
        try:
            resp = client.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/csv,text/html,application/json,*/*",
                },
            )
        except httpx.HTTPError as e:
            raise OpenDataFetchError(f"HTTP 取得に失敗しました: {url} ({e})") from e
        if resp.status_code != 200:
            raise OpenDataFetchError(
                f"HTTP {resp.status_code} で取得できませんでした: {url}"
            )
        return resp.content

    # -- データ源 1: CKAN API ------------------------------------------------

    def _fetch_from_ckan(self) -> list[Dataset] | None:
        """CKAN package_search でデータセット一覧を取得する。失敗時 None。"""
        url = f"{CKAN_BASE_URL}/api/3/action/package_search?rows=1000"
        try:
            raw = self._download(url)
        except OpenDataFetchError as e:
            self._warn(f"CKAN API を利用できません（{e}）。公式サイトの一覧へフォールバックします。")
            return None
        try:
            payload = _parse_json(raw)
        except OpenDataParseError as e:
            self._warn(f"CKAN API の応答を解釈できません（{e}）。公式サイトの一覧へフォールバックします。")
            return None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            self._warn("CKAN API がエラー応答を返しました。公式サイトの一覧へフォールバックします。")
            return None
        result = payload.get("result")
        if not isinstance(result, dict):
            return None
        packages = result.get("results") or []
        datasets: list[Dataset] = []
        for pkg in packages:
            if not isinstance(pkg, dict):
                continue
            extras = {
                str(e.get("key", "")): str(e.get("value", ""))
                for e in pkg.get("extras", [])
                if isinstance(e, dict)
            }
            resources = pkg.get("resources") or []
            fmt = ""
            if resources:
                fmt = str(resources[0].get("format", "")).strip() if isinstance(resources[0], dict) else ""
            datasets.append(
                Dataset(
                    id=str(pkg.get("id", "")),
                    name=str(pkg.get("title", pkg.get("name", ""))),
                    category=extras.get("分野", extras.get("category", "")),
                    description=str(pkg.get("notes", "")),
                    fields=extras.get("主な項目", ""),
                    fiscal_year=extras.get("作成年度・時点", extras.get("fiscal_year", "")),
                    update_frequency=extras.get("更新頻度", extras.get("update_frequency", "")),
                    format=fmt or extras.get("データ形式", ""),
                    url=str(pkg.get("url", "")),
                    department=extras.get("所属名", ""),
                    source=SOURCE_TEXT,
                    source_url=CKAN_BASE_URL,
                )
            )
        return datasets

    # -- データ源 2: 公式サイトのオープンデータ一覧 CSV ------------------------

    def _fetch_catalog_csv_url(self) -> str | None:
        """オープンデータ一覧ページから CSV の URL を探す。"""
        try:
            html = self._download(OPEN_DATA_PAGE_URL).decode("utf-8", errors="replace")
        except OpenDataFetchError as e:
            self._warn(f"オープンデータ一覧ページを取得できません（{e}）。")
            return None
        m = _CATALOG_CSV_PATTERN.search(html)
        if not m:
            self._warn("オープンデータ一覧ページに CSV リンクが見つかりませんでした。")
            return None
        return urljoin(OPEN_DATA_PAGE_URL, m.group("url"))

    def _parse_catalog_csv(self, raw: bytes) -> list[Dataset]:
        """一覧 CSV（cp932/UTF-8）を Dataset のリストに変換する。"""
        text = _decode_text(raw)
        rows = _parse_csv_rows(text)
        if not rows:
            raise OpenDataParseError("一覧 CSV が空です")
        header = [h.strip() for h in rows[0]]
        # ヘッダに「データ名」があればデータ行、なければヘッダなしとみなす
        data_rows = rows[1:] if "データ名" in header else rows
        datasets: list[Dataset] = []
        for row in data_rows:
            if len(row) < 9:
                continue
            num = row[0].strip()
            if not num.isdigit():
                continue
            url = row[9].strip()
            if not url.startswith("http"):
                url = urljoin(OPEN_DATA_PAGE_URL, url) if url.startswith("/") else ""
            datasets.append(
                Dataset(
                    id=num,
                    name=row[2].strip(),
                    category=_normalize_category(row[3]),
                    description=row[4].strip(),
                    fields=row[5].strip(),
                    fiscal_year=_normalize_fiscal_year(row[6]),
                    update_frequency=_normalize_frequency(row[7]),
                    format=_normalize_format(row[8]),
                    url=url,
                    department=row[1].strip() if len(row) > 1 else "",
                    source=SOURCE_TEXT,
                    source_url=OPEN_DATA_PAGE_URL,
                )
            )
        return datasets

    def _fetch_datasets(self) -> list[Dataset]:
        """データセット一覧を CKAN → 公式一覧 CSV → サンプル の順で取得する。"""
        # 1. CKAN API
        ckan = self._fetch_from_ckan()
        if ckan:
            return ckan
        # 2. 公式サイトの一覧 CSV
        csv_url = self._fetch_catalog_csv_url()
        if csv_url:
            try:
                raw = self._download(csv_url)
                datasets = self._parse_catalog_csv(raw)
                if datasets:
                    self._warn(
                        f"CKAN API が利用できないため、公式サイトの一覧 CSV を使用しました: {csv_url}"
                    )
                    return datasets
            except (OpenDataFetchError, OpenDataParseError) as e:
                self._warn(f"一覧 CSV の取得に失敗しました（{e}）。")
        # 3. 内蔵サンプル
        if self.fallback_to_sample:
            self._warn(
                "外部データ源を利用できなかったため、内蔵サンプルデータ（5 件）を返します。"
            )
            return [_sample_dataset(d) for d in _SAMPLE_DATASETS]
        raise OpenDataFetchError(
            "新潟県オープンデータカタログ（CKAN API・公式サイト一覧）からデータを取得できませんでした"
        )

    def _fetch_population(self) -> list[PopulationRecord]:
        """人口時系列データを取得する。実データが無ければサンプルを返す。"""
        csv_urls: list[str] = []
        try:
            html = self._download(POPULATION_PAGE_URL).decode("utf-8", errors="replace")
            csv_urls = list(
                dict.fromkeys(
                    urljoin(POPULATION_PAGE_URL, m.group("url"))
                    for m in re.finditer(
                        r'href="(?P<url>[^"]*uploaded/attachment/[^"]*\.csv)"', html, re.IGNORECASE
                    )
                )
            )
        except OpenDataFetchError as e:
            self._warn(f"人口時系列データのページを取得できません（{e}）。")
        all_records: list[PopulationRecord] = []
        seen_global: set[tuple[str, str]] = set()
        for url in csv_urls:
            try:
                raw = self._download(url)
                records = _parse_population_csv(raw, source_url=url)
                if records:
                    # ファイルをまたぐ重複（同じコード・同じ年月日）を除外する
                    unique: list[PopulationRecord] = []
                    for rec in records:
                        key = (rec.municipality_code, rec.date.strip())
                        if key in seen_global:
                            continue
                        seen_global.add(key)
                        unique.append(rec)
                    self._warn(f"人口時系列データを取得しました: {url}（{len(unique)} 行）")
                    all_records.extend(unique)
            except (OpenDataFetchError, OpenDataParseError) as e:
                self._warn(f"人口 CSV の取得に失敗しました（{url}: {e}）。")
        if all_records:
            # 複数ファイルに分かれているため、年月日の降順（新しい順）に並べ替える
            return _sort_population_newest_first(all_records)
        if self.fallback_to_sample:
            self._warn("人口時系列データを外部取得できなかったため、内蔵サンプルを返します。")
            return [_sample_population(d) for d in _SAMPLE_POPULATION]
        raise OpenDataFetchError("人口時系列データを取得できませんでした")

    def _fetch_michinoeki(self) -> list[MichiNoEki]:
        """道の駅一覧を公式ページの HTML テーブルから取得する。"""
        try:
            html = self._download(MICHINO_EKI_PAGE_URL).decode("utf-8", errors="replace")
            stations = _parse_michinoeki_html(html, source_url=MICHINO_EKI_PAGE_URL)
            if stations:
                self._warn(f"道の駅一覧を取得しました: {MICHINO_EKI_PAGE_URL}")
                return stations
        except OpenDataFetchError as e:
            self._warn(f"道の駅のページを取得できません（{e}）。")
        except OpenDataParseError as e:
            self._warn(f"道の駅のページを解釈できません（{e}）。")
        if self.fallback_to_sample:
            self._warn("道の駅一覧を外部取得できなかったため、内蔵サンプルを返します。")
            return [_sample_michinoeki(d) for d in _SAMPLE_MICHINO_EKI]
        raise OpenDataFetchError("道の駅一覧を取得できませんでした")


# ---------------------------------------------------------------------------
# パース補助関数
# ---------------------------------------------------------------------------


def _decode_text(raw: bytes) -> str:
    """CP932 / UTF-8 を自動判別してテキストにデコードする。"""
    for enc in ("cp932", "utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise OpenDataParseError("文字コードを判別できませんでした（CP932 / UTF-8 以外）")


def _parse_csv_rows(text: str) -> list[list[str]]:
    """CSV テキストを行リストにパースする。"""
    try:
        return [row for row in csv.reader(io.StringIO(text)) if row and any(c.strip() for c in row)]
    except csv.Error as e:
        raise OpenDataParseError(f"CSV のパースに失敗しました: {e}") from e


def _parse_json(raw: bytes) -> Any:
    """JSON バイト列をパースする。"""
    import json

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise OpenDataParseError(f"JSON のパースに失敗しました: {e}") from e


def _normalize_category(raw: str) -> str:
    """分類（内容）の「・0 」「1 」等の接頭辞を取り除く。"""
    s = raw.strip()
    m = re.match(r"^(?:・\s*)?\d*\s*(.*)$", s)
    return m.group(1) if m else s


def _normalize_format(raw: str) -> str:
    """データ形式の「4 PDF」等を「PDF」に正規化する。"""
    s = raw.strip()
    m = re.match(r"^\d+\s*(.*)$", s)
    return m.group(1) if m else s


def _normalize_fiscal_year(raw: str) -> str:
    """作成年度・時点の先頭の元号を返す（例: "H20", "R5"）。"""
    s = raw.strip()
    m = re.match(r"^(H\d{1,2}|R\d{1,2}|S\d{1,2})", s, re.IGNORECASE)
    return m.group(1).upper() if m else s


def _normalize_frequency(raw: str) -> str:
    """更新頻度を「毎月 / 毎年 / 随時 / 不定期 / 更新なし / その他」に正規化する。"""
    s = raw.strip()
    if any(k in s for k in ("毎月", "月１回", "月1回", "１分")):
        return "毎月"
    if "毎週" in s:
        return "毎週"
    if any(k in s for k in ("毎年", "年１回", "年1回", "年２回", "年2回")):
        return "毎年"
    if any(k in s for k in ("随時", "都度", "届出時")):
        return "随時"
    if any(k in s for k in ("なし", "しない", "廃止")):
        return "更新なし"
    if "不定期" in s:
        return "不定期"
    if "四半期" in s:
        return "四半期"
    return "その他" if s else "不明"


def _parse_population_csv(raw: bytes, source_url: str) -> list[PopulationRecord]:
    """人口時系列データ CSV をパースする。

    2 種類のレイアウトに対応:
      A. 広形式（市町村CD/郡CD/市町村名/人口総数/男計/女計 + 各歳別）
      B. コンパクト形式（団体コード/都道府県名・市区町村名/総数/男/女）

    「県計」「新潟県」の集計行は含めず、市町村行のみを採用する。
    市町村行は 5 桁コード（広形式）または 6 桁コード（コンパクト形式）で判定し、
    重複（同コード・同年月日）は最初の 1 件のみ採用する。
    """
    text = _decode_text(raw)
    rows = _parse_csv_rows(text)
    if not rows:
        raise OpenDataParseError("人口 CSV が空です")
    header = [h.strip() for h in rows[0]]

    # レイアウト A（広形式）
    layout_a = None
    try:
        layout_a = {
            "date": header.index("年月日"),
            "code": header.index("市町村CD"),
            "name": header.index("市町村名"),
            "total": header.index("人口総数"),
            "male": header.index("男計"),
            "female": header.index("女計"),
        }
    except ValueError:
        layout_a = None
    # レイアウト B（コンパクト形式）
    layout_b = None
    try:
        layout_b = {
            "date": header.index("年月日"),
            "code": header.index("団体コード"),
            "name": header.index("都道府県名・市区町村名"),
            "total": header.index("総数"),
            "male": header.index("男"),
            "female": header.index("女"),
        }
    except ValueError:
        layout_b = None
    if layout_a is None and layout_b is None:
        raise OpenDataParseError(
            f"人口 CSV のヘッダが想定と異なります: {header[:8]}"
        )

    def _code_len(layout: dict[str, int]) -> int:
        return 6 if layout is layout_b else 5

    records: list[PopulationRecord] = []
    seen: set[tuple[str, str]] = set()
    for row in rows[1:]:
        # どちらのレイアウトでも解釈できるよう、列数の合う方を優先
        for layout in (layout_a, layout_b):
            if layout is None or len(row) <= max(layout.values()):
                continue
            code = row[layout["code"]].strip()
            expected = _code_len(layout)
            if len(code) != expected or not code.isdigit():
                continue  # 集計行（県計など）を除外
            name = row[layout["name"]].strip()
            if not name or ("県" in name and len(name) <= 3):
                continue  # 「新潟県」等の集計行を除外
            key = (code, row[layout["date"]].strip())
            if key in seen:
                continue
            try:
                total = int(float(row[layout["total"]]))
                male = int(float(row[layout["male"]]))
                female = int(float(row[layout["female"]]))
            except ValueError:
                continue
            seen.add(key)
            records.append(
                PopulationRecord(
                    date=row[layout["date"]].strip(),
                    municipality_code=code,
                    municipality_name=name,
                    total=total,
                    male=male,
                    female=female,
                    source=SOURCE_TEXT,
                    source_url=source_url,
                )
            )
            break
    return records


_MICHINOEKI_ROW_RE = re.compile(
    r"<tr>\s*<td[^>]*>(\d+)</td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>"
    r"\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>",
    re.IGNORECASE | re.DOTALL,
)


def _parse_michinoeki_html(html: str, source_url: str) -> list[MichiNoEki]:
    """道の駅ページの HTML テーブル（番号/駅名/路線名/所在地/電話番号）をパースする。"""
    stations: list[MichiNoEki] = []
    for m in _MICHINOEKI_ROW_RE.finditer(html):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in m.groups()]
        if not cells[1]:
            continue
        try:
            sid = int(cells[0])
        except ValueError:
            continue
        stations.append(
            MichiNoEki(
                id=sid,
                name=cells[1],
                route=cells[2],
                address=cells[3],
                phone=cells[4],
                source=SOURCE_TEXT,
                source_url=source_url,
            )
        )
    if not stations:
        raise OpenDataParseError("道の駅のテーブルが見つかりませんでした")
    return stations


# ---------------------------------------------------------------------------
# フィルタ・サンプル変換
# ---------------------------------------------------------------------------


def _filter_datasets(
    datasets: list[Dataset],
    *,
    query: str | None = None,
    category: str | None = None,
    data_format: str | None = None,
) -> list[Dataset]:
    """データセット一覧を条件で絞り込む。"""
    result = datasets
    if query:
        q = query.strip()
        result = [
            d
            for d in result
            if q in d.name or q in d.description or q in d.category or q in d.fields
        ]
    if category:
        result = [d for d in result if category.strip() in d.category]
    if data_format:
        result = [d for d in result if d.format.lower() == data_format.strip().lower()]
    return result


def _sort_population_newest_first(records: list[PopulationRecord]) -> list[PopulationRecord]:
    """人口レコードを年月日（新しい順）に並べ替える。"""
    def _key(r: PopulationRecord) -> tuple[str, str]:
        date = r.date.strip()
        # "2024/10/1 0:00" 形式を YYYY-MM-DD に整えてソートキーにする
        m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", date)
        if m:
            return (f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}", r.municipality_code)
        return (date, r.municipality_code)

    return sorted(records, key=_key, reverse=True)


def _sample_dataset(d: dict[str, str]) -> Dataset:
    return Dataset(**d)


def _sample_population(d: dict[str, object]) -> PopulationRecord:
    return PopulationRecord(
        date=str(d["date"]),
        municipality_code=str(d["municipality_code"]),
        municipality_name=str(d["municipality_name"]),
        total=int(d["total"]),
        male=int(d["male"]),
        female=int(d["female"]),
        source=SOURCE_TEXT,
        source_url=POPULATION_PAGE_URL,
    )


def _sample_michinoeki(d: dict[str, object]) -> MichiNoEki:
    return MichiNoEki(
        id=int(d["id"]),
        name=str(d["name"]),
        route=str(d["route"]),
        address=str(d["address"]),
        phone=str(d["phone"]),
        source=SOURCE_TEXT,
        source_url=MICHINO_EKI_PAGE_URL,
    )


# ---------------------------------------------------------------------------
# モジュール関数（シンプルな利用向け）
# ---------------------------------------------------------------------------


def get_datasets(
    *,
    query: str | None = None,
    category: str | None = None,
    data_format: str | None = None,
    ttl: float = DEFAULT_TTL,
    timeout: float = 15.0,
    fallback_to_sample: bool = True,
) -> list[Dataset]:
    """データセット一覧を 1 コールで取得する（キャッシュ付き）。"""
    with OpenDataClient(ttl=ttl, timeout=timeout, fallback_to_sample=fallback_to_sample) as client:
        return client.get_datasets(
            query=query, category=category, data_format=data_format
        )


def get_population(
    *,
    municipality: str | None = None,
    ttl: float = DEFAULT_TTL,
    timeout: float = 15.0,
    fallback_to_sample: bool = True,
) -> list[PopulationRecord]:
    """統計データ（人口）を 1 コールで取得する。"""
    with OpenDataClient(ttl=ttl, timeout=timeout, fallback_to_sample=fallback_to_sample) as client:
        return client.get_population(municipality=municipality)


def get_tourism(
    *,
    ttl: float = DEFAULT_TTL,
    timeout: float = 15.0,
    fallback_to_sample: bool = True,
) -> list[MichiNoEki]:
    """観光データ（道の駅）を 1 コールで取得する。"""
    with OpenDataClient(ttl=ttl, timeout=timeout, fallback_to_sample=fallback_to_sample) as client:
        return client.get_tourism()
