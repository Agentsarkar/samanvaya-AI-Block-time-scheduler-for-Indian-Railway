"""
read_simulated_trains.py — Tool to inspect, export, and sync simulated trains and gap coverage potential.

Workflow:
1. View summary / inspect trains:
   python read_simulated_trains.py
   python read_simulated_trains.py --train 31824

2. Export clean CSV to fill 'train_gap_coverage_potential' in Excel:
   python read_simulated_trains.py --export-csv

3. Once you fill in 'train_gap_coverage_potential' in the CSV, sync it back to the DB & JSON:
   python read_simulated_trains.py --import-csv
"""

import json
import csv
import sqlite3
import argparse
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows terminal
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "train_cache.db"
JSON_FILE = BASE_DIR / "simulated_trains.json"
CSV_FILE = BASE_DIR / "simulated_trains_delay_analysis.csv"


def load_from_db():
    """Reads all operational trains directly from train_cache.db (excluding TRAIN ON DEMAND)."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # Ensure train_gap_coverage_potential column exists
    c.execute("PRAGMA table_info(corridor_trains)")
    cols = [col[1] for col in c.fetchall()]
    if "train_gap_coverage_potential" not in cols:
        c.execute("ALTER TABLE corridor_trains ADD COLUMN train_gap_coverage_potential TEXT DEFAULT ''")
        conn.commit()

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
        WHERE train_type != 'TRAIN ON DEMAND'
        ORDER BY corridor_id, train_number
    """)
    rows = c.fetchall()
    conn.close()

    trains = []
    for row in rows:
        (corridor_id, train_number, train_name, train_type,
         source_code, dest_code, run_days_raw, corridor_stops_raw,
         potential) = row

        try:
            run_days = json.loads(run_days_raw) if run_days_raw else []
        except Exception:
            run_days = run_days_raw

        try:
            corridor_stops = json.loads(corridor_stops_raw) if corridor_stops_raw else []
        except Exception:
            corridor_stops = corridor_stops_raw

        trains.append({
            "corridor_id": corridor_id,
            "train_number": str(train_number),
            "train_name": train_name or "",
            "train_type": train_type or "",
            "source_code": source_code or "",
            "dest_code": dest_code or "",
            "run_days": run_days,
            "corridor_stops": corridor_stops,
            "train_gap_coverage_potential": potential or ""
        })
    return trains


def save_to_json(trains, json_path=JSON_FILE):
    """Saves trains list to simulated_trains.json."""
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(trains, f, indent=2, ensure_ascii=False)
    print(f" Saved {len(trains)} trains to {json_path}")


def export_to_csv(csv_path=CSV_FILE):
    """Exports DB trains into CSV for Excel editing."""
    trains = load_from_db()

    fieldnames = [
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

    csv_rows = []
    for t in trains:
        stops = t.get("corridor_stops", [])
        if isinstance(stops, list) and stops:
            stop_str = " -> ".join(
                f"{s.get('station_code')}({s.get('arrival') or '--'}/{s.get('departure') or '--'})"
                for s in stops
            )
        else:
            stop_str = str(stops)

        run_days = t.get("run_days", [])
        days_str = ",".join(run_days) if isinstance(run_days, list) else str(run_days)

        csv_rows.append({
            "train_number": t.get("train_number"),
            "train_name": t.get("train_name"),
            "train_type": t.get("train_type"),
            "corridor_id": t.get("corridor_id"),
            "source_code": t.get("source_code"),
            "dest_code": t.get("dest_code"),
            "run_days": days_str,
            "corridor_stops": stop_str,
            "train_gap_coverage_potential": t.get("train_gap_coverage_potential", "")
        })

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)

    print(f" Successfully exported {len(csv_rows)} trains to CSV: {csv_path}")
    print(" -> Edit 'train_gap_coverage_potential' column in Excel, save, and run: python read_simulated_trains.py --import-csv")


