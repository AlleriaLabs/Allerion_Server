# Free push notifications with Firebase Cloud Messaging

Allerion can deliver threshold alerts and recovery messages to registered Android devices through Firebase Cloud Messaging (FCM). FCM is a no-cost Firebase product; this integration does not use SMS, mobile load, or a paid SMS gateway.

## 1. Create the Firebase project

1. Create a Firebase project using the no-cost Spark plan: https://console.firebase.google.com/
2. Add an Android app with the exact application ID from the Allerion Android project: `com.example.servermonitor`.
3. Download the Android `google-services.json` file. Keep it local in the Android repository at `app/google-services.json`; do not add it to a public repository unless you intentionally want to publish the app's Firebase client configuration.
4. In Firebase Project settings → Service accounts, generate a private key for the Firebase Admin SDK.

## 2. Install server credentials

On the CasaOS host, create:

```bash
mkdir -p /DATA/AppData/server-monitor/secrets
```

Copy the downloaded service-account JSON to:

```
/DATA/AppData/server-monitor/secrets/firebase-service-account.json
```

Restrict access:

```bash
chmod 600 /DATA/AppData/server-monitor/secrets/firebase-service-account.json
```

**Never upload this service-account JSON to GitHub or send it in chat.** It grants server-side access to the Firebase project.

The Compose file mounts this directory read-only at `/run/secrets`. The server reads the project ID from the service-account JSON automatically; `FCM_PROJECT_ID` is optional.

## 3. Build and install the Android app

The Android build reads the Firebase client configuration from `app/google-services.json`. The file is optional at compile time so developers can still build the app without push configured, but FCM remains inactive until the file is present.

On Android 13 and newer, allow Allerion to show notifications when prompted. Also ensure Google Play services and background data are enabled for Allerion.

## 4. Deploy the server

From the deployment directory:

```bash
cd /DATA/AppData/server-monitor
docker compose up -d --build --force-recreate server-monitor
```

The server stores device registration tokens in `./data/fcm_tokens.json` with restrictive file permissions. Authenticated Android clients register at `POST /api/notifications/fcm/register`; the token is never stored in the app repository.

## 5. Verify

After installing the FCM-enabled APK and opening Allerion with a configured server connection, check the server's authenticated `GET /api/integrations` response for an `fcm` item showing `configured: true` and `devices: 1` (or the number of registered devices).

Send a test notification with authenticated `POST /api/integrations/fcm/test`.

Threshold-crossing and recovery events use the existing Allerion alert pipeline. Existing webhook and Telegram integrations remain unchanged.
