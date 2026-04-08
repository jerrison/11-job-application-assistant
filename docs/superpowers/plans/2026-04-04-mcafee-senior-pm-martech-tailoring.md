# McAfee Senior PM Martech Tailoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tailor resume and cover letter for the Senior Product Manager, Martech role at McAfee.

**Architecture:** Phase 2 Resume Tailoring and Phase 2-4 Cover Letter Writing based on provided JD, research, and candidate materials.

**Tech Stack:** JSON (resume), Text (cover letter).

---

### Task 1: Resume Tailoring (Phase 2)

**Files:**
- Create: `output/mcafee/senior-pm-martech/content/resume_content.json`

- [ ] **Step 1: Define Tagline and Summary**
  - Tagline: "Senior Product Manager  |  Martech & Consumer Growth  |  Wharton MBA + Penn M.S. Computer Science"
  - Summary: Write a 2-3 sentence summary emphasizing 9+ years of experience in consumer growth, MarTech stacks (Braze, HighTouch, Adobe), and AI-enabled product optimization.

- [ ] **Step 2: Tailor Moody's Bullets (Select 7)**
  - Mirror JD language: "Product Strategy", "Roadmap & Delivery", "Data-driven", "AI-enabled", "Conversion", "Stakeholder Collaboration".
  - Include: Client growth (166% YoY), SlipStream AI (agentic AI), Workflow Builder (discovery to launch), IRP Navigator (AI chatbot), Package sizing (conversion/sales velocity), Unified UX standards, and Cloud migration ($15M account).

- [ ] **Step 3: Tailor Kyte Bullets (Select 6)**
  - Focus on: ML risk engine (0-to-1), A/B testing platform (experiment velocity), market expansion (revenue growth), matching algorithm (delivery optimization), partner integrations (RPU increase), and structured discovery.

- [ ] **Step 4: Tailor T-Mobile Bullets (Select 3)**
  - Focus on: OEM co-selling portal ($15M pipeline), IoT connectivity solutions (20% conversion), and cloud infrastructure strategy.

- [ ] **Step 5: Finalize and Save**
  - Ensure Moody's (>=6), Kyte (>=5), T-Mobile (>=3) bullets.
  - Set `page_break_before` to null (let pipeline decide later).
  - Save to `output/mcafee/senior-pm-martech/content/resume_content.json`.

### Task 2: Cover Letter Writing (Phases 2-4)

**Files:**
- Create: `output/mcafee/senior-pm-martech/content/cover_letter_text.txt`

- [ ] **Step 1: Write Cover Letter Body**
  - Paragraph 1: Enthusiastic intro referencing McAfee's mission and AI Scam Detector innovation.
  - Paragraph 2: Deep dive into Kyte experience (ML risk engine, A/B testing) to show fit for AI/data optimization.
  - Paragraph 3: Highlight Moody's/T-Mobile work on growth platforms and scalable infrastructure (SlipStream, OEM portal).
  - Paragraph 4: Connect specifically to Martech tools (Braze, HighTouch) and cross-functional leadership.
  - Paragraph 5: Closing and call to action.
  - Constraint: Avoid em dashes (`—`), use 300-450 words.

- [ ] **Step 2: Save and Verify**
  - Save to `output/mcafee/senior-pm-martech/content/cover_letter_text.txt`.
  - Verify word count and lack of em dashes.
