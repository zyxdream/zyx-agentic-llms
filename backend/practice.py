"""Voice Mentor Mode API endpoints."""

import json
from io import BytesIO
from flask import Blueprint, request, jsonify, Response
from flask_cognito import cognito_auth_required, current_user
from backend.models import Project
from backend.everything import bm25_search_chunks
from backend.ai.base import DEFAULT_MODEL, multichat_completion_text, chat_completion_j, get_openai_client

bp_practice = Blueprint("practice", __name__, url_prefix="/api/practice")

# ---------------------------------------------------------------------------
# Role & difficulty configuration
# ---------------------------------------------------------------------------

ROLE_PROMPTS = {
    "mentor": (
        "You are an AI legal argument mentor helping a user practice oral advocacy.\n\n"
        "Your role is to provide constructive, practical, and professional coaching. "
        "You are not acting as a judge or opposing counsel. Do not decide the case. "
        "Do not provide legal advice as a licensed attorney. Focus on helping the user "
        "improve the clarity, structure, persuasiveness, and delivery of their argument.\n\n"
        "Be clear, concise, practical, professional, and encouraging but honest. "
        "Do not be overly adversarial, too verbose, or generic.\n\n"
        "Respond with:\n"
        "1. A brief overall assessment (2-3 sentences)\n"
        "2. Two specific strengths in their argument\n"
        "3. Two specific areas to improve\n"
        "4. One suggested revised phrasing or structure\n"
        "5. One recommended next practice step\n\n"
        "Write in plain prose — no markdown, no bullet formatting. "
        "Keep the response concise enough to be spoken aloud naturally."
    ),
    "judge": (
        "You are playing the role of a federal district court judge during oral argument. "
        "The attorney is practicing their argument before you.\n\n"
        "React as a serious, experienced judge would: ask probing questions, press on weak "
        "points of law, challenge unsupported factual assertions, and test whether the "
        "attorney can handle interruptions. You may be skeptical, impatient, or curious "
        "depending on what the argument warrants.\n\n"
        "Do not give coaching feedback — stay fully in character as the judge. "
        "Respond with the kinds of questions or comments a real judge would make from the bench. "
        "Keep responses to 2-4 sentences. Write as if speaking, not writing. "
        "No markdown, no bullet points."
    ),
    "opponent": (
        "You are opposing counsel in a courtroom proceeding. "
        "The user is practicing their side of the argument against you.\n\n"
        "Your job is to challenge their argument as vigorously as a skilled opposing attorney would: "
        "raise counter-arguments, exploit weaknesses in their legal theory, distinguish adverse "
        "precedent they may rely on, and surface factual disputes. Be adversarial but professional.\n\n"
        "Do not break character to give coaching. Respond as opposing counsel would respond "
        "during argument — with objections, counter-points, or aggressive questioning. "
        "Keep responses to 2-5 sentences. Write as if speaking, not writing. "
        "No markdown, no bullet points."
    ),
}

DIFFICULTY_INSTRUCTIONS = {
    "supportive": (
        "Be especially encouraging. Emphasize what is working well. "
        "Suggest only one improvement at a time and frame it gently."
    ),
    "balanced": (
        "Balance encouragement with honest, direct feedback. "
        "Acknowledge strengths and be clear about what needs improvement."
    ),
    "challenging": (
        "Be rigorous and demanding. Push hard on logical gaps, weak factual support, "
        "unclear legal standards, and vague requested relief. "
        "A real judge or opposing counsel would not be lenient."
    ),
}

