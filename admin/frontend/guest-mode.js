/**
 * Guest Mode Management - Frontend JavaScript
 * Handles CRUD operations for guest entries and display of guest history
 */

// State variables
let guestHistoryData = [];
let guestHistoryOffset = 0;
let guestHistoryLimit = 20;
let guestHistoryTotal = 0;
let filterTimeout = null;

// Test mode state
let guestTestModeEnabled = false;

// Multi-guest display state
let expandedReservations = new Set();
let guestsByEvent = {};

// ============================================================================
// Data Loading Functions
// ============================================================================

async function loadGuestModeData() {
    await Promise.all([
        loadCurrentGuests(),
        loadUpcomingGuests(),
        loadGuestHistory(0),
        updateGuestModeStatus()
    ]);
}

async function loadCurrentGuests() {
    const container = document.getElementById('current-guests-container');

    try {
        const data = await apiRequest('/api/guest-mode/events/current');

        if (data.entries.length === 0) {
            container.innerHTML = `
                <div class="text-center text-gray-400 py-4">
                    <p>No current guests</p>
                </div>
            `;
            return;
        }

        // Fetch guest counts for all events
        const eventIds = data.entries.map(e => e.id).join(',');
        let guestCounts = {};
        try {
            const guestsData = await apiRequest(`/api/guests/by-events?event_ids=${eventIds}`);
            guestsByEvent = guestsData;
            for (const [eventId, guests] of Object.entries(guestsData)) {
                guestCounts[eventId] = guests.length;
            }
        } catch (e) {
            console.error('Failed to fetch guest counts', e);
        }

        container.innerHTML = data.entries.map(entry => {
            const guestCount = guestCounts[entry.id] || entry.guest_count || 1;
            const isExpanded = expandedReservations.has(entry.id);
            const guests = guestsByEvent[entry.id] || [];

            return `
            <div class="p-4 bg-dark-bg rounded-lg mb-3 border-l-4 ${entry.is_test ? 'border-yellow-500' : 'border-green-500'}" id="reservation-${entry.id}">
                <div class="flex justify-between items-start">
                    <div class="flex-1">
                        <div class="font-medium text-white flex items-center gap-2">
                            ${entry.is_test ? '<span class="text-yellow-400 text-xs">[TEST]</span>' : ''}
                            ${escapeHtml(entry.guest_name || 'Unknown')}
                            ${guestCount > 1 ? `<span class="text-xs bg-blue-600 text-white px-2 py-0.5 rounded-full">+${guestCount - 1} guests</span>` : ''}
                            ${infoIcon('guest-current-name')}
                        </div>
                        <div class="text-sm text-gray-400 mt-1">
                            ${formatDateRange(entry.checkin, entry.checkout)}
                        </div>
                        ${entry.guest_email ? `<div class="text-xs text-gray-500 mt-1">${escapeHtml(entry.guest_email)}</div>` : ''}
                    </div>
                    <div class="flex items-center gap-2">
                        <button onclick="toggleReservationExpand(${entry.id})"
                            class="text-blue-400 hover:text-blue-300 text-sm px-2 py-1 rounded border border-blue-400/30 hover:border-blue-400">
                            ${isExpanded ? '▼ Hide' : '▶ Show'} Guests
                        </button>
                        <span class="badge ${entry.is_test ? 'bg-yellow-600/20 text-yellow-400' : 'badge-success'}">
                            ${entry.is_test ? 'Test' : 'Active'}
                        </span>
                    </div>
                </div>

                ${isExpanded ? `
                <div class="mt-4 pt-4 border-t border-dark-border">
                    <div class="flex justify-between items-center mb-2">
                        <span class="text-sm text-gray-400">Guests (${guests.length})</span>
                        <button onclick="showAddGuestToReservationModal(${entry.id})"
                            class="text-xs text-green-400 hover:text-green-300">
                            + Add Guest
                        </button>
                    </div>
                    <div class="space-y-2">
                        ${guests.map(guest => `
                            <div class="flex justify-between items-center p-2 bg-dark-card rounded">
                                <div>
                                    <span class="text-white">${escapeHtml(guest.name)}</span>
                                    ${guest.is_primary ? '<span class="text-xs text-blue-400 ml-2">(Primary)</span>' : ''}
                                    ${guest.is_test ? '<span class="text-xs text-yellow-400 ml-2">[Test]</span>' : ''}
                                </div>
                                <div class="flex gap-2 text-xs">
                                    ${guest.email ? `<span class="text-gray-500">${escapeHtml(guest.email)}</span>` : ''}
                                    ${!guest.is_primary ? `
                                        <button onclick="deleteGuestFromReservation(${guest.id})" class="text-red-400 hover:text-red-300">Remove</button>
                                    ` : ''}
                                </div>
                            </div>
                        `).join('')}
                    </div>
                </div>
                ` : ''}
            </div>
            `;
        }).join('');
    } catch (error) {
        const errorMsg = error?.message || error?.detail || String(error) || 'Unknown error';
        container.innerHTML = `
            <div class="text-center text-red-400 py-4">
                <p>Failed to load: ${escapeHtml(errorMsg)}</p>
            </div>
        `;
    }
}

