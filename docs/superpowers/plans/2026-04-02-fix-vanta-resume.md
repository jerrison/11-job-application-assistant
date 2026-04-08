# Fix Resume Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shorten resume to 2 pages while maintaining mandatory bullet counts and tagline/summary quality.

**Architecture:** Update `resume_content.json` with shortened bullets and potentially removed older positions.

**Tech Stack:** JSON, Python (for validation check if needed)

---

### Task 1: Shorten Moody's Bullets (Min 6)

**Files:**
- Modify: `output/vanta/staff-pm-scoping-segmentation/content/resume_content.json`

- [ ] **Step 1: Shorten bullets 1-6**
    - Combine phrases, remove fluff.
    - Ensure keywords like "ML-driven", "LLM pipeline", "multi-tenant", "governance" remain.

### Task 2: Shorten Kyte Bullets (Min 5)

**Files:**
- Modify: `output/vanta/staff-pm-scoping-segmentation/content/resume_content.json`

- [ ] **Step 1: Shorten bullets 1-5**
    - Focus on "segmentation", "ML risk engine", "A/B testing".

### Task 3: Shorten T-Mobile Bullets (Min 3)

**Files:**
- Modify: `output/vanta/staff-pm-scoping-segmentation/content/resume_content.json`

- [ ] **Step 1: Shorten bullets 1-3**
    - Focus on "enterprise", "API strategy".

### Task 4: Trim Older Positions and Summary

**Files:**
- Modify: `output/vanta/staff-pm-scoping-segmentation/content/resume_content.json`

- [ ] **Step 1: Shorten Summary**
    - Make it 2-3 tight sentences.
- [ ] **Step 2: Remove Allstate position**
    - It's the oldest and least relevant to "Staff PM".
- [ ] **Step 3: Keep or trim Lyft**
    - Keep if space allows, otherwise remove.

### Task 5: Final Review and Save

**Files:**
- Modify: `output/vanta/staff-pm-scoping-segmentation/content/resume_content.json`

- [ ] **Step 1: Ensure tagline is correct**
    - "Staff Product Manager, Scoping & Segmentation"
- [ ] **Step 2: Save the file**
