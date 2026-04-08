from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ORIGINAL_RUNTIME_INPUTS: dict[Path, str | None] = {}

MASTER_RESUME_TEXT = """# Master Resume

CANDIDATE NAME
San Francisco, CA  |  candidate@example.com  |  555-0100  |  linkedin.com/in/candidate/  |  candidate.example.com
Work Authorization: United States Citizen

SUMMARY
Product manager focused on AI workflows, analytics, platform tooling, and high-stakes operational systems.

EXPERIENCE
MOODY'S ANALYTICS — Product Manager
New York, NY | January 2024 - Present
- Led platform roadmap and cross-functional execution for risk workflows, including SlipStream and IRP Navigator.
- Built a prototype for a $15M at-risk enterprise account to resolve a product-engineering stalemate with concrete customer feedback.
- Helped bring SlipStream to market, an agentic AI workflow that turns unstructured policy documents into structured risk data, reducing processing time from 60 minutes to 5 and unlocking automation across $200B+ in policy premiums.
- Improved IRP Navigator AI chatbot answer quality with RAG architecture changes, reducing support ticket volume 31%.

KYTE — Senior Product Manager
San Francisco, CA | January 2022 - December 2023
- Built customer and operator workflows across growth and operations, including automated chat workflows and a first ML risk engine.
- Used Python, SQL, an in-house A/B testing platform, analytics dashboards, session recordings, support ticket analysis, and user interviews to prioritize product decisions.

T-MOBILE — Product Manager
Seattle, WA | January 2020 - December 2021
- Improved acquisition, activation, and conversion journeys across digital channels, including a partner onboarding portal and business support systems.

LYFT — Product Manager
San Francisco, CA | January 2018 - December 2019
- Built driver risk models and helped establish the company's risk management function across cyber, tech E&O, and business risks.
- Delivered marketplace, onboarding, and growth improvements with analytics, experimentation, and fraud-risk compliance collaboration.

ALLSTATE — Product Manager
Chicago, IL | January 2016 - December 2017
- Built workflow automation and analytics capabilities for service operations.

EDUCATION
The Wharton School, University of Pennsylvania
Master of Business Administration (MBA), Finance | 2018 - 2020
Penn Engineering, University of Pennsylvania
Master of Science, Computer Science | 2018 - 2020
Florida State University
Bachelor of Science, Actuarial Science | 2009 - 2013

SKILLS & ADDITIONAL
Technical: Python, SQL, TypeScript, Figma | Product Analytics: in-house A/B testing platform, analytics dashboards, session recordings, support ticket analysis, user interviews
ML/AI: Claude Code, Codex, LLM orchestration, RAG systems, prompt evaluation
Languages: Spanish (native), English (fluent), Cantonese (native), Mandarin (advanced)
Certifications: Associate of the Casualty Actuarial Society (ACAS)
"""

APPLICATION_PROFILE_TEXT = """# Application Profile

Country: United States
Location: San Francisco, CA
Work Authorization Statement: Authorized to work in the United States without sponsorship.
Authorized to Work Unconditionally: Yes
Require Sponsorship Now: No
Require Sponsorship in Future: No
Minimum Years of Experience: Yes
Sponsorship Answer: No
Live in Job Location: Yes
Willing to Relocate: Yes
Comfortable Working On Site: Yes
Comfortable With Posted Salary: Yes
Text Message Consent: No
Available Cities: San Francisco, CA, New York, NY, Los Angeles, CA
Street Address: 123 Market Street
Zip Code: 94105
Age Range: 35 - 44
Gender: Male
Gender Identity: Male
Transgender Status: No
Race or Ethnicity: Hispanic or Latino
Veteran Status: I am not a protected veteran
Disability Status: No, I do not have a disability and have not had one in the past
Sexual Orientation: Straight / Heterosexual
Pronouns: He / Him / His
Verification Code Email: candidate@example.com
How Did You Hear About Us: Corporate website
LinkedIn: https://linkedin.com/in/candidate/
GitHub: https://github.com/candidate
Website: https://candidate.example.com
Languages Spoken: Spanish, English, Cantonese, Mandarin
Communities: Product, AI, Developer Tools
Compensation Expectations: I'm open and flexible on compensation. I'd prefer to learn more about the role's scope and total rewards package before discussing specific numbers. If the field requires a numeric-only amount, enter 1000.
Undergraduate GPA: 3.8/4.0
Citizen of Cuba, Iran, North Korea, or Syria: No
Maximum Travel Percentage: 50%
Notice Period: 2 weeks
Earliest Start Timing: 2 weeks from the application time
Interview Recording Consent: Yes
Default Years Of Experience: 10
PM People Management Years: 3

## Education
- The Wharton School, University of Pennsylvania, Master of Business Administration (MBA), Finance | graduation: 05/2020
- Penn Engineering, University of Pennsylvania, Master of Science in Computer Science | graduation: 05/2020
- Florida State University, Bachelor of Science in Actuarial Science | graduation: 05/2013
"""

WORK_STORIES_TEXT = """# Work Stories

- At Moody's Analytics, I used Figma and Claude Code to prototype a workflow solution for a $15M at-risk enterprise account, then turned that into a functional prototype in 3 days so the team could resolve a product-engineering stalemate with evidence instead of opinion.
- Launched SlipStream, a multi-agent LLM system that converts unstructured policy documents into structured underwriting data, cutting processing time from 60 minutes to 5 and unlocking automation across $200B+ in policy premiums.
- Improved IRP Navigator with RAG optimizations and high-volume customer prompt testing, reducing support ticket volume 31%.
- At Kyte and Lyft, I combined Python, SQL, an in-house A/B testing platform, analytics dashboards, session recordings, support ticket analysis, and user interviews to ship product improvements.
- Helped bring a recent cybersecurity workflow product to market by turning repeated customer pain and sales notes into an agentic AI workflow for underwriting teams.
"""

CANDIDATE_CONTEXT_TEXT = """# Candidate Context

- I was born and raised in Panama and have lived across different cultures.
- Candidate speaks and writes Spanish, English, Cantonese, and Mandarin.
- Motivated by product, platform, AI, and workflow automation problems.
- Uses Claude Code and Codex to prototype UI designs and workflow concepts quickly.
- Values clear writing, strong execution, and practical customer impact.
- Lives in 123 Market Street, San Francisco, CA 94105.

Writing Samples
- https://candidate.example.com/writing/product-strategy
- https://candidate.example.com/writing/ai-workflows
"""

RUNTIME_FIXTURES = {
    "master_resume.md": MASTER_RESUME_TEXT,
    "application_profile.md": APPLICATION_PROFILE_TEXT,
    "work_stories.md": WORK_STORIES_TEXT,
    "candidate_context.md": CANDIDATE_CONTEXT_TEXT,
}


def _ensure_runtime_inputs() -> None:
    for name, content in RUNTIME_FIXTURES.items():
        path = PROJECT_ROOT / name
        if path not in _ORIGINAL_RUNTIME_INPUTS:
            _ORIGINAL_RUNTIME_INPUTS[path] = path.read_text(encoding="utf-8") if path.exists() else None
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")


def _cleanup_runtime_inputs() -> None:
    for path, original_text in _ORIGINAL_RUNTIME_INPUTS.items():
        if original_text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(original_text, encoding="utf-8")
    _ORIGINAL_RUNTIME_INPUTS.clear()


def pytest_sessionstart(session) -> None:  # noqa: ARG001
    _ensure_runtime_inputs()


def pytest_runtest_setup(item) -> None:  # noqa: ARG001
    _ensure_runtime_inputs()


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    _cleanup_runtime_inputs()
