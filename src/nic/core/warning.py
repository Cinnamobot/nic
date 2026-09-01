"""気象庁防災情報XML（新潟県の警報・注意報）取得モジュール。

気象庁防災情報XML配信サービス（PULL型 Atom フィード）から、
新潟県（府県コード 150000）の気象特別警報・警報・注意報電文（VPWW53）を
取得・パースし、**府県 / 一次細分区域 / 市町村等をまとめた地域 / 市町村** の
4 階層それぞれについて、警報・注意報の種別・状態（発表/継続/解除）・
対象地域を返す。

データ源（2 段構成）:
  1. 高頻度フィード（毎分更新・直近10分以上の入電）
     https://www.data.jma.go.jp/developer/xml/feed/extra.xml
  2. 電文 XML（フィード内の <entry> から URL を取得してダウンロード）
     https://www.data.jma.go.jp/developer/xml/data/YYYYMMDDhhmmss_連番_VPWW53_150000.xml

利用条件: 気象庁防災情報XML配信（https://www.data.jma.go.jp/developer/xml/feed/）
公共データ利用規約 第1.0版（https://www.jma.go.jp/jma/kishou/info/coment.html）
出典表示必須。加工したことを明示すること。1日10GB以上のダウンロードはアクセス遮断。
"""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import httpx

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

SOURCE_TEXT = "出典:気象庁"
"""出典表示テキスト（表示・返却データに必ず含める）。"""

SOURCE_URL = "https://www.data.jma.go.jp/developer/xml/feed/extra.xml"
"""防災情報 XML 配信（高頻度・随時フィード）の URL。"""

NIIGATA_PREF_CODE = "150000"
"""新潟県の府県コード。"""

EXTRA_FEED_URL = "https://www.data.jma.go.jp/developer/xml/feed/extra.xml"
"""警報・注意報を含む随時発表フィード（毎分更新・直近10分以上）。"""

# VPWW53 / VPWW54 はどちらも「気象警報・注意報」の電文
_MESSAGE_KINDS = ("VPWW53", "VPWW54")
_TARGET_SUFFIX = f"_{NIIGATA_PREF_CODE}.xml"

# JMAXML ネームスペース
_NS = {
    "j": "http://xml.kishou.go.jp/jmaxml1/",
    "a": "http://www.w3.org/2005/Atom",
    "h": "http://xml.kishou.go.jp/jmaxml1/informationBasis1/",
    "m": "http://xml.kishou.go.jp/jmaxml1/body/meteorology1/",
}

DEFAULT_TTL = 60.0
"""キャッシュ有効時間（秒）。フィードは毎分更新されるため 60 秒が妥当。"""

USER_AGENT = "nic/0.1 (+https://github.com/Cinnamobot/nic)"

# 状態（<Status>）の正規化
_STATUS_NONE = "発表警報・注意報はなし"
_STATUS_MAP: dict[str, str] = {
    "発表": "発表",
    "継続": "継続",
    "解除": "解除",
    "特別警報から警報に切り替え": "特別警報から警報に切り替え",
    "警報から注意報に切り替え": "警報から注意報に切り替え",
    "注意報解除": "解除",
}

# Warning type 属性 → 階層
_LEVEL_LABELS: dict[str, str] = {
    "気象警報・注意報（府県予報区等）": "府県",
    "気象警報・注意報（一次細分区域等）": "一次細分",
    "気象警報・注意報（市町村等をまとめた地域等）": "地域",
    "気象警報・注意報（市町村等）": "市町村",
}


class WarningError(Exception):
    """警報・注意報データ取得に関する基底エラー。"""


class WarningFetchError(WarningError):
    """フィード・電文 XML の取得（通信・HTTP エラー）に失敗した場合のエラー。"""


class WarningParseError(WarningError):
    """フィード・電文 XML のパースに失敗した場合のエラー。"""


class WarningNotFoundError(WarningError):
    """新潟県の電文がフィードに見つからない場合のエラー。"""


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WarningKind:
    """警報・注意報の 1 種別。"""

    name: str  # 種別名 (例: "大雨注意報")
    code: str  # 気象コード (例: "10")
    status: str  # 状態（"発表" / "継続" / "解除" / "発表警報・注意報はなし"）


