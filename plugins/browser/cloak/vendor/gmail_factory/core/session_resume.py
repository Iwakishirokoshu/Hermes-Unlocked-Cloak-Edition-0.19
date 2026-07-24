"""
Session Resume - Save and restore interrupted batch creation sessions
"""
import os
import json
import logging
from datetime import datetime

logger = logging.getLogger('gmail_creator_session')

SESSION_FILE = "data/session_state.json"


class SessionManager:
    def __init__(self, filepath=SESSION_FILE):
        self.filepath = filepath

    def save_state(self, batch_config, completed_indices, results):
        """Save current batch progress for later resume."""
        state = {
            "saved_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "batch_config": batch_config,
            "completed_indices": completed_indices,
            "results": results,
        }
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        logger.info(f"Session saved: {len(completed_indices)} completed")

    def load_state(self):
        """Load saved session state. Returns None if no session found."""
        if not os.path.exists(self.filepath):
            return None
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                state = json.load(f)
            logger.info(f"Session loaded from {state.get('saved_at', 'unknown')}")
            return state
        except Exception as e:
            logger.warning(f"Failed to load session: {e}")
            return None

    def clear_state(self):
        """Remove saved session file."""
        if os.path.exists(self.filepath):
            os.remove(self.filepath)
            logger.info("Session state cleared")

    def has_saved_session(self):
        """Check if a saved session exists."""
        return os.path.exists(self.filepath)

    def get_remaining(self, state):
        """Get list of remaining account indices to create."""
        if not state:
            return []
        total = state["batch_config"].get("num_accounts", 0)
        completed = set(state.get("completed_indices", []))
        return [i for i in range(total) if i not in completed]


session_manager = SessionManager()
