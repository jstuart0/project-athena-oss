/**
 * LLM Performance Metrics Viewer
 *
 * Displays performance metrics for LLM backends including:
 * - Tokens per second
 * - Latency
 * - Request counts
 * - Time-series visualization
 */

let metricsData = [];
let filteredMetrics = [];

// Use shared utilities from utils.js (getAuthHeaders, safeShowToast available via window)

/**
 * Load metrics from backend
 */
async function loadMetrics(model = null, backend = null, limit = 100) {
    try {
        showMetricsLoading();

        // Build query params
        const params = new URLSearchParams();
        if (model) params.append('model', model);
        if (backend) params.append('backend', backend);
        params.append('limit', limit);

        const response = await fetch(`/api/llm-backends/metrics?${params}`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error(`Failed to load metrics: ${response.statusText}`);
        }

        metricsData = await response.json();
        filteredMetrics = [...metricsData];

        renderMetrics();
        renderMetricsStats();
        renderMetricsChart();

        console.log('Metrics loaded successfully:', metricsData.length, 'records');
    } catch (error) {
        console.error('Failed to load metrics:', error);
        safeShowToast('Failed to load performance metrics', 'error');
        showMetricsError(error.message);
    }
}

/**
 * Show loading state
 */
function showMetricsLoading() {
    const container = document.getElementById('metrics-table-container');
    if (container) {
        container.innerHTML = '<div class="loading">Loading metrics...</div>';
    }

    const statsContainer = document.getElementById('metrics-stats-container');
    if (statsContainer) {
        statsContainer.innerHTML = '<div class="loading">Calculating statistics...</div>';
    }
}

/**
 * Show error state
 */
function showMetricsError(message) {
    const container = document.getElementById('metrics-table-container');
    if (container) {
        container.innerHTML = `<div class="error">Error: ${message}</div>`;
    }
}

/**
 * Render metrics table
 */
function renderMetrics() {
    const container = document.getElementById('metrics-table-container');
    if (!container) return;

    if (filteredMetrics.length === 0) {
        container.innerHTML = '<div class="empty-state">No metrics data available</div>';
        return;
    }

    const html = `
        <table class="data-table">
            <thead>
                <tr>
                    <th><span class="inline-flex items-center gap-1">Timestamp${typeof infoIcon === 'function' ? infoIcon('metrics-timestamp') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Stage${typeof infoIcon === 'function' ? infoIcon('metrics-stage') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Model${typeof infoIcon === 'function' ? infoIcon('metrics-model') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Backend${typeof infoIcon === 'function' ? infoIcon('metrics-backend') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Tokens/sec${typeof infoIcon === 'function' ? infoIcon('metrics-tokens-sec') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Latency (s)${typeof infoIcon === 'function' ? infoIcon('metrics-latency') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Tokens${typeof infoIcon === 'function' ? infoIcon('metrics-tokens') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Source${typeof infoIcon === 'function' ? infoIcon('metrics-source') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Intent${typeof infoIcon === 'function' ? infoIcon('metrics-intent') : ''}</span></th>
                    <th><span class="inline-flex items-center gap-1">Session ID${typeof infoIcon === 'function' ? infoIcon('metrics-session-id') : ''}</span></th>
                </tr>
            </thead>
            <tbody>
                ${filteredMetrics.map(metric => `
                    <tr>
                        <td>${formatTimestamp(metric.timestamp)}</td>
                        <td>${metric.stage ? `<span class="tag tag-stage">${metric.stage}</span>` : '-'}</td>
                        <td><span class="tag tag-model">${metric.model}</span></td>
                        <td><span class="tag tag-backend">${metric.backend}</span></td>
                        <td class="metric-value">${metric.tokens_per_second.toFixed(2)}</td>
                        <td class="metric-value">${metric.latency_seconds.toFixed(3)}</td>
                        <td class="metric-value">${metric.tokens_generated}</td>
                        <td>${metric.source ? `<span class="tag tag-source">${metric.source}</span>` : '-'}</td>
                        <td>${metric.intent || '-'}</td>
                        <td class="session-id">${metric.session_id ? truncateId(metric.session_id) : '-'}</td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;

    container.innerHTML = html;
}

/**
 * Render metrics statistics
 */
