/**
 * Service Control - Frontend JavaScript
 *
 * Reads the unified GET /api/service-control envelope (ATHENA-118): every
 * row carries a server-resolved manager (Control Agent / Kubernetes / none),
 * a per-user-gated action list, and D17's server-computed group -- this file
 * never re-derives running/stopped or group membership from the row name.
 */

// State
let serviceControl = null; // full GET /api/service-control envelope
let ollamaHealth = null;   // null while loading
let ollamaModels = null;   // null while loading / on error
let ollamaModelsError = null;
let restartHistory = [];
let restartHistoryError = null; // 'permission' when /api/audit 403s

// Service dependency graph -- warning copy shown in the confirm modal.
const SERVICE_DEPENDENCIES = {
    'gateway': {
        dependents: ['orchestrator', 'voice-pipelines'],
        warning: 'Stopping Gateway will disconnect all voice clients and API consumers.'
    },
    'orchestrator': {
        dependents: ['rag-services'],
        warning: 'Stopping Orchestrator will halt all AI query processing.'
    },
    'ollama': {
        dependents: ['orchestrator'],
        warning: 'Stopping Ollama will disable all local LLM inference.'
    },
    'qdrant': {
        dependents: ['orchestrator'],
        warning: 'Stopping Qdrant will disable RAG vector search.'
    },
    'redis': {
        dependents: ['gateway', 'orchestrator'],
        warning: 'Stopping Redis will clear session cache and rate limiting state.'
    }
};

const ACTION_LABELS = { start: 'Start', stop: 'Stop', restart: 'Restart' };
const ACTION_CLASSES = {
    start: 'bg-green-600 hover:bg-green-700',
    stop: 'bg-red-600 hover:bg-red-700',
    restart: 'bg-yellow-600 hover:bg-yellow-700',
};
const ACTION_PAST_TENSE = { service_start: 'Started', service_stop: 'Stopped', service_restart: 'Restarted' };

// ============================================================================
// Initialization
// ============================================================================

async function loadServiceControl() {
    await Promise.all([
        loadServices(),
        refreshOllamaPanel(),
        refreshHistoryPanel(),
    ]);
}

// ============================================================================
// Data Loading
// ============================================================================

/**
 * Bare fetch + JSON parse that preserves the HTTP status and parsed body on
 * failure (err.status / err.payload) -- the shared apiRequest() stringifies
 * everything into an Error message, which loses the distinction between a
 * 403 (permission) and any other failure. Used wherever that distinction
 * matters (restart history's 403 handling).
 */
async function _getJson(url) {
    const response = await fetch(url, { headers: getAuthHeaders(), credentials: 'same-origin' });
    let payload = null;
    try {
        payload = await response.json();
    } catch (_e) {
        payload = null;
    }
    if (!response.ok) {
        const err = new Error(`HTTP ${response.status}`);
        err.status = response.status;
        err.payload = payload;
        throw err;
    }
    return payload;
}

async function loadServices() {
    try {
        serviceControl = await apiRequest('/api/service-control');
        renderServiceControlBanners();
        renderServiceControlTables();
        updateServiceControlCounts();
    } catch (error) {
        console.error('Failed to load service control data:', error);
        showServiceLoadError();
    }
}

// loadRagServicesFromRegistry is now a thin alias -- RAG rows live in the
// same unified envelope as every other row (D3/D17). Kept under its
// original name because it's still called from a few reload sites below.
async function loadRagServicesFromRegistry() {
    return loadServices();
}

async function refreshServiceStatus(btn) {
    // Accept either an element argument or fall back to event.target.
    // Passing the element explicitly prevents timing issues with async event
    // dispatch (event.target may be null by the time the await resolves).
    const button = btn || (typeof event !== 'undefined' ? event.target : null);
    if (button) {
        button.disabled = true;
        button.innerHTML = '<i data-lucide="loader" class="w-4 h-4 inline-block mr-1 animate-spin"></i> Refreshing…';
        if (typeof lucide !== 'undefined') lucide.createIcons();
    }

    try {
        const summary = await apiRequest('/api/service-registry/services/poll-now', { method: 'POST' });
        showToast(
            `Health check complete: ${summary.healthy || 0} healthy, ${summary.unhealthy || 0} unhealthy`,
            'success',
        );
        await loadServices();
    } catch (error) {
        showToast(`Refresh failed: ${error.message}`, 'error');
    } finally {
        if (button) {
            button.disabled = false;
            button.innerHTML = '<i data-lucide="refresh-cw" class="w-4 h-4 inline-block mr-1"></i> Refresh Status';
            if (typeof lucide !== 'undefined') lucide.createIcons();
        }
    }
}

// ============================================================================
// D18 -- manager degraded-mode banners / neutral note
// ============================================================================

const K8S_REASON_TEXT = {
    not_in_cluster: 'the admin-backend process is not running inside a Kubernetes pod',
    no_service_account_token: 'the admin-backend pod has no service-account token mounted — re-run the RBAC automount patch',
    forbidden: 'the service account lacks scale permission',
    not_found: 'the target Deployment was not found',
    conflict: 'the Kubernetes API reported a conflict',
    api_error: 'the Kubernetes API returned an error',
    unavailable: 'the Kubernetes API is unreachable',
};

function k8sReasonText(reason) {
    return K8S_REASON_TEXT[reason] || `an unexpected error (${reason})`;
}

const DOCS_POINTER = 'See docs/CONFIGURATION.md § Service Control on Kubernetes.';

function renderServiceControlBanners() {
    const container = document.getElementById('service-control-banners');
    if (!container || !serviceControl) return;

    const banners = [];
    const k8s = serviceControl.kubernetes;
    const ca = serviceControl.control_agent;

    const k8sDegraded = !!(k8s && k8s.enabled && !k8s.available);
    const caUnreachable = !!(ca && ca.enabled && !ca.reachable);
    const caDockerDown = !!(ca && ca.enabled && ca.reachable && ca.note === 'control_agent_docker_unavailable');

    if (k8sDegraded) {
        banners.push(`
            <div class="bg-amber-500/10 border border-amber-500/40 rounded-lg px-4 py-3 text-sm text-amber-300">
                Kubernetes control is enabled but unavailable — ${escapeHtml(k8sReasonText(k8s.reason))}. ${escapeHtml(DOCS_POINTER)}
            </div>
        `);
    }
    if (caUnreachable) {
        banners.push(`
            <div class="bg-amber-500/10 border border-amber-500/40 rounded-lg px-4 py-3 text-sm text-amber-300">
                Control Agent is enabled but unreachable — check that it's running and that CONTROL_AGENT_URL points at it. ${escapeHtml(DOCS_POINTER)}
            </div>
        `);
    } else if (caDockerDown) {
        // Low L3: CA reachable but Docker unavailable had no banner at all --
        // container-managed rows silently became "Managed externally".
        banners.push(`
            <div class="bg-amber-500/10 border border-amber-500/40 rounded-lg px-4 py-3 text-sm text-amber-300">
                Control Agent is reachable, but Docker is unavailable on that host — container-managed services can't be controlled from here.
            </div>
        `);
    }
    if (!k8sDegraded && !caUnreachable && !(ca && ca.enabled) && !(k8s && k8s.enabled)) {
        banners.push(`
            <div class="bg-gray-700/40 border border-dark-border rounded-lg px-4 py-3 text-sm text-gray-400">
                Start/stop/restart need the Control Agent or Kubernetes control; neither is enabled.
            </div>
        `);
    }

    container.innerHTML = banners.join('');
}

// ============================================================================
// Rendering -- grouping and per-row cells
// ============================================================================

/**
 * Bucket rows by the server-computed `row.group` (D17) -- 'core', 'rag', or
 * 'infrastructure'. This is a THIN reader: it must never re-derive group
 * membership from the row's name/host, which would silently reopen the
 * "RAG row rendered in Core" bug D17 exists to close.
 */
