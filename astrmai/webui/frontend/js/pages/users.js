function usersPage() {
    return {
        users: [],
        loading: false,
        
        // Detailed View State
        activeUser: null,
        
        // Slices Modal State
        showSliceModal: false,
        sliceModalMode: 'add', // 'add' or 'edit'
        sliceModalType: '', // 'memory_points', etc.
        sliceModalIndex: -1,
        sliceModalContent: '',

        async init() {
            window.addEventListener('hashchange', () => {
                const hash = location.hash.replace('#', '') || 'login';
                if (hash === 'users') {
                    this.activeUser = null;
                    this.loadUsers();
                }
            });
            if (location.hash.replace('#','') === 'users') this.loadUsers();
        },
        
        async loadUsers() {
            this.loading = true;
            try {
                this.users = await window.api.get('/users');
            } catch(e) {
                // error handled silently or with global interceptor
            } finally {
                this.loading = false;
            }
        },
        
        openUserDetail(u) {
            this.activeUser = JSON.parse(JSON.stringify(u)); // Deep copy to avoid mutating list directly
        },
        
        closeUserDetail() {
            this.activeUser = null;
            this.loadUsers(); // Refresh list to get latest scores/tags
        },

        async saveBasicInfo() {
            try {
                await window.api.patch(`/users/${window.api.segment(this.activeUser.user_id)}`, {
                    nickname: this.activeUser.nickname,
                    nickname_reason: this.activeUser.nickname_reason,
                    social_score: parseFloat(this.activeUser.social_score || 0),
                    identity: this.activeUser.identity,
                    tags: this.activeUser.tags,
                    persona_analysis: this.activeUser.persona_analysis
                });
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '基础画像保存成功', type: 'success' }}));
                
                // Update local list without re-fetching everything
                const idx = this.users.findIndex(u => u.user_id === this.activeUser.user_id);
                if (idx !== -1) {
                    this.users[idx] = { ...this.activeUser };
                }
            } catch(e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '基础资料保存失败', type: 'error' }}));
            }
        },

        async deleteUser() {
            if(await window.app.confirm({title: '极度危险: 清除用户数据', message: `即将彻底清除用户 ${this.activeUser.user_id} 的所有记忆与画像档案，此操作极度危险，是否继续？`, type: 'danger', confirmText: '彻底清除'})) {
                try {
                    await window.api.delete(`/users/${window.api.segment(this.activeUser.user_id)}`);
                    window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '用户已抹除', type: 'success' }}));
                    this.closeUserDetail();
                } catch(e) {
                    window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '抹除失败', type: 'error' }}));
                }
            }
        },

        // --- Slice CRUD ---
        
        openAddSlice(type) {
            this.sliceModalMode = 'add';
            this.sliceModalType = type;
            this.sliceModalContent = '';
            this.showSliceModal = true;
        },

        openEditSlice(type, index) {
            this.sliceModalMode = 'edit';
            this.sliceModalType = type;
            this.sliceModalIndex = index;
            this.sliceModalContent = this.activeUser[type][index];
            this.showSliceModal = true;
        },

        async saveSlice() {
            if (!this.sliceModalContent.trim()) return;
            
            try {
                if (this.sliceModalMode === 'add') {
                    await window.api.post(`/users/${window.api.segment(this.activeUser.user_id)}/slices`, { 
                        type: this.sliceModalType, 
                        content: this.sliceModalContent 
                    });
                    if (!this.activeUser[this.sliceModalType]) this.activeUser[this.sliceModalType] = [];
                    this.activeUser[this.sliceModalType].push(this.sliceModalContent);
                } else {
                    await window.api.put(`/users/${window.api.segment(this.activeUser.user_id)}/slices/${window.api.segment(this.sliceModalIndex)}`, { 
                        type: this.sliceModalType, 
                        content: this.sliceModalContent 
                    });
                    this.activeUser[this.sliceModalType][this.sliceModalIndex] = this.sliceModalContent;
                }
                
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '切片操作成功', type: 'success' }}));
                this.showSliceModal = false;
                
                // Update list in background
                const idx = this.users.findIndex(u => u.user_id === this.activeUser.user_id);
                if (idx !== -1) {
                    this.users[idx] = { ...this.activeUser };
                }
            } catch(e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '切片操作失败', type: 'error' }}));
            }
        },

        async delSlice(type, index) {
             if(await window.app.confirm({title: '确认删除切片', message: '确定要删除此特征切片吗？', type: 'danger', confirmText: '确认删除'})) {
                 try {
                     await window.api.delete(`/users/${window.api.segment(this.activeUser.user_id)}/slices/${window.api.segment(index)}?type=${encodeURIComponent(type)}`);
                     this.activeUser[type].splice(index, 1);
                     
                     // Update list in background
                     const idx = this.users.findIndex(u => u.user_id === this.activeUser.user_id);
                     if (idx !== -1) {
                         this.users[idx] = { ...this.activeUser };
                     }
                 } catch(e) {
                     window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '切片删除失败', type: 'error' }}));
                 }
             }
        },
        
        getSliceConfig(type) {
            const map = {
                'memory_points': { title: '长期记忆点', icon: 'brain-circuit', color: 'blue' },
                'identity_points': { title: '身份认知', icon: 'fingerprint', color: 'purple' },
                'preference_points': { title: '喜好倾向', icon: 'heart', color: 'rose' },
                'relationship_points': { title: '关系纽带', icon: 'link', color: 'amber' },
                'speech_style_points': { title: '语言风格', icon: 'message-square-quote', color: 'emerald' }
            };
            return map[type] || { title: type, icon: 'list', color: 'slate' };
        }
    }
}
