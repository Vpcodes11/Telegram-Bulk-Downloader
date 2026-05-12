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

    // Step 3: Fetch Media
    const btnFetch = document.getElementById('btn-fetch');
    const selectionArea = document.getElementById('selection-area');
    const folderView = document.getElementById('folder-view');
    const fileView = document.getElementById('file-view');
    const categoryFolders = document.getElementById('category-folders');
    const mediaList = document.getElementById('media-list');
    const selectedCount = document.getElementById('selected-count');
    const currentFolderName = document.getElementById('current-folder-name');
    
    const btnSelectAllGlobal = document.getElementById('btn-select-all-global');
    const btnSelectFolder = document.getElementById('btn-select-folder');
    const btnBackToFolders = document.getElementById('btn-back-to-folders');

    let fetchedMessages = [];
    let categorizedMessages = {};
    let selectedIds = new Set();
    let currentFolder = null;

    btnFetch.addEventListener('click', async () => {
        let chatName = document.getElementById('chat-name').value.trim();
        if (!chatName) return;

        btnFetch.disabled = true;
        btnFetch.textContent = 'Searching...';
        clearError('download-error');

        try {
            const limit = document.getElementById('fetch-limit').value;
            const res = await fetch(`/api/get_messages?chat_name=${encodeURIComponent(chatName)}&limit=${limit}`);
            const data = await res.json();
            
            if (!res.ok) {
                // If it fails, the backend already tries @, but let's show the helpful error
                throw new Error(data.detail || 'Fetch failed');
            }

            fetchedMessages = data.messages;
            if (fetchedMessages.length === 0) {
                showError('download-error', 'No media files found in this chat.');
                return;
            }

            categorizeMessages();
            renderFolders();
            selectionArea.classList.remove('hidden');
            btnFetch.classList.add('hidden');
        } catch (err) {
            showError('download-error', err.message);
        } finally {
            btnFetch.disabled = false;
            btnFetch.textContent = 'Fetch Available Media';
        }
    });

    function categorizeMessages() {
        categorizedMessages = {};
        fetchedMessages.forEach(msg => {
            if (!categorizedMessages[msg.type]) {
                categorizedMessages[msg.type] = [];
            }
            categorizedMessages[msg.type].push(msg);
        });
    }

    const categoryIcons = {
        'photos': '🖼️',
        'videos': '🎬',
        'files': '📂',
        'voice': '🎤',
        'round_video': '📹',
        'gifs': '🎞️',
        'other': '📦'
    };

    function renderFolders() {
        categoryFolders.innerHTML = '';
        Object.keys(categorizedMessages).sort().forEach(type => {
            const messages = categorizedMessages[type];
            const folder = document.createElement('div');
            folder.className = 'folder-card';
            
            const selectedInCategory = messages.filter(m => selectedIds.has(m.id)).length;
            const badge = selectedInCategory > 0 ? `<div class="folder-badge">${selectedInCategory}</div>` : '';
            
            folder.innerHTML = `
                ${badge}
                <div class="folder-icon">${categoryIcons[type] || '📁'}</div>
                <div class="folder-name">${type.charAt(0).toUpperCase() + type.slice(1)}</div>
                <div class="folder-count">${messages.length} items</div>
            `;

            folder.addEventListener('click', () => openFolder(type));
            categoryFolders.appendChild(folder);
        });
        folderView.classList.remove('hidden');
        fileView.classList.add('hidden');
    }

    function openFolder(type) {
        currentFolder = type;
        currentFolderName.textContent = type.charAt(0).toUpperCase() + type.slice(1);
        renderFileList(categorizedMessages[type]);
        folderView.classList.add('hidden');
        fileView.classList.remove('hidden');
    }

    function renderFileList(messages) {
        mediaList.innerHTML = '';
        messages.forEach(msg => {
            const item = document.createElement('div');
            item.className = `media-item ${selectedIds.has(msg.id) ? 'selected' : ''}`;
            item.dataset.id = msg.id;
            
            const date = new Date(msg.date).toLocaleDateString();
            
            item.innerHTML = `
                <div class="media-checkbox"></div>
                <div class="media-info">
                    <div class="media-filename">${msg.filename}</div>
                    <div class="media-meta">
                        <span>${date}</span>
                    </div>
                </div>
            `;

            item.addEventListener('click', () => {
                if (selectedIds.has(msg.id)) {
                    selectedIds.delete(msg.id);
                    item.classList.remove('selected');
                } else {
                    selectedIds.add(msg.id);
                    item.classList.add('selected');
                }
                updateSelectedCount();
            });

            mediaList.appendChild(item);
        });
    }

    function updateSelectedCount() {
        selectedCount.textContent = selectedIds.size;
        // Also update badges if in folder view
        if (!folderView.classList.contains('hidden')) {
            renderFolders();
        }
    }

    btnBackToFolders.addEventListener('click', () => {
        renderFolders();
    });

    btnSelectFolder.addEventListener('click', () => {
        if (!currentFolder) return;
        const messages = categorizedMessages[currentFolder];
        const allSelected = messages.every(m => selectedIds.has(m.id));
        
        if (allSelected) {
            messages.forEach(m => selectedIds.delete(m.id));
        } else {
            messages.forEach(m => selectedIds.add(m.id));
        }
        
        renderFileList(messages);
        updateSelectedCount();
    });

    btnSelectAllGlobal.addEventListener('click', () => {
        const allSelected = fetchedMessages.length > 0 && fetchedMessages.every(m => selectedIds.has(m.id));
        if (allSelected) {
            selectedIds.clear();
        } else {
            fetchedMessages.forEach(m => selectedIds.add(m.id));
        }
        renderFolders();
        updateSelectedCount();
    });

    // Step 4: Start Download
    downloadForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (selectedIds.size === 0) {
            showError('download-error', 'Please select at least one item to download');
            return;
        }
        
        clearError('download-error');
        
        const chatName = document.getElementById('chat-name').value;
        const concurrent = parseInt(document.getElementById('concurrent').value);
        const btnStart = document.getElementById('btn-start');

        btnStart.disabled = true;
        document.getElementById('progress-container').classList.remove('hidden');
        selectionArea.classList.add('hidden');

        try {
            const res = await fetch('/api/start_download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    chat_name: chatName, 
                    concurrent_downloads: concurrent,
                    message_ids: Array.from(selectedIds)
                })
            });
            const data = await res.json();

            if (!res.ok) throw new Error(data.detail || 'Failed to start');
            
            // Start polling — fast refresh for live speed display
            if (pollingInterval) clearInterval(pollingInterval);
            pollingInterval = setInterval(pollStatus, 300);
            
        } catch (err) {
            showError('download-error', err.message);
            btnStart.disabled = false;
            selectionArea.classList.remove('hidden');
        }
    });

    // Speed display elements (injected into progress-container on first poll)
    let speedInjected = false;
    function injectSpeedDisplay() {
        if (speedInjected) return;
        speedInjected = true;
        const container = document.getElementById('progress-container');
        const speedHtml = `
            <div id="speed-row" style="display:flex;gap:12px;margin-top:14px;flex-wrap:wrap;">
                <div class="speed-pill" id="pill-speed">
                    <span class="speed-icon">⚡</span>
                    <span id="speed-val">0.00</span> <span class="speed-unit">MB/s</span>
                </div>
                <div class="speed-pill" id="pill-skipped">
                    <span class="speed-icon">✓</span>
                    <span id="skipped-val">0</span> <span class="speed-unit">skipped</span>
                </div>
                <div class="speed-pill" id="pill-bytes">
                    <span class="speed-icon">💾</span>
                    <span id="bytes-val">0 MB</span>
                </div>
            </div>`;
        container.insertAdjacentHTML('beforeend', speedHtml);
    }

    async function pollStatus() {
        try {
            const res = await fetch('/api/status');
            const data = await res.json();
            
            const statusText = document.getElementById('status-text');
            const statusCount = document.getElementById('status-count');
            const progressFill = document.getElementById('progress-fill');
            const currentTitle = document.getElementById('current-chat-title');

            injectSpeedDisplay();

            statusText.textContent = data.message;
            statusCount.textContent = `${data.current} / ${data.total}`;
            currentTitle.textContent = data.chat_title || 'Extraction in progress';

            // Live speed & stats
            const speedMbps = data.speed_mbps || 0;
            document.getElementById('speed-val').textContent = speedMbps.toFixed(2);
            // Color-code: green when fast, amber when slow
            const pillSpeed = document.getElementById('pill-speed');
            if (speedMbps > 5) pillSpeed.style.borderColor = 'rgba(16,185,129,0.5)';
            else if (speedMbps > 1) pillSpeed.style.borderColor = 'rgba(251,191,36,0.5)';
            else pillSpeed.style.borderColor = '';

            document.getElementById('skipped-val').textContent = data.skipped || 0;

            const mb = ((data.bytes_downloaded || 0) / 1_048_576).toFixed(1);
            document.getElementById('bytes-val').textContent = `${mb} MB`;

            let percent = 0;
            if (data.total > 0) {
                percent = (data.current / data.total) * 100;
            }
            progressFill.style.width = `${percent}%`;

            if (!data.running && data.message !== 'Idle' && !data.message.includes('Gathering')) {
                clearInterval(pollingInterval);
                // Freeze speed at 0 when done
                document.getElementById('speed-val').textContent = '0.00';
                document.getElementById('completion-actions').classList.remove('hidden');
                document.getElementById('btn-start').classList.add('hidden');
                document.getElementById('download-form').classList.add('hidden');
            }
        } catch (err) {
            console.error('Polling error:', err);
        }
    }
});
