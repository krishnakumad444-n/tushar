import os
import json

# --- Auto-Configurator (JSON Support) ---
CONFIG_FILE = "config.json"
config_data = {}

if os.path.exists(CONFIG_FILE):
    try:
        with open(CONFIG_FILE, 'r') as f:
            config_data = json.load(f)
    except Exception as e:
        print(f"Error loading config.json: {e}")

def get_config(key, default):
    """Retrieves config from JSON first, then Environment, then Default."""
    return config_data.get(key, os.getenv(key, default))

# General App Configuration
SECRET_KEY = get_config("SECRET_KEY", "a_very_secret_and_long_random_string_for_production")
DB_FILE = get_config("DB_FILE", "Secure_enterprise.db")
JWT_SECRET_KEY = get_config("JWT_SECRET_KEY", "change-this-to-a-super-secret-jwt-key")

# ==============================================================================
#                          MONGODB CONNECTION - FIXED ✅
# ==============================================================================

# OPTION 1: LOCAL MONGODB (RECOMMENDED FOR DEVELOPMENT)
MONGO_URI = get_config("MONGO_URI", "mongodb://secure:secure@sn1.skynode.online:2016/?directConnection=true&serverSelectionTimeoutMS=2000&appName=mongosh+2.6.0")
MONGO_DB_NAME = get_config("MONGO_DB_NAME", "Secure_enterprise")


# Admin Credentials
ADMIN_USERNAME = get_config("ADMIN_USERNAME", "tushar!official")
ADMIN_PASSWORD = get_config("ADMIN_PASSWORD", "Aijune@123")

# OAuth 2.0 Credentials
GOOGLE_CLIENT_ID = get_config("GOOGLE_CLIENT_ID", "667838820149-rr4277895cami5b50id706uog98pk4l1.apps.googleusercontent.com")
GOOGLE_CLIENT_SECRET = get_config("GOOGLE_CLIENT_SECRET", "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")
DISCORD_CLIENT_ID = get_config("DISCORD_CLIENT_ID", "1474674229042745434")
DISCORD_CLIENT_SECRET = get_config("DISCORD_CLIENT_SECRET", "tVtzqgHZD1jl_kxcYtjhU5gSWyFY_1CK")

# Security Settings
HMAC_WINDOW_SECONDS = 30
NONCE_EXPIRY_SECONDS = 60
SESSION_EXPIRY_HOURS = 24
HWID_SIMILARITY_THRESHOLD = 0.8
MAX_LOGIN_ATTEMPTS = 10
IP_BAN_DURATION_SECONDS = 3600

# Rate Limiting
RATELIMIT_DEFAULT = "100 per minute"
RATELIMIT_SENSITIVE = "5 per minute"