/**
 * Debug Logs - View and search orchestrator logs with raw/table view toggle
 */

let debugLogsState = {
    files: [],
    selectedFile: null,
    entries: [],
    searchQuery: '',
    levelFilter: 'all',
    autoRefresh: false,
    autoRefreshInterval: null,
    loading: false,
    viewMode: 'raw', // 'raw' or 'table'
    sortField: 'timestamp',
    sortDirection: 'desc'
};

async function initDebugLogs() {
    console.log('Initializing debug logs...');

    // Check debug mode status first
    await checkDebugStatus();

    // Load available log files
    await loadLogFiles();

    // Set up event listeners
    setupDebugLogEventListeners();
}

async function checkDebugStatus() {
    try {
        const response = await fetch('/api/debug-logs/status');
        if (!response.ok) throw new Error('Failed to check debug status');

        const status = await response.json();

        const statusBadge = document.getElementById('debug-mode-status');
        if (statusBadge) {
            if (status.debug_mode) {
                statusBadge.className = 'px-3 py-1 bg-green-600/20 text-green-400 rounded-full text-sm';
                statusBadge.innerHTML = '<i data-lucide="check-circle" class="w-4 h-4 inline mr-2"></i>Debug Mode Active';
            } else {
                statusBadge.className = 'px-3 py-1 bg-yellow-600/20 text-yellow-400 rounded-full text-sm';
                statusBadge.innerHTML = '<i data-lucide="alert-circle" class="w-4 h-4 inline mr-2"></i>Debug Mode Inactive';
            }
            if (typeof lucide !== 'undefined') lucide.createIcons();
        }

        const statsEl = document.getElementById('debug-stats');
        if (statsEl) {
            statsEl.innerHTML = `
                <span class="text-gray-400">Directory:</span>
                <span class="text-gray-200 font-mono text-sm ml-1">${status.log_directory}</span>
                <span class="mx-3 text-gray-600">|</span>
                <span class="text-gray-400">Files:</span>
                <span class="text-gray-200 ml-1">${status.file_count}</span>
                <span class="mx-3 text-gray-600">|</span>
                <span class="text-gray-400">Size:</span>
                <span class="text-gray-200 ml-1">${status.total_size_mb} MB</span>
            `;
        }
    } catch (error) {
        console.error('Error checking debug status:', error);
    }
}

async function loadLogFiles() {
    try {
        const response = await fetch('/api/debug-logs/files?days=7');
        if (!response.ok) throw new Error('Failed to load log files');

        debugLogsState.files = await response.json();
        renderLogFileList();

        // Auto-select most recent file
        if (debugLogsState.files.length > 0 && !debugLogsState.selectedFile) {
            selectLogFile(debugLogsState.files[0].name);
        }
    } catch (error) {
        console.error('Error loading log files:', error);
        showToast('Failed to load log files', 'error');
    }
}

