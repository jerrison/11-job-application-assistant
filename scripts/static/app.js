'use strict';

// ── State ────────────────────────────────────────────────────────────────────
window.jobs = {};           // id → job data
window.queueRows = [];      // current queue page
window.workerRows = [];     // current worker snapshot
window.queueCounts = { all: 0, queued: 0, processing: 0, draft: 0, submitted: 0, stopped: 0, archived: 0 };
window.stagedChanges = {};  // fieldName → { original, edited }
let ws = null;
let currentFilter = '';     // status filter
let currentSearch = '';
let currentBoard = '';
let currentJobId = null;    // job id in detail view
let currentTab = 'answers';
let sortField = 'updated_at';
let sortDir = 'desc';       // 'asc' | 'desc'
let workerSortField = 'status';
let workerSortDir = 'asc';  // 'asc' | 'desc'
let workerPanelRunning = false;
let metricsSortField = 'company';
let metricsSortDir = 'asc';
let searchTimer = null;
let selectedIds = new Set();
let queueTotal = 0;
let queueLimit = 200;
let queueOffset = 0;
let queueRequestSerial = 0;
let queueRefreshTimer = null;
let _queueRefreshInFlight = null;
let _queueRefreshQueued = false;
let _queueRefreshPending = false;
let _jobDetailRefreshInFlight = false;
let _actionRequestSerial = 0;

const PROCESSING_STATUSES = new Set([
  'generating', 'resolving', 'submitting', 'autofilling', 'retrying', 'fix_in_progress', 'regenerating', 'reanswering'
]);
const ATTENTION_STATUSES = new Set(['needs_board_url', 'awaiting_captcha']);
const JOB_TAB_MAP = {
  answers: 'tab-answers',
  resume: 'tab-resume',
  'cover-letter': 'tab-cover-letter',
  screenshot: 'tab-screenshot',
  confirmation: 'tab-confirmation',
  logs: 'tab-logs',
  timeline: 'tab-timeline',
  'interview-prep': 'tab-interview-prep',
};
const SCROLLABLE_DOCK_RAILS = [
  ['job-actions-shell', 'action-row'],
  ['job-tabs-shell', 'tab-bar'],
];
const QUEUE_SORT_OPTIONS = [
  { value: 'updated_at', label: 'Updated', defaultDir: 'desc' },
  { value: 'status_entered_at', label: 'Status Entered', defaultDir: 'desc' },
  { value: 'id', label: 'Job ID', defaultDir: 'desc' },
  { value: 'status', label: 'Status', defaultDir: 'asc' },
  { value: 'company', label: 'Company', defaultDir: 'asc' },
  { value: 'role_title', label: 'Role', defaultDir: 'asc' },
  { value: 'board', label: 'Board', defaultDir: 'asc' },
  { value: 'progress', label: 'Progress', defaultDir: 'asc' },
  { value: 'confidence', label: 'Confidence', defaultDir: 'desc' },
];
const WORKER_SORT_OPTIONS = [
  { value: 'status', label: 'Status', defaultDir: 'asc' },
  { value: 'worker_id', label: 'Worker #', defaultDir: 'asc' },
  { value: 'job_id', label: 'Job ID', defaultDir: 'asc' },
  { value: 'company', label: 'Company', defaultDir: 'asc' },
  { value: 'role_title', label: 'Role', defaultDir: 'asc' },
  { value: 'phase', label: 'Phase', defaultDir: 'asc' },
  { value: 'progress', label: 'Progress', defaultDir: 'asc' },
  { value: 'elapsed', label: 'Elapsed', defaultDir: 'desc' },
  { value: 'board', label: 'Board', defaultDir: 'asc' },
];
const QUEUE_SORT_OPTION_BY_VALUE = Object.fromEntries(QUEUE_SORT_OPTIONS.map(option => [option.value, option]));
const WORKER_SORT_OPTION_BY_VALUE = Object.fromEntries(WORKER_SORT_OPTIONS.map(option => [option.value, option]));
const SERVER_SETTINGS_MATERIAL_FIELDS = [
  ['master_resume', 'settings-master-resume'],
  ['work_stories', 'settings-work-stories'],
  ['candidate_context', 'settings-candidate-context'],
  ['application_profile', 'settings-application-profile'],
];
const SERVER_SETTINGS_PROVIDER_FIELDS = [
  ['default_provider', 'settings-default-provider'],
  ['provider_chain', 'settings-provider-chain'],
  ['openai_model', 'settings-openai-model'],
  ['gemini_model', 'settings-gemini-model'],
  ['gemini_flash_model', 'settings-gemini-flash-model'],
  ['codex_model', 'settings-codex-model'],
  ['claude_model', 'settings-claude-model'],
  ['steel_base_url', 'settings-steel-base-url'],
];
const SERVER_SETTINGS_CREDENTIAL_FIELDS = [
  ['openai_api_key', 'settings-openai-api-key'],
  ['openai_api_keys', 'settings-openai-api-keys'],
  ['gemini_api_key', 'settings-gemini-api-key'],
  ['codex_api_key', 'settings-codex-api-key'],
  ['anthropic_api_key', 'settings-anthropic-api-key'],
  ['steel_api_key', 'settings-steel-api-key'],
];
const SERVER_SETTINGS_CREDENTIAL_BY_KEY = Object.fromEntries(
  SERVER_SETTINGS_CREDENTIAL_FIELDS.map(([key, inputId]) => [key, inputId])
);
const SERVER_SETTINGS_MATERIAL_BY_KEY = Object.fromEntries(
  SERVER_SETTINGS_MATERIAL_FIELDS.map(([key, inputId]) => [key, inputId])
);
const SERVER_SETTINGS_MATERIAL_LABELS = {
  master_resume: 'Master resume',
  work_stories: 'Work stories',
  candidate_context: 'Candidate context',
  application_profile: 'Application profile',
};
const STARTER_APPLICATION_PROFILE = [
  '# Application Profile',
  '',
  '## Work Authorization',
  '- Country: ',
  '- Location: ',
  '- Work Authorization Statement: ',
  '- Authorized to Work Unconditionally: ',
  '- Require Sponsorship Now: ',
  '- Require Sponsorship in Future: ',
  '',
  '## Optional',
  '- LinkedIn: ',
  '- GitHub: ',
  '- Website: ',
  '- Verification Code Email: ',
].join('\n');
let _serverSettingsCache = null;
let _serverSettingsLoadPromise = null;
let _pendingCredentialClears = new Set();
let _bootstrapState = null;
let _bootstrapLoadPromise = null;
let _realtimeAppServicesStarted = false;

// ── Utilities ────────────────────────────────────────────────────────────────

function escapeHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function mergeJobState(previousJob, nextJob) {
  const previous = previousJob && typeof previousJob === 'object' ? previousJob : {};
  const next = nextJob && typeof nextJob === 'object' ? nextJob : {};
  return { ...previous, ...next };
}

function initScrollableDockRails() {
  SCROLLABLE_DOCK_RAILS.forEach(([shellId, scrollerId]) => {
    const shell = document.getElementById(shellId);
    const scroller = document.getElementById(scrollerId);
    if (!shell || !scroller || shell.dataset.railBound === 'true') return;

    const refresh = () => {
      const maxScroll = Math.max(0, scroller.scrollWidth - scroller.clientWidth);
      shell.classList.toggle('has-left-overflow', scroller.scrollLeft > 4);
      shell.classList.toggle('has-right-overflow', maxScroll - scroller.scrollLeft > 4);
    };

    shell.dataset.railBound = 'true';
    shell._refreshRail = refresh;
    scroller.addEventListener('scroll', refresh, { passive: true });
    window.addEventListener('resize', refresh);
    requestAnimationFrame(refresh);
  });
}

function refreshScrollableDockRails() {
  SCROLLABLE_DOCK_RAILS.forEach(([shellId]) => {
    const shell = document.getElementById(shellId);
    if (shell && typeof shell._refreshRail === 'function') {
      shell._refreshRail();
    }
  });
}

function statusClass(status) {
  if (!status) return '';
  if (status === 'approved') return 'status-approved';
  if (PROCESSING_STATUSES.has(status)) return 'status-generating';
  if (_isStopped(status)) return 'status-failed';
  if (status === 'awaiting_captcha') return 'status-awaiting-captcha';
  return 'status-' + status;
}

function statusLabel(status) {
  if (!status) return '';
  const map = {
    queued: 'Queued',
    queued_submit: 'Queued',
    generating: 'Processing',
    resolving: 'Processing',
    autofilling: 'Autofilling',
    approved: 'Approved',
    submitting: 'Submitting',
    reanswering: 'Processing',
    regenerating: 'Regenerating',
    retrying: 'Processing',
    fix_in_progress: 'Processing',
    draft: 'Draft',
    submitted: 'Submitted',
    stopped: 'Stopped',
    needs_board_url: 'Needs URL',
    awaiting_captcha: 'Awaiting Captcha',
    archived: 'Archived',
  };
  return map[status] || status;
}

function submissionLockLabel(job) {
  if (!job.previously_submitted) return '';
  if (job.submission_lock_state === 'locked') return 'Locked';
  if (job.submission_lock_state === 'unlocked_for_resubmit') return 'Unlocked for resubmit';
  return '';
}

function isSubmissionLocked(job) {
  return !!job && job.submission_lock_state === 'locked';
}

function selectedJobsContainLockedSubmission() {
  return [...selectedIds].some(id => isSubmissionLocked(window.jobs[id]));
}

function parseUtcTimestamp(value) {
  if (!value) return null;
  const raw = String(value).trim();
  if (!raw) return null;
  let normalized = raw.replace(' UTC', '');
  const hasTimezone = /(?:Z|[+-]\d{2}:\d{2})$/.test(normalized);
  if (normalized.includes(' ') && !normalized.includes('T')) {
    normalized = normalized.replace(' ', 'T');
  }
  if (!hasTimezone) normalized += 'Z';
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

function timeAgo(dateStr) {
  if (!dateStr) return '';
  const d = parseUtcTimestamp(dateStr);
  if (!d) return dateStr;
  const diffMs = Date.now() - d.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60)  return diffSec + 's ago';
  if (diffSec < 3600) return Math.floor(diffSec / 60) + 'm ago';
  if (diffSec < 86400) return Math.floor(diffSec / 3600) + 'h ago';
  return Math.floor(diffSec / 86400) + 'd ago';
}

function formatUtcTimestamp(value) {
  if (!value) return '';
  const date = parseUtcTimestamp(value);
  if (!date) return String(value);
  return date.toISOString().replace(/\.\d{3}Z$/, 'Z').replace('T', ' ').replace('Z', ' UTC');
}

function queueTimestamp(job) {
  if (job.status_entered_at) return job.status_entered_at;
  if (job.queue_timestamp) return job.queue_timestamp;
  if (job.status === 'submitted' && job.completed_at) return job.completed_at;
  return job.updated_at || job.created_at || job.completed_at;
}

function _sortButtonLabel(direction) {
  return direction === 'asc' ? 'Ascending' : 'Descending';
}

function _syncSortableHeaders(selector, activeField, activeDir) {
  document.querySelectorAll(selector).forEach(header => {
    const field = header.dataset.sortField || '';
    const isActive = field === activeField;
    header.classList.toggle('sort-asc', isActive && activeDir === 'asc');
    header.classList.toggle('sort-desc', isActive && activeDir === 'desc');
    header.setAttribute('aria-sort', isActive ? (activeDir === 'asc' ? 'ascending' : 'descending') : 'none');
  });
}

function _syncQueueSortControls() {
  const fieldEl = document.getElementById('queue-sort-field');
  if (fieldEl) fieldEl.value = sortField;
  _syncSortableHeaders('#job-table thead th[data-sort-field]', sortField, sortDir);
  const dirBtn = document.getElementById('queue-sort-dir-btn');
  if (!dirBtn) return;
  dirBtn.textContent = _sortButtonLabel(sortDir);
  dirBtn.setAttribute('aria-label', 'Queue sort ' + _sortButtonLabel(sortDir).toLowerCase());
  dirBtn.title = 'Queue sort ' + _sortButtonLabel(sortDir).toLowerCase();
}

function _syncWorkerSortControls() {
  const fieldEl = document.getElementById('worker-sort-field');
  if (fieldEl) fieldEl.value = workerSortField;
  _syncSortableHeaders('#worker-table thead th[data-sort-field]', workerSortField, workerSortDir);
  const dirBtn = document.getElementById('worker-sort-dir-btn');
  if (!dirBtn) return;
  dirBtn.textContent = _sortButtonLabel(workerSortDir);
  dirBtn.setAttribute('aria-label', 'Worker sort ' + _sortButtonLabel(workerSortDir).toLowerCase());
  dirBtn.title = 'Worker sort ' + _sortButtonLabel(workerSortDir).toLowerCase();
}

function syncSearchClearButton() {
  const input = document.getElementById('search-input');
  const button = document.getElementById('search-clear-btn');
  if (!input || !button) return;
  const hasValue = Boolean(input.value.trim());
  button.hidden = !hasValue;
  button.disabled = !hasValue;
}

function clearSearchInput() {
  clearTimeout(searchTimer);
  const input = document.getElementById('search-input');
  if (!input) return;
  const hadValue = Boolean(input.value.trim()) || Boolean(currentSearch);
  input.value = '';
  currentSearch = '';
  queueOffset = 0;
  syncSearchClearButton();
  input.focus();
  if (hadValue) refreshQueueData({ showLoading: true });
}

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.classList.add('toast-fade');
    setTimeout(() => toast.remove(), 350);
  }, 3000);
}

function nextActionRequestId() {
  _actionRequestSerial += 1;
  return `jobapps-${Date.now()}-${_actionRequestSerial}`;
}

function createActionContext(surface, trigger) {
  return { surface, trigger, requestId: nextActionRequestId() };
}

async function apiCall(method, path, body, actionContext = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (actionContext && typeof actionContext === 'object') {
    if (actionContext.surface) opts.headers['X-Jobapps-Action-Surface'] = actionContext.surface;
    if (actionContext.trigger) opts.headers['X-Jobapps-Action-Trigger'] = actionContext.trigger;
    if (actionContext.requestId) opts.headers['X-Jobapps-Request-Id'] = actionContext.requestId;
  }
  if (body != null) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const err = (await res.json()).detail;
      detail = typeof err === 'string' ? err : JSON.stringify(err);
    } catch (_) {}
    throw new Error(detail);
  }
  try { return await res.json(); } catch (_) { return null; }
}

function _queueCountDefaults() {
  return { all: 0, queued: 0, processing: 0, draft: 0, submitted: 0, stopped: 0, archived: 0 };
}

function _setQueueRows(jobs) {
  window.queueRows = Array.isArray(jobs) ? jobs : [];
  window.queueRows.forEach(job => {
    window.jobs[job.id] = mergeJobState(window.jobs[job.id], job);
  });
}

function _queueParams() {
  const params = new URLSearchParams();
  if (currentFilter) params.set('status', currentFilter);
  if (currentBoard) params.set('board', currentBoard);
  if (currentSearch) params.set('search', currentSearch);
  params.set('sort_field', sortField);
  params.set('sort_dir', sortDir);
  params.set('limit', String(queueLimit));
  params.set('offset', String(queueOffset));
  return params;
}

async function refreshQueueData({ showLoading = false } = {}) {
  if (_queueRefreshInFlight) {
    _queueRefreshPending = true;
    _queueRefreshQueued = _queueRefreshQueued || showLoading;
    return _queueRefreshInFlight;
  }

  const tbody = document.getElementById('job-tbody');
  const requestSerial = ++queueRequestSerial;
  _queueRefreshInFlight = (async () => {
    if (showLoading && tbody && !window.queueRows.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="8">Loading...</td></tr>';
    }
    try {
      const data = await apiCall('GET', '/api/queue?' + _queueParams().toString());
      if (requestSerial !== queueRequestSerial) return;
      if (queueOffset > 0 && data.total > 0 && (!data.jobs || !data.jobs.length)) {
        queueOffset = Math.max(0, queueOffset - queueLimit);
        refreshQueueData();
        return;
      }
      _setQueueRows(data.jobs || []);
      window.queueCounts = { ..._queueCountDefaults(), ...(data.counts || {}) };
      queueTotal = data.total || 0;
      const visibleIds = new Set(window.queueRows.map(job => job.id));
      selectedIds = new Set([...selectedIds].filter(id => visibleIds.has(id)));
      renderQueue();
    } catch (e) {
      showToast('Queue load error: ' + e.message, 'error');
      if (tbody && !window.queueRows.length) {
        tbody.innerHTML = '<tr class="empty-row"><td colspan="8">Failed to load queue.</td></tr>';
      }
    }
  })();

  try {
    return await _queueRefreshInFlight;
  } finally {
    _queueRefreshInFlight = null;
    if (_queueRefreshPending) {
      const queuedShowLoading = _queueRefreshQueued;
      _queueRefreshPending = false;
      _queueRefreshQueued = false;
      refreshQueueData({ showLoading: queuedShowLoading });
    }
  }
}

async function refreshCurrentJobDetail() {
  if (_activeView() !== 'job' || !currentJobId || _jobDetailRefreshInFlight) return;
  _jobDetailRefreshInFlight = true;
  try {
    const updated = await apiCall('GET', `/api/jobs/${currentJobId}`);
    if (updated) updateJobDetailHeader(updated);
  } catch (_) {
    // Best-effort passive refresh only.
  } finally {
    _jobDetailRefreshInFlight = false;
  }
}

function refreshPassiveViewData() {
  if (_activeView() === 'queue') {
    refreshQueueData();
    return;
  }
  if (_activeView() === 'job' && currentJobId) {
    refreshCurrentJobDetail();
  }
}

function scheduleQueueRefresh() {
  clearTimeout(queueRefreshTimer);
  queueRefreshTimer = setTimeout(() => {
    if (_activeView() === 'queue') refreshQueueData();
  }, 150);
}

function renderQueuePagination() {
  const info = document.getElementById('queue-page-info');
  const prevBtn = document.getElementById('queue-prev-btn');
  const nextBtn = document.getElementById('queue-next-btn');
  if (!info || !prevBtn || !nextBtn) return;

  if (!queueTotal) {
    info.textContent = '0 jobs';
    prevBtn.disabled = true;
    nextBtn.disabled = true;
    return;
  }

  const start = queueOffset + 1;
  const end = Math.min(queueOffset + window.queueRows.length, queueTotal);
  info.textContent = `${start}-${end} of ${queueTotal}`;
  prevBtn.disabled = queueOffset <= 0;
  nextBtn.disabled = queueOffset + queueLimit >= queueTotal;
}

function nextQueuePage() {
  if (queueOffset + queueLimit >= queueTotal) return;
  queueOffset += queueLimit;
  refreshQueueData({ showLoading: true });
}

function prevQueuePage() {
  if (queueOffset <= 0) return;
  queueOffset = Math.max(0, queueOffset - queueLimit);
  refreshQueueData({ showLoading: true });
}

function _jobDetailRoute(hash = location.hash.slice(1)) {
  if (hash.startsWith('job-modal/')) {
    return { jobId: parseInt(hash.split('/')[1], 10), modal: true };
  }
  if (hash.startsWith('job/')) {
    return { jobId: parseInt(hash.split('/')[1], 10), modal: false };
  }
  return null;
}

function _showJobDetailModalChrome() {
  document.getElementById('job-detail-modal-backdrop').style.display = 'block';
  document.getElementById('view-job').classList.add('job-detail-modal');
  document.body.classList.add('job-detail-modal-open');
}

function _focusJobDetailModal() {
  const detailView = document.getElementById('view-job');
  if (!detailView) return;
  detailView.tabIndex = -1;
  requestAnimationFrame(() => {
    try { detailView.focus({ preventScroll: true }); }
    catch (_error) { detailView.focus(); }
  });
}

function _hideJobDetailModalChrome() {
  const backdrop = document.getElementById('job-detail-modal-backdrop');
  const detailView = document.getElementById('view-job');
  if (backdrop) backdrop.style.display = 'none';
  if (detailView) detailView.classList.remove('job-detail-modal');
  document.body.classList.remove('job-detail-modal-open');
}

function _isJobDetailModalOpen() {
  return location.hash.startsWith('#job-modal/');
}

function openJobDetailModal(jobId) {
  if (!jobId) return;
  location.hash = '#job-modal/' + jobId;
}

function openJobDetailModalFromEvent(event, jobId) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  openJobDetailModal(jobId);
}

function closeJobDetailModal() {
  if (_isJobDetailModalOpen()) {
    location.hash = '#queue';
    return;
  }
  _hideJobDetailModalChrome();
}

// ── Hash Router ──────────────────────────────────────────────────────────────
function navigate() {
  const hash = location.hash.slice(1) || 'queue';
  if (_bootstrapState && !_bootstrapOnboardingComplete() && hash !== 'settings') {
    if (location.hash !== '#settings') {
      location.hash = '#settings';
      return;
    }
  }
  const jobRoute = _jobDetailRoute(hash);
  const queueView = document.getElementById('view-queue');
  const queueWasVisible = !!queueView && queueView.style.display !== 'none';

  document.querySelectorAll('main > section').forEach(s => (s.style.display = 'none'));
  document.querySelectorAll('.nav-link').forEach(a => a.classList.remove('active'));
  _hideJobDetailModalChrome();

  if (jobRoute) {
    const { jobId, modal } = jobRoute;
    if (modal) {
      if (queueView) queueView.style.display = 'block';
      const queueNavLink = document.querySelector('.nav-link[href="#queue"]');
      if (queueNavLink) queueNavLink.classList.add('active');
      if (!queueWasVisible) {
        renderQueue();
        if (!window.queueRows.length) refreshQueueData();
      }
    }
    document.getElementById('view-job').style.display = 'block';
    if (modal) _showJobDetailModalChrome();
    const previousJobId = currentJobId;
    currentJobId = jobId;
    // Reset staged changes when switching jobs
    if (previousJobId !== jobId) {
      window.stagedChanges = {};
      updateChangesBar();
    }
    renderJobDetail(jobId);
    if (modal) _focusJobDetailModal();
    document.getElementById('nav-links').classList.remove('open');
  } else {
    const viewId = 'view-' + hash;
    const el = document.getElementById(viewId);
    if (el) {
      el.style.display = 'block';
      const navLink = document.querySelector(`.nav-link[href="#${hash}"]`);
      if (navLink) navLink.classList.add('active');
    }
    if (hash === 'queue') {
      if (!queueWasVisible) renderQueue();
      if (!queueWasVisible || !window.queueRows.length) refreshQueueData();
      else updateStatusBadges();
    }
    if (hash === 'dashboard') renderDashboard();
    if (hash === 'stats')     renderStats();
    if (hash === 'settings')  initSettings();
    if (hash === 'urls')      renderUrlsView();
    if (hash === 'discover')  renderDiscover();
    // Close mobile menu on navigate
    document.getElementById('nav-links').classList.remove('open');
  }
}
window.addEventListener('hashchange', navigate);

// ── WebSocket ────────────────────────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch (_) { return; }

    switch (msg.type) {
      case 'job_bulk':
        if (!window.queueRows.length && !currentFilter && !currentSearch && !currentBoard && queueOffset === 0) {
          _setQueueRows(msg.jobs || []);
          if (!queueTotal) queueTotal = window.queueRows.length;
          renderQueue();
        }
        break;
      case 'job_update':
        window.jobs[msg.job.id] = mergeJobState(window.jobs[msg.job.id], msg.job);
        updateQueueRow(window.jobs[msg.job.id]);
        if (_activeView() === 'job' && currentJobId === msg.job.id) {
          updateJobDetailHeader(window.jobs[msg.job.id]);
        }
        break;
      case 'job_deleted':
        delete window.jobs[msg.id];
        removeQueueRow(msg.id);
        break;
      case 'worker_status':
        updateWorkerButton(msg.running, msg.active_jobs);
        break;
      case 'worker_detail':
        renderWorkerPanel(msg.workers, msg.running);
        break;
      case 'stats_update':
        if (location.hash === '#dashboard') renderDashboard();
        break;
    }
    updateStatusBadges();
  };

  ws.onclose = () => setTimeout(connectWS, 3000);
  ws.onerror = () => ws.close();
}

// ── Worker Control ───────────────────────────────────────────────────────────
function updateWorkerButton(running, activeJobs) {
  const btn  = document.getElementById('worker-btn');
  const text = document.getElementById('worker-status-text');
  if (!btn) return;
  if (running) {
    btn.className = 'worker-btn worker-on';
    text.textContent = 'ON';
  } else {
    btn.className = 'worker-btn worker-off';
    text.textContent = 'OFF';
  }
  // Show/hide the worker controls panel based on running state
  const workerPanel = document.getElementById('worker-panel');
  if (workerPanel) {
    if (running) {
      workerPanel.style.display = 'block';
    } else {
      workerPanel.style.display = 'none';
    }
  }
}