async function loadUpcomingGuests() {
    const container = document.getElementById('upcoming-guests-container');
    const daysSelect = document.getElementById('upcoming-days');
    const days = parseInt(daysSelect.value) || 30;

    try {
        const data = await apiRequest(`/api/guest-mode/events/upcoming?days=${days}`);

        if (data.entries.length === 0) {
            container.innerHTML = `
                <div class="text-center text-gray-400 py-4">
                    <p>No upcoming guests in next ${days} days</p>
                </div>
            `;
            return;
        }

        container.innerHTML = data.entries.map(entry => `
            <div class="p-4 bg-dark-bg rounded-lg mb-3 border-l-4 ${entry.is_test ? 'border-yellow-500' : 'border-blue-500'}">
                <div class="flex justify-between items-start">
                    <div>
                        <div class="font-medium text-white flex items-center gap-2">
                            ${entry.is_test ? '<span class="text-yellow-400 text-xs">[TEST]</span>' : ''}
                            ${escapeHtml(entry.guest_name || 'Unknown')}
                            ${entry.guest_count > 1 ? `<span class="text-xs bg-blue-600 text-white px-2 py-0.5 rounded-full">+${entry.guest_count - 1} guests</span>` : ''}
                            ${infoIcon('guest-upcoming-name')}
                        </div>
                        <div class="text-sm text-gray-400 mt-1">
                            ${formatDateRange(entry.checkin, entry.checkout)}
                        </div>
                        <div class="text-xs text-gray-500 mt-1">
                            Arriving in ${getDaysUntil(entry.checkin)} days
                        </div>
                    </div>
                    <span class="badge ${entry.is_test ? 'bg-yellow-600/20 text-yellow-400' : (entry.status === 'confirmed' ? 'badge-success' : 'badge-warning')}">
                        ${entry.is_test ? 'Test' : entry.status}
                    </span>
                </div>
            </div>
        `).join('');
    } catch (error) {
        const errorMsg = error?.message || error?.detail || String(error) || 'Unknown error';
        container.innerHTML = `
            <div class="text-center text-red-400 py-4">
                <p>Failed to load: ${escapeHtml(errorMsg)}</p>
            </div>
        `;
    }
}

