# Mazda Relay

FastAPI service that wraps the Mazda Connected Services v2 API. Provides a simple REST interface for reading vehicle telemetry and sending remote commands.

Built on [fano0001/home-assistant-mazda](https://github.com/fano0001/home-assistant-mazda) (`v2.2.0-beta`), the only working implementation of the Mazda v2 API.

## Setup

### 1. Clone with submodule

```bash
git clone --recurse-submodules https://github.com/Kalmarv/mazda-relay.git
cd mazda-relay
```

If you already cloned without `--recurse-submodules`:

```bash
git submodule update --init --recursive
```

### 2. Get your refresh token

The Mazda API uses OAuth2 via Azure AD B2C. You need a one-time token capture to get a refresh token. After that, the relay maintains it indefinitely by auto-refreshing.

There are two ways to get the token:

#### Option A: Browser extension (easier)

The [fano0001/home-assistant-mazda](https://github.com/fano0001/home-assistant-mazda) HACS repo includes a browser extension that can capture the refresh token directly from the MyMazda web login flow. Check the repo's README/wiki for the extension and instructions.

#### Option B: MITM proxy

Intercept the MyMazda iOS app's traffic to extract the token.

**Prerequisites:**
- [mitmproxy](https://mitmproxy.org/) running on any machine on your network
- iOS device with MyMazda installed

**Steps:**

1. Start mitmproxy:
   ```bash
   mitmproxy          # interactive TUI
   # or
   mitmweb            # browser UI at localhost:8081
   ```

2. Configure your iOS device to use the proxy:
   - Go to **Settings → Wi-Fi → (your network) → Configure Proxy → Manual**
   - Set the proxy host/port to your mitmproxy machine

3. Install the mitmproxy CA certificate:
   - On the iOS device, open Safari and go to `mitm.it`
   - Download and install the iOS profile
   - Go to **Settings → General → About → Certificate Trust Settings** and enable full trust for the mitmproxy cert

4. In the MyMazda app, log out and log back in

5. In mitmproxy, find the request to `na.id.mazda.com` containing the token response. Copy the `refresh_token` value from the JSON response body.

> **Tip:** Filter mitmproxy with `~d na.id.mazda.com` to find the token exchange quickly.

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:
```
MAZDA_EMAIL=your-mymazda-email@example.com
MAZDA_REFRESH_TOKEN=the-long-token-string-from-step-2
```

### 4. Build and run

```bash
docker build -t mazda-relay .
docker run -d \
  --name mazda-relay \
  --env-file .env \
  -v "$(pwd)/.env:/app/.env" \
  -p 8201:8200 \
  --restart always \
  mazda-relay
```

The `.env` volume mount is required — the relay writes rotated refresh tokens back to it so they survive container restarts.

Check it's working:

```bash
docker logs mazda-relay
# Should see: "Vehicle: 2024 MAZDA3 ..." and "Application startup complete."

curl localhost:8201/vehicle
# Should return your vehicle info
```

## API

All read endpoints accept an optional `?units=imperial|metric` query parameter (default: `imperial`).

**Read:**
| Endpoint | Description |
|----------|-------------|
| `GET /vehicle` | Static info — model, VIN, color, engine, transmission, capabilities |
| `GET /status` | Live telemetry — fuel, odometer, location, doors, locks, windows, tires, oil |
| `GET /health` | Warning lights report |

**Write:**
| Endpoint | Description |
|----------|-------------|
| `POST /refresh` | Wake TCU and request fresh telemetry (~10s) |
| `POST /lock` | Lock all doors |
| `POST /unlock` | Unlock all doors |
| `POST /engine/start` | Remote start (max 2 consecutive before driving) |
| `POST /engine/stop` | Stop remotely-started engine |
| `POST /lights/flash` | Flash lights — `{"count": 2}` (short) or `{"count": 30}` (long) |

Interactive docs at `/docs` (Swagger UI).

## Troubleshooting

### Refresh token expired

If the relay hasn't run in 30+ days, the refresh token expires. Re-capture via MITM (step 2 above).

### "Application startup failed" / env var errors

Make sure you're passing both `--env-file .env` (loads vars into the container) and `-v .env:/app/.env` (lets the relay write back rotated tokens).

### API calls fail after phone app use

Only one device session is allowed at a time. Opening the MyMazda phone app kicks the relay's session. The relay auto-reconnects on the next request, but if it doesn't, restart the container.

### Everything worked yesterday, now nothing does

Mazda probably updated the app. Check [fano0001/home-assistant-mazda](https://github.com/fano0001/home-assistant-mazda) for recent commits on `v2.2.0-beta`. Update the submodule and rebuild:

```bash
cd vendor/ha-mazda
git pull origin v2.2.0-beta
cd ../..
docker build -t mazda-relay . && docker restart mazda-relay
```

See [docs/mazda-api.md](docs/mazda-api.md) for the full technical reference — encryption, headers, error codes, and app constants.
