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


// ========================================
// BACKEND STATUS
// ========================================

window.meetingAssistant.onBackendStatus(
    (state) => {

        if (state === "connected") {

            status.textContent =
                "LIVE";

            statusDot.classList.add(
                "connected"
            );

        } else {

            status.textContent =
                "Disconnected";

            statusDot.classList.remove(
                "connected"
            );
        }
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

        if (message.type === "SUGGESTION") {
            updateSuggestion(message);
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
// UPDATE SUGGESTION
// ========================================

function updateSuggestion(data) {

    setText(
        question,
        data.question
    );

    setText(
        answer,
        data.answer
    );

    setText(
        followup,
        data.follow_up
    );

    setText(
        keypoint,
        data.key_point
    );
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

const helpButton =
    document.getElementById("helpButton");


helpButton.addEventListener(
    "click",
    () => {

        window.meetingAssistant.requestHelp();

    }
);