function groupServiceControlRows(rows) {
    const groups = { core: [], rag: [], infrastructure: [] };
    for (const row of rows || []) {
        const key = Object.prototype.hasOwnProperty.call(groups, row.group) ? row.group : 'core';
        groups[key].push(row);
    }
    return groups;
}

const RUN_STATE_BADGES = {
    running: { label: 'Running', cls: 'bg-green-900 text-green-300', dot: '●' },
    stopped: { label: 'Stopped', cls: 'bg-red-900 text-red-300', dot: '●' },
    disabled: { label: 'Disabled', cls: 'bg-gray-700 text-gray-400', dot: '○' },
    unhealthy: { label: 'Unhealthy', cls: 'bg-amber-900 text-amber-300', dot: '●' },
};

/**
 * True/false when the row's native evidence (k8s replica count, or a
 * native_state string) says the underlying process/pod is actually up,
 * null when there's no such evidence at all. Used to distinguish "stopped
 * because actually stopped" from "stopped because unhealthy but the pod is
 * still up" (ruby H2) -- run_state alone conflates the two.
 */
function _isNativelyRunning(row) {
    if (row.k8s_replicas != null) return row.k8s_replicas > 0;
    if (typeof row.native_state === 'string' && row.native_state) {
        if (/\brunning\b|\bup\b/i.test(row.native_state)) return true;
        if (/\bstopped\b/i.test(row.native_state)) return false;
    }
    return null;
}

/**
 * One health vocabulary for every group (core/infra/RAG) -- Healthy /
 * Unhealthy / Needs setup / Checking… -- so the page stops speaking two
 * different status languages (ruby H2 knock-on).
 */
function _healthLine(row) {
    if (row.run_state === 'disabled') return null;
    switch (row.health_status) {
        case 'healthy':
            return { text: 'Healthy', cls: 'text-green-400' };
        case 'unconfigured':
            return { text: 'Needs setup', cls: 'text-amber-400' };
        case 'unhealthy':
        case 'degraded':
            return { text: 'Unhealthy', cls: 'text-red-400' };
        case 'pending':
        case null:
        case undefined:
            return { text: 'Checking…', cls: 'text-gray-400' };
        default:
            return null;
    }
}

/**
 * Pure: run-state badge + health line + native_state (D16) + last_error.
 * Shared by core/infrastructure rows AND RAG rows (ruby H2) -- one status
 * vocabulary for the whole page.
 */
function renderStatusCell(row) {
    let info = RUN_STATE_BADGES[row.run_state] || RUN_STATE_BADGES.stopped;
    if (row.run_state === 'stopped' && _isNativelyRunning(row) === true) {
        // The manager says the process/pod is still up even though health
        // says "stopped" -- read as Unhealthy, not a contradictory "Stopped
        // · 1/1 pods" (ruby H2).
        info = RUN_STATE_BADGES.unhealthy;
    }
    const badge = `<span class="px-2 py-1 text-xs rounded ${escapeHtml(info.cls)}">${info.dot} ${escapeHtml(info.label)}</span>`;
    const health = _healthLine(row);
    const healthLine = health
        ? `<div class="text-xs ${escapeHtml(health.cls)} mt-1">${escapeHtml(health.text)}</div>`
        : '';
    const nativeState = row.native_state
        ? `<div class="text-xs text-gray-500 mt-1">${escapeHtml(String(row.native_state))}</div>`
        : '';
    const errorDetail = row.last_error
        ? `<div class="text-xs text-red-400 mt-1">${escapeHtml(String(row.last_error).substring(0, 100))}</div>`
        : '';
    return `<div>${badge}${healthLine}${nativeState}${errorDetail}</div>`;
}

// ---------------------------------------------------------------------------
// Manager badge + human-readable manager_note text (ruby H3/H4).
// ---------------------------------------------------------------------------

function _managerNoteInfo(note) {
    if (!note) return { label: 'Unmanaged', sentence: null };
    if (note === 'protected') {
        return { label: 'Protected', sentence: 'This service can never be controlled from this page.' };
    }
    if (note === 'managed_externally') {
        return { label: 'Managed externally', sentence: null };
    }
    if (note === 'control_agent_unreachable') {
        return { label: 'Control Agent unreachable', sentence: 'Control Agent unreachable.' };
    }
    if (note === 'control_agent_docker_unavailable') {
        return { label: 'Docker unavailable', sentence: 'Control Agent is reachable, but Docker is unavailable on that host.' };
    }
    if (note === 'requires_owner') {
        return { label: 'Owner only', sentence: 'Only an owner can control this service (manage_infrastructure).' };
    }
    if (note === 'restart_interrupted') {
        return { label: 'Restart interrupted', sentence: 'A restart was interrupted and left this at 0 replicas. Press Start to bring it back.' };
    }
    if (note.startsWith('target_collision:')) {
        const target = note.slice('target_collision:'.length);
        return { label: 'Conflict', sentence: `Two registry rows point at Deployment ${target}; fix the registry so only one does.` };
    }
    if (note.startsWith('no_deployment:')) {
        const target = note.slice('no_deployment:'.length);
        return { label: 'No Deployment', sentence: `No Deployment named ${target}.` };
    }
    if (note.startsWith('kubernetes_unavailable:')) {
        const reason = note.slice('kubernetes_unavailable:'.length);
        return { label: 'Kubernetes unavailable', sentence: `Kubernetes unavailable (${k8sReasonText(reason)}).` };
    }
    return { label: 'Unmanaged', sentence: note };
}

function _managerBadgeLabel(row) {
    if (row.manager === 'control_agent') return 'Control Agent';
    if (row.manager === 'kubernetes') return 'Kubernetes';
    return _managerNoteInfo(row.manager_note).label;
}

/**
 * The raw code is now only a supplementary title= tooltip (ruby H3) -- the
 * human label is the badge text itself.
 */
function _managerBadge(row) {
    const label = _managerBadgeLabel(row);
    const cls = row.manager === 'control_agent'
        ? 'bg-blue-900 text-blue-300'
        : row.manager === 'kubernetes'
            ? 'bg-purple-900 text-purple-300'
            : 'bg-gray-700 text-gray-400';
    const title = row.manager_note ? ` title="${escapeHtml(row.manager_note)}"` : '';
    return `<span class="px-2 py-0.5 text-xs rounded ${escapeHtml(cls)}"${title}>${escapeHtml(label)}</span>`;
}

/**
 * Visible sentence explaining manager_note (ruby H3) -- rendered as real
 * text under the badge, not only in a tooltip. restart_interrupted (H4)
 * gets its own amber styling since it sits next to the Start button
 * regardless of whether the manager itself resolved successfully.
 */
function _managerNoteSentenceHtml(note) {
    if (!note) return '';
    if (note === 'restart_interrupted') {
        return `<div class="text-xs text-amber-400 mt-1">Restart interrupted — press Start to bring it back.</div>`;
    }
    const info = _managerNoteInfo(note);
    if (!info.sentence) return '';
    return `<div class="text-xs text-gray-400 mt-1">${escapeHtml(info.sentence)}</div>`;
}

/**
 * Pure: manager badge + note sentence + one button per row.actions
 * (server-gated per D20 -- an operator never sees a dead button for a
 * critical target). Action literals come straight from the fixed
 * 'start'/'stop'/'restart' server vocabulary. Each button carries
 * data-service-action="<name>:<action>" so the confirm modal can restore
 * focus to it on close (ruby H1).
 */
function renderServiceActions(row) {
    const badge = _managerBadge(row);
    const buttons = (row.actions || []).map((action) => {
        const label = ACTION_LABELS[action] || action;
        const cls = ACTION_CLASSES[action] || 'bg-gray-600 hover:bg-gray-700';
        return `<button data-service-action="${escapeHtml(row.name)}:${escapeHtml(action)}"
                        onclick="requestServiceAction('${escapeJsAttr(row.name)}', '${escapeJsAttr(action)}')"
                        class="px-2 py-1 text-xs ${escapeHtml(cls)} text-white rounded">${escapeHtml(label)}</button>`;
    }).join('');
    const note = _managerNoteSentenceHtml(row.manager_note);
    return `<div class="flex flex-col gap-1"><div class="flex flex-wrap items-center gap-2">${badge}${buttons}</div>${note}</div>`;
}

