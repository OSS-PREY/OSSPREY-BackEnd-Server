import os
import math
from flask import Blueprint, jsonify, redirect, request, url_for
from flask_cors import cross_origin
from app.config import Config
from pymongo import MongoClient
import logging
from app.pipeline.orchestrator import run_pipeline
from app.pipeline.run_pex import run_forecast
from app.pipeline.calibration import calibrate
from app.pipeline.rust_runner import run_rust_code
from app.pipeline.update_pex import update_pex_generator
from app.services.queue_manager import (
    QueueManager,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
)
from app.services.mailer import send_email
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, timezone
from flask_jwt_extended import create_access_token, get_jwt_identity, jwt_required
import hashlib
import re
import secrets

main_routes = Blueprint('main_routes', __name__)

# Reference data (forecasts, networks, links, measures) comes from JSON
# files; the stateful collections stay on Mongo. See app/db.py.
from .db import db, mongo_client

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _pipeline_result_is_error(result):
    """Treat a pipeline result that carries an 'error' key as a failed job."""
    return isinstance(result, dict) and bool(result.get('error'))


def _repo_name_from_git_link(git_link):
    """Extract an ``owner/repo`` name from a git URL (HTTPS or SSH form)."""
    if not git_link:
        return None
    link = git_link.strip()
    if link.lower().endswith('.git'):
        link = link[:-4]
    link = link.rstrip('/')
    if '://' not in link and ':' in link:
        # SSH form, e.g. git@github.com:owner/repo
        link = link.split(':', 1)[1]
    parts = [p for p in link.split('/') if p]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return parts[-1] if parts else None


def _persist_job(snapshot):
    """Mirror a queue job state change into MongoDB (``repo_jobs`` collection).

    Registered as the queue's ``on_update`` listener so job history survives
    backend restarts (the queue itself is in-memory). Failures here are logged
    but never affect the pipeline.
    """
    try:
        metadata = snapshot.get('metadata') or {}
        git_link = metadata.get('git_link', '')
        db.repo_jobs.update_one(
            {'job_id': snapshot['job_id']},
            {'$set': {
                'job_id': snapshot['job_id'],
                'git_link': git_link,
                'repo_name': _repo_name_from_git_link(git_link) or git_link,
                'status': snapshot['status'],
                'created_at': snapshot['created_at'],
                'started_at': snapshot['started_at'],
                'finished_at': snapshot['finished_at'],
                'error': snapshot['error'],
            }},
            upsert=True,
        )
    except Exception:
        logger.exception('Failed to persist job %s', snapshot.get('job_id'))


# Shared FIFO queue that runs the repository-processing pipeline with bounded
# concurrency. Tunable via environment variables:
#   MAX_CONCURRENT_JOBS   - max jobs running at once (default 2)
#   ESTIMATED_JOB_SECONDS - seed estimate for wait-time calculations (default 120)
pipeline_queue = QueueManager(
    worker=run_pipeline,
    max_concurrent=int(os.environ.get('MAX_CONCURRENT_JOBS', '2')),
    default_job_seconds=int(os.environ.get('ESTIMATED_JOB_SECONDS', '120')),
    result_is_error=_pipeline_result_is_error,
    on_update=_persist_job,
)


def _sweep_stale_jobs():
    """Fail MongoDB job records left non-terminal by a previous backend process.

    The queue lives in process memory, so after a restart nothing will ever
    advance those jobs again; without this sweep they would look pending
    forever.
    """
    try:
        result = db.repo_jobs.update_many(
            {'status': {'$in': [STATUS_QUEUED, STATUS_RUNNING]}},
            {'$set': {
                'status': STATUS_FAILED,
                'error': 'Interrupted by a backend restart.',
                'finished_at': datetime.now(timezone.utc).isoformat(),
            }},
        )
        if result.modified_count:
            logger.info('Marked %d stale repo job(s) as failed.', result.modified_count)
    except Exception:
        logger.exception('Failed to sweep stale repo jobs')


_sweep_stale_jobs()

# This is to prevent any error occurring because of NaN value - this is converted to null
def sanitize_document(doc):
    """
    Recursively sanitize the document by replacing NaN with None.
    """
    for key, value in doc.items():
        if isinstance(value, float) and math.isnan(value):
            doc[key] = None
        elif isinstance(value, dict):
            sanitize_document(value)
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                if isinstance(item, dict):
                    sanitize_document(item)
                elif isinstance(item, float) and math.isnan(item):
                    value[idx] = None
    return doc


# ------------------------- Authentication Endpoints -------------------------


@main_routes.route('/api/register', methods=['POST'])
@cross_origin(origin='*')
def register_user():
    """Register a new user with the provided information."""
    data = request.get_json(silent=True) or {}
    required_fields = ['full_name', 'email', 'affiliation', 'password', 'referral']
    if any(field not in data or not data[field] for field in required_fields):
        # Use 'message' key for errors
        return jsonify({'message': 'All fields are required.'}), 400

    email = data.get('email', '').strip().lower()
    data['email'] = email
    if db.users.find_one({'email': email}):
        # Use 'message' key for errors
        return jsonify({'message': 'User is Already Registered!'}), 400

    user_doc = {
        'full_name': data['full_name'],
        'email': email,
        'affiliation': data['affiliation'],
        'password_hash': generate_password_hash(data['password']),
        'referral': data['referral'],
        'registered_at': datetime.utcnow()
    }

    db.users.insert_one(user_doc)
    return jsonify({'message': 'User registered successfully.'}), 201


@main_routes.route('/api/login', methods=['POST'])
@cross_origin(origin='*')
def login_user():
    """Validate user credentials."""
    data = request.get_json(silent=True) or {}
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        # Use 'message' key for errors
        return jsonify({'message': 'Email and password are required.'}), 400

    user = db.users.find_one({'email': email})
    if not user or not check_password_hash(user.get('password_hash', ''), password):
        # Use 'message' key for errors
        return jsonify({'message': 'Invalid email or password.'}), 401

    # --- MODIFIED RESPONSE ---
    # Create an access token for the user
    access_token = create_access_token(identity=user['email'])
    
    # Prepare user data to return, matching the frontend's expectation
    user_data = _user_profile(user)

    # Return token and user data
    return jsonify(
        access_token=access_token,
        user=user_data
    ), 200


# Password reset tokens are single-use and short lived.
RESET_TOKEN_TTL = timedelta(hours=1)
# Refuse to issue a second reset mail for the same address within this window,
# so the endpoint cannot be used to flood someone's inbox.
RESET_RESEND_INTERVAL = timedelta(minutes=1)

# Returned for every /api/forgot_password call, whether or not the address is
# registered: a different reply for unknown addresses would turn this endpoint
# into an account-enumeration oracle.
RESET_GENERIC_MESSAGE = (
    'If an account exists for that address, a reset link is on its way.'
)


