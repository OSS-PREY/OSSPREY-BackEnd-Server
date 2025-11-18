const DEFAULT_ENDPOINT = '/api/users';

const apiConfigForm = document.getElementById('api-config-form');
const apiBaseInput = document.getElementById('api-base');
const statusMessage = document.getElementById('status-message');
const tableBody = document.getElementById('users-table-body');
const refreshButton = document.getElementById('refresh-button');
const rowTemplate = document.getElementById('user-row-template');

let apiBase = 'https://ossprey.ngrok.app'
if (apiBase) {
  apiBaseInput.value = apiBase;
}

apiConfigForm.addEventListener('submit', (event) => {
  event.preventDefault();
  apiBase = apiBaseInput.value.trim();
  if (apiBase.endsWith('/')) {
    apiBase = apiBase.slice(0, -1);
  }
  window.localStorage.setItem('ossprey-api-base', apiBase);
  fetchAndRenderUsers();
});

refreshButton.addEventListener('click', () => {
  fetchAndRenderUsers();
});

async function fetchAndRenderUsers() {
  updateStatus('Loading registered users…');
  setLoading(true);
  try {
    const response = await fetch(`${buildBaseUrl()}${DEFAULT_ENDPOINT}`, {
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
    updateStatus(`Loaded ${users.length} registered user${users.length === 1 ? '' : 's'}.`);
  } catch (error) {
    console.error('Unable to fetch users:', error);
    renderEmptyState('Failed to load users. Check the API URL and try again.');
    updateStatus(`Error: ${error.message}`);
  } finally {
    setLoading(false);
  }
}

function buildBaseUrl() {
  if (!apiBase) {
    return window.location.origin;
  }
  return apiBase;
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
  if (!value) {
    return '—';
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }

  return `${parsed.toLocaleDateString()} ${parsed.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
}

function updateStatus(message) {
  statusMessage.textContent = message;
}

function setLoading(isLoading) {
  refreshButton.disabled = isLoading;
  refreshButton.textContent = isLoading ? 'Loading…' : 'Refresh';
}

fetchAndRenderUsers();
