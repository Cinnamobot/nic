"""ngt コマンドのエントリポイント。

`uv run ngt --help` で利用可能。

サブコマンド:
  snow    積雪情報（ランキング・観測所指定）
  weather 気温・天気情報
  tour    観光情報（スポット・天気×おすすめ・温泉・入込客数）
  warning 警報・注意報一覧（府県/一次細分/地域/市町村別）
  stats   統計・オープンデータ
  search  全データ横断検索

共通オプション（ルート・各サブコマンドのどちらでも指定可）:
  --json   出力を JSON にする（表形式の代わり）
  --force  キャッシュを無視して再取得
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import typer

from nic.core.amedas import (
    AmedasClient,
    AmedasElement,
    AmedasError,
    AmedasStationNotFoundError,
    NIIGATA_STATIONS,
    SOURCE_TEXT as AMEDAS_SOURCE,
    SOURCE_URL as AMEDAS_SOURCE_URL,
)
from nic.core.opendata import (
    Dataset,
    OpenDataClient,
    OpenDataError,
    PopulationRecord,
)
from nic.core.tourism import (
    Spot,
    TourismClient,
    TourismError,
    SOURCE_TEXT as TOURISM_SOURCE,
    SOURCE_URL as TOURISM_SOURCE_URL,
)
from nic.core.warning import (
    WarningArea,
    WarningClient,
    WarningData,
    WarningError,
    SOURCE_TEXT as WARNING_SOURCE,
)

# Windows コンソール（cp932）でも UTF-8 で出力するためのエンコーディング固定。
# コンソールスクリプト（pyproject [project.scripts]）のエントリポイント起動時に実行される。
def _reconfigure_stdio_utf8() -> None:
    """stdout/stderr のエンコーディングを UTF-8 に固定する（Windows 文字化け対策）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass  # リダイレクト時など reconfigure 不能な環境では無視


_reconfigure_stdio_utf8()

app = typer.Typer(
    name="ngt",
    help="新潟県の情報（気象・河川・観光・交通・統計など）にアクセスする CLI ツール。",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    # rich のボックス枠線（┌─┐│└┘）を廃止し、プレーンなテキストでヘルプを表示する。
    # Windows 標準コンソールでの文字化けと、AI エージェントのパース性を両立させる。
    rich_markup_mode=None,
)

# 表形式の最大表示件数（--limit 未指定時の既定値）
DEFAULT_LIMIT = 20

# アメダス / オープンデータ共通のキャッシュ TTL（秒）
CLI_TTL = 300.0

# 気象要素表示名（表・JSON の要素名として利用）
_ELEMENT_LABELS: dict[AmedasElement, str] = {
    AmedasElement.SNOW: "積雪",
    AmedasElement.PRECIPITATION: "1時間降水量",
    AmedasElement.MAX_TEMP: "最高気温",
    AmedasElement.MIN_TEMP: "最低気温",
}


@app.callback()
def main(
    ctx: typer.Context,
    json_output: bool = typer.Option(
        False,
        "--json",
        help="出力を JSON 形式にする。",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="キャッシュを無視して再取得する。",
    ),
) -> None:
    """ngt ルートコマンド（サブコマンド群の親）。"""
    ctx.obj = {"json": json_output, "force": force}


# ---------------------------------------------------------------------------
# 共通オプションの受け渡し
# ---------------------------------------------------------------------------

# 各サブコマンドが解決した最終的なフラグを保持する（本文から参照するため）。
# サブコマンド冒頭で _apply_common_flags() により確定する。
_CONTEXT: dict[str, Any] = {"json": False, "force": False}


def _apply_common_flags(ctx: typer.Context, json_output: bool, force: bool) -> None:
    """ルート（ctx.obj）とサブコマンド引数のフラグをマージして確定する。"""
    base = ctx.obj if isinstance(ctx.obj, dict) else {}
    _CONTEXT["json"] = bool(base.get("json")) or bool(json_output)
    _CONTEXT["force"] = bool(base.get("force")) or bool(force)


def _is_json() -> bool:
    return bool(_CONTEXT.get("json", False))


def _is_force() -> bool:
    return bool(_CONTEXT.get("force", False))


# 観測所名 → コードの対応表（--station の名前解決用）
_STATION_NAME_TO_CODE: dict[str, str] = {
    st.name: st.code for st in NIIGATA_STATIONS.values()
}


def _resolve_station(token: str) -> str:
    """--station の要素 1 つを観測所コードに解決する（コード・名前の両対応）。

    5 桁の観測所コードはそのまま使い、観測所名（例: 長岡）はコードに変換する。
    解決できない場合はエラーヒント付きで終了する（exit code 2）。
    """
    if token in NIIGATA_STATIONS:
        return token
    if token.isdigit():
        typer.echo(
            f"エラー: 新潟県内に観測所番号 {token} は存在しません。",
            err=True,
        )
        typer.echo(
            "ヒント: ngt search で観測所名からコードを確認できます"
            "（例: ngt search 長岡）。",
            err=True,
        )
        raise typer.Exit(code=2)
    code = _STATION_NAME_TO_CODE.get(token)
    if code is not None:
        return code
    typer.echo(
        f"エラー: 観測所「{token}」は見つかりません"
        "（観測所コードまたは観測所名で指定してください）。",
        err=True,
    )
    typer.echo(
        "ヒント: ngt search で観測所名からコードを確認できます"
        "（例: ngt search 長岡）。",
        err=True,
    )
    raise typer.Exit(code=2)


def _split_stations(station: str | None) -> list[str] | None:
    """--station の文字列を観測所コードのリストに変換する。

    カンマ区切り（例: "54841,54232"）と空白区切り（例: "54841 54232"）の
    両方を受け付ける。観測所名（例: "長岡"）もコードに解決する。
    未指定なら None。
    """
    if not station:
        return None
    tokens = [t.strip() for t in station.replace(" ", ",").split(",") if t.strip()]
    codes = [_resolve_station(t) for t in tokens]
    return codes or None


# ---------------------------------------------------------------------------
# 出力ヘルパー
# ---------------------------------------------------------------------------


def _emit_json(payload: Any) -> None:
    """辞書・リストを整形済み JSON として出力する。"""
    typer.echo(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
    )


