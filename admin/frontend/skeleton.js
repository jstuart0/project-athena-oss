/**
 * Loading Skeleton System for Athena Admin
 * Provides animated placeholder content during loading states
 *
 * Part of Phase 6: Loading States & Skeletons
 */

(function() {
    const Skeleton = {
        /**
         * Generate a basic skeleton line
         *
         * @param {string} width - Width class (e.g., 'w-full', 'w-3/4')
         * @param {string} height - Height class (e.g., 'h-4', 'h-6')
         * @returns {string} - HTML string
         */
        line(width = 'w-full', height = 'h-4') {
            return `<div class="skeleton ${width} ${height} rounded"></div>`;
        },

        /**
         * Generate a skeleton circle (avatar placeholder)
         *
         * @param {string} size - Size class (e.g., 'w-10 h-10')
         * @returns {string} - HTML string
         */
        circle(size = 'w-10 h-10') {
            return `<div class="skeleton ${size} rounded-full"></div>`;
        },

        /**
         * Generate a skeleton card
         *
         * @returns {string} - HTML string
         */
        card() {
            return `
                <div class="skeleton-card bg-dark-card border border-dark-border rounded-lg p-4" aria-busy="true" aria-label="Loading card">
                    <div class="skeleton h-4 w-2/3 rounded mb-3"></div>
                    <div class="skeleton h-3 w-full rounded mb-2"></div>
                    <div class="skeleton h-3 w-5/6 rounded mb-2"></div>
                    <div class="skeleton h-3 w-4/5 rounded"></div>
                </div>
            `;
        },

        /**
         * Generate a skeleton stat card
         *
         * @returns {string} - HTML string
         */
        statCard() {
            return `
                <div class="skeleton-stat bg-dark-card border border-dark-border rounded-lg p-6" aria-busy="true" aria-label="Loading statistic">
                    <div class="skeleton h-3 w-1/3 rounded mb-3"></div>
                    <div class="skeleton h-8 w-1/2 rounded mb-2"></div>
                    <div class="skeleton h-3 w-2/3 rounded"></div>
                </div>
            `;
        },

        /**
         * Generate a skeleton table row
         *
         * @param {number} cols - Number of columns
         * @returns {string} - HTML string
         */
        tableRow(cols = 5) {
            const cells = Array(cols).fill(0).map((_, i) => {
                const width = i === 0 ? 'w-1/4' : i === cols - 1 ? 'w-20' : 'w-full';
                return `<td class="px-4 py-3"><div class="skeleton h-4 ${width} rounded"></div></td>`;
            }).join('');

            return `<tr class="border-b border-dark-border">${cells}</tr>`;
        },

        /**
         * Generate a skeleton table
         *
         * @param {number} rows - Number of rows
         * @param {number} cols - Number of columns
         * @returns {string} - HTML string
         */
        table(rows = 5, cols = 5) {
            const headerCells = Array(cols).fill(0).map(() =>
                `<th class="px-4 py-3 text-left"><div class="skeleton h-3 w-20 rounded"></div></th>`
            ).join('');

            const bodyRows = Array(rows).fill(0).map(() => this.tableRow(cols)).join('');

            return `
                <div class="skeleton-table bg-dark-card border border-dark-border rounded-lg overflow-hidden" aria-busy="true" aria-label="Loading table">
                    <table class="w-full">
                        <thead class="bg-dark-bg">
                            <tr>${headerCells}</tr>
                        </thead>
                        <tbody>${bodyRows}</tbody>
                    </table>
                </div>
            `;
        },

        /**
         * Generate a skeleton list item
         *
         * @param {boolean} withAvatar - Include avatar placeholder
         * @returns {string} - HTML string
         */
        listItem(withAvatar = true) {
            const avatar = withAvatar ? `<div class="skeleton w-10 h-10 rounded-full flex-shrink-0"></div>` : '';

            return `
                <div class="flex items-center gap-4 py-3">
                    ${avatar}
                    <div class="flex-1">
                        <div class="skeleton h-4 w-1/3 rounded mb-2"></div>
                        <div class="skeleton h-3 w-2/3 rounded"></div>
                    </div>
                </div>
            `;
        },

        /**
         * Generate a skeleton list
         *
         * @param {number} count - Number of items
         * @param {boolean} withAvatar - Include avatar placeholders
         * @returns {string} - HTML string
         */
        list(count = 5, withAvatar = true) {
            const items = Array(count).fill(0).map(() => this.listItem(withAvatar)).join('');

            return `
                <div class="skeleton-list divide-y divide-dark-border" aria-busy="true" aria-label="Loading list">
                    ${items}
                </div>
            `;
        },

        /**
         * Generate a skeleton form
         *
         * @param {number} fields - Number of form fields
         * @returns {string} - HTML string
         */
        form(fields = 4) {
            const formFields = Array(fields).fill(0).map(() => `
                <div class="mb-4">
                    <div class="skeleton h-3 w-1/4 rounded mb-2"></div>
                    <div class="skeleton h-10 w-full rounded"></div>
                </div>
            `).join('');

            return `
                <div class="skeleton-form" aria-busy="true" aria-label="Loading form">
                    ${formFields}
                    <div class="flex gap-3 mt-6">
                        <div class="skeleton h-10 w-24 rounded"></div>
                        <div class="skeleton h-10 w-24 rounded"></div>
                    </div>
                </div>
            `;
        },

        /**
         * Generate a full page skeleton (dashboard layout)
         *
         * @returns {string} - HTML string
         */
        page() {
            return `
                <div class="skeleton-page space-y-6" aria-busy="true" aria-label="Loading page">
                    <!-- Page Header -->
                    <div class="flex items-center justify-between mb-6">
                        <div class="skeleton h-8 w-48 rounded"></div>
                        <div class="flex gap-3">
                            <div class="skeleton h-10 w-24 rounded"></div>
                            <div class="skeleton h-10 w-32 rounded"></div>
                        </div>
                    </div>

                    <!-- Stats Grid -->
                    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                        ${this.statCard()}
                        ${this.statCard()}
                        ${this.statCard()}
                        ${this.statCard()}
                    </div>

                    <!-- Content Table -->
                    ${this.table(5, 5)}
                </div>
            `;
        },

        /**
         * Generate a dashboard skeleton
         *
         * @returns {string} - HTML string
         */
        dashboard() {
            return `
                <div class="skeleton-dashboard space-y-6" aria-busy="true" aria-label="Loading dashboard">
                    <!-- Stats Grid -->
                    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                        ${this.statCard()}
                        ${this.statCard()}
                        ${this.statCard()}
                        ${this.statCard()}
                    </div>

                    <!-- Two Column Layout -->
                    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                        <div>
                            <div class="skeleton h-6 w-32 rounded mb-4"></div>
                            ${this.list(4)}
                        </div>
                        <div>
                            <div class="skeleton h-6 w-40 rounded mb-4"></div>
                            ${this.card()}
                            <div class="mt-4">${this.card()}</div>
                        </div>
                    </div>
                </div>
            `;
        },

        /**
         * Show skeleton in a container
         *
         * @param {string} containerId - Container element ID
         * @param {string} type - Skeleton type ('page', 'table', 'list', 'form', 'dashboard')
         */
        show(containerId, type = 'page') {
            const container = document.getElementById(containerId);
            if (!container) return;

            let html;
            switch (type) {
                case 'table': html = this.table(); break;
                case 'list': html = this.list(); break;
                case 'form': html = this.form(); break;
                case 'dashboard': html = this.dashboard(); break;
                default: html = this.page();
            }

            container.innerHTML = html;
            container.setAttribute('aria-busy', 'true');
        },

        /**
         * Hide skeleton (remove aria-busy)
         *
         * @param {string} containerId - Container element ID
         */
        hide(containerId) {
            const container = document.getElementById(containerId);
            if (container) {
                container.removeAttribute('aria-busy');
            }
        }
    };

    // Expose on window
    window.Skeleton = Skeleton;
})();
