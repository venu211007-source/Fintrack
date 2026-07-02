import sys
import os

# Add your project folder to the path
path = os.path.dirname(os.path.abspath(__file__))
if path not in sys.path:
    sys.path.insert(0, path)

# Create tables on first run
from app import app, db
with app.app_context():
    db.create_all()

application = app
