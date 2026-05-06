document.addEventListener('DOMContentLoaded', () => {
    // Elements
    const stepConfig = document.getElementById('step-config');
    const stepCode = document.getElementById('step-code');
    const stepDownload = document.getElementById('step-download');
    
    const configForm = document.getElementById('config-form');
    const codeForm = document.getElementById('code-form');
    const downloadForm = document.getElementById('download-form');
    
    // Status polling
    let pollingInterval = null;

    function showError(elementId, msg) {
        document.getElementById(elementId).textContent = msg;
    }

    function clearError(elementId) {
        document.getElementById(elementId).textContent = '';
    }

    function setLoading(btnId, loaderId, isLoading) {
        const btn = document.getElementById(btnId);
        const loader = document.getElementById(loaderId);
        btn.disabled = isLoading;
        if (isLoading) {
            loader.classList.remove('hidden');
        } else {
            loader.classList.add('hidden');
        }
    }

    // Step 1: Login
    configForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        clearError('login-error');
        setLoading('btn-login', 'login-loader', true);

        const apiId = document.getElementById('api-id').value;
        const apiHash = document.getElementById('api-hash').value;
        const phone = document.getElementById('phone').value;

        try {
            const res = await fetch('/api/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ api_id: parseInt(apiId), api_hash: apiHash, phone: phone })
            });
            const data = await res.json();

            if (!res.ok) throw new Error(data.detail || 'Login failed');

            stepConfig.classList.remove('active');
            
            if (data.status === 'needs_code') {
                stepCode.classList.add('active');
            } else if (data.status === 'authorized') {
                stepDownload.classList.add('active');
            }
        } catch (err) {
            showError('login-error', err.message);
        } finally {
            setLoading('btn-login', 'login-loader', false);
        }
    });

    // Step 2: Verify Code
    codeForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        clearError('verify-error');
        setLoading('btn-verify', 'verify-loader', true);

        const code = document.getElementById('verification-code').value;
        const password = document.getElementById('2fa-password').value;

        try {
            const res = await fetch('/api/verify_code', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ code: code, password: password })
            });
            const data = await res.json();

            if (!res.ok) throw new Error(data.detail || 'Verification failed');

            if (data.status === 'needs_password') {
                document.getElementById('password-group').classList.remove('hidden');
                throw new Error('2FA Password required');
            } else if (data.status === 'authorized') {
                stepCode.classList.remove('active');
                stepDownload.classList.add('active');
            }
        } catch (err) {
            showError('verify-error', err.message);
        } finally {
            setLoading('btn-verify', 'verify-loader', false);
        }
    });

    // Step 3: Start Download
    downloadForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        clearError('download-error');
        
        const chatName = document.getElementById('chat-name').value;
        const concurrent = parseInt(document.getElementById('concurrent').value);
        const btnStart = document.getElementById('btn-start');

        btnStart.disabled = true;
        document.getElementById('progress-container').classList.remove('hidden');

        try {
            const res = await fetch('/api/start_download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chat_name: chatName, concurrent_downloads: concurrent })
            });
            const data = await res.json();

            if (!res.ok) throw new Error(data.detail || 'Failed to start');
            
            // Start polling
            if (pollingInterval) clearInterval(pollingInterval);
            pollingInterval = setInterval(pollStatus, 800);
            
        } catch (err) {
            showError('download-error', err.message);
            btnStart.disabled = false;
        }
    });

    async function pollStatus() {
        try {
            const res = await fetch('/api/status');
            const data = await res.json();
            
            const statusText = document.getElementById('status-text');
            const statusCount = document.getElementById('status-count');
            const progressFill = document.getElementById('progress-fill');

            statusText.textContent = data.message;
            statusCount.textContent = `${data.current} / ${data.total}`;
            
            let percent = 0;
            if (data.total > 0) {
                percent = (data.current / data.total) * 100;
            }
            progressFill.style.width = `${percent}%`;

            if (!data.running && data.message !== "Idle" && !data.message.includes("Gathering")) {
                clearInterval(pollingInterval);
                document.getElementById('btn-start').disabled = false;
            }
        } catch (err) {
            console.error('Polling error:', err);
        }
    }
});
