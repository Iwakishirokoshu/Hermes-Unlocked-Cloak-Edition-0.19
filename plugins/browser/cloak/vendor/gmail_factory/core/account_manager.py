"""
Account Manager - Unified account storage, export, and migration (SQLite-backed)
"""
import os
import csv
import json
import logging
from datetime import datetime
from core.database import DatabaseManager

logger = logging.getLogger('gmail_creator_accounts')


class AccountManager:
    def __init__(self):
        self.db = DatabaseManager()

    def save(self, email, password, first_name="", last_name="",
             proxy="", strategy="", sms_service="", phone_number="",
             birthday="", gender="", status="active", notes=""):
        return self.db.save_account(
            email=email, password=password,
            first_name=first_name, last_name=last_name,
            proxy=proxy, strategy=strategy,
            sms_service=sms_service, phone_number=phone_number,
            birthday=birthday, gender=gender,
            status=status, notes=notes,
        )

    def get_all(self):
        return self.db.get_all_accounts()

    def get_count(self):
        accounts = self.db.get_all_accounts()
        return len(accounts)

    def get_stats(self):
        accounts = self.db.get_all_accounts()
        total = len(accounts)
        active = sum(1 for a in accounts if a.get("status") == "active")
        strategies = {}
        sms_services = {}
        for a in accounts:
            s = a.get("strategy", "unknown") or "unknown"
            strategies[s] = strategies.get(s, 0) + 1
            svc = a.get("sms_service", "") or ""
            if svc:
                sms_services[svc] = sms_services.get(svc, 0) + 1

        return {
            "total": total,
            "active": active,
            "success_rate": (active / total * 100) if total > 0 else 0,
            "strategies": strategies,
            "sms_services": sms_services,
        }

    def get_last_account(self):
        accounts = self.db.get_all_accounts()
        return accounts[0] if accounts else None

    def export_csv(self, filepath=None):
        if not filepath:
            filepath = f"data/accounts_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        accounts = self.db.get_all_accounts()
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Email", "Password", "First Name", "Last Name",
                             "Proxy", "Strategy", "SMS Service", "Status", "Created At"])
            for acc in accounts:
                writer.writerow([
                    acc.get("email", ""), acc.get("password", ""),
                    acc.get("first_name", ""), acc.get("last_name", ""),
                    acc.get("proxy", ""), acc.get("strategy", ""),
                    acc.get("sms_service", ""), acc.get("status", ""),
                    acc.get("created_at", ""),
                ])
        logger.info(f"Exported {len(accounts)} accounts to CSV: {filepath}")
        return filepath

    def export_json(self, filepath=None):
        if not filepath:
            filepath = f"data/accounts_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        accounts = self.db.get_all_accounts()
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)
        logger.info(f"Exported {len(accounts)} accounts to JSON: {filepath}")
        return filepath

    def export_txt(self, filepath=None):
        if not filepath:
            filepath = f"data/accounts_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        accounts = self.db.get_all_accounts()
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            for acc in accounts:
                f.write(f"{acc.get('email', '')}:{acc.get('password', '')}\n")
        logger.info(f"Exported {len(accounts)} accounts to TXT: {filepath}")
        return filepath

    def migrate_old_data(self):
        migrated = 0

        # Migrate accounts.txt
        txt_path = "data/accounts.txt"
        if os.path.exists(txt_path):
            try:
                with open(txt_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and ":" in line:
                            parts = line.split(":")
                            email = parts[0]
                            password = parts[1] if len(parts) > 1 else ""
                            if self.db.save_account(email=email, password=password):
                                migrated += 1
                logger.info(f"Migrated {migrated} accounts from accounts.txt")
            except Exception as e:
                logger.error(f"TXT migration failed: {e}")

        # Migrate accounts.json
        json_path = "data/accounts.json"
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                count = 0
                for acc in data:
                    if isinstance(acc, dict) and "email" in acc:
                        if self.db.save_account(
                            email=acc.get("email", ""),
                            password=acc.get("password", ""),
                            first_name=acc.get("first_name", ""),
                            last_name=acc.get("last_name", ""),
                            status=acc.get("status", "active"),
                        ):
                            count += 1
                migrated += count
                logger.info(f"Migrated {count} accounts from accounts.json")
            except Exception as e:
                logger.error(f"JSON migration failed: {e}")

        return migrated


account_manager = AccountManager()