function renderServiceControlTables() {
    const groups = groupServiceControlRows(serviceControl ? serviceControl.services : []);
    _renderManagedServiceTable('core-services-table', groups.core);
    _renderManagedServiceTable('infrastructure-services-table', groups.infrastructure);
    renderRagServicesTable(groups.rag);
}

function _renderManagedServiceTable(containerId, rows) {
    const container = document.getElementById(containerId);
    if (!container) return;

    if (!rows || rows.length === 0) {
        container.innerHTML = '<div class="text-center text-gray-400 py-8">No services</div>';
        return;
    }

    const infoIconOr = (key) => (typeof infoIcon === 'function' ? infoIcon(key) : '');

    container.innerHTML = `
        <table class="w-full">
            <thead class="bg-gray-800">
                <tr>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase"><span class="inline-flex items-center gap-1">Service${infoIconOr('service-name')}</span></th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase"><span class="inline-flex items-center gap-1">Endpoint${infoIconOr('service-endpoint')}</span></th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase"><span class="inline-flex items-center gap-1">Status${infoIconOr('service-status')}</span></th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Actions</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-gray-700">
                ${rows.map((row) => `
                    <tr class="hover:bg-gray-800/50">
                        <td class="px-4 py-3">
                            <div class="text-white font-medium">${escapeHtml(row.display_name || row.name)}</div>
                            <div class="text-xs text-gray-500">${escapeHtml(row.description || '')}</div>
                        </td>
                        <td class="px-4 py-3">
                            <div class="text-sm text-gray-300">${escapeHtml(String(row.host || 'unknown'))}:${escapeHtml(String(row.port || ''))}</div>
                        </td>
                        <td class="px-4 py-3">${renderStatusCell(row)}</td>
                        <td class="px-4 py-3">${renderServiceActions(row)}</td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;
}

function updateServiceControlCounts() {
    const counts = (serviceControl && serviceControl.counts) || { running: 0, stopped: 0, disabled: 0 };
    const runningEl = document.getElementById('services-running-count');
    const stoppedEl = document.getElementById('services-stopped-count');
    const disabledEl = document.getElementById('services-disabled-count');
    if (runningEl) runningEl.textContent = counts.running;
    if (stoppedEl) stoppedEl.textContent = counts.stopped;
    if (disabledEl) disabledEl.textContent = counts.disabled;
}

/**
 * Load failure (ruby L5): keep the last-good tables on screen instead of
 * wiping them, surface a Retry affordance, and blank the counts to "—"
 * rather than leaving a stale number.
 */
function showServiceLoadError() {
    const banner = document.getElementById('service-control-banners');
    if (banner) {
        banner.innerHTML = `
            <div class="bg-red-500/10 border border-red-500/40 rounded-lg px-4 py-3 text-sm text-red-300 flex items-center justify-between gap-3">
                <span>Couldn't refresh service status.</span>
                <button onclick="loadServices()" class="px-3 py-1 text-xs bg-red-600 hover:bg-red-700 text-white rounded">Retry</button>
            </div>
        `;
    }
    ['services-running-count', 'services-stopped-count', 'services-disabled-count'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.textContent = '—';
    });
}

// ============================================================================
// RAG Services (a group within the unified envelope, D17) -- keeps its own
// richer freshness chip / aria-live status flips and Disable/Refresh/Edit
// action set, per plan step (e), but shares renderStatusCell's primary
// run-state badge + health vocabulary with core/infra rows (ruby H2).
// ============================================================================

// Status-change tracking for ATHENA-48 toast announcements.
const _lastStatusByService = new Map();

function renderRagServicesTable(ragRows) {
    const container = document.getElementById('rag-services-table');
    if (!container) return;

    const rows = ragRows || [];

    if (rows.length === 0) {
        // Actionable empty-state copy (ruby r1 / plan Phase 4 E).
        container.innerHTML = `
            <div class="text-center text-gray-400 py-8">
                <p class="font-medium text-gray-300 mb-1">No services registered.</p>
                <p class="text-sm">Run <code class="px-1 py-0.5 bg-gray-700 rounded text-xs">alembic upgrade head</code> and restart admin-backend to seed defaults. If running Control Agent, services populate on CA startup.</p>
            </div>
        `;
        return;
    }

    // Status-change announcements (ATHENA-48): one toast per flip, batched if >3 flips in a tick.
    const flips = [];
    for (const s of rows) {
        const name = s.name;
        if (!name) continue;
        const current = String(s.health_status ?? (s.run_state === 'running' ? 'healthy' : 'unhealthy'));
        const prev = _lastStatusByService.get(name);
        if (prev !== undefined && prev !== current) {
            flips.push({ name, from: prev, to: current });
        }
        _lastStatusByService.set(name, current);
    }
    if (flips.length > 0 && window.Athena?.components?.Toast) {
        const Toast = window.Athena.components.Toast;
        const typeFor = (to) => (to === 'healthy' || to === 'running' || to === 'online')
            ? 'success'
            : (to === 'error' || to === 'unreachable' || to === 'down') ? 'error' : 'warning';
        if (flips.length <= 3) {
            for (const f of flips) {
                Toast[typeFor(f.to)](`${f.name}: ${f.from} → ${f.to}`, { duration: 6000 });
            }
        } else {
            Toast.warning(`${flips.length} services changed status`, { duration: 8000 });
        }
    }

    container.innerHTML = `
        <table class="w-full">
            <thead class="bg-gray-800">
                <tr>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Service</th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Endpoint</th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Status</th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Actions</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-gray-700">
                ${rows.map((s) => renderRagServiceRow(s)).join('')}
            </tbody>
        </table>
    `;
}

/**
 * Compute a human-readable "freshness" string and CSS class from a
 * last_health_check ISO timestamp.
 *
 * Thresholds (ruby r1 / plan Phase 4 E):
 *   < 60s     → silent (no chip rendered)
 *   60s–5min  → grey  "checked Xs ago"
 *   5–15min   → amber "stale (Xm)"
 *   > 15min   → red   "stale (>15m)"
 *
 * Returns {text, colorClass} or null when silent (<60s or null timestamp).
 */
function _freshnessChip(lastHealthCheck) {
    if (!lastHealthCheck) return null;
    const ageMs = Date.now() - new Date(lastHealthCheck).getTime();
    if (isNaN(ageMs) || ageMs < 0) return null;
    const ageSec = Math.floor(ageMs / 1000);
    if (ageSec < 60) return null;
    if (ageSec < 300) {
        const label = ageSec < 120 ? '1m' : `${Math.floor(ageSec / 60)}m`;
        return { text: `checked ${label} ago`, colorClass: 'text-gray-500', title: 'How long ago the last health check ran.' };
    }
    if (ageSec < 900) {
        return { text: `stale (${Math.floor(ageSec / 60)}m)`, colorClass: 'text-yellow-500', title: 'Health check hasn\'t run recently — the poller may be stalled.' };
    }
    return { text: 'stale (>15m)', colorClass: 'text-red-500', title: 'Health check hasn\'t run in over 15 minutes — the poller may be stalled.' };
}

/**
 * Build a human-readable aria-label for a service row's status cell.
 * Form: "<ServiceName>: <status>, checked <N> seconds ago[, response <N>ms]."
 */
function _buildStatusAriaLabel(displayName, healthStatus, lastHealthCheck, responseTimeMs) {
    const parts = [`${displayName}: ${healthStatus || 'unknown'}`];
    if (lastHealthCheck) {
        const ageSec = Math.floor((Date.now() - new Date(lastHealthCheck).getTime()) / 1000);
        if (!isNaN(ageSec) && ageSec >= 0) {
            parts.push(`checked ${ageSec} seconds ago`);
        }
    }
    if (responseTimeMs != null) {
        parts.push(`response ${responseTimeMs}ms`);
    }
    return parts.join(', ') + '.';
}

