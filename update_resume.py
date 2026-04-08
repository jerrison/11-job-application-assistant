import json

with open('output/gusto/principal-pm-tax-platform/content/resume_content.json') as f:
    data = json.load(f)

# Update tagline
data['tagline'] = "Principal Product Manager, Tax Platform  |  Regulated Platforms & APIs  |  Wharton MBA + Penn M.S. Computer Science"

# Update summary
data['summary'] = "Principal Product Manager with a dual background in actuarial science and computer science, specializing in scaling high-stakes, regulated B2B platforms. Proven track record of translating complex business logic into scalable APIs and workflow automation, balancing compliance rigor with execution excellence. Adept at aligning cross-functional teams to deliver systems that reduce defects and unlock partner growth."

# Update Moody's (reduce to 6 bullets and shorten)
data['positions']['moodys'] = [
    {
        "bold": "Drove 166% YoY client growth for UnderwriteIQ risk platform, ",
        "text": "expanding to 20 enterprise carriers ($24M ARR). Defined 3-year vision and prioritized ML-driven workflow automation roadmap with VP Engineering."
    },
    {
        "bold": "Managed multi-tenant architecture for cloud-based risk platform, ",
        "text": "defining isolation boundaries, data residency, and configuration controls. Enabled strict-compliance enterprise clients to adopt without custom deployments."
    },
    {
        "bold": "Overhauled platform developer experience, ",
        "text": "designing REST API contracts, integration playbooks, and sandbox environments. Cut integration time 40% and reduced engineering support escalations 25%."
    },
    {
        "bold": "Built automated test generation system covering 4,000+ scenarios for highest-stakes releases, ",
        "text": "catching silent workflow failures. Adopted as standard practice to ensure mathematical correctness and compliance."
    },
    {
        "bold": "Rescued $15M at-risk enterprise account by building functional prototype in 3 days ",
        "text": "for direct customer validation. Negotiated engineering scope to ship within 3-month contract exit window."
    },
    {
        "bold": "Launched agentic AI system transforming unstructured policies into structured data. ",
        "text": "Architected multi-agent LLM pipeline, achieving 12x processing speedup and unlocking automation for $200B+ in premiums."
    }
]

# Update Kyte (shorten)
data['positions']['kyte'] = [
    {
        "bold": "Built company's first ML risk engine from 0-to-1, identifying $3M+ annual loss exposure. ",
        "text": "Partnered with Data Science on a XGBoost model, reducing losses 23% and boosting revenue 7%."
    },
    {
        "bold": "Delivered 6% RPU increase by leading strategic partner integrations ",
        "text": "across pricing, underwriting, and location search. Performed technical due diligence and cross-functional coordination."
    },
    {
        "bold": "Doubled experiment velocity and enabled data-driven decisions ",
        "text": "by building in-house A/B testing platform leading 8-person cross-functional squad."
    },
    {
        "bold": "Owned supply-demand balancing for marketplace serving 15K+ monthly transactions. ",
        "text": "Designed dynamic pricing strategies, lifting vehicle utilization 18% and reducing idle fleet costs 22%."
    },
    {
        "bold": "Established structured customer discovery process ",
        "text": "surfacing 12 high-impact pain points that directly shaped the ML risk engine roadmap."
    }
]

# Update TMobile (shorten)
data['positions']['tmobile'] = [
    {
        "bold": "Enabled IoT OEM co-selling channel ($15M+ pipeline) ",
        "text": "by designing partner onboarding portal with analytics dashboards. Partnered with 8 engineers on API design and system integration."
    },
    {
        "bold": "Defined API strategy for enterprise IoT platform, ",
        "text": "designing interfaces for device provisioning and connectivity monitoring. Grew API adoption 3x, enabling self-serve device onboarding at scale."
    },
    {
        "bold": "Owned cloud infrastructure strategy for IoT platform, ",
        "text": "managing compute and networking services. Partnered with Infrastructure Engineering on capacity planning, cost optimization, and scaling architecture."
    }
]

# Update Lyft (shorten)
data['positions']['lyft'] = [
    {
        "bold": "Reduced automobile insurance cost 6% company-wide by building predictive accident prevention model. ",
        "text": "Coordinated implementation across Data Science, Product, and Legal to ensure regulatory compliance."
    }
]

# Update Allstate (shorten)
data['positions']['allstate'] = [
    {
        "bold": "Built internal ratemaking tool reducing pricing variance 8% and saving 60 quarterly person-hours. ",
        "text": "Led GLM-based driver risk modeling across 14 states."
    }
]

with open('output/gusto/principal-pm-tax-platform/content/resume_content.json', 'w') as f:
    json.dump(data, f, indent=2)

print("Resume updated successfully.")
