import os
import hashlib
import pickle
import time
import json
import csv
import xml.etree.ElementTree as ET
import requests
# Persistent cache folder
CACHE_DIR = "api_cache"
os.makedirs(CACHE_DIR, exist_ok=True)
from datetime import datetime
from collections import Counter, defaultdict
from utils import create_zip
from functools import lru_cache

@lru_cache(maxsize=None)
def fetch_anime_info_cached(mal_id):
    return fetch_anime_info(mal_id)

MAL_BASE = "https://myanimelist.net/anime/"
OUTPUT_HTML = "sorted_plan_to_watch.html"
OUTPUT_JSON = "sorted_plan_to_watch.json"
OUTPUT_CSV = "sorted_plan_to_watch.csv"
HEADERS = {"User-Agent": "MAL Plan to Watch Sorter"}
USE_BG_IMAGE = True  # Set to False to disable background image

STATUS_OPTIONS = ["Completed", "Watching", "On-Hold", "Plan to Watch", "Dropped"]
ALLOWED_RELATIONS = {
    "Sequel",
    "Prequel",
    "Side story",
    "Side Story",            # MAL sometimes uses lowercase 's' in side story
    "Spin-off",
    "Summary",
    "Alternative version",
    "Parent story",
    "Full story",
    "Other",                 # Often used for CMs, PVs, Music videos
    "Character",             # Sometimes used to link specials/musics
    "Alternate setting"      # Extra variant some entries use
}
MAX_DEPTH = 20  # Increased depth for better franchise coverage

def safe_string(value, default=""):
    """Ensure we always return a non-None string"""
    if value is None or value == "":
        return default
    return str(value)

def safe_type_filter(anime_type):
    """Safely convert anime type to filter format"""
    if not anime_type or anime_type in [None, "", "None"]:
        return "unknown"
    return str(anime_type).lower().replace(" ", "_")

def safe_status_filter(status):
    """Safely convert status to filter format"""
    if not status or status in [None, "", "None"]:
        return "not_in_list"
    return str(status).lower().replace(" ", "_").replace("-", "_")

def parse_mal_xml(xml_path):
    tree = ET.parse(xml_path)
    root = tree.getroot()
    anime_ids = set()
    anime_info = {}
    anime_status = {}
    status_counter = Counter()

    for anime in root.findall("anime"):
        status = anime.find("my_status").text
        sid = anime.find("series_animedb_id").text
        try:
            mal_id = int(sid)
        except (ValueError, TypeError):
            continue
        title = anime.find("series_title").text
        anime_ids.add(mal_id)
        anime_info[mal_id] = title
        anime_status[mal_id] = status
        status_counter[status] += 1

    return anime_ids, anime_info, anime_status, status_counter

def fetch_mal_api(url, desc='', max_retries=3):
    # Create hashed filename for URL
    cache_key = hashlib.md5(url.encode('utf-8')).hexdigest()
    cache_file = os.path.join(CACHE_DIR, f"{cache_key}.pkl")

    # Step 1: Return cached response if available
    if os.path.exists(cache_file):
        with open(cache_file, "rb") as f:
            return pickle.load(f)

    if not os.path.exists(cache_file):
        time.sleep(0.2)

    # Step 2: Fetch from API if not cached
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=8)
            if r.status_code == 429:  # Rate-limited, wait a bit
                time.sleep(2)
                continue
            r.raise_for_status()
            data = r.json()

            # Save to cache for future runs
            with open(cache_file, "wb") as f:
                pickle.dump(data, f)
            return data
        except Exception:
            time.sleep(1)
    return None

def fetch_related_anime(mal_id):
    data = fetch_mal_api(f"https://api.jikan.moe/v4/anime/{mal_id}/relations")
    if data is not None:
        return data.get("data", [])
    return []

def get_total_episodes(mal_id, max_pages=100):
    """Get total episode count with pagination up to 10000 episodes"""
    total_episodes = 0
    page = 1
    
    while page <= max_pages:
        url = f"https://api.jikan.moe/v4/anime/{mal_id}/episodes?page={page}"
        data = fetch_mal_api(url)
        
        if not data or not data.get("data"):
            break
            
        episodes_on_page = len(data["data"])
        if episodes_on_page == 0:
            break
            
        total_episodes += episodes_on_page
        
        # Check if there are more pages
        pagination = data.get("pagination", {})
        if not pagination.get("has_next_page", False):
            break
            
        page += 1
        time.sleep(0.1)  # Small delay between pagination requests
    
    return total_episodes if total_episodes > 0 else None

