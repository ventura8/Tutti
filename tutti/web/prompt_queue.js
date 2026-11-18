// Tutti dashboard: the Chat prompt queue (persist in backend)
'use strict';

function getQueue() {
    return CHAT.prompt_queue || [];
}

async function addPrompt(text) {
    try {
        await chatApi({ action: 'add_prompt', text });
        await loadList();
    } catch (err) {
        say(err.message);
    }
}

async function deletePrompt(id) {
    try {
        await chatApi({ action: 'delete_prompt', id });
        await loadList();
    } catch (err) {
        say(err.message);
    }
}

async function editPrompt(id, text) {
    try {
        await chatApi({ action: 'edit_prompt', id, text });
        await loadList();
    } catch (err) {
        say(err.message);
    }
}

async function reorderQueue(pid, newIndex) {
    try {
        await chatApi({ action: 'reorder_prompt', id: pid, index: newIndex });
        await loadList();
    } catch (err) {
        say(err.message);
    }
}

function renderQueue() {
    const container = document.querySelector('#prompt-queue');
    if (!container) return;
    const queue = getQueue();
    container.innerHTML = '';
    queue.forEach((p, index) => {
        const div = document.createElement('div');
        div.className = 'prompt-item';
        div.draggable = true;
        div.dataset.index = index;
        div.textContent = p.text;
        
        const del = document.createElement('button');
        del.textContent = 'Delete';
        del.className = 'btn btn--text';
        del.onclick = () => deletePrompt(p.id);
        div.appendChild(del);

        const edit = document.createElement('button');
        edit.textContent = 'Edit';
        edit.className = 'btn btn--text';
        edit.onclick = () => {
            const newText = prompt('Edit prompt', p.text);
            if (newText !== null) editPrompt(p.id, newText);
        };
        div.appendChild(edit);
        
        div.ondragstart = (e) => e.dataTransfer.setData('text/plain', p.id);
        div.ondragover = (e) => e.preventDefault();
        div.ondrop = (e) => {
            e.preventDefault();
            const pid = e.dataTransfer.getData('text/plain');
            reorderQueue(pid, index);
        };
        
        container.appendChild(div);
    });
}
