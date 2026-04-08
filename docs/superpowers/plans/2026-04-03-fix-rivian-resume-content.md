# Fix Resume Content for Rivian Sr. Staff PM Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix resume validation errors (3 pages) and update tagline/summary for the Rivian Sr. Staff Product Manager, Enterprise Data role.

**Architecture:** Surgical edit of `resume_content.json` to shorten bullets, update tagline, and adjust summary while adhering to mandatory constraints.

**Tech Stack:** JSON, Python (for validation check)

---

### Task 1: Update Tagline and Summary

**Files:**
- Modify: `output/stripe/staff-pm-orchestration-lead/content/resume_content.json`

- [ ] **Step 1: Update tagline first segment to match JD**
Change `"Staff Product Manager, Orchestration Lead ..."` to `"Sr. Staff Product Manager, Enterprise Data ..."`.

- [ ] **Step 2: Refine summary for Rivian/Finance/Data focus**
Ensure summary highlights data strategy, analytics enablement, and finance transformation as per JD.

### Task 2: Shorten Bullets to Reduce Page Count

**Files:**
- Modify: `output/stripe/staff-pm-orchestration-lead/content/resume_content.json`

- [ ] **Step 1: Shorten Moody's bullets**
Shorten the 6 bullets to be more concise. Ensure at least 6 bullets remain. Focus on data/analytics impact.

- [ ] **Step 2: Shorten Kyte bullets**
Shorten the 5 bullets. Ensure at least 5 bullets remain. Highlight ML/Data platform aspects.

- [ ] **Step 3: Shorten T-Mobile bullets**
Shorten the 3 bullets. Ensure at least 3 bullets remain.

- [ ] **Step 4: Cut or shorten lower-valued roles (Lyft/Allstate)**
Since these are older, they are prime candidates for aggressive shortening to save space.

### Task 3: Final Review and Validation

**Files:**
- Read: `output/stripe/staff-pm-orchestration-lead/content/resume_content.json`

- [ ] **Step 1: Verify all constraints**
  - [ ] Tagline matches "Sr. Staff Product Manager, Enterprise Data"
  - [ ] Summary is non-null string (2-3 sentences)
  - [ ] Moody's has >= 6 bullets
  - [ ] Kyte has >= 5 bullets
  - [ ] T-Mobile has >= 3 bullets
  - [ ] Content is significantly shorter to hit 2-page target (pipeline will recompute page breaks)
