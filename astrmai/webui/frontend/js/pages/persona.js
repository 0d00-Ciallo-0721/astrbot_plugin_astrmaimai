function personaPage() {
    return {
        persona: null,
        rawPersona: {},
        loading: false,

        async init() {
            window.addEventListener('hashchange', () => {
                const hash = location.hash.replace('#', '') || 'login';
                if (hash === 'persona') this.loadPersona();
            });
            if (location.hash.replace('#','') === 'persona') this.loadPersona();
        },

        async loadPersona() {
            this.loading = true;
            try {
                const data = await window.api.get('/persona');
                if (!data || Object.keys(data).length === 0) {
                    this.rawPersona = {};
                    this.persona = {
                        summary: "",
                        first_person_rewrite: ""
                    };
                } else {
                    this.rawPersona = { ...data };
                    this.persona = {
                        summary: data.summary || "",
                        first_person_rewrite: data.first_person_rewrite || ""
                    };
                }
            } catch (e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '获取人设缓存失败', type: 'error' } }));
            } finally {
                this.loading = false;
            }
        },

        async savePersona() {
            try {
                const payload = { ...this.rawPersona, ...this.persona };
                await window.api.put('/persona', payload);
                this.rawPersona = { ...payload };
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '人设设定保存成功', type: 'success' } }));
            } catch(e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '保存失败', type: 'error' } }));
            }
        }
    }
}
