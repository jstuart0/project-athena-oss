/**
 * Room settings cards: "Room lights" and "Speaker rooms" on the Rooms tab.
 *
 * Loads after household-settings.js and plugs into it: it registers the
 * 'room_lights' and 'speaker_rooms' field kinds and defines the two cards, so
 * the dirty guard, the 409 recovery ("Re-apply my changes"), owner-only and
 * restricted rendering, idempotent mounting and the save flow are the generic
 * renderer's. Each list has the same three visible states as the other list
 * editors: not set here (null), explicitly none ([]), and rows.
 *
 * The pickers read Home Assistant through admin-backend:
 *   GET /api/settings/room-lights/candidates   lights (a group answers as its bulbs)
 *   GET /api/settings/speaker-rooms/satellites voice satellites
 * Both are owner-only, like the saved values. A picker that fails never blocks the card: an
 * entity id can still be typed.
 * Home Assistant's friendly names are data; they are only ever set as text or
 * as attribute values, never as markup.
 *
 * Built with createElement/textContent only; listeners use addEventListener.
 */
(function (global) {
    'use strict';

    const HS = global.HouseholdSettings;
    if (!HS || !HS.ui) {
        console.error('room-settings.js must load after household-settings.js');
        return;
    }
    const { el, clear, setLine, errorLine, INPUT_CLASS, BUTTON_CLASS, LINK_BUTTON_CLASS, HIDDEN } = HS.ui;

    const LIGHTS_CONTAINER = 'household-card-room-lights';
    const SPEAKERS_CONTAINER = 'household-card-speaker-rooms';
    const MAX_ROOMS = 64;
    const MAX_LIGHTS = 32;
    const MAX_ROOM_NAME = 64;
    const PICKER_STALE_MS = 30000;
    const OWNER_PERMISSION = 'manage_infrastructure';
    const EMPTY_STATE_ANNOUNCEMENT = "None are listed now. Save to use only Home Assistant areas, or use the server's setting.";

    const SECONDARY_BUTTON_CLASS = 'min-h-[44px] px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
    const LABEL_CLASS = 'block text-sm font-medium text-gray-400 mb-1';
    const HELP_CLASS = 'text-xs text-gray-500 mt-1';
    const LIGHT_ID_RE = /^light\.[a-z0-9_]+$/;
    const SATELLITE_ID_RE = /^assist_satellite\.[a-z0-9_]+$/;
    const DEVICE_ID_RE = /^[0-9a-f]{32}$/;
    const HEX_RE = /^[0-9a-f]+$/;
    const RESERVED_ROOM_KEYS = new Set([
        'whole_house', 'house', 'all', 'everywhere', 'unknown', 'multi_room',
        'here', 'this_room', 'my_room', 'the_room', 'in_here',
        'the_whole_house', 'entire_house', 'every_room', 'all_rooms', 'all_the_rooms',
        'throughout_the_house', 'all_over_the_house', 'my_current_room',
    ]);

    // --- messages: client checks and the server's 422 types share one table ----------------------

    const MESSAGES = {
        room_blank: () => 'Enter a room name.',
        room_too_long: () => `Room names can be up to ${MAX_ROOM_NAME} characters.`,
        bad_room: () => `Use a room name of up to ${MAX_ROOM_NAME} characters, with at least one letter or number.`,
        reserved_room: () => 'That name is reserved. Choose a room name such as Living room.',
        duplicate_room: f => `${f.name ? f.name : 'That room'} is already listed (names are matched ignoring case and spaces).`,
        no_lights: () => 'Add at least one light to this room, or remove the room.',
        not_a_light: () => 'Only light entities can be added here (light.…).',
        light_unmatched: () => 'Pick a light from the list, or type its entity id (light.…).',
        light_ambiguous: () => 'More than one light has that name. Pick it from the list, or type its entity id (light.…).',
        duplicate_light: () => 'That light is already in this room.',
        too_many_lights: () => `A room can have up to ${MAX_LIGHTS} lights.`,
        too_many_rooms: () => `Up to ${MAX_ROOMS} rooms.`,
        too_many_entries: () => `Up to ${MAX_ROOMS} satellites.`,
        key_blank: () => 'Pick a satellite, or enter its device id.',
        bad_satellite_key: () => 'A device id is 32 letters and numbers (0-9, a-f).',
        satellite_unmatched: () => 'Pick a satellite from the list, or enter its device id.',
        satellite_ambiguous: () => 'More than one satellite has that name. Pick it from the list, or enter its device id.',
        duplicate_key: () => 'That satellite is already listed.',
        duplicate_satellite_room: () => 'Another satellite already uses this room.',
    };
    const SERVER_ALIASES = { too_many: 'too_many_rooms' };

    function messageFor(type, fields) {
        const entry = MESSAGES[SERVER_ALIASES[type] || type];
        return typeof entry === 'function' ? entry(fields || {}) : "This value wasn't accepted.";
    }

    // --- the room vocabulary the server uses (the server stays the authority) ---------------------

    function roomKey(name) {
        return String(name || '').normalize('NFKC').toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(Boolean).join('_');
    }

    function roomNameProblem(name) {
        const text = String(name || '').trim();
        if (!text) return 'room_blank';
        if (text.length > MAX_ROOM_NAME) return 'room_too_long';
        const key = roomKey(text);
        if (!key) return 'bad_room';
        if (RESERVED_ROOM_KEYS.has(key)) return 'reserved_room';
        return '';
    }

    // --- pickers: one cache per Home Assistant list, shared by every row ---------------------------

    function createPicker(path) {
        return { path, status: 'idle', items: [], at: 0, token: 0, listeners: [] };
    }
    const lightsPicker = createPicker('/api/settings/room-lights/candidates');
    const satellitesPicker = createPicker('/api/settings/speaker-rooms/satellites');

    function sanitizeItems(list, withGroup) {
        if (!Array.isArray(list)) return [];
        return list
            .filter(item => item && typeof item.entity_id === 'string' && item.entity_id)
            .map(item => ({
                entity_id: item.entity_id,
                friendly_name: typeof item.friendly_name === 'string' && item.friendly_name ? item.friendly_name : item.entity_id,
                group: withGroup ? item.group === true : false,
            }));
    }

    function notify(picker) { picker.listeners.slice().forEach(listener => listener()); }

    async function loadPicker(picker, withGroup) {
        const token = ++picker.token;
        picker.status = 'loading';
        notify(picker);
        try {
            const response = await global.fetch(`${global.API_BASE || ''}${picker.path}`, {
                headers: HS.authHeaders(),
            });
            if (token !== picker.token) return;
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const list = await response.json();
            if (token !== picker.token) return;
            picker.items = sanitizeItems(list, withGroup);
            picker.status = 'ok';
        } catch (error) {
            if (token !== picker.token) return;
            console.error(`Failed to load ${picker.path}:`, error);
            picker.items = [];
            picker.status = 'unavailable';
        }
        picker.at = Date.now();
        notify(picker);
    }

    // The pickers list every light and satellite, so the server gives them to the owner only
    // (like the saved values); anyone else never asks and sees no "not answering" notice.
    function mayLoadPickers() {
        return typeof global.hasPermission === 'function' && global.hasPermission(OWNER_PERMISSION) === true;
    }

    function refreshPicker(picker, withGroup, force) {
        if (!mayLoadPickers()) return;
        if (picker.status === 'loading' && !force) return;
        const fresh = picker.status === 'ok' && Date.now() - picker.at < PICKER_STALE_MS;
        if (fresh && !force) return;
        loadPicker(picker, withGroup);
    }

    // A saved id the loaded Home Assistant list lacks (only once the list loaded).
    function missingFromHomeAssistant(picker, id) {
        return picker.status === 'ok' && !picker.items.some(item => item.entity_id === id);
    }

    function findByIdOrName(items, text) {
        const lower = text.toLowerCase();
        const byId = items.find(item => item.entity_id === lower);
        if (byId) return { item: byId };
        const byName = items.filter(item => item.friendly_name.toLowerCase() === lower);
        if (byName.length === 1) return { item: byName[0] };
        if (byName.length > 1) return { ambiguous: true };
        return {};
    }

    function resolveLight(text) {
        const found = findByIdOrName(lightsPicker.items, text);
        if (found.item) return { id: found.item.entity_id };
        if (found.ambiguous) return { problem: 'light_ambiguous' };
        const lower = text.toLowerCase();
        if (lightsPicker.status === 'ok') return { problem: 'light_unmatched' };
        return LIGHT_ID_RE.test(lower) ? { id: lower } : { problem: 'not_a_light' };
    }

    function resolveSatellite(text) {
        const raw = String(text || '').trim();
        if (!raw) return { problem: 'key_blank' };
        const found = findByIdOrName(satellitesPicker.items, raw);
        if (found.item) return { key: found.item.entity_id };
        if (found.ambiguous) return { problem: 'satellite_ambiguous' };
        const compact = raw.toLowerCase().replace(/\s+/g, '');
        if (DEVICE_ID_RE.test(compact)) return { key: compact };
        if (SATELLITE_ID_RE.test(compact)) {
            return satellitesPicker.status === 'ok' ? { problem: 'satellite_unmatched' } : { key: compact };
        }
        const listed = satellitesPicker.status === 'ok' && satellitesPicker.items.length > 0;
        if (listed && !HEX_RE.test(compact)) return { problem: 'satellite_unmatched' };
        return { problem: 'bad_satellite_key' };
    }

    function friendlyNameOf(picker, id) {
        const item = picker.items.find(candidate => candidate.entity_id === id);
        return item ? item.friendly_name : '';
    }

    // --- room name suggestions: rooms already on this page first, then the common names ------------

    const roomSources = [];   // functions returning the room names a control currently holds

    function humanize(name) {
        const text = String(name).replace(/_/g, ' ').trim();
        return text ? text.charAt(0).toUpperCase() + text.slice(1) : '';
    }

    function roomSuggestions() {
        const seen = new Set();
        const out = [];
        const add = (name, used) => {
            const key = roomKey(name);
            if (!name || !key || seen.has(key)) return;
            seen.add(key);
            out.push({ name, used });
        };
        roomSources.forEach(source => source().forEach(name => add(name, true)));
        const common = typeof availableRooms !== 'undefined' && Array.isArray(availableRooms) ? availableRooms : [];
        common.forEach(name => { if (typeof name === 'string') add(humanize(name), false); });
        return out;
    }

    // --- small builders ----------------------------------------------------------------------------

    let serial = 0;
    const nextId = prefix => `${prefix}-${++serial}`;

    function mark(input, node) {
        return {
            show(text) {
                setLine(node, text);
                input.setAttribute('aria-invalid', text ? 'true' : 'false');
            },
            has() { return input.getAttribute('aria-invalid') === 'true'; },
        };
    }

    function textInput(id, errorId, listId, helpId) {
        return el('input', {
            type: 'text', id, autocomplete: 'off', spellcheck: 'false', list: listId,
            'aria-invalid': 'false', 'aria-describedby': helpId ? `${helpId} ${errorId}` : errorId, class: INPUT_CLASS,
        });
    }

    function plainButton(text, className, handler, disabled) {
        const button = el('button', { type: 'button', class: className, disabled: disabled === true }, text);
        button.addEventListener('click', handler);
        return button;
    }

    function fillRoomList(datalist) {
        clear(datalist);
        roomSuggestions().forEach(entry => {
            datalist.appendChild(el('option', { value: entry.name, label: entry.used ? 'Already used on this page' : 'Common room name' }));
        });
    }

    function fillPickerList(datalist, picker) {
        clear(datalist);
        picker.items.forEach(item => {
            datalist.appendChild(el('option', { value: item.entity_id, label: item.friendly_name }));
        });
    }

    // --- Room lights: one row per room ------------------------------------------------------------

    function lightsRow(values, env) {
        const n = nextId('rl');
        const nameId = `${n}-room`;
        const addId = `${n}-add`;
        const lights = values && Array.isArray(values.lights) ? values.lights.filter(id => typeof id === 'string') : [];
        let disabled = false;
        let chipButtons = [];

        const nameError = errorLine(`${nameId}-error`);
        const nameInput = textInput(nameId, nameError.getAttribute('id'), env.roomsListId);
        nameInput.value = values && typeof values.room === 'string' ? values.room : '';
        const nameMark = mark(nameInput, nameError);

        const addError = errorLine(`${addId}-error`);
        const addHelp = el('p', { id: `${addId}-help`, class: HELP_CLASS }, 'Choose from the list, or type a light name or entity id.');
        const addInput = textInput(addId, addError.getAttribute('id'), env.pickerListId, `${addId}-help`);
        const addMark = mark(addInput, addError);
        const chipList = el('ul', { class: 'flex flex-wrap gap-2 mb-2', 'aria-label': 'Lights in this room' });
        const groupHelp = el('p', { class: `${HIDDEN}${HELP_CLASS} mb-2` }, "A group answers as its bulbs. Guests reach only the bulbs they're allowed.");
        const removeRoom = plainButton('Remove room', `${LINK_BUTTON_CLASS} sm:mt-6`, () => env.removeRow(row));
        const addButton = plainButton('Add', BUTTON_CLASS, () => commitLight(), false);

        function roomLabel() { return nameInput.value.trim() || 'this room'; }

        function relabel() {
            removeRoom.setAttribute('aria-label', `Remove room ${roomLabel()}`);
            chipButtons.forEach((button, index) => {
                const id = lights[index];
                const name = friendlyNameOf(lightsPicker, id) || id;
                const gone = missingFromHomeAssistant(lightsPicker, id) ? ' (not in Home Assistant)' : '';
                button.setAttribute('aria-label', `Remove ${name}${gone} from ${roomLabel()}`);
            });
        }

        function renderChips() {
            clear(chipList);
            chipButtons = [];
            let anyGroup = false;
            lights.forEach(id => {
                const item = lightsPicker.items.find(candidate => candidate.entity_id === id);
                const remove = el('button', {
                    type: 'button', disabled: disabled,
                    class: 'min-h-[44px] min-w-[44px] px-3 text-sm text-blue-400 hover:text-blue-300 underline disabled:opacity-50',
                }, 'Remove');
                remove.addEventListener('click', () => removeLight(id));
                chipButtons.push(remove);
                if (item && item.group) anyGroup = true;
                chipList.appendChild(el('li', { class: 'flex flex-wrap items-center gap-x-2 max-w-full min-w-0 bg-dark-bg border border-dark-border rounded-lg pl-3' },
                    el('span', { class: 'text-sm text-white break-words min-w-0' }, item ? item.friendly_name : id),
                    item && item.friendly_name !== id ? el('span', { class: 'text-xs text-gray-500 break-all w-full sm:w-auto' }, id) : null,
                    item && item.group ? el('span', { class: 'text-xs text-yellow-400' }, 'group') : null,
                    missingFromHomeAssistant(lightsPicker, id) ? el('span', { class: 'text-xs text-gray-400 italic' }, 'Not in Home Assistant') : null,
                    remove));
            });
            groupHelp.className = `${anyGroup ? '' : HIDDEN}${HELP_CLASS} mb-2`;
            addButton.disabled = disabled || lights.length >= MAX_LIGHTS;
            relabel();
        }

        function removeLight(id) {
            const index = lights.indexOf(id);
            if (index < 0) return;
            const name = friendlyNameOf(lightsPicker, id) || id;
            lights.splice(index, 1);
            renderChips();
            env.changed();
            env.announce(`${name} removed from ${roomLabel()}`);
            const target = chipButtons[index] || chipButtons[index - 1] || addInput;
            target.focus();
        }

        function commitLight() {
            const text = addInput.value.trim();
            if (!text) { addMark.show(''); return true; }
            const result = resolveLight(text);
            let problem = result.problem || '';
            if (!problem && lights.includes(result.id)) problem = 'duplicate_light';
            if (!problem && lights.length >= MAX_LIGHTS) problem = 'too_many_lights';
            if (problem) { addMark.show(messageFor(problem)); return false; }
            lights.push(result.id);
            addInput.value = '';
            addMark.show('');
            renderChips();
            env.changed();
            env.announce(`${friendlyNameOf(lightsPicker, result.id) || result.id} added to ${roomLabel()}`);
            addInput.focus();
            return true;
        }

        function checkName() {
            const problem = roomNameProblem(nameInput.value);
            nameMark.show(problem ? messageFor(problem) : '');
            return !problem;
        }

        nameInput.addEventListener('input', () => { nameMark.show(''); relabel(); env.changed(); });
        nameInput.addEventListener('focus', () => env.refreshRooms());
        nameInput.addEventListener('blur', () => { nameInput.value = nameInput.value.trim(); if (checkName()) env.nameSettled(row); });
        addInput.addEventListener('input', () => addMark.show(''));
        addInput.addEventListener('change', () => commitLight());
        addInput.addEventListener('keydown', event => {
            if (event.key === 'Enter') { event.preventDefault(); commitLight(); }
        });

        const row = {
            el: el('li', { class: 'mb-4 border border-dark-border rounded-lg p-3', 'data-room-row': 'lights' },
                el('div', { class: 'flex flex-wrap items-start justify-between gap-x-3 mb-3' },
                    el('div', { class: 'flex-1 min-w-0 basis-full sm:basis-auto sm:max-w-sm' },
                        el('label', { for: nameId, class: LABEL_CLASS }, 'Room name'),
                        nameInput, nameError),
                    removeRoom),
                el('p', { class: LABEL_CLASS }, 'Lights'),
                chipList, groupHelp,
                el('div', { class: 'sm:max-w-xl' },
                    el('label', { for: addId, class: LABEL_CLASS }, 'Add light'),
                    el('div', { class: 'flex flex-col sm:flex-row gap-2' }, addInput, addButton),
                    addHelp, addError)),
            roomName() { return nameInput.value.trim(); },
            read() { return { room: nameInput.value.trim(), lights: lights.slice() }; },
            validate() {
                let ok = checkName();
                const pending = addInput.value.trim();
                if (pending && !commitLight()) ok = false;
                if (lights.length === 0) { addMark.show(messageFor('no_lights')); ok = false; }
                return ok;
            },
            setNameError(type, name) { nameMark.show(type ? messageFor(type, { name }) : ''); },
            showServerError(rest, type) {
                if (rest[0] === 'lights') {
                    addMark.show(messageFor(rest.length === 1 && type === 'too_many' ? 'too_many_lights' : type));
                } else {
                    nameMark.show(messageFor(type, { name: nameInput.value.trim() }));
                }
            },
            hasError() { return nameMark.has() || addMark.has(); },
            focusFirst() { nameInput.focus(); },
            focusInvalid() { (nameMark.has() ? nameInput : addInput).focus(); },
            setDisabled(value) {
                disabled = value;
                nameInput.disabled = value;
                addInput.disabled = value;
                removeRoom.disabled = value;
                renderChips();
            },
            refreshPicker() { renderChips(); },
        };
        renderChips();
        return row;
    }

    // --- Speaker rooms: one row per satellite ------------------------------------------------------

    function speakerRow(values, env) {
        const n = nextId('sr');
        const keyId = `${n}-key`;
        const roomId = `${n}-room`;
        let disabled = false;

        const keyError = errorLine(`${keyId}-error`);
        const keyHelpId = `${keyId}-help`;
        const keyInput = textInput(keyId, keyError.getAttribute('id'), env.pickerListId, keyHelpId);
        keyInput.value = values && typeof values.key === 'string' ? values.key : '';
        const keyMark = mark(keyInput, keyError);
        const caption = el('p', { class: `${HIDDEN}${HELP_CLASS}` });

        const roomError = errorLine(`${roomId}-error`);
        const roomInput = textInput(roomId, roomError.getAttribute('id'), env.roomsListId);
        roomInput.value = values && typeof values.room === 'string' ? values.room : '';
        const roomMark = mark(roomInput, roomError);

        const remove = plainButton('Remove', SECONDARY_BUTTON_CLASS, () => env.removeRow(row));

        function relabel() {
            const key = keyInput.value.trim() || 'new satellite';
            const name = friendlyNameOf(satellitesPicker, key.toLowerCase());
            remove.setAttribute('aria-label', `Remove satellite ${name || key}`);
        }

        function showCaption() {
            const resolved = resolveSatellite(keyInput.value);
            const name = resolved.key ? friendlyNameOf(satellitesPicker, resolved.key) : '';
            const typed = keyInput.value.trim().toLowerCase();
            const gone = !name && SATELLITE_ID_RE.test(typed) && missingFromHomeAssistant(satellitesPicker, typed);
            const text = name ? `Home Assistant name: ${name}` : (gone ? 'Not found in Home Assistant' : '');
            caption.textContent = text;
            caption.className = `${text ? '' : HIDDEN}${HELP_CLASS}`;
        }

        function checkKey() {
            const resolved = resolveSatellite(keyInput.value);
            keyMark.show(resolved.problem ? messageFor(resolved.problem) : '');
            if (resolved.key) keyInput.value = resolved.key;
            showCaption();
            relabel();
            return !resolved.problem;
        }

        function checkRoom() {
            const problem = roomNameProblem(roomInput.value);
            roomMark.show(problem ? messageFor(problem) : '');
            return !problem;
        }

        keyInput.addEventListener('input', () => { keyMark.show(''); caption.className = `${HIDDEN}${HELP_CLASS}`; relabel(); env.changed(); });
        keyInput.addEventListener('blur', () => { if (keyInput.value.trim()) { if (checkKey()) env.nameSettled(row); } });
        roomInput.addEventListener('input', () => { roomMark.show(''); env.changed(); });
        roomInput.addEventListener('focus', () => env.refreshRooms());
        roomInput.addEventListener('blur', () => { roomInput.value = roomInput.value.trim(); if (checkRoom()) env.nameSettled(row); });

        const row = {
            el: el('li', { class: 'mb-4 border border-dark-border rounded-lg p-3', 'data-room-row': 'speakers' },
                el('div', { class: 'mb-3' },
                    el('label', { for: keyId, class: LABEL_CLASS }, 'Voice satellite or device id'),
                    keyInput,
                    el('p', { id: keyHelpId, class: HELP_CLASS }, 'Choose a satellite from the list, or enter its device id.'),
                    caption, keyError),
                el('div', { class: 'mb-3' },
                    el('label', { for: roomId, class: LABEL_CLASS }, 'Room'),
                    roomInput, roomError),
                remove),
            roomName() { return roomInput.value.trim(); },
            key() {
                const resolved = resolveSatellite(keyInput.value);
                return resolved.key || '';
            },
            read() {
                const resolved = resolveSatellite(keyInput.value);
                return { key: resolved.key || keyInput.value.trim().toLowerCase().replace(/\s+/g, ''), room: roomInput.value.trim() };
            },
            validate() {
                const keyOk = checkKey();
                const roomOk = checkRoom();
                return keyOk && roomOk;
            },
            setKeyError(type) { keyMark.show(type ? messageFor(type) : ''); },
            setRoomError(type) { roomMark.show(type ? messageFor(type) : ''); },
            showServerError(rest, type) {
                if (rest[0] === 'room') roomMark.show(messageFor(type));
                else keyMark.show(messageFor(type));
            },
            hasError() { return keyMark.has() || roomMark.has(); },
            focusFirst() { keyInput.focus(); },
            focusInvalid() { (keyMark.has() ? keyInput : roomInput).focus(); },
            setDisabled(value) {
                disabled = value;
                keyInput.disabled = value;
                roomInput.disabled = value;
                remove.disabled = value;
            },
            refreshPicker() { if (keyInput.value.trim()) showCaption(); },
        };
        relabel();
        showCaption();
        return row;
    }

    // --- cross-row rules ---------------------------------------------------------------------------

    function checkDuplicateRooms(rows) {
        const seen = new Map();
        let ok = true;
        rows.forEach(row => {
            const name = row.roomName();
            if (roomNameProblem(name)) return;
            const key = roomKey(name);
            if (seen.has(key)) { row.setNameError('duplicate_room', seen.get(key)); ok = false; }
            else seen.set(key, name);
        });
        return ok;
    }

    function checkSpeakerRows(rows) {
        const keys = new Set();
        const rooms = new Set();
        let ok = true;
        rows.forEach(row => {
            const key = row.key();
            if (!key) return;
            if (keys.has(key)) { row.setKeyError('duplicate_key'); ok = false; }
            keys.add(key);
            if (SATELLITE_ID_RE.test(key) && !roomNameProblem(row.roomName())) {
                const room = roomKey(row.roomName());
                if (rooms.has(room)) { row.setRoomError('duplicate_satellite_room'); ok = false; }
                rooms.add(room);
            }
        });
        return ok;
    }

    // --- the list control: three visible states, rows -----------------------------------------------

    const ROW_TYPES = {
        room_lights: {
            wire: 'rooms', noun: 'room', plural: 'rooms', addLabel: 'Add a room', maxRows: MAX_ROOMS,
            picker: lightsPicker, withGroup: true, make: lightsRow, cross: checkDuplicateRooms,
            unsetText: "Not set here. Athena uses the server's setting if there is one, otherwise your Home Assistant areas.",
            emptyText: "None. Athena ignores the server's setting and uses only your Home Assistant areas.",
            pickerNotice: "Home Assistant isn't answering, so the list of lights can't be shown. You can still type entity ids.",
            help: null,
            listLabel: 'Rooms',
        },
        speaker_rooms: {
            wire: 'entries', noun: 'satellite', plural: 'satellites', addLabel: 'Add a satellite', maxRows: MAX_ROOMS,
            picker: satellitesPicker, withGroup: false, make: speakerRow, cross: checkSpeakerRows,
            unsetText: "Not set here. Athena uses the server's setting if there is one, otherwise the Home Assistant area of each voice satellite.",
            emptyText: 'None. Athena ignores the server\'s setting and uses only the Home Assistant area of each voice satellite.',
            pickerNotice: "Home Assistant isn't answering, so the list of voice satellites can't be shown. You can still enter a device id.",
            help: "Picking a satellite works when Athena can read your Home Assistant areas. If it can't, use the device id instead.",
            listLabel: 'Voice satellites',
        },
    };

    function roomsControl(kind) {
        const type = ROW_TYPES[kind];
        return function build(field, ctx) {
            const id = `rs-${ctx.section}-${type.wire}`;
            const roomsListId = `${id}-room-names`;
            const pickerListId = `${id}-picker`;
            const roomsList = el('datalist', { id: roomsListId });
            const pickerList = el('datalist', { id: pickerListId });
            const root = el('div', { class: 'mb-4', id });
            const notice = el('p', { class: `${HIDDEN}text-sm text-yellow-400 mb-2`, role: 'status' });
            const listError = errorLine(`${id}-error`);
            let state = 'unset';
            let rows = [];
            let disabled = false;
            let listFlag = false;
            let primary = null;

            const env = {
                roomsListId, pickerListId,
                changed: () => ctx.changed(),
                announce: text => ctx.announce(text),
                refreshRooms: () => fillRoomList(roomsList),
                nameSettled: () => type.cross(rows),
                removeRow(row) { removeRow(row); },
            };

            roomSources.push(() => rows.map(row => row.roomName()).filter(Boolean));
            type.picker.listeners.push(onPicker);
            refreshPicker(type.picker, type.withGroup, false);

            function onPicker() {
                fillPickerList(pickerList, type.picker);
                const down = type.picker.status === 'unavailable';
                notice.textContent = down ? type.pickerNotice : '';
                notice.className = `${down ? '' : HIDDEN}text-sm text-yellow-400 mb-2`;
                rows.forEach(row => row.refreshPicker());
            }

            function setListError(text) {
                setLine(listError, text);
                listFlag = Boolean(text);
            }

            function enterRows(list) {
                state = 'rows';
                rows = list;
                render();
            }

            function addRow() {
                const row = type.make(null, env);
                rows.push(row);
                return row;
            }

            function removeRow(row) {
                const index = rows.indexOf(row);
                if (index < 0) return;
                rows.splice(index, 1);
                if (rows.length === 0) state = 'empty';
                setListError('');
                render();
                ctx.changed();
                const removed = `${type.noun.charAt(0).toUpperCase()}${type.noun.slice(1)} removed`;
                ctx.announce(rows.length === 0 ? `${removed}. ${EMPTY_STATE_ANNOUNCEMENT}` : removed);
                const target = rows[index] || rows[index - 1];
                if (target) target.focusFirst(); else firstButton().focus();
            }

            function useServer() {
                state = 'unset';
                rows = [];
                setListError('');
                render();
                ctx.changed();
                ctx.announce("Using the server's setting");
            }

            function addFirst() {
                state = 'rows';
                rows = [];
                const row = addRow();
                render();
                ctx.changed();
                row.focusFirst();
            }

            function firstButton() { return primary || root; }

            function render() {
                clear(root);
                root.appendChild(notice);
                root.appendChild(pickerList);
                root.appendChild(roomsList);
                if (state === 'unset') {
                    root.appendChild(el('p', { class: 'text-sm text-gray-400 mb-2' }, type.unsetText));
                    primary = plainButton(type.addLabel, BUTTON_CLASS, addFirst, disabled);
                    root.appendChild(primary);
                } else if (state === 'empty') {
                    root.appendChild(el('p', { class: 'text-sm text-gray-300 mb-2' }, type.emptyText));
                    primary = plainButton(type.addLabel, BUTTON_CLASS, addFirst, disabled);
                    root.appendChild(primary);
                    root.appendChild(plainButton("Use the server's setting", LINK_BUTTON_CLASS, useServer, disabled));
                } else {
                    const list = el('ul', { 'aria-label': type.listLabel });
                    rows.forEach(row => list.appendChild(row.el));
                    root.appendChild(list);
                    const full = rows.length >= type.maxRows;
                    primary = plainButton(type.addLabel, BUTTON_CLASS, () => {
                        const row = addRow();
                        render();
                        ctx.changed();
                        ctx.announce(`${type.noun.charAt(0).toUpperCase()}${type.noun.slice(1)} ${rows.length} added`);
                        row.focusFirst();
                    }, disabled || full);
                    root.appendChild(primary);
                    if (full) root.appendChild(el('p', { class: HELP_CLASS }, `You can add up to ${type.maxRows} ${type.plural}.`));
                    root.appendChild(plainButton("Use the server's setting", LINK_BUTTON_CLASS, useServer, disabled));
                }
                root.appendChild(listError);
                if (type.help) root.appendChild(el('p', { class: 'text-xs text-gray-500 mt-2' }, type.help));
                fillRoomList(roomsList);
                fillPickerList(pickerList, type.picker);
                rows.forEach(row => row.setDisabled(disabled));
            }

            render();
            onPicker();

            return {
                el: root,
                fields: [type.wire],
                get() {
                    if (state === 'unset') return { [type.wire]: null };
                    if (state === 'empty') return { [type.wire]: [] };
                    return { [type.wire]: rows.map(row => row.read()) };
                },
                set(value) {
                    const list = value ? value[type.wire] : null;
                    setListError('');
                    if (list === null || list === undefined) { state = 'unset'; rows = []; }
                    else if (!Array.isArray(list) || list.length === 0) { state = 'empty'; rows = []; }
                    else {
                        state = 'rows';
                        rows = list.filter(item => item && typeof item === 'object').map(item => type.make(item, env));
                        if (rows.length === 0) state = 'empty';
                    }
                    render();
                },
                validate() {
                    setListError('');
                    let ok = true;
                    rows.forEach(row => { if (!row.validate()) ok = false; });
                    if (!type.cross(rows)) ok = false;
                    return ok;
                },
                showServerError(loc, errorType) {
                    const row = typeof loc[1] === 'number' ? rows[loc[1]] : null;
                    if (row) row.showServerError(loc.slice(2), errorType);
                    else setListError(messageFor(errorType === 'too_many' && kind === 'speaker_rooms' ? 'too_many_entries' : errorType));
                },
                hasError() { return listFlag || rows.some(row => row.hasError()); },
                focus() {
                    const bad = rows.find(row => row.hasError());
                    if (bad) bad.focusInvalid();
                    else if (rows.length > 0) rows[0].focusFirst();
                    else firstButton().focus();
                },
                setDisabled(value) { if (value === disabled) return; disabled = value; render(); },
                refresh() {},
            };
        };
    }

    HS.registerKind('room_lights', roomsControl('room_lights'));
    HS.registerKind('speaker_rooms', roomsControl('speaker_rooms'));

    // --- cards -------------------------------------------------------------------------------------

    HS.defineCard('room_lights', {
        title: 'Room lights',
        blurb: 'Which lights answer when you ask for a room.',
        intro: ["Usually not needed. Athena finds each room's lights from your Home Assistant areas. Add a room here only when its area is missing or includes lights that shouldn't answer. A room listed here uses only these lights."],
        fields: [{ name: 'rooms', kind: 'room_lights', label: 'Rooms' }],
    });

    HS.defineCard('speaker_rooms', {
        title: 'Speaker rooms',
        blurb: 'Which room each voice satellite is in.',
        intro: ['Usually not needed. Athena uses the Home Assistant area of each voice satellite. Add one here when a satellite has no area, or should count as a different room.'],
        fields: [{ name: 'entries', kind: 'speaker_rooms', label: 'Voice satellites' }],
    });

    // --- public API --------------------------------------------------------------------------------

    // Idempotent: a clean card re-fetches, a card with unsaved edits keeps them. A Refresh or a
    // return to the tab also reloads a Home Assistant list that has gone stale or failed.
    function mountAll() {
        refreshPicker(lightsPicker, true, false);
        refreshPicker(satellitesPicker, false, false);
        return Promise.all([
            HS.mount('room_lights', LIGHTS_CONTAINER),
            HS.mount('speaker_rooms', SPEAKERS_CONTAINER),
        ]);
    }

    global.RoomSettings = { mountAll, roomKey };
})(window);
