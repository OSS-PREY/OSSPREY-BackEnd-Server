# flask-app/pipeline/rust_runner.py
import subprocess
import os
import logging
import time
import urllib.error
import urllib.request
from dotenv import load_dotenv
import pandas as pd
import json

from app.config import Config

load_dotenv()

OSS_SCRAPER_REPO_URL = os.getenv("OSS_SCRAPER_REPO_URL")
OSS_SCRAPER_DIR = os.getenv("OSS_SCRAPER_DIR")

def ensure_oss_scraper_repo():
    """Ensures the OSS‑Scraper repository is cloned locally.
       If the target directory does not exist, it is created and the repo is cloned.
       If it exists and is a git repository, perform a git pull to update it.
    """
    if not OSS_SCRAPER_DIR:
        raise Exception("OSS_SCRAPER_DIR is not set in your .env file.")
    
    if not os.path.exists(OSS_SCRAPER_DIR):
        try:
            os.makedirs(OSS_SCRAPER_DIR, exist_ok=True)
            logging.info(f"Directory {OSS_SCRAPER_DIR} did not exist; attempting to clone repository.")
            subprocess.run(
                ["git", "clone", OSS_SCRAPER_REPO_URL, OSS_SCRAPER_DIR],
                check=True
            )
        except Exception as e:
            logging.error(f"Failed to create or clone into {OSS_SCRAPER_DIR}: {e}")
            raise
    else:
        git_dir = os.path.join(OSS_SCRAPER_DIR, ".git")
        if os.path.exists(git_dir):
            logging.info(f"Directory {OSS_SCRAPER_DIR} exists and is a git repository; updating repository.")
            try:
                subprocess.run(["git", "pull"], cwd=OSS_SCRAPER_DIR, check=True)
            except Exception as e:
                logging.error(f"Failed to update repository at {OSS_SCRAPER_DIR}: {e}")
                raise
        else:
            logging.warning(f"Directory {OSS_SCRAPER_DIR} exists but is not a git repository. Skipping clone/update.")
    
    return os.path.abspath(OSS_SCRAPER_DIR)

# The miner clones each repository into a TempDir, which lands in /tmp -- the
# root filesystem, which runs at ~95% full. Four concurrent jobs held 6.5 GB of
# clones during a 50-repo load test and took 5 GB off the root volume in 20
# minutes; a full queue of large repositories would exhaust it and take the host
# down. /mnt/data1 has terabytes free, so point the miner's temp space there.
SCRAPER_TMPDIR = os.environ.get(
    "OSSPREY_SCRAPER_TMPDIR", "/mnt/data1/OSSPREY/.scraper-tmp")


def scraper_env():
    """Environment for the miner, with temp space on the big volume."""
    env = os.environ.copy()
    os.makedirs(SCRAPER_TMPDIR, exist_ok=True)
    env["TMPDIR"] = SCRAPER_TMPDIR
    return env


# The issue scrape is retried this many times when it comes back empty but the
# repository is known to have issues. Waits are long because a GitHub secondary
# rate limit asks for "a few minutes", not seconds.
ISSUE_FETCH_ATTEMPTS = 3
ISSUE_RETRY_WAITS = (60, 180)


