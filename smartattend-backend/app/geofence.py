"""
Campus boundary geofencing.
Reuses your 16-point polygon idea from SCEMS, plus GPS-noise handling.
"""
import math
from typing import List, Tuple
from .schemas import GPSReading

# TEMPORARY test boundary — ~150m box around a test coordinate the user
# provided (31.490827, 74.4080266) for end-to-end testing. Replace with the
# real 16-point campus boundary once available.
CAMPUS_BOUNDARY: List[Tuple[float, float]] = [
    (31.492174, 74.406446),
    (31.492174, 74.409607),
    (31.489480, 74.409607),
    (31.489480, 74.406446),
]

MAX_ACCEPTABLE_ACCURACY_M = 30.0  # reject readings noisier than this
BOUNDARY_BUFFER_M = 15.0  # treat points within this distance of edge as "inside" too


def pick_best_reading(readings: List[GPSReading]) -> GPSReading:
    """Pick lowest-accuracy-value (most precise) reading from a batch."""
    if not readings:
        raise ValueError("No GPS readings provided")
    return min(readings, key=lambda r: r.accuracy)


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Distance in meters between two lat/lng points."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def point_in_polygon(lat: float, lng: float, polygon: List[Tuple[float, float]]) -> bool:
    """Standard ray-casting point-in-polygon check."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        lat_i, lng_i = polygon[i]
        lat_j, lng_j = polygon[j]
        intersect = ((lng_i > lng) != (lng_j > lng)) and (
            lat < (lat_j - lat_i) * (lng - lng_i) / (lng_j - lng_i + 1e-15) + lat_i
        )
        if intersect:
            inside = not inside
        j = i
    return inside


def distance_to_polygon_edge_m(lat: float, lng: float, polygon: List[Tuple[float, float]]) -> float:
    """Shortest distance from point to any polygon edge, in meters."""
    min_dist = float("inf")
    n = len(polygon)
    for i in range(n):
        lat1, lng1 = polygon[i]
        lat2, lng2 = polygon[(i + 1) % n]
        # approximate by checking distance to segment endpoints + midpoint (good enough at campus scale)
        for lat_p, lng_p in [(lat1, lng1), (lat2, lng2), ((lat1 + lat2) / 2, (lng1 + lng2) / 2)]:
            d = haversine_m(lat, lng, lat_p, lng_p)
            min_dist = min(min_dist, d)
    return min_dist


def check_location(reading: GPSReading) -> dict:
    """
    Returns dict: {allowed: bool, reason: str, distance_to_boundary_m: float}
    Logic:
      1. Reject if GPS accuracy too poor to trust.
      2. Accept if strictly inside polygon.
      3. If outside but within BOUNDARY_BUFFER_M of an edge, accept (accounts for GPS drift).
      4. Otherwise reject.
    """
    if reading.accuracy > MAX_ACCEPTABLE_ACCURACY_M:
        return {
            "allowed": False,
            "reason": f"gps_too_noisy ({reading.accuracy:.0f}m > {MAX_ACCEPTABLE_ACCURACY_M:.0f}m)",
            "distance_to_boundary_m": None,
        }

    inside = point_in_polygon(reading.latitude, reading.longitude, CAMPUS_BOUNDARY)
    dist = distance_to_polygon_edge_m(reading.latitude, reading.longitude, CAMPUS_BOUNDARY)

    if inside:
        return {"allowed": True, "reason": "inside_boundary", "distance_to_boundary_m": dist}

    if dist <= BOUNDARY_BUFFER_M:
        return {"allowed": True, "reason": "within_buffer_zone", "distance_to_boundary_m": dist}

    return {"allowed": False, "reason": "outside_boundary", "distance_to_boundary_m": dist}
