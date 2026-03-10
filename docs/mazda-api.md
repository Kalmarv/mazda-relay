# Mazda Connected Services API — Reference

How the Mazda v2 API works, how we authenticate, and how to fix it when it breaks.

## Architecture Overview

```
MyMazda App (iOS/Android)
    │
    ├── OAuth2 (Azure AD B2C) ──► na.id.mazda.com
    │       └── Returns access_token + refresh_token
    │
    └── Mazda API ──► hgs2ivna.mazda.com
            ├── AES-128-CBC encrypted payloads
            ├── Akamai Bot Manager (X-acf-sensor-data)
            ├── TLS fingerprinting (specific cipher suite + sig algorithms)
            └── Device session management (attach/detach)
```

The relay impersonates an Android MyMazda app. All API traffic goes to `hgs2ivna.mazda.com` (North America). Requests are AES-encrypted with keys derived from the app identity, and responses are decrypted the same way.

## The HACS Repo (fano0001)

**Repo:** `fano0001/home-assistant-mazda` (GitHub)
**Branch we track:** `v2.2.0-beta` (most active, has OAuth2 flow)
**Submodule path:** `vendor/ha-mazda/`

This is the only working implementation of the Mazda v2 API. The original pymazda (bdr99) used the v1 API which was shut down in Feb 2026.

### Key source files

| File | What it does |
|------|-------------|
| `pymazda/connection.py` | Core API client — encryption, headers, Akamai sensor data, TLS config |
| `pymazda/controller.py` | All API endpoints (attach, getVehicleStatus, doorLock, etc.) |
| `pymazda/client.py` | High-level wrapper used by HA integration (processes raw responses) |
| `pymazda/crypto_utils.py` | AES-128-CBC encrypt/decrypt |
| `pymazda/sensordata/` | Akamai Bot Manager sensor data generator (9 files) |
| `pymazda/ssl_context_configurator/` | TLS fingerprinting via ctypes/libssl |
| `pymazda/exceptions.py` | Error types (encryption, token expired, session conflict, ToS) |
| `__init__.py` | HA integration glue (setup, coordinators, service registration) |

### How encryption works

1. **Key derivation:** `MD5(app_code + package_name)` → `MD5(result + cert_sig)` → extract `enc_key` (bytes 4-20) and `sign_key` (bytes 20-32 + 0-10 + 4-6)
2. **Initial keys:** Call `checkVersion` endpoint → server returns `encKey` + `signKey` encrypted with the derived key
3. **Request signing:** `SHA256(encrypted_payload + timestamp + timestamp_slices + sign_key)`
4. **Payload encryption:** AES-128-CBC with the server-provided `encKey` and fixed IV `0102030405060708`

### Android app constants (from connection.py)

```
APP_PACKAGE_ID  = "com.interrait.mymazda"
APP_OS          = "ANDROID"
APP_VERSION     = "9.0.8"
USER_AGENT      = "MyMazda/9.0.8 (Linux; Android 14)"
SHA256_CERT_SIG = "C022C9EE778CF903838F8B9C4B9FF0036A5C516CEFAAD6DC710B717CF97DCFCA"
SIGNATURE_MD5   = "C383D8C4D279B78130AD52DC71D95CAA"
app_code (MNAO) = "498345786246797888995"
```

If Mazda updates the app, these constants change and everything breaks. Check the APK.

## Authentication Flow

### OAuth2 (Azure AD B2C)

```
Endpoint: https://na.id.mazda.com/{tenant_id}/b2c_1a_signin/oauth2/v2.0/token
Tenant:   47801034-62d1-49f6-831b-ffdcf04f13fc
Client:   2daf581c-65c1-4fdb-b46a-efa98c6ba5b7
Scope:    https://pduspb2c01.onmicrosoft.com/0728deea-be48-4382-9ef1-d4ff6d679ffa/cv openid profile offline_access
Redirect: msauth.com.mazdausa.mazdaiphone://auth
```

