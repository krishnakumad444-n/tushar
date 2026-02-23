import sqlite3
import hashlib
import secrets
import json
import hmac
import requests
import os
import logging
import time
import shutil
from functools import wraps
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, redirect, url_for
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_dance.contrib.google import make_google_blueprint, google
from flask_dance.contrib.discord import make_discord_blueprint, discord
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity, verify_jwt_in_request
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson.objectid import ObjectId
from bson.json_util import dumps, loads

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import config
import base64
import subprocess
import threading
import signal

# --- LOGGING CONFIGURATION ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize Flask App
app = Flask(__name__, static_folder='statics')
CORS(app)
# Apply ProxyFix to handle headers correctly behind proxies (Render, Heroku, Nginx, etc.)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = config.SECRET_KEY
app.config["JWT_SECRET_KEY"] = config.JWT_SECRET_KEY

# Store running bot processes
bot_processes = {}
bot_process_lock = threading.Lock()

# --- MongoDB Setup (SSL FIXED) ---
try:
    # MongoDB connection
    client = MongoClient(
        config.MONGO_URI,
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=30000,
        socketTimeoutMS=30000,
        tlsAllowInvalidCertificates=True,
        tls=False
    )
    
    # Test connection
    client.admin.command('ping')
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ MongoDB Connected Successfully")
    
    db = client[config.MONGO_DB_NAME]
    
    # Collections
    apps_collection = db.apps
    users_collection = db.users
    licenses_collection = db.licenses
    app_vars_collection = db.app_vars
    resellers_collection = db.resellers
    system_logs_collection = db.system_logs
    reseller_requests_collection = db.reseller_requests
    system_settings_collection = db.system_settings
    hwid_blacklist_collection = db.hwid_blacklist
    daily_stats_collection = db.daily_stats
    sessions_collection = db.sessions
    
    # ==============================================================================
    #                        DISCORD BOT COLLECTIONS
    # ==============================================================================
    
    discord_bots_collection = db.discord_bots
    discord_guilds_collection = db.discord_guilds
    discord_commands_log = db.discord_commands_log
    discord_user_links_collection = db.discord_user_links
    
    MONGO_AVAILABLE = True
    
except Exception as e:
    logger.error(f"MongoDB Connection Error: {e}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ MongoDB connection failed: {str(e)[:100]}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ CRITICAL: MongoDB is required for this application!")
    
    # Create mock collections for development - ORIGINAL 2.5K LINES CODE
    class MockCollection:
        def __init__(self, name):
            self.name = name
            self.data = []
            self._id_counter = 1
        
        def find_one(self, query=None):
            if not query:
                return self.data[0] if self.data else None
            for item in self.data:
                match = True
                for key, value in query.items():
                    if item.get(key) != value:
                        match = False
                        break
                if match:
                    return item
            return None
        
        def find(self, query=None):
            if not query:
                return self.data
            result = []
            for item in self.data:
                match = True
                for key, value in query.items():
                    if item.get(key) != value:
                        match = False
                        break
                if match:
                    result.append(item)
            return result
        
        def insert_one(self, document):
            document['_id'] = self._id_counter
            self._id_counter += 1
            self.data.append(document)
            return type('obj', (object,), {'inserted_id': document['_id']})()
        
        def update_one(self, query, update, upsert=False):
            item = self.find_one(query)
            if item:
                if '$set' in update:
                    for key, value in update['$set'].items():
                        item[key] = value
                if '$inc' in update:
                    for key, value in update['$inc'].items():
                        item[key] = item.get(key, 0) + value
                if '$setOnInsert' in update and upsert:
                    for key, value in update['$setOnInsert'].items():
                        if key not in item:
                            item[key] = value
                return type('obj', (object,), {'matched_count': 1, 'modified_count': 1})()
            elif upsert:
                new_doc = query.copy()
                if '$set' in update:
                    new_doc.update(update['$set'])
                if '$setOnInsert' in update:
                    new_doc.update(update['$setOnInsert'])
                self.insert_one(new_doc)
                return type('obj', (object,), {'matched_count': 0, 'modified_count': 0, 'upserted_id': new_doc.get('_id')})()
            return type('obj', (object,), {'matched_count': 0, 'modified_count': 0})()
        
        def delete_one(self, query):
            for i, item in enumerate(self.data):
                match = True
                for key, value in query.items():
                    if item.get(key) != value:
                        match = False
                        break
                if match:
                    del self.data[i]
                    return type('obj', (object,), {'deleted_count': 1})()
            return type('obj', (object,), {'deleted_count': 0})()
        
        def delete_many(self, query):
            count = 0
            new_data = []
            for item in self.data:
                match = True
                for key, value in query.items():
                    if item.get(key) != value:
                        match = False
                        break
                if not match:
                    new_data.append(item)
                else:
                    count += 1
            self.data = new_data
            return type('obj', (object,), {'deleted_count': count})()
        
        def count_documents(self, query=None):
            if not query:
                return len(self.data)
            count = 0
            for item in self.data:
                match = True
                for key, value in query.items():
                    if item.get(key) != value:
                        match = False
                        break
                if match:
                    count += 1
            return count
        
        def create_index(self, index, **kwargs):
            return True  # Mock index creation
    
    # Create mock collections
    apps_collection = MockCollection('apps')
    users_collection = MockCollection('users')
    licenses_collection = MockCollection('licenses')
    app_vars_collection = MockCollection('app_vars')
    resellers_collection = MockCollection('resellers')
    system_logs_collection = MockCollection('system_logs')
    reseller_requests_collection = MockCollection('reseller_requests')
    system_settings_collection = MockCollection('system_settings')
    hwid_blacklist_collection = MockCollection('hwid_blacklist')
    daily_stats_collection = MockCollection('daily_stats')
    sessions_collection = MockCollection('sessions')
    
    # Discord bot mock collections
    discord_bots_collection = MockCollection('discord_bots')
    discord_guilds_collection = MockCollection('discord_guilds')
    discord_commands_log = MockCollection('discord_commands_log')
    discord_user_links_collection = MockCollection('discord_user_links')
    
    MONGO_AVAILABLE = False

# --- OAuth 2.0 Setup ---
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

google_bp = make_google_blueprint(
    client_id=config.GOOGLE_CLIENT_ID,
    client_secret=config.GOOGLE_CLIENT_SECRET,
    scope=["profile", "email"],
    redirect_to="google_callback",
    login_url="/login",
    authorized_url="/authorized"
)
app.register_blueprint(google_bp, url_prefix="/auth/google")
print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Google Auth Registered at /auth/google")

# --- Discord OAuth Setup ---
discord_bp = make_discord_blueprint(
    client_id=config.DISCORD_CLIENT_ID,
    client_secret=config.DISCORD_CLIENT_SECRET,
    scope=["identify", "email"],
    redirect_to="discord_callback",
    login_url="/login",
    authorized_url="/authorized"
)
app.register_blueprint(discord_bp, url_prefix="/auth/discord")
print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Discord Auth Registered at /auth/discord")

# Debug: Print Discord Routes to confirm they exist
print("--- Registered Discord Routes ---")
for rule in app.url_map.iter_rules():
    if "discord" in str(rule):
        print(f"Found Route: {rule} -> {rule.endpoint}")

# Debug: Print Google Routes
print("--- Registered Google Routes ---")
for rule in app.url_map.iter_rules():
    if "google" in str(rule):
        print(f"Found Route: {rule} -> {rule.endpoint}")

limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri="memory://",
    default_limits=[config.RATELIMIT_DEFAULT]
)

jwt = JWTManager(app)

# Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ADMIN_PASSWORD = config.ADMIN_PASSWORD
ADMIN_USERNAME = config.ADMIN_USERNAME

# ==============================================================================
#                           DATABASE ENGINE (MongoDB)
# ==============================================================================

