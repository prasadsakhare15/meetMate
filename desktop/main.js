const {
    app,
    BrowserWindow,
    globalShortcut,
    ipcMain
} = require("electron");

const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const WebSocket = require("ws");

const WINDOW_WIDTH = 420;
const WINDOW_HEIGHT = 640;

let mainWindow;
let ws;
let agentProcess = null;
let reconnectTimer = null;
let isQuitting = false;

function createWindow() {

    mainWindow = new BrowserWindow({
        width: WINDOW_WIDTH,
        height: WINDOW_HEIGHT,
        frame: false,
        transparent: true,
        resizable: false,
        alwaysOnTop: true,
        skipTaskbar: false,
        webPreferences: {
            preload: path.join(__dirname, "preload.js"),
            contextIsolation: true,
            nodeIntegration: false
        }
    });

    mainWindow.loadFile(path.join(__dirname, "index.html"));
    mainWindow.setAlwaysOnTop(true, "floating");
}

function resolvePython() {

    const venvPython = path.join(
        __dirname,
        "..",
        "agent",
        "venv",
        "Scripts",
        "python.exe"
    );

    if (fs.existsSync(venvPython)) {
        return venvPython;
    }

    return process.platform === "win32" ? "python" : "python3";
}

function startAgent() {

    if (process.env.ELECTRON_SKIP_AGENT === "1") {
        console.log("Skipping agent spawn (ELECTRON_SKIP_AGENT=1)");
        return;
    }

    const agentDir = path.join(__dirname, "..", "agent");
    const python = resolvePython();

    console.log("Starting local agent:", python);

    agentProcess = spawn(python, ["main.py"], {
        cwd: agentDir,
        windowsHide: true,
        env: {
            ...process.env,
            PYTHONUNBUFFERED: "1"
        }
    });

    agentProcess.stdout.on("data", (data) => {
        console.log("[agent]", data.toString().trimEnd());
    });

    agentProcess.stderr.on("data", (data) => {
        console.error("[agent]", data.toString().trimEnd());
    });

    agentProcess.on("exit", (code) => {
        console.log("Agent exited with code", code);
        agentProcess = null;
    });
}

function stopAgent() {

    if (!agentProcess) {
        return;
    }

    const pid = agentProcess.pid;

    if (process.platform === "win32" && pid) {
        spawn("taskkill", ["/pid", String(pid), "/t", "/f"], {
            windowsHide: true
        });
    } else {
        agentProcess.kill("SIGTERM");
    }

    agentProcess = null;
}

function sendToAgent(message) {

    if (ws && ws.readyState === 1) {
        ws.send(JSON.stringify(message));
    }
}

function connectToBackend() {

    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }

    if (isQuitting) {
        return;
    }

    if (ws) {
        try {
            ws.removeAllListeners();
            ws.close();
        } catch (error) {
            // ignore
        }
    }

    ws = new WebSocket("ws://127.0.0.1:8765");

    ws.on("open", () => {
        console.log("Connected to Python backend");

        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send("backend-status", "connected");
        }

        sendToAgent({ type: "LIST_DEVICES" });
    });

    ws.on("message", (data) => {
        let parsed;

        try {
            parsed = JSON.parse(data.toString());
        } catch (error) {
            console.error("Invalid backend message:", error);
            return;
        }

        console.log("Backend message:", parsed.type);

        if (!mainWindow || mainWindow.isDestroyed()) {
            return;
        }

        const sendToRenderer = () => {
            mainWindow.webContents.send("backend-message", parsed);
        };

        if (mainWindow.webContents.isLoading()) {
            mainWindow.webContents.once("did-finish-load", sendToRenderer);
            return;
        }

        sendToRenderer();
    });

    ws.on("close", () => {
        console.log("Backend disconnected");

        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send("backend-status", "disconnected");
        }

        if (!isQuitting) {
            reconnectTimer = setTimeout(connectToBackend, 2000);
        }
    });

    ws.on("error", (error) => {
        console.error("Backend socket error:", error.message);
    });
}

ipcMain.on("close-window", () => {
    if (mainWindow) {
        mainWindow.close();
    }
});

ipcMain.on("regenerate", () => {
    sendToAgent({ type: "HELP_ME" });
});

ipcMain.on("REQUEST_HELP", () => {
    sendToAgent({ type: "HELP_ME" });
});

ipcMain.on("agent-message", (_event, message) => {
    sendToAgent(message);
});

ipcMain.on("toggle-collapse", (_event, isCollapsed) => {
    if (!mainWindow) {
        return;
    }

    if (isCollapsed) {
        mainWindow.setSize(WINDOW_WIDTH, 64);
    } else {
        mainWindow.setSize(WINDOW_WIDTH, WINDOW_HEIGHT);
    }
});

app.whenReady().then(() => {
    startAgent();
    createWindow();

    globalShortcut.register("CommandOrControl+Space", () => {
        sendToAgent({ type: "HELP_ME" });

        if (mainWindow && !mainWindow.isDestroyed()) {
            mainWindow.webContents.send("hotkey");
        }
    });

    setTimeout(connectToBackend, 800);
});

app.on("before-quit", () => {
    isQuitting = true;

    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
    }

    globalShortcut.unregisterAll();

    if (ws) {
        ws.close();
    }

    stopAgent();
});
