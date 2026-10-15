/**
 * Household settings: editors for lists of objects (transit feeds, static
 * transit services and community event sources).
 *
 * Loads after household-settings.js and plugs into it: it registers the
 * 'object_list' field kind and defines the 'transit' and 'community_events'
 * cards. A list has the same three visible states as a list of text (not set
 * here, explicitly none, rows). Each row is a summary line with an Edit
 * disclosure; one row is open at a time.
 *
 * URL rules (the server is the authority; these only save a round trip):
 *   - A saved URL arrives masked (scheme and host) with <field>_masked: true.
 *     It is shown as read-only text with a Replace URL button. Unchanged, it is
 *     sent back as it came, with the flag. Replaced, the new full URL is sent
 *     with no flag. A masked value is never sent edited.
 *   - allow_private and allowed_private_hosts are server environment settings
 *     and are never read from or written to a row.
 *
 * Built with createElement/textContent only; no markup is assembled from data
 * and listeners use addEventListener.
 */
(function (global) {
    'use strict';

    const HS = global.HouseholdSettings;
    if (!HS || !HS.ui) {
        console.error('household-settings-lists.js must load after household-settings.js');
        return;
    }
    const { el, clear, setLine, errorLine, INPUT_CLASS, BUTTON_CLASS, LINK_BUTTON_CLASS, HIDDEN } = HS.ui;

    const SECONDARY_BUTTON_CLASS = 'min-h-[44px] px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
    const LABEL_CLASS = 'block text-sm font-medium text-gray-400 mb-1';
    const HELP_CLASS = 'text-xs text-gray-500 mt-1';
    const MASK = '•••';
    const MAX_STOPS = 500;
    const MAX_LINES = 20;
    const MIN_BYTES = 1048576;
    const MAX_BYTES = 104857600;
    const ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
    const TYPE_RE = /^[a-z_]{1,32}$/;
    const DECIMAL_RE = /^-?\d+(\.\d+)?$/;
    const MASKED_URL_COPY = "Paste the full URL, including its key. The saved key is hidden and can't be edited in place.";
    const PRIVATE_URL_COPY = "Use a public http or https address. Private-network and cluster addresses can only be set in the server's environment.";

    // --- messages: client checks and server 422 types share one table ----------------------------

    const MESSAGES = {
        blank: () => 'Enter a value.',
        string_too_short: () => 'Enter a value.',
        string_too_long: () => 'That is too long (100 characters at most).',
        bad_characters: () => "That has characters that aren't allowed.",
        url_invalid: () => 'Enter a full web address, like https://example.org/feed.zip.',
        url_scheme: () => 'Start the address with http:// or https://.',
        url_userinfo: () => 'Remove the username and password from the address.',
        url_fragment: () => 'Remove the # part of the address.',
        url_private_host: () => PRIVATE_URL_COPY,
        url_mask_token: () => MASKED_URL_COPY,
        url_masked_edit: () => MASKED_URL_COPY,
        duplicate_id: () => 'Another row already uses this id. Pick a different one.',
        bad_id: () => 'Use lowercase letters, numbers, - and _ (up to 64), starting with a letter or number.',
        bad_type: () => 'Use lowercase letters and _ only (up to 32).',
        bad_time: () => 'Use a time like 06:30.',
        hours_incomplete: () => 'Enter both a start and an end time, or clear both.',
        bounds_incomplete: () => 'Fill in all four, or clear all four.',
        bounds_order: () => 'South must be below North, and West below East.',
        bad_number: () => 'Enter a number, like 40.7128.',
        lat_range: () => 'Latitude is between -90 and 90.',
        lon_range: () => 'Longitude is between -180 and 180.',
        bad_link_path: () => 'Start with / and use no //, spaces or backslashes (200 characters at most).',
        lines_too_many: () => `At most ${MAX_LINES} lines.`,
        lines_too_long: () => 'Each line is at most 100 characters.',
        stops_required: () => 'Add at least one stop.',
        too_long: () => 'That list is too long.',
        literal_error: () => 'Pick one of the listed types.',
        out_of_range: f => f.rangeMessage || 'That number is out of range.',
    };
    const RANGE_TYPES = ['greater_than_equal', 'less_than_equal', 'int_parsing', 'int_type', 'int_from_float', 'float_parsing', 'float_type', 'finite_number'];

    function messageFor(type, desc) {
        if (RANGE_TYPES.includes(type)) type = 'out_of_range';
        const entry = MESSAGES[type];
        if (typeof entry === 'function') return entry(desc || {});
        return HS.messageFor(type, {});
    }

    // --- small helpers ---------------------------------------------------------------------------

    let serial = 0;
    const nextId = prefix => `${prefix}-${++serial}`;

    function describedBy(id, help) { return help ? `${id}-help ${id}-error` : `${id}-error`; }

    function errorMark(inputs, node) {
        return {
            show(text) {
                setLine(node, text);
                inputs.forEach(input => input.setAttribute('aria-invalid', text ? 'true' : 'false'));
            },
            has() { return inputs[0].getAttribute('aria-invalid') === 'true'; },
        };
    }

    function shell(id, labelText, control, help, error) {
        return el('div', { class: 'mb-3' },
            el('label', { for: id, class: LABEL_CLASS }, labelText),
            control,
            help ? el('p', { id: `${id}-help`, class: HELP_CLASS }, help) : null,
            error);
    }

    function slug(text) {
        const base = String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 56).replace(/-+$/g, '');
        return base || 'item';
    }

    function hostOf(url) {
        if (!url) return '';
        try { return new URL(url).hostname; } catch (error) { return ''; }
    }

    function parseDecimal(text) {
        const t = String(text).trim();
        if (t === '') return { value: null };
        if (!DECIMAL_RE.test(t)) return { value: null, type: 'bad_number' };
        return { value: Number(t) };
    }

    function urlCheck(value) {
        if (value.includes(MASK)) return 'url_masked_edit';
        if (/\s/.test(value)) return 'url_invalid';
        if (!/^https?:\/\//i.test(value)) return 'url_scheme';
        try {
            const parsed = new URL(value);
            if (!parsed.hostname) return 'url_invalid';
            if (parsed.username || parsed.password) return 'url_userinfo';
            if (parsed.hash || value.includes('#')) return 'url_fragment';
        } catch (error) {
            return 'url_invalid';
        }
        return '';
    }

    function focusAfterRemove(removes, index, fallback) {
        const target = removes[index] || removes[index - 1] || fallback;
        if (target) target.focus();
    }

    // --- item fields ---------------------------------------------------------------------------
    // Each builder returns { el, keys, read(out), write(data), validate(), showError(rest, type),
    // hasError(), focus(), setDisabled(bool) }.

    function textField(desc, item) {
        const id = item.uid(desc.key);
        const multiline = desc.kind === 'lines';
        const attrs = {
            id, autocomplete: 'off', spellcheck: 'false', 'aria-invalid': 'false', class: INPUT_CLASS,
            placeholder: desc.placeholder, maxlength: desc.maxlength, inputmode: desc.inputmode,
            'aria-describedby': describedBy(id, desc.help),
        };
        const input = multiline ? el('textarea', Object.assign(attrs, { rows: 3 })) : el('input', Object.assign(attrs, { type: 'text' }));
        const error = errorLine(`${id}-error`);
        const mark = errorMark([input], error);

        function parse() {
            const raw = String(input.value);
            if (desc.kind === 'lines') {
                const lines = raw.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
                return { value: lines.length ? lines : null };
            }
            if (desc.kind === 'int') {
                const t = raw.trim();
                if (t === '') return { value: null };
                return /^-?\d+$/.test(t) ? { value: parseInt(t, 10) } : { value: null, type: 'bad_number' };
            }
            const t = raw.trim();
            return { value: t === '' ? null : t };
        }
        function problem() {
            const parsed = parse();
            if (parsed.type) return parsed.type;
            const v = parsed.value;
            if (v === null) return desc.required ? 'blank' : '';
            if (desc.kind === 'lines') {
                if (v.length > MAX_LINES) return 'lines_too_many';
                if (v.some(line => line.length > 100)) return 'lines_too_long';
                return '';
            }
            if (desc.kind === 'int') return v < desc.min || v > desc.max ? 'out_of_range' : '';
            return desc.check ? desc.check(v) : '';
        }
        input.addEventListener('input', () => { mark.show(''); item.changed(); });
        input.addEventListener('blur', () => {
            if (!multiline) input.value = String(input.value).trim();
            const type = problem();
            mark.show(type ? messageFor(type, desc) : '');
        });

        return {
            el: shell(id, desc.label, input, desc.help, error),
            keys: [desc.key],
            read(out) { const v = parse().value; if (v !== null) out[desc.key] = v; },
            write(data) {
                const v = data[desc.key];
                input.value = v === null || v === undefined ? '' : (Array.isArray(v) ? v.join('\n') : String(v));
                mark.show('');
            },
            validate() { const type = problem(); mark.show(type ? messageFor(type, desc) : ''); return !type; },
            showError(rest, type) { mark.show(messageFor(type, desc)); },
            hasError: mark.has,
            focus() { input.focus(); },
            setDisabled(value) { input.disabled = value; },
        };
    }

    function selectField(desc, item) {
        const id = item.uid(desc.key);
        const options = desc.options.map(o => el('option', { value: o.value }, o.label));
        const select = el('select', { id, class: INPUT_CLASS, 'aria-invalid': 'false', 'aria-describedby': describedBy(id, true) }, ...options);
        const helpNode = el('p', { id: `${id}-help`, class: HELP_CLASS }, desc.help || '');
        const error = errorLine(`${id}-error`);
        const mark = errorMark([select], error);

        function showHelp() {
            const chosen = desc.options.find(o => o.value === String(select.value));
            helpNode.textContent = (chosen && chosen.help) || desc.help || '';
        }
        function read() {
            const v = String(select.value);
            if (desc.bool) return v === 'true' ? true : (v === 'false' ? false : null);
            return v === '' ? null : v;
        }
        select.addEventListener('change', () => { mark.show(''); showHelp(); item.changed(); });

        return {
            el: el('div', { class: 'mb-3' }, el('label', { for: id, class: LABEL_CLASS }, desc.label), select, helpNode, error),
            keys: [desc.key],
            read(out) { const v = read(); if (v !== null) out[desc.key] = v; },
            write(data) {
                const v = data[desc.key];
                select.value = v === null || v === undefined ? (desc.defaultValue || '') : String(v);
                mark.show('');
                showHelp();
            },
            validate() {
                const bad = desc.required && read() === null;
                mark.show(bad ? messageFor('literal_error', desc) : '');
                return !bad;
            },
            showError(rest, type) { mark.show(messageFor(type, desc)); },
            hasError: mark.has,
            focus() { select.focus(); },
            setDisabled(value) { select.disabled = value; },
        };
    }

    // A URL that may arrive masked. See the header for the wire rules.
    function urlField(desc, item) {
        const id = item.uid(desc.key);
        const input = el('input', {
            type: 'text', id, autocomplete: 'off', spellcheck: 'false', maxlength: 2048, inputmode: 'url',
            placeholder: desc.placeholder || 'https://', 'aria-invalid': 'false', 'aria-describedby': describedBy(id, true), class: INPUT_CLASS,
        });
        const maskedText = el('p', { id: `${id}-saved`, class: 'text-sm text-gray-200 break-all min-h-[44px] flex items-center' });
        const replace = el('button', { type: 'button', class: SECONDARY_BUTTON_CLASS, 'aria-describedby': `${id}-saved` }, 'Replace URL');
        const keep = el('button', { type: 'button', class: LINK_BUTTON_CLASS }, 'Keep the saved URL');
        const helpNode = el('p', { id: `${id}-help`, class: HELP_CLASS });
        const error = errorLine(`${id}-error`);
        const mark = errorMark([input], error);
        const savedRow = el('div', { role: 'group', 'aria-labelledby': `${id}-label`, class: 'flex flex-wrap items-center gap-2' }, maskedText, replace);
        const editRow = el('div', { class: 'flex flex-wrap items-center gap-2' }, input, keep);
        const labelNode = el('label', { for: id, id: `${id}-label`, class: LABEL_CLASS }, desc.label);
        let maskedValue = '';       // non-empty while a saved (masked) URL exists for this row
        let replacing = false;
        let disabled = false;

        function layout() {
            const showSaved = maskedValue !== '' && !replacing;
            savedRow.className = `${showSaved ? '' : HIDDEN}flex flex-wrap items-center gap-2`;
            editRow.className = `${showSaved ? HIDDEN : ''}flex flex-wrap items-center gap-2`;
            keep.className = `${maskedValue !== '' && replacing ? '' : HIDDEN}${LINK_BUTTON_CLASS}`;
            maskedText.textContent = maskedValue;
            helpNode.textContent = maskedValue !== ''
                ? 'The saved address is shown without its path and key. Replace it to change it.'
                : (desc.help || '');
            replace.disabled = disabled;
            input.disabled = disabled;
            keep.disabled = disabled;
        }
        function typed() { return String(input.value).trim(); }
        function problem() {
            if (maskedValue !== '' && (!replacing || typed() === '')) return '';
            const v = typed();
            if (v === '') return desc.required ? 'blank' : '';
            return urlCheck(v);
        }
        replace.addEventListener('click', () => { replacing = true; input.value = ''; mark.show(''); layout(); item.changed(); input.focus(); });
        keep.addEventListener('click', () => { replacing = false; input.value = ''; mark.show(''); layout(); item.changed(); replace.focus(); });
        input.addEventListener('input', () => { mark.show(''); item.changed(); });
        input.addEventListener('blur', () => {
            input.value = typed();
            const type = problem();
            mark.show(type ? messageFor(type, desc) : '');
        });

        return {
            el: el('div', { class: 'mb-3' }, labelNode, savedRow, editRow, helpNode, error),
            keys: [desc.key, `${desc.key}_masked`],
            read(out) {
                if (maskedValue !== '' && (!replacing || typed() === '')) {
                    out[desc.key] = maskedValue;
                    out[`${desc.key}_masked`] = true;
                } else if (typed() !== '') {
                    out[desc.key] = typed();
                }
            },
            write(data) {
                const v = data[desc.key];
                replacing = false;
                if (v && data[`${desc.key}_masked`] === true) { maskedValue = String(v); input.value = ''; }
                else { maskedValue = ''; input.value = v ? String(v) : ''; }
                mark.show('');
                layout();
            },
            validate() { const type = problem(); mark.show(type ? messageFor(type, desc) : ''); return !type; },
            showError(rest, type) {
                if (maskedValue !== '' && !replacing && (type === 'url_masked_edit' || type === 'url_mask_token')) {
                    replacing = true; input.value = ''; layout();
                }
                mark.show(messageFor(type, desc));
            },
            hasError: mark.has,
            focus() { (maskedValue !== '' && !replacing ? replace : input).focus(); },
            setDisabled(value) { disabled = value; layout(); },
            summaryUrl() { return maskedValue !== '' && !replacing ? maskedValue : typed(); },
        };
    }

    // The id of a row. New rows follow the name until the id is edited; a saved row's id is fixed
    // (it is how a masked URL finds its stored value), so renaming never changes it.
    function idField(desc, item) {
        const id = item.uid('id');
        const input = el('input', {
            type: 'text', id, autocomplete: 'off', spellcheck: 'false', maxlength: 64, 'aria-invalid': 'false',
            'aria-describedby': `${id}-help ${id}-error`, class: INPUT_CLASS,
        });
        const helpNode = el('p', { id: `${id}-help`, class: HELP_CLASS });
        const error = errorLine(`${id}-error`);
        const mark = errorMark([input], error);
        let fixed = false;
        let manual = false;

        function problem() {
            const v = String(input.value).trim();
            if (!ID_RE.test(v)) return 'bad_id';
            return item.idTaken(v) ? 'duplicate_id' : '';
        }
        function layout() {
            helpNode.textContent = fixed
                ? "Identifies this row. It stays the same when you rename it, so it can't be changed."
                : 'Made from the name. Change it only if you need to.';
            if (fixed) input.setAttribute('readonly', ''); else input.removeAttribute('readonly');
        }
        input.addEventListener('input', () => { manual = true; mark.show(''); item.changed(); });
        input.addEventListener('blur', () => { const type = problem(); mark.show(type ? messageFor(type, desc) : ''); });
        layout();

        return {
            el: el('div', { class: 'mb-3' }, el('label', { for: id, class: LABEL_CLASS }, 'Id'), input, helpNode, error),
            keys: ['id'],
            followName(name) { if (!fixed && !manual) { input.value = item.freeId(name); mark.show(''); } },
            read(out) { out.id = fixed ? String(input.value) : String(input.value).trim(); },
            write(data) {
                fixed = Boolean(data.id);
                manual = false;
                input.value = data.id ? String(data.id) : item.freeId('');
                mark.show('');
                layout();
            },
            validate() { const type = problem(); mark.show(type ? messageFor(type, desc) : ''); return !type; },
            showError(rest, type) { mark.show(messageFor(type, desc)); },
            hasError: mark.has,
            focus() { input.focus(); },
            setDisabled(value) { input.disabled = value; },
        };
    }

    // South, West, North, East. All four or none.
    function boundsField(desc, item) {
        const id = item.uid('bounds');
        const names = [['min_lat', 'South'], ['min_lon', 'West'], ['max_lat', 'North'], ['max_lon', 'East']];
        const error = errorLine(`${id}-error`);
        const inputs = {};
        const cells = names.map(([key, label]) => {
            const input = el('input', {
                type: 'number', step: 'any', inputmode: 'decimal', id: `${id}-${key}`, 'aria-invalid': 'false',
                'aria-describedby': `${id}-help ${id}-error`, class: INPUT_CLASS,
            });
            inputs[key] = input;
            input.addEventListener('input', () => { mark.show(''); item.changed(); });
            input.addEventListener('blur', () => { const type = problem(); mark.show(type ? messageFor(type) : ''); });
            return el('div', { class: 'flex-1 min-w-[7rem]' }, el('label', { for: `${id}-${key}`, class: LABEL_CLASS }, `${label} (${key.endsWith('lat') ? 'latitude' : 'longitude'})`), input);
        });
        const mark = errorMark(Object.values(inputs), error);

        function values() {
            const out = {};
            let type = '';
            names.forEach(([key]) => {
                const parsed = parseDecimal(inputs[key].value);
                if (parsed.type) type = parsed.type;
                out[key] = parsed.value;
            });
            return { out, type };
        }
        function problem() {
            const { out, type } = values();
            if (type) return type;
            const filled = names.filter(([key]) => out[key] !== null).length;
            if (filled === 0) return '';
            if (filled < 4) return 'bounds_incomplete';
            if (out.min_lat < -90 || out.max_lat > 90 || out.max_lat < -90 || out.min_lat > 90) return 'lat_range';
            if (out.min_lon < -180 || out.max_lon > 180 || out.max_lon < -180 || out.min_lon > 180) return 'lon_range';
            return out.min_lat < out.max_lat && out.min_lon < out.max_lon ? '' : 'bounds_order';
        }

        return {
            el: el('fieldset', { class: 'mb-3 border border-dark-border rounded-lg p-3' },
                el('legend', { class: 'px-1 text-sm font-medium text-gray-300' }, desc.label),
                el('div', { class: 'flex flex-wrap gap-3' }, ...cells),
                el('p', { id: `${id}-help`, class: HELP_CLASS }, desc.help), error),
            keys: ['bounds'],
            read(out) {
                const { out: v } = values();
                // A partly filled box is kept (so the card is dirty and validation catches it).
                if (names.some(([key]) => v[key] !== null)) out.bounds = v;
            },
            write(data) {
                const b = data.bounds || {};
                names.forEach(([key]) => { inputs[key].value = b[key] === null || b[key] === undefined ? '' : String(b[key]); });
                mark.show('');
            },
            validate() { const type = problem(); mark.show(type ? messageFor(type) : ''); return !type; },
            showError(rest, type) { mark.show(messageFor(type)); },
            hasError: mark.has,
            focus() { inputs.min_lat.focus(); },
            setDisabled(value) { Object.values(inputs).forEach(i => { i.disabled = value; }); },
        };
    }

    // Weekday and weekend ranges, each a start and an end time (both or neither).
    function hoursField(desc, item) {
        const id = item.uid('hours');
        const error = errorLine(`${id}-error`);
        const ranges = {};
        const groups = [['weekday', 'Weekdays'], ['weekend', 'Weekends']].map(([key, label]) => {
            const make = which => el('input', {
                type: 'time', id: `${id}-${key}-${which}`, 'aria-invalid': 'false', 'aria-describedby': `${id}-help ${id}-error`, class: INPUT_CLASS,
            });
            const start = make('start');
            const end = make('end');
            ranges[key] = { start, end };
            [start, end].forEach(input => {
                input.addEventListener('input', () => { mark.show(''); item.changed(); });
                input.addEventListener('blur', () => { const type = problem(); mark.show(type ? messageFor(type) : ''); });
            });
            return el('fieldset', { class: 'flex-1 min-w-[12rem] border border-dark-border rounded-lg p-3' },
                el('legend', { class: 'px-1 text-sm font-medium text-gray-300' }, label),
                el('label', { for: start.getAttribute('id'), class: LABEL_CLASS }, `${label} start`), start,
                el('label', { for: end.getAttribute('id'), class: `${LABEL_CLASS} mt-2` }, `${label} end`), end);
        });
        const mark = errorMark(Object.values(ranges).flatMap(r => [r.start, r.end]), error);
        const timeOk = v => /^([01]\d|2[0-3]):[0-5]\d$/.test(v);

        function problem() {
            for (const key of Object.keys(ranges)) {
                const a = String(ranges[key].start.value).trim();
                const b = String(ranges[key].end.value).trim();
                if (a === '' && b === '') continue;
                if (a === '' || b === '') return 'hours_incomplete';
                if (!timeOk(a) || !timeOk(b)) return 'bad_time';
            }
            return '';
        }

        return {
            el: el('div', { class: 'mb-3' },
                el('p', { class: 'text-sm font-medium text-gray-300 mb-1' }, desc.label),
                el('div', { class: 'flex flex-wrap gap-3' }, ...groups),
                el('p', { id: `${id}-help`, class: HELP_CLASS }, desc.help), error),
            keys: ['hours'],
            read(out) {
                const hours = {};
                Object.keys(ranges).forEach(key => {
                    const a = String(ranges[key].start.value).trim();
                    const b = String(ranges[key].end.value).trim();
                    if (a !== '' || b !== '') hours[key] = Object.assign(a !== '' ? { start: a } : {}, b !== '' ? { end: b } : {});
                });
                if (Object.keys(hours).length) out.hours = hours;
            },
            write(data) {
                const hours = data.hours || {};
                Object.keys(ranges).forEach(key => {
                    const r = hours[key] || {};
                    ranges[key].start.value = r.start || '';
                    ranges[key].end.value = r.end || '';
                });
                mark.show('');
            },
            validate() { const type = problem(); mark.show(type ? messageFor(type) : ''); return !type; },
            showError(rest, type) { mark.show(messageFor(type)); },
            hasError: mark.has,
            focus() { ranges.weekday.start.focus(); },
            setDisabled(value) { Object.values(ranges).forEach(r => { r.start.disabled = value; r.end.disabled = value; }); },
        };
    }

    // The stops of a static service: a sub-list of name, latitude, longitude.
    function stopsField(desc, item) {
        const id = item.uid('stops');
        const list = el('ul', { 'aria-label': 'Stops' });
        const error = errorLine(`${id}-error`);
        const addButton = el('button', { type: 'button', class: SECONDARY_BUTTON_CLASS }, 'Add stop');
        const root = el('div', { class: 'mb-3' }, el('p', { class: 'text-sm font-medium text-gray-300 mb-1' }, desc.label), list, addButton,
            el('p', { id: `${id}-help`, class: HELP_CLASS }, desc.help), error);
        const mark = {
            show(text) { setLine(error, text); flagged = Boolean(text); },
            has() { return flagged; },
        };
        let flagged = false;
        let stops = [];            // [{row, name, lat, lon, remove, errors}]
        let disabled = false;

        function makeStop(data) {
            const n = nextId(`${id}-s`);
            const make = (suffix, props) => el('input', Object.assign({ id: `${n}-${suffix}`, autocomplete: 'off', 'aria-invalid': 'false', class: INPUT_CLASS }, props));
            const stop = {
                name: make('name', { type: 'text', maxlength: 100, spellcheck: 'false' }),
                lat: make('lat', { type: 'text', inputmode: 'decimal' }),
                lon: make('lon', { type: 'text', inputmode: 'decimal' }),
                error: errorLine(`${n}-error`),
                remove: el('button', { type: 'button', class: SECONDARY_BUTTON_CLASS }, 'Remove'),
                label: {},
            };
            stop.name.value = (data && data.name) || '';
            stop.lat.value = data && data.lat !== undefined && data.lat !== null ? String(data.lat) : '';
            stop.lon.value = data && data.lon !== undefined && data.lon !== null ? String(data.lon) : '';
            ['name', 'lat', 'lon'].forEach(k => stop.label[k] = el('label', { for: `${n}-${k}`, class: LABEL_CLASS }));
            [stop.name, stop.lat, stop.lon].forEach(input => {
                input.setAttribute('aria-describedby', `${n}-error`);
                input.addEventListener('input', () => { setStopError(stop, ''); relabel(); item.changed(); });
                input.addEventListener('blur', () => { input.value = String(input.value).trim(); const type = stopProblem(stop); setStopError(stop, type ? messageFor(type) : ''); });
            });
            stop.remove.addEventListener('click', () => removeStop(stop));
            stop.row = el('li', { class: 'mb-3 border border-dark-border rounded-lg p-3' },
                el('div', { class: 'flex flex-wrap items-end gap-3' },
                    el('div', { class: 'flex-1 min-w-[10rem]' }, stop.label.name, stop.name),
                    el('div', { class: 'w-36' }, stop.label.lat, stop.lat),
                    el('div', { class: 'w-36' }, stop.label.lon, stop.lon),
                    stop.remove),
                stop.error);
            return stop;
        }
        function setStopError(stop, text) {
            setLine(stop.error, text);
            [stop.name, stop.lat, stop.lon].forEach(i => i.setAttribute('aria-invalid', text ? 'true' : 'false'));
        }
        function stopProblem(stop) {
            if (String(stop.name.value).trim() === '') return 'blank';
            const lat = parseDecimal(stop.lat.value);
            const lon = parseDecimal(stop.lon.value);
            if (lat.value === null || lon.value === null) return 'bad_number';
            if (lat.value < -90 || lat.value > 90) return 'lat_range';
            if (lon.value < -180 || lon.value > 180) return 'lon_range';
            return '';
        }
        function relabel() {
            stops.forEach((stop, i) => {
                const n = i + 1;
                const name = String(stop.name.value).trim();
                stop.label.name.textContent = `Stop ${n} name`;
                stop.label.lat.textContent = `Stop ${n} latitude`;
                stop.label.lon.textContent = `Stop ${n} longitude`;
                stop.remove.setAttribute('aria-label', `Remove stop ${n}${name ? ': ' + name : ''}`);
            });
            addButton.disabled = disabled || stops.length >= MAX_STOPS;
        }
        function render() {
            clear(list);
            stops.forEach(stop => list.appendChild(stop.row));
            relabel();
            stops.forEach(stop => { [stop.name, stop.lat, stop.lon].forEach(i => { i.disabled = disabled; }); stop.remove.disabled = disabled; });
        }
        function removeStop(stop) {
            const index = stops.indexOf(stop);
            stops.splice(index, 1);
            render();
            item.changed();
            item.announce(`Stop ${index + 1} removed`);
            focusAfterRemove(stops.map(s => s.remove), index, addButton);
        }
        addButton.addEventListener('click', () => {
            const stop = makeStop(null);
            stops.push(stop);
            render();
            item.changed();
            item.announce(`Stop ${stops.length} added`);
            stop.name.focus();
        });
        render();

        return {
            el: root,
            keys: ['stops'],
            read(out) {
                out.stops = stops.map(stop => {
                    const lat = parseDecimal(stop.lat.value).value;
                    const lon = parseDecimal(stop.lon.value).value;
                    return { name: String(stop.name.value).trim(), lat, lon };
                });
            },
            write(data) {
                stops = (data.stops || []).map(makeStop);
                mark.show('');
                render();
            },
            validate() {
                let ok = true;
                stops.forEach(stop => { const type = stopProblem(stop); setStopError(stop, type ? messageFor(type) : ''); if (type) ok = false; });
                if (stops.length === 0) { mark.show(messageFor('stops_required')); ok = false; } else mark.show('');
                return ok;
            },
            showError(rest, type) {
                const stop = typeof rest[0] === 'number' ? stops[rest[0]] : null;
                if (stop) setStopError(stop, messageFor(type)); else mark.show(messageFor(type));
            },
            hasError() { return mark.has() || stops.some(s => s.name.getAttribute('aria-invalid') === 'true'); },
            focus() {
                const bad = stops.find(s => s.name.getAttribute('aria-invalid') === 'true');
                if (bad) bad.name.focus(); else addButton.focus();
            },
            setDisabled(value) { disabled = value; render(); },
        };
    }

    const FIELD_BUILDERS = {
        text: textField, lines: textField, int: textField, select: selectField, url: urlField,
        id: idField, bounds: boundsField, hours: hoursField, stops: stopsField,
    };

    // --- row definitions -----------------------------------------------------------------------

    const FARE_OPTIONS = [
        { value: '', label: 'Not stated' }, { value: 'true', label: 'Free to ride' }, { value: 'false', label: 'Paid' },
    ];
    const COST_OPTIONS = [
        { value: '', label: 'Not stated' }, { value: 'true', label: 'Free' }, { value: 'false', label: 'Paid' },
    ];
    const SOURCE_TYPES = [
        { value: 'link_scan', label: 'Links on a page',
          help: "Reads a page, follows each link whose address contains the link path (under Advanced), and reads the event on each one." },
        { value: 'event_cards', label: 'Event cards on a page',
          help: 'Reads event cards listed on one page. Set the link path under Advanced to pick out the event links.' },
        { value: 'tribe_events_api', label: 'WordPress Events Calendar',
          help: 'For WordPress sites that use The Events Calendar plugin. Use the address of the site\'s events page.' },
        { value: 'squarespace_eventlist', label: 'Squarespace event list',
          help: "For Squarespace sites with an events list page. Use the address of that list." },
    ];
    const typeLabel = value => (SOURCE_TYPES.find(t => t.value === value) || {}).label || '';
    const typeCheck = v => (TYPE_RE.test(v) ? '' : 'bad_type');
    const NAME = { kind: 'text', key: 'name', label: 'Name', required: true, maxlength: 100 };
    const ID = { kind: 'id', key: 'id' };
    const MAX_BYTES_FIELD = {
        kind: 'int', key: 'max_bytes', label: 'Largest download (bytes)', inputmode: 'numeric', min: MIN_BYTES, max: MAX_BYTES,
        help: 'Between 1048576 (1 MB) and 104857600 (100 MB). Leave blank for 100 MB.',
        rangeMessage: 'Enter a whole number from 1048576 to 104857600, or leave it blank.',
    };

    const ROW_TYPES = {
        feed: {
            noun: 'feed', plural: 'feeds', maxItems: 20, addLabel: 'Add feed', itemLabel: 'Transit feed',
            emptyText: 'None. No transit feeds are loaded.',
            emptyHint: "No transit feeds yet. Add your agency's GTFS feed URL — most agencies list it on their developer or open-data page.",
            fields: [
                Object.assign({}, NAME, { help: 'A label for you, like the agency name.' }),
                { kind: 'url', key: 'url', label: 'Feed URL', required: true, help: "The feed's public http(s) address." },
                { kind: 'text', key: 'agency', label: 'Agency', maxlength: 100 },
                { kind: 'select', key: 'free', label: 'Fare', bool: true, options: FARE_OPTIONS },
                { kind: 'text', key: 'description', label: 'Description', maxlength: 300 },
            ],
            advanced: [
                ID,
                { kind: 'text', key: 'type', label: 'Stop type', maxlength: 32, check: typeCheck, help: 'Lowercase letters and _ only, like bus_stop. Leave blank for bus stops.' },
                { kind: 'bounds', key: 'bounds', label: 'Only load stops inside this area', help: 'Optional. Fill in all four numbers, or leave them all blank to load every stop.' },
                MAX_BYTES_FIELD,
            ],
            meta: (data, urlText) => [data.agency, hostOf(urlText)].filter(Boolean).join(' · '),
        },
        static_service: {
            noun: 'service', plural: 'services', maxItems: 20, addLabel: 'Add service', itemLabel: 'Fixed-route service',
            emptyText: 'None. No fixed-route services are listed.',
            emptyHint: 'No fixed-route services yet. Add a shuttle or ferry that runs on a set schedule, with its stops.',
            fields: [
                Object.assign({}, NAME, { help: 'A label for you, like the shuttle name.' }),
                { kind: 'int', key: 'frequency_minutes', label: 'Runs every (minutes)', required: true, inputmode: 'numeric', min: 1, max: 1440,
                  help: 'Whole minutes between departures, from 1 to 1440.', rangeMessage: 'Enter whole minutes from 1 to 1440.' },
                { kind: 'select', key: 'free', label: 'Fare', bool: true, options: FARE_OPTIONS },
                { kind: 'hours', key: 'hours', label: 'Service hours', help: 'Optional. Leave a range blank if the service does not run then.' },
                { kind: 'stops', key: 'stops', label: 'Stops', help: 'Latitude and longitude as decimal degrees, like 40.7128 and -74.0060.' },
            ],
            advanced: [
                ID,
                { kind: 'text', key: 'type', label: 'Stop type', maxlength: 32, check: typeCheck, help: 'Lowercase letters and _ only, like ferry_terminal.' },
                { kind: 'text', key: 'agency_name', label: 'Agency name', maxlength: 100 },
                { kind: 'text', key: 'description', label: 'Description', maxlength: 300 },
            ],
            meta: data => {
                const count = Array.isArray(data.stops) ? data.stops.length : 0;
                return [`${count} ${count === 1 ? 'stop' : 'stops'}`, data.frequency_minutes ? `every ${data.frequency_minutes} min` : ''].filter(Boolean).join(' · ');
            },
        },
        source: {
            noun: 'source', plural: 'sources', maxItems: 20, addLabel: 'Add source', itemLabel: 'Event source',
            emptyText: 'None. Community events are off.',
            emptyHint: 'No event sources yet. Add the address of a page that lists local events.',
            fields: [
                Object.assign({}, NAME, { help: 'A label for you, like the venue or town calendar.' }),
                { kind: 'select', key: 'type', label: 'How the page is read', required: true, options: SOURCE_TYPES, defaultValue: 'link_scan' },
                { kind: 'url', key: 'url', label: 'Page address', required: true, help: "The page's public http(s) address." },
                { kind: 'select', key: 'is_free', label: 'Cost', bool: true, options: COST_OPTIONS },
                { kind: 'text', key: 'category', label: 'Category', maxlength: 100, help: 'Optional, like downtown or family.' },
            ],
            advanced: [
                ID,
                { kind: 'url', key: 'base_url', label: 'Site address for relative links', placeholder: 'https://', help: 'Optional. Put in front of links on the page that start with /.' },
                { kind: 'text', key: 'link_path', label: 'Link path', maxlength: 200, placeholder: '/events/',
                  check: v => (v.startsWith('/') && !v.includes('//') && !v.includes('\\') && !/\s/.test(v) && v.length <= 200 ? '' : 'bad_link_path'),
                  help: 'Part of an event link\'s address, like /events/. Used by the link and event card types.' },
                { kind: 'text', key: 'default_location', label: 'Default venue', maxlength: 100, help: 'Used when an event lists no venue.' },
                { kind: 'text', key: 'default_address', label: 'Default address', maxlength: 100 },
                { kind: 'text', key: 'default_city', label: 'Default city', maxlength: 100 },
                { kind: 'text', key: 'default_state', label: 'Default state', maxlength: 100 },
                { kind: 'lines', key: 'location_match', label: 'Venue words to match', help: 'Optional. One per line (up to 20). Only events whose venue contains one of these are kept.' },
                { kind: 'lines', key: 'regions', label: 'Regions', help: 'Optional. One per line (up to 20). The areas this source covers.' },
                MAX_BYTES_FIELD,
            ],
            meta: (data, urlText) => [typeLabel(data.type), hostOf(urlText)].filter(Boolean).join(' · '),
        },
    };

    // --- one row ---------------------------------------------------------------------------------

    function createRow(rowType, data, listCtx) {
        const rowId = nextId('hhl-row');
        const panelId = `${rowId}-panel`;
        const item = {
            uid: key => `${rowId}-${key}`,
            changed() { refreshSummary(); listCtx.changed(); },
            announce: listCtx.announce,
            idTaken: id => listCtx.otherIds(row).includes(id),
            freeId: name => listCtx.freeId(row, name),
        };
        const fields = rowType.fields.map(desc => FIELD_BUILDERS[desc.kind](desc, item));
        const advanced = rowType.advanced.map(desc => FIELD_BUILDERS[desc.kind](desc, item));
        const all = fields.concat(advanced);
        const idControl = advanced.find(f => f.followName);
        const nameControl = fields[0];
        const urlControl = all.find(f => f.summaryUrl);

        const title = el('span', { class: 'font-medium text-white break-words' });
        const meta = el('span', { class: 'block text-xs text-gray-400 break-all' });
        const edit = el('button', { type: 'button', class: SECONDARY_BUTTON_CLASS, 'aria-expanded': 'false', 'aria-controls': panelId }, 'Edit');
        const remove = el('button', { type: 'button', class: SECONDARY_BUTTON_CLASS }, 'Remove');
        const advancedBox = el('details', { class: 'mb-2' },
            el('summary', { class: 'min-h-[44px] cursor-pointer text-sm text-gray-300 flex items-center' }, 'Advanced'),
            ...advanced.map(f => f.el));
        const rowError = errorLine(`${rowId}-error`);
        let rowFlag = false;
        const panel = el('div', { id: panelId, role: 'group', class: `${HIDDEN}px-3 pb-3` }, rowError, ...fields.map(f => f.el), advancedBox);
        const li = el('li', { class: 'mb-3 border border-dark-border rounded-lg', 'data-list-row': rowType.noun },
            el('div', { class: 'flex flex-wrap items-center gap-2 p-3' },
                el('div', { class: 'flex-1 min-w-[12rem]' }, title, meta), edit, remove),
            panel);
        let open = false;
        let disabled = false;

        const row = {
            el: li, remove, edit, data: null,
            read() { const out = {}; all.forEach(f => f.read(out)); return out; },
        };

        function nameText() { return String(row.read().name || '').trim(); }
        function refreshSummary() {
            const current = row.read();
            const name = nameText();
            if (idControl) idControl.followName(name);
            title.textContent = name || `New ${rowType.noun}`;
            meta.textContent = rowType.meta(current, urlControl ? urlControl.summaryUrl() : '');
            const label = name ? `${rowType.noun} ${name}` : `new ${rowType.noun}`;
            edit.textContent = open ? 'Done' : 'Edit';
            edit.setAttribute('aria-label', `${open ? 'Done editing' : 'Edit'} ${label}`);
            remove.setAttribute('aria-label', `Remove ${label}`);
            panel.setAttribute('aria-label', `Settings for ${label}`);
        }
        function setOpen(value) {
            open = value;
            panel.className = `${open ? '' : HIDDEN}px-3 pb-3`;
            edit.setAttribute('aria-expanded', open ? 'true' : 'false');
            refreshSummary();
        }
        function revealAdvanced() { advancedBox.setAttribute('open', ''); }

        edit.addEventListener('click', () => listCtx.toggle(row));
        remove.addEventListener('click', () => listCtx.remove(row));

        Object.assign(row, {
            isOpen: () => open,
            setOpen,
            write(values) {
                all.forEach(f => f.write(values || {}));
                refreshSummary();
            },
            id() { const out = {}; (idControl || { read() {} }).read(out); return out.id; },
            validate() {
                let ok = true;
                setLine(rowError, '');
                rowFlag = false;
                all.forEach(f => { if (!f.validate()) ok = false; });
                if (!ok) {
                    setOpen(true);
                    if (advanced.some(f => f.hasError())) revealAdvanced();
                }
                return ok;
            },
            hasError() { return all.some(f => f.hasError()) || rowFlag; },
            focusInvalid() {
                const bad = all.find(f => f.hasError());
                if (bad) { if (advanced.includes(bad)) revealAdvanced(); bad.focus(); } else edit.focus();
            },
            showServerError(rest, type) {
                setOpen(true);
                const key = rest[0];
                const owner = all.find(f => f.keys.includes(key));
                if (owner) {
                    if (advanced.includes(owner)) revealAdvanced();
                    owner.showError(rest.slice(1), type);
                } else {
                    setLine(rowError, messageFor(type));
                    rowFlag = true;
                }
            },
            setDisabled(value) {
                disabled = value;
                all.forEach(f => f.setDisabled(value));
                remove.disabled = value;
            },
            firstField: () => nameControl,
            isDisabled: () => disabled,
        });
        row.write(data);
        return row;
    }

    // --- the list control ------------------------------------------------------------------------

    function objectListControl(field, ctx) {
        const rowType = ROW_TYPES[field.itemType];
        if (!rowType) throw new Error(`unknown list item type ${field.itemType}`);
        const id = `hh-${ctx.section}-${field.name}`;
        const heading = el('p', { class: 'block text-sm font-medium text-gray-300 mb-2', id: `${id}-label` }, field.label);
        const root = el('div', { class: 'mb-6', id, tabindex: '-1' });
        const listError = errorLine(`${id}-error`);
        let state = 'unset';
        let rows = [];
        let disabled = false;
        let listFlag = false;
        let buttons = [];

        const listCtx = {
            changed: () => ctx.changed(),
            announce: text => ctx.announce(text),
            otherIds: row => rows.filter(r => r !== row).map(r => r.id()),
            freeId(row, name) {
                const taken = new Set(rows.filter(r => r !== row).map(r => r.id()));
                const base = slug(name || rowType.noun);
                let candidate = base;
                for (let n = 2; taken.has(candidate); n += 1) candidate = `${base.slice(0, 60)}-${n}`;
                return candidate;
            },
            toggle(row) {
                const willOpen = !row.isOpen();
                rows.forEach(r => r.setOpen(false));
                if (willOpen) row.setOpen(true);
            },
            remove(row) {
                const index = rows.indexOf(row);
                const name = String(row.read().name || '').trim() || `new ${rowType.noun}`;
                rows.splice(index, 1);
                if (rows.length === 0) state = 'empty';
                render();
                ctx.changed();
                ctx.announce(`Removed ${rowType.noun} ${name}`);
                const removes = rows.map(r => r.remove);
                if (removes.length) focusAfterRemove(removes, index, null);
                else firstButton().focus();
            },
        };

        function firstButton() { return buttons[0] || root; }
        function addRow() {
            const row = createRow(rowType, null, listCtx);
            rows.forEach(r => r.setOpen(false));
            rows.push(row);
            row.setOpen(true);
            row.setDisabled(disabled);
            return row;
        }
        function plainButton(text, className, handler) {
            const b = el('button', { type: 'button', class: className, disabled: disabled }, text);
            b.addEventListener('click', handler);
            buttons.push(b);
            return b;
        }
        function startList() {
            state = 'rows';
            rows = [];
            const row = addRow();
            render();
            ctx.changed();
            ctx.announce(`${rowType.noun} 1 added`);
            row.firstField().focus();
        }
        function useServer() {
            state = 'unset';
            rows = [];
            render();
            ctx.changed();
            ctx.announce("Using the server's setting");
            firstButton().focus();
        }
        function render() {
            clear(root);
            buttons = [];
            root.appendChild(heading);
            if (state === 'unset') {
                root.appendChild(el('p', { class: 'text-sm text-gray-400 mb-2' }, "Not set here. Athena uses the server's setting."));
                root.appendChild(plainButton('Set a list here', BUTTON_CLASS, startList));
            } else if (state === 'empty') {
                root.appendChild(el('p', { class: 'text-sm text-gray-300 mb-1' }, rowType.emptyText));
                root.appendChild(el('p', { class: 'text-xs text-gray-500 mb-2' }, rowType.emptyHint));
                root.appendChild(plainButton(rowType.addLabel, BUTTON_CLASS, () => {
                    state = 'rows';
                    const row = addRow();
                    render();
                    ctx.changed();
                    ctx.announce(`${rowType.noun} 1 added`);
                    row.firstField().focus();
                }));
                root.appendChild(plainButton("Use the server's setting", LINK_BUTTON_CLASS, useServer));
            } else {
                const list = el('ul', { 'aria-labelledby': `${id}-label` });
                rows.forEach(row => list.appendChild(row.el));
                root.appendChild(list);
                const full = rows.length >= rowType.maxItems;
                const add = plainButton(rowType.addLabel, BUTTON_CLASS, () => {
                    const row = addRow();
                    render();
                    ctx.changed();
                    ctx.announce(`${rowType.noun} ${rows.length} added`);
                    row.firstField().focus();
                });
                add.disabled = disabled || full;
                root.appendChild(add);
                if (full) root.appendChild(el('p', { class: 'text-xs text-gray-500 mt-1' }, `You can add up to ${rowType.maxItems} ${rowType.plural}.`));
                root.appendChild(plainButton("Use the server's setting", LINK_BUTTON_CLASS, useServer));
            }
            root.appendChild(listError);
            if (field.help) root.appendChild(el('p', { class: 'text-xs text-gray-500 mt-2' }, field.help));
            rows.forEach(row => row.setDisabled(disabled));
        }
        function listHasError() { return listFlag; }
        function setListError(text) {
            setLine(listError, text);
            listFlag = Boolean(text);
        }

        render();

        return {
            el: root,
            fields: [field.name],
            get() {
                if (state === 'unset') return { [field.name]: null };
                if (state === 'empty') return { [field.name]: [] };
                return { [field.name]: rows.map(r => r.read()) };
            },
            set(value) {
                const list = value ? value[field.name] : null;
                setListError('');
                if (list === null || list === undefined) { state = 'unset'; rows = []; }
                else if (list.length === 0) { state = 'empty'; rows = []; }
                else {
                    state = 'rows';
                    rows = list.map(values => createRow(rowType, values, listCtx));
                }
                render();
            },
            validate() {
                setListError('');
                let ok = true;
                let firstBad = null;
                const seen = new Set();
                rows.forEach(row => {
                    if (!row.validate()) { ok = false; firstBad = firstBad || row; }
                    seen.add(row.id());
                });
                if (ok && seen.size !== rows.length) { setListError(messageFor('duplicate_id')); ok = false; }
                if (firstBad) rows.forEach(r => { if (r !== firstBad) r.setOpen(false); });
                return ok;
            },
            showServerError(loc, type) {
                const row = typeof loc[1] === 'number' ? rows[loc[1]] : null;
                if (row) {
                    rows.forEach(r => { if (r !== row) r.setOpen(false); });
                    row.showServerError(loc.slice(2), type);
                } else {
                    setListError(messageFor(type));
                }
            },
            hasError() { return listHasError() || rows.some(r => r.hasError()); },
            focus() {
                const bad = rows.find(r => r.hasError());
                if (bad) bad.focusInvalid(); else firstButton().focus();
            },
            setDisabled(value) { if (value === disabled) return; disabled = value; render(); },
            refresh() {},
        };
    }

    HS.registerKind('object_list', objectListControl);

    // --- cards -------------------------------------------------------------------------------------

    HS.defineCard('transit', {
        title: 'Transit',
        blurb: 'The bus and rail data Athena uses to answer transit questions.',
        savedCopy: 'Saved. The transit service reloads within about a minute.',
        intro: ['Feeds are downloaded by the transit service. Private-network and cluster addresses can only be set in the server\'s environment.'],
        fields: [
            { name: 'region_name', kind: 'text', label: 'Region name', maxlength: 100, placeholder: 'Server setting',
              help: 'The area these feeds cover, like the city or agency region.' },
            { name: 'gtfs_feeds', kind: 'object_list', itemType: 'feed', label: 'Transit feeds (GTFS)' },
            { name: 'static_services', kind: 'object_list', itemType: 'static_service', label: 'Fixed-route services' },
        ],
    });

    HS.defineCard('community_events', {
        title: 'Community events',
        blurb: 'The local event pages Athena reads to answer what is on nearby.',
        savedCopy: 'Saved. The events service reloads within about a minute.',
        intro: ['Each source is a web page the events service reads. Private-network and cluster addresses can only be set in the server\'s environment.'],
        fields: [
            { name: 'sources', kind: 'object_list', itemType: 'source', label: 'Event sources' },
        ],
    });
})(window);
