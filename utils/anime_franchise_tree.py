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
from collections import deque, Counter
import re
from utils import create_zip
from functools import lru_cache
from rapidfuzz import fuzz

@lru_cache(maxsize=None)
def fetch_anime_info_cached(mal_id):
    return fetch_anime_info(mal_id)

MAL_BASE = "https://myanimelist.net/anime/"
OUTPUT_HTML = "anime_franchise_tree.html"
OUTPUT_JSON = "anime_franchise_tree.json"
OUTPUT_CSV = "anime_franchise_tree.csv"
HEADERS = {"User-Agent": "MAL Franchise Tree Builder"}
USE_BG_IMAGE = True  # Set to False to disable background image

ALLOWED_RELATIONS = {
    "Sequel",
    "Prequel",
    "Side story",
    "Side Story",
    "Spin-off",
    "Summary",
    "Alternative version",
    "Parent story",
    "Full story",
    "Other",                # <-- often used for CMs, PVs, Music vids
    "Character",            # <-- sometimes used to link specials/musics
    "Alternate setting",    # optional, in MAL's schema
    # add anything else you see in check_missing_anime.py
}
MAX_DEPTH = 20  # Increased depth for better franchise coverage
STATUS_OPTIONS = ["Completed", "Watching", "On-Hold", "Plan to Watch", "Dropped"]

def extract_root_keywords(root_info):
    """
    Extract keywords from the root anime title, English title, and synonyms.
    """
    exclude_words = {
        'the', 'a', 'an', 'of', 'and', 'or',
        'in', 'on', 'at', 'to', 'for', 'with', 'by'
    }
    keywords = set()

    def clean_and_add(text):
        text = re.sub(r"[^\w\s]", " ", text.lower())
        for w in text.split():
            if w not in exclude_words and len(w) > 2:
                keywords.add(w)

    if 'title' in root_info and root_info['title']:
        clean_and_add(root_info['title'])
    if 'title_english' in root_info and root_info['title_english']:
        clean_and_add(root_info['title_english'])
    for syn in root_info.get('synonyms', []):
        clean_and_add(syn)

    return list(keywords)

def is_same_franchise(root_title, related_title, relation_type=None, root_characters=None, root_keywords=None, root_studios=None, related_studios=None):
    if not root_title or not related_title:
        return False

    root_lower = root_title.lower()
    related_lower = related_title.lower()

    always_accept_relations = {
        "Side story", "Side Story", "Spin-off", "Character",
        "Summary", "Full story", "Parent story", "Alternative version", "Alternate setting"
    }
    if relation_type in always_accept_relations:
        return True

    conditional_relations = {"Other", "Special"}
    if relation_type in conditional_relations:
        if root_lower in related_lower or related_lower in root_lower:
            return True
        if root_keywords and any(kw in related_lower for kw in root_keywords):
            return True
        if root_characters and any(cname.lower() in related_lower for cname in root_characters):
            return True
        if root_studios and related_studios and not root_studios.isdisjoint(related_studios):
            return True
        if fuzz.token_set_ratio(root_title, related_title) >= 85:
            return True
        return False

    # fallback keyword match for other relation types
    if root_keywords:
        matches = sum(1 for word in root_keywords if word in related_lower)
        return matches / len(root_keywords) >= 0.6

    return False

def is_strong_franchise_match(root_title, related_title, root_keywords, root_characters):
    """
    Only returns True if related_title is purely from the same franchise.
    Blocks traversal into crossovers with other shows.
    """
    if not root_title or not related_title:
        return False

    rl = related_title.lower()

    # 1. Must have some root match
    match_root = (
        root_title.lower() in rl or
        (root_keywords and any(kw in rl for kw in root_keywords)) or
        (root_characters and any(c.lower() in rl for c in root_characters))
    )
    if not match_root:
        return False

    # 2. Handle crossovers
    if " x " in rl or "×" in rl:
        parts = [p.strip() for p in rl.replace("×", " x ").split(" x ")]
        for part in parts:
            has_root = (
                root_title.lower() in part or
                (root_keywords and any(kw in part for kw in root_keywords)) or
                (root_characters and any(c.lower() in part for c in root_characters))
            )
            if not has_root:
                return False

    return True

