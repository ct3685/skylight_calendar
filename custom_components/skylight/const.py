"""Constants for the shopping-list-only Skylight Home Assistant integration."""

DOMAIN = "skylight"

CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_DEVICE_FINGERPRINT = "device_fingerprint"
CONF_FRAME_ID = "frame_id"
CONF_FRAME_NAME = "frame_name"

# UNOFFICIAL Skylight API using the website's OAuth client.
# Important: the server currently requires "everything" OAuth scope.
# Removing non-list features from this fork DOES NOT narrow token privileges.
BASE_URL = "https://app.ourskylight.com"
OAUTH_URL = "https://app.ourskylight.com/oauth/token"
OAUTH_AUTHORIZE_URL = "https://app.ourskylight.com/oauth/authorize"
OAUTH_REDIRECT_URI = "https://ourskylight.com/welcome"
OAUTH_CLIENT_ID = "skylight-mobile"
CLIENT_ID = OAUTH_CLIENT_ID
OAUTH_SCOPE = "everything"
API_VERSION = "2026-05-01"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

LISTS_SCAN_INTERVAL = 120
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 7200
CONF_LISTS_INTERVAL = "lists_scan_interval"
SCAN_INTERVAL_OPTIONS = {CONF_LISTS_INTERVAL: LISTS_SCAN_INTERVAL}
