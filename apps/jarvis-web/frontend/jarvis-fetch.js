/*
 * jarvisFetch: the one way this page talks to jarvis-web, and the one place
 * that notices the session has ended.
 *
 * - Same-origin requests carry X-Jarvis-Request: 1. jarvis-web refuses a
 *   mutating request without it (403 reload_required), and a cross-origin
 *   page can't send it without a preflight that jarvis-web never grants.
 * - redirect: 'manual', so a sign-in redirect from an auth proxy surfaces
 *   as an opaque redirect instead of silently loading a login page as data.
 * - An opaque redirect, or any 401 from jarvis-web (the body is never
 *   read), means the session ended: sessionEnded() fires once and every
 *   listener (banner, pollers, reconnect loops, LiveKit) stops.
 * - A network TypeError or an unexpected WebSocket close triggers one
 *   probe of /api/welcome; only a redirect or 401 there counts as signed
 *   out. Anything else is ordinary offline handling.
 * - A 403 reload_required means this tab predates the server: offer a
 *   reload.
 */
(function () {
    'use strict';

    const state = { signedOut: false, listeners: [], probing: null };

    function isSameOrigin(url) {
        try {
            return new URL(url, window.location.href).origin === window.location.origin;
        } catch (e) {
            return false;
        }
    }

    function sessionEnded() {
        if (state.signedOut) return;
        state.signedOut = true;
        for (const listener of state.listeners) {
            try {
                listener();
            } catch (e) {
                console.error('[jarvisFetch] session-ended listener failed:', e);
            }
        }
    }

    function onSessionEnded(listener) {
        state.listeners.push(listener);
        if (state.signedOut) listener();
    }

    function isSignedOut() {
        return state.signedOut;
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

    function endsSession(response) {
        return response.type === 'opaqueredirect' || response.status === 401;
    }

    async function rawFetch(url, opts) {
        const options = Object.assign({}, opts || {});
        options.redirect = 'manual';
        if (isSameOrigin(url)) {
            const headers = new Headers(options.headers || {});
            headers.set('X-Jarvis-Request', '1');
            options.headers = headers;
            options.credentials = options.credentials || 'same-origin';
        }
        return fetch(url, options);
    }

    // One probe at a time: is the network down, or did the session end?
    function probeSession() {
        if (state.signedOut) return Promise.resolve(true);
        if (!state.probing) {
            state.probing = rawFetch('/api/welcome')
                .then((response) => {
                    if (endsSession(response)) sessionEnded();
                    return state.signedOut;
                })
                .catch(() => false)
                .finally(() => { state.probing = null; });
        }
        return state.probing;
    }

    async function jarvisFetch(url, opts) {
        let response;
        try {
            response = await rawFetch(url, opts);
        } catch (error) {
            if (error instanceof TypeError && isSameOrigin(url)) await probeSession();
            throw error;
        }
        if (isSameOrigin(url)) {
            if (endsSession(response)) {
                sessionEnded();
                return response;
            }
            if (response.status === 403) {
                try {
                    const body = await response.clone().json();
                    if (body && body.detail === 'reload_required') showReloadNotice();
                } catch (e) {
                    /* not JSON: nothing to add */
                }
            }
        }
        return response;
    }

    window.jarvisFetch = jarvisFetch;
    window.jarvisSession = { sessionEnded, onSessionEnded, isSignedOut, probe: probeSession };
})();
