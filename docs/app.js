const API_BASE = 'https://ossprey.ngrok.app';
const DEFAULT_ENDPOINT = '/api/users';
const VIEW_COUNT_ENDPOINT = '/api/view_count';

const statusMessage = document.getElementById('status-message');
const tableBody = document.getElementById('users-table-body');
const refreshButton = document.getElementById('refresh-button');
const rowTemplate = document.getElementById('user-row-template');
const viewCountElement = document.getElementById('view-count');

// Refresh button handler
refreshButton.addEventListener('click', () => {
  refreshData();
});

function refreshData() {
  fetchAndRenderUsers();
  fetchAndRenderViewCount();
}

// Fetch and display users
async function fetchAndRenderUsers() {
  updateStatus('Loading registered users…');
  setLoading(true);

  try {
    const response = await fetch(`${API_BASE}${DEFAULT_ENDPOINT}`, {
      headers: { 'Accept': 'application/json' },
    });

    if (!response.ok) {
      throw new Error(`Request failed with status ${response.status}`);
    }

    const data = await response.json();
    const users = Array.isArray(data?.users) ? data.users : [];

    if (!users.length) {
      renderEmptyState('No users registered yet.');
      updateStatus('Fetched successfully but no users were returned.');
      return;
    }

    renderUsers(users);
    updateStatus(`Loaded ${users.length} registered user${users.length > 1 ? 's' : ''}.`);
  } catch (error) {
    console.error('Unable to fetch users:', error);
    renderEmptyState('Failed to load users. Please try again later.');
    updateStatus(`Error: ${error.message}`);
  } finally {
    setLoading(false);
  }
}

function renderUsers(users) {
  tableBody.innerHTML = '';
  const fragment = document.createDocumentFragment();

  users.forEach((user) => {
    const row = rowTemplate.content.cloneNode(true);
    setCellValue(row, 'full_name', user.full_name ?? '—');
    setCellValue(row, 'email', user.email ?? '—');
    setCellValue(row, 'affiliation', user.affiliation ?? '—');
    setCellValue(row, 'referral', user.referral ?? '—');
    setCellValue(row, 'created_at', formatDate(user.created_at));
    fragment.appendChild(row);
  });

  tableBody.appendChild(fragment);
}

function renderEmptyState(message) {
  tableBody.innerHTML = '';
  const emptyRow = document.createElement('tr');
  const cell = document.createElement('td');
  cell.colSpan = 5;
  cell.className = 'placeholder';
  cell.textContent = message;
  emptyRow.appendChild(cell);
  tableBody.appendChild(emptyRow);
}

function setCellValue(rowFragment, field, value) {
  const cell = rowFragment.querySelector(`[data-field="${field}"]`);
  if (cell) {
    cell.textContent = value;
  }
}

function formatDate(value) {
  if (!value) return '—';

  const parsed = new Date(value);
  if (isNaN(parsed.getTime())) return value;

  return `${parsed.toLocaleDateString()} ${parsed.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  })}`;
}

function updateStatus(message) {
  statusMessage.textContent = message;
}

function setLoading(isLoading) {
  refreshButton.disabled = isLoading;
  refreshButton.textContent = isLoading ? 'Loading…' : 'Refresh';
}

async function fetchAndRenderViewCount() {
  if (!viewCountElement) return;

  setViewCountMessage('Fetching OSSPREY Views…');

  try {
    const response = await fetch(`${API_BASE}${VIEW_COUNT_ENDPOINT}`, {
      headers: { 'Accept': 'application/json' },
    });

    if (!response.ok) {
      throw new Error(`Request failed with status ${response.status}`);
    }

    const data = await response.json();
    const viewCount =
      typeof data?.view_count === 'number'
        ? data.view_count
        : typeof data?.views === 'number'
        ? data.views
        : typeof data?.count === 'number'
        ? data.count
        : null;

    if (typeof viewCount !== 'number') {
      throw new Error('Unexpected response payload');
    }

    setViewCountMessage(`OSSPREY Views: ${viewCount.toLocaleString()}`);
  } catch (error) {
    console.error('Unable to fetch view count:', error);
    setViewCountMessage('OSSPREY Views: unavailable');
  }
}

function setViewCountMessage(message) {
  if (viewCountElement) {
    viewCountElement.textContent = message;
  }
}

// Initial load
refreshData();
