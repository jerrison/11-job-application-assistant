# PART 1: RESUME TAILORING

---

## Resume Workflow

### Phase 1: Parse the JD & Research the Company (Deterministic + LLM)

**Do both of these before writing any content. The research informs the resume as much as the cover letter.**

#### 1a–1c. Run the Deterministic Pipeline (Preferred)

Use the orchestrator to run all deterministic steps in one command:

```bash
uv run scripts/run_pipeline.py <jd_source>
```

Where `<jd_source>` is a file path or URL. Company and role slugs are **auto-detected** from the parsed JD (company name + title). Override with `-c <company>` and/or `-r <role-slug>` if needed. For Greenhouse URLs, including company-hosted career pages that carry `gh_jid`, the API is called directly for better parsing. For Dover `app.dover.com/apply/...` URLs, the public application API is used directly for JD extraction so the deterministic pipeline avoids the Cloudflare-gated HTML shell. For company-hosted Ashby career pages that carry `ashby_jid`, the wrapper URL is resolved to the canonical `jobs.ashbyhq.com` posting before scraping and submit automation. Reject host-derived Ashby shell URLs whose `window.__appData` has no real `posting` payload, and fall back to the iframe/embed-discovered hosted-jobs slug in that case. Direct Ashby `jobs.ashbyhq.com/.../application` URLs are also canonicalized back to the posting URL before scrape so the deterministic pipeline reads the full posting instead of the thinner application shell, and thin same-site application shells should trigger a same-site search for the fuller JD page before extraction fails.

The orchestrator:
1. **Syncs `work_stories.md` and `candidate_context.md`** from Google Docs (hash-check, update only if changed). Use `--skip-sync` to skip.
2. **Scrapes the URL** if `<jd_source>` starts with `http` (Greenhouse and Dover APIs preferred when applicable, fallback to HTML scraping)
3. **Parses the JD** → `jd_parsed.json` (title, company, level, responsibilities, qualifications, keywords, signals)
4. **Auto-detects output directory** from company name and title → `output/<company>/<role-slug>/`
5. **Writes deterministic content artifacts under** `output/<company>/<role-slug>/content/`
6. **Ranks all bullets** → `content/ranked_bullets.json` (TF-IDF cosine similarity, cached bullet/story parsing)
7. **Drafts resume JSON** → `content/resume_content_draft.json` (pre-selected bullets, `summary: null` for LLM)

**No LLM tokens spent.** Review `jd_parsed.json` and `ranked_bullets.json` to understand the role before writing content.

<details>
<summary>Running steps individually (if needed)</summary>

```bash
uv run scripts/parse_jd.py <jd_source> -o output/<company>/<role-slug>/content/jd_parsed.json
uv run scripts/rank_bullets.py output/<company>/<role-slug>/content/jd_parsed.json -o output/<company>/<role-slug>/content/ranked_bullets.json
uv run scripts/draft_resume.py output/<company>/<role-slug>/content/ranked_bullets.json -o output/<company>/<role-slug>/content/resume_content_draft.json
```
</details>

#### 1d. Deep Company & Role Research (LLM)

Conduct the full research described in **CL Phase 1** (in `docs/cover-letter-generation.md`). This research is shared — it feeds into both the resume and the cover letter. **Cache company-level research** to `output/<company>/research_cache.json` so subsequent roles at the same company skip this step. **Cache role-specific research** to `{content_dir}/role_research_cache.json` (keyed by SHA-256 hash of `jd_parsed.json`).

Before researching, check if `output/<company>/research_cache.json` exists and is within the configurable TTL (default 30 days, override with `JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS`). If so, use the cached company research. Similarly, check `{content_dir}/role_research_cache.json` for role-specific research — it is fresh when the JD hash matches and the file is within TTL.

Key outputs that inform the resume:
- **Company/team language and terminology** — mirror it in bullet rewrites and the summary
- **Product domain context** — use it to frame the candidate's experience in terms the hiring manager thinks in
- **Strategic priorities and recent momentum** — tailor the summary to position the candidate as solving the company's current challenges
- **Team-specific culture signals** — match seniority framing and emphasis accordingly

### Phase 2: Tailor the Content (LLM)

**Start from `resume_content_draft.json`, not from scratch.** The draft already has the best-matching bullets pre-selected and pre-ordered. Your job is to:

1. **Review the draft selections** — override if the ranking missed something important. Check `_meta.bullets_excluded` for any bullets that should be added back.
2. **Rewrite bullet text** to mirror JD language, lead with impact, quantify results. Use work stories from `work_stories.md` to add specificity.
3. **Write the summary** — a sharp, high-signal pitch using JD keywords and company/domain language from Phase 1d research. Prefer 2-3 sentences, but use the length needed to communicate the candidate's fit clearly. The summary should read as if the candidate already speaks the company's language, not like a generic overview.
4. **Adjust the tagline** if the role demands a different framing, but only rewrite the first two segments. Keep the final credential segment as `Wharton MBA + Penn M.S. Computer Science`.
5. **Keep at least 6 Moody's bullets, at least 5 Kyte bullets, at least 3 T-Mobile bullets, at least 1 Lyft bullet, and at least 1 Allstate bullet** in the final resume, including after any validation-fix pass.
6. **You may rewrite and combine bullets** from the pool to create stronger, more targeted versions. Never fabricate accomplishments or metrics, but you may restructure and reframe real work.
7. **Determine `page_break_before` last** — after bullets and summary are finalized. Prefer the latest feasible break so page 1 is not sparse, but never sacrifice the right job-relevant bullet mix just to pack page 1. If the field is `null`, the deterministic pipeline will auto-balance it before build.