async function killServer() {
  try { await apiCall('POST', '/api/kill'); } catch (_) {}
  document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;font-size:18px;color:#666;">Server killed. Run <code style="margin-left:8px;background:#eee;padding:4px 8px;border-radius:4px;">job-assets web</code> to restart.</div>';
}

async function restartServer() {
  showToast('Restarting server...', 'info');
  try {
    await apiCall('POST', '/api/restart');
  } catch (_) { /* server dies before responding */ }
  // Wait for server to come back, then reload
  const poll = setInterval(async () => {
    try {
      await fetch('/api/health');
      clearInterval(poll);
      window.location.reload();
    } catch (_) { /* still restarting */ }
  }, 500);
}

async function toggleWorker() {
  try {
    const { running } = await apiCall('GET', '/api/workers/status');
    if (running) {
      await apiCall('POST', '/api/workers/stop');
      showToast('Worker stopped', 'info');
    } else {
      await apiCall('POST', '/api/workers/start');
      showToast('Worker started', 'success');
    }
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

// ── Worker Panel ─────────────────────────────────────────────────────────────

let _workerPanelCollapsed = false;

function toggleWorkerPanel() {
  _workerPanelCollapsed = !_workerPanelCollapsed;
  const body = document.getElementById('worker-panel-body');
  const toggle = document.getElementById('worker-panel-toggle');
  if (body) body.classList.toggle('collapsed', _workerPanelCollapsed);
  if (toggle) toggle.classList.toggle('collapsed', _workerPanelCollapsed);
}

function _phaseToPercent(phase) {
  const map = {
    resolving: 15,
    generating: 40,
    approved: 65,
    autofilling: 70,
    submitting: 70,
    reanswering: 70,
    retrying: 80,
    fix_in_progress: 85,
    draft: 95,
    submitted: 100,
  };
  return map[phase] || 0;
}

function _phasePipeline(phase, autoSubmit) {
  const lastLabel = (phase === 'submitting' || autoSubmit) ? 'Submit' : 'Draft';
  const steps = [
    { key: 'resolving', label: 'Resolve' },
    { key: 'generating', label: 'Generate' },
    { key: 'autofilling', label: 'Autofill' },
    { key: 'submitting', label: lastLabel },
  ];
  // Map some phases to their step
  const phaseToStep = {
    resolving: 'resolving',
    generating: 'generating',
    autofilling: 'autofilling',
    submitting: 'submitting',
    reanswering: 'autofilling',
    retrying: 'autofilling',
    fix_in_progress: 'autofilling',
  };
  const activeStep = phaseToStep[phase] || '';
  let foundActive = false;
  return '<div class="phase-pipeline">' + steps.map((s, i) => {
    let cls = 'phase-step';
    if (s.key === activeStep) {
      cls += ' active';
      foundActive = true;
    } else if (!foundActive && activeStep) {
      // Steps before the active one are completed
      const stepIdx = steps.findIndex(x => x.key === activeStep);
      if (i < stepIdx) cls += ' completed';
    }
    const arrow = i < steps.length - 1 ? '<span class="phase-arrow">&rarr;</span>' : '';
    return '<span class="' + cls + '">' + s.label + '</span>' + arrow;
  }).join('') + '</div>';
}

function _elapsedStr(startTime) {
  if (!startTime) return '-';
  const start = new Date(startTime.endsWith('Z') ? startTime : startTime + 'Z');
  if (isNaN(start)) return '-';
  const diff = Math.floor((Date.now() - start.getTime()) / 1000);
  if (diff < 0) return '-';
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  if (m > 0) return m + 'm ' + s + 's';
  return s + 's';
}

function _elapsedSeconds(startTime) {
  if (!startTime) return null;
  const start = parseUtcTimestamp(startTime);
  if (!start) return null;
  const diff = Math.floor((Date.now() - start.getTime()) / 1000);
  return diff >= 0 ? diff : null;
}

function _compareSortValues(left, right, direction) {
  const leftMissing = left == null || left === '';
  const rightMissing = right == null || right === '';
  if (leftMissing !== rightMissing) return leftMissing ? 1 : -1;
  if (leftMissing && rightMissing) return 0;
  let a = left;
  let b = right;
  if (typeof a === 'string') a = a.toLocaleLowerCase();
  if (typeof b === 'string') b = b.toLocaleLowerCase();
  if (a < b) return direction === 'asc' ? -1 : 1;
  if (a > b) return direction === 'asc' ? 1 : -1;
  return 0;
}

function _workerSortValue(worker, field) {
  const statusRank = { busy: 0, idle: 1, stopping: 2 };
  const phaseRank = {
    resolving: 0,
    generating: 1,
    autofilling: 2,
    reanswering: 2,
    retrying: 2,
    fix_in_progress: 2,
    submitting: 3,
    draft: 4,
    submitted: 5,
  };
  switch (field) {
    case 'status':
      return statusRank[worker.status] ?? 99;
    case 'worker_id':
      return worker.worker_id ?? null;
    case 'job_id':
      return worker.job_id ?? null;
    case 'company':
      return worker.company || null;
    case 'role_title':
      return worker.role_title || null;
    case 'phase':
      return phaseRank[worker.phase] ?? null;
    case 'progress':
      return worker.progress || null;
    case 'elapsed':
      return _elapsedSeconds(worker.start_time);
    case 'board':
      return worker.board || null;
    default:
      return worker[field] ?? null;
  }
}

function _sortedWorkerRows(workers) {
  return [...workers].sort((left, right) => {
    const cmp = _compareSortValues(
      _workerSortValue(left, workerSortField),
      _workerSortValue(right, workerSortField),
      workerSortDir,
    );
    if (cmp !== 0) return cmp;
    return _compareSortValues(left.worker_id ?? 0, right.worker_id ?? 0, 'asc');
  });
}

function setWorkerSortField(value) {
  const option = WORKER_SORT_OPTION_BY_VALUE[value];
  if (!option) return;
  workerSortField = option.value;
  workerSortDir = option.defaultDir || 'asc';
  _syncWorkerSortControls();
  renderWorkerPanel(window.workerRows, workerPanelRunning);
}

function toggleWorkerSortDir() {
  workerSortDir = workerSortDir === 'asc' ? 'desc' : 'asc';
  _syncWorkerSortControls();
  renderWorkerPanel(window.workerRows, workerPanelRunning);
}

function handleWorkerHeaderSort(field) {
  if (workerSortField === field) {
    toggleWorkerSortDir();
    return;
  }
  setWorkerSortField(field);
}

function renderWorkerPanel(workers, running) {
  window.workerRows = Array.isArray(workers) ? workers : [];
  workerPanelRunning = !!running;
  _syncWorkerSortControls();
  const panel = document.getElementById('worker-panel');
  if (!panel) return;

  // Show panel when workers are running
  if (running && window.workerRows.length > 0) {
    panel.style.display = 'block';
  } else if (!running) {
    panel.style.display = 'none';
    return;
  }

  // Update count
  const countEl = document.getElementById('worker-count-display');
  if (countEl) countEl.textContent = window.workerRows.length;

  const tbody = document.getElementById('worker-tbody');
  if (!tbody) return;

  if (!window.workerRows.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--base1);padding:12px;">No workers registered</td></tr>';
    return;
  }

  const sorted = _sortedWorkerRows(window.workerRows);

  tbody.innerHTML = sorted.map(w => {
    const wid = w.worker_id || '?';
    const status = w.status || 'idle';
    const statusBadge = '<span class="worker-status-badge worker-status-' + status + '">' +
      escapeHtml(status) + '</span>';

    let jobCell = '-';
    if (w.job_id && status === 'busy') {
      const company = escapeHtml(w.company || '?');
      const role = escapeHtml(w.role_title || '?');
      jobCell = '<a href="#job/' + w.job_id + '">' + company + ' &mdash; ' + role +
        ' <span style="color:var(--base1)">#' + w.job_id + '</span></a>';
    }

    const phaseCell = status === 'busy' ? _phasePipeline(w.phase, w.auto_submit) : '-';
    const pct = status === 'busy' ? _phaseToPercent(w.phase) : 0;
    const progressText = w.progress ? escapeHtml(w.progress) : '';
    const progressCell = status === 'busy'
      ? '<div class="worker-progress"><div class="worker-progress-inner" style="width:' +
        pct + '%"></div></div> <span style="font-size:10px;color:var(--base01)">' +
        progressText + '</span>'
      : '-';

    const elapsed = status === 'busy' ? _elapsedStr(w.start_time) : '-';
    const board = w.board ? escapeHtml(w.board) : '-';

    let actions = '';
    if (status === 'busy') {
      actions = '<button class="worker-action-btn" onclick="stopSingleWorker(' + wid +
        ')" title="Graceful stop">Stop</button>' +
        '<button class="worker-action-btn kill" onclick="killSingleWorker(' + wid +
        ')" title="Force kill + requeue">Kill</button>';
    } else if (status === 'idle') {
      actions = '<button class="worker-action-btn" onclick="stopSingleWorker(' + wid +
        ')" title="Stop this worker">Stop</button>';
    } else {
      actions = '<span style="color:var(--base1);font-size:11px">stopping...</span>';
    }

    return '<tr>' +
      '<td>' + wid + '</td>' +
      '<td>' + statusBadge + '</td>' +
      '<td>' + jobCell + '</td>' +
      '<td>' + phaseCell + '</td>' +
      '<td>' + progressCell + '</td>' +
      '<td>' + elapsed + '</td>' +
      '<td>' + board + '</td>' +
      '<td>' + actions + '</td>' +
      '</tr>';
  }).join('');
}

async function startAllWorkers() {
  try {
    await apiCall('POST', '/api/workers/start');
    showToast('Workers started', 'success');
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

async function stopAllWorkers() {
  try {
    await apiCall('POST', '/api/workers/stop');
    showToast('Workers stopped', 'info');
    // Pessimistically update local state — WS will confirm within 2s
    for (const job of Object.values(window.jobs)) {
      if (job.status === 'awaiting_captcha') {
        job.status = 'stopped';
      }
    }
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

async function restartAllWorkers() {
  try {
    await apiCall('POST', '/api/workers/restart');
    showToast('Workers restarted', 'success');
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

async function stopSingleWorker(workerId) {
  try {
    await apiCall('POST', '/api/workers/' + workerId + '/stop');
    showToast('Worker ' + workerId + ' stop requested', 'info');
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

async function killSingleWorker(workerId) {
  if (!confirm('Kill worker ' + workerId + '? Its current job will be requeued.')) return;
  try {
    await apiCall('POST', '/api/workers/' + workerId + '/kill');
    showToast('Worker ' + workerId + ' killed, job requeued', 'info');
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

// ── Status badge counts ──────────────────────────────────────────────────────
function _isStopped(s) {
  return s === 'stopped' || ATTENTION_STATUSES.has(s);
}

function updateStatusBadges() {
  const counts = { ..._queueCountDefaults(), ...(window.queueCounts || {}) };
  for (const [key, val] of Object.entries(counts)) {
    const el = document.getElementById('badge-count-' + key);
    if (el) el.textContent = val;
  }
}

function setStatusFilter(status) {
  currentFilter = status;
  queueOffset = 0;
  document.querySelectorAll('#badge-bar .badge-pill').forEach(p => {
    p.classList.toggle('active', p.dataset.status === status);
  });
  refreshQueueData({ showLoading: true });
}

function debounceSearch() {
  syncSearchClearButton();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    currentSearch = document.getElementById('search-input').value.trim();
    syncSearchClearButton();
    queueOffset = 0;
    refreshQueueData({ showLoading: true });
  }, 220);
}

function setBoardFilter() {
  currentBoard = document.getElementById('board-filter').value;
  queueOffset = 0;
  refreshQueueData({ showLoading: true });
}

function getFilteredJobs() {
  return Array.isArray(window.queueRows) ? [...window.queueRows] : [];
}

// ── Queue View ───────────────────────────────────────────────────────────────
function renderQueue() {
  const tbody = document.getElementById('job-tbody');
  if (!tbody) return;
  const jobs = getFilteredJobs();
  _syncQueueSortControls();
  syncSearchClearButton();
  updateStatusBadges();
  renderQueuePagination();

  if (jobs.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="5">No jobs found.</td></tr>';
    updateBulkActionsBar();
    return;
  }

  const rows = jobs.map(job => buildQueueRow(job)).join('');
  tbody.innerHTML = rows;

  // Restore checkbox states
  jobs.forEach(job => {
    const cb = tbody.querySelector(`input[data-id="${job.id}"]`);
    if (cb) cb.checked = selectedIds.has(job.id);
  });

  updateBulkActionsBar();
}

function updateQueueRow(job) {
  const existingIndex = window.queueRows.findIndex(row => row.id === job.id);
  if (existingIndex >= 0) window.queueRows[existingIndex] = job;
  if (document.getElementById('view-queue')?.style.display !== 'none') scheduleQueueRefresh();
}

function removeQueueRow(id) {
  if (!window.jobs[id]) return; // already handled (dedup modal or prior WS)
  delete window.jobs[id];
  selectedIds.delete(id);
  window.queueRows = window.queueRows.filter(job => job.id !== id);
  if (document.getElementById('view-queue')?.style.display !== 'none') scheduleQueueRefresh();
  else {
    renderQueue();
    updateStatusBadges();
  }
}

function _jobUrlIcons(job) {
  const parts = [];
  const src = job.source_url || job.url || '';
  const board = job.board_url || job.canonical_url || '';
  const linkStyle = 'display:inline-flex;align-items:center;justify-content:center;min-width:36px;height:32px;padding:0 8px;border-radius:5px;font-size:14px;font-weight:600;text-decoration:none;';
  if (src.includes('linkedin.com')) {
    parts.push(`<a href="${escapeHtml(src)}" target="_blank" rel="noopener" title="LinkedIn"
      style="${linkStyle}color:#0077b5;background:#e8f4fd;margin-right:6px">in</a>`);
  }
  if (board && board !== src) {
    parts.push(`<a href="${escapeHtml(board)}" target="_blank" rel="noopener" title="Job Board"
      style="${linkStyle}color:var(--base00);background:var(--base2)">\u2197</a>`);
  } else if (!parts.length && src) {
    parts.push(`<a href="${escapeHtml(src)}" target="_blank" rel="noopener" title="Job URL"
      style="${linkStyle}color:var(--base00);background:var(--base2)">\u2197</a>`);
  }
  return `<div style="display:flex;gap:4px;align-items:center">${parts.join('')}</div>`;
}

function _queueReviewSummary(job) {
  if (job && job.queue_review_summary) return job.queue_review_summary;
  return {
    overall_confidence: 'na',
    confidence_label: 'No review data',
    reason_chips: [],
    visible_actions: [],
  };
}

function _queueConfidenceLabel(level) {
  const labels = {
    high: 'High',
    medium: 'Medium',
    low: 'Low',
    pending: 'Pending',
    na: 'N/A',
  };
  return labels[level] || 'N/A';
}

function buildQueueJobCell(job) {
  const sClass = statusClass(job.status);
  const sLabel = statusLabel(job.status);
  const company = escapeHtml(job.company || '—');
  const role = escapeHtml(job.role_title || '—');
  const board = escapeHtml(job.board || '—');
  const progress = job.progress ? `<div class="progress-inline">${escapeHtml(job.progress)}</div>` : '';
  const prevSub = job.previously_submitted
    ? '<span class="prev-submitted-badge" title="Previously submitted">Submitted before</span>'
    : '';
  const lockLabel = submissionLockLabel(job);
  const lockBadge = lockLabel
    ? `<span class="prev-submitted-badge" title="${escapeHtml(lockLabel)}">${escapeHtml(lockLabel)}</span>`
    : '';
  const errMsg = (_isStopped(job.status) && job.error_message)
    ? `<div class="error-inline" title="${escapeHtml(job.error_message)}">${escapeHtml(job.error_message.length > 80 ? job.error_message.slice(0, 77) + '...' : job.error_message)}</div>`
    : '';

  return `<div class="queue-job-cell">
    <div class="queue-job-header">
      <a class="job-id-link" href="#job/${job.id}">#${job.id}</a>
      <button type="button" class="queue-job-modal-trigger" onclick="openJobDetailModalFromEvent(event, ${job.id})">Modal</button>
      <span class="status-badge ${sClass}">${sLabel}</span>
      ${prevSub}${lockBadge}
    </div>
    <a class="queue-job-title" href="#job/${job.id}">${company}</a>
    <div class="queue-job-role">${role}</div>
    <div class="queue-job-meta">
      <span class="queue-job-board">${board}</span>
      ${_jobUrlIcons(job)}
    </div>
    ${progress}${errMsg}
  </div>`;
}

function buildQueueEnteredCell(job) {
  const timestamp = formatUtcTimestamp(queueTimestamp(job));
  if (!timestamp) {
    return '<div class="queue-entered-cell queue-entered-cell-empty">-</div>';
  }
  return `<div class="queue-entered-cell">${escapeHtml(timestamp)}</div>`;
}

function buildQueueConfidenceCell(job) {
  const summary = _queueReviewSummary(job);
  const level = escapeHtml(summary.overall_confidence || 'na');
  const chips = Array.isArray(summary.reason_chips)
    ? summary.reason_chips.map(chip => {
      const tone = escapeHtml(chip.tone || 'muted');
      const label = escapeHtml(chip.label || '');
      return `<span class="queue-review-chip queue-review-chip-${tone}">${label}</span>`;
    }).join('')
    : '';

  return `<div class="queue-review-summary">
    <div class="queue-confidence-badge" data-confidence="${level}">${escapeHtml(_queueConfidenceLabel(summary.overall_confidence || 'na'))}</div>
    <div class="queue-confidence-label">${escapeHtml(summary.confidence_label || 'No review data')}</div>
    <div class="queue-review-chips">${chips}</div>
  </div>`;
}

function runQueueAction(event, jobId, actionId) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  const job = window.jobs[jobId] || window.queueRows.find(row => row.id === jobId);
  if (!job) return;
  const action = actionDescriptorForId(job, actionId, 'queue');
  if (!action) return;
  Promise.resolve(action.handler())
    .then(() => refreshQueueData())
    .catch(err => console.error('Queue action failed', err));
}

function buildQueueActionsCell(job) {
  const actions = getJobActionModels(job, 'queue');
  if (actions.length === 0) {
    return '<div class="queue-actions"><span class="queue-actions-empty">No actions</span></div>';
  }
  const buttons = actions.map(action =>
    `<button type="button" class="${escapeHtml(action.className)}" onclick="runQueueAction(event, ${job.id}, '${escapeHtml(action.id)}')">${escapeHtml(action.label)}</button>`
  ).join('');
  return `<div class="queue-actions">${buttons}</div>`;
}

function buildQueueRow(job) {
  return `<tr data-id="${job.id}" data-job-id="${job.id}">
    <td class="col-check"><input type="checkbox" data-id="${job.id}" onclick="toggleRowCheck(event, ${job.id})"></td>
    <td class="col-job">${buildQueueJobCell(job)}</td>
    <td class="col-entered">${buildQueueEnteredCell(job)}</td>
    <td class="col-confidence">${buildQueueConfidenceCell(job)}</td>
    <td class="col-actions">${buildQueueActionsCell(job)}</td>
  </tr>`;
}

// ── Table sorting ────────────────────────────────────────────────────────────
function handleQueueHeaderSort(field) {
  sortTable(field);
}

function setQueueSortField(value) {
  const option = QUEUE_SORT_OPTION_BY_VALUE[value];
  if (!option) return;
  sortField = option.value;
  sortDir = option.defaultDir || 'asc';
  _sortCycleIndex = Math.max(0, _sortColumns.indexOf(sortField));
  _syncQueueSortControls();
  queueOffset = 0;
  refreshQueueData({ showLoading: true });
}

function toggleQueueSortDir() {
  sortDir = sortDir === 'asc' ? 'desc' : 'asc';
  _syncQueueSortControls();
  queueOffset = 0;
  refreshQueueData({ showLoading: true });
}

function sortTable(field) {
  if (sortField === field) {
    toggleQueueSortDir();
    return;
  }
  setQueueSortField(field);
}

// ── Select / bulk actions ────────────────────────────────────────────────────
function toggleSelectAll() {
  const cb = document.getElementById('select-all');
  const rows = document.querySelectorAll('#job-tbody input[type=checkbox]');
  rows.forEach(r => {
    r.checked = cb.checked;
    const id = parseInt(r.dataset.id, 10);
    if (cb.checked) selectedIds.add(id);
    else selectedIds.delete(id);
  });
  updateBulkActionsBar();
}

function toggleRowCheck(event, id) {
  event.stopPropagation();
  const cb = event.target;
  if (cb.checked) selectedIds.add(id);
  else selectedIds.delete(id);
  updateBulkActionsBar();
  // Update select-all state
  const allCbs = document.querySelectorAll('#job-tbody input[type=checkbox]');
  const allChecked = allCbs.length > 0 && [...allCbs].every(c => c.checked);
  document.getElementById('select-all').checked = allChecked;
}

function updateBulkActionsBar() {
  const bar = document.getElementById('bulk-actions');
  const cnt = document.getElementById('selected-count');
  if (!bar) return;
  const hasLockedSelection = selectedJobsContainLockedSubmission();
  const lockDisabledTitle = 'Unlock selected locked jobs before rerun actions';
  const setLockState = (id) => {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.disabled = hasLockedSelection;
    if (hasLockedSelection) btn.title = lockDisabledTitle;
    else btn.removeAttribute('title');
  };
  setLockState('bulk-approve-btn');
  setLockState('bulk-restart-draft-btn');
  setLockState('bulk-restart-submit-btn');
  if (selectedIds.size > 0) {
    bar.style.display = 'flex';
    cnt.textContent = selectedIds.size + ' selected';
  } else {
    bar.style.display = 'none';
  }
}

async function _bulkFinish(ok, fail) {
  const msg = ok + ' jobs updated' + (fail ? ', ' + fail + ' failed' : '');
  showToast(msg, fail ? 'warning' : 'success');
  selectedIds.clear();
  updateBulkActionsBar();
  renderQueue();
}

async function bulkRestart(autoSubmit, actionContext = createActionContext('queue', 'bulk')) {
  const ids = [...selectedIds].filter(id => !isSubmissionLocked(window.jobs[id]));
  if (!ids.length) {
    showToast('Unlock selected locked jobs before restart actions', 'warning');
    return;
  }
  let ok = 0, fail = 0;
  for (const id of ids) {
    try { await apiCall('POST', `/api/jobs/${id}/restart-pipeline`, { auto_submit: !!autoSubmit }, actionContext); ok++; }
    catch (_) { fail++; }
  }
  _bulkFinish(ok, fail);
}

async function bulkSkip(actionContext = createActionContext('queue', 'bulk')) {
  const ids = [...selectedIds];
  let ok = 0, fail = 0;
  for (const id of ids) {
    try { await apiCall('POST', `/api/jobs/${id}/skip`, null, actionContext); ok++; }
    catch (_) { fail++; }
  }
  _bulkFinish(ok, fail);
}

async function bulkStop(actionContext = createActionContext('queue', 'bulk')) {
  const ids = [...selectedIds];
  let ok = 0, fail = 0;
  for (const id of ids) {
    try { await apiCall('POST', `/api/jobs/${id}/stop`, null, actionContext); ok++; }
    catch (_) { fail++; }
  }
  _bulkFinish(ok, fail);
}

async function bulkDelete(actionContext = createActionContext('queue', 'bulk')) {
  const ids = [...selectedIds];
  let ok = 0, fail = 0;
  for (const id of ids) {
    try { await apiCall('DELETE', `/api/jobs/${id}`, null, actionContext); ok++; }
    catch (_) { fail++; }
  }
  _bulkFinish(ok, fail);
}

async function bulkApprove(actionContext = createActionContext('queue', 'bulk')) {
  const ids = [...selectedIds].filter(id => !isSubmissionLocked(window.jobs[id]));
  if (!ids.length) {
    showToast('Unlock selected locked jobs before approve actions', 'warning');
    return;
  }
  let ok = 0, fail = 0;
  for (const id of ids) {
    try { await apiCall('POST', `/api/jobs/${id}/approve`, null, actionContext); ok++; }
    catch (_) { fail++; }
  }
  _bulkFinish(ok, fail);
}

async function bulkArchive(actionContext = createActionContext('queue', 'bulk')) {
  const ids = [...selectedIds];
  let ok = 0, fail = 0;
  for (const id of ids) {
    try { await apiCall('POST', `/api/jobs/${id}/archive`, null, actionContext); ok++; }
    catch (_) { fail++; }
  }
  _bulkFinish(ok, fail);
}

// ── Job Detail View ──────────────────────────────────────────────────────────
async function renderJobDetail(jobId) {
  currentJobId = jobId;
  window.stagedChanges = {};
  updateChangesBar();

  // Show loading state
  document.getElementById('job-title').textContent = 'Loading...';
  document.getElementById('job-meta').innerHTML = '';
  document.getElementById('answer-refresh-proof').innerHTML = '';
  document.getElementById('job-status-badge').textContent = '';
  document.getElementById('prev-submitted-indicator').style.display = 'none';
  document.getElementById('submission-lock-indicator').style.display = 'none';
  document.getElementById('action-row').innerHTML = '';
  document.getElementById('error-banner').style.display = 'none';
  document.getElementById('progress-wrap').style.display = 'none';
  document.getElementById('board-url-bar').style.display = 'none';

  let job;
  try {
    job = await apiCall('GET', `/api/jobs/${jobId}`);
  } catch (e) {
    showToast('Failed to load job: ' + e.message, 'error');
    return;
  }

  job = mergeJobState(window.jobs[job.id], job);
  window.jobs[job.id] = job;
  renderJobHeaderFull(job);
  renderJobActionRow(job);
  renderJobDetailHelpers(job);

  // Load active tab content
  switchTab(currentTab, job, false);

  // Timeline
  renderTimeline(job.timeline || [], job);
  refreshScrollableDockRails();
}

function defaultAnswerRefreshMessage(status) {
  const messages = {
    pending: 'Waiting for fresh answer generation proof.',
    fresh: 'Fresh answer generation proof recorded.',
    not_applicable: 'No generated application answers were present for this draft.',
    failed: 'Answer regeneration failed before fresh proof was recorded.',
    unknown: 'This draft predates the answer refresh proof contract.',
  };
  return messages[status] || 'Answer refresh state is not available.';
}

function formatAnswerRefreshTimestamp(value) {
  return formatUtcTimestamp(value) || null;
}

function formatAnswerRefreshRequest(kind) {
  const labels = {
    reanswer: 'Answers only',
    draft_overrides: 'Draft overrides',
    full_regenerate: 'Full regenerate',
    restart_pipeline: 'Restart pipeline',
  };
  return labels[kind] || (kind ? kind.replaceAll('_', ' ') : null);
}

function presentAnswerRefresh(answerRefresh) {
  if (!answerRefresh) return null;
  const status = answerRefresh.status || 'unknown';
  const presentation = {
    status,
    badge: 'LEGACY',
    title: 'Legacy draft',
    message: answerRefresh.message || defaultAnswerRefreshMessage(status),
    meta: [],
  };

  if (status === 'pending') {
    presentation.badge = 'PENDING';
    presentation.title = 'Answer refresh in progress';
  } else if (status === 'fresh') {
    presentation.badge = 'FRESH';
    presentation.title = 'Fresh answers verified';
  } else if (status === 'not_applicable') {
    presentation.badge = 'N/A';
    presentation.title = 'No generated answers';
  } else if (status === 'failed') {
    presentation.badge = 'FAILED';
    presentation.title = 'Answer refresh failed';
  }

  const requestLabel = formatAnswerRefreshRequest(answerRefresh.request_kind);
  if (requestLabel) presentation.meta.push(`Request: ${requestLabel}`);

  const requestedAt = formatAnswerRefreshTimestamp(answerRefresh.requested_at_utc);
  if (requestedAt) presentation.meta.push(`Requested: ${requestedAt}`);

  const resolvedAt = formatAnswerRefreshTimestamp(answerRefresh.resolved_at_utc);
  if (resolvedAt && status !== 'pending') presentation.meta.push(`Resolved: ${resolvedAt}`);

  if (answerRefresh.answer_provider) presentation.meta.push(`Provider: ${answerRefresh.answer_provider}`);

  const generatedAt = formatAnswerRefreshTimestamp(answerRefresh.answer_generated_at_utc);
  if (generatedAt) presentation.meta.push(`Generated: ${generatedAt}`);

  if (answerRefresh.generated_answer_count !== null && answerRefresh.generated_answer_count !== undefined) {
    presentation.meta.push(`Generated answers: ${answerRefresh.generated_answer_count}`);
  }

  return presentation;
}

function buildHelperProofCard(sectionLabel, proof) {
  if (!proof) return null;

  const card = document.createElement('div');
  card.className = 'answer-refresh-card';
  card.dataset.status = proof.status;
  card.setAttribute('aria-label', 'Answer refresh proof');

  const heading = document.createElement('div');
  heading.className = 'answer-refresh-heading';

  const headingCopy = document.createElement('div');
  headingCopy.className = 'answer-refresh-heading-copy';

  const label = document.createElement('div');
  label.className = 'answer-refresh-label';
  label.textContent = sectionLabel;
  headingCopy.appendChild(label);

  const title = document.createElement('div');
  title.className = 'answer-refresh-title';
  title.textContent = proof.title;
  headingCopy.appendChild(title);

  heading.appendChild(headingCopy);

  const badge = document.createElement('span');
  badge.className = 'answer-refresh-badge';
  badge.textContent = proof.badge;
  heading.appendChild(badge);

  card.appendChild(heading);

  const message = document.createElement('p');
  message.className = 'answer-refresh-message';
  message.textContent = proof.message;
  card.appendChild(message);

  if (proof.meta.length) {
    const meta = document.createElement('div');
    meta.className = 'answer-refresh-meta';
    proof.meta.forEach(item => {
      const chip = document.createElement('span');
      chip.className = 'answer-refresh-chip';
      chip.textContent = item;
      meta.appendChild(chip);
    });
    card.appendChild(meta);
  }

  return card;
}

function buildAnswerRefreshCard(answerRefresh) {
  return buildHelperProofCard('Answer refresh', presentAnswerRefresh(answerRefresh));
}

function presentDraftReviewState(reviewState) {
  if (!reviewState) return null;
  const state = reviewState.state || 'unknown';
  const presentation = {
    status: state,
    badge: 'DRAFT',
    title: 'Draft proof status',
    message: reviewState.reason || 'Draft proof state is not available.',
    meta: [],
  };

  if (state === 'ready') {
    presentation.badge = 'READY';
    presentation.title = 'Draft proof current';
  } else if (state === 'blocked') {
    presentation.badge = 'BLOCKED';
    presentation.title = 'Draft proof blocked';
  } else if (state === 'stale') {
    presentation.badge = 'STALE';
    presentation.title = 'Historical proof is stale';
  } else if (state === 'legacy') {
    presentation.badge = 'LEGACY';
    presentation.title = 'Legacy draft proof';
  } else if (state === 'unavailable') {
    presentation.badge = 'CLOSED';
    presentation.title = 'Posting unavailable';
  }

  if (reviewState.submit_dirname) presentation.meta.push(`Active submit: ${reviewState.submit_dirname}`);
  const historical = reviewState.historical_submit_dirs || [];
  if (historical.length) presentation.meta.push(`Historical proof: ${historical.join(', ')}`);

  return presentation;
}

function buildDraftReviewCard(reviewState) {
  return buildHelperProofCard('Draft proof', presentDraftReviewState(reviewState));
}

function renderJobHeaderFull(job) {
  const title = document.getElementById('job-title');
  const meta  = document.getElementById('job-meta');
  const badge = document.getElementById('job-status-badge');
  const prevBadge = document.getElementById('prev-submitted-indicator');
  const lockBadge = document.getElementById('submission-lock-indicator');

  // Use textContent to prevent XSS
  title.textContent = `${job.company || '(unknown)'} — ${job.role_title || '(no role)'}`;

  // Meta row: board, provider, status timestamp, url
  meta.innerHTML = '';
  if (job.board) {
    const s = document.createElement('span');
    s.textContent = 'Board: ' + job.board;
    meta.appendChild(s);
  }
  if (job.provider) {
    const s = document.createElement('span');
    s.textContent = 'Provider: ' + job.provider;
    meta.appendChild(s);
  }
  if (job.status_entered_at) {
    const s = document.createElement('span');
    s.textContent = 'Status since: ' + formatUtcTimestamp(job.status_entered_at);
    meta.appendChild(s);
  }
  // Show LinkedIn/source URL if available
  const sourceUrl = job.source_url || job.url || '';
  const boardUrl = job.board_url || job.canonical_url || '';
  const isLinkedIn = sourceUrl.includes('linkedin.com');
  if (isLinkedIn && sourceUrl) {
    const a = document.createElement('a');
    a.href = sourceUrl;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = 'LinkedIn';
    a.style.marginRight = '8px';
    meta.appendChild(a);
  }
  if (boardUrl) {
    const a = document.createElement('a');
    a.href = boardUrl;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = isLinkedIn ? 'Job Board' : 'Job URL';
    meta.appendChild(a);
  } else if (!isLinkedIn && sourceUrl) {
    const a = document.createElement('a');
    a.href = sourceUrl;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = 'Job URL';
    meta.appendChild(a);
  }

  // Status badge
  badge.className = 'status-badge ' + statusClass(job.status);
  badge.textContent = statusLabel(job.status) + (job.archived ? ' (archived)' : '');

  // Previously submitted indicator
  if (job.previously_submitted) {
    prevBadge.className = 'prev-submitted-badge';
    prevBadge.textContent = 'Submitted before';
    prevBadge.title = 'This job was previously submitted';
    prevBadge.style.display = '';
  } else {
    prevBadge.style.display = 'none';
  }

  const lockLabel = submissionLockLabel(job);
  if (lockLabel) {
    lockBadge.className = 'prev-submitted-badge';
    lockBadge.textContent = lockLabel;
    lockBadge.title = lockLabel;
    lockBadge.style.display = '';
  } else {
    lockBadge.style.display = 'none';
  }
}

function renderJobDetailHelpers(job) {
  const answerRefresh = document.getElementById('answer-refresh-proof');
  const errBanner = document.getElementById('error-banner');
  const errText = document.getElementById('error-message-text');
  const progressWrap = document.getElementById('progress-wrap');
  const progressLabel = document.getElementById('progress-label');
  const boardUrlBar = document.getElementById('board-url-bar');

  answerRefresh.innerHTML = '';
  const answerRefreshCard = buildAnswerRefreshCard(job.answer_refresh);
  if (answerRefreshCard) {
    answerRefresh.appendChild(answerRefreshCard);
  }
  const draftReviewCard = buildDraftReviewCard(job.draft_review_state);
  if (draftReviewCard) {
    answerRefresh.appendChild(draftReviewCard);
  }

  // Error banner
  if (job.error_message) {
    errText.textContent = job.error_message;
    errBanner.style.display = 'flex';
  } else {
    errBanner.style.display = 'none';
  }

  // Progress bar
  if (PROCESSING_STATUSES.has(job.status)) {
    progressWrap.style.display = 'block';
    progressLabel.textContent = job.progress || statusLabel(job.status) + '...';
  } else {
    progressWrap.style.display = 'none';
  }

  // Board URL bar
  if (job.status === 'needs_board_url') {
    boardUrlBar.style.display = 'flex';
    document.getElementById('board-url-input').value = job.board_url || '';
  } else {
    boardUrlBar.style.display = 'none';
  }
}

function updateJobDetailHeader(job) {
  if (!document.getElementById('view-job') ||
      document.getElementById('view-job').style.display === 'none') return;
  if (currentJobId !== job.id) return;
  const previousJob = window.jobs[job.id] || {};
  job = mergeJobState(previousJob, job);
  const shouldReloadCurrentTab = currentTab !== 'logs' && shouldReloadSelectedJobTab(previousJob, job);
  window.jobs[job.id] = job;
  renderJobHeaderFull(job);
  renderJobActionRow(job);
  renderJobDetailHelpers(job);
  if (shouldReloadCurrentTab) {
    switchTab(currentTab || 'answers', job, false);
  }
  refreshScrollableDockRails();
}

function jobDetailReloadFingerprint(job) {
  if (!job) return '';
  const proof = job.proof_artifacts || {};
  const draftReview = job.draft_review_state || {};
  const pendingQuestions = ((job.pending_user_input && job.pending_user_input.questions) || []).map(question => ({
    label: question.label || '',
    artifact_key: question.artifact_key || '',
    reason: question.reason || '',
    note: question.note || '',
    planned_value: question.planned_value || '',
  }));
  return JSON.stringify({
    failure_type: job.failure_type || '',
    error_message: job.error_message || '',
    submit_dirname: proof.submit_dirname || '',
    proof_revision: proof.proof_revision || '',
    pre_submit_screenshot: proof.pre_submit_screenshot || '',
    review_screenshot: proof.review_screenshot || '',
    post_submit_screenshot: proof.post_submit_screenshot || '',
    submit_debug_screenshot: proof.submit_debug_screenshot || '',
    report_json: proof.report_json || '',
    report_markdown: proof.report_markdown || '',
    pending_questions: pendingQuestions,
    draft_review_state: draftReview.state || '',
    draft_review_reason: draftReview.reason || '',
  });
}

function shouldReloadSelectedJobTab(previousJob, nextJob) {
  const previousStatus = (previousJob && previousJob.status) || '';
  const nextStatus = (nextJob && nextJob.status) || '';
  const statusChanged = previousStatus !== nextStatus;
  const processingTransition = statusChanged &&
    PROCESSING_STATUSES.has(previousStatus) &&
    PROCESSING_STATUSES.has(nextStatus);
  if (statusChanged && !processingTransition) return true;
  return jobDetailReloadFingerprint(previousJob) !== jobDetailReloadFingerprint(nextJob);
}

function actionDescriptorForId(job, actionId, surface = 'detail') {
  const queue = surface === 'queue';
  const descriptor = {
    unlock_resubmit: {
      detailLabel: 'Unlock to Resubmit',
      queueLabel: 'Unlock',
      detailClassName: 'btn-primary',
      queueClassName: 'queue-action-btn queue-action-btn-primary',
      handler: () => unlockForResubmit(job.id, createActionContext(surface, 'button')),
    },
    lock_resubmission: {
      detailLabel: 'Lock Resubmission',
      queueLabel: 'Lock',
      detailClassName: 'btn-outline',
      queueClassName: 'queue-action-btn queue-action-btn-neutral',
      handler: () => lockResubmission(job.id, createActionContext(surface, 'button')),
    },
    approve_submit: {
      detailLabel: 'Approve + Submit',
      queueLabel: 'Submit',
      detailClassName: 'btn-success',
      queueClassName: 'queue-action-btn queue-action-btn-success',
      handler: () => approveJob(job.id, createActionContext(surface, 'button')),
    },
    reset_to_new: {
      detailLabel: 'Reset to New',
      queueLabel: 'Reset',
      detailClassName: 'btn-warning',
      queueClassName: 'queue-action-btn queue-action-btn-warn',
      handler: () => resetJobToNew(job.id, createActionContext(surface, 'button')),
    },
    focus_browser: {
      detailLabel: 'Focus Browser',
      queueLabel: 'Focus',
      detailClassName: 'btn-primary',
      queueClassName: 'queue-action-btn queue-action-btn-primary',
      handler: () => focusCaptchaBrowser(job.id),
    },
    resubmit: {
      detailLabel: 'Resubmit',
      queueLabel: 'Resubmit',
      detailClassName: 'btn-success',
      queueClassName: 'queue-action-btn queue-action-btn-success',
      handler: () => restartPipeline(job.id, true, createActionContext(surface, 'button')),
    },
    restart_draft: {
      detailLabel: 'Restart → Draft',
      queueLabel: 'Redraw',
      detailClassName: _isStopped(job.status) ? 'btn-primary' : 'btn-outline',
      queueClassName: 'queue-action-btn queue-action-btn-neutral',
      handler: () => restartPipeline(job.id, false, createActionContext(surface, 'button')),
    },
    restart_submit: {
      detailLabel: 'Restart → Submit',
      queueLabel: 'Redraw + Submit',
      detailClassName: 'btn-outline',
      queueClassName: 'queue-action-btn queue-action-btn-primary',
      handler: () => restartPipeline(job.id, true, createActionContext(surface, 'button')),
    },
    stop: {
      detailLabel: 'Stop',
      queueLabel: 'Stop',
      detailClassName: 'btn-danger',
      queueClassName: 'queue-action-btn queue-action-btn-danger',
      handler: () => stopJob(job.id, createActionContext(surface, 'button')),
    },
    archive: {
      detailLabel: 'Archive',
      queueLabel: 'Archive',
      detailClassName: 'btn-outline',
      queueClassName: 'queue-action-btn queue-action-btn-neutral',
      handler: () => archiveJob(job.id, createActionContext(surface, 'button')),
    },
    unarchive: {
      detailLabel: 'Unarchive',
      queueLabel: 'Unarchive',
      detailClassName: 'btn-outline',
      queueClassName: 'queue-action-btn queue-action-btn-neutral',
      handler: () => unarchiveJob(job.id, createActionContext(surface, 'button')),
    },
    delete: {
      detailLabel: 'Delete',
      queueLabel: 'Delete',
      detailClassName: 'btn-outline btn-delete',
      queueClassName: 'queue-action-btn queue-action-btn-danger',
      handler: () => deleteJob(job.id, createActionContext(surface, 'button')),
    },
  }[actionId];

  if (!descriptor) return null;
  return {
    id: actionId,
    label: queue ? descriptor.queueLabel : descriptor.detailLabel,
    className: queue ? descriptor.queueClassName : descriptor.detailClassName,
    handler: descriptor.handler,
  };
}

function getJobActionModels(job, surface = 'detail') {
  const summary = _queueReviewSummary(job);
  const actionIds = Array.isArray(summary.visible_actions) ? summary.visible_actions : [];
  return actionIds
    .map(actionId => actionDescriptorForId(job, actionId, surface))
    .filter(Boolean);
}

function renderJobActionRow(job) {
  const row = document.getElementById('action-row');
  row.innerHTML = '';
  getJobActionModels(job, 'detail').forEach(action => {
    row.appendChild(makeBtn(action.label, action.className, action.handler));
  });
  row.scrollLeft = 0;
  refreshScrollableDockRails();
}

function makeBtn(label, cls, handler) {
  const btn = document.createElement('button');
  btn.className = 'btn ' + cls;
  btn.textContent = label;
  btn.addEventListener('click', handler);
  return btn;
}

// ── Tab switching ────────────────────────────────────────────────────────────
function keepActiveTabVisible(tabName) {
  const activeBtn = document.querySelector(`.tab-btn[data-tab="${tabName}"]`);
  if (activeBtn) {
    activeBtn.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
  }
}

function isVisibleHelperElement(el) {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  if (style.display === 'none' || style.visibility === 'hidden') return false;
  const rect = el.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}

function getJobDetailContentAnchor(panel) {
  const helperIds = ['board-url-bar', 'error-banner', 'progress-wrap', 'answer-refresh-proof'];
  for (const id of helperIds) {
    const el = document.getElementById(id);
    if (isVisibleHelperElement(el)) {
      return el;
    }
  }
  return panel;
}

function alignJobDetailContent(panel, options = {}) {
  const { preferPanelStart = false } = options;
  const dock = document.getElementById('job-detail-dock');
  const anchor = preferPanelStart ? panel : getJobDetailContentAnchor(panel);
  if (!dock || !anchor) return;

  const navHeight = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--nav-height')) || 52;
  const dockRect = dock.getBoundingClientRect();
  const anchorRect = anchor.getBoundingClientRect();
  const dockIsPinned = dockRect.top <= navHeight + 12;
  if (!dockIsPinned && anchorRect.top >= dockRect.bottom) return;

  const targetTop = dockRect.bottom + 12;
  const delta = anchorRect.top - targetTop;
  if (Math.abs(delta) > 4) {
    window.scrollBy({ top: delta, behavior: 'smooth' });
  }
}

function switchTab(tabName, job, userInitiated = false) {
  currentTab = tabName;
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tabName);
  });
  document.querySelectorAll('.tab-content').forEach(c => (c.style.display = 'none'));
  const el = document.getElementById(JOB_TAB_MAP[tabName]);
  if (el) {
    el.style.display = 'block';
    el.scrollTop = 0;
  }

  if (!currentJobId) return;
  const loadJob = job || window.jobs[currentJobId];

  if (tabName === 'answers')      loadAnswersTab(currentJobId, loadJob);
  if (tabName === 'resume')       loadResumeTab(currentJobId);
  if (tabName === 'cover-letter') loadCoverLetterTab(currentJobId);
  if (tabName === 'screenshot')   loadScreenshotTab(currentJobId, loadJob);
  if (tabName === 'confirmation') loadConfirmationTab(currentJobId, loadJob);
  if (tabName === 'logs')         loadLogsTab(currentJobId, loadJob);
  if (tabName === 'interview-prep') loadInterviewPrepTab(currentJobId);

  if (userInitiated && el) {
    keepActiveTabVisible(tabName);
    alignJobDetailContent(el, { preferPanelStart: true });
  }
  requestAnimationFrame(refreshScrollableDockRails);
}

async function loadAnswersTab(jobId, job) {
  const el = document.getElementById('tab-answers');
  el.innerHTML = '<div class="loading-msg">Loading answers...</div>';

  // Skip fetch for jobs that can't have autofill reports yet
  const job_ = job || window.jobs[jobId] || {};
  const NO_REPORT_STATUSES = new Set(['queued', 'queued_submit', 'generating', 'resolving']);
  if (!job_.status || NO_REPORT_STATUSES.has(job_.status)) {
    el.innerHTML = '<div class="loading-msg">This job hasn\'t been through autofill yet — no application answers generated.</div>';
    return;
  }

  const proof = job_.proof_artifacts || {};
  const candidates = [];
  // Trust active proof metadata here so missing optional files don't generate noisy 404 probes.
  if (proof.report_json) candidates.push(proof.report_json);

  let report = null;
  for (const filename of [...new Set(candidates)]) {
    try {
      report = await apiCall('GET', `/api/jobs/${jobId}/content/${filename}`);
      if (report) break;
    } catch (_) { /* try next */ }
  }

  let answerPayload = null;
  const answerCandidates = [];
  if (proof.application_answers_json) answerCandidates.push(proof.application_answers_json);
  for (const filename of [...new Set(answerCandidates)]) {
    try {
      answerPayload = await apiCall('GET', `/api/jobs/${jobId}/content/${filename}`);
      if (answerPayload) break;
    } catch (_) { /* try next */ }
  }

  if (!report && !answerPayload && !(job_.pending_user_input && (job_.pending_user_input.questions || []).length)) {
    el.innerHTML = '';
    el.appendChild(_tabActionBar(jobId, 'answers', 'Regenerate Answers'));
    const message = document.createElement('div');
    message.className = 'loading-msg';
    message.textContent = 'No answers available.';
    el.appendChild(message);
    return;
  }

  el.innerHTML = '';
  el.appendChild(_tabActionBar(jobId, 'answers', 'Regenerate Answers'));
  const container = document.createElement('div');
  el.appendChild(container);
  renderAnswersTab(container, report || {}, answerPayload || {}, job_);
}

function renderAnswersTab(container, report, answerPayload, job) {
  container.innerHTML = '';
  const pendingQuestions = (job && job.pending_user_input && job.pending_user_input.questions) || [];
  const answerMap = (answerPayload && answerPayload.answers) || {};
  const linkedResources = (answerPayload && answerPayload.linked_resources) || {};
  const linkedResourceByField = {};
  const linkedFailureByField = {};
  (linkedResources.resources || []).forEach((resource) => {
    const fieldKey = resource.field_name || '';
    if (!fieldKey) return;
    if (!linkedResourceByField[fieldKey]) linkedResourceByField[fieldKey] = [];
    linkedResourceByField[fieldKey].push(resource);
  });
  (linkedResources.failures || []).forEach((failure) => {
    const fieldKey = failure.field_name || '';
    if (!fieldKey) return;
    if (!linkedFailureByField[fieldKey]) linkedFailureByField[fieldKey] = [];
    linkedFailureByField[fieldKey].push(failure);
  });

  // report may be an array of {label, answer, field_type, ...} or an object
  let fields = Array.isArray(report) ? report : (report.fields || report.answers || []);
  // For draft jobs, fields may be empty but planned answers exist
  if (!fields.length && report.planned_but_unconfirmed_fields && report.planned_but_unconfirmed_fields.length) {
    fields = report.planned_but_unconfirmed_fields;
  }
  if (!fields.length && answerPayload && (answerPayload.questions || []).length) {
    fields = (answerPayload.questions || []).map((question) => {
      const fieldName = question.field_name || question.name || '';
      const answerValue = Object.prototype.hasOwnProperty.call(answerMap, fieldName) ? answerMap[fieldName] : '';
      return {
        field_name: fieldName,
        label: question.label || fieldName,
        kind: question.type || 'text',
        type: question.type || 'text',
        value: answerValue,
        answer: answerValue,
      };
    });
  }

  if (!pendingQuestions.length && !fields.length) {
    container.innerHTML = '<div class="loading-msg">No answers recorded.</div>';
    return;
  }

  if (pendingQuestions.length) {
    const needsReviewTitle = document.createElement('h3');
    needsReviewTitle.textContent = 'Needs Review';
    container.appendChild(needsReviewTitle);

    pendingQuestions.forEach((question, idx) => {
      const label = question.label || question.field_name || `Field ${idx + 1}`;
      const plannedValue = question.planned_value ? String(question.planned_value) : '(planned value not captured)';
      const kind = question.kind || '';
      const status = question.status || 'planned';

      const div = document.createElement('div');
      div.className = 'answer-field blocker-field';

      const labelEl = document.createElement('div');
      labelEl.className = 'answer-label';
      const labelText = document.createElement('span');
      labelText.textContent = label;
      labelEl.appendChild(labelText);
      if (kind) {
        const typeEl = document.createElement('span');
        typeEl.className = 'answer-type';
        typeEl.textContent = `${kind} • ${status}`;
        labelEl.appendChild(typeEl);
      }
      div.appendChild(labelEl);

      const valueEl = document.createElement('div');
      valueEl.className = 'answer-value';
      valueEl.textContent = plannedValue;
      div.appendChild(valueEl);

      const meta = [];
      if (question.page_index != null) meta.push(`Page ${question.page_index}`);
      if (question.artifact_key) meta.push(`Artifact: ${question.artifact_key}`);
      if (question.reason) meta.push(question.reason);
      if (question.note) meta.push(question.note);
      if (meta.length) {
        const metaEl = document.createElement('div');
        metaEl.className = 'original-value';
        metaEl.textContent = meta.join(' • ');
        div.appendChild(metaEl);
      }

      container.appendChild(div);
    });
  }

  if (fields.length) {
    const answersTitle = document.createElement('h3');
    answersTitle.textContent = 'Application Answers';
    container.appendChild(answersTitle);
  }

  fields.forEach((field, idx) => {
    const label     = field.label || field.question || field.name || field.field_name || `Field ${idx + 1}`;
    const fieldKey  = field.field_name || field.name || field.label || `field_${idx}`;
    const answerValue = Object.prototype.hasOwnProperty.call(answerMap, fieldKey)
      ? answerMap[fieldKey]
      : (field.answer != null ? field.answer : field.value);
    const answer    = answerValue != null ? String(answerValue) : '';
    const fieldType = field.field_type || field.type || field.kind || '';

    const div = document.createElement('div');
    div.className = 'answer-field';

    const labelEl = document.createElement('div');
    labelEl.className = 'answer-label';
    const labelText = document.createElement('span');
    labelText.textContent = label;
    labelEl.appendChild(labelText);
    if (fieldType) {
      const typeEl = document.createElement('span');
      typeEl.className = 'answer-type';
      typeEl.textContent = fieldType;
      labelEl.appendChild(typeEl);
    }
    div.appendChild(labelEl);

    const valueEl = document.createElement('div');
    valueEl.className = 'answer-value editable';
    valueEl.dataset.fieldKey = fieldKey;
    valueEl.dataset.original = answer;
    valueEl.textContent = answer || '(empty)';

    // Check if staged
    if (window.stagedChanges[fieldKey]) {
      valueEl.classList.add('edited');
      valueEl.textContent = window.stagedChanges[fieldKey].edited;
      const orig = document.createElement('div');
      orig.className = 'original-value';
      orig.textContent = window.stagedChanges[fieldKey].original;
      div.appendChild(labelEl);
      div.appendChild(valueEl);
      div.appendChild(orig);
    } else {
      div.appendChild(labelEl);
      div.appendChild(valueEl);
    }

    valueEl.addEventListener('click', () => makeEditable(valueEl, fieldKey, answer));
    const linkedResource = (linkedResourceByField[fieldKey] || [])[0];
    const linkedFailure = (linkedFailureByField[fieldKey] || [])[0];
    if (linkedResource || linkedFailure) {
      const metaEl = document.createElement('div');
      metaEl.className = 'original-value';
      if (linkedResource) {
        metaEl.textContent = `Linked resource: ${linkedResource.adapter} via ${linkedResource.url}`;
      } else {
        metaEl.textContent = `Linked resource failed: ${linkedFailure.url} (${linkedFailure.failure_reason})`;
      }
      div.appendChild(metaEl);
    }
    container.appendChild(div);
  });
}

async function loadResumeTab(jobId) {
  const el = document.getElementById('tab-resume');
  el.innerHTML = '<div class="loading-msg">Loading resume...</div>';
  try {
    // Try to embed the PDF first
    const docs = await apiCall('GET', `/api/jobs/${jobId}/documents`);
    const resumePdf = (docs.files || []).find(f => f.name.includes('Resume') && f.type === 'pdf');
    el.innerHTML = '';
    el.appendChild(_tabActionBar(jobId, 'resume', 'Regenerate Resume'));
    if (resumePdf) {
      const embed = document.createElement('iframe');
      embed.src = `/api/jobs/${jobId}/content/${encodeURIComponent(resumePdf.name)}`;
      embed.style.cssText = 'width:100%;height:800px;border:1px solid #ddd;border-radius:6px;';
      el.appendChild(embed);
      return;
    }
    // Fallback to JSON rendering
    const data = await apiCall('GET', `/api/jobs/${jobId}/content/resume_content.json`);
    const container = document.createElement('div');
    el.appendChild(container);
    renderResumeTab(container, data);
  } catch (_) {
    el.innerHTML = '<div class="loading-msg">No resume available.</div>';
  }
}

function renderResumeTab(container, data) {
  container.innerHTML = '';
  if (!data) {
    container.innerHTML = '<div class="loading-msg">No resume data.</div>';
    return;
  }

  // data may have sections like { summary, experience, education, skills, ... }
  const sections = data.sections || data;
  if (typeof sections === 'string') {
    const pre = document.createElement('pre');
    pre.style.whiteSpace = 'pre-wrap';
    pre.style.fontSize = '13px';
    pre.textContent = sections;
    container.appendChild(pre);
    return;
  }

  const renderSection = (title, items) => {
    const sec = document.createElement('div');
    sec.className = 'resume-section';
    const h3 = document.createElement('h3');
    h3.textContent = title;
    sec.appendChild(h3);
    if (typeof items === 'string') {
      const p = document.createElement('p');
      p.style.fontSize = '13px';
      p.textContent = items;
      sec.appendChild(p);
    } else if (Array.isArray(items)) {
      items.forEach(item => {
        if (typeof item === 'string') {
          const row = document.createElement('div');
          row.className = 'bullet-item';
          const dot = document.createElement('span');
          dot.className = 'bullet-dot';
          dot.textContent = '•';
          const text = document.createElement('span');
          text.textContent = item;
          row.appendChild(dot);
          row.appendChild(text);
          sec.appendChild(row);
        } else if (item && typeof item === 'object') {
          // Object entry (e.g. experience item)
          const role = document.createElement('div');
          role.style.marginBottom = '8px';
          for (const [k, v] of Object.entries(item)) {
            if (typeof v === 'string') {
              const p = document.createElement('div');
              p.style.fontSize = '13px';
              const b = document.createElement('strong');
              b.textContent = k + ': ';
              p.appendChild(b);
              p.appendChild(document.createTextNode(v));
              role.appendChild(p);
            } else if (Array.isArray(v)) {
              v.forEach(bullet => {
                const row = document.createElement('div');
                row.className = 'bullet-item';
                const dot = document.createElement('span');
                dot.className = 'bullet-dot';
                dot.textContent = '•';
                const text = document.createElement('span');
                text.textContent = bullet;
                row.appendChild(dot);
                row.appendChild(text);
                role.appendChild(row);
              });
            }
          }
          sec.appendChild(role);
        }
      });
    }
    container.appendChild(sec);
  };

  if (Array.isArray(sections)) {
    sections.forEach(sec => {
      if (sec.title || sec.section) {
        renderSection(sec.title || sec.section, sec.items || sec.bullets || sec.content || []);
      }
    });
  } else if (typeof sections === 'object') {
    for (const [key, val] of Object.entries(sections)) {
      if (key.startsWith('_')) continue;
      if (val === null || val === undefined || val === '') continue; // skip empty/null
      const title = key.charAt(0).toUpperCase() + key.slice(1).replace(/_/g, ' ');
      // Handle positions: { moodys: [{bold, text}, ...], kyte: [...], ... }
      if (key === 'positions' && val && typeof val === 'object' && !Array.isArray(val)) {
        const sec = document.createElement('div');
        sec.className = 'resume-section';
        const h3 = document.createElement('h3');
        h3.textContent = 'Positions';
        sec.appendChild(h3);
        for (const [posName, bullets] of Object.entries(val)) {
          const posDiv = document.createElement('div');
          posDiv.style.marginBottom = '16px';
          const posTitle = document.createElement('div');
          posTitle.style.fontWeight = '600';
          posTitle.style.fontSize = '14px';
          posTitle.style.marginBottom = '6px';
          posTitle.textContent = posName.charAt(0).toUpperCase() + posName.slice(1);
          posDiv.appendChild(posTitle);
          if (Array.isArray(bullets)) {
            bullets.forEach(b => {
              const row = document.createElement('div');
              row.className = 'bullet-item';
              const dot = document.createElement('span');
              dot.className = 'bullet-dot';
              dot.textContent = '•';
              row.appendChild(dot);
              if (b && typeof b === 'object' && (b.bold || b.text)) {
                const span = document.createElement('span');
                if (b.bold) { const strong = document.createElement('strong'); strong.textContent = b.bold; span.appendChild(strong); }
                if (b.text) span.appendChild(document.createTextNode(b.text));
                row.appendChild(span);
              } else {
                const text = document.createElement('span');
                text.textContent = typeof b === 'string' ? b : JSON.stringify(b);
                row.appendChild(text);
              }
              posDiv.appendChild(row);
            });
          }
          sec.appendChild(posDiv);
        }
        container.appendChild(sec);
      } else {
        renderSection(title, val);
      }
    }
  } else {
    const pre = document.createElement('pre');
    pre.style.whiteSpace = 'pre-wrap';
    pre.textContent = JSON.stringify(data, null, 2);
    container.appendChild(pre);
  }
}

async function loadCoverLetterTab(jobId) {
  const el = document.getElementById('tab-cover-letter');
  el.innerHTML = '<div class="loading-msg">Loading cover letter...</div>';
  try {
    // Try to embed the PDF first
    const docs = await apiCall('GET', `/api/jobs/${jobId}/documents`);
    const clPdf = (docs.files || []).find(f => f.name.includes('Cover Letter') && f.type === 'pdf');
    el.innerHTML = '';
    el.appendChild(_tabActionBar(jobId, 'cover_letter', 'Regenerate Cover Letter'));
    if (clPdf) {
      const embed = document.createElement('iframe');
      embed.src = `/api/jobs/${jobId}/content/${encodeURIComponent(clPdf.name)}`;
      embed.style.cssText = 'width:100%;height:800px;border:1px solid #ddd;border-radius:6px;';
      el.appendChild(embed);
      return;
    }
    // Fallback to text rendering
    const data = await apiCall('GET', `/api/jobs/${jobId}/content/cover_letter_text.txt`);
    const container = document.createElement('div');
    el.appendChild(container);
    renderCoverLetterTab(container, data.text || '');
  } catch (_) {
    el.innerHTML = '<div class="loading-msg">No cover letter available.</div>';
  }
}

function renderCoverLetterTab(container, text) {
  container.innerHTML = '';
  if (!text) {
    container.innerHTML = '<div class="loading-msg">No cover letter available.</div>';
    return;
  }
  let body = text.trim();
  // Add greeting if missing
  const hasGreeting = /^dear\s/i.test(body);
  // Add signoff if missing
  const hasSignoff = /\n(best regards|sincerely|regards|warm regards|thank you),?\s*\n/i.test(body);

  const div = document.createElement('div');
  div.className = 'cover-letter-text';

  if (!hasGreeting) {
    const greeting = document.createElement('p');
    greeting.style.marginBottom = '16px';
    greeting.textContent = 'Dear Hiring Team,';
    div.appendChild(greeting);
  }

  const bodyEl = document.createElement('div');
  bodyEl.style.whiteSpace = 'pre-wrap';
  bodyEl.textContent = body;
  div.appendChild(bodyEl);

  if (!hasSignoff) {
    const signoff = document.createElement('p');
    signoff.style.marginTop = '16px';
    signoff.innerHTML = 'Best regards,<br>Candidate Name';
    div.appendChild(signoff);
  }

  container.appendChild(div);
}

// ── Screenshot Tab ───────────────────────────────────────────────────────────
async function loadScreenshotTab(jobId, job) {
  const el = document.getElementById('tab-screenshot');
  el.innerHTML = '<div class="loading-msg">Loading screenshot...</div>';

  const job_ = job || window.jobs[jobId] || {};
  const NO_REPORT_STATUSES = new Set(['queued', 'queued_submit', 'generating', 'resolving']);
  if (!job_.status || NO_REPORT_STATUSES.has(job_.status)) {
    el.innerHTML = '<div class="loading-msg">No pre-submit screenshot — job hasn\'t been through autofill yet.</div>';
    return;
  }

  const proof = job_.proof_artifacts || {};
  const imageCandidates = [
    ['Pre-submit screenshot', proof.pre_submit_screenshot],
    ['Review screenshot', proof.review_screenshot],
    ['Submit debug screenshot', proof.submit_debug_screenshot],
  ].filter(([, filename]) => filename);

  el.innerHTML = '';
  // Action bar at top so it's visible without scrolling past the screenshot
  el.appendChild(_tabActionBar(jobId, 'answers', 'Regenerate Answers'));
  const seenProofFilenames = new Set();
  let renderedCount = 0;
  for (const [label, filename] of imageCandidates) {
    if (seenProofFilenames.has(filename)) {
      continue;
    }
    seenProofFilenames.add(filename);
    const block = document.createElement('div');
    if (renderedCount > 0) block.style.marginTop = '16px';

    const title = document.createElement('div');
    title.textContent = label;
    title.style.fontSize = '13px';
    title.style.fontWeight = '600';
    title.style.marginBottom = '8px';

    const img = document.createElement('img');
    img.src = `/api/jobs/${jobId}/content/${filename}?t=${Date.now()}`;
    img.alt = label;
    img.style.maxWidth = '100%';
    img.style.border = '1px solid #ddd';
    img.style.borderRadius = '6px';

    block.appendChild(title);
    block.appendChild(img);
    el.appendChild(block);
    renderedCount += 1;
  }
  if (!renderedCount) {
    const screenshotBlocker = ((job_.pending_user_input && job_.pending_user_input.questions) || [])
      .find(q => q.artifact_key === 'review_screenshot' || q.artifact_key === 'pre_submit_screenshot');
    const message = screenshotBlocker && (screenshotBlocker.reason || screenshotBlocker.note)
      ? `${screenshotBlocker.reason || screenshotBlocker.note}${screenshotBlocker.planned_value ? ` Expected: ${screenshotBlocker.planned_value}` : ''}`
      : 'No pre-submit screenshot available.';
    el.appendChild(Object.assign(document.createElement('div'), {className: 'loading-msg', textContent: message}));
  }
}

async function loadConfirmationTab(jobId, job) {
  const el = document.getElementById('tab-confirmation');
  el.innerHTML = '<div class="loading-msg">Loading confirmation...</div>';

  const job_ = job || window.jobs[jobId] || {};
  const SUBMITTED_STATUSES = new Set(['submitted']);
  if (!job_.status || !SUBMITTED_STATUSES.has(job_.status)) {
    el.innerHTML = '<div class="loading-msg">No confirmation yet — job hasn\'t been submitted.</div>';
    return;
  }

  const proof = job_.proof_artifacts || {};
  const board = (job_.board) ? job_.board.toLowerCase() : '';
  const ALL_BOARDS = ['greenhouse', 'ashby', 'lever', 'workday', 'dover', 'icims',
    'gem', 'phenom', 'eightfold', 'bamboohr', 'smartrecruiters', 'workable',
    'comeet', 'rippling', 'uber', 'motion', 'reducto'];
  const candidates = [];
  if (proof.post_submit_screenshot) candidates.push(proof.post_submit_screenshot);
  if (proof.submit_debug_screenshot) candidates.push(proof.submit_debug_screenshot);
  candidates.push('autofill_post_submit.png', 'submit_debug.png');
  if (board && board !== 'unknown') {
    candidates.push(board + '_autofill_post_submit.png', board + '_submit_debug.png');
  } else {
    candidates.push(...ALL_BOARDS.flatMap(b => [b + '_autofill_post_submit.png', b + '_submit_debug.png']));
  }

  el.innerHTML = '';
  let found = false;
  for (const filename of [...new Set(candidates)]) {
    const url = `/api/jobs/${jobId}/content/${filename}`;
    try {
      const resp = await fetch(url);
      if (resp.ok) {
        const img = document.createElement('img');
        img.src = url + '?t=' + Date.now();  // cache-bust
        img.alt = 'Post-submit confirmation';
        img.style.maxWidth = '100%';
        img.style.border = '1px solid #ddd';
        img.style.borderRadius = '6px';
        el.appendChild(img);
        found = true;
        break;
      }
    } catch (_) { /* try next */ }
  }
  if (!found) {
    el.innerHTML = '<div class="loading-msg">No post-submit screenshot available.</div>';
  }
}

function parseEventDetailJson(value) {
  if (!value) return null;
  if (typeof value === 'object') return value;
  try {
    return JSON.parse(value);
  } catch (_) {
    return null;
  }
}

function formatEventActionAudit(ev) {
  const payload = parseEventDetailJson(ev && ev.detail_json);
  const action = payload && payload.action;
  if (!action || typeof action !== 'object') return '';

  const parts = [];
  const surface = action.surface ? String(action.surface) : '';
  const trigger = action.trigger ? String(action.trigger) : '';
  if (surface || trigger) {
    parts.push(`Source: ${surface || 'unknown'}${trigger ? ` / ${trigger}` : ''}`);
  }
  if (action.request_id) {
    parts.push(`Request: ${action.request_id}`);
  }
  return parts.join(' · ');
}

// ── Timeline ─────────────────────────────────────────────────────────────────
function renderTimeline(events, job) {
  const content = document.getElementById('timeline-content');
  if (!content) return;
  if (!events.length) {
    const job_ = job || (currentJobId ? window.jobs[currentJobId] : null) || {};
    const isPreSubmit = !job_.status || job_.status === 'draft' || job_.status === 'queued';
    if (isPreSubmit) {
      content.innerHTML = '<div class="loading-msg">No submission events recorded — job is still in draft.</div>';
    } else {
      content.innerHTML = '<div class="loading-msg">No events recorded.</div>';
    }
    return;
  }
  content.innerHTML = '';
  events.forEach(ev => {
    const item = document.createElement('div');
    item.className = 'timeline-event';

    const dot = document.createElement('div');
    dot.className = 'timeline-dot';

    const body = document.createElement('div');
    body.className = 'timeline-content';

    const row = document.createElement('div');
    row.style.display = 'flex';
    row.style.justifyContent = 'space-between';
    row.style.alignItems = 'flex-start';
    row.style.gap = '8px';

    const typeEl = document.createElement('div');
    typeEl.className = 'timeline-type';
    typeEl.textContent = ev.event_type || ev.type || '';
    row.appendChild(typeEl);

    const timeEl = document.createElement('div');
    timeEl.className = 'timeline-time';
    timeEl.textContent = formatUtcTimestamp(ev.created_at);
    row.appendChild(timeEl);

    body.appendChild(row);

    if (ev.detail) {
      const detail = document.createElement('div');
      detail.className = 'timeline-detail';
      detail.textContent = ev.detail;
      body.appendChild(detail);
    }

    const actionAudit = formatEventActionAudit(ev);
    if (actionAudit) {
      const audit = document.createElement('div');
      audit.className = 'timeline-detail';
      audit.textContent = actionAudit;
      body.appendChild(audit);
    }

    item.appendChild(dot);
    item.appendChild(body);
    content.appendChild(item);
  });
}

// ── Logs Tab ─────────────────────────────────────────────────────────────────
let _logsInterval = null;

async function loadLogsTab(jobId, job) {
  const el = document.getElementById('tab-logs');
  el.innerHTML = '<div class="loading-msg">Loading logs...</div>';

  // Clear previous polling
  if (_logsInterval) { clearInterval(_logsInterval); _logsInterval = null; }

  async function fetchAndRender() {
    try {
      const data = await apiCall('GET', '/api/jobs/' + jobId + '/logs');
      renderLogsTab(el, data);
    } catch (_) {
      if (!el.querySelector('.logs-output')) {
        el.innerHTML = '<div class="loading-msg">No logs available.</div>';
      }
    }
  }

  await fetchAndRender();

  // Poll for live updates if job is active
  const s = (job && job.status) || '';
  const active = ['generating', 'resolving', 'submitting', 'autofilling', 'retrying',
                  'fix_in_progress', 'reanswering', 'queued'].includes(s);
  if (active) {
    _logsInterval = setInterval(fetchAndRender, 3000);
  }
}

function renderLogsTab(container, data) {
  container.innerHTML = '';
  const pre = document.createElement('pre');
  pre.className = 'logs-output';
  pre.style.cssText = 'background:#1e1e1e;color:#d4d4d4;padding:16px;border-radius:6px;' +
    'font-size:12px;font-family:monospace;overflow:auto;max-height:600px;white-space:pre-wrap;word-break:break-all;';
  pre.textContent = data.output || '(no output)';
  container.appendChild(pre);
  // Auto-scroll to bottom
  pre.scrollTop = pre.scrollHeight;
}

// ── Job actions ──────────────────────────────────────────────────────────────
let _focusBrowserInFlight = false;
async function focusCaptchaBrowser(jobId) {
  if (_focusBrowserInFlight) return;
  const job = window.jobs[jobId];
  if (job && job.status !== 'awaiting_captcha') {
    showToast(`Job is now "${statusLabel(job.status)}"`, 'info');
    return;
  }
  _focusBrowserInFlight = true;
  try {
    await apiCall('POST', `/api/jobs/${jobId}/focus-browser`);
    showToast('Browser focused', 'success');
  } catch (e) {
    // Handle 409 with optimistic status update
    try {
      const parsed = JSON.parse(e.message);
      if (parsed.current_status && window.jobs[jobId]) {
        window.jobs[jobId].status = parsed.current_status;
        renderJobActionRow(window.jobs[jobId]);
      }
    } catch (_) {}
    showToast('Focus failed: ' + e.message, 'error');
  } finally {
    _focusBrowserInFlight = false;
  }
}

async function approveJob(jobId, actionContext = createActionContext('detail', 'button')) {
  const job = window.jobs[jobId] || {};
  const company = job.company || '#' + jobId;
  const role    = job.role_title || '';
  try {
    await apiCall('POST', `/api/jobs/${jobId}/approve`, null, actionContext);
    showToast('Application approved — submitting!', 'success');
    const updated = await apiCall('GET', `/api/jobs/${jobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
  } catch (e) {
    showToast('Approve failed: ' + e.message, 'error');
  }
}

async function rejectJob(jobId, actionContext = createActionContext('detail', 'button')) {
  try {
    await apiCall('POST', `/api/jobs/${jobId}/reject`, null, actionContext);
    showToast('Draft rejected', 'info');
    const updated = await apiCall('GET', `/api/jobs/${jobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
  } catch (e) {
    showToast('Reject failed: ' + e.message, 'error');
  }
}

async function regenerateJob(jobId, actionContext = createActionContext('detail', 'button')) {
  try {
    await apiCall('POST', `/api/jobs/${jobId}/regenerate`, null, actionContext);
    showToast('Regenerating...', 'info');
    const updated = await apiCall('GET', `/api/jobs/${jobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
  } catch (e) {
    showToast('Regenerate failed: ' + e.message, 'error');
  }
}

async function resetJobToNew(jobId, actionContext = createActionContext('detail', 'button')) {
  try {
    await apiCall('POST', `/api/jobs/${jobId}/reset-to-new`, null, actionContext);
    showToast('Reset to newly added and re-queued', 'info');
    const updated = await apiCall('GET', `/api/jobs/${jobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
    await refreshQueueData();
  } catch (e) {
    showToast('Reset to new failed: ' + e.message, 'error');
  }
}

async function reanswerJob(jobId, actionContext = createActionContext('detail', 'button')) {
  try {
    await apiCall('POST', `/api/jobs/${jobId}/reanswer`, null, actionContext);
    showToast('Re-answering questions...', 'info');
    const updated = await apiCall('GET', `/api/jobs/${jobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
  } catch (e) {
    showToast('Re-answer failed: ' + e.message, 'error');
  }
}

async function retryJob(jobId, actionContext = createActionContext('detail', 'button')) {
  try {
    await apiCall('POST', `/api/jobs/${jobId}/retry`, null, actionContext);
    showToast('Job queued for retry', 'success');
    const updated = await apiCall('GET', `/api/jobs/${jobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
  } catch (e) {
    showToast('Retry failed: ' + e.message, 'error');
  }
}

async function skipJob(jobId, actionContext = createActionContext('detail', 'button')) {
  try {
    await apiCall('POST', `/api/jobs/${jobId}/skip`, null, actionContext);
    showToast('Job skipped', 'info');
    const updated = await apiCall('GET', `/api/jobs/${jobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
  } catch (e) {
    showToast('Skip failed: ' + e.message, 'error');
  }
}

async function deleteJob(jobId, actionContext = createActionContext('detail', 'button')) {
  try {
    await apiCall('DELETE', `/api/jobs/${jobId}`, null, actionContext);
    showToast('Job deleted', 'info');
    delete window.jobs[jobId];
    window.location.hash = '#/queue';
  } catch (e) {
    showToast('Delete failed: ' + e.message, 'error');
  }
}

async function archiveJob(jobId, actionContext = createActionContext('detail', 'button')) {
  try {
    await apiCall('POST', `/api/jobs/${jobId}/archive`, null, actionContext);
    showToast('Job archived', 'info');
    const updated = await apiCall('GET', `/api/jobs/${jobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
  } catch (e) {
    showToast('Archive failed: ' + e.message, 'error');
  }
}

async function unarchiveJob(jobId, actionContext = createActionContext('detail', 'button')) {
  try {
    await apiCall('POST', `/api/jobs/${jobId}/unarchive`, null, actionContext);
    showToast('Job unarchived', 'info');
    const updated = await apiCall('GET', `/api/jobs/${jobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
  } catch (e) {
    showToast('Unarchive failed: ' + e.message, 'error');
  }
}

function _tabActionBar(jobId, target, label) {
  const job = window.jobs[jobId] || null;
  if (job && job.submission_lock_state === 'locked') return document.createDocumentFragment();
  const bar = document.createElement('div');
  bar.style.cssText = 'display:flex;justify-content:flex-end;padding:8px 0;border-bottom:1px solid var(--base2);margin-bottom:12px;';
  const btn = makeBtn(label, 'btn-outline btn-sm', () => regenerateAsset(jobId, target));
  bar.appendChild(btn);
  return bar;
}

async function regenerateAsset(jobId, target, actionContext = createActionContext('detail', 'button')) {
  try {
    await apiCall('POST', `/api/jobs/${jobId}/regenerate-asset`, { target }, actionContext);
    showToast('Regenerating ' + target.replace('_', ' ') + '...', 'info');
    const updated = await apiCall('GET', `/api/jobs/${jobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
  } catch (e) {
    showToast('Regenerate failed: ' + e.message, 'error');
  }
}

async function restartPipeline(jobId, autoSubmit, actionContext = createActionContext('detail', 'button')) {
  try {
    await apiCall('POST', `/api/jobs/${jobId}/restart-pipeline`, { auto_submit: !!autoSubmit }, actionContext);
    showToast(autoSubmit ? 'Restarting → submit' : 'Restarting → draft', 'success');
    const updated = await apiCall('GET', `/api/jobs/${jobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
  } catch (e) {
    showToast('Restart failed: ' + e.message, 'error');
  }
}

async function unlockForResubmit(jobId, actionContext = createActionContext('detail', 'button')) {
  try {
    await apiCall('POST', `/api/jobs/${jobId}/unlock-resubmit`, null, actionContext);
    showToast('Job unlocked for resubmission', 'success');
    const updated = await apiCall('GET', `/api/jobs/${jobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
  } catch (e) {
    showToast('Unlock failed: ' + e.message, 'error');
  }
}

async function lockResubmission(jobId, actionContext = createActionContext('detail', 'button')) {
  try {
    await apiCall('POST', `/api/jobs/${jobId}/lock-resubmit`, null, actionContext);
    showToast('Resubmission lock restored', 'success');
    const updated = await apiCall('GET', `/api/jobs/${jobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
  } catch (e) {
    showToast('Lock failed: ' + e.message, 'error');
  }
}

async function stopJob(jobId, actionContext = createActionContext('detail', 'button')) {
  try {
    await apiCall('POST', `/api/jobs/${jobId}/stop`, null, actionContext);
    showToast('Job stopped', 'info');
    const updated = await apiCall('GET', `/api/jobs/${jobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
  } catch (e) {
    showToast('Stop failed: ' + e.message, 'error');
  }
}

async function submitBoardUrl(actionContext = createActionContext('detail', 'button')) {
  const url = document.getElementById('board-url-input').value.trim();
  if (!url || !url.startsWith('http')) {
    showToast('Enter a valid URL', 'error');
    return;
  }
  try {
    await apiCall('POST', `/api/jobs/${currentJobId}/board-url`, { url }, actionContext);
    showToast('Board URL set — job queued', 'success');
    document.getElementById('board-url-bar').style.display = 'none';
    const updated = await apiCall('GET', `/api/jobs/${currentJobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

// ── Inline editing ────────────────────────────────────────────────────────────
function makeEditable(element, fieldName, originalValue) {
  if (element.classList.contains('editing-active')) return;
  element.classList.add('editing-active');

  const isLong = originalValue && originalValue.length > 80;
  const input = document.createElement(isLong ? 'textarea' : 'input');
  input.className = isLong ? 'editing-textarea' : 'editing-input';
  input.value = window.stagedChanges[fieldName]
    ? window.stagedChanges[fieldName].edited
    : originalValue;
  if (isLong) input.rows = Math.min(8, Math.ceil(originalValue.length / 80) + 1);

  const origText = element.textContent;
  element.textContent = '';
  element.appendChild(input);
  input.focus();
  input.select();

  const save = () => {
    const newVal = input.value.trim();
    element.innerHTML = '';
    element.classList.remove('editing-active');
    if (newVal !== originalValue) {
      window.stagedChanges[fieldName] = { original: originalValue, edited: newVal };
      element.classList.add('edited');
      element.textContent = newVal;
      // Show strikethrough of original below
      let origEl = element.nextElementSibling;
      if (!origEl || !origEl.classList.contains('original-value')) {
        origEl = document.createElement('div');
        origEl.className = 'original-value';
        element.parentNode.insertBefore(origEl, element.nextSibling);
      }
      origEl.textContent = originalValue;
    } else {
      delete window.stagedChanges[fieldName];
      element.classList.remove('edited');
      element.textContent = origText;
      const origEl = element.nextElementSibling;
      if (origEl && origEl.classList.contains('original-value')) origEl.remove();
    }
    updateChangesBar();
  };

  const cancel = () => {
    element.innerHTML = '';
    element.classList.remove('editing-active');
    element.textContent = origText;
  };

  input.addEventListener('keydown', e => {
    if (!isLong && e.key === 'Enter') { e.preventDefault(); save(); }
    if (e.key === 'Escape') { e.preventDefault(); cancel(); }
  });
  input.addEventListener('blur', save);
}

function updateChangesBar() {
  const bar   = document.getElementById('changes-bar');
  const count = document.getElementById('changes-count');
  const n = Object.keys(window.stagedChanges).length;
  if (n > 0) {
    bar.style.display = 'flex';
    count.textContent = n + (n === 1 ? ' change' : ' changes');
  } else {
    bar.style.display = 'none';
  }
}

function discardAllChanges() {
  window.stagedChanges = {};
  updateChangesBar();
  // Re-render answers tab to clear highlights
  if (currentJobId) {
    const job = window.jobs[currentJobId];
    loadAnswersTab(currentJobId, job);
  }
  showToast('Changes discarded', 'info');
}

function showReviewModal() {
  const backdrop = document.getElementById('modal-backdrop');
  const modal    = document.getElementById('review-modal');
  const body     = document.getElementById('modal-body');

  const changes = Object.entries(window.stagedChanges);
  if (!changes.length) return;

  // Build diff table
  const table = document.createElement('table');
  table.className = 'diff-table';
  const thead = document.createElement('thead');
  thead.innerHTML = '<tr><th>Field</th><th>Original</th><th>New Value</th><th></th></tr>';
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  changes.forEach(([field, { original, edited }]) => {
    const tr = document.createElement('tr');

    const tdField = document.createElement('td');
    tdField.textContent = field;

    const tdOrig = document.createElement('td');
    tdOrig.className = 'diff-original';
    tdOrig.textContent = original;

    const tdNew = document.createElement('td');
    tdNew.className = 'diff-new';
    tdNew.textContent = edited;

    const tdAction = document.createElement('td');
    const revertBtn = document.createElement('button');
    revertBtn.className = 'btn btn-outline diff-revert btn-sm';
    revertBtn.textContent = 'Revert';
    revertBtn.addEventListener('click', () => {
      delete window.stagedChanges[field];
      tr.remove();
      updateChangesBar();
      if (!Object.keys(window.stagedChanges).length) closeModal();
    });
    tdAction.appendChild(revertBtn);

    tr.appendChild(tdField);
    tr.appendChild(tdOrig);
    tr.appendChild(tdNew);
    tr.appendChild(tdAction);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);

  body.innerHTML = '';
  body.appendChild(table);

  backdrop.style.display = 'block';
  modal.style.display = 'flex';
  document.body.classList.add('modal-open');
}

function closeModal() {
  document.getElementById('modal-backdrop').style.display = 'none';
  document.getElementById('review-modal').style.display = 'none';
  document.body.classList.remove('modal-open');
}

async function confirmChanges() {
  const overrides = {};
  for (const [field, { edited }] of Object.entries(window.stagedChanges)) {
    overrides[field] = edited;
  }
  try {
    await apiCall(
      'POST',
      `/api/jobs/${currentJobId}/draft-overrides`,
      { overrides },
      createActionContext('detail', 'button')
    );
    showToast(`Saved ${Object.keys(overrides).length} changes — re-answering with fixes...`, 'success');
    window.stagedChanges = {};
    updateChangesBar();
    closeModal();
    // Refresh job detail to show reanswering status
    const updated = await apiCall('GET', `/api/jobs/${currentJobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
  } catch (e) {
    showToast('Save failed: ' + e.message, 'error');
  }
}

// ── Add Jobs View ─────────────────────────────────────────────────────────────
function initAddJobs() {
  const textarea = document.getElementById('urls-input');
  if (!textarea) return;

  textarea.addEventListener('keydown', e => {
    if (e.ctrlKey && e.key === 'Enter') submitJobs();
  });
}

async function submitJobs() {
  const textarea  = document.getElementById('urls-input');
  const provSel   = document.getElementById('provider-select');
  const priSel    = document.getElementById('priority-select');
  const feedback  = document.getElementById('add-feedback');
  const btn       = document.getElementById('add-submit-btn');

  const raw = textarea.value.trim();
  if (!raw) { showToast('Enter at least one URL', 'warning'); return; }

  const urls = raw.split('\n').map(u => u.trim()).filter(u => u.startsWith('http'));
  if (!urls.length) { showToast('No valid URLs found (must start with http)', 'error'); return; }

  const provider = provSel.value || null;
  const priority = parseInt(priSel.value, 10) || 0;

  btn.disabled = true;
  btn.textContent = 'Adding...';
  feedback.style.display = 'none';

  try {
    const result = await apiCall('POST', '/api/jobs', { urls, provider, priority });
    const { added, duplicates } = result;
    feedback.className = 'add-feedback success';
    feedback.textContent = `Added ${added} job(s)${duplicates ? ` (${duplicates} duplicate${duplicates > 1 ? 's' : ''} skipped)` : ''}.`;
    feedback.style.display = 'block';
    if (added > 0) {
      textarea.value = '';
      showToast(`${added} job(s) added to queue`, 'success');
    }
  } catch (e) {
    feedback.className = 'add-feedback error';
    feedback.textContent = 'Error: ' + e.message;
    feedback.style.display = 'block';
    showToast('Failed to add jobs: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Add Jobs';
  }
}

// ── Add Jobs Modal (C key) ────────────────────────────────────────────────────

let _addJobModalOpen = false;

function openAddJobModal() {
  _addJobModalOpen = true;
  document.getElementById('addjob-backdrop').style.display = 'block';
  document.getElementById('addjob-modal').style.display = 'flex';
  document.body.classList.add('modal-open');
  const rows = document.getElementById('addjob-rows');
  rows.innerHTML = '';
  _addJobRow(rows, true);
}

function closeAddJobModal() {
  _addJobModalOpen = false;
  document.getElementById('addjob-backdrop').style.display = 'none';
  document.getElementById('addjob-modal').style.display = 'none';
  document.body.classList.remove('modal-open');
}

function _addJobRow(container, focus) {
  const row = document.createElement('div');
  row.className = 'addjob-row';
  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = 'Paste job URL...';
  input.addEventListener('keydown', _addJobRowKeydown);
  const removeBtn = document.createElement('button');
  removeBtn.className = 'addjob-remove';
  removeBtn.innerHTML = '&times;';
  removeBtn.title = 'Remove';
  removeBtn.onclick = () => {
    const allRows = container.querySelectorAll('.addjob-row');
    if (allRows.length > 1) { row.remove(); }
    else { input.value = ''; input.focus(); }
  };
  row.appendChild(input);
  row.appendChild(removeBtn);
  container.appendChild(row);
  if (focus) input.focus();
  return input;
}

function _addJobRowKeydown(e) {
  if (e.key === 'Enter' && e.shiftKey) {
    e.preventDefault();
    const container = document.getElementById('addjob-rows');
    _addJobRow(container, true);
    return;
  }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    submitAddJobModal();
    return;
  }
  if (e.key === 'Escape') {
    e.preventDefault();
    closeAddJobModal();
    return;
  }
}

async function submitAddJobModal() {
  const inputs = document.querySelectorAll('#addjob-rows input');
  const urls = [];
  inputs.forEach(i => { const v = i.value.trim(); if (v.startsWith('http')) urls.push(v); });
  if (!urls.length) { showToast('Enter at least one URL', 'warning'); return; }

  const btn = document.getElementById('addjob-submit-btn');
  btn.disabled = true;
  btn.textContent = 'Adding...';
  try {
    const result = await apiCall('POST', '/api/jobs', { urls, provider: null, priority: 0 });
    const { added, duplicates } = result;
    if (added > 0) showToast(`${added} job(s) added${duplicates ? ` (${duplicates} duplicate${duplicates > 1 ? 's' : ''})` : ''}`, 'success');
    else if (duplicates) showToast(`${duplicates} duplicate(s) skipped`, 'warning');
    closeAddJobModal();
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Add Jobs';
  }
}

// ── Dashboard View ────────────────────────────────────────────────────────────
async function renderDashboard() {
  const statsGrid = document.getElementById('dashboard-stats-grid');
  const feed      = document.getElementById('activity-feed');
  const boardDiv  = document.getElementById('board-breakdown');

  if (statsGrid) statsGrid.innerHTML = '<div class="loading-msg">Loading...</div>';
  if (feed)      feed.innerHTML      = '<div class="loading-msg">Loading...</div>';
  if (boardDiv)  boardDiv.innerHTML  = '<div class="loading-msg">Loading...</div>';

  try {
    const [counts, events, boards] = await Promise.all([
      apiCall('GET', '/api/stats/counts'),
      apiCall('GET', '/api/events/recent?limit=30'),
      apiCall('GET', '/api/stats/boards'),
    ]);

    // Worker status
    let workerRunning = false;
    try { const ws2 = await apiCall('GET', '/api/workers/status'); workerRunning = ws2.running; } catch (_) {}

    renderDashboardStats(statsGrid, counts, workerRunning);
    renderActivityFeed(feed, events);
    renderBoardBreakdown(boardDiv, boards);
  } catch (e) {
    showToast('Dashboard load error: ' + e.message, 'error');
  }
}

function renderDashboardStats(container, counts, workerRunning) {
  if (!container) return;

  // counts is an object like { queued: N, submitted: M, ... }
  const total = Object.values(counts).reduce((a, v) => a + (typeof v === 'number' ? v : 0), 0);
  const submitted  = counts.submitted || 0;
  const processing = (counts.generating || 0) + (counts.resolving || 0) + (counts.submitting || 0) +
                     (counts.autofilling || 0) + (counts.retrying || 0) + (counts.fix_in_progress || 0);
  const queued     = counts.queued || 0;
  const draft      = counts.draft || 0;
  const stopped    = counts.stopped || 0;
  const attention  = counts.needs_board_url || 0;
  const errorRate  = total > 0 ? ((stopped / total) * 100).toFixed(1) + '%' : '0%';

  const stats = [
    { label: 'Worker', value: workerRunning ? 'ON' : 'OFF', color: workerRunning ? 'green' : '' },
    { label: 'Total',  value: total,      color: '' },
    { label: 'Submitted', value: submitted, color: 'green' },
    { label: 'Processing', value: processing, color: 'cyan' },
    { label: 'Queued',  value: queued,  color: 'blue' },
    { label: 'Drafts',  value: draft,   color: 'orange' },
    { label: 'Stopped', value: stopped,  color: 'red' },
    { label: 'Needs URL', value: attention, color: 'magenta' },
    { label: 'Error Rate', value: errorRate, color: stopped > 0 ? 'red' : '' },
  ];

  container.innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="stat-label">${escapeHtml(s.label)}</div>
      <div class="stat-value ${s.color ? s.color : ''}">${escapeHtml(String(s.value))}</div>
    </div>
  `).join('');
}

function renderActivityFeed(container, events) {
  if (!container) return;
  if (!events || !events.length) {
    container.innerHTML = '<div class="loading-msg">No recent activity.</div>';
    return;
  }
  container.innerHTML = '';
  events.forEach(ev => {
    const item = document.createElement('div');
    item.className = 'activity-item';

    const dot = document.createElement('div');
    dot.className = 'activity-dot';

    const body = document.createElement('div');
    body.className = 'activity-content';

    const eventType = document.createElement('div');
    eventType.className = 'activity-event';
    eventType.textContent = ev.event_type || ev.type || '';

    const who = document.createElement('div');
    who.className = 'activity-who';
    const parts = [];
    if (ev.company) parts.push(ev.company);
    if (ev.role_title) parts.push(ev.role_title);
    if (ev.detail) parts.push(ev.detail.slice(0, 60));
    const actionAudit = formatEventActionAudit(ev);
    if (actionAudit) parts.push(actionAudit);
    who.textContent = parts.join(' — ');

    body.appendChild(eventType);
    body.appendChild(who);

    const time = document.createElement('div');
    time.className = 'activity-time';
    time.textContent = timeAgo(ev.created_at);

    item.appendChild(dot);
    item.appendChild(body);
    item.appendChild(time);
    container.appendChild(item);
  });
}

function renderBoardBreakdown(container, boards) {
  if (!container) return;
  if (!boards || !boards.length) {
    container.innerHTML = '<div class="loading-msg">No board data.</div>';
    return;
  }
  // boards is an array of { board, total, errors, error_rate }
  const maxTotal = Math.max(...boards.map(b => b.total || 0), 1);
  container.innerHTML = '';
  boards.forEach(b => {
    const row = document.createElement('div');
    row.className = 'board-bar-row';

    const label = document.createElement('div');
    label.className = 'board-bar-label';
    label.textContent = b.board || '(unknown)';

    const track = document.createElement('div');
    track.className = 'board-bar-track';

    const fill = document.createElement('div');
    fill.className = 'board-bar-fill';
    fill.style.width = ((b.total / maxTotal) * 100) + '%';
    track.appendChild(fill);

    const count = document.createElement('div');
    count.className = 'board-bar-count';
    count.textContent = b.total;

    row.appendChild(label);
    row.appendChild(track);
    row.appendChild(count);
    container.appendChild(row);
  });
}

// ── Stats View ───────────────────────────────────────────────────────────────
let allMetrics = [];

async function renderStats() {
  document.getElementById('processed-grid').innerHTML = '<div class="loading-msg">Loading...</div>';
  document.getElementById('metrics-tbody').innerHTML  = '<tr><td colspan="7">Loading...</td></tr>';
  document.getElementById('phase-chart').innerHTML    = '<div class="loading-msg">Loading...</div>';
  document.getElementById('board-error-chart').innerHTML = '<div class="loading-msg">Loading...</div>';

  try {
    const [processed, phases, boards, summary] = await Promise.all([
      apiCall('GET', '/api/stats/processed'),
      apiCall('GET', '/api/stats/phases'),
      apiCall('GET', '/api/stats/boards'),
      apiCall('GET', '/api/stats/summary'),
    ]);

    renderProcessedGrid(processed);
    renderMetricsTable(summary);
    renderPhaseChart(phases);
    renderBoardErrorChart(boards);
  } catch (e) {
    showToast('Stats load error: ' + e.message, 'error');
  }
}

function renderProcessedGrid(processed) {
  const grid = document.getElementById('processed-grid');
  if (!grid) return;
  if (!processed || typeof processed !== 'object') {
    grid.innerHTML = '<div class="loading-msg">No data.</div>';
    return;
  }
  // processed may be { today: N, week: M, month: P, total: Q }
  const items = [
    { label: 'Today',    value: processed.today  ?? processed.last_24h ?? '—' },
    { label: 'This Week', value: processed.week   ?? processed.last_7d  ?? '—' },
    { label: 'This Month', value: processed.month  ?? processed.last_30d ?? '—' },
    { label: 'All Time', value: processed.total   ?? processed.all_time ?? '—' },
  ];
  grid.innerHTML = items.map(i => `
    <div class="processed-stat">
      <div class="processed-stat-value">${escapeHtml(String(i.value))}</div>
      <div class="processed-stat-label">${escapeHtml(i.label)}</div>
    </div>
  `).join('');
}

function renderMetricsTable(data) {
  const tbody = document.getElementById('metrics-tbody');
  if (!tbody) return;

  // data may be a list of per-job metrics OR a summary object
  let metrics = [];
  if (Array.isArray(data)) {
    metrics = data;
  } else if (data && Array.isArray(data.jobs)) {
    metrics = data.jobs;
  } else if (data && typeof data === 'object') {
    // Try to build from counts/summary
    metrics = [];
  }

  allMetrics = metrics;
  renderMetricRows(tbody);
}

function renderMetricRows(tbody) {
  const metrics = [...allMetrics].sort((a, b) => {
    let av = a[metricsSortField] ?? '', bv = b[metricsSortField] ?? '';
    if (typeof av === 'string') av = av.toLowerCase();
    if (typeof bv === 'string') bv = bv.toLowerCase();
    if (av < bv) return metricsSortDir === 'asc' ? -1 : 1;
    if (av > bv) return metricsSortDir === 'asc' ? 1 : -1;
    return 0;
  });

  if (!metrics.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--base0);padding:20px">No metrics available.</td></tr>';
    return;
  }

  tbody.innerHTML = '';
  metrics.forEach(m => {
    const tr = document.createElement('tr');
    const cost = m.total_cost_usd != null ? '$' + Number(m.total_cost_usd).toFixed(4) : '—';
    const tokens = m.total_tokens != null ? Number(m.total_tokens).toLocaleString() : '—';
    const duration = m.total_duration_seconds != null
      ? formatDuration(m.total_duration_seconds) : '—';

    const cells = [
      m.company || m.job_id || '—',
      m.role_title || '—',
      cost,
      tokens,
      m.fix_attempts ?? '—',
      m.manual_interventions ?? '—',
      duration,
    ];
    cells.forEach(val => {
      const td = document.createElement('td');
      td.textContent = val;
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

function sortMetrics(field) {
  if (metricsSortField === field) {
    metricsSortDir = metricsSortDir === 'asc' ? 'desc' : 'asc';
  } else {
    metricsSortField = field;
    metricsSortDir = 'asc';
  }
  const tbody = document.getElementById('metrics-tbody');
  if (tbody) renderMetricRows(tbody);
}

function formatDuration(secs) {
  if (secs == null) return '—';
  secs = Math.round(secs);
  if (secs < 60)  return secs + 's';
  if (secs < 3600) return Math.floor(secs / 60) + 'm ' + (secs % 60) + 's';
  return Math.floor(secs / 3600) + 'h ' + Math.floor((secs % 3600) / 60) + 'm';
}

function renderPhaseChart(phases) {
  const container = document.getElementById('phase-chart');
  if (!container) return;
  if (!phases || !phases.length) {
    container.innerHTML = '<div class="loading-msg">No phase data.</div>';
    return;
  }
  // phases: [{ phase, avg_seconds }, ...]
  const maxVal = Math.max(...phases.map(p => p.avg_seconds || 0), 1);
  container.innerHTML = '';
  phases.forEach(p => {
    const row = document.createElement('div');
    row.className = 'chart-row';

    const label = document.createElement('div');
    label.className = 'chart-label';
    label.textContent = p.phase || '';

    const track = document.createElement('div');
    track.className = 'chart-track';

    const fill = document.createElement('div');
    fill.className = 'chart-fill';
    fill.style.width = ((p.avg_seconds / maxVal) * 100) + '%';
    track.appendChild(fill);

    const val = document.createElement('div');
    val.className = 'chart-value';
    val.textContent = formatDuration(p.avg_seconds);

    row.appendChild(label);
    row.appendChild(track);
    row.appendChild(val);
    container.appendChild(row);
  });
}

function renderBoardErrorChart(boards) {
  const container = document.getElementById('board-error-chart');
  if (!container) return;
  if (!boards || !boards.length) {
    container.innerHTML = '<div class="loading-msg">No board data.</div>';
    return;
  }
  // boards: [{ board, total, errors, error_rate }, ...]
  const maxRate = Math.max(...boards.map(b => b.error_rate || 0), 0.01);
  container.innerHTML = '';
  boards.forEach(b => {
    const row = document.createElement('div');
    row.className = 'chart-row';

    const label = document.createElement('div');
    label.className = 'chart-label';
    label.textContent = (b.board || '(unknown)') + ` (${b.total || 0})`;

    const track = document.createElement('div');
    track.className = 'chart-track';

    const fill = document.createElement('div');
    fill.className = 'chart-fill error-bar';
    const rate = b.error_rate || 0;
    fill.style.width = ((rate / maxRate) * 100) + '%';
    track.appendChild(fill);

    const val = document.createElement('div');
    val.className = 'chart-value';
    const pct = typeof rate === 'number' ? (rate * 100).toFixed(1) + '%' : '—';
    val.textContent = pct;

    row.appendChild(label);
    row.appendChild(track);
    row.appendChild(val);
    container.appendChild(row);
  });
}

// ── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initScrollableDockRails();
  _syncQueueSortControls();
  _syncWorkerSortControls();
  syncSearchClearButton();
  initAddJobs();

  // Hamburger menu toggle
  const hamburger = document.getElementById('hamburger');
  const navLinks  = document.getElementById('nav-links');
  if (hamburger && navLinks) {
    hamburger.addEventListener('click', () => navLinks.classList.toggle('open'));
  }

  // Select-all checkbox (delegated from thead)
  // (handlers attached inline in HTML)

  // Search debounce (attached inline via oninput)

  // Board filter (attached inline via onchange)

  // ── Keyboard Shortcuts & Command Palette ─────────────────────────
  initKeyboardShortcuts();
  initCmdK();

  bootstrapApp();
});

// ── Theme System ──────────────────────────────────────────────────

const THEMES = [
  { id: 'light',        label: 'Light',        icon: '\u2600' },
  { id: 'pure-light',   label: 'Pure Light',   icon: '\u25CB' },
  { id: 'dark',         label: 'Dark',         icon: '\u263E' },
  { id: 'magic-blue',   label: 'Magic Blue',   icon: '\u2726' },
  { id: 'classic-dark', label: 'Classic Dark',  icon: '\u25CF' },
  { id: 'midnight',     label: 'Midnight',     icon: '\u2605' },
  { id: 'nord',         label: 'Nord',         icon: '\u2744' },
  { id: 'catppuccin',   label: 'Catppuccin',   icon: '\u2615' },
];

function applyTheme(themeId) {
  if (themeId === 'light') {
    document.documentElement.removeAttribute('data-theme');
  } else {
    document.documentElement.setAttribute('data-theme', themeId);
  }
  localStorage.setItem('jobapps-theme', themeId);
  // Sync settings dropdown if visible
  const sel = document.getElementById('setting-theme');
  if (sel) sel.value = themeId;
}

function applyFontSize(size) {
  if (size === 'default') {
    document.documentElement.removeAttribute('data-font-size');
  } else {
    document.documentElement.setAttribute('data-font-size', size);
  }
  localStorage.setItem('jobapps-font-size', size);
  const sel = document.getElementById('setting-font-size');
  if (sel) sel.value = size;
}

function applyCompact(on) {
  document.documentElement.setAttribute('data-compact', on ? 'true' : 'false');
  localStorage.setItem('jobapps-compact', on ? 'true' : 'false');
  const cb = document.getElementById('setting-compact');
  if (cb) cb.checked = on;
}

function saveSetting(key, value) {
  localStorage.setItem('jobapps-' + key, JSON.stringify(value));
}

function loadSetting(key, fallback) {
  const v = localStorage.getItem('jobapps-' + key);
  if (v === null) return fallback;
  try { return JSON.parse(v); } catch (_) { return v; }
}

function initLocalSettings() {
  const theme = localStorage.getItem('jobapps-theme') || 'light';
  const fontSize = localStorage.getItem('jobapps-font-size') || 'default';
  const compact = localStorage.getItem('jobapps-compact') === 'true';

  applyTheme(theme);
  applyFontSize(fontSize);
  applyCompact(compact);
}

function _bootstrapOnboardingComplete() {
  return !!(_bootstrapState && _bootstrapState.onboarding && _bootstrapState.onboarding.complete);
}

function _setOnboardingCheckItem(id, ready, message) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.toggle('complete', !!ready);
  el.classList.toggle('missing', !ready);
  el.textContent = `${ready ? 'Ready' : 'Needed'}: ${message}`;
}

function renderBootstrapState(state) {
  _bootstrapState = state;
  const onboarding = (state && state.onboarding) || {};
  const required = onboarding.required_materials || {};
  const recommended = onboarding.recommended_materials || {};
  const status = document.getElementById('settings-onboarding-status');
  const continueBtn = document.getElementById('settings-onboarding-continue-btn');
  const complete = !!onboarding.complete;

  if (status) {
    status.textContent = complete
      ? 'Setup complete. Queue and worker features are ready.'
      : 'Import your master resume and add at least one provider credential to unlock the queue. Application profile, work stories, and candidate context stay editable below.';
  }

  _setOnboardingCheckItem('settings-onboarding-master-resume', !!required.master_resume, 'Master resume imported');
  _setOnboardingCheckItem(
    'settings-onboarding-provider-credentials',
    !!onboarding.credentials_ready,
    'At least one LLM provider credential configured'
  );
  _setOnboardingCheckItem(
    'settings-onboarding-application-profile',
    !!recommended.application_profile,
    'Application profile reviewed or added'
  );
  _setOnboardingCheckItem(
    'settings-onboarding-work-stories',
    !!recommended.work_stories,
    'Work stories imported'
  );
  _setOnboardingCheckItem(
    'settings-onboarding-candidate-context',
    !!recommended.candidate_context,
    'Candidate context imported'
  );

  if (continueBtn) continueBtn.disabled = !complete;
}

async function loadBootstrap(force = false) {
  if (!force && _bootstrapState) {
    renderBootstrapState(_bootstrapState);
    return _bootstrapState;
  }
  if (!force && _bootstrapLoadPromise) return _bootstrapLoadPromise;

  _bootstrapLoadPromise = (async () => {
    const data = await apiCall('GET', '/api/bootstrap');
    renderBootstrapState(data);
    return data;
  })().finally(() => {
    _bootstrapLoadPromise = null;
  });

  return _bootstrapLoadPromise;
}

function startRealtimeAppServices() {
  if (_realtimeAppServicesStarted) return;
  _realtimeAppServicesStarted = true;
  connectWS();
  if (!queueRefreshTimer) queueRefreshTimer = setInterval(refreshPassiveViewData, 5000);
  apiCall('GET', '/api/workers/status').then(d => updateWorkerButton(d.running, d.active_jobs)).catch(() => {});
}

async function bootstrapApp() {
  initLocalSettings();
  try {
    await loadBootstrap(true);
  } catch (error) {
    _bootstrapState = { onboarding: { complete: true } };
    showToast('Bootstrap failed: ' + error.message, 'error');
  }

  if (!_bootstrapOnboardingComplete()) {
    if (location.hash !== '#settings') {
      location.hash = '#settings';
    } else {
      navigate();
    }
    return;
  }

  startRealtimeAppServices();
  navigate();
}

function initSettings() {
  renderBootstrapState(_bootstrapState);
  loadServerSettings();
}

function _setSettingsStatus(message, tone = 'info') {
  const status = document.getElementById('settings-status');
  if (!status) return;
  status.textContent = message;
  status.dataset.tone = tone;
}

function _setInputValue(id, value) {
  const input = document.getElementById(id);
  if (input) input.value = value || '';
}

function _setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value || '';
}

function _setCredentialStatus(key, meta, pendingClear = false) {
  const inputId = SERVER_SETTINGS_CREDENTIAL_BY_KEY[key];
  const status = document.getElementById(`${inputId}-status`);
  if (!status) return;
  if (pendingClear) {
    status.textContent = 'Will be cleared on save';
    return;
  }
  if (meta && meta.configured) {
    status.textContent = `Configured (${meta.preview || 'hidden'})`;
    return;
  }
  if (key === 'openai_api_keys') {
    status.textContent = 'Optional comma-separated pool';
    return;
  }
  status.textContent = 'Not configured';
}

function clearCredentialPending(key) {
  if (!_pendingCredentialClears.has(key)) return;
  _pendingCredentialClears.delete(key);
  _setCredentialStatus(key, (_serverSettingsCache && _serverSettingsCache.credentials || {})[key] || null, false);
}

function markCredentialForClear(key) {
  const inputId = SERVER_SETTINGS_CREDENTIAL_BY_KEY[key];
  if (!inputId) return;
  _pendingCredentialClears.add(key);
  const input = document.getElementById(inputId);
  if (input) input.value = '';
  _setCredentialStatus(key, null, true);
}

function renderServerSettings(settings) {
  _serverSettingsCache = settings;
  const materials = settings.materials || {};
  SERVER_SETTINGS_MATERIAL_FIELDS.forEach(([key, inputId]) => {
    const entry = materials[key] || {};
    _setInputValue(inputId, entry.content || '');
    _setText(`${inputId}-path`, entry.path || '');
  });

  const providers = settings.providers || {};
  SERVER_SETTINGS_PROVIDER_FIELDS.forEach(([key, inputId]) => {
    _setInputValue(inputId, providers[key] || '');
  });
  const steelLocal = document.getElementById('settings-steel-local');
  if (steelLocal) steelLocal.checked = !!providers.steel_local;

  const credentials = settings.credentials || {};
  SERVER_SETTINGS_CREDENTIAL_FIELDS.forEach(([key, inputId]) => {
    const input = document.getElementById(inputId);
    if (input) input.value = '';
    _setCredentialStatus(key, credentials[key] || null, _pendingCredentialClears.has(key));
  });
}

function continueToQueueFromSettings() {
  if (!_bootstrapOnboardingComplete()) {
    showToast('Import a master resume and add a provider key first.', 'info');
    return;
  }
  startRealtimeAppServices();
  location.hash = '#queue';
}

async function loadServerSettings(force = false) {
  if (!force && _serverSettingsCache) {
    renderServerSettings(_serverSettingsCache);
    return _serverSettingsCache;
  }
  if (!force && _serverSettingsLoadPromise) return _serverSettingsLoadPromise;

  _setSettingsStatus('Loading backend settings...', 'info');
  _serverSettingsLoadPromise = (async () => {
    const data = await apiCall('GET', '/api/settings');
    renderServerSettings(data);
    _setSettingsStatus('Backend settings loaded.', 'success');
    return data;
  })()
    .catch((error) => {
      _setSettingsStatus('Failed to load backend settings: ' + error.message, 'error');
      throw error;
    })
    .finally(() => {
      _serverSettingsLoadPromise = null;
    });

  return _serverSettingsLoadPromise;
}

function _collectServerSettingsPayload() {
  const payload = {
    materials: {},
    providers: {},
    credentials: {},
  };

  SERVER_SETTINGS_MATERIAL_FIELDS.forEach(([key, inputId]) => {
    const input = document.getElementById(inputId);
    payload.materials[key] = input ? input.value : '';
  });

  SERVER_SETTINGS_PROVIDER_FIELDS.forEach(([key, inputId]) => {
    const input = document.getElementById(inputId);
    payload.providers[key] = input ? input.value.trim() : '';
  });
  const steelLocal = document.getElementById('settings-steel-local');
  payload.providers.steel_local = !!(steelLocal && steelLocal.checked);

  SERVER_SETTINGS_CREDENTIAL_FIELDS.forEach(([key, inputId]) => {
    const input = document.getElementById(inputId);
    const value = input ? input.value.trim() : '';
    if (value) {
      payload.credentials[key] = value;
      _pendingCredentialClears.delete(key);
    } else if (_pendingCredentialClears.has(key)) {
      payload.credentials[key] = '';
    }
  });

  return payload;
}

function fillStarterApplicationProfile() {
  const input = document.getElementById('settings-application-profile');
  if (!input) return;
  input.value = STARTER_APPLICATION_PROFILE;
  showToast('Starter application profile loaded. Save settings to persist it.', 'info');
}

function _settingsImportUrlId(materialKey) {
  const inputId = SERVER_SETTINGS_MATERIAL_BY_KEY[materialKey];
  return inputId ? `${inputId}-import-url` : '';
}

function _materialLabel(materialKey) {
  return SERVER_SETTINGS_MATERIAL_LABELS[materialKey] || materialKey;
}

function _fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error('Failed to read file'));
    reader.onload = () => {
      const result = String(reader.result || '');
      const marker = 'base64,';
      const markerIndex = result.indexOf(marker);
      resolve(markerIndex === -1 ? '' : result.slice(markerIndex + marker.length));
    };
    reader.readAsDataURL(file);
  });
}

async function importServerMaterial(materialKey, options = {}) {
  const label = _materialLabel(materialKey);
  const payload = { material_key: materialKey };
  if (typeof options.text === 'string') payload.text = options.text;
  if (typeof options.source_url === 'string') payload.source_url = options.source_url;
  if (typeof options.file_name === 'string') payload.file_name = options.file_name;
  if (typeof options.content_type === 'string') payload.content_type = options.content_type;
  if (typeof options.content_base64 === 'string') payload.content_base64 = options.content_base64;

  _setSettingsStatus(`Importing ${label}...`, 'info');
  try {
    const data = await apiCall('POST', '/api/settings/materials/import', payload);
    if (data && data.settings) renderServerSettings(data.settings);
    if (data && data.bootstrap) {
      renderBootstrapState(data.bootstrap);
      if (_bootstrapOnboardingComplete()) startRealtimeAppServices();
    }
    _setSettingsStatus(`${label} imported.`, 'success');
    showToast(`${label} imported`, 'success');
    return data;
  } catch (error) {
    _setSettingsStatus(`Import failed: ${error.message}`, 'error');
    showToast(`Import failed: ${error.message}`, 'error');
    throw error;
  }
}

async function importMaterialFromUrl(materialKey) {
  const urlId = _settingsImportUrlId(materialKey);
  const input = urlId ? document.getElementById(urlId) : null;
  const sourceUrl = input ? input.value.trim() : '';
  if (!sourceUrl) {
    showToast('Enter a public URL to import.', 'info');
    return;
  }
  await importServerMaterial(materialKey, { source_url: sourceUrl });
  if (input) input.value = '';
}

async function importMaterialFromFile(materialKey, input) {
  const file = input && input.files && input.files[0];
  if (!file) return;
  try {
    const contentBase64 = await _fileToBase64(file);
    await importServerMaterial(materialKey, {
      file_name: file.name,
      content_type: file.type || '',
      content_base64: contentBase64,
    });
  } finally {
    input.value = '';
  }
}

async function saveServerSettings() {
  const btn = document.getElementById('settings-save-btn');
  if (btn) btn.disabled = true;
  _setSettingsStatus('Saving settings...', 'info');
  try {
    const payload = _collectServerSettingsPayload();
    const data = await apiCall('POST', '/api/settings', payload);
    _pendingCredentialClears.clear();
    renderServerSettings(data);
    await loadBootstrap(true);
    if (_bootstrapOnboardingComplete()) startRealtimeAppServices();
    _setSettingsStatus('Settings saved.', 'success');
    showToast('Settings saved', 'success');
  } catch (e) {
    _setSettingsStatus('Save failed: ' + e.message, 'error');
    showToast('Save failed: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── URLs View ─────────────────────────────────────────────────────

function _getCheckedValues(containerId) {
  const el = document.getElementById(containerId);
  if (!el) return [];
  return [...el.querySelectorAll('input[type="checkbox"]:checked')].map(cb => cb.value);
}

function _populateBoardChips() {
  const container = document.getElementById('urls-board-chips');
  if (!container) return;
  const boards = new Set();
  Object.values(window.jobs || {}).forEach(j => { if (j.board) boards.add(j.board); });
  const sorted = [...boards].sort();
  if (!container.children.length || container.dataset.boards !== sorted.join(',')) {
    container.dataset.boards = sorted.join(',');
    container.innerHTML = sorted.map(b =>
      '<label class="urls-chip"><input type="checkbox" value="' + escapeHtml(b) + '" onchange="renderUrlsView()" checked> ' + escapeHtml(b) + '</label>'
    ).join('');
  }
}

function renderUrlsView() {
  _populateBoardChips();
  const selectedStatuses = _getCheckedValues('urls-status-chips');
  const selectedBoards = _getCheckedValues('urls-board-chips');
  const search = (document.getElementById('urls-search') || {}).value || '';
  const searchLower = search.toLowerCase().trim();
  const urlType = (document.getElementById('urls-type') || {}).value || 'source';
  const output = document.getElementById('urls-output');
  const countEl = document.getElementById('urls-count');
  if (!output) return;

  let jobs = Object.values(window.jobs || {});

  // Filter by checked statuses
  if (selectedStatuses.length > 0) {
    jobs = jobs.filter(j => {
      const s = j.status || '';
      for (const f of selectedStatuses) {
        if (f === 'processing' && PROCESSING_STATUSES.has(s)) return true;
        if (f === 'stopped' && _isStopped(s)) return true;
        if (f === 'queued' && (s === 'queued' || s === 'queued_submit')) return true;
        if (s === f) return true;
      }
      return false;
    });
  }

  // Filter by checked boards
  if (selectedBoards.length > 0) {
    jobs = jobs.filter(j => !j.board || selectedBoards.includes(j.board));
  }

  // Filter by search text
  if (searchLower) {
    jobs = jobs.filter(j => {
      const hay = [j.company, j.role_title, j.url, j.board_url, j.board].join(' ').toLowerCase();
      return hay.includes(searchLower);
    });
  }

  // Extract URLs
  const urls = [];
  jobs.forEach(j => {
    if (urlType === 'source' || urlType === 'both') {
      if (j.url) urls.push(j.url);
      else if (j.source_url) urls.push(j.source_url);
    }
    if (urlType === 'board' || urlType === 'both') {
      if (j.board_url && j.board_url !== j.url) urls.push(j.board_url);
    }
  });

  const unique = [...new Set(urls)];
  output.value = unique.join('\n');
  if (countEl) countEl.textContent = unique.length + ' URL' + (unique.length !== 1 ? 's' : '');
}

function copyAllUrls() {
  const output = document.getElementById('urls-output');
  if (!output || !output.value) return;
  navigator.clipboard.writeText(output.value).then(
    () => showToast('Copied ' + output.value.split('\n').length + ' URLs', 'success'),
    () => { output.select(); document.execCommand('copy'); showToast('Copied', 'success'); }
  );
}

// ── Command Palette ───────────────────────────────────────────────

let cmdkOpen = false;
let cmdkIndex = 0;
let cmdkFiltered = [];

function _activeView() {
  const hash = location.hash.replace('#', '');
  if (hash.startsWith('job-modal/')) return 'job';
  const view = hash.split('/')[0] || 'queue';
  return view;
}

function _jobRouteHash(jobId, modal = false) {
  return modal ? '#job-modal/' + jobId : '#job/' + jobId;
}

function _currentJobRouteIsModal() {
  return location.hash.startsWith('#job-modal/');
}

function _navigateToJob(jobId, modal = false) {
  if (!jobId) return;
  location.hash = _jobRouteHash(jobId, modal);
}

function _closeVisibleOverlay() {
  if (cmdkOpen) {
    closeCmdK();
    return true;
  }
  if (_addJobModalOpen) {
    closeAddJobModal();
    return true;
  }
  if (document.getElementById('review-modal')?.style.display !== 'none') {
    closeModal();
    return true;
  }
  if (document.getElementById('prep-modal')?.style.display !== 'none') {
    closePrepModal();
    return true;
  }
  if (document.getElementById('dedup-modal')?.style.display !== 'none') {
    closeDedupModal();
    return true;
  }
  if (_isJobDetailModalOpen()) {
    closeJobDetailModal();
    return true;
  }
  return false;
}

function _cmdkCommands() {
  const view = _activeView();
  const inDetail = view === 'job';
  const job = inDetail && currentJobId ? (window.jobs[currentJobId] || {}) : null;
  const isDraft = job && job.status === 'draft';
  const isProcessing = job && PROCESSING_STATUSES.has(job.status);
  const isLockedSubmission = job && job.submission_lock_state === 'locked';
  const isUnlockedSubmission = job && job.submission_lock_state === 'unlocked_for_resubmit';

  const cmds = [];

  // Navigation
  cmds.push({ group: 'Navigation', label: 'Go to Queue',     icon: '\u2630', keys: ['G', 'Q'], action: () => { location.hash = '#queue'; } });
  cmds.push({ group: 'Navigation', label: 'Go to Add Jobs',  icon: '\u002B', keys: ['G', 'A'], action: () => { location.hash = '#add'; } });
  cmds.push({ group: 'Navigation', label: 'Go to Discover',  icon: '\u{1F50D}', keys: ['G', 'I'], action: () => { location.hash = '#discover'; } });
  cmds.push({ group: 'Navigation', label: 'Go to Dashboard', icon: '\u2261', keys: ['G', 'D'], action: () => { location.hash = '#dashboard'; } });
  cmds.push({ group: 'Navigation', label: 'Go to Stats',     icon: '\u2237', keys: ['G', 'S'], action: () => { location.hash = '#stats'; } });
  cmds.push({ group: 'Navigation', label: 'Go to Settings',  icon: '\u2699', keys: ['G', 'E'], action: () => { location.hash = '#settings'; } });
  cmds.push({ group: 'Navigation', label: 'Go to URLs',      icon: '\u{1F517}', keys: ['G', 'U'], action: () => { location.hash = '#urls'; } });
  cmds.push({ group: 'Navigation', label: 'Add Jobs',        icon: '\u002B', keys: ['C'],       action: () => openAddJobModal() });

  // Job actions (only in detail view)
  if (inDetail && job) {
    if (isLockedSubmission) {
      cmds.push({ group: 'Job Actions', label: 'Unlock to Resubmit', icon: '\u{1F513}', keys: [], action: () => unlockForResubmit(job.id, createActionContext('detail', 'command_palette')) });
    }
    if (isUnlockedSubmission) {
      cmds.push({ group: 'Job Actions', label: 'Lock Resubmission', icon: '\u{1F512}', keys: [], action: () => lockResubmission(job.id, createActionContext('detail', 'command_palette')) });
    }
    if (!isLockedSubmission && isDraft) {
      cmds.push({ group: 'Job Actions', label: 'Approve + Submit',      icon: '\u2713', keys: ['A'],       action: () => approveJob(job.id, createActionContext('detail', 'command_palette')) });
      cmds.push({ group: 'Job Actions', label: 'Reset to New',          icon: '\u21BA', keys: [],          action: () => resetJobToNew(job.id, createActionContext('detail', 'command_palette')) });
      cmds.push({ group: 'Job Actions', label: 'Restart \u2192 Draft',  icon: '\u21BB', keys: ['R'],       action: () => restartPipeline(job.id, false, createActionContext('detail', 'command_palette')) });
      cmds.push({ group: 'Job Actions', label: 'Restart \u2192 Submit', icon: '\u21BB', keys: ['Shift+R'], action: () => restartPipeline(job.id, true, createActionContext('detail', 'command_palette')) });
      cmds.push({ group: 'Job Actions', label: 'Delete Job',            icon: '\u2717', keys: ['D'],       action: () => deleteJob(job.id, createActionContext('detail', 'command_palette')) });
    }
    if (isProcessing) {
      cmds.push({ group: 'Job Actions', label: 'Stop Job', icon: '\u25A0', keys: ['S'], action: () => stopJob(job.id, createActionContext('detail', 'command_palette')) });
    }
    if (!isLockedSubmission && !isProcessing && !isDraft) {
      cmds.push({ group: 'Job Actions', label: 'Restart \u2192 Draft',  icon: '\u21BB', keys: ['R'],       action: () => restartPipeline(job.id, false, createActionContext('detail', 'command_palette')) });
      cmds.push({ group: 'Job Actions', label: 'Restart \u2192 Submit', icon: '\u21BB', keys: ['Shift+R'], action: () => restartPipeline(job.id, true, createActionContext('detail', 'command_palette')) });
      cmds.push({ group: 'Job Actions', label: 'Delete Job',            icon: '\u2717', keys: ['D'],       action: () => deleteJob(job.id, createActionContext('detail', 'command_palette')) });
    }
    if (!job.archived) {
      cmds.push({ group: 'Job Actions', label: 'Archive Job', icon: '\u2709', keys: [], action: () => archiveJob(job.id, createActionContext('detail', 'command_palette')) });
    } else {
      cmds.push({ group: 'Job Actions', label: 'Unarchive Job', icon: '\u2709', keys: [], action: () => unarchiveJob(job.id, createActionContext('detail', 'command_palette')) });
    }
    cmds.push({ group: 'Job Actions', label: 'Back to Queue', icon: '\u2190', keys: ['\u232B'], action: () => { location.hash = '#queue'; } });
  }

  // Tabs (only in detail view)
  if (inDetail) {
    const tabs = ['Answers', 'Resume', 'Cover Letter', 'Screenshot', 'Confirmation', 'Logs', 'Timeline', 'Interview Prep'];
    tabs.forEach((t, i) => {
      const tabKey = t.toLowerCase().replace(/ /g, '-');
      cmds.push({ group: 'Tabs', label: t, icon: String(i + 1), keys: i < 9 ? [String(i + 1)] : [], action: () => switchTab(tabKey, null, true) });
    });
  }

  // Regenerate actions (only in detail view)
  if (!isLockedSubmission && inDetail && job) {
    cmds.push({ group: 'Regenerate', label: 'Regenerate Answers',      icon: '\u21BB', keys: ['Y'], action: () => regenerateAsset(job.id, 'answers') });
    cmds.push({ group: 'Regenerate', label: 'Regenerate Resume',       icon: '\u21BB', keys: ['U'], action: () => regenerateAsset(job.id, 'resume') });
    cmds.push({ group: 'Regenerate', label: 'Regenerate Cover Letter', icon: '\u21BB', keys: ['I'], action: () => regenerateAsset(job.id, 'cover_letter') });
    cmds.push({ group: 'Regenerate', label: 'Generate Interview Prep', icon: '\u{1F4DD}', keys: ['P'], action: () => openPrepModal() });
  }

  // External links (only in detail view)
  if (inDetail && job) {
    if (job.source_url) cmds.push({ group: 'Links', label: 'Open LinkedIn',  icon: '\u{1F517}', keys: [], action: () => window.open(job.source_url, '_blank') });
    if (job.url)        cmds.push({ group: 'Links', label: 'Open Job Board', icon: '\u{1F517}', keys: ['B'], action: () => window.open(job.url, '_blank') });
  }

  // Queue actions
  if (view === 'queue') {
    cmds.push({ group: 'Queue', label: 'Focus Search',       icon: '\u2315', keys: ['/'], action: () => { document.querySelector('.search-input')?.focus(); } });
    cmds.push({ group: 'Queue', label: 'Filter: All',        icon: '\u25CB', keys: [],    action: () => setStatusFilter('') });
    cmds.push({ group: 'Queue', label: 'Filter: Draft',      icon: '\u25CB', keys: [],    action: () => setStatusFilter('draft') });
    cmds.push({ group: 'Queue', label: 'Filter: Processing', icon: '\u25CB', keys: [],    action: () => setStatusFilter('processing') });
    cmds.push({ group: 'Queue', label: 'Filter: Submitted',  icon: '\u25CB', keys: [],    action: () => setStatusFilter('submitted') });
    cmds.push({ group: 'Queue', label: 'Filter: Archived',   icon: '\u25CB', keys: [],    action: () => setStatusFilter('archived') });
    if (selectedIds.size > 0) {
      if (!selectedJobsContainLockedSubmission()) {
        cmds.push({ group: 'Selection (' + selectedIds.size + ')', label: 'Approve + Submit',  icon: '\u2713', keys: [], action: () => bulkApprove(createActionContext('queue', 'command_palette')) });
        cmds.push({ group: 'Selection (' + selectedIds.size + ')', label: 'Restart \u2192 Draft',  icon: '\u21BB', keys: [], action: () => bulkRestart(false, createActionContext('queue', 'command_palette')) });
        cmds.push({ group: 'Selection (' + selectedIds.size + ')', label: 'Restart \u2192 Submit', icon: '\u21BB', keys: [], action: () => bulkRestart(true, createActionContext('queue', 'command_palette')) });
      }
      cmds.push({ group: 'Selection (' + selectedIds.size + ')', label: 'Archive Selected',  icon: '\u2709', keys: [], action: () => bulkArchive(createActionContext('queue', 'command_palette')) });
      cmds.push({ group: 'Selection (' + selectedIds.size + ')', label: 'Stop Selected',    icon: '\u25A0', keys: [], action: () => bulkStop(createActionContext('queue', 'command_palette')) });
      cmds.push({ group: 'Selection (' + selectedIds.size + ')', label: 'Delete Selected',  icon: '\u2717', keys: [], action: () => bulkDelete(createActionContext('queue', 'command_palette')) });
    }
  }

  // Server
  cmds.push({ group: 'Server', label: 'Restart Web Server', icon: '\u21BB', keys: [], action: () => restartServer() });
  cmds.push({ group: 'Server', label: 'Toggle Workers',    icon: '\u26A1', keys: [], action: () => toggleWorker() });

  // Themes
  THEMES.forEach(t => {
    cmds.push({ group: 'Theme', label: t.label, icon: t.icon, keys: [], action: () => { applyTheme(t.id); showToast('Theme: ' + t.label, 'info'); } });
  });

  // Jobs search
  const jobsList = Object.values(window.jobs || {});
  jobsList.slice(0, 20).forEach(j => {
    const label = (j.company || '?') + ' \u2014 ' + (j.role_title || '?');
    cmds.push({ group: 'Jobs', label: label, icon: '#' + j.id, subtitle: j.status, keys: [], action: () => { location.hash = '#job/' + j.id; } });
  });

  return cmds;
}

function openCmdK() {
  const backdrop = document.getElementById('cmdk-backdrop');
  const palette = document.getElementById('cmdk');
  const input = document.getElementById('cmdk-input');
  backdrop.style.display = 'block';
  palette.style.display = 'flex';
  input.value = '';
  cmdkOpen = true;
  cmdkIndex = 0;
  _renderCmdKList('');
  input.focus();
}

function closeCmdK() {
  document.getElementById('cmdk-backdrop').style.display = 'none';
  document.getElementById('cmdk').style.display = 'none';
  cmdkOpen = false;
}

function _renderCmdKList(query) {
  const list = document.getElementById('cmdk-list');
  const allCmds = _cmdkCommands();
  const q = query.toLowerCase().trim();

  cmdkFiltered = q
    ? allCmds.filter(c => c.label.toLowerCase().includes(q) || c.group.toLowerCase().includes(q) || (c.subtitle || '').toLowerCase().includes(q))
    : allCmds;

  if (!cmdkFiltered.length) {
    list.innerHTML = '<div class="cmdk-empty">No results found</div>';
    return;
  }

  let html = '';
  let lastGroup = '';
  cmdkFiltered.forEach((cmd, i) => {
    if (cmd.group !== lastGroup) {
      html += '<div class="cmdk-group-label">' + escapeHtml(cmd.group) + '</div>';
      lastGroup = cmd.group;
    }
    const active = i === cmdkIndex ? ' active' : '';
    const shortcut = cmd.keys.length
      ? '<span class="cmdk-shortcut">' + cmd.keys.map(k => '<kbd>' + escapeHtml(k) + '</kbd>').join('') + '</span>'
      : '';
    const subtitle = cmd.subtitle ? '<span class="cmdk-item-subtitle">' + escapeHtml(cmd.subtitle) + '</span>' : '';
    html += '<div class="cmdk-item' + active + '" data-index="' + i + '" onclick="cmdkSelect(' + i + ')" onmouseenter="cmdkHover(' + i + ')">'
      + '<span class="cmdk-item-icon">' + cmd.icon + '</span>'
      + '<span class="cmdk-item-label">' + escapeHtml(cmd.label) + subtitle + '</span>'
      + shortcut
      + '</div>';
  });
  list.innerHTML = html;
  const activeEl = list.querySelector('.cmdk-item.active');
  if (activeEl) activeEl.scrollIntoView({ block: 'nearest' });
}

function cmdkSelect(index) {
  if (cmdkFiltered[index]) {
    closeCmdK();
    cmdkFiltered[index].action();
  }
}

function cmdkHover(index) {
  cmdkIndex = index;
  _renderCmdKList(document.getElementById('cmdk-input').value);
}

function initCmdK() {
  const input = document.getElementById('cmdk-input');
  if (!input) return;
  input.addEventListener('input', () => {
    cmdkIndex = 0;
    _renderCmdKList(input.value);
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); cmdkIndex = Math.min(cmdkIndex + 1, cmdkFiltered.length - 1); _renderCmdKList(input.value); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); cmdkIndex = Math.max(cmdkIndex - 1, 0); _renderCmdKList(input.value); }
    else if (e.key === 'Enter') { e.preventDefault(); cmdkSelect(cmdkIndex); }
    else if (e.key === 'Escape') { e.preventDefault(); closeCmdK(); }
  });
}

// ── Keyboard Shortcuts ────────────────────────────────────────────

let _pendingG = false;
let _pendingGTimer = null;

function _isInputFocused() {
  const tag = document.activeElement?.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || document.activeElement?.isContentEditable;
}

function initKeyboardShortcuts() {
  document.addEventListener('keydown', e => {
    // Cmd+K / Ctrl+K — command palette
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
      e.preventDefault();
      if (cmdkOpen) closeCmdK(); else openCmdK();
      return;
    }
    // Cmd+Enter in add-jobs view
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      const addView = document.getElementById('view-add');
      if (addView && addView.style.display !== 'none') { submitJobs(); return; }
    }
    // Escape
    if (e.key === 'Escape') {
      if (_closeVisibleOverlay()) return;
      return;
    }
    // ? — show shortcut help overlay (works even in inputs via Shift+/)
    if (e.key === '?' && !e.metaKey && !e.ctrlKey && !_isInputFocused()) {
      e.preventDefault();
      _toggleShortcutHelp();
      return;
    }
    const hasShortcutModifier = e.metaKey || e.ctrlKey || e.altKey;
    if (hasShortcutModifier) return;
    // Don't intercept when typing in inputs or palette is open
    if (_isInputFocused() || cmdkOpen || _addJobModalOpen) return;
    // Close shortcut help on any key
    if (_shortcutHelpOpen && e.key !== '?') { _closeShortcutHelp(); }

    const view = _activeView();
    const inDetail = view === 'job';
    const job = inDetail && currentJobId ? (window.jobs[currentJobId] || {}) : null;
    const isLockedSubmission = job && job.submission_lock_state === 'locked';

    // G + second key (vim-style navigation)
    if (_pendingG) {
      _pendingG = false;
      clearTimeout(_pendingGTimer);
      if (e.key === 'q') { location.hash = '#queue'; return; }
      if (e.key === 'a') { location.hash = '#add'; return; }
      if (e.key === 'i') { location.hash = '#discover'; return; }
      if (e.key === 'd') { location.hash = '#dashboard'; return; }
      if (e.key === 's') { location.hash = '#stats'; return; }
      if (e.key === 'e') { location.hash = '#settings'; return; }
      if (e.key === 'u') { location.hash = '#urls'; return; }
      return;
    }
    if (e.key === 'g' && !e.metaKey && !e.ctrlKey) {
      _pendingG = true;
      clearTimeout(_pendingGTimer);
      _pendingGTimer = setTimeout(() => { _pendingG = false; }, 500);
      return;
    }

    // C — add jobs popup (like Linear's C for create issue)
    if (e.key === 'c' && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      openAddJobModal();
      return;
    }

    // Queue view
    if (view === 'queue') {
      if (e.key === '/') { e.preventDefault(); document.querySelector('.search-input')?.focus(); return; }
      if (e.key === 'j' || e.key === 'ArrowDown') { _moveQueueSelection(1); return; }
      if (e.key === 'k' || e.key === 'ArrowUp') { _moveQueueSelection(-1); return; }
      if (e.key === 'Enter' && e.shiftKey) { _openSelectedQueueRow(true); return; }
      if (e.key === 'Enter') { _openSelectedQueueRow(); return; }
      if (e.key === 'x') { _toggleSelectedQueueRow(); return; }
      if (e.key === 'a' && selectedIds.size > 0 && !selectedJobsContainLockedSubmission()) { bulkApprove(createActionContext('queue', 'shortcut')); return; }
      if (e.key === 'e') { _cycleStatusFilter(); return; }
      if (e.key === 'o') { _cycleSortColumn(); return; }
      return;
    }

    // Discover view
    if (view === 'discover') {
      if (e.key === '/') { e.preventDefault(); document.getElementById('discover-keywords')?.focus(); return; }
      if (e.key === 'j' || e.key === 'ArrowDown') { _moveDiscoverSelection(1); return; }
      if (e.key === 'k' || e.key === 'ArrowUp') { _moveDiscoverSelection(-1); return; }
      if (e.key === 'x') { _toggleDiscoverSelection(); return; }
      return;
    }

    // Job detail
    if (inDetail && job) {
      const tabNum = parseInt(e.key);
      if (tabNum >= 1 && tabNum <= 8) {
        const tabs = ['answers', 'resume', 'cover-letter', 'screenshot', 'confirmation', 'logs', 'timeline', 'interview-prep'];
        if (tabs[tabNum - 1]) switchTab(tabs[tabNum - 1], null, true);
        return;
      }
      if (e.key === 'Backspace') { location.hash = '#queue'; return; }
      // Job actions
      if (!isLockedSubmission && e.key === 'a' && job.status === 'draft') { approveJob(job.id, createActionContext('detail', 'shortcut')); return; }
      if (!isLockedSubmission && e.key === 'r' && !e.shiftKey) { restartPipeline(job.id, false, createActionContext('detail', 'shortcut')); return; }
      if (!isLockedSubmission && e.key === 'R' && e.shiftKey) { restartPipeline(job.id, true, createActionContext('detail', 'shortcut')); return; }
      if (e.key === 's') { stopJob(job.id, createActionContext('detail', 'shortcut')); return; }
      if (e.key === 'd') { deleteJob(job.id, createActionContext('detail', 'shortcut')); return; }
      if (e.key === 'e') { !job.archived ? archiveJob(job.id, createActionContext('detail', 'shortcut')) : unarchiveJob(job.id, createActionContext('detail', 'shortcut')); return; }
      // Regenerate actions
      if (!isLockedSubmission && e.key === 'y') { regenerateAsset(job.id, 'answers'); return; }
      if (!isLockedSubmission && e.key === 'u') { regenerateAsset(job.id, 'resume'); return; }
      if (!isLockedSubmission && e.key === 'i') { regenerateAsset(job.id, 'cover_letter'); return; }
      if (e.key === 'p') { openPrepModal(); return; }
      // External links
      if (e.key === 'b') { if (job.url) window.open(job.url, '_blank'); return; }
      // Navigate between jobs
      if (e.key === ']' || (e.key === 'n' && !e.shiftKey)) { _navigateJob(1); return; }
      if (e.key === '[' || (e.key === 'N' && e.shiftKey)) { _navigateJob(-1); return; }
      // Focus browser (captcha)
      if (e.key === 'f') { _focusBrowser(job.id); return; }
    }
  });
}

// ── Queue keyboard navigation ─────────────────────────────────────

let _queueSelectedIndex = -1;

function _getQueueRows() {
  return Array.from(document.querySelectorAll('#job-tbody tr[data-id]'));
}

function _moveQueueSelection(dir) {
  const rows = _getQueueRows();
  if (!rows.length) return;
  rows.forEach(r => r.classList.remove('queue-row-focused'));
  _queueSelectedIndex = Math.max(0, Math.min(rows.length - 1, _queueSelectedIndex + dir));
  const row = rows[_queueSelectedIndex];
  row.classList.add('queue-row-focused');
  row.scrollIntoView({ block: 'nearest' });
}

function _openSelectedQueueRow(modal = false) {
  const rows = _getQueueRows();
  if (_queueSelectedIndex >= 0 && _queueSelectedIndex < rows.length) {
    const id = rows[_queueSelectedIndex].dataset.id;
    if (id) location.hash = modal ? '#job-modal/' + id : '#job/' + id;
  }
}

function _toggleSelectedQueueRow() {
  const rows = _getQueueRows();
  if (_queueSelectedIndex >= 0 && _queueSelectedIndex < rows.length) {
    const id = parseInt(rows[_queueSelectedIndex].dataset.id);
    if (id) {
      if (selectedIds.has(id)) selectedIds.delete(id); else selectedIds.add(id);
      updateBulkActionsBar();
      renderQueue();
    }
  }
}

// ── Queue filter/sort cycling ─────────────────────────────────────────────────

const _statusFilters = ['', 'queued', 'processing', 'draft', 'submitted', 'stopped', 'archived'];
function _cycleStatusFilter() {
  const current = (typeof statusFilter !== 'undefined' ? statusFilter : '') || '';
  const idx = _statusFilters.indexOf(current);
  const next = _statusFilters[(idx + 1) % _statusFilters.length];
  setStatusFilter(next);
  showToast('Filter: ' + (next || 'All'), 'info');
}

const _sortColumns = QUEUE_SORT_OPTIONS.map(option => option.value);
let _sortCycleIndex = Math.max(0, _sortColumns.indexOf(sortField));
function _cycleSortColumn() {
  _sortCycleIndex = (_sortCycleIndex + 1) % _sortColumns.length;
  setQueueSortField(_sortColumns[_sortCycleIndex]);
}

// ── Job navigation (next/previous) ────────────────────────────────────────────

function _navigateJob(dir) {
  const rows = _getQueueRows();
  const modal = _currentJobRouteIsModal();
  if (!rows.length) {
    // Fall back to all known jobs sorted by id
    const ids = Object.keys(window.jobs || {}).map(Number).sort((a, b) => a - b);
    const idx = ids.indexOf(currentJobId);
    if (idx < 0) return;
    const next = ids[idx + dir];
    if (next != null) location.hash = modal ? '#job-modal/' + next : '#job/' + next;
    return;
  }
  const ids = rows.map(r => parseInt(r.dataset.id));
  const idx = ids.indexOf(currentJobId);
  if (idx < 0) return;
  const next = ids[idx + dir];
  if (next != null) location.hash = modal ? '#job-modal/' + next : '#job/' + next;
}

// ── Focus browser (captcha) ───────────────────────────────────────────────────

function _focusBrowser(jobId) { focusCaptchaBrowser(jobId); }

// ── Discover keyboard navigation ──────────────────────────────────────────────

let _discoverSelectedIndex = -1;

function _moveDiscoverSelection(dir) {
  const rows = Array.from(document.querySelectorAll('#discover-results tr[data-id], #discover-results .discover-card'));
  if (!rows.length) return;
  rows.forEach(r => r.classList.remove('queue-row-focused'));
  _discoverSelectedIndex = Math.max(0, Math.min(rows.length - 1, _discoverSelectedIndex + dir));
  const row = rows[_discoverSelectedIndex];
  row.classList.add('queue-row-focused');
  row.scrollIntoView({ block: 'nearest' });
}

function _toggleDiscoverSelection() {
  const rows = Array.from(document.querySelectorAll('#discover-results tr[data-id], #discover-results .discover-card'));
  if (_discoverSelectedIndex >= 0 && _discoverSelectedIndex < rows.length) {
    const cb = rows[_discoverSelectedIndex].querySelector('input[type="checkbox"]');
    if (cb) { cb.click(); }
  }
}

// ── Shortcut help overlay ─────────────────────────────────────────────────────

let _shortcutHelpOpen = false;

function _toggleShortcutHelp() {
  if (_shortcutHelpOpen) _closeShortcutHelp();
  else _openShortcutHelp();
}

function _openShortcutHelp() {
  _shortcutHelpOpen = true;
  const existing = document.getElementById('shortcut-help');
  if (existing) { existing.style.display = 'flex'; return; }

  const view = _activeView();
  const backdrop = document.createElement('div');
  backdrop.id = 'shortcut-help';
  backdrop.className = 'shortcut-help-backdrop';
  backdrop.onclick = _closeShortcutHelp;

  const panel = document.createElement('div');
  panel.className = 'shortcut-help-panel';
  panel.onclick = e => e.stopPropagation();

  const globalShortcuts = [
    ['?', 'Show this help'],
    ['\u2318K', 'Command palette'],
    ['C', 'Add jobs'],
    ['Esc', 'Close modal/overlay'],
  ];

  const navShortcuts = [
    ['G Q', 'Queue'],
    ['G A', 'Add Jobs'],
    ['G I', 'Discover'],
    ['G D', 'Dashboard'],
    ['G S', 'Stats'],
    ['G E', 'Settings'],
    ['G U', 'URLs'],
  ];

  const queueShortcuts = [
    ['J / \u2193', 'Move down'],
    ['K / \u2191', 'Move up'],
    ['Enter', 'Open job'],
    ['Shift+Enter', 'Open job as modal'],
    ['X', 'Toggle selection'],
    ['/', 'Focus search'],
    ['E', 'Cycle status filter'],
    ['O', 'Cycle sort column'],
    ['A', 'Approve selected (if any)'],
  ];

  const jobShortcuts = [
    ['1-8', 'Switch tabs'],
    ['A', 'Approve + Submit'],
    ['R', 'Restart \u2192 Draft'],
    ['Shift+R', 'Restart \u2192 Submit'],
    ['S', 'Stop job'],
    ['D', 'Delete job'],
    ['E', 'Archive / Unarchive'],
    ['Y', 'Regenerate Answers'],
    ['U', 'Regenerate Resume'],
    ['I', 'Regenerate Cover Letter'],
    ['P', 'Interview Prep'],
    ['B', 'Open Job Board'],
    ['F', 'Focus Browser (captcha)'],
    ['N / ]', 'Next job'],
    ['Shift+N / [', 'Previous job'],
    ['\u232B', 'Back to Queue'],
  ];

  const sections = [
    ['Global', globalShortcuts],
    ['Navigation', navShortcuts],
  ];

  if (view === 'queue') sections.push(['Queue', queueShortcuts]);
  else if (view === 'job') sections.push(['Job Detail', jobShortcuts]);
  else if (view === 'discover') sections.push(['Discover', [['J/K', 'Navigate'], ['X', 'Toggle'], ['/', 'Search']]]);

  // Always show all sections but highlight contextual ones
  if (view !== 'queue') sections.push(['Queue', queueShortcuts]);
  if (view !== 'job') sections.push(['Job Detail', jobShortcuts]);

  let html = '<h2>Keyboard Shortcuts</h2><div class="shortcut-columns">';
  sections.forEach(([title, shortcuts]) => {
    html += `<div class="shortcut-section"><h3>${title}</h3>`;
    shortcuts.forEach(([keys, desc]) => {
      html += `<div class="shortcut-row"><span class="shortcut-keys">${keys.split(' ').map(k => `<kbd>${k}</kbd>`).join(' ')}</span><span class="shortcut-desc">${desc}</span></div>`;
    });
    html += '</div>';
  });
  html += '</div><p class="shortcut-footer">Press <kbd>?</kbd> or <kbd>Esc</kbd> to close</p>';

  panel.innerHTML = html;
  backdrop.appendChild(panel);
  document.body.appendChild(backdrop);
}

function _closeShortcutHelp() {
  _shortcutHelpOpen = false;
  const el = document.getElementById('shortcut-help');
  if (el) el.remove();
}

// ── Discover Page ─────────────────────────────────────────────────────────────

let _discoverBound = false;
let _discoverFilter = '';
let _discoverSearchFilter = '';
let _discoverFilterTimer = null;
let _discoverSelectedIds = new Set();
let _discoverCandidates = [];

function renderDiscover() {
  fetchCandidates().then(() => {
    if (!_discoverBound) {
      bindDiscoverEvents();
      _discoverBound = true;
    }
  });
}

async function fetchCandidates() {
  const params = new URLSearchParams();
  if (_discoverFilter) params.set('status', _discoverFilter);
  if (_discoverSearchFilter) params.set('search', _discoverSearchFilter);
  params.set('limit', '200');
  try {
    const data = await apiCall('GET', '/api/discover/candidates?' + params.toString());
    _discoverCandidates = data.candidates || [];
    renderCandidateTable(_discoverCandidates);
    updateDiscoverFilterCounts(data.stats || {});
  } catch (e) {
    showToast('Failed to load candidates: ' + e.message, 'error');
  }
}

function updateDiscoverFilterCounts(stats) {
  const total = Object.values(stats).reduce((a, b) => a + b, 0);
  const el = id => document.getElementById(id);
  if (el('dcount-all')) el('dcount-all').textContent = total;
  if (el('dcount-new')) el('dcount-new').textContent = stats.new || 0;
  if (el('dcount-scored')) el('dcount-scored').textContent = stats.scored || 0;
  if (el('dcount-promoted')) el('dcount-promoted').textContent = stats.promoted || 0;
  if (el('dcount-skipped')) el('dcount-skipped').textContent = stats.skipped || 0;
}

function renderCandidateTable(candidates) {
  const tbody = document.getElementById('discover-tbody');
  if (!tbody) return;

  if (!candidates || !candidates.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="9">No candidates found. Try searching above.</td></tr>';
    return;
  }

  tbody.innerHTML = candidates.map(c => {
    const isSkipped = c.status === 'skipped';
    const rowClass = isSkipped ? 'row-dimmed' : '';
    const checked = _discoverSelectedIds.has(c.id) ? 'checked' : '';

    // Score badge
    let scoreBadge = '<span class="score-badge score-none">—</span>';
    if (c.score !== null && c.score !== undefined) {
      const s = parseInt(c.score);
      const cls = s >= 75 ? 'score-high' : s >= 50 ? 'score-mid' : 'score-low';
      scoreBadge = '<span class="score-badge ' + cls + '" title="' + escapeHtml(c.score_reason || '') + '">' + s + '</span>';
    }

    // Source badge
    const srcCls = 'source-' + (c.source || 'unknown').toLowerCase();
    const sourceBadge = '<span class="score-badge ' + srcCls + '">' + escapeHtml(c.source || '?') + '</span>';

    // Date posted
    const posted = c.date_posted ? c.date_posted.split('T')[0] : '';

    // Actions
    let actions = '';
    if (c.status === 'promoted') {
      actions = '<span style="color:var(--green);font-size:11px">Drafted #' + (c.promoted_job_id || '') + '</span>';
    } else if (isSkipped) {
      actions = '<button class="btn btn-xs btn-outline" onclick="unskipCandidate(' + c.id + ')">Unskip</button>';
    } else {
      actions = '<button class="btn btn-xs btn-success" onclick="draftCandidate(' + c.id + ')">Draft</button> ' +
        '<button class="btn btn-xs btn-outline" onclick="skipCandidate(' + c.id + ')">Skip</button>';
    }

    const jobLink = c.job_url
      ? '<a href="' + escapeHtml(c.job_url) + '" target="_blank" rel="noopener">' + escapeHtml(c.company) + '</a>'
      : escapeHtml(c.company || '');

    // Duplicate badge
    let dupBadge = '';
    if (c.duplicate_of) {
      const d = c.duplicate_of;
      const dupStatus = d.status || '';
      const dupLabel = dupStatus === 'submitted' ? 'Applied' : dupStatus === 'draft' ? 'Drafted' : 'In Queue';
      dupBadge = ' <a href="#job/' + d.job_id + '" class="dup-badge" title="' +
        escapeHtml(d.role_title || '') + ' (' + dupLabel + ')">' + dupLabel + ' #' + d.job_id + '</a>';
    }

    return '<tr class="' + rowClass + (c.duplicate_of ? ' row-dup' : '') + '" data-cid="' + c.id + '">' +
      '<td class="col-check"><input type="checkbox" ' + checked + ' onchange="discoverCheckChange(' + c.id + ', this.checked)"></td>' +
      '<td>' + scoreBadge + '</td>' +
      '<td>' + jobLink + dupBadge + '</td>' +
      '<td>' + escapeHtml(c.title || '') + '</td>' +
      '<td>' + escapeHtml(c.location || '') + '</td>' +
      '<td style="white-space:nowrap;font-size:11px">' + escapeHtml(c.salary || '') + '</td>' +
      '<td>' + sourceBadge + '</td>' +
      '<td style="font-size:11px">' + escapeHtml(posted) + '</td>' +
      '<td style="white-space:nowrap">' + actions + '</td>' +
      '</tr>';
  }).join('');
}

function bindDiscoverEvents() {
  // Search button
  const searchBtn = document.getElementById('discover-search-btn');
  if (searchBtn) {
    searchBtn.addEventListener('click', discoverSearch);
  }

  // Enter key on keyword/location inputs
  ['discover-keywords', 'discover-location'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('keydown', e => { if (e.key === 'Enter') discoverSearch(); });
  });

  // Select-all checkbox
  const selectAll = document.getElementById('discover-select-all');
  if (selectAll) selectAll.addEventListener('change', discoverToggleSelectAll);
}

function setDiscoverFilter(status) {
  _discoverFilter = status;
  document.querySelectorAll('#discover-filter-bar .badge-pill').forEach(p => {
    p.classList.toggle('active', (p.dataset.dstatus || '') === status);
  });
  fetchCandidates();
}

function discoverSearchFilter() {
  clearTimeout(_discoverFilterTimer);
  _discoverFilterTimer = setTimeout(() => {
    _discoverSearchFilter = (document.getElementById('discover-search-filter')?.value || '').trim();
    fetchCandidates();
  }, 250);
}

async function discoverSearch() {
  const keywords = (document.getElementById('discover-keywords')?.value || '').trim();
  if (!keywords) { showToast('Enter keywords to search', 'error'); return; }
  const location = (document.getElementById('discover-location')?.value || '').trim() || 'San Francisco, CA';
  const sources = Array.from(document.querySelectorAll('#discover-sources option:checked')).map(o => o.value);
  const btn = document.getElementById('discover-search-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Searching…'; }
  try {
    const data = await apiCall('POST', '/api/discover/search', {
      search_term: keywords,
      location,
      sources: sources.length ? sources : null,
      results_wanted: 50,
    });
    showToast('Found ' + (data.inserted || 0) + ' new jobs — scoring in background', 'success');
    await fetchCandidates();
    // Poll for scores at 2s, 8s, 20s
    [2000, 8000, 20000].forEach(delay => {
      setTimeout(() => fetchCandidates(), delay);
    });
  } catch (e) {
    showToast('Search failed: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Search'; }
  }
}

function discoverCheckChange(id, checked) {
  if (checked) _discoverSelectedIds.add(id);
  else _discoverSelectedIds.delete(id);
  updateDiscoverBulkBar();
}

function discoverToggleSelectAll() {
  const cb = document.getElementById('discover-select-all');
  if (!cb) return;
  if (cb.checked) {
    _discoverCandidates.forEach(c => _discoverSelectedIds.add(c.id));
  } else {
    _discoverSelectedIds.clear();
  }
  // Re-render to update row checkboxes
  renderCandidateTable(_discoverCandidates);
  updateDiscoverBulkBar();
}

function getSelectedCandidateIds() {
  return Array.from(_discoverSelectedIds);
}

function updateDiscoverBulkBar() {
  const bar = document.getElementById('discover-bulk-bar');
  const countEl = document.getElementById('discover-selected-count');
  const n = _discoverSelectedIds.size;
  if (!bar) return;
  if (n > 0) {
    bar.style.display = 'flex';
    if (countEl) countEl.textContent = n + ' selected';
  } else {
    bar.style.display = 'none';
  }
}

async function draftCandidate(id) {
  try {
    const data = await apiCall('POST', '/api/discover/candidates/' + id + '/promote');
    showToast('Drafted as job #' + data.job_id, 'success');
    await fetchCandidates();
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

async function skipCandidate(id) {
  try {
    await apiCall('POST', '/api/discover/candidates/' + id + '/skip');
    await fetchCandidates();
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

async function unskipCandidate(id) {
  try {
    await apiCall('POST', '/api/discover/candidates/' + id + '/unskip');
    await fetchCandidates();
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

async function discoverBulkDraft() {
  const ids = getSelectedCandidateIds();
  if (!ids.length) return;
  try {
    const data = await apiCall('POST', '/api/discover/candidates/promote-bulk', { ids });
    showToast('Drafted ' + (data.promoted || []).length + ' jobs', 'success');
    _discoverSelectedIds.clear();
    updateDiscoverBulkBar();
    await fetchCandidates();
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

async function discoverBulkSkip() {
  const ids = getSelectedCandidateIds();
  if (!ids.length) return;
  try {
    await Promise.all(ids.map(id => apiCall('POST', '/api/discover/candidates/' + id + '/skip')));
    showToast('Skipped ' + ids.length + ' candidates', 'info');
    _discoverSelectedIds.clear();
    updateDiscoverBulkBar();
    await fetchCandidates();
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

async function discoverScoreAll() {
  const btn = document.getElementById('discover-score-all-btn');
  if (!btn) return;
  btn.disabled = true; btn.textContent = 'Scoring...';
  try {
    const data = await apiCall('POST', '/api/discover/score-all');
    if (data.unscored === 0) {
      showToast('All candidates already scored', 'info');
    } else {
      showToast('Scoring ' + data.unscored + ' candidates in background. Refresh to see updates.', 'success');
      const poll = setInterval(async () => { await fetchCandidates(); }, 15000);
      setTimeout(() => clearInterval(poll), 600000);
    }
  } catch (e) {
    showToast('Scoring failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Score Unscored';
  }
}

// ── Interview Prep tab ──────────────────────────────────────
function loadInterviewPrepTab(jobId) {
  const container = document.getElementById('interview-prep-content');
  container.innerHTML = '<div class="loading-msg">Loading...</div>';

  fetch(`/api/jobs/${jobId}/interview-prep`)
    .then(r => r.json())
    .then(data => {
      if (data.exists) {
        let html = '<div class="prep-toolbar">';
        html += `<a href="${data.docx_download}" class="btn btn-outline btn-sm" download>Download .docx</a> `;
        html += `<a href="${data.pdf_download}" class="btn btn-outline btn-sm" download>Download .pdf</a> `;
        html += '<button class="btn btn-outline btn-sm" onclick="openPrepModal()">Regenerate</button>';
        html += '</div>';
        html += '<div class="prep-markdown">' + renderMarkdown(data.markdown) + '</div>';
        container.innerHTML = html;
      } else if (data.generating) {
        const detail = data.progress ? data.progress.detail : 'Generating...';
        container.innerHTML = `<div class="prep-generating"><div class="spinner-sm"></div> ${escapeHtml(detail)}</div>`;
        setTimeout(() => {
          if (currentTab === 'interview-prep' && currentJobId == jobId) {
            loadInterviewPrepTab(jobId);
          }
        }, 3000);
      } else {
        container.innerHTML = `
          <div class="prep-empty">
            <p>No interview prep guide generated yet.</p>
            <button class="btn btn-primary" onclick="openPrepModal()">Generate Interview Prep</button>
          </div>`;
      }
    })
    .catch(err => {
      container.innerHTML = `<div class="error-msg">Failed to load: ${escapeHtml(err.message)}</div>`;
    });
}

function renderMarkdown(md) {
  let html = md
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/^#### (.+)$/gm, '<h4>$1</h4>')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/```[\s\S]*?```/g, m => '<pre><code>' + m.slice(3, -3).replace(/^\w+\n/, '') + '</code></pre>')
    .replace(/`(.+?)`/g, '<code>$1</code>')
    .replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>')
    .replace(/^[-*] (.+)$/gm, '<li>$1</li>')
    .replace(/^---$/gm, '<hr>')
    .replace(/\n\n/g, '</p><p>')
    .replace(/\n/g, '<br>');
  html = html.replace(/((?:<li>.*?<\/li>\s*)+)/g, '<ul>$1</ul>');
  return '<div class="md-body"><p>' + html + '</p></div>';
}

function openPrepModal() {
  document.getElementById('prep-backdrop').style.display = 'block';
  document.getElementById('prep-modal').style.display = 'flex';
  document.body.classList.add('modal-open');
}

function closePrepModal() {
  document.getElementById('prep-backdrop').style.display = 'none';
  document.getElementById('prep-modal').style.display = 'none';
  document.body.classList.remove('modal-open');
}

function submitPrepModal() {
  const stage = document.getElementById('prep-stage').value;
  const interviewers = document.getElementById('prep-interviewers').value;
  const notes = document.getElementById('prep-notes').value;
  const btn = document.getElementById('prep-generate-btn');

  btn.disabled = true;
  btn.textContent = 'Starting...';

  fetch(`/api/jobs/${currentJobId}/interview-prep`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({stage, interviewers, notes}),
  })
    .then(r => {
      if (r.status === 409) throw new Error('Generation already in progress');
      if (!r.ok) throw new Error('Failed to start generation');
      return r.json();
    })
    .then(() => {
      closePrepModal();
      showToast('Interview prep generation started — this takes 5-15 minutes.');
      switchTab('interview-prep', null, true);
    })
    .catch(err => {
      showToast(err.message, 'error');
    })
    .finally(() => {
      btn.disabled = false;
      btn.textContent = 'Generate';
    });
}

// ── Dedup ─────────────────────────────────────────────────────────────

async function runDedup() {
  const btn = document.getElementById('dedup-btn');
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  try {
    const data = await apiCall('POST', '/api/jobs/dedup');
    const { fingerprints_added, fingerprints_skipped, duplicate_groups } = data;
    const body = document.getElementById('dedup-body');

    if (!duplicate_groups || duplicate_groups.length === 0) {
      body.innerHTML = `<p>No duplicate jobs found.</p>
        <p class="dedup-stats">${fingerprints_added} fingerprints computed, ${fingerprints_skipped} skipped (no JD text).</p>`;
    } else {
      let html = `<p class="dedup-stats">${duplicate_groups.length} duplicate group${duplicate_groups.length > 1 ? 's' : ''} found.
        ${fingerprints_added} new fingerprints computed.</p>`;
      duplicate_groups.forEach((group, i) => {
        html += `<div class="dedup-group"><h3>Group ${i + 1} (<span class="dedup-count">${group.length}</span> jobs) <button class="btn btn-sm btn-outline" style="margin-left:12px;font-size:12px" onclick="dedupKeepOldest(this.closest('.dedup-group'))">Keep Oldest</button></h3><table class="dedup-table"><thead><tr><th>ID</th><th>Company</th><th>Role</th><th>Status</th><th>Created</th><th></th><th></th></tr></thead><tbody>`;
        group.forEach(job => {
          const created = job.created_at ? new Date(job.created_at).toLocaleDateString() : '';
          const isActive = PROCESSING_STATUSES.has(job.status);
          html += `<tr data-job-id="${job.id}">
            <td>#${job.id}</td>
            <td>${escapeHtml(job.company || '')}</td>
            <td>${escapeHtml(job.role_title || '')}</td>
            <td><span class="status-badge ${statusClass(job.status)}">${statusLabel(job.status)}</span></td>
            <td>${created}</td>
            <td><a href="#job/${job.id}" onclick="closeDedupModal()" class="btn btn-sm btn-outline">View</a></td>
            <td><button class="btn btn-sm btn-outline btn-delete" onclick="dedupDeleteJob(${job.id}, this)"${isActive ? ' disabled title="Job is actively processing"' : ''}>Delete</button></td>
          </tr>`;
        });
        html += '</tbody></table></div>';
      });
      body.innerHTML = html;
    }
    document.getElementById('dedup-backdrop').style.display = 'block';
    document.getElementById('dedup-modal').style.display = 'flex';
    document.body.classList.add('modal-open');
  } catch (err) {
    showToast('Dedup failed: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Find Duplicates';
  }
}

function closeDedupModal() {
  document.getElementById('dedup-backdrop').style.display = 'none';
  document.getElementById('dedup-modal').style.display = 'none';
  document.body.classList.remove('modal-open');
}

function dedupCleanupGroup(groupEl) {
  if (!groupEl.isConnected) return;
  const remaining = groupEl.querySelectorAll('tbody tr');
  if (remaining.length <= 1) {
    groupEl.remove();
  } else {
    const countEl = groupEl.querySelector('.dedup-count');
    if (countEl) countEl.textContent = remaining.length;
  }
  const body = document.getElementById('dedup-body');
  if (body && !body.querySelector('.dedup-group')) {
    body.innerHTML = '<p>All duplicates resolved.</p>';
  }
}

async function dedupDeleteJob(jobId, btn) {
  const job = window.jobs[jobId];
  if (job && PROCESSING_STATUSES.has(job.status)) {
    showToast(`Job #${jobId} is now ${job.status} — cannot delete`, 'warning');
    btn.disabled = true;
    btn.title = 'Job is actively processing';
    return;
  }
  const group = btn.closest('.dedup-group');
  if (group._dedupBusy) {
    showToast('Bulk operation in progress — please wait', 'warning');
    return;
  }
  const label = job ? `#${jobId} (${job.company} — ${job.role_title}, ${job.status})` : `#${jobId}`;
  if (!confirm(`Delete ${label}? This cannot be undone.`)) return;
  try {
    await apiCall('DELETE', `/api/jobs/${jobId}`, null, createActionContext('dedup', 'button'));
    showToast(`Deleted #${jobId}`, 'info');
    delete window.jobs[jobId];
    btn.closest('tr').remove();
    dedupCleanupGroup(group);
  } catch (e) {
    showToast('Delete failed: ' + e.message, 'error');
  }
}

async function dedupKeepOldest(groupEl) {
  const rows = [...groupEl.querySelectorAll('tbody tr')];
  const keepRow = rows[0];
  const deleteRows = rows.slice(1);
  const toDelete = [];
  const skipped = [];
  for (const row of deleteRows) {
    const jobId = parseInt(row.dataset.jobId);
    const job = window.jobs[jobId];
    if (job && PROCESSING_STATUSES.has(job.status)) {
      skipped.push(`#${jobId} (${job.status})`);
    } else {
      toDelete.push({ id: jobId, row, label: job ? `#${jobId} ${job.company}` : `#${jobId}` });
    }
  }
  if (toDelete.length === 0) {
    showToast('No deletable jobs — all are actively processing', 'warning');
    return;
  }
  const keepId = parseInt(keepRow.dataset.jobId);
  const msg = `Keep #${keepId} and delete ${toDelete.length} job(s):\n${toDelete.map(j => j.label).join('\n')}` +
    (skipped.length ? `\n\nSkipping ${skipped.length} active: ${skipped.join(', ')}` : '') +
    '\n\nThis cannot be undone.';
  if (!confirm(msg)) return;
  const buttons = groupEl.querySelectorAll('button');
  buttons.forEach(b => b.disabled = true);
  groupEl._dedupBusy = true;
  let ok = 0, fail = 0;
  try {
    for (const { id, row } of toDelete) {
      try {
        await apiCall('DELETE', `/api/jobs/${id}`, null, createActionContext('dedup', 'bulk'));
        delete window.jobs[id];
        row.remove();
        ok++;
      } catch (_) { fail++; }
    }
    showToast(ok + ' jobs deleted' + (fail ? ', ' + fail + ' failed' : ''), ok ? 'info' : 'error');
  } finally {
    groupEl._dedupBusy = false;
    dedupCleanupGroup(groupEl);
  }
}

// ── Saved Portal Imports ─────────────────────────────────────────────────────

const SAVED_PORTAL_UI = {
  linkedin: { label: 'LinkedIn', buttonId: 'linkedin-import-btn', addButtonId: 'add-linkedin-import-btn' },
  trueup: { label: 'TrueUp', buttonId: 'trueup-import-btn', addButtonId: 'add-trueup-import-btn' },
  jackandjill: { label: 'Jack & Jill', buttonId: 'jackandjill-import-btn', addButtonId: 'add-jackandjill-import-btn' },
};
const SAVED_PORTAL_IMPORT_PENDING_COPY = 'this may take several minutes for large saved lists';

function _savedPortalConfig(portal) {
  const config = SAVED_PORTAL_UI[portal];
  if (!config) {
    throw new Error(`Unknown saved portal UI config: ${portal}`);
  }
  return config;
}

function _savedPortalLabel(portal) {
  return _savedPortalConfig(portal).label;
}

function _formatSavedPortalResult(label, result) {
  const status = result?.status || 'unknown';
  const scraped = result?.scraped ?? 0;
  const resolved = result?.resolved ?? 0;
  const added = result?.added ?? 0;
  const duplicates = result?.duplicates ?? 0;
  const unresolved = result?.skipped_unresolved ?? 0;
  const errors = result?.errors ?? 0;
  const msg = result?.message ? String(result.message).trim() : '';

  let line = `${label} import: ${added} added`;
  if (duplicates) line += `, ${duplicates} duplicate${duplicates === 1 ? '' : 's'}`;
  if (unresolved) line += `, ${unresolved} unresolved`;
  if (errors) line += `, ${errors} error${errors === 1 ? '' : 's'}`;
  line += ` (${scraped} scraped, ${resolved} resolved, status=${status})`;
  if (msg) line += `; ${msg}`;
  return line;
}

async function openSavedPortalAuthSetup(portal) {
  const label = _savedPortalLabel(portal);
  try {
    const result = await apiCall('POST', `/api/jobs/import/${encodeURIComponent(portal)}/auth`);
    showToast(result?.message || `Opened ${label} sign-in window. Sign in, close the browser, then retry import.`, 'info');
    return { ok: true, result };
  } catch (err) {
    showToast(`${label} sign-in setup failed: ${err.message}`, 'error');
    return { ok: false, error: err };
  }
}

async function _offerSavedPortalAuthSetup(portal) {
  const label = _savedPortalLabel(portal);
  if (!confirm(`${label} requires sign-in in its dedicated browser profile. Open the ${label} sign-in window now?`)) {
    return { ok: false, cancelled: true };
  }
  return openSavedPortalAuthSetup(portal);
}

async function _importSavedPortal(portal, { btn, provider, priority, toastPrefix }) {
  const label = _savedPortalLabel(portal);
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Importing...';
  showToast(`${label} import running — ${SAVED_PORTAL_IMPORT_PENDING_COPY}`, 'info');
  try {
    const result = await apiCall('POST', `/api/jobs/import/${encodeURIComponent(portal)}`, {
      provider: provider || null,
      priority: priority || 0,
    });

    const summary = _formatSavedPortalResult(label, result);
    const toastKind = result?.status === 'ok' ? 'success' : (result?.status === 'auth_required' ? 'warning' : 'error');
    showToast(`${toastPrefix}${summary}`, toastKind);
    if (result?.status === 'auth_required') {
      await _offerSavedPortalAuthSetup(portal);
    }
    return { ok: true, result };
  } catch (err) {
    showToast(`${label} import failed: ${err.message}`, 'error');
    return { ok: false, error: err };
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function importSavedPortalFromQueue(portal) {
  const btnId = _savedPortalConfig(portal).buttonId;
  const btn = document.getElementById(btnId);
  if (!btn) {
    throw new Error(`Missing saved portal queue button: ${btnId}`);
  }
  await _importSavedPortal(portal, { btn, provider: null, priority: 0, toastPrefix: '' });
}

async function importSavedPortalFromAddView(portal) {
  const btnId = _savedPortalConfig(portal).addButtonId;
  const btn = document.getElementById(btnId);
  if (!btn) {
    throw new Error(`Missing saved portal add-view button: ${btnId}`);
  }

  const provSel = document.getElementById('provider-select');
  const priSel = document.getElementById('priority-select');
  const provider = provSel ? (provSel.value || null) : null;
  const priority = priSel ? (parseInt(priSel.value, 10) || 0) : 0;

  const feedback = document.getElementById('add-feedback');
  if (feedback) {
    feedback.className = 'add-feedback warning';
    feedback.textContent = `Importing ${_savedPortalLabel(portal)} saved jobs... ${SAVED_PORTAL_IMPORT_PENDING_COPY}.`;
    feedback.style.display = 'block';
  }
  const { ok, result, error } = await _importSavedPortal(portal, { btn, provider, priority, toastPrefix: '' });
  if (ok && feedback && result) {
    const feedbackKind = result.status === 'ok' ? 'success' : (result.status === 'auth_required' ? 'warning' : 'error');
    feedback.className = `add-feedback ${feedbackKind}`;
    feedback.textContent = _formatSavedPortalResult(_savedPortalLabel(portal), result);
    feedback.style.display = 'block';
  } else if (feedback) {
    feedback.className = 'add-feedback error';
    feedback.textContent = `Error: ${ok ? 'unknown saved-portal failure' : error?.message || 'request failed'}`;
    feedback.style.display = 'block';
  }
}