def build_franchise_tree(root_id, user_anime_data):
    franchise = {}
    visited = set()
    queue = deque()

    if not isinstance(root_id, int):
        print(f"⚠️ Invalid root_id type ({type(root_id)}): {root_id}")
        return {}

    queue.append((root_id, 0))
    visited.add(root_id)

    root_info = fetch_anime_info_with_user(root_id, user_anime_data)
    if not root_info:
        return {}

    root_title = root_info['title']
    root_keywords = extract_root_keywords(root_info)
    root_characters = fetch_root_characters(root_id)

    franchise[root_id] = root_info
    print(f"🎯 Root anime: {root_title}")

    while queue:
        current_id, depth = queue.popleft()
        if depth > MAX_DEPTH:
            continue

        if depth > 0:
            anime_info = fetch_anime_info_with_user(current_id, user_anime_data)
            if anime_info:
                franchise[current_id] = anime_info

        if depth < MAX_DEPTH:
            try:
                relations = fetch_related_anime(current_id)
                for relation in relations:
                    relation_type = relation.get('relation')
                    if relation_type in ALLOWED_RELATIONS:
                        for entry in relation.get('entry', []):
                            rid = entry.get('mal_id')
                            rtitle = entry.get('name', '')

                            if not isinstance(rid, int):
                                print(f"⚠ Skipping invalid MAL ID for related entry: {rtitle} -> {rid} ({type(rid)})")
                                continue

                            if not is_same_franchise(root_title, rtitle, relation_type, root_characters, root_keywords):
                                print(f"⏭ Skipping unrelated: {rtitle} (ID: {rid}) - Not in same franchise")
                                continue

                            if rid not in visited:
                                # Check if we should traverse further from this entry
                                queue_traverse = is_strong_franchise_match(root_title, rtitle, root_keywords, root_characters)

                                if queue_traverse:
                                    queue.append((rid, depth + 1))
                                    print(f"  ➕ Added related & traversing: {rtitle} (ID: {rid})")
                                else:
                                    print(f"  ➕ Added collab/related entry but NOT traversing into: {rtitle} (ID: {rid})")

                                visited.add(rid)
                                # Always record the entry in the franchise
                                franchise[rid] = fetch_anime_info_with_user(rid, user_anime_data)
            except Exception as e:
                print(f"⚠️ Error fetching relations for {current_id}: {e}")

    return franchise
    
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

def find_xml_file():
    xml_files = [f for f in os.listdir() if f.endswith(".xml")]
    if len(xml_files) == 1:
        print(f"One XML file found: {xml_files[0]}, using it...")
        return xml_files[0]
    while True:
        file_name = input("Enter path to MAL XML file (leave empty to auto-detect): ").strip()
        if not file_name and len(xml_files) == 1:
            print(f"Using detected file {xml_files[0]}...")
            return xml_files[0]
        elif os.path.isfile(file_name):
            return file_name
        else:
            print("Invalid file path or no file detected. Please try again.")

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

def extract_mal_id_from_url(url):
    match = re.search(r'/anime/(\d+)', url)
    if match:
        return int(match.group(1))
    return None