function renderRagServiceRow(service) {
    const displayName = service.display_name || service.name || 'Unknown';
    const host = service.host || 'unknown';
    const port = service.port || 0;

    // Freshness pill — silent when < 60 s old, own tooltip (ruby M5-adjacent
    // chip tooltip ask). (ruby r1 / plan Phase 4 E)
    const chip = _freshnessChip(service.last_health_check);
    const freshnessPill = chip
        ? `<div class="text-xs ${chip.colorClass} mt-1" title="${escapeHtml(chip.title)}">${escapeHtml(chip.text)}</div>`
        : '';

    // last_error / health_message display (category only — URL/IP scrubbed
    // by backend MED-1).
    const healthMessage = service.health_message
        ? `<div class="text-xs text-gray-500 mt-1">${escapeHtml(service.health_message.substring(0, 100))}</div>`
        : '';

    // Accessible label for the status cell. (ruby B3)
    const ariaLabel = _buildStatusAriaLabel(
        displayName,
        service.health_status,
        service.last_health_check,
        service.last_response_time_ms,
    );

    const isEnabled = service.enabled !== false;
    // safeName is for display/attribute positions; rawName feeds the handler
    // attrs below, which escape it themselves at the call site -- wrapping
    // rawName in escapeHtml first would double-escape it (codex r2 F1).
    const rawName = service.name || '';
    const safeName = escapeHtml(rawName);

    // Check-type label (ATHENA-109): a tcp row has no HTTP path to show.
    const checkTypeLabel = service.protocol === 'tcp'
        ? 'TCP'
        : escapeHtml(service.health_endpoint || '/health');

    const managerBadge = _managerBadge(service);
    const managerNote = _managerNoteSentenceHtml(service.manager_note);

    return `
        <tr class="hover:bg-gray-800/50" id="rag-row-${safeName}">
            <td class="px-4 py-3">
                <div class="text-white font-medium">${escapeHtml(displayName)}</div>
                <div class="text-xs text-gray-500">${safeName}</div>
            </td>
            <td class="px-4 py-3">
                <div class="text-sm text-gray-300">${escapeHtml(String(host))}:${escapeHtml(String(port))}</div>
                <div class="text-xs text-gray-500 mt-1">${checkTypeLabel}</div>
                ${healthMessage}
            </td>
            <td class="px-4 py-3"
                aria-live="polite"
                aria-label="${escapeHtml(ariaLabel)}"
                data-status-item="${safeName}">
                ${renderStatusCell(service)}
                ${freshnessPill}
            </td>
            <td class="px-4 py-3">
                <div class="flex flex-col gap-1">
                    <div class="flex flex-wrap items-center gap-2">
                        ${managerBadge}
                        <button onclick="toggleRagService('${escapeJsAttr(rawName)}')"
                                class="px-2 py-1 text-xs ${isEnabled ? 'bg-yellow-600 hover:bg-yellow-700' : 'bg-green-600 hover:bg-green-700'} text-white rounded">
                            ${isEnabled ? 'Disable' : 'Enable'}
                        </button>
                        <button id="check-btn-${safeName}"
                                onclick="checkRagServiceHealth('${escapeJsAttr(rawName)}', this)"
                                class="px-2 py-1 text-xs bg-blue-600 hover:bg-blue-700 text-white rounded">
                            Refresh
                        </button>
                        <button onclick="showEditRagServiceModal('${escapeJsAttr(rawName)}')"
                                class="px-2 py-1 text-xs bg-gray-600 hover:bg-gray-700 text-white rounded">
                            Edit
                        </button>
                    </div>
                    ${managerNote}
                </div>
            </td>
        </tr>
    `;
}

// ============================================================================
// Registry row editor (ATHENA-109) -- lets an operator switch a row's check
// type between http/https (endpoint URL) and tcp (host:port connect only).
// Reuses the existing POST /api/service-registry/services upsert route.
// ============================================================================

function showEditRagServiceModal(serviceName) {
    const service = (serviceControl?.services || []).find(s => s.name === serviceName);
    if (!service) return;

    const isTcp = service.protocol === 'tcp';
    const currentEndpointUrl = service.endpoint_url || (
        service.host && service.port && !isTcp ? `${service.protocol || 'http'}://${service.host}:${service.port}${service.health_endpoint || ''}` : ''
    );

    const modal = `
        <div id="rag-edit-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onclick="if(event.target.id==='rag-edit-modal') closeModal('rag-edit-modal')">
            <div class="bg-dark-card border border-dark-border rounded-lg p-6 max-w-lg w-full mx-4">
                <h2 class="text-xl font-semibold text-white mb-4">Edit Service: ${escapeHtml(service.display_name || service.name)}</h2>
                <form onsubmit="saveRagServiceEdit(event, '${escapeJsAttr(service.name)}')" class="space-y-4">
                    <div>
                        <label for="rag-edit-check-type" class="block text-sm font-medium text-gray-300 mb-1">Check Type</label>
                        <select id="rag-edit-check-type" name="protocol" onchange="_toggleRagEditFields(this.value)"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white focus:outline-none focus:border-blue-500">
                            <option value="http" ${service.protocol === 'http' || !service.protocol ? 'selected' : ''}>HTTP</option>
                            <option value="https" ${service.protocol === 'https' ? 'selected' : ''}>HTTPS</option>
                            <option value="tcp" ${isTcp ? 'selected' : ''}>TCP (raw connect)</option>
                        </select>
                    </div>
                    <div id="rag-edit-url-field" class="${isTcp ? 'hidden' : ''}">
                        <label for="rag-edit-endpoint-url" class="block text-sm font-medium text-gray-300 mb-1">Endpoint URL</label>
                        <input type="text" id="rag-edit-endpoint-url" name="endpoint_url" value="${escapeHtml(currentEndpointUrl)}"
                            placeholder="http://host:port/health"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white focus:outline-none focus:border-blue-500">
                    </div>
                    <div id="rag-edit-tcp-fields" class="${isTcp ? '' : 'hidden'} grid grid-cols-2 gap-3">
                        <div>
                            <label for="rag-edit-host" class="block text-sm font-medium text-gray-300 mb-1">Host</label>
                            <input type="text" id="rag-edit-host" name="host" value="${escapeHtml(service.host || '')}"
                                class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white focus:outline-none focus:border-blue-500">
                        </div>
                        <div>
                            <label for="rag-edit-port" class="block text-sm font-medium text-gray-300 mb-1">Port</label>
                            <input type="number" id="rag-edit-port" name="port" min="1" max="65535" value="${escapeHtml(String(service.port || ''))}"
                                class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white focus:outline-none focus:border-blue-500">
                        </div>
                    </div>
                    <div>
                        <label for="rag-edit-display-name" class="block text-sm font-medium text-gray-300 mb-1">Display Name</label>
                        <input type="text" id="rag-edit-display-name" name="display_name" value="${escapeHtml(service.display_name || '')}"
                            class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white focus:outline-none focus:border-blue-500">
                    </div>
                    <div class="flex gap-2 pt-4">
                        <button type="submit"
                            class="flex-1 px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg font-medium transition-colors">
                            Save
                        </button>
                        <button type="button" onclick="closeModal('rag-edit-modal')"
                            class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg font-medium transition-colors">
                            Cancel
                        </button>
                    </div>
                </form>
            </div>
        </div>
    `;
    document.getElementById('modals-container').innerHTML = modal;
}

function _toggleRagEditFields(protocol) {
    const urlField = document.getElementById('rag-edit-url-field');
    const tcpFields = document.getElementById('rag-edit-tcp-fields');
    if (!urlField || !tcpFields) return;
    const isTcp = protocol === 'tcp';
    urlField.classList.toggle('hidden', isTcp);
    tcpFields.classList.toggle('hidden', !isTcp);
}

