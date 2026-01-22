/**
 * SMS Messaging Management - Frontend JavaScript
 * Handles SMS settings, history, and cost tracking
 */

// State variables
let smsHistoryData = [];
let smsHistoryOffset = 0;
let smsHistoryLimit = 20;
let smsHistoryTotal = 0;

// ============================================================================
// Data Loading Functions
// ============================================================================

async function loadSMSData() {
    await Promise.all([
        loadSMSSettings(),
        loadSMSHistory(0),
        loadSMSCostSummary(),
        loadSMSCostByStay(),
        loadSMSTemplates(),
        loadScheduledSMS(),
        loadTips(),
        loadIncomingSMS()
    ]);
}

async function loadSMSSettings() {
    try {
        const data = await apiRequest('/api/sms/settings');

        // Populate form fields
        document.getElementById('sms-account-sid').value = data.twilio_account_sid || '';
        document.getElementById('sms-auth-token').value = ''; // Don't show token, just placeholder
        document.getElementById('sms-from-number').value = data.twilio_from_number || '';
        document.getElementById('sms-enabled').checked = data.enabled || false;
        document.getElementById('sms-auto-detect').checked = data.auto_detect_textable_content || false;
        document.getElementById('sms-test-mode').checked = data.test_mode || false;
        document.getElementById('sms-rate-limit').value = data.rate_limit_per_hour || 10;

        // If auth token exists, show indicator
        if (data.twilio_auth_token) {
            document.getElementById('sms-auth-token').placeholder = '********** (configured)';
        }
    } catch (error) {
        console.error('Failed to load SMS settings:', error);
        showToast('Failed to load SMS settings', 'error');
    }
}

async function saveSMSSettings() {
    try {
        const settings = {
            twilio_account_sid: document.getElementById('sms-account-sid').value || null,
            twilio_from_number: document.getElementById('sms-from-number').value || null,
            enabled: document.getElementById('sms-enabled').checked,
            auto_detect_textable_content: document.getElementById('sms-auto-detect').checked,
            test_mode: document.getElementById('sms-test-mode').checked,
            rate_limit_per_hour: parseInt(document.getElementById('sms-rate-limit').value) || 10
        };

        // Only include auth token if it was changed (not empty)
        const authToken = document.getElementById('sms-auth-token').value;
        if (authToken && authToken.trim()) {
            settings.twilio_auth_token = authToken;
        }

        await apiRequest('/api/sms/settings', {
            method: 'PUT',
            body: JSON.stringify(settings)
        });

        showToast('SMS settings saved successfully', 'success');
        await loadSMSSettings(); // Reload to confirm
    } catch (error) {
        console.error('Failed to save SMS settings:', error);
        showToast('Failed to save SMS settings: ' + (error.message || error), 'error');
    }
}

async function testSMSConnection() {
    try {
        showToast('Testing Twilio connection...', 'info');

        // For now, just verify settings exist
        const settings = await apiRequest('/api/sms/settings');

        if (!settings.twilio_account_sid || !settings.twilio_from_number) {
            showToast('Please configure Twilio credentials first', 'warning');
            return;
        }

        if (!settings.twilio_auth_token) {
            showToast('Twilio auth token not configured', 'warning');
            return;
        }

        showToast('Twilio credentials configured. Send a test SMS to verify.', 'success');
    } catch (error) {
        console.error('Failed to test SMS connection:', error);
        showToast('Connection test failed: ' + (error.message || error), 'error');
    }
}

// ============================================================================
// SMS History Functions
// ============================================================================

async function loadSMSHistory(offset = 0) {
    const container = document.getElementById('sms-history-table-body');
    const statusFilter = document.getElementById('sms-history-filter').value;

    smsHistoryOffset = Math.max(0, offset);

    try {
        let url = `/api/sms/history?limit=${smsHistoryLimit}&offset=${smsHistoryOffset}`;
        if (statusFilter) {
            url += `&status=${statusFilter}`;
        }

        const data = await apiRequest(url);
        smsHistoryData = data.history || [];
        smsHistoryTotal = data.total || 0;

        updateSMSHistoryTable();
        updateSMSHistoryPagination();
    } catch (error) {
        console.error('Failed to load SMS history:', error);
        container.innerHTML = `
            <tr>
                <td colspan="6" class="text-center text-red-400 py-8">
                    <p>Failed to load SMS history</p>
                </td>
            </tr>
        `;
    }
}

