# PART 2: COVER LETTER

---

## Purpose

Generate a cover letter so targeted and well-researched that the hiring manager feels compelled to interview the candidate. This is not a template fill. This is a strategic persuasion document built on deep research and precise narrative alignment.

---

## Cover Letter Workflow

### CL Phase 1: Deep Company Research

**This phase is non-negotiable. Do not skip it. Do not abbreviate it.**

**Caching:** Two-tier research cache:
- **Company cache** — `output/<company>/research_cache.json` — shared across roles, configurable TTL (default 30 days, override with `JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS`). If fresh, skip company research.
- **Role cache** — `{content_dir}/role_research_cache.json` — per-JD (keyed by SHA-256 hash of `jd_parsed.json`), same TTL. If fresh, skip role-specific research.

After completing company research, save to `output/<company>/research_cache.json`. After completing role-specific research, save to `{content_dir}/role_research_cache.json` with `jd_hash` and `researched_at` fields. Set `JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS=0` to force re-research.

Use web search and `scripts/scrape_job.py` to conduct AT MINIMUM the following research. Run multiple searches in sequence. When you find a promising page (blog post, interview, about page), scrape it with the script to extract full content. Collect specific names, quotes, dates, and details — vague references are worthless.

#### Research Roadmap

| Topic | Search Patterns | Key Questions |
|-------|----------------|---------------|
| **Company Foundation** | `"[company]" mission vision values`, `"[company]" about us culture` | Mission? Values? Founding story? Culture language? |
| **Leadership Voice** | `"[company]" CEO interview podcast`, `"[company]" CTO/VP Engineering talk`, `"[company]" founder keynote` | Public direction statements? Repeated frameworks? What they look for in people? |
| **Technical & Product Culture** | `"[company]" engineering blog`, `site:[domain] blog engineering product` | Technical challenges? Engineering culture values? Team/product area blog posts? |
| **Recent Momentum** | `"[company]" news launch announcement` (6mo), `"[company]" funding growth` (12mo) | Current momentum? Recent launches/pivots/funding? Strategic narrative? |
| **Role Context** | `"[company]" [role/team keywords] blog article` | Public content about this team? Roadmaps? Strategic priorities? |

When you find a promising page, scrape it with `uv run python scripts/scrape_job.py` to extract full content. Collect specific names, quotes, dates — vague references are worthless.

#### Research Documentation
After research, create a brief internal summary (do NOT show this to the user) capturing:
- 3-5 most compelling company facts/quotes to potentially weave into the letter
- Key cultural/value signals that map to the candidate's experience
- Specific leadership language or phrasing worth echoing (subtly, not parroting)
- Recent momentum or news that creates a "why now" narrative hook

---

### CL Phase 2: Strategic Narrative Design

Before writing a single word, design the persuasion architecture.

#### 2.1 Identify the Core Match
What is the single most compelling reason this candidate should get this specific role? Not generic qualifications — the *specific intersection* of what they bring and what this company needs right now.

#### 2.2 Map the Evidence
For each key requirement in the JD, identify the candidate's strongest proof point from `master_resume.md`. Prioritize:
- **Quantified outcomes** (revenue, efficiency, user metrics, team scale)
- **Directly transferable context** (same industry, similar scale, analogous problems)
- **Narrative arcs** (grew something from X to Y, turned around a situation, built from scratch)

#### 2.3 Design the Emotional Hook
The opening must make the reader want to keep reading. Options:
- **Personal connection**: genuine enthusiasm for the product/mission, backed by specific evidence
- **Provocative insight**: a perspective on the company's space that shows the candidate thinks deeply
- **Shared challenge**: naming the exact problem the role solves, showing the candidate already understands it
- **Momentum alignment**: connecting the candidate's trajectory to the company's current moment

#### 2.4 Plan the "Why Now + Why Them" Thread
The letter must answer two implicit questions the reader has:
1. **Why this company and not somewhere else?** — Must be specific enough that it could NOT be copy-pasted to a competitor.
2. **Why is this candidate ready for this NOW?** — Connect career arc to this being the logical next step.

---

### CL Phase 3: Write the Cover Letter

#### Structural Framework

The letter should be **4-5 paragraphs**, roughly **300-450 words**. Tight. Every sentence earns its place.

**Paragraph 1 — The Hook (2-4 sentences)**
Open with energy. No "I am writing to apply for..." openers. Lead with WHY — why this company, why this role, why the candidate is genuinely excited. Weave in a specific company research finding (a leadership quote, a recent launch, a blog post insight) to prove this is not a form letter. Name the exact role.

**Paragraph 2 — The Strongest Proof (3-5 sentences)**
Lead with the candidate's single most impressive and relevant accomplishment. This should directly address the #1 thing the JD is asking for. Use specific numbers. Show impact at scale. Connect it explicitly to what the role requires.

