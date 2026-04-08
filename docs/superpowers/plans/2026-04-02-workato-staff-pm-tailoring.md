# Workato Staff PM Developer Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce tailored resume and cover letter for the Staff PM (Developer Tools) role at Workato.

**Architecture:** Phase-based content generation using JD parsing, company research, and candidate context.

**Tech Stack:** JSON for resume structure, TXT for cover letter.

---

### Task 1: Tailor Resume Content

**Files:**
- Create: `output/workato/staff-pm-developer-tools/content/resume_content.json`

- [ ] **Step 1: Define Tagline and Summary**
  - Tagline: `Staff Product Manager | AI/ML & Enterprise Orchestration | Wharton MBA + Penn M.S. Computer Science`
  - Summary: 2-3 sentences. Focus on technical background (MSCS), enterprise platform experience, and AI/ML orchestration. Mention building developer tools and scaling AI agents.

- [ ] **Step 2: Select and Rewrite Bullets**
  - **Moody's (6+ bullets):**
    1. SlipStream (Agentic AI/LLM pipeline) - emphasize "developer tools" aspect of architecting the pipeline.
    2. Workflow Builder - focus on "orchestration" and "developer experience".
    3. Automated test generation - high relevance for "developer tools".
    4. IRP Navigator (RAG) - AI/ML lifecycle relevance.
    5. Platform developer experience/API contracts (from excluded bullets).
    6. Cloud migration/Compatibility layer - platform complexity.
  - **Kyte (5+ bullets):**
    1. ML risk engine 0-to-1 - technical depth.
    2. A/B testing platform - internal developer tool.
    3. Matching algorithm optimization.
    4. Partner integrations (APIs).
    5. Customer discovery for ML roadmap.
  - **T-Mobile (3+ bullets):**
    1. API strategy/developer interfaces - high relevance.
    2. eUICC platform - technical complexity.
    3. Partner onboarding portal - developer platform aspect.

- [ ] **Step 3: Determine Page Break**
  - Place `page_break_before` strategically (likely before Kyte or T-Mobile) to ensure a 2-page fit.

- [ ] **Step 4: Save to `output/workato/staff-pm-developer-tools/content/resume_content.json`**

---

### Task 2: Write Cover Letter

**Files:**
- Create: `output/workato/staff-pm-developer-tools/content/cover_letter_text.txt`

- [ ] **Step 1: Narrative Design**
  - Hook: Interest in Wolf 2 and AI Labs, mentioning Workato's transition to agentic orchestration.
  - Body 1: Technical foundation (MSCS) + AI/ML experience (SlipStream at Moody's).
  - Body 2: Developer platform experience (T-Mobile API strategy, Moody's test automation).
  - Body 3: Strategic fit (Wharton MBA, scaling products from 0-to-1).
  - Close: Enthusiasm for building "enterprise-ready AI agents".

- [ ] **Step 2: Draft Content**
  - 4-5 paragraphs, 300-450 words.
  - **CRITICAL:** Avoid Unicode em dashes (`—`). Use commas, hyphens, or periods.

- [ ] **Step 3: Save to `output/workato/staff-pm-developer-tools/content/cover_letter_text.txt`**

---

### Task 3: Verification

- [ ] **Step 1: Verify Resume Requirements**
  - Check `resume_content.json`:
    - Summary is non-null.
    - Tagline is correct.
    - Moody's >= 6 bullets.
    - Kyte >= 5 bullets.
    - T-Mobile >= 3 bullets.
- [ ] **Step 2: Verify Cover Letter Requirements**
  - Check `cover_letter_text.txt`:
    - Word count is 300-450.
    - No Unicode em dashes.
