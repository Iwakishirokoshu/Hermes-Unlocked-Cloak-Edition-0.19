# ============================================
# 📝 CONSTANTS - Magic Numbers & Fixed Values
# 
# This file contains all magic numbers and fixed values
# used throughout the application for better maintainability.
# ============================================

# ═══════════════════════════════════════════════════════════════
#                    TIMING CONSTANTS (in seconds)
# ═══════════════════════════════════════════════════════════════

# Page load timeouts
PAGE_LOAD_TIMEOUT = 30
ELEMENT_WAIT_TIMEOUT = 10
BROWSER_TIMEOUT = 20

# Human-like typing delays (min, max in seconds)
TYPING_DELAY_MIN = 0.08
TYPING_DELAY_MAX = 0.22

# Mouse movement delays
MOUSE_MOVE_DELAY_MIN = 0.01
MOUSE_MOVE_DELAY_MAX = 0.03

# Click delays
PRE_CLICK_DELAY_MIN = 0.5
PRE_CLICK_DELAY_MAX = 1.5

# Session warming timings
SESSION_WARM_MIN = 3
SESSION_WARM_MAX = 5

# YouTube watch duration
YOUTUBE_WATCH_MIN = 20
YOUTUBE_WATCH_MAX = 40

# ═══════════════════════════════════════════════════════════════
#                    DATE/TIME CONSTANTS
# ═══════════════════════════════════════════════════════════════

# Milliseconds in a day
MILLISECONDS_PER_DAY = 86400000

# Milliseconds in an hour
MILLISECONDS_PER_HOUR = 3600000

# Year range for realistic birthdays
MIN_BIRTH_YEAR = 1970
MAX_BIRTH_YEAR = 2005

# Account age simulation range (days)
ACCOUNT_AGE_SIMULATION_MIN = 7
ACCOUNT_AGE_SIMULATION_MAX = 365

# ═══════════════════════════════════════════════════════════════
#                    FINGERPRINT CONSTANTS
# ═══════════════════════════════════════════════════════════════

# Canvas noise intensity
CANVAS_NOISE_INTENSITY = 0.0001

# Audio noise intensity
AUDIO_NOISE_INTENSITY = 0.0001

# WebGL noise probability
WEBGL_NOISE_PROBABILITY = 0.001

# Random pixel modification range
PIXEL_MODIFY_RANGE = 3

# Screen noise range (pixels)
SCREEN_NOISE_RANGE = 10

# ═══════════════════════════════════════════════════════════════
#                    RETRY & LIMIT CONSTANTS
# ═══════════════════════════════════════════════════════════════

# Maximum retry attempts
MAX_RETRIES = 3
MAX_BROWSER_RETRIES = 3
MAX_ELEMENT_FIND_RETRIES = 10

# Delay between retries (seconds)
RETRY_DELAY = 2

# Delay between accounts (seconds)
DELAY_BETWEEN_ACCOUNTS_DEFAULT = 10

# Random username number range
USERNAME_RANDOM_MIN = 1000
USERNAME_RANDOM_MAX = 9999

# ═══════════════════════════════════════════════════════════════
#                    SCROLL CONSTANTS
# ═══════════════════════════════════════════════════════════════

# Scroll amounts (pixels)
SCROLL_MIN = 200
SCROLL_MAX = 600

# Scroll step size
SCROLL_STEP_SMOOTH = 20
SCROLL_STEP_FAST = 100
SCROLL_STEP_HUMAN = 50

# ═══════════════════════════════════════════════════════════════
#                    TAB NAVIGATION CONSTANTS
# ═══════════════════════════════════════════════════════════════

# Tab key press count for keyboard bypass
KEYBOARD_TAB_COUNT = 10

# Tab press delay (seconds)
TAB_PRESS_DELAY = 0.3

# ═══════════════════════════════════════════════════════════════
#                    TRUST/COOKIE CONSTANTS
# ═══════════════════════════════════════════════════════════════

# Trust cookie valid days
TRUST_COOKIE_AGE_DAYS = 30

# Consent cookie random number range
CONSENT_COOKIE_MIN = 100
CONSENT_COOKIE_MAX = 999

# ═══════════════════════════════════════════════════════════════
#                    CHROME VERSION CONSTANTS
# ═══════════════════════════════════════════════════════════════

# Supported Chrome versions for fingerprint spoofing
CHROME_VERSIONS = ['128', '129', '130', '131', '132', '133']

# User agent build number range
BUILD_NUMBER_MIN = 6778
BUILD_NUMBER_MAX = 7000

# ═══════════════════════════════════════════════════════════════
#                    NETWORK CONSTANTS
# ═══════════════════════════════════════════════════════════════

# Connection timeout (seconds)
CONNECTION_TIMEOUT = 10

# Request timeout (seconds)
REQUEST_TIMEOUT = 30

# SMS code wait time (seconds)
SMS_CODE_WAIT_TIME = 120

# SMS polling interval (seconds)
SMS_POLL_INTERVAL = 5
