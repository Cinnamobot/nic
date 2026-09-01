"""nic.core: 新潟データ取得層。

キャッシュ・エラー処理・出典管理を一元化するコアモジュール。
CLI と MCP の両インターフェースから共通利用される。

現在の実装:
- amedas: 気象庁アメダス（新潟県）の積雪・気温・降水量取得
- opendata: 新潟県オープンデータ（データセット一覧・人口・道の駅）
- tourism: 新潟県観光（新潟市 CKAN・入込客数・温泉 GIS・国土数値情報 P33）
"""

from nic.core.amedas import (
    AmedasClient,
    AmedasData,
    AmedasElement,
    AmedasError,
    AmedasFetchError,
    AmedasParseError,
    AmedasStationNotFoundError,
    NIIGATA_STATIONS,
    Observation,
    SOURCE_TEXT,
    Station,
)
from nic.core.tourism import (
    CKAN_BASE_URL,
    IRIKOMI_CSV_URL,
    ONSEN_CSV_URL,
    P33_SOURCE_TEXT,
    P33_ZIP_URL,
    SOURCE_TEXT as TOURISM_SOURCE_TEXT,
    Spot,
    TourismClient,
    TourismDataset,
    TourismError,
    TourismFetchError,
    TourismNotFoundError,
    TourismParseError,
    TourismStat,
    get_tourism_datasets,
    get_tourism_spots,
    get_tourism_stats,
)

__all__ = [
    # amedas
    "AmedasClient",
    "AmedasData",
    "AmedasElement",
    "AmedasError",
    "AmedasFetchError",
    "AmedasParseError",
    "AmedasStationNotFoundError",
    "NIIGATA_STATIONS",
    "Observation",
    "SOURCE_TEXT",
    "Station",
    # tourism
    "CKAN_BASE_URL",
    "IRIKOMI_CSV_URL",
    "ONSEN_CSV_URL",
    "P33_SOURCE_TEXT",
    "P33_ZIP_URL",
    "TOURISM_SOURCE_TEXT",
    "Spot",
    "TourismClient",
    "TourismDataset",
    "TourismError",
    "TourismFetchError",
    "TourismNotFoundError",
    "TourismParseError",
    "TourismStat",
    "get_tourism_datasets",
    "get_tourism_spots",
    "get_tourism_stats",
]