async function loadGuestHistory(offset = 0) {
    const container = document.getElementById('guest-history-table-body');
    const includeDeleted = document.getElementById('show-deleted-guests').checked;
    const searchName = document.getElementById('guest-search').value.trim();

    guestHistoryOffset = offset;

    try {
        let url = `/api/guest-mode/history?limit=${guestHistoryLimit}&offset=${offset}&include_deleted=${includeDeleted}&include_test=${guestTestModeEnabled}`;
        if (searchName) {
            url += `&guest_name=${encodeURIComponent(searchName)}`;
        }

        const data = await apiRequest(url);
        guestHistoryData = data.entries;
        guestHistoryTotal = data.total;

        if (data.entries.length === 0) {
            container.innerHTML = `
                <tr>
                    <td colspan="7" class="text-center text-gray-400 py-8">
                        <div class="text-2xl mb-2">🏠</div>
                        <p>No guest entries found</p>
                    </td>
                </tr>
            `;
            updatePagination();
            return;
        }

        container.innerHTML = data.entries.map(entry => `
            <tr class="${entry.deleted_at ? 'opacity-50' : ''} ${entry.is_test ? 'bg-yellow-900/10' : ''}">
                <td class="text-white font-medium">
                    <span class="flex items-center gap-1">
                        ${entry.is_test ? '<span class="text-yellow-400 text-xs">[TEST]</span>' : ''}
                        ${escapeHtml(entry.guest_name || 'Unknown')}
                        ${entry.created_by === 'manual' ? '<span class="ml-2 text-xs text-purple-400">(manual)</span>' : ''}
                        ${entry.guest_count > 1 ? `<span class="text-xs bg-blue-600 text-white px-2 py-0.5 rounded-full ml-1">+${entry.guest_count - 1}</span>` : ''}
                        ${infoIcon('guest-history-name')}
                    </span>
                </td>
                <td>${formatDate(entry.checkin)}</td>
                <td>${formatDate(entry.checkout)}</td>
                <td>
                    ${entry.guest_email ? `<div class="text-xs">${escapeHtml(entry.guest_email)}</div>` : ''}
                    ${entry.guest_phone ? `<div class="text-xs text-gray-500">${escapeHtml(entry.guest_phone)}</div>` : ''}
                    ${!entry.guest_email && !entry.guest_phone ? '<span class="text-gray-500">-</span>' : ''}
                </td>
                <td>
                    <span class="tag ${entry.source === 'manual' ? 'tag-source' : 'tag-backend'}">
                        ${entry.source}
                    </span>
                </td>
                <td>
                    <span class="badge ${entry.is_test ? 'bg-yellow-600/20 text-yellow-400' : getStatusClass(entry.status)}">
                        ${entry.is_test ? 'Test' : entry.status}
                    </span>
                </td>
                <td>
                    <div class="flex gap-2">
                        <button onclick="editGuestEntry(${entry.id})"
                            class="text-blue-400 hover:text-blue-300 text-sm">
                            Edit
                        </button>
                        ${entry.created_by === 'manual' && !entry.deleted_at ? `
                            <button onclick="deleteGuestEntry(${entry.id})"
                                class="text-red-400 hover:text-red-300 text-sm">
                                Delete
                            </button>
                        ` : ''}
                    </div>
                </td>
            </tr>
        `).join('');

        updatePagination();
    } catch (error) {
        const errorMsg = error?.message || error?.detail || String(error) || 'Unknown error';
        container.innerHTML = `
            <tr>
                <td colspan="7" class="text-center text-red-400 py-8">
                    <p>Failed to load guest history: ${escapeHtml(errorMsg)}</p>
                </td>
            </tr>
        `;
    }
}

