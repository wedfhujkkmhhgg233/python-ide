/* =====================================================
   STATE
===================================================== */
let currentProject = null;
let currentFile = null;
let expandedFolders = new Set();
let dragPath = null;
let dragType = null;
let uploadTargetDir = "";
const editor = document.getElementById("editor");
/*
 * CodeMirror gives us syntax highlighting, auto-indent,
 * bracket matching, and auto-closing brackets on top of
 * the plain textarea. `cm` is what the rest of the app
 * reads/writes instead of editor.value directly.
 */
/*
 * Deletes the line(s) the cursor/selection is on,
 * including the line break - like VS Code's Ctrl+Shift+K.
 * Plain Backspace/Delete only remove one character at a
 * time (which merges lines one at a time); this removes a
 * whole line (or every line touched by the selection) in
 * one step.
 */
function deleteCurrentLines(instance) {
    const from = instance.getCursor("start");
    const to = instance.getCursor("end");
    const lastLine = instance.lastLine();
    if (to.line < lastLine) {
        instance.replaceRange(
            "",
            { line: from.line, ch: 0 },
            { line: to.line + 1, ch: 0 }
        );
    }
    else if (from.line > 0) {
        instance.replaceRange(
            "",
            {
                line: from.line - 1,
                ch: instance.getLine(from.line - 1).length
            },
            { line: to.line, ch: instance.getLine(to.line).length }
        );
    }
    else {
        instance.replaceRange(
            "",
            { line: 0, ch: 0 },
            { line: 0, ch: instance.getLine(0).length }
        );
    }
}
const cm =
    CodeMirror.fromTextArea(
        editor,
        {
            mode: "python",
            theme: "ideDark",
            lineNumbers: true,
            indentUnit: 4,
            tabSize: 4,
            indentWithTabs: false,
            matchBrackets: true,
            autoCloseBrackets: true,
            styleActiveLine: true,
            lineWrapping: false,
            extraKeys: {
                "Tab": function(instance) {
                    if (
                        instance.somethingSelected()
                    ) {
                        instance.indentSelection(
                            "add"
                        );
                    }
                    else {
                        instance.replaceSelection(
                            "    ",
                            "end"
                        );
                    }
                },
                "Ctrl-Shift-K": deleteCurrentLines,
                "Cmd-Shift-K": deleteCurrentLines
            }
        }
    );
cm.on(
    "change",
    function() {
        if (
            cm.getValue() !==
            editor.dataset.saved
        ) {
            dirtyIndicator.style.display = "inline";
        }
        else {
            dirtyIndicator.style.display = "none";
        }
    }
);
/* =====================================================
   STATUS BAR
   Reflects real state only - language comes from the
   open file's extension, position comes from
   CodeMirror's actual cursor. Nothing here is faked.
===================================================== */
const statusLangEl = document.getElementById("statusLang");
const statusPosEl = document.getElementById("statusPos");
const STATUS_LANGUAGE_BY_EXT = {
    py: "Python",
    js: "JavaScript",
    json: "JSON",
    md: "Markdown",
    txt: "Plain Text",
    html: "HTML",
    css: "CSS",
    sh: "Shell",
    yml: "YAML",
    yaml: "YAML",
    toml: "TOML",
    cfg: "Config",
    ini: "Config"
};
function updateStatusLine(path) {
    if (!statusLangEl) {
        return;
    }
    if (!path) {
        statusLangEl.textContent = "No file";
        return;
    }
    const dot = path.lastIndexOf(".");
    const ext =
        dot >= 0 ? path.slice(dot + 1).toLowerCase() : "";
    statusLangEl.textContent =
        STATUS_LANGUAGE_BY_EXT[ext] || "Plain Text";
}
function refreshCursorStatus() {
    if (!statusPosEl) {
        return;
    }
    const pos = cm.getCursor();
    statusPosEl.textContent =
        "Ln " + (pos.line + 1) + ", Col " + (pos.ch + 1);
}
cm.on("cursorActivity", refreshCursorStatus);
const output = document.getElementById("output");

