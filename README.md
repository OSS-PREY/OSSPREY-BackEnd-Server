# Open source sustainability Web server for OSSPREY


This servers as the host API Web server, providing data for projects belonging to either Apache Software Foundation , or Eclipse Software Foundation. Now, it is also facilitated to support the Local mode for OSPEX (Open source sustainability project explorer), which means, it can process data for any Github repository! Apart from serving Github REST APIs, which fetch social network, technical network, commits history, emails/issues history, graduation forecast, project details, number of senders, total emails/issues, and emails/issues per sender, commits, committers and commits per committer, it also doubles up as the sole point of control where OSPEX functionality is hosted from. This means supporting POST request for Github APIs, orchestrating that different functionalities work together, (ReACTs, RUST scraper and pex-forecaster), it also fetches and stores data to different collections in MongoDB.

## Installation

### Clone the Repository

```bash
git clone https://github.com/OSS-PREY/OSSPREY-BackEnd-Server.git
cd OSS-fetch-github-data
```

### Create a Virtual Environment
It's recommended to use a virtual environment to manage your project's dependencies.

For Unix/Linux/MacOS

```bash
python3 -m venv venv
source venv/bin/activate
```

For Windows
```bash
python -m venv venv
venv\Scripts\activate
```

### Install Dependencies

Install the required Python packages using pip:

```bash
pip install -r requirements.txt
```

### Usage
Running the Flask Application

Start the Flask application using the following command:

```bash
flask run
```
By default, the application will run on http://localhost:5000/.

#### Running with Gunicorn (auto-reload)
For deployments that should automatically restart when source files change, use the included Gunicorn configuration:

```bash
gunicorn -c gunicorn.conf.py run:app
```

The `gunicorn.conf.py` file enables `reload`, so any code updates will trigger a server restart and keep the running app in sync with your latest changes. Override the bind address with the `GUNICORN_BIND` environment variable (e.g. `GUNICORN_BIND=0.0.0.0:5500`).

> **Important — run a single worker.** The repository-processing queue (see *Repository Processing Queue* below) stores its state in memory, so the app **must** run with one worker (`workers = 1`, already set in `gunicorn.conf.py`). Do **not** launch with `-w`/`--workers` greater than 1 or with `--max-requests` — doing so makes status polls hit a different (or recycled) worker that doesn't know the job, returning **HTTP 404** ("Status request failed (404)" in the UI). Because command-line flags override the config file, always prefer `gunicorn -c gunicorn.conf.py run:app`.

### Defined end-points
Access the following endpoint in your web browser or use a tool like curl:

``` bash
http://127.0.0.1:5000/
```


## API Endpoints Documentation

This document provides an overview of the available API endpoints and their functionality.

### User Authentication

#### Register a User

```bash
POST /api/register
```

**Request Body**

```json
{
  "full_name": "Jane Doe",
  "email": "jane@example.com",
  "affiliation": "UC Davis",
  "password": "strongpassword",
  "referral": "Conference Booth"
}
```

- **Description**: Creates a new user account. All fields are required. The password is stored securely using a hash. The server records the registration time in a `registered_at` field.
- **Response**: `201 Created` on success with a confirmation message. Returns `400` if fields are missing or the email is already registered.

**Example**

```bash
curl -X POST http://127.0.0.1:5000/api/register \
  -H "Content-Type: application/json" \
  -d '{
        "full_name": "Jane Doe",
        "email": "jane@example.com",
        "affiliation": "UC Davis",
        "password": "strongpassword",
        "referral": "Conference Booth"
      }'
```

**Successful Response**

```json
{ "message": "User registered successfully." }
```

#### Validate Login

```bash
POST /api/login
```

**Request Body**

```json
{
  "email": "jane@example.com",
  "password": "strongpassword"
}
```

- **Description**: Verifies user credentials.
- **Response**: `200 OK` with a success message when the credentials are valid. Returns `401` for invalid credentials or `400` for incomplete requests.

**Example**

```bash
curl -X POST http://127.0.0.1:5000/api/login \
  -H "Content-Type: application/json" \
  -d '{"email": "jane@example.com", "password": "strongpassword"}'
```

**Successful Response**

```json
{ "message": "Login successful." }
```

#### Track User Login

```bash
POST /api/track_login
```

**Request Body**

```json
{
  "user_email": "jane@example.com"
}
```

- **Description**: Records when a user logs in by storing their email and the current server timestamp in the `login_tracking` collection.
- **Response**: `201 Created` on success with a confirmation message. Returns `400` if `user_email` is missing.

**Example**

