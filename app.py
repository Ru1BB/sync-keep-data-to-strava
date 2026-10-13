from __future__ import annotations

import base64
import calendar
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

import requests
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "sync.sqlite3"
KEEP_LOGIN_URL = "https://api.gotokeep.com/v1.1/users/login"
KEEP_STATS_URL = "https://api.gotokeep.com/pd/v3/stats/detail"
KEEP_DETAIL_URL = "https://api.gotokeep.com/pd/v3/{sport}log/{activity_id}"
STRAVA_API = "https://www.strava.com/api/v3"
STRAVA_AUTHORIZE = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN = "https://www.strava.com/oauth/token"
STRAVA_REVOKE = "https://www.strava.com/oauth/revoke"

KEEP_SPORTS = ("running", "cycling", "hiking")
KEEP_HISTORY_MONTHS = 2
KEEP_TYPE_MAP = {
    "outdoorRunning": ("Run", "跑步"),
    "indoorRunning": ("VirtualRun", "室内跑步"),
    "outdoorCycling": ("Ride", "骑行"),
    "mountaineering": ("Hike", "徒步"),
}
SPORT_LABELS = {"Run": "跑步", "VirtualRun": "室内跑步", "Ride": "骑行", "Hike": "徒步"}
GZIP_FLAG = 16 + zlib.MAX_WBITS

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY", "")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
    MAX_CONTENT_LENGTH=128 * 1024,
)

_worker_lock = threading.Lock()
_init_lock = threading.Lock()
_db_initialized = False


