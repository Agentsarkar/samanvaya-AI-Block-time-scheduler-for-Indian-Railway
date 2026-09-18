import os
import secrets
import math
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
from pathlib import Path

from fastapi import FastAPI, Request, Response, HTTPException, status, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Load environment variables from .env
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass  # python-dotenv not installed; env vars must be set manually

# ─── IST helpers ─────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))
DAY_NAMES = ["sunday","monday","tuesday","wednesday","thursday","friday","saturday"]

def get_sim_time() -> str:
    """Returns current IST time as HH:MM, respecting SIMULATE_TIME env override."""
    override = os.getenv("SIMULATE_TIME", "").strip()
    if override and ":" in override:
        return override
    now_ist = datetime.now(IST)
    return now_ist.strftime("%H:%M")

def get_sim_day() -> str:
    """Returns current IST weekday name (lowercase), respecting SIMULATE_DAY env override."""
    override = os.getenv("SIMULATE_DAY", "").strip().lower()
    if override and override in DAY_NAMES:
        return override
    now_ist = datetime.now(IST)
    return DAY_NAMES[now_ist.weekday() + 1 if now_ist.weekday() < 6 else 0]  # Mon=0 in Python

def get_sim_day_correct() -> str:
    """Returns current IST weekday name using Python's isoweekday mapping."""
    override = os.getenv("SIMULATE_DAY", "").strip().lower()
    if override and override in DAY_NAMES:
        return override
    now_ist = datetime.now(IST)
    # Python weekday(): Monday=0, Sunday=6
    py_to_day = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
    return py_to_day[now_ist.weekday()]

# ─── SQLite helper ────────────────────────────────────────────────────────────
DB_PATH = Path(__file__).resolve().parent / os.getenv("DB_PATH", "train_cache.db")

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

BASE_DIR = Path(__file__).resolve().parent
USERS_FILE = BASE_DIR / "users.txt"

# In-memory session store: session_token -> user_dict
ACTIVE_SESSIONS: Dict[str, Dict[str, Any]] = {}

# Fallback hardcoded users
DEFAULT_USERS = {
    "7842901": {
        "employee_id": "7842901",
        "password": "railway@123",
        "name": "Rajesh Sharma",
        "role_id": "maintenance-planner",
        "role_title": "Divisional Engineer (Eastern Railway)",
        "division": "Howrah",
        "section": "HWH–BWN Main Line",
    },
    "7842902": {
        "employee_id": "7842902",
        "password": "railway@123",
        "name": "Priya Mukherjee",
        "role_id": "control-room",
        "role_title": "Control Room Dispatcher (Live DSS)",
        "division": "Howrah",
        "section": "HWH–BWN Main Line",
    },
    "7842903": {
        "employee_id": "7842903",
        "password": "railway@123",
        "name": "Amitav Sen",
        "role_id": "maintenance-planner",
        "role_title": "Senior Divisional Engineer (Coordination)",
        "division": "Sealdah",
        "section": "SDAH–RHA Line",
    },
    "admin": {
        "employee_id": "admin",
        "password": "admin123",
        "name": "Er. V. K. Verma",
        "role_id": "maintenance-planner",
        "role_title": "Divisional Engineer (Eastern Railway)",
        "division": "Howrah",
        "section": "HWH–BWN Main Line",
    },
}


def load_users() -> Dict[str, Dict[str, Any]]:
    """Loads users from users.txt with fallback to hardcoded records."""
    users = dict(DEFAULT_USERS)
    if not USERS_FILE.exists():
        return users

    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(":")]
                if len(parts) >= 5:
                    emp_id = parts[0]
                    users[emp_id] = {
                        "employee_id": emp_id,
                        "password": parts[1],
                        "name": parts[2],
                        "role_id": parts[3],
                        "role_title": parts[4],
                        "division": parts[5] if len(parts) > 5 else "Howrah",
                        "section": parts[6] if len(parts) > 6 else "HWH–BWN Main Line",
                    }
    except Exception as e:
        print(f"[WARN] Failed reading users.txt: {e}. Using fallback credentials.")

    return users


app = FastAPI(title="SAMANVAY - Railway DSS Backend", version="1.0.0")

# Mount static files
static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Initialize block scheduler DB tables on startup
try:
    from block_scheduler import ensure_db_tables
    ensure_db_tables()
except Exception as _bs_err:
    print(f"[WARN] block_scheduler table init: {_bs_err}")


class LoginRequest(BaseModel):
    employeeId: str
    password: str
    role: Optional[str] = None
    captcha: Optional[str] = None


