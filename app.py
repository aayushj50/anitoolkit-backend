from flask import Flask, request, jsonify
from flask_cors import CORS
import os

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Import your processing functions here
# from utils.anime_franchise_tree import generate_franchise_tree
# from utils.check_missing_anime import find_missing_anime
# from utils.sort_plan_to_watch import sort_plan

def process_anime_list(file_path):
    # Example placeholder: call your actual processing functions here
    # Sample combined result dictionary
    results = {
        "franchise_tree": "Sample franchise tree data or structure here",
        "missing_anime": ["Anime A", "Anime B"],
        "sorted_plan": ["Anime X", "Anime Y"]
    }
    return results

@app.route("/api/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files['file']
    file_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(file_path)

    # Call your processing logic here
    results = process_anime_list(file_path)

    return jsonify({"status": "success", "file": file.filename, "results": results})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
