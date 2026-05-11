function reviewsPage() {
    return {
        tab: 'pending', // 'pending', 'all'
        
        pendingItems: [],
        allItems: [],
        
        loading: false,
        total: 0,
        page: 1,
        pageSize: 20,
        
        filters: { status: '', group_id: '', keyword: '' },
        selectedIds: [], 

        async init() {
            window.addEventListener('hashchange', () => {
                const hash = location.hash.replace('#', '') || 'login';
                if (hash === 'reviews') this.switchTab(this.tab);
            });
            if (location.hash.replace('#','') === 'reviews') this.switchTab(this.tab);
        },

        switchTab(t) {
            this.tab = t;
            this.selectedIds = [];
            this.page = 1;
            if (t === 'pending') {
                this.loadPending();
            } else {
                this.loadAll();
            }
        },

        async loadPending() {
            this.loading = true;
            try {
                this.pendingItems = await window.api.get('/reviews/pending');
            } catch (e) {
                // Ignore silent errors
            } finally {
                this.loading = false;
            }
        },

        async loadAll() {
            this.loading = true;
            try {
                const params = new URLSearchParams({
                    page: this.page,
                    page_size: this.pageSize
                });
                if (this.filters.status) params.append('status', this.filters.status);
                if (this.filters.group_id) params.append('group_id', this.filters.group_id);
                if (this.filters.keyword) params.append('keyword', this.filters.keyword);
                
                const res = await window.api.get(`/reviews?${params.toString()}`);
                this.allItems = res.items || [];
                this.total = res.total || 0;
            } catch (e) {
                // Ignore silent errors
            } finally {
                this.loading = false;
            }
        },
        
        applyFilters() {
            this.page = 1;
            this.loadAll();
        },

        async submitReview(id, action, currentItem = null) {
            try {
                let payload = { action };
                if (currentItem) {
                    payload.replacement = currentItem.expression;
                    payload.weight = parseFloat(currentItem.weight);
                }
                await window.api.post(`/reviews/${window.api.segment(id)}/submit`, payload);
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `已处理该条目`, type: 'success' }}));
                this.loadPending(); 
            } catch (e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '审核提交失败', type: 'error' }}));
            }
        },

        async batchReview(action) {
            if (this.selectedIds.length === 0) return;
            try {
                await window.api.post('/reviews/batch', { ids: this.selectedIds, action });
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `批量 ${action} 成功`, type: 'success' }}));
                this.selectedIds = [];
                this.loadPending();
            } catch (e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '批量操作失败', type: 'error' }}));
            }
        },
        
        toggleSelectAll(e) {
            if (e.target.checked) {
                this.selectedIds = this.pendingItems.map(i => i.id);
            } else {
                this.selectedIds = [];
            }
        },

        nextPage() {
            if (this.page * this.pageSize < this.total) {
                this.page++;
                this.loadAll();
            }
        },
        
        prevPage() {
            if (this.page > 1) {
                this.page--;
                this.loadAll();
            }
        },

        // Edit/Create Modal
        editItem: { id: null, situation: '', expression: '', style: '', weight: 1.0, group_id: 'GLOBAL' },
        showEditModal: false,
        modalMode: 'edit',
        
        openEditModal(item) {
            this.modalMode = 'edit';
            this.editItem = { ...item };
            this.showEditModal = true;
        },

        openCreateModal() {
            this.modalMode = 'create';
            this.editItem = { id: null, situation: '', expression: '', style: '', weight: 1.0, group_id: 'GLOBAL' };
            this.showEditModal = true;
        },
        
        async saveEdit() {
            try {
                if (this.modalMode === 'edit') {
                    await window.api.put(`/reviews/${window.api.segment(this.editItem.id)}`, { 
                        expression: this.editItem.expression, 
                        style: this.editItem.style, 
                        weight: parseFloat(this.editItem.weight) 
                    });
                    window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '保存成功', type: 'success' }}));
                } else {
                    await window.api.post(`/reviews`, {
                        situation: this.editItem.situation,
                        expression: this.editItem.expression,
                        style: this.editItem.style,
                        weight: parseFloat(this.editItem.weight),
                        group_id: this.editItem.group_id
                    });
                    window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '新增语料成功', type: 'success' }}));
                }
                this.showEditModal = false;
                this.loadAll();
            } catch(e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '操作失败', type: 'error' }}));
            }
        },

        async deleteItem(id) {
            if (await window.app.confirm({title: '极度危险: 永久删除', message: '您确定要永久删除这条表达记录吗？此操作无法撤销。', type: 'danger', confirmText: '永久删除'})) {
                try {
                    await window.api.delete(`/reviews/${window.api.segment(id)}`);
                    window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '删除成功', type: 'success' }}));
                    this.loadAll();
                } catch (e) {
                    window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '删除失败', type: 'error' }}));
                }
            }
        }
    }
}
