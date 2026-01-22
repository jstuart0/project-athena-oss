/**
 * Athena Memory & Context - Tabbed Interface Page
 *
 * Provides management for:
 * - Household Context - Persistent context about the household
 * - Guest Profiles - Guest access and preferences
 * - Base Knowledge - Location, timezone, defaults
 */
(function(Athena) {
    'use strict';

    if (!Athena) {
        console.error('[MemoryContext] Athena namespace not found');
        return;
    }

    // Page state
    const state = {
        activeTab: 'household',
        data: {}
    };

    /**
     * Initialize Memory & Context page.
     */
    async function init() {
        console.log('[MemoryContext] Initializing');

        const container = document.getElementById('memory-context-content');
        if (!container) {
            console.warn('[MemoryContext] Container not found');
            return;
        }

        // Render layout
        container.innerHTML = renderLayout();

        // Initialize icons
        if (window.lucide) {
            lucide.createIcons({ nodes: [container] });
        }

        // Load initial tab
        await loadTab(state.activeTab);

        console.log('[MemoryContext] Initialized');
    }

    /**
     * Destroy page and cleanup.
     */
    function destroy() {
        state.data = {};
        console.log('[MemoryContext] Destroyed');
    }

    /**
     * Render the page layout with tab navigation.
     */
    function renderLayout() {
        return `
            <!-- Tab Navigation -->
            <div class="flex border-b border-dark-border mb-6">
                <button onclick="Athena.pages.MemoryContext.switchTab('household')"
                        class="tab-btn px-6 py-3 text-sm font-medium text-blue-400 border-b-2 border-blue-400 transition-colors"
                        data-tab="household">
                    <i data-lucide="home" class="w-4 h-4 inline mr-2"></i>
                    Household Context
                </button>
                <button onclick="Athena.pages.MemoryContext.switchTab('guests')"
                        class="tab-btn px-6 py-3 text-sm font-medium text-gray-400 hover:text-white border-b-2 border-transparent transition-colors"
                        data-tab="guests">
                    <i data-lucide="users" class="w-4 h-4 inline mr-2"></i>
                    Guest Profiles
                </button>
                <button onclick="Athena.pages.MemoryContext.switchTab('base-knowledge')"
                        class="tab-btn px-6 py-3 text-sm font-medium text-gray-400 hover:text-white border-b-2 border-transparent transition-colors"
                        data-tab="base-knowledge">
                    <i data-lucide="database" class="w-4 h-4 inline mr-2"></i>
                    Base Knowledge
                </button>
            </div>

            <!-- Tab Content -->
            <div id="memory-tab-content">
                <div class="text-center py-8 text-gray-400">Loading...</div>
            </div>
        `;
    }

    /**
     * Switch to a different tab.
     */
    async function switchTab(tab) {
        state.activeTab = tab;

        // Update tab button styles
        document.querySelectorAll('.tab-btn').forEach(btn => {
            const isActive = btn.dataset.tab === tab;
            btn.classList.toggle('text-blue-400', isActive);
            btn.classList.toggle('border-blue-400', isActive);
            btn.classList.toggle('text-gray-400', !isActive);
            btn.classList.toggle('hover:text-white', !isActive);
            btn.classList.toggle('border-transparent', !isActive);
        });

        await loadTab(tab);
    }

    /**
     * Load content for a specific tab.
     */
    async function loadTab(tab) {
        const container = document.getElementById('memory-tab-content');
        if (!container) return;

        container.innerHTML = '<div class="text-center py-8 text-gray-400">Loading...</div>';

        try {
            switch (tab) {
                case 'household':
                    await loadHouseholdContext(container);
                    break;
                case 'guests':
                    await loadGuestProfiles(container);
                    break;
                case 'base-knowledge':
                    await loadBaseKnowledge(container);
                    break;
            }
        } catch (error) {
            console.error(`[MemoryContext] Failed to load ${tab}:`, error);
            container.innerHTML = `
                <div class="text-center py-8">
                    <p class="text-red-400">Failed to load ${tab} data</p>
                    <button onclick="Athena.pages.MemoryContext.loadTab('${tab}')"
                            class="mt-2 text-blue-400 hover:text-blue-300 text-sm">
                        Retry
                    </button>
                </div>
            `;
        }

        if (window.lucide) {
            lucide.createIcons({ nodes: [container] });
        }
    }

    /**
     * Load household context tab.
     */
    async function loadHouseholdContext(container) {
        let memories = [];

        try {
            const response = await Athena.api('/api/memories?scope=owner');
            memories = Array.isArray(response) ? response : (response.memories || []);
        } catch {
            // Use empty array if API not available
        }

        state.data.memories = memories;

        container.innerHTML = `
            <div class="flex justify-between items-center mb-4">
                <p class="text-sm text-gray-400">Persistent context about your household that Athena remembers</p>
                <button onclick="Athena.pages.MemoryContext.addHouseholdMemory()"
                        class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm rounded-lg transition-colors flex items-center gap-2">
                    <i data-lucide="plus" class="w-4 h-4"></i>
                    Add Memory
                </button>
            </div>

            <div class="bg-dark-card border border-dark-border rounded-xl overflow-hidden">
                <table class="data-table w-full">
                    <thead class="bg-dark-bg">
                        <tr>
                            <th class="text-left px-4 py-3 text-sm font-medium text-gray-400">Summary</th>
                            <th class="text-left px-4 py-3 text-sm font-medium text-gray-400">Content</th>
                            <th class="text-left px-4 py-3 text-sm font-medium text-gray-400">Updated</th>
                            <th class="text-right px-4 py-3 text-sm font-medium text-gray-400">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${memories.length > 0 ? memories.map(m => `
                            <tr class="border-t border-dark-border hover:bg-dark-bg/50 transition-colors">
                                <td class="px-4 py-3 text-white font-medium">${escapeHtml(m.summary || m.category || 'Memory')}</td>
                                <td class="px-4 py-3 text-gray-300 max-w-md truncate">${escapeHtml(m.content)}</td>
                                <td class="px-4 py-3 text-gray-500 text-sm">${formatDate(m.updated_at || m.created_at)}</td>
                                <td class="px-4 py-3 text-right">
                                    <button onclick="Athena.pages.MemoryContext.editMemory(${m.id})"
                                            class="text-blue-400 hover:text-blue-300 mr-3 text-sm">
                                        Edit
                                    </button>
                                    <button onclick="Athena.pages.MemoryContext.deleteMemory(${m.id})"
                                            class="text-red-400 hover:text-red-300 text-sm">
                                        Delete
                                    </button>
                                </td>
                            </tr>
                        `).join('') : `
                            <tr>
                                <td colspan="4" class="px-4 py-8 text-center text-gray-500">
                                    No household memories yet. Add context to help Athena understand your home.
                                </td>
                            </tr>
                        `}
                    </tbody>
                </table>
            </div>

            ${memories.length > 0 ? `
                <p class="mt-4 text-sm text-gray-500">
                    ${memories.length} ${memories.length === 1 ? 'memory' : 'memories'} stored
                </p>
            ` : ''}
        `;
    }

    /**
     * Load guest profiles tab.
     */
    async function loadGuestProfiles(container) {
        let guests = [];

        try {
            const response = await Athena.api('/api/guests');
            guests = response.guests || response || [];
        } catch {
            // Use empty array if API not available
        }

        state.data.guests = guests;

        container.innerHTML = `
            <div class="flex justify-between items-center mb-4">
                <p class="text-sm text-gray-400">Manage guest access and preferences for your household</p>
                <button onclick="Athena.pages.MemoryContext.addGuest()"
                        class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm rounded-lg transition-colors flex items-center gap-2">
                    <i data-lucide="user-plus" class="w-4 h-4"></i>
                    Add Guest
                </button>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                ${guests.length > 0 ? guests.map(g => `
                    <div class="bg-dark-card border border-dark-border rounded-xl p-4 hover:border-dark-border-hover transition-colors">
                        <div class="flex items-center gap-3 mb-3">
                            <div class="w-10 h-10 rounded-full bg-purple-500/20 flex items-center justify-center">
                                <span class="text-purple-400 font-medium">${escapeHtml(g.name.charAt(0).toUpperCase())}</span>
                            </div>
                            <div class="flex-1 min-w-0">
                                <p class="text-white font-medium truncate">${escapeHtml(g.name)}</p>
                                <p class="text-gray-500 text-xs truncate">${g.phone_number || g.email || 'No contact info'}</p>
                            </div>
                        </div>
                        ${g.notes ? `
                            <p class="text-gray-400 text-sm mb-3 truncate">${escapeHtml(g.notes)}</p>
                        ` : ''}
                        <div class="flex items-center justify-between">
                            <span class="px-2 py-1 text-xs rounded-full ${g.is_active ? 'bg-green-500/20 text-green-400' : 'bg-gray-500/20 text-gray-400'}">
                                ${g.is_active ? 'Active' : 'Inactive'}
                            </span>
                            <div class="flex gap-2">
                                <button onclick="Athena.pages.MemoryContext.editGuest(${g.id})"
                                        class="text-blue-400 hover:text-blue-300 text-sm">
                                    Edit
                                </button>
                                <button onclick="Athena.pages.MemoryContext.deleteGuest(${g.id})"
                                        class="text-red-400 hover:text-red-300 text-sm">
                                    Remove
                                </button>
                            </div>
                        </div>
                    </div>
                `).join('') : `
                    <div class="col-span-full text-center py-8 bg-dark-card border border-dark-border rounded-xl">
                        <i data-lucide="users" class="w-12 h-12 text-gray-600 mx-auto mb-3"></i>
                        <p class="text-gray-500">No guests configured</p>
                        <p class="text-gray-600 text-sm mt-1">Add guests to give them limited access to Athena</p>
                    </div>
                `}
            </div>
        `;
    }

    /**
     * Load base knowledge tab.
     */
    async function loadBaseKnowledge(container) {
        let knowledge = {};

        try {
            const response = await Athena.api('/api/internal/config/base-knowledge');
            knowledge = response || {};
        } catch {
            // Use defaults if API not available
            knowledge = {
                city: 'Baltimore',
                state: 'MD',
                latitude: '39.2904',
                longitude: '-76.6122',
                timezone: 'America/New_York'
            };
        }

        state.data.knowledge = knowledge;

        container.innerHTML = `
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <!-- Location Settings -->
                <div class="bg-dark-card border border-dark-border rounded-xl">
                    <div class="p-4 border-b border-dark-border">
                        <h3 class="text-lg font-semibold text-white flex items-center gap-2">
                            <i data-lucide="map-pin" class="w-5 h-5 text-blue-400"></i>
                            Location Settings
                        </h3>
                        <p class="text-sm text-gray-500 mt-1">Used for weather, local search, and time-based features</p>
                    </div>
                    <div class="p-4 space-y-4">
                        <div class="grid grid-cols-2 gap-4">
                            <div>
                                <label class="block text-sm font-medium text-gray-400 mb-2">City</label>
                                <input type="text" id="knowledge-city" value="${escapeHtml(knowledge.city || '')}"
                                       class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500 transition-colors">
                            </div>
                            <div>
                                <label class="block text-sm font-medium text-gray-400 mb-2">State</label>
                                <input type="text" id="knowledge-state" value="${escapeHtml(knowledge.state || '')}"
                                       class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500 transition-colors">
                            </div>
                        </div>
                        <div class="grid grid-cols-2 gap-4">
                            <div>
                                <label class="block text-sm font-medium text-gray-400 mb-2">Latitude</label>
                                <input type="text" id="knowledge-latitude" value="${escapeHtml(knowledge.latitude || '')}"
                                       class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500 transition-colors">
                            </div>
                            <div>
                                <label class="block text-sm font-medium text-gray-400 mb-2">Longitude</label>
                                <input type="text" id="knowledge-longitude" value="${escapeHtml(knowledge.longitude || '')}"
                                       class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500 transition-colors">
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Time Settings -->
                <div class="bg-dark-card border border-dark-border rounded-xl">
                    <div class="p-4 border-b border-dark-border">
                        <h3 class="text-lg font-semibold text-white flex items-center gap-2">
                            <i data-lucide="clock" class="w-5 h-5 text-purple-400"></i>
                            Time Settings
                        </h3>
                        <p class="text-sm text-gray-500 mt-1">Timezone for scheduling and time-aware responses</p>
                    </div>
                    <div class="p-4 space-y-4">
                        <div>
                            <label class="block text-sm font-medium text-gray-400 mb-2">Timezone</label>
                            <select id="knowledge-timezone"
                                    class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500 transition-colors">
                                <option value="America/New_York" ${knowledge.timezone === 'America/New_York' ? 'selected' : ''}>Eastern (America/New_York)</option>
                                <option value="America/Chicago" ${knowledge.timezone === 'America/Chicago' ? 'selected' : ''}>Central (America/Chicago)</option>
                                <option value="America/Denver" ${knowledge.timezone === 'America/Denver' ? 'selected' : ''}>Mountain (America/Denver)</option>
                                <option value="America/Los_Angeles" ${knowledge.timezone === 'America/Los_Angeles' ? 'selected' : ''}>Pacific (America/Los_Angeles)</option>
                                <option value="UTC" ${knowledge.timezone === 'UTC' ? 'selected' : ''}>UTC</option>
                            </select>
                        </div>
                        <div class="p-3 bg-dark-bg rounded-lg">
                            <p class="text-sm text-gray-400">Current local time:</p>
                            <p id="current-time" class="text-lg text-white font-mono mt-1">--:--:--</p>
                        </div>
                    </div>
                </div>

                <!-- Additional Settings -->
                <div class="bg-dark-card border border-dark-border rounded-xl lg:col-span-2">
                    <div class="p-4 border-b border-dark-border">
                        <h3 class="text-lg font-semibold text-white flex items-center gap-2">
                            <i data-lucide="settings" class="w-5 h-5 text-green-400"></i>
                            Additional Settings
                        </h3>
                    </div>
                    <div class="p-4 grid grid-cols-1 md:grid-cols-3 gap-4">
                        <div>
                            <label class="block text-sm font-medium text-gray-400 mb-2">Temperature Unit</label>
                            <select id="knowledge-temp-unit"
                                    class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500 transition-colors">
                                <option value="F" ${knowledge.temp_unit === 'F' ? 'selected' : ''}>Fahrenheit (°F)</option>
                                <option value="C" ${knowledge.temp_unit === 'C' ? 'selected' : ''}>Celsius (°C)</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm font-medium text-gray-400 mb-2">Distance Unit</label>
                            <select id="knowledge-distance-unit"
                                    class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500 transition-colors">
                                <option value="mi" ${knowledge.distance_unit === 'mi' ? 'selected' : ''}>Miles</option>
                                <option value="km" ${knowledge.distance_unit === 'km' ? 'selected' : ''}>Kilometers</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm font-medium text-gray-400 mb-2">Date Format</label>
                            <select id="knowledge-date-format"
                                    class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500 transition-colors">
                                <option value="MM/DD/YYYY" ${knowledge.date_format === 'MM/DD/YYYY' ? 'selected' : ''}>MM/DD/YYYY</option>
                                <option value="DD/MM/YYYY" ${knowledge.date_format === 'DD/MM/YYYY' ? 'selected' : ''}>DD/MM/YYYY</option>
                                <option value="YYYY-MM-DD" ${knowledge.date_format === 'YYYY-MM-DD' ? 'selected' : ''}>YYYY-MM-DD</option>
                            </select>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Save Button -->
            <div class="flex justify-end mt-6">
                <button onclick="Athena.pages.MemoryContext.saveBaseKnowledge()"
                        class="px-6 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition-colors flex items-center gap-2">
                    <i data-lucide="save" class="w-4 h-4"></i>
                    Save Changes
                </button>
            </div>
        `;

        // Start time update
        updateCurrentTime();
        if (window._memoryContextTimeInterval) {
            clearInterval(window._memoryContextTimeInterval);
        }
        window._memoryContextTimeInterval = setInterval(updateCurrentTime, 1000);
    }

    /**
     * Update current time display.
     */
    function updateCurrentTime() {
        const el = document.getElementById('current-time');
        if (el) {
            const timezone = document.getElementById('knowledge-timezone')?.value || 'America/New_York';
            try {
                el.textContent = new Date().toLocaleTimeString('en-US', { timeZone: timezone });
            } catch {
                el.textContent = new Date().toLocaleTimeString();
            }
        }
    }

    /**
     * Add a new household memory.
     */
    function addHouseholdMemory() {
        if (!Athena.components.Drawer) {
            alert('Drawer component not available');
            return;
        }

        Athena.components.Drawer.open({
            title: 'Add Household Memory',
            content: `
                <div class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Summary</label>
                        <input type="text" id="memory-summary" placeholder="e.g., Pet Name"
                               class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Content</label>
                        <textarea id="memory-content" rows="4" placeholder="e.g., Our dog is named Max, a golden retriever"
                                  class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500"></textarea>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Category</label>
                        <select id="memory-category"
                                class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                            <option value="fact">Fact</option>
                            <option value="preference">Preference</option>
                            <option value="context">Context</option>
                        </select>
                    </div>
                </div>
            `,
            saveText: 'Add Memory',
            onSave: async () => {
                const summary = document.getElementById('memory-summary').value.trim();
                const content = document.getElementById('memory-content').value.trim();
                const category = document.getElementById('memory-category').value;

                if (!content) {
                    if (Athena.components.Toast) {
                        Athena.components.Toast.warning('Content is required');
                    }
                    throw new Error('Validation failed');
                }

                await Athena.api('/api/memories', {
                    method: 'POST',
                    body: JSON.stringify({
                        content,
                        summary: summary || null,
                        category,
                        scope: 'owner'
                    })
                });

                if (Athena.components.Toast) {
                    Athena.components.Toast.success('Memory added');
                }
                loadTab('household');
            }
        });
    }

    /**
     * Edit a household memory.
     */
    function editMemory(id) {
        const memory = state.data.memories?.find(m => m.id === id);
        if (!memory || !Athena.components.Drawer) return;

        Athena.components.Drawer.open({
            title: 'Edit Memory',
            content: `
                <div class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Summary</label>
                        <input type="text" id="memory-summary" value="${escapeHtml(memory.summary || '')}"
                               class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Content</label>
                        <textarea id="memory-content" rows="4"
                                  class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">${escapeHtml(memory.content || '')}</textarea>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Category</label>
                        <select id="memory-category"
                                class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                            <option value="fact" ${memory.category === 'fact' ? 'selected' : ''}>Fact</option>
                            <option value="preference" ${memory.category === 'preference' ? 'selected' : ''}>Preference</option>
                            <option value="context" ${memory.category === 'context' ? 'selected' : ''}>Context</option>
                        </select>
                    </div>
                </div>
            `,
            saveText: 'Save Changes',
            onSave: async () => {
                const summary = document.getElementById('memory-summary').value.trim();
                const content = document.getElementById('memory-content').value.trim();
                const category = document.getElementById('memory-category').value;

                if (!content) {
                    if (Athena.components.Toast) {
                        Athena.components.Toast.warning('Content is required');
                    }
                    throw new Error('Validation failed');
                }

                await Athena.api(`/api/memories/${id}`, {
                    method: 'PUT',
                    body: JSON.stringify({
                        content,
                        summary: summary || null,
                        category
                    })
                });

                if (Athena.components.Toast) {
                    Athena.components.Toast.success('Memory updated');
                }
                loadTab('household');
            }
        });
    }

    /**
     * Delete a household memory.
     */
    async function deleteMemory(id) {
        if (!confirm('Are you sure you want to delete this memory?')) return;

        try {
            await Athena.api(`/api/memories/${id}`, { method: 'DELETE' });
            if (Athena.components.Toast) {
                Athena.components.Toast.success('Memory deleted');
            }
            loadTab('household');
        } catch (error) {
            console.error('[MemoryContext] Delete memory failed:', error);
            if (Athena.components.Toast) {
                Athena.components.Toast.error('Failed to delete memory');
            }
        }
    }

    /**
     * Add a new guest.
     */
    function addGuest() {
        if (!Athena.components.Drawer) {
            alert('Drawer component not available');
            return;
        }

        Athena.components.Drawer.open({
            title: 'Add Guest',
            content: `
                <div class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Name</label>
                        <input type="text" id="guest-name" placeholder="Guest name"
                               class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Phone Number (optional)</label>
                        <input type="tel" id="guest-phone" placeholder="+1 (555) 123-4567"
                               class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Email (optional)</label>
                        <input type="email" id="guest-email" placeholder="guest@example.com"
                               class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Notes (optional)</label>
                        <textarea id="guest-notes" rows="3" placeholder="Any notes about this guest"
                                  class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500"></textarea>
                    </div>
                    <div class="flex items-center gap-2">
                        <input type="checkbox" id="guest-active" checked
                               class="w-4 h-4 rounded border-dark-border bg-dark-bg text-blue-600 focus:ring-blue-500">
                        <label for="guest-active" class="text-sm text-gray-400">Active</label>
                    </div>
                </div>
            `,
            saveText: 'Add Guest',
            onSave: async () => {
                const name = document.getElementById('guest-name').value.trim();
                if (!name) {
                    if (Athena.components.Toast) {
                        Athena.components.Toast.warning('Name is required');
                    }
                    throw new Error('Validation failed');
                }

                await Athena.api('/api/guests', {
                    method: 'POST',
                    body: JSON.stringify({
                        name,
                        phone_number: document.getElementById('guest-phone').value.trim() || null,
                        email: document.getElementById('guest-email').value.trim() || null,
                        notes: document.getElementById('guest-notes').value.trim() || null,
                        is_active: document.getElementById('guest-active').checked
                    })
                });

                if (Athena.components.Toast) {
                    Athena.components.Toast.success('Guest added');
                }
                loadTab('guests');
            }
        });
    }

    /**
     * Edit a guest.
     */
    function editGuest(id) {
        const guest = state.data.guests?.find(g => g.id === id);
        if (!guest || !Athena.components.Drawer) return;

        Athena.components.Drawer.open({
            title: 'Edit Guest',
            content: `
                <div class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Name</label>
                        <input type="text" id="guest-name" value="${escapeHtml(guest.name)}"
                               class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Phone Number (optional)</label>
                        <input type="tel" id="guest-phone" value="${escapeHtml(guest.phone_number || '')}"
                               class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Email (optional)</label>
                        <input type="email" id="guest-email" value="${escapeHtml(guest.email || '')}"
                               class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-400 mb-2">Notes (optional)</label>
                        <textarea id="guest-notes" rows="3"
                                  class="w-full px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500">${escapeHtml(guest.notes || '')}</textarea>
                    </div>
                    <div class="flex items-center gap-2">
                        <input type="checkbox" id="guest-active" ${guest.is_active ? 'checked' : ''}
                               class="w-4 h-4 rounded border-dark-border bg-dark-bg text-blue-600 focus:ring-blue-500">
                        <label for="guest-active" class="text-sm text-gray-400">Active</label>
                    </div>
                </div>
            `,
            saveText: 'Save Changes',
            onSave: async () => {
                const name = document.getElementById('guest-name').value.trim();
                if (!name) {
                    if (Athena.components.Toast) {
                        Athena.components.Toast.warning('Name is required');
                    }
                    throw new Error('Validation failed');
                }

                await Athena.api(`/api/guests/${id}`, {
                    method: 'PUT',
                    body: JSON.stringify({
                        name,
                        phone_number: document.getElementById('guest-phone').value.trim() || null,
                        email: document.getElementById('guest-email').value.trim() || null,
                        notes: document.getElementById('guest-notes').value.trim() || null,
                        is_active: document.getElementById('guest-active').checked
                    })
                });

                if (Athena.components.Toast) {
                    Athena.components.Toast.success('Guest updated');
                }
                loadTab('guests');
            }
        });
    }

    /**
     * Delete a guest.
     */
    async function deleteGuest(id) {
        if (!confirm('Are you sure you want to remove this guest?')) return;

        try {
            await Athena.api(`/api/guests/${id}`, { method: 'DELETE' });
            if (Athena.components.Toast) {
                Athena.components.Toast.success('Guest removed');
            }
            loadTab('guests');
        } catch (error) {
            console.error('[MemoryContext] Delete guest failed:', error);
            if (Athena.components.Toast) {
                Athena.components.Toast.error('Failed to remove guest');
            }
        }
    }

    /**
     * Save base knowledge settings.
     */
    async function saveBaseKnowledge() {
        const data = {
            city: document.getElementById('knowledge-city')?.value.trim() || '',
            state: document.getElementById('knowledge-state')?.value.trim() || '',
            latitude: document.getElementById('knowledge-latitude')?.value.trim() || '',
            longitude: document.getElementById('knowledge-longitude')?.value.trim() || '',
            timezone: document.getElementById('knowledge-timezone')?.value || 'America/New_York',
            temp_unit: document.getElementById('knowledge-temp-unit')?.value || 'F',
            distance_unit: document.getElementById('knowledge-distance-unit')?.value || 'mi',
            date_format: document.getElementById('knowledge-date-format')?.value || 'MM/DD/YYYY'
        };

        try {
            await Athena.api('/api/internal/config/base-knowledge', {
                method: 'PUT',
                body: JSON.stringify(data)
            });

            if (Athena.components.Toast) {
                Athena.components.Toast.success('Base knowledge updated');
            }
        } catch (error) {
            console.error('[MemoryContext] Save base knowledge failed:', error);
            if (Athena.components.Toast) {
                Athena.components.Toast.error('Failed to save base knowledge');
            }
        }
    }

    /**
     * Format date for display.
     */
    function formatDate(timestamp) {
        if (!timestamp) return 'Unknown';
        try {
            return new Date(timestamp).toLocaleDateString();
        } catch {
            return 'Unknown';
        }
    }

    /**
     * Escape HTML.
     */
    function escapeHtml(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // Register page controller
    Athena.pages.MemoryContext = {
        init,
        destroy,
        switchTab,
        loadTab,
        addHouseholdMemory,
        editMemory,
        deleteMemory,
        addGuest,
        editGuest,
        deleteGuest,
        saveBaseKnowledge
    };

    // Also export as global for compatibility
    window.MemoryContext = Athena.pages.MemoryContext;

})(window.Athena);
