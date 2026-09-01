"""MCP サーバー: 新潟県データを MCP ツールとして公開する。

`uv run ngt-mcp` で起動し、stdio トランスポートで
MCP クライアント（Claude Desktop / Cursor 等）に以下の 7 ツールを提供する:

- get_snow_info:         積雪情報（気象庁アメダス、新潟県内観測所）
- get_weather_info:      気温（最高・最低）と降水量（気象庁アメダス）
- get_niigata_stats:     統計・オープンデータ（人口・道の駅・データセット一覧）
- get_tourist_spots:     観光スポット（温泉・集客施設、新潟市オープンデータ等）
- get_tour_recommendation: おすすめ観光ルート（スポット統合 + 入込客数・雨情報）
- get_warning_info:      警報・注意報（気象庁防災情報XML、府県〜市町村 4 階層）
- search_niigata_data:   全データ横断検索（観測所・人口・道の駅・データセット）

実装は mcp 公式 SDK（FastMCP）を使用し、コア（`nic.core.amedas` /
`nic.core.opendata` / `nic.core.tourism` /
`nic.core.warning`）の関数をそのままツールとして公開する。
全ツールのレスポンスには出典（source / source_url）を含める。

データ取得はコア側の TTL 付きキャッシュに従う（アメダス 300 秒 /
オープンデータ 3600 秒 / 観光 3600 秒 / 防災 60 秒）。force を指定すると
キャッシュを無視して再取得する。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from nic.core.amedas import (
    AmedasClient,
    AmedasElement,
    AmedasError,
    SOURCE_TEXT as AMEDAS_SOURCE,
)
from nic.core.opendata import (
    OpenDataClient,
    OpenDataError,
)
from nic.core.tourism import (
    SOURCE_TEXT as TOURISM_SOURCE,
    SOURCE_URL as TOURISM_SOURCE_URL,
    P33_SOURCE_TEXT as TOURISM_P33_SOURCE,
    TourismClient,
    TourismError,
)
from nic.core.warning import (
    SOURCE_TEXT as WARNING_SOURCE,
    SOURCE_URL as WARNING_SOURCE_URL,
    NIIGATA_PREF_CODE,
    WarningClient,
    WarningError,
    WarningKind,
)

# アメダス / オープンデータ共通のキャッシュ TTL（秒）
MCP_TTL = 300.0
# 警報・注意報（防災情報）のキャッシュ TTL（秒）。フィードは毎分更新されるため 60 秒。
MCP_WARNING_TTL = 60.0

# 各ツールのデフォルト表示件数（結果が巨大になりすぎないよう制限する）
DEFAULT_LIMIT = 50

# ---------------------------------------------------------------------------
# JSON 変換ヘルパー（コアのデータクラス → dict）
# ---------------------------------------------------------------------------


def _format_utc(dt: datetime) -> str:
    """datetime を UTC ISO8601 文字列に変換する。"""
    return dt.astimezone(timezone.utc).isoformat()


def _observation_json(obs) -> dict[str, Any]:
    """アメダス Observation を JSON 用 dict に変換する。"""
    return {
        "station_code": obs.station.code,
        "station_name": obs.station.name,
        "value": obs.value,
        "unit": "cm",
        "quality": obs.quality,
        "quality_text": obs.quality_text,
        "observed_at": _format_utc(obs.observed_at),
    }


def _dataset_json(d) -> dict[str, Any]:
    """Dataset を JSON 用 dict に変換する。"""
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


def _population_json(r) -> dict[str, Any]:
    """PopulationRecord を JSON 用 dict に変換する。"""
    return {
        "date": r.date,
        "municipality_code": r.municipality_code,
        "municipality_name": r.municipality_name,
        "total": r.total,
        "male": r.male,
        "female": r.female,
    }


def _michinoeki_json(st) -> dict[str, Any]:
    """MichiNoEki を JSON 用 dict に変換する。"""
    return {
        "id": st.id,
        "name": st.name,
        "route": st.route,
        "address": st.address,
        "phone": st.phone,
    }


def _opendata_warnings(client: OpenDataClient) -> list[str]:
    """オープンデータ取得時のフォールバック警告（データ源の失敗理由など）。"""
    return list(getattr(client, "warnings", []))


def _tourism_warnings(client: TourismClient) -> list[str]:
    """観光データ取得時のフォールバック警告（データ源の失敗理由など）。"""
    return list(getattr(client, "warnings", []))


def _spot_json(s) -> dict[str, Any]:
    """観光 Spot を JSON 用 dict に変換する。"""
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


def _tour_dataset_json(d) -> dict[str, Any]:
    """観光 TourismDataset を JSON 用 dict に変換する。"""
    return {
        "id": d.id,
        "name": d.name,
        "title": d.title,
        "description": d.description,
        "license": d.license,
        "license_url": d.license_url,
        "updated_at": d.updated_at,
        "url": d.url,
        "resources": list(d.resources),
        "source": d.source,
        "source_url": d.source_url,
    }


def _tour_stat_json(s) -> dict[str, Any]:
    """観光 TourismStat（入込客数）を JSON 用 dict に変換する。"""
    return {
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


def _kind_json(k: WarningKind) -> dict[str, Any]:
    """警報・注意報の種別 1 件を JSON 用 dict に変換する。"""
    return {
        "name": k.name,
        "code": k.code,
        "status": k.status,
    }


def _area_json(a) -> dict[str, Any]:
    """警報・注意報の対象地域 1 件を JSON 用 dict に変換する。"""
    return {
        "name": a.name,
        "code": a.code,
        "kinds": [_kind_json(k) for k in a.kinds],
        "status_summary": a.status_summary,
    }


def _level_json(lv) -> dict[str, Any]:
    """警報・注意報の階層 1 つ（府県/一次細分/地域/市町村）を JSON 用 dict に変換する。"""
    return {
        "level": lv.level,
        "type_label": lv.type_label,
        "areas": [_area_json(a) for a in lv.areas],
    }


# ---------------------------------------------------------------------------
# MCP ツール定義
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "nic",
    instructions=(
        "新潟県の情報（気象・統計・観光・防災・交通・オープンデータ）を提供する MCP サーバー。"
        "アメダス観測所のコードは 5 桁数字（例: 54232=新潟, 54841=湯沢）。"
        "ツールのレスポンスには必ず出典（source / source_url）が含まれるため、"
        "回答の際は出典を明記すること。"
    ),
)


@mcp.tool(
    name="get_snow_info",
    description=(
        "新潟県内のアメダス観測所の現在の積雪深（cm）を取得する。"
        "観測所コードを指定しない場合は県内全 44 観測所、"
        "指定した場合はその観測所のみを返す。"
        "積雪データは気象庁が冬季のみ提供しており、夏季（概ね5〜9月）はエラーになる。"
    ),
)
def get_snow_info(
    station_codes: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    force: bool = False,
) -> dict[str, Any]:
    """積雪情報を取得する（気象庁アメダス）。

    Args:
        station_codes: 観測所番号のリスト（例: ["54841", "54232"]）。
            None または空リストなら新潟県内の全観測所。
        limit: 返す観測所の最大件数（積雪の多い順）。
        force: True ならキャッシュを無視して再取得。

    Returns:
        積雪観測値のリスト（出典付き）。
    """
    codes = station_codes or None
    with AmedasClient(ttl=MCP_TTL) as client:
        data = client.fetch(AmedasElement.SNOW, codes=codes, force=force)
    observations = sorted(
        (o for o in data.observations if o.value is not None),
        key=lambda o: o.value,  # type: ignore[arg-type]
        reverse=True,
    )
    return {
        "element": "snow",
        "unit": "cm",
        "observations": [
            {**_observation_json(o), "rank": idx + 1}
            for idx, o in enumerate(observations[:limit])
        ],
        "fetched_at": _format_utc(data.fetched_at),
        "source": AMEDAS_SOURCE,
        "source_url": "https://www.data.jma.go.jp/stats/data/mdrr/docs/csv_dl_readme.html",
    }


@mcp.tool(
    name="get_weather_info",
    description=(
        "新潟県内のアメダス観測所の気温（当日の最高・最低）と 1 時間降水量を取得する。"
        "観測所コードを指定しない場合は新潟県内の全観測所、"
        "指定した場合はその観測所のみを返す。"
    ),
)
def get_weather_info(
    station_codes: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    force: bool = False,
) -> dict[str, Any]:
    """気温・降水量を取得する（気象庁アメダス）。

    Args:
        station_codes: 観測所番号のリスト（例: ["54232"]）。
            None または空リストなら新潟県内の全観測所。
        limit: 返す観測所の最大件数。
        force: True ならキャッシュを無視して再取得。

    Returns:
        観測所ごとの最高気温・最低気温・1時間降水量（出典付き）。
    """
    codes = station_codes or None
    elements = [
        AmedasElement.MAX_TEMP,
        AmedasElement.MIN_TEMP,
        AmedasElement.PRECIPITATION,
    ]
    with AmedasClient(ttl=MCP_TTL) as client:
        datas = [client.fetch(e, codes=codes, force=force) for e in elements]

    by_code: dict[str, dict[str, Any]] = {}
    for data in datas:
        for obs in data.observations:
            by_code.setdefault(obs.station.code, {})[data.element.value] = obs

    records: list[dict[str, Any]] = []
    for code, obs_map in by_code.items():
        rec: dict[str, Any] = {"station_code": code}
        first_obs = next(iter(obs_map.values()), None)
        if first_obs is not None:
            rec["station_name"] = first_obs.station.name
        for e in elements:
            obs = obs_map.get(e.value)
            rec[e.value] = obs.value if obs is not None else None
        records.append(rec)

    return {
        "element": "temperature_precipitation",
        "unit": {
            "max_temp": "℃",
            "min_temp": "℃",
            "precipitation": "mm",
        },
        "records": records[:limit],
        "fetched_at": _format_utc(datas[0].fetched_at),
        "source": AMEDAS_SOURCE,
        "source_url": "https://www.data.jma.go.jp/stats/data/mdrr/docs/csv_dl_readme.html",
    }


@mcp.tool(
    name="get_niigata_stats",
    description=(
        "新潟県の統計・オープンデータを取得する。"
        "data_type で内容を選択する:\n"
        "- \"datasets\": オープンデータカタログのデータセット一覧（query / category / "
        "data_format で絞り込み可）\n"
        "- \"population\": 人口時系列データ（市町村別、municipality で市町村名を絞り込み可）\n"
        "- \"michinoeki\": 道の駅一覧（駅名・路線名・所在地・電話番号）"
    ),
)
def get_niigata_stats(
    data_type: str = "datasets",
    query: str | None = None,
    category: str | None = None,
    data_format: str | None = None,
    municipality: str | None = None,
    limit: int = DEFAULT_LIMIT,
    force: bool = False,
) -> dict[str, Any]:
    """統計・オープンデータを取得する（新潟県オープンデータ）。

    Args:
        data_type: "datasets"（データセット一覧）/ "population"（人口）/
            "michinoeki"（道の駅）。
        query: データセット名・概要のキーワード検索（data_type="datasets" 時）。
        category: データセットの分類（内容）で絞り込み（例: "運輸・観光"）。
        data_format: データセットの形式で絞り込み（例: "CSV", "Excel"）。
        municipality: 人口データの市町村名で絞り込み（例: "新潟市"）。
        limit: 返す最大件数。
        force: True ならキャッシュを無視して再取得。

    Returns:
        選択した data_type に応じたデータ（出典付き）。

    Raises:
        ValueError: data_type が不正な場合。
    """
    with OpenDataClient(ttl=MCP_TTL) as client:
        if data_type == "population":
            records = client.get_population(
                municipality=municipality, force=force
            )
            return {
                "type": "population",
                "records": [_population_json(r) for r in records[:limit]],
                "source": records[0].source if records else None,
                "source_url": records[0].source_url if records else None,
                "warnings": _opendata_warnings(client),
            }
        if data_type == "michinoeki":
            stations = client.get_tourism(force=force)
            return {
                "type": "michinoeki",
                "stations": [_michinoeki_json(s) for s in stations[:limit]],
                "source": stations[0].source if stations else None,
                "source_url": stations[0].source_url if stations else None,
                "warnings": _opendata_warnings(client),
            }
        if data_type != "datasets":
            raise ValueError(
                f"data_type は 'datasets' / 'population' / 'michinoeki' のいずれかを指定してください (got: {data_type!r})"
            )
        datasets = client.get_datasets(
            query=query, category=category, data_format=data_format, force=force
        )
        return {
            "type": "datasets",
            "datasets": [_dataset_json(d) for d in datasets[:limit]],
            "count": len(datasets),
            "source": datasets[0].source if datasets else None,
            "source_url": datasets[0].source_url if datasets else None,
            "warnings": _opendata_warnings(client),
        }


@mcp.tool(
    name="search_niigata_data",
    description=(
        "新潟県の全データをキーワードで横断検索する。"
        "対象は アメダス観測所（名前・コード）/ 人口（市町村名）/ 道の駅（駅名・所在地）/ "
        "オープンデータデータセット（名前・分類・概要）の 4 種類。"
        "例: \"湯沢\", \"新潟市\", \"道の駅\", \"観光\"。"
    ),
)
def search_niigata_data(keyword: str, limit: int = DEFAULT_LIMIT, force: bool = False) -> dict[str, Any]:
    """全データを横断検索する。

    Args:
        keyword: 検索キーワード（観測所名・市町村名・データ名など）。
        limit: カテゴリごとの最大表示件数。
        force: True ならキャッシュを無視して再取得。

    Returns:
        カテゴリ（観測所 / 人口 / 道の駅 / データセット）ごとのヒット一覧。
    """
    results: dict[str, Any] = {}

    # 1. 観測所（アメダス）: 名前・番号の部分一致
    stations_hit: list[dict[str, Any]] = []
    with AmedasClient(ttl=MCP_TTL) as aclient:
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
    results["stations"] = stations_hit[:limit]

    # 2. オープンデータ（人口・道の駅・データセット）
    with OpenDataClient(ttl=MCP_TTL) as oclient:
        try:
            pop = oclient.get_population(force=force)
            results["population"] = [
                _population_json(r)
                for r in pop
                if keyword in r.municipality_name or keyword in r.municipality_code
            ][:limit]
        except OpenDataError as e:
            results["population_error"] = str(e)

        try:
            michi = oclient.get_tourism(force=force)
            results["michinoeki"] = [
                _michinoeki_json(s)
                for s in michi
                if keyword in s.name or keyword in s.address
            ][:limit]
        except OpenDataError as e:
            results["michinoeki_error"] = str(e)

        try:
            ds = oclient.get_datasets(force=force)
            results["datasets"] = [
                _dataset_json(d)
                for d in ds
                if keyword in d.name
                or keyword in d.category
                or keyword in d.description
                or keyword in d.fields
            ][:limit]
        except OpenDataError as e:
            results["datasets_error"] = str(e)

        warnings = _opendata_warnings(oclient)

    return {
        "keyword": keyword,
        "stations": results.get("stations", []),
        "population": results.get("population", []),
        "michinoeki": results.get("michinoeki", []),
        "datasets": results.get("datasets", []),
        "errors": [
            results[k]
            for k in ("population_error", "michinoeki_error", "datasets_error")
            if k in results
        ],
        "warnings": warnings,
    }


@mcp.tool(
    name="get_tourist_spots",
    description=(
        "新潟県内の観光スポット一覧を取得する。"
        "対象は 温泉（新潟市 GIS 温泉利用許可施設、泉質・緯度経度付き）と "
        "集客施設（国土数値情報 P33 映画館・公会堂・劇場等、2014年度版）の 2 系統。"
        "category で区分（例: \"温泉\", \"集客施設（映画館）\"）を、"
        "keyword でスポット名・住所・説明の部分一致検索ができる。"
        "データ源が取得できない場合は内蔵サンプルデータにフォールバックする。"
    ),
)
def get_tourist_spots(
    category: str | None = None,
    keyword: str | None = None,
    limit: int = DEFAULT_LIMIT,
    force: bool = False,
) -> dict[str, Any]:
    """観光スポット一覧を取得する（新潟市オープンデータ / 国土数値情報）。

    Args:
        category: スポット区分で絞り込み（例: "温泉", "集客施設（映画館）"）。
            None なら全区分。
        keyword: スポット名・住所・説明への部分一致検索語。None なら全件。
        limit: 返すスポットの最大件数。
        force: True ならキャッシュを無視して再取得。

    Returns:
        スポット一覧（出典付き）。データ源のフォールバック状況は warnings に含まれる。
    """
    with TourismClient(ttl=MCP_TTL) as client:
        spots = client.get_spots(category=category, force=force)
    if keyword:
        kw = keyword.strip()
        spots = [
            s
            for s in spots
            if kw in s.name or kw in s.address or kw in s.description or kw in s.category
        ]
    return {
        "type": "tourist_spots",
        "spots": [_spot_json(s) for s in spots[:limit]],
        "count": len(spots),
        "source": TOURISM_SOURCE,
        "source_url": TOURISM_SOURCE_URL,
        "warnings": _tourism_warnings(client),
    }


@mcp.tool(
    name="get_tour_recommendation",
    description=(
        "おすすめ観光ルート・観光スポットの推薦情報を取得する。"
        "新潟県内の観光スポット（温泉・集客施設）から選抜し、"
        "観光入込客数の多い年・市町村の傾向、および"
        "対象地域（area 指定時）の当日雨情報（アメダス1時間降水量）を組み合わせて返す。"
        "area には地域名（例: \"十日町市\"）または観測所名（例: \"湯沢\"）を指定できる。"
    ),
)
def get_tour_recommendation(
    area: str | None = None,
    limit: int = DEFAULT_LIMIT,
    force: bool = False,
) -> dict[str, Any]:
    """おすすめ観光ルート・スポットを生成する（観光 + 入込客数 + 雨情報）。

    Args:
        area: 対象地域名（スポット住所 / 観測所名への部分一致）。None なら県内全域。
        limit: 返すスポットの最大件数。
        force: True ならキャッシュを無視して再取得。

    Returns:
        推薦スポット・入込客数傾向・雨情報（出典付き）。
    """
    with TourismClient(ttl=MCP_TTL) as tourism:
        spots = tourism.get_spots(force=force)
        stats = tourism.get_irikomi(force=force)
        tourism_warnings = _tourism_warnings(tourism)

    if area:
        area_kw = area.strip()
        spots = [
            s
            for s in spots
            if area_kw in s.name
            or area_kw in s.address
            or area_kw in s.description
            or area_kw in s.category
        ]

    # 推定点数: カテゴリ情報量（description の長さ）で安定して並べる
    # （推薦結果が実行ごとに変わる乱数ソートはユーザーを誤解させるため不使用）
    scored = sorted(
        spots,
        key=lambda s: (len(s.description or ""), s.name),
        reverse=True,
    )

    # 雨情報: area を観測所名（またはコード）として解決し、アメダス1時間降水量を取得
    # （取得に失敗しても推薦自体は返す）
    rain: dict[str, Any] = {"precipitation": None, "note": None}
    station: Any = None
    if area:
        with AmedasClient(ttl=MCP_TTL) as amedas:
            station = _resolve_station(amedas, area)
            try:
                codes = [station.code] if station is not None else [area]
                data = amedas.fetch(
                    AmedasElement.PRECIPITATION, codes=codes, force=force
                )
                obs = [o for o in data.observations if o.value is not None]
                if obs:
                    rain = {
                        "station_code": obs[0].station.code,
                        "station_name": obs[0].station.name,
                        "precipitation": obs[0].value,
                        "unit": "mm",
                        "observed_at": _format_utc(obs[0].observed_at),
                        "source": AMEDAS_SOURCE,
                        "source_url": "https://www.data.jma.go.jp/stats/data/mdrr/docs/csv_dl_readme.html",
                    }
                else:
                    rain["note"] = (
                        f"{area} に該当する観測所の観測値がありません。"
                        "雨情報なし（アメダス観測所例: 新潟=54232, 湯沢=54841）"
                    )
            except AmedasError as e:
                # 観測所コード以外の地域名（市町村名等）は雨情報なしで続行
                rain["note"] = f"雨情報を取得できませんでした（{e}）"

    # 雨が降っている場合、観測所の周辺スポットを上位に持ってくる
    # （area 指定時のみ。座標があるスポットと観測所の距離で並べ替え）
    if station is not None and rain.get("precipitation") is not None and rain["precipitation"] >= 1.0:
        try:
            scored = sorted(
                scored,
                key=lambda s: (
                    _haversine_km(station.lat, station.lon, s.lat, s.lon)
                    if s.lat is not None and s.lon is not None
                    else 1e9
                ),
            )
        except Exception:
            pass  # 距離計算ができなくても推薦自体は返す

    stats_sorted = sorted(stats, key=lambda s: s.year, reverse=True)
    latest = stats_sorted[0] if stats_sorted else None
    busiest = max(
        stats, key=lambda s: s.total or 0, default=None
    )

    return {
        "type": "tour_recommendation",
        "area": area,
        "spots": [_spot_json(s) for s in scored[:limit]],
        "stats": {
            "latest_year": latest.year if latest else None,
            "latest_total": latest.total if latest else None,
            "busiest_year": busiest.year if busiest else None,
            "busiest_total": busiest.total if busiest else None,
            "records": [_tour_stat_json(s) for s in stats_sorted[:5]],
            "source": latest.source if latest else TOURISM_SOURCE,
            "source_url": latest.source_url if latest else TOURISM_SOURCE_URL,
        },
        "rain": rain,
        "source": TOURISM_SOURCE,
        "source_url": TOURISM_SOURCE_URL,
        "warnings": tourism_warnings,
    }


def _resolve_station(amedas: AmedasClient, area: str) -> Any | None:
    """地域名・観測所名・観測所コードからアメダス観測所を解決する。

    area が観測所コード（例: "54841"）ならその観測所、
    観測所名（例: "湯沢"）の部分一致なら該当観測所、
    どちらでもなければ None（雨情報なしで続行）。
    """
    area_kw = area.strip()
    for st in amedas.get_stations():
        if st.code == area_kw or area_kw in st.name:
            return st
    return None


def _haversine_km(lat1: float, lon1: float, lat2: float | None, lon2: float | None) -> float:
    """2 地点間の大円距離（km）。座標不明時は巨大値を返す。"""
    import math

    if lat2 is None or lon2 is None:
        return 1e9
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


@mcp.tool(
    name="get_warning_info",
    description=(
        "新潟県（府県コード 150000）の現在の警報・注意報を取得する。"
        "気象庁防災情報XML（VPWW53/VPWW54 電文）から、"
        "府県 → 一次細分区域 → 市町村等をまとめた地域 → 市町村 の 4 階層それぞれの"
        "警報・注意報の種別（大雨・雷・波浪など）と状態（発表/継続/解除）を返す。"
        "level で階層を絞り込み（\"府県\" / \"一次細分\" / \"地域\" / \"市町村\"）、"
        "active_only=True で発表のある地域のみに絞り込める。"
    ),
)
def get_warning_info(
    level: str = "府県",
    active_only: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """新潟県の警報・注意報を取得する（気象庁防災情報XML）。

    Args:
        level: 階層名（"府県" / "一次細分" / "地域" / "市町村"）。
            デフォルト "府県"（県全体の発表種別一覧）。
        active_only: True なら警報・注意報が発表されている地域のみ返す。
        force: True ならキャッシュを無視して再取得。

    Returns:
        指定階層の警報・注意報（4 階層全件 + 出典付き）。

    Raises:
        ValueError: level が不正な場合。
    """
    valid_levels = ("府県", "一次細分", "地域", "市町村")
    if level not in valid_levels:
        raise ValueError(
            f"level は {'/'.join(valid_levels)} のいずれかを指定してください (got: {level!r})"
        )
    with WarningClient(ttl=MCP_WARNING_TTL) as client:
        data = client.fetch(force=force)
    target = data.level(level)
    areas = target.active_areas if (target is not None and active_only) else (
        target.areas if target is not None else ()
    )
    return {
        "prefecture_code": NIIGATA_PREF_CODE,
        "level": level,
        "active_only": active_only,
        "title": data.title,
        "headline": data.headline,
        "info_type": data.info_type,
        "report_datetime": _format_utc(data.report_datetime),
        "editorial_office": data.editorial_office,
        "message_kind": data.message_kind,
        "message_url": data.message_url,
        "summary": data.summary,
        "levels": [_level_json(lv) for lv in data.levels],
        "areas": [_area_json(a) for a in areas],
        "source": WARNING_SOURCE,
        "source_url": WARNING_SOURCE_URL,
    }


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def main() -> None:
    """MCP サーバーを stdio トランスポートで起動する。"""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