def parse_air_date(anime_data):
    """Parse air date with multiple fallbacks."""
    if not anime_data:
        return "Unknown"
    # Check aired.from
    aired_from = anime_data.get("from")
    if aired_from:
        try:
            date_obj = datetime.strptime(aired_from[:10], "%Y-%m-%d")
            return date_obj.strftime("%b %d, %Y")
        except Exception:
            pass
    # Check aired.to
    aired_to = anime_data.get("to")
    if aired_to:
        try:
            date_obj = datetime.strptime(aired_to[:10], "%Y-%m-%d")
            return date_obj.strftime("%b %d, %Y")
        except Exception:
            pass
    # Fallback: season and year
    season = anime_data.get("season")
    year = anime_data.get("year")
    if season and year:
        try:
            year = int(year)
            season_lower = season.lower()
            if "winter" in season_lower:
                return datetime(year, 1, 1).strftime("%b %d, %Y")
            elif "spring" in season_lower:
                return datetime(year, 4, 1).strftime("%b %d, %Y")
            elif "summer" in season_lower:
                return datetime(year, 7, 1).strftime("%b %d, %Y")
            elif "fall" in season_lower or "autumn" in season_lower:
                return datetime(year, 10, 1).strftime("%b %d, %Y")
        except Exception:
            pass
    # Fallback: broadcast string
    broadcast = anime_data.get("broadcast", {})
    if broadcast and broadcast.get("string"):
        broadcast_str = broadcast.get("string")
        if "at" in broadcast_str:
            return f"Broadcast: {broadcast_str}"
    # If all else fails
    return "Unknown"

def convert_xml_start_date_to_aired_dict(start_date):
    """Convert an XML start_date string to the dict format parse_air_date expects."""
    if not start_date or start_date == '0000-00-00' or start_date.strip() == '':
        return {}
    return {'from': start_date.strip()}    

def parse_season_to_date(season_str, year):
    """Parse season string to approximate date"""
    if not season_str or not year:
        return "Unknown"
    
    season_lower = str(season_str).lower()
    try:
        year = int(year)
        if "spring" in season_lower:
            return datetime(year, 4, 1).strftime("%b %d, %Y")
        elif "summer" in season_lower:
            return datetime(year, 7, 1).strftime("%b %d, %Y")
        elif "fall" in season_lower or "autumn" in season_lower:
            return datetime(year, 10, 1).strftime("%b %d, %Y")
        elif "winter" in season_lower:
            return datetime(year, 1, 1).strftime("%b %d, %Y")
    except:
        pass
    
    return "Unknown"

def is_same_franchise(root_title, related_title):
    """Check if two titles belong to the same franchise"""
    if not root_title or not related_title:
        return False
    
    root_lower = root_title.lower()
    related_lower = related_title.lower()
    
    # Extract main franchise keywords
    root_words = [w for w in root_lower.split() if w not in ['the', 'a', 'an', 'of', 'and', 'or']][:2]
    
    # Check if the related title contains the main franchise keywords
    for word in root_words:
        if len(word) > 2 and word in related_lower:
            return True
    
    return False

def fetch_anime_info(mal_id, user_anime_data=None):
    """
    Fetch anime info from MAL API with English title, full aired date fallbacks, 
    correct episode count, and mark in_user_list status.
    """
    in_list = False
    user_status = "Not in list"
    if user_anime_data and mal_id in user_anime_data:
        in_list = True
        user_status = user_anime_data[mal_id]["status"]

    url = f"https://api.jikan.moe/v4/anime/{mal_id}"
    api_data = fetch_mal_api(url)
    if api_data and api_data.get("data"):
        data = api_data["data"]

        # Prefer English title if available
        title = data.get("title_english") or data.get("title", f"Unknown {mal_id}")

        # Aired date with fallbacks
        air_date = parse_air_date(data.get("aired", {}))
        if air_date == "Unknown":
            season_data = data.get("season")
            year_data = data.get("year")
            if season_data and year_data:
                air_date = parse_season_to_date(season_data, year_data)
        if air_date == "Unknown":
            broadcast = data.get("broadcast", {})
            if broadcast and broadcast.get("string"):
                b_str = broadcast["string"]
                if "at" in b_str:
                    air_date = f"Broadcast: {b_str}"

        default_episodes = data.get("episodes")
        if default_episodes and default_episodes > 0:
            episodes = default_episodes  # trust main endpoint
        else:
            episodes = get_total_episodes(mal_id) or 0

        anime_type = safe_string(data.get("type"), "Unknown")

        return {
            "id": mal_id,
            "title": title,
            "air_date": air_date,
            "type": anime_type,
            "type_filter": safe_type_filter(anime_type),
            "episodes": episodes,
            "mal_score": data.get("score", "N/A"),
            "url": f"{MAL_BASE}{mal_id}",
            "image_url": data.get("images", {}).get("jpg", {}).get("image_url", ""),
            "user_status": safe_string(user_status, "Not in list"),
            "in_user_list": in_list,
            "status": user_status
        }
    return None