class Database:
    @staticmethod
    def connect():
        """For MongoDB, we return the database client"""
        return db

    @staticmethod
    def init():
        """Initializes MongoDB Collections with default settings"""
        
        # Initialize Global Settings
        defaults = [
            {'key': 'maintenance_mode', 'value': 'false'},
            {'key': 'kill_switch', 'value': 'false'},
            {'key': 'min_client_version', 'value': '1.0'}
        ]
        
        for default in defaults:
            if MONGO_AVAILABLE:
                system_settings_collection.update_one(
                    {'key': default['key']},
                    {'$setOnInsert': default},
                    upsert=True
                )
            else:
                if system_settings_collection.count_documents({'key': default['key']}) == 0:
                    system_settings_collection.insert_one(default)

        # --- Auto-add your C# application for testing ---
        if MONGO_AVAILABLE and apps_collection.count_documents({'name': "AIMKILL"}) == 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 Adding your test application 'AIMKILL' to the database...")
            api_key = "api_" + secrets.token_hex(16)
            default_settings = {'enforce_hwid': True}
            
            app_data = {
                'owner_id': "5o_ZSUx97ks",
                'secret': "2c4bb6e7321e020dc77ae33513efbbe2c7e630351d81452e07de2883344c7890",
                'name': "AIMKILL",
                'created_at': get_time(),
                'plan': 'free',
                'api_key': api_key,
                'settings': json.dumps(default_settings),
                'status': 'active',
                'username': None,
                'password': None,
                'webhook': None,
                'parent_owner_id': None,
                'loader_hash': None,
                'plan_expiry': None
            }
            
            apps_collection.insert_one(app_data)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ 'AIMKILL' app added successfully.")

        # --- Auto-add premium test account for login testing ---
        if MONGO_AVAILABLE and apps_collection.count_documents({'username': 'testpremium'}) == 0:
            hashed_pass = SecurityCore.hash_password('test123')
            owner_id = secrets.token_urlsafe(8)
            secret = secrets.token_hex(32)
            
            apps_collection.insert_one({
                'owner_id': owner_id,
                'secret': secret,
                'name': "TEST PREMIUM",
                'created_at': get_time(),
                'username': 'testpremium',
                'password': hashed_pass,
                'plan': 'premium',
                'plan_expiry': (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S'),
                'status': 'active',
                'api_key': "api_" + secrets.token_hex(16),
                'settings': json.dumps({'enforce_hwid': True}),
                'parent_owner_id': None,
                'loader_hash': None,
                'webhook': None
            })
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Test account created: testpremium / test123")

        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 Secure SYSTEM FULLY LOADED & ONLINE")
    
    @staticmethod
    def backup():
        """Creates a secure backup of the database"""
        try:
            backup_dir = os.path.join(BASE_DIR, 'backups')
            os.makedirs(backup_dir, exist_ok=True)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            
            # Export all collections to JSON files
            collections = [
                {'name': 'apps', 'col': apps_collection},
                {'name': 'users', 'col': users_collection},
                {'name': 'licenses', 'col': licenses_collection},
                {'name': 'app_vars', 'col': app_vars_collection},
                {'name': 'resellers', 'col': resellers_collection},
                {'name': 'system_logs', 'col': system_logs_collection},
                {'name': 'reseller_requests', 'col': reseller_requests_collection},
                {'name': 'system_settings', 'col': system_settings_collection},
                {'name': 'hwid_blacklist', 'col': hwid_blacklist_collection},
                {'name': 'daily_stats', 'col': daily_stats_collection},
                {'name': 'sessions', 'col': sessions_collection},
                {'name': 'discord_bots', 'col': discord_bots_collection},
                {'name': 'discord_guilds', 'col': discord_guilds_collection},
                {'name': 'discord_commands_log', 'col': discord_commands_log},
                {'name': 'discord_user_links', 'col': discord_user_links_collection}
            ]
            
            for collection_info in collections:
                data = list(collection_info['col'].find({}))
                if data:
                    # Remove _id for clean backup
                    for item in data:
                        if '_id' in item:
                            del item['_id']
                    
                    backup_file = os.path.join(backup_dir, f"{collection_info['name']}_backup_{timestamp}.json")
                    with open(backup_file, 'w') as f:
                        json.dump(data, f, default=str, indent=2)
            
            return True
        except Exception as e:
            logger.error(f"Backup failed: {e}")
            return False

# --- UTILITY FUNCTIONS ---
def api_response(success, message, data=None, status_code=200):
    """Standardized API Response"""
    response = {"success": success, "message": message}
    if data:
        response.update(data)
    return jsonify(response), status_code

def validate_request(data, required_fields):
    """Validates that required fields exist in the request data"""
    missing = [f for f in required_fields if f not in data or not str(data[f]).strip()]
    if missing:
        return f"Missing required fields: {', '.join(missing)}"
    return None

def admin_required():
    """Decorator to protect Admin routes with JWT"""
    def wrapper(fn):
        @wraps(fn)
        def decorator(*args, **kwargs):
            try:
                verify_jwt_in_request()
                claims = get_jwt_identity()
                if claims != 'admin':
                    return api_response(False, "Admin access required", status_code=403)
                return fn(*args, **kwargs)
            except Exception as e:
                return api_response(False, f"Auth Error: {str(e)}", status_code=401)
        return decorator
    return wrapper

def get_time():
    """Returns current server time string"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def log_system(type, message, ip):
    """Logs an event to the database"""
    try:
        system_logs_collection.insert_one({
            'type': type,
            'message': message,
            'ip': ip,
            'timestamp': get_time()
        })
    except Exception as e:
        logger.error(f"Failed to log system event: {e}")

def send_discord_webhook(url, title, description, color):
    """Sends a log to Discord Webhook"""
    if not url: return
    try:
        payload = {
            "embeds": [{"title": title, "description": description, "color": color, "timestamp": datetime.utcnow().isoformat()}]
        }
        requests.post(url, json=payload, timeout=2)
    except: pass

def verify_hmac(key, message, signature):
    """Verify HMAC-SHA256 signature"""
    computed = hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)

# --- AES ENCRYPTION HELPERS ---
class SecurityCore:
    @staticmethod
    def get_aes_key(secret):
        """Derives a 32-byte key from the app secret"""
        return hashlib.sha256(secret.encode()).digest()

    @staticmethod
    def encrypt_aes(data, secret):
        """Encrypts string data using AES-256-CBC"""
        try:
            key = SecurityCore.get_aes_key(secret)
            iv = os.urandom(16)
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
            encryptor = cipher.encryptor()
            padder = padding.PKCS7(128).padder()
            padded_data = padder.update(data.encode()) + padder.finalize()
            ciphertext = encryptor.update(padded_data) + encryptor.finalize()
            return base64.b64encode(iv + ciphertext).decode()
        except Exception as e:
            logger.error(f"Encryption Error: {e}")
            return None

    @staticmethod
    def decrypt_aes(data, secret):
        """Decrypts base64 encoded string data"""
        try:
            raw = base64.b64decode(data)
            iv = raw[:16]
            ciphertext = raw[16:]
            key = SecurityCore.get_aes_key(secret)
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
            decryptor = cipher.decryptor()
            padded_data = decryptor.update(ciphertext) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            return (unpadder.update(padded_data) + unpadder.finalize()).decode()
        except Exception as e:
            logger.error(f"Decryption Error: {e}")
            return None

    @staticmethod
    def generate_signature(secret, payload, timestamp, nonce):
        """HMAC-SHA256(Secret, Payload + Timestamp + Nonce)"""
        data = f"{payload}{timestamp}{nonce}"
        return hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def hash_password(password):
        """Hashes password using Argon2ID if available, else PBKDF2"""
        try:
            return generate_password_hash(password, method='argon2')
        except:
            return generate_password_hash(password)

# ==============================================================================
#                           HTML PAGE ROUTING
# ==============================================================================

@app.route('/')
def welcome():
    """Serves the Landing Page"""
    return send_from_directory('.', 'welcome.html')

@app.route('/assets/<path:filename>')
def serve_assets(filename):
    """Serves static assets"""
    return send_from_directory('assets', filename)

@app.route('/statics/icon.png')
def serve_statics_icon():
    return send_from_directory(os.path.join(BASE_DIR, 'statics'), 'logo.png')

@app.route('/statics/<path:filename>')
def serve_statics(filename):
    return send_from_directory(os.path.join(BASE_DIR, 'statics'), filename)

@app.route('/dashboard')
def dashboard():
    """Serves the User/Owner Dashboard"""
    return send_from_directory('.', 'index.html')

@app.route('/admin')
def admin_panel():
    """Serves the God Mode Panel"""
    return send_from_directory('.', 'admin.html')

@app.route('/reseller')
def reseller_panel():
    """Serves the Reseller Panel"""
    return send_from_directory('.', 'reseller.html')

@app.route('/premium')
def premium_dashboard():
    """Serves the Premium User Dashboard"""
    return send_from_directory('.', 'premium_dashboard.html')

# ==============================================================================
#                        GOD MODE API (SUPER ADMIN)
# ==============================================================================

@app.route('/api/admin/login', methods=['POST'])
def admin_login_api():
    """Authenticates the Super Admin"""
    try:
        data = request.get_json(silent=True) or request.form or {}
        input_pass = str(data.get('password') or '').strip()
        
        if input_pass == ADMIN_PASSWORD:
            access_token = create_access_token(identity="admin", expires_delta=timedelta(hours=12))
            log_system('admin', 'Admin Login Successful', request.remote_addr)
            return api_response(True, "Welcome Master", {"access_token": access_token})
        else:
            log_system('admin', 'Admin Login Failed', request.remote_addr)
            return api_response(False, "Invalid Credentials", status_code=401)
    except Exception as e:
        logger.error(f"Admin Login Error: {e}")
        return api_response(False, "Internal Server Error", status_code=500)

@app.route('/api/admin/fetch_all', methods=['POST'])
@admin_required()
def admin_fetch_all():
    """Fetches ALL Apps and Global Stats"""
    try:
        apps_data = []
        total_users_global = 0
        master_accounts_count = 0
        
        for app in apps_collection.find():
            user_count = users_collection.count_documents({'owner_id': app['owner_id']})
            license_count = licenses_collection.count_documents({'owner_id': app['owner_id']})
            
            app_dict = dict(app)
            app_dict['_id'] = str(app_dict['_id'])
            app_dict['user_count'] = user_count
            app_dict['license_count'] = license_count
            apps_data.append(app_dict)
            
            total_users_global += user_count
            if not app.get('parent_owner_id'): 
                master_accounts_count += 1

        stats = {
            'total_apps': master_accounts_count,
            'total_users': total_users_global,
            'db_size': 'Optimal'
        }
        
        return api_response(True, "Data fetched", {'stats': stats, 'apps': apps_data})
    
    except Exception as e:
        logger.error(f"Fetch All Error: {e}")
        return api_response(False, str(e), status_code=500)

@app.route('/api/admin/logs', methods=['POST'])
@admin_required()
def admin_logs():
    """Fetches System Logs"""
    try:
        logs = list(system_logs_collection.find().sort('_id', -1).limit(100))
        for log in logs:
            log['_id'] = str(log['_id'])
        return api_response(True, "Logs fetched", {'logs': logs})
    except Exception as e:
        return api_response(False, str(e), status_code=500)

@app.route('/api/admin/reseller_requests', methods=['POST'])
@admin_required()
def admin_reseller_requests():
    """Fetches Reseller Requests"""
    try:
        requests_data = list(reseller_requests_collection.find().sort('created_at', -1))
        for req in requests_data:
            req['_id'] = str(req['_id'])
        return jsonify({'success': True, 'requests': requests_data})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/admin/reseller_requests/approve', methods=['POST'])
@admin_required()
def admin_approve_reseller_request():
    """Approves a Reseller Request"""
    data = request.get_json(silent=True) or request.form or {}
    req_id = data.get('id')
    
    try:
        req = reseller_requests_collection.find_one({'_id': ObjectId(req_id)}) if ObjectId.is_valid(req_id) else None
        
        if not req:
            return jsonify({'success': False, 'message': 'Request not found'}), 404
            
        if req['status'] != 'pending':
            return jsonify({'success': False, 'message': 'Request already processed'}), 400
            
        reseller = resellers_collection.find_one({
            'owner_id': req['owner_id'],
            'username': req['reseller_username']
        })
        
        if reseller:
            new_balance = reseller['balance'] + req['amount']
            resellers_collection.update_one(
                {'_id': reseller['_id']},
                {'$set': {'balance': new_balance}}
            )
        else:
            user = users_collection.find_one({
                'owner_id': req['owner_id'],
                'username': req['reseller_username']
            })
            password = user.get('password') if user else '123456'
            
            resellers_collection.insert_one({
                'owner_id': req['owner_id'],
                'username': req['reseller_username'],
                'password': password,
                'balance': req['amount'],
                'created_at': get_time(),
                'banned': 0
            })
        
        reseller_requests_collection.update_one(
            {'_id': req['_id']},
            {'$set': {'status': 'approved'}}
        )
        
        return jsonify({'success': True, 'message': 'Request Approved'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/admin/reseller_requests/reject', methods=['POST'])
@admin_required()
def admin_reject_reseller_request():
    """Rejects a Reseller Request"""
    data = request.get_json(silent=True) or request.form or {}
    req_id = data.get('id')
    
    try:
        if ObjectId.is_valid(req_id):
            reseller_requests_collection.update_one(
                {'_id': ObjectId(req_id)},
                {'$set': {'status': 'rejected'}}
            )
        return jsonify({'success': True, 'message': 'Request Rejected'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/admin/resellers', methods=['POST'])
@admin_required()
def admin_list_resellers():
    try:
        resellers = list(resellers_collection.find())
        for reseller in resellers:
            reseller['_id'] = str(reseller['_id'])
            app = apps_collection.find_one({'owner_id': reseller['owner_id']})
            reseller['app_name'] = app['name'] if app else 'N/A'
        
        return jsonify({'success': True, 'resellers': resellers})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/admin/resellers/delete', methods=['POST'])
@admin_required()
def admin_delete_reseller():
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    username = data.get('username')
    
    if not owner_id or not username:
        return jsonify({'success': False, 'message': 'owner_id and username required'}), 400
    
    try:
        resellers_collection.delete_one({'owner_id': owner_id, 'username': username})
        return jsonify({'success': True, 'message': 'Reseller deleted'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/admin/change_user_pass', methods=['POST'])
@admin_required()
def admin_change_user_pass():
    """Admin can change ANY user's password"""
    data = request.get_json(silent=True) or request.form or {}
    username = data.get('username')
    new_pass = data.get('new_password')
    
    try:
        users_collection.update_many(
            {'username': username},
            {'$set': {'password': new_pass}}
        )
        return jsonify({'success': True, 'message': 'Password updated'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/admin/delete_app', methods=['POST'])
@admin_required()
def admin_delete_app():
    """Nukes an App and all its data"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    
    try:
        apps_collection.delete_one({'owner_id': owner_id})
        users_collection.delete_many({'owner_id': owner_id})
        licenses_collection.delete_many({'owner_id': owner_id})
        app_vars_collection.delete_many({'owner_id': owner_id})
        resellers_collection.delete_many({'owner_id': owner_id})
        reseller_requests_collection.delete_many({'owner_id': owner_id})
        hwid_blacklist_collection.delete_many({'owner_id': owner_id})
        daily_stats_collection.delete_many({'owner_id': owner_id})
        sessions_collection.delete_many({})  # Sessions are global
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/admin/hwid_actions', methods=['POST'])
@admin_required()
def admin_hwid_actions():
    """Ban, Unban, or Fetch HWIDs"""
    data = request.get_json(silent=True) or request.form or {}
    
    try:
        action = data.get('action')
        owner_id = data.get('owner_id')
        hwid = data.get('hwid')

        if action == 'ban':
            target = owner_id if owner_id else 'GLOBAL'
            hwid_blacklist_collection.update_one(
                {'owner_id': target, 'hwid': hwid},
                {'$setOnInsert': {'banned_at': get_time()}},
                upsert=True
            )
        elif action == 'unban':
            if owner_id:
                hwid_blacklist_collection.delete_one({'owner_id': owner_id, 'hwid': hwid})
            else:
                hwid_blacklist_collection.delete_many({'hwid': hwid})
        elif action == 'fetch':
            if owner_id:
                blacklist = list(hwid_blacklist_collection.find({'owner_id': owner_id}))
            else:
                blacklist = list(hwid_blacklist_collection.find().sort('banned_at', -1))
            
            for item in blacklist:
                item['_id'] = str(item['_id'])
            
            return jsonify({'success': True, 'blacklist': blacklist})
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/admin/update_plan', methods=['POST'])
@admin_required()
def admin_update_plan():
    """Updates App Plan (Free/Premium)"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    new_plan = data.get('plan')
    
    try:
        plan_expiry = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S') if new_plan == 'premium' else None
        
        apps_collection.update_one(
            {'owner_id': owner_id},
            {'$set': {'plan': new_plan, 'plan_expiry': plan_expiry}}
        )
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/admin/backup_db')
@admin_required()
def backup_db():
    """Allows admin to download the database backup"""
    try:
        if Database.backup():
            backup_dir = os.path.join(BASE_DIR, 'backups')
            files = os.listdir(backup_dir)
            if files:
                latest = max(files, key=lambda x: os.path.getctime(os.path.join(backup_dir, x)))
                return send_from_directory(backup_dir, latest, as_attachment=True)
        return "Backup failed or no backups found", 404
    except Exception as e:
        return f"Backup failed: {str(e)}", 500

@app.route('/api/admin/create_premium', methods=['POST'])
@admin_required()
def admin_create_premium():
    """Admin creates a new premium app"""
    data = request.get_json(silent=True) or request.form or {}
    name = data.get('name')
    if not name: 
        return jsonify({'success': False, 'message': 'Name required'}), 400

    import string
    chars = string.ascii_letters + string.digits
    gen_user = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    gen_pass = ''.join(secrets.choice(chars) for _ in range(6))
    owner_id = secrets.token_urlsafe(8)
    secret = secrets.token_hex(32)

    hashed_pass = SecurityCore.hash_password(gen_pass)

    apps_collection.insert_one({
        'owner_id': owner_id,
        'secret': secret,
        'name': name,
        'created_at': get_time(),
        'username': gen_user,
        'password': hashed_pass,
        'plan': 'premium',
        'plan_expiry': (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S'),
        'status': 'active',
        'api_key': "api_" + secrets.token_hex(16),
        'settings': json.dumps({'enforce_hwid': True}),
        'parent_owner_id': None,
        'loader_hash': None,
        'webhook': None
    })

    return jsonify({'success': True, 'username': gen_user, 'password': gen_pass, 'owner_id': owner_id})

@app.route('/api/admin/announcement', methods=['POST'])
@admin_required()
def admin_set_announcement():
    """Sets a global announcement message."""
    data = request.get_json(silent=True) or request.form or {}
    message = data.get('message', '')
    
    try:
        system_settings_collection.update_one(
            {'key': 'global_msg'},
            {'$set': {'value': message}},
            upsert=True
        )
        log_system('admin', f'Global announcement updated: "{message}"', request.remote_addr)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/admin/reset_app_pass', methods=['POST'])
@admin_required()
def admin_reset_app_pass():
    """Resets an App Owner's password"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    new_pass = data.get('new_password')
    
    try:
        hashed_pass = SecurityCore.hash_password(new_pass)
        apps_collection.update_one(
            {'owner_id': owner_id},
            {'$set': {'password': hashed_pass}}
        )
        return jsonify({'success': True, 'message': 'App Password updated'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/admin/nuke_all', methods=['POST'])
@admin_required()
def admin_nuke_all():
    """Deletes all user-generated data from the system."""
    try:
        log_system('admin', 'SYSTEM NUKE INITIATED', request.remote_addr)
        
        collections_to_nuke = [
            apps_collection, users_collection, licenses_collection, 
            app_vars_collection, resellers_collection, reseller_requests_collection, 
            hwid_blacklist_collection, daily_stats_collection, sessions_collection,
            discord_bots_collection, discord_guilds_collection, discord_commands_log,
            discord_user_links_collection
        ]
        
        for collection in collections_to_nuke:
            collection.delete_many({})
        
        log_system('admin', 'SYSTEM NUKE COMPLETED', request.remote_addr)
        return jsonify({'success': True, 'message': 'System data has been wiped.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/create-app', methods=['POST'])
def create_app():
    """Creates a new FREE Application for a user"""
    data = request.get_json(silent=True) or request.form or {}
    name = data.get('name')
    
    if not name: 
        return jsonify({'success': False, 'message': 'App Name Required'}), 400

    return create_app_logic(name, 'free')

def create_app_logic(name, plan, social_info={}):
    """Core logic to create an app with a specific plan."""
    secret = secrets.token_hex(32)
    api_key = "api_" + secrets.token_hex(16)
    default_settings = json.dumps({'enforce_hwid': True})
    
    import string
    chars = string.ascii_letters + string.digits
    gen_user = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    gen_pass = ''.join(secrets.choice(chars) for _ in range(6))
    
    hashed_pass = SecurityCore.hash_password(gen_pass)

    plan_expiry = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S') if plan == 'premium' else None

    try:
        owner_id = secrets.token_urlsafe(8)
        app_doc = {
            'owner_id': owner_id,
            'secret': secret,
            'name': name,
            'created_at': get_time(),
            'username': gen_user,
            'password': hashed_pass,
            'plan': plan,
            'plan_expiry': plan_expiry,
            'api_key': api_key,
            'settings': default_settings,
            'status': 'active',
            'parent_owner_id': None,
            'loader_hash': None,
            'webhook': None
        }
        # Add social info if present
        if social_info.get('google_id'):
            app_doc['google_id'] = social_info['google_id']
        if social_info.get('discord_id'):
            app_doc['discord_id'] = social_info['discord_id']
        if social_info.get('email'):
            app_doc['email'] = social_info['email']

        apps_collection.insert_one(app_doc)
        
        print(f"[INFO] App Created: {name} (Plan: {plan})")
        return jsonify({'success': True, 'owner_id': owner_id, 'secret': secret, 'name': name,
                        'username': gen_user, 'password': gen_pass})
    except Exception as e:
        return jsonify({'success': False, 'message': 'Database Error: ' + str(e)}), 500

@app.route('/api/create-free-app', methods=['POST'])
def create_free_app():
    """Public endpoint to create a FREE plan app, now links social account if available"""
    data = request.get_json(silent=True) or request.form or {}
    name = data.get('name')
    if not name:
        return jsonify({'success': False, 'message': 'Name required'}), 400
    
    social_info = {}
    if google.authorized:
        resp = google.get("/oauth2/v2/userinfo")
        if resp.ok:
            user_info = resp.json()
            social_info['google_id'] = user_info.get('id')
            social_info['email'] = user_info.get('email')
    elif discord.authorized:
        resp = discord.get("/api/users/@me")
        if resp.ok:
            user_info = resp.json()
            social_info['discord_id'] = str(user_info.get('id'))
            social_info['email'] = user_info.get('email')

    return create_app_logic(name, 'free', social_info)

# ==============================================================================
#                        OWNER LOGIN - FIXED ✅
# ==============================================================================

@app.route('/api/owner/login', methods=['POST'])
def owner_login():
    """Logs in an App Owner - FULLY FIXED"""
    try:
        data = request.get_json(silent=True) or request.form or {}
        
        username = None
        password = None
        owner_id = None
        secret = None
        
        # Get credentials
        if data.get('username') and data.get('password'):
            username = str(data.get('username')).strip()
            password = str(data.get('password')).strip()
        elif data.get('owner_id') and data.get('secret'):
            owner_id = str(data.get('owner_id')).strip()
            secret = str(data.get('secret')).strip()
        else:
            return jsonify({'success': False, 'message': 'Missing credentials'}), 400
        
        app_data = None
        
        # Find by username/password
        if username and password:
            app_data = apps_collection.find_one({'username': username})
            
            if app_data:
                stored_pass = app_data.get('password', '')
                try:
                    if check_password_hash(stored_pass, password):
                        pass  # Valid
                    else:
                        app_data = None
                except:
                    if stored_pass != password:
                        app_data = None
                    else:
                        # Upgrade to hashed password
                        hashed = SecurityCore.hash_password(password)
                        apps_collection.update_one(
                            {'_id': app_data['_id']},
                            {'$set': {'password': hashed}}
                        )
        
        # Find by owner_id/secret
        elif owner_id and secret:
            app_data = apps_collection.find_one({'owner_id': owner_id, 'secret': secret})
        
        if app_data:
            log_system('login', f"Owner Login: {app_data.get('name', 'Unknown')}", request.remote_addr)
            
            app_dict = dict(app_data)
            if '_id' in app_dict:
                app_dict['_id'] = str(app_dict['_id'])
            
            plan = app_data.get('plan', 'free')
            dashboard_url = '/premium' if plan == 'premium' else '/dashboard'

            return jsonify({
                'success': True,
                'app': app_dict,
                'dashboard_url': dashboard_url
            })
        else:
            log_system('login', f"Owner Login Failed", request.remote_addr)
            return jsonify({'success': False, 'message': 'Invalid Credentials'}), 401
            
    except Exception as e:
        logger.error(f"Login Error: {e}")
        return jsonify({'success': False, 'message': f'Login Error: {str(e)}'}), 500

@app.route('/api/owner/social-login-data')
def social_login_data():
    """
    Checks for a linked application based on the current social auth session (Google/Discord)
    and returns the app data needed for the frontend to log in.
    """
    social_user = None
    app_data = None
    
    try:
        if google.authorized:
            resp = google.get("/oauth2/v2/userinfo")
            if resp.ok:
                social_user = resp.json()
                app_data = apps_collection.find_one({'google_id': social_user.get('id')})
        elif discord.authorized:
            resp = discord.get("/api/users/@me")
            if resp.ok:
                social_user = resp.json()
                app_data = apps_collection.find_one({'discord_id': str(social_user.get('id'))})

        if app_data:
            app_dict = dict(app_data)
            if '_id' in app_dict: app_dict['_id'] = str(app_dict['_id'])
            plan = app_data.get('plan', 'free')
            dashboard_url = '/premium' if plan == 'premium' else '/dashboard'
            return jsonify({'success': True, 'app': app_dict, 'dashboard_url': dashboard_url})
        else:
            return jsonify({'success': False, 'message': 'No application is linked to this social account.'}), 404
    except Exception as e:
        logger.error(f"Social Login Data Error: {e}")
        return jsonify({'success': False, 'message': f'An error occurred: {str(e)}'}), 500

@app.route('/api/owner/data', methods=['POST'])
def owner_data():
    """Fetches Dashboard Data for an Owner"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    
    if not owner_id:
        return jsonify({'success': False, 'message': 'Owner ID required'}), 400
    
    try:
        # Fix for Mock DB (No sort/limit support on lists)
        if MONGO_AVAILABLE:
            users = list(users_collection.find({'owner_id': owner_id}).sort('_id', -1))
            licenses = list(licenses_collection.find({'owner_id': owner_id}).sort('_id', -1).limit(200))
            requests = list(reseller_requests_collection.find({'owner_id': owner_id}).sort('_id', -1))
            analytics_rows = list(daily_stats_collection.find({'owner_id': owner_id}).sort('date', -1).limit(7))
        else:
            users = list(users_collection.find({'owner_id': owner_id}))
            licenses = list(licenses_collection.find({'owner_id': owner_id}))[:200]
            requests = list(reseller_requests_collection.find({'owner_id': owner_id}))
            analytics_rows = list(daily_stats_collection.find({'owner_id': owner_id}))[:7]

        app_info = apps_collection.find_one({'owner_id': owner_id})
        vars_list = list(app_vars_collection.find({'owner_id': owner_id}))
        resellers = list(resellers_collection.find({'owner_id': owner_id}))
        global_msg = system_settings_collection.find_one({'key': 'global_msg'})
        blacklist = list(hwid_blacklist_collection.find({'owner_id': owner_id}))
        
        if not app_info:
            return jsonify({'success': False, 'message': 'App not found'}), 404

        # Convert ObjectIds to strings
        for user in users:
            if '_id' in user: user['_id'] = str(user['_id'])
        for lic in licenses:
            if '_id' in lic: lic['_id'] = str(lic['_id'])
        for var in vars_list:
            if '_id' in var: var['_id'] = str(var['_id'])
        for reseller in resellers:
            if '_id' in reseller: reseller['_id'] = str(reseller['_id'])
        for req in requests:
            if '_id' in req: req['_id'] = str(req['_id'])
        for item in blacklist:
            if '_id' in item: item['_id'] = str(item['_id'])

        root_owner_id = owner_id
        if app_info and app_info.get('parent_owner_id'):
            root_owner_id = app_info['parent_owner_id']

        managed_apps = []
        if app_info and (app_info.get('plan') == 'premium' or app_info.get('parent_owner_id')):
            if MONGO_AVAILABLE:
                managed_apps_cursor = apps_collection.find({'parent_owner_id': root_owner_id}).sort('created_at', -1)
            else:
                managed_apps_cursor = apps_collection.find({'parent_owner_id': root_owner_id})
            
            for app in managed_apps_cursor:
                user_count = users_collection.count_documents({'owner_id': app['owner_id']})
                license_count = licenses_collection.count_documents({'owner_id': app['owner_id']})
                
                app_dict = dict(app)
                app_dict['_id'] = str(app_dict['_id'])
                app_dict['user_count'] = user_count
                app_dict['license_count'] = license_count
                managed_apps.append(app_dict)

        analytics = []
        for row in reversed(analytics_rows):
            analytics.append({
                'date': row['date'],
                'success': row.get('success_logins', 0),
                'failed': row.get('failed_logins', 0)
            })

        stats = {
            'users': len(users),
            'keys': len(licenses),
            'active': len([l for l in licenses if l.get('status') == 1]),
            'banned': len([u for u in users if u.get('banned') == 1]),
            'resellers': len(resellers),
            'plan': app_info.get('plan', 'free'),
            'app_status': app_info.get('status', 'active'),
            'plan_expiry': app_info.get('plan_expiry'),
            'api_key': app_info.get('api_key') if app_info.get('plan') == 'premium' else None,
            'settings': json.loads(app_info.get('settings', '{}'))
        }
        
        response_data = {
            'success': True,
            'stats': stats,
            'users': users,
            'licenses': licenses,
            'vars': vars_list,
            'resellers': resellers,
            'requests': requests,
            'blacklist': blacklist,
            'managed_apps': managed_apps,
            'announcement': global_msg.get('value') if global_msg else None,
            'root_owner_id': root_owner_id,
            'analytics': analytics
        }
        return jsonify(response_data)
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/owner/action', methods=['POST'])
def owner_action():
    """Handles all management actions (Ban, Gen Key, etc)"""
    data = request.get_json(silent=True) or request.form or {}
    action = data.get('action')
    owner_id = data.get('owner_id')
    secret = data.get('secret')
    
    if not action:
        return jsonify({'success': False, 'message': 'Action required'}), 400
    
    try:
        api_key = data.get('api_key')
        app_row = None
        if api_key:
            app_row = apps_collection.find_one({'api_key': api_key})
            if app_row:
                owner_id = app_row['owner_id']
            else:
                return jsonify({'success': False, 'message': 'Invalid API Key.'}), 401
        else:
            app_row = apps_collection.find_one({'owner_id': owner_id, 'secret': secret})

        if not app_row:
            return jsonify({'success': False, 'message': 'Authentication failed.'}), 401
        
        webhook_url = app_row.get('webhook')
        
        requesting_app_info = apps_collection.find_one({'owner_id': owner_id}, {'parent_owner_id': 1})
        root_owner_id = owner_id
        if requesting_app_info and requesting_app_info.get('parent_owner_id'):
            root_owner_id = requesting_app_info['parent_owner_id']

        app_info = apps_collection.find_one({'owner_id': root_owner_id}, {'plan': 1})
        if app_info and app_info.get('plan') == 'free':
            premium_actions = ['create_reseller', 'delete_reseller', 'add_balance', 'approve_credit', 
                              'set_var', 'toggle_maintenance', 'set_webhook', 'create_sub_app', 'regenerate_api_key',
                              'register_bot', 'bot_status', 'bot_update_settings', 'bot_stop']
            if action in premium_actions:
                return jsonify({'success': False, 'message': 'You have a Free Plan. Upgrade to Premium to unlock this feature.'}), 403

        if action == 'generate_keys':
            amount = int(data.get('amount'))
            
            app_info = apps_collection.find_one({'owner_id': owner_id}, {'plan': 1})
            if app_info and app_info.get('plan') == 'free':
                curr_count = licenses_collection.count_documents({'owner_id': owner_id})
                if curr_count + amount > 40:
                    return jsonify({'success': False, 'message': 'Free Plan Limit Reached (40 Keys). Upgrade to Premium.'}), 403

            days = int(data.get('days'))
            level = data.get('level', 'Premium')
            note = data.get('note')
            keys = []
            
            prefix = "Secure"
            if app_info and app_info.get('plan') == 'premium':
                if app_row.get('username'):
                    prefix = app_row['username'].upper()
                else:
                    prefix = "PREMIUM"

            new_licenses = []
            for _ in range(amount):
                k = f"{prefix}-{secrets.token_hex(4).upper()}-{secrets.token_hex(2).upper()}"
                
                license_data = {
                    'owner_id': owner_id,
                    'key': k,
                    'days': days,
                    'level': level,
                    'created_at': get_time(),
                    'note': note,
                    'status': 0,
                    'reset_count': 0,
                    'is_paused': 0,
                    'gen_by': None,
                    'hwid': None,
                    'expiry_date': None,
                    'used_by': None
                }
                
                licenses_collection.insert_one(license_data)
                keys.append(k)
                new_licenses.append(license_data)
            
            send_discord_webhook(webhook_url, "🔑 Keys Generated", f"**{amount}** new key(s) with **{days}** days duration were generated.", 3447003)
            
            for lic in new_licenses:
                lic['_id'] = str(lic['_id'])
            
            return jsonify({'success': True, 'message': f'{amount} keys generated.', 'licenses': new_licenses})
        
        elif action == 'create_user':
            username = str(data.get('username') or '').strip()
            password = str(data.get('password') or '').strip()
            key = str(data.get('key') or '').strip()
            hwid = str(data.get('hwid') or '').strip()
            ip = str(data.get('ip') or '').strip()
            days_str = data.get('days')
            
            if not username or not password:
                return jsonify({'success': False, 'message': 'Username and Password are required.'}), 400

            if users_collection.find_one({'username': username, 'owner_id': owner_id}):
                return jsonify({'success': False, 'message': 'Username already exists.'}), 400

            if key:
                lic = licenses_collection.find_one({'key': key, 'owner_id': owner_id})
                if not lic or lic.get('status') != 0:
                    return jsonify({'success': False, 'message': 'Provided license key is invalid or already used.'}), 400
                
                expiry = (datetime.now() + timedelta(days=lic['days'])).strftime('%Y-%m-%d %H:%M:%S')
                level = lic.get('level', 'Standard')
                
                licenses_collection.update_one(
                    {'_id': lic['_id']},
                    {'$set': {'status': 1, 'used_by': username}}
                )
            
            else:
                if not days_str:
                    return jsonify({'success': False, 'message': 'Duration (days) is required when no license key is provided.'}), 400
                
                try:
                    days = int(days_str)
                except (ValueError, TypeError):
                    return jsonify({'success': False, 'message': 'Invalid duration provided.'}), 400

                if app_info and app_info.get('plan') == 'premium':
                    safe_username = "".join(c for c in username if c.isalnum()).upper()
                    if not safe_username: safe_username = "USER"
                    key = f"{safe_username}-{secrets.token_hex(4).upper()}-{secrets.token_hex(2).upper()}"
                else:
                    key = "Secure-USER-" + secrets.token_hex(4).upper()

                level = data.get('level', 'Standard')
                expiry = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
                
                licenses_collection.insert_one({
                    'owner_id': owner_id,
                    'key': key,
                    'days': days,
                    'level': level,
                    'status': 1,
                    'used_by': username,
                    'created_at': get_time(),
                    'reset_count': 0,
                    'is_paused': 0,
                    'hwid': None,
                    'expiry_date': expiry,
                    'note': None
                })

            users_collection.insert_one({
                'owner_id': owner_id,
                'username': username,
                'password': password,
                'license': key,
                'hwid': hwid,
                'ip': ip,
                'expiry': expiry,
                'level': level,
                'created_at': get_time(),
                'last_login': None,
                'banned': 0,
                'ban_reason': None,
                'session_id': None,
                'total_launches': 0,
                'extra_data': None
            })
            
            return jsonify({'success': True, 'message': 'User created successfully'})

        elif action == 'ban_user':
            username = data.get('username')
            reason = data.get('reason', 'Violation')
            
            users_collection.update_one(
                {'username': username, 'owner_id': owner_id},
                {'$set': {'banned': 1, 'ban_reason': reason}}
            )
            
            user_row = users_collection.find_one({'username': username, 'owner_id': owner_id})
            send_discord_webhook(webhook_url, "🚫 User Banned", f"User `{username}` has been banned. Reason: {reason}", 15158332)
            
            if user_row:
                if user_row.get('license'):
                    licenses_collection.update_one(
                        {'key': user_row['license'], 'owner_id': owner_id},
                        {'$set': {'status': 2}}
                    )
                    
                    lic_row = licenses_collection.find_one({'owner_id': owner_id, 'key': user_row['license']})
                    if lic_row and lic_row.get('gen_by'):
                        resellers_collection.update_one(
                            {'owner_id': owner_id, 'username': lic_row['gen_by']},
                            {'$set': {'banned': 1}}
                        )
                
                if user_row.get('hwid'):
                    hwid_blacklist_collection.update_one(
                        {'owner_id': owner_id, 'hwid': user_row['hwid']},
                        {'$setOnInsert': {'banned_at': get_time()}},
                        upsert=True
                    )
            
            return jsonify({'success': True})

        elif action == 'ban_hwid':
            hwid = data.get('hwid')
            hwid_blacklist_collection.update_one(
                {'owner_id': owner_id, 'hwid': hwid},
                {'$setOnInsert': {'banned_at': get_time()}},
                upsert=True
            )
            return jsonify({'success': True})

        elif action == 'unban_hwid':
            hwid = data.get('hwid')
            hwid_blacklist_collection.delete_one({'owner_id': owner_id, 'hwid': hwid})
            return jsonify({'success': True})
            
        elif action == 'unban_user':
            username = data.get('username')
            
            users_collection.update_one(
                {'username': username, 'owner_id': owner_id},
                {'$set': {'banned': 0}}
            )
            
            send_discord_webhook(webhook_url, "✅ User Unbanned", f"User `{username}` has been unbanned.", 3066993)
            
            user_row = users_collection.find_one({'username': username, 'owner_id': owner_id})
            if user_row:
                if user_row.get('license'):
                    licenses_collection.update_one(
                        {'key': user_row['license'], 'owner_id': owner_id},
                        {'$set': {'status': 1}}
                    )
                
                if user_row.get('hwid'):
                    hwid_blacklist_collection.delete_one({'owner_id': owner_id, 'hwid': user_row['hwid']})
            
            return jsonify({'success': True})
            
        elif action == 'reset_hwid':
            username = data.get('username')
            users_collection.update_one(
                {'username': username, 'owner_id': owner_id},
                {'$set': {'hwid': None}}
            )
            return jsonify({'success': True})
            
        elif action == 'delete_user':
            username = data.get('username')
            user = users_collection.find_one({'username': username, 'owner_id': owner_id})
            
            if user:
                if user.get('license'):
                    licenses_collection.update_one(
                        {'key': user['license'], 'owner_id': owner_id},
                        {'$set': {'status': 0, 'used_by': None, 'hwid': None, 'expiry_date': None}}
                    )
                users_collection.delete_one({'username': username, 'owner_id': owner_id})
            return jsonify({'success': True})

        elif action == 'delete_license':
            key = data.get('key')
            
            licenses_collection.delete_one({'key': key, 'owner_id': owner_id})
            users_collection.delete_many({'license': key, 'owner_id': owner_id})
            
            remaining_licenses = list(licenses_collection.find({'owner_id': owner_id}).sort('_id', -1))
            for lic in remaining_licenses:
                lic['_id'] = str(lic['_id'])
            
            return jsonify({'success': True, 'message': 'License deleted.', 'licenses': remaining_licenses})

        elif action == 'set_var':
            name = data.get('var_name')
            val = data.get('var_value')
            
            app_vars_collection.update_one(
                {'owner_id': owner_id, 'var_name': name},
                {'$set': {'var_value': val}},
                upsert=True
            )
            return jsonify({'success': True})

        elif action == 'create_reseller':
            r_user = (data.get('r_username') or '').strip()
            r_pass = (data.get('r_password') or '').strip()
            try:
                r_bal = int(data.get('r_balance', 0))
            except:
                r_bal = 0
            
            if not r_user or not r_pass:
                return jsonify({'success': False, 'message': 'Username and Password required'}), 400
            
            final_username = r_user
            suffix = 2
            while resellers_collection.find_one({'owner_id': owner_id, 'username': final_username}):
                final_username = f"{r_user}-{suffix}"
                suffix += 1
            
            resellers_collection.insert_one({
                'owner_id': owner_id,
                'username': final_username,
                'password': r_pass,
                'balance': r_bal,
                'created_at': get_time(),
                'banned': 0
            })
            
            resellers = list(resellers_collection.find({'owner_id': owner_id}))
            for reseller in resellers:
                reseller['_id'] = str(reseller['_id'])
            
            return jsonify({'success': True, 'resellers': resellers, 'assigned_username': final_username})

        elif action == 'delete_reseller':
            r_user = data.get('r_username')
            
            resellers_collection.delete_one({'username': r_user, 'owner_id': owner_id})
            
            resellers = list(resellers_collection.find({'owner_id': owner_id}))
            for reseller in resellers:
                reseller['_id'] = str(reseller['_id'])
            
            return jsonify({'success': True, 'resellers': resellers})

        elif action == 'add_balance':
            r_user = data.get('r_username')
            amount = int(data.get('amount'))
            
            resellers_collection.update_one(
                {'username': r_user, 'owner_id': owner_id},
                {'$inc': {'balance': amount}}
            )
            return jsonify({'success': True})

        elif action == 'approve_credit':
            req_id = data.get('req_id')
            
            req = reseller_requests_collection.find_one({'_id': ObjectId(req_id), 'owner_id': owner_id})
            if req and req.get('status') == 'pending':
                resellers_collection.update_one(
                    {'username': req['reseller_username'], 'owner_id': owner_id},
                    {'$inc': {'balance': req['amount']}}
                )
                
                reseller_requests_collection.update_one(
                    {'_id': req['_id']},
                    {'$set': {'status': 'approved'}}
                )
            return jsonify({'success': True})

        elif action == 'toggle_maintenance':
            app = apps_collection.find_one({'owner_id': owner_id})
            if app:
                curr = app.get('status', 'active')
                new_status = 'maintenance' if curr == 'active' else 'active'
                
                apps_collection.update_one(
                    {'owner_id': owner_id},
                    {'$set': {'status': new_status}}
                )
                
                return jsonify({'success': True, 'new_status': new_status})
            
        elif action == 'set_webhook':
            url = data.get('webhook_url')
            apps_collection.update_one(
                {'owner_id': owner_id},
                {'$set': {'webhook': url}}
            )
            return jsonify({'success': True})
        
        elif action == 'create_sub_app':
            new_app_name = data.get('new_app_name')
            if not new_app_name:
                return jsonify({'success': False, 'message': 'New App Name is required'}), 400
            
            new_owner_id = secrets.token_urlsafe(8)
            new_secret = secrets.token_hex(32)
            
            apps_collection.insert_one({
                'owner_id': new_owner_id,
                'secret': new_secret,
                'name': new_app_name,
                'status': 'active',
                'created_at': get_time(),
                'plan': 'premium',
                'parent_owner_id': root_owner_id,
                'username': None,
                'password': None,
                'api_key': "api_" + secrets.token_hex(16),
                'settings': json.dumps({'enforce_hwid': True}),
                'plan_expiry': (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S'),
                'loader_hash': None,
                'webhook': None
            })
            
            return jsonify({
                'success': True, 
                'message': 'Premium app created successfully!',
                'new_app': {'owner_id': new_owner_id, 'secret': new_secret, 'name': new_app_name}
            })

        elif action == 'delete_sub_app':
            app_to_delete_id = data.get('app_to_delete')
            if not app_to_delete_id:
                return jsonify({'success': False, 'message': 'App ID to delete is required'}), 400

            app_to_delete_info = apps_collection.find_one({'owner_id': app_to_delete_id}, {'parent_owner_id': 1})
            if not app_to_delete_info or app_to_delete_info.get('parent_owner_id') != root_owner_id:
                return jsonify({'success': False, 'message': 'Permission denied. You can only delete your own sub-applications.'}), 403

            apps_collection.delete_one({'owner_id': app_to_delete_id})
            users_collection.delete_many({'owner_id': app_to_delete_id})
            licenses_collection.delete_many({'owner_id': app_to_delete_id})
            app_vars_collection.delete_many({'owner_id': app_to_delete_id})
            hwid_blacklist_collection.delete_many({'owner_id': app_to_delete_id})
            daily_stats_collection.delete_many({'owner_id': app_to_delete_id})
            
            return jsonify({'success': True, 'message': 'Application deleted successfully.'})

        elif action == 'reset_loader_hash':
            apps_collection.update_one(
                {'owner_id': owner_id},
                {'$set': {'loader_hash': None}}
            )
            return jsonify({'success': True, 'message': 'Loader hash has been reset.'})
        
        elif action == 'update_settings':
            settings_data = data.get('settings')
            if settings_data:
                try:
                    json.loads(settings_data)
                    apps_collection.update_one(
                        {'owner_id': owner_id},
                        {'$set': {'settings': settings_data}}
                    )
                    return jsonify({'success': True, 'message': 'Settings updated.'})
                except json.JSONDecodeError:
                    return jsonify({'success': False, 'message': 'Invalid settings format.'}), 400
            return jsonify({'success': False, 'message': 'No settings data provided.'}), 400

        elif action == 'regenerate_api_key':
            new_api_key = "api_" + secrets.token_hex(16)
            apps_collection.update_one(
                {'owner_id': owner_id},
                {'$set': {'api_key': new_api_key}}
            )
            return jsonify({'success': True, 'message': 'API Key regenerated.', 'api_key': new_api_key})

        elif action == 'reset_user_password':
            username = data.get('username')
            new_password = data.get('new_password')
            
            if not username or not new_password:
                return jsonify({'success': False, 'message': 'Username and New Password required'}), 400
            
            users_collection.update_one(
                {'username': username, 'owner_id': owner_id},
                {'$set': {'password': new_password}}
            )
            return jsonify({'success': True, 'message': 'Password updated successfully'})
        
        # ==============================================================================
        #                        DISCORD BOT ACTIONS - FIXED NO REQUEST CONTEXT ERROR
        # ==============================================================================
        
        elif action == 'register_bot':
            bot_token = data.get('bot_token')
            bot_client_id = data.get('bot_client_id')
            bot_owner_id = data.get('bot_owner_id')

            encrypted_token = SecurityCore.encrypt_aes(bot_token, app_row['secret'])

            # Stop existing bot if running
            with bot_process_lock:
                if owner_id in bot_processes:
                    try:
                        process = bot_processes[owner_id]
                        if process.poll() is None:
                            process.terminate()
                            time.sleep(2)
                            if process.poll() is None:
                                process.kill()
                        del bot_processes[owner_id]
                    except Exception as e:
                        print(f"Error stopping bot: {e}")

            # Save to database
            discord_bots_collection.update_one(
                {'owner_id': owner_id},
                {'$set': {
                    'bot_token': encrypted_token,
                    'bot_client_id': bot_client_id,
                    'bot_owner_id': bot_owner_id,
                    'status': 'starting',
                    'updated_at': get_time()
                }},
                upsert=True
            )
            
            # Capture current URL dynamically from the request (works on localhost and hosting)
            current_base_url = request.host_url.rstrip('/')

            # Start bot process in background thread - FIXED: NO REQUEST USAGE
            def start_bot():
                try:
                    # Decrypt token for bot process
                    decrypted_token = SecurityCore.decrypt_aes(encrypted_token, app_row['secret'])
                    
                    # Get the absolute path to discord_bot.py
                    bot_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'discord_bot.py')
                    
                    # Create log file for bot output
                    log_file = open(f'bot_{owner_id}.log', 'w')
                    
                    import sys
                    
                    print(f"Starting bot with URL: {current_base_url}")
                    
                    # Start bot process
                    process = subprocess.Popen([
                        sys.executable, bot_script,
                        owner_id, 
                        decrypted_token,
                        current_base_url,  # Using dynamic URL
                        config.MONGO_URI
                    ], stdout=log_file, stderr=subprocess.STDOUT, 
                       stdin=subprocess.DEVNULL,
                       start_new_session=True)
                    
                    with bot_process_lock:
                        bot_processes[owner_id] = process
                    
                    print(f"Started bot process for {owner_id} with PID {process.pid}")
                    
                    # Wait a bit and check if process is still running
                    time.sleep(5)
                    
                    if process.poll() is None:
                        discord_bots_collection.update_one(
                            {'owner_id': owner_id},
                            {'$set': {'status': 'online'}}
                        )
                        print(f"Bot for {owner_id} is online")
                    else:
                        # Process died, check exit code
                        exit_code = process.poll()
                        print(f"Bot for {owner_id} failed with exit code {exit_code}")
                        discord_bots_collection.update_one(
                            {'owner_id': owner_id},
                            {'$set': {'status': f'failed_{exit_code}'}}
                        )
                        
                except Exception as e:
                    print(f"Error starting bot: {str(e)}")
                    discord_bots_collection.update_one(
                        {'owner_id': owner_id},
                        {'$set': {'status': 'failed'}}
                    )

            # Start bot in background thread
            thread = threading.Thread(target=start_bot)
            thread.daemon = True
            thread.start()

            return jsonify({'success': True, 'message': 'Bot registered and starting automatically. It will be online in a few seconds.'})

        elif action == 'bot_status':
            bot_data = discord_bots_collection.find_one({'owner_id': owner_id})
            if not bot_data:
                return jsonify({'success': False, 'message': 'No bot found'})

            guilds = list(discord_guilds_collection.find({'owner_id': owner_id}))
            commands = list(discord_commands_log.find(
                {'owner_id': owner_id}
            ).sort('_id', -1).limit(20))

            for c in commands:
                if '_id' in c:
                    c['_id'] = str(c['_id'])

            # Check if process is actually running
            is_running = False
            with bot_process_lock:
                if owner_id in bot_processes:
                    process = bot_processes[owner_id]
                    if process and process.poll() is None:
                        is_running = True
            
            current_status = 'online' if is_running else bot_data.get('status', 'offline')

            return jsonify({
                'success': True,
                'bot': {
                    'status': current_status,
                    'total_servers': bot_data.get('total_servers', 0),
                    'settings': bot_data.get('settings', {})
                },
                'guilds': guilds,
                'recent_commands': commands
            })

        elif action == 'bot_update_settings':
            settings = data.get('settings', {})
            discord_bots_collection.update_one(
                {'owner_id': owner_id},
                {'$set': {'settings': settings}}
            )
            return jsonify({'success': True, 'message': 'Settings updated'})

        elif action == 'bot_stop':
            with bot_process_lock:
                if owner_id in bot_processes:
                    try:
                        process = bot_processes[owner_id]
                        if process.poll() is None:
                            process.terminate()
                            time.sleep(2)
                            if process.poll() is None:
                                process.kill()
                        del bot_processes[owner_id]
                    except Exception as e:
                        print(f"Error stopping bot: {e}")

            discord_bots_collection.update_one(
                {'owner_id': owner_id},
                {'$set': {'status': 'stopped'}}
            )
            return jsonify({'success': True, 'message': 'Bot stopped'})
            
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

# ==============================================================================
#                        DISCORD BOT API ENDPOINTS
# ==============================================================================

@app.route('/api/bot/register', methods=['POST'])
def register_discord_bot():
    """Premium users register their Discord bot"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    secret = data.get('secret')
    bot_token = data.get('bot_token')
    bot_client_id = data.get('bot_client_id')
    bot_owner_id = data.get('bot_owner_id')

    app_data = apps_collection.find_one({'owner_id': owner_id, 'secret': secret})
    if not app_data or app_data.get('plan') != 'premium':
        return jsonify({'success': False, 'message': 'Premium access required'}), 403

    encrypted_token = SecurityCore.encrypt_aes(bot_token, app_data['secret'])

    existing = discord_bots_collection.find_one({'owner_id': owner_id})
    if existing:
        discord_bots_collection.update_one(
            {'owner_id': owner_id},
            {'$set': {
                'bot_token': encrypted_token,
                'bot_client_id': bot_client_id,
                'bot_owner_id': bot_owner_id,
                'status': 'pending',
                'updated_at': get_time()
            }}
        )
    else:
        discord_bots_collection.insert_one({
            'owner_id': owner_id,
            'bot_token': encrypted_token,
            'bot_client_id': bot_client_id,
            'bot_owner_id': bot_owner_id,
            'status': 'pending',
            'created_at': get_time(),
            'total_servers': 0,
            'settings': {
                'auto_create_users': True,
                'log_channel': None,
                'admin_role': None
            }
        })

    return jsonify({
        'success': True,
        'message': 'Bot registered successfully'
    })

@app.route('/api/bot/status', methods=['POST'])
def get_bot_status():
    """Get bot status and stats"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    secret = data.get('secret')

    app_data = apps_collection.find_one({'owner_id': owner_id, 'secret': secret})
    if not app_data or app_data.get('plan') != 'premium':
        return jsonify({'success': False, 'message': 'Premium access required'}), 403

    bot_data = discord_bots_collection.find_one({'owner_id': owner_id})
    if not bot_data:
        return jsonify({'success': False, 'message': 'No bot registered'}), 404

    guilds = list(discord_guilds_collection.find({'owner_id': owner_id}))
    for g in guilds:
        g['_id'] = str(g['_id'])

    commands = list(discord_commands_log.find(
        {'owner_id': owner_id}
    ).sort('_id', -1).limit(50))
    for c in commands:
        c['_id'] = str(c['_id'])

    return jsonify({
        'success': True,
        'bot': {
            'bot_id': bot_data.get('bot_client_id'),
            'status': bot_data.get('status', 'offline'),
            'total_servers': bot_data.get('total_servers', 0),
            'created_at': bot_data.get('created_at'),
            'settings': bot_data.get('settings', {})
        },
        'guilds': guilds,
        'recent_commands': commands
    })

@app.route('/api/bot/update_settings', methods=['POST'])
def update_bot_settings():
    """Update bot settings"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    secret = data.get('secret')
    settings = data.get('settings', {})

    app_data = apps_collection.find_one({'owner_id': owner_id, 'secret': secret})
    if not app_data or app_data.get('plan') != 'premium':
        return jsonify({'success': False, 'message': 'Premium access required'}), 403

    discord_bots_collection.update_one(
        {'owner_id': owner_id},
        {'$set': {'settings': settings}}
    )

    return jsonify({'success': True, 'message': 'Settings updated'})

@app.route('/api/bot/stop', methods=['POST'])
def stop_discord_bot():
    """Stop the bot"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    secret = data.get('secret')

    app_data = apps_collection.find_one({'owner_id': owner_id, 'secret': secret})
    if not app_data or app_data.get('plan') != 'premium':
        return jsonify({'success': False, 'message': 'Premium access required'}), 403

    discord_bots_collection.update_one(
        {'owner_id': owner_id},
        {'$set': {'status': 'stopped'}}
    )

    return jsonify({'success': True, 'message': 'Bot stopped'})

@app.route('/api/bot/status_update', methods=['POST'])
def bot_status_update():
    """Bot updates its status"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    status = data.get('status')
    bot_id = data.get('bot_id')
    servers = data.get('servers', 0)
    bot_name = data.get('bot_name', '')

    discord_bots_collection.update_one(
        {'owner_id': owner_id},
        {'$set': {
            'status': status,
            'bot_id': bot_id,
            'bot_name': bot_name,
            'total_servers': servers,
            'last_seen': get_time()
        }}
    )

    return jsonify({'success': True})

@app.route('/api/bot/command/generate', methods=['POST'])
def bot_command_generate():
    """Handle /generate command from Discord bot"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    application_name = data.get('application')
    username = data.get('username')
    password = data.get('password')
    expiration = data.get('expiration')
    devices = data.get('devices', 1)
    unlimited = data.get('unlimited', False)
    discord_user_id = data.get('discord_user_id')

    bot_data = discord_bots_collection.find_one({'owner_id': owner_id})
    if not bot_data or bot_data.get('status') != 'online':
        return jsonify({'success': False, 'message': 'Bot is offline'}), 400

    app_data = apps_collection.find_one({'owner_id': owner_id, 'name': application_name})
    if not app_data:
        return jsonify({'success': False, 'message': 'Application not found'}), 404

    if users_collection.find_one({'username': username, 'owner_id': owner_id}):
        return jsonify({'success': False, 'message': 'Username already exists'}), 400

    import re
    expiry_date = None
    if expiration:
        if re.match(r'^\d{4}-\d{2}-\d{2}$', expiration):
            expiry_date = expiration + ' 23:59:59'
        else:
            match = re.match(r'^(\d+)([dhm])$', expiration)
            if match:
                value = int(match.group(1))
                unit = match.group(2)
                now = datetime.now()
                if unit == 'd':
                    expiry_date = (now + timedelta(days=value)).strftime('%Y-%m-%d 23:59:59')
                elif unit == 'h':
                    expiry_date = (now + timedelta(hours=value)).strftime('%Y-%m-%d %H:%M:%S')
                elif unit == 'm':
                    expiry_date = (now + timedelta(minutes=value)).strftime('%Y-%m-%d %H:%M:%S')
                elif unit == 'y':
                    expiry_date = (now + timedelta(days=value*365)).strftime('%Y-%m-%d 23:59:59')

    import secrets
    key = f"{application_name.upper()}-{secrets.token_hex(4).upper()}-{secrets.token_hex(2).upper()}"

    license_data = {
        'owner_id': owner_id,
        'key': key,
        'days': 0,
        'level': 'Premium',
        'created_at': get_time(),
        'note': f'Created by Discord user {discord_user_id}',
        'status': 0,
        'reset_count': 0,
        'is_paused': 0,
        'hwid': None,
        'expiry_date': expiry_date,
        'used_by': None,
        'devices': int(devices) if not unlimited else None,
        'unlimited': unlimited
    }
    licenses_collection.insert_one(license_data)

    user_data = {
        'owner_id': owner_id,
        'username': username,
        'password': password,
        'license': key,
        'hwid': None,
        'ip': None,
        'expiry': expiry_date,
        'level': 'Premium',
        'created_at': get_time(),
        'last_login': None,
        'banned': 0,
        'ban_reason': None,
        'session_id': None,
        'total_launches': 0,
        'extra_data': None,
        'devices': int(devices) if not unlimited else None,
        'unlimited': unlimited
    }
    users_collection.insert_one(user_data)

    licenses_collection.update_one(
        {'_id': license_data['_id']},
        {'$set': {'status': 1, 'used_by': username}
    })

    discord_commands_log.insert_one({
        'owner_id': owner_id,
        'command': '/generate',
        'discord_user_id': discord_user_id,
        'application': application_name,
        'username': username,
        'timestamp': get_time()
    })

    return jsonify({
        'success': True,
        'message': 'User created successfully',
        'username': username,
        'password': password,
        'key': key,
        'expiry': expiry_date,
        'devices': devices,
        'unlimited': unlimited
    })

@app.route('/api/bot/command/ban', methods=['POST'])
def bot_command_ban():
    """Handle /ban command from Discord bot"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    username = data.get('username')
    reason = data.get('reason', 'Banned via Discord')
    discord_user_id = data.get('discord_user_id')

    user = users_collection.find_one({'username': username, 'owner_id': owner_id})
    if not user:
        return jsonify({'success': False, 'message': 'User not found'}), 404

    users_collection.update_one(
        {'_id': user['_id']},
        {'$set': {'banned': 1, 'ban_reason': reason}}
    )

    if user.get('hwid'):
        hwid_blacklist_collection.update_one(
            {'owner_id': owner_id, 'hwid': user['hwid']},
            {'$setOnInsert': {'banned_at': get_time()}},
            upsert=True
        )

    if user.get('license'):
        licenses_collection.update_one(
            {'key': user['license'], 'owner_id': owner_id},
            {'$set': {'status': 2}}
        )

    discord_commands_log.insert_one({
        'owner_id': owner_id,
        'command': '/ban',
        'discord_user_id': discord_user_id,
        'username': username,
        'reason': reason,
        'timestamp': get_time()
    })

    return jsonify({'success': True, 'message': f'User {username} banned'})

@app.route('/api/bot/command/unban', methods=['POST'])
def bot_command_unban():
    """Handle /unban command from Discord bot"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    username = data.get('username')
    discord_user_id = data.get('discord_user_id')

    user = users_collection.find_one({'username': username, 'owner_id': owner_id})
    if not user:
        return jsonify({'success': False, 'message': 'User not found'}), 404

    users_collection.update_one(
        {'_id': user['_id']},
        {'$set': {'banned': 0, 'ban_reason': None}}
    )

    if user.get('hwid'):
        hwid_blacklist_collection.delete_one({'owner_id': owner_id, 'hwid': user['hwid']})

    if user.get('license'):
        licenses_collection.update_one(
            {'key': user['license'], 'owner_id': owner_id},
            {'$set': {'status': 1}}
        )

    discord_commands_log.insert_one({
        'owner_id': owner_id,
        'command': '/unban',
        'discord_user_id': discord_user_id,
        'username': username,
        'timestamp': get_time()
    })

    return jsonify({'success': True, 'message': f'User {username} unbanned'})

@app.route('/api/bot/command/hwid', methods=['POST'])
def bot_command_hwid():
    """Handle /hwid command from Discord bot (ban/unban HWID)"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    action = data.get('action')
    hwid = data.get('hwid')
    discord_user_id = data.get('discord_user_id')

    if action == 'ban':
        hwid_blacklist_collection.update_one(
            {'owner_id': owner_id, 'hwid': hwid},
            {'$setOnInsert': {'banned_at': get_time()}},
            upsert=True
        )
        message = f'HWID {hwid} banned'
    elif action == 'unban':
        hwid_blacklist_collection.delete_one({'owner_id': owner_id, 'hwid': hwid})
        message = f'HWID {hwid} unbanned'
    else:
        return jsonify({'success': False, 'message': 'Invalid action'}), 400

    discord_commands_log.insert_one({
        'owner_id': owner_id,
        'command': f'/hwid {action}',
        'discord_user_id': discord_user_id,
        'hwid': hwid,
        'timestamp': get_time()
    })

    return jsonify({'success': True, 'message': message})

@app.route('/api/bot/command/reset', methods=['POST'])
def bot_command_reset():
    """Handle /reset command from Discord bot (reset HWID)"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    username = data.get('username')
    discord_user_id = data.get('discord_user_id')

    user = users_collection.find_one({'username': username, 'owner_id': owner_id})
    if not user:
        return jsonify({'success': False, 'message': 'User not found'}), 404

    old_hwid = user.get('hwid')
    users_collection.update_one(
        {'_id': user['_id']},
        {'$set': {'hwid': None}}
    )

    if user.get('license'):
        licenses_collection.update_one(
            {'key': user['license'], 'owner_id': owner_id},
            {'$inc': {'reset_count': 1}}
        )

    discord_commands_log.insert_one({
        'owner_id': owner_id,
        'command': '/reset',
        'discord_user_id': discord_user_id,
        'username': username,
        'old_hwid': old_hwid,
        'timestamp': get_time()
    })

    return jsonify({'success': True, 'message': f'HWID reset for {username}'})

@app.route('/api/bot/command/createapp', methods=['POST'])
def bot_command_createapp():
    """Handle /createapp command from Discord bot"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    app_name = data.get('app_name')
    discord_user_id = data.get('discord_user_id')

    app_data = apps_collection.find_one({'owner_id': owner_id, 'name': app_name})
    if app_data:
        return jsonify({'success': False, 'message': 'Application already exists'}), 400

    import secrets
    secret = secrets.token_hex(32)
    api_key = "api_" + secrets.token_hex(16)

    apps_collection.insert_one({
        'owner_id': owner_id,
        'secret': secret,
        'name': app_name,
        'created_at': get_time(),
        'username': None,
        'password': None,
        'plan': 'premium',
        'plan_expiry': (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S'),
        'api_key': api_key,
        'settings': json.dumps({'enforce_hwid': True}),
        'status': 'active',
        'parent_owner_id': owner_id,
        'loader_hash': None,
        'webhook': None
    })

    discord_commands_log.insert_one({
        'owner_id': owner_id,
        'command': '/createapp',
        'discord_user_id': discord_user_id,
        'app_name': app_name,
        'timestamp': get_time()
    })

    return jsonify({
        'success': True,
        'message': f'Application {app_name} created',
        'owner_id': owner_id,
        'secret': secret,
        'api_key': api_key
    })

@app.route('/api/bot/command/premiumadd', methods=['POST'])
def bot_command_premiumadd():
    """Handle /premiumadd command - Add user and send credentials via DM"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    discord_user_id = data.get('discord_user_id')
    target_discord_id = data.get('target_discord_id')

    bot_data = discord_bots_collection.find_one({'owner_id': owner_id})
    if not bot_data:
        return jsonify({'success': False, 'message': 'Bot not found'}), 404

    import secrets
    import string
    chars = string.ascii_letters + string.digits
    username = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
    password = ''.join(secrets.choice(chars) for _ in range(10))

    user_data = {
        'owner_id': owner_id,
        'username': username,
        'password': password,
        'license': None,
        'hwid': None,
        'ip': None,
        'expiry': (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d 23:59:59'),
        'level': 'Premium',
        'created_at': get_time(),
        'last_login': None,
        'banned': 0,
        'ban_reason': None,
        'session_id': None,
        'total_launches': 0,
        'extra_data': json.dumps({'discord_id': target_discord_id}),
        'devices': 1,
        'unlimited': False
    }
    users_collection.insert_one(user_data)

    discord_user_links_collection.insert_one({
        'owner_id': owner_id,
        'discord_id': target_discord_id,
        'username': username,
        'created_at': get_time()
    })

    discord_commands_log.insert_one({
        'owner_id': owner_id,
        'command': '/premiumadd',
        'discord_user_id': discord_user_id,
        'target_discord_id': target_discord_id,
        'username': username,
        'timestamp': get_time()
    })

    return jsonify({
        'success': True,
        'username': username,
        'password': password,
        'target_discord_id': target_discord_id
    })

@app.route('/api/bot/user/link', methods=['POST'])
def bot_user_link():
    """Link Discord user to website account"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    discord_id = data.get('discord_id')
    username = data.get('username')
    password = data.get('password')

    user = users_collection.find_one({'username': username, 'owner_id': owner_id, 'password': password})
    if not user:
        return jsonify({'success': False, 'message': 'Invalid credentials'}), 401

    discord_user_links_collection.update_one(
        {'owner_id': owner_id, 'discord_id': discord_id},
        {'$set': {
            'username': username,
            'linked_at': get_time()
        }},
        upsert=True
    )

    return jsonify({'success': True, 'message': 'Account linked successfully'})

@app.route('/api/owner/resellers', methods=['POST'])
def owner_resellers():
    """Fetch resellers for a specific application"""
    data = request.get_json(silent=True) or request.form or {}
    owner_id = data.get('owner_id')
    secret = data.get('secret')
    
    try:
        app_row = apps_collection.find_one({'owner_id': owner_id, 'secret': secret})
        if not app_row:
            return jsonify({'success': False, 'message': 'Invalid app credentials'}), 401
        
        resellers = list(resellers_collection.find({'owner_id': owner_id}).sort('_id', -1))
        for reseller in resellers:
            reseller['_id'] = str(reseller['_id'])
        
        return jsonify({'success': True, 'resellers': resellers})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

# ==============================================================================
#                        RESELLER API SYSTEM
# ==============================================================================

@app.route('/api/reseller/login', methods=['POST'])
def reseller_login():
    """Logs in a Reseller"""
    data = request.get_json(silent=True) or request.form or {}
    
    try:
        username = str(data.get('username') or '').strip()
        password = str(data.get('password') or '').strip()
        
        reseller = resellers_collection.find_one({'username': username, 'password': password})
        if reseller:
            if reseller.get('banned') == 1:
                return jsonify({'success': False, 'message': 'Reseller account banned'}), 403
            
            log_system('reseller', f"Reseller Login: {username}", request.remote_addr)
            reseller_dict = dict(reseller)
            reseller_dict['_id'] = str(reseller_dict['_id'])
            
            return jsonify({'success': True, 'reseller': reseller_dict})
        
        log_system('reseller', f"Reseller Login Failed: {username}", request.remote_addr)
        return jsonify({'success': False, 'message': 'Invalid Reseller Credentials'}), 401
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/reseller/generate', methods=['POST'])
def reseller_generate():
    """Reseller generates keys"""
    data = request.get_json(silent=True) or request.form or {}
    username = str(data.get('username') or '').strip()
    password = str(data.get('password') or '').strip()
    days = int(data.get('days'))
    amount = int(data.get('amount', 1))
    
    try:
        reseller = resellers_collection.find_one({'username': username, 'password': password})
        if not reseller or reseller.get('banned') == 1 or reseller['balance'] < amount:
            return jsonify({'success': False, 'message': 'Insufficient Balance or Invalid Login'}), 403
        
        keys = []
        for _ in range(amount):
            k = "Secure-R-" + secrets.token_hex(4).upper()
            licenses_collection.insert_one({
                'owner_id': reseller['owner_id'],
                'key': k,
                'days': days,
                'level': 'ResellerKey',
                'created_at': get_time(),
                'gen_by': username,
                'status': 0,
                'reset_count': 0,
                'is_paused': 0,
                'hwid': None,
                'expiry_date': None,
                'used_by': None,
                'note': None
            })
            keys.append(k)
        
        resellers_collection.update_one(
            {'_id': reseller['_id']},
            {'$inc': {'balance': -amount}}
        )
        
        return jsonify({'success': True, 'keys': keys, 'new_balance': reseller['balance'] - amount})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/reseller/action', methods=['POST'])
def reseller_action():
    """Handles Reseller Actions"""
    data = request.get_json(silent=True) or request.form or {}
    username = str(data.get('username') or '').strip()
    password = str(data.get('password') or '').strip()
    action = data.get('action')
    
    try:
        reseller = resellers_collection.find_one({'username': username, 'password': password})
        if not reseller: 
            return jsonify({'success': False, 'message': 'Auth Failed'}), 401
        
        if reseller.get('banned') == 1:
            return jsonify({'success': False, 'message': 'Reseller account banned'}), 403

        if action == 'create_user':
            new_username = str(data.get('new_username') or '').strip()
            new_password = str(data.get('new_password') or '').strip()
            key = str(data.get('key') or '').strip()
            days_str = data.get('days')
            
            if not new_username or not new_password:
                return jsonify({'success': False, 'message': 'New Username and Password are required.'}), 400

            owner_id = reseller['owner_id']

            if users_collection.find_one({'username': new_username, 'owner_id': owner_id}):
                return jsonify({'success': False, 'message': 'Username already exists.'}), 400

            if key:
                lic = licenses_collection.find_one({'key': key, 'owner_id': owner_id})
                if not lic or lic.get('status') != 0:
                    return jsonify({'success': False, 'message': 'Provided license key is invalid or already used.'}), 400
                
                expiry = (datetime.now() + timedelta(days=lic['days'])).strftime('%Y-%m-%d %H:%M:%S')
                level = lic.get('level', 'Standard')
                
                licenses_collection.update_one(
                    {'_id': lic['_id']},
                    {'$set': {'status': 1, 'used_by': new_username}}
                )
            else:
                if not days_str:
                    return jsonify({'success': False, 'message': 'Duration (days) is required when no license key is provided.'}), 400
                
                try:
                    days = int(days_str)
                except (ValueError, TypeError):
                    return jsonify({'success': False, 'message': 'Invalid duration provided.'}), 400

                key = "Secure-USER-" + secrets.token_hex(4).upper()
                level = data.get('level', 'Standard')
                expiry = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
                
                licenses_collection.insert_one({
                    'owner_id': owner_id,
                    'key': key,
                    'days': days,
                    'level': level,
                    'status': 1,
                    'used_by': new_username,
                    'created_at': get_time(),
                    'gen_by': username,
                    'reset_count': 0,
                    'is_paused': 0,
                    'hwid': None,
                    'expiry_date': expiry,
                    'note': None
                })

            users_collection.insert_one({
                'owner_id': owner_id,
                'username': new_username,
                'password': new_password,
                'license': key,
                'ip': request.remote_addr,
                'expiry': expiry,
                'level': level,
                'created_at': get_time(),
                'last_login': None,
                'banned': 0,
                'ban_reason': None,
                'session_id': None,
                'total_launches': 0,
                'extra_data': None,
                'hwid': None
            })
            
            return jsonify({'success': True, 'message': 'User created successfully'})

        if action == 'request_credits':
            amount = int(data.get('amount'))
            reseller_requests_collection.insert_one({
                'reseller_username': username,
                'owner_id': reseller['owner_id'],
                'amount': amount,
                'status': 'pending',
                'created_at': get_time()
            })
            return jsonify({'success': True, 'message': 'Request Sent'})

        key = data.get('key')
        lic = licenses_collection.find_one({'key': key, 'gen_by': username})

        if not lic: 
            return jsonify({'success': False, 'message': 'Key not found or not owned by you'}), 404

        if action == 'reset_hwid':
            current_resets = lic.get('reset_count', 0)
            if current_resets >= 3:
                licenses_collection.update_one(
                    {'_id': lic['_id']},
                    {'$set': {'is_paused': 1}}
                )
                return jsonify({'success': False, 'message': 'Reset Limit Reached (3/3). Key Paused.'})
            
            licenses_collection.update_one(
                {'_id': lic['_id']},
                {'$inc': {'reset_count': 1}}
            )
            users_collection.update_many({'license': key}, {'$set': {'hwid': None}})
            
        elif action == 'toggle_pause':
            new_status = 0 if lic.get('is_paused') else 1
            licenses_collection.update_one(
                {'_id': lic['_id']},
                {'$set': {'is_paused': new_status}}
            )
            
        elif action == 'delete_key':
            licenses_collection.delete_one({'_id': lic['_id']})
            users_collection.delete_many({'license': key})

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/reseller/data', methods=['POST'])
def reseller_data_api():
    """Fetches Reseller Stats and History"""
    data = request.get_json(silent=True) or request.form or {}
    username = data.get('username')
    password = data.get('password')
    
    try:
        reseller = resellers_collection.find_one({'username': username, 'password': password})
        if not reseller:
            return jsonify({'success': False, 'message': 'Auth Failed'}), 401
        
        keys = list(licenses_collection.find({
            'owner_id': reseller['owner_id'],
            'gen_by': username
        }).sort('_id', -1))
        
        for key in keys:
            key['_id'] = str(key['_id'])
        
        return jsonify({'success': True, 'balance': reseller['balance'], 'keys': keys})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

# ==============================================================================
#                        CLIENT API (C# LOADER ENDPOINT)
# ==============================================================================

def prune_sessions():
    """Removes sessions inactive for > 15 minutes"""
    try:
        timeout = (datetime.now() - timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
        sessions_collection.delete_many({'last_heartbeat': {'$lt': timeout}})
    except: 
        pass

def sendsigned_response(data, secret):
    """Helper to send signed responses"""
    return jsonify(data)

def handle_init(appdata, conn):
    """Generates session and stores in MongoDB"""
    loader_hash = request.form.get('hash')
    if not loader_hash:
        return jsonify({"success": False, "message": "Loader hash missing"})

    app_settings = json.loads(appdata.get('settings', '{}'))
    auto_update_hash = app_settings.get('auto_update_hash', False)

    db_hash = appdata.get('loader_hash')
    
    if not db_hash or (auto_update_hash and db_hash != loader_hash):
        apps_collection.update_one(
            {'owner_id': appdata['owner_id']},
            {'$set': {'loader_hash': loader_hash}}
        )
    elif db_hash != loader_hash:
        return jsonify({"success": False, "message": "Loader integrity check failed"})

    prune_sessions()

    sessionid = secrets.token_hex(16)
    sessions_collection.insert_one({
        'session_id': sessionid,
        'created_at': datetime.now().isoformat(),
        'last_heartbeat': datetime.now().isoformat(),
        'data': None
    })
    
    return jsonify({"success": True, "message": "Initialized", "sessionid": sessionid})

def handle_license(appdata, conn):
    """Validates Key and HWID"""
    data = request.form
    sessionid = data.get('sessionid')
    key = data.get('key')
    hwid = data.get('hwid')
    owner_id = appdata['owner_id']
    
    extra_data = {
        'os': data.get('os_info'),
        'cpu': data.get('cpu'),
        'gpu': data.get('gpu'),
        'ram': data.get('ram'),
        'mobo': data.get('mobo'),
        'disk': data.get('disk'),
        'mac': data.get('mac'),
        'bios': data.get('bios'),
        'win_guid': data.get('win_guid'),
        'build': data.get('build'),
        'arch': data.get('arch'),
        'lang': data.get('lang'),
        'timezone': data.get('timezone'),
        'country': data.get('country'),
        'isp': data.get('isp'),
    }

    app_settings = json.loads(appdata.get('settings', '{}'))
    enforce_hwid = app_settings.get('enforce_hwid', True)

    blacklisted = hwid_blacklist_collection.find_one({
        '$or': [
            {'owner_id': owner_id, 'hwid': hwid},
            {'owner_id': 'GLOBAL', 'hwid': hwid}
        ]
    })
    
    if blacklisted:
        return jsonify({"success": False, "message": "HWID is Banned"})

    today = datetime.now().strftime('%Y-%m-%d')
    
    sess = sessions_collection.find_one({'session_id': sessionid})
    if not sess:
        return jsonify({"success": False, "message": "Session Invalid"})

    lic = licenses_collection.find_one({'key': key, 'owner_id': owner_id})
    if not lic:
        daily_stats_collection.update_one(
            {'owner_id': owner_id, 'date': today},
            {'$inc': {'failed_logins': 1}},
            upsert=True
        )
        return jsonify({"success": False, "message": "License Key Not Found"})

    if lic.get('status') == 2:
        daily_stats_collection.update_one(
            {'owner_id': owner_id, 'date': today},
            {'$inc': {'failed_logins': 1}},
            upsert=True
        )
        return jsonify({"success": False, "message": "License is Banned"})

    current_time = get_time()
    
    if lic.get('status') == 0:
        expiry = (datetime.now() + timedelta(days=lic['days'])).strftime('%Y-%m-%d %H:%M:%S')
        hwid_to_set = hwid if enforce_hwid else None
        
        licenses_collection.update_one(
            {'_id': lic['_id']},
            {'$set': {
                'status': 1,
                'hwid': hwid_to_set,
                'expiry_date': expiry,
                'used_by': key
            }}
        )
        
        users_collection.update_one(
            {'owner_id': owner_id, 'license': key},
            {'$setOnInsert': {
                'owner_id': owner_id,
                'username': key,
                'license': key,
                'hwid': hwid_to_set,
                'ip': request.remote_addr,
                'expiry': expiry,
                'level': lic.get('level', 'Standard'),
                'created_at': current_time,
                'last_login': current_time,
                'banned': 0,
                'ban_reason': None,
                'session_id': sessionid,
                'total_launches': 0,
                'extra_data': json.dumps(extra_data)
            }},
            upsert=True
        )
        
        daily_stats_collection.update_one(
            {'owner_id': owner_id, 'date': today},
            {'$inc': {'success_logins': 1}},
            upsert=True
        )
        
        return jsonify({"success": True, "message": "License Activated"})
    else:
        if enforce_hwid and lic.get('hwid') != hwid:
            daily_stats_collection.update_one(
                {'owner_id': owner_id, 'date': today},
                {'$inc': {'failed_logins': 1}},
                upsert=True
            )
            return jsonify({"success": False, "message": "HWID Mismatch"})
        
        if lic.get('expiry_date') and current_time > lic['expiry_date']:
            daily_stats_collection.update_one(
                {'owner_id': owner_id, 'date': today},
                {'$inc': {'failed_logins': 1}},
                upsert=True
            )
            return jsonify({"success": False, "message": "License Expired"})

        users_collection.update_one(
            {'license': key, 'owner_id': owner_id},
            {'$set': {
                'ip': request.remote_addr,
                'last_login': current_time,
                'hwid': hwid,
                'extra_data': json.dumps(extra_data)
            }, '$inc': {'total_launches': 1}}
        )

        daily_stats_collection.update_one(
            {'owner_id': owner_id, 'date': today},
            {'$inc': {'success_logins': 1}},
            upsert=True
        )
        
        return jsonify({"success": True, "message": "Login Successful", "level": lic.get('level', 'Standard')})

def handle_var(appdata, conn):
    data = request.get_json(silent=True) or request.form or {}
    varid = request.args.get('varid') or data.get('varid')
    
    var = app_vars_collection.find_one({
        'owner_id': appdata['owner_id'],
        'var_name': varid
    })
    
    if var: 
        return sendsigned_response({'success': True, 'response': var['var_value']}, appdata['secret'])
    
    return sendsigned_response({'success': False, 'response': 'Var not found'}, appdata['secret'])

def handle_details(appdata, conn):
    data = request.get_json(silent=True) or request.form or {}
    sessionid = request.args.get('sessionid') or data.get('sessionid')
    
    sess = sessions_collection.find_one({'session_id': sessionid})
    if not sess: 
        return jsonify(success=False)
    
    user = users_collection.find_one({'session_id': sessionid})
    if not user: 
        return jsonify(success=False)
    
    days_left = (datetime.strptime(user['expiry'], '%Y-%m-%d %H:%M:%S') - datetime.now()).days
    return sendsigned_response({
        'success': True, 
        'subscription': user.get('level', 'Standard'), 
        'daysLeft': days_left
    }, appdata['secret'])

def handle_session(appdata, conn):
    data = request.get_json(silent=True) or request.form or {}
    sessionid = request.args.get('sessionid') or data.get('sessionid')
    
    sess = sessions_collection.find_one({'session_id': sessionid})
    if sess: 
        return sendsigned_response({'success': True}, appdata['secret'])
    
    return sendsigned_response({'success': False, 'response': 'Session invalid'}, appdata['secret'])

def handle_blacklist(appdata, conn):
    data = request.get_json(silent=True) or request.form or {}
    hwid = request.args.get('hwid') or data.get('hwid')
    
    blacklisted = hwid_blacklist_collection.find_one({
        'owner_id': appdata['owner_id'],
        'hwid': hwid
    })
    
    return sendsigned_response({'success': not blacklisted}, appdata['secret'])

def handle_extend(appdata, conn):
    data = request.get_json(silent=True) or request.form or {}
    sessionid = request.args.get('sessionid') or data.get('sessionid')
    extension = int(request.args.get('extension', 1) or data.get('extension', 1))
    
    user = users_collection.find_one({'session_id': sessionid})
    if user:
        new_expiry = datetime.now() + timedelta(days=extension)
        users_collection.update_one(
            {'session_id': sessionid},
            {'$set': {'expiry': new_expiry.strftime('%Y-%m-%d %H:%M:%S')}}
        )
    
    return sendsigned_response({'success': True}, appdata['secret'])

def handle_upgrade(appdata, conn):
    data = request.get_json(silent=True) or request.form or {}
    sessionid = request.args.get('sessionid') or data.get('sessionid')
    key = request.args.get('key') or data.get('key')
    
    user = users_collection.find_one({'session_id': sessionid})
    lic = licenses_collection.find_one({'key': key, 'owner_id': appdata['owner_id']})
    
    if user and lic:
        expiry = datetime.now() + timedelta(days=lic['days'])
        users_collection.update_one(
            {'session_id': sessionid},
            {'$set': {
                'expiry': expiry.strftime('%Y-%m-%d %H:%M:%S'),
                'level': lic.get('level', 'Standard')
            }}
        )
    
    return sendsigned_response({'success': True}, appdata['secret'])

def handle_logs(appdata, conn):
    return sendsigned_response({'success': True, 'response': []}, appdata['secret'])

def handle_image(appdata, conn):
    data = request.get_json(silent=True) or request.form or {}
    fileid = request.args.get('fileid') or data.get('fileid')
    return sendsigned_response({'success': True, 'response': 'image data'}, appdata['secret'])

# ==============================================================================
#                        SECURE API v2 (HMAC + AES)
# ==============================================================================

@app.route('/api/v2', methods=['POST'])
@limiter.limit(config.RATELIMIT_DEFAULT)
def Secureapi():
    """
    Ultra-Secure API v2 Endpoint - For C++, Python, PHP, Java, Node.js, etc.
    Expects: ownerid, name, signature, timestamp, nonce, data (encrypted payload)
    """
    ownerid = request.form.get('ownerid')
    name = request.form.get('name')
    signature = request.form.get('signature')
    timestamp = request.form.get('timestamp')
    nonce = request.form.get('nonce')
    encrypted_payload = request.form.get('data')

    if not all([ownerid, name, signature, timestamp, nonce, encrypted_payload]):
        return jsonify({"status": "error", "message": "Missing parameters"}), 400

    try:
        app_data = apps_collection.find_one({'owner_id': ownerid, 'name': name})
        if not app_data:
            return jsonify({"status": "error", "message": "App not found"}), 404
        
        secret = app_data['secret']

        sys_settings = {item['key']: item['value'] for item in system_settings_collection.find()}
        if sys_settings.get('kill_switch') == 'true':
            return jsonify({"status": "error", "message": "System Disabled (Kill-Switch)"}), 403
        if sys_settings.get('maintenance_mode') == 'true':
            return jsonify({"status": "error", "message": "System under maintenance"}), 503

        expected_sig = SecurityCore.generate_signature(secret, encrypted_payload, timestamp, nonce)
        if not hmac.compare_digest(expected_sig, signature):
            return jsonify({"status": "error", "message": "Invalid Signature"}), 403

        try:
            if abs(time.time() - int(timestamp)) > config.HMAC_WINDOW_SECONDS:
                return jsonify({"status": "error", "message": "Request Expired"}), 403
        except ValueError:
            return jsonify({"status": "error", "message": "Invalid Timestamp"}), 400

        decrypted_json = SecurityCore.decrypt_aes(encrypted_payload, secret)
        if not decrypted_json:
            return jsonify({"status": "error", "message": "Decryption Failed"}), 400
        
        try:
            req_data = json.loads(decrypted_json)
        except:
            return jsonify({"status": "error", "message": "Invalid JSON"}), 400

        req_type = req_data.get('type')
        response_payload = {}

        if req_type == 'init':
            client_hash = req_data.get('hash')
            stored_hash = app_data.get('loader_hash')
            
            if not stored_hash:
                apps_collection.update_one(
                    {'owner_id': ownerid},
                    {'$set': {'loader_hash': client_hash}}
                )
            elif stored_hash != client_hash:
                log_system('security', f"Integrity Check Failed for {name}", request.remote_addr)
                return jsonify({"status": "error", "message": "Integrity Check Failed. Update your loader."}), 403

            session_id = secrets.token_hex(16)
            sessions_collection.insert_one({
                'session_id': session_id,
                'created_at': get_time(),
                'last_heartbeat': get_time(),
                'data': None
            })
            
            response_payload = {"session_id": session_id, "message": "Initialized"}

        elif req_type == 'login':
            session_id = req_data.get('session_id')
            key = req_data.get('key')
            hwid = req_data.get('hwid')

            sess = sessions_collection.find_one({'session_id': session_id})
            if not sess:
                return jsonify({"status": "error", "message": "Session Invalid"}), 401

            lic = licenses_collection.find_one({'key': key, 'owner_id': ownerid})
            if not lic:
                daily_stats_collection.update_one(
                    {'owner_id': ownerid, 'date': datetime.now().strftime('%Y-%m-%d')},
                    {'$inc': {'failed_logins': 1}},
                    upsert=True
                )
                return jsonify({"status": "error", "message": "Invalid Key"}), 403
            
            if lic.get('status') == 2:
                return jsonify({"status": "error", "message": "Key Banned"}), 403

            app_settings = json.loads(app_data.get('settings', '{}'))
            enforce_hwid = app_settings.get('enforce_hwid', True)

            if lic.get('status') == 0:
                expiry = (datetime.now() + timedelta(days=lic['days'])).strftime('%Y-%m-%d %H:%M:%S')
                hwid_to_set = hwid if enforce_hwid else None
                
                licenses_collection.update_one(
                    {'_id': lic['_id']},
                    {'$set': {
                        'status': 1,
                        'hwid': hwid_to_set,
                        'expiry_date': expiry,
                        'used_by': key
                    }}
                )
                
                users_collection.update_one(
                    {'owner_id': ownerid, 'license': key},
                    {'$setOnInsert': {
                        'owner_id': ownerid,
                        'username': key,
                        'license': key,
                        'hwid': hwid_to_set,
                        'ip': request.remote_addr,
                        'expiry': expiry,
                        'level': lic.get('level', 'Standard'),
                        'created_at': get_time(),
                        'last_login': None,
                        'banned': 0,
                        'ban_reason': None,
                        'session_id': session_id,
                        'total_launches': 0,
                        'extra_data': None
                    }},
                    upsert=True
                )
            else:
                if enforce_hwid and lic.get('hwid') != hwid:
                    return jsonify({"status": "error", "message": "HWID Mismatch"}), 403
                if lic.get('expiry_date') and get_time() > lic['expiry_date']:
                    return jsonify({"status": "error", "message": "Subscription Expired"}), 403

            daily_stats_collection.update_one(
                {'owner_id': ownerid, 'date': datetime.now().strftime('%Y-%m-%d')},
                {'$inc': {'success_logins': 1}},
                upsert=True
            )
            
            response_payload = {"message": "Logged in", "level": lic.get('level', 'Standard'), "expiry": lic.get('expiry_date')}

        elif req_type == 'heartbeat':
            session_id = req_data.get('session_id')
            sess = sessions_collection.find_one({'session_id': session_id})
            if not sess:
                return jsonify({"status": "error", "message": "Session Invalid"}), 401
            
            sessions_collection.update_one(
                {'session_id': session_id},
                {'$set': {'last_heartbeat': get_time()}}
            )
            
            response_payload = {"message": "Heartbeat Acknowledged"}

        else:
            return jsonify({"status": "error", "message": "Unknown Request Type"}), 400

        resp_str = json.dumps(response_payload)
        enc_resp = SecurityCore.encrypt_aes(resp_str, secret)
        resp_sig = SecurityCore.generate_signature(secret, enc_resp, timestamp, nonce)

        return jsonify({
            "status": "success",
            "payload": enc_resp,
            "signature": resp_sig
        })
    except Exception as e:
        logger.error(f"API Error: {e}")
        return jsonify({"status": "error", "message": "Internal Server Error"}), 500

# ==============================================================================
#                           SOCIAL LOGIN (OAUTH)
# ==============================================================================

@app.route("/auth/google/callback")
def google_callback():
    if not google.authorized:
        return redirect(url_for("welcome"))
    return redirect(url_for("dashboard", loggedin="true"))

@app.route("/auth/discord/callback")
def discord_callback():
    if not discord.authorized:
        return redirect(url_for("welcome"))
    return redirect(url_for("dashboard", loggedin="true"))

@app.route("/auth/status")
def auth_status():
    """Checks if a user is logged in via social auth."""
    user = None
    if google.authorized:
        resp = google.get("/oauth2/v2/userinfo")
        if resp.ok: user = resp.json()
    elif discord.authorized:
        resp = discord.get("/api/users/@me")
        if resp.ok: user = resp.json()
    
    if user:
        return jsonify({"logged_in": True, "user": user})
    return jsonify({"logged_in": False})

# ==============================================================================
#                        C# CLIENT API (SIMPLE ENDPOINT) - UNIVERSAL
# ==============================================================================

@app.route('/api/1.2', methods=['POST'])
def client_auth_v12():
    """
    UNIVERSAL CLIENT API - SAB LANGUAGES KE LIYE SAME ENDPOINT
    C#, C++, Python, PHP, Java, Node.js, Lua, Android, Rust, Go, VB.NET, Ruby, Swift, Perl
    
    Supports: init, login, register, license, var, details, session, blacklist, extend, upgrade, logs, image
    """
    try:
        data = request.form.to_dict()
        req_type = data.get('type')
        name = data.get('name')
        ownerid = data.get('ownerid')
        secret = data.get('secret')
        
        if not all([req_type, name, ownerid, secret]):
            return jsonify({'success': False, 'message': 'Missing parameters'})
        
        app = apps_collection.find_one({'owner_id': ownerid, 'name': name})
        
        if not app:
            return jsonify({'success': False, 'message': 'Application not found'})
        
        if app['secret'] != secret:
            return jsonify({'success': False, 'message': 'Invalid secret'})
        
        # ============ INIT ============
        if req_type == 'init':
            sessionid = secrets.token_hex(16)
            sessions_collection.insert_one({
                'session_id': sessionid,
                'created_at': get_time(),
                'last_heartbeat': get_time(),
                'data': None
            })
            
            return jsonify({
                'success': True,
                'message': 'Initialized',
                'sessionid': sessionid
            })
        
        # ============ LOGIN ============
        elif req_type == 'login':
            username = data.get('username')
            password = data.get('pass')
            hwid = data.get('hwid', 'DEFAULT')
            
            user = users_collection.find_one({'owner_id': ownerid, 'username': username})
            
            if not user:
                return jsonify({'success': False, 'message': 'Invalid username'})
            
            if user['password'] != password:
                return jsonify({'success': False, 'message': 'Invalid password'})
            
            if user.get('banned'):
                return jsonify({'success': False, 'message': 'User is banned'})
            
            if user.get('hwid') and user['hwid'] != hwid:
                return jsonify({'success': False, 'message': 'HWID mismatch'})
            
            if not user.get('hwid'):
                users_collection.update_one(
                    {'_id': user['_id']},
                    {'$set': {'hwid': hwid}}
                )
            
            users_collection.update_one(
                {'_id': user['_id']},
                {'$set': {'last_login': get_time(), 'ip': request.remote_addr}}
            )
            
            return jsonify({
                'success': True,
                'message': 'Login successful',
                'info': {
                    'username': user['username'],
                    'expiry': user.get('expiry', 'Lifetime'),
                    'ip': request.remote_addr,
                    'level': user.get('level', 'Standard'),
                    'subscription': user.get('level', 'Standard')
                }
            })
        
        # ============ REGISTER ============
        elif req_type == 'register':
            username = data.get('username')
            password = data.get('pass')
            key = data.get('key')
            hwid = data.get('hwid', 'DEFAULT')
            
            if users_collection.find_one({'owner_id': ownerid, 'username': username}):
                return jsonify({'success': False, 'message': 'Username already exists'})
            
            lic = licenses_collection.find_one({'owner_id': ownerid, 'key': key})
            
            if not lic or lic.get('status') != 0:
                return jsonify({'success': False, 'message': 'Invalid or used license key'})
            
            expiry = (datetime.now() + timedelta(days=lic['days'])).strftime('%Y-%m-%d %H:%M:%S')
            
            users_collection.insert_one({
                'owner_id': ownerid,
                'username': username,
                'password': password,
                'hwid': hwid,
                'ip': request.remote_addr,
                'expiry': expiry,
                'level': lic.get('level', 'Standard'),
                'created_at': get_time(),
                'last_login': get_time(),
                'license': key,
                'banned': 0,
                'ban_reason': None,
                'session_id': None,
                'total_launches': 0,
                'extra_data': None
            })
            
            licenses_collection.update_one(
                {'_id': lic['_id']},
                {'$set': {'status': 1, 'used_by': username}}
            )
            
            return jsonify({
                'success': True,
                'message': 'Registration successful'
            })
        
        # ============ LICENSE ============
        elif req_type == 'license':
            key = data.get('key')
            hwid = data.get('hwid', 'DEFAULT')
            
            lic = licenses_collection.find_one({'owner_id': ownerid, 'key': key})
            
            if not lic or lic.get('status') == 2:
                return jsonify({'success': False, 'message': 'Invalid license key'})
            
            if lic.get('hwid') and lic['hwid'] != hwid:
                return jsonify({'success': False, 'message': 'HWID mismatch'})
            
            if lic.get('status') == 0:
                expiry = (datetime.now() + timedelta(days=lic['days'])).strftime('%Y-%m-%d %H:%M:%S')
                licenses_collection.update_one(
                    {'_id': lic['_id']},
                    {'$set': {
                        'status': 1,
                        'hwid': hwid,
                        'expiry_date': expiry,
                        'used_by': key
                    }}
                )
            
            if lic.get('expiry_date') and get_time() > lic['expiry_date']:
                return jsonify({'success': False, 'message': 'License expired'})
            
            if not lic.get('hwid'):
                licenses_collection.update_one(
                    {'_id': lic['_id']},
                    {'$set': {'hwid': hwid}}
                )
            
            return jsonify({
                'success': True,
                'message': 'License validated',
                'info': {
                    'username': key,
                    'expiry': lic.get('expiry_date') if lic.get('expiry_date') else 'Lifetime',
                    'ip': request.remote_addr,
                    'level': lic.get('level', 'Standard'),
                    'subscription': lic.get('level', 'Standard')
                }
            })
        
        # ============ VAR ============
        elif req_type == 'var':
            varid = data.get('varid')
            
            var = app_vars_collection.find_one({
                'owner_id': ownerid,
                'var_name': varid
            })
            
            if var:
                return jsonify({'success': True, 'response': var['var_value']})
            
            return jsonify({'success': False, 'response': 'Var not found'})
        
        # ============ DETAILS ============
        elif req_type == 'details':
            sessionid = data.get('sessionid')
            
            sess = sessions_collection.find_one({'session_id': sessionid})
            if not sess:
                return jsonify({'success': False, 'message': 'Session invalid'})
            
            user = users_collection.find_one({'session_id': sessionid})
            if not user:
                return jsonify({'success': False, 'message': 'User not found'})
            
            days_left = (datetime.strptime(user['expiry'], '%Y-%m-%d %H:%M:%S') - datetime.now()).days
            
            return jsonify({
                'success': True,
                'subscription': user.get('level', 'Standard'),
                'daysLeft': days_left,
                'expiry': user['expiry']
            })
        
        # ============ SESSION ============
        elif req_type == 'session':
            sessionid = data.get('sessionid')
            
            sess = sessions_collection.find_one({'session_id': sessionid})
            if sess:
                return jsonify({'success': True})
            
            return jsonify({'success': False, 'response': 'Session invalid'})
        
        # ============ BLACKLIST ============
        elif req_type == 'blacklist':
            hwid = data.get('hwid')
            
            blacklisted = hwid_blacklist_collection.find_one({
                '$or': [
                    {'owner_id': ownerid, 'hwid': hwid},
                    {'owner_id': 'GLOBAL', 'hwid': hwid}
                ]
            })
            
            return jsonify({'success': not blacklisted})
        
        # ============ EXTEND ============
        elif req_type == 'extend':
            sessionid = data.get('sessionid')
            extension = int(data.get('extension', 1))
            
            user = users_collection.find_one({'session_id': sessionid})
            if user:
                new_expiry = datetime.now() + timedelta(days=extension)
                users_collection.update_one(
                    {'session_id': sessionid},
                    {'$set': {'expiry': new_expiry.strftime('%Y-%m-%d %H:%M:%S')}}
                )
            
            return jsonify({'success': True})
        
        # ============ UPGRADE ============
        elif req_type == 'upgrade':
            sessionid = data.get('sessionid')
            key = data.get('key')
            
            user = users_collection.find_one({'session_id': sessionid})
            lic = licenses_collection.find_one({'key': key, 'owner_id': ownerid})
            
            if user and lic and lic.get('status') == 0:
                expiry = datetime.now() + timedelta(days=lic['days'])
                users_collection.update_one(
                    {'session_id': sessionid},
                    {'$set': {
                        'expiry': expiry.strftime('%Y-%m-%d %H:%M:%S'),
                        'level': lic.get('level', 'Standard')
                    }}
                )
                
                licenses_collection.update_one(
                    {'_id': lic['_id']},
                    {'$set': {'status': 1, 'used_by': user['username']}}
                )
            
            return jsonify({'success': True})
        
        # ============ LOGS ============
        elif req_type == 'logs':
            return jsonify({'success': True, 'response': []})
        
        # ============ IMAGE ============
        elif req_type == 'image':
            fileid = data.get('fileid')
            return jsonify({'success': True, 'response': 'image data base64'})
        
        return jsonify({'success': False, 'message': 'Invalid request type'})
        
    except Exception as e:
        logger.error(f"Universal API Error: {e}")
        return jsonify({'success': False, 'message': str(e)})

# ==============================================================================
#                        ERROR HANDLERS
# ==============================================================================

@app.errorhandler(404)
def not_found(e):
    return jsonify({'success': False, 'message': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(e):
    return jsonify({'success': False, 'message': 'Internal server error'}), 500

if __name__ == '__main__':
    Database.init()
    # use_reloader=False is required on Windows to prevent WinError 10038 with threads
    app.run(host='0.0.0.0', port=2015, threaded=True, debug=True, use_reloader=False)