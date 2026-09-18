import sqlite3
import json
import csv
from pathlib import Path

DB_PATH = Path("train_cache.db")
JSON_PATH = Path("simulated_trains.json")
CSV_PATH = Path("simulated_trains_delay_analysis.csv")

def run_sync():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 1. Check and remove TRAIN ON DEMAND
    c.execute("SELECT COUNT(id) FROM corridor_trains WHERE train_type = 'TRAIN ON DEMAND'")
    tod_count = c.fetchone()[0]
    print(f"TRAIN ON DEMAND records found: {tod_count}")
    if tod_count > 0:
        c.execute("DELETE FROM corridor_trains WHERE train_type = 'TRAIN ON DEMAND'")
        conn.commit()
        print(f"Deleted {tod_count} TRAIN ON DEMAND records from corridor_trains.")

    # 2. Add column train_gap_coverage_potential if not present
    c.execute("PRAGMA table_info(corridor_trains)")
    cols = [col[1] for col in c.fetchall()]
    if 'train_gap_coverage_potential' not in cols:
        c.execute("ALTER TABLE corridor_trains ADD COLUMN train_gap_coverage_potential TEXT DEFAULT ''")
        conn.commit()
        print("Added 'train_gap_coverage_potential' column to corridor_trains table.")
    else:
        print("'train_gap_coverage_potential' column already exists in corridor_trains.")

    # 3. Fetch all trains in exact DB structure
    c.execute("""
        SELECT 
            corridor_id,
            train_number,
            train_name,
            train_type,
            source_code,
            dest_code,
            run_days,
            corridor_stops,
            train_gap_coverage_potential
        FROM corridor_trains
        ORDER BY corridor_id, train_number
    """)
    rows = c.fetchall()
    print(f"Total operational trains in DB: {len(rows)}")

    json_records = []
    csv_rows = []

    for row in rows:
        (corridor_id, train_number, train_name, train_type,
         source_code, dest_code, run_days_raw, corridor_stops_raw,
         train_gap_coverage_potential) = row

        # Parse JSON fields if valid, else keep raw
        try:
            run_days = json.loads(run_days_raw) if run_days_raw else []
        except Exception:
            run_days = run_days_raw

        try:
            corridor_stops = json.loads(corridor_stops_raw) if corridor_stops_raw else []
        except Exception:
            corridor_stops = corridor_stops_raw

        potential = train_gap_coverage_potential if train_gap_coverage_potential else ""

        record = {
            "corridor_id": corridor_id,
            "train_number": train_number,
            "train_name": train_name,
            "train_type": train_type,
            "source_code": source_code,
            "dest_code": dest_code,
            "run_days": run_days,
            "corridor_stops": corridor_stops,
            "train_gap_coverage_potential": potential
        }
        json_records.append(record)

        # Build readable summary for CSV corridor_stops
        if isinstance(corridor_stops, list) and corridor_stops:
            stop_summaries = []
            for s in corridor_stops:
                code = s.get("station_code", "")
                arr = s.get("arrival") or "--"
                dep = s.get("departure") or "--"
                stop_summaries.append(f"{code}({arr}/{dep})")
            stops_str = " -> ".join(stop_summaries)
        else:
            stops_str = str(corridor_stops_raw)

        run_days_str = ",".join(run_days) if isinstance(run_days, list) else str(run_days)

        csv_rows.append({
            "train_number": train_number,
            "train_name": train_name,
            "train_type": train_type,
            "corridor_id": corridor_id,
            "source_code": source_code,
            "dest_code": dest_code,
            "run_days": run_days_str,
            "corridor_stops": stops_str,
            "train_gap_coverage_potential": potential
        })

    # Save to JSON
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(json_records, f, indent=2, ensure_ascii=False)
    print(f"Saved clean dataset to {JSON_PATH} ({len(json_records)} records)")

    # Save to CSV
    csv_fieldnames = [
        "train_number",
        "train_name",
        "train_type",
        "corridor_id",
        "source_code",
        "dest_code",
        "run_days",
        "corridor_stops",
        "train_gap_coverage_potential"
    ]
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)
    print(f"Saved clean analysis CSV to {CSV_PATH} ({len(csv_rows)} rows)")

    conn.close()

if __name__ == "__main__":
    run_sync()