function renderLogFileList() {
    const container = document.getElementById('log-file-list');
    if (!container) return;

    if (debugLogsState.files.length === 0) {
        container.innerHTML = `
            <div class="text-center py-8 text-gray-500">
                <i data-lucide="file-text" class="w-8 h-8 mx-auto mb-3 opacity-50"></i>
                <p>No log files found</p>
                <p class="text-sm mt-2">Enable debug mode to start logging</p>
            </div>
        `;
        if (typeof lucide !== 'undefined') lucide.createIcons();
        return;
    }

    container.innerHTML = debugLogsState.files.map(file => `
        <button
            onclick="selectLogFile('${file.name}')"
            class="w-full text-left px-3 py-2 rounded-lg transition-colors ${
                debugLogsState.selectedFile === file.name
                    ? 'bg-purple-600/30 border border-purple-500/50'
                    : 'hover:bg-gray-800 border border-transparent'
            }"
        >
            <div class="flex items-center justify-between">
                <div class="flex items-center space-x-2">
                    <i data-lucide="file-text" class="w-4 h-4 text-gray-400"></i>
                    <span class="text-gray-200 font-mono text-sm">${file.service}</span>
                </div>
                <span class="text-gray-500 text-xs">${formatFileSize(file.size)}</span>
            </div>
            <div class="text-gray-500 text-xs mt-1">${file.date}</div>
        </button>
    `).join('');
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

async function selectLogFile(filename) {
    debugLogsState.selectedFile = filename;
    renderLogFileList();
    await loadLogEntries();
}

async function loadLogEntries() {
    if (!debugLogsState.selectedFile) return;

    debugLogsState.loading = true;
    updateLoadingState();

    try {
        const params = new URLSearchParams({
            file: debugLogsState.selectedFile,
            limit: '1000'
        });

        if (debugLogsState.searchQuery) {
            params.append('query', debugLogsState.searchQuery);
        }

        if (debugLogsState.levelFilter !== 'all') {
            params.append('level', debugLogsState.levelFilter);
        }

        const response = await fetch(`/api/debug-logs/search?${params}`);
        if (!response.ok) throw new Error('Failed to load log entries');

        const result = await response.json();
        debugLogsState.entries = result.entries;

        // Apply sorting
        sortEntries();

        renderLogView();

        // Update stats
        const statsEl = document.getElementById('log-entries-stats');
        if (statsEl) {
            statsEl.textContent = `Showing ${result.returned_lines} of ${result.total_lines} lines`;
        }
    } catch (error) {
        console.error('Error loading log entries:', error);
        showToast('Failed to load log entries', 'error');
    } finally {
        debugLogsState.loading = false;
        updateLoadingState();
    }
}

function sortEntries() {
    const { sortField, sortDirection } = debugLogsState;

    debugLogsState.entries.sort((a, b) => {
        let valA = a[sortField] || '';
        let valB = b[sortField] || '';

        if (sortField === 'line_number') {
            valA = parseInt(valA) || 0;
            valB = parseInt(valB) || 0;
        }

        if (valA < valB) return sortDirection === 'asc' ? -1 : 1;
        if (valA > valB) return sortDirection === 'asc' ? 1 : -1;
        return 0;
    });
}

function toggleSort(field) {
    if (debugLogsState.sortField === field) {
        debugLogsState.sortDirection = debugLogsState.sortDirection === 'asc' ? 'desc' : 'asc';
    } else {
        debugLogsState.sortField = field;
        debugLogsState.sortDirection = 'desc';
    }
    sortEntries();
    renderLogView();
}

function setViewMode(mode) {
    debugLogsState.viewMode = mode;

    // Update button states
    document.querySelectorAll('.view-mode-btn').forEach(btn => {
        btn.classList.remove('bg-purple-600', 'text-white');
        btn.classList.add('bg-gray-700', 'text-gray-400');
    });
    const activeBtn = document.querySelector(`.view-mode-btn[data-mode="${mode}"]`);
    if (activeBtn) {
        activeBtn.classList.remove('bg-gray-700', 'text-gray-400');
        activeBtn.classList.add('bg-purple-600', 'text-white');
    }

    renderLogView();
}

function renderLogView() {
    if (debugLogsState.viewMode === 'table') {
        renderTableView();
    } else {
        renderRawView();
    }
}

function renderRawView() {
    const container = document.getElementById('log-entries');
    if (!container) return;

    if (debugLogsState.entries.length === 0) {
        container.innerHTML = `
            <div class="text-center py-12 text-gray-500">
                <i data-lucide="search" class="w-8 h-8 mx-auto mb-3 opacity-50"></i>
                <p>No matching log entries</p>
                ${debugLogsState.searchQuery ? '<p class="text-sm mt-2">Try adjusting your search</p>' : ''}
            </div>
        `;
        if (typeof lucide !== 'undefined') lucide.createIcons();
        return;
    }

    container.innerHTML = `<div class="space-y-1">${debugLogsState.entries.map(entry => renderRawLogEntry(entry)).join('')}</div>`;
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

function renderRawLogEntry(entry) {
    const levelColors = {
        'debug': 'text-gray-400 bg-gray-700/30',
        'info': 'text-blue-400 bg-blue-700/30',
        'warning': 'text-yellow-400 bg-yellow-700/30',
        'error': 'text-red-400 bg-red-700/30',
        'critical': 'text-red-500 bg-red-800/40'
    };

    const level = (entry.level || 'info').toLowerCase();
    const levelClass = levelColors[level] || levelColors.info;

    // Highlight search matches
    let messageHtml = escapeHtml(entry.message);
    if (debugLogsState.searchQuery) {
        const regex = new RegExp(`(${escapeRegex(debugLogsState.searchQuery)})`, 'gi');
        messageHtml = messageHtml.replace(regex, '<mark class="bg-yellow-500/40 text-yellow-200 px-0.5 rounded">$1</mark>');
    }

    // Parse and format timestamp with date
    let timeDisplay = '';
    if (entry.timestamp) {
        try {
            const date = new Date(entry.timestamp);
            const dateStr = date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
            const timeStr = date.toLocaleTimeString('en-US', { hour12: false }) + '.' +
                           String(date.getMilliseconds()).padStart(3, '0');
            timeDisplay = `${dateStr} ${timeStr}`;
        } catch (e) {
            timeDisplay = entry.timestamp;
        }
    }

    return `
        <div class="log-entry group hover:bg-gray-800/50 px-3 py-2 rounded transition-colors border-l-2 ${getBorderColor(level)} relative">
            <div class="flex items-start space-x-3">
                <div class="flex-shrink-0 w-36">
                    <span class="text-gray-500 font-mono text-xs">${timeDisplay}</span>
                </div>
                <div class="flex-shrink-0">
                    <span class="px-2 py-0.5 rounded text-xs font-medium uppercase ${levelClass}">
                        ${level}
                    </span>
                </div>
                ${entry.service ? `
                    <div class="flex-shrink-0">
                        <span class="text-purple-400 text-xs font-mono">${entry.service}</span>
                    </div>
                ` : ''}
                ${entry.event ? `
                    <div class="flex-shrink-0">
                        <span class="text-cyan-400 text-xs font-mono">${entry.event}</span>
                    </div>
                ` : ''}
            </div>
            <div class="mt-1 ml-36 pl-3">
                <span class="text-gray-300 text-sm font-mono break-all">${messageHtml}</span>
            </div>
            <button
                onclick="showRawLog(${entry.line_number})"
                class="hidden group-hover:flex absolute right-3 top-2 text-gray-500 hover:text-gray-300 text-xs items-center gap-1"
            >
                <i data-lucide="code" class="w-3 h-3"></i>
                <span>Raw</span>
            </button>
        </div>
    `;
}

function renderTableView() {
    const container = document.getElementById('log-entries');
    if (!container) return;

    if (debugLogsState.entries.length === 0) {
        container.innerHTML = `
            <div class="text-center py-12 text-gray-500">
                <i data-lucide="search" class="w-8 h-8 mx-auto mb-3 opacity-50"></i>
                <p>No matching log entries</p>
                ${debugLogsState.searchQuery ? '<p class="text-sm mt-2">Try adjusting your search</p>' : ''}
            </div>
        `;
        if (typeof lucide !== 'undefined') lucide.createIcons();
        return;
    }

    const sortIcon = (field) => {
        if (debugLogsState.sortField !== field) return '';
        return debugLogsState.sortDirection === 'asc'
            ? '<i data-lucide="chevron-up" class="w-3 h-3 inline ml-1"></i>'
            : '<i data-lucide="chevron-down" class="w-3 h-3 inline ml-1"></i>';
    };

    container.innerHTML = `
        <div class="overflow-x-auto">
            <table class="w-full text-sm">
                <thead class="bg-gray-800/50 sticky top-0">
                    <tr>
                        <th onclick="toggleSort('line_number')" class="cursor-pointer px-3 py-2 text-left text-gray-400 font-medium hover:text-white transition-colors">
                            Line${sortIcon('line_number')}
                        </th>
                        <th onclick="toggleSort('timestamp')" class="cursor-pointer px-3 py-2 text-left text-gray-400 font-medium hover:text-white transition-colors">
                            Timestamp${sortIcon('timestamp')}
                        </th>
                        <th onclick="toggleSort('level')" class="cursor-pointer px-3 py-2 text-left text-gray-400 font-medium hover:text-white transition-colors">
                            Level${sortIcon('level')}
                        </th>
                        <th onclick="toggleSort('service')" class="cursor-pointer px-3 py-2 text-left text-gray-400 font-medium hover:text-white transition-colors">
                            Service${sortIcon('service')}
                        </th>
                        <th onclick="toggleSort('event')" class="cursor-pointer px-3 py-2 text-left text-gray-400 font-medium hover:text-white transition-colors">
                            Event${sortIcon('event')}
                        </th>
                        <th class="px-3 py-2 text-left text-gray-400 font-medium">Message</th>
                        <th class="px-3 py-2 text-center text-gray-400 font-medium w-16">Raw</th>
                    </tr>
                </thead>
                <tbody class="divide-y divide-gray-800">
                    ${debugLogsState.entries.map(entry => renderTableRow(entry)).join('')}
                </tbody>
            </table>
        </div>
    `;
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

function renderTableRow(entry) {
    const levelColors = {
        'debug': 'text-gray-400 bg-gray-700/30',
        'info': 'text-blue-400 bg-blue-700/30',
        'warning': 'text-yellow-400 bg-yellow-700/30',
        'error': 'text-red-400 bg-red-700/30',
        'critical': 'text-red-500 bg-red-800/40'
    };

    const level = (entry.level || 'info').toLowerCase();
    const levelClass = levelColors[level] || levelColors.info;

    // Highlight search matches
    let messageHtml = escapeHtml(entry.message);
    if (debugLogsState.searchQuery) {
        const regex = new RegExp(`(${escapeRegex(debugLogsState.searchQuery)})`, 'gi');
        messageHtml = messageHtml.replace(regex, '<mark class="bg-yellow-500/40 text-yellow-200 px-0.5 rounded">$1</mark>');
    }

    // Parse timestamp with date
    let timeDisplay = '';
    if (entry.timestamp) {
        try {
            const date = new Date(entry.timestamp);
            const dateStr = date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
            const timeStr = date.toLocaleTimeString('en-US', { hour12: false }) + '.' +
                           String(date.getMilliseconds()).padStart(3, '0');
            timeDisplay = `${dateStr} ${timeStr}`;
        } catch (e) {
            timeDisplay = entry.timestamp;
        }
    }

    const rowClass = level === 'error' || level === 'critical'
        ? 'bg-red-900/10 hover:bg-red-900/20'
        : level === 'warning'
        ? 'bg-yellow-900/10 hover:bg-yellow-900/20'
        : 'hover:bg-gray-800/50';

    return `
        <tr class="${rowClass} transition-colors">
            <td class="px-3 py-2 font-mono text-gray-500 text-xs">${entry.line_number}</td>
            <td class="px-3 py-2 font-mono text-gray-400 text-xs whitespace-nowrap">${timeDisplay}</td>
            <td class="px-3 py-2">
                <span class="px-2 py-0.5 rounded text-xs font-medium uppercase ${levelClass}">${level}</span>
            </td>
            <td class="px-3 py-2 font-mono text-purple-400 text-xs">${entry.service || '-'}</td>
            <td class="px-3 py-2 font-mono text-cyan-400 text-xs">${entry.event || '-'}</td>
            <td class="px-3 py-2 text-gray-300 text-xs max-w-md truncate font-mono" title="${escapeHtml(entry.message)}">${messageHtml}</td>
            <td class="px-3 py-2 text-center">
                <button onclick="showRawLog(${entry.line_number})" class="text-gray-500 hover:text-gray-300">
                    <i data-lucide="code" class="w-4 h-4"></i>
                </button>
            </td>
        </tr>
    `;
}

function getBorderColor(level) {
    const colors = {
        'debug': 'border-gray-600',
        'info': 'border-blue-500',
        'warning': 'border-yellow-500',
        'error': 'border-red-500',
        'critical': 'border-red-600'
    };
    return colors[level] || colors.info;
}

function showRawLog(lineNumber) {
    const entry = debugLogsState.entries.find(e => e.line_number === lineNumber);
    if (!entry) return;

    // Format JSON nicely if possible
    let formattedRaw = entry.raw;
    try {
        const parsed = JSON.parse(entry.raw);
        formattedRaw = JSON.stringify(parsed, null, 2);
    } catch (e) {
        // Not JSON, use as-is
    }

    const modal = document.createElement('div');
    modal.className = 'fixed inset-0 bg-black/70 flex items-center justify-center z-50';
    modal.onclick = (e) => { if (e.target === modal) modal.remove(); };

    modal.innerHTML = `
        <div class="bg-gray-900 border border-gray-700 rounded-lg max-w-4xl w-full mx-4 max-h-[80vh] overflow-hidden shadow-2xl">
            <div class="flex items-center justify-between p-4 border-b border-gray-700 bg-gray-800">
                <h3 class="text-lg font-semibold text-white">Raw Log Entry (Line ${lineNumber})</h3>
                <button onclick="this.closest('.fixed').remove()" class="text-gray-400 hover:text-white transition-colors">
                    <i data-lucide="x" class="w-5 h-5"></i>
                </button>
            </div>
            <div class="p-4 overflow-auto max-h-[60vh] bg-gray-950">
                <pre class="text-sm font-mono text-gray-300 whitespace-pre-wrap break-all">${escapeHtml(formattedRaw)}</pre>
            </div>
            <div class="p-4 border-t border-gray-700 bg-gray-800 flex justify-end space-x-3">
                <button onclick="copyToClipboard(atob('${btoa(entry.raw)}')); showToast('Copied to clipboard', 'success');"
                    class="px-4 py-2 bg-purple-600 hover:bg-purple-700 rounded-lg text-sm text-white transition-colors">
                    <i data-lucide="copy" class="w-4 h-4 inline mr-2"></i>Copy
                </button>
                <button onclick="this.closest('.fixed').remove()"
                    class="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm text-white transition-colors">
                    Close
                </button>
            </div>
        </div>
    `;

    document.body.appendChild(modal);
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

function setupDebugLogEventListeners() {
    // Search input
    const searchInput = document.getElementById('log-search-input');
    if (searchInput) {
        let debounceTimer;
        searchInput.addEventListener('input', (e) => {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => {
                debugLogsState.searchQuery = e.target.value;
                loadLogEntries();
            }, 300);
        });

        // Clear search on Escape
        searchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                searchInput.value = '';
                debugLogsState.searchQuery = '';
                loadLogEntries();
            }
        });
    }

    // Level filter
    const levelFilter = document.getElementById('log-level-filter');
    if (levelFilter) {
        levelFilter.addEventListener('change', (e) => {
            debugLogsState.levelFilter = e.target.value;
            loadLogEntries();
        });
    }

    // Auto-refresh toggle
    const autoRefreshToggle = document.getElementById('log-auto-refresh');
    if (autoRefreshToggle) {
        autoRefreshToggle.addEventListener('change', (e) => {
            debugLogsState.autoRefresh = e.target.checked;
            if (debugLogsState.autoRefresh) {
                debugLogsState.autoRefreshInterval = setInterval(() => loadLogEntries(), 5000);
                showToast('Auto-refresh enabled (5s)', 'info');
            } else {
                clearInterval(debugLogsState.autoRefreshInterval);
                showToast('Auto-refresh disabled', 'info');
            }
        });
    }

    // Refresh button
    const refreshBtn = document.getElementById('log-refresh-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => {
            loadLogFiles();
            loadLogEntries();
        });
    }

    // Tail button
    const tailBtn = document.getElementById('log-tail-btn');
    if (tailBtn) {
        tailBtn.addEventListener('click', tailLogFile);
    }

    // View mode buttons
    document.querySelectorAll('.view-mode-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            setViewMode(btn.dataset.mode);
        });
    });
}

