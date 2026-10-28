"""
actions/study_solver.py — Academic Problem Solver for Universal Learning Station
Solves NCTB HSC Physics, Chemistry, Higher Math, Biology, ICT, and custom subjects.
Displays step-by-step LaTeX-enabled solution cards on the Study Board.
"""

from __future__ import annotations

import datetime
import json
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any, Dict, Optional

# Core engine imports
try:
    from core import model_router as _router
except Exception:
    _router = None

try:
    from memory import study_cache as _cache
except Exception:
    _cache = None

try:
    from actions.web_search import _search
except Exception:
    _search = None


def _fix_bangla_unicode(obj: Any) -> Any:
    """Safe NFC normalization without deleting spaces between Bangla words."""
    if isinstance(obj, str):
        if not obj:
            return ""
        text = unicodedata.normalize("NFC", obj)
        text = re.sub(r"[\u200b\u200e\u200f]", "", text)
        # Only collapse accidental space immediately around hasant (্)
        text = re.sub(r"([\u0980-\u09FF])\s*্\s*([\u0980-\u09FF])", r"\1্\2", text)
        return text
    elif isinstance(obj, dict):
        return {k: _fix_bangla_unicode(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_fix_bangla_unicode(item) for item in obj]
    return obj

# Workspace for saving academic code snippets
_BASE_DIR = Path(__file__).resolve().parent.parent
_CODE_DIR = _BASE_DIR / "workspace" / "code"

TOOL: Dict[str, Any] = {
    "name": "study_solve_problem",
    "description": (
        "Solves academic syllabus problems, math derivations, concept questions, and explains "
        "code step-by-step on the Study Board. Formats clean visual cards with LaTeX equations."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "question": {
                "type": "string",
                "description": "The exact math problem, theory question, or code topic to solve.",
            },
            "subject": {
                "type": "string",
                "enum": [
                    "physics",
                    "chemistry",
                    "higher_math",
                    "biology",
                    "ict",
                    "bangla",
                    "english",
                    "general",
                ],
                "description": "Subject category. Determines the solver's academic domain guidelines.",
            },
            "from_screen": {
                "type": "string",
                "enum": ["yes", "no"],
                "description": "Set to 'yes' if the question was extracted from the user's computer screen.",
            },
            "reveal": {
                "type": "string",
                "enum": ["all", "steps"],
                "description": "'steps' reveals cards one-by-one with Next Step; 'all' shows the full solution at once.",
            },
            "force_refresh": {
                "type": "string",
                "enum": ["yes", "no"],
                "description": "Set to 'yes' to bypass local cache and regenerate fresh.",
            },
        },
        "required": ["question"],
    },
}

SUBJECT_PROMPTS: Dict[str, str] = {
    "physics": (
        "NCTB HSC Physics context. Explain principles intuitively in clear Bangla. Format ALL "
        "formula names, scientific labels, and variable expansions in clean Bangla using \\text{...} inside LaTeX "
        "(e.g., $$\\text{ত্বরণ} = \\frac{\\text{বেগের পরিবর্তন}}{\\text{সময়}}$$, $F = ma$). "
        "Prevent raw English strings in formula titles. "
        "List: 1. প্রদত্ত মান ও একক, 2. প্রয়োজনীয় সূত্র, 3. পর্যায়ক্রমিক গণনা ও ব্যাখ্যা, 4. এককসহ চূড়ান্ত উত্তর।"
    ),
    "chemistry": (
        "NCTB HSC Chemistry context. Balanced chemical reactions with state symbols, clear Bangla explanations, "
        "and step-by-step stoichiometry or electronic configurations. "
        "All formula names, titles, and variable expansions must be in clean Bangla (e.g., $$\\frac{\\text{মোলার ভর}}{\\text{স্থূল সংকেতের ভর}}$$). "
        "Prevent raw English strings in formula titles."
    ),
    "higher_math": (
        "NCTB HSC Higher Mathematics context. Calculus, algebra, coordinate geometry, or vectors. "
        "Show EVERY algebraic step cleanly in display LaTeX ($$...$$) with clear Bangla reasoning beside each step. "
        "All theorem and formula titles must be in clean Bangla (e.g., $$\\text{ক্ষেত্রফল} = \\int y \\, dx$$)."
    ),
    "biology": (
        "NCTB HSC Biology context. Clear Bangla explanations with standard scientific terms in clean Bangla "
        "(English terms allowed only in brackets after Bangla term). "
        "Use bullet points for anatomical differences and functions."
    ),
    "ict": (
        "NCTB HSC ICT context. Number systems, Boolean algebra, logic circuits, and HTML/C programming. "
        "Provide clean, formatted code blocks with English syntax and Bangla explanations."
    ),
    "bangla": "Use standard grammatical rules and clean academic Bangla.",
    "english": "Explain English grammar rules, sentence corrections, or writing structures clearly.",
    "general": (
        "Higher education, business math, or advanced programming. Structured breakdown with practical examples "
        "and clean Bangla terminology."
    ),
}