@dataclass(frozen=True)
class WarningArea:
    """警報・注意報の対象地域。"""

    name: str  # 地域名 (例: "中越" / "十日町市")
    code: str  # エリアコード (例: "150020" / "1521000")
    kinds: tuple[WarningKind, ...] = field(default_factory=tuple)
    """この地域で発表されている種別のリスト（なしなら空タプル）。"""

    @property
    def has_warning(self) -> bool:
        """1 件以上発表（継続・解除含む）されているか。"""
        return any(k.status != _STATUS_NONE for k in self.kinds)

    @property
    def status_summary(self) -> str:
        """この地域の状態サマリ（例: "大雨注意報 継続" / "発表警報・注意報はなし"）。"""
        active = [k for k in self.kinds if k.status != _STATUS_NONE]
        if not active:
            return _STATUS_NONE
        return "、".join(f"{k.name} {k.status}" for k in active)


@dataclass(frozen=True)
class WarningLevel:
    """府県/一次細分/地域/市町村の 1 階層。"""

    level: str  # "府県" / "一次細分" / "地域" / "市町村"
    type_label: str  # Warning type 属性の原文（"気象警報・注意報（府県予報区等）" など）
    areas: tuple[WarningArea, ...] = field(default_factory=tuple)

    @property
    def active_areas(self) -> tuple[WarningArea, ...]:
        """警報・注意報が発表されている（状態が「なし」でない）地域のみ返す。"""
        return tuple(a for a in self.areas if a.has_warning)


@dataclass(frozen=True)
class WarningData:
    """新潟県の警報・注意報 1 電文分の取得結果。"""

    title: str  # 見出し (例: "新潟県気象警報・注意報")
    headline: str  # サマリ文（<Headline>/<Text>）
    info_type: str  # 情報種別（"発表" / "継続" / "解除" / "訂正" など）
    report_datetime: datetime  # 発表日時（JST タイムゾーン付き）
    editorial_office: str  # 発信官署（例: "新潟地方気象台"）
    message_kind: str  # 電文種別（"VPWW53" など）
    message_url: str  # 電文 XML の URL
    levels: tuple[WarningLevel, ...] = field(default_factory=tuple)
    """府県 → 一次細分 → 地域 → 市町村 の順の 4 階層。"""

    fetched_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    source: str = SOURCE_TEXT
    source_url: str = SOURCE_URL

    # -- 階層別アクセサ -----------------------------------------------------

    def level(self, level_name: str) -> WarningLevel | None:
        """階層名（"府県"/"一次細分"/"地域"/"市町村"）で 1 階層を取得する。"""
        for lv in self.levels:
            if lv.level == level_name:
                return lv
        return None

    def get_areas(self, level_name: str) -> tuple[WarningArea, ...]:
        """指定階層の全エリアを返す（存在しなければ空タプル）。"""
        lv = self.level(level_name)
        return lv.areas if lv is not None else ()

    def get_active_areas(self, level_name: str) -> tuple[WarningArea, ...]:
        """指定階層で警報・注意報が発表されているエリアのみ返す。"""
        lv = self.level(level_name)
        return lv.active_areas if lv is not None else ()

    # -- 全体サマリ ---------------------------------------------------------

    @property
    def active_kinds(self) -> tuple[WarningKind, ...]:
        """府県階層で発表されている種別の一覧（重複なし・出現順）。"""
        pref = self.level("府県")
        if pref is None:
            return ()
        seen: set[tuple[str, str]] = set()
        result: list[WarningKind] = []
        for area in pref.areas:
            for k in area.kinds:
                if k.status == _STATUS_NONE:
                    continue
                key = (k.code, k.status)
                if key in seen:
                    continue
                seen.add(key)
                result.append(k)
        return tuple(result)

    @property
    def summary(self) -> str:
        """人間向けサマリ（例: "大雨注意報 継続、雷注意報 継続"）。"""
        kinds = self.active_kinds
        if not kinds:
            return _STATUS_NONE
        return "、".join(f"{k.name} {k.status}" for k in kinds)


