/**
 * Guest Context Management
 * Handles device fingerprinting and guest identification for multi-guest support.
 *
 * Uses vanilla JavaScript (no ES modules) to match existing frontend patterns.
 * FingerprintJS loaded via CDN in index.html.
 */

// ============================================================================
// Constants
// ============================================================================

const DEVICE_ID_KEY = 'athena_device_id';
const SESSION_ID_KEY = 'athena_session_id';
const USER_CONTEXT_KEY = 'athena_user_context';

// API base URL (same origin)
const GUEST_API_BASE = window.location.origin;

// ============================================================================
// Initialization
// ============================================================================

/**
 * Initialize device fingerprint and load user context.
 * Called after authentication is confirmed.
 */
async function initializeGuestContext() {
    try {
        // Get or generate device ID
        let deviceId = localStorage.getItem(DEVICE_ID_KEY);
        if (!deviceId) {
            deviceId = await generateDeviceFingerprint();
            localStorage.setItem(DEVICE_ID_KEY, deviceId);
            console.log('Generated new device ID:', deviceId.substring(0, 16) + '...');
        }

        // Get or generate session ID
        let sessionId = localStorage.getItem(SESSION_ID_KEY);
        if (!sessionId) {
            sessionId = crypto.randomUUID();
            localStorage.setItem(SESSION_ID_KEY, sessionId);
        }

        // Check if we have an existing session on the backend
        const existingSession = await getSessionByDevice(deviceId);

        if (existingSession) {
            // Restore user context
            saveUserContext({
                guest_id: existingSession.guest_id,
                guest_name: existingSession.guest_name,
                device_id: deviceId,
                session_id: sessionId
            });
            showSuccess('Welcome back, ' + existingSession.guest_name + '!');
            console.log('Restored session for:', existingSession.guest_name);
        } else {
            // Check if there's an active reservation with guests
            const currentGuests = await getCurrentGuests();
            if (currentGuests && currentGuests.length > 0) {
                // Show guest selection modal
                showGuestSelectionModal(currentGuests, deviceId, sessionId);
            }
        }
    } catch (error) {
        console.error('Failed to initialize guest context:', error);
        // Continue without guest context - not critical for app function
    }
}

/**
 * Generate device fingerprint using FingerprintJS.
 */
async function generateDeviceFingerprint() {
    try {
        // FingerprintJS is loaded via CDN in index.html
        if (typeof FingerprintJS === 'undefined') {
            console.warn('FingerprintJS not loaded, using fallback UUID');
            return 'fallback-' + crypto.randomUUID();
        }
        const fp = await FingerprintJS.load();
        const result = await fp.get();
        return result.visitorId;
    } catch (error) {
        console.error('FingerprintJS error:', error);
        // Fallback to random UUID
        return 'fallback-' + crypto.randomUUID();
    }
}

// ============================================================================
// API Functions
// ============================================================================

/**
 * Get current guests from admin backend.
 */
async function getCurrentGuests() {
    try {
        const response = await fetch(GUEST_API_BASE + '/api/guests/current');
        if (!response.ok) return [];
        return await response.json();
    } catch (error) {
        console.error('Failed to fetch current guests:', error);
        return [];
    }
}

/**
 * Get user session by device ID.
 */
async function getSessionByDevice(deviceId) {
    try {
        const response = await fetch(GUEST_API_BASE + '/api/user-sessions/device/' + encodeURIComponent(deviceId));
        if (response.status === 404) return null;
        if (!response.ok) throw new Error('Failed to fetch session');
        return await response.json();
    } catch (error) {
        console.error('Failed to fetch session:', error);
        return null;
    }
}

/**
 * Create or update user session on backend.
 */
async function saveSessionToBackend(context) {
    try {
        const response = await fetch(GUEST_API_BASE + '/api/user-sessions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: context.session_id,
                guest_id: context.guest_id,
                device_id: context.device_id,
                device_type: 'web',
                preferences: {}
            })
        });
        if (!response.ok) throw new Error('Failed to save session');
        return await response.json();
    } catch (error) {
        console.error('Failed to save session:', error);
        return null;
    }
}

/**
 * Add a new guest to the current reservation.
 */
async function addGuestToCurrentReservation(name, email, phone) {
    try {
        const response = await fetch(GUEST_API_BASE + '/api/guests/current/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, email, phone })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to add guest');
        }

        return await response.json();
    } catch (error) {
        console.error('Failed to add guest:', error);
        throw error;
    }
}

// ============================================================================
// Local Storage Functions
// ============================================================================

/**
 * Save user context to localStorage.
 */
function saveUserContext(context) {
    context.last_updated = new Date().toISOString();
    localStorage.setItem(USER_CONTEXT_KEY, JSON.stringify(context));
}

/**
 * Get user context from localStorage.
 */
function getUserContext() {
    const contextStr = localStorage.getItem(USER_CONTEXT_KEY);
    if (!contextStr) return null;
    try {
        return JSON.parse(contextStr);
    } catch (e) {
        return null;
    }
}

/**
 * Clear user context (logout from guest context).
 */
function clearUserContext() {
    localStorage.removeItem(USER_CONTEXT_KEY);
    localStorage.removeItem(SESSION_ID_KEY);
    // Keep device_id - it persists across sessions
}

/**
 * Get current device ID.
 */
