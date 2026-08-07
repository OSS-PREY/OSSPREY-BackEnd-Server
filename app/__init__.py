from flask import Flask
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from dotenv import load_dotenv
from datetime import timedelta
import os

def create_app():
    load_dotenv()

    app = Flask(__name__)
    
    # Enable CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # Load configuration
    app.config.from_object('app.config.Config')

    # Load JWT secret key from environment
    jwt_secret = os.environ.get("JWT_SECRET_KEY")
    if not jwt_secret:
        print("WARNING: JWT_SECRET_KEY not set in environment. Using default for development.")
        jwt_secret = "dev-secret-key"
    app.config["JWT_SECRET_KEY"] = jwt_secret

    # The default is 15 minutes, which expires mid-session for anyone who stays
    # active longer than that and then saves their profile. The real session
    # boundary is the front-end's 5-minute inactivity logout, so a long-lived
    # access token does not widen the window a stolen token is useful for.
    app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=12)
    
    # Initialize JWTManager
    jwt = JWTManager(app)

    # Register blueprints
    from app.routes import main_routes
    app.register_blueprint(main_routes)

    # Import and register your new auth blueprint
    from app.auth_routes import auth_bp
    app.register_blueprint(auth_bp)

    # Chat widget -> RepoWise adapter (/api/chat/*).
    from app.repowise import repowise_bp
    app.register_blueprint(repowise_bp)

    return app