"""
fetch_trains.py — Corridor Train Data Fetcher for SAMANVAY

Run this script to populate the SQLite cache with real timetable data for all corridors.

Usage:
    python fetch_trains.py                  # Full fetch for all corridors
    python fetch_trains.py --corridor hwh_bwn_main   # Single corridor
    python fetch_trains.py --dry-run        # Print plan without hitting API
    python fetch_trains.py --force          # Re-fetch even if cached

How it works:
1. For each corridor, fetches the station board for all key stations (with includeIntermediate=True)
2. Cross-matches train numbers: a train valid for a corridor must appear at ALL key stations
3. For each matched train, fetches the full timetable to get exact arrival/departure times at every stop
4. Builds corridor_stops: an ordered list of timings at each corridor station
5. Saves everything to train_cache.db
"""

import os
import sys
import json
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

# Handle argument for dotenv path when run directly
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from railradar_client import RailRadarClient, get_db, init_db, IST

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("fetch_trains")

# ─── Corridor Definitions ─────────────────────────────────────────────────────
# Key stations: a train must appear at ALL of these to be considered a corridor train.
# Ordered from one terminus to the other.

CORRIDORS: Dict[str, Dict] = {
    "sdah_knj_line": {
        "name": "Sealdah – Krishnanagar City Junction",
        "color": "#a855f7",
        "key_stations": ["SDAH", "RHA", "KNJ"],
        "station_names": {
            "SDAH": "Sealdah",
            "RHA":  "Ranaghat Junction",
            "KNJ":  "Krishnanagar City Jn",
        },
    },
    "hwh_skg_line": {
        "name": "Howrah – Saktigarh",
        "color": "#22c55e",
        "key_stations": ["HWH", "SKG"],
        "station_names": {
            "HWH":  "Howrah Junction",
            "SKG":  "Saktigarh",
        },
    },
    "bly_bwn_asn_trunk": {
        "name": "Bally – Barddhaman – Asansol Trunk",
        "color": "#f59e0b",
        "key_stations": ["BLY", "BWN", "ASN"],
        "station_names": {
            "BLY": "Bally",
            "BWN": "Barddhaman Junction",
            "ASN": "Asansol Junction",
        },
    },
    "hwh_bwn_asn_line": {
        "name": "Howrah – Barddhaman – Asansol",
        "color": "#06b6d4",
        "key_stations": ["HWH", "BWN", "ASN"],
        "station_names": {
            "HWH": "Howrah Junction",
            "BWN": "Barddhaman Junction",
            "ASN": "Asansol Junction",
        },
    },
    "hwh_bwn_main": {
        "name": "Howrah – Barddhaman Main Line",
        "color": "#3b82f6",
        "key_stations": ["HWH", "BDC", "BWN"],
        "station_names": {
            "HWH": "Howrah Junction",
            "BDC": "Bandel Junction",
            "BWN": "Barddhaman Junction",
        },
    },
}


# ─── Time Helpers ─────────────────────────────────────────────────────────────

def parse_time(t: str) -> Optional[int]:
    """Converts HH:MM string to minutes-from-midnight. Returns None if invalid."""
    if not t or t in ("--", "N/A", ""):
        return None
    try:
        parts = t.strip().split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return None


def minutes_to_hhmm(mins: int) -> str:
    """Converts minutes-from-midnight to HH:MM string (handles overnight: e.g. 1480 -> 00:40)."""
    mins = mins % 1440
    return f"{mins // 60:02d}:{mins % 60:02d}"

# ─── Station Board Fetcher ────────────────────────────────────────────────────

def fetch_station_board(client: RailRadarClient, station_code: str, dry_run: bool = False) -> List[dict]:
    """Fetches the station board and returns a list of train records."""
    if dry_run:
        logger.info(f"[DRY-RUN] Would fetch station board for: {station_code}")
        return []
    logger.info(f"[Fetch] Station board: {station_code}")
    try:
        data = client.get_station_board(station_code, include_intermediate=True)
    except RuntimeError as e:
        logger.error(f"[Fetch] Failed {station_code}: {e}")
        return []

    # Normalize response: try common field names
    trains = (
        data.get("trains") or
        data.get("data") or
        data.get("results") or
        data.get("schedule") or
        []
    )
    logger.info(f"[Fetch] {station_code}: {len(trains)} trains returned")
    return trains


def extract_train_number(t: dict) -> Optional[str]:
    """Extracts train number from a train record regardless of field name."""
    for key in ("trainNumber", "train_number", "number", "trainNo", "train_no"):
        val = t.get(key)
        if val:
            return str(val).strip()
    return None


def extract_run_days(t: dict) -> List[str]:
    """Extracts run_days list from a train record."""
    for key in ("runDays", "run_days", "days", "runningDays"):
        val = t.get(key)
        if val and isinstance(val, list):
            return [d.lower() for d in val]
        if val and isinstance(val, str):
            return [d.strip().lower() for d in val.split(",")]
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]  # Assume daily if unknown


