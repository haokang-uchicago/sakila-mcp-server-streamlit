# After the fixes (Part 4 evidence)

The same questions, asked again in the Streamlit app after the fixes of 2026-11-03.
Pair each file with the one of the same question number in ../before/.

| File | Question | What changed |
|---|---|---|
| q1_vague_answer | How are we doing? | Still a good answer: states its reading (revenue), the totals, the gap months, two sharper questions. Every number is in this turn's results, so the number check stays quiet. |
| q3_actor_no_false_alarm | Which actor brings in the most revenue? | The false alarm is gone: the number check now ignores years and dates ("July 2005"). |
| q4_2006_1..3 | Why did revenue drop so much in 2006? | The model now queries revenue by month itself instead of reusing numbers from an earlier answer. The chart keeps 2005-09..2006-01 as empty slots, so the gap is visible instead of looking like a fall from August to February. |
| q5b_sql_in_category_refused | Show revenue for the category "Action'; DROP TABLE ...; --" in July 2005. | The model no longer cleans the value to "Action" and runs it: it refuses and asks whether plain Action was meant. The app adds its own notes ("looks like SQL", "no query ran"). One flaw left: the answer says it is "passing it through unchanged", but no tool was called at all, so the allow-list still never saw the value. |
| q6a / q6b_category_july_1..2 | What was Action revenue in July 2005? then Revenue by category in July 2005 | No carry-over: one aggregate call, 16 rows, no category filter. The answer names the top and bottom categories in words instead of repeating the full table the app already shows. |

Not re-shot: q2 (customers), q5a (delete), q7 (store filter) and q8 (cost) were already handled well; see ../before/.
