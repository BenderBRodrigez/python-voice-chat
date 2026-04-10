import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from string import Template
from typing import Annotated

from dotenv import load_dotenv

load_dotenv()

from anthropic import Anthropic
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper import WhisperModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

whisper_model = WhisperModel("base", device="cpu", compute_type="int8")

anthropic_client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

_questions_file = Path(__file__).parent / "assets" / "questions.json"
interview_questions: list[str] = json.loads(_questions_file.read_text())[
    "questions"
]

_prompt_template_file = Template(
    (Path(__file__).parent / "assets" / "prompt_tmpl.txt").read_text()
)

_summary_prompt_template = Template(
    (Path(__file__).parent / "assets" / "summary_prompt_tmpl.txt").read_text()
)

# In-memory conversation storage
conversations = {}

# One initial turn + up to 2 follow-ups per question.
MAX_TURNS_PER_QUESTION = 3


def transcribe_audio(audio_path: str) -> str:
    """Transcribe audio file using faster-whisper."""
    segments, info = whisper_model.transcribe(audio_path, beam_size=5)
    transcription = " ".join([segment.text for segment in segments])
    return transcription.strip()


def parse_turn_json(text: str) -> dict:
    """Parse the turn evaluator's JSON output, tolerating stray code fences."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def analyze_response_with_claude(
    question: str,
    user_answer: str,
    conversation_history: list,
    question_index: int,
    attempt: int,
) -> dict:
    """
    Score the latest answer on S and P and decide whether to follow up.
    Returns dict with 'response', 'completed', and 'scores'.
    """

    if attempt >= MAX_TURNS_PER_QUESTION:
        return {
            "response": "Thank you, let's move on.",
            "completed": True,
            "scores": None,
        }

    follow_ups_remaining = MAX_TURNS_PER_QUESTION - attempt - 1
    system_prompt = _prompt_template_file.substitute(
        question_index=question_index + 1,
        question=question,
        follow_ups_remaining=follow_ups_remaining,
    )

    messages = conversation_history + [
        {
            "role": "user",
            "content": f"User's answer: {user_answer}",
        }
    ]

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=system_prompt,
        messages=messages,
    )

    response_text = next(
        block.text for block in response.content if block.type == "text"
    )

    try:
        parsed = parse_turn_json(response_text)
    except (json.JSONDecodeError, ValueError):
        return {
            "response": response_text.strip() or "Thank you, let's move on.",
            "completed": True,
            "scores": None,
        }

    return {
        "response": parsed["response"],
        "completed": bool(parsed.get("next", True)),
        "scores": parsed.get("scores"),
        "weakest": parsed.get("weakest"),
    }


def generate_summary(conversation: dict) -> str:
    """Generate an interview summary using Claude based on scores and transcript."""
    if conversation.get("summary"):
        return conversation["summary"]

    scores = conversation["scores"]
    history = conversation["history"]

    question_scores: dict[int, list] = {}
    for entry in scores:
        qi = entry["question_index"]
        question_scores.setdefault(qi, []).append(entry)

    scores_text = ""
    for qi, entries in sorted(question_scores.items()):
        q = interview_questions[qi]
        last = entries[-1]
        scores_text += (
            f"Q{qi + 1}: {q}\n"
            f"  Specificity={last['S']}, Problem-solving={last['P']}, weakest={last['weakest']}\n\n"
        )

    transcript_lines = []
    for msg in history:
        role = "Candidate" if msg["role"] == "user" else "Interviewer"
        transcript_lines.append(f"{role}: {msg['content']}")
    transcript_text = "\n".join(transcript_lines)

    prompt = _summary_prompt_template.substitute(
        scores_text=scores_text,
        transcript_text=transcript_text,
    )

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )

    summary = next(
        block.text for block in response.content if block.type == "text"
    )
    conversation["summary"] = summary
    return summary


@app.post("/api/answer")
async def process_answer(
    audio: Annotated[UploadFile, File()],
    session_id: Annotated[str, Form()],
):
    """
    Process audio answer from user.
    Returns the AI response and conversation state.
    """

    if session_id not in conversations:
        raise HTTPException(status_code=400, detail="Invalid session ID")

    conversation = conversations[session_id]

    if conversation["done"]:
        return JSONResponse(
            {
                "response": "The interview is complete. Thank you!",
                "questionIndex": len(interview_questions),
                "done": True,
                "sessionId": session_id,
            }
        )

    try:
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=".webm"
        ) as temp_audio:
            content = await audio.read()
            temp_audio.write(content)
            temp_audio_path = temp_audio.name

        transcription = transcribe_audio(temp_audio_path)

        os.unlink(temp_audio_path)

        if not transcription:
            raise HTTPException(
                status_code=400, detail="Could not transcribe audio"
            )

        question_index = conversation["question_index"]
        current_question = interview_questions[question_index]

        conversation["history"].append(
            {
                "role": "user",
                "content": transcription,
            }
        )

        result = analyze_response_with_claude(
            current_question,
            transcription,
            conversation["history"],
            question_index,
            conversation["attempt"],
        )

        if result.get("scores") is not None:
            conversation["scores"].append(
                {
                    "question_index": question_index,
                    "attempt": conversation["attempt"],
                    "S": result["scores"].get("S"),
                    "P": result["scores"].get("P"),
                    "weakest": result.get("weakest"),
                }
            )

        conversation["history"].append(
            {
                "role": "assistant",
                "content": result["response"],
            }
        )

        if result["completed"]:
            conversation["question_index"] += 1
            conversation["attempt"] = 0

            if conversation["question_index"] >= len(interview_questions):
                conversation["done"] = True
                return JSONResponse(
                    {
                        "response": result["response"]
                        + "\n\nThat completes our interview. Thank you for your time!",
                        "questionIndex": conversation["question_index"],
                        "done": True,
                        "sessionId": session_id,
                        "transcription": transcription,
                    }
                )

            next_question = interview_questions[conversation["question_index"]]

            return JSONResponse(
                {
                    "response": result["response"],
                    "nextQuestion": next_question,
                    "questionIndex": conversation["question_index"],
                    "done": False,
                    "sessionId": session_id,
                    "transcription": transcription,
                }
            )

        conversation["attempt"] += 1

        return JSONResponse(
            {
                "response": result["response"],
                "questionIndex": question_index,
                "done": False,
                "sessionId": session_id,
                "transcription": transcription,
            }
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/start")
async def start_interview():
    """Start a new interview session."""
    session_id = str(uuid.uuid4())

    conversations[session_id] = {
        "question_index": 0,
        "attempt": 0,
        "history": [],
        "done": False,
        "scores": [],
    }

    return JSONResponse(
        {
            "sessionId": session_id,
            "question": interview_questions[0],
            "questionIndex": 0,
            "totalQuestions": len(interview_questions),
        }
    )


@app.get("/api/summary")
async def get_summary(session_id: str):
    """Return a generated summary for a completed interview session."""
    if session_id not in conversations:
        raise HTTPException(status_code=400, detail="Invalid session ID")

    conversation = conversations[session_id]
    if not conversation["done"]:
        raise HTTPException(
            status_code=400, detail="Interview not yet complete"
        )

    summary = generate_summary(conversation)
    return JSONResponse({"summary": summary})


frontend_path = Path(__file__).parent.parent / "frontend"
if frontend_path.exists():
    app.mount(
        "/",
        StaticFiles(directory=str(frontend_path), html=True),
        name="frontend",
    )
