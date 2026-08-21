const {
    contextBridge,
    ipcRenderer
} = require("electron");


contextBridge.exposeInMainWorld(
    "meetingAssistant",
    {
        requestHelp: () => {
            ipcRenderer.send("REQUEST_HELP");
        },

        closeWindow: () => {
            ipcRenderer.send("close-window");
        },

        requestRegenerate: () => {
            ipcRenderer.send("regenerate");
        },

        toggleCollapse: (isCollapsed) => {
            ipcRenderer.send("toggle-collapse", isCollapsed);
        },

        onBackendStatus: (callback) => {
            ipcRenderer.on("backend-status", (event, state) => callback(state));
        },

        onMessage: (callback) => {
            ipcRenderer.on("backend-message", (event, message) => callback(message));
        },

        onHotkey: (callback) => {
            ipcRenderer.on("hotkey", (event) => callback());
        }
    }
);