/* =====================================================
   INTERACTIVE TERMINAL (xterm.js + WebSocket)
   A real shell running server-side, in the project's
   folder. This is what "Run" uses (so scripts run with
   no timeout and input() works), and it's also just a
   normal terminal you can type into - `pip install x`,
   `ls`, whatever.
===================================================== */
const outputTabBtn = document.getElementById("outputTabBtn");
const terminalTabBtn = document.getElementById("terminalTabBtn");
const outputPane = document.getElementById("outputPane");
const xtermPane = document.getElementById("xtermPane");
const terminalToolbar = document.getElementById("terminalToolbar");
const termStatusDot = document.getElementById("termStatusDot");
const cameraTabBtn = document.getElementById("cameraTabBtn");
const cameraPane = document.getElementById("cameraPane");
const cameraToolbar = document.getElementById("cameraToolbar");
const cameraStatusDot = document.getElementById(
    "cameraStatusDot"
);
const cameraStartBtn = document.getElementById(
    "cameraStartBtn"
);
const cameraVideoEl = document.getElementById("cameraVideoEl");
const cameraCaptureCanvas = document.getElementById(
    "cameraCaptureCanvas"
);
const cameraDisplayCanvas = document.getElementById(
    "cameraDisplayCanvas"
);
const cameraPlaceholder = document.getElementById(
    "cameraPlaceholder"
);
const cameraFpsEl = document.getElementById("cameraFps");
let bottomTab = "output";
let term = null;
let fitAddon = null;
let termSocket = null;
let termConnectedProject = null;
let termConnecting = false;
/* =====================================================
   OUTPUT COLOR HIGHLIGHTING
   The shell's raw bytes are decoded to text and scanned
   line-by-line for error/warning/success patterns, then
   re-encoded with ANSI color codes before xterm renders
   them - same idea as what VS Code/PyCharm do to their
   run/debug console output.
===================================================== */
const TERM_ANSI = {
    reset: "\x1b[0m",
    red: "\x1b[38;5;203m",
    yellow: "\x1b[38;5;221m",
    green: "\x1b[38;5;114m",
    cyan: "\x1b[38;5;80m",
    dim: "\x1b[2m",
    bold: "\x1b[1m"
};
function termColor(str, color, bold) {
    if (str === undefined || str === "") {
        return "";
    }
    return (
        (bold ? TERM_ANSI.bold : "") +
        color +
        str +
        TERM_ANSI.reset
    );
}
function highlightTermLine(line) {
    if (!line) {
        return line;
    }
    if (
        /^Traceback \(most recent call last\):/.test(line)
    ) {
        return termColor(line, TERM_ANSI.red, true);
    }
    let m = line.match(
        /^(\s*File ")([^"]+)(", line )(\d+)(, in .*)?$/
    );
    if (m) {
        return (
            m[1] +
            termColor(m[2], TERM_ANSI.cyan) +
            m[3] +
            termColor(m[4], TERM_ANSI.yellow) +
            (m[5] ? termColor(m[5], TERM_ANSI.dim) : "")
        );
    }
    m = line.match(
        /^([A-Za-z_][\w.]*(Error|Exception|Warning))(:?\s?)(.*)$/
    );
    if (m) {
        return (
            termColor(m[1], TERM_ANSI.red, true) +
            m[3] +
            termColor(m[4], TERM_ANSI.yellow)
        );
    }
    if (/^(WARNING|Warning)\b[:\s]/.test(line)) {
        return termColor(line, TERM_ANSI.yellow);
    }
    if (
        /^(Successfully installed|Successfully built|Process exited with code 0\b)/.test(
            line
        )
    ) {
        return termColor(line, TERM_ANSI.green);
    }
    if (/^Process exited with code [1-9]/.test(line)) {
        return termColor(line, TERM_ANSI.red, true);
    }
    return line;
}
const termTextDecoder = new TextDecoder("utf-8");
let termLineBuffer = "";
let termFlushTimer = null;
function flushTermLineBuffer() {
    clearTimeout(termFlushTimer);
    termFlushTimer = null;
    if (termLineBuffer) {
        term.write(
            highlightTermLine(
                termLineBuffer.replace(/\r$/, "")
            )
        );
        termLineBuffer = "";
    }
}
function feedTermData(chunkStr) {
    termLineBuffer += chunkStr;
    const lines = termLineBuffer.split("\n");
    /*
     * The last element is whatever comes after the final
     * \n in this chunk - i.e. an incomplete line (or "" if
     * the chunk ended cleanly on a newline). Hold it back
     * so a prompt like "Enter name: " isn't chopped mid-
     * word by highlighting, but flush it shortly after so
     * it still appears effectively instantly.
     */
    termLineBuffer = lines.pop();
    if (lines.length) {
        term.write(
            lines
                .map((l) =>
                    highlightTermLine(l.replace(/\r$/, ""))
                )
                .join("\r\n") + "\r\n"
        );
    }
    clearTimeout(termFlushTimer);
    if (termLineBuffer) {
        termFlushTimer = setTimeout(
            flushTermLineBuffer,
            40
        );
    }
}
/* =====================================================
   COPY SUPPORT
   Desktop: drag-select (built into xterm) then hit Copy,
   or Ctrl/Cmd+Shift+C. Mobile: long-press a line to copy
   just that line, since touch text-selection isn't
   reliably supported by xterm's own selection model.
===================================================== */
function copyTextToClipboard(text) {
    if (!text) {
        return Promise.resolve(false);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
        return navigator.clipboard
            .writeText(text)
            .then(() => true)
            .catch(() => legacyCopyFallback(text));
    }
    return Promise.resolve(legacyCopyFallback(text));
}
function legacyCopyFallback(text) {
    try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        const ok = document.execCommand("copy");
        document.body.removeChild(ta);
        return ok;
    } catch {
        return false;
    }
}
let termToastTimer = null;
function showTermToast(msg) {
    const el = document.getElementById("termToast");
    if (!el) {
        return;
    }
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(termToastTimer);
    termToastTimer = setTimeout(() => {
        el.classList.remove("show");
    }, 1100);
}
function copyTerminalOutput() {
    if (!term) {
        return;
    }
    let text;
    let label;
    if (term.hasSelection()) {
        text = term.getSelection();
        label = "Selection copied";
    } else {
        const buf = term.buffer.active;
        const rows = [];
        for (let i = 0; i < buf.length; i++) {
            const line = buf.getLine(i);
            if (line) {
                rows.push(line.translateToString(true));
            }
        }
        while (rows.length && rows[rows.length - 1] === "") {
            rows.pop();
        }
        text = rows.join("\n");
        label = "All output copied";
    }
    copyTextToClipboard(text).then((ok) => {
        showTermToast(ok ? label : "Copy failed");
    });
}
function getTermRowTextAtClientY(clientY) {
    const container = document.getElementById(
        "xtermContainer"
    );
    const rect = container.getBoundingClientRect();
    const rowHeight = rect.height / term.rows;
    let rowIndex = Math.floor(
        (clientY - rect.top) / rowHeight
    );
    rowIndex = Math.max(
        0,
        Math.min(term.rows - 1, rowIndex)
    );
    const buf = term.buffer.active;
    const line = buf.getLine(buf.viewportY + rowIndex);
    return line ? line.translateToString(true) : "";
}
function setupTerminalTouchCopy(container) {
    let pressTimer = null;
    let startPos = null;
    container.addEventListener(
        "touchstart",
        (e) => {
            if (e.touches.length !== 1) {
                return;
            }
            const t = e.touches[0];
            startPos = { x: t.clientX, y: t.clientY };
            clearTimeout(pressTimer);
            pressTimer = setTimeout(() => {
                const text = getTermRowTextAtClientY(
                    startPos.y
                );
                if (text) {
                    copyTextToClipboard(text).then((ok) => {
                        showTermToast(
                            ok ? "Line copied" : "Copy failed"
                        );
                    });
                    if (navigator.vibrate) {
                        navigator.vibrate(15);
                    }
                }
            }, 480);
        },
        { passive: true }
    );
    container.addEventListener(
        "touchmove",
        (e) => {
            if (!startPos) {
                return;
            }
            const t = e.touches[0];
            if (
                Math.abs(t.clientX - startPos.x) > 10 ||
                Math.abs(t.clientY - startPos.y) > 10
            ) {
                clearTimeout(pressTimer);
            }
        },
        { passive: true }
    );
    container.addEventListener(
        "touchend",
        () => {
            clearTimeout(pressTimer);
            startPos = null;
        },
        { passive: true }
    );
}
function setTermStatus(status, title) {
    termStatusDot.className =
        "term-status-dot" +
        (status !== "disconnected" ? " " + status : "");
    termStatusDot.title =
        title || status;
}
function loadTerminalRenderer(termInstance) {
    try {
        const webgl = new WebglAddon.WebglAddon();
        webgl.onContextLoss(() => {
            /*
             * Mobile browsers can yank the WebGL context
             * (e.g. tab backgrounded, too many contexts
             * open elsewhere). Drop back to Canvas instead
             * of silently reverting to the slow DOM renderer.
             */
            webgl.dispose();
            try {
                termInstance.loadAddon(
                    new CanvasAddon.CanvasAddon()
                );
            } catch {}
        });
        termInstance.loadAddon(webgl);
    } catch {
        try {
            termInstance.loadAddon(
                new CanvasAddon.CanvasAddon()
            );
        } catch {}
    }
}
function ensureTerminalWidget() {
    if (term) {
        return;
    }
    term = new Terminal({
        convertEol: true,
        cursorBlink: true,
        fontFamily: '"Courier New", monospace',
        fontSize: 13,
        scrollback: 5000,
        theme: {
            background: "#010409",
            foreground: "#e6edf3",
            cursor: "#e6edf3",
            selectionBackground: "#3b5070",
            /*
             * Vivid 16-color ANSI palette (GitHub Dark-
             * inspired, matching the rest of the IDE's UI)
             * so ls/grep/git/pip's own colors actually read
             * clearly against the near-black background,
             * on top of the red/yellow/green/cyan we already
             * inject for errors, warnings and the prompt.
             */
            black: "#484f58",
            red: "#ff7b72",
            green: "#3fb950",
            yellow: "#d29922",
            blue: "#58a6ff",
            magenta: "#bc8cff",
            cyan: "#39c5cf",
            white: "#b1bac4",
            brightBlack: "#6e7681",
            brightRed: "#ffa198",
            brightGreen: "#56d364",
            brightYellow: "#e3b341",
            brightBlue: "#79c0ff",
            brightMagenta: "#d2a8ff",
            brightCyan: "#56d4dd",
            brightWhite: "#f0f6fc"
        }
    });
    fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(
        document.getElementById("xtermContainer")
    );
    /*
     * Without a GPU-backed renderer, xterm falls back to
     * painting every glyph as a real DOM node - fine for
     * typing, but scrolling repaints the whole viewport on
     * every pixel of movement, which is what makes mobile
     * scrolling feel heavy/laggy. WebGL fixes that; Canvas
     * is the fallback for browsers/contexts without WebGL
     * (some mobile browsers refuse WebGL in background tabs
     * or after too many contexts are open).
     */
    loadTerminalRenderer(term);
    setupTerminalTouchCopy(
        document.getElementById("xtermContainer")
    );
    term.attachCustomKeyEventHandler((e) => {
        /*
         * Ctrl/Cmd+C is taken by the shell for SIGINT, so
         * copy lives on Ctrl/Cmd+Shift+C instead - the
         * standard convention most terminal apps use.
         */
        if (
            e.type === "keydown" &&
            (e.ctrlKey || e.metaKey) &&
            e.shiftKey &&
            e.key.toLowerCase() === "c"
        ) {
            copyTerminalOutput();
            return false;
        }
        return true;
    });
    term.onData((data) => {
        if (
            termSocket &&
            termSocket.readyState === WebSocket.OPEN
        ) {
            termSocket.send(
                JSON.stringify({
                    type: "input",
                    data: data
                })
            );
        }
    });
    window.addEventListener("resize", () => {
        if (bottomTab === "terminal") {
            try {
                fitAddon.fit();
            } catch {}
            sendTermResize();
        }
    });
}
function sendTermResize() {
    if (
        !term ||
        !termSocket ||
        termSocket.readyState !== WebSocket.OPEN
    ) {
        return;
    }
    termSocket.send(
        JSON.stringify({
            type: "resize",
            cols: term.cols,
            rows: term.rows
        })
    );
}
function connectTerminal(projectId, initialCommand) {
    ensureTerminalWidget();
    if (termSocket) {
        try {
            termSocket.onclose = null;
            termSocket.close();
        } catch {}
        termSocket = null;
    }
    clearTimeout(termFlushTimer);
    termFlushTimer = null;
    termLineBuffer = "";
    termConnectedProject = projectId;
    termConnecting = true;
    setTermStatus(
        "connecting",
        "Connecting..."
    );
    const proto =
        location.protocol === "https:" ?
            "wss:" :
            "ws:";
    let url =
        `${proto}//${location.host}` +
        `/ws/projects/${encodeURIComponent(projectId)}/terminal`;
    if (initialCommand) {
        /*
         * The server types this in once the shell has
         * actually produced its first prompt. Typing it
         * ourselves right after the socket opens would
         * race the shell's own startup and can garble or
         * drop the first thing sent - better to let the
         * server time it off something real.
         */
        url +=
            "?cmd=" +
            encodeURIComponent(initialCommand);
    }
    const socket = new WebSocket(url);
    socket.binaryType = "arraybuffer";
    socket.onopen = () => {
        termConnecting = false;
        setTermStatus(
            "connected",
            "Connected"
        );
        try {
            fitAddon.fit();
        } catch {}
        sendTermResize();
        term.focus();
    };
    socket.onmessage = (event) => {
        if (event.data instanceof ArrayBuffer) {
            const chunk = termTextDecoder.decode(
                new Uint8Array(event.data),
                { stream: true }
            );
            feedTermData(chunk);
            return;
        }
        try {
            const msg =
                JSON.parse(event.data);
            if (msg.type === "exit") {
                flushTermLineBuffer();
                term.write(
                    "\r\n\x1b[90m[shell exited - " +
                    "click Restart to start a new one]" +
                    "\x1b[0m\r\n"
                );
                setTermStatus(
                    "disconnected",
                    "Shell exited"
                );
            }
            else if (msg.type === "error") {
                flushTermLineBuffer();
                term.write(
                    "\r\n\x1b[31m" +
                    msg.message +
                    "\x1b[0m\r\n"
                );
                setTermStatus(
                    "error",
                    msg.message
                );
            }
        }
        catch {
            term.write(event.data);
        }
    };
    socket.onclose = () => {
        termConnecting = false;
        if (termSocket === socket) {
            setTermStatus(
                "disconnected",
                "Disconnected"
            );
        }
    };
    socket.onerror = () => {
        setTermStatus(
            "error",
            "Connection error"
        );
    };
    termSocket = socket;
}
function ensureTerminalConnected() {
    if (!currentProject) {
        return;
    }
    if (
        termConnectedProject === currentProject &&
        termSocket &&
        (
            termSocket.readyState === WebSocket.OPEN ||
            termConnecting
        )
    ) {
        return;
    }
    if (term) {
        term.reset();
    }
    connectTerminal(currentProject);
}
function restartTerminal() {
    if (!currentProject) {
        return;
    }
    if (term) {
        term.reset();
    }
    connectTerminal(currentProject);
}
function clearTerminalView() {
    if (term) {
        term.clear();
    }
}
function setBottomTab(tab) {
    bottomTab = tab;
    outputTabBtn.classList.toggle(
        "active",
        tab === "output"
    );
    terminalTabBtn.classList.toggle(
        "active",
        tab === "terminal"
    );
    cameraTabBtn.classList.toggle(
        "active",
        tab === "camera"
    );
    outputPane.classList.toggle(
        "active",
        tab === "output"
    );
    xtermPane.classList.toggle(
        "active",
        tab === "terminal"
    );
    cameraPane.classList.toggle(
        "active",
        tab === "camera"
    );
    terminalToolbar.classList.toggle(
        "show",
        tab === "terminal"
    );
    cameraToolbar.classList.toggle(
        "show",
        tab === "camera"
    );
    if (tab === "terminal") {
        ensureTerminalConnected();
        requestAnimationFrame(() => {
            if (fitAddon) {
                try {
                    fitAddon.fit();
                } catch {}
                sendTermResize();
            }
            if (term) {
                term.focus();
            }
        });
    }
}
function shellQuote(value) {
    return (
        "'" +
        String(value).replace(
            /'/g,
            "'\\''"
        ) +
        "'"
    );
}
function sendTerminalCommand(command, attempt) {
    if (!currentProject) {
        return;
    }
    attempt = attempt || 0;
    setBottomTab("terminal");
    const needsFreshConnection =
        termConnectedProject !== currentProject ||
        !termSocket ||
        (
            termSocket.readyState !== WebSocket.OPEN &&
            !termConnecting
        );
    if (needsFreshConnection) {
        /*
         * Brand-new shell: pass the command along so the
         * server can type it in once the shell has actually
         * produced its first prompt. (Typing it from here
         * immediately after "open" would race the shell's
         * own startup.)
         */
        connectTerminal(currentProject, command);
        return;
    }
    if (
        termSocket &&
        termSocket.readyState === WebSocket.OPEN
    ) {
        /*
         * Terminal's already open and idle/ready - just
         * type the command directly. (No Ctrl+C first: an
         * interrupt immediately followed by input is itself
         * a race against the shell's redraw and can garble
         * or drop the next thing typed. If something else
         * is already running here, the user can hit Ctrl+C
         * in the terminal themselves before running again.)
         */
        termSocket.send(
            JSON.stringify({
                type: "input",
                data: command + "\r"
            })
        );
    }
    else if (attempt < 40) {
        /*
         * A connection to this same project is already in
         * flight (termConnecting) - wait for it rather than
         * starting a second one.
         */
        setTimeout(
            () => sendTerminalCommand(command, attempt + 1),
            100
        );
    }
}
function installRequirements() {
    if (!currentProject) {
        alert(
            "Select a project first."
        );
        return;
    }
    sendTerminalCommand(
        "pip install -r requirements.txt"
    );
}
const projectSelect = document.getElementById("projectSelect");
const filename = document.getElementById("filename");
const dirtyIndicator = document.getElementById("dirty");
const runButton = document.getElementById("runButton");
const sidebarEl = document.getElementById("sidebar");
const backdropEl = document.getElementById("backdrop");
const tabEditorBtn = document.getElementById("tabEditorBtn");
const tabOutputBtn = document.getElementById("tabOutputBtn");
const isMobile = () =>
    window.matchMedia(
        "(max-width: 700px)"
    ).matches;
