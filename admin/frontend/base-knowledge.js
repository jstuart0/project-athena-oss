// Base Knowledge Management JavaScript
// Handles all CRUD operations and UI interactions for base knowledge management

const BASE_KNOWLEDGE_API = '/api/base-knowledge';

// ============================================================================
// DATA MANAGEMENT
// ============================================================================

let allKnowledge = [];

// Keep in step with shared/knowledge_tiers.py (tests/unit/test_base_knowledge_tier_parity.py).
const TIER_LABELS = {
    both: 'Everyone',
    guest: 'Guests only',
    household: 'Household',
    owner: 'Owner only'
};
const OWNER_TIER = 'owner';
const OWNER_CATEGORY = 'owner';
const OWNER_NAME_KEYS = ['owner_name', 'name'];
const MAX_BULK_TIER_IDS = 500;
const OWNER_BANNER_DISMISSED_KEY = 'kb-owner-banner-dismissed';

const selectedKnowledgeIds = new Set();

async function loadBaseKnowledge() {
    try {
        const response = await fetch(BASE_KNOWLEDGE_API, {
            headers: { 'Authorization': `Bearer ${getToken()}` }
        });

        if (!response.ok) throw new Error('Failed to load base knowledge');

        allKnowledge = await response.json();
        selectedKnowledgeIds.clear();
        updateOwnerBanner();
        filterBaseKnowledge();
    } catch (error) {
        console.error('Error loading base knowledge:', error);
        showError('base-knowledge-container', 'Failed to load base knowledge entries');
    }
}

function filterBaseKnowledge() {
    const category = document.getElementById('knowledge-category-filter')?.value;
    const appliesTo = document.getElementById('knowledge-applies-filter')?.value;
    const enabledOnly = document.getElementById('knowledge-enabled-filter')?.checked;

    let filtered = allKnowledge;

    if (category) {
        filtered = filtered.filter(k => k.category === category);
    }
    if (appliesTo) {
        filtered = filtered.filter(k => k.applies_to === appliesTo);
    }
    if (enabledOnly) {
        filtered = filtered.filter(k => k.enabled);
    }

    renderBaseKnowledge(filtered);
}