Save the final content to `output/<company>/<role-slug>/content/resume_content.json`.

**Field reference:**
- `tagline` — the subtitle line under the name. Usually keep as-is unless the role demands a different framing.
  Only the first two segments should change for a job-specific variant. Keep the final credential segment as `Wharton MBA + Penn M.S. Computer Science`.
- `summary` — a sharp, high-signal pitch tailored to the JD. Prefer 2-3 sentences, but use the length needed to communicate enough, or `null` to omit the summary section.
- `positions` — each key is a position ID (`moodys`, `kyte`, `tmobile`, `lyft`, `allstate`). Value is an array of bullet objects. Each bullet has `bold` (the leading phrase, bolded) and `text` (the rest, normal weight). Omit a position key entirely to exclude that position (not recommended).
- `page_break_before` — position ID where page 2 starts. Set this only after the final bullets and summary are decided. Prefer the latest feasible break so page 1 is not sparse. `null` is allowed; the deterministic pipeline will choose the break before build.

### Phase 3: Build, Validate, and Enforce 2-Page Constraint

Finalize the page break after content is settled:

```bash
uv run scripts/optimize_page_break.py output/<company>/<role-slug>/content/resume_content.json
```

Then run the deterministic builder:

```bash
uv run scripts/build_resume.py output/<company>/<role-slug>/content/resume_content.json -o "output/<company>/<role-slug>/documents/Jerrison Li Resume - <Company>.docx"
```

The builder handles all formatting, produces both `.docx` and `.pdf`, and **auto-validates** the PDF (page count = 2, candidate name present). Use `--dry-run` to skip PDF conversion for faster iteration.

**If the resume exceeds 2 pages:** Shorten bullet text → cut low-value bullets → remove summary (`"summary": null`) → re-run optimizer.
**If too short for 2 pages:** Add bullets from the pool → expand summary only if needed → re-run optimizer.
Keep **at least 6 Moody's, 5 Kyte, 3 T-Mobile, 1 Lyft, and 1 Allstate bullets** regardless.

Regenerate after each change. Use `--dry-run` for fast iteration, then drop the flag for the final build.

### Phase 5: Deliver

The builder script automatically converts the `.docx` to `.pdf` using LibreOffice (headless) and auto-validates. Both files are saved to the output directory.

**To build both resume and cover letter**, use the orchestrator's `--build` flag:

```bash
uv run scripts/run_pipeline.py <jd_source> --build
```

This runs `build_resume.py` and `build_cover_letter.py` sequentially and validates the PDF (requires `content/resume_content.json` and `content/cover_letter_text.txt` to exist in the role output directory).

Deliver both the `.docx` and `.pdf` for each.

---

## Resume Constraint Rules — HARD RULES

### Rule 1: Allowed Changes (Exhaustive)

| Element | Allowed Actions |
|---|---|
| **Bullet points** under each position | Select from master resume pool, rewrite, reorder, merge, combine, or **delete** |
| **Summary section text** | Replace entirely, or **remove the section** |
| **Tagline** | Rewrite to reframe for the target role |
| **page_break_before** | Change to balance content across pages, but only after bullets and summary are final |

### Rule 2: Forbidden Changes

You may **NOT** change:
- The candidate's name or contact info
- Job titles, company names, locations, or date ranges
- Section headers or their order (Summary → Experience → Education → Skills)
- Education content
- Skills & Additional content
- Any formatting — all formatting is controlled by the deterministic builder script

Resume header note:
For roles whose listed locations include California, keep `San Francisco, CA` at the start of the contact line. For roles outside California, omit that city/state prefix so the line starts with `jerrisonli@gmail.com`.

Submit runtime note:
For browser-based job-board submits, prefer the persistent local Chrome-backed Playwright profile at `~/.job-assets/playwright-submit-profile` before falling back to bundled Chromium. This reduces avoidable CAPTCHA challenges from fresh ephemeral browser sessions. `JOB_ASSETS_SUBMIT_BROWSER_PROFILE_DIR` overrides the profile path and `JOB_ASSETS_SUBMIT_SLOW_MO_MS` overrides headed interaction pacing. On macOS, if the persistent-profile launch aborts early, retry once without persistence before failing the local-browser path, and avoid redundant same-browser fallbacks that would immediately relaunch the same Chrome app under a different label. On non-macOS persistent-profile launches, override inherited `HOME` and `XDG_*` paths to the submit-profile home so CI and headless Linux runs stay isolated from the runner's global browser state.
On macOS, keep the native GUI environment for headed local Chrome launches. Do not rewrite `HOME` or `XDG_*` there; Playwright's persistent user-data dir already isolates the session, and synthetic home directories can crash AppKit/HIServices startup. For headless local Chrome launches, prefer an isolated submit-home so Chrome state such as Crashpad does not point at sandbox-protected user directories.

