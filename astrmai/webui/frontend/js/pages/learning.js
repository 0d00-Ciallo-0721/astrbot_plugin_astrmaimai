function learningPage() {
    return {
        // Proactive Status
        proactiveStatus: null,
        dreamStatus: null,
        diaryStatus: null,
        wakeupStatus: null,
        learningStatus: null,
        
        // Memory Feedback
        feedbackItems: [],
        feedbackSources: [],
        
        // Chat Activity
        activeChats: [],
        
        loading: false,
        pollInterval: null,

        async init() {
            window.addEventListener('hashchange', () => {
                const hash = location.hash.replace('#', '') || 'login';
                if (hash === 'learning') {
                    this.loadAll();
                    this.startPolling();
                } else {
                    this.stopPolling();
                }
            });
            if (location.hash.replace('#','') === 'learning') {
                this.loadAll();
                this.startPolling();
            }
        },

        async loadAll() {
            if (!this.proactiveStatus) this.loading = true;
            try {
                // Fetch basic statuses
                const [
                    proactiveRes, 
                    dreamRes, 
                    diaryRes, 
                    wakeupRes, 
                    learningRes,
                    feedbackRes,
                    sourcesRes,
                    chatsRes
                ] = await Promise.all([
                    window.api.get('/proactive/status').catch(() => null),
                    window.api.get('/proactive/dream/status').catch(() => null),
                    window.api.get('/proactive/diary/status').catch(() => null),
                    window.api.get('/proactive/wakeup/status').catch(() => null),
                    window.api.get('/learning/status').catch(() => null),
                    window.api.get('/memory-feedback?limit=30').catch(() => ({items:[]})),
                    window.api.get('/memory-feedback/sources').catch(() => ({items:[]})),
                    window.api.get('/chats/active?max_age_seconds=1800').catch(() => ({items:[]}))
                ]);

                this.proactiveStatus = proactiveRes?.data || null;
                this.dreamStatus = dreamRes?.data || null;
                this.diaryStatus = diaryRes?.data || null;
                this.wakeupStatus = wakeupRes?.data || null;
                this.learningStatus = learningRes?.data || null;
                
                this.feedbackItems = feedbackRes?.items || [];
                this.feedbackSources = sourcesRes?.items || [];
                
                // Fetch details for active chats
                if (chatsRes?.items?.length) {
                    const chatPromises = chatsRes.items.map(chatId => 
                        window.api.get(`/chats/${window.api.segment(chatId)}/runtime`).catch(() => null)
                    );
                    const chatDetails = await Promise.all(chatPromises);
                    this.activeChats = chatDetails.filter(c => c && c.data).map(c => c.data).sort((a,b) => b.latest_activity_ts - a.latest_activity_ts);
                } else {
                    this.activeChats = [];
                }
                
            } catch (e) {
                console.error("Learning page polling error:", e);
            } finally {
                this.loading = false;
            }
        },

        startPolling() {
            this.stopPolling();
            this.pollInterval = setInterval(() => {
                this.loadAll();
            }, 5000);
        },
        
        stopPolling() {
            if (this.pollInterval) {
                clearInterval(this.pollInterval);
                this.pollInterval = null;
            }
        },
        
        // --- Actions ---
        
        async triggerDream() {
            try {
                await window.api.post('/proactive/dream/run-once');
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `已派发造梦 (Dream) 任务到队列`, type: 'success' }}));
            } catch (e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `触发失败或依赖未绑定`, type: 'error' }}));
            }
        },
        
        async triggerDiary() {
            try {
                await window.api.post('/proactive/diary/run-once');
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `已派发日记 (Diary) 任务到队列`, type: 'success' }}));
            } catch (e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `触发失败或依赖未绑定`, type: 'error' }}));
            }
        },
        
        async triggerReflect(chatId) {
            try {
                await window.api.post(`/learning/reflect/run-once?chat_id=${encodeURIComponent(chatId)}`);
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `已派发 ${chatId} 的反思总结任务`, type: 'success' }}));
            } catch (e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `触发反思失败`, type: 'error' }}));
            }
        },
        
        async deleteFeedback(feedbackId) {
            const result = await window.app.confirm("删除反馈", "确定要禁用这条认知反馈吗？当前后端不会物理删除底层记录。");
            if (!result) return;
            
            try {
                await window.api.post(`/memory-feedback/${window.api.segment(feedbackId)}/disable`);
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `已清除反馈`, type: 'success' }}));
                this.loadAll(); // reload instantly
            } catch (e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `清除反馈失败`, type: 'error' }}));
            }
        },

        async clearChatRuntime(chatId) {
            const ok = await window.app.confirm({
                title: '清理会话运行态',
                message: `确定清理 ${chatId} 的等待目标、活动态和 Heartflow 冷却吗？`,
                type: 'danger'
            });
            if (!ok) return;

            try {
                await window.api.post(`/chats/${window.api.segment(chatId)}/runtime/clear`);
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `会话状态已清理`, type: 'success' }}));
                this.loadAll();
            } catch (e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `清理会话状态失败`, type: 'error' }}));
            }
        },
        
        formatTime(ts) {
            if (!ts) return 'Unknown';
            const d = new Date(ts * 1000);
            return d.toLocaleString();
        }
    }
}