def find_plan_to_watch_franchises(anime_list):
    """
    Build franchises including all related anime for Plan to Watch entries.
    Uses BFS traversal with allowed relation types and minimal filtering to 
    include all related titles.
    """
    plan_to_watch_ids = set()
    anime_dict = {a["id"]: a for a in anime_list}
    for anime in anime_list:
        if anime["status"] == "Plan to Watch":
            plan_to_watch_ids.add(anime["id"])

    franchise_groups = {}
    visited = set()

    def build_franchise(root_id):
        queue = [root_id]
        franchise_set = set()
        while queue:
            current_id = queue.pop(0)
            if current_id in franchise_set:
                continue
            franchise_set.add(current_id)

            rel_data = fetch_related_anime(current_id)
            for relation in rel_data:
                if relation["relation"] in ALLOWED_RELATIONS:
                    for entry in relation["entry"]:
                        rid = entry["mal_id"]
                        if rid not in franchise_set:
                            queue.append(rid)
            time.sleep(0.3)  # respect rate limits
        return franchise_set

    for ptw_id in plan_to_watch_ids:
        if ptw_id not in visited:
            franchise = build_franchise(ptw_id)
            visited.update(franchise)
            franchise_name = None
            for fid in franchise:
                if fid in anime_dict:
                    franchise_name = anime_dict[fid]["title"]
                    break
            if not franchise_name:
                franchise_name = f"Franchise {min(franchise)}"
            franchise_groups[franchise_name] = franchise

    return franchise_groups

def build_status_dropdown(entries):
    """Build status dropdown with correct options and counts"""
    status_counts = {
        "watching": 0, "completed": 0, "plan_to_watch": 0,
        "on_hold": 0, "dropped": 0
    }
    total = len(entries)
    in_list_total = 0
    not_in_list_total = 0
    
    for entry in entries:
        if entry.get("in_user_list", False):
            in_list_total += 1
            user_status = safe_status_filter(entry.get("status", ""))
            if user_status in status_counts:
                status_counts[user_status] += 1
        else:
            not_in_list_total += 1
    
    dropdown_html = f'<option value="all">All ({total})</option>\n'
    dropdown_html += f'<option value="in-list">In Your List ({in_list_total})</option>\n'
    dropdown_html += f'<option value="not-in-list">Not In Your List ({not_in_list_total})</option>\n'
    dropdown_html += f'<option value="watching">Watching ({status_counts["watching"]})</option>\n'
    dropdown_html += f'<option value="completed">Completed ({status_counts["completed"]})</option>\n'
    dropdown_html += f'<option value="plan_to_watch">Plan To Watch ({status_counts["plan_to_watch"]})</option>\n'
    dropdown_html += f'<option value="on_hold">On Hold ({status_counts["on_hold"]})</option>\n'
    dropdown_html += f'<option value="dropped">Dropped ({status_counts["dropped"]})</option>\n'
    
    return dropdown_html

def build_type_dropdown(entries):
    """Build type dropdown with correct options and counts"""
    type_counts = {
        "tv": 0, "ova": 0, "movie": 0, "special": 0,
        "music": 0, "ona": 0, "cm": 0, "pv": 0,
        "tv_special": 0, "unknown": 0
    }
    total = len(entries)
    
    for entry in entries:
        entry_type = safe_string(entry.get("type_filter"), "unknown")
        if entry_type in type_counts:
            type_counts[entry_type] += 1
        else:
            type_counts["unknown"] += 1
    
    dropdown_html = f'<option value="all">All ({total})</option>\n'
    dropdown_html += f'<option value="tv">TV ({type_counts["tv"]})</option>\n'
    dropdown_html += f'<option value="ova">OVA ({type_counts["ova"]})</option>\n'
    dropdown_html += f'<option value="movie">Movie ({type_counts["movie"]})</option>\n'
    dropdown_html += f'<option value="special">Special ({type_counts["special"]})</option>\n'
    dropdown_html += f'<option value="music">Music ({type_counts["music"]})</option>\n'
    dropdown_html += f'<option value="ona">ONA ({type_counts["ona"]})</option>\n'
    dropdown_html += f'<option value="cm">CM ({type_counts["cm"]})</option>\n'
    dropdown_html += f'<option value="pv">PV ({type_counts["pv"]})</option>\n'
    dropdown_html += f'<option value="tv_special">TV Special ({type_counts["tv_special"]})</option>\n'
    dropdown_html += f'<option value="unknown">Unknown ({type_counts["unknown"]})</option>\n'
    
    return dropdown_html

