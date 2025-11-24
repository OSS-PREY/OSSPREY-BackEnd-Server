from flask import Flask
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from dotenv import load_dotenv
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
    
    # Initialize JWTManager
    jwt = JWTManager(app)

    # Register blueprints
    from app.routes import main_routes
    app.register_blueprint(main_routes)

    # Import and register your new auth blueprint
    from app.auth_routes import auth_bp
    app.register_blueprint(auth_bp)

    return app