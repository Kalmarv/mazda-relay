"""Test: use fano0001's pymazda to auth and dump vehicle info via Mazda v2 API."""

import asyncio
import json
import os
import sys

# Add the submodule's pymazda to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "vendor/ha-mazda/custom_components/mazda_cs"))

import aiohttp  # noqa: E402

from pymazda.connection import Connection  # noqa: E402
from pymazda.controller import Controller  # noqa: E402


OAUTH_CONFIG = {
    "host": "https://na.id.mazda.com",
    "tenant_id": "47801034-62d1-49f6-831b-ffdcf04f13fc",
    "client_id": "2daf581c-65c1-4fdb-b46a-efa98c6ba5b7",
    "scope": "https://pduspb2c01.onmicrosoft.com/0728deea-be48-4382-9ef1-d4ff6d679ffa/cv openid profile offline_access",
    "redirect_uri": "msauth.com.mazdausa.mazdaiphone://auth",
}


class TokenManager:
    """Manages OAuth tokens — refreshes automatically, persists new refresh tokens."""

    def __init__(self, refresh_token: str, env_path: str | None = None):
        self.refresh_token = refresh_token
        self.access_token: str | None = None
        self.env_path = env_path

    async def get_access_token(self) -> str:
        """Called by Connection when it needs an access token."""
        if self.access_token:
            return self.access_token
        await self.refresh()
        return self.access_token

    async def refresh(self):
        """Exchange refresh token for new access + refresh tokens."""
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
                    print(f"OAuth refresh failed ({resp.status}): {text}")
                    sys.exit(1)
                token_data = await resp.json()

        self.access_token = token_data["access_token"]
        old_refresh = self.refresh_token
        self.refresh_token = token_data["refresh_token"]

        expires_in = token_data.get("expires_in", "?")
        refresh_expires = token_data.get("refresh_token_expires_in", "?")
        print(f"Access token expires in: {expires_in}s")
        print(f"Refresh token expires in: {refresh_expires}s ({int(refresh_expires) // 86400}d)")

        # Persist new refresh token to .env
        if self.env_path and os.path.exists(self.env_path):
            with open(self.env_path) as f:
                content = f.read()
            if old_refresh in content:
                content = content.replace(old_refresh, self.refresh_token)
                with open(self.env_path, "w") as f:
                    f.write(content)
                print("Updated .env with new refresh token")


async def main():
    refresh_token = os.environ.get("MAZDA_REFRESH_TOKEN")
    email = os.environ.get("MAZDA_EMAIL")

    if not refresh_token or not email:
        print("Set MAZDA_REFRESH_TOKEN and MAZDA_EMAIL env vars")
        sys.exit(1)

    env_path = os.path.join(os.path.dirname(__file__), ".env")
    token_mgr = TokenManager(refresh_token, env_path)

    # Step 1: Refresh OAuth token
    print("--- Refreshing OAuth token ---")
    await token_mgr.refresh()

    # Step 2: Create controller using fano0001's pymazda
    print("\n--- Connecting to Mazda API ---")
    controller = Controller(
        email=email,
        region="MNAO",
        access_token_provider=token_mgr.get_access_token,
    )

    try:
        # Step 3: Attach device session
        print("\n--- Attaching device session ---")
        attach_resp = await controller.attach()
        session_id = attach_resp.get("data", {}).get("userinfo", {}).get("sessionId")
        if session_id:
            controller.connection.device_session_id = session_id
            print(f"Session ID: {session_id}")
        else:
            print("WARNING: No sessionId in attach response!")
        print(json.dumps(attach_resp, indent=2, default=str))

        # Step 4: Get vehicles
        print("\n--- Getting vehicles ---")
        vehicles = await controller.get_vec_base_infos()
        print(json.dumps(vehicles, indent=2, default=str))

        # Step 5: Get status for each vehicle
        vec_infos = vehicles.get("vecBaseInfos", [])
        for v in vec_infos:
            cv_info = v.get("Vehicle", {}).get("CvInformation", {})
            internal_vin = cv_info.get("internalVin")
            vin = v.get("vin", "unknown")
            print(f"\n--- Vehicle: {vin} (internalVin: {internal_vin}) ---")

            if internal_vin:
                # Force fresh telemetry from the car's TCU
                try:
                    print("\nRefreshing vehicle status (waking TCU)...")
                    refresh_resp = await controller.refresh_vehicle_status(internal_vin)
                    print(f"Refresh result: {refresh_resp.get('resultCode')}")
                except Exception as e:
                    print(f"refreshVehicleStatus failed: {e}")

                # Brief pause to let TCU push fresh data
                import time
                print("Waiting 10s for TCU to report...")
                time.sleep(10)

                try:
                    print("\nStatus:")
                    status = await controller.get_vehicle_status(internal_vin)
                    print(json.dumps(status, indent=2, default=str))
                except Exception as e:
                    print(f"getVehicleStatus failed: {e}")

                try:
                    print("\nHealth Report:")
                    health = await controller.get_health_report(internal_vin)
                    print(json.dumps(health, indent=2, default=str))
                except Exception as e:
                    print(f"getHealthReport failed: {e}")

    finally:
        await controller.close()


asyncio.run(main())
