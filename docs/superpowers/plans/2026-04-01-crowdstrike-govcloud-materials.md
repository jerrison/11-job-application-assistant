# CrowdStrike Sr. PM GovCloud Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tailor the resume and write a cover letter for the Senior Product Manager, GovCloud role at CrowdStrike, grounding both in company and role research.

**Architecture:** Use a 5-phase approach for both resume and cover letter tailoring, ensuring alignment with CrowdStrike's "machine-speed defense" and "platform consolidation" themes while adhering to specific bullet count and formatting requirements.

**Tech Stack:** Markdown, JSON, Python (for UV-based checks if needed).

---

### Task 1: Tailor Resume Content

**Files:**
- Create: `output/crowdstrike/sr-pm-govcloud-remote/content/resume_content.json`
- Reference: `output/crowdstrike/sr-pm-govcloud-remote/content/resume_content_draft.json`
- Reference: `master_resume.md`
- Reference: `jd_parsed.json`
- Reference: `research_cache.json`

- [ ] **Step 1: Define Resume Tagline**
  - Use: "Senior Product Manager, GovCloud | AI/ML & Enterprise Platform | Wharton MBA + Penn M.S. Computer Science"

- [ ] **Step 2: Write High-Signal Summary**
  - Focus: 2-3 sentences. Bridge between high-growth commercial innovation (Kyte/Moody's) and complex regulated infrastructure (T-Mobile/Moody's). Mention AI-native platform leadership and compliance (FedRAMP/SOC 2 context).

- [ ] **Step 3: Select and Rewrite Bullets for Moody's (6+ bullets)**
  - Priority: Cloud migration ($15M), SlipStream (AI/LLM), Workflow Builder (automation), Unified UX (standards), Chatbot (RAG), and Automated Testing.
  - Rewrite to mirror JD language: "platform", "roadmap", "compliance", "stakeholder management".

- [ ] **Step 4: Select and Rewrite Bullets for Kyte (5+ bullets)**
  - Priority: ML Risk Engine (0-to-1), Expansion strategy, A/B Testing platform, Discovery process, RPU/Loss reduction.

- [ ] **Step 5: Select and Rewrite Bullets for T-Mobile (3+ bullets)**
  - Priority: IoT Platform security/compliance (SOC 2), API strategy, Cloud infrastructure strategy. This is critical for GovCloud context.

- [ ] **Step 6: Final Review and Save**
  - Ensure total bullets and layout fit 2 pages.
  - Ensure `summary` is non-null.
  - Save to `output/crowdstrike/sr-pm-govcloud-remote/content/resume_content.json`.

---

### Task 2: Write Cover Letter

**Files:**
- Create: `output/crowdstrike/sr-pm-govcloud-remote/content/cover_letter_text.txt`
- Reference: `work_stories.md`
- Reference: `research_cache.json`
- Reference: `role_research_cache.json`

- [ ] **Step 1: Design Narrative**
  - Hook: Stopping breaches at machine speed in the public sector.
  - Body: Scaling regulated platforms (T-Mobile) + AI-native innovation (Moody's/Kyte).
  - Alignment: CrowdStrike's mission and GovCloud's strategic importance.

- [ ] **Step 2: Write Letter Body**
  - 4-5 paragraphs, 300-450 words.
  - Avoid Unicode em dashes.
  - Use company-specific terminology: "Falcon platform", "Charlotte AI", "FedRAMP High".

- [ ] **Step 3: Self-Review for Quality**
  - Check word count, em dashes, and research grounding.

- [ ] **Step 4: Save to File**
  - Save to `output/crowdstrike/sr-pm-govcloud-remote/content/cover_letter_text.txt`.
