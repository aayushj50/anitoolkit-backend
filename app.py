from flask import Flask, request, jsonify
from flask_cors import CORS
import os

# Import your existing processing functions here
# from utils.anime_franchise_tree import process_tree

app = Flask(__name__)
CORS(app)  # Allow frontend from GitHub Pages to call this API

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route("/")
def home():
    return jsonify({"message": "AniToolKit Backend is running!"})

@app.route("/api/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    file = request.files['file']
    file_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(file_path)

    # Here you can call your existing MAL XML parsing functions
    # results = process_tree(file_path)

    return jsonify({"status": "success", "file": file.filename})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
