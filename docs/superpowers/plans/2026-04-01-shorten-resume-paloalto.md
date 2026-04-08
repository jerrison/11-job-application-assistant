# Resume Content Shortening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shorten the resume content to fit on 2 pages while adhering to minimum bullet counts and tagline requirements.

**Architecture:** Update the JSON content for the Palo Alto Networks resume to be more concise.

**Tech Stack:** JSON

---

### Task 1: Update Tagline and Shorten Moody's Bullets

**Files:**
- Modify: `output/paloaltonetworks/career-site/content/resume_content.json`

- [ ] **Step 1: Update tagline and shorten Moody's bullets**

Update the tagline to match the JD exactly and shorten the text part of each Moody's bullet to save vertical space.

```json
{
    "tagline": "Senior Director, Product Management (Unit 42)  |  AI Security & Enterprise Platforms  |  Wharton MBA + Penn M.S. Computer Science",
    "summary": "Strategic product leader with a Wharton MBA and Penn M.S. in Computer Science, specializing in AI-driven enterprise platforms and high-stakes risk modeling. Proven track record of launching agentic AI systems like SlipStream and scaling autonomous workflow frameworks to drive platformization and enterprise growth. Expert at bridging human-led expertise with scalable software operations to deliver mission-critical security and infrastructure solutions.",
    "positions": {
        "moodys": [
            {
                "bold": "Launched SlipStream agentic AI system transforming unstructured policy documents into structured data, ",
                "text": "achieving a 12x processing speedup (60 to 5 minutes) via a multi-agent LLM pipeline. Optimized latency and cost through parallelized processing, unlocking automation for $200B+ in policy premiums."
            },
            {
                "bold": "Shipped Workflow Builder automation framework enabling underwriters to orchestrate end-to-end modeling workflows without Admin support. ",
                "text": "Increased underwriter productivity 60% and unlocked 4 new enterprise deals ($12M TCV) by reducing operational friction and standardizing complex decision-making."
            },
            {
                "bold": "Led $15M enterprise account cloud migration, ",
                "text": "designing a cross-generation catastrophe model compatibility layer and phased migration plan. Managed API contracts and data validation, establishing a reusable framework adopted across 5 subsequent accounts."
            },
            {
                "bold": "Rescued $15M at-risk enterprise account by building a functional prototype in 3 days, ",
                "text": "deploying for direct validation and weekly design sessions to lock requirements. Negotiated engineering scope to ship within a 3-month contract window, overcoming leadership resistance via evidence-based prototyping."
            },
            {
                "bold": "Owned platform reliability and SLA commitments for UnderwriteIQ cloud platform serving 20 enterprise carriers, ",
                "text": "defining 99.9% uptime targets and incident protocols. Reduced P1 resolution time and improved customer confidence through proactive operational health monitoring."
            },
            {
                "bold": "Reduced support ticket volume 31% by driving product improvements to IRP Navigator AI chatbot, ",
                "text": "partnering with Engineering to tune RAG architecture optimizations. Enhanced self-serve capabilities for enterprise users, mirroring AI-driven remediation patterns."
            }
        ]
        // ... rest of positions in next task
    }
}
```

### Task 2: Shorten Kyte and T-Mobile Bullets

**Files:**
- Modify: `output/paloaltonetworks/career-site/content/resume_content.json`

- [ ] **Step 1: Shorten Kyte bullets**

```json
"kyte": [
    {
        "bold": "Built company's first ML risk engine from 0-to-1, ",
        "text": "identifying $3M+ annual loss exposure. Partnered with Data Science to develop an XGBoost model, reducing losses 23% ($700K annually) and boosting revenue 7% by breaking the growth-versus-profitability tradeoff."
    },
    {
        "bold": "Doubled experiment velocity and enabled data-driven decision-making across the product org by building an in-house A/B testing platform. ",
        "text": "Led an 8-person cross-functional squad to deliver a unified testing framework used for all strategic product changes."
    },
    {
        "bold": "Reduced support ticket volume 30% and response time 50% by implementing automated chat workflows. ",
        "text": "Defined escalation logic and integrated NLP-based intent classification, driving autonomous operational efficiency."
    },
    {
        "bold": "Led product strategy for geographic market expansion, ",
        "text": "defining a launch playbook including pricing and success metrics. Expanded into 3 new markets, generating $2M+ incremental annual revenue within two quarters."
    },
    {
        "bold": "Established a structured customer discovery process surfacing 12 high-impact pain points that shaped the ML risk engine roadmap. ",
        "text": "Increased feature adoption 35% by replacing ad-hoc requests with evidence-based prioritization and deep user-session analysis."
    }
]
```

- [ ] **Step 2: Shorten T-Mobile bullets**

```json
"tmobile": [
    {
        "bold": "Defined API strategy for enterprise IoT connectivity platform, ",
        "text": "designing REST interfaces for device provisioning and management. Grew API adoption 3x among enterprise customers and OEM partners, enabling self-serve onboarding at scale."
    },
    {
        "bold": "Drove security and compliance requirements for IoT connectivity platform, ",
        "text": "including cellular authentication and enterprise data isolation standards. Achieved SOC 2 compliance and unblocked 4 enterprise deals with strict security prerequisites."
    },
    {
        "bold": "Owned cloud infrastructure strategy for IoT connectivity platform, ",
        "text": "managing compute, storage, and networking for a 10K+ device fleet. Partnered with Infrastructure on capacity planning, cost optimization, and scaling for global growth."
    }
]
```

### Task 3: Shorten Lyft and Allstate Bullets

**Files:**
- Modify: `output/paloaltonetworks/career-site/content/resume_content.json`

- [ ] **Step 1: Shorten Lyft and Allstate bullets**

```json
"lyft": [
    {
        "bold": "Established Lyft’s enterprise risk management function as the first actuarial hire, ",
        "text": "building frameworks for cyber and tech E&O exposure. Streamlined insurance data pipelines and reporting, reducing monthly close cycles by 3 days."
    }
],
"allstate": [
    {
        "bold": "Built internal ratemaking tool reducing pricing variance 8% and saving 60 quarterly person-hours. ",
        "text": "Led GLM-based driver risk modeling and introduced 2 new pricing factors across 14 states to optimize portfolio performance."
    }
]
```

- [ ] **Step 2: Finalize file**

Ensure the entire file is saved correctly with the updated tagline and shortened bullets.
Verify counts:
- Moody's: 6
- Kyte: 5
- T-Mobile: 3
- Lyft: 1
- Allstate: 1
Total: 16 bullets.

- [ ] **Step 3: Commit changes**

```bash
git add output/paloaltonetworks/career-site/content/resume_content.json
git commit -m "docs: shorten resume content for Palo Alto Networks to fit 2 pages"
```