# Regex to protect LaTeX backslashes from JSON corruption
_LATEX_ESC_RX = re.compile(r"\\([a-zA-Z]+|\{|\})")



_BN_DIGITS = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")

def _to_bn_num(n: int) -> str:
    return str(n).translate(_BN_DIGITS)

def _ensure_numbered_lines(md: str) -> tuple[str, str]:
    """Tags ONLY sequential mathematical derivation lines with a subtle marker
    for the right-corner '?' button, and builds a clean 1..N line map for voice queries."""
    lines = md.splitlines()
    out_lines = []
    line_map = []
    line_no = 0
    in_code = False
    bs = chr(92)

    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            out_lines.append(raw)
            continue
        if in_code or not stripped:
            out_lines.append(raw)
            continue

        # Strip any old bulky [লাইন X] / [Line X] tags that the LLM or cache might have included
        cleaned = re.sub(r"\*\*\[(?:লাইন|Line)\s*[০-৯0-9]+\]\*\*\s*", "", stripped)
        cleaned = re.sub(r"\[(?:লাইন|Line)\s*[০-৯0-9]+\]\s*", "", cleaned)
        cleaned_no_bullet = re.sub(r"^[-*•]\s+", "", cleaned).strip()

        # Never tag headings, dividers, or final answer summary text
        if (
            cleaned.startswith("#")
            or cleaned in ("---", "***")
            or re.match(r"^\*\*(?:চূড়ান্ত উত্তর|Final Answer)", cleaned_no_bullet, re.I)
            or cleaned_no_bullet.startswith(("সুতরাং,", "অতএব, প্রদত্ত", "প্রদত্ত তথ্য", "সূত্র:"))
        ):
            out_lines.append(cleaned)
            continue

        # Only tag genuine sequential math derivation lines (equations with =, ⇒, বা, ∴, \frac, etc.)
        has_eq = (
            ("=" in cleaned_no_bullet and ("$" in cleaned_no_bullet or bs in cleaned_no_bullet or any(c.isdigit() for c in cleaned_no_bullet)))
            or cleaned_no_bullet.startswith(("বা,", "বা ", "⇒", "∴", bs + "Rightarrow", bs + "therefore"))
            or re.search(r"\$\$.*?\=.*?\$\$", cleaned_no_bullet)
        )

        if has_eq:
            line_no += 1
            bn_idx = _to_bn_num(line_no)
            # Append a clean hidden marker [[L:N]] at the end of the line so JS can render a tiny right-corner '?'
            out_lines.append(f"- {cleaned_no_bullet} [[L:{bn_idx}]]")
            line_map.append(f"Line {line_no} (লাইন {bn_idx}): {cleaned_no_bullet}")
        else:
            out_lines.append(cleaned)

    return "\n".join(out_lines), "\n".join(line_map)