function getDeviceId() {
    return localStorage.getItem(DEVICE_ID_KEY);
}

/**
 * Get current session ID.
 */
function getSessionId() {
    return localStorage.getItem(SESSION_ID_KEY);
}

// ============================================================================
// Modal Functions
// ============================================================================

/**
 * Show guest selection modal.
 */
function showGuestSelectionModal(guests, deviceId, sessionId) {
    const modal = document.getElementById('guest-selection-modal');
    const guestList = document.getElementById('guest-list');

    if (!modal || !guestList) {
        console.error('Guest selection modal elements not found');
        return;
    }

    // Build guest buttons
    guestList.innerHTML = guests.map(function(guest) {
        const primaryLabel = guest.is_primary ? ' (Primary Guest)' : '';
        return '<button class="w-full bg-dark-bg border border-dark-border text-white py-3 px-4 rounded hover:bg-dark-accent/20 text-left transition-colors" ' +
               'onclick="selectGuest(' + guest.id + ', \'' + escapeHtmlAttr(guest.name) + '\', \'' + deviceId + '\', \'' + sessionId + '\')">' +
               escapeHtml(guest.name) + '<span class="text-gray-400 text-sm">' + primaryLabel + '</span>' +
               '</button>';
    }).join('');

    // Show modal
    modal.classList.remove('hidden');

    // Handle "I'm someone else" button
    const addNewBtn = document.getElementById('btn-add-new-guest');
    if (addNewBtn) {
        addNewBtn.onclick = function() {
            modal.classList.add('hidden');
            showAddGuestModal(deviceId, sessionId);
        };
    }
}

/**
 * Escape HTML for attribute values (stricter than content escaping).
 */
function escapeHtmlAttr(str) {
    if (!str) return '';
    return str.replace(/[&<>"']/g, function(match) {
        const escapeMap = {
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;'
        };
        return escapeMap[match];
    });
}

/**
 * Handle guest selection.
 */
async function selectGuest(guestId, guestName, deviceId, sessionId) {
    const context = {
        guest_id: guestId,
        guest_name: guestName,
        device_id: deviceId,
        session_id: sessionId
    };

    // Save to backend
    await saveSessionToBackend(context);

    // Save locally
    saveUserContext(context);

    // Hide modal
    const modal = document.getElementById('guest-selection-modal');
    if (modal) {
        modal.classList.add('hidden');
    }

    showSuccess('Welcome, ' + guestName + '!');
}

/**
 * Show add new guest modal.
 */
function showAddGuestModal(deviceId, sessionId) {
    const modal = document.getElementById('add-guest-modal');
    if (!modal) {
        console.error('Add guest modal not found');
        return;
    }

    // Clear form
    const nameInput = document.getElementById('new-guest-name');
    const emailInput = document.getElementById('new-guest-email');
    const phoneInput = document.getElementById('new-guest-phone');

    if (nameInput) nameInput.value = '';
    if (emailInput) emailInput.value = '';
    if (phoneInput) phoneInput.value = '';

    // Store context for form submission
    modal.dataset.deviceId = deviceId;
    modal.dataset.sessionId = sessionId;

    // Show modal
    modal.classList.remove('hidden');

    // Focus name input
    if (nameInput) nameInput.focus();
}

/**
 * Hide add guest modal.
 */
function hideAddGuestModal() {
    const modal = document.getElementById('add-guest-modal');
    if (modal) {
        modal.classList.add('hidden');
    }
}

/**
 * Handle add guest form submission.
 */
async function handleAddGuestSubmit(event) {
    event.preventDefault();

    const modal = document.getElementById('add-guest-modal');
    const nameInput = document.getElementById('new-guest-name');
    const emailInput = document.getElementById('new-guest-email');
    const phoneInput = document.getElementById('new-guest-phone');

    if (!modal || !nameInput) return;

    const name = nameInput.value.trim();
    const email = emailInput ? emailInput.value.trim() : '';
    const phone = phoneInput ? phoneInput.value.trim() : '';
    const deviceId = modal.dataset.deviceId;
    const sessionId = modal.dataset.sessionId;

    if (!name) {
        showError('Please enter your name');
        return;
    }

    try {
        // Add guest to current reservation
        const result = await addGuestToCurrentReservation(name, email || null, phone || null);
        const newGuest = result.guest;

        // Create session for new guest
        await selectGuest(newGuest.id, newGuest.name, deviceId, sessionId);

        // Hide modal
        hideAddGuestModal();

    } catch (error) {
        showError(error.message || 'Failed to add guest');
    }
}

// ============================================================================
// Event Listeners
// ============================================================================

// Initialize form handler on DOM ready
document.addEventListener('DOMContentLoaded', function() {
    const addGuestForm = document.getElementById('add-guest-form');
    if (addGuestForm) {
        addGuestForm.addEventListener('submit', handleAddGuestSubmit);
    }
});

// ============================================================================
// Expose to Global Scope (vanilla JS pattern)
// ============================================================================

window.initializeGuestContext = initializeGuestContext;
window.selectGuest = selectGuest;
window.showAddGuestModal = showAddGuestModal;
window.hideAddGuestModal = hideAddGuestModal;
window.getUserContext = getUserContext;
window.getDeviceId = getDeviceId;
window.getSessionId = getSessionId;
window.clearUserContext = clearUserContext;