/* =====================================================
   MOBILE: SIDEBAR DRAWER
===================================================== */
function openSidebar() {
    sidebarEl.classList.add(
        "open"
    );
    backdropEl.classList.add(
        "show"
    );
}
function closeSidebar() {
    sidebarEl.classList.remove(
        "open"
    );
    backdropEl.classList.remove(
        "show"
    );
}
function toggleSidebar() {
    if (
        sidebarEl.classList.contains(
            "open"
        )
    ) {
        closeSidebar();
    }
    else {
        openSidebar();
    }
}
/* =====================================================
   MOBILE: EDITOR / OUTPUT TABS
===================================================== */
function setMobileView(
    view
) {
    document.body.setAttribute(
        "data-mobile-view",
        view
    );
    tabEditorBtn.classList.toggle(
        "active",
        view === "editor"
    );
    tabOutputBtn.classList.toggle(
        "active",
        view === "output"
    );
    if (
        view === "editor"
    ) {
        /*
         * CodeMirror can render blank/misaligned after being
         * toggled with display:none, so nudge it to
         * re-measure itself once it's visible again.
         */
        setTimeout(
            () => cm.refresh(),
            0
        );
    }
}
/* =====================================================
   FILE CONTEXT MENU (rename / delete)
===================================================== */
const fileContextMenuEl = document.getElementById("fileContextMenu");
const newFileHereBtn = document.getElementById("newFileHereBtn");
const newFolderHereBtn = document.getElementById("newFolderHereBtn");
const uploadHereBtn = document.getElementById("uploadHereBtn");
const renameFileBtn = document.getElementById("renameFileBtn");
const deleteFileBtn = document.getElementById("deleteFileBtn");
let contextMenuPath = null;
let contextMenuType = null; // 'file' | 'folder' | 'root'
function showFileContextMenu(
    path,
    x,
    y,
    type
) {
    contextMenuPath = path;
    contextMenuType = type;
    /*
     * Root (empty sidebar area): only the "create new"
     * actions make sense. A file: only rename/delete. A
     * folder gets everything.
     */
    const showCreateActions =
        type === "folder" || type === "root";
    const showRenameDelete =
        type === "file" || type === "folder";
    newFileHereBtn.style.display =
        showCreateActions ? "flex" : "none";
    newFolderHereBtn.style.display =
        showCreateActions ? "flex" : "none";
    uploadHereBtn.style.display =
        showCreateActions ? "flex" : "none";
    renameFileBtn.style.display =
        showRenameDelete ? "flex" : "none";
    deleteFileBtn.style.display =
        showRenameDelete ? "flex" : "none";
    fileContextMenuEl.classList.add(
        "show"
    );
    /*
     * Position first, then clamp so the menu never renders
     * off the right/bottom edge of the screen.
     */
    fileContextMenuEl.style.left =
        x + "px";
    fileContextMenuEl.style.top =
        y + "px";
    const rect =
        fileContextMenuEl.getBoundingClientRect();
    const maxLeft =
        window.innerWidth -
        rect.width -
        8;
    const maxTop =
        window.innerHeight -
        rect.height -
        8;
    fileContextMenuEl.style.left =
        Math.min(x, Math.max(8, maxLeft)) +
        "px";
    fileContextMenuEl.style.top =
        Math.min(y, Math.max(8, maxTop)) +
        "px";
}
function hideFileContextMenu() {
    fileContextMenuEl.classList.remove(
        "show"
    );
    contextMenuPath = null;
    contextMenuType = null;
}
/* =====================================================
   HEADER "MORE ACTIONS" DROPDOWN (mobile)
   New Project/File/Folder, Upload, and Install collapse
   into this menu on narrow screens so Run and Save stay
   reachable instead of getting pushed off-screen.
===================================================== */
const toolbarMoreEl = document.getElementById("toolbarMore");
function toggleMoreToolbarMenu() {
    toolbarMoreEl.classList.toggle(
        "show"
    );
}
function hideMoreToolbarMenu() {
    toolbarMoreEl.classList.remove(
        "show"
    );
}
document.addEventListener(
    "pointerdown",
    (event) => {
        if (
            toolbarMoreEl.classList.contains("show") &&
            !toolbarMoreEl.contains(event.target) &&
            !document.getElementById("moreToolbarBtn").contains(
                event.target
            )
        ) {
            hideMoreToolbarMenu();
        }
    }
);
/*
 * Close the menu on any click/tap outside it, and on
 * scroll (so it doesn't float over the wrong file).
 */
