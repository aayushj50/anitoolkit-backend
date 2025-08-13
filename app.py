from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
import shutil

# Import your existing processing functions
# adapt these imports to your real function names
from utils.anime_franchise_tree import generate_franchise_tree
from utils.check_missing_anime import check_missing_anime
from utils.sort_plan_to_watch import sort_plan_to_watch

app = Flask(__name__, static_folder="static")
CORS(app)

UPLOAD_FOLDER = "uploads"
REPORT_FOLDER = "reports"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(REPORT_FOLDER, exist_ok=True)

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

    # Clean previous reports
    for f in os.listdir(REPORT_FOLDER):
        path = os.path.join(REPORT_FOLDER, f)
        if os.path.isfile(path):
            os.remove(path)

    # Run your original processing scripts here
    # These functions should generate HTML reports in REPORT_FOLDER
    generate_franchise_tree(file_path, REPORT_FOLDER)  
    check_missing_anime(file_path, REPORT_FOLDER)      
    sort_plan_to_watch(file_path, REPORT_FOLDER)       

    # List generated reports
    reports = [f"/reports/{f}" for f in os.listdir(REPORT_FOLDER) if f.endswith(".html")]

    return jsonify({
        "status": "success",
        "file": file.filename,
        "report_urls": [request.host_url.strip("/") + url for url in reports]
    })

# Serve generated reports
@app.route("/reports/<path:filename>")
def serve_report(filename):
    return send_from_directory(REPORT_FOLDER, filename)

# Serve static image assets
@app.route("/static/<path:filename>")
def serve_static_files(filename):
    return send_from_directory(app.static_folder, filename)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
