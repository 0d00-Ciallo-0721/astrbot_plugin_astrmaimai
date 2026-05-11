function loginPage() {
    return {
        password: '',
        error: '',
        loading: false,

        async init() {
            // Check if existing token is valid on load
            const token = localStorage.getItem('token');
            if (token) {
                this.loading = true;
                try {
                    await window.api.get('/auth/verify');
                    // Token is valid, auto-navigate
                    this.$store?.app?.navigate('dashboard') || (window.location.hash = 'dashboard');
                } catch (e) {
                    // Invalid token, clear it
                    localStorage.removeItem('token');
                } finally {
                    this.loading = false;
                }
            }
        },

        async submit() {
            if (!this.password) return;
            
            this.loading = true;
            this.error = '';

            try {
                const data = await window.api.post('/auth/login', { password: this.password });
                localStorage.setItem('token', data.token);
                // Trigger appState sync if possible, or just navigate
                window.location.hash = 'dashboard';
                // Find parent appState and inject toast
                if (window.dispatchEvent) {
                     window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '登录成功', type: 'success' } }));
                }
            } catch (err) {
                this.error = '密码错误';
                // The shake animation is triggered by reactive class binding in HTML
                
                // Reset error state slightly after to allow re-triggering animation if needed
                setTimeout(() => {
                    // We don't clear the error string, we just remove the shake class via Alpine if we wanted to
                    // but bounding it to error !== '' is fine if we toggle a bool.
                    // Let's use a trick: assigning an object to toggle 'shake' class nicely.
                }, 500);
            } finally {
                this.loading = false;
            }
        }
    }
}