# ─── Cross-Match Logic ────────────────────────────────────────────────────────

def cross_match_trains(station_train_sets: Dict[str, Set[str]]) -> Set[str]:
    """
    Returns the set of train numbers that appear at ALL key stations.
    Non-halting trains (includeIntermediate=true) are included.
    """
    sets = list(station_train_sets.values())
    if not sets:
        return set()
    common = sets[0].copy()
    for s in sets[1:]:
        common &= s
    return common


# ─── Timetable Parser ─────────────────────────────────────────────────────────

def get_stop_at_station(stops: List[dict], station_code: str) -> Optional[dict]:
    """Finds a stop record for a specific station from a train's timetable."""
    station_code = station_code.upper()
    for stop in stops:
        code = (
            stop.get("stationCode") or stop.get("station_code") or
            stop.get("code") or stop.get("stationId") or ""
        ).upper()
        if code == station_code:
            return stop
    return None


def extract_stop_time(stop: dict) -> Dict[str, str]:
    """Extracts arrival and departure times from a stop record."""
    arrival = (
        stop.get("arrival") or stop.get("arrivalTime") or
        stop.get("arrival_time") or stop.get("scheduledArrival") or ""
    )
    departure = (
        stop.get("departure") or stop.get("departureTime") or
        stop.get("departure_time") or stop.get("scheduledDeparture") or ""
    )
    day = int(stop.get("day") or stop.get("arrivalDay") or stop.get("dayCount") or 1) - 1  # 0-indexed
    return {
        "arrival": str(arrival).strip() if arrival else None,
        "departure": str(departure).strip() if departure else None,
        "day": day,  # 0 = same day as origin
    }


