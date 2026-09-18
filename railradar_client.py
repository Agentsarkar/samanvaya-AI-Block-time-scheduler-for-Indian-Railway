"""
railradar_client.py — RailRadar API Client with Key Rotation & SQLite Cache

Features:
- Automatic API key rotation on 429 (rate limit) errors
- If all keys exhausted: waits 60 s, then retries from key 0
- All successful responses saved to SQLite (train_cache.db) before returning
- Cache TTL: 24 hours for station board data, 48 hours for train timetables
- Respects 10 requests/min per key limit using timestamp queuing

Usage:
    client = RailRadarClient()
    data = client.get_station_board("HWH", include_intermediate=True)
    data = client.get_train_timetable("12301")
"""

import os
import json
import time
import sqlite3
import hashlib
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List

import httpx
from dotenv import load_dotenv

# Load .env from project directory
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("railradar")

# ─── Config ──────────────────────────────────────────────────────────────────

BASE_URL = os.getenv("RAILRADAR_BASE_URL", "https://api.railradar.in").rstrip("/")
RATE_LIMIT = int(os.getenv("RAILRADAR_RATE_LIMIT_PER_MIN", "10"))
DB_PATH = BASE_DIR / os.getenv("DB_PATH", "train_cache.db")

# Collect all API keys from env (RAILRADAR_API_KEY_1, _2, _3, ...)
def _load_api_keys() -> List[str]:
    keys = []
    i = 1
    while True:
        k = os.getenv(f"RAILRADAR_API_KEY_{i}")
        if not k:
            break
        keys.append(k.strip())
        i += 1
    if not keys:
        raise RuntimeError("No RAILRADAR_API_KEY_* found in .env — add at least RAILRADAR_API_KEY_1")
    return keys

IST = timezone(timedelta(hours=5, minutes=30))

# ─── Database Setup ───────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    """Returns a sqlite3 connection to the project-local train_cache.db."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    """Creates all required tables if they don't exist."""
    conn = get_db()
    c = conn.cursor()

    c.executescript("""
    CREATE TABLE IF NOT EXISTS api_cache (
        cache_key   TEXT PRIMARY KEY,
        endpoint    TEXT NOT NULL,
        params      TEXT,
        response    TEXT NOT NULL,
        fetched_at  TEXT NOT NULL,
        expires_at  TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS station_trains (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        station_code    TEXT NOT NULL,
        train_number    TEXT NOT NULL,
        train_name      TEXT,
        train_type      TEXT,
        source_code     TEXT,
        dest_code       TEXT,
        arrival_time    TEXT,
        departure_time  TEXT,
        arrival_day     INTEGER DEFAULT 0,
        departure_day   INTEGER DEFAULT 0,
        distance_km     INTEGER DEFAULT 0,
        stop_type       TEXT DEFAULT 'intermediate',
        run_days        TEXT DEFAULT '[]',
        fetched_at      TEXT NOT NULL,
        UNIQUE(station_code, train_number)
    );

    CREATE TABLE IF NOT EXISTS corridor_trains (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        corridor_id     TEXT NOT NULL,
        train_number    TEXT NOT NULL,
        train_name      TEXT,
        train_type      TEXT,
        source_code     TEXT,
        dest_code       TEXT,
        run_days        TEXT DEFAULT '[]',
        corridor_stops  TEXT,
        computed_at     TEXT NOT NULL,
        UNIQUE(corridor_id, train_number)
    );

    CREATE TABLE IF NOT EXISTS train_timetable (
        train_number    TEXT PRIMARY KEY,
        train_name      TEXT,
        train_type      TEXT,
        source_code     TEXT,
        dest_code       TEXT,
        run_days        TEXT DEFAULT '[]',
        stops           TEXT,
        fetched_at      TEXT NOT NULL,
        expires_at      TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_station_trains_stn ON station_trains(station_code);
    CREATE INDEX IF NOT EXISTS idx_corridor_trains_cid ON corridor_trains(corridor_id);
    """)

    conn.commit()
    conn.close()
    logger.info(f"[DB] Initialized: {DB_PATH}")


