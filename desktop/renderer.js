const windowElement =
    document.querySelector(".window");

const content =
    document.getElementById("content");

const footer =
    document.getElementById("footer");

const question =
    document.getElementById("question");

const answer =
    document.getElementById("answer");

const followup =
    document.getElementById("followup");

const keypoint =
    document.getElementById("keypoint");

const status =
    document.getElementById("status");

const statusDot =
    document.getElementById("status-dot");

const collapseBtn =
    document.getElementById("collapse-btn");

const closeBtn =
    document.getElementById("close-btn");

const copyBtn =
    document.getElementById("copy-btn");

const regenerateBtn =
    document.getElementById(
        "regenerate-btn"
    );

const helpButton =
    document.getElementById("helpButton");

const outputSelect =
    document.getElementById("output-select");

const micSelect =
    document.getElementById("mic-select");

const captureBtn =
    document.getElementById("capture-btn");

let isThinking = false;
let isCapturing = false;
let isConnected = false;
let whisperReady = false;


// ========================================
// BACKEND STATUS
// ========================================

window.meetingAssistant.onBackendStatus(
    (state) => {

        isConnected = state === "connected";

        if (state !== "connected") {
            isCapturing = false;
            updateCaptureButton();
            setActionsEnabled(false);

            if (!isThinking) {
                setAppStatus("Disconnected", "disconnected");
            }
            return;
        }

        if (isThinking) {
            return;
        }

        setAppStatus("LIVE", "connected");
        window.meetingAssistant.sendToAgent({
            type: "LIST_DEVICES"
        });
    }
);


// ========================================
// BACKEND MESSAGE
// ========================================

window.meetingAssistant.onMessage(
    (message) => {

        console.log(
            "Backend message:",
            message
        );

        if (message.type === "DEVICES") {
            whisperReady = Boolean(message.whisper_ready);
            fillDeviceSelect(outputSelect, message.outputs || []);
            fillDeviceSelect(micSelect, message.microphones || []);
            isCapturing = Boolean(message.capturing);
            updateCaptureButton();
            setActionsEnabled(isCapturing && !isThinking);
        }

        if (message.type === "CAPTURE_STARTED") {
            isCapturing = true;
            updateCaptureButton();
            setActionsEnabled(!isThinking);
        }

        if (message.type === "CAPTURE_STOPPED") {
            isCapturing = false;
            updateCaptureButton();
            setActionsEnabled(false);
        }

        if (message.type === "SUGGESTION") {
            expandWindow();
            updateSuggestion(message);
        }

        if (message.type === "STATUS") {
            handleStatus(message.text);
        }

        if (message.type === "ERROR") {
            handleStatus("ERROR");
        }

        if (
            message.type ===
            "QUESTION"
        ) {

            setText(
                question,
                message.text
            );
        }
    }
);


// ========================================
// STATUS HANDLING
// ========================================

function handleStatus(text) {

    const normalized = (text || "").toUpperCase();

    if (normalized === "THINKING...") {
        isThinking = true;
        setAppStatus("Thinking...", "thinking");
        setActionsEnabled(false);
        return;
    }

    if (normalized === "LISTENING") {
        isThinking = false;
        setAppStatus("Listening", "connected");
        setActionsEnabled(isCapturing);
        return;
    }

    if (normalized === "ERROR") {
        isThinking = false;
        setAppStatus("Error", "error");
        setActionsEnabled(isCapturing);
        return;
    }

    setAppStatus(text, "connected");
}


function setAppStatus(text, dotState) {

    status.textContent = text;

    statusDot.classList.remove(
        "connected",
        "thinking",
        "error"
    );

    if (dotState) {
        statusDot.classList.add(dotState);
    }
}


function setActionsEnabled(enabled) {

    helpButton.disabled = !enabled;
    regenerateBtn.disabled = !enabled;

    helpButton.classList.toggle(
        "disabled",
        !enabled
    );

    regenerateBtn.classList.toggle(
        "disabled",
        !enabled
    );
}