document.addEventListener(
    "pointerdown",
    (event) => {
        if (
            fileContextMenuEl.classList.contains("show") &&
            !fileContextMenuEl.contains(event.target)
        ) {
            hideFileContextMenu();
        }
    }
);
document.addEventListener(
    "scroll",
    hideFileContextMenu,
    true
);
/*
 * Right-clicking (or long-pressing) empty space in the
 * sidebar - not on a specific file/folder row - opens the
 * "create new" menu scoped to the project root.
 */
document.getElementById("files").addEventListener(
    "contextmenu",
    (event) => {
        if (event.target.closest(".file")) {
            return;
        }
        event.preventDefault();
        showFileContextMenu("", event.clientX, event.clientY, "root");
    }
);
function newFileFromMenu() {
    const prefix =
        contextMenuType === "folder" ? contextMenuPath : "";
    hideFileContextMenu();
    newFile(prefix);
}
function newFolderFromMenu() {
    const prefix =
        contextMenuType === "folder" ? contextMenuPath : "";
    hideFileContextMenu();
    newFolder(prefix);
}
function uploadFromMenu() {
    const prefix =
        contextMenuType === "folder" ? contextMenuPath : "";
    hideFileContextMenu();
    triggerUpload(prefix);
}
async function renameSelectedFile() {
    const oldPath = contextMenuPath;
    const type = contextMenuType;
    hideFileContextMenu();
    if (
        !oldPath ||
        !currentProject
    ) {
        return;
    }
    const input =
        prompt(
            type === "folder" ? "Rename folder:" : "Rename file:",
            oldPath
        );
    if (
        input === null
    ) {
        return;
    }
    const newPath =
        input.trim();
    if (
        !newPath ||
        newPath === oldPath
    ) {
        return;
    }
    try {
        await api(
            `/api/projects/${encodeURIComponent(
                currentProject
            )}/file/rename`,
            {
                method: "POST",
                headers: {
                    "Content-Type":
                        "application/json"
                },
                body:
                    JSON.stringify({
                        old_path:
                            oldPath,
                        new_path:
                            newPath
                    })
            }
        );
        /*
         * If the renamed file is the one currently open,
         * keep the editor pointed at its new name/path -
         * and if a whole folder was renamed and the open
         * file lived inside it, update its prefix too.
         */
        if (
            currentFile === oldPath
        ) {
            currentFile = newPath;
            filename.textContent = newPath;
            updateStatusLine(newPath);
        }
        else if (
            type === "folder" &&
            currentFile &&
            currentFile.startsWith(oldPath + "/")
        ) {
            currentFile =
                newPath + currentFile.slice(oldPath.length);
            filename.textContent = currentFile;
            updateStatusLine(currentFile);
        }
        if (type === "folder") {
            expandedFolders.delete(oldPath);
            expandedFolders.add(newPath);
        }
        await loadFiles();
    }
    catch (error) {
        output.textContent =
            "Rename failed:\n" +
            error.message;
    }
}
async function deleteSelectedFile() {
    const path = contextMenuPath;
    const type = contextMenuType;
    hideFileContextMenu();
    if (
        !path ||
        !currentProject
    ) {
        return;
    }
    const confirmed =
        confirm(
            type === "folder"
                ? `Delete folder "${path}" and everything ` +
                  "inside it?\n\nThis cannot be undone."
                : `Delete "${path}"?\n\n` +
                  "This cannot be undone."
        );
    if (
        !confirmed
    ) {
        return;
    }
    try {
        const endpoint =
            type === "folder" ? "folder" : "file";
        await api(
            `/api/projects/${encodeURIComponent(
                currentProject
            )}/${endpoint}?path=${encodeURIComponent(
                path
            )}`,
            {
                method: "DELETE"
            }
        );
        /*
         * If the deleted file (or a file inside the deleted
         * folder) was open in the editor, clear it out so we
         * don't keep editing something that no longer exists.
         */
        const openFileWasDeleted =
            currentFile === path ||
            (
                type === "folder" &&
                currentFile &&
                currentFile.startsWith(path + "/")
            );
        if (
            openFileWasDeleted
        ) {
            currentFile = null;
            cm.setValue(
                ""
            );
            editor.dataset.saved = "";
            filename.textContent = "No file selected";
            updateStatusLine(null);
            dirtyIndicator.style.display = "none";
        }
        expandedFolders.delete(path);
        await loadFiles();
    }
    catch (error) {
        output.textContent =
            "Delete failed:\n" +
            error.message;
    }
}
/* =====================================================
   API HELPER
===================================================== */
async function api(
    url,
    options = {}
) {
    /*
     * Without this, a server that's slow to wake up (or just
     * never responds) leaves fetch() hanging forever with no
     * error and no feedback - the UI just sits frozen with no
     * sign anything is wrong. 20s is generous enough for a
     * cold-starting free-tier server, but still finite.
     */
    const controller = new AbortController();
    const timeoutId =
        setTimeout(
            () => controller.abort(),
            20000
        );
    let response;
    try {
        response =
            await fetch(
                url,
                {
                    ...options,
                    signal: controller.signal
                }
            );
    }
    catch (error) {
        if (error.name === "AbortError") {
            throw new Error(
                "The server took too long to respond " +
                "(20s+). It may still be starting up - " +
                "try again in a moment."
            );
        }
        throw new Error(
            "Could not reach the server. Check your " +
            "connection and try again."
        );
    }
    finally {
        clearTimeout(
            timeoutId
        );
    }
    let data;
    try {
        data =
            await response.json();
    }
    catch {
        throw new Error(
            "Server returned invalid data."
        );
    }
    if (!response.ok) {
        throw new Error(
            data.error ||
            "Request failed."
        );
    }
    return data;
}
/* =====================================================
   EXPLORER STATE (empty / error / loading)
   One shared renderer so "no files", "couldn't load",
   and "starting up" all look and behave consistently
   instead of three different one-off DOM blocks.
===================================================== */
function renderExplorerState(container, options) {
    container.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.className = "explorer-state";
    if (options.icon) {
        const iconWrap = document.createElement("span");
        iconWrap.className = "explorer-state-icon";
        iconWrap.setAttribute("aria-hidden", "true");
        iconWrap.innerHTML =
            '<svg class="icon icon-lg"><use href="#i-' +
            options.icon +
            '"></use></svg>';
        wrap.appendChild(iconWrap);
    }
    const title = document.createElement("div");
    title.className = "explorer-state-title";
    title.textContent = options.title;
    wrap.appendChild(title);
    if (options.message) {
        const msg = document.createElement("div");
        msg.className = "explorer-state-message";
        msg.textContent = options.message;
        wrap.appendChild(msg);
    }
    if (options.retry) {
        const retryBtn = document.createElement("button");
        retryBtn.className = "explorer-state-retry";
        retryBtn.innerHTML =
            '<svg class="icon"><use href="#i-refresh"></use></svg> Retry';
        retryBtn.onclick = options.retry;
        wrap.appendChild(retryBtn);
    }
    container.appendChild(wrap);
}
/* =====================================================
   LOAD PROJECTS
===================================================== */
async function loadProjects() {
    try {
        const data =
            await api(
                "/api/projects"
            );
        projectSelect.innerHTML = "";
        for (
            const project
            of data.projects
        ) {
            const option =
                document.createElement(
                    "option"
                );
            option.value = project.id;
            option.textContent = project.name;
            projectSelect.appendChild(
                option
            );
        }
        /*
         * If there are no projects,
         * automatically create one.
         */
        if (
            data.projects.length === 0
        ) {
            await createDefaultProject();
            return;
        }
        /*
         * Select first project.
         */
        currentProject =
            data.projects[0].id;
        projectSelect.value = currentProject;
        currentFile = null;
        await loadFiles();
    }
    catch (error) {
        /*
         * This is the very first thing the app does, and the
         * sidebar (with its "Loading..." placeholder) is what
         * the user is actually looking at - not the Output
         * panel, which is hidden by default on mobile. Show
         * the error (and a retry button) right there instead
         * of somewhere the user may never see.
         */
        output.textContent =
            "Startup error:\n" +
            error.message;
        const container =
            document.getElementById(
                "files"
            );
        renderExplorerState(container, {
            icon: "alert",
            title: "Couldn't start up",
            message: error.message,
            retry: loadProjects
        });
    }
}
/* =====================================================
   DEFAULT PROJECT
===================================================== */
async function createDefaultProject() {
    const data =
        await api(
            "/api/projects",
            {
                method: "POST",
                headers: {
                    "Content-Type":
                        "application/json"
                },
                body:
                    JSON.stringify({
                        name:
                            "my-project"
                    })
            }
        );
    currentProject = data.id;
    projectSelect.innerHTML = "";
    const option =
        document.createElement(
            "option"
        );
    option.value = data.id;
    option.textContent = data.name;
    projectSelect.appendChild(
        option
    );
    await loadFiles();
}
/* =====================================================
   NEW PROJECT
===================================================== */
async function newProject() {
    const name =
        prompt(
            "Project name:",
            "my-project"
        );
    if (
        name === null
    ) {
        return;
    }
    const trimmed =
        name.trim();
    if (!trimmed) {
        alert(
            "Project name cannot be empty."
        );
        return;
    }
    try {
        const data =
            await api(
                "/api/projects",
                {
                    method: "POST",
                    headers: {
                        "Content-Type":
                            "application/json"
                    },
                    body:
                        JSON.stringify({
                            name:
                                trimmed
                        })
                }
            );
        currentProject = data.id;
        currentFile = null;
        await loadProjects();
        projectSelect.value = data.id;
        currentProject = data.id;
        await loadFiles();
        if (bottomTab === "terminal") {
            ensureTerminalConnected();
        }
        output.textContent =
            "Created project:\n" +
            data.id;
    }
    catch (error) {
        alert(
            "Could not create project:\n" +
            error.message
        );
    }
}
/* =====================================================
   SWITCH PROJECT
===================================================== */
async function switchProject() {
    const selected = projectSelect.value;
    if (!selected) {
        return;
    }
    /*
     * Warn about unsaved changes.
     */
    if (
        currentFile &&
        cm.getValue() !==
        editor.dataset.saved
    ) {
        const save =
            confirm(
                "You have unsaved changes.\n\n" +
                "Save them before switching?"
            );
        if (save) {
            try {
                await saveFile();
            }
            catch (error) {
                return;
            }
        }
    }
    currentProject = selected;
    currentFile = null;
    cm.setValue(
        ""
    );
    editor.dataset.saved = "";
    filename.textContent = "No file selected";
    updateStatusLine(null);
    dirtyIndicator.style.display = "none";
    await loadFiles();
    if (bottomTab === "terminal") {
        ensureTerminalConnected();
    }
}
/* =====================================================
   LOAD FILES
===================================================== */
async function loadFiles() {
    if (!currentProject) {
        return;
    }
    try {
        const data =
            await api(
                `/api/projects/${encodeURIComponent(
                    currentProject
                )}/files`
            );
        const container = document.getElementById("files");
        if (
            data.tree.length === 0
        ) {
            renderExplorerState(container, {
                icon: "folder",
                title: "No files yet",
                message: "Create a file or folder to get started.",
                retry: null
            });
        }
        else {
            container.innerHTML = "";
            renderTree(
                data.tree,
                container,
                0
            );
        }
        /*
         * Open first file
         * automatically.
         */
        if (
            !currentFile &&
            data.files.length > 0
        ) {
            await openFile(
                data.files[0]
            );
        }
    }
    catch (error) {
        output.textContent =
            "Could not load files:\n" +
            error.message;
        const container =
            document.getElementById(
                "files"
            );
        renderExplorerState(container, {
            icon: "alert",
            title: "Unable to load files",
            message: error.message,
            retry: loadFiles
        });
    }
}
/* =====================================================
   FILE / FOLDER TREE RENDERING
   Flat DOM, indentation via padding - simpler to manage
   drag & drop and long-press than a nested-div tree.
===================================================== */
function renderTree(
    nodes,
    container,
    depth
) {
    for (
        const node
        of nodes
    ) {
        if (
            node.type === "folder"
        ) {
            container.appendChild(
                renderFolderRow(
                    node,
                    depth
                )
            );
            if (
                expandedFolders.has(
                    node.path
                )
            ) {
                renderTree(
                    node.children,
                    container,
                    depth + 1
                );
            }
        }
        else {
            container.appendChild(
                renderFileRow(
                    node,
                    depth
                )
            );
        }
    }
}
function attachRowDrag(
    item,
    path,
    type
) {
    item.draggable = true;
    item.addEventListener(
        "dragstart",
        (event) => {
            dragPath = path;
            dragType = type;
            item.classList.add(
                "dragging"
            );
            event.dataTransfer.effectAllowed = "move";
        }
    );
    item.addEventListener(
        "dragend",
        () => {
            item.classList.remove(
                "dragging"
            );
            dragPath = null;
            dragType = null;
        }
    );
}
/*
 * Shared right-click / long-press handling for both file
 * and folder rows.
 */
