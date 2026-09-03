import os
import logging
import requests
from flask import Blueprint, request, jsonify
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from flask_jwt_extended import create_access_token
from pymongo import MongoClient
from app.config import Config
from datetime import datetime

# Initialize MongoDB client (same as in your routes.py)
mongo_client = MongoClient(Config.MONGODB_URI)
db = mongo_client[Config.MONGODB_DB_NAME]

auth_bp = Blueprint('auth_bp', __name__)

@auth_bp.route('/api/google_login', methods=['POST'])
def google_auth():
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    if not GOOGLE_CLIENT_ID:
        return jsonify({"message": "Server configuration error: GOOGLE_CLIENT_ID not set"}), 500

    data = request.get_json()
    
    # Check for 'credential' key from your Vue app
    if not data or 'credential' not in data:
        # Use 'message' key for errors
        return jsonify({"message": "Missing credential"}), 400

    token = data['credential']

    try:
        # Verify the Google ID token
        idinfo = id_token.verify_oauth2_token(
            token, google_requests.Request(), GOOGLE_CLIENT_ID
        )

        user_email = idinfo['email']
        user_name = idinfo.get('name')

        # --- Your User Logic ---
        # Find user in your MongoDB 'users' collection
        user = db.users.find_one({'email': user_email})
        
        if not user:
            # User doesn't exist, create a new one
            user_doc = {
                'full_name': user_name,
                'email': user_email,
                'affiliation': idinfo.get('hd', 'Google User'), # 'hd' is hosted domain
                'password_hash': None, # No password for Google login
                'referral': 'Google Sign-In',
                'registered_at': datetime.utcnow()
            }
            db.users.insert_one(user_doc)
        
        # --- End of User Logic ---

        # Create your own session token for the user
        access_token = create_access_token(identity=user_email)
        
        # Send back the token and user info, just as your Vue app expects.
        # Re-read the account so an existing user's saved profile comes back
        # instead of being overwritten by whatever Google reports.
        stored = db.users.find_one({'email': user_email}) or {}

        return jsonify(
            access_token=access_token,
            user={
                "email": user_email,
                "name": stored.get('full_name', user_name),
                "affiliation": stored.get('affiliation', ''),
                "role": stored.get('role', ''),
            }
        ), 200

    except ValueError:
        # Use 'message' key for errors
        return jsonify({"message": "Invalid or expired Google token"}), 401
    except Exception as e:
        # Log the detail; never return it -- an exception string can carry
        # internals (paths, hostnames, driver errors) to an unauthenticated caller.
        logging.error(f"Google login failed: {e}")
        return jsonify({"message": "An internal error occurred."}), 500


GITHUB_HTTP_TIMEOUT = 15      # both GitHub calls are small, fast API requests


@auth_bp.route('/api/github_login', methods=['POST'])
def github_auth():
    """OAuth login via GitHub. The browser sends the ``code`` GitHub redirected
    back with; we exchange it for an access token and resolve the user's email."""
    GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID")
    GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET")
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        return jsonify({"message": "Server configuration error: GITHUB_CLIENT_ID/GITHUB_CLIENT_SECRET not set"}), 500

    data = request.get_json(silent=True) or {}
    code = data.get('code')
    if not code:
        # Use 'message' key for errors
        return jsonify({"message": "Missing code"}), 400

    try:
        # Exchange the code for an access token. GitHub answers failures with
        # HTTP 200 and an error payload, so the body decides, not the status.
        token_response = requests.post(
            'https://github.com/login/oauth/access_token',
            json={'client_id': GITHUB_CLIENT_ID,
                  'client_secret': GITHUB_CLIENT_SECRET,
                  'code': code},
            headers={'Accept': 'application/json'},
            timeout=GITHUB_HTTP_TIMEOUT,
        )
        token_data = token_response.json() if token_response.ok else {}
        access_token = token_data.get('access_token')
        if not access_token:
            logging.warning(
                f"GitHub code exchange failed: {token_data.get('error')} "
                f"({token_data.get('error_description')})"
                if token_data else f"GitHub code exchange returned {token_response.status_code}")
            return jsonify({"message": "Invalid or expired GitHub login code"}), 401

        headers = {'Authorization': f'Bearer {access_token}',
                   'Accept': 'application/vnd.github+json'}
        profile = requests.get('https://api.github.com/user', headers=headers,
                               timeout=GITHUB_HTTP_TIMEOUT)
        if profile.status_code != 200:
            logging.error(f"GitHub /user returned {profile.status_code}")
            return jsonify({"message": "Could not read the GitHub profile"}), 401
        profile = profile.json()

        # The email is null when the user keeps it private; the primary
        # verified address is then available from the email endpoint.
        user_email = profile.get('email')
        if not user_email:
            emails = requests.get('https://api.github.com/user/emails', headers=headers,
                                  timeout=GITHUB_HTTP_TIMEOUT)
            if emails.status_code == 200:
                primary = next((e for e in emails.json()
                                if e.get('primary') and e.get('verified')), None)
                user_email = (primary or {}).get('email')
        if not user_email:
            return jsonify({"message": "Your GitHub account has no verified email address."}), 401
        user_email = user_email.strip().lower()

        user_name = profile.get('name') or profile.get('login')

        # --- Same account logic as google_auth ---
        user = db.users.find_one({'email': user_email})
        if not user:
            user_doc = {
                'full_name': user_name,
                'email': user_email,
                'affiliation': profile.get('company') or 'GitHub User',
                'password_hash': None,  # No password for GitHub login
                'referral': 'GitHub Sign-In',
                'registered_at': datetime.utcnow()
            }
            db.users.insert_one(user_doc)

        session_token = create_access_token(identity=user_email)

        # Re-read the account so an existing user's saved profile comes back
        # instead of being overwritten by whatever GitHub reports.
        stored = db.users.find_one({'email': user_email}) or {}

        return jsonify(
            access_token=session_token,
            user={
                "email": user_email,
                "name": stored.get('full_name', user_name),
                "affiliation": stored.get('affiliation', ''),
                "role": stored.get('role', ''),
            }
        ), 200

    except requests.RequestException as e:
        # GitHub itself could not be reached at all.
        logging.error(f"GitHub login request failed: {e}")
        return jsonify({"message": "Could not reach GitHub. Please try again shortly."}), 503
    except Exception as e:
        # Log the detail; never return it -- an exception string can carry
        # internals (paths, hostnames, driver errors) to an unauthenticated caller.
        logging.error(f"GitHub login failed: {e}")
        return jsonify({"message": "An internal error occurred."}), 500