async function saveRagServiceEdit(event, serviceName) {
    event.preventDefault();
    const form = event.target;
    const fd = new FormData(form);
    const protocol = fd.get('protocol');

    const params = new URLSearchParams({
        name: serviceName,
        display_name: fd.get('display_name') || '',
        protocol: protocol,
    });

    if (protocol === 'tcp') {
        const host = fd.get('host');
        const port = fd.get('port');
        if (!host || !port) {
            showServiceError('Host and port are required for a TCP check.');
            return;
        }
        params.set('host', host);
        params.set('port', port);
    } else {
        const endpointUrl = fd.get('endpoint_url');
        if (!endpointUrl) {
            showServiceError('Endpoint URL is required for an HTTP(S) check.');
            return;
        }
        params.set('endpoint_url', endpointUrl);
    }

    try {
        await apiRequest(`/api/service-registry/services?${params.toString()}`, { method: 'POST' });
        closeModal('rag-edit-modal');
        showNotification(`Service ${serviceName} updated`, 'success');
        await loadServices();
    } catch (error) {
        showServiceError(`Failed to update service: ${error.message}`);
    }
}

async function toggleRagService(serviceName) {
    try {
        const result = await apiRequest(`/api/service-registry/services/${encodeURIComponent(serviceName)}/toggle`, {
            method: 'POST'
        });
        showToast(result.message, 'success');
        await loadServices();
    } catch (error) {
        showToast(`Failed to toggle ${serviceName}: ${error.message}`, 'error');
    }
}

/**
 * Per-row on-demand health check.
 * Disables the Refresh button and shows "Checking…" in the status cell until
 * the response returns, then reloads the row.  (ruby r1 / plan Phase 4 E)
 *
 * @param {string} serviceName - service name from data model
 * @param {HTMLElement} btn - the button element that was clicked
 */
async function checkRagServiceHealth(serviceName, btn) {
    // Disable button + show checking state in the status cell.
    const statusCell = document.querySelector(`[data-status-item="${CSS.escape(serviceName)}"]`);
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Checking…';
    }
    if (statusCell) {
        statusCell.innerHTML = '<span class="px-2 py-1 text-xs rounded bg-gray-600 text-gray-300">Checking…</span>';
    }

    try {
        const result = await apiRequest(
            `/api/service-registry/services/${encodeURIComponent(serviceName)}/check`,
            { method: 'POST' },
        );
        const status = result.health_status || 'unknown';
        showToast(`${serviceName}: ${status}`, status === 'healthy' ? 'success' : 'warning');
    } catch (error) {
        showToast(`Health check failed for ${serviceName}: ${error.message}`, 'error');
    }

    // Always reload the full table to pick up the updated row.
    await loadServices();
}

async function refreshRagService(serviceName) {
    try {
        const result = await apiRequest(`/api/service-registry/services/${encodeURIComponent(serviceName)}/refresh`, {
            method: 'POST'
        });
        showToast(result.message, 'success');
        await loadServices();
    } catch (error) {
        showToast(`Failed to refresh ${serviceName}: ${error.message}`, 'error');
    }
}

function updateModelCounts() {
    const models = ollamaModels || [];
    const loaded = models.filter(m => m.loaded).length;
    const total = models.length;

    const loadedEl = document.getElementById('ollama-models-loaded');
    const totalEl = document.getElementById('ollama-models-total');
    if (loadedEl) loadedEl.textContent = loaded;
    if (totalEl) totalEl.textContent = total;
}

// ============================================================================
// Service Actions
// ============================================================================

/**
 * POST helper for lifecycle-action routes that returns the PARSED error
 * detail on failure (never the generic apiRequest()'s stringified message)
 * -- 403/409/500 responses carry a structured {error, ...} body this
 * caller needs to read directly (e.g. detail.error).
 */
async function _postServiceControlAction(url, body) {
    const response = await fetch(url, {
        method: 'POST',
        headers: getAuthHeaders({ 'Content-Type': 'application/json' }),
        credentials: 'same-origin',
        body: JSON.stringify(body || {}),
    });

    let payload = null;
    try {
        payload = await response.json();
    } catch (_e) {
        payload = null;
    }

    if (!response.ok) {
        const detail = payload && typeof payload === 'object' ? payload.detail : null;
        const message = (detail && typeof detail === 'object' && detail.error)
            || (typeof detail === 'string' ? detail : null)
            || `HTTP ${response.status}`;
        const err = new Error(message);
        err.status = response.status;
        err.detail = detail;
        throw err;
    }

    return payload;
}

/**
 * Map a 403/409/500 detail.error code to an operator-facing sentence
 * (ruby H6). Unknown codes fall back to the raw code, never a blank toast.
 */
function actionErrorText(code, name) {
    if (!code) return null;
    if (code === 'action_in_progress') return `Another action on ${name} is still running — try again in a minute.`;
    if (code === 'confirmation_required') return `The typed name didn't match ${name}.`;
    if (code === 'insufficient_role') return 'Only an owner can do this.';
    if (code === 'ollama_not_manageable') return 'Ollama is not manageable from this page right now.';
    if (code === 'dispatch_failed') return `Something went wrong controlling ${name}. Check the admin-backend logs.`;
    if (code === 'action_not_available') return `That action isn't available for ${name} right now.`;
    if (code.startsWith('target_collision:')) {
        const target = code.slice('target_collision:'.length);
        return `Two registry rows point at Deployment ${target}; fix the registry so only one does.`;
    }
    return code;
}

async function refreshHistoryPanel() {
    await loadRestartHistory();
    renderRestartTimeline();
}

async function requestServiceAction(name, action) {
    const row = (serviceControl?.services || []).find(r => r.name === name);
    if (!row) {
        showToast(`Unknown service '${name}'`, 'error');
        return;
    }

    // D9/D20: a critical target requires typing the RESOLVED target's name
    // -- codex diff review r2 High #1: this is row.confirm_name, server-
    // resolved and NEVER derived client-side. row.manager_target is not a
    // safe substitute: for a Control-Agent process it's just the bare
    // port number, while confirm_name is 'process:<port>' -- guessing from
    // manager_target would mean an owner could never actually confirm.
    const requireTyped = row.confirm_required ? row.confirm_name : null;
    const displayName = row.display_name || row.name;

    let message;
    if (row.manager === 'kubernetes' && action === 'stop') {
        message = `Scales '${row.manager_target}' to 0 replicas; it stays down until you press Start.`;
    } else if (row.manager === 'kubernetes' && action === 'restart') {
        message = `This restarts '${row.manager_target}' by scaling it to 0 replicas and back. The service will be UNAVAILABLE for the duration of the restart.`;
    } else if (row.manager === 'control_agent' && action === 'restart') {
        message = `This restarts '${displayName}' — it will be briefly unavailable while it restarts.`;
    } else if (action === 'start' && row.run_state === 'disabled') {
        message = `'${displayName}' is disabled in the registry. Starting it brings the underlying process/container up, but it stays unpolled and unrouted until re-enabled. Starting a parked service also starts whatever it fetches on its own (for example live GTFS/Amtrak polling).`;
    } else if (requireTyped) {
        message = `This is a critical, infrastructure-managed target. Type its name to confirm.`;
    } else {
        message = `Are you sure you want to ${action} '${displayName}'?`;
    }

    const confirmed = await showServiceConfirmModal({
        title: `${action.charAt(0).toUpperCase()}${action.slice(1)} ${displayName}`,
        message,
        serviceName: row.name,
        action,
        requireTyped,
    });
    if (confirmed === false) return;

    const body = requireTyped ? { confirm_name: confirmed } : {};

    try {
        const result = await _postServiceControlAction(
            `/api/service-control/${encodeURIComponent(name)}/${encodeURIComponent(action)}`,
            body,
        );
        showToast(result.message, result.success ? 'success' : 'warning');
    } catch (error) {
        const code = error.detail && typeof error.detail === 'object' ? error.detail.error : null;
        const mapped = actionErrorText(code, displayName);
        showToast(mapped || `Failed to ${action} ${name}: ${error.message}`, 'warning');
    }

    await loadServices();
    await refreshHistoryPanel();
}

// ============================================================================
// Model Actions
// ============================================================================