function renderBaseKnowledge(knowledge) {
    const container = document.getElementById('base-knowledge-container');

    if (!knowledge || knowledge.length === 0) {
        container.innerHTML = `
            <div class="text-center text-gray-400 py-8">
                <div class="text-2xl mb-2">📚</div>
                <p>No base knowledge entries found</p>
                <p class="text-sm mt-2">Create your first entry to get started</p>
            </div>
        `;
        pruneSelection([]);
        updateSelectionToolbar();
        return;
    }

    // Sort by priority (highest first), then by created_at
    knowledge.sort((a, b) => {
        if (b.priority !== a.priority) {
            return b.priority - a.priority;
        }
        return new Date(b.created_at) - new Date(a.created_at);
    });

    pruneSelection(knowledge);

    container.innerHTML = `
        <table class="crud-table">
            <thead>
                <tr>
                    <th><input type="checkbox" data-kb-select-all aria-label="Select all shown entries"></th>
                    <th><span class="inline-flex items-center gap-1">Category${typeof infoIcon === 'function' ? infoIcon('knowledge-category') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Key${typeof infoIcon === 'function' ? infoIcon('knowledge-key') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Value${typeof infoIcon === 'function' ? infoIcon('knowledge-value') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Who hears this entry${typeof infoIcon === 'function' ? infoIcon('knowledge-applies-to') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Priority${typeof infoIcon === 'function' ? infoIcon('knowledge-priority') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Status${typeof infoIcon === 'function' ? infoIcon('knowledge-status') : ''}</span></th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                ${knowledge.map(entry => `
                    <tr>
                        <td><input type="checkbox" data-kb-select="${escapeHtml(String(entry.id))}" aria-label="${escapeHtml('Select ' + entry.key)}"></td>
                        <td>
                            <span class="px-2 py-1 text-xs rounded-full ${getCategoryColor(entry.category)}">
                                ${escapeHtml(String(entry.category).toUpperCase())}
                            </span>
                        </td>
                        <td class="text-white font-medium">${escapeHtml(entry.key)}</td>
                        <td class="max-w-md">
                            <div class="text-gray-300 truncate" title="${escapeHtml(entry.value)}">
                                ${escapeHtml(entry.value)}
                            </div>
                            ${descriptionHtml(entry)}
                        </td>
                        <td>
                            <span class="px-2 py-1 text-xs rounded-full ${getAppliesToColor(entry.applies_to)}">
                                ${escapeHtml(tierLabel(entry.applies_to))}
                            </span>
                        </td>
                        <td class="text-center">
                            <span class="${priorityClass(entry.priority)}">
                                ${escapeHtml(String(Number(entry.priority)))}
                            </span>
                        </td>
                        <td>
                            <span class="px-2 py-1 text-xs rounded-full ${statusClass(entry)}">
                                ${statusLabel(entry)}
                            </span>
                        </td>
                        <td>
                            <div class="flex gap-2">
                                <button type="button" data-kb-action="edit" data-kb-id="${escapeHtml(String(entry.id))}"
                                        class="px-3 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm transition-colors">
                                    Edit
                                </button>
                                <button type="button" data-kb-action="toggle" data-kb-id="${escapeHtml(String(entry.id))}" data-kb-enable="${toggleTarget(entry)}"
                                        class="px-3 py-1 ${toggleClass(entry)} text-white rounded text-sm transition-colors">
                                    ${toggleLabel(entry)}
                                </button>
                                <button type="button" data-kb-action="delete" data-kb-id="${escapeHtml(String(entry.id))}"
                                        class="px-3 py-1 bg-red-600 hover:bg-red-700 text-white rounded text-sm transition-colors">
                                    Delete
                                </button>
                            </div>
                        </td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;

    container.querySelectorAll('input[data-kb-select]').forEach(box => {
        box.checked = selectedKnowledgeIds.has(parseInt(box.dataset.kbSelect, 10));
    });
    updateSelectionToolbar();
}

// Row-template helpers: each returns a constant string or escaped markup.
function tierLabel(tier) {
    return Object.prototype.hasOwnProperty.call(TIER_LABELS, tier) ? TIER_LABELS[tier] : 'Unknown: ' + tier;
}

function priorityClass(priority) {
    const n = Number(priority);
    return n > 50 ? 'text-green-400' : n > 0 ? 'text-blue-400' : 'text-gray-400';
}

function descriptionHtml(entry) {
    return entry.description
        ? '<div class="text-xs text-gray-500 mt-1">' + escapeHtml(entry.description) + '</div>'
        : '';
}

function statusClass(entry) {
    return entry.enabled ? 'bg-green-900/30 text-green-400' : 'bg-gray-700 text-gray-400';
}

function statusLabel(entry) {
    return entry.enabled ? '✓ Enabled' : '✗ Disabled';
}

function toggleTarget(entry) {
    return entry.enabled ? 'false' : 'true';
}

function toggleClass(entry) {
    return entry.enabled ? 'bg-yellow-600 hover:bg-yellow-700' : 'bg-green-600 hover:bg-green-700';
}

function toggleLabel(entry) {
    return entry.enabled ? 'Disable' : 'Enable';
}

// ============================================================================
// OWNER-ONLY BANNER AND SELECTION
// ============================================================================

function updateOwnerBanner() {
    const banner = document.getElementById('knowledge-owner-banner');
    if (!banner) return;
    const count = allKnowledge.filter(k => k.enabled && k.applies_to === OWNER_TIER).length;
    let dismissed = false;
    try {
        dismissed = sessionStorage.getItem(OWNER_BANNER_DISMISSED_KEY) === '1';
    } catch (e) {
        dismissed = false;
    }
    document.getElementById('knowledge-owner-banner-count').textContent = String(count);
    banner.classList.toggle('hidden', count === 0 || dismissed);
}

function dismissOwnerBanner() {
    try {
        sessionStorage.setItem(OWNER_BANNER_DISMISSED_KEY, '1');
    } catch (e) {
        // Dismissal then lasts until the next reload.
    }
    document.getElementById('knowledge-owner-banner').classList.add('hidden');
}