def generate_html(anime_list, status_counter, output_path=OUTPUT_HTML):
    # Find Plan to Watch franchises
    franchise_groups = find_plan_to_watch_franchises(anime_list)
    
    # Build complete franchise entries with API data
    all_entries = []
    anime_dict = {anime["id"]: anime for anime in anime_list}
    
    for franchise_name, franchise_ids in franchise_groups.items():
        franchise_entries = []

        for anime_id in franchise_ids:
          if anime_id in anime_dict:
              # In‑list: copy XML entry & enrich from API
              entry = anime_dict[anime_id].copy()
              entry["franchise"] = franchise_name

              if "type_filter" not in entry:
                  entry["type_filter"] = safe_type_filter(entry.get("type"))

              entry["in_user_list"] = True
              entry["user_status"] = entry["status"]
              entry["url"] = f"{MAL_BASE}{anime_id}"

              # Fetch API to enrich entry
              api_info = fetch_anime_info_cached(anime_id)
              if api_info:
                  entry["title"] = api_info.get("title", entry["title"])
                  entry["image_url"] = api_info.get("image_url", "")
                  entry["air_date"] = api_info.get("air_date", "Unknown")
                  entry["episodes"] = api_info.get("episodes", 0)
                  entry["type"] = api_info.get("type", "Unknown")
                  entry["type_filter"] = api_info.get("type_filter", "unknown")
                  entry["mal_score"] = api_info.get("mal_score", "N/A")

              franchise_entries.append(entry)

          else:
              # Not in user's list: build entry solely from API info
              api_info = fetch_anime_info_cached(anime_id)
              if api_info:
                  entry = {
                      "id": anime_id,
                      "title": api_info.get("title", f"Unknown {anime_id}"),
                      "status": "Not in list",
                      "user_status": "Not in list",
                      "in_user_list": False,
                      "franchise": franchise_name,
                      "air_date": api_info.get("air_date", "Unknown"),
                      "episodes": api_info.get("episodes", 0),
                      "type": api_info.get("type", "Unknown"),
                      "type_filter": api_info.get("type_filter", "unknown"),
                      "mal_score": api_info.get("mal_score", "N/A"),
                      "url": api_info.get("url", f"{MAL_BASE}{anime_id}"),
                      "image_url": api_info.get("image_url", "")
                  }
                  franchise_entries.append(entry)

          time.sleep(0.2)  # Respect API rate limits

        all_entries.extend(franchise_entries)
    
    status_dropdown_options = build_status_dropdown(all_entries)
    type_dropdown_options = build_type_dropdown(all_entries)
    bg_class = "bg-on" if USE_BG_IMAGE else ""
    
    html_header = fr'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>Sorted Plan to Watch</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@600&family=Bebas+Neue&display=swap');
* {{
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}}
body {{
  font-family: 'Poppins', sans-serif;
  background-color: #121212;
  color: #ffd700;
  margin: 0; 
  padding: 1rem 1.5rem;
  user-select: none;
  min-height: 100vh;
}}
body.bg-on {{
  background: url('../images/one_piece_bg.jpg') no-repeat center center fixed;
  background-size: cover;
  position: relative;
}}
body.bg-on::before {{
  content: "";
  position: fixed;
  inset: 0;
  background: rgba(18, 18, 18, 0.85);
  z-index: -1;
}}
h1 {{
  font-family: 'Bebas Neue', cursive;
  font-size: 3.4rem;
  text-align: center;
  color: #f5c531;
  margin-bottom: 1.5rem;
  text-shadow:
    0 0 8px #f5c531aa,
    0 0 16px #f5c53177;
}}
.ui-panel {{
  max-width: 1500px;
  margin: 0 auto 2rem;
  display: grid;
  grid-template-columns:
    200px /* Status */
    200px /* Type */
    200px /* Title Sort */
    200px /* Air Date Sort */
    200px /* Episodes Sort */
    1fr   /* Search */
    ;
  gap: 1rem;
  padding: 12px 20px;
  background-color: #1a1a1acc;
  border-radius: 12px;
  box-shadow: 0 0 12px #ffd700bb;
}}
.ui-panel label {{
  font-weight: 600;
  color: #c9b037;
  font-size: 1rem;
  user-select: none;
  margin-bottom: 4px;
  display: block;
}}
.ui-panel select,
.ui-panel input[type="search"] {{
  appearance: none;
  background: #222;
  border: 2px solid #444;
  border-radius: 8px;
  color: #ffd700;
  font-weight: 600;
  font-size: 0.95rem;
  padding: 8px 14px;
  box-shadow: 0 0 4px #d4af37cc;
  outline: none;
  width: 100%;
  min-width: 200px;
  transition: border-color 0.3s ease, box-shadow 0.3s ease;
}}
.ui-panel select:hover,
.ui-panel input[type="search"]:hover {{
  border-color: #ffd700;
  box-shadow: 0 0 8px #ffd700dd;
}}
.ui-panel select:focus,
.ui-panel input[type="search"]:focus {{
  border-color: #f8e71c;
  box-shadow: 0 0 12px #f8e71ccc;
  color: #fff;
}}
.franchise-group {{
  background: #1a1a1acc;
  margin-bottom: 24px;
  border-radius: 12px;
  box-shadow:
    0 0 10px #0008,
    0 0 20px #d4af3722;
  overflow: hidden;
}}
.franchise-header {{
  cursor: pointer;
  display: flex;
  align-items: center;
  background: #262626dd;
  color: #f8e71c;
  padding: 18px 24px;
  font-weight: 700;
  font-size: 1.45rem;
  user-select: none;
  border-radius: 12px 12px 0 0;
  box-shadow: inset 0 -2px 10px #d4af3722;
  transition: background 0.3s ease;
}}
.franchise-header:hover {{
  background: #3b3b3bcc;
}}
.toggle-icon {{
  margin-left: auto;
  transition: transform 0.3s ease;
  font-weight: 900;
  font-size: 1.65rem;
  user-select: none;
  color: #f5c531dd;
}}
.franchise-group.collapsed .toggle-icon {{
  transform: rotate(90deg); /* points left toward title */
}}
.franchise-group:not(.collapsed) .toggle-icon {{
  transform: rotate(0deg); /* points down */
}}
.franchise-content {{
  overflow: hidden;
  max-height: 0;                       /* Collapsed height */
  padding: 0 24px;                     /* Collapsed padding */
  transition: max-height 0.5s ease, padding 0.5s ease;
}}

