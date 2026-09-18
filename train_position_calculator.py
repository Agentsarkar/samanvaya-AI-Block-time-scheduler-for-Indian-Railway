#!/usr/bin/env python3
"""
train_position_calculator.py — Precision Track-Following Train Position Algorithm
================================================================================
SAMANVAY Rail Decision Support System (DSS)

ALGORITHM SPECIFICATION (As per Railway Operations Requirement):
----------------------------------------------------------------
1. Inputs:
   - Origin Station (e.g. SDAH / Sealdah) with scheduled departure time T_dep (e.g. 12:00)
   - Destination Station (e.g. KNJ / Krishnanagar) with scheduled arrival time T_arr (e.g. 16:00)
   - Current Simulation/Evaluation Time T_now (e.g. 15:00)
   - Corridor route network containing all intermediate station coordinates (from stationloc.txt / corridor_network.json)
     and the true curved track polyline drawn on the GIS map.

2. Distance Calculation Across Drawn Track Path:
   - Never use Euclidean straight-line distance (which cuts through fields/rivers off-track).
   - Find all intermediate stations in sequence between Origin and Destination.
   - Extract the polyline coordinates connecting these stations along the corridor.
   - Calculate cumulative Haversine distance along each track segment:
       d = 2 * R * arcsin( sqrt( sin²(Δlat/2) + cos(lat1)*cos(lat2)*sin²(Δlon/2) ) )
   - Sum to find Total Track Distance D_total (in km).

3. Block Time & Estimated Speed:
   - Block Time ΔT = T_arr - T_dep (in hours / minutes).
   - Average Speed V = D_total / ΔT (in km/h).

4. Estimated Position at Current Time T_now:
   - Elapsed Time t_elapsed = T_now - T_dep (in hours / minutes).
   - If 0 <= t_elapsed <= ΔT:
       - Distance covered from base/origin: D_covered = V * t_elapsed = (t_elapsed / ΔT) * D_total (km).
       - Distance remaining to destination: D_remaining = D_total - D_covered (km).
       - Journey progress fraction: f = D_covered / D_total (0.0 to 1.0).
   - Walk the curved polyline to find the exact coordinate (lat, lon) at distance D_covered.
   - Identify the current intermediate section (e.g. "between station X and station Y").
   - Apply lateral parallel track offset (±0.00018°) so Up and Down trains do not overlap.

Usage:
  # Standalone CLI demonstration:
  python train_position_calculator.py --example
  python train_position_calculator.py --origin SDAH --destination KNJ --dep 12:00 --arr 16:00 --now 15:00
  python train_position_calculator.py --train 13038 --now 02:55
  python train_position_calculator.py --corridor hwh_bwn_main --now 08:30
"""

import os
import sys
import json
import math
import sqlite3
import argparse
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

BASE_DIR = Path(__file__).resolve().parent
STATIONLOC_FILE = BASE_DIR / "stationloc.txt"
CORRIDORS_FILE = BASE_DIR / "corridor_network.json"
DB_FILE = BASE_DIR / "train_cache.db"

EARTH_RADIUS_KM = 6371.0


# ─── 1. GEOMETRY & HAVERSINE HELPERS ─────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Computes great-circle distance between two points in kilometers."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (math.sin(dphi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * (math.sin(dlambda / 2.0) ** 2))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return EARTH_RADIUS_KM * c