function showOwnerOnlyEntries() {
    document.getElementById('knowledge-applies-filter').value = OWNER_TIER;
    filterBaseKnowledge();
}

function pruneSelection(shown) {
    const shownIds = new Set(shown.map(k => k.id));
    selectedKnowledgeIds.forEach(id => {
        if (!shownIds.has(id)) selectedKnowledgeIds.delete(id);
    });
}

function updateSelectionToolbar() {
    const count = selectedKnowledgeIds.size;
    const button = document.getElementById('knowledge-move-household');
    if (!button) return;
    button.textContent = 'Move ' + count + ' to Household';
    button.setAttribute('aria-disabled', count === 0 ? 'true' : 'false');
    button.title = count === 0 ? 'Select entries first' : 'Move the selected entries to Household';
    document.getElementById('knowledge-selected-count').textContent =
        count === 0 ? 'No entries selected' : count + ' selected';

    const all = document.querySelector('#base-knowledge-container input[data-kb-select-all]');
    if (all) {
        const boxes = document.querySelectorAll('#base-knowledge-container input[data-kb-select]');
        all.checked = boxes.length > 0 && Array.from(boxes).every(box => box.checked);
    }
}

function handleSelectionChange(target) {
    if (target.matches('input[data-kb-select-all]')) {
        const boxes = document.querySelectorAll('#base-knowledge-container input[data-kb-select]');
        selectedKnowledgeIds.clear();
        let capped = false;
        boxes.forEach(box => {
            const id = parseInt(box.dataset.kbSelect, 10);
            if (target.checked && selectedKnowledgeIds.size < MAX_BULK_TIER_IDS) {
                selectedKnowledgeIds.add(id);
                box.checked = true;
            } else {
                box.checked = false;
                capped = capped || target.checked;
            }
        });
        if (capped) showToast('At most ' + MAX_BULK_TIER_IDS + ' entries can be moved at once, so the first ' + MAX_BULK_TIER_IDS + ' are selected', 'error');
    } else if (target.matches('input[data-kb-select]')) {
        const id = parseInt(target.dataset.kbSelect, 10);
        if (target.checked && selectedKnowledgeIds.size >= MAX_BULK_TIER_IDS) {
            target.checked = false;
            showToast('At most ' + MAX_BULK_TIER_IDS + ' entries can be moved at once', 'error');
        } else if (target.checked) {
            selectedKnowledgeIds.add(id);
        } else {
            selectedKnowledgeIds.delete(id);
        }
    } else {
        return;
    }
    updateSelectionToolbar();
}

function handleRowAction(button) {
    const id = parseInt(button.dataset.kbId, 10);
    const action = button.dataset.kbAction;
    if (action === 'edit') showEditKnowledgeModal(id);
    else if (action === 'toggle') toggleKnowledge(id, button.dataset.kbEnable === 'true');
    else if (action === 'delete') deleteKnowledge(id);
}

async function moveSelectedToHousehold() {
    const ids = Array.from(selectedKnowledgeIds);
    if (ids.length === 0) return;
    const movesOwnerOnly = allKnowledge.some(k => selectedKnowledgeIds.has(k.id) && k.applies_to === OWNER_TIER);
    if (movesOwnerOnly && !confirm('Move ' + ids.length + ' entries to Household? Voice, SMS and anyone at home will hear them.')) {
        return;
    }
    try {
        const response = await fetch(BASE_KNOWLEDGE_API + '/bulk-tier', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ ids: ids, applies_to: 'household' })
        });
        if (!response.ok) throw new Error(await responseErrorMessage(response, 'Failed to move the selected entries', 'Only your admin owner account can move entries out of Owner only.'));
        showToast('Moved ' + ids.length + ' entries to Household', 'success');
        loadBaseKnowledge();
    } catch (error) {
        console.error('Error moving knowledge:', error);
        showToast(error.message || 'Failed to move the selected entries', 'error');
    }
}

