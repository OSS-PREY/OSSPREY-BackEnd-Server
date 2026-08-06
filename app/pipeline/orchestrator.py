import os
import glob
import json
import logging
import traceback
import concurrent.futures
from datetime import datetime
from dotenv import load_dotenv
from pymongo import MongoClient
from .update_pex import update_pex_generator
from .rust_runner import run_rust_code
from .run_pex import run_forecast  # Still imported so forecast can run if needed
from .store_commit_issues import process_project_data  # Import MongoDB processing
from .github_metadata import get_github_metadata

load_dotenv()

executor = concurrent.futures.ThreadPoolExecutor(max_workers=16)  # Ensures non-blocking execution

MONGODB_URI = os.environ.get("MONGODB_URI")
db_name = os.environ.get("MONGO_DB_NAME", "decal-db")  # Use the correct DB name
client = MongoClient(MONGODB_URI)
db = client[db_name]  # Explicitly select database

def extract_owner_repo(git_link):
    """Extract the (owner, repo) pair from a git URL.

    e.g. "https://github.com/RepoWise/frontend.git" -> ("RepoWise", "frontend")
    """
    link = git_link[:-4] if git_link.endswith(".git") else git_link
    parts = link.rstrip("/").split("/")
    repo = parts[-1] if parts else ""
    owner = parts[-2] if len(parts) >= 2 else ""
    return owner, repo

def extract_project_name(git_link):
    """Bare repository name.

    This matches the file names the scraper writes (e.g.
    "frontend-commit-file-dev.csv"), so it is used for locating those CSVs.
    """
    return extract_owner_repo(git_link)[1]

def make_project_key(owner, repo):
    """Owner-qualified, filesystem-safe identifier used as the caching key, the
    forecaster project name, and the basis for project_id.

    The scraper names its output CSVs by repo name only, but the caches
    (net-vis/<key>.json, forecasts/<key>.json) and the forecaster's own cache are
    keyed by this value, so qualifying it with the owner prevents two repos that
    share a name but have different owners (e.g. a/frontend vs b/frontend) from
    colliding and serving each other's stale results.
    """
    raw = f"{owner}_{repo}" if owner else repo
    return "".join(c if (c.isalnum() or c in ("-", "_")) else "_" for c in raw)

def generate_project_id(project_name):
    """Generate a project_id by removing non-alphanumeric characters and lowercasing."""
    return ''.join(c for c in project_name if c.isalnum()).lower()

def fetch_project_data_from_db(project_id):
    """Retrieve processed data from MongoDB for the given project_id.
       Returns sanitized keys (commit_data and issue_data) so that internal collection names are hidden.
    """
    result = {}

    # Note: using projection {"_id": 0} to hide internal MongoDB identifiers
    commit_data = db.local_commit_links.find_one({"project_id": project_id}, {"_id": 0})
    issue_data = db.local_issue_links.find_one({"project_id": project_id}, {"_id": 0})

    if commit_data:
        logging.info(f"Found commit links data in DB for project_id='{project_id}'")
        result["commit_data"] = commit_data
    if issue_data:
        logging.info(f"Found issue links data in DB for project_id='{project_id}'")
        result["issue_data"] = issue_data

    return result


def get_pre_computed_data(result_summary, net_vis_file, forecasts_file, project_name, project_id):
    with open(net_vis_file, 'r') as f:
        net_vis_data = json.load(f)
    tech_net = net_vis_data.get("tech", {})
    social_net = net_vis_data.get("social", {})
    tech_net["project_name"] = project_name
    tech_net["project_id"] = project_id
    social_net["project_name"] = project_name
    social_net["project_id"] = project_id
    result_summary["tech_net"] = tech_net
    result_summary["social_net"] = social_net   
    
    with open(forecasts_file, 'r') as f:
        forecasts_data = json.load(f)
    result_summary["forecast_json"] = forecasts_data

    return result_summary
    

# Header for a placeholder (empty) issues CSV, matching the scraper's schema.
# Used when a repository has no issues so the social network is simply empty.
_EMPTY_ISSUES_HEADER = (
    "type,issue_url,comment_url,repo_name,id,issue_num,title,user_login,"
    "user_id,user_name,user_email,issue_state,created_at,updated_at,body,reactions"
)