def _hash_reset_token(token):
    """Store only the hash, so a leaked database cannot be used to reset passwords."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _password_is_acceptable(password):
    """At least 8 characters and 3 of: lower, upper, digit, special.

    Mirrors the rule the front-end shows on the register and reset pages; the
    client check is a convenience, this one is the one that counts.
    """
    if not isinstance(password, str) or len(password) < 8:
        return False

    categories = (
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(not c.isalnum() for c in password),
    )

    return sum(categories) >= 3


@main_routes.route('/api/forgot_password', methods=['POST'])
@cross_origin(origin='*')
def forgot_password():
    """Email a password reset link to a registered address."""
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()

    if not email:
        return jsonify({'message': 'Email is required.'}), 400

    user = db.users.find_one({'email': email})
    if not user:
        return jsonify({'message': RESET_GENERIC_MESSAGE}), 200

    now = datetime.utcnow()

    recent = db.password_resets.find_one({
        'email': email,
        'created_at': {'$gt': now - RESET_RESEND_INTERVAL},
    })
    if recent:
        # Same reply as the success path, again to keep the endpoint silent
        # about which addresses exist.
        return jsonify({'message': RESET_GENERIC_MESSAGE}), 200

    # Any outstanding link for this address stops working once a new one is
    # issued. This also keeps expired rows from piling up per user.
    # ponytail: cleanup is per-address on issue; add a TTL index on expires_at
    # if the collection ever needs bounding without a reset request.
    db.password_resets.delete_many({'email': email})

    token = secrets.token_urlsafe(32)
    db.password_resets.insert_one({
        'email': email,
        'token_hash': _hash_reset_token(token),
        'created_at': now,
        'expires_at': now + RESET_TOKEN_TTL,
        'used_at': None,
    })

    base_url = os.environ.get('FRONTEND_BASE_URL', 'http://localhost:3000').rstrip('/')
    reset_link = f"{base_url}/reset-password?token={token}"
    hours = int(RESET_TOKEN_TTL.total_seconds() // 3600)

    send_email(
        email,
        'Reset your OSSPREY password',
        'We received a request to reset the password for your OSSPREY account.\n\n'
        f'Open this link to choose a new password:\n{reset_link}\n\n'
        f'The link expires in {hours} hour(s) and can only be used once.\n'
        'If you did not request a reset you can ignore this message; '
        'your password will not change.\n',
    )

    return jsonify({'message': RESET_GENERIC_MESSAGE}), 200


@main_routes.route('/api/reset_password', methods=['POST'])
@cross_origin(origin='*')
def reset_password():
    """Set a new password given a valid, unexpired reset token."""
    data = request.get_json(silent=True) or {}
    token = (data.get('token') or '').strip()
    password = data.get('password') or ''

    if not token or not password:
        return jsonify({'message': 'Token and password are required.'}), 400

    if not _password_is_acceptable(password):
        return jsonify({
            'message': 'Password must be at least 8 characters long and include at '
                       'least three of the following: lower case letters, upper case '
                       'letters, numbers, and special characters.',
        }), 400

    record = db.password_resets.find_one({'token_hash': _hash_reset_token(token)})
    now = datetime.utcnow()
    if not record or record.get('used_at') or record.get('expires_at', now) <= now:
        return jsonify({'message': 'This reset link is invalid or has expired.'}), 400

    result = db.users.update_one(
        {'email': record['email']},
        {'$set': {
            'password_hash': generate_password_hash(password),
            'password_reset_at': now,
        }},
    )
    if not result.matched_count:
        # Account removed between the request and the reset.
        return jsonify({'message': 'This reset link is invalid or has expired.'}), 400

    # Burn the token, and drop any other outstanding link for the account.
    db.password_resets.update_one({'_id': record['_id']}, {'$set': {'used_at': now}})
    db.password_resets.delete_many({'email': record['email'], 'used_at': None})

    logger.info('Password reset completed for %s', record['email'])

    return jsonify({'message': 'Password has been reset.'}), 200


# Longest accepted value for a free-text profile field, so a caller cannot
# grow a user document without bound.
PROFILE_FIELD_MAX_LENGTH = 200


def _user_profile(user):
    """The user shape the front-end stores in localStorage."""
    return {
        'email': user['email'],
        'name': user.get('full_name', user['email']),
        'affiliation': user.get('affiliation', ''),
        'role': user.get('role', ''),
    }


@main_routes.route('/api/update_profile', methods=['POST'])
@cross_origin(origin='*')
@jwt_required()
def update_profile():
    """Update the signed-in user's own profile.

    The account is taken from the access token, never from the request body:
    the email is the primary key and is not editable here, and trusting a body
    field would let anyone rewrite another account's profile by naming it.
    """
    email = get_jwt_identity()
    data = request.get_json(silent=True) or {}

    # The front-end sends the email back for readability; accept it only when
    # it agrees with the token, so a mismatch surfaces instead of silently
    # editing the wrong account.
    body_email = (data.get('email') or '').strip().lower()
    if body_email and body_email != email:
        return jsonify({'message': 'You can only update your own profile.'}), 403

    name = (data.get('name') or '').strip()
    affiliation = (data.get('affiliation') or '').strip()
    role = (data.get('role') or '').strip()

    if not name or not affiliation:
        return jsonify({'message': 'Name and affiliation are required.'}), 400

    if any(len(v) > PROFILE_FIELD_MAX_LENGTH for v in (name, affiliation, role)):
        return jsonify({
            'message': f'Profile fields must be {PROFILE_FIELD_MAX_LENGTH} characters or fewer.',
        }), 400

    result = db.users.update_one(
        {'email': email},
        {'$set': {
            'full_name': name,
            'affiliation': affiliation,
            'role': role,
            'profile_updated_at': datetime.utcnow(),
        }},
    )
    if not result.matched_count:
        return jsonify({'message': 'User not found.'}), 404

    user = db.users.find_one({'email': email})

    return jsonify({
        'message': 'Profile updated successfully.',
        'user': _user_profile(user),
    }), 200


@main_routes.route('/api/track_login', methods=['POST'])
@cross_origin(origin='*')
def track_login():
    """Record a user's login event."""
    data = request.get_json(silent=True) or {}
    user_email = data.get('user_email')
    if not user_email:
        # Use 'message' key for errors
        return jsonify({'message': 'user_email is required.'}), 400

    record = {
        'user_email': user_email,
        'timestamp': datetime.utcnow()
    }

    try:
        db.login_tracking.insert_one(record)
        return jsonify({'message': 'Login tracked.'}), 201
    except Exception as e:
        logger.error(f"Error recording login for {user_email}: {e}")
        # Use 'message' key for errors
        return jsonify({'message': 'Failed to track login.'}), 500


@main_routes.route('/api/track_logout', methods=['POST'])
@cross_origin(origin='*')
def track_logout():
    """Record a user's logout event."""
    data = request.get_json(silent=True) or {}
    user_email = data.get('user_email')
    if not user_email:
        # Use 'message' key for errors
        return jsonify({'message': 'user_email is required.'}), 400

    record = {
        'user_email': user_email,
        'timestamp': datetime.utcnow()
    }

    try:
        db.logout_tracking.insert_one(record)
        return jsonify({'message': 'Logout tracked.'}), 201
    except Exception as e:
        logger.error(f"Error recording logout for {user_email}: {e}")
        # Use 'message' key for errors
        return jsonify({'message': 'Failed to track logout.'}), 500