// Turns an error response into one readable sentence. The API's detail is a
// string, a {"error": ...} object (owner-role refusals) or a validation list.
async function responseErrorMessage(response, fallback, roleMessage) {
    let detail;
    try {
        detail = (await response.json()).detail;
    } catch (e) {
        return fallback;
    }
    if (typeof detail === 'string') return detail;
    if (detail && detail.error === 'insufficient_role') {
        return roleMessage || 'Your account does not have permission to change this entry.';
    }
    if (Array.isArray(detail) && detail.length > 0 && typeof detail[0].msg === 'string') {
        return detail[0].msg;
    }
    return fallback;
}

// ============================================================================
// MODAL OPERATIONS
// ============================================================================

function setTierRadios(checkedTier, required) {
    document.querySelectorAll('input[name="knowledge-tier"]').forEach(radio => {
        radio.checked = radio.value === checkedTier;
        radio.required = required;
    });
}

function selectedTier() {
    const radio = document.querySelector('input[name="knowledge-tier"]:checked');
    return radio ? radio.value : '';
}

function modalCategoryAndKey() {
    const editing = !!document.getElementById('knowledge-id').value;
    const category = editing
        ? document.getElementById('knowledge-category-display').value
        : document.getElementById('knowledge-category').value;
    const key = editing
        ? document.getElementById('knowledge-key-display').value
        : document.getElementById('knowledge-key').value;
    return { category: category.trim().toLowerCase(), key: key };
}

// Mirrors the server rule: an Owner-category entry is Owner only, except owner_name and name.
function refreshKnowledgeModalState() {
    const { category, key } = modalCategoryAndKey();
    document.getElementById('knowledge-instruction-warning').classList.toggle('hidden', category !== 'instruction');
    const ownerLocked = category === OWNER_CATEGORY && !OWNER_NAME_KEYS.includes(key);
    document.querySelectorAll('input[name="knowledge-tier"]').forEach(radio => {
        radio.disabled = ownerLocked && radio.value !== OWNER_TIER;
    });
    if (ownerLocked && selectedTier() && selectedTier() !== OWNER_TIER) {
        setTierRadios(OWNER_TIER, !document.getElementById('knowledge-id').value);
    }
}

let knowledgeModalOpener = null;

function openKnowledgeModal(initialFocusId) {
    knowledgeModalOpener = document.activeElement;
    const modal = document.getElementById('knowledge-modal');
    modal.classList.remove('hidden');
    modal.classList.add('flex');
    document.addEventListener('keydown', closeKnowledgeModalOnEscape);
    const target = document.getElementById(initialFocusId) || firstAvailableTierRadio();
    if (target) target.focus();
}

function firstAvailableTierRadio() {
    const radios = Array.from(document.querySelectorAll('input[name="knowledge-tier"]'));
    return radios.find(r => r.checked && !r.disabled) || radios.find(r => !r.disabled) || null;
}

function closeKnowledgeModalOnEscape(event) {
    if (event.key === 'Escape') closeKnowledgeModal();
}

function setKnowledgeModalMode(editing) {
    const category = document.getElementById('knowledge-category');
    const key = document.getElementById('knowledge-key');
    category.disabled = editing;
    category.required = !editing;
    key.disabled = editing;
    key.required = !editing;
    document.getElementById('knowledge-category-field').classList.toggle('hidden', editing);
    document.getElementById('knowledge-key-field').classList.toggle('hidden', editing);
    document.getElementById('knowledge-category-display-field').classList.toggle('hidden', !editing);
    document.getElementById('knowledge-key-display-field').classList.toggle('hidden', !editing);
}

function showCreateKnowledgeModal() {
    // Reset form: no audience is pre-selected, so the choice is always deliberate
    document.getElementById('knowledge-form').reset();
    document.getElementById('knowledge-id').value = '';
    document.getElementById('knowledge-enabled').checked = true;
    document.getElementById('knowledge-priority').value = '0';
    document.getElementById('knowledge-modal-title').textContent = 'Add Base Knowledge Entry';
    document.getElementById('knowledge-tier-legacy-note').classList.add('hidden');
    setKnowledgeModalMode(false);
    setTierRadios('', true);
    refreshKnowledgeModalState();
    openKnowledgeModal('knowledge-category');
}