def _json_default(o: Any) -> Any:
    """JSON 変換できない値のフォールバック（datetime など）。"""
    if isinstance(o, datetime):
        return o.astimezone(timezone.utc).isoformat()
    return str(o)


def _display_width(text: str) -> int:
    """表示幅（全角=2、半角=1）を計算する。"""
    return sum(2 if ord(c) > 0x2E7F else 1 for c in text)


def _render_table(headers: list[str], rows: list[list[str]]) -> None:
    """ヘッダ + 行の表をターミナルで読みやすい形式で出力する。

    枠線を使わないプレーン形式（ヘッダー行 + 区切り線 + データ行）で、
    AI エージェントやパイプ処理でもパースしやすい。
    """
    if not rows:
        typer.echo("（該当データがありません）")
        return
    # ヘッダーもデータ行と同じ「全角=2」の表示幅計算に統一する（桁ずれ防止）
    widths = [_display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], _display_width(cell))
    # ヘッダー
    typer.echo("  ".join(_pad(h, widths[i]) for i, h in enumerate(headers)))
    typer.echo("  ".join("-" * w for w in widths))
    # データ行
    for row in rows:
        typer.echo(
            "  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)).rstrip()
        )


def _pad(text: str, width: int) -> str:
    """表示幅（全角=2）を考慮して右詰めパディングする。"""
    return text + " " * max(0, width - _display_width(text))


def _print_warnings(client: OpenDataClient) -> None:
    """フォールバック状況などの警告があれば注記として表示する。"""
    for w in getattr(client, "warnings", []):
        typer.echo(f"注: {w}", err=True)


# 表出力用の表示タイムゾーン（日本標準時 = UTC+9、夏時間なし）
_JST = timezone(timedelta(hours=9))


def _format_observed_at(observed_at: datetime) -> str:
    """観測日時を UTC ISO8601 文字列に変換する（JSON 出力用。UTC のまま）。"""
    return observed_at.astimezone(timezone.utc).isoformat()


def _format_observed_at_jst(observed_at: datetime) -> str:
    """観測日時を JST（Asia/Tokyo = UTC+9）ISO8601 文字列に変換する（表出力用）。"""
    return observed_at.astimezone(_JST).isoformat()


# ---------------------------------------------------------------------------
# データ変換ヘルパー（JSON 用）
# ---------------------------------------------------------------------------


def _observation_json(obs) -> dict[str, Any]:
    """Observation を JSON 用の辞書に変換する。"""
    return {
        "station_code": obs.station.code,
        "station_name": obs.station.name,
        "value": obs.value,
        "quality": obs.quality,
        "quality_text": obs.quality_text,
        "observed_at": _format_observed_at(obs.observed_at),
    }


def _dataset_json(d: Dataset) -> dict[str, Any]:
    """Dataset を JSON 用の辞書に変換する。"""
    return {
        "id": d.id,
        "name": d.name,
        "category": d.category,
        "description": d.description,
        "fields": d.fields,
        "fiscal_year": d.fiscal_year,
        "update_frequency": d.update_frequency,
        "format": d.format,
        "url": d.url,
        "department": d.department,
    }


def _population_json(r: PopulationRecord) -> dict[str, Any]:
    """PopulationRecord を JSON 用の辞書に変換する。"""
    return {
        "date": r.date,
        "municipality_code": r.municipality_code,
        "municipality_name": r.municipality_name,
        "total": r.total,
        "male": r.male,
        "female": r.female,
    }


def _michinoeki_json(st) -> dict[str, Any]:
    """MichiNoEki を JSON 用の辞書に変換する。"""
    return {
        "id": st.id,
        "name": st.name,
        "route": st.route,
        "address": st.address,
        "phone": st.phone,
    }


# ---------------------------------------------------------------------------
# tour: 観光情報
# ---------------------------------------------------------------------------

# 天気×おすすめ: 観測所名 → 推奨カテゴリ名（雨の日の屋内・温泉向きの分類）
# 雨が降っている地域の観測所に応じて、カテゴリにマッチするスポットを推奨する。
_WEATHER_RECOMMENDATION: dict[str, str] = {
    # 温泉地・スキーエリア・山間部（雨の日は温泉がおすすめ）
    "湯沢": "温泉",
    "塩沢": "温泉",
    "十日町": "温泉",
    "津南": "温泉",
    "安塚": "温泉",
    "松代": "温泉",
    "大湯": "温泉",
    "糸魚川": "温泉",
    "能生": "温泉",
    "平岩": "温泉",
    "関山": "温泉",
    "守門": "温泉",
    "小出": "温泉",
    "小国": "温泉",
    "栃尾": "温泉",
    "樽本": "温泉",
    "赤谷": "温泉",
    "津川": "温泉",
    "室谷": "温泉",
    "高根": "温泉",
    "三面": "温泉",
    "下関": "温泉",
    "宮寄上": "温泉",
    "筒方": "温泉",
    "川谷": "温泉",
    # 都市部・海岸・島（雨の日は屋内の集客施設がおすすめ）
    "新潟": "集客施設",
    "新津": "集客施設",
    "巻": "集客施設",
    "三条": "集客施設",
    "村松": "集客施設",
    "中条": "集客施設",
    "長岡": "集客施設",
    "高田": "集客施設",
    "大潟": "集客施設",
    "村上": "集客施設",
    "松浜": "集客施設",
    "瓢湖": "集客施設",
    "弾崎": "集客施設",
    "羽茂": "集客施設",
    "相川": "集客施設",
    "両津": "集客施設",
    "粟島": "集客施設",
    "寺泊": "集客施設",
    "柏崎": "集客施設",
}


# 推奨スポットを「雨が降っている観測所から半径何 km 以内」に限定するか（地理的妥当性のため）
_RECOMMEND_RADIUS_KM = 40.0


