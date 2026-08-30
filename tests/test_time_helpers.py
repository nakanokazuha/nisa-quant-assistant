from datetime import datetime, timezone


def current_utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()
