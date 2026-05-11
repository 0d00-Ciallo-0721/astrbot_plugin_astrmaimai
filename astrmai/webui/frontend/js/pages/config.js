function configPage() {
    return {
        schema: {},
        config: {},
        meta: {},
        loading: true,
        applying: false,
        advancedBusy: false,
        expandAll: false,
        sections: [],
        showRawConfigModal: false,
        rawConfigText: '',
        
        async init() {
            window.addEventListener('hashchange', () => {
                const hash = location.hash.replace('#', '') || 'login';
                if (hash === 'settings') this.loadData();
            });
            if (location.hash.replace('#','') === 'settings') this.loadData();
        },

        async loadData() {
            this.loading = true;
            try {
                // Read from schema, effective config and meta concurrently
                const [schemaData, configData, metaData] = await Promise.all([
                    window.api.get('/config/schema'),
                    window.api.get('/config/effective'),
                    window.api.get('/config/meta')
                ]);
                
                this.schema = schemaData;
                this.config = configData;
                this.meta = metaData;
                
                // Construct sections for UI
                this.sections = Object.keys(schemaData).map(key => ({
                    key,
                    title: key,
                    def: schemaData[key],
                    open: false
                }));
            } catch (err) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '配置加载失败', type: 'error' }}));
            } finally {
                this.loading = false;
            }
        },

        toggleAll() {
            this.expandAll = !this.expandAll;
            this.sections.forEach(s => s.open = this.expandAll);
        },

        async saveSection(sectionKey) {
            try {
                const dataToSave = this.config[sectionKey] || {};
                const res = await window.api.patch(`/config/${window.api.segment(sectionKey)}`, dataToSave);
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `${sectionKey} 保存成功`, type: 'success' }}));
                
                if (res.reload_required) {
                    window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '提示：由于配置涉及核心服务，需重启插件后方可生效。', type: 'info' }}));
                }
                
                // Update meta to reflect pending apply state
                this.meta = await window.api.get('/config/meta');
            } catch (e) {
                if (e.data && e.data.detail && Array.isArray(e.data.detail)) {
                    const msg = e.data.detail
                        .map(d => `${d.path || (d.loc ? d.loc.join('.') : '')}: ${d.message || d.msg || 'Invalid value'}`)
                        .join('\n');
                    window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '校验失败: ' + msg, type: 'error' }}));
                } else {
                    window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '保存失败', type: 'error' }}));
                }
            }
        },

        async resetSection(sectionKey) {
            if (await window.app.confirm({message: `确认重置 ${sectionKey} 为默认值？这可能会导致您的自定义设置丢失。`, type: 'danger'})) {
                try {
                    const res = await window.api.post(`/config/reset/${window.api.segment(sectionKey)}`);
                    this.config[sectionKey] = res.data;
                    window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: `${sectionKey} 已重置`, type: 'success' }}));
                    this.meta = await window.api.get('/config/meta');
                } catch (e) {
                    window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '重置失败', type: 'error' }}));
                }
            }
        },
        
        async applyConfig() {
            if (await window.app.confirm({title: '应用配置', message: '将配置写入并同步至运行时实例？部分配置可能需要重启插件才能生效。'})) {
                this.applying = true;
                try {
                    const res = await window.api.post('/config/apply');
                    if (res.reload_required) {
                        window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '配置已保存，但包含需要重启插件生效的修改。', type: 'info' }}));
                    } else {
                        window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '配置已热重载生效！', type: 'success' }}));
                    }
                    this.meta = await window.api.get('/config/meta');
                } catch (e) {
                    window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '应用配置失败', type: 'error' }}));
                } finally {
                    this.applying = false;
                }
            }
        },

        async saveAllConfig() {
            if (await window.app.confirm({title: '保存全部配置', message: '将当前页面中的全部配置整体写回 config.json，确定继续吗？'})) {
                this.advancedBusy = true;
                try {
                    const res = await window.api.put('/config', this.config);
                    this.config = res.config || this.config;
                    this.meta = await window.api.get('/config/meta');
                    window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '全部配置已保存', type: 'success' }}));
                } catch (e) {
                    window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '保存全部配置失败', type: 'error' }}));
                } finally {
                    this.advancedBusy = false;
                }
            }
        },

        async resetAllConfig() {
            if (await window.app.confirm({title: '重置全部配置', message: '这会用 schema 默认值覆盖当前全部配置，操作风险较高。确定继续吗？', type: 'danger'})) {
                this.advancedBusy = true;
                try {
                    const res = await window.api.post('/config/reset');
                    this.config = res.data || {};
                    this.meta = await window.api.get('/config/meta');
                    window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '全部配置已重置', type: 'success' }}));
                } catch (e) {
                    window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '重置全部配置失败', type: 'error' }}));
                } finally {
                    this.advancedBusy = false;
                }
            }
        },

        async openRawConfig() {
            this.advancedBusy = true;
            try {
                const data = await window.api.get('/config');
                this.rawConfigText = JSON.stringify(data, null, 2);
                this.showRawConfigModal = true;
            } catch (e) {
                window.dispatchEvent(new CustomEvent('toast-notify', { detail: { message: '读取原始配置失败', type: 'error' }}));
            } finally {
                this.advancedBusy = false;
            }
        },

        // Helper to get nested value
        getValue(section, key) {
            if(!this.config[section]) this.config[section] = {};
            return this.config[section][key];
        },
        setValue(section, key, val) {
            if(!this.config[section]) this.config[section] = {};
            this.config[section][key] = val;
        },

        // Helpers for list tags
        addToList(section, key, valueEvent) {
             const val = valueEvent.target.value.trim();
             if(!val) return;
             if(!this.config[section]) this.config[section] = {};
             if(!Array.isArray(this.config[section][key])) this.config[section][key] = [];
             this.config[section][key].push(val);
             valueEvent.target.value = '';
        },
        removeFromList(section, key, index) {
             this.config[section][key].splice(index, 1);
        },

        sectionFields(section) {
            return section?.def?.items || section?.def?.keys || {};
        },

        formatJsonValue(val) {
            try {
                if (val === undefined || val === null) return '';
                return JSON.stringify(val, null, 2);
            } catch (e) {
                return '';
            }
        },
        
        tryParseAndSet(section, key, str) {
            if (!str || str.trim() === '') {
                // allow empty string to mean null/empty dict depending on schema, or just skip
                return true; 
            }
            try {
                const parsed = JSON.parse(str);
                this.setValue(section, key, parsed);
                return true;
            } catch (e) {
                return false;
            }
        },

        formatMetaTime(ts) {
            if (!ts) return '未知';
            return new Date(ts * 1000).toLocaleString();
        }
    }
}