def _write_empty_issues_csv(path):
    """Create an issues CSV containing only a header row (no data).

    The pex-forecaster explicitly supports an empty social network (it only
    requires that the technical and social inputs are not *both* empty), so when
    a repo has no issues we hand it a header-only CSV instead of failing.
    """
    with open(path, "w", newline="") as f:
        f.write(_EMPTY_ISSUES_HEADER + "\n")


def run_pipeline(git_link, tasks="ALL", month_range="0,-1"):
    """Orchestrates the entire pipeline and returns a structured JSON result."""
    result_summary = {}

    # Store the git link immediately.
    result_summary["git_link"] = git_link

    # Captures any reason the forecast stage fails, so the end of the pipeline can
    # surface it clearly instead of returning a "completed" job with zero months.
    forecast_error = None

    # ``project_name`` MUST be the bare repository name. The pex-forecaster derives
    # a project's identity from the scraped CSV contents (the commit CSV's
    # ``project`` column and the issues CSV's ``repo_name`` column, both the bare
    # repo name): it names its network edgelists "<repo>__<month>.edgelist", runs a
    # cross-contamination check that every network file starts with the project
    # name, and looks the name up in its start-dates/incubation registry. Passing
    # an owner-qualified key (e.g. "owner_repo") makes that check fail, so the
    # forecast silently aborts and the UI shows 0 months. The scraper also names its
    # output CSVs by the bare repo, so the same value locates them and keys the cache.
    repo_name = extract_project_name(git_link)
    project_name = repo_name
    project_id = generate_project_id(project_name)
    
    # --- Step 0: Fetch GitHub Repository Metadata ---
    print(git_link)
    try:
        metadata = get_github_metadata(git_link.lower())
          # Add it to the final JSON response
    except Exception as e:
        metadata = {"error": str(e)}
    print("SCRAPED META-DATA PRINTING:", metadata)
    result_summary["metadata"] = metadata


    pex_generator_dir = os.getenv("PEX_GENERATOR_DIR")
    net_vis_file = os.path.join(pex_generator_dir, "net-vis", f"{project_name}.json")
    forecasts_file = os.path.join(pex_generator_dir, "forecasts", f"{project_name}.json")

    if os.path.exists(net_vis_file) and os.path.exists(forecasts_file):   
        result_summary = get_pre_computed_data(result_summary, net_vis_file, forecasts_file, project_name, project_id)
    else:
        # --- Step 1: Update and ensure PEX‑Forecaster ---
        # try:
        #     pex_update = update_pex_generator()
        # except Exception as e:
        #     pex_update = {"error": str(e)}
        # result_summary["pex_update"] = pex_update

        # --- Step 2: Run the Rust scraper ---
        try:
            rust_result = run_rust_code(git_link)
        except Exception as e:
            rust_result = {"error": str(e)}
        result_summary["rust_result"] = rust_result

        # --- Verify output folder exists ---
        output_dir = rust_result.get("output_dir")
        if not output_dir or not os.path.exists(output_dir):
            result_summary["error"] = "GitHub scraping failed: Repository is private!"
            return result_summary

        output_dir = os.path.abspath(output_dir)
        logging.info(f"Output directory: {output_dir}")
        try:
            files_in_output = os.listdir(output_dir)
            logging.info(f"Files in output directory: {files_in_output}")
        except Exception as e:
            logging.error(f"Error listing files in output directory: {e}")

        # ✅ **Blocking MongoDB Processing (Ensures Completion)**
        logging.info("Starting MongoDB processing...")
        # Pass project_id and project_name so the CSV processing uses a consistent identifier

        # process_project_data(output_dir, project_id, project_name)  # Ensures data is stored before fetching

        # --- Step 3: Locate CSV files for social and technical networks ---
        print(f"Looking for: {repo_name+'_issues.csv'} and {repo_name+'-commit-file-dev.csv'}")

        social_csvs = glob.glob(os.path.join(output_dir, repo_name+"_issues.csv"))
        tech_csvs = glob.glob(os.path.join(output_dir, repo_name+"-commit-file-dev.csv"))
        # Technical (commit) data is required: without it there is no network to
        # build and the scrape almost certainly failed.
        if not tech_csvs:
            result_summary["error"] = "No technical network CSV found."
            return result_summary
        tech_csv = os.path.abspath(tech_csvs[0])

        # Optional safety valve for very large repositories whose forecasting can
        # exhaust memory (ending in an uncatchable OOM kill). Disabled by default;
        # set MAX_COMMIT_CSV_MB to the largest commit-CSV size (MB) this host can
        # process so oversized repos fail fast with a clear message.
        try:
            max_commit_csv_mb = float(os.environ.get("MAX_COMMIT_CSV_MB", "0"))
        except ValueError:
            max_commit_csv_mb = 0
        if max_commit_csv_mb > 0:
            tech_csv_mb = os.path.getsize(tech_csv) / (1024 * 1024)
            if tech_csv_mb > max_commit_csv_mb:
                result_summary["error"] = (
                    f"Repository is too large to process safely: commit data is "
                    f"{tech_csv_mb:.0f} MB, exceeding the {max_commit_csv_mb:.0f} MB "
                    f"limit (MAX_COMMIT_CSV_MB). Increase the limit or provision more memory."
                )
                logging.error(result_summary["error"])
                return result_summary

        # Social (issues) data is OPTIONAL. A repository may simply have no
        # issues; the forecaster accepts an empty social network, so synthesize a
        # header-only issues CSV and continue (technical network + forecast still
        # run, with an empty social network) instead of failing the whole job.
        if social_csvs:
            social_csv = os.path.abspath(social_csvs[0])
        else:
            social_csv = os.path.join(output_dir, repo_name + "_issues.csv")
            logging.warning(
                "No issues CSV for '%s'; proceeding with an empty social network "
                "(placeholder: %s).", project_name, social_csv
            )
            _write_empty_issues_csv(social_csv)
            result_summary["social_data_missing"] = True

        print("social and tech csv file names", social_csv, tech_csv)
        
        # --- Step 4: Run pex‑forecaster forecast (run for side effects only) ---
        try:
            forecast_result = run_forecast(tech_csv, social_csv, project_name, tasks, month_range)
            if isinstance(forecast_result, dict) and forecast_result.get("error"):
                forecast_error = forecast_result["error"]
                logging.error("Forecast reported an error: %s", forecast_error)
        except Exception as e:
            forecast_error = f"{type(e).__name__}: {e}"
            logging.error("Forecast processing crashed:\n%s", traceback.format_exc())

        # ✅ Fetch Data from MongoDB and Add to Response (After Processing Completes)
        # print("PROJECT NAME AND ID PRINITNG")
        # print(project_name, project_id)
        # mongo_data = fetch_project_data_from_db(project_id)
        # print("MONGO DAtA SO FAR", mongo_data)
        # result_summary.update(mongo_data)
        # print("Summary so far: ", result_summary)
        
        # --- Step 4 - (Cache collection) Move CSV files to archive folder ---
        # try:
        #     parent_dir = os.path.dirname(output_dir)
        #     archive_dir = os.path.join(parent_dir, "archive")

        #     if not os.path.exists(archive_dir):
        #         os.makedirs(archive_dir)

        #     # Move files
        #     social_csv_dest = os.path.join(archive_dir, os.path.basename(social_csv))
        #     tech_csv_dest = os.path.join(archive_dir, os.path.basename(tech_csv))
            
        #     os.rename(social_csv, social_csv_dest)
        #     os.rename(tech_csv, tech_csv_dest)

        #     logging.info(f"Moved {social_csv} to {social_csv_dest}")
        #     logging.info(f"Moved {tech_csv} to {tech_csv_dest}")
        # except Exception as e:
        #     logging.error(f"Error moving CSV files to archive: {e}")

    
    
    # --- Load the forecast / network-visualization outputs ---
    # NOTE: ReACT is intentionally NOT run here. It is handled entirely in the
    # front-end; the backend must never invoke the ReACT extractor.
    # The forecaster writes net-vis/<project>.json and forecasts/<project>.json.
    # If both exist we attach them; otherwise the run produced no usable forecast,
    # so the job is failed with a specific reason rather than reported "completed"
    # with zero months.
    if os.path.exists(net_vis_file) and os.path.exists(forecasts_file):
        try:
            result_summary = get_pre_computed_data(
                result_summary, net_vis_file, forecasts_file, project_name, project_id
            )
        except Exception as e:
            result_summary["error"] = (
                f"Failed to read the forecast output for '{project_name}': "
                f"{type(e).__name__}: {e}"
            )
            logging.error("Reading forecast output failed:\n%s", traceback.format_exc())
    elif "error" not in result_summary:
        reason = forecast_error or (
            "the forecaster produced no output (no months could be computed) — "
            "check the backend logs for the forecast stage"
        )
        result_summary["error"] = f"Forecast unavailable for '{project_name}': {reason}"
        logging.error(result_summary["error"])

    return result_summary