# ---------------------------------------------------------------------------
# キャッシュ付きクライアント
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    """キャッシュエントリ。"""

    data: Any
    expires_at: float


class WarningClient:
    """気象庁防災情報XML（新潟県の警報・注意報）取得クライアント。

    高頻度フィード（extra.xml）から新潟県の VPWW53/VPWW54 電文を探し、
    最新の電文 XML を取得・パースして 4 階層の警報・注意報を返す。
    TTL 付きインメモリキャッシュでフィード取得を抑制する。

    Attributes:
        ttl: キャッシュ有効時間（秒）。デフォルト 60 秒（フィードは毎分更新）。
        timeout: HTTP リクエストのタイムアウト（秒）。
    """

    def __init__(
        self,
        *,
        ttl: float = DEFAULT_TTL,
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

    def __enter__(self) -> "WarningClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- 公開 API -----------------------------------------------------------

    def fetch(
        self,
        *,
        force: bool = False,
        feed_url: str = EXTRA_FEED_URL,
    ) -> WarningData:
        """新潟県の最新の警報・注意報を取得する。

        Args:
            force: True ならキャッシュを無視して再取得。
            feed_url: 使用するフィード URL（テスト・差し替え用）。

        Returns:
            WarningData（4 階層の種別・状態・対象地域、発表日時、出典付き）。

        Raises:
            WarningNotFoundError: フィードに新潟県の警報・注意報電文がない場合。
            WarningFetchError: HTTP 取得に失敗した場合。
            WarningParseError: XML のパースに失敗した場合。
        """
        data = self._get_cached(force)
        if data is None:
            data = self._fetch_and_parse(feed_url)
            self._put_cache(data)
        return data

    def fetch_prefecture(self, *, force: bool = False) -> WarningData:
        """府県階層の警報・注意報を取得する（fetch と同じ）。"""
        return self.fetch(force=force)

    def list_levels(self, *, force: bool = False) -> tuple[WarningLevel, ...]:
        """4 階層すべての警報・注意報を取得して返す。"""
        return self.fetch(force=force).levels

    # -- 内部実装 -----------------------------------------------------------

    def _get_cached(self, force: bool) -> WarningData | None:
        if force:
            return None
        with self._lock:
            entry = self._cache.get("warning")
            if entry is not None and entry.expires_at > time.monotonic():
                return entry.data
        return None

    def _put_cache(self, data: WarningData) -> None:
        with self._lock:
            self._cache["warning"] = _CacheEntry(
                data=data, expires_at=time.monotonic() + self.ttl
            )

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
                    "Accept": "application/xml,text/xml,*/*",
                },
            )
        except httpx.HTTPError as e:
            raise WarningFetchError(f"HTTP 取得に失敗しました: {url} ({e})") from e
        if resp.status_code != 200:
            raise WarningFetchError(
                f"HTTP {resp.status_code} で取得できませんでした: {url}"
            )
        return resp.content

    def _fetch_and_parse(self, feed_url: str) -> WarningData:
        """フィード → 電文 XML の 2 段階で取得・パースする。"""
        feed_raw = self._download(feed_url)
        message_url = find_niigata_message_url(feed_raw, base_url=feed_url)
        if message_url is None:
            raise WarningNotFoundError(
                f"フィード {feed_url} に新潟県（{NIIGATA_PREF_CODE}）の"
                f"警報・注意報電文（VPWW53/VPWW54）が見つかりませんでした"
            )
        # SSRF 対策: 電文 XML の取得先を気象庁公式ドメインに限定する
        # （フィード内容が改ざん・差し替えされた場合に任意ホストへの
        #   リクエストが発生しないようにする）
        from urllib.parse import urlparse

        parsed = urlparse(message_url)
        if not parsed.scheme.startswith("http") or not parsed.hostname or not parsed.hostname.endswith(
            ".data.jma.go.jp"
        ):
            raise WarningFetchError(
                f"電文 XML の取得先が気象庁ドメインではありません: {message_url}"
            )
        message_raw = self._download(message_url)
        return parse_warning_xml(message_raw, message_url=message_url)


# ---------------------------------------------------------------------------
# モジュール関数（シンプルな利用向け）
# ---------------------------------------------------------------------------