.franchise-group:not(.collapsed) .franchise-content {{
  padding: 16px 24px;                  /* Expanded padding */
  /* max-height handled dynamically by JavaScript */
}}
.anime-entry {{
  display: grid;
  grid-template-columns:
    60px   /* Poster */
    120px /* Status badge */
    120px /* Type */
    1fr   /* Title */
    140px /* Air Date */
    120px /* Episodes */
    40px  /* External Link */
    ;
  align-items: center;
  gap: 0.75rem;
  background-color: #242424;
  border-radius: 10px;
  margin-bottom: 12px;
  padding: 14px 16px;
  box-shadow: 0 0 4px #0008 inset;
  cursor: pointer;
  user-select: none;
  border-left: 7px solid transparent;
  color: #d4d4d4;
  transition:
    background-color 0.3s ease,
    box-shadow 0.3s ease,
    border-color 0.3s ease;
}}
.poster img {{
    width: 60px;
    height: 85px;
    object-fit: cover;
    border-radius: 6px;
    box-shadow: 0 0 6px rgba(0,0,0,0.5);
}}
.anime-entry:hover {{
  background-color: #3e3e3e;
  box-shadow: 0 0 15px #ffd700aa;
  transform: scale(1.01);
  transition: all 0.3s ease;
}}
.anime-entry.in-list {{
  border-left-color: #00FFFF;
  color: #aef0f0;
}}
.anime-entry.not-in-list {{
  border-left-color: #FF0000;
  color: #fdbaba;
  background-color: #3a1b1b;
}}
.status-badge {{
  padding: 5px 16px;
  border-radius: 18px;
  font-weight: 700;
  font-size: 0.85rem;
  text-align: center;
  user-select: none;
  box-shadow: 0 0 6px rgb(0 0 0 / 0.4);
  min-width: 100px;
  max-width: 120px;
  white-space: nowrap;
  color: #222;
  transition: background-color 0.3s ease, color 0.3s ease;
}}
.status-badge.not_in_list {{
  background-color: #FAF9F6;
  color: #222;
  box-shadow: 0 0 8px #999999aa;
}}
.status-badge.completed {{
  background-color: #28a745;
  color: #fff;
  box-shadow: 0 0 8px #28a745bb;
}}
.status-badge.watching {{
  background-color: #007bffcc;
  color: #fff;
  box-shadow: 0 0 8px #007bffcc;
}}
.status-badge.plan_to_watch {{
  background-color: #ffc107;
  color: #222;
  box-shadow: 0 0 8px #ffc107cc;
}}
.status-badge.on_hold {{
  background-color: #6f42c1;
  color: #fff;
  box-shadow: 0 0 8px #6f42c1cc;
}}
.status-badge.dropped {{
  background-color: #6c757d;
  color: #eee;
  box-shadow: 0 0 6px #6c757dcc;
}}
.anime-entry > div.type,
.anime-entry > div.air-date,
.anime-entry > div.episodes {{
  font-style: italic;
  font-size: 1rem;
  user-select: none;
  color: #b6b6b6;
  overflow-wrap: break-word;
}}
.anime-entry > div.title {{
  font-weight: 700;
  font-size: 1.15rem;
  color: #ffd700;
  text-shadow:
    0 0 5px #ffd70099,
    0 0 10px #ffd70088;
  user-select: text;
  overflow-wrap: break-word;
}}
.anime-entry > div.title a {{
  color: inherit;
  text-decoration: none;
  transition: color 0.3s ease, text-shadow 0.5s ease;
}}
.anime-entry > div.title a:hover {{
  color: #f9e72c;
  text-shadow:
    0 0 8px #f9e72ccc,
    0 0 15px #f9e72ccc;
}}
.anime-entry > div.link a {{
  font-size: 1.3rem;
  color: #ffd700aa;
  text-decoration: none;
  transition: color 0.3s ease;
}}
.anime-entry > div.link a:hover {{
  color: #ffff00;
}}
@media (max-width: 900px) {{
  .ui-panel {{
    grid-template-columns: repeat(1, 1fr);
  }}
  .anime-entry {{
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    font-size: 0.95rem;
    padding: 10px 12px;
  }}
  .anime-entry > div.title {{
    font-size: 1.03rem;
  }}
}}
</style>
<script>
function resetOtherSorts(except) {{
  if (except !== "title") document.getElementById("title-sort").value = "default";
  if (except !== "airdate") document.getElementById("airdate-sort").value = "default";
  if (except !== "episodes") document.getElementById("episodes-sort").value = "default";
}}

