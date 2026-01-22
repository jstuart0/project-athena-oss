/**
 * Intent Analytics Viewer
 *
 * Displays analytics for query intents including:
 * - Intent classification statistics
 * - RAG service availability
 * - Service gap analysis
 * - Query trends
 */

let analyticsData = null;
let selectedDays = 7;

// Use shared utilities from utils.js (loaded via window.AthenaUtils)

/**
 * Load intent analytics from backend
 */
async function loadIntentAnalytics(days = 7) {
    try {
        showAnalyticsLoading();
        selectedDays = days;

        const response = await fetch(`/api/analytics/intents?days=${days}`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error(`Failed to load analytics: ${response.statusText}`);
        }

        analyticsData = await response.json();

        renderAnalyticsSummary();
        renderIntentsTable();
        renderServiceGaps();

        console.log('Analytics loaded successfully:', analyticsData);
    } catch (error) {
        console.error('Failed to load analytics:', error);
        safeShowToast('Failed to load intent analytics', 'error');
        showAnalyticsError(error.message);
    }
}

/**
 * Show loading state
 */
function showAnalyticsLoading() {
    const container = document.getElementById('analytics-container');
    if (container) {
        container.innerHTML = '<div class="loading">Loading intent analytics...</div>';
    }
}

/**
 * Show error state
 */
function showAnalyticsError(message) {
    const container = document.getElementById('analytics-container');
    if (container) {
        container.innerHTML = `<div class="error">Error: ${message}</div>`;
    }
}

/**
 * Render analytics summary
 */
function renderAnalyticsSummary() {
    const container = document.getElementById('analytics-summary');
    if (!container || !analyticsData) return;

    const totalWithRag = analyticsData.intents.filter(i => i.has_rag_service).reduce((sum, i) => sum + i.count, 0);
    const totalWithoutRag = analyticsData.total_queries - totalWithRag;
    const ragCoverage = analyticsData.total_queries > 0
        ? Math.round((totalWithRag / analyticsData.total_queries) * 100)
        : 0;

    container.innerHTML = `
        <div class="stats-grid">
            <div class="stat-card">
                <h3 class="flex items-center">Total Queries${typeof infoIcon === 'function' ? infoIcon('analytics-total-queries') : ''}</h3>
                <div class="stat-value">${analyticsData.total_queries}</div>
                <div class="stat-label">${analyticsData.date_range}</div>
            </div>
            <div class="stat-card">
                <h3 class="flex items-center">Unique Intents${typeof infoIcon === 'function' ? infoIcon('analytics-unique-intents') : ''}</h3>
                <div class="stat-value">${analyticsData.intents.length}</div>
                <div class="stat-label">Different intent types</div>
            </div>
            <div class="stat-card">
                <h3 class="flex items-center">RAG Coverage${typeof infoIcon === 'function' ? infoIcon('analytics-rag-coverage') : ''}</h3>
                <div class="stat-value">${ragCoverage}%</div>
                <div class="stat-label">${totalWithRag} queries with RAG services</div>
            </div>
            <div class="stat-card">
                <h3 class="flex items-center">Service Gaps${typeof infoIcon === 'function' ? infoIcon('analytics-service-gaps') : ''}</h3>
                <div class="stat-value">${totalWithoutRag}</div>
                <div class="stat-label">Queries without RAG services</div>
            </div>
        </div>
    `;
}

/**
 * Render intents table
 */
function renderIntentsTable() {
    const container = document.getElementById('intents-table-container');
    if (!container || !analyticsData || !analyticsData.intents.length) {
        container.innerHTML = '<div class="empty">No intent data available for selected period</div>';
        return;
    }

    const tableHTML = `
        <table class="crud-table">
            <thead>
                <tr>
                    <th><span class="inline-flex items-center gap-1">Intent${typeof infoIcon === 'function' ? infoIcon('analytics-intent-name') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Count${typeof infoIcon === 'function' ? infoIcon('analytics-intent-count') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Percentage${typeof infoIcon === 'function' ? infoIcon('analytics-intent-percentage') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">RAG Service${typeof infoIcon === 'function' ? infoIcon('analytics-rag-service') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">System Mapping${typeof infoIcon === 'function' ? infoIcon('analytics-system-mapping') : ''}</span></th>
                </tr>
            </thead>
            <tbody>
                ${analyticsData.intents.map(intent => `
                    <tr>
                        <td><strong>${escapeHtml(intent.intent)}</strong></td>
                        <td>${intent.count}</td>
                        <td>
                            <div class="progress-container">
                                <div class="progress-bar" style="width: ${intent.percentage}%"></div>
                                <span class="progress-text">${intent.percentage}%</span>
                            </div>
                        </td>
                        <td>
                            ${intent.has_rag_service
                                ? '<span class="badge badge-success">✓ Yes</span>'
                                : '<span class="badge badge-warning">⚠ No</span>'}
                        </td>
                        <td><code>${escapeHtml(intent.system_mapping)}</code></td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;

    container.innerHTML = tableHTML;
}

/**
 * Render service gaps analysis
 */
function renderServiceGaps() {
    const container = document.getElementById('service-gaps-container');
    if (!container || !analyticsData) return;

    const gaps = analyticsData.intents.filter(i => !i.has_rag_service);

    if (!gaps.length) {
        container.innerHTML = '<div class="success">All intents have dedicated RAG services!</div>';
        return;
    }

    const gapsHTML = `
        <div class="gaps-header">
            <h3>Service Gaps Analysis</h3>
            <p>These intents don't have dedicated RAG services and may benefit from one:</p>
        </div>
        <table class="crud-table">
            <thead>
                <tr>
                    <th>Intent</th>
                    <th>Query Count</th>
                    <th>Percentage</th>
                    <th>Priority</th>
                </tr>
            </thead>
            <tbody>
                ${gaps.map(intent => {
                    const priority = intent.percentage > 10 ? 'High' : intent.percentage > 5 ? 'Medium' : 'Low';
                    const priorityClass = priority.toLowerCase();
                    return `
                        <tr>
                            <td><strong>${escapeHtml(intent.intent)}</strong></td>
                            <td>${intent.count}</td>
                            <td>${intent.percentage}%</td>
                            <td><span class="priority-badge priority-${priorityClass}">${priority}</span></td>
                        </tr>
                    `;
                }).join('')}
            </tbody>
        </table>
        <div class="gaps-footer">
            <p><strong>Recommendation:</strong> Consider implementing RAG services for high-priority intents (>10% of queries)</p>
        </div>
    `;

    container.innerHTML = gapsHTML;
}

/**
 * Handle time range filter change
 */
function handleTimeRangeChange(event) {
    const days = parseInt(event.target.value);
    loadIntentAnalytics(days);
}

// escapeHtml is now provided by utils.js

/**
 * Initialize analytics page
 */
function initAnalytics() {
    // Load default analytics (30 days to capture historical data)
    loadIntentAnalytics(30);

    // Set up time range selector
    const timeRangeSelect = document.getElementById('analytics-time-range');
    if (timeRangeSelect) {
        timeRangeSelect.value = '30';
        timeRangeSelect.addEventListener('change', handleTimeRangeChange);
    }

    console.log('Intent Analytics initialized');
}

// Auto-initialize if on analytics page
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        if (window.location.hash === '#analytics' || window.location.hash === '#intent-analytics') {
            initAnalytics();
        }
    });
} else {
    if (window.location.hash === '#analytics' || window.location.hash === '#intent-analytics') {
        initAnalytics();
    }
}
