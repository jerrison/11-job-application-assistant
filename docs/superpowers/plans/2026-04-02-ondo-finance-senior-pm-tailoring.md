# Ondo Finance Senior Product Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create tailored resume and cover letter for Senior Product Manager role at Ondo Finance.

**Architecture:** Phase 2 (Resume Tailoring) and Phases 2-4 (Cover Letter Writing) as defined in docs/resume-generation.md and docs/cover-letter-generation.md.

**Tech Stack:** JSON (resume), Markdown/TXT (cover letter), Python (uv run).

---

### Task 1: Resume Tailoring (Phase 2)

**Files:**
- Modify: `output/we/senior-pm/content/resume_content.json` (create new)
- Read: `output/we/senior-pm/content/resume_content_draft.json`
- Read: `output/we/senior-pm/content/jd_parsed.json`
- Read: `output/we/senior-pm/content/role_research_cache.json`
- Read: `master_resume.md`

- [ ] **Step 1: Write Summary**
  - Write 2-3 sentences: Technical PM with fintech/blockchain expertise, history of shipping institutional-grade products, and driving ecosystem integrations.
  - Keywords: RWA, tokenized treasuries, institutional-grade infrastructure, cross-functional execution, partner discovery.

- [ ] **Step 2: Update Tagline**
  - Set to: "Senior Product Manager  |  RWA & Institutional Digital Assets  |  Wharton MBA + Penn M.S. Computer Science"

- [ ] **Step 3: Refine Bullets**
  - Rewrite Moody's bullets (6+) to emphasize enterprise account management, cloud migration (relevant to institutional-grade), and automation.
  - Rewrite Kyte bullets (5+) to emphasize ML risk engines (relevant to compliance/security), two-sided marketplaces (relevant to liquidity), and customer discovery.
  - Rewrite T-Mobile bullets (3+) to emphasize IoT platform, API strategy, and OEM co-selling (relevant to integrations/partnerships).
  - Lead with impact, mirror JD language (e.g., "partners", "integrations", "discovery").

- [ ] **Step 4: Save to `output/we/senior-pm/content/resume_content.json`**

### Task 2: Cover Letter Writing (Phases 2-4)

**Files:**
- Create: `output/we/senior-pm/content/cover_letter_text.txt`
- Read: `work_stories.md`
- Read: `candidate_context.md`
- Read: `output/we/senior-pm/content/role_research_cache.json`

- [ ] **Step 1: Design Narrative**
  - Focus on Ondo's leadership in tokenized treasuries (RWA).
  - Connect Moody's (enterprise risk) and Kyte (ML risk) to Ondo's institutional-grade focus.
  - Use T-Mobile story (OEM co-selling) to show integration expertise.

- [ ] **Step 2: Write 4-5 Paragraphs (300-450 words)**
  - Intro: Enthusiasm for Ondo's mission to bridge TradFi and DeFi.
  - Body 1: Moody's experience with $15M enterprise accounts and cloud migration.
  - Body 2: Kyte ML risk engine and breaking growth/profitability tradeoffs.
  - Body 3: T-Mobile's $15M pipeline through OEM portals (integration focus).
  - Conclusion: Alignment with Ondo's scaling phase and technical infrastructure.

- [ ] **Step 3: Review and Refine**
  - Avoid Unicode em dash (—).
  - Ensure word count and tone (professional, technical).

- [ ] **Step 4: Save to `output/we/senior-pm/content/cover_letter_text.txt`**
