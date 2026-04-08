# Fix Resume Content Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct `resume_content.json` to pass validation by reducing length to 2 pages, ensuring the tagline matches the JD, and maintaining minimum bullet counts.

**Architecture:** Surgical edit of JSON content to prune low-value and verbose bullets.

**Tech Stack:** JSON, Python (for validation if needed)

---

### Task 1: Research and Pruning Strategy

**Files:**
- Modify: `output/filevine/senior-technical-pm/content/resume_content.json`

- [ ] **Step 1: Shorten Summary**
Reduce summary from 3 sentences to 2 tight sentences.

- [ ] **Step 2: Prune Moody's (7 -> 6 bullets)**
Remove the bullet about "Rescued $15M at-risk enterprise account by building functional prototype in 3 days..." as it's less "technical platform" focused than the others. Keep the rest (6).

- [ ] **Step 3: Prune Kyte (5 -> 5 bullets - no change)**
Already at the minimum of 5. Will shorten verbose text instead.

- [ ] **Step 4: Prune T-Mobile (4 -> 3 bullets)**
Remove "Enabled IoT OEM co-selling channel..." as it's less "technical infrastructure" focused. Keep 3 bullets (minimum).

- [ ] **Step 5: Prune/Shorten Lyft & Allstate**
Keep 1 bullet each for Lyft and Allstate but tighten them.

- [ ] **Step 6: Update Tagline**
Ensure it starts with "Senior Technical Product Manager".

### Task 2: Apply Changes

**Files:**
- Modify: `output/filevine/senior-technical-pm/content/resume_content.json`

- [ ] **Step 1: Write the updated JSON**

```json
{
    "tagline": "Senior Technical Product Manager  |  Platform Reliability & Infrastructure  |  Wharton MBA + Penn M.S. Computer Science",
    "summary": "Technical Product Manager with 10+ years of experience scaling reliable cloud infrastructure and AI-driven platforms for enterprise SaaS. Expert at bridging infrastructure engineering and business value to deliver high-availability systems and complex cloud migrations.",
    "positions": {
        "moodys": [
            {
                "bold": "Owned platform reliability and SLA commitments for UnderwriteIQ cloud platform serving 20 enterprise carriers, ",
                "text": "defining uptime targets (99.9%), incident classification, and escalation protocols. Reduced P1 resolution time and improved stability through proactive monitoring."
            },
            {
                "bold": "Led $15M enterprise cloud migration, ",
                "text": "designing cross-generation model compatibility layers and phased migration plans. Managed API contract negotiations and cutover sequencing, establishing a framework adopted across 5 subsequent accounts."
            },
            {
                "bold": "Built automated test generation system covering 4,000+ model-region-peril scenarios for high-stakes releases ($20M revenue dependent). ",
                "text": "Caught silent failures missed by manual testing; methodology adopted as standard practice for complex platform releases."
            },
            {
                "bold": "Launched SlipStream agentic AI system for unstructured policy data. ",
                "text": "Architected multi-agent LLM pipeline, balancing latency vs. cost and achieving 12x processing speedup (60 to 5 min), unlocking automation for $200B+ in premiums."
            },
            {
                "bold": "Overhauled platform developer experience, ",
                "text": "designing standardized API contracts, integration playbooks, and sandbox environments. Cut integration time 40% and reduced engineering support escalations 25% across 5 applications."
            },
            {
                "bold": "Managed multi-tenant architecture for cloud risk modeling platform, ",
                "text": "defining isolation boundaries, data residency, and per-tenant configuration controls. Enabled secure adoption for enterprise customers with strict data sovereignty requirements."
            }
        ],
        "kyte": [
            {
                "bold": "Built company's first ML risk engine from 0-to-1, identifying $3M+ loss exposure. ",
                "text": "Partnered with Data Science on an XGBoost model, reducing losses 23% and boosting revenue 7% through improved risk separation."
            },
            {
                "bold": "Doubled experiment velocity by building in-house A/B testing platform, ",
                "text": "leading an 8-person cross-functional squad. Standardized metrics and reporting to accelerate the release lifecycle."
            },
            {
                "bold": "Delivered 6% RPU increase and 7% loss reduction by leading strategic partner integrations across pricing, ",
                "text": "driver underwriting, and search ranking. Performed technical due diligence and API integration design."
            },
            {
                "bold": "Reduced support ticket volume 30% and response time 50% by implementing automated chat workflows. ",
                "text": "Defined escalation logic and integrated NLP-based intent classification."
            },
            {
                "bold": "Led product strategy for geographic market expansion, ",
                "text": "defining launch playbook and success metrics. Expanded into 3 new markets, generating $2M+ incremental annual revenue within two quarters."
            }
        ],
        "tmobile": [
            {
                "bold": "Owned cloud infrastructure strategy for IoT connectivity platform, ",
                "text": "managing compute, storage, and networking backing real-time monitoring. Partnered with Engineering on capacity planning, cost optimization, and scaling for a 10K+ device fleet."
            },
            {
                "bold": "Defined API strategy for enterprise IoT platform, ",
                "text": "designing developer-facing interfaces for device provisioning and SIM management. Grew API adoption 3x among enterprise customers and OEM partners."
            },
            {
                "bold": "Drove security and compliance for IoT platform, ",
                "text": "including device identity management, cellular protocols, and data isolation standards. Achieved SOC 2 compliance."
            }
        ],
        "lyft": [
            {
                "bold": "Reduced automobile insurance cost 6% by building predictive accident prevention model. ",
                "text": "Coordinated implementation across Data Science and Legal to ensure actuarial soundness and regulatory compliance."
            }
        ],
        "allstate": [
            {
                "bold": "Built internal ratemaking tool reducing pricing variance 8% and saving 60 quarterly person-hours. ",
                "text": "Led GLM-based driver risk modeling; introduced 2 new pricing factors across 14 states."
            }
        ]
    },
    "page_break_before": "kyte"
}
```

### Task 3: Verification

- [ ] **Step 1: Verify tagline**
Check that it starts with "Senior Technical Product Manager".

- [ ] **Step 2: Verify bullet counts**
- Moody's: 6 (>=6)
- Kyte: 5 (>=5)
- T-Mobile: 3 (>=3)

- [ ] **Step 3: Check summary**
Ensure it's a non-null string.

- [ ] **Step 4: Check file format**
Ensure valid JSON.