# ─── RailRadar Client ─────────────────────────────────────────────────────────

class RailRadarClient:
    """
    Thread-safe HTTP client for RailRadar API with:
    - Multi-key rotation on 429
    - Per-key rate tracking (10 req/min)
    - SQLite caching (24h TTL for station boards, 48h for timetables)
    """

    def __init__(self):
        self.keys: List[str] = _load_api_keys()
        self.key_index: int = 0
        # Per-key sliding window: deque of timestamps (keep last RATE_LIMIT entries)
        self.key_timestamps: Dict[int, deque] = {i: deque(maxlen=RATE_LIMIT) for i in range(len(self.keys))}
        logger.info(f"[RailRadar] Loaded {len(self.keys)} API key(s), base URL: {BASE_URL}")
        init_db()

    # ── Current key ──────────────────────────────────────────────────────────

    @property
    def current_key(self) -> str:
        return self.keys[self.key_index]

    def _rotate_key(self):
        old = self.key_index
        self.key_index = (self.key_index + 1) % len(self.keys)
        logger.warning(f"[RailRadar] Rotated key from #{old} to #{self.key_index}")

    def _all_keys_rate_limited(self) -> bool:
        """Returns True if ALL keys have 10+ requests in the last 60 seconds."""
        now = time.time()
        for i in range(len(self.keys)):
            ts = self.key_timestamps[i]
            recent = [t for t in ts if now - t < 60.0]
            if len(recent) < RATE_LIMIT:
                return False
        return True

    def _throttle_if_needed(self):
        """Wait if current key has hit its per-minute limit."""
        now = time.time()
        ts = self.key_timestamps[self.key_index]
        recent = [t for t in ts if now - t < 60.0]
        if len(recent) >= RATE_LIMIT:
            wait = 60.0 - (now - min(recent)) + 0.5
            logger.info(f"[RailRadar] Key #{self.key_index} rate window full, waiting {wait:.1f}s …")
            time.sleep(wait)

    def _record_request(self):
        self.key_timestamps[self.key_index].append(time.time())

    # ── Cache helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _cache_key(endpoint: str, params: dict) -> str:
        raw = endpoint + json.dumps(params, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _get_cached(self, cache_key: str) -> Optional[Dict[str, Any]]:
        conn = get_db()
        row = conn.execute(
            "SELECT response, expires_at FROM api_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        conn.close()
        if not row:
            return None
        expires = datetime.fromisoformat(row["expires_at"])
        if datetime.now(IST) > expires:
            logger.debug(f"[Cache] Expired: {cache_key}")
            return None
        logger.info(f"[Cache] HIT: {cache_key}")
        return json.loads(row["response"])

    def _save_cache(self, cache_key: str, endpoint: str, params: dict, response: dict, ttl_hours: int = 24):
        now = datetime.now(IST)
        expires = now + timedelta(hours=ttl_hours)
        for attempt in range(5):
            conn = None
            try:
                conn = get_db()
                conn.execute("""
                    INSERT OR REPLACE INTO api_cache (cache_key, endpoint, params, response, fetched_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (cache_key, endpoint, json.dumps(params), json.dumps(response), now.isoformat(), expires.isoformat()))
                conn.commit()
                logger.info(f"[Cache] SAVED: {cache_key} (TTL {ttl_hours}h)")
                return
            except Exception as e:
                logger.warning(f"[Cache] Save attempt {attempt+1} failed: {e}")
                time.sleep(1 + attempt)
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
        logger.error(f"[Cache] Failed to save {cache_key} after 5 attempts")


    # ── Core request ──────────────────────────────────────────────────────────

    def _get(self, endpoint: str, params: dict = None, ttl_hours: int = 24, max_retries: int = 20) -> Dict[str, Any]:
        """
        Makes a GET request to BASE_URL/{endpoint} with key rotation on 429.
        Checks cache first, saves successful responses to cache.
        Waits up to 60s if all keys are rate-limited, retries up to max_retries times.
        """
        params = params or {}
        ck = self._cache_key(endpoint, params)
        cached = self._get_cached(ck)
        if cached is not None:
            return cached

        url = f"{BASE_URL}/{endpoint.lstrip('/')}"
        retries = 0

        while retries < max_retries:
            # If all keys rate limited, wait a full minute
            if self._all_keys_rate_limited():
                logger.warning("[RailRadar] All keys rate-limited — waiting 60s …")
                time.sleep(61)

            self._throttle_if_needed()

            headers = {
                "x-api-key": self.current_key,
                "Accept": "application/json",
                "User-Agent": "SAMANVAY-DSS/1.0"
            }

            try:
                self._record_request()
                logger.info(f"[RailRadar] GET {url} params={params} key=#{self.key_index}")
                with httpx.Client(timeout=30.0) as http:
                    resp = http.get(url, params=params, headers=headers)

                if resp.status_code == 200:
                    data = resp.json()
                    self._save_cache(ck, endpoint, params, data, ttl_hours)
                    return data

                elif resp.status_code == 429:
                    logger.warning(f"[RailRadar] 429 on key #{self.key_index} — rotating …")
                    self._rotate_key()
                    retries += 1
                    time.sleep(1)

                elif resp.status_code == 404:
                    logger.warning(f"[RailRadar] 404 for {url}: Not found")
                    return {"error": "not_found", "status": 404}

                else:
                    logger.error(f"[RailRadar] HTTP {resp.status_code} for {url}: {resp.text[:200]}")
                    retries += 1
                    time.sleep(2)

            except (httpx.ConnectError, httpx.TimeoutException) as e:
                logger.error(f"[RailRadar] Network error: {e}")
                retries += 1
                time.sleep(5)

        raise RuntimeError(f"[RailRadar] Failed after {max_retries} retries for {endpoint}")

    # ── Public API Methods ────────────────────────────────────────────────────

    def get_station_board(self, station_code: str, include_intermediate: bool = True) -> Dict[str, Any]:
        """
        GET /v1/stations/{code}/trains
        Returns all trains at this station (halting + non-halting if include_intermediate=True).
        Cached for 24 hours.

        Response format: { success, data: { station, trains: [{train:{...}, stop:{...}}, ...], count }, meta }
        """
        endpoint = f"v1/stations/{station_code.upper()}/trains"
        params = {}
        if include_intermediate:
            params["includeIntermediate"] = "true"
        raw = self._get(endpoint, params, ttl_hours=24)

        # Normalize to a flat list: extract data.data.trains (each item = {train, stop})
        trains_raw = (
            raw.get("data", {}).get("trains")
            or raw.get("trains")
            or raw.get("data")
            or []
        )
        if isinstance(trains_raw, dict):
            # Sometimes data itself is the trains object
            trains_raw = list(trains_raw.values())

        # Normalize each item: flatten {train:{...}, stop:{...}} into a single flat dict
        trains_flat = []
        for item in trains_raw:
            if isinstance(item, dict) and "train" in item:
                t = item["train"]
                s = item.get("stop", {})
                trains_flat.append({
                    "trainNumber":   t.get("number", ""),
                    "trainName":     t.get("name", ""),
                    "trainType":     t.get("type", ""),
                    "sourceCode":    t.get("source", {}).get("code", "") if isinstance(t.get("source"), dict) else t.get("source", ""),
                    "destCode":      t.get("destination", {}).get("code", "") if isinstance(t.get("destination"), dict) else t.get("destination", ""),
                    "runDays":       t.get("runDays", []),
                    "arrival":       s.get("arrival"),
                    "departure":     s.get("departure"),
                    "arrivalDay":    s.get("arrivalDay", 0),
                    "departureDay":  s.get("departureDay", 0),
                    "distance":      s.get("distance", 0),
                    "stopType":      s.get("stopType", "intermediate"),
                })
            elif isinstance(item, dict):
                trains_flat.append(item)

        # Save to station_trains DB
        if trains_flat:
            self._save_station_trains(station_code.upper(), trains_flat)

        # Return normalized data
        return {"trains": trains_flat, "station_code": station_code.upper(), "count": len(trains_flat)}

    def get_train_timetable(self, train_number: str) -> Dict[str, Any]:
        """
        GET /v1/trains/{number}
        Returns full stop-by-stop timetable for a train.
        Cached for 48 hours.

        Response format: { success, data: { train:{...}, stops:[{...},...] }, meta }
        """
        endpoint = f"v1/trains/{train_number}"
        raw = self._get(endpoint, {}, ttl_hours=48)

        # Normalize: extract data field if nested
        if "data" in raw and isinstance(raw["data"], dict):
            data = raw["data"]
            # Stops may be under data.stops or data.schedule
            stops_raw = data.get("stops") or data.get("schedule") or []
            train_info = data.get("train") or {}
            normalized = {
                "trainNumber": train_info.get("number") or train_number,
                "trainName":   train_info.get("name") or data.get("name") or "",
                "trainType":   train_info.get("type") or data.get("type") or "",
                "sourceCode":  (train_info.get("source", {}) or {}).get("code") or "",
                "destCode":    (train_info.get("destination", {}) or {}).get("code") or "",
                "runDays":     train_info.get("runDays") or data.get("runDays") or [],
                "stops":       stops_raw,
            }
        else:
            normalized = raw

        if normalized and "error" not in normalized:
            self._save_train_timetable(train_number, normalized)

        return normalized

    # ── DB Persistence Helpers ────────────────────────────────────────────────

    def _save_station_trains(self, station_code: str, trains: List[dict]):
        """Upserts train records into station_trains table."""
        if not trains:
            return
        conn = get_db()
        now = datetime.now(IST).isoformat()
        for t in trains:
            num = str(t.get("trainNumber") or t.get("train_number") or t.get("number") or "")
            if not num:
                continue
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO station_trains
                    (station_code, train_number, train_name, train_type, source_code, dest_code,
                     arrival_time, departure_time, arrival_day, departure_day, distance_km,
                     stop_type, run_days, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    station_code,
                    num,
                    t.get("trainName") or t.get("train_name") or t.get("name") or "",
                    t.get("trainType") or t.get("train_type") or t.get("type") or "",
                    t.get("sourceCode") or t.get("source") or "",
                    t.get("destCode") or t.get("destination") or t.get("dest") or "",
                    t.get("arrival") or t.get("arrivalTime") or t.get("arrival_time") or "",
                    t.get("departure") or t.get("departureTime") or t.get("departure_time") or "",
                    t.get("arrivalDay") or t.get("arrival_day") or 0,
                    t.get("departureDay") or t.get("departure_day") or 0,
                    t.get("distance") or t.get("distance_km") or 0,
                    t.get("stopType") or t.get("stop_type") or "intermediate",
                    json.dumps(t.get("runDays") or t.get("run_days") or []),
                    now
                ))
            except Exception as e:
                logger.warning(f"[DB] station_trains insert error for {num}: {e}")
        conn.commit()
        conn.close()
        logger.info(f"[DB] Saved {len(trains)} trains for station {station_code}")

    def _save_train_timetable(self, train_number: str, data: dict):
        """Upserts a train's full timetable into train_timetable table."""
        conn = get_db()
        now = datetime.now(IST).isoformat()
        expires = (datetime.now(IST) + timedelta(hours=48)).isoformat()
        # Normalize stops field
        stops = data.get("stops") or data.get("schedule") or data.get("stations") or []
        try:
            conn.execute("""
                INSERT OR REPLACE INTO train_timetable
                (train_number, train_name, train_type, source_code, dest_code, run_days, stops, fetched_at, expires_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                str(train_number),
                data.get("trainName") or data.get("name") or "",
                data.get("trainType") or data.get("type") or "",
                data.get("sourceCode") or data.get("source") or "",
                data.get("destCode") or data.get("destination") or "",
                json.dumps(data.get("runDays") or data.get("run_days") or []),
                json.dumps(stops),
                now,
                expires
            ))
            conn.commit()
        except Exception as e:
            logger.warning(f"[DB] train_timetable insert error for {train_number}: {e}")
        conn.close()
