"""
block_scheduler.py — SAMANVAY Intelligent Maintenance Block Scheduler
=====================================================================
Core algorithms for:
1. Fault Merger: cluster faults within configurable distance, block_dur = MAX of individual durations
2. Station-Gap Finder: for each fault location, find train-to-train gap in that specific section
3. Free Window Finder: Mon-Sun generic free gaps across entire corridor
4. Train Impact Scorer: priority-weighted delay penalty per candidate window
5. OpenRouter AI Advisor: sends structured context for feasibility verdict
"""

import os
import json
import math
import sqlite3
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from train_position_calculator import sort_stops_chronologically, parse_hhmm

BASE_DIR = Path(__file__).resolve().parent
DB_FILE  = BASE_DIR / "train_cache.db"
FAULTS_FILE = BASE_DIR / "faults.json"

IST = timezone(timedelta(hours=5, minutes=30))

# ─── Train Priority Classification ───────────────────────────────────────────
TRAIN_PRIORITY = {
    "Rajdhani Express": 5,
    "Vande Bharat Express": 5,
    "Duronto Express": 5,
    "Humsafar Express": 4,
    "Shatabdi Express": 4,
    "SUPERFAST": 3,
    "Superfast Express": 3,
    "MAIL EXPRESS": 3,
    "Mail/Express": 3,
    "Amrit Bharat Express": 3,
    "MEMU": 2,
    "PASSENGER": 2,
    "SUBURBAN": 1,
    "PARCEL EXPRESS": 1,
    "FREIGHT": 1,
}

def _priority(train_type: str) -> int:
    for k, v in TRAIN_PRIORITY.items():
        if k.lower() in train_type.lower():
            return v
    return 2  # default medium


# ─── Haversine Distance ───────────────────────────────────────────────────────
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ─── Time Helpers ─────────────────────────────────────────────────────────────
def hhmm_to_min(s: str) -> Optional[int]:
    """Convert HH:MM string to minutes-from-midnight. Returns None on failure."""
    if not s or s.strip() in ("--", "", "null", "None"):
        return None
    try:
        h, m = s.strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def min_to_hhmm(m: int) -> str:
    m = m % 1440  # wrap around midnight
    return f"{m // 60:02d}:{m % 60:02d}"


# ─── Load Faults ──────────────────────────────────────────────────────────────
CORRIDOR_FAULT_MAPPING = {
    "all": ["hwh_bwn_main", "hwh_bwn_chord", "sdah_knj_line", "bwn_asn_trunk", "hwh_bwn_asn_line", "bly_bwn_asn_trunk", "hwh_skg_line"],
    "hwh_bwn_main": ["hwh_bwn_main"],
    "hwh_bwn_asn_line": ["bwn_asn_trunk", "hwh_bwn_asn_line"],
    "bly_bwn_asn_trunk": ["bwn_asn_trunk", "bly_bwn_asn_trunk"],
    "sdah_knj_line": ["sdah_knj_line"],
    "hwh_skg_line": ["hwh_bwn_main", "hwh_skg_line"],
    "hwh_bwn_chord": ["hwh_bwn_chord"],
}

