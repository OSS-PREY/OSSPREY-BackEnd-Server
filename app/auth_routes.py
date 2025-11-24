import os
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
        
        # Send back the token and user info, just as your Vue app expects
        return jsonify(
            access_token=access_token,
            user={"email": user_email, "name": user_name}
        ), 200

    except ValueError:
        # Use 'message' key for errors
        return jsonify({"message": "Invalid or expired Google token"}), 401
    except Exception as e:
        # Use 'message' key for errors
        return jsonify({"message": f"An internal error occurred: {e}"}), 500