async function loadModel(modelName) {
    try {
        showToast(`Loading ${modelName}... This may take a moment.`, 'info');

        const result = await apiRequest(`/api/service-control/ollama/models/${encodeURIComponent(modelName)}/load`, {
            method: 'POST'
        });

        showToast(result.message, result.success ? 'success' : 'error');
    } catch (error) {
        showToast(`Failed to load model: ${error.message}`, 'error');
    }

    await refreshOllamaPanel();
}

async function unloadModel(modelName) {
    try {
        const result = await apiRequest(`/api/service-control/ollama/models/${encodeURIComponent(modelName)}/unload`, {
            method: 'POST'
        });

        showToast(result.message, result.success ? 'success' : 'error');
    } catch (error) {
        showToast(`Failed to unload model: ${error.message}`, 'error');
    }

    await refreshOllamaPanel();
}

// ============================================================================
// Ollama Service Control
// ============================================================================

async function loadOllamaHealth() {
    try {
        ollamaHealth = await apiRequest('/api/service-control/ollama/health');
    } catch (error) {
        console.error('Failed to load Ollama health:', error);
        // ruby H8: this is the ADMIN-BACKEND'S OWN fetch failing (network
        // down between the browser and admin-backend), not Ollama itself
        // reporting a real status -- fetchFailed distinguishes the two so
        // ollamaModelsMessage doesn't mislabel it "Managed on its host".
        ollamaHealth = {
            healthy: false,
            status: 'error',
            fetchFailed: true,
            api_reachable: false,
            models_loaded: 0,
            version: null,
            host: null,
            manager: 'none',
            manager_target: null,
            manager_note: 'health_check_failed',
            native_actions: [],
            allowed_actions: [],
            confirm_required: false,
            confirm_name: null,
            row_name: null,
        };
    }
}

async function loadOllamaModels() {
    try {
        ollamaModels = await apiRequest('/api/service-control/ollama/models');
        ollamaModelsError = null;
    } catch (error) {
        console.error('Failed to load Ollama models:', error);
        ollamaModels = null;
        ollamaModelsError = error.message || 'Failed to load Ollama models';
    }
}

/**
 * The one Ollama refresh entry point (plan step (i)) -- every reload site
 * calls this instead of awaiting loadOllamaHealth()/loadOllamaModels()
 * directly, so the health card and the models table never drift out of
 * sync with each other.
 */
async function refreshOllamaPanel() {
    await Promise.allSettled([loadOllamaHealth(), loadOllamaModels()]);
    renderOllamaStatus();
    renderOllamaModelsTable();
    updateModelCounts();
}

const OLLAMA_STATUS_BADGES = {
    healthy: { label: 'Healthy', cls: 'bg-green-900 text-green-300' },
    idle: { label: 'Idle (No Models Loaded)', cls: 'bg-blue-900 text-blue-300' },
    offline: { label: 'Offline', cls: 'bg-red-900 text-red-300' },
    error: { label: 'Error', cls: 'bg-red-900 text-red-300' },
    ssrf_blocked: { label: 'Blocked (network guard)', cls: 'bg-yellow-900 text-yellow-300' },
};

function renderOllamaStatus() {
    const container = document.getElementById('ollama-status-panel');
    if (!container) return;

    if (!ollamaHealth) {
        container.innerHTML = '<div class="text-center text-gray-400 py-4">Loading Ollama status…</div>';
        return;
    }

    const badgeInfo = OLLAMA_STATUS_BADGES[ollamaHealth.status] || { label: 'Unknown', cls: 'bg-gray-700 text-gray-300' };
    const statusBadge = `<span class="px-3 py-1 text-sm rounded-full ${escapeHtml(badgeInfo.cls)}">${escapeHtml(badgeInfo.label)}</span>`;
    const isOnline = !!ollamaHealth.healthy;
    // M5: one manager chip (badge + visible sentence, title kept as
    // supplementary), not a badge plus a separate duplicate "Managed on
    // its host" span.
    const managerBadge = _managerBadge({ manager: ollamaHealth.manager, manager_note: ollamaHealth.manager_note });
    const managerNote = _managerNoteSentenceHtml(ollamaHealth.manager_note);

    const actionButtons = ollamaHealth.manager === 'none'
        ? ''
        : (ollamaHealth.allowed_actions || []).map((action) => {
            const label = ACTION_LABELS[action] || action;
            const cls = ACTION_CLASSES[action] || 'bg-gray-600 hover:bg-gray-700';
            return `<button data-service-action="ollama:${escapeHtml(action)}"
                            onclick="requestOllamaAction('${escapeJsAttr(action)}')"
                            class="px-3 py-2 text-sm ${escapeHtml(cls)} text-white rounded transition">${escapeHtml(label)}</button>`;
        }).join('');

    container.innerHTML = `
        <div class="flex items-center justify-between p-4 bg-gray-800 rounded-lg">
            <div class="flex items-center gap-4">
                <div class="w-3 h-3 rounded-full ${isOnline ? 'bg-green-500 animate-pulse' : 'bg-red-500'}"></div>
                <div>
                    <div class="flex items-center gap-3">
                        <span class="text-lg font-semibold text-white">Ollama LLM Server</span>
                        ${statusBadge}
                        ${managerBadge}
                    </div>
                    <div class="text-sm text-gray-400 mt-1">
                        ${ollamaHealth.version ? `Version: ${escapeHtml(ollamaHealth.version)}` : 'Version: Unknown'}
                        | Models Loaded: ${escapeHtml(String(ollamaHealth.models_loaded || 0))}
                        | Host: ${escapeHtml(ollamaHealth.host || 'localhost:11434')}
                    </div>
                    ${managerNote}
                </div>
            </div>
            <div class="flex gap-2">
                ${actionButtons}
                <button onclick="refreshOllamaPanel()"
                        class="px-3 py-2 text-sm bg-blue-600 hover:bg-blue-700 text-white rounded transition">
                    Refresh
                </button>
            </div>
        </div>
    `;
}

/**
 * Pure: what to show in the Ollama models panel before we have a real
 * table to render. Returns null when the caller should render the models
 * table instead of a message.
 *
 * ruby H8: the manager only governs the start/stop/restart buttons --
 * model load/unload hit the Ollama URL directly regardless of manager, so
 * `manager === 'none'` must NOT hide the table (that broke model
 * management for the default OSS deployment, CA and k8s both off).
 */
function ollamaModelsMessage(health, modelsError) {
    if (!health) return 'Loading Ollama status…';
    if (health.fetchFailed) {
        return 'Ollama status unavailable — couldn\'t reach admin-backend.';
    }
    if (health.status === 'ssrf_blocked') {
        return 'Blocked by the network guard — add the Ollama host to HEALTH_POLL_ALLOWED_PRIVATE_HOSTS.';
    }
    if (health.status === 'offline' || health.status === 'error') {
        return 'Ollama appears unreachable. Start it, then refresh to manage models.';
    }
    if (modelsError) {
        return `The models endpoint is unavailable: ${modelsError}`;
    }
    return null;
}

function renderOllamaModelsTable() {
    const container = document.getElementById('ollama-models-table');
    if (!container) return;

    const message = ollamaModelsMessage(ollamaHealth, ollamaModelsError);
    if (message !== null) {
        container.innerHTML = `<div class="text-center text-gray-400 py-8">${escapeHtml(message)}</div>`;
        return;
    }

    const models = ollamaModels || [];
    if (models.length === 0) {
        container.innerHTML = '<div class="text-center text-gray-400 py-8">No models available</div>';
        return;
    }

    const infoIconOr = (key) => (typeof infoIcon === 'function' ? infoIcon(key) : '');

    container.innerHTML = `
        <table class="w-full">
            <thead class="bg-gray-800">
                <tr>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase"><span class="inline-flex items-center gap-1">Model${infoIconOr('ollama-model-name')}</span></th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase"><span class="inline-flex items-center gap-1">Size${infoIconOr('ollama-model-size')}</span></th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase"><span class="inline-flex items-center gap-1">Status${infoIconOr('ollama-model-status')}</span></th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-400 uppercase">Actions</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-gray-700">
                ${models.map(m => renderModelRow(m)).join('')}
            </tbody>
        </table>
    `;
}