def issues_have_rows(path):
    """True when the issues CSV holds at least one row beyond its header."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.readline()
            return bool(fh.readline().strip())
    except OSError:
        return False


def github_issue_count(git_link):
    """How many issues GitHub reports for the repo, or None if unknown.

    A header-only CSV is ambiguous on its own: laravel/laravel genuinely has
    zero issues while golang/go has 72,726 and was simply refused. This is the
    one call that tells the two apart, so an empty-but-correct scrape is not
    retried and a rejected one is not accepted.
    """
    tokens = getattr(Config, "GITHUB_TOKENS", None) or []
    if not tokens:
        return None
    owner, repo = extract_owner_repo_local(git_link)
    if not owner or not repo:
        return None
    url = (f"https://api.github.com/search/issues"
           f"?q=repo:{owner}/{repo}+type:issue&per_page=1")
    req = urllib.request.Request(url, headers={
        "Authorization": f"bearer {tokens[0]}",
        "User-Agent": "ossprey-pipeline",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("total_count")
    except Exception as e:
        logging.warning("Could not read issue count for %s/%s: %s", owner, repo, e)
        return None


def extract_owner_repo_local(git_link):
    """(owner, repo) from a git URL.

    Duplicated from orchestrator rather than imported: orchestrator imports this
    module, so importing back would be a cycle.
    """
    link = git_link[:-4] if git_link.endswith(".git") else git_link
    parts = link.rstrip("/").split("/")
    return (parts[-2] if len(parts) >= 2 else "", parts[-1] if parts else "")


def fetch_issues_with_retry(cmd, scraper_dir, issues_path, git_link):
    """Run the issue scrape, retrying while it returns nothing it should have.

    Returns True when the CSV ends up trustworthy -- either it has rows, or the
    repository really has no issues.
    """
    for attempt in range(ISSUE_FETCH_ATTEMPTS):
        result = subprocess.run(cmd, cwd=scraper_dir, capture_output=True,
                                text=True, check=True, env=scraper_env())
        logging.info("Command 1 output: " + (result.stdout or ""))
        if issues_have_rows(issues_path):
            return True

        expected = github_issue_count(git_link)
        if expected == 0:
            logging.info("%s has no issues; empty issues CSV is correct.", git_link)
            return True
        if expected is None:
            logging.warning(
                "Issues CSV for %s is empty and the true issue count could not be "
                "checked; accepting it rather than retrying blindly.", git_link)
            return False

        if attempt < ISSUE_FETCH_ATTEMPTS - 1:
            wait = ISSUE_RETRY_WAITS[min(attempt, len(ISSUE_RETRY_WAITS) - 1)]
            logging.warning(
                "Issues CSV for %s is empty but GitHub reports %d issues -- the "
                "scrape was rejected (secondary rate limit). Retry %d/%d in %ds.",
                git_link, expected, attempt + 2, ISSUE_FETCH_ATTEMPTS, wait)
            time.sleep(wait)
        else:
            logging.error(
                "Issues CSV for %s still empty after %d attempts though GitHub "
                "reports %d issues. The social network will be empty.",
                git_link, ISSUE_FETCH_ATTEMPTS, expected)
    return False


def run_rust_code(git_link, function_purpose = 1): #Purpose = 1; it is being run for OSSPREY, otherwise its for other tool
    """
    Given a .git URL, this function:
      1. Ensures the OSS‑Scraper repository is cloned/updated.
      2. Runs `cargo clean` and `cargo build`.
      3. Executes two miner commands to generate CSV outputs.
    Returns a dictionary with the outputs.
    """
    try:
        scraper_dir = OSS_SCRAPER_DIR
        logging.info("OSS‑Scraper directory: " + scraper_dir)

        # Ensure the output folder exists (if not, create it)
        output_folder = os.path.join(scraper_dir, "output")
        if not os.path.exists(output_folder):
            logging.info(f"Output folder {output_folder} does not exist. Creating it.")
            os.makedirs(output_folder, exist_ok=True)
        
        """
        # Please note: This is a blocking operation that may take a while. Only enable for debuggin purposes
        logging.info("Running cargo clean...")
        subprocess.run(["cargo", "clean"], cwd=scraper_dir, check=True)

        # Please note: This is a blocking operation that may take a while. Only enable for debuggin purposes
        logging.info("Running cargo build...")
        build_result = subprocess.run(
            ["cargo", "build"],
            cwd=scraper_dir,
            capture_output=True,
            text=True,
            check=True
        )
        logging.info("Cargo build output: " + build_result.stdout)

        # Please note: This is a blocking operation that may take a while. Only enable for debuggin purposes
        logging.info("Running cargo fix bin biner...")
        build_result = subprocess.run(
            ["cargo", "fix", "--bin", "miner", "--allow-dirty"],
            cwd=scraper_dir,
            capture_output=True,
            text=True,
            check=True
        )
        logging.info("Cargo fix output: " + build_result.stdout)
        """        
        
        # Clear any output left by a previous run before scraping. These files
        # are shared between accounts at mode 664, so if the previous scrape ran
        # as a different user the miner cannot truncate them and fails with
        # EACCES -- but it still exits 0, so the failure surfaces much later as a
        # bogus "Repository is private!". Unlinking works regardless of owner
        # because the directory itself is world-writable.
        repo_stem = git_link.rstrip("/").split("/")[-1].replace(".git", "")
        for stale in (f"{repo_stem}_issues.csv", f"{repo_stem}-commit-file-dev.csv"):
            stale_path = os.path.join(output_folder, stale)
            try:
                os.remove(stale_path)
                logging.info("Removed stale scraper output %s", stale_path)
            except FileNotFoundError:
                pass
            except OSError as e:
                logging.warning("Could not remove stale %s: %s", stale_path, e)

        cmd1 = [
            os.path.join("target", "debug", "miner"),
            "--fetch-github-issues",
            f"--github-url={git_link}",
            "--github-output-folder=output"
        ]
        logging.info("Running command: " + " ".join(cmd1))
        issues_path = os.path.join(output_folder, f"{repo_stem}_issues.csv")
        fetch_issues_with_retry(cmd1, scraper_dir, issues_path, git_link)

        cmd2 = [
            os.path.join("target", "debug", "miner"),
            "--commit-devs-files",
            "--time-window=30",
            "--threads=16",
            "--output-folder=output",
            f"--git-online-url={git_link}"
        ]
        logging.info("Running command: " + " ".join(cmd2))
        cmd2_result = subprocess.run(
            cmd2,
            cwd=scraper_dir,
            capture_output=True,
            text=True,
            check=True,
            env=scraper_env()
        )
        logging.info("Command 2 output: " + cmd2_result.stdout)
        logging.info("Final output directory: " + os.path.abspath(output_folder))

        git_project_cropped_name = "/mnt/data1/OSPEX/root-linode/OSS-scraper/output/"+ git_link.rstrip('/').split('/')[-1].replace('.git','')

        git_project_commit_file_name = git_project_cropped_name+ "-commit-file-dev.csv"
        git_project_issue_file_name = git_project_cropped_name + "_issues.csv"
        print(git_link, git_project_cropped_name, git_project_commit_file_name, git_project_issue_file_name)

        # data = {
        # "commits": pd.read_csv(git_project_commit_file_name).to_dict("records"),
        # "issues": pd.read_csv(git_project_issue_file_name).to_dict("records")
        # }

        # return json.dumps(data, indent=2)
        if function_purpose:
            return {"output_dir": os.path.abspath(output_folder)}
        else:
            return {
            "fetch_github_issues": pd.read_csv(git_project_issue_file_name).to_dict("records"),
            "commit_devs_files": pd.read_csv(git_project_commit_file_name).to_dict("records"),
            }

    except subprocess.CalledProcessError as e:
        logging.error("Rust tool execution failed: " + str(e))
        return {"error": "Rust tool execution failed: " + str(e)}
    except Exception as ex:
        logging.error("Unexpected error: " + str(ex))
        return {"error": "Unexpected error: " + str(ex)}