FOCUS_LABELS = {
    "general_argument":  "General argument practice",
    "opening_argument":  "Opening argument",
    "motion_argument":   "Motion argument",
    "rebuttal":          "Rebuttal argument",
    "objection_response": "Objection response",
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_matter_context(project_id: int, transcript: str) -> str:
    """Return relevant document chunks from the matter using transcript keywords."""
    keywords = [w for w in transcript.split() if len(w) > 3][:20]
    if not keywords:
        return ""
    try:
        chunks = bm25_search_chunks(
            draft_id=None,
            keywords=keywords,
            top_k=8,
            project_id=project_id,
            file_scope="all_matter_and_chat",
        )
        return "\n\n".join(c.get("content", "") for c in chunks if c.get("content"))
    except Exception:
        return ""


def _build_user_prompt(transcript: str, matter_context: str,
                       practice_focus: str, difficulty: str) -> str:
    focus_label = FOCUS_LABELS.get(practice_focus, practice_focus)
    difficulty_instr = DIFFICULTY_INSTRUCTIONS.get(difficulty, DIFFICULTY_INSTRUCTIONS["balanced"])

    parts = []
    if matter_context:
        parts.append(f"Relevant matter context:\n{matter_context}")
    parts.append(f"Practice focus: {focus_label}")
    parts.append(f"Difficulty guidance: {difficulty_instr}")
    parts.append(f"Attorney's argument:\n{transcript}")
    parts.append("Provide your mentor coaching response:")
    return "\n\n".join(parts)


def _history_to_pairs(history: list) -> list:
    """Convert [{role, text}, ...] history to [(user_text, mentor_text), ...] tuples."""
    pairs = []
    i = 0
    while i + 1 < len(history):
        if history[i].get("role") == "user" and history[i + 1].get("role") == "mentor":
            pairs.append((history[i]["text"], history[i + 1]["text"]))
            i += 2
        else:
            i += 1
    return pairs


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@bp_practice.post("/transcribe")
@cognito_auth_required
def practice_transcribe():
    """Transcribe a recorded audio blob using OpenAI Whisper."""
    if 'audio' not in request.files:
        return jsonify({"error": "audio file is required"}), 400

    audio_file = request.files['audio']
    filename = audio_file.filename or 'recording.webm'
    content_type = audio_file.content_type or 'audio/webm'
    audio_bytes = audio_file.read()

    if not audio_bytes:
        return jsonify({"error": "empty audio file"}), 400

    try:
        result = get_openai_client().audio.transcriptions.create(
            model="whisper-1",
            file=(filename, BytesIO(audio_bytes), content_type),
        )
        return jsonify({"transcript": result.text})
    except Exception as e:
        return jsonify({"error": f"Transcription failed: {str(e)}"}), 500


@bp_practice.post("/chat")
@cognito_auth_required
def practice_chat():
    """Submit a spoken argument transcript; receive mentor coaching response."""
    data = request.json or {}
    matter_id = data.get("matter_id")
    transcript = (data.get("transcript") or "").strip()
    history = data.get("history") or []
    practice_focus = data.get("practice_focus", "general_argument")
    difficulty = data.get("difficulty", "balanced")
    role = data.get("role", "mentor")

    if not transcript:
        return jsonify({"error": "transcript is required"}), 400

    if matter_id:
        project = Project.query.filter_by(id=matter_id).first()
        if not project or project.userId != current_user.id:
            return jsonify({"error": "Matter not found"}), 404
        matter_context = _get_matter_context(int(matter_id), transcript)
    else:
        matter_context = ""

    sys_prompt = ROLE_PROMPTS.get(role, ROLE_PROMPTS["mentor"])
    user_prompt = _build_user_prompt(transcript, matter_context, practice_focus, difficulty)
    prev_pairs = _history_to_pairs(history)

    response_text = multichat_completion_text(
        prev_prompt_output=prev_pairs,
        new_prompt=user_prompt,
        sys_prompt=sys_prompt,
        model=DEFAULT_MODEL,
        reasoning_effort="high" if DEFAULT_MODEL in {"gpt-6-sol", "gpt-6.1-sol"} else None,
    )

    if not response_text:
        return jsonify({"error": "Failed to generate mentor response"}), 500

    return jsonify({"response": response_text})


@bp_practice.post("/tts")
@cognito_auth_required
def practice_tts():
    """Convert mentor response text to speech audio (MP3)."""
    data = request.json or {}
    text = (data.get("text") or "").strip()
    voice = data.get("voice", "onyx")

    if not text:
        return jsonify({"error": "text is required"}), 400

    text = text[:4096]

    try:
        audio_response = get_openai_client().audio.speech.create(
            model="tts-1",
            voice=voice,
            input=text,
        )
        return Response(
            audio_response.content,
            mimetype="audio/mpeg",
            headers={
                "Content-Disposition": "inline",
                "Cache-Control": "no-store",
            },
        )
    except Exception as e:
        return jsonify({"error": f"TTS failed: {str(e)}"}), 500


_SUMMARY_SYS_PROMPT = (
    "You are an AI legal argument coach generating a structured post-practice summary.\n"
    "Review the full session and provide an honest, constructive assessment.\n\n"
    "Respond with a JSON object with exactly these keys:\n"
    '{\n'
    '  "overall_assessment": "string",\n'
    '  "strengths": ["string", "string"],\n'
    '  "areas_to_improve": ["string", "string"],\n'
    '  "suggested_rewrite": "string",\n'
    '  "next_step": "string"\n'
    '}'
)


@bp_practice.post("/summary")
@cognito_auth_required
def practice_summary():
    """Generate a structured post-session feedback summary from all turns."""
    data = request.json or {}
    turns = data.get("turns") or []
    practice_focus = data.get("practice_focus", "general_argument")

    if not turns:
        return jsonify({"error": "turns is required"}), 400

    lines = []
    for i, turn in enumerate(turns, 1):
        lines.append(f"[Round {i}]")
        lines.append(f"Attorney: {turn.get('user', '')}")
        lines.append(f"Mentor: {turn.get('mentor', '')}")
        lines.append("")
    session_transcript = "\n".join(lines)

    focus_label = FOCUS_LABELS.get(practice_focus, practice_focus)
    summary_prompt = (
        f"Practice focus: {focus_label}\n\n"
        f"Full session transcript:\n{session_transcript}\n\n"
        "Generate a structured coaching summary based on the full session above."
    )

    raw = chat_completion_j(
        prompt=summary_prompt,
        sys_prompt=_SUMMARY_SYS_PROMPT,
        model=DEFAULT_MODEL,
        reasoning_effort="high" if DEFAULT_MODEL in {"gpt-6-sol", "gpt-6.1-sol"} else None,
    )
    if not raw:
        return jsonify({"error": "Failed to generate summary"}), 500

    try:
        return jsonify(json.loads(raw))
    except json.JSONDecodeError:
        return jsonify({"error": "Failed to parse summary"}), 500