function renderModelRow(model) {
    const statusBadge = model.loaded
        ? '<span class="px-2 py-1 text-xs rounded bg-green-900 text-green-300">● Loaded</span>'
        : '<span class="px-2 py-1 text-xs rounded bg-gray-700 text-gray-400">○ Not Loaded</span>';

    return `
        <tr class="hover:bg-gray-800/50">
            <td class="px-4 py-3">
                <div class="text-white font-medium">${escapeHtml(model.name)}</div>
            </td>
            <td class="px-4 py-3 text-sm text-gray-300">${formatBytes(model.size)}</td>
            <td class="px-4 py-3">${statusBadge}</td>
            <td class="px-4 py-3">
                ${model.loaded ? `
                    <button onclick="unloadModel('${escapeJsAttr(model.name)}')"
                            class="px-2 py-1 text-xs bg-yellow-600 hover:bg-yellow-700 text-white rounded">
                        Unload
                    </button>
                ` : `
                    <button onclick="loadModel('${escapeJsAttr(model.name)}')"
                            class="px-2 py-1 text-xs bg-green-600 hover:bg-green-700 text-white rounded">
                        Load
                    </button>
                `}
            </td>
        </tr>
    `;
}

async function requestOllamaAction(action) {
    if (!ollamaHealth) return;

    const requireTyped = ollamaHealth.confirm_required ? (ollamaHealth.confirm_name || 'ollama') : null;
    const message = requireTyped
        ? `This is a critical, infrastructure-managed target. Type its name to confirm.`
        : `Are you sure you want to ${action} Ollama?`;

    const confirmed = await showServiceConfirmModal({
        title: `${action.charAt(0).toUpperCase()}${action.slice(1)} Ollama`,
        message,
        serviceName: 'ollama',
        action,
        requireTyped,
    });
    if (confirmed === false) return;

    const body = requireTyped ? { confirm_name: confirmed } : {};

    try {
        const result = await _postServiceControlAction(`/api/service-control/ollama/${encodeURIComponent(action)}`, body);
        showToast(result.message, result.success ? 'success' : 'warning');
    } catch (error) {
        const code = error.detail && typeof error.detail === 'object' ? error.detail.error : null;
        const mapped = actionErrorText(code, 'Ollama');
        showToast(mapped || `Failed to ${action} Ollama: ${error.message}`, 'error');
    }

    await refreshOllamaPanel();
    await refreshHistoryPanel();
}

// ============================================================================
// Utilities
// ============================================================================

function showServiceError(message) {
    ['core-services-table', 'rag-services-table', 'infrastructure-services-table'].forEach(id => {
        const container = document.getElementById(id);
        if (container) {
            container.innerHTML = `<div class="text-center text-red-400 py-8">${escapeHtml(message)}</div>`;
        }
    });
}

// formatBytes, escapeHtml, and showNotification are now provided by utils.js
// / escape-html.js.

// ============================================================================
// Runbook: Restart Timeline (D15)
// ============================================================================

/**
 * Load restart history from the audit log. audit.py's `action` filter
 * accepts exactly one value (no OR), so the three lifecycle actions are
 * fetched separately and merged client-side, newest first. A 403 on any
 * of the three (no view_audit permission) is remembered distinctly from
 * "no history yet" (ruby M4) rather than silently swallowed into an empty
 * list.
 */
async function loadRestartHistory() {
    restartHistoryError = null;
    const actions = ['service_start', 'service_stop', 'service_restart'];
    const results = await Promise.all(
        actions.map(async (action) => {
            try {
                return await _getJson(`/api/audit?action=${encodeURIComponent(action)}&limit=20`);
            } catch (error) {
                if (error.status === 403) restartHistoryError = 'permission';
                return [];
            }
        })
    );
    restartHistory = results
        .flat()
        .sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp))
        .slice(0, 20);
}

function _serviceNameForAuditLog(log) {
    if (log.resource_id != null && serviceControl?.services) {
        const row = serviceControl.services.find(r => r.id === log.resource_id);
        if (row) return row.display_name || row.name;
    }
    if (log.new_value && log.new_value.target) return log.new_value.target;
    return 'Service';
}

function _historyHeadingHtml() {
    return `
        <div class="mb-4">
            <h3 class="text-lg font-semibold text-white mb-2">Service Lifecycle History</h3>
            <p class="text-sm text-gray-400">Recent service start/stop/restart events</p>
        </div>
    `;
}

function renderRestartTimeline() {
    const container = document.getElementById('restart-timeline');
    if (!container) return;

    // Persistent heading (ruby M4) -- the panel always identifies itself,
    // whether it's populated, empty, or permission-denied.
    const heading = _historyHeadingHtml();

    if (restartHistoryError === 'permission') {
        container.innerHTML = heading + `
            <div class="text-center text-gray-500 py-8">
                You don't have permission to view service history (view_audit).
            </div>
        `;
        return;
    }

    if (restartHistory.length === 0) {
        container.innerHTML = heading + `
            <div class="text-center text-gray-500 py-8">
                <i data-lucide="history" class="w-8 h-8 mx-auto mb-2 opacity-50"></i>
                <p>No recent restart history</p>
            </div>
        `;
        if (typeof lucide !== 'undefined') lucide.createIcons();
        return;
    }

    container.innerHTML = heading + `
        <div class="relative">
            <div class="absolute left-4 top-0 bottom-0 w-0.5 bg-dark-border"></div>
            <div class="space-y-4">
                ${restartHistory.map(event => renderTimelineEvent(event)).join('')}
            </div>
        </div>
    `;

    if (typeof lucide !== 'undefined') lucide.createIcons();
}

function renderTimelineEvent(event) {
    const timestamp = new Date(event.timestamp);
    const timeAgo = formatTimeAgo(timestamp);
    const serviceName = _serviceNameForAuditLog(event);
    const action = event.action || 'service_restart';
    const actionText = ACTION_PAST_TENSE[action] || action;
    const user = event.username || event.user || 'System';

    let iconColor = 'text-blue-400';
    let bgColor = 'bg-blue-500/20';
    let icon = 'refresh-cw';

    if (action.includes('stop')) {
        iconColor = 'text-red-400';
        bgColor = 'bg-red-500/20';
        icon = 'square';
    } else if (action.includes('start')) {
        iconColor = 'text-green-400';
        bgColor = 'bg-green-500/20';
        icon = 'play';
    }

    const outcome = event.success === false
        ? `<span class="text-red-400"> (failed${event.error_message ? `: ${escapeHtml(event.error_message)}` : ''})</span>`
        : '';

    return `
        <div class="relative pl-10">
            <div class="absolute left-2 w-4 h-4 rounded-full ${bgColor} flex items-center justify-center">
                <i data-lucide="${icon}" class="w-2.5 h-2.5 ${iconColor}"></i>
            </div>
            <div class="bg-dark-elevated rounded-lg p-3 border border-dark-border">
                <div class="flex items-center justify-between">
                    <span class="font-medium text-white">${escapeHtml(serviceName)}</span>
                    <span class="text-xs text-gray-500">${escapeHtml(timeAgo)}</span>
                </div>
                <p class="text-sm text-gray-400 mt-1">${escapeHtml(actionText)} by ${escapeHtml(user)}${outcome}</p>
            </div>
        </div>
    `;
}

function formatTimeAgo(date) {
    const seconds = Math.floor((new Date() - date) / 1000);

    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
}

// ============================================================================
// Confirmation modal (extended with typed confirmation, D9/D20, and full
// dialog semantics / focus management, ruby H1)
// ============================================================================

/**
 * Pure: does the typed value exactly (case-sensitively) match the expected
 * target name? Extracted so it's testable without a DOM (ruby M1 / tessa).
 */
function typedConfirmMatches(expected, value) {
    return expected != null && value === expected;
}

function _focusableElements(container) {
    return Array.from(container.querySelectorAll(
        'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])'
    ));
}

