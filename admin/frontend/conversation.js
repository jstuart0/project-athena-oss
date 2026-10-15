/**
 * Conversation Context Management Functions
 * Phase 0-2: Database, Session Management, and Device Sessions
 */

// ============================================================================
// CONVERSATION SETTINGS
// ============================================================================

async function loadConversationSettings() {
    // Its own request and its own error handling: a failure or a 403 here never
    // hides the cards below, and theirs never hides this one.
    loadFavoriteTeams();

    try {
        const response = await fetch(`${API_BASE}/api/conversation/settings`, {
            headers: {
                'Authorization': `Bearer ${authToken}`
            }
        });

        if (!response.ok) {
            throw new Error('Failed to load conversation settings');
        }

        const data = await response.json();
        renderConversationSettings(data);

        // Load other sections
        loadClarificationTypes();
        loadSportsTeams();
        loadConversationAnalytics();

    } catch (error) {
        console.error('Failed to load conversation settings:', error);
        showError('Failed to load conversation settings');
    }
}

function renderConversationSettings(settings) {
    const container = document.getElementById('conversation-settings-container');
    const historyMode = settings.history_mode || 'full';

    container.innerHTML = `
        <div>
            <label for="timeout-seconds" class="block text-sm font-medium text-gray-400 mb-2 flex items-center">Session Timeout (seconds)${infoIcon('conv-session-timeout')}</label>
            <input type="number" id="timeout-seconds" value="${settings.timeout_seconds || 1800}"
                class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
            <p class="text-xs text-gray-500 mt-1">Time before session expires (default: 1800 = 30 min)</p>
        </div>

        <div>
            <label for="session-ttl-seconds" class="block text-sm font-medium text-gray-400 mb-2 flex items-center">Session TTL (seconds)${infoIcon('conv-session-timeout', 'Maximum absolute session lifetime regardless of activity. Sessions older than this are automatically expired.')}</label>
            <input type="number" id="session-ttl-seconds" value="${settings.session_ttl_seconds || 3600}"
                class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
            <p class="text-xs text-gray-500 mt-1">Maximum session lifetime (default: 3600 = 1 hour)</p>
        </div>

        <div>
            <label for="max-messages" class="block text-sm font-medium text-gray-400 mb-2 flex items-center">Max Messages${infoIcon('conv-history-length')}</label>
            <input type="number" id="max-messages" value="${settings.max_messages || 20}"
                class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
            <p class="text-xs text-gray-500 mt-1">Maximum messages to store in session (default: 20)</p>
        </div>

        <div>
            <label for="cleanup-interval-seconds" class="block text-sm font-medium text-gray-400 mb-2 flex items-center">Cleanup Interval (seconds)${infoIcon('conv-session-timeout', 'How often to check for and remove expired sessions.')}</label>
            <input type="number" id="cleanup-interval-seconds" value="${settings.cleanup_interval_seconds || 60}"
                class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
            <p class="text-xs text-gray-500 mt-1">Session cleanup frequency (default: 60)</p>
        </div>

        <div class="flex items-center">
            <input type="checkbox" id="conversation-enabled" ${settings.enabled ? 'checked' : ''}
                class="w-4 h-4 bg-dark-bg border border-dark-border rounded">
            <label for="conversation-enabled" class="ml-2 text-sm text-gray-400 flex items-center">Enable Conversation Context${infoIcon('conv-session-timeout', 'When enabled, maintains conversation context across multiple turns.')}</label>
        </div>

        <div class="flex items-center">
            <input type="checkbox" id="use-context" ${settings.use_context ? 'checked' : ''}
                class="w-4 h-4 bg-dark-bg border border-dark-border rounded">
            <label for="use-context" class="ml-2 text-sm text-gray-400 flex items-center">Use Context in LLM Requests${infoIcon('conv-clarification', 'Include conversation history when making LLM requests.')}</label>
        </div>

        <!-- History Mode Selection -->
        <div class="col-span-2 mt-4 p-4 bg-dark-bg border border-dark-border rounded-lg">
            <p class="block text-sm font-medium text-white mb-3">
                Conversation History Mode
                <span class="ml-2 text-xs text-gray-400">(affects response latency)</span>
            </p>

            <div class="grid grid-cols-3 gap-4">
                <label class="relative flex flex-col p-4 border rounded-lg cursor-pointer ${historyMode === 'none' ? 'border-green-500 bg-green-900/20' : 'border-dark-border hover:border-gray-500'}">
                    <input type="radio" name="history-mode" value="none" ${historyMode === 'none' ? 'checked' : ''}
                        class="sr-only" onchange="updateHistoryModeUI()">
                    <span class="text-sm font-medium text-white">No History</span>
                    <span class="text-xs text-green-400 mt-1">~2s response</span>
                    <span class="text-xs text-gray-400 mt-2">Each query is independent. Best for quick commands like "turn on lights" or "what's the weather".</span>
                </label>

                <label class="relative flex flex-col p-4 border rounded-lg cursor-pointer ${historyMode === 'summarized' ? 'border-blue-500 bg-blue-900/20' : 'border-dark-border hover:border-gray-500'}">
                    <input type="radio" name="history-mode" value="summarized" ${historyMode === 'summarized' ? 'checked' : ''}
                        class="sr-only" onchange="updateHistoryModeUI()">
                    <span class="text-sm font-medium text-white">Summarized</span>
                    <span class="text-xs text-blue-400 mt-1">~3-4s response</span>
                    <span class="text-xs text-gray-400 mt-2">Compresses recent conversation into a brief summary. Good for follow-up questions.</span>
                </label>

                <label class="relative flex flex-col p-4 border rounded-lg cursor-pointer ${historyMode === 'full' ? 'border-purple-500 bg-purple-900/20' : 'border-dark-border hover:border-gray-500'}">
                    <input type="radio" name="history-mode" value="full" ${historyMode === 'full' ? 'checked' : ''}
                        class="sr-only" onchange="updateHistoryModeUI()">
                    <span class="text-sm font-medium text-white">Full History</span>
                    <span class="text-xs text-purple-400 mt-1">~5-15s response</span>
                    <span class="text-xs text-gray-400 mt-2">Includes complete conversation history. Best for complex multi-turn conversations.</span>
                </label>
            </div>
        </div>

        <!-- Max History Messages (only visible for 'full' mode) -->
        <div id="max-history-container" class="col-span-2 ${historyMode === 'full' ? '' : 'hidden'}">
            <label for="max-llm-history-messages" class="block text-sm font-medium text-gray-400 mb-2 flex items-center">
                Max LLM History Messages
                ${infoIcon('conv-history-length', 'Number of previous messages to include in LLM context. Higher = more context but slower responses.')}
            </label>
            <input type="range" id="max-llm-history-messages" value="${settings.max_llm_history_messages || 10}"
                min="2" max="20" step="2"
                class="w-full h-2 bg-dark-border rounded-lg appearance-none cursor-pointer"
                oninput="document.getElementById('history-count-display').textContent = this.value">
            <div class="flex justify-between text-xs text-gray-500 mt-1">
                <span>2 (faster)</span>
                <span id="history-count-display">${settings.max_llm_history_messages || 10}</span>
                <span>20 (more context)</span>
            </div>
        </div>

        <div class="col-span-2 mt-4">
            <button onclick="saveConversationSettings()"
                class="px-6 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm font-medium transition-colors">
                Save Settings
            </button>
        </div>
    `;
}

