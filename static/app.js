const API_URL = '/v1/chat/completions';
let messageHistory = [];
let hasStartedChat = false;
let amapInstance = null;
let sessionId = sessionStorage.getItem('zua_session_id') || '';

// Save/restore chat history to sessionStorage (per-tab, cleared on tab close)
function restoreChatHistory() {
    try {
        const saved = sessionStorage.getItem('zua_chat_history');
        if (saved) {
            const msgs = JSON.parse(saved);
            if (Array.isArray(msgs) && msgs.length > 0) {
                messageHistory = msgs;
                hasStartedChat = true;
                document.getElementById('mainHeader').classList.add('compact');
                const welcome = document.getElementById('welcomeScreen');
                if (welcome) welcome.style.display = 'none';
                msgs.forEach(m => appendMessageRow(m.role === 'user' ? 'user' : 'ai', m.content));
                return true;
            }
        }
    } catch (e) { /* ignore corrupt data */ }
    return false;
}

function saveChatHistory() {
    try {
        sessionStorage.setItem('zua_chat_history', JSON.stringify(messageHistory));
    } catch (e) { /* ignore quota exceeded */ }
}

// Save session_id from response headers or JSON body
function saveSessionId(id) {
    if (id) {
        sessionId = id;
        sessionStorage.setItem('zua_session_id', id);
    }
}

marked.setOptions({ breaks: true, gfm: true });

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebarOverlay');
    if (window.innerWidth <= 768) {
        sidebar.classList.toggle('active');
        overlay.classList.toggle('active');
    } else {
        sidebar.classList.toggle('collapsed');
    }
}

function openScoreModal() {
    if (window.innerWidth <= 768) {
        document.getElementById('sidebar').classList.remove('active');
        document.getElementById('sidebarOverlay').classList.remove('active');
    }
    const modal = document.getElementById('scoreModal');
    modal.style.display = 'flex';
    requestAnimationFrame(() => modal.classList.add('active'));
}

function closeScoreModal() {
    const modal = document.getElementById('scoreModal');
    modal.classList.remove('active');
    setTimeout(() => { modal.style.display = 'none'; }, 250);
}

function submitScoreModal() {
    const prov = document.getElementById('provinceSelect').value;
    const year = document.getElementById('yearSelect').value;
    closeScoreModal();
    sendQuery('请查询 ' + year + '年 ' + prov + ' 考生分数数据，列出完整信息。');
}

function openMapModal() {
    if (window.innerWidth <= 768) {
        document.getElementById('sidebar').classList.remove('active');
        document.getElementById('sidebarOverlay').classList.remove('active');
    }
    const modal = document.getElementById('mapModal');
    modal.style.display = 'flex';
    setTimeout(() => {
        modal.classList.add('active');
        if (!amapInstance) {
            amapInstance = new AMap.Map('amapContainer', {
                zoom: 15, center: [113.7864966, 34.786103], viewMode: '2D'
            });
            amapInstance.add(new AMap.Marker({
                position: [113.7864966, 34.786103],
                title: '郑州航空工业管理学院(龙子湖校区)'
            }));
        } else {
            amapInstance.resize();
        }
    }, 10);
}

function closeMapModal() {
    const modal = document.getElementById('mapModal');
    modal.classList.remove('active');
    setTimeout(() => { modal.style.display = 'none'; }, 250);
}

function openLightbox(src) {
    const lb = document.getElementById('lightbox');
    lb.textContent = '';
    const img = document.createElement('img');
    img.src = src;
    img.alt = 'preview';
    img.style.maxWidth = '92%';
    img.style.maxHeight = '92%';
    img.style.borderRadius = '8px';
    lb.appendChild(img);
    lb.classList.add('active');
}

function closeLightbox() {
    document.getElementById('lightbox').classList.remove('active');
}

function startNewChat() {
    showChatView();
    sessionId = '';
    sessionStorage.removeItem('zua_session_id');
    sessionStorage.removeItem('zua_chat_history');
    messageHistory = [];
    hasStartedChat = false;
    document.getElementById('mainHeader').classList.remove('compact');
    const chatbox = document.getElementById('chatbox');
    chatbox.textContent = '';
    const wb = document.createElement('div');
    wb.className = 'welcome-screen';
    wb.id = 'welcomeScreen';
    wb.innerHTML = '<h2>欢迎咨询郑州航院</h2><div class="quick-tags">' +
        '<button class="tag-btn" onclick="openScoreModal()">历年分数查询</button>' +
        '<button class="tag-btn" onclick="sendQuery(\'计算机科学与技术专业属于哪个学院？\')">王牌专业解读</button>' +
        '<button class="tag-btn" onclick="sendQuery(\'介绍一下学校\')">介绍学校</button></div>';
    chatbox.appendChild(wb);
    if (window.innerWidth <= 768) toggleSidebar();
}