/**
 * Show a service action confirmation modal with dependency warnings, and
 * (when requireTyped is set) a typed-confirmation input the operator must
 * fill in with the EXACT resolved target name before Proceed is enabled.
 *
 * Resolves:
 *   - `false` on cancel / Escape / backdrop click
 *   - `true` when requireTyped is falsy and the operator clicks Proceed
 *   - the typed string when requireTyped is set and it matches exactly
 *
 * Dialog semantics (ruby H1): role="dialog" + aria-modal + aria-labelledby;
 * focus moves into the modal on open (the typed input if present, else
 * Cancel); Tab is trapped inside the modal; Enter in the typed input
 * submits only when the match is exact (a <form> + submit handler, so a
 * disabled submit button can't fire early); on close, focus returns to the
 * row's own action button (data-service-action="<name>:<action>"),
 * falling back to the page heading, since loadServices()'s re-render
 * destroys the original trigger element.
 */
function showServiceConfirmModal({ title, message, services, action, serviceName, requireTyped = null }) {
    return new Promise((resolve) => {
        let warnings = [];
        const targetServices = serviceName ? [serviceName] : (services || []);

        for (const svc of targetServices) {
            const dep = SERVICE_DEPENDENCIES[svc.toLowerCase()];
            if (dep && (action === 'stop' || action === 'restart')) {
                warnings.push({
                    service: svc,
                    warning: dep.warning,
                    dependents: dep.dependents
                });
            }
        }

        const restoreSelector = (serviceName && action)
            ? `[data-service-action="${CSS.escape(serviceName)}:${CSS.escape(action)}"]`
            : null;

        const modal = document.createElement('div');
        modal.className = 'fixed inset-0 bg-black/60 flex items-center justify-center z-50';
        modal.id = 'service-confirm-modal';

        const titleId = 'service-confirm-title';

        modal.innerHTML = `
            <div class="bg-dark-card rounded-lg shadow-xl border border-dark-border max-w-md w-full mx-4" role="dialog" aria-modal="true" aria-labelledby="${escapeHtml(titleId)}">
                <form id="service-confirm-form">
                    <div class="p-6">
                        <div class="flex items-center gap-3 mb-4">
                            <div class="p-2 bg-yellow-500/20 rounded-lg">
                                <i data-lucide="alert-triangle" class="w-6 h-6 text-yellow-400"></i>
                            </div>
                            <h3 id="${escapeHtml(titleId)}" class="text-lg font-semibold text-white">${escapeHtml(title)}</h3>
                        </div>

                        <p class="text-gray-300 mb-4">${escapeHtml(message)}</p>

                        ${services ? `
                            <div class="flex flex-wrap gap-2 mb-4">
                                ${services.map((s, i) => `
                                    <span class="inline-flex items-center gap-1 px-3 py-1 bg-gray-700 text-gray-200 rounded-full text-sm">
                                        ${i > 0 ? '<i data-lucide="arrow-right" class="w-3 h-3 text-gray-500"></i>' : ''}
                                        ${escapeHtml(s)}
                                    </span>
                                `).join('')}
                            </div>
                        ` : ''}

                        ${warnings.length > 0 ? `
                            <div class="bg-yellow-500/10 border border-yellow-500/30 rounded-lg p-3 mb-4">
                                <p class="text-sm font-medium text-yellow-400 mb-2">Warning</p>
                                ${warnings.map(w => `
                                    <p class="text-sm text-yellow-300/80 mb-1">
                                        <strong>${escapeHtml(w.service)}:</strong> ${escapeHtml(w.warning)}
                                    </p>
                                `).join('')}
                            </div>
                        ` : ''}

                        <div id="service-confirm-typed-container"></div>
                    </div>

                    <div class="px-6 py-4 bg-gray-800/50 border-t border-dark-border flex justify-end gap-3 rounded-b-lg">
                        <button type="button" id="confirm-cancel" class="px-4 py-2 text-sm text-gray-400 hover:text-white transition">
                            Cancel
                        </button>
                        <button type="submit" id="confirm-proceed" class="px-4 py-2 text-sm bg-yellow-600 hover:bg-yellow-700 text-white rounded-lg transition">
                            Proceed
                        </button>
                    </div>
                </form>
            </div>
        `;

        document.body.appendChild(modal);

        const form = modal.querySelector('#service-confirm-form');
        const proceedBtn = modal.querySelector('#confirm-proceed');
        const cancelBtn = modal.querySelector('#confirm-cancel');
        let typedInput = null;

        if (requireTyped) {
            const container = modal.querySelector('#service-confirm-typed-container');
            const labelId = 'service-confirm-typed-label';

            const label = document.createElement('label');
            label.className = 'block text-sm font-medium text-gray-300 mb-1';
            label.id = labelId;
            label.setAttribute('for', 'service-confirm-typed-input');
            // requireTyped is the server-resolved target name -- textContent
            // renders it as plain text, never as HTML (D9/plan step f).
            label.textContent = `Type "${requireTyped}" to confirm:`;

            typedInput = document.createElement('input');
            typedInput.type = 'text';
            typedInput.id = 'service-confirm-typed-input';
            typedInput.autocomplete = 'off';
            typedInput.spellcheck = false;
            // L2: associate the disabled Proceed button with the label
            // explaining why, for screen-reader users.
            typedInput.setAttribute('aria-describedby', labelId);
            typedInput.className = 'w-full px-3 py-2 bg-dark-bg border border-dark-border rounded text-white focus:outline-none focus:border-yellow-500';

            container.appendChild(label);
            container.appendChild(typedInput);

            proceedBtn.disabled = true;
            proceedBtn.setAttribute('aria-describedby', labelId);
            proceedBtn.classList.add('opacity-50', 'cursor-not-allowed');

            typedInput.addEventListener('input', () => {
                const matches = typedConfirmMatches(requireTyped, typedInput.value);
                proceedBtn.disabled = !matches;
                proceedBtn.classList.toggle('opacity-50', !matches);
                proceedBtn.classList.toggle('cursor-not-allowed', !matches);
            });
        }

        if (typeof lucide !== 'undefined') lucide.createIcons();

        function restoreFocus() {
            const target = (restoreSelector && document.querySelector(restoreSelector))
                || document.querySelector('#tab-service-control h2');
            if (target && typeof target.focus === 'function') target.focus();
        }

        function finish(result) {
            modal.remove();
            document.removeEventListener('keydown', keyHandler);
            restoreFocus();
            resolve(result);
        }

        const keyHandler = (e) => {
            if (e.key === 'Escape') {
                finish(false);
                return;
            }
            if (e.key === 'Tab') {
                const focusables = _focusableElements(modal);
                if (focusables.length === 0) return;
                const first = focusables[0];
                const last = focusables[focusables.length - 1];
                if (e.shiftKey && document.activeElement === first) {
                    e.preventDefault();
                    last.focus();
                } else if (!e.shiftKey && document.activeElement === last) {
                    e.preventDefault();
                    first.focus();
                }
            }
        };
        document.addEventListener('keydown', keyHandler);

        cancelBtn.onclick = () => finish(false);

        form.addEventListener('submit', (e) => {
            e.preventDefault();
            if (requireTyped && !typedConfirmMatches(requireTyped, typedInput ? typedInput.value : '')) return;
            finish(requireTyped ? typedInput.value : true);
        });

        modal.onclick = (e) => {
            if (e.target === modal) finish(false);
        };

        if (typedInput) {
            typedInput.focus();
        } else {
            cancelBtn.focus();
        }
    });
}

// Node-testability (mirrors utils.js's window.formatBytes = formatBytes
// pattern): in a real browser these top-level function declarations are
// already window properties (classic-script semantics); under Node's
// CommonJS require() they are not, so the pure functions T12–T14 exercise
// are attached explicitly.
if (typeof window !== 'undefined') {
    window.groupServiceControlRows = groupServiceControlRows;
    window.renderStatusCell = renderStatusCell;
    window.renderServiceActions = renderServiceActions;
    window.ollamaModelsMessage = ollamaModelsMessage;
    window.typedConfirmMatches = typedConfirmMatches;
}