function updateSMSHistoryTable() {
    const container = document.getElementById('sms-history-table-body');

    if (smsHistoryData.length === 0) {
        container.innerHTML = `
            <tr>
                <td colspan="6" class="text-center text-gray-400 py-8">
                    <div class="text-2xl mb-2">📱</div>
                    <p>No SMS messages found</p>
                </td>
            </tr>
        `;
        return;
    }

    container.innerHTML = smsHistoryData.map(msg => `
        <tr>
            <td class="text-sm">${formatDateTime(msg.sent_at)}</td>
            <td class="text-sm">${escapeHtml(maskPhoneNumber(msg.to_number))}</td>
            <td><span class="badge badge-info">${escapeHtml(msg.content_type || 'custom')}</span></td>
            <td class="text-sm max-w-xs truncate" title="${escapeHtml(msg.content)}">${escapeHtml(truncateText(msg.content, 50))}</td>
            <td>${getStatusBadge(msg.status)}</td>
            <td class="text-sm">${msg.cost ? '$' + msg.cost.toFixed(4) : '-'}</td>
        </tr>
    `).join('');
}

function updateSMSHistoryPagination() {
    document.getElementById('sms-history-count').textContent = smsHistoryTotal;

    const prevBtn = document.getElementById('sms-prev-page-btn');
    const nextBtn = document.getElementById('sms-next-page-btn');

    prevBtn.disabled = smsHistoryOffset === 0;
    nextBtn.disabled = smsHistoryOffset + smsHistoryLimit >= smsHistoryTotal;
}

// ============================================================================
// Cost Tracking Functions
// ============================================================================

async function loadSMSCostSummary() {
    const container = document.getElementById('sms-cost-summary');

    try {
        const data = await apiRequest('/api/sms/costs/summary');

        container.innerHTML = `
            <div class="flex justify-between items-center p-3 bg-dark-bg rounded-lg">
                <span class="text-gray-400">This Month</span>
                <span class="text-white font-medium">$${(data.current_month_cost || 0).toFixed(2)}</span>
            </div>
            <div class="flex justify-between items-center p-3 bg-dark-bg rounded-lg">
                <span class="text-gray-400">Messages Sent</span>
                <span class="text-white font-medium">${data.current_month_messages || 0}</span>
            </div>
            <div class="flex justify-between items-center p-3 bg-dark-bg rounded-lg">
                <span class="text-gray-400">Last Month</span>
                <span class="text-white font-medium">$${(data.last_month_cost || 0).toFixed(2)}</span>
            </div>
            <div class="flex justify-between items-center p-3 bg-dark-bg rounded-lg">
                <span class="text-gray-400">All Time</span>
                <span class="text-white font-medium">$${(data.total_cost || 0).toFixed(2)}</span>
            </div>
        `;
    } catch (error) {
        console.error('Failed to load SMS cost summary:', error);
        container.innerHTML = `
            <div class="text-center text-gray-400 py-4">
                <p>Cost tracking not available</p>
            </div>
        `;
    }
}

async function loadSMSCostByStay() {
    const container = document.getElementById('sms-cost-by-stay');

    try {
        const data = await apiRequest('/api/sms/costs/by-stay?limit=10');

        if (!data.stays || data.stays.length === 0) {
            container.innerHTML = `
                <div class="text-center text-gray-400 py-4">
                    <p>No stay costs recorded</p>
                </div>
            `;
            return;
        }

        container.innerHTML = data.stays.map(stay => `
            <div class="flex justify-between items-center p-3 bg-dark-bg rounded-lg">
                <div>
                    <div class="text-sm text-white">${escapeHtml(stay.guest_name || 'Unknown Guest')}</div>
                    <div class="text-xs text-gray-400">${stay.message_count} messages</div>
                </div>
                <span class="text-white font-medium">$${(stay.total_cost || 0).toFixed(2)}</span>
            </div>
        `).join('');
    } catch (error) {
        console.error('Failed to load SMS cost by stay:', error);
        container.innerHTML = `
            <div class="text-center text-gray-400 py-4">
                <p>Failed to load stay costs</p>
            </div>
        `;
    }
}

// ============================================================================
// Send Test SMS
// ============================================================================

async function sendTestSMS() {
    const toNumber = document.getElementById('sms-test-to').value;
    const message = document.getElementById('sms-test-message').value;

    if (!toNumber || !message) {
        showToast('Please enter phone number and message', 'warning');
        return;
    }

    try {
        await apiRequest('/api/sms/send', {
            method: 'POST',
            body: JSON.stringify({
                phone_number: toNumber,
                content: message,
                content_type: 'test'
            })
        });

        showToast('Test SMS sent successfully', 'success');

        // Clear form
        document.getElementById('sms-test-to').value = '';
        document.getElementById('sms-test-message').value = '';

        // Refresh history
        await loadSMSHistory(0);
    } catch (error) {
        console.error('Failed to send test SMS:', error);
        showToast('Failed to send SMS: ' + (error.message || error), 'error');
    }
}

// ============================================================================
// Helper Functions
// ============================================================================

function maskPhoneNumber(phone) {
    if (!phone || phone.length < 7) return phone;
    return phone.slice(0, 3) + '****' + phone.slice(-4);
}

function truncateText(text, maxLength) {
    if (!text || text.length <= maxLength) return text;
    return text.slice(0, maxLength) + '...';
}

