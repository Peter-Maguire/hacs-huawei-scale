"""Constants for the Huawei Scale 3 integration."""
from __future__ import annotations

DOMAIN = "huawei_scale3"

# Config entry data. These are optional overrides: when an entry has no keys
# the built-in defaults below are used.
CONF_CAK = "cak"
CONF_C1 = "c1"
CONF_C2 = "c2"

# Built-in Huawei Health secrets (see FINDINGS-hagrid-protocol.md, step 3).
# The app's native library calls them CAK (id 33), A (id 1033) and B (id 2033);
# this integration's C1/C2 are A/B. Each is 16 bytes, hex-encoded.
DEFAULT_CAK = "90B96ECA297EF78717E66E491084D3F8"
DEFAULT_C1 = "CA4946D061C9FE534F6044F930EBB69B"
DEFAULT_C2 = "FBCE6E2B4BAF80ED969BA26B4A4B9325"

# Config entry options
CONF_PROFILES = "profiles"
CONF_READ_HISTORY = "read_history"
CONF_COOLDOWN = "cooldown"
CONF_BIND_UNBOUND = "bind_unbound_scale"

DEFAULT_READ_HISTORY = False
DEFAULT_BIND_UNBOUND = False
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
