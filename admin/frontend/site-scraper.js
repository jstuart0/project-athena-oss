/**
 * Site Scraper Configuration
 * Manages URL access restrictions and domain whitelists
 */

// Load site scraper configuration
async function loadSiteScraperConfig() {
    try {
        const response = await fetch(`${API_BASE}/api/site-scraper/config`, {
            headers: {
                'Authorization': `Bearer ${authToken}`
            }
        });
        if (!response.ok) throw new Error('Failed to load config');
        return await response.json();
    } catch (error) {
        console.error('Error loading site scraper config:', error);
        return null;
    }
}

// Save site scraper configuration
async function saveSiteScraperConfig(config) {
    try {
        const response = await fetch(`${API_BASE}/api/site-scraper/config`, {
            method: 'PUT',
            headers: {
                'Authorization': `Bearer ${authToken}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(config)
        });

        if (!response.ok) throw new Error('Failed to save config');

        showToast('Site scraper configuration saved', 'success');
        return await response.json();
    } catch (error) {
        console.error('Error saving site scraper config:', error);
        showToast('Failed to save configuration', 'error');
        return null;
    }
}

// Initialize site scraper tab
async function initSiteScraperTab() {
    const container = document.getElementById('site-scraper-config');
    if (!container) return;

    container.innerHTML = '<div class="text-center py-4">Loading configuration...</div>';

    const config = await loadSiteScraperConfig();
    if (!config) {
        container.innerHTML = '<div class="text-red-500 py-4">Failed to load configuration</div>';
        return;
    }

    container.innerHTML = `
        <form id="site-scraper-form" class="space-y-6">
            <!-- Owner Mode Settings -->
            <div class="bg-gray-800 rounded-lg p-4">
                <h4 class="text-lg font-medium text-white mb-3">Owner Mode Settings</h4>
                <label class="flex items-center gap-3 cursor-pointer">
                    <input type="checkbox" id="owner-any-url"
                           class="w-5 h-5 rounded bg-gray-700 border-gray-600 text-blue-500 focus:ring-blue-500"
                           ${config.owner_mode_any_url ? 'checked' : ''}>
                    <span class="text-gray-300">Allow scraping any URL</span>
                </label>
                <p class="text-gray-500 text-sm mt-1 ml-8">When enabled, owner mode can scrape any website</p>
            </div>

            <!-- Guest Mode Settings -->
            <div class="bg-gray-800 rounded-lg p-4">
                <h4 class="text-lg font-medium text-white mb-3">Guest Mode Settings</h4>
                <label class="flex items-center gap-3 cursor-pointer">
                    <input type="checkbox" id="guest-any-url"
                           class="w-5 h-5 rounded bg-gray-700 border-gray-600 text-blue-500 focus:ring-blue-500"
                           ${config.guest_mode_any_url ? 'checked' : ''}>
                    <span class="text-gray-300">Allow scraping any URL</span>
                </label>
                <p class="text-gray-500 text-sm mt-1 ml-8">When disabled, guests can only scrape whitelisted domains</p>
            </div>

            <!-- Domain Whitelist -->
            <div class="bg-gray-800 rounded-lg p-4">
                <h4 class="text-lg font-medium text-white mb-3">Domain Whitelist (Guest Mode)</h4>
                <textarea id="allowed-domains" rows="4"
                    class="w-full bg-gray-700 border border-gray-600 rounded-lg p-3 text-white placeholder-gray-400 focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                    placeholder="Enter domains, one per line (e.g., example.com)">${(config.allowed_domains || []).join('\n')}</textarea>
                <p class="text-gray-500 text-sm mt-2">Domains guests can scrape when "any URL" is disabled. Leave empty to allow all.</p>
            </div>

            <!-- Domain Blacklist -->
            <div class="bg-gray-800 rounded-lg p-4">
                <h4 class="text-lg font-medium text-white mb-3">Domain Blacklist (All Modes)</h4>
                <textarea id="blocked-domains" rows="4"
                    class="w-full bg-gray-700 border border-gray-600 rounded-lg p-3 text-white placeholder-gray-400 focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                    placeholder="Enter domains to block, one per line">${(config.blocked_domains || []).join('\n')}</textarea>
                <p class="text-gray-500 text-sm mt-2">These domains are blocked for ALL users (owner and guest)</p>
            </div>

            <!-- Advanced Settings -->
            <div class="bg-gray-800 rounded-lg p-4">
                <h4 class="text-lg font-medium text-white mb-3">Advanced Settings</h4>
                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label for="max-content-length" class="block text-gray-300 mb-2">Max Content Length (chars)</label>
                        <input type="number" id="max-content-length"
                            class="w-full bg-gray-700 border border-gray-600 rounded-lg p-3 text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                            value="${config.max_content_length}" min="1000" max="500000">
                    </div>
                    <div>
                        <label for="cache-ttl" class="block text-gray-300 mb-2">Cache TTL (seconds)</label>
                        <input type="number" id="cache-ttl"
                            class="w-full bg-gray-700 border border-gray-600 rounded-lg p-3 text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                            value="${config.cache_ttl}" min="60" max="86400">
                    </div>
                </div>
            </div>

            <!-- Submit Button -->
            <div class="flex justify-end">
                <button type="submit"
                    class="px-6 py-3 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors">
                    Save Configuration
                </button>
            </div>
        </form>
    `;

    // Add form submit handler
    document.getElementById('site-scraper-form').addEventListener('submit', async (e) => {
        e.preventDefault();

        const newConfig = {
            owner_mode_any_url: document.getElementById('owner-any-url').checked,
            guest_mode_any_url: document.getElementById('guest-any-url').checked,
            allowed_domains: document.getElementById('allowed-domains').value
                .split('\n').map(d => d.trim()).filter(d => d),
            blocked_domains: document.getElementById('blocked-domains').value
                .split('\n').map(d => d.trim()).filter(d => d),
            max_content_length: parseInt(document.getElementById('max-content-length').value),
            cache_ttl: parseInt(document.getElementById('cache-ttl').value)
        };

        await saveSiteScraperConfig(newConfig);
    });
}