- **Access token:** 2 hours, used in `Authorization: Bearer` and `access-token` headers
- **Refresh token:** 30 days, but slides forward on each use (effectively infinite if the relay refreshes regularly)
- The relay persists the new refresh token to `.env` after every refresh

### Initial token acquisition

You need a one-time MITM capture of the iOS MyMazda app to get the initial refresh token. After that, the relay maintains it indefinitely by refreshing before expiry.

### Device session (attach/detach)

After OAuth, you must call `remoteServices/attach/v4` to register a device session. This returns a `sessionId` that must be sent as `X-device-session-id` on all subsequent requests.

**Critical:** Only one device session is allowed at a time. Calling `attach` kicks the previous device (including the phone app). The relay becomes the sole connected device.

**Session ID extraction:** The sessionId is nested at `response["data"]["userinfo"]["sessionId"]`, NOT at the top level. Getting this wrong causes all subsequent calls to fail with `errorCode: 920000, extraCode: CST400000` ("Please check the input and try again (400C01)") — which looks like a ToS error but is actually a missing header.

## MITM Proxy Setup

We have mitmproxy running on the server for capturing iOS app traffic.

### How to capture new tokens

1. Run mitmproxy (any setup — Docker, local, etc.)
2. Configure iOS device to use the proxy
3. Install mitmproxy CA cert on iOS (visit `mitm.it` through the proxy)
4. Open MyMazda app, log out, log back in
5. Extract the refresh token from the OAuth token response in the captured flows

### iOS app identity (from MITM capture)

```
device-id:     SHA1 of email (same as Android)
app-code:      635529297359258474866 (different from Android)
app-unique-id: com.mazdausa.mazdaiphone
app-os:        IOS
User-Agent:    MyMazda-ios/9.0.10
```

We don't use these (we impersonate Android), but they're documented here for reference. The iOS app uses different encryption key derivation (different app_code, cert_sig) and the Akamai sensor data generator only produces Android fingerprints.

## When Things Break

### Symptom → Cause → Fix

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `errorCode: 600001` | Encryption keys rejected | App version updated — check APK for new `SHA256_CERT_SIG`, `SIGNATURE_MD5`, `app_code` |
| `errorCode: 600002` | Access token expired | Should auto-refresh; check refresh token hasn't expired (30 days without use) |
| `errorCode: 600100` | Session conflict | Another device called `attach` — re-attach |
| `errorCode: 920000, extraCode: 400S01` | Request in progress | Wait 30s and retry (another command is running) |
| `errorCode: 920000, extraCode: CST400000` | Missing `X-device-session-id` header | Session ID not set after attach — check extraction path |
| OAuth refresh 400/401 | Refresh token expired | Re-capture via MITM proxy |
| SSL/TLS errors | TLS fingerprint mismatch | Mazda updated server TLS config — check cipher suites in connection.py |
| Akamai 403 | Bot detection | Sensor data generator needs updating — check fano0001 repo for updates |
| All API calls return HTML | Cloudflare/WAF block | IP reputation issue or rate limiting — wait and retry |

### Where to look for updates

1. **fano0001/home-assistant-mazda** — check `v2.2.0-beta` branch for commits, especially changes to `connection.py` constants
2. **APK analysis** — if app version changes, decompile new APK for updated constants
3. **MITM capture** — compare current request headers/payloads against what the real app sends
4. **fano0001 issues/discussions** — other users report breakages first

### Updating the submodule

```bash
cd vendor/ha-mazda
git fetch origin
git log --oneline v2.2.0-beta..origin/v2.2.0-beta  # see what's new
git pull origin v2.2.0-beta
cd ../..
# rebuild container
```

## Vehicle Info

Vehicle-specific details (VIN, internalVin, etc.) are discovered automatically at startup via `getVecBaseInfos/v4`. The relay currently uses the first vehicle on the account.