async function tailLogFile() {
    if (!debugLogsState.selectedFile) {
        showToast('Select a log file first', 'warning');
        return;
    }

    try {
        const response = await fetch(`/api/debug-logs/tail/${debugLogsState.selectedFile}?lines=100`);
        if (!response.ok) throw new Error('Failed to tail log file');

        const result = await response.json();
        debugLogsState.entries = result.entries;
        sortEntries();
        renderLogView();

        // Scroll to bottom
        const container = document.getElementById('log-entries');
        if (container) {
            container.scrollTop = container.scrollHeight;
        }

        showToast(`Showing last ${result.returned_lines} lines`, 'success');
    } catch (error) {
        console.error('Error tailing log file:', error);
        showToast('Failed to tail log file', 'error');
    }
}

function updateLoadingState() {
    const loadingOverlay = document.getElementById('log-loading-overlay');
    if (loadingOverlay) {
        loadingOverlay.classList.toggle('hidden', !debugLogsState.loading);
    }
}

function clearSearch() {
    const searchInput = document.getElementById('log-search-input');
    if (searchInput) {
        searchInput.value = '';
    }
    debugLogsState.searchQuery = '';
    loadLogEntries();
}

// Utility functions
function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function escapeRegex(string) {
    return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
        // Toast handled by caller
    }).catch(() => {
        showToast('Failed to copy', 'error');
    });
}

// Export for navigation
window.initDebugLogs = initDebugLogs;
window.toggleSort = toggleSort;
window.setViewMode = setViewMode;
window.showRawLog = showRawLog;
window.selectLogFile = selectLogFile;
window.clearSearch = clearSearch;