def _guard_latex(text: str) -> str:
    """Repairs corrupted LaTeX backslashes, form-feeds (\x0crac), and shorthand frac57."""
    if not text:
        return ""
    bs = chr(92)
    # 1. Restore control chars that swallowed backslashes (\f -> \frac, \t -> \text/\tan/\times, \b -> \beta)
    text = text.replace("\x0crac", bs + "frac").replace("\x0c", bs + "f")
    text = re.sub(r"\x08(eta|ar|inom|egin|f)", r"\\b\1", text)
    text = re.sub(r"\t(ext|an|heta|au|imes|hickapprox|herefore)", r"\\t\1", text)
    text = re.sub(r"\r(ight|ho)", r"\\r\1", text)

    # 2. Fix missing backslash before frac / sqrt / text
    text = re.sub(r"(?<!\\)\bfrac\s*\{", r"\\frac{", text)
    text = re.sub(r"(?<!\\)\bsqrt\s*\{", r"\\sqrt{", text)
    text = re.sub(r"(?<!\\)\btext\s*\{", r"\\text{", text)

    # 3. Fix shorthand `\frac57` or `frac57` or `frac 5 7` -> `\frac{5}{7}`
    text = re.sub(r"(?:\\)?\bfrac\s*([0-9০-৯a-zA-Z])\s*([0-9০-৯a-zA-Z])(?![0-9০-৯a-zA-Z\}])", r"\\frac{\1}{\2}", text)
    text = re.sub(r"(?:\\)?\bfrac\s+([0-9০-৯a-zA-Z]+)\s+([0-9০-৯a-zA-Z]+)", r"\\frac{\1}{\2}", text)

    # 4. Merge split equations where `x =` is on one line and `$$...$$` is on the next line
    text = re.sub(r"(\b[a-zA-Z]\s*=\s*)\n+\s*\$\$([\s\S]+?)\$\$", r"$$\1 \2$$", text)
    text = re.sub(r"\$([a-zA-Z]\s*=)\$\s*\n*\s*\$\$([\s\S]+?)\$\$", r"$$\1 \2$$", text)
    # Also keep inline `x = $$...$$` on a single inline/display block
    text = re.sub(r"([a-zA-Z]\s*=\s*)\$\$([\s\S]+?)\$\$", r"$\1 \2$", text)
    return text


def _archive_code_blocks(subject: str, solution_md: str, title: str) -> None:
    """Extracts code blocks from the solution and archives them cleanly in workspace/code/."""
    code_matches = re.findall(r"```(c|python|cpp|html|javascript)?\s*\n(.*?)```", solution_md, re.S)
    if not code_matches:
        return

    safe_title = re.sub(r"[^\w\-]", "_", title.strip())[:30] or "snippet"
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    for lang, snippet in code_matches:
        lang = (lang or "txt").lower()
        ext = {"c": "c", "python": "py", "cpp": "cpp", "html": "html", "javascript": "js"}.get(lang, "txt")
        dest_dir = _CODE_DIR / lang
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            file_path = dest_dir / f"{safe_title}_{timestamp}.{ext}"
            file_path.write_text(snippet.strip(), encoding="utf-8")
        except Exception:
            pass


def _detect_subject(question: str) -> str:
    """Fallback detector if subject isn't passed."""
    q = question.lower()
    if any(k in q for k in ["dy/dx", "dx", "ইন্টিগ্রেশন", "ডিফারেনসিয়েশন", "ম্যাট্রিক্স", "ফাংশন", "উচ্চতর গণিত", "matrix", "integral"]):
        return "higher_math"
    if any(k in q for k in ["বেগ", "ত্বরণ", "কুলম্ব", "মহাকর্ষ", "তড়িৎ", "পদার্থ", "physics", "force", "velocity", "vector"]):
        return "physics"
    if any(k in q for k in ["মোল", "যোজ্যতা", "বিক্রিয়া", "রসায়ন", "chemistry", "acid", "reaction", "compound"]):
        return "chemistry"
    if any(k in q for k in ["কোষ", "ডিএনএ", "মাইটোসিস", "টেরিডোফাইটা", "ব্রায়োফাইটা", "জীববিজ্ঞান", "biology"]):
        return "biology"
    if any(k in q for k in ["সি প্রোগ্রামিং", "এইচটিএমএল", "লজিক গেট", "আইসিটি", "ict", "html", "printf"]):
        return "ict"
    return "general"