def _required_environment() -> None:
    missing = [name for name in ("APP_PASSWORD", "APP_SECRET_KEY", "TOKEN_ENCRYPTION_KEY") if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Required environment variables are missing: " + ", ".join(missing))


def _fernet() -> Fernet:
    key = os.environ.get("TOKEN_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY must be a Fernet key; generate one with Fernet.generate_key()") from exc


def _db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _init_db() -> None:
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activities (
                source_key TEXT PRIMARY KEY,
                keep_id TEXT NOT NULL,
                keep_sport TEXT NOT NULL,
                sport_type TEXT NOT NULL,
                name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                distance REAL NOT NULL DEFAULT 0,
                duration REAL NOT NULL DEFAULT 0,
                calories INTEGER,
                preview_json TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'new',
                upload_id TEXT,
                strava_activity_id TEXT,
                message TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS activities_started_at_idx ON activities(started_at);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                selected_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                message TEXT
            );
            """
        )
        # A web process may be restarted while a one-off upload worker is
        # running. Preserve uncertain submissions for manual reconciliation.
        conn.execute("UPDATE activities SET state='failed',message='服务重启前任务尚未开始，可重新确认上传。',updated_at=? WHERE state='queued'", (_now_iso(),))
        conn.execute("UPDATE activities SET state='unknown',message='服务重启时上传结果未确认。请先检查 Strava；重试会沿用相同去重 ID。',updated_at=? WHERE state='uploading'", (_now_iso(),))
        conn.execute("UPDATE jobs SET state='interrupted',finished_at=?,message='服务重启时任务中断。' WHERE state IN ('queued','running')", (_now_iso(),))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _history_cutoff(now: datetime | None = None) -> str:
    """Include the entire Shanghai calendar day two calendar months ago."""
    local = (now or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=8)))
    month_index = local.year * 12 + local.month - 1 - KEEP_HISTORY_MONTHS
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(local.day, calendar.monthrange(year, month)[1])
    cutoff = local.replace(year=year, month=month, day=day, hour=0, minute=0, second=0, microsecond=0)
    return cutoff.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc_from_ms(value: Any) -> str:
    return datetime.fromtimestamp(float(value) / 1000, timezone.utc).isoformat(timespec="seconds")


def _get_setting(key: str) -> str | None:
    with _db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def _set_setting(key: str, value: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def _token(key: str) -> str | None:
    encrypted = _get_setting(key)
    if not encrypted:
        return None
    try:
        return _fernet().decrypt(encrypted.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise RuntimeError(f"无法解密 {key}。请检查 TOKEN_ENCRYPTION_KEY 是否与首次部署时一致。") from exc


def _store_token(key: str, value: str) -> None:
    encrypted = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    _set_setting(key, encrypted)


def _require_login(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify(error="请先登录网页。"), 401
            return redirect(url_for("login_page", next=request.path))
        return fn(*args, **kwargs)

    return wrapped


def _json_error(message: str, status: int = 400):
    return jsonify(error=message), status


def _check_keep_response(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Keep 返回了无法识别的响应（HTTP {response.status_code}）。") from exc
    if not response.ok:
        msg = payload.get("message") or payload.get("msg") or f"HTTP {response.status_code}"
        if response.status_code == 401:
            raise PermissionError("Keep 登录已失效，请重新登录 Keep。")
        raise RuntimeError(f"Keep 接口请求失败：{msg}")
    if payload.get("success") is False or payload.get("code") not in (None, 0, "0", "200", 200):
        raise RuntimeError(str(payload.get("message") or payload.get("msg") or "Keep 接口暂不可用。"))
    return payload


def _keep_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Accept": "application/json",
    }


def _keep_records(token: str, sport: str, watermark: str) -> list[dict[str, Any]]:
    if sport not in KEEP_SPORTS:
        raise ValueError("不支持的 Keep 运动类型。")
    result: list[dict[str, Any]] = []
    cursor = 0
    seen_cursors: set[int] = set()
    for _ in range(100):
        response = requests.get(
            KEEP_STATS_URL,
            params={"dateUnit": "all", "type": sport, "lastDate": cursor},
            headers=_keep_headers(token),
            timeout=30,
        )
        payload = _check_keep_response(response)
        data = payload.get("data") or {}
        records = data.get("records") or []
        for record in records:
            for log in record.get("logs") or []:
                stat = log.get("stats") or {}
                if stat.get("id") and not stat.get("isDoubtful"):
                    result.append({"raw": stat, "sport": sport, "record_date": record.get("date")})
        next_cursor = int(data.get("lastTimestamp") or 0)
        # Keep paginates newest-to-oldest. Stop once the next page cursor is
        # older than the requested history range.
        watermark_ms = int(datetime.fromisoformat(watermark).timestamp() * 1000)
        if next_cursor and next_cursor < watermark_ms:
            break
        if not next_cursor or next_cursor in seen_cursors or next_cursor == cursor:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        time.sleep(0.15)
    else:
        raise RuntimeError("Keep 记录分页过多，已停止检查以避免请求失控。")
    return result


def _decode_track(value: str, encrypted: bool = True) -> list[dict[str, Any]]:
    raw = base64.b64decode(value)
    if encrypted:
        # Constants are part of the public Keep-compatible decoder used by the reference project.
        key = base64.b64decode("NTZmZTU5OzgyZzpkODczYw==")
        iv = base64.b64decode("MjM0Njg5MjQzMjkyMDMwMA==")
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        raw = decryptor.update(raw) + decryptor.finalize()
        # The published Keep decoder feeds the decrypted CBC block directly
        # to zlib (which safely ignores trailing cipher padding bytes).
    try:
        decoded = zlib.decompress(raw, GZIP_FLAG)
    except zlib.error:
        decoded = zlib.decompress(raw)
    points = json.loads(decoded)
    if not isinstance(points, list):
        raise ValueError("Keep 轨迹数据格式不正确。")
    return points


def _gcj02_to_wgs84(lat: float, lon: float) -> tuple[float, float]:
    if not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271):
        return lat, lon
    import math

    a = 6378245.0
    ee = 0.00669342162296594323

    def transform_lat(x: float, y: float) -> float:
        value = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        value += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
        value += (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3
        value += (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3
        return value

    def transform_lon(x: float, y: float) -> float:
        value = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
        value += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
        value += (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3
        value += (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3
        return value

    d_lat = transform_lat(lon - 105.0, lat - 35.0)
    d_lon = transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - ee * magic * magic
    sqrt_magic = math.sqrt(magic)
    d_lat = d_lat * 180 / ((a * (1 - ee)) / (magic * sqrt_magic) * math.pi)
    d_lon = d_lon * 180 / (a / sqrt_magic * math.cos(rad_lat) * math.pi)
    return lat * 2 - (lat + d_lat), lon * 2 - (lon + d_lon)


def _record_from_keep(item: dict[str, Any], token: str) -> dict[str, Any] | None:
    raw = item["raw"]
    sport = item["sport"]
    full_id = str(raw["id"])
    response = requests.get(
        KEEP_DETAIL_URL.format(sport=sport, activity_id=full_id),
        headers=_keep_headers(token),
        timeout=30,
    )
    payload = _check_keep_response(response)
    detail = payload.get("data") or {}
    data_type = str(detail.get("dataType") or "")
    mapped = KEEP_TYPE_MAP.get(data_type)
    if not mapped:
        return None
    sport_type, label = mapped
    started_ms = detail.get("startTime") or raw.get("doneDate") or raw.get("statsDate")
    duration = float(detail.get("duration") or raw.get("duration") or 0)
    distance = float(detail.get("distance") or raw.get("distance") or raw.get("kmDistance") or 0)
    # Keep's kmDistance is a display field in kilometers; the API distance field is meters.
    if not detail.get("distance") and not raw.get("distance") and raw.get("kmDistance"):
        distance *= 1000
    if not started_ms or duration <= 0:
        return None
    started_at = _utc_from_ms(started_ms)
    parsed_keep_id = full_id.split("_")[1] if len(full_id.split("_")) > 1 else full_id
    source_key = f"{sport}:{full_id}"
    name = f"{label} from Keep"
    return {
        "source_key": source_key,
        "keep_id": parsed_keep_id,
        "keep_sport": sport,
        "sport_type": sport_type,
        "name": name,
        "started_at": started_at,
        "distance": distance,
        "duration": duration,
        "calories": detail.get("calorie") or raw.get("calorie"),
        "detail": detail,
    }


def _summary_is_after_watermark(item: dict[str, Any], watermark: str) -> bool:
    raw = item["raw"]
    value = raw.get("doneDate") or raw.get("statsDate") or item.get("record_date")
    if value is None:
        return True
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            stamp = float(value)
            # Keep uses milliseconds for epoch values; date-only groups can
            # otherwise arrive as small day counters, which we do not filter.
            if stamp < 100_000_000_000:
                if stamp < 1_000_000_000:
                    return True
                stamp *= 1000
            candidate = datetime.fromtimestamp(stamp / 1000, timezone.utc)
        else:
            candidate = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if candidate.tzinfo is None:
                candidate = candidate.replace(tzinfo=timezone.utc)
        boundary = datetime.fromisoformat(watermark)
        return candidate >= boundary
    except (ValueError, OverflowError, OSError, TypeError):
        return True


def _decode_detail_points(detail: dict[str, Any]) -> list[dict[str, Any]]:
    encoded = detail.get("geoPoints")
    if not encoded:
        raise ValueError("这条 Keep 记录没有 GPS 轨迹点，无法上传 GPX。")
    points = _decode_track(encoded, encrypted=True)
    if not points:
        raise ValueError("Keep 轨迹为空。")
    start_ms = int(detail.get("startTime") or 0)
    first_timestamp = int(points[0].get("timestamp") or points[0].get("unixTimestamp") or 0)
    absolute = first_timestamp > 3_600_000
    result = []
    for point in points:
        lat = point.get("latitude")
        lon = point.get("longitude")
        if lat is None or lon is None:
            continue
        lat, lon = _gcj02_to_wgs84(float(lat), float(lon))
        raw_time = point.get("timestamp", point.get("unixTimestamp", 0))
        # The public Keep format uses deciseconds; older records store an offset from activity start.
        stamp_seconds = float(raw_time) / 10 if absolute else start_ms / 1000 + float(raw_time) / 10
        point_time = datetime.fromtimestamp(stamp_seconds, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        result.append({"lat": lat, "lon": lon, "time": point_time, "ele": point.get("altitude"), "hr": point.get("hr")})
    if len(result) < 2:
        raise ValueError("有效 GPS 轨迹点少于两个，无法上传。")
    return result


def _gpx(record: dict[str, Any], detail: dict[str, Any]) -> bytes:
    points = _decode_detail_points(detail)
    gpx = ET.Element("gpx", {"version": "1.1", "creator": "Keep Strava Sync", "xmlns": "http://www.topografix.com/GPX/1/1", "xmlns:gpxtpx": "http://www.garmin.com/xmlschemas/TrackPointExtension/v1"})
    metadata = ET.SubElement(gpx, "metadata")
    ET.SubElement(metadata, "name").text = record["name"]
    track = ET.SubElement(gpx, "trk")
    ET.SubElement(track, "name").text = record["name"]
    ET.SubElement(track, "type").text = record["sport_type"]
    segment = ET.SubElement(track, "trkseg")
    for point in points:
        node = ET.SubElement(segment, "trkpt", {"lat": f"{point['lat']:.7f}", "lon": f"{point['lon']:.7f}"})
        if point["ele"] is not None:
            ET.SubElement(node, "ele").text = str(point["ele"])
        ET.SubElement(node, "time").text = point["time"]
        if point["hr"] is not None:
            ext = ET.SubElement(node, "extensions")
            tpx = ET.SubElement(ext, "gpxtpx:TrackPointExtension")
            ET.SubElement(tpx, "gpxtpx:hr").text = str(point["hr"])
    return ET.tostring(gpx, encoding="utf-8", xml_declaration=True)


def _strava_access_token() -> str:
    access = _token("strava_access")
    refresh = _token("strava_refresh")
    expires_at = int(_get_setting("strava_expires_at") or "0")
    if not refresh:
        raise PermissionError("请先连接 Strava。")
    if access and expires_at > int(time.time()) + 300:
        return access
    response = requests.post(
        STRAVA_TOKEN,
        data={
            "client_id": os.environ.get("STRAVA_CLIENT_ID", ""),
            "client_secret": os.environ.get("STRAVA_CLIENT_SECRET", ""),
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        },
        timeout=30,
    )
    if not response.ok:
        raise PermissionError("Strava 授权已失效，请重新连接 Strava。")
    data = response.json()
    _store_token("strava_access", data["access_token"])
    _store_token("strava_refresh", data["refresh_token"])
    _set_setting("strava_expires_at", str(data["expires_at"]))
    _set_setting("strava_athlete", str((data.get("athlete") or {}).get("id") or ""))
    return data["access_token"]


def _strava_request(method: str, path: str, token: str, **kwargs) -> requests.Response:
    response = requests.request(
        method,
        STRAVA_API + path,
        headers={"Authorization": f"Bearer {token}"},
        timeout=45,
        **kwargs,
    )
    if response.status_code == 401:
        raise PermissionError("Strava 授权已失效，请重新连接 Strava。")
    return response


def _external_id(source_key: str) -> str:
    return "keep-" + hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:40]


def _set_activity_state(source_key: str, state: str, *, message: str | None = None, upload_id: str | None = None, activity_id: str | None = None) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE activities SET state=?,message=?,upload_id=COALESCE(?,upload_id),strava_activity_id=COALESCE(?,strava_activity_id),updated_at=? WHERE source_key=?",
            (state, message, upload_id, activity_id, _now_iso(), source_key),
        )


def _upload_one(source_key: str) -> None:
    token = _token("keep_token")
    if not token:
        raise PermissionError("请先连接 Keep。")
    with _db() as conn:
        row = conn.execute("SELECT * FROM activities WHERE source_key=?", (source_key,)).fetchone()
    if row is None:
        raise RuntimeError("待上传记录已不存在，请重新检查 Keep。")
    record = dict(row)
    if record["state"] == "success":
        return
    if record["state"] == "processing" and record["upload_id"]:
        _poll_upload(record)
        return
    detail = json.loads(record["preview_json"]).get("detail")
    if not detail:
        response = requests.get(
            KEEP_DETAIL_URL.format(sport=record["keep_sport"], activity_id=json.loads(record["preview_json"])["full_id"]),
            headers=_keep_headers(token),
            timeout=30,
        )
        detail = _check_keep_response(response).get("data") or {}
    payload_record = {**record, "sport_type": record["sport_type"]}
    gpx_data = _gpx(payload_record, detail)
    _set_activity_state(source_key, "uploading", message=None)
    token = _strava_access_token()
    response = _strava_request(
        "POST",
        "/uploads",
        token,
        data={"data_type": "gpx", "sport_type": record["sport_type"], "name": record["name"], "external_id": _external_id(source_key)},
        files={"file": (f"{_external_id(source_key)}.gpx", gpx_data, "application/gpx+xml")},
    )
    if response.status_code not in (200, 201):
        message = _strava_message(response)
        # Preserve the same external_id on retry; Strava also validates duplicate uploads.
        _set_activity_state(source_key, "failed", message=message)
        raise RuntimeError(message)
    upload = response.json()
    upload_id = str(upload.get("id") or "")
    if not upload_id:
        _set_activity_state(source_key, "unknown", message="Strava 已接收请求，但没有返回上传任务编号。请检查 Strava 后再决定是否重试。")
        return
    _set_activity_state(source_key, "processing", upload_id=upload_id, message="Strava 正在处理轨迹。")
    record["upload_id"] = upload_id
    _poll_upload(record)


def _strava_message(response: requests.Response) -> str:
    try:
        payload = response.json()
        errors = payload.get("errors") or []
        if errors:
            return "; ".join(str(item.get("field") or item.get("resource") or "") + ": " + str(item.get("code") or "上传失败") for item in errors)
        return str(payload.get("message") or payload.get("error") or f"Strava 返回 HTTP {response.status_code}")
    except ValueError:
        return f"Strava 返回 HTTP {response.status_code}。"


def _poll_upload(record: dict[str, Any]) -> None:
    token = _strava_access_token()
    upload_id = str(record["upload_id"])
    for _ in range(30):
        response = _strava_request("GET", f"/uploads/{upload_id}", token)
        if not response.ok:
            message = _strava_message(response)
            _set_activity_state(record["source_key"], "failed", message=message)
            raise RuntimeError(message)
        data = response.json()
        if data.get("activity_id"):
            _set_activity_state(record["source_key"], "success", activity_id=str(data["activity_id"]), message="已上传到 Strava。")
            return
        error = data.get("error")
        if error:
            _set_activity_state(record["source_key"], "failed", message=str(error))
            raise RuntimeError(str(error))
        time.sleep(1)
    _set_activity_state(record["source_key"], "processing", message="Strava 仍在处理。可以稍后点“刷新状态”。")


def _run_job(job_id: str, selected: list[str]) -> None:
    with _worker_lock:
        with _db() as conn:
            conn.execute("UPDATE jobs SET state='running',message=NULL WHERE id=?", (job_id,))
        failures = []
        for source_key in selected:
            try:
                _upload_one(source_key)
            except Exception as exc:  # Keep the job moving; per-record outcomes are persisted.
                message = str(exc) or "同步失败。"
                with _db() as conn:
                    row = conn.execute("SELECT state FROM activities WHERE source_key=?", (source_key,)).fetchone()
                if row and row["state"] in ("uploading", "new", "queued"):
                    _set_activity_state(source_key, "failed", message=message)
                failures.append(message)
        with _db() as conn:
            conn.execute(
                "UPDATE jobs SET state=?,finished_at=?,message=? WHERE id=?",
                ("finished_with_errors" if failures else "finished", _now_iso(), "；".join(failures[:3]) if failures else None, job_id),
            )


@app.before_request
def _ensure_configuration() -> None:
    global _db_initialized
    if not app.secret_key:
        app.secret_key = os.environ.get("APP_SECRET_KEY", "")
    if not _db_initialized:
        with _init_lock:
            if not _db_initialized:
                if not DB_PATH.parent.exists():
                    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                _init_db()
                _db_initialized = True


@app.get("/login")
def login_page():
    return render_template("login.html", error=None)


@app.post("/login")
def login():
    if not os.environ.get("APP_PASSWORD") or not app.secret_key or not os.environ.get("TOKEN_ENCRYPTION_KEY"):
        return render_template("login.html", error="服务端尚未设置 APP_PASSWORD、APP_SECRET_KEY 或 TOKEN_ENCRYPTION_KEY。"), 503
    try:
        _fernet()
    except RuntimeError as exc:
        return render_template("login.html", error=str(exc)), 503
    password = request.form.get("password", "")
    expected = os.environ["APP_PASSWORD"]
    if not secrets.compare_digest(password, expected):
        return render_template("login.html", error="密码不正确，请重试。"), 401
    session.clear()
    session["logged_in"] = True
    return redirect(url_for("index"))


@app.post("/logout")
@_require_login
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.get("/")
@_require_login
def index():
    # Allow the public root URL to be the OAuth callback while retaining the
    # dedicated callback route for existing deployments.
    if "code" in request.args or "state" in request.args:
        return strava_callback()
    return render_template("index.html")


@app.get("/auth/strava")
@_require_login
def strava_authorize():
    client_id = os.environ.get("STRAVA_CLIENT_ID")
    if not client_id or not os.environ.get("STRAVA_CLIENT_SECRET"):
        return redirect(url_for("index", error="请先在服务器配置 Strava Client ID 和 Client Secret。"))
    state = secrets.token_urlsafe(24)
    session["strava_oauth_state"] = state
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": os.environ.get("STRAVA_REDIRECT_URI", request.url_root.rstrip("/") + "/auth/strava/callback"),
            # Force consent so a previously read-only grant cannot silently
            # carry forward without the upload scope this app requires.
            "approval_prompt": "force",
            "scope": "activity:write",
            "state": state,
        }
    )
    return redirect(f"{STRAVA_AUTHORIZE}?{query}")


@app.get("/auth/strava/callback")
@_require_login
def strava_callback():
    returned_state = request.args.get("state", "")
    expected_state = session.pop("strava_oauth_state", "")
    if not expected_state or not secrets.compare_digest(returned_state, expected_state):
        return redirect(url_for("index", error="Strava 授权校验失败，请重新连接。"))
    if request.args.get("error"):
        return redirect(url_for("index", error="你取消了 Strava 授权。"))
    code = request.args.get("code")
    if not code:
        return redirect(url_for("index", error="没有收到 Strava 授权码。"))
    response = requests.post(
        STRAVA_TOKEN,
        data={
            "client_id": os.environ.get("STRAVA_CLIENT_ID", ""),
            "client_secret": os.environ.get("STRAVA_CLIENT_SECRET", ""),
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if not response.ok:
        return redirect(url_for("index", error="Strava 授权交换失败，请重试。"))
    data = response.json()
    granted = set(str(data.get("scope", "")).replace(",", " ").split())
    if "activity:write" not in granted:
        return redirect(url_for("index", error="Strava 未授予活动上传权限，请重新授权 activity:write。"))
    _store_token("strava_access", data["access_token"])
    _store_token("strava_refresh", data["refresh_token"])
    _set_setting("strava_expires_at", str(data["expires_at"]))
    _set_setting("strava_athlete", str((data.get("athlete") or {}).get("id") or ""))
    return redirect(url_for("index", connected="strava"))


@app.post("/api/keep/login")
@_require_login
def keep_login():
    body = request.get_json(silent=True) or {}
    mobile = str(body.get("mobile", "")).strip()
    password = str(body.get("password", ""))
    if not mobile or not password:
        return _json_error("请输入 Keep 手机号和密码。")
    try:
        response = requests.post(
            KEEP_LOGIN_URL,
            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
            data={"mobile": mobile, "password": password},
            timeout=30,
        )
        payload = _check_keep_response(response)
        token = (payload.get("data") or {}).get("token")
        if not token:
            return _json_error("Keep 登录响应中没有会话令牌；可能需要更新 Keep 适配。", 502)
        _store_token("keep_token", str(token))
        return jsonify(ok=True, connected=True)
    except PermissionError as exc:
        return _json_error(str(exc), 401)
    except Exception as exc:
        return _json_error(str(exc), 502)


@app.delete("/api/keep")
@_require_login
def keep_disconnect():
    _set_setting("keep_token", "")
    return jsonify(ok=True)


@app.delete("/api/strava")
@_require_login
def strava_disconnect():
    refresh = _token("strava_refresh")
    if refresh and os.environ.get("STRAVA_CLIENT_ID") and os.environ.get("STRAVA_CLIENT_SECRET"):
        try:
            requests.post(
                STRAVA_REVOKE,
                auth=(os.environ["STRAVA_CLIENT_ID"], os.environ["STRAVA_CLIENT_SECRET"]),
                data={"token": refresh, "token_type_hint": "refresh_token"},
                timeout=20,
            )
        except requests.RequestException:
            pass
    for key in ("strava_access", "strava_refresh", "strava_expires_at", "strava_athlete"):
        _set_setting(key, "")
    return jsonify(ok=True)


@app.get("/api/state")
@_require_login
def state():
    try:
        with _db() as conn:
            activities = conn.execute(
                "SELECT source_key,name,sport_type,started_at,distance,duration,state,upload_id,strava_activity_id,message FROM activities ORDER BY started_at DESC LIMIT 100"
            ).fetchall()
            jobs = conn.execute("SELECT id,state,created_at,finished_at,message FROM jobs ORDER BY created_at DESC LIMIT 10").fetchall()
        return jsonify(
            keep_connected=bool(_token("keep_token")),
            strava_connected=bool(_token("strava_refresh")),
            strava_athlete=_get_setting("strava_athlete") or None,
            watermark=_history_cutoff(),
            history_since=_history_cutoff(),
            history_months=KEEP_HISTORY_MONTHS,
            activities=[dict(row) for row in activities],
            jobs=[dict(row) for row in jobs],
        )
    except RuntimeError as exc:
        return _json_error(str(exc), 500)


@app.post("/api/check")
@_require_login
def check_keep():
    token = _token("keep_token")
    if not token:
        return _json_error("请先登录 Keep。", 401)
    watermark = _history_cutoff()
    warnings = []
    with _db() as conn:
        synced = {row["source_key"] for row in conn.execute("SELECT source_key FROM activities WHERE state='success'")}
    try:
        for sport in KEEP_SPORTS:
            for item in _keep_records(token, sport, watermark):
                if f"{sport}:{item['raw']['id']}" in synced:
                    continue
                if not _summary_is_after_watermark(item, watermark):
                    continue
                try:
                    record = _record_from_keep(item, token)
                except PermissionError:
                    raise
                except Exception as exc:
                    warnings.append(f"部分{sport}记录详情读取失败：{exc}")
                    continue
                if not record or record["started_at"] < watermark:
                    continue
                record.pop("detail")
                raw = item["raw"]
                full_id = str(raw["id"])
                # Do not persist GPS/heart-rate payloads; fetch the detail again
                # only after the user confirms an upload.
                preview = {"full_id": full_id}
                with _db() as conn:
                    existing = conn.execute("SELECT state FROM activities WHERE source_key=?", (record["source_key"],)).fetchone()
                    conn.execute(
                        """INSERT INTO activities(source_key,keep_id,keep_sport,sport_type,name,started_at,distance,duration,calories,preview_json,state,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,'new',?) ON CONFLICT(source_key) DO UPDATE SET
                        name=excluded.name,started_at=excluded.started_at,distance=excluded.distance,duration=excluded.duration,
                        calories=excluded.calories,preview_json=excluded.preview_json,updated_at=excluded.updated_at
                        WHERE activities.state IN ('new','failed')""",
                        (record["source_key"], record["keep_id"], record["keep_sport"], record["sport_type"], record["name"], record["started_at"], record["distance"], record["duration"], record["calories"], json.dumps(preview, ensure_ascii=False), _now_iso()),
                    )
                    if existing and existing["state"] == "success":
                        continue
        with _db() as conn:
            rows = conn.execute(
                "SELECT source_key,name,sport_type,started_at,distance,duration,state,message FROM activities WHERE started_at>=? AND state IN ('new','failed','processing','unknown') ORDER BY started_at DESC",
                (watermark,),
            ).fetchall()
        return jsonify(watermark=watermark, history_since=watermark, history_months=KEEP_HISTORY_MONTHS, activities=[dict(row) for row in rows], warnings=warnings[:5], checked_at=_now_iso())
    except PermissionError as exc:
        return _json_error(str(exc), 401)
    except requests.RequestException as exc:
        return _json_error(f"连接 Keep 失败：{exc}", 502)
    except Exception as exc:
        return _json_error(str(exc), 502)


@app.post("/api/sync")
@_require_login
def sync_selected():
    if not _token("keep_token"):
        return _json_error("请先连接 Keep。", 401)
    if not _token("strava_refresh"):
        return _json_error("请先连接 Strava。", 401)
    body = request.get_json(silent=True) or {}
    selected = body.get("activity_ids")
    if not isinstance(selected, list) or not selected:
        return _json_error("请选择至少一条运动记录。")
    selected = list(dict.fromkeys(str(value) for value in selected))
    if len(selected) > 50:
        return _json_error("一次最多确认上传 50 条记录。")
    watermark = _history_cutoff()
    with _db() as conn:
        rows = conn.execute(
            f"SELECT source_key,state,started_at FROM activities WHERE source_key IN ({','.join('?' for _ in selected)})",
            selected,
        ).fetchall()
        allowed = {row["source_key"] for row in rows if row["started_at"] >= watermark and row["state"] in ("new", "failed", "processing", "unknown")}
        if len(allowed) != len(selected):
            return _json_error("有记录未在当前待同步清单中或已经成功同步，请重新检查。", 409)
        job_id = secrets.token_urlsafe(12)
        conn.execute("INSERT INTO jobs(id,state,selected_json,created_at) VALUES(?,'queued',?,?)", (job_id, json.dumps(selected), _now_iso()))
        conn.executemany("UPDATE activities SET state='queued',message='等待上传',updated_at=? WHERE source_key=? AND state IN ('new','failed')", [(_now_iso(), key) for key in selected])
    thread = threading.Thread(target=_run_job, args=(job_id, selected), daemon=True)
    thread.start()
    return jsonify(job_id=job_id, state="queued"), 202


@app.post("/api/refresh-status")
@_require_login
def refresh_status():
    body = request.get_json(silent=True) or {}
    source_key = str(body.get("activity_id", ""))
    with _db() as conn:
        row = conn.execute("SELECT * FROM activities WHERE source_key=?", (source_key,)).fetchone()
    if not row:
        return _json_error("找不到这条记录。", 404)
    record = dict(row)
    if record["state"] != "processing" or not record["upload_id"]:
        return jsonify(state=record["state"], message=record["message"])
    try:
        _poll_upload(record)
    except Exception as exc:
        return _json_error(str(exc), 502)
    with _db() as conn:
        latest = conn.execute("SELECT state,message,strava_activity_id FROM activities WHERE source_key=?", (source_key,)).fetchone()
    return jsonify(dict(latest))


@app.get("/api/jobs/<job_id>")
@_require_login
def job_status(job_id: str):
    with _db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return _json_error("找不到同步任务。", 404)
        keys = json.loads(row["selected_json"])
        activities = conn.execute(
            f"SELECT source_key,name,state,strava_activity_id,message FROM activities WHERE source_key IN ({','.join('?' for _ in keys)})",
            keys,
        ).fetchall()
    return jsonify(job=dict(row), activities=[dict(item) for item in activities])


@app.get("/health")
def health():
    return jsonify(ok=True)


if __name__ == "__main__":
    _required_environment()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)
