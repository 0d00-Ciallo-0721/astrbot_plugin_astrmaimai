function dashboardPage() {
    return {
        currentTab: 'overview', // 'overview', 'heartflow', 'cognition', 'tools'
        stats: null,
        health: null,
        models: null,
        capabilities: null,
        
        heartflowStatus: null,
        heartflowChats: [],
        recentDecisions: [],
        schedulerStatus: null,
        schedulerDueSelection: null,
        schedulerChatLoop: null,
        schedulerChatId: '',
        contextEconomy: null,
        contextEconomyTemplates: [],
        contextEconomyAvailableFamilies: [],
        contextEconomyFilterText: '',
        contextEconomyWorkloadFamily: '',
        contextEconomyQuickView: 'high_rotate',
        contextEconomySortBy: 'rotate',
        toolsStatus: null,
        toolsPolicy: null,
        recentToolCalls: [],
        
        loading: false,
        pollInterval: null,
        
        // Modal state for hidden context
        showHiddenContextModal: false,
        activeHiddenContextStr: '',
        activeHiddenContextChat: '',
        showChatTraceModal: false,
        activeTraceChat: '',
        chatTraceLoading: false,
        chatDecisions: [],
        chatToolCalls: [],
        chatTraceEvents: [],

        async init() {
            window.addEventListener('hashchange', () => {
                const hash = location.hash.replace('#', '') || 'login';
                if (hash === 'dashboard') {
                    this.loadAll();
                    this.startPolling();
                } else {
                    this.stopPolling();
                }
            });
            if (location.hash.replace('#','') === 'dashboard') {
                this.loadAll();
                this.startPolling();
            }
        },
        
        setTab(tab) {
            this.currentTab = tab;
            this.loadAll(); // immediately load new tab data
        },

        async loadAll() {
            if (!this.stats && this.currentTab === 'overview') this.loading = true;
            try {
                // Base APIs
                const promises = [
                    window.api.get('/runtime/health').then(res => this.health = res),
                    window.api.get('/runtime/models').catch(() => null).then(res => this.models = res)
                ];

                if (this.currentTab === 'overview') {
                    promises.push(window.api.get('/dashboard').then(res => this.stats = res));
                    promises.push(window.api.get('/runtime/capabilities').catch(() => null).then(res => this.capabilities = res));
                } else if (this.currentTab === 'heartflow') {
                    promises.push(window.api.get('/heartflow/status').then(res => this.heartflowStatus = res));
                    promises.push(window.api.get('/heartflow/chats').then(res => this.heartflowChats = res.items || []));
                } else if (this.currentTab === 'cognition') {
                    promises.push(window.api.get('/cognition/recent-decisions?limit=30').then(res => this.recentDecisions = res.items || []));
                    promises.push(window.api.get('/cognition/scheduler/status').catch(() => null).then(res => this.schedulerStatus = res));
                    promises.push(
                        window.api.get('/cognition/scheduler/due-selection').catch(() => null).then(async res => {
                            this.schedulerDueSelection = res;
                            const selected = res?.data?.report?.selected || [];
                            if (!this.schedulerChatId && selected.length > 0) {
                                this.schedulerChatId = selected[0];
                            }
                            if (this.schedulerChatId) {
                                await this.loadSchedulerChatLoop(this.schedulerChatId);
                            }
                        })
                    );
                    promises.push(window.api.get('/cognition/context-economy?limit=20').catch(() => null).then(res => {
                        this.contextEconomy = res?.data?.overview || null;
                    }));
                    promises.push(this.loadContextEconomyTemplates());
                } else if (this.currentTab === 'tools') {
                    promises.push(window.api.get('/tools/status').then(res => this.toolsStatus = res));
                    promises.push(window.api.get('/tools/policy').catch(() => null).then(res => this.toolsPolicy = res));
                    promises.push(window.api.get('/tools/recent-calls?limit=30').then(res => this.recentToolCalls = res.items || []));
                }

                await Promise.all(promises);
            } catch (e) {
                // fail silently for polling
            } finally {
                this.loading = false;
            }
        },
        
        async clearCooldowns(chatId) {
            try {
                await window.api.post(`/heartflow/chats/${window.api.segment(chatId)}/cooldowns/clear`);
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `已重置 ${chatId} 的冷却状态`, type: 'success' }}));
                this.loadAll();
            } catch(e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `重置失败`, type: 'error' }}));
            }
        },

        async openHiddenContext(chatId) {
            try {
                const res = await window.api.get(`/heartflow/chats/${window.api.segment(chatId)}/hidden-context`);
                this.activeHiddenContextStr = res.data?.hidden_context || 'No hidden context found.';
                this.activeHiddenContextChat = chatId;
                this.showHiddenContextModal = true;
            } catch(e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `探查潜意识失败`, type: 'error' }}));
            }
        },

        async openChatTraceDetails(chatId) {
            this.activeTraceChat = chatId;
            this.chatDecisions = [];
            this.chatToolCalls = [];
            this.chatTraceEvents = [];
            this.showChatTraceModal = true;
            this.chatTraceLoading = true;
            try {
                const encodedChat = window.api.segment(chatId);
                const [decisionRes, toolRes, traceRes] = await Promise.all([
                    window.api.get(`/cognition/chats/${encodedChat}/recent-decisions?limit=20`).catch(() => ({ items: [] })),
                    window.api.get(`/tools/chats/${encodedChat}/recent-calls?limit=20`).catch(() => ({ items: [] })),
                    window.api.get(`/cognition/chats/${encodedChat}/trace-events?limit=40`).catch(() => ({ items: [] })),
                ]);
                this.chatDecisions = decisionRes.items || [];
                this.chatToolCalls = toolRes.items || [];
                this.chatTraceEvents = traceRes.items || [];
            } finally {
                this.chatTraceLoading = false;
            }
        },

        summarizeFailureEvidence(item) {
            const evidence = item?.failure_evidence || {};
            const parts = [];
            if (evidence.failure_kind) parts.push(`failure=${evidence.failure_kind}`);
            if (Array.isArray(evidence.attempted_models) && evidence.attempted_models.length) {
                parts.push(`models=${evidence.attempted_models.join(', ')}`);
            }
            if (evidence.protocol_passthrough) {
                parts.push(`protocol=${evidence.protocol_type || 'passthrough'}`);
            }
            if (evidence.vision_failure_kind) {
                parts.push(`vision=${evidence.vision_failure_kind}`);
            }
            return parts.join(' | ');
        },

        startPolling() {
            this.stopPolling();
            this.pollInterval = setInterval(() => {
                this.loadAll();
            }, 5000); // Poll every 5 seconds
        },
        
        stopPolling() {
            if (this.pollInterval) {
                clearInterval(this.pollInterval);
                this.pollInterval = null;
            }
        },
        
        // CSS helper for ring chart
        getDashArray(percent) {
            const circumference = 2 * Math.PI * 36; // r=36
            const strokeDasharray = `${(percent / 100) * circumference} ${circumference}`;
            return strokeDasharray;
        },
        
        formatTime(ts) {
            if (!ts) return 'Unknown';
            const d = new Date(ts * 1000);
            return d.toLocaleTimeString();
        },

        capabilityEntries() {
            const data = this.capabilities?.data || {};
            return Object.entries(data).map(([key, value]) => ({ key, value }));
        },

        formatJson(value) {
            try {
                return JSON.stringify(value, null, 2);
            } catch (e) {
                return String(value);
            }
        },

        formatPercent(value) {
            return `${(Number(value || 0) * 100).toFixed(1)}%`;
        },

        formatFamilies(families) {
            const entries = Object.entries(families || {});
            if (!entries.length) return '—';
            return entries
                .sort((a, b) => b[1] - a[1])
                .map(([name, count]) => `${name} (${count})`)
                .join(', ');
        },

        schedulerReport() {
            return this.schedulerDueSelection?.data?.report || {};
        },

        schedulerOverview() {
            return this.schedulerStatus?.data?.overview || {};
        },

        async loadSchedulerChatLoop(chatId = null) {
            const targetChat = (chatId ?? this.schedulerChatId ?? '').trim();
            if (!targetChat) {
                this.schedulerChatLoop = null;
                return;
            }
            this.schedulerChatId = targetChat;
            const encodedChat = window.api.segment(targetChat);
            this.schedulerChatLoop = await window.api.get(`/cognition/scheduler/chats/${encodedChat}`).catch(() => null);
        },

        contextEconomySortParams() {
            if (this.contextEconomySortBy === 'session_reuse') {
                return { sort_by: 'session_reuse', sort_dir: 'asc' };
            }
            if (this.contextEconomySortBy === 'calls') {
                return { sort_by: 'calls', sort_dir: 'desc' };
            }
            return { sort_by: 'rotate', sort_dir: 'desc' };
        },

        setContextEconomyQuickView(view) {
            this.contextEconomyQuickView = view;
            if (view === 'low_reuse') {
                this.contextEconomySortBy = 'session_reuse';
            } else if (view === 'high_traffic') {
                this.contextEconomySortBy = 'calls';
            } else {
                this.contextEconomySortBy = 'rotate';
            }
            return this.applyContextEconomyFilters();
        },

        async loadContextEconomyTemplates() {
            const sort = this.contextEconomySortParams();
            const params = new URLSearchParams({
                limit: '20',
                sort_by: sort.sort_by,
                sort_dir: sort.sort_dir,
            });
            if (this.contextEconomyFilterText.trim()) {
                params.set('template_id', this.contextEconomyFilterText.trim());
            }
            if (this.contextEconomyWorkloadFamily) {
                params.set('workload_family', this.contextEconomyWorkloadFamily);
            }
            const res = await window.api.get(`/cognition/context-economy/templates?${params.toString()}`).catch(() => null);
            this.contextEconomyTemplates = res?.items || [];
            this.contextEconomyAvailableFamilies = res?.available_workload_families || [];
        },

        async applyContextEconomyFilters() {
            if (this.currentTab !== 'cognition') return;
            if (this.contextEconomySortBy === 'session_reuse') {
                this.contextEconomyQuickView = 'low_reuse';
            } else if (this.contextEconomySortBy === 'calls') {
                this.contextEconomyQuickView = 'high_traffic';
            } else {
                this.contextEconomyQuickView = 'high_rotate';
            }
            await this.loadContextEconomyTemplates();
        },

        async copyJson(obj) {
            try {
                await navigator.clipboard.writeText(this.formatJson(obj));
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: 'JSON 已复制到剪贴板', type: 'success' }}));
            } catch (err) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '复制失败', type: 'error' }}));
            }
        }
    }
}