function attachRowContextMenu(
    item,
    path,
    type,
    onTap
) {
    let longPressTimer = null;
    let longPressFired = false;
    item.oncontextmenu =
        (event) => {
            event.preventDefault();
            showFileContextMenu(
                path,
                event.clientX,
                event.clientY,
                type
            );
        };
    item.addEventListener(
        "pointerdown",
        (event) => {
            if (
                event.pointerType !== "touch"
            ) {
                return;
            }
            longPressFired = false;
            longPressTimer =
                setTimeout(
                    () => {
                        longPressFired = true;
                        if (
                            navigator.vibrate
                        ) {
                            navigator.vibrate(
                                15
                            );
                        }
                        showFileContextMenu(
                            path,
                            event.clientX,
                            event.clientY,
                            type
                        );
                    },
                    550
                );
        }
    );
    const cancelLongPress =
        () => {
            if (
                longPressTimer
            ) {
                clearTimeout(
                    longPressTimer
                );
                longPressTimer = null;
            }
        };
    item.addEventListener(
        "pointerup",
        cancelLongPress
    );
    item.addEventListener(
        "pointermove",
        cancelLongPress
    );
    item.addEventListener(
        "pointercancel",
        cancelLongPress
    );
    item.onclick =
        () => {
            if (
                longPressFired
            ) {
                longPressFired = false;
                return;
            }
            onTap();
        };
    /*
     * Keyboard access: these rows are plain divs (not
     * buttons) so drag-and-drop and long-press can share
     * one element, but that means Tab/Enter need to be
     * wired up by hand for keyboard users.
     */
    item.tabIndex = 0;
    item.setAttribute(
        "role",
        type === "folder" ? "treeitem" : "button"
    );
    item.addEventListener(
        "keydown",
        (event) => {
            if (
                event.key === "Enter" ||
                event.key === " "
            ) {
                event.preventDefault();
                onTap();
            }
            else if (
                event.key === "ContextMenu" ||
                (event.shiftKey && event.key === "F10")
            ) {
                event.preventDefault();
                const rect =
                    item.getBoundingClientRect();
                showFileContextMenu(
                    path,
                    rect.left,
                    rect.bottom,
                    type
                );
            }
        }
    );
}
function renderFileRow(
    node,
    depth
) {
    const item =
        document.createElement(
            "div"
        );
    item.className = "file";
    if (
        node.path === currentFile
    ) {
        item.classList.add(
            "active"
        );
    }
    item.style.paddingLeft =
        (depth * 16 + 8) + "px";
    const icon =
        document.createElement(
            "span"
        );
    const isPython = node.name.endsWith(".py");
    icon.className =
        "file-icon" + (isPython ? " file-icon-py" : "");
    icon.innerHTML =
        '<svg class="icon"><use href="#i-file"></use></svg>';
    const name =
        document.createElement(
            "span"
        );
    name.className = "file-name";
    name.textContent = node.name;
    item.appendChild(
        icon
    );
    item.appendChild(
        name
    );
    item.title = node.path;
    attachRowDrag(
        item,
        node.path,
        "file"
    );
    attachRowContextMenu(
        item,
        node.path,
        "file",
        () => openFile(node.path)
    );
    return item;
}
function renderFolderRow(
    node,
    depth
) {
    const item =
        document.createElement(
            "div"
        );
    item.className = "file";
    item.style.paddingLeft =
        (depth * 16 + 8) + "px";
    const isOpen =
        expandedFolders.has(
            node.path
        );
    const arrow =
        document.createElement(
            "span"
        );
    arrow.className =
        "folder-arrow" + (isOpen ? " open" : "");
    arrow.innerHTML =
        '<svg class="icon"><use href="#i-chevron"></use></svg>';
    const icon =
        document.createElement(
            "span"
        );
    icon.className = "file-icon";
    icon.innerHTML =
        '<svg class="icon"><use href="#i-folder"></use></svg>';
    const name =
        document.createElement(
            "span"
        );
    name.className = "file-name";
    name.textContent = node.name;
    const addFileBtn =
        document.createElement(
            "span"
        );
    addFileBtn.className = "folder-add-btn";
    addFileBtn.textContent = "＋";
    addFileBtn.title = "New file in this folder";
    addFileBtn.setAttribute(
        "role",
        "button"
    );
    addFileBtn.setAttribute(
        "aria-label",
        "New file in " + node.name
    );
    addFileBtn.tabIndex = 0;
    const triggerAddFile =
        (event) => {
            event.stopPropagation();
            expandedFolders.add(
                node.path
            );
            newFile(
                node.path
            );
        };
    addFileBtn.onclick = triggerAddFile;
    addFileBtn.addEventListener(
        "keydown",
        (event) => {
            if (
                event.key === "Enter" ||
                event.key === " "
            ) {
                event.preventDefault();
                triggerAddFile(event);
            }
        }
    );
    item.appendChild(
        arrow
    );
    item.appendChild(
        icon
    );
    item.appendChild(
        name
    );
    item.appendChild(
        addFileBtn
    );
    item.title = node.path;
    attachRowDrag(
        item,
        node.path,
        "folder"
    );
    attachRowContextMenu(
        item,
        node.path,
        "folder",
        () => {
            if (
                expandedFolders.has(
                    node.path
                )
            ) {
                expandedFolders.delete(
                    node.path
                );
            }
            else {
                expandedFolders.add(
                    node.path
                );
            }
            loadFiles();
        }
    );
    /*
     * Drop target: dropping a dragged file/folder here
     * moves it into this folder.
     */
    item.addEventListener(
        "dragover",
        (event) => {
            if (!dragPath) {
                return;
            }
            event.preventDefault();
            item.classList.add(
                "drop-target"
            );
        }
    );
    item.addEventListener(
        "dragleave",
        () => {
            item.classList.remove(
                "drop-target"
            );
        }
    );
    item.addEventListener(
        "drop",
        (event) => {
            event.preventDefault();
            event.stopPropagation();
            item.classList.remove(
                "drop-target"
            );
            moveEntry(
                dragPath,
                dragType,
                node.path
            );
        }
    );
    return item;
}
/*
 * Dropping on empty sidebar space (not on any row) moves
 * the dragged item to the project root.
 */
