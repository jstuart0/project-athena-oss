/**
 * Athena Toast - Notification System
 *
 * Provides toast notifications for user feedback.
 * Supports success, error, warning, and info types.
 */
(function(Athena) {
    'use strict';

    if (!Athena) {
        console.error('[AthenaToast] Athena namespace not found');
        return;
    }

    // Toast container element
    let container = null;

    // Active toasts
    const activeToasts = [];

    // Configuration
    const config = {
        position: 'top-right', // top-right, top-left, bottom-right, bottom-left, top-center, bottom-center
        duration: 4000,
        maxVisible: 5,
        gap: 12
    };

    // Toast type configurations
    const TYPES = {
        success: {
            icon: 'check-circle',
            bgClass: 'bg-green-500/10',
            borderClass: 'border-green-500/30',
            iconClass: 'text-green-400',
            titleClass: 'text-green-400'
        },
        error: {
            icon: 'x-circle',
            bgClass: 'bg-red-500/10',
            borderClass: 'border-red-500/30',
            iconClass: 'text-red-400',
            titleClass: 'text-red-400'
        },
        warning: {
            icon: 'alert-triangle',
            bgClass: 'bg-yellow-500/10',
            borderClass: 'border-yellow-500/30',
            iconClass: 'text-yellow-400',
            titleClass: 'text-yellow-400'
        },
        info: {
            icon: 'info',
            bgClass: 'bg-blue-500/10',
            borderClass: 'border-blue-500/30',
            iconClass: 'text-blue-400',
            titleClass: 'text-blue-400'
        }
    };

    /**
     * Initialize the toast system.
     */
    function init() {
        if (container) return;

        container = document.createElement('div');
        container.id = 'athena-toast-container';
        container.className = getPositionClasses();
        container.setAttribute('aria-live', 'polite');
        container.setAttribute('aria-label', 'Notifications');
        document.body.appendChild(container);

        console.log('[AthenaToast] Initialized');
    }

    /**
     * Get CSS classes for container position.
     */
    function getPositionClasses() {
        const positions = {
            'top-right': 'fixed top-4 right-4 z-[var(--z-toast,120)]',
            'top-left': 'fixed top-4 left-4 z-[var(--z-toast,120)]',
            'top-center': 'fixed top-4 left-1/2 -translate-x-1/2 z-[var(--z-toast,120)]',
            'bottom-right': 'fixed bottom-4 right-4 z-[var(--z-toast,120)]',
            'bottom-left': 'fixed bottom-4 left-4 z-[var(--z-toast,120)]',
            'bottom-center': 'fixed bottom-4 left-1/2 -translate-x-1/2 z-[var(--z-toast,120)]'
        };
        return positions[config.position] + ' flex flex-col gap-3 pointer-events-none';
    }

    /**
     * Show a toast notification.
     * @param {Object} options - Toast options
     * @param {string} options.message - Toast message
     * @param {string} [options.type='info'] - Toast type (success, error, warning, info)
     * @param {string} [options.title] - Optional title
     * @param {number} [options.duration] - Duration in ms (0 for persistent)
     * @param {Function} [options.action] - Optional action button callback
     * @param {string} [options.actionText] - Action button text
     * @returns {Object} Toast control object with dismiss method
     */
    function show(options) {
        init();

        const {
            message,
            type = 'info',
            title = null,
            duration = config.duration,
            action = null,
            actionText = 'Undo'
        } = typeof options === 'string' ? { message: options } : options;

        // Remove oldest toast if at max
        if (activeToasts.length >= config.maxVisible) {
            dismiss(activeToasts[0]);
        }

        const typeConfig = TYPES[type] || TYPES.info;
        const toastId = 'toast-' + Date.now();

        // Create toast element
        const toast = document.createElement('div');
        toast.id = toastId;
        toast.className = `
            pointer-events-auto
            flex items-start gap-3
            p-4 min-w-[320px] max-w-[420px]
            ${typeConfig.bgClass} ${typeConfig.borderClass}
            border rounded-xl
            shadow-lg backdrop-blur-sm
            transform transition-all duration-300 ease-out
            translate-x-full opacity-0
        `;
        toast.setAttribute('role', 'alert');

        // Build toast HTML
        toast.innerHTML = `
            <div class="flex-shrink-0 mt-0.5">
                <i data-lucide="${typeConfig.icon}" class="w-5 h-5 ${typeConfig.iconClass}"></i>
            </div>
            <div class="flex-1 min-w-0">
                ${title ? `<p class="font-medium ${typeConfig.titleClass} mb-1">${escapeHtml(title)}</p>` : ''}
                <p class="text-sm text-gray-300">${escapeHtml(message)}</p>
            </div>
            ${action ? `
                <button class="action-btn flex-shrink-0 text-sm font-medium text-blue-400 hover:text-blue-300 transition-colors">
                    ${escapeHtml(actionText)}
                </button>
            ` : ''}
            <button class="close-btn flex-shrink-0 text-gray-500 hover:text-gray-300 transition-colors">
                <i data-lucide="x" class="w-4 h-4"></i>
            </button>
        `;

        // Add to container
        container.appendChild(toast);
        activeToasts.push(toast);

        // Initialize icons
        if (window.lucide) {
            lucide.createIcons({ nodes: [toast] });
        }

        // Bind events
        const closeBtn = toast.querySelector('.close-btn');
        closeBtn?.addEventListener('click', () => dismiss(toast));

        const actionBtn = toast.querySelector('.action-btn');
        if (actionBtn && action) {
            actionBtn.addEventListener('click', () => {
                action();
                dismiss(toast);
            });
        }

        // Animate in
        requestAnimationFrame(() => {
            toast.classList.remove('translate-x-full', 'opacity-0');
            toast.classList.add('translate-x-0', 'opacity-100');
        });

        // Auto dismiss
        let timeoutId = null;
        if (duration > 0) {
            timeoutId = setTimeout(() => dismiss(toast), duration);
        }

        // Return control object
        return {
            dismiss: () => dismiss(toast),
            element: toast,
            id: toastId
        };
    }

    /**
     * Dismiss a toast.
     * @param {HTMLElement} toast - Toast element to dismiss
     */
    function dismiss(toast) {
        if (!toast || !toast.parentNode) return;

        // Animate out
        toast.classList.remove('translate-x-0', 'opacity-100');
        toast.classList.add('translate-x-full', 'opacity-0');

        // Remove from DOM after animation
        setTimeout(() => {
            toast.remove();
            const index = activeToasts.indexOf(toast);
            if (index > -1) activeToasts.splice(index, 1);
        }, 300);
    }

    /**
     * Dismiss all toasts.
     */
    function dismissAll() {
        [...activeToasts].forEach(dismiss);
    }

    /**
     * Escape HTML to prevent XSS.
     */
    function escapeHtml(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // Convenience methods
    const success = (message, options = {}) => show({ ...options, message, type: 'success' });
    const error = (message, options = {}) => show({ ...options, message, type: 'error' });
    const warning = (message, options = {}) => show({ ...options, message, type: 'warning' });
    const info = (message, options = {}) => show({ ...options, message, type: 'info' });

    // Export to Athena namespace
    Athena.components.Toast = {
        init,
        show,
        success,
        error,
        warning,
        info,
        dismiss,
        dismissAll,
        config
    };

    // Also export as global for compatibility
    window.AthenaToast = Athena.components.Toast;

})(window.Athena);
