"""Free Firebase Cloud Messaging delivery for Allerion server alerts.

Credentials are loaded only from a local service-account JSON file. Never commit
that file or place it in the Android application.
"""
import json
import os
import threading
import time

DATA_DIR = os.environ.get("DATA_DIR", "/data")
TOKENS_FILE = os.path.join(DATA_DIR, "fcm_tokens.json")
SERVICE_ACCOUNT_FILE = os.environ.get(
    "FCM_SERVICE_ACCOUNT_FILE", "/run/secrets/firebase-service-account.json"
)
PROJECT_ID = os.environ.get("FCM_PROJECT_ID", "").strip()
_lock = threading.RLock()
_app = None


def _read_tokens():
    try:
        with open(TOKENS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_tokens(tokens):
    os.makedirs(DATA_DIR, exist_ok=True)
    temporary = TOKENS_FILE + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(tokens, handle)
    os.replace(temporary, TOKENS_FILE)
    os.chmod(TOKENS_FILE, 0o600)


def registered_count():
    with _lock:
        return len(_read_tokens())


def configured():
    if not os.path.isfile(SERVICE_ACCOUNT_FILE):
        return False
    try:
        with open(SERVICE_ACCOUNT_FILE, "r", encoding="utf-8") as handle:
            account = json.load(handle)
        return bool(PROJECT_ID or account.get("project_id"))
    except (OSError, ValueError, TypeError):
        return False


def register_device(token):
    token = str(token or "").strip()
    if not token or len(token) > 4096:
        raise ValueError("A valid FCM registration token is required")
    if not configured():
        raise RuntimeError("FCM is not configured on the server")
    with _lock:
        tokens = _read_tokens()
        tokens[token] = {"registered_at": int(time.time())}
        _write_tokens(tokens)
        return {"ok": True, "registered": True, "devices": len(tokens)}


def unregister_device(token):
    token = str(token or "").strip()
    if not token:
        raise ValueError("An FCM registration token is required")
    with _lock:
        tokens = _read_tokens()
        tokens.pop(token, None)
        _write_tokens(tokens)
        return {"ok": True, "registered": False, "devices": len(tokens)}


def _firebase():
    global _app
    if _app is not None:
        return _app
    if not configured():
        raise RuntimeError("FCM service-account credentials are not configured")
    import firebase_admin
    from firebase_admin import credentials
    with _lock:
        if _app is None:
            credential = credentials.Certificate(SERVICE_ACCOUNT_FILE)
            options = {"projectId": PROJECT_ID} if PROJECT_ID else None
            try:
                _app = firebase_admin.get_app()
            except ValueError:
                _app = firebase_admin.initialize_app(credential, options)
    return _app


def send_message(title, body, data=None):
    """Send one push to each registered device; remove expired tokens."""
    with _lock:
        tokens = list(_read_tokens().keys())
    if not tokens:
        return {"ok": True, "sent": 0, "devices": 0}
    from firebase_admin import messaging
    app = _firebase()
    sent = 0
    invalid = []
    errors = 0
    payload = {str(k): str(v) for k, v in (data or {}).items() if v is not None}
    for token in tokens:
        message = messaging.Message(
            token=token,
            notification=messaging.Notification(title=str(title)[:120], body=str(body)[:1000]),
            data=payload,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="allerion_alerts", sound="default"
                ),
            ),
        )
        try:
            messaging.send(message, app=app)
            sent += 1
        except messaging.UnregisteredError:
            invalid.append(token)
        except Exception:
            errors += 1
    if invalid:
        with _lock:
            current = _read_tokens()
            for token in invalid:
                current.pop(token, None)
            _write_tokens(current)
    return {"ok": errors == 0, "sent": sent, "devices": len(tokens),
            "removed_invalid": len(invalid), "failed": errors}


def send_alert(event):
    recovered = bool(event.get("recovered"))
    server = str(event.get("server") or "Server")
    metric = str(event.get("metric") or "health")
    value = event.get("value")
    threshold = event.get("threshold")
    title = "ALLERION · " + ("Recovered" if recovered else "Alert")
    if recovered:
        body = server + " · " + metric.upper() + " returned to normal."
    else:
        body = server + " · " + metric.upper() + " is " + str(value)
        if threshold is not None:
            body += " (threshold " + str(threshold) + ")"
    return send_message(title, body, {
        "type": "alert", "server": server, "metric": metric,
        "recovered": "true" if recovered else "false",
        "timestamp": event.get("timestamp", int(time.time() * 1000)),
    })


def send_test():
    if not configured():
        raise RuntimeError("FCM service-account credentials are not configured")
    if registered_count() == 0:
        raise RuntimeError("No Allerion devices are registered for push notifications")
    return send_message("ALLERION · Test notification",
                        "Push notifications are connected and working.",
                        {"type": "test", "timestamp": int(time.time() * 1000)})
