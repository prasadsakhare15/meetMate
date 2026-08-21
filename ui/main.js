const {
    app,
    BrowserWindow,
    globalShortcut,
    ipcMain
} = require("electron");

const path = require("path");
const WebSocket = require("ws");

let mainWindow;
let ws;

function createWindow() {

    mainWindow = new BrowserWindow({
        width: 420,
        height: 520,

        frame: false,
        transparent: true,
        resizable: false,

        alwaysOnTop: true,

        skipTaskbar: false,

        webPreferences: {
            preload: path.join(
                __dirname,
                "preload.js"
            ),

            contextIsolation: true,
            nodeIntegration: false
        }
    });

    mainWindow.loadFile(
        path.join(
            __dirname,
            "index.html"
        )
    );

    // Keep above normal application windows
    mainWindow.setAlwaysOnTop(
        true,
        "floating"
    );
}

ipcMain.on(
    "close-window",
    () => {
        mainWindow.close();
    }
);

ipcMain.on(
    "regenerate",
    () => {
        console.log(
            "Regenerate requested"
        );

        if (
            ws &&
            ws.readyState === 1
        ) {
            ws.send(
                JSON.stringify({
                    type: "HELP_ME"
                })
            );
        }
    }
);

ipcMain.on(
    "toggle-collapse",
    (event, isCollapsed) => {
        if (isCollapsed) {
            mainWindow.setSize(420, 64);
        } else {
            mainWindow.setSize(420, 520);
        }
    }
);

ipcMain.on(
    "REQUEST_HELP",
    () => {

        console.log(
            "🚨 HELP ME requested"
        );

        if (
            ws &&
            ws.readyState === 1
        ) {

            ws.send(
                JSON.stringify({
                    type: "HELP_ME"
                })
            );

        }

    }
);





function connectToBackend() {

    ws = new WebSocket(
        "ws://127.0.0.1:8765"
    );

    ws.on("open", () => {

        console.log(
            "Connected to Python backend"
        );

        mainWindow.webContents.send(
            "backend-status",
            "connected"
        );
    });

    ws.on("message", (data) => {

        try {

            const message = JSON.parse(
                data.toString()
            );

            mainWindow.webContents.send(
                "backend-message",
                message
            );

        } catch (error) {

            console.error(
                "Invalid backend message:",
                error
            );
        }
    });

    ws.on("close", () => {

        console.log(
            "Backend disconnected"
        );

        mainWindow.webContents.send(
            "backend-status",
            "disconnected"
        );

        setTimeout(
            connectToBackend,
            3000
        );
    });

    ws.on("error", () => {
        // close event will handle reconnect
    });
}

app.whenReady().then(() => {

    createWindow();

    // Global shortcut for later
   globalShortcut.register(
    "CommandOrControl+Space",
    () => {

        console.log(
            "🚨 HELP ME shortcut"
        );

        if (
            ws &&
            ws.readyState === 1
        ) {

            ws.send(
                JSON.stringify({
                    type: "HELP_ME"
                })
            );

        }

        mainWindow.webContents.send("hotkey");
    }
);

    // Give Electron a moment to load
    setTimeout(
        connectToBackend,
        1000
    );
});

app.on(
    "will-quit",
    () => {

        globalShortcut.unregisterAll();

        if (ws) {
            ws.close();
        }
    }
);