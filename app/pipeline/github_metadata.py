import requests
import logging
import itertools
from urllib.parse import urlparse
from app.config import Config  # Ensure this has GITHUB_TOKENS: List[str]

# Rotate available GitHub tokens
token_cycle = itertools.cycle(Config.GITHUB_TOKENS) if Config.GITHUB_TOKENS else None

def get_github_metadata(repo_url):
    """Fetch metadata from GitHub API with rotating tokens.
    
    Includes:
    - Basic repo info
    - Programming languages
    - Latest release (if any)
    """

    try:
        if not token_cycle:
            logging.error("No GitHub tokens found in Config!")
            return {"error": "No GitHub tokens available"}

        # Parse GitHub repo URL
        parsed_url = urlparse(repo_url)
        path_parts = parsed_url.path.strip("/").split("/")

        if len(path_parts) < 2:
            logging.error(f"Invalid GitHub repository URL: {repo_url}")
            return {"error": "Invalid GitHub repository URL"}

        owner = path_parts[0]
        repo = path_parts[1].removesuffix(".git")  # Python 3.9+

        api_base_url = f"https://api.github.com/repos/{owner}/{repo}"

        # Rotate GitHub tokens
        token = next(token_cycle)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }

        # Fetch main repo metadata
        response = requests.get(api_base_url, headers=headers)

        # Rotate through tokens if rate limited
        if response.status_code == 403:
            logging.warning(f"Rate limit hit for token: {token}")
            for _ in range(len(Config.GITHUB_TOKENS) - 1):
                token = next(token_cycle)
                headers["Authorization"] = f"token {token}"
                response = requests.get(api_base_url, headers=headers)
                if response.status_code != 403:
                    break

        if response.status_code != 200:
            logging.error(f"GitHub API error: {response.status_code} - {response.text}")
            return {"error": f"GitHub API error: {response.status_code}"}

        repo_data = response.json()

        # Get languages
        lang_resp = requests.get(f"{api_base_url}/languages", headers=headers)
        languages = list(lang_resp.json().keys()) if lang_resp.status_code == 200 else []

        # Get latest release
        release_resp = requests.get(f"{api_base_url}/releases/latest", headers=headers)
        if release_resp.status_code == 200:
            release_data = release_resp.json()
            latest_release = {
                "tag": release_data.get("tag_name"),
                "name": release_data.get("name"),
                "published_at": release_data.get("published_at"),
            }
        else:
            latest_release = "No releases available"

        # Extract owner data safely
        # owner_data = repo_data.get("owner") or {}

        # Final metadata
        metadata = {
            "name": repo_data.get("name", "Unknown"),
            "owner": owner,
            "description": repo_data.get("description") or "No description provided",
            "stars": repo_data.get("stargazers_count", 0),
            "watchers": repo_data.get("watchers_count", 0),
            "forks": repo_data.get("forks_count", 0),
            "license": (repo_data.get("license") or {}).get("name", "No license"),
            "created_at": repo_data.get("created_at"),
            "updated_at": repo_data.get("updated_at"),
            "open_issues": repo_data.get("open_issues_count", 0),
            "languages": languages,
            "latest_release": latest_release
        }

        return metadata

    except requests.exceptions.RequestException as e:
        logging.error(f"Request error: {e}")
        return {"error": "Failed to connect to GitHub API"}

    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        return {"error": f"Unexpected error: {str(e)}"}