**Paragraph 3 — Breadth & Depth (3-5 sentences)**
Cover 2-3 additional qualifications from the JD, each with a concrete (but brief) proof point. Show range. If the role requires cross-functional work, show cross-functional wins. If it requires technical depth, show technical depth. Weave in another company-specific reference to show ongoing alignment.

**Paragraph 4 — Why This Company Specifically (2-4 sentences)**
This is where the research pays off. Reference something specific: a value the candidate shares (with evidence), a company challenge the candidate is uniquely positioned to help with, a leadership statement that resonates. This paragraph should make it impossible to imagine this letter being sent anywhere else.

**Paragraph 5 — The Close (2-3 sentences)**
Confident, forward-looking, warm. Express enthusiasm about contributing to [specific thing]. Invite conversation. No groveling, no "I hope to hear from you" passivity. Project confidence and peer-to-peer energy.

#### Writing Rules — MANDATORY

1. **No em dashes** unless strictly necessary. Use commas, semicolons, colons, or restructure the sentence.
2. **No cliches**: "passionate about", "excited to leverage", "proven track record", "hit the ground running", "unique opportunity", "I believe I would be a great fit". Find fresher language.
3. **No hollow adjectives**: "dynamic", "innovative", "cutting-edge", "world-class". Replace with specifics.
4. **Active voice throughout**. "I led" not "I was responsible for leading".
5. **Concrete over abstract**: Never claim a quality without evidence. "I'm a strong communicator" → "I presented quarterly strategy to 200+ stakeholders and secured $3M in incremental budget."
6. **Company-specific over generic**: Every claim of enthusiasm must be backed by a specific reference to the company.
7. **Candidate's authentic voice**: Match the tone to the candidate's background and the company's culture. A fintech startup letter reads differently than one to a government agency.
8. **No more than 1 exclamation point** in the entire letter. Prefer zero.
9. **Vary sentence length**: Mix short punchy sentences with longer explanatory ones. Avoid monotonous rhythm.
10. **First and last sentences are the most important**. Spend extra effort on these.

#### Formatting Rules

- **Salutation**: "Dear [Hiring Manager Name]," if known. Otherwise "Dear Hiring Team," (never "To Whom It May Concern" or "Dear Sir/Madam").
- **Sign-off**: "Best regards," or "Sincerely," followed by the candidate's full name.
- **No date or address block** unless the candidate specifically requests formal business letter format.
- **Single-spaced** with a blank line between paragraphs.

---

### CL Phase 4: Self-Review

Before generating files, verify: compelling opening hook | >=1 specific research finding woven in | every claim has proof/metric | letter only works for THIS company | 300-450 words | no em dashes | no banned cliches | confident close | clear narrative arc (past → role → company future) | sounds like a real person.

---

### CL Phase 5: Build and Deliver

**Do NOT build the .docx yourself.** Write the cover letter text to a file, then use the deterministic builder:

1. **Save the cover letter text** to `output/<company>/<role-slug>/content/cover_letter_text.txt` (just the letter body, starting with "Dear ...")
2. **Run the builder:**
   ```bash
   uv run scripts/build_cover_letter.py output/<company>/<role-slug>/content/cover_letter_text.txt -o "output/<company>/<role-slug>/documents/Jerrison Li Cover Letter - <Company>.docx"
   ```
   Example: `uv run scripts/build_cover_letter.py output/samsara/agent-platform-pm/content/cover_letter_text.txt -o "output/samsara/agent-platform-pm/documents/Jerrison Li Cover Letter - Samsara.docx"`

   The builder produces `.docx`, `.txt`, and `.pdf` files with consistent formatting (Calibri 11pt, US Letter, 1in margins, candidate name and contact header).

3. **Deliver** the `.docx`, `.pdf`, and `.txt` files.

4. **Display** the full cover letter text in chat so the candidate can copy-paste it into application text boxes.

---

## Cover Letter Critical Reminders

1. **Research depth is the differentiator.** A cover letter without company-specific research is a template. Templates don't get interviews. Spend the time on CL Phase 1.
2. **Specificity wins.** Every vague sentence is a missed opportunity. "I grew the team" → "I grew the team from 3 to 12 engineers across two time zones while reducing sprint cycle time by 30%."
3. **The candidate's voice matters.** Let the personality from the master resume come through. A cover letter that sounds like it was written by AI will be detected and discarded.
4. **Shorter is almost always better.** Hiring managers are busy. Respect their time. If you can say it in 350 words, don't use 450.
5. **The "why this company" must be unimpeachably specific.** This is the #1 signal that separates a serious candidate from a mass-applier.
6. **All proof points must come from `master_resume.md`.** Never fabricate accomplishments, metrics, or responsibilities.