(function setUpRootDropZone() {
    const container =
        document.getElementById(
            "files"
        );
    container.addEventListener(
        "dragover",
        (event) => {
            if (
                !dragPath ||
                event.target.closest(".file")
            ) {
                return;
            }
            event.preventDefault();
        }
    );
    container.addEventListener(
        "drop",
        (event) => {
            if (
                !dragPath ||
                event.target.closest(".file")
            ) {
                return;
            }
            event.preventDefault();
            moveEntry(
                dragPath,
                dragType,
                ""
            );
        }
    );
})();
async function moveEntry(
    oldPath,
    type,
    newParentFolder
) {
    if (!oldPath || !currentProject) {
        return;
    }
    const baseName =
        oldPath.split("/").pop();
    const newPath =
        newParentFolder
            ? `${newParentFolder}/${baseName}`
            : baseName;
    if (
        newPath === oldPath
    ) {
        return;
    }
    /*
     * Guard against dropping a folder into itself or one
     * of its own descendants.
     */
    if (
        type === "folder" &&
        (
            newParentFolder === oldPath ||
            newParentFolder.startsWith(oldPath + "/")
        )
    ) {
        return;
    }
    try {
        await api(
            `/api/projects/${encodeURIComponent(
                currentProject
            )}/file/rename`,
            {
                method: "POST",
                headers: {
                    "Content-Type":
                        "application/json"
                },
                body:
                    JSON.stringify({
                        old_path: oldPath,
                        new_path: newPath
                    })
            }
        );
        if (
            currentFile === oldPath
        ) {
            currentFile = newPath;
            filename.textContent = newPath;
            updateStatusLine(newPath);
        }
        else if (
            type === "folder" &&
            currentFile &&
            currentFile.startsWith(oldPath + "/")
        ) {
            currentFile =
                newPath + currentFile.slice(oldPath.length);
            filename.textContent = currentFile;
            updateStatusLine(currentFile);
        }
        if (type === "folder") {
            expandedFolders.delete(oldPath);
            expandedFolders.add(newPath);
        }
        await loadFiles();
    }
    catch (error) {
        output.textContent =
            "Move failed:\n" +
            error.message;
    }
}
/* =====================================================
   OPEN FILE
===================================================== */
async function openFile(
    path
) {
    /*
     * Detect unsaved changes.
     */
    if (
        currentFile &&
        cm.getValue() !==
        editor.dataset.saved
    ) {
        const save =
            confirm(
                "You have unsaved changes.\n\n" +
                "Save them?"
            );
        if (save) {
            try {
                await saveFile();
            }
            catch (error) {
                return;
            }
        }
    }
    try {
        const data =
            await api(
                `/api/projects/${encodeURIComponent(
                    currentProject
                )}/file?path=${encodeURIComponent(
                    path
                )}`
            );
        currentFile = path;
        cm.setValue(
            data.content
        );
        editor.dataset.saved = data.content;
        filename.textContent = path;
        updateStatusLine(path);
        dirtyIndicator.style.display = "none";
        /*
         * On mobile, opening a file should close the
         * drawer and bring the editor into view.
         */
        if (
            isMobile()
        ) {
            closeSidebar();
            setMobileView(
                "editor"
            );
        }
        await loadFiles();
    }
    catch (error) {
        output.textContent =
            "Could not open file:\n" +
            error.message;
    }
}
/* =====================================================
   SAVE FILE
===================================================== */
async function saveFile() {
    if (
        !currentProject
    ) {
        return;
    }
    if (
        !currentFile
    ) {
        return;
    }
    try {
        await api(
            `/api/projects/${encodeURIComponent(
                currentProject
            )}/file`,
            {
                method: "POST",
                headers: {
                    "Content-Type":
                        "application/json"
                },
                body:
                    JSON.stringify({
                        path:
                            currentFile,
                        content:
                            cm.getValue()
                    })
            }
        );
        editor.dataset.saved =
            cm.getValue();
        dirtyIndicator.style.display = "none";
        output.textContent =
            "Saved:\n" +
            currentFile;
        await loadFiles();
    }
    catch (error) {
        output.textContent =
            "Save failed:\n" +
            error.message;
        throw error;
    }
}
/* =====================================================
   NEW FILE
===================================================== */
async function newFile(prefix) {
    if (!currentProject) {
        alert(
            "Create a project first."
        );
        return;
    }
    const suggestion =
        prefix ? `${prefix}/test.py` : "test.py";
    const path =
        prompt(
            "File path:",
            suggestion
        );
    if (
        path === null
    ) {
        return;
    }
    const cleanPath =
        path.trim();
    if (!cleanPath) {
        return;
    }
    try {
        await api(
            `/api/projects/${encodeURIComponent(
                currentProject
            )}/file`,
            {
                method: "POST",
                headers: {
                    "Content-Type":
                        "application/json"
                },
                body:
                    JSON.stringify({
                        path:
                            cleanPath,
                        content:
                            ""
                    })
            }
        );
        if (prefix) {
            expandedFolders.add(prefix);
        }
        await loadFiles();
        await openFile(
            cleanPath
        );
        output.textContent =
            "Created:\n" +
            cleanPath;
    }
    catch (error) {
        alert(
            "Could not create file:\n" +
            error.message
        );
    }
}
/* =====================================================
   NEW FOLDER
===================================================== */
async function newFolder(prefix) {
    if (!currentProject) {
        alert(
            "Create a project first."
        );
        return;
    }
    const suggestion =
        prefix ? `${prefix}/new-folder` : "new-folder";
    const path =
        prompt(
            "Folder path:",
            suggestion
        );
    if (
        path === null
    ) {
        return;
    }
    const cleanPath =
        path.trim();
    if (!cleanPath) {
        return;
    }
    try {
        await api(
            `/api/projects/${encodeURIComponent(
                currentProject
            )}/folder`,
            {
                method: "POST",
                headers: {
                    "Content-Type":
                        "application/json"
                },
                body:
                    JSON.stringify({
                        path: cleanPath
                    })
            }
        );
        if (prefix) {
            expandedFolders.add(prefix);
        }
        expandedFolders.add(cleanPath);
        await loadFiles();
        output.textContent =
            "Created folder:\n" +
            cleanPath;
    }
    catch (error) {
        alert(
            "Could not create folder:\n" +
            error.message
        );
    }
}
/* =====================================================
   UPLOAD (files and zips - zips auto-extract)
===================================================== */
function triggerUpload(targetDir) {
    if (!currentProject) {
        alert(
            "Create a project first."
        );
        return;
    }
    uploadTargetDir = targetDir || "";
    document.getElementById(
        "uploadInput"
    ).click();
}
async function handleUploadInputChange() {
    const input =
        document.getElementById(
            "uploadInput"
        );
    const files = input.files;
    if (
        !files ||
        files.length === 0 ||
        !currentProject
    ) {
        return;
    }
    const formData = new FormData();
    for (
        const file
        of files
    ) {
        formData.append(
            "files",
            file,
            file.name
        );
    }
    formData.append(
        "target_dir",
        uploadTargetDir
    );
    output.textContent =
        "Uploading " +
        files.length +
        " item(s)...";
    try {
        const data =
            await api(
                `/api/projects/${encodeURIComponent(
                    currentProject
                )}/upload`,
                {
                    method: "POST",
                    body: formData
                }
            );
        if (uploadTargetDir) {
            expandedFolders.add(
                uploadTargetDir
            );
        }
        await loadFiles();
        let summary =
            "Uploaded:\n" +
            data.saved.join("\n");
        if (
            data.skipped &&
            data.skipped.length > 0
        ) {
            summary +=
                "\n\nSkipped (invalid or too large):\n" +
                data.skipped.join("\n");
        }
        output.textContent = summary;
    }
    catch (error) {
        output.textContent =
            "Upload failed:\n" +
            error.message;
    }
    finally {
        input.value = "";
    }
}
/* =====================================================
   RUN PROJECT
===================================================== */
async function runCurrent() {
    if (
        !currentProject
    ) {
        output.textContent = "No project selected.";
        return;
    }
    if (
        !currentFile
    ) {
        output.textContent = "Select a Python file first.";
        return;
    }
    if (
        !currentFile.endsWith(".py")
    ) {
        output.textContent = "Only Python files can be run.";
        return;
    }
    /*
     * Save before running.
     */
    if (
        cm.getValue() !==
        editor.dataset.saved
    ) {
        try {
            await saveFile();
        }
        catch {
            return;
        }
    }
    /*
     * Running now happens in the real interactive
     * terminal instead of the old sandboxed/timed-out
     * subprocess call. This means:
     *
     *  - no artificial time limit
     *  - input() prompts actually work
     *  - normal stdout/stderr colors and formatting
     *  - the process can be interrupted with Ctrl+C
     *    directly in the terminal
     *
     * On mobile this also switches to the Output/Terminal
     * tab so the user doesn't have to switch manually.
     */
    if (
        isMobile()
    ) {
        setMobileView(
            "output"
        );
    }
    sendTerminalCommand(
        "python3 -u " +
        shellQuote(currentFile)
    );
}
/* =====================================================
   EDITOR
   (dirty-state tracking and Tab handling now live on the
   CodeMirror instance itself - see cm.on("change", ...)
   and extraKeys.Tab set up where `cm` is created above.)
===================================================== */
/*
 * Ctrl+S / Cmd+S
 */
