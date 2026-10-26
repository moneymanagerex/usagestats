import hashlib
import logging
import os
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

# ---------------- 配置（全部走环境变量） ----------------
API_KEY      = os.environ["AMPLITUDE_API_KEY"]                    # 必须注入，缺失则启动失败
AMP_URL      = os.environ.get("AMPLITUDE_URL", "https://api2.amplitude.com/2/httpapi")
SAMPLE_RATE  = int(os.environ.get("SAMPLE_RATE", "20"))
UPSTREAM_TMO = float(os.environ.get("UPSTREAM_TIMEOUT", "5"))
V1_FORWARD   = os.environ.get("V1_FORWARD", "true").lower() in ("1", "true", "yes")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("usagestats")

# ---------------- 全局 AsyncClient，lifespan 中创建/关闭 ----------------
http: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http
    http = httpx.AsyncClient(
        timeout=UPSTREAM_TMO,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    log.info("started url=%s sample_rate=%d v1_forward=%s",
             AMP_URL, SAMPLE_RATE, V1_FORWARD)
    yield
    await http.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ---------------- 工具函数 ----------------
def get_ip(req: Request) -> str:
    # 与 PHP 原逻辑一致：XFF 取最后一项，其次 X-Real-IP，最后 REMOTE_ADDR
    xff = req.headers.get("x-forwarded-for", "")
    last = xff.split(",")[-1].strip() if xff else ""
    if last:
        return last
    xri = req.headers.get("x-real-ip", "").strip()
    if xri:
        return xri
    return req.client.host if req.client else ""


def _int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _dt(s: str | None) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return datetime.fromtimestamp(int(s), tz=timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


async def send_amp(data: dict) -> bool:
    try:
        r = await http.post(AMP_URL, json=data,
                            headers={"Content-Type": "application/json"})
        if r.status_code >= 400:
            log.warning("amp upstream %s body=%s", r.status_code, r.text[:300])
            return False
        return True
    except httpx.HTTPError as e:
        log.error("amp http error: %r", e)
        return False


# ---------------- 路由 ----------------
@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/API/main_stats_v2.php")
async def stats_v2(req: Request):
    if random.randint(1, 100) > SAMPLE_RATE:
        return PlainTextResponse("Sampled out, event not sent.")

    try:
        body = await req.json()
    except Exception:
        return PlainTextResponse("Invalid JSON", status_code=400)

    if not isinstance(body, dict) or not isinstance(body.get("events"), list):
        return PlainTextResponse("Invalid payload: events required", status_code=400)

    ip = get_ip(req)
    body["api_key"] = API_KEY

    for ev in body["events"]:
        if not isinstance(ev, dict):
            continue
        ev["ip_address"] = ip
        ev["event_type"] = "page_view"
        ev["device_id"]  = hashlib.md5(str(ev.get("device_id", "")).encode("utf-8")).hexdigest()
        props = ev.get("event_properties")
        if not isinstance(props, dict):
            props = {}
            ev["event_properties"] = props
        props["engagement_time_msec"] = _int(props.get("plt")) + 1

    if not await send_amp(body):
        return PlainTextResponse("Upstream error", status_code=502)

    log.info("v2 forwarded events=%d ip=%s", len(body["events"]), ip)
    return PlainTextResponse("Event sent successfully." + AMP_URL)


@app.get("/API/main_stats_v1.php")
async def stats_v1(req: Request):
    q = req.query_params
    uid = q.get("User_ID")
    if not uid:
        return PlainTextResponse("Required parameter missing")

    now   = datetime.now(timezone.utc)
    start = _dt(q.get("Start_Time")) or now
    end   = _dt(q.get("End_Time"))   or now
    # 修复原 PHP 的 bug：原算法 ($interval->s*1000 + $interval->f/1000)
    # 会把 17.5 小时算成 10 秒；这里用真实总秒数
    engagement_ms = max(0, int((end - start).total_seconds() * 1000))

    page_path  = q.get("Page_Path")  or "/pathplaceholder"
    page_title = q.get("Page_Title") or "titleplaceholder"

    event = {
        "device_id": hashlib.md5(uid.encode("utf-8")).hexdigest(),
        "user_id": uid,
        "event_type": "page_view",
        "ip_address": get_ip(req),
        "platform":     q.get("Platform", ""),
        "language":     q.get("Language", ""),
        "os_name":      q.get("OperatingSystem", ""),
        "version_name": q.get("Version", ""),
        "session_id": round(start.timestamp() / 3600) * 3600,
        "time": int(start.timestamp()),
        "event_properties": {
            "page_title": page_title,
            "page_path": page_path,
            "page_location": page_path,
            "plt": engagement_ms,
            "engagement_time_msec": engagement_ms,
        },
    }

    if not V1_FORWARD:
        log.info("v1 dry-run user=%s eng_ms=%s ip=%s",
                 uid, engagement_ms, event["ip_address"])
        return PlainTextResponse("Event accepted (dry-run).")

    if not await send_amp({"api_key": API_KEY, "events": [event]}):
        return PlainTextResponse("Upstream error", status_code=502)

    log.info("v1 forwarded user=%s eng_ms=%s ip=%s",
             uid, engagement_ms, event["ip_address"])
    return PlainTextResponse("Event sent successfully." + AMP_URL)