```bash
curl -X POST http://127.0.0.1:5000/api/track_login \
  -H "Content-Type: application/json" \
  -d '{"user_email": "jane@example.com"}'
```

**Successful Response**

```json
{ "message": "Login tracked." }
```

#### Track User Logout

```bash
POST /api/track_logout
```

**Request Body**

```json
{
  "user_email": "jane@example.com"
}
```

- **Description**: Records when a user logs out by storing their email and the current server timestamp in the `logout_tracking` collection.
- **Response**: `201 Created` on success with a confirmation message. Returns `400` if `user_email` is missing.

**Example**

```bash
curl -X POST http://127.0.0.1:5000/api/track_logout \
  -H "Content-Type: application/json" \
  -d '{"user_email": "jane@example.com"}'
```

**Successful Response**

```json
{ "message": "Logout tracked." }
```

### Record Repository Processing Request

```bash
POST /api/process_repo
```

**Request Body**

```json
{
  "user_email": "user@example.com",
  "github_repo": "https://github.com/org/repo",
  "timestamp": "2024-06-12T15:32:00Z"
}
```

- **Description**: Records a user's request to process a specific GitHub repository and stores it in the `user_repo_requests` collection in MongoDB.
- **Response**: `201 Created` on success with a confirmation message. Returns `400` if required fields are missing or the timestamp is malformed.

**Example**

```bash
curl -X POST http://127.0.0.1:5000/api/process_repo \
  -H "Content-Type: application/json" \
  -d '{
        "user_email": "user@example.com",
        "github_repo": "https://github.com/org/repo",
        "timestamp": "2024-06-12T15:32:00Z"
      }'
```

**Successful Response**

```json
{ "message": "Repository request recorded." }
```

### Repository Processing Queue (Local Mode)

Processing a GitHub repository runs the full OSSPREY pipeline (RUST scraper →
pex-forecaster → ReACT), which can take several minutes. To avoid overloading
the server, requests are placed in a **FIFO queue** and only a bounded number run
concurrently (default **2**). Instead of blocking until processing finishes, the
upload endpoint returns a **job handle** immediately and the client polls for
status.

**Concurrency configuration** (environment variables):

| Variable | Default | Description |
| --- | --- | --- |
| `MAX_CONCURRENT_JOBS` | `2` | Maximum number of pipeline jobs running at once. |
| `ESTIMATED_JOB_SECONDS` | `120` | Seed estimate (seconds) used for wait-time calculations until real timings are observed. |