document.addEventListener(
    "keydown",
    async function(event) {
        if (
            (event.ctrlKey ||
             event.metaKey)
            &&
            event.key.toLowerCase()
                === "s"
        ) {
            event.preventDefault();
            await saveFile();
        }
    }
);
/*
 * Ctrl+Enter / Cmd+Enter
 * runs the current file.
 */
document.addEventListener(
    "keydown",
    function(event) {
        if (
            (event.ctrlKey ||
             event.metaKey)
            &&
            event.key === "Enter"
        ) {
            event.preventDefault();
            runCurrent();
        }
    }
);
/* =====================================================
   LIVE CAMERA (phone camera -> server OpenCV -> back)
===================================================== */
let cameraStream = null;
let cameraSocket = null;
let cameraRunning = false;
let cameraFacingMode = "environment";
let cameraCaptureTimer = null;
let cameraFramesInFlight = 0;
let cameraFpsCount = 0;
let cameraFpsTimer = null;
function setCameraStatus(status, title) {
    cameraStatusDot.className =
        "term-status-dot" +
        (status !== "disconnected" ? " " + status : "");
    cameraStatusDot.title = title || status;
}
function toggleCamera() {
    if (cameraRunning) {
        stopCamera("Stopped");
    } else {
        startCamera();
    }
}
async function startCamera() {
    if (!currentProject) {
        alert("Open or create a project first.");
        return;
    }
    if (
        !navigator.mediaDevices ||
        !navigator.mediaDevices.getUserMedia
    ) {
        alert(
            "This browser doesn't support camera access " +
            "(getUserMedia)."
        );
        return;
    }
    cameraStartBtn.disabled = true;
    setCameraStatus("connecting", "Starting camera...");
    try {
        cameraStream =
            await navigator.mediaDevices.getUserMedia({
                video: { facingMode: cameraFacingMode },
                audio: false
            });
    } catch (error) {
        cameraStartBtn.disabled = false;
        setCameraStatus("error", String(error));
        alert(
            "Couldn't access the camera: " +
            (error && error.message
                ? error.message
                : error)
        );
        return;
    }
    cameraVideoEl.srcObject = cameraStream;
    await cameraVideoEl.play().catch(() => {});
    const proto =
        location.protocol === "https:" ? "wss:" : "ws:";
    cameraSocket = new WebSocket(
        proto +
        "//" +
        location.host +
        "/ws/projects/" +
        encodeURIComponent(currentProject) +
        "/camera"
    );
    cameraSocket.binaryType = "arraybuffer";
    cameraSocket.onopen = () => {
        cameraRunning = true;
        cameraStartBtn.disabled = false;
        cameraStartBtn.innerHTML =
            '<span class="btn-icon"><svg class="icon"><use href="#i-square"></use></svg></span>' +
            '<span class="btn-label">Stop</span>';
        setCameraStatus("connected", "Streaming");
        cameraPlaceholder.classList.add("hide");
        cameraDisplayCanvas.classList.add("show");
        cameraFpsEl.classList.add("show");
        cameraFpsCount = 0;
        clearInterval(cameraFpsTimer);
        cameraFpsTimer = setInterval(() => {
            cameraFpsEl.textContent =
                cameraFpsCount + " fps";
            cameraFpsCount = 0;
        
        }, 1000);
        cameraFramesInFlight = 0;
        clearInterval(cameraCaptureTimer);
        cameraCaptureTimer = setInterval(
            captureAndSendCameraFrame,
            100
        );
    };
    cameraSocket.onmessage = (event) => {
        cameraFramesInFlight = Math.max(
            0,
            cameraFramesInFlight - 1
        );
        if (!(event.data instanceof ArrayBuffer)) {
            return;
        }
        const blob = new Blob(
            [event.data],
            { type: "image/jpeg" }
        );
        createImageBitmap(blob).then((bitmap) => {
            if (
                cameraDisplayCanvas.width !== bitmap.width ||
                cameraDisplayCanvas.height !== bitmap.height
            ) {
                cameraDisplayCanvas.width = bitmap.width;
                cameraDisplayCanvas.height = bitmap.height;
            }
            const ctx =
                cameraDisplayCanvas.getContext("2d");
            ctx.drawImage(bitmap, 0, 0);
            bitmap.close();
            cameraFpsCount++;
        }).catch(() => {});
    };
    cameraSocket.onclose = () => {
        if (cameraRunning) {
            stopCamera("Disconnected");
        }
    };
    cameraSocket.onerror = () => {
        setCameraStatus("error", "Connection error");
    };
}