def search_anime_by_name_online(name):
    url = f"https://api.jikan.moe/v4/anime"
    params = {"q": name, "limit": 5}
    try:
        response = requests.get(url, headers=HEADERS, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("data"):
            found_title = data["data"][0].get('title_english') or data["data"][0].get('title')
            found_id = data["data"][0]['mal_id']
            print(f"✅ Found: {found_title} (ID: {found_id})")
            return found_id, found_title
        else:
            print("No anime found online with that name.")
            return None, None
    except Exception as e:
        print(f"Error searching for anime: {e}")
        return None, None

def find_anime_in_list(query, anime_data):
    matches = []
    query_lower = query.lower()
    for mal_id, data in anime_data.items():
        if query_lower in data["title"].lower():
            matches.append((mal_id, data["title"]))
    return matches

def search_local_and_online(anime_data):
    # Clear any previous search state to fix inconsistent search results
    while True:
        query = input("Enter MAL anime ID, name, or MAL anime url to build franchise tree: ").strip()
        if not query:
            print("Please enter a valid input.")
            continue

        # Reset any cached/previous search variables
        query = query.strip().lower()
        
        if "myanimelist.net" in query:
            mal_id = extract_mal_id_from_url(query)
            if mal_id:
                print(f"🔍 Extracted MAL ID {mal_id} from URL")
                return mal_id
            else:
                print("Could not extract MAL ID from URL. Please try again.")
                continue
        try:
            mal_id = int(query)
            # If the MAL ID is in the user's anime list, show "Found in your list"
            if mal_id in anime_data:
                print(f"✅ Found in your list: {anime_data[mal_id]['title']} (ID: {mal_id})")
            else:
                print(f"🔍 Using MAL ID {mal_id}")
            return mal_id
        except ValueError:
            # Search locally first
            original_query = query  # Keep original for display
            matches = find_anime_in_list(query, anime_data)
            if matches:
                print(f"✅ Found in your list: {matches[0][1]} (ID: {matches[0][0]})")
                return matches[0][0]
            # Search online as fallback
            print(f"🔍 Searching '{original_query}' on MyAnimeList...")
            found_id, found_title = search_anime_by_name_online(original_query)
            if found_id:
                return found_id
            print("❌ Could not find the anime. Please try again.")

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
    url = f"https://api.jikan.moe/v4/anime/{mal_id}/relations"
    data = fetch_mal_api(url)
    if data is not None:
        relations = data.get("data", [])
        return relations
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

def parse_air_date(aired_data):
    """Parse air date with multiple fallbacks"""
    if not aired_data:
        return "Unknown"
    
    # Try aired.from first
    aired_from = aired_data.get("from")
    if aired_from:
        try:
            date_obj = datetime.strptime(aired_from[:10], "%Y-%m-%d")
            return date_obj.strftime("%b %d, %Y")
        except:
            pass
    
    # Try aired.to if from is not available
    aired_to = aired_data.get("to")
    if aired_to:
        try:
            date_obj = datetime.strptime(aired_to[:10], "%Y-%m-%d")
            return date_obj.strftime("%b %d, %Y")
        except:
            pass
    
    return "Unknown"

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

@lru_cache(maxsize=None)
def fetch_anime_info_cached(mal_id):
    return fetch_anime_info(mal_id)

# Wrapper to pass user_anime_data without breaking cache
def fetch_anime_info_with_user(mal_id, user_anime_data=None):
    info = fetch_anime_info_cached(mal_id)
    if not info:
        return None
    info = dict(info)  # make a mutable copy
    if user_anime_data and mal_id in user_anime_data:
        info["user_status"] = user_anime_data[mal_id]["status"]
        info["in_user_list"] = True
    return info

def is_same_franchise(root_title, related_title, relation_type=None, root_characters=None, root_keywords=None):
    """
    Dynamically verify that a related anime belongs to the same franchise:
    - Always allow safe, inherently in-universe relations (Side Story, Spin-off, Character, etc.).
    - For riskier types like 'Other'/'Special', allow only if title or characters match the root franchise keywords.
    """
    if not root_title or not related_title:
        return False

    root_lower = root_title.lower()
    related_lower = related_title.lower()

    # ✅ Always accept safe MAL relations
    always_accept_relations = {
        "Side story", "Side Story",
        "Spin-off",
        "Character",
        "Summary",
        "Full story",
        "Parent story",
        "Alternative version",
        "Alternate setting"
    }
    if relation_type in always_accept_relations:
        return True

    # ⚖ Conditional acceptance for risky types
    conditional_relations = {"Other", "Special"}
    if relation_type in conditional_relations:
        # 1. Franchise title check
        if root_lower in related_lower or related_lower in root_lower:
            return True
        # 2. Root keywords check
        if root_keywords:
            for kw in root_keywords:
                if kw in related_lower:
                    return True
        # 3. Character name match
        if root_characters:
            for cname in root_characters:
                if cname.lower() in related_lower:
                    return True
        return False

    # ✅ Fallback keyword match (normally for Merge / Compilation / Unknown types)
    if root_keywords:
        matches = sum(1 for word in root_keywords if word in related_lower)
        return matches / len(root_keywords) >= 0.6

    return False

from collections import deque
import time

MAX_DEPTH = 20

ALLOWED_RELATIONS = {
    "Sequel",
    "Prequel",
    "Side story",
    "Side Story",
    "Spin-off",
    "Summary",
    "Alternative version",
    "Parent story",
    "Full story",
    "Other",                # <-- often used for CMs, PVs, Music vids
    "Character",            # <-- sometimes used to link specials/musics
    "Alternate setting",    # optional, in MAL's schema
    # add anything else you see in check_missing_anime.py
}

from collections import deque

def fetch_root_characters(mal_id, max_chars=20):
    """Get a list of main character names for the root anime"""
    url = f"https://api.jikan.moe/v4/anime/{mal_id}/characters"
    data = fetch_mal_api(url)
    characters = []
    if data and "data" in data:
        for char_entry in data["data"][:max_chars]:
            cname = char_entry.get("character", {}).get("name")
            if cname:
                characters.append(cname)
    return characters

def build_franchise_tree(root_id, user_anime_data):
    franchise = {}
    visited = set()
    queue = deque()

    if not isinstance(root_id, int):
        print(f"⚠️ Invalid root_id type ({type(root_id)}): {root_id}")
        return {}

    queue.append((root_id, 0))
    visited.add(root_id)

    root_info = fetch_anime_info_with_user(root_id, user_anime_data)
    if not root_info:
        return {}

    root_title = root_info['title']
    root_keywords = extract_root_keywords(root_info)  # ✅ NEW
    root_characters = fetch_root_characters(root_id)  # ✅

    franchise[root_id] = root_info
    print(f"🎯 Root anime: {root_title}")

    while queue:
        current_id, depth = queue.popleft()
        if depth > MAX_DEPTH:
            continue

        if depth > 0:
            anime_info = fetch_anime_info_with_user(current_id, user_anime_data)
            if anime_info:
                franchise[current_id] = anime_info

        if depth < MAX_DEPTH:
            try:
                relations = fetch_related_anime(current_id)
                for relation in relations:
                    relation_type = relation.get('relation')
                    if relation_type in ALLOWED_RELATIONS:
                        for entry in relation.get('entry', []):
                            rid = entry.get('mal_id')
                            rtitle = entry.get('name', '')

                            if not isinstance(rid, int):
                                print(f"⚠ Skipping invalid MAL ID for related entry: {rtitle} -> {rid} ({type(rid)})")
                                continue

                            # ✅ Now passes dynamic root_keywords + root_characters
                            if not is_same_franchise(root_title, rtitle, relation_type, root_characters, root_keywords):
                                print(f"⏭ Skipping unrelated: {rtitle} (ID: {rid}) - Not in same franchise")
                                continue

                            if rid not in visited:
                                queue_traverse = is_strong_franchise_match(root_title, rtitle, root_keywords, root_characters)
                                if queue_traverse:
                                    queue.append((rid, depth + 1))
                                    print(f"  ➕ Added related & traversing: {rtitle} (ID: {rid})")
                                else:
                                    print(f"  ➕ Added collab/related entry but NOT traversing into: {rtitle} (ID: {rid})")

                                visited.add(rid)
                                franchise[rid] = fetch_anime_info_with_user(rid, user_anime_data)
            except Exception as e:
                print(f"⚠️ Error fetching relations for {current_id}: {e}")

    return franchise
    
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

def build_status_dropdown(franchise):
    """Build status dropdown with correct options and counts"""
    status_counts = {
        "watching": 0, "completed": 0, "plan_to_watch": 0,
        "on_hold": 0, "dropped": 0
    }
    total = len(franchise)
    in_list_total = 0
    not_in_list_total = 0
    
    for anime_id, info in franchise.items():
        if info.get("in_user_list", False):
            in_list_total += 1
            user_status = safe_status_filter(info.get("user_status", ""))
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

def build_type_dropdown(franchise):
    """Build type dropdown with correct options and counts"""
    type_counts = {
        "tv": 0, "ova": 0, "movie": 0, "special": 0,
        "music": 0, "ona": 0, "cm": 0, "pv": 0,
        "tv_special": 0, "unknown": 0
    }
    total = len(franchise)
    
    for anime_id, info in franchise.items():
        anime_type = safe_string(info.get("type_filter"), "unknown")
        if anime_type in type_counts:
            type_counts[anime_type] += 1
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

def generate_html(trees, anime_data, output_path=OUTPUT_HTML):
    # Calculate dropdown counts
    all_franchise = {}
    for src_title, franchise in trees.items():
        all_franchise.update(franchise)
    
    status_dropdown_options = build_status_dropdown(all_franchise)
    type_dropdown_options = build_type_dropdown(all_franchise)
    bg_class = "bg-on" if USE_BG_IMAGE else ""
    
    html_header = fr'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>Anime Franchise Tree</title>
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

    // â† ADD THIS BLOCK HERE
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
  <h1>Anime Franchise Tree</h1>
  
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
    for src_title, franchise in trees.items():
        if franchise:
            html_body += f'  <div class="franchise-group">\n'
            html_body += f'    <div class="franchise-header" onclick="toggleFranchise(this.parentElement)">\n'
            html_body += f'      <span>{src_title} Franchise ({len(franchise)} entries)</span>\n'
            html_body += f'      <span class="toggle-icon">▼</span>\n'
            html_body += f'    </div>\n'
            html_body += f'    <div class="franchise-content">\n'
            
            # Sort by air date by default
            sorted_entries = sorted(franchise.items(), key=lambda x: x[1].get('air_date', 'Unknown'))
            
            for anime_id, anime_info in sorted_entries:
                css_class = "anime-entry in-list" if anime_info["in_user_list"] else "anime-entry not-in-list"
                user_status = safe_string(anime_info.get('user_status'), 'Not in list')
                entry_status = safe_status_filter(user_status)
                if not anime_info["in_user_list"]:
                    entry_status = "not_in_list"
                    badge_class = "not_in_list"
                else:
                    badge_class = safe_status_filter(user_status)
                
                status_badge = f'<div class="status-badge {badge_class}">{user_status}</div>'
                
                # Add data attributes for sorting
                air_date_sort = "1900-01-01"
                if anime_info.get("air_date") and anime_info["air_date"] != "Unknown" and "Broadcast:" not in anime_info["air_date"]:
                    try:
                        date_obj = datetime.strptime(anime_info["air_date"], "%b %d, %Y")
                        air_date_sort = date_obj.strftime("%Y-%m-%d")
                    except:
                        pass

                if not anime_info.get("image_url"):
                    print(f"⚠️ Missing image URL for anime: {anime_info.get('title', 'Unknown')}")

                episodes_sort = str(anime_info.get("episodes", 0))
                type_filter = safe_string(anime_info.get("type_filter"), "unknown")
                
                html_body += f'      <div class="{css_class}" data-status="{entry_status}" data-air-date="{air_date_sort}" data-episodes="{episodes_sort}" data-type="{type_filter}">\n'
                
                # Poster image
                image_url = anime_info.get("image_url", "") or "https://cdn.myanimelist.net/images/anime/default_image.jpg"
                html_body += f'        <div class="poster"><img src="{image_url}" alt="Poster" loading="lazy" /></div>\n'

                html_body += f'        {status_badge}\n'
                html_body += f'        <div class="type">{anime_info["type"]}</div>\n'
                html_body += f'        <div class="title"><a href="{anime_info["url"]}" target="_blank">{anime_info["title"]}</a></div>\n'
                html_body += f'        <div class="air-date">{anime_info["air_date"]}</div>\n'
                html_body += f'        <div class="episodes">{anime_info["episodes"]} eps</div>\n'
                html_body += f'        <div class="link"><a href="{anime_info["url"]}" target="_blank">🔗</a></div>\n'
                html_body += f'      </div>\n'
            
            html_body += f'    </div>\n'
            html_body += f'  </div>\n'
    
    if not html_body:
        html_body = '<p style="color:#ccc; text-align:center;">No franchise entries found.</p>'
    
    html_tail = '''</div>
</body>
</html>'''
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_header + html_body + html_tail)
    print(f"✅ HTML saved: {output_path}")

