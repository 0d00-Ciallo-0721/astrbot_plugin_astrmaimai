function memoryPage() {
    return {
        currentTab: 'events', // 'events', 'reflections', 'nodes', 'jargon'
        
        // Data
        events: [],
        reflectionsMap: {},
        calendarDays: [],
        currentMonthStr: '',
        activeReflectionDate: null,
        
        nodes: [],
        jargons: [],
        
        loading: false,

        async init() {
            const d = new Date();
            this.currentMonthStr = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`;

            window.addEventListener('hashchange', () => {
                const hash = location.hash.replace('#', '') || 'login';
                if (hash === 'memories') this.loadAll();
            });
            if (location.hash.replace('#','') === 'memories') this.loadAll();
        },
        
        setTab(tab) {
            this.currentTab = tab;
            this.loadAll();
        },

        async loadAll() {
            this.loading = true;
            try {
                if (this.currentTab === 'events') {
                    this.events = await window.api.get('/memories/events');
                } else if (this.currentTab === 'reflections') {
                    await this.loadReflections(this.currentMonthStr);
                } else if (this.currentTab === 'nodes') {
                    this.nodes = await window.api.get('/memories/nodes');
                } else if (this.currentTab === 'jargon') {
                    this.jargons = await window.api.get('/memories/jargon');
                }
            } catch(e) {
                // Ignore silent errors
            } finally {
                this.loading = false;
            }
        },

        // --- Events ---
        newEvent: { narrative: '', memory_kind: 'Misc', importance: 0.5, tags: '' },
        showEventModal: false,
        
        openEventModal() {
            this.newEvent = { narrative: '', memory_kind: 'Misc', importance: 0.5, tags: '' };
            this.showEventModal = true;
        },
        
        async saveEvent() {
            try {
                await window.api.post('/memories/events', this.newEvent);
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '新增事件成功', type: 'success' }}));
                this.showEventModal = false;
                this.loadAll();
            } catch(e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '新增失败', type: 'error' }}));
            }
        },
        
        async delEvent(id) {
            if(await window.app.confirm({title: '极度危险: 删除记忆', message: '您确定要物理删除这条核心记忆事件吗？此操作无法撤销。', type: 'danger', confirmText: '物理删除'})) {
                await window.api.delete('/memories/events/' + window.api.segment(id));
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '记忆已粉碎', type: 'success' }}));
                this.loadAll();
            }
        },

        // --- Reflections ---
        async loadReflections(monthStr) {
            this.currentMonthStr = monthStr;
            const items = await window.api.get(`/memories/reflections?month=${encodeURIComponent(monthStr)}`);
            this.reflectionsMap = {};
            if (Array.isArray(items)) {
                items.forEach(i => { this.reflectionsMap[i.date] = i; });
            }
            
            const [y, m] = monthStr.split('-');
            const daysInMonth = new Date(parseInt(y), parseInt(m), 0).getDate();
            const firstDayIndex = new Date(parseInt(y), parseInt(m)-1, 1).getDay();
            
            const grid = [];
            for(let i=0; i<firstDayIndex; i++) grid.push(null);
            for(let d=1; d<=daysInMonth; d++) {
                grid.push(`${y}-${m.padStart(2,'0')}-${String(d).padStart(2,'0')}`);
            }
            this.calendarDays = grid;
        },

        prevMonth() {
            const [y, m] = this.currentMonthStr.split('-');
            let d = new Date(parseInt(y), parseInt(m)-2, 1);
            this.loadReflections(`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`);
        },

        nextMonth() {
            const [y, m] = this.currentMonthStr.split('-');
            let d = new Date(parseInt(y), parseInt(m), 1);
            this.loadReflections(`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`);
        },
        
        openReflection(date) {
            this.activeReflectionDate = this.activeReflectionDate === date ? null : date;
        },
        
        async saveReflection(date) {
            const text = document.getElementById(`ref-input-${date}`).value;
            try {
                if(this.reflectionsMap[date]) {
                    await window.api.put(`/memories/reflections/${window.api.segment(date)}`, { summary: text });
                } else {
                    await window.api.post(`/memories/reflections`, { date, summary: text, raw_log: '', meta: '{}' });
                }
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '总结保存成功', type: 'success' }}));
                this.loadAll();
                this.activeReflectionDate = null;
            } catch(e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '保存失败', type: 'error' }}));
            }
        },

        async delReflection(date) {
            if(await window.app.confirm({title: '确认删除', message: '删除该日的总结与日志？', type: 'danger', confirmText: '确认删除'})) {
                await window.api.delete(`/memories/reflections/${window.api.segment(date)}`);
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '反思已删除', type: 'success' }}));
                this.loadAll();
                this.activeReflectionDate = null;
            }
        },

        // --- Nodes ---
        newNode: { id: null, name: '', type: 'preference', description: '' },
        showNodeModal: false,
        
        openNodeModal(node = null) {
            if (node) {
                this.newNode = { ...node };
            } else {
                this.newNode = { id: null, name: '', type: 'preference', description: '' };
            }
            this.showNodeModal = true;
        },
        
        async saveNode() {
            try {
                if (this.newNode.id) {
                    await window.api.put(`/memories/nodes/${window.api.segment(this.newNode.id)}`, this.newNode);
                } else {
                    await window.api.post('/memories/nodes', this.newNode);
                }
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '节点已保存', type: 'success' }}));
                this.showNodeModal = false;
                this.loadAll();
            } catch(e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '保存失败', type: 'error' }}));
            }
        },
        
        async delNode(id) {
            if(await window.app.confirm({title: '确认删除实体', message: '删除实体节点将影响图谱完整性，确定删除？', type: 'danger', confirmText: '确认删除'})) {
                await window.api.delete(`/memories/nodes/${window.api.segment(id)}`);
                this.loadAll();
            }
        },

        // --- Jargon ---
        newJargon: { id: null, content: '', meaning: '', is_jargon: 1, is_complete: 1, group_id: 'GLOBAL' },
        showJargonModal: false,
        
        openJargonModal(j = null) {
            if (j) {
                this.newJargon = { ...j };
            } else {
                this.newJargon = { id: null, content: '', meaning: '', is_jargon: 1, is_complete: 1, group_id: 'GLOBAL' };
            }
            this.showJargonModal = true;
        },
        
        async saveJargon() {
            try {
                if (this.newJargon.id) {
                    await window.api.put(`/memories/jargon/${window.api.segment(this.newJargon.id)}`, { 
                        meaning: this.newJargon.meaning, 
                        is_jargon: this.newJargon.is_jargon, 
                        is_complete: this.newJargon.is_complete 
                    });
                } else {
                    await window.api.post('/memories/jargon', this.newJargon);
                }
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '黑话词汇保存成功', type: 'success' }}));
                this.showJargonModal = false;
                this.loadAll();
            } catch(e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '保存失败', type: 'error' }}));
            }
        },

        async delJargon(id) {
             if(await window.app.confirm({title: '确认删除', message: '确定要删除此词条吗？', type: 'danger', confirmText: '确认删除'})) {
                await window.api.delete(`/memories/jargon/${window.api.segment(id)}`);
                this.loadAll();
            }
        },
        
        formatDate(ts) {
            if (!ts) return '';
            const d = new Date(ts * 1000);
            return d.toLocaleDateString();
        }
    }
}