function updateHistoryModeUI() {
    const selectedMode = document.querySelector('input[name="history-mode"]:checked')?.value || 'full';
    const maxHistoryContainer = document.getElementById('max-history-container');

    // Show/hide max history slider based on mode
    if (selectedMode === 'full') {
        maxHistoryContainer.classList.remove('hidden');
    } else {
        maxHistoryContainer.classList.add('hidden');
    }

    // Update visual selection state
    document.querySelectorAll('input[name="history-mode"]').forEach(radio => {
        const label = radio.closest('label');
        // Remove all highlight classes
        label.classList.remove('border-green-500', 'border-blue-500', 'border-purple-500',
                               'bg-green-900/20', 'bg-blue-900/20', 'bg-purple-900/20');
        label.classList.add('border-dark-border');

        if (radio.checked) {
            label.classList.remove('border-dark-border');
            if (radio.value === 'none') {
                label.classList.add('border-green-500', 'bg-green-900/20');
            } else if (radio.value === 'summarized') {
                label.classList.add('border-blue-500', 'bg-blue-900/20');
            } else {
                label.classList.add('border-purple-500', 'bg-purple-900/20');
            }
        }
    });
}

async function saveConversationSettings() {
    try {
        const settings = {
            enabled: document.getElementById('conversation-enabled').checked,
            use_context: document.getElementById('use-context').checked,
            max_messages: parseInt(document.getElementById('max-messages').value),
            timeout_seconds: parseInt(document.getElementById('timeout-seconds').value),
            cleanup_interval_seconds: parseInt(document.getElementById('cleanup-interval-seconds').value),
            session_ttl_seconds: parseInt(document.getElementById('session-ttl-seconds').value),
            max_llm_history_messages: parseInt(document.getElementById('max-llm-history-messages').value),
            history_mode: document.querySelector('input[name="history-mode"]:checked')?.value || 'full'
        };

        const response = await fetch(`${API_BASE}/api/conversation/settings`, {
            method: 'PUT',
            headers: {
                'Authorization': `Bearer ${authToken}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(settings)
        });

        if (!response.ok) {
            throw new Error('Failed to save settings');
        }

        showSuccess('Settings saved successfully');
        loadConversationSettings(); // Reload to show updated values

    } catch (error) {
        console.error('Failed to save settings:', error);
        showError('Failed to save conversation settings');
    }
}

// ============================================================================
// CLARIFICATION TYPES
// ============================================================================

async function loadClarificationTypes() {
    try {
        const response = await fetch(`${API_BASE}/api/conversation/clarification/types`, {
            headers: {
                'Authorization': `Bearer ${authToken}`
            }
        });

        if (!response.ok) {
            throw new Error('Failed to load clarification types');
        }

        const types = await response.json();
        renderClarificationTypes(types);

    } catch (error) {
        console.error('Failed to load clarification types:', error);
        showError('Failed to load clarification types');
    }
}

function renderClarificationTypes(types) {
    const container = document.getElementById('clarification-types-container');

    if (!types || types.length === 0) {
        container.innerHTML = '<p class="text-gray-400 text-sm">No clarification types configured. Click "Add Clarification Type" to create one.</p>';
        return;
    }

    container.innerHTML = types.map(type => `
        <div class="bg-dark-bg border border-dark-border rounded-lg p-4">
            <div class="flex justify-between items-start mb-2">
                <div>
                    <h4 class="font-medium text-white">${type.type || 'Unknown Type'}</h4>
                    <p class="text-sm text-gray-400 mt-1">${type.description || 'No description'}</p>
                </div>
                <span class="px-2 py-1 rounded text-xs ${type.enabled ? 'bg-green-900/30 text-green-400' : 'bg-gray-900/30 text-gray-400'}">
                    ${type.enabled ? 'Enabled' : 'Disabled'}
                </span>
            </div>
            <div class="mt-2 text-xs text-gray-500 flex gap-4">
                <span><span class="font-medium">Priority:</span> ${type.priority || 0}</span>
                ${type.timeout_seconds ? `<span><span class="font-medium">Timeout:</span> ${type.timeout_seconds}s</span>` : ''}
            </div>
        </div>
    `).join('');
}

function showCreateClarificationTypeModal() {
    // Note: Backend doesn't support creating new types via API
    // This modal now shows info about existing types
    const modal = document.createElement('div');
    modal.id = 'clarification-type-modal';
    modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-md">
            <h3 class="text-lg font-semibold text-white mb-4">Clarification Types</h3>

            <div class="bg-blue-900/20 border border-blue-700/50 rounded-lg p-4 mb-4">
                <p class="text-blue-200 text-sm">
                    Clarification types are pre-configured in the database. Use this section to view and
                    enable/disable existing types like: device, location, time, sports_team.
                </p>
            </div>

            <div class="space-y-4">
                <p class="text-gray-400 text-sm">
                    Supported clarification types:
                </p>
                <ul class="text-gray-300 text-sm space-y-2 ml-4">
                    <li><span class="text-blue-400">device</span> - Clarify which device to control</li>
                    <li><span class="text-blue-400">location</span> - Clarify which location/room</li>
                    <li><span class="text-blue-400">time</span> - Clarify time references</li>
                    <li><span class="text-blue-400">sports_team</span> - Clarify ambiguous team names</li>
                </ul>
            </div>

            <div class="flex gap-3 mt-6">
                <button onclick="closeModal('clarification-type-modal')"
                    class="flex-1 px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-sm font-medium transition-colors">
                    Close
                </button>
            </div>
        </div>
    `;

    document.getElementById('modals-container').appendChild(modal);
}

// ============================================================================
// FAVOURITE TEAMS
// ============================================================================
// An ordered list of the household's teams, each with an optional league. It is
// built with createElement/textContent only (a team name is data, never markup)
// and saved whole with the revision it was loaded at.

const FAVORITE_TEAMS_URL = '/api/settings/favorite-teams';
const FAVORITE_TEAM_NAME_RULE = "Use letters, numbers, spaces and . - ' only";
const FAVORITE_TEAM_STALE_MESSAGE = "Someone else changed this list while you were editing. Your changes weren't saved. The latest list is shown below; make your changes again.";
const FAVORITE_TEAM_REJECTED_MESSAGE = "A team name wasn't accepted. Names use letters, numbers, spaces and . - ' only (up to 64 characters), a list holds up to 8 teams, and a team can't appear twice.";
const FAVORITE_TEAM_LEAVE_MESSAGE = 'You have unsaved changes to Favourite teams. Leave without saving?';

const favoriteTeamsState = {
    view: 'loading',      // loading | ready | error | forbidden
    teams: [],            // the working copy: [{name, league}]
    saved: '[]',          // JSON of the last saved/loaded list
    revision: 0,
    leagues: [],          // [{code, label, sport}] from the server
    max: 8,
    saving: false,
    wired: false,
};

function normalizeFavoriteName(value) {
    return String(value == null ? '' : value).trim().normalize('NFC');
}

function favoriteNameRuleOk(name) {
    if (name.length < 1 || name.length > 64) return false;
    for (const ch of name) {
        if (!/[\p{L}\p{N}]/u.test(ch) && " .-'".indexOf(ch) === -1) return false;
    }
    return true;
}

// '' when the name can be added, else the sentence naming the broken rule.
function favoriteTeamProblem(rawName, teams, max) {
    const name = normalizeFavoriteName(rawName);
    if (!name) return 'Enter a team name';
    if (!favoriteNameRuleOk(name)) return FAVORITE_TEAM_NAME_RULE;
    const lower = name.toLowerCase();
    if (teams.some(t => t.name.toLowerCase() === lower)) return 'That team is already in the list';
    if (teams.length >= max) return `Up to ${max} teams`;
    return '';
}

function moveFavoriteTeam(teams, index, delta) {
    const target = index + delta;
    if (index < 0 || index >= teams.length || target < 0 || target >= teams.length) return teams.slice();
    const copy = teams.slice();
    const [moved] = copy.splice(index, 1);
    copy.splice(target, 0, moved);
    return copy;
}

function favoriteTeamsDirty() {
    return favoriteTeamsState.view === 'ready' && JSON.stringify(favoriteTeamsState.teams) !== favoriteTeamsState.saved;
}

function favoriteLeagueLabel(code) {
    const league = favoriteTeamsState.leagues.find(l => l.code === code);
    return league ? league.label : 'Any league';
}

function favoriteTeamsEl(id) {
    return document.getElementById(id);
}

function setFavoriteTeamsMessage(text, tone) {
    const el = favoriteTeamsEl('favorite-teams-message');
    if (!el) return;
    el.textContent = text || '';
    el.className = `${text ? '' : 'hidden '}text-sm mb-3 ${tone === 'error' ? 'text-red-400' : 'text-green-400'}`;
}

function announceFavoriteTeams(text) {
    const el = favoriteTeamsEl('favorite-teams-status');
    if (el) el.textContent = text || '';
}

function setFavoriteNameError(text) {
    const el = favoriteTeamsEl('favorite-team-name-error');
    const input = favoriteTeamsEl('favorite-team-name');
    if (el) {
        el.textContent = text || '';
        el.className = `${text ? '' : 'hidden '}text-xs text-red-400 mt-1`;
    }
    if (input) input.setAttribute('aria-invalid', text ? 'true' : 'false');
}

function updateFavoriteTeamsControls() {
    const ready = favoriteTeamsState.view === 'ready';
    const dirty = favoriteTeamsDirty();
    const save = favoriteTeamsEl('favorite-teams-save');
    if (save) {
        save.disabled = !dirty || favoriteTeamsState.saving;
        save.textContent = favoriteTeamsState.saving ? 'Saving…' : 'Save';
    }
    const marker = favoriteTeamsEl('favorite-teams-dirty');
    if (marker) marker.className = `${dirty ? '' : 'hidden '}text-xs text-yellow-400`;
    ['favorite-team-name', 'favorite-team-league', 'favorite-team-add'].forEach(id => {
        const el = favoriteTeamsEl(id);
        if (el) el.disabled = !ready || favoriteTeamsState.saving;
    });
}

function populateFavoriteLeagues() {
    const select = favoriteTeamsEl('favorite-team-league');
    if (!select) return;
    select.textContent = '';
    const any = document.createElement('option');
    any.value = '';
    any.textContent = 'Any league';
    select.appendChild(any);
    const groups = new Map();
    favoriteTeamsState.leagues.forEach(league => {
        if (!groups.has(league.sport)) {
            const group = document.createElement('optgroup');
            group.label = league.sport;
            groups.set(league.sport, group);
            select.appendChild(group);
        }
        const option = document.createElement('option');
        option.value = league.code;
        option.textContent = league.label;
        groups.get(league.sport).appendChild(option);
    });
}

function favoriteButton(text, label, onClick, disabled) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = text;
    button.setAttribute('aria-label', label);
    button.title = label;
    button.disabled = !!disabled;
    button.className = 'min-h-[44px] min-w-[44px] px-3 py-1 bg-dark-bg border border-dark-border rounded-lg text-gray-300 text-sm hover:bg-dark-border disabled:opacity-40 disabled:cursor-not-allowed';
    button.addEventListener('click', onClick);
    return button;
}

// focus: {index, kind: 'up' | 'down' | 'remove'}, or {name: true} for the name input.
function renderFavoriteTeams(focus) {
    const container = favoriteTeamsEl('favorite-teams-container');
    if (!container) return;
    container.textContent = '';
    const view = favoriteTeamsState.view;

    if (view !== 'ready') {
        const note = document.createElement('p');
        note.className = 'text-sm text-gray-400';
        if (view === 'loading') {
            note.textContent = 'Loading…';
        } else if (view === 'forbidden') {
            note.textContent = "You don't have permission to view this setting";
        } else {
            note.textContent = "Couldn't load your favourite teams. ";
            const retry = document.createElement('button');
            retry.type = 'button';
            retry.textContent = 'Retry';
            retry.className = 'min-h-[44px] px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm';
            retry.addEventListener('click', () => loadFavoriteTeams(true));
            note.appendChild(retry);
        }
        container.appendChild(note);
        updateFavoriteTeamsControls();
        return;
    }

    const teams = favoriteTeamsState.teams;
    if (teams.length === 0) {
        const empty = document.createElement('p');
        empty.className = 'text-sm text-gray-400';
        empty.textContent = 'No favourite teams yet. Add one below.';
        container.appendChild(empty);
    }

    const list = document.createElement('ol');
    list.className = 'space-y-2';
    const targets = [];
    teams.forEach((team, index) => {
        const row = document.createElement('li');
        row.className = 'flex items-center gap-3 bg-dark-bg border border-dark-border rounded-lg p-2';

        const position = document.createElement('span');
        position.className = 'w-6 text-center text-sm text-gray-500';
        position.textContent = String(index + 1);

        const text = document.createElement('div');
        text.className = 'flex-1 min-w-0';
        const name = document.createElement('div');
        name.className = 'font-medium text-white truncate';
        name.textContent = team.name;
        const league = document.createElement('div');
        league.className = 'text-xs text-gray-400';
        league.textContent = favoriteLeagueLabel(team.league);
        text.appendChild(name);
        text.appendChild(league);

        const up = favoriteButton('▲', `Move ${team.name} up`, () => moveFavorite(index, -1, 'up'), index === 0);
        const down = favoriteButton('▼', `Move ${team.name} down`, () => moveFavorite(index, 1, 'down'), index === teams.length - 1);
        const remove = favoriteButton('Remove', `Remove ${team.name}`, () => removeFavorite(index), false);
        targets.push({ up, down, remove });

        row.appendChild(position);
        row.appendChild(text);
        row.appendChild(up);
        row.appendChild(down);
        row.appendChild(remove);
        list.appendChild(row);
    });
    if (teams.length > 0) container.appendChild(list);

    updateFavoriteTeamsControls();

    if (focus) {
        let target = null;
        if (focus.name) {
            target = favoriteTeamsEl('favorite-team-name');
        } else if (targets[focus.index]) {
            const row = targets[focus.index];
            // A disabled arrow can't take focus: hand it to the other arrow.
            target = row[focus.kind].disabled ? (focus.kind === 'up' ? row.down : row.up) : row[focus.kind];
        }
        if (target && !target.disabled) target.focus();
    }
}

function moveFavorite(index, delta, kind) {
    const moved = favoriteTeamsState.teams[index];
    favoriteTeamsState.teams = moveFavoriteTeam(favoriteTeamsState.teams, index, delta);
    setFavoriteTeamsMessage('');
    renderFavoriteTeams({ index: index + delta, kind });
    announceFavoriteTeams(`${moved.name} moved to position ${index + delta + 1} of ${favoriteTeamsState.teams.length}`);
}

function removeFavorite(index) {
    const [removed] = favoriteTeamsState.teams.splice(index, 1);
    setFavoriteTeamsMessage('');
    const remaining = favoriteTeamsState.teams.length;
    // Focus goes to the next Remove button, else the previous one, else the name input.
    const focus = remaining === 0 ? { name: true } : { index: Math.min(index, remaining - 1), kind: 'remove' };
    renderFavoriteTeams(focus);
    announceFavoriteTeams(`${removed.name} removed`);
}

function addFavoriteTeam() {
    const input = favoriteTeamsEl('favorite-team-name');
    const select = favoriteTeamsEl('favorite-team-league');
    if (!input || favoriteTeamsState.view !== 'ready') return;
    const problem = favoriteTeamProblem(input.value, favoriteTeamsState.teams, favoriteTeamsState.max);
    if (problem) {
        setFavoriteNameError(problem);
        input.focus();
        return;
    }
    setFavoriteNameError('');
    setFavoriteTeamsMessage('');
    const name = normalizeFavoriteName(input.value);
    favoriteTeamsState.teams.push({ name, league: select && select.value ? select.value : null });
    input.value = '';
    renderFavoriteTeams();
    input.focus();
    announceFavoriteTeams(`${name} added at position ${favoriteTeamsState.teams.length}`);
}

function applyFavoriteTeamsPayload(data) {
    favoriteTeamsState.teams = (data.teams || []).map(t => ({ name: t.name, league: t.league || null }));
    favoriteTeamsState.saved = JSON.stringify(favoriteTeamsState.teams);
    favoriteTeamsState.revision = data.revision || 0;
    favoriteTeamsState.leagues = data.leagues || favoriteTeamsState.leagues;
    favoriteTeamsState.max = data.max || favoriteTeamsState.max;
}

// force: reload even over unsaved edits (Retry, and after a conflict). A plain
// reload (the page's Refresh) keeps edits that haven't been saved.
async function loadFavoriteTeams(force) {
    initFavoriteTeams();
    if (!force && favoriteTeamsDirty()) return;
    favoriteTeamsState.view = 'loading';
    renderFavoriteTeams();
    try {
        const response = await fetch(`${API_BASE}${FAVORITE_TEAMS_URL}`, {
            headers: {
                'Authorization': `Bearer ${authToken}`
            }
        });
        if (response.status === 403) {
            favoriteTeamsState.view = 'forbidden';
        } else if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        } else {
            applyFavoriteTeamsPayload(await response.json());
            populateFavoriteLeagues();
            favoriteTeamsState.view = 'ready';
        }
    } catch (error) {
        console.error('Failed to load favourite teams:', error);
        favoriteTeamsState.view = 'error';
    }
    renderFavoriteTeams();
}

async function saveFavoriteTeams() {
    if (!favoriteTeamsDirty() || favoriteTeamsState.saving) return;
    favoriteTeamsState.saving = true;
    setFavoriteTeamsMessage('');
    updateFavoriteTeamsControls();
    try {
        const response = await fetch(`${API_BASE}${FAVORITE_TEAMS_URL}`, {
            method: 'PUT',
            headers: {
                'Authorization': `Bearer ${authToken}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ teams: favoriteTeamsState.teams, revision: favoriteTeamsState.revision })
        });
        if (response.ok) {
            applyFavoriteTeamsPayload(await response.json());
            favoriteTeamsState.saving = false;
            renderFavoriteTeams();
            setFavoriteTeamsMessage('Saved. Changes reach the assistant within a minute.', 'success');
            announceFavoriteTeams('Favourite teams saved');
            return;
        }
        favoriteTeamsState.saving = false;
        if (response.status === 409) {
            await loadFavoriteTeams(true);
            setFavoriteTeamsMessage(FAVORITE_TEAM_STALE_MESSAGE, 'error');
        } else if (response.status === 422) {
            setFavoriteTeamsMessage(FAVORITE_TEAM_REJECTED_MESSAGE, 'error');
        } else if (response.status === 403) {
            setFavoriteTeamsMessage("You don't have permission to change this setting.", 'error');
        } else {
            setFavoriteTeamsMessage("Couldn't save your favourite teams. Try again in a moment.", 'error');
        }
    } catch (error) {
        console.error('Failed to save favourite teams:', error);
        setFavoriteTeamsMessage("Couldn't save your favourite teams. Check your connection and try again.", 'error');
    }
    favoriteTeamsState.saving = false;
    updateFavoriteTeamsControls();
}

// Listeners for the card's static markup, attached once.
function initFavoriteTeams() {
    if (favoriteTeamsState.wired) return;
    const save = favoriteTeamsEl('favorite-teams-save');
    const add = favoriteTeamsEl('favorite-team-add');
    const input = favoriteTeamsEl('favorite-team-name');
    if (!save || !add || !input) return;
    favoriteTeamsState.wired = true;
    save.addEventListener('click', saveFavoriteTeams);
    add.addEventListener('click', addFavoriteTeam);
    input.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
            event.preventDefault();
            addFavoriteTeam();
        }
    });
    input.addEventListener('input', () => setFavoriteNameError(''));
}

window.addEventListener('beforeunload', event => {
    if (favoriteTeamsDirty()) {
        event.preventDefault();
        event.returnValue = '';
    }
});

// Leaving the tab with unsaved favourites asks first (showTab is app.js's).
(function guardFavoriteTeamsOnTabSwitch() {
    if (typeof window.showTab !== 'function') return;
    const original = window.showTab;
    window.showTab = function (tabName) {
        if (tabName !== 'conversation-context' && favoriteTeamsDirty() && !window.confirm(FAVORITE_TEAM_LEAVE_MESSAGE)) {
            return undefined;
        }
        return original.apply(this, arguments);
    };
})();

// ============================================================================
// SPORTS TEAMS
// ============================================================================

async function loadSportsTeams() {
    try {
        const response = await fetch(`${API_BASE}/api/conversation/sports-teams`, {
            headers: {
                'Authorization': `Bearer ${authToken}`
            }
        });

        if (!response.ok) {
            throw new Error('Failed to load sports teams');
        }

        const teams = await response.json();
        renderSportsTeams(teams);

    } catch (error) {
        console.error('Failed to load sports teams:', error);
        showError('Failed to load sports teams');
    }
}

function renderSportsTeams(teams) {
    const container = document.getElementById('sports-teams-container');

    if (!teams || teams.length === 0) {
        container.innerHTML = '<p class="col-span-3 text-gray-400 text-sm">No sports teams configured. Click "Add Team" to create disambiguation rules.</p>';
        return;
    }

    container.innerHTML = teams.map(team => `
        <div class="bg-dark-bg border border-dark-border rounded-lg p-4">
            <div class="flex justify-between items-start mb-2">
                <div>
                    <h4 class="font-medium text-white">${escapeHtml(team.team_name || 'Unknown Team')}</h4>
                </div>
                <span class="px-2 py-1 rounded text-xs ${team.requires_disambiguation ? 'bg-yellow-900/30 text-yellow-400' : 'bg-gray-900/30 text-gray-400'}">
                    ${team.requires_disambiguation ? 'Needs Clarification' : 'No Clarification'}
                </span>
            </div>
            ${team.options && team.options.length > 0 ? `
                <div class="mt-2">
                    <span class="text-gray-500 text-xs">Options:</span>
                    <div class="flex flex-wrap gap-1 mt-1">
                        ${team.options.map(opt => `
                            <span class="px-2 py-0.5 bg-blue-900/30 text-blue-400 rounded text-xs">
                                ${escapeHtml(opt.label || opt.id)} ${opt.sport ? `(${escapeHtml(opt.sport)})` : ''}
                            </span>
                        `).join('')}
                    </div>
                </div>
            ` : ''}
        </div>
    `).join('');
}

function showCreateSportsTeamModal() {
    const modal = document.createElement('div');
    modal.id = 'sports-team-modal';
    modal.className = 'fixed inset-0 bg-black/50 flex items-center justify-center z-50';
    modal.innerHTML = `
        <div class="bg-dark-card border border-dark-border rounded-lg p-6 w-full max-w-lg">
            <h3 class="text-lg font-semibold text-white mb-4">Add Sports Team Disambiguation</h3>

            <div class="bg-blue-900/20 border border-blue-700/50 rounded-lg p-3 mb-4">
                <p class="text-blue-200 text-xs">
                    Use this to add teams with ambiguous names (e.g., "Giants" could be NY Giants NFL or SF Giants MLB).
                    Add at least 2 options to disambiguate.
                </p>
            </div>

            <div class="space-y-4">
                <div>
                    <label for="new-team-name" class="block text-sm font-medium text-gray-400 mb-2">Ambiguous Team Name</label>
                    <input type="text" id="new-team-name" placeholder="e.g., Giants, Cardinals, Jets"
                        class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white">
                    <p class="text-xs text-gray-500 mt-1">The ambiguous name that needs clarification</p>
                </div>

                <div class="flex items-center">
                    <input type="checkbox" id="new-team-requires-disambiguation" checked
                        class="w-4 h-4 bg-dark-bg border border-dark-border rounded">
                    <label for="new-team-requires-disambiguation" class="ml-2 text-sm text-gray-400">Requires Disambiguation</label>
                </div>

                <div>
                    <p class="block text-sm font-medium text-gray-400 mb-2">Option 1</p>
                    <div class="grid grid-cols-3 gap-2">
                        <input type="text" id="option1-id" placeholder="ID (e.g., ny-giants)"
                            class="px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white text-sm">
                        <input type="text" id="option1-label" placeholder="Label (e.g., NY Giants)"
                            class="px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white text-sm">
                        <input type="text" id="option1-sport" placeholder="Sport (e.g., NFL)"
                            class="px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white text-sm">
                    </div>
                </div>

                <div>
                    <p class="block text-sm font-medium text-gray-400 mb-2">Option 2</p>
                    <div class="grid grid-cols-3 gap-2">
                        <input type="text" id="option2-id" placeholder="ID (e.g., sf-giants)"
                            class="px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white text-sm">
                        <input type="text" id="option2-label" placeholder="Label (e.g., SF Giants)"
                            class="px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white text-sm">
                        <input type="text" id="option2-sport" placeholder="Sport (e.g., MLB)"
                            class="px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white text-sm">
                    </div>
                </div>
            </div>

            <div class="flex gap-3 mt-6">
                <button onclick="createSportsTeam()"
                    class="flex-1 px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm font-medium transition-colors">
                    Create
                </button>
                <button onclick="closeModal('sports-team-modal')"
                    class="flex-1 px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-sm font-medium transition-colors">
                    Cancel
                </button>
            </div>
        </div>
    `;

    document.getElementById('modals-container').appendChild(modal);
}

async function createSportsTeam() {
    try {
        const teamName = document.getElementById('new-team-name').value;
        if (!teamName) {
            showError('Team name is required');
            return;
        }

        // Build options array
        const options = [];

        const opt1Id = document.getElementById('option1-id').value;
        const opt1Label = document.getElementById('option1-label').value;
        const opt1Sport = document.getElementById('option1-sport').value;
        if (opt1Id && opt1Label) {
            options.push({ id: opt1Id, label: opt1Label, sport: opt1Sport || '' });
        }

        const opt2Id = document.getElementById('option2-id').value;
        const opt2Label = document.getElementById('option2-label').value;
        const opt2Sport = document.getElementById('option2-sport').value;
        if (opt2Id && opt2Label) {
            options.push({ id: opt2Id, label: opt2Label, sport: opt2Sport || '' });
        }

        if (options.length < 2) {
            showError('At least 2 options are required for disambiguation');
            return;
        }

        const data = {
            team_name: teamName,
            requires_disambiguation: document.getElementById('new-team-requires-disambiguation').checked,
            options: options
        };

        const response = await fetch(`${API_BASE}/api/conversation/sports-teams`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${authToken}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(data)
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.detail || 'Failed to create sports team');
        }

        showSuccess('Sports team disambiguation added');
        closeModal('sports-team-modal');
        loadSportsTeams();

    } catch (error) {
        console.error('Failed to create sports team:', error);
        showError(error.message || 'Failed to create sports team');
    }
}

// ============================================================================
// ANALYTICS
// ============================================================================

async function loadConversationAnalytics() {
    try {
        // Use the summary endpoint for aggregated stats
        const response = await fetch(`${API_BASE}/api/conversation/analytics/summary?hours=24`, {
            headers: {
                'Authorization': `Bearer ${authToken}`
            }
        });

        if (!response.ok) {
            throw new Error('Failed to load analytics');
        }

        const summary = await response.json();
        renderConversationAnalytics(summary);

    } catch (error) {
        console.error('Failed to load analytics:', error);
        // Don't show error - analytics might not exist yet
        const container = document.getElementById('conversation-analytics-container');
        container.innerHTML = '<p class="col-span-4 text-gray-400 text-sm">No analytics data available yet.</p>';
    }
}

function renderConversationAnalytics(summary) {
    const container = document.getElementById('conversation-analytics-container');

    if (!summary) {
        container.innerHTML = '<p class="col-span-4 text-gray-400 text-sm">No analytics data available yet.</p>';
        return;
    }

    // Get event types breakdown
    const eventTypes = summary.events_by_type || {};
    const eventTypesList = Object.entries(eventTypes)
        .map(([type, count]) => `<span class="text-xs">${type}: ${count}</span>`)
        .join(', ') || 'None';

    container.innerHTML = `
        <div class="bg-dark-bg border border-dark-border rounded-lg p-4">
            <div class="text-2xl font-bold text-blue-400">${summary.total_events || 0}</div>
            <div class="text-sm text-gray-400 mt-1">Total Events (24h)</div>
        </div>
        <div class="bg-dark-bg border border-dark-border rounded-lg p-4">
            <div class="text-2xl font-bold text-green-400">${summary.recent_sessions || 0}</div>
            <div class="text-sm text-gray-400 mt-1">Active Sessions</div>
        </div>
        <div class="bg-dark-bg border border-dark-border rounded-lg p-4">
            <div class="text-2xl font-bold text-purple-400">${summary.avg_session_length ? summary.avg_session_length.toFixed(1) : 'N/A'}</div>
            <div class="text-sm text-gray-400 mt-1">Avg Session Length</div>
        </div>
        <div class="bg-dark-bg border border-dark-border rounded-lg p-4 col-span-1">
            <div class="text-sm font-medium text-yellow-400 mb-1">Events by Type</div>
            <div class="text-xs text-gray-400">${eventTypesList}</div>
        </div>
    `;
}