function showEditKnowledgeModal(knowledgeId) {
    const entry = allKnowledge.find(k => k.id === knowledgeId);
    if (!entry) {
        showToast('Entry not found', 'error');
        return;
    }

    // Populate form
    document.getElementById('knowledge-id').value = entry.id;
    document.getElementById('knowledge-category-display').value = entry.category;
    document.getElementById('knowledge-key-display').value = entry.key;
    document.getElementById('knowledge-value').value = entry.value;
    document.getElementById('knowledge-priority').value = entry.priority;
    document.getElementById('knowledge-description').value = entry.description || '';
    document.getElementById('knowledge-enabled').checked = entry.enabled;
    document.getElementById('knowledge-modal-title').textContent = 'Edit Base Knowledge Entry';
    setKnowledgeModalMode(true);

    const knownTier = Object.prototype.hasOwnProperty.call(TIER_LABELS, entry.applies_to);
    setTierRadios(knownTier ? entry.applies_to : '', false);
    const note = document.getElementById('knowledge-tier-legacy-note');
    note.textContent = knownTier
        ? ''
        : tierLabel(entry.applies_to) + '. Nobody hears this entry. Choose who can hear it, or leave it as it is.';
    note.classList.toggle('hidden', knownTier);
    refreshKnowledgeModalState();
    openKnowledgeModal();
}

function closeKnowledgeModal(event) {
    // Only close if clicking outside or close button
    if (event && event.target !== event.currentTarget && !event.target.classList.contains('close-btn')) {
        return;
    }

    document.removeEventListener('keydown', closeKnowledgeModalOnEscape);
    const modal = document.getElementById('knowledge-modal');
    modal.classList.add('hidden');
    modal.classList.remove('flex');
    if (knowledgeModalOpener && knowledgeModalOpener.isConnected && typeof knowledgeModalOpener.focus === 'function') {
        knowledgeModalOpener.focus();
    }
    knowledgeModalOpener = null;
}

async function saveKnowledge(event) {
    event.preventDefault();

    const knowledgeId = document.getElementById('knowledge-id').value;
    const isEdit = !!knowledgeId;
    const tier = selectedTier();

    if (!isEdit && !tier) {
        showToast('Choose who can hear this entry', 'error');
        document.getElementById('knowledge-tier-both').focus();
        return;
    }

    const data = {
        value: document.getElementById('knowledge-value').value,
        priority: parseInt(document.getElementById('knowledge-priority').value, 10) || 0,
        description: document.getElementById('knowledge-description').value || null,
        enabled: document.getElementById('knowledge-enabled').checked
    };
    if (tier) data.applies_to = tier;
    if (!isEdit) {
        data.category = document.getElementById('knowledge-category').value;
        data.key = document.getElementById('knowledge-key').value;
    }

    try {
        const url = isEdit ? `${BASE_KNOWLEDGE_API}/${knowledgeId}` : BASE_KNOWLEDGE_API;
        const method = isEdit ? 'PUT' : 'POST';

        const response = await fetch(url, {
            method: method,
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify(data)
        });

        if (!response.ok) throw new Error(await responseErrorMessage(response, 'Failed to save entry', 'Only your admin owner account can move an entry out of Owner only.'));

        showToast(isEdit ? 'Entry updated successfully' : 'Entry created successfully', 'success');
        closeKnowledgeModal();
        loadBaseKnowledge();
    } catch (error) {
        console.error('Error saving knowledge:', error);
        showToast(error.message || 'Failed to save entry', 'error');
    }
}

async function toggleKnowledge(knowledgeId, enabled) {
    try {
        const response = await fetch(`${BASE_KNOWLEDGE_API}/${knowledgeId}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${getToken()}`
            },
            body: JSON.stringify({ enabled })
        });

        if (!response.ok) throw new Error(await responseErrorMessage(response, 'Failed to toggle entry'));

        showToast(`Entry ${enabled ? 'enabled' : 'disabled'} successfully`, 'success');
        loadBaseKnowledge();
    } catch (error) {
        console.error('Error toggling knowledge:', error);
        showToast(error.message || 'Failed to toggle entry', 'error');
    }
}