function toggleFranchise(franchiseElement) {{
  franchiseElement.classList.toggle('collapsed');
  const content = franchiseElement.querySelector('.franchise-content');

  if (!franchiseElement.classList.contains('collapsed')) {{
    // Expanding from collapsed state: animate max-height from 0 to scrollHeight
    content.style.maxHeight = content.scrollHeight + 'px';

    // After animation, remove maxHeight so content can resize naturally
    content.addEventListener('transitionend', function removeMaxHeight() {{
      if (!franchiseElement.classList.contains('collapsed')) {{
        content.style.maxHeight = 'none';
      }}
      content.removeEventListener('transitionend', removeMaxHeight);
    }});
  }} else {{
    // Collapsing: animate max-height from current height to 0 smoothly
    content.style.maxHeight = content.scrollHeight + 'px';  // Set current height explicitly

    content.offsetHeight;  // Force reflow to enable transition

    content.style.maxHeight = '0';  // Collapse to zero height
  }}
}}

function updateFranchiseHeaderCounts() {{
  document.querySelectorAll('.franchise-group').forEach(group => {{
    // Count visible entries in this franchise group
    const entries = group.querySelectorAll('.anime-entry');
    const visibleCount = Array.from(entries).filter(e => e.style.display !== "none").length;
    
    // Update the header count
    const headerSpan = group.querySelector('.franchise-header span');
    if (headerSpan) {{
      const originalTitle = headerSpan.textContent.replace(/\s*(\(\d+\s*entries?\))+$/, '').trim();
      headerSpan.textContent = `${{originalTitle}} (${{visibleCount}} entries)`;
    }}
    
    // Hide franchise if no visible entries
    group.style.display = visibleCount > 0 ? '' : 'none';
  }});
}}

function updateDisplay() {{
  const statusVal   = document.getElementById('status-filter').value;
  const typeVal     = document.getElementById('type-filter').value;
  const titleSort   = document.getElementById('title-sort').value;
  const airdateSort = document.getElementById('airdate-sort').value;
  const episodesSort= document.getElementById('episodes-sort').value;
  const searchTerm  = document.getElementById('search-box').value.toLowerCase();

  // Get all entries
  let entries = Array.from(document.querySelectorAll('.anime-entry'));

  // FILTER step
  entries.forEach(entry => {{
    const entryStatus = entry.getAttribute('data-status');
    const entryType   = entry.getAttribute('data-type');
    const entryTitle  = entry.querySelector('.title a').textContent.toLowerCase();

    let show = true;
    // Status filter logic
    if (statusVal && statusVal !== 'all') {{
      if (statusVal === 'in-list') show = entry.classList.contains('in-list');
      else if (statusVal === 'not-in-list') show = entry.classList.contains('not-in-list');
      else show = (entryStatus === statusVal);
    }}
    // Type filter logic
    if (show && typeVal && typeVal !== 'all') {{
      show = (entryType === typeVal);
    }}
    // Search
    if (show && searchTerm) {{
      show = entryTitle.includes(searchTerm);
    }}
    entry.style.display = show ? '' : 'none';
  }});

  // SORT step (only one can be active at a time besides filters)
  // Get filtered visible entries inside each franchise group
  document.querySelectorAll('.franchise-group').forEach(group => {{
    const container = group.querySelector('.franchise-content');
    let visible = Array.from(container.querySelectorAll('.anime-entry')).filter(e => e.style.display !== "none");
    // Sort
    if (titleSort !== "default") {{
      visible.sort((a, b) => {{
        const at = a.querySelector('.title a').textContent.toLowerCase();
        const bt = b.querySelector('.title a').textContent.toLowerCase();
        return titleSort === "a-z" ? at.localeCompare(bt) : bt.localeCompare(at);
      }});
    }} else if (airdateSort !== "default") {{
      visible.sort((a, b) => {{
        const ad = a.getAttribute('data-air-date') || "1900-01-01";
        const bd = b.getAttribute('data-air-date') || "1900-01-01";
        return airdateSort === "oldest" ? ad.localeCompare(bd) : bd.localeCompare(ad);
      }});
    }} else if (episodesSort !== "default") {{
      visible.sort((a, b) => {{
        const ae = parseInt(a.getAttribute('data-episodes') || '0', 10);
        const be = parseInt(b.getAttribute('data-episodes') || '0', 10);
        return episodesSort === "fewest" ? ae - be : be - ae;
      }});
    }}
    // Re-append in sorted order
    visible.forEach(e => container.appendChild(e));
  }});
  
  // Update franchise header counts and hide empty groups
  updateFranchiseHeaderCounts();
}}

