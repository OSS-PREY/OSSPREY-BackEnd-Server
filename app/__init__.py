from flask import Flask
from flask_cors import CORS
from flask_jwt_extended import JWTManager  # <-- ADD
from dotenv import load_dotenv             # <-- ADD
import os                                  # <-- ADD

def create_app():
    load_dotenv()  # <-- ADD (Loads .env variables)

    app = Flask(__name__)
    
    # Enable CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}}) # <-- MODIFIED
    
    # Load configuration
    app.config.from_object('app.config.Config')

    # --- ADD THIS BLOCK ---
    # Load JWT secret key from environment
    app.config["JWT_SECRET_KEY"] = os.environ.get("JWT_SECRET_KEY")
    # Initialize JWTManager
    jwt = JWTManager(app)
    # ----------------------

    # Register blueprints
    from app.routes import main_routes
    app.register_blueprint(main_routes)

    # --- ADD THIS BLOCK ---
    # Import and register your new auth blueprint
    from app.auth_routes import auth_bp
    app.register_blueprint(auth_bp)
    # ----------------------

    return app