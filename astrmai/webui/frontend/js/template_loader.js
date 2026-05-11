const templateMounts = [
    { selector: '#layout-overlays', path: 'components/overlays.html' },
    { selector: '#layout-sidebar', path: 'components/sidebar.html' },
    { selector: '#page-slot-login', path: 'pages/login/index.html' },
    { selector: '#page-slot-dashboard', path: 'pages/dashboard/index.html' },
    { selector: '#page-slot-learning', path: 'pages/learning/index.html' },
    { selector: '#page-slot-settings', path: 'pages/settings/index.html' },
    { selector: '#page-slot-reviews', path: 'pages/reviews/index.html' },
    { selector: '#page-slot-memories', path: 'pages/memories/index.html' },
    { selector: '#page-slot-users', path: 'pages/users/index.html' },
    { selector: '#page-slot-persona', path: 'pages/persona/index.html' },
];

async function injectTemplate(mount) {
    const target = document.querySelector(mount.selector);
    if (!target) {
        return;
    }
    const response = await fetch(mount.path);
    if (!response.ok) {
        throw new Error(`Failed to load template: ${mount.path}`);
    }
    target.innerHTML = await response.text();
}

function mountAlpine() {
    const script = document.createElement('script');
    script.defer = true;
    script.src = 'vendor/alpine.min.js';
    document.body.appendChild(script);
}

document.addEventListener('DOMContentLoaded', async () => {
    for (const mount of templateMounts) {
        await injectTemplate(mount);
    }
    mountAlpine();
});
