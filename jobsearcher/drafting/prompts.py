"""Prompts for drafting and for the grounding check."""

from __future__ import annotations

from jobsearcher.config import LetterStyle
from jobsearcher.models import Contact, Job
from jobsearcher.ranking.ranker import JobAssessment

# Bump when a prompt or schema changes in a way that should invalidate cached drafts.
PROMPT_VERSION = "4"

DRAFT_SYSTEM = """\
You write job application documents for one candidate, using only the CVs you are given. \
Everything is written in English, whatever language the job ad is in, using the same \
spelling (American or British) as the CVs.

THE HARD RULE: never invent anything about the candidate. Use only facts that are stated in \
the CVs. You may select, reorder, shorten and rephrase them, and you may use the ad's own \
words for things the CVs already support. You may NOT add employers, job titles, dates, \
numbers, percentages, team sizes, budgets, skills, tools, certifications, degrees, languages \
or contact details that are not in the CVs, and you may not make an achievement bigger than \
the CVs state. If the ad asks for something the CVs don't show, don't claim it: leave it out \
of the documents and say so in `notes`.

Cover letter: about 250-350 words, three or four short paragraphs, specific to this role and \
employer, in a warm, direct, professional voice. Open with why this role, show two or three \
pieces of the candidate's real experience that fit the ad's main requirements, and close with \
a simple next step. Use the salutation given in the request. Sign off with the candidate's \
name as written in the CV. Don't mention scores, rankings or that an AI wrote it. Don't \
mention work permits or relocation unless the instructions say to.

Tailored CV: Markdown, in the same structure as the base CV (a "# Name" heading, a contact \
line, a summary, core competencies, experience, education/certification). Keep every \
employer, title and date exactly as in the CVs; reorder and trim bullets so the most relevant \
come first, rewrite the summary for this role, and keep it to about two pages.

Her standing instructions, when given, apply to every application: follow them, but they \
never allow facts the CVs don't contain.

`notes`: short plain-English points for the candidate: what you emphasised and why, which \
requirements her CV doesn't cover, and any question only she can answer.
"""

CHECK_SYSTEM = """\
You verify job application documents against the candidate's CVs. The CVs are the only \
source of truth about the candidate.

List every factual claim the documents make about the CANDIDATE: employment (employers, \
titles, dates), responsibilities, achievements, numbers, scope, skills, tools, \
certifications, education, languages, location. For each claim give a short quote from the \
CVs that supports it, or mark it unsupported.

A claim is supported when the CVs say the same thing, even reworded or summarised. It is \
unsupported when it adds a specific the CVs don't contain (a new number, tool, title, \
employer, date, certification, or a bigger scope or result than the CVs state). Statements \
about the employer or the role (taken from the ad), opinions, enthusiasm, and the greeting \
are not claims about the candidate: don't list them.
"""


def cv_context(cvs: list[tuple[str, str]]) -> str:
    """The CVs as stable context (cacheable by providers): the base CV first, then the
    others as extra facts about the same person."""
    (base_name, base_text), rest = cvs[0], cvs[1:]
    parts = [f"# Base CV ({base_name})", base_text.strip()]
    for name, text in rest:
        parts += ["", f"# Other CV: {name} (more facts about the same person)", text.strip()]
    return "\n".join(parts)


def salutation(contact: Contact | None, greeting: str = "Dear") -> str:
    if contact and contact.name:
        return f"{greeting} {contact.name},"
    # "Dear Hiring Manager," but "To the Hiring Manager,"
    return (
        f"{greeting} Hiring Manager," if greeting == "Dear" else f"{greeting} the Hiring Manager,"
    )


def letterhead(style: LetterStyle | None, regarding: str, date: str) -> list[str]:
    """The letterhead the letter starts with, when the profile wants one."""
    if style is None or not style.header:
        return []
    return [
        "",
        "Start the cover letter with this letterhead, one item per line (single line breaks, "
        "no blank lines between them), then a blank line and the salutation:",
        "1. Her name in bold, exactly as in the base CV's heading",
        '2. Her email and phone number from the CV, separated by " | " (when the CV has more '
        "than one number, pick one as her standing instructions say)",
        f"3. Re: {regarding}",
        f"4. {date}",
    ]