def polyline_length_km(coords: List[List[float]]) -> float:
    """Calculates the total length of a polyline [[lat, lon], ...] in km."""
    if len(coords) < 2:
        return 0.0
    total = 0.0
    for i in range(len(coords) - 1):
        total += haversine_km(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
    return total


def cumulative_distances(coords: List[List[float]]) -> List[float]:
    """Returns list of cumulative distances [0.0, d1, d2, ... total_km] for coords."""
    if not coords:
        return []
    dists = [0.0]
    for i in range(len(coords) - 1):
        step = haversine_km(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
        dists.append(dists[-1] + step)
    return dists


def interpolate_along_polyline(
    coords: List[List[float]],
    dists: List[float],
    target_km: float,
    offset_lateral_deg: float = 0.0
) -> Tuple[float, float, int]:
    """
    Finds the exact (lat, lon) along a polyline at target_km distance from start.
    Optionally applies a perpendicular offset (offset_lateral_deg) for dual-track separation.
    Returns (lat, lon, segment_index).
    """
    total_km = dists[-1] if dists else 0.0
    target_km = max(0.0, min(total_km, target_km))

    seg_idx = 0
    seg_alpha = 0.0

    for i in range(len(dists) - 1):
        if dists[i] <= target_km <= dists[i + 1]:
            seg_dist = dists[i + 1] - dists[i]
            seg_alpha = (target_km - dists[i]) / seg_dist if seg_dist > 0 else 0.0
            lat = coords[i][0] + (coords[i + 1][0] - coords[i][0]) * seg_alpha
            lon = coords[i][1] + (coords[i + 1][1] - coords[i][1]) * seg_alpha
            seg_idx = i
            break
    else:
        lat, lon = coords[-1][0], coords[-1][1]
        seg_idx = max(0, len(coords) - 2)

    # Perpendicular lateral offset for parallel track rendering
    if abs(offset_lateral_deg) > 1e-7 and len(coords) >= 2:
        p1 = coords[seg_idx]
        p2 = coords[min(seg_idx + 1, len(coords) - 1)]
        dlat = p2[0] - p1[0]
        dlon = p2[1] - p1[1]
        length = math.sqrt(dlat * dlat + dlon * dlon) or 1.0
        # Normal vector perpendicular to track direction
        n_lat = -dlon / length
        n_lon = dlat / length
        lat += n_lat * offset_lateral_deg
        lon += n_lon * offset_lateral_deg

    return lat, lon, seg_idx


# ─── 2. TIME PARSING & ARITHMETIC ────────────────────────────────────────────

def parse_hhmm(s: Optional[str]) -> Optional[int]:
    """Converts 'HH:MM' string to minutes from midnight (0..1439)."""
    if not s or s in ("None", "--", "N/A"):
        return None
    try:
        parts = str(s).strip().split(":")
        h = int(parts[0])
        m = int(parts[1])
        return (h * 60 + m) % 1440
    except Exception:
        return None


def format_hhmm(minutes: int) -> str:
    """Converts minutes from midnight into 'HH:MM' string."""
    m_clean = int((minutes % 1440 + 1440) % 1440)
    return f"{m_clean // 60:02d}:{m_clean % 60:02d}"


def calculate_block_time_minutes(dep_min: int, arr_min: int) -> int:
    """
    Computes total block time in minutes between departure and arrival.
    Handles overnight crossings (e.g. 23:45 to 02:15 = 150 mins).
    """
    if dep_min <= arr_min:
        return arr_min - dep_min
    else:
        # Crosses midnight
        return (arr_min + 1440) - dep_min


def calculate_elapsed_minutes(dep_min: int, arr_min: int, now_min: int) -> Optional[int]:
    """
    Calculates elapsed minutes from departure to now.
    Returns None if the train is not active at now_min.
    """
    if dep_min <= arr_min:
        if dep_min <= now_min <= arr_min:
            return now_min - dep_min
        return None
    else:
        # Crosses midnight
        if now_min >= dep_min:
            return now_min - dep_min
        elif now_min <= arr_min:
            return (now_min + 1440) - dep_min
        return None


# ─── 3. STATION & TRACK DATA LOADERS ──────────────────────────────────────────

_STATION_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
_CORRIDORS_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


def load_stations_from_stationloc() -> Dict[str, Dict[str, Any]]:
    """Loads station lat/lon dictionary from stationloc.txt."""
    global _STATION_CACHE
    if _STATION_CACHE is not None:
        return _STATION_CACHE

    stations: Dict[str, Dict[str, Any]] = {}
    if not STATIONLOC_FILE.exists():
        print(f"[Warning] {STATIONLOC_FILE} not found", file=sys.stderr)
        return stations

    try:
        with open(STATIONLOC_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for feat in data.get("features", []):
                props = feat.get("properties", {})
                code = (props.get("code") or "").strip().upper()
                if not code:
                    continue
                lat = float(props.get("lat") or 0.0)
                lon = float(props.get("long") or 0.0)
                name = props.get("name") or code
                stations[code] = {
                    "code": code,
                    "name": name,
                    "lat": lat,
                    "lon": lon,
                    "state": props.get("state", ""),
                    "zone": props.get("zone", ""),
                }
        _STATION_CACHE = stations
    except Exception as e:
        print(f"[Error] Failed parsing {STATIONLOC_FILE}: {e}", file=sys.stderr)

    return _STATION_CACHE or {}


def load_corridor_network() -> Dict[str, Dict[str, Any]]:
    """
    Loads corridor polylines and intermediate stations from corridor_network.json.
    Precomputes cumulative track distance profiles for each corridor.
    """
    global _CORRIDORS_CACHE
    if _CORRIDORS_CACHE is not None:
        return _CORRIDORS_CACHE

    corridors: Dict[str, Dict[str, Any]] = {}
    if not CORRIDORS_FILE.exists():
        return corridors

    try:
        with open(CORRIDORS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for c in data.get("primary_corridors", []):
                cid = c.get("id")
                coords = c.get("coordinates", [])
                if len(coords) < 2:
                    continue
                dists = cumulative_distances(coords)
                total_km = dists[-1]
                stations = [s.get("code", "").upper() for s in c.get("stations", [])]
                station_km = {}
                for idx, s in enumerate(c.get("stations", [])):
                    code = (s.get("code") or "").upper()
                    if code:
                        station_km[code] = dists[idx] if idx < len(dists) else dists[-1]

                corridors[cid] = {
                    "id": cid,
                    "name": c.get("name", cid),
                    "coords": coords,
                    "dists": dists,
                    "total_km": total_km,
                    "stations": stations,
                    "station_km": station_km,
                    "station_details": c.get("stations", []),
                }
        _CORRIDORS_CACHE = corridors
    except Exception as e:
        print(f"[Error] Failed parsing {CORRIDORS_FILE}: {e}", file=sys.stderr)

    return _CORRIDORS_CACHE or {}


# ─── 4. CORE POSITION CALCULATION ALGORITHM ───────────────────────────────────

def calculate_single_train_position(
    origin_code: str,
    dest_code: str,
    dep_time_str: str,
    arr_time_str: str,
    now_time_str: str,
    corridor_id: Optional[str] = None,
    train_number: str = "DEMO",
    train_name: str = "Demo Express",
    direction: str = "auto"
) -> Dict[str, Any]:
    """
    Core Algorithm Implementation:
    Calculates exact track-following position of a train given its timetable.

    Example from specification:
      Train leaves Sealdah (SDAH) at 12:00, reaches Krishnanagar (KNJ) at 16:00.
      Block Time = 4.0 hrs (240 min).
      Total Track Distance = 98.6 km across intermediate stations.
      Speed = 98.6 km / 4.0 h = 24.65 km/h.
      At 15:00 (elapsed = 3.0 h = 180 min):
        Distance from Sealdah = 73.95 km.
        Distance away from Krishnanagar = 24.65 km.
        Position (lat, lon) interpolated along the exact drawn track polyline.
    """
    dep_min = parse_hhmm(dep_time_str)
    arr_min = parse_hhmm(arr_time_str)
    now_min = parse_hhmm(now_time_str)

    if dep_min is None or arr_min is None or now_min is None:
        return {
            "success": False,
            "error": "Invalid time format. Please provide HH:MM (24-hour).",
            "active": False,
        }

    # 1. Block Time (Duration)
    block_time_mins = calculate_block_time_minutes(dep_min, arr_min)
    if block_time_mins <= 0:
        return {"success": False, "error": "Departure and arrival times cannot be equal.", "active": False}

    block_time_hours = block_time_mins / 60.0

    # 2. Elapsed Time & Transit Check
    elapsed_mins = calculate_elapsed_minutes(dep_min, arr_min, now_min)
    is_active = (elapsed_mins is not None)

    if not is_active:
        return {
            "success": True,
            "active": False,
            "status": "NOT_RUNNING",
            "message": f"Train is not in transit at {now_time_str} (Scheduled: {dep_time_str} -> {arr_time_str})",
            "train_number": train_number,
            "train_name": train_name,
            "origin": origin_code,
            "destination": dest_code,
            "dep_time": dep_time_str,
            "arr_time": arr_time_str,
            "block_time_hours": round(block_time_hours, 2),
        }

    fraction_completed = min(1.0, max(0.0, elapsed_mins / float(block_time_mins)))

    # 3. Corridor Track Selection
    corridors = load_corridor_network()
    track = None

    if corridor_id and corridor_id in corridors:
        track = corridors[corridor_id]
    else:
        # Match corridor automatically based on origin/destination
        pair = {origin_code.upper(), dest_code.upper()}
        if {"SDAH", "KNJ"}.issubset(pair) or "SDAH" in pair or "KNJ" in pair:
            track = corridors.get("sdah_knj_line")
        elif "ASN" in pair:
            track = corridors.get("bly_bwn_asn_trunk") or corridors.get("bwn_asn_trunk")
        elif {"HWH", "SKG"}.issubset(pair):
            track = corridors.get("hwh_skg_line") or corridors.get("hwh_bwn_main")
        elif {"HWH", "BWN"}.issubset(pair):
            track = corridors.get("hwh_bwn_main") or corridors.get("hwh_bwn_chord")
        else:
            # Fallback to first available corridor
            track = next(iter(corridors.values())) if corridors else None

    if not track or not track.get("coords"):
        # Fallback to stationloc.txt straight line if no corridor polyline exists
        stations = load_stations_from_stationloc()
        st1 = stations.get(origin_code.upper(), {"lat": 22.584, "lon": 88.341})
        st2 = stations.get(dest_code.upper(), {"lat": 23.400, "lon": 88.500})
        total_dist_km = haversine_km(st1["lat"], st1["lon"], st2["lat"], st2["lon"])
        speed_kmh = total_dist_km / block_time_hours if block_time_hours > 0 else 50.0
        d_from_origin = fraction_completed * total_dist_km
        d_to_dest = total_dist_km - d_from_origin
        cur_lat = st1["lat"] + (st2["lat"] - st1["lat"]) * fraction_completed
        cur_lon = st1["lon"] + (st2["lon"] - st1["lon"]) * fraction_completed
        return {
            "success": True,
            "active": True,
            "status": "RUNNING_STRAIGHT_FALLBACK",
            "train_number": train_number,
            "train_name": train_name,
            "current_time": now_time_str,
            "origin": origin_code,
            "destination": dest_code,
            "dep_time": dep_time_str,
            "arr_time": arr_time_str,
            "block_time_hours": round(block_time_hours, 2),
            "elapsed_hours": round(elapsed_mins / 60.0, 2),
            "speed_kmh": round(speed_kmh, 1),
            "total_distance_km": round(total_dist_km, 2),
            "distance_from_origin_km": round(d_from_origin, 2),
            "distance_to_destination_km": round(d_to_dest, 2),
            "journey_progress_pct": round(fraction_completed * 100, 1),
            "lat": round(cur_lat, 6),
            "lon": round(cur_lon, 6),
        }

    # 4. Accurate Curved Polyline Distance Calculation
    coords = track["coords"]
    dists = track["dists"]
    total_track_km = track["total_km"]

    # 5. Determine Direction (Up vs Down along polyline)
    # The polyline starts at origin (index 0) and ends at destination (index -1).
    # Check if this train travels forward (0 -> -1) or reversed (-1 -> 0).
    stn_list = track.get("stations", [])
    reversed_dir = False

    if direction == "down":
        reversed_dir = True
    elif direction == "up":
        reversed_dir = False
    elif origin_code.upper() in stn_list and dest_code.upper() in stn_list:
        idx_orig = stn_list.index(origin_code.upper())
        idx_dest = stn_list.index(dest_code.upper())
        reversed_dir = (idx_orig > idx_dest)
    elif origin_code.upper() in ("KNJ", "BWN", "ASN", "SKG"):
        reversed_dir = True

    # 6. Physical Speed Calculation
    avg_speed_kmh = total_track_km / block_time_hours if block_time_hours > 0 else 60.0

    # 7. Distance Along Path
    distance_from_origin_km = fraction_completed * total_track_km
    distance_to_dest_km = total_track_km - distance_from_origin_km

    # Position on polyline:
    target_km = (1.0 - fraction_completed) * total_track_km if reversed_dir else (fraction_completed * total_track_km)

    # Lateral offset for dual-track (+0.00018° vs -0.00018°)
    offset_deg = 0.00018 * (-1.0 if reversed_dir else 1.0)

    lat, lon, seg_idx = interpolate_along_polyline(coords, dists, target_km, offset_deg)

    # 8. Identify Immediate Intermediate Stations Surrounding the Train
    intermediate_info = _find_surrounding_stations(track, target_km, reversed_dir)

    return {
        "success": True,
        "active": True,
        "status": "RUNNING_ON_CORRIDOR",
        "train_number": train_number,
        "train_name": train_name,
        "corridor_id": track["id"],
        "corridor_name": track.get("name", track["id"]),
        "current_time": now_time_str,
        "origin": origin_code,
        "destination": dest_code,
        "departure_time": dep_time_str,
        "arrival_time": arr_time_str,
        "block_time_hours": round(block_time_hours, 2),
        "block_time_minutes": block_time_mins,
        "elapsed_hours": round(elapsed_mins / 60.0, 2),
        "elapsed_minutes": elapsed_mins,
        "speed_kmh": round(avg_speed_kmh, 1),
        "total_track_distance_km": round(total_track_km, 2),
        "distance_from_origin_km": round(distance_from_origin_km, 2),
        "distance_to_destination_km": round(distance_to_dest_km, 2),
        "journey_progress_pct": round(fraction_completed * 100, 1),
        "direction": "DOWN (Inbound)" if reversed_dir else "UP (Outbound)",
        "coordinates": {
            "lat": round(lat, 6),
            "lon": round(lon, 6),
        },
        "intermediate_section": intermediate_info,
    }


def _find_surrounding_stations(track: Dict[str, Any], target_km: float, reversed_dir: bool) -> Dict[str, Any]:
    """Finds which pair of stations the train is currently running between."""
    stns = track.get("station_details", [])
    if len(stns) < 2:
        return {"description": "Along main corridor"}

    coords = track["coords"]
    # Approximate station kilometer distances along track
    stn_kms = []
    for s in stns:
        s_lat = s.get("lat") or 0.0
        s_lon = s.get("long") or 0.0
        # Find closest point on track polyline
        best_d = 999999.0
        best_km = 0.0
        dists = track["dists"]
        for i, pt in enumerate(coords):
            d = haversine_km(s_lat, s_lon, pt[0], pt[1])
            if d < best_d:
                best_d = d
                best_km = dists[i]
        stn_kms.append((best_km, s.get("code", "STN"), s.get("name", "Station")))

    stn_kms.sort(key=lambda x: x[0])

    prev_s = stn_kms[0]
    next_s = stn_kms[-1]

    for i in range(len(stn_kms) - 1):
        if stn_kms[i][0] <= target_km <= stn_kms[i + 1][0]:
            prev_s = stn_kms[i]
            next_s = stn_kms[i + 1]
            break

    return {
        "previous_station": prev_s[1],
        "previous_station_name": prev_s[2],
        "next_station": next_s[1],
        "next_station_name": next_s[2],
        "section": f"{prev_s[1]} -> {next_s[1]}",
        "section_name": f"{prev_s[2]} to {next_s[2]}",
    }


# ─── 5. BATCH CALCULATION FOR ALL DATABASE TRAINS ────────────────────────────

def sort_stops_chronologically(stops: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Determines true direction of travel by comparing forward cumulative hop duration
    against reverse cumulative hop duration across the 24-hour clock.
    Returns (ordered_stops, is_reversed).
    """
    if not stops or len(stops) < 2:
        return stops, False

    valid = [s for s in stops if (s.get("departure") or s.get("arrival")) not in (None, "None", "--")]
    if len(valid) < 2:
        return stops, False

    t_fwd = [parse_hhmm(s.get("departure") or s.get("arrival")) for s in valid]
    if any(t is None for t in t_fwd):
        return valid, False

    dur_fwd = sum((t_fwd[i + 1] - t_fwd[i] + 1440) % 1440 for i in range(len(t_fwd) - 1))

    t_rev = list(reversed(t_fwd))
    dur_rev = sum((t_rev[i + 1] - t_rev[i] + 1440) % 1440 for i in range(len(t_rev) - 1))

    if dur_rev < dur_fwd:
        return list(reversed(valid)), True
    return valid, False


def is_time_between(dep: int, arr: int, now: int) -> bool:
    """Checks if now falls between departure and arrival, handling midnight crossings."""
    if dep <= arr:
        return dep <= now <= arr
    else:
        return now >= dep or now <= arr


def get_elapsed_minutes(dep: int, arr: int, now: int) -> int:
    """Calculates elapsed minutes from dep to now."""
    if dep <= arr:
        return now - dep
    else:
        return (now - dep) if now >= dep else (now + 1440 - dep)


def calculate_train_position_from_stops(
    stops: List[Dict[str, Any]],
    now_time_str: str,
    corridor_id: Optional[str] = None,
    train_number: str = "DEMO",
    train_name: str = "Demo Train"
) -> Dict[str, Any]:
    """
    Master Position Engine with Segment-Level Precision:
    Finds which consecutive station stop pair the train is in at now_time_str.
    Returns exact position and speed, or active: False if journey finished/not started.
    """
    now_min = parse_hhmm(now_time_str)
    if now_min is None:
        return {"success": False, "error": "Invalid current time format", "active": False}

    ordered_stops, is_reversed = sort_stops_chronologically(stops)
    if len(ordered_stops) < 2:
        return {"success": False, "error": "Insufficient valid stops", "active": False}

    journey_origin = ordered_stops[0]
    journey_dest = ordered_stops[-1]
    final_dest_code = (journey_dest.get("station_code") or "").upper()
    orig_origin_code = (journey_origin.get("station_code") or "").upper()

    # Step-by-step segment locator: Find consecutive pair where train is right now
    active_seg = None
    for i in range(len(ordered_stops) - 1):
        s1 = ordered_stops[i]
        s2 = ordered_stops[i + 1]
        dep = parse_hhmm(s1.get("departure") or s1.get("arrival"))
        arr = parse_hhmm(s2.get("arrival") or s2.get("departure"))
        if dep is None or arr is None:
            continue

        if is_time_between(dep, arr, now_min):
            elapsed = get_elapsed_minutes(dep, arr, now_min)
            block = (arr - dep + 1440) % 1440
            if block > 0:
                active_seg = {
                    "from_stn": s1,
                    "to_stn": s2,
                    "dep_min": dep,
                    "arr_min": arr,
                    "elapsed_mins": elapsed,
                    "block_mins": block,
                    "fraction": min(1.0, max(0.0, elapsed / float(block))),
                    "seg_index": i,
                }
                break

    # If now_min is not within any scheduled stop segment, the train is NOT running
    if not active_seg:
        return {
            "success": True,
            "active": False,
            "status": "NOT_RUNNING",
            "message": f"Train {train_number} is not in transit at {now_time_str} (Finished or not yet departed)",
            "train_number": train_number,
            "train_name": train_name,
            "origin": orig_origin_code,
            "destination": final_dest_code,
        }

    s_from = (active_seg["from_stn"].get("station_code") or "").upper()
    s_to = (active_seg["to_stn"].get("station_code") or "").upper()

    # Select appropriate track polyline for this active section
    corridors = load_corridor_network()
    track = None
    if (s_from == "ASN" and s_to == "BWN") or (s_from == "BWN" and s_to == "ASN"):
        track = corridors.get("bwn_asn_trunk")
    elif orig_origin_code in ("SDAH", "KNJ") or final_dest_code in ("SDAH", "KNJ"):
        track = corridors.get("sdah_knj_line")
    elif corridor_id and corridor_id in corridors:
        track = corridors[corridor_id]
    else:
        track = corridors.get("hwh_bwn_chord") or corridors.get("hwh_bwn_main") or next(iter(corridors.values()))

    if not track or not track.get("coords"):
        return {"success": False, "error": "Corridor track not found", "active": False}

    coords = track["coords"]
    dists = track["dists"]
    total_km = track["total_km"]

    station_km_map = track.get("station_km", {})
    km1 = station_km_map.get(s_from)
    km2 = station_km_map.get(s_to)
    if km1 is None or km2 is None:
        if km1 is None:
            km1 = (active_seg["seg_index"] / max(1, len(ordered_stops) - 1)) * total_km
        if km2 is None:
            km2 = ((active_seg["seg_index"] + 1) / max(1, len(ordered_stops) - 1)) * total_km

    fraction = active_seg["fraction"]
    block_mins = active_seg["block_mins"]
    elapsed_mins = active_seg["elapsed_mins"]

    leg_dist_km = abs(km2 - km1)
    leg_block_hours = block_mins / 60.0
    speed_kmh = max(20.0, min(130.0, leg_dist_km / leg_block_hours)) if leg_block_hours > 0 else 50.0

    # Piece-wise interpolation bounded strictly between station km1 and station km2
    target_km = km1 + fraction * (km2 - km1)

    # Direction: Heading towards Howrah or Sealdah if km2 < km1 or destination is HWH/SDAH
    reversed_dir = (km2 < km1) or (final_dest_code in ("HWH", "SDAH") or orig_origin_code in ("KNJ", "BWN", "ASN", "SKG"))
    offset_deg = 0.00018 * (-1.0 if reversed_dir else 1.0)

    lat, lon, seg_idx = interpolate_along_polyline(coords, dists, target_km, offset_deg)

    return {
        "success": True,
        "active": True,
        "status": "RUNNING_ON_CORRIDOR",
        "train_number": train_number,
        "train_name": train_name,
        "corridor_id": track["id"],
        "corridor_name": track.get("name", track["id"]),
        "current_time": now_time_str,
        "origin": s_from,
        "destination": s_to,
        "final_destination": final_dest_code,
        "departure_time": format_hhmm(active_seg["dep_min"]),
        "arrival_time": format_hhmm(active_seg["arr_min"]),
        "block_time_hours": round(block_mins / 60.0, 2),
        "block_time_minutes": block_mins,
        "elapsed_hours": round(elapsed_mins / 60.0, 2),
        "elapsed_minutes": elapsed_mins,
        "speed_kmh": round(speed_kmh, 1),
        "total_track_distance_km": round(total_km, 2),
        "distance_from_origin_km": round(target_km, 2),
        "distance_to_destination_km": round(abs(total_km - target_km), 2),
        "journey_progress_pct": round((target_km / total_km) * 100, 1),
        "direction": "INBOUND (Towards Howrah/Sealdah)" if reversed_dir else "OUTBOUND",
        "coordinates": {
            "lat": round(lat, 6),
            "lon": round(lon, 6),
        },
        "intermediate_section": {
            "section": f"{s_from} -> {s_to}",
            "from": s_from,
            "to": s_to,
            "final_destination": final_dest_code,
        }
    }


def calculate_all_active_corridor_trains(sim_time: str, sim_day: str = "fri") -> List[Dict[str, Any]]:
    """
    Evaluates all corridor trains stored in train_cache.db at sim_time (HH:MM).
    Returns list of active trains with exact track coordinates, train numbers, and train names.
    """
    if not DB_FILE.exists():
        print(f"[Warning] {DB_FILE} does not exist", file=sys.stderr)
        return []

    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM corridor_trains").fetchall()
    conn.close()

    active_trains: List[Dict[str, Any]] = []
    norm_day = sim_day.lower()[:3]

    for row in rows:
        run_days = json.loads(row["run_days"] or "[]")
        run_days_norm = [d.lower()[:3] for d in run_days]
        if run_days_norm and norm_day not in run_days_norm:
            continue

        stops = json.loads(row["corridor_stops"] or "[]")
        if len(stops) < 2:
            continue

        pos = calculate_train_position_from_stops(
            stops=stops,
            now_time_str=sim_time,
            corridor_id=row["corridor_id"],
            train_number=row["train_number"],
            train_name=row["train_name"],
        )

        if pos.get("active"):
            active_trains.append(pos)

    return active_trains


# ─── 6. CLI INTERFACE & DEMONSTRATION ─────────────────────────────────────────

def run_cli_demo():
    parser = argparse.ArgumentParser(
        description="Railway Track-Following Train Position Algorithm (SAMANVAY DSS)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # 1. Run the user's exact specification example (Sealdah to Krishnanagar):
  python train_position_calculator.py --example

  # 2. Calculate position of a custom train run:
  python train_position_calculator.py --origin SDAH --destination KNJ --dep 12:00 --arr 16:00 --now 15:00

  # 3. Query all active trains from database at 08:30 AM Peak:
  python train_position_calculator.py --all-active --now 08:30 --day fri
        """
    )
    parser.add_argument("--example", action="store_true", help="Run user's SDAH -> KNJ 12:00->16:00 @ 15:00 example")
    parser.add_argument("--origin", type=str, default="SDAH", help="Origin station code (e.g. SDAH)")
    parser.add_argument("--destination", type=str, default="KNJ", help="Destination station code (e.g. KNJ)")
    parser.add_argument("--dep", type=str, default="12:00", help="Departure time HH:MM (e.g. 12:00)")
    parser.add_argument("--arr", type=str, default="16:00", help="Arrival time HH:MM (e.g. 16:00)")
    parser.add_argument("--now", type=str, default="15:00", help="Current simulation time HH:MM (e.g. 15:00)")
    parser.add_argument("--train-number", type=str, default="31811", help="Train number")
    parser.add_argument("--train-name", type=str, default="Sealdah - Krishnanagar City Local", help="Train name")
    parser.add_argument("--all-active", action="store_true", help="Calculate all active corridor trains from DB")
    parser.add_argument("--day", type=str, default="fri", help="Day of week (mon, tue, ...)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON response")

    args = parser.parse_args()

    if args.all_active:
        results = calculate_all_active_corridor_trains(sim_time=args.now, sim_day=args.day)
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            print(f"\n{'='*75}")
            print(f"  ACTIVE CORRIDOR TRAINS AT {args.now} IST ({args.day.upper()})")
            print(f"{'='*75}")
            print(f"Total Active Trains: {len(results)}\n")
            for t in results[:15]:
                print(f"[{t['train_number']}] {t['train_name'][:30]:<30} | {t['origin']} -> {t['destination']}")
                print(f"   Pos: ({t['coordinates']['lat']}, {t['coordinates']['lon']}) | "
                      f"Speed: {t['speed_kmh']} km/h | Progress: {t['journey_progress_pct']}% "
                      f"({t['distance_from_origin_km']} km from {t['origin']}, {t['distance_to_destination_km']} km to {t['destination']})")
                sec = t.get("intermediate_section", {})
                print(f"   Section: {sec.get('section', 'N/A')} ({sec.get('section_name', '')})")
                print("-" * 75)
            if len(results) > 15:
                print(f"... and {len(results) - 15} more active trains.")
        return

    # Single train calculation
    if args.example:
        orig = "SDAH"
        dest = "KNJ"
        dep = "12:00"
        arr = "16:00"
        now = "15:00"
        tnum = "31811"
        tname = "Sealdah - Krishnanagar City Local"
    else:
        orig = args.origin.upper()
        dest = args.destination.upper()
        dep = args.dep
        arr = args.arr
        now = args.now
        tnum = args.train_number
        tname = args.train_name

    res = calculate_single_train_position(
        origin_code=orig,
        dest_code=dest,
        dep_time_str=dep,
        arr_time_str=arr,
        now_time_str=now,
        train_number=tnum,
        train_name=tname,
    )

    if args.json:
        print(json.dumps(res, indent=2))
        return

    print("\n" + "=" * 80)
    print("  SAMANVAY RAILWAY DSS — TRACK-FOLLOWING POSITION ALGORITHM")
    print("=" * 80)
    print(f"Train:              {res.get('train_number')} - {res.get('train_name')}")
    print(f"Corridor:           {res.get('corridor_name', 'N/A')} (ID: {res.get('corridor_id')})")
    print(f"Origin -> Dest:     {res.get('origin')} ({res.get('departure_time')}) -> {res.get('destination')} ({res.get('arrival_time')})")
    print(f"Current Eval Time:  {res.get('current_time')}")
    print(f"Status:             {res.get('status')}")
    print("-" * 80)
    print("STEP-BY-STEP CALCULATION:")
    print(f"1. Block Time:               {res.get('block_time_hours')} hours ({res.get('block_time_minutes')} mins)")
    print(f"2. Total Track Distance:     {res.get('total_track_distance_km')} km (curved drawn polyline via intermediate stns)")
    print(f"3. Calculated Train Speed:   {res.get('speed_kmh')} km/h  (Speed = Total Distance / Block Time)")
    print(f"4. Elapsed Journey Time:     {res.get('elapsed_hours')} hours ({res.get('elapsed_minutes')} mins)")
    print(f"5. Distance From Origin:     {res.get('distance_from_origin_km')} km from {res.get('origin')}")
    print(f"6. Distance To Destination:  {res.get('distance_to_destination_km')} km away from {res.get('destination')}")
    print(f"7. Journey Progress:         {res.get('journey_progress_pct')}% completed")
    coords = res.get("coordinates", {})
    print(f"8. Interpolated GIS Coord:   Latitude {coords.get('lat')}, Longitude {coords.get('lon')}")
    sec = res.get("intermediate_section", {})
    print(f"9. Current Track Section:    {sec.get('section', 'N/A')} ({sec.get('section_name', 'N/A')})")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    run_cli_demo()
