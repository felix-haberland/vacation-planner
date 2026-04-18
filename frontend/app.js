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
        const newTrip = ref({ name: '', description: '' });
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

        // --- Create trip ---
        async function createTrip() {
            creating.value = true;
            try {
                const trip = await api('/api/trips', {
                    method: 'POST',
                    body: JSON.stringify(newTrip.value),
                });
                const desc = newTrip.value.description;
                newTrip.value = { name: '', description: '' };
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
            // Restore view from URL hash
            const hash = window.location.hash;
            const tripMatch = hash.match(/^#trip\/(\d+)$/);
            if (tripMatch) {
                await openTrip(parseInt(tripMatch[1]));
            }
        });

        return {
            view, trips, currentTrip, messages, chatInput, sending, creating,
            newTrip, messagesContainer, chatInputEl,
            editingMessageId, editingContent, zoomedMessage, zoomQuestions, chatExpanded, activeConvId,
            lastMessageHasQuestions, zoomLastAssistant,
            goHome, createTrip, openTrip, sendMessage, doSend,
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
        };
    },
}).mount('#app');
