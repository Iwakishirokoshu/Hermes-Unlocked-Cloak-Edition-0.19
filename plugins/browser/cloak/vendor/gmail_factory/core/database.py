"""
Database Manager - SQLite storage for accounts, logs, and session data
"""
import sqlite3
import os
import logging
from datetime import datetime
import json

logger = logging.getLogger('gmail_creator_db')


class DatabaseManager:
    def __init__(self, db_path="data/database.db"):
        self.db_path = db_path
        self._ensure_dir()
        self._init_db()
        self._migrate_schema()

    def _ensure_dir(self):
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        email TEXT UNIQUE NOT NULL,
                        password TEXT NOT NULL,
                        first_name TEXT DEFAULT '',
                        last_name TEXT DEFAULT '',
                        birthday TEXT DEFAULT '',
                        gender TEXT DEFAULT '',
                        proxy TEXT DEFAULT '',
                        strategy TEXT DEFAULT '',
                        sms_service TEXT DEFAULT '',
                        phone_number TEXT DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        status TEXT DEFAULT 'active',
                        notes TEXT DEFAULT ''
                    )
                ''')

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS execution_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        level TEXT,
                        message TEXT
                    )
                ''')

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS session_stats (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_start TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        total_attempts INTEGER DEFAULT 0,
                        successes INTEGER DEFAULT 0,
                        failures INTEGER DEFAULT 0,
                        strategies_used TEXT DEFAULT '{}',
                        errors TEXT DEFAULT '{}',
                        duration_seconds REAL DEFAULT 0
                    )
                ''')

                conn.commit()
                logger.debug("Database initialized successfully.")
        except sqlite3.Error as e:
            logger.error(f"Database initialization failed: {e}")

    def _migrate_schema(self):
        new_columns = [
            ("birthday", "TEXT DEFAULT ''"),
            ("gender", "TEXT DEFAULT ''"),
            ("strategy", "TEXT DEFAULT ''"),
            ("sms_service", "TEXT DEFAULT ''"),
            ("phone_number", "TEXT DEFAULT ''"),
            ("notes", "TEXT DEFAULT ''"),
        ]
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA table_info(accounts)")
                existing = {row[1] for row in cursor.fetchall()}
                for col_name, col_def in new_columns:
                    if col_name not in existing:
                        cursor.execute(f"ALTER TABLE accounts ADD COLUMN {col_name} {col_def}")
                        logger.info(f"Migrated: added column '{col_name}' to accounts table")
                conn.commit()
        except sqlite3.Error as e:
            logger.warning(f"Schema migration warning: {e}")

    def save_account(self, email, password, first_name="", last_name="",
                     proxy="", strategy="", sms_service="", phone_number="",
                     birthday="", gender="", status="active", notes=""):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO accounts
                    (email, password, first_name, last_name, birthday, gender,
                     proxy, strategy, sms_service, phone_number, status, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (email, password, first_name, last_name, birthday, gender,
                      proxy, strategy, sms_service, phone_number, status, notes,
                      datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
                conn.commit()
                logger.info(f"Account saved: {email}")
                return True
        except sqlite3.IntegrityError:
            logger.warning(f"Account already exists: {email}")
            return False
        except sqlite3.Error as e:
            logger.error(f"Failed to save account {email}: {e}")
            return False

    def get_all_accounts(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM accounts ORDER BY created_at DESC')
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Failed to retrieve accounts: {e}")
            return []

    def get_account_count(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM accounts')
                return cursor.fetchone()[0]
        except sqlite3.Error:
            return 0

    def update_account_status(self, email, status, notes=""):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'UPDATE accounts SET status=?, notes=? WHERE email=?',
                    (status, notes, email)
                )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Failed to update account {email}: {e}")
            return False

    def log_event(self, level, message):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO execution_logs (level, message, timestamp)
                    VALUES (?, ?, ?)
                ''', (level, message, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to save log: {e}")

    def save_session_stats(self, total_attempts, successes, failures,
                           strategies_used, errors, duration_seconds):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO session_stats
                    (total_attempts, successes, failures, strategies_used, errors, duration_seconds)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (total_attempts, successes, failures,
                      json.dumps(strategies_used), json.dumps(errors),
                      duration_seconds))
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to save session stats: {e}")

    def get_session_history(self, limit=10):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    'SELECT * FROM session_stats ORDER BY session_start DESC LIMIT ?',
                    (limit,)
                )
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error:
            return []

    def run_migration(self, old_json_path="data/accounts.json", old_txt_path="data/accounts.txt"):
        migrated_count = 0

        if os.path.exists(old_txt_path):
            try:
                with open(old_txt_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and ":" in line:
                            parts = line.split(":")
                            email = parts[0]
                            password = parts[1] if len(parts) > 1 else ""
                            if self.save_account(email, password):
                                migrated_count += 1
            except Exception as e:
                logger.error(f"TXT migration failed: {e}")

        if os.path.exists(old_json_path):
            try:
                with open(old_json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for acc in data:
                    if isinstance(acc, dict) and 'email' in acc and 'password' in acc:
                        if self.save_account(
                            email=acc.get('email'),
                            password=acc.get('password'),
                            first_name=acc.get('first_name', ''),
                            last_name=acc.get('last_name', ''),
                            status=acc.get('status', 'active'),
                        ):
                            migrated_count += 1
            except Exception as e:
                logger.error(f"JSON migration failed: {e}")

        return migrated_count