def fetch_corridor(client: RailRadarClient, corridor_id: str, corridor_cfg: dict,
                   dry_run: bool = False, force: bool = False, fetch_timetables: bool = False) -> int:
    """
    Fetches trains for a single corridor and saves to corridor_trains table.
    Returns count of corridor trains found.
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"[Corridor] {corridor_id}: {corridor_cfg['name']}")
    key_stations = corridor_cfg["key_stations"]

    # Check if already computed recently (unless force)
    if not force and not dry_run:
        conn = get_db()
        row = conn.execute(
            "SELECT computed_at FROM corridor_trains WHERE corridor_id = ? LIMIT 1",
            (corridor_id,)
        ).fetchone()
        conn.close()
        if row:
            computed_at = datetime.fromisoformat(row["computed_at"])
            age_hours = (datetime.now(IST) - computed_at.replace(tzinfo=IST)).total_seconds() / 3600
            if age_hours < 20:
                logger.info(f"[Corridor] {corridor_id}: Already cached ({age_hours:.1f}h old), skipping")
                return 0

    # Step 1: Fetch station boards for all key stations
    station_train_sets: Dict[str, Set[str]] = {}
    station_train_records: Dict[str, Dict[str, dict]] = {}  # station -> {train_num -> record}

    for stn in key_stations:
        trains = fetch_station_board(client, stn, dry_run=dry_run)
        nums: Set[str] = set()
        records: Dict[str, dict] = {}
        for t in trains:
            num = extract_train_number(t)
            if num:
                nums.add(num)
                records[num] = t
        station_train_sets[stn] = nums
        station_train_records[stn] = records
        logger.info(f"  {stn}: {len(nums)} unique trains")

    if dry_run:
        logger.info(f"[DRY-RUN] {corridor_id}: would cross-match {len(key_stations)} stations")
        return 0

    # Step 2: Cross-match
    matched_trains = cross_match_trains(station_train_sets)
    logger.info(f"[Corridor] {corridor_id}: {len(matched_trains)} trains pass through all {len(key_stations)} stations")

    if not matched_trains:
        logger.warning(f"[Corridor] {corridor_id}: 0 matched trains — check station codes")
        return 0

    # Step 3: Fetch full timetable for each matched train to get exact timings at corridor stations
    saved_count = 0
    now = datetime.now(IST).isoformat()

    for train_num in sorted(matched_trains):
        tt_data = None
        if fetch_timetables:
            logger.info(f"  [Train] {train_num}: fetching timetable …")
            try:
                tt_data = client.get_train_timetable(train_num)
            except RuntimeError as e:
                logger.warning(f"  [Train] {train_num}: timetable fetch failed: {e}")
        else:
            # Check if already in cache without network hit
            ck = client._cache_key(f"/v1/trains/{train_num}", {})
            tt_data = client._get_cached(ck)

        if not tt_data or "error" in tt_data:
            stops_raw = []
        else:
            stops_raw = (
                tt_data.get("stops") or tt_data.get("schedule") or
                tt_data.get("stations") or tt_data.get("data") or []
            )

        # Build corridor_stops: timing at each key station (in order)
        corridor_stops = []
        for stn in key_stations:
            if stops_raw:
                stop = get_stop_at_station(stops_raw, stn)
                if stop:
                    timing = extract_stop_time(stop)
                    corridor_stops.append({
                        "station_code": stn,
                        "station_name": corridor_cfg["station_names"].get(stn, stn),
                        "arrival": timing["arrival"],
                        "departure": timing["departure"],
                        "day": timing["day"],
                    })
                    continue

            # Fallback: use station board record timings
            rec = station_train_records.get(stn, {}).get(train_num, {})
            if rec:
                arrival = rec.get("arrival") or rec.get("arrivalTime") or None
                departure = rec.get("departure") or rec.get("departureTime") or None
                corridor_stops.append({
                    "station_code": stn,
                    "station_name": corridor_cfg["station_names"].get(stn, stn),
                    "arrival": str(arrival).strip() if arrival else None,
                    "departure": str(departure).strip() if departure else None,
                    "day": 0,
                })

        if len(corridor_stops) < 2:
            logger.warning(f"  [Train] {train_num}: insufficient corridor stop data, skipping")
            continue

        # Sort corridor_stops chronologically so Origin is first and Destination is last
        def _parse_hhmm(s):
            if not s or str(s).strip() in ('None', '--', 'N/A', ''): return None
            p = str(s).strip().split(':')
            return int(p[0])*60 + int(p[1]) if len(p)>=2 else None

        _valid = [s for s in corridor_stops if (s.get('departure') or s.get('arrival')) not in (None, '', 'None', '--')]
        if len(_valid) >= 2:
            _times = [_parse_hhmm(s.get('departure') or s.get('arrival')) for s in _valid]
            if all(t is not None for t in _times):
                dur_fwd = sum((_times[i+1] - _times[i] + 1440) % 1440 for i in range(len(_times)-1))
                dur_rev = sum((_times[i] - _times[i+1] + 1440) % 1440 for i in range(len(_times)-1))
                if dur_rev < dur_fwd:
                    corridor_stops = list(reversed(corridor_stops))

        # Get train meta from any station record
        train_name = train_num
        train_type = "EXP"
        run_days = []
        for s in key_stations:
            rec = station_train_records.get(s, {}).get(train_num, {})
            name = (
                rec.get("trainName") or rec.get("train_name") or rec.get("name") or
                (rec.get("train") or {}).get("name") if isinstance(rec.get("train"), dict) else None
            )
            if name and str(name).strip() and str(name).strip() != train_num:
                train_name = str(name).strip()
                train_type = str(
                    rec.get("trainType") or rec.get("train_type") or
                    (rec.get("train") or {}).get("type") if isinstance(rec.get("train"), dict) else "EXP"
                ).strip()
                run_days = extract_run_days(rec)
                break

        if not run_days:
            first_rec = next(
                (station_train_records[s][train_num] for s in key_stations
                 if train_num in station_train_records.get(s, {})),
                {}
            )
            run_days = extract_run_days(first_rec)

        source = corridor_stops[0]["station_code"]
        dest = corridor_stops[-1]["station_code"]

        conn = None
        try:
            conn = get_db()
            conn.execute("""
                INSERT OR REPLACE INTO corridor_trains
                (corridor_id, train_number, train_name, train_type, source_code, dest_code,
                 run_days, corridor_stops, computed_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                corridor_id, train_num, str(train_name), str(train_type),
                source, dest,
                json.dumps(run_days),
                json.dumps(corridor_stops),
                now
            ))
            conn.commit()
            saved_count += 1
            logger.info(f"  [Train] {train_num} ({train_name}): {len(corridor_stops)} stops saved")
        except Exception as e:
            logger.warning(f"  [Train] {train_num}: DB save error: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    logger.info(f"[Corridor] {corridor_id}: ✓ {saved_count} corridor trains saved")
    return saved_count


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fetch RailRadar corridor train data")
    parser.add_argument("--corridor", help="Fetch only this corridor ID (e.g. hwh_bwn_main)")
    parser.add_argument("--dry-run", action="store_true", help="Plan only, no API calls")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if recently cached")
    parser.add_argument("--fetch-timetables", action="store_true", help="Also hit API to fetch complete multi-stop timetables for each train")
    args = parser.parse_args()

    client = RailRadarClient()
    init_db()

    corridors_to_fetch = CORRIDORS
    if args.corridor:
        if args.corridor not in CORRIDORS:
            print(f"Unknown corridor: {args.corridor}. Options: {list(CORRIDORS.keys())}")
            sys.exit(1)
        corridors_to_fetch = {args.corridor: CORRIDORS[args.corridor]}

    total = 0
    for cid, cfg in corridors_to_fetch.items():
        count = fetch_corridor(client, cid, cfg, dry_run=args.dry_run, force=args.force, fetch_timetables=args.fetch_timetables)
        total += count

    print(f"\n{'='*60}")
    print(f"  FETCH COMPLETE: {total} corridor trains saved across {len(corridors_to_fetch)} corridors")
    print(f"  Database: {BASE_DIR / 'train_cache.db'}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
