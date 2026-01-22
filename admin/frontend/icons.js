/**
 * Icon System for Athena Admin
 * Provides Lucide icon utilities and replaces emoji icons
 *
 * Part of Phase 2: Icon System Replacement
 */

(function() {
    /**
     * Icon mapping from semantic names to Lucide icon names
     */
    const ICON_MAP = {
        // Dashboard & Navigation
        'dashboard': 'layout-dashboard',
        'home': 'home',
        'settings': 'settings',
        'config': 'settings-2',

        // Configuration
        'policies': 'file-text',
        'secrets': 'lock',
        'api-keys': 'key',
        'features': 'sliders-horizontal',
        'flags': 'flag',

        // AI & LLM
        'ai': 'brain',
        'llm': 'cpu',
        'model': 'box',
        'models': 'boxes',
        'cloud': 'cloud',
        'cloud-provider': 'cloud-cog',
        'download': 'download',

        // Voice & Audio
        'voice': 'mic',
        'microphone': 'mic',
        'speaker': 'volume-2',
        'audio': 'volume-2',
        'music': 'music',
        'play': 'play',
        'pause': 'pause',
        'stop': 'square',

        // Devices & Smart Home
        'device': 'monitor',
        'devices': 'monitor-smartphone',
        'zone': 'map-pin',
        'zones': 'map',
        'room': 'home',
        'light': 'lightbulb',
        'tv': 'tv',
        'thermostat': 'thermometer',

        // Users & Security
        'user': 'user',
        'users': 'users',
        'admin': 'user-cog',
        'security': 'shield',
        'auth': 'lock',
        'login': 'log-in',
        'logout': 'log-out',
        'audit': 'clipboard-list',

        // Data & Analytics
        'analytics': 'bar-chart-2',
        'metrics': 'activity',
        'chart': 'line-chart',
        'database': 'database',
        'table': 'table',

        // RAG & Knowledge
        'rag': 'brain-circuit',
        'knowledge': 'book-open',
        'search': 'search',
        'calendar': 'calendar',

        // Alerts & Notifications
        'alerts': 'bell',
        'warning': 'alert-triangle',
        'error': 'alert-circle',
        'info': 'info',
        'success': 'check-circle',

        // Actions
        'add': 'plus',
        'create': 'plus-circle',
        'edit': 'pencil',
        'delete': 'trash-2',
        'refresh': 'refresh-cw',
        'save': 'save',
        'cancel': 'x',
        'close': 'x',
        'expand': 'chevron-down',
        'collapse': 'chevron-up',
        'menu': 'menu',
        'more': 'more-horizontal',

        // Status
        'online': 'wifi',
        'offline': 'wifi-off',
        'loading': 'loader',
        'check': 'check',
        'healthy': 'heart-pulse',

        // System
        'service': 'server',
        'services': 'server',
        'system': 'cpu',
        'logs': 'file-text',
        'terminal': 'terminal',
        'code': 'code',
        'api': 'plug',
        'webhook': 'webhook',

        // Communication
        'sms': 'message-square',
        'email': 'mail',
        'notification': 'bell',
        'chat': 'message-circle',

        // Tools
        'tools': 'wrench',
        'tool': 'wrench',
        'scraper': 'globe',
        'directions': 'navigation',
        'routing': 'git-branch',

        // Guest
        'guest': 'user-plus',
        'guests': 'users-round',

        // Misc
        'rocket': 'rocket',
        'star': 'star',
        'heart': 'heart',
        'link': 'link',
        'external': 'external-link',
        'copy': 'copy',
        'clipboard': 'clipboard',
        'file': 'file',
        'folder': 'folder',
        'image': 'image',
        'video': 'video',
        'list': 'list',
        'grid': 'grid',
        'filter': 'filter',
        'sort': 'arrow-up-down',
        'time': 'clock',
        'date': 'calendar',
        'location': 'map-pin',
        'phone': 'phone',
        'help': 'help-circle',
        'question': 'help-circle'
    };

    /**
     * Get a Lucide icon element
     *
     * @param {string} name - Icon name (from ICON_MAP or Lucide name)
     * @param {Object} options - Options { class, size }
     * @returns {string} - HTML string for icon
     */
    function getIcon(name, options = {}) {
        const lucideName = ICON_MAP[name] || name;
        const className = options.class || '';
        const size = options.size || 20;

        return `<i data-lucide="${lucideName}" class="${className}" style="width: ${size}px; height: ${size}px;"></i>`;
    }

    /**
     * Initialize all Lucide icons on the page
     * Call this after dynamic content is added
     */
    function initIcons() {
        if (typeof lucide !== 'undefined') {
            lucide.createIcons();
        }
    }

    /**
     * Replace emoji with Lucide icon in text
     *
     * @param {string} text - Text containing emoji
     * @param {Object} emojiMap - Map of emoji to icon names
     * @returns {string} - Text with icons
     */
    function replaceEmoji(text, emojiMap = {}) {
        const defaultEmojiMap = {
            '📊': 'dashboard',
            '⚙️': 'settings',
            '📋': 'policies',
            '🔐': 'secrets',
            '🔑': 'api-keys',
            '🎛️': 'features',
            '🤖': 'ai',
            '💻': 'llm',
            '📦': 'model',
            '☁️': 'cloud',
            '⬇️': 'download',
            '🎤': 'voice',
            '🔊': 'speaker',
            '🎵': 'music',
            '📱': 'device',
            '🏠': 'home',
            '💡': 'light',
            '📺': 'tv',
            '👤': 'user',
            '👥': 'users',
            '🛡️': 'security',
            '📈': 'analytics',
            '📉': 'chart',
            '🗄️': 'database',
            '🧠': 'rag',
            '📚': 'knowledge',
            '🔍': 'search',
            '📅': 'calendar',
            '🔔': 'alerts',
            '⚠️': 'warning',
            '❌': 'error',
            'ℹ️': 'info',
            '✅': 'success',
            '➕': 'add',
            '✏️': 'edit',
            '🗑️': 'delete',
            '🔄': 'refresh',
            '💾': 'save',
            '🖥️': 'service',
            '📝': 'logs',
            '💬': 'sms',
            '📧': 'email',
            '🔧': 'tools',
            '🌐': 'scraper',
            '🧭': 'directions',
            '👋': 'guest',
            '🚀': 'rocket',
            '⭐': 'star',
            '❤️': 'heart',
            '🔗': 'link',
            '📁': 'folder',
            '🖼️': 'image',
            '⏰': 'time',
            '📍': 'location',
            '📞': 'phone',
            '❓': 'help'
        };

        const combinedMap = { ...defaultEmojiMap, ...emojiMap };

        let result = text;
        for (const [emoji, iconName] of Object.entries(combinedMap)) {
            result = result.replace(new RegExp(emoji, 'g'), getIcon(iconName));
        }

        return result;
    }

    /**
     * Get status indicator icon
     *
     * @param {string} status - Status type ('healthy', 'warning', 'error', 'unknown')
     * @param {number} size - Icon size
     * @returns {string} - HTML string for status icon
     */
    function getStatusIcon(status, size = 16) {
        const icons = {
            'healthy': { name: 'check-circle', color: 'text-green-400' },
            'success': { name: 'check-circle', color: 'text-green-400' },
            'warning': { name: 'alert-triangle', color: 'text-yellow-400' },
            'degraded': { name: 'alert-triangle', color: 'text-yellow-400' },
            'error': { name: 'alert-circle', color: 'text-red-400' },
            'critical': { name: 'alert-octagon', color: 'text-red-400' },
            'unknown': { name: 'help-circle', color: 'text-gray-400' },
            'loading': { name: 'loader', color: 'text-blue-400 animate-spin' }
        };

        const config = icons[status] || icons['unknown'];
        return getIcon(config.name, { class: config.color, size });
    }

    // Expose on window
    window.Icons = {
        ICON_MAP,
        getIcon,
        initIcons,
        replaceEmoji,
        getStatusIcon
    };

    // Also expose commonly used functions directly
    window.getIcon = getIcon;
    window.initIcons = initIcons;
})();
