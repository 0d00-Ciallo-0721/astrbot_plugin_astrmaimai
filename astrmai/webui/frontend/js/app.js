function appState() {
    return {
        currentPage: 'login',
        token: localStorage.getItem('token'),
        
        // Toast System State
        toasts: [],
        toastIdCounter: 0,
        
        // Modal System State
        modal: {
            open: false,
            title: '',
            content: '',
            payload: null,
            component: '' // used if specific x-html or dynamic component is needed
        },

        // Confirm System State
        confirm: {
            open: false,
            title: '确认操作',
            message: '您确定要执行此操作吗？',
            type: 'default', // 'default' or 'danger'
            confirmText: '', // optional custom text
            onConfirm: null,
            onCancel: null
        },

        init() {
            window.app = {
                confirm: (titleOrOptions, message, type = 'default') => {
                    const options = typeof titleOrOptions === 'object'
                        ? titleOrOptions
                        : { title: titleOrOptions, message, type };
                    return this.openConfirm(options || {});
                },
                openConfirm: (options) => this.openConfirm(options || {}),
                toast: (message, type = 'info') => this.showToast(message, type),
                showToast: (message, type = 'info') => this.showToast(message, type),
            };
            window.addEventListener('hashchange', () => this.route());
            this.route();
        },

        route() {
            const hash = location.hash.replace('#', '') || 'login';
            
            // Re-check token on route change
            this.token = localStorage.getItem('token');

            if (hash !== 'login' && !this.token) {
                location.hash = 'login';
                return;
            }
            this.currentPage = hash;
        },

        navigate(page) {
            location.hash = page;
        },

        async logout() {
            if (await this.openConfirm({title: '确认退出', message: '您确定要注销当前会话吗？', type: 'danger', confirmText: '确认退出'})) {
                localStorage.removeItem('token');
                this.token = null;
                this.navigate('login');
                this.showToast('已安全退出', 'info');
            }
        },

        // Global UI Utilities
        showToast(message, type = 'info') {
            const id = ++this.toastIdCounter;
            this.toasts.push({ id, message, type, show: false });
            
            // Trigger animation frame for CSS transition
            setTimeout(() => {
                const toast = this.toasts.find(t => t.id === id);
                if (toast) toast.show = true;
            }, 10);

            setTimeout(() => {
                const toast = this.toasts.find(t => t.id === id);
                if (toast) toast.show = false;
                setTimeout(() => {
                    this.toasts = this.toasts.filter(t => t.id !== id);
                }, 300); // Wait for fade out animation
            }, 3000);
        },

        openModal(options) {
            this.modal = {
                open: true,
                title: options.title || '',
                content: options.content || '',
                payload: options.payload || null,
                component: options.component || ''
            };
        },

        closeModal() {
            this.modal.open = false;
            setTimeout(() => {
                this.modal = { open: false, title: '', content: '', payload: null, component: '' };
            }, 200); // Matches CSS transition duration
        },

        openConfirm(options) {
            return new Promise((resolve) => {
                this.confirm = {
                    open: true,
                    title: options.title || '确认操作',
                    message: options.message || '您确定要执行此操作吗？',
                    type: options.type || 'default',
                    confirmText: options.confirmText || '',
                    onConfirm: () => {
                        this.closeConfirm();
                        resolve(true);
                    },
                    onCancel: () => {
                        this.closeConfirm();
                        resolve(false);
                    }
                };
            });
        },

        closeConfirm() {
            this.confirm.open = false;
        }
    }
}