function renderMetricsStats() {
    const container = document.getElementById('metrics-stats-container');
    if (!container || filteredMetrics.length === 0) {
        if (container) container.innerHTML = '';
        return;
    }

    // Calculate statistics
    const stats = calculateStats(filteredMetrics);

    const html = `
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label flex items-center">Total Requests${typeof infoIcon === 'function' ? infoIcon('metrics-total-requests') : ''}</div>
                <div class="stat-value">${stats.totalRequests}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label flex items-center">Avg Tokens/sec${typeof infoIcon === 'function' ? infoIcon('metrics-avg-tokens') : ''}</div>
                <div class="stat-value">${stats.avgTokensPerSec.toFixed(2)}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label flex items-center">Avg Latency${typeof infoIcon === 'function' ? infoIcon('metrics-avg-latency') : ''}</div>
                <div class="stat-value">${stats.avgLatency.toFixed(3)}s</div>
            </div>
            <div class="stat-card">
                <div class="stat-label flex items-center">Total Tokens${typeof infoIcon === 'function' ? infoIcon('metrics-total-tokens') : ''}</div>
                <div class="stat-value">${stats.totalTokens.toLocaleString()}</div>
            </div>
        </div>

        <div class="stats-breakdown">
            <h3>By Backend</h3>
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Backend</th>
                        <th>Requests</th>
                        <th>Avg Tokens/sec</th>
                        <th>Avg Latency (s)</th>
                    </tr>
                </thead>
                <tbody>
                    ${Object.entries(stats.byBackend).map(([backend, data]) => `
                        <tr>
                            <td><span class="tag tag-backend">${backend}</span></td>
                            <td>${data.count}</td>
                            <td class="metric-value">${data.avgTokensPerSec.toFixed(2)}</td>
                            <td class="metric-value">${data.avgLatency.toFixed(3)}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>

            <h3>By Model</h3>
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Model</th>
                        <th>Requests</th>
                        <th>Avg Tokens/sec</th>
                        <th>Avg Latency (s)</th>
                    </tr>
                </thead>
                <tbody>
                    ${Object.entries(stats.byModel).map(([model, data]) => `
                        <tr>
                            <td><span class="tag tag-model">${model}</span></td>
                            <td>${data.count}</td>
                            <td class="metric-value">${data.avgTokensPerSec.toFixed(2)}</td>
                            <td class="metric-value">${data.avgLatency.toFixed(3)}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        </div>
    `;

    container.innerHTML = html;
}

/**
 * Calculate statistics from metrics
 */
function calculateStats(metrics) {
    const stats = {
        totalRequests: metrics.length,
        avgTokensPerSec: 0,
        avgLatency: 0,
        totalTokens: 0,
        byBackend: {},
        byModel: {}
    };

    let totalTokensPerSec = 0;
    let totalLatency = 0;

    metrics.forEach(metric => {
        // Overall stats
        totalTokensPerSec += metric.tokens_per_second;
        totalLatency += metric.latency_seconds;
        stats.totalTokens += metric.tokens_generated;

        // By backend
        if (!stats.byBackend[metric.backend]) {
            stats.byBackend[metric.backend] = {
                count: 0,
                totalTokensPerSec: 0,
                totalLatency: 0,
                avgTokensPerSec: 0,
                avgLatency: 0
            };
        }
        stats.byBackend[metric.backend].count++;
        stats.byBackend[metric.backend].totalTokensPerSec += metric.tokens_per_second;
        stats.byBackend[metric.backend].totalLatency += metric.latency_seconds;

        // By model
        if (!stats.byModel[metric.model]) {
            stats.byModel[metric.model] = {
                count: 0,
                totalTokensPerSec: 0,
                totalLatency: 0,
                avgTokensPerSec: 0,
                avgLatency: 0
            };
        }
        stats.byModel[metric.model].count++;
        stats.byModel[metric.model].totalTokensPerSec += metric.tokens_per_second;
        stats.byModel[metric.model].totalLatency += metric.latency_seconds;
    });

    // Calculate averages
    stats.avgTokensPerSec = totalTokensPerSec / metrics.length;
    stats.avgLatency = totalLatency / metrics.length;

    // Calculate backend averages
    Object.keys(stats.byBackend).forEach(backend => {
        const data = stats.byBackend[backend];
        data.avgTokensPerSec = data.totalTokensPerSec / data.count;
        data.avgLatency = data.totalLatency / data.count;
    });

    // Calculate model averages
    Object.keys(stats.byModel).forEach(model => {
        const data = stats.byModel[model];
        data.avgTokensPerSec = data.totalTokensPerSec / data.count;
        data.avgLatency = data.totalLatency / data.count;
    });

    return stats;
}

/**
 * Render metrics chart (simple bar chart using CSS)
 */
function renderMetricsChart() {
    const container = document.getElementById('metrics-chart-container');
    if (!container || filteredMetrics.length === 0) {
        if (container) container.innerHTML = '';
        return;
    }

    // Get last 20 metrics for chart
    const chartData = filteredMetrics.slice(0, 20).reverse();
    const maxTokensPerSec = Math.max(...chartData.map(m => m.tokens_per_second));

    const html = `
        <div class="chart">
            <h3>Recent Performance (Last 20 Requests)</h3>
            <div class="chart-bars">
                ${chartData.map((metric, index) => {
                    const height = (metric.tokens_per_second / maxTokensPerSec) * 100;
                    return `
                        <div class="chart-bar-container" title="${metric.model} - ${metric.tokens_per_second.toFixed(2)} tokens/sec">
                            <div class="chart-bar" style="height: ${height}%"></div>
                            <div class="chart-label">${index + 1}</div>
                        </div>
                    `;
                }).join('')}
            </div>
            <div class="chart-legend">
                <span>Tokens per second (higher is better)</span>
            </div>
        </div>
    `;

    container.innerHTML = html;
}

/**
 * Apply filters to metrics
 */
function applyFilters() {
    const modelFilter = document.getElementById('filter-model')?.value || '';
    const backendFilter = document.getElementById('filter-backend')?.value || '';
    const limitFilter = parseInt(document.getElementById('filter-limit')?.value || '100');

    // Reload with filters
    loadMetrics(
        modelFilter || null,
        backendFilter || null,
        limitFilter
    );
}

/**
 * Clear all filters
 */
function clearFilters() {
    const modelFilter = document.getElementById('filter-model');
    const backendFilter = document.getElementById('filter-backend');
    const limitFilter = document.getElementById('filter-limit');

    if (modelFilter) modelFilter.value = '';
    if (backendFilter) backendFilter.value = '';
    if (limitFilter) limitFilter.value = '100';

    loadMetrics(null, null, 100);
}

/**
 * Format timestamp for display
 */
function formatTimestamp(timestamp) {
    const date = new Date(timestamp);
    return date.toLocaleString('en-US', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });
}

/**
 * Truncate long IDs for display
 */
function truncateId(id) {
    if (!id) return '';
    return id.length > 12 ? id.substring(0, 12) + '...' : id;
}

/**
 * Initialize metrics page
 */
function initMetricsPage() {
    console.log('Initializing metrics page');

    // Load initial metrics
    loadMetrics();

    // Set up auto-refresh using RefreshManager
    const refreshCallback = () => {
        const modelFilter = document.getElementById('filter-model')?.value || null;
        const backendFilter = document.getElementById('filter-backend')?.value || null;
        const limitFilter = parseInt(document.getElementById('filter-limit')?.value || '100');
        loadMetrics(modelFilter, backendFilter, limitFilter);
    };

    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.createInterval('metrics-refresh', refreshCallback, 30000);
    } else {
        setInterval(refreshCallback, 30000);
    }
}

