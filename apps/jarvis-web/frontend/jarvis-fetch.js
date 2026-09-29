/*
 * jarvisFetch: the one way this page talks to jarvis-web.
 *
 * - Same-origin requests carry X-Jarvis-Request: 1. jarvis-web refuses a
 *   mutating request without it (403 reload_required), and a cross-origin
 *   page can't send it without a preflight that jarvis-web never grants.
 * - redirect: 'manual', so a sign-in redirect from an auth proxy surfaces
 *   as an opaque redirect instead of silently loading a login page as data.
 * - A 403 reload_required means this tab predates the server: offer a
 *   reload.
 */
(function () {
    'use strict';

    function isSameOrigin(url) {
        try {
            return new URL(url, window.location.href).origin === window.location.origin;
        } catch (e) {
            return false;
        }
    }

    function showReloadNotice() {
        if (document.getElementById('jarvis-reload-notice')) return;
        const notice = document.createElement('div');
        notice.id = 'jarvis-reload-notice';
        notice.className = 'jarvis-notice';
        notice.setAttribute('role', 'alert');
        const text = document.createElement('span');
        text.textContent = 'Jarvis was updated. Reload the page.';
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = 'Reload';
        button.addEventListener('click', () => window.location.reload());
        notice.appendChild(text);
        notice.appendChild(button);
        (document.querySelector('main') || document.body).prepend(notice);
    }

    async function jarvisFetch(url, opts) {
        const options = Object.assign({}, opts || {});
        options.redirect = 'manual';
        if (isSameOrigin(url)) {
            const headers = new Headers(options.headers || {});
            headers.set('X-Jarvis-Request', '1');
            options.headers = headers;
            options.credentials = options.credentials || 'same-origin';
        }
        const response = await fetch(url, options);
        if (response.status === 403) {
            try {
                const body = await response.clone().json();
                if (body && body.detail === 'reload_required') showReloadNotice();
            } catch (e) {
                /* not JSON: nothing to add */
            }
        }
        return response;
    }

    window.jarvisFetch = jarvisFetch;
})();