> **Important:** The queue is held **in-process**, so the backend must run with a
> **single worker** (`gunicorn.conf.py` sets `workers = 1`). Each worker process
> keeps its own independent queue, so with multiple workers a status poll can land
> on a worker that never saw the job and return **HTTP 404** ("Status request
> failed (404)" in the UI). For the same reason, do **not** pass `--max-requests`
> (recycling a worker mid-job wipes the queue and the running pipeline). To run
> multiple workers you must first move the queue state to a shared store such as
> Redis.

#### Queue a Repository for Processing

```bash
POST /api/upload_git_link
```

**Request Body**

```json
{ "git_link": "https://github.com/owner/repo.git" }
```

- **Description**: Validates the `.git` link and enqueues a processing job.
  Returns immediately without waiting for the pipeline to finish.
- **Response**: `202 Accepted` with the initial job handle. Returns `400` if the
  link is missing or is not a valid `.git` URL.

**Successful Response**

```json
{
  "job_id": "264535f4492c428cad4d8ac3747e1397",
  "status": "queued",
  "position": 1,
  "estimated_wait_seconds": 120,
  "queue_length": 1,
  "running": 2,
  "max_concurrent": 2,
  "created_at": "2024-06-12T15:32:00+00:00",
  "started_at": null,
  "finished_at": null,
  "error": null,
  "metadata": { "git_link": "https://github.com/owner/repo.git" }
}
```

#### Poll Job Status

```bash
GET /api/queue_status/<job_id>
```

- **Description**: Returns the current state of a job. `status` is one of
  `queued`, `running`, `completed`, `failed`, or `cancelled`. While `queued`,
  `position` and `estimated_wait_seconds` indicate where the job sits in line.
  Once the job is `completed` (or `failed`), the response also includes the
  pipeline `result` payload.
- **Response**: `200 OK` with the job snapshot, or `404` if the job id is
  unknown.

**Example**

```bash
curl http://127.0.0.1:5000/api/queue_status/264535f4492c428cad4d8ac3747e1397
```

#### Queue Statistics

```bash
GET /api/queue_stats
```

- **Description**: Returns aggregate queue stats (`running`, `queued`,
  `max_concurrent`, `avg_job_seconds`, `total_jobs`).
- **Response**: `200 OK`.

#### Cancel a Queued Job

```bash
POST /api/cancel_job/<job_id>
```

- **Description**: Cancels a job that has **not started running yet**. Running
  jobs cannot be interrupted. Cancelling keeps the queue consistent and allows
  the next waiting job to start.
- **Response**: `200 OK` with the updated snapshot, or `404` if the job id is
  unknown.

### List Registered Users

```bash
GET /api/users
```

- **Description**: Retrieves all registered users. The response includes each user's email and any other metadata stored in the database.
- **Response**: `200 OK` with a list of user records.

**Example**

```bash
curl http://127.0.0.1:5000/api/users
```

**Successful Response**

```json
{
  "users": [
    {
      "full_name": "Jane Doe",
      "email": "jane@example.com",
      "affiliation": "UC Davis",
      "referral": "Conference Booth",
      "created_at": "2024-06-12T15:32:00"
    }
  ]
}
```

### List User's Processed GitHub Repositories

```bash
GET /api/user_repositories?email=<user_email>
```

- **Description**: Returns all GitHub repositories processed through the system by the specified user.
- **Response**: `200 OK` with a list of repository URLs. Returns `400` if the `email` query parameter is missing.

**Example**

```bash
curl "http://127.0.0.1:5000/api/user_repositories?email=user@example.com"
```

**Successful Response**

```json
{
  "repositories": [
    "https://github.com/org/repo"
  ]
}
```

### Fetching GitHub Repository Data

```bash
GET /api/projects
```
- **Description**: Fetches all GitHub repositories stored under the organization `apache`.

```bash
GET /api/github_stars
```
- **Description**: Fetches stars, forks, and watch information for each GitHub repository.

### Fetching Project Information

```bash
GET /api/project_description
```
- **Description**: Fetches project information such as mentors, project status, etc., from the Apache website for all projects.

```bash
GET /api/project_info
```
- **Description**: Fetches all combined project information from the endpoints above.

### Technical and Social Networks (Month-wise)

```bash
GET /api/tech_net/<project_id>/int:month
```
- **Description**: Fetches the technical network for a specific project, filtered by month.

```bash
GET /api/social_net/<project_id>/int:month
```
- **Description**: Fetches the social network for a specific project, filtered by month.

### Commit and Email Information (Month-wise)

```bash
GET /api/commit_links/<project_id>/int:month
```
- **Description**: Fetches commit information for a specific project, filtered by month.

```bash
GET /api/email_links/<project_id>/int:month
```
- **Description**: Fetches email information for a specific project, filtered by month.

### Commit and Email Measures (Month-wise)

```bash
GET /api/commit_measure/<project_id>/int:month
```
- **Description**: Fetches commit measure information for a specific project, filtered by month.

```bash
GET /api/email_measure/<project_id>/int:month
```
- **Description**: Fetches email measure information for a specific project, filtered by month.

### Fetching Monthly Ranges

```bash
GET /api/monthly_ranges
```
- **Description**: Fetches the monthly range for all available Apache projects.


### View Tracking

#### Record a View

```bash
POST /api/record_view
```
- **Description**: Records the current timestamp each time the endpoint is called and stores it in MongoDB.
- **Example**:

```bash
curl -X POST http://127.0.0.1:5000/api/record_view
```

#### Get View Count

```bash
GET /api/view_count
```
- **Description**: Returns the total number of recorded view timestamps.
- **Example**:

```bash
curl http://127.0.0.1:5000/api/view_count
```


### Notes
- Replace `<project_id>` with the unique identifier for the project.
- Replace `int:month` with the specific month you want to query.

---

## [Feature] Database worker

Run the scripts for uploading data into MongoDB using this command (Please note that this takes in static .json/.csv files from the data folder, available on the server and creates collections accordingly)

``` bash
python3 ./workers/apache_mongo_worker.py
```

### Required

Ensure you have the following installed on your system:

Python 3.10 or higher
pip package manager

### Contributing

Contributions are welcome! Please feel free to open a Pull Request describing your changes. For major changes, please open an issue first to discuss what you'd like to change.

### Contact
If you have any questions or concerns, feel free to contact the current tech lead,  
**Nafiz Imtiaz Khan** ([nikhan@ucdavis.edu](mailto:nikhan@ucdavis.edu)).

For general discussions, contributions, and community updates, join our  
[OSSPREY Slack workspace](https://join.slack.com/t/osspreyworkspace/shared_invite/zt-35bsf2ypc-tS1a5~~n~33FzVUZptKFUA).

### License
This project is licensed under the Apache License 2.0.