/**
 * Cleanup metrics page
 */
function destroyMetricsPage() {
    if (typeof RefreshManager !== 'undefined') {
        RefreshManager.clearInterval('metrics-refresh');
    }
}

// Export cleanup function
if (typeof window !== 'undefined') {
    window.destroyMetricsPage = destroyMetricsPage;
}

// ============================================================================
// Metrics Export Feature
// ============================================================================

/**
 * Export metrics data to CSV format
 */
function exportMetricsToCSV() {
    if (filteredMetrics.length === 0) {
        safeShowToast('No metrics data to export', 'warning');
        return;
    }

    const headers = [
        'Timestamp',
        'Stage',
        'Model',
        'Backend',
        'Tokens/sec',
        'Latency (s)',
        'Tokens Generated',
        'Source',
        'Intent',
        'Session ID'
    ];

    const rows = filteredMetrics.map(metric => [
        metric.timestamp || '',
        metric.stage || '',
        metric.model || '',
        metric.backend || '',
        metric.tokens_per_second?.toFixed(2) || '',
        metric.latency_seconds?.toFixed(3) || '',
        metric.tokens_generated || '',
        metric.source || '',
        metric.intent || '',
        metric.session_id || ''
    ]);

    const csv = [
        headers.join(','),
        ...rows.map(row => row.map(cell => `"${String(cell).replace(/"/g, '""')}"`).join(','))
    ].join('\n');

    downloadFile(csv, `athena-metrics-${Date.now()}.csv`, 'text/csv');
    safeShowToast('Metrics exported to CSV', 'success');
}