def generate_json(trees, output_path=OUTPUT_JSON):
    output_data = []
    for src_title, franchise in trees.items():
        franchise_data = {
            "franchise_name": src_title,
            "franchise_size": len(franchise),
            "entries": list(franchise.values())
        }
        output_data.append(franchise_data)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)
    print(f"✅ JSON saved: {output_path}")

def generate_csv(trees, output_path=OUTPUT_CSV):
    with open(output_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Franchise', 'Anime_ID', 'Title', 'Air_Date', 'Type', 'Episodes', 'Score', 'User_Status', 'In_User_List', 'URL'])
        for src_title, franchise in trees.items():
            for anime_id, anime_info in franchise.items():
                writer.writerow([
                    src_title,
                    anime_id,
                    anime_info['title'],
                    anime_info['air_date'],
                    anime_info['type'],
                    anime_info['episodes'],
                    anime_info['score'],
                    anime_info.get('user_status', 'Not in list'),
                    anime_info.get('in_user_list', False),
                    anime_info['url']
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

    # Build user data dict
    user_anime_data = {
        mid: {"title": anime_info[mid], "status": anime_status[mid]}
        for mid in anime_ids
    }

    # Prompt user for root anime choice
    root_id = search_local_and_online(user_anime_data)

    # Build franchise tree
    print("🌐 Building franchise tree (this may take a while)...")
    franchise = build_franchise_tree(root_id, user_anime_data)

    root_title = anime_info.get(root_id) or franchise.get(root_id, {}).get("title", f"ID {root_id}")
    if not isinstance(root_title, str):
        root_title = str(root_title)
    trees = {root_title: franchise}

    # Output generation
    output_files = []
    if "html" in output_formats:
        generate_html(trees, anime_info)
        output_files.append(OUTPUT_HTML)
    if "json" in output_formats:
        generate_json(trees)
        output_files.append(OUTPUT_JSON)
    if "csv" in output_formats:
        generate_csv(trees)
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