@main_routes.route('/api/process_repo', methods=['POST'])
@cross_origin(origin='*')
def process_repo():
    """Record a repository processing request."""
    data = request.get_json(silent=True) or {}
    user_email = data.get('user_email')
    github_repo = data.get('github_repo')
    timestamp = data.get('timestamp')

    if not user_email or not github_repo or not timestamp:
        # Use 'message' key for errors
        return jsonify({'message': 'user_email, github_repo, and timestamp are required.'}), 400

    try:
        timestamp_dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        # Use 'message' key for errors
        return jsonify({'message': 'Invalid timestamp format.'}), 400

    record = {
        'user_email': user_email,
        'github_repo': github_repo,
        'timestamp': timestamp_dt
    }

    try:
        db.user_repo_requests.insert_one(record)
        return jsonify({'message': 'Repository request recorded.'}), 201
    except Exception as e:
        logger.error(f"Error saving repository request: {e}")
        # Use 'message' key for errors
        return jsonify({'message': 'Failed to record request.'}), 500

# --------------------- User Data Retrieval Endpoints ---------------------

# (The rest of your routes.py file remains unchanged)
# ... all your other routes ...

@main_routes.route('/api/users', methods=['GET'])
@cross_origin(origin='*')
def get_all_users():
    """Fetch all registered users with their metadata."""
    try:
        users = list(db.users.find({}, {'_id': 0, 'password_hash': 0}))
        users = [sanitize_document(user) for user in users]
        return jsonify({'users': users}), 200
    except Exception as e:
        logger.error(f"Error fetching users from MongoDB: {e}")
        return jsonify({'error': 'Failed to fetch users.'}), 500


@main_routes.route('/api/user_repositories', methods=['GET'])
@cross_origin(origin='*')
def get_user_repositories():
    """Fetch all GitHub repositories processed by a given user."""
    email = request.args.get('email')
    if not email:
        return jsonify({'error': 'email query parameter is required.'}), 400

    try:
        records = list(db.user_repo_requests.find({'user_email': email}, {
            '_id': 0,
            'github_repo': 1
        }))
        repos = sorted({rec['github_repo'] for rec in records})
        return jsonify({'repositories': repos}), 200
    except Exception as e:
        logger.error(f"Error fetching repositories for user {email}: {e}")
        return jsonify({'error': 'Failed to fetch repositories.'}), 500

# ------------------------- View Tracking Endpoints -------------------------


@main_routes.route('/api/record_view', methods=['POST'])
@cross_origin(origin='*')
def record_view():
    """Record a view by storing the current timestamp."""
    timestamp = datetime.utcnow()
    try:
        db.view_timestamps.insert_one({'timestamp': timestamp})
        return jsonify({
            'message': 'View recorded.',
            'timestamp': timestamp.isoformat() + 'Z'
        }), 201
    except Exception as e:
        logger.error(f"Error recording view: {e}")
        return jsonify({'error': 'Failed to record view.'}), 500


@main_routes.route('/api/view_count', methods=['GET'])
@cross_origin(origin='*')
def get_view_count():
    """Return the total number of recorded view timestamps."""
    try:
        count = db.view_timestamps.count_documents({})
        return jsonify({'count': count}), 200
    except Exception as e:
        logger.error(f"Error retrieving view count: {e}")
        return jsonify({'error': 'Failed to retrieve view count.'}), 500


# --------------------- Processed Repository Endpoints ---------------------


@main_routes.route('/track-processed-repo', methods=['POST'])
@cross_origin(origin='*')
def track_processed_repo():
    """Record a processed repository event with timestamp and incremented count."""
    timestamp = datetime.utcnow()
    try:
        current_count = db.processed_repo_events.count_documents({})
        new_count = current_count + 1
        db.processed_repo_events.insert_one({
            'timestamp': timestamp,
            'count': new_count
        })
        return jsonify({
            'message': 'Processed repository recorded.',
            'count': new_count,
            'timestamp': timestamp.isoformat() + 'Z'
        }), 201
    except Exception as e:
        logger.error(f"Error recording processed repository: {e}")
        return jsonify({'error': 'Failed to record processed repository.'}), 500


@main_routes.route('/processed-repo-count', methods=['GET'])
@cross_origin(origin='*')
def get_processed_repo_count():
    """Return the total number of processed repository events."""
    try:
        count = db.processed_repo_events.count_documents({})
        return jsonify({'count': count}), 200
    except Exception as e:
        logger.error(f"Error retrieving processed repository count: {e}")
        return jsonify({'error': 'Failed to retrieve processed repository count.'}), 500

# Homepage
@main_routes.route('/')
@cross_origin(origin='*')
def landing_page():
    return "Welcome to the Repository Fetcher for Apache and Eclipse foundations!"

# Redirect invalid API endpoints
@main_routes.route('/<path:invalid_path>')
def handle_invalid_path(invalid_path):
    if invalid_path.startswith('api/'):
        return jsonify({'error': 'Invalid API endpoint'}), 404
    return redirect(url_for('main_routes.landing_page'))

# Fetch all the Apache projects (combined from Apache and Github)
@main_routes.route('/api/projects', methods=['GET'])
@cross_origin(origin='*') 
def get_all_projects():
    try:
        projects = list(db.github_repositories.find({}, {'_id': 0}))
        projects = [sanitize_document(project) for project in projects]
        return jsonify({'projects': projects}), 200
    except Exception as e:
        logger.error(f"Error fetching projects from MongoDB: {e}")
        return jsonify({'error': 'Failed to fetch projects.'}), 500

# Fetch all Apache github repositories - include fetching stars, forks and watch for each repo
@main_routes.route('/api/github_stars', methods=['GET'])
def get_github_stars():
    try:
        repos = list(db.github_repositories.find({}, {'_id': 0}))
        repos = [sanitize_document(repo) for repo in repos]
        return jsonify({'repositories': repos}), 200
    except Exception as e:
        logger.error(f"Error fetching repositories from MongoDB: {e}")
        return jsonify({'error': 'Failed to fetch repositories.'}), 500

# Fetch all the repos from GitHub
@main_routes.route('/api/github_repositories', methods=['GET'])
def get_github_repositories():
    try:
        repos = list(db.github_repositories.find({}, {'_id': 0}))
        repos = [sanitize_document(repo) for repo in repos]
        return jsonify({'repositories': repos}), 200
    except Exception as e:
        logger.error(f"Error fetching repositories from MongoDB: {e}")
        return jsonify({'error': 'Failed to fetch repositories.'}), 500

# [Tested] [Currently used by Vue.js] Fetch project descriptions from Apache scraping
@main_routes.route('/api/project_description', methods=['GET'])
@cross_origin(origin='*') 
def get_project_description():
    try:
        description = list(db.apache_projects.find({}, {'_id': 0}))
        description = [sanitize_document(doc) for doc in description]
        return jsonify({'description': description}), 200
    except Exception as e:
        logger.error(f"Error fetching project descriptions from MongoDB: {e}")
        return jsonify({'error': 'Failed to fetch project descriptions.'}), 500

# [APACHE] Fetch all Apache projects project_info
@main_routes.route('/api/project_info', methods=['GET'])
@cross_origin(origin='*') 
def get_all_project_info():
    """
    Fetch all project information.
    """
    try:
        projects = list(db.project_info.find({}, {'_id': 0}))
        projects = [sanitize_document(project) for project in projects]
        return jsonify({'projects': projects}), 200
    except Exception as e:
        logger.error(f"Error fetching project_info from MongoDB: {e}")
        return jsonify({'error': 'Failed to fetch project information.'}), 500

