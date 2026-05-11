const api = {
    base: '/api',
    async request(method, path, body) {
        const token = localStorage.getItem('token');
        const headers = {
            'Content-Type': 'application/json'
        };
        
        if (token) {
            headers['Authorization'] = `Bearer ${token}`;
        }

        const options = {
            method,
            headers
        };

        if (body !== undefined) {
            options.body = JSON.stringify(body);
        }

        try {
            const res = await fetch(this.base + path, options);
            
            if (res.status === 401) {
                // Ignore 401 on login and verify endpoints internally
                if (path !== '/auth/login' && path !== '/auth/verify') {
                    localStorage.removeItem('token');
                    window.location.hash = 'login';
                }
                throw res;
            }

            // For APIs that might not return JSON, handle gracefully
            const isJson = res.headers.get('content-type')?.includes('application/json');
            const data = isJson ? await res.json() : await res.text();

            if (!res.ok) {
                throw { status: res.status, data };
            }

            return data;
        } catch (error) {
            // Re-throw for specific handling in components
            throw error;
        }
    },

    get: (path) => api.request('GET', path),
    post: (path, body) => api.request('POST', path, body),
    patch: (path, body) => api.request('PATCH', path, body),
    put: (path, body) => api.request('PUT', path, body),
    delete: (path) => api.request('DELETE', path),
    segment: (value) => encodeURIComponent(String(value)),
};

// Export for global usage if needed, though in Alpine it can be global var
window.api = api;