async function updateGuestModeStatus() {
    const banner = document.getElementById('guest-mode-status-banner');

    try {
        const config = await apiRequest('/api/guest-mode/config');
        const currentData = await apiRequest('/api/guest-mode/events/current');

        const hasCurrentGuests = currentData.entries.length > 0;
        const isEnabled = config.enabled;

        if (hasCurrentGuests) {
            banner.innerHTML = `
                <div class="p-4 bg-green-900/20 border border-green-700/50 rounded-lg flex items-center gap-3">
                    <span class="text-2xl">🏠</span>
                    <div>
                        <div class="text-green-200 font-medium">Guest Mode Active</div>
                        <div class="text-green-300/70 text-sm">
                            ${currentData.entries.length} guest(s) currently staying
                        </div>
                    </div>
                </div>
            `;
        } else if (isEnabled) {
            banner.innerHTML = `
                <div class="p-4 bg-blue-900/20 border border-blue-700/50 rounded-lg flex items-center gap-3">
                    <span class="text-2xl">📅</span>
                    <div>
                        <div class="text-blue-200 font-medium">Guest Mode Ready</div>
                        <div class="text-blue-300/70 text-sm">
                            System is monitoring calendar for upcoming guests
                        </div>
                    </div>
                </div>
            `;
        } else {
            banner.innerHTML = `
                <div class="p-4 bg-gray-900/20 border border-gray-700/50 rounded-lg flex items-center gap-3">
                    <span class="text-2xl">⏸️</span>
                    <div>
                        <div class="text-gray-200 font-medium">Guest Mode Disabled</div>
                        <div class="text-gray-300/70 text-sm">
                            Enable guest mode to track rental bookings
                        </div>
                    </div>
                </div>
            `;
        }
    } catch (error) {
        banner.innerHTML = `
            <div class="p-4 bg-yellow-900/20 border border-yellow-700/50 rounded-lg flex items-center gap-3">
                <span class="text-2xl">⚠️</span>
                <div>
                    <div class="text-yellow-200 font-medium">Status Unknown</div>
                    <div class="text-yellow-300/70 text-sm">
                        Unable to fetch guest mode configuration
                    </div>
                </div>
            </div>
        `;
    }
}

// ============================================================================
// Pagination
// ============================================================================

function updatePagination() {
    const countEl = document.getElementById('guest-history-count');
    const prevBtn = document.getElementById('prev-page-btn');
    const nextBtn = document.getElementById('next-page-btn');

    countEl.textContent = guestHistoryTotal;

    prevBtn.disabled = guestHistoryOffset === 0;
    nextBtn.disabled = guestHistoryOffset + guestHistoryLimit >= guestHistoryTotal;
}

// ============================================================================
// Filter Functions
// ============================================================================

function filterGuestHistory() {
    // Debounce the search
    if (filterTimeout) {
        clearTimeout(filterTimeout);
    }
    filterTimeout = setTimeout(() => {
        loadGuestHistory(0);
    }, 300);
}

// ============================================================================
// Modal Functions
// ============================================================================

function showAddGuestModal() {
    const modal = document.getElementById('guest-modal');
    const title = document.getElementById('guest-modal-title');
    const form = document.getElementById('guest-form');
    const warning = document.getElementById('guest-form-ical-warning');

    // Reset form
    form.reset();
    document.getElementById('guest-entry-id').value = '';
    document.getElementById('guest-entry-source').value = 'manual';

    // Set default dates
    const now = new Date();
    const tomorrow = new Date(now);
    tomorrow.setDate(tomorrow.getDate() + 1);
    const nextWeek = new Date(now);
    nextWeek.setDate(nextWeek.getDate() + 7);

    document.getElementById('guest-checkin').value = formatDateTimeLocal(tomorrow);
    document.getElementById('guest-checkout').value = formatDateTimeLocal(nextWeek);

    // Set test mode checkbox based on current toggle state
    const testCheckbox = document.getElementById('guest-is-test');
    if (testCheckbox) {
        testCheckbox.checked = guestTestModeEnabled;
    }

    // Enable all fields for new entry
    setFormFieldsEnabled(true);
    warning.classList.add('hidden');

    title.textContent = 'Add Guest Entry';
    modal.classList.remove('hidden');
    modal.classList.add('flex');
}

async function editGuestEntry(id) {
    const entry = guestHistoryData.find(e => e.id === id);
    if (!entry) {
        console.error('Entry not found:', id);
        return;
    }

    const modal = document.getElementById('guest-modal');
    const title = document.getElementById('guest-modal-title');
    const warning = document.getElementById('guest-form-ical-warning');

    // Populate form
    document.getElementById('guest-entry-id').value = entry.id;
    document.getElementById('guest-entry-source').value = entry.created_by;
    document.getElementById('guest-name').value = entry.guest_name || '';
    document.getElementById('guest-status').value = entry.status || 'confirmed';
    document.getElementById('guest-checkin').value = formatDateTimeLocal(new Date(entry.checkin));
    document.getElementById('guest-checkout').value = formatDateTimeLocal(new Date(entry.checkout));
    document.getElementById('guest-email').value = entry.guest_email || '';
    document.getElementById('guest-phone').value = entry.guest_phone || '';
    document.getElementById('guest-notes').value = entry.notes || '';

    // Set field editability based on source
    const isManual = entry.created_by === 'manual';
    setFormFieldsEnabled(isManual);

    if (isManual) {
        warning.classList.add('hidden');
    } else {
        warning.classList.remove('hidden');
    }

    title.textContent = 'Edit Guest Entry';
    modal.classList.remove('hidden');
    modal.classList.add('flex');
}