document.addEventListener('DOMContentLoaded', function() {{
  // Add event listeners
  document.getElementById("title-sort").addEventListener("change", function() {{
    resetOtherSorts("title");
    updateDisplay();
  }});
  document.getElementById("airdate-sort").addEventListener("change", function() {{
    resetOtherSorts("airdate");
    updateDisplay();
  }});
  document.getElementById("episodes-sort").addEventListener("change", function() {{
    resetOtherSorts("episodes");
    updateDisplay();
  }});

  ["status-filter", "type-filter", "search-box"].forEach(function(id) {{
    document.getElementById(id).addEventListener("change", updateDisplay);
    document.getElementById(id).addEventListener("input", updateDisplay);
  }});

 // ← ADD THIS BLOCK HERE
    document.querySelectorAll('.franchise-group:not(.collapsed) .franchise-content').forEach(content => {{
      content.style.maxHeight = 'none';
    }});

    // Original call to initialize display
    updateDisplay();
}});
</script>
</head>
<body class="{bg_class}">
<div class="container">
  <h1>Sorted Plan to Watch</h1>
  
  <div class="ui-panel">
    <div>
      <label for="status-filter">Status</label>
      <select id="status-filter">{status_dropdown_options}</select>
    </div>
    <div>
      <label for="type-filter">Type</label>
      <select id="type-filter">{type_dropdown_options}</select>
    </div>
    <div>
      <label for="title-sort">Title</label>
      <select id="title-sort">
        <option value="default">Default</option>
        <option value="a-z">A - Z Ascending</option>
        <option value="z-a">Z - A Descending</option>
      </select>
    </div>
    <div>
      <label for="airdate-sort">Air Date</label>
      <select id="airdate-sort">
        <option value="default">Default</option>
        <option value="oldest">Oldest First</option>
        <option value="newest">Newest First</option>
      </select>
    </div>
    <div>
      <label for="episodes-sort">Episodes</label>
      <select id="episodes-sort">
        <option value="default">Default</option>
        <option value="fewest">Fewest First</option>
        <option value="most">Most First</option>
      </select>
    </div>
    <div>
      <label for="search-box">Search</label>
      <input id="search-box" type="search" placeholder="Search titles..." />
    </div>
  </div>
