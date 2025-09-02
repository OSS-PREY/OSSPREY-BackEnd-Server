# `/api/process_repo` Endpoint

This endpoint records when a user asks the system to process a GitHub repository.

- **URL**: `/api/process_repo`
- **Method**: `POST`
- **Payload**: JSON object containing:
  - `user_email` (string) – Email address of the requester.
  - `github_repo` (string) – URL or identifier of the repository to process.
  - `timestamp` (string) – Time of the request in ISO‑8601 format (e.g. `2024-06-12T15:32:00Z`).
- **Success Response**: `201 Created` with `{"message": "Repository request recorded."}`
- **Error Responses**:
  - `400 Bad Request` – Missing required fields.
  - `500 Internal Server Error` – Database insertion failed.

## Example

```bash
curl -X POST http://127.0.0.1:5000/api/process_repo \
  -H "Content-Type: application/json" \
  -d '{
        "user_email": "user@example.com",
        "github_repo": "https://github.com/org/repo",
        "timestamp": "2024-06-12T15:32:00Z"
      }'
```

### Sample Successful Response

```json
{ "message": "Repository request recorded." }
```

The request data is stored in the `user_repo_requests` collection in MongoDB with the `timestamp` saved as a native `datetime` value.
