import time
from llm_client import LLMClient
from difflib import SequenceMatcher

from audio_capture import DualAudioCapture
from transcriber import Transcriber
from transcript_store import TranscriptStore
from meeting_context import MeetingContext
from conversation_processor import ConversationProcessor


CHUNK_DURATION = 60
OVERLAP = 5


def normalize_text(text):

    return " ".join(
        text.lower().strip().split()
    )


def is_duplicate_segment(
    current,
    previous_segments,
    overlap_start,
    overlap_end
):

    current_start = current["start"]
    current_end = current["end"]

    # Segment doesn't touch overlap
    if (
        current_end <= overlap_start
        or current_start >= overlap_end
    ):
        return False

    current_text = normalize_text(
        current["text"]
    )

    if not current_text:
        return False

    for previous in previous_segments:

        previous_start = previous["start"]
        previous_end = previous["end"]

        # No temporal overlap
        if (
            current_end <= previous_start
            or current_start >= previous_end
        ):
            continue

        previous_text = normalize_text(
            previous["text"]
        )

        if not previous_text:
            continue

        similarity = SequenceMatcher(
            None,
            current_text,
            previous_text
        ).ratio()

        # Strong text match
        if similarity >= 0.75:

            return True

        # If one transcript is contained inside
        # the other, it's almost certainly duplicate.
        if (
            current_text in previous_text
            or previous_text in current_text
        ):
            return True

    return False


def deduplicate_segments(
    current_segments,
    previous_segments,
    overlap_start,
    overlap_end
):

    result = []

    for segment in current_segments:

        if is_duplicate_segment(
            segment,
            previous_segments,
            overlap_start,
            overlap_end
        ):
            continue

        result.append(segment)

    return result


