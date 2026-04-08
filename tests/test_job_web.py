import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import saved_portal_import


def test_health_endpoint():
    from fastapi.testclient import TestClient
    from job_web import create_app

    app = create_app()
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


@pytest.fixture
def client():
    """Create a test client with a temp database."""
    import job_web
    from fastapi.testclient import TestClient
    from job_db import init_db

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    original_path = job_web.DB_PATH
    job_web.DB_PATH = Path(db_path)
    job_web._local.conn = None
    # init_db once to create schema (matches the lifespan() pattern)
    conn = init_db(db_path)
    conn.close()
    app = job_web.create_app()
    with TestClient(app, client=("127.0.0.1", 50000)) as test_client:
        yield test_client
    job_web.close_all_connections()
    job_web.DB_PATH = original_path
    job_web._local.conn = None


def _filled_autofill_report_payload(**overrides):
    payload = {
        "fields": [
            {
                "field_name": "candidate_name",
                "label": "Full Name",
                "kind": "text",
                "status": "filled",
                "value": "Candidate Name",
                "source": "application_profile.md",
            }
        ]
    }
    payload.update(overrides)
    return payload


def test_list_jobs_empty(client):
    resp = client.get("/api/jobs")
    assert resp.status_code == 200
    assert resp.json() == []


def test_root_includes_job_detail_dock_shell(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert 'id="job-detail-dock"' in resp.text
    assert 'id="job-detail-helper-stack"' in resp.text
    assert 'id="answer-refresh-proof"' in resp.text
    assert 'class="dock-row dock-row-primary"' in resp.text
    assert 'class="dock-row dock-row-tabs"' in resp.text

    dock_segment, helper_segment = resp.text.split('id="job-detail-helper-stack"', maxsplit=1)
    assert 'id="answer-refresh-proof"' not in dock_segment
    assert 'id="job-detail-dock"' in dock_segment
    assert helper_segment.index('id="progress-wrap"') < helper_segment.index('id="answer-refresh-proof"')
    assert 'id="linkedin-import-btn"' in resp.text
    assert 'id="trueup-import-btn"' in resp.text
    assert 'id="jackandjill-import-btn"' in resp.text
    assert 'id="add-linkedin-import-btn"' in resp.text
    assert 'id="add-trueup-import-btn"' in resp.text
    assert 'id="add-jackandjill-import-btn"' in resp.text
    assert "Saved Portals" in resp.text


def test_root_includes_submission_lock_indicator_shell(client):
    resp = client.get("/")

    assert resp.status_code == 200
    assert 'id="submission-lock-indicator"' in resp.text


def test_root_includes_job_detail_modal_shell(client):
    resp = client.get("/")

    assert resp.status_code == 200
    assert 'id="job-detail-modal-backdrop"' in resp.text


def test_root_declares_inline_favicon_to_avoid_browser_404_noise(client):
    resp = client.get("/")

    assert resp.status_code == 200
    assert '<link rel="icon" href="data:,">' in resp.text


def test_favicon_endpoint_returns_empty_success(client):
    resp = client.get("/favicon.ico")

    assert resp.status_code == 204
    assert resp.content == b""


def test_app_js_does_not_hide_submitted_before_badge_for_submitted_jobs(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "job.previously_submitted && job.status !== 'submitted'" not in resp.text


def test_root_includes_backend_settings_editor_controls(client):
    resp = client.get("/")

    assert resp.status_code == 200
    assert 'id="settings-onboarding-status"' in resp.text
    assert 'id="settings-onboarding-continue-btn"' in resp.text
    assert 'id="settings-master-resume"' in resp.text
    assert 'id="settings-master-resume-import-url"' in resp.text
    assert 'id="settings-master-resume-import-file"' in resp.text
    assert 'id="settings-application-profile"' in resp.text
    assert 'id="settings-default-provider"' in resp.text
    assert 'id="settings-provider-chain"' in resp.text
    assert 'id="settings-openai-api-key"' in resp.text
    assert 'id="settings-save-btn"' in resp.text


def test_app_js_loads_and_saves_server_backed_settings(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "async function bootstrapApp()" in resp.text
    assert "await apiCall('GET', '/api/bootstrap')" in resp.text
    assert "if (!_bootstrapOnboardingComplete()) {" in resp.text
    assert "startRealtimeAppServices();" in resp.text
    assert "async function loadServerSettings(force = false)" in resp.text
    assert "await apiCall('GET', '/api/settings')" in resp.text
    assert "async function importServerMaterial(materialKey, options = {})" in resp.text
    assert "await apiCall('POST', '/api/settings/materials/import', payload)" in resp.text
    assert "async function saveServerSettings()" in resp.text
    assert "await apiCall('POST', '/api/settings', payload)" in resp.text


def test_bootstrap_endpoint_reports_onboarding_state(client):
    with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(
        os.environ, {"JOB_ASSETS_APP_HOME": tmpdir}, clear=False
    ):
        resp = client.get("/api/bootstrap")

        assert resp.status_code == 200
        data = resp.json()
        assert data["onboarding"]["complete"] is False
        assert data["onboarding"]["required_materials"]["master_resume"] is False
        assert data["onboarding"]["credentials_ready"] is False

        Path(tmpdir, "master_resume.md").write_text("# Resume\n", encoding="utf-8")
        Path(tmpdir, ".env.local").write_text('OPENAI_API_KEY="sk-test-12345678"\n', encoding="utf-8")

        ready_resp = client.get("/api/bootstrap")

    assert ready_resp.status_code == 200
    assert ready_resp.json()["onboarding"]["complete"] is True


def test_settings_endpoint_round_trips_materials_and_redacts_credentials(client):
    with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(
        os.environ, {"JOB_ASSETS_APP_HOME": tmpdir}, clear=False
    ):
        resp = client.post(
            "/api/settings",
            json={
                "materials": {
                    "master_resume": "# Resume\n",
                    "application_profile": "email: person@example.com\n",
                },
                "providers": {
                    "default_provider": "openai",
                    "provider_chain": "openai,gemini",
                    "openai_model": "gpt-5.4",
                },
                "credentials": {
                    "openai_api_key": "sk-test-12345678",
                },
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["materials"]["master_resume"]["content"] == "# Resume\n"
        assert data["providers"]["default_provider"] == "openai"
        assert data["credentials"]["openai_api_key"]["configured"] is True
        assert data["credentials"]["openai_api_key"]["preview"] != "sk-test-12345678"

        get_resp = client.get("/api/settings")

    assert get_resp.status_code == 200
    assert get_resp.json()["materials"]["application_profile"]["content"] == "email: person@example.com\n"


def test_material_import_endpoint_accepts_text_and_writes_runtime_material(client):
    with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(
        os.environ, {"JOB_ASSETS_APP_HOME": tmpdir}, clear=False
    ):
        resp = client.post(
            "/api/settings/materials/import",
            json={
                "material_key": "master_resume",
                "text": "# Imported Resume\n",
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["settings"]["materials"]["master_resume"]["content"] == "# Imported Resume\n"
        assert Path(tmpdir, "master_resume.md").read_text(encoding="utf-8") == "# Imported Resume\n"


def test_material_import_endpoint_fetches_public_url_content(client, monkeypatch):
    import web_settings_api

    with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(
        os.environ, {"JOB_ASSETS_APP_HOME": tmpdir}, clear=False
    ):
        def fake_import_user_material(material_key, **kwargs):
            assert material_key == "master_resume"
            assert kwargs["source_url"] == "https://example.com/resume.txt"
            return {
                "material_key": material_key,
                "text": "Imported from URL\n",
                "settings": {
                    "materials": {
                        "master_resume": {
                            "content": "Imported from URL\n",
                        }
                    }
                },
                "bootstrap": {"onboarding": {"complete": False}},
            }

        monkeypatch.setattr(web_settings_api, "import_user_material", fake_import_user_material)

        resp = client.post(
            "/api/settings/materials/import",
            json={
                "material_key": "master_resume",
                "source_url": "https://example.com/resume.txt",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["settings"]["materials"]["master_resume"]["content"] == "Imported from URL\n"


def test_app_js_get_job_action_models_uses_backend_visible_action_ids(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "const summary = _queueReviewSummary(job);" in resp.text
    assert "const actionIds = Array.isArray(summary.visible_actions) ? summary.visible_actions : [];" in resp.text


def test_start_workers_launches_worker_pool_with_devnull_stdin(monkeypatch):
    import types

    import job_web

    popen_calls: list[dict] = []

    def _fake_popen(cmd, **kwargs):
        popen_calls.append({"cmd": list(cmd), "kwargs": dict(kwargs)})
        return types.SimpleNamespace(pid=4242, poll=lambda: None)

    monkeypatch.setattr(job_web, "_worker_proc", None)
    monkeypatch.setattr(job_web, "is_worker_running", lambda: False)
    monkeypatch.setattr(job_web.subprocess, "Popen", _fake_popen)

    job_web.start_workers(num_workers=3)

    assert len(popen_calls) == 1
    assert popen_calls[0]["kwargs"]["stdin"] is job_web.subprocess.DEVNULL


def test_app_js_action_descriptor_includes_archive_and_unarchive_mappings(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "archive: {" in resp.text
    assert "detailLabel: 'Archive'" in resp.text
    assert "unarchive: {" in resp.text
    assert "detailLabel: 'Unarchive'" in resp.text


def test_app_js_command_palette_adds_unlock_and_gates_locked_rerun_actions(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "const isLockedSubmission = job && job.submission_lock_state === 'locked';" in resp.text
    assert "if (isLockedSubmission) {" in resp.text
    assert "label: 'Unlock to Resubmit'" in resp.text
    assert "if (!isLockedSubmission && isDraft) {" in resp.text
    assert "if (!isLockedSubmission && inDetail && job) {" in resp.text


def test_app_js_exposes_relock_controls_for_unlocked_resubmissions(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "lock_resubmission: {" in resp.text
    assert "detailLabel: 'Lock Resubmission'" in resp.text
    assert "queueLabel: 'Lock'" in resp.text
    assert "handler: () => lockResubmission(job.id, createActionContext(surface, 'button'))" in resp.text


def test_app_js_keyboard_shortcuts_gate_locked_rerun_and_regenerate_keys(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "if (!isLockedSubmission && e.key === 'a' && job.status === 'draft')" in resp.text
    assert "if (!isLockedSubmission && e.key === 'r' && !e.shiftKey)" in resp.text
    assert "if (!isLockedSubmission && e.key === 'R' && e.shiftKey)" in resp.text
    assert "if (!isLockedSubmission && e.key === 'y')" in resp.text
    assert "if (!isLockedSubmission && e.key === 'u')" in resp.text
    assert "if (!isLockedSubmission && e.key === 'i')" in resp.text


def test_app_js_does_not_hijack_browser_modified_shortcuts_or_bind_linkedin_hotkey(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "const hasShortcutModifier = e.metaKey || e.ctrlKey || e.altKey;" in resp.text
    assert "if (hasShortcutModifier) return;" in resp.text
    assert "keys: ['L']" not in resp.text
    assert "if (e.key === 'l')" not in resp.text


def test_app_js_tab_action_bar_hides_regenerate_when_locked(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "if (job && job.submission_lock_state === 'locked') return document.createDocumentFragment();" in resp.text


def test_app_js_exposes_reset_to_new_helper_and_action(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "async function resetJobToNew(jobId, actionContext = createActionContext('detail', 'button'))" in resp.text
    assert "await apiCall('POST', `/api/jobs/${jobId}/reset-to-new`, null, actionContext);" in resp.text
    assert "label: 'Reset to New'" in resp.text


def test_app_js_attaches_structured_action_audit_headers(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "X-Jobapps-Action-Surface" in resp.text
    assert "X-Jobapps-Action-Trigger" in resp.text
    assert "X-Jobapps-Request-Id" in resp.text
    assert "function createActionContext(surface, trigger)" in resp.text


def test_app_js_renders_action_audit_metadata_in_timeline_and_activity_feed(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "function formatEventActionAudit(ev)" in resp.text
    assert "Source:" in resp.text
    assert "Request:" in resp.text


def test_app_js_opens_dedup_modal_with_flex_layout(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "document.getElementById('dedup-modal').style.display = 'flex';" in resp.text


def test_app_js_user_tab_switch_resets_to_panel_start(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "function alignJobDetailContent(panel, options = {}) {" in resp.text
    assert "const { preferPanelStart = false } = options;" in resp.text
    assert "const anchor = preferPanelStart ? panel : getJobDetailContentAnchor(panel);" in resp.text
    assert "alignJobDetailContent(el, { preferPanelStart: true });" in resp.text


def test_app_js_resets_job_action_row_scroll_before_rerender(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "row.scrollLeft = 0;" in resp.text


def test_app_js_exposes_job_detail_modal_route_and_queue_entrypoints(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "if (hash.startsWith('job-modal/')) {" in resp.text
    assert "function openJobDetailModal(jobId) {" in resp.text
    assert "location.hash = '#job-modal/' + jobId;" in resp.text
    assert "document.getElementById('job-detail-modal-backdrop').style.display = 'block';" in resp.text
    assert "document.getElementById('view-job').classList.add('job-detail-modal');" in resp.text
    assert "class=\"queue-job-modal-trigger\"" in resp.text
    assert "onclick=\"openJobDetailModalFromEvent(event, ${job.id})\"" in resp.text


def test_app_js_moves_focus_into_job_detail_modal_when_opened(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "detailView.tabIndex = -1;" in resp.text
    assert "detailView.focus({ preventScroll: true });" in resp.text


def test_app_js_queue_keyboard_shortcuts_expose_open_as_modal(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "if (e.key === 'Enter' && e.shiftKey) { _openSelectedQueueRow(true); return; }" in resp.text
    assert "function _openSelectedQueueRow(modal = false) {" in resp.text
    assert "if (id) location.hash = modal ? '#job-modal/' + id : '#job/' + id;" in resp.text


def test_app_js_escape_overlay_close_path_includes_job_detail_modal(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "function _closeVisibleOverlay() {" in resp.text
    assert "if (_isJobDetailModalOpen()) {" in resp.text
    assert "closeJobDetailModal();" in resp.text
    assert "if (e.key === 'Escape') {" in resp.text
    assert "if (_closeVisibleOverlay()) return;" in resp.text


def test_root_and_shortcut_help_document_queue_modal_open_shortcut(client):
    root_resp = client.get("/")
    app_resp = client.get("/static/app.js")

    assert root_resp.status_code == 200
    assert app_resp.status_code == 200
    assert '<kbd>&#8679;Enter</kbd><span>Open selected job as modal</span>' in root_resp.text
    assert "['Shift+Enter', 'Open job as modal']" in app_resp.text


def test_bulk_action_shell_exposes_lock_aware_controls(client):
    root_resp = client.get("/")
    app_resp = client.get("/static/app.js")

    assert root_resp.status_code == 200
    assert app_resp.status_code == 200
    assert 'id="bulk-approve-btn"' in root_resp.text
    assert 'id="bulk-restart-draft-btn"' in root_resp.text
    assert 'id="bulk-restart-submit-btn"' in root_resp.text
    assert "selectedJobsContainLockedSubmission()" in app_resp.text


def test_root_queue_uses_job_confidence_and_actions_columns(client):
    resp = client.get("/")

    assert resp.status_code == 200
    assert 'data-sort-field="company"' in resp.text
    assert 'data-sort-field="status_entered_at"' in resp.text
    assert 'data-sort-field="confidence"' in resp.text
    assert ">Actions<" in resp.text
    assert '<tr class="empty-row"><td colspan="5">Loading...</td></tr>' in resp.text


def test_root_exposes_queue_search_clear_and_clickable_headers(client):
    resp = client.get("/")

    assert resp.status_code == 200
    assert 'id="search-clear-btn"' in resp.text
    assert 'class="search-input-shell"' in resp.text
    assert 'data-sort-field="company"' in resp.text
    assert 'data-sort-field="status_entered_at"' in resp.text
    assert 'data-sort-field="confidence"' in resp.text
    assert 'onclick="handleQueueHeaderSort(' in resp.text
    assert 'onclick="handleWorkerHeaderSort(' in resp.text


def test_root_exposes_queue_and_worker_sort_controls(client):
    resp = client.get("/")

    assert resp.status_code == 200
    assert 'id="queue-sort-field"' in resp.text
    assert 'id="queue-sort-dir-btn"' in resp.text
    assert 'id="worker-sort-field"' in resp.text
    assert 'id="worker-sort-dir-btn"' in resp.text
    assert 'value="worker_id"' in resp.text
    assert 'value="status"' in resp.text
    assert 'value="company"' in resp.text
    assert 'value="role_title"' in resp.text
    assert 'value="status_entered_at"' in resp.text
    assert 'value="progress"' in resp.text
    assert 'value="confidence"' in resp.text
    assert 'value="phase"' in resp.text
    assert 'value="elapsed"' in resp.text
    assert 'value="board"' in resp.text
    assert 'value="job_id"' in resp.text


def test_root_shortcut_help_omits_removed_linkedin_hotkey(client):
    resp = client.get("/")

    assert resp.status_code == 200
    assert '<kbd>L</kbd><span>Open LinkedIn</span>' not in resp.text


def test_app_js_exposes_queue_specific_action_and_review_helpers(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "function actionDescriptorForId(job, actionId, surface = 'detail') {" in resp.text
    assert "function getJobActionModels(job, surface = 'detail') {" in resp.text
    assert "function runQueueAction(event, jobId, actionId) {" in resp.text
    assert "function buildQueueJobCell(job) {" in resp.text
    assert "function buildQueueConfidenceCell(job) {" in resp.text
    assert "function buildQueueActionsCell(job) {" in resp.text
    assert "queue_review_summary" in resp.text
    assert 'onclick="rowClick(event, ${job.id})"' not in resp.text


def test_app_js_exposes_worker_and_queue_sort_models(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "const QUEUE_SORT_OPTIONS = [" in resp.text
    assert "const WORKER_SORT_OPTIONS = [" in resp.text
    assert "function handleQueueHeaderSort(field) {" in resp.text
    assert "function handleWorkerHeaderSort(field) {" in resp.text
    assert "function clearSearchInput() {" in resp.text
    assert "function syncSearchClearButton() {" in resp.text


def test_app_js_coalesces_overlapping_queue_refreshes(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "let _queueRefreshInFlight = null;" in resp.text
    assert "let _queueRefreshQueued = false;" in resp.text
    assert "if (_queueRefreshInFlight) {" in resp.text
    assert "_queueRefreshQueued = _queueRefreshQueued || showLoading;" in resp.text
    assert "function setQueueSortField(value) {" in resp.text
    assert "function toggleQueueSortDir() {" in resp.text
    assert "function setWorkerSortField(value) {" in resp.text
    assert "function toggleWorkerSortDir() {" in resp.text


def test_app_js_answers_tab_uses_active_proof_artifacts_without_guessing_missing_files(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "if (proof.report_json) candidates.push(proof.report_json);" in resp.text
    assert "if (proof.application_answers_json) answerCandidates.push(proof.application_answers_json);" in resp.text
    assert "candidates.push('autofill_report.json');" not in resp.text
    assert "answerCandidates.push('application_answers.json');" not in resp.text


def test_app_js_screenshot_tab_renders_all_active_proof_images(client):
    resp = client.get("/static/app.js")
    screenshot_tab_src = resp.text.split("async function loadScreenshotTab", maxsplit=1)[1].split(
        "async function loadConfirmationTab",
        maxsplit=1,
    )[0]

    assert resp.status_code == 200
    assert "const imageCandidates = [" in screenshot_tab_src
    assert "['Review screenshot', proof.review_screenshot]" in screenshot_tab_src
    assert "['Pre-submit screenshot', proof.pre_submit_screenshot]" in screenshot_tab_src
    assert "['Submit debug screenshot', proof.submit_debug_screenshot]" in screenshot_tab_src
    assert "let renderedCount = 0;" in screenshot_tab_src
    assert "for (const [label, filename] of imageCandidates)" in screenshot_tab_src
    assert "img.alt = label;" in screenshot_tab_src
    assert "renderedCount += 1;" in screenshot_tab_src
    assert "candidates.push('autofill_pre_submit.png', 'submit_debug.png');" not in screenshot_tab_src
    assert "ALL_BOARDS.flatMap" not in screenshot_tab_src


def test_app_js_screenshot_tab_dedupes_duplicate_proof_images(client):
    resp = client.get("/static/app.js")
    screenshot_tab_src = resp.text.split("async function loadScreenshotTab", maxsplit=1)[1].split(
        "async function loadConfirmationTab",
        maxsplit=1,
    )[0]

    assert resp.status_code == 200
    assert "const seenProofFilenames = new Set();" in screenshot_tab_src
    assert "if (seenProofFilenames.has(filename)) {" in screenshot_tab_src
    assert "seenProofFilenames.add(filename);" in screenshot_tab_src


def test_app_js_reloads_selected_tab_when_selected_job_proof_or_failure_changes(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "function jobDetailReloadFingerprint(job) {" in resp.text
    assert "function shouldReloadSelectedJobTab(previousJob, nextJob) {" in resp.text
    assert "proof_revision: proof.proof_revision || ''" in resp.text
    assert "const shouldReloadCurrentTab = currentTab !== 'logs' && shouldReloadSelectedJobTab(previousJob, job);" in resp.text
    assert "if (shouldReloadCurrentTab) {" in resp.text


def test_app_js_passively_refreshes_queue_and_selected_job_views(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "async function refreshCurrentJobDetail() {" in resp.text
    assert "function refreshPassiveViewData() {" in resp.text
    assert "setInterval(refreshPassiveViewData, 5000);" in resp.text


def test_app_js_merges_queue_and_websocket_jobs_into_existing_detail_state(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "function mergeJobState(previousJob, nextJob) {" in resp.text
    assert "window.jobs[job.id] = mergeJobState(window.jobs[job.id], job);" in resp.text
    assert "window.jobs[msg.job.id] = mergeJobState(window.jobs[msg.job.id], msg.job);" in resp.text
    assert "job = mergeJobState(window.jobs[job.id], job);" in resp.text


def test_style_css_includes_queue_confidence_and_micro_action_selectors(client):
    resp = client.get("/static/style.css")

    assert resp.status_code == 200
    assert ".queue-job-cell" in resp.text
    assert ".queue-entered-cell" in resp.text
    assert ".queue-review-summary" in resp.text
    assert ".queue-actions" in resp.text
    assert ".queue-action-btn" in resp.text
    assert ".queue-confidence-badge" in resp.text
    assert ".search-input-shell" in resp.text
    assert ".search-clear-btn" in resp.text


def test_compact_mode_targets_job_table_not_removed_queue_table_class(client):
    resp = client.get("/static/style.css")

    assert resp.status_code == 200
    assert '[data-compact="true"] .job-table td' in resp.text
    assert '[data-compact="true"] .job-table th' in resp.text
    assert '[data-compact="true"] .queue-table td' not in resp.text
    assert '[data-compact="true"] .queue-table th' not in resp.text


def test_add_jobs(client):
    resp = client.post(
        "/api/jobs",
        json={
            "urls": ["https://boards.greenhouse.io/co/jobs/1", "https://boards.greenhouse.io/co/jobs/2"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["added"] == 2
    assert data["duplicates"] == 0


def test_import_saved_portal_endpoint_dispatches_and_returns_result(client):
    import job_web

    expected = {
        "status": "ok",
        "message": "",
        "scraped": 6,
        "resolved": 4,
        "added": 2,
        "duplicates": 1,
        "skipped_unresolved": 3,
        "errors": 0,
    }

    with mock.patch.object(job_web, "_import_saved_portal_jobs", return_value=expected) as run_import:
        resp = client.post(
            "/api/jobs/import/trueup",
            json={"priority": 5, "provider": "codex"},
        )

    assert resp.status_code == 200
    assert resp.json() == expected
    run_import.assert_called_once()
    _conn = run_import.call_args.args[0]
    assert isinstance(_conn, sqlite3.Connection)
    assert run_import.call_args.kwargs["portal"] == "trueup"
    assert run_import.call_args.kwargs["priority"] == 5
    assert run_import.call_args.kwargs["provider"] == "codex"


def test_import_saved_portal_endpoint_supports_jackandjill(client):
    import job_web

    expected = {
        "status": "ok",
        "message": "",
        "scraped": 5,
        "resolved": 4,
        "added": 3,
        "duplicates": 1,
        "skipped_unresolved": 1,
        "errors": 0,
    }

    with mock.patch.object(job_web, "_import_saved_portal_jobs", return_value=expected) as run_import:
        resp = client.post(
            "/api/jobs/import/jackandjill",
            json={"priority": 10, "provider": "claude"},
        )

    assert resp.status_code == 200
    assert resp.json() == expected
    assert run_import.call_args.kwargs["portal"] == "jackandjill"
    assert run_import.call_args.kwargs["priority"] == 10
    assert run_import.call_args.kwargs["provider"] == "claude"


def test_import_saved_portal_endpoint_rejects_unknown_portal(client):
    resp = client.post("/api/jobs/import/unknown", json={"priority": 0, "provider": None})
    assert resp.status_code == 404


def test_import_saved_portal_jobs_uses_shared_registry_module_loader(client):
    import job_web

    fake_module = mock.Mock()
    fake_module.import_saved_jobs.return_value = {"status": "ok", "added": 2}

    with mock.patch("saved_portal_import.load_saved_portal_module", return_value=fake_module) as load_module:
        result = job_web._import_saved_portal_jobs(
            job_web.get_conn(),
            portal="jackandjill",
            priority=5,
            provider="codex",
        )

    assert result == {"status": "ok", "added": 2}
    load_module.assert_called_once_with("jackandjill")
    fake_module.import_saved_jobs.assert_called_once()


def test_import_linkedin_saved_alias_dispatches_to_saved_portal_import(client):
    import job_web

    expected = {
        "status": "ok",
        "message": "",
        "scraped": 3,
        "resolved": 2,
        "added": 1,
        "duplicates": 1,
        "skipped_unresolved": 1,
        "errors": 0,
    }

    with mock.patch.object(job_web, "_import_saved_portal_jobs", return_value=expected) as run_import:
        resp = client.post("/api/jobs/import-linkedin-saved")

    assert resp.status_code == 200
    assert resp.json() == expected
    run_import.assert_called_once()
    _conn = run_import.call_args.args[0]
    assert isinstance(_conn, sqlite3.Connection)
    assert run_import.call_args.kwargs["portal"] == "linkedin"


def test_app_js_exposes_generic_saved_portal_lookup_for_jackandjill(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "const SAVED_PORTAL_UI =" in resp.text
    assert "jackandjill: { label: 'Jack & Jill'" in resp.text
    assert "buttonId: 'jackandjill-import-btn'" in resp.text
    assert "addButtonId: 'add-jackandjill-import-btn'" in resp.text
    assert "Unknown saved portal UI config" in resp.text


def test_saved_portal_ui_stays_in_sync_with_registry(client):
    root_resp = client.get("/")
    app_resp = client.get("/static/app.js")

    assert root_resp.status_code == 200
    assert app_resp.status_code == 200
    for spec in saved_portal_import.list_saved_portals():
        assert f'id="{spec.key}-import-btn"' in root_resp.text
        assert f'id="add-{spec.key}-import-btn"' in root_resp.text
        assert f"{spec.key}: {{ label: '{spec.label}'" in app_resp.text


def test_list_jobs_returns_current_status_timestamp_for_redrafted_draft(client):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/redraft-when"]})
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft")
    conn.execute(
        "UPDATE events SET created_at = '2026-03-26 22:00:00' "
        "WHERE job_id = 1 AND event_type = 'status_change' AND detail = 'draft'"
    )
    conn.execute("DROP TRIGGER IF EXISTS trg_jobs_updated_at")
    conn.execute(
        "UPDATE jobs SET updated_at = '2026-03-26 22:01:07', completed_at = '2026-03-18 05:10:46' WHERE id = 1"
    )
    conn.commit()

    resp = client.get("/api/jobs")

    assert resp.status_code == 200
    assert resp.json()[0]["status_entered_at"] == "2026-03-26 22:00:00"
    assert resp.json()[0]["status_entered_at_source"] == "status_change"
    assert resp.json()[0]["queue_timestamp"] == "2026-03-26 22:00:00"
    assert resp.json()[0]["queue_timestamp_source"] == "status_change"


def test_get_job_detail_returns_current_status_timestamp(client):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/detail-status-time"]})
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "stopped", error_message="Needs manual review")
    conn.execute(
        "UPDATE events SET created_at = '2026-03-26 22:02:03' "
        "WHERE job_id = 1 AND event_type = 'status_change' AND detail = 'stopped'"
    )
    conn.execute("DROP TRIGGER IF EXISTS trg_jobs_updated_at")
    conn.execute("UPDATE jobs SET updated_at = '2026-03-26 22:09:59' WHERE id = 1")
    conn.commit()

    resp = client.get("/api/jobs/1")

    assert resp.status_code == 200
    assert resp.json()["status"] == "stopped"
    assert resp.json()["status_entered_at"] == "2026-03-26 22:02:03"
    assert resp.json()["status_entered_at_source"] == "status_change"


def test_app_js_formats_status_timestamps_in_utc(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "function formatUtcTimestamp(value)" in resp.text
    assert "function buildQueueEnteredCell(job) {" in resp.text
    assert "const timestamp = formatUtcTimestamp(queueTimestamp(job));" in resp.text
    assert "timeEl.textContent = formatUtcTimestamp(ev.created_at);" in resp.text


def test_add_duplicate(client):
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/1"]})
    resp = client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/1"]})
    data = resp.json()
    assert data["added"] == 0
    assert data["duplicates"] == 1


def test_add_duplicate_canonical_url_variant(client):
    client.post(
        "/api/jobs",
        json={"urls": ["https://www.linkedin.com/jobs/view/4366508877?trackingId=abc123"]},
    )

    resp = client.post(
        "/api/jobs",
        json={"urls": ["https://www.linkedin.com/jobs/view/4366508877/"]},
    )

    data = resp.json()
    assert data["added"] == 0
    assert data["duplicates"] == 1


def test_reconcile_dedup_endpoint_archives_late_cross_source_duplicate(client, tmp_path):
    import job_web
    from job_db import add_job, get_job, update_status

    conn = job_web.get_conn()
    keeper_out = tmp_path / "output" / "valon" / "senior-pm-product-infrastructure"
    keeper_out.mkdir(parents=True)
    (keeper_out / ".pipeline_meta.json").write_text(
        json.dumps(
            {
                "company": "valon",
                "company_proper": "Valon",
                "role": "senior-pm-product-infrastructure",
                "board": "ashby",
                "board_url": "https://jobs.ashbyhq.com/valon/0f1fbd7d-e30d-4b8c-9e3d-04ff6bfd7638",
            }
        ),
        encoding="utf-8",
    )

    keeper_id = add_job(conn, "https://www.linkedin.com/jobs/view/4366508877?trackingId=abc123")
    update_status(conn, keeper_id, "draft", output_dir=str(keeper_out))
    duplicate_id = add_job(
        conn,
        "https://jobs.ashbyhq.com/valon/0f1fbd7d-e30d-4b8c-9e3d-04ff6bfd7638",
        company="Valon",
        role_title="Valon hiring Senior Product Manager, Product Infrastructure in San Francisco, CA | LinkedIn",
    )
    update_status(
        conn,
        duplicate_id,
        "stopped",
        company="Valon",
        role_title="Valon hiring Senior Product Manager, Product Infrastructure in San Francisco, CA | LinkedIn",
        board="ashby",
        board_url="https://jobs.ashbyhq.com/valon/0f1fbd7d-e30d-4b8c-9e3d-04ff6bfd7638",
    )

    resp = client.post("/api/jobs/dedup/reconcile")

    assert resp.status_code == 200
    assert resp.json()["metadata_backfilled"] == 1
    assert resp.json()["archived"] == 1
    assert resp.json()["archived_job_ids"] == [duplicate_id]
    duplicate = get_job(conn, duplicate_id)
    assert duplicate["status"] == "stopped"
    assert bool(duplicate["archived"]) is True
    assert duplicate["failure_type"] == "duplicate"
    assert f"job #{keeper_id}" in str(duplicate["error_message"] or "")


def test_get_job(client):
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/1"]})
    resp = client.get("/api/jobs/1")
    assert resp.status_code == 200
    assert resp.json()["id"] == 1


def test_get_job_not_found(client):
    resp = client.get("/api/jobs/999")
    assert resp.status_code == 404


def test_retry_job(client):
    from job_db import RETRY_AFTER_SENTINEL, get_job, update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/1"]})
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "stopped")
    conn.execute("UPDATE jobs SET provider = 'claude', retry_after = datetime('now', '+1 hour') WHERE id = 1")
    conn.commit()
    resp = client.post("/api/jobs/1/retry")
    assert resp.status_code == 200
    job = get_job(conn, 1)
    assert job["status"] == "queued"
    assert job["provider"] is None
    assert job["retry_after"] == RETRY_AFTER_SENTINEL


def test_get_job_detail_includes_answer_refresh_state(client, tmp_path):
    from answer_refresh_state import mark_answer_refresh_pending
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/refresh-detail"]})
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir))
    mark_answer_refresh_pending(out_dir, request_kind="reanswer")

    resp = client.get("/api/jobs/1")

    assert resp.status_code == 200
    assert resp.json()["answer_refresh"]["status"] == "pending"
    assert resp.json()["answer_refresh"]["request_kind"] == "reanswer"


def test_get_job_detail_includes_resolved_answer_refresh_metadata(client, tmp_path):
    from answer_refresh_state import finalize_answer_refresh, mark_answer_refresh_pending
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/refresh-fresh"]})
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir))
    pending = mark_answer_refresh_pending(out_dir, request_kind="reanswer")
    finalize_answer_refresh(
        out_dir,
        request_id=pending["request_id"],
        status="fresh",
        message="Fresh answer generation proof recorded.",
        answer_provider="claude",
        answer_generated_at_utc="2026-03-26T18:30:00+00:00",
        generated_answer_count=3,
    )

    resp = client.get("/api/jobs/1")

    assert resp.status_code == 200
    assert resp.json()["answer_refresh"]["status"] == "fresh"
    assert resp.json()["answer_refresh"]["answer_provider"] == "claude"
    assert resp.json()["answer_refresh"]["generated_answer_count"] == 3


def test_get_job_detail_includes_active_pending_user_input(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/pending"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "pending_user_input.json").write_text(
        json.dumps({"status": "pending_user_input", "questions": [{"label": "Ethnicity"}]}),
        encoding="utf-8",
    )
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir))

    resp = client.get("/api/jobs/1")

    assert resp.status_code == 200
    assert resp.json()["pending_user_input"]["questions"][0]["label"] == "Ethnicity"


def test_queue_endpoint_includes_queue_review_summary(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/review-summary"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "pending_user_input.json").write_text(
        json.dumps({"status": "pending_user_input", "questions": [{"label": "Portfolio URL"}]}),
        encoding="utf-8",
    )

    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir))

    resp = client.get("/api/queue")

    assert resp.status_code == 200
    summary = resp.json()["jobs"][0]["queue_review_summary"]
    assert summary["overall_confidence"] == "low"
    assert summary["confidence_label"] == "Needs review before submit"
    assert "approve_submit" in summary["visible_actions"]


def test_queue_endpoint_downgrades_ready_drafts_with_optional_unknown_questions(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://jobs.ashbyhq.com/co/optional-review"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "ashby_autofill_report.json").write_text(
        json.dumps(
            _filled_autofill_report_payload(
                unknown_questions=[
                    {
                        "field_name": "application_current_company",
                        "label": "Current company",
                        "field_type": "String",
                        "required": False,
                        "status": "unknown_optional",
                    }
                ]
            )
        ),
        encoding="utf-8",
    )
    (submit_dir / "ashby_autofill_pre_submit.png").write_bytes(b"png")
    (out_dir / "answer_verification_status.json").write_text(
        json.dumps({"status": "verified"}),
        encoding="utf-8",
    )

    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="ashby")

    resp = client.get("/api/queue")

    assert resp.status_code == 200
    summary = resp.json()["jobs"][0]["queue_review_summary"]
    assert summary["overall_confidence"] == "medium"
    assert summary["confidence_label"] == "Usable, but review recommended"
    assert [chip["label"] for chip in summary["reason_chips"]] == [
        "Proof current",
        "No AI answers",
        "1 unresolved optional field",
    ]


def test_queue_endpoint_clears_stale_progress_when_current_proof_promotes_job_back_to_draft(client, tmp_path):
    from job_db import get_job, update_status

    client.post("/api/jobs", json={"urls": ["https://apply.workable.com/j/stale-progress-proof"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (docs_dir / "Candidate Name Resume - Workable.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Candidate Name Cover Letter - Workable.pdf").write_text("cover", encoding="utf-8")
    (submit_dir / "workable_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (submit_dir / "workable_autofill_pre_submit.png").write_bytes(b"png")

    import job_web

    conn = job_web.get_conn()
    update_status(
        conn,
        1,
        "queued",
        output_dir=str(out_dir),
        board="workable",
        progress="Provider claude failed: stale worker error",
    )

    resp = client.get("/api/queue")

    assert resp.status_code == 200
    job = resp.json()["jobs"][0]
    assert job["status"] == "draft"
    assert job["progress"] == ""
    assert get_job(conn, 1)["progress"] == ""


def test_get_job_detail_blocks_visible_self_id_unknown_optional_question(client, tmp_path, monkeypatch):
    import application_submit_common as submit_common
    from job_db import update_status

    application_profile = tmp_path / "application_profile.md"
    application_profile.write_text(
        "\n".join(
            [
                "Country: United States",
                "Location: San Francisco, CA",
                "Work Authorization Statement: Authorized to work in the United States.",
                "Authorized to Work Unconditionally: Yes",
                "Require Sponsorship Now: No",
                "Require Sponsorship in Future: No",
                "Sponsorship Answer: No",
                "Gender: Man",
                "Gender Identity: Man",
                "Race or Ethnicity: Asian",
                "Veteran Status: I am not a protected veteran",
                "Disability Status: No, I do not have a disability",
                "Sexual Orientation: Prefer not to answer",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(submit_common, "APPLICATION_PROFILE_PATH", application_profile)

    client.post("/api/jobs", json={"urls": ["https://jobs.ashbyhq.com/co/visible-self-id-blocker"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "ashby_autofill_report.json").write_text(
        json.dumps(
            _filled_autofill_report_payload(
                unknown_questions=[
                    {
                        "field_name": "survey_1_what_is_your_gender_identity",
                        "label": "What is your gender identity?",
                        "field_type": "ValueSelect",
                        "required": False,
                        "status": "unknown_optional",
                    }
                ]
            )
        ),
        encoding="utf-8",
    )
    (submit_dir / "ashby_autofill_pre_submit.png").write_bytes(b"png")

    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="ashby")

    resp = client.get("/api/jobs/1")

    assert resp.status_code == 200
    assert resp.json()["draft_review_state"]["state"] == "blocked"
    assert "gender identity" in resp.json()["draft_review_state"]["reason"].lower()


def test_queue_endpoint_sorts_by_status_entered_at(client):
    from job_db import update_status

    client.post(
        "/api/jobs",
        json={
            "urls": [
                "https://boards.greenhouse.io/co/jobs/status-sort-1",
                "https://boards.greenhouse.io/co/jobs/status-sort-2",
                "https://boards.greenhouse.io/co/jobs/status-sort-3",
            ]
        },
    )
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft")
    update_status(conn, 2, "draft")
    update_status(conn, 3, "draft")
    conn.execute(
        "UPDATE events SET created_at = '2026-03-26 22:00:00' "
        "WHERE job_id = 1 AND event_type = 'status_change' AND detail = 'draft'"
    )
    conn.execute(
        "UPDATE events SET created_at = '2026-03-26 22:01:00' "
        "WHERE job_id = 2 AND event_type = 'status_change' AND detail = 'draft'"
    )
    conn.execute(
        "UPDATE events SET created_at = '2026-03-26 22:02:00' "
        "WHERE job_id = 3 AND event_type = 'status_change' AND detail = 'draft'"
    )
    conn.commit()

    resp = client.get("/api/queue?sort_field=status_entered_at&sort_dir=asc&limit=10")

    assert resp.status_code == 200
    assert [job["id"] for job in resp.json()["jobs"][:3]] == [1, 2, 3]


def test_queue_endpoint_sorts_by_confidence(client, tmp_path):
    from job_db import update_status

    client.post(
        "/api/jobs",
        json={
            "urls": [
                "https://boards.greenhouse.io/co/jobs/confidence-medium",
                "https://boards.greenhouse.io/co/jobs/confidence-pending",
                "https://boards.greenhouse.io/co/jobs/confidence-low",
            ]
        },
    )
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "pending_user_input.json").write_text(
        json.dumps({"status": "pending_user_input", "questions": [{"label": "Portfolio URL"}]}),
        encoding="utf-8",
    )

    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft")
    update_status(conn, 3, "draft", output_dir=str(out_dir))

    resp = client.get("/api/queue?sort_field=confidence&sort_dir=asc&limit=10")

    assert resp.status_code == 200
    assert [job["id"] for job in resp.json()["jobs"][:3]] == [2, 3, 1]


def test_queue_endpoint_sorts_by_progress(client):
    from job_db import update_status

    client.post(
        "/api/jobs",
        json={
            "urls": [
                "https://boards.greenhouse.io/co/jobs/progress-sort-1",
                "https://boards.greenhouse.io/co/jobs/progress-sort-2",
                "https://boards.greenhouse.io/co/jobs/progress-sort-3",
            ]
        },
    )
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", progress="Resume tailoring ready")
    update_status(conn, 2, "draft", progress="Answer review pending")
    update_status(conn, 3, "draft", progress="Cover letter drafted")

    resp = client.get("/api/queue?sort_field=progress&sort_dir=asc&limit=10")

    assert resp.status_code == 200
    assert [job["id"] for job in resp.json()["jobs"][:3]] == [2, 3, 1]


def test_queue_endpoint_text_sorts_keep_blank_fields_last(client):
    from job_db import update_status

    client.post(
        "/api/jobs",
        json={
            "urls": [
                "https://boards.greenhouse.io/co/jobs/company-sort-blank",
                "https://boards.greenhouse.io/co/jobs/company-sort-alpha",
                "https://boards.greenhouse.io/co/jobs/company-sort-zeta",
            ]
        },
    )
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", company="")
    update_status(conn, 2, "draft", company="Airtable")
    update_status(conn, 3, "draft", company="Zoom")

    resp = client.get("/api/queue?sort_field=company&sort_dir=asc&limit=10")

    assert resp.status_code == 200
    assert [job["id"] for job in resp.json()["jobs"][:3]] == [2, 3, 1]


def test_job_detail_includes_queue_review_summary(client):
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/detail-summary"]})

    resp = client.get("/api/jobs/1")

    assert resp.status_code == 200
    assert "queue_review_summary" in resp.json()
    assert "visible_actions" in resp.json()["queue_review_summary"]


def test_get_job_detail_includes_proof_artifacts_from_active_submit_dir(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/proof"]})
    out_dir = tmp_path / "job-output"
    active_submit = out_dir / "submit-20260326T010203Z"
    active_submit.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    (active_submit / "greenhouse_autofill_report.json").write_text("{}", encoding="utf-8")
    (active_submit / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="greenhouse")

    resp = client.get("/api/jobs/1")

    assert resp.status_code == 200
    assert resp.json()["proof_artifacts"]["report_json"] == "greenhouse_autofill_report.json"
    assert resp.json()["proof_artifacts"]["pre_submit_screenshot"] == "greenhouse_autofill_pre_submit.png"


def test_get_job_detail_includes_all_active_proof_image_names(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/proof-images"]})
    out_dir = tmp_path / "job-output"
    active_submit = out_dir / "submit-20260326T010203Z"
    active_submit.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    (active_submit / "greenhouse_autofill_report.json").write_text("{}", encoding="utf-8")
    (active_submit / "greenhouse_autofill_review.png").write_text("png", encoding="utf-8")
    (active_submit / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (active_submit / "greenhouse_submit_debug.png").write_text("png", encoding="utf-8")
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="greenhouse")

    resp = client.get("/api/jobs/1")

    assert resp.status_code == 200
    assert resp.json()["proof_artifacts"]["review_screenshot"] == "greenhouse_autofill_review.png"
    assert resp.json()["proof_artifacts"]["pre_submit_screenshot"] == "greenhouse_autofill_pre_submit.png"
    assert resp.json()["proof_artifacts"]["submit_debug_screenshot"] == "greenhouse_submit_debug.png"
    assert resp.json()["proof_artifacts"]["proof_revision"]


def test_get_job_detail_proof_revision_changes_when_active_proof_is_rewritten(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/proof-revision"]})
    out_dir = tmp_path / "job-output"
    active_submit = out_dir / "submit-20260326T010203Z"
    active_submit.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    report = active_submit / "greenhouse_autofill_report.json"
    screenshot = active_submit / "greenhouse_autofill_pre_submit.png"
    report.write_text("{}", encoding="utf-8")
    screenshot.write_text("png", encoding="utf-8")
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="greenhouse")

    first_resp = client.get("/api/jobs/1")
    first_revision = first_resp.json()["proof_artifacts"]["proof_revision"]

    updated_ns = screenshot.stat().st_mtime_ns + 1_000_000
    os.utime(screenshot, ns=(updated_ns, updated_ns))

    second_resp = client.get("/api/jobs/1")
    second_revision = second_resp.json()["proof_artifacts"]["proof_revision"]

    assert first_revision != second_revision


def test_get_job_detail_syncs_stale_row_from_current_submit_result(client, tmp_path):
    from job_db import get_job, update_status

    client.post("/api/jobs", json={"urls": ["https://qualcomm.eightfold.ai/careers/job/446717098736"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "skipped_auth",
                "board": "eightfold",
                "failure_type": "auth_guarded",
                "auth_state": "sign_in_gate",
                "auth_scope": "eightfold:qualcomm.eightfold.ai",
                "message": "Eightfold requires sign in or account creation before the application form is available.",
            }
        ),
        encoding="utf-8",
    )
    (submit_dir / "eightfold_autofill_pre_submit.png").write_text("png", encoding="utf-8")

    import job_web

    conn = job_web.get_conn()
    update_status(
        conn,
        1,
        "stopped",
        output_dir=str(out_dir),
        board="eightfold",
        failure_type="pending_user_input",
        error_message="Submission paused because one or more answers require manual user input.",
    )

    resp = client.get("/api/jobs/1")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["failure_type"] == "auth_guarded"
    assert payload["auth_state"] == "sign_in_gate"
    assert payload["auth_scope"] == "eightfold:qualcomm.eightfold.ai"
    assert payload["error_message"] == "Eightfold requires sign in or account creation before the application form is available."
    assert payload["proof_artifacts"]["pre_submit_screenshot"] == "eightfold_autofill_pre_submit.png"

    job = get_job(conn, 1)
    assert job["failure_type"] == "auth_guarded"
    assert job["auth_state"] == "sign_in_gate"


def test_get_job_detail_includes_linked_resource_artifacts(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/linked-proof"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "application_answers.json").write_text(
        json.dumps({"answers": {"sql_task": "done"}}), encoding="utf-8"
    )
    (submit_dir / "linked_resource_context.json").write_text(json.dumps({"resources": []}), encoding="utf-8")
    (submit_dir / "linked_resource_failures.json").write_text(json.dumps([]), encoding="utf-8")
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="greenhouse")

    resp = client.get("/api/jobs/1")

    assert resp.status_code == 200
    assert resp.json()["proof_artifacts"]["application_answers_json"] == "application_answers.json"
    assert resp.json()["proof_artifacts"]["linked_resource_context_json"] == "linked_resource_context.json"
    assert resp.json()["proof_artifacts"]["linked_resource_failures_json"] == "linked_resource_failures.json"


def test_queue_endpoint_syncs_visible_rows_from_current_submit_result(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://qualcomm.eightfold.ai/careers/job/446717098736?ref=queue"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "skipped_auth",
                "board": "eightfold",
                "failure_type": "auth_guarded",
                "auth_state": "sign_in_gate",
                "auth_scope": "eightfold:qualcomm.eightfold.ai",
                "message": "Eightfold requires sign in or account creation before the application form is available.",
            }
        ),
        encoding="utf-8",
    )
    (submit_dir / "eightfold_autofill_pre_submit.png").write_text("png", encoding="utf-8")

    import job_web

    conn = job_web.get_conn()
    update_status(
        conn,
        1,
        "stopped",
        output_dir=str(out_dir),
        board="eightfold",
        failure_type="pending_user_input",
        error_message="Submission paused because one or more answers require manual user input.",
    )

    resp = client.get("/api/queue?status=stopped")

    assert resp.status_code == 200
    payload = resp.json()["jobs"][0]
    assert payload["failure_type"] == "auth_guarded"
    assert payload["auth_state"] == "sign_in_gate"
    assert payload["error_message"] == "Eightfold requires sign in or account creation before the application form is available."


def test_sync_jobs_for_response_skips_repeated_noop_syncs_for_same_row(client, tmp_path):
    import job_web
    from job_db import get_job, update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/cache-sync"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="greenhouse")
    job = get_job(conn, 1)

    job_web._queue_response_sync_cache.clear()

    with mock.patch.object(job_web, "sync_job_from_disk", return_value={"updates": [], "changed": False}) as sync_disk:
        assert job_web._sync_jobs_for_response(conn, [job]) is False
        assert job_web._sync_jobs_for_response(conn, [job]) is False

    assert sync_disk.call_count == 1


def test_enrich_queue_rows_reuses_cached_review_summary_for_unchanged_rows(client, tmp_path):
    import job_web
    from job_db import get_job, update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/cache-summary"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="greenhouse")
    first_job = get_job(conn, 1)
    second_job = get_job(conn, 1)

    job_web._queue_review_summary_cache.clear()

    def fake_attach(rows):
        for row in rows:
            row["queue_review_summary"] = {"overall_confidence": "high"}
        return rows

    with mock.patch.object(job_web, "attach_queue_review_summary", side_effect=fake_attach) as attach_summary:
        job_web._enrich_queue_rows(conn, [first_job])
        job_web._enrich_queue_rows(conn, [second_job])

    assert attach_summary.call_count == 1
    assert first_job["queue_review_summary"]["overall_confidence"] == "high"
    assert second_job["queue_review_summary"]["overall_confidence"] == "high"


def test_list_documents_prioritizes_canonical_resume_and_cover_letter(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://www.linkedin.com/jobs/view/4242864623/"]})
    out_dir = tmp_path / "job-output"
    docs_dir = out_dir / "documents"
    docs_dir.mkdir(parents=True)
    (out_dir / ".pipeline_meta.json").write_text(
        json.dumps({"company_proper": "Cresta", "jd_url": "https://www.linkedin.com/jobs/view/4242864623/"}),
        encoding="utf-8",
    )
    (docs_dir / "Candidate Name Resume - Cresta..pdf").write_bytes(b"%PDF-stale-resume")
    (docs_dir / "Candidate Name Resume - Cresta.pdf").write_bytes(b"%PDF-canonical-resume")
    (docs_dir / "Candidate Name Cover Letter - Cresta..pdf").write_bytes(b"%PDF-stale-cover")
    (docs_dir / "Candidate Name Cover Letter - Cresta.pdf").write_bytes(b"%PDF-canonical-cover")

    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir))

    resp = client.get("/api/jobs/1/documents")

    assert resp.status_code == 200
    pdf_names = [entry["name"] for entry in resp.json()["files"] if entry["type"] == "pdf"]
    assert pdf_names[0] == "Candidate Name Resume - Cresta.pdf"
    assert pdf_names[1] == "Candidate Name Cover Letter - Cresta.pdf"


def test_get_job_detail_marks_stale_draft_when_only_historical_proof_has_screenshot(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/stale-proof"]})
    out_dir = tmp_path / "job-output"
    stale_submit = out_dir / "submit"
    stale_submit.mkdir(parents=True)
    (stale_submit / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    active_submit = out_dir / "submit-20260326T010203Z"
    active_submit.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    (active_submit / "greenhouse_autofill_report.json").write_text("{}", encoding="utf-8")

    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="greenhouse")

    resp = client.get("/api/jobs/1")

    assert resp.status_code == 200
    assert resp.json()["draft_review_state"]["state"] == "stale"
    assert "historical proof exists" in resp.json()["draft_review_state"]["reason"].lower()


def test_get_job_detail_allows_greenhouse_draft_when_review_screenshot_missing(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/missing-review-proof"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "greenhouse_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")

    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="greenhouse")

    resp = client.get("/api/jobs/1")

    assert resp.status_code == 200
    assert resp.json()["draft_review_state"]["state"] == "ready"


def test_get_job_detail_hides_duplicate_greenhouse_review_screenshot(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/duplicate-review-proof"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    pre_submit = submit_dir / "greenhouse_autofill_pre_submit.png"
    review = submit_dir / "greenhouse_autofill_review.png"
    pre_submit.write_bytes(b"same-proof")
    review.write_bytes(b"same-proof")
    (submit_dir / "greenhouse_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )

    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="greenhouse")

    resp = client.get("/api/jobs/1")

    assert resp.status_code == 200
    assert resp.json()["draft_review_state"]["state"] == "ready"
    assert resp.json()["proof_artifacts"]["pre_submit_screenshot"] == "greenhouse_autofill_pre_submit.png"
    assert resp.json()["proof_artifacts"]["review_screenshot"] is None


def test_get_job_detail_marks_legacy_draft_when_only_legacy_artifacts_exist(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/legacy-proof"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "autofill_report.json").write_text("{}", encoding="utf-8")
    (submit_dir / "pre_submit_screenshot.png").write_text("png", encoding="utf-8")

    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="greenhouse")

    resp = client.get("/api/jobs/1")

    assert resp.status_code == 200
    assert resp.json()["draft_review_state"]["state"] == "legacy"
    assert "legacy submit artifacts" in resp.json()["draft_review_state"]["reason"].lower()


def test_get_job_detail_marks_confirmed_submitted_duplicate_review_proof_as_legacy(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/confirmed-legacy-proof"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    pre_submit = submit_dir / "greenhouse_autofill_pre_submit.png"
    pre_submit.write_text("png", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (submit_dir / "greenhouse_autofill_payload.json").write_text(
        json.dumps({"artifacts": {"review_screenshot": str(pre_submit)}}),
        encoding="utf-8",
    )
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps({"status": "confirmed", "website_confirmed": True}),
        encoding="utf-8",
    )

    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "submitted", output_dir=str(out_dir), board="greenhouse")

    resp = client.get("/api/jobs/1")

    assert resp.status_code == 200
    assert resp.json()["draft_review_state"]["state"] == "legacy"
    assert "confirmed before the current draft-proof contract" in resp.json()["draft_review_state"]["reason"].lower()


def test_approve_endpoint_rejects_incomplete_draft(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/approve-blocked"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "pending_user_input.json").write_text(
        json.dumps({"status": "pending_user_input", "questions": [{"label": "Race or Ethnicity"}]}),
        encoding="utf-8",
    )
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir))

    resp = client.post("/api/jobs/1/approve")

    assert resp.status_code == 409
    assert "incomplete draft" in resp.json()["detail"].lower()


def test_approve_endpoint_records_action_audit_metadata(client, tmp_path):
    from job_db import get_job_timeline, update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/approve-audit"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Candidate Name Resume - Acme.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Candidate Name Cover Letter - Acme.pdf").write_text("cover", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_report.json").write_text("{}", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")

    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="greenhouse")

    resp = client.post(
        "/api/jobs/1/approve",
        headers={
            "X-Jobapps-Action-Surface": "detail",
            "X-Jobapps-Action-Trigger": "button",
            "X-Jobapps-Request-Id": "req-approve-1",
        },
    )

    assert resp.status_code == 200
    events = get_job_timeline(conn, 1)
    approved = next(ev for ev in events if ev["event_type"] == "approved_for_submit")
    assert approved["initiator"] == "web"
    assert json.loads(approved["detail_json"]) == {
        "action": {
            "surface": "detail",
            "trigger": "button",
            "request_id": "req-approve-1",
            "route": "/api/jobs/1/approve",
        }
    }


def test_approve_endpoint_clears_stale_current_attempt_result_before_submit(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/approve-stale-result"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Candidate Name Resume - Acme.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Candidate Name Cover Letter - Acme.pdf").write_text("cover", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_report.json").write_text("{}", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    stale_result = submit_dir / "application_submission_result.json"
    stale_result.write_text(
        json.dumps(
            {
                "status": "pending_user_input",
                "board": "greenhouse",
                "message": "Submission paused because one or more answers require manual user input.",
                "questions": [
                    {
                        "field_name": "Consent",
                        "label": "Consent",
                        "kind": "validation",
                        "required": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="greenhouse")

    approve_resp = client.post("/api/jobs/1/approve")
    detail_resp_1 = client.get("/api/jobs/1")
    detail_resp_2 = client.get("/api/jobs/1")
    row = conn.execute(
        "SELECT status, failure_type, error_message FROM jobs WHERE id = ?",
        (1,),
    ).fetchone()

    assert approve_resp.status_code == 200
    assert approve_resp.json() == {"status": "approved"}
    assert detail_resp_1.status_code == 200
    assert detail_resp_2.status_code == 200
    assert detail_resp_1.json()["status"] == "approved"
    assert detail_resp_2.json()["status"] == "approved"
    assert row["status"] == "approved"
    assert row["failure_type"] is None
    assert row["error_message"] in ("", None)
    assert not stale_result.exists()


def test_content_endpoint_reads_report_from_active_submit_dir(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/content-active"]})
    out_dir = tmp_path / "job-output"
    active_submit = out_dir / "submit-20260326T010203Z"
    active_submit.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    (active_submit / "ashby_autofill_report.json").write_text(
        json.dumps({"fields": [{"field_name": "first_name", "value": "Candidate"}]}),
        encoding="utf-8",
    )
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="ashby")

    resp = client.get("/api/jobs/1/content/ashby_autofill_report.json")

    assert resp.status_code == 200
    assert resp.json()["fields"][0]["field_name"] == "first_name"


def test_content_endpoint_reads_application_answers_from_active_submit_dir(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/content-answers"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "application_answers.json").write_text(
        json.dumps({"answers": {"sql_task": "Top card is abc123"}}),
        encoding="utf-8",
    )
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="ashby")

    resp = client.get("/api/jobs/1/content/application_answers.json")

    assert resp.status_code == 200
    assert resp.json()["answers"]["sql_task"] == "Top card is abc123"


def test_content_endpoint_serves_generic_pre_submit_alias_from_active_submit_dir(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/content-proof"]})
    out_dir = tmp_path / "job-output"
    active_submit = out_dir / "submit-20260326T010203Z"
    active_submit.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    screenshot = active_submit / "ashby_autofill_pre_submit.png"
    screenshot.write_bytes(b"png")
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="ashby")

    resp = client.get("/api/jobs/1/content/autofill_pre_submit.png")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


def test_content_endpoint_serves_review_screenshot_from_active_submit_dir(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/content-review-proof"]})
    out_dir = tmp_path / "job-output"
    active_submit = out_dir / "submit-20260326T010203Z"
    active_submit.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    screenshot = active_submit / "greenhouse_autofill_review.png"
    screenshot.write_bytes(b"png")
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="greenhouse")

    resp = client.get("/api/jobs/1/content/greenhouse_autofill_review.png")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


def test_content_endpoint_blocks_symlink_escape_for_active_submit_alias(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/content-proof-escape"]})
    out_dir = tmp_path / "job-output"
    active_submit = out_dir / "submit-20260326T010203Z"
    active_submit.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    escaped_dir = tmp_path / "job-output-escaped"
    escaped_dir.mkdir()
    escaped_target = escaped_dir / "ashby_autofill_pre_submit.png"
    escaped_target.write_bytes(b"png")
    os.symlink(escaped_target, active_submit / "ashby_autofill_pre_submit.png")
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="ashby")

    resp = client.get("/api/jobs/1/content/autofill_pre_submit.png")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Path traversal blocked"


def test_logs_endpoint_includes_workday_auth_artifact(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://factset.wd108.myworkdayjobs.com/FactSetCareers/job/1"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "workday_auth_failure.json").write_text(
        json.dumps(
            {
                "status": "auth_unknown",
                "auth_state": "create_account_gate",
                "message": "Workday never reached the form after sign in, password reset, and create account.",
            }
        ),
        encoding="utf-8",
    )
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "stopped", output_dir=str(out_dir), board="workday")

    resp = client.get("/api/jobs/1/logs")

    assert resp.status_code == 200
    assert "submit/workday_auth_failure.json" in resp.json()["output"]
    assert "auth_unknown" in resp.json()["output"]


def test_logs_endpoint_includes_job_unavailable_artifact(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/acme/jobs/404"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "job_unavailable.json").write_text(
        json.dumps(
            {
                "status": "job_closed",
                "board": "greenhouse",
                "message": "job_closed: Job posting not found (HTTP 404)",
            }
        ),
        encoding="utf-8",
    )
    import job_web

    conn = job_web.get_conn()
    update_status(
        conn, 1, "stopped", output_dir=str(out_dir), board="greenhouse", archived=True, failure_type="job_closed"
    )

    resp = client.get("/api/jobs/1/logs")

    assert resp.status_code == 200
    assert "submit/job_unavailable.json" in resp.json()["output"]
    assert "job_closed" in resp.json()["output"]


def test_reanswer_endpoint_marks_answer_refresh_pending(client, tmp_path):
    from answer_refresh_state import load_answer_refresh_state
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/reanswer"]})
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir))

    resp = client.post("/api/jobs/1/reanswer")

    assert resp.status_code == 200
    state = load_answer_refresh_state(out_dir)
    assert state["status"] == "pending"
    assert state["request_kind"] == "reanswer"


def test_restart_pipeline_marks_answer_refresh_pending(client, tmp_path):
    from answer_refresh_state import load_answer_refresh_state
    from job_db import get_job_timeline, update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/restart"]})
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir))

    resp = client.post(
        "/api/jobs/1/restart-pipeline",
        json={"auto_submit": False},
        headers={
            "X-Jobapps-Action-Surface": "queue",
            "X-Jobapps-Action-Trigger": "bulk",
            "X-Jobapps-Request-Id": "req-restart-1",
        },
    )

    assert resp.status_code == 200
    state = load_answer_refresh_state(out_dir)
    assert state["status"] == "pending"
    assert state["request_kind"] == "restart_pipeline"
    events = get_job_timeline(conn, 1)
    restart_event = next(ev for ev in events if ev["event_type"] == "pipeline_restarted")
    status_event = next(ev for ev in events if ev["event_type"] == "status_change" and ev["detail"] == "queued")
    expected = {
        "action": {
            "surface": "queue",
            "trigger": "bulk",
            "request_id": "req-restart-1",
            "route": "/api/jobs/1/restart-pipeline",
        }
    }
    assert json.loads(restart_event["detail_json"]) == expected
    assert json.loads(status_event["detail_json"]) == expected


def test_restart_pipeline_clears_stale_current_draft_proof_artifacts(client, tmp_path):
    from answer_refresh_state import load_answer_refresh_state
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://apply.workable.com/j/restart-proof"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)

    payload_path = submit_dir / "workable_autofill_payload.json"
    report_path = submit_dir / "workable_autofill_report.json"
    screenshot_path = submit_dir / "workable_autofill_pre_submit.png"
    unknown_questions_path = submit_dir / "workable_unknown_questions.json"
    application_page_path = submit_dir / "workable_application_page.html"
    pending_input_path = submit_dir / "pending_user_input.json"
    result_path = submit_dir / "application_submission_result.json"
    confirmation_website_path = submit_dir / "application_confirmation_website.json"
    confirmation_email_path = submit_dir / "application_confirmation_email.json"
    confirmation_reply_path = submit_dir / "confirmation_email_reply.json"
    notion_sync_path = submit_dir / "notion_sync_status.json"

    report_path.write_text(json.dumps({"fields": []}), encoding="utf-8")
    screenshot_path.write_bytes(b"png")
    unknown_questions_path.write_text(json.dumps({"questions": []}), encoding="utf-8")
    application_page_path.write_text("<html></html>", encoding="utf-8")
    pending_input_path.write_text(json.dumps({"status": "pending_user_input", "questions": []}), encoding="utf-8")
    result_path.write_text(json.dumps({"status": "unknown"}), encoding="utf-8")
    confirmation_website_path.write_text(json.dumps({"status": "submitted"}), encoding="utf-8")
    confirmation_email_path.write_text(json.dumps({"status": "sent"}), encoding="utf-8")
    confirmation_reply_path.write_text(json.dumps({"status": "replied"}), encoding="utf-8")
    notion_sync_path.write_text(json.dumps({"status": "synced"}), encoding="utf-8")
    payload_path.write_text(
        json.dumps(
            {
                "artifacts": {
                    "payload_path": str(payload_path),
                    "report_json": str(report_path),
                    "pre_submit_screenshot": str(screenshot_path),
                    "unknown_questions_json": str(unknown_questions_path),
                    "application_page_html": str(application_page_path),
                }
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "draft_status.json").write_text(
        json.dumps({"status": "awaiting_review", "draft_review_state": {"state": "ready"}}),
        encoding="utf-8",
    )
    (out_dir / "draft_summary.md").write_text("# summary", encoding="utf-8")
    (out_dir / "draft_summary.original.md").write_text("# summary", encoding="utf-8")
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")

    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="workable")

    before = client.get("/api/jobs/1/content/autofill_pre_submit.png")
    assert before.status_code == 200

    resp = client.post("/api/jobs/1/restart-pipeline", json={"auto_submit": False})

    assert resp.status_code == 200
    assert not screenshot_path.exists()
    assert not report_path.exists()
    assert not pending_input_path.exists()
    assert not result_path.exists()
    assert not confirmation_website_path.exists()
    assert not confirmation_email_path.exists()
    assert not confirmation_reply_path.exists()
    assert not notion_sync_path.exists()
    assert not (out_dir / "draft_status.json").exists()
    assert not (out_dir / "draft_summary.md").exists()
    assert not (out_dir / "draft_summary.original.md").exists()
    assert not (out_dir / "draft_summary.png").exists()
    assert payload_path.exists()
    assert unknown_questions_path.exists()
    assert application_page_path.exists()

    after = client.get("/api/jobs/1/content/autofill_pre_submit.png")
    assert after.status_code == 404

    state = load_answer_refresh_state(out_dir)
    assert state["status"] == "pending"
    assert state["request_kind"] == "restart_pipeline"


def test_locked_restart_pipeline_returns_409(client, tmp_path):
    from answer_refresh_state import load_answer_refresh_state
    from job_db import get_job

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/locked-restart"]})
    import job_web

    conn = job_web.get_conn()
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    conn.execute(
        "UPDATE jobs SET status = 'submitted', output_dir = ?, confirmed_at = ?, submission_lock_state = 'locked' "
        "WHERE id = 1",
        (str(out_dir), "2026-03-18T17:11:18+00:00"),
    )
    conn.commit()

    resp = client.post("/api/jobs/1/restart-pipeline", json={"auto_submit": False})

    assert resp.status_code == 409
    assert "Unlock it before redrafting or resubmitting" in resp.text
    assert get_job(conn, 1)["status"] == "submitted"
    assert load_answer_refresh_state(out_dir)["status"] == "unknown"


def test_locked_reanswer_returns_409(client, tmp_path):
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/locked-reanswer"]})
    import job_web

    conn = job_web.get_conn()
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    conn.execute(
        "UPDATE jobs SET status = 'submitted', output_dir = ?, confirmed_at = ?, submission_lock_state = 'locked' "
        "WHERE id = 1",
        (str(out_dir), "2026-03-18T17:11:18+00:00"),
    )
    conn.commit()

    resp = client.post("/api/jobs/1/reanswer")

    assert resp.status_code == 409


def test_locked_regenerate_asset_returns_409_without_priority_mutation(client, tmp_path):
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/locked-regenerate-asset"]})
    import job_web

    conn = job_web.get_conn()
    out_dir = tmp_path / "job-output"
    (out_dir / "content").mkdir(parents=True)
    (out_dir / "documents").mkdir()
    conn.execute(
        "UPDATE jobs SET status = 'submitted', output_dir = ?, confirmed_at = ?, submission_lock_state = 'locked', "
        "priority = 7 WHERE id = 1",
        (str(out_dir), "2026-03-18T17:11:18+00:00"),
    )
    conn.commit()

    resp = client.post("/api/jobs/1/regenerate-asset", json={"target": "resume"})

    row = conn.execute("SELECT status, priority FROM jobs WHERE id = 1").fetchone()
    assert resp.status_code == 409
    assert row["status"] == "submitted"
    assert row["priority"] == 7


def test_locked_board_url_returns_409_without_mutation(client):
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/locked-board-url"]})
    import job_web

    conn = job_web.get_conn()
    before = conn.execute("SELECT board_url, canonical_url FROM jobs WHERE id = 1").fetchone()
    conn.execute(
        "UPDATE jobs SET status = 'submitted', confirmed_at = ?, submission_lock_state = 'locked' WHERE id = 1",
        ("2026-03-18T17:11:18+00:00",),
    )
    conn.commit()

    resp = client.post("/api/jobs/1/board-url", json={"url": "https://boards.greenhouse.io/co/jobs/manual"})

    row = conn.execute("SELECT status, board_url, canonical_url FROM jobs WHERE id = 1").fetchone()
    assert resp.status_code == 409
    assert row["status"] == "submitted"
    assert row["board_url"] == before["board_url"]
    assert row["canonical_url"] == before["canonical_url"]


def test_unlock_flow_endpoint_allows_restart_after_unlock(client, tmp_path):
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/unlock-flow"]})
    import job_web

    conn = job_web.get_conn()
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps({"status": "submitted"}),
        encoding="utf-8",
    )
    (submit_dir / "application_confirmation_website.json").write_text(
        json.dumps({"status": "submitted"}),
        encoding="utf-8",
    )
    (submit_dir / "application_confirmation_email.json").write_text(
        json.dumps({"status": "sent"}),
        encoding="utf-8",
    )
    conn.execute(
        "UPDATE jobs SET status = 'submitted', output_dir = ?, confirmed_at = ?, submission_lock_state = 'locked' "
        "WHERE id = 1",
        (str(out_dir), "2026-03-18T17:11:18+00:00"),
    )
    conn.commit()

    unlock_resp = client.post("/api/jobs/1/unlock-resubmit")
    restart_resp = client.post("/api/jobs/1/restart-pipeline", json={"auto_submit": False})

    row = conn.execute("SELECT status, submission_lock_state FROM jobs WHERE id = 1").fetchone()
    assert unlock_resp.status_code == 200
    assert unlock_resp.json()["status"] == "unlocked_for_resubmit"
    assert restart_resp.status_code == 200
    assert row["status"] == "queued"
    assert row["submission_lock_state"] == "unlocked_for_resubmit"
    assert not (submit_dir / "application_submission_result.json").exists()
    assert not (submit_dir / "application_confirmation_website.json").exists()
    assert not (submit_dir / "application_confirmation_email.json").exists()


def test_relock_flow_endpoint_locks_unlocked_job(client, tmp_path):
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/relock-flow"]})
    import job_web

    conn = job_web.get_conn()
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    conn.execute(
        "UPDATE jobs SET status = 'submitted', output_dir = ?, confirmed_at = ?, submission_lock_state = 'unlocked_for_resubmit' "
        "WHERE id = 1",
        (str(out_dir), "2026-03-18T17:11:18+00:00"),
    )
    conn.commit()

    relock_resp = client.post("/api/jobs/1/lock-resubmit")

    row = conn.execute("SELECT status, submission_lock_state FROM jobs WHERE id = 1").fetchone()
    assert relock_resp.status_code == 200
    assert relock_resp.json()["status"] == "locked"
    assert row["status"] == "submitted"
    assert row["submission_lock_state"] == "locked"


def test_reset_to_new_endpoint_requeues_draft_job(client, tmp_path):
    from answer_refresh_state import load_answer_refresh_state

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/reset-to-new"]})
    import job_web

    conn = job_web.get_conn()
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    conn.execute(
        "UPDATE jobs SET status = 'draft', board = 'greenhouse', output_dir = ?, provider = ?, "
        "progress = ?, error_message = ? WHERE id = 1",
        (str(out_dir), "openai", "Draft ready", "Needs reset"),
    )
    conn.commit()

    resp = client.post("/api/jobs/1/reset-to-new")

    row = conn.execute(
        "SELECT status, provider, progress, error_message FROM jobs WHERE id = 1"
    ).fetchone()
    assert resp.status_code == 200
    assert resp.json() == {"status": "queued"}
    assert row["status"] == "queued"
    assert row["provider"] is None
    assert row["progress"] == ""
    assert row["error_message"] == ""
    assert load_answer_refresh_state(out_dir)["request_kind"] == "reset_to_new"


def test_delete_job(client):
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/1"]})
    resp = client.delete("/api/jobs/1")
    assert resp.status_code == 200
    resp = client.get("/api/jobs/1")
    assert resp.status_code == 404


def test_worker_status(client):
    with mock.patch("job_web.is_worker_running", return_value=False):
        resp = client.get("/api/workers/status")
        assert resp.status_code == 200
        assert resp.json()["running"] is False


def test_main_applies_workers_flag_to_configured_worker_count(monkeypatch):
    import job_web

    monkeypatch.setattr(job_web, "_configured_num_workers", 99)
    monkeypatch.setattr(sys, "argv", ["job_web.py", "--workers", "16"])
    monkeypatch.setattr(job_web, "_kill_port", lambda _port: None)
    monkeypatch.setattr(job_web, "create_app", lambda: object())
    monkeypatch.setattr(job_web, "close_all_connections", lambda: None)
    monkeypatch.setattr(job_web.sys, "exit", lambda _code=0: None)
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=lambda *_args, **_kwargs: None))

    job_web.main()

    assert job_web._configured_num_workers == 16


def test_worker_status_reports_repair_supervisor_running(client):
    with (
        mock.patch("job_web.is_worker_running", return_value=False),
        mock.patch("job_web.is_repair_supervisor_running", return_value=True),
    ):
        resp = client.get("/api/workers/status")

    assert resp.status_code == 200
    assert resp.json()["repair_supervisor_running"] is True


def test_worker_status_reports_actual_repair_supervisor_runtime_when_flag_disabled(client):
    with (
        mock.patch("job_web.is_worker_running", return_value=False),
        mock.patch("job_web.is_repair_supervisor_running", return_value=True),
    ):
        resp = client.get("/api/workers/status")

    assert resp.status_code == 200
    assert resp.json()["repair_supervisor_running"] is True


def test_worker_status_reports_repair_queue_pause_state(client):
    with (
        mock.patch("job_web.is_worker_running", return_value=False),
        mock.patch("job_web.is_repair_supervisor_running", return_value=True),
        mock.patch(
            "job_web.get_repair_queue_pause",
            return_value={"rollout_id": 7, "reason": "fingerprint_recurred"},
        ),
    ):
        resp = client.get("/api/workers/status")

    assert resp.status_code == 200
    assert resp.json()["repair_queue_paused"] is True
    assert resp.json()["repair_queue_pause"]["rollout_id"] == 7


def test_websocket_initial_worker_status_includes_repair_supervisor_state(client):
    with (
        mock.patch("job_web.is_worker_running", return_value=False),
        mock.patch("job_web.is_repair_supervisor_running", return_value=True),
    ):
        with client.websocket_connect("/ws") as ws:
            bulk = ws.receive_json()
            status = ws.receive_json()

    assert bulk["type"] == "job_bulk"
    assert status["type"] == "worker_status"
    assert status["repair_supervisor_running"] is True


def test_websocket_initial_worker_status_includes_repair_queue_pause(client):
    with (
        mock.patch("job_web.is_worker_running", return_value=False),
        mock.patch("job_web.is_repair_supervisor_running", return_value=True),
        mock.patch(
            "job_web.get_repair_queue_pause",
            return_value={"rollout_id": 9, "reason": "unexpected_hard_failures"},
        ),
    ):
        with client.websocket_connect("/ws") as ws:
            _bulk = ws.receive_json()
            status = ws.receive_json()

    assert status["repair_queue_paused"] is True
    assert status["repair_queue_pause"]["rollout_id"] == 9


@pytest.mark.anyio
async def test_broadcast_changes_includes_repair_supervisor_state(monkeypatch):
    import job_web

    messages: list[dict] = []
    sleep_calls = {"count": 0}

    class FakeConn:
        def execute(self, *_args, **_kwargs):
            class Result:
                def fetchall(self_nonlocal):
                    return []

            return Result()

    async def fake_broadcast(message: dict):
        messages.append(message)

    async def fake_sleep(_seconds: float):
        sleep_calls["count"] += 1
        if sleep_calls["count"] == 1:
            return
        raise asyncio.CancelledError

    monkeypatch.setattr(job_web, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(job_web, "get_repair_queue_pause", lambda _conn: None)
    monkeypatch.setattr(job_web, "is_worker_running", lambda: False)
    monkeypatch.setattr(job_web, "is_repair_supervisor_running", lambda **_: True)
    monkeypatch.setattr(job_web.manager, "active", [object()])
    monkeypatch.setattr(job_web.manager, "get_changed_jobs", lambda _conn: [])
    monkeypatch.setattr(job_web.manager, "broadcast", fake_broadcast)
    monkeypatch.setattr(job_web.asyncio, "sleep", fake_sleep)

    await job_web._broadcast_changes()

    worker_status = next(message for message in messages if message["type"] == "worker_status")
    assert worker_status["repair_supervisor_running"] is True
    assert worker_status["repair_queue_paused"] is False
    assert worker_status["repair_queue_pause"] is None


def test_stats_summary(client):
    resp = client.get("/api/stats/summary")
    assert resp.status_code == 200
    assert "total" in resp.json()


def test_stats_phases(client):
    resp = client.get("/api/stats/phases")
    assert resp.status_code == 200


def test_stats_processed(client):
    resp = client.get("/api/stats/processed")
    assert resp.status_code == 200
    assert "all_time" in resp.json()


def test_websocket_sends_initial_bulk(client):
    # Add a job first
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/1"]})
    with client.websocket_connect("/ws") as ws:
        data = ws.receive_json()
        assert data["type"] == "job_bulk"
        assert len(data["jobs"]) >= 1


def test_websocket_initial_bulk_includes_queue_review_summary(client):
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/ws-summary"]})

    with client.websocket_connect("/ws") as ws:
        bulk = ws.receive_json()

    assert bulk["type"] == "job_bulk"
    assert "queue_review_summary" in bulk["jobs"][0]
    assert "visible_actions" in bulk["jobs"][0]["queue_review_summary"]


def _seed_large_queue_with_older_terminal_rows(client):
    import job_web
    from job_db import add_job

    conn = job_web.get_conn()
    for idx in range(503):
        add_job(conn, f"https://boards.greenhouse.io/co/jobs/{idx}")

    conn.execute("DROP TRIGGER IF EXISTS trg_jobs_updated_at")
    conn.execute(
        "UPDATE jobs SET status = 'submitted', updated_at = '2026-03-30 10:00:00' WHERE id = 1"
    )
    conn.execute(
        "UPDATE jobs SET status = 'draft', updated_at = '2026-03-30 10:00:01' WHERE id = 2"
    )
    conn.execute(
        "UPDATE jobs SET status = 'stopped', updated_at = '2026-03-30 10:00:02' WHERE id = 3"
    )
    conn.execute(
        "UPDATE jobs SET updated_at = '2026-03-30 11:00:00' WHERE id >= 4"
    )
    conn.commit()


def test_queue_endpoint_reports_counts_beyond_latest_500(client):
    _seed_large_queue_with_older_terminal_rows(client)

    resp = client.get("/api/queue")

    assert resp.status_code == 200
    data = resp.json()
    assert data["counts"]["all"] == 503
    assert data["counts"]["queued"] == 500
    assert data["counts"]["submitted"] == 1
    assert data["counts"]["draft"] == 1
    assert data["counts"]["stopped"] == 1


def test_websocket_changed_jobs_include_submission_history_flags(client):
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/1"]})
    import job_web

    conn = job_web.get_conn()
    conn.execute(
        "UPDATE jobs SET status = 'submitted', confirmed_at = ?, submission_lock_state = 'locked' WHERE id = 1",
        ("2026-03-18T17:11:18+00:00",),
    )
    conn.commit()

    manager = job_web.ConnectionManager()
    changed = manager.get_changed_jobs(conn)

    assert len(changed) == 1
    assert changed[0]["id"] == 1
    assert changed[0]["previously_submitted"] is True
    assert changed[0]["submission_lock_state"] == "locked"


def test_websocket_changed_jobs_only_enrich_changed_rows(client, monkeypatch):
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/1"]})
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/2"]})
    import job_web

    conn = job_web.get_conn()
    conn.execute("DROP TRIGGER IF EXISTS trg_jobs_updated_at")
    conn.execute("UPDATE jobs SET updated_at = '2026-03-30 10:00:00' WHERE id = 1")
    conn.execute("UPDATE jobs SET updated_at = '2026-03-30 10:00:01' WHERE id = 2")
    conn.commit()

    manager = job_web.ConnectionManager()
    original_enrich = job_web._enrich_queue_rows
    seen_batches: list[list[int]] = []

    def spy_enrich(sync_conn, jobs):
        seen_batches.append([int(job["id"]) for job in jobs])
        return original_enrich(sync_conn, jobs)

    monkeypatch.setattr(job_web, "_enrich_queue_rows", spy_enrich)

    first = manager.get_changed_jobs(conn)
    assert {job["id"] for job in first if not job.get("_deleted")} == {1, 2}

    seen_batches.clear()
    conn.execute("UPDATE jobs SET updated_at = '2026-03-30 10:00:02' WHERE id = 1")
    conn.commit()

    changed = manager.get_changed_jobs(conn)

    assert [job["id"] for job in changed if not job.get("_deleted")] == [1]
    assert seen_batches == [[1]]


def test_queue_endpoint_fetches_requested_status_beyond_latest_500(client):
    _seed_large_queue_with_older_terminal_rows(client)

    resp = client.get("/api/queue?status=submitted&limit=50")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert [job["id"] for job in data["jobs"]] == [1]
    assert data["jobs"][0]["status"] == "submitted"


def test_app_js_uses_server_side_queue_endpoint(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "/api/queue" in resp.text


def test_full_workflow(client):
    """Add job → check queue → get detail → approve."""
    # Add
    resp = client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/1"]})
    assert resp.json()["added"] == 1

    # List
    resp = client.get("/api/jobs")
    assert len(resp.json()) == 1

    # Detail
    resp = client.get("/api/jobs/1")
    assert resp.json()["status"] == "queued"

    # Stats
    resp = client.get("/api/stats/summary")
    assert resp.json()["total"] >= 1

    # Worker status
    with mock.patch("job_web.is_worker_running", return_value=True):
        resp = client.get("/api/workers/status")
        assert resp.json()["running"] is True

    # Recent events
    resp = client.get("/api/events/recent")
    assert resp.status_code == 200

    # Status counts
    resp = client.get("/api/stats/counts")
    assert resp.status_code == 200
    assert resp.json().get("queued", 0) >= 1


# ---------------------------------------------------------------------------
# Connection registry tests
# ---------------------------------------------------------------------------


def test_close_all_connections():
    """close_all_connections (now in job_db) closes all tracked connections."""
    import job_db

    original = job_db._connections.copy()
    job_db._connections.clear()

    conn1 = sqlite3.connect(":memory:")
    conn2 = sqlite3.connect(":memory:")
    with job_db._conn_lock:
        job_db._connections.add(conn1)
        job_db._connections.add(conn2)

    job_db.close_all_connections()

    # Verify connections are closed (executing on closed connection raises ProgrammingError)
    with pytest.raises(sqlite3.ProgrammingError):
        conn1.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn2.execute("SELECT 1")

    # Registry should be empty
    assert len(job_db._connections) == 0

    # Restore original state
    with job_db._conn_lock:
        job_db._connections.update(original)