def get_niigata_warnings(
    *,
    ttl: float = DEFAULT_TTL,
    timeout: float = 15.0,
    force: bool = False,
) -> WarningData:
    """新潟県の最新の警報・注意報を 1 コールで取得する（キャッシュ付き）。"""
    with WarningClient(ttl=ttl, timeout=timeout) as client:
        return client.fetch(force=force)


# ---------------------------------------------------------------------------
# フィードパース
# ---------------------------------------------------------------------------


def find_niigata_message_url(feed_xml: bytes | str, *, base_url: str = EXTRA_FEED_URL) -> str | None:
    """Atom フィードから新潟県の最新の警報・注意報電文 URL を探す。

    フィード中の <entry> を新しい順に走査し、電文種別 VPWW53/VPWW54 かつ
    ファイル名が _150000.xml で終わる <link href> を最初に見つけた時点で返す。

    Args:
        feed_xml: フィードのバイト列または文字列。
        base_url: 相対 URL の場合の基準（フィード自身の URL）。

    Returns:
        電文 XML の絶対 URL。見つからなければ None。

    Raises:
        WarningParseError: フィード XML のパースに失敗した場合。
    """
    root = _parse_xml(feed_xml, "フィード")
    if root.tag != f"{{{_NS['a']}}}feed":
        raise WarningParseError("フィードのルート要素が <feed> ではありません")

    entries = root.findall(f"{{{_NS['a']}}}entry")
    for entry in entries:
        title = _text(entry, f"{{{_NS['a']}}}title")
        link_el = entry.find(f"{{{_NS['a']}}}link")
        if link_el is None:
            continue
        href = link_el.get("href")
        if not href:
            continue
        # 電文種別（タイトル）で判別: 気象特別警報・警報・注意報 (VPWW53)
        # / 気象警報・注意報 (VPWW54)
        kind_matched = False
        for kind in _MESSAGE_KINDS:
            if kind in title or kind in href:
                kind_matched = True
                break
        if not kind_matched:
            continue
        # 府県コード（ファイル名の末尾）で新潟県を判定
        if not href.rstrip("/").endswith(_TARGET_SUFFIX):
            continue
        return urljoin(base_url, href)
    return None


def list_message_urls(
    feed_xml: bytes | str,
    *,
    base_url: str = EXTRA_FEED_URL,
    limit: int = 20,
) -> list[str]:
    """フィード中の新潟県の警報・注意報電文 URL を新しい順に列挙する。"""
    root = _parse_xml(feed_xml, "フィード")
    urls: list[str] = []
    for entry in root.findall(f"{{{_NS['a']}}}entry"):
        title = _text(entry, f"{{{_NS['a']}}}title")
        link_el = entry.find(f"{{{_NS['a']}}}link")
        if link_el is None:
            continue
        href = link_el.get("href")
        if not href:
            continue
        kind_matched = any(k in title or k in href for k in _MESSAGE_KINDS)
        if not kind_matched or not href.rstrip("/").endswith(_TARGET_SUFFIX):
            continue
        urls.append(urljoin(base_url, href))
        if len(urls) >= limit:
            break
    return urls


# ---------------------------------------------------------------------------
# 電文 XML パース
# ---------------------------------------------------------------------------


