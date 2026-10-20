"""Constants for the Huawei Scale 3 integration."""
from __future__ import annotations

DOMAIN = "huawei_scale3"

# Config entry data
CONF_CAK = "cak"
CONF_C1 = "c1"
CONF_C2 = "c2"

# Config entry options
CONF_PROFILES = "profiles"
CONF_READ_HISTORY = "read_history"
CONF_COOLDOWN = "cooldown"

DEFAULT_READ_HISTORY = False
DEFAULT_COOLDOWN = 60  # seconds to ignore the scale after a successful weigh-in
MIN_COOLDOWN = 10
MAX_COOLDOWN = 600

# Profile fields (stored in options)
CONF_NAME = "name"
CONF_SEX = "sex"
CONF_BIRTH_DATE = "birth_date"
CONF_HEIGHT = "height_cm"
CONF_INITIAL_WEIGHT = "initial_weight_kg"

SEX_MALE = "male"
SEX_FEMALE = "female"

EVENT_MEASUREMENT = f"{DOMAIN}_measurement"

# Session outcome after an error, before we try again on the next advertisement
RETRY_DELAY = 15
AUTH_RETRY_DELAY = 120
AUTH_FAILURES_BEFORE_REAUTH = 3
SESSION_TIMEOUT = 150

STATUS_OPTIONS = [
    "idle",
    "connecting",
    "authenticating",
    "syncing",
    "measuring",
    "reading_history",
    "error",
    "auth_error",
]