@app.get("/railway.jpg")
async def get_railway_image():
    img_path = BASE_DIR / "railway.jpg"
    if img_path.exists():
        return FileResponse(str(img_path), media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Image not found")


@app.get("/", response_class=FileResponse)
async def serve_root(request: Request):
    # If not logged in, client-side script or session check can redirect, or direct serve index.html
    return FileResponse(str(BASE_DIR / "index.html"))


@app.get("/index.html", response_class=FileResponse)
async def serve_index():
    return FileResponse(str(BASE_DIR / "index.html"))


@app.get("/login", response_class=FileResponse)
@app.get("/login.html", response_class=FileResponse)
async def serve_login():
    return FileResponse(str(BASE_DIR / "login.html"))


@app.get("/map", response_class=FileResponse)
@app.get("/map.html", response_class=FileResponse)
async def serve_map():
    return FileResponse(str(BASE_DIR / "map.html"), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/block", response_class=FileResponse)
@app.get("/block.html", response_class=FileResponse)
async def serve_block():
    return FileResponse(str(BASE_DIR / "block.html"), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/degradation", response_class=FileResponse)
@app.get("/degradation.html", response_class=FileResponse)
async def serve_degradation():
    return FileResponse(str(BASE_DIR / "degradation.html"), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})



@app.get("/api/faults")
async def get_faults():
    faults_file = BASE_DIR / "faults.json"
    if faults_file.exists():
        return FileResponse(str(faults_file), media_type="application/json", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return {"corridor": "Howrah - Barddhaman", "faults": []}


@app.get("/api/network")
async def get_network():
    net_file = BASE_DIR / "corridor_network.json"
    if net_file.exists():
        return FileResponse(str(net_file), media_type="application/json", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    raise HTTPException(status_code=404, detail="Network data not found")


@app.get("/api/stations")
async def get_stations():
    stations_file = BASE_DIR / "all_stations.json"
    if stations_file.exists():
        return FileResponse(str(stations_file), media_type="application/json", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    raise HTTPException(status_code=404, detail="Stations data not found")


@app.get("/api/users")
async def get_available_users():
    """Returns available test users (without passwords) for demonstration ease."""
    users = load_users()
    demo_list = []
    for u in users.values():
        demo_list.append({
            "employeeId": u["employee_id"],
            "name": u["name"],
            "roleId": u["role_id"],
            "roleTitle": u["role_title"],
            "division": u["division"]
        })
    return {"users": demo_list}


@app.post("/api/login")
async def api_login(req: LoginRequest, response: Response):
    users = load_users()
    emp_id = req.employeeId.strip()
    pwd = req.password.strip()

    # Validate CAPTCHA
    # Login page shows RAIL 7842
    if req.captcha is not None:
        captcha_clean = req.captcha.strip().replace(" ", "").upper()
        # Accept 'RAIL7842' or '7842' or general variations
        if captcha_clean not in ["RAIL7842", "7842"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"success": False, "message": "Invalid Security Verification (CAPTCHA). Expected 'RAIL 7842'."}
            )

    if emp_id not in users:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"success": False, "message": f"Employee ID '{emp_id}' not found in railway directory."}
        )

    user = users[emp_id]
    if user["password"] != pwd:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"success": False, "message": "Incorrect password. Please verify your credentials."}
        )

    # If module/role selected, check compatibility or automatically sync
    selected_role = (req.role or "").strip()
    if selected_role and selected_role != user["role_id"]:
        # If user selected a different module, we can inform or adapt
        expected_name = "Maintenance Planner" if user["role_id"] == "maintenance-planner" else "Control Room Dispatcher"
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "success": False,
                "message": f"Authorization mismatch: {user['name']} is designated for the '{expected_name}' module."
            }
        )

    # Create session
    session_token = secrets.token_hex(24)
    ACTIVE_SESSIONS[session_token] = {
        "employeeId": user["employee_id"],
        "name": user["name"],
        "roleId": user["role_id"],
        "roleTitle": user["role_title"],
        "division": user["division"],
        "section": user["section"],
    }

    # Set cookie
    response.set_cookie(
        key="samanvay_session",
        value=session_token,
        httponly=False,  # readable by client JS for smooth state sync
        max_age=86400,
        samesite="lax"
    )

    return {
        "success": True,
        "token": session_token,
        "user": ACTIVE_SESSIONS[session_token],
        "redirect": "/index.html"
    }


def get_current_user_from_request(request: Request) -> Optional[Dict[str, Any]]:
    # 1. From Cookie
    token = request.cookies.get("samanvay_session")
    # 2. From Authorization header
    if not token:
        auth = request.headers.get("Authorization")
        if auth and auth.startswith("Bearer "):
            token = auth.split(" ")[1]

    if token and token in ACTIVE_SESSIONS:
        return ACTIVE_SESSIONS[token]
    return None


@app.get("/api/me")
async def api_me(request: Request):
    user = get_current_user_from_request(request)
    if not user:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"authenticated": False, "message": "No active session"}
        )
    return {"authenticated": True, "user": user}


