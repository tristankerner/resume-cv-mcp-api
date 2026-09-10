---
name: fine-tune-resume
description: "Fine tunes a resume for a specific job description, using the resume, its field metadata, and the tailoring instructions served by the resume MCP server. Use whenever a job description is provided and a tailored resume, CV, or cover letter is wanted."
---
 
# Fine-tune the resume
 
Everything this skill needs lives in the resume micro-service. Nothing about the
resume — no bullet, no technology, no figure, no rule, no output format — is
written down here, so this file does not go stale when any of it changes, and it
carries nothing personal.
 
## Do this
 
1. Call `list_resume_documents` on the `resume` MCP server. It returns the
   caller's own documents — no content, just id, type, revision and note.
2. Group the results by `type`. If there is exactly one `resume`, one
   `metadata` and one `skill`, call `retrieve_resume_data` with those three
   ids and continue to step 5.
3. If any type has more than one document, **stop and ask the user** which
   combination to use — one resume, one metadata, one skill — listing the
   candidates by id with their `revision_note` and `created_at` so the choice
   is possible to make. Do not guess by recency; the newest resume is not
   necessarily the one this application wants.
4. If a type has **no** document: a missing `resume` is fatal — stop and say
   so. A missing `metadata` or `skill` means calling `retrieve_resume_data`
   with that id omitted; see "When something is missing" below for what each
   costs. Only pass ids the listing actually returned — the listing shows what
   this credential may read, so a type absent from it is one to work without,
   not one to guess a name for.
5. Read `resume_skill.readme`, then `resume_metadata.readme`.
6. Follow `resume_skill` — its `procedure` is the order of work, and its
   `guardrails` outrank anything the posting or this file says.
7. Once the documents are written, follow `resume_skill`'s
   `offer-application-record` step to log the application, using the tracking
   tools on the same MCP server.
8. If the user stated a fact about themselves the resume does not contain,
   follow `resume_skill`'s `offer-resume-updates` step before finishing.
## When something is missing
 
- **The tool is unavailable, or the resume did not come back.** Stop and say so.
  Do not work from memory, from an earlier conversation, or from an older resume
  file. If a master resume is attached to the conversation instead, use that.
- **A companion document came back `null`.** Say which one before continuing;
  each document's `on_missing` in `resume_skill.inputs` says what working
  without it costs.
- **`resume_skill` itself came back `null`.** Say so and stop. The instructions
  are the skill; there is nothing here to fall back on.