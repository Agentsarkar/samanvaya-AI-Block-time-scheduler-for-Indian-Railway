import json
import sqlite3
from pathlib import Path
from collections import defaultdict

from block_scheduler import get_train_corridor_window, min_to_hhmm, _priority
from train_position_calculator import sort_stops_chronologically

DB_FILE = Path(__file__).parent / "train_cache.db"
OUTPUT_FILE = Path(__file__).parent / "gap_schedules.json"

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_SHORT = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed",
    "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"
}

def load_trains_from_db():
    if not DB_FILE.exists():
        print("No DB file found.")
        return []
    
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM corridor_trains WHERE train_type != 'TRAIN ON DEMAND'").fetchall()
    conn.close()
    
    trains = []
    for row in rows:
        try:
            stops = json.loads(row["corridor_stops"] or "[]")
            run_days = json.loads(row["run_days"] or "[]")
            if not stops:
                continue
                
            trains.append({
                "corridor_id": row["corridor_id"],
                "train_number": row["train_number"],
                "train_name": row["train_name"] or "",
                "train_type": row["train_type"] or "",
                "source_code": row["source_code"] or "",
                "dest_code": row["dest_code"] or "",
                "run_days": run_days,
                "corridor_stops": stops,
            })
        except Exception as e:
            print(f"Error processing train {row['train_number']}: {e}")
            pass
    return trains

def compute_gaps_for_direction(trains_list, day_short, min_gap_duration=30):
    active = []
    for t in trains_list:
        run_days = t.get("run_days", [])
        run_norm = [d[:3].lower() for d in run_days]
        if not run_norm or day_short in run_norm:
            # Sort chronologically to properly handle midnight crossovers
            ordered, _ = sort_stops_chronologically(t["corridor_stops"])
            if not ordered:
                dep_min, arr_min = get_train_corridor_window(t["corridor_stops"])
            else:
                first = ordered[0]
                last = ordered[-1]
                # Try to parse string times
                from train_position_calculator import parse_hhmm
                dep_min = parse_hhmm(first.get("departure") or first.get("arrival"))
                arr_min = parse_hhmm(last.get("arrival") or last.get("departure"))
                
                if dep_min is None or arr_min is None:
                    dep_min, arr_min = get_train_corridor_window(t["corridor_stops"])
            
            if dep_min is not None and arr_min is not None:
                # If arrival is next day because of crossover, arr_min will naturally be less than dep_min
                # Wait, parse_hhmm just returns 0-1439. If it crossed midnight, arr_min < dep_min.
                # However we need to be careful if train takes > 24 hrs which is not the case for these short corridors.
                if arr_min < dep_min:
                    arr_min += 1440
                active.append({
                    "train_number": t["train_number"],
                    "train_name": t["train_name"],
                    "train_type": t["train_type"],
                    "priority": _priority(t["train_type"]),
                    "dep_min": dep_min,
                    "arr_min": arr_min,
                })

    active.sort(key=lambda x: x["dep_min"])

    # Build occupied intervals (merged)
    occupied = []
    for t in active:
        if occupied and t["dep_min"] <= occupied[-1][1]:
            occupied[-1] = (occupied[-1][0], max(occupied[-1][1], t["arr_min"]))
        else:
            occupied.append((t["dep_min"], t["arr_min"]))

    # Free gaps = inverted occupied
    free_gaps = []
    prev_end = 0
    for (start, end) in occupied:
        if start > prev_end:
            gap_dur = start - prev_end
            if gap_dur >= min_gap_duration:
                free_gaps.append({
                    "gap_start": min_to_hhmm(prev_end),
                    "gap_end": min_to_hhmm(start),
                    "gap_start_min": prev_end,
                    "gap_end_min": start,
                    "gap_duration_min": gap_dur,
                })
        prev_end = max(prev_end, end)
        
    if prev_end < 1440:
        gap_dur = 1440 - prev_end
        if gap_dur >= min_gap_duration:
            free_gaps.append({
                "gap_start": min_to_hhmm(prev_end),
                "gap_end": "24:00",
                "gap_start_min": prev_end,
                "gap_end_min": 1440,
                "gap_duration_min": gap_dur,
            })
            
    return free_gaps, active

def calculate_all_gap_schedules():
    trains = load_trains_from_db()
    print(f"Loaded {len(trains)} trains from DB.")
    
    # Group by corridor, then direction
    corridor_groups = defaultdict(lambda: {"UP": [], "DOWN": []})
    
    for t in trains:
        cid = t["corridor_id"]
        
        # Calculate direction using source and destination codes
        dest = t.get("dest_code", "")
        orig = t.get("source_code", "")
        
        # DOWN = Towards Howrah/Sealdah (Inbound)
        # UP = Away from Howrah/Sealdah (Outbound)
        is_inbound = False
        
        # Explicit checks
        if dest in ("HWH", "SDAH"):
            is_inbound = True
        elif orig in ("HWH", "SDAH"):
            is_inbound = False
        # If neither origin/dest is HWH/SDAH, guess based on major outward stations
        elif orig in ("KNJ", "BWN", "ASN", "SKG", "RHA", "KWAE", "BHP"):
            is_inbound = True
        elif dest in ("KNJ", "BWN", "ASN", "SKG", "RHA", "KWAE", "BHP"):
            is_inbound = False
        
        direction = "DOWN" if is_inbound else "UP"
        corridor_groups[cid][direction].append(t)
            
    results = {}
    
    for cid, dirs in corridor_groups.items():
        results[cid] = {"UP": {}, "DOWN": {}}
        for direction, t_list in dirs.items():
            for day in DAYS:
                day_short = DAY_SHORT[day]
                gaps, active_trains = compute_gaps_for_direction(t_list, day_short, min_gap_duration=30)
                
                results[cid][direction][day] = {
                    "gaps": gaps,
                    "trains": active_trains
                }
                
    # Save to JSON
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
        
    print(f"Successfully calculated and saved gap schedules to {OUTPUT_FILE.name}")
    return results

if __name__ == "__main__":
    calculate_all_gap_schedules()
