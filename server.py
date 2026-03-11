"""Mazda Relay — FastAPI service wrapping fano0001's pymazda v2 API."""

import json
import logging
import os
import sys
from contextlib import asynccontextmanager

import aiohttp
from enum import Enum

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel


class Units(str, Enum):
    metric = "metric"
    imperial = "imperial"

# Add the submodule's pymazda to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "vendor/ha-mazda/custom_components/mazda_cs"))

from pymazda.controller import Controller  # noqa: E402
from pymazda.exceptions import MazdaException  # noqa: E402

log = logging.getLogger("mazda-relay")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")

# ---------------------------------------------------------------------------
# OAuth config
# ---------------------------------------------------------------------------
OAUTH_CONFIG = {
    "host": "https://na.id.mazda.com",
    "tenant_id": "47801034-62d1-49f6-831b-ffdcf04f13fc",
    "client_id": "2daf581c-65c1-4fdb-b46a-efa98c6ba5b7",
    "scope": "https://pduspb2c01.onmicrosoft.com/0728deea-be48-4382-9ef1-d4ff6d679ffa/cv openid profile offline_access",
    "redirect_uri": "msauth.com.mazdausa.mazdaiphone://auth",
}

# ---------------------------------------------------------------------------
# Token manager
# ---------------------------------------------------------------------------
class TokenManager:
    def __init__(self, refresh_token: str, env_path: str | None = None):
        self.refresh_token = refresh_token
        self.access_token: str | None = None
        self.env_path = env_path

    async def get_access_token(self) -> str:
        """Always fetch a fresh token — this is only called when Connection has
        cleared its cached token (startup or after 600002), so a real OAuth
        refresh is the correct thing to do."""
        await self.refresh()
        return self.access_token

    async def refresh(self):
        url = f"{OAUTH_CONFIG['host']}/{OAUTH_CONFIG['tenant_id']}/b2c_1a_signin/oauth2/v2.0/token"
        data = {
            "client_id": OAUTH_CONFIG["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "scope": OAUTH_CONFIG["scope"],
            "redirect_uri": OAUTH_CONFIG["redirect_uri"],
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"OAuth refresh failed ({resp.status}): {text}")
                token_data = await resp.json()

        self.access_token = token_data["access_token"]
        old_refresh = self.refresh_token
        self.refresh_token = token_data["refresh_token"]

        expires_in = token_data.get("expires_in", "?")
        log.info("Access token refreshed (expires in %ss)", expires_in)

        if self.env_path and os.path.exists(self.env_path):
            with open(self.env_path) as f:
                content = f.read()
            if old_refresh in content:
                content = content.replace(old_refresh, self.refresh_token)
                with open(self.env_path, "w") as f:
                    f.write(content)
                log.info("Persisted new refresh token to .env")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
class MazdaState:
    def __init__(self):
        self.controller: Controller | None = None
        self.token_mgr: TokenManager | None = None
        self.internal_vin: int | None = None
        self.vin: str | None = None
        self.vehicle_info: dict | None = None

    async def connect(self):
        email = os.environ.get("MAZDA_EMAIL")
        refresh_token = os.environ.get("MAZDA_REFRESH_TOKEN")
        if not email or not refresh_token:
            raise RuntimeError("Set MAZDA_EMAIL and MAZDA_REFRESH_TOKEN env vars")

        env_path = os.path.join(os.path.dirname(__file__), ".env")
        self.token_mgr = TokenManager(refresh_token, env_path)

        log.info("Refreshing OAuth token...")
        await self.token_mgr.refresh()

        self.controller = Controller(
            email=email,
            region="MNAO",
            access_token_provider=self.token_mgr.get_access_token,
        )

        await self._attach()
        await self._discover_vehicle()

    async def _attach(self):
        log.info("Attaching device session...")
        resp = await self.controller.attach()
        session_id = resp.get("data", {}).get("userinfo", {}).get("sessionId")
        if session_id:
            self.controller.connection.device_session_id = session_id
            log.info("Session ID: %s", session_id)
        else:
            log.warning("No sessionId in attach response")

    async def _discover_vehicle(self):
        vehicles = await self.controller.get_vec_base_infos()
        vec_infos = vehicles.get("vecBaseInfos", [])
        if not vec_infos:
            raise RuntimeError("No vehicles found on account")

        v = vec_infos[0]
        self.vin = v.get("vin")
        cv_info = v.get("Vehicle", {}).get("CvInformation", {})
        self.internal_vin = cv_info.get("internalVin")

        veh_info_raw = v.get("Vehicle", {}).get("vehicleInformation", "{}")
        veh_info = json.loads(veh_info_raw) if isinstance(veh_info_raw, str) else veh_info_raw
        other = veh_info.get("OtherInformation", {})

        self.vehicle_info = {
            "vin": self.vin,
            "internalVin": self.internal_vin,
            "modelYear": other.get("modelYear"),
            "modelName": other.get("modelName"),
            "carlineName": other.get("carlineName"),
            "exteriorColor": other.get("exteriorColorName"),
            "interiorColor": other.get("interiorColorName"),
            "engine": other.get("engineInformation"),
            "transmission": other.get("transmissionName"),
            "hasRemoteStart": v.get("remoteEngineStartFlg") == 1,
            "hasFlashLights": v.get("flashLightFlg") == 1,
        }
        log.info("Vehicle: %s %s (%s)", other.get("modelYear"), other.get("modelName"), self.vin)

    async def reconnect(self):
        """Re-attach after session conflict or token expiry."""
        log.info("Reconnecting...")
        self.token_mgr.access_token = None
        await self.token_mgr.refresh()
        await self._attach()

    async def close(self):
        if self.controller:
            await self.controller.close()


state = MazdaState()

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await state.connect()
    yield
    await state.close()

app = FastAPI(title="Mazda Relay", version="0.1.0", lifespan=lifespan)


def _km_to_miles(km):
    return round(km * 0.621371, 1) if km is not None else None


async def _api_call(coro_fn, *args):
    """Wrap a controller call with reconnect-on-failure."""
    try:
        return await coro_fn(*args)
    except MazdaException as e:
        err = str(e)
        # Session conflict, token issues, or exhausted retries — reconnect and retry once
        if any(code in err for code in ["600100", "600002", "CST400000", "max number of retries"]):
            await state.reconnect()
            return await coro_fn(*args)
        raise HTTPException(status_code=502, detail=err)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------
@app.get("/vehicle")
async def get_vehicle():
    """Vehicle info (static — model, VIN, color, features)."""
    return state.vehicle_info


@app.get("/status")
async def get_status(units: Units = Query(Units.imperial)):
    """Current vehicle status (fuel, odometer, location, doors, tires, oil)."""
    raw = await _api_call(state.controller.get_vehicle_status, state.internal_vin)
    imperial = units == Units.imperial

    remote = raw.get("remoteInfos", [{}])[0]
    alert = raw.get("alertInfos", [{}])[0]
    pos = remote.get("PositionInfo", {})
    door = alert.get("Door", {})
    pw = alert.get("Pw", {})
    tpms = remote.get("TPMSInformation", {})
    fuel = remote.get("ResidualFuel", {})
    oil = remote.get("OilMntInformation", {})
    drive = remote.get("DriveInformation", {})
    elec = remote.get("ElectricalInformation", {})

    # Longitude sign: LongitudeFlag 0 = west (negative), 1 = east (positive)
    lon = pos.get("Longitude")
    if lon is not None and pos.get("LongitudeFlag") == 0:
        lon = -lon

    # Latitude sign: LatitudeFlag 0 = north (positive), 1 = south (negative)
    lat = pos.get("Latitude")
    if lat is not None and pos.get("LatitudeFlag") == 1:
        lat = -lat

    odo = drive.get("OdoDispValueMile") if imperial else drive.get("OdoDispValue")
    fuel_range = fuel.get("RemDrvDistDActlMile") if imperial else fuel.get("RemDrvDistDActlKm")
    oil_next = _km_to_miles(oil.get("RemOilDistK")) if imperial else oil.get("RemOilDistK")

    return {
        "lastUpdated": alert.get("OccurrenceDate"),
        "units": units.value,
        "location": {
            "latitude": lat,
            "longitude": lon,
            "timestamp": pos.get("AcquisitionDatetime"),
        },
        "fuel": {
            "remainingPercent": fuel.get("FuelSegementDActl"),
            "remainingRange": fuel_range,
        },
        "odometer": odo,
        "engine": {
            "state": elec.get("EngineState"),  # 3 = off
            "powerControlStatus": elec.get("PowerControlStatus"),
        },
        "doors": {
            "driverOpen": door.get("DrStatDrv") == 1,
            "passengerOpen": door.get("DrStatPsngr") == 1,
            "rearLeftOpen": door.get("DrStatRl") == 1,
            "rearRightOpen": door.get("DrStatRr") == 1,
            "trunkOpen": door.get("DrStatTrnkLg") == 1,
            "hoodOpen": door.get("DrStatHood") == 1,
            "fuelLidOpen": door.get("FuelLidOpenStatus") == 1,
        },
        "locks": {
            "driverUnlocked": door.get("LockLinkSwDrv") == 1,
            "passengerUnlocked": door.get("LockLinkSwPsngr") == 1,
            "rearLeftUnlocked": door.get("LockLinkSwRl") == 1,
            "rearRightUnlocked": door.get("LockLinkSwRr") == 1,
        },
        "windows": {
            "driverOpen": pw.get("PwPosDrv") == 1,
            "passengerOpen": pw.get("PwPosPsngr") == 1,
            "rearLeftOpen": pw.get("PwPosRl") == 1,
            "rearRightOpen": pw.get("PwPosRr") == 1,
        },
        "tirePressure": {
            "frontLeftPsi": tpms.get("FLTPrsDispPsi"),
            "frontRightPsi": tpms.get("FRTPrsDispPsi"),
            "rearLeftPsi": tpms.get("RLTPrsDispPsi"),
            "rearRightPsi": tpms.get("RRTPrsDispPsi"),
        },
        "oil": {
            "lifePercent": oil.get("DROilDeteriorateLevel"),
            "nextChange": oil_next,
            "levelStatus": oil.get("OilLevelStatusMonitor"),
        },
        "hazardLights": alert.get("HazardLamp", {}).get("HazardSw") == 1,
    }


@app.get("/health")
async def get_health(units: Units = Query(Units.imperial)):
    """Health report (warning lights)."""
    raw = await _api_call(state.controller.get_health_report, state.internal_vin)
    remote = raw.get("remoteInfos", [{}])[0]
    imperial = units == Units.imperial
    return {
        "units": units.value,
        "odometer": remote.get("OdoDispValueMile") if imperial else remote.get("OdoDispValue"),
        "warnings": {
            "oilAmountExceed": remote.get("WngOilAmountExceed") == 1,
            "oilShortage": remote.get("WngOilShortage") == 1,
            "headLamp": remote.get("WngHeadLamp") == 1,
            "smallLamp": remote.get("WngSmallLamp") == 1,
            "turnLamp": remote.get("WngTurnLamp") == 1,
            "tailLamp": remote.get("WngTailLamp") == 1,
            "brakeLamp": remote.get("WngBreakLamp") == 1,
            "rearFogLamp": remote.get("WngRearFogLamp") == 1,
            "backLamp": remote.get("WngBackLamp") == 1,
            "tirePressureLow": remote.get("WngTyrePressureLow") == 1,
            "tpmsStatus": remote.get("WngTpmsStatus") == 1,
        },
    }


# ---------------------------------------------------------------------------
# Write endpoints
# ---------------------------------------------------------------------------
@app.post("/refresh")
async def refresh_status():
    """Wake the TCU and request fresh telemetry from the car."""
    result = await _api_call(state.controller.refresh_vehicle_status, state.internal_vin)
    return {"result": result.get("resultCode")}


@app.post("/lock")
async def lock_doors():
    result = await _api_call(state.controller.door_lock, state.internal_vin)
    return {"result": result.get("resultCode")}


@app.post("/unlock")
async def unlock_doors():
    result = await _api_call(state.controller.door_unlock, state.internal_vin)
    return {"result": result.get("resultCode")}


@app.post("/engine/start")
async def engine_start():
    """Remote start engine. Max 2 consecutive starts before driving."""
    result = await _api_call(state.controller.engine_start, state.internal_vin)
    return {"result": result.get("resultCode")}


@app.post("/engine/stop")
async def engine_stop():
    result = await _api_call(state.controller.engine_stop, state.internal_vin)
    return {"result": result.get("resultCode")}


class FlashRequest(BaseModel):
    count: int = 2  # 2 = short (2 flashes + beeps), 30 = long (2 beeps + 8 silent)


_FLASH_PARAMS = {2: 0, 30: 2}


@app.post("/lights/flash")
async def flash_lights(req: FlashRequest):
    if req.count not in _FLASH_PARAMS:
        raise HTTPException(status_code=400, detail="count must be 2 or 30")
    result = await _api_call(state.controller.flash_lights, state.internal_vin, _FLASH_PARAMS[req.count])
    return {"result": result.get("resultCode"), "flashes": req.count}