def load_faults(corridor_id: Optional[str] = None) -> List[Dict]:
    if not FAULTS_FILE.exists():
        return []
    with open(FAULTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    all_faults = data.get("faults", [])
    if not corridor_id or corridor_id == "all":
        return all_faults
    valid_cids = CORRIDOR_FAULT_MAPPING.get(corridor_id, [corridor_id])
    return [f for f in all_faults if f.get("corridor_id") in valid_cids]


# ─── Load Trains from DB ──────────────────────────────────────────────────────
def load_trains_for_corridor(corridor_id: str) -> List[Dict]:
    if not DB_FILE.exists():
        return []
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    if corridor_id == "all":
        rows = conn.execute(
            "SELECT * FROM corridor_trains WHERE train_type != 'TRAIN ON DEMAND' GROUP BY train_number"
        ).fetchall()
    elif corridor_id == "hwh_bwn_chord":
        rows = conn.execute(
            "SELECT * FROM corridor_trains WHERE corridor_id IN ('hwh_bwn_chord', 'hwh_bwn_main') AND train_type != 'TRAIN ON DEMAND'"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM corridor_trains WHERE corridor_id = ? AND train_type != 'TRAIN ON DEMAND'",
            (corridor_id,)
        ).fetchall()
    if not rows and corridor_id != "hwh_bwn_main":
        rows = conn.execute(
            "SELECT * FROM corridor_trains WHERE corridor_id = 'hwh_bwn_main' AND train_type != 'TRAIN ON DEMAND'"
        ).fetchall()
    conn.close()
    trains = []
    for row in rows:
        try:
            stops = json.loads(row["corridor_stops"] or "[]")
            run_days = json.loads(row["run_days"] or "[]")
            trains.append({
                "train_number": row["train_number"],
                "train_name": row["train_name"] or "",
                "train_type": row["train_type"] or "",
                "source_code": row["source_code"] or "",
                "dest_code": row["dest_code"] or "",
                "run_days": run_days,
                "corridor_stops": stops,
                "train_gap_coverage_potential": row["train_gap_coverage_potential"] if "train_gap_coverage_potential" in row.keys() else "",
            })
        except Exception:
            pass
    return trains


# ─── Get Corridor Time for a Train ───────────────────────────────────────────
def get_train_corridor_window(stops: List[Dict]) -> Tuple[Optional[int], Optional[int]]:
    """Returns (first_departure_min, last_arrival_min) across all stops."""
    if not stops:
        return None, None
    first_dep = None
    last_arr = None
    for s in stops:
        dep = hhmm_to_min(s.get("departure") or "")
        arr = hhmm_to_min(s.get("arrival") or "")
        if dep is not None:
            if first_dep is None or dep < first_dep:
                first_dep = dep
        if arr is not None:
            if last_arr is None or arr > last_arr:
                last_arr = arr
    # If only one stop has a departure (origin), set last_arr = first_dep
    if first_dep is not None and last_arr is None:
        last_arr = first_dep
    if last_arr is not None and first_dep is None:
        first_dep = last_arr
    return first_dep, last_arr


# ─── 1. FAULT MERGER ──────────────────────────────────────────────────────────
def merge_faults(
    selected_fault_ids: List[str],
    time_overrides: Dict[str, int],  # fault_id -> required_min (engineer-entered)
    merge_km: float = 15.0
) -> Dict:
    """
    Clusters selected faults within merge_km of each other.
    Block duration for a cluster = MAX of all individual time requirements.
    Returns clusters + standalone faults.
    """
    all_faults = load_faults()
    selected = [f for f in all_faults if f["id"] in selected_fault_ids]

    if not selected:
        return {"clusters": [], "standalone": [], "merge_km": merge_km}

    # Build adjacency
    used = set()
    clusters = []
    standalone = []

    for i, fa in enumerate(selected):
        if i in used:
            continue
        group = [fa]
        used.add(i)
        for j, fb in enumerate(selected):
            if j in used:
                continue
            # Check fb is within merge_km of ALL current group members
            all_close = all(
                haversine_km(fb["lat"], fb["long"], gm["lat"], gm["long"]) <= merge_km
                for gm in group
            )
            if all_close:
                group.append(fb)
                used.add(j)

        # Compute block duration = max of overrides or faults.json value
        durations = []
        for gf in group:
            fid = gf["id"]
            dur = time_overrides.get(fid) or gf.get("required_block_duration_min", 60)
            durations.append(int(dur))
        block_dur = max(durations)
        time_saved = sum(durations) - block_dur if len(group) > 1 else 0

        # Cluster center
        c_lat = sum(gf["lat"] for gf in group) / len(group)
        c_lon = sum(gf["long"] for gf in group) / len(group)

        # Nearest stations
        stations = list(dict.fromkeys(gf.get("nearest_station", "") for gf in group))

        # Max span
        max_span = 0.0
        for a in group:
            for b in group:
                d = haversine_km(a["lat"], a["long"], b["lat"], b["long"])
                if d > max_span:
                    max_span = d

        entry = {
            "cluster_id": f"CLU-{i+1:02d}",
            "corridor_id": group[0].get("corridor_id", ""),
            "fault_ids": [gf["id"] for gf in group],
            "fault_types": [gf.get("fault_type", "") for gf in group],
            "categories": list(dict.fromkeys(gf.get("category", "") for gf in group)),
            "nearest_stations": stations,
            "center_lat": round(c_lat, 6),
            "center_lon": round(c_lon, 6),
            "max_span_km": round(max_span, 2),
            "individual_durations": durations,
            "block_duration_min": block_dur,
            "time_saved_min": time_saved,
            "merged": len(group) > 1,
            "fault_count": len(group),
            "faults": [
                {
                    "id": gf["id"],
                    "fault_type": gf.get("fault_type", ""),
                    "category": gf.get("category", ""),
                    "nearest_station": gf.get("nearest_station", ""),
                    "chainage": gf.get("chainage", ""),
                    "location_name": gf.get("location_name", ""),
                    "severity": gf.get("severity", ""),
                    "lat": gf["lat"],
                    "lon": gf["long"],
                    "required_min": time_overrides.get(gf["id"]) or gf.get("required_block_duration_min", 60),
                    "action": gf.get("action_required", ""),
                }
                for gf in group
            ],
        }
        if len(group) >= 2:
            clusters.append(entry)
        else:
            standalone.append(entry)

    return {
        "clusters": clusters,
        "standalone": standalone,
        "merge_km": merge_km,
        "total_faults": len(selected),
    }


# ─── 2. STATION-SPECIFIC GAP FINDER (per fault location) ─────────────────────
def find_station_specific_gaps(
    fault_id: str,
    corridor_id: str,
    block_duration_min: int,
    day: str
) -> Dict:
    """
    For a specific fault, finds the two nearest stations in the corridor,
    then checks: after a train departs station A, before the next train arrives — 
    what is the gap available at that specific section?
    Returns per-fault personalized gaps for each day.
    """
    all_faults = load_faults()
    fault = next((f for f in all_faults if f["id"] == fault_id), None)
    if not fault:
        return {"fault_id": fault_id, "error": "Fault not found", "gaps": []}

    f_lat, f_lon = fault["lat"], fault["long"]
    trains = load_trains_for_corridor(corridor_id)

    DAY_SHORT = {
        "monday": "mon", "tuesday": "tue", "wednesday": "wed",
        "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"
    }
    day_short = DAY_SHORT.get(day.lower(), day[:3].lower())

    # Filter trains running today on this corridor
    active_trains = []
    for t in trains:
        run_days = t.get("run_days", [])
        run_norm = [d[:3].lower() for d in run_days]
        if not run_norm or day_short in run_norm:
            active_trains.append(t)

    # For each train, find which segment (between consecutive stops) is closest to fault
    # Then extract (departure from seg_start, arrival at seg_end) as the "occupied window"
    # for this specific section
    section_occupancy = []  # list of (dep_min, arr_min, train_number, train_name, train_type, prio)

    for t in active_trains:
        stops = t["corridor_stops"]
        if len(stops) < 2:
            continue

        min_dist = float("inf")
        best_dep_min = None
        best_arr_min = None

        for i in range(len(stops) - 1):
            s1 = stops[i]
            s2 = stops[i + 1]
            # Midpoint of segment
            mid_lat = (s1.get("lat", f_lat) + s2.get("lat", f_lat)) / 2
            mid_lon = (s1.get("lon", f_lon) + s2.get("lon", f_lon)) / 2
            # Fall back to fault coord if stops don't have coords
            dist = haversine_km(f_lat, f_lon, mid_lat, mid_lon)
            if dist < min_dist:
                min_dist = dist
                dep_min = hhmm_to_min(s1.get("departure") or s1.get("arrival") or "")
                arr_min = hhmm_to_min(s2.get("arrival") or s2.get("departure") or "")
                best_dep_min = dep_min
                best_arr_min = arr_min

        if best_dep_min is not None and best_arr_min is not None:
            # Handle midnight crossing
            if best_arr_min < best_dep_min:
                best_arr_min += 1440
            section_occupancy.append({
                "dep_min": best_dep_min,
                "arr_min": best_arr_min,
                "train_number": t["train_number"],
                "train_name": t["train_name"],
                "train_type": t["train_type"],
                "priority": _priority(t["train_type"]),
                "gap_coverage_potential": t.get("train_gap_coverage_potential", ""),
            })

    # Sort by departure time
    section_occupancy.sort(key=lambda x: x["dep_min"])

    # Find gaps between consecutive trains
    gaps = []
    for i in range(len(section_occupancy) - 1):
        curr = section_occupancy[i]
        nxt = section_occupancy[i + 1]
        gap_start = curr["arr_min"]
        gap_end = nxt["dep_min"]
        if gap_end > gap_start:
            gap_dur = gap_end - gap_start
            feasible = gap_dur >= block_duration_min
            gaps.append({
                "gap_start": min_to_hhmm(gap_start),
                "gap_end": min_to_hhmm(gap_end),
                "gap_start_min": gap_start,
                "gap_end_min": gap_end,
                "gap_duration_min": gap_dur,
                "required_min": block_duration_min,
                "headroom_min": max(0, gap_dur - block_duration_min),
                "feasible": feasible,
                "train_before": curr["train_number"],
                "train_before_name": curr["train_name"],
                "train_after": nxt["train_number"],
                "train_after_name": nxt["train_name"],
            })

    # Also check before first train and after last train
    if section_occupancy:
        first = section_occupancy[0]
        last_t = section_occupancy[-1]
        if first["dep_min"] > 0:
            gap_dur = first["dep_min"]
            gaps.insert(0, {
                "gap_start": "00:00",
                "gap_end": min_to_hhmm(first["dep_min"]),
                "gap_start_min": 0,
                "gap_end_min": first["dep_min"],
                "gap_duration_min": gap_dur,
                "required_min": block_duration_min,
                "headroom_min": max(0, gap_dur - block_duration_min),
                "feasible": gap_dur >= block_duration_min,
                "train_before": "START_OF_DAY",
                "train_before_name": "Start of Day",
                "train_after": first["train_number"],
                "train_after_name": first["train_name"],
            })
        end_gap = 1440 - last_t["arr_min"]
        if end_gap > 0:
            gaps.append({
                "gap_start": min_to_hhmm(last_t["arr_min"]),
                "gap_end": "24:00",
                "gap_start_min": last_t["arr_min"],
                "gap_end_min": 1440,
                "gap_duration_min": end_gap,
                "required_min": block_duration_min,
                "headroom_min": max(0, end_gap - block_duration_min),
                "feasible": end_gap >= block_duration_min,
                "train_before": last_t["train_number"],
                "train_before_name": last_t["train_name"],
                "train_after": "END_OF_DAY",
                "train_after_name": "End of Day",
            })

    feasible_gaps = [g for g in gaps if g["feasible"]]
    feasible_gaps.sort(key=lambda x: -x["headroom_min"])

    return {
        "fault_id": fault_id,
        "fault_location": fault.get("location_name", ""),
        "nearest_station": fault.get("nearest_station", ""),
        "day": day,
        "block_duration_required_min": block_duration_min,
        "all_gaps": gaps,
        "feasible_gaps": feasible_gaps,
        "train_count_in_section": len(section_occupancy),
        "trains_in_section": section_occupancy,
    }


# ─── 3. GENERIC FREE WINDOW FINDER (full corridor, Mon–Sun) ──────────────────
def get_train_direction(t: Dict) -> str:
    """Classifies train into UP or DOWN matching gap_calculator.py logic."""
    dest = t.get("dest_code", "")
    orig = t.get("source_code", "")
    is_inbound = False
    if dest in ("HWH", "SDAH"):
        is_inbound = True
    elif orig in ("HWH", "SDAH"):
        is_inbound = False
    elif orig in ("KNJ", "BWN", "ASN", "SKG", "RHA", "KWAE", "BHP"):
        is_inbound = True
    elif dest in ("KNJ", "BWN", "ASN", "SKG", "RHA", "KWAE", "BHP"):
        is_inbound = False
    return "DOWN" if is_inbound else "UP"


def find_free_windows_all_days(
    corridor_id: str,
    block_duration_min: int,
    direction: str = "UP"
) -> Dict:
    """
    For the full corridor, finds free time gaps on every day Mon–Sun
    for the specified track direction (UP, DOWN, or ALL), algorithmically
    synchronized with index.html gap schedules.
    """
    DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    DAY_SHORT = {
        "monday": "mon", "tuesday": "tue", "wednesday": "wed",
        "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"
    }

    all_trains = load_trains_for_corridor(corridor_id)
    
    if direction in ("UP", "DOWN"):
        trains = [t for t in all_trains if get_train_direction(t) == direction]
    else:
        trains = all_trains

    week_view = {}

    for day in DAYS:
        day_short = DAY_SHORT[day]
        active = []
        for t in trains:
            run_days = t.get("run_days", [])
            run_norm = [d[:3].lower() for d in run_days]
            if not run_norm or day_short in run_norm:
                ordered, _ = sort_stops_chronologically(t.get("corridor_stops", []))
                if not ordered:
                    dep_min, arr_min = get_train_corridor_window(t.get("corridor_stops", []))
                else:
                    first = ordered[0]
                    last = ordered[-1]
                    dep_min = parse_hhmm(first.get("departure") or first.get("arrival"))
                    arr_min = parse_hhmm(last.get("arrival") or last.get("departure"))
                    if dep_min is None or arr_min is None:
                        dep_min, arr_min = get_train_corridor_window(t.get("corridor_stops", []))
                
                if dep_min is not None and arr_min is not None:
                    if arr_min < dep_min:
                        arr_min += 1440
                    active.append({
                        "train_number": t["train_number"],
                        "train_name": t["train_name"],
                        "train_type": t["train_type"],
                        "priority": _priority(t["train_type"]),
                        "dep_min": dep_min,
                        "arr_min": arr_min,
                        "gap_coverage_potential": t.get("train_gap_coverage_potential", ""),
                    })

        active.sort(key=lambda x: x["dep_min"])

        # Build occupied intervals (merged)
        occupied = []
        for t in active:
            if occupied and t["dep_min"] <= occupied[-1][1]:
                occupied[-1] = (occupied[-1][0], max(occupied[-1][1], t["arr_min"]))
            else:
                occupied.append((t["dep_min"], t["arr_min"]))

        # Free gaps = inverted occupied (matching index.html timetable gaps)
        free_gaps = []
        prev_end = 0
        for (start, end) in occupied:
            if start > prev_end:
                gap_dur = start - prev_end
                if gap_dur >= 20:  # Candidate gaps >= 20 min
                    free_gaps.append({
                        "gap_start": min_to_hhmm(prev_end),
                        "gap_end": min_to_hhmm(start) if start < 1440 else "24:00",
                        "gap_start_min": prev_end,
                        "gap_end_min": min(start, 1440),
                        "gap_duration_min": gap_dur,
                        "required_min": block_duration_min,
                        "headroom_min": max(0, gap_dur - block_duration_min),
                        "feasible": gap_dur >= block_duration_min,
                    })
            prev_end = max(prev_end, end)

        if prev_end < 1440:
            gap_dur = 1440 - prev_end
            if gap_dur >= 20:
                free_gaps.append({
                    "gap_start": min_to_hhmm(prev_end),
                    "gap_end": "24:00",
                    "gap_start_min": prev_end,
                    "gap_end_min": 1440,
                    "gap_duration_min": gap_dur,
                    "required_min": block_duration_min,
                    "headroom_min": max(0, gap_dur - block_duration_min),
                    "feasible": gap_dur >= block_duration_min,
                })

        day_occ = []
        for s, e in occupied:
            if s < 1440:
                day_occ.append({
                    "start": min_to_hhmm(s),
                    "end": "24:00" if e >= 1440 else min_to_hhmm(e),
                    "start_min": s,
                    "end_min": min(e, 1440),
                })

        week_view[day] = {
            "day": day,
            "active_train_count": len(active),
            "occupied_intervals": day_occ,
            "free_gaps": free_gaps,
            "feasible_gaps": [g for g in free_gaps if g["feasible"]],
            "trains": active,
        }

    return {
        "corridor_id": corridor_id,
        "direction": direction,
        "block_duration_min": block_duration_min,
        "week": week_view
    }


# ─── 4. TRAIN IMPACT SCORER ───────────────────────────────────────────────────
def score_window_impact(
    corridor_id: str,
    day: str,
    window_start_min: int,
    window_end_min: int,
    block_duration_required_min: Optional[int] = None,
) -> Dict:
    """
    For a given candidate window, finds all trains affected and computes
    priority-weighted penalty scores and slack recovery capability.
    """
    DAY_SHORT = {
        "monday": "mon", "tuesday": "tue", "wednesday": "wed",
        "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"
    }
    day_short = DAY_SHORT.get(day.lower(), day[:3].lower())
    trains = load_trains_for_corridor(corridor_id)

    affected = []
    total_penalty = 0

    for t in trains:
        run_days = t.get("run_days", [])
        run_norm = [d[:3].lower() for d in run_days]
        if run_norm and day_short not in run_norm:
            continue

        dep_min, arr_min = get_train_corridor_window(t["corridor_stops"])
        if dep_min is None or arr_min is None:
            continue
        if arr_min < dep_min:
            arr_min += 1440

        # Check overlap with window
        overlap_start = max(dep_min, window_start_min)
        overlap_end = min(arr_min, window_end_min)
        overlap_min = max(0, overlap_end - overlap_start)

        if overlap_min > 0:
            prio = _priority(t["train_type"])
            penalty = prio * overlap_min
            total_penalty += penalty
            gap_cov = t.get("train_gap_coverage_potential", "")
            # Parse gap coverage potential (e.g. "42 mins", "Yes (15 min)" -> 42, 15)
            cov_min = None
            if gap_cov:
                import re
                match = re.search(r'\d+', str(gap_cov))
                if match:
                    cov_min = int(match.group())

            window_dur = window_end_min - window_start_min
            extra_needed = max(0, (block_duration_required_min or window_dur) - window_dur)
            can_cover_overlap = (cov_min is not None and cov_min >= overlap_min) if overlap_min > 0 else True
            can_cover_deficit = (cov_min is not None and cov_min >= extra_needed) if extra_needed > 0 else True
            can_cover = can_cover_overlap or can_cover_deficit or (cov_min is not None and cov_min > 0)
            absorbable_min = min(cov_min or 0, overlap_min)

            if cov_min is None or cov_min == 0:
                slack_status = "none"
            elif can_cover_overlap:
                slack_status = "full"
            elif can_cover_deficit:
                slack_status = "deficit_covered"
            else:
                slack_status = "partial"

            affected.append({
                "train_number": t["train_number"],
                "train_name": t["train_name"],
                "train_type": t["train_type"],
                "priority": prio,
                "priority_label": "★" * prio,
                "corridor_dep": min_to_hhmm(dep_min),
                "corridor_arr": min_to_hhmm(arr_min % 1440),
                "overlap_min": overlap_min,
                "penalty_score": penalty,
                "gap_coverage_potential": gap_cov,
                "gap_coverage_min": cov_min or 0,
                "can_cover_block": can_cover,
                "can_cover_overlap": can_cover_overlap,
                "can_cover_deficit": can_cover_deficit,
                "absorbable_min": absorbable_min,
                "slack_status": slack_status,
            })

    affected.sort(key=lambda x: -x["penalty_score"])

    total_slack = sum(a["gap_coverage_min"] for a in affected)
    window_dur = window_end_min - window_start_min
    deficit = max(0, (block_duration_required_min or window_dur) - window_dur)

    return {
        "corridor_id": corridor_id,
        "day": day,
        "window_start": min_to_hhmm(window_start_min),
        "window_end": min_to_hhmm(window_end_min),
        "window_duration_min": window_dur,
        "block_deficit_min": deficit,
        "total_penalty_score": total_penalty,
        "affected_train_count": len(affected),
        "affected_trains": affected,
        "high_priority_affected": sum(1 for a in affected if a["priority"] >= 4),
        "trains_that_can_cover": sum(1 for a in affected if a["can_cover_overlap"] or a["can_cover_deficit"]),
        "total_slack_available_min": total_slack,
    }


# ─── 5. OPENROUTER AI ADVISOR ─────────────────────────────────────────────────
def call_ai_advisor(
    corridor_name: str,
    cluster_info: Dict,
    window_info: Dict,
    impact_info: Dict,
    station_gaps: Optional[List[Dict]] = None,
) -> Dict:
    """
    2-Layer AI Verification Engine:
    Layer 1: Deterministic Mathematical Pre-Check (Time Deficit vs Train Slack Coverage).
    Layer 2: LLM Executive Verification & DRM Summary Generation.
    """
    # ── LAYER 1: Deterministic Feasibility Pre-Check ────────────────────────
    req_dur = cluster_info.get("block_duration_min", 60)
    avail_gap = window_info.get("window_duration_min", 0)
    time_deficit = max(0, req_dur - avail_gap)

    affected_trains = impact_info.get("affected_trains", [])
    total_slack_mins = 0

    for tr in affected_trains:
        cov_min = tr.get("gap_coverage_min")
        if cov_min is None:
            cov_str = tr.get("gap_coverage_potential", "")
            if cov_str:
                import re
                match = re.search(r'\d+', str(cov_str))
                if match:
                    cov_min = int(match.group())
                else:
                    cov_min = 0
            else:
                cov_min = 0
        total_slack_mins += cov_min

    high_prio_count = impact_info.get("high_priority_affected", 0)

    if time_deficit == 0:
        deterministic_verdict = "FEASIBLE"
        deterministic_reason = (
            f"The proposed free gap ({avail_gap} mins) fully satisfies the required maintenance duration ({req_dur} mins). "
            f"Zero time deficit. Train traffic operates cleanly without delay."
        )
    elif total_slack_mins >= time_deficit and high_prio_count == 0:
        deterministic_verdict = "FEASIBLE"
        deterministic_reason = (
            f"Required duration ({req_dur} mins) exceeds free gap ({avail_gap} mins) by a {time_deficit} min deficit. "
            f"However, running trains provide {total_slack_mins} mins of total slack buffer which fully absorbs the deficit without cascading delays."
        )
    elif total_slack_mins >= time_deficit and high_prio_count > 0:
        deterministic_verdict = "RISKY"
        deterministic_reason = (
            f"Required duration ({req_dur} mins) has a {time_deficit} min deficit covered by {total_slack_mins} mins of train slack. "
            f"However, {high_prio_count} high-priority train(s) (Rajdhani/Vande Bharat/Shatabdi) are affected, posing schedule impact risk."
        )
    else:
        deterministic_verdict = "NOT_FEASIBLE"
        deterministic_reason = (
            f"Required duration ({req_dur} mins) exceeds free gap ({avail_gap} mins) by {time_deficit} mins. "
            f"Total available train slack buffer ({total_slack_mins} mins) is insufficient to absorb the deficit, leading to an unrecoverable {time_deficit - total_slack_mins} min delay."
        )

    layer1_summary = {
        "deterministic_verdict": deterministic_verdict,
        "required_duration_min": req_dur,
        "available_gap_min": avail_gap,
        "time_deficit_min": time_deficit,
        "train_slack_capacity_min": total_slack_mins,
        "deterministic_reason": deterministic_reason
    }

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    model = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-r1")
    site_url = os.getenv("OPENROUTER_SITE_URL", "http://localhost:8000")
    site_name = os.getenv("OPENROUTER_SITE_NAME", "SAMANVAY-DSS")

    fallback_response = {
        "verdict": deterministic_verdict,
        "summary": deterministic_reason,
        "high_risk_trains": [t["train_number"] for t in affected_trains if t.get("priority", 0) >= 4],
        "suggestions": [
            f"Shift block start time to align with a larger free gap (deficit: {time_deficit} mins).",
            f"Split the {req_dur} min block into two shorter maintenance sessions if deficit cannot be absorbed."
        ],
        "split_recommended": time_deficit > 30,
        "split_suggestion": f"Split into {req_dur//2} min and {req_dur - req_dur//2} min sessions" if time_deficit > 30 else None,
        "layer1_math": layer1_summary
    }

    if not api_key:
        return fallback_response

    # Build affected trains summary with accurate slack capability
    affected_trains_text = ""
    for a in affected_trains[:12]:
        cov_m = a.get("gap_coverage_min", 0) or 0
        ov_m = a.get("overlap_min", 0)
        cov_str = a.get("gap_coverage_potential", f"{cov_m} mins")
        if a.get("can_cover_overlap") or (cov_m >= ov_m and cov_m > 0):
            status_desc = f"✓ FULL OVERLAP COVER ({cov_m}m slack >= {ov_m}m overlap)"
        elif a.get("can_cover_deficit") or (cov_m >= time_deficit and time_deficit > 0):
            status_desc = f"✓ ABSORBS ENTIRE DEFICIT ({cov_m}m slack >= {time_deficit}m deficit)"
        elif cov_m > 0:
            abs_m = a.get("absorbable_min", min(cov_m, ov_m))
            status_desc = f"⚡ PARTIAL ({cov_m}m slack absorbs {abs_m}m of delay)"
        else:
            status_desc = "✗ NO SLACK BUFFER"

        affected_trains_text += (
            f"  - Train {a['train_number']} ({a['train_name']}) | "
            f"Type: {a['train_type']} | Priority: {a['priority']}/5 | "
            f"Overlap: {ov_m} min | Timetable Slack Buffer: {cov_str} [{status_desc}]\n"
        )

    # Station-specific gap info
    gap_text = ""
    if station_gaps:
        for sg in station_gaps:
            gap_text += f"\n  Fault {sg.get('fault_id')} at {sg.get('fault_location', 'Unknown')}:\n"
            feasible_gaps = sg.get("feasible_gaps", [])
            if feasible_gaps:
                g = feasible_gaps[0]
                gap_text += (
                    f"    Best local gap: {g['gap_start']}–{g['gap_end']} "
                    f"({g['gap_duration_min']} min available, {g['required_min']} min needed, "
                    f"{g['headroom_min']} min headroom)\n"
                    f"    Between trains: {g['train_before_name']} → {g['train_after_name']}\n"
                )
            else:
                gap_text += f"    No feasible local gap found for {sg.get('block_duration_required_min')} min block.\n"

    cluster_desc = ""
    for f in cluster_info.get("faults", []):
        cluster_desc += (
            f"  - [{f.get('category','')}] {f.get('fault_type','')} at {f.get('location_name','')}"
            f"(Severity: {f.get('severity','')}, Required: {f.get('required_min', 60)} min, "
            f"Action: {f.get('action','')})\n"
        )

    prompt = f"""You are an expert Indian Railways maintenance planning AI advisor for the SAMANVAY DSS system.

== CORRIDOR ==
{corridor_name}

== MAINTENANCE WORK REQUIRED ==
Cluster ID: {cluster_info.get("cluster_id", "N/A")}
Merged Faults: {cluster_info.get("fault_count", 1)} fault(s)
Block Duration Required: {req_dur} minutes
Time Saved by Merging: {cluster_info.get("time_saved_min", 0)} minutes
{cluster_desc}

== PROPOSED WINDOW ==
Day: {window_info.get("day", "N/A")}
Start: {window_info.get("window_start")} | End: {window_info.get("window_end")}
Duration: {avail_gap} min
Available Headroom: {window_info.get("headroom_min", 0)} min

== LAYER 1 DETERMINISTIC MATHEMATICAL PRE-CHECK ==
Required Maintenance Time: {req_dur} mins
Available Natural Gap: {avail_gap} mins
Corridor Time Deficit: {time_deficit} mins
Total Train Slack Buffer Across Active Trains: {total_slack_mins} mins
Net Slack Surplus: {max(0, total_slack_mins - time_deficit)} mins
Calculated Feasibility Verdict: {deterministic_verdict}
Mathematical Rationale: {deterministic_reason}

== STATION-SPECIFIC GAP ANALYSIS ==
{gap_text if gap_text else "Not computed for this request."}

== TRAIN IMPACT & SLACK RECOVERY ANALYSIS ==
Total Affected Trains: {impact_info.get("affected_train_count", 0)}
High-Priority Trains Affected (Priority 4-5): {impact_info.get("high_priority_affected", 0)}
Trains With Timetable Slack Coverage: {impact_info.get("trains_that_can_cover", 0)}
Total Slack Buffer Available: {total_slack_mins} mins
Total Penalty Score: {impact_info.get("total_penalty_score", 0)} (lower = better)

Affected Trains & Slack Breakdown:
{affected_trains_text}

== YOUR LAYER 2 VERIFICATION TASK ==
1. Confirm the Layer 1 Verdict ({deterministic_verdict}). Do not contradict the mathematical pre-check.
2. Provide a professional executive summary (2-3 sentences) for a Divisional Railway Manager explaining how the {time_deficit} min time deficit is absorbed by the {total_slack_mins} mins of train slack buffers without cascading delays.
3. If RISKY or NOT_FEASIBLE: list 2-3 specific actionable suggestions.
4. If any Rajdhani/Vande Bharat/Duronto trains are affected, explicitly name them and state the penalty.

Respond in this exact JSON format:
{{
  "verdict": "{deterministic_verdict}",
  "summary": "Professional DRM executive summary based on the math",
  "high_risk_trains": ["list of high priority train numbers at risk"],
  "suggestions": ["suggestion 1", "suggestion 2"],
  "split_recommended": true/false,
  "split_suggestion": "How to split if recommended, else null"
}}"""

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 3500,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": site_url,
            "X-Title": site_name,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            response_data = json.loads(resp.read().decode("utf-8"))
        msg = response_data["choices"][0]["message"]
        content = msg.get("content") or ""
        if not content and msg.get("reasoning"):
            content = msg.get("reasoning")
        content = content.strip()
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        result = json.loads(content)
        result["model_used"] = model
        result["layer1_math"] = layer1_summary
        result["raw_response"] = content
        return result
    except Exception as e:
        print(f"OpenRouter API call exception, returning Layer 1 fallback: {e}")
        return fallback_response


# ─── DB Helpers: Category Time Overrides ──────────────────────────────────────
def save_time_override(corridor_id: str, fault_id: str, category: str, override_min: int, set_by: str = "engineer"):
    conn = sqlite3.connect(str(DB_FILE))
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    # Upsert
    conn.execute("""
        INSERT INTO category_time_overrides (corridor_id, fault_id, category, override_min, set_by, set_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(fault_id) DO UPDATE SET override_min=excluded.override_min, set_by=excluded.set_by, set_at=excluded.set_at
    """, (corridor_id, fault_id, category, override_min, set_by, now))
    conn.commit()
    conn.close()


def load_time_overrides(corridor_id: Optional[str] = None) -> Dict[str, int]:
    """Returns {fault_id: override_min}"""
    conn = sqlite3.connect(str(DB_FILE))
    if corridor_id:
        rows = conn.execute(
            "SELECT fault_id, override_min FROM category_time_overrides WHERE corridor_id = ?",
            (corridor_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT fault_id, override_min FROM category_time_overrides").fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def save_block_decision(
    block_id: str,
    corridor_id: str,
    fault_ids: List[str],
    category: str,
    block_start: str,
    block_end: str,
    block_dur_min: int,
    day_of_week: str,
    window_type: str,
    ai_recommendation: str,
    ai_feasibility: str,
    manager_decision: str,
    manager_notes: str = "",
):
    conn = sqlite3.connect(str(DB_FILE))
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT OR REPLACE INTO maintenance_blocks
        (block_id, corridor_id, fault_ids, category, block_start, block_end, block_dur_min,
         day_of_week, window_type, ai_recommendation, ai_feasibility, manager_decision,
         manager_notes, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (block_id, corridor_id, json.dumps(fault_ids), category, block_start, block_end,
          block_dur_min, day_of_week, window_type, ai_recommendation, ai_feasibility,
          manager_decision, manager_notes, now, now))
    conn.commit()
    conn.close()


def load_scheduled_blocks(corridor_id: Optional[str] = None) -> List[Dict]:
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    if corridor_id:
        rows = conn.execute(
            "SELECT * FROM maintenance_blocks WHERE corridor_id = ? ORDER BY day_of_week, block_start",
            (corridor_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM maintenance_blocks ORDER BY corridor_id, day_of_week, block_start"
        ).fetchall()
    conn.close()
    blocks = []
    for r in rows:
        d = dict(r)
        try:
            d["fault_ids"] = json.loads(d.get("fault_ids") or "[]")
        except Exception:
            pass
        blocks.append(d)
    return blocks


def ensure_db_tables():
    """Create new tables if they don't exist yet."""
    conn = sqlite3.connect(str(DB_FILE))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS maintenance_blocks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            block_id    TEXT    NOT NULL UNIQUE,
            corridor_id TEXT    NOT NULL,
            fault_ids   TEXT    NOT NULL,
            category    TEXT    NOT NULL,
            block_start TEXT    NOT NULL,
            block_end   TEXT    NOT NULL,
            block_dur_min INTEGER NOT NULL,
            day_of_week TEXT    NOT NULL,
            window_type TEXT    DEFAULT 'scheduled',
            ai_recommendation TEXT,
            ai_feasibility TEXT,
            manager_decision TEXT DEFAULT 'pending',
            manager_notes TEXT,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS category_time_overrides (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            corridor_id TEXT    NOT NULL,
            fault_id    TEXT    NOT NULL UNIQUE,
            category    TEXT    NOT NULL,
            override_min INTEGER NOT NULL,
            set_by      TEXT,
            set_at      TEXT
        )
    """)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    ensure_db_tables()
    print("DB tables ensured.")
    # Quick test
    faults = load_faults()
    print(f"Loaded {len(faults)} faults")
    if faults:
        f0 = faults[0]
        print(f"Testing station-gap finder for fault {f0['id']} on {f0.get('corridor_id')}...")
        result = find_station_specific_gaps(f0["id"], f0.get("corridor_id", "hwh_bwn_main"), 60, "monday")
        print(f"  Feasible gaps: {len(result.get('feasible_gaps', []))}")
