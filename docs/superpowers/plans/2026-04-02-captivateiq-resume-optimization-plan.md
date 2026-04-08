# CaptivateIQ Resume Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce resume length to 2 pages for CaptivateIQ Staff PM role while maintaining all required bullet minimums and signal.

**Architecture:** Surgical content reduction of `resume_content.json`.

**Tech Stack:** JSON, manual content optimization.

---

### Task 1: Condense Summary and Bullets

**Files:**
- Modify: `output/captivateiq/staff-pm-builder-experience-incentive-compensation-management/content/resume_content.json`

- [ ] **Step 1: Shorten summary**
  - Reduce to 2 sentences.
- [ ] **Step 2: Shorten Moody's bullets**
  - Condense bullets 2, 3, and 4 into single-line bold sections where possible, or significantly shorten the `text` field.
- [ ] **Step 3: Shorten Kyte bullets**
  - Condense bullets 1 and 2.
- [ ] **Step 4: Shorten T-Mobile bullets**
  - Condense bullet 1.
- [ ] **Step 5: Verify all minimums are still met**
  - Moody's: 6 bullets
  - Kyte: 5 bullets
  - T-Mobile: 3 bullets
  - Summary: Non-null string
  - Tagline: "Staff PM - Builder Experience & Incentive Compensation Management" (already correct)

- [ ] **Step 6: Update `resume_content.json`**

```json
{
    "tagline": "Staff Product Manager | Builder Experience & Incentive Compensation Management | Wharton MBA + Penn M.S. Computer Science",
    "summary": "Staff Product Manager with 8+ years of experience building complex data-modeling and workflow automation platforms. Proven track record of shipping \"Builder Experience\" frameworks and reusable platform components that accelerate implementation speed and translate sophisticated business logic into intuitive, auditable modeling interfaces.",
    "positions": {
        "moodys": [
            {
                "bold": "Drove 166% YoY client growth ($24M ARR expansion) for UnderwriteIQ risk modeling platform. ",
                "text": "Defined 3-year product vision and ML-driven workflow automation roadmap to operationalize complex risk models."
            },
            {
                "bold": "Shipped Workflow Builder framework enabling underwriters to orchestrate data workflows without Admin support. ",
                "text": "Increased productivity 60% and unlocked $12M TCV by balancing modeling power with intuitive UX."
            },
            {
                "bold": "Established unified UX design standards across 5 platform applications. ",
                "text": "Reduced front-end rework 20% and improved task-completion rates 15% through reusable component patterns."
            },
            {
                "bold": "Overhauled developer experience, designing standardized API contracts and sandbox environments. ",
                "text": "Cut integration time 40% and reduced engineering escalations 25% across 5 applications."
            },
            {
                "bold": "Rescued $15M account by building functional prototype in 3 days for direct customer validation. ",
                "text": "Navigated senior leadership resistance through evidence-based prototyping and weekly design sessions."
            },
            {
                "bold": "Accelerated sales velocity 13% by designing package sizing simulation tools, ",
                "text": "enabling Solutions Engineering to data-justify enterprise incentive and capacity deals."
            }
        ],
        "kyte": [
            {
                "bold": "Built first ML-driven risk engine from 0-to-1, reducing losses 23% ($700K annually) and boosting revenue 7% via XGBoost modeling. ",
                "text": "Automated complex business rules to drive loss reduction."
            },
            {
                "bold": "Established structured customer discovery process surfacing 12 high-impact pain points. ",
                "text": "Increased feature adoption 35% by replacing ad-hoc requests with evidence-based prioritization."
            },
            {
                "bold": "Led geographic market expansion strategy, ",
                "text": "defining scalable launch playbooks for 3 new markets and generating $2M+ incremental annual revenue."
            },
            {
                "bold": "Doubled experiment velocity by building an in-house A/B testing platform, ",
                "text": "leading an 8-person cross-functional squad of analysts, designers, and engineers."
            },
            {
                "bold": "Delivered 6% RPU increase by leading strategic partner integrations across pricing and location search. ",
                "text": "Managed technical due diligence and API integration design."
            }
        ],
        "tmobile": [
            {
                "bold": "Enabled IoT co-selling channel ($15M+ pipeline) via a self-serve partner portal with configuration tools. ",
                "text": "Led 8-engineer team through API design to remove manual bottlenecks."
            },
            {
                "bold": "Defined API strategy for enterprise IoT platform, ",
                "text": "designing developer interfaces for device provisioning and monitoring. Grew API adoption 3x."
            },
            {
                "bold": "Launched eUICC (multi-SIM) platform integration, generating $8M+ contracted revenue. ",
                "text": "Led technical requirements and go-to-market for localized cellular connectivity."
            }
        ],
        "lyft": [
            {
                "bold": "Reduced automobile insurance cost 6% company-wide by building predictive accident prevention model. ",
                "text": "Coordinated cross-functional implementation across Data Science, Product, and Legal."
            }
        ],
        "allstate": [
            {
                "bold": "Built internal ratemaking tool reducing pricing variance 8% and saving 60 quarterly person-hours. ",
                "text": "Led GLM-based driver risk modeling; introduced 2 new pricing factors across 14 states."
            }
        ]
    },
    "page_break_before": "lyft"
}
```
