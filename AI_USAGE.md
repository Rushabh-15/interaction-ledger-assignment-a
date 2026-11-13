# AI Usage Disclosure
 
## Tools used
Claude (Anthropic), first as a tutor, then as the implementer of the whole solution.
 
## What I used them for
- **Planning and learning:** understanding the spec, the glossary and the data generator, and a
  step-by-step plan for the assignment.
- **Guided data exploration:** Claude gave me questions to answer by looking at the generated
  data, and explained the ones I couldn't answer (the three timestamp forms and why naive means
  IST, overrides that carry no phone, truncated JSON lines).
- **The implementation.** I fell behind my planned schedule after the exploration step, so I
  asked Claude to take over. **Claude designed and wrote all of the code in `solution/ledger/`,
  all of the tests in `solution/tests/`, and drafted `SUBMISSION.md` and this file.** I did not
  write any of the code.
- **Verification:** Claude wrote a separate, naive reference implementation (not committed) and
  compared its output with the solution on 8 seeds plus the large dataset. They were identical.
- **Debugging and review:** Claude diagnosed the install problem below, and at my request
  reviewed the whole repo against the assignment before submission and fixed what it found.
## What I wrote or decided myself
- Chose Assignment A, set up the environment, and generated the data.
- Explored the data by hand. I found that `batch_003` renames and re-orders the columns and adds an
  extra `agent_notes` column that must be ignored, that `+1 415 555 0134` must be rejected, and
  that rows like `,,,,,` are blank.
- Ran the pipeline end to end on my Mac, checked that re-running a drop skips every file, and
  timed the large dataset (ingest 23.8 s, report 7.0 s).
- Ran the diagnostic that confirmed the install problem below, and moved to the
  `PYTHONPATH` way of running it.
- Decided to hand the implementation over when I fell behind, and to disclose that fully here.
- **Not yet done:** I have not reviewed every module line by line. I am studying the code
  (`normalize.py` → `db.py` → `leads.py` → `events.py` → `derive.py` → `reports.py`) before the
  follow-up interview.
## Where the AI was wrong or unhelpful
1. **Broken install on macOS.** Claude's setup used `pip install -e ./solution` to create a
   `ledger` command. On my Mac it failed with `ModuleNotFoundError: No module named 'ledger'`.
   Claude suggested checking file flags. I ran `ls -lO` on site-packages, which showed the
   editable-install `.pth` file marked `hidden`; newer Python versions skip hidden `.pth`
   files. We switched to `PYTHONPATH=solution python -m ledger ...`, which needs no install.
2. **Wrong debugging hint.** It suggested `grep -v '"event_id"'` to find malformed event lines.
   That found nothing, because the truncated lines still contain the text `"event_id"`
   (e.g. `{"event_id": "evt_00002099"`). Parsing each line with `json.loads` found them.
3. **Crash on a second dataset.** Claude's first version raised a raw Python traceback
   (`BatchConflict`) when a different seed was ingested into an existing `ledger.db`, which a
   reviewer testing a new seed could easily hit. Claude found it in its own final review, not me.
   It now exits with a clear error and a hint, and there is a test for it.
## How I verified AI-generated code
- Ran `pytest solution` on my Mac: 77 passed.
- Ran ingest drop by drop, re-ran a drop (every file skipped), and ran the report.
- Ran the large dataset myself.
- Correctness against the spec rests mainly on Claude's checks: the independent reference
  implementation on 8 seeds, and its review of each rule. I haven't verified every edge-case
  rule independently.