# src/routes.py

import math
from flask import Blueprint, jsonify, redirect, request, url_for
from flask_cors import cross_origin
from app.config import Config
from pymongo import MongoClient
import logging
from app.pipeline.orchestrator import run_pipeline
from app.pipeline.run_pex import run_forecast
from app.pipeline.rust_runner import run_rust_code
from app.pipeline.update_pex import update_pex_generator
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import requests
import json

main_routes = Blueprint('main_routes', __name__)

# Initialize MongoDB client
mongo_client = MongoClient(Config.MONGODB_URI)
db = mongo_client[Config.MONGODB_DB_NAME]

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
        return jsonify({'error': 'All fields are required.'}), 400

    email = data.get('email', '').strip().lower()
    data['email'] = email
    if db.users.find_one({'email': email}):
        return jsonify({'error': 'User is Already Registered!'}), 400

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
        return jsonify({'error': 'Email and password are required.'}), 400

    user = db.users.find_one({'email': email})
    if not user or not check_password_hash(user.get('password_hash', ''), password):
        return jsonify({'error': 'Invalid email or password.'}), 401

    return jsonify({'message': 'Login successful.'}), 200


@main_routes.route('/api/track_login', methods=['POST'])
@cross_origin(origin='*')
def track_login():
    """Record a user's login event."""
    data = request.get_json(silent=True) or {}
    user_email = data.get('user_email')
    if not user_email:
        return jsonify({'error': 'user_email is required.'}), 400

    record = {
        'user_email': user_email,
        'timestamp': datetime.utcnow()
    }

    try:
        db.login_tracking.insert_one(record)
        return jsonify({'message': 'Login tracked.'}), 201
    except Exception as e:
        logger.error(f"Error recording login for {user_email}: {e}")
        return jsonify({'error': 'Failed to track login.'}), 500


@main_routes.route('/api/track_logout', methods=['POST'])
@cross_origin(origin='*')
def track_logout():
    """Record a user's logout event."""
    data = request.get_json(silent=True) or {}
    user_email = data.get('user_email')
    if not user_email:
        return jsonify({'error': 'user_email is required.'}), 400

    record = {
        'user_email': user_email,
        'timestamp': datetime.utcnow()
    }

    try:
        db.logout_tracking.insert_one(record)
        return jsonify({'message': 'Logout tracked.'}), 201
    except Exception as e:
        logger.error(f"Error recording logout for {user_email}: {e}")
        return jsonify({'error': 'Failed to track logout.'}), 500


@main_routes.route('/api/process_repo', methods=['POST'])
@cross_origin(origin='*')
def process_repo():
    """Record a repository processing request."""
    data = request.get_json(silent=True) or {}
    user_email = data.get('user_email')
    github_repo = data.get('github_repo')
    timestamp = data.get('timestamp')

    if not user_email or not github_repo or not timestamp:
        return jsonify({'error': 'user_email, github_repo, and timestamp are required.'}), 400

    try:
        timestamp_dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid timestamp format.'}), 400

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
        return jsonify({'error': 'Failed to record request.'}), 500

# --------------------- User Data Retrieval Endpoints ---------------------


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

# ------------------------- Chatbot LLM Endpoints -------------------------

@main_routes.route('/api/health', methods=['GET'])
@cross_origin(origin='*')
def health_check():
    """
    Health check endpoint to verify the API is running.
    """
    return jsonify({'status': 'ok'}), 200


@main_routes.route('/api/chat', methods=['POST'])
@cross_origin(origin='*')
def chat_with_llm():
    """
    Chat endpoint that processes user messages using Llama 3.2 1B via Ollama.
    Accepts a message and optional repoName for context.
    """
    try:
        data = request.get_json(silent=True) or {}
        user_message = data.get('message', '').strip()
        repo_name = data.get('repoName', '').strip()

        if not user_message:
            return jsonify({'error': 'Message is required.'}), 400

        # Prepare the prompt with context if repo name is provided
        if repo_name:
            prompt = f"Context: User is working on GitHub repository '{repo_name}'.\n\nUser question: {user_message}\n\nAssistant:"
        else:
            prompt = user_message

        # Call Ollama API
        ollama_url = "http://localhost:11434/api/generate"
        payload = {
            "model": "llama3.2:1b",
            "prompt": prompt,
            "stream": False
        }

        logger.info(f"Sending request to Ollama for message: {user_message[:50]}...")

        try:
            response = requests.post(
                ollama_url,
                json=payload,
                timeout=30
            )
            response.raise_for_status()

            result = response.json()
            llm_response = result.get('response', '').strip()

            if not llm_response:
                logger.warning("Ollama returned empty response")
                return jsonify({'error': 'Model returned empty response.'}), 500

            logger.info(f"Successfully received response from Ollama")

            return jsonify({
                'response': llm_response,
                'timestamp': datetime.utcnow().isoformat()
            }), 200

        except requests.exceptions.ConnectionError:
            logger.error("Cannot connect to Ollama service")
            return jsonify({'error': 'Model unavailable. Please ensure Ollama is running.'}), 500
        except requests.exceptions.Timeout:
            logger.error("Ollama request timed out")
            return jsonify({'error': 'Request timed out. Please try again.'}), 500
        except requests.exceptions.RequestException as e:
            logger.error(f"Ollama request failed: {e}")
            return jsonify({'error': 'Model unavailable. Please try again later.'}), 500

    except Exception as e:
        logger.error(f"Error in chat endpoint: {e}")
        return jsonify({'error': 'Failed to process message.'}), 500

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
        return jsonify(project['forecast']), 200
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
        return jsonify(project['forecast']), 200
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
    Receives a .git link from the frontend and triggers the pipeline.
    """
    try:
        data = request.get_json()
        git_link = data.get('git_link', '').strip()
        if not git_link:
            return jsonify({'error': 'No git link provided.'}), 400
        if not git_link.lower().endswith('.git'):
            return jsonify({'error': 'Provided URL is not a valid .git link.'}), 400

        logging.info(f"Received .git link: {git_link}")
        pipeline_result = run_pipeline(git_link)
        return jsonify(pipeline_result), 200
    except Exception as e:
        logging.error(f"Error processing git link: {e}")
        return jsonify({'error': 'Internal server error.'}), 500