# [ECLIPSE] Fetch all Eclipse projects project_info
# Note that this would fetch the month-wise data too
@main_routes.route('/eclipse/project_info', methods=['GET'])
@cross_origin(origin='*') 
def get_all_eclipse_project_info():
    """
    Fetch all project information.
    """
    try:
        projects = list(db.eclipse_project_info.find({}, {'_id': 0}))
        projects = [sanitize_document(project) for project in projects]
        return jsonify({'projects': projects}), 200
    except Exception as e:
        logger.error(f"Error fetching project_info from MongoDB: {e}")
        return jsonify({'error': 'Failed to fetch project information.'}), 500

# [APACHE] Fetch all Apache month ranges for a project
@main_routes.route('/api/monthly_ranges', methods=['GET'])
@cross_origin(origin='*') 
def get_all_monthly_ranges():
    """
    Fetch all monthly ranges for all projects.
    """
    try:
        projects = list(db.monthly_ranges.find({}, {'_id': 0}))
        projects = [sanitize_document(project) for project in projects]
        return jsonify({'project_ranges': projects}), 200
    except Exception as e:
        logger.error(f"Error fetching project_ranges from MongoDB: {e}")
        return jsonify({'error': 'Failed to fetch project ranges.'}), 500


# ------------------ New API Endpoint: Tech Net Data ------------------

