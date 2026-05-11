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
            this.showChatTraceModal = true;
            this.chatTraceLoading = true;
            try {
                const encodedChat = window.api.segment(chatId);
                const [decisionRes, toolRes] = await Promise.all([
                    window.api.get(`/cognition/chats/${encodedChat}/recent-decisions?limit=20`).catch(() => ({ items: [] })),
                    window.api.get(`/tools/chats/${encodedChat}/recent-calls?limit=20`).catch(() => ({ items: [] })),
                ]);
                this.chatDecisions = decisionRes.items || [];
                this.chatToolCalls = toolRes.items || [];
            } finally {
                this.chatTraceLoading = false;
            }
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
