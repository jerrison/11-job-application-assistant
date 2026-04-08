# Design Doc: Snowflake Senior PM - Applied AI Material Tailoring

**Date:** 2026-04-01
**Topic:** Tailoring Resume and Cover Letter for Snowflake Senior Product Manager - Applied AI

## 1. Objective
Produce a high-signal, tailored resume (`resume_content.json`) and cover letter (`cover_letter_text.txt`) for Jerrison Li applying to the Senior PM - Applied AI role at Snowflake. The materials must emphasize technical depth in AI productization, developer experience, and enterprise platform scaling.

## 2. Resume Design
### Tagline
- **Current:** Principal Product Manager | AI/ML & Enterprise B2B | Wharton MBA + Penn M.S. Computer Science
- **Proposed:** Senior Product Manager | Applied AI & Enterprise Platform | Wharton MBA + Penn M.S. Computer Science
- **Rationale:** Aligns with the target role title ("Senior Product Manager") and emphasizes the "Applied AI" and "Platform" focus of the Snowflake role.

### Summary
- **Content:** 2-3 sentences. Keywords: Applied AI, SQL-native, developer-centric, enterprise platform, Wharton MBA, Penn CS.
- **Draft:** Technically-grounded Product Manager with a Wharton MBA and Penn M.S. in Computer Science, specialized in productizing Applied AI and enterprise data platforms. Expert at delivering SQL-native AI primitives and developer-centric tools that transform complex technical capabilities into scalable business value in high-growth environments.

### Experience Tailoring
- **Moody’s (7-8 bullets):** 
    - Prioritize **SlipStream** (LLM pipelines, multimodal, agentic).
    - Prioritize **DX/API Overhaul** (developer ergonomics).
    - Prioritize **Workflow Builder** (automation/agentic).
    - Prioritize **UnderwriteIQ** (scale, ARR growth).
    - Prioritize **Cloud Migration** (enterprise platform).
    - Prioritize **RAG Chatbot** (applied AI).
    - Prioritize **Prototype/Rescue** (high-ambiguity execution).
- **Kyte (6 bullets):**
    - Prioritize **ML Risk Engine** (0-to-1 Applied AI).
    - Prioritize **A/B Testing Platform** (developer focus).
    - Prioritize **Partner Integrations** (technical due diligence).
    - Prioritize **Matching Algorithm** (technical optimization).
    - Prioritize **Market Expansion** (GTM strategy).
    - Prioritize **Customer Discovery** (product sense).
- **T-Mobile (3 bullets):**
    - Prioritize **API Strategy** (developer-facing interfaces).
    - Prioritize **OEM Portal** (technical scaling).
    - Prioritize **Cloud Infrastructure** (platform architecture).

## 3. Cover Letter Design
### Narrative Structure
1.  **Opening:** Connect to Snowflake's vision of the "Agentic Enterprise" and the recent GA of Cortex AI Functions. Frame Jerrison as a builder of the technical primitives that enable this vision.
2.  **Moody's/SlipStream:** Detail the creation of SlipStream. Focus on the multimodal nature (extracting data from PDFs) and the "SQL-native" analogy (making complex AI available as a simple platform primitive). Emphasize the 95% accuracy bar for enterprise production.
3.  **Kyte/Technical Depth:** Discuss building the ML risk engine and the A/B testing platform. This demonstrates the ability to build for both internal "developer" customers (data scientists) and external business outcomes.
4.  **Fit & Culture:** Mention the Wharton/Penn CS background. Align with Snowflake's "Get It Done" and "Integrity Always" values. Express excitement for the Applied AI / Cortex team's roadmap.
5.  **Closing:** Professional call to action.

### Style Constraints
- 300-450 words.
- No Unicode em dashes (—). Use hyphens or commas.
- Grounded in `research_cache.json`.

## 4. Technical Implementation
- Use `write_file` to save `output/snowflake/senior-pm-applied-ai/content/resume_content.json`.
- Use `write_file` to save `output/snowflake/senior-pm-applied-ai/content/cover_letter_text.txt`.
- No build scripts.

## 5. Success Criteria
- Resume is exactly 2 pages (managed by pipeline, but content must be appropriately sized).
- Summary is non-null.
- Bullet counts meet minimums: Moody's (6), Kyte (5), T-Mobile (3).
- Tagline matches target title.
- Materials reflect Snowflake's "Agentic Enterprise" and Cortex strategy.