function fillDeviceSelect(select, devices) {

    const previous = select.value;

    select.innerHTML = "";

    if (!devices.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No devices found";
        select.appendChild(option);
        return;
    }

    devices.forEach((device) => {
        const option = document.createElement("option");
        option.value = String(device.index);
        option.textContent = device.name;
        select.appendChild(option);
    });

    if (
        previous &&
        [...select.options].some((option) => option.value === previous)
    ) {
        select.value = previous;
    }
}


function updateCaptureButton() {

    captureBtn.disabled = !isConnected;

    if (isCapturing) {
        captureBtn.textContent = "Stop meeting";
        captureBtn.classList.add("running");
        outputSelect.disabled = true;
        micSelect.disabled = true;
        return;
    }

    captureBtn.textContent = whisperReady
        ? "Start meeting"
        : "Start when ready";
    captureBtn.classList.remove("running");
    outputSelect.disabled = false;
    micSelect.disabled = false;
}


// ========================================
// UPDATE SUGGESTION
// ========================================

function updateSuggestion(data) {

    const questionText = data.question || "";
    const answerText = data.answer || "";
    const followupText = data.follow_up || "";
    const keypointText = data.key_point || "";

    setText(question, questionText);
    setText(
        answer,
        answerText || (
            data.question_detected === false
                ? "No question needs a response right now."
                : ""
        )
    );
    setText(followup, followupText);
    setText(keypoint, keypointText);
}


function expandWindow() {

    if (!windowElement.classList.contains("collapsed")) {
        return;
    }

    windowElement.classList.remove("collapsed");
    collapseBtn.textContent = "−";
    window.meetingAssistant.toggleCollapse(false);
}


// ========================================
// SET TEXT
// ========================================

function setText(element, text) {

    if (!text) {

        element.textContent = "—";

        element.classList.add(
            "empty"
        );

        return;
    }

    element.textContent = text;

    element.classList.remove(
        "empty"
    );
}


// ========================================
// COLLAPSE
// ========================================

collapseBtn.addEventListener(
    "click",
    () => {

        windowElement.classList.toggle(
            "collapsed"
        );

        const isCollapsed =
            windowElement.classList.contains(
                "collapsed"
            );

        collapseBtn.textContent =
            isCollapsed
                ? "+"
                : "−";

        window.meetingAssistant.toggleCollapse(
            isCollapsed
        );
    }
);


// ========================================
// COPY ANSWER
// ========================================

copyBtn.addEventListener(
    "click",
    async () => {

        const text =
            answer.textContent;

        if (
            !text ||
            text === "—"
        ) {
            return;
        }

        await navigator.clipboard.writeText(
            text
        );

        copyBtn.textContent =
            "Copied";

        setTimeout(
            () => {

                copyBtn.textContent =
                    "Copy";

            },
            1200
        );
    }
);


// ========================================
// REGENERATE
// ========================================

regenerateBtn.addEventListener(
    "click",
    () => {

        if (regenerateBtn.disabled) {
            return;
        }

        window.meetingAssistant
            .requestRegenerate();
    }
);


// ========================================
// CLOSE
// ========================================

closeBtn.addEventListener(
    "click",
    () => {

        window.meetingAssistant
            .closeWindow();
    }
);


// ========================================
// HOTKEY
// ========================================

window.meetingAssistant.onHotkey(
    () => {

        if (
            windowElement.classList.contains(
                "collapsed"
            )
        ) {
            windowElement.classList.remove(
                "collapsed"
            );

            collapseBtn.textContent = "−";

            window.meetingAssistant.toggleCollapse(
                false
            );
        }
    }
);

helpButton.addEventListener(
    "click",
    () => {

        if (helpButton.disabled) {
            return;
        }

        window.meetingAssistant.requestHelp();

    }
);

captureBtn.addEventListener(
    "click",
    () => {

        if (!isConnected || captureBtn.disabled) {
            return;
        }

        if (isCapturing) {
            window.meetingAssistant.sendToAgent({
                type: "STOP_CAPTURE"
            });
            return;
        }

        window.meetingAssistant.sendToAgent({
            type: "START_CAPTURE",
            output_index: Number(outputSelect.value),
            mic_index: Number(micSelect.value)
        });
    }
);

setActionsEnabled(false);
updateCaptureButton();