function formatDateTime(isoString) {
    if (!isoString) return '-';
    const date = new Date(isoString);
    return date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function getStatusBadge(status) {
    const statusConfig = {
        'sent': { class: 'badge-info', text: 'Sent' },
        'delivered': { class: 'badge-success', text: 'Delivered' },
        'failed': { class: 'badge-error', text: 'Failed' },
        'pending': { class: 'badge-warning', text: 'Pending' }
    };
    const config = statusConfig[status] || { class: 'badge-info', text: status || 'Unknown' };
    return `<span class="badge ${config.class}">${config.text}</span>`;
}

// ============================================================================
// SMS Templates Functions
// ============================================================================

async function loadSMSTemplates() {
    const container = document.getElementById('sms-templates-container');

    try {
        const data = await apiRequest('/api/sms/templates');
        // API returns array directly, not {templates: [...]}
        const templates = Array.isArray(data) ? data : (data.templates || []);

        if (templates.length === 0) {
            container.innerHTML = `
                <div class="text-center text-gray-400 py-4">
                    <p>No templates configured. Add one to get started!</p>
                </div>
            `;
            return;
        }

        container.innerHTML = templates.map(template => `
            <div class="p-4 bg-dark-bg rounded-lg border-l-4 ${template.enabled ? 'border-blue-500' : 'border-gray-600'}">
                <div class="flex justify-between items-start">
                    <div class="flex-1">
                        <div class="font-medium text-white">${escapeHtml(template.name)}</div>
                        <div class="text-xs text-gray-400 mt-1">${escapeHtml(template.category || 'custom')}</div>
                        <div class="text-sm text-gray-300 mt-2 line-clamp-2">${escapeHtml(template.body)}</div>
                        ${template.variables && template.variables.length > 0 ? `
                            <div class="text-xs text-gray-500 mt-2">Variables: ${template.variables.join(', ')}</div>
                        ` : ''}
                    </div>
                    <div class="flex gap-2 ml-4">
                        <button onclick="showEditTemplateModal(${template.id})"
                            class="px-2 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-xs">
                            Edit
                        </button>
                        <button onclick="toggleTemplate(${template.id}, ${!template.enabled})"
                            class="px-2 py-1 ${template.enabled ? 'bg-gray-600' : 'bg-green-600'} hover:opacity-80 text-white rounded text-xs">
                            ${template.enabled ? 'Disable' : 'Enable'}
                        </button>
                        <button onclick="deleteTemplate(${template.id})"
                            class="px-2 py-1 bg-red-600 hover:bg-red-700 text-white rounded text-xs">
                            Delete
                        </button>
                    </div>
                </div>
            </div>
        `).join('');
    } catch (error) {
        console.error('Failed to load SMS templates:', error);
        container.innerHTML = `
            <div class="text-center text-gray-400 py-4">
                <p>Failed to load templates</p>
            </div>
        `;
    }
}

function showAddTemplateModal() {
    const modalHtml = `
        <div id="template-modal" class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
            <div class="bg-dark-card rounded-lg p-6 w-full max-w-lg mx-4">
                <h3 class="text-lg font-semibold text-white mb-4">Add SMS Template</h3>
                <form id="add-template-form" class="space-y-4">
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Template Name</label>
                        <input type="text" id="template-name" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700 focus:border-blue-500" required>
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Category</label>
                        <select id="template-category" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700">
                            <option value="info">Info</option>
                            <option value="recommendations">Recommendations</option>
                            <option value="property">Property</option>
                            <option value="checkout">Checkout</option>
                            <option value="system">System</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Subject (optional)</label>
                        <input type="text" id="template-subject" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700">
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Message Body</label>
                        <textarea id="template-body" rows="4" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700" required placeholder="Use {variable} for dynamic content"></textarea>
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Variables (comma-separated)</label>
                        <input type="text" id="template-variables" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700" placeholder="location, forecast">
                    </div>
                    <div class="flex items-center gap-2">
                        <input type="checkbox" id="template-enabled" checked class="rounded">
                        <label for="template-enabled" class="text-sm text-gray-400">Enabled</label>
                    </div>
                    <div class="flex justify-end gap-3 mt-6">
                        <button type="button" onclick="closeTemplateModal()" class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded">Cancel</button>
                        <button type="submit" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded">Save Template</button>
                    </div>
                </form>
            </div>
        </div>
    `;
    document.body.insertAdjacentHTML('beforeend', modalHtml);
    document.getElementById('add-template-form').addEventListener('submit', saveNewTemplate);
}

function closeTemplateModal() {
    const modal = document.getElementById('template-modal');
    if (modal) modal.remove();
}

async function saveNewTemplate(e) {
    e.preventDefault();
    const variables = document.getElementById('template-variables').value
        .split(',')
        .map(v => v.trim())
        .filter(v => v);

    const template = {
        name: document.getElementById('template-name').value,
        category: document.getElementById('template-category').value,
        subject: document.getElementById('template-subject').value || null,
        body: document.getElementById('template-body').value,
        variables: variables.length > 0 ? variables : null,
        enabled: document.getElementById('template-enabled').checked
    };

    try {
        await apiRequest('/api/sms/templates', {
            method: 'POST',
            body: JSON.stringify(template)
        });
        closeTemplateModal();
        showToast('Template created successfully', 'success');
        await loadSMSTemplates();
    } catch (error) {
        showToast('Failed to create template: ' + (error.message || error), 'error');
    }
}

async function toggleTemplate(id, enabled) {
    try {
        await apiRequest(`/api/sms/templates/${id}`, {
            method: 'PATCH',
            body: JSON.stringify({ enabled })
        });
        await loadSMSTemplates();
        showToast('Template updated', 'success');
    } catch (error) {
        showToast('Failed to update template', 'error');
    }
}

async function deleteTemplate(id) {
    if (!confirm('Delete this template?')) return;
    try {
        await apiRequest(`/api/sms/templates/${id}`, { method: 'DELETE' });
        await loadSMSTemplates();
        showToast('Template deleted', 'success');
    } catch (error) {
        showToast('Failed to delete template', 'error');
    }
}

async function showEditTemplateModal(templateId) {
    try {
        const template = await apiRequest(`/api/sms/templates/${templateId}`);
        const modalHtml = `
            <div id="template-modal" class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
                <div class="bg-dark-card rounded-lg p-6 w-full max-w-lg mx-4">
                    <h3 class="text-lg font-semibold text-white mb-4">Edit SMS Template</h3>
                    <form id="edit-template-form" class="space-y-4">
                        <input type="hidden" id="edit-template-id" value="${template.id}">
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Template Name</label>
                            <input type="text" id="edit-template-name" value="${escapeHtml(template.name || '')}" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700" required>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Category</label>
                            <select id="edit-template-category" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700">
                                <option value="info" ${template.category === 'info' ? 'selected' : ''}>Info</option>
                                <option value="recommendations" ${template.category === 'recommendations' ? 'selected' : ''}>Recommendations</option>
                                <option value="property" ${template.category === 'property' ? 'selected' : ''}>Property</option>
                                <option value="checkout" ${template.category === 'checkout' ? 'selected' : ''}>Checkout</option>
                                <option value="system" ${template.category === 'system' ? 'selected' : ''}>System</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Subject (optional)</label>
                            <input type="text" id="edit-template-subject" value="${escapeHtml(template.subject || '')}" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700">
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Message Body</label>
                            <textarea id="edit-template-body" rows="4" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700" required>${escapeHtml(template.body || '')}</textarea>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Variables (comma-separated)</label>
                            <input type="text" id="edit-template-variables" value="${template.variables ? template.variables.join(', ') : ''}" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700">
                        </div>
                        <div class="flex items-center gap-2">
                            <input type="checkbox" id="edit-template-enabled" ${template.enabled ? 'checked' : ''} class="rounded">
                            <label for="edit-template-enabled" class="text-sm text-gray-400">Enabled</label>
                        </div>
                        <div class="flex justify-end gap-3 mt-6">
                            <button type="button" onclick="closeTemplateModal()" class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded">Cancel</button>
                            <button type="submit" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded">Save Changes</button>
                        </div>
                    </form>
                </div>
            </div>
        `;
        document.body.insertAdjacentHTML('beforeend', modalHtml);
        document.getElementById('edit-template-form').addEventListener('submit', saveEditedTemplate);
    } catch (error) {
        showToast('Failed to load template: ' + (error.message || error), 'error');
    }
}

async function saveEditedTemplate(e) {
    e.preventDefault();
    const templateId = document.getElementById('edit-template-id').value;
    const variablesStr = document.getElementById('edit-template-variables').value;
    const template = {
        name: document.getElementById('edit-template-name').value,
        category: document.getElementById('edit-template-category').value,
        subject: document.getElementById('edit-template-subject').value || null,
        body: document.getElementById('edit-template-body').value,
        variables: variablesStr ? variablesStr.split(',').map(v => v.trim()).filter(v => v) : [],
        enabled: document.getElementById('edit-template-enabled').checked
    };

    try {
        await apiRequest(`/api/sms/templates/${templateId}`, {
            method: 'PATCH',
            body: JSON.stringify(template)
        });
        closeTemplateModal();
        showToast('Template updated successfully', 'success');
        await loadSMSTemplates();
    } catch (error) {
        showToast('Failed to update template: ' + (error.message || error), 'error');
    }
}

// ============================================================================
// Scheduled SMS Functions
// ============================================================================

async function loadScheduledSMS() {
    const container = document.getElementById('scheduled-sms-container');

    try {
        const data = await apiRequest('/api/sms/scheduled');
        // API returns array directly
        const scheduled = Array.isArray(data) ? data : (data.scheduled || []);

        if (scheduled.length === 0) {
            container.innerHTML = `
                <div class="text-center text-gray-400 py-4">
                    <p>No scheduled messages configured.</p>
                </div>
            `;
            return;
        }

        container.innerHTML = scheduled.map(item => `
            <div class="p-4 bg-dark-bg rounded-lg border-l-4 ${item.enabled ? 'border-purple-500' : 'border-gray-600'}">
                <div class="flex justify-between items-start">
                    <div class="flex-1">
                        <div class="font-medium text-white">${escapeHtml(item.name)}</div>
                        <div class="text-xs text-gray-400 mt-1">
                            Trigger: ${item.trigger_type}
                            ${item.trigger_offset_hours ? `(${item.trigger_offset_hours} hours)` : ''}
                        </div>
                        ${item.template_id ? `
                            <div class="text-xs text-gray-500 mt-1">Using template #${item.template_id}</div>
                        ` : ''}
                    </div>
                    <div class="flex gap-2 ml-4">
                        <button onclick="showEditScheduledModal(${item.id})"
                            class="px-2 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-xs">
                            Edit
                        </button>
                        <button onclick="toggleScheduled(${item.id}, ${!item.enabled})"
                            class="px-2 py-1 ${item.enabled ? 'bg-gray-600' : 'bg-green-600'} hover:opacity-80 text-white rounded text-xs">
                            ${item.enabled ? 'Disable' : 'Enable'}
                        </button>
                        <button onclick="deleteScheduled(${item.id})"
                            class="px-2 py-1 bg-red-600 hover:bg-red-700 text-white rounded text-xs">
                            Delete
                        </button>
                    </div>
                </div>
            </div>
        `).join('');
    } catch (error) {
        console.error('Failed to load scheduled SMS:', error);
        container.innerHTML = `
            <div class="text-center text-gray-400 py-4">
                <p>Failed to load scheduled messages</p>
            </div>
        `;
    }
}

function showAddScheduledModal() {
    const modalHtml = `
        <div id="scheduled-modal" class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
            <div class="bg-dark-card rounded-lg p-6 w-full max-w-lg mx-4">
                <h3 class="text-lg font-semibold text-white mb-4">Add Scheduled SMS Configuration</h3>
                <form id="add-scheduled-form" class="space-y-4">
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Configuration Name</label>
                        <input type="text" id="scheduled-name" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700" required placeholder="e.g., Welcome Message">
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Trigger Type</label>
                        <select id="scheduled-trigger-type" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700">
                            <option value="before_checkin">Before Check-in</option>
                            <option value="after_checkin">After Check-in</option>
                            <option value="before_checkout">Before Checkout</option>
                            <option value="time_of_day">Time of Day</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Trigger Offset (hours)</label>
                        <input type="number" id="scheduled-offset" value="0" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700">
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Custom Message (optional)</label>
                        <textarea id="scheduled-message" rows="3" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700" placeholder="Leave blank to use template"></textarea>
                    </div>
                    <div class="flex items-center gap-2">
                        <input type="checkbox" id="scheduled-enabled" checked class="rounded">
                        <label for="scheduled-enabled" class="text-sm text-gray-400">Enabled</label>
                    </div>
                    <div class="flex justify-end gap-3 mt-6">
                        <button type="button" onclick="closeScheduledModal()" class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded">Cancel</button>
                        <button type="submit" class="px-4 py-2 bg-purple-600 hover:bg-purple-700 text-white rounded">Save</button>
                    </div>
                </form>
            </div>
        </div>
    `;
    document.body.insertAdjacentHTML('beforeend', modalHtml);
    document.getElementById('add-scheduled-form').addEventListener('submit', saveNewScheduled);
}

function closeScheduledModal() {
    const modal = document.getElementById('scheduled-modal');
    if (modal) modal.remove();
}

async function saveNewScheduled(e) {
    e.preventDefault();
    const scheduled = {
        name: document.getElementById('scheduled-name').value,
        trigger_type: document.getElementById('scheduled-trigger-type').value,
        trigger_offset_hours: parseInt(document.getElementById('scheduled-offset').value) || 0,
        custom_message: document.getElementById('scheduled-message').value || null,
        enabled: document.getElementById('scheduled-enabled').checked
    };

    try {
        await apiRequest('/api/sms/scheduled', {
            method: 'POST',
            body: JSON.stringify(scheduled)
        });
        closeScheduledModal();
        showToast('Scheduled SMS configuration created', 'success');
        await loadScheduledSMS();
    } catch (error) {
        showToast('Failed to create configuration: ' + (error.message || error), 'error');
    }
}

async function showEditScheduledModal(scheduledId) {
    try {
        const item = await apiRequest(`/api/sms/scheduled/${scheduledId}`);
        const modalHtml = `
            <div id="scheduled-modal" class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
                <div class="bg-dark-card rounded-lg p-6 w-full max-w-lg mx-4">
                    <h3 class="text-lg font-semibold text-white mb-4">Edit Scheduled SMS Configuration</h3>
                    <form id="edit-scheduled-form" class="space-y-4">
                        <input type="hidden" id="edit-scheduled-id" value="${item.id}">
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Configuration Name</label>
                            <input type="text" id="edit-scheduled-name" value="${escapeHtml(item.name || '')}" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700" required>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Trigger Type</label>
                            <select id="edit-scheduled-trigger-type" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700">
                                <option value="before_checkin" ${item.trigger_type === 'before_checkin' ? 'selected' : ''}>Before Check-in</option>
                                <option value="after_checkin" ${item.trigger_type === 'after_checkin' ? 'selected' : ''}>After Check-in</option>
                                <option value="before_checkout" ${item.trigger_type === 'before_checkout' ? 'selected' : ''}>Before Checkout</option>
                                <option value="time_of_day" ${item.trigger_type === 'time_of_day' ? 'selected' : ''}>Time of Day</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Trigger Offset (hours)</label>
                            <input type="number" id="edit-scheduled-offset" value="${item.trigger_offset_hours || 0}" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700">
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Custom Message (optional)</label>
                            <textarea id="edit-scheduled-message" rows="3" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700">${escapeHtml(item.custom_message || '')}</textarea>
                        </div>
                        <div class="flex items-center gap-2">
                            <input type="checkbox" id="edit-scheduled-enabled" ${item.enabled ? 'checked' : ''} class="rounded">
                            <label for="edit-scheduled-enabled" class="text-sm text-gray-400">Enabled</label>
                        </div>
                        <div class="flex justify-end gap-3 mt-6">
                            <button type="button" onclick="closeScheduledModal()" class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded">Cancel</button>
                            <button type="submit" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded">Save Changes</button>
                        </div>
                    </form>
                </div>
            </div>
        `;
        document.body.insertAdjacentHTML('beforeend', modalHtml);
        document.getElementById('edit-scheduled-form').addEventListener('submit', saveEditedScheduled);
    } catch (error) {
        showToast('Failed to load configuration: ' + (error.message || error), 'error');
    }
}

async function saveEditedScheduled(e) {
    e.preventDefault();
    const scheduledId = document.getElementById('edit-scheduled-id').value;
    const scheduled = {
        name: document.getElementById('edit-scheduled-name').value,
        trigger_type: document.getElementById('edit-scheduled-trigger-type').value,
        trigger_offset_hours: parseInt(document.getElementById('edit-scheduled-offset').value) || 0,
        custom_message: document.getElementById('edit-scheduled-message').value || null,
        enabled: document.getElementById('edit-scheduled-enabled').checked
    };

    try {
        await apiRequest(`/api/sms/scheduled/${scheduledId}`, {
            method: 'PATCH',
            body: JSON.stringify(scheduled)
        });
        closeScheduledModal();
        showToast('Configuration updated successfully', 'success');
        await loadScheduledSMS();
    } catch (error) {
        showToast('Failed to update configuration: ' + (error.message || error), 'error');
    }
}

async function toggleScheduled(id, enabled) {
    try {
        await apiRequest(`/api/sms/scheduled/${id}`, {
            method: 'PATCH',
            body: JSON.stringify({ enabled })
        });
        await loadScheduledSMS();
        showToast('Schedule updated', 'success');
    } catch (error) {
        showToast('Failed to update schedule', 'error');
    }
}

async function deleteScheduled(id) {
    if (!confirm('Delete this scheduled message?')) return;
    try {
        await apiRequest(`/api/sms/scheduled/${id}`, { method: 'DELETE' });
        await loadScheduledSMS();
        showToast('Schedule deleted', 'success');
    } catch (error) {
        showToast('Failed to delete schedule', 'error');
    }
}

// ============================================================================
// Tips Functions
// ============================================================================

async function loadTips() {
    const container = document.getElementById('tips-container');

    try {
        const data = await apiRequest('/api/tips');
        // API returns array directly
        const tips = Array.isArray(data) ? data : (data.tips || []);

        if (tips.length === 0) {
            container.innerHTML = `
                <div class="text-center text-gray-400 py-4">
                    <p>No tips configured.</p>
                </div>
            `;
            return;
        }

        container.innerHTML = tips.map(tip => `
            <div class="p-4 bg-dark-bg rounded-lg border-l-4 ${tip.enabled ? 'border-yellow-500' : 'border-gray-600'}">
                <div class="flex justify-between items-start">
                    <div class="flex-1">
                        <div class="font-medium text-white">${escapeHtml(tip.title || '')}</div>
                        <div class="text-xs text-gray-400 mt-1">
                            ${escapeHtml(tip.category || 'general')} | Priority: ${tip.priority || 0}
                        </div>
                        <div class="text-sm text-gray-300 mt-2">${escapeHtml(tip.content || '')}</div>
                    </div>
                    <div class="flex gap-2 ml-4">
                        <button onclick="showEditTipModal(${tip.id})"
                            class="px-2 py-1 bg-blue-600 hover:bg-blue-700 text-white rounded text-xs">
                            Edit
                        </button>
                        <button onclick="toggleTip(${tip.id}, ${!tip.enabled})"
                            class="px-2 py-1 ${tip.enabled ? 'bg-gray-600' : 'bg-green-600'} hover:opacity-80 text-white rounded text-xs">
                            ${tip.enabled ? 'Disable' : 'Enable'}
                        </button>
                        <button onclick="deleteTip(${tip.id})"
                            class="px-2 py-1 bg-red-600 hover:bg-red-700 text-white rounded text-xs">
                            Delete
                        </button>
                    </div>
                </div>
            </div>
        `).join('');
    } catch (error) {
        console.error('Failed to load tips:', error);
        container.innerHTML = `
            <div class="text-center text-gray-400 py-4">
                <p>Failed to load tips</p>
            </div>
        `;
    }
}

function showAddTipModal() {
    const modalHtml = `
        <div id="tip-modal" class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
            <div class="bg-dark-card rounded-lg p-6 w-full max-w-lg mx-4">
                <h3 class="text-lg font-semibold text-white mb-4">Add Tip</h3>
                <form id="add-tip-form" class="space-y-4">
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Title</label>
                        <input type="text" id="tip-title" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700" required>
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Category</label>
                        <select id="tip-category" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700">
                            <option value="local">Local Tips</option>
                            <option value="property">Property Info</option>
                            <option value="dining">Dining</option>
                            <option value="activities">Activities</option>
                            <option value="transportation">Transportation</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Content</label>
                        <textarea id="tip-content" rows="3" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700" required></textarea>
                    </div>
                    <div>
                        <label class="block text-sm text-gray-400 mb-1">Priority (higher = shown first)</label>
                        <input type="number" id="tip-priority" value="0" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700">
                    </div>
                    <div class="flex items-center gap-2">
                        <input type="checkbox" id="tip-enabled" checked class="rounded">
                        <label for="tip-enabled" class="text-sm text-gray-400">Enabled</label>
                    </div>
                    <div class="flex justify-end gap-3 mt-6">
                        <button type="button" onclick="closeTipModal()" class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded">Cancel</button>
                        <button type="submit" class="px-4 py-2 bg-yellow-600 hover:bg-yellow-700 text-white rounded">Save Tip</button>
                    </div>
                </form>
            </div>
        </div>
    `;
    document.body.insertAdjacentHTML('beforeend', modalHtml);
    document.getElementById('add-tip-form').addEventListener('submit', saveNewTip);
}

function closeTipModal() {
    const modal = document.getElementById('tip-modal');
    if (modal) modal.remove();
}

async function saveNewTip(e) {
    e.preventDefault();
    const tip = {
        title: document.getElementById('tip-title').value,
        category: document.getElementById('tip-category').value,
        content: document.getElementById('tip-content').value,
        priority: parseInt(document.getElementById('tip-priority').value) || 0,
        enabled: document.getElementById('tip-enabled').checked
    };

    try {
        await apiRequest('/api/tips', {
            method: 'POST',
            body: JSON.stringify(tip)
        });
        closeTipModal();
        showToast('Tip created successfully', 'success');
        await loadTips();
    } catch (error) {
        showToast('Failed to create tip: ' + (error.message || error), 'error');
    }
}

async function toggleTip(id, enabled) {
    try {
        await apiRequest(`/api/tips/${id}`, {
            method: 'PATCH',
            body: JSON.stringify({ enabled })
        });
        await loadTips();
        showToast('Tip updated', 'success');
    } catch (error) {
        showToast('Failed to update tip', 'error');
    }
}

async function deleteTip(id) {
    if (!confirm('Delete this tip?')) return;
    try {
        await apiRequest(`/api/tips/${id}`, { method: 'DELETE' });
        await loadTips();
        showToast('Tip deleted', 'success');
    } catch (error) {
        showToast('Failed to delete tip', 'error');
    }
}

async function showEditTipModal(tipId) {
    try {
        const tip = await apiRequest(`/api/tips/${tipId}`);
        const modalHtml = `
            <div id="tip-modal" class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
                <div class="bg-dark-card rounded-lg p-6 w-full max-w-lg mx-4">
                    <h3 class="text-lg font-semibold text-white mb-4">Edit Tip</h3>
                    <form id="edit-tip-form" class="space-y-4">
                        <input type="hidden" id="edit-tip-id" value="${tip.id}">
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Title</label>
                            <input type="text" id="edit-tip-title" value="${escapeHtml(tip.title || '')}" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700" required>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Category</label>
                            <select id="edit-tip-category" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700">
                                <option value="sms_offer" ${tip.category === 'sms_offer' ? 'selected' : ''}>SMS Offer</option>
                                <option value="feature_hint" ${tip.category === 'feature_hint' ? 'selected' : ''}>Feature Hint</option>
                                <option value="local_tip" ${tip.category === 'local_tip' ? 'selected' : ''}>Local Tip</option>
                                <option value="local" ${tip.category === 'local' ? 'selected' : ''}>Local</option>
                                <option value="property" ${tip.category === 'property' ? 'selected' : ''}>Property</option>
                                <option value="dining" ${tip.category === 'dining' ? 'selected' : ''}>Dining</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Content</label>
                            <textarea id="edit-tip-content" rows="3" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700" required>${escapeHtml(tip.content || '')}</textarea>
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">Priority (higher = shown first)</label>
                            <input type="number" id="edit-tip-priority" value="${tip.priority || 0}" class="w-full bg-dark-bg text-white px-3 py-2 rounded border border-gray-700">
                        </div>
                        <div class="flex items-center gap-2">
                            <input type="checkbox" id="edit-tip-enabled" ${tip.enabled ? 'checked' : ''} class="rounded">
                            <label for="edit-tip-enabled" class="text-sm text-gray-400">Enabled</label>
                        </div>
                        <div class="flex justify-end gap-3 mt-6">
                            <button type="button" onclick="closeTipModal()" class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded">Cancel</button>
                            <button type="submit" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded">Save Changes</button>
                        </div>
                    </form>
                </div>
            </div>
        `;
        document.body.insertAdjacentHTML('beforeend', modalHtml);
        document.getElementById('edit-tip-form').addEventListener('submit', saveEditedTip);
    } catch (error) {
        showToast('Failed to load tip: ' + (error.message || error), 'error');
    }
}

async function saveEditedTip(e) {
    e.preventDefault();
    const tipId = document.getElementById('edit-tip-id').value;
    const tip = {
        title: document.getElementById('edit-tip-title').value,
        category: document.getElementById('edit-tip-category').value,
        content: document.getElementById('edit-tip-content').value,
        priority: parseInt(document.getElementById('edit-tip-priority').value) || 0,
        enabled: document.getElementById('edit-tip-enabled').checked
    };

    try {
        await apiRequest(`/api/tips/${tipId}`, {
            method: 'PATCH',
            body: JSON.stringify(tip)
        });
        closeTipModal();
        showToast('Tip updated successfully', 'success');
        await loadTips();
    } catch (error) {
        showToast('Failed to update tip: ' + (error.message || error), 'error');
    }
}

// ============================================================================
// Incoming SMS Functions
// ============================================================================

async function loadIncomingSMS() {
    const container = document.getElementById('incoming-sms-table-body');

    try {
        const data = await apiRequest('/api/sms/incoming?limit=20');
        // API returns array directly
        const messages = Array.isArray(data) ? data : (data.messages || []);

        if (messages.length === 0) {
            container.innerHTML = `
                <tr>
                    <td colspan="5" class="text-center text-gray-400 py-8">
                        <div class="text-2xl mb-2">📥</div>
                        <p>No incoming messages yet</p>
                    </td>
                </tr>
            `;
            return;
        }

        container.innerHTML = messages.map(msg => `
            <tr>
                <td class="text-sm">${formatDateTime(msg.received_at)}</td>
                <td class="text-sm">${escapeHtml(maskPhoneNumber(msg.phone_number))}</td>
                <td class="text-sm max-w-xs truncate" title="${escapeHtml(msg.message)}">${escapeHtml(truncateText(msg.message, 40))}</td>
                <td class="text-sm">${msg.matched_guest ? '<span class="badge badge-success">Matched</span>' : '<span class="badge badge-warning">Unknown</span>'}</td>
                <td class="text-sm">${msg.response_sent ? '<span class="text-green-400">Yes</span>' : '<span class="text-gray-400">No</span>'}</td>
            </tr>
        `).join('');
    } catch (error) {
        console.error('Failed to load incoming SMS:', error);
        container.innerHTML = `
            <tr>
                <td colspan="5" class="text-center text-red-400 py-8">
                    <p>Failed to load incoming messages</p>
                </td>
            </tr>
        `;
    }
}

// ============================================================================
// Tab Initialization
// ============================================================================

// Update main data loader to include enhanced features
async function loadSMSDataEnhanced() {
    await Promise.all([
        loadSMSSettings(),
        loadSMSHistory(0),
        loadSMSCostSummary(),
        loadSMSCostByStay(),
        loadSMSTemplates(),
        loadScheduledSMS(),
        loadTips(),
        loadIncomingSMS()
    ]);
}

// Register tab loader (override with enhanced version)
if (typeof window.tabLoaders === 'undefined') {
    window.tabLoaders = {};
}
window.tabLoaders['sms'] = loadSMSDataEnhanced;
