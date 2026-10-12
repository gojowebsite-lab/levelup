import json
import os
import time
from typing import Dict, Any, Optional

CONFIG_PATH = "config.json"
CACHE_PATH = "token_cache.json"

class LoginManager:
    def __init__(self):
        self.config = self._load_json(CONFIG_PATH)
        self.cache = self._load_json(CACHE_PATH)

    def _load_json(self, path: str) -> Dict[str, Any]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                print(f"[!] [CACHE] Corrupted file detected at {path}. Reinitializing...")
        return {}

    def _save_json(self, path: str, data: Dict[str, Any]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)

    def evaluate_login_safety(self, uid: str) -> bool:
        """
        Evaluates whether a MajorLogin is safe based on the gap configuration 
        to prevent account bans.
        """
        safety_config = self.config.get("safety", {})
        gap_days = safety_config.get("major_login_gap_days", 5)
        
        account_data = self.cache.get(uid, {})
        last_login_time = account_data.get("last_major_login", 0.0)
        current_time = time.time()
        
        # Convert days to seconds
        gap_seconds = gap_days * 24 * 60 * 60
        
        if last_login_time > 0 and (current_time - last_login_time) < gap_seconds:
            hours_ago = (current_time - last_login_time) / 3600
            print(f"[-] [SAFETY] Fresh LEVEL-UP login refused: uid {uid} logged in {hours_ago:.1f} h ago - blocked to prevent ban.")
            return False
        
        return True

    def handle_session_auth(self, uid: str, open_id: str) -> Optional[str]:
        """
        Handles session authentication, prioritizing session reuse via OpenID 
        to bypass restricted MajorLogin triggers.
        """
        safety_config = self.config.get("safety", {})
        reuse_enabled = safety_config.get("reuse_session_by_open_id", True)

        account_data = self.cache.get(uid)

        # 1. Attempt token recovery from cache if valid
        if account_data and account_data.get("token"):
            if not account_data.get("invalidated", False):
                print(f"[+] [CACHE] Successfully retrieved valid token for UID: {uid}")
                return account_data["token"]
            else:
                print(f"[!] [CACHE] Token invalidated for UID: {uid}")

        # 2. Check if we can reuse session via OpenID
        if reuse_enabled and account_data and account_data.get("open_id") == open_id:
            print(f"[+] [SESSION] Reusing existing active session for OpenID mapping: {open_id}")
            # Refresh token mapping without triggering a MajorLogin
            token = f"reused_session_token_{uid}"
            account_data["token"] = token
            account_data["invalidated"] = False
            self._save_json(CACHE_PATH, self.cache)
            return token

        # 3. Fallback to MajorLogin check if session reuse isn't possible
        if not self.evaluate_login_safety(uid):
            print(f"[!] [RETRY] Login bypassed for UID {uid} due to safety constraints. Retrying later.")
            return None

        # Perform Fresh MajorLogin
        print(f"[*] [AUTH] Executing fresh MajorLogin for UID: {uid}")
        new_token = f"major_login_token_{uid}"
        
        self.cache[uid] = {
            "token": new_token,
            "open_id": open_id,
            "last_major_login": time.time(),
            "invalidated": False
        }
        self._save_json(CACHE_PATH, self.cache)
        
        return new_token

if __name__ == "__main__":
    manager = LoginManager()
    # Example execution test
    test_uid = "123456789"
    test_open_id = "openid_abc_xyz"
    
    token = manager.handle_session_auth(test_uid, test_open_id)
    print(f"Authentication Result Token: {token}")
    