# Mazda Relay

FastAPI service that wraps the Mazda Connected Services v2 API. Provides a simple REST interface for reading vehicle telemetry and sending remote commands.

## API

**Read:**
- `GET /vehicle` — model, VIN, color, features
- `GET /status` — fuel, odometer, location, doors, locks, windows, tires, oil
- `GET /health` — warning lights
- `POST /refresh` — wake TCU for fresh telemetry

**Write:**
- `POST /lock` / `POST /unlock`
- `POST /engine/start` / `POST /engine/stop`
- `POST /lights/flash` — `{"count": 2}` or `{"count": 30}`

Interactive docs at `/docs` (Swagger UI).

## Setup

Requires a one-time MITM capture of the iOS MyMazda app to get a refresh token. See [docs/mazda-api.md](docs/mazda-api.md) for the full auth flow.

```bash
cp .env.example .env
# Fill in MAZDA_EMAIL and MAZDA_REFRESH_TOKEN
```

## Run

```bash
docker build -t mazda-relay .
docker run -d --name mazda-relay --env-file .env -v "$(pwd)/.env:/app/.env" -p 8201:8200 mazda-relay
```

The `.env` volume mount is required — the relay persists rotated refresh tokens back to it.

## How it works

Uses [fano0001/home-assistant-mazda](https://github.com/fano0001/home-assistant-mazda) (v2.2.0-beta) as a git submodule for the pymazda v2 client. The relay impersonates an Android MyMazda app with synthetic Akamai sensor data and TLS fingerprinting.

On startup: refreshes OAuth token → attaches device session → discovers vehicle. Background reconnection handles token expiry and session conflicts automatically.

See [docs/mazda-api.md](docs/mazda-api.md) for architecture details and troubleshooting.
