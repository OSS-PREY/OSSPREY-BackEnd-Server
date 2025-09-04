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
