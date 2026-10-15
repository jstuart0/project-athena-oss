/**
 * Household settings cards.
 *
 * One generic renderer over GET /api/settings/household and
 * PUT /api/settings/household/{section}. A card is a table entry (CARDS) that
 * lists fields; each field has a kind (KINDS) that builds a control. Pages call
 * HouseholdSettings.mount(section, containerId): it is idempotent, so a page's
 * Refresh can call it again (a clean card re-fetches, a card with unsaved edits
 * keeps them).
 *
 * Extension points for list editors (household-settings-lists.js, which loads
 * after this file): HouseholdSettings.registerKind(kind, build),
 * HouseholdSettings.defineCard(section, definition) and HouseholdSettings.ui.
 * A card definition may set savedCopy to replace the default success line. A control is
 *   { el, fields: [wire names], get() -> {name: value|null}, set(value),
 *     validate() -> bool, showServerError(loc, type), hasError(), focus(),
 *     setDisabled(bool), refresh() }.
 *
 * Everything is built with createElement/textContent (a value is data, never
 * markup); no handler text is written, listeners use addEventListener.
 */
(function (global) {
    'use strict';

    const URL_BASE = '/api/settings/household';
    const OWNER_PERMISSION = 'manage_infrastructure';
    const SAVED_COPY = 'Saved. Changes reach the assistant within a minute.';
    const OWNER_ONLY_NOTE = 'Only the house owner can change these settings.';
    const WRITE_ONLY_NOTE = 'You can view these settings. Changing them needs write access.';
    const RESTRICTED_NOTE = 'Only the house owner can see and change these settings.';
    const LEGEND = "Fields marked \"Server setting\" aren't set here. Athena uses the server's value or its built-in default.";
    const LEGEND_KINDS = ['text', 'entity', 'temperature_pair'];
    const NARROW_INPUT = 'max-w-[8rem]';
    const MEDIUM_INPUT = 'max-w-xl';
    const INVALID_NOTE = 'Some fields need attention. Fix the highlighted fields and save again.';

    const INPUT_CLASS = 'w-full min-h-[44px] px-3 py-2 bg-dark-bg border border-dark-border rounded-lg text-white focus:outline-none focus:border-blue-500 transition-colors disabled:opacity-50';
    const BUTTON_CLASS = 'min-h-[44px] px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
    const LINK_BUTTON_CLASS = 'min-h-[44px] px-2 py-2 text-sm text-blue-400 hover:text-blue-300 underline disabled:opacity-50 disabled:cursor-not-allowed';
    const SECONDARY_BUTTON_CLASS = 'min-h-[44px] px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
    const HIDDEN = 'hidden ';

    // --- tiny DOM helper (text only: strings become text nodes) ---------------------------------

    function el(tag, props, ...children) {
        const node = document.createElement(tag);
        if (props) {
            Object.keys(props).forEach(key => {
                const value = props[key];
                if (value === null || value === undefined || value === false) return;
                if (key === 'class') node.className = value;
                else if (key === 'text') node.textContent = value;
                else if (key === 'on') Object.keys(value).forEach(name => node.addEventListener(name, value[name]));
                else node.setAttribute(key, value === true ? '' : String(value));
            });
        }
        children.forEach(child => { if (child !== null && child !== undefined && child !== false) node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child); });
        return node;
    }

    function clear(node) { node.textContent = ''; }

    // --- messages: one table for client checks and server 422 types -----------------------------

    const RANGE_MESSAGE = 'Lowest must be 40–95°F and no higher than highest.';
    const MESSAGES = {
        bad_entity_id: f => `Use an entity id like ${f.example}.`,
        bad_entity_domain: f => `Use a ${(f.domains || []).map(d => d + '.').join(' or ')} entity id, like ${f.example}.`,
        entity_not_allowed: f => f.notAllowed || "That entity isn't allowed for this setting. Pick a different one.",
        bad_station: () => 'Use the 3-letter Amtrak code.',
        bad_country_code: () => 'Enter 1–3 digits, e.g. 1 or 44.',
        bad_model_name: () => 'Model names use letters, numbers and . _ : / - only.',
        bad_room: () => 'Up to 64 letters, numbers, spaces and \' _ . & - only. Names like "whole house" or "my room" aren\'t allowed.',
        temperature_range: () => RANGE_MESSAGE,
        temperature_order: () => RANGE_MESSAGE,
        greater_than_equal: () => RANGE_MESSAGE,
        less_than_equal: () => RANGE_MESSAGE,
        int_parsing: () => RANGE_MESSAGE,
        int_type: () => RANGE_MESSAGE,
        int_from_float: () => RANGE_MESSAGE,
        temperature_pair_incomplete: () => "Fill in both temperatures, or clear both. Until both are set here, Athena uses the server's pair.",
        glob_invalid: () => 'Start with light. and use only lowercase letters, numbers, _ and *.',
        glob_too_broad: () => "A pattern can't start with *. Name the start of the light, like light.garage_*.",
        glob_too_long: () => 'A pattern is at most 128 characters.',
        glob_too_many_wildcards: () => 'Use at most 3 * in a pattern.',
        blank: () => 'Enter a value or remove this row.',
        too_long: () => 'That list is too long.',
        list_type: () => "That list wasn't accepted.",
        string_too_long: () => 'That value is too long.',
        bad_characters: () => 'That value has characters that aren\'t allowed.',
    };

    function messageFor(type, field) {
        const entry = MESSAGES[type];
        if (typeof entry === 'function') return entry(field || {});
        return "This value wasn't accepted.";
    }

    // --- client checks (they mirror the server's rules; the server stays the authority) ----------

    const ENTITY_RE = /^[a-z_]+\.[a-z0-9_]+$/;
    const APPLIANCE_BAD = ['hot_water', 'boiler', 'tank', 'cylinder', 'geyser'];
    const MODEL_RE = /^[A-Za-z0-9._:/-]{1,255}$/;
    const ROOM_RE = /^[A-Za-z0-9 _'.&-]{1,64}$/;
    const GLOB_BODY_RE = /^[a-z0-9_*]+$/;

    function entityCheck(field) {
        return value => {
            if (value.length > 255 || !ENTITY_RE.test(value)) return 'bad_entity_id';
            const dot = value.indexOf('.');
            const domain = value.slice(0, dot);
            const object = value.slice(dot + 1);
            if (!(field.domains || []).includes(domain)) return 'bad_entity_domain';
            if (field.appliance && (object === 'water_heater' || APPLIANCE_BAD.some(t => object.includes(t)))) return 'entity_not_allowed';
            if ((field.deny || []).some(t => object.includes(t))) return 'entity_not_allowed';
            return '';
        };
    }

    function globCheck(value) {
        if (value.length > 128) return 'glob_too_long';
        if (!value.startsWith('light.')) return 'glob_invalid';
        const body = value.slice('light.'.length);
        if (!body || !GLOB_BODY_RE.test(body) || body.includes('**')) return 'glob_invalid';
        if (body.startsWith('*')) return 'glob_too_broad';
        if (body.split('*').length - 1 > 3) return 'glob_too_many_wildcards';
        return '';
    }

    // --- controls --------------------------------------------------------------------------------

    function fieldId(ctx, name) { return `hh-${ctx.section}-${name}`; }

    function errorLine(id) {
        return el('p', { id, role: 'alert', class: `${HIDDEN}text-xs text-red-400 mt-1` });
    }

    function setLine(node, text) {
        node.textContent = text || '';
        node.className = `${text ? '' : HIDDEN}text-xs text-red-400 mt-1`;
    }

    // A single text input. Emptying it stores null (unset), never "".
    function textControl(field, ctx) {
        const id = fieldId(ctx, field.name);
        const helpId = `${id}-help`;
        const errorId = `${id}-error`;
        const input = el('input', {
            type: 'text', id, autocomplete: 'off', spellcheck: 'false',
            maxlength: field.maxlength || 255, inputmode: field.inputmode,
            placeholder: field.placeholder || 'Server setting',
            'aria-describedby': `${helpId} ${errorId}`, 'aria-invalid': 'false',
            class: field.narrow ? `${INPUT_CLASS} ${NARROW_INPUT}` : (field.medium ? `${INPUT_CLASS} ${MEDIUM_INPUT}` : INPUT_CLASS),
        });
        const error = errorLine(errorId);
        const clearButton = el('button', {
            type: 'button', class: `${HIDDEN}${LINK_BUTTON_CLASS}`,
            'aria-label': `Clear ${field.label}, use the server setting`, text: 'Clear',
        });
        let disabled = false;

        function normalized() {
            let value = String(input.value).trim();
            if (field.normalize) value = field.normalize(value);
            return value;
        }
        function showError(text) {
            setLine(error, text);
            input.setAttribute('aria-invalid', text ? 'true' : 'false');
        }
        function refresh() {
            clearButton.className = `${normalized() !== '' && !disabled ? '' : HIDDEN}${LINK_BUTTON_CLASS}`;
        }
        function validate() {
            const value = normalized();
            const type = value === '' || !field.check ? '' : field.check(value);
            showError(type ? messageFor(type, field) : '');
            return !type;
        }

        input.addEventListener('input', () => { showError(''); refresh(); ctx.changed(); });
        input.addEventListener('blur', () => { input.value = normalized(); refresh(); validate(); });
        clearButton.addEventListener('click', () => {
            input.value = '';
            showError('');
            refresh();
            ctx.changed();
            input.focus();
        });

        const el_ = el('div', { class: 'mb-4' },
            el('label', { for: id, class: 'block text-sm font-medium text-gray-400 mb-2' }, field.label),
            el('div', { class: 'flex items-center gap-2' }, input, clearButton),
            el('p', { id: helpId, class: 'text-xs text-gray-500 mt-1' }, field.help || ''),
            error);

        return {
            el: el_,
            fields: [field.name],
            get() { const v = normalized(); return { [field.name]: v === '' ? null : v }; },
            set(value) {
                const v = value && value[field.name];
                input.value = v === null || v === undefined ? '' : String(v);
                showError('');
                refresh();
            },
            validate,
            showServerError(loc, type) { showError(messageFor(type, field)); },
            hasError() { return input.getAttribute('aria-invalid') === 'true'; },
            focus() { input.focus(); },
            setDisabled(value) { disabled = value; input.disabled = value; clearButton.disabled = value; refresh(); },
            refresh,
        };
    }

    function entityControl(field, ctx) {
        const merged = Object.assign({
            medium: true,
            maxlength: 255,
            help: `A Home Assistant entity id, e.g. ${field.example}.`,
            check: entityCheck(field),
        }, field);
        return textControl(merged, ctx);
    }

    // Two whole-degree Fahrenheit inputs that are saved together or not at all.
    function temperaturePairControl(field, ctx) {
        const id = fieldId(ctx, 'temps');
        const errorId = `${id}-error`;
        const helpId = `${id}-help`;
        const make = (name, placeholder) => el('input', {
            type: 'number', id: `${id}-${name}`, min: 40, max: 95, step: 1, inputmode: 'numeric',
            placeholder, 'aria-describedby': `${helpId} ${errorId}`, 'aria-invalid': 'false', class: INPUT_CLASS,
        });
        const low = make('min', '65');
        const high = make('max', '75');
        const error = errorLine(errorId);
        const clearButton = el('button', {
            type: 'button', class: `${HIDDEN}${LINK_BUTTON_CLASS}`,
            'aria-label': 'Clear the temperature range, use the server setting', text: 'Clear',
        });
        let disabled = false;

        const read = input => {
            const text = String(input.value).trim();
            if (text === '') return null;
            return /^-?\d+$/.test(text) ? parseInt(text, 10) : NaN;
        };
        function showError(text) {
            setLine(error, text);
            [low, high].forEach(i => i.setAttribute('aria-invalid', text ? 'true' : 'false'));
        }
        function refresh() {
            const any = read(low) !== null || read(high) !== null;
            clearButton.className = `${any && !disabled ? '' : HIDDEN}${LINK_BUTTON_CLASS}`;
        }
        function validate() {
            const a = read(low);
            const b = read(high);
            let type = '';
            if (a === null && b === null) type = '';
            else if (a === null || b === null) type = 'temperature_pair_incomplete';
            else if (Number.isNaN(a) || Number.isNaN(b) || a < 40 || b > 95 || a > 95 || b < 40) type = 'temperature_range';
            else if (a > b) type = 'temperature_order';
            showError(type ? messageFor(type, field) : '');
            return !type;
        }
        [low, high].forEach(input => {
            input.addEventListener('input', () => { showError(''); refresh(); ctx.changed(); });
            input.addEventListener('blur', validate);
        });
        clearButton.addEventListener('click', () => {
            low.value = '';
            high.value = '';
            showError('');
            refresh();
            ctx.changed();
            low.focus();
        });

        const labelled = (input, text) => el('div', { class: 'flex-1' },
            el('label', { for: input.getAttribute('id'), class: 'block text-sm font-medium text-gray-400 mb-2' }, text), input);
        return {
            el: el('fieldset', { class: 'mb-4' },
                el('legend', { class: 'block text-sm font-medium text-gray-400 mb-2' }, field.label),
                el('div', { class: 'flex items-end gap-3' },
                    labelled(low, 'Lowest temperature (°F)'), labelled(high, 'Highest temperature (°F)'), clearButton),
                el('p', { id: helpId, class: 'text-xs text-gray-500 mt-1' }, field.help || ''),
                error),
            fields: ['min_temp', 'max_temp'],
            get() { return { min_temp: read(low), max_temp: read(high) }; },
            set(value) {
                const set = (input, v) => { input.value = v === null || v === undefined ? '' : String(v); };
                set(low, value && value.min_temp);
                set(high, value && value.max_temp);
                showError('');
                refresh();
            },
            validate,
            showServerError(loc, type) { showError(messageFor(type, field)); },
            hasError() { return low.getAttribute('aria-invalid') === 'true'; },
            focus() { low.focus(); },
            setDisabled(value) { disabled = value; low.disabled = value; high.disabled = value; clearButton.disabled = value; refresh(); },
            refresh,
        };
    }

    // A list of short strings with three visible states: unset (null), explicitly
    // empty ([]), and rows. Moving between them is a deliberate button press.
    function textListControl(field, ctx) {
        const id = fieldId(ctx, field.name);
        const maxItems = field.maxItems || 50;
        const itemLabel = field.itemLabel || 'Item';
        let state = 'unset';
        let items = [];       // [{row, input, error}]
        let disabled = false;
        let itemSerial = 0;
        const root = el('div', { class: 'mb-4', id });
        const heading = el('p', { class: 'block text-sm font-medium text-gray-400 mb-2', id: `${id}-label` }, field.label);

        function makeItem(value) {
            const index = items.length + 1;
            itemSerial += 1;
            const input = el('input', {
                type: 'text', autocomplete: 'off', spellcheck: 'false', maxlength: field.maxlength || 128,
                placeholder: field.placeholder, 'aria-label': `${itemLabel} ${index}`, 'aria-invalid': 'false', class: INPUT_CLASS,
            });
            input.value = value || '';
            const error = errorLine(`${id}-error-${itemSerial}`);
            input.setAttribute('aria-describedby', error.getAttribute('id'));
            const remove = el('button', { type: 'button', class: SECONDARY_BUTTON_CLASS }, 'Remove');
            const item = { input, error, remove, row: null };
            input.addEventListener('input', () => { setItemError(item, ''); relabel(); ctx.changed(); });
            input.addEventListener('blur', () => { input.value = String(input.value).trim(); validateItem(item); });
            remove.addEventListener('click', () => removeItem(item));
            item.row = el('li', { class: 'mb-2' }, el('div', { class: 'flex items-center gap-2' }, input, remove), error);
            return item;
        }
        function setItemError(item, text) {
            setLine(item.error, text);
            item.input.setAttribute('aria-invalid', text ? 'true' : 'false');
        }
        function validateItem(item) {
            const value = String(item.input.value).trim();
            const type = value === '' ? 'blank' : (field.check ? field.check(value) : '');
            setItemError(item, type ? messageFor(type, field) : '');
            return !type;
        }
        function relabel() {
            items.forEach((item, i) => {
                const value = String(item.input.value).trim();
                item.input.setAttribute('aria-label', `${itemLabel} ${i + 1}`);
                item.remove.setAttribute('aria-label', `Remove ${itemLabel.toLowerCase()} ${i + 1}${value ? ': ' + value : ''}`);
            });
        }

        let addButton = null;
        function render() {
            clear(root);
            root.appendChild(heading);
            if (state === 'unset') {
                root.appendChild(el('p', { class: 'text-sm text-gray-400 mb-2' }, "Not set here. Athena uses the server's setting."));
                const set = el('button', { type: 'button', class: BUTTON_CLASS, disabled: disabled }, 'Set a list here');
                set.addEventListener('click', () => { state = 'rows'; items = [makeItem('')]; render(); ctx.changed(); items[0].input.focus(); });
                root.appendChild(set);
            } else if (state === 'empty') {
                root.appendChild(el('p', { class: 'text-sm text-gray-300 mb-2' }, field.emptyText || 'None.'));
                const add = el('button', { type: 'button', class: BUTTON_CLASS, disabled: disabled }, `Add ${itemLabel.toLowerCase()}`);
                add.addEventListener('click', () => { state = 'rows'; items = [makeItem('')]; render(); ctx.changed(); items[0].input.focus(); });
                root.appendChild(add);
                root.appendChild(useServerButton());
            } else {
                relabel();
                const list = el('ul', { 'aria-labelledby': `${id}-label` });
                items.forEach(item => list.appendChild(item.row));
                root.appendChild(list);
                addButton = el('button', { type: 'button', class: BUTTON_CLASS, disabled: disabled || items.length >= maxItems }, `Add ${itemLabel.toLowerCase()}`);
                addButton.addEventListener('click', () => {
                    const item = makeItem('');
                    items.push(item);
                    render();
                    ctx.changed();
                    ctx.announce(`${itemLabel} ${items.length} added`);
                    item.input.focus();
                });
                root.appendChild(addButton);
                root.appendChild(useServerButton());
            }
            if (field.help) root.appendChild(el('p', { class: 'text-xs text-gray-500 mt-2' }, field.help));
            items.forEach(item => { item.input.disabled = disabled; item.remove.disabled = disabled; });
        }
        function useServerButton() {
            const button = el('button', { type: 'button', class: LINK_BUTTON_CLASS, disabled: disabled }, "Use the server's setting");
            button.addEventListener('click', () => { state = 'unset'; items = []; render(); ctx.changed(); ctx.announce("Using the server's setting"); });
            return button;
        }
        function removeItem(item) {
            const index = items.indexOf(item);
            items.splice(index, 1);
            if (items.length === 0) state = 'empty';
            render();
            ctx.changed();
            ctx.announce(`${itemLabel} removed`);
            const removes = items.map(i => i.remove);
            if (removes.length) (removes[index] || removes[index - 1]).focus();
            else focusFirstButton();
        }
        function focusFirstButton() {
            const buttons = Array.from(root.children).filter(c => c.tagName === 'BUTTON');
            if (buttons.length) buttons[0].focus();
        }

        render();

        return {
            el: root,
            fields: [field.name],
            get() {
                if (state === 'unset') return { [field.name]: null };
                if (state === 'empty') return { [field.name]: [] };
                return { [field.name]: items.map(i => String(i.input.value).trim()) };
            },
            set(value) {
                const list = value ? value[field.name] : null;
                if (list === null || list === undefined) { state = 'unset'; items = []; }
                else if (list.length === 0) { state = 'empty'; items = []; }
                else { state = 'rows'; items = list.map(makeItem); }
                render();
            },
            validate() {
                let ok = true;
                items.forEach(item => { if (!validateItem(item)) ok = false; });
                return ok;
            },
            showServerError(loc, type) {
                const item = typeof loc[1] === 'number' ? items[loc[1]] : null;
                if (item) setItemError(item, messageFor(type, field));
                else if (items[0]) setItemError(items[0], messageFor(type, field));
            },
            hasError() { return items.some(i => i.input.getAttribute('aria-invalid') === 'true'); },
            focus() { const bad = items.find(i => i.input.getAttribute('aria-invalid') === 'true') || items[0]; if (bad) bad.input.focus(); else focusFirstButton(); },
            setDisabled(value) { if (value === disabled) return; disabled = value; render(); },
            refresh() {},
        };
    }

    const KINDS = {
        text: textControl,
        entity: entityControl,
        temperature_pair: temperaturePairControl,
        text_list: textListControl,
    };

    function registerKind(kind, build) {
        if (typeof build !== 'function') throw new Error('a field kind is a function (field, ctx) -> control');
        KINDS[kind] = build;
    }

    // --- card definitions ------------------------------------------------------------------------

    const CARDS = {};

    function defineCard(section, definition) { CARDS[section] = definition; }

    defineCard('gateway', {
        title: 'Gateway fallback model',
        blurb: "The model the gateway falls back to when its chosen model is unavailable.",
        intro: ["Reset to Defaults on this page doesn't change this setting."],
        fields: [{
            name: 'fallback_model', kind: 'text', label: 'Fallback model', maxlength: 255, medium: true,
            placeholder: 'Server setting, else the default model', check: v => (MODEL_RE.test(v) ? '' : 'bad_model_name'),
            help: "Used when the gateway's chosen model is unavailable.",
        }],
    });

    defineCard('sms', {
        title: 'Default country code',
        blurb: 'Used to read guest phone numbers that have no country prefix.',
        dirtyWarning: "Changing this changes which bookings a text is matched to. After saving, check that a current guest's texts are still recognised.",
        fields: [{
            name: 'default_country_code', kind: 'text', label: 'Country code', maxlength: 4, inputmode: 'numeric',
            placeholder: 'Server setting', narrow: true,
            normalize: v => (v.startsWith('+') ? v.slice(1) : v),
            check: v => (/^[0-9]{1,3}$/.test(v) && parseInt(v, 10) !== 0 ? '' : 'bad_country_code'),
            help: "Used when a guest's phone number has no + prefix. US/Canada: 1.",
        }],
    });

    defineCard('amtrak', {
        title: 'Amtrak station',
        blurb: 'The station Athena uses for train questions when none is named.',
        fields: [{
            name: 'default_station', kind: 'text', label: 'Station code', maxlength: 3, placeholder: 'Server setting', narrow: true,
            normalize: v => v.toUpperCase(),
            check: v => (/^[A-Z]{3}$/.test(v) ? '' : 'bad_station'),
            help: 'Three-letter station code, e.g. NYP.',
        }],
    });

    defineCard('lights', {
        title: "Lights that aren't lamps",
        blurb: 'Lights that "the kitchen lights" and similar commands leave alone.',
        intro: [
            "Whenever a list is saved here, Athena also skips voice-satellite LED rings and status LEDs (names containing led_ring or status_led). A list saved here replaces the server's list.",
        ],
        fields: [{
            name: 'room_light_exclude', kind: 'text_list', label: 'Lights to skip', itemLabel: 'Pattern', maxItems: 50, maxlength: 128,
            placeholder: 'light.garage_strip', check: globCheck,
            emptyText: 'None. Only the built-in rule applies.',
            help: 'An entity id, or a pattern ending in * (e.g. light.hallway_*). Other wildcard placements aren\'t supported.',
        }],
    });

    const bedSwitch = (name, label) => ({
        name, kind: 'entity', label, domains: ['switch'], example: 'switch.bed_power',
        deny: ['door', 'gate', 'garage', 'lock', 'unlock', 'opener', 'water_heater', 'boiler', 'alarm', 'siren', 'valve'],
        notAllowed: 'That switch looks like a door, lock, valve or similar, so it can\'t be a bed warmer control.',
        placeholder: 'Server setting',
    });
    const bedLevel = (name, label) => ({
        name, kind: 'entity', label, domains: ['select'], example: 'select.bed_heat_level', placeholder: 'Server setting',
    });

    defineCard('bed_warmer', {
        title: 'Bed warmer',
        blurb: 'The Home Assistant entities Athena switches for the bed warmer.',
        intro: ["A bed warmer is saved as a set. Athena uses this one only when all five are filled in; otherwise it uses the server's setting."],
        fields: [
            Object.assign(bedLevel('level_left', 'Left heat level'), { group: 'Left side' }),
            Object.assign(bedLevel('level_right', 'Right heat level'), { group: 'Right side' }),
            Object.assign(bedSwitch('power_main', 'Main power'), { group: 'Power' }),
            Object.assign(bedSwitch('power_side_a', 'Side A power'), { group: 'Power' }),
            Object.assign(bedSwitch('power_side_b', 'Side B power'), { group: 'Power' }),
        ],
        warn(values) {
            const names = ['level_left', 'level_right', 'power_main', 'power_side_a', 'power_side_b'];
            const filled = names.filter(n => values[n] !== null && values[n] !== undefined).length;
            return filled > 0 && filled < names.length
                ? "The bed warmer needs all five. Until they're all filled in, Athena uses the server's setting, which may be a different bed."
                : '';
        },
    });

    const appliance = (name, label, example) => ({
        name, kind: 'entity', label, domains: ['water_heater'], appliance: true, example,
        notAllowed: "That looks like the house water heater or a hot-water tank. Pick the appliance's own entity.",
        placeholder: 'Server setting',
    });
    const sensor = (name, label, domains, example) => ({
        name, kind: 'entity', label, domains, example, placeholder: 'Server setting', disclosure: 'Appliance sensors',
    });

    defineCard('jarvis_web', {
        title: 'Jarvis web app',
        blurb: 'The room, thermostat and kitchen appliances the Jarvis web app controls.',
        fields: [
            {
                name: 'default_room', kind: 'text', label: 'Default room', maxlength: 64, medium: true, placeholder: 'Server setting, else guest',
                check: v => (ROOM_RE.test(v) ? '' : 'bad_room'),
                help: 'The room the Jarvis web app controls by default.',
            },
            { name: 'climate_entity', kind: 'entity', label: 'Thermostat', domains: ['climate'], example: 'climate.living_room', placeholder: 'Server setting, else climate.thermostat' },
            {
                name: 'temperature_range', kind: 'temperature_pair', label: 'Thermostat range',
                help: "°F. Guests can't set the thermostat outside this range. If the server has no setting either, Athena uses 65–75°F.",
            },
            appliance('oven_entity', 'Oven', 'water_heater.oven'),
            appliance('fridge_entity', 'Fridge', 'water_heater.fridge'),
            appliance('freezer_entity', 'Freezer', 'water_heater.freezer'),
            sensor('stove_cook_mode_sensor', 'Stove cook mode sensor', ['sensor'], 'sensor.stove_cook_mode'),
            sensor('stove_display_temp_sensor', 'Stove display temperature sensor', ['sensor'], 'sensor.stove_display_temp'),
            sensor('stove_timer_sensor', 'Stove timer sensor', ['sensor'], 'sensor.stove_timer'),
            sensor('fridge_door_sensor', 'Fridge door sensor', ['binary_sensor', 'sensor'], 'binary_sensor.fridge_door'),
        ],
    });

    // --- the dirty registry and the leave guard (one for every card on every page) ----------------

    const cards = new Map();   // section -> card

    function dirtyCards() { return Array.from(cards.values()).filter(card => card.isDirty()); }

    function joinNames(names) {
        if (names.length <= 1) return names.join('');
        return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
    }

    function leaveMessage(list) {
        return `You have unsaved changes to ${joinNames(list.map(c => c.title))}. Leave without saving?`;
    }

    // A browser Back/Forward has already put the destination in the URL; staying
    // means the URL must name the tab that is still showing.
    function restoreHash(card) {
        const id = card.tabId();
        if (id.startsWith('tab-') && global.history && typeof global.history.replaceState === 'function') {
            global.history.replaceState(null, '', `#${id.slice(4)}`);
        }
    }

    let tabGuardInstalled = false;
    function installTabGuard() {
        if (tabGuardInstalled || typeof global.showTab !== 'function') return;
        tabGuardInstalled = true;
        const original = global.showTab;
        global.showTab = function (tabName) {
            const leaving = dirtyCards().filter(card => card.tabShowing() && card.tabId() !== `tab-${tabName}`);
            if (leaving.length) {
                if (!global.confirm(leaveMessage(leaving))) {
                    restoreHash(leaving[0]);
                    return undefined;
                }
                leaving.forEach(card => card.discard());
            }
            return original.apply(this, arguments);
        };
    }

    global.addEventListener('beforeunload', event => {
        if (dirtyCards().length) {
            event.preventDefault();
            event.returnValue = '';
        }
    });
    installTabGuard();

    // --- a card ------------------------------------------------------------------------------------

    function canDo(permission) {
        return typeof global.hasPermission === 'function' && global.hasPermission(permission) === true;
    }

    // app.js keeps the token in a script-scoped `let` (not on window), so ask the shared
    // accessor from utils.js, which falls back to the stored login.
    function signedInToken() {
        if (typeof global.getAuthToken === 'function') return global.getAuthToken();
        return global.localStorage ? global.localStorage.getItem('auth_token') : null;
    }

    function authHeaders(extra) {
        return Object.assign({ 'Authorization': `Bearer ${signedInToken()}` }, extra || {});
    }

    function createCard(section, container) {
        const def = CARDS[section];
        const base = `hh-${section}`;
        const card = {
            section, title: def.title, container,
            view: 'loading',          // loading | ready | error | forbidden
            restricted: false, readOnlyNote: '',
            revision: 0, stored: {}, baseline: '[]',
            controls: [], owner: new Map(),
            saving: false, conflict: null, loadToken: 0, bodyKey: '',
        };

        const heading = el('h3', { id: `${base}-heading`, tabindex: '-1', class: 'text-lg font-semibold text-white focus:outline-none' }, def.title);
        const dirtyMarker = el('span', { class: `${HIDDEN}text-xs text-yellow-400` }, 'Unsaved changes');
        const discardButton = el('button', { type: 'button', class: `${HIDDEN}${LINK_BUTTON_CLASS}` }, 'Discard changes');
        const SAVE_CLASS = 'min-h-[44px] px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
        const saveButton = el('button', { type: 'button', disabled: true, class: `${HIDDEN}${SAVE_CLASS}` }, 'Save');
        const legendText = def.legend !== undefined
            ? def.legend
            : (def.fields.some(field => LEGEND_KINDS.includes(field.kind)) ? LEGEND : '');
        const legend = el('p', { class: 'text-xs text-gray-500 mb-4' }, legendText);
        const note = el('p', { class: `${HIDDEN}text-sm text-gray-400 mb-3` });
        const conflictText = el('p', { role: 'alert', class: 'text-sm text-yellow-400 mb-2' });
        const reapply = el('button', { type: 'button', class: BUTTON_CLASS }, 'Re-apply my changes');
        const conflictBox = el('div', { class: `${HIDDEN}mb-3` }, conflictText, reapply);
        const message = el('p', { role: 'alert', class: `${HIDDEN}text-sm mb-3` });
        const status = el('span', { class: 'sr-only', role: 'status', 'aria-live': 'polite' });
        const warning = el('p', { class: `${HIDDEN}text-sm text-yellow-400 mb-3` });
        const body = el('div', { 'aria-labelledby': `${base}-heading` });

        const root = el('div', { class: 'bg-dark-card border border-dark-border rounded-lg p-6 mb-6', 'data-household-card': section },
            el('div', { class: 'flex justify-between items-start gap-4 mb-2' },
                el('div', null, heading, el('p', { class: 'text-sm text-gray-400 mt-1' }, def.blurb)),
                el('div', { class: 'flex items-center gap-3 shrink-0' }, dirtyMarker, discardButton, saveButton)),
            ...(def.intro || []).map(line => el('p', { class: 'text-xs text-gray-500 mb-2' }, line)),
            legend, note, conflictBox, message, status, warning, body);

        function announce(text) { status.textContent = text || ''; }

        function setMessage(text, tone) {
            card.messageTone = text ? tone : '';
            message.textContent = text || '';
            message.setAttribute('role', tone === 'success' ? 'status' : 'alert');
            message.className = `${text ? '' : HIDDEN}text-sm mb-3 ${tone === 'error' ? 'text-red-400' : 'text-green-400'}`;
        }

        // --- values ---
        function collect() {
            const value = {};
            card.controls.forEach(control => Object.assign(value, control.get()));
            return value;
        }
        function serialize() { return JSON.stringify(collect()); }

        function editable() { return card.view === 'ready' && !card.restricted && !card.readOnlyNote; }
        function isDirty() { return editable() && serialize() !== card.baseline; }

        function ensureControls() {
            if (card.controls.length) return;
            const ctx = { section, changed, announce };
            def.fields.forEach(field => {
                const build = KINDS[field.kind];
                if (!build) throw new Error(`unknown field kind ${field.kind}`);
                const control = build(field, ctx);
                control.field = field;
                card.controls.push(control);
                control.fields.forEach(name => card.owner.set(name, control));
            });
        }

        function applyValue(value) {
            card.controls.forEach(control => control.set(value || {}));
        }

        function updateChrome() {
            const dirty = isDirty();
            saveButton.disabled = !dirty || card.saving;
            saveButton.textContent = card.saving ? 'Saving…' : 'Save';
            saveButton.className = `${editable() ? '' : HIDDEN}${SAVE_CLASS}`;
            dirtyMarker.className = `${dirty ? '' : HIDDEN}text-xs text-yellow-400`;
            discardButton.className = `${dirty && !card.saving ? '' : HIDDEN}${LINK_BUTTON_CLASS}`;
            const warnings = [];
            if (editable() && def.warn) warnings.push(def.warn(collect()));
            if (dirty && def.dirtyWarning) warnings.push(def.dirtyWarning);
            const warnText = warnings.filter(Boolean).join(' ');
            warning.textContent = warnText;
            warning.className = `${warnText ? '' : HIDDEN}text-sm text-yellow-400 mb-3`;
            conflictBox.className = `${card.conflict && editable() ? '' : HIDDEN}mb-3`;
            card.controls.forEach(control => { control.setDisabled(!editable() || card.saving); });
        }

        // An edit: the "Saved" line is no longer true, and a warning that appears
        // because of the edit is announced (a warning present at load is not).
        function changed() {
            if (card.messageTone === 'success') setMessage('');
            const before = warning.textContent;
            updateChrome();
            if (warning.textContent && warning.textContent !== before) announce(warning.textContent);
        }

        // --- body ---
        function fieldStored(field) {
            const names = field.kind === 'temperature_pair' ? ['min_temp', 'max_temp'] : [field.name];
            return names.some(name => card.stored[name] === true);
        }

        function renderRestricted() {
            const list = el('ul', { class: 'space-y-2' });
            def.fields.forEach(field => {
                list.appendChild(el('li', { class: 'text-sm text-gray-300' },
                    el('span', { class: 'text-gray-400' }, `${field.label}: `),
                    fieldStored(field) ? 'Set by the owner' : 'Not set here'));
            });
            return list;
        }

        function renderControls() {
            const wrap = el('div', null);
            let group = null;
            let groupKey = '';
            def.fields.forEach((field, index) => {
                const control = card.controls[index];
                const key = field.group ? `g:${field.group}` : (field.disclosure ? `d:${field.disclosure}` : '');
                if (key !== groupKey) {
                    groupKey = key;
                    group = null;
                    if (field.group) {
                        group = el('fieldset', { class: 'mb-2 border border-dark-border rounded-lg p-3' },
                            el('legend', { class: 'px-1 text-sm font-medium text-gray-300' }, field.group));
                    } else if (field.disclosure) {
                        group = el('details', { class: 'mb-2' },
                            el('summary', { class: 'min-h-[44px] cursor-pointer text-sm text-gray-300 flex items-center' }, field.disclosure));
                    }
                    if (group) wrap.appendChild(group);
                }
                (group || wrap).appendChild(control.el);
            });
            return wrap;
        }

        function renderBody() {
            const key = `${card.view}|${card.restricted}|${card.readOnlyNote}`;
            if (key === card.bodyKey) return;
            card.bodyKey = key;
            clear(body);
            legend.className = `${legendText && card.view === 'ready' && !card.restricted ? '' : HIDDEN}text-xs text-gray-500 mb-4`;
            const noteText = card.restricted ? RESTRICTED_NOTE : card.readOnlyNote;
            note.textContent = noteText;
            note.className = `${noteText ? '' : HIDDEN}text-sm text-gray-400 mb-3`;
            if (card.view === 'loading') {
                body.appendChild(el('p', { class: 'text-sm text-gray-400' }, 'Loading…'));
            } else if (card.view === 'forbidden') {
                body.appendChild(el('p', { class: 'text-sm text-gray-400' }, "You don't have permission to view this setting."));
            } else if (card.view === 'error') {
                const retry = el('button', { type: 'button', class: BUTTON_CLASS }, 'Retry');
                retry.addEventListener('click', () => load(true));
                body.appendChild(el('p', { class: 'text-sm text-red-400 mb-2', role: 'alert' }, `Couldn't load ${def.title}.`));
                body.appendChild(retry);
            } else if (card.restricted) {
                body.appendChild(renderRestricted());
            } else {
                ensureControls();
                body.appendChild(renderControls());
            }
        }

        function render() { renderBody(); updateChrome(); }

        // --- loading ---
        function applyEntry(entry) {
            card.revision = entry.revision || 0;
            card.stored = entry.stored || {};
            card.restricted = entry.restricted === true;
            const needed = entry.required_permission || 'write';
            card.readOnlyNote = '';
            if (!card.restricted && !canDo(needed)) {
                card.readOnlyNote = needed === OWNER_PERMISSION ? OWNER_ONLY_NOTE : WRITE_ONLY_NOTE;
            }
            card.view = 'ready';
            if (!card.restricted) {
                ensureControls();
                applyValue(entry.value);
                card.baseline = serialize();
            }
        }

        async function load(force) {
            if (!force && (isDirty() || card.saving)) return;
            const token = ++card.loadToken;
            card.view = 'loading';
            render();
            try {
                const response = await global.fetch(`${global.API_BASE || ''}${URL_BASE}?sections=${encodeURIComponent(section)}`, { headers: authHeaders() });
                if (token !== card.loadToken) return;
                if (response.status === 403) {
                    card.view = 'forbidden';
                } else if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                } else {
                    const body_ = await response.json();
                    if (token !== card.loadToken) return;
                    if (!body_ || !body_[section]) throw new Error('section missing');
                    applyEntry(body_[section]);
                }
            } catch (error) {
                if (token !== card.loadToken) return;
                console.error(`Failed to load household setting ${section}:`, error);
                card.view = 'error';
            }
            card.bodyKey = '';
            render();
        }

        // --- saving ---
        function firstInvalid() { return card.controls.find(control => control.hasError()); }

        function showConflict() {
            conflictText.textContent = `Someone else saved ${def.title} while you were editing. Your changes weren't saved. The latest version is shown below.`;
            updateChrome();
        }
        function hideConflict() {
            card.conflict = null;
            updateChrome();
        }

        function applyServerErrors(detail) {
            const list = Array.isArray(detail) ? detail : [];
            const unplaced = [];
            list.forEach(problem => {
                const loc = Array.isArray(problem.loc) ? problem.loc : [];
                const control = card.owner.get(loc[0]);
                if (control) control.showServerError(loc, problem.type);
                else unplaced.push(messageFor(problem.type, {}));
            });
            setMessage([INVALID_NOTE].concat(unplaced).join(' '), 'error');
            const bad = firstInvalid();
            if (bad) bad.focus();
        }

        async function save() {
            if (!isDirty() || card.saving) return;
            let valid = true;
            card.controls.forEach(control => { if (!control.validate()) valid = false; });
            if (!valid) {
                setMessage(INVALID_NOTE, 'error');
                const bad = firstInvalid();
                if (bad) bad.focus();
                return;
            }
            card.saving = true;
            setMessage('');
            updateChrome();
            try {
                const response = await global.fetch(`${global.API_BASE || ''}${URL_BASE}/${encodeURIComponent(section)}`, {
                    method: 'PUT',
                    headers: authHeaders({ 'Content-Type': 'application/json' }),
                    body: JSON.stringify({ value: collect(), revision: card.revision }),
                });
                if (response.ok) {
                    card.saving = false;
                    hideConflict();
                    applyEntry(await response.json());
                    card.bodyKey = '';
                    render();
                    setMessage(def.savedCopy || SAVED_COPY, 'success');
                    heading.focus();
                    return;
                }
                card.saving = false;
                if (response.status === 409) {
                    const draft = collect();
                    await load(true);
                    card.conflict = draft;
                    showConflict();
                    if (editable()) reapply.focus();
                    return;
                }
                if (response.status === 422) {
                    let detail = null;
                    try { detail = (await response.json()).detail; } catch (e) { detail = null; }
                    applyServerErrors(detail);
                } else if (response.status === 403) {
                    setMessage("You don't have permission to change this setting.", 'error');
                } else {
                    setMessage(`Couldn't save ${def.title}. Try again in a moment.`, 'error');
                }
            } catch (error) {
                console.error(`Failed to save household setting ${section}:`, error);
                setMessage(`Couldn't save ${def.title}. Check your connection and try again.`, 'error');
            }
            card.saving = false;
            updateChrome();
        }

        function reapplyDraft() {
            if (!card.conflict || !editable()) return;
            applyValue(card.conflict);
            hideConflict();
            updateChrome();
            announce('Your changes are back. Review them, then save.');
            const first = card.controls[0];
            if (first) first.focus();
        }

        function discard() {
            if (card.baseline && card.controls.length) applyValue(JSON.parse(card.baseline));
            hideConflict();
            setMessage('');
            updateChrome();
        }

        function discardByUser() {
            discard();
            heading.focus();
            announce('Changes discarded.');
        }

        function tabElement() {
            let node = container;
            while (node) {
                if (String(node.className || '').split(/\s+/).includes('tab-content')) return node;
                node = node.parentElement;
            }
            return null;
        }

        saveButton.addEventListener('click', save);
        discardButton.addEventListener('click', discardByUser);
        reapply.addEventListener('click', reapplyDraft);

        Object.assign(card, {
            el: root, heading, isDirty, load, save, discard, render,
            tabId() { const tab = tabElement(); return tab ? tab.getAttribute('id') : ''; },
            tabShowing() { const tab = tabElement(); return !!tab && !String(tab.className).split(/\s+/).includes('hidden'); },
        });
        clear(container);
        container.appendChild(root);
        return card;
    }

    // --- public API -------------------------------------------------------------------------------

    // force: reload even over unsaved edits. A plain mount keeps them.
    function mount(section, containerId, options) {
        if (!CARDS[section]) throw new Error(`no household card for section ${section}`);
        const container = document.getElementById(containerId);
        if (!container) return Promise.resolve();
        installTabGuard();
        let card = cards.get(section);
        if (!card || card.container !== container) {
            card = createCard(section, container);
            cards.set(section, card);
            return card.load(true);
        }
        return card.load(Boolean(options && options.force));
    }

    function focusHeading(section) {
        const card = cards.get(section);
        if (card) card.heading.focus();
    }

    // The small DOM helpers and control classes, shared with household-settings-lists.js.
    const ui = { el, clear, setLine, errorLine, INPUT_CLASS, BUTTON_CLASS, SECONDARY_BUTTON_CLASS, LINK_BUTTON_CLASS, HIDDEN };

    global.HouseholdSettings = { mount, focusHeading, registerKind, defineCard, messageFor, ui, authHeaders };
})(window);