# [APACHE]
@main_routes.route('/api/tech_net/<project_id>/<int:month>', methods=['GET'])
@cross_origin(origin='*') 
def get_tech_net(project_id, month):
    """
    Fetch technical network data for a specific project and month.
    """
    try:
        normalized_project_id = project_id.strip().lower()
        project = db.tech_net.find_one({'project_id': normalized_project_id})
        if not project:
            return jsonify({'error': f"Project '{project_id}' not found."}), 404
        
        month_str = str(month)
        if 'months' not in project or month_str not in project['months']:
            return jsonify({'error': f"Month '{month}' data not found for project '{project_id}'."}), 404
        
        data = project['months'][month_str]
        # Sanitize data if necessary (assuming data is list of lists with [string, string, number])
        sanitized_data = []
        for entry in data:
            if isinstance(entry, list) and len(entry) == 3:
                name, tech, value = entry
                sanitized_entry = [
                    name if isinstance(name, str) else '',
                    tech if isinstance(tech, str) else '',
                    value if isinstance(value, (int, float)) else 0
                ]
                sanitized_data.append(sanitized_entry)
            else:
                # Handle unexpected data formats
                sanitized_data.append(['', '', 0])
        
        return jsonify({
            'project_id': project['project_id'],
            'project_name': project['project_name'],
            'month': month,
            'data': sanitized_data
        }), 200
    except Exception as e:
        logger.error(f"Error fetching tech_net data for project '{project_id}', month '{month}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500

# [ECLIPSE]
@main_routes.route('/eclipse/tech_net/<project_id>/<int:month>', methods=['GET'])
@cross_origin(origin='*') 
def get_eclipse_tech_net(project_id, month):
    """
    Fetch technical network data for a specific project and month.
    """
    try:
        normalized_project_id = project_id.strip().lower().replace(' ','').replace('-','')
        project = db.eclipse_tech_net.find_one({'project_id': normalized_project_id})
        if not project:
            return jsonify({'error': f"Project '{project_id}' not found."}), 404
        
        month_str = str(month)
        if 'months' not in project or month_str not in project['months']:
            return jsonify({'error': f"Month '{month}' data not found for project '{project_id}'."}), 404
        
        data = project['months'][month_str]
        # Sanitize data if necessary (assuming data is list of lists with [string, string, number])
        sanitized_data = []
        for entry in data:
            if isinstance(entry, list) and len(entry) == 3:
                name, tech, value = entry
                sanitized_entry = [
                    name if isinstance(name, str) else '',
                    tech if isinstance(tech, str) else '',
                    value if isinstance(value, (int, float)) else 0
                ]
                sanitized_data.append(sanitized_entry)
            else:
                # Handle unexpected data formats
                sanitized_data.append(['', '', 0])
        
        return jsonify({
            'project_id': project['project_id'],
            'project_name': project['project_name'],
            'month': month,
            'data': sanitized_data
        }), 200
    except Exception as e:
        logger.error(f"Error fetching tech_net data for project '{project_id}', month '{month}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500

# [APACHE] This is to fetch the social network data for a specific project and month
@main_routes.route('/api/social_net/<project_id>/<int:month>', methods=['GET'])
@cross_origin(origin='*')
def get_social_net(project_id, month):
    """
    Fetch social network data for a specific project and month.
    """
    try:
        # Normalize project ID
        normalized_project_id = project_id.strip().lower()

        # Fetch project from the database
        project = db.social_net.find_one({'project_id': normalized_project_id})
        if not project:
            return jsonify({'error': f"Project '{project_id}' not found."}), 404

        # Convert the month parameter to string for key lookup
        month_str = str(month)

        # Check if the month exists in the project's "months" field
        if 'months' not in project or month_str not in project['months']:
            return jsonify({'error': f"Month '{month}' data not found for project '{project_id}'."}), 404

        # Fetch data for the specified month
        data = project['months'][month_str]

        # Sanitize the data
        sanitized_data = []
        for entry in data:
            if isinstance(entry, list) and len(entry) == 3:
                name, relation, value = entry

                # Convert the value field to an integer or float
                try:
                    value = int(value) if isinstance(value, str) and value.isdigit() else float(value)
                except ValueError:
                    logger.warning(f"Invalid value in entry: {entry}")
                    continue  # Skip this entry if value conversion fails

                sanitized_entry = [
                    name if isinstance(name, str) else '',
                    relation if isinstance(relation, str) else '',
                    value  # Use the converted numeric value
                ]
                sanitized_data.append(sanitized_entry)
            else:
                logger.warning(f"Skipping invalid entry structure: {entry}")

        # Return the processed data
        return jsonify({
            'project_id': project['project_id'],
            'project_name': project.get('project_name', 'Unknown Project'),
            'month': month,
            'data': sanitized_data
        }), 200

    except Exception as e:
        logger.error(f"Error fetching social_net data for project '{project_id}', month '{month}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500


# [ECLIPSE] This is to fetch the social network data for a specific project and month
@main_routes.route('/eclipse/social_net/<project_id>/<int:month>', methods=['GET'])
@cross_origin(origin='*')
def get_eclipse_social_net(project_id, month):
    """
    Fetch social network data for a specific project and month.
    """
    try:
        # Normalize project ID
        normalized_project_id = project_id.strip().lower().replace(' ','').replace('-','')

        # Fetch project from the database
        project = db.eclipse_social_net.find_one({'project_id': normalized_project_id})
        if not project:
            return jsonify({'error': f"Project '{project_id}' not found."}), 404

        # Convert the month parameter to string for key lookup
        month_str = str(month)

        # Check if the month exists in the project's "months" field
        if 'months' not in project or month_str not in project['months']:
            return jsonify({'error': f"Month '{month}' data not found for project '{project_id}'."}), 404

        # Fetch data for the specified month
        data = project['months'][month_str]

        # Sanitize the data
        sanitized_data = []
        for entry in data:
            if isinstance(entry, list) and len(entry) == 3:
                name, relation, value = entry

                # Convert the value field to an integer or float
                try:
                    value = int(value) if isinstance(value, str) and value.isdigit() else float(value)
                except ValueError:
                    logger.warning(f"Invalid value in entry: {entry}")
                    continue  # Skip this entry if value conversion fails

                sanitized_entry = [
                    name if isinstance(name, str) else '',
                    relation if isinstance(relation, str) else '',
                    value  # Use the converted numeric value
                ]
                sanitized_data.append(sanitized_entry)
            else:
                logger.warning(f"Skipping invalid entry structure: {entry}")

        # Return the processed data
        return jsonify({
            'project_id': project['project_id'],
            'month': month,
            'data': sanitized_data
        }), 200

    except Exception as e:
        logger.error(f"Error fetching social_net data for project '{project_id}', month '{month}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500

# This is to fetch commit links data for a particular project for a particular month
@main_routes.route('/api/commit_links/<project_id>/<int:month>', methods=['GET'])
@cross_origin(origin='*') 
def get_commit_links(project_id, month):
    """
    Fetch commit links data for a specific project and month.
    """
    try:
        normalized_project_id = project_id.strip().lower()
        project = db.commit_links.find_one({'project_id': normalized_project_id})
        if not project:
            return jsonify({'error': f"Project '{project_id}' not found."}), 404
        
        month_str = str(month)
        if 'months' not in project or month_str not in project['months']:
            return jsonify({'error': f"Month '{month}' data not found for project '{project_id}'."}), 404
        
        commits = project['months'][month_str]
        # Assuming commits is a list of dictionaries or lists; sanitize accordingly
        sanitized_commits = []
        for commit in commits:
            if isinstance(commit, dict):
                sanitized_commit = sanitize_document(commit)
                sanitized_commits.append(sanitized_commit)
            elif isinstance(commit, list):
                # Example: [commit_id, author, message]
                sanitized_commit = [
                    commit[0] if len(commit) > 0 and isinstance(commit[0], str) else '',
                    commit[1] if len(commit) > 1 and isinstance(commit[1], str) else '',
                    commit[2] if len(commit) > 2 and isinstance(commit[2], str) else ''
                ]
                sanitized_commits.append(sanitized_commit)
            else:
                sanitized_commits.append({})
        
        return jsonify({
            'project_id': project['project_id'],
            'project_name': project['project_name'],
            'month': month,
            'commits': sanitized_commits
        }), 200
    except Exception as e:
        logger.error(f"Error fetching commit_links data for project '{project_id}', month '{month}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500

# Locally processed repos: the per-developer commit/issue links behind the
# network node drilldowns. Served on demand rather than bundled into the
# pipeline response -- gem5's table alone is 4 MB, which would be paid on every
# dashboard load whether or not anyone clicks a node.
def _local_links(collection, project_id, month, key):
    # The dashboard identifies a locally processed repo as "local_<repo name>"
    # while the pipeline stores it under generate_project_id(repo name), i.e.
    # alphanumerics only, lowercased. Accept either.
    raw = (project_id or '').strip()
    if raw.lower().startswith('local_'):
        raw = raw[len('local_'):]
    normalized = ''.join(c for c in raw if c.isalnum()).lower()
    project = collection.find_one({'project_id': normalized})
    if not project:
        return jsonify({'error': f"Project '{project_id}' not found."}), 404
    entries = (project.get('months') or {}).get(str(month))
    if entries is None:
        return jsonify({'error': f"Month '{month}' data not found for project '{project_id}'."}), 404
    return jsonify({
        'project_id': project['project_id'],
        'project_name': project.get('project_name', ''),
        'month': month,
        key: [sanitize_document(e) for e in entries if isinstance(e, dict)],
    }), 200


@main_routes.route('/api/local_commit_links/<project_id>/<int:month>', methods=['GET'])
@cross_origin(origin='*')
def get_local_commit_links(project_id, month):
    try:
        return _local_links(db.local_commit_links, project_id, month, 'commits')
    except Exception as e:
        logger.error(f"Error fetching local commit links for '{project_id}'/{month}: {e}")
        return jsonify({'error': 'Internal server error.'}), 500


@main_routes.route('/api/local_issue_links/<project_id>/<int:month>', methods=['GET'])
@cross_origin(origin='*')
def get_local_issue_links(project_id, month):
    try:
        # keyed 'commits' because the frontend parses both link tables the same way
        return _local_links(db.local_issue_links, project_id, month, 'commits')
    except Exception as e:
        logger.error(f"Error fetching local issue links for '{project_id}'/{month}: {e}")
        return jsonify({'error': 'Internal server error.'}), 500


# [ECLIPSE] This is to fetch commit links data for a particular project for a particular month
@main_routes.route('/eclipse/commit_links/<project_id>/<int:month>', methods=['GET'])
@cross_origin(origin='*') 
def get_eclipse_commit_links(project_id, month):
    """
    Fetch commit links data for a specific project and month.
    """
    try:
        normalized_project_id = project_id.strip().lower()
        project = db.eclipse_commit_links.find_one({'project_id': normalized_project_id})
        if not project:
            return jsonify({'error': f"Project '{project_id}' not found."}), 404
        
        month_str = str(month)
        if 'months' not in project or month_str not in project['months']:
            return jsonify({'error': f"Month '{month}' data not found for project '{project_id}'."}), 404
        
        commits = project['months'][month_str]
        # Assuming commits is a list of dictionaries or lists; sanitize accordingly
        sanitized_commits = []
        for commit in commits:
            if isinstance(commit, dict):
                sanitized_commit = sanitize_document(commit)
                sanitized_commits.append(sanitized_commit)
            elif isinstance(commit, list):
                # Example: [commit_id, author, message]
                sanitized_commit = [
                    commit[0] if len(commit) > 0 and isinstance(commit[0], str) else '',
                    commit[1] if len(commit) > 1 and isinstance(commit[1], str) else '',
                    commit[2] if len(commit) > 2 and isinstance(commit[2], str) else ''
                ]
                sanitized_commits.append(sanitized_commit)
            else:
                sanitized_commits.append({})
        
        return jsonify({
            'project_id': project['project_id'],
            'month': month,
            'commits': sanitized_commits
        }), 200
    except Exception as e:
        logger.error(f"Error fetching commit_links data for project '{project_id}', month '{month}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500

# [APACHE] This is to fetch email links data for a particular project for a particular month
@main_routes.route('/api/email_links/<project_id>/<int:month>', methods=['GET'])
@cross_origin(origin='*') 
def get_email_links(project_id, month):
    """
    Fetch email links data for a specific project and month.
    """
    try:
        normalized_project_id = project_id.strip().lower()
        project = db.email_links.find_one({'project_id': normalized_project_id})
        if not project:
            return jsonify({'error': f"Project '{project_id}' not found."}), 404
        
        month_str = str(month)
        if 'months' not in project or month_str not in project['months']:
            return jsonify({'error': f"Month '{month}' data not found for project '{project_id}'."}), 404
        
        commits = project['months'][month_str]
        # Assuming commits is a list of dictionaries or lists; sanitize accordingly
        sanitized_commits = []
        for commit in commits:
            if isinstance(commit, dict):
                sanitized_commit = sanitize_document(commit)
                sanitized_commits.append(sanitized_commit)
            elif isinstance(commit, list):
                # Example: [email, relation, count]
                sanitized_commit = [
                    commit[0] if len(commit) > 0 and isinstance(commit[0], str) else '',
                    commit[1] if len(commit) > 1 and isinstance(commit[1], str) else '',
                    commit[2] if len(commit) > 2 and isinstance(commit[2], (int, float)) else 0
                ]
                sanitized_commits.append(sanitized_commit)
            else:
                sanitized_commits.append({})
        
        return jsonify({
            'project_id': project['project_id'],
            'project_name': project['project_name'],
            'month': month,
            'commits': sanitized_commits
        }), 200
    except Exception as e:
        logger.error(f"Error fetching email_links data for project '{project_id}', month '{month}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500

# [ECLIPSE] This is to fetch email links data for a particular project for a particular month
@main_routes.route('/eclipse/email_links/<project_id>/<int:month>', methods=['GET'])
@cross_origin(origin='*') 
def get_eclipse_email_links(project_id, month):
    """
    Fetch email links data for a specific project and month.
    """
    try:
        normalized_project_id = project_id.strip().lower()
        project = db.eclipse_email_links.find_one({'project_id': normalized_project_id})
        if not project:
            return jsonify({'error': f"Project '{project_id}' not found."}), 404
        
        month_str = str(month)
        if 'months' not in project or month_str not in project['months']:
            return jsonify({'error': f"Month '{month}' data not found for project '{project_id}'."}), 404
        
        commits = project['months'][month_str]
        # Assuming commits is a list of dictionaries or lists; sanitize accordingly
        sanitized_commits = []
        for commit in commits:
            if isinstance(commit, dict):
                sanitized_commit = sanitize_document(commit)
                sanitized_commits.append(sanitized_commit)
            elif isinstance(commit, list):
                # Example: [email, relation, count]
                sanitized_commit = [
                    commit[0] if len(commit) > 0 and isinstance(commit[0], str) else '',
                    commit[1] if len(commit) > 1 and isinstance(commit[1], str) else '',
                    commit[2] if len(commit) > 2 and isinstance(commit[2], (int, float)) else 0
                ]
                sanitized_commits.append(sanitized_commit)
            else:
                sanitized_commits.append({})
        
        return jsonify({
            'project_id': project['project_id'],
            'month': month,
            'commits': sanitized_commits
        }), 200
    except Exception as e:
        logger.error(f"Error fetching email_links data for project '{project_id}', month '{month}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500

# [APACHE] Fetch project_info for a specific project_id
@main_routes.route('/api/project_info/<project_id>', methods=['GET'])
@cross_origin(origin='*') 
def get_project_info_api(project_id):
    """
    Fetch combined project information for a specific project.
    """
    try:
        normalized_project_id = project_id.strip().lower()
        project = db.project_info.find_one({'project_id': normalized_project_id})
        if not project:
            return jsonify({'error': f"Project '{project_id}' not found."}), 404
        
        # Remove MongoDB's _id field and sanitize
        project = sanitize_document(project)
        project.pop('_id', None)
        return jsonify(project), 200
    except Exception as e:
        logger.error(f"Error fetching project_info for project '{project_id}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500


# [APACHE] Fetch commit_measures for projects month-wise
@main_routes.route('/api/commit_measure/<project_id>/<int:month>', methods=['GET'])
@cross_origin(origin='*') 
def get_commit_measure(project_id, month):
    """
    Fetch commit measure data for a specific project and month.
    """
    try:
        normalized_project_id = project_id.strip().lower()
        project = db.commit_measure.find_one({'project_id': normalized_project_id})
        if not project:
            return jsonify({'error': f"Project '{project_id}' not found."}), 404
        
        month_str = str(month)
        if 'months' not in project or month_str not in project['months']:
            return jsonify({'error': f"Month '{month}' data not found for project '{project_id}'."}), 404
        
        data = project['months'][month_str]
        # Directly return the data without processing into a list
        return jsonify({
            'project_id': project['project_id'],
            'project_name': project['project_name'],
            'month': month,
            'data': data  # Ensure 'data' is a dictionary/object
        }), 200
    except Exception as e:
        logger.error(f"Error fetching commit_measure data for project '{project_id}', month '{month}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500

# [ECLIPSE] Fetch commit_measures for projects month-wise
@main_routes.route('/eclipse/commit_measure/<project_id>/<int:month>', methods=['GET'])
@cross_origin(origin='*') 
def get_eclipse_commit_measure(project_id, month):
    """
    Fetch commit measure data for a specific project and month.
    """
    try:
        normalized_project_id = project_id.strip().lower().replace(' ','').replace('-','')
        project = db.eclipse_commit_measure.find_one({'project_id': normalized_project_id})
        if not project:
            return jsonify({'error': f"Project '{project_id}' not found."}), 404
        
        month_str = str(month)
        if 'months' not in project or month_str not in project['months']:
            return jsonify({'error': f"Month '{month}' data not found for project '{project_id}'."}), 404
        
        data = project['months'][month_str]
        # Directly return the data without processing into a list
        return jsonify({
            'project_id': project['project_id'],
            'month': month,
            'data': data  # Ensure 'data' is a dictionary/object
        }), 200
    except Exception as e:
        logger.error(f"Error fetching commit_measure data for project '{project_id}', month '{month}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500


# [APACHE] This is to fetch the emails measure data for a month and project   
@main_routes.route('/api/email_measure/<project_id>/<int:month>', methods=['GET'])
@cross_origin(origin='*') 
def get_email_measure(project_id, month):
    """
    Fetch email measure data for a specific project and month.
    """
    try:
        normalized_project_id = project_id.strip().lower()
        project = db.email_measure.find_one({'project_id': normalized_project_id})
        if not project:
            return jsonify({'error': f"Project '{project_id}' not found."}), 404
        
        month_str = str(month)
        if 'months' not in project or month_str not in project['months']:
            return jsonify({'error': f"Month '{month}' data not found for project '{project_id}'."}), 404
        
        data = project['months'][month_str]
       # Directly return the data without processing into a list
        return jsonify({
            'project_id': project['project_id'],
            'project_name': project['project_name'],
            'month': month,
            'data': data  # Ensure 'data' is a dictionary/object
        }), 200 
    except Exception as e:
        logger.error(f"Error fetching email_measure data for project '{project_id}', month '{month}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500


# [ECLIPSE] This is to fetch the emails measure data for a month and project   
@main_routes.route('/eclipse/email_measure/<project_id>/<int:month>', methods=['GET'])
@cross_origin(origin='*') 
def get_eclipse_email_measure(project_id, month):
    """
    Fetch email measure data for a specific project and month.
    """
    try:
        normalized_project_id = project_id.strip().lower().replace(' ','').replace('-','')
        project = db.eclipse_email_measure.find_one({'project_id': normalized_project_id})
        if not project:
            return jsonify({'error': f"Project '{project_id}' not found."}), 404
        
        month_str = str(month)
        if 'months' not in project or month_str not in project['months']:
            return jsonify({'error': f"Month '{month}' data not found for project '{project_id}'."}), 404
        
        data = project['months'][month_str]
       # Directly return the data without processing into a list
        return jsonify({
            'project_id': project['project_id'],
            'month': month,
            'data': data  # Ensure 'data' is a dictionary/object
        }), 200 
    except Exception as e:
        logger.error(f"Error fetching email_measure data for project '{project_id}', month '{month}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500


# [Discuss] [ECLIPSE] This is to fetch the issues measure data for a month and project - removing email as of now
@main_routes.route('/eclipse/issue_measure/<project_id>/<int:month>', methods=['GET'])
@cross_origin(origin='*') 
def get_eclipse_issue_measure(project_id, month):
    """
    Fetch email measure data for a specific project and month.
    """
    try:
        normalized_project_id = project_id.strip().lower().replace(' ','').replace('-','')
        project = db.eclipse_issue_measure.find_one({'project_id': normalized_project_id})
        if not project:
            return jsonify({'error': f"Project '{project_id}' not found."}), 404
        
        month_str = str(month)
        if 'months' not in project or month_str not in project['months']:
            return jsonify({'error': f"Month '{month}' data not found for project '{project_id}'."}), 404
        
        data = project['months'][month_str]
       # Directly return the data without processing into a list
        return jsonify({
            'project_id': project['project_id'],
            'month': month,
            'data': data  # Ensure 'data' is a dictionary/object
        }), 200 
    except Exception as e:
        logger.error(f"Error fetching email_measure data for project '{project_id}', month '{month}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500



# Shared by both foundation grad_forecast endpoints. The forecaster saturates at
# 1.0 for months on end, so the stored probability is re-expressed against the
# project's own commit/issue activity before it reaches the chart; see
# app/pipeline/calibration.py. The response shape is unchanged -- each entry
# keeps its 'date'/'month' and 'close' keys, only 'close' moves.
def _calibrated_forecast(forecast, tech_collection, social_collection, project_id):
    """Return ``forecast`` with each entry's 'close' calibrated.

    Best effort: if the activity data is missing or malformed the original
    forecast is returned untouched rather than failing the request.
    """
    try:
        raw = {}
        for key, entry in forecast.items():
            month = entry.get('date', entry.get('month')) if isinstance(entry, dict) else None
            if month is None:
                month = key
            try:
                raw[int(month)] = float(entry['close'] if isinstance(entry, dict) else entry)
            except (TypeError, ValueError, KeyError):
                continue
        if not raw:
            return forecast

        tech = tech_collection.find_one({'project_id': project_id}, {'months': 1, '_id': 0}) or {}
        social = social_collection.find_one({'project_id': project_id}, {'months': 1, '_id': 0}) or {}
        calibrated = calibrate(raw, tech.get('months'), social.get('months'))

        out = {}
        for key, entry in forecast.items():
            if not isinstance(entry, dict):
                out[key] = entry
                continue
            month = entry.get('date', entry.get('month', key))
            try:
                month = int(month)
            except (TypeError, ValueError):
                out[key] = entry
                continue
            out[key] = {**entry, 'close': calibrated.get(month, entry.get('close'))}
        return out
    except Exception as e:
        logger.error(f"Forecast calibration failed for '{project_id}': {e}")
        return forecast


# [APACHE] Fetch grad_forecast for a specific project_id
@main_routes.route('/api/grad_forecast/<project_id>', methods=['GET'])
@cross_origin(origin='*') 
def get_grad_forecast_api(project_id):
    """
    Fetch forecast data for a specific project.
    """
    try:
        normalized_project_id = project_id.strip().lower()
        project = db.grad_forecast.find_one({'project_id': normalized_project_id}, {'forecast': 1, '_id': 0})
        if not project or 'forecast' not in project:
            return jsonify({'error': f"Forecast data for project '{project_id}' not found."}), 404
        
        # Return only the forecast data
        return jsonify(_calibrated_forecast(
            project['forecast'], db.tech_net, db.social_net,
            normalized_project_id)), 200
    except Exception as e:
        logger.error(f"Error fetching forecast data for project '{project_id}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500

# [ECLIPSE] Fetch grad_forecast for a specific project_id
@main_routes.route('/eclipse/grad_forecast/<project_id>', methods=['GET'])
@cross_origin(origin='*') 
def get_eclipse_grad_forecast_api(project_id):
    """
    Fetch forecast data for a specific project.
    """
    try:
        normalized_project_id = project_id.strip().lower().replace(' ','').replace('-','')
        project = db.eclipse_grad_forecast.find_one({'project_id': normalized_project_id}, {'forecast': 1, '_id': 0})
        if not project or 'forecast' not in project:
            return jsonify({'error': f"Forecast data for project '{project_id}' not found."}), 404
        
        # Return only the forecast data
        return jsonify(_calibrated_forecast(
            project['forecast'], db.eclipse_tech_net, db.eclipse_social_net,
            normalized_project_id)), 200
    except Exception as e:
        logger.error(f"Error fetching forecast data for project '{project_id}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500

# [APACHE] New feature, this is for displaying the month-wise predictions
@main_routes.route('/api/predictions/<project_id>/<int:month>', methods=['GET'])
def get_predictions_api(project_id, month):
    """
    GET /api/predictions/<project_id>/<int:month>
    Returns adjusted forecasts for the next three months based on the selected month's value.
    Ensures that close values remain between 0 and 1 with a reduced adjustment factor.
    """
    try:
        normalized_project_id = project_id.strip().lower()
        project = db.grad_forecast.find_one({'project_id': normalized_project_id}, {'forecast': 1, 'project_name': 1, '_id': 0})
        if not project or 'forecast' not in project:
            return jsonify({'error': f"Project '{project_id}' not found."}), 404

        forecast = project.get('forecast', {})
        month_str = str(month)
        if month_str not in forecast:
            return jsonify({'error': f"Forecast data for month '{month}' not found for project '{project_id}'."}), 404

        current_close = forecast[month_str]['close']

        # Determine adjustment factor (reduced from 5% to 3%)
        adjustment_factor = 1.03 if current_close > 0.5 else 0.97  # Increase by 3% if > 0.5, else decrease by 3%

        # Adjust the next three months
        adjusted_forecast = {}
        for i in range(1, 4):
            next_month = month + i
            next_month_str = str(next_month)
            if next_month_str in forecast:
                original_close = forecast[next_month_str]['close']
                adjusted_close = original_close * adjustment_factor
                # Ensure the adjusted_close is between 0 and 1
                adjusted_close = min(max(adjusted_close, 0), 1)
                adjusted_close = round(adjusted_close, 4)
                adjusted_forecast[next_month_str] = {
                    "date": next_month,
                    "close": adjusted_close
                }
            else:
                # Handle missing months if necessary
                logger.warning(f"Forecast data for month '{next_month}' is missing for project '{project_id}'.")
                continue

        return jsonify({
            'project_id': project_id,
            'month': month,
            'adjusted_forecast': adjusted_forecast
        }), 200

    except Exception as e:
        logger.error(f"Error fetching predictions for project '{project_id}', month '{month}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500

# [ECLIPSE] New feature, this is for displaying the month-wise predictions
@main_routes.route('/eclipse/predictions/<project_id>/<int:month>', methods=['GET'])
def get_eclipse_predictions_api(project_id, month):
    """
    GET /api/predictions/<project_id>/<int:month>
    Returns adjusted forecasts for the next three months based on the selected month's value.
    Ensures that close values remain between 0 and 1 with a reduced adjustment factor.
    """
    try:
        normalized_project_id = project_id.strip().lower()
        project = db.eclipse_grad_forecast.find_one({'project_id': normalized_project_id}, {'forecast': 1, 'project_name': 1, '_id': 0})
        if not project or 'forecast' not in project:
            return jsonify({'error': f"Project '{project_id}' not found."}), 404

        forecast = project.get('forecast', {})
        month_str = str(month)
        if month_str not in forecast:
            return jsonify({'error': f"Forecast data for month '{month}' not found for project '{project_id}'."}), 404

        current_close = forecast[month_str]['close']

        # Determine adjustment factor (reduced from 5% to 3%)
        adjustment_factor = 1.03 if current_close > 0.5 else 0.97  # Increase by 3% if > 0.5, else decrease by 3%

        # Adjust the next three months
        adjusted_forecast = {}
        for i in range(1, 4):
            next_month = month + i
            next_month_str = str(next_month)
            if next_month_str in forecast:
                original_close = forecast[next_month_str]['close']
                adjusted_close = original_close * adjustment_factor
                # Ensure the adjusted_close is between 0 and 1
                adjusted_close = min(max(adjusted_close, 0), 1)
                adjusted_close = round(adjusted_close, 4)
                adjusted_forecast[next_month_str] = {
                    "date": next_month,
                    "close": adjusted_close
                }
            else:
                # Handle missing months if necessary
                logger.warning(f"Forecast data for month '{next_month}' is missing for project '{project_id}'.")
                continue

        return jsonify({
            'project_id': project_id,
            'month': month,
            'adjusted_forecast': adjusted_forecast
        }), 200

    except Exception as e:
        logger.error(f"Error fetching predictions for project '{project_id}', month '{month}': {e}")
        return jsonify({'error': 'Internal server error.'}), 500


# A GitHub repo URL is exactly https://github.com/<owner>/<repo> with an
# optional .git suffix -- no extra path segments, no second URL glued on.
GITHUB_REPO_URL_RE = re.compile(
    r'^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(?:\.git)?/?$',
    re.IGNORECASE,
)


## Scrape repository independently
@main_routes.route('/api/scrape_repository', methods=['POST'])
@cross_origin(origin='*')
def scrape_repository():
    """Trigger the Rust scraper for a GitHub repository and persist the results."""
    data = request.get_json(silent=True) or {}
    github_link = data.get('github_link') or data.get('github_repo')

    if not github_link:
        return jsonify({'error': 'github_link is required.'}), 400

    result = run_rust_code(github_link, 0) # 0 means the purpose of this scraping is not related to OSSPREY

    status_code = 200
    if not isinstance(result, dict) or result.get('error'):
        status_code = 500

    return jsonify(result), status_code
    
# [LOCAL GIT]
@main_routes.route('/api/upload_git_link', methods=['POST'])
@cross_origin(origin='*')
def upload_git_link():
    """
    Receives a .git link from the frontend and queues it for processing.

    Processing is bounded to a fixed number of concurrent jobs; any extra
    requests wait in a FIFO queue. Instead of blocking until the pipeline
    finishes, this returns a job handle immediately (HTTP 202). The client polls
    ``GET /api/queue_status/<job_id>`` to track progress and retrieve the result.
    """
    try:
        data = request.get_json(silent=True) or {}
        git_link = data.get('git_link', '').strip()
        if not git_link:
            return jsonify({'error': 'No git link provided.'}), 400
        if not git_link.lower().endswith('.git'):
            return jsonify({'error': 'Provided URL is not a valid .git link.'}), 400
        # Must be exactly one owner/repo path. Without this, a mangled value such
        # as two concatenated URLs still ends in ".git", and the pipeline (which
        # takes the LAST path segment as the project) happily analyses a
        # different repository and reports success for it.
        if not GITHUB_REPO_URL_RE.match(git_link):
            return jsonify({
                'error': 'Provided URL is not a valid GitHub repository link. '
                         'Expected https://github.com/<owner>/<repo>.git'
            }), 400

        logging.info(f"Queueing .git link: {git_link}")
        job = pipeline_queue.submit(git_link, metadata={'git_link': git_link})
        return jsonify(job), 202
    except Exception as e:
        logging.error(f"Error queueing git link: {e}")
        return jsonify({'error': 'Internal server error.'}), 500


@main_routes.route('/api/queue_status/<job_id>', methods=['GET'])
@cross_origin(origin='*')
def queue_status(job_id):
    """Return the status of a queued/running/finished pipeline job.

    The response includes ``status`` (queued|running|completed|failed|cancelled),
    ``position`` in the queue, ``estimated_wait_seconds`` and, once the job is
    finished, the pipeline ``result`` payload.
    """
    job = pipeline_queue.get_status(job_id, include_result=True)
    if job is None:
        return jsonify({'error': 'Job not found.'}), 404
    return jsonify(job), 200


@main_routes.route('/api/queue_stats', methods=['GET'])
@cross_origin(origin='*')
def queue_stats():
    """Return aggregate queue statistics (running, queued, capacity)."""
    return jsonify(pipeline_queue.stats()), 200


@main_routes.route('/api/repo_jobs', methods=['GET'])
@cross_origin(origin='*')
def repo_jobs():
    """List pipeline jobs for the "All Repos" page.

    Returns two lists:
      * ``pending``   - jobs still queued or running, taken from the live
        in-memory queue so estimates and statuses are current. Each entry has
        ``repo_name``, ``status``, ``created_at`` (ISO-8601 UTC) and
        ``estimated_seconds`` until completion.
      * ``processed`` - jobs that reached a terminal state
        (completed|failed|cancelled), read from the MongoDB job history so
        they survive backend restarts. Each entry has ``repo_name``,
        ``status``, ``created_at`` and ``finished_at``.
    """
    try:
        pending = []
        for snap in pipeline_queue.list_jobs(statuses=(STATUS_QUEUED, STATUS_RUNNING)):
            git_link = (snap.get('metadata') or {}).get('git_link', '')
            pending.append({
                'job_id': snap['job_id'],
                'repo_name': _repo_name_from_git_link(git_link) or git_link,
                'git_link': git_link,
                'status': snap['status'],
                'created_at': snap['created_at'],
                'estimated_seconds': snap['estimated_wait_seconds'],
            })

        processed = list(
            db.repo_jobs.find(
                {'status': {'$in': [STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED]}},
                {'_id': 0, 'job_id': 1, 'repo_name': 1, 'git_link': 1, 'status': 1,
                 'created_at': 1, 'started_at': 1, 'finished_at': 1, 'error': 1},
            ).sort('created_at', -1).limit(200)
        )

        return jsonify({'pending': pending, 'processed': processed}), 200
    except Exception:
        logger.exception('Error listing repo jobs')
        return jsonify({'error': 'Internal server error.'}), 500


@main_routes.route('/api/cancel_job/<job_id>', methods=['POST'])
@cross_origin(origin='*')
def cancel_job(job_id):
    """Cancel a job that has not started running yet."""
    job = pipeline_queue.cancel(job_id)
    if job is None:
        return jsonify({'error': 'Job not found.'}), 404
    return jsonify(job), 200