def study_solve_problem(parameters: Dict[str, Any], player: Any = None, **kwargs) -> str:
    question = str(parameters.get("question") or "").strip()
    if not question:
        return "[STUDY_BOARD] Error: Question parameter cannot be empty."

    subject = str(parameters.get("subject") or "").strip().lower()
    if subject not in SUBJECT_PROMPTS:
        subject = _detect_subject(question)

    from_screen = str(parameters.get("from_screen") or "no").lower() == "yes"
    # Always start step-by-step from Step 1 unless user explicitly asked for all at once
    reveal = "steps"
    force_refresh = str(parameters.get("force_refresh") or "no").lower() == "yes"

    # When the user explicitly asks to solve/explain a problem on the board,
    # ensure any stale quiz lock is released so the explanation renders cleanly.
    try:
        from actions import study_quiz
        if hasattr(study_quiz, "set_quiz_active"):
            study_quiz.set_quiz_active(False)
    except Exception:
        pass

    # 1. Engage Study Mode if not already active
    try:
        if player and hasattr(player, "set_study_mode"):
            if not getattr(player, "study_mode_active", False):
                player.set_study_mode(True)

        # 2. Check local zero-cost cache
        cache_key = f"solve_{subject}_{question}"
        if not force_refresh and _cache and hasattr(_cache, "get_cached"):
            cached_data = _cache.get_cached("solution", query=cache_key)
            if cached_data:
                c_title = _fix_bangla_unicode(cached_data.get("title", "Solution"))
                c_md = _fix_bangla_unicode(cached_data.get("markdown", ""))
                c_final = _fix_bangla_unicode(cached_data.get("final_answer", ""))
                if player and hasattr(player, "study_solution"):
                    player.study_solution(
                        c_title,
                        c_md,
                        subject,
                        reveal,
                    )
                c_md, c_line_map = _ensure_numbered_lines(c_md)
                if player and hasattr(player, "study_solution"):
                    player.study_solution(c_title, c_md, subject, reveal)
                return (
                    f"[STUDY_BOARD] (Cached) Solved on Study Board (Step 1 shown first): '{c_title}'\n"
                    f"FINAL: {c_final}\n"
                    f"[SOLUTION_LINES_CONTEXT]:\n{c_line_map or c_md[:2500]}\n\n"
                    f"[FULL_SOLUTION_MARKDOWN]:\n{c_md[:3000]}"
                )

        # 3. Post 'Solving...' status to the board
        if player and hasattr(player, "study_status"):
            player.study_status(f"সমাধান প্রস্তুত করা হচ্ছে ({subject.replace('_', ' ').title()})…")

        # 4. Web search fallback if not from screen and question is a specific factual query
        web_snippet = ""
        if not from_screen and _search and len(question.split()) < 25:
            try:
                res = _search(question)
                if res and isinstance(res, str) and len(res) > 20:
                    web_snippet = f"\n\n[Search Reference Context (Untrusted)]: {res[:2000]}"
            except Exception:
                pass

        # 5. Build prompt
        subj_guide = SUBJECT_PROMPTS.get(subject, SUBJECT_PROMPTS["general"])
        sys_prompt = (
            "You are a master academic teacher for a Bangladeshi HSC Science student.\n"
            f"Domain Guide: {subj_guide}\n\n"
            "Formatting & Bangla Formula Rules:\n"
            "1. Title Line: Start with a concise title in clean Bangla: `# <বাংলা শিরোনাম>` (Never use English titles).\n"
            "2. Break the solution into logical, numbered step cards using markdown `## ` headings (ALWAYS start the very first card with `## ধাপ ১: প্রদত্ত তথ্য ও সূত্র` so nothing is cut off at the top):\n"
            "   - `## ধাপ ১: প্রদত্ত তথ্য ও সূত্র` (Given & Formulas)\n"
            "   - `## ধাপ ২: পর্যায়ক্রমিক গণনা ও সমীকরণ` (Step-by-Step Derivation)\n"
            "   - `## ধাপ ৩: চূড়ান্ত উত্তর` (Final Answer)\n"
            "2B. SEQUENTIAL MATH EQUATIONS:\n"
            "   - In Step 2 (`## ধাপ ২: পর্যায়ক্রমিক গণনা ও সমীকরণ`), write each algebraic/calculation line on its own line starting with `বা, ` or `⇒ ` or `∴ ` inside `$...$` (e.g., `- বা, $3x = 5$`).\n"
            "   - Always keep both sides of an equation inside the SAME `$...$` or `$$...$$` block (e.g., `$x = \\frac{5}{7}$`). NEVER put `x =` outside and `\\frac{5}{7}` on a new line!\n"
            "   - ALWAYS write fractions with explicit curly braces: `\\frac{a}{b}` (NEVER write `frac57` or `\\frac57`).\n"
            "3. Bangla Scientific Terminology & LaTeX Formulas:\n"
            "   - Strictly require ALL formula names, scientific labels, and variable expansions in clean Bangla.\n"
            "   - Format formula titles and variable names inside LaTeX using \\text{...} in Bangla (e.g., $$\\frac{\\text{মোলার ভর}}{\\text{স্থূল সংকেতের ভর}}$$, $$\\text{বল} = \\text{ভর} \\times \\text{ত্বরণ}$$).\n"
            "   - Prevent raw English strings in formula titles, labels, or step headings.\n"
            "4. Enclose inline math in `$ ... $` and display equations in `$$ ... $$`.\n"
            "5. Conclude with a clear bold line: **চূড়ান্ত উত্তর: <মান ও একক>** (or **Final Answer: <value with unit>**).\n"
            "6. QUIZ INTEGRITY (ANTI-CHEATING): If an exam or quiz is currently active and the user is asking for direct answers, solutions, or choices, you MUST strictly refuse with: 'এটি একটি পরীক্ষা চলছে। পরীক্ষার সময় আমি সরাসরি উত্তর বা সমাধান বলে দিতে পারব না।'"
        )

        user_prompt = f"Question: {question}{web_snippet}"

        # 6. Route to appropriate model
        task = "study" if subject in ("higher_math", "physics", "chemistry") else "study_fast"
        solution_raw = ""
        if _router and hasattr(_router, "call"):
            solution_raw = _router.call(
                prompt=user_prompt,
                task=task,
                system=sys_prompt,
                timeout=45,
                budget=90,
            )

        if not solution_raw:
            try:
                from core import gemini as _gemini
                solution_raw = _gemini.text(f"{sys_prompt}\n\n{user_prompt}", tier=_gemini.FAST)
            except Exception:
                pass

        if not solution_raw:
            if player and hasattr(player, "study_status"):
                player.study_status("সমাধান তৈরি করা সম্ভব হয়নি। দয়া করে আবার চেষ্টা করুন।")
            return "[STUDY_BOARD] Solver failed to get a response from the model router."

        solution_md = _fix_bangla_unicode(_guard_latex(solution_raw.strip()))
        solution_md, line_map_str = _ensure_numbered_lines(solution_md)

        # Extract Title
        title_match = re.search(r"^#\s+(.+)$", solution_md, re.M)
        title = _fix_bangla_unicode(title_match.group(1).strip() if title_match else f"{subject.replace('_', ' ').title()} Solution")

        # Extract Final Answer for voice summary
        final_match = re.search(r"\*\*(?:Final Answer|চূড়ান্ত উত্তর)[^*]*\*\*:\s*([^\n]+)", solution_md, re.I)
        final_ans = _fix_bangla_unicode(final_match.group(1).strip() if final_match else "See Study Board")

        # 7. Render on Study Board
        if player and hasattr(player, "study_solution"):
            player.study_solution(title, solution_md, subject, reveal)

        # 8. Archive any code blocks
        threading.Thread(target=_archive_code_blocks, args=(subject, solution_md, title), daemon=True).start()

        # 9. Save to local cache
        if _cache and hasattr(_cache, "save_cache"):
            _cache.save_cache(
                "solution",
                query=cache_key,
                payload={
                    "title": title,
                    "markdown": solution_md,
                    "subject": subject,
                    "final_answer": final_ans,
                },
            )

        # Return tool feedback WITH complete Line-by-Line map so JARVIS can answer "explain line N"
        return (
            f"[STUDY_BOARD] Solved on Study Board (Step-by-Step mode — Step 1 is now shown at the top; student can press Enter/Space for next steps): '{title}'\n"
            f"Subject: {subject} | FINAL: {final_ans}\n"
            f"Briefly introduce Step 1 in Bangla. Tell the student they can press Enter for the next step, or ask you to explain any specific line number (e.g. '৩ নম্বর লাইনটা বুঝিয়ে দাও').\n\n"
            f"[SOLUTION_LINES_CONTEXT — USE THIS WHEN STUDENT ASKS ABOUT A SPECIFIC LINE NUMBER]:\n"
            f"{line_map_str or solution_md[:2500]}\n\n"
            f"[FULL_SOLUTION_MARKDOWN]:\n{solution_md[:3000]}"
        )
    except Exception as e:
        if player and hasattr(player, "study_status"):
            player.study_status("সমাধান তৈরিতে সমস্যা দেখা দিয়েছে।")
        return f"[STUDY_BOARD] Solver error: {e}"

TOOL["handler"] = study_solve_problem