function setFormFieldsEnabled(isManual) {
    const fields = ['guest-name', 'guest-checkin', 'guest-checkout', 'guest-email', 'guest-phone'];
    fields.forEach(fieldId => {
        const field = document.getElementById(fieldId);
        if (field) {
            field.disabled = !isManual;
            field.classList.toggle('opacity-50', !isManual);
            field.classList.toggle('cursor-not-allowed', !isManual);
        }
    });
}

function closeGuestModal(event) {
    if (event && event.target !== event.currentTarget) return;
    const modal = document.getElementById('guest-modal');
    modal.classList.add('hidden');
    modal.classList.remove('flex');
}

// ============================================================================
// CRUD Operations
// ============================================================================

async function saveGuestEntry(event) {
    event.preventDefault();

    const id = document.getElementById('guest-entry-id').value;
    const source = document.getElementById('guest-entry-source').value;
    const isNew = !id;

    const data = {
        guest_name: document.getElementById('guest-name').value,
        checkin: new Date(document.getElementById('guest-checkin').value).toISOString(),
        checkout: new Date(document.getElementById('guest-checkout').value).toISOString(),
        guest_email: document.getElementById('guest-email').value || null,
        guest_phone: document.getElementById('guest-phone').value || null,
        notes: document.getElementById('guest-notes').value || null,
        status: document.getElementById('guest-status').value,
        is_test: document.getElementById('guest-is-test')?.checked || false
    };

    // For non-manual entries, only send allowed fields
    if (!isNew && source !== 'manual') {
        const allowedData = {
            notes: data.notes,
            status: data.status
        };
        Object.assign(data, {});
        Object.assign(data, allowedData);
    }

    try {
        if (isNew) {
            await apiRequest('/api/guest-mode/events', {
                method: 'POST',
                body: JSON.stringify(data)
            });
        } else {
            await apiRequest(`/api/guest-mode/events/${id}`, {
                method: 'PATCH',
                body: JSON.stringify(data)
            });
        }

        closeGuestModal();
        loadGuestModeData();
    } catch (error) {
        const errorMsg = error?.message || error?.detail || String(error) || 'Unknown error';
        alert(`Failed to save guest entry: ${errorMsg}`);
    }
}

async function deleteGuestEntry(id) {
    if (!confirm('Are you sure you want to delete this guest entry? This action cannot be undone.')) {
        return;
    }

    try {
        await apiRequest(`/api/guest-mode/events/${id}`, {
            method: 'DELETE'
        });
        loadGuestModeData();
    } catch (error) {
        const errorMsg = error?.message || error?.detail || String(error) || 'Unknown error';
        alert(`Failed to delete guest entry: ${errorMsg}`);
    }
}

// ============================================================================
// Helper Functions
// ============================================================================

function formatDateRange(checkin, checkout) {
    const checkInDate = new Date(checkin);
    const checkOutDate = new Date(checkout);

    const options = { month: 'short', day: 'numeric' };
    const checkInStr = checkInDate.toLocaleDateString('en-US', options);
    const checkOutStr = checkOutDate.toLocaleDateString('en-US', options);

    return `${checkInStr} - ${checkOutStr}`;
}

function formatDate(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
        year: 'numeric'
    });
}