def parse_warning_xml(raw: bytes | str, *, message_url: str = "") -> WarningData:
    """VPWW53/VPWW54 電文 XML をパースして 4 階層の警報・注意報を返す。

    Args:
        raw: 電文 XML のバイト列または文字列。
        message_url: 電文の取得元 URL（メタデータとして記録）。

    Returns:
        WarningData（府県 → 一次細分 → 地域 → 市町村 の順の 4 階層）。

    Raises:
        WarningParseError: XML のパースに失敗した場合。
    """
    root = _parse_xml(raw, "電文")

    # -- 発信情報（Control） ------------------------------------------------
    control_title = _text(root, f"{{{_NS['j']}}}Control/{{{_NS['j']}}}Title") or ""
    editorial = _text(root, f"{{{_NS['j']}}}Control/{{{_NS['j']}}}EditorialOffice") or ""

    # -- 見出し情報（Head） ---------------------------------------------------
    headline_text = ""
    info_type = ""
    report_dt: datetime | None = None
    head = root.find(f"{{{_NS['h']}}}Head")
    if head is not None:
        info_type = _text(head, f"{{{_NS['h']}}}InfoType") or ""
        raw_dt = _text(head, f"{{{_NS['h']}}}ReportDateTime") or ""
        report_dt = _parse_datetime(raw_dt)
        head_text = head.find(f"{{{_NS['h']}}}Headline/{{{_NS['h']}}}Text")
        if head_text is not None and head_text.text:
            headline_text = head_text.text.strip()
    # 見出し（Head/Title、例: "新潟県気象警報・注意報"）。無ければ Control/Title にフォールバック
    title = ""
    if head is not None:
        title = _text(head, f"{{{_NS['h']}}}Title") or ""
    if not title:
        title = control_title

    # -- 警報・注意報本体（Body/Warning） ------------------------------------
    levels: list[WarningLevel] = []
    for warning in root.findall(f"{{{_NS['m']}}}Body/{{{_NS['m']}}}Warning"):
        type_label = warning.get("type") or ""
        level_name = _LEVEL_LABELS.get(type_label, type_label)
        areas: list[WarningArea] = []
        for item in warning.findall(f"{{{_NS['m']}}}Item"):
            area = item.find(f"{{{_NS['m']}}}Area")
            if area is None:
                continue
            name = _text(area, f"{{{_NS['m']}}}Name") or ""
            code = _text(area, f"{{{_NS['m']}}}Code") or ""
            kinds: list[WarningKind] = []
            for kind in item.findall(f"{{{_NS['m']}}}Kind"):
                kinds.append(
                    WarningKind(
                        name=_text(kind, f"{{{_NS['m']}}}Name") or "",
                        code=_text(kind, f"{{{_NS['m']}}}Code") or "",
                        status=_normalize_status(_text(kind, f"{{{_NS['m']}}}Status") or ""),
                    )
                )
            areas.append(WarningArea(name=name, code=code, kinds=tuple(kinds)))
        levels.append(WarningLevel(level=level_name, type_label=type_label, areas=tuple(areas)))

    # Warning セクションが無い（電文の形式が想定外）場合はエラーにする
    if not levels:
        raise WarningParseError(
            "電文 XML に <Warning> セクションが見つかりませんでした（VPWW53/VPWW54 以外の可能性）"
        )

    return WarningData(
        title=title,
        headline=headline_text,
        info_type=info_type,
        report_datetime=report_dt or datetime.now().astimezone(),
        editorial_office=editorial,
        message_kind=_detect_message_kind(control_title),
        message_url=message_url,
        levels=tuple(levels),
    )


def _detect_message_kind(title: str) -> str:
    """電文タイトルから電文種別（VPWW53/VPWW54 等）を推定する。"""
    if "特別警報" in title:
        return "VPWW53"
    if "警報・注意報" in title or "警報" in title:
        return "VPWW54"
    return ""


def _normalize_status(status: str) -> str:
    """<Status> のテキストを正規化する。"""
    status = status.strip()
    if not status:
        return _STATUS_NONE
    return _STATUS_MAP.get(status, status)


# ---------------------------------------------------------------------------
# パース補助
# ---------------------------------------------------------------------------


def _parse_xml(raw: bytes | str, label: str) -> ET.Element:
    """バイト列・文字列を XML パースしてルート要素を返す。"""
    try:
        return ET.fromstring(raw)
    except ET.ParseError as e:
        raise WarningParseError(f"{label} XML のパースに失敗しました: {e}") from e


def _text(element: ET.Element, path: str) -> str | None:
    """子要素をパスで探してテキストを返す（見つからなければ None）。"""
    node = element.find(path)
    if node is None or node.text is None:
        return None
    return node.text.strip()


def _parse_datetime(raw: str) -> datetime | None:
    """ISO8601 文字列（"2026-08-31T14:02:00+09:00"）を datetime に変換する。

    タイムゾーンオフセット付きの形式は Python 3.11+ の
    datetime.fromisoformat でそのまま解釈できる。
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
