const { createApp, ref, computed, nextTick, onMounted } = Vue;

const API = '';

createApp({
    setup() {
        const view = ref('list');
        const trips = ref([]);
        const currentTrip = ref(null);
        const messages = ref([]);
        const chatInput = ref('');
        const sending = ref(false);
        const creating = ref(false);
        // Spec 006 FR-017a: activity_weights with a default single-row "golf 100%"
        const activityTagVocab = ['golf', 'hiking', 'beach', 'city', 'culture', 'relaxation', 'food', 'nature', 'wellness', 'adventure'];
        const newTrip = ref({
            name: '', description: '',
            activities: [{ tag: 'golf', pct: 100 }],
        });
        const activityWeightsTotal = computed(() =>
            newTrip.value.activities.reduce((sum, a) => sum + (a.pct || 0), 0)
        );
        function addActivityRow() {
            const used = new Set(newTrip.value.activities.map(a => a.tag));
            const next = activityTagVocab.find(t => !used.has(t)) || 'hiking';
            newTrip.value.activities.push({ tag: next, pct: 0 });
        }
        const messagesContainer = ref(null);
        const chatInputEl = ref(null);
        const editingMessageId = ref(null);
        const editingContent = ref('');
        const zoomedMessage = ref(null);
        const zoomQuestions = ref([]);
        const chatExpanded = ref(false);
        const activeConvId = ref(null);
        const showArchivedConvs = ref(false);
        const editingDescription = ref(false);
        const editDescValue = ref('');
        const descEditEl = ref(null);
        const expandedReasoning = ref(new Set());

        // --- Spec 006 — Golf Library ---
        const library = ref({ loading: false, resorts: [], courses: [] });
        const libAdd = ref(_emptyLibAdd());
        const libBrowse = ref(_emptyLibBrowse());
        const libDetail = ref({
            resort: null, course: null, carouselIdx: 0,
            editing: null, edit: {},
            deleteBlocked: null,
            regionSearch: '', regionResults: [],
            resortSearch: '', resortResults: [],
            newImageUrl: '',
        });

        let _regionSearchTimer = null;
        let _resortSearchTimer = null;

        function _emptyLibBrowse() {
            return {
                mode: 'resorts',
                loading: false,
                total: 0,
                results: [],
                q: '',
                country: '',
                sort: 'rank_rating',
                sort_dir: 'desc',
                limit: 50,
                offset: 0,
                filters: {
                    // Resort filters
                    price_category: [],
                    hotel_type: [],
                    month: null,
                    // Course filters
                    course_type: [],
                    min_difficulty: null,
                    max_difficulty: null,
                    min_holes: null,
                    parent_resort: 'any',
                    max_green_fee_eur: null,
                    // Shared
                    region_match: 'any',
                },
            };
        }

        let _browseTimer = null;

        function _emptyLibAdd() {
            return {
                entityType: 'resort',
                url: '',
                name: '',
                extracting: false,
                extracted: false,
                manual: false,
                saving: false,
                form: _emptyForm('resort'),
                bestMonthsText: '',
                tagsText: '',
                imageCandidates: [],
                sources: [],
                warnings: [],
                possibleParent: null,
                error: null,
                duplicate: null,
            };
        }

        function _emptyForm(entityType) {
            const base = {
                name: '', country_code: '', url: null,
                region_name_raw: null, vacationmap_region_key: null,
                town: null, latitude: null, longitude: null,
                description: null, personal_notes: null, rank_rating: null,
            };
            if (entityType === 'resort') {
                return {
                    ...base,
                    hotel_name: null, hotel_type: null, star_rating: null,
                    price_category: null,
                };
            }
            return {
                ...base, resort_id: null,
                holes: null, par: null, length_yards: null, type: null,
                architect: null, year_opened: null, difficulty: null,
                signature_holes: null, green_fee_low_eur: null,
                green_fee_high_eur: null, green_fee_notes: null,
            };
        }

        const activeConversations = computed(() => {
            const convs = (currentTrip.value?.conversations || []).filter(c => c.status !== 'archived');
            // "Main" always first
            return convs.sort((a, b) => {
                if (a.name === 'Main') return -1;
                if (b.name === 'Main') return 1;
                return a.created_at < b.created_at ? -1 : 1;
            });
        });
        const archivedConversations = computed(() =>
            (currentTrip.value?.conversations || []).filter(c => c.status === 'archived')
        );

        // --- API helpers ---
        async function api(path, opts = {}) {
            const res = await fetch(`${API}${path}`, {
                headers: { 'Content-Type': 'application/json' },
                ...opts,
            });
            if (res.status === 204) return null;
            if (!res.ok) {
                const err = await res.json().catch(() => ({ detail: 'Request failed' }));
                throw new Error(err.detail || 'Request failed');
            }
            return res.json();
        }

        // --- Trip list ---
        async function loadTrips() {
            trips.value = await api('/api/trips');
        }

        function goHome() {
            view.value = 'list';
            currentTrip.value = null;
            messages.value = [];
            window.location.hash = '';
            loadTrips();
        }

        // Map the current view to a top-level section, used for the active
        // tab highlight in the header nav.
        const currentSection = computed(() => {
            const v = view.value;
            if (v === 'year' || v === 'year-detail') return 'year';
            if (v && v.startsWith('library')) return 'golf';
            return 'trips';
        });

        // --- Create trip ---
        async function createTrip() {
            creating.value = true;
            try {
                // Build the base trip (legacy endpoint only accepts name + description).
                const trip = await api('/api/trips', {
                    method: 'POST',
                    body: JSON.stringify({
                        name: newTrip.value.name,
                        description: newTrip.value.description,
                    }),
                });
                // Spec 006: attach activity_weights via PUT /trips/{id}.
                const weights = {};
                for (const row of newTrip.value.activities) {
                    if (row.tag && row.pct > 0) weights[row.tag] = row.pct;
                }
                if (Object.keys(weights).length) {
                    await api(`/api/trips/${trip.id}`, {
                        method: 'PUT',
                        body: JSON.stringify({ activity_weights: weights }),
                    });
                }
                const desc = newTrip.value.description;
                newTrip.value = {
                    name: '', description: '',
                    activities: [{ tag: 'golf', pct: 100 }],
                };
                await openTrip(trip.id, desc);
            } finally {
                creating.value = false;
            }
        }

        // --- Open trip ---
        async function openTrip(tripId, autoMessage) {
            const trip = await api(`/api/trips/${tripId}`);
            currentTrip.value = trip;
            editingDescription.value = false;

            // Pick the last conversation, or create one if none exist
            let convId;
            if (trip.conversations.length > 0) {
                convId = trip.conversations[trip.conversations.length - 1].id;
            } else {
                const conv = await api(`/api/trips/${tripId}/conversations`, {
                    method: 'POST',
                    body: JSON.stringify({ name: 'Main' }),
                });
                convId = conv.id;
                // Refresh trip to include the new conversation
                currentTrip.value = await api(`/api/trips/${tripId}`);
            }

            activeConvId.value = convId;
            messages.value = await api(`/api/conversations/${convId}/messages`);
            view.value = 'planning';
            window.location.hash = `trip/${tripId}`;
            await nextTick();
            scrollToBottom();

            // Auto-send the trip description as first message for new trips
            if (autoMessage && messages.value.length === 0) {
                await doSend(autoMessage);
            }
        }

        // --- Chat ---
        async function sendMessage() {
            const content = chatInput.value.trim();
            if (!content || sending.value) return;
            chatInput.value = '';
            await doSend(content);
        }

        async function doSend(content) {
            sending.value = true;

            messages.value.push({
                id: Date.now(),
                role: 'user',
                content: content,
                created_at: new Date().toISOString(),
            });
            await nextTick();
            scrollToBottom();

            try {
                const res = await api(`/api/conversations/${activeConvId.value}/messages`, {
                    method: 'POST',
                    body: JSON.stringify({ content }),
                });

                messages.value[messages.value.length - 1] = res.user_message;
                messages.value.push(res.assistant_message);

                // Always refresh trip state to pick up suggested/shortlisted/excluded changes
                if (res.trip_state_changed) {
                    currentTrip.value = await api(`/api/trips/${currentTrip.value.id}`);
                }
            } catch (err) {
                messages.value.push({
                    id: Date.now() + 1,
                    role: 'assistant',
                    content: `Error: ${err.message}. Please try again.`,
                    created_at: new Date().toISOString(),
                });
            } finally {
                sending.value = false;
                await nextTick();
                scrollToBottom();
                if (chatInputEl.value) chatInputEl.value.focus();
            }
        }

        // --- Quick actions ---
        function quickAction(msg) {
            if (sending.value) return;
            doSend(msg);
        }

        function promptChangeFocus() {
            const focus = prompt('What would you like to change? (e.g., "switch to hiking focus" or "look at October instead")');
            if (focus) doSend(`Change of plans: ${focus}`);
        }

        // --- Suggested destination actions ---
        async function shortlistSuggested(dest) {
            const note = prompt('Add a note (optional):', '');
            await api(`/api/trips/${currentTrip.value.id}/suggested/${dest.id}/shortlist`, {
                method: 'POST',
                body: JSON.stringify({ user_note: note || null }),
            });
            currentTrip.value = await api(`/api/trips/${currentTrip.value.id}`);
        }

        async function excludeSuggested(dest) {
            const prefill = dest.pre_filled_exclude_reason || '';
            const reason = prompt('Why exclude this destination?', prefill);
            if (reason === null) return; // cancelled
            await api(`/api/trips/${currentTrip.value.id}/suggested/${dest.id}/exclude`, {
                method: 'POST',
                body: JSON.stringify({ reason: reason || 'Not interested' }),
            });
            currentTrip.value = await api(`/api/trips/${currentTrip.value.id}`);
        }

        // --- Trip description editing ---
        async function startEditDescription() {
            editDescValue.value = currentTrip.value.description;
            editingDescription.value = true;
            await nextTick();
            if (descEditEl.value) descEditEl.value.focus();
        }

        async function saveDescription() {
            const desc = editDescValue.value.trim();
            if (!desc) return;
            await api(`/api/trips/${currentTrip.value.id}`, {
                method: 'PUT',
                body: JSON.stringify({ description: desc }),
            });
            currentTrip.value.description = desc;
            editingDescription.value = false;
        }

        // --- Conversation management ---
        async function switchConversation(convId) {
            activeConvId.value = convId;
            messages.value = await api(`/api/conversations/${convId}/messages`);
            await nextTick();
            scrollToBottom();
        }

        async function archiveConversation(convId) {
            if (!confirm('Archive this conversation?')) return;
            await api(`/api/conversations/${convId}/archive`, { method: 'POST' });
            currentTrip.value = await api(`/api/trips/${currentTrip.value.id}`);
            if (convId === activeConvId.value) {
                const active = activeConversations.value;
                if (active.length) {
                    await switchConversation(active[active.length - 1].id);
                } else {
                    messages.value = [];
                    activeConvId.value = null;
                }
            }
        }

        async function unarchiveConversation(convId) {
            await api(`/api/conversations/${convId}/unarchive`, { method: 'POST' });
            currentTrip.value = await api(`/api/trips/${currentTrip.value.id}`);
        }

        async function deleteConversation(convId, convName) {
            if (!confirm(`Permanently delete "${convName}" and all its messages? This cannot be undone.`)) return;
            await api(`/api/conversations/${convId}`, { method: 'DELETE' });
            currentTrip.value = await api(`/api/trips/${currentTrip.value.id}`);
            if (convId === activeConvId.value) {
                const active = activeConversations.value;
                if (active.length) {
                    await switchConversation(active[active.length - 1].id);
                } else {
                    messages.value = [];
                    activeConvId.value = null;
                }
            }
        }

        async function renameConversation(convId) {
            const conv = (currentTrip.value?.conversations || []).find(c => c.id === convId);
            const name = prompt('Rename conversation:', conv?.name || '');
            if (!name || name === conv?.name) return;
            await api(`/api/conversations/${convId}/rename`, {
                method: 'PUT',
                body: JSON.stringify({ name }),
            });
            currentTrip.value = await api(`/api/trips/${currentTrip.value.id}`);
        }

        async function newConversation() {
            const name = prompt('Conversation name:', 'Follow-up');
            if (!name) return;
            const conv = await api(`/api/trips/${currentTrip.value.id}/conversations`, {
                method: 'POST',
                body: JSON.stringify({ name }),
            });
            activeConvId.value = conv.id;
            messages.value = [];
            // Refresh trip to include new conversation in tabs
            currentTrip.value = await api(`/api/trips/${currentTrip.value.id}`);
            await nextTick();
            if (chatInputEl.value) chatInputEl.value.focus();
        }

        // --- Destination detail popup ---
        const destDetail = ref(null);
        const destDetailLoading = ref(false);

        async function showDestDetail(dest) {
            if (!dest.region_lookup_key) {
                // No VacationMap link — show what we have from scores_snapshot
                destDetail.value = {
                    destination: dest.destination_name,
                    ...(dest.scores_snapshot || {}),
                    _noLink: true,
                };
                return;
            }
            destDetail.value = { destination: dest.destination_name };
            destDetailLoading.value = true;
            try {
                const month = currentTrip.value?.target_month || 'jun';
                const data = await api(`/api/vacationmap/regions/${encodeURIComponent(dest.region_lookup_key)}/details?month=${month}`);
                destDetail.value = data;
            } catch (e) {
                destDetail.value = {
                    destination: dest.destination_name,
                    ...(dest.scores_snapshot || {}),
                    _error: e.message,
                };
            } finally {
                destDetailLoading.value = false;
            }
        }

        function fmt(val, suffix) {
            if (val == null) return '—';
            const n = typeof val === 'number' ? val.toFixed(1) : val;
            return suffix ? `${n}${suffix}` : n;
        }

        // --- Region linking ---
        const linkingDest = ref(null); // { section: 'suggested'|'shortlisted', dest }
        const linkSearch = ref('');
        const linkResults = ref([]);
        const linkSearchEl = ref(null);
        let linkSearchTimer = null;

        async function startLinking(section, dest) {
            linkingDest.value = { section, dest };
            linkSearch.value = dest.destination_name.split(',')[0].trim();
            linkResults.value = [];
            await nextTick();
            if (linkSearchEl.value) linkSearchEl.value.focus();
            searchRegions();
        }

        function searchRegions() {
            clearTimeout(linkSearchTimer);
            if (linkSearch.value.length < 2) { linkResults.value = []; return; }
            linkSearchTimer = setTimeout(async () => {
                linkResults.value = await api(`/api/vacationmap/regions/search?q=${encodeURIComponent(linkSearch.value)}`);
            }, 200);
        }

        async function confirmLink(region) {
            const { section, dest } = linkingDest.value;
            await api(`/api/trips/${currentTrip.value.id}/${section}/${dest.id}/link`, {
                method: 'POST',
                body: JSON.stringify({ lookup_key: region.lookup_key }),
            });
            linkingDest.value = null;
            currentTrip.value = await api(`/api/trips/${currentTrip.value.id}`);
        }

        // --- Inline note editing ---
        async function editNote(section, dest) {
            const current = dest.user_note || '';
            const note = prompt('Note:', current);
            if (note === null) return; // cancelled
            await api(`/api/trips/${currentTrip.value.id}/${section}/${dest.id}/note`, {
                method: 'PUT',
                body: JSON.stringify({ user_note: note || null }),
            });
            dest.user_note = note || null;
        }

        // --- Shortlist actions ---
        async function excludeShortlisted(dest) {
            const reason = prompt('Why exclude this destination?', '');
            if (reason === null) return;
            await api(`/api/trips/${currentTrip.value.id}/shortlisted/${dest.id}/exclude`, {
                method: 'POST',
                body: JSON.stringify({ reason: reason || 'Reconsidered' }),
            });
            currentTrip.value = await api(`/api/trips/${currentTrip.value.id}`);
        }

        async function unreviewShortlisted(dest) {
            await api(`/api/trips/${currentTrip.value.id}/shortlisted/${dest.id}/unreview`, {
                method: 'POST',
            });
            currentTrip.value = await api(`/api/trips/${currentTrip.value.id}`);
        }

        // --- Reconsider excluded ---
        async function reconsiderExcluded(dest) {
            const note = prompt('Add a note (optional):', '');
            await api(`/api/trips/${currentTrip.value.id}/excluded/${dest.id}/reconsider`, {
                method: 'POST',
                body: JSON.stringify({ user_note: note || null }),
            });
            currentTrip.value = await api(`/api/trips/${currentTrip.value.id}`);
        }

        // --- Message edit/delete ---
        function startEditMessage(msg) {
            editingMessageId.value = msg.id;
            editingContent.value = msg.content;
        }

        async function saveMessageEdit(msg) {
            await api(`/api/messages/${msg.id}`, {
                method: 'PUT',
                body: JSON.stringify({ content: editingContent.value }),
            });
            msg.content = editingContent.value;
            editingMessageId.value = null;
        }

        async function deleteMessage(msg, idx) {
            if (!confirm('Delete this message?')) return;
            await api(`/api/messages/${msg.id}`, { method: 'DELETE' });
            messages.value.splice(idx, 1);
        }

        // --- Trip management ---
        async function renameTrip(trip) {
            const name = prompt('New name:', trip.name);
            if (name && name !== trip.name) {
                await api(`/api/trips/${trip.id}`, {
                    method: 'PUT',
                    body: JSON.stringify({ name }),
                });
                await loadTrips();
            }
        }

        async function toggleArchive(trip) {
            const status = trip.status === 'archived' ? 'active' : 'archived';
            await api(`/api/trips/${trip.id}`, {
                method: 'PUT',
                body: JSON.stringify({ status }),
            });
            await loadTrips();
        }

        async function confirmDelete(trip) {
            if (confirm(`Delete "${trip.name}" and all its data?`)) {
                await api(`/api/trips/${trip.id}`, { method: 'DELETE' });
                await loadTrips();
            }
        }

        // --- Helpers ---
        function scrollToBottom() {
            if (messagesContainer.value) {
                messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight;
            }
        }

        function formatDate(iso) {
            return new Date(iso).toLocaleDateString('en-GB', {
                day: 'numeric', month: 'short', year: 'numeric',
            });
        }

        function sv(dest, key) {
            if (!dest.scores_snapshot || !dest.scores_snapshot.total_score) return '—';
            const v = dest.scores_snapshot[key];
            return v != null ? (typeof v === 'number' ? v.toFixed(1) : v) : '—';
        }

        function toggleReasoning(destId) {
            const s = new Set(expandedReasoning.value);
            if (s.has(destId)) s.delete(destId); else s.add(destId);
            expandedReasoning.value = s;
        }

        function isReasoningExpanded(destId) {
            return expandedReasoning.value.has(destId);
        }

        function flightVal(dest) {
            if (!dest.scores_snapshot || !dest.scores_snapshot.total_score) return '—';
            const h = dest.scores_snapshot.flight_hours;
            return h != null ? `${h}h` : '—';
        }

        function renderMarkdown(text) {
            if (!text) return '';
            // Full markdown render for sidebar too — tables, bold, lists
            return marked.parse(text);
        }

        function renderMarkdownFull(text) {
            if (!text) return '';
            // Full markdown render for zoom overlay — tables, headers, lists, code
            return marked.parse(text);
        }

        const lastMessageHasQuestions = computed(() => {
            if (!messages.value.length) return false;
            const last = messages.value[messages.value.length - 1];
            if (last.role !== 'assistant') return false;
            return parseQuestions(last.content).length > 0;
        });

        function zoomLastAssistant() {
            const last = [...messages.value].reverse().find(m => m.role === 'assistant');
            if (last) zoomMessage(last);
        }

        function zoomMessage(msg) {
            zoomedMessage.value = msg;
            // Parse questions from assistant messages
            if (msg.role === 'assistant') {
                zoomQuestions.value = parseQuestions(msg.content);
            } else {
                zoomQuestions.value = [];
            }
        }

        function parseQuestions(text) {
            // Match numbered questions: "1. ...", "2) ...", "- **Q1**: ..."
            // Also match "**Question**:" patterns and lines ending with "?"
            const questions = [];
            const lines = text.split('\n');
            for (const line of lines) {
                const trimmed = line.trim();
                // Numbered: "1. Something?" or "1) Something?"
                const numbered = trimmed.match(/^\d+[\.\)]\s*\**(.+?)\**\s*$/);
                if (numbered && trimmed.includes('?')) {
                    questions.push({ text: numbered[1].trim(), answer: '' });
                    continue;
                }
                // Bold question: "**Something?**" or "- **Something?**"
                const bold = trimmed.match(/^[-•*]*\s*\*\*(.+?\?)\*\*/);
                if (bold) {
                    questions.push({ text: bold[1].trim(), answer: '' });
                    continue;
                }
                // Plain question line ending with ?
                if (trimmed.endsWith('?') && trimmed.length > 10 && !trimmed.startsWith('|')) {
                    // Skip if it looks like a table row or too short
                    const clean = trimmed.replace(/^[-•*]+\s*/, '').replace(/\*\*/g, '');
                    questions.push({ text: clean, answer: '' });
                }
            }
            return questions;
        }

        async function sendZoomAnswers() {
            const answers = zoomQuestions.value
                .filter(q => q.answer.trim())
                .map(q => `${q.text} → ${q.answer.trim()}`)
                .join('\n');
            if (!answers) return;
            zoomedMessage.value = null;
            zoomQuestions.value = [];
            await doSend(answers);
        }

        onMounted(async () => {
            await loadTrips();
            await restoreFromHash();
            // Also respond to hash changes triggered by the user (back/forward).
            window.addEventListener('hashchange', restoreFromHash);
        });

        async function restoreFromHash() {
            const hash = window.location.hash;
            // Legacy trip route
            const tripMatch = hash.match(/^#trip\/(\d+)$/);
            if (tripMatch) {
                await openTrip(parseInt(tripMatch[1]));
                return;
            }
            // Spec 006 — Golf Library routes
            const resortDetailMatch = hash.match(/^#\/library\/resort\/(\d+)$/);
            if (resortDetailMatch) {
                await openResortDetail(parseInt(resortDetailMatch[1]));
                return;
            }
            const courseDetailMatch = hash.match(/^#\/library\/course\/(\d+)$/);
            if (courseDetailMatch) {
                await openCourseDetail(parseInt(courseDetailMatch[1]));
                return;
            }
            if (hash === '#/library/add') {
                openLibraryAdd();
                return;
            }
            const browseMatch = hash.match(/^#\/library(?:\/(resorts|courses))?$/);
            if (browseMatch) {
                const mode = browseMatch[1] || 'resorts';
                libBrowse.value.mode = mode;
                view.value = 'library';
                await reloadLibrary();
                return;
            }
            // Spec 007 — Year routes
            const yearListMatch = hash.match(/^#\/year\/(\d{4})$/);
            if (yearListMatch) {
                yearFilter.value = parseInt(yearListMatch[1]);
                view.value = 'year';
                await loadYearPlans();
                return;
            }
            const yearPlanMatch = hash.match(/^#\/year-plan\/(\d+)$/);
            if (yearPlanMatch) {
                await openYearPlan(parseInt(yearPlanMatch[1]));
                return;
            }
        }

        // ------------------------------------------------------------------
        // Spec 006 — Golf Library: Add flow
        // ------------------------------------------------------------------

        async function openLibrary() {
            view.value = 'library';
            window.location.hash = `#/library/${libBrowse.value.mode}`;
            await reloadLibrary();
        }

        function setBrowseMode(mode) {
            libBrowse.value.mode = mode;
            libBrowse.value.offset = 0;
            libBrowse.value.sort = 'rank_rating';
            window.location.hash = `#/library/${mode}`;
            reloadLibrary();
        }

        function resetLibraryFilters() {
            const prevMode = libBrowse.value.mode;
            libBrowse.value = _emptyLibBrowse();
            libBrowse.value.mode = prevMode;
            reloadLibrary();
        }

        function hasAnyFilter() {
            const b = libBrowse.value;
            if (b.q || b.country) return true;
            const f = b.filters;
            if (f.price_category.length || f.hotel_type.length) return true;
            if (f.course_type.length) return true;
            if (f.month != null || f.min_difficulty != null || f.max_difficulty != null) return true;
            if (f.min_holes != null || f.max_green_fee_eur != null) return true;
            if (f.parent_resort && f.parent_resort !== 'any') return true;
            if (f.region_match && f.region_match !== 'any') return true;
            return false;
        }

        function debouncedReload() {
            if (_browseTimer) clearTimeout(_browseTimer);
            _browseTimer = setTimeout(reloadLibrary, 200);
        }

        async function reloadLibrary() {
            libBrowse.value.loading = true;
            const b = libBrowse.value;
            const params = new URLSearchParams();
            if (b.q) params.append('q', b.q);
            if (b.country) params.append('country', b.country.toUpperCase());
            params.append('sort', b.sort);
            params.append('sort_dir', b.sort_dir);
            params.append('limit', String(b.limit));
            params.append('offset', String(b.offset));
            params.append('region_match', b.filters.region_match);

            let path;
            if (b.mode === 'resorts') {
                b.filters.price_category.forEach(p => params.append('price_category', p));
                b.filters.hotel_type.forEach(h => params.append('hotel_type', h));
                if (b.filters.month != null) params.append('month', String(b.filters.month));
                path = '/api/golf-library/resorts?' + params.toString();
            } else {
                b.filters.course_type.forEach(t => params.append('course_type', t));
                if (b.filters.min_difficulty != null) params.append('min_difficulty', String(b.filters.min_difficulty));
                if (b.filters.max_difficulty != null) params.append('max_difficulty', String(b.filters.max_difficulty));
                if (b.filters.min_holes != null) params.append('min_holes', String(b.filters.min_holes));
                if (b.filters.max_green_fee_eur != null) params.append('max_green_fee_eur', String(b.filters.max_green_fee_eur));
                params.append('parent_resort', b.filters.parent_resort);
                path = '/api/golf-library/courses?' + params.toString();
            }

            try {
                const resp = await api(path);
                b.total = resp.total;
                b.results = resp.results;
            } catch (e) {
                b.total = 0;
                b.results = [];
                console.error('library reload failed', e);
            } finally {
                b.loading = false;
            }
        }

        function libraryPrevPage() {
            libBrowse.value.offset = Math.max(0, libBrowse.value.offset - libBrowse.value.limit);
            reloadLibrary();
        }
        function libraryNextPage() {
            libBrowse.value.offset += libBrowse.value.limit;
            reloadLibrary();
        }

        function _resetDetailState() {
            libDetail.value = {
                resort: null, course: null, carouselIdx: 0,
                editing: null, edit: {},
                deleteBlocked: null,
                regionSearch: '', regionResults: [],
                resortSearch: '', resortResults: [],
                newImageUrl: '',
            };
        }

        async function openResortDetail(id) {
            _resetDetailState();
            try {
                const detail = await api(`/api/golf-library/resorts/${id}`);
                libDetail.value.resort = detail;
                view.value = 'library-resort-detail';
                window.location.hash = `#/library/resort/${id}`;
            } catch (e) {
                alert('Failed to load resort detail: ' + e.message);
            }
        }

        async function openCourseDetail(id) {
            _resetDetailState();
            try {
                const detail = await api(`/api/golf-library/courses/${id}`);
                libDetail.value.course = detail;
                view.value = 'library-course-detail';
                window.location.hash = `#/library/course/${id}`;
            } catch (e) {
                alert('Failed to load course detail: ' + e.message);
            }
        }

        // --- Edit (US4) ---

        function startEditResort() {
            const r = libDetail.value.resort;
            libDetail.value.edit = {
                name: r.name, country_code: r.country_code, town: r.town,
                hotel_name: r.hotel_name, hotel_type: r.hotel_type,
                price_category: r.price_category, rank_rating: r.rank_rating,
                description: r.description, personal_notes: r.personal_notes,
            };
            libDetail.value.editing = 'resort';
        }

        function startEditCourse() {
            const c = libDetail.value.course;
            libDetail.value.edit = {
                name: c.name, country_code: c.country_code,
                holes: c.holes, par: c.par, length_yards: c.length_yards,
                type: c.type, architect: c.architect, difficulty: c.difficulty,
                rank_rating: c.rank_rating, green_fee_low_eur: c.green_fee_low_eur,
                green_fee_high_eur: c.green_fee_high_eur, green_fee_notes: c.green_fee_notes,
                description: c.description, personal_notes: c.personal_notes,
            };
            libDetail.value.editing = 'course';
        }

        async function saveResortEdit() {
            const id = libDetail.value.resort.id;
            await api(`/api/golf-library/resorts/${id}`, {
                method: 'PATCH',
                body: JSON.stringify(libDetail.value.edit),
            });
            libDetail.value.editing = null;
            await openResortDetail(id);
        }

        async function saveCourseEdit() {
            const id = libDetail.value.course.id;
            await api(`/api/golf-library/courses/${id}`, {
                method: 'PATCH',
                body: JSON.stringify(libDetail.value.edit),
            });
            libDetail.value.editing = null;
            await openCourseDetail(id);
        }

        // --- Delete (US4) ---

        async function confirmDeleteResort() {
            const id = libDetail.value.resort.id;
            const res = await fetch(`${API}/api/golf-library/resorts/${id}`, { method: 'DELETE' });
            if (res.status === 204) {
                view.value = 'library';
                await reloadLibrary();
                return;
            }
            if (res.status === 409) {
                libDetail.value.deleteBlocked = (await res.json()).detail;
                return;
            }
            alert('Delete failed: ' + res.status);
        }

        async function confirmDeleteCourse() {
            const id = libDetail.value.course.id;
            const res = await fetch(`${API}/api/golf-library/courses/${id}`, { method: 'DELETE' });
            if (res.status === 204) {
                view.value = 'library';
                await reloadLibrary();
                return;
            }
            if (res.status === 409) {
                libDetail.value.deleteBlocked = (await res.json()).detail;
                return;
            }
            alert('Delete failed: ' + res.status);
        }

        // --- Region + resort linking (US5) ---

        function searchLibraryRegion() {
            if (_regionSearchTimer) clearTimeout(_regionSearchTimer);
            _regionSearchTimer = setTimeout(async () => {
                const q = libDetail.value.regionSearch;
                if (!q || q.length < 2) {
                    libDetail.value.regionResults = [];
                    return;
                }
                libDetail.value.regionResults = await api(
                    `/api/vacationmap/regions/search?q=${encodeURIComponent(q)}`
                );
            }, 200);
        }

        function searchLibraryResort() {
            if (_resortSearchTimer) clearTimeout(_resortSearchTimer);
            _resortSearchTimer = setTimeout(async () => {
                const q = libDetail.value.resortSearch;
                if (!q || q.length < 2) {
                    libDetail.value.resortResults = [];
                    return;
                }
                const resp = await api(
                    `/api/golf-library/resorts?q=${encodeURIComponent(q)}&limit=10`
                );
                libDetail.value.resortResults = resp.results;
            }, 200);
        }

        async function saveRegionLink(vmKey) {
            const isResort = !!libDetail.value.resort;
            const id = isResort ? libDetail.value.resort.id : libDetail.value.course.id;
            const endpoint = isResort
                ? `/api/golf-library/resorts/${id}/link-region`
                : `/api/golf-library/courses/${id}/link-region`;
            await api(endpoint, {
                method: 'POST',
                body: JSON.stringify({ vacationmap_region_key: vmKey }),
            });
            libDetail.value.regionSearch = '';
            libDetail.value.regionResults = [];
            if (isResort) await openResortDetail(id);
            else await openCourseDetail(id);
        }

        async function saveCourseResortLink(resortId) {
            const id = libDetail.value.course.id;
            try {
                await api(`/api/golf-library/courses/${id}/link-resort`, {
                    method: 'POST',
                    body: JSON.stringify({ resort_id: resortId }),
                });
            } catch (e) {
                alert('Link failed: ' + e.message);
                return;
            }
            libDetail.value.resortSearch = '';
            libDetail.value.resortResults = [];
            await openCourseDetail(id);
        }

        // --- Image management ---

        async function addImageToEntity(entityType, entityId) {
            const url = libDetail.value.newImageUrl;
            if (!url) return;
            try {
                await api('/api/golf-library/images', {
                    method: 'POST',
                    body: JSON.stringify({
                        entity_type: entityType, entity_id: entityId, url,
                    }),
                });
                libDetail.value.newImageUrl = '';
                if (entityType === 'resort') await openResortDetail(entityId);
                else await openCourseDetail(entityId);
            } catch (e) {
                alert('Could not add image: ' + e.message);
            }
        }

        async function saveImageCaption(img) {
            await api(`/api/golf-library/images/${img.id}`, {
                method: 'PATCH',
                body: JSON.stringify({ caption: img.caption }),
            });
        }

        async function deleteImage(id) {
            await fetch(`${API}/api/golf-library/images/${id}`, { method: 'DELETE' });
            if (libDetail.value.resort) await openResortDetail(libDetail.value.resort.id);
            else if (libDetail.value.course) await openCourseDetail(libDetail.value.course.id);
        }

        function openLibraryAdd() {
            libAdd.value = _emptyLibAdd();
            view.value = 'library-add';
            window.location.hash = '#/library/add';
        }

        function resetLibraryAdd() {
            libAdd.value = _emptyLibAdd();
        }

        function extractErrorTitle(status) {
            return {
                api_error: 'Claude API error',
                no_match: 'No match found',
                fetch_error: 'Could not fetch the URL',
                ambiguous: 'Multiple candidates detected',
            }[status] || 'Extraction failed';
        }

        async function runExtract() {
            const payload = {
                entity_type: libAdd.value.entityType,
                url: libAdd.value.url || null,
                name: libAdd.value.name || null,
            };
            libAdd.value.extracting = true;
            libAdd.value.error = null;
            try {
                const res = await fetch(`${API}/api/golf-library/extract`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const body = await res.json();
                if (!res.ok) {
                    libAdd.value.error = body.detail || body;
                    return;
                }
                _applyExtracted(body);
            } catch (e) {
                libAdd.value.error = { status: 'api_error', message: String(e) };
            } finally {
                libAdd.value.extracting = false;
            }
        }

        function _applyExtracted(result) {
            // result.entity_type, result.data, result.image_candidates,
            // result.source_urls, result.warnings, result.possible_parent_resort
            libAdd.value.entityType = result.entity_type;
            libAdd.value.form = { ..._emptyForm(result.entity_type), ...result.data };
            libAdd.value.bestMonthsText = (result.data.best_months || []).join(',');
            libAdd.value.tagsText = (result.data.tags || []).join(',');
            libAdd.value.imageCandidates = result.image_candidates || [];
            libAdd.value.sources = result.source_urls || [];
            libAdd.value.warnings = result.warnings || [];
            libAdd.value.possibleParent = result.possible_parent_resort || null;
            libAdd.value.extracted = true;
        }

        function enterManualMode() {
            libAdd.value.extracted = true;
            libAdd.value.manual = true;
            libAdd.value.error = null;
            libAdd.value.form = _emptyForm(libAdd.value.entityType);
            libAdd.value.imageCandidates = [];
            libAdd.value.sources = [];
            libAdd.value.warnings = [];
        }

        function switchEntityType() {
            const overlap = ['name', 'url', 'country_code', 'region_name_raw',
                             'vacationmap_region_key', 'town', 'latitude', 'longitude',
                             'description', 'personal_notes', 'rank_rating'];
            const preserved = {};
            overlap.forEach(k => { if (libAdd.value.form[k] != null) preserved[k] = libAdd.value.form[k]; });
            const newType = libAdd.value.entityType === 'resort' ? 'course' : 'resort';
            libAdd.value.entityType = newType;
            libAdd.value.form = { ..._emptyForm(newType), ...preserved };
        }

        function linkToExistingResort(resortId) {
            libAdd.value.form.resort_id = resortId;
            libAdd.value.possibleParent = null;
        }

        async function saveLibraryEntry(force = false) {
            // Parse CSV helpers
            const months = libAdd.value.bestMonthsText
                .split(',').map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n) && n >= 1 && n <= 12);
            const tags = libAdd.value.tagsText
                .split(',').map(s => s.trim()).filter(Boolean);

            const payload = {
                ...libAdd.value.form,
                best_months: months,
                tags,
                source_urls: libAdd.value.sources,
                image_urls: libAdd.value.imageCandidates
                    .filter(c => c.validation !== 'unreachable' && c.validation !== 'wrong_type')
                    .map(c => c.url),
            };

            // Strip nulls to keep the API payload clean
            Object.keys(payload).forEach(k => {
                if (payload[k] === null || payload[k] === '') delete payload[k];
            });

            const endpoint = libAdd.value.entityType === 'resort'
                ? '/api/golf-library/resorts'
                : '/api/golf-library/courses';
            const qs = force ? '?force=true' : '';

            libAdd.value.saving = true;
            try {
                const res = await fetch(`${API}${endpoint}${qs}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const body = await res.json();
                if (res.status === 409) {
                    libAdd.value.duplicate = body.detail || body;
                    return;
                }
                if (!res.ok) {
                    alert('Save failed: ' + (body.detail || 'unknown'));
                    return;
                }
                // Success
                alert(`Saved! id=${body.id}`);
                view.value = 'library';
                await openLibrary();
            } finally {
                libAdd.value.saving = false;
            }
        }

        async function forceCreateDuplicate() {
            libAdd.value.duplicate = null;
            await saveLibraryEntry(true);
        }

        function editExistingDuplicate() {
            // Detail page ships in US2; for now, just close and note.
            const id = libAdd.value.duplicate?.existing_entity?.id;
            libAdd.value.duplicate = null;
            alert(`The existing entry has id ${id}. The detail/edit page ships in the next phase; for now, start over or save with "Create anyway".`);
        }

        // ==================================================================
        // F009 — Yearly Vacation Planner (YearPlan → YearOption → Slot)
        // ==================================================================
        const yearPlans = ref([]);
        const currentYearPlan = ref(null);
        const yearFilter = ref(new Date().getFullYear());
        const yearActiveConvId = ref(null);
        const yearMessages = ref([]);
        const yearChatInput = ref('');
        const yearSending = ref(false);
        const yearChatEl = ref(null);

        // Windows editor (shared across options)
        const editingWindows = ref(false);
        const draftWindows = ref([]);

        // Per-cell trip-idea creation/edit (cell = intersection of one option & one window)
        const addingSlotToOption = ref(null);   // option id being added to
        const addingInWindow = ref(null);       // window index being added to
        const newSlot = ref(_emptySlot());
        const editingSlotId = ref(null);
        const editSlot = ref(_emptySlot());

        function _emptySlot() {
            // Dates inherit from the window by default; leave everything date-shaped blank.
            return {
                label: '',
                theme: '',
                window_index: null,
                duration_days: null,
                climate_hint: '',
                constraints_note: '',
            };
        }

        function _emptyWindow() {
            const y = new Date().getFullYear();
            return { label: '', start_date: `${y}-06-01`, end_date: `${y}-06-14`, duration_hint: null, constraints: '' };
        }

        const yearActiveConversations = computed(() => {
            const convs = (currentYearPlan.value?.conversations || []).filter(c => c.status !== 'archived');
            return convs.sort((a, b) => {
                if (a.name === 'Main') return -1;
                if (b.name === 'Main') return 1;
                return a.created_at < b.created_at ? -1 : 1;
            });
        });

        async function openYear() {
            view.value = 'year';
            window.location.hash = `#/year/${yearFilter.value}`;
            await loadYearPlans();
        }

        async function loadYearPlans() {
            yearPlans.value = await api(`/api/year-plans?year=${yearFilter.value}`);
        }

        async function setYearFilter(yr) {
            yearFilter.value = yr;
            await loadYearPlans();
            window.location.hash = `#/year/${yearFilter.value}`;
        }

        async function createYearPlan() {
            const name = prompt('New year plan name (e.g. "Main", "Option A — adventurous"):');
            if (!name) return;
            const intent = prompt('Short intent / narrative (optional):', '') || '';
            const plan = await api('/api/year-plans', {
                method: 'POST',
                body: JSON.stringify({ year: yearFilter.value, name, intent }),
            });
            await openYearPlan(plan.id);
        }

        async function openYearPlan(planId) {
            const detail = await api(`/api/year-plans/${planId}`);
            currentYearPlan.value = detail;
            view.value = 'year-detail';
            window.location.hash = `#/year-plan/${planId}`;
            // Pick last conversation
            if (detail.conversations && detail.conversations.length) {
                const conv = detail.conversations[detail.conversations.length - 1];
                yearActiveConvId.value = conv.id;
                yearMessages.value = await api(`/api/conversations/${conv.id}/messages`);
            } else {
                const conv = await api(`/api/year-plans/${planId}/conversations`, {
                    method: 'POST',
                    body: JSON.stringify({ name: 'Main' }),
                });
                yearActiveConvId.value = conv.id;
                yearMessages.value = [];
                await reloadYearPlan();
            }
        }

        async function reloadYearPlan() {
            if (!currentYearPlan.value) return;
            currentYearPlan.value = await api(`/api/year-plans/${currentYearPlan.value.id}`);
        }

        async function renameYearPlan() {
            const name = prompt('Rename year plan:', currentYearPlan.value.name);
            if (!name || name === currentYearPlan.value.name) return;
            await api(`/api/year-plans/${currentYearPlan.value.id}`, {
                method: 'PATCH',
                body: JSON.stringify({ name }),
            });
            await reloadYearPlan();
        }

        async function editYearIntent() {
            const intent = prompt('Year intent / narrative:', currentYearPlan.value.intent || '');
            if (intent === null) return;
            await api(`/api/year-plans/${currentYearPlan.value.id}`, {
                method: 'PATCH',
                body: JSON.stringify({ intent }),
            });
            await reloadYearPlan();
        }

async function deleteYearPlan(planId, planName) {
            if (!confirm(`Permanently delete year plan "${planName}" and all its slots and conversations? Linked trips are kept. This cannot be undone.`)) return;
            await api(`/api/year-plans/${planId}?confirm=true`, { method: 'DELETE' });
            currentYearPlan.value = null;
            view.value = 'year';
            await loadYearPlans();
        }

        // --- Windows editor (shared across options) ---
        function startEditWindows() {
            draftWindows.value = (currentYearPlan.value?.windows || []).map(w => ({
                label: w.label || '',
                start_date: w.start_date,
                end_date: w.end_date,
                duration_hint: w.duration_hint || null,
                constraints: w.constraints || '',
            }));
            if (!draftWindows.value.length) draftWindows.value.push(_emptyWindow());
            editingWindows.value = true;
        }

        function cancelEditWindows() {
            editingWindows.value = false;
            draftWindows.value = [];
        }

        function addWindowRow() {
            draftWindows.value.push(_emptyWindow());
        }

        function slotsReferencingWindow(windowIndex) {
            const lines = [];
            for (const opt of currentYearPlan.value?.options || []) {
                for (const s of opt.slots || []) {
                    if (s.window_index === windowIndex) {
                        lines.push(`${opt.name} — ${s.label || 'unnamed'}`);
                    }
                }
            }
            return lines;
        }
        function removeWindowRow(idx) {
            const affected = slotsReferencingWindow(idx);
            if (!affected.length) {
                draftWindows.value.splice(idx, 1);
                return;
            }
            dialogKind.value = 'confirmRemoveWindow';
            dialogCtx.value = { windowIndex: idx, affected };
        }
        function confirmRemoveWindowRow() {
            const { windowIndex } = dialogCtx.value;
            draftWindows.value.splice(windowIndex, 1);
            closeDialog();
        }

        async function saveWindows() {
            const windows = draftWindows.value
                .filter(w => w.start_date && w.end_date)
                .map(w => ({
                    label: w.label || null,
                    start_date: w.start_date,
                    end_date: w.end_date,
                    duration_hint: w.duration_hint ? parseInt(w.duration_hint) : null,
                    constraints: w.constraints || null,
                }));
            await api(`/api/year-plans/${currentYearPlan.value.id}`, {
                method: 'PATCH',
                body: JSON.stringify({ windows }),
            });
            editingWindows.value = false;
            draftWindows.value = [];
            await reloadYearPlan();
        }

        function describeWindow(w, idx) {
            const parts = [];
            if (w.label) parts.push(w.label);
            parts.push(`${w.start_date} → ${w.end_date}`);
            if (w.duration_hint) parts.push(`~${w.duration_hint}d`);
            return `[${idx}] ${parts.join(' · ')}`;
        }

        // --- Options ---
        async function createOption() {
            const name = prompt('New option name (e.g. "Adventurous mix", "Golf-heavy"):');
            if (!name) return;
            const summary = prompt('One-line summary (optional):', '') || '';
            await api(`/api/year-plans/${currentYearPlan.value.id}/options`, {
                method: 'POST',
                body: JSON.stringify({ name, summary, created_by: 'user' }),
            });
            await reloadYearPlan();
        }

        async function renameOption(opt) {
            const name = prompt('Rename option:', opt.name);
            if (!name || name === opt.name) return;
            await api(`/api/year-options/${opt.id}`, {
                method: 'PATCH',
                body: JSON.stringify({ name }),
            });
            await reloadYearPlan();
        }

        async function editOptionSummary(opt) {
            const summary = prompt('Option summary (optional):', opt.summary || '');
            if (summary === null) return;
            await api(`/api/year-options/${opt.id}`, {
                method: 'PATCH',
                body: JSON.stringify({ summary }),
            });
            await reloadYearPlan();
        }

        async function forkOption(opt) {
            const name = prompt('Name for the forked option:', `${opt.name} (copy)`);
            if (!name) return;
            await api(`/api/year-options/${opt.id}/fork`, {
                method: 'POST',
                body: JSON.stringify({ name }),
            });
            await reloadYearPlan();
        }

        async function markOptionChosen(opt) {
            await api(`/api/year-options/${opt.id}/mark-chosen`, { method: 'POST' });
            await reloadYearPlan();
        }

        async function unpickOption(opt) {
            await api(`/api/year-options/${opt.id}/unpick`, { method: 'POST' });
            await reloadYearPlan();
        }

        // Unified dialog state — only one open at a time.
        const dialogKind = ref(null);
        const dialogCtx = ref({});
        const dialogSubmitting = ref(false);
        function closeDialog() {
            dialogKind.value = null;
            dialogCtx.value = {};
            dialogSubmitting.value = false;
        }

        function excludeOption(opt) {
            dialogKind.value = 'excludeOption';
            dialogCtx.value = { target: opt, reason: '' };
        }
        async function submitExcludeOption() {
            const { target, reason } = dialogCtx.value;
            if (!reason || !reason.trim()) return;
            dialogSubmitting.value = true;
            try {
                await api(`/api/year-options/${target.id}/exclude`, {
                    method: 'POST',
                    body: JSON.stringify({ reason: reason.trim() }),
                });
                closeDialog();
                await reloadYearPlan();
            } finally {
                dialogSubmitting.value = false;
            }
        }

        async function unexcludeOption(opt) {
            await api(`/api/year-options/${opt.id}/unexclude`, { method: 'POST' });
            await reloadYearPlan();
        }

async function deleteOption(opt) {
            if (!confirm(`Delete option "${opt.name}" and its trip ideas? (Linked trips are kept.)`)) return;
            await api(`/api/year-options/${opt.id}?confirm=true`, { method: 'DELETE' });
            await reloadYearPlan();
        }

        // --- Trip ideas (cells in the option × window grid) ---
        function ideasInCell(option, windowIndex) {
            if (!option || !option.slots) return [];
            return option.slots
                .filter(s => s.window_index === windowIndex)
                .sort((a, b) => (a.position ?? 0) - (b.position ?? 0) || a.id - b.id);
        }

        function startAddIdeaInCell(optionId, windowIndex) {
            addingSlotToOption.value = optionId;
            addingInWindow.value = windowIndex;
            newSlot.value = _emptySlot();
            newSlot.value.window_index = windowIndex;
        }

        function cancelAddSlot() {
            addingSlotToOption.value = null;
            addingInWindow.value = null;
            newSlot.value = _emptySlot();
        }

        async function saveNewSlot() {
            if (!addingSlotToOption.value) return;
            try {
                await api(`/api/year-options/${addingSlotToOption.value}/slots`, {
                    method: 'POST',
                    body: JSON.stringify(_buildSlotBody(newSlot.value)),
                });
                cancelAddSlot();
                await reloadYearPlan();
            } catch (e) {
                alert('Could not add trip idea: ' + e.message);
            }
        }

        function _buildSlotBody(s) {
            // Dates inherit from the window when omitted — only send what the
            // user actually typed.
            const body = {
                window_index: parseInt(s.window_index),
                label: s.label || null,
                theme: s.theme || '',
                duration_days: s.duration_days ? parseInt(s.duration_days) : null,
                climate_hint: s.climate_hint || null,
                constraints_note: s.constraints_note || null,
            };
            return body;
        }

        function startEditSlot(slot) {
            editingSlotId.value = slot.id;
            editSlot.value = {
                label: slot.label || '',
                theme: slot.theme || '',
                window_index: slot.window_index,
                duration_days: slot.duration_days || null,
                climate_hint: slot.climate_hint || '',
                constraints_note: slot.constraints_note || '',
            };
        }

        async function saveSlotEdit() {
            try {
                await api(`/api/slots/${editingSlotId.value}`, {
                    method: 'PATCH',
                    body: JSON.stringify(_buildSlotBody(editSlot.value)),
                });
                editingSlotId.value = null;
                await reloadYearPlan();
            } catch (e) {
                alert('Could not update trip idea: ' + e.message);
            }
        }

        function cancelSlotEdit() {
            editingSlotId.value = null;
        }

        async function deleteSlot(slotId) {
            if (!confirm('Delete this trip idea? (The linked trip, if any, is kept.)')) return;
            await api(`/api/slots/${slotId}?confirm=true`, { method: 'DELETE' });
            await reloadYearPlan();
        }

        // --- Slot → Trip actions ---
        async function acceptSlot(slotId) {
            await api(`/api/slots/${slotId}/accept`, { method: 'POST' });
            await reloadYearPlan();
        }

        async function unreviewSlot(slotId) {
            await api(`/api/slots/${slotId}/unreview`, { method: 'POST' });
            await reloadYearPlan();
        }

        function excludeIdea(idea) {
            dialogKind.value = 'excludeIdea';
            dialogCtx.value = { target: idea, reason: '' };
        }
        async function submitExcludeIdea() {
            const { target, reason } = dialogCtx.value;
            if (!reason || !reason.trim()) return;
            dialogSubmitting.value = true;
            try {
                await api(`/api/slots/${target.id}/exclude`, {
                    method: 'POST',
                    body: JSON.stringify({ reason: reason.trim() }),
                });
                closeDialog();
                await reloadYearPlan();
            } finally {
                dialogSubmitting.value = false;
            }
        }

        async function unexcludeIdea(idea) {
            await api(`/api/slots/${idea.id}/unexclude`, { method: 'POST' });
            await reloadYearPlan();
        }

        // --- Per-cell "show excluded" toggle state ---
        const expandedExcludedCells = ref(new Set());
        function cellKey(optId, widx) { return `${optId}:${widx}`; }
        function isExcludedShownInCell(optId, widx) {
            return expandedExcludedCells.value.has(cellKey(optId, widx));
        }
        function toggleShowExcludedInCell(optId, widx) {
            const s = new Set(expandedExcludedCells.value);
            const k = cellKey(optId, widx);
            if (s.has(k)) s.delete(k); else s.add(k);
            expandedExcludedCells.value = s;
        }
        function activeIdeasInCell(opt, widx) {
            return ideasInCell(opt, widx).filter(s => s.status !== 'excluded');
        }
        function excludedIdeasInCell(opt, widx) {
            return ideasInCell(opt, widx).filter(s => s.status === 'excluded');
        }

        // --- Option-level show-excluded toggle ---
        const showExcludedOptions = ref(false);
        function toggleShowExcludedOptions() {
            showExcludedOptions.value = !showExcludedOptions.value;
        }

        // --- Option focus (compare subset) ---
        const focusedOptionIds = ref(new Set());
        function isOptionFocused(id) { return focusedOptionIds.value.has(id); }
        function hasFocusedOptions() { return focusedOptionIds.value.size > 0; }
        function toggleOptionFocus(optId) {
            const s = new Set(focusedOptionIds.value);
            if (s.has(optId)) s.delete(optId); else s.add(optId);
            focusedOptionIds.value = s;
        }
        function clearOptionFocus() {
            focusedOptionIds.value = new Set();
        }
        function shouldShowOption(opt) {
            if (focusedOptionIds.value.size > 0 && !focusedOptionIds.value.has(opt.id)) return false;
            return opt.status !== 'excluded' || showExcludedOptions.value;
        }

        // --- Display name fallback: label > window label > "Window #N" > "(unnamed)" ---
        function ideaDisplayName(idea) {
            if (idea.label) return idea.label;
            const win = currentYearPlan.value?.windows?.[idea.window_index];
            if (win?.label) return win.label;
            if (idea.window_index !== null && idea.window_index !== undefined) {
                return `Window #${idea.window_index + 1}`;
            }
            return '(unnamed)';
        }

        // --- Inline edit for YearPlan fields ---
        const editingYearPlanField = ref(null); // 'name' | 'intent' | null
        const yearPlanFieldDraft = ref('');
        function startEditYearPlanField(field) {
            if (!currentYearPlan.value) return;
            editingYearPlanField.value = field;
            yearPlanFieldDraft.value = field === 'name'
                ? (currentYearPlan.value.name || '')
                : (currentYearPlan.value.intent || '');
        }
        function cancelEditYearPlanField() {
            editingYearPlanField.value = null;
            yearPlanFieldDraft.value = '';
        }
        async function saveYearPlanField() {
            const field = editingYearPlanField.value;
            if (!field || !currentYearPlan.value) return;
            const value = yearPlanFieldDraft.value.trim();
            if (field === 'name' && !value) { cancelEditYearPlanField(); return; }
            const patch = field === 'name' ? { name: value } : { intent: value };
            await api(`/api/year-plans/${currentYearPlan.value.id}`, {
                method: 'PATCH',
                body: JSON.stringify(patch),
            });
            cancelEditYearPlanField();
            await reloadYearPlan();
        }

        // --- Inline edit for YearOption fields ---
        const editingOptionField = ref(null); // { id, field } | null
        const optionFieldDraft = ref('');
        function startEditOptionField(opt, field) {
            editingOptionField.value = { id: opt.id, field };
            optionFieldDraft.value = field === 'name'
                ? (opt.name || '')
                : (opt.summary || '');
        }
        function cancelEditOptionField() {
            editingOptionField.value = null;
            optionFieldDraft.value = '';
        }
        async function saveOptionField() {
            const sel = editingOptionField.value;
            if (!sel) return;
            const value = optionFieldDraft.value.trim();
            if (sel.field === 'name' && !value) { cancelEditOptionField(); return; }
            const patch = sel.field === 'name' ? { name: value } : { summary: value };
            await api(`/api/year-options/${sel.id}`, {
                method: 'PATCH',
                body: JSON.stringify(patch),
            });
            cancelEditOptionField();
            await reloadYearPlan();
        }
        function isEditingOptionField(opt, field) {
            const sel = editingOptionField.value;
            return sel && sel.id === opt.id && sel.field === field;
        }

        // --- Per-idea overflow menu (•••) ---
        const openIdeaMenuId = ref(null);
        function toggleIdeaMenu(ideaId, event) {
            if (event) event.stopPropagation();
            openIdeaMenuId.value = openIdeaMenuId.value === ideaId ? null : ideaId;
        }
        function closeIdeaMenu() {
            openIdeaMenuId.value = null;
        }
        async function ideaMenuUnreview(idea) {
            closeIdeaMenu();
            await unreviewSlot(idea.id);
        }

        async function startTripForSlot(slotId) {
            const res = await api(`/api/slots/${slotId}/start-trip`, { method: 'POST' });
            if (res?.trip_id) {
                await openTrip(res.trip_id);
            }
        }

        async function openSlotTrip(slot) {
            if (slot.trip_plan_id) {
                await openTrip(slot.trip_plan_id);
            }
        }

        async function unlinkSlotTrip(slotId) {
            if (!confirm('Unlink the trip from this slot? The trip itself is kept.')) return;
            await api(`/api/slots/${slotId}/unlink-trip`, { method: 'POST' });
            await reloadYearPlan();
        }

        async function linkExistingTrip(slotId) {
            const candidates = currentYearPlan.value?.attachable_trip_ids || [];
            if (!candidates.length) {
                alert('No unlinked trips this year.');
                return;
            }
            dialogKind.value = 'linkTrip';
            dialogCtx.value = {
                slotId,
                candidates,
                candidateTrips: [],
                selectedTripId: candidates[0],
                loading: true,
            };
            // Fetch trip details so the picker shows real names, not just IDs.
            try {
                const allTrips = await api('/api/trips');
                const byId = new Map(allTrips.map(t => [t.id, t]));
                const detailed = candidates
                    .map(id => byId.get(id))
                    .filter(Boolean);
                dialogCtx.value = {
                    ...dialogCtx.value,
                    candidateTrips: detailed,
                    loading: false,
                };
            } catch {
                dialogCtx.value = { ...dialogCtx.value, loading: false };
            }
        }
        async function submitLinkTrip() {
            const { slotId, selectedTripId } = dialogCtx.value;
            if (!selectedTripId) return;
            dialogSubmitting.value = true;
            try {
                await api(`/api/slots/${slotId}/link-trip`, {
                    method: 'POST',
                    body: JSON.stringify({ trip_id: parseInt(selectedTripId) }),
                });
                closeDialog();
                await reloadYearPlan();
            } finally {
                dialogSubmitting.value = false;
            }
        }

        // --- Year advisor shortcuts (dialog-backed) ---
        function askAISuggestForCell(option, windowIndex) {
            if (yearSending.value) return;
            const win = currentYearPlan.value?.windows?.[windowIndex];
            const windowLabel = win?.label || `Window #${windowIndex + 1}`;
            dialogKind.value = 'suggestCell';
            dialogCtx.value = {
                option,
                windowIndex,
                windowLabel,
                count: 2,
                hint: '',
            };
        }
        async function submitSuggestCell() {
            const { option, windowIndex, windowLabel, count, hint } = dialogCtx.value;
            if (!count || count < 1) return;
            const win = currentYearPlan.value?.windows?.[windowIndex];
            const winDates = win ? `${win.start_date} → ${win.end_date}` : '';
            const existing = (option.slots || []).filter(s => s.window_index === windowIndex);
            const existingBlock = existing.length
                ? ` Existing ideas in this cell: ${existing.map(s => `"${s.label || 'unlabeled'}"`).join(', ')}. Propose NEW ideas that are meaningfully different.`
                : '';
            yearChatInput.value =
                `Please propose ${count} trip idea(s) for option "${option.name}" (id=${option.id}) in window ${windowIndex} (${windowLabel}, ${winDates}).`
                + (hint ? ` Guidance: ${hint}.` : '')
                + existingBlock
                + ` Use the propose_slot_in_option tool for each idea with option_id=${option.id} and window_index=${windowIndex}. Stay at the theme level — no specific destinations.`;
            closeDialog();
            await sendYearMessage();
        }

        function askAIForOptions() {
            dialogKind.value = 'askOptions';
            dialogCtx.value = { count: 3, hint: '' };
        }
        async function submitAskOptions() {
            const { count, hint } = dialogCtx.value;
            if (!count || count < 1) return;
            const existing = currentYearPlan.value?.options || [];
            const existingBlock = existing.length
                ? ` I already have these options: ${existing.map(o => `"${o.name}"`).join(', ')}. Please propose NEW options that are meaningfully different from those (different themes, different regions, different vibe).`
                : '';
            const basePrompt = hint
                ? `Please generate ${count} full-year option(s) for me. Style hint: ${hint}.`
                : `Please generate ${count} full-year option(s) for me. Aim for variety — contrast themes, seasons, and activity mixes across the options.`;
            yearChatInput.value = basePrompt
                + existingBlock
                + ' Each option should fill some or all of my open windows (skipping a window is OK if it fits the theme). Stay at the *theme* level — do not lock in specific destinations.';
            closeDialog();
            await sendYearMessage();
        }

        // --- Yearly chat ---
        async function switchYearConversation(convId) {
            yearActiveConvId.value = convId;
            yearMessages.value = await api(`/api/conversations/${convId}/messages`);
            await nextTick();
            if (yearChatEl.value) yearChatEl.value.scrollTop = yearChatEl.value.scrollHeight;
        }

        async function newYearConversation() {
            const name = prompt('Name for the new year-plan conversation:', 'Follow-up');
            if (!name) return;
            const conv = await api(`/api/year-plans/${currentYearPlan.value.id}/conversations`, {
                method: 'POST',
                body: JSON.stringify({ name }),
            });
            await reloadYearPlan();
            await switchYearConversation(conv.id);
        }

        async function sendYearMessage() {
            const content = yearChatInput.value.trim();
            if (!content || yearSending.value || !yearActiveConvId.value) return;
            yearChatInput.value = '';
            yearSending.value = true;
            yearMessages.value.push({
                id: Date.now(), role: 'user', content, created_at: new Date().toISOString(),
            });
            try {
                const res = await api(`/api/conversations/${yearActiveConvId.value}/messages`, {
                    method: 'POST',
                    body: JSON.stringify({ content }),
                });
                yearMessages.value[yearMessages.value.length - 1] = res.user_message;
                yearMessages.value.push(res.assistant_message);
                if (res.year_plan_state_changed) {
                    await reloadYearPlan();
                }
            } catch (err) {
                yearMessages.value.push({
                    id: Date.now() + 1,
                    role: 'assistant',
                    content: `Error: ${err.message}. Please try again.`,
                    created_at: new Date().toISOString(),
                });
            } finally {
                yearSending.value = false;
                await nextTick();
                if (yearChatEl.value) yearChatEl.value.scrollTop = yearChatEl.value.scrollHeight;
            }
        }

        const MONTH_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
        function formatSlotSpan(slot) {
            if (slot.exact_start_date && slot.exact_end_date) {
                return `${slot.exact_start_date} → ${slot.exact_end_date}`;
            }
            const start = `${MONTH_SHORT[slot.start_month - 1]} ${slot.start_year}`;
            if (slot.start_year === slot.end_year && slot.start_month === slot.end_month) {
                return start;
            }
            const end = `${MONTH_SHORT[slot.end_month - 1]} ${slot.end_year}`;
            return `${start} → ${end}`;
        }

        return {
            view, trips, currentTrip, messages, chatInput, sending, creating,
            newTrip, messagesContainer, chatInputEl,
            editingMessageId, editingContent, zoomedMessage, zoomQuestions, chatExpanded, activeConvId,
            activityTagVocab, activityWeightsTotal, addActivityRow,
            lastMessageHasQuestions, zoomLastAssistant,
            goHome, createTrip, openTrip, sendMessage, doSend,
            currentSection,
            quickAction, promptChangeFocus,
            activeConversations, archivedConversations, showArchivedConvs,
            editingDescription, editDescValue, descEditEl,
            startEditDescription, saveDescription,
            destDetail, destDetailLoading, showDestDetail, fmt,
            expandedReasoning, toggleReasoning, isReasoningExpanded,
            linkingDest, linkSearch, linkResults, linkSearchEl,
            startLinking, searchRegions, confirmLink,
            switchConversation, newConversation,
            archiveConversation, unarchiveConversation, deleteConversation, renameConversation,
            shortlistSuggested, excludeSuggested,
            excludeShortlisted, unreviewShortlisted,
            reconsiderExcluded, editNote,
            startEditMessage, saveMessageEdit, deleteMessage,
            renameTrip, toggleArchive, confirmDelete,
            formatDate, sv, flightVal, renderMarkdown, renderMarkdownFull,
            zoomMessage, sendZoomAnswers,
            // Spec 006 — Golf Library
            library, libAdd, libBrowse, libDetail,
            openLibrary, openLibraryAdd, resetLibraryAdd,
            setBrowseMode, resetLibraryFilters, hasAnyFilter,
            debouncedReload, reloadLibrary,
            libraryPrevPage, libraryNextPage,
            openResortDetail, openCourseDetail,
            startEditResort, startEditCourse, saveResortEdit, saveCourseEdit,
            confirmDeleteResort, confirmDeleteCourse,
            searchLibraryRegion, searchLibraryResort,
            saveRegionLink, saveCourseResortLink,
            addImageToEntity, saveImageCaption, deleteImage,
            runExtract, enterManualMode, switchEntityType,
            linkToExistingResort, saveLibraryEntry,
            forceCreateDuplicate, editExistingDuplicate,
            extractErrorTitle,
            // F010 — Yearly planner (grid: windows × options, trip-ideas in cells)
            yearPlans, currentYearPlan, yearFilter, yearActiveConvId, yearMessages,
            yearChatInput, yearSending, yearChatEl,
            editingWindows, draftWindows,
            addingSlotToOption, addingInWindow, newSlot, editingSlotId, editSlot,
            yearActiveConversations,
            openYear, loadYearPlans, setYearFilter,
            createYearPlan, openYearPlan, reloadYearPlan, renameYearPlan,
            editYearIntent, deleteYearPlan,
            startEditWindows, cancelEditWindows, addWindowRow, removeWindowRow, saveWindows, describeWindow,
            createOption, renameOption, editOptionSummary, forkOption,
            markOptionChosen, unpickOption, excludeOption, unexcludeOption, deleteOption,
            slotsReferencingWindow, confirmRemoveWindowRow,
            focusedOptionIds, isOptionFocused, hasFocusedOptions,
            toggleOptionFocus, clearOptionFocus, shouldShowOption, ideaDisplayName,
            ideasInCell, activeIdeasInCell, excludedIdeasInCell,
            startAddIdeaInCell, cancelAddSlot, saveNewSlot,
            startEditSlot, saveSlotEdit, cancelSlotEdit, deleteSlot,
            acceptSlot, unreviewSlot, excludeIdea, unexcludeIdea,
            startTripForSlot, openSlotTrip, unlinkSlotTrip, linkExistingTrip,
            isExcludedShownInCell, toggleShowExcludedInCell,
            showExcludedOptions, toggleShowExcludedOptions,
            openIdeaMenuId, toggleIdeaMenu, closeIdeaMenu, ideaMenuUnreview,
            editingYearPlanField, yearPlanFieldDraft,
            startEditYearPlanField, cancelEditYearPlanField, saveYearPlanField,
            editingOptionField, optionFieldDraft,
            startEditOptionField, cancelEditOptionField, saveOptionField, isEditingOptionField,
            askAIForOptions, askAISuggestForCell,
            dialogKind, dialogCtx, dialogSubmitting, closeDialog,
            submitExcludeOption, submitExcludeIdea,
            submitAskOptions, submitSuggestCell, submitLinkTrip,
            switchYearConversation, newYearConversation, sendYearMessage,
            formatSlotSpan,
        };
    },
}).mount('#app');
