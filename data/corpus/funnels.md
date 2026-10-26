# Funnels

A funnel measures how many entities complete a sequence of steps in order, and where
they drop off.

## Configuration

| Setting | Default | Allowed |
|---|---|---|
| Steps | 2 | 2 to 10 |
| Conversion window | 7 days | 1 to 30 days |
| Ordering | ordered | ordered or unordered |
| Entity | user | user or account |
| Breakdown | none | up to 2 properties |

The **conversion window** is the maximum time allowed between the first step and the
last step, not between adjacent steps. A 7-day window with three steps allows step one
to step three to span seven days total.

## Ordered versus unordered

An ordered funnel requires steps to happen in sequence. An unordered funnel asks only
whether all steps happened inside the window, in any order. Use unordered when the
product genuinely allows steps in any order, such as "created a report" and "shared a
report" - otherwise ordered will undercount.

## Counting rules

Each entity is counted **once** per funnel, at its first conversion. Repeated
completions do not inflate the conversion rate. To measure repeat behaviour, use a
retention chart instead.

Step one defines the denominator. If step one is "signed up", conversion is measured
against everyone who signed up in the period, including entities who signed up before
the period started if they appear in the window. This surprises people: the denominator
is the count of step-one completions inside the selected date range.

## Multi-path funnels

A step may contain several events joined by `or`, which is how you model a step that can
be satisfied by any of several actions, such as "invited a teammate" via email or via
link. Multi-path steps count as one step for window and ordering purposes.

## Breakdowns

Breaking down by a property splits the funnel into parallel funnels. High-cardinality
breakdowns are rejected: any property with more than 500 distinct values in the window
cannot be used as a funnel breakdown. Aggregate to a lower-cardinality property first.

## Interpreting drop-off

A large drop between two steps usually means one of three things: the event is
misnamed on the client, the step genuinely requires a permission the user lacks, or the
conversion window is too short for the workflow. Check event volume before assuming the
product is at fault.