def _instruction_lines(standing: str, instructions: str) -> list[str]:
    lines = []
    if standing.strip():
        lines += ["", f"Her standing instructions (for every application): {standing.strip()}"]
    if instructions.strip():
        lines += ["", f"Her instructions for this one: {instructions.strip()}"]
    return lines


def ad_prompt(
    job: Job,
    assessment: JobAssessment | None,
    contact: Contact | None,
    instructions: str,
    max_ad_chars: int = 12_000,
    standing: str = "",
    style: LetterStyle | None = None,
    date: str = "",
) -> str:
    lines = [
        "Write a tailored CV and cover letter for this job.",
        "",
        f"Job title: {job.title}",
        f"Employer: {job.company or '(not stated)'}",
    ]
    place = ", ".join(x for x in (job.location, job.region) if x)
    if place:
        lines.append(f"Location: {place}")
    if job.deadline:
        lines.append(f"Application deadline: {job.deadline.date().isoformat()}")
    lines += letterhead(style, job.title, date)
    greeting = style.greeting if style else "Dear"
    lines += ["", f"Salutation to use: {salutation(contact, greeting)}"]
    if contact and contact.name:
        role = f" ({contact.role})" if contact.role else ""
        lines.append(f"The ad names this contact person: {contact.name}{role}.")
    else:
        lines.append("The ad names no contact person; don't invent one.")
    if assessment:
        if assessment.matched_requirements:
            lines += ["", "Requirements her CV already meets (lead with these):"]
            lines += [f"- {r}" for r in assessment.matched_requirements]
        if assessment.missing_requirements:
            lines += ["", "Requirements her CV doesn't show (don't claim them):"]
            lines += [f"- {r}" for r in assessment.missing_requirements]
    lines += _instruction_lines(standing, instructions)
    ad = job.description or "(no description available)"
    if len(ad) > max_ad_chars:
        ad = ad[:max_ad_chars] + "\n[ad truncated]"
    lines += ["", "Ad text:", ad]
    return "\n".join(lines)


def spontaneous_prompt(
    company: str,
    signals: list[dict[str, str]],
    target_roles: list[str],
    contact: Contact | None,
    instructions: str,
    standing: str = "",
    style: LetterStyle | None = None,
    date: str = "",
) -> str:
    lines = [
        "Write a tailored CV and a SPONTANEOUS (unsolicited) application cover letter to this "
        "company. There is no open position: don't pretend there is one, and don't name a "
        "specific vacancy.",
        "",
        f"Company: {company}",
        f"Salutation to use: {salutation(contact, style.greeting if style else 'Dear')}",
        *letterhead(style, "<the kind of role she is writing about>", date),
        "",
        "Why now: recent news about the company. Use only what is stated here, as the reason "
        "for writing, and don't add details about the news from your own knowledge:",
    ]
    for s in signals:
        lines.append(f"- {s['summary']} (source: {s['title']})")
    if target_roles:
        lines += [
            "",
            "Kinds of role she'd like (pick what her CV supports): " + ", ".join(target_roles),
        ]
    lines += _instruction_lines(standing, instructions)
    return "\n".join(lines)


def check_prompt(cv_markdown: str, letter_markdown: str) -> str:
    return (
        "Documents to verify.\n\n## Tailored CV\n" + cv_markdown.strip()
        + "\n\n## Cover letter\n" + letter_markdown.strip()
    )  # fmt: skip


def repair_prompt(
    base_prompt: str, previous_cv: str, previous_letter: str, claims: list[str]
) -> str:
    bullets = "\n".join(f"- {c}" for c in claims)
    return (
        base_prompt
        + "\n\nYour previous draft was checked against the CVs, and these statements are NOT "
        "supported by them:\n" + bullets
        + "\n\nWrite the documents again, removing each of those statements or rewording it "
        "to say only what the CVs support. Keep everything else that was supported."
        "\n\nPrevious CV:\n" + previous_cv.strip()
        + "\n\nPrevious cover letter:\n" + previous_letter.strip()
    )  # fmt: skip
