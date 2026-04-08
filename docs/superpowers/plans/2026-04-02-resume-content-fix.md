# Resume Content Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shorten the resume to pass validation (under 3 pages) while matching the JD title and keeping required bullet counts.

**Architecture:** Surgical JSON update to `resume_content.json`.

**Tech Stack:** JSON.

---

### Task 1: Update Resume Content

**Files:**
- Modify: `output/vanta/senior-pm-access-management/content/resume_content.json`

- [ ] **Step 1: Apply shortened content and JD title match**

```json
{
  "tagline": "Senior Product Manager, Access Management | Enterprise Workflow Automation | Wharton MBA + Penn M.S. Computer Science",
  "summary": "Senior product leader building enterprise workflow automation, AI-powered products, and integration-heavy B2B platforms. Owned roadmap and delivery for workflow builders, self-serve onboarding, and AI systems, driving $24M ARR expansion and 12x faster document processing workflows.",
  "positions": {
    "moodys": [
      {
        "bold": "Owned roadmap for UnderwriteIQ, driving 166% YoY client growth ",
        "text": "and $24M ARR expansion across 20 enterprise insurance carriers."
      },
      {
        "bold": "Shipped Workflow Builder automation for underwriting, ",
        "text": "enabling self-serve orchestration, 60% higher productivity, and $12M in enterprise deals."
      },
      {
        "bold": "Rescued $15M at-risk account by building a prototype in 3 days, ",
        "text": "using direct validation to define a scalable workflow solution shipped within 3 months."
      },
      {
        "bold": "Led $15M enterprise cloud migration, ",
        "text": "designing compatibility layers and API changes to retain full account spend and create a new onboarding framework."
      },
      {
        "bold": "Overhauled developer experience across 5 platform applications, ",
        "text": "standardizing API contracts and playbooks to reduce integration time 40%."
      },
      {
        "bold": "Launched SlipStream agentic AI workflow, ",
        "text": "cutting document processing time 12x (60 mins to 5) across $200B+ in policy premiums."
      }
    ],
    "kyte": [
      {
        "bold": "Built 0-to-1 ML risk engine, ",
        "text": "reducing losses 23% ($700K annually) and identifying $3M+ in loss exposure."
      },
      {
        "bold": "Established structured customer discovery, ",
        "text": "surfacing 12 key pain points and increasing feature adoption 35% via evidence-based decisions."
      },
      {
        "bold": "Doubled experiment velocity via in-house A/B platform, ",
        "text": "leading an 8-person squad to improve iterative delivery."
      },
      {
        "bold": "Led partner integrations for pricing and underwriting, ",
        "text": "designing APIs to deliver 6% higher revenue per user and 7% lower losses."
      },
      {
        "bold": "Increased completed deliveries 11% ",
        "text": "by optimizing real-time matching algorithms in partnership with Engineering."
      }
    ],
    "tmobile": [
      {
        "bold": "Defined API strategy for IoT platform, ",
        "text": "growing adoption 3x and enabling self-serve onboarding for enterprise OEM partners."
      },
      {
        "bold": "Built partner onboarding portal unlocking $15M+ pipeline, ",
        "text": "leading API/system design to reduce manual onboarding friction."
      },
      {
        "bold": "Drove security requirements and SOC 2 compliance, ",
        "text": "covering device identity and authentication to unblock 4 enterprise deals."
      }
    ],
    "lyft": [
      {
        "bold": "Reduced insurance costs 6% via predictive risk models, ",
        "text": "partnering across Data Science and Legal to implement a measurable decisioning system."
      }
    ],
    "allstate": [
      {
        "bold": "Built internal pricing tools saving 60 person-hours per quarter, ",
        "text": "using GLM-based modeling to improve decision quality across 14 states."
      }
    ]
  },
  "page_break_before": "tmobile"
}
```

- [ ] **Step 2: Commit changes**

```bash
git add output/vanta/senior-pm-access-management/content/resume_content.json
git commit -m "fix(resume): shorten bullets and match tagline to JD title to pass validation"
```
