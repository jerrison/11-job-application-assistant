# HPE Sr. PM Resume Validation Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shorten the resume at `output/hpe/sr-pm/content/resume_content.json` to pass the 2-page validation limit while maintaining mandatory bullet counts and tagline tailoring.

**Architecture:** Surgical JSON edits to shorten verbose bullets and remove low-value ones, ensuring `Moody's` has 7 bullets (min 6), `Kyte` has 5 bullets (min 5), and `T-Mobile` has 3 bullets (min 3).

**Tech Stack:** JSON, Python (validation)

---

### Task 1: Shorten Moody's Bullets

**Files:**
- Modify: `output/hpe/sr-pm/content/resume_content.json`

- [ ] **Step 1: Shorten Moody's bullets**

```json
{
    "bold": "Overhauled platform developer experience by designing standardized API contracts, ",
    "text": "cutting customer integration time 40% and reducing support escalations 25% across 5 applications."
},
{
    "bold": "Established unified UX design standards and interaction patterns across 5 applications, ",
    "text": "reducing dev rework 20% and improving enterprise task-completion rates 15%."
},
{
    "bold": "Built automated test generation system covering 4,000+ scenarios for high-stakes platform releases; ",
    "text": "methodology adopted as the standard for complex platform reliability and scale."
}
```

### Task 2: Shorten Kyte Bullets

**Files:**
- Modify: `output/hpe/sr-pm/content/resume_content.json`

- [ ] **Step 1: Shorten Kyte bullets**

```json
{
    "bold": "Built company's first ML risk engine from 0-to-1, ",
    "text": "reducing losses 23% ($700K annually) and boosting revenue 7% through XGBoost model deployment."
},
{
    "bold": "Established customer discovery processes that surfaced 12 high-impact pain points, ",
    "text": "increasing feature adoption 35% through evidence-based roadmap prioritization."
}
```

### Task 3: Shorten T-Mobile and Summary

**Files:**
- Modify: `output/hpe/sr-pm/content/resume_content.json`

- [ ] **Step 1: Shorten T-Mobile bullets**

```json
{
    "bold": "Enabled IoT OEM co-selling channel ($15M+ pipeline) via a partner onboarding portal, ",
    "text": "removing manual bottlenecks to scale distribution through third-party manufacturers."
}
```

- [ ] **Step 2: Tighten Summary**

```json
"summary": "Technical Product Leader (Wharton MBA / MS CS) specializing in scaling enterprise hybrid cloud and AI/ML platforms. Expert at productizing complex infrastructure into repeatable GTM solutions, optimizing strategic pricing, and driving double-digit ARR growth by bridging engineering and commercial operations."
```

### Task 4: Verify Tagline

**Files:**
- Modify: `output/hpe/sr-pm/content/resume_content.json`

- [ ] **Step 1: Ensure tagline starts with "Sr. Product Manager"**

The current tagline is `"Sr. Product Manager | AI/ML & Hybrid Cloud Enterprise B2B | Wharton MBA + Penn M.S. Computer Science"`.