# 推奨カテゴリ → スポットのマッチングキーワード（名称・区分・住所・説明の部分一致）
_TOUR_RECOMMEND_KEYWORDS: dict[str, tuple[str, ...]] = {
    "温泉": ("温泉",),
    "集客施設": ("集客施設", "映画館", "劇場", "公会堂", "展示場", "体育館"),
}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2 地点間の大円距離（km）を計算する（ヒュベニ近似の代わりに球面三角法）。"""
    import math

    r = 6371.0088  # 地球平均半径 km
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


# 観光入込客数の分類名（表示用ラベル）
_IRIKOMI_COLUMN_LABELS: dict[str, str] = {
    "total": "観光入込客数合計",
    "event_total": "行祭事・イベント合計",
    "spot_total": "観光地点合計",
    "nature": "自然",
    "history_culture": "歴史・文化",
    "onsen_health": "温泉・健康",
    "sports_recreation": "スポーツ・レクリエーション",
    "urban_tourism": "都市型観光",
    "other": "その他",
}


def _spot_json(s: Spot) -> dict[str, Any]:
    """Spot を JSON 用の辞書に変換する。"""
    return {
        "id": s.id,
        "name": s.name,
        "category": s.category,
        "lat": s.lat,
        "lon": s.lon,
        "address": s.address,
        "phone": s.phone,
        "url": s.url,
        "description": s.description,
        "source": s.source,
        "source_url": s.source_url,
    }


def _recommended_spots(spots: list[Spot], kinds: tuple[str, ...]) -> list[Spot]:
    """推奨カテゴリ（区分）に一致するスポットを返す。

    区分（category）の前方一致でマッチさせる（例: 温泉、集客施設（映画館））。
    名称・住所・説明の全文部分一致は使わない（誤推奨の原因のため）。
    """
    hit: list[Spot] = []
    for s in spots:
        if any(s.category.startswith(kw) for kw in kinds):
            hit.append(s)
    return hit


def _tour_warnings(client) -> None:
    """観光クライアントのフォールバック状況を注記として表示する。"""
    for w in getattr(client, "warnings", []):
        typer.echo(f"注: {w}", err=True)


@app.command()
def tour(
    ctx: typer.Context,
    spots: bool = typer.Option(
        False,
        "--spots",
        help="観光スポット一覧を表示する（温泉 + 集客施設）。",
    ),
    onsen: bool = typer.Option(
        False,
        "--onsen",
        help="温泉スポット一覧を表示する。",
    ),
    category: str = typer.Option(
        None,
        "--category",
        help="スポットの区分で絞り込み（例: 温泉, 集客施設）。",
    ),
    weather: bool = typer.Option(
        False,
        "--weather",
        help="観光スポットと気象情報（アメダス）を組み合わせて表示する。",
    ),
    recommend: bool = typer.Option(
        False,
        "--recommend",
        help="天気に合わせたおすすめスポットを表示する（--weather と同時指定）。",
    ),
    irikomi: bool = typer.Option(
        False,
        "--irikomi",
        help="観光入込客数（年別・分類別）を表示する。",
    ),
    year: int = typer.Option(
        None,
        "--year",
        help="入込客数の年（例: 2024）。未指定は全件。",
    ),
    limit: int = typer.Option(
        DEFAULT_LIMIT,
        "--limit",
        "-n",
        min=1,
        max=200,
        help="表示件数。",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="出力を JSON 形式にする。",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="キャッシュを無視して再取得する。",
    ),
) -> None:
    """観光情報を表示する（スポット・天気×おすすめ・温泉・入込客数）。

    フラグ未指定時は観光スポット一覧（温泉 + 集客施設）を表示します。
    --weather で気象情報を、--recommend で天気に合わせたおすすめを表示します。
    """
    _apply_common_flags(ctx, json_output, force)
    flags = [spots, onsen, weather, irikomi]
    if sum(flags) > 1:
        typer.echo(
            "エラー: --spots / --onsen / --weather / --irikomi は同時に指定できません",
            err=True,
        )
        raise typer.Exit(code=2)
    if recommend and not weather:
        typer.echo(
            "エラー: --recommend は --weather と同時に指定してください",
            err=True,
        )
        raise typer.Exit(code=2)
    if year is not None and not irikomi:
        typer.echo(
            "エラー: --year は --irikomi と同時に指定してください",
            err=True,
        )
        raise typer.Exit(code=2)

    if irikomi:
        _tour_irikomi(ctx, year=year, limit=limit)
        return
    if onsen:
        _tour_onsen(ctx, limit=limit)
        return
    if weather:
        _tour_weather(ctx, recommend=recommend, limit=limit)
        return
    _tour_spots(ctx, category=category, limit=limit)


def _tour_spots(ctx: typer.Context, *, category: str | None, limit: int) -> None:
    """観光スポット一覧（温泉 + 集客施設）を表示する。"""
    with TourismClient(ttl=CLI_TTL) as client:
        try:
            spots = client.get_spots(category=category, force=_is_force())
        except TourismError as e:
            typer.echo(f"エラー: {e}", err=True)
            typer.echo(
                "ヒント: データ源が一時的に利用できない可能性があります。"
                "時間をおいて再試行してください。",
                err=True,
            )
            raise typer.Exit(code=1)
        _tour_warnings(client)
        if _is_json():
            _emit_json(
                {
                    "type": "spots",
                    "category": category,
                    "spots": [_spot_json(s) for s in spots[:limit]],
                    "count": len(spots),
                    "source": TOURISM_SOURCE,
                    "source_url": TOURISM_SOURCE_URL,
                }
            )
            return
        if category:
            typer.echo(f"観光スポット一覧（区分: {category}）")
        else:
            typer.echo(f"観光スポット一覧（{len(spots)} 件中 先頭 {min(limit, len(spots))} 件）")
        headers = ["№", "スポット名", "区分", "所在地", "電話番号", "URL"]
        rows = []
        for idx, s in enumerate(spots[:limit], start=1):
            rows.append(
                [
                    str(idx),
                    s.name,
                    s.category,
                    s.address,
                    s.phone,
                    s.url,
                ]
            )
        _render_table(headers, rows)
        typer.echo(f"{TOURISM_SOURCE} / 国土数値情報（国土交通省）")


def _tour_onsen(ctx: typer.Context, *, limit: int) -> None:
    """温泉スポット一覧（新潟市 GIS 温泉利用許可施設）を表示する。"""
    with TourismClient(ttl=CLI_TTL) as client:
        try:
            spots = client.get_onsen_spots(force=_is_force())
        except TourismError as e:
            typer.echo(f"エラー: {e}", err=True)
            typer.echo(
                "ヒント: データ源が一時的に利用できない可能性があります。"
                "時間をおいて再試行してください。",
                err=True,
            )
            raise typer.Exit(code=1)
        _tour_warnings(client)
        if _is_json():
            _emit_json(
                {
                    "type": "onsen",
                    "spots": [_spot_json(s) for s in spots[:limit]],
                    "count": len(spots),
                    "source": TOURISM_SOURCE,
                    "source_url": TOURISM_SOURCE_URL,
                }
            )
            return
        typer.echo(f"温泉スポット一覧（{len(spots)} 件中 先頭 {min(limit, len(spots))} 件）")
        headers = ["№", "温泉名", "所在地", "電話番号", "泉質・備考"]
        rows = []
        for idx, s in enumerate(spots[:limit], start=1):
            description = s.description
            if description.startswith("（") and description.endswith("）"):
                description = description[1:-1]
            rows.append([str(idx), s.name, s.address, s.phone, description])
        _render_table(headers, rows)
        typer.echo(f"{TOURISM_SOURCE}（CC-BY）")


def _tour_weather(
    ctx: typer.Context, *, recommend: bool, limit: int
) -> None:
    """観光スポットと気象情報（アメダス）を組み合わせて表示する。"""
    with TourismClient(ttl=CLI_TTL) as tclient, AmedasClient(ttl=CLI_TTL) as aclient:
        try:
            spots = tclient.get_spots(force=_is_force())
            data = aclient.fetch(
                AmedasElement.PRECIPITATION, force=_is_force()
            )
        except (TourismError, AmedasError) as e:
            typer.echo(f"エラー: {e}", err=True)
            typer.echo(
                "ヒント: データ源が一時的に利用できない可能性があります。"
                "時間をおいて再試行してください。",
                err=True,
            )
            raise typer.Exit(code=1)
        _tour_warnings(tclient)

        # 降水量の多い観測所（雨が降っている地域）を抽出
        rainy: list[dict[str, Any]] = [
            {
                "name": o.station.name,
                "lat": o.station.lat,
                "lon": o.station.lon,
            }
            for o in data.observations
            if o.value is not None and o.value >= 1.0
        ]

        if _is_json():
            recommendations: list[dict[str, Any]] = []
            if recommend:
                shown: set[str] = set()
                for st in rainy:
                    group = _WEATHER_RECOMMENDATION.get(st["name"])
                    if group is None:
                        continue
                    keywords = _TOUR_RECOMMEND_KEYWORDS.get(group, (group,))
                    for s in _recommended_spots(spots, keywords):
                        if s.id in shown:
                            continue
                        shown.add(s.id)
                        if st.get("lat") is not None and s.lat is not None and s.lon is not None:
                            dist = _haversine_km(st["lat"], st["lon"], s.lat, s.lon)
                            if dist > _RECOMMEND_RADIUS_KM:
                                continue
                        recommendations.append(
                            {
                                "spot": _spot_json(s),
                                "reason": (
                                    f"雨が降っている{st['name']}方面は{group}がおすすめ"
                                ),
                                "distance_km": round(dist, 1)
                                if st.get("lat") is not None
                                and s.lat is not None
                                and s.lon is not None
                                else None,
                            }
                        )
            _emit_json(
                {
                    "type": "weather_recommend" if recommend else "weather",
                    "observed_at": _format_observed_at(data.fetched_at),
                    "rainy_stations": [st["name"] for st in rainy],
                    "spots": [_spot_json(s) for s in spots[:limit]],
                    "recommendations": recommendations if recommend else None,
                    "source": TOURISM_SOURCE,
                    "source_url": TOURISM_SOURCE_URL,
                }
            )
            return

        typer.echo(f"観光スポットと天気（{AMEDAS_SOURCE.removeprefix('出典:')}）")
        typer.echo(f"観測時刻: {_format_observed_at_jst(data.fetched_at)}")
        typer.echo(
            f"雨が降っている地域: {', '.join(st['name'] for st in rainy) if rainy else 'なし'}"
        )

        headers = ["№", "スポット名", "区分", "所在地", "電話番号", "URL"]
        rows = [
            [
                str(idx),
                s.name,
                s.category,
                s.address,
                s.phone,
                s.url,
            ]
            for idx, s in enumerate(spots[:limit], start=1)
        ]
        _render_table(headers, rows)
        typer.echo(f"{TOURISM_SOURCE} / 国土数値情報（国土交通省）")

        if recommend:
            _tour_recommend_table(spots, rainy)


def _tour_recommend_table(spots: list[Spot], rainy: list[dict[str, Any]]) -> None:
    """天気に合わせたおすすめスポットの表を表示する。

    雨が降っている観測所の周辺（_RECOMMEND_RADIUS_KM 以内）のスポットに
    限定することで、「県内のどこかで雨が降っている」だけで県全域の
    温泉を列挙するのを避ける。
    """
    typer.echo("\n■ 天気に合わせたおすすめスポット")
    shown: set[str] = set()
    for st in rainy:
        group = _WEATHER_RECOMMENDATION.get(st["name"])
        if group is None:
            continue
        keywords = _TOUR_RECOMMEND_KEYWORDS.get(group, (group,))
        hit = _recommended_spots(spots, keywords)
        for s in hit:
            if s.id in shown:
                continue
            # 観測所から遠いスポットは推奨しない（座標が両方ある場合のみ判定）
            if (
                st.get("lat") is not None
                and s.lat is not None
                and s.lon is not None
            ):
                dist = _haversine_km(st["lat"], st["lon"], s.lat, s.lon)
                if dist > _RECOMMEND_RADIUS_KM:
                    continue
                typer.echo(
                    f"・{s.name}（{s.category}）— 雨が降っている{st['name']}方面は"
                    f"{group}がおすすめ（約{dist:.0f}km）"
                )
            else:
                typer.echo(
                    f"・{s.name}（{s.category}）— 雨が降っている{st['name']}方面は"
                    f"{group}がおすすめ"
                )
            shown.add(s.id)
    if not shown:
        typer.echo("（雨が降っている地域の周辺におすすめスポットが見つかりませんでした）")


def _tour_irikomi(ctx: typer.Context, *, year: int | None, limit: int) -> None:
    """観光入込客数（年別・分類別）を表示する。"""
    with TourismClient(ttl=CLI_TTL) as client:
        try:
            stats = client.get_irikomi(year=year, force=_is_force())
        except TourismError as e:
            typer.echo(f"エラー: {e}", err=True)
            typer.echo(
                "ヒント: データ源が一時的に利用できない可能性があります。"
                "時間をおいて再試行してください。",
                err=True,
            )
            raise typer.Exit(code=1)
        _tour_warnings(client)
        if _is_json():
            _emit_json(
                {
                    "type": "irikomi",
                    "year": year,
                    "stats": [
                        {
                            "year": s.year,
                            "era_year": s.era_year,
                            "total": s.total,
                            "event_total": s.event_total,
                            "spot_total": s.spot_total,
                            "nature": s.nature,
                            "history_culture": s.history_culture,
                            "onsen_health": s.onsen_health,
                            "sports_recreation": s.sports_recreation,
                            "urban_tourism": s.urban_tourism,
                            "other": s.other,
                            "source": s.source,
                            "source_url": s.source_url,
                        }
                        for s in stats[:limit]
                    ],
                    "source": TOURISM_SOURCE,
                    "source_url": TOURISM_SOURCE_URL,
                }
            )
            return
        typer.echo("観光入込客数（新潟市、年別・分類別）")
        headers = ["年", "和暦", "入込客数合計(千人)", "行祭事・イベント(千人)", "観光地点合計(千人)"]
        rows = [
            [
                str(s.year),
                s.era_year,
                f"{s.total:,}" if s.total is not None else "-",
                f"{s.event_total:,}" if s.event_total is not None else "-",
                f"{s.spot_total:,}" if s.spot_total is not None else "-",
            ]
            for s in stats[:limit]
        ]
        _render_table(headers, rows)
        typer.echo(f"{TOURISM_SOURCE}（CC-BY）")


# ---------------------------------------------------------------------------
# warning: 警報・注意報
# ---------------------------------------------------------------------------


# 階層名 → 表示用ラベル
_LEVEL_LABELS: dict[str, str] = {
    "府県": "府県",
    "一次細分": "一次細分",
    "地域": "地域",
    "市町村": "市町村",
}


# 警報・注意報の表示順（種別の重複表示を避けるための並び替え基準）
_WARNING_KIND_ORDER: tuple[str, ...] = (
    "特別警報",
    "暴風",
    "大雨",
    "洪水",
    "高潮",
    "波浪",
    "大雪",
    "暴風雪",
    "雷",
    "融雪",
    "濃霧",
    "乾燥",
    "なだれ",
    "低温",
    "霜",
    "着氷",
    "着雪",
)


_WARNING_LEVEL_CHOICES = ("府県", "一次細分", "地域", "市町村")


def _warning_areas(data: WarningData, level_name: str) -> tuple[WarningArea, ...]:
    """指定階層のエリア一覧を返す（存在しなければ空タプル）。"""
    lv = data.level(level_name)
    return lv.areas if lv is not None else ()


def _warning_area_rows(areas: tuple[WarningArea, ...]) -> list[list[str]]:
    """警報・注意報のエリア一覧を行リストに変換する。"""
    rows: list[list[str]] = []
    for a in areas:
        if a.has_warning:
            kinds = "、".join(f"{k.name} {k.status}" for k in a.kinds)
        else:
            kinds = a.status_summary
        rows.append([a.name, a.code, kinds])
    return rows


def _warning_kind_rows(areas: tuple[WarningArea, ...]) -> list[list[str]]:
    """警報・注意報の種別を行リストに変換する（種別 × 対象地域）。"""
    seen: set[tuple[str, str]] = set()
    rows: list[list[str]] = []
    for a in areas:
        for k in a.kinds:
            if k.status == "発表警報・注意報はなし":
                continue
            key = (k.name, k.status)
            if key in seen:
                continue
            seen.add(key)
            rows.append([k.name, k.status, a.name])
    return rows


def _warning_fallback_summary(data: WarningData) -> str:
    """警報・注意報の要約文（発表・継続中のみ）を返す。"""
    kinds = data.active_kinds
    if not kinds:
        return "発表警報・注意報はなし"
    return "、".join(f"{k.name} {k.status}" for k in kinds)


@app.command()
def warning(
    ctx: typer.Context,
    level: str = typer.Option(
        None,
        "--level",
        "-l",
        help="表示する階層（府県 / 一次細分 / 地域 / 市町村）。未指定は府県。",
    ),
    area: str = typer.Option(
        None,
        "--area",
        "-a",
        help="地域名で絞り込み（例: 中越, 十日町市）。部分一致。",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="出力を JSON 形式にする。",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="キャッシュを無視して再取得する。",
    ),
) -> None:
    """新潟県の警報・注意報一覧を表示する（気象庁防災情報XML）。

    府県 / 一次細分 / 地域 / 市町村の 4 階層を --level で切り替え、
    --area で地域名を絞り込めます。デフォルトは府県階層（新潟県全域）。
    """
    _apply_common_flags(ctx, json_output, force)
    level_name = level or "府県"
    if level_name not in _WARNING_LEVEL_CHOICES:
        typer.echo(
            f"エラー: --level は {', '.join(_WARNING_LEVEL_CHOICES)} のいずれかを指定してください",
            err=True,
        )
        raise typer.Exit(code=2)

    with WarningClient(ttl=60.0) as client:
        try:
            data = client.fetch(force=_is_force())
        except WarningError as e:
            typer.echo(f"エラー: {e}", err=True)
            typer.echo(
                "ヒント: データ源（気象庁防災情報XML）が一時的に利用できない可能性があります。"
                "時間をおいて再試行してください。",
                err=True,
            )
            raise typer.Exit(code=1)

        areas = _warning_areas(data, level_name)
        if area:
            areas = tuple(a for a in areas if area in a.name)

        if _is_json():
            _emit_json(
                {
                    "type": "warning",
                    "level": level_name,
                    "title": data.title,
                    "headline": data.headline,
                    "info_type": data.info_type,
                    "report_datetime": _format_observed_at(data.report_datetime),
                    "editorial_office": data.editorial_office,
                    "summary": _warning_fallback_summary(data),
                    "areas": [
                        {
                            "name": a.name,
                            "code": a.code,
                            "status": a.status_summary,
                            "active": a.has_warning,
                        }
                        for a in areas
                    ],
                    "source": WARNING_SOURCE,
                    "source_url": data.source_url,
                    "message_url": data.message_url,
                }
            )
            return

        typer.echo(f"警報・注意報一覧（{level_name}階層、{WARNING_SOURCE.removeprefix('出典:')}）")
        typer.echo(
            f"発表日時: {_format_observed_at_jst(data.report_datetime)} / "
            f"{data.editorial_office}"
        )
        if data.headline:
            typer.echo(data.headline)

        if areas:
            _render_table(
                ["地域名", "コード", "警報・注意報"],
                _warning_area_rows(areas),
            )
        else:
            typer.echo("（該当データがありません）")

        typer.echo(f"{WARNING_SOURCE}（公共データ利用規約 第1.0版）")


# ---------------------------------------------------------------------------
# snow: 積雪情報
# ---------------------------------------------------------------------------


@app.command()
def snow(
    ctx: typer.Context,
    rank: bool = typer.Option(
        False,
        "--rank",
        help="積雪の多い順のランキングを表示する。",
    ),
    station: str = typer.Option(
        None,
        "--station",
        "-s",
        help="観測所コードまたは観測所名（例: 54841, 湯沢）。カンマ・空白区切りで複数指定可。",
    ),
    limit: int = typer.Option(
        DEFAULT_LIMIT,
        "--limit",
        "-n",
        min=1,
        max=100,
        help="表示件数（ランキング時の上限）。",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="出力を JSON 形式にする。",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="キャッシュを無視して再取得する。",
    ),
) -> None:
    """積雪情報を表示する（気象庁アメダス）。

    デフォルトは新潟県内の積雪観測値を一覧表示する。
    冬季のみ提供（夏季は気象庁が提供休止のためエラーになる場合があります）。
    """
    _apply_common_flags(ctx, json_output, force)
    element = AmedasElement.SNOW
    with AmedasClient(ttl=CLI_TTL) as client:
        try:
            data = client.fetch(element, codes=_split_stations(station), force=_is_force())
        except AmedasError as e:
            typer.echo(f"エラー: {e}", err=True)
            if isinstance(e, AmedasStationNotFoundError):
                typer.echo(
                    "ヒント: ngt search で観測所名からコードを確認できます"
                    "（例: ngt search 長岡）。",
                    err=True,
                )
            else:
                typer.echo(
                    "ヒント: 積雪データは冬季のみ提供されています（夏季は提供休止）。"
                    "気温・降水量は weather コマンドで確認できます。",
                    err=True,
                )
            raise typer.Exit(code=1)

        observations = sorted(
            (o for o in data.observations if o.value is not None),
            key=lambda o: o.value,  # type: ignore[arg-type]
            reverse=True,
        )

        if _is_json():
            _emit_json(
                {
                    "element": element.value,
                    "element_label": _ELEMENT_LABELS[element],
                    "unit": "cm",
                    "observations": [
                        {**_observation_json(o), "rank": idx + 1}
                        for idx, o in enumerate(observations[:limit])
                    ],
                    "fetched_at": _format_observed_at(data.fetched_at),
                    "source": AMEDAS_SOURCE,
                    "source_url": AMEDAS_SOURCE_URL,
                }
            )
            return

        typer.echo(f"積雪情報（{AMEDAS_SOURCE.removeprefix('出典:')}、単位: cm）")
        if station:
            typer.echo(f"観測所: {', '.join(_split_stations(station) or [])}")
        if rank:
            typer.echo(f"順位は積雪の多い順（上位 {len(observations[:limit])} 地点）:")
        else:
            typer.echo(f"観測時刻: {_format_observed_at_jst(data.fetched_at)}")

        headers = ["順位", "観測所", "コード", "積雪(cm)", "品質", "観測時刻"]
        rows = [
            [
                str(idx + 1),
                o.station.name,
                o.station.code,
                f"{o.value:.1f}" if o.value is not None else "-",
                o.quality_text,
                _format_observed_at_jst(o.observed_at),
            ]
            for idx, o in enumerate(observations[:limit])
        ]
        _render_table(headers, rows)
        typer.echo(f"{AMEDAS_SOURCE} / 気象庁「最新の気象データ」CSV")


# ---------------------------------------------------------------------------
# weather: 気温・降水量
# ---------------------------------------------------------------------------


@app.command()
def weather(
    ctx: typer.Context,
    station: str = typer.Option(
        None,
        "--station",
        "-s",
        help="観測所コードまたは観測所名（例: 54232, 長岡）。カンマ・空白区切りで複数指定可。",
    ),
    limit: int = typer.Option(
        DEFAULT_LIMIT,
        "--limit",
        "-n",
        min=1,
        max=100,
        help="表示件数。",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="出力を JSON 形式にする。",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="キャッシュを無視して再取得する。",
    ),
) -> None:
    """気温（最高・最低）と降水量を表示する（気象庁アメダス）。"""
    _apply_common_flags(ctx, json_output, force)
    elements = [
        AmedasElement.MAX_TEMP,
        AmedasElement.MIN_TEMP,
        AmedasElement.PRECIPITATION,
    ]

    with AmedasClient(ttl=CLI_TTL) as client:
        try:
            datas = [
                client.fetch(e, codes=_split_stations(station), force=_is_force())
                for e in elements
            ]
        except AmedasError as e:
            typer.echo(f"エラー: {e}", err=True)
            if isinstance(e, AmedasStationNotFoundError):
                typer.echo(
                    "ヒント: ngt search で観測所名からコードを確認できます"
                    "（例: ngt search 長岡）。",
                    err=True,
                )
            else:
                typer.echo(
                    "ヒント: データ源が一時的に利用できない可能性があります。"
                    "時間をおいて再試行してください。",
                    err=True,
                )
            raise typer.Exit(code=1)

        # 観測所コード → 要素ごとの観測値
        by_code: dict[str, dict[str, Any]] = {}
        for data in datas:
            for obs in data.observations:
                by_code.setdefault(obs.station.code, {})[data.element.value] = obs

        if _is_json():
            records = []
            for code, obs_map in by_code.items():
                rec: dict[str, Any] = {}
                first_obs = next(iter(obs_map.values()), None)
                if first_obs is not None:
                    rec["station_name"] = first_obs.station.name
                for e in elements:
                    obs = obs_map.get(e.value)
                    rec[e.value] = obs.value if obs is not None else None
                records.append({"station_code": code, **rec})
            _emit_json(
                {
                    "element": "temperature_precipitation",
                    "unit": {
                        "max_temp": "℃",
                        "min_temp": "℃",
                        "precipitation": "mm",
                    },
                    "records": records,
                    "fetched_at": _format_observed_at(datas[0].fetched_at),
                    "source": AMEDAS_SOURCE,
                }
            )
            return

        typer.echo(f"気温・降水量（{AMEDAS_SOURCE.removeprefix('出典:')}）")
        typer.echo(f"観測時刻: {_format_observed_at_jst(datas[0].fetched_at)}")
        headers = ["観測所", "コード", "最高気温(℃)", "最低気温(℃)", "1時間降水量(mm)"]
        rows = []
        for code, obs_map in by_code.items():
            max_obs = obs_map.get(AmedasElement.MAX_TEMP.value)
            min_obs = obs_map.get(AmedasElement.MIN_TEMP.value)
            pre_obs = obs_map.get(AmedasElement.PRECIPITATION.value)
            name = (
                max_obs.station.name
                if max_obs is not None
                else min_obs.station.name
                if min_obs is not None
                else pre_obs.station.name
                if pre_obs is not None
                else code
            )
            rows.append(
                [
                    name,
                    code,
                    f"{max_obs.value:.1f}" if max_obs and max_obs.value is not None else "-",
                    f"{min_obs.value:.1f}" if min_obs and min_obs.value is not None else "-",
                    f"{pre_obs.value:.1f}" if pre_obs and pre_obs.value is not None else "-",
                ]
            )
        _render_table(headers, rows[:limit])
        typer.echo(f"{AMEDAS_SOURCE} / 気象庁「最新の気象データ」CSV")


# ---------------------------------------------------------------------------
# stats: 統計・オープンデータ
# ---------------------------------------------------------------------------


@app.command()
def stats(
    ctx: typer.Context,
    datasets: bool = typer.Option(
        False,
        "--datasets",
        help="オープンデータカタログのデータセット一覧を表示する。",
    ),
    population: bool = typer.Option(
        False,
        "--population",
        help="人口時系列データ（市町村別）を表示する。",
    ),
    tourism: bool = typer.Option(
        False,
        "--tourism",
        help="道の駅一覧（観光）を表示する。",
    ),
    category: str = typer.Option(
        None,
        "--category",
        help="データセットの分類（内容）で絞り込み（例: 運輸・観光）。",
    ),
    data_format: str = typer.Option(
        None,
        "--format",
        help="データセットの形式で絞り込み（例: CSV, Excel）。",
    ),
    query: str = typer.Option(
        None,
        "--query",
        "-q",
        help="データセット名・概要のキーワード検索。",
    ),
    municipality: str = typer.Option(
        None,
        "--municipality",
        "-m",
        help="人口データの市町村名で絞り込み（例: 新潟市）。",
    ),
    limit: int = typer.Option(
        DEFAULT_LIMIT,
        "--limit",
        "-n",
        min=1,
        max=200,
        help="表示件数。",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="出力を JSON 形式にする。",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="キャッシュを無視して再取得する。",
    ),
) -> None:
    """統計・オープンデータを表示する（新潟県オープンデータ）。

    フラグ未指定時はデータセット一覧を表示します。
    """
    _apply_common_flags(ctx, json_output, force)
    flags = [datasets, population, tourism]
    if sum(flags) > 1:
        typer.echo(
            "エラー: --datasets / --population / --tourism は同時に指定できません",
            err=True,
        )
        raise typer.Exit(code=2)

    with OpenDataClient(ttl=CLI_TTL) as client:
        if population:
            try:
                records = client.get_population(
                    municipality=municipality, force=_is_force()
                )
            except OpenDataError as e:
                typer.echo(f"エラー: {e}", err=True)
                raise typer.Exit(code=1)
            _print_warnings(client)
            if _is_json():
                _emit_json(
                    {
                        "type": "population",
                        "records": [_population_json(r) for r in records[:limit]],
                        "source": records[0].source if records else None,
                        "source_url": records[0].source_url if records else None,
                    }
                )
                return
            typer.echo("人口時系列データ（市町村別、人口総数）")
            typer.echo(
                f"{records[0].source} / {records[0].source_url}"
                if records
                else "出典: データなし"
            )
            headers = ["年月日", "市町村", "人口総数", "男", "女"]
            rows = [
                [
                    r.date,
                    r.municipality_name,
                    f"{r.total:,}",
                    f"{r.male:,}",
                    f"{r.female:,}",
                ]
                for r in records[:limit]
            ]
            _render_table(headers, rows)
            return

        if tourism:
            try:
                stations = client.get_tourism(force=_is_force())
            except OpenDataError as e:
                typer.echo(f"エラー: {e}", err=True)
                raise typer.Exit(code=1)
            _print_warnings(client)
            if _is_json():
                _emit_json(
                    {
                        "type": "michinoeki",
                        "stations": [_michinoeki_json(s) for s in stations[:limit]],
                        "source": stations[0].source if stations else None,
                        "source_url": stations[0].source_url if stations else None,
                    }
                )
                return
            typer.echo("道の駅一覧（新潟県）")
            typer.echo(
                f"{stations[0].source} / {stations[0].source_url}"
                if stations
                else "出典: データなし"
            )
            headers = ["番号", "駅名", "路線名", "所在地", "電話番号"]
            rows = [
                [str(s.id), s.name, s.route, s.address, s.phone]
                for s in stations[:limit]
            ]
            _render_table(headers, rows)
            return

        # デフォルト: データセット一覧
        try:
            ds = client.get_datasets(
                query=query,
                category=category,
                data_format=data_format,
                force=_is_force(),
            )
        except OpenDataError as e:
            typer.echo(f"エラー: {e}", err=True)
            raise typer.Exit(code=1)
        _print_warnings(client)
        if _is_json():
            _emit_json(
                {
                    "type": "datasets",
                    "datasets": [_dataset_json(d) for d in ds[:limit]],
                    "count": len(ds),
                    "source": ds[0].source if ds else None,
                }
            )
            return
        typer.echo(f"オープンデータカタログ（{len(ds)} 件中 先頭 {min(limit, len(ds))} 件）")
        headers = ["№", "データ名", "分類", "形式", "更新頻度", "年度"]
        rows = [
            [d.id, d.name, d.category, d.format, d.update_frequency, d.fiscal_year]
            for d in ds[:limit]
        ]
        _render_table(headers, rows)
        typer.echo(f"{ds[0].source}" if ds else "出典: データなし")


# ---------------------------------------------------------------------------
# search: 全データ横断検索
# ---------------------------------------------------------------------------


@app.command()
def search(
    ctx: typer.Context,
    keyword: str = typer.Argument(
        ...,
        help="検索キーワード（観測所名・市町村名・データ名など）。",
    ),
    limit: int = typer.Option(
        DEFAULT_LIMIT,
        "--limit",
        "-n",
        min=1,
        max=100,
        help="表示件数。",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="出力を JSON 形式にする。",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="キャッシュを無視して再取得する。",
    ),
) -> None:
    """全データを横断検索する（観測所・人口・道の駅・データセット）。"""
    _apply_common_flags(ctx, json_output, force)
    results: dict[str, Any] = {}

    # 1. 観測所（アメダス）: 名前・番号の部分一致
    stations_hit: list[dict[str, Any]] = []
    with AmedasClient(ttl=CLI_TTL) as aclient:
        for st in aclient.get_stations():
            if keyword in st.name or keyword in st.code:
                stations_hit.append(
                    {
                        "station_code": st.code,
                        "station_name": st.name,
                        "lat": st.lat,
                        "lon": st.lon,
                        "altitude": st.altitude,
                        "station_type": st.station_type,
                    }
                )
    results["stations"] = stations_hit

    # 2. オープンデータ（人口・道の駅・データセット）
    with OpenDataClient(ttl=CLI_TTL) as oclient:
        try:
            pop = oclient.get_population(force=_is_force())
            results["population"] = [
                _population_json(r)
                for r in pop
                if keyword in r.municipality_name or keyword in r.municipality_code
            ]
            results["population_source"] = pop[0].source if pop else None
            results["population_source_url"] = pop[0].source_url if pop else None
        except OpenDataError as e:
            results["population_error"] = str(e)

        try:
            michi = oclient.get_tourism(force=_is_force())
            results["michinoeki"] = [
                _michinoeki_json(s)
                for s in michi
                if keyword in s.name or keyword in s.address
            ]
            results["michinoeki_source"] = michi[0].source if michi else None
            results["michinoeki_source_url"] = michi[0].source_url if michi else None
        except OpenDataError as e:
            results["michinoeki_error"] = str(e)

        try:
            ds = oclient.get_datasets(force=_is_force())
            results["datasets"] = [
                _dataset_json(d)
                for d in ds
                if keyword in d.name
                or keyword in d.category
                or keyword in d.description
                or keyword in d.fields
            ]
            results["datasets_source"] = ds[0].source if ds else None
        except OpenDataError as e:
            results["datasets_error"] = str(e)

    _print_warnings(oclient)

    if _is_json():
        _emit_json(
            {
                "keyword": keyword,
                "count": {
                    "stations": len(results.get("stations", [])),
                    "population": len(results.get("population", [])),
                    "michinoeki": len(results.get("michinoeki", [])),
                    "datasets": len(results.get("datasets", [])),
                },
                "stations": results.get("stations", [])[:limit],
                "population": results.get("population", [])[:limit],
                "michinoeki": results.get("michinoeki", [])[:limit],
                "datasets": results.get("datasets", [])[:limit],
                "source": {
                    "stations": AMEDAS_SOURCE,
                    "population": results.get("population_source"),
                    "michinoeki": results.get("michinoeki_source"),
                    "datasets": results.get("datasets_source"),
                },
                "errors": [
                    results[k]
                    for k in ("population_error", "michinoeki_error", "datasets_error")
                    if k in results
                ],
            }
        )
        return

    total = (
        len(results.get("stations", []))
        + len(results.get("population", []))
        + len(results.get("michinoeki", []))
        + len(results.get("datasets", []))
    )
    typer.echo(f"検索キーワード: {keyword}（ヒット {total} 件）")

    stations_hit = results.get("stations", [])
    if stations_hit:
        typer.echo(f"\n■ アメダス観測所（{len(stations_hit)} 件）")
        _render_table(
            ["コード", "観測所", "緯度", "経度", "標高(m)", "種別"],
            [
                [
                    s["station_code"],
                    s["station_name"],
                    f"{s['lat']:.4f}",
                    f"{s['lon']:.4f}",
                    str(s["altitude"]),
                    s["station_type"],
                ]
                for s in stations_hit[:limit]
            ],
        )

    pop_hit = results.get("population", [])
    if pop_hit:
        shown = min(limit, len(pop_hit))
        typer.echo(f"\n■ 人口（{len(pop_hit)} 件中 先頭 {shown} 件）")
        _render_table(
            ["年月日", "市町村", "人口総数"],
            [
                [p["date"], p["municipality_name"], f"{p['total']:,}"]
                for p in pop_hit[:limit]
            ],
        )

    michi_hit = results.get("michinoeki", [])
    if michi_hit:
        typer.echo(f"\n■ 道の駅（{len(michi_hit)} 件）")
        _render_table(
            ["番号", "駅名", "所在地", "電話番号"],
            [
                [str(m["id"]), m["name"], m["address"], m["phone"]]
                for m in michi_hit[:limit]
            ],
        )

    ds_hit = results.get("datasets", [])
    if ds_hit:
        typer.echo(f"\n■ データセット（{len(ds_hit)} 件）")
        _render_table(
            ["№", "データ名", "分類", "形式"],
            [[d["id"], d["name"], d["category"], d["format"]] for d in ds_hit[:limit]],
        )

    for key in ("population_error", "michinoeki_error", "datasets_error"):
        if key in results:
            typer.echo(f"注: {results[key]}", err=True)

    if total == 0:
        typer.echo(
            "ヒットしませんでした。観測所名（例: 湯沢）・市町村名（例: 長岡）・"
            "キーワード（例: 観光）などをお試しください。",
            err=True,
        )
        raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """バージョン情報を表示する。"""
    from nic import __version__

    typer.echo(f"nic {__version__}")


def run() -> None:
    """エントリポイント（pyproject [project.scripts] から呼ばれる）。

    Windows コンソールでの文字化けを防ぐため、出力ストリームの
    エンコーディングを UTF-8 に固定してから Typer アプリを起動する。
    """
    _reconfigure_stdio_utf8()
    app()


if __name__ == "__main__":
    run()