function appendMessageRow(role, content) {
    const chatbox = document.getElementById('chatbox');
    const row = document.createElement('div');
    row.className = 'message-row ' + (role === 'user' ? 'user' : 'ai');

    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    if (role === 'ai') {
        const img = document.createElement('img');
        img.src = './img.png';
        img.alt = 'ZUA';
        avatar.appendChild(img);
    } else {
        avatar.textContent = '我';
    }

    const msgContent = document.createElement('div');
    msgContent.className = 'bubble';

    if (content) {
        const rendered = role === 'user' ? content : marked.parse(content);
        msgContent.innerHTML = rendered;
        if (role === 'ai') {
            msgContent.querySelectorAll('img').forEach(img => {
                img.addEventListener('click', () => openLightbox(img.src));
            });
        }
    }

    row.appendChild(avatar);
    row.appendChild(msgContent);
    chatbox.appendChild(row);
    chatbox.scrollTo({ top: chatbox.scrollHeight, behavior: 'smooth' });
    return msgContent;
}

function showTypingDots(el) {
    const dots = document.createElement('div');
    dots.className = 'typing-dots';
    for (let i = 0; i < 3; i++) {
        const s = document.createElement('span');
        dots.appendChild(s);
    }
    el.appendChild(dots);
}

async function sendQuery(queryText) {
    showChatView();
    const inputEl = document.getElementById('userInput');
    const text = queryText || inputEl.value.trim();
    if (!text) return;

    if (!hasStartedChat) {
        document.getElementById('mainHeader').classList.add('compact');
        hasStartedChat = true;
    }
    if (window.innerWidth <= 768 && document.getElementById('sidebar').classList.contains('active')) {
        toggleSidebar();
    }

    const welcome = document.getElementById('welcomeScreen');
    if (welcome) welcome.style.display = 'none';

    inputEl.value = '';
    document.getElementById('sendBtn').disabled = true;

    appendMessageRow('user', text);
    messageHistory.push({ role: "user", content: text });

    const aiContentEl = appendMessageRow('ai', '');
    showTypingDots(aiContentEl);

    try {
        const response = await fetch(API_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                model: "default",
                messages: messageHistory,
                temperature: 0.2,
                stream: true,
                session_id: sessionId || null
            })
        });

        // Save session_id from response header (works for both stream and non-stream)
        saveSessionId(response.headers.get('X-Session-Id'));

        if (!response.ok) throw new Error('网络请求失败');

        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let aiFullText = "";
        let buffer = "";

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (line.trim().startsWith('data: ')) {
                    const jsonStr = line.trim().substring(6);
                    if (jsonStr === '[DONE]') break;
                    try {
                        const data = JSON.parse(jsonStr);
                        const token = data.choices[0].delta.content;
                        if (token) {
                            aiFullText += token;
                            aiContentEl.innerHTML = marked.parse(aiFullText);
                            document.getElementById('chatbox').scrollTop = document.getElementById('chatbox').scrollHeight;
                        }
                    } catch (e) { /* skip */ }
                }
            }
        }

        aiContentEl.innerHTML = marked.parse(aiFullText);
        aiContentEl.querySelectorAll('img').forEach(img => {
            img.addEventListener('click', () => openLightbox(img.src));
        });

        messageHistory.push({ role: "assistant", content: aiFullText });
        saveChatHistory();
        if (messageHistory.length > 6) {
            messageHistory = messageHistory.slice(-6);
        }

    } catch (error) {
        console.error(error);
        aiContentEl.textContent = "抱歉，系统响应异常，请确保后端服务已启动并稍后重试。";
    } finally {
        document.getElementById('sendBtn').disabled = false;
        inputEl.focus();
    }
}

function handleKeyPress(event) {
    if (event.key === 'Enter') sendQuery();
}

function showChatView() {
    document.getElementById('chatbox').style.display = 'block';
    document.getElementById('inputArea').style.display = 'block';
    document.getElementById('docContainer').style.display = 'none';
    document.querySelector('#mainHeader h1').textContent = 'ZUA 招生智能问答助手';
}

async function loadMarkdownDocument(filename, title) {
    if (window.innerWidth <= 768 && document.getElementById('sidebar').classList.contains('active')) {
        toggleSidebar();
    }
    document.getElementById('chatbox').style.display = 'none';
    document.getElementById('inputArea').style.display = 'none';
    document.getElementById('docContainer').style.display = 'block';
    document.querySelector('#mainHeader h1').textContent = title;
    document.getElementById('mainHeader').classList.add('compact');

    const docContent = document.getElementById('docContent');
    docContent.textContent = '加载中...';

    try {
        const response = await fetch('./' + filename);
        if (!response.ok) throw new Error('文件未找到');
        let mdText = await response.text();
        mdText = mdText.replace(/<Badge.*?\/>/g, '')
                       .replace(/\[\[toc\]\]/g, '')
                       .replace(/::: (warning|tip|details)(.*)\n([\s\S]*?):::/g, (match, type, title, content) => {
            const blockTitle = title.trim() || (type === 'warning' ? '注意' : '提示');
            return '> **' + blockTitle + '**\n> \n> ' + content.trim().split('\n').join('\n> ') + '\n';
        });
        docContent.innerHTML = marked.parse(mdText);
    } catch (error) {
        docContent.textContent = '加载失败：无法读取 ' + filename + '。';
    }
}

// Restore chat history on page load
restoreChatHistory();