function formatDateTimeLocal(date) {
    const d = new Date(date);
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function getDaysUntil(dateStr) {
    const now = new Date();
    const target = new Date(dateStr);
    const diffTime = target - now;
    const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
    return Math.max(0, diffDays);
}

function getStatusClass(status) {
    switch (status) {
        case 'confirmed':
            return 'badge-success';
        case 'pending':
            return 'badge-warning';
        case 'cancelled':
            return 'bg-red-900/20 text-red-400';
        default:
            return '';
    }
}

// Note: escapeHtml is defined in app.js

// ============================================================================
// Test Mode Functions
// ============================================================================

function toggleGuestTestMode() {
    guestTestModeEnabled = document.getElementById('guest-test-mode').checked;

    // Show/hide clear test data button
    const clearBtn = document.getElementById('clear-test-data-btn');
    if (clearBtn) {
        clearBtn.classList.toggle('hidden', !guestTestModeEnabled);
    }

    // Reload data with test filter
    loadGuestModeData();
}

async function clearTestData() {
    if (!confirm('Are you sure you want to delete ALL test reservations and guests? This cannot be undone.')) {
        return;
    }

    try {
        const result = await apiRequest('/api/guest-mode/test-data', {
            method: 'DELETE'
        });

        alert(`Cleared ${result.deleted_events} test reservations and ${result.deleted_guests} test guests.`);
        loadGuestModeData();
    } catch (error) {
        const errorMsg = error?.message || error?.detail || String(error) || 'Unknown error';
        alert(`Failed to clear test data: ${errorMsg}`);
    }
}

// ============================================================================
// Multi-Guest Functions
// ============================================================================

function toggleReservationExpand(eventId) {
    if (expandedReservations.has(eventId)) {
        expandedReservations.delete(eventId);
    } else {
        expandedReservations.add(eventId);
        loadGuestsForEvent(eventId);
    }
    // Re-render current guests to update expand state
    loadCurrentGuests();
}

async function loadGuestsForEvent(eventId) {
    try {
        const guests = await apiRequest(`/api/guests?calendar_event_id=${eventId}`);
        guestsByEvent[eventId] = guests;
    } catch (error) {
        console.error('Failed to load guests for event', eventId, error);
    }
}

function showAddGuestToReservationModal(eventId) {
    // Store the event ID for the form submission
    document.getElementById('add-guest-event-id').value = eventId;

    // Show modal
    const modal = document.getElementById('add-guest-to-reservation-modal');
    modal.classList.remove('hidden');
    modal.classList.add('flex');

    // Clear form
    document.getElementById('new-guest-name-for-reservation').value = '';
    document.getElementById('new-guest-email-for-reservation').value = '';
    document.getElementById('new-guest-phone-for-reservation').value = '';
}

function closeAddGuestToReservationModal() {
    const modal = document.getElementById('add-guest-to-reservation-modal');
    modal.classList.add('hidden');
    modal.classList.remove('flex');
}

async function saveGuestToReservation(event) {
    event.preventDefault();

    const eventId = document.getElementById('add-guest-event-id').value;
    const name = document.getElementById('new-guest-name-for-reservation').value;
    const email = document.getElementById('new-guest-email-for-reservation').value || null;
    const phone = document.getElementById('new-guest-phone-for-reservation').value || null;

    try {
        await apiRequest('/api/guests', {
            method: 'POST',
            body: JSON.stringify({
                calendar_event_id: parseInt(eventId),
                name: name,
                email: email,
                phone: phone,
                is_primary: false,
                is_test: guestTestModeEnabled
            })
        });

        closeAddGuestToReservationModal();

        // Refresh the guests for this event
        await loadGuestsForEvent(parseInt(eventId));
        loadCurrentGuests();

    } catch (error) {
        const errorMsg = error?.message || error?.detail || String(error) || 'Unknown error';
        alert(`Failed to add guest: ${errorMsg}`);
    }
}

async function deleteGuestFromReservation(guestId) {
    if (!confirm('Remove this guest from the reservation?')) {
        return;
    }

    try {
        await apiRequest(`/api/guests/${guestId}`, {
            method: 'DELETE'
        });
        loadGuestModeData();
    } catch (error) {
        const errorMsg = error?.message || error?.detail || String(error) || 'Unknown error';
        alert(`Failed to remove guest: ${errorMsg}`);
    }
}
