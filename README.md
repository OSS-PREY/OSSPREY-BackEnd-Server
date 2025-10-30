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

### Prerequisites for Chatbot Feature

Before running the backend, make sure you have Ollama installed and the Llama 3.2 1B model downloaded:

#### Install Ollama
Visit [https://ollama.ai](https://ollama.ai) and follow the installation instructions for your operating system.

#### Pull the Llama 3.2 1B Model
```bash
ollama pull llama3.2:1b
```

#### Start Ollama Service
Ollama typically runs as a background service on port 11434. Verify it's running:
```bash
curl http://localhost:11434/api/version
```

### Usage
Running the Flask Application

Start the Flask application using the following command:

```bash
flask run
```
By default, the application will run on http://localhost:5000/.

#### Testing the Chat Endpoint

A test script is provided to verify the chat functionality:

```bash
python test_chat.py
```

This will test both the health check endpoint and the chat endpoint with sample queries.

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

### Chatbot Endpoints

#### Health Check

```bash
GET /api/health
```

- **Description**: Simple health check endpoint to verify the API is running.
- **Response**: `200 OK` with status message.

**Example**

```bash
curl http://127.0.0.1:5000/api/health
```

**Successful Response**

```json
{ "status": "ok" }
```

#### Chat with LLM

```bash
POST /api/chat
```

**Request Body**

```json
{
  "message": "What is CI/CD?",
  "repoName": "apache/airflow"
}
```

- **Description**: Processes user messages using Llama 3.2 1B model via Ollama. The `repoName` field is optional and provides context to the LLM about which repository the user is working on.
- **Response**: `200 OK` with the LLM's response. Returns `400` if message is missing, `500` if Ollama is unavailable or times out.

**Example**

```bash
curl -X POST http://127.0.0.1:5000/api/chat \
  -H "Content-Type: application/json" \
  -d '{
        "message": "How can I improve code quality?",
        "repoName": "apache/airflow"
      }'
```

**Successful Response**

```json
{
  "response": "To improve code quality, consider implementing...",
  "timestamp": "2024-10-03T13:22:00.000000"
}
```

**Error Responses**

- **Model Unavailable** (500):
```json
{
  "error": "Model unavailable. Please ensure Ollama is running."
}
```

- **Missing Message** (400):
```json
{
  "error": "Message is required."
}
```

- **Request Timeout** (500):
```json
{
  "error": "Request timed out. Please try again."
}
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