def import_from_csv(csv_path=CSV_FILE):
    """
    Reads the user's updated CSV with 'train_gap_coverage_potential'
    and updates BOTH train_cache.db and simulated_trains.json.
    """
    if not Path(csv_path).exists():
        print(f"Error: CSV file '{csv_path}' not found.")
        return

    csv_updates = {}
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            t_num = str(row.get("train_number", "")).strip()
            c_id = str(row.get("corridor_id", "")).strip()
            potential = str(row.get("train_gap_coverage_potential", "")).strip()
            if t_num:
                csv_updates[(t_num, c_id)] = potential

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    updated_db_count = 0
    for (t_num, c_id), potential in csv_updates.items():
        if potential != "":
            # Try exact match first, then leading zero normalized match (for Excel-modified numbers like '763' vs '00763')
            clean_num = t_num.lstrip('0') or '0'
            if c_id:
                c.execute("""
                    UPDATE corridor_trains 
                    SET train_gap_coverage_potential = ? 
                    WHERE (train_number = ? OR train_number = ? OR ltrim(train_number, '0') = ?) 
                      AND corridor_id = ?
                """, (potential, t_num, t_num.zfill(5), clean_num, c_id))
            else:
                c.execute("""
                    UPDATE corridor_trains 
                    SET train_gap_coverage_potential = ? 
                    WHERE (train_number = ? OR train_number = ? OR ltrim(train_number, '0') = ?)
                """, (potential, t_num, t_num.zfill(5), clean_num))
            updated_db_count += c.rowcount

    conn.commit()
    conn.close()

    print(f" Updated database (train_cache.db): {updated_db_count} records received potential values.")

    # Re-sync JSON with the updated database values
    fresh_trains = load_from_db()
    save_to_json(fresh_trains)
    print(f" Synced updated values into {JSON_FILE}")


def display_summary():
    """Prints a clean summary of simulated trains and potential coverage status."""
    trains = load_from_db()
    total = len(trains)
    unique_nums = len(set(t["train_number"] for t in trains))
    filled_count = sum(1 for t in trains if t.get("train_gap_coverage_potential", "").strip() != "")

    print("\n" + "=" * 65)
    print(" SAMANVAY RAILWAY DSS — SIMULATED TRAINS DATASET")
    print("=" * 65)
    print(f" Operational Trains (DB & JSON) : {total}")
    print(f" Unique Train Numbers           : {unique_nums}")
    print(f" Gap Coverage Potential Filled   : {filled_count} / {total}")
    print(f" Pending Manual Entries          : {total - filled_count}")
    print("-" * 65)

    corridors = {}
    for t in trains:
        cid = t.get("corridor_id", "unknown")
        corridors[cid] = corridors.get(cid, 0) + 1

    print("Trains By Corridor:")
    for cid, cnt in sorted(corridors.items(), key=lambda x: x[1], reverse=True):
        print(f"  • {cid:<22} : {cnt} trains")
    print("=" * 65 + "\n")


def display_train(train_number):
    """Displays exact details of a train by its number."""
    trains = load_from_db()
    matches = [t for t in trains if str(t.get("train_number")).strip() == str(train_number).strip()]
    if not matches:
        print(f"No operational train found with number '{train_number}'.")
        return

    for i, t in enumerate(matches, 1):
        print(f"\n--- Train #{t['train_number']} ({t['corridor_id']}) ---")
        print(f"Name                         : {t['train_name']}")
        print(f"Type                         : {t['train_type']}")
        print(f"Source -> Dest               : {t['source_code']} -> {t['dest_code']}")
        print(f"Run Days                     : {', '.join(t['run_days'])}")
        print(f"Train Gap Coverage Potential : '{t['train_gap_coverage_potential']}'")
        print("Corridor Stops:")
        for s in t.get("corridor_stops", []):
            arr = s.get("arrival") or "--"
            dep = s.get("departure") or "--"
            print(f"  [{s.get('station_code')}] {s.get('station_name'):<26} Arr: {arr:<5} | Dep: {dep:<5}")


def main():
    parser = argparse.ArgumentParser(description="Simulated Trains and Delay Gap Coverage Tool")
    parser.add_argument("--export-csv", action="store_true", help="Export dataset to CSV for editing in Excel")
    parser.add_argument("--import-csv", action="store_true", help="Import filled CSV back into train_cache.db & JSON")
    parser.add_argument("--train", type=str, help="View train details by train number")
    parser.add_argument("--sync", action="store_true", help="Sync JSON directly from database")

    args = parser.parse_args()

    if args.export_csv:
        export_to_csv()
    elif args.import_csv:
        import_from_csv()
    elif args.sync:
        trains = load_from_db()
        save_to_json(trains)
        export_to_csv()
    elif args.train:
        display_train(args.train)
    else:
        display_summary()


if __name__ == "__main__":
    main()