'''
    
    html_body = ""
    
    # Group entries by franchise
    franchise_entries = defaultdict(list)
    for entry in all_entries:
        franchise_entries[entry["franchise"]].append(entry)
    
    for franchise_name, entries in franchise_entries.items():
        if entries:
            html_body += f'  <div class="franchise-group">\n'
            html_body += f'    <div class="franchise-header" onclick="toggleFranchise(this.parentElement)">\n'
            html_body += f'      <span>{franchise_name} ({len(entries)} entries)</span>\n'
            html_body += f'      <span class="toggle-icon">▼</span>\n'
            html_body += f'    </div>\n'
            html_body += f'    <div class="franchise-content">\n'
            
            # Sort by air date by default
            sorted_entries = sorted(entries, key=lambda x: x.get("air_date", "Unknown"))
            
            for entry in sorted_entries:
                # CRITICAL FIX: Use in_user_list to determine CSS class
                css_class = "anime-entry in-list" if entry.get("in_user_list", False) else "anime-entry not-in-list"
                user_status = safe_string(entry.get('status'), 'Not in list')
                entry_status = safe_status_filter(user_status)
                
                # Fix status for not-in-list entries
                if not entry.get("in_user_list", False):
                    entry_status = "not_in_list"
                    badge_class = "not_in_list"
                else:
                    badge_class = safe_status_filter(user_status)
                
                status_badge = f'<div class="status-badge {badge_class}">{user_status}</div>'
                
                # Add data attributes for sorting
                air_date_sort = "1900-01-01"
                if entry.get("air_date") and entry["air_date"] != "Unknown" and "Broadcast:" not in entry["air_date"]:
                    try:
                        date_obj = datetime.strptime(entry["air_date"], "%b %d, %Y")
                        air_date_sort = date_obj.strftime("%Y-%m-%d")
                    except:
                        pass

                if not entry.get("image_url"):
                    print(f"⚠️ Missing image URL for anime: {entry.get('title', 'Unknown')}")


                if not entry.get("image_url"):
                    print(f"⚠️ Missing image URL for anime: {entry.get('title', 'Unknown')}")
                
                episodes_sort = str(entry.get("episodes", 0))
                type_filter = safe_string(entry.get("type_filter"), "unknown")
                
                html_body += f'      <div class="{css_class}" data-status="{entry_status}" data-air-date="{air_date_sort}" data-episodes="{episodes_sort}" data-type="{type_filter}">\n'
                                
                # Poster image
                image_url = entry.get("image_url") or "https://cdn.myanimelist.net/images/anime/default_image.jpg"
                html_body += f'        <div class="poster"><img src="{image_url}" alt="Poster" loading="lazy" /></div>\n'
                url = entry.get("url", "#")
                html_body += f'        {status_badge}\n'
                html_body += f'        <div class="type">{entry["type"]}</div>\n'
                html_body += f'        <div class="title"><a href="{url}" target="_blank">{entry.get("title","Unknown")}</a></div>\n'
                html_body += f'        <div class="air-date">{entry.get("air_date", "Unknown")}</div>\n'
                html_body += f'        <div class="episodes">{entry["episodes"]} eps</div>\n'
                html_body += f'        <div class="link"><a href="{url}" target="_blank">🔗</a></div>\n'
                html_body += f'      </div>\n'
            html_body += f'    </div>\n'
            html_body += f'  </div>\n'
    
    if not html_body:
        html_body = '<p style="color:#ccc; text-align:center;">No Plan to Watch anime found in your list.</p>'
    
    html_tail = '''</div>
</body>
</html>'''
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_header + html_body + html_tail)
    print(f"✅ HTML saved: {output_path}")

def generate_json(anime_list, output_path=OUTPUT_JSON):
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(anime_list, f, indent=2, ensure_ascii=False)
    print(f"✅ JSON saved: {output_path}")

def generate_csv(anime_list, output_path=OUTPUT_CSV):
    with open(output_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Anime_ID', 'Title', 'Status', 'Your_Score', 'Air_Date', 'Type', 'Episodes', 'MAL_Score', 'URL'])
        for anime in anime_list:
            writer.writerow([
                anime['id'], anime['title'], anime['status'], anime.get('score', 0),
                anime.get('air_date', 'Unknown'), anime['type'], anime['episodes'],
                anime.get('mal_score', 'N/A'), anime['url']
            ])
    print(f"✅ CSV saved: {output_path}")

def main(xml_path=None, output_formats=None):
    start_time = time.time()

    # Detect or prompt for XML file
    if xml_path is None:
        xml_files = [f for f in os.listdir() if f.endswith(".xml")]
        if len(xml_files) == 1:
            print(f"One XML file found: {xml_files[0]}, using it...")
            xml_path = xml_files[0]
        else:
            xml_path = input("Enter path to MAL XML file (leave empty for manual input): ").strip()
            if not xml_path or not os.path.isfile(xml_path):
                print("Please provide a valid XML file path.")
                return

    # Handle output formats argument or prompt
    if output_formats is None:
        choice = input("Choose output formats (comma separated html,json,csv; default html): ").strip()
        if not choice:
            output_formats = ["html"]
        else:
            output_formats = [x.strip().lower() for x in choice.split(",")]

    # Friendly status messages
    print("🔄 Loading your anime list...")
    anime_ids, anime_info, anime_status, status_counter = parse_mal_xml(xml_path)
    print(f"Loaded {len(anime_ids)} anime from your list.")

    print("🌐 Sorting plan to watch (this may take a while)...")

    # Build anime_list for generate_html
    anime_list = [
        {"id": mid, "title": anime_info[mid], "status": anime_status[mid]}
        for mid in anime_ids
    ]

    # Output generation
    output_files = []
    if "html" in output_formats:
        generate_html(anime_list, status_counter)
        output_files.append(OUTPUT_HTML)
    if "json" in output_formats:
        generate_json(anime_list)
        output_files.append(OUTPUT_JSON)
    if "csv" in output_formats:
        generate_csv(anime_list)
        output_files.append(OUTPUT_CSV)

    # Zip outputs if more than one format
    if len(output_files) > 1:
        zip_path = create_zip(output_files)
        print(f"📦 Output files zipped as {zip_path}")
    else:
        print(f"📁 Output files: {output_files}")

    # Finished
    elapsed_time = time.time() - start_time
    print(f"✅ Script completed in {elapsed_time:.2f} seconds.")


if __name__ == "__main__":
    main()