### Rule 3: Authenticity & Meaning Preservation

All bullet content must come from or be grounded in the **master resume bullet pool**. You may rewrite, combine, reframe, and sharpen bullets from the pool — but:

- **Never fabricate accomplishments, metrics, technologies, or responsibilities** that don't appear in the master resume.
- **Never change the meaning of a metric or claim.** "Increased underwriter productivity 60%" must NOT become "Increased platform adoption 60%" — these are different things. You may rephrase for flow or emphasis, but the underlying claim must remain factually identical to the source material.
- **When adapting language to match a JD**, change framing and emphasis, not substance. Example: reordering a bullet to lead with the IoT angle is fine; changing what was actually measured is not.

### Rule 4: Formatting is Deterministic

All formatting (fonts, sizes, colors, margins, spacing, bullet styling) is handled by `scripts/build_resume.py`. **Do NOT attempt to control formatting through the JSON content.** The builder script exactly replicates the Google Doc template. Your only job is to produce the right content.

### Rule 5: Exactly 2 Pages

The final `.docx` must render as **exactly 2 pages**. Not 1. Not 3. Achieve this through content changes only:
1. **Content**: Rewrite bullets more concisely, delete low-impact bullets, expand high-impact bullets
2. **Summary**: Remove it (`"summary": null`) if it frees needed space; if you add or expand it, make sure it communicates enough first, then keep it as tight as possible
3. **Page break placement**: Finalize `page_break_before` after the content is settled so page 1 is as full as possible without sacrificing the right bullets

### Rule 6: No Position Splits Across Pages

Every position block (company + title + location/dates + all bullets) must appear **entirely on one page**. The `page_break_before` field in the JSON controls where page 2 starts — set it to a position ID so the break falls between positions.

---

## Resume Tailoring Philosophy

**Optimize for interview conversion. Be aggressive, not generic.**

- **Cherry-pick from the bullet pool.** The master resume is a menu — select the combination that makes the strongest case for this specific role. Don't default to the Google Doc's current selection.
- **Mirror the JD's language.** If the posting says "cross-functional stakeholder alignment," use that phrase — don't say "worked with other teams."
- **Lead with impact.** Every bullet: strong action verb → what you did → quantified result.
- **Keep the summary sharp, but sufficient.** It must communicate the candidate's fit clearly before you optimize for brevity. Make it as tight as possible without dropping important signal, and never let it turn into a padded paragraph that repeats the bullets.
- **Use ownership and leadership verbs for PM roles.** Product manager bullets must open with verbs that convey ownership: Launched, Drove, Shipped, Designed, Pioneered, Owned, Led, Defined, Established, Spearheaded, Orchestrated. Avoid passive or weak verbs like Created, Made, Helped, Assisted, Participated, Worked on, Was responsible for, Increased/Reduced (use "Drove X% increase/reduction" instead).
- **Prioritize by relevance.** Most relevant bullets first within each position. Cut aggressively — a shorter, sharper resume beats a comprehensive one.
- **Page 1 density matters, but relevance matters more.** Prefer a fuller first page when possible, but never keep or drop the wrong bullet just to manipulate whitespace.
- **Match seniority signals.** Staff/Principal roles → emphasize strategy, cross-org influence, systems thinking, vision-setting. Mid-level roles → emphasize execution, delivery, direct IC contribution.
- **Preserve the candidate's voice.** The resume should sound like the same person, just more precisely targeted.
- **Every line must pull its weight.** If a bullet doesn't make the hiring manager think "this person can do this job," it doesn't belong on this version of the resume.

---

## Resume Output Requirements

### Deliver:
1. The `.docx` file — named `Jerrison Li Resume - <Company>.docx`
2. The `.pdf` file — named `Jerrison Li Resume - <Company>.pdf`

### Then provide a brief changelog:
- Summary: rewritten / removed / unchanged
- For each position: which bullets were rewritten, added, deleted, or reordered (brief description)
- Page split: which positions are on page 1 vs. page 2
- Confidence level in 2-page / no-split constraint (and how you verified)

### Do NOT:
- Show a full text preview of the resume in chat
- Over-explain your process — the file is the deliverable, the changelog is the explanation

---

## Resume Edge Cases

- **Google Doc not accessible**: Ask user to check sharing permissions (must be viewable via link)
- **Job link behind login wall**: Ask user to paste the job description text
- **Resume currently 1 page**: Expand bullets with richer detail from the master resume pool to fill 2 pages — do not fabricate
- **Resume currently 3+ pages**: Aggressively condense — delete low-relevance bullets first
- **Multiple fonts in source**: Preserve each font in its original context
- **JD is vague or short**: Do your best with available signals; ask user for additional context about what to emphasize if truly ambiguous