@app.post("/api/logout")
async def api_logout(request: Request, response: Response):
    token = request.cookies.get("samanvay_session")
    if token and token in ACTIVE_SESSIONS:
        del ACTIVE_SESSIONS[token]

    response.delete_cookie("samanvay_session")
    return {"success": True, "message": "Logged out successfully"}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Computes great-circle distance between two points in kilometers."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0)**2
    return R * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def compute_simulated_megablocks(max_km: float = 15.0) -> Dict[str, Any]:
    """Clusters active corridor faults within a strict geographic limit (default: 15 km)."""
    faults_file = BASE_DIR / "faults.json"
    if not faults_file.exists():
        return {"success": False, "clusters": [], "unclustered": [], "max_km": max_km}

    with open(faults_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    faults = data.get("faults", [])

    # Group faults by corridor
    corridor_map: Dict[str, List[Dict[str, Any]]] = {}
    for f in faults:
        cid = f.get("corridor_id", "hwh_bwn_main")
        corridor_map.setdefault(cid, []).append(f)

    clusters = []
    unclustered = []
    cluster_idx = 1

    for cid, f_list in corridor_map.items():
        used = set()
        for i, f1 in enumerate(f_list):
            if i in used:
                continue
            group = [f1]
            used.add(i)
            for j, f2 in enumerate(f_list):
                if j in used:
                    continue
                # Strictly enforce that f2 is within max_km of all existing items in group
                if all(haversine_km(f2["lat"], f2["long"], m["lat"], m["long"]) <= max_km for m in group):
                    group.append(f2)
                    used.add(j)

            if len(group) >= 2:
                # Max distance between any two faults in this cluster
                max_d = 0.0
                for a in group:
                    for b in group:
                        d = haversine_km(a["lat"], a["long"], b["lat"], b["long"])
                        if d > max_d:
                            max_d = d

                lats = [m["lat"] for m in group]
                longs = [m["long"] for m in group]
                center = [round(sum(lats) / len(lats), 6), round(sum(longs) / len(longs), 6)]
                bounds = [
                    [round(min(lats) - 0.012, 6), round(min(longs) - 0.015, 6)],
                    [round(max(lats) + 0.012, 6), round(max(longs) + 0.015, 6)]
                ]

                durations = [m.get("required_block_duration_min", 60) for m in group]
                combined_dur = max(durations)
                time_saved = sum(durations) - combined_dur
                stn_start = group[0].get("nearest_station", "")
                stn_end = group[-1].get("nearest_station", "")
                depts = list(dict.fromkeys(m.get("category", m.get("department", "Track")) for m in group))

                _CORRIDOR_NAMES = {
                    "hwh_bwn_main":  "Howrah–Barddhaman Main Line",
                    "bwn_asn_trunk": "Bally–Barddhaman–Asansol Trunk",
                    "sdah_knj_line": "Sealdah–Krishnanagar Line",
                    "hwh_sgkh_line": "Howrah–Saktigarh Line",
                    "hwh_bwn_chord": "Howrah–Barddhaman Chord Line",
                }
                corridor_name = _CORRIDOR_NAMES.get(cid, cid)

                clusters.append({
                    "id": f"MB-SIM-{cluster_idx:02d}",
                    "name": f"{stn_start}–{stn_end} Integrated Mega-Block",
                    "corridor_id": cid,
                    "corridor_name": corridor_name,
                    "max_span_km": round(max_d, 2),
                    "limit_km": max_km,
                    "duration_min": combined_dur,
                    "time_saved_min": time_saved,
                    "window": f"00:30 – {combined_dur // 60:02d}:{combined_dur % 60:02d} IST ({combined_dur} min)",
                    "departments": depts,
                    "task_count": len(group),
                    "tasks": [
                        {
                            "code": m["task_code"],
                            "dept": m.get("category", m.get("department", "Track")),
                            "fault_type": m["fault_type"],
                            "nearest_station": m.get("nearest_station", ""),
                            "chainage": m.get("chainage", ""),
                            "duration_min": m.get("required_block_duration_min", 60),
                            "speed_restriction_kmph": m.get("speed_restriction_kmph", 20),
                            "lat": m["lat"],
                            "long": m["long"]
                        } for m in group
                    ],
                    "bounds": bounds,
                    "center": center,
                    "color": "#ef4444" if cid == "hwh_bwn_main" else "#a855f7"
                })
                cluster_idx += 1
            else:
                unclustered.append({
                    "code": f1["task_code"],
                    "dept": f1.get("category", f1.get("department", "Track")),
                    "fault_type": f1["fault_type"],
                    "nearest_station": f1.get("nearest_station", ""),
                    "chainage": f1.get("chainage", ""),
                    "duration_min": f1.get("required_block_duration_min", 60),
                    "corridor_id": cid,
                    "lat": f1["lat"],
                    "long": f1["long"],
                    "reason": f"Exceeds {max_km} km distance from nearest concurrent works on {cid}"
                })

    return {
        "success": True,
        "max_km": max_km,
        "clusters_count": len(clusters),
        "unclustered_count": len(unclustered),
        "clusters": clusters,
        "unclustered": unclustered
    }


@app.get("/api/simulate-megablocks")
@app.post("/api/simulate-megablocks")
async def api_simulate_megablocks(max_km: float = 15.0):
    """Simulates multi-department mega-blocks restricted by a customizable distance limit (default: 15 km)."""
    result = compute_simulated_megablocks(max_km=max_km)
    return JSONResponse(
        content=result,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )


# ─── Real Train API Endpoints ─────────────────────────────────────────────────

@app.get("/api/real-trains")
async def api_real_trains(corridor_id: Optional[str] = None):
    """
    Returns corridor-matched trains from the SQLite cache (populated by fetch_trains.py).
    Optionally filter by corridor_id.
    Also returns sim_config (current IST time, day, and any overrides) so the frontend
    can initialize the simulation engine with correct time context.
    """
    if not DB_PATH.exists():
        return JSONResponse(
            content={"trains": [], "sim_config": _build_sim_config(),
                     "message": "train_cache.db not found — run fetch_trains.py first"},
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
        )
    try:
        conn = get_db()
        if corridor_id:
            rows = conn.execute(
                "SELECT * FROM corridor_trains WHERE corridor_id = ? ORDER BY train_number",
                (corridor_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM corridor_trains ORDER BY corridor_id, train_number"
            ).fetchall()
        conn.close()
    except Exception as e:
        return JSONResponse(
            content={"trains": [], "error": str(e), "sim_config": _build_sim_config()},
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
        )

    trains = []
    for row in rows:
        try:
            trains.append({
                "corridor_id":     row["corridor_id"],
                "train_number":    row["train_number"],
                "train_name":      row["train_name"] or "",
                "train_type":      row["train_type"] or "",
                "source_code":     row["source_code"] or "",
                "dest_code":       row["dest_code"] or "",
                "run_days":        json.loads(row["run_days"] or "[]"),
                "corridor_stops":  json.loads(row["corridor_stops"] or "[]"),
                "computed_at":     row["computed_at"] or "",
            })
        except Exception:
            pass

    return JSONResponse(
        content={
            "trains":     trains,
            "count":      len(trains),
            "sim_config": _build_sim_config(),
        },
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )


@app.get("/api/simulated-trains-dataset")
async def api_simulated_trains_dataset():
    """Serves the full simulated_trains.json dataset with gap coverage fields."""
    dataset_path = BASE_DIR / "simulated_trains.json"
    if not dataset_path.exists():
        return JSONResponse(status_code=404, content={"error": "simulated_trains.json not found"})
    try:
        with open(dataset_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return JSONResponse(content=data, headers={"Cache-Control": "no-cache"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


def _build_sim_config() -> Dict[str, Any]:
    """Returns current simulation time/day config (real IST + any env overrides)."""
    now_ist = datetime.now(IST)
    return {
        "real_ist_time": now_ist.strftime("%H:%M"),
        "real_ist_day":  ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"][now_ist.weekday()],
        "sim_time":      os.getenv("SIMULATE_TIME", "") or None,
        "sim_day":       os.getenv("SIMULATE_DAY", "") or None,
        "effective_time": get_sim_time(),
        "effective_day":  get_sim_day_correct(),
    }


@app.get("/api/train-position")
async def api_train_position(train: str, corridor_id: Optional[str] = None):
    """
    Returns the estimated real-time position of a specific train on a corridor.
    Uses IST clock (or SIMULATE_TIME/SIMULATE_DAY env overrides).
    """
    if not DB_PATH.exists():
        raise HTTPException(status_code=503, detail="train_cache.db not found")

    try:
        conn = get_db()
        q = "SELECT * FROM corridor_trains WHERE train_number = ?"
        params: List[Any] = [train]
        if corridor_id:
            q += " AND corridor_id = ?"
            params.append(corridor_id)
        row = conn.execute(q, params).fetchone()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not row:
        raise HTTPException(status_code=404, detail=f"Train {train} not found in cache")

    stops = json.loads(row["corridor_stops"] or "[]")
    sim_config = _build_sim_config()
    eff_time = sim_config["effective_time"]
    eff_day  = sim_config["effective_day"]

    run_days = json.loads(row["run_days"] or "[]")
    run_days_norm = [d.lower()[:3] for d in run_days]
    runs_today = (not run_days_norm) or eff_day.lower()[:3] in run_days_norm

    pos = None
    if len(stops) >= 2:
        try:
            from train_position_calculator import calculate_train_position_from_stops
            pos = calculate_train_position_from_stops(
                stops=stops,
                now_time_str=eff_time,
                corridor_id=row["corridor_id"],
                train_number=row["train_number"],
                train_name=row["train_name"],
            )
        except Exception as e:
            print(f"[Position Engine Error] {e}")
            pos = _interpolate_position(stops, _hhmm_to_minutes(eff_time))
    else:
        pos = {"status": "insufficient_stops", "active": False}

    return JSONResponse(
        content={
            "train_number": row["train_number"],
            "train_name":   row["train_name"],
            "corridor_id":  row["corridor_id"],
            "runs_today":   runs_today,
            "position":     pos,
            "sim_config":   sim_config,
        },
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )


def _hhmm_to_minutes(hhmm: str) -> int:
    """Converts HH:MM string to minutes-from-midnight."""
    try:
        h, m = hhmm.strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _interpolate_position(stops: List[dict], now_min: int) -> Optional[Dict[str, Any]]:
    """Backend position interpolator (mirrors train_sim.js logic)."""
    if not stops or len(stops) < 2:
        return None

    for i in range(len(stops) - 1):
        curr = stops[i]
        nxt  = stops[i + 1]
        curr_dep = _hhmm_to_minutes(curr.get("departure") or curr.get("arrival") or "")
        next_arr = _hhmm_to_minutes(nxt.get("arrival") or nxt.get("departure") or "")
        if curr_dep == 0 and next_arr == 0:
            continue
        # Overnight adjustment
        curr_dep_adj = curr_dep + (curr.get("day", 0) * 1440)
        next_arr_adj = next_arr + (nxt.get("day", 0) * 1440)
        now_adj = now_min + (1440 if now_min < curr_dep and curr_dep_adj > 1200 else 0)

        if curr_dep_adj <= now_adj <= next_arr_adj:
            seg = next_arr_adj - curr_dep_adj
            frac = round((now_adj - curr_dep_adj) / seg, 4) if seg > 0 else 0
            return {
                "status":      "between",
                "prev_stn":   curr["station_code"],
                "next_stn":   nxt["station_code"],
                "fraction":   frac,
                "segment":    i,
            }

    return {"status": "not_on_corridor", "now_min": now_min}


@app.post("/api/fetch-trains")
async def api_fetch_trains(background_tasks: BackgroundTasks,
                           corridor_id: Optional[str] = None,
                           force: bool = False):
    """
    Triggers a background re-fetch of corridor train data from RailRadar API.
    This is an admin/manual endpoint — use sparingly (1,000 API call lifetime budget).
    """
    def _run_fetch():
        try:
            import subprocess, sys
            args = [sys.executable, str(BASE_DIR / "fetch_trains.py")]
            if corridor_id:
                args += ["--corridor", corridor_id]
            if force:
                args += ["--force"]
            subprocess.run(args, cwd=str(BASE_DIR), timeout=300)
        except Exception as e:
            print(f"[fetch-trains] Background fetch error: {e}")

    background_tasks.add_task(_run_fetch)
    return {"success": True, "message": "Fetch started in background", "corridor_id": corridor_id or "all"}


@app.get("/api/sim-config")
async def api_sim_config():
    """Returns current simulation time/day settings."""
    return JSONResponse(content=_build_sim_config(),
                        headers={"Cache-Control": "no-cache"})


# ─── Block Scheduler Page ─────────────────────────────────────────────────────
@app.get("/block", response_class=FileResponse)
@app.get("/block.html", response_class=FileResponse)
async def serve_block():
    return FileResponse(str(BASE_DIR / "block.html"), headers={"Cache-Control": "no-cache, no-store"})


# ─── Block Scheduler API Endpoints ────────────────────────────────────────────
import block_scheduler as _bs


@app.get("/api/block-scheduler/faults")
def api_bs_faults(corridor_id: Optional[str] = None):
    """Returns faults filtered by corridor enriched with current time overrides from DB."""
    faults = _bs.load_faults(corridor_id)
    overrides = _bs.load_time_overrides(corridor_id)
    for f in faults:
        f["required_min_override"] = overrides.get(f["id"], f.get("required_block_duration_min", 60))
    return JSONResponse(content={"faults": faults, "count": len(faults)},
                        headers={"Cache-Control": "no-cache"})


class TimeOverrideRequest(BaseModel):
    corridor_id: str
    fault_id: str
    category: str
    override_min: int
    set_by: Optional[str] = "engineer"


@app.post("/api/block-scheduler/set-time-requirements")
async def api_bs_set_time(req: TimeOverrideRequest):
    """Saves engineer-entered time requirement for a specific fault."""
    try:
        _bs.save_time_override(req.corridor_id, req.fault_id, req.category, req.override_min, req.set_by or "engineer")
        return {"success": True, "fault_id": req.fault_id, "override_min": req.override_min}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class MergeRequest(BaseModel):
    selected_fault_ids: List[str]
    merge_km: float = 15.0
    time_overrides: Optional[Dict[str, int]] = None


@app.post("/api/block-scheduler/merge-faults")
async def api_bs_merge_faults(req: MergeRequest):
    """Merges selected faults within distance threshold, returns clusters with block durations."""
    db_overrides = _bs.load_time_overrides()
    combined = {**db_overrides, **(req.time_overrides or {})}
    result = _bs.merge_faults(req.selected_fault_ids, combined, req.merge_km)
    return JSONResponse(content=result, headers={"Cache-Control": "no-cache"})


class WindowRequest(BaseModel):
    corridor_id: str
    selected_fault_ids: List[str]
    block_duration_min: int
    merge_km: float = 15.0
    time_overrides: Optional[Dict[str, int]] = None
    direction: Optional[str] = "UP"


@app.post("/api/block-scheduler/compute-windows")
async def api_bs_compute_windows(req: WindowRequest):
    """
    Core compute endpoint:
    1. Merges faults
    2. Computes generic Mon-Sun free windows for full corridor
    3. Computes station-specific gaps per fault
    Returns both generic and personalized gap views.
    """
    db_overrides = _bs.load_time_overrides(req.corridor_id)
    combined_overrides = {**db_overrides, **(req.time_overrides or {})}

    # Fault merge
    merge_result = _bs.merge_faults(req.selected_fault_ids, combined_overrides, req.merge_km)

    # Generic week view
    generic_week = _bs.find_free_windows_all_days(req.corridor_id, req.block_duration_min, req.direction or "UP")

    # Per-fault station-specific gaps for all 7 days
    per_fault_gaps: Dict[str, Any] = {}
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for fid in req.selected_fault_ids:
        fault_days = {}
        override_min = combined_overrides.get(fid, req.block_duration_min)
        for day in days:
            fault_days[day] = _bs.find_station_specific_gaps(fid, req.corridor_id, override_min, day)
        per_fault_gaps[fid] = fault_days

    # Score top windows from generic view
    top_windows = []
    for day, dv in generic_week["week"].items():
        for gap in dv["feasible_gaps"]:
            impact = _bs.score_window_impact(
                req.corridor_id, day, gap["gap_start_min"], gap["gap_end_min"], req.block_duration_min
            )
            top_windows.append({
                "day": day,
                "gap_start": gap["gap_start"],
                "gap_end": gap["gap_end"],
                "gap_start_min": gap["gap_start_min"],
                "gap_end_min": gap["gap_end_min"],
                "gap_duration_min": gap["gap_duration_min"],
                "headroom_min": gap["headroom_min"],
                "impact": impact,
            })

    top_windows.sort(key=lambda x: (x["impact"]["total_penalty_score"], -x["headroom_min"]))
    top5 = top_windows[:5]

    return JSONResponse(content={
        "merge_result": merge_result,
        "generic_week": generic_week,
        "per_fault_gaps": per_fault_gaps,
        "top_windows": top5,
    }, headers={"Cache-Control": "no-cache"})


class AIAdviseRequest(BaseModel):
    corridor_id: str
    corridor_name: Optional[str] = ""
    cluster_info: Dict[str, Any]
    window_day: str
    window_start: str
    window_end: str
    window_start_min: int
    window_end_min: int
    station_gaps: Optional[List[Dict]] = None


class ImpactRequest(BaseModel):
    corridor_id: str
    window_day: str
    window_start_min: int
    window_end_min: int
    block_duration_min: Optional[int] = None


@app.post("/api/block-scheduler/score-impact")
def api_bs_score_impact(req: ImpactRequest):
    """Fast synchronous impact scoring without calling external LLM."""
    impact = _bs.score_window_impact(
        req.corridor_id, req.window_day, req.window_start_min, req.window_end_min, req.block_duration_min
    )
    return JSONResponse(content={"impact": impact}, headers={"Cache-Control": "no-cache"})


@app.post("/api/block-scheduler/ai-advise")
def api_bs_ai_advise(req: AIAdviseRequest):
    """Calls OpenRouter AI advisor in a worker threadpool so event loop never blocks."""
    req_dur = req.cluster_info.get("block_duration_min", 60)
    impact = _bs.score_window_impact(
        req.corridor_id, req.window_day, req.window_start_min, req.window_end_min, req_dur
    )
    window_info = {
        "day": req.window_day,
        "window_start": req.window_start,
        "window_end": req.window_end,
        "window_duration_min": req.window_end_min - req.window_start_min,
        "headroom_min": max(0, (req.window_end_min - req.window_start_min) - req.cluster_info.get("block_duration_min", 60)),
    }
    corridor_name = req.corridor_name or req.corridor_id
    result = _bs.call_ai_advisor(
        corridor_name=corridor_name,
        cluster_info=req.cluster_info,
        window_info=window_info,
        impact_info=impact,
        station_gaps=req.station_gaps,
    )
    result["impact"] = impact
    result["window"] = window_info
    return JSONResponse(content=result, headers={"Cache-Control": "no-cache"})


class DecisionRequest(BaseModel):
    block_id: str
    corridor_id: str
    fault_ids: List[str]
    category: str
    block_start: str
    block_end: str
    block_dur_min: int
    day_of_week: str
    window_type: str = "scheduled"
    ai_recommendation: Optional[str] = ""
    ai_feasibility: Optional[str] = "UNKNOWN"
    manager_decision: str  # 'approved' | 'rejected'
    manager_notes: Optional[str] = ""


@app.post("/api/block-scheduler/decide")
async def api_bs_decide(req: DecisionRequest):
    """Saves Divisional Manager's approval or rejection of a block."""
    try:
        _bs.save_block_decision(
            block_id=req.block_id,
            corridor_id=req.corridor_id,
            fault_ids=req.fault_ids,
            category=req.category,
            block_start=req.block_start,
            block_end=req.block_end,
            block_dur_min=req.block_dur_min,
            day_of_week=req.day_of_week,
            window_type=req.window_type,
            ai_recommendation=req.ai_recommendation or "",
            ai_feasibility=req.ai_feasibility or "UNKNOWN",
            manager_decision=req.manager_decision,
            manager_notes=req.manager_notes or "",
        )
        return {"success": True, "block_id": req.block_id, "decision": req.manager_decision}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/block-scheduler/schedule")
async def api_bs_schedule(corridor_id: Optional[str] = None):
    """Returns all scheduled/approved/pending maintenance blocks for week view."""
    blocks = _bs.load_scheduled_blocks(corridor_id)
    return JSONResponse(content={"blocks": blocks, "count": len(blocks)},
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/block-scheduler/next-best")
async def api_bs_next_best(
    corridor_id: str,
    block_duration_min: int,
    current_day: Optional[str] = None,
    current_start: Optional[int] = None,
):
    """Returns next-best candidate window after a block is rejected."""
    generic_week = _bs.find_free_windows_all_days(corridor_id, block_duration_min)
    candidates = []
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for day in days:
        dv = generic_week["week"][day]
        for gap in dv["feasible_gaps"]:
            # Skip the rejected slot
            if current_day and current_start:
                if day == current_day and abs(gap["gap_start_min"] - current_start) < 30:
                    continue
            impact = _bs.score_window_impact(
                corridor_id, day, gap["gap_start_min"], gap["gap_end_min"]
            )
            candidates.append({
                "day": day,
                "gap_start": gap["gap_start"],
                "gap_end": gap["gap_end"],
                "gap_start_min": gap["gap_start_min"],
                "gap_end_min": gap["gap_end_min"],
                "gap_duration_min": gap["gap_duration_min"],
                "headroom_min": gap["headroom_min"],
                "total_penalty_score": impact["total_penalty_score"],
                "affected_train_count": impact["affected_train_count"],
            })
    candidates.sort(key=lambda x: (x["total_penalty_score"], -x["headroom_min"]))
    return JSONResponse(content={"next_best": candidates[:3]}, headers={"Cache-Control": "no-cache"})

from dbscan_clustering import run_dbscan

@app.get("/api/cluster-faults")
def api_cluster_faults():
    try:
        clusters = run_dbscan(os.path.join(BASE_DIR, 'faults.json'))
        return JSONResponse({"clusters": clusters})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

from nsga2_optimizer import run_nsga2
from gap_calculator import load_trains_from_db

@app.get("/api/nsga2-optimize")
def api_nsga2_optimize():
    try:
        clusters = run_dbscan(os.path.join(BASE_DIR, 'faults.json'))
        trains = load_trains_from_db()
        day_short = get_sim_day_correct()
        
        # Run NSGA-II to get Pareto-optimal schedules
        results = run_nsga2(clusters, trains, day_short, pop_size=30, max_gen=20)
        
        return JSONResponse({"options": results})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/timetable-gaps")
def get_timetable_gaps():
    """Serves the pre-computed gap schedules for the mega-block dashboard."""
    gap_file = BASE_DIR / "gap_schedules.json"
    if not gap_file.exists():
        try:
            from gap_calculator import calculate_all_gap_schedules
            calculate_all_gap_schedules()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to generate gap schedules: {e}")
    
    try:
        with open(gap_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return JSONResponse(content=data, headers={"Cache-Control": "no-cache"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read gap schedules: {e}")


# ── Active Advisory State Sync (Connects Block AI Scheduler to Main Dashboard) ──
ACTIVE_ADVISORY_STATE: Dict[str, Any] = {}

def get_default_advisory() -> Dict[str, Any]:
    try:
        merge_res = _bs.merge_faults(["TMS-042", "TDMS-11"])
        c0 = (merge_res.get("clusters") or merge_res.get("standalone") or [{}])[0]
        req_dur = c0.get("block_duration_min", 90)
        time_saved = c0.get("time_saved_min", 30)
        ind_durs = c0.get("individual_durations", [60, 60])
        ind_sum = sum(ind_durs) if ind_durs else (req_dur + time_saved)
        eff_pct = round((time_saved / ind_sum) * 100) if ind_sum > 0 else 25

        impact = _bs.score_window_impact("hwh_bwn_main", "tuesday", 200, 255, req_dur)
        aff_trains = impact.get("affected_trains", [])

        return {
            "corridor_id": "hwh_bwn_main",
            "corridor_name": "Howrah - Barddhaman Main Line",
            "cluster_id": c0.get("cluster_id", "CLU-01"),
            "fault_count": c0.get("fault_count", 2),
            "faults": c0.get("faults", [
                {"id": "TMS-042", "category": "Track", "required_min": 60, "location_name": "Between Bandel Jn and Adi Saptagram (KM 108/4)"},
                {"id": "TDMS-11", "category": "Traction", "required_min": 60, "location_name": "Between Memari & Rasulpur"}
            ]),
            "requested_block_min": req_dur,
            "time_saved_min": time_saved,
            "individual_durations": ind_durs,
            "efficiency_pct": eff_pct,
            "window_day": "tuesday",
            "window_start": "03:20",
            "window_end": "04:15",
            "available_gap_min": 55,
            "headroom_min": 0,
            "affected_trains": aff_trains,
            "affected_train_count": len(aff_trains),
            "total_penalty_score": impact.get("total_penalty_score", 365),
            "is_approved": False,
        }
    except Exception as e:
        return {
            "corridor_id": "hwh_bwn_main",
            "corridor_name": "Howrah - Barddhaman Main Line",
            "cluster_id": "CLU-01",
            "fault_count": 2,
            "faults": [
                {"id": "TMS-042", "category": "Track", "required_min": 60, "location_name": "Between Bandel Jn and Adi Saptagram (KM 108/4)"},
                {"id": "TDMS-11", "category": "Traction", "required_min": 60, "location_name": "Between Memari & Rasulpur"}
            ],
            "requested_block_min": 90,
            "time_saved_min": 30,
            "individual_durations": [60, 60],
            "efficiency_pct": 25,
            "window_day": "tuesday",
            "window_start": "03:20",
            "window_end": "04:15",
            "available_gap_min": 55,
            "headroom_min": 0,
            "affected_trains": [
                {"train_number": "13027", "train_name": "Azimganj Kaviguru Express"},
                {"train_number": "13022", "train_name": "Mithila Express"},
                {"train_number": "37812", "train_name": "Barddhaman - Howrah Local"},
                {"train_number": "13030", "train_name": "Mokama - Howrah Express"}
            ],
            "affected_train_count": 4,
            "total_penalty_score": 365,
            "is_approved": False,
        }

@app.get("/api/block-scheduler/active-advisory")
def api_get_active_advisory():
    global ACTIVE_ADVISORY_STATE
    if not ACTIVE_ADVISORY_STATE:
        ACTIVE_ADVISORY_STATE = get_default_advisory()
    return JSONResponse(content=ACTIVE_ADVISORY_STATE, headers={"Cache-Control": "no-cache"})

@app.post("/api/block-scheduler/active-advisory")
def api_post_active_advisory(req_data: Dict[str, Any]):
    global ACTIVE_ADVISORY_STATE
    if not ACTIVE_ADVISORY_STATE:
        ACTIVE_ADVISORY_STATE = get_default_advisory()
    ACTIVE_ADVISORY_STATE.update(req_data)
    return JSONResponse(content={"success": True, "active_advisory": ACTIVE_ADVISORY_STATE})


class DegradationSimRequest(BaseModel):
    fault_id: Optional[str] = "TMS-042"
    fault_type: Optional[str] = "track_gap"
    asset_name: Optional[str] = "KM 41/4 Rail Joint Gap"
    location: Optional[str] = "Bandel Jn - Adi Saptagram"
    corridor_id: Optional[str] = "hwh_bwn_main"
    gap_inches: Optional[float] = 2.0
    sag_mm: Optional[float] = 18.0
    wire_wear_pct: Optional[float] = 42.0
    trains_per_day: Optional[int] = 148
    axle_load_tonnes: Optional[float] = 22.5
    speed_limit_kmph: Optional[int] = 110


@app.post("/api/degradation/simulate")
async def api_degradation_simulate(req: DegradationSimRequest):
    ftype = (req.fault_type or "track_gap").lower()
    trains_day = max(10, req.trains_per_day or 148)
    axle_load = req.axle_load_tonnes or 22.5
    gap_in = req.gap_inches or 2.0
    sag_mm = req.sag_mm or 18.0
    wear_pct = req.wire_wear_pct or 42.0

    timeline = []
    curr_health = 88.0 if ftype == "track_gap" else (85.0 if ftype == "traction_sag" else 90.0)
    warning_day = None
    emergency_day = None

    for d in range(46):
        if ftype == "track_gap":
            impact_factor = 1.0 + 0.35 * (gap_in ** 1.35) * (axle_load / 22.5) * ((req.speed_limit_kmph or 110) / 100.0)
            daily_drop = 0.42 * (impact_factor ** 1.8) * (trains_day / 100.0) * (1.0 + (d / 20.0) ** 1.5)
        elif ftype == "traction_sag":
            panto_force = 70.0 + 2.4 * sag_mm * ((req.speed_limit_kmph or 110) / 100.0)
            daily_drop = (0.25 + 0.15 * ((wear_pct / 40.0) ** 2)) * (panto_force / 70.0) * (trains_day / 100.0) * (1.0 + (d / 22.0) ** 1.4)
        elif ftype == "cms_crossing":
            daily_drop = 0.55 * ((axle_load / 22.5) ** 2.2) * (trains_day / 100.0) * (1.0 + (d / 18.0) ** 1.6)
        else:
            daily_drop = 0.65 * (trains_day / 100.0) * (1.0 + (d / 25.0) ** 1.2)

        health_val = max(3.0, round(curr_health, 1))

        status_label = "SAFE"
        if health_val < 25.0:
            status_label = "PRIORITY EMERGENCY"
            if emergency_day is None:
                emergency_day = d
        elif health_val < 55.0:
            status_label = "WARNING"
            if warning_day is None:
                warning_day = d

        timeline.append({
            "day": d,
            "health": health_val,
            "status": status_label,
            "cumulative_trains": d * trains_day
        })

        curr_health -= daily_drop

    if warning_day is None:
        warning_day = 12
    if emergency_day is None:
        emergency_day = 18

    now_dt = datetime.now(IST)
    warning_date_str = (now_dt + timedelta(days=warning_day)).strftime("%d-%b-%Y")
    emergency_date_str = (now_dt + timedelta(days=emergency_day)).strftime("%d-%b-%Y")

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    model = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-r1")
    site_url = os.getenv("OPENROUTER_SITE_URL", "http://localhost:8000")
    site_name = os.getenv("OPENROUTER_SITE_NAME", "SAMANVAY-DSS")

    ai_response = None

    if ftype == "track_gap":
        mech_text = (
            f"The {gap_in}-inch gap under {trains_day} daily train passes causes high dynamic impact loading (K={round(1.0+0.35*(gap_in**1.35)*(axle_load/22.5),2)}). "
            f"According to Paris' Law fatigue growth, heavy {axle_load}T axle stress concentration accelerates rail micro-fissure propagation into a full transverse crack fracture."
        )
        conseq_text = (
            f"By Day {emergency_day} ({emergency_date_str}), accumulated wheel impacts ({emergency_day * trains_day} trains) will breach critical crack length. "
            f"Unmitigated operation past this point creates catastrophic derailment risk for high-speed passenger expresses."
        )
        recs = [
            f"Impose immediate 20 km/h speed restriction on {req.location} to drop dynamic impact by 45%.",
            f"Schedule a 105-minute emergency track block prior to Day {warning_day} ({warning_date_str}) for thermit weld re-execution.",
            f"Deploy Ultrasonic Flaw Detection (USFD) vehicle to monitor crack depth growth twice weekly."
        ]
    elif ftype == "traction_sag":
        mech_text = (
            f"An OHE wire sag of {sag_mm}mm combined with {wear_pct}% contact wire section loss increases pantograph dynamic strike force to {round(70.0+2.4*sag_mm, 1)} N. "
            f"Each pantograph pass ({trains_day * 2} passes/day) generates localized arcing, thermal degradation, and mechanical notches."
        )
        conseq_text = (
            f"By Day {emergency_day} ({emergency_date_str}), contact wire cross-section will drop below minimum tensile safety threshold, leading to catenary wire snap, pantograph entanglement, and multi-track power trip."
        )
        recs = [
            f"Schedule 90-min OHE power block before Day {warning_day} ({warning_date_str}) for tower wagon re-tensioning.",
            f"Replace worn contact wire splice and recalibrate dropper tension.",
            f"Issue advisory to electric locomotives to lower pantograph speed when passing KM 166/8."
        ]
    else:
        mech_text = f"Asset load of {trains_day} trains/day is causing cyclic stress degradation. Failure projected at Day {emergency_day}."
        conseq_text = f"Asset failure by Day {emergency_day} will cause severe blockages and signal trips."
        recs = ["Schedule maintenance block within 7 days", "Enforce local caution order"]

    if api_key:
        prompt = f"""You are the Lead Railway Infrastructure Safety AI (DeepSeek R1) for Indian Railways SAMANVAY DSS.

ASSET DEGRADATION EVENT DATA:
- Asset Name: {req.asset_name}
- Category/Type: {req.fault_type}
- Location: {req.location} ({req.corridor_id})
- Parameter: Gap={gap_in} in / Sag={sag_mm} mm / Wear={wear_pct}%
- Operating Load: {trains_day} trains/day ({axle_load}T axle load)
- Calculated Emergency Threshold Day: DAY {emergency_day} ({emergency_date_str})
- Total Trains Passed Until Failure: {emergency_day * trains_day} trains

Provide a concise, ultra-authoritative engineering safety diagnosis in valid JSON format:
{{
  "degradation_mechanism": "Explanation of mechanical/electrical fatigue physics under heavy traffic load",
  "failure_consequence": "Explicit failure prediction (e.g. derailment risk, catenary snap) on Day {emergency_day} ({emergency_date_str})",
  "derailment_risk_rating": "CRITICAL (94% Failure Probability on Day {emergency_day})",
  "action_recommendations": [
    "Action 1 (Speed restriction)",
    "Action 2 (Emergency Block Schedule recommendation)",
    "Action 3 (Inspection protocol)"
  ]
}}"""
        try:
            payload = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 1000,
            }).encode("utf-8")

            req_obj = urllib.request.Request(
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
            with urllib.request.urlopen(req_obj, timeout=12) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
                raw_content = resp_json["choices"][0]["message"]["content"]
                if "```json" in raw_content:
                    raw_content = raw_content.split("```json")[1].split("```")[0].strip()
                elif "```" in raw_content:
                    raw_content = raw_content.split("```")[1].split("```")[0].strip()
                ai_response = json.loads(raw_content)
        except Exception as _e:
            print(f"[WARN] OpenRouter DeepSeek R1 call exception: {_e}")

    if not ai_response:
        ai_response = {
            "degradation_mechanism": mech_text,
            "failure_consequence": conseq_text,
            "derailment_risk_rating": f"CRITICAL ({emergency_day} Days to Failure)",
            "action_recommendations": recs
        }

    return JSONResponse(content={
        "success": True,
        "fault_id": req.fault_id,
        "fault_type": ftype,
        "asset_name": req.asset_name,
        "location": req.location,
        "parameters": {
            "gap_inches": gap_in,
            "sag_mm": sag_mm,
            "wire_wear_pct": wear_pct,
            "trains_per_day": trains_day,
            "axle_load_tonnes": axle_load
        },
        "days_to_warning": warning_day,
        "warning_date": warning_date_str,
        "days_to_emergency": emergency_day,
        "emergency_date": emergency_date_str,
        "total_trains_before_failure": emergency_day * trains_day,
        "health_timeline": timeline,
        "ai_analysis": ai_response
    }, headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    import uvicorn
    print("\n=======================================================")
    print("  SAMANVAY - Strategic Maintenance Decision Support System")
    print("  Backend Server running at: http://127.0.0.1:8000")
    print("  Login Page: http://127.0.0.1:8000/login.html")
    print("  Dashboard:  http://127.0.0.1:8000/index.html")
    print("  Block Scheduler: http://127.0.0.1:8000/block.html")
    print("=======================================================\n")
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