/*
 * Grabs the current video frame, JPEG-encodes it, and
 * sends it to the server for processing. Waits for each
 * frame's reply before sending the next one (tracked via
 * cameraFramesInFlight) so a slow network/server can't
 * cause frames to pile up faster than they can be shown.
 */
function captureAndSendCameraFrame() {
    if (
        !cameraSocket ||
        cameraSocket.readyState !== WebSocket.OPEN
    ) {
        return;
    }
    if (cameraFramesInFlight > 0) {
        return;
    }
    if (
        !cameraVideoEl.videoWidth ||
        !cameraVideoEl.videoHeight
    ) {
        return;
    }
    cameraCaptureCanvas.width = cameraVideoEl.videoWidth;
    cameraCaptureCanvas.height = cameraVideoEl.videoHeight;
    const ctx = cameraCaptureCanvas.getContext("2d");
    ctx.drawImage(cameraVideoEl, 0, 0);
    cameraCaptureCanvas.toBlob(
        (blob) => {
            if (
                !blob ||
                !cameraSocket ||
                cameraSocket.readyState !== WebSocket.OPEN
            ) {
                return;
            }
            cameraFramesInFlight++;
            blob.arrayBuffer().then((buffer) => {
                if (
                    cameraSocket &&
                    cameraSocket.readyState === WebSocket.OPEN
                ) {
                    cameraSocket.send(buffer);
                }
            });
        },
        "image/jpeg",
        0.7
    );
}

function stopCamera(reason) {
    cameraRunning = false;
    clearInterval(cameraCaptureTimer);
    cameraCaptureTimer = null;
    clearInterval(cameraFpsTimer);
    cameraFpsTimer = null;
    cameraFramesInFlight = 0;
    if (cameraSocket) {
        try {
            cameraSocket.onclose = null;
            cameraSocket.close();
        } catch {}
        cameraSocket = null;
    }
    if (cameraStream) {
        cameraStream.getTracks().forEach(
            (track) => track.stop()
        );
        cameraStream = null;
    }
    cameraVideoEl.srcObject = null;
    cameraStartBtn.disabled = false;
    cameraStartBtn.innerHTML =
        '<span class="btn-icon"><svg class="icon"><use href="#i-play"></use></svg></span>' +
        '<span class="btn-label">Start</span>';
    cameraDisplayCanvas.classList.remove("show");
    cameraFpsEl.classList.remove("show");
    cameraPlaceholder.classList.remove("hide");
    setCameraStatus("disconnected", reason || "Stopped");
}

function switchCameraFacing() {
    cameraFacingMode =
        cameraFacingMode === "environment"
            ? "user"
            : "environment";
    if (cameraRunning) {
        stopCamera("Switching camera...");
        startCamera();
    }
}

/* =====================================================
   TERMINAL PANEL RESIZE (desktop)
   Drag the handle above the terminal to resize it.
   Persisted for this tab only (sessionStorage), and
   only applies on desktop - mobile already manages the
   terminal's height itself via data-mobile-view.
===================================================== */
const terminalResizeHandle = document.getElementById(
    "terminalResizeHandle"
);
const terminalEl = document.getElementById("terminal");
const TERMINAL_MIN_HEIGHT = 120;
function applyTerminalHeight(px) {
    const maxHeight = Math.max(
        TERMINAL_MIN_HEIGHT,
        window.innerHeight - 200
    );
    const clamped = Math.min(
        Math.max(px, TERMINAL_MIN_HEIGHT),
        maxHeight
    );
    terminalEl.style.height = clamped + "px";
    if (bottomTab === "terminal" && fitAddon) {
        try {
            fitAddon.fit();
        } catch {}
        sendTermResize();
    }
    return clamped;
}
(function setUpTerminalResize() {
    const saved = sessionStorage.getItem(
        "ide-terminal-height"
    );
    if (saved && !isMobile()) {
        applyTerminalHeight(parseInt(saved, 10));
    }
    if (!terminalResizeHandle) {
        return;
    }
    let dragging = false;
    let startY = 0;
    let startHeight = 0;
    function onMove(clientY) {
        const delta = startY - clientY;
        const next = applyTerminalHeight(
            startHeight + delta
        );
        sessionStorage.setItem(
            "ide-terminal-height",
            String(next)
        );
    }
    terminalResizeHandle.addEventListener(
        "pointerdown",
        (event) => {
            if (isMobile()) {
                return;
            }
            dragging = true;
            startY = event.clientY;
            startHeight = terminalEl.getBoundingClientRect().height;
            terminalResizeHandle.setPointerCapture(
                event.pointerId
            );
        }
    );
    terminalResizeHandle.addEventListener(
        "pointermove",
        (event) => {
            if (!dragging) {
                return;
            }
            onMove(event.clientY);
        }
    );
    ["pointerup", "pointercancel"].forEach((evt) => {
        terminalResizeHandle.addEventListener(evt, () => {
            dragging = false;
        });
    });
    /* Keyboard resize for accessibility (arrow keys) */
    terminalResizeHandle.addEventListener(
        "keydown",
        (event) => {
            if (isMobile()) {
                return;
            }
            const current = terminalEl.getBoundingClientRect()
                .height;
            if (event.key === "ArrowUp") {
                event.preventDefault();
                const next = applyTerminalHeight(current + 20);
                sessionStorage.setItem(
                    "ide-terminal-height",
                    String(next)
                );
            }
            else if (event.key === "ArrowDown") {
                event.preventDefault();
                const next = applyTerminalHeight(current - 20);
                sessionStorage.setItem(
                    "ide-terminal-height",
                    String(next)
                );
            }
        }
    );
})();

/* =====================================================
   MOBILE CODING TOOLBAR
   Inserts characters at CodeMirror's actual cursor
   position (or wraps the current selection for paired
   characters) - not a fake overlay, this talks straight
   to the same `cm` instance the rest of the app uses.
===================================================== */
function mobileInsertText(text) {
    cm.replaceSelection(text, "end");
    cm.focus();
}
function mobileInsertPair(open, close) {
    if (cm.somethingSelected()) {
        const selected = cm.getSelection();
        cm.replaceSelection(
            open + selected + close,
            "around"
        );
    }
    else {
        const pos = cm.getCursor();
        cm.replaceSelection(open + close, "start");
        cm.setCursor({
            line: pos.line,
            ch: pos.ch + open.length
        });
    }
    cm.focus();
}
function mobileMoveCursor(delta) {
    const pos = cm.getCursor();
    const newCh = Math.max(0, pos.ch + delta);
    cm.setCursor({ line: pos.line, ch: newCh });
    cm.focus();
}

/* =====================================================
   BOOTSTRAP
   Kicks the whole app off - loads (or creates) a
   project and populates the file tree. Everything else
   (Run, Save, the terminal, etc.) is inert until this
   has set currentProject.
===================================================== */
loadProjects();