/**
 * Export metrics summary statistics to JSON
 */
function exportMetricsToJSON() {
    if (filteredMetrics.length === 0) {
        safeShowToast('No metrics data to export', 'warning');
        return;
    }

    const stats = calculateStats(filteredMetrics);
    const exportData = {
        exported_at: new Date().toISOString(),
        summary: {
            total_requests: stats.totalRequests,
            avg_tokens_per_sec: stats.avgTokensPerSec,
            avg_latency_seconds: stats.avgLatency,
            total_tokens: stats.totalTokens
        },
        by_backend: stats.byBackend,
        by_model: stats.byModel,
        raw_data: filteredMetrics
    };

    const json = JSON.stringify(exportData, null, 2);
    downloadFile(json, `athena-metrics-${Date.now()}.json`, 'application/json');
    safeShowToast('Metrics exported to JSON', 'success');
}

/**
 * Export chart as PNG image
 */
function exportChartToPNG() {
    // Look for Chart.js canvas
    const chartCanvas = document.querySelector('#metrics-chart-container canvas');

    if (chartCanvas && typeof Chart !== 'undefined') {
        // If using Chart.js
        const chart = Chart.getChart(chartCanvas);
        if (chart) {
            const link = document.createElement('a');
            link.download = `athena-metrics-chart-${Date.now()}.png`;
            link.href = chart.toBase64Image();
            link.click();
            safeShowToast('Chart exported as PNG', 'success');
            return;
        }
    }

    // Fallback: capture the chart container as is (using html2canvas if available)
    const chartContainer = document.getElementById('metrics-chart-container');
    if (!chartContainer) {
        safeShowToast('No chart to export', 'warning');
        return;
    }

    if (typeof html2canvas !== 'undefined') {
        html2canvas(chartContainer).then(canvas => {
            const link = document.createElement('a');
            link.download = `athena-metrics-chart-${Date.now()}.png`;
            link.href = canvas.toDataURL();
            link.click();
            safeShowToast('Chart exported as PNG', 'success');
        });
    } else {
        safeShowToast('Chart export requires Chart.js or html2canvas', 'warning');
    }
}

/**
 * Helper function to download a file
 */
function downloadFile(content, filename, mimeType) {
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
}

/**
 * Render export buttons in the metrics UI
 */
function renderExportButtons() {
    const container = document.getElementById('metrics-export-buttons');
    if (!container) return;

    container.innerHTML = `
        <div class="flex items-center gap-2">
            <span class="text-sm text-gray-400 mr-2">Export:</span>
            <button onclick="exportMetricsToCSV()"
                    class="px-3 py-1.5 text-sm bg-gray-700 hover:bg-gray-600 text-gray-200 rounded-lg transition flex items-center gap-2"
                    title="Export data as CSV">
                <i data-lucide="file-spreadsheet" class="w-4 h-4"></i>
                CSV
            </button>
            <button onclick="exportMetricsToJSON()"
                    class="px-3 py-1.5 text-sm bg-gray-700 hover:bg-gray-600 text-gray-200 rounded-lg transition flex items-center gap-2"
                    title="Export data and stats as JSON">
                <i data-lucide="file-json" class="w-4 h-4"></i>
                JSON
            </button>
            <button onclick="exportChartToPNG()"
                    class="px-3 py-1.5 text-sm bg-gray-700 hover:bg-gray-600 text-gray-200 rounded-lg transition flex items-center gap-2"
                    title="Export chart as PNG image">
                <i data-lucide="image" class="w-4 h-4"></i>
                PNG
            </button>
        </div>
    `;

    if (typeof lucide !== 'undefined') lucide.createIcons();
}

// Export functions to window
if (typeof window !== 'undefined') {
    window.exportMetricsToCSV = exportMetricsToCSV;
    window.exportMetricsToJSON = exportMetricsToJSON;
    window.exportChartToPNG = exportChartToPNG;
    window.renderExportButtons = renderExportButtons;
}