def main():

    capture = DualAudioCapture(
        chunk_duration=CHUNK_DURATION,
        overlap=OVERLAP
    )

    try:

        # ========================================
        # DEVICE SELECTION
        # ========================================

        system_device = (
            capture.select_output_device()
        )

        microphone_device = (
            capture.select_microphone()
        )

        # ========================================
        # WHISPER
        # ========================================

        transcriber = Transcriber(
            model_size="small",
            device="cpu",
            compute_type="int8"
        )

        # ========================================
        # STORAGE
        # ========================================

        store = TranscriptStore()

        context = MeetingContext(
            recent_seconds=300
        )

        processor = ConversationProcessor()

        llm = LLMClient()

        last_llm_call = time.time()
        LLM_INTERVAL = 180  # 3 minutes

        print("\nTranscript files:")
        print("TXT :", store.txt_file)
        print("JSON:", store.json_file)

        # ========================================
        # START CAPTURE
        # ========================================

        capture.start(
            system_device,
            microphone_device
        )

        print(
            "\nMeeting capture running..."
        )

        print(
            "Chunk size : 60 seconds"
        )

        print(
            "Overlap    : 5 seconds"
        )

        print(
            "Whisper input : 65 seconds"
        )

        print(
            "\nPress Ctrl+C to stop.\n"
        )

        # ========================================
        # PREVIOUS ACCEPTED SEGMENTS
        # ========================================

        previous_you_segments = []
        previous_others_segments = []

        # ========================================
        # PROCESSING LOOP
        # ========================================

        while True:

            # ------------------------------------
            # Wait for chunk
            # ------------------------------------

            if not capture.chunk_ready():

                time.sleep(0.1)

                continue

            result = (
                capture.record_chunk()
            )

            if result is None:

                continue

            (
                system_file,
                mic_file,
                chunk_number
            ) = result

            print(
                f"\n========== CHUNK "
                f"{chunk_number} =========="
            )

            # ====================================
            # TRANSCRIBE OTHERS
            # ====================================

            print(
                "\nTranscribing OTHERS..."
            )

            others_segments = (
                transcriber.transcribe(
                    system_file
                )
            )

            # ====================================
            # TRANSCRIBE YOU
            # ====================================

            print(
                "Transcribing YOU..."
            )

            you_segments = (
                transcriber.transcribe(
                    mic_file
                )
            )

            # ====================================
            # GLOBAL TIME
            # ====================================

            chunk_start = (
                (chunk_number - 1)
                * CHUNK_DURATION
            )

            overlap_start = (
                chunk_start
                if chunk_number == 1
                else chunk_start
            )

            overlap_end = (
                chunk_start
                + OVERLAP
            )

            # ====================================
            # CONVERT TO GLOBAL TIMESTAMPS
            # ====================================

            for segment in you_segments:

                segment["start"] += (
                    chunk_start
                )

                segment["end"] += (
                    chunk_start
                )

            for segment in others_segments:

                segment["start"] += (
                    chunk_start
                )

                segment["end"] += (
                    chunk_start
                )

            # ====================================
            # REMOVE DUPLICATES
            # ====================================

            if chunk_number > 1:

                you_segments = (
                    deduplicate_segments(
                        you_segments,
                        previous_you_segments,
                        overlap_start,
                        overlap_end
                    )
                )

                others_segments = (
                    deduplicate_segments(
                        others_segments,
                        previous_others_segments,
                        overlap_start,
                        overlap_end
                    )
                )

            # ====================================
            # SAVE FOR NEXT CHUNK
            # ====================================

            previous_you_segments = (
                you_segments
            )

            previous_others_segments = (
                others_segments
            )

            # ====================================
            # STORE TRANSCRIPT
            # ====================================

            store.add_chunk(
                you_segments=you_segments,
                others_segments=others_segments,
                chunk_start=0
            )

            # ====================================
            # COMBINE
            # ====================================

            combined = []

            for segment in you_segments:

                combined.append({
                    "speaker": "YOU",
                    "start": segment["start"],
                    "end": segment["end"],
                    "text": (
                        segment["text"]
                        .strip()
                    )
                })

            for segment in others_segments:

                combined.append({
                    "speaker": "OTHERS",
                    "start": segment["start"],
                    "end": segment["end"],
                    "text": (
                        segment["text"]
                        .strip()
                    )
                })

            combined.sort(
                key=lambda x: x["start"]
            )

            # ====================================
            # PROCESS CONVERSATION
            # ====================================

            combined = processor.process(
                combined
            )

            # Add cleaned conversation to context
            context.add_segments(
                combined
            )


            
            

            # ====================================
            # BUILD CLEAN CONTEXT
            # ====================================

            conversation = (
                processor.build_conversation(
                    combined
                )
            )

            questions = (
                processor.find_questions(
                    combined
                )
            )

            important = (
                processor.find_important(
                    combined
                )
            )

            # ====================================
            # LLM TRIGGER
            # ====================================

            current_time = time.time()

            if current_time - last_llm_call >= LLM_INTERVAL:

                print("\n" + "=" * 70)
                print("GENERATING MEETING SUGGESTIONS...")
                print("=" * 70)

                try:

                    meeting_context = context.build_context()

                    if meeting_context.strip():

                        suggestion = (
                            llm.generate_suggestions(
                                meeting_context
                            )
                        )

                        print("\n")
                        print("🤖 MEETING ASSISTANT")
                        print("-" * 70)
                        print(suggestion)
                        print("-" * 70)

                    last_llm_call = current_time

                except Exception as e:

                    print(
                        "\n⚠️ LLM request failed:"
                    )

                    print(e)

            # ====================================
            # DISPLAY
            # ====================================

            print("\n" + "=" * 70)
            print("CLEAN CONVERSATION")
            print("=" * 70)

            print(conversation)

            print("\nQUESTIONS DETECTED:")

            for question in questions:

                print(
                    f"[{question['speaker']}] "
                    f"{question['text']}"
                )

            print("\nIMPORTANT POINTS:")

            for item in important:

                print(
                    f"[{item['speaker']}] "
                    f"{item['text']}"
                )

            print("=" * 70)

    except KeyboardInterrupt:

        print(
            "\n\nCtrl+C detected."
        )

    except Exception as e:

        print(
            "\n❌ Fatal error:"
        )

        print(e)

    finally:

        capture.close()

        print(
            "\nMeeting Assistant stopped."
        )


if __name__ == "__main__":
    main()