/**
 * Model Downloads UI
 *
 * Search, download, and manage models from Hugging Face Hub.
 * Supports GGUF (Ollama) and MLX (Apple Silicon) formats with filtering.
 */

let downloads = [];
let searchResults = [];
let selectedRepoFiles = [];
let selectedRepo = null;

// Default filter state
const defaultFilters = {
    query: '',
    model_format: 'gguf',
    quantizations: [],
    tool_support: false,
    author: '',
    limit: 20
};

let currentFilters = { ...defaultFilters };

// Quantization options
const QUANTIZATION_OPTIONS = [
    { value: 'Q4_K_M', label: 'Q4_K_M', desc: 'Good balance (4-bit)' },
    { value: 'Q5_K_M', label: 'Q5_K_M', desc: 'Better quality (5-bit)' },
    { value: 'Q8_0', label: 'Q8_0', desc: 'Best quality (8-bit)' },
    { value: 'Q4_0', label: 'Q4_0', desc: 'Smaller (4-bit)' },
    { value: 'Q3_K_M', label: 'Q3_K_M', desc: 'Compact (3-bit)' },
    { value: 'Q6_K', label: 'Q6_K', desc: 'High quality (6-bit)' }
];

/**
 * Load current downloads
 */
async function loadDownloads() {
    try {
        const response = await fetch('/api/model-downloads', {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error(`Failed to load downloads: ${response.statusText}`);
        }

        downloads = await response.json();
        renderDownloads();
    } catch (error) {
        console.error('Failed to load downloads:', error);
        safeShowToast('Failed to load downloads', 'error');
    }
}

/**
 * Search Hugging Face Hub
 */
async function searchHuggingFace() {
    // Capture query from input field
    const queryInput = document.getElementById('search-query');
    if (queryInput) {
        currentFilters.query = queryInput.value;
    }

    const searchBtn = document.getElementById('search-btn');
    if (searchBtn) {
        searchBtn.disabled = true;
        searchBtn.innerHTML = `
            <svg class="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            Searching...
        `;
    }

    console.log('Searching with filters:', currentFilters);

    try {
        const response = await fetch('/api/model-downloads/search', {
            method: 'POST',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(currentFilters)
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Search failed');
        }

        searchResults = await response.json();
        console.log('Search results:', searchResults);
        console.log('searchResults length:', searchResults.length);
        console.log('About to call renderModelSearchResults...');
        try {
            renderModelSearchResults();
            console.log('renderModelSearchResults completed');
        } catch (renderError) {
            console.error('Error in renderModelSearchResults:', renderError);
        }
        safeShowToast(`Found ${searchResults.length} models`, 'success');

    } catch (error) {
        console.error('Search failed:', error);
        safeShowToast(error.message, 'error');
    } finally {
        if (searchBtn) {
            searchBtn.disabled = false;
            searchBtn.innerHTML = `
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                </svg>
                Search
            `;
        }
    }
}

/**
 * Load files for a repository
 */
async function loadRepoFiles(repoId) {
    selectedRepo = repoId;

    try {
        const encodedRepo = encodeURIComponent(repoId);
        const response = await fetch(`/api/model-downloads/repo/${encodedRepo}/files?format_filter=${currentFilters.model_format}`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error('Failed to load files');
        }

        selectedRepoFiles = await response.json();
        showRepoFilesModal(repoId, selectedRepoFiles);

    } catch (error) {
        console.error('Failed to load repo files:', error);
        safeShowToast('Failed to load repository files', 'error');
    }
}

/**
 * Start a download
 */
async function startDownload(repoId, filename, quantization) {
    try {
        const response = await fetch('/api/model-downloads', {
            method: 'POST',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                repo_id: repoId,
                filename: filename,
                model_format: currentFilters.model_format,
                quantization: quantization
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to start download');
        }

        const download = await response.json();
        safeShowToast(`Download started: ${filename}`, 'success');
        closeRepoFilesModal();
        await loadDownloads();

    } catch (error) {
        console.error('Failed to start download:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Cancel a download
 */
async function cancelDownload(downloadId) {
    try {
        const response = await fetch(`/api/model-downloads/${downloadId}/cancel`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to cancel');
        }

        safeShowToast('Download cancelled', 'success');
        await loadDownloads();

    } catch (error) {
        console.error('Failed to cancel download:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Retry a failed download
 */
async function retryDownload(downloadId) {
    try {
        const response = await fetch(`/api/model-downloads/${downloadId}/retry`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to retry');
        }

        safeShowToast('Download retried', 'success');
        await loadDownloads();

    } catch (error) {
        console.error('Failed to retry download:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Delete a download
 */
async function deleteDownload(downloadId, filename) {
    if (!confirm(`Delete download "${filename}"? This will also delete the file if downloaded.`)) {
        return;
    }

    try {
        const response = await fetch(`/api/model-downloads/${downloadId}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to delete');
        }

        safeShowToast('Download deleted', 'success');
        await loadDownloads();

    } catch (error) {
        console.error('Failed to delete download:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Import to Ollama
 */
async function importToOllama(downloadId) {
    const download = downloads.find(d => d.id === downloadId);
    if (!download) return;

    // Suggest model name
    const suggestedName = download.filename
        .replace(/\.gguf$/i, '')
        .replace(/[^a-zA-Z0-9_-]/g, '-')
        .toLowerCase();

    const modelName = prompt('Enter model name for Ollama:', suggestedName);
    if (!modelName) return;

    try {
        const response = await fetch(`/api/model-downloads/${downloadId}/import-ollama`, {
            method: 'POST',
            headers: {
                ...getAuthHeaders(),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ model_name: modelName })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Import failed');
        }

        safeShowToast(`Model imported as "${modelName}"`, 'success');
        await loadDownloads();

    } catch (error) {
        console.error('Failed to import to Ollama:', error);
        safeShowToast(error.message, 'error');
    }
}

/**
 * Render the main page
 */
function renderModelDownloadsPage() {
    const container = document.getElementById('model-downloads-container');
    if (!container) return;

    const html = `
        <!-- Search Section -->
        <div class="bg-dark-card border border-dark-border rounded-xl p-6 mb-6">
            <div class="flex items-center justify-between mb-4">
                <h3 class="text-lg font-semibold text-white flex items-center gap-2">
                    <span class="text-2xl">&#129303;</span>
                    Search Hugging Face Hub
                </h3>
            </div>

            <!-- Search Input -->
            <div class="flex gap-4 mb-4">
                <div class="flex-1">
                    <input type="text" id="search-query" placeholder="Search models..."
                           class="w-full px-4 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
                           value="${currentFilters.query}"
                           onkeyup="if(event.key==='Enter') searchHuggingFace()">
                </div>
                <button id="search-btn" onclick="searchHuggingFace()"
                        class="px-6 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors flex items-center gap-2">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                    </svg>
                    Search
                </button>
            </div>

            <!-- Filters -->
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                <!-- Format Filter -->
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Format</label>
                    <div class="flex gap-2">
                        <label class="flex items-center gap-2 cursor-pointer">
                            <input type="radio" name="format" value="gguf" ${currentFilters.model_format === 'gguf' ? 'checked' : ''}
                                   onchange="updateFilter('model_format', 'gguf')"
                                   class="w-4 h-4 bg-dark-bg border-dark-border">
                            <span class="text-sm text-gray-300">GGUF</span>
                        </label>
                        <label class="flex items-center gap-2 cursor-pointer">
                            <input type="radio" name="format" value="mlx" ${currentFilters.model_format === 'mlx' ? 'checked' : ''}
                                   onchange="updateFilter('model_format', 'mlx')"
                                   class="w-4 h-4 bg-dark-bg border-dark-border">
                            <span class="text-sm text-gray-300">MLX</span>
                        </label>
                        <label class="flex items-center gap-2 cursor-pointer">
                            <input type="radio" name="format" value="all" ${currentFilters.model_format === 'all' ? 'checked' : ''}
                                   onchange="updateFilter('model_format', 'all')"
                                   class="w-4 h-4 bg-dark-bg border-dark-border">
                            <span class="text-sm text-gray-300">All</span>
                        </label>
                    </div>
                </div>

                <!-- Author Filter -->
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Author</label>
                    <input type="text" id="author-filter" placeholder="e.g., TheBloke"
                           class="w-full px-3 py-1.5 bg-dark-bg border border-dark-border rounded-lg text-white text-sm"
                           value="${currentFilters.author}"
                           onchange="updateFilter('author', this.value)">
                </div>

                <!-- Tool Support -->
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Capabilities</label>
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="checkbox" ${currentFilters.tool_support ? 'checked' : ''}
                               onchange="updateFilter('tool_support', this.checked)"
                               class="w-4 h-4 rounded bg-dark-bg border-dark-border">
                        <span class="text-sm text-gray-300">Tool/Function Calling</span>
                    </label>
                </div>

                <!-- Quantizations -->
                <div>
                    <label class="block text-sm font-medium text-gray-400 mb-2">Quantization</label>
                    <div class="flex flex-wrap gap-1">
                        ${QUANTIZATION_OPTIONS.slice(0, 3).map(q => `
                            <label class="flex items-center gap-1 cursor-pointer px-2 py-1 rounded border ${currentFilters.quantizations.includes(q.value) ? 'border-blue-500 bg-blue-500/20' : 'border-dark-border'} text-xs">
                                <input type="checkbox" ${currentFilters.quantizations.includes(q.value) ? 'checked' : ''}
                                       onchange="toggleQuantization('${q.value}')"
                                       class="hidden">
                                <span class="text-gray-300">${q.label}</span>
                            </label>
                        `).join('')}
                    </div>
                </div>
            </div>
        </div>

        <!-- Search Results -->
        <div id="search-results-container" class="mb-6">
            ${searchResults.length > 0 ? renderModelSearchResultsHTML() : `
                <div class="bg-dark-card border border-dark-border rounded-xl p-8 text-center">
                    <div class="text-4xl mb-4">&#128269;</div>
                    <p class="text-gray-400">Search Hugging Face to find models</p>
                </div>
            `}
        </div>

        <!-- Downloads Section -->
        <div class="bg-dark-card border border-dark-border rounded-xl p-6">
            <h3 class="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                <span class="text-2xl">&#128229;</span>
                Downloads
            </h3>
            <div id="downloads-container">
                ${renderDownloadsHTML()}
            </div>
        </div>
    `;

    container.innerHTML = html;
}

/**
 * Escape HTML to prevent XSS and template issues
 */
function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

/**
 * Render model search results (unique name to avoid conflicts)
 */
function renderModelSearchResults() {
    console.log('=== renderModelSearchResults CALLED ===');
    try {
        const container = document.getElementById('search-results-container');
        console.log('Container element:', container);
        console.log('searchResults variable:', searchResults);
        console.log('Render search results - container found:', !!container, 'results count:', searchResults ? searchResults.length : 'undefined');
        if (container) {
            const html = renderModelSearchResultsHTML();
            console.log('Generated HTML length:', html.length);
            container.innerHTML = html;
            console.log('Search results rendered successfully');
        } else {
            console.error('search-results-container not found!');
        }
    } catch (error) {
        console.error('Error rendering search results:', error);
        safeShowToast('Error displaying search results', 'error');
    }
}

function renderModelSearchResultsHTML() {
    if (searchResults.length === 0) {
        return `
            <div class="bg-dark-card border border-dark-border rounded-xl p-8 text-center">
                <div class="text-4xl mb-4">&#128533;</div>
                <p class="text-gray-400">No models found matching your criteria</p>
            </div>
        `;
    }

    const resultsHtml = searchResults.map(model => {
        const repoId = escapeHtml(model.repo_id || '');
        const downloads = model.downloads || 0;
        const likes = model.likes || 0;
        const updated = model.updated || '';
        const hasToolSupport = model.has_tool_support || false;
        const tags = Array.isArray(model.tags) ? model.tags : [];

        return `
            <div class="p-4 hover:bg-dark-bg/50 transition-colors">
                <div class="flex items-start justify-between">
                    <div class="flex-1">
                        <div class="flex items-center gap-2 mb-1">
                            <span class="text-lg">&#128230;</span>
                            <span class="font-medium text-white">${repoId}</span>
                            ${hasToolSupport ? '<span class="px-2 py-0.5 bg-green-500/20 text-green-400 text-xs rounded-full">Tools</span>' : ''}
                        </div>
                        <div class="flex items-center gap-4 text-sm text-gray-400">
                            <span>&#11015; ${formatNumber(downloads)}</span>
                            <span>&#10084; ${formatNumber(likes)}</span>
                            ${updated ? `<span>Updated: ${formatDate(updated)}</span>` : ''}
                        </div>
                        ${tags.length > 0 ? `
                            <div class="flex flex-wrap gap-1 mt-2">
                                ${tags.slice(0, 5).map(tag => `
                                    <span class="px-2 py-0.5 bg-dark-bg border border-dark-border rounded text-xs text-gray-400">${escapeHtml(tag)}</span>
                                `).join('')}
                            </div>
                        ` : ''}
                    </div>
                    <button onclick="loadRepoFiles('${repoId}')"
                            class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors">
                        View Files
                    </button>
                </div>
            </div>
        `;
    }).join('');

    return `
        <div class="bg-dark-card border border-dark-border rounded-xl overflow-hidden">
            <div class="p-4 border-b border-dark-border">
                <h3 class="text-lg font-semibold text-white">Search Results (${searchResults.length})</h3>
            </div>
            <div class="divide-y divide-dark-border">
                ${resultsHtml}
            </div>
        </div>
    `;
}

/**
 * Render downloads list
 */
function renderDownloads() {
    const container = document.getElementById('downloads-container');
    if (container) {
        container.innerHTML = renderDownloadsHTML();
    }
}

function renderDownloadsHTML() {
    if (downloads.length === 0) {
        return `
            <div class="text-center py-8">
                <div class="text-4xl mb-4">&#128230;</div>
                <p class="text-gray-400">No downloads yet</p>
            </div>
        `;
    }

    return `
        <div class="space-y-3">
            ${downloads.map(download => {
                const statusColors = {
                    'pending': 'bg-yellow-500/20 text-yellow-400',
                    'downloading': 'bg-blue-500/20 text-blue-400',
                    'completed': 'bg-green-500/20 text-green-400',
                    'failed': 'bg-red-500/20 text-red-400',
                    'cancelled': 'bg-gray-500/20 text-gray-400'
                };
                const statusColor = statusColors[download.status] || 'bg-gray-500/20 text-gray-400';

                return `
                    <div class="bg-dark-bg border border-dark-border rounded-lg p-4">
                        <div class="flex items-center justify-between mb-2">
                            <div>
                                <div class="font-medium text-white">${download.filename}</div>
                                <div class="text-sm text-gray-400">${download.repo_id}</div>
                            </div>
                            <span class="px-2 py-1 rounded-full text-xs font-medium ${statusColor}">
                                ${download.status}
                            </span>
                        </div>

                        ${download.status === 'downloading' ? `
                            <div class="mb-2">
                                <div class="flex justify-between text-sm mb-1">
                                    <span class="text-gray-400">${formatBytes(download.downloaded_bytes)} / ${formatBytes(download.file_size_bytes)}</span>
                                    <span class="text-gray-400">${download.progress_percent.toFixed(1)}%</span>
                                </div>
                                <div class="w-full bg-dark-border rounded-full h-2">
                                    <div class="bg-blue-500 h-2 rounded-full transition-all" style="width: ${download.progress_percent}%"></div>
                                </div>
                            </div>
                        ` : ''}

                        ${download.error_message ? `
                            <div class="text-sm text-red-400 mb-2">${download.error_message}</div>
                        ` : ''}

                        <div class="flex items-center justify-between">
                            <div class="text-xs text-gray-500">
                                ${download.quantization ? `Quant: ${download.quantization}` : ''}
                                ${download.file_size_bytes ? ` | Size: ${formatBytes(download.file_size_bytes)}` : ''}
                            </div>
                            <div class="flex items-center gap-2">
                                ${download.status === 'downloading' ? `
                                    <button onclick="cancelDownload(${download.id})" class="px-3 py-1 text-sm text-red-400 hover:bg-red-500/20 rounded transition-colors">
                                        Cancel
                                    </button>
                                ` : ''}
                                ${download.status === 'failed' || download.status === 'cancelled' ? `
                                    <button onclick="retryDownload(${download.id})" class="px-3 py-1 text-sm text-blue-400 hover:bg-blue-500/20 rounded transition-colors">
                                        Retry
                                    </button>
                                ` : ''}
                                ${download.status === 'completed' && download.model_format === 'gguf' && !download.ollama_imported ? `
                                    <button onclick="importToOllama(${download.id})" class="px-3 py-1 text-sm text-green-400 hover:bg-green-500/20 rounded transition-colors">
                                        Import to Ollama
                                    </button>
                                ` : ''}
                                ${download.ollama_imported ? `
                                    <span class="px-3 py-1 text-sm text-green-400">&#10003; ${download.ollama_model_name}</span>
                                ` : ''}
                                <button onclick="deleteDownload(${download.id}, '${download.filename}')" class="px-3 py-1 text-sm text-gray-400 hover:bg-red-500/20 hover:text-red-400 rounded transition-colors">
                                    Delete
                                </button>
                            </div>
                        </div>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

/**
 * Show repo files modal
 */
function showRepoFilesModal(repoId, files) {
    const html = `
        <div id="repo-files-modal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onclick="if(event.target.id === 'repo-files-modal') closeRepoFilesModal()">
            <div class="bg-dark-card border border-dark-border rounded-xl w-full max-w-2xl max-h-[80vh] overflow-hidden m-4">
                <div class="p-4 border-b border-dark-border flex items-center justify-between">
                    <div>
                        <h3 class="text-lg font-semibold text-white">${repoId}</h3>
                        <p class="text-sm text-gray-400">${files.length} files available</p>
                    </div>
                    <button onclick="closeRepoFilesModal()" class="p-2 hover:bg-dark-bg rounded-lg transition-colors">
                        <svg class="w-5 h-5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                        </svg>
                    </button>
                </div>
                <div class="p-4 overflow-y-auto max-h-[60vh]">
                    ${files.length === 0 ? `
                        <div class="text-center py-8">
                            <p class="text-gray-400">No ${currentFilters.model_format} files found</p>
                        </div>
                    ` : `
                        <div class="space-y-2">
                            ${files.map(file => `
                                <div class="flex items-center justify-between p-3 bg-dark-bg border border-dark-border rounded-lg hover:border-blue-500/50 transition-colors">
                                    <div class="flex-1 min-w-0">
                                        <div class="font-medium text-white truncate">${file.filename}</div>
                                        <div class="flex items-center gap-3 text-sm text-gray-400">
                                            <span>${file.size_gb.toFixed(2)} GB</span>
                                            ${file.quantization ? `<span class="px-2 py-0.5 bg-purple-500/20 text-purple-400 rounded">${file.quantization}</span>` : ''}
                                        </div>
                                    </div>
                                    <button onclick="startDownload('${repoId}', '${file.filename}', '${file.quantization || ''}')"
                                            class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors whitespace-nowrap ml-4">
                                        Download
                                    </button>
                                </div>
                            `).join('')}
                        </div>
                    `}
                </div>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', html);
}

function closeRepoFilesModal() {
    const modal = document.getElementById('repo-files-modal');
    if (modal) {
        modal.remove();
    }
}

/**
 * Update filter value
 */
function updateFilter(key, value) {
    currentFilters[key] = value;

    // Update query from input
    const queryInput = document.getElementById('search-query');
    if (queryInput) {
        currentFilters.query = queryInput.value;
    }
}

/**
 * Toggle quantization filter
 */
function toggleQuantization(quant) {
    const idx = currentFilters.quantizations.indexOf(quant);
    if (idx === -1) {
        currentFilters.quantizations.push(quant);
    } else {
        currentFilters.quantizations.splice(idx, 1);
    }
    renderModelDownloadsPage();
}

/**
 * Format helpers
 */
function formatNumber(num) {
    if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
    if (num >= 1000) return (num / 1000).toFixed(1) + 'K';
    return num.toString();
}

function formatBytes(bytes) {
    if (!bytes) return '0 B';
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return (bytes / Math.pow(1024, i)).toFixed(2) + ' ' + sizes[i];
}

function formatDate(dateStr) {
    if (!dateStr) return '';
    const date = new Date(dateStr);
    return date.toLocaleDateString();
}

/**
 * Handle WebSocket events for download progress
 */
function handleDownloadEvent(event) {
    const eventType = event.event_type;
    const data = event.data;

    if (!eventType || !eventType.startsWith('model_download_')) return;

    switch (eventType) {
        case 'model_download_started':
        case 'model_download_completed':
        case 'model_download_failed':
        case 'model_download_cancelled':
            loadDownloads();
            break;

        case 'model_download_progress':
            // Update progress inline
            const download = downloads.find(d => d.id === data.download_id);
            if (download) {
                download.progress_percent = data.progress_percent;
                download.downloaded_bytes = data.downloaded_bytes;
                renderDownloads();
            }
            break;
    }
}

/**
 * Initialize the page
 */
function initModelDownloadsPage() {
    console.log('Initializing Model Downloads page');
    renderModelDownloadsPage();
    loadDownloads();

    // Subscribe to WebSocket events
    if (typeof subscribeToWebSocket === 'function') {
        subscribeToWebSocket(handleDownloadEvent);
    }
}

// Export for external use
if (typeof window !== 'undefined') {
    window.initModelDownloadsPage = initModelDownloadsPage;
    window.loadDownloads = loadDownloads;
    window.searchHuggingFace = searchHuggingFace;
    window.loadRepoFiles = loadRepoFiles;
    window.startDownload = startDownload;
    window.cancelDownload = cancelDownload;
    window.retryDownload = retryDownload;
    window.deleteDownload = deleteDownload;
    window.importToOllama = importToOllama;
    window.updateFilter = updateFilter;
    window.toggleQuantization = toggleQuantization;
    window.closeRepoFilesModal = closeRepoFilesModal;
    window.handleDownloadEvent = handleDownloadEvent;
}