async function deleteKnowledge(knowledgeId) {
    const entry = allKnowledge.find(k => k.id === knowledgeId);
    if (!entry) return;

    const warning = entry.applies_to === OWNER_TIER ? '\n\nThis is an Owner-only entry; only your admin owner account can delete it.' : '';
    if (!confirm('Are you sure you want to delete the entry "' + entry.key + '"?\n\nThis action cannot be undone.' + warning)) {
        return;
    }

    try {
        const response = await fetch(`${BASE_KNOWLEDGE_API}/${knowledgeId}`, {
            method: 'DELETE',
            headers: {
                'Authorization': `Bearer ${getToken()}`
            }
        });

        if (!response.ok) throw new Error(await responseErrorMessage(response, 'Failed to delete entry', 'Only your admin owner account can delete an Owner-only entry.'));

        showToast('Entry deleted successfully', 'success');
        loadBaseKnowledge();
    } catch (error) {
        console.error('Error deleting knowledge:', error);
        showToast(error.message || 'Failed to delete entry', 'error');
    }
}

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

function getCategoryColor(category) {
    const colors = {
        'property': 'bg-purple-900/30 text-purple-400',
        'location': 'bg-blue-900/30 text-blue-400',
        'user': 'bg-green-900/30 text-green-400',
        'temporal': 'bg-orange-900/30 text-orange-400',
        'general': 'bg-gray-700 text-gray-300',
        'owner': 'bg-purple-900/30 text-purple-300',
        'instruction': 'bg-yellow-900/30 text-yellow-300'
    };
    return Object.prototype.hasOwnProperty.call(colors, category) ? colors[category] : colors.general;
}

function getAppliesToColor(appliesTo) {
    // Light -300 text on a dark tint keeps at least 4.5:1 contrast.
    const colors = {
        'both': 'bg-blue-900/30 text-blue-300',
        'guest': 'bg-green-900/30 text-green-300',
        'household': 'bg-teal-900/30 text-teal-300',
        'owner': 'bg-purple-900/30 text-purple-300'
    };
    return Object.prototype.hasOwnProperty.call(colors, appliesTo) ? colors[appliesTo] : 'bg-gray-700 text-gray-300';
}

// escapeHtml and showNotification are now provided by utils.js

function showError(containerId, message) {
    const container = document.getElementById(containerId);
    container.innerHTML = `
        <div class="p-4 bg-red-900/20 border border-red-500/30 rounded-lg">
            <div class="flex items-start gap-3">
                <span class="text-2xl">X</span>
                <div>
                    <div class="text-red-400 font-semibold">Error</div>
                    <div class="text-sm text-red-300 mt-1" data-kb-error-message></div>
                </div>
            </div>
        </div>
    `;
    container.querySelector('[data-kb-error-message]').textContent = message;
}

// ============================================================================
// INITIALIZATION
// ============================================================================

// Auto-load when tab is shown
if (typeof window.tabChangeCallbacks === 'undefined') {
    window.tabChangeCallbacks = {};
}

window.tabChangeCallbacks['base-knowledge'] = loadBaseKnowledge;

function initBaseKnowledgeUi() {
    const container = document.getElementById('base-knowledge-container');
    if (!container) return;
    container.addEventListener('click', event => {
        const button = event.target.closest('button[data-kb-action]');
        if (button) handleRowAction(button);
    });
    container.addEventListener('change', event => handleSelectionChange(event.target));
    document.getElementById('knowledge-move-household').addEventListener('click', moveSelectedToHousehold);
    document.getElementById('knowledge-show-owner').addEventListener('click', showOwnerOnlyEntries);
    document.getElementById('knowledge-dismiss-banner').addEventListener('click', dismissOwnerBanner);
    ['knowledge-category', 'knowledge-key'].forEach(id => {
        document.getElementById(id).addEventListener('input', refreshKnowledgeModalState);
    });
}

initBaseKnowledgeUi();
