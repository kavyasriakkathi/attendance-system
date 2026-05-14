import os
import logging
from dotenv import find_dotenv, dotenv_values

MAIL_KEYS = [
    "MAIL_USERNAME",
    "MAIL_PASSWORD",
    "MAIL_SERVER",
    "MAIL_PORT",
    "MAIL_USE_TLS",
    "MAIL_FROM",
    "RESEND_API_KEY",
    "REPORT_ADMIN_EMAIL",
]


def _mask_env_value(key: str, value: str) -> str:
    if value is None:
        return "<not set>"
    s = str(value)
    lower = key.lower()
    if any(tok in lower for tok in ("password", "secret", "api_key", "apikey", "token")):
        if len(s) <= 6:
            return "<set>"
        return s[:3] + "..." + s[-3:]
    if "@" in s:
        parts = s.split("@")
        user = parts[0]
        if len(user) <= 2:
            masked_user = "*"
        else:
            masked_user = user[0] + "..." + user[-1]
        return masked_user + "@" + parts[1]
    if len(s) <= 4:
        return s[0] + "..."
    return s[:2] + "..." + s[-2:]


def _read_dotenv_values():
    """Return mapping of dotenv values if a .env file exists, else empty dict."""
    path = find_dotenv()
    if not path:
        return {}, None
    try:
        vals = dotenv_values(path)
        return vals or {}, path
    except Exception:
        return {}, path


def setup_mail_config(app):
    """Centralized mail configuration loader and startup diagnostics.

    - Loads environment variables (os.environ takes precedence)
    - Reads .env file values for diagnostics (does not override os.environ)
    - Sets `app.config` keys for mail-related settings where applicable
    - Prints masked diagnostics to stdout and returns a dict report
    """
    logger = logging.getLogger("app.mailconfig")

    dotenv_vals, dotenv_path = _read_dotenv_values()

    report = {"source": {}, "values": {}, "missing": []}

    for key in MAIL_KEYS:
        env_val = os.environ.get(key)
        dot_val = dotenv_vals.get(key) if dotenv_vals is not None else None

        if env_val is not None and str(env_val).strip() != "":
            source = "env"
            value = env_val
        elif dot_val is not None and str(dot_val).strip() != "":
            source = ".env"
            value = dot_val
        else:
            source = "none"
            value = None

        report["source"][key] = source
        report["values"][key] = _mask_env_value(key, value)
        if source == "none":
            report["missing"].append(key)

        # Apply safe defaults only for server/port/use_tls where sensible
        if key == "MAIL_SERVER" and value is None:
            value = "smtp.gmail.com"
        if key == "MAIL_PORT" and value is None:
            value = 587
        if key == "MAIL_USE_TLS" and value is None:
            value = True if str(app.config.get("MAIL_USE_TLS", "True")).lower() in ("true", "1", "yes") else False

        # Persist into app.config for centralized lookups
        try:
            app.config[key] = value
        except Exception:
            # Should not crash startup
            logger.exception("Failed to set app.config[%s]", key)

    # Print summary to stdout so it's visible on Render logs
    env_note = ".env file loaded" if dotenv_path else "no .env file"
    print(f"[mail.config] Mail config startup diagnostics ({env_note}). os.environ keys take precedence over .env values.")
    if dotenv_path:
        print(f"[mail.config] .env path: {dotenv_path}")

    for key in MAIL_KEYS:
        src = report["source"][key]
        masked = report["values"][key]
        print(f"[mail.config] {key}: {masked} (source={src})")

    if report["missing"]:
        print(f"[mail.config] Missing mail vars: {', '.join(report['missing'])}")
    else:
        print("[mail.config] All mail vars detected (masked above).")

    # Attach the report to app for runtime inspection if needed
    try:
        app.mail_config_report = report
    except Exception:
        pass

    return report


def is_mail_configured(app):
    """Return True if we have enough information to send mail.

    This prefers RESEND_API_KEY + MAIL_FROM if present; otherwise falls back
    to SMTP credentials (MAIL_USERNAME, MAIL_PASSWORD, MAIL_FROM).
    """
    resend = app.config.get("RESEND_API_KEY")
    mail_from = app.config.get("MAIL_FROM")
    if resend and mail_from:
        return True
    username = app.config.get("MAIL_USERNAME")
    password = app.config.get("MAIL_PASSWORD")
    return bool(username and password and mail_from)


def get_report(app):
    return getattr(app, "mail_config_report", None)
