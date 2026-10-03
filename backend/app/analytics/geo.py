"""Geospatial analytics.

Pure-numpy implementations are always available. When PostgreSQL has PostGIS,
`SqlStore` exposes the same operations as SQL (ST_DWithin / ST_Distance on
geography) for set-based queries over the full table; the functions here are
used on already-retrieved, customer-scoped frames.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.analytics.stats import epoch_seconds

EARTH_RADIUS_KM = 6371.0088
PHYSICAL_CHANNELS = {"pos", "atm", "branch"}


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km. Accepts scalars or numpy arrays."""
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(x, dtype=float)) for x in (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def calculate_transaction_distance(t1: dict, t2: dict) -> float | None:
    """Distance in km between two transactions that both carry coordinates."""
    if any(t.get(k) is None or pd.isna(t.get(k)) for t in (t1, t2) for k in ("latitude", "longitude")):
        return None
    return float(haversine_km(t1["latitude"], t1["longitude"], t2["latitude"], t2["longitude"]))


def find_transactions_near_location(tx: pd.DataFrame, lat: float, lon: float, radius_km: float) -> pd.DataFrame:
    geo = tx.dropna(subset=["latitude", "longitude"])
    if geo.empty:
        return geo.assign(distance_km=pd.Series(dtype=float))
    d = haversine_km(lat, lon, geo.latitude.to_numpy(), geo.longitude.to_numpy())
    out = geo.assign(distance_km=d)
    return out[out.distance_km <= radius_km].sort_values("distance_km")


@dataclass
class TravelViolation:
    from_txn: str
    to_txn: str
    distance_km: float
    hours: float
    speed_kmh: float


def detect_impossible_travel(tx: pd.DataFrame, max_speed_kmh: float = 900.0, min_distance_km: float = 500.0,
                             physical_only: bool = True) -> list[TravelViolation]:
    """Consecutive geolocated transactions whose implied speed exceeds `max_speed_kmh`.

    Digital channels are excluded by default: their coordinates come from IP
    geolocation, which VPNs and mobile carriers make unreliable.
    """
    geo = tx.dropna(subset=["latitude", "longitude"])
    if physical_only:
        geo = geo[geo.channel.isin(PHYSICAL_CHANNELS)]
    geo = geo.sort_values("timestamp")
    if len(geo) < 2:
        return []
    lat = geo.latitude.to_numpy()
    lon = geo.longitude.to_numpy()
    ts = epoch_seconds(geo.timestamp)
    ids = geo.transaction_id.to_numpy()
    dist = haversine_km(lat[:-1], lon[:-1], lat[1:], lon[1:])
    hours = np.maximum((ts[1:] - ts[:-1]) / 3600.0, 1 / 60)  # floor at one minute
    speed = dist / hours
    out = []
    for i in np.where((speed > max_speed_kmh) & (dist >= min_distance_km))[0]:
        out.append(TravelViolation(str(ids[i]), str(ids[i + 1]), round(float(dist[i]), 1),
                                   round(float(hours[i]), 2), round(float(speed[i]), 0)))
    return out


def detect_geographic_outliers(tx: pd.DataFrame, home_lat: float | None, home_lon: float | None,
                               baseline: pd.DataFrame | None = None, quantile: float = 0.99,
                               min_km: float = 300.0) -> pd.DataFrame:
    """Transactions farther from home than the customer's own baseline distance profile."""
    geo = tx.dropna(subset=["latitude", "longitude"])
    if geo.empty or home_lat is None or home_lon is None:
        return geo.iloc[0:0]
    d = haversine_km(home_lat, home_lon, geo.latitude.to_numpy(), geo.longitude.to_numpy())
    geo = geo.assign(distance_from_home_km=np.round(d, 1))
    limit = min_km
    if baseline is not None:
        b = baseline.dropna(subset=["latitude", "longitude"])
        if len(b) >= 10:
            bd = haversine_km(home_lat, home_lon, b.latitude.to_numpy(), b.longitude.to_numpy())
            limit = max(min_km, float(np.quantile(bd, quantile)) * 1.5)
    return geo[geo.distance_from_home_km > limit]
